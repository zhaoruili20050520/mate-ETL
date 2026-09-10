"""EvidenceLedger ORM model -- tamper-evident hash-chained audit log.

The ``EvidenceLedger`` is an **append-only** table that records every
significant lifecycle event in the prepare -> approve -> commit ->
execute flow.  Each row is linked to the previous one via
``prev_hash``, forming a chain where altering any historical row
invalidates all subsequent hashes.

Hash chain construction:
    payload = json.dumps({
        "event_type": ...,
        "actor_id": ...,
        "preparation_id": ...,
        "details": ...,
        "timestamp": ...,
        "prev_hash": <previous row's row_hash or "0"*64 for genesis>,
    }, sort_keys=True)
    payload_hash = sha256(payload).hexdigest()
    row_hash = sha256(prev_hash + payload_hash).hexdigest()

    stored columns: prev_hash, payload_hash, row_hash

Verification:
    To audit the chain, start from the genesis row (prev_hash = "0"*64)
    and verify that each row's row_hash == sha256(prev_hash + payload_hash).
    Any tampering with a historical row's payload will change its
    payload_hash, which changes the next row's row_hash, cascading
    through all subsequent rows.
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
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from etl_agent.db.base import Base
from etl_agent.models.mixins import TimestampMixin, UUIDPKMixin


class EvidenceEventType(enum.StrEnum):
    """Types of events recorded in the evidence ledger."""

    prepared = "prepared"
    approved = "approved"
    rejected = "rejected"
    commit = "commit"
    capability_issued = "capability_issued"
    capability_consumed = "capability_consumed"
    execution_started = "execution_started"
    execution_finished = "execution_finished"
    execution_failed = "execution_failed"
    rollback = "rollback"
    outbox_published = "outbox_published"
    outbox_failed = "outbox_failed"


# Genesis hash -- the first row has no predecessor
GENESIS_HASH = "0" * 64


class EvidenceLedger(UUIDPKMixin, TimestampMixin, Base):
    """A single entry in the tamper-evident hash chain.

    Rows are **append-only**: never updated or deleted.  Each row's
    ``row_hash`` depends on the previous row's ``row_hash``, so
    any modification to a historical row breaks the chain for all
    subsequent rows.
    """

    __tablename__ = "evidence_ledger"

    # ---- Chain links ----
    prev_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )
    payload_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    row_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )

    # ---- Event metadata ----
    event_type: Mapped[EvidenceEventType] = mapped_column(
        Enum(EvidenceEventType, name="evidence_event_type", native_enum=True),
        nullable=False,
        index=True,
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ---- Context ----
    preparation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("preparations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    execution_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("execution_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ---- Payload ----
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )

    # Human-readable summary for quick log scanning
    summary: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
    )

    # ---- Ed25519 signature (optional, for cross-system verification) ----
    signature: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # When the event occurred (may differ from created_at if backfilled)
    event_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<EvidenceLedger(id={self.id}, "
            f"event={self.event_type.value}, "
            f"row_hash={self.row_hash[:12]}...)>"
        )


__all__ = [
    "EvidenceEventType",
    "EvidenceLedger",
    "GENESIS_HASH",
]
