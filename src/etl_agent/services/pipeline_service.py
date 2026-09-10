"""Service layer bridging API routes and the LangGraph pipeline-generation chain.

This module is the **only** place that:
1. Creates / updates ``Pipeline`` ORM rows.
2. Invokes the compiled ``pipeline_graph``.
3. Persists graph output (EtlPlan, HOCON, validation issues, clarification
   questions) back to the ``pipelines`` table.
4. Freezes immutable ``PipelineVersion`` rows with SHA256.

Route handlers in ``api/v1/pipelines.py`` call these functions; they do
not invoke the graph or touch ORM rows directly.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from etl_agent.agents.graph import pipeline_graph
from etl_agent.agents.state import (
    STATUS_DRAFT,
    STATUS_NEEDS_CLARIFICATION,
    PipelineState,
    initial_state,
)
from etl_agent.core.exceptions import InvalidInputError, NotFoundError
from etl_agent.core.permissions import assert_project_member, assert_project_write
from etl_agent.models.etl_plan import Pipeline, PipelineStatus
from etl_agent.models.pipeline_version import (
    PipelineVersion,
    PipelineVersionHistory,
    VersionAction,
    VersionStatus,
)
from etl_agent.models.project import Project
from etl_agent.schemas.generation import (
    EtlPlan,
    GeneratedHocon,
    PipelineRequirement,
    ValidationIssue,
)

logger = logging.getLogger(__name__)


# =====================================================================
# Helpers
# =====================================================================


def _to_pipeline_status(status_str: str) -> PipelineStatus:
    """Map a state-machine status string to the ORM enum."""
    try:
        return PipelineStatus(status_str)
    except ValueError:
        # Map graph-specific statuses not in the ORM enum
        if status_str in ("needs_review", "generating"):
            return PipelineStatus.generating
        return PipelineStatus.error


def _persist_graph_state(pipeline: Pipeline, final_state: dict[str, Any]) -> None:
    """Write the LangGraph final state back to the Pipeline ORM row.

    Called after ``pipeline_graph.invoke()`` returns.  Reads specific
    keys from the state dict and serialises them to the Pipeline's
    JSONB / Text columns.
    """
    status_str = final_state.get("status", STATUS_DRAFT)
    pipeline.status = _to_pipeline_status(status_str)
    pipeline.autofix_attempts = final_state.get("autofix_attempts", 0)

    # Persist the requirement (or parsed_intent when clarification needed)
    requirement = final_state.get("requirement")
    if requirement is not None and isinstance(requirement, PipelineRequirement):
        pipeline.source_requirement = requirement.model_dump(mode="json")
    else:
        parsed_intent = final_state.get("parsed_intent")
        if parsed_intent:
            pipeline.source_requirement = dict(parsed_intent)

    # Persist EtlPlan
    etl_plan = final_state.get("etl_plan")
    if etl_plan is not None and isinstance(etl_plan, EtlPlan):
        pipeline.etl_plan = etl_plan.model_dump(mode="json")

    # Persist HOCON text
    hocon = final_state.get("hocon_config")
    if hocon is not None and isinstance(hocon, GeneratedHocon):
        pipeline.hocon_config = hocon.config_text

    # Persist validation issues
    issues = final_state.get("validation_issues", [])
    if issues:
        pipeline.validation_issues = [
            i.model_dump(mode="json") if isinstance(i, ValidationIssue) else dict(i) for i in issues
        ]

    # Persist clarification questions
    questions = final_state.get("clarification_questions", [])
    pipeline.clarification_questions = list(questions) if questions else None

    # Persist error
    error = final_state.get("error")
    if error:
        logger.warning("Pipeline %s ended with error: %s", pipeline.id, error)
        pipeline.status = PipelineStatus.error


def _persist_frozen_version(
    db: Session,
    pipeline: Pipeline,
    final_state: dict[str, Any],
) -> None:
    """Persist the immutable version prepared by the graph freeze node."""
    prepared = final_state.get("pipeline_version")
    if not isinstance(prepared, dict):
        return
    existing = db.scalar(
        select(PipelineVersion).where(
            PipelineVersion.pipeline_id == pipeline.id,
            PipelineVersion.version_number == int(prepared.get("version_number", 1)),
        ),
    )
    if existing is not None:
        return
    if pipeline.etl_plan is None or pipeline.hocon_config is None:
        return

    version = PipelineVersion(
        pipeline_id=pipeline.id,
        tenant_id=pipeline.tenant_id,
        project_id=pipeline.project_id,
        version_number=int(prepared.get("version_number", 1)),
        etl_plan=pipeline.etl_plan,
        hocon_text=pipeline.hocon_config,
        sha256=str(prepared.get("sha256", "")),
        source_requirement=pipeline.source_requirement or {},
        validation_results=pipeline.validation_issues or [],
        status=VersionStatus.frozen,
        frozen_by=pipeline.created_by,
    )
    db.add(version)
    db.flush()
    db.add(
        PipelineVersionHistory(
            pipeline_version_id=version.id,
            action=VersionAction.frozen,
            action_by=pipeline.created_by,
            notes="Automatically frozen after pipeline generation",
        ),
    )


def _build_resume_state(pipeline: Pipeline, answers: dict[str, str]) -> PipelineState:
    """Reconstruct a PipelineState from a persisted Pipeline row + user answers.

    This is used when the pipeline was interrupted in
    ``needs_clarification`` state and the user has now provided
    answers to the clarification questions.
    """
    source_req = pipeline.source_requirement or {}
    return PipelineState(
        natural_language=source_req.get("natural_language", ""),
        source_connection_id=str(pipeline.source_connection_id),
        target_connection_id=str(pipeline.target_connection_id),
        project_id=str(pipeline.project_id),
        tenant_id=str(pipeline.tenant_id),
        user_id=str(pipeline.created_by) if pipeline.created_by else "",
        pipeline_id=str(pipeline.id),
        status=STATUS_NEEDS_CLARIFICATION,
        clarification_needed=True,
        clarification_questions=pipeline.clarification_questions or [],
        answers=answers,
        parsed_intent=source_req,
        autofix_attempts=pipeline.autofix_attempts,
    )


def _get_pipeline_or_404(db: Session, pipeline_id: uuid.UUID) -> Pipeline:
    pipeline = db.get(Pipeline, pipeline_id)
    if pipeline is None:
        raise NotFoundError(f"Pipeline {pipeline_id} not found")
    return pipeline


def _auto_name(natural_language: str) -> str:
    """Generate a pipeline name from the natural-language text."""
    name = natural_language.strip()[:50]
    if len(natural_language.strip()) > 50:
        name += "..."
    return name or "Untitled Pipeline"


# =====================================================================
# Public API
# =====================================================================


def generate_pipeline(
    db: Session,
    *,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
    source_connection_id: uuid.UUID,
    target_connection_id: uuid.UUID,
    natural_language: str,
    name: str | None = None,
    hints: dict[str, Any] | None = None,
) -> Pipeline:
    """Create a pipeline and run the LangGraph generation chain.

    1. Verify the user has write access to the project.
    2. Verify source/target connections belong to the project.
    3. Create a Pipeline row (status=draft).
    4. Build the initial LangGraph state (with hints merged in).
    5. Invoke ``pipeline_graph``.
    6. Persist the final state back to the Pipeline row.

    Returns the updated Pipeline ORM object.
    """
    # ---- Access check ----
    role = assert_project_write(db, actor_id, project_id)

    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"Project {project_id} not found")

    # ---- Verify connections belong to this project ----
    from etl_agent.models.data_source_connection import DataSourceConnection

    for label, conn_id in (
        ("source", source_connection_id),
        ("target", target_connection_id),
    ):
        conn = db.scalar(
            select(DataSourceConnection).where(
                DataSourceConnection.id == conn_id,
                DataSourceConnection.project_id == project_id,
            )
        )
        if conn is None:
            raise NotFoundError(
                f"{label.capitalize()} connection {conn_id} not found in project {project_id}"
            )

    # ---- Create Pipeline row ----
    pipeline = Pipeline(
        tenant_id=project.tenant_id,
        project_id=project_id,
        name=name or _auto_name(natural_language),
        source_connection_id=source_connection_id,
        target_connection_id=target_connection_id,
        status=PipelineStatus.draft,
        autofix_attempts=0,
        created_by=actor_id,
    )
    db.add(pipeline)
    db.flush()  # Get UUID without committing

    # ---- Build initial state ----
    state = initial_state(
        natural_language=natural_language,
        source_connection_id=str(source_connection_id),
        target_connection_id=str(target_connection_id),
        project_id=str(project_id),
        tenant_id=str(project.tenant_id),
        user_id=str(actor_id),
    )
    state["pipeline_id"] = str(pipeline.id)

    # Merge user-provided hints into state for the intent node to pick up
    if hints:
        sync_mode = hints.get("sync_mode")
        if sync_mode:
            state["sync_mode"] = str(sync_mode)
        source_table = hints.get("source_table")
        if source_table:
            state["source_table"] = str(source_table)
        target_table = hints.get("target_table")
        if target_table:
            state["target_table"] = str(target_table)
        primary_keys = hints.get("primary_keys")
        if isinstance(primary_keys, list) and primary_keys:
            state["primary_keys"] = [str(k) for k in primary_keys]
        watermark_column = hints.get("watermark_column")
        if watermark_column:
            state["watermark_column"] = str(watermark_column)

    # ---- Invoke the graph ----
    try:
        final_state = pipeline_graph.invoke(state)
    except Exception as exc:
        logger.exception("LangGraph invocation failed for pipeline %s", pipeline.id)
        pipeline.status = PipelineStatus.error
        pipeline.source_requirement = {"natural_language": natural_language, "error": str(exc)}
        db.commit()
        raise InvalidInputError(f"Pipeline generation failed: {exc}") from exc

    # ---- Persist graph output ----
    _persist_graph_state(pipeline, final_state)
    _persist_frozen_version(db, pipeline, final_state)
    db.commit()
    db.refresh(pipeline)

    logger.info(
        "Pipeline %s generated, status=%s, role=%s",
        pipeline.id,
        pipeline.status.value,
        role,
    )
    return pipeline


def clarify_pipeline(
    db: Session,
    *,
    user_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    answers: dict[str, str],
) -> Pipeline:
    """Submit clarification answers and resume the generation chain.

    1. Get the pipeline (with access check).
    2. Verify it is in ``needs_clarification`` status.
    3. Reconstruct state from the persisted row + user answers.
    4. Re-invoke the graph.
    5. Persist the updated state.
    """
    pipeline = _get_pipeline_or_404(db, pipeline_id)

    # Access check
    assert_project_member(db, user_id, pipeline.project_id)

    # Status check
    if pipeline.status != PipelineStatus.needs_clarification:
        raise InvalidInputError(
            f"Pipeline is in '{pipeline.status.value}' state, "
            f"not 'needs_clarification'. Cannot submit answers."
        )

    # ---- Rebuild state and resume ----
    state = _build_resume_state(pipeline, answers)

    try:
        final_state = pipeline_graph.invoke(state)
    except Exception as exc:
        logger.exception("LangGraph resume failed for pipeline %s", pipeline.id)
        pipeline.status = PipelineStatus.error
        db.commit()
        raise InvalidInputError(f"Pipeline resume failed: {exc}") from exc

    _persist_graph_state(pipeline, final_state)
    _persist_frozen_version(db, pipeline, final_state)
    db.commit()
    db.refresh(pipeline)

    logger.info("Pipeline %s clarified, status=%s", pipeline.id, pipeline.status.value)
    return pipeline


def freeze_version(
    db: Session,
    *,
    user_id: uuid.UUID,
    pipeline_id: uuid.UUID,
) -> PipelineVersion:
    """Freeze the pipeline's current validated state into an immutable version.

    1. Get the pipeline (with write access).
    2. Verify status is ``validated`` (gate checker passed) or ``frozen``.
    3. Count existing versions to determine the next version_number.
    4. Compute SHA256 from the EtlPlan + HOCON text.
    5. Create PipelineVersion + PipelineVersionHistory rows.
    6. Update Pipeline status to ``frozen``.
    """
    pipeline = _get_pipeline_or_404(db, pipeline_id)

    # Write access required (engineer+)
    assert_project_write(db, user_id, pipeline.project_id)

    # Status check: only validated pipelines can be frozen
    if pipeline.status not in (PipelineStatus.validated, PipelineStatus.frozen):
        raise InvalidInputError(
            f"Pipeline must be in 'validated' state to freeze, "
            f"current state is '{pipeline.status.value}'"
        )

    # The freeze node in the graph already prepared the version dict.
    # But we also re-derive it here from the persisted state to be safe.
    etl_plan_dict = pipeline.etl_plan
    hocon_text = pipeline.hocon_config

    if etl_plan_dict is None or hocon_text is None:
        raise InvalidInputError("Cannot freeze: EtlPlan or HOCON is missing")

    import hashlib
    import json

    # Canonical JSON for SHA256
    plan_json = json.dumps(etl_plan_dict, sort_keys=True, ensure_ascii=False)
    combined = plan_json + "\n" + hocon_text
    sha256 = hashlib.sha256(combined.encode("utf-8")).hexdigest()

    # Determine version number by counting existing versions
    existing_versions = db.scalars(
        select(PipelineVersion).where(PipelineVersion.pipeline_id == pipeline_id)
    ).all()
    version_number = len(existing_versions) + 1

    # Check for duplicate SHA256 (same content already frozen)
    for v in existing_versions:
        if v.sha256 == sha256:
            raise InvalidInputError(
                f"Version {v.version_number} already has the same content (SHA256 match). "
                "Cannot create a duplicate frozen version."
            )

    # Create the frozen version
    version = PipelineVersion(
        pipeline_id=pipeline_id,
        tenant_id=pipeline.tenant_id,
        project_id=pipeline.project_id,
        version_number=version_number,
        etl_plan=etl_plan_dict,
        hocon_text=hocon_text,
        sha256=sha256,
        source_requirement=pipeline.source_requirement or {},
        validation_results=pipeline.validation_issues or [],
        status=VersionStatus.frozen,
        frozen_by=user_id,
    )
    db.add(version)
    db.flush()

    # Audit history
    history = PipelineVersionHistory(
        pipeline_version_id=version.id,
        action=VersionAction.frozen,
        action_by=user_id,
        notes=f"Frozen by user {user_id}",
    )
    db.add(history)

    # Update pipeline status
    pipeline.status = PipelineStatus.frozen
    db.commit()
    db.refresh(version)

    logger.info(
        "Pipeline %s frozen: version %d, sha256=%s...",
        pipeline_id,
        version_number,
        sha256[:12],
    )
    return version


def list_pipelines(
    db: Session,
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
) -> list[Pipeline]:
    """List pipelines in a project (isolated)."""
    assert_project_member(db, user_id, project_id)
    return list(
        db.scalars(
            select(Pipeline)
            .where(Pipeline.project_id == project_id)
            .order_by(Pipeline.created_at.desc())
        )
    )


def get_pipeline(
    db: Session,
    *,
    user_id: uuid.UUID,
    pipeline_id: uuid.UUID,
) -> Pipeline:
    """Get a single pipeline (with access check)."""
    pipeline = _get_pipeline_or_404(db, pipeline_id)
    assert_project_member(db, user_id, pipeline.project_id)
    return pipeline


def delete_pipeline(
    db: Session,
    *,
    user_id: uuid.UUID,
    pipeline_id: uuid.UUID,
) -> None:
    """Delete an unfinished pipeline that has no frozen versions."""
    pipeline = _get_pipeline_or_404(db, pipeline_id)
    assert_project_write(db, user_id, pipeline.project_id)
    version_count = db.scalar(
        select(func.count())
        .select_from(PipelineVersion)
        .where(PipelineVersion.pipeline_id == pipeline_id),
    )
    if version_count:
        raise InvalidInputError(
            "Pipeline has frozen versions and cannot be deleted. "
            "Create a new version or keep it for audit."
        )
    db.delete(pipeline)
    db.commit()


def list_versions(
    db: Session,
    *,
    user_id: uuid.UUID,
    pipeline_id: uuid.UUID,
) -> list[PipelineVersion]:
    """List frozen versions for a pipeline (with access check)."""
    pipeline = _get_pipeline_or_404(db, pipeline_id)
    assert_project_member(db, user_id, pipeline.project_id)
    return list(
        db.scalars(
            select(PipelineVersion)
            .where(PipelineVersion.pipeline_id == pipeline_id)
            .order_by(PipelineVersion.version_number.desc())
        )
    )


def get_version(
    db: Session,
    *,
    user_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    version_id: uuid.UUID,
) -> PipelineVersion:
    """Get a specific frozen version (with access check)."""
    pipeline = _get_pipeline_or_404(db, pipeline_id)
    assert_project_member(db, user_id, pipeline.project_id)

    version = db.scalar(
        select(PipelineVersion).where(
            PipelineVersion.id == version_id,
            PipelineVersion.pipeline_id == pipeline_id,
        )
    )
    if version is None:
        raise NotFoundError(f"Version {version_id} not found in pipeline {pipeline_id}")
    return version


__all__ = [
    "clarify_pipeline",
    "delete_pipeline",
    "freeze_version",
    "generate_pipeline",
    "get_pipeline",
    "get_version",
    "list_pipelines",
    "list_versions",
]
