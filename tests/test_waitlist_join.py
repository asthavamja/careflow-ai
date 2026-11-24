import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from agents.waitlist import (
    WaitlistJoinError,
    expire_waiting_entries,
    join_waitlist_for_request,
    notify_no_slots_patient,
)
from agents.graph import after_no_slots, no_slots_node
from api.schemas import WaitlistJoinIn


class _Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, request=None, *, duplicate=False, notify_row=None):
        self.request = request
        self.duplicate = duplicate
        self.notify_row = notify_row
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params=None):
        self.statements.append((query, params))
        if "select r.status, r.patient_id" in query:
            return _Cursor(self.request)
        if "select r.contact_email, p.email as patient_email" in query:
            return _Cursor(self.notify_row)
        if "select 1 from careflow.waitlist" in query:
            return _Cursor({"exists": 1} if self.duplicate else None)
        if "insert into careflow.waitlist" in query:
            return _Cursor({"id": 91})
        if "select value from careflow.system_settings" in query:
            return _Cursor({"value": 30})
        if "with expired as" in query:
            return _Cursor({"expired": 4})
        return _Cursor()


def _request(email="patient@controlled-test.org", status="waiting_patient"):
    return {
        "status": status,
        "patient_id": 501,
        "department_id": 7,
        "offered_slots": [],
        "contact_email": None,
        "patient_email": email,
        "is_active": True,
        "merged_into_id": None,
        "department_name": "Dermatology",
    }


class WaitlistJoinTests(unittest.TestCase):
    def test_identified_patient_gets_no_slots_message_without_staff_approval(self):
        state = {"request_id": 44, "patient_id": 501, "department_id": 7}
        with (
            patch(
                "agents.graph.notify_no_slots_patient",
                return_value={"sent": True, "error": None},
            ) as notify,
            patch("agents.graph.mark_waitlist_request") as mark_status,
            patch("agents.graph.log_step") as log_step,
            patch("agents.graph.create_approval") as create_approval,
        ):
            result = no_slots_node(state)

        self.assertEqual(result["next_status"], "waiting_patient")
        self.assertEqual(after_no_slots(result), "end")
        notify.assert_called_once_with(44, 501, 7)
        self.assertEqual(mark_status.call_args.args[1:3], (7, "waiting_patient"))
        create_approval.assert_not_called()
        self.assertEqual(log_step.call_args.args[2:4], ("tool", "success"))

    def test_unidentified_patient_keeps_staff_review_fallback(self):
        state = {"request_id": 44, "patient_id": None, "department_id": 7}
        with (
            patch("agents.graph.mark_waitlist_request") as mark_status,
            patch("agents.graph.create_approval", return_value=88),
            patch("agents.graph.log_step"),
        ):
            result = no_slots_node(state)

        self.assertEqual(result["next_status"], "awaiting_approval")
        self.assertEqual(after_no_slots(result), "review")
        self.assertEqual(result["approval_id"], 88)
        self.assertEqual(mark_status.call_args.args[1:3], (7, "awaiting_approval"))

    def test_join_serializes_duplicate_check_and_records_preferred_window(self):
        connection = _Connection(_request())
        preferred_from = datetime(2026, 12, 1, 9, tzinfo=timezone.utc)
        preferred_to = datetime(2026, 12, 10, 17, tzinfo=timezone.utc)

        with (
            patch("agents.waitlist.get_connection", return_value=connection),
            patch(
                "agents.waitlist.send_via_n8n",
                return_value={"status": "sent", "error": None},
            ) as send,
        ):
            result = join_waitlist_for_request(44, preferred_from, preferred_to)

        self.assertEqual(result["status"], "joined")
        self.assertTrue(result["email_sent"])
        sql = [query for query, _ in connection.statements]
        self.assertLess(
            next(i for i, query in enumerate(sql) if "pg_advisory_xact_lock" in query),
            next(i for i, query in enumerate(sql) if "select 1 from careflow.waitlist" in query),
        )
        insert = next(
            params for query, params in connection.statements
            if "insert into careflow.waitlist" in query
        )
        self.assertEqual(insert, (501, 7, preferred_from, preferred_to))
        self.assertEqual(send.call_args.kwargs["template"], "waitlist_joined_v1")

    def test_duplicate_active_department_entry_returns_conflict_without_email(self):
        connection = _Connection(_request(), duplicate=True)

        with (
            patch("agents.waitlist.get_connection", return_value=connection),
            patch("agents.waitlist.send_via_n8n") as send,
        ):
            with self.assertRaises(WaitlistJoinError) as raised:
                join_waitlist_for_request(44)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("already waiting", raised.exception.detail)
        self.assertFalse(any(
            "insert into careflow.waitlist" in query
            for query, _ in connection.statements
        ))
        send.assert_not_called()

    def test_example_com_address_is_never_used_for_no_slots_notice(self):
        connection = _Connection(notify_row={
            "contact_email": None,
            "patient_email": "patient@example.com",
            "is_active": True,
            "merged_into_id": None,
            "department_name": "Dermatology",
        })

        with (
            patch("agents.waitlist.get_connection", return_value=connection),
            patch("agents.waitlist.send_via_n8n") as send,
        ):
            result = notify_no_slots_patient(44, 501, 7)

        self.assertFalse(result["sent"])
        send.assert_not_called()

    def test_waitlist_join_body_accepts_omitted_preferences_and_rejects_bad_window(self):
        self.assertEqual(WaitlistJoinIn().preferred_from, None)
        with self.assertRaises(ValueError):
            WaitlistJoinIn(
                preferred_from="2026-12-10T10:00:00+05:30",
                preferred_to="2026-12-01T10:00:00+05:30",
            )

    def test_expiry_job_uses_configured_day_limit(self):
        connection = _Connection()
        with patch("agents.waitlist.get_connection", return_value=connection):
            result = expire_waiting_entries()

        self.assertEqual(result, {"expired": 4, "days": 30})
        update = next(
            (query, params) for query, params in connection.statements
            if "with expired as" in query
        )
        self.assertIn("created_at <= now() - make_interval(days => %s)", update[0])
        self.assertEqual(update[1], (30,))


if __name__ == "__main__":
    unittest.main()
