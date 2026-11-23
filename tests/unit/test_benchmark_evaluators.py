"""Tests for the benchmark L1 fault-injection evaluation.

The quality gate intentionally distinguishes blocking findings
(``severity=error``) from advisory ones (``severity=warning``).
The evaluator must reflect that distinction, otherwise warning-level
cases look like "the gate failed to intercept the fault".
"""

from __future__ import annotations

from etl_agent.benchmark.evaluators import evaluate_l1_case


def _case(*, should_intercept: bool, codes: list[str]) -> dict:
    return {
        "case_id": "L1-TEST",
        "expect": {
            "should_intercept": should_intercept,
            "expected_codes": codes,
        },
    }


def test_blocking_fault_is_intercepted() -> None:
    result = evaluate_l1_case(
        _case(should_intercept=True, codes=["MISSING_PRIMARY_KEY"]),
        intercepted=True,
        issue_codes=["MISSING_PRIMARY_KEY"],
    )

    assert result.passed is True
    assert "Correctly intercepted" in result.message


def test_blocking_fault_missed_fails() -> None:
    result = evaluate_l1_case(
        _case(should_intercept=True, codes=["MISSING_PRIMARY_KEY"]),
        intercepted=False,
        issue_codes=[],
    )

    assert result.passed is False


def test_warning_level_fault_passes_without_blocking() -> None:
    """A warning-level code must count as detected, not as a failure."""
    result = evaluate_l1_case(
        _case(should_intercept=False, codes=["MISSING_WATERMARK"]),
        intercepted=False,
        issue_codes=["MISSING_WATERMARK"],
    )

    assert result.passed is True
    assert "warned without blocking" in result.message


def test_warning_level_fault_missing_code_fails() -> None:
    result = evaluate_l1_case(
        _case(should_intercept=False, codes=["MISSING_WATERMARK"]),
        intercepted=False,
        issue_codes=[],
    )

    assert result.passed is False


def test_valid_input_without_errors_is_not_a_false_positive() -> None:
    result = evaluate_l1_case(
        _case(should_intercept=False, codes=[]),
        intercepted=False,
        issue_codes=[],
    )

    assert result.passed is True
    assert "no false positive" in result.message


def test_valid_input_rejected_is_a_false_positive() -> None:
    result = evaluate_l1_case(
        _case(should_intercept=False, codes=[]),
        intercepted=True,
        issue_codes=["FIELD_NOT_FOUND"],
    )

    assert result.passed is False
    assert "False positive" in result.message
