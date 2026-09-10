"""Tests for the knowledge-management platform.

Covers five areas:

1. **Permission isolation** -- the four-dimensional check_permission
   function and filter_chunks_by_permission.
2. **Q&A with audit** -- answer_question writes KnowledgeQaAudit, FAQ
   hit short-circuits retrieval, permission filtering blocks chunks.
3. **FAQ clustering & publishing** -- cluster_faq_candidates creates
   FaqCandidate rows; publish_faq creates PublishedFaq.
4. **Knowledge-gap diagnosis** -- unanswered questions create/refresh
   KnowledgeGap; resolve_gap flips status.
5. **Operational metrics** -- get_operation_metrics aggregates correct
   counts from audit and gap records.

Pure-function tests (check_permission, _split_text) need no DB.
Service-layer tests use SessionLocal with per-test rollback.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import etl_agent.services.knowledge_service as knowledge_service
from etl_agent.db.session import SessionLocal
from etl_agent.models.knowledge import (
    FaqCandidate,
    FaqStatus,
    KnowledgeChunk,
    KnowledgeDocumentStatus,
    KnowledgeGapStatus,
    KnowledgeRole,
    PublishedFaq,
)
from etl_agent.models.tenant import Tenant, TenantStatus
from etl_agent.models.user import User, UserStatus
from etl_agent.services.knowledge_service import (
    _split_text,
    answer_question,
    assign_knowledge_member,
    check_permission,
    cluster_faq_candidates,
    filter_chunks_by_permission,
    get_operation_metrics,
    import_document,
    list_audits,
    list_gaps,
    list_role_permissions,
    publish_document,
    publish_faq,
    reject_faq,
    resolve_gap,
    set_role_permissions,
)


@pytest.fixture(autouse=True)
def _disable_local_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unit tests deterministic without requiring an external model."""
    monkeypatch.setattr(
        knowledge_service,
        "_generate_local_answer",
        lambda question, chunks: "",
    )


# =====================================================================
# Pure-function unit tests (no DB required)
# =====================================================================


class TestCheckPermission:
    """Test the four-dimensional permission logic."""

    def test_public_visibility_allows_everyone(self) -> None:
        perms = {
            "visibility": "public",
            "departments": [],
            "roles": [],
            "users": [],
        }
        assert check_permission(perms, uuid.uuid4(), None, None) is True

    def test_tenant_visibility_allows_any_tenant_member(self) -> None:
        perms = {
            "visibility": "tenant",
            "departments": [],
            "roles": [],
            "users": [],
        }
        assert check_permission(perms, uuid.uuid4(), None, None) is True

    def test_restricted_with_empty_lists_allows_tenant_level(self) -> None:
        """visibility=restricted + empty departments/roles/users = tenant access.

        Per the permission evaluation order, empty departments + empty roles
        is treated as tenant-wide access (step 3), even for restricted
        visibility.  To actually deny, a department or role restriction must
        be set that doesn't match the user.
        """
        perms = {
            "visibility": "restricted",
            "departments": [],
            "roles": [],
            "users": [],
        }
        assert check_permission(perms, uuid.uuid4(), None, None) is True

    def test_restricted_with_unmatched_department_denies(self) -> None:
        """visibility=restricted + department mismatch = deny."""
        perms = {
            "visibility": "restricted",
            "departments": ["sales"],
            "roles": [],
            "users": [],
        }
        assert check_permission(perms, uuid.uuid4(), "engineering", None) is False

    def test_user_allowlist_grants_access(self) -> None:
        user_id = uuid.uuid4()
        perms = {
            "visibility": "restricted",
            "departments": [],
            "roles": [],
            "users": [str(user_id)],
        }
        assert check_permission(perms, user_id, None, None) is True

    def test_user_allowlist_overrides_department_restriction(self) -> None:
        """A user in the allow-list gets access even if dept doesn't match."""
        user_id = uuid.uuid4()
        perms = {
            "visibility": "restricted",
            "departments": ["sales"],
            "roles": [],
            "users": [str(user_id)],
        }
        assert check_permission(perms, user_id, "engineering", None) is True

    def test_user_not_in_allowlist_denied_by_department(self) -> None:
        """User not in allow-list + dept mismatch = denied."""
        perms = {
            "visibility": "restricted",
            "departments": ["sales"],
            "roles": [],
            "users": [str(uuid.uuid4())],
        }
        assert check_permission(perms, uuid.uuid4(), "engineering", None) is False

    def test_department_match_grants_access(self) -> None:
        perms = {
            "visibility": "restricted",
            "departments": ["engineering"],
            "roles": [],
            "users": [],
        }
        assert check_permission(perms, uuid.uuid4(), "engineering", None) is True

    def test_department_mismatch_denies(self) -> None:
        perms = {
            "visibility": "restricted",
            "departments": ["engineering"],
            "roles": [],
            "users": [],
        }
        assert check_permission(perms, uuid.uuid4(), "sales", None) is False

    def test_role_match_grants_access(self) -> None:
        perms = {
            "visibility": "restricted",
            "departments": [],
            "roles": ["knowledge_admin"],
            "users": [],
        }
        assert check_permission(perms, uuid.uuid4(), None, "knowledge_admin") is True

    def test_role_mismatch_denies(self) -> None:
        perms = {
            "visibility": "restricted",
            "departments": [],
            "roles": ["knowledge_admin"],
            "users": [],
        }
        assert check_permission(perms, uuid.uuid4(), None, "viewer") is False


class TestSplitText:
    """Test the text chunking function."""

    def test_empty_text_returns_single_empty_chunk(self) -> None:
        chunks = _split_text("")
        assert len(chunks) == 1
        assert chunks[0] == ("", "")

    def test_short_text_single_chunk(self) -> None:
        text = "Hello world"
        chunks = _split_text(text)
        assert len(chunks) == 1
        assert chunks[0][1] == "Hello world"
        assert chunks[0][0] == "Hello world"

    def test_long_text_produces_multiple_chunks(self) -> None:
        text = "A" * 2500  # > 3 chunks of 800
        chunks = _split_text(text, chunk_size=800, overlap=100)
        assert len(chunks) >= 3

    def test_chunk_titles_derived_from_first_line(self) -> None:
        text = "Section Title\nbody content here"
        chunks = _split_text(text, chunk_size=800, overlap=0)
        assert chunks[0][0] == "Section Title"
        assert "body content" in chunks[0][1]

    def test_cjk_text_splits_by_character(self) -> None:
        text = "这是测试内容" * 200
        chunks = _split_text(text, chunk_size=800, overlap=100)
        assert len(chunks) >= 2


# =====================================================================
# DB-backed test fixtures
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
def seed(db):
    """Create minimal seed data: tenant + two users."""
    suffix = uuid.uuid4().hex[:8]

    tenant = Tenant(name=f"kb-tenant-{suffix}", status=TenantStatus.active)
    db.add(tenant)
    db.flush()

    pw_hash = "$2b$12$dummyhashfornonauthenticationpurposesonly"
    user_a = User(
        username=f"kb_user_a_{suffix}",
        password_hash=pw_hash,
        status=UserStatus.active,
    )
    user_b = User(
        username=f"kb_user_b_{suffix}",
        password_hash=pw_hash,
        status=UserStatus.active,
    )
    db.add_all([user_a, user_b])
    db.flush()

    return SimpleNamespace(
        tenant=tenant,
        user_a=user_a,
        user_b=user_b,
    )


# =====================================================================
# 1. Permission isolation (DB-backed)
# =====================================================================


class TestPermissionFiltering:
    """Test filter_chunks_by_permission with real chunk objects."""

    def _make_chunk(self, perms: dict) -> KnowledgeChunk:
        return KnowledgeChunk(
            document_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            title="test chunk",
            content="test content",
            chunk_index=0,
            permissions=perms,
        )

    def test_public_chunk_allowed(self) -> None:
        chunk = self._make_chunk(
            {
                "visibility": "public",
                "departments": [],
                "roles": [],
                "users": [],
            }
        )
        allowed, blocked = filter_chunks_by_permission([chunk], uuid.uuid4(), None, None)
        assert len(allowed) == 1
        assert len(blocked) == 0

    def test_restricted_chunk_blocked_for_unlisted_user(self) -> None:
        chunk = self._make_chunk(
            {
                "visibility": "restricted",
                "departments": ["sales"],
                "roles": [],
                "users": [],
            }
        )
        allowed, blocked = filter_chunks_by_permission([chunk], uuid.uuid4(), "engineering", None)
        assert len(allowed) == 0
        assert len(blocked) == 1

    def test_mixed_chunks_split_correctly(self) -> None:
        public_chunk = self._make_chunk(
            {
                "visibility": "public",
                "departments": [],
                "roles": [],
                "users": [],
            }
        )
        restricted_chunk = self._make_chunk(
            {
                "visibility": "restricted",
                "departments": ["sales"],
                "roles": [],
                "users": [],
            }
        )
        allowed, blocked = filter_chunks_by_permission(
            [public_chunk, restricted_chunk],
            uuid.uuid4(),
            "engineering",
            None,
        )
        assert len(allowed) == 1
        assert len(blocked) == 1
        assert allowed[0] is public_chunk
        assert blocked[0] is restricted_chunk


# =====================================================================
# 2. Q&A with audit (DB-backed)
# =====================================================================


class TestAnswerQuestion:
    """Test answer_question: FAQ hit, RAG retrieval, audit, gap creation."""

    def test_answer_from_published_faq(self, db, seed) -> None:
        """A published FAQ with a matching question returns immediately."""
        # Create a published FAQ
        faq = PublishedFaq(
            tenant_id=seed.tenant.id,
            cluster_key="test-key",
            question="how to reset password",
            answer="Click 'Forgot Password' on the login page.",
            related_chunk_ids=[],
            confidence=1.0,
        )
        db.add(faq)
        db.flush()

        result = answer_question(
            db,
            tenant_id=seed.tenant.id,
            user_id=seed.user_a.id,
            question="how to reset password",
            session_id="sess-1",
        )

        assert result["faq_hit"] is True
        assert "Forgot Password" in result["answer"]
        assert result["audit_id"] is not None
        assert result["gap_created"] is False

        # Verify audit record
        audits = list_audits(db, tenant_id=seed.tenant.id)
        assert len(audits) == 1
        assert audits[0].faq_hit is True
        assert audits[0].question == "how to reset password"

    def test_answer_from_rag_retrieval(self, db, seed) -> None:
        """Without a FAQ hit, the system retrieves chunks by keyword overlap."""
        # Import and publish a document
        doc = import_document(
            db,
            tenant_id=seed.tenant.id,
            title="Password Guide",
            content=(
                "To reset your password, go to settings. "
                "Click the reset button to receive an email."
            ),
            permissions={
                "visibility": "public",
                "departments": [],
                "roles": [],
                "users": [],
            },
            created_by=seed.user_a.id,
            chunk_size=800,
            chunk_overlap=0,
        )
        publish_document(db, doc.id)

        result = answer_question(
            db,
            tenant_id=seed.tenant.id,
            user_id=seed.user_a.id,
            question="how to reset password",
            session_id="sess-2",
        )

        assert result["faq_hit"] is False
        assert result["audit_id"] is not None
        # Should have found the chunk
        assert len(result["allowed_chunks"]) > 0
        assert "reset" in result["answer"].lower()

    def test_unanswered_question_creates_gap(self, db, seed) -> None:
        """A question with no matching chunks creates a knowledge gap."""
        result = answer_question(
            db,
            tenant_id=seed.tenant.id,
            user_id=seed.user_a.id,
            question="what is the airspeed velocity of an unladen swallow",
            session_id="sess-3",
        )

        assert result["faq_hit"] is False
        assert result["gap_created"] is True
        assert "未找到" in result["answer"]

        gaps = list_gaps(db, tenant_id=seed.tenant.id)
        assert len(gaps) == 1
        assert "swallow" in gaps[0].question

    def test_repeated_unanswered_question_increments_gap_frequency(self, db, seed) -> None:
        """Asking the same unanswered question twice increments frequency."""
        question = "where is the cafeteria"
        for _ in range(3):
            answer_question(
                db,
                tenant_id=seed.tenant.id,
                user_id=seed.user_a.id,
                question=question,
                session_id="sess-repeat",
            )

        gaps = list_gaps(db, tenant_id=seed.tenant.id)
        assert len(gaps) == 1
        assert gaps[0].frequency == 3

    def test_restricted_chunk_blocked_in_answer(self, db, seed) -> None:
        """A chunk with restricted visibility is blocked, not in the answer."""
        # Assign user_a to "engineering" department
        assign_knowledge_member(
            db,
            tenant_id=seed.tenant.id,
            user_id=seed.user_a.id,
            role=KnowledgeRole.viewer,
            department="engineering",
        )

        # Create a document restricted to "sales" only
        doc = import_document(
            db,
            tenant_id=seed.tenant.id,
            title="Sales Secrets",
            content="The secret sales strategy is to target enterprise clients.",
            permissions={
                "visibility": "restricted",
                "departments": ["sales"],
                "roles": [],
                "users": [],
            },
            created_by=seed.user_b.id,
        )
        publish_document(db, doc.id)

        result = answer_question(
            db,
            tenant_id=seed.tenant.id,
            user_id=seed.user_a.id,
            question="sales strategy",
            session_id="sess-blocked",
            department="engineering",
            role="viewer",
        )

        # User_a (engineering) should not see the sales-restricted chunk
        assert result["faq_hit"] is False
        assert len(result["blocked_chunks"]) > 0
        assert len(result["allowed_chunks"]) == 0
        assert result["missing_permission"] is True


# =====================================================================
# 3. FAQ clustering & publishing (DB-backed)
# =====================================================================


class TestFaqClustering:
    """Test cluster_faq_candidates and publish_faq."""

    def test_clustering_requires_min_frequency(self, db, seed) -> None:
        """Questions asked fewer than min_frequency times do not cluster."""
        for _ in range(3):
            answer_question(
                db,
                tenant_id=seed.tenant.id,
                user_id=seed.user_a.id,
                question="how to deploy",
                session_id="sess-cluster",
            )
        # Only 3 occurrences, min_frequency=4 should produce nothing
        candidates = cluster_faq_candidates(
            db,
            tenant_id=seed.tenant.id,
            days=7,
            min_frequency=4,
        )
        assert len(candidates) == 0

    def test_clustering_creates_candidate(self, db, seed) -> None:
        """Repeated questions create a FaqCandidate with correct frequency."""
        question = "how to configure ssl"
        for _ in range(5):
            answer_question(
                db,
                tenant_id=seed.tenant.id,
                user_id=seed.user_a.id,
                question=question,
                session_id="sess-cluster-2",
            )

        candidates = cluster_faq_candidates(
            db,
            tenant_id=seed.tenant.id,
            days=7,
            min_frequency=3,
        )
        assert len(candidates) == 1
        cand = candidates[0]
        assert cand.frequency == 5
        assert cand.status == FaqStatus.proposed
        assert "ssl" in cand.cluster_question.lower()

    def test_publish_faq_creates_published_record(self, db, seed) -> None:
        """Publishing a candidate creates a PublishedFaq and flips status."""
        # Seed: create audits -> cluster -> get candidate
        for _ in range(3):
            answer_question(
                db,
                tenant_id=seed.tenant.id,
                user_id=seed.user_a.id,
                question="how to create user",
                session_id="sess-publish",
            )
        candidates = cluster_faq_candidates(
            db,
            tenant_id=seed.tenant.id,
            days=7,
            min_frequency=3,
        )
        assert len(candidates) == 1

        # Publish
        published = publish_faq(
            db,
            candidate_id=candidates[0].id,
            answer="Go to admin panel and click add user.",
            published_by=seed.user_b.id,
        )
        assert published is not None
        assert published.question == candidates[0].cluster_question
        assert "admin panel" in published.answer

        # Verify candidate status updated
        cand = db.scalar(select(FaqCandidate).where(FaqCandidate.id == candidates[0].id))
        assert cand.status == FaqStatus.published

        # Verify published FAQ exists
        faqs = list(
            db.scalars(select(PublishedFaq).where(PublishedFaq.tenant_id == seed.tenant.id)).all()
        )
        assert len(faqs) == 1

    def test_reject_faq_sets_rejected_status(self, db, seed) -> None:
        """Rejecting a candidate sets status to rejected."""
        for _ in range(3):
            answer_question(
                db,
                tenant_id=seed.tenant.id,
                user_id=seed.user_a.id,
                question="how to delete account",
                session_id="sess-reject",
            )
        candidates = cluster_faq_candidates(
            db,
            tenant_id=seed.tenant.id,
            days=7,
            min_frequency=3,
        )
        assert len(candidates) == 1

        updated = reject_faq(db, candidate_id=candidates[0].id)
        assert updated is not None
        assert updated.status == FaqStatus.rejected

    def test_published_faq_short_circuits_retrieval(self, db, seed) -> None:
        """After a FAQ is published, the same question hits FAQ, not RAG."""
        # Create an audit trail for clustering
        for _ in range(3):
            answer_question(
                db,
                tenant_id=seed.tenant.id,
                user_id=seed.user_a.id,
                question="what is etl",
                session_id="sess-shortcircuit",
            )

        candidates = cluster_faq_candidates(
            db,
            tenant_id=seed.tenant.id,
            days=7,
            min_frequency=3,
        )
        assert len(candidates) == 1

        publish_faq(
            db,
            candidate_id=candidates[0].id,
            answer="ETL stands for Extract, Transform, Load.",
        )

        # Now ask the same question again
        result = answer_question(
            db,
            tenant_id=seed.tenant.id,
            user_id=seed.user_a.id,
            question="what is etl",
            session_id="sess-after-publish",
        )
        assert result["faq_hit"] is True
        assert "Extract" in result["answer"]


# =====================================================================
# 4. Knowledge-gap diagnosis (DB-backed)
# =====================================================================


class TestKnowledgeGaps:
    """Test gap creation, listing, and resolution."""

    def test_list_gaps_returns_open_gaps(self, db, seed) -> None:
        """list_gaps returns open gaps sorted by frequency."""
        # Create two different unanswered questions with different frequencies
        for _ in range(5):
            answer_question(
                db,
                tenant_id=seed.tenant.id,
                user_id=seed.user_a.id,
                question="what is kubernetes",
                session_id="sess-gap-1",
            )
        for _ in range(2):
            answer_question(
                db,
                tenant_id=seed.tenant.id,
                user_id=seed.user_a.id,
                question="what is docker",
                session_id="sess-gap-2",
            )

        gaps = list_gaps(db, tenant_id=seed.tenant.id)
        assert len(gaps) == 2
        # Higher-frequency gap should be first
        assert gaps[0].frequency == 5
        assert gaps[1].frequency == 2
        assert all(g.status == KnowledgeGapStatus.open for g in gaps)

    def test_list_gaps_filters_by_status(self, db, seed) -> None:
        """list_gaps with status filter returns only matching gaps."""
        answer_question(
            db,
            tenant_id=seed.tenant.id,
            user_id=seed.user_a.id,
            question="what is terraform",
            session_id="sess-filter",
        )

        # Resolve it
        all_gaps = list_gaps(db, tenant_id=seed.tenant.id)
        assert len(all_gaps) == 1
        resolve_gap(db, gap_id=all_gaps[0].id, resolved_by=seed.user_b.id)

        # Should not appear in open gaps
        open_gaps = list_gaps(db, tenant_id=seed.tenant.id, status=KnowledgeGapStatus.open)
        assert len(open_gaps) == 0

        # Should appear in resolved gaps
        resolved_gaps = list_gaps(db, tenant_id=seed.tenant.id, status=KnowledgeGapStatus.resolved)
        assert len(resolved_gaps) == 1
        assert resolved_gaps[0].status == KnowledgeGapStatus.resolved

    def test_resolve_gap_sets_resolved_by_and_timestamp(self, db, seed) -> None:
        """resolve_gap sets the resolved_by and resolved_at fields."""
        answer_question(
            db,
            tenant_id=seed.tenant.id,
            user_id=seed.user_a.id,
            question="what is ansible",
            session_id="sess-resolve",
        )
        gaps = list_gaps(db, tenant_id=seed.tenant.id)
        assert len(gaps) == 1

        resolved = resolve_gap(db, gap_id=gaps[0].id, resolved_by=seed.user_b.id)
        assert resolved is not None
        assert resolved.status == KnowledgeGapStatus.resolved
        assert resolved.resolved_by == seed.user_b.id
        assert resolved.resolved_at is not None


# =====================================================================
# 5. Operational metrics (DB-backed)
# =====================================================================


class TestOperationMetrics:
    """Test get_operation_metrics aggregation."""

    def test_empty_metrics_for_new_tenant(self, db, seed) -> None:
        """A tenant with no Q&A activity should have zero metrics."""
        metrics = get_operation_metrics(db, tenant_id=seed.tenant.id, days=7)
        assert metrics["total_questions"] == 0
        assert metrics["unique_users"] == 0
        assert metrics["faq_hit_count"] == 0
        assert metrics["faq_hit_rate"] == 0.0
        assert metrics["blocked_access_count"] == 0
        assert metrics["published_documents"] == 0
        assert metrics["open_gaps"] == 0
        assert metrics["proposed_faqs"] == 0
        assert len(metrics["daily"]) == 7

    def test_metrics_reflect_qa_activity(self, db, seed) -> None:
        """Metrics should reflect questions, FAQ hits, and gaps."""
        # Three unanswered questions -> 3 gaps, 3 questions
        answer_question(
            db,
            tenant_id=seed.tenant.id,
            user_id=seed.user_a.id,
            question="unanswered q1",
            session_id="sess-m1",
        )
        answer_question(
            db,
            tenant_id=seed.tenant.id,
            user_id=seed.user_b.id,
            question="unanswered q2",
            session_id="sess-m2",
        )

        # Create and publish a FAQ, then ask it
        for _ in range(3):
            answer_question(
                db,
                tenant_id=seed.tenant.id,
                user_id=seed.user_a.id,
                question="faq question",
                session_id="sess-m3",
            )
        candidates = cluster_faq_candidates(
            db,
            tenant_id=seed.tenant.id,
            days=7,
            min_frequency=3,
        )
        assert len(candidates) == 1
        publish_faq(
            db,
            candidate_id=candidates[0].id,
            answer="faq answer",
        )

        # Ask the published FAQ question
        faq_result = answer_question(
            db,
            tenant_id=seed.tenant.id,
            user_id=seed.user_a.id,
            question="faq question",
            session_id="sess-m4",
        )
        assert faq_result["faq_hit"] is True

        metrics = get_operation_metrics(db, tenant_id=seed.tenant.id, days=7)

        # total_questions = 2 (unanswered) + 3 (for clustering) + 1 (faq hit) = 6
        assert metrics["total_questions"] == 6
        assert metrics["unique_users"] == 2  # user_a + user_b
        assert metrics["faq_hit_count"] == 1
        assert metrics["open_gaps"] >= 2  # at least "unanswered q1" and "q2"
        # "faq question" gap may also exist from before publishing
        assert metrics["proposed_faqs"] == 0  # the one candidate was published

    def test_published_documents_counted(self, db, seed) -> None:
        """published_documents counts docs with status=published."""
        import_document(
            db,
            tenant_id=seed.tenant.id,
            title="Doc 1",
            content="content",
        )
        doc2 = import_document(
            db,
            tenant_id=seed.tenant.id,
            title="Doc 2",
            content="content",
        )
        publish_document(db, doc2.id)

        metrics = get_operation_metrics(db, tenant_id=seed.tenant.id, days=7)
        assert metrics["published_documents"] == 1


# =====================================================================
# 6. Document & chunk management (DB-backed)
# =====================================================================


class TestDocumentManagement:
    """Test import_document and publish_document."""

    def test_import_document_creates_chunks(self, db, seed) -> None:
        """Importing a document auto-splits it into chunks."""
        long_text = "This is paragraph one.\n\n" + "Content " * 200
        doc = import_document(
            db,
            tenant_id=seed.tenant.id,
            title="Test Document",
            content=long_text,
            category="test",
            created_by=seed.user_a.id,
        )

        assert doc.status == KnowledgeDocumentStatus.draft
        assert doc.title == "Test Document"

        chunks = list(
            db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == doc.id)).all()
        )
        assert len(chunks) >= 2
        assert all(c.tenant_id == seed.tenant.id for c in chunks)
        assert all(c.permissions == doc.permissions for c in chunks)

    def test_publish_document_changes_status(self, db, seed) -> None:
        """Publishing a document changes status from draft to published."""
        doc = import_document(
            db,
            tenant_id=seed.tenant.id,
            title="Publishable Doc",
            content="Short content",
        )
        assert doc.status == KnowledgeDocumentStatus.draft

        published = publish_document(db, doc.id)
        assert published is not None
        assert published.status == KnowledgeDocumentStatus.published

    def test_import_document_with_custom_permissions(self, db, seed) -> None:
        """Importing with custom permissions propagates to chunks."""
        perms = {
            "visibility": "restricted",
            "departments": ["engineering"],
            "roles": [],
            "users": [str(seed.user_a.id)],
        }
        doc = import_document(
            db,
            tenant_id=seed.tenant.id,
            title="Restricted Doc",
            content="restricted content",
            permissions=perms,
        )

        chunks = list(
            db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == doc.id)).all()
        )
        assert len(chunks) == 1
        assert chunks[0].permissions["visibility"] == "restricted"
        assert "engineering" in chunks[0].permissions["departments"]


class TestRolePermissions:
    """Tests for tenant-scoped button permission overrides."""

    def test_replacing_permissions_flushes_deleted_rows_first(self, db, seed) -> None:
        set_role_permissions(
            db,
            tenant_id=seed.tenant.id,
            role_name="viewer",
            permissions=["kb:document:read", "kb:ask"],
        )
        set_role_permissions(
            db,
            tenant_id=seed.tenant.id,
            role_name="viewer",
            permissions=["kb:document:read"],
        )
        result = set_role_permissions(
            db,
            tenant_id=seed.tenant.id,
            role_name="viewer",
            permissions=["kb:document:read", "kb:ask", "kb:metrics:read"],
        )
        assert result["viewer"] == [
            "kb:ask",
            "kb:document:read",
            "kb:metrics:read",
        ]
        assert list_role_permissions(db, seed.tenant.id)["viewer"] == result["viewer"]

    def test_tenant_admin_cannot_be_downgraded(self, db, seed) -> None:
        result = set_role_permissions(
            db,
            tenant_id=seed.tenant.id,
            role_name="tenant_admin",
            permissions=["kb:ask"],
        )
        assert "dept:manage" in result["tenant_admin"]
        assert "perm:manage" in result["tenant_admin"]
        assert "kb:ask" in result["tenant_admin"]
