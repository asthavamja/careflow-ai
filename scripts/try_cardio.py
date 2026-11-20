from agents.intake import run_intake

text = "I'm Amit Patel, I need a cardiologist, my phone is 9001010001."
for i in range(3):
    r = run_intake(text)["result"]
    print(i + 1, r.request_type, r.confidence, r.urgent_flag)