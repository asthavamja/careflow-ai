import sys

from agents.alerts import send_urgent_alert

print(send_urgent_alert(int(sys.argv[1])))