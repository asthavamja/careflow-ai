import re
import time

from api.db import get_connection

CACHE_SECONDS = 60
_cache = {"phrases": None, "loaded_at": 0.0}


def _normalize(text: str) -> str:
    text = text.lower().replace("'", "").replace("’", "")
    text = re.sub(r"[^a-z0-9\s]", " ", text)   # punctuation becomes spaces
    return re.sub(r"\s+", " ", text).strip()


def load_urgent_phrases() -> list[str]:
    now = time.time()
    if _cache["phrases"] is not None and now - _cache["loaded_at"] < CACHE_SECONDS:
        return _cache["phrases"]
    with get_connection() as conn:
        row = conn.execute(
            "select value from careflow.system_settings where key = 'urgent_phrases'"
        ).fetchone()
    if row is None:
        raise RuntimeError("urgent_phrases setting is missing")   # fail loudly, never silently
    phrases = [_normalize(p) for p in row["value"]]
    _cache.update(phrases=phrases, loaded_at=now)
    return phrases


def find_urgent_phrases(text: str, phrases: list[str] | None = None) -> list[str]:
    phrases = load_urgent_phrases() if phrases is None else phrases
    padded = f" {_normalize(text)} "
    return [p for p in phrases if f" {p} " in padded]   