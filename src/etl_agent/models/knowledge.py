"""Knowledge management platform models (optional step 7 module).

The knowledge platform adds a second product surface on top of the ETL
control plane: documents are chunked, every chunk carries four-dimensional
permission rules, questions are answered from allowed chunks, and every
answer is audited. FAQ candidates, knowledge gaps and operational metrics
are derived from those audit records.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from etl_agent.db.base import Base
from etl_agent.models.mixins import TimestampMixin, UUIDPKMixin


class KnowledgeRole(enum.StrEnum):
    """Roles on the knowledge-management side of the product."""

    viewer = "viewer"
    knowledge_admin = "knowledge_admin"
    system_admin = "system_admin"


class KnowledgeDocumentStatus(enum.StrEnum):
    """Lifecycle state of a knowledge document."""

    draft = "draft"
    published = "published"
    archived = "archived"


class FaqStatus(enum.StrEnum):
    """Lifecycle state of a FAQ candidate or published answer."""

    proposed = "proposed"
    published = "published"
    rejected = "rejected"


class KnowledgeGapStatus(enum.StrEnum):
    """Lifecycle state of a knowledge-gap diagnosis record."""

    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"


class KnowledgeMember(UUIDPKMixin, TimestampMixin, Base):
    """Knowledge-side role assignment inside a tenant."""

    __tablename__ = "knowledge_members"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            name="uq_knowledge_member_tenant_user",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[KnowledgeRole] = mapped_column(
        Enum(KnowledgeRole, name="knowledge_role", native_enum=True),
        nullable=False,
        default=KnowledgeRole.viewer,
    )
    department: Mapped[str | None] = mapped_column(String(128), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<KnowledgeMember(id={self.id}, tenant={self.tenant_id}, "
            f"user={self.user_id}, role={self.role.value})>"
        )


class KnowledgeDocument(UUIDPKMixin, TimestampMixin, Base):
    """A source document imported into the knowledge base."""

    __tablename__ = "knowledge_documents"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[KnowledgeDocumentStatus] = mapped_column(
        Enum(
            KnowledgeDocumentStatus,
            name="knowledge_document_status",
            native_enum=True,
        ),
        nullable=False,
        default=KnowledgeDocumentStatus.draft,
        index=True,
    )
    permissions: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment=(
            "Four dimensions: visibility, departments, roles and users"
        ),
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    def __repr__(self) -> str:
        return f"<KnowledgeDocument(id={self.id}, title={self.title!r})>"


class KnowledgeChunk(UUIDPKMixin, Base):
    """A chunked knowledge unit derived from a document."""

    __tablename__ = "knowledge_chunks"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    permissions: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<KnowledgeChunk(id={self.id}, title={self.title!r})>"


class FaqCandidate(UUIDPKMixin, TimestampMixin, Base):
    """A candidate FAQ surfaced from repeated questions."""

    __tablename__ = "faq_candidates"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cluster_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    cluster_question: Mapped[str] = mapped_column(Text, nullable=False)
    frequency: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    related_chunk_ids: Mapped[list[uuid.UUID]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
    )
    recommended_answer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[FaqStatus] = mapped_column(
        Enum(FaqStatus, name="faq_status", native_enum=True),
        nullable=False,
        default=FaqStatus.proposed,
        index=True,
    )

    def __repr__(self) -> str:
        return f"<FaqCandidate(id={self.id}, question={self.cluster_question!r})>"


class PublishedFaq(UUIDPKMixin, TimestampMixin, Base):
    """An admin-reviewed FAQ answer ready for direct matching."""

    __tablename__ = "published_faqs"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cluster_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    related_chunk_ids: Mapped[list[uuid.UUID]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    def __repr__(self) -> str:
        return f"<PublishedFaq(id={self.id}, question={self.question!r})>"


class KnowledgeQaAudit(UUIDPKMixin, Base):
    """One audit record for every knowledge Q&A call."""

    __tablename__ = "knowledge_qa_audits"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    recalled_chunk_ids: Mapped[list[uuid.UUID]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
    )
    allowed_chunk_ids: Mapped[list[uuid.UUID]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
    )
    blocked_chunk_ids: Mapped[list[uuid.UUID]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
    )
    faq_hit: Mapped[bool] = mapped_column(nullable=False, default=False)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    def __repr__(self) -> str:
        return f"<KnowledgeQaAudit(id={self.id}, question={self.question[:40]!r})>"


class KnowledgeGap(UUIDPKMixin, TimestampMixin, Base):
    """A knowledge gap discovered from unanswered questions."""

    __tablename__ = "knowledge_gaps"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    department: Mapped[str | None] = mapped_column(String(128), nullable=True)
    frequency: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    highest_similarity: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
    )
    suggested_category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[KnowledgeGapStatus] = mapped_column(
        Enum(KnowledgeGapStatus, name="knowledge_gap_status", native_enum=True),
        nullable=False,
        default=KnowledgeGapStatus.open,
        index=True,
    )
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    def __repr__(self) -> str:
        return f"<KnowledgeGap(id={self.id}, question={self.question[:40]!r})>"


__all__ = [
    "FaqCandidate",
    "FaqStatus",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "KnowledgeDocumentStatus",
    "KnowledgeGap",
    "KnowledgeGapStatus",
    "KnowledgeMember",
    "KnowledgeQaAudit",
    "KnowledgeRole",
    "PublishedFaq",
]
