import unittest
from unittest.mock import patch

from pydantic import ValidationError

from agents.communication import (
    check_wrapper,
    clarification_labels,
    fallback_clarification_question,
)
from agents.graph import ask_patient_node
from api.schemas import MoreInfoIn, RequestIn


class _Cursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params):
        return _Cursor(self.row)


class ClarificationTests(unittest.TestCase):
    def test_request_models_accept_contact_email_and_nonempty_reply(self):
        request = RequestIn(
            channel="api",
            raw_text="Hello!!",
            contact_email="patient@example.org",
        )
        self.assertEqual(request.contact_email, "patient@example.org")
        self.assertEqual(MoreInfoIn(text="My reply").text, "My reply")

    def test_request_models_reject_invalid_email_and_blank_reply(self):
        with self.assertRaises(ValidationError):
            RequestIn(
                channel="api",
                raw_text="Hello!!",
                contact_email="not-an-email",
            )
        with self.assertRaises(ValidationError):
            MoreInfoIn(text="   ")

    def test_clarification_fallback_is_field_limited_and_digit_free(self):
        labels = clarification_labels(["first_name", "phone"])
        question = fallback_clarification_question(labels)

        self.assertIn("first name", question)
        self.assertIn("phone number", question)
        self.assertIsNone(check_wrapper(question))
        self.assertEqual(
            clarification_labels([]),
            ["what you need help with"],
        )

    def test_initial_clarification_sends_one_question(self):
        row = {
            "contact_email": "patient@example.org",
            "patient_id": None,
            "extracted_data": {},
            "clarification_count": 0,
        }
        drafted = {
            "question": "Could you tell us what you need help with?",
            "fallback_used": False,
            "attempts": 1,
            "error": None,
            "latency_ms": 10,
            "input_tokens": 5,
            "output_tokens": 7,
        }
        state = {
            "request_id": 12,
            "request_type": "unclear",
            "missing_fields": [],
        }

        with (
            patch("agents.graph.get_connection", return_value=_Connection(row)),
            patch("agents.graph.write_clarification", return_value=drafted) as write,
            patch("agents.graph.send_via_n8n", return_value={"status": "sent"}) as send,
            patch("agents.graph.change_status") as change_status,
            patch("agents.graph.log_step") as log_step,
        ):
            result = ask_patient_node(state)

        self.assertEqual(result["next_status"], "waiting_patient")
        write.assert_called_once_with(["request_type"])
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["to_email"], "patient@example.org")
        self.assertEqual(send.call_args.kwargs["template"], "patient_clarification_v1")
        change_status.assert_called_once_with(
            12, "waiting_patient", "system", "More information needed",
        )
        log_step.assert_called_once()

    def test_second_unclear_turn_goes_to_staff_approval(self):
        row = {
            "contact_email": "patient@example.org",
            "patient_id": None,
            "extracted_data": {},
            "clarification_count": 1,
        }
        state = {"request_id": 13, "request_type": "unclear", "missing_fields": []}

        with (
            patch("agents.graph.get_connection", return_value=_Connection(row)),
            patch("agents.graph.create_approval", return_value=27) as create_approval,
            patch("agents.graph.change_status") as change_status,
            patch("agents.graph.log_step") as log_step,
            patch("agents.graph.send_via_n8n") as send,
        ):
            result = ask_patient_node(state)

        self.assertEqual(result["next_status"], "awaiting_approval")
        self.assertEqual(result["approval_id"], 27)
        create_approval.assert_called_once_with(13, "unclear_request")
        change_status.assert_called_once()
        log_step.assert_called_once()
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
