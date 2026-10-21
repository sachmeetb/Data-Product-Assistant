---
name: migration-pipeline-generator-dlt
description: Generates a data-migration pipeline (DLT reference implementation) for a data-migration (dmig) project. Reads the discovered source metadata and emits a framework-neutral migration.json contract plus DLT pipeline artifacts, ready to assemble into a downloadable, executable package that lift-and-shifts the source to the target platform. Trigger for the dmig_generate_pipeline stage or when the user asks to "generate the migration pipeline", "build the DLT migration", or "create migration.json". DLT is the first reference framework; the framework-neutral migration.json is the swappable contract.
---

# Migration Pipeline Generator (DLT)

Generates the migration pipeline for a **data migration** (`dmig`) project. The
output is TWO layers:

1. **`migration.json`** — the framework-neutral migration contract (per-dataset
   source→target + write disposition). This is what the backend runner and the
   downloadable package consume. It is framework-agnostic on purpose: a future
   `migration-pipeline-generator-<other-framework>` would emit the same
   `migration.json` and only differ in the runner it renders.
2. **DLT knowledge** — how the `migration.json` maps onto a runnable dlt pipeline
   (source `sql_database`, per-platform destination, write dispositions). The
   actual dlt code lives in the packaged runner (`run.py`), which the backend
   assembles; you do NOT hand-write dlt code here.

**Landing is raw / lift-and-shift** in this phase: preserve original column names
and types, no transformation. `target_table == source_table`.

## Steps

1. Read the migration directive in your prompt — it names the **target platform**,
   **landing strategy**, and **write disposition** to use.
2. Generate `migration.json` from the discovered source metadata. All logic is in
   the bundled script — run it, do not re-implement:

   ```
   python ${CLAUDE_SKILL_DIR}/scripts/generate_migration_config.py \
     --source-platform <source_platform> \
     --target-platform <target_platform> \
     --target-schema <target_schema> \
     --write-disposition <replace|append> \
     --discovery-dir data_discovery \
     --output migration/migration.json
   ```

   It reads the `<schema>__<table>.yaml` files `data-discovery` wrote and emits one
   `datasets[]` entry per table (source_schema, source_table, target_table,
   write_disposition, primary_key).

   **Thin sources (Oracle / SQL Server) with no discovery skill:** add `--reflect
   --source-url <sqlalchemy_url> [--source-schema <schema>]` instead of reading
   discovery YAML — the generator enumerates tables + primary keys by reflecting the
   live source via SQLAlchemy (the same schema dlt reflects at load time). Supported
   source dialects: `postgresql+psycopg2`, `mysql+pymysql`, `oracle+oracledb`,
   `mssql+pyodbc`.
3. **Verify** `migration/migration.json` exists and lists every discovered table.
   If a table is missing, discovery is incomplete — stop and report; do not
   hand-edit the JSON.
4. Do NOT run the migration. The `dmig_execute_transfer` stage (Run Migration)
   assembles the package and runs it; you only produce the contract.

## Reference corpus (bundled)

Per-platform + DLT knowledge is co-located under `reference/`. Consult it — do not
restate type-mapping tables (the canonical type system owns those):

- `reference/dlt/sources.md` — the `sql_database` source per SQLAlchemy dialect.
- `reference/dlt/destinations.md` — native vs SQLAlchemy destinations + `dlt[<extra>]`.
- `reference/dlt/write_dispositions.md` — replace / append / merge + primary keys.

Per-platform type gotchas are surfaced upstream by the `dmig_assess_plan` stage
(the `migration-assessment-advisor` skill's `reference/platforms/` corpus); consult
its `assessment.json` output rather than re-deriving type caveats here.
