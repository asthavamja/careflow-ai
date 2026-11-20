import hmac

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from api.config import settings

_key_header = APIKeyHeader(name="X-Webhook-Secret", auto_error=False)


def require_secret(key: str | None = Security(_key_header)):
    expected = settings.n8n_shared_secret
    if not expected or not key or not hmac.compare_digest(key, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")