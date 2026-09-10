"""L1 Simulated Fault -- inject fault scenarios and verify the quality gate catches them.

L1 tests the quality_checker_node directly by constructing EtlPlan
instances with deliberate faults (missing PK, missing watermark, sensitive
field without masking, etc.) and verifying that the quality checker
produces the expected error codes.

This level measures:
- Interception rate: fraction of faults correctly caught
- False-positive rate: fraction of valid inputs incorrectly rejected
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from etl_agent.benchmark.datasets import L1_CASES
from etl_agent.benchmark.evaluators import evaluate_l1_case
from etl_agent.benchmark.schemas import CaseResult, LevelResult
from etl_agent.schemas.generation import (
    CleaningRule,
    EtlPlan,
    FieldMapping,
    GeneratedHocon,
    QualityContract,
    SourceConfig,
    SyncMode,
    SyncStrategy,
    TargetConfig,
)

logger = logging.getLogger(__name__)

_SOURCE_CONN_ID = uuid.uuid4()
_TARGET_CONN_ID = uuid.uuid4()


def _build_mock_metadata_for_l1(
    sensitive_fields: list[str] | None = None,
) -> dict[str, Any]:
    """Build mock metadata with optional sensitive fields.

    The quality checker reads source/target column names and types
    from metadata_context.  We provide a consistent schema that
    includes sensitive fields (phone, email) when needed.
    """
    columns = [
        {"name": "id", "type": "int", "nullable": False},
        {"name": "username", "type": "varchar(64)", "nullable": False},
    ]
    if sensitive_fields:
        for sf in sensitive_fields:
            col_type = "varchar(32)" if sf == "phone" else "varchar(255)"
            columns.append({"name": sf, "type": col_type, "nullable": True})
    else:
        columns.append({"name": "data", "type": "varchar(255)", "nullable": True})
    columns.append({"name": "updated_at", "type": "datetime", "nullable": True})

    return {
        "source": {
            "selected_table": {
                "table_name": "test_table",
                "schema_name": "public",
                "columns_info": columns,
            },
            "all_tables": [
                {"table_name": "test_table", "schema_name": "public"},
            ],
        },
        "target": {
            "selected_table": {
                "table_name": "test_table",
                "schema_name": "public",
                "columns_info": columns,
            },
            "all_tables": [
                {"table_name": "test_table", "schema_name": "public"},
            ],
        },
    }


def _build_faulty_plan(case: dict[str, Any]) -> tuple[EtlPlan, dict[str, Any]]:
    """Construct an EtlPlan with the fault specified in the test case.

    Returns (plan, metadata_context) ready to be passed to
    quality_checker_node.
    """
    fault = case.get("fault", {})
    inject = fault.get("inject", {})
    sync_mode_str = inject.get("sync_mode", "full")
    sync_mode = SyncMode(sync_mode_str)
    primary_keys = inject.get("primary_keys", ["id"])
    watermark = inject.get("watermark_column")
    sensitive_fields = inject.get("sensitive_fields", [])
    omit_quality = inject.get("omit_quality_contract", False)
    omit_dead_letter = inject.get("omit_dead_letter", False)
    omit_shadow = inject.get("omit_shadow_table", False)
    has_cleaning_rules = inject.get("has_cleaning_rules", False)

    # Build field mappings
    mappings = [
        FieldMapping(
            source_field="id",
            target_field="id",
            source_type="int",
            target_type="int",
            transform=None,
        ),
        FieldMapping(
            source_field="username",
            target_field="username",
            source_type="varchar",
            target_type="varchar",
            transform=None,
        ),
    ]
    for sf in sensitive_fields:
        # If this is a sensitive-field-plain fault, map WITHOUT transform
        mappings.append(
            FieldMapping(
                source_field=sf,
                target_field=sf,
                source_type="varchar",
                target_type="varchar",
                transform=None,  # No masking = fault
            )
        )

    # Build cleaning rules
    cleaning_rules = []
    if has_cleaning_rules:
        cleaning_rules.append(
            CleaningRule(
                field="username",
                rule="not_null",
            )
        )

    # Build quality contract
    quality_contract = None
    if not omit_quality:
        qc_kwargs: dict[str, Any] = {
            "max_error_ratio": 0.01,
        }
        if not omit_dead_letter:
            qc_kwargs["dead_letter_table"] = "test_dead_letter"
            qc_kwargs["dead_letter_schema"] = "public"
        else:
            # Missing dead_letter_table
            qc_kwargs["dead_letter_table"] = ""
            qc_kwargs["dead_letter_schema"] = "public"
        quality_contract = QualityContract(**qc_kwargs)
    elif has_cleaning_rules:
        # Has cleaning rules but no quality contract
        quality_contract = None
    else:
        quality_contract = QualityContract(
            max_error_ratio=0.01,
            dead_letter_table="test_dead_letter",
            dead_letter_schema="public",
        )

    # Build target config
    target_kwargs: dict[str, Any] = {
        "connection_id": _TARGET_CONN_ID,
        "database_name": "testdb",
        "schema_name": "public",
        "table_name": "test_table",
    }
    if not omit_shadow and sync_mode == SyncMode.full:
        target_kwargs["shadow_table"] = "test_table_shadow"

    plan = EtlPlan(
        name="l1-fault-test",
        description=f"L1 fault case: {case['description']}",
        source=SourceConfig(
            connection_id=_SOURCE_CONN_ID,
            database_name="testdb",
            schema_name="public",
            table_name="test_table",
        ),
        target=TargetConfig(**target_kwargs),
        field_mappings=mappings,
        cleaning_rules=cleaning_rules,
        sync_strategy=SyncStrategy(
            mode=sync_mode,
            primary_keys=primary_keys,
            watermark_column=watermark,
        ),
        quality_contract=quality_contract,
    )

    metadata = _build_mock_metadata_for_l1(
        sensitive_fields=sensitive_fields if sensitive_fields else None
    )

    return plan, metadata


def _build_hocon_placeholder() -> GeneratedHocon:
    """Build a minimal valid HOCON for the quality checker.

    The quality checker also validates HOCON syntax, so we need
    a structurally valid HOCON string.
    """
    return GeneratedHocon(
        config_text="""
        source {
            Jdbc {
                url = "jdbc:mysql://localhost:3307/testdb"
                table = "test_table"
            }
        }
        sink {
            Jdbc {
                url = "jdbc:doris://localhost:9030/testdb"
                table = "test_table"
            }
        }
        """,
        source_table_name="test_table",
        result_table_name="test_table",
    )


def run_l1_suite(quality_checker_fn: Any = None) -> LevelResult:
    """Run all L1 fault-injection cases through the quality checker.

    If ``quality_checker_fn`` is provided, uses it directly; otherwise
    imports ``quality_checker_node`` from the agents module.

    Returns a :class:`LevelResult` with interception and
    false-positive rates.
    """
    if quality_checker_fn is None:
        from etl_agent.agents.nodes.quality_checker import quality_checker_node

        quality_checker_fn = quality_checker_node

    case_results: list[CaseResult] = []
    total_latency = 0.0

    for case in L1_CASES:
        case_id = case["case_id"]
        logger.info("L1 running case %s: %s", case_id, case["description"])

        start = time.monotonic()
        try:
            plan, metadata = _build_faulty_plan(case)
            hocon = _build_hocon_placeholder()

            state = {
                "etl_plan": plan,
                "hocon_config": hocon,
                "metadata_context": metadata,
            }

            result_state = quality_checker_fn(state)
            latency_ms = (time.monotonic() - start) * 1000

            issues = result_state.get("validation_issues", [])

            # Extract issue codes
            issue_codes = []
            for issue in issues:
                if hasattr(issue, "code"):
                    issue_codes.append(
                        str(issue.code.value if hasattr(issue.code, "value") else issue.code)
                    )
                elif hasattr(issue, "model_dump"):
                    d = issue.model_dump()
                    code = d.get("code", "")
                    if hasattr(code, "value"):
                        code = code.value
                    issue_codes.append(str(code))
                elif isinstance(issue, dict):
                    code = issue.get("code", "")
                    if hasattr(code, "value"):
                        code = code.value
                    issue_codes.append(str(code))

            # Determine if intercepted (has error-severity issues)
            has_errors = False
            for issue in issues:
                severity = None
                if hasattr(issue, "severity"):
                    severity = issue.severity
                    if hasattr(severity, "value"):
                        severity = severity.value
                elif isinstance(issue, dict):
                    severity = issue.get("severity", "")
                    if hasattr(severity, "value"):
                        severity = severity.value
                if severity == "error":
                    has_errors = True
                    break

            intercepted = has_errors
            result = evaluate_l1_case(case, intercepted, issue_codes)

        except Exception as exc:
            latency_ms = (time.monotonic() - start) * 1000
            logger.exception("L1 case %s failed: %s", case_id, exc)
            result = CaseResult(
                case_id=case_id,
                passed=False,
                score=0.0,
                message=f"Exception: {exc}",
                details={"exception": str(exc), "intercepted": False},
            )

        result.details["latency_ms"] = round(latency_ms, 1)
        total_latency += latency_ms
        case_results.append(result)

    total = len(case_results)
    passed = sum(1 for r in case_results if r.passed)
    failed = total - passed
    avg_latency = total_latency / total if total > 0 else 0.0

    # Calculate interception and false-positive rates
    should_intercept_total = 0
    should_intercept_passed = 0
    should_not_intercept_total = 0
    should_not_intercept_failed = 0

    for i, r in enumerate(case_results):
        case = L1_CASES[i]
        expect = case.get("expect", {})
        should_intercept = expect.get("should_intercept", False)

        if should_intercept:
            should_intercept_total += 1
            if r.passed:
                should_intercept_passed += 1
        else:
            should_not_intercept_total += 1
            if not r.passed:
                should_not_intercept_failed += 1

    interception_rate = (
        should_intercept_passed / should_intercept_total if should_intercept_total > 0 else 0.0
    )
    false_positive_rate = (
        should_not_intercept_failed / should_not_intercept_total
        if should_not_intercept_total > 0
        else 0.0
    )

    return LevelResult(
        level="L1",
        description="Simulated fault -- quality gate interception rate",
        total_cases=total,
        passed_cases=passed,
        failed_cases=failed,
        accuracy_rate=passed / total if total > 0 else 0.0,
        interception_rate=interception_rate,
        false_positive_rate=false_positive_rate,
        avg_latency_ms=round(avg_latency, 1),
        case_results=case_results,
    )


__all__ = ["run_l1_suite"]
