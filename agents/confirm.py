import logging

from agents.communication import PROMPT_VERSION, assemble, write_wrapper
from agents.notify import get_confirmation_facts, send_via_n8n
from agents.trail import change_status, log_step
from api.config import settings
from api.db import get_connection

logger = logging.getLogger("careflow")


def _status(request_id):
    with get_connection() as conn:
        return conn.execute(
            "select status from careflow.patient_requests where id = %s", (request_id,)
        ).fetchone()["status"]

def _attempts_left(log_id):
    with get_connection() as conn:
        used = conn.execute(
            "select attempt_count from careflow.automation_logs where id = %s", (log_id,)
        ).fetchone()["attempt_count"]
        row = conn.execute(
            "select value from careflow.system_settings where key = 'automation_max_attempts'"
        ).fetchone()
    return used < (int(row["value"]) if row else 4)

def send_confirmation(appointment_id: int):
    f = get_confirmation_facts(appointment_id)
    rid = f["request_id"]

    w = write_wrapper(f["department"])
    rescheduled = f.get("request_type") == "reschedule"
    if rescheduled:
        w["opening"] = "Your appointment has been rescheduled."
    if rid is not None:
        log_step(
            rid, "communication_agent", "llm",
            "success" if (w["attempts"] == 1 and not w["fallback_used"]) else "retried",
            model=settings.groq_model, prompt_version=PROMPT_VERSION,
            input_summary="department name only",
            output={"fallback_used": w["fallback_used"], "attempts": w["attempts"]},
            input_tokens=w["input_tokens"], output_tokens=w["output_tokens"],
            latency_ms=w["latency_ms"], attempt=w["attempts"], error_message=w["error"],
        )

    subject, body = assemble(f, w["opening"], w["closing"])
    if rescheduled:
        subject = "Your appointment has been rescheduled"
    try:
        result = send_via_n8n(
            request_id=rid, patient_id=f["patient_id"],
            key=f"appt-{appointment_id}-confirmation", kind="booking_confirmation",
            to_email=f["email"], subject=subject, body=body,
            template="booking_confirmation_v2",
            meta={"appointment_id": appointment_id},
        )
        sent = result["status"] in ("sent", "already_sent")
        reason = result.get("error")
        retryable = bool(result.get("retryable"))
    except Exception as exc:
        result, sent, reason, retryable = {}, False, type(exc).__name__, False
    if rid is None:
        return      # a waitlist booking has no request to update
    if _status(rid) != "executing":
        return
    if sent:
        change_status(rid, "confirmed", "n8n", "Confirmation email sent")
    elif retryable and _attempts_left(result["log_id"]):
        return      # stays "executing"; the scheduler will retry
    else:
        change_status(rid, "failed", "n8n", f"Confirmation could not be sent: {reason}")


def run_confirmation(appointment_id: int):
    """Safe wrapper for background use: never raises, never logs patient text."""
    try:
        send_confirmation(appointment_id)
    except Exception as exc:
        logger.error("Confirmation step crashed: %s", type(exc).__name__)