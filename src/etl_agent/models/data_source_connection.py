"""Data-source-connection model.

Security principle: this table stores ONLY a Vault secret reference
(``secret_ref``), never plaintext credentials.

Isolation principle: every record carries ``tenant_id`` and ``project_id``
so that queries can be scoped to the caller's project context.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from etl_agent.db.base import Base
from etl_agent.models.mixins import TimestampMixin, UUIDPKMixin


class DataSourceType(enum.StrEnum):
    postgresql = "postgresql"
    mysql = "mysql"
    oracle = "oracle"
    clickhouse = "clickhouse"
    doris = "doris"


class ConnectionStatus(enum.StrEnum):
    """Lifecycle status of a data-source connection."""

    untested = "untested"
    connected = "connected"
    failed = "failed"


class DataSourceConnection(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "data_source_connections"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source_type: Mapped[DataSourceType] = mapped_column(
        Enum(DataSourceType, name="data_source_type", native_enum=True),
        nullable=False,
    )
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    database_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # DB username stored in plaintext (not a secret) so the
    # frontend can display it in connection lists and test results.
    # The password remains exclusively in Vault.
    db_username: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="",
    )
    # Vault path only, e.g. "secret/etl-agent/ds/abc123" -- never the actual password
    secret_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # ---- Connection test state ----
    last_test_status: Mapped[ConnectionStatus] = mapped_column(
        Enum(ConnectionStatus, name="connection_status", native_enum=True),
        default=ConnectionStatus.untested,
        nullable=False,
    )
    last_tested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_test_message: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
    )
    last_test_latency_ms: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    def __repr__(self) -> str:
        return (
            f"<DataSourceConnection(id={self.id}, name={self.name!r}, "
            f"type={self.source_type.value}, host={self.host}:{self.port}, "
            f"status={self.last_test_status.value})>"
        )


__all__ = ["ConnectionStatus", "DataSourceConnection", "DataSourceType"]
