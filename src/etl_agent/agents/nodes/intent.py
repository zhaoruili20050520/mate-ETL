"""Intent node -- parse natural-language requirement into structured form.

This is the first node in the LangGraph pipeline-generation chain.
It takes the user's raw natural-language text and any structured
hints (source/target connection IDs, sync mode hints, etc.) and
produces a ``PipelineRequirement`` object.

If required fields cannot be parsed from the text, the node sets
``clarification_needed=True`` and populates
``clarification_questions`` so that the ``clarify`` node can
interrupt and ask the user.

Required fields that trigger clarification when missing:
- ``sync_mode`` (full or incremental)
- ``source_table`` (table name on the source connection)
- ``target_table`` (table name on the target connection)
- ``primary_keys`` (when sync_mode is incremental)
- ``watermark_column`` (when sync_mode is incremental)
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from etl_agent.agents.state import (
    STATUS_DRAFT,
    STATUS_NEEDS_CLARIFICATION,
    PipelineState,
)
from etl_agent.schemas.generation import PipelineRequirement, SyncMode
from etl_agent.services.llm_service import LLMError, get_llm_service

logger = logging.getLogger(__name__)

# Fields that are considered mandatory for a valid PipelineRequirement.
# If any of these are missing after intent parsing, clarification is triggered.
_REQUIRED_FIELDS: list[tuple[str, str]] = [
    ("sync_mode", "Please specify the sync mode: 'full' or 'incremental'."),
    ("source_table", "Please provide the source table name."),
    ("target_table", "Please provide the target table name."),
]

# Fields only required for incremental sync.
_INCREMENTAL_FIELDS: list[tuple[str, str]] = [
    ("primary_keys", "Please provide at least one primary key column for incremental sync."),
    (
        "watermark_column",
        "Please provide the watermark column (e.g. updated_at) for incremental sync.",
    ),
]


def _build_clarification_questions(
    parsed: dict[str, Any],
) -> list[dict[str, str]]:
    """Return a list of clarification questions for missing fields.

    Each question is ``{"field": "field_name", "question": "..."}``.
    """
    questions: list[dict[str, str]] = []

    for field_name, prompt in _REQUIRED_FIELDS:
        value = parsed.get(field_name)
        if value is None or (isinstance(value, str) and not value.strip()):
            if field_name in ("source_table", "target_table"):
                # These are nullable in PipelineRequirement but needed
                # for a meaningful generation -- ask the user.
                questions.append({"field": field_name, "question": prompt})
            elif field_name == "sync_mode":
                questions.append({"field": field_name, "question": prompt})

    # Incremental-specific fields
    sync_mode = parsed.get("sync_mode")
    if sync_mode == SyncMode.incremental.value:
        pk = parsed.get("primary_keys", [])
        if not pk:
            questions.append({"field": "primary_keys", "question": _INCREMENTAL_FIELDS[0][1]})
        wm = parsed.get("watermark_column")
        if not wm or (isinstance(wm, str) and not wm.strip()):
            questions.append({"field": "watermark_column", "question": _INCREMENTAL_FIELDS[1][1]})

    return questions


def _merge_hints(
    parsed: dict[str, Any],
    hints: dict[str, Any],
) -> dict[str, Any]:
    """Merge user-provided hints into the LLM-parsed result.

    Hints take precedence over LLM output -- the user's explicit
    choices always win over the model's inference.
    """
    for key, value in hints.items():
        if value is not None and value != "":
            if key == "primary_keys" and isinstance(value, list) and not value:
                continue  # empty list hint = no override
            parsed[key] = value
    return parsed


def intent_node(state: PipelineState) -> dict[str, Any]:
    """Parse the natural-language requirement into a PipelineRequirement.

    This node:
    1. Calls the LLM to parse ``natural_language`` into structured fields.
    2. Merges any user-provided hints from the state (source_table,
       target_table, sync_mode, primary_keys, watermark_column).
    3. Checks for missing required fields.
    4. If missing fields are found, sets ``clarification_needed=True``
       and populates ``clarification_questions``.
    5. Otherwise, constructs a ``PipelineRequirement`` and sets it.

    Returns a partial state dict to be merged by LangGraph.
    """
    nl = state.get("natural_language", "")
    if not nl:
        return {"error": "natural_language is required", "status": "error"}

    # Collect user-provided hints from the state
    hints: dict[str, Any] = {}
    for hint_key in (
        "sync_mode",
        "source_table",
        "target_table",
        "primary_keys",
        "watermark_column",
    ):
        val = state.get(hint_key)
        if val is not None:
            hints[hint_key] = val

    try:
        llm = get_llm_service()
        parsed = llm.parse_intent(nl)
    except LLMError as exc:
        logger.error("Intent parsing failed: %s", exc)
        return {"error": f"Intent parsing failed: {exc}", "status": "error"}

    # Merge hints (user explicit values override LLM inference)
    parsed = _merge_hints(parsed, hints)

    # Inject connection IDs and natural language from state so the
    # clarify node can build a PipelineRequirement from parsed_intent.
    parsed.setdefault("source_connection_id", state.get("source_connection_id", ""))
    parsed.setdefault("target_connection_id", state.get("target_connection_id", ""))
    parsed.setdefault("natural_language", nl)

    # Check for missing required fields
    questions = _build_clarification_questions(parsed)

    if questions:
        logger.info("Intent node: %d clarification questions needed", len(questions))
        return {
            "clarification_needed": True,
            "clarification_questions": questions,
            "parsed_intent": parsed,
            "status": STATUS_NEEDS_CLARIFICATION,
        }

    # All required fields present -- build PipelineRequirement
    try:
        requirement = PipelineRequirement(
            natural_language=nl,
            source_connection_id=uuid.UUID(state.get("source_connection_id", "")),
            target_connection_id=uuid.UUID(state.get("target_connection_id", "")),
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
        logger.error("Failed to build PipelineRequirement: %s", exc)
        return {"error": f"Invalid requirement: {exc}", "status": "error"}

    logger.info("Intent node: requirement parsed successfully, sync_mode=%s", requirement.sync_mode)
    return {
        "requirement": requirement,
        "clarification_needed": False,
        "clarification_questions": [],
        "status": STATUS_DRAFT,
    }


__all__ = ["intent_node"]
