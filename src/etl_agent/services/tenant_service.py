"""Tenant management service: CRUD and member administration.

All functions operate within explicit tenant boundaries.  Member
management requires ``tenant_admin`` role, verified via
``core.permissions``.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import case, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from etl_agent.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from etl_agent.core.permissions import (
    assert_tenant_admin,
    assert_tenant_exists,
    assert_tenant_member,
)
from etl_agent.models.role import Role
from etl_agent.models.tenant import Tenant, TenantStatus
from etl_agent.models.tenant_member import TenantMember
from etl_agent.models.user import User

logger = logging.getLogger(__name__)


# =====================================================================
# Tenant CRUD
# =====================================================================


def create_tenant(db: Session, *, user_id: uuid.UUID, name: str) -> Tenant:
    """Create a new tenant.

    The caller must be a ``tenant_admin`` of *any* tenant (i.e. they
    already have administrative privileges).  The creator is
    automatically added as a ``tenant_admin`` of the new tenant.
    """
    # Verify the caller is a tenant_admin somewhere (at least one tenant).
    existing_memberships = db.scalars(
        select(TenantMember).where(TenantMember.user_id == user_id)
    ).all()
    is_admin = any(m.role == Role.tenant_admin for m in existing_memberships)
    if not is_admin:
        raise PermissionDeniedError("Only an existing tenant_admin can create new tenants")

    tenant = Tenant(name=name, status=TenantStatus.active)
    db.add(tenant)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ConflictError(f"Tenant '{name}' already exists") from None

    # Auto-enroll creator as tenant_admin of the new tenant.
    membership = TenantMember(
        tenant_id=tenant.id,
        user_id=user_id,
        role=Role.tenant_admin,
    )
    db.add(membership)
    db.commit()
    db.refresh(tenant)
    logger.info(
        "Created tenant %s (id=%s), creator=%s enrolled as tenant_admin",
        tenant.name,
        tenant.id,
        user_id,
    )
    return tenant


def get_tenant(db: Session, *, user_id: uuid.UUID, tenant_id: uuid.UUID) -> Tenant:
    """Return a tenant after verifying the user has access to it."""
    tenant = assert_tenant_exists(db, tenant_id)
    assert_tenant_member(db, user_id, tenant_id)
    return tenant


def list_tenants_for_user(db: Session, *, user_id: uuid.UUID) -> list[Tenant]:
    """Return all tenants the user is a member of."""
    rows = (
        db.execute(
            select(Tenant)
            .join(TenantMember, TenantMember.tenant_id == Tenant.id)
            .where(TenantMember.user_id == user_id)
            .order_by(
                case((Tenant.name == "default", 0), else_=1),
                Tenant.created_at.asc(),
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


# =====================================================================
# Tenant member management
# =====================================================================


def add_tenant_member(
    db: Session,
    *,
    actor_id: uuid.UUID,
    tenant_id: uuid.UUID,
    username: str,
    role: Role,
) -> TenantMember:
    """Add a user to a tenant with the specified role.

    Requires ``tenant_admin`` privileges.  The target user must already
    exist in the system (registered via ``POST /auth/register``).
    """
    assert_tenant_admin(db, actor_id, tenant_id)

    target_user = db.scalar(select(User).where(User.username == username))
    if target_user is None:
        raise NotFoundError(f"User '{username}' not found")

    existing = db.scalar(
        select(TenantMember).where(
            TenantMember.tenant_id == tenant_id,
            TenantMember.user_id == target_user.id,
        )
    )
    if existing is not None:
        raise ConflictError(f"User '{username}' is already a member of this tenant")

    membership = TenantMember(
        tenant_id=tenant_id,
        user_id=target_user.id,
        role=role,
    )
    db.add(membership)
    db.commit()
    db.refresh(membership)
    logger.info(
        "Added user %s to tenant %s as %s (by %s)",
        username,
        tenant_id,
        role.value,
        actor_id,
    )
    return membership


def list_tenant_members(
    db: Session, *, actor_id: uuid.UUID, tenant_id: uuid.UUID
) -> list[tuple[TenantMember, str]]:
    """List all members of a tenant with their usernames.

    Requires any tenant membership (any role can list members).
    """
    assert_tenant_member(db, actor_id, tenant_id)

    rows = db.execute(
        select(TenantMember, User.username)
        .join(User, User.id == TenantMember.user_id)
        .where(TenantMember.tenant_id == tenant_id)
        .order_by(TenantMember.created_at)
    ).all()
    return [(member, username) for member, username in rows]


def remove_tenant_member(
    db: Session,
    *,
    actor_id: uuid.UUID,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Remove a user from a tenant.

    Requires ``tenant_admin``.  Cannot remove the last remaining
    ``tenant_admin`` (would orphan the tenant).
    """
    assert_tenant_admin(db, actor_id, tenant_id)

    membership = db.scalar(
        select(TenantMember).where(
            TenantMember.tenant_id == tenant_id,
            TenantMember.user_id == user_id,
        )
    )
    if membership is None:
        raise NotFoundError(f"User {user_id} is not a member of tenant {tenant_id}")

    # Guard: prevent removing the last tenant_admin.
    if membership.role == Role.tenant_admin:
        from sqlalchemy import func

        admin_count = db.scalar(
            select(func.count())
            .select_from(TenantMember)
            .where(
                TenantMember.tenant_id == tenant_id,
                TenantMember.role == Role.tenant_admin,
            )
        )
        if admin_count is not None and admin_count <= 1:
            raise PermissionDeniedError("Cannot remove the last tenant_admin of a tenant")

    # Cascade: remove from all projects within this tenant.
    from sqlalchemy import delete as sa_delete

    from etl_agent.models.project import Project
    from etl_agent.models.project_member import ProjectMember

    project_ids = db.scalars(select(Project.id).where(Project.tenant_id == tenant_id)).all()

    if project_ids:
        db.execute(
            sa_delete(ProjectMember).where(
                ProjectMember.project_id.in_(project_ids),
                ProjectMember.user_id == user_id,
            )
        )

    db.delete(membership)
    db.commit()
    logger.info(
        "Removed user %s from tenant %s (by %s)",
        user_id,
        tenant_id,
        actor_id,
    )


__all__ = [
    "add_tenant_member",
    "create_tenant",
    "get_tenant",
    "list_tenant_members",
    "list_tenants_for_user",
    "remove_tenant_member",
]
