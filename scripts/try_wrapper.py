from agents.communication import assemble, write_wrapper
from agents.notify import get_confirmation_facts

for i in range(3):
    w = write_wrapper("Dermatology")
    print(i + 1, "| fallback:", w["fallback_used"], "| attempts:", w["attempts"], "| error:", w["error"])
    print("   OPENING:", w["opening"])
    print("   CLOSING:", w["closing"])

f = get_confirmation_facts(2)
w = write_wrapper(f["department"])
subject, body = assemble(f, w["opening"], w["closing"])
print("\n" + body)