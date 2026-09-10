"""Clarify node -- handle missing-parameter interruption and resume.

This node is invoked when the ``intent`` node detects that required
fields are missing from the user's request.  It has two modes:

1. **Interrupt**: when ``clarification_needed=True`` and no answers
   are yet provided, the node simply passes through the
   clarification questions so the API layer can return them to
   the user.  The graph stops here (conditional edge routes to END).

2. **Resume**: when the user has provided answers (via
   ``POST /pipelines/{id}/clarify``), the node merges the answers
   into the parsed requirement dict, re-checks for missing fields,
   and either:
   - Sets ``clarification_needed=False`` if all fields are now
     present, allowing the graph to continue to ``metadata``.
   - Re-populates ``clarification_questions`` if some answers
     are still missing or invalid.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from etl_agent.agents.nodes.intent import _build_clarification_questions
from etl_agent.agents.state import (
    STATUS_DRAFT,
    STATUS_NEEDS_CLARIFICATION,
    PipelineState,
)
from etl_agent.schemas.generation import PipelineRequirement, SyncMode

logger = logging.getLogger(__name__)


def _apply_answers(
    parsed: dict[str, Any],
    answers: dict[str, str],
) -> dict[str, Any]:
    """Apply user-provided answers to the parsed requirement dict.

    Answers come as ``{field_name: string_value}``.  This function
    converts them to the right type:
    - ``sync_mode`` -> ``SyncMode`` value string
    - ``primary_keys`` -> ``list[str]`` (split on comma)
    - other fields -> string as-is
    """
    for field_name, answer in answers.items():
        answer = answer.strip()
        if not answer:
            continue

        if field_name == "sync_mode":
            # Normalize to lowercase enum value
            parsed[field_name] = answer.lower()
        elif field_name == "primary_keys":
            # Split on comma and strip each key
            parsed[field_name] = [k.strip() for k in answer.split(",") if k.strip()]
        else:
            parsed[field_name] = answer

    return parsed


def _get_parsed_from_requirement(
    requirement: PipelineRequirement | None,
) -> dict[str, Any]:
    """Convert a PipelineRequirement back to a plain dict for re-validation."""
    if requirement is None:
        return {}
    d = requirement.model_dump()
    # Convert UUIDs back to strings for consistency
    for key in ("source_connection_id", "target_connection_id"):
        if key in d and d[key] is not None:
            d[key] = str(d[key])
    # Convert SyncMode to plain string
    if "sync_mode" in d and d["sync_mode"] is not None:
        d["sync_mode"] = (
            d["sync_mode"].value if hasattr(d["sync_mode"], "value") else str(d["sync_mode"])
        )
    return d


def clarify_node(state: PipelineState) -> dict[str, Any]:
    """Handle clarification interrupt and resume.

    Behaviour:
    - If ``clarification_needed`` is True and ``answers`` is empty
      or missing, this is the initial interrupt -- pass through
      questions, do not continue.
    - If ``answers`` are present, apply them to the requirement,
      re-check for missing fields, and either continue or re-ask.

    Returns a partial state dict for LangGraph to merge.
    """
    clarification_needed = state.get("clarification_needed", False)
    answers = state.get("answers", {})

    if not clarification_needed:
        # No clarification needed -- this node is a no-op pass-through.
        return {}

    if not answers:
        # Initial interrupt: questions already set by intent node.
        # Just signal that we need to wait for user input.
        logger.info(
            "Clarify node: waiting for user answers to %d questions",
            len(state.get("clarification_questions", [])),
        )
        return {"status": STATUS_NEEDS_CLARIFICATION}

    # Resume mode: start from the partial parsed intent stored by intent node.
    parsed: dict[str, Any] = dict(state.get("parsed_intent", {}))

    # Fallback: if parsed_intent is missing (shouldn't happen), try
    # to reconstruct from a PipelineRequirement in state.
    if not parsed:
        parsed = _get_parsed_from_requirement(state.get("requirement"))

    # If still no parsed dict, reconstruct minimal from state fields.
    if not parsed:
        parsed = {
            "natural_language": state.get("natural_language", ""),
            "source_connection_id": state.get("source_connection_id", ""),
            "target_connection_id": state.get("target_connection_id", ""),
        }

    # Apply the LLM-parsed hints too (stored from the original intent run)
    # In the initial pass, intent_node may not have set requirement
    # because it was missing fields.  We need the partial parsed dict.
    # The answers from the user fill those gaps.
    parsed = _apply_answers(parsed, answers)

    # Re-check for missing fields
    questions = _build_clarification_questions(parsed)

    if questions:
        logger.info(
            "Clarify node: %d questions still unanswered after resume",
            len(questions),
        )
        return {
            "clarification_needed": True,
            "clarification_questions": questions,
            "status": STATUS_NEEDS_CLARIFICATION,
        }

    # All fields present -- build the PipelineRequirement
    try:
        req = PipelineRequirement(
            natural_language=parsed.get("natural_language", state.get("natural_language", "")),
            source_connection_id=uuid.UUID(str(parsed.get("source_connection_id", ""))),
            target_connection_id=uuid.UUID(str(parsed.get("target_connection_id", ""))),
            sync_mode=SyncMode(parsed["sync_mode"]),
            source_table=parsed.get("source_table"),
            target_table=parsed.get("target_table"),
            source_schema=parsed.get("source_schema"),
            target_schema=parsed.get("target_schema"),
            primary_keys=parsed.get("primary_keys", []),
            watermark_column=parsed.get("watermark_column"),
            extra_params=parsed.get("extra_params", {}),
        )
    except (KeyError, ValueError) as exc:
        logger.error("Clarify node: failed to build requirement after answers: %s", exc)
        return {"error": f"Invalid requirement after clarification: {exc}", "status": "error"}

    logger.info("Clarify node: all answers provided, continuing to metadata")
    return {
        "requirement": req,
        "clarification_needed": False,
        "clarification_questions": [],
        "status": STATUS_DRAFT,
    }


__all__ = ["clarify_node"]
