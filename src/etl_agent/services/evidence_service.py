"""Evidence Ledger service -- append-only hash-chained audit log.

This module provides the single entry point ``log_event`` that
appends a new row to the ``evidence_ledger`` table, computing the
hash chain link from the previous row.

It also provides ``verify_chain`` for auditing the integrity of
the entire chain.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from etl_agent.models.evidence_ledger import (
    GENESIS_HASH,
    EvidenceEventType,
    EvidenceLedger,
)

logger = logging.getLogger(__name__)


def log_event(
    db: Session,
    *,
    event_type: EvidenceEventType | str,
    actor_id: uuid.UUID | None = None,
    preparation_id: uuid.UUID | None = None,
    execution_run_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
    summary: str = "",
) -> EvidenceLedger:
    """Append a new event to the evidence ledger.

    The hash chain is maintained by:
    1. Querying the last row's ``row_hash`` (or GENESIS_HASH if empty).
    2. Computing ``payload_hash`` from the event payload.
    3. Computing ``row_hash = sha256(prev_hash + payload_hash)``.
    4. Inserting the new row.

    This function does NOT commit -- the caller is responsible for
    committing the transaction (so it can be part of a larger atomic
    operation like commit_preparation).
    """
    # Normalize event_type to the enum
    if isinstance(event_type, str):
        event_type = EvidenceEventType(event_type)

    # Get the last row's hash (the chain tip)
    last_row = db.scalar(
        select(EvidenceLedger)
        .order_by(EvidenceLedger.event_timestamp.desc(), EvidenceLedger.id.desc())
        .limit(1),
    )
    prev_hash = last_row.row_hash if last_row is not None else GENESIS_HASH

    # Build the payload
    now = datetime.now(UTC).isoformat()
    payload = {
        "event_type": event_type.value,
        "actor_id": str(actor_id) if actor_id else None,
        "preparation_id": str(preparation_id) if preparation_id else None,
        "execution_run_id": str(execution_run_id) if execution_run_id else None,
        "details": details or {},
        "timestamp": now,
        "prev_hash": prev_hash,
    }
    payload_json = json.dumps(payload, sort_keys=True, default=str)
    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

    # Compute the row hash
    row_hash = hashlib.sha256(
        (prev_hash + payload_hash).encode("utf-8"),
    ).hexdigest()

    # Create the ledger entry
    event_timestamp = datetime.fromisoformat(now)
    entry = EvidenceLedger(
        prev_hash=prev_hash,
        payload_hash=payload_hash,
        row_hash=row_hash,
        event_type=event_type,
        actor_id=actor_id,
        preparation_id=preparation_id,
        execution_run_id=execution_run_id,
        tenant_id=tenant_id,
        project_id=project_id,
        details=details or {},
        summary=summary or f"{event_type.value} at {now}",
        event_timestamp=event_timestamp,
    )
    db.add(entry)
    db.flush()

    logger.info(
        "EVIDENCE LEDGER: event=%s row_hash=%s... prev=%s...",
        event_type.value,
        row_hash[:12],
        prev_hash[:12],
    )
    return entry


def verify_chain(db: Session) -> tuple[bool, list[str]]:
    """Verify the integrity of the entire hash chain.

    Returns (is_valid, errors).  Each error string describes a
    broken link in the chain (row index + expected vs actual hash).
    """
    rows = list(
        db.scalars(
            select(EvidenceLedger).order_by(
                EvidenceLedger.event_timestamp,
                EvidenceLedger.id,
            ),
        ).all(),
    )

    errors: list[str] = []
    expected_prev = GENESIS_HASH

    for i, row in enumerate(rows):
        # Check prev_hash linkage
        if row.prev_hash != expected_prev:
            errors.append(
                f"Row {i} (id={row.id}): prev_hash mismatch -- "
                f"expected {expected_prev[:12]}..., "
                f"got {row.prev_hash[:12]}...",
            )

        # Recompute payload_hash
        payload = {
            "event_type": row.event_type.value,
            "actor_id": str(row.actor_id) if row.actor_id else None,
            "preparation_id": str(row.preparation_id) if row.preparation_id else None,
            "execution_run_id": (str(row.execution_run_id) if row.execution_run_id else None),
            "details": row.details,
            "timestamp": row.event_timestamp.isoformat(),
            "prev_hash": row.prev_hash,
        }
        payload_json = json.dumps(payload, sort_keys=True, default=str)
        recomputed_payload_hash = hashlib.sha256(
            payload_json.encode("utf-8"),
        ).hexdigest()

        if recomputed_payload_hash != row.payload_hash:
            errors.append(
                f"Row {i} (id={row.id}): payload_hash mismatch -- "
                f"expected {recomputed_payload_hash[:12]}..., "
                f"got {row.payload_hash[:12]}...",
            )

        # Recompute row_hash
        recomputed_row_hash = hashlib.sha256(
            (row.prev_hash + row.payload_hash).encode("utf-8"),
        ).hexdigest()

        if recomputed_row_hash != row.row_hash:
            errors.append(
                f"Row {i} (id={row.id}): row_hash mismatch -- "
                f"expected {recomputed_row_hash[:12]}..., "
                f"got {row.row_hash[:12]}...",
            )

        expected_prev = row.row_hash

    is_valid = len(errors) == 0
    if is_valid:
        logger.info("Evidence ledger chain verified: %d rows, all valid", len(rows))
    else:
        logger.error(
            "Evidence ledger chain BROKEN: %d rows, %d errors",
            len(rows),
            len(errors),
        )

    return is_valid, errors


def get_chain_stats(db: Session) -> dict[str, Any]:
    """Return summary statistics about the ledger."""
    total = db.scalar(select(func.count(EvidenceLedger.id))) or 0
    if total == 0:
        return {"total_events": 0, "chain_tip": GENESIS_HASH}

    last_row = db.scalar(
        select(EvidenceLedger)
        .order_by(EvidenceLedger.event_timestamp.desc(), EvidenceLedger.id.desc())
        .limit(1),
    )
    return {
        "total_events": total,
        "chain_tip": last_row.row_hash if last_row else GENESIS_HASH,
        "last_event_type": (last_row.event_type.value if last_row else None),
        "last_event_at": (last_row.event_timestamp.isoformat() if last_row else None),
    }


__all__ = [
    "get_chain_stats",
    "log_event",
    "verify_chain",
]
