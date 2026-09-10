"""Tests for authentication services and API routes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from etl_agent.core.dependencies import CurrentUser, get_current_user
from etl_agent.db.session import get_db
from etl_agent.main import app
from etl_agent.models.role import Role
from etl_agent.models.tenant_member import TenantMember
from etl_agent.models.user import User, UserStatus
from etl_agent.schemas.auth import RegisterRequest
from etl_agent.services import auth_service


def _override_db() -> Mock:
    db = Mock()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    return db


def _clear_overrides() -> None:
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


def test_hash_and_verify_password() -> None:
    hashed = auth_service.hash_password("secret123")
    assert hashed != "secret123"
    assert auth_service.verify_password("secret123", hashed)
    assert not auth_service.verify_password("wrong", hashed)


def test_create_and_decode_token() -> None:
    user_id = uuid.uuid4()
    token = auth_service.create_access_token(user_id=user_id, username="alice")
    payload = auth_service.decode_token(token)
    assert payload["sub"] == str(user_id)
    assert payload["username"] == "alice"


def test_register_schema_rejects_overlong_password() -> None:
    with pytest.raises(ValidationError):
        RegisterRequest(username="alice", password="x" * 73)


def test_register_user_duplicate_name_raises() -> None:
    db = Mock()
    db.scalar.return_value = object()
    request = RegisterRequest(username="alice", password="secret123")

    with pytest.raises(ValueError, match="already taken"):
        auth_service.register_user(db, request)


def test_register_user_handles_database_unique_conflict() -> None:
    db = Mock()
    db.scalar.return_value = None
    db.commit.side_effect = IntegrityError("INSERT", {}, Exception("duplicate key"))
    request = RegisterRequest(username="alice", password="secret123")

    with pytest.raises(ValueError, match="already taken"):
        auth_service.register_user(db, request)
    db.rollback.assert_called_once()


def test_register_route_returns_201(monkeypatch) -> None:
    user_id = uuid.uuid4()
    now = datetime.now(UTC)
    fake_user = SimpleNamespace(
        id=user_id,
        username="alice",
        status=UserStatus.active,
        created_at=now,
    )
    monkeypatch.setattr(auth_service, "register_user", lambda db, req: fake_user)
    _override_db()
    try:
        response = TestClient(app).post(
            "/api/v1/auth/register",
            json={"username": "alice", "password": "secret123"},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 201
    assert response.json()["username"] == "alice"


def test_login_route_returns_token(monkeypatch) -> None:
    user_id = uuid.uuid4()
    monkeypatch.setattr(
        auth_service,
        "login",
        lambda db, username, password, tenant_id: {
            "access_token": "token",
            "token_type": "bearer",
            "expires_in": 3600,
            "user_id": user_id,
            "username": "alice",
            "tenant_id": None,
            "project_id": None,
            "role": "owner",
        },
    )
    _override_db()
    try:
        response = TestClient(app).post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "secret123"},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json()["access_token"] == "token"


def test_me_requires_token() -> None:
    _override_db()
    try:
        response = TestClient(app).get("/api/v1/auth/me")
    finally:
        _clear_overrides()

    assert response.status_code == 401


def test_me_returns_current_user() -> None:
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=user_id,
        username="alice",
        status=UserStatus.active.value,
        tenant_id=tenant_id,
        role="owner",
    )
    try:
        response = TestClient(app).get("/api/v1/auth/me")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json()["username"] == "alice"
    assert response.json()["tenant_id"] == str(tenant_id)


def test_current_user_rejects_removed_tenant_membership() -> None:
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user = User(
        id=user_id,
        username="alice",
        password_hash="unused",
        status=UserStatus.active,
    )
    token = auth_service.create_access_token(
        user_id=user_id,
        username="alice",
        tenant_id=tenant_id,
        role="owner",
    )
    db = Mock()
    db.scalar.side_effect = [user, None]
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    with pytest.raises(HTTPException) as exc:
        get_current_user(credentials=credentials, db=db)

    assert exc.value.status_code == 403


def test_current_user_uses_live_membership_role() -> None:
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    last_login_at = datetime.now(UTC)
    user = User(
        id=user_id,
        username="alice",
        password_hash="unused",
        status=UserStatus.active,
        last_login_at=last_login_at,
    )
    membership = TenantMember(
        tenant_id=tenant_id,
        user_id=user_id,
        role=Role.tenant_admin,
    )
    token = auth_service.create_access_token(
        user_id=user_id,
        username="alice",
        tenant_id=tenant_id,
        role="auditor",
    )
    db = Mock()
    db.scalar.side_effect = [user, membership]
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    current = get_current_user(credentials=credentials, db=db)

    assert current.role == "tenant_admin"
    assert current.last_login_at == last_login_at
