"""
publisher_agent.py — Publisher Agent.

Two separate functions:
  analyze()  — LLM call: extract transformations, joins, aggregations, sample data
               Runs BEFORE user approval so the preview is rich.
  execute()  — SQL execution only via BigQueryPublisher.
               Runs AFTER user approves. No LLM call.
"""

import os
import re

from dotenv import load_dotenv
from .base import run_agent, _PROJECT_ROOT
from tools.databricks_tool import DatabricksPublisher, query_table

load_dotenv(_PROJECT_ROOT / ".env")

_UC_CATALOG = os.environ.get("DATABRICKS_CATALOG") or "main"


def _rewrite_sql_to_uc(sql: str) -> str:
    """
    Rewrite design-time catalog.schema.table FQNs to Unity Catalog
    `catalog`.layer.table FQNs.
    """
    if not sql or not _UC_CATALOG:
        return sql

    for old_catalog, schema_name in (
        ("acn_consumption", "gold"),
        ("acn_aggregated",  "silver"),
        ("acn_source",      "bronze"),
    ):
        sql = re.sub(
            r"`?" + re.escape(old_catalog) + r"`?\.\w+\.([\w]+)",
            lambda m, sc=schema_name: f"`{_UC_CATALOG}`.{sc}.{m.group(1)}",
            sql,
            flags=re.IGNORECASE,
        )

    sql = re.sub(
        r"`?bq_project`?\.([\w]+)\.([\w]+)",
        lambda m: f"`{_UC_CATALOG}`.{m.group(1)}.{m.group(2)}",
        sql,
        flags=re.IGNORECASE,
    )

    return sql


async def analyze(validated_code: str, spec: dict | None = None, session=None) -> dict:
    """
    LLM analysis of the generated SQL pipeline.
    Returns transformations, joins, aggregations, and 20 sample rows per table.
    Called BEFORE the user approves — shown in the preview.
    """
    if spec is None:
        spec = {}

    context = {
        "generated_code": validated_code,
        "spec": spec,
    }

    return await run_agent(
        "publisher",
        "Analyze the SQL pipeline. Extract all transformations, joins, and aggregations. "
        "Generate 20 realistic sample rows for each Silver table and 10+ rows for each Gold table.",
        context=context,
        session=session,
    )


def execute(validated_code: str, spec: dict | None = None, test_report: dict | None = None) -> dict:  # noqa: ARG001
    """
    Execute the validated SQL against Google BigQuery, then query each published
    silver/gold table to return the actual written rows.
    Called AFTER the user approves.
    """
    if spec is None:
        spec = {}

    # Rewrite design-time FQNs to Unity Catalog FQNs
    validated_code = _rewrite_sql_to_uc(validated_code)

    publisher = DatabricksPublisher(mode="live")
    publish_results = publisher.publish(pipeline_code=validated_code)

    # Query actual table data for silver/gold tables after publishing
    actual_data: dict = {}
    for table in publish_results.get("published_tables", []):
        if "_bronze" in table or ".bronze." in table:
            continue  # skip bronze — source data, not output
        result = query_table(table.strip("`"))
        if result.get("columns"):
            actual_data[table] = result

    publish_results["actual_table_data"] = actual_data
    return publish_results


def run(validated_code: str, spec: dict | None = None, test_report: dict | None = None) -> dict:
    """Legacy combined call — kept for CLI/pipeline.py compatibility."""
    publish_results = execute(validated_code, spec, test_report)
    analysis = analyze(validated_code, spec)
    return {
        **publish_results,
        "transformations": analysis.get("transformations", []),
        "joins": analysis.get("joins", []),
        "aggregations": analysis.get("aggregations", []),
        "sample_data": analysis.get("sample_data", {}),
    }
