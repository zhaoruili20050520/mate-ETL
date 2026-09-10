"""HOCON generator node -- produce SeaTunnel HOCON from an EtlPlan.

This is the fifth node in the LangGraph chain (after planner).
It takes the ``EtlPlan`` from the state and calls the LLM service
to generate a ``GeneratedHocon`` containing the SeaTunnel executable
HOCON configuration text.

The generated HOCON is later validated by the ``quality_checker``
node for syntax, field mappings, and security rules.

If the LLM call fails or the response cannot be parsed into a valid
GeneratedHocon, the node sets ``error`` and ``status=error``.
"""

from __future__ import annotations

import logging
from typing import Any

from etl_agent.agents.state import (
    STATUS_ERROR,
    STATUS_GENERATING,
    PipelineState,
)
from etl_agent.schemas.generation import EtlPlan
from etl_agent.services.llm_service import LLMError, get_llm_service

logger = logging.getLogger(__name__)


def hocon_generator_node(state: PipelineState) -> dict[str, Any]:
    """Generate SeaTunnel HOCON from the EtlPlan.

    This node:
    1. Reads ``etl_plan`` from the state.
    2. Calls ``LLMService.generate_hocon`` with the EtlPlan.
    3. Sets ``hocon_config`` in the state.

    Returns a partial state dict for LangGraph to merge.
    """
    etl_plan: EtlPlan | None = state.get("etl_plan")
    if etl_plan is None:
        return {"error": "etl_plan is not set", "status": STATUS_ERROR}

    try:
        llm = get_llm_service()
        generated = llm.generate_hocon(etl_plan)
    except LLMError as exc:
        logger.error("HOCON generator node: LLM call failed: %s", exc)
        return {"error": f"HOCON generation failed: {exc}", "status": STATUS_ERROR}
    except Exception as exc:
        logger.error("HOCON generator node: unexpected error: %s", exc)
        return {"error": f"HOCON generation error: {exc}", "status": STATUS_ERROR}

    logger.info(
        "HOCON generator node: generated config, source_table_name=%s, "
        "result_table_name=%s, length=%d",
        generated.source_table_name,
        generated.result_table_name,
        len(generated.config_text),
    )

    return {
        "hocon_config": generated,
        "status": STATUS_GENERATING,
    }


__all__ = ["hocon_generator_node"]
