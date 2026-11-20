import re
import time

from langchain_groq import ChatGroq
from pydantic import BaseModel, ConfigDict

from api.config import settings

MAX_PART = 300
FORBIDDEN = ["diagnos", "medicine", "medication", "treatment", "prescri",
             "dose", "symptom", "emergency", "cure"]


def build_details_block(f: dict) -> str:
    return (
        "Appointment details\n"
        f"Department: {f['department']}\n"
        f"Doctor: {f['doctor_name']}\n"
        f"When: {f['when']}\n"
        f"Appointment code: {f['appointment_code']}"
    )


def what_to_bring(f: dict) -> str:
    items = ["A photo ID", "Your appointment code"]
    if f.get("requires_referral"):
        items.append("Your referral letter")
    return "Please bring:\n" + "\n".join(f"- {i}" for i in items)


def check_wrapper(text) -> str | None:
    """Return None if the text is safe, otherwise the reason it was rejected."""
    if not isinstance(text, str) or not text.strip():
        return "empty"
    if len(text) > MAX_PART:
        return "too long"
    if re.search(r"\d", text):
        return "contains digits"          # the model must not write times, dates or codes
    if re.search(r"https?://|www\.|@", text, re.I):
        return "contains a link or address"
    lowered = text.lower()
    for word in FORBIDDEN:
        if word in lowered:
            return f"contains '{word}'"
    return None


def fallback_wrapper() -> tuple[str, str]:
    return ("Your appointment is confirmed.",
            "If you need to change or cancel it, please quote your appointment code. We look forward to seeing you.")


def assemble(f: dict, opening: str, closing: str) -> tuple[str, str]:
    subject = "Your appointment is confirmed"
    body = (
        f"Hello {f['first_name']},\n\n"
        f"{opening.strip()}\n\n"
        f"{build_details_block(f)}\n\n"
        f"{what_to_bring(f)}\n\n"
        f"{closing.strip()}\n\n"
        "CareFlow Demo Hospital (simulated)"
    )
    return subject, body

PROMPT_VERSION = "communication_v1"

SYSTEM_PROMPT = """You write two short parts of a hospital appointment confirmation email.
- opening: one or two warm sentences saying the appointment is booked.
- closing: one or two sentences asking the patient to quote the appointment code if they need to change or cancel, and saying we look forward to seeing them.

Rules:
1. Do NOT write any numbers, dates, times, doctor names, codes, links or email addresses. Those are added separately by the system.
2. Do not give medical advice or mention symptoms, illness or treatment.
3. Use plain, friendly, professional English. No emojis."""


class Wrapper(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opening: str
    closing: str


def write_wrapper(department: str, max_attempts: int = 2) -> dict:
    llm = ChatGroq(
        model=settings.groq_model,
        api_key=settings.groq_api_key,
        temperature=0,
        reasoning_effort="low",
    )
    chain = llm.with_structured_output(Wrapper, method="json_schema", include_raw=True)

    last_problem = None
    for attempt in range(1, max_attempts + 1):
        started = time.perf_counter()
        try:
            out = chain.invoke([
                ("system", SYSTEM_PROMPT),
                ("human", f"Department: {department}. Write the two parts."),
            ])
            parsed = out["parsed"]
            if parsed is None:
                raise ValueError("output did not match the form")
            problem = check_wrapper(parsed.opening) or check_wrapper(parsed.closing)
            if problem is None:
                usage = getattr(out["raw"], "usage_metadata", None) or {}
                return {
                    "opening": parsed.opening, "closing": parsed.closing,
                    "fallback_used": False, "attempts": attempt, "error": None,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                }
            last_problem = f"rejected: {problem}"
        except Exception as exc:
            last_problem = type(exc).__name__
        time.sleep(1)

    opening, closing = fallback_wrapper()
    return {
        "opening": opening, "closing": closing, "fallback_used": True,
        "attempts": max_attempts, "error": last_problem, "latency_ms": None,
        "input_tokens": None, "output_tokens": None,
    }