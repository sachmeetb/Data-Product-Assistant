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

_UC_CATALOG = os.environ.get("DATABRICKS_CATALOG", "main")


def inspect_tables(table_names: list[str]) -> dict[str, dict]:
    """
    Return the current schema for each fully-qualified Unity Catalog target table.
    """
    if not table_names:
        return {}

    if not os.environ.get("DATABRICKS_HOST"):
        return {t: {"exists": False, "columns": []} for t in table_names}

    output: dict[str, dict] = {}
    try:
        from tools.databricks_auth import get_sql_connection
        with get_sql_connection(catalog=_UC_CATALOG) as conn, conn.cursor() as cur:
            for table in table_names:
                clean = table.strip("`")
                quoted = ".".join(f"`{p}`" for p in clean.split("."))
                try:
                    cur.execute(f"DESCRIBE TABLE {quoted}")
                    cols = [
                        {"name": r[0], "data_type": str(r[1]).upper()}
                        for r in cur.fetchall()
                        if r[0] and not str(r[0]).startswith("#")
                    ]
                    output[table] = {"exists": True, "columns": cols}
                except Exception:
                    output[table] = {"exists": False, "columns": []}
    except Exception:
        return {t: {"exists": False, "columns": []} for t in table_names}

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
