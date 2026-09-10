"""add connection test status fields

Revision ID: cf7bd51db9a1
Revises: a3782f096d44
Create Date: 2026-09-09 11:29:43.802609

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "cf7bd51db9a1"
down_revision: Union[str, None] = "a3782f096d44"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create the enum type first (Alembic autogenerate omits this).
    connection_status_enum = postgresql.ENUM(
        "untested",
        "connected",
        "failed",
        name="connection_status",
        create_type=False,
    )
    connection_status_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "data_source_connections",
        sa.Column(
            "last_test_status",
            sa.Enum("untested", "connected", "failed", name="connection_status"),
            nullable=False,
            server_default="untested",
        ),
    )
    op.add_column(
        "data_source_connections",
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "data_source_connections",
        sa.Column("last_test_message", sa.String(length=1024), nullable=True),
    )
    op.add_column(
        "data_source_connections", sa.Column("last_test_latency_ms", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("data_source_connections", "last_test_latency_ms")
    op.drop_column("data_source_connections", "last_test_message")
    op.drop_column("data_source_connections", "last_tested_at")
    op.drop_column("data_source_connections", "last_test_status")
    postgresql.ENUM(name="connection_status").drop(op.get_bind(), checkfirst=True)
