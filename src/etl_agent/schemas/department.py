"""Pydantic schemas for department management."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class DepartmentCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=1, max_length=64)
    parent_id: uuid.UUID | None = Field(default=None)
    sort_order: int = Field(default=0, ge=0)


class DepartmentUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    code: str | None = Field(default=None, min_length=1, max_length=64)
    parent_id: uuid.UUID | None = Field(default=None)
    sort_order: int | None = Field(default=None, ge=0)


class DepartmentResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    code: str
    sort_order: int
    created_at: datetime
    updated_at: datetime


class DepartmentTreeNode(BaseModel):
    """A department with its children for tree display."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    code: str
    sort_order: int
    children: list[DepartmentTreeNode] = Field(default_factory=list)


__all__ = [
    "DepartmentCreateRequest",
    "DepartmentResponse",
    "DepartmentTreeNode",
    "DepartmentUpdateRequest",
]
