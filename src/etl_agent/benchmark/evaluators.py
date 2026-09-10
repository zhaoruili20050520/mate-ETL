"""Scoring and evaluation functions for benchmark test cases.

- L0: plan accuracy scoring (compare EtlPlan fields against expectations)
- L1: interception rate and false-positive rate calculation
"""

from __future__ import annotations

from typing import Any

from etl_agent.benchmark.schemas import CaseResult

# =====================================================================
# L0 plan-accuracy evaluation
# =====================================================================


def evaluate_l0_case(
    case: dict[str, Any],
    etl_plan: Any | None,
    hocon_text: str | None,
    validation_errors: list[Any] | None,
) -> CaseResult:
    """Evaluate a single L0 test case against its expectations.

    Checks:
    1. EtlPlan was generated (``has_etl_plan``).
    2. HOCON was generated (``has_hocon``).
    3. Plan fields match expected values (sync_mode, source_table, etc.).
    4. HOCON contains expected substrings.
    5. No error-severity validation issues remain.

    Returns a :class:`CaseResult` with ``score`` (0.0-1.0) and details.
    """
    case_id = case["case_id"]
    expect = case.get("expect", {})
    details: dict[str, Any] = {}
    checks_passed = 0
    checks_total = 0

    # 1. EtlPlan existence
    checks_total += 1
    if expect.get("has_etl_plan"):
        if etl_plan is not None:
            checks_passed += 1
            details["has_etl_plan"] = True
        else:
            details["has_etl_plan"] = False
            details["etl_plan_error"] = "EtlPlan was not generated"
    else:
        checks_passed += 1

    # 2. HOCON existence
    checks_total += 1
    if expect.get("has_hocon"):
        if hocon_text and hocon_text.strip():
            checks_passed += 1
            details["has_hocon"] = True
        else:
            details["has_hocon"] = False
            details["hocon_error"] = "HOCON config was not generated"
    else:
        checks_passed += 1

    # 3. Plan field matching
    if etl_plan is not None:
        plan_dict = etl_plan if isinstance(etl_plan, dict) else etl_plan.model_dump()

        # sync_mode
        if "sync_mode" in expect:
            checks_total += 1
            actual_mode = plan_dict.get("sync_strategy", {}).get("mode", "")
            if actual_mode == expect["sync_mode"]:
                checks_passed += 1
            else:
                details["sync_mode_mismatch"] = f"expected={expect['sync_mode']}, got={actual_mode}"

        # source_table
        if "source_table" in expect:
            checks_total += 1
            actual_src = plan_dict.get("source", {}).get("table_name", "")
            if actual_src.lower() == expect["source_table"].lower():
                checks_passed += 1
            else:
                details["source_table_mismatch"] = (
                    f"expected={expect['source_table']}, got={actual_src}"
                )

        # target_table
        if "target_table" in expect:
            checks_total += 1
            actual_tgt = plan_dict.get("target", {}).get("table_name", "")
            if actual_tgt.lower() == expect["target_table"].lower():
                checks_passed += 1
            else:
                details["target_table_mismatch"] = (
                    f"expected={expect['target_table']}, got={actual_tgt}"
                )

        # watermark_column
        if "watermark_column" in expect:
            checks_total += 1
            actual_wm = plan_dict.get("sync_strategy", {}).get("watermark_column")
            if actual_wm and actual_wm.lower() == expect["watermark_column"].lower():
                checks_passed += 1
            else:
                details["watermark_mismatch"] = (
                    f"expected={expect['watermark_column']}, got={actual_wm}"
                )

        # primary_keys_contains
        if "primary_keys_contains" in expect:
            checks_total += 1
            actual_pks = {
                pk.lower()
                for pk in plan_dict.get("sync_strategy", {}).get("primary_keys", [])
            }
            expected_pks = {pk.lower() for pk in expect["primary_keys_contains"]}
            if expected_pks.issubset(actual_pks):
                checks_passed += 1
            else:
                details["primary_keys_mismatch"] = (
                    f"expected={sorted(expected_pks)}, got={sorted(actual_pks)}"
                )

    # 4. HOCON substring checks
    if hocon_text:
        for substring in expect.get("hocon_contains", []):
            checks_total += 1
            if substring.lower() in hocon_text.lower():
                checks_passed += 1
            else:
                details[f"hocon_missing_{substring}"] = f"HOCON does not contain '{substring}'"

    # 5. No error-severity validation issues
    if validation_errors is not None:
        checks_total += 1
        error_codes = [e for e in validation_errors if e.get("severity") == "error"]
        if not error_codes:
            checks_passed += 1
        else:
            details["validation_errors"] = [e.get("code", "") for e in error_codes]

    score = checks_passed / checks_total if checks_total > 0 else 0.0
    passed = score == 1.0

    return CaseResult(
        case_id=case_id,
        passed=passed,
        score=score,
        message=f"{checks_passed}/{checks_total} checks passed",
        details=details,
    )


# =====================================================================
# L1 interception-rate evaluation
# =====================================================================


def evaluate_l1_case(
    case: dict[str, Any],
    intercepted: bool,
    issue_codes: list[str],
) -> CaseResult:
    """Evaluate a single L1 fault-injection case.

    For ``should_intercept=True`` cases: pass if the quality gate
    caught the fault and produced at least one expected error code.

    For ``should_intercept=False`` cases (false-positive checks):
    pass if the quality gate did **not** raise any error-severity issues.
    """
    case_id = case["case_id"]
    expect = case.get("expect", {})
    should_intercept = expect.get("should_intercept", False)
    expected_codes = set(expect.get("expected_codes", []))
    actual_codes = {c.upper() for c in issue_codes}

    if should_intercept:
        if intercepted and (not expected_codes or expected_codes & actual_codes):
            return CaseResult(
                case_id=case_id,
                passed=True,
                score=1.0,
                message=f"Correctly intercepted. codes={sorted(actual_codes & expected_codes)}",
                details={
                    "intercepted": True,
                    "expected_codes": sorted(expected_codes),
                    "actual_codes": sorted(actual_codes),
                },
            )
        return CaseResult(
            case_id=case_id,
            passed=False,
            score=0.0,
            message=(
                f"Failed to intercept fault. expected={sorted(expected_codes)}, "
                f"actual={sorted(actual_codes)}"
            ),
            details={
                "intercepted": intercepted,
                "expected_codes": sorted(expected_codes),
                "actual_codes": sorted(actual_codes),
            },
        )

    # False-positive check: should NOT be intercepted
    if not intercepted:
        return CaseResult(
            case_id=case_id,
            passed=True,
            score=1.0,
            message="Correctly allowed (no false positive)",
            details={"intercepted": False, "actual_codes": sorted(actual_codes)},
        )
    return CaseResult(
        case_id=case_id,
        passed=False,
        score=0.0,
        message=f"False positive: valid input was rejected. codes={sorted(actual_codes)}",
        details={"intercepted": True, "actual_codes": sorted(actual_codes)},
    )


def calculate_l1_metrics(results: list[CaseResult]) -> tuple[float, float]:
    """Calculate interception rate and false-positive rate from L1 results.

    - Interception rate: among cases that should be intercepted,
      fraction correctly caught.
    - False-positive rate: among cases that should NOT be intercepted,
      fraction incorrectly rejected.

    Returns ``(interception_rate, false_positive_rate)``.
    """
    # We need to re-examine each result to know which group it belongs to.
    # This function is called from the runner which has the case definitions,
    # but here we infer from CaseResult details.
    should_intercept_total = 0
    should_intercept_passed = 0
    should_not_intercept_total = 0
    should_not_intercept_failed = 0

    for r in results:
        details = r.details or {}
        # If the case was expected to be intercepted, it will have
        # "expected_codes" in details (populated by evaluate_l1_case).
        expected_codes = details.get("expected_codes", [])

        if expected_codes:
            # This is a should_intercept case
            should_intercept_total += 1
            if r.passed:
                should_intercept_passed += 1
        else:
            # This is a should_not_intercept case
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

    return interception_rate, false_positive_rate


__all__ = [
    "calculate_l1_metrics",
    "evaluate_l0_case",
    "evaluate_l1_case",
]
