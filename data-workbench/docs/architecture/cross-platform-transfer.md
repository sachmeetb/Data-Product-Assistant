# Cross-platform transfer — `transfer_then_transform` (Phase 2)

> **STATUS: Phase 2.0–2.5 SHIPPED (experimental).** The execution seam
> (`TransferExecutionProvider` + `DuckDBTransferProvider`), the pure
> transform-placement planner (`platform/transform_placement.py`), the transfer
> worker (`transfer_execution.py`), the `serving_transfer` stage, the advisor
> feasibility flip, and the `/serving/transfer` REST + `run_transfer` MCP surface
> are all built. **Two engines** (`transfer_execution._target_engine`):
> **DuckDB** for Postgres/DuckDB targets (reads Postgres/MySQL via extensions,
> writes an ATTACH'd Postgres or a DuckDB catalog — no extra deps), and **dlt**
> (Phase 2.1) for **Snowflake/Databricks** targets (DuckDB extracts the shaped
> Arrow result; a dlt pipeline loads it into the warehouse). dlt is a **lazy,
> optional** import — `_dlt_load` raises an actionable `engine_unavailable` error
> when `dlt[snowflake]`/`dlt[databricks]` isn't installed (a deploy concern, same
> posture as dbt/GX). `transfer_source` is `preview` for Postgres/MySQL/DuckDB;
> `transfer_target` is `preview` for Postgres/DuckDB (DuckDB engine) **and**
> Snowflake/Databricks (dlt engine). **Phase 2.2 shipped:** true **ELT push-down**
> for DuckDB-writable targets — `transform_placement.build_elt_plan` +
> `rewrite_base_relations` split the compiled body into raw base-relation extracts
> (landed in a `wb_landing` zone) and a base-rewritten target body run in the
> target; `transfer_execution._duckdb_elt` executes it. It is **governance-gated**
> (only on an explicit `transfer_then_transform` placement + a *confirmed-zero*
> mask count via `_has_mask_transforms`, fail-safe) and **falls back transparently**
> to `transform_on_extract` on any error (identical output from the shared core).
> **Phase 2.3 shipped:** (a) **extract-side projection narrowing** — the ELT
> extract lands only the columns the compiled body references
> (`plan_extract_projections` via sqlglot; safe by construction — over-narrowing
> just triggers the fallback); (b) **warehouse target-side ELT** — for
> Snowflake/Databricks the dlt engine now lands raw (narrowed) base tables and
> runs the target-side transform *in the warehouse* via `dlt`'s `sql_client`
> (`_dlt_elt`), rather than computing on the source. The warehouse ELT path is
> **unverified against a live warehouse** and falls back to `transform_on_extract`
> on any error. **Phase 2.4 shipped:** **row-filter push-down** on the extract —
> `plan_pushdown_filters` (sqlglot) pushes a WHERE conjunct onto a landed base
> relation ONLY when every column in it is qualified to the FROM **anchor** (never
> a nullable join side); RIGHT/FULL joins, nullable-side/multi-table conjuncts,
> and shared-landing datasets that disagree (cross-dataset intersection) are all
> skipped. The target body retains the full WHERE, so a pushed conjunct only
> pre-reduces rows — provably result-preserving. **Phase 2.5 shipped:** an opt-in
> **dbt-project-in-warehouse** for the warehouse target-side T —
> `generate_warehouse_dbt_project` scaffolds a dbt project (models = the landed-
> rewritten target bodies; `profiles.yml` via `env_var()`, no secrets on disk)
> and `_run_warehouse_dbt` `dbt build`s it (`WB_WAREHOUSE_ELT_DBT=1`; falls back
> to the `sql_client` `CREATE TABLE AS` otherwise). The scaffold generation is
> pure + unit-tested; the live `dbt build` is deploy-gated (needs dbt + adapter +
> a live warehouse, same posture as the same-platform dbt materialization) and
> degrades to `transform_on_extract` on any error.

## 1. Why this exists (the load-bearing framing)

Today a product is served three ways, all of which resolve the same compiled
`select_body`:

| Pattern | Boundary | Engine |
|---|---|---|
| `native_virtual` | none | `CREATE VIEW` in the source DB |
| `native_materialized` | none (in-DB CTAS) | **dbt** |
| `lakehouse_file` | to files | **DuckDB** → Parquet (Phase 1) |

dbt is **Transform-in-place**: it runs `CREATE TABLE AS SELECT` through **one**
connection, so it structurally **cannot** span two platform instances.
Materializing a product into a *different* platform than its source (Postgres →
Snowflake, MySQL → Databricks) requires a real **Extract + Load** step — dbt is
only the "T". `lakehouse_file` already crosses *to files*; `transfer_then_transform`
is the generalization that crosses *to another database platform*.

**dlt** (`pip install dlt`) is the EL engine: a Python library with the same
"embedded engine we drive" posture as dbt and Great Expectations. It does EL
only — schema inference/evolution, incremental/CDC state, normalization — into
Postgres / Snowflake / Databricks / DuckDB / filesystem, and **composes** with
dbt (dlt lands the data, dbt transforms it in the target). It plugs into the
already-shipped `platform/transfer_batch.py` contract (authored citing dlt /
Singer as prior art).

## 2. The execution seam — `TransferExecutionProvider`

A new duck-typed Protocol alongside the existing ones in
`platform/interfaces.py` (`ConnectionProvider`, `DiscoveryProvider`,
`QueryExecutor`, `DeploymentProvider`). It owns the extract→load half of a
cross-platform move; the transform half stays in the shared SQL core (dbt or the
target's engine).

```python
@runtime_checkable
class TransferExecutionProvider(Protocol):
    """Moves data across a platform boundary (Extract + Load). Emits a
    TransferBatch per run for reconciliation. The Transform half is NOT here —
    it is compiled once by the shared SQL core and placed by the planner (§4)."""

    def plan_transfer(
        self, spec: "TransferSpec"
    ) -> "TransferPlan": ...
        # Resolve source/target refs, resource set, write disposition
        # (replace/append/merge), incremental cursor. Pure — no I/O.

    def execute_transfer(
        self, plan: "TransferPlan", idempotency_key: str
    ) -> "TransferBatch": ...
        # Run the EL. Returns a durable TransferBatch manifest (identity,
        # schema evidence, file/table addresses, incremental_state_after,
        # reconciliation evidence). Resumable via idempotency_key.

    def verify_transfer(self, batch: "TransferBatch") -> "VerificationReport": ...
        # Post-load counts/stats vs the batch's *expected* (post-transform)
        # figures — NOT raw source parity (see §5).

    def compensate(self, batch: "TransferBatch") -> "CompensationReport": ...
        # Roll back / quarantine a partial load.
```

Registered fail-closed via the existing registry pattern
(`_get_transfer_provider(platform_type)` in `routers/connections.py`, mirroring
`_get_connection_provider` / `_get_discovery_provider`). Gated by the
`transfer_source` / `transfer_target` capability keys already declared
`unsupported` in every manifest — Phase 2 flips the relevant ones to `preview`.

### `DuckDBTransferProvider` (Phase-2.0 engine) / `dltTransferProvider` (Phase 2.1)

**Shipped:** `platform/providers/transfer.py:DuckDBTransferProvider` +
`transfer_execution.run_transfer`. DuckDB is the coordinator — it `ATTACH`es the
source (Postgres/MySQL, `READ_ONLY`) and the target (an ATTACH'd Postgres or a
DuckDB catalog file), `USE`s the source so the compiled `select_body`'s bare
`"schema"."table"` refs resolve, and runs `CREATE TABLE tgt.<schema>.<t> AS
(<body>)` per dataset (verify: landed vs expected count). No new dependency;
reuses the proven `lakehouse_export` ATTACH machinery.

*(Phase 2.1)* the **dlt** engine below is the swap-in for warehouse targets
(Snowflake/Databricks) DuckDB can't write. Driven as a subprocess/embedded
runner exactly like the dbt path (`routers/materialization.py`) and the DuckDB
path (`lakehouse_export.py`):

- **Source resource**: a dlt `sql_database` source (or a Parquet/filesystem
  source when the upstream is a lakehouse), scoped to the extract-side query the
  planner produced (§4).
- **Destination**: dlt destination for the target platform, credentials injected
  through the subprocess `env` (never written to disk — same no-secrets model as
  dbt's `profiles.yml` via `env_var()`).
- **Write disposition**: `replace` (full), `append` (immutable log), or `merge`
  (upsert on `primary_key_columns` from the `TransferBatch`).
- **Output**: one `TransferBatch` per run, persisted next to the target and
  linked from `:ServingDefinition {servingMode:'transfer_then_transform'}`.

## 3. Where the transform runs — the placement planner (Axis 2)

Because the Workbench compiles the product SELECT **once** into `select_body`,
transform placement is a **planner split of one IR**, not two pipelines. The
planner takes the compiled per-dataset plan and cuts it into two halves:

| Placement | Extract-side (pushed to source) | Target-side (after load) |
|---|---|---|
| `transform_on_extract` (ETL) | everything | nothing |
| `transfer_then_transform` (ELT) | nothing (raw) | everything |
| `hybrid` (EtLT — **default**) | projection, row-filter, partition-prune, **required masking** | joins, aggregations, SCD2, dedup windows, business rules |

**Implementation (shipped, Phase 2.2 — DuckDB targets):** the executor reuses the
base-relation list the compiler already surfaces per dataset
(`summary['tables']` = `["schema.table", …]`) rather than re-parsing the DSL.
`transform_placement.build_elt_plan` emits:

1. one **extract per base relation** (`CREATE TABLE tgt.wb_landing.<safe> AS
   SELECT * FROM src."schema"."table"`) — raw base tables landed in a
   `wb_landing` zone; and
2. a **target body** — the compiled `select_body` with each base relation
   rewritten `"schema"."table"` → `"wb_landing"."<safe>"`
   (`rewrite_base_relations`), run in the target (`USE tgt`) so the heavy
   joins/aggregations/SCD execute against the landed data.

The extract and target body are two projections of one IR — no forked transform
logic (the same invariant the view/dbt/lakehouse emitters honor). Because base
tables land **raw**, ELT push-down is **governance-gated to no-mask products**
(`_has_mask_transforms`, fail-safe) so unmasked values never leave the source;
masked products stay on `transform_on_extract`. The extract is **column-narrowed**
(Phase 2.3, `plan_extract_projections`) and **row-filtered** (Phase 2.4,
`plan_pushdown_filters` — anchor-only conjuncts) so only the needed slice moves;
warehouse targets (Snowflake/Databricks) run the target body **in the warehouse**
via dlt's `sql_client` — or, with `WB_WAREHOUSE_ELT_DBT=1`, via a scaffolded
`dbt build` in the warehouse (`generate_warehouse_dbt_project` + `_run_warehouse_dbt`,
Phase 2.5; live build deploy-gated, falls back on error).

`mask` transforms **force** their column onto the extract side regardless of the
chosen placement (governance: sensitive values never leave the source). This is
exactly the `transform_placement` the advisor already returns (`_placement()` in
`routers/serving_strategy.py`): `transform_on_extract` when a mask is present,
else `hybrid`.

## 4. Reconciliation under non-parity (design in now, don't retrofit)

Once a transfer transforms in-flight, **source-rows == target-rows parity is
gone** — reconciliation keys on the *expected post-transform* count/stats, not
raw parity. The shipped `TransferBatch` already carries what's needed:

- `operation_encoding` (`full_load` / … — Phase-1 lakehouse already stamps
  `transformed`) tells the reconciler a transform happened.
- `column_statistics` + `schema_fingerprint` for drift + distribution checks.
- `prior_run_reconciliation` (`ReconciliationEvidence`) for run-over-run deltas.

`verify_transfer` compares the landed target against the **plan's** expected
figures (the extract query's own row count after pushed filters, the target
model's expected grain), never against `SELECT count(*)` on the raw source. The
placement planner therefore records the expected post-transform row-count band on
the `TransferPlan` so `verify_transfer` has a ground truth to check.

## 5. How it flips the advisor

`routers/serving_strategy.py:_build_patterns` currently emits
`transfer_then_transform` as `not_yet_supported`. Phase 2:

- Add a `feasible` branch gated on
  `registry.is_usable(source, "transfer_source")` **and**
  `registry.is_usable(target, "transfer_target")`.
- When source ≠ target platform and both support transfer, this becomes the
  **recommended** pattern (today the recommendation is `None` for a cross-platform
  DB target — see `test_serving_advisor.TestCrossPlatform`). The
  `_placement()` output is already wired onto the pattern; no advisor-shape
  change is needed, only the feasibility branch + the manifest capability flip.
- The unified `ConfigureServingDialog` and MCP `get_serving_advice` render it
  with zero UI changes — the "Not available (coming soon)" entry simply moves
  into the feasible list.

## 6. Wiring & persistence

- **Serving stage**: a fourth `serving` exclusive-group member
  `serving_transfer` (modeled on `serving_lakehouse_export`), non-LLM, config =
  target connection + write disposition + placement override. `DEPENDENCY_GRAPH`:
  `["configure_serving"]`. `EXCLUSIVE_GROUP_DEPENDENTS["serving"]` maps it to a
  post-load verify gate (a dbt-build-in-target or a reconciliation stage).
- **Target**: reuses `MaterializationTarget.target_connection_id` (Phase-2A) — no
  new persistence for the destination; the source stays the bound `SourceBinding`
  / `:CONSUMES` borrow.
- **Serving node**: `:ServingDefinition {servingMode:'transfer_then_transform'}`
  carrying `targetPlatform`, `manifestUri` (the `TransferBatch`), `dbtProjectPath`
  (the target-side transform), `buildStatus`.
- **MCP**: `run_transfer` / `get_transfer_status` (mirroring
  `export_lakehouse` / `get_lakehouse_status`); `set_serving_mode` gains
  `"transfer"`.

## 7. Relationship to the migration program (`MigrationPlan`)

A one-shot `transfer_then_transform` serving run is the **cutover-agnostic** case.
The full **migration program** (Phase 4) drives the shipped
`platform/migration_plan.py` state machine
(`draft → assessed → initial_snapshot_* → change_capture_catching_up →
reconciled → cutover_* → observation → source_decommissioned`), where each
`TransferBatch` is one step and `MaintenancePolicy` (compaction / snapshot /
orphan owners) gates promotion into `cutover_in_progress`. The
`TransferExecutionProvider` is the execution primitive both share; the migration
program adds CDC catch-up + cutover orchestration on top.

## 8. Phasing

- **Phase 2** — `TransferExecutionProvider` + `dltTransferProvider` (Postgres /
  Snowflake / Databricks / DuckDB destinations), the placement planner, the
  advisor feasibility flip, `serving_transfer` stage + MCP. Flips the
  cross-platform DB target from `not_yet_supported` → `feasible`.
- **Phase 2b** — true Iceberg output: `pyiceberg` writer + local filesystem /
  SQLite catalog (REST / Glue / Unity for cloud), `LakehouseTableRef`, snapshot +
  `metadata.json` lifecycle. DuckDB reads Iceberg; writing is the hard part.
- **Phase 3** — cloud object stores + `warehouse_native_load` + `federated`
  (boto3 / GCS / ADLS session providers; SF external tables / stages / `COPY
  INTO`; Databricks external locations / Unity; FDW / Trino for federation). The
  advisor already surfaces these as `not_yet_supported` with a reason.
- **Phase 4** — the migration program (§7).

## 9. What's genuinely hard

- **The placement split** — cutting one `select_body` into extract-side vs
  target-side, and the resulting non-parity reconciliation (§4). This is the core
  Phase-2 risk; grounded by keeping the split a projection of one IR.
- **Type fidelity across the round trip** — timestamp-tz, decimal precision,
  nested types. The canonical `platform/type_system.py` (`map_to_canonical` /
  `map_from_canonical`, per-platform profiles) must be *actively used* on both the
  extract-schema capture and the target-DDL emission, recording lossy/ambiguous
  warnings (as Phase-1 lakehouse already does).
- **Credentials + resumability** — no-secrets subprocess env, idempotent
  re-runs keyed on `idempotency_key`, and `compensate()` for partial loads.
- **dlt schema evolution vs the declared contract** — dlt infers/evolves schema;
  the Workbench has a *declared* `:DProdColumn` contract. The provider must
  reconcile inferred vs declared and fail closed on incompatible drift, not
  silently widen the contract.

## 10. Non-goals for Phase 2

- No streaming/CDC serving (that's the migration program's `change_capture_*`).
- No `federated` (live cross-system query) — deferred to Phase 3.
- No new secret backend — reuse the shipped `platform/secrets.py` resolver.
