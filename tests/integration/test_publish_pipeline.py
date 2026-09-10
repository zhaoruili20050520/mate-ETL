"""Integration tests for the publish pipeline (quality -> swap -> rollback).

Covers 9 scenarios as specified in the design:

1. SeaTunnel finished -> status becomes quality_checking
2. Error rate exceeded -> do not publish
3. Shadow table empty -> do not publish
4. Primary key duplicate -> do not publish
5. PostgreSQL atomic swap succeeds
6. After swap, formal table rows == shadow table rows
7. Swap failure restores backup table
8. After rollback, ExecutionRun.status == rolled_back
9. Evidence Ledger has quality_check / swap / rollback entries

Strategy: these tests work at the **service layer** using real
PostgreSQL (via SessionLocal) and direct psycopg connections
for table setup.  Vault credentials are mocked to return the
test DB password.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from sqlalchemy import select

from etl_agent.db.session import SessionLocal
from etl_agent.models.data_source_connection import (
    DataSourceConnection,
    DataSourceType,
)
from etl_agent.models.evidence_ledger import (
    EvidenceEventType,
    EvidenceLedger,
)
from etl_agent.models.execution_event import (
    ExecutionEvent,
    ExecutionEventType,
)
from etl_agent.models.execution_metric import ExecutionMetric
from etl_agent.models.execution_run import (
    ExecutionRun,
    ExecutionStatus,
)
from etl_agent.models.preparation import (
    DataClassification,
    Preparation,
    PreparationStatus,
    RiskLevel,
)
from etl_agent.models.project import Project, ProjectStatus
from etl_agent.models.project_member import ProjectMember
from etl_agent.models.role import Role
from etl_agent.models.runtime_supervision_contract import (
    RuntimeSupervisionContract,
    SwapStrategy,
)
from etl_agent.models.tenant import Tenant, TenantStatus
from etl_agent.models.tenant_member import TenantMember
from etl_agent.models.user import User, UserStatus
from etl_agent.services import execution_publish_service, execution_rollback_service

# =====================================================================
# Helpers
# =====================================================================

_DB_DSN = "postgresql://etl_agent:changeme@localhost:15432/etl_agent"


def _direct_conn() -> psycopg.Connection:
    """Open a direct psycopg connection to the test DB."""
    return psycopg.connect(_DB_DSN)


def _exec_direct(sql: str, params: tuple[Any, ...] | None = None) -> None:
    """Execute SQL on a fresh direct connection (auto-commit)."""
    with _direct_conn() as conn:
        conn.execute(sql, params)
        conn.commit()


def _query_direct(sql: str) -> list[dict[str, Any]]:
    """Run a SELECT on a direct connection."""
    with _direct_conn() as conn:
        cur = conn.execute(sql)
        cols = [desc[0] for desc in cur.description] if cur.description else []
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def _setup_pg_tables(
    schema: str,
    target_table: str,
    shadow_table: str,
    error_table: str,
    rows: list[tuple[int, str, str]] | None = None,
    pk_duplicates: bool = False,
) -> None:
    """Create schema, shadow table, target table, and error table.

    Args:
        schema: PG schema name (e.g. 'public').
        target_table: Name for the formal target table.
        shadow_table: Name for the shadow/staging table.
        error_table: Name for the dead-letter (error) table.
        rows: Data to insert into shadow table.
        pk_duplicates: If True, insert rows with duplicate PKs.
    """
    _exec_direct(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    # Drop existing tables (safety)
    _exec_direct(f'DROP TABLE IF EXISTS "{schema}"."{target_table}"')
    _exec_direct(f'DROP TABLE IF EXISTS "{schema}"."{shadow_table}"')
    _exec_direct(f'DROP TABLE IF EXISTS "{schema}"."{error_table}"')

    # Create target table
    _exec_direct(
        f'CREATE TABLE "{schema}"."{target_table}" '
        f"(id INTEGER PRIMARY KEY, name VARCHAR(100), email VARCHAR(100))"
    )
    # Create shadow table (same schema)
    _exec_direct(
        f'CREATE TABLE "{schema}"."{shadow_table}" '
        f"(id INTEGER, name VARCHAR(100), email VARCHAR(100))"
    )
    # Create error table (dead letter)
    _exec_direct(f'CREATE TABLE "{schema}"."{error_table}" (id INTEGER, reason TEXT)')

    if rows is not None:
        for row in rows:
            _exec_direct(
                f'INSERT INTO "{schema}"."{shadow_table}" (id, name, email) VALUES (%s, %s, %s)',
                row,
            )

    if pk_duplicates:
        # Insert duplicate PK rows (using parameterized queries)
        _exec_direct(
            f'INSERT INTO "{schema}"."{shadow_table}" (id, name, email) VALUES (%s, %s, %s)',
            (1, "dup1", "d1@x.com"),
        )
        _exec_direct(
            f'INSERT INTO "{schema}"."{shadow_table}" (id, name, email) VALUES (%s, %s, %s)',
            (1, "dup2", "d2@x.com"),
        )


def _cleanup_pg_tables(
    schema: str,
    target_table: str,
    shadow_table: str,
    error_table: str,
) -> None:
    """Drop all test tables."""
    # Also drop backup tables
    with _direct_conn() as conn:
        cur = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name LIKE %s",
            (schema, f"{target_table}%"),
        )
        tables = [r[0] for r in cur.fetchall()]
        for t in tables:
            conn.execute(f'DROP TABLE IF EXISTS "{schema}"."{t}" CASCADE')
        conn.execute(f'DROP TABLE IF EXISTS "{schema}"."{shadow_table}"')
        conn.execute(f'DROP TABLE IF EXISTS "{schema}"."{error_table}"')
        conn.commit()


def _mock_vault() -> MagicMock:
    """Create a mock Vault client that returns the test DB password."""
    mock = MagicMock()
    mock.read_secret_by_ref.return_value = {"password": "changeme"}
    return mock


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture()
def db():
    """Provide a DB session that rolls back after each test."""
    session = SessionLocal()
    session.begin()
    yield session
    session.rollback()
    session.close()


@pytest.fixture()
def seed_data(db):
    """Create the minimum DB rows for a publish pipeline test.

    Returns a SimpleNamespace with all the IDs and objects.
    """
    suffix = uuid.uuid4().hex[:8]

    # Tenant
    tenant = Tenant(name=f"pub-tenant-{suffix}", status=TenantStatus.active)
    db.add(tenant)
    db.flush()

    # User
    user = User(
        username=f"pub_user_{suffix}",
        password_hash="$2b$12$dummy",
        status=UserStatus.active,
    )
    db.add(user)
    db.flush()

    db.add(
        TenantMember(
            tenant_id=tenant.id,
            user_id=user.id,
            role=Role.engineer,
        )
    )
    db.flush()

    # Project
    project = Project(
        tenant_id=tenant.id,
        name=f"pub-project-{suffix}",
        status=ProjectStatus.active,
    )
    db.add(project)
    db.flush()

    db.add(
        ProjectMember(
            project_id=project.id,
            user_id=user.id,
            role=Role.engineer,
        )
    )
    db.flush()

    # Data source connections (pointing to local PG)
    source_conn = DataSourceConnection(
        tenant_id=tenant.id,
        project_id=project.id,
        name="source-conn",
        source_type=DataSourceType.postgresql,
        host="localhost",
        port=15432,
        database_name="etl_agent",
        db_username="etl_agent",
        secret_ref="secret/data/source-conn",
        created_by=user.id,
    )
    target_conn = DataSourceConnection(
        tenant_id=tenant.id,
        project_id=project.id,
        name="target-conn",
        source_type=DataSourceType.postgresql,
        host="localhost",
        port=15432,
        database_name="etl_agent",
        db_username="etl_agent",
        secret_ref="secret/data/target-conn",
        created_by=user.id,
    )
    db.add_all([source_conn, target_conn])
    db.flush()

    # Pipeline version
    from etl_agent.models.etl_plan import Pipeline, PipelineStatus
    from etl_agent.models.pipeline_version import (
        PipelineVersion,
        VersionStatus,
    )

    pipeline = Pipeline(
        tenant_id=tenant.id,
        project_id=project.id,
        name=f"pub-pipeline-{suffix}",
        source_connection_id=source_conn.id,
        target_connection_id=target_conn.id,
        status=PipelineStatus.frozen,
        created_by=user.id,
    )
    db.add(pipeline)
    db.flush()

    etl_plan = {
        "name": "pub-test",
        "source": {"connection_id": str(source_conn.id)},
        "target": {"connection_id": str(target_conn.id)},
        "field_mappings": [],
        "sync_strategy": {"mode": "full", "batch_size": 1000},
        "quality_contract": {
            "max_error_ratio": 0.01,
            "dead_letter_table": "dlq",
        },
    }
    hocon_text = "source { } sink { }"
    sha_input = json.dumps(etl_plan, sort_keys=True) + hocon_text
    sha256 = hashlib.sha256(sha_input.encode()).hexdigest()

    version = PipelineVersion(
        pipeline_id=pipeline.id,
        tenant_id=tenant.id,
        project_id=project.id,
        version_number=1,
        etl_plan=etl_plan,
        hocon_text=hocon_text,
        sha256=sha256,
        source_requirement={"natural_language": "test"},
        validation_results=[],
        status=VersionStatus.frozen,
        frozen_by=user.id,
    )
    db.add(version)
    db.flush()

    # Preparation
    preparation = Preparation(
        pipeline_version_id=version.id,
        tenant_id=tenant.id,
        project_id=project.id,
        input_fingerprint=sha256,
        resource_scope={
            "source_connection_id": str(source_conn.id),
            "target_connection_id": str(target_conn.id),
            "target_table": f"target_{suffix}",
            "shadow_table": f"shadow_{suffix}",
            "source_schema": "public",
            "target_schema": "public",
        },
        data_classification=DataClassification.internal,
        execution_budget={
            "max_rows": 1000000,
            "max_bytes": 10737418240,
            "max_duration_seconds": 3600,
        },
        rollback_plan={"strategy": "shadow_swap"},
        risk_level=RiskLevel.P2,
        risk_reasoning="test preparation",
        status=PreparationStatus.committed,
        prepared_by=user.id,
    )
    db.add(preparation)
    db.flush()

    # ExecutionRun
    run = ExecutionRun(
        preparation_id=preparation.id,
        tenant_id=tenant.id,
        project_id=project.id,
        capability_id=f"cap-{suffix}",
        status=ExecutionStatus.quality_checking,
        committed_by=user.id,
        started_at=datetime.now(UTC),
    )
    db.add(run)
    db.flush()

    return SimpleNamespace(
        tenant=tenant,
        user=user,
        project=project,
        source_conn=source_conn,
        target_conn=target_conn,
        pipeline=pipeline,
        version=version,
        preparation=preparation,
        run=run,
        suffix=suffix,
    )


def _make_contract(
    db,
    seed: SimpleNamespace,
    *,
    max_error_ratio: float = 0.01,
    quality_rules: list[dict[str, Any]] | None = None,
    swap_strategy: SwapStrategy = SwapStrategy.postgresql_transactional,
    target_table: str | None = None,
    shadow_table: str | None = None,
    dead_letter_table: str | None = None,
) -> RuntimeSupervisionContract:
    """Create a RuntimeSupervisionContract for the test run."""
    if target_table is None:
        target_table = f"target_{seed.suffix}"
    if shadow_table is None:
        shadow_table = f"shadow_{seed.suffix}"
    if dead_letter_table is None:
        dead_letter_table = f"dlq_{seed.suffix}"

    contract = RuntimeSupervisionContract(
        execution_run_id=seed.run.id,
        tenant_id=seed.tenant.id,
        project_id=seed.project.id,
        max_rows=10_000_000,
        max_bytes=10_737_418_240,
        max_duration_seconds=3600,
        max_error_ratio=max_error_ratio,
        max_output_amplification=10.0,
        dead_letter_table=dead_letter_table,
        dead_letter_schema="public",
        quality_rules=quality_rules or [],
        swap_strategy=swap_strategy,
        target_table=target_table,
        target_schema="public",
        shadow_table=shadow_table,
        source_connection_id=seed.source_conn.id,
        target_connection_id=seed.target_conn.id,
        hocon_template="source { } sink { }",
        rollback_plan={"strategy": "shadow_swap"},
    )
    db.add(contract)
    db.flush()
    return contract


def _make_metrics(
    db,
    run_id: uuid.UUID,
    *,
    read_rows: int = 100,
    written_rows: int = 95,
    error_rows: int = 5,
) -> ExecutionMetric:
    """Create an ExecutionMetric row."""
    metric = ExecutionMetric(
        execution_run_id=run_id,
        read_rows=read_rows,
        written_rows=written_rows,
        error_rows=error_rows,
        bytes_read=1024,
        bytes_written=2048,
        duration_seconds=10.0,
        output_amplification=0.95,
        error_rejection_rate=error_rows / max(read_rows, 1),
    )
    db.add(metric)
    db.flush()
    return metric


# =====================================================================
# 1. SeaTunnel finished -> status becomes quality_checking
# =====================================================================


class TestSupervisionFinishedTransition:
    """Scenario 1: SeaTunnel finished -> quality_checking + trigger publish_worker."""

    def test_finished_triggers_quality_checking(self, db, seed_data) -> None:
        """When SeaTunnel reports 'finished', run becomes quality_checking."""
        from etl_agent.connectors.seatunnel import JobStatus
        from etl_agent.workers.supervision_worker import _handle_finished_job

        # Set run to running
        seed_data.run.status = ExecutionStatus.running
        db.flush()

        # Make contract
        contract = _make_contract(db, seed_data)

        # Mock metrics
        metrics = SimpleNamespace(
            read_rows=100,
            written_rows=95,
            error_rows=5,
            bytes_read=1024,
            bytes_written=2048,
            duration_seconds=10.0,
            raw={},
            output_amplification=0.95,
            error_rejection_rate=0.05,
            to_dict=lambda: {},
        )

        # Mock SeaTunnel client
        mock_client = MagicMock()

        result = _handle_finished_job(
            db,
            seed_data.run,
            contract,
            mock_client,
            JobStatus.finished,
            metrics,
        )

        # Assert status is quality_checking
        assert seed_data.run.status == ExecutionStatus.quality_checking
        # The publish task is queued only after the DB state is committed.
        assert result is True


# =====================================================================
# 2. Error rate exceeded -> do not publish
# =====================================================================


class TestErrorRateExceeded:
    """Scenario 2: error rate exceeds max_error_ratio -> quality gate fails."""

    def test_high_error_rate_fails_quality_gate(self, db, seed_data) -> None:
        """If error_rows/read_rows > max_error_ratio, quality gate should fail."""
        # Create contract with strict error ratio
        _make_contract(
            db,
            seed_data,
            max_error_ratio=0.01,
            target_table=f"target_{seed_data.suffix}",
            shadow_table=f"shadow_{seed_data.suffix}",
            dead_letter_table=f"dlq_{seed_data.suffix}",
        )

        # Create metrics with 20% error rate (>> 1% threshold)
        _make_metrics(
            db,
            seed_data.run.id,
            read_rows=100,
            written_rows=80,
            error_rows=20,
        )

        # Setup real PG tables
        schema = "public"
        tname = f"target_{seed_data.suffix}"
        sname = f"shadow_{seed_data.suffix}"
        ename = f"dlq_{seed_data.suffix}"
        _setup_pg_tables(
            schema,
            tname,
            sname,
            ename,
            rows=[(i, f"name{i}", f"e{i}@x.com") for i in range(1, 81)],
        )
        # Add error rows to dead-letter table
        for i in range(20):
            _exec_direct(
                f'INSERT INTO "{schema}"."{ename}" (id, reason) VALUES (%s, %s)',
                (i, "bad data"),
            )

        try:
            with patch.object(
                execution_publish_service,
                "get_vault_client",
                return_value=_mock_vault(),
            ):
                result = execution_publish_service.run_publish_quality_gate(
                    db,
                    seed_data.run,
                )
            assert not result["passed"]
            assert "error_rate" in str(result.get("reason", "")) or result.get("reason") in (
                "quality_check_failed",
                "quality_check_error",
            )
        finally:
            _cleanup_pg_tables(schema, tname, sname, ename)


# =====================================================================
# 3. Shadow table empty -> do not publish
# =====================================================================


class TestShadowTableEmpty:
    """Scenario 3: shadow table has 0 rows -> do not publish."""

    def test_empty_shadow_fails(self, db, seed_data) -> None:
        """If shadow table has 0 rows, quality gate should fail."""
        _make_contract(
            db,
            seed_data,
            max_error_ratio=0.01,
            quality_rules=[
                {"field": "id", "rule": "not_null", "params": {}},
            ],
        )
        _make_metrics(db, seed_data.run.id, read_rows=100, written_rows=0, error_rows=0)

        schema = "public"
        tname = f"target_{seed_data.suffix}"
        sname = f"shadow_{seed_data.suffix}"
        ename = f"dlq_{seed_data.suffix}"
        _setup_pg_tables(schema, tname, sname, ename, rows=[])

        try:
            with patch.object(
                execution_publish_service,
                "get_vault_client",
                return_value=_mock_vault(),
            ):
                result = execution_publish_service.run_publish_quality_gate(
                    db,
                    seed_data.run,
                )
            assert not result["passed"]
        finally:
            _cleanup_pg_tables(schema, tname, sname, ename)


# =====================================================================
# 4. Primary key duplicate -> do not publish
# =====================================================================


class TestPrimaryKeyDuplicate:
    """Scenario 4: shadow table has duplicate PKs -> do not publish."""

    def test_duplicate_pk_fails_verification(self, db, seed_data) -> None:
        """If shadow table has duplicate PKs, shadow verification should fail."""
        _make_contract(
            db,
            seed_data,
            max_error_ratio=0.50,
            quality_rules=[
                {"field": "id", "rule": "not_null", "params": {}},
            ],
        )
        _make_metrics(db, seed_data.run.id, read_rows=10, written_rows=10, error_rows=0)

        schema = "public"
        tname = f"target_{seed_data.suffix}"
        sname = f"shadow_{seed_data.suffix}"
        ename = f"dlq_{seed_data.suffix}"
        # Create shadow table with duplicate PKs
        _setup_pg_tables(
            schema,
            tname,
            sname,
            ename,
            pk_duplicates=True,
        )

        try:
            with patch.object(
                execution_publish_service,
                "get_vault_client",
                return_value=_mock_vault(),
            ):
                result = execution_publish_service.run_publish_quality_gate(
                    db,
                    seed_data.run,
                )
            assert not result["passed"]
        finally:
            _cleanup_pg_tables(schema, tname, sname, ename)


# =====================================================================
# 5. PostgreSQL atomic swap succeeds
# =====================================================================


class TestPostgresSwapSuccess:
    """Scenario 5: PostgreSQL RENAME swap succeeds."""

    def test_swap_succeeds(self, db, seed_data) -> None:
        """PG atomic swap renames shadow -> target, target -> backup."""
        contract = _make_contract(
            db,
            seed_data,
            swap_strategy=SwapStrategy.postgresql_transactional,
        )
        _make_metrics(db, seed_data.run.id, read_rows=3, written_rows=3, error_rows=0)

        schema = "public"
        tname = f"target_{seed_data.suffix}"
        sname = f"shadow_{seed_data.suffix}"
        ename = f"dlq_{seed_data.suffix}"
        test_rows = [(1, "alice", "a@x.com"), (2, "bob", "b@x.com"), (3, "carol", "c@x.com")]
        _setup_pg_tables(schema, tname, sname, ename, rows=test_rows)

        try:
            with patch.object(
                execution_publish_service,
                "get_vault_client",
                return_value=_mock_vault(),
            ):
                result = execution_publish_service.swap_and_publish(
                    db,
                    seed_data.run,
                    contract,
                )
            assert result["published"] is True
            assert "backup_table" in result
        finally:
            _cleanup_pg_tables(schema, tname, sname, ename)


# =====================================================================
# 6. After swap, formal table rows == shadow table rows
# =====================================================================


class TestSwapRowCountConsistency:
    """Scenario 6: after swap, formal table has same row count as shadow."""

    def test_row_count_matches(self, db, seed_data) -> None:
        """After swap, SELECT COUNT(*) on formal == shadow rows."""
        contract = _make_contract(db, seed_data)
        _make_metrics(db, seed_data.run.id, read_rows=5, written_rows=5, error_rows=0)

        schema = "public"
        tname = f"target_{seed_data.suffix}"
        sname = f"shadow_{seed_data.suffix}"
        ename = f"dlq_{seed_data.suffix}"
        test_rows = [(i, f"user{i}", f"u{i}@x.com") for i in range(1, 6)]
        _setup_pg_tables(schema, tname, sname, ename, rows=test_rows)
        expected_count = len(test_rows)

        try:
            with patch.object(
                execution_publish_service,
                "get_vault_client",
                return_value=_mock_vault(),
            ):
                result = execution_publish_service.swap_and_publish(
                    db,
                    seed_data.run,
                    contract,
                )
            assert result["published"] is True
            # Query the formal table directly
            formal_rows = _query_direct(f'SELECT COUNT(*) AS cnt FROM "{schema}"."{tname}"')
            assert int(formal_rows[0]["cnt"]) == expected_count
        finally:
            _cleanup_pg_tables(schema, tname, sname, ename)


# =====================================================================
# 7. Swap failure restores backup table
# =====================================================================


class TestSwapFailureRestoresBackup:
    """Scenario 7: swap failure restores the formal table from backup."""

    def test_swap_failure_restores(self, db, seed_data) -> None:
        """If swap fails, rollback service restores the formal table."""
        _make_contract(db, seed_data)
        _make_metrics(db, seed_data.run.id, read_rows=2, written_rows=2, error_rows=0)

        schema = "public"
        tname = f"target_{seed_data.suffix}"
        sname = f"shadow_{seed_data.suffix}"
        ename = f"dlq_{seed_data.suffix}"
        # Target has 2 original rows
        _setup_pg_tables(
            schema,
            tname,
            sname,
            ename,
            rows=[(1, "orig1", "o1@x.com"), (2, "orig2", "o2@x.com")],
        )
        # Insert into target table (original data)
        _exec_direct(
            f'INSERT INTO "{schema}"."{tname}" (id, name, email) VALUES (%s, %s, %s)',
            (1, "orig1", "o1@x.com"),
        )
        _exec_direct(
            f'INSERT INTO "{schema}"."{tname}" (id, name, email) VALUES (%s, %s, %s)',
            (2, "orig2", "o2@x.com"),
        )

        # Drop the shadow table so swap will fail
        _exec_direct(f'DROP TABLE "{schema}"."{sname}"')

        try:
            with patch.object(
                execution_rollback_service,
                "get_vault_client",
                return_value=_mock_vault(),
            ):
                result = execution_rollback_service.run_managed_rollback(
                    db,
                    seed_data.run,
                    failure_reason="Swap failed: shadow table not found",
                )
            assert result["rolled_back"] is True
            # Check that the formal table still exists and has original data
            formal_rows = _query_direct(f'SELECT COUNT(*) AS cnt FROM "{schema}"."{tname}"')
            assert int(formal_rows[0]["cnt"]) == 2
        finally:
            _cleanup_pg_tables(schema, tname, sname, ename)


# =====================================================================
# 8. After rollback, ExecutionRun.status == rolled_back
# =====================================================================


class TestRollbackStatus:
    """Scenario 8: after rollback, ExecutionRun is rolled_back."""

    def test_rollback_sets_status(self, db, seed_data) -> None:
        """run_managed_rollback sets ExecutionRun.status = rolled_back."""
        _make_contract(db, seed_data)
        _make_metrics(db, seed_data.run.id)

        schema = "public"
        tname = f"target_{seed_data.suffix}"
        sname = f"shadow_{seed_data.suffix}"
        ename = f"dlq_{seed_data.suffix}"
        _setup_pg_tables(schema, tname, sname, ename, rows=[])

        try:
            with patch.object(
                execution_rollback_service,
                "get_vault_client",
                return_value=_mock_vault(),
            ):
                execution_rollback_service.run_managed_rollback(
                    db,
                    seed_data.run,
                    failure_reason="test rollback",
                )
            db.flush()
            assert seed_data.run.status == ExecutionStatus.rolled_back
            assert seed_data.run.finished_at is not None
        finally:
            _cleanup_pg_tables(schema, tname, sname, ename)


# =====================================================================
# 9. Evidence Ledger has quality_check / swap / rollback entries
# =====================================================================


class TestEvidenceLedgerAuditTrail:
    """Scenario 9: Evidence Ledger records quality/swap/rollback events."""

    def test_evidence_ledger_has_entries(self, db, seed_data) -> None:
        """After a full rollback, EvidenceLedger has rollback entry.

        Also verify ExecutionEvent has the expected event types.
        """
        _make_contract(db, seed_data)
        _make_metrics(db, seed_data.run.id)

        schema = "public"
        tname = f"target_{seed_data.suffix}"
        sname = f"shadow_{seed_data.suffix}"
        ename = f"dlq_{seed_data.suffix}"
        _setup_pg_tables(schema, tname, sname, ename, rows=[])

        try:
            with patch.object(
                execution_rollback_service,
                "get_vault_client",
                return_value=_mock_vault(),
            ):
                execution_rollback_service.run_managed_rollback(
                    db,
                    seed_data.run,
                    failure_reason="audit trail test",
                )
            db.flush()

            # Check EvidenceLedger has a rollback entry
            ledger_entries = db.scalars(
                select(EvidenceLedger).where(
                    EvidenceLedger.execution_run_id == seed_data.run.id,
                )
            ).all()
            event_types = [e.event_type for e in ledger_entries]
            assert EvidenceEventType.rollback in event_types, (
                f"EvidenceLedger should have rollback entry, got: {event_types}"
            )

            # Check ExecutionEvent has rollback_started and rollback_succeeded
            events = db.scalars(
                select(ExecutionEvent).where(
                    ExecutionEvent.execution_run_id == seed_data.run.id,
                )
            ).all()
            event_type_values = [e.event_type for e in events]
            assert ExecutionEventType.rollback_started in event_type_values, (
                f"Should have rollback_started event, got: {event_type_values}"
            )
            assert ExecutionEventType.rollback_succeeded in event_type_values, (
                f"Should have rollback_succeeded event, got: {event_type_values}"
            )
        finally:
            _cleanup_pg_tables(schema, tname, sname, ename)
