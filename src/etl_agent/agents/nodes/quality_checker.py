"""Quality checker node -- deterministic gate validation for generated pipelines.

This node is the sixth step in the LangGraph chain (after HOCON
generation).  It performs **deterministic** validation checks that
do NOT rely on the LLM -- these are hard rules that the generated
EtlPlan and HOCON must satisfy before the pipeline can be frozen.

Validation checks (each produces zero or more ``ValidationIssue``):

1. **HOCON syntax** -- basic structural validity (balanced braces).
2. **Source table existence** -- the source table in the EtlPlan
   must exist in the real metadata profiles.
3. **Target table existence** -- same for the target table.
4. **Field mapping existence** -- every ``source_field`` in the
   EtlPlan must exist in the source metadata, and every
   ``target_field`` must exist in the target metadata.
5. **Data type matching** -- source and target types should be
   compatible (with known widening conversions).
6. **Incremental sync primary key** -- incremental sync must have
   at least one primary key.
7. **Incremental sync watermark** -- incremental sync must have
   a watermark column.
8. **Shadow table strategy** -- full sync should specify a shadow
   table for atomic swap.
9. **Quality contract** -- a quality contract with dead-letter table
   should be present.
10. **Sensitive field plain mapping** -- columns matching sensitive
    patterns (password, secret, token, etc.) should not be mapped
    without a masking transform.

Issues with ``severity=error`` block freezing; ``warning`` is
informational.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from etl_agent.agents.state import (
    STATUS_ERROR,
    STATUS_NEEDS_REVIEW,
    STATUS_VALIDATED,
    PipelineState,
)
from etl_agent.schemas.generation import (
    EtlPlan,
    GeneratedHocon,
    IssueSeverity,
    SyncMode,
    ValidationCode,
    ValidationIssue,
)

logger = logging.getLogger(__name__)

# =====================================================================
# Sensitive column patterns (reused from metadata_service logic)
# =====================================================================

_SENSITIVE_PATTERNS = re.compile(
    r"(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|credential|ssn|national[_-]?id|"
    r"phone|mobile|email|address|id[_-]?card)",
    re.IGNORECASE,
)

# Known compatible type widening rules.
# Source type -> set of acceptable target types.
_TYPE_WIDENING: dict[str, set[str]] = {
    "int": {"int", "bigint", "long", "decimal", "float", "double", "varchar", "text"},
    "integer": {"int", "bigint", "long", "decimal", "float", "double", "varchar", "text"},
    "smallint": {
        "int",
        "integer",
        "bigint",
        "long",
        "decimal",
        "float",
        "double",
        "varchar",
        "text",
    },
    "bigint": {"bigint", "long", "decimal", "varchar", "text"},
    "long": {"bigint", "long", "decimal", "varchar", "text"},
    "varchar": {"varchar", "text", "string"},
    "char": {"char", "varchar", "text", "string"},
    "text": {"text", "varchar", "string"},
    "string": {"string", "text", "varchar"},
    "float": {"float", "double", "decimal"},
    "double": {"double", "decimal"},
    "decimal": {"decimal", "numeric"},
    "numeric": {"numeric", "decimal"},
    "date": {"date", "datetime", "timestamp"},
    "datetime": {"datetime", "timestamp"},
    "timestamp": {"timestamp", "datetime"},
    "boolean": {"boolean", "bool"},
    "bool": {"bool", "boolean"},
}


def _normalize_type(t: str) -> str:
    """Normalize a type string for comparison.

    Strips length/precision qualifiers: ``varchar(255)`` -> ``varchar``,
    ``decimal(10,2)`` -> ``decimal``.
    """
    t = t.strip().lower()
    # Strip parenthetical qualifiers
    if "(" in t:
        t = t[: t.index("(")]
    return t


def _is_type_compatible(source_type: str, target_type: str) -> bool:
    """Check if source type can be widened to target type.

    Returns True if the types are directly equal, or if the source
    type is in the widening set for the target type, or vice versa.
    """
    s = _normalize_type(source_type)
    t = _normalize_type(target_type)
    if s == t:
        return True
    # Check if target can accept source
    if t in _TYPE_WIDENING and s in _TYPE_WIDENING[t]:
        return True
    # Check reverse direction (source can be narrowed to target)
    return s in _TYPE_WIDENING and t in _TYPE_WIDENING[s]


def _is_sensitive_field(field_name: str) -> bool:
    """Check if a field name matches sensitive patterns."""
    return bool(_SENSITIVE_PATTERNS.search(field_name))


def _check_hocon_syntax(hocon_text: str) -> ValidationIssue | None:
    """Basic HOCON syntax check: balanced braces.

    This is NOT a full HOCON parser -- it catches the most common
    structural errors (unbalanced braces, empty config).
    """
    if not hocon_text or not hocon_text.strip():
        return ValidationIssue(
            severity=IssueSeverity.error,
            code=ValidationCode.HOCON_SYNTAX_ERROR,
            message="HOCON config is empty",
            suggestion="Regenerate the HOCON configuration",
        )

    # Count opening and closing braces
    open_braces = hocon_text.count("{")
    close_braces = hocon_text.count("}")
    if open_braces != close_braces:
        return ValidationIssue(
            severity=IssueSeverity.error,
            code=ValidationCode.HOCON_SYNTAX_ERROR,
            message=(
                f"HOCON syntax error: unbalanced braces ({open_braces} open, {close_braces} close)"
            ),
            suggestion="Fix the HOCON to have matching open and close braces",
        )

    return None


def _get_column_names(profile: dict[str, Any]) -> set[str]:
    """Extract column names from a metadata profile dict."""
    columns = profile.get("columns_info", [])
    return {c.get("name", "").lower() for c in columns if c.get("name")}


def _get_column_types(profile: dict[str, Any]) -> dict[str, str]:
    """Extract column name -> type mapping from a metadata profile."""
    columns = profile.get("columns_info", [])
    result: dict[str, str] = {}
    for c in columns:
        name = c.get("name", "")
        if name:
            result[name.lower()] = c.get("type", "unknown")
    return result


def quality_checker_node(state: PipelineState) -> dict[str, Any]:
    """Run deterministic gate validation on the generated EtlPlan + HOCON.

    This node:
    1. Reads ``etl_plan``, ``hocon_config``, and ``metadata_context``.
    2. Runs all deterministic validation checks.
    3. Sets ``validation_issues`` with the list of findings.
    4. Sets ``status`` to ``validated`` (no errors) or ``needs_review``
       (has errors).

    Returns a partial state dict for LangGraph to merge.
    """
    etl_plan: EtlPlan | None = state.get("etl_plan")
    hocon_config: GeneratedHocon | None = state.get("hocon_config")
    metadata_context = state.get("metadata_context")

    if etl_plan is None:
        return {"error": "etl_plan is not set", "status": STATUS_ERROR}
    if hocon_config is None:
        return {"error": "hocon_config is not set", "status": STATUS_ERROR}
    if metadata_context is None:
        return {"error": "metadata_context is not set", "status": STATUS_ERROR}

    issues: list[ValidationIssue] = []

    # Get source and target metadata
    source_profile = metadata_context.get("source", {}).get("selected_table", {})
    target_profile = metadata_context.get("target", {}).get("selected_table", {})
    source_columns = _get_column_names(source_profile)
    target_columns = _get_column_names(target_profile)
    source_types = _get_column_types(source_profile)
    target_types = _get_column_types(target_profile)

    # ---- 1. HOCON syntax ----
    syntax_issue = _check_hocon_syntax(hocon_config.config_text)
    if syntax_issue:
        issues.append(syntax_issue)

    # ---- 2. Source table existence ----
    source_table = etl_plan.source.table_name
    source_tables_all = {
        t.get("table_name", "").lower()
        for t in metadata_context.get("source", {}).get("all_tables", [])
    }
    if source_table.lower() not in source_tables_all:
        issues.append(
            ValidationIssue(
                severity=IssueSeverity.error,
                code=ValidationCode.SOURCE_TABLE_NOT_FOUND,
                message=f"Source table '{source_table}' not found in metadata profiles",
                suggestion=f"Available tables: {sorted(source_tables_all)}",
                field=source_table,
            )
        )

    # ---- 3. Target table existence ----
    target_table = etl_plan.target.table_name
    target_tables_all = {
        t.get("table_name", "").lower()
        for t in metadata_context.get("target", {}).get("all_tables", [])
    }
    if target_table.lower() not in target_tables_all:
        issues.append(
            ValidationIssue(
                severity=IssueSeverity.error,
                code=ValidationCode.TARGET_TABLE_NOT_FOUND,
                message=f"Target table '{target_table}' not found in metadata profiles",
                suggestion=f"Available tables: {sorted(target_tables_all)}",
                field=target_table,
            )
        )

    # ---- 4. Field mapping existence + 5. Type matching + 10. Sensitive fields ----
    for mapping in etl_plan.field_mappings:
        src_field = mapping.source_field
        tgt_field = mapping.target_field

        # Source field must exist
        if src_field.lower() not in source_columns:
            issues.append(
                ValidationIssue(
                    severity=IssueSeverity.error,
                    code=ValidationCode.FIELD_NOT_FOUND,
                    message=(
                        f"Source field '{src_field}' not found in source schema "
                        f"(table '{source_table}')"
                    ),
                    suggestion=f"Available columns: {sorted(source_columns)}",
                    field=src_field,
                )
            )

        # Target field must exist
        if tgt_field.lower() not in target_columns:
            issues.append(
                ValidationIssue(
                    severity=IssueSeverity.error,
                    code=ValidationCode.FIELD_NOT_FOUND,
                    message=(
                        f"Target field '{tgt_field}' not found in target schema "
                        f"(table '{target_table}')"
                    ),
                    suggestion=f"Available columns: {sorted(target_columns)}",
                    field=tgt_field,
                )
            )

        # Type compatibility (only if both fields exist)
        if src_field.lower() in source_types and tgt_field.lower() in target_types:
            src_type = source_types[src_field.lower()]
            tgt_type = target_types[tgt_field.lower()]
            if not _is_type_compatible(src_type, tgt_type):
                issues.append(
                    ValidationIssue(
                        severity=IssueSeverity.warning,
                        code=ValidationCode.TYPE_MISMATCH,
                        message=(
                            f"Type mismatch: field '{src_field}' ({src_type}) "
                            f"-> '{tgt_field}' ({tgt_type})"
                        ),
                        suggestion=(
                            f"Consider casting {src_type} to {tgt_type} in the transform block"
                        ),
                        field=src_field,
                    )
                )

        # Sensitive field plain mapping (no transform)
        if _is_sensitive_field(src_field) and not mapping.transform:
            issues.append(
                ValidationIssue(
                    severity=IssueSeverity.error,
                    code=ValidationCode.SENSITIVE_FIELD_PLAIN_MAPPING,
                    message=(
                        f"Sensitive field '{src_field}' is mapped without a masking transform"
                    ),
                    suggestion=(
                        "Apply a masking transform (e.g. 'mask' or 'hash') "
                        "to this field in the field mapping"
                    ),
                    field=src_field,
                )
            )

    # ---- 6. Incremental sync primary key ----
    strategy = etl_plan.sync_strategy
    if strategy.mode == SyncMode.incremental:
        if not strategy.primary_keys:
            issues.append(
                ValidationIssue(
                    severity=IssueSeverity.error,
                    code=ValidationCode.MISSING_PRIMARY_KEY,
                    message="Incremental sync requires primary key(s) for deduplication",
                    suggestion=(
                        "Add primary key column(s) to the sync strategy or switch to full sync"
                    ),
                )
            )

        # ---- 7. Incremental sync watermark ----
        if not strategy.watermark_column:
            issues.append(
                ValidationIssue(
                    severity=IssueSeverity.warning,
                    code=ValidationCode.MISSING_WATERMARK,
                    message=(
                        "Incremental sync has no watermark column -- "
                        "full table scan will occur on each run"
                    ),
                    suggestion=(
                        "Specify a watermark column (e.g. 'updated_at') "
                        "for efficient incremental CDC"
                    ),
                )
            )

    # ---- 8. Shadow table strategy (full sync) ----
    if strategy.mode == SyncMode.full and not etl_plan.target.shadow_table:
        issues.append(
            ValidationIssue(
                severity=IssueSeverity.warning,
                code=ValidationCode.MISSING_SHADOW_TABLE,
                message=(
                    "Full sync without shadow table -- "
                    "data will be visible during load (non-atomic)"
                ),
                suggestion=(
                    "Set target.shadow_table for atomic swap strategy (e.g. 'users_shadow')"
                ),
            )
        )

    # ---- 9. Quality contract + dead letter ----
    qc = etl_plan.quality_contract
    if qc is None:
        issues.append(
            ValidationIssue(
                severity=IssueSeverity.warning,
                code=ValidationCode.MISSING_QUALITY_CONTRACT,
                message="No quality contract defined -- rejected rows will be lost",
                suggestion="Add a quality_contract with max_error_ratio and dead_letter_table",
            )
        )
    else:
        if not qc.dead_letter_table:
            issues.append(
                ValidationIssue(
                    severity=IssueSeverity.error,
                    code=ValidationCode.MISSING_DEAD_LETTER,
                    message="Quality contract has no dead_letter_table",
                    suggestion="Specify a dead_letter_table for rejected rows",
                )
            )

    # ---- Determine status ----
    has_errors = any(i.severity == IssueSeverity.error for i in issues)
    if has_errors:
        status = STATUS_NEEDS_REVIEW
        logger.info(
            "Quality checker: %d issues found (%d errors), status=needs_review",
            len(issues),
            sum(1 for i in issues if i.severity == IssueSeverity.error),
        )
    else:
        status = STATUS_VALIDATED
        logger.info(
            "Quality checker: %d issues found (0 errors), status=validated",
            len(issues),
        )

    return {
        "validation_issues": issues,
        "status": status,
    }


__all__ = ["quality_checker_node"]
