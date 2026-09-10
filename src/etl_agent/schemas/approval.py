"""Pydantic schemas for the approval API endpoints.

These schemas define the request/response bodies for submitting
and listing approvals.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from etl_agent.models.approval import ApprovalDecision, ApprovalRole


class CreateApprovalRequest(BaseModel):
    """Request body for submitting an approval or rejection."""

    approval_role: ApprovalRole = Field(
        ...,
        description="Role under which the approval is issued",
    )
    decision: ApprovalDecision = Field(
        ...,
        description="Approval or rejection",
    )
    comment: str | None = Field(
        default=None,
        max_length=2000,
        description="Approver's comment or rejection reason",
    )


class ApprovalResponse(BaseModel):
    """Response body for a single approval."""

    id: uuid.UUID
    preparation_id: uuid.UUID
    approver_id: uuid.UUID | None = None
    approval_role: str
    decision: str
    comment: str | None = None
    signed_at: datetime
    created_at: datetime


__all__ = [
    "ApprovalResponse",
    "CreateApprovalRequest",
]
