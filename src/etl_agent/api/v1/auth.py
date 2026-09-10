"""Authentication routes: register, login, current-user."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from etl_agent.core.dependencies import CurrentUser, get_current_user
from etl_agent.db.session import get_db
from etl_agent.schemas.auth import (
    CurrentUserResponse,
    LoginRequest,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
)
from etl_agent.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(
    req: RegisterRequest,
    db: Session = Depends(get_db),  # noqa: B008
) -> RegisterResponse:
    """Register a new user account."""
    try:
        user = auth_service.register_user(db, req)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return RegisterResponse(
        id=user.id,
        username=user.username,
        status=user.status.value,
        created_at=user.created_at,
    )


@router.post("/login", response_model=TokenResponse)
def login(
    req: LoginRequest,
    db: Session = Depends(get_db),  # noqa: B008
) -> TokenResponse:
    """Authenticate and return a JWT access token."""
    try:
        result = auth_service.login(db, req.username, req.password, req.tenant_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return TokenResponse(**result)


@router.get("/me", response_model=CurrentUserResponse)
def me(
    current: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> CurrentUserResponse:
    """Return the authenticated user's context."""
    return CurrentUserResponse(
        user_id=current.user_id,
        username=current.username,
        status=current.status,
        tenant_id=current.tenant_id,
        project_id=current.project_id,
        role=current.role,
        last_login_at=current.last_login_at,
    )
