"""Pydantic schemas for metadata exploration."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ExploreRequest(BaseModel):
    """Request body for triggering a metadata exploration."""

    project_id: uuid.UUID = Field(..., description="Project the connection belongs to")
    max_tables: int = Field(default=100, ge=1, le=500, description="Maximum tables to profile")
    max_sample_rows: int = Field(
        default=50, ge=1, le=200, description="Maximum sample rows per table"
    )
    max_execution_seconds: int = Field(
        default=120,
        ge=10,
        le=600,
        description="Maximum total exploration time in seconds",
    )


class ExploreResponse(BaseModel):
    """Response returned immediately after triggering exploration."""

    task_id: uuid.UUID
    connection_id: uuid.UUID
    status: str
    message: str


class ExplorationTaskResponse(BaseModel):
    """Full status of an exploration task."""

    id: uuid.UUID
    connection_id: uuid.UUID
    status: str
    error_message: str | None = None
    table_count: int | None = None
    total_rows: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime


# =====================================================================
# Metadata profile response
# =====================================================================


class MetadataProfileResponse(BaseModel):
    """A single table's profiling result."""

    id: uuid.UUID
    connection_id: uuid.UUID
    schema_name: str
    table_name: str
    columns_info: list[dict[str, Any]] | None = None
    stats_info: dict[str, Any] | None = None
    value_distribution: list[dict[str, Any]] | None = None
    sample_data: list[dict[str, Any]] | None = None
    profiled_at: datetime


__all__ = [
    "ExploreRequest",
    "ExploreResponse",
    "ExplorationTaskResponse",
    "MetadataProfileResponse",
]
