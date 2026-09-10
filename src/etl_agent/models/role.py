"""Unified role enum for the entire system.

Minimal role set as specified:
  - tenant_admin: manages a tenant, can create projects and assign members
  - project_admin: manages a project, can add/remove members and connections
  - engineer: can create and run pipelines within a project
  - auditor: read-only access to project resources
"""

from __future__ import annotations

import enum


class Role(enum.StrEnum):
    tenant_admin = "tenant_admin"
    project_admin = "project_admin"
    engineer = "engineer"
    auditor = "auditor"


__all__ = ["Role"]
