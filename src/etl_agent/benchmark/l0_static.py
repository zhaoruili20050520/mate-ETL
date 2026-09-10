"""L0 Static Injection -- run the LangGraph generation chain with mock metadata.

L0 tests the pipeline-generation chain (intent -> planner -> hocon ->
quality_checker) without requiring real database connections.  Each
test case provides:
- Natural-language input + structured hints
- Mock metadata context (source/target table profiles)

The evaluator checks that the generated EtlPlan and HOCON match the
expected fields defined in the test case.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from etl_agent.benchmark.datasets import L0_CASES
from etl_agent.benchmark.evaluators import evaluate_l0_case
from etl_agent.benchmark.schemas import CaseResult, LevelResult

logger = logging.getLogger(__name__)


def _build_mock_metadata(case: dict[str, Any]) -> dict[str, Any]:
    """Build a mock metadata_context for a test case.

    The metadata context mimics the structure produced by the
    ``metadata_node`` in the LangGraph chain.  It provides:
    - ``source.selected_table``: columns and types for the source table
    - ``target.selected_table``: columns and types for the target table
    - ``source.all_tables`` / ``target.all_tables``: list of available tables
    """
    hints = case.get("hints", {})
    source_table = hints.get("source_table", "orders")
    target_table = hints.get("target_table", source_table)

    # Build a generic column set based on the table name
    # This is intentionally simple -- L0 tests the graph mechanics,
    # not real schema fidelity.
    if source_table in ("orders", "benchmark_orders"):
        columns = [
            {"name": "id", "type": "int", "nullable": False},
            {"name": "customer_id", "type": "int", "nullable": False},
            {"name": "total", "type": "decimal(10,2)", "nullable": False},
            {"name": "status", "type": "varchar(50)", "nullable": True},
            {"name": "created_at", "type": "datetime", "nullable": False},
        ]
    elif source_table in ("users", "benchmark_users"):
        columns = [
            {"name": "id", "type": "int", "nullable": False},
            {"name": "username", "type": "varchar(64)", "nullable": False},
            {"name": "email", "type": "varchar(255)", "nullable": True},
            {"name": "phone", "type": "varchar(32)", "nullable": True},
            {"name": "updated_at", "type": "datetime", "nullable": True},
        ]
    elif source_table in ("products",):
        columns = [
            {"name": "id", "type": "int", "nullable": False},
            {"name": "product_name", "type": "varchar(255)", "nullable": False},
            {"name": "price", "type": "decimal(10,2)", "nullable": False},
            {"name": "stock", "type": "int", "nullable": True},
        ]
    elif source_table in ("customer",):
        columns = [
            {"name": "id", "type": "int", "nullable": False},
            {"name": "name", "type": "varchar(128)", "nullable": False},
            {"name": "phone", "type": "varchar(32)", "nullable": True},
            {"name": "email", "type": "varchar(255)", "nullable": True},
        ]
    elif source_table in ("order_items",):
        columns = [
            {"name": "order_id", "type": "int", "nullable": False},
            {"name": "item_id", "type": "int", "nullable": False},
            {"name": "quantity", "type": "int", "nullable": False},
            {"name": "price", "type": "decimal(10,2)", "nullable": False},
            {"name": "updated_at", "type": "datetime", "nullable": True},
        ]
    elif source_table in ("events",):
        columns = [
            {"name": "id", "type": "bigint", "nullable": False},
            {"name": "event_type", "type": "varchar(100)", "nullable": False},
            {"name": "payload", "type": "text", "nullable": True},
            {"name": "created_at", "type": "datetime", "nullable": False},
        ]
    elif source_table in ("payments",):
        columns = [
            {"name": "id", "type": "int", "nullable": False},
            {"name": "amount", "type": "decimal(10,2)", "nullable": False},
            {"name": "status", "type": "varchar(20)", "nullable": False},
            {"name": "created_at", "type": "datetime", "nullable": False},
        ]
    elif source_table in ("logs", "benchmark_bad_data"):
        columns = [
            {"name": "id", "type": "bigint", "nullable": False},
            {"name": "level", "type": "varchar(20)", "nullable": False},
            {"name": "message", "type": "text", "nullable": True},
            {"name": "created_at", "type": "datetime", "nullable": False},
        ]
    else:
        columns = [
            {"name": "id", "type": "int", "nullable": False},
            {"name": "data", "type": "varchar(255)", "nullable": True},
        ]

    return {
        "source": {
            "selected_table": {
                "table_name": source_table,
                "schema_name": "public",
                "columns_info": columns,
            },
            "all_tables": [
                {"table_name": source_table, "schema_name": "public"},
                {"table_name": "other_table", "schema_name": "public"},
            ],
        },
        "target": {
            "selected_table": {
                "table_name": target_table,
                "schema_name": "public",
                "columns_info": columns,
            },
            "all_tables": [
                {"table_name": target_table, "schema_name": "public"},
            ],
        },
    }


def _build_initial_state(case: dict[str, Any]) -> dict[str, Any]:
    """Build a PipelineState-like dict for the graph invoke.

    Instead of going through the intent + metadata nodes (which require
    LLM and DB), we skip directly to the planner by pre-populating
    the requirement and metadata_context.
    """
    hints = case.get("hints", {})
    source_table = hints.get("source_table", "orders")
    target_table = hints.get("target_table", source_table)

    requirement = {
        "natural_language": case["natural_language"],
        "source_connection_id": str(uuid.uuid4()),
        "target_connection_id": str(uuid.uuid4()),
        "sync_mode": hints.get("sync_mode", "full"),
        "source_table": source_table,
        "target_table": target_table,
        "primary_keys": hints.get("primary_keys", ["id"]),
        "watermark_column": hints.get("watermark_column"),
    }

    return {
        "requirement": requirement,
        "metadata_context": _build_mock_metadata(case),
    }


def run_l0_suite(graph: Any | None = None) -> LevelResult:
    """Run all L0 test cases through the LangGraph chain.

    If ``graph`` is provided, uses ``graph.invoke()`` to run the full
    chain.  If not, attempts to import and use the compiled pipeline_graph.

    Returns a :class:`LevelResult` with accuracy metrics.
    """
    if graph is None:
        from etl_agent.agents.graph import pipeline_graph

        graph = pipeline_graph

    case_results: list[CaseResult] = []
    total_latency = 0.0

    for case in L0_CASES:
        case_id = case["case_id"]
        logger.info("L0 running case %s: %s", case_id, case["description"])

        start = time.monotonic()
        try:
            initial_state = _build_initial_state(case)
            final_state = graph.invoke(initial_state)
            latency_ms = (time.monotonic() - start) * 1000

            etl_plan = final_state.get("etl_plan")
            hocon_config = final_state.get("hocon_config")
            hocon_text = hocon_config.config_text if hocon_config else None
            validation_issues = final_state.get("validation_issues", [])

            # Convert validation issues to dicts for evaluation
            val_errors = []
            for issue in validation_issues:
                if hasattr(issue, "model_dump"):
                    val_errors.append(issue.model_dump())
                elif isinstance(issue, dict):
                    val_errors.append(issue)

            result = evaluate_l0_case(case, etl_plan, hocon_text, val_errors)
        except Exception as exc:
            latency_ms = (time.monotonic() - start) * 1000
            logger.exception("L0 case %s failed: %s", case_id, exc)
            result = CaseResult(
                case_id=case_id,
                passed=False,
                score=0.0,
                message=f"Exception: {exc}",
                details={"exception": str(exc)},
            )

        result.details["latency_ms"] = round(latency_ms, 1)
        total_latency += latency_ms
        case_results.append(result)

    total = len(case_results)
    passed = sum(1 for r in case_results if r.passed)
    failed = total - passed
    avg_latency = total_latency / total if total > 0 else 0.0

    return LevelResult(
        level="L0",
        description="Static injection -- LangGraph generation chain accuracy",
        total_cases=total,
        passed_cases=passed,
        failed_cases=failed,
        accuracy_rate=passed / total if total > 0 else 0.0,
        avg_latency_ms=round(avg_latency, 1),
        case_results=case_results,
    )


__all__ = ["run_l0_suite"]
