"""Project management routes.

Projects are always created within a tenant.  Resource queries
(connections, profiles) are scoped by ``project_id`` and the
project's ``tenant_id`` -- there is no endpoint that returns
unscoped data.
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
from etl_agent.db.session import get_db
from etl_agent.models.data_source_connection import DataSourceConnection
from etl_agent.models.user import User
from etl_agent.schemas.common import PageResponse
from etl_agent.schemas.metadata import MetadataProfileResponse
from etl_agent.schemas.tenant import (
    AddProjectMemberRequest,
    ConnectionTestResponse,
    ConnectionUpdateRequest,
    DataSourceConnectionCreate,
    DataSourceConnectionResponse,
    ProjectCreate,
    ProjectMemberResponse,
    ProjectResponse,
    ProjectUpdateRequest,
)
from etl_agent.services import project_service

router = APIRouter(prefix="/projects", tags=["projects"])


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
# Project CRUD
# =====================================================================


@router.post(
    "",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_project(
    req: ProjectCreate,
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ProjectResponse:
    """Create a project within a tenant (requires tenant_admin)."""
    try:
        project = project_service.create_project(
            db,
            actor_id=current.user_id,
            tenant_id=req.tenant_id,
            name=req.name,
            description=req.description,
        )
    except (NotFoundError, PermissionDeniedError) as exc:
        raise _to_domain_error(exc) from exc
    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        status=project.status.value,
        tenant_id=project.tenant_id,
        created_at=project.created_at,
    )


@router.get("", response_model=PageResponse[ProjectResponse])
def list_projects(
    tenant_id: uuid.UUID | None = None,
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),  # noqa: B008
    page_size: int = Query(default=50, ge=1, le=500, description="Items per page"),  # noqa: B008
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> PageResponse[ProjectResponse]:
    """List projects the user is a member of (paginated).

    Optional ``?tenant_id=`` query param filters to a specific tenant.
    """
    projects = project_service.list_projects_for_user(
        db, user_id=current.user_id, tenant_id=tenant_id
    )
    total = len(projects)
    start = (page - 1) * page_size
    paged = projects[start : start + page_size]
    return PageResponse(
        items=[
            ProjectResponse(
                id=p.id,
                name=p.name,
                description=p.description,
                status=p.status.value,
                tenant_id=p.tenant_id,
                created_at=p.created_at,
            )
            for p in paged
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ProjectResponse:
    """Get details of a project the user has access to."""
    try:
        project = project_service.get_project(db, user_id=current.user_id, project_id=project_id)
    except (NotFoundError, PermissionDeniedError) as exc:
        raise _to_domain_error(exc) from exc
    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        status=project.status.value,
        tenant_id=project.tenant_id,
        created_at=project.created_at,
    )


@router.patch(
    "/{project_id}",
    response_model=ProjectResponse,
)
def update_project(
    project_id: uuid.UUID,
    req: ProjectUpdateRequest,
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ProjectResponse:
    """Update a project's name or description."""
    try:
        project = project_service.update_project(
            db,
            actor_id=current.user_id,
            project_id=project_id,
            name=req.name,
            description=req.description,
        )
    except (NotFoundError, PermissionDeniedError, ConflictError) as exc:
        raise _to_domain_error(exc) from exc
    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        status=project.status.value,
        tenant_id=project.tenant_id,
        created_at=project.created_at,
    )


# =====================================================================
# Project member management
# =====================================================================


@router.post(
    "/{project_id}/members",
    response_model=ProjectMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_project_member(
    project_id: uuid.UUID,
    req: AddProjectMemberRequest,
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ProjectMemberResponse:
    """Add a user to a project (requires project_admin or tenant_admin)."""
    try:
        membership = project_service.add_project_member(
            db,
            actor_id=current.user_id,
            project_id=project_id,
            username=req.username,
            role=req.role,
        )
    except (NotFoundError, PermissionDeniedError, ConflictError) as exc:
        raise _to_domain_error(exc) from exc

    user = db.get(User, membership.user_id)
    return ProjectMemberResponse(
        id=membership.id,
        project_id=membership.project_id,
        user_id=membership.user_id,
        username=user.username if user else "",
        role=membership.role.value,
        created_at=membership.created_at,
    )


@router.get(
    "/{project_id}/members",
    response_model=PageResponse[ProjectMemberResponse],
)
def list_project_members(
    project_id: uuid.UUID,
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),  # noqa: B008
    page_size: int = Query(default=50, ge=1, le=500, description="Items per page"),  # noqa: B008
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> PageResponse[ProjectMemberResponse]:
    """List members of a project (paginated)."""
    try:
        members = project_service.list_project_members(
            db, user_id=current.user_id, project_id=project_id
        )
    except (NotFoundError, PermissionDeniedError) as exc:
        raise _to_domain_error(exc) from exc

    total = len(members)
    start = (page - 1) * page_size
    paged = members[start : start + page_size]
    return PageResponse(
        items=[
            ProjectMemberResponse(
                id=m.id,
                project_id=m.project_id,
                user_id=m.user_id,
                username=username,
                role=m.role.value,
                created_at=m.created_at,
            )
            for m, username in paged
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.delete(
    "/{project_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_project_member(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> None:
    """Remove a user from a project (requires project_admin or tenant_admin)."""
    try:
        project_service.remove_project_member(
            db,
            actor_id=current.user_id,
            project_id=project_id,
            user_id=user_id,
        )
    except (NotFoundError, PermissionDeniedError) as exc:
        raise _to_domain_error(exc) from exc


# =====================================================================
# Data source connections (project-scoped, isolated)
# =====================================================================


@router.get(
    "/{project_id}/connections",
    response_model=PageResponse[DataSourceConnectionResponse],
)
def list_connections(
    project_id: uuid.UUID,
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),  # noqa: B008
    page_size: int = Query(default=50, ge=1, le=500, description="Items per page"),  # noqa: B008
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> PageResponse[DataSourceConnectionResponse]:
    """List data-source connections in a project (paginated, isolated)."""
    try:
        conns = project_service.list_connections(db, user_id=current.user_id, project_id=project_id)
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


@router.post(
    "/{project_id}/connections",
    response_model=DataSourceConnectionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_connection(
    project_id: uuid.UUID,
    req: DataSourceConnectionCreate,
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> DataSourceConnectionResponse:
    """Create a data-source connection in a project (requires engineer+).

    The password is written to Vault; the database stores only the
    Vault path reference.  The response never includes the password.
    """
    try:
        conn = project_service.create_connection(
            db,
            actor_id=current.user_id,
            project_id=project_id,
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


@router.get(
    "/{project_id}/connections/{connection_id}",
    response_model=DataSourceConnectionResponse,
)
def get_connection(
    project_id: uuid.UUID,
    connection_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> DataSourceConnectionResponse:
    """Get a single data-source connection (isolated)."""
    try:
        conn = project_service.get_connection(
            db,
            user_id=current.user_id,
            project_id=project_id,
            connection_id=connection_id,
        )
    except (NotFoundError, PermissionDeniedError) as exc:
        raise _to_domain_error(exc) from exc

    return _conn_to_response(conn)


@router.patch(
    "/{project_id}/connections/{connection_id}",
    response_model=DataSourceConnectionResponse,
)
def update_connection(
    project_id: uuid.UUID,
    connection_id: uuid.UUID,
    req: ConnectionUpdateRequest,
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> DataSourceConnectionResponse:
    """Update a connection's metadata or Vault credentials."""
    try:
        conn = project_service.update_connection(
            db,
            actor_id=current.user_id,
            project_id=project_id,
            connection_id=connection_id,
            name=req.name,
            source_type=req.source_type.value if req.source_type else None,
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


@router.post(
    "/{project_id}/connections/{connection_id}/test",
    response_model=ConnectionTestResponse,
)
def test_connection(
    project_id: uuid.UUID,
    connection_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ConnectionTestResponse:
    """Test a data-source connection using credentials from Vault.

    The response message is sanitized -- it never contains the
    plaintext password.
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

    # Read back the persisted status from the DB (test_connection
    # already refreshed the conn object, but we re-fetch to be safe).
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


@router.delete(
    "/{project_id}/connections/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_connection(
    project_id: uuid.UUID,
    connection_id: uuid.UUID,
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> None:
    """Delete a connection and its Vault credentials (requires admin)."""
    try:
        project_service.delete_connection(
            db,
            actor_id=current.user_id,
            project_id=project_id,
            connection_id=connection_id,
        )
    except (NotFoundError, PermissionDeniedError) as exc:
        raise _to_domain_error(exc) from exc


# =====================================================================
# Metadata profiles (project-scoped, isolated, read-only)
# =====================================================================


@router.get(
    "/{project_id}/profiles",
    response_model=PageResponse[MetadataProfileResponse],
)
def list_profiles(
    project_id: uuid.UUID,
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),  # noqa: B008
    page_size: int = Query(default=50, ge=1, le=500, description="Items per page"),  # noqa: B008
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> PageResponse[MetadataProfileResponse]:
    """List metadata profiles in a project (paginated, isolated)."""
    try:
        profiles = project_service.list_profiles(db, user_id=current.user_id, project_id=project_id)
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
