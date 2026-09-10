"""LangGraph shared state for the pipeline-generation chain.

This module defines ``PipelineState``, the single TypedDict that
flows through every node in the LangGraph state machine.  Each node
reads some fields, updates others, and returns a partial dict that
LangGraph merges back into the shared state.

State lifecycle::

    intent      -> sets requirement, clarification_needed
    clarify     -> if clarification_needed: sets clarification_questions, stops
                   else: clears clarification, continues
    metadata    -> sets metadata_context (source + target profiles)
    planner     -> sets etl_plan
    hocon       -> sets hocon_config
    quality     -> sets validation_issues
    autofix     -> if errors and attempts < max: patches etl_plan/hocon, loops back to quality
                   else if errors remain: sets status=needs_review, stops
    freeze      -> sets pipeline_version, status=frozen

The state is also serialised to the ``pipelines`` table's JSONB
columns so that interrupted runs can resume from a checkpoint.
"""

from __future__ import annotations

from typing import Any, TypedDict

from etl_agent.schemas.generation import (
    EtlPlan,
    GeneratedHocon,
    PipelineRequirement,
    ValidationIssue,
)


class PipelineState(TypedDict, total=False):
    """Shared state for the pipeline-generation LangGraph.

    ``total=False`` means every field is optional -- nodes only set
    the fields they produce.  This allows the graph to start with
    a minimal initial state and accumulate fields as nodes execute.

    Fields:
        requirement: Parsed user requirement (from intent node).
        clarification_needed: True when the intent node detects
            missing required parameters.
        clarification_questions: List of ``{"field": str, "question": str}``
            dicts populated when clarification is needed.
        answers: User-provided answers to clarification questions
            (set by the clarify node when resuming).
        metadata_context: Dict with ``source`` and ``target`` keys,
            each containing the real MetadataProfile data for the
            respective connection.
        etl_plan: The structured logical plan (from planner node).
        hocon_config: The generated SeaTunnel HOCON (from hocon node).
        validation_issues: List of gate-check findings (from quality node).
        pipeline_version: The frozen version dict (from freeze node).
        error: Error message if any node failed.
        resume_point: Resumption point for interrupted runs.
        autofix_attempts: How many autofix iterations have run.
        status: Current pipeline status (draft, needs_clarification,
            validated, frozen, error).
        pipeline_id: UUID of the Pipeline ORM row (for persistence).
        user_id: UUID of the user who initiated the generation.
        project_id: UUID of the project scope.
        tenant_id: UUID of the tenant scope.
    """

    # ---- Input ----
    requirement: PipelineRequirement
    natural_language: str
    source_connection_id: str
    target_connection_id: str
    sync_mode: str
    source_table: str | None
    target_table: str | None
    primary_keys: list[str]
    watermark_column: str | None

    # ---- Clarification ----
    clarification_needed: bool
    clarification_questions: list[dict[str, str]]
    answers: dict[str, str]
    # Partial parsed intent dict stored when clarification is triggered,
    # so the clarify node can merge user answers on resume.
    parsed_intent: dict[str, Any]

    # ---- Metadata ----
    metadata_context: dict[str, Any]

    # ---- Generated artifacts ----
    etl_plan: EtlPlan
    hocon_config: GeneratedHocon

    # ---- Validation ----
    validation_issues: list[ValidationIssue]

    # ---- Frozen version ----
    pipeline_version: dict[str, Any]

    # ---- Control flow ----
    error: str
    resume_point: str
    autofix_attempts: int
    status: str
    existing_version_count: int

    # ---- Persistence context ----
    pipeline_id: str
    user_id: str
    project_id: str
    tenant_id: str


# =====================================================================
# Status constants
# =====================================================================

STATUS_DRAFT = "draft"
STATUS_NEEDS_CLARIFICATION = "needs_clarification"
STATUS_GENERATING = "generating"
STATUS_VALIDATED = "validated"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_FROZEN = "frozen"
STATUS_ERROR = "error"

# Maximum autofix iterations before escalating to human review
MAX_AUTOFIX_ATTEMPTS = 3


def initial_state(
    *,
    natural_language: str,
    source_connection_id: str,
    target_connection_id: str,
    project_id: str,
    tenant_id: str,
    user_id: str,
) -> PipelineState:
    """Create the initial state for a new pipeline generation run.

    This is the minimal seed state that the ``intent`` node expects
    as input.  All other fields are populated by subsequent nodes.
    """
    return PipelineState(
        natural_language=natural_language,
        source_connection_id=source_connection_id,
        target_connection_id=target_connection_id,
        project_id=project_id,
        tenant_id=tenant_id,
        user_id=user_id,
        status=STATUS_DRAFT,
        autofix_attempts=0,
        clarification_needed=False,
    )


__all__ = [
    "MAX_AUTOFIX_ATTEMPTS",
    "PipelineState",
    "STATUS_DRAFT",
    "STATUS_ERROR",
    "STATUS_FROZEN",
    "STATUS_GENERATING",
    "STATUS_NEEDS_CLARIFICATION",
    "STATUS_NEEDS_REVIEW",
    "STATUS_VALIDATED",
    "initial_state",
]
