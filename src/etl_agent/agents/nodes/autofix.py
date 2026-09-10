r"""Autofix node -- limited automatic repair of validation issues.

This is the seventh node in the LangGraph chain (after quality
checker).  It reads the ``validation_issues`` produced by the
quality checker and attempts to fix **error**-severity issues
using deterministic rules -- no LLM calls for simple fixes.

Deterministic fixes applied (only for clear-rule errors):

1. **MISSING_PRIMARY_KEY** -- infer PK from metadata (column named
   ``id``, ``{table}_id``, or first non-nullable column).
2. **FIELD_NOT_FOUND** -- case-insensitive match against real
   column names in the metadata profile.
3. **SOURCE_TABLE_NOT_FOUND** / **TARGET_TABLE_NOT_FOUND** --
   case-insensitive match against ``all_tables`` in metadata.
4. **MISSING_DEAD_LETTER** -- default to ``{target_table}_dl``.
5. **MISSING_QUALITY_CONTRACT** -- add a default contract.
6. **MISSING_SHADOW_TABLE** (warning) -- default to
   ``{target_table}_shadow``.
7. **MISSING_WATERMARK** (warning) -- infer ``updated_at`` or
   similar from metadata.
8. **SENSITIVE_FIELD_PLAIN_MAPPING** (warning) -- add
   ``transform='mask'``.
9. **INVALID_SOURCE_NAME** / **INVALID_RESULT_TABLE_NAME** -- fix
   from the EtlPlan table names.

For **HOCON_SYNTAX_ERROR** that cannot be auto-fixed by rules,
the node regenerates the HOCON from the (potentially patched)
EtlPlan via the LLM service.

If the plan was modified and the HOCON depends on plan fields
(e.g. table names changed), the HOCON is regenerated.

After fixing, the graph routes back to ``quality_checker`` for
re-validation.  If ``autofix_attempts`` reaches
``MAX_AUTOFIX_ATTEMPTS`` or no fixes can be applied, the node
sets ``status=needs_review`` to escalate to human confirmation.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from etl_agent.agents.nodes.quality_checker import _is_sensitive_field
from etl_agent.agents.state import (
    MAX_AUTOFIX_ATTEMPTS,
    STATUS_NEEDS_REVIEW,
    STATUS_VALIDATED,
    PipelineState,
)
from etl_agent.schemas.generation import (
    EtlPlan,
    GeneratedHocon,
    IssueSeverity,
    QualityContract,
    ValidationCode,
    ValidationIssue,
)
from etl_agent.services.llm_service import LLMError, get_llm_service

logger = logging.getLogger(__name__)

# Candidate column names for primary key inference, in priority order.
_PK_CANDIDATES = ("id", "{table}_id", "{table}id", "pk", "uid", "guid")

# Candidate column names for watermark inference, in priority order.
_WATERMARK_CANDIDATES = (
    "updated_at",
    "update_time",
    "last_modified",
    "modified_at",
    "gmt_modified",
    "last_updated",
)


def _get_source_columns(metadata_context: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the source table's columns_info list from metadata."""
    return cast(
        list[dict[str, Any]],
        metadata_context.get("source", {}).get("selected_table", {}).get("columns_info", []),
    )


def _get_target_columns(metadata_context: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the target table's columns_info list from metadata."""
    return cast(
        list[dict[str, Any]],
        metadata_context.get("target", {}).get("selected_table", {}).get("columns_info", []),
    )


def _get_source_all_tables(metadata_context: dict[str, Any]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], metadata_context.get("source", {}).get("all_tables", []))


def _get_target_all_tables(metadata_context: dict[str, Any]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], metadata_context.get("target", {}).get("all_tables", []))


# =====================================================================
# Fix 1: Infer primary key from metadata
# =====================================================================


def _infer_primary_key(
    metadata_context: dict[str, Any],
    table_name: str,
) -> str | None:
    """Try to infer a primary key column from the metadata.

    Priority:
    1. Column named ``id``
    2. Column named ``{table}_id``
    3. First non-nullable column
    """
    columns = _get_source_columns(metadata_context)
    if not columns:
        return None

    col_names = {c.get("name", "").lower(): c for c in columns if c.get("name")}

    # Try each candidate pattern
    for pattern in _PK_CANDIDATES:
        candidate = pattern.replace("{table}", table_name.lower())
        if candidate in col_names:
            col = col_names[candidate]
            if not col.get("nullable", True):
                return cast(str, col["name"])

    # Fallback: first non-nullable column
    for col in columns:
        if not col.get("nullable", True) and col.get("name"):
            return cast(str, col["name"])

    # Last resort: column named "id" even if nullable
    if "id" in col_names:
        return cast(str, col_names["id"]["name"])

    return None


# =====================================================================
# Fix 2: Infer watermark column from metadata
# =====================================================================


def _infer_watermark(
    metadata_context: dict[str, Any],
) -> str | None:
    """Try to infer a watermark column from the metadata."""
    columns = _get_source_columns(metadata_context)
    if not columns:
        return None

    col_names_lower = {c.get("name", "").lower(): c["name"] for c in columns if c.get("name")}

    for candidate in _WATERMARK_CANDIDATES:
        if candidate in col_names_lower:
            return cast(str, col_names_lower[candidate])

    return None


# =====================================================================
# Fix 3: Case-insensitive table name match
# =====================================================================


def _fix_table_name_case(
    requested: str,
    all_tables: list[dict[str, Any]],
) -> str | None:
    """Find a case-insensitive match for *requested* in *all_tables*.

    Returns the correctly-cased table name, or None if no match.
    """
    for t in all_tables:
        real_name = t.get("table_name", "")
        if real_name.lower() == requested.lower():
            return cast(str, real_name)
    return None


# =====================================================================
# Fix 4: Case-insensitive field name match
# =====================================================================


def _fix_field_name_case(
    field: str,
    real_columns: list[dict[str, Any]],
) -> str | None:
    """Find a case-insensitive match for *field* in *real_columns*.

    Returns the correctly-cased column name, or None.
    """
    for col in real_columns:
        real_name = col.get("name", "")
        if real_name.lower() == field.lower():
            return cast(str, real_name)
    return None


# =====================================================================
# Fix 5: Patch HOCON table name identifiers
# =====================================================================


def _patch_hocon_identifiers(
    hocon: GeneratedHocon,
    etl_plan: EtlPlan,
) -> GeneratedHocon:
    """Fix source_table_name and result_table_name in the HOCON.

    If the HOCON's internal identifiers don't match the EtlPlan's
    table names, correct them and do a string replacement in the
    config text.
    """
    config_text = hocon.config_text
    source_table = etl_plan.source.table_name
    target_table = etl_plan.target.table_name

    # Fix source_table_name
    if hocon.source_table_name != source_table:
        config_text = config_text.replace(hocon.source_table_name, source_table)
        hocon = hocon.model_copy(
            update={"source_table_name": source_table, "config_text": config_text}
        )

    # Fix result_table_name (should be target table or a derived name)
    expected_result = f"{target_table}_result"
    if hocon.result_table_name != target_table and hocon.result_table_name != expected_result:
        config_text = config_text.replace(hocon.result_table_name, target_table)
        hocon = hocon.model_copy(
            update={"result_table_name": target_table, "config_text": config_text}
        )

    return hocon


# =====================================================================
# Main: apply all deterministic fixes
# =====================================================================


def _apply_deterministic_fixes(
    plan: EtlPlan,
    hocon: GeneratedHocon | None,
    issues: list[ValidationIssue],
    metadata_context: dict[str, Any],
) -> tuple[EtlPlan, GeneratedHocon | None, bool, bool]:
    """Apply all deterministic fixes to the EtlPlan and/or HOCON.

    Returns ``(new_plan, new_hocon, plan_modified, hocon_modified)``.
    """
    plan_modified = False
    hocon_modified = False

    source_columns = _get_source_columns(metadata_context)
    target_columns = _get_target_columns(metadata_context)
    source_all = _get_source_all_tables(metadata_context)
    target_all = _get_target_all_tables(metadata_context)

    # ---- Fix error-severity issues ----
    error_codes = {i.code for i in issues if i.severity == IssueSeverity.error}

    # 1. MISSING_PRIMARY_KEY
    if ValidationCode.MISSING_PRIMARY_KEY in error_codes:
        pk = _infer_primary_key(metadata_context, plan.source.table_name)
        if pk and not plan.sync_strategy.primary_keys:
            plan.sync_strategy.primary_keys = [pk]
            plan_modified = True
            logger.info("Autofix: inferred primary key '%s' from metadata", pk)

    # 2. FIELD_NOT_FOUND -- fix case mismatches
    if ValidationCode.FIELD_NOT_FOUND in error_codes:
        for mapping in plan.field_mappings:
            # Fix source field case
            fixed_src = _fix_field_name_case(mapping.source_field, source_columns)
            if fixed_src and fixed_src != mapping.source_field:
                mapping.source_field = fixed_src
                plan_modified = True
                logger.info(
                    "Autofix: corrected source field '%s' -> '%s'",
                    mapping.source_field if not plan_modified else fixed_src,
                    fixed_src,
                )
            # Fix target field case
            fixed_tgt = _fix_field_name_case(mapping.target_field, target_columns)
            if fixed_tgt and fixed_tgt != mapping.target_field:
                mapping.target_field = fixed_tgt
                plan_modified = True
                logger.info("Autofix: corrected target field -> '%s'", fixed_tgt)

    # 3. SOURCE_TABLE_NOT_FOUND -- fix case
    if ValidationCode.SOURCE_TABLE_NOT_FOUND in error_codes:
        fixed = _fix_table_name_case(plan.source.table_name, source_all)
        if fixed and fixed != plan.source.table_name:
            plan.source.table_name = fixed
            plan_modified = True
            logger.info("Autofix: corrected source table -> '%s'", fixed)

    # 4. TARGET_TABLE_NOT_FOUND -- fix case
    if ValidationCode.TARGET_TABLE_NOT_FOUND in error_codes:
        fixed = _fix_table_name_case(plan.target.table_name, target_all)
        if fixed and fixed != plan.target.table_name:
            plan.target.table_name = fixed
            plan_modified = True
            logger.info("Autofix: corrected target table -> '%s'", fixed)

    # 5. MISSING_DEAD_LETTER
    if (
        ValidationCode.MISSING_DEAD_LETTER in error_codes
        and plan.quality_contract
        and not plan.quality_contract.dead_letter_table
    ):
        plan.quality_contract.dead_letter_table = f"{plan.target.table_name}_dl"
        plan_modified = True
        logger.info(
            "Autofix: set dead_letter_table to '%s'", plan.quality_contract.dead_letter_table
        )

    # ---- Fix warning-severity issues (clear rules only) ----
    warning_codes = {i.code for i in issues if i.severity == IssueSeverity.warning}

    # 6. MISSING_QUALITY_CONTRACT (warning)
    if ValidationCode.MISSING_QUALITY_CONTRACT in warning_codes and plan.quality_contract is None:
        plan.quality_contract = QualityContract(
            max_error_ratio=0.01,
            dead_letter_table=f"{plan.target.table_name}_dl",
        )
        plan_modified = True
        logger.info("Autofix: added default quality contract")

    # 7. MISSING_SHADOW_TABLE
    if ValidationCode.MISSING_SHADOW_TABLE in warning_codes and not plan.target.shadow_table:
        plan.target.shadow_table = f"{plan.target.table_name}_shadow"
        plan_modified = True
        logger.info("Autofix: set shadow_table to '%s'", plan.target.shadow_table)

    # 8. MISSING_WATERMARK
    if ValidationCode.MISSING_WATERMARK in warning_codes:
        wm = _infer_watermark(metadata_context)
        if wm and not plan.sync_strategy.watermark_column:
            plan.sync_strategy.watermark_column = wm
            plan_modified = True
            logger.info("Autofix: inferred watermark column '%s'", wm)

    # 9. SENSITIVE_FIELD_PLAIN_MAPPING
    all_issue_codes = {i.code for i in issues}
    if ValidationCode.SENSITIVE_FIELD_PLAIN_MAPPING in all_issue_codes:
        for mapping in plan.field_mappings:
            if _is_sensitive_field(mapping.source_field) and not mapping.transform:
                mapping.transform = "mask"
                plan_modified = True
                logger.info(
                    "Autofix: added 'mask' transform to sensitive field '%s'", mapping.source_field
                )

    # 10. INVALID_SOURCE_NAME / INVALID_RESULT_TABLE_NAME
    if hocon is not None and (
        ValidationCode.INVALID_SOURCE_NAME in error_codes
        or ValidationCode.INVALID_RESULT_TABLE_NAME in error_codes
    ):
        new_hocon = _patch_hocon_identifiers(hocon, plan)
        if new_hocon != hocon:
            hocon = new_hocon
            hocon_modified = True
            logger.info("Autofix: patched HOCON identifiers")

    return plan, hocon, plan_modified, hocon_modified


# =====================================================================
# Node entry point
# =====================================================================


def autofix_node(state: PipelineState) -> dict[str, Any]:
    """Attempt to automatically fix validation issues.

    This node:
    1. Reads ``validation_issues``, ``etl_plan``, ``hocon_config``,
       ``autofix_attempts``, and ``metadata_context``.
    2. Applies deterministic fixes for clear-rule errors.
    3. If the plan was modified and HOCON syntax errors exist,
       regenerates the HOCON via the LLM.
    4. Returns patched ``etl_plan`` and/or ``hocon_config`` for
       re-validation by the quality checker.
    5. If no fixes can be applied or max attempts reached, sets
       ``status=needs_review`` to escalate to human confirmation.

    The graph routes back to ``quality_checker`` after a successful
    fix so the patched plan can be re-validated.

    Returns a partial state dict for LangGraph to merge.
    """
    etl_plan: EtlPlan | None = state.get("etl_plan")
    hocon_config: GeneratedHocon | None = state.get("hocon_config")
    validation_issues: list[ValidationIssue] = state.get("validation_issues", [])
    metadata_context = state.get("metadata_context", {})
    attempts = state.get("autofix_attempts", 0)

    if etl_plan is None:
        return {"error": "etl_plan is not set", "status": "error"}

    # ---- Check attempt limit ----
    if attempts >= MAX_AUTOFIX_ATTEMPTS:
        logger.warning(
            "Autofix: max attempts (%d) reached, escalating to human review",
            MAX_AUTOFIX_ATTEMPTS,
        )
        return {
            "status": STATUS_NEEDS_REVIEW,
            "autofix_attempts": attempts,
        }

    # ---- Only fix error-severity issues (warnings are informational) ----
    errors = [i for i in validation_issues if i.severity == IssueSeverity.error]
    if not errors:
        # No errors to fix — quality checker should not have routed here
        logger.info("Autofix: no error-severity issues, nothing to fix")
        return {"status": STATUS_VALIDATED}

    # ---- Apply deterministic fixes ----
    new_plan, new_hocon, plan_modified, hocon_modified = _apply_deterministic_fixes(
        etl_plan.model_copy(deep=True),
        hocon_config,
        validation_issues,
        metadata_context,
    )

    # ---- Handle HOCON_SYNTAX_ERROR: regenerate if plan was fixed ----
    has_hocon_syntax_error = any(
        i.code == ValidationCode.HOCON_SYNTAX_ERROR and i.severity == IssueSeverity.error
        for i in validation_issues
    )

    if has_hocon_syntax_error and (plan_modified or hocon_modified):
        # Regenerate HOCON from the patched plan
        try:
            llm = get_llm_service()
            new_hocon = llm.generate_hocon(new_plan)
            hocon_modified = True
            logger.info("Autofix: regenerated HOCON after plan fixes")
        except LLMError as exc:
            logger.warning("Autofix: HOCON regeneration failed: %s", exc)
            # Can't fix HOCON syntax — will escalate to human review

    # ---- Also regenerate HOCON if plan table names changed ----
    if plan_modified and not hocon_modified and new_hocon is not None:
        # Patch identifiers in existing HOCON text
        patched = _patch_hocon_identifiers(new_hocon, new_plan)
        if patched != new_hocon:
            new_hocon = patched
            hocon_modified = True

    # ---- Build result ----
    result: dict[str, Any] = {"autofix_attempts": attempts + 1}

    if plan_modified:
        result["etl_plan"] = new_plan
    if hocon_modified and new_hocon is not None:
        result["hocon_config"] = new_hocon

    # ---- If no fixes could be applied, escalate ----
    if not plan_modified and not hocon_modified:
        logger.warning(
            "Autofix: no deterministic fixes could be applied for issues: %s",
            [i.code for i in errors],
        )
        return {
            "status": STATUS_NEEDS_REVIEW,
            "autofix_attempts": attempts + 1,
            "error": "Automatic fixes could not resolve all validation errors",
        }

    # Log what was fixed
    fixed_desc = []
    if plan_modified:
        fixed_desc.append("etl_plan")
    if hocon_modified:
        fixed_desc.append("hocon_config")
    logger.info(
        "Autofix: applied fixes to %s (attempt %d/%d)",
        " + ".join(fixed_desc),
        attempts + 1,
        MAX_AUTOFIX_ATTEMPTS,
    )

    # Status stays as generating — graph will route back to quality_checker
    result["status"] = "generating"

    return result


__all__ = ["autofix_node"]
