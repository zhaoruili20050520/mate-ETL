"""Preparation routes -- create, view, and commit preparations.

Routes:
    POST /preparations                  -- create a preparation from a frozen version
    GET  /preparations/{id}             -- get preparation details
    POST /preparations/{id}/commit      -- commit an approved preparation

The approval routes (POST/GET /preparations/{id}/approvals) are
served by the ``approval`` router in ``api/v1/approval.py``.

All routes enforce tenant/project isolation via the service layer.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from etl_agent.core.dependencies import CurrentUser, get_current_user
from etl_agent.core.exceptions import DomainError
from etl_agent.db.session import get_db
from etl_agent.models.preparation import Preparation
from etl_agent.schemas.commit import CommitResponse
from etl_agent.schemas.preparation import (
    CreatePreparationRequest,
    PreparationResponse,
)
from etl_agent.services import commit_service, preparation_service

router = APIRouter(prefix="/preparations", tags=["preparations"])


def _to_domain_error(exc: DomainError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


def _prep_to_response(prep: Preparation) -> PreparationResponse:
    """Build a PreparationResponse from an ORM object."""
    return PreparationResponse(
        id=prep.id,
        pipeline_version_id=prep.pipeline_version_id,
        tenant_id=prep.tenant_id,
        project_id=prep.project_id,
        input_fingerprint=prep.input_fingerprint,
        resource_scope=prep.resource_scope,
        data_classification=prep.data_classification.value,
        execution_budget=prep.execution_budget,
        rollback_plan=prep.rollback_plan,
        risk_level=prep.risk_level.value,
        risk_reasoning=prep.risk_reasoning,
        status=prep.status.value,
        prepared_by=prep.prepared_by,
        committed_at=prep.committed_at,
        created_at=prep.created_at,
        updated_at=prep.updated_at,
    )


# =====================================================================
# POST /preparations -- create
# =====================================================================


@router.post(
    "",
    response_model=PreparationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_preparation(
    body: CreatePreparationRequest,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> PreparationResponse:
    """Create a Preparation from a frozen PipelineVersion.

    The caller must have write access to the project that owns the
    pipeline version.  The PDP will assign a risk level (P0-P3) based
    on the data classification, resource scope, and rollback strategy.
    """
    try:
        prep = preparation_service.create_preparation(
            db,
            pipeline_version_id=body.pipeline_version_id,
            user_id=current_user.user_id,
            data_classification=body.data_classification,
            execution_budget=body.execution_budget,
            is_production_source=body.is_production_source,
            is_multi_tenant=body.is_multi_tenant,
        )
    except DomainError as exc:
        raise _to_domain_error(exc) from exc

    return _prep_to_response(prep)


# =====================================================================
# GET /preparations/{id} -- details
# =====================================================================


@router.get(
    "/{preparation_id}",
    response_model=PreparationResponse,
)
def get_preparation(
    preparation_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> PreparationResponse:
    """Get the details of a preparation.

    The caller must be a project member.
    """
    try:
        prep = preparation_service.get_preparation(
            db,
            preparation_id,
            current_user.user_id,
        )
    except DomainError as exc:
        raise _to_domain_error(exc) from exc

    return _prep_to_response(prep)


# =====================================================================
# POST /preparations/{id}/commit -- commit
# =====================================================================


@router.post(
    "/{preparation_id}/commit",
    response_model=CommitResponse,
    status_code=status.HTTP_201_CREATED,
)
def commit_preparation(
    preparation_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> CommitResponse:
    """Commit an approved preparation.

    This is the final gate before execution.  It:
    1. Re-validates the input fingerprint (tamper detection).
    2. Verifies all required approvals are in place.
    3. Verifies the caller has project write access.
    4. Creates an ExecutionRun.
    5. Issues an Ed25519 Capability token (5-min, single-use).
    6. Creates an Outbox message for async dispatch.
    7. Logs the commit + capability_issued events to the Evidence Ledger.

    All of steps 4-7 are in a single DB transaction.
    """
    try:
        run = commit_service.commit_preparation(
            db,
            preparation_id=preparation_id,
            user_id=current_user.user_id,
        )
    except DomainError as exc:
        raise _to_domain_error(exc) from exc

    return CommitResponse(
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
