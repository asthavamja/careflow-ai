from rapidfuzz import fuzz

from agents.normalize import normalize_email, normalize_phone, parse_dob
from api.db import get_connection

NAME_CLOSE = 85   # similarity (0-100) needed to count as "probably the same name"
NEEDS_IDENTITY = {"new_registration", "book_appointment"}


def _name(first, last):
    return f"{(first or '').strip()} {(last or '').strip()}".lower().strip()


def _result(match_result, notes, patient_id=None, candidates=None):
    return {
        "match_result": match_result,
        "patient_id": patient_id,
        "candidates": candidates or [],
        "notes": notes,
    }


def validate_patient(request_type: str, details: dict) -> dict:
    if request_type not in NEEDS_IDENTITY:
        return _result("skipped", ["identity is checked later for this request type"])

    first, last = details.get("first_name"), details.get("last_name")
    phone = normalize_phone(details.get("phone"))
    email = normalize_email(details.get("email"))
    dob_text = details.get("date_of_birth")
    dob = parse_dob(dob_text)

    notes = []
    if dob_text and dob is None:
        notes.append("date_of_birth not understood")
    if not first or not last:
        return _result("insufficient_info", notes + ["name incomplete"])
    if not (phone or email or dob):
        return _result("insufficient_info", notes + ["no phone, email or date of birth"])

    with get_connection() as conn:
        rows = conn.execute(
            """select id, patient_code, first_name, last_name, date_of_birth,
                      phone_normalized, lower(email) as email
               from careflow.patients
               where is_active and merged_into_id is null
                 and (phone_normalized = %s or lower(email) = %s or date_of_birth = %s)""",
            (phone, email, dob),
        ).fetchall()

    incoming = _name(first, last)
    good, conflicting = [], []
    for r in rows:
        score = fuzz.token_sort_ratio(incoming, _name(r["first_name"], r["last_name"]))
        if score < NAME_CLOSE:
            continue                      # a different person who shares a phone or birthday
        phone_ok = phone is not None and r["phone_normalized"] == phone
        email_ok = email is not None and r["email"] == email
        dob_ok = dob is not None and r["date_of_birth"] == dob
        if not (phone_ok or email_ok or dob_ok):
            continue
        cand = {"id": r["id"], "patient_code": r["patient_code"],
                "name_score": round(score), "phone": phone_ok,
                "email": email_ok, "dob": dob_ok}
        if dob is not None and not dob_ok:
            conflicting.append(cand)      # name and contact match, birth date does not
        else:
            good.append(cand)

    if request_type == "new_registration":
        if good:
            return _result("possible_duplicate", notes + ["an existing patient matches"], None, good)
        if conflicting:
            return _result("conflict", notes + ["name and contact match but birth date differs"], None, conflicting)
        return _result("new", notes)

    # book_appointment
    if len(good) == 1:
        return _result("found", notes, good[0]["id"], good)
    if len(good) > 1:
        return _result("conflict", notes + ["several patients match"], None, good)
    if conflicting:
        return _result("conflict", notes + ["name and contact match but birth date differs"], None, conflicting)
    return _result("new", notes + ["no registered patient found"])