import re
from datetime import date

from agents.normalize import normalize_phone, parse_dob
from api.db import get_connection

EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def is_minor(date_of_birth: date, today: date | None = None) -> bool:
    today = today or date.today()
    age = today.year - date_of_birth.year
    if (today.month, today.day) < (date_of_birth.month, date_of_birth.day):
        age -= 1
    return age < 18


def prepare_patient_values(details: dict, contact_email: str | None) -> dict:
    first_name = (details.get("first_name") or "").strip()
    last_name = (details.get("last_name") or "").strip()
    date_of_birth = parse_dob(details.get("date_of_birth"))
    phone = details.get("phone")
    phone_normalized = normalize_phone(phone)

    if not first_name or not last_name:
        raise ValueError("registration_name_missing")
    if date_of_birth is None or date_of_birth > date.today():
        raise ValueError("registration_date_of_birth_invalid")
    if not phone_normalized:
        raise ValueError("registration_phone_invalid")

    email = None
    for candidate in (details.get("email"), contact_email):
        if candidate:
            candidate = candidate.strip().lower()
            if EMAIL_PATTERN.fullmatch(candidate):
                email = candidate
                break
    if email is None:
        raise ValueError("registration_email_missing_or_invalid")

    return {
        "first_name": first_name,
        "last_name": last_name,
        "date_of_birth": date_of_birth,
        "phone": phone,
        "phone_normalized": phone_normalized,
        "email": email,
    }


def register_new_patient(
    request_id: int,
    *,
    allow_minor: bool = False,
    actor: str = "system",
    actor_user_id: int | None = None,
) -> dict:
    with get_connection() as conn:
        req = conn.execute(
            """select request_type, status, patient_id, contact_email, extracted_data
               from careflow.patient_requests where id = %s for update""",
            (request_id,),
        ).fetchone()
        if req is None:
            raise ValueError("Request not found")
        if req["request_type"] != "new_registration":
            raise ValueError("Request is not a new registration")
        if req["status"] not in ("validated", "awaiting_approval"):
            raise ValueError("Registration request is not ready for patient creation")
        if req["patient_id"] is not None:
            return {
                "patient_id": req["patient_id"],
                "created": False,
                "requires_approval": False,
            }

        values = prepare_patient_values(req["extracted_data"] or {}, req["contact_email"])
        minor = is_minor(values["date_of_birth"])
        if minor and not allow_minor:
            return {
                "patient_id": None,
                "created": False,
                "requires_approval": True,
            }

        patient = conn.execute(
            """insert into careflow.patients
                   (first_name, last_name, date_of_birth, phone, phone_normalized, email)
               values (%s, %s, %s, %s, %s, %s)
               returning id""",
            (
                values["first_name"],
                values["last_name"],
                values["date_of_birth"],
                values["phone"],
                values["phone_normalized"],
                values["email"],
            ),
        ).fetchone()
        conn.execute(
            """update careflow.patient_requests
               set patient_id = %s, status = 'validated', updated_at = now()
               where id = %s""",
            (patient["id"], request_id),
        )
        conn.execute(
            """insert into careflow.request_events
                   (request_id, from_status, to_status, actor, actor_user_id, note)
               values (%s, %s, 'validated', %s, %s, %s)""",
            (
                request_id,
                req["status"],
                actor,
                actor_user_id,
                "New patient record created and linked to registration",
            ),
        )

    return {
        "patient_id": patient["id"],
        "created": True,
        "requires_approval": False,
    }
