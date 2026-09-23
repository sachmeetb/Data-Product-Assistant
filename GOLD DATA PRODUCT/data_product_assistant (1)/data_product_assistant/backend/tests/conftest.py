# tests/conftest.py
# Session-scoped fixtures — runs the orchestrator once and caches the spec
# so downstream agent tests can reuse it without extra API calls.
import os
import sys

# Avoid live Databricks execution during unit tests unless explicitly enabled.
os.environ.setdefault("DATABRICKS_PUBLISHER_MODE", "dry_run")

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents.base import run_agent

SAMPLE_SPEC = {
    "source_tables": [
        {
            "name": "retail_catalog.bronze.raw_orders",
            "columns": [
                {"name": "order_id", "type": "STRING", "nullable": True},
                {"name": "amount",   "type": "STRING", "nullable": True},
                {"name": "order_dt", "type": "STRING", "nullable": True},
            ],
        }
    ],
    "target_tables": [
        {
            "layer": "silver",
            "name": "retail_catalog.silver.orders_clean",
            "columns": [
                {"name": "order_id", "type": "BIGINT",    "nullable": False},
                {"name": "amount",   "type": "DOUBLE",    "nullable": False},
                {"name": "order_dt", "type": "TIMESTAMP", "nullable": True},
            ],
        }
    ],
    "sttm": [
        {"source_table": "retail_catalog.bronze.raw_orders", "source_column": "order_id",
         "target_table": "retail_catalog.silver.orders_clean", "target_column": "order_id",
         "transformation": "CAST(order_id AS BIGINT)"},
        {"source_table": "retail_catalog.bronze.raw_orders", "source_column": "amount",
         "target_table": "retail_catalog.silver.orders_clean", "target_column": "amount",
         "transformation": "CAST(amount AS DOUBLE)"},
    ],
    "data_contract": {
        "freshness_sla": "daily",
        "quality_rules": [
            {"column": "order_id", "rule": "not_null"},
            {"column": "amount",   "rule": "positive"},
        ],
        "row_count_expectation": {"min_rows": 100},
    },
    "domain": "retail",
    "pipeline_type": "dlt",
}


@pytest.fixture(scope="session")
def pipeline_spec() -> dict:
    return SAMPLE_SPEC


@pytest.fixture(scope="session")
def generated_code_output(pipeline_spec) -> dict:
    from agents import pipeline_generator
    return pipeline_generator.run(pipeline_spec)


@pytest.fixture(scope="session")
def test_report_output(generated_code_output, pipeline_spec) -> dict:
    from agents import test_agent
    code = generated_code_output.get("generated_code", "")
    return test_agent.run(code, pipeline_spec)
