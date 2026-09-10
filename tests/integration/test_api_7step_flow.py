"""End-to-end 7-step API flow test.

Walks the full frontend interaction chain:
1. Login
2. Select project
3. View connection list
4. Create new connection
5. Connection test
6. Trigger metadata exploration
7. View Schema / fields / masked sample data

Each step asserts that the response shape is directly consumable by a
frontend (unified structure, no internal fields leaked, paginated lists,
structured errors).
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

# Celery eager mode must be set BEFORE importing the app so that
# explore_metadata_task runs synchronously during the test.
from etl_agent.workers.celery_app import celery_app

celery_app.conf.update(
    task_always_eager=True,
    task_eager_propagates=True,
)

from etl_agent.main import app  # noqa: E402

client = TestClient(app)

BASE = "/api/v1"

# ── helpers ──────────────────────────────────────────────────────────


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── Step 1: Login ────────────────────────────────────────────────────


def _step1_login() -> str:
    """POST /auth/login returns TokenResponse with all fields."""
    resp = client.post(
        f"{BASE}/auth/login",
        json={"username": "admin", "password": "admin123"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Frontend needs these fields
    assert "access_token" in body
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert body["user_id"]
    assert body["username"] == "admin"
    assert body["tenant_id"]  # admin has a tenant
    assert body["role"]

    print(f"\n[Step 1] Login OK: user={body['username']}, role={body['role']}")
    return body["access_token"]


# ── Step 2: Select project ────────────────────────────────────────────


def _step2_select_project(token: str = "") -> tuple[str, str]:
    """GET /projects returns PageResponse; pick the first project."""
    if not token:
        token = _step1_login()
    resp = client.get(
        f"{BASE}/projects",
        headers=_auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # PageResponse shape
    assert "items" in body
    assert "total" in body
    assert "page" in body
    assert "page_size" in body
    assert isinstance(body["items"], list)
    assert body["total"] >= 1

    proj = body["items"][0]
    # ProjectResponse fields
    assert proj["id"]
    assert proj["name"]
    assert proj["status"]
    assert proj["tenant_id"]

    project_id = proj["id"]
    print(f"\n[Step 2] Select project OK: name={proj['name']}, total={body['total']}")
    return project_id, token


# ── Step 3: View connection list ──────────────────────────────────────


def _step3_connection_list(token: str = "", project_id: str = "") -> tuple[str, str, str]:
    """GET /connections?project_id= returns PageResponse."""
    if not token:
        token = _step1_login()
    if not project_id:
        project_id, token = _step2_select_project(token)

    resp = client.get(
        f"{BASE}/connections",
        params={"project_id": project_id},
        headers=_auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # PageResponse shape
    assert "items" in body
    assert "total" in body
    assert "page" in body
    assert "page_size" in body

    # Verify connection response fields (if any exist)
    if body["items"]:
        conn = body["items"][0]
        # DataSourceConnectionResponse fields
        assert conn["id"]
        assert conn["name"]
        assert conn["source_type"]
        assert conn["host"]
        assert conn["port"]
        assert conn["database_name"]
        assert conn["username"] is not None  # should be db_username, not ""
        assert conn["password"] is None  # never returned
        assert "secret_ref" not in conn  # internal Vault path, not exposed
        assert "last_test_status" in conn
        assert "updated_at" in conn

    print(f"\n[Step 3] Connection list OK: total={body['total']}, page={body['page']}")
    return project_id, token, ""


# ── Step 4: Create new connection ────────────────────────────────────


def _step4_create_connection(token: str = "", project_id: str = "") -> tuple[str, str, str]:
    """POST /connections creates a connection and returns it (password=None)."""
    if not token:
        token = _step1_login()
    if not project_id:
        project_id, token = _step2_select_project(token)

    unique_name = f"e2e-7step-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        f"{BASE}/connections",
        json={
            "project_id": project_id,
            "name": unique_name,
            "source_type": "postgresql",
            "host": "localhost",
            "port": 15432,
            "database_name": "etl_agent",
            "username": "etl_agent",
            "password": "changeme",
            "description": "E2E 7-step test connection",
        },
        headers=_auth_headers(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    # Response must be DataSourceConnectionResponse
    assert body["id"]
    assert body["name"] == unique_name
    assert body["source_type"] == "postgresql"
    assert body["host"] == "localhost"
    assert body["port"] == 15432
    assert body["username"] == "etl_agent"
    assert body["password"] is None  # never returned
    assert "secret_ref" not in body  # internal, not exposed
    assert body["last_test_status"] == "untested"

    conn_id = body["id"]
    print(f"\n[Step 4] Create connection OK: name={unique_name}, id={conn_id}")
    return conn_id, token, project_id


# ── Step 5: Connection test ───────────────────────────────────────────


def _step5_test_connection(
    conn_id: str = "", token: str = "", project_id: str = ""
) -> tuple[str, str, str]:
    """POST /connections/{id}/test returns ConnectionTestResponse with status."""
    if not conn_id or not token or not project_id:
        conn_id, token, project_id = _step4_create_connection()

    resp = client.post(
        f"{BASE}/connections/{conn_id}/test",
        params={"project_id": project_id},
        headers=_auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # ConnectionTestResponse fields
    assert "success" in body
    assert "message" in body
    assert "latency_ms" in body
    assert "status" in body  # the fix: status field must be present
    assert body["status"] in ("connected", "failed", "untested")
    assert body["success"] is True  # the creds are correct (etl_agent:changeme)
    assert "password" not in body.get("message", "").lower()

    # Also verify the connection list now reflects the test status
    list_resp = client.get(
        f"{BASE}/connections",
        params={"project_id": project_id},
        headers=_auth_headers(token),
    )
    assert list_resp.status_code == 200
    conns = list_resp.json()["items"]
    updated = [c for c in conns if c["id"] == conn_id]
    if updated:
        assert updated[0]["last_test_status"] == body["status"]

    print(
        f"\n[Step 5] Test connection OK: success={body['success']}, "
        f"status={body['status']}, latency={body['latency_ms']}ms"
    )
    return conn_id, token, project_id


# ── Step 6: Trigger metadata exploration ──────────────────────────────


def _step6_trigger_explore(
    conn_id: str = "", token: str = "", project_id: str = ""
) -> tuple[str, str, str]:
    """POST /connections/{id}/metadata/explore returns 202 with task info."""
    if not conn_id or not token or not project_id:
        conn_id, token, project_id = _step5_test_connection()

    resp = client.post(
        f"{BASE}/connections/{conn_id}/metadata/explore",
        json={
            "project_id": project_id,
            "max_tables": 100,
            "max_sample_rows": 50,
            "max_execution_seconds": 120,
        },
        headers=_auth_headers(token),
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()

    # ExploreResponse fields
    assert body["task_id"]
    assert body["connection_id"] == conn_id
    assert body["status"] in ("pending", "running", "completed", "failed")
    assert body["message"]

    task_id = body["task_id"]
    print(f"\n[Step 6] Trigger explore OK: task_id={task_id}, status={body['status']}")
    return conn_id, token, project_id


# ── Step 7: View Schema / fields / masked sample ─────────────────────


def _step7_view_profiles(conn_id: str = "", token: str = "", project_id: str = "") -> None:
    """GET /connections/{id}/metadata/profiles returns PageResponse with
    columns_info, value_distribution, sample_data."""
    if not conn_id or not token or not project_id:
        conn_id, token, project_id = _step6_trigger_explore()

    resp = client.get(
        f"{BASE}/connections/{conn_id}/metadata/profiles",
        params={"project_id": project_id},
        headers=_auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # PageResponse shape
    assert "items" in body
    assert "total" in body
    assert "page" in body
    assert "page_size" in body
    assert isinstance(body["items"], list)
    assert body["total"] >= 1

    profile = body["items"][0]
    # MetadataProfileResponse fields
    assert profile["id"]
    assert profile["connection_id"] == conn_id
    assert profile["schema_name"]
    assert profile["table_name"]
    assert profile["columns_info"] is not None
    assert profile["profiled_at"]

    # columns_info: list of {name, type, nullable, ...}
    cols = profile["columns_info"]
    assert isinstance(cols, list)
    assert len(cols) > 0
    first_col = cols[0]
    assert "name" in first_col
    assert "type" in first_col

    # sample_data: list of dicts (masked)
    samples = profile.get("sample_data")
    if samples:
        assert isinstance(samples, list)

    # Check masking: password_hash field should be masked if present
    if samples:
        for row in samples:
            for key, val in row.items():
                if "password" in key.lower() and val:
                    assert val != "changeme"  # must not be the real password
                    assert "*" in str(val) or val == "*****"

    # Also test the task status endpoint
    trigger_resp = client.post(
        f"{BASE}/connections/{conn_id}/metadata/explore",
        json={
            "project_id": project_id,
            "max_tables": 5,
            "max_sample_rows": 5,
            "max_execution_seconds": 60,
        },
        headers=_auth_headers(token),
    )
    trigger_resp.raise_for_status()
    task_id = trigger_resp.json()["task_id"]
    resp2 = client.get(
        f"{BASE}/connections/{conn_id}/metadata/tasks/{task_id}",
        params={"project_id": project_id},
        headers=_auth_headers(token),
    )
    # This may be 200 (found) or 404 (already completed and cleaned up)
    if resp2.status_code == 200:
        task_body = resp2.json()
        assert task_body["id"]
        assert task_body["status"] in ("pending", "running", "completed", "failed")

    print(
        f"\n[Step 7] View profiles OK: total={body['total']} tables, "
        f"first table={profile['table_name']}, "
        f"columns={len(cols)}"
    )


# ── Error format test ────────────────────────────────────────────────


def test_error_format_no_token() -> None:
    """Calling a protected endpoint without a token returns structured error."""
    resp = client.get(f"{BASE}/projects")
    assert resp.status_code in (401, 403), resp.text
    body = resp.json()
    # The error should be structured {error: {code, message, details}}
    # or FastAPI's default {detail: "..."} for HTTPException
    # We accept both but log the shape for visibility
    if "error" in body:
        assert body["error"]["code"]
        assert body["error"]["message"]
    elif "detail" in body:
        assert body["detail"]
    print(f"\n[Error] No-token response: status={resp.status_code}, keys={list(body.keys())}")


# ── Pagination param test ─────────────────────────────────────────────


def test_pagination_params(token: str = "") -> None:
    """Verify page and page_size query params work on list endpoints."""
    if not token:
        token = _step1_login()
    resp = client.get(
        f"{BASE}/projects",
        params={"page": 1, "page_size": 2},
        headers=_auth_headers(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["page"] == 1
    assert body["page_size"] == 2
    assert len(body["items"]) <= 2
    print(
        f"\n[Pagination] page={body['page']}, page_size={body['page_size']}, "
        f"items={len(body['items'])}, total={body['total']}"
    )


# ── Test wrappers ────────────────────────────────────────────────────


def test_step1_login() -> None:
    _step1_login()


def test_step2_select_project() -> None:
    _step2_select_project()


def test_step3_connection_list() -> None:
    _step3_connection_list()


def test_step4_create_connection() -> None:
    _step4_create_connection()


def test_step5_test_connection() -> None:
    _step5_test_connection()


def test_step6_trigger_explore() -> None:
    _step6_trigger_explore()


def test_step7_view_profiles() -> None:
    _step7_view_profiles()
