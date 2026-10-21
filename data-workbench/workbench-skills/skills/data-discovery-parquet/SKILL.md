---
name: data-discovery-parquet
description: Extracts and documents Parquet file schema for data discovery and cataloging via DuckDB. Use this skill whenever the user wants to explore, catalog, document, or extract schema information from a directory of Parquet files — including column names, types, nullability, and row counts. Trigger when the user mentions "data discovery" on Parquet/lakehouse files, a DuckDB-local source, a directory of `.parquet` files, or provides a directory path + glob and wants schema information.
---

# Data Discovery — Parquet (DuckDB)

Reads a directory of Parquet files with DuckDB, lets the user browse the files
found, then extracts schema metadata for each into one YAML file per file in the
`data_discovery/` subdirectory of the current working directory. The YAML shape
is identical to the relational discovery skills, so `data-discovery-to-dcat-neo4j`
consumes it unchanged.

All extraction logic is in the bundled script — do not rewrite it. Run it with
the right arguments.

## Script

`scripts/discover_parquet.py` — lists Parquet files under a directory (glob),
runs DuckDB `DESCRIBE` on each, and writes `<dir>__<file>.yaml`. Self-installs
`duckdb` + `pyyaml` if missing.

## Connection format

A Parquet source is a **directory** plus an optional file **glob** (default
`*.parquet`) — not a networked DSN. The backend passes these via the bound
`duckdb_local` connection's `extra_config` (`dir`, `glob`).

## Workflow

### Step 1 — get the directory (+ optional glob)

Ask the user for the base directory of their Parquet files, or read it from the
bound connection. Default glob is `*.parquet`.

### Step 2 — run discovery

```
python scripts/discover_parquet.py --dir <base_dir> [--glob '*.parquet'] --output-dir .
```

Each Parquet file becomes one YAML document under `data_discovery/` with:
`schema` (the directory name), `table` (the file stem), `columns`
(name / type / nullable), `row_count`, and empty `primary_key` / `foreign_keys`
(files carry no relational constraints).

### Step 3 — report

Summarize the files discovered and their column counts. The downstream
`data-discovery-to-dcat-neo4j` graph loader picks up the YAML from
`data_discovery/`.
