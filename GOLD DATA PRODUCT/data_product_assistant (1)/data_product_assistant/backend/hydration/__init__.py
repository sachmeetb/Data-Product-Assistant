"""
hydration — standalone BigQuery hydration for the data catalog.

This package is intentionally DECOUPLED from the running app: nothing in
server.py or the agents imports it, and importing it has no side effects. It
exists so you can (re)create the medallion datasets + tables in BigQuery from a
catalog file "as and when necessary", separately from the live pipeline.

Entry points:
  * CLI:    python hydration/hydrate.py            (dry-run by default)
  * Python: from hydration import ... (see modules below)

Modules:
  * type_mapping       — Databricks/Delta type → BigQuery type translation
  * catalog_loader     — parse utility_catalog*.json into TableSpec/ColumnSpec
  * bigquery_hydrator  — build CREATE SCHEMA/TABLE DDL and (optionally) execute it
  * hydrate            — the CLI

The modules use flat imports (e.g. `from type_mapping import ...`) and add this
directory to sys.path when run, so they work both as `python hydration/hydrate.py`
and when this directory is on sys.path.
"""
