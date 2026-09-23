# tests/test_agents.py — individual agent tests for the new pipeline
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents import pipeline_generator, test_agent, publisher_agent

SAMPLE_SPEC = {
    "source_tables": [
        {
            "name": "retail_catalog.bronze.raw_orders",
            "columns": [
                {"name": "order_id", "type": "STRING", "nullable": True},
                {"name": "amount",   "type": "STRING", "nullable": True},
            ],
        }
    ],
    "target_tables": [
        {
            "layer": "silver",
            "name": "retail_catalog.silver.orders_clean",
            "columns": [
                {"name": "order_id", "type": "BIGINT",  "nullable": False},
                {"name": "amount",   "type": "DOUBLE",  "nullable": False},
            ],
        }
    ],
    "sttm": [
        {"source_column": "order_id", "target_column": "order_id",
         "transformation": "CAST(order_id AS BIGINT)"},
        {"source_column": "amount",   "target_column": "amount",
         "transformation": "CAST(amount AS DOUBLE)"},
    ],
    "data_contract": {
        "freshness_sla": "daily",
        "quality_rules": [{"column": "order_id", "rule": "not_null"}],
        "row_count_expectation": {"min_rows": 100},
    },
    "domain": "retail",
    "pipeline_type": "dlt",
}


# ── Pytest tests (fixtures from conftest.py) ──────────────────────────────────

def test_pipeline_generator_returns_code(generated_code_output):
    out = generated_code_output
    assert "generated_code" in out, f"Missing generated_code: {out}"
    assert len(out["generated_code"]) > 100, "Generated code too short"
    assert "pipeline_type" in out, f"Missing pipeline_type: {out}"
    print(f"PASS: pipeline-generator — {out.get('pipeline_type')} pipeline, "
          f"{len(out['generated_code'])} chars")


def test_test_agent_returns_report(test_report_output):
    out = test_report_output
    assert "test_status" in out, f"Missing test_status: {out}"
    assert out["test_status"] in ("passed", "failed"), f"Invalid test_status: {out}"
    assert "summary" in out, f"Missing summary: {out}"
    print(f"PASS: test-agent — status={out['test_status']}, summary={out.get('summary')}")


def test_publisher_agent_returns_report(generated_code_output, test_report_output):
    code = generated_code_output.get("generated_code", "-- no code")
    out = publisher_agent.run(code, SAMPLE_SPEC, test_report_output)
    assert "publish_status" in out, f"Missing publish_status: {out}"
    assert "published_tables" in out, f"Missing published_tables: {out}"
    print(f"PASS: publisher — status={out['publish_status']}, "
          f"tables={out.get('published_tables')}")


# ── Direct script execution ───────────────────────────────────────────────────

if __name__ == "__main__":
    print("Running agents in sequence...\n")

    gen = pipeline_generator.run(SAMPLE_SPEC)
    assert "generated_code" in gen, f"Missing generated_code: {gen}"
    print(f"PASS: pipeline-generator — {gen.get('pipeline_type')}, "
          f"{len(gen.get('generated_code', ''))} chars")

    code = gen["generated_code"]
    tr = test_agent.run(code, SAMPLE_SPEC)
    assert "test_status" in tr, f"Missing test_status: {tr}"
    print(f"PASS: test-agent — status={tr['test_status']}")
    print(f"  summary : {tr.get('summary')}\n")

    pub = publisher_agent.run(code, SAMPLE_SPEC, tr)
    assert "publish_status" in pub, f"Missing publish_status: {pub}"
    print(f"PASS: publisher — {pub.get('publish_status')}")
    print(f"  tables  : {pub.get('published_tables')}\n")

    print("All agent tests passed.")
