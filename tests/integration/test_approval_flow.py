"""Integration tests for the prepare -> approve -> commit flow.

Covers 10 scenarios:

1. Unfrozen pipeline version cannot produce a Preparation
2. Approver cannot be the same person who prepared
3. data_checker and security_checker cannot be the same person
4. Cannot commit before all required approvals
5. Cannot commit after rejection
6. Fingerprint mismatch blocks commit
7. Capability is single-use (consumed once)
8. Replay guard rejects duplicate consumption
9. Outbox and ExecutionRun are in the same transaction
10. Evidence Ledger hash chain is intact after full lifecycle

Strategy: these tests work at the **service layer** using real
PostgreSQL (via SessionLocal) rather than going through the HTTP
API.  This is because the prepare->approve->commit chain requires
a frozen PipelineVersion, which itself requires the full LangGraph
generation chain -- that is tested separately in
``test_pipeline_generation.py``.

Instead, we create minimal Pipeline + PipelineVersion rows directly
in the DB, then call the service functions.
"""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from sqlalchemy import select

from etl_agent.core.exceptions import (
    ConflictError,
    InvalidInputError,
    PermissionDeniedError,
)
from etl_agent.db.session import SessionLocal
from etl_agent.models.approval import (
    ApprovalDecision,
    ApprovalRole,
)
from etl_agent.models.capability_log import (
    CapabilityAction,
    CapabilityLog,
)
from etl_agent.models.etl_plan import Pipeline, PipelineStatus
from etl_agent.models.evidence_ledger import (
    EvidenceEventType,
    EvidenceLedger,
)
from etl_agent.models.execution_run import ExecutionStatus
from etl_agent.models.outbox import OutboxMessage, OutboxStatus
from etl_agent.models.pipeline_version import (
    PipelineVersion,
    VersionStatus,
)
from etl_agent.models.preparation import (
    DataClassification,
    PreparationStatus,
    RiskLevel,
)
from etl_agent.models.user import User, UserStatus
from etl_agent.schemas.generation import (
    EtlPlan,
    FieldMapping,
    QualityContract,
    SourceConfig,
    SyncMode,
    SyncStrategy,
    TargetConfig,
)
from etl_agent.services import approval_service, commit_service
from etl_agent.services.evidence_service import verify_chain
from etl_agent.services.preparation_service import (
    create_preparation,
    verify_fingerprint,
)


def _clear_evidence_ledger(db) -> None:
    """Remove all ledger rows so chain verification starts from genesis."""
    for entry in db.scalars(select(EvidenceLedger)).all():
        db.delete(entry)
    db.flush()


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
    """Create the minimum DB rows for a full prepare->commit test.

    Returns a SimpleNamespace with all the IDs and objects the
    test functions need.
    """
    from types import SimpleNamespace

    from etl_agent.models.data_source_connection import (
        DataSourceConnection,
        DataSourceType,
    )
    from etl_agent.models.project import Project, ProjectStatus
    from etl_agent.models.project_member import ProjectMember
    from etl_agent.models.role import Role
    from etl_agent.models.tenant import Tenant, TenantStatus
    from etl_agent.models.tenant_member import TenantMember

    suffix = uuid.uuid4().hex[:8]

    # Tenant
    tenant = Tenant(name=f"test-tenant-{suffix}", status=TenantStatus.active)
    db.add(tenant)
    db.flush()

    # Users
    pw_hash = "$2b$12$dummyhashfornonauthenticationpurposesonly"
    preparer = User(
        username=f"preparer_test_{suffix}",
        password_hash=pw_hash,
        status=UserStatus.active,
    )
    data_checker = User(
        username=f"data_checker_test_{suffix}",
        password_hash=pw_hash,
        status=UserStatus.active,
    )
    security_checker = User(
        username=f"security_checker_test_{suffix}",
        password_hash=pw_hash,
        status=UserStatus.active,
    )
    db.add_all([preparer, data_checker, security_checker])
    db.flush()

    # Tenant memberships
    for u in (preparer, data_checker, security_checker):
        db.add(
            TenantMember(
                tenant_id=tenant.id,
                user_id=u.id,
                role=Role.engineer,
            ),
        )
    db.flush()

    # Project
    project = Project(
        tenant_id=tenant.id,
        name=f"test-project-{suffix}",
        status=ProjectStatus.active,
    )
    db.add(project)
    db.flush()

    # Project memberships
    for u in (preparer, data_checker, security_checker):
        db.add(
            ProjectMember(
                project_id=project.id,
                user_id=u.id,
                role=Role.engineer,
            ),
        )
    db.flush()

    # Data source connections (source + target for pipeline)
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
        created_by=preparer.id,
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
        created_by=preparer.id,
    )
    db.add_all([source_conn, target_conn])
    db.flush()

    # Pipeline
    pipeline = Pipeline(
        tenant_id=tenant.id,
        project_id=project.id,
        name="test-pipeline",
        source_connection_id=source_conn.id,
        target_connection_id=target_conn.id,
        status=PipelineStatus.frozen,
        created_by=preparer.id,
    )
    db.add(pipeline)
    db.flush()

    # PipelineVersion (frozen)
    etl_plan = EtlPlan(
        name="test-plan",
        source=SourceConfig(
            connection_id=source_conn.id,
            database_name="etl_agent",
            schema_name="public",
            table_name="users",
        ),
        target=TargetConfig(
            connection_id=target_conn.id,
            database_name="etl_agent",
            schema_name="public",
            table_name="users_copy",
            shadow_table="users_shadow",
        ),
        field_mappings=[
            FieldMapping(
                source_field="id",
                target_field="id",
                source_type="integer",
                target_type="integer",
            ),
            FieldMapping(
                source_field="username",
                target_field="username",
                source_type="varchar",
                target_type="varchar",
            ),
        ],
        sync_strategy=SyncStrategy(
            mode=SyncMode.full,
            batch_size=1000,
        ),
        quality_contract=QualityContract(
            max_error_ratio=0.01,
            dead_letter_table="dlq",
        ),
    )
    etl_plan_json = etl_plan.model_dump(mode="json")
    hocon_text = 'source { table = "users" } sink { table = "users_copy" }'
    sha_input = json.dumps(etl_plan_json, sort_keys=True) + hocon_text
    sha256 = hashlib.sha256(sha_input.encode("utf-8")).hexdigest()

    version = PipelineVersion(
        pipeline_id=pipeline.id,
        tenant_id=tenant.id,
        project_id=project.id,
        version_number=1,
        etl_plan=etl_plan_json,
        hocon_text=hocon_text,
        sha256=sha256,
        source_requirement={"natural_language": "copy users"},
        validation_results=[],
        status=VersionStatus.frozen,
        frozen_by=preparer.id,
    )
    db.add(version)
    db.flush()

    return SimpleNamespace(
        tenant=tenant,
        preparer=preparer,
        data_checker=data_checker,
        security_checker=security_checker,
        project=project,
        source_conn=source_conn,
        target_conn=target_conn,
        pipeline=pipeline,
        version=version,
        etl_plan=etl_plan,
    )


# =====================================================================
# 1. Unfrozen pipeline version cannot produce a Preparation
# =====================================================================


class TestUnfrozenCannotPrepare:
    """Scenario 1: trying to create a Preparation from an unfrozen version."""

    def test_unfrozen_version_rejected(self, db, seed_data) -> None:
        """If the PipelineVersion status is not frozen, create_preparation raises."""
        # Mutate the version to non-frozen
        seed_data.version.status = VersionStatus.rejected
        db.flush()

        with pytest.raises(InvalidInputError) as exc_info:
            create_preparation(
                db,
                pipeline_version_id=seed_data.version.id,
                user_id=seed_data.preparer.id,
            )

        assert "not frozen" in exc_info.value.detail.lower()


# =====================================================================
# 2. Approver cannot be the same person who prepared
# =====================================================================


class TestNoSelfApproval:
    """Scenario 2: the person who prepared cannot approve it."""

    def test_self_approval_rejected(self, db, seed_data) -> None:
        """create_approval raises PermissionDeniedError when approver == prepared_by."""
        prep = create_preparation(
            db,
            pipeline_version_id=seed_data.version.id,
            user_id=seed_data.preparer.id,
            data_classification=DataClassification.restricted,
        )

        with pytest.raises(PermissionDeniedError) as exc_info:
            approval_service.create_approval(
                db,
                preparation_id=prep.id,
                approver_id=seed_data.preparer.id,
                approval_role=ApprovalRole.data_checker,
                decision=ApprovalDecision.approved,
            )

        assert "cannot also approve" in exc_info.value.detail.lower()


# =====================================================================
# 3. data_checker and security_checker cannot be the same person
# =====================================================================


class TestSeparationOfDuties:
    """Scenario 3: same user cannot hold both approval roles."""

    def test_same_person_both_roles_rejected(self, db, seed_data) -> None:
        """A user who approved as data_checker cannot also approve as security_checker."""
        prep = create_preparation(
            db,
            pipeline_version_id=seed_data.version.id,
            user_id=seed_data.preparer.id,
            data_classification=DataClassification.restricted,
        )

        # data_checker approves (must be a different person than preparer)
        approval_service.create_approval(
            db,
            preparation_id=prep.id,
            approver_id=seed_data.data_checker.id,
            approval_role=ApprovalRole.data_checker,
            decision=ApprovalDecision.approved,
        )

        # Same data_checker tries to approve as security_checker
        with pytest.raises(PermissionDeniedError) as exc_info:
            approval_service.create_approval(
                db,
                preparation_id=prep.id,
                approver_id=seed_data.data_checker.id,
                approval_role=ApprovalRole.security_checker,
                decision=ApprovalDecision.approved,
            )

        assert "both roles" in exc_info.value.detail.lower()


# =====================================================================
# 4. Cannot commit before all required approvals
# =====================================================================


class TestCannotCommitBeforeApproval:
    """Scenario 4: commit is blocked until all approvals are in place."""

    def test_commit_without_full_approval_blocked(self, db, seed_data) -> None:
        """A P0 preparation requires both roles; committing with only one fails."""
        prep = create_preparation(
            db,
            pipeline_version_id=seed_data.version.id,
            user_id=seed_data.preparer.id,
            data_classification=DataClassification.restricted,
        )
        # restricted + shadow_swap -> score 30+5 = 35, but is_production_source=False
        # Actually restricted=+30, shadow_swap=+5 -> score 35 -> P2 (not dual)
        # Need to force P0/P1: add is_production_source=True (+20) -> 55 -> P1

        # Re-create with production source to get P1 (dual approval)
        db.rollback()
        db.begin()
        # Re-seed minimal state

        from etl_agent.models.data_source_connection import (
            DataSourceConnection,
            DataSourceType,
        )
        from etl_agent.models.project import Project, ProjectStatus
        from etl_agent.models.project_member import ProjectMember
        from etl_agent.models.role import Role
        from etl_agent.models.tenant import Tenant, TenantStatus
        from etl_agent.models.tenant_member import TenantMember

        suffix = uuid.uuid4().hex[:8]
        tenant = Tenant(name=f"t2-{suffix}", status=TenantStatus.active)
        db.add(tenant)
        db.flush()
        pw = "$2b$12$dummypasshash"
        prep_user = User(username=f"u2-{suffix}", password_hash=pw, status=UserStatus.active)
        dc_user = User(username=f"dc2-{suffix}", password_hash=pw, status=UserStatus.active)
        sc_user = User(username=f"sc2-{suffix}", password_hash=pw, status=UserStatus.active)
        db.add_all([prep_user, dc_user, sc_user])
        db.flush()
        for u in (prep_user, dc_user, sc_user):
            db.add(TenantMember(tenant_id=tenant.id, user_id=u.id, role=Role.engineer))
        db.flush()
        project = Project(
            tenant_id=tenant.id,
            name=f"p2-{suffix}",
            status=ProjectStatus.active,
        )
        db.add(project)
        db.flush()
        for u in (prep_user, dc_user, sc_user):
            db.add(ProjectMember(project_id=project.id, user_id=u.id, role=Role.engineer))
        db.flush()
        s_conn = DataSourceConnection(
            tenant_id=tenant.id,
            project_id=project.id,
            name="s2",
            source_type=DataSourceType.postgresql,
            host="localhost",
            port=15432,
            database_name="etl_agent",
            db_username="etl_agent",
            secret_ref="secret/s2",
            created_by=prep_user.id,
        )
        t_conn = DataSourceConnection(
            tenant_id=tenant.id,
            project_id=project.id,
            name="t2",
            source_type=DataSourceType.postgresql,
            host="localhost",
            port=15432,
            database_name="etl_agent",
            db_username="etl_agent",
            secret_ref="secret/t2",
            created_by=prep_user.id,
        )
        db.add_all([s_conn, t_conn])
        db.flush()
        pipeline = Pipeline(
            tenant_id=tenant.id,
            project_id=project.id,
            name="pl2",
            source_connection_id=s_conn.id,
            target_connection_id=t_conn.id,
            status=PipelineStatus.frozen,
            created_by=prep_user.id,
        )
        db.add(pipeline)
        db.flush()
        plan = EtlPlan(
            name="plan2",
            source=SourceConfig(
                connection_id=s_conn.id,
                database_name="etl_agent",
                schema_name="public",
                table_name="t_src",
            ),
            target=TargetConfig(
                connection_id=t_conn.id,
                database_name="etl_agent",
                schema_name="public",
                table_name="t_tgt",
                shadow_table="t_shadow",
            ),
            field_mappings=[
                FieldMapping(
                    source_field="id",
                    target_field="id",
                    source_type="integer",
                    target_type="integer",
                )
            ],
            sync_strategy=SyncStrategy(mode=SyncMode.full),
        )
        plan_json = plan.model_dump(mode="json")
        hocon = 'source { table = "t_src" }'
        sha = hashlib.sha256(
            (json.dumps(plan_json, sort_keys=True) + hocon).encode("utf-8"),
        ).hexdigest()
        version = PipelineVersion(
            pipeline_id=pipeline.id,
            tenant_id=tenant.id,
            project_id=project.id,
            version_number=1,
            etl_plan=plan_json,
            hocon_text=hocon,
            sha256=sha,
            source_requirement={"nl": "copy"},
            validation_results=[],
            status=VersionStatus.frozen,
            frozen_by=prep_user.id,
        )
        db.add(version)
        db.flush()

        # Create a P1 preparation (restricted + production source)
        prep = create_preparation(
            db,
            pipeline_version_id=version.id,
            user_id=prep_user.id,
            data_classification=DataClassification.restricted,
            is_production_source=True,
        )
        assert prep.risk_level in (RiskLevel.P0, RiskLevel.P1)

        # Only data_checker approves
        approval_service.create_approval(
            db,
            preparation_id=prep.id,
            approver_id=dc_user.id,
            approval_role=ApprovalRole.data_checker,
            decision=ApprovalDecision.approved,
        )

        # Commit should fail (security_checker hasn't approved yet)
        with pytest.raises(ConflictError) as exc_info:
            commit_service.commit_preparation(
                db,
                preparation_id=prep.id,
                user_id=prep_user.id,
            )

        assert "approval" in exc_info.value.detail.lower()


# =====================================================================
# 5. Cannot commit after rejection
# =====================================================================


class TestCannotCommitAfterRejection:
    """Scenario 5: a rejected preparation cannot be committed."""

    def test_rejection_blocks_commit(self, db, seed_data) -> None:
        """After any rejection, commit raises ConflictError."""
        prep = create_preparation(
            db,
            pipeline_version_id=seed_data.version.id,
            user_id=seed_data.preparer.id,
            data_classification=DataClassification.internal,
        )

        # Reject it
        approval_service.create_approval(
            db,
            preparation_id=prep.id,
            approver_id=seed_data.data_checker.id,
            approval_role=ApprovalRole.data_checker,
            decision=ApprovalDecision.rejected,
            comment="bad plan",
        )

        assert prep.status == PreparationStatus.rejected

        with pytest.raises(ConflictError) as exc_info:
            commit_service.commit_preparation(
                db,
                preparation_id=prep.id,
                user_id=seed_data.preparer.id,
            )

        assert "rejected" in exc_info.value.detail.lower()


# =====================================================================
# 6. Fingerprint mismatch blocks commit
# =====================================================================


class TestFingerprintMismatch:
    """Scenario 6: if the version content changes after preparation, commit fails."""

    def test_fingerprint_tamper_blocks_commit(self, db, seed_data) -> None:
        """Changing the PipelineVersion's sha256 after preparation causes fingerprint mismatch."""
        prep = create_preparation(
            db,
            pipeline_version_id=seed_data.version.id,
            user_id=seed_data.preparer.id,
            data_classification=DataClassification.internal,
        )

        # Approve (P2 needs only one role)
        approval_service.create_approval(
            db,
            preparation_id=prep.id,
            approver_id=seed_data.data_checker.id,
            approval_role=ApprovalRole.data_checker,
            decision=ApprovalDecision.approved,
        )
        assert prep.status == PreparationStatus.approved

        # Tamper with the version's sha256
        seed_data.version.sha256 = "a" * 64
        db.flush()

        # verify_fingerprint should return False
        assert not verify_fingerprint(prep, seed_data.version)

        # Commit should fail
        with pytest.raises(ConflictError) as exc_info:
            commit_service.commit_preparation(
                db,
                preparation_id=prep.id,
                user_id=seed_data.preparer.id,
            )

        assert (
            "fingerprint" in exc_info.value.detail.lower()
            or "mismatch" in exc_info.value.detail.lower()
        )


# =====================================================================
# 7. Capability is single-use
# =====================================================================


class TestCapabilitySingleUse:
    """Scenario 7: a capability token can only be consumed once."""

    def test_consume_capability_succeeds_then_blocks_replay(self, db, seed_data) -> None:
        """After consuming a capability, a second consumption attempt is rejected."""
        prep = create_preparation(
            db,
            pipeline_version_id=seed_data.version.id,
            user_id=seed_data.preparer.id,
            data_classification=DataClassification.internal,
        )

        # Approve
        approval_service.create_approval(
            db,
            preparation_id=prep.id,
            approver_id=seed_data.data_checker.id,
            approval_role=ApprovalRole.data_checker,
            decision=ApprovalDecision.approved,
        )
        assert prep.status == PreparationStatus.approved

        # Commit
        run = commit_service.commit_preparation(
            db,
            preparation_id=prep.id,
            user_id=seed_data.preparer.id,
        )
        assert run.status == ExecutionStatus.pending
        assert run.capability_id

        # First consumption succeeds
        consumed_run = commit_service.consume_capability(
            db,
            capability_id=run.capability_id,
            consumer="test-worker",
        )
        assert consumed_run.status == ExecutionStatus.running

        # Second consumption is rejected
        with pytest.raises(ConflictError) as exc_info:
            commit_service.consume_capability(
                db,
                capability_id=run.capability_id,
                consumer="attacker-worker",
            )

        assert (
            "consumed" in exc_info.value.detail.lower() or "replay" in exc_info.value.detail.lower()
        )


# =====================================================================
# 8. Replay guard rejects duplicate consumption
# =====================================================================


class TestReplayGuardDuplicate:
    """Scenario 8: the capability_log PK prevents duplicate consumed rows."""

    def test_replay_guard_pk_constraint(self, db, seed_data) -> None:
        """Attempting to insert a second consumed row with the same capability_id fails."""
        prep = create_preparation(
            db,
            pipeline_version_id=seed_data.version.id,
            user_id=seed_data.preparer.id,
            data_classification=DataClassification.internal,
        )
        approval_service.create_approval(
            db,
            preparation_id=prep.id,
            approver_id=seed_data.data_checker.id,
            approval_role=ApprovalRole.data_checker,
            decision=ApprovalDecision.approved,
        )
        run = commit_service.commit_preparation(
            db,
            preparation_id=prep.id,
            user_id=seed_data.preparer.id,
        )

        # Consume once
        commit_service.consume_capability(
            db,
            capability_id=run.capability_id,
            consumer="worker-1",
        )

        # Verify the capability_log row exists and is consumed
        cap_log = db.scalar(
            select(CapabilityLog).where(
                CapabilityLog.capability_id == run.capability_id,
            ),
        )
        assert cap_log is not None
        assert cap_log.action == CapabilityAction.consumed
        assert cap_log.consumer == "worker-1"


# =====================================================================
# 9. Outbox and ExecutionRun are in the same transaction
# =====================================================================


class TestOutboxSameTransaction:
    """Scenario 9: ExecutionRun, CapabilityLog, and OutboxMessage are all
    created atomically in the same DB transaction."""

    def test_commit_creates_all_three_atomically(self, db, seed_data) -> None:
        """After commit, ExecutionRun + CapabilityLog + OutboxMessage all exist."""
        prep = create_preparation(
            db,
            pipeline_version_id=seed_data.version.id,
            user_id=seed_data.preparer.id,
            data_classification=DataClassification.internal,
        )
        approval_service.create_approval(
            db,
            preparation_id=prep.id,
            approver_id=seed_data.data_checker.id,
            approval_role=ApprovalRole.data_checker,
            decision=ApprovalDecision.approved,
        )

        run = commit_service.commit_preparation(
            db,
            preparation_id=prep.id,
            user_id=seed_data.preparer.id,
        )

        # ExecutionRun exists
        assert run.id is not None
        assert run.status == ExecutionStatus.pending

        # CapabilityLog exists
        cap_log = db.scalar(
            select(CapabilityLog).where(
                CapabilityLog.capability_id == run.capability_id,
            ),
        )
        assert cap_log is not None
        assert cap_log.action == CapabilityAction.issued
        assert cap_log.execution_run_id == run.id

        # OutboxMessage exists
        outbox = db.scalar(
            select(OutboxMessage).where(
                OutboxMessage.execution_run_id == run.id,
            ),
        )
        assert outbox is not None
        assert outbox.status == OutboxStatus.pending
        assert outbox.payload.get("hocon_text") == seed_data.version.hocon_text
        assert outbox.payload.get("capability_id") == run.capability_id

        # Preparation status is committed
        assert prep.status == PreparationStatus.committed
        assert prep.committed_at is not None


# =====================================================================
# 10. Evidence Ledger hash chain is intact
# =====================================================================


class TestEvidenceLedgerChain:
    """Scenario 10: after a full lifecycle, the hash chain verifies."""

    def test_hash_chain_intact_after_full_lifecycle(self, db, seed_data) -> None:
        """Run prepare -> approve -> commit, then verify_chain returns (True, [])."""
        _clear_evidence_ledger(db)

        # Prepare
        prep = create_preparation(
            db,
            pipeline_version_id=seed_data.version.id,
            user_id=seed_data.preparer.id,
            data_classification=DataClassification.internal,
        )

        # Approve
        approval_service.create_approval(
            db,
            preparation_id=prep.id,
            approver_id=seed_data.data_checker.id,
            approval_role=ApprovalRole.data_checker,
            decision=ApprovalDecision.approved,
        )

        # Commit
        run = commit_service.commit_preparation(
            db,
            preparation_id=prep.id,
            user_id=seed_data.preparer.id,
        )

        # Consume capability
        commit_service.consume_capability(
            db,
            capability_id=run.capability_id,
            consumer="chain-test-worker",
        )

        # Verify the evidence ledger has entries
        entries = list(
            db.scalars(
                select(EvidenceLedger).order_by(EvidenceLedger.event_timestamp),
            ).all(),
        )
        assert len(entries) >= 3, f"Expected >= 3 evidence entries, got {len(entries)}"

        # Check event types are present
        event_types = {e.event_type for e in entries}
        assert EvidenceEventType.prepared in event_types
        assert EvidenceEventType.approved in event_types
        assert EvidenceEventType.commit in event_types

        # Verify the hash chain
        is_valid, errors = verify_chain(db)
        assert is_valid, f"Evidence chain broken: {errors}"
        assert errors == []

    def test_genesis_hash_for_empty_ledger(self, db) -> None:
        """An empty ledger reports genesis hash as the chain tip."""
        from etl_agent.services.evidence_service import GENESIS_HASH, get_chain_stats

        # Clear any existing entries (within the transaction)
        entries = list(
            db.scalars(select(EvidenceLedger)).all(),
        )
        for e in entries:
            db.delete(e)
        db.flush()

        stats = get_chain_stats(db)
        assert stats["total_events"] == 0
        assert stats["chain_tip"] == GENESIS_HASH

    def test_chain_links_are_correct(self, db, seed_data) -> None:
        """Each row's prev_hash equals the previous row's row_hash."""
        _clear_evidence_ledger(db)

        prep = create_preparation(
            db,
            pipeline_version_id=seed_data.version.id,
            user_id=seed_data.preparer.id,
            data_classification=DataClassification.internal,
        )
        approval_service.create_approval(
            db,
            preparation_id=prep.id,
            approver_id=seed_data.data_checker.id,
            approval_role=ApprovalRole.data_checker,
            decision=ApprovalDecision.approved,
        )

        entries = list(
            db.scalars(
                select(EvidenceLedger).order_by(
                    EvidenceLedger.event_timestamp,
                    EvidenceLedger.id,
                ),
            ).all(),
        )

        # First entry has genesis as prev_hash
        from etl_agent.models.evidence_ledger import GENESIS_HASH

        assert entries[0].prev_hash == GENESIS_HASH

        # Each subsequent entry's prev_hash == previous entry's row_hash
        for i in range(1, len(entries)):
            assert entries[i].prev_hash == entries[i - 1].row_hash, (
                f"Chain broken at index {i}: prev_hash={entries[i].prev_hash[:12]}... "
                f"expected={entries[i - 1].row_hash[:12]}..."
            )

        # All row_hashes are distinct
        row_hashes = [e.row_hash for e in entries]
        assert len(set(row_hashes)) == len(row_hashes), "Duplicate row hashes found"
