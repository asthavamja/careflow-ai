from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from rapidfuzz import fuzz

from agents.notify import send_via_n8n
from api.db import get_connection

IST = ZoneInfo("Asia/Kolkata")
NAME_CLOSE = 85


def verify_cancel(details: dict):
    """Return (appointment, None) if verified, otherwise (None, reason). The reason is for logs only."""
    code = (details.get("appointment_code") or "").strip().upper()
    first, last = details.get("first_name"), details.get("last_name")
    if not code:
        return None, "no appointment code"
    if not first or not last:
        return None, "name missing"
    with get_connection() as conn:
        row = conn.execute(
            """select a.id, a.status, a.slot_id, a.patient_id, a.appointment_code,
                      s.start_time, p.first_name, p.last_name, p.email,
                      doc.id as doctor_id, doc.name as doctor_name,
                      dep.id as department_id, dep.name as department
               from careflow.appointments a
               join careflow.time_slots s on s.id = a.slot_id
               join careflow.doctors doc on doc.id = s.doctor_id
               join careflow.departments dep on dep.id = doc.department_id
               join careflow.patients p on p.id = a.patient_id
               where upper(a.appointment_code) = %s""",
            (code,),
        ).fetchone()
    if row is None:
        return None, "code not found"
    if row["status"] != "booked":
        return None, "appointment not active"
    if row["start_time"] <= datetime.now(timezone.utc):
        return None, "appointment in the past"
    given = f"{first} {last}".lower().strip()
    on_file = f"{row['first_name']} {row['last_name']}".lower().strip()
    if fuzz.token_sort_ratio(given, on_file) < NAME_CLOSE:
        return None, "name does not match"
    return row, None


def cancel_appointment(appointment_id: int):
    """Cancel and free the slot in one transaction. Returns the freed slot id, or None."""
    with get_connection() as conn:
        row = conn.execute(
            """update careflow.appointments set status = 'cancelled', updated_at = now()
               where id = %s and status = 'booked' returning slot_id""",
            (appointment_id,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "update careflow.time_slots set status = 'free', held_until = null where id = %s",
            (row["slot_id"],),
        )
        return row["slot_id"]


def send_cancellation_notice(request_id: int, appt: dict) -> bool:
    when = appt["start_time"].astimezone(IST).strftime("%A, %d %B %Y at %I:%M %p")
    body = (
        f"Hello {appt['first_name']},\n\n"
        "Your appointment has been cancelled.\n\n"
        f"Department: {appt['department']}\n"
        f"Doctor: {appt['doctor_name']}\n"
        f"Was scheduled for: {when}\n"
        f"Appointment code: {appt['appointment_code']}\n\n"
        "If you would like a new appointment, please send us a new request.\n\n"
        "CareFlow Demo Hospital (simulated)"
    )
    try:
        res = send_via_n8n(
            request_id=request_id, patient_id=appt["patient_id"],
            key=f"appt-{appt['id']}-cancellation", kind="cancellation_notice",
            to_email=appt["email"], subject="Your appointment has been cancelled",
            body=body, template="cancellation_notice_v1",
            meta={"appointment_id": appt["id"]},
        )
        return res["status"] in ("sent", "already_sent")
    except Exception:
        return False