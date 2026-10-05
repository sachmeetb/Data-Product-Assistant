# Consumer-aligned ODCS ingest

> **Read on demand.** Parallel entry point to `NewProductWizard` — PO registers an existing ODCS spec (source-aligned OR consumer-aligned) at `/product/ingest`. The high-level flow lives in `CLAUDE.md`'s consumer-aligned section; this doc covers the router endpoints, shared component, and skill fallbacks. Open when working on `routers/ingest_products.py`, `pages/product/ingest/*`, or the wizard's source-resolution steps (3 + 8).

Router prefix: `/api/ingest-products/`. Companion page: `pages/product/IngestExistingProductPage.tsx`.

## Endpoints

- **`POST /parse-odcs`** — deterministic YAML/JSON parse + `_canonicalize_v3_1`. No side effects.
- **`POST /classify-archetype`** — invokes the `data-product-archetype-classifier` skill (**in-repo vendored** at `workbench-skills/skills/`, programmatic-only) to detect source vs consumer with rationale + signals + `inferred_dependencies`. Heuristic fallback when the skill SDK call returns nothing.
- **`POST /match-inputs`** — for consumer specs, returns per-slot marketplace candidates ranked by exact-URI → exact-name → semantic match. The candidate pool is EVERY published product — **source-aligned, aggregate, OR consumer-aligned** (a consumer may build on any of them; multi-hop chains are supported) — explicitly filtered to `lifecycle_state ∈ {published, superseded}` and `product_kind ∈ {source, aggregate, consumer}` (excludes draft-only + empty-kind rows), and excluding the consumer's own contract. Each candidate carries its `product_kind`. Semantic tier uses the same classifier skill in `MODE: match_inputs`; falls back to `_heuristic_rank_marketplace` (Jaccard-style keyword overlap + domain-match boost) when the skill returns nothing. **Domain-matched candidates preselect at score ≥ 65** (vs **75** for cross-domain — `threshold = 65 if rank_entry.get("domain_matched") else 75`) because the wizard already filters the pool by domain. Synthesises a "discovery slot" from the spec when nothing was declared.
- **`POST /drafts`** (upsert) / **`GET /drafts`** (list) / **`GET /drafts/{id}`** / **`PATCH /drafts/{id}`** — in-flight ingest state survives the PO stepping away to author missing source products. Drafts auto-save on every step transition + slot change; PATCH stamps `spawned_request_id` / `spawned_project_id` per slot when `NewSourceProductWizard` is launched from a "Create now" gap.
- **`POST /from-odcs`** — commit. Accepts `archetype_override` (`dpe-sa` | `dpe-cf`), `input_selections[]`, `ingest_draft_id`. For `dpe-cf`: scaffolds `DPE_CF_WORKFLOW_TEMPLATES`, MERGEs `:CONSUMES` edges from matched selections via `sync_consumes_edges`, does NOT auto-publish (lands at `lifecycleState='submitted'`), refuses commit if any slot is unresolved.
- **`POST /projects/{id}/gap-analysis`** — pre-flight gap check (mounted at the Confirm step, wizard step 9). Invokes the **in-repo vendored** `data-product-gap-analyzer` skill (`workbench-skills/skills/`); heuristic fallback compares consumer columns to candidate-source columns by token overlap, returning per-column status (`covered` | `derivable` | `ambiguous` | `gap`). Generic suffix tokens (`id`, `key`, `code`, `name`, …) are stop-listed in `_meaningful()` so `customer_id` doesn't false-positive against `nasa_sat_id`.

## Shared component

`pages/product/shared/ResolveAndBindSourcesStep.tsx` is mounted by both `NewProductWizard` (steps 3 + 8) and `IngestExistingProductPage`. State lives in the host (`wizardSlots` / `slots`); the component renders per-slot status chips, candidate dropdowns, "Mark as gap" + "Create now" gap-fulfillment buttons. Manual slots (PO picked from marketplace) hide the dropdown and show an inline Remove link via the optional `onRemove` prop.

`pages/product/ingest/ArchetypeClassificationBanner.tsx` shows the classifier's recommendation + override toggle on the ingest Confirm step. `pages/product/shared/GapAnalysisSection.tsx` is the step-8 manual-fire pre-flight check.

## Pacing UX

The ingest classifier + matcher runs synchronously before the user transitions to the next step — the host shows a spinner panel matching the existing `NewProductWizard` re-rank pattern. No mid-screen pop-in.

## Skill fallback contract

The classifier and gap-analyzer skills are **vendored in-repo** at `workbench-skills/skills/` (version-controlled, baked into the backend image) — they are programmatic-only (invoked from the FastAPI endpoints, not the pipeline), not something a deployer installs separately. Every endpoint that calls one still has a deterministic heuristic fallback so the system stays usable if the SDK call fails or returns nothing; the skill grounds richer narrative + better synonym handling. SDK invocation template lives in `_run_classifier_skill` in `routers/ingest_products.py`.
