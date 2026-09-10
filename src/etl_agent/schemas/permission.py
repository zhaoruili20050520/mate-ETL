"""Pydantic schemas for button-level permission management."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from etl_agent.models.permission import PERMISSION_CODES


class RolePermissionResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    role_name: str
    permission_code: str
    created_at: datetime


class AssignPermissionsRequest(BaseModel):
    """Assign a set of permission codes to a role."""

    role_name: str = Field(min_length=1, max_length=64)
    permission_codes: list[str] = Field(default_factory=list)


class UserPermissionsResponse(BaseModel):
    """All permission codes effective for a specific user."""

    user_id: uuid.UUID
    role_name: str
    permissions: list[str]


class PermissionCodeListResponse(BaseModel):
    """All known permission codes in the system."""

    codes: list[str] = Field(default_factory=lambda: list(PERMISSION_CODES))


__all__ = [
    "AssignPermissionsRequest",
    "PermissionCodeListResponse",
    "RolePermissionResponse",
    "UserPermissionsResponse",
]
