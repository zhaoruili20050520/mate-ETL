"""Tests for tenant/project isolation and membership guards."""

from __future__ import annotations

import uuid
from unittest.mock import Mock

import pytest

from etl_agent.core.exceptions import PermissionDeniedError
from etl_agent.core.permissions import assert_project_member
from etl_agent.models.project import Project, ProjectStatus
from etl_agent.models.project_member import ProjectMember
from etl_agent.models.role import Role
from etl_agent.models.tenant_member import TenantMember
from etl_agent.services import project_service


def _project(tenant_id: uuid.UUID) -> Project:
    return Project(name="project-a", status=ProjectStatus.active, tenant_id=tenant_id)


def _tenant_member(user_id: uuid.UUID, tenant_id: uuid.UUID) -> TenantMember:
    return TenantMember(tenant_id=tenant_id, user_id=user_id, role=Role.tenant_admin)


def test_project_member_rejects_user_without_project_membership() -> None:
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    project_id = uuid.uuid4()
    project = _project(tenant_id)
    project.id = project_id
    db = Mock()
    db.scalar.side_effect = [project, _tenant_member(user_id, tenant_id), None]

    with pytest.raises(PermissionDeniedError):
        assert_project_member(db, user_id=user_id, project_id=project_id)


def test_project_member_returns_live_project_role() -> None:
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    project_id = uuid.uuid4()
    project = _project(tenant_id)
    project.id = project_id
    membership = ProjectMember(project_id=project_id, user_id=user_id, role=Role.engineer)
    db = Mock()
    db.scalar.side_effect = [project, _tenant_member(user_id, tenant_id), membership]

    role = assert_project_member(db, user_id=user_id, project_id=project_id)

    assert role == Role.engineer.value


def test_get_project_calls_membership_check(monkeypatch) -> None:
    def deny(db, user_id, project_id) -> None:
        raise PermissionDeniedError("not a member")

    monkeypatch.setattr(project_service, "assert_project_member", deny)

    with pytest.raises(PermissionDeniedError):
        project_service.get_project(
            db=Mock(),
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
        )


def test_remove_project_member_guards_last_admin(monkeypatch) -> None:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership = ProjectMember(project_id=project_id, user_id=user_id, role=Role.project_admin)
    db = Mock()
    db.scalar.side_effect = [membership, 1]
    monkeypatch.setattr(
        project_service,
        "assert_project_admin",
        lambda db, actor_id, project_id: Role.project_admin.value,
    )

    with pytest.raises(PermissionDeniedError):
        project_service.remove_project_member(
            db,
            actor_id=uuid.uuid4(),
            project_id=project_id,
            user_id=user_id,
        )
