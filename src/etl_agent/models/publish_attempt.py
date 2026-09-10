"""PublishAttempt ORM model -- atomic-swap attempt tracking.

A ``PublishAttempt`` records a single attempt to atomically swap
the shadow table into the formal (target) table after quality
verification passes.  It tracks:

- The swap strategy used (doris / mysql / postgresql / clickhouse / oracle).
- The target, shadow and backup table names.
- The attempt status (running → succeeded | failed | rolled_back).
- Error message on failure.
- Start and finish timestamps.

One execution run may have at most one publish attempt (unique index
on ``execution_run_id``), because the publish worker is the single
entry point for the swap phase.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from etl_agent.db.base import Base
from etl_agent.models.mixins import UUIDPKMixin


class PublishAttemptStatus(enum.StrEnum):
    """Status of an atomic-swap attempt."""

    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    rolled_back = "rolled_back"


class PublishAttempt(UUIDPKMixin, Base):
    """A single atomic-swap attempt for an execution run.

    One-to-one with ``ExecutionRun`` (unique index on
    ``execution_run_id``).
    """

    __tablename__ = "publish_attempts"

    execution_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("execution_runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    strategy: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        comment="doris | mysql | postgresql | clickhouse | oracle",
    )

    target_table: Mapped[str] = mapped_column(String(255), nullable=False)
    shadow_table: Mapped[str] = mapped_column(String(255), nullable=False)
    backup_table: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Formal table renamed to this name during swap",
    )

    status: Mapped[PublishAttemptStatus] = mapped_column(
        Enum(
            PublishAttemptStatus,
            name="publish_attempt_status",
            native_enum=True,
        ),
        nullable=False,
        default=PublishAttemptStatus.running,
        index=True,
    )

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    def __repr__(self) -> str:
        return (
            f"<PublishAttempt(id={self.id}, "
            f"run_id={self.execution_run_id}, "
            f"strategy={self.strategy}, "
            f"status={self.status.value})>"
        )


__all__ = [
    "PublishAttempt",
    "PublishAttemptStatus",
]
