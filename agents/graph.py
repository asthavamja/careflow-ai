from datetime import datetime, timedelta, timezone
import time
from agents.cancel import cancel_appointment, send_cancellation_notice, verify_cancel
from agents.waitlist import notify_no_slots_patient, offer_freed_slot
from agents.documents import start_document_followup
from agents.notify import send_via_n8n
from agents.checkpoint import checkpointer
from agents.trail import create_approval, link_patient
from agents.validation import validate_patient
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from agents.routing import PROMPT_VERSION as ROUTING_VERSION
from agents.routing import run_routing
from agents.intake import PROMPT_VERSION, run_intake
from agents.intake_checks import compute_missing, finalize_intake
from agents.communication import write_clarification
from agents.registration import register_new_patient
from agents.state import CareFlowState
from agents.trail import (
    change_status,
    log_step,
    mark_waitlist_request,
    save_reschedule_context,
    save_intake_result,
    schedule_slot_followups,
)
from agents.scheduling import PROMPT_VERSION as SCHEDULING_VERSION
from agents.scheduling import find_offers, find_reschedule_offers
from agents.trail import save_offers
from agents.alerts import send_urgent_alert
from api.config import settings
from api.db import get_connection


def intake_node(state: CareFlowState):
    rid = state["request_id"]
    out = run_intake(state["raw_text"])
    final = finalize_intake(state["raw_text"], out)
    if (
        final["request_type"] == "new_registration"
        and not final["details"].get("email")
        and state.get("contact_email")
    ):
        final["details"]["email"] = state["contact_email"]
        final["missing_fields"] = compute_missing(
            final["request_type"], final["details"],
        )

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


def _unclear_request_approval(request_id: int, note: str) -> dict:
    approval_id = create_approval(request_id, "unclear_request")
    change_status(request_id, "awaiting_approval", "system", note)
    log_step(
        request_id, "communication_agent", "tool", "success",
        input_summary="clarification could not resolve the request",
        output={"approval_created": True, "reason": "unclear_request"},
    )
    return {
        "next_status": "awaiting_approval",
        "approval_id": approval_id,
        "review_reason": "unclear_request",
    }


def ask_patient_node(state: CareFlowState):
    request_id = state["request_id"]
    with get_connection() as conn:
        req = conn.execute(
            """select contact_email, patient_id, extracted_data, clarification_count
               from careflow.patient_requests where id = %s""",
            (request_id,),
        ).fetchone()
    if req is None:
        raise ValueError("Request not found")

    if req["clarification_count"] >= 1:
        return _unclear_request_approval(
            request_id, "Request remained unclear after one patient clarification",
        )

    details = req["extracted_data"] or {}
    email = req["contact_email"] or details.get("email")
    if not email:
        return _unclear_request_approval(
            request_id, "No contact email is available for clarification",
        )

    missing_fields = state.get("missing_fields") or []
    if not missing_fields and state.get("request_type") == "unclear":
        missing_fields = ["request_type"]
    drafted = write_clarification(missing_fields)
    first_name = details.get("first_name") or "there"
    body = (
        f"Hello {first_name},\n\n"
        f"{drafted['question']}\n\n"
        "Please share the requested information with the CareFlow registration team.\n\n"
        "CareFlow Demo Hospital (simulated)"
    )

    sent = False
    error = None
    try:
        result = send_via_n8n(
            request_id=request_id,
            patient_id=req["patient_id"],
            key=f"request-{request_id}-clarification-1",
            kind="patient_clarification",
            to_email=email,
            subject="A quick question about your request",
            body=body,
            template="patient_clarification_v1",
        )
        sent = result["status"] in ("sent", "already_sent")
        error = result.get("error")
    except Exception as exc:
        error = type(exc).__name__

    if sent:
        change_status(request_id, "waiting_patient", "system", "More information needed")
    else:
        change_status(
            request_id, "failed", "system",
            "Could not send the patient clarification message",
        )
    log_step(
        request_id, "communication_agent", "tool", "success" if sent else "failed",
        input_summary="clarification fields only",
        output={
            "missing_fields": missing_fields,
            "fallback_used": drafted["fallback_used"],
            "message_sent": sent,
        },
        model=settings.groq_model,
        prompt_version="communication_clarification_v1",
        input_tokens=drafted["input_tokens"],
        output_tokens=drafted["output_tokens"],
        latency_ms=drafted["latency_ms"],
        attempt=drafted["attempts"],
        error_message=error or drafted["error"],
    )
    return {"next_status": "waiting_patient" if sent else "failed"}


def after_ask_patient(state: CareFlowState) -> str:
    return "review" if state["next_status"] == "awaiting_approval" else "end"


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
    if match == "skipped" and state["request_type"] == "reschedule":
        return "reschedule"
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


def register_patient_node(state: CareFlowState):
    rid = state["request_id"]
    try:
        result = register_new_patient(rid)
    except ValueError as exc:
        change_status(rid, "failed", "system", "New registration data could not be saved")
        log_step(
            rid, "registration_agent", "tool", "failed",
            input_summary="validated new registration",
            error_message=str(exc),
        )
        return {"next_status": "failed"}

    if result["requires_approval"]:
        approval_id = create_approval(rid, "minor_patient")
        change_status(rid, "awaiting_approval", "system",
                      "Minor registration requires staff approval")
        log_step(
            rid, "registration_agent", "tool", "success",
            input_summary="minor registration",
            output={"approval_required": True, "reason": "minor_patient"},
        )
        return {
            "next_status": "awaiting_approval",
            "approval_id": approval_id,
            "review_reason": "minor_patient",
        }

    log_step(
        rid, "registration_agent", "tool", "success",
        input_summary="validated new registration",
        output={"patient_created": result["created"]},
    )
    return {"next_status": "validated", "patient_id": result["patient_id"]}


def after_registration(state: CareFlowState) -> str:
    if state["next_status"] == "awaiting_approval":
        return "review"
    if state["next_status"] == "validated":
        return "documents"
    return "end"


def after_validated(state: CareFlowState) -> str:
    if state["request_type"] == "book_appointment":
        return "route"
    if state["request_type"] == "new_registration":
        return "register_patient"
    return "end"


def document_request_node(state: CareFlowState):
    rid = state["request_id"]
    result = start_document_followup(rid)
    ok = result["status"] == "closed" or (
        result["status"] == "waiting_patient" and result.get("notification_sent", False)
    )
    log_step(
        rid, "document_agent", "tool", "success" if ok else "failed",
        input_summary="registration document requirements",
        output={"required_count": result.get("required_count", 0),
                "notification_sent": result.get("notification_sent", False)},
        error_message=result.get("error") or result.get("notification_error"),
    )
    return {"next_status": result["status"]}


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
    department_id = state.get("department_id")
    if department_id is None:
        raise ValueError("Department is required for slot search")
    started = time.perf_counter()
    res = find_offers(department_id, state["details"].get("preferred_date_time"))
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
    rid = state["request_id"]
    context = state.get("reschedule_context") or {}
    department_id = context.get("department_id") or state.get("department_id")
    patient_id = context.get("patient_id") or state.get("patient_id")
    notice = {"sent": False, "error": "patient_or_department_unavailable"}
    if patient_id is not None and department_id is not None:
        notice = notify_no_slots_patient(rid, patient_id, department_id)

    if notice["sent"]:
        mark_waitlist_request(
            rid, department_id, "waiting_patient",
            "No appointment slots available; patient notified about the waitlist",
        )
        log_step(
            rid, "waitlist_agent", "tool", "success",
            input_summary="no slots for selected department",
            output={"waitlist_option_sent": True},
        )
        return {"next_status": "waiting_patient"}

    approval_id = create_approval(rid, "other")
    mark_waitlist_request(
        rid, department_id, "awaiting_approval",
        "No slots available; staff review required",
    )
    log_step(
        rid, "waitlist_agent", "tool", "failed",
        input_summary="no slots for selected department",
        output={"waitlist_option_sent": False, "staff_review_required": True},
        error_message=notice["error"],
    )
    return {
        "next_status": "awaiting_approval",
        "approval_id": approval_id,
        "review_reason": "other",
    }


def after_no_slots(state: CareFlowState) -> str:
    return "review" if state["next_status"] == "awaiting_approval" else "end"

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
            status("validated", "Staff confirmed the existing patient record")
            return {"outcome": "to_documents", "patient_id": d["patient_id"]}
        status("validated", "Staff confirmed the patient record")
        return {"outcome": "to_routing", "patient_id": d["patient_id"]}
    if choice == "approve_registration" and state["request_type"] == "new_registration":
        try:
            result = register_new_patient(
                rid, allow_minor=True, actor="staff", actor_user_id=who,
            )
        except ValueError as exc:
            status("failed", "Approved registration data could not be saved")
            log_step(
                rid, "registration_agent", "tool", "failed",
                input_summary="staff-approved minor registration",
                error_message=str(exc),
            )
            return {"outcome": "stop"}
        log_step(
            rid, "registration_agent", "tool", "success",
            input_summary="staff-approved minor registration",
            output={"patient_created": result["created"]},
        )
        return {
            "outcome": "to_documents",
            "patient_id": result["patient_id"],
        }
    if choice == "different_person":
        if state["request_type"] == "new_registration":
            status("validated", "Staff: different person, register as a new patient")
            return {"outcome": "to_registration"}
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


def reschedule_node(state: CareFlowState):
    rid = state["request_id"]
    appointment, reason = verify_cancel(state["details"])
    if appointment is not None:
        with get_connection() as conn:
            row = conn.execute(
                """select value from careflow.system_settings
                   where key = 'slot_release_cutoff_hours'"""
            ).fetchone()
        cutoff_hours = int(row["value"]) if row else 4
        if appointment["start_time"] <= (
            datetime.now(timezone.utc) + timedelta(hours=cutoff_hours)
        ):
            appointment, reason = None, "appointment inside slot release cutoff"

    if appointment is None:
        approval_id = create_approval(rid, "failed_identity_check")
        change_status(rid, "awaiting_approval", "system",
                      "Rescheduling could not be verified")
        log_step(
            rid, "reschedule_agent", "tool", "failed",
            input_summary="reschedule verification",
            output={"verified": False},
            error_message=reason,
        )
        return {
            "next_status": "awaiting_approval",
            "approval_id": approval_id,
            "review_reason": "failed_identity_check",
            "reschedule_result": "review",
        }

    context = {
        "appointment_id": appointment["id"],
        "patient_id": appointment["patient_id"],
        "department_id": appointment["department_id"],
        "doctor_id": appointment["doctor_id"],
        "old_slot_id": appointment["slot_id"],
    }
    save_reschedule_context(
        rid,
        context["appointment_id"],
        context["patient_id"],
        context["department_id"],
    )
    result = find_reschedule_offers(
        context["department_id"],
        state["details"].get("preferred_date_time"),
        context["old_slot_id"],
        context["doctor_id"],
    )
    offers = result["offers"]
    log_step(
        rid, "reschedule_agent", "tool", "success",
        input_summary="reschedule timing wording",
        output={"verified": True, "offers": len(offers)},
    )

    if not offers:
        return {
            "next_status": "no_slots",
            "reschedule_result": "no_slots",
            "reschedule_context": context,
        }

    save_offers(rid, offers)
    schedule_slot_followups(rid)
    change_status(
        rid,
        "waiting_patient",
        "system",
        f"{len(offers)} reschedule slots offered, waiting for the patient to choose",
    )
    return {
        "next_status": "waiting_patient",
        "reschedule_result": "offered",
        "reschedule_context": context,
    }


def after_cancel(state: CareFlowState) -> str:
    return "review" if state["cancel_result"] == "review" else "end"


def after_reschedule(state: CareFlowState) -> str:
    return state["reschedule_result"]


builder = StateGraph(CareFlowState)
builder.add_node("intake_agent", intake_node)
builder.add_node("validation_agent", validation_node)
builder.add_node("routing_agent", routing_node)
builder.add_node("scheduling_agent", scheduling_node)
builder.add_node("escalate_urgent", urgent_node)
builder.add_node("mark_failed", make_status_node("failed", "A step could not finish"))
builder.add_node("ask_patient", ask_patient_node)
builder.add_node("staff_review", review_node)
builder.add_node("mark_validated", validated_node)
builder.add_node("routing_review", routing_review_node)
builder.add_node("mark_routed", make_status_node("routed", "Department decided"))
builder.add_node("offer_slots", offer_node)
builder.add_node("no_slots", no_slots_node)
builder.add_node("wait_for_staff", wait_for_staff_node)
builder.add_node("apply_decision", apply_decision_node)
builder.add_node("cancel_agent", cancel_node)
builder.add_node("reschedule_agent", reschedule_node)
builder.add_node("request_documents", document_request_node)
builder.add_node("register_patient", register_patient_node)

builder.add_edge(START, "intake_agent")
builder.add_conditional_edges(
    "intake_agent", after_intake,
    {"urgent": "escalate_urgent", "failed": "mark_failed",
     "clarify": "ask_patient", "continue": "validation_agent"},
)
builder.add_conditional_edges(
    "validation_agent", after_validation,
        {"failed": "mark_failed", "review": "staff_review",
        "ask": "ask_patient", "continue": "mark_validated", "cancel": "cancel_agent",
        "reschedule": "reschedule_agent"},
)
builder.add_conditional_edges(
    "mark_validated", after_validated,
    {"route": "routing_agent", "register_patient": "register_patient", "end": END},
)
builder.add_conditional_edges(
    "register_patient", after_registration,
    {"review": "wait_for_staff", "documents": "request_documents", "end": END},
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
for name in ("escalate_urgent", "mark_failed", "offer_slots"):
    builder.add_edge(name, END)
builder.add_conditional_edges(
    "ask_patient", after_ask_patient,
    {"review": "wait_for_staff", "end": END},
)
builder.add_edge("request_documents", END)
for name in ("staff_review", "routing_review"):
    builder.add_edge(name, "wait_for_staff")
builder.add_conditional_edges(
    "no_slots", after_no_slots,
    {"review": "wait_for_staff", "end": END},
)
builder.add_edge("wait_for_staff", "apply_decision")
builder.add_conditional_edges(
    "apply_decision", after_decision,
    {
        "stop": END,
        "to_registration": "register_patient",
        "to_documents": "request_documents",
        "to_routing": "routing_agent",
        "to_scheduling": "scheduling_agent",
    },
)
builder.add_conditional_edges(
    "cancel_agent", after_cancel,
    {"review": "wait_for_staff", "end": END},
)
builder.add_conditional_edges(
    "reschedule_agent", after_reschedule,
    {"offered": END, "review": "wait_for_staff", "no_slots": "no_slots"},
)
graph = builder.compile(checkpointer=checkpointer)

def run_request(request_id: int):
    with get_connection() as conn:
        row = conn.execute(
            "select raw_text, status, contact_email from careflow.patient_requests where id = %s",
            (request_id,),
        ).fetchone()
    if row is None:
        raise ValueError("request not found")
    if row["status"] != "received":
        raise ValueError(f"request is already '{row['status']}', not running it again")
    config = {"configurable": {"thread_id": f"request-{request_id}"}}
    return graph.invoke(
        {
            "request_id": request_id,
            "raw_text": row["raw_text"],
            "contact_email": row["contact_email"],
            "errors": [],
        },
        config,
    )