# Connected-Estate Data-Product Feasibility (top-down)

> **This is the architecture deep-dive.** For the two companion docs:
> - **[`data-product-feasibility.md`](data-product-feasibility.md)** — the
>   **functional guide** (read first): what the capability does, how the grading
>   strategy works in plain terms, and how a Data Product Owner should use it.
> - **[`estate-discovery.md`](estate-discovery.md)** — the hands-on **setup &
>   connection reference** (per-platform fields, scan drill-down, troubleshooting).
> - **[`offline-extraction.md`](offline-extraction.md)** — the **offline** path for
>   clients who won't grant a live connection: a client-run, no-LLM extraction kit
>   produces a reviewable manifest DW replays into an `:EstateScan` identical in shape
>   to the live scan this doc describes (same `write_scan_snapshot`, same graph).
>
> The canonical internals are in the root `CLAUDE.md` and `workbench/backend/CLAUDE.md`;
> this document explains the *why* behind them.

> A new **top-down entry point** for the Product Workbench. You arrive with a *catalog of
> desired data products* and ask: **which of these can I build right now, from what my
> organization already has?** Data Workbench connects to your live platforms itself, scans
> the estate, and grades every desired product with a stoplight.

## The idea in one minute

Most of the Workbench is **bottom-up**: start from a source, discover it, shape a product.
This capability flips it. You start from **what you *want*** — a per-domain catalog of
reference data-product specs (e.g. "a Credit Card product with these attributes at this
grain") — and the Workbench tells you **how buildable each one is today** against your
connected estate, then helps you act on the buildable ones.

Four business tiers (a stoplight):

| Tier | Colour | Meaning | What you do |
|---|---|---|---|
| **`ready`** | 🟢 bright green | A governed, published product already matches almost exactly. | Adopt / endorse it in the marketplace. |
| **`adaptable`** | 🟢 light green | A close published product exists but needs a bounded, allowed adaptation (a rename, a currency normalization, a weekly→monthly **aggregation**). | Author a consumer product that CONSUMES it, seeded from the spec. |
| **`assemblable`** | 🟠 amber | The raw data exists in the estate but isn't a governed product yet. | Compose a modernization portfolio (source products first, then a consumer/aggregate) via intake. |
| **`absent`** | 🔴 red | No matching data anywhere. | Log it as a gap. |

> **Why not monthly→weekly?** You can always roll finer data up to a coarser grain
> (weekly → monthly is an **aggregation**). You **cannot** reliably split a coarse grain
> back into a finer one without finer-grained sources — so a spec that needs finer grain
> than the estate offers is a genuine gap, never an `adaptable`.

### A worked example

Teresa's team wants a **Credit Card** data product with 30 attributes, keyed on `card_id`,
at current-state grain. The Workbench:

1. **Scans** the connected Postgres estate and finds a `public.cards` table + a
   `public.card_txns` table.
2. **Compares** the spec's 30 attributes against (a) every *published* product and (b) the
   raw estate tables — assigning each spec attribute to at most one column (no
   double-counting).
3. Finds a published **"Cards"** product covering 95% of the required attributes, needing
   only a rename and a currency normalization → tier **`adaptable`**.
4. Offers the action: *"Author a consumer product that CONSUMES the Cards product, adapted
   per these notes."*

Meanwhile a **"Collections 360"** aggregate spec finds no matching product but its raw
tables exist and join on `customer_id` → tier **`assemblable`**, and the Workbench composes
a modernization portfolio for review.

## Bounded context — Connected Estate vs Pulse (load-bearing)

Data Workbench already has an estate-discovery surface: the **Pulse integration**
(`routers/discovery.py` + `playbook/discovery/estate.yaml`), where an external tool (Pulse)
supplies an estate *assessment* and DW presents/acts on it **bottom-up** for
migration/modernization/disposition. The Connected-Estate capability is a **separate
bounded context** — DW connects to live platforms and discovers the estate **itself**,
top-down.

| Capability | Source of estate knowledge | Purpose | Routes |
|---|---|---|---|
| **Pulse discovery** (existing) | Pulse-provided assessment | Bottom-up migration / modernization / disposition | `/api/projects/{id}/…` (`routers/discovery.py`) |
| **Connected Estate** (this) | **Live DW platform scans** | **Top-down data-product feasibility** | **`/api/estates/*`**, **`/api/feasibility/*`** |
| Future bridge | Pulse snapshot imported into the Estate model | Optional convergence | (adapter, later) |

**Consequence:** the Pulse routes, files, UI, and MCP behavior are **unchanged**. This
capability has its own SQL models, its own `:Estate`/`:EstateScan` graph anchors, its own
Product Workbench pages, and its own PO MCP tools. Any future Pulse convergence is an
**adapter into the Estate model**, never a prerequisite. (A test pins this boundary:
`tests/test_connected_estate_boundary.py`.)

## How it works

```mermaid
flowchart TD
  subgraph PW[Product Workbench]
    E[/product/estate\nEstatePage] --> F[/product/feasibility\nFeasibilityGrid]
  end
  E -->|create Estate + EstateSource\n(pick a connection + namespace policy)| API1[/api/estates/*/]
  E -->|launch scan| SCAN[EstateScan queued]
  SCAN --> W[estate_worker\nleased]
  W -->|deterministic, provider-driven\nlist_relations + list_columns| G[(Neo4j\n:Estate/:EstateScan\n:EstateDataset/:EstateColumn)]
  F -->|evaluate| API2[/api/feasibility/*/]
  API2 --> RUN[FeasibilityRun queued]
  RUN --> W
  W -->|evidence + verdict| SCORES[(FeasibilityScore\nper spec)]
  SCORES --> F
```

### 1. The estate (identity + connection + scan-state are split)

Three SQL tables, deliberately **not** one row:

- **`Estate`** — a stable business scope (`name`, `domain`, `status`).
- **`EstateSource`** — a registered `PlatformConnection`, scoped to one **catalog**
  (Unity Catalog / database container; empty for 2-level platforms like Postgres/MySQL)
  + an **editable namespace policy** (`{mode: all|include|exclude, namespaces: [...]}` — the
  saved schema selection). Add **several sources — one per catalog** — to cover more of a
  platform in one estate. You can rename, disable, edit the schema selection, or hard-delete
  a source (which cascades away its scans + graph subtree); the catalog itself is immutable
  once the source has been scanned (delete and re-add to switch).
- **`EstateScan`** — one discovery **snapshot**. A rescan is a *new* row
  (`scan_version` increments); prior snapshots are retained.

Each scan is materialized as **first-class graph nodes** DW owns:

```
(:Estate {uri:"estate:{id}"})-[:HAS_SCAN]->(:EstateScan {uri:"estatescan:{id}", version})
(:EstateScan)-[:OBSERVED]->(:EstateDataset {uri:"estatedataset:{estate}:{source}:{db}.{schema}.{table}"})
(:EstateDataset)-[:HAS_COLUMN]->(:EstateColumn)
```

Identities carry **source + database + schema + relation**, so two schemas (or two sources)
with the same table name never collide. Datasets/columns have **stable URIs across scans**;
a feasibility read pins cleanly to one scan via `:OBSERVED`.

### 2. The scan is deterministic — NOT an LLM

The broad metadata scan uses the platform **`DiscoveryProvider`** directly
(`list_namespaces` → `list_relations` → `list_columns`). Running an LLM across hundreds of
tables would be slow, costly, and non-deterministic. Agent skills are reserved for the
**selective deeper pass** (PII-safe profiling of PO-chosen datasets).

**Rescan diff:** new objects added, changed schemas versioned, **deleted objects tombstoned**
(never silently dropped), profiles marked stale when a dataset's columns changed. A **live scan
progress** feed (`EstateScan.scan_progress_json`: namespaces done/total, current namespace,
relations/columns found) drives the page's scan bar + a `Scanned <ts> · <duration>` line.

**Beyond bare metadata, a scan also captures:**

- **Volumetrics** — `list_relations` returns `size_bytes`, `last_modified`, and row-count
  metrics (estimate-flagged) for all scannable platforms, persisted onto `:EstateDataset`.
- **Code assets** (Snowflake & Databricks) — `:EstateCodeAsset` nodes for tasks / dynamic
  tables / streams / notebooks / procedures / jobs / DLT pipelines (with a ≤4 KB definition
  preview + dependency edges), via `(:EstateScan)-[:OBSERVED_ASSET]->`. Early visibility into
  the estate's *logic*, not just its tables.
- **Enrichment** — `POST /api/estates/{id}/enrich` generates LLM table/column descriptions that
  materially sharpen the Stage-1 schema shortlist. `enrichment_summary_json.schema_description_source`
  tracks `llm` vs a deterministic `fallback`; `feasibility._resolve_schema_descs` adds a read-time
  synth so pre-enrichment scans still improve, and a `fallback`-sourced description is down-weighted
  so synthesized prose isn't treated as authoritative.
- **Column embeddings** — enrichment ALSO persists a text-similarity vector on every
  `:EstateColumn` (`embedding` + `embeddingTextHash` + `embeddingModel`), written **off the event
  loop** and **content-hash keyed** (unchanged re-enrich → skip; description edit / model swap →
  recompute). A native `estate_column_embedding` vector index (384-dim cosine) is created at enrich
  start. Feasibility then **reads** these vectors instead of recomputing them — the single largest
  chunk of the old `building_evidence` CPU cost — so a re-run over the same scan is near-free on the
  estate side. The embedded text is canonical (`schema_dna.estate_column_embed_text`), byte-identical
  to what the scorer looks up. Backfill for pre-embedding scans:
  `scripts/backfill_estate_embeddings.py`. See "Feasibility scoring" below + the freeze root-cause in
  `research/2026-08-24-background-jobs-and-async-freeze.md`.

**Scan-outcome states** are persisted per namespace (`EstateScanNamespace`) and the overall
`EstateScan.state` is one of `completed | partial | failed`. **A failed/partial scan never
produces an `absent` feasibility result** — the evaluator reads the scan state and returns
`insufficient_evidence` instead (see below).

> **One catalog per source — but many sources per estate.** Each `EstateSource` is scoped to
> a single catalog (the provider's `list_namespaces` lists schemas *within one connected
> catalog*). To cover several catalogs (e.g. Databricks `samples` + `workspace`), add one
> source per catalog to the same estate — a **feasibility run spans the latest scan of every
> enabled source**, so it assesses all of them together. A single source scanning *across*
> catalogs (one scan, many catalogs) remains explicit future work.

### 3. The evidence (deterministic) → the verdict (skill, bounded)

For each spec the backend computes evidence **deterministically**, then a skill picks the
tier — bounded so it can't over-promise. The deterministic heuristic tier is the **ceiling**;
the AI can only match it or go more conservative.

> The **plain-language version** of everything in this section — the two pools, two-stage
> matching, entity/authority-awareness, composite derivations, the grain gate, and the
> AI-as-bounded-judge model — is in **[`data-product-feasibility.md`](data-product-feasibility.md)**
> §5. This section is the engineering-grade restatement.

**Evidence** (`feasibility.py`):

- **Product vs raw, never conflated.** Published products (version-pinned) are the
  `ready`/`adaptable` pool (`enumerate_product_candidates`); estate raw datasets are the
  `assemblable` pool.
- **Tiered matching: schema shortlist → scoped column mapping.** Because a scan's schemas are
  unrelated functional areas (a `sales` and an `employees` schema may both carry a
  `product_id`), **Stage 1** (`_schema_shortlist`) matches each spec against schema-level text
  (enriched schema description + table names/descriptions + a column sample) via
  `schema_dna.text_similarity` (embed-cosine, token-Jaccard fallback). The shortlist is
  **score-driven**: a schema at/above `SCHEMA_SHORTLIST_THRESHOLD` (default **70** — the confident
  band) is shortlisted, up to `MAX_SCHEMAS_PER_SPEC` (default **5**); `SCHEMA_RELEVANCE_FLOOR`
  (default **45**) is the hard "is anything relevant" gate. **Best-one fallback:** when nothing
  reaches the threshold but a schema clears the floor, the single best floor-passer is still
  evaluated (low confidence) so a weakly-relevant estate grades instead of a false `absent`.
  **Stage 2** (`_raw_evidence`) runs column matching **only** over the in-scope datasets. When no
  schema clears the floor (and no product covers the spec), the verdict is a clean `absent` with a
  "no relevant schema" rationale (`schema_scope_empty`). Levers ride `EvaluateRequest`
  (`scope_to_schemas` / `schema_relevance_floor` / `schema_shortlist_threshold` /
  `max_schemas_per_spec`).
- **Entity/authority-aware column matching (R2, `SCORING_VERSION="r2"`).** Each attribute ×
  column cell is re-ranked `adjusted = (1 − TABLE_AFFINITY_WEIGHT)·column_semantic +
  TABLE_AFFINITY_WEIGHT·table_affinity` (`TABLE_AFFINITY_WEIGHT = 0.25`), where **table_affinity
  is per-ATTRIBUTE** (so a cross-entity attr like `transaction_date` isn't biased toward the
  product's dominant entity). An inferred **FK-carrier** column (`_infer_fk_carrier`,
  camelCase/singular-plural aware) is docked `FK_CARRIER_PENALTY = 15` when the authoritative
  table is in scope and carries a matching column — so `customer_id`→`customers.customer_id`
  beats a fact table's FK. Rows carry `column_semantic` / `table_affinity` / `adjusted` /
  `fk_role`, a stable `attr_id`, a per-attr `status ∈ {direct_match, derivable, missing,
  unknown}`, and structured `alternatives` (each with a `reason`).
- **Bipartite 1:1 assignment.** A greedy, **deterministic** (tie-broken) uniqueness assignment
  means **one column can't satisfy two requirements**. Required vs optional coverage tracked
  separately. (Optimal max-weight matching is deferred.)
- **Composite derivations as first-class candidates (R3).** `_raw_evidence` is one candidate-
  merge pass over **direct** and **composite** candidates. A composite satisfies a **non-key**
  attribute by composing ≥2 columns of the **same table** per a curated
  `feasibility_derivations.DerivationPattern` (e.g. `name = first_name + last_name`;
  `age = birth_date`). Components resolve **injectively** to distinct columns
  (`COMPONENT_MATCH_THRESHOLD = 70`, with `type_family` gates). Scoring:
  `composite_adjusted = (1 − TABLE_AFFINITY_WEIGHT)·(0.7·component_min + 0.3·component_mean)
  + TABLE_AFFINITY_WEIGHT·affinity − DERIVATION_PENALTY` (`= 6`) — **the penalty IS the
  replacement margin**, so a real direct column always beats a derived one. Composites consume
  **no** physical column (components stay reusable); they're **skipped for key/grain attrs**.
  The `data-product-derivation-advisor` skill may propose extra derivations **gaps-only**,
  validated exact-ref against the supplied inventory. Catalog loads fail-closed
  (`catalog_version` stamped; one malformed entry rejects the whole catalog).
- **Reasoning-based matching + description visibility (R4).** Two layers correct semantically-wrong
  matches the deterministic scorer alone produced (`name`→`nation.n_name`; `customer_type`→
  `c_customer_id`):
  - *Phase 1 (always on):* the spec attribute **`description` now feeds the semantic axis**
    (`_spec_features`, symmetric with the estate side); the **name-floor** is applied only when
    two names are truly equal, so the tokenizer no longer ties `n_name`/`c_name`
    (`schema_dna._s3_semantic_pair`); an **identifier-shape gate** demotes a category↔`*_id`
    mismatch to a gap unless it clears `threshold + IDENT_MISMATCH_MARGIN = 15` (a soft margin,
    `reason=identifier_mismatch`); and each assignment row carries `evidence_quality` + a
    `name_only` flag, with a run-level `description_coverage` that flags failed-enrichment schemas
    (all columns undescribed → re-enrich recommended).
  - *Phase 2 (`data-product-feasibility-column-matcher`, tool-less):* over only the **uncertain**
    attributes (`_uncertain_attrs`: gray-band / name-only / ambiguous-entity / identifier-gated /
    near-miss-gap), the skill **reasons over the estate's generated column & table descriptions**
    and returns validated `match|gap` decisions. `_validate_matches` fail-closes exact-ref against
    the shown candidates (the LLM can only pick a shown column or say gap); valid decisions are
    **pinned** and `build_spec_evidence(..., pinned_matches=)` re-runs the one greedy reconciliation
    with the pins seated first (a match consumes its column 1:1 — even one the identifier gate had
    excluded, the rescue path; a gap forces the attr unmatched). Audited in `summary_json.matcher`.
    Fully fail-safe: SDK absent / any failure leaves the deterministic result unchanged; the
    evaluator, grain gate, and heuristic ceiling are untouched.
- **Multi-dataset coverage + joinability** for `assemblable`: a joinability check (shared
  identity keys) confirms the datasets actually join, reporting join paths / disconnected
  islands.

**Tier thresholds** (`heuristic_verdict`): `ready` needs a product at `FLOOR_READY = 0.90`
required-coverage with no material derivation; `adaptable` a product at `FLOOR_ADAPTABLE =
0.60`; `assemblable` raw at `FLOOR_ASSEMBLABLE = 0.60` **and** a joinable plan; else `absent`.
A **grain-key hard gate** (`_apply_grain_gate`) then caps any buildable tier to `absent` when a
`spec.grain.keys` / required `is_key` attribute is unmatched by a **direct** column (a composite
never satisfies a key).

**Verdict** (`data-product-feasibility-evaluator` skill, tool-less): **one grounded call per
domain batch** (not per spec) over the pre-computed evidence; picks the ceiling tier *or a more
conservative one* with a better rationale. A greener-than-evidence verdict — or one missing a
real product candidate for green — is **rejected and the row falls back to the heuristic**
(`finalize_verdict`). All coverage numbers stay deterministic. If the SDK is unavailable, the
whole run uses the heuristic.

**Recommend which specs to evaluate.** `POST /api/feasibility/recommend-specs` reads the
enriched estate (`build_estate_description`) and pre-checks which specs are worth evaluating —
the `data-product-feasibility-recommender` skill with an always-available embedding-heuristic
fallback (`heuristic_recommend`), banded `strong` / `tentative` (`RECOMMEND_FLOOR = 55`).

**Evaluation state is distinct from the business tier:**

| `evaluation_state` | Meaning |
|---|---|
| `completed` | The scan was complete; the tier is trustworthy. |
| `partial` | The scan was partial; some evidence is missing. |
| `insufficient_evidence` | A gap/partial scan with no evidence — **shown instead of a bare `absent`**. |

**Live progress.** `FeasibilityRun.progress_json` advances `shortlisting → building_evidence →
(deriving) → (adjudicating) → evaluating → finalizing` (or `failed`), polled by the Feasibility
page. Every run stamps its `corpus_version` / `evaluator_version` / `skill_version` /
`embedding_model` / R2 scoring levers / pinned `scan_id`(s) for audit (`FeasibilityRun` +
`FeasibilityScore`).

### 4. Act on the verdict

| Tier | Action | Reuse |
|---|---|---|
| `ready` | Navigate to the matched product's marketplace detail (adopt/endorse). | marketplace |
| `adaptable` | Seed a `dpe-cf` `NewProductWizard` that CONSUMES the matched product. | CF wizard |
| `assemblable` | Compose a **modernization portfolio** blueprint (SA sources → aggregate/consumer) and stage it as a *proposed* `IntakeSubmission` — the PO approves it in the existing **Intake** surface, whose scaffold saga creates the projects. | `intake_scaffold` saga |
| `absent` | Log as a gap. (Backend action exists; the grid renders no button.) | — |

**Candidates + run history.** Independent of the tier action, any `FeasibilityScore` can be
**saved as a `FeasibilityCandidate`** (a PO-flagged pipeline idea + optional note, de-duped on
`(score_id, saved_by)`, `status ∈ {saved, dismissed}`) or dismissed — a lightweight backlog on
top of the verdicts. The Feasibility page also keeps a **run history** (last runs) so a prior
grid can be reopened.

## The reference specs (read live from the Blueprint Library)

The desired products a scan grades against are the **published Blueprint-Library templates**,
read **live from the graph** at runtime (`template_corpus.load_specs_from_graph` — projecting
each published `:ProductTemplate` through `feasibility_map.odcs_to_feasibility_spec` and
validating it fail-closed against `feasibility_spec.FeasibilitySpec`, skipping any single
malformed/attribute-less template rather than breaking the whole list). Each spec is enriched
beyond name/type/concept — buildability needs more:

- per-attribute **required vs optional** + **is_key** + **classification** (PII/…);
- **grain + keys**, **freshness/history** (current/snapshot/scd2);
- **allowed derivations** (rename, currency_normalize, aggregate, …);
- **composition** (join keys + composing spec ids for aggregates).

Because specs are read live, **publishing a template makes it appear in Feasibility
immediately** (and it survives a container rebuild) — there is no baked corpus to regenerate or
drift. The evaluator LLM never reads the specs directly; it grades a backend-built evidence
bundle tool-lessly, so every spec consumer is backend Python holding a DB session. The vendored
corpus at `workbench-skills/skills/data-product-feasibility-evaluator/reference/` (41 banking
specs across 20 domains) is **no longer read at runtime** — it survives only as a fresh-instance
seed (loaded into the Library at bootstrap) and a test fixture; a run still stamps
`corpus_version = "graph"` as a cheap live marker (no reproducibility snapshot is kept).

## API + MCP surface

**REST** (`routers/estates.py`, `routers/feasibility.py`):

- `POST /api/estates`, `GET /api/estates`, `GET /api/estates/{id}`, `DELETE /api/estates/{id}`
- `POST /api/estates/{id}/sources`, `GET .../sources`, `PATCH .../sources/{sid}` (name / enabled
  / **namespace_policy** — catalog is immutable once scanned), `DELETE .../sources/{sid}`
  (hard cascade), `GET .../sources/{sid}/namespaces[/{ns}/relations]`,
  `GET /api/estates/connections/{id}/catalogs` (catalog→schema enumeration for the add-source tree)
- `POST /api/estates/{id}/scans`, `GET .../scans`, `GET /api/estates/scans/{id}[/datasets|/assets]`,
  `POST /api/estates/{id}/enrich` (LLM table/column descriptions),
  `POST /api/estates/scans/{id}/profile` (engineer-gated deeper pass)
- `GET /api/feasibility/specs[/{id}]`, `POST /api/feasibility/recommend-specs`,
  `POST /api/feasibility/evaluate`, `GET /api/feasibility/runs[/{id}]`,
  `GET .../runs/{id}/scores/{spec_id}[/action]`, `POST .../runs/{id}/scores/{spec_id}/act`,
  `POST/DELETE .../runs/{id}/scores/{spec_id}/candidate` (save / dismiss a candidate)

**PO MCP** (`/po-mcp`, **+14 PO tools** for this capability): `create_estate`, `add_estate_source`,
`list_estate_catalogs`, `update_estate_source`, `remove_estate_source`, `list_estate_namespaces`,
`run_estate_scan`, `get_estate_scan`, `get_estate_scan_datasets`, `get_estate_scan_assets`,
`list_feasibility_specs`, `evaluate_feasibility`, `get_feasibility_run`, `create_product_from_spec`
— every one delegates to a router handler (zero raw Cypher).

## Security & operations

- **Async, restartable, cancellable.** Scans + evaluations are claimable, leased rows on
  `estate_worker` (mirrors `intake_worker`); a crash mid-run is reclaimed after the lease
  expires. **One active scan per source**; one active run per (scan, domain).
- **Authz.** The metadata scan is **PO self-service**; the deeper profiling pass (a live-data
  read) is **engineer-gated**. (This split is the one Phase-1 policy to confirm with the team.)
- **Sensitive data.** Columns are name-classified; the deeper profiling pass is PII-safe
  (row counts only, no top-value sampling).
- **Untrusted input.** DB object names could carry injected instructions, so the evidence
  bundle reaches the evaluator strictly as JSON data (tool-less, no plugins) — mirroring the
  intake parser discipline.

## What's deferred (honest gaps)

- **Multiple catalogs in ONE source/scan** — you can already cover many catalogs by adding
  one source per catalog (feasibility spans them all), but a single scan enumerating *across*
  catalogs — a catalog-enumeration layer above `list_namespaces` — is the explicit extension.
- **Table-level scan selection** — the scan selection is schema-grained (pick which schemas);
  ticking individual tables within a schema is the next increment (the browse endpoint that
  would feed it already exists).
- **FK-based joinability** — v1 infers joins from shared identity-key *names* (the metadata
  scan carries no FK constraints); real FK introspection is a follow-up.
- **Same-table composites only** — a composite derivation composes columns of ONE table;
  cross-table/joinable composition is deferred.
- **Name-based key authority** — FK-carrier demotion + grain-key matching reason about names,
  not profiled data; profiling-based authority + real FK edges are a follow-up. No scored
  benchmark harness yet (quality is pinned by construction + tests).
- **The `assemblable` estate→project source binding** — the composed modernization portfolio
  is staged for the existing intake saga; auto-binding the scaffolded SA projects back to the
  estate source is a documented follow-up.
- **Rationalizing existing models against each other** (three Customer 360s — agree/conflict)
  — the secondary use case, deferred (same matcher, pairwise).

## Deep-dive peers

- `feasibility_spec.py` — the `FeasibilitySpec` contract (+ retained offline/test file loader);
  `template_corpus.load_specs_from_graph` is the runtime graph-backed loader.
- `estate.py` / `estate_scan.py` / `estate_enrich.py` — graph identity + rescan diff /
  deterministic scan / LLM enrichment + deterministic fallback.
- `feasibility.py` — schema shortlist + entity-aware evidence + skill verdict + invariants +
  act-on-green.
- `feasibility_derivations.py` + `workbench-skills/skills/data-product-derivation-advisor/` —
  the curated composite-derivation catalog (R3).
- `docs/data-product-feasibility.md` — the plain-language functional companion to this doc.
- `docs/architecture/transformations.md` — the transform DSL the derivations reference.
