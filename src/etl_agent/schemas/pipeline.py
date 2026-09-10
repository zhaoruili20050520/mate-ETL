"""Pydantic schemas for the pipeline API endpoints.

These schemas are the **API boundary** -- request bodies and response
bodies for the pipeline generation, freezing, and listing endpoints.

The route handlers in ``api/v1/pipelines.py`` use these schemas to
validate input and shape output.  Business logic lives in the
service layer and LangGraph nodes, not here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from etl_agent.schemas.generation import (
    EtlPlan,
    ValidationIssue,
)

# =====================================================================
# Generate request / response
# =====================================================================


class GeneratePipelineRequest(BaseModel):
    """Request body for ``POST /api/v1/pipelines/generate``.

    The caller provides either a natural-language description or a
    pre-structured ``PipelineRequirement``.  If ``requirement`` is
    provided, it takes precedence over ``natural_language``.
    """

    project_id: uuid.UUID = Field(..., description="Project scope")
    source_connection_id: uuid.UUID = Field(
        ...,
        description="Source data-source connection",
    )
    target_connection_id: uuid.UUID = Field(
        ...,
        description="Target data-source connection",
    )
    natural_language: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="Natural-language description of the ETL requirement",
    )
    name: str | None = Field(
        default=None,
        max_length=255,
        description="Pipeline name (auto-generated from NL if omitted)",
    )
    # Optional structured hints that override LLM parsing
    sync_mode: str | None = Field(
        default=None,
        description="Hint: 'full' or 'incremental'",
    )
    source_table: str | None = Field(
        default=None,
        max_length=255,
        description="Hint: source table name",
    )
    target_table: str | None = Field(
        default=None,
        max_length=255,
        description="Hint: target table name",
    )
    primary_keys: list[str] = Field(
        default_factory=list,
        description="Hint: primary key columns",
    )
    watermark_column: str | None = Field(
        default=None,
        max_length=255,
        description="Hint: watermark column for incremental sync",
    )


class GeneratePipelineResponse(BaseModel):
    """Response from the generate endpoint.

    Contains the pipeline ID and its current state.  If the
    LangGraph chain determined that clarification is needed,
    ``clarification_questions`` is populated and ``status``
    is ``needs_clarification``.
    """

    pipeline_id: uuid.UUID
    status: str = Field(
        ...,
        description="Pipeline status: draft, needs_clarification, validated, error",
    )
    etl_plan: EtlPlan | None = None
    hocon_config: str | None = None
    validation_issues: list[ValidationIssue] = Field(default_factory=list)
    clarification_questions: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


# =====================================================================
# Freeze version request / response
# =====================================================================


class FreezeVersionRequest(BaseModel):
    """Request body for ``POST /api/v1/pipelines/{id}/versions``.

    Currently no additional fields are needed -- the version is
    frozen from the pipeline's current validated state.
    """

    pass


class PipelineVersionResponse(BaseModel):
    """A frozen pipeline version."""

    id: uuid.UUID
    pipeline_id: uuid.UUID
    version_number: int
    etl_plan: dict[str, Any]
    hocon_text: str
    sha256: str
    source_requirement: dict[str, Any]
    validation_results: list[dict[str, Any]]
    status: str
    frozen_by: uuid.UUID | None = None
    created_at: datetime


# =====================================================================
# Pipeline list / detail
# =====================================================================


class PipelineSummaryResponse(BaseModel):
    """Summary of a pipeline for list views."""

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    status: str
    source_connection_id: uuid.UUID
    target_connection_id: uuid.UUID
    created_by: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class PipelineDetailResponse(BaseModel):
    """Full detail of a pipeline including working state."""

    id: uuid.UUID
    name: str
    description: str | None = None
    status: str
    source_connection_id: uuid.UUID
    target_connection_id: uuid.UUID
    source_requirement: dict[str, Any] | None = None
    etl_plan: dict[str, Any] | None = None
    hocon_config: str | None = None
    validation_issues: list[dict[str, Any]] | None = None
    clarification_questions: list[dict[str, Any]] | None = None
    autofix_attempts: int = 0
    created_by: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


# =====================================================================
# Clarify (answer questions) request / response
# =====================================================================


class ClarifyRequest(BaseModel):
    """Request body for submitting clarification answers.

    The user answers the questions posed during the
    ``needs_clarification`` state, then the pipeline resumes
    from the LangGraph checkpoint.
    """

    answers: dict[str, str] = Field(
        ...,
        description="Mapping of field name to user-provided value",
    )


class ClarifyResponse(BaseModel):
    """Response after submitting clarification answers."""

    pipeline_id: uuid.UUID
    status: str
    etl_plan: EtlPlan | None = None
    hocon_config: str | None = None
    validation_issues: list[ValidationIssue] = Field(default_factory=list)
    clarification_questions: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


__all__ = [
    "ClarifyRequest",
    "ClarifyResponse",
    "FreezeVersionRequest",
    "GeneratePipelineRequest",
    "GeneratePipelineResponse",
    "PipelineDetailResponse",
    "PipelineSummaryResponse",
    "PipelineVersionResponse",
]
