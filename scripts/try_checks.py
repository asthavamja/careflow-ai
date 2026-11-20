from agents.intake_checks import compute_missing
from agents.urgent import find_urgent_phrases

for text in [
    "My father can't breathe",
    "He has CHEST   pain!!",
    "I need a skin specialist",
    "no chest-pain, just a checkup",
]:
    print(repr(text), "->", find_urgent_phrases(text))

print(compute_missing("book_appointment",
      {"first_name": "Amit", "last_name": "Patel",
       "named_department": "skin specialist", "preferred_date_time": "Tuesday"}))
print(compute_missing("cancel", {"appointment_code": "3FA9C21B"}))