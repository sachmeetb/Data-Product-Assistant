---
name: data-export-parquet
description: Exports one relational table as a Parquet file and writes a TransferBatch v1 manifest alongside it. Use this skill when the user wants to snapshot a table to a file for downstream lakehouse ingestion, cross-platform transfer, or archiving. Trigger when the user mentions "export to parquet", "snapshot table", "transfer batch", "write parquet", "data export", "export for iceberg", "file export", or asks to move a table out of the source database into a file format. Requires pyarrow>=19.0 in the Python environment. Produces a TransferBatch v1 JSON manifest that can be validated against the platform.transfer_batch.TransferBatch schema.
---

# Data Export — Parquet

Connects to a relational source (Postgres or MySQL), reads a table, writes a Parquet file, and produces a TransferBatch v1 JSON manifest.  This is the Phase 3 data-plane proof: it shows that a source snapshot can be moved through a versioned contract to a durable file without touching the serving or graph layers.

All export logic is in the bundled script.  Do not re-implement it — just run it with the right arguments.

**Relationship to the lakehouse serving package.** The TransferBatch v1 manifest this script writes is the same machine-readable delivery contract the lakehouse serving package emits (`serving_runners/run_lakehouse.py`), and is loadable by `platform.transfer_batch.TransferBatch`. When a product is served as a lakehouse package, the runnable package (`run.py` producer/reader + `query.py` + `explore.sql` + `catalog.duckdb` + a README from `data-serving-lakehouse-documenter`) is the artifact Data Workbench runs *and* the engineer downloads — this per-table skill is the lower-level primitive for one-off snapshots. Keep the manifest shape identical (`operation_encoding: full_load` for a full snapshot, parallel `file_uris`/`file_checksums`) so both paths validate against the same model.

## Script

`scripts/export_table.py`

```
python ${CLAUDE_SKILL_DIR}/scripts/export_table.py \
  "<connection_string>" \
  "<schema>" \
  "<table>" \
  "<output_dir>" \
  [--run-id RUN_ID] \
  [--batch-id BATCH_ID] \
  [--limit N] \
  [--compression CODEC] \
  [--primary-key COL ...]
```

- `connection_string`: Postgres (`postgresql://...` or `postgres://...`) or MySQL (`mysql://...`)
- `schema`: source schema name (e.g. `public`, `sales`)
- `table`: source table name (e.g. `orders`)
- `output_dir`: directory to write `<schema>__<table>.parquet` and `<schema>__<table>__manifest.json`
- `--run-id`: optional run UUID (auto-generated if omitted)
- `--batch-id`: optional batch identifier within the run (defaults to `batch-001`)
- `--limit N`: max rows to export (default 0 = full table; use for sampling during development)
- `--compression`: Parquet compression codec — `zstd` (default), `snappy`, or `none`
- `--primary-key COL`: repeatable; records the primary-key column(s) in the manifest

## Output files

For `--schema public --table orders --output-dir ./exports`:

- `exports/public__orders.parquet` — the actual data
- `exports/public__orders__manifest.json` — TransferBatch v1 manifest

## Manifest fields (key ones)

| Field | Value |
|---|---|
| `contract_version` | `"1"` |
| `run_id` | UUID or caller-supplied string |
| `batch_id` | `"batch-001"` by default |
| `source_asset_ref.kind` | `"relational_relation"` |
| `source_asset_ref.relation` | table name |
| `schema_fingerprint` | SHA-256 of canonical column→type map |
| `file_uris` | `["file://<abs-path>.parquet"]` |
| `file_checksums` | SHA-256 of each file |
| `row_count` | exact row count after export |
| `byte_count` | raw bytes of the Parquet file |
| `column_statistics` | per-column null_count, value_count |
| `completion_marker` | `"complete"` or `"failed"` |

## DuckDB verification

After export, you can verify the file with DuckDB:

```python
import duckdb
result = duckdb.query("SELECT COUNT(*) FROM read_parquet('exports/public__orders.parquet')").fetchone()
print(result)
```

Or use the companion `data-profile-parquet` skill (Phase 3) for full column statistics.

## Error handling

- Unknown connection scheme → `ValueError` with a clear message; no silent fallback.
- Query failure → `completion_marker: "failed"` in the manifest; exception re-raised.
- Missing `pyarrow` or driver → explicit ImportError with install instruction.
