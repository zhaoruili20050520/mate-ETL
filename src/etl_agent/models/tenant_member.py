"""Tenant-member association model."""

from __future__ import annotations

import uuid

from sqlalchemy import Enum, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from etl_agent.db.base import Base
from etl_agent.models.mixins import TimestampMixin, UUIDPKMixin
from etl_agent.models.role import Role


class TenantMember(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "tenant_members"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_tenant_member"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[Role] = mapped_column(
        Enum(Role, name="tenant_role", native_enum=True),
        default=Role.tenant_admin,
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<TenantMember(tenant_id={self.tenant_id}, "
            f"user_id={self.user_id}, role={self.role.value})>"
        )


__all__ = ["TenantMember"]
