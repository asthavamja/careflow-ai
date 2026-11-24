import logging

from fastapi import APIRouter, HTTPException
from langgraph.types import Command
from psycopg.types.json import Jsonb

from agents.graph import graph
from agents.review import get_approval_view
from api.db import get_connection
from api.schemas import DecideIn

logger = logging.getLogger("careflow")
router = APIRouter()

ROLES_BY_LEVEL = {1: {"receptionist", "coordinator", "approver"}, 2: {"approver"}}
DB_DECISION = {
    "same_person": "approved", "different_person": "approved",
    "approve_registration": "approved",
    "choose_department": "approved", "approve_without_referral": "approved",
    "reject": "rejected",
    "more_info_needed": "more_info_needed", "request_referral": "more_info_needed",
}


@router.get("/approvals/pending")
def list_pending():
    with get_connection() as conn:
        return conn.execute(
            """select id, request_id, reason_for_review, level, created_at
               from careflow.approvals where decision = 'pending' order by created_at"""
        ).fetchall()


@router.get("/approvals/{approval_id}")
def view_approval(approval_id: int):
    view = get_approval_view(approval_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    return view


@router.post("/approvals/{approval_id}/decide")
def decide(approval_id: int, body: DecideIn):
    view = get_approval_view(approval_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    if view["decision"] != "pending":
        raise HTTPException(status_code=409, detail="This approval was already decided")
    if body.decision not in view["allowed_decisions"]:
        raise HTTPException(status_code=400, detail=f"Allowed decisions: {view['allowed_decisions']}")

    with get_connection() as conn:
        user = conn.execute(
            "select id, role from careflow.users where id = %s and is_active", (body.reviewer_id,)
        ).fetchone()
        dept_ok = True
        if body.decision == "choose_department":
            dept_ok = body.department_id is not None and conn.execute(
                "select 1 from careflow.departments where id = %s and is_active",
                (body.department_id,),
            ).fetchone() is not None
    if user is None:
        raise HTTPException(status_code=400, detail="Unknown reviewer")
    if user["role"] not in ROLES_BY_LEVEL.get(view["level"], set()):
        raise HTTPException(status_code=403, detail="This role cannot decide this approval")
    if body.decision == "same_person" and body.patient_id not in [c["patient_id"] for c in view["candidates"]]:
        raise HTTPException(status_code=400, detail="patient_id must be one of the candidates")
    if not dept_ok:
        raise HTTPException(status_code=400, detail="A valid department_id is required")

    # Record the decision in one step. Only the first decider wins.
    with get_connection() as conn:
        row = conn.execute(
            """update careflow.approvals
               set decision = %s, reviewer_id = %s, decision_reason = %s, decided_at = now()
               where id = %s and decision = 'pending' returning request_id""",
            (DB_DECISION[body.decision], body.reviewer_id, body.reason, approval_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=409, detail="This approval was already decided")
        conn.execute(
            """insert into careflow.audit_logs
                   (actor_type, actor_user_id, action, entity_type, entity_id, after_data)
               values ('user', %s, 'approval_decided', 'approval', %s, %s)""",
            (body.reviewer_id, str(approval_id),
             Jsonb({"decision": body.decision, "request_id": row["request_id"]})),
        )

    request_id = row["request_id"]
    try:
        graph.invoke(
            Command(resume={"decision": body.decision, "reviewer_id": body.reviewer_id,
                            "patient_id": body.patient_id, "department_id": body.department_id}),
            {"configurable": {"thread_id": f"request-{request_id}"}},
        )
    except Exception as exc:
        logger.error("Could not resume request %s: %s", request_id, type(exc).__name__)
        raise HTTPException(status_code=500,
                            detail="Decision saved, but the workflow could not continue")

    with get_connection() as conn:
        status = conn.execute(
            "select status from careflow.patient_requests where id = %s", (request_id,)
        ).fetchone()["status"]
    return {"status": "decided", "request_id": request_id, "request_status": status}