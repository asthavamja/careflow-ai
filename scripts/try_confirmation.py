import sys

from agents.notify import get_confirmation_facts, send_via_n8n, template_confirmation

appt_id = int(sys.argv[1])
f = get_confirmation_facts(appt_id)
subject, body = template_confirmation(f)
result = send_via_n8n(
    request_id=f["request_id"], patient_id=f["patient_id"],
    key=f"appt-{appt_id}-confirmation", kind="booking_confirmation",
    to_email=f["email"], subject=subject, body=body, template="booking_confirmation_v1",
)
print(result)