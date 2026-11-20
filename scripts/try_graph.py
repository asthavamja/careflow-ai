import sys

from agents.graph import run_request
from api.db import get_connection

for rid in map(int, sys.argv[1:]):
    run_request(rid)
    with get_connection() as conn:
        status = conn.execute(
            "select status from careflow.patient_requests where id = %s", (rid,)
        ).fetchone()["status"]
        steps = conn.execute(
            "select step_name, status from careflow.agent_executions "
            "where request_id = %s order by id", (rid,)
        ).fetchall()
    print(rid, "->", status, [(s["step_name"], s["status"]) for s in steps])