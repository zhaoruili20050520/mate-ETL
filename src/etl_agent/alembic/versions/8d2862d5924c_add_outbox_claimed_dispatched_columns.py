"""add_outbox_claimed_dispatched_columns

Revision ID: 8d2862d5924c
Revises: 29f34abbf10e
Create Date: 2026-09-09 16:39:00.000000

Adds two timestamp columns to the ``outbox`` table:
- ``claimed_at`` -- when the publisher claimed the row (pending → claimed)
- ``dispatched_at`` -- when the publisher dispatched to Celery (claimed → dispatched)

Also creates an index on ``claimed_at`` for efficient stale-claim
reclamation queries.

Depends on migration ``29f34abbf10e`` which added the ``claimed``,
``dispatched``, and ``succeeded`` enum values to ``outbox_status``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8d2862d5924c"
down_revision: str | None = "29f34abbf10e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox",
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "outbox",
        sa.Column(
            "dispatched_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_outbox_claimed_at",
        "outbox",
        ["claimed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_claimed_at", table_name="outbox")
    op.drop_column("outbox", "dispatched_at")
    op.drop_column("outbox", "claimed_at")
