"""create knowledge platform tables

Revision ID: d3e4f5a6b7c8
Revises: 62a3a35eda2a
Create Date: 2026-09-09 20:20:00.000000

Creates the optional knowledge-management module: role memberships,
documents/chunks, FAQ candidates/published FAQs, Q&A audits and
knowledge-gap diagnoses.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d3e4f5a6b7c8"
down_revision: str | None = "62a3a35eda2a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_members",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "viewer",
                "knowledge_admin",
                "system_admin",
                name="knowledge_role",
                native_enum=True,
            ),
            nullable=False,
            server_default="viewer",
        ),
        sa.Column("department", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            name="uq_knowledge_member_tenant_user",
        ),
    )
    op.create_index(
        "ix_knowledge_members_tenant_id",
        "knowledge_members",
        ["tenant_id"],
    )
    op.create_index(
        "ix_knowledge_members_user_id",
        "knowledge_members",
        ["user_id"],
    )

    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=128), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "published",
                "archived",
                name="knowledge_document_status",
                native_enum=True,
            ),
            nullable=False,
            server_default="draft",
        ),
        sa.Column(
            "permissions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_documents_tenant_id",
        "knowledge_documents",
        ["tenant_id"],
    )
    op.create_index(
        "ix_knowledge_documents_status",
        "knowledge_documents",
        ["status"],
    )
    op.create_index(
        "ix_knowledge_documents_title",
        "knowledge_documents",
        ["title"],
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "permissions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["knowledge_documents.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_chunks_document_id",
        "knowledge_chunks",
        ["document_id"],
    )
    op.create_index(
        "ix_knowledge_chunks_tenant_id",
        "knowledge_chunks",
        ["tenant_id"],
    )

    op.create_table(
        "faq_candidates",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("cluster_key", sa.String(length=255), nullable=False),
        sa.Column("cluster_question", sa.Text(), nullable=False),
        sa.Column("frequency", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "related_chunk_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "recommended_answer",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "status",
            sa.Enum(
                "proposed",
                "published",
                "rejected",
                name="faq_status",
                native_enum=True,
            ),
            nullable=False,
            server_default="proposed",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_faq_candidates_status",
        "faq_candidates",
        ["status"],
    )
    op.create_index(
        "ix_faq_candidates_tenant_id",
        "faq_candidates",
        ["tenant_id"],
    )
    op.create_index(
        "ix_faq_candidates_cluster_key",
        "faq_candidates",
        ["cluster_key"],
    )

    op.create_table(
        "published_faqs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("cluster_key", sa.String(length=255), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column(
            "related_chunk_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1"),
        sa.Column("published_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["published_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_published_faqs_tenant_id",
        "published_faqs",
        ["tenant_id"],
    )
    op.create_index(
        "ix_published_faqs_cluster_key",
        "published_faqs",
        ["cluster_key"],
    )

    op.create_table(
        "knowledge_qa_audits",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column(
            "recalled_chunk_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "allowed_chunk_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "blocked_chunk_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("faq_hit", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("token_estimate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_qa_audits_created_at",
        "knowledge_qa_audits",
        ["created_at"],
    )
    op.create_index(
        "ix_knowledge_qa_audits_tenant_id",
        "knowledge_qa_audits",
        ["tenant_id"],
    )
    op.create_index(
        "ix_knowledge_qa_audits_user_id",
        "knowledge_qa_audits",
        ["user_id"],
    )
    op.create_index(
        "ix_knowledge_qa_audits_session_id",
        "knowledge_qa_audits",
        ["session_id"],
    )

    op.create_table(
        "knowledge_gaps",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("department", sa.String(length=128), nullable=True),
        sa.Column("frequency", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "highest_similarity",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("suggested_category", sa.String(length=128), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "open",
                "in_progress",
                "resolved",
                name="knowledge_gap_status",
                native_enum=True,
            ),
            nullable=False,
            server_default="open",
        ),
        sa.Column("resolved_by", sa.UUID(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_gaps_status",
        "knowledge_gaps",
        ["status"],
    )
    op.create_index(
        "ix_knowledge_gaps_tenant_id",
        "knowledge_gaps",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_table("knowledge_gaps")
    op.execute("DROP TYPE IF EXISTS knowledge_gap_status")
    op.drop_table("knowledge_qa_audits")
    op.drop_table("published_faqs")
    op.drop_table("faq_candidates")
    op.execute("DROP TYPE IF EXISTS faq_status")
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_documents")
    op.execute("DROP TYPE IF EXISTS knowledge_document_status")
    op.drop_table("knowledge_members")
    op.execute("DROP TYPE IF EXISTS knowledge_role")
