import logging
from datetime import datetime, timedelta, timezone
from agents.notify import send_via_n8n
from agents.alerts import send_staff_notice
from agents.alerts import send_urgent_alert
from agents.confirm import send_confirmation
from agents.documents import (
    expire_document_request,
    get_document_reminder_context,
    send_document_reminder,
)
from api.db import get_connection
from agents.waitlist import expire_waitlist_offers, expire_waiting_entries

logger = logging.getLogger("careflow")

LEASE_MINUTES = 2
BATCH = 10


def _claim_urgent():
    """Take due re-alerts. The due time moves forward 2 minutes, so an overlapping tick skips them."""
    with get_connection() as conn:
        return conn.execute(
            """update careflow.follow_ups
               set due_at = now() + make_interval(mins => %s),
                   attempt_count = attempt_count + 1
               where id in (
                   select id from careflow.follow_ups
                   where kind = 'urgent_realert' and status = 'pending' and due_at <= now()
                   order by due_at limit %s for update skip locked)
               returning id, request_id""",
            (LEASE_MINUTES, BATCH),
        ).fetchall()


def _finish(follow_up_id, status):
    with get_connection() as conn:
        conn.execute(
            """update careflow.follow_ups
               set status = %s,
                   sent_at = case when %s = 'sent' then now() else sent_at end
               where id = %s""",
            (status, status, follow_up_id),
        )


def _request_status(request_id):
    with get_connection() as conn:
        return conn.execute(
            "select status from careflow.patient_requests where id = %s", (request_id,)
        ).fetchone()["status"]


def run_urgent_realerts() -> dict:
    out = {"claimed": 0, "sent": 0, "cancelled": 0, "failed": 0}
    for fu in _claim_urgent():
        out["claimed"] += 1
        if _request_status(fu["request_id"]) != "urgent_escalated":
            _finish(fu["id"], "cancelled")      # someone already handled it
            out["cancelled"] += 1
            continue
        try:
            res = send_urgent_alert(fu["request_id"], repeat=True, attempt_no=2)
            ok = res["status"] in ("sent", "already_sent")
        except Exception:
            ok = False
        if ok:
            _finish(fu["id"], "sent")
            out["sent"] += 1
        else:
            out["failed"] += 1                  # stays pending and is retried after the lease
    return out

def _minutes(key, default):
    with get_connection() as conn:
        row = conn.execute(
            "select value from careflow.system_settings where key = %s", (key,)
        ).fetchone()
    return int(row["value"]) if row else default


def _waited(created_at):
    return int((datetime.now(timezone.utc) - created_at).total_seconds() // 60)


def run_approval_reminders() -> dict:
    wait = _minutes("approval_reminder_minutes", 60)
    out = {"claimed": 0, "sent": 0, "failed": 0}
    with get_connection() as conn:
        rows = conn.execute(
            """update careflow.approvals set reminder_sent_at = now()
               where id in (
                   select id from careflow.approvals
                   where decision = 'pending' and level = 1 and reminder_sent_at is null
                     and created_at <= now() - make_interval(mins => %s)
                   order by created_at limit %s for update skip locked)
               returning id, request_id, reason_for_review, created_at""",
            (wait, BATCH),
        ).fetchall()
    for a in rows:
        out["claimed"] += 1
        msg = (f"APPROVAL WAITING\nApproval #{a['id']} (request #{a['request_id']})\n"
               f"Reason: {a['reason_for_review']}\nWaiting: {_waited(a['created_at'])} minutes\n"
               "Please review in CareFlow.")
        try:
            res = send_staff_notice(a["request_id"], f"approval-{a['id']}-reminder",
                                    "approval_reminder", msg)
            ok = res["status"] in ("sent", "already_sent")
        except Exception:
            ok = False
        if ok:
            out["sent"] += 1
        else:
            out["failed"] += 1
            with get_connection() as conn:       # give it back so a later tick retries
                conn.execute("update careflow.approvals set reminder_sent_at = null where id = %s", (a["id"],))
    return out


def run_approval_escalations() -> dict:
    wait = _minutes("approval_escalate_minutes", 120)
    out = {"claimed": 0, "sent": 0, "failed": 0}
    with get_connection() as conn:
        rows = conn.execute(
            """update careflow.approvals set level = 2, escalated_at = now()
               where id in (
                   select id from careflow.approvals
                   where decision = 'pending' and level = 1 and escalated_at is null
                     and created_at <= now() - make_interval(mins => %s)
                   order by created_at limit %s for update skip locked)
               returning id, request_id, reason_for_review, created_at""",
            (wait, BATCH),
        ).fetchall()
    for a in rows:
        out["claimed"] += 1
        with get_connection() as conn:
            conn.execute(
                """insert into careflow.request_events
                       (request_id, from_status, to_status, actor, note)
                   values (%s, 'awaiting_approval', 'awaiting_approval', 'system',
                           'Approval escalated to a senior reviewer')""",
                (a["request_id"],),
            )
        msg = (f"APPROVAL ESCALATED\nApproval #{a['id']} (request #{a['request_id']})\n"
               f"Reason: {a['reason_for_review']}\nWaiting: {_waited(a['created_at'])} minutes, no decision.\n"
               "Senior review needed in CareFlow.")
        try:
            res = send_staff_notice(a["request_id"], f"approval-{a['id']}-escalation",
                                    "approval_escalation", msg)
            ok = res["status"] in ("sent", "already_sent")
        except Exception:
            ok = False
        out["sent" if ok else "failed"] += 1
    return out

def run_confirmation_retries() -> dict:
    with get_connection() as conn:
        s = {r["key"]: r["value"] for r in conn.execute(
            """select key, value from careflow.system_settings
               where key in ('automation_max_attempts', 'automation_retry_minutes')"""
        ).fetchall()}
    max_attempts = int(s.get("automation_max_attempts", 4))
    waits = [int(x) for x in s.get("automation_retry_minutes", [1, 5, 15])]

    with get_connection() as conn:
        rows = conn.execute(
            """update careflow.automation_logs set updated_at = now()
               where id in (
                   select id from careflow.automation_logs
                   where action = 'booking_confirmation' and status = 'failed'
                     and attempt_count < %(max)s
                     and (http_status is null or http_status >= 500)
                     and request_id in (select id from careflow.patient_requests
                                        where status = 'executing')
                     and updated_at <= now() - make_interval(
                           mins => (%(waits)s::int[])[least(attempt_count,
                                                            array_length(%(waits)s::int[], 1))])
                   order by updated_at limit %(batch)s for update skip locked)
               returning id, (request_payload->>'appointment_id')::bigint as appointment_id""",
            {"max": max_attempts, "waits": waits, "batch": BATCH},
        ).fetchall()

    out = {"claimed": len(rows), "retried": 0}
    for r in rows:
        try:
            send_confirmation(r["appointment_id"])
            out["retried"] += 1
        except Exception:
            pass
    return out

def _claim_slot_reminders():
    with get_connection() as conn:
        return conn.execute(
            """update careflow.follow_ups
               set due_at = now() + make_interval(mins => %s),
                   attempt_count = attempt_count + 1
               where id in (
                   select id from careflow.follow_ups
                   where kind = 'patient_slot_reminder' and status = 'pending'
                     and due_at <= now() and attempt_count < 5
                   order by due_at limit %s for update skip locked)
               returning id, request_id""",
            (LEASE_MINUTES, BATCH),
        ).fetchall()


def run_patient_slot_reminders() -> dict:
    out = {"claimed": 0, "sent": 0, "cancelled": 0, "failed": 0}
    for fu in _claim_slot_reminders():
        out["claimed"] += 1
        if _request_status(fu["request_id"]) != "waiting_patient":
            _finish(fu["id"], "cancelled")           # the patient already acted
            out["cancelled"] += 1
            continue
        with get_connection() as conn:
            req = conn.execute(
                """select r.patient_id, r.offered_slots, p.email, p.first_name
                   from careflow.patient_requests r
                   join careflow.patients p on p.id = r.patient_id
                   where r.id = %s""",
                (fu["request_id"],),
            ).fetchone()
        if (req is None or not req["offered_slots"]
                or (req["email"] or "").lower().endswith("@example.com")):
            _finish(fu["id"], "cancelled")           # nothing to send, or a demo address
            out["cancelled"] += 1
            continue

        options = "\n".join(f"{i}. {o['label']}" for i, o in enumerate(req["offered_slots"], 1))
        body = (
            f"Hello {req['first_name']},\n\n"
            "You have not yet chosen a slot for your appointment. The options below are still "
            "available, but they are not reserved and may be taken by someone else.\n\n"
            f"{options}\n\n"
            "Please choose your slot through our booking form. If we do not hear from you, "
            "these offers will expire.\n\n"
            "CareFlow Demo Hospital (simulated)"
        )
        try:
            res = send_via_n8n(
                request_id=fu["request_id"], patient_id=req["patient_id"],
                key=f"patient-{fu['request_id']}-slot-reminder",
                kind="patient_slot_reminder", to_email=req["email"],
                subject="Reminder: please choose an appointment slot", body=body,
                template="booking_slot_reminder_v1",
            )
            ok = res["status"] in ("sent", "already_sent")
        except Exception:
            ok = False
        if ok:
            _finish(fu["id"], "sent")
            out["sent"] += 1
        else:
            out["failed"] += 1                       # retried after the lease, up to 5 times
    return out


def _claim_slot_expiry():
    with get_connection() as conn:
        return conn.execute(
            """update careflow.follow_ups
               set due_at = now() + make_interval(mins => %s)
               where id in (
                   select id from careflow.follow_ups
                   where kind = 'patient_slot_expiry' and status = 'pending' and due_at <= now()
                   order by due_at limit %s for update skip locked)
               returning id, request_id""",
            (LEASE_MINUTES, BATCH),
        ).fetchall()


def run_patient_slot_expiry() -> dict:
    out = {"claimed": 0, "expired": 0, "cancelled": 0}
    for fu in _claim_slot_expiry():
        out["claimed"] += 1
        with get_connection() as conn:
            done = conn.execute(
                """update careflow.patient_requests
                   set status = 'expired', offered_slots = '[]'::jsonb, updated_at = now()
                   where id = %s and status = 'waiting_patient' returning id""",
                (fu["request_id"],),
            ).fetchone()
            if done is None:                         # the patient chose in time
                conn.execute("update careflow.follow_ups set status = 'cancelled' where id = %s", (fu["id"],))
                out["cancelled"] += 1
                continue
            conn.execute(
                """insert into careflow.request_events
                       (request_id, from_status, to_status, actor, note)
                   values (%s, 'waiting_patient', 'expired', 'system',
                           'Patient did not choose a slot before the expiry limit')""",
                (fu["request_id"],),
            )
            conn.execute(
                "update careflow.follow_ups set status = 'sent', sent_at = now() where id = %s", (fu["id"],)
            )
            conn.execute(
                """update careflow.follow_ups set status = 'cancelled'
                   where request_id = %s and kind = 'patient_slot_reminder' and status = 'pending'""",
                (fu["request_id"],),
            )
        out["expired"] += 1
    return out


def _claim_missing_document_followups():
    with get_connection() as conn:
        return conn.execute(
            """update careflow.follow_ups
               set due_at = now() + make_interval(mins => %s),
                   attempt_count = attempt_count + 1
               where id in (
                   select id from careflow.follow_ups
                   where kind = 'missing_document' and status = 'pending'
                     and due_at <= now()
                   order by due_at limit %s for update skip locked)
               returning id, request_id, created_at, attempt_count""",
            (LEASE_MINUTES, BATCH),
        ).fetchall()


def run_missing_document_followups() -> dict:
    reminder_hours = _minutes("patient_reminder_hours", 12)
    expiry_hours = _minutes("patient_expire_hours", 24)
    if reminder_hours <= 0 or expiry_hours <= reminder_hours:
        raise ValueError("Document reminder/expiry settings must be positive and expiry later than reminder")

    out = {"claimed": 0, "sent": 0, "expired": 0, "cancelled": 0, "failed": 0}
    for fu in _claim_missing_document_followups():
        out["claimed"] += 1
        try:
            context = get_document_reminder_context(fu["request_id"])
            if context["request_status"] != "waiting_patient" or not context["missing"]:
                _finish(fu["id"], "cancelled")
                out["cancelled"] += 1
                continue

            if datetime.now(timezone.utc) - fu["created_at"] >= timedelta(hours=expiry_hours):
                if expire_document_request(fu["request_id"], fu["id"]):
                    out["expired"] += 1
                else:
                    out["cancelled"] += 1
                continue

            result = send_document_reminder(fu["request_id"], fu["attempt_count"])
            if result["status"] in ("sent", "already_sent"):
                with get_connection() as conn:
                    conn.execute(
                        """update careflow.follow_ups
                           set due_at = now() + make_interval(hours => %s)
                           where id = %s and status = 'pending'""",
                        (reminder_hours, fu["id"]),
                    )
                out["sent"] += 1
            elif result["status"] == "not_needed":
                _finish(fu["id"], "cancelled")
                out["cancelled"] += 1
            else:
                out["failed"] += 1
        except Exception as exc:
            logger.error("Missing-document follow-up failed: %s", type(exc).__name__)
            out["failed"] += 1
    return out


def _safe(fn):
    try:
        return fn()
    except Exception as exc:
        return {"error": type(exc).__name__}
    
def run_tick() -> dict:
    return {
        "urgent_realerts": _safe(run_urgent_realerts),
        "approval_reminders": _safe(run_approval_reminders),
        "approval_escalations": _safe(run_approval_escalations),
        "confirmation_retries": _safe(run_confirmation_retries),
        "patient_slot_reminders": _safe(run_patient_slot_reminders),
        "patient_slot_expiry": _safe(run_patient_slot_expiry),
        "missing_document_followups": _safe(run_missing_document_followups),
        "waitlist_expiry": _safe(expire_waitlist_offers),
        "waitlist_entry_expiry": _safe(expire_waiting_entries),
    }