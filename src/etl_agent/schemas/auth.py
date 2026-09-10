"""Pydantic schemas for authentication endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

# ---- Register ----


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)

    @field_validator("password")
    @classmethod
    def password_within_bcrypt_limit(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Password must be at most 72 bytes")
        return value


class RegisterResponse(BaseModel):
    id: uuid.UUID
    username: str
    status: str
    created_at: datetime


# ---- Login ----


class LoginRequest(BaseModel):
    username: str
    password: str
    # Optional: select which tenant context to bind into the JWT.
    # If omitted, the user's first tenant membership is used (or null).
    tenant_id: uuid.UUID | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: uuid.UUID
    username: str
    tenant_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    role: str | None = None


# ---- Current user (returned by /me) ----


class CurrentUserResponse(BaseModel):
    user_id: uuid.UUID
    username: str
    status: str
    tenant_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    role: str | None = None
    last_login_at: datetime | None = None
