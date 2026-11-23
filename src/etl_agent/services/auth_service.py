"""Authentication service: password hashing, JWT, registration, login, seed user."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import bcrypt
from jose import JWTError, jwt  # type: ignore[import-untyped]
from sqlalchemy import case, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from etl_agent.models.knowledge import KnowledgeMember, KnowledgeRole
from etl_agent.models.project import Project, ProjectStatus
from etl_agent.models.project_member import ProjectMember
from etl_agent.models.role import Role
from etl_agent.models.tenant import Tenant, TenantStatus
from etl_agent.models.tenant_member import TenantMember
from etl_agent.models.user import User, UserStatus
from etl_agent.schemas.auth import RegisterRequest
from etl_agent.settings import settings

logger = logging.getLogger(__name__)

# ---- Password hashing (bcrypt direct, bypassing passlib compat issues) ----


def hash_password(plain: str) -> str:
    """Hash a plaintext password using bcrypt."""
    if len(plain.encode("utf-8")) > 72:
        raise ValueError("Password must be at most 72 bytes")
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a stored bcrypt hash."""
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


# ---- JWT ----

ALGORITHM = settings.jwt_algorithm
SECRET_KEY = settings.jwt_secret_key
# 0 = tokens never expire (no ``exp`` claim is written).
EXPIRE_MINUTES = settings.jwt_expire_minutes


def create_access_token(
    *,
    user_id: uuid.UUID,
    username: str,
    tenant_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    role: str | None = None,
) -> str:
    """Sign a JWT with user identity and context information.

    When ``JWT_EXPIRE_MINUTES`` is 0 the token carries no ``exp`` claim, so it
    never expires (it only becomes invalid if ``JWT_SECRET_KEY`` is rotated).
    """
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "username": username,
        "tenant_id": str(tenant_id) if tenant_id else None,
        "project_id": str(project_id) if project_id else None,
        "role": role,
        "iat": int(now.timestamp()),
    }
    if EXPIRE_MINUTES > 0:
        payload["exp"] = int((now + timedelta(minutes=EXPIRE_MINUTES)).timestamp())
    return cast(str, jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM))


def decode_token(token: str) -> dict[str, Any]:
    """Decode and verify a JWT, returning the full payload."""
    try:
        return cast(dict[str, Any], jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]))
    except JWTError as exc:
        logger.warning("JWT decode failed: %s", exc)
        raise ValueError("Invalid or expired token") from exc


# ---- User operations ----


def register_user(db: Session, req: RegisterRequest) -> User:
    """Create a new user with a bcrypt-hashed password.

    Raises ``ValueError`` if the username already exists.
    """
    existing = db.scalar(select(User).where(User.username == req.username))
    if existing is not None:
        raise ValueError(f"Username '{req.username}' is already taken")

    user = User(
        username=req.username,
        password_hash=hash_password(req.password),
        status=UserStatus.active,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ValueError(f"Username '{req.username}' is already taken") from None
    db.refresh(user)
    logger.info("Registered new user: %s (id=%s)", user.username, user.id)
    return user


def authenticate(db: Session, username: str, password: str) -> User:
    """Verify credentials and return the user, or raise ``ValueError``."""
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        raise ValueError("Invalid username or password")
    if not verify_password(password, user.password_hash):
        raise ValueError("Invalid username or password")
    if user.status != UserStatus.active:
        raise ValueError(f"User account is {user.status.value}")

    # Update last_login_at
    user.last_login_at = datetime.now(UTC)
    db.commit()
    db.refresh(user)
    return user


def get_user_context(
    db: Session, user_id: uuid.UUID, tenant_id: uuid.UUID | None = None
) -> tuple[uuid.UUID | None, str | None]:
    """Resolve the tenant_id and role for the user.

    If ``tenant_id`` is provided, verify the membership and return that role.
    Otherwise, pick the first tenant membership available.
    Returns ``(tenant_id, role)`` or ``(None, None)`` if no membership exists.
    """
    if tenant_id is not None:
        membership = db.scalar(
            select(TenantMember).where(
                TenantMember.tenant_id == tenant_id,
                TenantMember.user_id == user_id,
            )
        )
        if membership is None:
            raise ValueError("User is not a member of the specified tenant")
        return tenant_id, membership.role.value

    memberships = db.scalars(
        select(TenantMember)
        .join(Tenant, Tenant.id == TenantMember.tenant_id)
        .where(TenantMember.user_id == user_id)
        .order_by(
            case((Tenant.name == "default", 0), else_=1),
            Tenant.created_at.asc(),
        ),
    ).all()
    if not memberships:
        return None, None
    best = memberships[0]
    return best.tenant_id, best.role.value


def login(
    db: Session, username: str, password: str, tenant_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """Authenticate user, resolve context, and sign JWT.

    Returns a dict suitable for ``TokenResponse``.
    """
    user = authenticate(db, username, password)
    resolved_tenant_id, role = get_user_context(db, user.id, tenant_id)
    token = create_access_token(
        user_id=user.id,
        username=user.username,
        tenant_id=resolved_tenant_id,
        project_id=None,
        role=role,
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": EXPIRE_MINUTES * 60,  # 0 = the token never expires
        "user_id": user.id,
        "username": user.username,
        "tenant_id": resolved_tenant_id,
        "project_id": None,
        "role": role,
    }


# ---- Seed user ----


def seed_admin_user(db: Session) -> None:
    """Create a default admin user + tenant if none exist.

    Credentials: admin / admin123
    """
    existing = db.scalar(select(User).where(User.username == "admin"))
    if existing is not None:
        logger.info("Seed admin user already exists, skipping")
        return

    tenant = db.scalar(select(Tenant).where(Tenant.name == "default"))
    if tenant is None:
        tenant = Tenant(name="default", status=TenantStatus.active)
        db.add(tenant)
        db.flush()

    admin = User(
        username="admin",
        password_hash=hash_password("admin123"),
        status=UserStatus.active,
    )
    db.add(admin)
    db.flush()

    membership = TenantMember(
        tenant_id=tenant.id,
        user_id=admin.id,
        role=Role.tenant_admin,
    )
    db.add(membership)
    db.commit()
    logger.info("Seed admin user created (username=admin, password=admin123)")


def seed_rbac_users(db: Session) -> dict[str, str]:
    """Create the 2.8.2 role accounts on the default tenant/project.

    The ETL control plane currently has four project roles
    (engineer / project_admin / auditor), while approval roles are
    modeled as separate approval slots. The five 2.8.2 responsibilities
    are therefore mapped onto those primitives as follows:

    - data_engineer  -> engineer
    - data_checker   -> engineer + data approval slot
    - security_checker -> engineer + security approval slot
    - system_operator -> project_admin
    - auditor        -> auditor
    """
    tenant = db.scalar(select(Tenant).where(Tenant.name == "default"))
    if tenant is None:
        tenant = Tenant(name="default", status=TenantStatus.active)
        db.add(tenant)
        db.flush()

    project = db.scalar(
        select(Project).where(
            Project.tenant_id == tenant.id,
            Project.name == "RBAC演示项目",
        ),
    )
    if project is None:
        project = Project(
            tenant_id=tenant.id,
            name="RBAC演示项目",
            description="用于 2.8.2 用户角色与职责权限演示",
            status=ProjectStatus.active,
        )
        db.add(project)
        db.flush()

    role_accounts: list[tuple[str, str, Role, Role, KnowledgeRole]] = [
        (
            "data_engineer",
            "Maker@2026Etl",
            Role.engineer,
            Role.engineer,
            KnowledgeRole.knowledge_admin,
        ),
        (
            "data_checker",
            "Checker1@2026Etl",
            Role.engineer,
            Role.engineer,
            KnowledgeRole.knowledge_admin,
        ),
        (
            "security_checker",
            "Checker2@2026Etl",
            Role.engineer,
            Role.engineer,
            KnowledgeRole.system_admin,
        ),
        (
            "system_operator",
            "Operator@2026Etl",
            Role.project_admin,
            Role.project_admin,
            KnowledgeRole.system_admin,
        ),
        (
            "auditor",
            "Auditor@2026Etl",
            Role.auditor,
            Role.auditor,
            KnowledgeRole.system_admin,
        ),
    ]
    credentials: dict[str, str] = {}

    for username, password, tenant_role, project_role, knowledge_role in role_accounts:
        user = db.scalar(select(User).where(User.username == username))
        if user is None:
            user = User(
                username=username,
                password_hash=hash_password(password),
                status=UserStatus.active,
            )
            db.add(user)
            db.flush()
        else:
            user.password_hash = hash_password(password)
            user.status = UserStatus.active

        membership = db.scalar(
            select(TenantMember).where(
                TenantMember.tenant_id == tenant.id,
                TenantMember.user_id == user.id,
            ),
        )
        if membership is None:
            db.add(
                TenantMember(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    role=tenant_role,
                ),
            )
        else:
            membership.role = tenant_role

        project_membership = db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == user.id,
            ),
        )
        if project_membership is None:
            db.add(
                ProjectMember(
                    project_id=project.id,
                    user_id=user.id,
                    role=project_role,
                ),
            )
        else:
            project_membership.role = project_role

        knowledge_membership = db.scalar(
            select(KnowledgeMember).where(
                KnowledgeMember.tenant_id == tenant.id,
                KnowledgeMember.user_id == user.id,
            ),
        )
        if knowledge_membership is None:
            db.add(
                KnowledgeMember(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    role=knowledge_role,
                ),
            )
        else:
            knowledge_membership.role = knowledge_role

        credentials[username] = password

    # The seeded tenant admin must also be able to open and edit the
    # dedicated demo project created above.
    admin_user = db.scalar(select(User).where(User.username == "admin"))
    if admin_user is not None:
        admin_project_membership = db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == admin_user.id,
            ),
        )
        if admin_project_membership is None:
            db.add(
                ProjectMember(
                    project_id=project.id,
                    user_id=admin_user.id,
                    role=Role.project_admin,
                ),
            )
        else:
            admin_project_membership.role = Role.project_admin

    db.commit()
    logger.info("Seeded RBAC demo users on project %s", project.id)
    return credentials
