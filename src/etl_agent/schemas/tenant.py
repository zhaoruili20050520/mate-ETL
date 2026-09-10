"""Pydantic schemas for tenant, project, connection, and profile resources."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from etl_agent.models.data_source_connection import DataSourceType
from etl_agent.models.role import Role

# =====================================================================
# Tenant
# =====================================================================


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class TenantResponse(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    created_at: datetime


# =====================================================================
# Tenant member
# =====================================================================


class AddTenantMemberRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    role: Role = Field(default=Role.engineer)


class TenantMemberResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    username: str
    role: str
    created_at: datetime


# =====================================================================
# Project
# =====================================================================


class ProjectCreate(BaseModel):
    tenant_id: uuid.UUID
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)


class ProjectUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)


class ProjectResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    status: str
    tenant_id: uuid.UUID
    created_at: datetime


# =====================================================================
# Project member
# =====================================================================


class AddProjectMemberRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    role: Role = Field(default=Role.engineer)


class ProjectMemberResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID
    username: str
    role: str
    created_at: datetime


# =====================================================================
# Data source connection (project-scoped)
# =====================================================================


class DataSourceConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    source_type: DataSourceType
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    database_name: str = Field(min_length=1, max_length=255)
    username: str = Field(min_length=1, max_length=255, description="Database username")
    password: str = Field(
        min_length=1,
        max_length=255,
        description="Database password (written to Vault, never stored in DB)",
    )
    description: str | None = Field(default=None, max_length=2000)


class DataSourceConnectionResponse(BaseModel):
    """Connection response sent to the frontend.

    - ``password`` is always ``None`` (never returned).
    - ``secret_ref`` is NOT exposed -- it's an internal Vault path.
    - ``username`` comes from the DB column (not the Vault secret).
    - ``updated_at`` is included for change tracking.
    """

    id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID
    name: str
    source_type: str
    host: str
    port: int
    database_name: str
    username: str
    password: str | None = None
    description: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime | None = None
    # Connection test state (persisted in DB)
    last_test_status: str = "untested"
    last_tested_at: datetime | None = None
    last_test_message: str | None = None
    last_test_latency_ms: int | None = None


class ConnectionCreateRequest(BaseModel):
    """Flat-route create request: includes project_id for isolation."""

    project_id: uuid.UUID
    name: str = Field(min_length=1, max_length=128)
    source_type: DataSourceType
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    database_name: str = Field(min_length=1, max_length=255)
    username: str = Field(min_length=1, max_length=255, description="Database username")
    password: str = Field(
        min_length=1,
        max_length=255,
        description="Database password (written to Vault, never stored in DB)",
    )
    description: str | None = Field(default=None, max_length=2000)


class ConnectionUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    source_type: DataSourceType | None = None
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    database_name: str | None = Field(default=None, min_length=1, max_length=255)
    username: str | None = Field(default=None, min_length=1, max_length=255)
    password: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class ConnectionTestResponse(BaseModel):
    success: bool
    message: str
    latency_ms: int | None = None
    status: str = "untested"


# NOTE: MetadataProfileResponse is defined in schemas/metadata.py
# and imported by both project.py and connections.py.
# There is no duplicate definition here.


__all__ = [
    "AddProjectMemberRequest",
    "AddTenantMemberRequest",
    "ConnectionCreateRequest",
    "ConnectionUpdateRequest",
    "ConnectionTestResponse",
    "DataSourceConnectionCreate",
    "DataSourceConnectionResponse",
    "ProjectCreate",
    "ProjectUpdateRequest",
    "ProjectMemberResponse",
    "ProjectResponse",
    "TenantCreate",
    "TenantMemberResponse",
    "TenantResponse",
]
