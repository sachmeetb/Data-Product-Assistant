---
name: migration-package-documenter
description: Authors a contextual README for a data-migration package. Given the migration contract (source/target platforms, target schema, write disposition, datasets), returns a single JSON {files:{README.md:...}} verdict describing the actual tables being migrated and how to run the package. Trigger when assembling a migration serving package or when asked to document a migration. Mirrors data-serving-{view,dbt,lakehouse}-documenter — pure text, no file writes, no shell.
---

# Migration Package Documenter

Authors the README for a **data-migration** package (the runnable DLT package that
lift-and-shifts a source to a target platform). Same programmatic contract as the
serving documenters: return exactly ONE fenced ```json block of the form
`{"files": {"README.md": "<markdown>"}}`. Do not write files. Do not run shell.

## Inputs (provided as JSON)

`project_code`, `source_platform`, `target_platform`, `target_schema`,
`write_disposition`, `datasets` (list of `{source_schema, source_table,
target_table, write_disposition, primary_key}`).

## README structure (seven sections, in order)

1. **Title + what this is** — "`<project_code>` — data-migration package
   (`<source>` → `<target>`)"; one paragraph on the lift-and-shift.
2. **Datasets** — a bullet list of the tables being migrated (`source → target`).
3. **What's in this package** — an inventory table: `run.py`, `migration.json`,
   `requirements.txt`, `.env.example`, `manifests/*.json`, `_wb_runresult.py`.
4. **How it works** — a short mermaid `flowchart LR` from source → dlt → target,
   with `.env → WB_SOURCE_*/WB_TARGET_*` feeding it and `manifests/*` as output.
5. **How the technology works** — **read the bundled `reference/tech/dlt.md` corpus**
   and write the under-the-hood mechanics: `dlt` is **extract → normalize (rows →
   Parquet) → load**, and the load step branches on the **target family** — a
   **warehouse** target (`snowflake` / `databricks` / `redshift` / `bigquery`) *stages
   the Parquet then bulk `COPY INTO`*; a **relational** target (`postgres` / `mysql` /
   `oracle` / `sqlserver`) *bulk-loads directly, no external stage*. State the
   `write_disposition` semantics (replace / append / merge-on-PK). Emit a *second*,
   distinct "under the hood" mermaid matching the target family — do NOT restate §4.
   Keep it a short paragraph + the one mermaid.
6. **Configuration** — the `WB_SOURCE_*` and `WB_TARGET_*` env vars (per the target
   platform), and the system-shell vs `.env` conventions.
7. **Usage** — `pip install -r requirements.txt`, copy `.env.example` → `.env`,
   `python run.py --mode load` (migrate) / `--mode verify` (reconcile) / `--mode
   plan` (dry run), with a flag table; then **Outputs** (target tables, per-dataset
   TransferBatch manifests, `run_result.json`).

Ground every section in the ACTUAL datasets + platforms from the inputs — name the
real tables, not placeholders.

## Reference corpus (bundled)

The under-the-hood mechanics for §5 are human-verified in a co-located corpus —
`Read` it, don't invent the mechanics:

- `reference/tech/dlt.md` — the extract → normalize → load pipeline, the warehouse
  (stage + `COPY INTO`) vs relational (direct load) branch, write-disposition
  semantics, and both "under the hood" mermaid variants.
