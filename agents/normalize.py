import re
from datetime import datetime

DOB_FORMATS = [
    "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
    "%d %B %Y", "%d %b %Y", "%B %d %Y", "%B %d, %Y",
]


def normalize_phone(text):
    """Digits only, last 10. Returns None if fewer than 10 digits."""
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    return digits[-10:] if len(digits) >= 10 else None


def normalize_email(text):
    return text.strip().lower() if text and "@" in text else None


def parse_dob(text):
    """Day-first (Indian style). Returns None if not clearly understood."""
    if not text:
        return None
    cleaned = re.sub(r"\s+", " ", text.strip())
    for fmt in DOB_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None