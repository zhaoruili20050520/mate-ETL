"""Pydantic schemas for commit and execution-run endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class CommitResponse(BaseModel):
    """Response returned by POST /preparations/{id}/commit."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    preparation_id: uuid.UUID
    status: str
    capability_id: str
    committed_by: uuid.UUID | None
    committed_at: datetime
    seatunnel_job_id: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None


class ExecutionRunResponse(BaseModel):
    """Response for GET /execution-runs/{id}."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    preparation_id: uuid.UUID
    status: str
    capability_id: str
    committed_by: uuid.UUID | None
    committed_at: datetime
    seatunnel_job_id: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None


# =====================================================================
# Monitoring schemas (Step 6.11)
# =====================================================================


class MetricSnapshotResponse(BaseModel):
    """A single metrics sample for an execution run."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    read_rows: int
    written_rows: int
    error_rows: int
    bytes_read: int
    bytes_written: int
    duration_seconds: float
    output_amplification: float
    error_rejection_rate: float
    sampled_at: datetime


class MetricsListResponse(BaseModel):
    """Paginated metrics history for a run."""

    items: list[MetricSnapshotResponse]
    total: int
    latest: MetricSnapshotResponse | None = None


class QualityReportResponse(BaseModel):
    """Quality report for GET /execution-runs/{id}/quality-report."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    execution_run_id: uuid.UUID
    read_rows: int
    shadow_rows: int
    error_rows: int
    error_rate: float
    max_error_ratio: float
    status: str
    rule_results: list[dict[str, Any]]
    created_at: datetime


class PublishAttemptResponse(BaseModel):
    """Publish attempt details."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    execution_run_id: uuid.UUID
    strategy: str
    target_table: str
    shadow_table: str
    backup_table: str | None = None
    status: str
    error_message: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class ExecutionEventResponse(BaseModel):
    """A single execution event for the event log."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    execution_run_id: uuid.UUID
    event_type: str
    severity: str
    message: str
    details: dict[str, Any] | None = None
    occurred_at: datetime


class CancelRunRequest(BaseModel):
    """Request body for POST /execution-runs/{id}/cancel."""

    reason: str | None = None


class RollbackRunRequest(BaseModel):
    """Request body for POST /execution-runs/{id}/rollback."""

    reason: str | None = None


class CancelRunResponse(BaseModel):
    """Response for POST /execution-runs/{id}/cancel."""

    run_id: uuid.UUID
    status: str
    seatunnel_job_id: int | None = None
    cancelled: bool


class RollbackRunResponse(BaseModel):
    """Response for POST /execution-runs/{id}/rollback."""

    run_id: uuid.UUID
    status: str
    rollback_result: dict[str, Any]


class RunDetailResponse(ExecutionRunResponse):
    """Extended run response with quality report and publish attempt."""

    quality_report: QualityReportResponse | None = None
    publish_attempt: PublishAttemptResponse | None = None


__all__ = [
    "CancelRunRequest",
    "CancelRunResponse",
    "CommitResponse",
    "ExecutionEventResponse",
    "ExecutionRunResponse",
    "MetricSnapshotResponse",
    "MetricsListResponse",
    "PublishAttemptResponse",
    "QualityReportResponse",
    "RollbackRunRequest",
    "RollbackRunResponse",
    "RunDetailResponse",
]
