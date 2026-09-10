"""Knowledge-management API routes.

Routes:
    POST   /knowledge/documents                -- import a document
    GET    /knowledge/documents                 -- list documents
    GET    /knowledge/documents/{id}            -- get document + chunks
    POST   /knowledge/documents/{id}/publish   -- publish a document
    POST   /knowledge/documents/{id}/archive    -- archive a document
    PUT    /knowledge/chunks/{id}               -- update a chunk
    DELETE /knowledge/chunks/{id}              -- delete a chunk
    POST   /knowledge/ask                       -- ask a question (RAG)
    GET    /knowledge/faqs/candidates           -- list FAQ candidates
    POST   /knowledge/faqs/{id}/publish         -- publish a FAQ candidate
    POST   /knowledge/faqs/{id}/reject          -- reject a FAQ candidate
    GET    /knowledge/faqs/published            -- list published FAQs
    POST   /knowledge/cluster                   -- trigger FAQ clustering
    GET    /knowledge/gaps                      -- list knowledge gaps
    POST   /knowledge/gaps/{id}/resolve         -- resolve a gap
    GET    /knowledge/audits                    -- list Q&A audit records
    GET    /knowledge/metrics                   -- get operational metrics
    POST   /knowledge/members                   -- assign knowledge role
    GET    /knowledge/members                   -- list knowledge members
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from etl_agent.core.dependencies import CurrentUser, get_current_user
from etl_agent.db.session import get_db
from etl_agent.models.department import Department
from etl_agent.models.knowledge import (
    FaqCandidate,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeGap,
    KnowledgeGapStatus,
    KnowledgeMember,
    KnowledgeQaAudit,
    PublishedFaq,
)
from etl_agent.models.user import User
from etl_agent.schemas.common import PageResponse
from etl_agent.schemas.knowledge import (
    AskRequest,
    AskResponse,
    AuditResponse,
    CandidateResponse,
    ChunkResponse,
    ChunkUpdateRequest,
    DepartmentCreateRequest,
    DepartmentResponse,
    DepartmentUpdateRequest,
    DocumentBatchCreateRequest,
    DocumentBatchResponse,
    DocumentCreateRequest,
    DocumentListItem,
    DocumentResponse,
    GapResponse,
    KnowledgeAccessResponse,
    KnowledgeMemberRequest,
    KnowledgeMemberResponse,
    OperationMetricsResponse,
    PublishCandidateRequest,
    PublishedFaqResponse,
    ResolveGapRequest,
    RolePermissionRequest,
    RolePermissionResponse,
)
from etl_agent.services.knowledge_service import (
    answer_question,
    archive_document,
    assign_knowledge_member,
    cluster_faq_candidates,
    create_department,
    delete_audit,
    delete_department,
    delete_document,
    delete_faq_candidate,
    delete_gap,
    delete_published_faq,
    get_document_chunks,
    get_knowledge_member,
    get_operation_metrics,
    import_document,
    list_audits,
    list_departments,
    list_gaps,
    list_role_permissions,
    publish_document,
    publish_faq,
    reject_faq,
    resolve_gap,
    seed_knowledge_demo,
    set_role_permissions,
    update_chunk,
    update_department,
)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


# =====================================================================
# Helpers
# =====================================================================


def _chunk_to_response(chunk: KnowledgeChunk) -> ChunkResponse:
    return ChunkResponse(
        id=chunk.id,
        document_id=chunk.document_id,
        title=chunk.title,
        content=chunk.content,
        chunk_index=chunk.chunk_index,
        permissions=chunk.permissions,
    )


def _doc_to_response(
    doc: KnowledgeDocument,
    chunks: list[KnowledgeChunk],
) -> DocumentResponse:
    return DocumentResponse(
        id=doc.id,
        tenant_id=doc.tenant_id,
        title=doc.title,
        category=doc.category,
        status=doc.status.value,
        permissions=doc.permissions,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        chunks=[_chunk_to_response(c) for c in chunks],
    )


def _doc_to_list_item(doc: KnowledgeDocument, chunk_count: int) -> DocumentListItem:
    return DocumentListItem(
        id=doc.id,
        tenant_id=doc.tenant_id,
        title=doc.title,
        category=doc.category,
        status=doc.status.value,
        chunk_count=chunk_count,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


def _get_tenant_id(current_user: CurrentUser) -> uuid.UUID:
    """Extract tenant_id from the current user context."""
    if current_user.tenant_id is None:
        raise HTTPException(
            status_code=403,
            detail="Knowledge platform requires a tenant context",
        )
    return current_user.tenant_id


def _get_user_dept_role(
    db: Session,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[str | None, str | None]:
    """Get the user's department and knowledge role."""
    member = get_knowledge_member(db, tenant_id, user_id)
    if member is not None:
        return member.department, member.role.value
    return None, None


def _effective_knowledge_role(
    db: Session,
    tenant_id: uuid.UUID,
    current_user: CurrentUser,
) -> str:
    """Return the role used for knowledge-side button permissions."""
    if current_user.role == "tenant_admin":
        return "tenant_admin"
    member = get_knowledge_member(db, tenant_id, current_user.user_id)
    if member is not None:
        return member.role.value
    return current_user.role or "viewer"


def _require_permission(
    db: Session,
    tenant_id: uuid.UUID,
    current_user: CurrentUser,
    permission_code: str,
) -> None:
    """Require one button-level permission, with tenant admin as a bypass."""
    if current_user.role == "tenant_admin":
        return
    role_name = _effective_knowledge_role(db, tenant_id, current_user)
    role_permissions = list_role_permissions(db, tenant_id).get(role_name, [])
    if permission_code not in role_permissions:
        raise HTTPException(
            status_code=403,
            detail=f"Permission required: {permission_code}",
        )


@router.get(
    "/access",
    response_model=KnowledgeAccessResponse,
)
def get_knowledge_access(
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> KnowledgeAccessResponse:
    """Return the current user's effective knowledge-side permissions."""
    tenant_id = _get_tenant_id(current_user)
    role_name = _effective_knowledge_role(db, tenant_id, current_user)
    permissions = list_role_permissions(db, tenant_id).get(role_name, [])
    return KnowledgeAccessResponse(
        role_name=role_name,
        permissions=sorted(permissions),
    )


# =====================================================================
# Document endpoints
# =====================================================================


@router.post(
    "/demo/seed",
    response_model=dict[str, int],
)
def seed_demo(
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> dict[str, int]:
    """Load the sample knowledge documents used by the demo scenarios."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:document:create")
    return seed_knowledge_demo(
        db,
        tenant_id=tenant_id,
        created_by=current_user.user_id,
    )


@router.post(
    "/documents",
    response_model=DocumentResponse,
    status_code=201,
)
def create_document(
    body: DocumentCreateRequest,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> DocumentResponse:
    """Import a new document with auto-chunking."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:document:create")
    doc = import_document(
        db,
        tenant_id=tenant_id,
        title=body.title,
        content=body.content,
        category=body.category,
        permissions=body.permissions.model_dump(mode="json"),
        created_by=current_user.user_id,
    )
    chunks = get_document_chunks(db, doc.id)
    db.commit()
    return _doc_to_response(doc, chunks)


@router.post(
    "/documents/batch",
    response_model=DocumentBatchResponse,
    status_code=201,
)
def create_documents_batch(
    body: DocumentBatchCreateRequest,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> DocumentBatchResponse:
    """Import up to 100 documents in one operation."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(
        db,
        tenant_id,
        current_user,
        "kb:document:batch",
    )
    document_ids: list[uuid.UUID] = []
    chunk_count = 0
    for item in body.documents:
        doc = import_document(
            db,
            tenant_id=tenant_id,
            title=item.title,
            content=item.content,
            category=item.category,
            permissions=item.permissions.model_dump(mode="json"),
            created_by=current_user.user_id,
        )
        document_ids.append(doc.id)
        chunk_count += len(get_document_chunks(db, doc.id))
    db.commit()
    return DocumentBatchResponse(
        total=len(body.documents),
        created=len(document_ids),
        document_ids=document_ids,
        chunk_count=chunk_count,
    )


@router.get(
    "/documents",
    response_model=PageResponse[DocumentListItem],
)
def list_documents(
    page: int = Query(default=1, ge=1),  # noqa: B008
    page_size: int = Query(default=50, ge=1, le=500),  # noqa: B008
    status: str | None = Query(default=None),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> PageResponse[DocumentListItem]:
    """List documents in the current tenant (paginated)."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:document:read")
    stmt = (
        select(KnowledgeDocument)
        .where(KnowledgeDocument.tenant_id == tenant_id)
        .order_by(KnowledgeDocument.created_at.desc())
    )
    if status:
        stmt = stmt.where(KnowledgeDocument.status == status)
    docs = list(db.scalars(stmt).all())
    total = len(docs)
    start = (page - 1) * page_size
    paged = docs[start : start + page_size]

    items: list[DocumentListItem] = []
    for doc in paged:
        all_chunks = list(
            db.scalars(
                select(KnowledgeChunk).where(
                    KnowledgeChunk.document_id == doc.id,
                ),
            ).all(),
        )
        count = len(all_chunks)
        items.append(_doc_to_list_item(doc, count))

    return PageResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/documents/{doc_id}",
    response_model=DocumentResponse,
)
def get_document(
    doc_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> DocumentResponse:
    """Get a document with all its chunks."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:document:read")
    doc = db.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.id == doc_id,
            KnowledgeDocument.tenant_id == tenant_id,
        ),
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    chunks = get_document_chunks(db, doc.id)
    return _doc_to_response(doc, chunks)


@router.post(
    "/documents/{doc_id}/publish",
    response_model=DocumentResponse,
)
def publish_doc(
    doc_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> DocumentResponse:
    """Publish a document (make its chunks searchable)."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:document:publish")
    doc = publish_document(db, doc_id)
    if doc is None or doc.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")
    chunks = get_document_chunks(db, doc.id)
    db.commit()
    return _doc_to_response(doc, chunks)


@router.post(
    "/documents/{doc_id}/archive",
    response_model=DocumentResponse,
)
def archive_doc(
    doc_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> DocumentResponse:
    """Archive a document."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:document:archive")
    doc = archive_document(db, doc_id)
    if doc is None or doc.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")
    chunks = get_document_chunks(db, doc.id)
    db.commit()
    return _doc_to_response(doc, chunks)


@router.delete(
    "/documents/{doc_id}",
    status_code=204,
)
def delete_doc(
    doc_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> None:
    """Delete a document and all of its chunks."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:document:delete")
    doc = db.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.id == doc_id,
            KnowledgeDocument.tenant_id == tenant_id,
        ),
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    delete_document(db, doc_id)
    db.commit()


# =====================================================================
# Chunk endpoints
# =====================================================================


@router.put(
    "/chunks/{chunk_id}",
    response_model=ChunkResponse,
)
def update_chunk_endpoint(
    chunk_id: uuid.UUID,
    body: ChunkUpdateRequest,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> ChunkResponse:
    """Update a chunk's title, content, or permissions."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:chunk:update")
    chunk = update_chunk(
        db,
        chunk_id,
        title=body.title,
        content=body.content,
        permissions=body.permissions.model_dump(mode="json") if body.permissions else None,
    )
    if chunk is None or chunk.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Chunk not found")
    db.commit()
    return _chunk_to_response(chunk)


@router.delete(
    "/chunks/{chunk_id}",
    status_code=204,
)
def delete_chunk_endpoint(
    chunk_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> None:
    """Delete a chunk."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:chunk:delete")
    chunk = db.scalar(
        select(KnowledgeChunk).where(
            KnowledgeChunk.id == chunk_id,
            KnowledgeChunk.tenant_id == tenant_id,
        ),
    )
    if chunk is None:
        raise HTTPException(status_code=404, detail="Chunk not found")
    db.delete(chunk)
    db.commit()


# =====================================================================
# Q&A endpoint
# =====================================================================


@router.post(
    "/ask",
    response_model=AskResponse,
)
def ask_question(
    body: AskRequest,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> AskResponse:
    """Ask a question and get an answer from the knowledge base.

    The system:
    1. Checks published FAQs for a direct hit.
    2. If no FAQ hit, retrieves top-k chunks by keyword overlap.
    3. Filters chunks by four-dimensional permission.
    4. Composes an answer from allowed chunks.
    5. Writes a full audit record.
    6. Creates a knowledge-gap record if no chunks found.
    """
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:ask")
    department, role = _get_user_dept_role(db, tenant_id, current_user.user_id)

    result = answer_question(
        db,
        tenant_id=tenant_id,
        user_id=current_user.user_id,
        question=body.question,
        session_id=body.session_id,
        department=department,
        role=role,
    )

    return AskResponse(
        question=body.question,
        answer=result["answer"],
        session_id=body.session_id or str(result["audit_id"]),
        audit_id=result["audit_id"],
        faq_hit=result["faq_hit"],
        faq_cache_hit=result.get("faq_cache_hit", False),
        allowed_chunks=[_chunk_to_response(c) for c in result["allowed_chunks"]],
        blocked_chunks=[_chunk_to_response(c) for c in result["blocked_chunks"]],
        missing_permission=result["missing_permission"],
        gap_created=result["gap_created"],
        latency_ms=result["latency_ms"],
    )


# =====================================================================
# FAQ endpoints
# =====================================================================


@router.post(
    "/cluster",
    response_model=list[CandidateResponse],
)
def trigger_clustering(
    days: int = Query(default=7, ge=1, le=90),  # noqa: B008
    min_frequency: int = Query(default=3, ge=2, le=100),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> list[CandidateResponse]:
    """Trigger FAQ clustering from recent Q&A audit records."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:faq:cluster")
    candidates = cluster_faq_candidates(
        db,
        tenant_id=tenant_id,
        days=days,
        min_frequency=min_frequency,
    )
    return [
        CandidateResponse(
            id=c.id,
            tenant_id=c.tenant_id,
            cluster_key=c.cluster_key,
            cluster_question=c.cluster_question,
            frequency=c.frequency,
            related_chunk_ids=c.related_chunk_ids,
            recommended_answer=c.recommended_answer,
            confidence=c.confidence,
            status=c.status.value,
        )
        for c in candidates
    ]


@router.get(
    "/faqs/candidates",
    response_model=list[CandidateResponse],
)
def list_faq_candidates(
    status: str | None = Query(default=None),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> list[CandidateResponse]:
    """List FAQ candidates."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:faq:publish")
    stmt = (
        select(FaqCandidate)
        .where(FaqCandidate.tenant_id == tenant_id)
        .order_by(FaqCandidate.frequency.desc())
    )
    if status:
        stmt = stmt.where(FaqCandidate.status == status)
    candidates = list(db.scalars(stmt).all())
    return [
        CandidateResponse(
            id=c.id,
            tenant_id=c.tenant_id,
            cluster_key=c.cluster_key,
            cluster_question=c.cluster_question,
            frequency=c.frequency,
            related_chunk_ids=c.related_chunk_ids,
            recommended_answer=c.recommended_answer,
            confidence=c.confidence,
            status=c.status.value,
        )
        for c in candidates
    ]


@router.delete(
    "/faqs/candidates/{candidate_id}",
    status_code=204,
)
def delete_faq_candidate_endpoint(
    candidate_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> None:
    """Delete a FAQ candidate."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:faq:delete")
    candidate = db.scalar(
        select(FaqCandidate).where(
            FaqCandidate.id == candidate_id,
            FaqCandidate.tenant_id == tenant_id,
        ),
    )
    if candidate is None:
        raise HTTPException(status_code=404, detail="FAQ candidate not found")
    delete_faq_candidate(db, candidate_id)
    db.commit()


@router.post(
    "/faqs/{candidate_id}/publish",
    response_model=PublishedFaqResponse,
)
def publish_faq_candidate(
    candidate_id: uuid.UUID,
    body: PublishCandidateRequest | None = None,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> PublishedFaqResponse:
    """Publish a FAQ candidate as a PublishedFaq."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:faq:publish")
    candidate = db.scalar(
        select(FaqCandidate).where(
            FaqCandidate.id == candidate_id,
            FaqCandidate.tenant_id == tenant_id,
        ),
    )
    if candidate is None:
        raise HTTPException(status_code=404, detail="FAQ candidate not found")

    published = publish_faq(
        db,
        candidate_id,
        answer=body.answer if body else None,
        question=body.question if body else None,
        published_by=current_user.user_id,
    )
    if published is None:
        raise HTTPException(status_code=500, detail="Failed to publish FAQ")
    return PublishedFaqResponse(
        id=published.id,
        tenant_id=published.tenant_id,
        cluster_key=published.cluster_key,
        question=published.question,
        answer=published.answer,
        related_chunk_ids=published.related_chunk_ids,
        confidence=published.confidence,
    )


@router.post(
    "/faqs/{candidate_id}/reject",
    response_model=CandidateResponse,
)
def reject_faq_candidate(
    candidate_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> CandidateResponse:
    """Reject a FAQ candidate."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:faq:reject")
    candidate = db.scalar(
        select(FaqCandidate).where(
            FaqCandidate.id == candidate_id,
            FaqCandidate.tenant_id == tenant_id,
        ),
    )
    if candidate is None:
        raise HTTPException(status_code=404, detail="FAQ candidate not found")

    updated = reject_faq(db, candidate_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="FAQ candidate not found")
    return CandidateResponse(
        id=updated.id,
        tenant_id=updated.tenant_id,
        cluster_key=updated.cluster_key,
        cluster_question=updated.cluster_question,
        frequency=updated.frequency,
        related_chunk_ids=updated.related_chunk_ids,
        recommended_answer=updated.recommended_answer,
        confidence=updated.confidence,
        status=updated.status.value,
    )


@router.get(
    "/faqs/published",
    response_model=list[PublishedFaqResponse],
)
def list_published_faqs(
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> list[PublishedFaqResponse]:
    """List all published FAQs."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:faq:publish")
    faqs = list(
        db.scalars(
            select(PublishedFaq)
            .where(PublishedFaq.tenant_id == tenant_id)
            .order_by(PublishedFaq.created_at.desc()),
        ).all(),
    )
    return [
        PublishedFaqResponse(
            id=f.id,
            tenant_id=f.tenant_id,
            cluster_key=f.cluster_key,
            question=f.question,
            answer=f.answer,
            related_chunk_ids=f.related_chunk_ids,
            confidence=f.confidence,
        )
        for f in faqs
    ]


@router.delete(
    "/faqs/published/{faq_id}",
    status_code=204,
)
def delete_published_faq_endpoint(
    faq_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> None:
    """Delete a published FAQ."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:faq:delete")
    faq = db.scalar(
        select(PublishedFaq).where(
            PublishedFaq.id == faq_id,
            PublishedFaq.tenant_id == tenant_id,
        ),
    )
    if faq is None:
        raise HTTPException(status_code=404, detail="Published FAQ not found")
    delete_published_faq(db, faq_id)
    db.commit()


# =====================================================================
# Knowledge-gap endpoints
# =====================================================================


@router.get(
    "/gaps",
    response_model=list[GapResponse],
)
def list_knowledge_gaps(
    status: str | None = Query(default=None),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> list[GapResponse]:
    """List knowledge gaps."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:gap:resolve")
    gap_status = None
    if status:
        try:
            gap_status = KnowledgeGapStatus(status)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status: {status}",
            ) from exc

    gaps = list_gaps(db, tenant_id=tenant_id, status=gap_status)
    return [
        GapResponse(
            id=g.id,
            tenant_id=g.tenant_id,
            question=g.question,
            department=g.department,
            frequency=g.frequency,
            highest_similarity=g.highest_similarity,
            suggested_category=g.suggested_category,
            status=g.status.value,
            created_at=g.created_at,
            updated_at=g.updated_at,
        )
        for g in gaps
    ]


@router.delete(
    "/gaps/{gap_id}",
    status_code=204,
)
def delete_knowledge_gap(
    gap_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> None:
    """Delete a knowledge-gap record."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:gap:delete")
    gap = db.scalar(
        select(KnowledgeGap).where(
            KnowledgeGap.id == gap_id,
            KnowledgeGap.tenant_id == tenant_id,
        ),
    )
    if gap is None:
        raise HTTPException(status_code=404, detail="Knowledge gap not found")
    delete_gap(db, gap_id)
    db.commit()


@router.post(
    "/gaps/{gap_id}/resolve",
    response_model=GapResponse,
)
def resolve_knowledge_gap(
    gap_id: uuid.UUID,
    body: ResolveGapRequest | None = None,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> GapResponse:
    """Resolve a knowledge gap (e.g. after importing a new document)."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:gap:resolve")
    gap = db.scalar(
        select(KnowledgeGap).where(
            KnowledgeGap.id == gap_id,
            KnowledgeGap.tenant_id == tenant_id,
        ),
    )
    if gap is None:
        raise HTTPException(status_code=404, detail="Knowledge gap not found")

    resolved = resolve_gap(db, gap_id, resolved_by=current_user.user_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Knowledge gap not found")
    return GapResponse(
        id=resolved.id,
        tenant_id=resolved.tenant_id,
        question=resolved.question,
        department=resolved.department,
        frequency=resolved.frequency,
        highest_similarity=resolved.highest_similarity,
        suggested_category=resolved.suggested_category,
        status=resolved.status.value,
        created_at=resolved.created_at,
        updated_at=resolved.updated_at,
    )


# =====================================================================
# Audit endpoints
# =====================================================================


@router.get(
    "/audits",
    response_model=list[AuditResponse],
)
def list_qa_audits(
    user_id: uuid.UUID | None = Query(default=None),  # noqa: B008
    session_id: str | None = Query(default=None),  # noqa: B008
    limit: int = Query(default=50, ge=1, le=500),  # noqa: B008
    offset: int = Query(default=0, ge=0),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> list[AuditResponse]:
    """List Q&A audit records."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:audit:read")
    audits = list_audits(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        limit=limit,
        offset=offset,
    )
    return [
        AuditResponse(
            id=a.id,
            tenant_id=a.tenant_id,
            user_id=a.user_id,
            session_id=a.session_id,
            question=a.question,
            answer=a.answer,
            recalled_chunk_ids=a.recalled_chunk_ids,
            allowed_chunk_ids=a.allowed_chunk_ids,
            blocked_chunk_ids=a.blocked_chunk_ids,
            faq_hit=a.faq_hit,
            token_estimate=a.token_estimate,
            latency_ms=a.latency_ms,
            created_at=a.created_at,
        )
        for a in audits
    ]


@router.delete(
    "/audits/{audit_id}",
    status_code=204,
)
def delete_qa_audit(
    audit_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> None:
    """Delete a single Q&A audit record."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:audit:delete")
    audit = db.scalar(
        select(KnowledgeQaAudit).where(
            KnowledgeQaAudit.id == audit_id,
            KnowledgeQaAudit.tenant_id == tenant_id,
        ),
    )
    if audit is None:
        raise HTTPException(status_code=404, detail="Audit record not found")
    delete_audit(db, audit_id)
    db.commit()


# =====================================================================
# Operational metrics endpoint
# =====================================================================


@router.get(
    "/metrics",
    response_model=OperationMetricsResponse,
)
def get_metrics(
    days: int = Query(default=7, ge=1, le=90),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> OperationMetricsResponse:
    """Get operational metrics for the knowledge platform."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:metrics:read")
    metrics = get_operation_metrics(db, tenant_id=tenant_id, days=days)
    return OperationMetricsResponse(**metrics)


# =====================================================================
# Department tree and role-permission endpoints
# =====================================================================


def _department_tree(departments: list[Department]) -> list[DepartmentResponse]:
    by_id = {
        department.id: DepartmentResponse(
            id=department.id,
            tenant_id=department.tenant_id,
            parent_id=department.parent_id,
            name=department.name,
            code=department.code,
            sort_order=department.sort_order,
            children=[],
        )
        for department in departments
    }
    roots: list[DepartmentResponse] = []
    for department in departments:
        node = by_id[department.id]
        if department.parent_id and department.parent_id in by_id:
            by_id[department.parent_id].children.append(node)
        else:
            roots.append(node)
    return roots


@router.get(
    "/departments/tree",
    response_model=list[DepartmentResponse],
)
def get_department_tree(
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> list[DepartmentResponse]:
    """Return the tenant department tree."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "dept:manage")
    return _department_tree(list_departments(db, tenant_id))


@router.post(
    "/departments",
    response_model=DepartmentResponse,
    status_code=201,
)
def create_department_endpoint(
    body: DepartmentCreateRequest,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> DepartmentResponse:
    """Create a department node."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "dept:manage")
    try:
        department = create_department(
            db,
            tenant_id=tenant_id,
            name=body.name,
            code=body.code,
            parent_id=body.parent_id,
            sort_order=body.sort_order,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return DepartmentResponse(
        id=department.id,
        tenant_id=department.tenant_id,
        parent_id=department.parent_id,
        name=department.name,
        code=department.code,
        sort_order=department.sort_order,
    )


@router.patch(
    "/departments/{department_id}",
    response_model=DepartmentResponse,
)
def update_department_endpoint(
    department_id: uuid.UUID,
    body: DepartmentUpdateRequest,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> DepartmentResponse:
    """Update a department node."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "dept:manage")
    try:
        department = update_department(
            db,
            department_id=department_id,
            tenant_id=tenant_id,
            name=body.name,
            code=body.code,
            parent_id=body.parent_id,
            sort_order=body.sort_order,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if department is None:
        raise HTTPException(status_code=404, detail="Department not found")
    db.commit()
    return DepartmentResponse(
        id=department.id,
        tenant_id=department.tenant_id,
        parent_id=department.parent_id,
        name=department.name,
        code=department.code,
        sort_order=department.sort_order,
    )


@router.delete(
    "/departments/{department_id}",
    status_code=204,
)
def delete_department_endpoint(
    department_id: uuid.UUID,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> None:
    """Delete a department and its children."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "dept:manage")
    if not delete_department(
        db,
        department_id=department_id,
        tenant_id=tenant_id,
    ):
        raise HTTPException(status_code=404, detail="Department not found")
    db.commit()


@router.get(
    "/role-permissions",
    response_model=list[RolePermissionResponse],
)
def get_role_permissions(
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> list[RolePermissionResponse]:
    """Return button-level permissions by role."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "perm:manage")
    return [
        RolePermissionResponse(role_name=role, permissions=permissions)
        for role, permissions in list_role_permissions(db, tenant_id).items()
    ]


@router.put(
    "/role-permissions",
    response_model=list[RolePermissionResponse],
)
def put_role_permissions(
    body: RolePermissionRequest,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> list[RolePermissionResponse]:
    """Replace button-level permissions for one role."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "perm:manage")
    result = set_role_permissions(
        db,
        tenant_id=tenant_id,
        role_name=body.role_name,
        permissions=body.permissions,
    )
    db.commit()
    return [
        RolePermissionResponse(role_name=role, permissions=permissions)
        for role, permissions in result.items()
    ]


# =====================================================================
# Knowledge member (role assignment) endpoints
# =====================================================================


@router.post(
    "/members",
    response_model=KnowledgeMemberResponse,
    status_code=201,
)
def assign_member(
    body: KnowledgeMemberRequest,
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> KnowledgeMemberResponse:
    """Assign or update a knowledge role for a user."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:member:assign")
    member = assign_knowledge_member(
        db,
        tenant_id=tenant_id,
        user_id=body.user_id,
        role=body.role,
        department=body.department,
    )
    user = db.scalar(select(User).where(User.id == body.user_id))
    db.commit()
    return KnowledgeMemberResponse(
        id=member.id,
        tenant_id=member.tenant_id,
        user_id=member.user_id,
        username=user.username if user else None,
        role=member.role.value,
        department=member.department,
    )


@router.get(
    "/members",
    response_model=list[KnowledgeMemberResponse],
)
def list_members(
    db: Session = Depends(get_db),  # noqa: B008
    current_user: CurrentUser = Depends(get_current_user),  # noqa: B008
) -> list[KnowledgeMemberResponse]:
    """List all knowledge members in the current tenant."""
    tenant_id = _get_tenant_id(current_user)
    _require_permission(db, tenant_id, current_user, "kb:member:read")
    members = list(
        db.scalars(
            select(KnowledgeMember).where(
                KnowledgeMember.tenant_id == tenant_id,
            ),
        ).all(),
    )
    user_ids = [member.user_id for member in members]
    username_by_id = {
        user.id: user.username
        for user in db.scalars(select(User).where(User.id.in_(user_ids))).all()
    } if user_ids else {}
    return [
        KnowledgeMemberResponse(
            id=m.id,
            tenant_id=m.tenant_id,
            user_id=m.user_id,
            username=username_by_id.get(m.user_id),
            role=m.role.value,
            department=m.department,
        )
        for m in members
    ]
