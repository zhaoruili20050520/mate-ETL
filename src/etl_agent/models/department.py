"""Department model with self-referencing tree structure.

Each tenant can build a multi-level department hierarchy.  A
``parent_id`` foreign key to the same table enables the tree;
root nodes have ``parent_id = NULL``.

Users are linked to departments via ``TenantMember.department_id``
or ``KnowledgeMember.department`` (text field for backwards
compatibility).
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from etl_agent.db.base import Base
from etl_agent.models.mixins import TimestampMixin, UUIDPKMixin


class Department(UUIDPKMixin, TimestampMixin, Base):
    """A department node in a tenant's org tree."""

    __tablename__ = "departments"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "code",
            name="uq_department_tenant_code",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("departments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return (
            f"<Department(id={self.id}, name={self.name!r}, "
            f"code={self.code!r}, parent={self.parent_id})>"
        )


__all__ = ["Department"]
