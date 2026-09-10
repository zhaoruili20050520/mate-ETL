r"""Exploration-task model.

Tracks the lifecycle of an async metadata-exploration job.  Each
task is scoped by ``tenant_id`` and ``project_id`` and linked to a
specific ``connection_id``.

State transitions::

    pending  ->  running  ->  completed
                        \->  failed

The ``error_message`` field is populated only when ``status=failed``.
The ``table_count`` and ``total_rows`` fields summarise the result
once exploration is complete.
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


class ExplorationStatus(enum.StrEnum):
    """Lifecycle status of a metadata-exploration task."""

    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class ExplorationTask(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "exploration_tasks"

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
    triggered_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[ExplorationStatus] = mapped_column(
        Enum(ExplorationStatus, name="exploration_status", native_enum=True),
        default=ExplorationStatus.pending,
        nullable=False,
        index=True,
    )
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    table_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
            f"<ExplorationTask(id={self.id}, "
            f"connection_id={self.connection_id}, "
            f"status={self.status!r})>"
        )


__all__ = ["ExplorationStatus", "ExplorationTask"]
