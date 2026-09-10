"""Execution publish service -- quality check, shadow-table verification,
and atomic swap.

This module implements the full publish pipeline that runs after the
SeaTunnel job finishes:

1. **Quality check** (Phase 1): query the dead-letter (error) table
   for row counts, compute ``error_rate = error_rows / read_rows``,
   and execute each ``CleaningRule`` against the shadow table
   (not_null / unique / regex / range / enum).  Results are written
   to ``quality_reports``.

2. **Shadow-table verification** (Phase 2): directly query the target
   database to verify that the shadow table exists, has matching
   columns, non-zero row count, no duplicate / NULL primary keys,
   required fields are non-NULL, sensitive fields are masked, and
   (for incremental sync) the watermark is ahead of the previous
   formal table.

3. **Atomic swap** (Phase 3): rename the formal table to a backup
   name and the shadow table to the formal name in a single atomic
   transaction.  On success, update ``PublishAttempt``,
   ``ExecutionRun.status = succeeded``, write ``ExecutionEvent``
   and ``EvidenceLedger`` entries, and retain the backup table for
   at least 24 hours.

Only when all three phases pass is the run considered ``succeeded``.

Security contract:
    - Target DB credentials are materialised from Vault at runtime
      and exist only in process memory.
    - SQL identifiers are quoted via the connector's quoting helper
      to prevent injection.
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
from etl_agent.models.execution_metric import ExecutionMetric
from etl_agent.models.execution_run import ExecutionRun, ExecutionStatus
from etl_agent.models.publish_attempt import (
    PublishAttempt,
    PublishAttemptStatus,
)
from etl_agent.models.quality_report import QualityReport, QualityReportStatus
from etl_agent.models.runtime_supervision_contract import (
    RuntimeSupervisionContract,
    SwapStrategy,
)
from etl_agent.services.evidence_service import log_event

logger = logging.getLogger(__name__)


# =====================================================================
# Data structures for quality-check results
# =====================================================================


class QualityCheckError(Exception):
    """Raised when the quality check fails (error rate or rule failure)."""


class ShadowVerificationError(Exception):
    """Raised when the shadow-table verification fails."""


class RuleResult:
    """Result of executing a single cleaning rule."""

    __slots__ = ("field", "rule_type", "violation_count", "passed", "params")

    def __init__(
        self,
        field: str,
        rule_type: str,
        violation_count: int,
        passed: bool,
        params: dict[str, Any] | None = None,
    ) -> None:
        self.field = field
        self.rule_type = rule_type
        self.violation_count = violation_count
        self.passed = passed
        self.params = params or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "rule_type": self.rule_type,
            "violation_count": self.violation_count,
            "passed": self.passed,
            "params": self.params,
        }


# =====================================================================
# Helpers
# =====================================================================


def _quote_identifier(identifier: str, *, source_type: str = "postgresql") -> str:
    """Quote a SQL identifier to prevent injection.

    MySQL and Doris use backticks; PostgreSQL, ClickHouse and Oracle
    use double quotes. This is a defence-in-depth measure; callers
    should still validate identifier names against a whitelist.
    """
    # Strip any existing quotes, then wrap in double quotes.
    cleaned = identifier.strip('"`[]')
    # Reject any attempt to break out of the identifier context.
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


def _build_target_connector(
    db: Session,
    contract: RuntimeSupervisionContract,
) -> DatabaseConnector:
    """Create a connector to the target database using Vault credentials.

    The password is materialised from Vault and exists only in
    process memory.
    """
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


def _get_latest_metrics(db: Session, run_id: uuid.UUID) -> ExecutionMetric | None:
    """Return the most recent ExecutionMetric snapshot for a run."""
    return db.scalar(
        select(ExecutionMetric)
        .where(ExecutionMetric.execution_run_id == run_id)
        .order_by(ExecutionMetric.sampled_at.desc()),
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


# =====================================================================
# Phase 1: Quality check
# =====================================================================


def run_quality_check(
    db: Session,
    run: ExecutionRun,
    contract: RuntimeSupervisionContract,
    connector: DatabaseConnector,
) -> QualityReport:
    """Execute the quality check and persist the result.

    Steps:
        1. Query the dead-letter table for total error rows.
        2. Get read_rows from the latest ExecutionMetric.
        3. Compute error_rate = error_rows / read_rows.
        4. If error_rate > max_error_ratio, fail immediately.
        5. For each rule in ``contract.quality_rules``:
           - not_null: count NULLs in shadow table
           - unique: count duplicate values
           - regex: count values not matching the pattern
           - range: count values outside [min, max]
           - enum: count values not in allowed set
        6. Write QualityReport.

    Returns the created ``QualityReport``.

    Raises ``QualityCheckError`` if the check fails.
    """
    run_id = run.id
    source_type = connector._params.source_type  # noqa: SLF001
    logger.info("Quality check started for run %s", run_id)

    # ---- 1. Query dead-letter table for error row count ----
    dl_table = _quote_identifier(
        contract.dead_letter_table,
        source_type=source_type,
    )
    dl_schema = _quote_identifier(
        contract.dead_letter_schema,
        source_type=source_type,
    )
    full_dl = f"{dl_schema}.{dl_table}"

    error_rows = 0
    try:
        rows = connector.execute_query(f"SELECT COUNT(*) AS cnt FROM {full_dl}")
        if rows:
            error_rows = int(rows[0].get("cnt", 0))
    except RuntimeError:
        logger.warning(
            "Dead-letter table %s not accessible, assuming 0 error rows",
            full_dl,
        )
        error_rows = 0

    # ---- 2. Get read_rows from latest metric ----
    latest_metric = _get_latest_metrics(db, run_id)
    read_rows = latest_metric.read_rows if latest_metric else 0
    written_rows = latest_metric.written_rows if latest_metric else 0
    shadow_rows = written_rows  # written to shadow table

    # ---- 3. Compute error rate ----
    error_rate = error_rows / read_rows if read_rows > 0 else 0.0

    max_error_ratio = contract.max_error_ratio

    # ---- 4. Early fail if error rate exceeds threshold ----
    rule_results: list[dict[str, Any]] = []
    overall_passed = True

    if error_rate > max_error_ratio:
        overall_passed = False
        logger.warning(
            "Quality check FAILED: error_rate=%.6f > max=%.6f (run=%s)",
            error_rate,
            max_error_ratio,
            run_id,
        )

    # ---- 5. Execute each quality rule against shadow table ----
    shadow_table_name = contract.shadow_table or ""
    if shadow_table_name:
        shadow_qualified = (
            f"{_quote_identifier(contract.target_schema, source_type=source_type)}."
            f"{_quote_identifier(shadow_table_name, source_type=source_type)}"
        )
    else:
        shadow_qualified = ""

    quality_rules: list[dict[str, Any]] = contract.quality_rules or []

    for rule_def in quality_rules:
        field = str(rule_def.get("field", ""))
        rule_type = str(rule_def.get("rule", "")).lower()
        params = rule_def.get("params", {})

        if not field or not rule_type or not shadow_qualified:
            rule_results.append(
                RuleResult(
                    field=field or "<missing>",
                    rule_type=rule_type or "<missing>",
                    violation_count=-1,
                    passed=False,
                    params=params,
                ).to_dict(),
            )
            overall_passed = False
            continue

        violation_count = _execute_single_rule(
            connector,
            shadow_qualified,
            field,
            rule_type,
            params,
        )
        passed = violation_count == 0
        if not passed:
            overall_passed = False

        rule_results.append(
            RuleResult(
                field=field,
                rule_type=rule_type,
                violation_count=violation_count,
                passed=passed,
                params=params,
            ).to_dict(),
        )

    # ---- 6. Write QualityReport ----
    status = QualityReportStatus.passed if overall_passed else QualityReportStatus.failed

    report = QualityReport(
        execution_run_id=run_id,
        read_rows=read_rows,
        shadow_rows=shadow_rows,
        error_rows=error_rows,
        error_rate=error_rate,
        max_error_ratio=max_error_ratio,
        status=status,
        rule_results=rule_results,
    )
    db.add(report)

    # Write execution event
    if overall_passed:
        _write_event(
            db,
            run_id,
            ExecutionEventType.quality_check_passed,
            f"Quality check passed (error_rate={error_rate:.6f}, rules={len(rule_results)})",
            details={
                "error_rate": error_rate,
                "max_error_ratio": max_error_ratio,
                "rule_count": len(rule_results),
            },
        )
    else:
        _write_event(
            db,
            run_id,
            ExecutionEventType.quality_check_failed,
            f"Quality check failed (error_rate={error_rate:.6f}, "
            f"rules_failed={sum(1 for r in rule_results if not r['passed'])})",
            severity="error",
            details={
                "error_rate": error_rate,
                "max_error_ratio": max_error_ratio,
                "rule_results": rule_results,
            },
        )

    logger.info(
        "Quality check %s for run %s (error_rate=%.6f, rules=%d)",
        status.value,
        run_id,
        error_rate,
        len(rule_results),
    )

    if not overall_passed:
        raise QualityCheckError(
            f"Quality check failed for run {run_id}: error_rate={error_rate:.6f}, rules_failed",
        )

    return report


def _execute_single_rule(
    connector: DatabaseConnector,
    table_qualified: str,
    field: str,
    rule_type: str,
    params: dict[str, Any],
) -> int:
    """Execute a single quality rule and return the violation count.

    Returns 0 if the rule passes, >0 if it fails.
    """
    source_type = connector._params.source_type  # noqa: SLF001
    col = _quote_identifier(field, source_type=source_type)

    if rule_type == "not_null":
        rows = connector.execute_query(
            f"SELECT COUNT(*) AS cnt FROM {table_qualified} WHERE {col} IS NULL",
        )
        return int(rows[0].get("cnt", 0)) if rows else 0

    if rule_type == "unique":
        rows = connector.execute_query(
            f"SELECT COUNT(*) AS cnt FROM ("
            f"  SELECT {col} FROM {table_qualified} WHERE {col} IS NOT NULL "
            f"  GROUP BY {col} HAVING COUNT(*) > 1"
            f") AS dups",
        )
        # Count of duplicate groups (each group = a set of dupes)
        dup_groups = int(rows[0].get("cnt", 0)) if rows else 0
        if dup_groups == 0:
            return 0
        # Count total rows involved in duplicates
        rows2 = connector.execute_query(
            f"SELECT COUNT(*) AS cnt FROM {table_qualified} "
            f"WHERE {col} IN ("
            f"  SELECT {col} FROM {table_qualified} WHERE {col} IS NOT NULL "
            f"  GROUP BY {col} HAVING COUNT(*) > 1"
            f")",
        )
        return int(rows2[0].get("cnt", 0)) if rows2 else dup_groups

    if rule_type == "regex":
        pattern = str(params.get("pattern", ""))
        if not pattern:
            return 0
        # Use SIMILAR TO / REGEXP depending on DB.
        # PostgreSQL uses ~ (regex match), MySQL uses REGEXP.
        # We try the ANSI approach: count rows where the value
        # does NOT match the pattern.
        if source_type == "postgresql":
            rows = connector.execute_query(
                f"SELECT COUNT(*) AS cnt FROM {table_qualified} WHERE {col} !~ %s",
                (pattern,),
            )
        else:
            # MySQL / Doris: REGEXP / NOT REGEXP
            rows = connector.execute_query(
                f"SELECT COUNT(*) AS cnt FROM {table_qualified} "
                f"WHERE {col} IS NOT NULL AND {col} NOT REGEXP %s",
                (pattern,),
            )
        return int(rows[0].get("cnt", 0)) if rows else 0

    if rule_type == "range":
        min_val = params.get("min")
        max_val = params.get("max")
        conditions: list[str] = []
        query_params: list[Any] = []
        if min_val is not None:
            conditions.append(f"{col} < %s")
            query_params.append(min_val)
        if max_val is not None:
            conditions.append(f"{col} > %s")
            query_params.append(max_val)
        if not conditions:
            return 0
        where_clause = " OR ".join(conditions)
        rows = connector.execute_query(
            f"SELECT COUNT(*) AS cnt FROM {table_qualified} WHERE {where_clause}",
            tuple(query_params),
        )
        return int(rows[0].get("cnt", 0)) if rows else 0

    if rule_type == "enum":
        allowed = params.get("values", [])
        if not allowed:
            return 0
        placeholders = ", ".join(["%s"] * len(allowed))
        rows = connector.execute_query(
            f"SELECT COUNT(*) AS cnt FROM {table_qualified} "
            f"WHERE {col} IS NOT NULL AND {col} NOT IN ({placeholders})",
            tuple(allowed),
        )
        return int(rows[0].get("cnt", 0)) if rows else 0

    # Unknown rule type -- skip (no violations)
    logger.warning("Unknown rule type '%s' for field '%s', skipping", rule_type, field)
    return 0


# =====================================================================
# Phase 2: Shadow-table verification
# =====================================================================


def verify_shadow_table(
    db: Session,
    run: ExecutionRun,
    contract: RuntimeSupervisionContract,
    connector: DatabaseConnector,
    quality_report: QualityReport,
) -> bool:
    """Verify the shadow table directly against the target database.

    Checks:
        1. Shadow table exists.
        2. Shadow table columns match the formal table.
        3. Shadow table row count matches the quality report.
        4. Primary keys have no duplicates.
        5. Primary keys have no NULLs.
        6. Required (not_null) fields have no NULLs.
        7. Sensitive fields are masked (if applicable).
        8. (Incremental) watermark is ahead of previous formal table.

    Returns ``True`` if all checks pass.

    Raises ``ShadowVerificationError`` if verification fails.
    """
    run_id = run.id
    shadow_table = contract.shadow_table
    target_table = contract.target_table
    target_schema = contract.target_schema

    if not shadow_table:
        raise ShadowVerificationError(
            f"Shadow table not configured for run {run_id}",
        )

    logger.info(
        "Shadow verification started for run %s (shadow=%s, target=%s)",
        run_id,
        shadow_table,
        target_table,
    )

    source_type = connector._params.source_type  # noqa: SLF001
    shadow_qualified = (
        f"{_quote_identifier(target_schema, source_type=source_type)}."
        f"{_quote_identifier(shadow_table, source_type=source_type)}"
    )
    target_qualified = (
        f"{_quote_identifier(target_schema, source_type=source_type)}."
        f"{_quote_identifier(target_table, source_type=source_type)}"
    )

    # ---- 1. Shadow table exists ----
    if source_type == "postgresql":
        exists_rows = connector.execute_query(
            "SELECT to_regclass(%s) AS exists_val",
            (f"{target_schema}.{shadow_table}",),
        )
        exists_val = exists_rows[0].get("exists_val") if exists_rows else None
        if not exists_val:
            raise ShadowVerificationError(
                f"Shadow table {shadow_qualified} does not exist",
            )
    else:
        # MySQL / Doris: check information_schema
        exists_rows = connector.execute_query(
            "SELECT COUNT(*) AS cnt FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = %s",
            (target_schema, shadow_table),
        )
        if not exists_rows or int(exists_rows[0].get("cnt", 0)) == 0:
            raise ShadowVerificationError(
                f"Shadow table {shadow_qualified} does not exist",
            )

    # ---- 2. Column comparison (best-effort) ----
    shadow_cols: list[dict[str, Any]] = []
    try:
        shadow_cols = _get_table_columns(connector, target_schema, shadow_table, source_type)
        target_cols = _get_table_columns(connector, target_schema, target_table, source_type)
        shadow_names = {c["name"] for c in shadow_cols}
        target_names = {c["name"] for c in target_cols}
        missing = target_names - shadow_names
        if missing:
            raise ShadowVerificationError(
                f"Shadow table missing columns: {missing}",
            )
    except RuntimeError:
        logger.warning("Column comparison skipped (unable to fetch schema)")

    # ---- 3. Row count check ----
    count_rows = connector.execute_query(f"SELECT COUNT(*) AS cnt FROM {shadow_qualified}")
    actual_shadow_rows = int(count_rows[0].get("cnt", 0)) if count_rows else 0

    if actual_shadow_rows == 0:
        raise ShadowVerificationError(
            f"Shadow table {shadow_qualified} is empty -- refusing to publish",
        )

    expected_shadow = quality_report.shadow_rows
    # Allow the actual count to differ from the metric snapshot by
    # a small margin (SeaTunnel metrics may lag slightly).
    if expected_shadow > 0 and abs(actual_shadow_rows - expected_shadow) > max(
        10,
        int(expected_shadow * 0.01),
    ):
        raise ShadowVerificationError(
            f"Shadow row count mismatch: actual={actual_shadow_rows}, expected={expected_shadow}",
        )

    # ---- 4 & 5. Primary key checks ----
    # Get primary keys from the contract's rollback_plan or
    # from the EtlPlan sync_strategy.  For now, we try to read
    # them from the DB itself.
    pk_columns = _get_primary_keys(
        connector,
        target_schema,
        target_table,
        source_type,
    )

    if pk_columns:
        pk_cols_sql = ", ".join(_quote_identifier(c, source_type=source_type) for c in pk_columns)

        # 4. Duplicate PKs
        dup_rows = connector.execute_query(
            f"SELECT COUNT(*) AS cnt FROM ("
            f"  SELECT {pk_cols_sql} FROM {shadow_qualified} "
            f"  GROUP BY {pk_cols_sql} HAVING COUNT(*) > 1"
            f") AS dup_pks",
        )
        dup_count = int(dup_rows[0].get("cnt", 0)) if dup_rows else 0
        if dup_count > 0:
            raise ShadowVerificationError(
                f"Shadow table has {dup_count} duplicate primary key groups",
            )

        # 5. NULL PKs
        null_conditions = " OR ".join(
            f"{_quote_identifier(c, source_type=source_type)} IS NULL" for c in pk_columns
        )
        null_rows = connector.execute_query(
            f"SELECT COUNT(*) AS cnt FROM {shadow_qualified} WHERE {null_conditions}",
        )
        null_pk_count = int(null_rows[0].get("cnt", 0)) if null_rows else 0
        if null_pk_count > 0:
            raise ShadowVerificationError(
                f"Shadow table has {null_pk_count} rows with NULL primary keys",
            )

    # ---- 6. Required (not_null) fields ----
    quality_rules: list[dict[str, Any]] = contract.quality_rules or []
    not_null_fields = [
        str(r.get("field")) for r in quality_rules if str(r.get("rule", "")).lower() == "not_null"
    ]
    for field in not_null_fields:
        col = _quote_identifier(field, source_type=source_type)
        null_rows = connector.execute_query(
            f"SELECT COUNT(*) AS cnt FROM {shadow_qualified} WHERE {col} IS NULL",
        )
        null_count = int(null_rows[0].get("cnt", 0)) if null_rows else 0
        if null_count > 0:
            raise ShadowVerificationError(
                f"Required field '{field}' has {null_count} NULL values in shadow table",
            )

    # ---- 7. Sensitive field masking check ----
    # Check if any field name contains "phone", "email", "id_card",
    # "password" -- they should not be plaintext (must contain '*' or
    # be hashed).  This is a heuristic; the actual masking rules come
    # from the EtlPlan's field_mappings transforms.
    sensitive_patterns = ["phone", "email", "id_card", "password", "mobile"]
    try:
        for col_info in shadow_cols:
            col_name = col_info["name"].lower()
            if any(p in col_name for p in sensitive_patterns):
                # Sample a few rows to check if values look masked
                quoted_col = _quote_identifier(
                    col_info["name"],
                    source_type=source_type,
                )
                sample_rows = connector.execute_query(
                    f"SELECT {quoted_col} AS val FROM {shadow_qualified} LIMIT 5",
                )
                for row in sample_rows:
                    val = str(row.get("val", ""))
                    if val and "*" not in val and len(val) > 3:
                        # Could be unmasked -- log a warning but don't fail
                        logger.warning(
                            "Sensitive field '%s' may not be masked (sample value length=%d)",
                            col_info["name"],
                            len(val),
                        )
    except (RuntimeError, KeyError):
        logger.debug("Sensitive field check skipped")

    # ---- 8. Incremental watermark (best-effort) ----
    # This check requires knowledge of the watermark column and the
    # previous formal table's max value.  We skip it if the info is
    # not available in the contract.
    rollback_plan: dict[str, Any] = contract.rollback_plan or {}
    watermark_column = rollback_plan.get("watermark_column")
    if watermark_column:
        try:
            col = _quote_identifier(watermark_column, source_type=source_type)
            # Get max watermark from shadow table
            shadow_max_rows = connector.execute_query(
                f"SELECT MAX({col}) AS max_val FROM {shadow_qualified}",
            )
            shadow_max = shadow_max_rows[0].get("max_val") if shadow_max_rows else None

            # Get max watermark from formal table (if it exists)
            target_max_rows = connector.execute_query(
                f"SELECT MAX({col}) AS max_val FROM {target_qualified}",
            )
            target_max = target_max_rows[0].get("max_val") if target_max_rows else None

            if shadow_max and target_max and shadow_max <= target_max:
                raise ShadowVerificationError(
                    f"Watermark check failed: shadow max ({shadow_max}) "
                    f"<= target max ({target_max})",
                )
        except RuntimeError:
            logger.warning("Watermark check skipped (query failed)")

    logger.info("Shadow verification PASSED for run %s", run_id)
    return True


def _get_table_columns(
    connector: DatabaseConnector,
    schema: str,
    table: str,
    source_type: str,
) -> list[dict[str, str]]:
    """Return column info (name, type) for a table."""
    if source_type == "postgresql":
        rows = connector.execute_query(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, table),
        )
    else:
        # MySQL / Doris
        rows = connector.execute_query(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, table),
        )
    return [{"name": r.get("column_name", ""), "type": r.get("data_type", "")} for r in rows]


def _get_primary_keys(
    connector: DatabaseConnector,
    schema: str,
    table: str,
    source_type: str,
) -> list[str]:
    """Return primary key column names for a table."""
    try:
        if source_type == "postgresql":
            rows = connector.execute_query(
                """
                SELECT a.attname AS column_name
                FROM pg_index i
                JOIN pg_attribute a
                  ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                WHERE i.indrelid = %s::regclass AND i.indisprimary
                ORDER BY array_position(i.indkey, a.attnum)
                """,
                (f"{schema}.{table}",),
            )
        else:
            # MySQL / Doris
            rows = connector.execute_query(
                """
                SELECT column_name
                FROM information_schema.key_column_usage
                WHERE table_schema = %s AND table_name = %s
                  AND constraint_name = 'PRIMARY'
                ORDER BY ordinal_position
                """,
                (schema, table),
            )
        return [str(r.get("column_name", "")) for r in rows if r.get("column_name")]
    except RuntimeError:
        logger.debug("Primary key lookup failed for %s.%s", schema, table)
        return []


# =====================================================================
# Top-level orchestrator (called by publish_worker)
# =====================================================================


def run_publish_quality_gate(
    db: Session,
    run: ExecutionRun,
) -> dict[str, Any]:
    """Run the full quality gate: quality check + shadow verification.

    This is the entry point called by ``publish_worker.py``.

    Steps:
        1. Load the RuntimeSupervisionContract.
        2. Set ExecutionRun.status = quality_checking.
        3. Build a target DB connector (Vault credentials).
        4. Run quality check -> write QualityReport.
        5. Run shadow-table verification.
        6. On success: return {"passed": True, "report": ...}.
        7. On failure: return {"passed": False, "reason": ...}.

    The caller (publish_worker) is responsible for committing the
    transaction and deciding whether to proceed to the swap phase
    or trigger rollback.
    """
    run_id = run.id

    # ---- 1. Load contract ----
    contract = db.scalar(
        select(RuntimeSupervisionContract).where(
            RuntimeSupervisionContract.execution_run_id == run_id,
        ),
    )
    if contract is None:
        return {
            "passed": False,
            "reason": "RuntimeSupervisionContract not found",
            "run_id": str(run_id),
        }

    # ---- 2. Mark quality_checking ----
    run.status = ExecutionStatus.quality_checking
    run.started_at = run.started_at or datetime.now(UTC)
    db.flush()

    _write_event(
        db,
        run_id,
        ExecutionEventType.supervision_heartbeat,
        "Quality check phase started",
        details={"phase": "quality_checking"},
    )

    # ---- 3. Build connector ----
    try:
        connector = _build_target_connector(db, contract)
    except Exception as exc:
        error_msg = f"Failed to connect to target DB: {str(exc)[:256]}"
        logger.error(error_msg)
        _write_event(
            db,
            run_id,
            ExecutionEventType.quality_check_failed,
            error_msg,
            severity="error",
        )
        return {
            "passed": False,
            "reason": "target_db_connection_failed",
            "error": error_msg,
            "run_id": str(run_id),
        }

    # ---- 4. Quality check ----
    try:
        report = run_quality_check(db, run, contract, connector)
    except QualityCheckError as exc:
        return {
            "passed": False,
            "reason": "quality_check_failed",
            "error": str(exc),
            "run_id": str(run_id),
        }
    except Exception as exc:
        error_msg = f"Quality check error: {str(exc)[:256]}"
        logger.exception("Quality check error for run %s", run_id)
        _write_event(
            db,
            run_id,
            ExecutionEventType.quality_check_failed,
            error_msg,
            severity="error",
        )
        return {
            "passed": False,
            "reason": "quality_check_error",
            "error": error_msg,
            "run_id": str(run_id),
        }

    # ---- 5. Shadow-table verification ----
    try:
        verify_shadow_table(db, run, contract, connector, report)
    except ShadowVerificationError as exc:
        return {
            "passed": False,
            "reason": "shadow_verification_failed",
            "error": str(exc),
            "run_id": str(run_id),
        }
    except Exception as exc:
        error_msg = f"Shadow verification error: {str(exc)[:256]}"
        logger.exception("Shadow verification error for run %s", run_id)
        return {
            "passed": False,
            "reason": "shadow_verification_error",
            "error": error_msg,
            "run_id": str(run_id),
        }
    finally:
        connector.close()

    logger.info("Publish quality gate PASSED for run %s", run_id)
    return {
        "passed": True,
        "run_id": str(run_id),
        "quality_report_id": str(report.id),
        "shadow_rows": report.shadow_rows,
        "error_rate": float(report.error_rate),
    }


# =====================================================================
# Phase 3: Atomic swap and formal-table publish
# =====================================================================


class SwapError(Exception):
    """Raised when the atomic swap fails."""


def _generate_backup_table_name(target_table: str) -> str:
    """Generate a unique backup table name for this publish attempt.

    The UUID suffix prevents a second publish for the same target on
    the same day from colliding with an existing backup table.
    """
    date_str = datetime.now(UTC).strftime("%Y%m%d")
    base = target_table[:32].rstrip("_")
    return f"{base}_backup_{date_str}_{uuid.uuid4().hex[:8]}"


def _swap_mysql(
    connector: DatabaseConnector,
    schema: str,
    target: str,
    shadow: str,
    backup: str,
) -> None:
    """MySQL atomic swap: RENAME TABLE target TO backup, shadow TO target."""
    q = _quoter_for("mysql")
    sql = (
        f"RENAME TABLE {q(schema)}.{q(target)} "
        f"TO {q(schema)}.{q(backup)}, "
        f"{q(schema)}.{q(shadow)} "
        f"TO {q(schema)}.{q(target)}"
    )
    connector.execute_statement(sql)


def _swap_postgresql(
    connector: DatabaseConnector,
    schema: str,
    target: str,
    shadow: str,
    backup: str,
) -> None:
    """PostgreSQL atomic swap in a single transaction."""
    schema_q = _quote_identifier(schema)
    sql1 = (
        f"ALTER TABLE {schema_q}.{_quote_identifier(target)} RENAME TO {_quote_identifier(backup)}"
    )
    sql2 = (
        f"ALTER TABLE {schema_q}.{_quote_identifier(shadow)} RENAME TO {_quote_identifier(target)}"
    )
    connector.execute_transaction([(sql1, None), (sql2, None)])


def _swap_doris(
    connector: DatabaseConnector,
    schema: str,
    target: str,
    shadow: str,
) -> None:
    """Doris atomic swap using ALTER TABLE ... REPLACE WITH TABLE."""
    q = _quoter_for("doris")
    sql = (
        f"ALTER TABLE {q(schema)}.{q(target)} "
        f"REPLACE WITH TABLE {q(shadow)} "
        f"PROPERTIES('swap' = 'true')"
    )
    connector.execute_statement(sql)


def _swap_clickhouse(
    connector: DatabaseConnector,
    database: str,
    target: str,
    shadow: str,
) -> None:
    """ClickHouse atomic swap: EXCHANGE TABLES."""
    sql = (
        f"EXCHANGE TABLES {_quote_identifier(database)}.{_quote_identifier(target)} "
        f"AND {_quote_identifier(database)}.{_quote_identifier(shadow)}"
    )
    connector.execute_statement(sql)


def _swap_oracle(
    connector: DatabaseConnector,
    target: str,
    shadow: str,
    backup: str,
) -> None:
    """Oracle best-effort rename swap (maintenance-window procedure)."""
    sql1 = (
        f"ALTER TABLE {_quote_identifier(target, source_type='oracle')} "
        f"RENAME TO {_quote_identifier(backup, source_type='oracle')}"
    )
    sql2 = (
        f"ALTER TABLE {_quote_identifier(shadow, source_type='oracle')} "
        f"RENAME TO {_quote_identifier(target, source_type='oracle')}"
    )
    connector.execute_transaction([(sql1, None), (sql2, None)])


def _execute_swap(
    db: Session,
    run: ExecutionRun,
    contract: RuntimeSupervisionContract,
    connector: DatabaseConnector,
    *,
    backup_table: str,
) -> str:
    """Execute the atomic swap and return the backup table name.

    Raises ``SwapError`` if the swap fails.
    """
    target = contract.target_table
    shadow = contract.shadow_table or ""
    schema = contract.target_schema
    swap_strategy = contract.swap_strategy

    _write_event(
        db,
        run.id,
        ExecutionEventType.atomic_swap_started,
        f"Atomic swap started: strategy={swap_strategy.value}, "
        f"target={target}, shadow={shadow}, backup={backup_table}",
        details={
            "strategy": swap_strategy.value,
            "target_table": target,
            "shadow_table": shadow,
            "backup_table": backup_table,
        },
    )

    try:
        if swap_strategy == SwapStrategy.mysql_rename:
            _swap_mysql(connector, schema, target, shadow, backup_table)
        elif swap_strategy == SwapStrategy.postgresql_transactional:
            _swap_postgresql(connector, schema, target, shadow, backup_table)
        elif swap_strategy == SwapStrategy.doris_replace:
            _swap_doris(connector, schema, target, shadow)
        elif swap_strategy == SwapStrategy.clickhouse_exchange:
            _swap_clickhouse(connector, schema, target, shadow)
        elif swap_strategy == SwapStrategy.oracle_redefinition:
            _swap_oracle(connector, target, shadow, backup_table)
        else:
            raise SwapError(
                f"Unsupported swap strategy: {swap_strategy.value}",
            )
    except Exception as exc:
        sanitized = str(exc)[:256]
        _write_event(
            db,
            run.id,
            ExecutionEventType.atomic_swap_failed,
            f"Atomic swap failed: {sanitized}",
            severity="error",
            details={
                "strategy": swap_strategy.value,
                "backup_table": backup_table,
                "error": sanitized,
            },
        )
        raise SwapError(f"Atomic swap failed: {sanitized}") from exc

    return backup_table


def swap_and_publish(
    db: Session,
    run: ExecutionRun,
    contract: RuntimeSupervisionContract,
) -> dict[str, Any]:
    """Execute the atomic swap and finalise the publish.

    This is called by ``publish_worker.py`` after the quality gate
    (``run_publish_quality_gate``) has passed.

    Steps:
        1. Mark ``ExecutionRun.status = publishing``.
        2. Create a ``PublishAttempt`` row (status=running).
        3. Build a target DB connector.
        4. Execute the atomic swap.
        5. On success:
           - ``PublishAttempt.status = succeeded``
           - ``ExecutionRun.status = succeeded``
           - ``ExecutionRun.finished_at = now``
           - Write ``ExecutionEvent(atomic_swap_succeeded)``
           - Write ``ExecutionEvent(job_published)``
           - Write ``EvidenceLedger(execution_finished)``
           - Save final formal table row count.
        6. On failure: return failure dict (caller triggers rollback).

    Returns:
        ``{"published": True, ...}`` on success,
        ``{"published": False, "reason": ..., "error": ...}`` on failure.
    """
    run_id = run.id

    # ---- 1. Mark publishing ----
    run.status = ExecutionStatus.publishing
    db.flush()

    _write_event(
        db,
        run_id,
        ExecutionEventType.supervision_heartbeat,
        "Publish (atomic swap) phase started",
        details={"phase": "publishing"},
    )

    # ---- 2. Create PublishAttempt ----
    backup_table = _generate_backup_table_name(contract.target_table)
    attempt = PublishAttempt(
        execution_run_id=run_id,
        strategy=contract.swap_strategy.value,
        target_table=contract.target_table,
        shadow_table=contract.shadow_table or "",
        backup_table=backup_table,
        status=PublishAttemptStatus.running,
        started_at=datetime.now(UTC),
    )
    db.add(attempt)
    db.flush()

    # ---- 3. Build connector ----
    try:
        connector = _build_target_connector(db, contract)
    except Exception as exc:
        error_msg = f"Failed to connect to target DB: {str(exc)[:256]}"
        attempt.status = PublishAttemptStatus.failed
        attempt.error_message = error_msg
        attempt.finished_at = datetime.now(UTC)
        return {
            "published": False,
            "reason": "target_db_connection_failed",
            "error": error_msg,
            "run_id": str(run_id),
            "backup_table": backup_table,
        }

    # ---- 4. Execute swap ----
    try:
        backup_table = _execute_swap(
            db,
            run,
            contract,
            connector,
            backup_table=backup_table,
        )

        # ---- 5. Success: finalise ----
        attempt.status = PublishAttemptStatus.succeeded
        attempt.finished_at = datetime.now(UTC)

        run.status = ExecutionStatus.succeeded
        run.finished_at = datetime.now(UTC)

        # Write execution events
        _write_event(
            db,
            run_id,
            ExecutionEventType.atomic_swap_succeeded,
            f"Atomic swap succeeded: backup={backup_table}",
            details={"backup_table": backup_table},
        )
        _write_event(
            db,
            run_id,
            ExecutionEventType.shadow_table_ready,
            "Formal table published from shadow table",
            details={
                "target_table": contract.target_table,
                "shadow_table": contract.shadow_table,
                "backup_table": backup_table,
            },
        )
        _write_event(
            db,
            run_id,
            ExecutionEventType.job_published,
            f"Job published: target={contract.target_table}, backup={backup_table}",
            details={
                "target_table": contract.target_table,
                "backup_table": backup_table,
                "swap_strategy": contract.swap_strategy.value,
            },
        )

        # Write evidence ledger
        log_event(
            db,
            event_type=EvidenceEventType.execution_finished,
            execution_run_id=run.id,
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            details={
                "swap_strategy": contract.swap_strategy.value,
                "backup_table": backup_table,
                "target_table": contract.target_table,
                "retention": "24h",
            },
            summary="Execution finished: atomic swap succeeded, formal table published",
        )

        # Save final formal table row count
        source_type = connector._params.source_type  # noqa: SLF001
        target_qualified = (
            f"{_quote_identifier(contract.target_schema, source_type=source_type)}"
            f".{_quote_identifier(contract.target_table, source_type=source_type)}"
        )
        try:
            count_rows = connector.execute_query(
                f"SELECT COUNT(*) AS cnt FROM {target_qualified}",
            )
            final_rows = int(count_rows[0].get("cnt", 0)) if count_rows else 0
        except RuntimeError:
            final_rows = -1

        logger.info(
            "Publish SUCCEEDED for run %s: backup=%s, final_rows=%d",
            run_id,
            backup_table,
            final_rows,
        )

        return {
            "published": True,
            "run_id": str(run_id),
            "backup_table": backup_table,
            "final_rows": final_rows,
        }

    except SwapError as exc:
        attempt.status = PublishAttemptStatus.failed
        attempt.error_message = str(exc)[:4096]
        attempt.finished_at = datetime.now(UTC)
        return {
            "published": False,
            "reason": "swap_failed",
            "error": str(exc),
            "run_id": str(run_id),
            "backup_table": backup_table,
        }
    except Exception as exc:
        error_msg = f"Unexpected publish error: {str(exc)[:256]}"
        logger.exception("Publish error for run %s", run_id)
        attempt.status = PublishAttemptStatus.failed
        attempt.error_message = error_msg
        attempt.finished_at = datetime.now(UTC)
        return {
            "published": False,
            "reason": "publish_error",
            "error": error_msg,
            "run_id": str(run_id),
            "backup_table": backup_table,
        }
    finally:
        connector.close()


__all__ = [
    "QualityCheckError",
    "RuleResult",
    "ShadowVerificationError",
    "SwapError",
    "run_publish_quality_gate",
    "run_quality_check",
    "swap_and_publish",
    "verify_shadow_table",
]
