# Serving mode: dbt-materialized physical copy

> **Read on demand.** CLAUDE.md's "Serving mode" section is the canonical summary of the serving modes and the exclusive group. This doc is the deep-dive it points to: the dbt build path, the verification gate state machine, the per-product target connection, and the no-secrets scaffold. Open when working on `routers/materialization.py`, `workbench-skills/skills/data-serving-virtual-view/scripts/generate_dbt_project.py`, or `MaterializationGateDialog.tsx`.

A data product is served one of **four** ways, discriminated by `:ServingDefinition.servingMode`. This doc is the deep-dive on the two **same-platform** modes:

- **`virtual_view`** — a `CREATE OR REPLACE VIEW` deployed against the source Postgres (always fresh, no history). Authored by `serving_virtual_view`, deployed by `deploy_virtual_view` (`routers/serving.py`).
- **`dbt_materialized`** — a real, runnable dbt project scaffolded on the backend and built into physical tables (and dbt **snapshots** for SCD2 history) on a target Postgres. Authored/built by `serving_physical_copy` (`routers/materialization.py`).

The other two modes are copies that leave the source instance and are documented elsewhere:

- **`lakehouse_local`** — extract to Parquet + a DuckDB catalog (portable file hop, no live-DB dependency). Authored by `serving_lakehouse_export` (`lakehouse_export.py`).
- **`transfer_then_transform`** — cross-platform Extract+Load into a different target engine, then transform. Authored by `serving_transfer` (`transfer_execution.py`); see `cross-platform-transfer.md`.

Modes can coexist on one product; `servingMode` discriminates. `:ServingDefinition` (materialized variant) carries `dbtMaterialization`, `targetSchema`, `dbtProjectPath`, `modelsJson` (per-model `ddl` + status), `summaryJson`, `buildStatus`, `builtAt/By`, `buildDurationMs`, `buildError`.

## One SQL core, two emitters

The view path and the dbt path share **one compiler**. `generate_view_ddl.py:_assemble_view_ddl` exposes a `select_body`; the scaffold script (`workbench-skills/skills/data-serving-virtual-view/scripts/generate_dbt_project.py`) does `import generate_view_ddl as gv` and calls `gv.generate_dbt_models()` to reuse the per-dataset compiler for the model bodies. The virtual emit wraps `select_body` in `CREATE OR REPLACE VIEW … AS`; the dbt emit wraps the **same** body in `{{ config(materialized=...) }} <select_body>` (SCD2 datasets emit a `{% snapshot %}` block instead — falling back to a plain table when no PK is flagged). The SELECT is byte-identical between modes — joins, transform DSL, dialect, and SCD lowering all resolve once in the shared core. **Do not fork transform/join logic into the dbt path.**

## Exclusive serving group

`serving_virtual_view` (enabled by default), `serving_physical_copy`, `serving_lakehouse_export`, and `serving_transfer` (the latter three disabled by default) are members of `exclusive_group: "serving"` in `archetypes.py` — at most one runs per product. `deploy_virtual_view` is present only on the virtual path (`EXCLUSIVE_GROUP_DEPENDENTS`); for the materialized path the `dbt build` *is* the deploy. Switching members goes through `POST /{project_id}/workflows/{workflow_id}/exclusive-group` → `switch_exclusive_group` (`routers/projects.py`); the MCP `set_serving_mode` tool wraps it. **`StageRun` identity is positional** (resolved by `stage_number`, no `stage_id` column), so the switch reconciles StageRun rows in two passes — capture the pre-edit `stage_number → stage_id` map, flip the `workflow_json` enabled flags, then re-key the rows so completed stages keep their results, the deselected member's row drops, and the newly-selected member gets a fresh `pending` row.

## Verification gate (sample → approve → full)

`POST /serving/materialize` (`materialization.py`, `mode=sample|full`, `sample_limit`, `force`) scaffolds the dbt project then runs `dbt build`. A **full** build is server-blocked unless a recent **sample** build passed. The gate state is derived by `GET /serving/materialization/status`:

| `gate_state` | Meaning |
|---|---|
| `no_sample` | No sample build on record — full blocked |
| `awaiting_approval` | Sample built OK, not yet approved — full blocked |
| `built` | Approved / full build present |
| `rejected` | Sample was rejected (`POST /serving/materialization/reject`) — full re-blocked |
| `sample_failed` | Sample build failed |

`force=true` bypasses the gate (the MCP path, which has no interactive preview). Timeouts: **300s** sample / **1800s** full (`_BUILD_TIMEOUT_SAMPLE_S` / `_BUILD_TIMEOUT_FULL_S`; scaffold has its own 180s ceiling). `MaterializationGateDialog.tsx` drives the sample → inspect preview → approve → full UX (sample rows + row counts read from `<schema>_preview` tables). SCD2 `table → snapshot` transitions call `_reconcile_snapshot_targets` to drop a stale non-snapshot relation first (RESTRICT, not CASCADE).

## Per-product target connection

`MaterializationTarget` (SQLite, primary key `contract_id = {project_code}-contract`) holds an optional dbt target: `pg_connection` (a Postgres DSN that must contain the source tables/views the dbt models read), `platform`, `load_strategy` (`'direct'` is the only mode today; `'fdw'`/`'load'` reserved for a future target that doesn't hold the sources). `resolve_materialization_connection()` falls back to the source connection (`Project.pg_connection`) when no target row exists — today's same-instance behavior. CRUD at `GET/PUT/DELETE /serving/materialization-target`; the MCP `set_materialization_target` tool wraps it.

## No-secrets dbt project

`profiles.yml` reads `WB_DBT_HOST/PORT/USER/PASSWORD/DBNAME` via dbt's `env_var()`; the backend injects them into the subprocess `env` via `_dbt_env()` (which parses the resolved target DSN). So the scaffold is **shareable without leaking credentials** — which is why `GET /serving/dbt-project?format=zip|json` (and the MCP `get_dbt_project` tool) can hand the whole project to an engineer. `collect_dbt_files()` excludes `target/`, `logs/`, `dbt_packages/`.
