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
from tools.bigquery_tool import BigQueryPublisher, query_table

load_dotenv(_PROJECT_ROOT / ".env")

_BQ_PROJECT = os.environ.get("BQ_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")

# Design-time catalog placeholders baked into DDI artifacts and utility_catalog.json.
# At execute-time these are rewritten to the actual BQ project + layer dataset.
_LAYER_MAP = {
    "acn_consumption": "gold",
    "acn_aggregated":  "silver",
    "acn_source":      "bronze",
    "bq_project":     None,   # handled separately — replace with real project only
}


def _rewrite_sql_to_bq(sql: str) -> str:
    """
    Rewrite design-time Databricks-style catalog.schema.table FQNs to BigQuery
    project.dataset.table FQNs.

    Mapping:
      acn_consumption.<domain>.<table>  →  `{BQ_PROJECT}`.gold.<table>
      acn_aggregated.<domain>.<table>   →  `{BQ_PROJECT}`.silver.<table>
      acn_source.<domain>.<table>       →  `{BQ_PROJECT}`.bronze.<table>
      bq_project.<dataset>.<table>     →  `{BQ_PROJECT}`.<dataset>.<table>
    """
    if not sql or not _BQ_PROJECT:
        return sql

    # Three-level: acn_consumption.domain.table → `project`.gold.table
    for old_catalog, dataset in (
        ("acn_consumption", "gold"),
        ("acn_aggregated",  "silver"),
        ("acn_source",      "bronze"),
    ):
        # Match: acn_consumption.any_schema.table_name
        sql = re.sub(
            r"`?" + re.escape(old_catalog) + r"`?\.\w+\.([\w]+)",
            lambda m, ds=dataset: f"`{_BQ_PROJECT}`.{ds}.{m.group(1)}",
            sql,
            flags=re.IGNORECASE,
        )

    # Two-level: bq_project.dataset.table → `project`.dataset.table
    sql = re.sub(
        r"`?bq_project`?\.([\w]+)\.([\w]+)",
        lambda m: f"`{_BQ_PROJECT}`.{m.group(1)}.{m.group(2)}",
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

    # Rewrite design-time Databricks-style FQNs to BigQuery FQNs
    validated_code = _rewrite_sql_to_bq(validated_code)

    publisher = BigQueryPublisher()
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
