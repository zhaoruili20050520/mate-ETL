"""Knowledge platform service layer.

Implements the full business logic for the knowledge-management
platform:

1. **Document & chunk management** -- import documents, split into
   chunks, edit/delete chunks, publish/archive documents.

2. **Four-dimensional permission check** -- given a user's identity
   (department, role, user_id) and a chunk's permission rules
   (visibility / departments / roles / users), determine whether
   the chunk is visible.

3. **Q&A with RAG retrieval** -- given a question, retrieve relevant
   chunks by simple keyword overlap (no embedding model dependency),
   filter by four-dimensional permission, compose an answer, and
   write a full audit record.

4. **FAQ clustering** -- aggregate repeated questions from audit
   records into candidate FAQs.

5. **Knowledge-gap diagnosis** -- detect questions that hit zero
   or low-similarity chunks and create gap records.

6. **Operational metrics** -- aggregate daily/weekly totals, FAQ
   hit rate, average latency, blocked-access count.

Security contract:
    - Every Q&A call writes a ``KnowledgeQaAudit`` row recording
      recalled / allowed / blocked chunk IDs.
    - Permission filtering happens *after* retrieval and *before*
      answer composition, so blocked chunks never appear in the
      answer.
    - No credentials or sensitive chunk content appear in logs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from openai import OpenAI
from redis import Redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from etl_agent.models.department import Department
from etl_agent.models.knowledge import (
    FaqCandidate,
    FaqStatus,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentStatus,
    KnowledgeGap,
    KnowledgeGapStatus,
    KnowledgeMember,
    KnowledgeQaAudit,
    KnowledgeRole,
    PublishedFaq,
)
from etl_agent.models.permission import (
    DEFAULT_ROLE_PERMISSIONS,
    PERMISSION_CODES,
    RolePermission,
)
from etl_agent.settings import settings

logger = logging.getLogger(__name__)

# =====================================================================
# 1. Document & chunk management
# =====================================================================

_DEFAULT_CHUNK_SIZE = 800
_DEFAULT_CHUNK_OVERLAP = 100
_FAQ_CACHE_PREFIX = "etl-agent:knowledge:faq"

_SMALL_TALK_RESPONSES: dict[tuple[str, ...], str] = {
    (
        "你好",
        "您好",
        "hello",
        "hi",
        "hey",
        "嗨",
        "哈喽",
        "在吗",
        "早上好",
        "下午好",
        "晚上好",
    ): "你好，很高兴见到你。你可以问我公司制度、业务流程或数据平台相关的问题。",
    (
        "谢谢",
        "感谢",
        "thank you",
        "thanks",
    ): "不客气，有问题随时再来问我。",
    (
        "再见",
        "拜拜",
        "bye",
    ): "再见，有需要时随时回来找我。",
}


def _detect_small_talk(question: str) -> str | None:
    """Return a friendly answer for simple small-talk messages."""
    normalized = re.sub(r"[\s!?。？！，,\.]+", "", question.lower().strip())
    if not normalized:
        return None
    for phrases, answer in _SMALL_TALK_RESPONSES.items():
        if normalized in phrases:
            return answer
    return None


def _faq_cache_key(tenant_id: uuid.UUID, question: str) -> str:
    normalized = re.sub(r"\s+", " ", question.lower().strip())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{_FAQ_CACHE_PREFIX}:{tenant_id}:{digest}"


def _redis_client() -> Redis:
    return Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=1,
        socket_timeout=1,
        decode_responses=True,
    )


def _read_faq_cache(
    tenant_id: uuid.UUID,
    question: str,
) -> dict[str, Any] | None:
    """Read a published FAQ from Redis, returning None on any cache failure."""
    client: Redis | None = None
    try:
        client = _redis_client()
        payload = client.get(_faq_cache_key(tenant_id, question))
        return json.loads(payload) if payload else None
    except Exception as exc:
        logger.debug("FAQ cache read skipped: %s", str(exc)[:160])
        return None
    finally:
        if client is not None:
            client.close()


def _write_faq_cache(faq: PublishedFaq) -> None:
    """Cache a published FAQ for fast exact-question answers."""
    client: Redis | None = None
    try:
        client = _redis_client()
        payload = json.dumps(
            {
                "question": faq.question,
                "answer": faq.answer,
                "related_chunk_ids": [str(cid) for cid in faq.related_chunk_ids],
            },
            ensure_ascii=False,
        )
        client.set(
            _faq_cache_key(faq.tenant_id, faq.question),
            payload,
            ex=86400,
        )
    except Exception as exc:
        logger.debug("FAQ cache write skipped: %s", str(exc)[:160])
    finally:
        if client is not None:
            client.close()


def _invalidate_faq_cache(tenant_id: uuid.UUID, question: str) -> None:
    """Delete one published FAQ cache entry."""
    client: Redis | None = None
    try:
        client = _redis_client()
        client.delete(_faq_cache_key(tenant_id, question))
    except Exception as exc:
        logger.debug("FAQ cache invalidation skipped: %s", str(exc)[:160])
    finally:
        if client is not None:
            client.close()


def import_document(
    db: Session,
    tenant_id: uuid.UUID,
    title: str,
    content: str,
    category: str | None = None,
    permissions: dict[str, Any] | None = None,
    created_by: uuid.UUID | None = None,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = _DEFAULT_CHUNK_OVERLAP,
) -> KnowledgeDocument:
    """Import a document, auto-chunk it, and return the ORM object.

    The document is created with status ``draft``.  Chunks inherit
    the document's permission rules.
    """
    perms = permissions or {
        "visibility": "public",
        "departments": [],
        "roles": [],
        "users": [],
    }

    doc = KnowledgeDocument(
        tenant_id=tenant_id,
        title=title,
        category=category,
        content=content,
        status=KnowledgeDocumentStatus.draft,
        permissions=perms,
        created_by=created_by,
    )
    db.add(doc)
    db.flush()

    chunks = _split_text(content, chunk_size, chunk_overlap)
    for idx, (chunk_title, chunk_text) in enumerate(chunks):
        chunk = KnowledgeChunk(
            document_id=doc.id,
            tenant_id=tenant_id,
            title=f"{title} [chunk {idx + 1}]" if chunk_title == "" else chunk_title,
            content=chunk_text,
            chunk_index=idx,
            permissions=perms,
        )
        db.add(chunk)

    db.flush()
    logger.info(
        "Imported document %s with %d chunks (tenant=%s)",
        doc.id,
        len(chunks),
        tenant_id,
    )
    return doc


def seed_knowledge_demo(
    db: Session,
    tenant_id: uuid.UUID,
    created_by: uuid.UUID | None = None,
) -> dict[str, int]:
    """Seed a small runnable demo matching the sample business scenarios."""
    demo_docs: list[dict[str, Any]] = [
        {
            "title": "差旅报销标准",
            "category": "财务制度",
            "content": (
                "差旅报销标准：单次住宿上限500元，市内交通费用凭票报销，"
                "餐饮补贴每日120元。员工出差前需完成审批，报销单需附行程与票据。"
            ),
            "permissions": {
                "visibility": "public",
                "departments": [],
                "roles": [],
                "users": [],
            },
        },
        {
            "title": "高管薪酬与股权激励细则",
            "category": "人事制度",
            "content": (
                "高管薪酬与股权激励细则：高管年度绩效奖金由董事会薪酬委员会核定，"
                "股权授予计划仅限人力资源部与管理层查阅，普通部门无权访问。"
            ),
            "permissions": {
                "visibility": "restricted",
                "departments": ["人力资源部"],
                "roles": ["system_admin"],
                "users": [],
            },
        },
        {
            "title": "生鲜食品破损退款流程",
            "category": "客服FAQ",
            "content": (
                "生鲜食品破损退款流程：收货后48小时内拍摄外包装、破损商品和面单照片，"
                "在订单页申请退款并选择“商品破损”，系统核对后1-3个工作日原路退回。"
            ),
            "permissions": {
                "visibility": "public",
                "departments": [],
                "roles": [],
                "users": [],
            },
        },
        {
            "title": "ETL Agent 是什么",
            "category": "产品说明",
            "content": (
                "ETL-Agent 是智能 ETL 数据管道生成与治理平台。用户用自然语言描述数据同步需求，"
                "系统自动识别源表和目标表、生成 SeaTunnel HOCON 配置、执行质量校验、"
                "审批后提交 SeaTunnel，最后通过影子表原子发布到正式表。"
            ),
            "permissions": {
                "visibility": "public",
                "departments": [],
                "roles": [],
                "users": [],
            },
        },
        {
            "title": "质量校验与原子发布流程",
            "category": "运维规范",
            "content": (
                "ETL 作业完成后进入质量校验：检查错误率、影子表行数、主键重复和空值。"
                "校验通过后执行原子交换，把影子表替换为正式表，原正式表保留为备份；"
                "任何阶段失败都会自动回滚并保留错误表用于排查。"
            ),
            "permissions": {
                "visibility": "public",
                "departments": [],
                "roles": [],
                "users": [],
            },
        },
        {
            "title": "MySQL 到 Doris 数据同步指南",
            "category": "数据平台",
            "content": (
                "MySQL 同步到 Doris 时，需要先登记源和目标连接并测试连通性。"
                "全量同步建议使用影子表与原子替换；增量同步需要配置主键和 watermark 字段。"
                "SeaTunnel 作业写入目标后，平台会执行质量门禁并通过后发布。"
            ),
            "permissions": {
                "visibility": "public",
                "departments": [],
                "roles": [],
                "users": [],
            },
        },
        {
            "title": "数据安全分级与敏感字段脱敏",
            "category": "数据安全",
            "content": (
                "数据按敏感度分为公开、内部、敏感和机密四级。手机号、身份证、邮箱等敏感字段"
                "在 ETL 同步前必须使用掩码或哈希脱敏，不能明文映射到目标表。"
                "高敏数据访问需要审批记录，并在证据账本中留痕。"
            ),
            "permissions": {
                "visibility": "tenant",
                "departments": [],
                "roles": [],
                "users": [],
            },
        },
        {
            "title": "员工请休假制度",
            "category": "人事制度",
            "content": (
                "员工年假按入职年限核算，法定节假日不占用年假。事假需提前一天申请，"
                "病假超过三天需提供医院证明。请假审批通过后方可休假，并同步到考勤系统。"
            ),
            "permissions": {
                "visibility": "tenant",
                "departments": [],
                "roles": [],
                "users": [],
            },
        },
    ]

    created = 0
    for item in demo_docs:
        existing = db.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.title == item["title"],
            ),
        )
        if existing is not None:
            if existing.status != KnowledgeDocumentStatus.published:
                existing.status = KnowledgeDocumentStatus.published
                db.flush()
            continue
        import_document(
            db,
            tenant_id=tenant_id,
            title=item["title"],
            category=item["category"],
            content=item["content"],
            permissions=item["permissions"],
            created_by=created_by,
        )
        doc = db.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.title == item["title"],
            ),
        )
        if doc is not None:
            doc.status = KnowledgeDocumentStatus.published
            created += 1

    db.commit()
    return {"created": created, "total": len(demo_docs)}


def _split_text(
    text: str,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    overlap: int = _DEFAULT_CHUNK_OVERLAP,
) -> list[tuple[str, str]]:
    """Split text into overlapping chunks.

    Returns a list of ``(title, content)`` tuples.  The title is
    the first non-empty line of the chunk (truncated to 255 chars),
    or an empty string if the chunk has no lines.
    """
    text = text.strip()
    if not text:
        return [("", "")]

    chunks: list[tuple[str, str]] = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        segment = text[start:end]

        # Try to break at a paragraph or sentence boundary
        if end < len(text):
            for sep in ("\n\n", "\n", "。", ". ", "! ", "? "):
                pos = segment.rfind(sep)
                if pos > chunk_size * 0.3:
                    end = start + pos + len(sep)
                    segment = text[start:end]
                    break

        # Derive a title from the first line
        lines = segment.lstrip().split("\n", 1)
        title = lines[0][:255] if lines and lines[0].strip() else ""

        chunks.append((title, segment.strip()))
        start = end - overlap if end - overlap > start else end

    return chunks


def update_chunk(
    db: Session,
    chunk_id: uuid.UUID,
    title: str | None = None,
    content: str | None = None,
    permissions: dict[str, Any] | None = None,
) -> KnowledgeChunk | None:
    """Update a chunk's title, content, or permissions."""
    chunk = db.scalar(select(KnowledgeChunk).where(KnowledgeChunk.id == chunk_id))
    if chunk is None:
        return None
    if title is not None:
        chunk.title = title[:255]
    if content is not None:
        chunk.content = content
    if permissions is not None:
        chunk.permissions = permissions
    db.flush()
    return chunk


def delete_chunk(db: Session, chunk_id: uuid.UUID) -> bool:
    """Delete a chunk. Returns True if deleted."""
    chunk = db.scalar(select(KnowledgeChunk).where(KnowledgeChunk.id == chunk_id))
    if chunk is None:
        return False
    db.delete(chunk)
    db.flush()
    return True


def publish_document(db: Session, doc_id: uuid.UUID) -> KnowledgeDocument | None:
    """Set document status to published."""
    doc = db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id))
    if doc is None:
        return None
    doc.status = KnowledgeDocumentStatus.published
    db.flush()
    return doc


def archive_document(db: Session, doc_id: uuid.UUID) -> KnowledgeDocument | None:
    """Set document status to archived."""
    doc = db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id))
    if doc is None:
        return None
    doc.status = KnowledgeDocumentStatus.archived
    db.flush()
    return doc


def delete_document(db: Session, doc_id: uuid.UUID) -> bool:
    """Delete a document and all of its chunks."""
    doc = db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id))
    if doc is None:
        return False
    db.delete(doc)
    db.flush()
    return True


def get_document_chunks(
    db: Session,
    doc_id: uuid.UUID,
) -> list[KnowledgeChunk]:
    """Get all chunks for a document, ordered by chunk_index."""
    return list(
        db.scalars(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.document_id == doc_id)
            .order_by(KnowledgeChunk.chunk_index),
        ).all(),
    )


# =====================================================================
# 2. Four-dimensional permission check
# =====================================================================


def check_permission(
    permissions: dict[str, Any],
    user_id: uuid.UUID,
    department: str | None,
    role: str | None,
) -> bool:
    """Check whether a user can access a chunk/document.

    Permission rules (JSONB with four dimensions):
    - visibility: "public" | "tenant" | "restricted"
    - departments: list of department names (empty = all)
    - roles: list of role names (empty = all)
    - users: list of user UUIDs (empty = all)

    Evaluation order:
    1. If visibility == "public", allow.
    2. If user is in the users list, allow.
    3. If departments is empty and roles is empty, allow (tenant-level).
    4. If user's department is in departments, allow.
    5. If user's role is in roles, allow.
    6. Otherwise deny.
    """
    visibility = permissions.get("visibility", "public")
    if visibility == "public":
        return True

    # Tenant and system administrators retain break-glass visibility.
    if role in {"tenant_admin", "system_admin"}:
        return True

    # Explicit user allow-list
    allowed_users = permissions.get("users", [])
    if allowed_users:
        allowed_uuids = []
        for u in allowed_users:
            try:
                allowed_uuids.append(uuid.UUID(str(u)))
            except (ValueError, TypeError):
                continue
        if user_id in allowed_uuids:
            return True

    departments = permissions.get("departments", [])
    roles = permissions.get("roles", [])

    # Empty departments + empty roles = tenant-wide access
    if not departments and not roles:
        return True

    if department and department in departments:
        return True

    return bool(role and role in roles)


def filter_chunks_by_permission(
    chunks: list[KnowledgeChunk],
    user_id: uuid.UUID,
    department: str | None,
    role: str | None,
) -> tuple[list[KnowledgeChunk], list[KnowledgeChunk]]:
    """Split chunks into (allowed, blocked) based on permission rules.

    Returns a tuple of two lists.
    """
    allowed: list[KnowledgeChunk] = []
    blocked: list[KnowledgeChunk] = []
    for chunk in chunks:
        if check_permission(chunk.permissions, user_id, department, role):
            allowed.append(chunk)
        else:
            blocked.append(chunk)
    return allowed, blocked


# =====================================================================
# 3. Knowledge member management
# =====================================================================


def assign_knowledge_member(
    db: Session,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    role: KnowledgeRole,
    department: str | None = None,
) -> KnowledgeMember:
    """Assign or update a knowledge role for a user in a tenant."""
    existing = db.scalar(
        select(KnowledgeMember).where(
            KnowledgeMember.tenant_id == tenant_id,
            KnowledgeMember.user_id == user_id,
        ),
    )
    if existing is not None:
        existing.role = role
        existing.department = department
        db.flush()
        return existing

    member = KnowledgeMember(
        tenant_id=tenant_id,
        user_id=user_id,
        role=role,
        department=department,
    )
    db.add(member)
    db.flush()
    return member


def get_knowledge_member(
    db: Session,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> KnowledgeMember | None:
    """Get the knowledge member record for a user in a tenant."""
    return db.scalar(
        select(KnowledgeMember).where(
            KnowledgeMember.tenant_id == tenant_id,
            KnowledgeMember.user_id == user_id,
        ),
    )


def list_departments(
    db: Session,
    tenant_id: uuid.UUID,
) -> list[Department]:
    """List all departments in stable tree order."""
    return list(
        db.scalars(
            select(Department)
            .where(Department.tenant_id == tenant_id)
            .order_by(Department.sort_order, Department.name),
        ).all(),
    )


def create_department(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    name: str,
    code: str,
    parent_id: uuid.UUID | None = None,
    sort_order: int = 0,
) -> Department:
    """Create a department tree node."""
    if parent_id is not None:
        parent = db.scalar(
            select(Department).where(
                Department.id == parent_id,
                Department.tenant_id == tenant_id,
            ),
        )
        if parent is None:
            raise ValueError("Parent department not found")
    department = Department(
        tenant_id=tenant_id,
        parent_id=parent_id,
        name=name,
        code=code,
        sort_order=sort_order,
    )
    db.add(department)
    db.flush()
    return department


def update_department(
    db: Session,
    *,
    department_id: uuid.UUID,
    tenant_id: uuid.UUID,
    name: str | None = None,
    code: str | None = None,
    parent_id: uuid.UUID | None = None,
    sort_order: int | None = None,
) -> Department | None:
    """Update a department node."""
    department = db.scalar(
        select(Department).where(
            Department.id == department_id,
            Department.tenant_id == tenant_id,
        ),
    )
    if department is None:
        return None
    if parent_id == department.id:
        raise ValueError("A department cannot be its own parent")
    if name is not None:
        department.name = name
    if code is not None:
        department.code = code
    if parent_id is not None:
        department.parent_id = parent_id
    if sort_order is not None:
        department.sort_order = sort_order
    db.flush()
    return department


def delete_department(
    db: Session,
    *,
    department_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> bool:
    """Delete a department and its children."""
    department = db.scalar(
        select(Department).where(
            Department.id == department_id,
            Department.tenant_id == tenant_id,
        ),
    )
    if department is None:
        return False
    db.delete(department)
    db.flush()
    return True


def list_role_permissions(
    db: Session,
    tenant_id: uuid.UUID,
) -> dict[str, list[str]]:
    """Return effective role -> permission-code mappings.

    Built-in defaults are returned for roles without tenant overrides.
    Once a tenant saves an explicit permission set for a role, that set
    replaces the built-in default for that role.
    """
    rows = list(
        db.scalars(
            select(RolePermission)
            .where(RolePermission.tenant_id == tenant_id)
            .order_by(RolePermission.role_name, RolePermission.permission_code),
        ).all(),
    )
    overridden_roles = {row.role_name for row in rows}
    result: dict[str, list[str]] = {
        role_name: sorted(permissions)
        for role_name, permissions in DEFAULT_ROLE_PERMISSIONS.items()
        if role_name not in overridden_roles
    }
    for row in rows:
        result.setdefault(row.role_name, []).append(row.permission_code)
    # The tenant administrator is the break-glass account and must never
    # lose navigation or recovery permissions through a role override.
    result["tenant_admin"] = sorted(PERMISSION_CODES)
    return {
        role_name: sorted(dict.fromkeys(permissions))
        for role_name, permissions in result.items()
    }


def set_role_permissions(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    role_name: str,
    permissions: list[str],
) -> dict[str, list[str]]:
    """Replace all button permissions for one role."""
    if role_name == "tenant_admin":
        permissions = list(PERMISSION_CODES)
    existing = list(
        db.scalars(
            select(RolePermission).where(
                RolePermission.tenant_id == tenant_id,
                RolePermission.role_name == role_name,
            ),
        ).all(),
    )
    for row in existing:
        db.delete(row)
    db.flush()
    for permission_code in dict.fromkeys(permissions):
        db.add(
            RolePermission(
                tenant_id=tenant_id,
                role_name=role_name,
                permission_code=permission_code,
            ),
        )
    db.flush()
    return list_role_permissions(db, tenant_id)


# =====================================================================
# 4. Q&A with RAG retrieval + audit
# =====================================================================


def _tokenize(text: str) -> set[str]:
    """Simple tokenizer: lowercase, split on non-alphanumeric (CJK-aware)."""
    text = text.lower().strip()
    # Split CJK characters individually, ASCII words by whitespace/punctuation
    tokens: set[str] = set()
    current_word: list[str] = []
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            if current_word:
                tokens.add("".join(current_word))
                current_word = []
            tokens.add(ch)
        elif ch.isalnum():
            current_word.append(ch)
        else:
            if current_word:
                tokens.add("".join(current_word))
                current_word = []
    if current_word:
        tokens.add("".join(current_word))
    # Filter very short tokens
    # Keep single CJK characters (they carry meaning in Chinese), but
    # drop single ASCII characters which are usually noise.
    return {
        t
        for t in tokens
        if len(t) >= 2
        or any("\u4e00" <= ch <= "\u9fff" for ch in t)
    }


def _score_chunk(chunk: KnowledgeChunk, question_tokens: set[str]) -> float:
    """Score a chunk by keyword overlap with the question.

    Uses a simple Jaccard-like ratio: |question_tokens ∩ chunk_tokens|
    / |question_tokens|.  This is a lightweight retrieval method
    suitable for testing; a production system would use embeddings.
    """
    if not question_tokens:
        return 0.0
    chunk_tokens = _tokenize(chunk.content + " " + chunk.title)
    overlap = question_tokens & chunk_tokens
    return len(overlap) / len(question_tokens)


def _generate_local_answer(question: str, chunks: list[KnowledgeChunk]) -> str:
    """Ask the local Ollama model to answer from the allowed context."""
    if not chunks:
        return ""
    context_parts = []
    for idx, chunk in enumerate(chunks[:5], start=1):
        context_parts.append(
            f"[{idx}] 标题：{chunk.title}\n内容：{chunk.content[:1200]}"
        )
    context_text = "\n\n".join(context_parts)

    client = OpenAI(
        api_key=settings.knowledge_llm_api_key,
        base_url=settings.knowledge_llm_base_url,
        timeout=90,
        max_retries=0,
    )
    response = client.chat.completions.create(
        model=settings.knowledge_llm_model,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是企业内部知识库问答助手。请只依据下方提供的知识库资料回答问题，"
                    "不要编造资料中不存在的内容。回答要简洁、清晰，使用中文；"
                    "如果资料不足以回答，请说明资料中缺少哪些信息。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"用户问题：{question}\n\n"
                    f"知识库资料：\n{context_text}"
                ),
            },
        ],
        temperature=0.2,
        max_tokens=800,
    )
    content = response.choices[0].message.content if response.choices else ""
    return content.strip() if content else ""


def answer_question(
    db: Session,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    question: str,
    session_id: str = "",
    department: str | None = None,
    role: str | None = None,
    top_k: int = 5,
    min_similarity: float = 0.25,
) -> dict[str, Any]:
    """Answer a question using RAG retrieval + permission filtering.

    Steps:
        1. Check published FAQs for a direct hit.
        2. If no FAQ hit, retrieve top-k chunks by keyword overlap.
        3. Filter chunks by four-dimensional permission.
        4. Compose answer from allowed chunks.
        5. Write ``KnowledgeQaAudit`` record.
        6. If no chunks retrieved or all blocked, create/update a
           ``KnowledgeGap`` record.

    Returns a dict with:
        - answer: str
        - faq_hit: bool
        - allowed_chunks: list[KnowledgeChunk]
        - blocked_chunks: list[KnowledgeChunk]
        - audit_id: uuid.UUID
        - gap_created: bool
        - latency_ms: int
    """
    from datetime import datetime as _dt

    start_time = _dt.now(UTC)

    small_talk_answer = _detect_small_talk(question)
    if small_talk_answer:
        latency_ms = int((_dt.now(UTC) - start_time).total_seconds() * 1000)
        audit = KnowledgeQaAudit(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id or str(uuid.uuid4()),
            question=question,
            answer=small_talk_answer,
            recalled_chunk_ids=[],
            allowed_chunk_ids=[],
            blocked_chunk_ids=[],
            faq_hit=False,
            token_estimate=len(question) // 4 + len(small_talk_answer) // 4,
            latency_ms=latency_ms,
        )
        db.add(audit)
        db.commit()
        return {
            "answer": small_talk_answer,
            "faq_hit": False,
            "allowed_chunks": [],
            "blocked_chunks": [],
            "audit_id": audit.id,
            "gap_created": False,
            "latency_ms": latency_ms,
            "missing_permission": False,
        }

    # ---- 1. Check published FAQs (Redis first, then PostgreSQL) ----
    faq_hit = False
    faq_cache_hit = False
    faq_permission_blocked = False
    answer_text = ""
    faq_chunk_ids: list[uuid.UUID] = []
    faq_allowed_chunks: list[KnowledgeChunk] = []
    faq_blocked_chunks: list[KnowledgeChunk] = []

    cached_faq = _read_faq_cache(tenant_id, question)
    if cached_faq is not None:
        faq_hit = True
        faq_cache_hit = True
        answer_text = str(cached_faq.get("answer") or "")
        faq_chunk_ids = [
            uuid.UUID(str(cid))
            for cid in cached_faq.get("related_chunk_ids", [])
        ]
    else:
        published_faqs = list(
            db.scalars(
                select(PublishedFaq).where(PublishedFaq.tenant_id == tenant_id),
            ).all(),
        )
        question_lower = question.lower().strip()
        for faq in published_faqs:
            if faq.question.lower().strip() == question_lower:
                faq_hit = True
                answer_text = faq.answer
                faq_chunk_ids = [
                    uuid.UUID(str(cid)) for cid in faq.related_chunk_ids
                ]
                _write_faq_cache(faq)
                break
            # Fuzzy: check if question keywords are subset of FAQ question
            faq_tokens = _tokenize(faq.question)
            q_tokens = _tokenize(question)
            if q_tokens and faq_tokens and q_tokens.issubset(faq_tokens):
                faq_hit = True
                answer_text = faq.answer
                faq_chunk_ids = [
                    uuid.UUID(str(cid)) for cid in faq.related_chunk_ids
                ]
                _write_faq_cache(faq)
                break

    # A cached answer must never bypass the source-chunk permission check.
    if faq_hit and faq_chunk_ids:
        faq_context_chunks = list(
            db.scalars(
                select(KnowledgeChunk).where(
                    KnowledgeChunk.id.in_(faq_chunk_ids),
                    KnowledgeChunk.tenant_id == tenant_id,
                ),
            ).all(),
        )
        faq_allowed_chunks, faq_blocked_chunks = filter_chunks_by_permission(
            faq_context_chunks,
            user_id,
            department,
            role,
        )
        if faq_context_chunks and faq_blocked_chunks:
            faq_permission_blocked = True
            faq_hit = False
            faq_cache_hit = False
            answer_text = (
                "检测到相关制度文档，但您当前所属部门/角色无权查阅该内容。"
            )
        elif faq_allowed_chunks:
            try:
                llm_answer = _generate_local_answer(question, faq_allowed_chunks)
                if llm_answer:
                    answer_text = llm_answer
            except Exception as exc:
                logger.warning(
                    "Local model failed for FAQ answer, using cached FAQ: %s",
                    str(exc)[:160],
                )

    # ---- 2. Retrieve chunks ----
    recalled_chunks: list[KnowledgeChunk] = []
    allowed_chunks: list[KnowledgeChunk] = []
    blocked_chunks: list[KnowledgeChunk] = []
    scored: list[tuple[KnowledgeChunk, float]] = []

    if not faq_hit and not faq_permission_blocked:
        question_tokens = _tokenize(question)

        # Get all published documents' chunks
        all_chunks = list(
            db.scalars(
                select(KnowledgeChunk)
                .where(KnowledgeChunk.tenant_id == tenant_id)
                .join(
                    KnowledgeDocument,
                    KnowledgeChunk.document_id == KnowledgeDocument.id,
                )
                .where(KnowledgeDocument.status == KnowledgeDocumentStatus.published),
            ).all(),
        )

        # Score and rank
        scored = [(c, _score_chunk(c, question_tokens)) for c in all_chunks]
        scored.sort(key=lambda x: x[1], reverse=True)
        recalled_chunks = [
            c for c, s in scored[:top_k] if s >= min_similarity
        ]

        # ---- 3. Permission filter ----
        allowed_chunks, blocked_chunks = filter_chunks_by_permission(
            recalled_chunks,
            user_id,
            department,
            role,
        )

        # If the highest-ranked matching unit is blocked, do not answer
        # from a lower-ranked unrelated unit.  This preserves the security
        # contract while still allowing legitimately authorized users.
        if allowed_chunks and blocked_chunks:
            score_by_id = {chunk.id: score for chunk, score in scored}
            top_allowed = max(
                (score_by_id.get(chunk.id, 0.0) for chunk in allowed_chunks),
                default=0.0,
            )
            top_blocked = max(
                (score_by_id.get(chunk.id, 0.0) for chunk in blocked_chunks),
                default=0.0,
            )
            if top_blocked >= top_allowed:
                allowed_chunks = []

        # ---- 4. Compose answer ----
        if allowed_chunks:
            parts: list[str] = []
            for chunk in allowed_chunks[:3]:
                source = f"[{chunk.title}]"
                parts.append(f"{source}\n{chunk.content[:500]}")
            fallback_answer = "\n\n---\n\n".join(parts)
            try:
                llm_answer = _generate_local_answer(
                    question,
                    allowed_chunks[:5],
                )
                answer_text = llm_answer or fallback_answer
            except Exception as exc:
                logger.warning(
                    "Local knowledge model unavailable, using context fallback: %s",
                    str(exc)[:160],
                )
                answer_text = fallback_answer
        elif blocked_chunks:
            answer_text = (
                "检测到相关制度文档，但您当前所属部门/角色无权查阅该内容。"
            )
        else:
            answer_text = "未找到与您问题相关的知识内容。"

    # ---- 5. Write audit ----
    latency_ms = int((_dt.now(UTC) - start_time).total_seconds() * 1000)
    token_estimate = len(question) // 4 + len(answer_text) // 4

    # Convert UUIDs to strings for JSONB serialization
    if faq_permission_blocked:
        recalled_ids = [str(cid) for cid in faq_chunk_ids]
        allowed_ids = []
        blocked_ids = [str(c.id) for c in faq_blocked_chunks]
    elif faq_hit:
        recalled_ids = [str(cid) for cid in faq_chunk_ids]
        allowed_ids = [str(c.id) for c in faq_allowed_chunks]
        blocked_ids = [str(c.id) for c in faq_blocked_chunks]
    else:
        recalled_ids = [str(c.id) for c in recalled_chunks]
        allowed_ids = [str(c.id) for c in allowed_chunks]
        blocked_ids = [str(c.id) for c in blocked_chunks]

    audit = KnowledgeQaAudit(
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id or str(uuid.uuid4()),
        question=question,
        answer=answer_text,
        recalled_chunk_ids=recalled_ids,
        allowed_chunk_ids=allowed_ids,
        blocked_chunk_ids=blocked_ids,
        faq_hit=faq_hit,
        token_estimate=token_estimate,
        latency_ms=latency_ms,
    )
    db.add(audit)
    db.flush()

    # ---- 6. Knowledge gap detection ----
    gap_created = False
    if not faq_hit and not faq_permission_blocked and not recalled_chunks:
        highest_sim = scored[0][1] if scored else 0.0
        gap_created = _create_or_update_gap(
            db,
            tenant_id=tenant_id,
            question=question,
            department=department,
            highest_similarity=highest_sim,
        )

    db.commit()

    return {
        "answer": answer_text,
        "faq_hit": faq_hit,
        "faq_cache_hit": faq_cache_hit,
        "allowed_chunks": allowed_chunks if not faq_hit else faq_allowed_chunks,
        "blocked_chunks": blocked_chunks if not faq_hit else faq_blocked_chunks,
        "audit_id": audit.id,
        "gap_created": gap_created,
        "latency_ms": latency_ms,
        "missing_permission": bool(
            faq_permission_blocked
            or (blocked_chunks and not allowed_chunks and not faq_hit)
        ),
    }


def _create_or_update_gap(
    db: Session,
    tenant_id: uuid.UUID,
    question: str,
    department: str | None,
    highest_similarity: float,
) -> bool:
    """Create or update a knowledge gap record. Returns True if created."""
    normalized = question.lower().strip()

    # Find existing gaps with the same normalized question text
    existing_gaps = list(
        db.scalars(
            select(KnowledgeGap).where(
                KnowledgeGap.tenant_id == tenant_id,
                KnowledgeGap.status == KnowledgeGapStatus.open,
            ),
        ).all(),
    )
    for gap in existing_gaps:
        if gap.question.lower().strip() == normalized:
            gap.frequency += 1
            gap.highest_similarity = max(
                gap.highest_similarity,
                highest_similarity,
            )
            db.flush()
            return False  # Updated, not created

    gap = KnowledgeGap(
        tenant_id=tenant_id,
        question=question,
        department=department,
        frequency=1,
        highest_similarity=highest_similarity,
        status=KnowledgeGapStatus.open,
    )
    db.add(gap)
    db.flush()
    return True


# =====================================================================
# 5. FAQ clustering
# =====================================================================


def cluster_faq_candidates(
    db: Session,
    tenant_id: uuid.UUID,
    days: int = 7,
    min_frequency: int = 3,
) -> list[FaqCandidate]:
    """Aggregate recent Q&A audit records into FAQ candidates.

    Groups questions by normalized text (lowercased, whitespace-
    normalized).  Creates or updates ``FaqCandidate`` records.

    Returns the list of created/updated candidates.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)

    audits = list(
        db.scalars(
            select(KnowledgeQaAudit)
            .where(
                KnowledgeQaAudit.tenant_id == tenant_id,
                KnowledgeQaAudit.created_at >= cutoff,
            )
            .order_by(KnowledgeQaAudit.created_at.asc()),
        ).all(),
    )

    # Group by normalized question
    question_groups: dict[str, list[KnowledgeQaAudit]] = {}
    for audit in audits:
        normalized = re.sub(r"\s+", " ", audit.question.lower().strip())
        question_groups.setdefault(normalized, []).append(audit)

    candidates: list[FaqCandidate] = []
    for normalized_q, group in question_groups.items():
        if len(group) < min_frequency:
            continue

        cluster_key = hashlib.md5(normalized_q.encode()).hexdigest()[:16]
        representative = group[0]

        # Collect chunk IDs from all audits in the group
        all_chunk_ids: list[uuid.UUID] = []
        for a in group:
            all_chunk_ids.extend(a.allowed_chunk_ids)
        # Most common chunk IDs
        chunk_counter = Counter(all_chunk_ids)
        related_chunk_ids = [cid for cid, _ in chunk_counter.most_common(5)]

        # Use the first audit's answer as the recommended answer
        recommended_answer = representative.answer

        confidence = min(1.0, len(group) / 10.0)

        existing = db.scalar(
            select(FaqCandidate).where(
                FaqCandidate.tenant_id == tenant_id,
                FaqCandidate.cluster_key == cluster_key,
            ),
        )
        if existing is not None:
            existing.frequency = len(group)
            existing.related_chunk_ids = related_chunk_ids
            existing.recommended_answer = recommended_answer
            existing.confidence = confidence
            db.flush()
            candidates.append(existing)
        else:
            candidate = FaqCandidate(
                tenant_id=tenant_id,
                cluster_key=cluster_key,
                cluster_question=representative.question,
                frequency=len(group),
                related_chunk_ids=related_chunk_ids,
                recommended_answer=recommended_answer,
                confidence=confidence,
                status=FaqStatus.proposed,
            )
            db.add(candidate)
            db.flush()
            candidates.append(candidate)

    db.commit()
    logger.info(
        "Clustered %d FAQ candidates from %d audits (tenant=%s)",
        len(candidates),
        len(audits),
        tenant_id,
    )
    return candidates


def publish_faq(
    db: Session,
    candidate_id: uuid.UUID,
    answer: str | None = None,
    question: str | None = None,
    published_by: uuid.UUID | None = None,
) -> PublishedFaq | None:
    """Publish a FAQ candidate as a PublishedFaq.

    Updates the candidate status to ``published`` and creates a
    ``PublishedFaq`` record.
    """
    candidate = db.scalar(
        select(FaqCandidate).where(FaqCandidate.id == candidate_id),
    )
    if candidate is None:
        return None

    final_question = question or candidate.cluster_question
    final_answer = answer or candidate.recommended_answer

    published = PublishedFaq(
        tenant_id=candidate.tenant_id,
        cluster_key=candidate.cluster_key,
        question=final_question,
        answer=final_answer,
        related_chunk_ids=candidate.related_chunk_ids,
        confidence=candidate.confidence,
        published_by=published_by,
    )
    db.add(published)

    candidate.status = FaqStatus.published
    db.flush()
    db.commit()
    _write_faq_cache(published)
    return published


def reject_faq(
    db: Session,
    candidate_id: uuid.UUID,
) -> FaqCandidate | None:
    """Reject a FAQ candidate. Returns the updated candidate or None."""
    candidate = db.scalar(
        select(FaqCandidate).where(FaqCandidate.id == candidate_id),
    )
    if candidate is None:
        return None
    candidate.status = FaqStatus.rejected
    db.flush()
    db.commit()
    return candidate


def delete_faq_candidate(db: Session, candidate_id: uuid.UUID) -> bool:
    """Hard-delete a FAQ candidate."""
    candidate = db.scalar(
        select(FaqCandidate).where(FaqCandidate.id == candidate_id),
    )
    if candidate is None:
        return False
    db.delete(candidate)
    db.flush()
    return True


def delete_published_faq(db: Session, faq_id: uuid.UUID) -> bool:
    """Delete a published FAQ."""
    faq = db.scalar(
        select(PublishedFaq).where(PublishedFaq.id == faq_id),
    )
    if faq is None:
        return False
    tenant_id = faq.tenant_id
    question = faq.question
    db.delete(faq)
    db.flush()
    _invalidate_faq_cache(tenant_id, question)
    return True


# =====================================================================
# 6. Knowledge-gap diagnosis
# =====================================================================


def list_gaps(
    db: Session,
    tenant_id: uuid.UUID,
    status: KnowledgeGapStatus | None = None,
) -> list[KnowledgeGap]:
    """List knowledge gaps, optionally filtered by status."""
    stmt = (
        select(KnowledgeGap)
        .where(KnowledgeGap.tenant_id == tenant_id)
        .order_by(KnowledgeGap.frequency.desc())
    )
    if status is not None:
        stmt = stmt.where(KnowledgeGap.status == status)
    return list(db.scalars(stmt).all())


def resolve_gap(
    db: Session,
    gap_id: uuid.UUID,
    resolved_by: uuid.UUID | None = None,
) -> KnowledgeGap | None:
    """Mark a knowledge gap as resolved."""
    gap = db.scalar(select(KnowledgeGap).where(KnowledgeGap.id == gap_id))
    if gap is None:
        return None
    gap.status = KnowledgeGapStatus.resolved
    gap.resolved_by = resolved_by
    gap.resolved_at = datetime.now(UTC)
    db.flush()
    db.commit()
    return gap


def delete_gap(db: Session, gap_id: uuid.UUID) -> bool:
    """Delete a knowledge-gap diagnosis record."""
    gap = db.scalar(select(KnowledgeGap).where(KnowledgeGap.id == gap_id))
    if gap is None:
        return False
    db.delete(gap)
    db.flush()
    return True


# =====================================================================
# 7. Operational metrics
# =====================================================================


def get_operation_metrics(
    db: Session,
    tenant_id: uuid.UUID,
    days: int = 7,
) -> dict[str, Any]:
    """Aggregate operational metrics for the knowledge platform.

    Returns:
        total_questions: int
        access_count: int
        unique_users: int
        faq_hit_count: int
        faq_hit_rate: float
        average_latency_ms: float
        total_tokens: int
        blocked_access_count: int
        knowledge_units: int
        published_documents: int
        open_gaps: int
        proposed_faqs: int
        top_questions: list[dict]
        hot_knowledge: list[dict]
        daily: list[dict]  -- per-day breakdown
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)

    # Total questions and unique users
    audits = list(
        db.scalars(
            select(KnowledgeQaAudit).where(
                KnowledgeQaAudit.tenant_id == tenant_id,
                KnowledgeQaAudit.created_at >= cutoff,
            ),
        ).all(),
    )
    total_questions = len(audits)
    unique_users = len({a.user_id for a in audits})
    faq_hit_count = sum(1 for a in audits if a.faq_hit)
    faq_hit_rate = faq_hit_count / total_questions if total_questions > 0 else 0.0
    avg_latency = (
        sum(a.latency_ms for a in audits) / total_questions if total_questions > 0 else 0.0
    )
    total_tokens = sum(a.token_estimate for a in audits)
    blocked_access_count = sum(1 for a in audits if a.blocked_chunk_ids and not a.allowed_chunk_ids)

    # Knowledge-unit and document totals
    knowledge_units = (
        db.scalar(
            select(func.count(KnowledgeChunk.id)).where(
                KnowledgeChunk.tenant_id == tenant_id,
            ),
        )
        or 0
    )
    published_docs = (
        db.scalar(
            select(func.count(KnowledgeDocument.id)).where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.status == KnowledgeDocumentStatus.published,
            ),
        )
        or 0
    )

    # Frequently asked questions and hot knowledge units
    question_counter = Counter(
        re.sub(r"\s+", " ", audit.question.lower().strip())
        for audit in audits
        if audit.question.strip()
    )
    top_questions = [
        {"question": question, "count": count}
        for question, count in question_counter.most_common(10)
    ]

    hot_counter = Counter(
        str(chunk_id)
        for audit in audits
        for chunk_id in audit.allowed_chunk_ids
    )
    hot_ids: list[uuid.UUID] = []
    for chunk_id in hot_counter:
        try:
            hot_ids.append(uuid.UUID(str(chunk_id)))
        except (ValueError, TypeError):
            continue
    chunks_by_id: dict[str, KnowledgeChunk] = {}
    if hot_ids:
        chunks_by_id = {
            str(chunk.id): chunk
            for chunk in db.scalars(
                select(KnowledgeChunk).where(
                    KnowledgeChunk.tenant_id == tenant_id,
                    KnowledgeChunk.id.in_(hot_ids),
                ),
            ).all()
        }
    hot_knowledge = [
        {
            "chunk_id": chunk_id,
            "title": chunks_by_id[chunk_id].title
            if chunk_id in chunks_by_id
            else "已删除知识单元",
            "hit_count": count,
        }
        for chunk_id, count in hot_counter.most_common(10)
    ]

    # Open gaps
    open_gaps = (
        db.scalar(
            select(func.count(KnowledgeGap.id)).where(
                KnowledgeGap.tenant_id == tenant_id,
                KnowledgeGap.status == KnowledgeGapStatus.open,
            ),
        )
        or 0
    )

    # Proposed FAQs
    proposed_faqs = (
        db.scalar(
            select(func.count(FaqCandidate.id)).where(
                FaqCandidate.tenant_id == tenant_id,
                FaqCandidate.status == FaqStatus.proposed,
            ),
        )
        or 0
    )

    # Resolved gaps
    resolved_gaps = (
        db.scalar(
            select(func.count(KnowledgeGap.id)).where(
                KnowledgeGap.tenant_id == tenant_id,
                KnowledgeGap.status == KnowledgeGapStatus.resolved,
            ),
        )
        or 0
    )

    # Daily breakdown
    daily: list[dict[str, Any]] = []
    for i in range(days):
        day_start = datetime.now(UTC).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ) - timedelta(days=i)
        day_end = day_start + timedelta(days=1)
        day_audits = [a for a in audits if day_start <= a.created_at.replace(tzinfo=UTC) < day_end]
        day_latency = (
            sum(a.latency_ms for a in day_audits) / len(day_audits)
            if day_audits
            else 0.0
        )
        daily.append(
            {
                "date": day_start.strftime("%Y-%m-%d"),
                "questions": len(day_audits),
                "faq_hits": sum(1 for a in day_audits if a.faq_hit),
                "unique_users": len({a.user_id for a in day_audits}),
                "tokens": sum(a.token_estimate for a in day_audits),
                "average_latency_ms": round(day_latency, 2),
            }
        )

    daily.reverse()

    return {
        "total_questions": total_questions,
        "access_count": total_questions,
        "unique_users": unique_users,
        "faq_hit_count": faq_hit_count,
        "faq_hit_rate": round(faq_hit_rate, 4),
        "average_latency_ms": round(avg_latency, 2),
        "total_tokens": total_tokens,
        "blocked_access_count": blocked_access_count,
        "knowledge_units": knowledge_units,
        "published_documents": published_docs,
        "open_gaps": open_gaps,
        "proposed_faqs": proposed_faqs,
        "resolved_gaps": resolved_gaps,
        "top_questions": top_questions,
        "hot_knowledge": hot_knowledge,
        "daily": daily,
    }


# =====================================================================
# 8. Audit query
# =====================================================================


def list_audits(
    db: Session,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    session_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[KnowledgeQaAudit]:
    """List Q&A audit records."""
    stmt = (
        select(KnowledgeQaAudit)
        .where(KnowledgeQaAudit.tenant_id == tenant_id)
        .order_by(KnowledgeQaAudit.created_at.desc())
    )
    if user_id is not None:
        stmt = stmt.where(KnowledgeQaAudit.user_id == user_id)
    if session_id is not None:
        stmt = stmt.where(KnowledgeQaAudit.session_id == session_id)
    stmt = stmt.limit(limit).offset(offset)
    return list(db.scalars(stmt).all())


def delete_audit(db: Session, audit_id: uuid.UUID) -> bool:
    """Delete a single Q&A audit record."""
    audit = db.scalar(
        select(KnowledgeQaAudit).where(KnowledgeQaAudit.id == audit_id),
    )
    if audit is None:
        return False
    db.delete(audit)
    db.flush()
    return True
