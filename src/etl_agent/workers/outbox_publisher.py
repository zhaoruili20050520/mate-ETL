"""Outbox publisher -- polls pending outbox messages and dispatches to Celery.

The publisher runs as a standalone process (or Celery beat task) that:

1. Polls the ``outbox`` table for rows with ``status=pending`` and
   ``next_attempt_at <= now()``.
2. Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` to claim a batch of
   rows, marking them ``claimed`` (``pending → claimed``).
3. Dispatches each message to the SeaTunnel execution Celery task.
4. On successful dispatch, marks the row as ``dispatched``
   (``claimed → dispatched``).
5. On dispatch failure, increments ``retry_count``; if retries are
   exhausted, marks the row as ``dead_letter`` (otherwise reverts
   to ``pending`` for retry).
6. Reclaims stale ``claimed`` rows whose publisher crashed before
   completing the dispatch cycle.

**Separation of concerns:**
    - The Publisher only handles ``pending → claimed → dispatched``.
    - The Execution Worker handles ``dispatched → succeeded / failed``.
    - The Publisher never marks a row as ``succeeded``.

Run as:
    python -m etl_agent.workers.outbox_publisher

Or as a Celery beat task:
    @celery_app.task(name="etl_agent.publish_outbox")
    def publish_outbox() -> int:
        return run_publish_cycle()
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from etl_agent.db.session import SessionLocal
from etl_agent.models.evidence_ledger import EvidenceEventType
from etl_agent.models.execution_run import (
    ExecutionRun,
    ExecutionStatus,
)
from etl_agent.models.outbox import (
    OutboxMessage,
    OutboxStatus,
)
from etl_agent.services.evidence_service import log_event
from etl_agent.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Polling configuration
_BATCH_SIZE = 10
_POLL_INTERVAL_SECONDS = 2
_RETRY_BACKOFF_SECONDS = 30
_MAX_RETRIES = 3
# Stale claim timeout: rows in ``claimed`` for longer than this are
# reclaimed to ``pending`` (publisher crashed mid-dispatch).
_CLAIM_TIMEOUT_SECONDS = 300


def reclaim_stale_claims(db: Session) -> int:
    """Reclaim outbox rows stuck in ``claimed`` state.

    If a publisher crashed after claiming a row but before dispatching
    or reverting it, the row remains ``claimed`` indefinitely.  This
    function finds rows that have been in ``claimed`` for longer than
    ``_CLAIM_TIMEOUT_SECONDS`` and reverts them to ``pending``.

    Returns the number of reclaimed rows.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=_CLAIM_TIMEOUT_SECONDS)

    result = db.execute(
        update(OutboxMessage)
        .where(
            OutboxMessage.status == OutboxStatus.claimed,
            OutboxMessage.claimed_at < cutoff,
        )
        .values(
            status=OutboxStatus.pending,
            claimed_at=None,
        ),
    )
    reclaimed = result.rowcount or 0  # type: ignore[attr-defined]
    if reclaimed:
        logger.info("Reclaimed %d stale claimed outbox rows", reclaimed)
    return reclaimed


def claim_pending_messages(
    db: Session,
    batch_size: int = _BATCH_SIZE,
) -> list[OutboxMessage]:
    """Atomically claim a batch of pending outbox messages.

    Uses ``FOR UPDATE SKIP LOCKED`` so multiple publisher instances
    can run concurrently without contention.  Each claimed row is
    immediately transitioned from ``pending`` to ``claimed`` with a
    timestamp, preventing other publishers from picking it up.

    Returns the claimed rows (already updated to ``claimed``).
    """
    now = datetime.now(UTC)

    rows = db.scalars(
        select(OutboxMessage)
        .where(
            OutboxMessage.status == OutboxStatus.pending,
            OutboxMessage.next_attempt_at <= now,
        )
        .order_by(OutboxMessage.next_attempt_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True),
    ).all()

    for row in rows:
        row.status = OutboxStatus.claimed
        row.claimed_at = now

    return list(rows)


def dispatch_message(msg: OutboxMessage) -> tuple[bool, str | None]:
    """Dispatch a single outbox message to the Celery execution task.

    Returns (success, error_message).

    This function only sends the task to Celery -- it does NOT mark
    the outbox row.  The caller is responsible for updating the row
    status based on the dispatch result.
    """
    payload = msg.payload
    try:
        result = celery_app.send_task(
            "etl_agent.execute_seatunnel_job",
            kwargs={
                "outbox_id": str(msg.id),
                "execution_run_id": str(msg.execution_run_id),
                "hocon_text": payload.get("hocon_text", ""),
                "capability_id": payload.get("capability_id", ""),
                "capability_token": payload.get("capability_token", ""),
                "resource_scope": payload.get("resource_scope", {}),
                "rollback_plan": payload.get("rollback_plan", {}),
            },
        )
        logger.info(
            "Dispatched outbox %s to Celery task (async result id=%s)",
            msg.id,
            result.id,
        )
    except Exception as exc:
        return False, str(exc)[:4096]

    return True, None


def run_publish_cycle(db: Session | None = None) -> int:
    """Run one poll-publish cycle.

    1. Reclaims stale ``claimed`` rows.
    2. Claims pending rows (``pending → claimed``).
    3. Dispatches each to Celery.
    4. On success: ``claimed → dispatched``.
    5. On failure: revert to ``pending`` (retry) or ``dead_letter``.

    Returns the number of messages processed.

    If ``db`` is provided, uses that session; otherwise creates
    a new one (for standalone execution).
    """
    own_session = db is None
    session = db if db is not None else SessionLocal()

    try:
        # Reclaim stale claims first
        reclaim_stale_claims(session)

        messages = claim_pending_messages(session)
        if not messages:
            session.commit()
            return 0

        for msg in messages:
            success, error = dispatch_message(msg)

            now = datetime.now(UTC)

            if success:
                # claimed → dispatched
                msg.status = OutboxStatus.dispatched
                msg.dispatched_at = now

                # Log to evidence ledger
                log_event(
                    session,
                    event_type=EvidenceEventType.outbox_published,
                    execution_run_id=msg.execution_run_id,
                    tenant_id=msg.tenant_id,
                    project_id=msg.project_id,
                    details={
                        "outbox_id": str(msg.id),
                        "status": "dispatched",
                    },
                )

                logger.info(
                    "Outbox %s dispatched (run=%s)",
                    msg.id,
                    msg.execution_run_id,
                )
            else:
                msg.retry_count += 1
                msg.last_error = error

                if msg.retry_count >= msg.max_retries:
                    msg.status = OutboxStatus.dead_letter
                    msg.next_attempt_at = now + timedelta(hours=1)

                    # Mark the execution run as failed
                    run = session.scalar(
                        select(ExecutionRun).where(
                            ExecutionRun.id == msg.execution_run_id,
                        ),
                    )
                    if run:
                        run.status = ExecutionStatus.failed
                        run.error_message = (
                            f"Outbox dispatch failed after {msg.retry_count} retries: {error}"
                        )

                    # Log to evidence ledger
                    log_event(
                        session,
                        event_type=EvidenceEventType.execution_failed,
                        execution_run_id=msg.execution_run_id,
                        tenant_id=msg.tenant_id,
                        project_id=msg.project_id,
                        details={
                            "outbox_id": str(msg.id),
                            "retries": msg.retry_count,
                            "error": (error or "")[:512],
                        },
                    )

                    logger.error(
                        "Outbox %s moved to dead_letter (retries=%d): %s",
                        msg.id,
                        msg.retry_count,
                        error,
                    )
                else:
                    # Revert to pending for retry
                    msg.status = OutboxStatus.pending
                    msg.claimed_at = None
                    msg.next_attempt_at = now + timedelta(
                        seconds=_RETRY_BACKOFF_SECONDS * msg.retry_count,
                    )
                    logger.warning(
                        "Outbox %s dispatch failed (retry %d/%d): %s",
                        msg.id,
                        msg.retry_count,
                        msg.max_retries,
                        error,
                    )

        session.commit()
        return len(messages)

    except Exception:
        session.rollback()
        logger.exception("Outbox publish cycle failed")
        return 0
    finally:
        if own_session:
            session.close()


def run_publisher_loop() -> None:
    """Run the publisher in a continuous polling loop.

    Intended for standalone execution:
        python -m etl_agent.workers.outbox_publisher
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Starting outbox publisher loop (interval=%ds)", _POLL_INTERVAL_SECONDS)

    while True:
        count = run_publish_cycle()
        if count > 0:
            logger.info("Processed %d outbox messages", count)
        time.sleep(_POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_publisher_loop()


__all__ = [
    "claim_pending_messages",
    "dispatch_message",
    "reclaim_stale_claims",
    "run_publish_cycle",
    "run_publisher_loop",
]
