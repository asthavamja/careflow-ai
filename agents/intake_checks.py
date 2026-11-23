from agents.urgent import find_urgent_phrases

REQUIRED = {
    "new_registration": ["first_name", "last_name", "date_of_birth", "phone"],
    "book_appointment": ["first_name", "last_name", "named_department"],
    "reschedule":       ["appointment_code", "preferred_date_time"],
    "cancel":           ["first_name", "last_name", "appointment_code"],
    "documents":        [],
    "unclear":          [],
}


def compute_missing(request_type: str, details: dict) -> list[str]:
    missing = [f for f in REQUIRED[request_type] if not details.get(f)]
    if request_type == "book_appointment" and not any(
        details.get(f) for f in ("phone", "date_of_birth", "email")
    ):
        missing.append("identifier")   # phone, date of birth or email
    return missing


def finalize_intake(raw_text: str, out: dict) -> dict:
    """Combine the model's answer with the plain-code checks."""
    r = out["result"]
    details = r.details.model_dump()
    matched = find_urgent_phrases(raw_text)

    reasons = []
    if r.urgent_flag and r.urgent_reason:
        reasons.append(f"model: {r.urgent_reason}")
    if matched:
        reasons.append("word list: " + ", ".join(matched))

    return {
        "request_type": r.request_type,
        "details": details,
        "urgent_flag": r.urgent_flag or bool(matched),   # either layer is enough
        "urgent_reason": "; ".join(reasons) or None,
        "confidence": r.confidence,
        "missing_fields": compute_missing(r.request_type, details),
        "llm_failed": out["llm_failed"],
    }