"""Celery application instance and configuration."""

from __future__ import annotations

from celery import Celery  # type: ignore[import-untyped]
from celery.schedules import schedule  # type: ignore[import-untyped]

from etl_agent.settings import settings

# In Celery 5.x, schedule(run_every=<float_seconds>) creates a
# periodic interval.  We use short intervals because outbox messages
# and supervision polling are latency-sensitive.

celery_app = Celery(
    "etl_agent",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,
    task_soft_time_limit=3000,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=100,
    # ---- Beat schedule for periodic tasks ----
    # Outbox publisher: poll pending outbox messages every 5 seconds.
    # Supervision worker: poll running SeaTunnel jobs every 10 seconds.
    beat_schedule={
        "publish-outbox": {
            "task": "etl_agent.publish_outbox",
            "schedule": schedule(run_every=5.0),
        },
        "supervise-running-jobs": {
            "task": "etl_agent.supervise_running_jobs",
            "schedule": schedule(run_every=10.0),
        },
    },
)

# Auto-discover tasks in workers module
celery_app.autodiscover_tasks(["etl_agent.workers"])
