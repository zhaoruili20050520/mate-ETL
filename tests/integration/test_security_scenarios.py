"""Comprehensive integration tests covering 7 security & functionality scenarios.

Each test section is self-contained and labelled with a banner comment.
The tests use the real PostgreSQL, Vault, and MySQL containers; Celery
runs in eager mode so metadata exploration completes synchronously.

Prerequisites (containers must be running):
    - PostgreSQL: localhost:15432  (etl_agent / changeme / etl_agent)
    - Vault:       localhost:8200   (token=root)
    - MySQL:       localhost:3307   (testuser / testpass / testdb)
    - Redis:       localhost:6380
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

# Celery eager mode BEFORE importing the app
from etl_agent.workers.celery_app import celery_app

celery_app.conf.update(task_always_eager=True, task_eager_propagates=True)

from etl_agent.db.session import engine as app_engine  # noqa: E402
from etl_agent.main import app  # noqa: E402

client = TestClient(app)
BASE = "/api/v1"

# Container connection constants
PG_HOST = "localhost"
PG_PORT = 15432
PG_USER = "etl_agent"
PG_PASS = "changeme"
PG_DB = "etl_agent"

MYSQL_HOST = "localhost"
MYSQL_PORT = 3307
MYSQL_USER = "testuser"
MYSQL_PASS = "testpass"
MYSQL_DB = "testdb"


# =====================================================================
# Helpers
# =====================================================================


def _login(username: str = "admin", password: str = "admin123") -> str:
    """Login and return the JWT access_token."""
    resp = client.post(
        f"{BASE}/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register_user(username: str, password: str = "TestPass123") -> None:
    """Register a new user via the API."""
    resp = client.post(
        f"{BASE}/auth/register",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 201, f"Register failed: {resp.text}"


def _create_tenant(token: str, name: str) -> str:
    """Create a new tenant; return its UUID."""
    resp = client.post(f"{BASE}/tenants", json={"name": name}, headers=_auth(token))
    assert resp.status_code == 201, f"Create tenant failed: {resp.text}"
    return resp.json()["id"]


def _create_project(token: str, tenant_id: str, name: str) -> str:
    """Create a project within a tenant; return its UUID."""
    resp = client.post(
        f"{BASE}/projects",
        json={"tenant_id": tenant_id, "name": name},
        headers=_auth(token),
    )
    assert resp.status_code == 201, f"Create project failed: {resp.text}"
    return resp.json()["id"]


def _add_tenant_member(token: str, tenant_id: str, username: str, role: str = "engineer") -> None:
    """Add a user to a tenant."""
    resp = client.post(
        f"{BASE}/tenants/{tenant_id}/members",
        json={"username": username, "role": role},
        headers=_auth(token),
    )
    assert resp.status_code == 201, f"Add tenant member failed: {resp.text}"


def _add_project_member(token: str, project_id: str, username: str, role: str = "engineer") -> None:
    """Add a user to a project."""
    resp = client.post(
        f"{BASE}/projects/{project_id}/members",
        json={"username": username, "role": role},
        headers=_auth(token),
    )
    assert resp.status_code == 201, f"Add project member failed: {resp.text}"


def _create_connection(
    token: str,
    project_id: str,
    name: str,
    source_type: str = "postgresql",
    host: str = PG_HOST,
    port: int = PG_PORT,
    database_name: str = PG_DB,
    username: str = PG_USER,
    password: str = PG_PASS,
) -> str:
    """Create a connection via POST /connections; return its UUID."""
    resp = client.post(
        f"{BASE}/connections",
        json={
            "project_id": project_id,
            "name": name,
            "source_type": source_type,
            "host": host,
            "port": port,
            "database_name": database_name,
            "username": username,
            "password": password,
            "description": "integration test",
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201, f"Create connection failed: {resp.text}"
    return resp.json()["id"]


# =====================================================================
# 1. Login success / failure
# =====================================================================


class TestLogin:
    """Scenario 1: login with valid and invalid credentials."""

    def test_login_success_returns_jwt(self) -> None:
        """admin/admin123 logs in successfully and receives a JWT."""
        resp = client.post(
            f"{BASE}/auth/login",
            json={"username": "admin", "password": "admin123"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["access_token"]
        assert body["token_type"] == "bearer"
        assert body["expires_in"] > 0
        assert body["username"] == "admin"
        assert body["tenant_id"] is not None
        assert body["role"] == "tenant_admin"

    def test_login_wrong_password_returns_401(self) -> None:
        """Wrong password returns 401."""
        resp = client.post(
            f"{BASE}/auth/login",
            json={"username": "admin", "password": "WRONG-PASSWORD-XXX"},
        )
        assert resp.status_code == 401
        # Error body must be structured (either {detail} or {error})
        body = resp.json()
        assert "detail" in body or "error" in body

    def test_login_nonexistent_user_returns_401(self) -> None:
        """Non-existent user returns 401 (same error to prevent enumeration)."""
        resp = client.post(
            f"{BASE}/auth/login",
            json={"username": "no_such_user_99999", "password": "anything"},
        )
        assert resp.status_code == 401

    def test_login_without_token_is_unauthorized(self) -> None:
        """Accessing /me without a token returns 401/403."""
        resp = client.get(f"{BASE}/auth/me")
        assert resp.status_code in (401, 403)


# =====================================================================
# 2. Cross-tenant isolation
# =====================================================================


class TestCrossTenantIsolation:
    """Scenario 2: a user in tenant A cannot access tenant B's resources."""

    def setup_method(self) -> None:
        """Create a second tenant + project + connection as admin,
        then register a user who is only in the 'default' tenant."""
        self.admin_token = _login()
        self.suffix = uuid.uuid4().hex[:8]

        # Create a second tenant
        self.other_tenant_id = _create_tenant(self.admin_token, f"cross-tenant-{self.suffix}")

        # Create a project in the second tenant
        self.other_project_id = _create_project(
            self.admin_token, self.other_tenant_id, f"ct-proj-{self.suffix}"
        )

        # Create a connection in the second tenant's project
        self.other_conn_id = _create_connection(
            self.admin_token,
            self.other_project_id,
            f"ct-conn-{self.suffix}",
        )

        # Register a user and add only to the 'default' tenant
        self.user_name = f"ctuser-{self.suffix}"
        _register_user(self.user_name)
        # admin adds this user to 'default' tenant as engineer
        resp = client.get(
            f"{BASE}/tenants",
            params={"page_size": 500},
            headers=_auth(self.admin_token),
        )
        default_tenant = next(t for t in resp.json()["items"] if t["name"] == "default")
        self.default_tenant_id = default_tenant["id"]
        _add_tenant_member(self.admin_token, self.default_tenant_id, self.user_name, "engineer")

        # Create a dedicated project in the default tenant for this test
        self.default_project_id = _create_project(
            self.admin_token,
            self.default_tenant_id,
            f"ct-default-proj-{self.suffix}",
        )
        _add_project_member(self.admin_token, self.default_project_id, self.user_name, "engineer")

        # Login as the isolated user
        self.user_token = _login(self.user_name, "TestPass123")

    def test_user_cannot_list_other_tenant_project(self) -> None:
        """User not in tenant B cannot see tenant B's project."""
        # Try to list connections in the other tenant's project
        resp = client.get(
            f"{BASE}/connections",
            params={"project_id": self.other_project_id},
            headers=_auth(self.user_token),
        )
        assert resp.status_code in (403, 404)

    def test_user_cannot_test_other_tenant_connection(self) -> None:
        """User not in tenant B cannot test tenant B's connection."""
        resp = client.post(
            f"{BASE}/connections/{self.other_conn_id}/test",
            params={"project_id": self.other_project_id},
            headers=_auth(self.user_token),
        )
        assert resp.status_code in (403, 404)

    def test_user_cannot_explore_other_tenant_connection(self) -> None:
        """User not in tenant B cannot trigger exploration on tenant B's connection."""
        resp = client.post(
            f"{BASE}/connections/{self.other_conn_id}/metadata/explore",
            json={
                "project_id": self.other_project_id,
                "max_tables": 5,
                "max_sample_rows": 5,
                "max_execution_seconds": 60,
            },
            headers=_auth(self.user_token),
        )
        assert resp.status_code in (403, 404)

    def test_user_cannot_view_other_tenant_profiles(self) -> None:
        """User not in tenant B cannot view profiles in tenant B's project."""
        resp = client.get(
            f"{BASE}/connections/{self.other_conn_id}/metadata/profiles",
            params={"project_id": self.other_project_id},
            headers=_auth(self.user_token),
        )
        assert resp.status_code in (403, 404)


# =====================================================================
# 3. Password safety: no plaintext password in DB
# =====================================================================


class TestPasswordSafety:
    """Scenario 3: after creating a connection, the DB has no plaintext password."""

    def test_db_has_no_plaintext_password(self) -> None:
        """Create a connection with a known password, then scan every text
        column in data_source_connections for that password string."""
        token = _login()
        suffix = uuid.uuid4().hex[:8]
        project_id = _create_project(token, _get_default_tenant_id(token), f"pwd-safety-{suffix}")

        secret_password = f"MySecret-{suffix}-Password123!"
        conn_id = _create_connection(
            token,
            project_id,
            f"pwd-test-{suffix}",
            password=secret_password,
        )

        # Query the DB directly for this connection row
        with app_engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT name, host, port, database_name, db_username, "
                    "secret_ref, description "
                    "FROM data_source_connections WHERE id = :cid"
                ),
                {"cid": conn_id},
            )
            row = result.fetchone()
            assert row is not None, "Connection not found in DB"

        # Verify: the plaintext password must NOT appear in ANY column
        for col_name, col_value in zip(row._fields, row, strict=False):
            if col_value is not None:
                assert secret_password not in str(col_value), (
                    f"Plaintext password found in column '{col_name}'! Value: {col_value!r}"
                )

        # Verify: secret_ref points to Vault (contains 'ds/')
        assert "ds/" in row.secret_ref or "etl-agent" in row.secret_ref

        # Verify: db_username is stored (non-sensitive)
        assert row.db_username == PG_USER

        # Verify: API response never returns the password
        resp = client.get(
            f"{BASE}/connections",
            params={"project_id": project_id},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        for item in resp.json()["items"]:
            assert item.get("password") is None
            assert "secret_ref" not in item

    def test_get_default_tenant_id(self) -> None:
        """Sanity: the helper works."""
        token = _login()
        tid = _get_default_tenant_id(token)
        assert tid is not None


def _get_default_tenant_id(token: str) -> str:
    """Return the UUID of the 'default' tenant."""
    resp = client.get(
        f"{BASE}/tenants",
        params={"page_size": 500},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    for t in resp.json()["items"]:
        if t["name"] == "default":
            return t["id"]
    raise RuntimeError("Default tenant not found")


# =====================================================================
# 4. Vault unavailable: connection test fails
# =====================================================================


class TestVaultUnavailable:
    """Scenario 4: when Vault is down, connection test returns failure
    with a sanitized message (no password leak)."""

    def test_vault_down_connection_test_fails(self) -> None:
        """Mock VaultClient.read_secret_by_ref to raise, then verify:
        - test returns success=False, status='failed'
        - error message does NOT contain the password."""
        token = _login()
        suffix = uuid.uuid4().hex[:8]
        project_id = _create_project(token, _get_default_tenant_id(token), f"vault-down-{suffix}")

        secret_password = f"VaultSecret-{suffix}-123"
        conn_id = _create_connection(
            token,
            project_id,
            f"vault-down-conn-{suffix}",
            password=secret_password,
        )

        # Mock Vault to simulate an outage
        mock_vault = MagicMock()
        mock_vault.read_secret_by_ref.side_effect = KeyError("Secret not found (Vault may be down)")

        with patch(
            "etl_agent.services.project_service.get_vault_client",
            return_value=mock_vault,
        ):
            resp = client.post(
                f"{BASE}/connections/{conn_id}/test",
                params={"project_id": project_id},
                headers=_auth(token),
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is False
        assert body["status"] == "failed"
        # CRITICAL: the password must never appear in the response
        assert secret_password not in body["message"]
        assert secret_password not in resp.text

    def test_vault_down_message_is_sanitized(self) -> None:
        """Even if the connector error somehow contains credentials,
        the sanitize_error_message function strips them."""
        token = _login()
        suffix = uuid.uuid4().hex[:8]
        project_id = _create_project(
            token, _get_default_tenant_id(token), f"vault-sanitize-{suffix}"
        )

        secret_password = f"SanitizeMe-{suffix}-Pass!"
        conn_id = _create_connection(
            token,
            project_id,
            f"vault-san-conn-{suffix}",
            password=secret_password,
        )

        # Mock the connector to raise an exception that includes the password.
        # This hits the `except Exception` path in test_connection,
        # which calls sanitize_error_message to strip credentials.
        mock_connector_cm = MagicMock()
        mock_connector_cm.__enter__.return_value.test_connection.side_effect = RuntimeError(
            f"Connection failed with password={secret_password} user=admin"
        )

        with patch(
            "etl_agent.services.project_service.create_connector",
            return_value=mock_connector_cm,
        ):
            resp = client.post(
                f"{BASE}/connections/{conn_id}/test",
                params={"project_id": project_id},
                headers=_auth(token),
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is False
        assert secret_password not in resp.text


# =====================================================================
# 5. MySQL / PostgreSQL / Doris connection tests
# =====================================================================


class TestMultiDatabaseConnections:
    """Scenario 5: test all three connector types."""

    def setup_method(self) -> None:
        self.token = _login()
        self.suffix = uuid.uuid4().hex[:8]
        self.project_id = _create_project(
            self.token, _get_default_tenant_id(self.token), f"multi-db-{self.suffix}"
        )

    def test_postgresql_connection_succeeds(self) -> None:
        """PostgreSQL connector (psycopg v3) connects to the real container."""
        conn_id = _create_connection(
            self.token,
            self.project_id,
            f"pg-conn-{self.suffix}",
            source_type="postgresql",
            host=PG_HOST,
            port=PG_PORT,
            database_name=PG_DB,
            username=PG_USER,
            password=PG_PASS,
        )
        resp = client.post(
            f"{BASE}/connections/{conn_id}/test",
            params={"project_id": self.project_id},
            headers=_auth(self.token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True, f"PG test failed: {body['message']}"
        assert body["status"] == "connected"
        assert body["latency_ms"] is not None
        assert body["latency_ms"] >= 0

    def test_mysql_connection_succeeds(self) -> None:
        """MySQL connector (pymysql) connects to the real MySQL container."""
        conn_id = _create_connection(
            self.token,
            self.project_id,
            f"mysql-conn-{self.suffix}",
            source_type="mysql",
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            database_name=MYSQL_DB,
            username=MYSQL_USER,
            password=MYSQL_PASS,
        )
        resp = client.post(
            f"{BASE}/connections/{conn_id}/test",
            params={"project_id": self.project_id},
            headers=_auth(self.token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True, f"MySQL test failed: {body['message']}"
        assert body["status"] == "connected"
        assert body["latency_ms"] is not None

    def test_doris_connection_succeeds(self) -> None:
        """Doris connector (inherits MySQL, uses pymysql) connects to MySQL container
        (Doris uses MySQL wire protocol, so SELECT 1 works against MySQL)."""
        conn_id = _create_connection(
            self.token,
            self.project_id,
            f"doris-conn-{self.suffix}",
            source_type="doris",
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            database_name=MYSQL_DB,
            username=MYSQL_USER,
            password=MYSQL_PASS,
        )
        resp = client.post(
            f"{BASE}/connections/{conn_id}/test",
            params={"project_id": self.project_id},
            headers=_auth(self.token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True, f"Doris test failed: {body['message']}"
        assert body["status"] == "connected"
        assert body["latency_ms"] is not None

    def test_bad_credentials_connection_fails(self) -> None:
        """Connection with wrong password returns failure."""
        conn_id = _create_connection(
            self.token,
            self.project_id,
            f"bad-conn-{self.suffix}",
            source_type="postgresql",
            host=PG_HOST,
            port=PG_PORT,
            database_name=PG_DB,
            username=PG_USER,
            password="DEFINITELY-WRONG-PASSWORD",
        )
        resp = client.post(
            f"{BASE}/connections/{conn_id}/test",
            params={"project_id": self.project_id},
            headers=_auth(self.token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert body["status"] == "failed"
        # The wrong password must NOT appear in the message
        assert "DEFINITELY-WRONG-PASSWORD" not in body["message"]
        assert "DEFINITELY-WRONG-PASSWORD" not in resp.text


# =====================================================================
# 6. Metadata exploration returns Schema and masked sample data
# =====================================================================


class TestMetadataExploration:
    """Scenario 6: explore_metadata returns columns_info with types,
    and sample_data with sensitive fields masked."""

    def setup_method(self) -> None:
        self.token = _login()
        self.suffix = uuid.uuid4().hex[:8]
        self.project_id = _create_project(
            self.token, _get_default_tenant_id(self.token), f"meta-test-{self.suffix}"
        )

    def test_explore_pg_returns_schema_and_columns(self) -> None:
        """Exploring the app's own PostgreSQL returns schema info."""
        conn_id = _create_connection(
            self.token,
            self.project_id,
            f"explore-pg-{self.suffix}",
        )
        # Trigger exploration
        resp = client.post(
            f"{BASE}/connections/{conn_id}/metadata/explore",
            json={
                "project_id": self.project_id,
                "max_tables": 100,
                "max_sample_rows": 10,
                "max_execution_seconds": 120,
            },
            headers=_auth(self.token),
        )
        assert resp.status_code == 202
        task_body = resp.json()
        assert task_body["task_id"]

        # Fetch profiles
        resp = client.get(
            f"{BASE}/connections/{conn_id}/metadata/profiles",
            params={"project_id": self.project_id},
            headers=_auth(self.token),
        )
        assert resp.status_code == 200
        profiles = resp.json()["items"]
        assert len(profiles) > 0, "No profiles returned"

        # Verify each profile has the expected structure
        for p in profiles:
            assert p["schema_name"]
            assert p["table_name"]
            assert p["columns_info"] is not None
            assert isinstance(p["columns_info"], list)
            assert len(p["columns_info"]) > 0
            # Each column info must have at least name and type
            for col in p["columns_info"]:
                assert "name" in col
                assert "type" in col

    def test_explore_mysql_masks_sensitive_fields(self) -> None:
        """Exploring the MySQL test_users table masks password_hash,
        email, and phone columns in sample_data."""
        conn_id = _create_connection(
            self.token,
            self.project_id,
            f"explore-mysql-{self.suffix}",
            source_type="mysql",
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            database_name=MYSQL_DB,
            username=MYSQL_USER,
            password=MYSQL_PASS,
        )

        # Trigger exploration
        resp = client.post(
            f"{BASE}/connections/{conn_id}/metadata/explore",
            json={
                "project_id": self.project_id,
                "max_tables": 50,
                "max_sample_rows": 50,
                "max_execution_seconds": 60,
            },
            headers=_auth(self.token),
        )
        assert resp.status_code == 202

        # Fetch profiles
        resp = client.get(
            f"{BASE}/connections/{conn_id}/metadata/profiles",
            params={"project_id": self.project_id},
            headers=_auth(self.token),
        )
        assert resp.status_code == 200
        profiles = resp.json()["items"]

        # Find the test_users table
        test_users_profile = None
        for p in profiles:
            if p["table_name"] == "test_users":
                test_users_profile = p
                break

        assert test_users_profile is not None, "test_users table not found in profiles"

        # Verify columns_info includes sensitive columns
        col_names = [c["name"] for c in test_users_profile["columns_info"]]
        assert "password_hash" in col_names
        assert "email" in col_names
        assert "phone" in col_names
        assert "username" in col_names  # not sensitive

        # Verify sample_data exists and sensitive fields are masked
        samples = test_users_profile.get("sample_data")
        assert samples is not None, "No sample data returned for test_users"
        assert len(samples) > 0

        for row in samples:
            # password_hash must be masked (first char + asterisks or full mask)
            pwd_val = row.get("password_hash")
            if pwd_val is not None:
                assert pwd_val != "secret123hash", "password_hash NOT masked!"
                assert pwd_val != "bobpass456", "password_hash NOT masked!"
                assert "*" in str(pwd_val), f"password_hash should contain '*' but got: {pwd_val}"

            # email must be masked
            email_val = row.get("email")
            if email_val is not None:
                assert "alice@example.com" not in str(email_val), "email NOT masked!"
                assert "bob@test.com" not in str(email_val), "email NOT masked!"

            # phone must be masked
            phone_val = row.get("phone")
            if phone_val is not None:
                assert "13800138000" not in str(phone_val), "phone NOT masked!"
                assert "13900139000" not in str(phone_val), "phone NOT masked!"

            # username should NOT be masked (not a sensitive field)
            username_val = row.get("username")
            if username_val is not None:
                assert username_val in ("alice", "bob"), (
                    f"username should not be masked but got: {username_val}"
                )

    def test_explore_select_only_guard(self) -> None:
        """The metadata service refuses non-SELECT queries."""
        from etl_agent.services.metadata_service import _validate_readonly_query

        # SELECT is allowed (no raise)
        _validate_readonly_query("SELECT * FROM users")
        _validate_readonly_query("WITH cte AS (SELECT 1) SELECT * FROM cte")

        # DDL/DML must raise RuntimeError
        with pytest.raises(RuntimeError, match="non-SELECT"):
            _validate_readonly_query("DELETE FROM users WHERE 1=1")

        with pytest.raises(RuntimeError, match="forbidden keyword"):
            _validate_readonly_query("SELECT * FROM users; DROP TABLE users")

    def test_sensitive_column_detection(self) -> None:
        """The _is_sensitive_column function matches all sensitive patterns."""
        from etl_agent.services.metadata_service import _is_sensitive_column

        # Must be detected as sensitive
        sensitive_names = [
            "password",
            "password_hash",
            "passwd",
            "pwd",
            "secret",
            "api_key",
            "apikey",
            "token",
            "access_key",
            "private_key",
            "credential",
            "credentials",
            "email",
            "user_email",
            "phone",
            "mobile",
            "tel",
            "id_card",
            "identity_number",
            "ssn",
            "bank_card",
            "credit_card",
            "salary",
            "address",
        ]
        for name in sensitive_names:
            assert _is_sensitive_column(name), f"'{name}' should be sensitive"

        # Must NOT be detected as sensitive
        safe_names = ["id", "name", "created_at", "updated_at", "status", "username"]
        for name in safe_names:
            assert not _is_sensitive_column(name), f"'{name}' should NOT be sensitive"

    def test_mask_value_function(self) -> None:
        """_mask_value masks strings as first-char + asterisks."""
        from etl_agent.services.metadata_service import _mask_value

        # Long string: first char + asterisks
        assert _mask_value("secret123") == "s********"
        assert _mask_value("abc") == "a**"

        # Short string: full mask
        assert _mask_value("ab") == "**"
        assert _mask_value("a") == "*"

        # None stays None
        assert _mask_value(None) is None

        # Non-string: full mask
        assert _mask_value(12345) == "********"


# =====================================================================
# 7. Same-tenant different-project data isolation
# =====================================================================


class TestProjectIsolation:
    """Scenario 7: within the same tenant, a user in project A
    cannot access project B's connections or profiles."""

    def setup_method(self) -> None:
        self.admin_token = _login()
        self.suffix = uuid.uuid4().hex[:8]
        self.tenant_id = _get_default_tenant_id(self.admin_token)

        # Create two projects in the SAME tenant
        self.proj_a = _create_project(self.admin_token, self.tenant_id, f"proj-a-{self.suffix}")
        self.proj_b = _create_project(self.admin_token, self.tenant_id, f"proj-b-{self.suffix}")

        # Create a connection in each project
        self.conn_a = _create_connection(self.admin_token, self.proj_a, f"conn-a-{self.suffix}")
        self.conn_b = _create_connection(self.admin_token, self.proj_b, f"conn-b-{self.suffix}")

        # Register a user, add to tenant, add to proj A only
        self.user_name = f"isouser-{self.suffix}"
        _register_user(self.user_name)
        _add_tenant_member(self.admin_token, self.tenant_id, self.user_name, "engineer")
        _add_project_member(self.admin_token, self.proj_a, self.user_name, "engineer")
        # NOT added to proj_b

        self.user_token = _login(self.user_name, "TestPass123")

    def test_user_sees_project_a_connections(self) -> None:
        """User can list connections in project A (they are a member)."""
        resp = client.get(
            f"{BASE}/connections",
            params={"project_id": self.proj_a},
            headers=_auth(self.user_token),
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) > 0
        assert all(c["id"] != self.conn_b for c in items)

    def test_user_cannot_see_project_b_connections(self) -> None:
        """User cannot list connections in project B (not a member)."""
        resp = client.get(
            f"{BASE}/connections",
            params={"project_id": self.proj_b},
            headers=_auth(self.user_token),
        )
        assert resp.status_code in (403, 404)

    def test_user_cannot_test_project_b_connection(self) -> None:
        """User cannot test a connection in project B."""
        resp = client.post(
            f"{BASE}/connections/{self.conn_b}/test",
            params={"project_id": self.proj_b},
            headers=_auth(self.user_token),
        )
        assert resp.status_code in (403, 404)

    def test_user_cannot_explore_project_b(self) -> None:
        """User cannot trigger exploration on project B's connection."""
        resp = client.post(
            f"{BASE}/connections/{self.conn_b}/metadata/explore",
            json={
                "project_id": self.proj_b,
                "max_tables": 5,
                "max_sample_rows": 5,
                "max_execution_seconds": 60,
            },
            headers=_auth(self.user_token),
        )
        assert resp.status_code in (403, 404)

    def test_user_cannot_view_project_b_profiles(self) -> None:
        """User cannot view profiles for project B's connection."""
        resp = client.get(
            f"{BASE}/connections/{self.conn_b}/metadata/profiles",
            params={"project_id": self.proj_b},
            headers=_auth(self.user_token),
        )
        assert resp.status_code in (403, 404)

    def test_user_cannot_access_conn_a_via_project_b(self) -> None:
        """Even if the user knows conn_a's ID, they cannot access it
        through project B's context (cross-project ID guessing)."""
        resp = client.get(
            f"{BASE}/connections/{self.conn_a}/metadata/profiles",
            params={"project_id": self.proj_b},
            headers=_auth(self.user_token),
        )
        assert resp.status_code in (403, 404)
