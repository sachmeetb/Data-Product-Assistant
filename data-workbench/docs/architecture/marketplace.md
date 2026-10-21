# Marketplace surfaces

> **Read on demand.** The consumer-facing marketplace (`/marketplace`) and its detail view. Open when working on `routers/marketplace.py`, `pages/MarketplacePage.tsx`, `components/MappingGraphView.tsx`, or any product-detail rendering. Load-bearing pinning gotchas live here — getting them wrong silently flips the listing to a draft.

## Endpoints

The router (`routers/marketplace.py`) is wider than just list + detail. Current surface:

- **`GET /api/marketplace`** (`ALL_PRODUCTS`) — product list; returns `product_kind` (`'source'` | `'consumer'`), `has_inflight_edit`, OSI band, column count.
- **`GET /api/marketplace/detail`** (`PRODUCT_DETAIL`) — per-product detail incl. `consumes[]` / `consumed_by[]`, owners/stewards/team/roles, latest OSI + QA evaluation.
- **`POST /api/marketplace/products/{cid}/preview`** — sample rows from the deployed views (Phase-1 preview path).
- **`POST /api/marketplace/products/{cid}/qa/execute`** — run a curated QA question's SQL against the deployed views (see [`qa-and-reflection.md`](qa-and-reflection.md)).
- **`POST /api/marketplace/chat`** — Semantic Q&A (NL → SQL → grounded answer) via `marketplace_chat.answer_question` (see [`semantic-layer.md`](semantic-layer.md)).
- **`POST /api/marketplace/qa/probe`** — classify a free-form consumer question as answerable / partial / no.
- **`POST /api/marketplace/gaps`** / **`GET /api/marketplace/gaps`** / **`POST /api/marketplace/gaps/{id}/resolve`** — `MarketplaceGap` backlog (Semantic-Q&A refusals).
- **`GET /api/marketplace/products/{cid}/reflection/latest`** — latest deployment-reflection report.
- **`GET /api/marketplace/{cid}/revisions`** — contract version history.
- **`GET /api/marketplace/lineage`** / **`GET /api/marketplace/mapping-graph`** — lineage + mapping-graph projections for `MappingGraphView`.
- **`GET /api/marketplace/product-lineage`** — coarse product-to-product `:CONSUMES` DAG for `ProductLineageView` (the **Lineage** sub-tab).
- **`GET /api/marketplace/flow`** (`?domain=`) — the layered value-flow ("Sankey view" sub-tab) payload; see below.
- **`GET /api/marketplace/{cid}/report`** — markdown product report.
- **`GET /api/marketplace/{cid}/odcs.yaml`** — ODCS export.
- **`GET /api/marketplace/okf-bundle`** + **`GET /api/marketplace/products/{cid}/okf-bundle`** (`routers/okf.py`, `?format=zip|json`) — Open Knowledge Format export (see below).

## OKF export (Open Knowledge Format)

`okf_export.py:collect_okf_files(settings, uri=None)` projects published products into an OKF v0.1 bundle — markdown docs with YAML frontmatter, cross-linked by `:CONSUMES`. It's a deterministic, **lossy-by-design** *briefing* for an external AI agent that can't reach our MCP/graph — complements ODCS (the contract) and the dbt project (runnable); never a default pipeline stage.

- **Two modes, one core.** `uri` given → single-product bundle (`product.md` + `datasets/*` + `quality.md` + optional `reflection.md`). `uri` omitted → whole-marketplace bundle (`catalog.md` + `products/<code>/…`), with `:CONSUMES` resolved to bundle-relative cross-links so products built on each other are navigable. The single-product bundle is the same assembly scoped to one product (neighbors fall back to prose URIs).
- **Reuses the marketplace projection.** Fetches via `marketplace.PRODUCT_DETAIL` + `_shape_detail`, so OKF tracks the exact shape the marketplace renders — no parallel query to drift.
- **Mirrors the listing's product set.** Marketplace mode applies **no** status filter (same as `GET /api/marketplace`): both source- AND consumer-aligned products are included, even consumer products whose `dp.status` is still `'draft'` while their lifecycle is `approved` / `in_engineering`. `ALL_PRODUCTS` already version-pins to the latest published/superseded contract version, so no in-flight draft schema leaks — status was never the right gate. `catalog.md` tags each non-published product with its lifecycle state.
- **Untyped-link mitigation.** OKF links are untyped ("relationship conveyed by prose"); we state each relationship in prose AND stash the edge type in a custom `consumes:` / `relationship:` frontmatter key (consumers tolerate unknown keys). Reserved filenames (`index.md` / `log.md`) are deliberately avoided → trivial v0.1 conformance (every doc carries a non-empty `type`).
- **Surfaces.** `MarketplacePage.tsx` renders **"Export Open Knowledge Format"** twice (per-product in the detail header; whole-marketplace in the listing header), sharing a `downloadZip` helper that preserves the server's `Content-Disposition` filename. The MCP `get_okf_bundle(contract_id)` tool is the single-product path for engineers (see [`../mcp-architecture.md`](../mcp-architecture.md)).

## UI surfaces

- **`ProductKindChip`** (`components/ProductKindChip.tsx`) — shared 3-way badge (blue source / purple aggregate / orange consumer); the single label authority (a `kind → {bg,fg,border,label,title}` map). Used in marketplace list cards + detail header + dashboard Inputs detail + product home tiles + the wizard upstream picker.
- **Filter chips** (All / Source / Aggregate / Consumer) above the marketplace product list.
- **Reclassify nudge** — a soft banner in the product detail when a `consumer` product's `consumed_by` is non-empty (other products build on it → consider reclassifying as Aggregate). No auto-change; the PO reclassifies via the wizard's Product-Details intent step.
- **Cross-references**: `PRODUCT_DETAIL` returns `consumes[]` and `consumed_by[]` via `:CONSUMES` traversals. Overview tab renders click-through buttons that pivot via `selectProduct`.
- **Score OSI now** button next to OSI stat (PO + owner-only). POSTs `/api/projects/{id}/osi/evaluate?trigger=manual&skip_advisor=true`.
- **Lineage canvas** (`MappingGraphView.tsx`) groups product columns by `dataset_name` — SA products with multiple datasets render as N right-side boxes, not one collapsed product. `colUriToNodeId` map routes mapping edges to the correct dataset node. Backend queries (`MARKETPLACE_MAPPING_GRAPH_QUERY`, `_GRAPH_MAPPINGS_RETURN`, `GRAPH_PRODUCT_COLUMNS_QUERY[_S]`) RETURN `product_dataset_name`.
- **Lookup sources in lineage.** A `lookup`-kind mapping reads from a reference table — often a **different** CONSUMES'd product than the mapping's primary `:MAPS_SOURCE_COLUMN` source. Those reads are edged as `:LOOKUP_VIA` (see `docs/architecture/transformations.md`). `MARKETPLACE_LOOKUP_GRAPH_QUERY` + `LINEAGE_LOOKUP_QUERY` surface them: the lookup table shows in the **Source Tables** list with a "lookup" tag (and a lookup-column count instead of an X/Y mapped ratio), and on the canvas as a left-side source node with a dashed, "lookup"-badged edge. `lookup_via.merge_lookup_graph_rows` is the shared fold used by both the marketplace and engineer (`reviews.py`) graph handlers. Without this, the Source Tables list and canvas showed only the primary source product — the original "Customer 360 only shows Customer Master" gap.

## Layered value-flow ("Sankey view") — `GET /api/marketplace/flow`

An **additive** left→right layered flow across **five fixed columns** — Source Schemas → Source-Aligned → Aggregate/Derived → Consumer-Aligned → Use Cases — that always shows the full 5-column story, filling real columns from what exists today and rendering ghost placeholders where it doesn't (greenfield-friendly). The existing lineage views are untouched. Deliberately **not** d3-sankey: it's a uniform-box node-link diagram with empty/ghost columns (a fixed-column React Flow layout, `components/flow-sankey/`).

- **Contract.** One normalized `FlowPayload` — ALWAYS 5 columns in fixed order (`source_system`, `source_aligned`, `aggregate`, `consumer`, `use_case`) + `links` (`kinds: consumes | source_binding | supports | feasibility_match`) — assembled by the **pure, DB-free** `backend/flow_payload.py` (`FlowBuilder` + `bucket_product_kind`). An empty column carries `nodes: []`; the frontend synthesizes one ghost node so the column still occupies space. The SAME payload/component serves the Estate/Feasibility mount (fast-follow); the `source_system` column's KEY is stable but its human **label is context-overridable** ("Source Schemas" in the marketplace, "Source Systems" in estate).
- **Middle three columns + `consumes` links.** A row-level-lifecycle-guarded variant of the product-lineage DAG. `FLOW_PRODUCT_NODES` = `PRODUCT_LINEAGE_NODES` **plus a required `WHERE cv.lifecycleState IN ['published','superseded']`** — kept as a separate constant so the legacy `/product-lineage` endpoint is byte-unchanged. (Without the added guard, `PRODUCT_LINEAGE_NODES` leaks never-published drafts: its lifecycle filter lives inside an OPTIONAL MATCH and falls back to `dc.currentVersion`, which can be a draft.) `PRODUCT_LINEAGE_EDGES` is reused verbatim. Each node is bucketed by `productKind` (`bucket_product_kind`; blank/unknown → `source_aligned`).
- **Source Schemas column + `source_binding` links.** `FLOW_SOURCE_SYSTEMS` — a marketplace-wide generalization of `LINEAGE_QUERY`'s **catalog arm**: per published/superseded source-aligned product, walk `:DProdColumn ←:MAPS_TO_PRODUCT_COLUMN- (:ColumnMapping {isCurrent}) -:MAPS_SOURCE_COLUMN→ :Column ←:HAS_COLUMN- :Dataset ←:DCAT_DATASET- :Catalog` and group by the **project-scoped `:Catalog` node** (URI `catalog:{project_code}:{schema}`), NOT the bare schema name — so a Postgres `public` shows once **per project** ("sales · public") instead of collapsing into one global node. Each catalog → one `source_system` node (`meta.synthesized=true`, `project_code`, `schema`, `domain`) + a `source_binding` link into the product it feeds (`weight` = # source columns).
- **Use Cases column** is a **pure placeholder** in v1: the live `_generate_dprod` path never writes `keyUseCases`, so a read would always be empty. No use-case nodes/links are emitted; the frontend renders one ghost "use cases coming soon" node. Making it real needs the deferred LLM synthesis OR persisting use-cases on `_generate_dprod`.
- **Scope + counts.** Optional Python `?domain=` filter over products (the builder then drops any link whose endpoint was filtered out — the same defensive dangling-link drop as `get_product_lineage`). `count`: a source node = # distinct source-aligned products it feeds; a product node = # direct downstream `:CONSUMES` consumers.
- **Honest gaps.** A marketplace "source system" is a **synthesized** catalog/schema grouping, not a real business system (real named systems are the Estate view's job). A source-aligned product with no `isCurrent` `:ColumnMapping` contributes no source node (mapping lifecycle is independent of the contract; same caveat as `LINEAGE_QUERY`).
- **Frontend.** `components/flow-sankey/` — `FlowSankeyView.tsx` (presentational; module-scope `NODE_TYPES`, `NodeProps<Node<FlowNodeData>>`, `links-on-select` mode default-ON above a hairball threshold, select-to-dim highlight via `focusSetFor`, reuse-heat toggle scoped to product columns via `computeFanOut`, `×N` count badges, status dot **and** label for a11y) + `FlowSankeyCatalogTab.tsx` (self-fetch) + `flowSankeyLayout.ts` (fixed-column layout, reuses `estateLayout.traverse`) + `flowSankeyTypes.ts` + `flowStatusStyles.ts`. Mounted as the **Sankey view** sub-tab in `MarketplacePage.tsx`; "Open ↗" deep-links a product node to its detail. Tests: `backend/tests/test_flow_views.py`.

## Pinning (critical)

The pinning model migrated to the versioned **`:ContractVersion`** node — the old `lifecycleVersion` property + `isCurrent` flag on `:DataContract` are no longer the pin key. `ALL_PRODUCTS` and `PRODUCT_DETAIL` (`routers/marketplace.py`) now pin to the latest **deployed** version:

```cypher
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv_pub:ContractVersion)
WHERE cv_pub.lifecycleState IN ['published','superseded']
WITH dp, dc, cv_pub ORDER BY cv_pub.version DESC
WITH dp, dc, head(collect(cv_pub)) AS cv_deployed
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv_cur:ContractVersion {version: dc.currentVersion})
WITH dp, dc, coalesce(cv_deployed, cv_cur) AS cv, cv_deployed, cv_cur
```

i.e. pick the highest-version published/superseded `:ContractVersion` (`head(collect(cv_pub)) ORDER BY cv_pub.version DESC`), and fall back to `coalesce(cv_deployed, cv_cur)` — the version keyed on `dc.currentVersion` — only when nothing was ever deployed (so a PO's draft-only product still shows in their My Products list). A `has_inflight_edit` flag fires when a deployed cv exists AND the current cv differs and sits in a drafty state. All substructure walks filter their temporal edges by `cv.version`. Without this pin, opening an edit on a deployed product would silently flip the marketplace listing to render the in-flight draft.

## dprod-leak mitigation

`:DProdColumn` / `:DProdOutputDataset` are NOT versioned alongside `:DataContract`. `_generate_dprod` wipes and rebuilds them from the current head. To prevent column data from flipping mid-edit, `NewProductWizard.ensureProjectAndSaveSpec` skips the `/generate-dprod` call when the hydrated `lifecycle_state` is `'published'` **OR** `'approved'` (`branchingState` guard, `NewProductWizard.tsx` ~1913) — for `published` it would flip the marketplace listing to the draft's columns despite the pinned metadata; for `approved` it would orphan the engineer's existing `:ColumnMapping` nodes whose target URIs reference the about-to-be-deleted `:DProdColumn`s. Either way dprod regeneration is deferred to the engineer's `odcs_to_dprod` stage.
