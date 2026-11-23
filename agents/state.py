from typing import Optional, TypedDict

class CareFlowState(TypedDict, total=False):
    request_id: int
    raw_text: str

    # Written by the Intake Agent
    request_type: str
    details: dict
    urgent_flag: bool
    urgent_reason: Optional[str]
    confidence: float
    missing_fields: list
    llm_failed: bool

    # Written by the Validation Agent
    match_result: str
    patient_id: Optional[int]
    candidates: list
    validation_notes: list

    # Written by the orchestrator
    next_status: str
    errors: list
    # Written by the Routing Agent
    department_id: Optional[int]
    department_name: Optional[str]
    requires_referral: bool
    routing_method: str
    routing_confidence: float
    # Written by the Scheduling Agent
    offers: list
    offer_source: str
    # Written by the review steps
    approval_id: int
    review_reason: str
    decision: dict
    outcome: str
    # Written by the cancel step
    cancel_result: str
    freed_slot_id: int