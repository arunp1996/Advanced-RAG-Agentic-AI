import os

from celery import Celery
from celery.schedules import crontab

redis_host = os.getenv("MONITORING_REDIS_HOST", "redis")
redis_port = os.getenv("MONITORING_REDIS_PORT", "6379")
redis_password = os.environ["MONITORING_REDIS_PASSWORD"]

redis_url = (
    f"redis://:{redis_password}@{redis_host}:{redis_port}"
)

app = Celery(
    "advanced_rag_monitoring",
    broker=f"{redis_url}/5",
    backend=f"{redis_url}/6",
    include=["tasks", "reporting.orchestrator", "reporting.daily_report"],
)

app.conf.update(
    timezone="Asia/Kolkata",
    enable_utc=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "reporting-orchestrator-every-15-minutes": {
            "task": "monitoring.reporting_orchestrator",
            "schedule": 900.0,
        },
        **(
            {
                "daily-report-generation-at-8-15pm-ist": {
                    "task": "monitoring.generate_daily_report",
                    "schedule": crontab(hour=20, minute=15),
                },
            }
            if os.getenv("ENABLE_DAILY_REPORT_SCHEDULE", "false").strip().lower() == "true"
            else {}
        ),
    },
)


# Create independent SQLAlchemy connection pools after Celery forks.
from celery.signals import worker_process_init

@worker_process_init.connect
def reset_database_pool_after_fork(**kwargs):
    import monitoring_store
    monitoring_store.engine.dispose(close=False)
