"""Button-level permission service layer.

Manages role-to-permission-code mappings and provides a
``get_user_permissions`` helper that merges database overrides
with the built-in defaults.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from etl_agent.models.permission import (
    RolePermission,
    get_default_permissions,
)


def get_role_permissions(
    db: Session,
    tenant_id: uuid.UUID,
    role_name: str,
) -> set[str]:
    """Return the effective permission codes for a role.

    If the role has explicit ``RolePermission`` rows in the database,
    those are used.  Otherwise the built-in defaults apply.
    """
    rows = list(
        db.scalars(
            select(RolePermission).where(
                RolePermission.tenant_id == tenant_id,
                RolePermission.role_name == role_name,
            ),
        ).all(),
    )
    if rows:
        return {r.permission_code for r in rows}
    return set(get_default_permissions(role_name))


def assign_role_permissions(
    db: Session,
    tenant_id: uuid.UUID,
    role_name: str,
    permission_codes: list[str],
) -> list[RolePermission]:
    """Replace the permission set for a role (delete-then-insert)."""
    existing = list(
        db.scalars(
            select(RolePermission).where(
                RolePermission.tenant_id == tenant_id,
                RolePermission.role_name == role_name,
            ),
        ).all(),
    )
    for row in existing:
        db.delete(row)
    db.flush()
    created: list[RolePermission] = []
    for code in permission_codes:
        rp = RolePermission(
            tenant_id=tenant_id,
            role_name=role_name,
            permission_code=code,
        )
        db.add(rp)
        created.append(rp)
    db.flush()
    return created


def get_user_permissions(
    db: Session,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    role_name: str,
) -> set[str]:
    """Get all permission codes effective for a user.

    Delegates to ``get_role_permissions`` using the user's role.
    """
    return get_role_permissions(db, tenant_id, role_name)


def has_permission(
    db: Session,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    role_name: str,
    code: str,
) -> bool:
    """Check whether a user has a specific permission code."""
    return code in get_user_permissions(db, tenant_id, user_id, role_name)


def list_role_permission_rows(
    db: Session,
    tenant_id: uuid.UUID,
    role_name: str | None = None,
) -> list[RolePermission]:
    """List role-permission rows, optionally filtered by role."""
    stmt = (
        select(RolePermission)
        .where(RolePermission.tenant_id == tenant_id)
        .order_by(RolePermission.role_name, RolePermission.permission_code)
    )
    if role_name is not None:
        stmt = stmt.where(RolePermission.role_name == role_name)
    return list(db.scalars(stmt).all())


__all__ = [
    "assign_role_permissions",
    "get_role_permissions",
    "get_user_permissions",
    "has_permission",
    "list_role_permission_rows",
]
