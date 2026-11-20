from agents.communication import assemble, check_wrapper, fallback_wrapper
from agents.notify import get_confirmation_facts

f = get_confirmation_facts(2)
opening, closing = fallback_wrapper()
subject, body = assemble(f, opening, closing)
print(body)

for bad in ["See you at 10:30!", "Visit www.example.org", "Take your medication", ""]:
    print(repr(bad), "->", check_wrapper(bad))
print("ok text ->", check_wrapper("We are happy to confirm your visit."))