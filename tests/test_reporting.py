import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agents.reporting import weekly_metrics


class _Result:
    def __init__(self, *, row=None, rows=None):
        self.row = row
        self.rows = rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self):
        self.end_at = datetime(2026, 11, 24, 8, tzinfo=timezone.utc)
        self.start_at = self.end_at - timedelta(days=7)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params=None):
        if "select now() -" in query:
            return _Result(row={"start_at": self.start_at, "end_at": self.end_at})
        if "group by request_type" in query:
            return _Result(rows=[{"request_type": "new_registration", "count": 2}])
        if "group by status order by status" in query:
            return _Result(rows=[{"status": "closed", "count": 1}])
        if "from careflow.appointments" in query:
            return _Result(row={"bookings_created": 3, "cancellations": 1})
        if "action = 'waitlist_offer'" in query:
            return _Result(row={"offers": 2})
        if "from careflow.waitlist w" in query:
            return _Result(row={"accepts": 1})
        if "from careflow.approvals" in query:
            return _Result(row={
                "opened": 4,
                "decided": 3,
                "average_decision_seconds": 60.5,
                "escalated": 1,
            })
        if "action = 'urgent_alert'" in query:
            return _Result(row={"alert_attempts": 2, "requests_alerted": 1})
        if "with escalations as" in query:
            return _Result(row={
                "acknowledged": 1,
                "average_acknowledge_seconds": 25,
            })
        if "attempt_count > 1" in query:
            return _Result(row={"logged": 6, "failed": 2, "retried": 1})
        if "with request_lifecycles as" in query:
            return _Result(row={
                "completed_requests": 5,
                "average_processing_seconds": 10.25,
            })
        if "from careflow.agent_executions" in query:
            return _Result(rows=[{
                "step_name": "intake_agent",
                "prompt_version": "intake_v3",
                "model": "test-model",
                "executions": 2,
                "calls": 3,
                "input_tokens": 40,
                "output_tokens": 20,
                "average_latency_ms": 100,
                "failures": 0,
                "retried_executions": 1,
            }])
        raise AssertionError(f"Unexpected reporting query: {query}")


class WeeklyMetricsTests(unittest.TestCase):
    def test_weekly_metrics_returns_aggregates_for_each_report_section(self):
        with patch("agents.reporting.get_connection", return_value=_Connection()):
            report = weekly_metrics()

        self.assertEqual(report["period"]["days"], 7)
        self.assertEqual(report["requests"]["by_type"], [
            {"request_type": "new_registration", "count": 2},
        ])
        self.assertEqual(report["appointments"], {
            "bookings_created": 3,
            "cancellations": 1,
        })
        self.assertEqual(report["waitlist"], {"offers": 2, "accepts": 1})
        self.assertEqual(report["approvals"]["average_decision_seconds"], 60.5)
        self.assertEqual(report["urgent_alerts"]["average_acknowledge_seconds"], 25)
        self.assertEqual(report["automations"]["failed"], 2)
        self.assertEqual(report["processing"]["average_seconds"], 10.25)
        self.assertEqual(report["llm"]["by_step_and_prompt"][0]["calls"], 3)


if __name__ == "__main__":
    unittest.main()
