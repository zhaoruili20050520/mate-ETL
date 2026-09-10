"""Supervision worker -- polls running SeaTunnel jobs and enforces budget limits.

The supervision worker runs as a periodic Celery beat task.  For each
``ExecutionRun`` in ``running`` status, it:

1. Queries SeaTunnel for the job's current status and metrics.
2. Writes an ``ExecutionMetric`` snapshot.
3. Compares the metrics against the ``RuntimeSupervisionContract``
   budget limits.
4. If any limit is exceeded, triggers a **hard interrupt**:
   - Stop the SeaTunnel job.
   - Mark the ExecutionRun as ``failed``.
   - Write an ``ExecutionEvent`` with severity ``critical``.
   - Log to the Evidence Ledger.
5. If the job has finished, checks quality and updates the run status.

The worker is designed to be safe to run in multiple instances --
each instance claims a batch of running jobs using
``FOR UPDATE SKIP LOCKED``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from etl_agent.connectors.seatunnel import (
    JobStatus,
    SeaTunnelClient,
    SeaTunnelError,
    SeaTunnelJobNotFoundError,
    get_seatunnel_client,
)
from etl_agent.db.session import SessionLocal
from etl_agent.models.evidence_ledger import EvidenceEventType
from etl_agent.models.execution_event import (
    ExecutionEvent,
    ExecutionEventType,
)
from etl_agent.models.execution_metric import ExecutionMetric
from etl_agent.models.execution_run import (
    ExecutionRun,
    ExecutionStatus,
)
from etl_agent.models.runtime_supervision_contract import (
    RuntimeSupervisionContract,
)
from etl_agent.services.evidence_service import log_event
from etl_agent.workers.celery_app import celery_app
from etl_agent.workers.publish_worker import publish_execution_run

logger = logging.getLogger(__name__)

# Batch size for supervision polling
_BATCH_SIZE = 20
# Poll interval for the beat task (seconds)
_POLL_INTERVAL = 10


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


def _check_budget(
    metrics_read_rows: int,
    metrics_written_rows: int,
    metrics_error_rows: int,
    metrics_total_bytes: int,
    metrics_duration: float,
    contract: RuntimeSupervisionContract,
) -> list[str]:
    """Check if any budget limit is exceeded.

    Returns a list of violation messages (empty if within budget).
    """
    violations: list[str] = []

    if metrics_written_rows > contract.max_rows:
        violations.append(
            f"Row limit exceeded: {metrics_written_rows} > {contract.max_rows}",
        )

    if metrics_total_bytes > contract.max_bytes:
        violations.append(
            f"Byte limit exceeded: {metrics_total_bytes} > {contract.max_bytes}",
        )

    if metrics_duration > contract.max_duration_seconds:
        violations.append(
            f"Duration limit exceeded: {metrics_duration:.1f}s > {contract.max_duration_seconds}s",
        )

    read_rows = metrics_read_rows or 1
    error_rate = metrics_error_rows / read_rows
    if error_rate > contract.max_error_ratio:
        violations.append(
            f"Error rate exceeded: {error_rate:.4f} > {contract.max_error_ratio:.4f}",
        )

    amplification = metrics_written_rows / read_rows if read_rows > 0 else 0.0
    if amplification > contract.max_output_amplification:
        violations.append(
            f"Output amplification exceeded: {amplification:.2f} > "
            f"{contract.max_output_amplification:.2f}",
        )

    return violations


def _trigger_hard_interrupt(
    db: Session,
    run: ExecutionRun,
    contract: RuntimeSupervisionContract,
    client: SeaTunnelClient,
    violations: list[str],
) -> None:
    """Trigger a hard interrupt: stop the job, mark failed, log events."""
    violation_summary = "; ".join(violations)[:512]

    logger.warning(
        "Hard interrupt triggered for run %s: %s",
        run.id,
        violation_summary,
    )

    # Stop the SeaTunnel job
    if run.seatunnel_job_id is not None:
        stopped = client.stop_job(run.seatunnel_job_id)
        _write_event(
            db,
            run.id,
            ExecutionEventType.hard_interrupt_triggered,
            f"Hard interrupt: stop_job returned {stopped}. Violations: {violation_summary}",
            severity="critical",
            details={
                "violations": violations,
                "seatunnel_job_id": run.seatunnel_job_id,
                "stopped": stopped,
            },
        )
    else:
        _write_event(
            db,
            run.id,
            ExecutionEventType.hard_interrupt_triggered,
            f"Hard interrupt: no seatunnel_job_id. Violations: {violation_summary}",
            severity="critical",
            details={"violations": violations},
        )

    # Mark the run as failed
    run.status = ExecutionStatus.failed
    run.error_message = f"Budget exceeded: {violation_summary}"
    run.finished_at = datetime.now(UTC)

    # Write a budget_exceeded event
    _write_event(
        db,
        run.id,
        ExecutionEventType.budget_exceeded,
        violation_summary,
        severity="critical",
        details={"violations": violations},
    )

    # Log to Evidence Ledger
    log_event(
        db,
        event_type=EvidenceEventType.execution_failed,
        execution_run_id=run.id,
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        details={
            "reason": "budget_exceeded",
            "violations": violations,
            "seatunnel_job_id": run.seatunnel_job_id,
        },
    )


def _handle_finished_job(
    db: Session,
    run: ExecutionRun,
    contract: RuntimeSupervisionContract,
    client: SeaTunnelClient,
    final_status: JobStatus,
    metrics: Any,
) -> bool:
    """Handle a finished job and report whether publish should be queued.

    Returns ``True`` when the SeaTunnel job completed successfully and
    the run should be handed to the publish worker.
    """
    now = datetime.now(UTC)
    run.finished_at = now

    if final_status == JobStatus.finished:
        # SeaTunnel job completed successfully.  Do NOT mark the run
        # as succeeded -- the publish pipeline (quality check +
        # shadow verification + atomic swap) must pass first.
        # Mark as quality_checking and delegate to publish_worker.
        run.status = ExecutionStatus.quality_checking

        _write_event(
            db,
            run.id,
            ExecutionEventType.job_finished,
            f"SeaTunnel job finished. read={metrics.read_rows}, "
            f"written={metrics.written_rows}, errors={metrics.error_rows}",
            severity="info",
            details={
                "read_rows": metrics.read_rows,
                "written_rows": metrics.written_rows,
                "error_rows": metrics.error_rows,
                "duration_seconds": metrics.duration_seconds,
            },
        )

        logger.info(
            "Run %s marked quality_checking, publish will be queued after commit",
            run.id,
        )
        return True

    elif final_status == JobStatus.failed:
        run.status = ExecutionStatus.failed
        error_msg = (
            client.get_job_error(
                run.seatunnel_job_id,  # type: ignore[arg-type]
            )
            or "SeaTunnel job failed (no error details available)"
        )
        run.error_message = error_msg[:1024]

        _write_event(
            db,
            run.id,
            ExecutionEventType.job_failed,
            f"SeaTunnel job failed: {error_msg[:256]}",
            severity="error",
            details={"error": error_msg[:1024]},
        )

        log_event(
            db,
            event_type=EvidenceEventType.execution_failed,
            execution_run_id=run.id,
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            details={
                "reason": "seatunnel_job_failed",
                "error": error_msg[:512],
            },
        )

    elif final_status == JobStatus.canceled:
        run.status = ExecutionStatus.failed
        run.error_message = "SeaTunnel job was canceled"

        _write_event(
            db,
            run.id,
            ExecutionEventType.job_cancelled,
            "SeaTunnel job was canceled",
            severity="warning",
        )

        log_event(
            db,
            event_type=EvidenceEventType.execution_failed,
            execution_run_id=run.id,
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            details={"reason": "seatunnel_job_canceled"},
        )

    return False


@celery_app.task(  # type: ignore[untyped-decorator]
    name="etl_agent.supervise_running_jobs",
)
def supervise_running_jobs() -> dict[str, int]:
    """Supervise all running ExecutionRuns.

    For each running run:
    1. Query SeaTunnel for job status and metrics.
    2. Write an ExecutionMetric snapshot.
    3. Check budget limits.
    4. If exceeded, trigger hard interrupt.
    5. If job finished, update run status.

    Returns a summary dict with counts.
    """
    db = SessionLocal()
    client = get_seatunnel_client()

    checked = 0
    interrupted = 0
    finished = 0
    failed = 0
    errors = 0
    publish_run_ids: list[str] = []

    try:
        # Claim a batch of running jobs
        stmt = (
            select(ExecutionRun)
            .where(ExecutionRun.status == ExecutionStatus.running)
            .limit(_BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
        runs = db.scalars(stmt).all()

        for run in runs:
            checked += 1
            try:
                if run.seatunnel_job_id is None:
                    logger.warning(
                        "Run %s is running but has no seatunnel_job_id",
                        run.id,
                    )
                    continue

                # Load the supervision contract
                contract = db.scalar(
                    select(RuntimeSupervisionContract).where(
                        RuntimeSupervisionContract.execution_run_id == run.id,
                    ),
                )
                if contract is None:
                    logger.warning(
                        "No supervision contract for run %s, skipping",
                        run.id,
                    )
                    continue

                # Query SeaTunnel for status
                try:
                    job_status = client.get_job_status(run.seatunnel_job_id)
                except SeaTunnelJobNotFoundError:
                    logger.warning(
                        "SeaTunnel job %d not found for run %s",
                        run.seatunnel_job_id,
                        run.id,
                    )
                    run.status = ExecutionStatus.failed
                    run.error_message = "SeaTunnel job not found"
                    run.finished_at = datetime.now(UTC)
                    failed += 1
                    continue

                # Query metrics
                try:
                    metrics = client.get_job_metrics(run.seatunnel_job_id)
                except SeaTunnelError as exc:
                    logger.warning(
                        "Failed to get metrics for job %d: %s",
                        run.seatunnel_job_id,
                        str(exc)[:200],
                    )
                    metrics = type(
                        "EmptyMetrics",
                        (),
                        {
                            "read_rows": 0,
                            "written_rows": 0,
                            "error_rows": 0,
                            "bytes_read": 0,
                            "bytes_written": 0,
                            "duration_seconds": 0.0,
                            "raw": {},
                            "output_amplification": 0.0,
                            "error_rejection_rate": 0.0,
                            "to_dict": lambda: {},
                        },
                    )()

                # Write an ExecutionMetric snapshot
                metric_row = ExecutionMetric(
                    execution_run_id=run.id,
                    read_rows=metrics.read_rows,
                    written_rows=metrics.written_rows,
                    error_rows=metrics.error_rows,
                    bytes_read=metrics.bytes_read,
                    bytes_written=metrics.bytes_written,
                    duration_seconds=metrics.duration_seconds,
                    output_amplification=metrics.output_amplification,
                    error_rejection_rate=metrics.error_rejection_rate,
                    seatunnel_metrics=metrics.raw or None,
                )
                db.add(metric_row)

                # Write a heartbeat event
                _write_event(
                    db,
                    run.id,
                    ExecutionEventType.supervision_heartbeat,
                    f"Heartbeat: status={job_status.value}, "
                    f"read={metrics.read_rows}, written={metrics.written_rows}",
                    severity="info",
                    details=metrics.to_dict() if hasattr(metrics, "to_dict") else {},
                )

                # Check budget limits
                violations = _check_budget(
                    metrics.read_rows,
                    metrics.written_rows,
                    metrics.error_rows,
                    int(metrics.bytes_read) + int(metrics.bytes_written),
                    metrics.duration_seconds,
                    contract,
                )

                if violations:
                    _trigger_hard_interrupt(
                        db,
                        run,
                        contract,
                        client,
                        violations,
                    )
                    interrupted += 1
                    continue

                # Check if job has finished
                if job_status in (JobStatus.finished, JobStatus.failed, JobStatus.canceled):
                    should_publish = _handle_finished_job(
                        db,
                        run,
                        contract,
                        client,
                        job_status,
                        metrics,
                    )
                    if should_publish:
                        finished += 1
                        publish_run_ids.append(str(run.id))
                    else:
                        failed += 1

            except SeaTunnelError as exc:
                logger.warning(
                    "SeaTunnel error during supervision of run %s: %s",
                    run.id,
                    str(exc)[:200],
                )
                errors += 1
            except Exception as exc:
                logger.exception(
                    "Unexpected error supervising run %s: %s",
                    run.id,
                    str(exc)[:200],
                )
                errors += 1

        db.commit()

        # Queue publish only after the quality_checking state and the
        # final metrics snapshot are visible to other sessions.
        for publish_run_id in publish_run_ids:
            try:
                publish_execution_run.delay(publish_run_id)
                logger.info(
                    "Triggered publish_worker for run %s (job finished)",
                    publish_run_id,
                )
            except Exception as exc:
                logger.error(
                    "Failed to trigger publish_worker for run %s: %s",
                    publish_run_id,
                    str(exc)[:200],
                )

    except Exception:
        db.rollback()
        logger.exception("Supervision cycle failed")
    finally:
        db.close()

    return {
        "checked": checked,
        "interrupted": interrupted,
        "finished": finished,
        "failed": failed,
        "errors": errors,
    }


__all__ = ["supervise_running_jobs"]
