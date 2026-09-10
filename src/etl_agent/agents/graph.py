r"""LangGraph assembly -- wire all pipeline-generation nodes into a state machine.

This module builds the ``StateGraph`` that orchestrates the full
pipeline-generation chain::

    START
      -> intent
      -> clarify (conditional: needs_clarification? -> END, else -> metadata)
      -> metadata
      -> planner
      -> hocon_generator
      -> quality_checker (conditional:
           validated? -> freeze -> END
           needs_review? -> autofix (conditional:
             still fixable? -> quality_checker (loop)
             max attempts / unfixable? -> END (needs_review)
           )
           error? -> END)

The graph uses conditional edges to implement:

1. **Clarification interrupt**: when the intent node sets
   ``clarification_needed=True`` and no answers are provided, the
   clarify node stops the graph so the API layer can return the
   questions to the user.

2. **Autofix loop**: when the quality checker finds errors, the
   autofix node attempts to patch them and routes back to the
   quality checker for re-validation, up to ``MAX_AUTOFIX_ATTEMPTS``.

3. **Freeze**: when the quality checker returns ``validated``, the
   freeze node creates the immutable ``PipelineVersion`` + SHA256.

The graph is compiled once at module load and exposed as
``pipeline_graph``.  Callers invoke it via ``pipeline_graph.invoke(state)``.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from langgraph.graph import END, START, StateGraph

from etl_agent.agents.nodes.autofix import autofix_node
from etl_agent.agents.nodes.clarify import clarify_node
from etl_agent.agents.nodes.hocon_generator import hocon_generator_node
from etl_agent.agents.nodes.intent import intent_node
from etl_agent.agents.nodes.metadata import metadata_node
from etl_agent.agents.nodes.planner import planner_node
from etl_agent.agents.nodes.quality_checker import quality_checker_node
from etl_agent.agents.state import (
    MAX_AUTOFIX_ATTEMPTS,
    STATUS_DRAFT,
    STATUS_ERROR,
    STATUS_FROZEN,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_NEEDS_REVIEW,
    STATUS_VALIDATED,
    PipelineState,
)

logger = logging.getLogger(__name__)

# =====================================================================
# Node names (used as graph node identifiers)
# =====================================================================

NODE_INTENT = "intent"
NODE_CLARIFY = "clarify"
NODE_METADATA = "metadata"
NODE_PLANNER = "planner"
NODE_HOCON = "hocon"
NODE_QUALITY = "quality"
NODE_AUTOFIX = "autofix"
NODE_FREEZE = "freeze"


# =====================================================================
# Freeze node -- create immutable PipelineVersion
# =====================================================================


def freeze_node(state: PipelineState) -> dict[str, Any]:
    """Freeze the validated EtlPlan + HOCON into an immutable version.

    This node:
    1. Reads the validated ``etl_plan`` and ``hocon_config``.
    2. Serialises them to a canonical JSON string.
    3. Computes SHA256 of the serialised content.
    4. Builds a ``pipeline_version`` dict with version number, SHA256,
       created_by, source requirement, and validation results.
    5. Sets ``status=frozen``.

    The actual persistence to ``pipeline_versions`` table is done by
    the API/service layer after the graph completes -- this node only
    prepares the version payload.
    """
    etl_plan = state.get("etl_plan")
    hocon_config = state.get("hocon_config")
    validation_issues = state.get("validation_issues", [])
    requirement = state.get("requirement")

    if etl_plan is None or hocon_config is None:
        return {"error": "Cannot freeze: etl_plan or hocon_config missing", "status": STATUS_ERROR}

    # Canonical JSON for SHA256 -- sorted keys, no extra whitespace
    plan_json = etl_plan.model_dump_json()
    hocon_text = hocon_config.config_text
    combined = plan_json + "\n" + hocon_text

    sha256 = hashlib.sha256(combined.encode("utf-8")).hexdigest()

    # Determine version number (incremental within a pipeline)
    existing_versions = state.get("existing_version_count", 0)
    version_number = existing_versions + 1

    pipeline_version: dict[str, Any] = {
        "version_number": version_number,
        "sha256": sha256,
        "etl_plan_json": plan_json,
        "hocon_text": hocon_text,
        "source_requirement": requirement.model_dump() if requirement else None,
        "validation_results": [i.model_dump() for i in validation_issues],
        "created_by": state.get("user_id", ""),
        "pipeline_id": state.get("pipeline_id", ""),
        "frozen_at": None,  # Set by service layer on DB insert
    }

    logger.info(
        "Freeze node: version %d, sha256=%s...",
        version_number,
        sha256[:12],
    )

    return {
        "pipeline_version": pipeline_version,
        "status": STATUS_FROZEN,
    }


# =====================================================================
# Conditional edge routers
# =====================================================================


def _route_after_clarify(state: PipelineState) -> str:
    """Route after the clarify node.

    - If ``status`` is ``needs_clarification``: stop (go to END).
    - If ``status`` is ``error``: stop (go to END).
    - Otherwise (``draft``): continue to metadata.
    """
    status = state.get("status", STATUS_DRAFT)
    if status in (STATUS_NEEDS_CLARIFICATION, STATUS_ERROR):
        return END
    return NODE_METADATA


def _route_after_quality(state: PipelineState) -> str:
    """Route after the quality checker node.

    - If ``status`` is ``validated``: go to freeze.
    - If ``status`` is ``needs_review``: go to autofix.
    - If ``status`` is ``error``: stop (go to END).
    """
    status = state.get("status", STATUS_DRAFT)
    if status == STATUS_VALIDATED:
        return NODE_FREEZE
    if status == STATUS_NEEDS_REVIEW:
        return NODE_AUTOFIX
    return END


def _route_after_autofix(state: PipelineState) -> str:
    """Route after the autofix node.

    - If ``status`` is ``generating``: go back to quality checker.
    - If ``status`` is ``needs_review``: stop (human review needed).
    - If ``status`` is ``validated``: go to freeze.
    - If ``status`` is ``error``: stop.
    """
    status = state.get("status", STATUS_DRAFT)

    # Check attempt limit
    attempts = state.get("autofix_attempts", 0)
    if attempts >= MAX_AUTOFIX_ATTEMPTS:
        logger.warning("Autofix: max attempts reached, escalating to human review")
        return END

    if status == STATUS_VALIDATED:
        return NODE_FREEZE
    if status == STATUS_NEEDS_REVIEW:
        return END
    # status == "generating" -> re-validate
    return NODE_QUALITY


def _route_after_intent(state: PipelineState) -> str:
    """Route after the intent node.

    - If ``clarification_needed`` is True: go to clarify.
    - If ``status`` is ``error``: stop.
    - Otherwise: go directly to metadata (skip clarify).
    """
    if state.get("clarification_needed", False):
        return NODE_CLARIFY
    status = state.get("status", STATUS_DRAFT)
    if status == STATUS_ERROR:
        return END
    return NODE_METADATA


def _route_after_metadata(state: PipelineState) -> str:
    """Continue only when metadata collection succeeded."""
    if state.get("status", STATUS_DRAFT) == STATUS_ERROR:
        return END
    return NODE_PLANNER


def _route_after_planner(state: PipelineState) -> str:
    """Continue only when plan generation succeeded."""
    if state.get("status", STATUS_DRAFT) == STATUS_ERROR:
        return END
    return NODE_HOCON


def _route_after_hocon(state: PipelineState) -> str:
    """Continue only when HOCON generation succeeded."""
    if state.get("status", STATUS_DRAFT) == STATUS_ERROR:
        return END
    return NODE_QUALITY


# =====================================================================
# Build the graph
# =====================================================================


def build_pipeline_graph() -> Any:
    """Build and compile the LangGraph StateGraph for pipeline generation.

    Returns a compiled graph that can be invoked with
    ``graph.invoke(initial_state)``.
    """
    graph = StateGraph(PipelineState)

    # ---- Add all nodes ----
    graph.add_node(NODE_INTENT, intent_node)
    graph.add_node(NODE_CLARIFY, clarify_node)
    graph.add_node(NODE_METADATA, metadata_node)
    graph.add_node(NODE_PLANNER, planner_node)
    graph.add_node(NODE_HOCON, hocon_generator_node)
    graph.add_node(NODE_QUALITY, quality_checker_node)
    graph.add_node(NODE_AUTOFIX, autofix_node)
    graph.add_node(NODE_FREEZE, freeze_node)

    # ---- Entry: START -> intent ----
    graph.add_edge(START, NODE_INTENT)

    # ---- intent -> conditional ----
    graph.add_conditional_edges(
        NODE_INTENT,
        _route_after_intent,
        {
            NODE_CLARIFY: NODE_CLARIFY,
            NODE_METADATA: NODE_METADATA,
            END: END,
        },
    )

    # ---- clarify -> conditional ----
    graph.add_conditional_edges(
        NODE_CLARIFY,
        _route_after_clarify,
        {
            NODE_METADATA: NODE_METADATA,
            END: END,
        },
    )

    # ---- metadata -> planner ----
    graph.add_conditional_edges(
        NODE_METADATA,
        _route_after_metadata,
        {
            NODE_PLANNER: NODE_PLANNER,
            END: END,
        },
    )

    # ---- planner -> hocon ----
    graph.add_conditional_edges(
        NODE_PLANNER,
        _route_after_planner,
        {
            NODE_HOCON: NODE_HOCON,
            END: END,
        },
    )

    # ---- hocon -> quality ----
    graph.add_conditional_edges(
        NODE_HOCON,
        _route_after_hocon,
        {
            NODE_QUALITY: NODE_QUALITY,
            END: END,
        },
    )

    # ---- quality -> conditional ----
    graph.add_conditional_edges(
        NODE_QUALITY,
        _route_after_quality,
        {
            NODE_FREEZE: NODE_FREEZE,
            NODE_AUTOFIX: NODE_AUTOFIX,
            END: END,
        },
    )

    # ---- autofix -> conditional (loop back to quality or END) ----
    graph.add_conditional_edges(
        NODE_AUTOFIX,
        _route_after_autofix,
        {
            NODE_QUALITY: NODE_QUALITY,
            NODE_FREEZE: NODE_FREEZE,
            END: END,
        },
    )

    # ---- freeze -> END ----
    graph.add_edge(NODE_FREEZE, END)

    return graph.compile()


# =====================================================================
# Module-level compiled graph singleton
# =====================================================================

pipeline_graph = build_pipeline_graph()


__all__ = [
    "NODE_AUTOFIX",
    "NODE_CLARIFY",
    "NODE_FREEZE",
    "NODE_HOCON",
    "NODE_INTENT",
    "NODE_METADATA",
    "NODE_PLANNER",
    "NODE_QUALITY",
    "build_pipeline_graph",
    "freeze_node",
    "pipeline_graph",
]
