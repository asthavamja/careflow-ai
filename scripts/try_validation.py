from agents.validation import validate_patient

CASES = [
    ("book_appointment", {"first_name": "Amit", "last_name": "Patel", "phone": "+91 90010 10001"}),
    ("new_registration", {"first_name": "Rahul", "last_name": "Sha", "phone": "98765 43210", "date_of_birth": "14/05/1990"}),
    ("book_appointment", {"first_name": "Priya", "last_name": "Mehta", "phone": "98200 11122"}),
    ("book_appointment", {"first_name": "Priya", "last_name": "Mehta", "phone": "98200 11122", "date_of_birth": "1988-02-11"}),
    ("book_appointment", {"first_name": "Priya", "last_name": "Mehta", "phone": "98200 11122", "date_of_birth": "1990-01-01"}),
    ("book_appointment", {"first_name": "Ravi", "last_name": "Kumar", "phone": "90000 00000"}),
    ("book_appointment", {"first_name": "Amit", "last_name": None}),
    ("cancel", {"appointment_code": "3FA9C21B"}),
]

for i, (rtype, details) in enumerate(CASES, 1):
    out = validate_patient(rtype, details)
    codes = [c["patient_code"] for c in out["candidates"]]
    print(f"{i}. {rtype:17} -> {out['match_result']:18} patient_id={out['patient_id']} candidates={codes} {out['notes']}")