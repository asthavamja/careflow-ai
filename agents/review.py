from agents.validation import validate_patient
from api.db import get_connection

DUPLICATE_REASONS = {"possible_duplicate", "duplicate_conflict"}

ALLOWED = {
    "possible_duplicate": ["same_person", "different_person", "more_info_needed", "reject"],
    "duplicate_conflict": ["same_person", "different_person", "more_info_needed", "reject"],
    "unclear_department": ["choose_department", "more_info_needed", "reject"],
    "unclear_request": ["reject"],
    "minor_patient": ["approve_registration", "reject"],
    "special_authorization": ["approve_without_referral", "request_referral", "reject"],
    "other": ["more_info_needed", "reject"],
    "failed_identity_check": ["more_info_needed", "reject"],
}
DEFAULT_ALLOWED = ["approve", "reject", "more_info_needed"]


def mask_phone(p):
    digits = "".join(c for c in (p or "") if c.isdigit())
    return "XXXXXX" + digits[-4:] if len(digits) >= 4 else None


def mask_email(e):
    if not e or "@" not in e:
        return None
    name, domain = e.split("@", 1)
    return name[:1] + "***@" + domain


def get_approval_view(approval_id: int):
    with get_connection() as conn:
        a = conn.execute(
            """select a.id, a.request_id, a.reason_for_review, a.level, a.decision,
                      a.created_at, r.request_type, r.extracted_data, r.status
               from careflow.approvals a
               join careflow.patient_requests r on r.id = a.request_id
               where a.id = %s""",
            (approval_id,),
        ).fetchone()
    if a is None:
        return None

    d = a["extracted_data"] or {}
    candidates = []
    if a["reason_for_review"] in DUPLICATE_REASONS:
        found = validate_patient(a["request_type"], d)["candidates"]
        ids = [c["id"] for c in found]
        if ids:
            with get_connection() as conn:
                rows = conn.execute(
                    """select id, patient_code, first_name, last_name, date_of_birth,
                              phone_normalized, email, city
                       from careflow.patients where id = any(%s)""",
                    (ids,),
                ).fetchall()
            by_id = {r["id"]: r for r in rows}
            for c in found:
                r = by_id[c["id"]]
                candidates.append({
                    "patient_id": r["id"],
                    "patient_code": r["patient_code"],
                    "name": f"{r['first_name']} {r['last_name']}",
                    "date_of_birth": r["date_of_birth"],
                    "phone": mask_phone(r["phone_normalized"]),
                    "email": mask_email(r["email"]),
                    "city": r["city"],
                    "name_similarity": c["name_score"],
                    "phone_matches": c["phone"],
                    "email_matches": c["email"],
                    "dob_matches": c["dob"],
                })

    name = f"{d.get('first_name') or ''} {d.get('last_name') or ''}".strip()
    return {
        "approval_id": a["id"],
        "request_id": a["request_id"],
        "reason": a["reason_for_review"],
        "level": a["level"],
        "decision": a["decision"],
        "request_type": a["request_type"],
        "request_status": a["status"],
        "created_at": a["created_at"],
        "patient_wrote": {
            "name": name or None,
            "date_of_birth": d.get("date_of_birth"),
            "phone": mask_phone(d.get("phone")),
            "email": mask_email(d.get("email")),
            "department": d.get("named_department"),
            "preferred_time": d.get("preferred_date_time"),
        },
        "candidates": candidates,
        "allowed_decisions": ALLOWED.get(a["reason_for_review"], DEFAULT_ALLOWED),
    }