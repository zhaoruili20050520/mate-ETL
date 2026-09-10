"""ORM models package -- import all models so Alembic autogenerate sees them."""

from __future__ import annotations

from etl_agent.models.approval import (
    Approval,
    ApprovalDecision,
    ApprovalRole,
)
from etl_agent.models.capability_log import (
    CapabilityAction,
    CapabilityLog,
)
from etl_agent.models.data_source_connection import (
    ConnectionStatus,
    DataSourceConnection,
    DataSourceType,
)
from etl_agent.models.department import Department
from etl_agent.models.etl_plan import Pipeline, PipelineStatus
from etl_agent.models.evidence_ledger import (
    GENESIS_HASH,
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
from etl_agent.models.exploration_task import ExplorationStatus, ExplorationTask
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
from etl_agent.models.metadata_profile import MetadataProfile
from etl_agent.models.mixins import TimestampMixin, UUIDPKMixin
from etl_agent.models.outbox import (
    OutboxMessage,
    OutboxStatus,
)
from etl_agent.models.permission import (
    PERMISSION_CODES,
    RolePermission,
)
from etl_agent.models.pipeline_version import (
    PipelineVersion,
    PipelineVersionHistory,
    VersionAction,
    VersionStatus,
)
from etl_agent.models.preparation import (
    DataClassification,
    Preparation,
    PreparationStatus,
    RiskLevel,
)
from etl_agent.models.project import Project, ProjectStatus
from etl_agent.models.project_member import ProjectMember
from etl_agent.models.publish_attempt import (
    PublishAttempt,
    PublishAttemptStatus,
)
from etl_agent.models.quality_report import (
    QualityReport,
    QualityReportStatus,
)
from etl_agent.models.role import Role
from etl_agent.models.runtime_supervision_contract import (
    RuntimeSupervisionContract,
    SwapStrategy,
)
from etl_agent.models.tenant import Tenant, TenantStatus
from etl_agent.models.tenant_member import TenantMember
from etl_agent.models.user import User, UserStatus

__all__ = [
    "Approval",
    "ApprovalDecision",
    "ApprovalRole",
    "CapabilityAction",
    "CapabilityLog",
    "ConnectionStatus",
    "DataSourceConnection",
    "DataSourceType",
    "Department",
    "DataClassification",
    "ExecutionEvent",
    "ExecutionEventType",
    "ExecutionMetric",
    "ExecutionRun",
    "ExecutionStatus",
    "FaqCandidate",
    "Department",
    "FaqStatus",
    "EvidenceEventType",
    "EvidenceLedger",
    "GENESIS_HASH",
    "ExplorationStatus",
    "ExplorationTask",
    "MetadataProfile",
    "PERMISSION_CODES",
    "RolePermission",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "KnowledgeDocumentStatus",
    "KnowledgeGap",
    "KnowledgeGapStatus",
    "KnowledgeMember",
    "KnowledgeQaAudit",
    "KnowledgeRole",
    "OutboxMessage",
    "OutboxStatus",
    "Pipeline",
    "PipelineStatus",
    "PipelineVersion",
    "PipelineVersionHistory",
    "Preparation",
    "PreparationStatus",
    "Project",
    "ProjectMember",
    "ProjectStatus",
    "PublishAttempt",
    "PublishAttemptStatus",
    "QualityReport",
    "QualityReportStatus",
    "PublishedFaq",
    "RolePermission",
    "RiskLevel",
    "Role",
    "RuntimeSupervisionContract",
    "SwapStrategy",
    "Tenant",
    "TenantMember",
    "TenantStatus",
    "TimestampMixin",
    "UUIDPKMixin",
    "User",
    "UserStatus",
    "VersionAction",
    "VersionStatus",
]
