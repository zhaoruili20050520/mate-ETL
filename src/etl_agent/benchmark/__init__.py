"""Benchmark evaluation module for the ETL-Agent pipeline.

Three-tier evaluation framework:

- **L0 (Static Injection)**: Pre-defined test cases with mock metadata.
  Tests the LangGraph generation chain (intent -> planner -> hocon -> quality)
  without requiring real database connections. Measures plan accuracy and
  HOCON structural correctness.

- **L1 (Simulated Fault)**: Inject fault scenarios (missing PK, type mismatch,
  sensitive field unmasked, etc.) and verify the quality gate catches them.
  Measures interception rate and false-positive rate.

- **L2 (Real Chain)**: End-to-end test with real database connections
  (MySQL -> Doris). Requires Docker infrastructure. Measures full-chain
  success rate, latency, and rollback correctness.

Usage::

    from etl_agent.benchmark.runner import BenchmarkRunner

    runner = BenchmarkRunner()
    report = runner.run_all()
    print(runner.format_report(report))
"""

from etl_agent.benchmark.runner import BenchmarkRunner
from etl_agent.benchmark.schemas import BenchmarkReport, LevelResult

__all__ = [
    "BenchmarkRunner",
    "BenchmarkReport",
    "LevelResult",
]
