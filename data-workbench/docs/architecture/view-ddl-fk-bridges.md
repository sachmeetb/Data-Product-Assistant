# View-DDL: FK-driven join construction (auto-bridges + temporal wrap)

> **Read on demand.** The high-level "consumer view fails with no-FK-path" recovery hint lives in CLAUDE.md under the dpe-cf flow specifics. This doc carries the full algorithm for `_build_from_fk_inferred`, `_resolve_bridges`, `_rank_bridge_paths`, temporal-bridge wrapping, and the multiplication-risk warning. Open when working on `generate_view_ddl.py` FROM-clause assembly or debugging an unexpected join graph in a virtual view.

## Auto-bridge in `_build_from_fk_inferred`

When mapped tables span the FK graph but don't all sit in one connected component of *direct* FKs (e.g. `employee` and `department` reach each other only via the junction table `department_employee`), `_resolve_bridges` BFS-discovers the shortest path through unmapped intermediates and auto-includes them in the FROM/JOIN scope. The bridge contributes **no SELECT columns** — its only purpose is to enable the join chain. The widened `FK_QUERY` / `DPROD_FK_QUERY` fetch the full FK graph per project / source-product contract (not just edges touching mapped tables) so the BFS has the full graph to walk. Joins are emitted by a worklist that defers each table until at least one of its FK partners is already in scope, so bridges that share a `tables[]` entry with a dependent always come first in the rendered DDL.

## Name-similarity ranking when multiple shortest paths exist

BFS often finds two equally-short bridge candidates (e.g. `department_employee` and `department_manager` both length-2 between `employee↔department`). `_rank_bridge_paths` computes a composite score = `primary * 10 + secondary` where:

- **Primary** (name similarity): count of endpoint name tokens appearing as substrings in the intermediate node names. `department_employee` contains both `employee` and `department` → primary 2; `department_manager` contains only `department` → primary 1.
- **Secondary** (PO-approved classification): sum of `_RELATIONSHIP_KIND_WEIGHTS` over the intermediates' `:TableDescription.relationshipKind` values. `general_membership` = +3, `lookup_dimension` / `fact` = +1, `specialization` = −1, others = 0. The classification is set at metadata-enrichment time (skill: `metadata-enrichment`, script: `write_table_descriptions.py`), reviewed by the PO in `SourceProductValidationPanel`'s **Tables** tab, then propagated to `:DProdOutputDataset.relationshipKind` by `_generate_dprod` so the ranker can read it without joining back through the contract.

Multiplier ensures primary always dominates secondary; the secondary signal breaks ties when name patterns are equally informative (e.g. two junctions that both contain both endpoint names — classification picks the general one). Strict winner (top total score > second-best) auto-picks; tied top tier falls through to `_AmbiguousBridges`. Auto-picks are surfaced as `summary['auto_bridge_choice']` and as a `-- auto-bridge: <path> (<rationale>)` comment with the relationship_kind embedded. Override path remains: declare explicit `:DatasetTransform.joins[]` or map a column from the desired junction.

## Auto-temporal bridge wrapping

When an auto-bridge table has columns matching temporal patterns (`to_date`, `valid_to`, `effective_date`, `from_date`, `*_at`, etc. — see `_TEMPORAL_PATTERNS`), the bridge LEFT JOIN is wrapped in a derived table with `ROW_NUMBER() OVER (PARTITION BY <fk_to_anchor> ORDER BY <temporal_col> DESC NULLS LAST) = 1` so the bridge contributes the latest row per natural key. Eliminates row-multiplication when the bridge is a history table (e.g. `department_employee` with from/to dates). `_pick_temporal_order_column` ranks pattern matches by specificity (`to_date` beats `_at`). `TABLE_COLUMNS_QUERY` fetches column lists for all in-scope tables + FK-graph neighbours so the detector has the data it needs. The `-- temporal bridge:` audit comment names the column.

## Mapped SCD-satellite current-row dedup (`_satellite_needs_dedup`)

A mapped (non-bridge) table pulled into the base FROM as a LEFT-joined satellite — e.g. `job_assignment_history` brought in only to supply a lookup's *key* (`job_title` via `job_id`) — fans the view out to one row per historical version unless deduped. `_build_from_fk_inferred` now wraps such tables in the same current-row `ROW_NUMBER() OVER (PARTITION BY <fk_to_anchor> ORDER BY <temporal> DESC) = 1` derived table the bridges get (plus a `WHERE <is_current> = true` narrow when an `is_current`-style flag exists). The anchor table (`table_list[0]`) is never wrapped — it defines the grain.

`_satellite_needs_dedup(table_key, meta)` decides: a **strong SCD signal** — an `is_current` flag OR an effective-dating `from/to` pair — triggers dedup *even when the relationshipKind classifier tagged the table `audit_log`* (the job_assignment_history case: labelled audit_log but shaped as an SCD-2 dimension). Absent those signals it falls back to the conservative guard (skip `audit_log`/`fact` kinds and plain audit timestamps). Auto-deduped satellites are reported in `summary['auto_satellite_dedup']` + a `-- scd:` audit comment, and are excluded from the multiplication warning below.

When `latest_only` is declared but can't synthesize an outer dedupe (no product temporal column), the lowering now recognizes **structural satisfaction**: if every effective-dated satellite was auto-deduped and the base is current-state, the policy is satisfied and the old "cannot be lowered" warning is suppressed (`summary['scd_satisfied_via_satellite_dedup']`).

## Multiplication-risk warning for remaining temporal sources

For mapped tables with a temporal column but NO strong SCD signal (and not already auto-deduped), view-DDL still emits `summary['multiplication_warnings']` + a `-- warning:` comment listing the table + temporal column + a suggested `dedupe` block. The engineer's fix is to author a dedupe, or — the PO-facing path — declare an SCD-1 `latest_only` policy in the Shape wizard step (synthesizes a ROW_NUMBER dedupe at view-DDL time). Raw `:DatasetTransform.dedupeJson` is still Cypher-authored.

## Explicit joins also get the SCD dedup

`_build_from_explicit` (Phase 4, when `:DatasetTransform.joins[]` is set) applies the **same** `_satellite_needs_dedup` wrap to LEFT/INNER-joined SCD satellites. This is what lets a consumer anchor on one source product's table (`salary_history`) and bridge to another product's history table (`job_assignment_history`) via an explicit join without re-introducing per-key fan-out. The FROM anchor (`joins[0]`) is never wrapped.

## Band/grade columns must bucket (`semantic_warnings`)

A product column NAMED like a band/grade/tier/bracket but whose transform carries no bucketing (no CASE, no `bucket` kind) passes its source value through verbatim — leaking the raw (often sensitive) value under a column that claims to be a discrete band. View-DDL flags this in `summary['semantic_warnings']` + a `-- warning:` comment. The generator can't invent band boundaries (domain knowledge); the mapping must author a bucketing CASE in `transformExpression` (or use the `bucket` kind). See `docs/architecture/transformations.md`.

## Transitive natural-key chaining (cross-product bridges)

The cross-product natural-key fallback (the `if not progress:` block in `_build_from_fk_inferred`) bridges a stuck table to **any already-joined table** — anchor first (when the anchor shares an identity key the emission is byte-identical to the pre-chaining behaviour), then other joined tables ranked by `_score_chain_key` (+2 when the key's stem names either endpoint table, +1 when it's the declared grain natural key from `dedupe.keys[0]`/`grouping_keys[0]`; ties break on join order). Because the worklist loop re-runs the fallback, multi-hop chains fall out naturally: `account —account_id→ transaction —customer_id→ customer` resolves in two passes even though `account` and `customer` share no column. The three emission modes (as-of for SCD-2 targets, current-row for effective-dated tables, plain equi) are unchanged; the **as-of pivot always stays on the anchor's span-start column** — the anchor defines the grain regardless of which alias the equi-key references. Each synthesized bridge is reported in `cross_product_bridges` (→ `summary['auto_satellite_dedup']` with `cross_product_bridge: true`) carrying a `via` field naming the partner table, and rendered as a `-- cross-product bridge:` audit comment. When a chain routes another table THROUGH a mapped `fact`/`audit_log` (ledger-grain) table and no dedupe/grouping is declared, an informational `multiplication_warnings` entry flags that the joined rows sit at the ledger's grain.

## Junction bridges from consumed-but-unmapped datasets (pure bridge, DISTINCT projection)

When transitive chaining still leaves disconnected components (no shared identity key at all — the banking case: `account` has no `customer_id`), the generator searches the **CONSUMES'd products' other output datasets** (`CONSUMED_DATASETS_QUERY`) for a **junction**: a dataset sharing **different** identity keys with the two sides (`transaction` carries `account_id` + `customer_id`; same-key sharing is the shared-dimension case chaining already handles). Scope is deliberately **CONSUMES-only** — a dataset in a product the consumer never subscribed to must not silently become a dependency. (A same-domain marketplace search was considered and rejected for auto-synthesis; if ever added it should only *list* candidates with an "add the CONSUMES edge first" instruction.)

Candidates are ranked mirroring `_rank_bridge_paths`: endpoint-name containment (0..2) × 10 + `_RELATIONSHIP_KIND_WEIGHTS`. A **strict winner** is synthesized; a tie raises `ViewGenerationError` listing every top-tier candidate (advise-not-guess); no candidate extends the classic no-FK-path error with a 4th cause ("the junction may live in a product this consumer doesn't CONSUME").

**Fan-out safety:** the junction contributes no SELECT columns, so it is emitted as a DISTINCT-projected derived table —

```sql
LEFT JOIN (
    SELECT DISTINCT account_id, customer_id
    FROM public.vw_transaction
) AS t3
    ON t3.account_id = t1.account_id
```

— collapsing N ledger rows per entity to the distinct link pairs. The projection is patched after the join loop completes so it covers every column later joins reference on the junction alias. SCD-2 target + effective-dated junction: the validity columns join the DISTINCT projection and the ON gains the as-of interval vs the anchor pivot (period-correct linkage without version fan-out). A genuinely M:N junction (joint accounts) still multiplies — surfaced as an informational `multiplication_warnings` entry recommending dedupe/grouping, never a guessed aggregation. Synthesized junctions are reported with `mode: "junction_distinct"`, `bridge_only: true` in `summary['auto_satellite_dedup']` + a `-- junction bridge:` audit comment.

**Explicit-joins path:** a `joins[]` entry flagged `bridge_only: true` (or any resolvable entry no mapping reads from) gets the same DISTINCT wrap in `_build_from_explicit`; its relation resolves through `_resolve_explicit_join_aliases` (which now also returns alias→relation for unmapped entries — previously they emitted a broken `?.?` placeholder). `bridge_only` round-trips through `PUT /dataset-transform/joins` (`_validate_joins` persists it only when true), the ODCS `transform.joins[]` canonicalizer (accepts `bridgeOnly`), and `JoinsOverridePanel`'s per-row "Pure bridge" checkbox.

## Join-connectivity preflight (`backend/join_preflight.py`)

The base tables a dataset's mappings read from must form ONE FK-connected component or serving fails with `ViewGenerationError: … no FK path …`. This bites consumer-aligned products spanning **two source products** — `:REFERENCES` never crosses products (only `:CONSUMES` does), so two tables describing the same entity (`salary_history` + `employee`, both keyed by `employee_id`) are never FK-connected. `analyze_dataset_connectivity` union-finds the mapped base tables over `:REFERENCES`, and on a split runs the same **transitive chain planner** the generator uses (anchor component first, then BFS over shared identity keys, `_score_chain_key` tie-breaks) followed by the same **junction discovery** over the CONSUMES'd products' other datasets — the two sides carry paired "mirrors — keep in sync" comments and an executable consistency test (`test_preflight_generator_consistency_banking`). It emits a `recommended_joins` payload shaped exactly like the `PUT /dataset-transform/joins` body (one-click apply), in dependency order, each chained entry carrying an informational `via {table, key}` and junction entries flagged `bridge_only: true`. Additive result fields: `junction_bridges[]` (synthesized junctions + their source's `src_deployment_status`), `junction_candidates[]` (tied candidates when auto-pick refused), and `warnings[]` (e.g. junction's source view not deployed). A bridgeable split reports `connected: true, bridged_via: "auto_natural_key"`; only a table with no chainable key and no unambiguous junction is a genuine gap (`connected: false`, `unbridged_tables`). It respects an already-declared `joins[]` (`bridged_via='explicit_joins'`). Surfaced at `GET /api/projects/{id}/serving/join-preflight`; the `data-mapping-neo4j` skill (Step 3.6) runs the same check while authoring so it *recommends* the bridge instead of emitting a dead-end mapping. Lookup reference tables are NOT base tables (keyed LEFT JOINs need no FK path) and are excluded.
