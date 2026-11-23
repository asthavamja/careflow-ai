import time
from agents.cancel import cancel_appointment, send_cancellation_notice, verify_cancel
from agents.waitlist import offer_freed_slot
from agents.checkpoint import checkpointer
from agents.trail import create_approval, link_patient
from agents.validation import validate_patient
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from agents.routing import PROMPT_VERSION as ROUTING_VERSION
from agents.routing import run_routing
from agents.intake import PROMPT_VERSION, run_intake
from agents.intake_checks import finalize_intake
from agents.state import CareFlowState
from agents.trail import change_status, log_step, save_intake_result,schedule_slot_followups
from agents.scheduling import PROMPT_VERSION as SCHEDULING_VERSION
from agents.scheduling import find_offers
from agents.trail import save_offers
from agents.alerts import send_urgent_alert
from api.config import settings
from api.db import get_connection


def intake_node(state: CareFlowState):
    rid = state["request_id"]
    out = run_intake(state["raw_text"])
    final = finalize_intake(state["raw_text"], out)

    save_intake_result(rid, final["request_type"], final["details"],
                       final["urgent_flag"], final["urgent_reason"])
    log_step(
        rid, "intake_agent", "llm",
        "failed" if out["llm_failed"] else "success",
        model=settings.groq_model, prompt_version=PROMPT_VERSION,
        input_summary="patient message",
        output={                       # safe fields only: no names or contact details
            "request_type": final["request_type"],
            "urgent_flag": final["urgent_flag"],
            "missing_fields": final["missing_fields"],
            "llm_failed": final["llm_failed"],
        },
        confidence=final["confidence"],
        input_tokens=out["input_tokens"], output_tokens=out["output_tokens"],
        latency_ms=out["latency_ms"], attempt=out["attempt"],
        error_message=out["error"],
    )
    return {
        "request_type": final["request_type"],
        "details": final["details"],
        "urgent_flag": final["urgent_flag"],
        "urgent_reason": final["urgent_reason"],
        "confidence": final["confidence"],
        "missing_fields": final["missing_fields"],
        "llm_failed": final["llm_failed"],
    }


def after_intake(state: CareFlowState) -> str:
    if state["urgent_flag"]:
        return "urgent"
    if state["llm_failed"]:
        return "failed"
    if state["request_type"] == "unclear" or state["missing_fields"]:
        return "clarify"
    return "continue"


def make_status_node(to_status: str, note: str):
    def node(state: CareFlowState):
        change_status(state["request_id"], to_status, "system", note)
        return {"next_status": to_status}
    return node

def validation_node(state: CareFlowState):
    rid = state["request_id"]
    started = time.perf_counter()
    try:
        res = validate_patient(state["request_type"], state["details"])
    except Exception as exc:
        log_step(rid, "validation_agent", "tool", "failed",
                 input_summary="details from intake", error_message=type(exc).__name__)
        return {"match_result": "error", "patient_id": None,
                "candidates": [], "validation_notes": []}

    if res["patient_id"]:
        link_patient(rid, res["patient_id"])
    log_step(
        rid, "validation_agent", "tool", "success",
        input_summary="details from intake",
        output={"match_result": res["match_result"],
                "candidate_codes": [c["patient_code"] for c in res["candidates"]]},
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return {"match_result": res["match_result"], "patient_id": res["patient_id"],
            "candidates": res["candidates"], "validation_notes": res["notes"]}


def after_validation(state: CareFlowState) -> str:
    match = state["match_result"]
    if match == "error":
        return "failed"
    if match in ("possible_duplicate", "conflict"):
        return "review"
    if match == "insufficient_info" or (match == "new" and state["request_type"] == "book_appointment"):
        return "ask"
    if match == "skipped" and state["request_type"] == "cancel":
        return "cancel"
    return "continue"


def review_node(state: CareFlowState):
    reason = "possible_duplicate" if state["match_result"] == "possible_duplicate" else "duplicate_conflict"
    aid = create_approval(state["request_id"], reason)
    change_status(state["request_id"], "awaiting_approval", "system",
                  f"Staff review needed: {state['match_result']}")
    return {"next_status": "awaiting_approval", "approval_id": aid, "review_reason": reason}

def validated_node(state: CareFlowState):
    if state["match_result"] in ("found", "new"):
        change_status(state["request_id"], "validated", "system",
                      f"Patient check: {state['match_result']}")
        return {"next_status": "validated"}
    return {}   # skipped (cancel, reschedule, documents): unchanged for now

def after_validated(state: CareFlowState) -> str:
    return "route" if state["request_type"] == "book_appointment" else "end"


def routing_node(state: CareFlowState):
    rid = state["request_id"]
    started = time.perf_counter()
    res = run_routing(state["details"].get("named_department"))
    used_llm = res["method"] == "llm"
    log_step(
        rid, "routing_agent", "llm" if used_llm else "tool",
        "failed" if res["llm_failed"] else "success",
        model=settings.groq_model if used_llm else None,
        prompt_version=ROUTING_VERSION if used_llm else None,
        input_summary="department wording",
        output={"department": res["department_name"], "method": res["method"],
                "requires_referral": res["requires_referral"]},
        confidence=res["confidence"],
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return {
        "department_id": res["department_id"],
        "department_name": res["department_name"],
        "requires_referral": res["requires_referral"],
        "routing_method": res["method"],
        "routing_confidence": res["confidence"],
        "llm_failed": res["llm_failed"],
    }


def after_routing(state: CareFlowState) -> str:
    if state["llm_failed"]:
        return "failed"
    if state["department_id"] is None:
        return "review"
    if state["requires_referral"]:
        return "review"
    return "routed"

def routing_review_node(state: CareFlowState):
    if state["department_id"] is None:
        reason, note = "unclear_department", "Department could not be decided"
    else:
        reason, note = "special_authorization", f"{state['department_name']} needs a referral"
    aid = create_approval(state["request_id"], reason)
    change_status(state["request_id"], "awaiting_approval", "system", note)
    return {"next_status": "awaiting_approval", "approval_id": aid, "review_reason": reason}

def scheduling_node(state: CareFlowState):
    rid = state["request_id"]
    started = time.perf_counter()
    res = find_offers(state["department_id"], state["details"].get("preferred_date_time"))
    log_step(
        rid, "scheduling_agent", "llm" if res["llm_used"] else "tool", "success",
        model=settings.groq_model if res["llm_used"] else None,
        prompt_version=SCHEDULING_VERSION if res["llm_used"] else None,
        input_summary="preferred time wording",
        output={"offers": len(res["offers"]), "source": res["source"],
                "widened": res["widened"]},
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return {"offers": res["offers"], "offer_source": res["source"]}


def after_scheduling(state: CareFlowState) -> str:
    return "offer" if state["offers"] else "review"


def offer_node(state: CareFlowState):
    save_offers(state["request_id"], state["offers"])
    schedule_slot_followups(state["request_id"])
    change_status(state["request_id"], "waiting_patient", "system",
                  f"{len(state['offers'])} slots offered, waiting for the patient to choose")
    return {"next_status": "waiting_patient"}


def no_slots_node(state: CareFlowState):
    aid = create_approval(state["request_id"], "other")
    change_status(state["request_id"], "awaiting_approval", "system",
                  "No free slots found for this department")
    return {"next_status": "awaiting_approval", "approval_id": aid, "review_reason": "other"}

def wait_for_staff_node(state: CareFlowState):
    # Only the pause lives here. On resume this step runs again from its first line.
    decision = interrupt({"approval_id": state["approval_id"], "reason": state["review_reason"]})
    return {"decision": decision}


def _department(department_id):
    with get_connection() as conn:
        return conn.execute(
            "select id, name, requires_referral from careflow.departments where id = %s and is_active",
            (department_id,),
        ).fetchone()


def apply_decision_node(state: CareFlowState):
    rid, d = state["request_id"], state["decision"]
    choice, who = d["decision"], d.get("reviewer_id")

    def status(to, note):
        change_status(rid, to, "staff", note, actor_user_id=who)

    if choice == "reject":
        status("rejected", "Staff rejected the request")
        return {"outcome": "stop"}
    if choice in ("more_info_needed", "request_referral"):
        status("waiting_patient", "Staff asked the patient for more information")
        return {"outcome": "stop"}
    if choice == "same_person":
        link_patient(rid, d["patient_id"])
        if state["request_type"] == "new_registration":
            status("closed", "Matched an existing patient; no new record created")
            return {"outcome": "stop", "patient_id": d["patient_id"]}
        status("validated", "Staff confirmed the patient record")
        return {"outcome": "to_routing", "patient_id": d["patient_id"]}
    if choice == "different_person":
        if state["request_type"] == "new_registration":
            status("validated", "Staff: different person, register as a new patient")
        else:
            status("waiting_patient", "Staff: no matching patient, registration needed first")
        return {"outcome": "stop"}
    if choice == "choose_department":
        dept = _department(d["department_id"])
        status("routed", "Staff chose the department")
        return {"outcome": "to_scheduling", "department_id": dept["id"],
                "department_name": dept["name"],
                "requires_referral": bool(dept["requires_referral"])}
    if choice == "approve_without_referral":
        status("routed", "Staff approved without a referral")
        return {"outcome": "to_scheduling"}
    status("failed", "Unknown staff decision")
    return {"outcome": "stop"}


def after_decision(state: CareFlowState) -> str:
    return state["outcome"]

def urgent_node(state: CareFlowState):
    rid = state["request_id"]
    change_status(rid, "urgent_escalated", "system", "Urgent wording detected")
    try:
        res = send_urgent_alert(rid)
        ok, err = res["status"] in ("sent", "already_sent"), res.get("error")
    except Exception as exc:
        ok, err = False, type(exc).__name__
    log_step(rid, "urgent_alert", "tool", "success" if ok else "failed",
             input_summary="urgent flag", output={"alert_sent": ok},
             error_message=None if ok else err)
    return {"next_status": "urgent_escalated"}

def cancel_node(state: CareFlowState):
    rid = state["request_id"]
    appt, reason = verify_cancel(state["details"])
    slot_id = cancel_appointment(appt["id"]) if appt else None
    if slot_id is None:
        reason = reason or "appointment no longer active"
        aid = create_approval(rid, "failed_identity_check")
        change_status(rid, "awaiting_approval", "system", "Cancellation could not be verified")
        log_step(rid, "cancel_agent", "tool", "success", input_summary="cancellation check",
                 output={"verified": False, "reason": reason})
        return {"next_status": "awaiting_approval", "approval_id": aid,
                "review_reason": "failed_identity_check", "cancel_result": "review"}
    sent = send_cancellation_notice(rid, appt)
    try:
        offer = offer_freed_slot(slot_id)
    except Exception:
        offer = None
    log_step(rid, "cancel_agent", "tool", "success", input_summary="cancellation check",
             output={"verified": True, "freed_slot_id": slot_id, "notice_sent": sent,
                     "waitlist_offered": offer is not None})
    change_status(rid, "closed", "system", "Appointment cancelled and slot freed")
    return {"next_status": "closed", "freed_slot_id": slot_id, "cancel_result": "done"}


def after_cancel(state: CareFlowState) -> str:
    return "review" if state["cancel_result"] == "review" else "end"

builder = StateGraph(CareFlowState)
builder.add_node("intake_agent", intake_node)
builder.add_node("validation_agent", validation_node)
builder.add_node("routing_agent", routing_node)
builder.add_node("scheduling_agent", scheduling_node)
builder.add_node("escalate_urgent", urgent_node)
builder.add_node("mark_failed", make_status_node("failed", "A step could not finish"))
builder.add_node("ask_patient", make_status_node("waiting_patient", "More information needed"))
builder.add_node("staff_review", review_node)
builder.add_node("mark_validated", validated_node)
builder.add_node("routing_review", routing_review_node)
builder.add_node("mark_routed", make_status_node("routed", "Department decided"))
builder.add_node("offer_slots", offer_node)
builder.add_node("no_slots", no_slots_node)
builder.add_node("wait_for_staff", wait_for_staff_node)
builder.add_node("apply_decision", apply_decision_node)
builder.add_node("cancel_agent", cancel_node)

builder.add_edge(START, "intake_agent")
builder.add_conditional_edges(
    "intake_agent", after_intake,
    {"urgent": "escalate_urgent", "failed": "mark_failed",
     "clarify": "ask_patient", "continue": "validation_agent"},
)
builder.add_conditional_edges(
    "validation_agent", after_validation,
        {"failed": "mark_failed", "review": "staff_review",
        "ask": "ask_patient", "continue": "mark_validated", "cancel": "cancel_agent"},
)
builder.add_conditional_edges(
    "mark_validated", after_validated,
    {"route": "routing_agent", "end": END},
)
builder.add_conditional_edges(
    "routing_agent", after_routing,
    {"failed": "mark_failed", "review": "routing_review", "routed": "mark_routed"},
)
builder.add_edge("mark_routed", "scheduling_agent")
builder.add_conditional_edges(
    "scheduling_agent", after_scheduling,
    {"offer": "offer_slots", "review": "no_slots"},
)
for name in ("escalate_urgent", "mark_failed", "ask_patient", "offer_slots"):
    builder.add_edge(name, END)
for name in ("staff_review", "routing_review", "no_slots"):
    builder.add_edge(name, "wait_for_staff")
builder.add_edge("wait_for_staff", "apply_decision")
builder.add_conditional_edges(
    "apply_decision", after_decision,
    {"stop": END, "to_routing": "routing_agent", "to_scheduling": "scheduling_agent"},
)
builder.add_conditional_edges(
    "cancel_agent", after_cancel,
    {"review": "wait_for_staff", "end": END},
)
graph = builder.compile(checkpointer=checkpointer)

def run_request(request_id: int):
    with get_connection() as conn:
        row = conn.execute(
            "select raw_text, status from careflow.patient_requests where id = %s",
            (request_id,),
        ).fetchone()
    if row is None:
        raise ValueError("request not found")
    if row["status"] != "received":
        raise ValueError(f"request is already '{row['status']}', not running it again")
    config = {"configurable": {"thread_id": f"request-{request_id}"}}
    return graph.invoke({"request_id": request_id, "raw_text": row["raw_text"], "errors": []}, config)