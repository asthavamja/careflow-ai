import logging

from fastapi import APIRouter, HTTPException

from agents.reporting import weekly_metrics

logger = logging.getLogger("careflow")
router = APIRouter()


@router.get("/reports/weekly")
def weekly_report():
    try:
        return weekly_metrics()
    except Exception as exc:
        logger.error("Weekly report failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Could not build the weekly report")
