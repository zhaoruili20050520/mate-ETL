"""Pipeline ORM model.

A ``Pipeline`` represents the working draft of an ETL pipeline --
the editable, pre-freeze state.  Once the gate checker passes and
the user approves, the current EtlPlan + HOCON are frozen into an
immutable ``PipelineVersion`` (see ``models/pipeline_version.py``).

The pipeline stores the current working state as JSONB so that the
LangGraph state machine can checkpoint and resume:

- ``source_requirement``  -- the parsed PipelineRequirement
- ``etl_plan``            -- the current logical EtlPlan
- ``hocon_config``        -- the current generated HOCON text
- ``validation_issues``   -- the latest gate-check results
- ``clarification_questions`` -- questions when ``needs_clarification``

Isolation: every record carries ``tenant_id`` and ``project_id``.
"""

from __future__ import annotations

import enum
import uuid
from typing import Any

from sqlalchemy import Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from etl_agent.db.base import Base
from etl_agent.models.mixins import TimestampMixin, UUIDPKMixin


class PipelineStatus(enum.StrEnum):
    """Lifecycle status of a pipeline working draft."""

    draft = "draft"
    generating = "generating"
    needs_clarification = "needs_clarification"
    validated = "validated"
    frozen = "frozen"
    error = "error"


class Pipeline(UUIDPKMixin, TimestampMixin, Base):
    """Working-draft pipeline entity.

    A pipeline is linked to a source and target data-source connection
    and holds the evolving EtlPlan + HOCON + validation state.
    When the user freezes a version, a ``PipelineVersion`` row is
    created from the current pipeline state.
    """

    __tablename__ = "pipelines"

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
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    source_connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("data_source_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("data_source_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ---- Working state (JSONB) ----
    # Serialized PipelineRequirement (schemas.generation.PipelineRequirement)
    source_requirement: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    # Serialized EtlPlan (schemas.generation.EtlPlan)
    etl_plan: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    # Current HOCON text
    hocon_config: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    # List of serialized ValidationIssue
    validation_issues: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    # List of {"question": "...", "field": "..."} when clarification is needed
    clarification_questions: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    # ---- Lifecycle ----
    status: Mapped[PipelineStatus] = mapped_column(
        Enum(PipelineStatus, name="pipeline_status", native_enum=True),
        default=PipelineStatus.draft,
        nullable=False,
        index=True,
    )
    # Tracks how many autofix attempts have been made (max N before human)
    autofix_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    def __repr__(self) -> str:
        return f"<Pipeline(id={self.id}, name={self.name!r}, status={self.status.value})>"


__all__ = ["Pipeline", "PipelineStatus"]
