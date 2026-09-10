"""Pipeline generation, freezing, and listing routes.

Routes:
    POST   /pipelines/generate               -- generate a pipeline from NL
    POST   /pipelines/{id}/clarify            -- submit clarification answers
    POST   /pipelines/{id}/versions           -- freeze a validated pipeline
    GET    /pipelines                          -- list pipelines in a project
    GET    /pipelines/{id}                     -- get pipeline detail
    GET    /pipelines/{id}/versions            -- list frozen versions
    GET    /pipelines/{id}/versions/{vid}      -- get a specific version

All routes enforce tenant/project isolation via ``assert_project_member``
or ``assert_project_write`` inside the service layer.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from etl_agent.core.dependencies import CurrentUser, get_current_user
from etl_agent.core.exceptions import (
    DomainError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
)
from etl_agent.db.session import get_db
from etl_agent.models.etl_plan import Pipeline
from etl_agent.models.pipeline_version import PipelineVersion
from etl_agent.schemas.common import PageResponse
from etl_agent.schemas.generation import EtlPlan, ValidationIssue
from etl_agent.schemas.pipeline import (
    ClarifyRequest,
    ClarifyResponse,
    FreezeVersionRequest,
    GeneratePipelineRequest,
    GeneratePipelineResponse,
    PipelineDetailResponse,
    PipelineSummaryResponse,
    PipelineVersionResponse,
)
from etl_agent.services import pipeline_service

router = APIRouter(prefix="/pipelines", tags=["pipelines"])


def _to_domain_error(exc: DomainError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


def _build_generate_response(pipeline: Pipeline) -> GeneratePipelineResponse:
    """Build a GeneratePipelineResponse from a Pipeline ORM object."""
    # Deserialize EtlPlan if present
    etl_plan = None
    if pipeline.etl_plan:
        try:
            etl_plan = EtlPlan.model_validate(pipeline.etl_plan)
        except Exception:
            etl_plan = None

    # Deserialize validation issues if present
    issues = []
    if pipeline.validation_issues:
        try:
            issues = [ValidationIssue.model_validate(i) for i in pipeline.validation_issues]
        except Exception:
            issues = []

    # Build clarification questions
    questions = pipeline.clarification_questions or []

    return GeneratePipelineResponse(
        pipeline_id=pipeline.id,
        status=pipeline.status.value,
        etl_plan=etl_plan,
        hocon_config=pipeline.hocon_config,
        validation_issues=issues,
        clarification_questions=list(questions) if questions else [],
        error=None,
    )


def _build_clarify_response(pipeline: Pipeline) -> ClarifyResponse:
    """Build a ClarifyResponse from a Pipeline ORM object."""
    etl_plan = None
    if pipeline.etl_plan:
        try:
            etl_plan = EtlPlan.model_validate(pipeline.etl_plan)
        except Exception:
            etl_plan = None

    issues = []
    if pipeline.validation_issues:
        try:
            issues = [ValidationIssue.model_validate(i) for i in pipeline.validation_issues]
        except Exception:
            issues = []

    questions = pipeline.clarification_questions or []

    return ClarifyResponse(
        pipeline_id=pipeline.id,
        status=pipeline.status.value,
        etl_plan=etl_plan,
        hocon_config=pipeline.hocon_config,
        validation_issues=issues,
        clarification_questions=list(questions) if questions else [],
        error=None,
    )


def _build_version_response(version: PipelineVersion) -> PipelineVersionResponse:
    """Build a PipelineVersionResponse from a PipelineVersion ORM object."""
    return PipelineVersionResponse(
        id=version.id,
        pipeline_id=version.pipeline_id,
        version_number=version.version_number,
        etl_plan=version.etl_plan,
        hocon_text=version.hocon_text,
        sha256=version.sha256,
        source_requirement=version.source_requirement,
        validation_results=version.validation_results,
        status=version.status.value,
        frozen_by=version.frozen_by,
        created_at=version.created_at,
    )


# =====================================================================
# POST /pipelines/generate
# =====================================================================


@router.post(
    "/generate",
    response_model=GeneratePipelineResponse,
)
def generate_pipeline(
    req: GeneratePipelineRequest,
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> GeneratePipelineResponse:
    """Generate a pipeline from a natural-language requirement.

    The LangGraph chain parses the requirement, pulls metadata, generates
    an EtlPlan + HOCON, runs gate checks, and (if all pass) prepares a
    frozen version.  If the requirement is missing required fields, the
    response includes ``clarification_questions`` and the status is
    ``needs_clarification``.
    """
    # Build hints dict from optional structured fields
    hints: dict[str, object] = {}
    if req.sync_mode is not None:
        hints["sync_mode"] = req.sync_mode
    if req.source_table is not None:
        hints["source_table"] = req.source_table
    if req.target_table is not None:
        hints["target_table"] = req.target_table
    if req.primary_keys:
        hints["primary_keys"] = req.primary_keys
    if req.watermark_column is not None:
        hints["watermark_column"] = req.watermark_column

    try:
        pipeline = pipeline_service.generate_pipeline(
            db,
            actor_id=current.user_id,
            project_id=req.project_id,
            source_connection_id=req.source_connection_id,
            target_connection_id=req.target_connection_id,
            natural_language=req.natural_language,
            name=req.name,
            hints=hints if hints else None,
        )
    except (
        InvalidInputError,
        NotFoundError,
        PermissionDeniedError,
    ) as exc:
        raise _to_domain_error(exc) from exc

    return _build_generate_response(pipeline)


# =====================================================================
# POST /pipelines/{id}/clarify
# =====================================================================


@router.post(
    "/{pipeline_id}/clarify",
    response_model=ClarifyResponse,
)
def clarify_pipeline(
    pipeline_id: uuid.UUID,
    req: ClarifyRequest,
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ClarifyResponse:
    """Submit clarification answers and resume the generation chain.

    The pipeline must be in ``needs_clarification`` state.  The answers
    are merged into the parsed requirement and the graph resumes from
    the metadata node onward.
    """
    try:
        pipeline = pipeline_service.clarify_pipeline(
            db,
            user_id=current.user_id,
            pipeline_id=pipeline_id,
            answers=req.answers,
        )
    except (
        InvalidInputError,
        NotFoundError,
        PermissionDeniedError,
    ) as exc:
        raise _to_domain_error(exc) from exc

    return _build_clarify_response(pipeline)


# =====================================================================
# POST /pipelines/{id}/versions -- freeze
# =====================================================================


@router.post(
    "/{pipeline_id}/versions",
    response_model=PipelineVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
def freeze_version(
    pipeline_id: uuid.UUID,
    req: FreezeVersionRequest,  # noqa: ARG001 -- no fields yet, reserved for future
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> PipelineVersionResponse:
    """Freeze a validated pipeline into an immutable version.

    The pipeline must be in ``validated`` state.  A SHA256 hash is
    computed from the EtlPlan + HOCON text for integrity verification.
    Versions are append-only -- once frozen they cannot be modified.
    """
    try:
        version = pipeline_service.freeze_version(
            db,
            user_id=current.user_id,
            pipeline_id=pipeline_id,
        )
    except (
        InvalidInputError,
        NotFoundError,
        PermissionDeniedError,
    ) as exc:
        raise _to_domain_error(exc) from exc

    return _build_version_response(version)


# =====================================================================
# GET /pipelines -- list (filtered by project_id)
# =====================================================================


@router.get(
    "",
    response_model=PageResponse[PipelineSummaryResponse],
)
def list_pipelines(
    project_id: uuid.UUID = Query(..., description="Filter pipelines by project"),  # noqa: B008
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),  # noqa: B008
    page_size: int = Query(default=50, ge=1, le=500, description="Items per page"),  # noqa: B008
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> PageResponse[PipelineSummaryResponse]:
    """List pipelines in a project (paginated, isolated)."""
    try:
        pipelines = pipeline_service.list_pipelines(
            db,
            user_id=current.user_id,
            project_id=project_id,
        )
    except (NotFoundError, PermissionDeniedError) as exc:
        raise _to_domain_error(exc) from exc

    total = len(pipelines)
    start = (page - 1) * page_size
    paged = pipelines[start : start + page_size]
    return PageResponse(
        items=[
            PipelineSummaryResponse(
                id=p.id,
                project_id=p.project_id,
                name=p.name,
                status=p.status.value,
                source_connection_id=p.source_connection_id,
                target_connection_id=p.target_connection_id,
                created_by=p.created_by,
                created_at=p.created_at,
                updated_at=p.updated_at,
            )
            for p in paged
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


# =====================================================================
# GET /pipelines/{id} -- detail
# =====================================================================


@router.get(
    "/{pipeline_id}",
    response_model=PipelineDetailResponse,
)
def get_pipeline(
    pipeline_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> PipelineDetailResponse:
    """Get the full detail of a pipeline including working state."""
    try:
        pipeline = pipeline_service.get_pipeline(
            db,
            user_id=current.user_id,
            pipeline_id=pipeline_id,
        )
    except (NotFoundError, PermissionDeniedError) as exc:
        raise _to_domain_error(exc) from exc

    return PipelineDetailResponse(
        id=pipeline.id,
        name=pipeline.name,
        description=pipeline.description,
        status=pipeline.status.value,
        source_connection_id=pipeline.source_connection_id,
        target_connection_id=pipeline.target_connection_id,
        source_requirement=pipeline.source_requirement,
        etl_plan=pipeline.etl_plan,
        hocon_config=pipeline.hocon_config,
        validation_issues=pipeline.validation_issues,
        clarification_questions=pipeline.clarification_questions,
        autofix_attempts=pipeline.autofix_attempts,
        created_by=pipeline.created_by,
        created_at=pipeline.created_at,
        updated_at=pipeline.updated_at,
    )


@router.delete(
    "/{pipeline_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_pipeline(
    pipeline_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> None:
    """Delete a draft or clarification pipeline that has no frozen versions."""
    try:
        pipeline_service.delete_pipeline(
            db,
            user_id=current.user_id,
            pipeline_id=pipeline_id,
        )
    except (InvalidInputError, NotFoundError, PermissionDeniedError) as exc:
        raise _to_domain_error(exc) from exc


# =====================================================================
# GET /pipelines/{id}/versions -- list frozen versions
# =====================================================================


@router.get(
    "/{pipeline_id}/versions",
    response_model=PageResponse[PipelineVersionResponse],
)
def list_versions(
    pipeline_id: uuid.UUID,
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),  # noqa: B008
    page_size: int = Query(default=50, ge=1, le=500, description="Items per page"),  # noqa: B008
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> PageResponse[PipelineVersionResponse]:
    """List frozen versions for a pipeline (paginated, isolated)."""
    try:
        versions = pipeline_service.list_versions(
            db,
            user_id=current.user_id,
            pipeline_id=pipeline_id,
        )
    except (NotFoundError, PermissionDeniedError) as exc:
        raise _to_domain_error(exc) from exc

    total = len(versions)
    start = (page - 1) * page_size
    paged = versions[start : start + page_size]
    return PageResponse(
        items=[_build_version_response(v) for v in paged],
        total=total,
        page=page,
        page_size=page_size,
    )


# =====================================================================
# GET /pipelines/{id}/versions/{vid} -- get specific version
# =====================================================================


@router.get(
    "/{pipeline_id}/versions/{version_id}",
    response_model=PipelineVersionResponse,
)
def get_version(
    pipeline_id: uuid.UUID,
    version_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> PipelineVersionResponse:
    """Get a specific frozen version of a pipeline."""
    try:
        version = pipeline_service.get_version(
            db,
            user_id=current.user_id,
            pipeline_id=pipeline_id,
            version_id=version_id,
        )
    except (NotFoundError, PermissionDeniedError) as exc:
        raise _to_domain_error(exc) from exc

    return _build_version_response(version)
