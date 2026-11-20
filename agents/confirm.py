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


def send_confirmation(appointment_id: int):
    f = get_confirmation_facts(appointment_id)
    rid = f["request_id"]

    w = write_wrapper(f["department"])
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
    try:
        result = send_via_n8n(
            request_id=rid, patient_id=f["patient_id"],
            key=f"appt-{appointment_id}-confirmation", kind="booking_confirmation",
            to_email=f["email"], subject=subject, body=body,
            template="booking_confirmation_v2",
        )
        sent = result["status"] in ("sent", "already_sent")
        reason = result.get("error")
    except Exception as exc:
        sent, reason = False, type(exc).__name__

    if _status(rid) == "executing":
        if sent:
            change_status(rid, "confirmed", "n8n", "Confirmation email sent")
        else:
            change_status(rid, "failed", "n8n", f"Confirmation could not be sent: {reason}")


def run_confirmation(appointment_id: int):
    """Safe wrapper for background use: never raises, never logs patient text."""
    try:
        send_confirmation(appointment_id)
    except Exception as exc:
        logger.error("Confirmation step crashed: %s", type(exc).__name__)