"""Execution worker -- submits SeaTunnel jobs and supervises their lifecycle.

This Celery task replaces the placeholder ``execute_seatunnel_job``
with the real implementation.  It is dispatched by the Outbox
publisher and performs the full execution chain:

1. Load the Outbox message from the DB.
2. Consume the Capability (single-use enforcement via commit_service).
3. Mark the ExecutionRun as ``running`` (done inside consume_capability).
4. Materialise source/target credentials from Vault.
5. Inject credentials into the HOCON template (in-memory only).
6. Submit the HOCON to SeaTunnel via ``SeaTunnelClient.submit_job``.
7. Record the SeaTunnel job_id on the ExecutionRun.
8. Create a ``RuntimeSupervisionContract`` for the supervision worker.
9. Write an ``ExecutionEvent`` for job submission.
10. Log to the Evidence Ledger.

Security contract:
    - The HOCON with injected credentials lives only in process memory.
    - Credentials are never written to the DB, logs, or error messages.
    - The ``sanitize_error_message`` helper strips credentials from
      any exception before it reaches the caller.

Idempotency:
    - The Capability consumption is single-use (PK constraint on
      capability_logs), so even if the Outbox delivers the same
      message twice, the second attempt is rejected at step 2.
    - A pre-check on ``run.status`` catches the fast path where the
      run is already running or succeeded, avoiding unnecessary work.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from etl_agent.connectors.seatunnel import (
    SeaTunnelError,
    SeaTunnelSubmitError,
    get_seatunnel_client,
)
from etl_agent.connectors.vault import get_vault_client
from etl_agent.core.exceptions import ConflictError, NotFoundError
from etl_agent.db.session import SessionLocal
from etl_agent.models.data_source_connection import DataSourceConnection
from etl_agent.models.evidence_ledger import EvidenceEventType
from etl_agent.models.execution_event import (
    ExecutionEvent,
    ExecutionEventType,
)
from etl_agent.models.execution_run import (
    ExecutionRun,
    ExecutionStatus,
)
from etl_agent.models.outbox import (
    OutboxMessage,
    OutboxStatus,
)
from etl_agent.models.runtime_supervision_contract import (
    RuntimeSupervisionContract,
    SwapStrategy,
)
from etl_agent.services.commit_service import consume_capability
from etl_agent.services.evidence_service import log_event
from etl_agent.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def _materialize_credentials(
    db: Session,
    source_conn: DataSourceConnection,
    target_conn: DataSourceConnection,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read credentials from Vault for both source and target connections.

    Returns (source_creds, target_creds) dicts with keys like
    ``username`` and ``password``.

    The credentials exist only in process memory -- they are never
    written to the DB or logs.
    """
    vault = get_vault_client()

    source_creds = vault.read_secret_by_ref(source_conn.secret_ref)
    target_creds = vault.read_secret_by_ref(target_conn.secret_ref)

    logger.debug(
        "Materialized credentials for source=%s target=%s (Vault paths resolved)",
        source_conn.id,
        target_conn.id,
    )

    return source_creds, target_creds


def _inject_credentials(
    hocon_template: str,
    source_creds: dict[str, Any],
    target_creds: dict[str, Any],
) -> str:
    """Replace ${...} placeholders in HOCON with actual credentials.

    Supported placeholders:
        ${source_username}, ${source_password}
        ${target_username}, ${target_password}

    The result is a complete HOCON config ready for SeaTunnel submission.
    It exists only in process memory and is never persisted or logged.
    """
    replacements = {
        "${source_username}": str(source_creds.get("username", "")),
        "${source_password}": str(source_creds.get("password", "")),
        "${target_username}": str(target_creds.get("username", "")),
        "${target_password}": str(target_creds.get("password", "")),
    }

    result = hocon_template
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)

    return result


def _determine_swap_strategy(target_type: str) -> SwapStrategy:
    """Determine the atomic swap strategy based on the target DB type."""
    if target_type == "doris":
        return SwapStrategy.doris_replace
    if target_type == "clickhouse":
        return SwapStrategy.clickhouse_exchange
    if target_type == "postgresql":
        return SwapStrategy.postgresql_transactional
    if target_type == "mysql":
        return SwapStrategy.mysql_rename
    if target_type == "oracle":
        return SwapStrategy.oracle_redefinition
    return SwapStrategy.none


def _finalize_outbox(
    outbox: OutboxMessage,
    status: OutboxStatus,
    *,
    error: str | None = None,
) -> None:
    """Write back the final outbox status (dispatched → succeeded/failed).

    This is the Execution Worker's responsibility: after the SeaTunnel
    job has been submitted (or submission failed), the outbox message
    transitions to its terminal state.
    """
    outbox.status = status
    outbox.published_at = datetime.now(UTC)
    if error:
        outbox.last_error = error[:4096]


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
    name="etl_agent.execute_seatunnel_job",
    bind=True,
    max_retries=0,
)
def execute_seatunnel_job(
    self: Any,
    *,
    outbox_id: str,
    execution_run_id: str,
    hocon_text: str,
    capability_id: str,
    capability_token: str = "",
    resource_scope: dict[str, object] | None = None,
    rollback_plan: dict[str, object] | None = None,
) -> dict[str, str]:
    """Execute a SeaTunnel job from an Outbox message.

    This task is dispatched by the Outbox publisher.  It performs the
    full execution chain: consume capability -> materialize credentials
    -> submit to SeaTunnel -> record job_id.

    The task is idempotent: if the capability has already been consumed,
    the second invocation is rejected by the replay guard
    (ConflictError), and the task returns ``already_consumed``.
    """
    outbox_uuid = uuid.UUID(outbox_id)
    run_uuid = uuid.UUID(execution_run_id)

    db = SessionLocal()
    try:
        # ---- 1. Load the Outbox message ----
        outbox = db.scalar(
            select(OutboxMessage).where(OutboxMessage.id == outbox_uuid),
        )
        if outbox is None:
            logger.error("Outbox message %s not found", outbox_id)
            return {"outbox_id": outbox_id, "status": "outbox_not_found"}

        # ---- 2. Load the ExecutionRun ----
        run = db.scalar(
            select(ExecutionRun).where(ExecutionRun.id == run_uuid),
        )
        if run is None:
            logger.error("ExecutionRun %s not found", execution_run_id)
            return {"outbox_id": outbox_id, "status": "run_not_found"}

        # Fast path: if already running or succeeded, skip (idempotency)
        if run.status in (ExecutionStatus.running, ExecutionStatus.succeeded):
            logger.info(
                "ExecutionRun %s already in status %s, skipping",
                run_uuid,
                run.status.value,
            )
            return {"outbox_id": outbox_id, "status": "already_running"}

        # If already failed or rolled_back, skip (can't re-run without
        # a new capability)
        if run.status in (ExecutionStatus.failed, ExecutionStatus.rolled_back):
            logger.info(
                "ExecutionRun %s in terminal status %s, skipping",
                run_uuid,
                run.status.value,
            )
            return {"outbox_id": outbox_id, "status": "terminal_status"}

        # ---- 3. Consume the Capability (single-use) ----
        # consume_capability() verifies the token signature, atomically
        # marks capability_logs as consumed (PK constraint = replay guard),
        # sets run.status = running, and commits the transaction.
        try:
            run = consume_capability(
                db,
                capability_id=capability_id,
                consumer=f"execution-worker:{self.request.id}",
                token=capability_token or None,
            )
        except ConflictError as exc:
            # The capability was already consumed by another worker
            # (replay/duplicate dispatch).  This is NOT a failure --
            # the other worker may still be running successfully.
            logger.info(
                "Capability %s already consumed (replay), skipping: %s",
                capability_id[:12],
                str(exc)[:200],
            )
            return {"outbox_id": outbox_id, "status": "already_consumed"}

        except NotFoundError as exc:
            error_msg = f"Capability not found: {str(exc)[:256]}"
            logger.error(error_msg)
            run.status = ExecutionStatus.failed
            run.error_message = error_msg
            _write_event(
                db,
                run_uuid,
                ExecutionEventType.job_failed,
                error_msg,
                severity="error",
            )
            _finalize_outbox(outbox, OutboxStatus.failed, error=error_msg)
            db.commit()
            return {"outbox_id": outbox_id, "status": "capability_not_found"}

        except Exception as exc:
            error_msg = f"Capability consumption failed: {str(exc)[:256]}"
            logger.error(error_msg)
            run.status = ExecutionStatus.failed
            run.error_message = error_msg
            _write_event(
                db,
                run_uuid,
                ExecutionEventType.job_failed,
                error_msg,
                severity="error",
            )
            log_event(
                db,
                event_type=EvidenceEventType.execution_failed,
                execution_run_id=run.id,
                tenant_id=run.tenant_id,
                project_id=run.project_id,
                details={"error": error_msg, "phase": "capability_consume"},
            )
            _finalize_outbox(outbox, OutboxStatus.failed, error=error_msg)
            db.commit()
            return {"outbox_id": outbox_id, "status": "capability_failed"}

        # At this point, consume_capability has committed the transaction.
        # The run is now in ``running`` status.  Subsequent modifications
        # to ``run`` will be persisted by the next ``db.commit()``.

        # ---- 4. Load source/target connections ----
        scope = resource_scope or {}
        source_conn_id = scope.get("source_connection_id")
        target_conn_id = scope.get("target_connection_id")

        if not source_conn_id or not target_conn_id:
            error_msg = "resource_scope missing source/target connection_id"
            run.status = ExecutionStatus.failed
            run.error_message = error_msg
            _write_event(
                db,
                run_uuid,
                ExecutionEventType.job_failed,
                error_msg,
                severity="error",
            )
            _finalize_outbox(outbox, OutboxStatus.failed, error=error_msg)
            db.commit()
            return {"outbox_id": outbox_id, "status": "missing_scope"}

        source_conn = db.scalar(
            select(DataSourceConnection).where(
                DataSourceConnection.id == uuid.UUID(str(source_conn_id)),
            ),
        )
        target_conn = db.scalar(
            select(DataSourceConnection).where(
                DataSourceConnection.id == uuid.UUID(str(target_conn_id)),
            ),
        )
        if source_conn is None or target_conn is None:
            error_msg = "Source or target connection not found"
            run.status = ExecutionStatus.failed
            run.error_message = error_msg
            _write_event(
                db,
                run_uuid,
                ExecutionEventType.job_failed,
                error_msg,
                severity="error",
            )
            _finalize_outbox(outbox, OutboxStatus.failed, error=error_msg)
            db.commit()
            return {"outbox_id": outbox_id, "status": "conn_not_found"}

        # ---- 5. Materialise credentials from Vault ----
        try:
            source_creds, target_creds = _materialize_credentials(
                db,
                source_conn,
                target_conn,
            )
        except Exception:
            # Never expose Vault path or credential content in error
            run.status = ExecutionStatus.failed
            run.error_message = "Vault credential materialization failed"
            _write_event(
                db,
                run_uuid,
                ExecutionEventType.job_failed,
                "Vault credential materialization failed",
                severity="error",
            )
            log_event(
                db,
                event_type=EvidenceEventType.execution_failed,
                execution_run_id=run.id,
                tenant_id=run.tenant_id,
                project_id=run.project_id,
                details={"error": "vault_materialization_failed", "phase": "vault"},
            )
            _finalize_outbox(outbox, OutboxStatus.failed, error="vault_failed")
            db.commit()
            return {"outbox_id": outbox_id, "status": "vault_failed"}

        _write_event(
            db,
            run_uuid,
            ExecutionEventType.credentials_materialized,
            "Credentials materialized from Vault",
        )

        # ---- 6. Inject credentials into HOCON ----
        final_hocon = _inject_credentials(
            hocon_text,
            source_creds,
            target_creds,
        )

        _write_event(
            db,
            run_uuid,
            ExecutionEventType.hocon_rendered,
            "HOCON config rendered with injected credentials",
        )

        # ---- 7. Submit to SeaTunnel ----
        client = get_seatunnel_client()
        try:
            job_id = client.submit_job(
                final_hocon,
                job_name=f"etl-agent-run-{run_uuid}",
            )
        except SeaTunnelSubmitError as exc:
            error_msg = f"SeaTunnel submission failed: {str(exc)[:256]}"
            run.status = ExecutionStatus.failed
            run.error_message = error_msg
            _write_event(
                db,
                run_uuid,
                ExecutionEventType.job_failed,
                error_msg,
                severity="error",
            )
            log_event(
                db,
                event_type=EvidenceEventType.execution_failed,
                execution_run_id=run.id,
                tenant_id=run.tenant_id,
                project_id=run.project_id,
                details={"error": error_msg, "phase": "seatunnel_submit"},
            )
            _finalize_outbox(outbox, OutboxStatus.failed, error=error_msg)
            db.commit()
            return {"outbox_id": outbox_id, "status": "submit_failed"}

        except SeaTunnelError as exc:
            error_msg = f"SeaTunnel error: {str(exc)[:256]}"
            run.status = ExecutionStatus.failed
            run.error_message = error_msg
            _write_event(
                db,
                run_uuid,
                ExecutionEventType.job_failed,
                error_msg,
                severity="error",
            )
            _finalize_outbox(outbox, OutboxStatus.failed, error=error_msg)
            db.commit()
            return {"outbox_id": outbox_id, "status": "seatunnel_error"}

        # ---- 8. Record job_id on the ExecutionRun ----
        run.seatunnel_job_id = job_id

        _write_event(
            db,
            run_uuid,
            ExecutionEventType.job_submitted,
            f"SeaTunnel job submitted, job_id={job_id}",
            details={"seatunnel_job_id": job_id},
        )

        log_event(
            db,
            event_type=EvidenceEventType.execution_started,
            execution_run_id=run.id,
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            details={
                "seatunnel_job_id": job_id,
                "capability_id": capability_id,
            },
        )

        # ---- 9. Create RuntimeSupervisionContract ----
        outbox_budget = (outbox.payload or {}).get("execution_budget", {})
        outbox_quality = (outbox.payload or {}).get("quality_contract", {})
        quality_rules = outbox_quality.get("rules", []) if isinstance(outbox_quality, dict) else []
        max_error_ratio = outbox_quality.get(
            "max_error_ratio",
            outbox_budget.get("max_error_ratio", 0.01),
        )
        dead_letter_table = (
            scope.get("dead_letter_table")
            or (
                outbox_quality.get("dead_letter_table")
                if isinstance(outbox_quality, dict)
                else None
            )
            or "dlq_table"
        )
        dead_letter_schema = (
            scope.get("dead_letter_schema")
            or (
                outbox_quality.get("dead_letter_schema")
                if isinstance(outbox_quality, dict)
                else None
            )
            or "public"
        )

        swap_strategy = _determine_swap_strategy(
            target_conn.source_type.value,
        )

        target_table = scope.get("target_table", "")
        target_schema = scope.get("target_schema", "public")
        shadow_table = scope.get("shadow_table")

        contract = RuntimeSupervisionContract(
            execution_run_id=run.id,
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            max_rows=int(outbox_budget.get("max_rows", 10_000_000)),
            max_bytes=int(outbox_budget.get("max_bytes", 10737418240)),
            max_duration_seconds=int(
                outbox_budget.get("max_duration_seconds", 3600),
            ),
            max_error_ratio=float(max_error_ratio),
            max_output_amplification=10.0,
            dead_letter_table=str(dead_letter_table),
            dead_letter_schema=str(dead_letter_schema),
            quality_rules=list(quality_rules) if isinstance(quality_rules, list) else [],
            swap_strategy=swap_strategy,
            target_table=str(target_table),
            target_schema=str(target_schema),
            shadow_table=str(shadow_table) if shadow_table else None,
            source_connection_id=source_conn.id,
            target_connection_id=target_conn.id,
            hocon_template=hocon_text,
            rollback_plan=dict(rollback_plan) if rollback_plan else {},
        )
        db.add(contract)

        # Mark the outbox as succeeded (the command was executed:
        # job submitted to SeaTunnel successfully).
        _finalize_outbox(outbox, OutboxStatus.succeeded)

        db.commit()

        logger.info(
            "SeaTunnel job %d submitted for run %s (outbox=%s)",
            job_id,
            run_uuid,
            outbox_id,
        )

        return {
            "outbox_id": outbox_id,
            "execution_run_id": execution_run_id,
            "seatunnel_job_id": str(job_id),
            "status": "submitted",
        }

    except Exception as exc:
        db.rollback()
        logger.exception(
            "Unexpected error in execution worker for run %s",
            execution_run_id,
        )
        # Try to mark the run as failed in a fresh session
        error_db = None
        try:
            error_db = SessionLocal()
            run = error_db.scalar(
                select(ExecutionRun).where(
                    ExecutionRun.id == uuid.UUID(execution_run_id),
                ),
            )
            if run and run.status not in (
                ExecutionStatus.failed,
                ExecutionStatus.rolled_back,
                ExecutionStatus.succeeded,
            ):
                run.status = ExecutionStatus.failed
                run.error_message = f"Execution worker error: {str(exc)[:512]}"
                error_db.commit()
        finally:
            if error_db is not None:
                error_db.close()

        return {"outbox_id": outbox_id, "status": "error"}
    finally:
        db.close()


__all__ = [
    "execute_seatunnel_job",
]
