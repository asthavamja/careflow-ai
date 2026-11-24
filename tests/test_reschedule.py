import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agents.booking import BookingError, book_choice
from agents.confirm import send_confirmation
from agents.graph import (
    after_reschedule,
    after_validation,
    no_slots_node,
    reschedule_node,
)
from agents.intake_checks import compute_missing
from agents.scheduling import find_reschedule_offers


class _Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, *, request_type=None, request=None, old=None, slot_available=True):
        self.request_type = request_type
        self.request = request
        self.old = old
        self.slot_available = slot_available
        self.statements = []
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.committed = exc_type is None
        return False

    def execute(self, query, params=None):
        normalized = " ".join(query.lower().split())
        self.statements.append((normalized, params))
        if "select request_type from careflow.patient_requests" in normalized:
            return _Cursor({"request_type": self.request_type})
        if "select id, status, request_type, patient_id, offered_slots" in normalized:
            return _Cursor(self.request)
        if "select a.id, a.status, a.slot_id, a.patient_id" in normalized:
            return _Cursor(self.old)
        if "update careflow.time_slots" in normalized and "set status = 'booked'" in normalized:
            return _Cursor({"id": params[0]} if self.slot_available else None)
        if "insert into careflow.appointments" in normalized:
            return _Cursor({"id": 902, "appointment_code": "NEWCODE9"})
        if "update careflow.appointments" in normalized:
            return _Cursor({"slot_id": self.old["slot_id"]})
        if "update careflow.time_slots" in normalized and "set status = 'free'" in normalized:
            return _Cursor({"id": params[0]})
        if "update careflow.patient_requests" in normalized and "returning id" in normalized:
            return _Cursor({"id": params[0]})
        return _Cursor()


def _reschedule_request():
    return {
        "id": 55,
        "status": "waiting_patient",
        "request_type": "reschedule",
        "patient_id": 501,
        "offered_slots": [{"slot_id": 701, "label": "Fri 27 Nov, 09:00 AM"}],
        "reschedule_appointment_id": 801,
        "extracted_data": {"preferred_date_time": "next Friday morning"},
    }


def _old_appointment():
    return {
        "id": 801,
        "status": "booked",
        "slot_id": 700,
        "patient_id": 501,
        "doctor_id": 21,
        "department_id": 3,
    }


class RescheduleTests(unittest.TestCase):
    def test_reschedule_requires_names_and_code_but_not_preferred_time(self):
        details = {
            "first_name": "Sneha",
            "last_name": "Iyer",
            "appointment_code": "OLD12345",
        }
        self.assertEqual(compute_missing("reschedule", details), [])
        self.assertEqual(
            compute_missing("reschedule", {"appointment_code": "OLD12345"}),
            ["first_name", "last_name"],
        )

    def test_skipped_validation_routes_reschedule_to_its_node(self):
        self.assertEqual(
            after_validation({"match_result": "skipped", "request_type": "reschedule"}),
            "reschedule",
        )

    def test_reschedule_offers_include_same_doctor_and_exclude_old_slot(self):
        base = datetime(2026, 11, 27, 4, tzinfo=timezone.utc)
        first_rows = [
            {"id": 101, "start_time": base, "doctor_id": 22, "doctor_name": "Dr B"},
            {"id": 102, "start_time": base + timedelta(hours=1), "doctor_id": 23, "doctor_name": "Dr C"},
            {"id": 103, "start_time": base + timedelta(hours=2), "doctor_id": 23, "doctor_name": "Dr C"},
        ]
        same_doctor = [
            {"id": 104, "start_time": base + timedelta(hours=3), "doctor_id": 21, "doctor_name": "Dr A"},
        ]
        with patch("agents.scheduling._query", side_effect=[first_rows, same_doctor]) as query:
            result = find_reschedule_offers(3, None, 700, 21)

        self.assertEqual([offer["slot_id"] for offer in result["offers"]], [101, 102, 104])
        self.assertTrue(all(offer["slot_id"] != 700 for offer in result["offers"]))
        self.assertEqual(query.call_args_list[0].kwargs["exclude_slot_id"], 700)
        self.assertEqual(query.call_args_list[1].kwargs["doctor_id"], 21)
        self.assertLessEqual(len(result["offers"]), 3)

    def test_invalid_choice_does_not_update_slots_or_appointments(self):
        request = _Connection(request_type="reschedule")
        transaction = _Connection(request=_reschedule_request(), old=_old_appointment())
        with patch("agents.booking.get_connection", side_effect=[request, transaction]):
            with self.assertRaises(BookingError) as raised:
                book_choice(55, 4)

        self.assertEqual(raised.exception.code, "invalid_choice")
        self.assertFalse(any(
            "update careflow.time_slots" in query or "insert into careflow.appointments" in query
            for query, _ in transaction.statements
        ))

    def test_swap_commits_before_offering_old_slot_to_waitlist(self):
        request = _Connection(request_type="reschedule")
        transaction = _Connection(request=_reschedule_request(), old=_old_appointment())
        with (
            patch("agents.booking.get_connection", side_effect=[request, transaction]),
            patch(
                "agents.booking.offer_freed_slot",
                side_effect=lambda slot_id: self.assertTrue(transaction.committed),
            ) as offer_freed,
        ):
            result = book_choice(55, 1)

        self.assertEqual(result["status"], "rescheduled")
        self.assertEqual(result["appointment_id"], 902)
        self.assertEqual(result["appointment_code"], "NEWCODE9")
        offer_freed.assert_called_once_with(700)
        statements = [query for query, _ in transaction.statements]
        self.assertLess(
            next(i for i, query in enumerate(statements) if "set status = 'booked'" in query),
            next(i for i, query in enumerate(statements) if "insert into careflow.appointments" in query),
        )
        self.assertLess(
            next(i for i, query in enumerate(statements) if "insert into careflow.appointments" in query),
            next(i for i, query in enumerate(statements) if "set status = 'cancelled'" in query),
        )
        self.assertLess(
            next(i for i, query in enumerate(statements) if "set status = 'cancelled'" in query),
            next(i for i, query in enumerate(statements) if "set status = 'free'" in query),
        )

    def test_taken_slot_returns_fresh_offers_without_cancelling_original(self):
        request = _Connection(request_type="reschedule")
        transaction = _Connection(
            request=_reschedule_request(), old=_old_appointment(), slot_available=False,
        )
        fresh = [{"slot_id": 702, "label": "Fri 27 Nov, 10:00 AM"}]
        with (
            patch("agents.booking.get_connection", side_effect=[request, transaction]),
            patch("agents.booking.find_reschedule_offers", return_value={"offers": fresh}),
            patch("agents.booking.save_offers") as save,
        ):
            with self.assertRaises(BookingError) as raised:
                book_choice(55, 1)

        self.assertEqual(raised.exception.code, "slot_unavailable")
        self.assertEqual(raised.exception.new_offers, fresh)
        self.assertFalse(any(
            "insert into careflow.appointments" in query
            or "set status = 'cancelled'" in query
            or "set status = 'free'" in query
            for query, _ in transaction.statements
        ))
        save.assert_called_once_with(55, fresh)

    def test_verification_failure_creates_staff_approval_without_changing_appointment(self):
        with (
            patch("agents.graph.verify_cancel", return_value=(None, "code not found")),
            patch("agents.graph.create_approval", return_value=66) as approval,
            patch("agents.graph.change_status") as status,
            patch("agents.graph.log_step") as log_step,
        ):
            result = reschedule_node({"request_id": 55, "details": {}})

        self.assertEqual(result["reschedule_result"], "review")
        self.assertEqual(result["review_reason"], "failed_identity_check")
        approval.assert_called_once_with(55, "failed_identity_check")
        status.assert_called_once_with(
            55, "awaiting_approval", "system", "Rescheduling could not be verified",
        )
        self.assertEqual(log_step.call_args.args[1:4], (
            "reschedule_agent", "tool", "failed",
        ))

    def test_cutoff_failure_uses_staff_review(self):
        appointment = {
            "id": 801,
            "patient_id": 501,
            "start_time": datetime.now(timezone.utc) + timedelta(hours=2),
        }
        setting = _Connection()
        setting.execute = lambda query, params=None: _Cursor({"value": 4})
        with (
            patch("agents.graph.verify_cancel", return_value=(appointment, None)),
            patch("agents.graph.get_connection", return_value=setting),
            patch("agents.graph.create_approval", return_value=66) as approval,
            patch("agents.graph.change_status") as status,
            patch("agents.graph.save_reschedule_context") as save_context,
            patch("agents.graph.log_step"),
        ):
            result = reschedule_node({"request_id": 55, "details": {}})

        self.assertEqual(result["reschedule_result"], "review")
        approval.assert_called_once_with(55, "failed_identity_check")
        status.assert_called_once()
        save_context.assert_not_called()

    def test_verified_reschedule_saves_context_and_schedules_followups(self):
        appointment = {
            "id": 801,
            "patient_id": 501,
            "department_id": 3,
            "doctor_id": 21,
            "slot_id": 700,
            "start_time": datetime.now(timezone.utc) + timedelta(hours=5),
        }
        setting = _Connection()
        setting.execute = lambda query, params=None: _Cursor({"value": 4})
        offers = [{"slot_id": 701, "label": "Fri 27 Nov, 09:00 AM"}]
        with (
            patch("agents.graph.verify_cancel", return_value=(appointment, None)),
            patch("agents.graph.get_connection", return_value=setting),
            patch("agents.graph.save_reschedule_context") as save_context,
            patch("agents.graph.find_reschedule_offers", return_value={"offers": offers}) as find,
            patch("agents.graph.save_offers") as save_offers,
            patch("agents.graph.schedule_slot_followups") as followups,
            patch("agents.graph.change_status") as status,
            patch("agents.graph.log_step"),
        ):
            result = reschedule_node({
                "request_id": 55,
                "details": {"preferred_date_time": "next Friday morning"},
            })

        self.assertEqual(result["reschedule_result"], "offered")
        self.assertEqual(after_reschedule(result), "offered")
        save_context.assert_called_once_with(55, 801, 501, 3)
        find.assert_called_once_with(3, "next Friday morning", 700, 21)
        save_offers.assert_called_once_with(55, offers)
        followups.assert_called_once_with(55)
        status.assert_called_once_with(
            55,
            "waiting_patient",
            "system",
            "1 reschedule slots offered, waiting for the patient to choose",
        )

    def test_no_slots_branch_uses_reschedule_context_for_patient_notice(self):
        state = {
            "request_id": 55,
            "reschedule_context": {"patient_id": 501, "department_id": 3},
        }
        with (
            patch("agents.graph.notify_no_slots_patient", return_value={"sent": True}) as notify,
            patch("agents.graph.mark_waitlist_request") as mark_status,
            patch("agents.graph.log_step"),
        ):
            result = no_slots_node(state)

        self.assertEqual(result["next_status"], "waiting_patient")
        self.assertEqual(after_reschedule({"reschedule_result": "no_slots"}), "no_slots")
        notify.assert_called_once_with(55, 501, 3)
        self.assertEqual(mark_status.call_args.args[1:3], (3, "waiting_patient"))

    def test_reschedule_confirmation_uses_rescheduled_subject_and_opening(self):
        facts = {
            "request_id": 55,
            "request_type": "reschedule",
            "appointment_code": "NEWCODE9",
            "start_time": datetime.now(timezone.utc),
            "doctor_name": "Dr A",
            "department": "Dermatology",
            "requires_referral": False,
            "patient_id": 501,
            "first_name": "Sneha",
            "email": "patient@controlled-test.org",
            "when": "Friday, 27 November 2026 at 09:30 AM",
        }
        with (
            patch("agents.confirm.get_confirmation_facts", return_value=facts),
            patch(
                "agents.confirm.write_wrapper",
                return_value={
                    "opening": "Appointment booked.",
                    "closing": "See you soon.",
                    "attempts": 1,
                    "fallback_used": False,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "latency_ms": 0,
                    "error": None,
                },
            ),
            patch("agents.confirm.log_step"),
            patch("agents.confirm.send_via_n8n", return_value={"status": "sent"}) as send,
            patch("agents.confirm._status", return_value="executing"),
            patch("agents.confirm.change_status") as status,
        ):
            send_confirmation(902)

        self.assertEqual(send.call_args.kwargs["subject"], "Your appointment has been rescheduled")
        self.assertIn("Your appointment has been rescheduled.", send.call_args.kwargs["body"])
        status.assert_called_once_with(55, "confirmed", "n8n", "Confirmation email sent")


if __name__ == "__main__":
    unittest.main()
