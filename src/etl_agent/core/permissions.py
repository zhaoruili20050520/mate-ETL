"""Permission-checking helpers.

Every function queries the database to verify membership *for the specific
tenant or project being accessed* -- it does NOT rely on the JWT's
``tenant_id`` / ``project_id`` claims, because the caller may be operating
on a different resource than the one bound to their token.

Each function returns the resolved ``Role`` value (str) on success and
raises ``PermissionDeniedError`` or ``NotFoundError`` on failure.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from etl_agent.core.exceptions import NotFoundError, PermissionDeniedError
from etl_agent.models.project import Project
from etl_agent.models.project_member import ProjectMember
from etl_agent.models.role import Role
from etl_agent.models.tenant import Tenant
from etl_agent.models.tenant_member import TenantMember

# Roles allowed to perform administrative actions within a project.
_PROJECT_ADMIN_ROLES = {Role.project_admin, Role.tenant_admin}

# Roles allowed to perform write operations within a project
# (create connections, trigger profiling, etc.).
_PROJECT_WRITE_ROLES = {Role.engineer, Role.project_admin, Role.tenant_admin}


def assert_tenant_exists(db: Session, tenant_id: uuid.UUID) -> Tenant:
    """Return the tenant or raise ``NotFoundError``."""
    tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id))
    if tenant is None:
        raise NotFoundError(f"Tenant {tenant_id} not found")
    return tenant


def assert_tenant_member(db: Session, user_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    """Verify the user is a member of the tenant; return their role.

    Raises ``PermissionDeniedError`` if not a member.
    """
    membership = db.scalar(
        select(TenantMember).where(
            TenantMember.tenant_id == tenant_id,
            TenantMember.user_id == user_id,
        )
    )
    if membership is None:
        raise PermissionDeniedError(f"User {user_id} is not a member of tenant {tenant_id}")
    return membership.role.value


def assert_tenant_admin(db: Session, user_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    """Verify the user is a ``tenant_admin`` of the tenant; return role.

    Raises ``PermissionDeniedError`` if the user is not a member or
    does not hold the ``tenant_admin`` role.
    """
    role = assert_tenant_member(db, user_id, tenant_id)
    if role != Role.tenant_admin.value:
        raise PermissionDeniedError(f"User {user_id} is not a tenant_admin of tenant {tenant_id}")
    return role


def assert_project_exists(db: Session, project_id: uuid.UUID) -> Project:
    """Return the project or raise ``NotFoundError``."""
    project = db.scalar(select(Project).where(Project.id == project_id))
    if project is None:
        raise NotFoundError(f"Project {project_id} not found")
    return project


def assert_project_member(db: Session, user_id: uuid.UUID, project_id: uuid.UUID) -> str:
    """Verify the user is a member of the project; return their role.

    Also implicitly verifies the user is a tenant member, because
    project membership requires tenant membership first.
    """
    project = assert_project_exists(db, project_id)

    # Must also be a tenant member of the project's tenant.
    assert_tenant_member(db, user_id, project.tenant_id)

    membership = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    )
    if membership is None:
        raise PermissionDeniedError(f"User {user_id} is not a member of project {project_id}")
    return membership.role.value


def assert_project_write(db: Session, user_id: uuid.UUID, project_id: uuid.UUID) -> str:
    """Verify the user can perform write operations in the project.

    Allowed roles: ``engineer``, ``project_admin``, ``tenant_admin``.
    """
    role = assert_project_member(db, user_id, project_id)
    if Role(role) not in _PROJECT_WRITE_ROLES:
        raise PermissionDeniedError(
            f"User {user_id} does not have write access to project {project_id}"
        )
    return role


def assert_project_admin(db: Session, user_id: uuid.UUID, project_id: uuid.UUID) -> str:
    """Verify the user can perform admin operations in the project.

    Allowed roles: ``project_admin``, ``tenant_admin``.
    """
    role = assert_project_member(db, user_id, project_id)
    if Role(role) not in _PROJECT_ADMIN_ROLES:
        raise PermissionDeniedError(f"User {user_id} is not an admin of project {project_id}")
    return role


__all__ = [
    "assert_project_admin",
    "assert_project_exists",
    "assert_project_member",
    "assert_project_write",
    "assert_tenant_admin",
    "assert_tenant_exists",
    "assert_tenant_member",
]
