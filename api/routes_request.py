import logging
from fastapi.responses import JSONResponse
from fastapi import APIRouter, BackgroundTasks, Body, HTTPException
from agents.booking import BookingError, book_choice
from agents.confirm import run_confirmation
from api.schemas import ReplyIn
from fastapi import APIRouter, HTTPException
from psycopg.types.json import Jsonb

from agents.trail import change_status
from api.schemas import AckIn
from api.db import get_connection
from api.schemas import RequestIn, RequestOut
from agents.waitlist import (
    WaitlistJoinError,
    accept_waitlist_offer,
    join_waitlist_for_request,
)
from api.schemas import WaitlistAcceptIn, WaitlistJoinIn
from agents.documents import (
    get_document_checklist,
    record_document_submission,
    review_document_submission,
)
from agents.graph import run_request
from api.schemas import DocumentReviewIn, DocumentSubmitIn, MoreInfoIn

logger = logging.getLogger("careflow")
router = APIRouter()
DOCUMENT_REVIEW_ROLES = {"receptionist", "coordinator", "approver", "admin"}


@router.post("/requests", response_model=RequestOut, status_code=201)
def create_request(body: RequestIn):
    try:
        with get_connection() as conn:
            row = conn.execute(
                """insert into careflow.patient_requests
                       (channel, request_type, raw_text, contact_email, status)
                   values (%s, 'unclear', %s, %s, 'received')
                   returning id, status""",
                (body.channel, body.raw_text, body.contact_email),
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


@router.post("/requests/{request_id}/more-info")
def provide_more_info(request_id: int, body: MoreInfoIn):
    with get_connection() as conn:
        req = conn.execute(
            """select raw_text, status, clarification_count
               from careflow.patient_requests where id = %s for update""",
            (request_id,),
        ).fetchone()
        if req is None:
            raise HTTPException(status_code=404, detail="Request not found")
        if req["status"] != "waiting_patient":
            raise HTTPException(
                status_code=409, detail="Request is not waiting for clarification",
            )
        if req["clarification_count"] >= 1:
            raise HTTPException(
                status_code=409, detail="The clarification round has already been used",
            )
        asked = conn.execute(
            """select exists (
                   select 1 from careflow.request_events
                   where request_id = %s and to_status = 'waiting_patient'
                     and note = 'More information needed'
               ) as asked""",
            (request_id,),
        ).fetchone()["asked"]
        if not asked:
            raise HTTPException(
                status_code=409, detail="This request was not waiting for clarification",
            )

        updated_text = f"{req['raw_text']}\n\nPatient reply: {body.text}"
        if len(updated_text) > 4000:
            raise HTTPException(
                status_code=413, detail="Combined request text is too long",
            )
        conn.execute(
            """update careflow.patient_requests
               set raw_text = %s, status = 'received', clarification_count = 1,
                   updated_at = now()
               where id = %s""",
            (updated_text, request_id),
        )
        conn.execute(
            """insert into careflow.request_events
                   (request_id, from_status, to_status, actor, note)
               values (%s, 'waiting_patient', 'received', 'system',
                       'Patient provided clarification')""",
            (request_id,),
        )

    try:
        run_request(request_id)
    except Exception as exc:
        logger.error("Could not resume clarified request %s: %s", request_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="Could not process the clarification")

    with get_connection() as conn:
        status = conn.execute(
            "select status from careflow.patient_requests where id = %s",
            (request_id,),
        ).fetchone()["status"]
    return {"request_id": request_id, "status": status}


@router.get("/requests/{request_id}/documents")
def document_checklist(request_id: int):
    try:
        return {"request_id": request_id, "documents": get_document_checklist(request_id)}
    except ValueError as exc:
        code = 404 if str(exc) == "Request not found" else 409
        raise HTTPException(status_code=code, detail=str(exc))


@router.post("/requests/{request_id}/documents")
def submit_registration_document(request_id: int, body: DocumentSubmitIn):
    try:
        document = record_document_submission(request_id, body.requirement_id)
        return {"status": "submitted", "document": document}
    except ValueError as exc:
        code = 404 if str(exc) == "Request not found" else 409
        raise HTTPException(status_code=code, detail=str(exc))
    except Exception as exc:
        logger.error("Could not record document submission: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Could not record the document submission")


@router.post("/documents/{document_id}/review")
def review_registration_document(document_id: int, body: DocumentReviewIn):
    with get_connection() as conn:
        reviewer = conn.execute(
            "select id, role from careflow.users where id = %s and is_active",
            (body.reviewer_id,),
        ).fetchone()
    if reviewer is None:
        raise HTTPException(status_code=400, detail="Unknown reviewer")
    if reviewer["role"] not in DOCUMENT_REVIEW_ROLES:
        raise HTTPException(status_code=403, detail="This role cannot review documents")
    try:
        result = review_document_submission(document_id, body.reviewer_id, body.decision)
        return {"status": "reviewed", **result}
    except ValueError as exc:
        code = 404 if str(exc) == "Document submission not found" else 409
        raise HTTPException(status_code=code, detail=str(exc))
    except Exception as exc:
        logger.error("Could not review document: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Could not review the document")


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

@router.post("/waitlist/accept")
def accept_waitlist(body: WaitlistAcceptIn, background: BackgroundTasks):
    try:
        result = accept_waitlist_offer(body.offer_code)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.error("Could not accept waitlist offer: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Could not accept the waitlist offer")
    background.add_task(run_confirmation, result["appointment_id"])
    return result


@router.post("/requests/{request_id}/waitlist-join", status_code=201)
def join_request_waitlist(
    request_id: int,
    body: WaitlistJoinIn | None = Body(default=None),
):
    try:
        result = join_waitlist_for_request(
            request_id,
            body.preferred_from if body else None,
            body.preferred_to if body else None,
        )
    except WaitlistJoinError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    if not result["email_sent"]:
        logger.error(
            "Waitlist confirmation email failed for request %s: %s",
            request_id,
            result["email_error"] or "unknown error",
        )
        raise HTTPException(
            status_code=503,
            detail="Joined the waitlist, but the confirmation email could not be sent",
        )
    return result


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