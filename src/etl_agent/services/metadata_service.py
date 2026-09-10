"""Metadata exploration service.

This module implements the read-only profiling of a data source:

1. **Schema discovery** -- uses the connector's ``fetch_schema`` to
   enumerate tables, columns and types.
2. **Row counting** -- ``SELECT count(*) FROM <schema>.<table>``.
3. **Value distribution** -- top-N most frequent values for
   low-cardinality columns.
4. **Data sampling** -- ``SELECT * FROM <table> LIMIT N``.
5. **Sensitive-field masking** -- columns whose names match
   sensitive patterns are masked in sample rows and value
   distributions.

Security guarantees:

- **SELECT-only**: all queries are validated to start with
  ``SELECT``.  A regex guard rejects anything that looks like DDL
  (CREATE, ALTER, DROP, TRUNCATE) or DML (INSERT, UPDATE, DELETE,
  MERGE, GRANT, REVOKE).
- **Execution limits**: ``max_tables``, ``max_sample_rows`` and
  ``max_execution_seconds`` are enforced.
- **Credential safety**: error messages are sanitised before
  leaving the service.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from etl_agent.connectors.base import (
    ConnectionParams,
    TableInfo,
    sanitize_error_message,
)
from etl_agent.connectors.factory import create_connector
from etl_agent.connectors.vault import get_vault_client
from etl_agent.models.data_source_connection import DataSourceConnection
from etl_agent.models.exploration_task import ExplorationStatus, ExplorationTask
from etl_agent.models.metadata_profile import MetadataProfile

logger = logging.getLogger(__name__)

# Type alias — connectors are duck-typed at runtime
Connector = Any

# =====================================================================
# Limits
# =====================================================================

DEFAULT_MAX_TABLES = 100
DEFAULT_MAX_SAMPLE_ROWS = 50
DEFAULT_MAX_EXECUTION_SECONDS = 120
MAX_VALUE_DISTRIBUTION_BUCKETS = 10
VALUE_DISTRIBUTION_CARDINALITY_THRESHOLD = 1000

# =====================================================================
# Sensitive-field detection
# =====================================================================

# Column-name patterns that indicate sensitive data.
# Matched case-insensitively against the column name.
_SENSITIVE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"password",
        r"passwd",
        r"pwd",
        r"secret",
        r"token",
        r"api[_-]?key",
        r"access[_-]?key",
        r"private[_-]?key",
        r"credential",
        r"email",
        r"phone",
        r"mobile",
        r"tel",
        r"id[_-]?card",
        r"identity",
        r"ssn",
        r"bank[_-]?card",
        r"credit[_-]?card",
        r"card[_-]?number",
        r"account",
        r"salary",
        r"address",
        r"addr",
    )
]


def _is_sensitive_column(name: str) -> bool:
    """Return True if the column name matches a sensitive pattern."""
    return any(p.search(name) for p in _SENSITIVE_PATTERNS)


def _mask_value(value: Any) -> Any:
    """Mask a single sensitive value.

    - Strings longer than 2 chars: show first char + asterisks.
    - Short strings / non-strings: full mask.
    """
    if value is None:
        return None
    if isinstance(value, str):
        if len(value) <= 2:
            return "*" * len(value) if value else "*"
        return value[0] + "*" * (len(value) - 1)
    # Non-string types: mask entirely
    return "********"


def _json_safe(value: Any) -> Any:
    """Convert a DB-returned value to a JSON-serialisable type.

    Handles ``datetime``, ``date``, ``time``, ``Decimal``, ``bytes``,
    and nested lists/dicts.
    """
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    from datetime import date
    from datetime import datetime as dt
    from datetime import time as tm
    from decimal import Decimal

    if isinstance(value, (dt, date, tm)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")[:200]
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    # Fallback: stringify
    return str(value)[:500]


def _mask_sample_rows(
    rows: list[dict[str, Any]],
    sensitive_cols: set[str],
) -> list[dict[str, Any]]:
    """Mask sensitive columns in each sample row."""
    if not sensitive_cols:
        return rows
    masked: list[dict[str, Any]] = []
    for row in rows:
        masked_row: dict[str, Any] = {}
        for key, val in row.items():
            if key in sensitive_cols:
                masked_row[key] = _mask_value(val)
            else:
                masked_row[key] = val
        masked.append(masked_row)
    return masked


# =====================================================================
# SELECT-only guard
# =====================================================================

_FORBIDDEN_KEYWORDS: re.Pattern[str] = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|"
    r"GRANT|REVOKE|CALL|EXEC|EXECUTE|HANDLER|LOCK|UNLOCK|"
    r"REPLACE|LOAD|CALL|DO|SET|RESET|VACUUM|ANALYZE|REINDEX|"
    r"REFRESH|CLUSTER|COPY|REASSIGN|SECURITY)\b",
    re.IGNORECASE,
)


def _validate_readonly_query(sql: str) -> None:
    """Ensure a SQL string is SELECT-only.

    Raises ``RuntimeError`` if any forbidden keyword is found.
    """
    stripped = sql.strip().lstrip("(").strip()
    if not stripped.upper().startswith("SELECT") and not stripped.upper().startswith("WITH"):
        raise RuntimeError(f"Refusing to execute non-SELECT query: {sql[:80]}")
    match = _FORBIDDEN_KEYWORDS.search(stripped)
    if match:
        raise RuntimeError(
            f"Refusing to execute query containing forbidden keyword '{match.group()}': {sql[:80]}",
        )


# =====================================================================
# Query helpers (dispatch by connector type)
# =====================================================================


def _quote_identifier(identifier: str, quote_char: str = '"') -> str:
    """Quote an SQL identifier safely."""
    return quote_char + identifier.replace(quote_char, quote_char * 2) + quote_char


def _build_qualified_table(schema: str, table: str, quote_char: str = '"') -> str:
    """Build a fully-qualified, quoted table reference."""
    return f"{_quote_identifier(schema, quote_char)}.{_quote_identifier(table, quote_char)}"


def _execute_select(connector: Connector, sql: str) -> list[tuple[Any, ...]]:
    """Execute a SELECT on the given connector and return rows.

    Works for both psycopg (PostgreSQL) and pymysql (MySQL/Doris)
    by detecting the connection type.
    """
    _validate_readonly_query(sql)

    conn = getattr(connector, "_conn", None)  # noqa: SLF001

    # PostgresConnector uses psycopg.Connection (has .execute)
    if conn is not None and hasattr(conn, "execute"):
        cursor = conn.execute(sql)
        try:
            return list(cursor.fetchall())
        finally:
            cursor.close()

    # MySQLConnector / DorisConnector use pymysql connection (has .cursor)
    if conn is not None and hasattr(conn, "cursor"):
        with conn.cursor() as cursor:
            cursor.execute(sql)
            return list(cursor.fetchall())

    raise RuntimeError("Unknown connector type, cannot execute query")


def _execute_select_with_columns(
    connector: Connector,
    sql: str,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Execute a SELECT and return (column_names, rows).

    For pymysql cursors, ``cursor.description`` gives column names.
    For psycopg, the cursor has ``.description``.
    """
    _validate_readonly_query(sql)

    conn = getattr(connector, "_conn", None)  # noqa: SLF001

    # psycopg (PostgreSQL)
    if conn is not None and hasattr(conn, "execute"):
        cursor = conn.execute(sql)
        try:
            rows = list(cursor.fetchall())
            col_names = [desc[0] for desc in cursor.description] if cursor.description else []
            return col_names, rows
        finally:
            cursor.close()

    # pymysql (MySQL/Doris)
    if conn is not None and hasattr(conn, "cursor"):
        with conn.cursor() as cursor:
            cursor.execute(sql)
            rows = list(cursor.fetchall())
            col_names = [desc[0] for desc in cursor.description] if cursor.description else []
            return col_names, rows

    raise RuntimeError("Unknown connector type, cannot execute query")


# =====================================================================
# Per-table profiling
# =====================================================================


def _profile_table(
    connector: Connector,
    table: TableInfo,
    max_sample_rows: int,
    password: str,
    username: str,
) -> dict[str, Any]:
    """Profile a single table.

    Returns a dict with keys:
        columns_info, stats_info, value_distribution, sample_data
    """
    source_type = connector._params.source_type  # noqa: SLF001
    quote_char = "`" if source_type in ("mysql", "doris") else '"'
    qualified = _build_qualified_table(
        table.schema_name,
        table.table_name,
        quote_char=quote_char,
    )

    # -- Columns info --
    columns_info: list[dict[str, Any]] = [
        {
            "name": col.name,
            "type": col.type,
            "nullable": col.nullable,
            "comment": col.comment,
            "default_value": col.default_value,
        }
        for col in table.columns
    ]

    # Identify sensitive columns
    sensitive_cols: set[str] = {col.name for col in table.columns if _is_sensitive_column(col.name)}

    # -- Row count --
    row_count: int | None = None
    try:
        rows = _execute_select(connector, f"SELECT count(*) FROM {qualified}")
        if rows:
            row_count = int(rows[0][0])
    except RuntimeError as exc:
        logger.warning("Row count failed for %s: %s", qualified, str(exc)[:200])

    # -- Value distribution (top-N for low-cardinality columns) --
    value_distribution: list[dict[str, Any]] = []
    if row_count is not None and row_count > 0:
        for col in table.columns:
            # Skip high-cardinality columns and very large tables
            if row_count > 100_000:
                break
            # Skip sensitive columns in distribution
            if col.name in sensitive_cols:
                continue
            # Only profile text/enum-like columns (skip large objects)
            col_type_lower = (col.type or "").lower()
            if any(kw in col_type_lower for kw in ("text", "json", "blob", "bytea", "binary")):
                continue
            try:
                distinct_sql = (
                    f"SELECT {_quote_identifier(col.name, quote_char)} AS val, "
                    f"count(*) AS cnt "
                    f"FROM {qualified} "
                    f"WHERE {_quote_identifier(col.name, quote_char)} IS NOT NULL "
                    f"GROUP BY {_quote_identifier(col.name, quote_char)} "
                    f"ORDER BY cnt DESC "
                    f"LIMIT {MAX_VALUE_DISTRIBUTION_BUCKETS}"
                )
                dist_rows = _execute_select(connector, distinct_sql)
                if dist_rows:
                    values = [
                        {"value": str(_json_safe(r[0]))[:200], "count": int(r[1])}
                        for r in dist_rows
                    ]
                    value_distribution.append({"column": col.name, "values": values})
            except RuntimeError as exc:
                logger.warning(
                    "Value distribution failed for %s.%s: %s",
                    qualified,
                    col.name,
                    str(exc)[:200],
                )

    # -- Sample data (with masking) --
    sample_data: list[dict[str, Any]] = []
    try:
        sample_sql = f"SELECT * FROM {qualified} LIMIT {max_sample_rows}"
        col_names, raw_rows = _execute_select_with_columns(connector, sample_sql)
        if col_names and raw_rows:
            unmasked = [
                {col_names[i]: _json_safe(val) for i, val in enumerate(row)} for row in raw_rows
            ]
            sample_data = _mask_sample_rows(unmasked, sensitive_cols)
    except RuntimeError as exc:
        sanitized = sanitize_error_message(str(exc), password=password, username=username)
        logger.warning("Sample failed for %s: %s", qualified, sanitized[:200])

    return {
        "columns_info": columns_info,
        "stats_info": {
            "row_count": row_count,
            "column_count": len(table.columns),
        },
        "value_distribution": value_distribution or None,
        "sample_data": sample_data or None,
    }


# =====================================================================
# Main exploration function
# =====================================================================


def explore_metadata(
    db: Session,
    *,
    connection_id: uuid.UUID,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    max_tables: int = DEFAULT_MAX_TABLES,
    max_sample_rows: int = DEFAULT_MAX_SAMPLE_ROWS,
    max_execution_seconds: int = DEFAULT_MAX_EXECUTION_SECONDS,
) -> dict[str, Any]:
    """Run metadata exploration on a data source connection.

    Returns a summary dict with ``table_count``, ``total_rows``,
    and ``profiles`` (list of MetadataProfile dicts).

    All operations are SELECT-only.  Sensitive columns are masked
    in sample data and value distributions.
    """
    conn = db.scalar(
        select(DataSourceConnection).where(
            DataSourceConnection.id == connection_id,
            DataSourceConnection.tenant_id == tenant_id,
            DataSourceConnection.project_id == project_id,
        )
    )
    if conn is None:
        raise RuntimeError(f"Connection {connection_id} not found")

    # Read credentials from Vault
    vault = get_vault_client()
    try:
        creds = vault.read_secret_by_ref(conn.secret_ref)
    except KeyError:
        raise RuntimeError(f"Credential not found in Vault for connection '{conn.name}'") from None

    db_username = creds.get("username", "")
    db_password = creds.get("password", "")

    params = ConnectionParams(
        source_type=conn.source_type.value,
        host=conn.host,
        port=conn.port,
        database_name=conn.database_name,
        username=db_username,
        password=db_password,
    )

    start = time.monotonic()
    total_rows = 0
    tables_profiled = 0

    try:
        with create_connector(params) as connector:
            # 1. Fetch schema
            tables = connector.fetch_schema()
            # Limit the number of tables
            if len(tables) > max_tables:
                logger.info(
                    "Limiting exploration to %d tables (of %d discovered)",
                    max_tables,
                    len(tables),
                )
                tables = tables[:max_tables]

            # 2. Profile each table
            for table in tables:
                elapsed = time.monotonic() - start
                if elapsed >= max_execution_seconds:
                    logger.warning(
                        "Exploration time limit (%ds) reached after %d tables",
                        max_execution_seconds,
                        tables_profiled,
                    )
                    break

                result = _profile_table(
                    connector,
                    table,
                    max_sample_rows,
                    password=db_password,
                    username=db_username,
                )

                if result["stats_info"].get("row_count") is not None:
                    total_rows += result["stats_info"]["row_count"]

                # Upsert into metadata_profiles
                existing = db.scalar(
                    select(MetadataProfile).where(
                        MetadataProfile.connection_id == conn.id,
                        MetadataProfile.schema_name == table.schema_name,
                        MetadataProfile.table_name == table.table_name,
                    )
                )
                now = datetime.now(UTC)
                if existing is not None:
                    existing.columns_info = result["columns_info"]
                    existing.stats_info = result["stats_info"]
                    existing.value_distribution = result["value_distribution"]
                    existing.sample_data = result["sample_data"]
                    existing.profiled_at = now
                else:
                    profile = MetadataProfile(
                        tenant_id=tenant_id,
                        project_id=project_id,
                        connection_id=conn.id,
                        schema_name=table.schema_name,
                        table_name=table.table_name,
                        columns_info=result["columns_info"],
                        stats_info=result["stats_info"],
                        value_distribution=result["value_distribution"],
                        sample_data=result["sample_data"],
                        profiled_at=now,
                    )
                    db.add(profile)

                tables_profiled += 1

            db.flush()

    except RuntimeError as exc:
        sanitized = sanitize_error_message(str(exc), password=db_password, username=db_username)
        raise RuntimeError(sanitized) from exc

    return {
        "table_count": tables_profiled,
        "total_rows": total_rows,
    }


# =====================================================================
# Exploration task management
# =====================================================================


def create_exploration_task(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    connection_id: uuid.UUID,
    triggered_by: uuid.UUID,
) -> ExplorationTask:
    """Create a pending exploration task record."""
    task = ExplorationTask(
        tenant_id=tenant_id,
        project_id=project_id,
        connection_id=connection_id,
        triggered_by=triggered_by,
        status=ExplorationStatus.pending,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def update_task_status(
    db: Session,
    task_id: uuid.UUID,
    status: ExplorationStatus,
    *,
    celery_task_id: str | None = None,
    error_message: str | None = None,
    table_count: int | None = None,
    total_rows: int | None = None,
) -> ExplorationTask | None:
    """Update an exploration task's status and metadata."""
    task = db.get(ExplorationTask, task_id)
    if task is None:
        return None

    task.status = status
    if celery_task_id is not None:
        task.celery_task_id = celery_task_id
    if error_message is not None:
        task.error_message = error_message[:4096]
    if table_count is not None:
        task.table_count = table_count
    if total_rows is not None:
        task.total_rows = total_rows
    if status == ExplorationStatus.running:
        task.started_at = datetime.now(UTC)
    if status in (ExplorationStatus.completed, ExplorationStatus.failed):
        task.finished_at = datetime.now(UTC)

    db.commit()
    db.refresh(task)
    return task


def get_exploration_task(db: Session, task_id: uuid.UUID) -> ExplorationTask | None:
    return db.get(ExplorationTask, task_id)


def list_profiles_for_connection(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    connection_id: uuid.UUID,
) -> list[MetadataProfile]:
    """List all metadata profiles for a connection (isolated)."""
    return list(
        db.scalars(
            select(MetadataProfile)
            .where(
                MetadataProfile.tenant_id == tenant_id,
                MetadataProfile.project_id == project_id,
                MetadataProfile.connection_id == connection_id,
            )
            .order_by(MetadataProfile.schema_name, MetadataProfile.table_name)
        ).all()
    )


__all__ = [
    "create_exploration_task",
    "explore_metadata",
    "get_exploration_task",
    "list_profiles_for_connection",
    "update_task_status",
]
