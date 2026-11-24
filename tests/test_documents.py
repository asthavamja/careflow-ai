import unittest

from agents.documents import (
    _all_required_accepted,
    _request_email,
    _required_to_submit,
)


class DocumentRequirementTests(unittest.TestCase):
    def test_only_mandatory_unsubmitted_or_rejected_documents_need_patient_action(self):
        checklist = [
            {"document_name": "Photo ID", "is_mandatory": True, "status": None},
            {"document_name": "Insurance", "is_mandatory": False, "status": None},
            {"document_name": "Address proof", "is_mandatory": True, "status": "rejected"},
            {"document_name": "Birth certificate", "is_mandatory": True, "status": "submitted"},
            {"document_name": "Referral", "is_mandatory": True, "status": "accepted"},
        ]

        self.assertEqual(
            [row["document_name"] for row in _required_to_submit(checklist)],
            ["Photo ID", "Address proof"],
        )

    def test_required_documents_are_complete_only_after_staff_acceptance(self):
        self.assertTrue(_all_required_accepted([
            {"is_mandatory": True, "status": "accepted"},
            {"is_mandatory": False, "status": None},
        ]))
        self.assertFalse(_all_required_accepted([
            {"is_mandatory": True, "status": "submitted"},
        ]))
        self.assertFalse(_all_required_accepted([
            {"is_mandatory": False, "status": None},
        ]))

    def test_request_contact_email_is_used_before_extracted_email(self):
        req = {
            "patient_email": None,
            "request_contact_email": "contact@example.org",
        }

        self.assertEqual(_request_email(req, {}), "contact@example.org")
        self.assertEqual(
            _request_email(req, {"email": "extracted@example.org"}),
            "contact@example.org",
        )


if __name__ == "__main__":
    unittest.main()
