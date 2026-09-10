"""Approval ORM model -- four-eyes dual approval for preparations.

An ``Approval`` records one person's sign-off (or rejection) of a
``Preparation``.  Each approval is tagged with a role:

- ``data_checker``: verifies data correctness, field mappings,
  quality contracts, and that the EtlPlan matches real metadata.
- ``security_checker``: verifies data classification, access
  scopes, sensitive-field handling, and rollback safety.

Constraints enforced by the service layer (not DB constraints):

1. **No self-approval**: ``approver_id`` must differ from
   ``Preparation.prepared_by``.
2. **Separation of duties**: the same user cannot hold both
   ``data_checker`` and ``security_checker`` roles for the same
   preparation.
3. **Rejection blocks commit**: if any approval has
   ``decision=rejected``, the preparation reverts to ``rejected``
   status and cannot proceed to commit without revision.
4. **Dual approval for P0/P1**: preparations with ``risk_level``
   P0 or P1 require both roles to approve; P2 requires only one
   role; P3 auto-approves (no approval rows needed).

Approval rows are append-only -- they are never updated or deleted.
Re-approval after rejection creates a new row.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from etl_agent.db.base import Base
from etl_agent.models.mixins import TimestampMixin, UUIDPKMixin


class ApprovalRole(enum.StrEnum):
    """Role under which an approval is issued."""

    data_checker = "data_checker"
    security_checker = "security_checker"


class ApprovalDecision(enum.StrEnum):
    """The approver's decision."""

    approved = "approved"
    rejected = "rejected"


class Approval(UUIDPKMixin, TimestampMixin, Base):
    """A single approval or rejection of a preparation.

    Each row represents one person signing off (or rejecting) under
    a specific role.  A preparation may have multiple approval rows
    (e.g. data_checker approved + security_checker approved, or a
    rejected row followed by a revised approved row).
    """

    __tablename__ = "approvals"
    __table_args__ = (
        # A person cannot approve twice under the same role for the
        # same preparation (rejection + later re-approval are different
        # rows, but the unique constraint is on active approvals only
        # -- we enforce this in service logic rather than DB because
        # rejected rows should not block re-approval).
        UniqueConstraint(
            "preparation_id",
            "approval_role",
            "decision",
            name="uq_preparation_role_decision",
        ),
    )

    preparation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("preparations.id", ondelete="CASCADE"),
        nullable=False,
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

    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    approval_role: Mapped[ApprovalRole] = mapped_column(
        Enum(ApprovalRole, name="approval_role", native_enum=True),
        nullable=False,
        index=True,
    )

    decision: Mapped[ApprovalDecision] = mapped_column(
        Enum(ApprovalDecision, name="approval_decision", native_enum=True),
        nullable=False,
        index=True,
    )

    comment: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    signed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<Approval(id={self.id}, "
            f"preparation_id={self.preparation_id}, "
            f"role={self.approval_role.value}, "
            f"decision={self.decision.value})>"
        )


__all__ = [
    "Approval",
    "ApprovalDecision",
    "ApprovalRole",
]
