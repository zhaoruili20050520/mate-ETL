"""Celery tasks for the ETL-Agent.

This module is auto-discovered by Celery's ``autodiscover_tasks``.
The execution and supervision workers are imported here so their
``@celery_app.task`` decorated functions are registered when this
module is loaded.
"""

from __future__ import annotations

import logging
import uuid

from etl_agent.db.session import SessionLocal
from etl_agent.models.exploration_task import ExplorationStatus
from etl_agent.services.metadata_service import (
    explore_metadata,
    update_task_status,
)

# Import execution and supervision workers so their tasks are registered.
# autodiscover_tasks only loads this ``tasks.py`` module; without these
# imports the task functions in execution_worker.py and
# supervision_worker.py would never be decorated/registered.
#
# ``outbox_publisher`` must be imported too: it owns the
# ``etl_agent.publish_outbox`` beat task.  Without this import the beat
# schedule references an unregistered task, so committed preparations
# never reach the execution worker and runs stay in ``pending`` forever.
from etl_agent.workers import execution_worker as _execution_worker  # noqa: F401
from etl_agent.workers import outbox_publisher as _outbox_publisher  # noqa: F401
from etl_agent.workers import supervision_worker as _supervision_worker  # noqa: F401
from etl_agent.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="etl_agent.test_task")  # type: ignore[untyped-decorator]
def test_task(payload: str = "ping") -> str:
    """Simple echo task for verifying Redis broker is working."""
    return f"pong: {payload}"


@celery_app.task(name="etl_agent.explore_metadata_task")  # type: ignore[untyped-decorator]
def explore_metadata_task(
    task_id: str,
    connection_id: str,
    tenant_id: str,
    project_id: str,
    max_tables: int = 100,
    max_sample_rows: int = 50,
    max_execution_seconds: int = 120,
) -> dict[str, object]:
    """Async metadata exploration task.

    Updates the ExplorationTask record through its lifecycle:
    pending -> running -> completed/failed.
    """
    task_uuid = uuid.UUID(task_id)
    conn_uuid = uuid.UUID(connection_id)
    tenant_uuid = uuid.UUID(tenant_id)
    project_uuid = uuid.UUID(project_id)

    db = SessionLocal()
    try:
        # Mark as running
        update_task_status(db, task_uuid, ExplorationStatus.running)

        result = explore_metadata(
            db,
            connection_id=conn_uuid,
            tenant_id=tenant_uuid,
            project_id=project_uuid,
            max_tables=max_tables,
            max_sample_rows=max_sample_rows,
            max_execution_seconds=max_execution_seconds,
        )

        # Mark as completed
        update_task_status(
            db,
            task_uuid,
            ExplorationStatus.completed,
            table_count=result["table_count"],
            total_rows=result["total_rows"],
        )

        return {
            "task_id": task_id,
            "status": "completed",
            "table_count": result["table_count"],
            "total_rows": result["total_rows"],
        }

    except Exception as exc:
        error_msg = str(exc)[:4096]
        logger.error("Exploration task %s failed: %s", task_id, error_msg)
        # Use a fresh session — the original db may be in a
        # pending-rollback state and cannot be reused safely.
        db.rollback()
        error_db = SessionLocal()
        try:
            update_task_status(
                error_db,
                task_uuid,
                ExplorationStatus.failed,
                error_message=error_msg,
            )
        except Exception:  # noqa: BLE001
            logger.error("Failed to update task status to 'failed' for task %s", task_id)
        finally:
            error_db.close()
        return {
            "task_id": task_id,
            "status": "failed",
            "error": error_msg,
        }
    finally:
        db.close()
