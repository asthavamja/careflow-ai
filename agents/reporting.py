from api.db import get_connection


def _count(value) -> int:
    return int(value or 0)


def _seconds(value):
    return None if value is None else round(float(value), 2)


def _grouped_rows(conn, query, start_at, end_at) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(query, (start_at, end_at)).fetchall()
    ]


def weekly_metrics() -> dict:
    """Return SQL-only aggregate metrics for the last seven days."""
    with get_connection() as conn:
        period = conn.execute(
            """select now() - interval '7 days' as start_at, now() as end_at"""
        ).fetchone()
        start_at, end_at = period["start_at"], period["end_at"]

        requests_by_type = _grouped_rows(
            conn,
            """select request_type, count(*) as count
               from careflow.patient_requests
               where created_at >= %s and created_at < %s
               group by request_type order by request_type""",
            start_at, end_at,
        )
        requests_by_status = _grouped_rows(
            conn,
            """select status, count(*) as count
               from careflow.patient_requests
               where created_at >= %s and created_at < %s
               group by status order by status""",
            start_at, end_at,
        )
        appointments = conn.execute(
            """select
                   count(*) filter (
                       where created_at >= %s and created_at < %s
                   ) as bookings_created,
                   count(*) filter (
                       where status = 'cancelled'
                         and updated_at >= %s and updated_at < %s
                   ) as cancellations
               from careflow.appointments""",
            (start_at, end_at, start_at, end_at),
        ).fetchone()
        waitlist_offers = conn.execute(
            """select count(distinct idempotency_key) as offers
               from careflow.automation_logs
               where action = 'waitlist_offer'
                 and created_at >= %s and created_at < %s""",
            (start_at, end_at),
        ).fetchone()
        waitlist_accepts = conn.execute(
            """select count(distinct w.id) as accepts
               from careflow.waitlist w
               join careflow.appointments a
                 on a.patient_id = w.patient_id
                and a.slot_id = w.offered_slot_id
               where w.status = 'booked'
                 and a.created_at >= %s and a.created_at < %s""",
            (start_at, end_at),
        ).fetchone()
        approvals = conn.execute(
            """select
                   count(*) filter (
                       where created_at >= %s and created_at < %s
                   ) as opened,
                   count(*) filter (
                       where decided_at >= %s and decided_at < %s
                   ) as decided,
                   round(
                       (avg(extract(epoch from (decided_at - created_at)))
                           filter (where decided_at >= %s and decided_at < %s))::numeric,
                       2
                   ) as average_decision_seconds,
                   count(*) filter (
                       where escalated_at >= %s and escalated_at < %s
                   ) as escalated
               from careflow.approvals""",
            (
                start_at, end_at, start_at, end_at,
                start_at, end_at, start_at, end_at,
            ),
        ).fetchone()
        urgent_alerts = conn.execute(
            """select count(*) as alert_attempts,
                      count(distinct request_id) as requests_alerted
               from careflow.automation_logs
               where action = 'urgent_alert'
                 and created_at >= %s and created_at < %s""",
            (start_at, end_at),
        ).fetchone()
        urgent_acknowledgements = conn.execute(
            """with escalations as (
                   select request_id, min(created_at) as escalated_at
                   from careflow.request_events
                   where to_status = 'urgent_escalated'
                   group by request_id
               ),
               acknowledgements as (
                   select request_id, min(created_at) as acknowledged_at
                   from careflow.request_events
                   where note = 'Urgent request acknowledged by clinical staff'
                   group by request_id
               )
               select count(*) as acknowledged,
                      round(
                          avg(extract(epoch from
                              (a.acknowledged_at - e.escalated_at)))::numeric,
                          2
                      ) as average_acknowledge_seconds
               from escalations e
               join acknowledgements a using (request_id)
               where a.acknowledged_at >= %s and a.acknowledged_at < %s""",
            (start_at, end_at),
        ).fetchone()
        automations = conn.execute(
            """select
                   count(*) filter (
                       where created_at >= %s and created_at < %s
                   ) as logged,
                   count(*) filter (
                       where status = 'failed'
                         and updated_at >= %s and updated_at < %s
                   ) as failed,
                   count(*) filter (
                       where attempt_count > 1
                         and updated_at >= %s and updated_at < %s
                   ) as retried
               from careflow.automation_logs""",
            (start_at, end_at, start_at, end_at, start_at, end_at),
        ).fetchone()
        processing = conn.execute(
            """with request_lifecycles as (
                   select e.request_id,
                          min(e.created_at) as started_at,
                          max(e.created_at) filter (
                              where e.to_status in (
                                  'closed', 'confirmed', 'rejected', 'failed', 'expired'
                              )
                          ) as completed_at
                   from careflow.request_events e
                   join careflow.patient_requests r on r.id = e.request_id
                   where r.status in (
                       'closed', 'confirmed', 'rejected', 'failed', 'expired'
                   )
                   group by e.request_id
               )
               select count(*) filter (
                          where completed_at >= %s and completed_at < %s
                      ) as completed_requests,
                      round(
                          (avg(extract(epoch from (completed_at - started_at)))
                              filter (where completed_at >= %s and completed_at < %s))::numeric,
                          2
                      ) as average_processing_seconds
               from request_lifecycles""",
            (start_at, end_at, start_at, end_at),
        ).fetchone()
        llm_steps = _grouped_rows(
            conn,
            """select step_name, prompt_version, model,
                      count(*) as executions,
                      coalesce(sum(attempt), 0) as calls,
                      coalesce(sum(input_tokens), 0) as input_tokens,
                      coalesce(sum(output_tokens), 0) as output_tokens,
                      round(avg(latency_ms))::integer as average_latency_ms,
                      count(*) filter (where status = 'failed') as failures,
                      count(*) filter (
                          where attempt > 1 or status = 'retried'
                      ) as retried_executions
               from careflow.agent_executions
               where step_type = 'llm'
                 and created_at >= %s and created_at < %s
               group by step_name, prompt_version, model
               order by step_name, prompt_version, model""",
            start_at, end_at,
        )

    return {
        "period": {
            "start": start_at.isoformat(),
            "end": end_at.isoformat(),
            "days": 7,
        },
        "requests": {
            "by_type": [
                {"request_type": row["request_type"], "count": _count(row["count"])}
                for row in requests_by_type
            ],
            "by_status": [
                {"status": row["status"], "count": _count(row["count"])}
                for row in requests_by_status
            ],
        },
        "appointments": {
            "bookings_created": _count(appointments["bookings_created"]),
            "cancellations": _count(appointments["cancellations"]),
        },
        "waitlist": {
            "offers": _count(waitlist_offers["offers"]),
            "accepts": _count(waitlist_accepts["accepts"]),
        },
        "approvals": {
            "opened": _count(approvals["opened"]),
            "decided": _count(approvals["decided"]),
            "average_decision_seconds": _seconds(
                approvals["average_decision_seconds"]
            ),
            "escalated": _count(approvals["escalated"]),
        },
        "urgent_alerts": {
            "attempts": _count(urgent_alerts["alert_attempts"]),
            "requests_alerted": _count(urgent_alerts["requests_alerted"]),
            "acknowledged": _count(urgent_acknowledgements["acknowledged"]),
            "average_acknowledge_seconds": _seconds(
                urgent_acknowledgements["average_acknowledge_seconds"]
            ),
        },
        "automations": {
            "logged": _count(automations["logged"]),
            "failed": _count(automations["failed"]),
            "retried": _count(automations["retried"]),
        },
        "processing": {
            "completed_requests": _count(processing["completed_requests"]),
            "average_seconds": _seconds(processing["average_processing_seconds"]),
        },
        "llm": {
            "by_step_and_prompt": [
                {
                    **row,
                    "executions": _count(row["executions"]),
                    "calls": _count(row["calls"]),
                    "input_tokens": _count(row["input_tokens"]),
                    "output_tokens": _count(row["output_tokens"]),
                    "average_latency_ms": (
                        None if row["average_latency_ms"] is None
                        else int(row["average_latency_ms"])
                    ),
                    "failures": _count(row["failures"]),
                    "retried_executions": _count(row["retried_executions"]),
                }
                for row in llm_steps
            ],
        },
    }
