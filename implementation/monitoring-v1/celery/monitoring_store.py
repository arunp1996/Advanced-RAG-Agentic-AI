"""PostgreSQL task persistence for isolated Celery monitoring workers."""

import os
import time
from datetime import datetime, timezone

from sqlalchemy import MetaData, Table, create_engine, select, update


DATABASE_URL = os.environ["DATABASE_URL"]

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
)

metadata = MetaData()

agent_tasks = Table(
    "agent_tasks",
    metadata,
    autoload_with=engine,
)


def utc_now():
    return datetime.now(timezone.utc)


def get_task(task_id):
    with engine.connect() as conn:
        row = conn.execute(
            select(agent_tasks).where(agent_tasks.c.id == task_id)
        ).mappings().first()

        return dict(row) if row else None


def update_task(task_id, **values):
    values["updated_at"] = utc_now()

    with engine.begin() as conn:
        conn.execute(
            update(agent_tasks)
            .where(agent_tasks.c.id == task_id)
            .values(**values)
        )


def add_log(task_id, message, level="info"):
    task = get_task(task_id)

    if task is None:
        raise RuntimeError("Agent task does not exist.")

    logs = list(task.get("logs") or [])
    logs.append({
        "at": utc_now().isoformat(),
        "level": level,
        "message": str(message)[:1000],
    })

    update_task(task_id, logs=logs[-100:])


def checkpoint(task_id, interval=1):
    """Wait while paused; return False when cancelled or missing."""

    while True:
        task = get_task(task_id)

        if task is None:
            return False

        if task["cancel_requested"]:
            update_task(
                task_id,
                status="cancelled",
                finished_at=utc_now(),
                progress=min(task["progress"], 99),
            )
            add_log(task_id, "Task cancelled cooperatively.", "warning")
            return False

        if not task["pause_requested"]:
            if task["status"] == "paused":
                update_task(task_id, status="running")
                add_log(task_id, "Task resumed.")
            return True

        if task["status"] != "paused":
            update_task(task_id, status="paused")
            add_log(task_id, "Task paused.", "warning")

        time.sleep(interval)


def close():
    engine.dispose()
