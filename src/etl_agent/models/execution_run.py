"""ExecutionRun ORM model -- a single committed execution of a preparation.

An ``ExecutionRun`` is created atomically during the ``commit`` phase
(Step 5c).  It represents the transition from "approved plan" to
"actual execution" and tracks:

- Which ``Preparation`` it was committed from.
- The ``Capability`` token that was issued for it (single-use, 5-min).
- The current execution status (pending / running / succeeded /
  failed / rolled_back).
- SeaTunnel job ID once the job is submitted.
- Error details if the run failed.
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
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from etl_agent.db.base import Base
from etl_agent.models.mixins import TimestampMixin, UUIDPKMixin


class ExecutionStatus(enum.StrEnum):
    """Lifecycle status of an execution run.

    State machine::

        pending → running → quality_checking → publishing → succeeded
                              ↓ (fail)           ↓ (fail)
                          rolled_back          rolled_back

    ``quality_checking`` and ``publishing`` are intermediate states
    between SeaTunnel job completion and the final ``succeeded`` /
    ``rolled_back`` terminal state.  They exist so that the publish
    worker can orchestrate quality verification and atomic swap as
    distinct phases, visible to the API and audit trail.
    """

    pending = "pending"
    running = "running"
    quality_checking = "quality_checking"
    publishing = "publishing"
    succeeded = "succeeded"
    failed = "failed"
    rolled_back = "rolled_back"


class ExecutionRun(UUIDPKMixin, TimestampMixin, Base):
    """A single committed execution of a preparation.

    Integrity contract: created atomically in the same transaction
    as the commit operation.  The ``capability_id`` links to the
    single-use Capability token that authorizes SeaTunnel to
    execute the HOCON.
    """

    __tablename__ = "execution_runs"

    preparation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("preparations.id", ondelete="CASCADE"),
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

    # The single-use capability token issued at commit time
    capability_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
    )

    status: Mapped[ExecutionStatus] = mapped_column(
        Enum(ExecutionStatus, name="execution_status", native_enum=True),
        default=ExecutionStatus.pending,
        nullable=False,
        index=True,
    )

    # SeaTunnel job ID (filled when the Outbox publisher submits the job)
    seatunnel_job_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    # Error details if the run failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_details: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    committed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    committed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    def __repr__(self) -> str:
        return (
            f"<ExecutionRun(id={self.id}, "
            f"preparation_id={self.preparation_id}, "
            f"status={self.status.value})>"
        )


__all__ = [
    "ExecutionRun",
    "ExecutionStatus",
]
