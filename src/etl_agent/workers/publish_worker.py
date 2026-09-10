"""Publish worker -- orchestrates the full publish pipeline.

This Celery task is triggered by ``supervision_worker`` after a
SeaTunnel job finishes successfully.  It runs the complete
publish pipeline:

1. **Quality gate** (quality_checking):
   - Quality check (error rate + rule validation)
   - Shadow-table verification (row count, PK, NULL, schema)
2. **Atomic swap** (publishing):
   - Execute the swap strategy (MySQL/PG/Doris/CH/Oracle)
   - On success: mark succeeded, write events, evidence ledger
3. **Managed rollback** (on any failure):
   - Restore formal table from backup if swapped
   - Clean up shadow table
   - Preserve dead-letter table
   - Mark rolled_back

The worker is idempotent: if the run is already in a terminal
state (succeeded / failed / rolled_back), it does nothing.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from etl_agent.db.session import SessionLocal
from etl_agent.models.execution_event import (
    ExecutionEvent,
    ExecutionEventType,
)
from etl_agent.models.execution_run import (
    ExecutionRun,
    ExecutionStatus,
)
from etl_agent.models.runtime_supervision_contract import (
    RuntimeSupervisionContract,
)
from etl_agent.services.execution_publish_service import (
    run_publish_quality_gate,
    swap_and_publish,
)
from etl_agent.services.execution_rollback_service import run_managed_rollback
from etl_agent.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Terminal states that indicate the run is already done
_TERMINAL_STATES = frozenset(
    {
        ExecutionStatus.succeeded,
        ExecutionStatus.failed,
        ExecutionStatus.rolled_back,
    },
)


def _write_event(
    db: Session,
    run_id: uuid.UUID,
    event_type: ExecutionEventType,
    message: str,
    *,
    severity: str = "info",
    details: dict[str, Any] | None = None,
) -> None:
    """Write an ExecutionEvent row."""
    db.add(
        ExecutionEvent(
            execution_run_id=run_id,
            event_type=event_type,
            severity=severity,
            message=message,
            details=details,
        ),
    )


@celery_app.task(  # type: ignore[untyped-decorator]
    name="etl_agent.publish_execution_run",
    bind=True,
    max_retries=0,
)
def publish_execution_run(
    self: Any,  # noqa: PLR0911  # Celery task signature
    execution_run_id: str,
) -> dict[str, Any]:
    """Publish an ExecutionRun through the full pipeline.

    Called by ``supervision_worker`` when a SeaTunnel job finishes.

    Args:
        execution_run_id: UUID string of the ExecutionRun.

    Returns:
        Dict with the final status and details.
    """
    run_id = uuid.UUID(execution_run_id)
    db = SessionLocal()
    swap_result: dict[str, Any] | None = None

    try:
        # ---- Load the run with row-level lock ----
        run = db.scalar(
            select(ExecutionRun).where(ExecutionRun.id == run_id).with_for_update(skip_locked=True),
        )
        if run is None:
            logger.error("ExecutionRun %s not found for publish", run_id)
            return {
                "published": False,
                "run_id": execution_run_id,
                "reason": "run_not_found",
            }

        # ---- Idempotency: skip if already terminal ----
        if run.status in _TERMINAL_STATES:
            logger.info(
                "Run %s already in terminal state %s, skipping publish",
                run_id,
                run.status.value,
            )
            return {
                "published": run.status == ExecutionStatus.succeeded,
                "run_id": execution_run_id,
                "reason": f"already_{run.status.value}",
            }

        # ---- Phase 1: Quality gate ----
        _write_event(
            db,
            run_id,
            ExecutionEventType.supervision_heartbeat,
            "Publish worker started: entering quality gate",
            details={"phase": "publish_worker_start"},
        )
        db.flush()

        gate_result = run_publish_quality_gate(db, run)

        if not gate_result.get("passed"):
            # Quality gate failed -> rollback
            reason = gate_result.get("reason", "quality_gate_failed")
            error = gate_result.get("error", "")

            logger.warning(
                "Quality gate FAILED for run %s: reason=%s, error=%s",
                run_id,
                reason,
                str(error)[:200],
            )

            rollback_result = run_managed_rollback(
                db,
                run,
                failure_reason=f"Quality gate failed: {reason}",
                failure_details={
                    "phase": "quality_gate",
                    "reason": reason,
                    "error": error,
                },
            )
            db.commit()

            return {
                "published": False,
                "run_id": execution_run_id,
                "reason": reason,
                "rolled_back": True,
                "rollback": rollback_result,
            }

        # ---- Quality gate passed ----
        logger.info(
            "Quality gate PASSED for run %s: report=%s, shadow_rows=%s",
            run_id,
            gate_result.get("quality_report_id"),
            gate_result.get("shadow_rows"),
        )
        db.flush()

        # ---- Load contract for swap phase ----
        contract = db.scalar(
            select(RuntimeSupervisionContract).where(
                RuntimeSupervisionContract.execution_run_id == run_id,
            ),
        )
        if contract is None:
            # Should not happen -- quality gate already loaded it
            logger.error(
                "Contract not found for run %s after quality gate",
                run_id,
            )
            rollback_result = run_managed_rollback(
                db,
                run,
                failure_reason="Contract not found after quality gate",
                failure_details={"phase": "swap"},
            )
            db.commit()
            return {
                "published": False,
                "run_id": execution_run_id,
                "reason": "contract_not_found",
                "rolled_back": True,
                "rollback": rollback_result,
            }

        # ---- Phase 2: Atomic swap and publish ----
        swap_result = swap_and_publish(db, run, contract)

        if swap_result.get("published"):
            # ---- Success ----
            logger.info(
                "Publish SUCCEEDED for run %s: backup=%s, final_rows=%s",
                run_id,
                swap_result.get("backup_table"),
                swap_result.get("final_rows"),
            )
            db.commit()
            return {
                "published": True,
                "run_id": execution_run_id,
                "backup_table": swap_result.get("backup_table"),
                "final_rows": swap_result.get("final_rows"),
            }

        # ---- Swap failed -> rollback ----
        reason = swap_result.get("reason", "swap_failed")
        error = swap_result.get("error", "")

        logger.error(
            "Swap FAILED for run %s: reason=%s, error=%s",
            run_id,
            reason,
            str(error)[:200],
        )

        rollback_result = run_managed_rollback(
            db,
            run,
            failure_reason=f"Swap failed: {reason}",
            failure_details={
                "phase": "swap",
                "reason": reason,
                "error": error,
                "backup_table": swap_result.get("backup_table"),
            },
            swap_attempted=True,
        )
        db.commit()

        return {
            "published": False,
            "run_id": execution_run_id,
            "reason": reason,
            "rolled_back": True,
            "rollback": rollback_result,
        }

    except Exception as exc:
        db.rollback()
        logger.exception(
            "Unexpected error in publish worker for run %s: %s",
            run_id,
            str(exc)[:200],
        )

        # If the external swap succeeded but the final DB commit failed,
        # the backup/shadow state still needs to be restored.
        swap_was_published = bool(
            swap_result is not None and swap_result.get("published"),
        )

        # Attempt a last-resort rollback in a fresh transaction
        try:
            db2 = SessionLocal()
            run2 = db2.scalar(
                select(ExecutionRun)
                .where(ExecutionRun.id == run_id)
                .with_for_update(skip_locked=True),
            )
            if run2 is not None and run2.status not in _TERMINAL_STATES:
                run_managed_rollback(
                    db2,
                    run2,
                    failure_reason=f"Publish worker exception: {str(exc)[:256]}",
                    failure_details={"phase": "publish_worker", "exception": str(exc)[:512]},
                    swap_attempted=swap_was_published,
                )
                db2.commit()
            db2.close()
        except Exception:
            logger.exception(
                "Last-resort rollback also failed for run %s",
                run_id,
            )

        return {
            "published": False,
            "run_id": execution_run_id,
            "reason": "publish_worker_exception",
            "error": str(exc)[:512],
            "rolled_back": True,
        }

    finally:
        db.close()


__all__ = ["publish_execution_run"]
