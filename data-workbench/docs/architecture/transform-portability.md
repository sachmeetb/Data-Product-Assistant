# Transform portability — neutral DSL + skill-owned capability artifact

**Status:** Phases 0–8 implemented — the capability artifact + AST validator +
`CompileResult` + read-only preflight (REST/MCP/editor) + staged enforcement gate
+ structured dialect-portable compilers (incl. the neutral `date_difference` op)
+ catalog/re-parse/real-engine conformance. **Remaining follow-ups:** the three
deferred Phase-7 cleanups (`format` date-token translation, `_compile_window`
NULLS-LAST polish, the full 5-way dialect-binding consolidation + `platform/sql_renderer`
fold/retire) and the live-graph steps (the sample-AGE re-author + flipping
enforcement to `block` after a clean portfolio audit). Load this when working on
`generate_view_ddl.py`, `dialect_sql.py`, `platform/transform_capabilities.py`,
`transform_preflight.py`/`transform_audit.py`, the `data-transform-translation`
skill, or `MappingReviewPanel.tsx` remediation.

## Why this exists — the incident

A `dpe-cf` product ("Workforce Roster") deployed to Databricks failed at deploy
with three coupled symptoms in its view DDL:

1. the SELECT used **`AGE(...)`** — a Postgres-only function — verbatim;
2. the FROM referenced **`workspace.public.employee`** when the real source
   tables live under `hr_core`;
3. the DDL was emitted in the **postgres** dialect even though the target was
   Databricks.

### Phase 0 code-level diagnosis

The sample project `dpe-08112026-01` is not present in every checkout, so this is
the **code-path** diagnosis; the live per-project config (which specific field
was empty) must be read from the running environment. All three symptoms trace
to two causes:

- **Verbatim raw-SQL emission (the structural gap).** In
  `generate_view_ddl.py:_compile_select_expr`, any mapping carrying a
  `transformExpression` is passed through `_substitute_inputs` and emitted
  **verbatim into whatever dialect is active** (`generate_view_ddl.py:1670-1672`).
  The `Dialect` layer abstracts only 5 primitives (`cast`, `hash_md5`,
  `hash_sha`, `regexp_replace_global`, `str_concat`); the raw-SQL kinds (`cast`,
  `format`, `concat`, `split`, `substring`, `case`, `arithmetic`, `expression`)
  and several nested SQL-bearing fields carry author-written Postgres SQL that
  no layer re-checks. `AGE(...)` therefore reaches Databricks untouched. This is
  systemic, not specific to one mapping.
  - **RESOLVED for the `expression` kind (emit path).** `_compile_select_expr`'s
    raw-`expression` branch now calls `_translate_raw_expression`, which renders
    the authored SQL to the target dialect via `dialect_sql.compile_expression`
    (consuming the `CompileResult.sql` the diagnostics pass already computed but
    discarded) — so a Postgres-authored `REGEXP_REPLACE(..., 'g')` emits the
    Databricks 3-arg form. It activates **only cross-dialect** (authored != target,
    so same-dialect products are byte-unchanged) and is **fail-open** (an
    unsupported/unknown function or any error leaves the expression verbatim, exactly
    as before; diagnostics/preflight remain the enforcement surface). The other
    nested raw-SQL surfaces below (lookup fragments, `filterPredicate`, join
    predicates) are still emitted verbatim — migrating them to structured ops or the
    same render step is the remaining work.

- **Resolver returned the postgres default + no served map.**
  `stage_execution._resolve_platform_context` (`:168-279`) derives the serving
  dialect and the per-source `source_served_map` from the project's
  `SourceBinding` / `MaterializationTarget` / the `:CONSUMES`'d source's served
  location (`pg_resolver.resolve_consumed_source_serving`). When none of those
  resolve a non-Postgres platform — e.g. the consumed source had not been
  materialized to Databricks at authoring time, so the code fell to the
  origin-borrow branch — the function returns `None` (`:263`, `:273`), so
  `serving_virtual_view` runs with `get_dialect(None)` → `PostgresDialect` and an
  empty `source_served_map`, and the FROM falls back to the raw
  `public.<table>` name. That produces symptoms 2 and 3.

The dialect derivation + `source_served_map` machinery **already exists and is
tested** (`test_dialect_autodrive.py`); the serving-wiring workstream is
therefore *diagnose-first* — only change resolver code once a specific project's
persisted config demonstrates a resolver bug rather than missing config. The one
known code defect is the MySQL inconsistency (see "MySQL reconcile").

The point-fix (re-author the one mapping + correct the source-location config) is
a hand-remediation of the incident. This document specifies the **systemic** fix:
store transforms dialect-neutral, resolve concrete SQL per platform at build time
through one shared compiler, and fail closed on anything untranslatable **at
author time**, not at deploy.

## The pipeline

Every SQL-bearing surface compiles through one shared pipeline. `sqlglot` is the
parse/render **engine**; the capability artifact is the **authority**.

```
parse(expressionDialect)          # legacy raw SQL is Postgres, not "neutral"
  → validate source AST           # walk funcs/nodes vs the artifact — fail closed on unknown/unsupported
  → lower neutral ops             # date_difference{semantics}, split_part, … → per-platform emit
  → render(targetDialect)         # sqlglot transpile as the mechanical re-printer
  → re-validate rendered AST      # catch anything the render didn't translate away
  → CompileResult{ sql?, errors[], warnings[], used_capabilities[], catalog_version }
```

**Transpile success ≠ capability.** Verified against installed `sqlglot`
30.16.0: `transpile(read=postgres, write=databricks)` emits `AGE(...)` unchanged,
and `write=bigquery|mysql` emits `SPLIT_PART(...)` unchanged. sqlglot accepts the
syntax without proving the engine supports it. So validation **walks the AST**
(`dialect_sql.validate_expression`) and checks every function name against the
artifact — a function that is `unsupported`, or unknown (neither in
`portable_functions` nor in the platform's map), is an **error**.

## Full inventory of SQL-bearing surfaces

The transform *kind* registry is **15** kinds (`write_mappings.py:76`:
`direct, cast, format, concat, split, substring, case, arithmetic, lookup,
literal, expression, bucket, mask, hash, window`). But portability must cover
**every surface that can carry engine-specific SQL**, several of which are nested
fields, not kinds:

| surface | where | portability treatment |
|---|---|---|
| raw-SQL kinds: `cast`, `format`, `concat`, `split`, `substring`, `case`, `arithmetic` | `:ColumnMapping.transformExpression` / `transformParams` | migrate to structured neutral ops (Phase 7); until then parse-validated as `expression` |
| `expression` (engineer escape hatch) | `:ColumnMapping.transformExpression` | **always** parse-validated against the artifact, never trusted verbatim; carries `expressionDialect`. At emit, `_translate_raw_expression` **renders it to the target dialect** (cross-dialect only, fail-open) via `compile_expression` — closing the verbatim-emit gap for this kind. |
| `lookup.aggregate_expression`, `lookup.filter_clause` | `transformParams` (nested) | raw SQL fragments → parse-validated |
| decorators `standardization`, `default_if_null` | `transformDecorators` | rendered via `_apply_decorators`; validated |
| `window` frame + ordering | `:DatasetTransform.windowSpecsJson`, `_compile_window` (`generate_view_ddl.py:1544`) | frame string is dialect-sensitive; `nulls_last` already dialect-aware |
| `literal.literal_value` + `target_type` | `transformParams` | literal + optional `dialect.cast`; type is a canonical type |
| `mask` / `hash` internals | `_compile_mask` (`:1401`), `_compile_hash` (`:1470`) | hardcoded `SUBSTRING(x FROM s FOR l)`, `||`, `REPEAT`, `REGEXP_REPLACE` — route through the dialect (Phase 7) |
| `:DatasetTransform.filterPredicate` | dataset-level WHERE | raw SQL → parse-validated |
| grouping / `groupingKeysJson` / `aggregateFunction` | dataset-level | function names validated |
| `:DatasetTransform.joinsJson` predicates | explicit-join FROM | join `on` predicates → parse-validated |
| derived-on-derived (`depends_on_product_columns`) | mapping references another product column | validated once resolved |
| SCD `snapshot` date literal | `:DatasetTransform.scdPolicyJson` | pinned-point-in-time literal |

## The neutral, versioned DSL

### `transformSchemaVersion`

Every SQL-bearing surface is stamped with `transformSchemaVersion` (added to
`:ColumnMapping` and the dataset-transform SQL fields; existing rows backfill to
`v1` via the `_migrate()`-style path). The stamp lets the compiler evolve
op-lowering semantics without silently re-interpreting old rows.

### Ops with explicit semantics

Neutral ops carry an explicit **semantics discriminator** wherever a
syntactically-valid translation could change the business result. The canonical
example is `date_difference`:

```
date_difference {
  unit:      year | month | day | ...
  semantics: completed_units | boundary_count | symbolic_interval
  start, end
}
```

- **`completed_units`** — completed *elapsed* whole units (age-in-years). PG:
  `EXTRACT(YEAR FROM AGE(end, start))`; Databricks/Snowflake:
  `FLOOR(MONTHS_BETWEEN(end, start)/12)`; MySQL: `TIMESTAMPDIFF(YEAR, start, end)`;
  BigQuery: `DATE_DIFF(end, start, YEAR)` **minus a month/day boundary
  adjustment**.
- **`boundary_count`** — calendar-boundary crossings (`YEAR(end) - YEAR(start)`);
  BigQuery `DATE_DIFF(..., YEAR)` and Snowflake `DATEDIFF('year', …)` are this,
  **not** completed years — the off-by-one that motivated the discriminator.
- **`symbolic_interval`** — the composite years+months+days interval. Only
  Postgres (`AGE`) expresses it natively; everywhere else it is `unsupported`
  with a "decompose per unit" remediation — **an error, never a quiet
  substitution.**

Other ops in the seed: `split_part` (native pg/dbx/sf; emulated bq via
`SPLIT()[SAFE_OFFSET(i-1)]`, mysql via nested `SUBSTRING_INDEX`). `cast` routes
through `type_system.CanonicalType` + `dialect.cast`; `concat` through
`dialect.str_concat`; `format` tokens as a neutral set. Each op documents its
null / type-coercion / timezone / locale / overflow behavior in the
`data-transform-translation` corpus `notes`.

### Composable ops — "transform of a computed value"

A transform whose input is *itself* a computed value (e.g. a tenure **band** over
completed-years-of-tenure) is expressed neutrally by nesting a value-op, not by
materialising the intermediate as a separate product column. `bucket` carries an
optional `transformParams.value` = a nested neutral op:

```
bucket:
  value: { op: date_difference, unit: year, semantics: completed_units }
  boundaries: [1, 3, 7]
  labels: [under_1y, 1_to_3y, 3_to_7y, 7y_plus]
→ CASE WHEN (<value lowered per-dialect>) < 1 THEN 'under_1y' … ELSE '7y_plus' END
```

`generate_view_ddl._compile_value_op` lowers `value.op` via the `Dialect`
(reusing `_compile_date_difference`) and `_compile_bucket` bands over the
parenthesised result — one self-contained, portable column. The value expr
references source columns through the **join alias** (`t1.hire_date`), so it
passes `_validate_alias_resolution`, unlike a bare product-column token in a
derived-on-derived CASE (which fails when the product maps the band but not the
intermediate column). An unsupported `(dialect, semantics)` fails closed
(`ViewGenerationError`) **and** is surfaced by the preflight (the diagnostics
accumulator capability-checks a bucket's nested `value.op`). `value.op` is a
general composition point (v1: `date_difference`; extensible to `cast`/
`arithmetic`). This is why the `hr.yaml` `tenure_band` recipe is a
`bucket`+`date_difference`, not a hand-written `AGE()` CASE.

### Raw SQL as a tagged escape hatch — `expressionDialect`

Legacy expressions have **no declared source dialect** — they are implicitly
Postgres. Parsing them as sqlglot-"neutral" risks a semantic misread, so the
`expression` kind carries an **`expressionDialect`** (default `postgres` on
backfill). The compiler parses with that grammar (`render_for_platform(...,
read=<expressionDialect>)`). When `expressionDialect` is **unknown** (no sqlglot
binding), the compiler **fails closed** with `unknown_source_dialect` and routes
the mapping to review rather than guessing.

## The capability artifact — one authoring source, one runtime artifact

**Authoring** lives in the client-forkable skill
`workbench-skills/skills/data-transform-translation/`:
`reference/portable_functions.yaml` (ANSI-safe on all served platforms) +
`reference/<platform>/transforms.yaml` (per-platform `native | emulated |
unsupported` for each divergent function + neutral op, each with an emit rule,
remediation, engine/version assumption, `status` (`verified` vs
`conformance-verify`), and a doc citation).

**Build** (`scripts/build_artifact.py`) validates every YAML against
`schema/transforms_source.schema.json`, merges + inverts them into one
**checksummed** `platform/transform_capabilities.<schemaver>.json`, and validates
the result against `schema/transform_capabilities.schema.json`. It **fails
closed** — a malformed YAML or unknown capability aborts the build and writes
nothing.

**Runtime** reads *only* the artifact via
`platform/transform_capabilities.py:load_capabilities()`, which re-verifies the
checksum on load (a hand-edited artifact fails closed) — **the backend never
imports the skill.** Capability lookup:

- `name ∈ portable_functions` → `native`;
- else `functions[name].platforms[target]` → its capability;
- else **unknown** → fail closed.

`native` and `emulated` both pass the AST validator; `unsupported` and *unknown*
both fail closed.

### Client fork → rebuild → validate → promote → rollback

A client that runs a different engine version forks the `reference/**` YAML,
reruns `build_artifact.py` (bump `--schema-version` only for a breaking shape
change; a rule change keeps the version, changes the checksum), runs the backend
guard (`test_transform_capabilities_artifact.py`), promotes by replacing the
`transform_capabilities.<ver>.json` file (the loader picks the highest `vN`), and
rolls back by restoring the previous file. Nothing else changes — the artifact is
a build output, treated like compiled code and never hand-edited.

## `CompileResult` — the single result contract

`dialect_sql.CompileResult{ sql, catalog_version, warnings[], errors[],
used_capabilities[] }` is the one shape every build/deploy path surfaces. **No
caller receives deployable `sql` while `errors` is non-empty** — `sql` is
populated only on a clean compile. `used_capabilities` records the
`FUNCTION@platform:capability` claims relied on (audit + conformance). REST
(`routers/serving.py`, materialization, transfer), MCP, the dbt/lakehouse/transfer
emitters, and the generated `run.py` all consume `CompileResult` and merely
surface its diagnostics; enforcement lives in the result, not in any one router,
so no path (dbt/lakehouse/transfer/direct-generator/`run.py`/future tools) can
bypass it.

`dialect_sql.compile_expression(sql, target, read=…)` is the per-expression
entry point: validate source → render → re-validate → `CompileResult`.

### Enforcement (Phase 6)

`WB_TRANSFORM_ENFORCEMENT` (read live, mirrors `WB_READ_ONLY`) stages the gate:

- **`off`** — the gate is disabled (no preflight cost on deploy/build).
- **`warn`** (default) — the gate runs and **logs** capability errors but never
  blocks; visibility lives in the Phase-5 editor callout + preflight endpoint.
  Safe to ship — no behavior change.
- **`block`** — refuses the deploy/build when a transform is unsupported on the
  target platform, returning a clean **422** (`transform_capability_block` with
  per-mapping `remediation`). Flip to this only **after** the portfolio audit
  (`transform_audit`) is clean, or existing deployed products may be blocked.

The gate is `transform_preflight.raise_if_blocked(result, context=…)` /
`raise_if_summary_blocked(summary, context=…)` — enforcement lives in the shared
result, not per-router, so no path bypasses it. Wired into: **serving deploy**
(`routers/serving.py`, re-runs the preflight against the resolved dialect before
executing the stored DDL — the AGE-incident path), **dbt** `materialize` +
`build_dbt_package` (off the scaffold summary), and the **ELT transfer target**
(`transfer_execution.py`, the target-platform compile). The generated **`run.py`**
is stdlib+driver only and cannot import the artifact, so it is gated by the
deploy endpoint that invokes it — enforcement is at the caller, not inside the
runner. The lakehouse path compiles DuckDB (not a served native-view platform),
so its gate is a deliberate no-op until DuckDB gets a capability profile.

### Surfacing (Phase 5)

The result is exposed read-only (no blocking — that's Phase 6):

- **REST** — `GET /api/projects/{id}/serving/transform-preflight` resolves the
  serving platform + view schema via `_resolve_platform_context` (the same path
  the deploy uses), runs `transform_preflight.preflight_product`, and returns the
  `CompileResult.to_dict()` plus `platform` / `view_schema`. `validated:false`
  means the check was skipped (non-served dialect), **not** "clean".
- **MCP** — `get_transform_preflight(project_code)` delegates to the same router
  handler (read-only, per-mapping `product_col` + `remediation`).
- **Editor** — `TransformPreflightCallout.tsx` (mounted at the top of
  `MappingReviewPanel`) auto-fires the endpoint and renders the AGE-style
  "won't compile on `<platform>`" findings with their fix, so the engineer
  re-authors (Replace) before deploy.

### How it's threaded (the summary channel)

`_generate_ddl_for_dataset` is a 4-tuple-returning function with many callers and
monkeypatch tests, so rather than change its signature, per-dataset capability
findings ride in the **existing** out-channel: `summary['transform_diagnostics']`
(`{errors, warnings, used_capabilities, validated, catalog_version}`). This
summary already flows to all three emitters (`generate_ddl` /
`generate_dbt_models` / `generate_lakehouse_models`, each rolled up at the
emitter level) and is persisted as `:ServingDefinition.summaryJson`. The backend
`transform_preflight.py` **lifts** that dict into the real `CompileResult`
(withholding `sql` when `errors` is non-empty).

`generate_view_ddl.py` runs both as a standalone subprocess and imported by the
backend, so its validator is an **optional, degrade-to-no-op hook**
(`_get_capability_validator`): it defensively adds the repo root to `sys.path`
and imports the backend validator, skipping silently if unavailable. **This is
not a bypass** — enforcement is backend-side: `transform_preflight.preflight_product`
re-runs the compiler over the mappings for the target platform, so the
virtual-view path (whose DDL is authored in the agent's subprocess and deployed
without re-generation) is validated by the backend regardless. Validation is
scoped to the five served native-view platforms; the `ansi`/`duckdb` dialects
report `validated=False` (skipped, **not** "clean").

## MySQL reconcile — RESOLVED (Phase 7)

MySQL was a served platform in the capability corpus but routed to the `"ansi"`
fallback in serving (`stage_execution._PLATFORM_DIALECT_MAP["mysql"] = "ansi"`)
despite `generate_view_ddl.MySQLDialect` existing and `dialect_sql.PLATFORM_TO_SQLGLOT`
mapping `mysql → mysql`. **Resolved by option (a):** `_PLATFORM_DIALECT_MAP["mysql"]`
now routes to `"mysql"`, so MySQL-served views emit MySQL-valid SQL (backtick
quoting, `CHAR` casts, no `NULLS LAST`, `SUBSTRING_INDEX` split emulation,
`CONCAT` instead of `||`), and — because `mysql` is a served native-view platform
in `_SERVED_VALIDATION_PLATFORMS` — the capability preflight now validates MySQL
transforms too. `test_dialect_autodrive.py` was updated to assert the new mapping.

## Delivery status

| phase | scope | status |
|---|---|---|
| 0 | incident diagnosis + hand-remediation | code-level diagnosis done (above); live per-project fix needs the running env |
| 1 | this spec + `transformSchemaVersion` model | **doc done**; graph field backfill pending |
| 2 | capability artifact: skill YAML + JSON schema + build step + generated artifact + loader | **done** (`data-transform-translation/`, `platform/transform_capabilities.{py,v1.json}`) |
| 3 | `CompileResult` + AST validation (`read=` param, walk funcs/nodes, fail closed); thread through the compiler + siblings | **done** — validator in `dialect_sql.py`; findings threaded via `summary['transform_diagnostics']` through all three emitters; lifted to `CompileResult` by `transform_preflight.py` |
| 4 | `expressionDialect`/`transformSchemaVersion` on `:ColumnMapping`; read-only portfolio audit | **done for `:ColumnMapping`** — `write_mappings.py` stamps both; `MAPPINGS_QUERY` coalesces (`postgres`/`v1`) as the read-side backfill; `transform_audit.py` is the portfolio audit. Dataset-transform SQL fields (`filterPredicate`, join predicates) stamp is a follow-up |
| 5 | read-only preflight endpoint + MCP tool + editor remediation; re-author sample AGE via `date_difference` | **done (surfacing)** — `GET /api/projects/{id}/serving/transform-preflight` (`routers/serving.py`), MCP `get_transform_preflight`, and `TransformPreflightCallout.tsx` in `MappingReviewPanel` (errors + remediation at author time). Sample AGE re-author needs the live graph (the `date_difference` op *compiler* lands in Phase 7) |
| 6 | staged enforcement (warn → block) across all build/deploy paths | **done** — `WB_TRANSFORM_ENFORCEMENT` (`off`/`warn` default/`block`); gate in `transform_preflight.raise_if_blocked` / `raise_if_summary_blocked`; wired into serving deploy (clean 422), dbt `materialize` + `build_dbt_package`, and the ELT transfer target. `run.py` is covered by the deploy-endpoint gate (it can't import the artifact) |
| 7 | structured compilers for `cast`/`split`/`substring`/`concat`; the neutral `date_difference` op; fix mask/hash Postgres-isms; MySQL reconcile | **mostly done** — `Dialect.substring`/`split_part`/`date_difference` + structured compilers routed through the Dialect (`generate_view_ddl.py`); `date_difference` kind (all 5 platforms, `symbolic_interval` fail-closed off-Postgres) with op-level preflight; `_compile_mask`/`_compile_hash` now emit portable `SUBSTR`/`CONCAT` (was ANSI `SUBSTRING FROM/FOR` + `\|\|`); MySQL reconciled to `MySQLDialect`. **Deferred:** `format` date-token translation, `_compile_window` NULLS-LAST polish, the full 5-way dialect-binding consolidation + `platform/sql_renderer` fold/retire (documented follow-ups) |
| 8 | golden + real-engine conformance; sqlglot re-parse tests; sync guard | **done** — `test_transform_catalog.py` (every offered op/kind renders-or-flagged on every served platform; the compiler's fail-closed choice AGREES with the artifact; rendered SQL re-parses under the target dialect); sync guard in `test_transform_capabilities_artifact.py` (served-platform set consistent across artifact / `_SERVED_VALIDATION_PLATFORMS` / `PLATFORM_TO_SQLGLOT`; every op covers every platform); opt-in credential-gated `test_real_engine_conformance.py` (executes the seeded `date_difference` renderings on real engines — dates chosen so completed-age ≠ boundary-count) |

### Tests (seed, implemented)

- `test_transform_capabilities_artifact.py` — artifact loads + checksum verifies;
  the committed artifact is **in sync** with the YAML (rebuild reproduces the
  checksum); a corrupt/missing artifact fails closed; the AGE/SPLIT_PART matrix;
  every `unsupported` entry carries remediation; `date_difference` declares all
  three semantics on every served platform.
- `test_transform_preflight.py` — the **transpile-success ≠ support** proof (AGE
  for Databricks, SPLIT_PART for BigQuery/MySQL re-emit unchanged yet are
  flagged); AGE matrix; portable expressions clean on every platform;
  fail-closed on unknown function / subquery / multi-statement / unknown source
  dialect; `CompileResult` yields `sql` only when clean.
