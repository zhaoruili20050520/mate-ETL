"""Add job_published to execution_event_type enum.

Revision ID: 62a3a35eda2a
Revises: a1b2c3d4e5f6
Create Date: 2026-09-09 18:00:00

"""

from __future__ import annotations

import alembic

# revision identifiers, used by Alembic.
revision = "62a3a35eda2a"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add 'job_published' value to execution_event_type enum."""
    alembic.op.execute(
        "ALTER TYPE execution_event_type ADD VALUE IF NOT EXISTS 'job_published'",
    )


def downgrade() -> None:
    """Downgrade is not supported for enum value removal.

    PostgreSQL does not support removing individual enum values.
    To fully revert, the enum type would need to be recreated.
    """
