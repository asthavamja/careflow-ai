import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from psycopg.types.json import Jsonb

from api.config import settings
from api.db import get_connection

IST = ZoneInfo("Asia/Kolkata")


def get_confirmation_facts(appointment_id: int) -> dict:
    with get_connection() as conn:
        r = conn.execute(
            """select a.id, a.appointment_code, a.request_id, s.start_time,
                      doc.name as doctor_name, dep.name as department,dep.requires_referral,
                      p.id as patient_id, p.first_name, p.email
               from careflow.appointments a
               join careflow.time_slots s on s.id = a.slot_id
               join careflow.doctors doc on doc.id = s.doctor_id
               join careflow.departments dep on dep.id = doc.department_id
               join careflow.patients p on p.id = a.patient_id
               where a.id = %s""",
            (appointment_id,),
        ).fetchone()
    if r is None:
        raise ValueError("appointment not found")
    r["when"] = r["start_time"].astimezone(IST).strftime("%A, %d %B %Y at %I:%M %p")
    return r


def template_confirmation(f: dict) -> tuple[str, str]:
    subject = "Your appointment is confirmed"
    body = (
        f"Hello {f['first_name']},\n\n"
        "Your appointment is confirmed.\n\n"
        f"Department: {f['department']}\n"
        f"Doctor: {f['doctor_name']}\n"
        f"When: {f['when']}\n"
        f"Appointment code: {f['appointment_code']}\n\n"
        "Please keep your appointment code. You will need it to cancel or reschedule.\n\n"
        "CareFlow Demo Hospital (simulated)"
    )
    return subject, body


def send_via_n8n(*, request_id, patient_id, key, kind, to_email, subject, body, template):
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to_email or ""):
        raise ValueError("invalid email address")
    if not settings.n8n_webhook_url:
        raise RuntimeError("N8N_WEBHOOK_URL is not set")
    if to_email.lower().endswith("@example.com"):
        raise ValueError("demo address (example.com): not sending")

    # Record the attempt first. The unique key stops a second send.
    with get_connection() as conn:
        row = conn.execute(
            """insert into careflow.automation_logs
                   (request_id, workflow_name, action, idempotency_key, status, request_payload)
               values (%s, 'careflow-patient-message', %s, %s, 'pending', %s)
               on conflict (idempotency_key) do nothing returning id""",
            (request_id, kind, key, Jsonb({"template": template})),
        ).fetchone()
        if row is None:
            old = conn.execute(
                "select id, status from careflow.automation_logs where idempotency_key = %s",
                (key,),
            ).fetchone()
            if old["status"] == "succeeded":
                return {"status": "already_sent", "log_id": old["id"]}
            log_id = old["id"]          # an earlier attempt failed: retry it
        else:
            log_id = row["id"]

    http_status, ok, error = None, False, None
    try:
        resp = httpx.post(
            settings.n8n_webhook_url,
            json={"idempotency_key": key, "kind": kind, "to_email": to_email,
                  "subject": subject, "body_text": body},
            headers={"X-Webhook-Secret": settings.n8n_shared_secret},
            timeout=20,
        )
        http_status = resp.status_code
        ok = http_status == 200 and resp.json().get("status") == "sent"
        if not ok:
            error = f"HTTP {http_status}"
    except Exception as exc:
        error = type(exc).__name__      # type only: never log the message

    with get_connection() as conn:
        conn.execute(
            """update careflow.automation_logs
               set status = %s, http_status = %s, attempt_count = attempt_count + 1,
                   error_message = %s, updated_at = now() where id = %s""",
            ("succeeded" if ok else "failed", http_status, error, log_id),
        )
        conn.execute(
            """insert into careflow.notifications
                   (request_id, recipient_patient_id, channel, template, payload,
                    status, automation_log_id, sent_at)
               values (%s, %s, 'email', %s, '{}'::jsonb, %s, %s, %s)""",
            (request_id, patient_id, template, "sent" if ok else "failed", log_id,
             datetime.now(timezone.utc) if ok else None),
        )
    return {"status": "sent" if ok else "failed", "log_id": log_id, "error": error}