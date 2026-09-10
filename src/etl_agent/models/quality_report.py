"""QualityReport ORM model -- per-run quality verification report.

A ``QualityReport`` is created by the publish worker after the
SeaTunnel job finishes and the data has landed in the shadow table.
It records:

- Row counts (read, shadow, error) and the resulting error rate.
- The ``max_error_ratio`` threshold from the QualityContract.
- Pass/fail status.
- Per-rule execution results (JSON array of rule-level verdicts).

Only when ``status == passed`` may the publish worker proceed to
the atomic-swap phase.
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
    Numeric,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from etl_agent.db.base import Base
from etl_agent.models.mixins import UUIDPKMixin


class QualityReportStatus(enum.StrEnum):
    """Outcome of the quality check."""

    passed = "passed"
    failed = "failed"


class QualityReport(UUIDPKMixin, Base):
    """Quality verification result for a single execution run.

    One-to-one with ``ExecutionRun`` (enforced by a unique index on
    ``execution_run_id``).
    """

    __tablename__ = "quality_reports"

    execution_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("execution_runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # ---- Row counts (from ExecutionMetric / shadow table / dead-letter) ----
    read_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shadow_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ---- Error rate vs threshold ----
    error_rate: Mapped[float] = mapped_column(
        Numeric(8, 6),
        nullable=False,
        default=0.0,
        comment="error_rows / read_rows",
    )
    max_error_ratio: Mapped[float] = mapped_column(
        Numeric(8, 6),
        nullable=False,
        default=0.0,
        comment="Threshold from QualityContract",
    )

    status: Mapped[QualityReportStatus] = mapped_column(
        Enum(
            QualityReportStatus,
            name="quality_report_status",
            native_enum=True,
        ),
        nullable=False,
        index=True,
    )

    # ---- Per-rule results ----
    # JSON array, each element:
    # {"rule_type": "not_null", "field": "user_id",
    #  "violations": 0, "passed": true}
    rule_results: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Per-rule execution results",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<QualityReport(id={self.id}, "
            f"run_id={self.execution_run_id}, "
            f"status={self.status.value}, "
            f"error_rate={self.error_rate})>"
        )


__all__ = [
    "QualityReport",
    "QualityReportStatus",
]
