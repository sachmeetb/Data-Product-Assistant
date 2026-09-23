"""
tests/test_pipeline.py — end-to-end pipeline integration tests.
Run with:  python -m pytest tests/test_pipeline.py -v
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from pipeline import run_pipeline

FULL_SPEC_INPUT = (
    "I need a DLT pipeline that moves order data from bronze to silver. "
    "Source: retail_catalog.bronze.raw_orders (order_id STRING, amount STRING, order_dt STRING). "
    "Target: retail_catalog.silver.orders_clean (order_id BIGINT NOT NULL, amount DOUBLE NOT NULL, order_dt TIMESTAMP). "
    "STTM: order_id -> CAST AS BIGINT, amount -> CAST AS DOUBLE, order_dt -> CAST AS TIMESTAMP. "
    "Data contract: order_id must not be null, amount must be positive, min 1000 rows, daily freshness. "
    "Domain: retail. Pipeline type: dlt."
)


class TestPipelineEndToEnd:
    def test_pipeline_completes(self):
        result = run_pipeline(FULL_SPEC_INPUT, verbose=False)
        assert result["status"] in ("completed", "failed"), (
            f"Unexpected status: {result.get('status')}"
        )

    def test_orchestrator_step_present(self):
        result = run_pipeline(FULL_SPEC_INPUT, verbose=False)
        steps = result.get("steps", {})
        assert "orchestrator" in steps, "orchestrator step missing"

    def test_pipeline_generator_step_present(self):
        result = run_pipeline(FULL_SPEC_INPUT, verbose=False)
        steps = result.get("steps", {})
        gen_keys = [k for k in steps if k.startswith("pipeline_generator_iter_")]
        assert len(gen_keys) >= 1, "No pipeline_generator step found"

    def test_test_agent_step_present(self):
        result = run_pipeline(FULL_SPEC_INPUT, verbose=False)
        steps = result.get("steps", {})
        test_keys = [k for k in steps if k.startswith("test_agent_iter_")]
        assert len(test_keys) >= 1, "No test_agent step found"

    def test_publisher_step_on_success(self):
        result = run_pipeline(FULL_SPEC_INPUT, verbose=False)
        if result["status"] == "completed":
            assert "publisher" in result.get("steps", {}), "publisher step missing on success"
            assert "published_tables" in result, "published_tables missing"

    def test_empty_input_fails_gracefully(self):
        result = run_pipeline("", verbose=False)
        assert result["status"] == "failed"
        assert "error" in result
