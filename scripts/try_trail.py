from agents.trail import log_step
from api.db import get_connection

log_step(1, "test_step", "tool", "success",
         input_summary="manual test", output={"ok": True}, latency_ms=5)

with get_connection() as conn:
    row = conn.execute(
        "select step_name, step_type, status, output, latency_ms "
        "from careflow.agent_executions order by id desc limit 1"
    ).fetchone()
print(row)