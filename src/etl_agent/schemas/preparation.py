"""Pydantic schemas for preparation endpoints.

These schemas define the request/response bodies for creating
preparations, viewing preparation details, and committing
preparations.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from etl_agent.models.preparation import DataClassification


class CreatePreparationRequest(BaseModel):
    """Request body for POST /preparations."""

    pipeline_version_id: uuid.UUID = Field(
        ...,
        description="The frozen pipeline version to create a preparation from",
    )
    data_classification: DataClassification = Field(
        default=DataClassification.internal,
        description="Sensitivity level of the data being processed",
    )
    execution_budget: dict[str, Any] | None = Field(
        default=None,
        description="Custom execution budget (max_rows, max_bytes, max_duration_seconds)",
    )
    is_production_source: bool = Field(
        default=False,
        description="Whether the source connection is a production environment",
    )
    is_multi_tenant: bool = Field(
        default=False,
        description="Whether the resource is shared across tenants",
    )


class PreparationResponse(BaseModel):
    """Response body for a single preparation."""

    id: uuid.UUID
    pipeline_version_id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID
    input_fingerprint: str
    resource_scope: dict[str, Any]
    data_classification: str
    execution_budget: dict[str, Any]
    rollback_plan: dict[str, Any]
    risk_level: str
    risk_reasoning: str
    status: str
    prepared_by: uuid.UUID | None = None
    committed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


__all__ = [
    "CreatePreparationRequest",
    "PreparationResponse",
]
