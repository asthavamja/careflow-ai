from agents.scheduling import find_offers, hold_slot
from api.db import get_connection

with get_connection() as conn:
    dept = conn.execute(
        "select id from careflow.departments where name = 'Dermatology'"
    ).fetchone()["id"]

for text in [None, "next Tuesday morning", "tomorrow afternoon",
             "any time next week", "sometime in March 2027"]:
    r = find_offers(dept, text)
    print("\nWORDING:", text, "|", r["source"], "| widened:", r["widened"])
    for o in r["offers"]:
        print("   ", o["slot_id"], o["label"])

first = find_offers(dept, None)["offers"][0]["slot_id"]
print("\nHold the first slot:", hold_slot(first))
print("Hold the same slot again:", hold_slot(first))