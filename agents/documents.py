import logging

from agents.notify import send_via_n8n
from api.db import get_connection

logger = logging.getLogger("careflow")


def _request(conn, request_id: int, *, lock: bool = False) -> dict:
    suffix = " for update of req" if lock else ""
    row = conn.execute(
        """select req.id, req.request_type, req.status, req.patient_id,
                  req.extracted_data, req.contact_email as request_contact_email,
                  p.first_name as patient_first_name,
                  p.email as patient_email
           from careflow.patient_requests req
           left join careflow.patients p on p.id = req.patient_id
           where req.id = %s""" + suffix,
        (request_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Request not found")
    if row["request_type"] != "new_registration":
        raise ValueError("Document follow-up currently supports new registrations only")
    return row


def _checklist(conn, request_id: int) -> list[dict]:
    return conn.execute(
        """select r.id as requirement_id, r.document_name, r.is_mandatory,
                  latest.id as document_id, latest.status, latest.created_at as submitted_at,
                  latest.reviewed_at
           from careflow.document_requirements r
           left join lateral (
               select d.id, d.status, d.created_at, d.reviewed_at
               from careflow.patient_documents d
               where d.request_id = %s and d.requirement_id = r.id
               order by d.created_at desc, d.id desc
               limit 1
           ) latest on true
           where r.request_type = 'new_registration' and r.department_id is null
           order by r.id""",
        (request_id,),
    ).fetchall()


def _required_to_submit(checklist: list[dict]) -> list[dict]:
    return [
        row for row in checklist
        if row["is_mandatory"] and row["status"] not in ("submitted", "accepted")
    ]


def _all_required_accepted(checklist: list[dict]) -> bool:
    required = [row for row in checklist if row["is_mandatory"]]
    return bool(required) and all(row["status"] == "accepted" for row in required)


def _request_email(req: dict, details: dict) -> str | None:
    return req["patient_email"] or req["request_contact_email"] or details.get("email")


def _hours(conn) -> tuple[int, int]:
    settings = {row["key"]: int(row["value"]) for row in conn.execute(
        """select key, value from careflow.system_settings
           where key in ('patient_reminder_hours', 'patient_expire_hours')"""
    ).fetchall()}
    reminder = settings.get("patient_reminder_hours", 12)
    expiry = settings.get("patient_expire_hours", 24)
    if reminder <= 0 or expiry <= reminder:
        raise ValueError("Document reminder/expiry settings must be positive and expiry later than reminder")
    return reminder, expiry


def _event(conn, request_id: int, from_status: str, to_status: str, note: str, actor="system",
           actor_user_id=None):
    conn.execute(
        """insert into careflow.request_events
               (request_id, from_status, to_status, actor, actor_user_id, note)
           values (%s, %s, %s, %s, %s, %s)""",
        (request_id, from_status, to_status, actor, actor_user_id, note),
    )


def _set_request_status(conn, request_id: int, current: str, target: str, note: str):
    if current == target:
        return
    conn.execute(
        """update careflow.patient_requests set status = %s, updated_at = now()
           where id = %s""",
        (target, request_id),
    )
    _event(conn, request_id, current, target, note)


def _cancel_followups(conn, request_id: int):
    conn.execute(
        """update careflow.follow_ups set status = 'cancelled'
           where request_id = %s and kind = 'missing_document' and status = 'pending'""",
        (request_id,),
    )


def _ensure_followup(conn, request_id: int, *, immediate: bool = False):
    reminder_hours, _ = _hours(conn)
    row = conn.execute(
        """select id from careflow.follow_ups
           where request_id = %s and kind = 'missing_document' and status = 'pending'
           order by created_at desc limit 1 for update""",
        (request_id,),
    ).fetchone()
    if row:
        if immediate:
            conn.execute(
                "update careflow.follow_ups set due_at = now() where id = %s",
                (row["id"],),
            )
        return
    conn.execute(
        """insert into careflow.follow_ups (request_id, kind, due_at)
           values (%s, 'missing_document',
                   now() + make_interval(hours => %s))""",
        (request_id, 0 if immediate else reminder_hours),
    )


def get_document_checklist(request_id: int) -> list[dict]:
    """Return registration requirements with each requirement's latest submission."""
    with get_connection() as conn:
        _request(conn, request_id)
        return _checklist(conn, request_id)


def start_document_followup(request_id: int) -> dict:
    """Ask a new-registration patient for missing mandatory documents."""
    with get_connection() as conn:
        req = _request(conn, request_id, lock=True)
        checklist = _checklist(conn, request_id)
        if not checklist:
            raise ValueError("No document requirements are configured for new registrations")

        if req["status"] not in ("validated", "waiting_patient"):
            raise ValueError("Registration request is not ready for document follow-up")

        missing = _required_to_submit(checklist)
        if _all_required_accepted(checklist):
            _set_request_status(
                conn, request_id, req["status"], "closed",
                "Registration is complete; all required documents are accepted",
            )
            _cancel_followups(conn, request_id)
            return {"status": "closed", "required_count": 0, "notification_sent": False}
        if not missing:
            _set_request_status(
                conn, request_id, req["status"], "waiting_patient",
                "Submitted registration documents are waiting for staff review",
            )
            _cancel_followups(conn, request_id)
            return {"status": "waiting_patient", "required_count": 0, "notification_sent": False}

        details = req["extracted_data"] or {}
        email = _request_email(req, details)
        first_name = req["patient_first_name"] or details.get("first_name") or "there"
        if not email:
            _set_request_status(
                conn, request_id, req["status"], "failed",
                "Cannot request required documents without a contact email",
            )
            return {"status": "failed", "error": "missing_contact_email"}

        _hours(conn)
        _set_request_status(
            conn, request_id, req["status"], "waiting_patient",
            "Waiting for required registration documents",
        )
        _ensure_followup(conn, request_id)

    required_names = "\n".join(f"- {row['document_name']}" for row in missing)
    optional_names = "\n".join(
        f"- {row['document_name']}" for row in checklist
        if not row["is_mandatory"] and row["status"] != "accepted"
    )
    body = (
        f"Hello {first_name},\n\n"
        "Welcome to CareFlow. To complete your registration, please provide these required documents:\n"
        f"{required_names}\n\n"
        "Please use the submission instructions provided by the registration team. "
        "A staff member will review each document before it is accepted."
    )
    if optional_names:
        body += f"\n\nOptional documents:\n{optional_names}"
    body += "\n\nCareFlow Demo Hospital (simulated)"

    try:
        result = send_via_n8n(
            request_id=request_id,
            patient_id=req["patient_id"],
            key=f"registration-{request_id}-documents-request",
            kind="missing_document_request",
            to_email=email,
            subject="Welcome to CareFlow - documents needed to complete registration",
            body=body,
            template="registration_documents_request_v1",
        )
        sent = result["status"] in ("sent", "already_sent")
        error = result.get("error")
    except Exception as exc:
        sent, error = False, type(exc).__name__

    if not sent:
        logger.error("Document request notification failed: %s", error or "unknown error")
        with get_connection() as conn:
            conn.execute(
                """update careflow.follow_ups set due_at = now()
                   where request_id = %s and kind = 'missing_document' and status = 'pending'""",
                (request_id,),
            )
    return {
        "status": "waiting_patient",
        "required_count": len(missing),
        "notification_sent": sent,
        "notification_error": error,
    }


def record_document_submission(request_id: int, requirement_id: int) -> dict:
    """Record a simulated submission for staff review; no file is stored."""
    with get_connection() as conn:
        req = _request(conn, request_id, lock=True)
        if req["status"] != "waiting_patient":
            raise ValueError("Request is not waiting for registration documents")
        requirement = conn.execute(
            """select id from careflow.document_requirements
               where id = %s and request_type = 'new_registration'
                 and department_id is null""",
            (requirement_id,),
        ).fetchone()
        if requirement is None:
            raise ValueError("Document requirement not found")

        previous = conn.execute(
            """select id, status from careflow.patient_documents
               where request_id = %s and requirement_id = %s
               order by created_at desc, id desc limit 1 for update""",
            (request_id, requirement_id),
        ).fetchone()
        if previous and previous["status"] == "accepted":
            raise ValueError("Document was already accepted")
        if previous and previous["status"] == "submitted":
            raise ValueError("Document is already waiting for staff review")

        row = conn.execute(
            """insert into careflow.patient_documents
                   (request_id, patient_id, requirement_id, status)
               values (%s, %s, %s, 'submitted')
               returning id, request_id, requirement_id, status, created_at""",
            (request_id, req["patient_id"], requirement_id),
        ).fetchone()
        _event(
            conn, request_id, req["status"], req["status"],
            f"Document requirement {requirement_id} submitted for staff review",
        )

        checklist = _checklist(conn, request_id)
        if not _required_to_submit(checklist):
            _cancel_followups(conn, request_id)

    return row


def review_document_submission(
    document_id: int, reviewer_id: int, decision: str
) -> dict:
    if decision not in ("accepted", "rejected"):
        raise ValueError("Decision must be accepted or rejected")

    with get_connection() as conn:
        doc = conn.execute(
            """select d.id, d.request_id, d.requirement_id, d.status,
                      req.status as request_status
               from careflow.patient_documents d
               join careflow.patient_requests req on req.id = d.request_id
               where d.id = %s and req.request_type = 'new_registration'
               for update of d, req""",
            (document_id,),
        ).fetchone()
        if doc is None:
            raise ValueError("Document submission not found")
        if doc["status"] != "submitted":
            raise ValueError("Document is no longer waiting for review")

        row = conn.execute(
            """update careflow.patient_documents
               set status = %s, reviewed_by = %s, reviewed_at = now()
               where id = %s and status = 'submitted'
               returning id, request_id, requirement_id, status, reviewed_by, reviewed_at""",
            (decision, reviewer_id, document_id),
        ).fetchone()
        checklist = _checklist(conn, doc["request_id"])
        missing = _required_to_submit(checklist)

        _event(
            conn, doc["request_id"], doc["request_status"], doc["request_status"],
            f"Document submission {decision} by staff",
            actor="staff", actor_user_id=reviewer_id,
        )
        if _all_required_accepted(checklist):
            _set_request_status(
                conn, doc["request_id"], doc["request_status"], "closed",
                "Registration is complete; all required documents are accepted",
            )
            _cancel_followups(conn, doc["request_id"])
        elif missing:
            if decision == "rejected":
                _set_request_status(
                    conn, doc["request_id"], doc["request_status"], "waiting_patient",
                    "A registration document needs to be resubmitted",
                )
                _ensure_followup(conn, doc["request_id"], immediate=True)
            else:
                _ensure_followup(conn, doc["request_id"])
        else:
            _cancel_followups(conn, doc["request_id"])

    return {"document": row, "all_required_accepted": _all_required_accepted(checklist)}


def get_document_reminder_context(request_id: int) -> dict:
    with get_connection() as conn:
        req = _request(conn, request_id)
        checklist = _checklist(conn, request_id)
    details = req["extracted_data"] or {}
    return {
        "request_status": req["status"],
        "patient_id": req["patient_id"],
        "email": _request_email(req, details),
        "first_name": req["patient_first_name"] or details.get("first_name") or "there",
        "missing": _required_to_submit(checklist),
    }


def send_document_reminder(request_id: int, attempt: int) -> dict:
    context = get_document_reminder_context(request_id)
    if not context["missing"]:
        return {"status": "not_needed"}
    if not context["email"]:
        raise ValueError("No contact email for document reminder")

    names = "\n".join(f"- {row['document_name']}" for row in context["missing"])
    body = (
        f"Hello {context['first_name']},\n\n"
        "We are still waiting for these required registration documents:\n"
        f"{names}\n\n"
        "Please use the submission instructions provided by the registration team. "
        "Submitted documents are reviewed by staff before acceptance.\n\n"
        "CareFlow Demo Hospital (simulated)"
    )
    return send_via_n8n(
        request_id=request_id,
        patient_id=context["patient_id"],
        key=f"registration-{request_id}-documents-reminder-{attempt}",
        kind="missing_document_reminder",
        to_email=context["email"],
        subject="Reminder: registration documents are still needed",
        body=body,
        template="registration_documents_reminder_v1",
    )


def expire_document_request(request_id: int, follow_up_id: int) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            """update careflow.patient_requests
               set status = 'expired', updated_at = now()
               where id = %s and status = 'waiting_patient'
               returning status""",
            (request_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                "update careflow.follow_ups set status = 'cancelled' where id = %s",
                (follow_up_id,),
            )
            return False
        _event(
            conn, request_id, "waiting_patient", "expired",
            "Registration document deadline expired",
        )
        conn.execute(
            """update careflow.follow_ups set status = 'cancelled'
               where request_id = %s and kind = 'missing_document' and status = 'pending'""",
            (request_id,),
        )
    return True
