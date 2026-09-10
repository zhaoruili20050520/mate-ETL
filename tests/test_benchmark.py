"""Tests for the benchmark evaluation module.

Covers:
- L0 suite runs with a mock graph (no LLM/DB required)
- L1 suite runs with the real quality_checker_node
- L2 suite skips correctly when infrastructure is unavailable
- BenchmarkRunner produces a valid report
- Benchmark API endpoint returns a report
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from etl_agent.benchmark.datasets import L0_CASES, L1_CASES, L2_CASES
from etl_agent.benchmark.evaluators import evaluate_l0_case, evaluate_l1_case
from etl_agent.benchmark.l1_fault import run_l1_suite
from etl_agent.benchmark.l2_chain import run_l2_suite
from etl_agent.benchmark.runner import BenchmarkRunner
from etl_agent.benchmark.schemas import BenchmarkReport, LevelResult
from etl_agent.main import app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockGraph:
    """A minimal mock of the LangGraph compiled graph.

    Returns a pre-built final state so L0 tests can run without
    calling the LLM or database.
    """

    def __init__(self, final_state: dict[str, Any] | None = None) -> None:
        self._final_state = final_state or {}

    def invoke(self, input_state: dict[str, Any]) -> dict[str, Any]:
        # Merge the input state with the pre-built final state
        merged = {**input_state, **self._final_state}
        return merged


def _build_mock_graph() -> MockGraph:
    """Build a mock graph that returns a valid EtlPlan + HOCON."""

    import uuid

    from etl_agent.schemas.generation import (
        EtlPlan,
        FieldMapping,
        GeneratedHocon,
        QualityContract,
        SourceConfig,
        SyncMode,
        SyncStrategy,
        TargetConfig,
    )

    conn_id = uuid.uuid4()

    def _make_plan(req: dict[str, Any]) -> EtlPlan:
        hints = req.get("requirement", {})
        sync_mode = SyncMode(hints.get("sync_mode", "full"))
        source_table = hints.get("source_table", "orders")
        target_table = hints.get("target_table", source_table)
        watermark = hints.get("watermark_column")

        return EtlPlan(
            name="mock-plan",
            description="Mock plan for benchmark",
            source=SourceConfig(
                connection_id=conn_id,
                database_name="testdb",
                schema_name="public",
                table_name=source_table,
            ),
            target=TargetConfig(
                connection_id=conn_id,
                database_name="testdb",
                schema_name="public",
                table_name=target_table,
                shadow_table=f"{target_table}_shadow" if sync_mode == SyncMode.full else None,
            ),
            field_mappings=[
                FieldMapping(
                    source_field="id",
                    target_field="id",
                    source_type="int",
                    target_type="int",
                    transform=None,
                ),
            ],
            cleaning_rules=[],
            sync_strategy=SyncStrategy(
                mode=sync_mode,
                primary_keys=hints.get("primary_keys", ["id"]),
                watermark_column=watermark,
            ),
            quality_contract=QualityContract(
                max_error_ratio=0.01,
                dead_letter_table="dead_letter",
                dead_letter_schema="public",
            ),
        )

    def _invoke(input_state: dict[str, Any]) -> dict[str, Any]:
        plan = _make_plan(input_state)
        hocon_text = (
            'source { Jdbc { url = "jdbc:mysql://localhost:3307/testdb" '
            'table = "test_table" } } '
            'sink { Jdbc { url = "jdbc:doris://localhost:9030/testdb" '
            'table = "test_table" } }'
        )
        hocon = GeneratedHocon(
            config_text=hocon_text,
            source_table_name="test_table",
            result_table_name="test_table",
        )
        return {
            **input_state,
            "etl_plan": plan,
            "hocon_config": hocon,
            "validation_issues": [],
        }

    graph = MockGraph()
    graph.invoke = _invoke  # type: ignore[method-assign]
    return graph


# ---------------------------------------------------------------------------
# Dataset integrity tests
# ---------------------------------------------------------------------------


class TestDatasetIntegrity:
    """Verify the test-case datasets are well-formed."""

    def test_l0_cases_count(self) -> None:
        assert len(L0_CASES) == 8

    def test_l0_case_ids_unique(self) -> None:
        ids = [c["case_id"] for c in L0_CASES]
        assert len(ids) == len(set(ids))

    def test_l0_cases_have_required_fields(self) -> None:
        for case in L0_CASES:
            assert "case_id" in case
            assert "description" in case
            assert "natural_language" in case
            assert "expect" in case

    def test_l1_cases_count(self) -> None:
        assert len(L1_CASES) == 8

    def test_l1_case_ids_unique(self) -> None:
        ids = [c["case_id"] for c in L1_CASES]
        assert len(ids) == len(set(ids))

    def test_l1_cases_have_fault_and_expect(self) -> None:
        for case in L1_CASES:
            assert "fault" in case
            assert "expect" in case
            assert "should_intercept" in case["expect"]

    def test_l1_has_false_positive_cases(self) -> None:
        """At least 2 cases should be valid inputs (should_intercept=False)."""
        non_intercept = [c for c in L1_CASES if not c["expect"]["should_intercept"]]
        assert len(non_intercept) >= 2

    def test_l2_cases_count(self) -> None:
        assert len(L2_CASES) == 3

    def test_l2_all_require_infra(self) -> None:
        for case in L2_CASES:
            assert case.get("requires_infra") is True


# ---------------------------------------------------------------------------
# Evaluator unit tests
# ---------------------------------------------------------------------------


class TestL0Evaluator:
    """Test the L0 evaluation function in isolation."""

    def test_perfect_plan_passes(self) -> None:
        import uuid

        from etl_agent.schemas.generation import (
            EtlPlan,
            FieldMapping,
            QualityContract,
            SourceConfig,
            SyncMode,
            SyncStrategy,
            TargetConfig,
        )

        conn_id = uuid.uuid4()
        plan = EtlPlan(
            name="test",
            description="test",
            source=SourceConfig(
                connection_id=conn_id,
                database_name="testdb",
                schema_name="public",
                table_name="orders",
            ),
            target=TargetConfig(
                connection_id=conn_id,
                database_name="testdb",
                schema_name="public",
                table_name="orders",
                shadow_table="orders_shadow",
            ),
            field_mappings=[
                FieldMapping(
                    source_field="id",
                    target_field="id",
                    source_type="int",
                    target_type="int",
                    transform=None,
                ),
            ],
            cleaning_rules=[],
            sync_strategy=SyncStrategy(
                mode=SyncMode.full,
                primary_keys=["id"],
                watermark_column=None,
            ),
            quality_contract=QualityContract(
                max_error_ratio=0.01,
                dead_letter_table="dl",
                dead_letter_schema="public",
            ),
        )

        case = {
            "case_id": "TEST-001",
            "expect": {
                "sync_mode": "full",
                "source_table": "orders",
                "target_table": "orders",
                "has_etl_plan": True,
                "has_hocon": True,
            },
        }

        result = evaluate_l0_case(
            case,
            etl_plan=plan,
            hocon_text="source { Jdbc { } } sink { Jdbc { } }",
            validation_errors=[],
        )

        assert result.passed is True
        assert result.score == 1.0

    def test_missing_plan_fails(self) -> None:
        case = {
            "case_id": "TEST-002",
            "expect": {"has_etl_plan": True, "has_hocon": True},
        }

        result = evaluate_l0_case(case, etl_plan=None, hocon_text=None, validation_errors=[])

        assert result.passed is False
        assert result.score < 1.0
        assert "has_etl_plan" in result.details
        assert result.details["has_etl_plan"] is False

    def test_wrong_sync_mode_fails(self) -> None:
        import uuid

        from etl_agent.schemas.generation import (
            EtlPlan,
            FieldMapping,
            QualityContract,
            SourceConfig,
            SyncMode,
            SyncStrategy,
            TargetConfig,
        )

        conn_id = uuid.uuid4()
        plan = EtlPlan(
            name="test",
            description="test",
            source=SourceConfig(
                connection_id=conn_id,
                database_name="testdb",
                schema_name="public",
                table_name="orders",
            ),
            target=TargetConfig(
                connection_id=conn_id,
                database_name="testdb",
                schema_name="public",
                table_name="orders",
            ),
            field_mappings=[
                FieldMapping(
                    source_field="id",
                    target_field="id",
                    source_type="int",
                    target_type="int",
                    transform=None,
                ),
            ],
            cleaning_rules=[],
            sync_strategy=SyncStrategy(
                mode=SyncMode.incremental,  # Wrong: expected "full"
                primary_keys=["id"],
                watermark_column="updated_at",
            ),
            quality_contract=QualityContract(
                max_error_ratio=0.01,
                dead_letter_table="dl",
                dead_letter_schema="public",
            ),
        )

        case = {
            "case_id": "TEST-003",
            "expect": {"sync_mode": "full", "has_etl_plan": True},
        }

        result = evaluate_l0_case(case, etl_plan=plan, hocon_text=None, validation_errors=[])

        assert result.passed is False
        assert "sync_mode_mismatch" in result.details


class TestL1Evaluator:
    """Test the L1 evaluation function in isolation."""

    def test_correct_interception_passes(self) -> None:
        case = {
            "case_id": "TEST-L1-001",
            "expect": {
                "should_intercept": True,
                "expected_codes": ["MISSING_PRIMARY_KEY"],
            },
        }

        result = evaluate_l1_case(
            case,
            intercepted=True,
            issue_codes=["MISSING_PRIMARY_KEY"],
        )

        assert result.passed is True
        assert result.score == 1.0

    def test_failed_interception_fails(self) -> None:
        case = {
            "case_id": "TEST-L1-002",
            "expect": {
                "should_intercept": True,
                "expected_codes": ["MISSING_PRIMARY_KEY"],
            },
        }

        result = evaluate_l1_case(case, intercepted=False, issue_codes=[])

        assert result.passed is False
        assert result.score == 0.0

    def test_false_positive_fails(self) -> None:
        case = {
            "case_id": "TEST-L1-003",
            "expect": {
                "should_intercept": False,
                "expected_codes": [],
            },
        }

        result = evaluate_l1_case(
            case,
            intercepted=True,
            issue_codes=["SOME_ERROR"],
        )

        assert result.passed is False
        assert "False positive" in result.message

    def test_correct_non_interception_passes(self) -> None:
        case = {
            "case_id": "TEST-L1-004",
            "expect": {
                "should_intercept": False,
                "expected_codes": [],
            },
        }

        result = evaluate_l1_case(case, intercepted=False, issue_codes=[])

        assert result.passed is True
        assert result.score == 1.0


# ---------------------------------------------------------------------------
# L0 suite tests (with mock graph)
# ---------------------------------------------------------------------------


class TestL0Suite:
    """Test the L0 suite with a mock graph."""

    def test_l0_suite_runs_with_mock_graph(self) -> None:
        from etl_agent.benchmark.l0_static import run_l0_suite

        graph = _build_mock_graph()
        result = run_l0_suite(graph=graph)

        assert isinstance(result, LevelResult)
        assert result.level == "L0"
        assert result.total_cases == 8
        assert result.passed_cases + result.failed_cases == result.total_cases
        assert result.accuracy_rate >= 0.0
        assert result.avg_latency_ms >= 0.0

    def test_l0_suite_case_results_have_ids(self) -> None:
        from etl_agent.benchmark.l0_static import run_l0_suite

        graph = _build_mock_graph()
        result = run_l0_suite(graph=graph)

        case_ids = [cr.case_id for cr in result.case_results]
        assert len(case_ids) == 8
        assert len(case_ids) == len(set(case_ids))

    def test_l0_suite_each_case_has_latency(self) -> None:
        from etl_agent.benchmark.l0_static import run_l0_suite

        graph = _build_mock_graph()
        result = run_l0_suite(graph=graph)

        for cr in result.case_results:
            assert "latency_ms" in cr.details
            assert cr.details["latency_ms"] >= 0.0


# ---------------------------------------------------------------------------
# L1 suite tests (real quality_checker_node, no DB)
# ---------------------------------------------------------------------------


class TestL1Suite:
    """Test the L1 suite with the real quality_checker_node."""

    def test_l1_suite_runs(self) -> None:
        result = run_l1_suite()

        assert isinstance(result, LevelResult)
        assert result.level == "L1"
        assert result.total_cases == 8
        assert result.passed_cases + result.failed_cases == result.total_cases

    def test_l1_suite_has_interception_rate(self) -> None:
        result = run_l1_suite()

        assert 0.0 <= result.interception_rate <= 1.0
        assert 0.0 <= result.false_positive_rate <= 1.0

    def test_l1_suite_case_ids(self) -> None:
        result = run_l1_suite()

        case_ids = [cr.case_id for cr in result.case_results]
        assert len(case_ids) == 8
        expected_ids = {
            "L1-001",
            "L1-002",
            "L1-003",
            "L1-004",
            "L1-005",
            "L1-006",
            "L1-007",
            "L1-008",
        }
        assert set(case_ids) == expected_ids

    def test_l1_suite_false_positive_cases_pass(self) -> None:
        """L1-006 and L1-007 should NOT be intercepted (valid inputs)."""
        result = run_l1_suite()

        for cr in result.case_results:
            if cr.case_id in ("L1-006", "L1-007"):
                # These are valid inputs; they should pass (not be intercepted)
                # If they fail, it means false positive
                assert cr.passed, f"L1 case {cr.case_id} (valid input) failed: {cr.message}"


# ---------------------------------------------------------------------------
# L2 suite tests (skip when no infra)
# ---------------------------------------------------------------------------


class TestL2Suite:
    """Test the L2 suite with infra explicitly disabled."""

    def test_l2_suite_skips_without_infra(self) -> None:
        result = run_l2_suite(infra_available=False)

        assert isinstance(result, LevelResult)
        assert result.level == "L2"
        assert result.total_cases == 3
        assert result.skipped_cases == 3
        assert result.passed_cases == 0

    def test_l2_skip_details(self) -> None:
        result = run_l2_suite(infra_available=False)

        for cr in result.case_results:
            assert cr.details.get("skipped") is True

    def test_l2_case_ids(self) -> None:
        result = run_l2_suite(infra_available=False)

        case_ids = [cr.case_id for cr in result.case_results]
        assert set(case_ids) == {"L2-001", "L2-002", "L2-003"}


# ---------------------------------------------------------------------------
# Runner tests
# ---------------------------------------------------------------------------


class TestBenchmarkRunner:
    """Test the BenchmarkRunner orchestration."""

    def test_run_all_returns_report(self) -> None:
        runner = BenchmarkRunner()
        report = runner.run_all()

        assert isinstance(report, BenchmarkReport)
        assert len(report.levels) == 3
        assert report.overall_accuracy >= 0.0

    def test_run_all_levels_present(self) -> None:
        runner = BenchmarkRunner()
        report = runner.run_all()

        level_names = [lvl.level for lvl in report.levels]
        assert level_names == ["L0", "L1", "L2"]

    def test_last_report_cached(self) -> None:
        runner = BenchmarkRunner()
        report1 = runner.run_all()
        assert runner.last_report is not None
        assert runner.last_report is report1

    def test_format_report(self) -> None:
        runner = BenchmarkRunner()
        report = runner.run_all()
        text = runner.format_report(report)

        assert "ETL-Agent Benchmark Report" in text
        assert "L0:" in text
        assert "L1:" in text
        assert "L2:" in text

    def test_format_report_none(self) -> None:
        runner = BenchmarkRunner()
        text = runner.format_report(None)
        assert "No benchmark report available" in text

    def test_report_summary_nonempty(self) -> None:
        runner = BenchmarkRunner()
        report = runner.run_all()
        assert report.summary
        assert "L0:" in report.summary

    def test_report_timestamp_present(self) -> None:
        runner = BenchmarkRunner()
        report = runner.run_all()
        assert report.timestamp
        assert "T" in report.timestamp  # ISO format


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestBenchmarkAPI:
    """Test the benchmark API endpoint."""

    def test_benchmark_api_returns_report(self) -> None:
        client = TestClient(app)
        response = client.get("/api/v1/benchmark?run=true")

        assert response.status_code == 200
        data = response.json()
        assert "version" in data
        assert "levels" in data
        assert len(data["levels"]) == 3
        assert "overall_accuracy" in data

    def test_benchmark_api_text_endpoint(self) -> None:
        client = TestClient(app)
        response = client.get("/api/v1/benchmark/text")

        assert response.status_code == 200
        text = response.text
        assert "ETL-Agent Benchmark Report" in text or "No benchmark" in text

    def test_benchmark_api_cached_report(self) -> None:
        """Second call without ?run=true should return the same cached report."""
        client = TestClient(app)

        # First call runs the benchmark
        r1 = client.get("/api/v1/benchmark?run=true")
        assert r1.status_code == 200

        # Second call should return cached (not re-run)
        r2 = client.get("/api/v1/benchmark")
        assert r2.status_code == 200

        assert r1.json()["timestamp"] == r2.json()["timestamp"]
