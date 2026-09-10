"""Commit service -- the final gate before execution.

This module is the **only** place that:
1. Re-validates a Preparation at commit time (fingerprint, approvals,
   budget, permissions).
2. Creates an ``ExecutionRun``.
3. Issues an Ed25519 Capability token (5-min, single-use).
4. Records the Capability in the replay-guard table.
5. Logs the commit event to the Evidence Ledger.

Commit flow:
    1. Load Preparation -- must be in ``approved`` status.
    2. Re-verify fingerprint (detect tampering).
    3. Verify all required approvals are in place.
    4. Verify the caller has project write access.
    5. Create ExecutionRun (status=pending).
    6. Issue Capability token.
    7. Insert CapabilityLog row (action=issued).
    8. Update Preparation status to ``committed``.
    9. Commit the transaction (atomic).

Steps 5-8 are in a single DB transaction.  If any step fails, the
entire commit is rolled back.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from etl_agent.core.capability import issue_capability, verify_capability
from etl_agent.core.exceptions import (
    ConflictError,
    NotFoundError,
)
from etl_agent.core.permissions import assert_project_write
from etl_agent.models.capability_log import (
    CapabilityAction,
    CapabilityLog,
)
from etl_agent.models.evidence_ledger import EvidenceEventType
from etl_agent.models.execution_run import (
    ExecutionRun,
    ExecutionStatus,
)
from etl_agent.models.outbox import (
    OutboxMessage,
    OutboxStatus,
)
from etl_agent.models.pipeline_version import PipelineVersion
from etl_agent.models.preparation import (
    Preparation,
    PreparationStatus,
)
from etl_agent.services.approval_service import can_commit
from etl_agent.services.evidence_service import log_event
from etl_agent.services.preparation_service import verify_fingerprint

logger = logging.getLogger(__name__)


def commit_preparation(
    db: Session,
    *,
    preparation_id: uuid.UUID,
    user_id: uuid.UUID,
) -> ExecutionRun:
    """Commit an approved preparation and issue a capability.

    This is the final gate before execution.  It atomically:
    1. Re-validates fingerprint, approvals, and permissions.
    2. Creates an ExecutionRun.
    3. Issues an Ed25519 Capability token.
    4. Records the capability in the replay-guard table.
    5. Marks the preparation as ``committed``.

    Returns the created ``ExecutionRun`` (with the capability token
    stored in ``capability_id``).
    """
    # ---- 1. Load preparation ----
    prep = db.scalar(select(Preparation).where(Preparation.id == preparation_id))
    if prep is None:
        raise NotFoundError(f"Preparation {preparation_id} not found")

    # ---- 2. Status check ----
    if prep.status == PreparationStatus.committed:
        raise ConflictError(
            f"Preparation {preparation_id} is already committed",
        )
    if prep.status == PreparationStatus.rejected:
        raise ConflictError(
            f"Preparation {preparation_id} was rejected and cannot be committed",
        )
    if prep.status != PreparationStatus.approved or not can_commit(db, prep):
        raise ConflictError(
            "Preparation does not have all required approvals or has a rejection",
        )

    # ---- 3. Permission check ----
    assert_project_write(db, user_id, prep.project_id)

    # ---- 4. Re-verify fingerprint (tamper detection) ----
    version = db.scalar(
        select(PipelineVersion).where(PipelineVersion.id == prep.pipeline_version_id),
    )
    if version is None:
        raise ConflictError(
            f"PipelineVersion {prep.pipeline_version_id} not found for preparation",
        )

    if not verify_fingerprint(prep, version):
        raise ConflictError(
            "Input fingerprint mismatch -- the pipeline version or scope "
            "has changed since the preparation was created",
        )

    # ---- 5. Re-verify approvals ----
    if not can_commit(db, prep):
        raise ConflictError(
            "Preparation does not have all required approvals or has a rejection",
        )

    # ---- 6. Create ExecutionRun ----
    run = ExecutionRun(
        preparation_id=prep.id,
        tenant_id=prep.tenant_id,
        project_id=prep.project_id,
        capability_id="pending",
        status=ExecutionStatus.pending,
        committed_by=user_id,
    )
    db.add(run)
    db.flush()  # get the run.id

    # ---- 7. Issue Capability token ----
    issued = issue_capability(run.id)

    # Store the cap_id on the run
    run.capability_id = issued.payload.cap_id

    # ---- 8. Record in replay-guard table ----
    cap_log = CapabilityLog(
        capability_id=issued.payload.cap_id,
        execution_run_id=run.id,
        tenant_id=prep.tenant_id,
        project_id=prep.project_id,
        action=CapabilityAction.issued,
        consumer=None,
        details={
            "token": issued.token,
            "run_id": str(run.id),
        },
        issued_at=issued.payload.issued_at,
        expires_at=issued.payload.expires_at,
    )
    db.add(cap_log)

    # ---- 9. Mark preparation as committed ----
    prep.status = PreparationStatus.committed
    prep.committed_at = datetime.now(UTC)

    # ---- 10. Create Outbox message (same transaction) ----
    outbox_msg = OutboxMessage(
        execution_run_id=run.id,
        tenant_id=prep.tenant_id,
        project_id=prep.project_id,
        status=OutboxStatus.pending,
        payload={
            "hocon_text": version.hocon_text,
            "capability_id": issued.payload.cap_id,
            "capability_token": issued.token,
            "resource_scope": prep.resource_scope,
            "rollback_plan": prep.rollback_plan,
            "execution_budget": prep.execution_budget,
            "quality_contract": (version.etl_plan or {}).get("quality_contract", {}),
            "pipeline_version_id": str(version.id),
            "pipeline_version_sha256": version.sha256,
        },
        retry_count=0,
        max_retries=3,
    )
    db.add(outbox_msg)

    # ---- 11. Log to evidence ledger (same transaction) ----
    log_event(
        db,
        event_type=EvidenceEventType.commit,
        actor_id=user_id,
        preparation_id=prep.id,
        execution_run_id=run.id,
        tenant_id=prep.tenant_id,
        project_id=prep.project_id,
        details={
            "execution_run_id": str(run.id),
            "capability_id": issued.payload.cap_id,
            "expires_at": issued.payload.expires_at.isoformat(),
        },
    )
    log_event(
        db,
        event_type=EvidenceEventType.capability_issued,
        actor_id=user_id,
        preparation_id=prep.id,
        execution_run_id=run.id,
        tenant_id=prep.tenant_id,
        project_id=prep.project_id,
        details={
            "capability_id": issued.payload.cap_id,
            "expires_at": issued.payload.expires_at.isoformat(),
        },
    )

    # ---- 12. Atomic commit (ExecutionRun + CapabilityLog + Outbox +
    #         Preparation status + EvidenceLedger all in one tx) ----
    db.commit()
    db.refresh(run)

    logger.info(
        "Committed preparation %s -> execution run %s, capability=%s",
        prep.id,
        run.id,
        issued.payload.cap_id[:12] + "...",
    )
    return run


def consume_capability(
    db: Session,
    *,
    capability_id: str,
    consumer: str,
    token: str | None = None,
) -> ExecutionRun:
    """Consume a capability token (single-use enforcement).

    This is called by the execution engine (e.g. SeaTunnel runner)
    before starting the actual job.  It:
    1. Verifies the token signature and expiry (in core/capability).
    2. Attempts to INSERT a ``consumed`` row in capability_logs.
       If the capability_id already exists as consumed, the INSERT
       fails (PK violation) and the replay is rejected.
    3. Updates the ExecutionRun status to ``running``.

    Raises ``ConflictError`` on replay attempt.
    Raises ``NotFoundError`` if the capability was never issued.
    """
    # Load the issued capability log
    cap_log = db.scalar(
        select(CapabilityLog).where(CapabilityLog.capability_id == capability_id),
    )
    if cap_log is None:
        raise NotFoundError(f"Capability {capability_id} was never issued")

    # Verify the signed token before doing anything else.
    token_value = token or (cap_log.details or {}).get("token")
    if not token_value:
        raise ConflictError(f"Capability {capability_id} has no signed token to verify")
    try:
        payload = verify_capability(token_value)
    except ValueError as exc:
        raise ConflictError(f"Capability signature verification failed: {exc}") from exc
    if payload.cap_id != capability_id:
        raise ConflictError("Capability token does not match the requested capability_id")
    if cap_log.execution_run_id is None or payload.run_id != str(cap_log.execution_run_id):
        raise ConflictError("Capability token is bound to a different execution run")

    # Check expiry
    now = datetime.now(UTC)
    if now > cap_log.expires_at:
        raise ConflictError(f"Capability {capability_id} has expired")

    if cap_log.action != CapabilityAction.issued:
        raise ConflictError(
            f"Capability {capability_id} is not in consumable state "
            f"(action={cap_log.action.value})",
        )

    # Load the execution run
    run = db.scalar(
        select(ExecutionRun).where(ExecutionRun.id == cap_log.execution_run_id),
    )
    if run is None:
        raise NotFoundError(
            f"ExecutionRun for capability {capability_id} not found",
        )

    # Atomically mark as consumed. The WHERE clause on action=issued
    # makes this an optimistic compare-and-swap: only one concurrent
    # consumer can update the row to consumed.
    updated = db.execute(
        update(CapabilityLog)
        .where(
            CapabilityLog.capability_id == capability_id,
            CapabilityLog.action == CapabilityAction.issued,
        )
        .values(
            action=CapabilityAction.consumed,
            consumer=consumer,
            consumed_at=now,
        ),
    )
    if updated.rowcount != 1:  # type: ignore[attr-defined]
        db.rollback()
        raise ConflictError(
            f"Capability {capability_id} was consumed by another process (replay)",
        )

    cap_log.action = CapabilityAction.consumed
    cap_log.consumer = consumer
    cap_log.consumed_at = now
    run.status = ExecutionStatus.running
    run.started_at = now

    try:
        # Log to evidence ledger (same transaction)
        log_event(
            db,
            event_type=EvidenceEventType.capability_consumed,
            actor_id=run.committed_by,
            preparation_id=run.preparation_id,
            execution_run_id=run.id,
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            details={
                "capability_id": capability_id,
                "consumer": consumer,
            },
        )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(
            f"Capability {capability_id} was consumed by another process (replay)",
        ) from exc

    logger.info(
        "Capability %s consumed by %s, execution run %s -> running",
        capability_id[:12] + "...",
        consumer,
        run.id,
    )
    return run


__all__ = [
    "commit_preparation",
    "consume_capability",
]
