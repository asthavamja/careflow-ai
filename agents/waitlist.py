import re
import secrets
from zoneinfo import ZoneInfo

from agents.notify import send_via_n8n
from api.db import get_connection

IST = ZoneInfo("Asia/Kolkata")
EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class WaitlistJoinError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _waitlist_email(*candidates) -> str | None:
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        email = candidate.strip()
        if (
            EMAIL_PATTERN.fullmatch(email)
            and not email.lower().endswith("@example.com")
        ):
            return email.lower()
    return None


def _setting(conn, key, default):
    row = conn.execute(
        "select value from careflow.system_settings where key = %s", (key,)
    ).fetchone()
    return int(row["value"]) if row else default


def offer_freed_slot(slot_id: int):
    """Reserve a freed slot for the first suitable waiting patient. Returns the offer, or None."""
    with get_connection() as conn:
        minutes = _setting(conn, "waitlist_hold_minutes", 120)
        cutoff = _setting(conn, "slot_release_cutoff_hours", 4)
        slot = conn.execute(
            """select s.id, s.start_time, s.doctor_id, doc.department_id,
                      doc.name as doctor_name, dep.name as department
               from careflow.time_slots s
               join careflow.doctors doc on doc.id = s.doctor_id
               join careflow.departments dep on dep.id = doc.department_id
               where s.id = %s and s.status = 'free' and s.start_time > now()""",
            (slot_id,),
        ).fetchone()
        if slot is None:
            return None

        # Skip anyone who already missed this same slot (their row still remembers it)
        wl = conn.execute(
            """select w.id, w.patient_id from careflow.waitlist w
               where w.status = 'waiting' and w.department_id = %(dept)s
                 and (w.doctor_id is null or w.doctor_id = %(doc)s)
                 and (w.preferred_from is null or w.preferred_from <= %(start)s)
                 and (w.preferred_to is null or w.preferred_to >= %(start)s)
                 and w.offered_slot_id is distinct from %(slot)s
               order by w.created_at limit 1 for update skip locked""",
            {"dept": slot["department_id"], "doc": slot["doctor_id"],
             "start": slot["start_time"], "slot": slot_id},
        ).fetchone()
        if wl is None:
            return None

        held = conn.execute(
            """update careflow.time_slots
               set status = 'held',
                   held_until = least(now() + make_interval(mins => %(m)s),
                                      start_time - make_interval(hours => %(h)s))
               where id = %(id)s and status = 'free'
                 and least(now() + make_interval(mins => %(m)s),
                           start_time - make_interval(hours => %(h)s)) > now()
               returning held_until""",
            {"m": minutes, "h": cutoff, "id": slot_id},
        ).fetchone()
        if held is None:
            return None

        code = secrets.token_hex(4).upper()
        conn.execute(
            """update careflow.waitlist
               set status = 'offered', offered_slot_id = %s, offered_at = now(), offer_code = %s
               where id = %s""",
            (slot_id, code, wl["id"]),
        )
        patient = conn.execute(
            "select first_name, email from careflow.patients where id = %s", (wl["patient_id"],)
        ).fetchone()

    # The email goes out after the reservation is saved
    when = slot["start_time"].astimezone(IST).strftime("%A, %d %B %Y at %I:%M %p")
    until = held["held_until"].astimezone(IST).strftime("%d %B at %I:%M %p")
    body = (
        f"Hello {patient['first_name']},\n\n"
        f"Good news: a slot has opened up in {slot['department']}.\n\n"
        f"Doctor: {slot['doctor_name']}\n"
        f"When: {when}\n\n"
        f"This slot is reserved for you until {until} (IST). "
        f"Your offer code is: {code}\n\n"
        "If we do not hear from you by then, the slot will be offered to the next person.\n\n"
        "CareFlow Demo Hospital (simulated)"
    )
    try:
        res = send_via_n8n(
            request_id=None, patient_id=wl["patient_id"],
            key=f"waitlist-{wl['id']}-offer-{slot_id}", kind="waitlist_offer",
            to_email=patient["email"], subject="A slot has opened up for you",
            body=body, template="waitlist_offer_v1", meta={"waitlist_id": wl["id"]},
        )
        sent = res["status"] in ("sent", "already_sent")
    except Exception:
        sent = False
    return {"waitlist_id": wl["id"], "email_sent": sent}


def accept_waitlist_offer(offer_code: str) -> dict:
    code = (offer_code or "").strip().upper()
    if not code:
        raise ValueError("Offer code is required")

    with get_connection() as conn:
        wl = conn.execute(
            """select w.id, w.patient_id, w.offered_slot_id
               from careflow.waitlist w
               where upper(w.offer_code) = %s and w.status = 'offered'
               for update of w""",
            (code,),
        ).fetchone()
        if wl is None:
            raise ValueError("Waitlist offer is invalid or already used")

        slot = conn.execute(
            """update careflow.time_slots set status = 'booked', held_until = null
               where id = %s and status = 'held'
                 and held_until > now() and start_time > now()
               returning id""",
            (wl["offered_slot_id"],),
        ).fetchone()
        if slot is None:
            raise ValueError("Waitlist offer has expired")

        appt = conn.execute(
            """insert into careflow.appointments (patient_id, slot_id)
               values (%s, %s) returning id, appointment_code""",
            (wl["patient_id"], slot["id"]),
        ).fetchone()
        conn.execute(
            "update careflow.waitlist set status = 'booked' where id = %s", (wl["id"],)
        )

    return {
        "status": "booked",
        "message": "Your waitlist slot is booked.",
        "appointment_id": appt["id"],
        "appointment_code": appt["appointment_code"],
    }


def expire_waitlist_offers() -> dict:
    """Release expired holds. The patient goes back to waiting; the slot goes to the next person."""
    freed_slots = []
    with get_connection() as conn:
        rows = conn.execute(
            """select w.id, w.offered_slot_id
               from careflow.waitlist w
               join careflow.time_slots s on s.id = w.offered_slot_id
               where w.status = 'offered' and s.status = 'held' and s.held_until <= now()
               order by s.held_until
               for update of w skip locked"""
        ).fetchall()
        for row in rows:
            released = conn.execute(
                """update careflow.time_slots set status = 'free', held_until = null
                   where id = %s and status = 'held' and held_until <= now()
                   returning id""",
                (row["offered_slot_id"],),
            ).fetchone()
            if released:
                conn.execute(
                    """update careflow.waitlist
                       set status = 'waiting', offered_at = null, offer_code = null
                       where id = %s""",
                    (row["id"],),
                )
                freed_slots.append(released["id"])

    offered = 0
    for slot_id in freed_slots:
        try:
            if offer_freed_slot(slot_id):
                offered += 1
        except Exception:
            pass
    return {"expired": len(freed_slots), "offered_next": offered}


def notify_no_slots_patient(request_id: int, patient_id: int, department_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            """select r.contact_email, p.email as patient_email,
                      p.is_active, p.merged_into_id, d.name as department_name
               from careflow.patient_requests r
               join careflow.patients p on p.id = r.patient_id
               join careflow.departments d on d.id = %s
               where r.id = %s and r.patient_id = %s""",
            (department_id, request_id, patient_id),
        ).fetchone()
    if row is None or not row["is_active"] or row["merged_into_id"] is not None:
        return {"sent": False, "error": "identified_patient_or_department_unavailable"}

    email = _waitlist_email(row["patient_email"], row["contact_email"])
    if email is None:
        return {"sent": False, "error": "missing_deliverable_patient_email"}

    body = (
        f"No appointment slots are currently available in {row['department_name']}.\n\n"
        "You can join the department's waitlist through CareFlow. "
        "If you join, we'll email you if a matching slot becomes available.\n\n"
        "CareFlow Demo Hospital (simulated)"
    )
    try:
        result = send_via_n8n(
            request_id=request_id,
            patient_id=patient_id,
            key=f"request-{request_id}-no-slots-waitlist",
            kind="no_slots_waitlist_offer",
            to_email=email,
            subject="No appointment slots are currently available",
            body=body,
            template="no_slots_waitlist_v1",
        )
    except Exception as exc:
        return {"sent": False, "error": type(exc).__name__}
    return {
        "sent": result["status"] in ("sent", "already_sent"),
        "error": result.get("error"),
    }


def join_waitlist_for_request(
    request_id: int,
    preferred_from=None,
    preferred_to=None,
) -> dict:
    with get_connection() as conn:
        request = conn.execute(
            """select r.status, r.patient_id, r.department_id, r.offered_slots,
                      r.contact_email, p.email as patient_email, p.is_active,
                      p.merged_into_id, d.name as department_name
               from careflow.patient_requests r
               left join careflow.patients p on p.id = r.patient_id
               left join careflow.departments d on d.id = r.department_id
               where r.id = %s
               for update of r""",
            (request_id,),
        ).fetchone()
        if request is None:
            raise WaitlistJoinError(404, "Request not found")
        if request["status"] != "waiting_patient":
            raise WaitlistJoinError(409, "Request is not waiting for a no-slots decision")
        if (
            request["patient_id"] is None
            or not request["is_active"]
            or request["merged_into_id"] is not None
        ):
            raise WaitlistJoinError(409, "Only an identified registered patient can join")
        if request["department_id"] is None or request["department_name"] is None:
            raise WaitlistJoinError(409, "The request has no department assigned")
        if request["offered_slots"]:
            raise WaitlistJoinError(409, "This request already has appointment slots offered")

        email = _waitlist_email(request["patient_email"], request["contact_email"])
        if email is None:
            raise WaitlistJoinError(
                409, "A deliverable patient email is required to join the waitlist",
            )

        # Serialize joins for the same patient and department before checking duplicates.
        conn.execute(
            "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"waitlist:{request['patient_id']}:{request['department_id']}",),
        )
        duplicate = conn.execute(
            """select 1 from careflow.waitlist
               where patient_id = %s and department_id = %s
                 and status in ('waiting', 'offered')
               limit 1""",
            (request["patient_id"], request["department_id"]),
        ).fetchone()
        if duplicate is not None:
            raise WaitlistJoinError(
                409, "You are already waiting for or have an active offer in this department",
            )

        waitlist_row = conn.execute(
            """insert into careflow.waitlist
                   (patient_id, department_id, doctor_id, preferred_from,
                    preferred_to, status)
               values (%s, %s, null, %s, %s, 'waiting')
               returning id""",
            (
                request["patient_id"],
                request["department_id"],
                preferred_from,
                preferred_to,
            ),
        ).fetchone()
        conn.execute(
            """update careflow.follow_ups set status = 'cancelled'
               where request_id = %s
                 and kind in ('patient_slot_reminder', 'patient_slot_expiry')
                 and status = 'pending'""",
            (request_id,),
        )
        conn.execute(
            """update careflow.patient_requests
               set status = 'closed', updated_at = now()
               where id = %s and status = 'waiting_patient'""",
            (request_id,),
        )
        conn.execute(
            """insert into careflow.request_events
                   (request_id, from_status, to_status, actor, note)
               values (%s, 'waiting_patient', 'closed', 'system',
                       'Patient joined the department waitlist')""",
            (request_id,),
        )

    body = (
        f"You are on the waitlist for {request['department_name']}.\n\n"
        "We'll email you if a matching appointment slot becomes available.\n\n"
        "CareFlow Demo Hospital (simulated)"
    )
    try:
        result = send_via_n8n(
            request_id=request_id,
            patient_id=request["patient_id"],
            key=f"request-{request_id}-waitlist-joined",
            kind="waitlist_joined",
            to_email=email,
            subject="You are on the waitlist",
            body=body,
            template="waitlist_joined_v1",
        )
    except Exception as exc:
        return {
            "status": "joined",
            "waitlist_id": waitlist_row["id"],
            "email_sent": False,
            "email_error": type(exc).__name__,
        }

    return {
        "status": "joined",
        "waitlist_id": waitlist_row["id"],
        "email_sent": result["status"] in ("sent", "already_sent"),
        "email_error": result.get("error"),
    }


def expire_waiting_entries() -> dict:
    with get_connection() as conn:
        days = _setting(conn, "waitlist_expire_days", 30)
        if days <= 0:
            raise ValueError("waitlist_expire_days must be a positive number")
        row = conn.execute(
            """with expired as (
                   update careflow.waitlist
                   set status = 'expired'
                   where status = 'waiting'
                     and created_at <= now() - make_interval(days => %s)
                   returning id
               )
               select count(*) as expired from expired""",
            (days,),
        ).fetchone()
    return {"expired": int(row["expired"]), "days": days}