---
name: data-profile-parquet
description: Profiles a Parquet file (or a set of files identified by a TransferBatch v1 manifest) using DuckDB, and verifies the row count against a second independent reader (pyarrow). Produces the same per-column YAML profile format as the data-profiling and data-profiling-mysql skills so the output can be loaded into the knowledge graph via data-profiling-to-dqv-neo4j. Use this skill after data-export-parquet to verify what was written, or when the source is a Parquet file collection. Trigger when the user says "profile parquet", "verify export", "check what was written", "profile the manifest", "DuckDB profile", or when a TransferBatch manifest exists and the user wants to inspect its content.
---

# Data Profiling — Parquet (DuckDB)

Reads one or more Parquet files via DuckDB, computes per-column statistics, verifies row counts with a second independent reader (pyarrow), and writes a profile YAML compatible with the `data-profiling-to-dqv-neo4j` graph loader.

This is the Phase 3 **DuckDB read engine** proof: it shows the same data is observable through DuckDB and pyarrow independently.

All logic is in the bundled script.  Do not re-implement it — just run it with the right arguments.

## Script

`scripts/profile_parquet.py`

```
python ${CLAUDE_SKILL_DIR}/scripts/profile_parquet.py \
  "<source>" \
  "<output_dir>" \
  [--schema SCHEMA] \
  [--table TABLE] \
  [--top-n N] \
  [--no-verify]
```

- `source`: path to a Parquet file, a glob pattern (`exports/*.parquet`), OR a TransferBatch manifest JSON path (`*__manifest.json`)
- `output_dir`: where to write the profile YAML(s)
- `--schema SCHEMA`: override the schema name in the profile (default: derived from file stem)
- `--table TABLE`: override the table name in the profile (default: derived from file stem)
- `--top-n N`: number of top frequent values per column (default: 10)
- `--no-verify`: skip the pyarrow row-count cross-check (faster but no dual-engine proof)

## Output files

For `exports/public__orders.parquet`:

- `output_dir/public__orders__profile.yaml` — column statistics in the standard profiler format

## Verification

When `--no-verify` is NOT set (the default), the script reads the same file with pyarrow and compares the row count to DuckDB's count.  Mismatch raises a `RuntimeError` before writing output — this is the Phase 3 dual-engine gate.

```
[verify] DuckDB row_count=1000, pyarrow row_count=1000 ✓
```

## Profile YAML fields

Same as the Postgres/MySQL profiler:

```yaml
schema: public
table: orders
profiled_at: "2026-07-28T09:00:00"
row_count: 1000
sample_size: 1000
columns:
  - name: id
    dtype: int64
    null_count: 0
    null_rate: 0.0
    distinct_count: 1000
    min: 1
    max: 1000
  - name: amount
    dtype: double
    null_count: 5
    null_rate: 0.005
    distinct_count: 800
    mean: 49.95
    stddev: 12.3
    min: 0.01
    max: 999.99
```

## Error handling

- Mismatched row counts between DuckDB and pyarrow → `RuntimeError` with both counts.
- Missing pyarrow or duckdb → explicit ImportError with install instruction.
- Empty Parquet file → profile with `row_count: 0` and all null stats.
