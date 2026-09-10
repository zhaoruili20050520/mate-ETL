"""ExecutionMetric ORM model -- periodic metrics snapshots during execution.

An ``ExecutionMetric`` row is written each time the supervision
worker polls SeaTunnel for running job statistics.  It captures a
point-in-time snapshot of:

- Rows read from source / written to sink / rejected to error table.
- Bytes read / written.
- Elapsed wall-clock duration.
- Output amplification ratio (written / read).
- Error rejection rate (error_rows / read_rows).

These snapshots form a time series that the supervision worker
uses to detect budget overruns and trigger hard interrupts.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from etl_agent.db.base import Base
from etl_agent.models.mixins import UUIDPKMixin


class ExecutionMetric(UUIDPKMixin, Base):
    """A single metrics sample for an ``ExecutionRun``.

    Multiple rows are created per run (one per supervision poll cycle).
    The supervision worker compares each sample against the
    ``RuntimeSupervisionContract`` budget limits and triggers a
    hard interrupt if any limit is exceeded.
    """

    __tablename__ = "execution_metrics"

    execution_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("execution_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ---- Row counts (cumulative since job start) ----
    read_rows: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )
    written_rows: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )
    error_rows: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )

    # ---- Byte counts (cumulative) ----
    bytes_read: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )
    bytes_written: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )

    # ---- Derived metrics ----
    duration_seconds: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
    )
    output_amplification: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        comment="written_rows / read_rows; >1 may indicate fan-out",
    )
    error_rejection_rate: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        comment="error_rows / read_rows; compared against max_error_ratio",
    )

    # ---- Raw SeaTunnel counters (for debugging) ----
    seatunnel_metrics: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    # When this sample was taken (may differ from created_at due to
    # async writeback)
    sampled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    def __repr__(self) -> str:
        return (
            f"<ExecutionMetric(id={self.id}, "
            f"run_id={self.execution_run_id}, "
            f"read={self.read_rows}, written={self.written_rows}, "
            f"errors={self.error_rows})>"
        )


__all__ = ["ExecutionMetric"]
