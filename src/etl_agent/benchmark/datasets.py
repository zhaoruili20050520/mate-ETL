"""Benchmark test-case datasets for L0, L1, and L2 levels.

Each dataset is a list of dictionaries with a consistent schema:

- ``case_id``: Unique identifier string.
- ``description``: Human-readable test purpose.
- ``natural_language``: The NL input text.
- ``hints``: Optional structured hints dict.
- ``expect``: Expected outcome dict (fields vary by level).
- ``fault``: (L1 only) Fault injection specification.
- ``requires_infra``: (L2 only) Whether real DB connections are needed.
"""

from __future__ import annotations

from typing import Any

# =====================================================================
# L0 -- Static injection test cases
# =====================================================================

L0_CASES: list[dict[str, Any]] = [
    {
        "case_id": "L0-001",
        "description": "Full sync, MySQL to Doris, simple table",
        "natural_language": "Sync the orders table from MySQL to Doris in full mode",
        "hints": {
            "sync_mode": "full",
            "source_table": "orders",
            "target_table": "orders",
            "primary_keys": ["id"],
        },
        "expect": {
            "sync_mode": "full",
            "source_table": "orders",
            "target_table": "orders",
            "has_etl_plan": True,
            "has_hocon": True,
            "hocon_contains": ["source", "sink", "Jdbc"],
        },
    },
    {
        "case_id": "L0-002",
        "description": "Incremental sync with watermark",
        "natural_language": (
            "Incrementally sync the users table from MySQL to Doris "
            "using updated_at as the watermark column"
        ),
        "hints": {
            "sync_mode": "incremental",
            "source_table": "users",
            "target_table": "users",
            "primary_keys": ["id"],
            "watermark_column": "updated_at",
        },
        "expect": {
            "sync_mode": "incremental",
            "source_table": "users",
            "target_table": "users",
            "watermark_column": "updated_at",
            "has_etl_plan": True,
            "has_hocon": True,
        },
    },
    {
        "case_id": "L0-003",
        "description": "Full sync with column rename",
        "natural_language": (
            "Full sync products table from MySQL to Doris, "
            "rename column product_name to name in target"
        ),
        "hints": {
            "sync_mode": "full",
            "source_table": "products",
            "target_table": "products",
            "primary_keys": ["id"],
        },
        "expect": {
            "sync_mode": "full",
            "source_table": "products",
            "target_table": "products",
            "has_etl_plan": True,
            "has_hocon": True,
        },
    },
    {
        "case_id": "L0-004",
        "description": "Full sync with sensitive field masking",
        "natural_language": (
            "Full sync the customer table from MySQL to Doris, mask the phone column"
        ),
        "hints": {
            "sync_mode": "full",
            "source_table": "customer",
            "target_table": "customer",
            "primary_keys": ["id"],
        },
        "expect": {
            "sync_mode": "full",
            "source_table": "customer",
            "target_table": "customer",
            "has_etl_plan": True,
            "has_hocon": True,
        },
    },
    {
        "case_id": "L0-005",
        "description": "Incremental sync with composite primary key",
        "natural_language": (
            "Incrementally sync order_items from MySQL to Doris, "
            "primary key is (order_id, item_id), watermark is updated_at"
        ),
        "hints": {
            "sync_mode": "incremental",
            "source_table": "order_items",
            "target_table": "order_items",
            "primary_keys": ["order_id", "item_id"],
            "watermark_column": "updated_at",
        },
        "expect": {
            "sync_mode": "incremental",
            "source_table": "order_items",
            "target_table": "order_items",
            "primary_keys_contains": ["order_id", "item_id"],
            "watermark_column": "updated_at",
            "has_etl_plan": True,
            "has_hocon": True,
        },
    },
    {
        "case_id": "L0-006",
        "description": "Full sync minimal NL, no hints",
        "natural_language": "Full sync the events table from MySQL to Doris",
        "hints": {},
        "expect": {
            "sync_mode": "full",
            "has_etl_plan": True,
            "has_hocon": True,
        },
    },
    {
        "case_id": "L0-007",
        "description": "Full sync with quality rules",
        "natural_language": (
            "Full sync the payments table from MySQL to Doris, "
            "ensure amount is not null and status must be in "
            "(pending, paid, failed)"
        ),
        "hints": {
            "sync_mode": "full",
            "source_table": "payments",
            "target_table": "payments",
            "primary_keys": ["id"],
        },
        "expect": {
            "sync_mode": "full",
            "source_table": "payments",
            "target_table": "payments",
            "has_etl_plan": True,
            "has_hocon": True,
        },
    },
    {
        "case_id": "L0-008",
        "description": "Incremental sync, PostgreSQL source",
        "natural_language": (
            "Incrementally sync the logs table from PostgreSQL to Doris, "
            "watermark is created_at, primary key is id"
        ),
        "hints": {
            "sync_mode": "incremental",
            "source_table": "logs",
            "target_table": "logs",
            "primary_keys": ["id"],
            "watermark_column": "created_at",
        },
        "expect": {
            "sync_mode": "incremental",
            "source_table": "logs",
            "target_table": "logs",
            "watermark_column": "created_at",
            "has_etl_plan": True,
            "has_hocon": True,
        },
    },
]


# =====================================================================
# L1 -- Simulated fault test cases
# =====================================================================

L1_CASES: list[dict[str, Any]] = [
    {
        "case_id": "L1-001",
        "description": "Incremental sync missing primary key",
        "fault": {
            "type": "missing_primary_key",
            "inject": {"sync_mode": "incremental", "primary_keys": []},
        },
        "expect": {
            "should_intercept": True,
            "expected_codes": ["MISSING_PRIMARY_KEY"],
        },
    },
    {
        "case_id": "L1-002",
        "description": "Incremental sync missing watermark",
        "fault": {
            "type": "missing_watermark",
            "inject": {
                "sync_mode": "incremental",
                "primary_keys": ["id"],
                "watermark_column": None,
            },
        },
        "expect": {
            # 缺水位列只是风险提示，质量门禁按设计只告警不拦截，
            # 因此这里要求"报告该告警且不阻塞"。
            "should_intercept": False,
            "expected_codes": ["MISSING_WATERMARK"],
        },
    },
    {
        "case_id": "L1-003",
        "description": "Sensitive field mapped without masking",
        "fault": {
            "type": "sensitive_field_plain",
            "inject": {
                "sync_mode": "full",
                "primary_keys": ["id"],
                "sensitive_fields": ["phone", "email"],
            },
        },
        "expect": {
            "should_intercept": True,
            "expected_codes": ["SENSITIVE_FIELD_PLAIN_MAPPING"],
        },
    },
    {
        "case_id": "L1-004",
        "description": "Missing quality contract for data with rules",
        "fault": {
            "type": "missing_quality_contract",
            "inject": {
                "sync_mode": "full",
                "primary_keys": ["id"],
                "has_cleaning_rules": True,
                "omit_quality_contract": True,
            },
        },
        "expect": {
            # 缺质量契约属于告警级（不阻塞冻结），断言"报告告警但不拦截"。
            "should_intercept": False,
            "expected_codes": ["MISSING_QUALITY_CONTRACT"],
        },
    },
    {
        "case_id": "L1-005",
        "description": "Missing dead-letter table",
        "fault": {
            "type": "missing_dead_letter",
            "inject": {
                "sync_mode": "full",
                "primary_keys": ["id"],
                "omit_dead_letter": True,
            },
        },
        "expect": {
            "should_intercept": True,
            "expected_codes": ["MISSING_DEAD_LETTER"],
        },
    },
    {
        "case_id": "L1-006",
        "description": "Valid full sync -- should NOT be intercepted (false positive check)",
        "fault": {
            "type": "valid_input",
            "inject": {
                "sync_mode": "full",
                "primary_keys": ["id"],
            },
        },
        "expect": {
            "should_intercept": False,
            "expected_codes": [],
        },
    },
    {
        "case_id": "L1-007",
        "description": "Valid incremental sync with PK + watermark -- should NOT be intercepted",
        "fault": {
            "type": "valid_input",
            "inject": {
                "sync_mode": "incremental",
                "primary_keys": ["id"],
                "watermark_column": "updated_at",
            },
        },
        "expect": {
            "should_intercept": False,
            "expected_codes": [],
        },
    },
    {
        "case_id": "L1-008",
        "description": "Missing shadow table for atomic swap",
        "fault": {
            "type": "missing_shadow_table",
            "inject": {
                "sync_mode": "full",
                "primary_keys": ["id"],
                "omit_shadow_table": True,
            },
        },
        "expect": {
            # 缺影子表属于告警级（不阻塞冻结），断言"报告告警但不拦截"。
            "should_intercept": False,
            "expected_codes": ["MISSING_SHADOW_TABLE"],
        },
    },
]


# =====================================================================
# L2 -- Real chain test cases (require infrastructure)
# =====================================================================

L2_CASES: list[dict[str, Any]] = [
    {
        "case_id": "L2-001",
        "description": "End-to-end MySQL to Doris full sync",
        "natural_language": "Full sync the benchmark_orders table from MySQL to Doris",
        "hints": {
            "sync_mode": "full",
            "source_table": "benchmark_orders",
            "target_table": "benchmark_orders",
            "primary_keys": ["id"],
        },
        "requires_infra": True,
        "expect": {
            "final_status": "succeeded",
            "quality_passed": True,
            "swap_succeeded": True,
        },
    },
    {
        "case_id": "L2-002",
        "description": "End-to-end MySQL to Doris incremental sync",
        "natural_language": (
            "Incrementally sync benchmark_users from MySQL to Doris, watermark is updated_at"
        ),
        "hints": {
            "sync_mode": "incremental",
            "source_table": "benchmark_users",
            "target_table": "benchmark_users",
            "primary_keys": ["id"],
            "watermark_column": "updated_at",
        },
        "requires_infra": True,
        "expect": {
            "final_status": "succeeded",
            "quality_passed": True,
            "swap_succeeded": True,
        },
    },
    {
        "case_id": "L2-003",
        "description": "Quality failure triggers rollback",
        "natural_language": "Full sync benchmark_bad_data from MySQL to Doris",
        "hints": {
            "sync_mode": "full",
            "source_table": "benchmark_bad_data",
            "target_table": "benchmark_bad_data",
            "primary_keys": ["id"],
        },
        "requires_infra": True,
        "expect": {
            "final_status": "rolled_back",
            "quality_passed": False,
        },
    },
]


__all__ = [
    "L0_CASES",
    "L1_CASES",
    "L2_CASES",
]
