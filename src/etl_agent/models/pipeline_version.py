"""PipelineVersion and PipelineVersionHistory ORM models.

A ``PipelineVersion`` is an **immutable snapshot** of a pipeline at
the moment it passed the gate checker and was frozen.  It stores:

- The structured ``EtlPlan`` (JSONB)
- The final ``HOCON`` text
- A ``SHA-256`` hash of (etl_plan + hocon_text) for integrity verification
- The original ``source_requirement`` that triggered the generation
- The ``validation_results`` at freeze time
- The creator (``frozen_by``)

Versions are **append-only**: once frozen they cannot be modified.
Any change requires creating a new version with an incremented
``version_number``.

The ``PipelineVersionHistory`` table records all lifecycle events
(frozen, viewed, approved, rejected, superseded) for audit.
"""

from __future__ import annotations

import enum
import uuid
from typing import Any

from sqlalchemy import (
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from etl_agent.db.base import Base
from etl_agent.models.mixins import TimestampMixin, UUIDPKMixin


class VersionStatus(enum.StrEnum):
    """Status of a frozen pipeline version."""

    frozen = "frozen"
    approved = "approved"
    rejected = "rejected"
    superseded = "superseded"


class VersionAction(enum.StrEnum):
    """Actions recorded in the version history table."""

    frozen = "frozen"
    viewed = "viewed"
    approved = "approved"
    rejected = "rejected"
    superseded = "superseded"


class PipelineVersion(UUIDPKMixin, TimestampMixin, Base):
    """Immutable frozen version of a pipeline.

    Integrity contract: ``sha256 = SHA256(json(etl_plan) + hocon_text)``.
    This hash can be verified at any time to detect tampering.
    """

    __tablename__ = "pipeline_versions"
    __table_args__ = (
        UniqueConstraint(
            "pipeline_id",
            "version_number",
            name="uq_pipeline_version_number",
        ),
    )

    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pipelines.id", ondelete="CASCADE"),
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

    version_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    # ---- Frozen content ----
    # Serialized EtlPlan (schemas.generation.EtlPlan)
    etl_plan: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Final HOCON text
    hocon_text: Mapped[str] = mapped_column(Text, nullable=False)
    # SHA-256 hash of (json(etl_plan) + hocon_text)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # ---- Provenance ----
    # Original PipelineRequirement that triggered the generation
    source_requirement: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
    )
    # List of serialized ValidationIssue at freeze time
    validation_results: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
    )

    status: Mapped[VersionStatus] = mapped_column(
        Enum(VersionStatus, name="pipeline_version_status", native_enum=True),
        default=VersionStatus.frozen,
        nullable=False,
        index=True,
    )

    frozen_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    def __repr__(self) -> str:
        return (
            f"<PipelineVersion(id={self.id}, "
            f"pipeline_id={self.pipeline_id}, "
            f"version={self.version_number}, "
            f"sha256={self.sha256[:12]}..., "
            f"status={self.status.value})>"
        )


class PipelineVersionHistory(UUIDPKMixin, TimestampMixin, Base):
    """Audit log for pipeline version lifecycle events.

    Every significant action on a version (freeze, view, approve,
    reject, supersede) is recorded here.  This table is
    append-only -- rows are never updated or deleted.
    """

    __tablename__ = "pipeline_versions_history"

    pipeline_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pipeline_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action: Mapped[VersionAction] = mapped_column(
        Enum(VersionAction, name="pipeline_version_action", native_enum=True),
        nullable=False,
    )
    action_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<PipelineVersionHistory(id={self.id}, "
            f"version_id={self.pipeline_version_id}, "
            f"action={self.action.value})>"
        )


__all__ = [
    "PipelineVersion",
    "PipelineVersionHistory",
    "VersionAction",
    "VersionStatus",
]
