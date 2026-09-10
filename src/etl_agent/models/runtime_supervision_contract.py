"""RuntimeSupervisionContract ORM model -- budget limits for execution supervision.

A ``RuntimeSupervisionContract`` is created at commit time alongside
the ``ExecutionRun``.  It captures the **budget limits** that the
supervision worker enforces during the SeaTunnel job execution:

- ``max_rows``: maximum total rows that may be written.
- ``max_bytes``: maximum total bytes that may be read/written.
- ``max_duration_seconds``: maximum wall-clock execution time.
- ``max_error_ratio``: maximum acceptable error rejection rate.
- ``max_output_amplification``: maximum allowed written/read ratio.

If any limit is exceeded during a supervision poll, the worker
triggers a **hard interrupt**: stop the SeaTunnel job, mark the
ExecutionRun as ``failed``, write a failure snapshot to the
Evidence Ledger, and notify the operator.

The contract also carries the **quality contract** (dead-letter
table, cleaning rules) and the **swap strategy** (shadow table name,
target database type) needed for the atomic publish step.
"""

from __future__ import annotations

import enum
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from etl_agent.db.base import Base
from etl_agent.models.mixins import TimestampMixin, UUIDPKMixin


class SwapStrategy(enum.StrEnum):
    """Atomic swap strategies by target database type."""

    doris_replace = "doris_replace"
    clickhouse_exchange = "clickhouse_exchange"
    postgresql_transactional = "postgresql_transactional"
    mysql_rename = "mysql_rename"
    oracle_redefinition = "oracle_redefinition"
    none = "none"


class RuntimeSupervisionContract(UUIDPKMixin, TimestampMixin, Base):
    """Budget limits and quality contract for a single execution run.

    Created atomically in the same transaction as the
    ``ExecutionRun`` (commit phase).  The supervision worker reads
    this contract on each poll cycle to decide whether the run
    is still within budget.
    """

    __tablename__ = "runtime_supervision_contracts"

    execution_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("execution_runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
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

    # ---- Budget limits ----
    max_rows: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=10_000_000,
        comment="Maximum total rows that may be written",
    )
    max_bytes: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=10737418240,  # 10 GB
        comment="Maximum total bytes that may be read or written",
    )
    max_duration_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=3600,
        comment="Maximum wall-clock execution time in seconds",
    )
    max_error_ratio: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.01,
        comment="Maximum acceptable error rejection rate",
    )
    max_output_amplification: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=10.0,
        comment="Maximum allowed written/read ratio (fan-out guard)",
    )

    # ---- Quality contract ----
    dead_letter_table: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Table where rejected/bad rows are written",
    )
    dead_letter_schema: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="public",
    )
    quality_rules: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Cleaning rules from the EtlPlan QualityContract",
    )

    # ---- Swap / publish strategy ----
    swap_strategy: Mapped[SwapStrategy] = mapped_column(
        Enum(SwapStrategy, name="swap_strategy", native_enum=True),
        nullable=False,
        default=SwapStrategy.postgresql_transactional,
    )
    target_table: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    target_schema: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="public",
    )
    shadow_table: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="Shadow/staging table for atomic swap",
    )

    # ---- Source references (for credential materialization) ----
    source_connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("data_source_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("data_source_connections.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ---- HOCON template (with placeholders, not yet injected) ----
    hocon_template: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="HOCON config with ${source_password} etc. placeholders",
    )

    # ---- Rollback plan (JSON) ----
    rollback_plan: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Rollback strategy: swap back, truncate shadow, etc.",
    )

    def __repr__(self) -> str:
        return (
            f"<RuntimeSupervisionContract(id={self.id}, "
            f"run_id={self.execution_run_id}, "
            f"max_rows={self.max_rows}, "
            f"swap={self.swap_strategy.value})>"
        )


__all__ = [
    "RuntimeSupervisionContract",
    "SwapStrategy",
]
