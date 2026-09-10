"""Metadata-profile model.

Stores the result of a schema profiling / profiling run for a specific
table in a data source connection.

Isolation principle: every record carries ``tenant_id`` and ``project_id``
so that queries can be scoped to the caller's project context.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from etl_agent.db.base import Base
from etl_agent.models.mixins import TimestampMixin, UUIDPKMixin


class MetadataProfile(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "metadata_profiles"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "schema_name",
            "table_name",
            name="uq_metadata_profile_connection_schema_table",
        ),
    )

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
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("data_source_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    schema_name: Mapped[str] = mapped_column(String(255), nullable=False)
    table_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # JSON: [{"name":"col1","type":"varchar","nullable":false,"comment":"..."}]
    columns_info: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    # JSON: {"row_count":12345,"size_mb":10.2,"distinct_values":{"col1":100}}
    stats_info: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # JSON: top-N value distribution per low-cardinality column
    value_distribution: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    # JSON: masked sample rows [{"col1":"v1","col2":"v2"}, ...]
    sample_data: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)

    profiled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    def __repr__(self) -> str:
        return (
            f"<MetadataProfile(id={self.id}, "
            f"schema={self.schema_name!r}, table={self.table_name!r})>"
        )


__all__ = ["MetadataProfile"]
