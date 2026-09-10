"""Execution rollback service -- managed rollback after publish failure.

This module is called by the ``publish_worker`` when any phase of
the publish pipeline fails (quality check, shadow verification, or
atomic swap).  It performs a **managed rollback**:

1. Determine whether the formal table was already swapped.
2. If swapped: restore the formal table from the backup table.
3. If not swapped: nothing to restore (formal table untouched).
4. Clean up any residual shadow table.
5. Preserve the dead-letter (error) table for investigation.
6. Write ``ExecutionEvent(rollback_started / rollback_succeeded)``.
7. Write ``EvidenceLedger(rollback)``.
8. Update ``PublishAttempt.status = rolled_back``.
9. Update ``ExecutionRun.status = rolled_back``.

Security contract:
    - Target DB credentials are materialised from Vault and exist
      only in process memory.
    - No credentials appear in error messages or logs.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from etl_agent.connectors.base import ConnectionParams, DatabaseConnector
from etl_agent.connectors.factory import create_connector
from etl_agent.connectors.vault import get_vault_client
from etl_agent.models.data_source_connection import DataSourceConnection
from etl_agent.models.evidence_ledger import EvidenceEventType
from etl_agent.models.execution_event import ExecutionEvent, ExecutionEventType
from etl_agent.models.execution_run import ExecutionRun, ExecutionStatus
from etl_agent.models.publish_attempt import (
    PublishAttempt,
    PublishAttemptStatus,
)
from etl_agent.models.runtime_supervision_contract import (
    RuntimeSupervisionContract,
    SwapStrategy,
)
from etl_agent.services.evidence_service import log_event

logger = logging.getLogger(__name__)


# =====================================================================
# Identifier quoting (must match execution_publish_service)
# =====================================================================


def _quote_identifier(identifier: str, *, source_type: str = "postgresql") -> str:
    """Quote a SQL identifier safely for the target database type."""
    cleaned = identifier.strip('"`[]')
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", cleaned):
        raise ValueError(f"Invalid SQL identifier: {identifier!r}")
    if source_type in {"mysql", "doris"}:
        return f"`{cleaned}`"
    return f'"{cleaned}"'


def _quoter_for(source_type: str) -> Callable[[str], str]:
    """Return an identifier quoting function for a target database type."""

    def quote(identifier: str) -> str:
        return _quote_identifier(identifier, source_type=source_type)

    return quote


def _generate_backup_table_name(target_table: str) -> str:
    """Generate the same style of unique backup name as the publish step."""
    date_str = datetime.now(UTC).strftime("%Y%m%d")
    base = target_table[:32].rstrip("_")
    return f"{base}_backup_{date_str}_{uuid.uuid4().hex[:8]}"


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


def _build_target_connector(
    db: Session,
    contract: RuntimeSupervisionContract,
) -> DatabaseConnector:
    """Create a connector to the target database using Vault credentials."""
    target_conn = db.scalar(
        select(DataSourceConnection).where(
            DataSourceConnection.id == contract.target_connection_id,
        ),
    )
    if target_conn is None:
        raise RuntimeError("Target connection not found")

    vault = get_vault_client()
    creds = vault.read_secret_by_ref(target_conn.secret_ref)

    params = ConnectionParams(
        source_type=target_conn.source_type.value,
        host=target_conn.host,
        port=target_conn.port,
        database_name=target_conn.database_name,
        username=target_conn.db_username,
        password=str(creds.get("password", "")),
    )
    return create_connector(params)


# =====================================================================
# Rollback helpers
# =====================================================================


def _restore_postgresql(
    connector: DatabaseConnector,
    schema: str,
    target: str,
    backup: str,
    shadow: str,
) -> bool:
    """PostgreSQL rollback: rename target back to shadow, backup back to target."""
    schema_q = _quote_identifier(schema)
    # The swap did: target -> backup, shadow -> target.
    # To reverse: current target (was shadow) -> shadow_name,
    #             backup -> target.
    # But if swap failed mid-way, target may still be the original.
    # We detect this by checking if backup table exists.
    exists_rows = connector.execute_query(
        "SELECT to_regclass(%s) AS exists_val",
        (f"{schema}.{backup}",),
    )
    backup_exists = exists_rows and exists_rows[0].get("exists_val")

    if backup_exists:
        # Swap was completed: reverse it.
        sql1 = (
            f"ALTER TABLE {schema_q}.{_quote_identifier(target)} "
            f"RENAME TO {_quote_identifier(shadow)}"
        )
        sql2 = (
            f"ALTER TABLE {schema_q}.{_quote_identifier(backup)} "
            f"RENAME TO {_quote_identifier(target)}"
        )
        connector.execute_transaction([(sql1, None), (sql2, None)])
        return True
    else:
        # Swap didn't happen or was partial: just clean up shadow.
        logger.info(
            "PostgreSQL rollback: backup table %s not found, formal table likely untouched",
            backup,
        )
        return False


def _restore_mysql(
    connector: DatabaseConnector,
    schema: str,
    target: str,
    backup: str,
    shadow: str,
) -> bool:
    """MySQL rollback: RENAME target back to shadow, backup back to target."""
    # Check if backup exists
    exists_rows = connector.execute_query(
        "SELECT COUNT(*) AS cnt FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = %s",
        (schema, backup),
    )
    backup_exists = exists_rows and int(exists_rows[0].get("cnt", 0)) > 0

    if backup_exists:
        q = _quoter_for("mysql")
        sql = (
            f"RENAME TABLE {q(schema)}.{q(target)} "
            f"TO {q(schema)}.{q(shadow)}, "
            f"{q(schema)}.{q(backup)} "
            f"TO {q(schema)}.{q(target)}"
        )
        connector.execute_statement(sql)
        return True
    else:
        logger.info(
            "MySQL rollback: backup table %s not found, formal table likely untouched",
            backup,
        )
        return False


def _restore_doris(
    connector: DatabaseConnector,
    schema: str,
    target: str,
    shadow: str,
) -> bool:
    """Doris rollback: reverse a completed REPLACE swap."""
    q = _quoter_for("doris")
    sql = (
        f"ALTER TABLE {q(schema)}.{q(target)} "
        f"REPLACE WITH TABLE {q(shadow)} "
        f"PROPERTIES('swap' = 'true')"
    )
    connector.execute_statement(sql)
    return True


def _restore_clickhouse(
    connector: DatabaseConnector,
    schema: str,
    target: str,
    shadow: str,
) -> bool:
    """ClickHouse rollback: EXCHANGE back."""
    sql = (
        f"EXCHANGE TABLES {_quote_identifier(schema)}.{_quote_identifier(target)} "
        f"AND {_quote_identifier(schema)}.{_quote_identifier(shadow)}"
    )
    connector.execute_statement(sql)
    return True


def _restore_oracle(
    connector: DatabaseConnector,
    target: str,
    backup: str,
    shadow: str,
) -> bool:
    """Oracle rollback: reverse the rename."""
    sql1 = (
        f"ALTER TABLE {_quote_identifier(target, source_type='oracle')} "
        f"RENAME TO {_quote_identifier(shadow, source_type='oracle')}"
    )
    sql2 = (
        f"ALTER TABLE {_quote_identifier(backup, source_type='oracle')} "
        f"RENAME TO {_quote_identifier(target, source_type='oracle')}"
    )
    connector.execute_transaction([(sql1, None), (sql2, None)])
    return True


def _cleanup_shadow_table(
    connector: DatabaseConnector,
    schema: str,
    shadow: str,
    source_type: str,
) -> None:
    """Drop the shadow table if it still exists."""
    if source_type == "postgresql":
        exists_rows = connector.execute_query(
            "SELECT to_regclass(%s) AS exists_val",
            (f"{schema}.{shadow}",),
        )
        if exists_rows and exists_rows[0].get("exists_val"):
            connector.execute_statement(
                f"DROP TABLE IF EXISTS {_quote_identifier(schema)}.{_quote_identifier(shadow)}",
            )
    else:
        q = _quoter_for(source_type)
        connector.execute_statement(
            f"DROP TABLE IF EXISTS {q(schema)}.{q(shadow)}",
        )


# =====================================================================
# Top-level rollback orchestrator
# =====================================================================


def run_managed_rollback(
    db: Session,
    run: ExecutionRun,
    failure_reason: str = "",
    failure_details: dict[str, Any] | None = None,
    *,
    swap_attempted: bool = False,
) -> dict[str, Any]:
    """Execute a managed rollback for a failed publish attempt.

    This is called by ``publish_worker.py`` when any publish phase
    fails.  It:

    1. Loads the RuntimeSupervisionContract and PublishAttempt.
    2. Writes ``ExecutionEvent(rollback_started)``.
    3. Connects to the target DB (Vault credentials).
    4. Determines whether the formal table was swapped:
       - If swapped: restore from backup table.
       - If not: nothing to restore.
    5. Cleans up the shadow table.
    6. Preserves the error (dead-letter) table.
    7. Updates ``PublishAttempt.status = rolled_back``.
    8. Updates ``ExecutionRun.status = rolled_back``.
    9. Writes ``ExecutionEvent(rollback_succeeded)``.
    10. Writes ``EvidenceLedger(rollback)``.

    ``swap_attempted`` must be ``True`` when the atomic-swap phase
    actually started. It is ``False`` for quality-gate failures so
    symmetric strategies (Doris / ClickHouse) are never reversed
    when no swap has happened.

    Returns a dict with the rollback result.
    """
    run_id = run.id

    # ---- 1. Load contract and attempt ----
    contract = db.scalar(
        select(RuntimeSupervisionContract).where(
            RuntimeSupervisionContract.execution_run_id == run_id,
        ),
    )
    if contract is None:
        logger.error("Contract not found for run %s during rollback", run_id)
        run.status = ExecutionStatus.rolled_back
        run.finished_at = datetime.now(UTC)
        run.error_message = f"Rollback: contract not found. {failure_reason}"
        return {
            "rolled_back": True,
            "run_id": str(run_id),
            "restored": False,
            "reason": "contract_not_found",
        }

    attempt = db.scalar(
        select(PublishAttempt).where(
            PublishAttempt.execution_run_id == run_id,
        ),
    )

    # ---- 2. Write rollback_started event ----
    _write_event(
        db,
        run_id,
        ExecutionEventType.rollback_started,
        f"Managed rollback started: reason={failure_reason}",
        severity="warning",
        details={
            "failure_reason": failure_reason,
            "failure_details": failure_details or {},
            "swap_strategy": contract.swap_strategy.value,
        },
    )

    # ---- 3. Connect to target DB ----
    try:
        connector = _build_target_connector(db, contract)
    except Exception as exc:
        error_msg = f"Rollback: target DB connection failed: {str(exc)[:256]}"
        logger.error(error_msg)
        # Can't restore the DB, but still mark as rolled_back
        run.status = ExecutionStatus.rolled_back
        run.finished_at = datetime.now(UTC)
        run.error_message = f"{failure_reason}. {error_msg}"
        if attempt:
            attempt.status = PublishAttemptStatus.rolled_back
            attempt.finished_at = datetime.now(UTC)
            attempt.error_message = error_msg[:4096]
        _write_event(
            db,
            run_id,
            ExecutionEventType.rollback_failed,
            error_msg,
            severity="error",
        )
        log_event(
            db,
            event_type=EvidenceEventType.rollback,
            execution_run_id=run.id,
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            details={
                "success": False,
                "reason": "target_db_connection_failed",
                "original_failure": failure_reason,
            },
            summary="Rollback failed: could not connect to target DB",
        )
        return {
            "rolled_back": True,
            "run_id": str(run_id),
            "restored": False,
            "reason": "target_db_connection_failed",
            "error": error_msg,
        }

    # ---- 4. Determine swap state and restore ----
    target = contract.target_table
    shadow = contract.shadow_table or ""
    schema = contract.target_schema
    swap_strategy = contract.swap_strategy
    source_type = connector._params.source_type  # noqa: SLF001

    # Prefer the backup name persisted before the swap was attempted.
    backup_table = (
        attempt.backup_table
        if attempt is not None and attempt.backup_table
        else _generate_backup_table_name(target)
    )

    restored = False
    try:
        can_restore_by_name = swap_strategy in {
            SwapStrategy.postgresql_transactional,
            SwapStrategy.mysql_rename,
        }
        if can_restore_by_name or swap_attempted:
            if swap_strategy == SwapStrategy.postgresql_transactional:
                restored = _restore_postgresql(
                    connector,
                    schema,
                    target,
                    backup_table,
                    shadow,
                )
            elif swap_strategy == SwapStrategy.mysql_rename:
                restored = _restore_mysql(
                    connector,
                    schema,
                    target,
                    backup_table,
                    shadow,
                )
            elif swap_strategy == SwapStrategy.doris_replace:
                restored = _restore_doris(connector, schema, target, shadow)
            elif swap_strategy == SwapStrategy.clickhouse_exchange:
                restored = _restore_clickhouse(connector, schema, target, shadow)
            elif swap_strategy == SwapStrategy.oracle_redefinition:
                restored = _restore_oracle(
                    connector,
                    target,
                    backup_table,
                    shadow,
                )
            else:
                logger.warning(
                    "Rollback: swap strategy %s has no restore handler",
                    swap_strategy.value,
                )
        else:
            logger.info(
                "Rollback: swap was not attempted, skipping table restore (strategy=%s)",
                swap_strategy.value,
            )

        # ---- 5. Clean up shadow table ----
        if shadow:
            try:
                _cleanup_shadow_table(connector, schema, shadow, source_type)
            except RuntimeError:
                logger.warning(
                    "Rollback: shadow table cleanup failed for %s",
                    shadow,
                )

        # ---- 6. Error table is preserved (do nothing) ----
        logger.info(
            "Rollback: dead-letter table %s preserved for investigation",
            contract.dead_letter_table,
        )

    except Exception as exc:
        error_msg = f"Rollback restore failed: {str(exc)[:256]}"
        logger.exception("Rollback restore failed for run %s", run_id)
        _write_event(
            db,
            run_id,
            ExecutionEventType.rollback_failed,
            error_msg,
            severity="error",
            details={
                "swap_strategy": swap_strategy.value,
                "backup_table": backup_table,
                "error": error_msg,
            },
        )
        # Still mark as rolled_back -- the formal table state
        # may be inconsistent, but the run is terminated.
        run.status = ExecutionStatus.rolled_back
        run.finished_at = datetime.now(UTC)
        run.error_message = f"{failure_reason}. {error_msg}"
        if attempt:
            attempt.status = PublishAttemptStatus.rolled_back
            attempt.finished_at = datetime.now(UTC)
            attempt.error_message = error_msg[:4096]
        log_event(
            db,
            event_type=EvidenceEventType.rollback,
            execution_run_id=run.id,
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            details={
                "success": False,
                "reason": "restore_failed",
                "original_failure": failure_reason,
                "restore_error": error_msg,
                "backup_table": backup_table,
            },
            summary="Rollback partially failed: restore error",
        )
        return {
            "rolled_back": True,
            "run_id": str(run_id),
            "restored": False,
            "reason": "restore_failed",
            "error": error_msg,
        }
    finally:
        connector.close()

    # ---- 7. Update PublishAttempt ----
    if attempt:
        attempt.status = PublishAttemptStatus.rolled_back
        attempt.finished_at = datetime.now(UTC)
        if failure_reason:
            attempt.error_message = (f"{attempt.error_message or ''}; rollback: {failure_reason}")[
                :4096
            ]

    # ---- 8. Update ExecutionRun ----
    run.status = ExecutionStatus.rolled_back
    run.finished_at = datetime.now(UTC)
    run.error_message = failure_reason[:4096] if failure_reason else None

    # ---- 9. Write rollback_succeeded event ----
    _write_event(
        db,
        run_id,
        ExecutionEventType.rollback_succeeded,
        f"Managed rollback succeeded: restored={restored}, "
        f"backup={backup_table}, shadow_cleaned={'yes' if shadow else 'n/a'}",
        details={
            "restored": restored,
            "backup_table": backup_table,
            "shadow_table": shadow,
            "shadow_cleaned": bool(shadow),
            "dead_letter_preserved": True,
        },
    )

    # ---- 10. Write Evidence Ledger ----
    log_event(
        db,
        event_type=EvidenceEventType.rollback,
        execution_run_id=run.id,
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        details={
            "success": True,
            "restored": restored,
            "backup_table": backup_table,
            "shadow_table": shadow,
            "original_failure": failure_reason,
            "failure_details": failure_details or {},
        },
        summary=(
            "Rollback succeeded: formal table restored, shadow table cleaned"
            if restored
            else "Rollback succeeded: no restore was required, shadow table cleaned"
        ),
    )

    logger.info(
        "Rollback SUCCEEDED for run %s: restored=%s, backup=%s",
        run_id,
        restored,
        backup_table,
    )

    return {
        "rolled_back": True,
        "run_id": str(run_id),
        "restored": restored,
        "backup_table": backup_table,
        "shadow_cleaned": bool(shadow),
        "dead_letter_preserved": True,
    }


__all__ = [
    "run_managed_rollback",
]
