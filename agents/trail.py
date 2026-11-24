from psycopg.types.json import Jsonb

from api.db import get_connection


def log_step(request_id, step_name, step_type, status, *, model=None,
             prompt_version=None, input_summary=None, output=None,
             confidence=None, input_tokens=None, output_tokens=None,
             latency_ms=None, attempt=1, error_message=None):
    """One row in agent_executions."""
    with get_connection() as conn:
        conn.execute(
            """insert into careflow.agent_executions
                 (request_id, step_name, step_type, status, model, prompt_version,
                  input_summary, output, confidence, input_tokens, output_tokens,
                  latency_ms, attempt, error_message)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (request_id, step_name, step_type, status, model, prompt_version,
             input_summary, Jsonb(output) if output is not None else None,
             confidence, input_tokens, output_tokens, latency_ms, attempt,
             error_message),
        )


def change_status(request_id, to_status, actor, note=None, actor_user_id=None):
    """Move a request to a new status and record the change. Returns the old status."""
    with get_connection() as conn:
        row = conn.execute(
            "select status from careflow.patient_requests where id = %s for update",
            (request_id,),
        ).fetchone()
        if row is None:
            raise ValueError("request not found")
        conn.execute(
            "update careflow.patient_requests set status = %s, updated_at = now() where id = %s",
            (to_status, request_id),
        )
        conn.execute(
            """insert into careflow.request_events
                 (request_id, from_status, to_status, actor, actor_user_id, note)
               values (%s, %s, %s, %s, %s, %s)""",
            (request_id, row["status"], to_status, actor, actor_user_id, note),
        )
        return row["status"]


def mark_waitlist_request(request_id: int, department_id: int | None,
                          to_status: str, note: str):
    """Persist the no-slots department, clear stale offers, and record the transition."""
    with get_connection() as conn:
        row = conn.execute(
            "select status from careflow.patient_requests where id = %s for update",
            (request_id,),
        ).fetchone()
        if row is None:
            raise ValueError("request not found")
        conn.execute(
            """update careflow.patient_requests
               set department_id = %s, offered_slots = '[]'::jsonb,
                   status = %s, updated_at = now()
               where id = %s""",
            (department_id, to_status, request_id),
        )
        conn.execute(
            """insert into careflow.request_events
                 (request_id, from_status, to_status, actor, note)
               values (%s, %s, %s, 'system', %s)""",
            (request_id, row["status"], to_status, note),
        )
        return row["status"]


def save_intake_result(request_id, request_type, details, urgent_flag, urgent_reason):
    """Store what Intake found on the request itself."""
    with get_connection() as conn:
        conn.execute(
            """update careflow.patient_requests
               set request_type = %s, extracted_data = %s, urgent_flag = %s,
                   urgent_reason = %s, updated_at = now()
               where id = %s""",
            (request_type, Jsonb(details), urgent_flag, urgent_reason, request_id),
        )

def link_patient(request_id, patient_id):
    with get_connection() as conn:
        conn.execute(
            "update careflow.patient_requests set patient_id = %s, updated_at = now() where id = %s",
            (patient_id, request_id),
        )


def save_reschedule_context(request_id, appointment_id, patient_id, department_id):
    with get_connection() as conn:
        row = conn.execute(
            """update careflow.patient_requests
               set reschedule_appointment_id = %s, patient_id = %s,
                   department_id = %s, updated_at = now()
               where id = %s and status = 'received'
               returning id""",
            (appointment_id, patient_id, department_id, request_id),
        ).fetchone()
        if row is None:
            raise ValueError("reschedule request is no longer processing")


def create_approval(request_id, reason):
    """Open a pending staff review. Returns the approval id."""
    with get_connection() as conn:
        return conn.execute(
            """insert into careflow.approvals (request_id, reason_for_review)
               values (%s, %s) returning id""",
            (request_id, reason),
        ).fetchone()["id"]

def save_offers(request_id, offers):
    with get_connection() as conn:
        conn.execute(
            "update careflow.patient_requests set offered_slots = %s, updated_at = now() where id = %s",
            (Jsonb(offers), request_id),
        )

def schedule_slot_followups(request_id):
    with get_connection() as conn:
        s = {r["key"]: r["value"] for r in conn.execute(
            """select key, value from careflow.system_settings
               where key in ('patient_reminder_hours', 'patient_expire_hours')"""
        ).fetchall()}
        plan = (("patient_slot_reminder", float(s.get("patient_reminder_hours", 12))),
                ("patient_slot_expiry",   float(s.get("patient_expire_hours", 24))))
        for kind, hours in plan:
            conn.execute(
                """insert into careflow.follow_ups (request_id, kind, due_at)
                   select %s, %s, now() + %s * interval '1 hour'
                   where not exists (select 1 from careflow.follow_ups
                                     where request_id = %s and kind = %s and status = 'pending')""",
                (request_id, kind, hours, request_id, kind),
            )