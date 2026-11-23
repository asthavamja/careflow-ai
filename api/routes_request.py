import logging
from fastapi.responses import JSONResponse
from fastapi import APIRouter, BackgroundTasks, HTTPException
from agents.booking import BookingError, book_choice
from agents.confirm import run_confirmation
from api.schemas import ReplyIn
from fastapi import APIRouter, HTTPException
from psycopg.types.json import Jsonb

from agents.trail import change_status
from api.schemas import AckIn
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

@router.post("/requests/{request_id}/acknowledge")
def acknowledge_urgent(request_id: int, body: AckIn):
    with get_connection() as conn:
        user = conn.execute(
            "select id, role from careflow.users where id = %s and is_active", (body.reviewer_id,)
        ).fetchone()
        req = conn.execute(
            "select status from careflow.patient_requests where id = %s", (request_id,)
        ).fetchone()
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found")
    if user is None or user["role"] not in ("clinical_staff", "approver", "admin"):
        raise HTTPException(status_code=403, detail="Only clinical staff can acknowledge urgent requests")
    if req["status"] != "urgent_escalated":
        raise HTTPException(status_code=409, detail="This request is not waiting as urgent")

    change_status(request_id, "closed", "staff", "Urgent request acknowledged by clinical staff",
                  actor_user_id=body.reviewer_id)
    with get_connection() as conn:
        conn.execute(
            """update careflow.follow_ups set status = 'cancelled'
               where request_id = %s and kind = 'urgent_realert' and status = 'pending'""",
            (request_id,),
        )
        conn.execute(
            """insert into careflow.audit_logs
                   (actor_type, actor_user_id, action, entity_type, entity_id, after_data)
               values ('user', %s, 'urgent_acknowledged', 'request', %s, %s)""",
            (body.reviewer_id, str(request_id), Jsonb({"note_given": bool(body.note)})),
        )
    return {"status": "acknowledged", "request_id": request_id}