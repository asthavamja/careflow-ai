import logging
from fastapi.responses import JSONResponse
from fastapi import APIRouter, BackgroundTasks, HTTPException
from agents.booking import BookingError, book_choice
from agents.confirm import run_confirmation
from api.schemas import ReplyIn
from fastapi import APIRouter, HTTPException

from api.db import get_connection
from api.schemas import RequestIn, RequestOut

logger = logging.getLogger("careflow")
router = APIRouter()


@router.post("/requests", response_model=RequestOut, status_code=201)
def create_request(body: RequestIn):
    try:
        with get_connection() as conn:
            row = conn.execute(
                """insert into careflow.patient_requests
                       (channel, request_type, raw_text, status)
                   values (%s, 'unclear', %s, 'received')
                   returning id, status""",
                (body.channel, body.raw_text),
            ).fetchone()
            conn.execute(
                """insert into careflow.request_events
                       (request_id, from_status, to_status, actor, note)
                   values (%s, null, 'received', 'system', 'Request created')""",
                (row["id"],),
            )
    except Exception as exc:
        # Log only the error type: error details can contain the message text
        logger.error("Could not save request: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Could not save the request")
    return RequestOut(request_id=row["id"], status=row["status"])

@router.post("/requests/{request_id}/reply")
def reply_to_request(request_id: int, body: ReplyIn, background: BackgroundTasks):
    try:
        result = book_choice(request_id, body.choice)
        background.add_task(run_confirmation, result["appointment_id"])
        return result
    except BookingError as e:
        code = {"not_found": 404, "invalid_choice": 400}.get(e.code, 409)
        return JSONResponse(status_code=code, content={
            "status": e.code,
            "message": e.message,
            "new_offers": [{"slot_id": o["slot_id"], "label": o["label"]} for o in e.new_offers],
        })
    except Exception as exc:
        logger.error("Could not book: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Could not complete the booking")