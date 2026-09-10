"""Pydantic schemas for the knowledge-management platform."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from etl_agent.models.knowledge import KnowledgeRole


class PermissionRule(BaseModel):
    """Four-dimensional access rule attached to a knowledge unit."""

    visibility: str = Field(default="public", pattern="^(public|tenant|restricted)$")
    departments: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    users: list[uuid.UUID] = Field(default_factory=list)


class DocumentCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    category: str | None = Field(default=None, max_length=128)
    content: str = Field(min_length=1)
    permissions: PermissionRule = Field(default_factory=PermissionRule)


class DocumentBatchCreateRequest(BaseModel):
    documents: list[DocumentCreateRequest] = Field(min_length=1, max_length=100)


class DocumentBatchResponse(BaseModel):
    total: int
    created: int
    document_ids: list[uuid.UUID]
    chunk_count: int


class ChunkUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)
    permissions: PermissionRule | None = None


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(default="", max_length=128)


class ChunkResponse(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    title: str
    content: str
    chunk_index: int
    permissions: dict[str, Any]


class DocumentResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    title: str
    category: str | None
    status: str
    permissions: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    chunks: list[ChunkResponse] = Field(default_factory=list)


class DocumentListItem(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    title: str
    category: str | None
    status: str
    chunk_count: int = 0
    created_at: datetime
    updated_at: datetime


class AskResponse(BaseModel):
    question: str
    answer: str
    session_id: str
    audit_id: uuid.UUID
    faq_hit: bool
    faq_cache_hit: bool = False
    allowed_chunks: list[ChunkResponse]
    blocked_chunks: list[ChunkResponse]
    missing_permission: bool
    gap_created: bool
    latency_ms: int


class CandidateResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    cluster_key: str
    cluster_question: str
    frequency: int
    related_chunk_ids: list[uuid.UUID]
    recommended_answer: str
    confidence: float
    status: str


class PublishCandidateRequest(BaseModel):
    answer: str | None = Field(default=None, max_length=8000)
    question: str | None = Field(default=None, max_length=2000)


class PublishedFaqResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    cluster_key: str
    question: str
    answer: str
    related_chunk_ids: list[uuid.UUID]
    confidence: float


class AuditResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    session_id: str
    question: str
    answer: str
    recalled_chunk_ids: list[uuid.UUID]
    allowed_chunk_ids: list[uuid.UUID]
    blocked_chunk_ids: list[uuid.UUID]
    faq_hit: bool
    token_estimate: int
    latency_ms: int
    created_at: datetime


class GapResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    question: str
    department: str | None
    frequency: int
    highest_similarity: float
    suggested_category: str | None
    status: str
    created_at: datetime
    updated_at: datetime


class ResolveGapRequest(BaseModel):
    document_id: uuid.UUID


class KnowledgeMemberRequest(BaseModel):
    user_id: uuid.UUID
    role: KnowledgeRole
    department: str | None = Field(default=None, max_length=128)


class KnowledgeMemberResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    username: str | None = None
    role: str
    department: str | None


class DepartmentCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=1, max_length=64)
    parent_id: uuid.UUID | None = None
    sort_order: int = Field(default=0, ge=0)


class DepartmentUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    code: str | None = Field(default=None, min_length=1, max_length=64)
    parent_id: uuid.UUID | None = None
    sort_order: int | None = Field(default=None, ge=0)


class DepartmentResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    code: str
    sort_order: int
    children: list[DepartmentResponse] = Field(default_factory=list)


class RolePermissionRequest(BaseModel):
    role_name: str = Field(min_length=1, max_length=64)
    permissions: list[str] = Field(default_factory=list)


class RolePermissionResponse(BaseModel):
    role_name: str
    permissions: list[str]


class KnowledgeAccessResponse(BaseModel):
    role_name: str
    permissions: list[str]


class OperationMetricsResponse(BaseModel):
    total_questions: int
    access_count: int = 0
    unique_users: int
    faq_hit_count: int
    faq_hit_rate: float
    average_latency_ms: float
    total_tokens: int = 0
    blocked_access_count: int
    knowledge_units: int = 0
    published_documents: int
    open_gaps: int
    proposed_faqs: int
    resolved_gaps: int = 0
    top_questions: list[dict[str, Any]] = Field(default_factory=list)
    hot_knowledge: list[dict[str, Any]] = Field(default_factory=list)
    daily: list[dict[str, Any]] = Field(default_factory=list)


__all__ = [
    "AskRequest",
    "AskResponse",
    "AuditResponse",
    "CandidateResponse",
    "DepartmentCreateRequest",
    "DepartmentResponse",
    "DepartmentUpdateRequest",
    "ChunkResponse",
    "ChunkUpdateRequest",
    "DocumentBatchCreateRequest",
    "DocumentBatchResponse",
    "DocumentCreateRequest",
    "DocumentListItem",
    "DocumentResponse",
    "GapResponse",
    "KnowledgeMemberRequest",
    "KnowledgeMemberResponse",
    "KnowledgeAccessResponse",
    "OperationMetricsResponse",
    "PermissionRule",
    "PublishCandidateRequest",
    "PublishedFaqResponse",
    "RolePermissionRequest",
    "RolePermissionResponse",
    "ResolveGapRequest",
]
