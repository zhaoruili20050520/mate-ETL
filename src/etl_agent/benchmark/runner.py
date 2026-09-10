"""Benchmark runner -- orchestrates L0, L1, L2 evaluation and produces a report.

Usage::

    from etl_agent.benchmark.runner import BenchmarkRunner

    runner = BenchmarkRunner()
    report = runner.run_all()
    print(runner.format_report(report))
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from etl_agent.benchmark.l0_static import run_l0_suite
from etl_agent.benchmark.l1_fault import run_l1_suite
from etl_agent.benchmark.l2_chain import run_l2_suite
from etl_agent.benchmark.schemas import BenchmarkReport, LevelResult

logger = logging.getLogger(__name__)


class BenchmarkRunner:
    """Orchestrates the three-tier benchmark evaluation.

    The runner caches the last report in memory so the API endpoint
    can serve it without re-running the benchmark.
    """

    def __init__(self) -> None:
        self._last_report: BenchmarkReport | None = None

    def run_all(self) -> BenchmarkReport:
        """Run all three levels and return a complete report.

        L0 and L1 always run (they don't need infrastructure).
        L2 probes Docker connectivity and skips if unavailable.
        """
        logger.info("Benchmark: starting full evaluation run")

        levels: list[LevelResult] = []

        # L0: Static injection (always runs)
        logger.info("Benchmark: running L0 static injection suite")
        l0_result = run_l0_suite()
        levels.append(l0_result)

        # L1: Simulated fault (always runs)
        logger.info("Benchmark: running L1 simulated fault suite")
        l1_result = run_l1_suite()
        levels.append(l1_result)

        # L2: Real chain (may skip)
        logger.info("Benchmark: running L2 real chain suite")
        l2_result = run_l2_suite()
        levels.append(l2_result)

        # Calculate overall metrics
        # Overall accuracy: weighted average of L0 and L1 accuracy
        # (L2 is skipped without infra, so it doesn't count)
        l0 = l0_result
        l1 = l1_result
        l2 = l2_result

        total_non_skipped = l0.total_cases + l1.total_cases
        passed_non_skipped = l0.passed_cases + l1.passed_cases
        overall_accuracy = passed_non_skipped / total_non_skipped if total_non_skipped > 0 else 0.0
        overall_interception = l1.interception_rate

        # Build summary text
        summary_parts = [
            f"L0: {l0.passed_cases}/{l0.total_cases} passed (accuracy={l0.accuracy_rate:.1%})",
            f"L1: {l1.passed_cases}/{l1.total_cases} passed "
            f"(interception={l1.interception_rate:.1%}, "
            f"false_positive={l1.false_positive_rate:.1%})",
        ]
        if l2.skipped_cases > 0:
            summary_parts.append(
                f"L2: {l2.skipped_cases}/{l2.total_cases} skipped (infra unavailable)"
            )
        else:
            summary_parts.append(f"L2: {l2.passed_cases}/{l2.total_cases} passed")

        report = BenchmarkReport(
            version="1.0",
            timestamp=datetime.now(UTC).isoformat(),
            overall_accuracy=overall_accuracy,
            overall_interception=overall_interception,
            levels=levels,
            summary=" | ".join(summary_parts),
        )

        self._last_report = report
        logger.info("Benchmark: complete. %s", report.summary)
        return report

    @property
    def last_report(self) -> BenchmarkReport | None:
        """Return the most recent benchmark report, or None if not run yet."""
        return self._last_report

    def format_report(self, report: BenchmarkReport | None = None) -> str:
        """Format a benchmark report as a human-readable string."""
        if report is None:
            report = self._last_report
        if report is None:
            return "No benchmark report available. Run runner.run_all() first."

        lines: list[str] = [
            "=" * 60,
            "ETL-Agent Benchmark Report",
            "=" * 60,
            f"Version:     {report.version}",
            f"Timestamp:  {report.timestamp}",
            f"Overall Accuracy:     {report.overall_accuracy:.1%}",
            f"Overall Interception: {report.overall_interception:.1%}",
            "",
        ]

        for level in report.levels:
            lines.extend(
                [
                    "-" * 50,
                    f"{level.level}: {level.description}",
                    "-" * 50,
                    f"  Total:   {level.total_cases}",
                    f"  Passed:  {level.passed_cases}",
                    f"  Failed:  {level.failed_cases}",
                    f"  Skipped: {level.skipped_cases}",
                    f"  Accuracy:      {level.accuracy_rate:.1%}",
                ]
            )
            if level.level == "L1":
                lines.append(f"  Interception:  {level.interception_rate:.1%}")
                lines.append(f"  False Positive: {level.false_positive_rate:.1%}")
            lines.append(f"  Avg Latency:   {level.avg_latency_ms:.1f} ms")
            lines.append("")

            for case in level.case_results:
                status = "PASS" if case.passed else "FAIL"
                if case.details.get("skipped"):
                    status = "SKIP"
                lines.append(f"    [{status}] {case.case_id}: {case.message}")

            lines.append("")

        lines.append("=" * 60)
        lines.append(report.summary)
        lines.append("=" * 60)

        return "\n".join(lines)


__all__ = ["BenchmarkRunner"]
