"""CapabilityLog ORM model -- replay guard for single-use Capability tokens.

Each ``CapabilityLog`` row records the consumption of a Capability
token.  The table enforces single-use semantics: a capability_id
can appear at most once, enforced by a primary-key constraint on
``capability_id``.

When SeaTunnel (or any consumer) attempts to use a Capability:

1. The consumer presents the signed Capability token.
2. The system verifies the Ed25519 signature and checks expiry.
3. The system INSERTs a row here.  If the capability_id already
   exists, the INSERT fails (unique violation) and the replay
   attempt is rejected.
4. If the INSERT succeeds, the capability is "consumed" and cannot
   be used again.
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
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from etl_agent.db.base import Base
from etl_agent.models.mixins import TimestampMixin


class CapabilityAction(enum.StrEnum):
    """What happened to a capability token."""

    issued = "issued"
    consumed = "consumed"
    expired = "expired"
    revoked = "revoked"


class CapabilityLog(TimestampMixin, Base):
    """Replay guard + audit log for capability tokens.

    The ``capability_id`` column is the primary key, enforcing
    single-use: attempting to insert a second row with the same
    capability_id will fail with a unique-constraint violation,
    which the service layer catches and converts to a replay
    rejection.
    """

    __tablename__ = "capability_logs"

    # capability_id IS the primary key -- one row per token, ever.
    capability_id: Mapped[str] = mapped_column(
        String(128),
        primary_key=True,
    )

    execution_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("execution_runs.id", ondelete="SET NULL"),
        nullable=True,
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

    action: Mapped[CapabilityAction] = mapped_column(
        Enum(CapabilityAction, name="capability_action", native_enum=True),
        nullable=False,
        index=True,
    )

    # The consumer identity (e.g. "seatunnel-worker-1", "api-client")
    consumer: Mapped[str | None] = mapped_column(
        String(256),
        nullable=True,
    )

    # Extra context (IP, user-agent, etc.)
    details: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    def __repr__(self) -> str:
        return (
            f"<CapabilityLog(capability_id={self.capability_id[:12]}..., "
            f"action={self.action.value})>"
        )


__all__ = [
    "CapabilityAction",
    "CapabilityLog",
]
