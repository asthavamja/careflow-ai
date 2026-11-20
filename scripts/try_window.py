from datetime import datetime

from agents.scheduling import IST, _ask_timing, _resolve_window

today = datetime.now(IST).date()
for t in ["any time next week", "next week", "this week", "next Monday",
          "tomorrow", "Tuesday morning", "sometime in March 2027"]:
    timing = _ask_timing(t)
    print(f"{t:24} -> {timing.kind:10} {timing.part_of_day:9} {_resolve_window(timing, today)}")