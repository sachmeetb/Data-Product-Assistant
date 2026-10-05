# Transformations

> **Read on demand.** Column-level transformation model — four authoring sources, per-kind `transformParams` schemas, view-DDL emission. Open when working on `generate_view_ddl.py` SELECT compilation, `TransformEditor.tsx` per-kind UI arms, or extending the mapping skill's `VALID_TRANSFORM_KINDS`. The dataset-level peer (`:DatasetTransform`, FROM/JOIN assembly, SCD lowering, dialect picker) is in `dataset-transform.md`.

## Four-source `transformAuthor` model

`:ColumnMapping` carries a structured DSL (concat / cast / lookup / case / arithmetic / literal / ...). `transformAuthor` mirrors the four-source DQ rule pattern:

- **`po_hint`** ↔ `spec` — declared in ODCS YAML `transform` block; persisted as `:DataContractProperty.transformHint` and propagated to `:DProdColumn.transformHint` via `_generate_dprod`.
- **`steward_catalog`** ↔ `domain` — matched from `playbook/transformation_catalogs/{domain}.yaml` by `match_transformation_catalog.py`.
- **`engineer`** ↔ `user` — hand-edited in `MappingReviewPanel`.
- **`ai_suggestion`** ↔ `observation` — LLM-proposed when no hint or catalog template applies. Prompt enforces priority: hint → catalog → LLM.

Structured fields drive the editor UI and `generate_view_ddl.py` (word-boundary substitution, decorator wrapping, LEFT JOIN for `lookup`).

## Constant `literal` kind

Product columns whose value is a fixed constant use `transformKind='literal'` with `transformParams.literal_value` holding the engineer-quoted SQL literal. Literal mappings have **no** `:MAPS_SOURCE_COLUMN` edges — source-required checks are gated on `kind != 'literal'`. View-DDL skips them in FROM/JOIN computation; `_compile_select_expr` emits the value verbatim with optional cast.

## Phase 1 column-level kinds

- **`bucket`** — discretise a continuous column into ordered bands. `transformParams: {boundaries: [...], labels: [...]}`, N+1 labels for N boundaries. Compiles to a `CASE WHEN` chain.
- **`mask`** — format-preserving redaction. `transformParams: {algorithm: 'keep_last' | 'keep_first' | 'middle', keep_n: 4, mask_char: 'X', keep_format: bool}`. When `keep_format=true`, non-alphanumerics (dashes, dots, spaces) survive so `4111-1111-1111-1234` → `XXXX-XXXX-XXXX-1234`.
- **`hash`** — irreversible digest. `transformParams: {algorithm: 'md5' | 'sha1' | 'sha256', salt: ''}`. md5 is built-in PG; sha1/sha256 use pgcrypto's `digest()`. **Not a security primitive** — salt is for determinism / namespace separation, not protection.

## `lookup.selection_strategy`

Extends the `lookup` kind with five shapes:

- `equi` (default, historical) — `LEFT JOIN ref ON src.fk = lk.key`, return `lk.value_column`.
- `latest` — derived-table join with `ROW_NUMBER() OVER (PARTITION BY key ORDER BY <order_by_column> DESC NULLS LAST) = 1`. For `current_X` / `most_recent_X` patterns. Requires `order_by_column`.
- `aggregate` — derived-table join with `GROUP BY key`. Requires `aggregate_function ∈ {SUM, COUNT, AVG, MIN, MAX, COUNT_DISTINCT}` — or a raw `aggregate_expression` (below).
- `exists` — same equi shape, SELECT becomes `(lk.key IS NOT NULL)`. For `has_X` / `is_X` boolean flags.
- `asof` — point-in-time join against an effective-dated reference (`lk.effective_from <= src.<pivot> AND (lk.effective_to IS NULL OR > pivot)`). For SCD-2 targets whose lookup reference is itself history. Requires `effective_from_column` + `pivot_column`; `effective_to_column` optional.

**`aggregate_expression` (composite aggregates).** When the target column is a formula over two or more aggregates of the same reference table (recency/frequency scores, ratios of sums), a single `aggregate_function` can't express it. `params.aggregate_expression` carries raw SQL over the lookup table's columns — any number of aggregates, each with its own `FILTER (WHERE …)` when windows differ — emitted verbatim as `SELECT <key_column>, <aggregate_expression> AS agg_value FROM <lookup_table> [WHERE <filter_clause>] GROUP BY <key_column>`; the compiled value is `lk.agg_value`. Supersedes `aggregate_function` / `value_column` / the mapping's `transformExpression` when present. Note `filter_clause` constrains EVERY aggregate in the scan — use per-aggregate `FILTER` for windowed sub-aggregates. Authoring surfaces: TransformEditor's aggregate arm (advanced textarea), the data-mapping skill (worked churn-score example in its SKILL.md), and the project-chat-assistant "Guide me" cheat sheet.

Lookup-derived joins are **deduped** by `(table, src alias+col, strategy, order_by, agg_fn-or-aggregate_expression, filter_clause, asof columns)` so two product columns sharing the same history-table scan emit one derived-table join, not two; differing aggregate expressions get their own scans.

## Derived-on-derived columns (`depends_on_product_columns`)

A mapping whose `transformExpression` references **other product columns of the same dataset** (not source columns) declares them in `transform_params.depends_on_product_columns` (a JSON list of product-column names). The view generator defers such columns to an outer **enriched CTE** layer where the referenced columns exist as real columns, with greedy topological layering so chains of derived-on-derived work (`generate_view_ddl.py:_generate_ddl_for_dataset` → `deferred_derived` / `_substitute_product_aliases`). Authoring surfaces: TransformEditor's raw-expression arm ("Depends on product columns" field) and the data-mapping skill guidance. Use it to compose across sources (e.g. a score combining two aggregate columns that are product columns in their own right); prefer a single `aggregate_expression` lookup when all inputs come from one reference table.

**Raw `expression` is dialect-translated at emit.** A raw `transformExpression` is authored in one grammar (`expressionDialect`, default `postgres`) but served on whatever platform the product targets. After `_substitute_inputs`, `_compile_select_expr` runs `_translate_raw_expression` to render it to the target dialect via `dialect_sql.compile_expression` (cross-dialect only; **fail-open** — an unsupported/unknown function leaves it verbatim). So a Postgres-authored `REGEXP_REPLACE(..., 'g')` emits the Databricks 3-arg form rather than a `'g'`→INT cast failure. See [`transform-portability.md`](transform-portability.md); structured kinds remain preferable since they don't depend on the transpiler.

### `:LOOKUP_VIA` lineage edge

`lookup_table` / `value_column` / `key_column` live as bare-name strings in `transformParams` (no URI, no edge), so for a long time a lookup's reference table — often a **different** CONSUMES'd source product than the mapping's primary `:MAPS_SOURCE_COLUMN` source — was invisible in lineage. It is now materialised as `(:ColumnMapping)-[:LOOKUP_VIA {role, strategy, createdBy}]->(:Column | :DProdColumn)`, `role ∈ {value, key}`. The edge is additive — no existing query changes behaviour unless it opts into the traversal.

- **Created by:** the data-mapping skill's `write_mappings.py` at authoring time, and the backend resolver `workbench/backend/lookup_via.py:reconcile_lookup_via` (auto-invoked on mapping approve / replace, and runnable as a backfill via `scripts/backfill_lookup_via.py --project-code <code>`). Both resolve the bare `lookup_table` by stripping a `schema.` qualifier + `vw_` override to the bare physical name, then matching `:Dataset.name` (catalog) or `:DProdOutputDataset.physicalName` reachable via the consumer contract's `:CONSUMES` (dprod). Unresolvable names (undiscovered reference table / ambiguous bare name) are skipped — the DDL stays the source of truth.
- **Read by:** marketplace `LINEAGE_QUERY` / `MARKETPLACE_MAPPING_GRAPH_QUERY`, engineer `reviews.py:GRAPH_MAPPINGS_QUERY` (+ `GRAPH_LOOKUP_QUERY`), and `summary.py:MAPPINGS_DETAIL`. The shared `lookup_via.merge_lookup_graph_rows` folds lookup rows into the graph payload; lookup sources render with a distinct dashed edge + "lookup" badge (`MappingGraphView.tsx`) so they aren't mistaken for primary 1:1 mappings.

## Steward escalation

Sets `:ColumnMapping.status='steward_review'` + `transformEscalationReason`. Steward sees `TransformationEscalationsPanel`; answering inline re-emits with `transformAuthor='steward_catalog'`.

## Apply protocol — `column_transform_set`

The product chat assistant emits `{applies_to: 'column_transform_set', column: '<name>', transform: {kind, inputs, params, decorators}}` to set a transform hint on a single **custom** column (catalog-picked columns are not eligible — assistant first proposes `schema_add_columns`). Hint mirrors `:DataContractProperty.transformHint` and propagates to `:DProdColumn.transformHint` via `_generate_dprod`. Engineer sees `transformAuthor='po_hint'` in the mapping queue.
