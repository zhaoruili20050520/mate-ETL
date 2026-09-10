"""Shared FastAPI dependencies for authentication and authorization."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from etl_agent.db.session import get_db
from etl_agent.models.project import Project
from etl_agent.models.project_member import ProjectMember
from etl_agent.models.tenant_member import TenantMember
from etl_agent.models.user import User, UserStatus
from etl_agent.services.auth_service import decode_token

# Bearer token extractor -- auto-reads the Authorization header
_security_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    """Resolved user context injected into every protected route."""

    user_id: uuid.UUID
    username: str
    status: str
    tenant_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    role: str | None = None
    last_login_at: datetime | None = None


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security_scheme),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> CurrentUser:
    """Validate the Bearer token and return the resolved user context.

    Raises ``401 Unauthorized`` if the token is missing, malformed,
    expired, or the user no longer exists / is inactive.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decode_token(credentials.credentials)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user_id_str = payload.get("sub")
    if not user_id_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token payload missing user identifier",
        )

    try:
        user_id = uuid.UUID(user_id_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token payload contains invalid user identifier",
        ) from None

    user = db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    if user.status != UserStatus.active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User account is {user.status.value}",
        )

    # Resolve context fields from JWT payload
    tenant_id_raw = payload.get("tenant_id")
    tenant_id = uuid.UUID(tenant_id_raw) if tenant_id_raw else None

    project_id_raw = payload.get("project_id")
    project_id = uuid.UUID(project_id_raw) if project_id_raw else None

    resolved_role: str | None = (
        payload.get("role") if isinstance(payload.get("role"), str) else None
    )

    if tenant_id is not None:
        tenant_membership = db.scalar(
            select(TenantMember).where(
                TenantMember.tenant_id == tenant_id,
                TenantMember.user_id == user.id,
            )
        )
        if tenant_membership is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Tenant membership is no longer valid",
            )
        resolved_role = tenant_membership.role.value

    if project_id is not None:
        if tenant_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Project access requires a tenant context",
            )
        project = db.scalar(select(Project).where(Project.id == project_id))
        if project is None or project.tenant_id != tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Project does not belong to the active tenant",
            )
        project_membership = db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user.id,
            )
        )
        if project_membership is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Project membership is no longer valid",
            )
        resolved_role = project_membership.role.value

    return CurrentUser(
        user_id=user.id,
        username=user.username,
        status=user.status.value,
        tenant_id=tenant_id,
        project_id=project_id,
        role=resolved_role,
        last_login_at=user.last_login_at,
    )
