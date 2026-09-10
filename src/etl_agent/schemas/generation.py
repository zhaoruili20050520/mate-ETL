"""Pydantic schemas for the LangGraph pipeline-generation chain.

These structures define the **contract** between the LLM, the
deterministic gate-checker, and the persistence layer.

Flow::

    PipelineRequirement  -- user input (natural language + structured hints)
        |
    EtlPlan               -- logical plan (source, target, mappings, rules)
        |
    GeneratedHocon        -- SeaTunnel executable HOCON
        |
    ValidationIssue       -- gate-check findings (severity + fix suggestion)

All schemas use ``model_config = ConfigDict(extra='forbid')`` so that
unexpected fields from the LLM are rejected rather than silently ignored.
"""

from __future__ import annotations

import enum
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# =====================================================================
# Enums
# =====================================================================


class SyncMode(enum.StrEnum):
    """Synchronisation mode."""

    full = "full"
    incremental = "incremental"


class PipelineStatus(enum.StrEnum):
    """Lifecycle status of a pipeline working draft."""

    draft = "draft"
    generating = "generating"
    needs_clarification = "needs_clarification"
    validated = "validated"
    frozen = "frozen"
    error = "error"


class IssueSeverity(enum.StrEnum):
    """Severity of a validation issue."""

    error = "error"
    warning = "warning"


# =====================================================================
# 1. PipelineRequirement -- user input
# =====================================================================


class PipelineRequirement(BaseModel):
    """Structured representation of a user's natural-language request.

    The ``intent`` node parses free text into this object.  Fields that
    cannot be parsed are left as ``None`` / empty, triggering the
    ``clarify`` node to ask the user for the missing information.
    """

    model_config = ConfigDict(extra="forbid")

    natural_language: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="Raw user input text",
    )
    source_connection_id: uuid.UUID = Field(
        ...,
        description="Source data-source connection UUID",
    )
    target_connection_id: uuid.UUID = Field(
        ...,
        description="Target data-source connection UUID",
    )
    sync_mode: SyncMode = Field(
        ...,
        description="Full or incremental sync",
    )
    source_table: str | None = Field(
        default=None,
        max_length=255,
        description="Source table name (may be extracted from NL)",
    )
    target_table: str | None = Field(
        default=None,
        max_length=255,
        description="Target table name (may be extracted from NL)",
    )
    source_schema: str | None = Field(
        default=None,
        max_length=255,
        description="Source database schema",
    )
    target_schema: str | None = Field(
        default=None,
        max_length=255,
        description="Target database schema",
    )
    primary_keys: list[str] = Field(
        default_factory=list,
        description="Primary key columns for incremental sync / dedup",
    )
    watermark_column: str | None = Field(
        default=None,
        max_length=255,
        description="Watermark column for incremental CDC (e.g. updated_at)",
    )
    extra_params: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional hints parsed from NL (filters, expressions)",
    )


# =====================================================================
# 2. EtlPlan -- logical plan
# =====================================================================


class SourceConfig(BaseModel):
    """Source side of an ETL pipeline."""

    model_config = ConfigDict(extra="forbid")

    connection_id: uuid.UUID
    database_name: str = Field(..., max_length=255)
    schema_name: str = Field(default="public", max_length=255)
    table_name: str = Field(..., max_length=255)


class TargetConfig(BaseModel):
    """Target side of an ETL pipeline.

    ``shadow_table`` is the staging / shadow table used by the
    swap strategy before the final atomic publish.
    """

    model_config = ConfigDict(extra="forbid")

    connection_id: uuid.UUID
    database_name: str = Field(..., max_length=255)
    schema_name: str = Field(default="public", max_length=255)
    table_name: str = Field(..., max_length=255)
    shadow_table: str | None = Field(
        default=None,
        max_length=255,
        description="Shadow/staging table for atomic swap",
    )


class FieldMapping(BaseModel):
    """A single source-to-target field mapping."""

    model_config = ConfigDict(extra="forbid")

    source_field: str = Field(..., max_length=255)
    target_field: str = Field(..., max_length=255)
    source_type: str = Field(..., max_length=100, description="Source column data type")
    target_type: str = Field(..., max_length=100, description="Target column data type")
    transform: str | None = Field(
        default=None,
        max_length=255,
        description="Transform expression (e.g. 'uppercase', 'hash', 'mask')",
    )
    nullable: bool = True


class CleaningRule(BaseModel):
    """A data-quality rule applied during ETL."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(..., max_length=255)
    rule: str = Field(
        ...,
        max_length=100,
        description="Rule type: not_null, unique, regex, range, enum",
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Rule parameters (e.g. regex pattern, min/max)",
    )


class SyncStrategy(BaseModel):
    """Synchronisation strategy."""

    model_config = ConfigDict(extra="forbid")

    mode: SyncMode
    primary_keys: list[str] = Field(default_factory=list)
    watermark_column: str | None = Field(default=None, max_length=255)
    batch_size: int = Field(default=1000, ge=1, le=100000)
    parallelism: int = Field(default=1, ge=1, le=32)
    retry_times: int = Field(default=3, ge=0, le=10)


class QualityContract(BaseModel):
    """Quality contract attached to the pipeline.

    Defines the acceptable error ratio and the dead-letter table
    where rejected rows are routed.
    """

    model_config = ConfigDict(extra="forbid")

    max_error_ratio: float = Field(
        default=0.01,
        ge=0.0,
        le=1.0,
        description="Maximum acceptable error ratio (0.0 = zero tolerance)",
    )
    dead_letter_table: str = Field(
        ...,
        max_length=255,
        description="Table where rejected/bad rows are written",
    )
    dead_letter_schema: str = Field(
        default="public",
        max_length=255,
    )
    rules: list[CleaningRule] = Field(default_factory=list)


class EtlPlan(BaseModel):
    """Logical ETL plan produced by the ``planner`` node.

    This is the central artifact: the LLM generates it from the
    user's requirement + real metadata profiles.  It is then used
    to generate HOCON and to run deterministic gate checks.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    source: SourceConfig
    target: TargetConfig
    field_mappings: list[FieldMapping] = Field(
        ...,
        min_length=1,
        description="At least one field mapping is required",
    )
    cleaning_rules: list[CleaningRule] = Field(default_factory=list)
    sync_strategy: SyncStrategy
    quality_contract: QualityContract | None = None


# =====================================================================
# 3. GeneratedHocon -- SeaTunnel executable config
# =====================================================================


class GeneratedHocon(BaseModel):
    """SeaTunnel HOCON configuration generated from an EtlPlan.

    ``config_text`` is the raw HOCON string.  ``source_table_name``
    and ``result_table_name`` are the SeaTunnel internal identifiers
    used in the HOCON ``source`` and ``sink`` blocks.
    """

    model_config = ConfigDict(extra="forbid")

    config_text: str = Field(
        ...,
        min_length=1,
        description="The full HOCON configuration text",
    )
    source_table_name: str = Field(
        ...,
        max_length=255,
        description="SeaTunnel source_name / table_name in the source block",
    )
    result_table_name: str = Field(
        ...,
        max_length=255,
        description="SeaTunnel result_table_name in the sink block",
    )
    plugin_info: dict[str, str] = Field(
        default_factory=dict,
        description="Source/sink plugin versions (e.g. {'source': 'Jdbc', 'sink': 'Jdbc'})",
    )


# =====================================================================
# 4. ValidationIssue -- gate-check result
# =====================================================================


class ValidationIssue(BaseModel):
    """A single issue found by the deterministic gate checker.

    Attributes:
        severity: ``error`` blocks freezing; ``warning`` is informational.
        code: Machine-readable issue code (see ``ValidationCode``).
        message: Human-readable description.
        suggestion: How to fix the issue (used by the autofix node).
        field: The field or table that caused the issue.
    """

    model_config = ConfigDict(extra="forbid")

    severity: IssueSeverity
    code: str = Field(
        ...,
        max_length=100,
        description="Issue code, e.g. MISSING_PRIMARY_KEY, FIELD_NOT_FOUND",
    )
    message: str = Field(..., max_length=2000)
    suggestion: str | None = Field(default=None, max_length=2000)
    field: str | None = Field(default=None, max_length=255)


# =====================================================================
# Validation code constants
# =====================================================================


class ValidationCode:
    """Machine-readable validation issue codes.

    These are string constants (not an Enum) so that new codes can be
    added without a migration.  The autofix node matches on these
    codes to decide which automatic fixes to apply.
    """

    # HOCON syntax
    HOCON_SYNTAX_ERROR = "HOCON_SYNTAX_ERROR"

    # Source / target existence
    SOURCE_TABLE_NOT_FOUND = "SOURCE_TABLE_NOT_FOUND"
    TARGET_TABLE_NOT_FOUND = "TARGET_TABLE_NOT_FOUND"

    # Field mapping
    FIELD_NOT_FOUND = "FIELD_NOT_FOUND"
    TYPE_MISMATCH = "TYPE_MISMATCH"

    # Incremental sync
    MISSING_PRIMARY_KEY = "MISSING_PRIMARY_KEY"
    MISSING_WATERMARK = "MISSING_WATERMARK"

    # Shadow table
    MISSING_SHADOW_TABLE = "MISSING_SHADOW_TABLE"

    # Quality contract
    MISSING_QUALITY_CONTRACT = "MISSING_QUALITY_CONTRACT"
    MISSING_DEAD_LETTER = "MISSING_DEAD_LETTER"

    # Sensitive fields
    SENSITIVE_FIELD_PLAIN_MAPPING = "SENSITIVE_FIELD_PLAIN_MAPPING"

    # SeaTunnel identifiers
    INVALID_SOURCE_NAME = "INVALID_SOURCE_NAME"
    INVALID_RESULT_TABLE_NAME = "INVALID_RESULT_TABLE_NAME"


__all__ = [
    "CleaningRule",
    "FieldMapping",
    "GeneratedHocon",
    "IssueSeverity",
    "PipelineRequirement",
    "PipelineStatus",
    "QualityContract",
    "SourceConfig",
    "SyncMode",
    "SyncStrategy",
    "TargetConfig",
    "EtlPlan",
    "ValidationCode",
    "ValidationIssue",
]
