# Semantic layer & Semantic Q&A

> **Read on demand.** A business-concept **semantic layer** sits over the deployed data products and powers a natural-language **Semantic Q&A** chat over the marketplace. CLAUDE.md and `architecture.md` §12.4 carry the high-level shape; this doc covers the modules and the graph nodes. Open when working on `business_concepts.py`, `semantic_recommender.py`, `entity_scaffolding.py`, `embeddings.py`, `concept_svg.py`, `marketplace_chat.py`, `routers/semantic.py`, or `SemanticRecommenderPage.tsx`.

## `:BusinessConcept` graph shape (`business_concepts.py`)

A 3-level concept tree, all one label discriminated by `level`:

```
(:BusinessConcept {uri, name, definition, domain, level, value_token?, embedding, status})
  level ∈ {entity, attribute, value}
  entity -[:HAS_ATTRIBUTE]-> attribute -[:HAS_VALUE]-> value     // the concept tree
  -[:REPRESENTED_BY]-> (:DProdOutputDataset | :DProdColumn)       // polymorphic binding
  (entity)-[:RELATES_TO {kind, via_column, status}]->(entity)     // the join graph
```

- `:REPRESENTED_BY` is **polymorphic** — an entity binds to a `:DProdOutputDataset` (table), an attribute to a `:DProdColumn`. It is many-to-many; wiped + recreated from the canonical column/dataset sets on each upsert.
- `:RELATES_TO` is the entity↔entity join graph. `via_column` tells the SQL pass which column to filter when joining. **Shared reference entities** (reserved `shared` domain — e.g. `Country`) are referenced cross-domain via `:RELATES_TO`. Edges are MERGEd `pending` and promoted to `active` by the Steward.
- Concepts carry a 384-dim `embedding` (see below) and a `status` (`active` / `pending` / deprecated). Deprecating a concept strips its vector.

## Scaffolding + enrichment (`entity_scaffolding.py`)

A **deterministic, no-LLM** pass derives the concept tree from the graph: entity candidates are tables whose `relationshipKind ∈ {fact, lookup_dimension, ...}`, attributes from their `:DProdColumn`s + PK flags, relationships from each `:REFERENCES` FK whose endpoints are both entities (kind inferred from the endpoints' `relationshipKind`). It is **preview → apply** — `POST /api/semantic/entities/scaffold` returns the proposed model; `POST /api/semantic/entities/enrich` runs an LLM pass that adds business-friendly names / definitions / synonyms (including coded value labels like AU → Australia).

## Local embeddings (`embeddings.py`)

Self-hosted, CPU-only, **no external embedding API**: `fastembed` (ONNX runtime, no torch) with `BAAI/bge-small-en-v1.5` — 384-dim. Stored on `:BusinessConcept.embedding` behind a Neo4j **vector index** (declared in `business_concepts._CONSTRAINT_QUERIES`). Everything degrades gracefully — if `fastembed` isn't installed or the model download fails, embedding becomes a no-op and Semantic Q&A falls back to Full-Context retrieval. Backfill via `POST /api/semantic/concepts/backfill-embeddings`.

**Two reuse layers so a text is embedded at most once.** (1) First-class graph nodes keep their vector ON the node — `:BusinessConcept.embedding` (here) and `:EstateColumn.embedding` (Connected Estate; written at enrichment time behind the `estate_column_embedding` vector index, read back by feasibility instead of recomputed — see `connected-estate.md`). (2) Everything that isn't a node — the static feasibility spec corpus, per-run schema/affinity texts, value-resolution — is served by a **persistent content-addressed cache** transparently backing `embed_documents` (`EmbeddingCache` SQLModel, keyed `sha256(MODEL_NAME + text)`; a fully-cached batch needs no live model, and a model swap re-keys so nothing goes stale). No caller signatures change; the cache is cross-run and cross-restart.

## Endpoint surface (`routers/semantic.py`, prefix `/api/semantic`)

Concept CRUD + tree/search/diagram + relationships + the recommender + scaffolding + the discovery sequence:
`POST /concepts/recommend`, `GET /concepts/latest`, `POST /concepts/{id}/reject`, `GET /concepts/export.yaml`, `POST /concepts`, `POST /concepts/from-recommendation/{rec_uri}`, `POST /recommendations/accept-all`, `GET /concepts/tree`, `GET /relationships`, `POST /relationships`, `POST /concepts/update`, `POST /concepts/search`, `GET /concepts/diagram`, `POST /entities/{scaffold,enrich}`, `POST /concepts/backfill-embeddings`, `DELETE /concepts/{uri}`, and the **discovery** trio `GET /discovery/status`, `POST /discovery/run` (`step ∈ {scaffold,recommend,enrich}`), `POST /discovery/reset`.

`concept_svg.py` is a server-side SVG renderer for the ontology diagram (`GET /concepts/diagram`) — it replaced the client-side Mermaid `graph LR` string; `build_concept_svg(domain)` / `build_all_domains_svg()` make the layered model explicit with no SVG/layout library.

## Discovery sequence (`semantic_discovery.py` + `SemanticRecommenderPage.tsx`)

The concept layer is **built per domain** in three ordered, independently re-runnable steps, orchestrated by `semantic_discovery.run_step` (shared by the router and the MCP tools):

1. **scaffold** — `entity_scaffolding.scaffold` (deterministic, no-LLM): the entity/attribute/value spine + `:REPRESENTED_BY` bindings + FK `:RELATES_TO`. Run first; re-running deprecates the domain's concepts and rebuilds.
2. **recommend** — `semantic_recommender.run_advisor` gathers a cross-project snapshot (data products + `:DProdColumn`s + approved `:ColumnDescription`s + `:TableDescription.relationshipKind` + per-domain catalog YAML keywords + OSI evals) → the **`business-concept-advisor`** skill proposes ranked candidate concepts with column-level evidence (persisted as `:BusinessConceptRecommendation`). `semantic_discovery.auto_promote_recommendations` then promotes proposals at/above a confidence threshold (default 0.7) into real `:BusinessConcept`s — **bound to their evidence columns and parented under the entity that sits on the same dataset as those columns** (`_RESOLVE_ENTITY_VIA_EVIDENCE`), which is why recommend must run *after* scaffold. Lower-confidence proposals stay `pending` for human triage in the **Review Queue** tab.
3. **enrich** — `entity_scaffolding.enrich` LLM pass: business-friendly names / definitions / synonyms (including coded value labels like AU → Australia), then re-embed.

**Run tracking & staleness.** Each step (and `reset`) upserts a `:SemanticDiscoveryRun {uri: "discovery-run:{domain}:{step}"}` with status / last-run / stats and a **source fingerprint** (counts of products/datasets/columns/mappings + max contract version). `get_status` recomputes the fingerprint and flags a step **stale** when the sources changed since it ran, or when an upstream step (`reset`, or an earlier step) ran more recently (`upstream_rerun`). **Stranded** concepts — entity w/o a `:REPRESENTED_BY` dataset, attribute w/o a column, value w/o a `:HAS_VALUE` parent — are surfaced in the status payload, never auto-deleted (the product decision: stranded is allowed but must be visible). `reset` (`deprecate_domain_concepts`) soft-deprecates the domain for a clean rebuild; the data-product layer is read-only throughout.

**Steward UI** (`SemanticRecommenderPage.tsx`) has three tabs: **Discovery** (run the sequence — step cards with status / last-run / staleness, the **Clear concepts** action, and a stranded-concept validation panel), **Concepts** (manual add/edit/deprecate + the diagram), and **Review Queue** (triage proposals queued below the threshold — Reject / Edit / Export YAML / accept). Engineers can drive the same sequence headlessly over MCP: `get_semantic_discovery_status` / `run_semantic_discovery_step` / `reset_semantic_discovery` (domain-scoped, role-gated to Steward / Engineer / PO).

## Marketplace Semantic Q&A (`marketplace_chat.py` + `POST /api/marketplace/chat`)

> Deep dive (retrieval modes, the Explain trace, the full NL→SQL workflow, safety, token accounting, response/trace shape): **[`semantic-qa.md`](semantic-qa.md)**. The summary below is the orientation.

Two retrieval modes (`retrieval_mode`): **Full Context** (`full` — whole-domain concept dump) and **Concept-Guided** (`concept_guided` — decompose question → embedding vector-search → 1-hop `:RELATES_TO` neighbours → only the matched concepts; narrowing happens via `apply_concept_guided_retrieval`, which can fall back to Full Context). The selected context + the domain's deployed views + conversation history go to the **`marketplace-product-chat-assistant`** skill, which emits a single SELECT against the deployed views + a chat reply + 0-3 follow-ups. The SQL runs through `sql_executor`'s gated path; a two-phase synthesis produces a grounded markdown answer (+ optional chart). `marketplace_chat.answer_question` is the shared orchestration behind both the UI's `POST /api/marketplace/chat` and the MCP `query_semantic_layer` tool.

Every response echoes retrieval metadata so the UI is self-explanatory: `retrieval_mode`, `concepts_in_context` (how many concepts were actually sent to the model — symmetric across both modes), `concepts_used` (which the generated SQL referenced), `concept_fallback_reason`, and a per-question **`token_usage`** (working tokens summed across the 2–3 LLM passes via a `contextvars` accumulator — `begin_qa_usage()` + `_note_usage`; see "LLM token accounting" in CLAUDE.md). The panel renders a badge (`mode · N in scope/retrieved → M referenced · K tok`) plus an **Explain trace** (decompose → match_concepts → resolve_views → generate_sql → execute). Each answered question also writes one `:LlmUsageEvent` ledger row (`source='semantic_qa'`, tagged `domain` + `retrieval_mode`), so `/api/usage/semantic-qa` can compare Full-vs-Concept token cost in aggregate.

**Gap log** (`MarketplaceGap` model): when Semantic Q&A refuses, a "⚑ Report this gap" action logs it to a marketplace-level backlog (`/api/marketplace/gaps`, status `open → triaged → resolved/dismissed`, plus an `audience` tag of `triage`/`po`/`engineer`), surfaced in the Product Workbench **Gaps** tab.
