from datetime import datetime, timezone

from agents.alerts import send_staff_notice
from agents.alerts import send_urgent_alert
from agents.confirm import send_confirmation
from api.db import get_connection

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

def run_tick() -> dict:
    return {
        "urgent_realerts": run_urgent_realerts(),
        "approval_reminders": run_approval_reminders(),
        "approval_escalations": run_approval_escalations(),
        "confirmation_retries": run_confirmation_retries(),
    }