"""Approval service -- four-eyes dual approval with separation of duties.

This module is the **only** place that:
1. Creates ``Approval`` rows.
2. Enforces the four-eyes constraints (no self-approval, no same
   person for both roles).
3. Updates ``Preparation.status`` when approvals are submitted.
4. Logs approval events to the Evidence Ledger.

Constraints enforced:
- **No self-approval**: ``approver_id`` must differ from
  ``Preparation.prepared_by``.
- **Separation of duties**: the same user cannot submit both
  ``data_checker`` and ``security_checker`` approvals for the same
  preparation.
- **Rejection blocks commit**: any rejection sets the preparation
  to ``rejected`` status; it cannot proceed to commit without
  revision (a new preparation must be created).
- **Dual approval for P0/P1**: preparations with risk level P0 or
  P1 require both ``data_checker`` and ``security_checker`` to
  approve before the preparation can be committed.  P2 requires
  only one role.  P3 auto-approves (no approval rows needed).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from etl_agent.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from etl_agent.core.permissions import assert_project_member
from etl_agent.models.approval import (
    Approval,
    ApprovalDecision,
    ApprovalRole,
)
from etl_agent.models.evidence_ledger import EvidenceEventType
from etl_agent.models.preparation import (
    Preparation,
    PreparationStatus,
    RiskLevel,
)
from etl_agent.services.evidence_service import log_event
from etl_agent.services.preparation_service import requires_dual_approval

logger = logging.getLogger(__name__)


def create_approval(
    db: Session,
    *,
    preparation_id: uuid.UUID,
    approver_id: uuid.UUID,
    approval_role: ApprovalRole,
    decision: ApprovalDecision,
    comment: str | None = None,
) -> Approval:
    """Create an approval or rejection for a preparation.

    Enforces all four-eyes constraints:
    1. Preparation must exist and be in ``prepared`` or ``approved``
       status (cannot approve a committed/rejected preparation).
    2. Approver must be a project member.
    3. Approver cannot be the person who prepared it (no self-approval).
    4. The same person cannot hold both data_checker and
       security_checker roles.
    5. An approval for the same role+decision must not already exist.

    On rejection, the preparation is set to ``rejected`` status.
    On approval, if all required roles have approved, the preparation
    is set to ``approved`` status.
    """
    # Load the preparation
    prep = db.scalar(select(Preparation).where(Preparation.id == preparation_id))
    if prep is None:
        raise NotFoundError(f"Preparation {preparation_id} not found")

    # Check preparation is in a valid state for approval
    if prep.status not in (PreparationStatus.prepared, PreparationStatus.approved):
        raise ConflictError(
            f"Preparation is in '{prep.status.value}' state, cannot submit approval"
        )

    # Permission: must be project member
    assert_project_member(db, approver_id, prep.project_id)

    # Constraint 1: No self-approval
    if prep.prepared_by is not None and approver_id == prep.prepared_by:
        raise PermissionDeniedError("The person who prepared the pipeline cannot also approve it")

    # Constraint 2: Separation of duties
    existing_approvals = list(
        db.scalars(
            select(Approval).where(
                Approval.preparation_id == preparation_id,
                Approval.decision == ApprovalDecision.approved,
            )
        ).all()
    )
    for existing in existing_approvals:
        if existing.approver_id == approver_id and existing.approval_role != approval_role:
            raise PermissionDeniedError(
                f"User {approver_id} already approved as "
                f"{existing.approval_role.value} -- cannot hold both roles"
            )

    # Constraint 3: No duplicate role+decision
    duplicate = db.scalar(
        select(Approval).where(
            Approval.preparation_id == preparation_id,
            Approval.approval_role == approval_role,
            Approval.decision == decision,
        )
    )
    if duplicate is not None:
        raise ConflictError(
            f"Approval for role '{approval_role.value}' with decision "
            f"'{decision.value}' already exists"
        )

    # Create the approval
    approval = Approval(
        preparation_id=prep.id,
        tenant_id=prep.tenant_id,
        project_id=prep.project_id,
        approver_id=approver_id,
        approval_role=approval_role,
        decision=decision,
        comment=comment,
    )
    db.add(approval)
    db.flush()

    # Update preparation status based on decision
    if decision == ApprovalDecision.rejected:
        prep.status = PreparationStatus.rejected
        logger.info(
            "Preparation %s rejected by %s (%s)",
            prep.id,
            approver_id,
            approval_role.value,
        )
    elif decision == ApprovalDecision.approved:
        # Check if all required approvals are now in place
        all_approved = _check_all_approved(db, prep)
        if all_approved:
            prep.status = PreparationStatus.approved
            logger.info(
                "Preparation %s fully approved, status -> approved",
                prep.id,
            )

    # Log to evidence ledger (same transaction as approval + status change)
    log_event(
        db,
        event_type=(
            EvidenceEventType.rejected
            if decision == ApprovalDecision.rejected
            else EvidenceEventType.approved
        ),
        actor_id=approver_id,
        preparation_id=prep.id,
        tenant_id=prep.tenant_id,
        project_id=prep.project_id,
        details={
            "role": approval_role.value,
            "decision": decision.value,
            "comment": comment,
        },
    )

    db.commit()
    db.refresh(approval)

    return approval


def list_approvals(
    db: Session,
    preparation_id: uuid.UUID,
    user_id: uuid.UUID,
) -> list[Approval]:
    """List all approvals for a preparation.

    The caller must be a project member.
    """
    prep = db.scalar(select(Preparation).where(Preparation.id == preparation_id))
    if prep is None:
        raise NotFoundError(f"Preparation {preparation_id} not found")

    assert_project_member(db, user_id, prep.project_id)

    return list(
        db.scalars(
            select(Approval)
            .where(Approval.preparation_id == preparation_id)
            .order_by(Approval.signed_at)
        ).all()
    )


def is_fully_approved(db: Session, prep: Preparation) -> bool:
    """Check if a preparation has all required approvals.

    - P0/P1: requires both data_checker and security_checker approved.
    - P2: requires at least one role approved.
    - P3: auto-approved (no rows needed).
    """
    if prep.risk_level == RiskLevel.P3:
        return True

    approved_roles = {
        a.approval_role
        for a in db.scalars(
            select(Approval).where(
                Approval.preparation_id == prep.id,
                Approval.decision == ApprovalDecision.approved,
            )
        ).all()
    }

    if requires_dual_approval(prep.risk_level):
        # P0/P1: need both roles
        return ApprovalRole.data_checker in approved_roles and (
            ApprovalRole.security_checker in approved_roles
        )
    # P2: need at least one role
    return len(approved_roles) > 0


def can_commit(db: Session, prep: Preparation) -> bool:
    """Check if a preparation can proceed to commit.

    Requires:
    - Status is ``approved``.
    - No rejection rows exist.
    """
    if prep.status != PreparationStatus.approved:
        return False

    # Check for any rejection
    has_rejection = db.scalar(
        select(Approval).where(
            Approval.preparation_id == prep.id,
            Approval.decision == ApprovalDecision.rejected,
        )
    )
    if has_rejection is not None:
        return False

    return is_fully_approved(db, prep)


# =====================================================================
# Internal helpers
# =====================================================================


def _check_all_approved(db: Session, prep: Preparation) -> bool:
    """Check if all required approvals are in place after a new approval."""
    if prep.risk_level == RiskLevel.P3:
        return True

    approved_roles = {
        a.approval_role
        for a in db.scalars(
            select(Approval).where(
                Approval.preparation_id == prep.id,
                Approval.decision == ApprovalDecision.approved,
            )
        ).all()
    }

    if requires_dual_approval(prep.risk_level):
        return ApprovalRole.data_checker in approved_roles and (
            ApprovalRole.security_checker in approved_roles
        )
    return len(approved_roles) > 0


__all__ = [
    "can_commit",
    "create_approval",
    "is_fully_approved",
    "list_approvals",
]
