import logging
from fastapi import Depends

from api.security import require_secret
from fastapi import FastAPI, HTTPException
from api.routes_request import router as requests_router
from api.routes_approvals import router as approvals_router
from api.db import get_connection
from api.routes_automation import router as automation_router

logger = logging.getLogger("careflow")

app = FastAPI(title="CareFlow AI", dependencies=[Depends(require_secret)])
app.include_router(requests_router)
app.include_router(approvals_router)
app.include_router(automation_router)

@app.get("/health")
def health():
    """Is the service alive and can it reach the database?"""
    try:
        with get_connection() as conn:
            row = conn.execute(
                "select count(*) as n from information_schema.tables "
                "where table_schema = 'careflow'"
            ).fetchone()
    except Exception:
        logger.exception("Health check failed")
        raise HTTPException(status_code=503, detail="Database not reachable")
    return {"status": "ok", "tables": row["n"]}

@app.get("/automation/ping")
def ping():
    return {"status": "ok"}