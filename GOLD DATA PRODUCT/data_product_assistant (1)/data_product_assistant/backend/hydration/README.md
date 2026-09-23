# BigQuery hydration (standalone)

Recreate the data catalog's medallion tables in BigQuery **on demand**, from the
same `utility_catalog*.json` files the app already uses.

> **Not wired into the app.** Nothing in `server.py` or `agents/` imports this
> package. It's a separate, manually-run tool — safe to keep here without
> affecting the live pipeline. When you're ready to connect it (e.g. seed a
> fresh environment before a demo), run the CLI below.

## What it does

- Reads a catalog file → normalised table/column specs (`catalog_loader.py`).
- Translates Databricks/Delta column types → BigQuery types (`type_mapping.py`),
  e.g. `BIGINT→INT64`, `DOUBLE→FLOAT64`, `DECIMAL(18,2)→NUMERIC(18, 2)`,
  `INT→INT64`, `STRING/TIMESTAMP/DATE/BOOL` unchanged.
- Emits idempotent `CREATE SCHEMA IF NOT EXISTS` + `CREATE TABLE IF NOT EXISTS`
  DDL (`bigquery_hydrator.py`), with column descriptions, `NOT NULL`, and
  unenforced `PRIMARY KEY` constraints.
- Optionally executes it against BigQuery.

## Conventions (match the app's publisher / schema inspector)

- BigQuery **dataset = medallion layer**: `bronze` | `silver` | `gold`.
  (Design catalogs `acn_source`/`acn_aggregated`/`acn_consumption` → `bronze`/`silver`/`gold`.)
- Table FQN = `` `{project}.{layer}.{table_name}` ``.

## Usage

```bash
cd backend

# Dry-run (default): print DDL for the current catalog. Touches nothing.
python hydration/hydrate.py

# Dry-run and save SQL to a file:
python hydration/hydrate.py --out /tmp/catalog_ddl.sql

# Include the runtime gold star schema too:
python hydration/hydrate.py --catalog data/utility_catalog.json data/utility_catalog_pre_ddi.json

# Only the silver layer:
python hydration/hydrate.py --layers silver

# LIVE — create datasets + tables in BigQuery (needs google-cloud-bigquery + creds):
python hydration/hydrate.py --project my-gcp-project --location us-central1 --live
```

Flags: `--project` (defaults to `$BQ_PROJECT` / `$GOOGLE_CLOUD_PROJECT`),
`--location` (default `us-central1`), `--layers`, `--replace`
(**destructive** `CREATE OR REPLACE`), `--load-sample-data`, `--live`, `--out`.

## Tests

```bash
cd backend
python tests/test_hydration.py     # plain python, no deps
# or, when pytest is installed:
pytest tests/test_hydration.py -q
```

Live execution needs `google-cloud-bigquery` (already a backend dependency) and
GCP credentials (`GOOGLE_APPLICATION_CREDENTIALS` or workload identity). Dry-run
needs neither — the DDL builders are pure Python.
