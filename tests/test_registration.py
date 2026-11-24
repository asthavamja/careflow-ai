import unittest
from datetime import date
from unittest.mock import patch

from agents.intake_checks import compute_missing
from agents.registration import (
    is_minor,
    prepare_patient_values,
    register_new_patient,
)


class _Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, request_row):
        self.request_row = request_row
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params):
        self.statements.append((query, params))
        if "from careflow.patient_requests where id = %s for update" in query:
            return _Cursor(self.request_row)
        if "insert into careflow.patients" in query:
            return _Cursor({"id": 501})
        return _Cursor()


def _request_row(*, date_of_birth="1990-01-02", status="validated"):
    return {
        "request_type": "new_registration",
        "status": status,
        "patient_id": None,
        "contact_email": "contact@example.org",
        "extracted_data": {
            "first_name": "A2",
            "last_name": "Test",
            "date_of_birth": date_of_birth,
            "phone": "+91 90000 11111",
            "email": None,
        },
    }


class RegistrationTests(unittest.TestCase):
    def test_age_boundary_requires_approval_only_under_eighteen(self):
        today = date(2026, 11, 24)
        self.assertTrue(is_minor(date(2008, 11, 25), today))
        self.assertFalse(is_minor(date(2008, 11, 24), today))

    def test_patient_values_use_contact_email_and_normalize_phone(self):
        values = prepare_patient_values(_request_row()["extracted_data"], "CONTACT@example.org")

        self.assertEqual(values["date_of_birth"], date(1990, 1, 2))
        self.assertEqual(values["phone_normalized"], "9000011111")
        self.assertEqual(values["email"], "contact@example.org")

    def test_registration_requests_require_an_email(self):
        missing = compute_missing("new_registration", {
            "first_name": "A2",
            "last_name": "Test",
            "date_of_birth": "1990-01-02",
            "phone": "9000011111",
        })

        self.assertEqual(missing, ["email"])

    def test_minor_is_not_created_before_staff_approval(self):
        connection = _Connection(_request_row(date_of_birth="2014-01-02"))

        with patch("agents.registration.get_connection", return_value=connection):
            result = register_new_patient(21)

        self.assertTrue(result["requires_approval"])
        self.assertFalse(any(
            "insert into careflow.patients" in query
            for query, _ in connection.statements
        ))

    def test_approved_minor_is_created_and_linked(self):
        connection = _Connection(
            _request_row(date_of_birth="2014-01-02", status="awaiting_approval")
        )

        with patch("agents.registration.get_connection", return_value=connection):
            result = register_new_patient(
                22, allow_minor=True, actor="staff", actor_user_id=1,
            )

        self.assertTrue(result["created"])
        self.assertEqual(result["patient_id"], 501)
        self.assertTrue(any(
            "update careflow.patient_requests" in query
            for query, _ in connection.statements
        ))


if __name__ == "__main__":
    unittest.main()
