import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from psycopg.types.json import Jsonb

from api.config import settings
from api.db import get_connection

IST = ZoneInfo("Asia/Kolkata")


def _safe_reason(reason):
    """Keep only the matched word-list phrases. Never pass the model's free text to Slack."""
    reason = reason or ""
    parts = []
    m = re.search(r"word list:\s*([^;]+)", reason)
    if m:
        parts.append("word list: " + m.group(1).strip())
    if "model:" in reason:
        parts.append("model judgment")
    return "; ".join(parts) or "flagged"


def build_alert_message(req, repeat=False):
    when = req["created_at"].astimezone(IST).strftime("%d %b %Y, %I:%M %p")
    head = "URGENT REQUEST (reminder: not yet acknowledged)" if repeat else "URGENT REQUEST FLAGGED"
    return (
        f"{head}\n"
        f"Request #{req['id']}\n"
        f"Reason: {_safe_reason(req['urgent_reason'])}\n"
        f"Received: {when} IST via {req['channel']}\n"
        "Please review in CareFlow."
    )


def send_urgent_alert(request_id: int, repeat: bool = False, attempt_no: int = 1) -> dict:
    if not settings.n8n_alert_webhook_url:
        raise RuntimeError("N8N_ALERT_WEBHOOK_URL is not set")

    with get_connection() as conn:
        req = conn.execute(
            "select id, urgent_flag, urgent_reason, channel, created_at "
            "from careflow.patient_requests where id = %s", (request_id,)
        ).fetchone()
    if req is None or not req["urgent_flag"]:
        raise ValueError("not an urgent request")

    key = f"urgent-{request_id}-alert-{attempt_no}"
    message = build_alert_message(req, repeat)

    with get_connection() as conn:
        row = conn.execute(
            """insert into careflow.automation_logs
                   (request_id, workflow_name, action, idempotency_key, status, request_payload)
               values (%s, 'careflow-staff-alert', 'urgent_alert', %s, 'pending', %s)
               on conflict (idempotency_key) do nothing returning id""",
            (request_id, key, Jsonb({"repeat": repeat})),
        ).fetchone()
        if row is None:
            old = conn.execute(
                "select id, status from careflow.automation_logs where idempotency_key = %s", (key,)
            ).fetchone()
            if old["status"] == "succeeded":
                return {"status": "already_sent", "log_id": old["id"]}
            log_id = old["id"]
        else:
            log_id = row["id"]

    http_status, ok, error = None, False, None
    try:
        resp = httpx.post(
            settings.n8n_alert_webhook_url,
            json={"idempotency_key": key, "kind": "urgent_alert", "message": message},
            headers={"X-Webhook-Secret": settings.n8n_shared_secret},
            timeout=20,
        )
        http_status = resp.status_code
        ok = http_status == 200 and resp.json().get("status") == "sent"
        if not ok:
            error = f"HTTP {http_status}"
    except Exception as exc:
        error = type(exc).__name__

    with get_connection() as conn:
        conn.execute(
            """update careflow.automation_logs
               set status = %s, http_status = %s, attempt_count = attempt_count + 1,
                   error_message = %s, updated_at = now() where id = %s""",
            ("succeeded" if ok else "failed", http_status, error, log_id),
        )
        if ok:
            conn.execute(
                """insert into careflow.request_events
                       (request_id, from_status, to_status, actor, note)
                   values (%s, 'urgent_escalated', 'urgent_escalated', 'n8n', %s)""",
                (request_id, "Urgent alert sent to clinical staff" + (" (re-alert)" if repeat else "")),
            )
            if not repeat:
                mins = conn.execute(
                    "select value from careflow.system_settings where key = 'urgent_realert_minutes'"
                ).fetchone()
                due = datetime.now(timezone.utc) + timedelta(minutes=int(mins["value"]) if mins else 5)
                conn.execute(
                    """insert into careflow.follow_ups (request_id, kind, due_at)
                       select %s, 'urgent_realert', %s
                       where not exists (select 1 from careflow.follow_ups
                                         where request_id = %s and kind = 'urgent_realert')""",
                    (request_id, due, request_id),
                )
    return {"status": "sent" if ok else "failed", "log_id": log_id, "error": error}

def send_staff_notice(request_id: int, key: str, action: str, message: str) -> dict:
    """A plain Slack notice to staff through the same n8n alert workflow. One send per key."""
    if not settings.n8n_alert_webhook_url:
        raise RuntimeError("N8N_ALERT_WEBHOOK_URL is not set")

    with get_connection() as conn:
        row = conn.execute(
            """insert into careflow.automation_logs
                   (request_id, workflow_name, action, idempotency_key, status, request_payload)
               values (%s, 'careflow-staff-alert', %s, %s, 'pending', '{}'::jsonb)
               on conflict (idempotency_key) do nothing returning id""",
            (request_id, action, key),
        ).fetchone()
        if row is None:
            old = conn.execute(
                "select id, status from careflow.automation_logs where idempotency_key = %s", (key,)
            ).fetchone()
            if old["status"] == "succeeded":
                return {"status": "already_sent", "log_id": old["id"]}
            log_id = old["id"]
        else:
            log_id = row["id"]

    http_status, ok, error = None, False, None
    try:
        resp = httpx.post(
            settings.n8n_alert_webhook_url,
            json={"idempotency_key": key, "kind": action, "message": message},
            headers={"X-Webhook-Secret": settings.n8n_shared_secret},
            timeout=20,
        )
        http_status = resp.status_code
        ok = http_status == 200 and resp.json().get("status") == "sent"
        if not ok:
            error = f"HTTP {http_status}"
    except Exception as exc:
        error = type(exc).__name__

    with get_connection() as conn:
        conn.execute(
            """update careflow.automation_logs
               set status = %s, http_status = %s, attempt_count = attempt_count + 1,
                   error_message = %s, updated_at = now() where id = %s""",
            ("succeeded" if ok else "failed", http_status, error, log_id),
        )
    return {"status": "sent" if ok else "failed", "log_id": log_id, "error": error}