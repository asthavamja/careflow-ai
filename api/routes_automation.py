import logging

from fastapi import APIRouter, HTTPException

from agents.scheduler import run_tick

logger = logging.getLogger("careflow")
router = APIRouter()


@router.post("/automation/tick")
def tick():
    try:
        return run_tick()
    except Exception as exc:
        logger.error("Scheduler tick failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Tick failed")