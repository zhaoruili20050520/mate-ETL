"""Button-level permission management API routes.

Routes:
    GET    /permissions/codes              -- list all permission codes
    GET    /permissions/roles/{role}        -- list permissions for a role
    POST   /permissions/roles/{role}        -- assign permissions to a role
    GET    /permissions/me                   -- current user's permissions
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from etl_agent.core.dependencies import CurrentUser, get_current_user
from etl_agent.db.session import get_db
from etl_agent.models.permission import PERMISSION_CODES
from etl_agent.schemas.permission import (
    AssignPermissionsRequest,
    PermissionCodeListResponse,
    RolePermissionResponse,
    UserPermissionsResponse,
)
from etl_agent.services.permission_service import (
    assign_role_permissions,
    get_role_permissions,
)

router = APIRouter(prefix="/permissions", tags=["permissions"])


def _get_tenant_id(current_user: CurrentUser) -> uuid.UUID:
    if current_user.tenant_id is None:
        raise HTTPException(status_code=403, detail="Requires a tenant context")
    return current_user.tenant_id


def _require_perm_admin(current_user: CurrentUser) -> None:
    if current_user.role not in {"tenant_admin", "system_admin"}:
        raise HTTPException(
            status_code=403,
            detail="Tenant or system admin required to manage permissions",
        )


@router.get("/codes", response_model=PermissionCodeListResponse)
def list_codes(
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> PermissionCodeListResponse:
    """List all known permission codes in the system."""
    return PermissionCodeListResponse(codes=list(PERMISSION_CODES))


@router.get("/roles/{role_name}", response_model=list[str])
def get_role_perms(
    role_name: str,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> list[str]:
    """Get all permission codes for a role."""
    tenant_id = _get_tenant_id(current_user)
    perms = get_role_permissions(db, tenant_id, role_name)
    return sorted(perms)


@router.post("/roles/{role_name}", response_model=list[RolePermissionResponse])
def assign_perms(
    role_name: str,
    body: AssignPermissionsRequest,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> list[RolePermissionResponse]:
    """Assign a set of permission codes to a role (replaces existing)."""
    tenant_id = _get_tenant_id(current_user)
    _require_perm_admin(current_user)
    rows = assign_role_permissions(db, tenant_id, role_name, body.permission_codes)
    db.commit()
    return [
        RolePermissionResponse(
            id=r.id,
            tenant_id=r.tenant_id,
            role_name=r.role_name,
            permission_code=r.permission_code,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.get("/me", response_model=UserPermissionsResponse)
def get_my_permissions(
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> UserPermissionsResponse:
    """Get the current user's effective permissions."""
    tenant_id = _get_tenant_id(current_user)
    role = current_user.role or "viewer"
    perms = get_role_permissions(db, tenant_id, role)
    return UserPermissionsResponse(
        user_id=current_user.user_id,
        role_name=role,
        permissions=sorted(perms),
    )
