"""Execution-run routes -- list, inspect, monitor, cancel, rollback.

Routes:
    GET   /execution-runs                          -- list (paginated, project-scoped)
    GET   /execution-runs/{id}                      -- run details
    GET   /execution-runs/{id}/metrics              -- metrics time-series
    GET   /execution-runs/{id}/events               -- execution event log
    GET   /execution-runs/{id}/quality-report       -- quality report
    GET   /execution-runs/{id}/publish-attempt      -- publish attempt details
    POST  /execution-runs/{id}/cancel                -- cancel a running job
    POST  /execution-runs/{id}/rollback              -- trigger managed rollback

All routes enforce tenant/project isolation.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from etl_agent.connectors.seatunnel import SeaTunnelClient
from etl_agent.core.dependencies import CurrentUser, get_current_user
from etl_agent.core.exceptions import DomainError, NotFoundError
from etl_agent.core.permissions import assert_project_member
from etl_agent.db.session import get_db
from etl_agent.models.execution_event import ExecutionEvent, ExecutionEventType
from etl_agent.models.execution_metric import ExecutionMetric
from etl_agent.models.execution_run import ExecutionRun, ExecutionStatus
from etl_agent.models.publish_attempt import PublishAttempt, PublishAttemptStatus
from etl_agent.models.quality_report import QualityReport
from etl_agent.schemas.commit import (
    CancelRunRequest,
    CancelRunResponse,
    ExecutionEventResponse,
    ExecutionRunResponse,
    MetricsListResponse,
    MetricSnapshotResponse,
    PublishAttemptResponse,
    QualityReportResponse,
    RollbackRunRequest,
    RollbackRunResponse,
    RunDetailResponse,
)
from etl_agent.schemas.common import PageResponse
from etl_agent.settings import settings

router = APIRouter(prefix="/execution-runs", tags=["execution-runs"])

_TERMINAL_STATUSES = frozenset(
    {ExecutionStatus.succeeded, ExecutionStatus.failed, ExecutionStatus.rolled_back},
)


def _to_domain_error(exc: DomainError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


def _get_run_or_404(db: Session, run_id: uuid.UUID) -> ExecutionRun:
    """Load an ExecutionRun or raise 404."""
    run = db.scalar(select(ExecutionRun).where(ExecutionRun.id == run_id))
    if run is None:
        raise _to_domain_error(NotFoundError(f"ExecutionRun {run_id} not found"))
    return run


def _check_project_access(
    db: Session,
    current_user: CurrentUser,
    run: ExecutionRun,
) -> None:
    """Assert the current user is a member of the run's project."""
    try:
        assert_project_member(db, current_user.user_id, run.project_id)
    except DomainError as exc:
        raise _to_domain_error(exc) from exc


def _run_to_response(run: ExecutionRun) -> ExecutionRunResponse:
    """Build an ExecutionRunResponse from an ORM object."""
    return ExecutionRunResponse(
        id=run.id,
        preparation_id=run.preparation_id,
        status=run.status.value,
        capability_id=run.capability_id,
        committed_by=run.committed_by,
        committed_at=run.committed_at,
        seatunnel_job_id=run.seatunnel_job_id,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error_message=run.error_message,
    )


# =====================================================================
# GET /execution-runs -- list (paginated, project-scoped)
# =====================================================================


@router.get(
    "",
    response_model=PageResponse[ExecutionRunResponse],
)
def list_execution_runs(
    project_id: uuid.UUID = Query(..., description="Filter runs by project"),  # noqa: B008
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),  # noqa: B008
    page_size: int = Query(default=50, ge=1, le=500, description="Items per page"),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> PageResponse[ExecutionRunResponse]:
    """List execution runs in a project (paginated, isolated).

    The caller must be a project member.
    """
    try:
        assert_project_member(db, current_user.user_id, project_id)
    except DomainError as exc:
        raise _to_domain_error(exc) from exc

    stmt = (
        select(ExecutionRun)
        .where(ExecutionRun.project_id == project_id)
        .order_by(ExecutionRun.committed_at.desc())
    )
    runs = list(db.scalars(stmt).all())

    total = len(runs)
    start = (page - 1) * page_size
    paged = runs[start : start + page_size]
    return PageResponse(
        items=[_run_to_response(r) for r in paged],
        total=total,
        page=page,
        page_size=page_size,
    )


# =====================================================================
# GET /execution-runs/{id} -- details (extended)
# =====================================================================


@router.get(
    "/{run_id}",
    response_model=RunDetailResponse,
)
def get_execution_run(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> RunDetailResponse:
    """Get the details of an execution run.

    Includes the quality report and publish attempt if they exist.
    The caller must be a project member of the run's project.
    """
    run = _get_run_or_404(db, run_id)
    _check_project_access(db, current_user, run)

    quality_report = db.scalar(
        select(QualityReport).where(QualityReport.execution_run_id == run_id),
    )
    publish_attempt = db.scalar(
        select(PublishAttempt).where(PublishAttempt.execution_run_id == run_id),
    )

    return RunDetailResponse(
        **_run_to_response(run).model_dump(),
        quality_report=QualityReportResponse.model_validate(quality_report)
        if quality_report
        else None,
        publish_attempt=PublishAttemptResponse.model_validate(publish_attempt)
        if publish_attempt
        else None,
    )


# =====================================================================
# GET /execution-runs/{id}/metrics -- metrics time-series
# =====================================================================


@router.get(
    "/{run_id}/metrics",
    response_model=MetricsListResponse,
)
def get_run_metrics(
    run_id: uuid.UUID,
    page: int = Query(default=1, ge=1),  # noqa: B008
    page_size: int = Query(default=50, ge=1, le=500),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> MetricsListResponse:
    """Get the metrics time-series for an execution run.

    Returns paginated metric snapshots ordered by ``sampled_at`` descending,
    plus the most recent snapshot as ``latest``.
    """
    run = _get_run_or_404(db, run_id)
    _check_project_access(db, current_user, run)

    stmt = (
        select(ExecutionMetric)
        .where(ExecutionMetric.execution_run_id == run_id)
        .order_by(ExecutionMetric.sampled_at.desc())
    )
    metrics = list(db.scalars(stmt).all())

    total = len(metrics)
    start = (page - 1) * page_size
    paged = metrics[start : start + page_size]
    latest = MetricSnapshotResponse.model_validate(metrics[0]) if metrics else None

    return MetricsListResponse(
        items=[MetricSnapshotResponse.model_validate(m) for m in paged],
        total=total,
        latest=latest,
    )


# =====================================================================
# GET /execution-runs/{id}/events -- execution event log
# =====================================================================


@router.get(
    "/{run_id}/events",
    response_model=PageResponse[ExecutionEventResponse],
)
def get_run_events(
    run_id: uuid.UUID,
    page: int = Query(default=1, ge=1),  # noqa: B008
    page_size: int = Query(default=50, ge=1, le=500),  # noqa: B008
    severity: str | None = Query(default=None, description="Filter by severity"),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> PageResponse[ExecutionEventResponse]:
    """Get the execution event log for a run.

    Events are ordered by ``occurred_at`` descending.
    Optional ``severity`` filter (info / warning / error / critical).
    """
    run = _get_run_or_404(db, run_id)
    _check_project_access(db, current_user, run)

    stmt = (
        select(ExecutionEvent)
        .where(ExecutionEvent.execution_run_id == run_id)
        .order_by(ExecutionEvent.occurred_at.desc())
    )
    if severity:
        stmt = stmt.where(ExecutionEvent.severity == severity)

    events = list(db.scalars(stmt).all())
    total = len(events)
    start = (page - 1) * page_size
    paged = events[start : start + page_size]

    return PageResponse(
        items=[ExecutionEventResponse.model_validate(e) for e in paged],
        total=total,
        page=page,
        page_size=page_size,
    )


# =====================================================================
# GET /execution-runs/{id}/quality-report
# =====================================================================


@router.get(
    "/{run_id}/quality-report",
    response_model=QualityReportResponse,
)
def get_quality_report(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> QualityReportResponse:
    """Get the quality report for an execution run.

    Returns 404 if the quality report has not been created yet
    (i.e. the SeaTunnel job has not finished or quality check
    has not been executed).
    """
    run = _get_run_or_404(db, run_id)
    _check_project_access(db, current_user, run)

    report = db.scalar(
        select(QualityReport).where(QualityReport.execution_run_id == run_id),
    )
    if report is None:
        raise _to_domain_error(
            NotFoundError(f"QualityReport for run {run_id} not found"),
        )
    return QualityReportResponse.model_validate(report)


# =====================================================================
# GET /execution-runs/{id}/publish-attempt
# =====================================================================


@router.get(
    "/{run_id}/publish-attempt",
    response_model=PublishAttemptResponse,
)
def get_publish_attempt(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> PublishAttemptResponse:
    """Get the publish (atomic-swap) attempt for an execution run.

    Returns 404 if the publish attempt has not been created yet.
    """
    run = _get_run_or_404(db, run_id)
    _check_project_access(db, current_user, run)

    attempt = db.scalar(
        select(PublishAttempt).where(PublishAttempt.execution_run_id == run_id),
    )
    if attempt is None:
        raise _to_domain_error(
            NotFoundError(f"PublishAttempt for run {run_id} not found"),
        )
    return PublishAttemptResponse.model_validate(attempt)


# =====================================================================
# POST /execution-runs/{id}/cancel
# =====================================================================


@router.post(
    "/{run_id}/cancel",
    response_model=CancelRunResponse,
)
def cancel_execution_run(
    run_id: uuid.UUID,
    body: CancelRunRequest | None = None,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> CancelRunResponse:
    """Cancel a running execution by stopping the SeaTunnel job.

    Only runs in ``pending`` or ``running`` status can be cancelled.
    Sends a stop request to SeaTunnel, marks the run as ``failed``,
    and writes an ``ExecutionEvent(job_cancelled)``.

    Returns ``cancelled=False`` if the job was already finished or
    the stop request was rejected by SeaTunnel.
    """
    run = _get_run_or_404(db, run_id)
    _check_project_access(db, current_user, run)

    if run.status in _TERMINAL_STATUSES:
        return CancelRunResponse(
            run_id=run_id,
            status=run.status.value,
            seatunnel_job_id=run.seatunnel_job_id,
            cancelled=False,
        )

    cancelled = False
    error_msg = None

    if run.seatunnel_job_id is not None:
        try:
            client = SeaTunnelClient(base_url=settings.seatunnel_base_url)
            cancelled = client.stop_job(run.seatunnel_job_id)
        except Exception as exc:
            error_msg = f"SeaTunnel stop failed: {str(exc)[:256]}"
    else:
        # Job not yet submitted -- mark as cancelled directly
        cancelled = True

    run.status = ExecutionStatus.failed
    run.error_message = (
        f"Cancelled by user: {body.reason if body and body.reason else 'unspecified'}"
    )
    if error_msg:
        run.error_message += f" | {error_msg}"

    from datetime import UTC, datetime

    run.finished_at = datetime.now(UTC)
    reason_text = body.reason if body and body.reason else "unspecified"
    db.add(
        ExecutionEvent(
            execution_run_id=run_id,
            event_type=ExecutionEventType.job_cancelled,
            severity="warning",
            message=f"Job cancelled by user (reason: {reason_text})",
            details={
                "cancelled_by": str(current_user.user_id),
                "reason": body.reason if body and body.reason else None,
                "seatunnel_stop_accepted": cancelled,
            },
        ),
    )
    db.commit()

    return CancelRunResponse(
        run_id=run_id,
        status=run.status.value,
        seatunnel_job_id=run.seatunnel_job_id,
        cancelled=cancelled,
    )


# =====================================================================
# POST /execution-runs/{id}/rollback
# =====================================================================


@router.post(
    "/{run_id}/rollback",
    response_model=RollbackRunResponse,
)
def rollback_execution_run(
    run_id: uuid.UUID,
    body: RollbackRunRequest | None = None,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> RollbackRunResponse:
    """Trigger a managed rollback for an execution run.

    This calls the rollback service to restore the formal table from
    the backup table, clean up the shadow table, and mark the run
    as ``rolled_back``.

    Only runs in ``quality_checking``, ``publishing``, ``succeeded``,
    or ``failed`` status can be rolled back.  Runs already in
    ``rolled_back`` status are idempotent (return existing state).
    """
    run = _get_run_or_404(db, run_id)
    _check_project_access(db, current_user, run)

    if run.status == ExecutionStatus.rolled_back:
        return RollbackRunResponse(
            run_id=run_id,
            status=run.status.value,
            rollback_result={"already_rolled_back": True},
        )

    if run.status not in (
        ExecutionStatus.quality_checking,
        ExecutionStatus.publishing,
        ExecutionStatus.succeeded,
        ExecutionStatus.failed,
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot rollback run in status '{run.status.value}'. "
                "Run must be in quality_checking, publishing, "
                "succeeded, or failed."
            ),
        )

    # Determine if swap was attempted
    publish_attempt = db.scalar(
        select(PublishAttempt).where(PublishAttempt.execution_run_id == run_id),
    )
    swap_attempted = publish_attempt is not None and publish_attempt.status in (
        PublishAttemptStatus.succeeded,
        PublishAttemptStatus.failed,
    )

    from etl_agent.services.execution_rollback_service import run_managed_rollback

    result = run_managed_rollback(
        db,
        run,
        failure_reason=(body.reason if body and body.reason else "Manual rollback via API"),
        failure_details={
            "triggered_by": str(current_user.user_id),
            "source": "api",
        },
        swap_attempted=swap_attempted,
    )
    db.commit()

    return RollbackRunResponse(
        run_id=run_id,
        status=run.status.value,
        rollback_result=result,
    )
