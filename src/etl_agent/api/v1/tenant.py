"""Tenant management routes.

All routes require authentication.  Member-level access (any role)
suffices for listing/viewing; ``tenant_admin`` is required for
creating tenants, adding/removing members, and creating new projects
within a tenant.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from etl_agent.core.dependencies import CurrentUser, get_current_user
from etl_agent.core.exceptions import (
    ConflictError,
    DomainError,
    NotFoundError,
    PermissionDeniedError,
)
from etl_agent.db.session import get_db
from etl_agent.models.user import User
from etl_agent.schemas.common import PageResponse
from etl_agent.schemas.tenant import (
    AddTenantMemberRequest,
    TenantCreate,
    TenantMemberResponse,
    TenantResponse,
)
from etl_agent.services import tenant_service

router = APIRouter(prefix="/tenants", tags=["tenants"])


def _to_domain_error(exc: DomainError) -> HTTPException:
    """Convert a DomainError to the equivalent HTTPException."""
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


# =====================================================================
# Tenant CRUD
# =====================================================================


@router.post(
    "",
    response_model=TenantResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_tenant(
    req: TenantCreate,
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> TenantResponse:
    """Create a new tenant (requires existing tenant_admin role)."""
    try:
        tenant = tenant_service.create_tenant(db, user_id=current.user_id, name=req.name)
    except (PermissionDeniedError, ConflictError) as exc:
        raise _to_domain_error(exc) from exc
    return TenantResponse(
        id=tenant.id,
        name=tenant.name,
        status=tenant.status.value,
        created_at=tenant.created_at,
    )


@router.get("", response_model=PageResponse[TenantResponse])
def list_tenants(
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),  # noqa: B008
    page_size: int = Query(default=50, ge=1, le=500, description="Items per page"),  # noqa: B008
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> PageResponse[TenantResponse]:
    """List tenants the current user is a member of (paginated)."""
    tenants = tenant_service.list_tenants_for_user(db, user_id=current.user_id)
    total = len(tenants)
    start = (page - 1) * page_size
    paged = tenants[start : start + page_size]
    return PageResponse(
        items=[
            TenantResponse(id=t.id, name=t.name, status=t.status.value, created_at=t.created_at)
            for t in paged
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{tenant_id}", response_model=TenantResponse)
def get_tenant(
    tenant_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> TenantResponse:
    """Get details of a tenant the user has access to."""
    try:
        tenant = tenant_service.get_tenant(db, user_id=current.user_id, tenant_id=tenant_id)
    except (NotFoundError, PermissionDeniedError) as exc:
        raise _to_domain_error(exc) from exc
    return TenantResponse(
        id=tenant.id,
        name=tenant.name,
        status=tenant.status.value,
        created_at=tenant.created_at,
    )


# =====================================================================
# Tenant member management
# =====================================================================


@router.post(
    "/{tenant_id}/members",
    response_model=TenantMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_tenant_member(
    tenant_id: uuid.UUID,
    req: AddTenantMemberRequest,
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> TenantMemberResponse:
    """Add a user to a tenant (requires tenant_admin)."""
    try:
        membership = tenant_service.add_tenant_member(
            db,
            actor_id=current.user_id,
            tenant_id=tenant_id,
            username=req.username,
            role=req.role,
        )
    except (NotFoundError, PermissionDeniedError, ConflictError) as exc:
        raise _to_domain_error(exc) from exc

    user = db.get(User, membership.user_id)
    return TenantMemberResponse(
        id=membership.id,
        tenant_id=membership.tenant_id,
        user_id=membership.user_id,
        username=user.username if user else "",
        role=membership.role.value,
        created_at=membership.created_at,
    )


@router.get("/{tenant_id}/members", response_model=PageResponse[TenantMemberResponse])
def list_tenant_members(
    tenant_id: uuid.UUID,
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),  # noqa: B008
    page_size: int = Query(default=50, ge=1, le=500, description="Items per page"),  # noqa: B008
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> PageResponse[TenantMemberResponse]:
    """List all members of a tenant (paginated)."""
    try:
        members = tenant_service.list_tenant_members(
            db, actor_id=current.user_id, tenant_id=tenant_id
        )
    except (NotFoundError, PermissionDeniedError) as exc:
        raise _to_domain_error(exc) from exc

    total = len(members)
    start = (page - 1) * page_size
    paged = members[start : start + page_size]
    return PageResponse(
        items=[
            TenantMemberResponse(
                id=m.id,
                tenant_id=m.tenant_id,
                user_id=m.user_id,
                username=username,
                role=m.role.value,
                created_at=m.created_at,
            )
            for m, username in paged
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.delete("/{tenant_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_tenant_member(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> None:
    """Remove a user from a tenant (requires tenant_admin)."""
    try:
        tenant_service.remove_tenant_member(
            db,
            actor_id=current.user_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
    except (NotFoundError, PermissionDeniedError) as exc:
        raise _to_domain_error(exc) from exc
