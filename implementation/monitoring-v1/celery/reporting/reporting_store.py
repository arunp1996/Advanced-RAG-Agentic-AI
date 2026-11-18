"""Persistent storage for automated consolidated monitoring reports."""

import os
from datetime import datetime, timezone

from sqlalchemy import (
    MetaData, Table, Column, String, Integer, DateTime,
    Date, Boolean, JSON, Text, ForeignKey, UniqueConstraint,
    create_engine, select, update, insert
)

DATABASE_URL = os.environ["DATABASE_URL"]

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
)

metadata = MetaData()

reporting_cycles = Table(
    "monitoring_reporting_cycles",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("cycle_key", String(100), unique=True, nullable=False),
    Column("window_start", DateTime(timezone=True), nullable=False),
    Column("window_end", DateTime(timezone=True), nullable=False),
    Column("status", String(30), nullable=False, default="pending"),
    Column("expected_agents", JSON, nullable=False),
    Column("validation", JSON),
    Column("consolidated_result", JSON),
    Column("retry_count", Integer, nullable=False, default=0),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
)

cycle_agents = Table(
    "monitoring_reporting_cycle_agents",
    metadata,
    Column("id", String(36), primary_key=True),
    Column(
        "cycle_id",
        String(36),
        ForeignKey("monitoring_reporting_cycles.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("agent_type", String(40), nullable=False),
    Column("task_id", String(36)),
    Column("status", String(30), nullable=False, default="pending"),
    Column("result", JSON),
    Column("error_summary", Text),
    Column("attempts", Integer, nullable=False, default=0),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    UniqueConstraint("cycle_id", "agent_type", name="uq_cycle_agent"),
)

daily_reports = Table(
    "monitoring_daily_reports",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("report_date", Date, nullable=False, unique=True),
    Column("status", String(30), nullable=False, default="pending"),
    Column("dataset", JSON),
    Column("html_path", Text),
    Column("excel_path", Text),
    Column("json_path", Text),
    Column("validation", JSON),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("generated_at", DateTime(timezone=True)),
)

email_deliveries = Table(
    "monitoring_email_deliveries",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("report_date", Date, nullable=False, unique=True),
    Column("idempotency_key", String(120), nullable=False, unique=True),
    Column("recipient", String(320), nullable=False),
    Column("status", String(30), nullable=False, default="pending"),
    Column("attempts", Integer, nullable=False, default=0),
    Column("message_id", String(200)),
    Column("last_error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("sent_at", DateTime(timezone=True)),
)


AGENT_TYPES = (
    "api_monitor",
    "infrastructure_agent",
    "log_agent",
    "langfuse_agent",
)


def utc_now():
    return datetime.now(timezone.utc)


def initialize():
    """Create only the new reporting tables."""
    metadata.create_all(
        engine,
        tables=[
            reporting_cycles,
            cycle_agents,
            daily_reports,
            email_deliveries,
        ],
    )


def create_cycle(cycle_id, cycle_key, window_start, window_end):
    now = utc_now()

    with engine.begin() as conn:
        existing = conn.execute(
            select(reporting_cycles).where(
                reporting_cycles.c.cycle_key == cycle_key
            )
        ).mappings().first()

        if existing:
            return dict(existing), False

        conn.execute(
            insert(reporting_cycles).values(
                id=cycle_id,
                cycle_key=cycle_key,
                window_start=window_start,
                window_end=window_end,
                status="pending",
                expected_agents=list(AGENT_TYPES),
                created_at=now,
            )
        )

        for agent in AGENT_TYPES:
            import uuid

            conn.execute(
                insert(cycle_agents).values(
                    id=str(uuid.uuid4()),
                    cycle_id=cycle_id,
                    agent_type=agent,
                    status="pending",
                    attempts=0,
                )
            )

        row = conn.execute(
            select(reporting_cycles).where(
                reporting_cycles.c.id == cycle_id
            )
        ).mappings().one()

        return dict(row), True


def update_cycle(cycle_id, **values):
    with engine.begin() as conn:
        conn.execute(
            update(reporting_cycles)
            .where(reporting_cycles.c.id == cycle_id)
            .values(**values)
        )


def update_cycle_agent(cycle_id, agent_type, **values):
    with engine.begin() as conn:
        conn.execute(
            update(cycle_agents)
            .where(
                cycle_agents.c.cycle_id == cycle_id,
                cycle_agents.c.agent_type == agent_type,
            )
            .values(**values)
        )


def get_cycle(cycle_id):
    with engine.connect() as conn:
        cycle = conn.execute(
            select(reporting_cycles).where(
                reporting_cycles.c.id == cycle_id
            )
        ).mappings().first()

        if not cycle:
            return None

        agents = conn.execute(
            select(cycle_agents)
            .where(cycle_agents.c.cycle_id == cycle_id)
            .order_by(cycle_agents.c.agent_type)
        ).mappings().all()

        result = dict(cycle)
        result["agents"] = [dict(agent) for agent in agents]
        return result


def save_daily_report(report_id, report_date, dataset,
                      html_path, excel_path, json_path, validation,
                      status="generated"):
    now = utc_now()

    with engine.begin() as conn:
        existing = conn.execute(
            select(daily_reports).where(
                daily_reports.c.report_date == report_date
            )
        ).mappings().first()

        values = {
            "status": status,
            "dataset": dataset,
            "html_path": html_path,
            "excel_path": excel_path,
            "json_path": json_path,
            "validation": validation,
            "generated_at": now,
        }

        if existing:
            conn.execute(
                update(daily_reports)
                .where(daily_reports.c.report_date == report_date)
                .values(**values)
            )
            return existing["id"]

        conn.execute(
            insert(daily_reports).values(
                id=report_id,
                report_date=report_date,
                created_at=now,
                **values,
            )
        )
        return report_id


def get_daily_report(report_date):
    with engine.connect() as conn:
        row = conn.execute(
            select(daily_reports).where(
                daily_reports.c.report_date == report_date
            )
        ).mappings().first()
        return dict(row) if row else None


def list_daily_reports(limit=30):
    with engine.connect() as conn:
        rows = conn.execute(
            select(daily_reports)
            .order_by(daily_reports.c.report_date.desc())
            .limit(min(max(int(limit), 1), 100))
        ).mappings().all()
        return [dict(row) for row in rows]



def list_cycles_for_window(window_start, window_end):
    """Return reporting cycles whose start falls within the requested window."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(reporting_cycles)
            .where(
                reporting_cycles.c.window_start >= window_start,
                reporting_cycles.c.window_start < window_end,
            )
            .order_by(reporting_cycles.c.window_start.asc())
        ).mappings().all()

    return [
        get_cycle(row["id"])
        for row in rows
    ]


def close():
    engine.dispose()


# ---------------------------------------------------------------------------
# Atomic email delivery protection
# ---------------------------------------------------------------------------

def claim_email_delivery(report_date, recipient, max_attempts=3):
    """
    Atomically claim one daily email.

    Only pending/failed deliveries can be claimed.
    Sending, sent and uncertain deliveries cannot be claimed again.
    """
    import uuid
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    idempotency_key = f"daily-report-{report_date.isoformat()}"

    with engine.begin() as conn:
        conn.execute(
            pg_insert(email_deliveries)
            .values(
                id=str(uuid.uuid4()),
                report_date=report_date,
                idempotency_key=idempotency_key,
                recipient=recipient,
                status="pending",
                attempts=0,
                created_at=utc_now(),
            )
            .on_conflict_do_nothing(
                index_elements=[email_deliveries.c.report_date]
            )
        )

        claimed = conn.execute(
            update(email_deliveries)
            .where(
                email_deliveries.c.report_date == report_date,
                email_deliveries.c.status.in_(["pending", "failed"]),
                email_deliveries.c.attempts < max_attempts,
            )
            .values(
                status="sending",
                attempts=email_deliveries.c.attempts + 1,
                last_error=None,
            )
            .returning(email_deliveries)
        ).mappings().first()

        return dict(claimed) if claimed else None


def mark_email_delivery_sent(report_date, message_id=None):
    """Record confirmed provider acceptance."""
    with engine.begin() as conn:
        result = conn.execute(
            update(email_deliveries)
            .where(
                email_deliveries.c.report_date == report_date,
                email_deliveries.c.status == "sending",
            )
            .values(
                status="sent",
                message_id=message_id,
                sent_at=utc_now(),
                last_error=None,
            )
        )
        return result.rowcount == 1


def mark_email_delivery_failed(report_date, error):
    """Use only when the provider confirms the message was not accepted."""
    with engine.begin() as conn:
        result = conn.execute(
            update(email_deliveries)
            .where(
                email_deliveries.c.report_date == report_date,
                email_deliveries.c.status == "sending",
            )
            .values(
                status="failed",
                last_error=str(error)[:2000],
            )
        )
        return result.rowcount == 1


def mark_email_delivery_uncertain(report_date, error):
    """
    Record an ambiguous provider outcome.
    Uncertain deliveries must never be automatically resent.
    """
    with engine.begin() as conn:
        result = conn.execute(
            update(email_deliveries)
            .where(
                email_deliveries.c.report_date == report_date,
                email_deliveries.c.status == "sending",
            )
            .values(
                status="uncertain",
                last_error=str(error)[:2000],
            )
        )
        return result.rowcount == 1


def get_email_delivery(report_date):
    with engine.connect() as conn:
        row = conn.execute(
            select(email_deliveries).where(
                email_deliveries.c.report_date == report_date
            )
        ).mappings().first()
        return dict(row) if row else None
