"""Tenant model."""

from __future__ import annotations

import enum

from sqlalchemy import Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from etl_agent.db.base import Base
from etl_agent.models.mixins import TimestampMixin, UUIDPKMixin


class TenantStatus(enum.StrEnum):
    active = "active"
    suspended = "suspended"
    terminated = "terminated"


class Tenant(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    status: Mapped[TenantStatus] = mapped_column(
        Enum(TenantStatus, name="tenant_status", native_enum=True),
        default=TenantStatus.active,
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Tenant(id={self.id}, name={self.name!r}, status={self.status.value})>"


__all__ = ["Tenant", "TenantStatus"]
