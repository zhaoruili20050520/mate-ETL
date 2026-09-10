"""Outbox ORM model -- transactional outbox for reliable event delivery.

The Transactional Outbox pattern ensures that an ``ExecutionRun``
and its dispatch message are created **atomically in the same DB
transaction**.  A separate ``OutboxPublisher`` process polls the
``outbox`` table for ``pending`` rows and dispatches them to
Celery/SeaTunnel.

Lifecycle::

    1. ``commit_preparation`` creates an ExecutionRun + an Outbox
       row with ``status=pending`` in the same transaction.
    2. The publisher claims pending rows (``pending → claimed``)
       using ``FOR UPDATE SKIP LOCKED``, dispatches the job to
       Celery (``claimed → dispatched``), and releases the row.
    3. The Execution Worker receives the Celery task, consumes the
       Capability, submits the HOCON to SeaTunnel, and on success
       marks the row ``succeeded``; on failure marks it ``failed``.
    4. If dispatch or execution fails and retries are exhausted,
       the row is moved to ``dead_letter``.

The Publisher and Execution Worker are strictly separated:
    - **Publisher** only claims and dispatches (``pending → claimed →
      dispatched``).  It never marks a row as ``succeeded``.
    - **Execution Worker** does the real work and writes back the
      final status (``dispatched → succeeded / failed``).

This guarantees **at-least-once delivery**: if the publisher crashes
after claiming but before dispatching, the row remains ``claimed``
and will be reclaimed after a timeout.  The Execution Worker is
idempotent via the Capability replay guard.
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
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from etl_agent.db.base import Base
from etl_agent.models.mixins import TimestampMixin, UUIDPKMixin


class OutboxStatus(enum.StrEnum):
    """Lifecycle status of an outbox message.

    State flow::

        pending → claimed → dispatched → succeeded / failed
                                                ↓
                                            dead_letter (after max retries)
    """

    pending = "pending"
    claimed = "claimed"
    dispatched = "dispatched"
    succeeded = "succeeded"
    failed = "failed"
    dead_letter = "dead_letter"


class OutboxMessage(UUIDPKMixin, TimestampMixin, Base):
    """A single outbox message for asynchronous dispatch.

    Created atomically in the same transaction as the
    ``ExecutionRun`` and the ``Preparation`` status update.  The
    publisher claims ``pending`` rows, dispatches them to Celery
    (``claimed → dispatched``), and the Execution Worker writes
    back the final status (``succeeded`` or ``failed``).
    """

    __tablename__ = "outbox"

    execution_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("execution_runs.id", ondelete="CASCADE"),
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

    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus, name="outbox_status", native_enum=True),
        default=OutboxStatus.pending,
        nullable=False,
        index=True,
    )

    # The message payload to be dispatched (e.g. HOCON text, run_id,
    # capability token, connection refs)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
    )

    # Retry tracking
    retry_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    max_retries: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=3,
    )

    # Error info from the last dispatch attempt
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps for publisher polling and execution tracking
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # For FOR UPDATE SKIP LOCKED polling efficiency
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    def __repr__(self) -> str:
        return (
            f"<OutboxMessage(id={self.id}, "
            f"run_id={self.execution_run_id}, "
            f"status={self.status.value}, "
            f"retries={self.retry_count})>"
        )


__all__ = [
    "OutboxMessage",
    "OutboxStatus",
]
