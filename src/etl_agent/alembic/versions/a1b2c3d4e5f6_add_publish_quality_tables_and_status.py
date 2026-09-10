"""add_publish_quality_tables_and_status

Revision ID: a1b2c3d4e5f6
Revises: 8d2862d5924c
Create Date: 2026-09-09 17:20:00.000000

This migration:
1. Adds ``quality_checking`` and ``publishing`` enum values to the
   ``execution_status`` type (requires autocommit block in PostgreSQL).
2. Creates the ``quality_reports`` table.
3. Creates the ``publish_attempts`` table.

Step 6.7a -- adds the data model for the publish/quality-check phase
of the execution pipeline.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "8d2862d5924c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- 1. Add new enum values to execution_status ----
    # PostgreSQL requires ADD VALUE outside a transaction block.
    op.execute("ALTER TYPE execution_status ADD VALUE IF NOT EXISTS 'quality_checking'")
    op.execute("ALTER TYPE execution_status ADD VALUE IF NOT EXISTS 'publishing'")

    # ---- 2. Create quality_reports table ----
    op.create_table(
        "quality_reports",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "execution_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("execution_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("read_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("shadow_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "error_rate",
            sa.Numeric(8, 6),
            nullable=False,
            server_default="0",
            comment="error_rows / read_rows",
        ),
        sa.Column(
            "max_error_ratio",
            sa.Numeric(8, 6),
            nullable=False,
            server_default="0",
            comment="Threshold from QualityContract",
        ),
        sa.Column(
            "status",
            sa.Enum(
                "passed",
                "failed",
                name="quality_report_status",
                native_enum=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "rule_results",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="Per-rule execution results",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_quality_reports_execution_run_id",
        "quality_reports",
        ["execution_run_id"],
        unique=True,
    )
    op.create_index(
        "ix_quality_reports_status",
        "quality_reports",
        ["status"],
    )

    # ---- 3. Create publish_attempts table ----
    op.create_table(
        "publish_attempts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "execution_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("execution_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "strategy",
            sa.String(30),
            nullable=False,
            comment="doris | mysql | postgresql | clickhouse | oracle",
        ),
        sa.Column("target_table", sa.String(255), nullable=False),
        sa.Column("shadow_table", sa.String(255), nullable=False),
        sa.Column(
            "backup_table",
            sa.String(255),
            nullable=True,
            comment="Formal table renamed to this name during swap",
        ),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "succeeded",
                "failed",
                "rolled_back",
                name="publish_attempt_status",
                native_enum=True,
            ),
            nullable=False,
            server_default="running",
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_publish_attempts_execution_run_id",
        "publish_attempts",
        ["execution_run_id"],
        unique=True,
    )
    op.create_index(
        "ix_publish_attempts_status",
        "publish_attempts",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_publish_attempts_status", table_name="publish_attempts")
    op.drop_index("ix_publish_attempts_execution_run_id", table_name="publish_attempts")
    op.drop_table("publish_attempts")
    op.execute("DROP TYPE IF EXISTS publish_attempt_status")

    op.drop_index("ix_quality_reports_status", table_name="quality_reports")
    op.drop_index("ix_quality_reports_execution_run_id", table_name="quality_reports")
    op.drop_table("quality_reports")
    op.execute("DROP TYPE IF EXISTS quality_report_status")

    # Note: PostgreSQL does not support removing individual enum values.
    # The execution_status type will still contain 'quality_checking'
    # and 'publishing' after downgrade.  This is acceptable because
    # unused enum values are harmless.
