"""Project management service: CRUD, member administration, and
**isolated** resource queries.

Every query that touches ``data_source_connections`` or
``metadata_profiles`` is scoped by ``tenant_id`` **and** ``project_id``.
There is no code path that performs a global (unfiltered) select on
these tables.  This is the core tenant/project isolation guarantee.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from etl_agent.connectors.base import ConnectionParams
from etl_agent.connectors.factory import create_connector
from etl_agent.connectors.vault import VaultClient, get_vault_client
from etl_agent.core.exceptions import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
)
from etl_agent.core.permissions import (
    assert_project_admin,
    assert_project_exists,
    assert_project_member,
    assert_project_write,
    assert_tenant_admin,
    assert_tenant_member,
)
from etl_agent.models.data_source_connection import (
    DataSourceConnection,
    DataSourceType,
)
from etl_agent.models.metadata_profile import MetadataProfile
from etl_agent.models.project import Project, ProjectStatus
from etl_agent.models.project_member import ProjectMember
from etl_agent.models.role import Role
from etl_agent.models.user import User

logger = logging.getLogger(__name__)


# =====================================================================
# Project CRUD
# =====================================================================


def create_project(
    db: Session,
    *,
    actor_id: uuid.UUID,
    tenant_id: uuid.UUID,
    name: str,
    description: str | None = None,
) -> Project:
    """Create a project within a tenant.

    Requires ``tenant_admin`` of the tenant.
    """
    assert_tenant_admin(db, actor_id, tenant_id)

    project = Project(
        name=name,
        description=description,
        status=ProjectStatus.active,
        tenant_id=tenant_id,
    )
    db.add(project)
    db.flush()

    # Auto-enroll creator as project_admin.
    membership = ProjectMember(
        project_id=project.id,
        user_id=actor_id,
        role=Role.project_admin,
    )
    db.add(membership)
    db.commit()
    db.refresh(project)
    logger.info(
        "Created project %s (id=%s) in tenant %s by %s",
        project.name,
        project.id,
        tenant_id,
        actor_id,
    )
    return project


def get_project(db: Session, *, user_id: uuid.UUID, project_id: uuid.UUID) -> Project:
    """Return a project after verifying the user has access."""
    assert_project_member(db, user_id, project_id)
    return assert_project_exists(db, project_id)


def update_project(
    db: Session,
    *,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
    name: str | None = None,
    description: str | None = None,
) -> Project:
    """Update a project. Requires project admin or tenant admin."""
    assert_project_admin(db, actor_id, project_id)
    project = assert_project_exists(db, project_id)
    if name is not None:
        project.name = name
    if description is not None:
        project.description = description
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ConflictError(f"Project name '{name}' is already in use") from None
    db.refresh(project)
    logger.info("Updated project %s by %s", project_id, actor_id)
    return project


def list_projects_for_user(
    db: Session, *, user_id: uuid.UUID, tenant_id: uuid.UUID | None = None
) -> list[Project]:
    """List all projects the user is a member of.

    If ``tenant_id`` is provided, filter to that tenant only.
    """
    stmt = (
        select(Project)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(ProjectMember.user_id == user_id)
        .order_by(Project.name)
    )
    if tenant_id is not None:
        stmt = stmt.where(Project.tenant_id == tenant_id)
    return list(db.scalars(stmt).all())


# =====================================================================
# Project member management
# =====================================================================


def add_project_member(
    db: Session,
    *,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
    username: str,
    role: Role,
) -> ProjectMember:
    """Add a user to a project.

    Requires ``project_admin`` or ``tenant_admin``.  The target user
    must already be a member of the tenant that owns the project.
    """
    assert_project_admin(db, actor_id, project_id)
    project = assert_project_exists(db, project_id)

    target_user = db.scalar(select(User).where(User.username == username))
    if target_user is None:
        raise NotFoundError(f"User '{username}' not found")

    # Must be a tenant member first.
    assert_tenant_member(db, target_user.id, project.tenant_id)

    existing = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == target_user.id,
        )
    )
    if existing is not None:
        raise ConflictError(f"User '{username}' is already a member of this project")

    membership = ProjectMember(
        project_id=project_id,
        user_id=target_user.id,
        role=role,
    )
    db.add(membership)
    db.commit()
    db.refresh(membership)
    logger.info(
        "Added user %s to project %s as %s (by %s)",
        username,
        project_id,
        role.value,
        actor_id,
    )
    return membership


def list_project_members(
    db: Session, *, user_id: uuid.UUID, project_id: uuid.UUID
) -> list[tuple[ProjectMember, str]]:
    """List all members of a project with their usernames."""
    assert_project_member(db, user_id, project_id)

    rows = db.execute(
        select(ProjectMember, User.username)
        .join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == project_id)
        .order_by(ProjectMember.created_at)
    ).all()
    return [(member, username) for member, username in rows]


def remove_project_member(
    db: Session,
    *,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Remove a user from a project."""
    assert_project_admin(db, actor_id, project_id)

    membership = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    )
    if membership is None:
        raise NotFoundError(f"User {user_id} is not a member of project {project_id}")

    if membership.role == Role.project_admin:
        from sqlalchemy import func

        admin_count = db.scalar(
            select(func.count())
            .select_from(ProjectMember)
            .where(
                ProjectMember.project_id == project_id,
                ProjectMember.role == Role.project_admin,
            )
        )
        if admin_count is not None and admin_count <= 1:
            raise PermissionDeniedError("Cannot remove the last project_admin of a project")

    db.delete(membership)
    db.commit()
    logger.info(
        "Removed user %s from project %s (by %s)",
        user_id,
        project_id,
        actor_id,
    )


# =====================================================================
# Data source connections -- ISOLATED queries
# =====================================================================


def list_connections(
    db: Session, *, user_id: uuid.UUID, project_id: uuid.UUID
) -> list[DataSourceConnection]:
    """List all data-source connections in a project.

    Scoped by ``tenant_id`` **and** ``project_id``.
    """
    assert_project_member(db, user_id, project_id)
    project = assert_project_exists(db, project_id)

    # DUAL FILTER: tenant_id AND project_id -- no global scan possible.
    return list(
        db.scalars(
            select(DataSourceConnection)
            .where(
                DataSourceConnection.tenant_id == project.tenant_id,
                DataSourceConnection.project_id == project_id,
            )
            .order_by(DataSourceConnection.name)
        ).all()
    )


def get_connection(
    db: Session,
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    connection_id: uuid.UUID,
) -> DataSourceConnection:
    """Get a single connection, scoped by tenant + project."""
    assert_project_member(db, user_id, project_id)
    project = assert_project_exists(db, project_id)

    conn = db.scalar(
        select(DataSourceConnection).where(
            DataSourceConnection.id == connection_id,
            # DUAL FILTER ensures isolation even if the ID is guessed.
            DataSourceConnection.tenant_id == project.tenant_id,
            DataSourceConnection.project_id == project_id,
        )
    )
    if conn is None:
        raise NotFoundError(f"Connection {connection_id} not found in project {project_id}")
    return conn


def create_connection(
    db: Session,
    *,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
    name: str,
    source_type: str,
    host: str,
    port: int,
    database_name: str,
    username: str,
    password: str,
    description: str | None = None,
) -> DataSourceConnection:
    """Create a new data-source connection.

    The plaintext ``password`` is written to Vault under a path
    derived from the connection ID.  The database stores only the
    Vault ``secret_ref`` path -- never the password itself.

    Requires ``engineer`` or higher role.
    """
    assert_project_write(db, actor_id, project_id)
    project = assert_project_exists(db, project_id)

    try:
        src_type = DataSourceType(source_type)
    except ValueError:
        raise InvalidInputError(
            f"Invalid source_type '{source_type}'. Valid types: {[t.value for t in DataSourceType]}"
        ) from None

    # Step 1: create the DB record first (we need the UUID for the Vault path).
    conn = DataSourceConnection(
        tenant_id=project.tenant_id,
        project_id=project_id,
        name=name,
        source_type=src_type,
        host=host,
        port=port,
        database_name=database_name,
        db_username=username,
        secret_ref="",  # placeholder -- updated after Vault write
        description=description,
        created_by=actor_id,
    )
    db.add(conn)
    try:
        db.flush()  # get the UUID without committing
    except IntegrityError:
        db.rollback()
        raise ConflictError(f"Connection '{name}' already exists in this project") from None

    # Step 2: write credentials to Vault.
    vault_path = VaultClient.build_secret_path(conn.id)
    vault = get_vault_client()
    secret_ref = vault.write_secret(
        vault_path,
        {"username": username, "password": password},
    )

    # Step 3: update the DB record with the Vault reference and commit.
    conn.secret_ref = secret_ref
    db.commit()
    db.refresh(conn)
    logger.info(
        "Created connection %s in project %s (by %s), vault_ref=%s",
        conn.name,
        project_id,
        actor_id,
        secret_ref,
    )
    return conn


def update_connection(
    db: Session,
    *,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
    connection_id: uuid.UUID,
    name: str | None = None,
    source_type: str | None = None,
    host: str | None = None,
    port: int | None = None,
    database_name: str | None = None,
    username: str | None = None,
    password: str | None = None,
    description: str | None = None,
) -> DataSourceConnection:
    """Update a connection and optionally rotate its Vault secret."""
    assert_project_admin(db, actor_id, project_id)
    conn = get_connection(
        db,
        user_id=actor_id,
        project_id=project_id,
        connection_id=connection_id,
    )

    if name is not None:
        conn.name = name
    if source_type is not None:
        try:
            conn.source_type = DataSourceType(source_type)
        except ValueError:
            raise InvalidInputError(
                f"Invalid source_type '{source_type}'. "
                f"Valid types: {[t.value for t in DataSourceType]}"
            ) from None
    if host is not None:
        conn.host = host
    if port is not None:
        conn.port = port
    if database_name is not None:
        conn.database_name = database_name
    if username is not None:
        conn.db_username = username
    if description is not None:
        conn.description = description

    if username is not None or password is not None:
        vault = get_vault_client()
        existing = vault.read_secret_by_ref(conn.secret_ref)
        vault.write_secret(
            VaultClient.build_secret_path(conn.id),
            {
                "username": username or str(existing.get("username", conn.db_username)),
                "password": password or str(existing.get("password", "")),
            },
        )

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ConflictError(
            f"Connection '{name or conn.name}' already exists in this project"
        ) from None
    db.refresh(conn)
    logger.info("Updated connection %s in project %s by %s", conn.id, project_id, actor_id)
    return conn


def delete_connection(
    db: Session,
    *,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
    connection_id: uuid.UUID,
) -> None:
    """Delete a connection and its Vault credentials.

    Requires ``project_admin`` or ``tenant_admin``.
    """
    assert_project_admin(db, actor_id, project_id)
    conn = get_connection(
        db,
        user_id=actor_id,
        project_id=project_id,
        connection_id=connection_id,
    )

    # Delete the secret from Vault first.
    vault = get_vault_client()
    vault.delete_secret_by_ref(conn.secret_ref)

    db.delete(conn)
    db.commit()
    logger.info(
        "Deleted connection %s in project %s (by %s), vault cleaned",
        conn.name,
        project_id,
        actor_id,
    )


def _save_test_result(
    conn: DataSourceConnection,
    status: str,
    message: str,
    latency_ms: int | None,
) -> None:
    """Persist a connection test result to the model instance.

    Caller is responsible for ``db.commit()``.
    """
    from etl_agent.models.data_source_connection import ConnectionStatus

    conn.last_test_status = ConnectionStatus(status) if isinstance(status, str) else status
    conn.last_test_message = message[:1024] if message else None
    conn.last_test_latency_ms = latency_ms
    conn.last_tested_at = datetime.now(UTC)


def test_connection(
    db: Session,
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    connection_id: uuid.UUID,
) -> tuple[bool, str, int | None]:
    """Test a data-source connection using credentials from Vault.

    Returns ``(success, message, latency_ms)``.
    The message is **sanitized** -- it never contains the password.

    The test result is persisted to the connection record so the
    frontend never has to guess the connection state.
    """
    from etl_agent.models.data_source_connection import ConnectionStatus

    assert_project_write(db, user_id, project_id)
    conn = get_connection(
        db,
        user_id=user_id,
        project_id=project_id,
        connection_id=connection_id,
    )

    # Read credentials from Vault.
    vault = get_vault_client()
    try:
        creds = vault.read_secret_by_ref(conn.secret_ref)
    except KeyError:
        msg = f"Credential not found in Vault for connection '{conn.name}'"
        _save_test_result(conn, ConnectionStatus.failed, msg, None)
        db.commit()
        db.refresh(conn)
        return False, msg, None

    db_username = creds.get("username", "")
    db_password = creds.get("password", "")

    params = ConnectionParams(
        source_type=conn.source_type.value,
        host=conn.host,
        port=conn.port,
        database_name=conn.database_name,
        username=db_username,
        password=db_password,
    )

    import time

    start = time.monotonic()
    try:
        with create_connector(params) as connector:
            ok = connector.test_connection()
        elapsed = int((time.monotonic() - start) * 1000)
        if ok:
            msg = "Connection successful"
            _save_test_result(conn, ConnectionStatus.connected, msg, elapsed)
            db.commit()
            db.refresh(conn)
            return True, msg, elapsed
        msg = "Connection test returned False"
        _save_test_result(conn, ConnectionStatus.failed, msg, elapsed)
        db.commit()
        db.refresh(conn)
        return False, msg, elapsed
    except (NotImplementedError, RuntimeError) as exc:
        elapsed = int((time.monotonic() - start) * 1000)
        from etl_agent.connectors.base import sanitize_error_message

        msg = sanitize_error_message(
            str(exc),
            password=db_password,
            username=db_username,
        )
        _save_test_result(conn, ConnectionStatus.failed, msg, elapsed)
        db.commit()
        db.refresh(conn)
        return False, msg, elapsed
    except Exception as exc:
        elapsed = int((time.monotonic() - start) * 1000)
        from etl_agent.connectors.base import sanitize_error_message

        msg = sanitize_error_message(
            str(exc),
            password=db_password,
            username=db_username,
        )
        _save_test_result(conn, ConnectionStatus.failed, msg, elapsed)
        db.commit()
        db.refresh(conn)
        return False, msg, elapsed


# =====================================================================
# Metadata profiles -- ISOLATED queries
# =====================================================================


def list_profiles(
    db: Session, *, user_id: uuid.UUID, project_id: uuid.UUID
) -> list[MetadataProfile]:
    """List all metadata profiles in a project.

    Scoped by ``tenant_id`` **and** ``project_id``.
    """
    assert_project_member(db, user_id, project_id)
    project = assert_project_exists(db, project_id)

    return list(
        db.scalars(
            select(MetadataProfile)
            .where(
                MetadataProfile.tenant_id == project.tenant_id,
                MetadataProfile.project_id == project_id,
            )
            .order_by(MetadataProfile.profiled_at.desc())
        ).all()
    )


__all__ = [
    "add_project_member",
    "create_connection",
    "create_project",
    "delete_connection",
    "get_connection",
    "get_project",
    "list_connections",
    "list_profiles",
    "list_project_members",
    "list_projects_for_user",
    "remove_project_member",
    "test_connection",
]
