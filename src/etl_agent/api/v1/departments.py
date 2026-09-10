"""Department CRUD API routes.

Routes:
    POST   /departments          -- create a department
    GET    /departments          -- list all (flat)
    GET    /departments/tree      -- tree structure
    PUT    /departments/{id}     -- update
    DELETE /departments/{id}     -- delete (re-parents children)
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from etl_agent.core.dependencies import CurrentUser, get_current_user
from etl_agent.db.session import get_db
from etl_agent.models.department import Department
from etl_agent.schemas.department import (
    DepartmentCreateRequest,
    DepartmentResponse,
    DepartmentTreeNode,
    DepartmentUpdateRequest,
)
from etl_agent.services.department_service import (
    create_department,
    delete_department,
    get_department_tree,
    list_departments,
    update_department,
)

router = APIRouter(prefix="/departments", tags=["departments"])


def _dept_to_response(d: Department) -> DepartmentResponse:
    return DepartmentResponse(
        id=d.id,
        tenant_id=d.tenant_id,
        parent_id=d.parent_id,
        name=d.name,
        code=d.code,
        sort_order=d.sort_order,
        created_at=d.created_at,
        updated_at=d.updated_at,
    )


def _get_tenant_id(current_user: CurrentUser) -> uuid.UUID:
    if current_user.tenant_id is None:
        raise HTTPException(status_code=403, detail="Requires a tenant context")
    return current_user.tenant_id


def _require_admin(current_user: CurrentUser) -> None:
    if current_user.role not in {"tenant_admin", "system_admin", "knowledge_admin"}:
        raise HTTPException(
            status_code=403,
            detail="Admin role required to manage departments",
        )


@router.post("", response_model=DepartmentResponse, status_code=201)
def create_dept(
    body: DepartmentCreateRequest,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> DepartmentResponse:
    """Create a new department node."""
    tenant_id = _get_tenant_id(current_user)
    _require_admin(current_user)
    try:
        dept = create_department(
            db,
            tenant_id=tenant_id,
            name=body.name,
            code=body.code,
            parent_id=body.parent_id,
            sort_order=body.sort_order,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return _dept_to_response(dept)


@router.get("", response_model=list[DepartmentResponse])
def list_depts(
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> list[DepartmentResponse]:
    """List all departments (flat)."""
    tenant_id = _get_tenant_id(current_user)
    depts = list_departments(db, tenant_id)
    return [_dept_to_response(d) for d in depts]


@router.get("/tree", response_model=list[DepartmentTreeNode])
def get_tree(
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> list[DepartmentTreeNode]:
    """Get the department tree."""
    tenant_id = _get_tenant_id(current_user)
    return get_department_tree(db, tenant_id)


@router.put("/{dept_id}", response_model=DepartmentResponse)
def update_dept(
    dept_id: uuid.UUID,
    body: DepartmentUpdateRequest,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> DepartmentResponse:
    """Update a department."""
    tenant_id = _get_tenant_id(current_user)
    _require_admin(current_user)
    try:
        dept = update_department(
            db,
            dept_id=dept_id,
            tenant_id=tenant_id,
            name=body.name,
            code=body.code,
            parent_id=body.parent_id,
            sort_order=body.sort_order,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if dept is None:
        raise HTTPException(status_code=404, detail="Department not found")
    db.commit()
    return _dept_to_response(dept)


@router.delete("/{dept_id}", status_code=204)
def delete_dept(
    dept_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> None:
    """Delete a department. Children are re-parented to the deleted node's parent."""
    tenant_id = _get_tenant_id(current_user)
    _require_admin(current_user)
    deleted = delete_department(db, dept_id, tenant_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Department not found")
    db.commit()
