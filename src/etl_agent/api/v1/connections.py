"""Flat connection routes.

These routes provide a project-agnostic URL structure while
maintaining strict tenant/project isolation.  The ``project_id``
is supplied in the request body (for create) or as a query
parameter (for list and test).

Routes:
    POST   /connections                          -- create a connection
    GET    /connections                           -- list connections in a project
    POST   /connections/{id}/test                 -- test a connection
    POST   /connections/{id}/metadata/explore     -- trigger async metadata exploration
    GET    /connections/{id}/metadata/profiles   -- list exploration results

All test results are persisted to the connection's status fields
in the database so the frontend never has to guess.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from etl_agent.core.dependencies import CurrentUser, get_current_user
from etl_agent.core.exceptions import (
    ConflictError,
    DomainError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
)
from etl_agent.core.permissions import assert_project_member, assert_project_write
from etl_agent.db.session import get_db
from etl_agent.models.data_source_connection import DataSourceConnection
from etl_agent.models.project import Project
from etl_agent.schemas.common import PageResponse
from etl_agent.schemas.metadata import (
    ExplorationTaskResponse,
    ExploreRequest,
    ExploreResponse,
    MetadataProfileResponse,
)
from etl_agent.schemas.tenant import (
    ConnectionCreateRequest,
    ConnectionTestResponse,
    DataSourceConnectionResponse,
)
from etl_agent.services import project_service
from etl_agent.services.metadata_service import (
    create_exploration_task,
    get_exploration_task,
    list_profiles_for_connection,
)

router = APIRouter(prefix="/connections", tags=["connections"])


def _to_domain_error(exc: DomainError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


def _conn_to_response(conn: DataSourceConnection) -> DataSourceConnectionResponse:
    """Build a response from a connection model.

    - ``username`` comes from the DB column (not the Vault secret).
    - ``password`` is always ``None`` -- never returned.
    - ``secret_ref`` is NOT exposed to the frontend.
    - Test status fields are read from the DB.
    """
    return DataSourceConnectionResponse(
        id=conn.id,
        tenant_id=conn.tenant_id,
        project_id=conn.project_id,
        name=conn.name,
        source_type=conn.source_type.value,
        host=conn.host,
        port=conn.port,
        database_name=conn.database_name,
        username=conn.db_username,
        password=None,
        description=conn.description,
        created_by=conn.created_by,
        created_at=conn.created_at,
        updated_at=conn.updated_at,
        last_test_status=conn.last_test_status.value,
        last_tested_at=conn.last_tested_at,
        last_test_message=conn.last_test_message,
        last_test_latency_ms=conn.last_test_latency_ms,
    )


# =====================================================================
# POST /connections -- create
# =====================================================================


@router.post(
    "",
    response_model=DataSourceConnectionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_connection(
    req: ConnectionCreateRequest,
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> DataSourceConnectionResponse:
    """Create a data-source connection in a project.

    The ``project_id`` in the request body scopes the connection.
    The password is written to Vault; the database stores only
    the Vault path reference.
    """
    try:
        conn = project_service.create_connection(
            db,
            actor_id=current.user_id,
            project_id=req.project_id,
            name=req.name,
            source_type=req.source_type,
            host=req.host,
            port=req.port,
            database_name=req.database_name,
            username=req.username,
            password=req.password,
            description=req.description,
        )
    except (
        InvalidInputError,
        NotFoundError,
        PermissionDeniedError,
        ConflictError,
    ) as exc:
        raise _to_domain_error(exc) from exc

    return _conn_to_response(conn)


# =====================================================================
# GET /connections -- list (filtered by project_id)
# =====================================================================


@router.get(
    "",
    response_model=PageResponse[DataSourceConnectionResponse],
)
def list_connections(
    project_id: uuid.UUID = Query(..., description="Filter connections by project"),  # noqa: B008
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),  # noqa: B008
    page_size: int = Query(default=50, ge=1, le=500, description="Items per page"),  # noqa: B008
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> PageResponse[DataSourceConnectionResponse]:
    """List data-source connections in a project (paginated, isolated)."""
    try:
        conns = project_service.list_connections(
            db,
            user_id=current.user_id,
            project_id=project_id,
        )
    except (NotFoundError, PermissionDeniedError) as exc:
        raise _to_domain_error(exc) from exc

    total = len(conns)
    start = (page - 1) * page_size
    paged = conns[start : start + page_size]
    return PageResponse(
        items=[_conn_to_response(c) for c in paged],
        total=total,
        page=page,
        page_size=page_size,
    )


# =====================================================================
# POST /connections/{id}/test -- test and persist result
# =====================================================================


@router.post(
    "/{connection_id}/test",
    response_model=ConnectionTestResponse,
)
def test_connection(
    connection_id: uuid.UUID,
    project_id: uuid.UUID = Query(..., description="Project the connection belongs to"),  # noqa: B008
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ConnectionTestResponse:
    """Test a data-source connection and persist the result to DB.

    The response message is sanitized -- it never contains the
    plaintext password.  The test result is also written to the
    connection's ``last_test_*`` fields in the database.
    """
    try:
        success, message, latency_ms = project_service.test_connection(
            db,
            user_id=current.user_id,
            project_id=project_id,
            connection_id=connection_id,
        )
    except (NotFoundError, PermissionDeniedError) as exc:
        raise _to_domain_error(exc) from exc

    from etl_agent.models.data_source_connection import ConnectionStatus

    # Read back the persisted status from the DB.
    conn = project_service.get_connection(
        db,
        user_id=current.user_id,
        project_id=project_id,
        connection_id=connection_id,
    )

    return ConnectionTestResponse(
        success=success,
        message=message,
        latency_ms=latency_ms,
        status=conn.last_test_status.value
        if conn.last_test_status
        else ConnectionStatus.untested.value,
    )


# =====================================================================
# POST /connections/{id}/metadata/explore -- trigger async exploration
# =====================================================================


@router.post(
    "/{connection_id}/metadata/explore",
    response_model=ExploreResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_explore(
    connection_id: uuid.UUID,
    req: ExploreRequest,
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ExploreResponse:
    """Trigger an async metadata exploration for a connection.

    Creates an ``ExplorationTask`` in ``pending`` state and
    dispatches a Celery task.  The caller should poll the task
    status via the returned ``task_id``.
    """
    # Verify access: exploration is a write action, not available to auditors.
    try:
        assert_project_write(db, current.user_id, req.project_id)
    except (NotFoundError, PermissionDeniedError) as exc:
        raise _to_domain_error(exc) from exc

    # Verify the connection exists in the project (isolated).
    project_service.get_connection(
        db,
        user_id=current.user_id,
        project_id=req.project_id,
        connection_id=connection_id,
    )

    # Look up tenant_id from the project.
    project = db.get(Project, req.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # Create exploration task record.
    task = create_exploration_task(
        db,
        tenant_id=project.tenant_id,
        project_id=req.project_id,
        connection_id=connection_id,
        triggered_by=current.user_id,
    )

    # Dispatch Celery task.
    from etl_agent.workers.tasks import explore_metadata_task

    celery_result = explore_metadata_task.delay(
        str(task.id),
        str(connection_id),
        str(project.tenant_id),
        str(req.project_id),
        max_tables=req.max_tables,
        max_sample_rows=req.max_sample_rows,
        max_execution_seconds=req.max_execution_seconds,
    )

    # Persist the celery task ID (without overriding the status —
    # in eager mode the task may have already completed by now).
    task.celery_task_id = celery_result.id
    db.commit()

    # Re-read the task to get the latest status (the Celery task
    # may have already finished in eager mode).
    db.refresh(task)

    status_msg = task.status.value
    if status_msg == "completed":
        msg = (
            "Metadata exploration completed. "
            "View results via GET /connections/{id}/metadata/profiles."
        )
    elif status_msg == "failed":
        msg = f"Metadata exploration failed: {task.error_message or 'unknown error'}"
    else:
        msg = (
            "Metadata exploration started. "
            "Poll for results via GET /connections/{id}/metadata/profiles."
        )

    return ExploreResponse(
        task_id=task.id,
        connection_id=connection_id,
        status=status_msg,
        message=msg,
    )


# =====================================================================
# GET /connections/{id}/metadata/profiles -- list exploration results
# =====================================================================


@router.get(
    "/{connection_id}/metadata/profiles",
    response_model=PageResponse[MetadataProfileResponse],
)
def list_metadata_profiles(
    connection_id: uuid.UUID,
    project_id: uuid.UUID = Query(..., description="Project the connection belongs to"),  # noqa: B008
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),  # noqa: B008
    page_size: int = Query(default=50, ge=1, le=500, description="Items per page"),  # noqa: B008
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> PageResponse[MetadataProfileResponse]:
    """List metadata profiles for a connection (paginated, isolated)."""
    try:
        assert_project_member(db, current.user_id, project_id)
    except (NotFoundError, PermissionDeniedError) as exc:
        raise _to_domain_error(exc) from exc

    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        project_service.get_connection(
            db,
            user_id=current.user_id,
            project_id=project_id,
            connection_id=connection_id,
        )
        profiles = list_profiles_for_connection(
            db,
            tenant_id=project.tenant_id,
            project_id=project_id,
            connection_id=connection_id,
        )
    except (NotFoundError, PermissionDeniedError) as exc:
        raise _to_domain_error(exc) from exc

    total = len(profiles)
    start = (page - 1) * page_size
    paged = profiles[start : start + page_size]
    return PageResponse(
        items=[
            MetadataProfileResponse(
                id=p.id,
                connection_id=p.connection_id,
                schema_name=p.schema_name,
                table_name=p.table_name,
                columns_info=p.columns_info,
                stats_info=p.stats_info,
                value_distribution=p.value_distribution,
                sample_data=p.sample_data,
                profiled_at=p.profiled_at,
            )
            for p in paged
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


# =====================================================================
# GET /connections/{id}/metadata/tasks/{task_id} -- poll task status
# =====================================================================


@router.get(
    "/{connection_id}/metadata/tasks/{task_id}",
    response_model=ExplorationTaskResponse,
)
def get_exploration_task_status(
    connection_id: uuid.UUID,
    task_id: uuid.UUID,
    project_id: uuid.UUID = Query(..., description="Project the connection belongs to"),  # noqa: B008
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ExplorationTaskResponse:
    """Poll the status of an async metadata exploration task."""
    try:
        assert_project_member(db, current.user_id, project_id)
    except (NotFoundError, PermissionDeniedError) as exc:
        raise _to_domain_error(exc) from exc

    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    task = get_exploration_task(db, task_id)
    if task is None or task.connection_id != connection_id:
        raise HTTPException(status_code=404, detail="Exploration task not found")

    if task.tenant_id != project.tenant_id or task.project_id != project_id:
        raise HTTPException(status_code=404, detail="Exploration task not found")

    return ExplorationTaskResponse(
        id=task.id,
        connection_id=task.connection_id,
        status=task.status.value,
        error_message=task.error_message,
        table_count=task.table_count,
        total_rows=task.total_rows,
        started_at=task.started_at,
        finished_at=task.finished_at,
        created_at=task.created_at,
    )
