"""Benchmark API routes.

Provides a single endpoint to run the three-tier benchmark suite
and retrieve the report.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from etl_agent.benchmark.runner import BenchmarkRunner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/benchmark", tags=["benchmark"])

# Module-level singleton so the API can serve the last report without re-running
_runner = BenchmarkRunner()


@router.get("/")
def get_benchmark_report(run: bool = False) -> dict[str, object]:
    """Return the benchmark report.

    By default returns the last cached report (or runs if none exists).
    Set ``?run=true`` to force a fresh evaluation.
    """
    if run or _runner.last_report is None:
        logger.info("Benchmark: starting fresh evaluation via API")
        try:
            report = _runner.run_all()
        except Exception as exc:
            logger.exception("Benchmark run failed: %s", exc)
            raise HTTPException(
                status_code=500,
                detail=f"Benchmark evaluation failed: {exc}",
            ) from exc
    else:
        report = _runner.last_report

    return report.model_dump()


@router.get("/text")
def get_benchmark_text() -> str:
    """Return the benchmark report as plain text."""
    if _runner.last_report is None:
        try:
            _runner.run_all()
        except Exception as exc:
            logger.exception("Benchmark run failed: %s", exc)
            raise HTTPException(
                status_code=500,
                detail=f"Benchmark evaluation failed: {exc}",
            ) from exc
    return _runner.format_report()


__all__ = ["router"]
