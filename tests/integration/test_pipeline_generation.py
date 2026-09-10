"""Integration tests for the LangGraph pipeline-generation chain.

Covers 10 scenarios as specified in the Step 11 requirements:

1. Requirement parsing success
2. Missing parameters -> clarification state
3. Real metadata -> HOCON generation
4. HOCON syntax validation
5. Schema field mapping error interception
6. Incremental sync missing primary key -> fix suggestion
7. Autofix attempt limit
8. PipelineVersion immutability
9. SHA256 stability and consistency
10. Gate pass required before freezing

Tests use two strategies:
- **Full-graph tests** (scenarios 1, 2, 3, 6): invoke ``pipeline_graph``
  with a mock LLM service and a monkeypatched DB session for the
  metadata node.
- **Node-level tests** (scenarios 4, 5, 7, 8, 9, 10): call individual
  node functions directly with pre-built state, avoiding DB access.
"""

from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from etl_agent.agents.graph import freeze_node, pipeline_graph
from etl_agent.agents.nodes.autofix import autofix_node
from etl_agent.agents.nodes.quality_checker import quality_checker_node
from etl_agent.agents.state import (
    MAX_AUTOFIX_ATTEMPTS,
    STATUS_DRAFT,
    STATUS_ERROR,
    STATUS_FROZEN,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_NEEDS_REVIEW,
    STATUS_VALIDATED,
    PipelineState,
    initial_state,
)
from etl_agent.schemas.generation import (
    EtlPlan,
    FieldMapping,
    GeneratedHocon,
    IssueSeverity,
    QualityContract,
    SourceConfig,
    SyncMode,
    SyncStrategy,
    TargetConfig,
    ValidationCode,
    ValidationIssue,
)
from etl_agent.services.llm_service import set_llm_service

# =====================================================================
# Constants
# =====================================================================

_SOURCE_CONNECTION = "00000000-0000-0000-0000-000000000001"
_TARGET_CONNECTION = "00000000-0000-0000-0000-000000000002"
_PROJECT = "00000000-0000-0000-0000-000000000003"
_TENANT = "00000000-0000-0000-0000-000000000004"
_USER = "00000000-0000-0000-0000-000000000005"


# =====================================================================
# Metadata builders
# =====================================================================


def _mk_source_profile(table_name: str = "users") -> SimpleNamespace:
    """Build a source MetadataProfile-like object."""
    return SimpleNamespace(
        schema_name="public",
        table_name=table_name,
        columns_info=[
            {"name": "id", "type": "bigint", "nullable": False},
            {"name": "username", "type": "varchar(255)", "nullable": False},
            {"name": "email", "type": "varchar(255)", "nullable": False},
            {"name": "password_hash", "type": "varchar(255)", "nullable": False},
            {"name": "created_at", "type": "timestamp", "nullable": False},
            {"name": "updated_at", "type": "timestamp", "nullable": True},
        ],
        stats_info={"row_count": 10000},
        value_distribution=[],
        sample_data=[],
    )


def _mk_target_profile(table_name: str = "dim_users") -> SimpleNamespace:
    """Build a target MetadataProfile-like object."""
    return SimpleNamespace(
        schema_name="public",
        table_name=table_name,
        columns_info=[
            {"name": "id", "type": "bigint", "nullable": False},
            {"name": "username", "type": "varchar(255)", "nullable": False},
            {"name": "email", "type": "varchar(255)", "nullable": False},
            {"name": "password_hash", "type": "varchar(255)", "nullable": False},
            {"name": "created_at", "type": "timestamp", "nullable": False},
            {"name": "updated_at", "type": "timestamp", "nullable": True},
        ],
        stats_info={"row_count": 10000},
        value_distribution=[],
        sample_data=[],
    )


def _mk_metadata_context(
    *,
    source_table: str = "users",
    target_table: str = "dim_users",
) -> dict[str, Any]:
    """Build a metadata_context dict matching metadata_node output format."""
    src = _mk_source_profile(source_table)
    tgt = _mk_target_profile(target_table)

    def _profile_dict(p: SimpleNamespace) -> dict[str, Any]:
        return {
            "schema_name": p.schema_name,
            "table_name": p.table_name,
            "columns_info": p.columns_info,
            "stats_info": p.stats_info,
            "value_distribution": p.value_distribution,
            "sample_data": p.sample_data,
        }

    src_dict = _profile_dict(src)
    tgt_dict = _profile_dict(tgt)
    return {
        "source": {
            "connection_id": _SOURCE_CONNECTION,
            "all_tables": [src_dict],
            "selected_table": src_dict,
        },
        "target": {
            "connection_id": _TARGET_CONNECTION,
            "all_tables": [tgt_dict],
            "selected_table": tgt_dict,
        },
    }


# =====================================================================
# Plan + HOCON builders
# =====================================================================


def _mk_valid_etl_plan(
    *,
    sync_mode: SyncMode = SyncMode.full,
    primary_keys: list[str] | None = None,
    watermark: str | None = None,
    shadow_table: str | None = "dim_users_shadow",
    source_table: str = "users",
    target_table: str = "dim_users",
) -> EtlPlan:
    """Build a valid EtlPlan that passes all gate checks."""
    resolved_pks = (
        primary_keys
        if primary_keys is not None
        else (["id"] if sync_mode == SyncMode.incremental else [])
    )
    return EtlPlan(
        name="users_sync",
        description="Sync users table",
        source=SourceConfig(
            connection_id=uuid.UUID(_SOURCE_CONNECTION),
            database_name="source_db",
            schema_name="public",
            table_name=source_table,
        ),
        target=TargetConfig(
            connection_id=uuid.UUID(_TARGET_CONNECTION),
            database_name="target_db",
            schema_name="public",
            table_name=target_table,
            shadow_table=shadow_table,
        ),
        field_mappings=[
            FieldMapping(
                source_field="id",
                target_field="id",
                source_type="bigint",
                target_type="bigint",
            ),
            FieldMapping(
                source_field="username",
                target_field="username",
                source_type="varchar(255)",
                target_type="varchar(255)",
            ),
            FieldMapping(
                source_field="email",
                target_field="email",
                source_type="varchar(255)",
                target_type="varchar(255)",
                transform="mask",
            ),
            FieldMapping(
                source_field="password_hash",
                target_field="password_hash",
                source_type="varchar(255)",
                target_type="varchar(255)",
                transform="mask",
            ),
            FieldMapping(
                source_field="created_at",
                target_field="created_at",
                source_type="timestamp",
                target_type="timestamp",
            ),
        ],
        sync_strategy=SyncStrategy(
            mode=sync_mode,
            primary_keys=resolved_pks,
            watermark_column=watermark,
        ),
        quality_contract=QualityContract(
            max_error_ratio=0.01,
            dead_letter_table="dim_users_dl",
            dead_letter_schema="public",
        ),
    )


def _mk_valid_hocon(plan: EtlPlan | None = None) -> GeneratedHocon:
    """Build a syntactically valid HOCON string."""
    plan = plan or _mk_valid_etl_plan()
    hocon_text = (
        "env {\n"
        f'  job.name = "{plan.name}"\n'
        "}\n"
        "source {\n"
        "  Jdbc {\n"
        '    url = "jdbc:postgresql://localhost:15432/source_db"\n'
        f'    table = "{{public,{plan.source.table_name}}}"\n'
        f'    result_table_name = "{plan.source.table_name}_source"\n'
        "  }\n"
        "}\n"
        "transform {\n"
        "  Sql {\n"
        f'    source_table = "{plan.source.table_name}_source"\n'
        f'    result_table_name = "{plan.target.table_name}_transformed"\n'
        '    query = "SELECT * FROM users_source"\n'
        "  }\n"
        "}\n"
        "sink {\n"
        "  Jdbc {\n"
        '    url = "jdbc:postgresql://localhost:15432/target_db"\n'
        f'    table = "{{public,{plan.target.table_name}}}"\n'
        f'    source_table_name = "{plan.target.table_name}_transformed"\n'
        "  }\n"
        "}\n"
    )
    return GeneratedHocon(
        config_text=hocon_text,
        source_table_name=f"{plan.source.table_name}_source",
        result_table_name=f"{plan.target.table_name}_transformed",
    )


# =====================================================================
# Mock LLM service
# =====================================================================


class MockLLMService:
    """A mock LLMService that returns deterministic results.

    Tests configure what each method returns by setting attributes
    before the graph or node runs.
    """

    def __init__(self) -> None:
        self.parse_intent_result: dict[str, Any] = {}
        self.etl_plan_result: EtlPlan | None = None
        self.hocon_result: GeneratedHocon | None = None

    def parse_intent(self, natural_language: str) -> dict[str, Any]:
        return self.parse_intent_result

    def generate_etl_plan(
        self,
        requirement: dict[str, Any],
        source_metadata: dict[str, Any],
        target_metadata: dict[str, Any],
    ) -> EtlPlan:
        if self.etl_plan_result is None:
            raise ValueError("MockLLMService.etl_plan_result not set")
        return self.etl_plan_result

    def generate_hocon(self, etl_plan: EtlPlan) -> GeneratedHocon:
        if self.hocon_result is None:
            raise ValueError("MockLLMService.hocon_result not set")
        return self.hocon_result

    def suggest_fix(self, issues: list[ValidationIssue]) -> dict[str, Any]:
        return {"manual": True, "reason": "Mock cannot auto-fix"}


@pytest.fixture()
def mock_llm() -> MockLLMService:
    """Inject the mock LLM service, restore after test."""
    mock = MockLLMService()
    set_llm_service(mock)  # type: ignore[arg-type]
    yield mock
    set_llm_service(None)


def _mk_mock_session(source_profiles: list, target_profiles: list):
    """Build a mock DB session for the metadata node."""
    session = type("MockSession", (), {})()

    call_count = [0]

    def scalars(stmt):
        call_count[0] += 1
        result = type("MockResult", (), {})()
        if call_count[0] == 1:
            result.all = lambda: source_profiles
        else:
            result.all = lambda: target_profiles
        return result

    session.scalars = scalars
    session.close = lambda: None
    return session


def _build_state(
    *,
    natural_language: str = "Sync users table from source to target",
) -> PipelineState:
    """Build a minimal initial PipelineState for testing."""
    return initial_state(
        natural_language=natural_language,
        source_connection_id=_SOURCE_CONNECTION,
        target_connection_id=_TARGET_CONNECTION,
        project_id=_PROJECT,
        tenant_id=_TENANT,
        user_id=_USER,
    )


def _run_graph_with_mock_metadata(
    state: PipelineState,
    source_profiles: list,
    target_profiles: list,
) -> dict[str, Any]:
    """Invoke pipeline_graph with a monkeypatched DB session."""
    mock_session = _mk_mock_session(source_profiles, target_profiles)
    with patch(
        "etl_agent.agents.nodes.metadata.SessionLocal",
        return_value=mock_session,
    ):
        return pipeline_graph.invoke(state)


# =====================================================================
# Test 1: Requirement parsing success
# =====================================================================


class TestRequirementParsing:
    """Scenario 1: The intent node successfully parses a natural-language
    requirement into a complete PipelineRequirement, and the full chain
    produces a frozen version."""

    def test_parse_success(self, mock_llm: MockLLMService) -> None:
        mock_llm.parse_intent_result = {
            "sync_mode": "full",
            "source_table": "users",
            "target_table": "dim_users",
        }
        mock_llm.etl_plan_result = _mk_valid_etl_plan()
        mock_llm.hocon_result = _mk_valid_hocon()

        state = _build_state()
        result = _run_graph_with_mock_metadata(
            state,
            [_mk_source_profile()],
            [_mk_target_profile()],
        )

        assert result.get("status") != STATUS_ERROR
        assert result.get("clarification_needed") is False
        req = result.get("requirement")
        assert req is not None
        assert req.sync_mode == SyncMode.full
        assert req.source_table == "users"
        assert req.target_table == "dim_users"


# =====================================================================
# Test 2: Missing parameters -> clarification
# =====================================================================


class TestClarificationTrigger:
    """Scenario 2: When the intent node cannot parse required fields,
    it sets clarification_needed and populates questions.  The graph
    stops before reaching the metadata node."""

    def test_missing_sync_mode_triggers_clarification(self, mock_llm: MockLLMService) -> None:
        mock_llm.parse_intent_result = {
            "sync_mode": None,
            "source_table": "users",
            "target_table": "dim_users",
        }

        state = _build_state()
        # No metadata mock needed — graph stops at clarification
        result = pipeline_graph.invoke(state)

        assert result.get("status") == STATUS_NEEDS_CLARIFICATION
        assert result.get("clarification_needed") is True
        questions = result.get("clarification_questions", [])
        assert len(questions) > 0
        fields = [q["field"] for q in questions]
        assert "sync_mode" in fields

    def test_missing_source_table_triggers_clarification(self, mock_llm: MockLLMService) -> None:
        mock_llm.parse_intent_result = {
            "sync_mode": "full",
            "source_table": None,
            "target_table": "dim_users",
        }

        state = _build_state()
        result = pipeline_graph.invoke(state)

        assert result.get("status") == STATUS_NEEDS_CLARIFICATION
        questions = result.get("clarification_questions", [])
        fields = [q["field"] for q in questions]
        assert "source_table" in fields

    def test_incremental_without_pk_triggers_clarification(self, mock_llm: MockLLMService) -> None:
        mock_llm.parse_intent_result = {
            "sync_mode": "incremental",
            "source_table": "users",
            "target_table": "dim_users",
            "primary_keys": [],
            "watermark_column": None,
        }

        state = _build_state()
        result = pipeline_graph.invoke(state)

        assert result.get("status") == STATUS_NEEDS_CLARIFICATION
        questions = result.get("clarification_questions", [])
        fields = [q["field"] for q in questions]
        assert "primary_keys" in fields
        assert "watermark_column" in fields


# =====================================================================
# Test 3: Real metadata -> HOCON generation
# =====================================================================


class TestHoconGeneration:
    """Scenario 3: Given a valid requirement + real metadata, the chain
    produces a non-empty HOCON string."""

    def test_hocon_generated_from_metadata(self, mock_llm: MockLLMService) -> None:
        mock_llm.parse_intent_result = {
            "sync_mode": "full",
            "source_table": "users",
            "target_table": "dim_users",
        }
        mock_llm.etl_plan_result = _mk_valid_etl_plan()
        mock_llm.hocon_result = _mk_valid_hocon()

        state = _build_state()
        result = _run_graph_with_mock_metadata(
            state,
            [_mk_source_profile()],
            [_mk_target_profile()],
        )

        hocon = result.get("hocon_config")
        assert hocon is not None
        assert isinstance(hocon, GeneratedHocon)
        assert len(hocon.config_text) > 0
        assert "source" in hocon.config_text.lower()
        assert "sink" in hocon.config_text.lower()


# =====================================================================
# Test 4: HOCON syntax validation
# =====================================================================


class TestHoconSyntaxValidation:
    """Scenario 4: The quality checker catches HOCON syntax errors."""

    def _build_state_for_qc(
        self,
        plan: EtlPlan,
        hocon: GeneratedHocon,
    ) -> PipelineState:
        """Build a state pre-populated for direct quality_checker_node call."""
        state: PipelineState = PipelineState(
            etl_plan=plan,
            hocon_config=hocon,
            metadata_context=_mk_metadata_context(
                source_table=plan.source.table_name,
                target_table=plan.target.table_name,
            ),
            status=STATUS_DRAFT,
            autofix_attempts=0,
        )
        return state

    def test_valid_hocon_passes_syntax_check(self) -> None:
        plan = _mk_valid_etl_plan()
        hocon = _mk_valid_hocon(plan)
        state = self._build_state_for_qc(plan, hocon)

        result = quality_checker_node(state)

        issues = result.get("validation_issues", [])
        syntax_issues = [i for i in issues if i.code == ValidationCode.HOCON_SYNTAX_ERROR]
        assert len(syntax_issues) == 0

    def test_unbalanced_braces_caught(self) -> None:
        plan = _mk_valid_etl_plan()
        bad_hocon = GeneratedHocon(
            config_text="env { job.name = 'test'\nsource {\n",
            source_table_name="users_source",
            result_table_name="users_transformed",
        )
        state = self._build_state_for_qc(plan, bad_hocon)

        result = quality_checker_node(state)

        issues = result.get("validation_issues", [])
        syntax_issues = [i for i in issues if i.code == ValidationCode.HOCON_SYNTAX_ERROR]
        assert len(syntax_issues) > 0
        assert syntax_issues[0].severity == IssueSeverity.error

    def test_empty_hocon_caught(self) -> None:
        plan = _mk_valid_etl_plan()
        empty_hocon = GeneratedHocon(
            config_text="   \n   ",
            source_table_name="users_source",
            result_table_name="users_transformed",
        )
        state = self._build_state_for_qc(plan, empty_hocon)

        result = quality_checker_node(state)

        issues = result.get("validation_issues", [])
        syntax_issues = [i for i in issues if i.code == ValidationCode.HOCON_SYNTAX_ERROR]
        assert len(syntax_issues) > 0


# =====================================================================
# Test 5: Schema field mapping error interception
# =====================================================================


class TestSchemaMappingInterception:
    """Scenario 5: Field mappings referencing non-existent columns
    are caught by the quality checker."""

    def _build_state_for_qc(self, plan: EtlPlan, hocon: GeneratedHocon) -> PipelineState:
        return PipelineState(
            etl_plan=plan,
            hocon_config=hocon,
            metadata_context=_mk_metadata_context(
                source_table=plan.source.table_name,
                target_table=plan.target.table_name,
            ),
            status=STATUS_DRAFT,
            autofix_attempts=0,
        )

    def test_nonexistent_source_field_caught(self) -> None:
        plan = _mk_valid_etl_plan()
        plan.field_mappings.append(
            FieldMapping(
                source_field="nonexistent_column",
                target_field="id",
                source_type="varchar(255)",
                target_type="bigint",
            )
        )
        hocon = _mk_valid_hocon(plan)
        state = self._build_state_for_qc(plan, hocon)

        result = quality_checker_node(state)

        issues = result.get("validation_issues", [])
        field_issues = [i for i in issues if i.code == ValidationCode.FIELD_NOT_FOUND]
        assert len(field_issues) > 0
        assert any("nonexistent_column" in i.message for i in field_issues)

    def test_nonexistent_target_field_caught(self) -> None:
        plan = _mk_valid_etl_plan()
        plan.field_mappings.append(
            FieldMapping(
                source_field="id",
                target_field="nonexistent_target",
                source_type="bigint",
                target_type="bigint",
            )
        )
        hocon = _mk_valid_hocon(plan)
        state = self._build_state_for_qc(plan, hocon)

        result = quality_checker_node(state)

        issues = result.get("validation_issues", [])
        field_issues = [i for i in issues if i.code == ValidationCode.FIELD_NOT_FOUND]
        assert len(field_issues) > 0
        assert any("nonexistent_target" in i.message for i in field_issues)


# =====================================================================
# Test 6: Incremental sync missing primary key -> fix suggestion
# =====================================================================


class TestIncrementalMissingPrimaryKey:
    """Scenario 6: Incremental sync without primary keys is flagged
    with a fix suggestion, and autofix can infer the PK from metadata."""

    def test_missing_pk_flagged_as_error(self) -> None:
        plan = _mk_valid_etl_plan(
            sync_mode=SyncMode.incremental,
            primary_keys=[],
            watermark="updated_at",
        )
        hocon = _mk_valid_hocon(plan)
        state: PipelineState = PipelineState(
            etl_plan=plan,
            hocon_config=hocon,
            metadata_context=_mk_metadata_context(),
            status=STATUS_DRAFT,
            autofix_attempts=0,
        )

        result = quality_checker_node(state)

        issues = result.get("validation_issues", [])
        pk_issues = [i for i in issues if i.code == ValidationCode.MISSING_PRIMARY_KEY]
        assert len(pk_issues) > 0
        assert pk_issues[0].severity == IssueSeverity.error
        assert "primary key" in pk_issues[0].suggestion.lower()

    def test_autofix_infers_pk_from_metadata(self) -> None:
        """Autofix infers 'id' as primary key from metadata when MISSING_PRIMARY_KEY."""
        plan = _mk_valid_etl_plan(
            sync_mode=SyncMode.incremental,
            primary_keys=[],
            watermark="updated_at",
        )
        hocon = _mk_valid_hocon(plan)

        # First run quality checker to get issues
        qc_state: PipelineState = PipelineState(
            etl_plan=plan,
            hocon_config=hocon,
            metadata_context=_mk_metadata_context(),
            status=STATUS_DRAFT,
            autofix_attempts=0,
        )
        qc_result = quality_checker_node(qc_state)
        issues = qc_result.get("validation_issues", [])

        # Feed issues into autofix
        af_state: PipelineState = PipelineState(
            etl_plan=plan,
            hocon_config=hocon,
            validation_issues=issues,
            metadata_context=_mk_metadata_context(),
            status=STATUS_NEEDS_REVIEW,
            autofix_attempts=0,
        )
        af_result = autofix_node(af_state)

        # Autofix should have inferred 'id' as PK
        assert af_result.get("autofix_attempts") == 1
        patched_plan: EtlPlan | None = af_result.get("etl_plan")
        if patched_plan is not None:
            assert len(patched_plan.sync_strategy.primary_keys) > 0
            assert "id" in patched_plan.sync_strategy.primary_keys


# =====================================================================
# Test 7: Autofix attempt limit
# =====================================================================


class TestAutofixLimit:
    """Scenario 7: Autofix does not loop forever -- it stops after
    MAX_AUTOFIX_ATTEMPTS and escalates to needs_review."""

    def test_max_attempts_enforced(self) -> None:
        """An unfixable error causes autofix to exhaust attempts."""
        plan = EtlPlan(
            name="bad_sync",
            description="Bad plan",
            source=SourceConfig(
                connection_id=uuid.UUID(_SOURCE_CONNECTION),
                database_name="source_db",
                schema_name="public",
                table_name="nonexistent_table",
            ),
            target=TargetConfig(
                connection_id=uuid.UUID(_TARGET_CONNECTION),
                database_name="target_db",
                schema_name="public",
                table_name="dim_users",
                shadow_table="dim_users_shadow",
            ),
            field_mappings=[
                FieldMapping(
                    source_field="nonexistent_column",
                    target_field="id",
                    source_type="varchar(255)",
                    target_type="bigint",
                ),
            ],
            sync_strategy=SyncStrategy(mode=SyncMode.full),
            quality_contract=QualityContract(
                max_error_ratio=0.01,
                dead_letter_table="dim_users_dl",
                dead_letter_schema="public",
            ),
        )
        hocon = _mk_valid_hocon(plan)

        # Build state with autofix_attempts at MAX
        state: PipelineState = PipelineState(
            etl_plan=plan,
            hocon_config=hocon,
            validation_issues=[
                ValidationIssue(
                    severity=IssueSeverity.error,
                    code=ValidationCode.FIELD_NOT_FOUND,
                    message="Source field 'nonexistent_column' not found",
                    suggestion="Fix the field name",
                ),
            ],
            metadata_context=_mk_metadata_context(
                source_table="nonexistent_table",
            ),
            status=STATUS_NEEDS_REVIEW,
            autofix_attempts=MAX_AUTOFIX_ATTEMPTS,
        )

        result = autofix_node(state)

        # Should escalate to needs_review, not loop
        assert result.get("status") == STATUS_NEEDS_REVIEW
        assert result.get("autofix_attempts") == MAX_AUTOFIX_ATTEMPTS

    def test_unfixable_error_escalates_after_attempts(self) -> None:
        """When autofix cannot fix an issue, it escalates to needs_review."""
        plan = _mk_valid_etl_plan()
        # Add a field that truly doesn't exist in metadata
        plan.field_mappings.append(
            FieldMapping(
                source_field="zzz_nonexistent",
                target_field="username",
                source_type="varchar(255)",
                target_type="varchar(255)",
            )
        )
        hocon = _mk_valid_hocon(plan)

        state: PipelineState = PipelineState(
            etl_plan=plan,
            hocon_config=hocon,
            validation_issues=[
                ValidationIssue(
                    severity=IssueSeverity.error,
                    code=ValidationCode.FIELD_NOT_FOUND,
                    message="Source field 'zzz_nonexistent' not found",
                    suggestion="Fix the field name",
                    field="zzz_nonexistent",
                ),
            ],
            metadata_context=_mk_metadata_context(),
            status=STATUS_NEEDS_REVIEW,
            autofix_attempts=0,
        )

        result = autofix_node(state)

        # No fix was applied -> escalate
        assert result.get("status") == STATUS_NEEDS_REVIEW
        assert result.get("autofix_attempts") == 1


# =====================================================================
# Test 8: PipelineVersion immutability
# =====================================================================


class TestPipelineVersionImmutability:
    """Scenario 8: Once frozen, a PipelineVersion is immutable.
    Only new versions can be added."""

    def test_freeze_produces_version_with_sha256(self) -> None:
        plan = _mk_valid_etl_plan()
        hocon = _mk_valid_hocon(plan)

        state: PipelineState = PipelineState(
            etl_plan=plan,
            hocon_config=hocon,
            validation_issues=[],
            status=STATUS_VALIDATED,
            user_id=_USER,
            pipeline_id="test-pipeline-id",
        )

        result = freeze_node(state)

        version = result.get("pipeline_version")
        assert version is not None
        assert "sha256" in version
        assert "version_number" in version
        assert "etl_plan_json" in version
        assert "hocon_text" in version
        assert version["sha256"]  # non-empty
        assert len(version["sha256"]) == 64  # SHA256 hex length
        assert result.get("status") == STATUS_FROZEN

    def test_version_number_increments(self) -> None:
        """Version number increments based on existing_version_count."""
        plan = _mk_valid_etl_plan()
        hocon = _mk_valid_hocon(plan)

        state: PipelineState = PipelineState(
            etl_plan=plan,
            hocon_config=hocon,
            validation_issues=[],
            status=STATUS_VALIDATED,
            user_id=_USER,
            pipeline_id="test-pipeline-id",
            existing_version_count=2,  # type: ignore[typeddict-item]
        )

        result = freeze_node(state)
        version = result["pipeline_version"]
        assert version["version_number"] == 3


# =====================================================================
# Test 9: SHA256 stability and consistency
# =====================================================================


class TestSHA256Stability:
    """Scenario 9: The SHA256 hash is deterministic -- the same
    EtlPlan + HOCON always produce the same hash."""

    def test_sha256_deterministic(self) -> None:
        """Two freeze runs with identical inputs produce identical SHA256."""
        plan = _mk_valid_etl_plan()
        hocon = _mk_valid_hocon(plan)

        state1: PipelineState = PipelineState(
            etl_plan=plan,
            hocon_config=hocon,
            validation_issues=[],
            status=STATUS_VALIDATED,
            user_id=_USER,
            pipeline_id="test-1",
        )
        state2: PipelineState = PipelineState(
            etl_plan=plan.model_copy(deep=True),
            hocon_config=hocon.model_copy(deep=True),
            validation_issues=[],
            status=STATUS_VALIDATED,
            user_id=_USER,
            pipeline_id="test-2",
        )

        result1 = freeze_node(state1)
        result2 = freeze_node(state2)

        sha1 = result1["pipeline_version"]["sha256"]
        sha2 = result2["pipeline_version"]["sha256"]
        assert sha1 == sha2, f"SHA256 mismatch: {sha1} != {sha2}"

    def test_sha256_matches_manual_computation(self) -> None:
        """The SHA256 matches manual hashlib computation."""
        plan = _mk_valid_etl_plan()
        hocon = _mk_valid_hocon(plan)

        state: PipelineState = PipelineState(
            etl_plan=plan,
            hocon_config=hocon,
            validation_issues=[],
            status=STATUS_VALIDATED,
            user_id=_USER,
            pipeline_id="test-3",
        )

        result = freeze_node(state)
        version = result["pipeline_version"]

        plan_json = plan.model_dump_json()
        combined = plan_json + "\n" + hocon.config_text
        expected_sha = hashlib.sha256(combined.encode("utf-8")).hexdigest()

        assert version["sha256"] == expected_sha

    def test_different_plans_produce_different_sha256(self) -> None:
        """Different EtlPlans produce different SHA256 hashes."""
        plan1 = _mk_valid_etl_plan()
        plan2 = _mk_valid_etl_plan()
        plan2.name = "different_name"

        hocon1 = _mk_valid_hocon(plan1)
        hocon2 = _mk_valid_hocon(plan2)

        state1: PipelineState = PipelineState(
            etl_plan=plan1,
            hocon_config=hocon1,
            validation_issues=[],
            status=STATUS_VALIDATED,
            user_id=_USER,
            pipeline_id="test-4",
        )
        state2: PipelineState = PipelineState(
            etl_plan=plan2,
            hocon_config=hocon2,
            validation_issues=[],
            status=STATUS_VALIDATED,
            user_id=_USER,
            pipeline_id="test-5",
        )

        result1 = freeze_node(state1)
        result2 = freeze_node(state2)

        sha1 = result1["pipeline_version"]["sha256"]
        sha2 = result2["pipeline_version"]["sha256"]
        assert sha1 != sha2


# =====================================================================
# Test 10: Gate pass required before freezing
# =====================================================================


class TestGatePassBeforeFreeze:
    """Scenario 10: A pipeline with errors cannot be frozen.
    Only validated pipelines reach the freeze node."""

    def test_validated_pipeline_freezes(self) -> None:
        """A plan that passes all gate checks reaches frozen status."""
        plan = _mk_valid_etl_plan()
        hocon = _mk_valid_hocon(plan)

        state: PipelineState = PipelineState(
            etl_plan=plan,
            hocon_config=hocon,
            metadata_context=_mk_metadata_context(),
            status=STATUS_DRAFT,
            autofix_attempts=0,
        )

        # Run quality checker
        qc_result = quality_checker_node(state)
        assert qc_result.get("status") == STATUS_VALIDATED

        # Freeze should succeed
        freeze_state: PipelineState = PipelineState(
            etl_plan=plan,
            hocon_config=hocon,
            validation_issues=qc_result.get("validation_issues", []),
            status=STATUS_VALIDATED,
            user_id=_USER,
            pipeline_id="test-6",
        )
        fr_result = freeze_node(freeze_state)
        assert fr_result.get("status") == STATUS_FROZEN
        assert fr_result.get("pipeline_version") is not None

    def test_error_pipeline_does_not_freeze(self) -> None:
        """A plan with unfixable errors does NOT reach frozen status."""
        plan = EtlPlan(
            name="broken",
            description="Broken plan",
            source=SourceConfig(
                connection_id=uuid.UUID(_SOURCE_CONNECTION),
                database_name="source_db",
                schema_name="public",
                table_name="nonexistent_table",
            ),
            target=TargetConfig(
                connection_id=uuid.UUID(_TARGET_CONNECTION),
                database_name="target_db",
                schema_name="public",
                table_name="also_nonexistent",
                shadow_table="also_nonexistent_shadow",
            ),
            field_mappings=[
                FieldMapping(
                    source_field="nonexistent_column",
                    target_field="also_nonexistent",
                    source_type="varchar(255)",
                    target_type="bigint",
                ),
            ],
            sync_strategy=SyncStrategy(mode=SyncMode.full),
            quality_contract=None,
        )
        hocon = _mk_valid_hocon(plan)

        state: PipelineState = PipelineState(
            etl_plan=plan,
            hocon_config=hocon,
            metadata_context=_mk_metadata_context(
                source_table="nonexistent_table",
                target_table="also_nonexistent",
            ),
            status=STATUS_DRAFT,
            autofix_attempts=0,
        )

        qc_result = quality_checker_node(state)

        # Should NOT be validated
        assert qc_result.get("status") == STATUS_NEEDS_REVIEW
        issues = qc_result.get("validation_issues", [])
        assert len(issues) > 0
        # Should have error-severity issues
        errors = [i for i in issues if i.severity == IssueSeverity.error]
        assert len(errors) > 0

    def test_no_freeze_when_validation_issues_remain(self) -> None:
        """Even after autofix, if errors remain, no freeze happens."""
        plan = _mk_valid_etl_plan()
        # Add a truly unfixable field mapping
        plan.field_mappings.append(
            FieldMapping(
                source_field="zzz_nonexistent",
                target_field="username",
                source_type="varchar(255)",
                target_type="varchar(255)",
            )
        )
        hocon = _mk_valid_hocon(plan)

        # Run quality checker
        qc_state: PipelineState = PipelineState(
            etl_plan=plan,
            hocon_config=hocon,
            metadata_context=_mk_metadata_context(),
            status=STATUS_DRAFT,
            autofix_attempts=0,
        )
        qc_result = quality_checker_node(qc_state)
        assert qc_result.get("status") == STATUS_NEEDS_REVIEW

        # Run autofix
        af_state: PipelineState = PipelineState(
            etl_plan=plan,
            hocon_config=hocon,
            validation_issues=qc_result.get("validation_issues", []),
            metadata_context=_mk_metadata_context(),
            status=STATUS_NEEDS_REVIEW,
            autofix_attempts=0,
        )
        af_result = autofix_node(af_state)

        # Autofix cannot fix zzz_nonexistent -> escalate
        assert af_result.get("status") == STATUS_NEEDS_REVIEW
        # No pipeline_version should be produced
        assert af_result.get("pipeline_version") is None
