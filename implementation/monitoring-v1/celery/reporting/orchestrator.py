"""Four-agent asynchronous reporting orchestrator."""

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from celery import chord
from sqlalchemy import insert, update

from celery_app import app
import monitoring_store as store
from monitoring_store import agent_tasks
from reporting import reporting_store as rs


IST = ZoneInfo("Asia/Kolkata")

AGENTS = {
    "api_monitor": "monitoring.api_monitor",
    "infrastructure_agent": "monitoring.infrastructure_agent",
    "log_agent": "monitoring.log_agent",
    "langfuse_agent": "monitoring.langfuse_agent",
}


def reporting_window(now=None):
    """Return the current completed 15-minute IST reporting window."""
    now = now or datetime.now(IST)
    end = now.replace(
        minute=(now.minute // 15) * 15,
        second=0,
        microsecond=0,
    )
    start = end - timedelta(minutes=15)
    return start, end


@app.task(
    bind=True,
    name="monitoring.reporting_orchestrator",
    acks_late=True,
)
def reporting_orchestrator(self):
    """Create one cycle and dispatch all four agents concurrently."""

    rs.initialize()

    start, end = reporting_window()
    cycle_key = "15m-" + end.strftime("%Y%m%dT%H%M%S%z")
    cycle_id = str(uuid.uuid4())

    cycle, created = rs.create_cycle(
        cycle_id,
        cycle_key,
        start.astimezone(timezone.utc),
        end.astimezone(timezone.utc),
    )

    if not created:
        return {
            "status": "duplicate_cycle_skipped",
            "cycle_id": cycle["id"],
            "cycle_key": cycle_key,
        }

    signatures = []
    now = store.utc_now()

    try:
        for agent_type, task_name in AGENTS.items():
            task_id = str(uuid.uuid4())

            # Create the existing AgentTask record before dispatch.
            with store.engine.begin() as conn:
                conn.execute(
                    insert(agent_tasks).values(
                        id=task_id,
                        agent_type=agent_type,
                        status="pending",
                        progress=0,
                        requested_by_user_id=None,
                        params={
                            "reporting_cycle_id": cycle_id,
                            "reporting_window_start": start.isoformat(),
                            "reporting_window_end": end.isoformat(),
                        },
                        result=None,
                        logs=[],
                        error_summary=None,
                        pause_requested=False,
                        cancel_requested=False,
                        created_at=now,
                        started_at=None,
                        finished_at=None,
                        updated_at=now,
                    )
                )

            rs.update_cycle_agent(
                cycle_id,
                agent_type,
                task_id=task_id,
                status="queued",
                attempts=1,
            )

            signatures.append(
                app.signature(
                    task_name,
                    args=[task_id],
                    task_id=task_id,
                )
            )

        rs.update_cycle(
            cycle_id,
            status="running",
        )

        # All four agents are submitted as one asynchronous chord.
        callback = finalize_reporting_cycle.s(cycle_id)
        callback.link_error(finalize_reporting_failure.s(cycle_id))

        job = chord(signatures)(callback)

        rs.update_cycle(
            cycle_id,
            consolidated_result={
                "group_task_id": job.id,
                "reporting_window_start": start.isoformat(),
                "reporting_window_end": end.isoformat(),
            },
        )

        return {
            "status": "dispatched",
            "cycle_id": cycle_id,
            "cycle_key": cycle_key,
            "group_task_id": job.id,
            "agents": list(AGENTS),
        }

    except Exception as exc:
        rs.update_cycle(
            cycle_id,
            status="dispatch_failed",
            validation={
                "dispatch_error": str(exc)[:2000],
            },
            finished_at=store.utc_now(),
        )
        raise


@app.task(
    bind=True,
    name="monitoring.finalize_reporting_cycle",
    acks_late=True,
)
def finalize_reporting_cycle(self, results, cycle_id):
    """Collect the four results and persist one cycle summary."""

    cycle = rs.get_cycle(cycle_id)
    if not cycle:
        raise RuntimeError("Reporting cycle does not exist.")

    collected = {}
    missing = []
    failed = []

    for agent in cycle["agents"]:
        agent_type = agent["agent_type"]
        task_id = agent.get("task_id")
        task = store.get_task(task_id) if task_id else None

        if not task:
            missing.append(agent_type)
            rs.update_cycle_agent(
                cycle_id,
                agent_type,
                status="missing",
                error_summary="Agent task record not found.",
            )
            continue

        result = task.get("result")
        status = task.get("status")

        if status != "completed" or not isinstance(result, dict):
            failed.append(agent_type)
            rs.update_cycle_agent(
                cycle_id,
                agent_type,
                status="failed",
                result=result,
                error_summary=task.get("error_summary") or
                    f"Agent finished with status: {status}",
                finished_at=task.get("finished_at") or store.utc_now(),
            )
            continue

        collected[agent_type] = result

        rs.update_cycle_agent(
            cycle_id,
            agent_type,
            status="completed",
            result=result,
            finished_at=task.get("finished_at") or store.utc_now(),
        )

    expected = set(AGENTS)
    actual = set(collected)
    missing = sorted(set(missing) | (expected - actual))

    expected_start = cycle["window_start"]
    expected_end = cycle["window_end"]

    if expected_start.tzinfo is None or expected_end.tzinfo is None:
        raise RuntimeError("Reporting cycle window must be timezone-aware.")

    cycle_mismatches = {}

    for agent in cycle["agents"]:
        agent_type = agent["agent_type"]
        task_id = agent.get("task_id")
        task = store.get_task(task_id) if task_id else None
        params = (task or {}).get("params") or {}
        problems = []

        if params.get("reporting_cycle_id") != cycle_id:
            problems.append("reporting_cycle_id mismatch")

        try:
            agent_start = datetime.fromisoformat(params["reporting_window_start"])
            agent_end = datetime.fromisoformat(params["reporting_window_end"])

            if agent_start.tzinfo is None or agent_end.tzinfo is None:
                problems.append("agent reporting window is not timezone-aware")
            else:
                if agent_start.astimezone(timezone.utc) != expected_start.astimezone(timezone.utc):
                    problems.append("reporting_window_start mismatch")
                if agent_end.astimezone(timezone.utc) != expected_end.astimezone(timezone.utc):
                    problems.append("reporting_window_end mismatch")

        except (KeyError, TypeError, ValueError):
            problems.append("reporting window metadata missing or invalid")

        if problems:
            cycle_mismatches[agent_type] = problems

    same_reporting_cycle = not cycle_mismatches

    validation = {
        "expected_agents": sorted(expected),
        "completed_agents": sorted(actual),
        "missing_agents": missing,
        "failed_agents": sorted(set(failed)),
        "all_agents_completed": (
            actual == expected
            and not failed
            and not missing
            and same_reporting_cycle
        ),
        "same_reporting_cycle": same_reporting_cycle,
        "cycle_mismatches": cycle_mismatches,
    }

    status = (
        "completed"
        if validation["all_agents_completed"]
        else "partial"
    )

    summary = {
        "cycle_id": cycle_id,
        "reporting_window_start": cycle["window_start"].isoformat(),
        "reporting_window_end": cycle["window_end"].isoformat(),
        "agent_results": collected,
        "validation": validation,
        "consolidated_at_utc": store.utc_now().isoformat(),
    }

    rs.update_cycle(
        cycle_id,
        status=status,
        validation=validation,
        consolidated_result=summary,
        finished_at=store.utc_now(),
    )

    return {
        "cycle_id": cycle_id,
        "status": status,
        "completed_agents": sorted(actual),
        "missing_agents": missing,
        "failed_agents": sorted(set(failed)),
    }


@app.task(
    bind=True,
    name="monitoring.finalize_reporting_failure",
)
def finalize_reporting_failure(self, request, exc, traceback, cycle_id):
    """Record a chord-level failure without losing the cycle history."""

    cycle = rs.get_cycle(cycle_id)
    if not cycle:
        return {"cycle_id": cycle_id, "status": "cycle_not_found"}

    failed = []
    collected = {}

    for agent in cycle["agents"]:
        task_id = agent.get("task_id")
        task = store.get_task(task_id) if task_id else None

        if task and task.get("status") == "completed":
            collected[agent["agent_type"]] = task.get("result")
            rs.update_cycle_agent(
                cycle_id,
                agent["agent_type"],
                status="completed",
                result=task.get("result"),
                finished_at=task.get("finished_at") or store.utc_now(),
            )
        else:
            failed.append(agent["agent_type"])
            rs.update_cycle_agent(
                cycle_id,
                agent["agent_type"],
                status=task.get("status", "failed") if task else "missing",
                result=task.get("result") if task else None,
                error_summary=(
                    task.get("error_summary") if task else str(exc)
                ) or str(exc)[:2000],
                finished_at=store.utc_now(),
            )

    validation = {
        "expected_agents": sorted(AGENTS),
        "completed_agents": sorted(collected),
        "failed_or_missing_agents": sorted(failed),
        "chord_error": str(exc)[:2000],
        "all_agents_completed": False,
    }

    rs.update_cycle(
        cycle_id,
        status="partial",
        validation=validation,
        consolidated_result={
            "cycle_id": cycle_id,
            "agent_results": collected,
            "validation": validation,
        },
        finished_at=store.utc_now(),
    )

    return {
        "cycle_id": cycle_id,
        "status": "partial",
        "failed_or_missing_agents": sorted(failed),
    }
