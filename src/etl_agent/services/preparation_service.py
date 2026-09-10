"""Preparation service -- PDP risk rating, fingerprint computation, and
Preparation lifecycle management.

This module is the **only** place that:
1. Creates ``Preparation`` rows from a frozen ``PipelineVersion``.
2. Computes the ``input_fingerprint`` (SHA-256 of version content +
   resource scope + budget + rollback plan).
3. Runs the PDP (Policy Decision Point) to assign a P0-P3 risk level.
4. Verifies the fingerprint at commit time.

PDP Risk Factors (each adds weighted points):
- Sensitive data involved         -> +30
- Overwrite/drop production table -> +25
- Source is production environment -> +20
- Large data volume (>1M rows)   -> +15
- Multi-tenant shared resource    -> +15
- Not rollback-able               -> +10

Score -> Risk Level:
- 70+  -> P0 (critical, dual approval, no auto-commit)
- 50-69 -> P1 (high, dual approval)
- 25-49 -> P2 (medium, single approval, audit retained)
- 0-24  -> P3 (low, auto-approved, audit retained)
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from etl_agent.core.exceptions import InvalidInputError, NotFoundError
from etl_agent.core.permissions import assert_project_write
from etl_agent.models.evidence_ledger import EvidenceEventType
from etl_agent.models.pipeline_version import PipelineVersion, VersionStatus
from etl_agent.models.preparation import (
    DataClassification,
    Preparation,
    PreparationStatus,
    RiskLevel,
)
from etl_agent.schemas.generation import EtlPlan
from etl_agent.services.evidence_service import log_event

logger = logging.getLogger(__name__)

# =====================================================================
# Fingerprint computation
# =====================================================================


def compute_input_fingerprint(
    version: PipelineVersion,
    resource_scope: dict[str, Any],
    execution_budget: dict[str, Any],
    rollback_plan: dict[str, Any],
) -> str:
    """Compute the SHA-256 input fingerprint.

    The fingerprint covers:
    - The PipelineVersion's own SHA-256 (which itself covers etl_plan + hocon)
    - The resource scope (source/target tables, connections, shadow table)
    - The execution budget (max rows, bytes, duration)
    - The rollback plan (strategy, shadow table, restore SQL)

    This is computed once at preparation creation and re-verified at
    commit time.  If the PipelineVersion's content or any scope/budget/
    rollback parameter changed between prepare and commit, the
    fingerprint will not match and the commit is rejected.
    """
    payload = json.dumps(
        {
            "version_sha256": version.sha256,
            "resource_scope": _sort_dict(resource_scope),
            "execution_budget": _sort_dict(execution_budget),
            "rollback_plan": _sort_dict(rollback_plan),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sort_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Recursively sort dict keys for stable JSON serialization."""
    if not isinstance(d, dict):
        return d
    return {k: _sort_dict(v) if isinstance(v, dict) else v for k, v in sorted(d.items())}


# =====================================================================
# PDP (Policy Decision Point) risk assessment
# =====================================================================

# Score thresholds for risk level mapping
_P0_THRESHOLD = 70
_P1_THRESHOLD = 50
_P2_THRESHOLD = 25


def assess_risk(
    *,
    data_classification: DataClassification,
    resource_scope: dict[str, Any],
    execution_budget: dict[str, Any],
    rollback_plan: dict[str, Any],
    is_production_source: bool = False,
    is_multi_tenant: bool = False,
) -> tuple[RiskLevel, str]:
    """Assess risk and return (RiskLevel, human-readable reasoning).

    The assessment is deterministic -- no LLM involved.  Each risk
    factor adds weighted points, and the total maps to P0-P3.
    """
    score = 0
    reasons: list[str] = []

    # Factor 1: Sensitive data
    if data_classification in (DataClassification.confidential, DataClassification.restricted):
        score += 30
        reasons.append(f"Data classification is {data_classification.value} (+30)")
    elif data_classification == DataClassification.internal:
        score += 10
        reasons.append("Data classification is internal (+10)")

    # Factor 2: Overwrite/drop production table
    strategy = rollback_plan.get("strategy", "none")
    if strategy in ("truncate_restore", "drop_recreate"):
        score += 25
        reasons.append(f"Rollback strategy '{strategy}' is destructive (+25)")
    elif strategy == "shadow_swap":
        score += 5
        reasons.append("Rollback uses shadow swap (low risk, +5)")
    else:
        score += 10
        reasons.append("No rollback plan specified (+10)")

    # Factor 3: Source is production
    if is_production_source:
        score += 20
        reasons.append("Source connection is production environment (+20)")

    # Factor 4: Large data volume
    max_rows = execution_budget.get("max_rows", 0)
    if max_rows and max_rows > 1_000_000:
        score += 15
        reasons.append(f"Large data volume ({max_rows} rows, +15)")
    elif max_rows and max_rows > 100_000:
        score += 5
        reasons.append(f"Medium data volume ({max_rows} rows, +5)")

    # Factor 5: Multi-tenant shared resource
    if is_multi_tenant:
        score += 15
        reasons.append("Multi-tenant shared resource (+15)")

    # Map score to risk level
    if score >= _P0_THRESHOLD:
        risk = RiskLevel.P0
        reasons.append(f"Total score {score} >= {_P0_THRESHOLD} -> P0")
    elif score >= _P1_THRESHOLD:
        risk = RiskLevel.P1
        reasons.append(f"Total score {score} >= {_P1_THRESHOLD} -> P1")
    elif score >= _P2_THRESHOLD:
        risk = RiskLevel.P2
        reasons.append(f"Total score {score} >= {_P2_THRESHOLD} -> P2")
    else:
        risk = RiskLevel.P3
        reasons.append(f"Total score {score} < {_P2_THRESHOLD} -> P3")

    return risk, "; ".join(reasons)


def requires_dual_approval(risk: RiskLevel) -> bool:
    """P0 and P1 require dual (data + security) approval."""
    return risk in (RiskLevel.P0, RiskLevel.P1)


# =====================================================================
# Preparation lifecycle
# =====================================================================


def create_preparation(
    db: Session,
    *,
    pipeline_version_id: uuid.UUID,
    user_id: uuid.UUID,
    data_classification: DataClassification = DataClassification.internal,
    execution_budget: dict[str, Any] | None = None,
    is_production_source: bool = False,
    is_multi_tenant: bool = False,
) -> Preparation:
    """Create a Preparation from a frozen PipelineVersion.

    Steps:
    1. Load the PipelineVersion (must be frozen).
    2. Extract the EtlPlan to build resource_scope and rollback_plan.
    3. Compute the input_fingerprint.
    4. Run the PDP to assign a risk level.
    5. Insert the Preparation row.

    The caller must have write access to the project.
    """
    # Load the version
    version = db.scalar(select(PipelineVersion).where(PipelineVersion.id == pipeline_version_id))
    if version is None:
        raise NotFoundError(f"PipelineVersion {pipeline_version_id} not found")

    if version.status != VersionStatus.frozen:
        raise InvalidInputError(
            f"PipelineVersion {pipeline_version_id} is not frozen (status={version.status.value})"
        )

    # Permission check
    assert_project_write(db, user_id, version.project_id)

    # Parse the EtlPlan from the version's JSONB
    try:
        etl_plan = EtlPlan.model_validate(version.etl_plan)
    except Exception as exc:
        raise InvalidInputError(f"Failed to parse EtlPlan from version: {exc}") from exc

    # Build resource scope from the EtlPlan
    resource_scope = _build_resource_scope(version, etl_plan)

    # Build default execution budget
    budget = execution_budget or _default_budget(etl_plan)

    # Build rollback plan from the EtlPlan's shadow table
    rollback_plan = _build_rollback_plan(etl_plan)

    # Compute fingerprint
    fingerprint = compute_input_fingerprint(version, resource_scope, budget, rollback_plan)

    # Run PDP
    risk_level, reasoning = assess_risk(
        data_classification=data_classification,
        resource_scope=resource_scope,
        execution_budget=budget,
        rollback_plan=rollback_plan,
        is_production_source=is_production_source,
        is_multi_tenant=is_multi_tenant,
    )

    # Create the preparation
    prep = Preparation(
        pipeline_version_id=version.id,
        tenant_id=version.tenant_id,
        project_id=version.project_id,
        input_fingerprint=fingerprint,
        resource_scope=resource_scope,
        data_classification=data_classification,
        execution_budget=budget,
        rollback_plan=rollback_plan,
        risk_level=risk_level,
        risk_reasoning=reasoning,
        status=(
            PreparationStatus.approved if risk_level == RiskLevel.P3 else PreparationStatus.prepared
        ),
        prepared_by=user_id,
    )
    db.add(prep)
    db.flush()

    # Log to evidence ledger (same transaction)
    log_event(
        db,
        event_type=EvidenceEventType.prepared,
        actor_id=user_id,
        preparation_id=prep.id,
        tenant_id=prep.tenant_id,
        project_id=prep.project_id,
        details={
            "pipeline_version_id": str(version.id),
            "risk_level": risk_level.value,
            "data_classification": data_classification.value,
        },
    )

    db.commit()
    db.refresh(prep)

    logger.info(
        "Created Preparation %s for version %s, risk=%s",
        prep.id,
        version.id,
        risk_level.value,
    )
    return prep


def get_preparation(
    db: Session,
    preparation_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Preparation:
    """Load a Preparation and verify the caller has project access."""
    prep = db.scalar(select(Preparation).where(Preparation.id == preparation_id))
    if prep is None:
        raise NotFoundError(f"Preparation {preparation_id} not found")

    from etl_agent.core.permissions import assert_project_member

    assert_project_member(db, user_id, prep.project_id)
    return prep


def verify_fingerprint(
    prep: Preparation,
    version: PipelineVersion,
) -> bool:
    """Re-compute the fingerprint and compare to the stored value.

    Called at commit time to detect any tampering with the version
    content or scope/budget/rollback parameters since the preparation
    was created.
    """
    recomputed = compute_input_fingerprint(
        version,
        prep.resource_scope,
        prep.execution_budget,
        prep.rollback_plan,
    )
    return recomputed == prep.input_fingerprint


# =====================================================================
# Builders
# =====================================================================


def _build_resource_scope(
    version: PipelineVersion,
    etl_plan: EtlPlan,
) -> dict[str, Any]:
    """Extract the resource scope from the EtlPlan + version."""
    return {
        "source_connection_id": str(etl_plan.source.connection_id),
        "source_database": etl_plan.source.database_name,
        "source_schema": etl_plan.source.schema_name,
        "source_table": etl_plan.source.table_name,
        "target_connection_id": str(etl_plan.target.connection_id),
        "target_database": etl_plan.target.database_name,
        "target_schema": etl_plan.target.schema_name,
        "target_table": etl_plan.target.table_name,
        "shadow_table": etl_plan.target.shadow_table,
    }


def _default_budget(etl_plan: EtlPlan) -> dict[str, Any]:
    """Build a default execution budget from the EtlPlan."""
    return {
        "max_rows": 10_000_000,
        "max_bytes": 10 * 1024 * 1024 * 1024,  # 10 GB
        "max_duration_seconds": 3600,
    }


def _build_rollback_plan(etl_plan: EtlPlan) -> dict[str, Any]:
    """Build the rollback plan from the EtlPlan's shadow table."""
    if etl_plan.target.shadow_table:
        restore = (
            f"ALTER TABLE {etl_plan.target.schema_name}"
            f".{etl_plan.target.shadow_table} "
            f"RENAME TO {etl_plan.target.table_name};"
        )
        return {
            "strategy": "shadow_swap",
            "shadow_table": etl_plan.target.shadow_table,
            "restore_sql": restore,
        }
    return {
        "strategy": "none",
        "shadow_table": None,
        "restore_sql": None,
    }


__all__ = [
    "assess_risk",
    "compute_input_fingerprint",
    "create_preparation",
    "get_preparation",
    "requires_dual_approval",
    "verify_fingerprint",
]
