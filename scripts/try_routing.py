from agents.routing import run_routing

for text in ["skin specialist", "cardiologist", "skin doc", "heart doctor",
             "bone doctor", "stomach pain doctor", "banana", None]:
    r = run_routing(text)
    print(f"{str(text):22} -> {str(r['department_name']):17} "
          f"method={r['method']:5} conf={r['confidence']:.2f} "
          f"referral={r['requires_referral']} {r['notes']}")