"""L2 Real Chain -- end-to-end tests with real database connections.

L2 tests the full pipeline: NL generation -> freeze -> commit ->
SeaTunnel execution -> quality gate -> atomic swap -> verification.
Each case requires Docker infrastructure (MySQL + Doris + SeaTunnel).

If infrastructure is unavailable, all L2 cases are marked as skipped.
"""

from __future__ import annotations

import logging
import time

from etl_agent.benchmark.datasets import L2_CASES
from etl_agent.benchmark.schemas import CaseResult, LevelResult

logger = logging.getLogger(__name__)


def _check_infra() -> bool:
    """Check if Docker infrastructure is available.

    Tests MySQL (3307) and Doris (9030) connectivity.
    """
    import socket

    mysql_ok = False
    doris_ok = False

    try:
        s = socket.create_connection(("localhost", 3307), timeout=2)
        s.close()
        mysql_ok = True
    except Exception:
        pass

    try:
        s = socket.create_connection(("localhost", 9030), timeout=2)
        s.close()
        doris_ok = True
    except Exception:
        pass

    if not (mysql_ok and doris_ok):
        logger.warning(
            "L2 infra check: mysql=%s doris=%s -- L2 cases will be skipped",
            mysql_ok,
            doris_ok,
        )
    return mysql_ok and doris_ok


def _check_seatunnel() -> bool:
    """Check if SeaTunnel REST API is reachable."""
    import socket

    try:
        s = socket.create_connection(("localhost", 5801), timeout=2)
        s.close()
        return True
    except Exception:
        logger.warning("SeaTunnel not reachable -- L2 cases will be skipped")
        return False


def run_l2_suite(infra_available: bool | None = None) -> LevelResult:
    """Run all L2 real-chain test cases.

    If ``infra_available`` is explicitly provided, uses that value.
    Otherwise, probes MySQL + Doris + SeaTunnel connectivity.

    If infrastructure is unavailable, all cases are marked as skipped
    (not failed) and accuracy_rate is 0.0.
    """
    if infra_available is None:
        infra_available = _check_infra() and _check_seatunnel()

    case_results: list[CaseResult] = []

    for case in L2_CASES:
        case_id = case["case_id"]
        logger.info("L2 running case %s: %s", case_id, case["description"])

        if not infra_available:
            case_results.append(
                CaseResult(
                    case_id=case_id,
                    passed=False,
                    score=0.0,
                    message="Skipped: Docker infrastructure not available",
                    details={"skipped": True, "reason": "infra_unavailable"},
                )
            )
            continue

        # L2 real-chain execution would require a full integration test
        # with the API + Celery workers running. For the benchmark module,
        # we mark these as requiring manual execution.
        start = time.monotonic()
        try:
            # In a real run, this would:
            # 1. Call POST /api/v1/pipelines/generate
            # 2. Freeze -> Create Preparation -> Approve -> Commit
            # 3. Wait for SeaTunnel job to finish
            # 4. Check quality report + publish attempt
            # 5. Verify target table data

            # For now, mark as requiring manual execution
            latency_ms = (time.monotonic() - start) * 1000
            case_results.append(
                CaseResult(
                    case_id=case_id,
                    passed=False,
                    score=0.0,
                    message="L2 requires manual execution via API + Celery workers",
                    details={
                        "skipped": True,
                        "reason": "requires_manual_execution",
                        "latency_ms": round(latency_ms, 1),
                    },
                )
            )
        except Exception as exc:
            logger.exception("L2 case %s failed: %s", case_id, exc)
            case_results.append(
                CaseResult(
                    case_id=case_id,
                    passed=False,
                    score=0.0,
                    message=f"Exception: {exc}",
                    details={"exception": str(exc)},
                )
            )

    total = len(case_results)
    passed = sum(1 for r in case_results if r.passed)
    failed = total - passed
    skipped = sum(1 for r in case_results if r.details.get("skipped"))

    return LevelResult(
        level="L2",
        description="Real chain -- end-to-end MySQL to Doris sync",
        total_cases=total,
        passed_cases=passed,
        failed_cases=failed,
        skipped_cases=skipped,
        accuracy_rate=passed / total if total > 0 else 0.0,
        avg_latency_ms=0.0,
        case_results=case_results,
    )


__all__ = ["run_l2_suite"]
