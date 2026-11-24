from psycopg.types.json import Jsonb

from api.db import get_connection

DEFAULT_SETTINGS = {
    "slot_hold_hours": 24,
    "slot_release_cutoff_hours": 4,
    "patient_reminder_hours": 12,
    "patient_expire_hours": 24,
    "approval_reminder_minutes": 60,
    "approval_escalate_minutes": 120,
    "urgent_realert_minutes": 5,
    "urgent_escalate_minutes": 15,
    "automation_max_attempts": 4,
    "automation_retry_minutes": [1, 5, 15],
    "waitlist_hold_minutes": 120,
}

CLEAR_IN_ORDER = [
    "notifications",
    "automation_logs",
    "agent_executions",
    "request_events",
    "approvals",
    "patient_documents",
    "follow_ups",
    "appointments",
    "patient_requests",
    "checkpoint_writes",
    "checkpoint_blobs",
    "checkpoints",
]

NEW_SLOTS = """
insert into careflow.time_slots (doctor_id, start_time, end_time, status)
select s.doctor_id,
       gs at time zone 'Asia/Kolkata',
       (gs + make_interval(mins => s.slot_minutes)) at time zone 'Asia/Kolkata',
       'free'
from generate_series((current_date + 1)::timestamp,
                     (current_date + 28)::timestamp, interval '1 day') as day
join careflow.doctor_schedules s on s.weekday = extract(isodow from day)::int
cross join lateral generate_series(
       day + s.start_time,
       day + s.end_time - make_interval(mins => s.slot_minutes),
       make_interval(mins => s.slot_minutes)) as gs
on conflict (doctor_id, start_time) do nothing
"""


def main():
    answer = input(
        "This clears ALL requests, appointments and logs. "
        "Type RESET to continue: "
    )
    if answer.strip() != "RESET":
        print("Cancelled. Nothing was changed.")
        return

    with get_connection() as conn:
        for table in CLEAR_IN_ORDER:
            deleted = conn.execute(f"delete from careflow.{table}").rowcount
            print(f"cleared {table}: {deleted} rows")

        conn.execute(
            "update careflow.time_slots set status = 'free', held_until = null "
            "where status <> 'free'"
        )
        conn.execute(
            """update careflow.waitlist
               set status = 'waiting', offered_slot_id = null,
                   offered_at = null, offer_code = null"""
        )
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                """update careflow.system_settings
                   set value = %s, updated_at = now()
                   where key = %s""",
                (Jsonb(value), key),
            )
        added = conn.execute(NEW_SLOTS).rowcount
        print(f"new slots added: {added}")

    print("Done. Patients, doctors, departments, staff and rules were kept.")


if __name__ == "__main__":
    main()
