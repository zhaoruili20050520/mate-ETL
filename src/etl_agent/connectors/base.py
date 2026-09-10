"""Abstract base class and data structures for database connectors.

A ``DatabaseConnector`` wraps a specific database driver and exposes
four operations:

- **test_connection**: verify that the stored credentials work.
- **fetch_schema**: discover tables, columns and basic statistics
  for metadata profiling.
- **execute_query**: run a SELECT and return rows as list of dicts
  (used by quality-check and shadow-table verification).
- **execute_statement**: run a DDL/DML statement with no result set
  (used by atomic swap: RENAME, ALTER TABLE, EXCHANGE, etc.).

Security contract: the ``password`` field inside
``ConnectionParams`` is provided by Vault at runtime.  It must
**never** appear in log messages, API responses, or exception
texts.  The ``_sanitize_error`` helper strips the password from
any error string before it reaches the caller.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# =====================================================================
# Data structures
# =====================================================================


@dataclass(frozen=True)
class ConnectionParams:
    """All parameters needed to connect to a data source.

    ``password`` is populated from Vault at runtime and must never
    be persisted, logged, or returned in an API response.
    """

    source_type: str
    host: str
    port: int
    database_name: str
    username: str
    password: str  # from Vault -- never logged, never returned

    def safe_repr(self) -> str:
        """Return a string representation without the password."""
        return (
            f"ConnectionParams(source_type={self.source_type!r}, "
            f"host={self.host!r}, port={self.port}, "
            f"database={self.database_name!r}, "
            f"username={self.username!r})"
        )


@dataclass
class ColumnInfo:
    """Metadata for a single database column."""

    name: str
    type: str
    nullable: bool = True
    comment: str | None = None
    default_value: str | None = None


@dataclass
class TableInfo:
    """Metadata for a single table."""

    schema_name: str
    table_name: str
    columns: list[ColumnInfo] = field(default_factory=list)
    row_count: int | None = None
    size_mb: float | None = None
    comment: str | None = None


# =====================================================================
# Error sanitisation
# =====================================================================

_REDACTED = "********"


def sanitize_error_message(message: str, *, password: str = "", username: str = "") -> str:
    """Remove credentials from an error message.

    Replaces exact occurrences of ``password`` and ``username``
    with ``********``.  If both are empty, returns the message
    unchanged.
    """
    result = message
    if password:
        result = result.replace(password, _REDACTED)
    if username:
        result = result.replace(username, _REDACTED)
    return result


# =====================================================================
# Abstract connector
# =====================================================================


class DatabaseConnector(abc.ABC):
    """Abstract base for database connectors.

    Subclasses implement ``test_connection`` and ``fetch_schema``
    using the appropriate driver for their database type.

    The password is accessible via ``self._password`` for internal
    use only -- it must never appear in log output or return values.
    """

    def __init__(self, params: ConnectionParams) -> None:
        self._params = params

    # ---- Properties (internal use) ----

    @property
    def _password(self) -> str:
        """The plaintext password from Vault. Internal use only."""
        return self._params.password

    @property
    def _username(self) -> str:
        return self._params.username

    # ---- Helpers ----

    def _sanitize(self, exc: Exception) -> str:
        """Strip credentials from an exception message."""
        return sanitize_error_message(
            str(exc),
            password=self._password,
            username=self._username,
        )

    def _log_safe(self, msg: str, *args: Any) -> None:
        """Log at debug level without credentials."""
        logger.debug(
            "%s [%s]",
            msg % args if args else msg,
            self._params.safe_repr(),
        )

    # ---- Abstract methods ----

    @abc.abstractmethod
    def test_connection(self) -> bool:
        """Test connectivity with the stored credentials.

        Returns ``True`` on success.
        Raises ``RuntimeError`` with a **sanitised** message on
        failure -- the password must not appear in the message.
        """
        ...

    @abc.abstractmethod
    def fetch_schema(self) -> list[TableInfo]:
        """Discover all tables and their column metadata.

        Raises ``RuntimeError`` with a **sanitised** message on
        failure.
        """
        ...

    # ---- Query / DDL operations (used by publish & quality-check) ----

    def execute_query(
        self,
        sql: str,
        params: tuple[Any, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a SELECT and return rows as a list of dicts.

        Each dict maps column-name -> value.  Only read queries
        should be passed here; for DDL/DML use ``execute_statement``.

        Raises ``RuntimeError`` with a **sanitised** message on
        failure.
        """
        raise NotImplementedError(
            f"{type(self).__name__}.execute_query is not implemented",
        )

    def execute_statement(
        self,
        sql: str,
        params: tuple[Any, ...] | None = None,
    ) -> None:
        """Execute a DDL or DML statement that has no result set.

        Used by the atomic-swap phase (RENAME TABLE, ALTER TABLE,
        EXCHANGE TABLES, etc.).  The caller is responsible for
        ensuring the statement is appropriate for the target DB.

        Raises ``RuntimeError`` with a **sanitised** message on
        failure.
        """
        raise NotImplementedError(
            f"{type(self).__name__}.execute_statement is not implemented",
        )

    def execute_transaction(
        self,
        statements: list[tuple[str, tuple[Any, ...] | None]],
    ) -> None:
        """Execute several DDL/DML statements in one transaction.

        Subclasses may override this to provide true transactional DDL.
        The default implementation falls back to calling
        ``execute_statement`` for each item.
        """
        for sql, params in statements:
            self.execute_statement(sql, params)

    # ---- Context manager ----

    def __enter__(self) -> DatabaseConnector:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    def close(self) -> None:  # noqa: B027
        """Release any open connections. Override in subclasses."""
        ...


__all__ = [
    "ColumnInfo",
    "ConnectionParams",
    "DatabaseConnector",
    "TableInfo",
    "sanitize_error_message",
]
