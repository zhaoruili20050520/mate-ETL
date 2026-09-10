"""Department CRUD service layer.

Provides tree-structured department management: create, update,
delete, list (flat or tree), and move (change parent).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from etl_agent.models.department import Department
from etl_agent.schemas.department import DepartmentTreeNode


def create_department(
    db: Session,
    tenant_id: uuid.UUID,
    name: str,
    code: str,
    parent_id: uuid.UUID | None = None,
    sort_order: int = 0,
) -> Department:
    """Create a new department node under an optional parent."""
    if parent_id is not None:
        parent = db.scalar(
            select(Department).where(
                Department.id == parent_id,
                Department.tenant_id == tenant_id,
            ),
        )
        if parent is None:
            raise ValueError("Parent department not found")
    dept = Department(
        tenant_id=tenant_id,
        parent_id=parent_id,
        name=name,
        code=code,
        sort_order=sort_order,
    )
    db.add(dept)
    db.flush()
    return dept


def update_department(
    db: Session,
    dept_id: uuid.UUID,
    tenant_id: uuid.UUID,
    name: str | None = None,
    code: str | None = None,
    parent_id: uuid.UUID | None = None,
    sort_order: int | None = None,
) -> Department | None:
    """Update a department's fields."""
    dept = db.scalar(
        select(Department).where(
            Department.id == dept_id,
            Department.tenant_id == tenant_id,
        ),
    )
    if dept is None:
        return None
    if parent_id is not None and parent_id != dept.parent_id:
        if str(parent_id) == str(dept_id):
            raise ValueError("Cannot set department as its own parent")
        if parent_id is not None:
            child = db.scalar(
                select(Department).where(
                    Department.id == parent_id,
                    Department.tenant_id == tenant_id,
                ),
            )
            if child is None:
                raise ValueError("New parent department not found")
            if _is_descendant(db, tenant_id, parent_id, dept_id):
                raise ValueError("Cannot move a department under its own descendant")
        dept.parent_id = parent_id
    if name is not None:
        dept.name = name
    if code is not None:
        dept.code = code
    if sort_order is not None:
        dept.sort_order = sort_order
    db.flush()
    return dept


def delete_department(db: Session, dept_id: uuid.UUID, tenant_id: uuid.UUID) -> bool:
    """Delete a department. Children are re-parented to the deleted node's parent."""
    dept = db.scalar(
        select(Department).where(
            Department.id == dept_id,
            Department.tenant_id == tenant_id,
        ),
    )
    if dept is None:
        return False
    children = list(
        db.scalars(
            select(Department).where(
                Department.parent_id == dept_id,
                Department.tenant_id == tenant_id,
            ),
        ).all(),
    )
    for child in children:
        child.parent_id = dept.parent_id
    db.flush()
    db.delete(dept)
    db.flush()
    return True


def list_departments(
    db: Session,
    tenant_id: uuid.UUID,
) -> list[Department]:
    """List all departments in a tenant, ordered by sort_order."""
    return list(
        db.scalars(
            select(Department)
            .where(Department.tenant_id == tenant_id)
            .order_by(Department.sort_order, Department.name),
        ).all(),
    )


def get_department_tree(
    db: Session,
    tenant_id: uuid.UUID,
) -> list[DepartmentTreeNode]:
    """Build a tree of departments from the flat list."""
    all_depts = list_departments(db, tenant_id)
    dept_map: dict[uuid.UUID, DepartmentTreeNode] = {}
    for d in all_depts:
        dept_map[d.id] = DepartmentTreeNode(
            id=d.id,
            tenant_id=d.tenant_id,
            parent_id=d.parent_id,
            name=d.name,
            code=d.code,
            sort_order=d.sort_order,
            children=[],
        )
    roots: list[DepartmentTreeNode] = []
    for d in all_depts:
        node = dept_map[d.id]
        if d.parent_id is not None and d.parent_id in dept_map:
            dept_map[d.parent_id].children.append(node)
        else:
            roots.append(node)
    return roots


def _is_descendant(
    db: Session,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    ancestor_id: uuid.UUID,
) -> bool:
    """Check if ``candidate_id`` is a descendant of ``ancestor_id``."""
    visited: set[uuid.UUID] = set()
    queue: list[uuid.UUID] = [ancestor_id]
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        children = list(
            db.scalars(
                select(Department).where(
                    Department.parent_id == current,
                    Department.tenant_id == tenant_id,
                ),
            ).all(),
        )
        for child in children:
            if child.id == candidate_id:
                return True
            queue.append(child.id)
    return False


__all__ = [
    "create_department",
    "delete_department",
    "get_department_tree",
    "list_departments",
    "update_department",
]
