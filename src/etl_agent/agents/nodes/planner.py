"""Planner node -- generate a structured EtlPlan from requirement + metadata.

This is the fourth node in the LangGraph chain (after metadata).
It takes the ``PipelineRequirement`` and ``metadata_context`` from
the state and calls the LLM service to generate a structured
``EtlPlan``.

The EtlPlan is the central artifact of the generation chain:
- It contains source/target config, field mappings, cleaning rules,
  sync strategy, and quality contract.
- It is used by the ``hocon_generator`` node to produce SeaTunnel HOCON.
- It is validated by the ``quality_checker`` node against real metadata.

If the LLM call fails or the response cannot be parsed into a valid
EtlPlan, the node sets ``error`` and ``status=error``.
"""

from __future__ import annotations

import logging
from typing import Any

from etl_agent.agents.state import (
    STATUS_ERROR,
    STATUS_GENERATING,
    PipelineState,
)
from etl_agent.services.llm_service import LLMError, get_llm_service

logger = logging.getLogger(__name__)


def _requirement_to_dict(state: PipelineState) -> dict[str, Any]:
    """Convert the PipelineRequirement from state into a plain dict.

    The LLM service expects a JSON-serializable dict, not a Pydantic
    model.  We also strip the ``natural_language`` field to keep
    the prompt compact.
    """
    requirement = state.get("requirement")
    if requirement is None:
        return {}
    d = requirement.model_dump()
    # Convert UUIDs to strings for JSON serialization
    for key in ("source_connection_id", "target_connection_id"):
        if key in d and d[key] is not None:
            d[key] = str(d[key])
    # Convert SyncMode to plain string
    if "sync_mode" in d and d["sync_mode"] is not None:
        sm = d["sync_mode"]
        d["sync_mode"] = sm.value if hasattr(sm, "value") else str(sm)
    return d


def _extract_source_metadata(metadata_context: dict[str, Any]) -> dict[str, Any]:
    """Extract the source-side metadata for the LLM prompt.

    Returns the ``selected_table`` profile (the table the user wants
    to sync from) plus the list of all available tables for context.
    """
    source = metadata_context.get("source", {})
    return {
        "selected_table": source.get("selected_table", {}),
        "available_tables": [
            {"table_name": t["table_name"], "schema_name": t["schema_name"]}
            for t in source.get("all_tables", [])
        ],
    }


def _extract_target_metadata(metadata_context: dict[str, Any]) -> dict[str, Any]:
    """Extract the target-side metadata for the LLM prompt."""
    target = metadata_context.get("target", {})
    return {
        "selected_table": target.get("selected_table", {}),
        "available_tables": [
            {"table_name": t["table_name"], "schema_name": t["schema_name"]}
            for t in target.get("all_tables", [])
        ],
    }


def planner_node(state: PipelineState) -> dict[str, Any]:
    """Generate an EtlPlan from the requirement + metadata context.

    This node:
    1. Reads ``requirement`` and ``metadata_context`` from state.
    2. Converts the requirement to a plain dict for the LLM.
    3. Extracts source/target metadata profiles.
    4. Calls ``LLMService.generate_etl_plan`` to produce a structured plan.
    5. Sets ``etl_plan`` in the state.

    Returns a partial state dict for LangGraph to merge.
    """
    requirement_dict = _requirement_to_dict(state)
    if not requirement_dict:
        return {"error": "requirement is not set", "status": STATUS_ERROR}

    metadata_context = state.get("metadata_context")
    if not metadata_context:
        return {"error": "metadata_context is not set", "status": STATUS_ERROR}

    source_metadata = _extract_source_metadata(metadata_context)
    target_metadata = _extract_target_metadata(metadata_context)

    try:
        llm = get_llm_service()
        etl_plan = llm.generate_etl_plan(
            requirement=requirement_dict,
            source_metadata=source_metadata,
            target_metadata=target_metadata,
        )
    except LLMError as exc:
        logger.error("Planner node: LLM call failed: %s", exc)
        return {"error": f"Plan generation failed: {exc}", "status": STATUS_ERROR}
    except Exception as exc:
        logger.error("Planner node: unexpected error: %s", exc)
        return {"error": f"Plan generation error: {exc}", "status": STATUS_ERROR}

    logger.info(
        "Planner node: EtlPlan generated, name=%s, mappings=%d",
        etl_plan.name,
        len(etl_plan.field_mappings),
    )

    return {
        "etl_plan": etl_plan,
        "status": STATUS_GENERATING,
    }


__all__ = ["planner_node"]
