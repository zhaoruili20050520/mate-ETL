"""Button-level permission model and permission-code constants.

The system uses a code-based permission scheme: every actionable
operation (e.g. creating a document, publishing a FAQ) is
identified by a dotted string code such as ``kb:document:create``.

``RolePermission`` rows map a role name (tenant-level or knowledge-
level) to a set of permission codes within a tenant.  The
``DEFAULT_ROLE_PERMISSIONS`` constant provides sensible defaults
that are seeded on first use.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from etl_agent.db.base import Base
from etl_agent.models.mixins import TimestampMixin, UUIDPKMixin

# ------------------------------------------------------------------ #
# Permission-code catalog
# ------------------------------------------------------------------ #

PERMISSION_CODES: tuple[str, ...] = (
    # Knowledge document operations
    "kb:document:create",
    "kb:document:read",
    "kb:document:delete",
    "kb:document:publish",
    "kb:document:archive",
    # Knowledge chunk operations
    "kb:chunk:update",
    "kb:chunk:delete",
    # Q&A
    "kb:ask",
    # FAQ operations
    "kb:faq:cluster",
    "kb:faq:publish",
    "kb:faq:reject",
    "kb:faq:delete",
    # Knowledge gap operations
    "kb:gap:resolve",
    "kb:gap:delete",
    # Audit operations
    "kb:audit:read",
    "kb:audit:delete",
    # Metrics
    "kb:metrics:read",
    # Member management
    "kb:member:assign",
    "kb:member:read",
    # Department management
    "dept:manage",
    # Permission management
    "perm:manage",
    # Batch import
    "kb:document:batch",
)

# ------------------------------------------------------------------ #
# Default role -> permission-code mapping
# ------------------------------------------------------------------ #

_ALL_PERMISSIONS: frozenset[str] = frozenset(PERMISSION_CODES)

_VIEWER_PERMISSIONS: frozenset[str] = frozenset(
    {
        "kb:document:read",
        "kb:ask",
        "kb:metrics:read",
    },
)

_KB_ADMIN_PERMISSIONS: frozenset[str] = frozenset(
    {
        "kb:document:create",
        "kb:document:read",
        "kb:document:delete",
        "kb:document:publish",
        "kb:document:archive",
        "kb:document:batch",
        "kb:chunk:update",
        "kb:chunk:delete",
        "kb:ask",
        "kb:faq:cluster",
        "kb:faq:publish",
        "kb:faq:reject",
        "kb:faq:delete",
        "kb:gap:resolve",
        "kb:gap:delete",
        "kb:audit:read",
        "kb:audit:delete",
        "kb:metrics:read",
        "kb:member:assign",
        "kb:member:read",
        "dept:manage",
    },
)

DEFAULT_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "tenant_admin": _ALL_PERMISSIONS,
    "system_admin": _ALL_PERMISSIONS,
    "knowledge_admin": _KB_ADMIN_PERMISSIONS,
    "viewer": _VIEWER_PERMISSIONS,
    "project_admin": _ALL_PERMISSIONS,
    "engineer": _VIEWER_PERMISSIONS,
    "auditor": _VIEWER_PERMISSIONS,
}


class RolePermission(UUIDPKMixin, TimestampMixin, Base):
    """Maps a role name to a permission code within a tenant."""

    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "role_name",
            "permission_code",
            name="uq_role_permission_code",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    permission_code: Mapped[str] = mapped_column(String(128), nullable=False)

    def __repr__(self) -> str:
        return f"<RolePermission(role={self.role_name!r}, code={self.permission_code!r})>"


def get_default_permissions(role_name: str) -> frozenset[str]:
    """Return the default permission codes for a role."""
    return DEFAULT_ROLE_PERMISSIONS.get(
        role_name,
        _VIEWER_PERMISSIONS,
    )


def all_permission_codes() -> Sequence[str]:
    """Return all known permission codes in canonical order."""
    return PERMISSION_CODES


__all__ = [
    "DEFAULT_ROLE_PERMISSIONS",
    "PERMISSION_CODES",
    "RolePermission",
    "all_permission_codes",
    "get_default_permissions",
]
