"""
schema_inspector.py — Pre-flight BigQuery table schema inspection.

Called by the Pipeline Generator before LLM invocation.
Queries each target table via the BigQuery client so the agent knows whether to:
  - CREATE TABLE IF NOT EXISTS  (table does not exist)
  - ALTER TABLE ADD COLUMN ...  (table exists, spec adds new columns)
  - Skip DDL entirely           (table exists, schema unchanged)

If BigQuery credentials are absent or the project is not set, every table is
returned as {"exists": False} — the Pipeline Generator falls back to CREATE TABLE
for everything, which is safe because CREATE TABLE IF NOT EXISTS is idempotent.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent.parent
load_dotenv(ROOT_DIR / ".env")

_BQ_PROJECT  = os.environ.get("BQ_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
_BQ_LOCATION = os.environ.get("BQ_LOCATION", "us-central1")


def _parse_bq_schema(bq_table) -> list[dict]:
    """Convert a BigQuery Table schema to a list of {name, data_type} dicts."""
    return [
        {"name": field.name, "data_type": field.field_type}
        for field in (bq_table.schema or [])
    ]


def inspect_tables(table_names: list[str]) -> dict[str, dict]:
    """
    Return the current schema for each fully-qualified BigQuery target table.

    Args:
        table_names: List of three-level names, e.g.
                     ["my-project.silver.silver_sales", "my-project.gold.gold_sales"]

    Returns:
        {
            "my-project.silver.silver_sales": {
                "exists": True,
                "columns": [
                    {"name": "customer_id", "data_type": "STRING"},
                    {"name": "sales",       "data_type": "NUMERIC"},
                ]
            },
            "my-project.gold.gold_sales": {
                "exists": False,
                "columns": []
            }
        }
    """
    if not table_names:
        return {}

    if not _BQ_PROJECT:
        return {t: {"exists": False, "columns": []} for t in table_names}

    try:
        from google.cloud import bigquery  # noqa: PLC0415
        from google.cloud.exceptions import NotFound  # noqa: PLC0415
        client = bigquery.Client(project=_BQ_PROJECT, location=_BQ_LOCATION)
    except Exception:
        return {t: {"exists": False, "columns": []} for t in table_names}

    output: dict[str, dict] = {}
    for table in table_names:
        try:
            bq_table = client.get_table(table.strip("`"))
            output[table] = {"exists": True, "columns": _parse_bq_schema(bq_table)}
        except NotFound:
            output[table] = {"exists": False, "columns": []}
        except Exception:
            output[table] = {"exists": False, "columns": []}

    return output


def extract_target_tables(spec: dict) -> list[str]:
    """
    Derive the fully-qualified BigQuery target table names the Pipeline Generator will create.

    FQN format: {bq_project}.{dataset}.{table}
    Datasets: gold (Gold layer), silver (Silver layer), bronze (Bronze layer)

    Reads:
      - sttm.mappings[n].target_table + layer  (primary source)
      - silver_schema.tables[n].name           (supplementary)
      - gold_schema.tables[n].name             (supplementary)
    """
    project  = spec.get("bq_project") or _BQ_PROJECT or "bq_project"
    seen: set[str] = set()
    tables: list[str] = []

    _LAYER_DATASET = {"gold": "gold", "silver": "silver", "bronze": "bronze"}

    for mapping in spec.get("sttm", {}).get("mappings", []):
        target = mapping.get("target_table", "")
        layer  = mapping.get("layer", "silver")
        if target:
            dataset = _LAYER_DATASET.get(layer, layer)
            fqn = f"`{project}`.{dataset}.{target}"
            if fqn not in seen:
                tables.append(fqn)
                seen.add(fqn)

    for section, layer in [("silver_schema", "silver"), ("gold_schema", "gold")]:
        for tbl in spec.get(section, {}).get("tables", []):
            name = tbl.get("name", tbl.get("table_name", ""))
            if name:
                dataset = _LAYER_DATASET.get(layer, layer)
                fqn = f"`{project}`.{dataset}.{name}"
                if fqn not in seen:
                    tables.append(fqn)
                    seen.add(fqn)

    return tables
