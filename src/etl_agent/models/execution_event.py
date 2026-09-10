"""ExecutionEvent ORM model -- execution process facts.

An ``ExecutionEvent`` records a discrete event during the lifecycle
of an ``ExecutionRun`` -- e.g. "job submitted to SeaTunnel", "vault
credentials materialized", "quality check passed", "atomic swap
executed", "hard interrupt triggered".

Unlike ``EvidenceLedger`` (which is an append-only hash-chained
audit log), ``ExecutionEvent`` is an operational log that the
supervision worker and execution worker write to during the run
for debugging and monitoring purposes.

The ``EvidenceLedger`` is the authoritative audit trail;
``ExecutionEvent`` is the operational breadcrumb trail.
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
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from etl_agent.db.base import Base
from etl_agent.models.mixins import UUIDPKMixin


class ExecutionEventType(enum.StrEnum):
    """Types of events recorded during execution."""

    job_submitted = "job_submitted"
    job_running = "job_running"
    job_finished = "job_finished"
    job_failed = "job_failed"
    job_cancelled = "job_cancelled"
    credentials_materialized = "credentials_materialized"
    hocon_rendered = "hocon_rendered"
    shadow_table_ready = "shadow_table_ready"
    quality_check_passed = "quality_check_passed"
    quality_check_failed = "quality_check_failed"
    atomic_swap_started = "atomic_swap_started"
    atomic_swap_succeeded = "atomic_swap_succeeded"
    atomic_swap_failed = "atomic_swap_failed"
    job_published = "job_published"
    hard_interrupt_triggered = "hard_interrupt_triggered"
    rollback_started = "rollback_started"
    rollback_succeeded = "rollback_succeeded"
    rollback_failed = "rollback_failed"
    budget_exceeded = "budget_exceeded"
    supervision_heartbeat = "supervision_heartbeat"


class ExecutionEvent(UUIDPKMixin, Base):
    """A single operational event during an ``ExecutionRun``.

    Multiple rows are created per run.  Events are ordered by
    ``occurred_at`` timestamp.  The ``severity`` field allows
    quick filtering of warnings and errors.
    """

    __tablename__ = "execution_events"

    execution_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("execution_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    event_type: Mapped[ExecutionEventType] = mapped_column(
        Enum(
            ExecutionEventType,
            name="execution_event_type",
            native_enum=True,
        ),
        nullable=False,
        index=True,
    )

    severity: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="info",
        index=True,
        comment="info | warning | error | critical",
    )

    message: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    # Structured details (e.g. seatunnel_job_id, metric snapshot,
    # swap SQL, error stack trace)
    details: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    # When the event actually occurred (vs. when the row was written)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    def __repr__(self) -> str:
        return (
            f"<ExecutionEvent(id={self.id}, "
            f"run_id={self.execution_run_id}, "
            f"type={self.event_type.value}, "
            f"severity={self.severity})>"
        )


__all__ = [
    "ExecutionEvent",
    "ExecutionEventType",
]
