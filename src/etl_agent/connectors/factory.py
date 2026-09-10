"""Connector factory.

Dispatches to the correct ``DatabaseConnector`` subclass based on
``DataSourceType``.  PostgreSQL, MySQL and Doris are fully
implemented.  Oracle and ClickHouse remain stubbed -- they raise
``NotImplementedError`` until their respective drivers are installed.

Usage::

    params = ConnectionParams(
        source_type="postgresql",
        host="localhost", port=15432,
        database_name="etl_agent",
        username="etl_agent",
        password=vault_creds["password"],
    )
    with create_connector(params) as conn:
        ok = conn.test_connection()
        tables = conn.fetch_schema()
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
import pymysql  # type: ignore[import-untyped]

from etl_agent.connectors.base import (
    ColumnInfo,
    ConnectionParams,
    DatabaseConnector,
    TableInfo,
)
from etl_agent.models.data_source_connection import DataSourceType

logger = logging.getLogger(__name__)


# =====================================================================
# PostgreSQL connector (fully implemented)
# =====================================================================


class PostgresConnector(DatabaseConnector):
    """PostgreSQL connector using ``psycopg`` (v3)."""

    def __init__(self, params: ConnectionParams) -> None:
        super().__init__(params)
        self._conn: psycopg.Connection | None = None

    def _connect(self) -> psycopg.Connection:
        """Open a connection if not already open."""
        if self._conn is None or self._conn.closed:
            self._log_safe("Connecting to PostgreSQL")
            try:
                self._conn = psycopg.connect(
                    host=self._params.host,
                    port=self._params.port,
                    dbname=self._params.database_name,
                    user=self._params.username,
                    password=self._params.password,
                    connect_timeout=10,
                )
            except Exception as exc:
                sanitized = self._sanitize(exc)
                logger.error("PostgreSQL connection failed: %s", sanitized)
                raise RuntimeError(f"PostgreSQL connection failed: {sanitized}") from exc
        return self._conn

    def test_connection(self) -> bool:
        """Run ``SELECT 1`` to verify connectivity."""
        try:
            conn = self._connect()
            conn.execute("SELECT 1").fetchone()
            self._log_safe("PostgreSQL connection test OK")
            return True
        except RuntimeError:
            raise
        except Exception as exc:
            sanitized = self._sanitize(exc)
            raise RuntimeError(f"PostgreSQL connection test failed: {sanitized}") from exc

    def fetch_schema(self) -> list[TableInfo]:
        """Fetch all schemas, tables and columns from ``information_schema``."""
        conn = self._connect()
        try:
            # Get all user tables (exclude system schemas)
            table_rows = conn.execute(
                """
                SELECT
                    table_schema,
                    table_name
                FROM information_schema.tables
                WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                  AND table_type = 'BASE TABLE'
                ORDER BY table_schema, table_name
                """,
            ).fetchall()

            if not table_rows:
                return []

            # Batch-fetch columns for all tables at once
            col_rows = conn.execute(
                """
                SELECT
                    table_schema,
                    table_name,
                    column_name,
                    data_type,
                    is_nullable,
                    column_default,
                    col_description(
                        (quote_ident(table_schema) || '.' || quote_ident(table_name))::regclass,
                        ordinal_position
                    ) AS comment
                FROM information_schema.columns
                WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                ORDER BY table_schema, table_name, ordinal_position
                """,
            ).fetchall()

            # Group columns by (schema, table)
            columns_map: dict[tuple[str, str], list[ColumnInfo]] = {}
            for row in col_rows:
                schema_name, table_name = row[0], row[1]
                key = (schema_name, table_name)
                if key not in columns_map:
                    columns_map[key] = []
                columns_map[key].append(
                    ColumnInfo(
                        name=row[2],
                        type=row[3],
                        nullable=row[4] == "YES",
                        default_value=row[5],
                        comment=row[6],
                    )
                )

            # Build TableInfo list
            tables: list[TableInfo] = []
            for schema_name, table_name in table_rows:
                key = (schema_name, table_name)
                tables.append(
                    TableInfo(
                        schema_name=schema_name,
                        table_name=table_name,
                        columns=columns_map.get(key, []),
                    )
                )

            self._log_safe("Fetched %d tables from PostgreSQL", len(tables))
            return tables

        except Exception as exc:
            sanitized = self._sanitize(exc)
            raise RuntimeError(f"PostgreSQL schema fetch failed: {sanitized}") from exc

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
            self._conn = None

    # ---- Query / DDL ----

    def execute_query(
        self,
        sql: str,
        params: tuple[Any, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a SELECT and return rows as list of dicts."""
        conn = self._connect()
        try:
            cur = conn.execute(sql, params)
            cols = [desc[0] for desc in cur.description] if cur.description else []
            return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]
        except Exception as exc:
            sanitized = self._sanitize(exc)
            raise RuntimeError(f"PostgreSQL query failed: {sanitized}") from exc

    def execute_statement(
        self,
        sql: str,
        params: tuple[Any, ...] | None = None,
    ) -> None:
        """Execute a DDL/DML statement (no result set)."""
        conn = self._connect()
        try:
            conn.execute(sql, params)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            sanitized = self._sanitize(exc)
            raise RuntimeError(f"PostgreSQL statement failed: {sanitized}") from exc

    def execute_transaction(
        self,
        statements: list[tuple[str, tuple[Any, ...] | None]],
    ) -> None:
        """Execute PostgreSQL DDL/DML statements atomically."""
        conn = self._connect()
        try:
            for sql, params in statements:
                conn.execute(sql, params)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            sanitized = self._sanitize(exc)
            raise RuntimeError(
                f"PostgreSQL transaction failed: {sanitized}",
            ) from exc


# =====================================================================
# MySQL connector (implemented with pymysql)
# =====================================================================


class MySQLConnector(DatabaseConnector):
    """MySQL connector using ``pymysql``.

    Doris is wire-compatible with MySQL; ``DorisConnector`` inherits
    from this class and overrides only the excluded-schema list and
    the database label.

    All connections use ``connect_timeout=10`` and
    ``read_timeout=30`` / ``write_timeout=30`` to prevent metadata
    exploration from hanging indefinitely.
    """

    # Schemas excluded from fetch_schema results.
    _excluded_schemas: tuple[str, ...] = (
        "information_schema",
        "mysql",
        "performance_schema",
        "sys",
    )

    # Label used in log messages and error texts.
    _db_label: str = "MySQL"

    def __init__(self, params: ConnectionParams) -> None:
        super().__init__(params)
        self._conn: pymysql.Connection | None = None

    def _connect(self) -> pymysql.Connection:
        """Open a connection if not already open."""
        if self._conn is None or not self._conn.open:
            self._log_safe("Connecting to %s", self._db_label)
            try:
                self._conn = pymysql.connect(
                    host=self._params.host,
                    port=self._params.port,
                    database=self._params.database_name,
                    user=self._params.username,
                    password=self._params.password,
                    connect_timeout=10,
                    read_timeout=30,
                    write_timeout=30,
                    charset="utf8mb4",
                )
            except Exception as exc:
                sanitized = self._sanitize(exc)
                logger.error("%s connection failed: %s", self._db_label, sanitized)
                raise RuntimeError(
                    f"{self._db_label} connection failed: {sanitized}",
                ) from exc
        return self._conn

    def test_connection(self) -> bool:
        """Run ``SELECT 1`` to verify connectivity."""
        try:
            conn = self._connect()
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            self._log_safe("%s connection test OK", self._db_label)
            return True
        except RuntimeError:
            raise
        except Exception as exc:
            sanitized = self._sanitize(exc)
            raise RuntimeError(
                f"{self._db_label} connection test failed: {sanitized}",
            ) from exc

    def fetch_schema(self) -> list[TableInfo]:
        """Fetch all tables and columns from ``information_schema``."""
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_COMMENT
                    FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_TYPE = 'BASE TABLE'
                    ORDER BY TABLE_SCHEMA, TABLE_NAME
                    """,
                )
                table_rows = cursor.fetchall()

                if not table_rows:
                    return []

                cursor.execute(
                    """
                    SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE,
                           IS_NULLABLE, COLUMN_DEFAULT, COLUMN_COMMENT
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                    ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
                    """,
                )
                col_rows = cursor.fetchall()

            # Group columns by (schema, table).
            columns_map: dict[tuple[str, str], list[ColumnInfo]] = {}
            for row in col_rows:
                schema_name, table_name = row[0], row[1]
                key = (schema_name, table_name)
                if key not in columns_map:
                    columns_map[key] = []
                columns_map[key].append(
                    ColumnInfo(
                        name=row[2],
                        type=row[3],
                        nullable=row[4] == "YES",
                        default_value=row[5],
                        comment=row[6],
                    ),
                )

            tables: list[TableInfo] = []
            for row in table_rows:
                schema_name, table_name = row[0], row[1]
                key = (schema_name, table_name)
                tables.append(
                    TableInfo(
                        schema_name=schema_name,
                        table_name=table_name,
                        columns=columns_map.get(key, []),
                        comment=row[2],
                    ),
                )

            self._log_safe("Fetched %d tables from %s", len(tables), self._db_label)
            return tables

        except Exception as exc:
            sanitized = self._sanitize(exc)
            raise RuntimeError(
                f"{self._db_label} schema fetch failed: {sanitized}",
            ) from exc

    def close(self) -> None:
        if self._conn is not None and self._conn.open:
            self._conn.close()
            self._conn = None

    # ---- Query / DDL ----

    def execute_query(
        self,
        sql: str,
        params: tuple[Any, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a SELECT and return rows as list of dicts."""
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                cols = [desc[0] for desc in cursor.description] if cursor.description else []
                return [dict(zip(cols, row, strict=False)) for row in cursor.fetchall()]
        except Exception as exc:
            sanitized = self._sanitize(exc)
            raise RuntimeError(f"{self._db_label} query failed: {sanitized}") from exc

    def execute_statement(
        self,
        sql: str,
        params: tuple[Any, ...] | None = None,
    ) -> None:
        """Execute a DDL/DML statement (no result set)."""
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            sanitized = self._sanitize(exc)
            raise RuntimeError(
                f"{self._db_label} statement failed: {sanitized}",
            ) from exc


# =====================================================================
# Oracle connector (stub -- requires oracledb)
# =====================================================================


class OracleConnector(DatabaseConnector):
    """Oracle connector (not yet implemented).

    Requires ``oracledb`` (the successor to cx_Oracle).
    Install::  pip install oracledb
    """

    def test_connection(self) -> bool:
        raise NotImplementedError(
            "Oracle connector is not yet implemented. "
            "Install oracledb and implement OracleConnector.test_connection()."
        )

    def fetch_schema(self) -> list[TableInfo]:
        raise NotImplementedError(
            "Oracle connector is not yet implemented. "
            "Install oracledb and implement OracleConnector.fetch_schema()."
        )


# =====================================================================
# ClickHouse connector (stub -- requires clickhouse-driver)
# =====================================================================


class ClickHouseConnector(DatabaseConnector):
    """ClickHouse connector (not yet implemented).

    Requires ``clickhouse-driver``.
    Install::  pip install clickhouse-driver
    """

    def test_connection(self) -> bool:
        raise NotImplementedError(
            "ClickHouse connector is not yet implemented. "
            "Install clickhouse-driver and implement ClickHouseConnector."
        )

    def fetch_schema(self) -> list[TableInfo]:
        raise NotImplementedError(
            "ClickHouse connector is not yet implemented. "
            "Install clickhouse-driver and implement ClickHouseConnector."
        )


# =====================================================================
# Doris connector (inherits MySQL -- wire-compatible)
# =====================================================================


class DorisConnector(MySQLConnector):
    """Apache Doris connector using MySQL wire protocol (pymysql).

    Doris is wire-compatible with MySQL, so this class inherits the
    full implementation from ``MySQLConnector`` and only overrides
    the database label and the list of excluded system schemas.
    """

    _excluded_schemas = (
        "information_schema",
        "mysql",
        "performance_schema",
        "sys",
        "__internal_schema",
    )

    _db_label = "Doris"


# =====================================================================
# Factory
# =====================================================================

# Registry mapping DataSourceType values to connector classes.
_CONNECTOR_REGISTRY: dict[str, type[DatabaseConnector]] = {
    DataSourceType.postgresql.value: PostgresConnector,
    DataSourceType.mysql.value: MySQLConnector,
    DataSourceType.oracle.value: OracleConnector,
    DataSourceType.clickhouse.value: ClickHouseConnector,
    DataSourceType.doris.value: DorisConnector,
}


def create_connector(params: ConnectionParams) -> DatabaseConnector:
    """Create and return a ``DatabaseConnector`` for the given params.

    Raises ``ValueError`` if ``params.source_type`` is not a recognised
    type.
    """
    source_type = params.source_type
    connector_cls = _CONNECTOR_REGISTRY.get(source_type)
    if connector_cls is None:
        raise ValueError(
            f"Unsupported source_type '{source_type}'. "
            f"Supported types: {list(_CONNECTOR_REGISTRY.keys())}"
        )

    connector = connector_cls(params)
    logger.debug(
        "Created %s connector for %s:%d/%s",
        connector_cls.__name__,
        params.host,
        params.port,
        params.database_name,
    )
    return connector


def supported_types() -> list[str]:
    """Return the list of supported source types."""
    return list(_CONNECTOR_REGISTRY.keys())


__all__ = [
    "ClickHouseConnector",
    "DorisConnector",
    "MySQLConnector",
    "OracleConnector",
    "PostgresConnector",
    "create_connector",
    "supported_types",
]
