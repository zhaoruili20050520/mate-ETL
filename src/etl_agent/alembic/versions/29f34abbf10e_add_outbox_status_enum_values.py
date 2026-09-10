"""add_outbox_status_enum_values

Revision ID: 29f34abbf10e
Revises: 0988d46f0edf
Create Date: 2026-09-09 16:38:30.000000

Adds three new values to the ``outbox_status`` PostgreSQL enum type:
``claimed``, ``dispatched``, ``succeeded``.

PostgreSQL requires ``ALTER TYPE ... ADD VALUE`` to run outside a
transaction block (or in autocommit mode) and the new values cannot
be used in DML until the transaction that added them is committed.
This migration uses ``autocommit_block`` to ensure the ALTER TYPE
runs in its own committed transaction.

The follow-up migration ``<next_rev>`` adds the new columns and
indexes that depend on these new enum values.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "29f34abbf10e"
down_revision: str | None = "0988d46f0edf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE must be committed before the new
    # values can be used in DML.  autocommit_block commits the current
    # transaction, switches to autocommit mode, runs the statements,
    # then switches back.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE outbox_status ADD VALUE IF NOT EXISTS 'claimed'",
        )
        op.execute(
            "ALTER TYPE outbox_status ADD VALUE IF NOT EXISTS 'dispatched'",
        )
        op.execute(
            "ALTER TYPE outbox_status ADD VALUE IF NOT EXISTS 'succeeded'",
        )


def downgrade() -> None:
    # PostgreSQL does not support removing individual enum values.
    # The values 'claimed', 'dispatched', and 'succeeded' will remain
    # in the type but will not be used by the application after
    # downgrade.  To fully remove them, the type would need to be
    # recreated.
    pass
