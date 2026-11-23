import secrets
from zoneinfo import ZoneInfo

from agents.notify import send_via_n8n
from api.db import get_connection

IST = ZoneInfo("Asia/Kolkata")


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

        wl = conn.execute(
            """select w.id, w.patient_id from careflow.waitlist w
               where w.status = 'waiting' and w.department_id = %(dept)s
                 and (w.doctor_id is null or w.doctor_id = %(doc)s)
                 and (w.preferred_from is null or w.preferred_from <= %(start)s)
                 and (w.preferred_to is null or w.preferred_to >= %(start)s)
               order by w.created_at limit 1 for update skip locked""",
            {"dept": slot["department_id"], "doc": slot["doctor_id"], "start": slot["start_time"]},
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
            key=f"waitlist-{wl['id']}-offer", kind="waitlist_offer",
            to_email=patient["email"], subject="A slot has opened up for you",
            body=body, template="waitlist_offer_v1", meta={"waitlist_id": wl["id"]},
        )
        sent = res["status"] in ("sent", "already_sent")
    except Exception:
        sent = False
    return {"waitlist_id": wl["id"], "email_sent": sent}