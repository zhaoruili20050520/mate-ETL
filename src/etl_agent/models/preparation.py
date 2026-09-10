"""Preparation ORM model -- the "what are we about to publish" frozen fact.

A ``Preparation`` is the bridge between a frozen ``PipelineVersion``
(step 4) and the commit/sign-off flow (step 5).  It captures:

- **input_fingerprint**: SHA-256 of the PipelineVersion's SHA-256 +
  source/target table names + shadow table + quality contract +
  execution budget + rollback plan.  Recomputed at commit time to
  detect tampering.
- **resource_scope**: which connections, tables, and schemas this
  run will touch.
- **data_classification**: sensitivity level (public / internal /
  confidential / restricted).
- **execution_budget**: row count, byte size, and wall-clock limits.
- **rollback_plan**: the swap/restore strategy if the run fails.
- **risk_level**: PDP-assigned P0-P3 risk rating that determines
  approval requirements (P0/P1 = dual approval, P2/P3 = reduced).

Once created, a Preparation is **append-only** -- fields are not
updated; new events (approved, rejected, committed) are recorded
as separate rows in the approvals / evidence-ledger tables.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from etl_agent.db.base import Base
from etl_agent.models.mixins import TimestampMixin, UUIDPKMixin


class PreparationStatus(enum.StrEnum):
    """Lifecycle status of a preparation."""

    prepared = "prepared"
    approved = "approved"
    rejected = "rejected"
    committed = "committed"
    cancelled = "cancelled"


class RiskLevel(enum.StrEnum):
    """PDP-assigned risk level (P0 highest, P3 lowest)."""

    P0 = "P0"  # Critical: dual approval required, cannot auto-commit
    P1 = "P1"  # High: dual approval required
    P2 = "P2"  # Medium: single approval, audit retained
    P3 = "P3"  # Low: auto-approved, audit retained


class DataClassification(enum.StrEnum):
    """Data sensitivity classification."""

    public = "public"
    internal = "internal"
    confidential = "confidential"
    restricted = "restricted"


class Preparation(UUIDPKMixin, TimestampMixin, Base):
    """Frozen-fact preparation record for a pipeline version release.

    Integrity contract: ``input_fingerprint`` is computed at creation
    from the PipelineVersion's immutable content + the execution
    parameters.  At commit time it is recomputed and must match.
    """

    __tablename__ = "preparations"

    pipeline_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pipeline_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
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

    # ---- Integrity ----
    # SHA-256 of (version_sha256 + resource_scope + budget + rollback)
    input_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )

    # ---- Resource scope ----
    # {source_connection_id, target_connection_id, source_table,
    #  target_table, shadow_table, source_schema, target_schema}
    resource_scope: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
    )

    # ---- Data sensitivity ----
    data_classification: Mapped[DataClassification] = mapped_column(
        Enum(
            DataClassification,
            name="data_classification",
            native_enum=True,
        ),
        nullable=False,
    )

    # ---- Execution budget ----
    # {max_rows, max_bytes, max_duration_seconds}
    execution_budget: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
    )

    # ---- Rollback plan ----
    # {strategy: "shadow_swap" | "truncate_restore" | "none",
    #  shadow_table, restore_sql}
    rollback_plan: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
    )

    # ---- PDP risk assessment ----
    risk_level: Mapped[RiskLevel] = mapped_column(
        Enum(RiskLevel, name="risk_level", native_enum=True),
        nullable=False,
        index=True,
    )
    # Human-readable explanation of why this risk level was assigned
    risk_reasoning: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
    )

    # ---- Lifecycle ----
    status: Mapped[PreparationStatus] = mapped_column(
        Enum(
            PreparationStatus,
            name="preparation_status",
            native_enum=True,
        ),
        default=PreparationStatus.prepared,
        nullable=False,
        index=True,
    )

    prepared_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    committed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    def __repr__(self) -> str:
        return (
            f"<Preparation(id={self.id}, "
            f"version_id={self.pipeline_version_id}, "
            f"risk={self.risk_level.value}, "
            f"status={self.status.value})>"
        )


__all__ = [
    "DataClassification",
    "Preparation",
    "PreparationStatus",
    "RiskLevel",
]
