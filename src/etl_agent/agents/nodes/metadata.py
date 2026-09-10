"""Metadata node -- fetch real schema profiles for LLM context.

This node is the third step in the LangGraph chain (after intent
parsing and clarification).  It reads the ``metadata_profiles``
table for both the source and target connections, then assembles
a structured ``metadata_context`` dict that the ``planner`` node
passes to the LLM.

The context includes:
- Table names and schema names
- Column names, types, nullable flags, comments
- Masked sample data (sensitive fields are already masked)
- Value distribution for low-cardinality columns

This ensures the LLM generates plans based on **real metadata**
rather than guessing field names and types.

If no metadata profiles exist for a connection, the node returns
an error -- the user must run metadata exploration first.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from etl_agent.agents.state import STATUS_ERROR, STATUS_GENERATING, PipelineState
from etl_agent.db.session import SessionLocal
from etl_agent.models.metadata_profile import MetadataProfile

logger = logging.getLogger(__name__)


def _fetch_profiles_for_connection(
    db: Session,
    connection_id: uuid.UUID,
) -> list[MetadataProfile]:
    """Fetch all metadata profiles for a specific connection.

    Profiles are ordered by table name for stable output.
    """
    return list(
        db.scalars(
            select(MetadataProfile)
            .where(MetadataProfile.connection_id == connection_id)
            .order_by(MetadataProfile.table_name)
        ).all()
    )


def _profile_to_dict(profile: MetadataProfile) -> dict[str, Any]:
    """Convert a MetadataProfile ORM object to a serializable dict.

    Only the fields needed by the LLM are included -- we omit
    internal IDs and tenant/project scoping fields to keep the
    context compact.
    """
    return {
        "schema_name": profile.schema_name,
        "table_name": profile.table_name,
        "columns_info": profile.columns_info or [],
        "stats_info": profile.stats_info or {},
        "value_distribution": profile.value_distribution or [],
        "sample_data": profile.sample_data or [],
    }


def _find_table_profile(
    profiles: list[dict[str, Any]],
    table_name: str | None,
) -> dict[str, Any] | None:
    """Find the profile matching a specific table name.

    Does a case-insensitive match since databases may have
    different case sensitivity rules.
    """
    if table_name is None:
        return None
    target = table_name.lower()
    for p in profiles:
        if p["table_name"].lower() == target:
            return p
    return None


def metadata_node(state: PipelineState) -> dict[str, Any]:
    """Fetch metadata profiles for source and target connections.

    Reads ``requirement`` from the state to determine which
    connections and tables to look up.  Stores the result as
    ``metadata_context`` with ``source`` and ``target`` keys.

    Returns a partial state dict for LangGraph to merge.
    """
    requirement = state.get("requirement")
    if requirement is None:
        return {"error": "requirement is not set", "status": STATUS_ERROR}

    source_conn_id = requirement.source_connection_id
    target_conn_id = requirement.target_connection_id
    source_table = requirement.source_table
    target_table = requirement.target_table

    db = SessionLocal()
    try:
        # Fetch all profiles for both connections
        source_profiles = _fetch_profiles_for_connection(db, source_conn_id)
        target_profiles = _fetch_profiles_for_connection(db, target_conn_id)

        if not source_profiles:
            return {
                "error": (
                    f"No metadata profiles found for source connection "
                    f"{source_conn_id}. Please run metadata exploration first."
                ),
                "status": STATUS_ERROR,
            }

        if not target_profiles:
            return {
                "error": (
                    f"No metadata profiles found for target connection "
                    f"{target_conn_id}. Please run metadata exploration first."
                ),
                "status": STATUS_ERROR,
            }

        # Convert to dicts
        source_dicts = [_profile_to_dict(p) for p in source_profiles]
        target_dicts = [_profile_to_dict(p) for p in target_profiles]

        # Find the specific source and target table profiles
        source_table_profile = _find_table_profile(source_dicts, source_table)
        target_table_profile = _find_table_profile(target_dicts, target_table)

        # Build the context for the LLM
        # Include all tables (for discovery) + highlight the specific table
        metadata_context: dict[str, Any] = {
            "source": {
                "connection_id": str(source_conn_id),
                "all_tables": source_dicts,
                "selected_table": source_table_profile or source_dicts[0],
            },
            "target": {
                "connection_id": str(target_conn_id),
                "all_tables": target_dicts,
                "selected_table": target_table_profile or target_dicts[0],
            },
        }

        # Warn if the requested table is not found
        warnings: list[str] = []
        if source_table and source_table_profile is None:
            warnings.append(
                f"Source table '{source_table}' not found in metadata profiles. "
                f"Available tables: {[t['table_name'] for t in source_dicts]}"
            )
        if target_table and target_table_profile is None:
            warnings.append(
                f"Target table '{target_table}' not found in metadata profiles. "
                f"Available tables: {[t['table_name'] for t in target_dicts]}"
            )

        logger.info(
            "Metadata node: fetched %d source tables, %d target tables",
            len(source_dicts),
            len(target_dicts),
        )

        result: dict[str, Any] = {
            "metadata_context": metadata_context,
            "status": STATUS_GENERATING,
        }
        if warnings:
            result["metadata_warnings"] = warnings
        return result

    except Exception as exc:
        logger.error("Metadata node failed: %s", exc)
        return {"error": f"Metadata fetch failed: {exc}", "status": STATUS_ERROR}
    finally:
        db.close()


__all__ = ["metadata_node"]
