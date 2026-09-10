"""Approval routes -- four-eyes dual approval for preparations.

Routes:
    POST /preparations/{id}/approvals   -- submit an approval or rejection
    GET  /preparations/{id}/approvals   -- list all approvals

All routes enforce tenant/project isolation via ``assert_project_member``
inside the service layer.

Approval constraints (enforced in ``approval_service.create_approval``):
1. The person who prepared the pipeline cannot approve it.
2. The same person cannot hold both data_checker and security_checker
   roles.
3. A rejection sets the preparation to ``rejected`` -- it cannot
   proceed to commit without revision.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from etl_agent.core.dependencies import CurrentUser, get_current_user
from etl_agent.core.exceptions import DomainError
from etl_agent.db.session import get_db
from etl_agent.models.approval import Approval
from etl_agent.schemas.approval import (
    ApprovalResponse,
    CreateApprovalRequest,
)
from etl_agent.services import approval_service

router = APIRouter(prefix="/preparations", tags=["approvals"])


def _to_domain_error(exc: DomainError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


def _to_response(approval: Approval) -> ApprovalResponse:
    """Build an ApprovalResponse from an Approval ORM object."""
    return ApprovalResponse(
        id=approval.id,
        preparation_id=approval.preparation_id,
        approver_id=approval.approver_id,
        approval_role=approval.approval_role.value,
        decision=approval.decision.value,
        comment=approval.comment,
        signed_at=approval.signed_at,
        created_at=approval.created_at,
    )


@router.post(
    "/{preparation_id}/approvals",
    response_model=ApprovalResponse,
    status_code=status.HTTP_201_CREATED,
)
def submit_approval(
    preparation_id: uuid.UUID,
    body: CreateApprovalRequest,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> ApprovalResponse:
    """Submit an approval or rejection for a preparation.

    The caller must be a project member but cannot be the person who
    prepared the pipeline (no self-approval).  The same person cannot
    hold both ``data_checker`` and ``security_checker`` roles.

    On approval, if all required roles have approved, the preparation
    transitions to ``approved`` status.

    On rejection, the preparation transitions to ``rejected`` status
    and cannot proceed to commit without creating a new preparation.
    """
    try:
        approval = approval_service.create_approval(
            db,
            preparation_id=preparation_id,
            approver_id=current_user.user_id,
            approval_role=body.approval_role,
            decision=body.decision,
            comment=body.comment,
        )
    except DomainError as exc:
        raise _to_domain_error(exc) from exc

    return _to_response(approval)


@router.get(
    "/{preparation_id}/approvals",
    response_model=list[ApprovalResponse],
)
def list_approvals(
    preparation_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> list[ApprovalResponse]:
    """List all approvals for a preparation.

    The caller must be a project member.
    """
    try:
        approvals = approval_service.list_approvals(
            db,
            preparation_id,
            current_user.user_id,
        )
    except DomainError as exc:
        raise _to_domain_error(exc) from exc

    return [_to_response(a) for a in approvals]
