# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
Subsystem detail loads automatically when you read files in subdirectories: **`workbench/backend/CLAUDE.md`** (pipeline, stages, reviews, serving, MCP, auth, skills, tests) and **`workbench/frontend/CLAUDE.md`** (role access table, Apply protocol, wizard UX, engineer dashboard).

## What This Is

Data Workbench — a web-based orchestrator for data engineering pipelines. Projects are containers that hold multiple named workflows (Source Discovery, Quality Assessment, Quality Remediation, etc.). Each workflow has stages executed via the Claude Agent SDK, with real-time streaming output, Neo4j knowledge graph integration, persona-based access control, and a data marketplace for published products.

Supported archetypes (`archetypes.ARCHETYPE_REGISTRY`): `dd` (Data Discovery), `dq` (Data Quality), `dpe-sa` (Data Product Engineering — **Source-aligned**), `dpe-cf` (Data Product Engineering — **Consumer-aligned**, post source/consumer split). `dmig` (Data Migration — engineer-initiated raw platform-to-platform lift-and-shift; see `docs/data-migration.md`) is implemented. `cmig` (Code Migration — engineer-initiated legacy-code → target-platform conversion via spec-first reverse/forward engineering with curated Platform SME corpora; links to a `dmig` project for the source→target schema; see `docs/code-migration.md`) is implemented. `dmod` (Data Modernization) is the remaining placeholder with `implemented=False`.

## Reading orientation

This file is the **source of truth for evolving internals** — stage registry, role permissions, graph queries, mapping/transform semantics, Cypher gotchas. Trust it over the older files.

- **`README.md`** — feature-level framing for the two-workbench split, the wizards, the chat panels, and the marketplace. Read first for the user-facing model.
- **`docs/architecture.md`** — system diagrams (Mermaid) + stable contracts. CLAUDE.md is the canonical version of any detail; architecture.md explicitly defers.
- **`docs/engineer-guide.md`** — Data Engineer's guide to driving the Workbench from their own Claude Code over MCP (setup, the tool surface, the freeform-iterate→commit model). Pairs with the `engineer-kit/` plugin.
- **`docs/architecture/`** — per-subsystem deep dives, referenced inline in the sub-CLAUDE.md files as **load this when working on X**:
  - `ontologies.md` — every node label, relationship, property, enum value, and URI prefix across all layers (DCAT/DQV/SHACL/PROV-O/DPROD/ODCS + custom layers). Start here for the *shape* of the graph.
  - `transformations.md` — column-level `transformAuthor` model, per-kind `transformParams`, view-DDL SELECT emission, `column_transform_set` apply protocol.
  - `dataset-transform.md` — `:DatasetTransform` reserved fields, SCD lowering, suppressed columns, window emission, dialect picker.
  - `view-ddl-fk-bridges.md` — FK BFS, bridge ranker, temporal wrapping.
  - `consumer-ingest.md` — `/product/ingest` router endpoints, archetype classifier, gap analysis, shared resolve-and-bind component.
  - `marketplace.md` — `/api/marketplace` projection, lineage canvas, pinning + dprod-leak mitigation.
  - `data-product-scoring.md` — OSI scoring model.
  - `serving-materialized-dbt.md` — the dbt-materialized serving path: one-core/two-emitters, the sample→full verification gate, `MaterializationTarget`.
  - `semantic-layer.md` — `:BusinessConcept` model, the Discovery sequence (scaffold→recommend→enrich + reset), fastembed embeddings + Neo4j vector index.
  - `semantic-qa.md` — marketplace Semantic Q&A: retrieval modes, Explain trace, NL→SQL workflow, statelessness/safety.
  - `value-resolution.md` — instance value resolution (record-level lookups): probe-first/data-decides strategy, hybrid pg_trgm/rapidfuzz matcher, grounded_values injection.
  - `qa-and-reflection.md` — `:QAEvaluation` (question analyzer/executor) and `:DeploymentReflection`.
  - `transform-portability.md` — cross-platform transform portability: the neutral, `transformSchemaVersion`-stamped DSL with explicit semantics; the skill-owned, build-generated capability artifact (`data-transform-translation/` → `platform/transform_capabilities.<ver>.json`); the parse→validate→lower→render→re-validate compiler; `CompileResult`; the AST validator that fails closed on unknown/unsupported functions (transpile-success ≠ capability); the compiler's rendered SQL now also feeds the **serving-DDL emit path** for raw `expression`-kind transforms (`_translate_raw_expression`, cross-dialect only, fail-open — closes the verbatim-emit gap); the MySQL native-view reconcile.
- **`docs/userguide.md`** — end-user walkthrough for both shells.
- **`docs/data-migration.md`** — user guide for Data Migration (`dmig`): the raw lift-and-shift model, the discover→configure→assess→generate→run→reconcile flow, the downloadable/git-pushable package, `dmig` vs. the `transfer_then_transform` serving mode, and where transformation does/doesn't happen. Its transformation roadmap lives in `research/2026-08-03-migration-transformation-gap-analysis.md`. Since Part A, a reconciled migration also **materializes its target as `:Dataset:MigrationTarget` graph nodes** on the isolated `(:Project)-[:HAS_MIGRATION_TARGET]->` path (with `:MIGRATED_TO` lineage) — the anchor code migration links to.
- **`docs/code-migration.md`** — user guide for Code Migration (`cmig`): the spec-first reverse→review→forward→package flow, the two curated Platform SME corpora (source "what-to-identify" + target "patterns/anti-patterns"), the blocking spec review gate, linking to a `dmig` project, and the downloadable/git-pushable package. Sample legacy code lives in `samples/code-migration/`.
- **`docs/data-quality-testing.md`** — user guide for DQ testing: the Configure→Build→Run lifecycle, how behaviour differs by scenario (dataset vs source-aligned vs consumer-aligned = catalog vs dprod mode), the downloadable/git-pushable package, gaps, and a layman-introduced deep-dive.
- **`docs/mapping-and-transformation.md`** — strategy + per-platform reference for Mapping & Transformation: the platform-independent transform DSL authored once in the graph and compiled to per-platform SQL by the hand-written `Dialect` emitter, validated for portability by the capability artifact (`data-transform-translation` → `platform/transform_capabilities.json`) with sqlglot as the parse/render engine. Full transform-kind catalog with DSL shapes + per-kind × per-platform rendering matrices. Layman-introduced; the deep-dive peer is `architecture/transform-portability.md`.
- **`docs/inbound-intake.md`** — the inbound integration point: an external assessment tool POSTs unstructured migration/modernization recommendations to `/api/intake/submit` (scoped machine credential), an isolated LLM parser normalizes them into a strict confidence-graded blueprint, a practitioner reviews/edits, and an idempotent scaffold saga creates a `dmig` project (migration) or a `dpe-sa`+`dpe-cf` portfolio (modernization). Backend + MCP review parity implemented; review UI is the next increment. See the "Inbound intake" section in `workbench/backend/CLAUDE.md`.
- **`docs/data-product-feasibility.md`** — the **functional guide** (read-first) for Connected Estate + Data-Product Feasibility, DPO-facing: the shopping-list-vs-pantry framing, the stoplight (`ready` | `adaptable` | `assemblable` | `absent`; adaptable=blue, absent=slate), the scan→enrich→recommend→evaluate→act journey, and — the crux — **how a grade is actually decided in plain terms** (two evidence pools, two-stage schema-shortlist→column matching, R2 entity/authority-aware scoring, R3 composite derivations, the coverage thresholds + grain-key gate, the AI-as-bounded-judge ceiling model, evaluation-state vs tier, the tuning levers), how to read a result, how to act, a usage playbook, and honest gaps. Peers: `estate-discovery.md` (setup) + `connected-estate.md` (architecture).
- **`docs/estate-discovery.md`** — the **setup & connection reference** for Estate Discovery (Connected Estate): what it is (vs Pulse Discovery), the 2-level vs 3-level platform mental model, the crux **per-platform connection-setup table** (postgres/mysql/snowflake/databricks — host/port/database/credential/`extra_config`; the load-bearing rule that the catalog is set on the estate *source*, not the connection), the step-by-step (multi-catalog add-tree, live scan progress, enrich-before-grade, feasibility levers/recommend/candidates), troubleshooting ("two sources, same results"), and honest gaps. Pairs with `data-product-feasibility.md` (functional) + `connected-estate.md` (architecture).
- **`docs/offline-extraction.md`** — the **functional guide** for **Offline Extraction**: when a client won't grant DW a live connection, they run a small, vetted, dependency-light, no-LLM **extraction kit** in their own environment, produce a reviewable **DCAT-in-YAML manifest** (metadata + volumetrics + safe-by-default, PII-redacted profiling), and upload it. DW previews/validates it (no graph write), then replays it into a real `:EstateScan` **identical in shape to a live scan** — so enrichment + feasibility work unchanged. Backend + client tool + kit packaging + endpoints + PO MCP (+2) + UI implemented for **both phases**: Phase 1 = estate (replay into `:EstateScan`); Phase 2 = greenfield `dpe-sa` (the same manifest seeds a source-aligned product's `:Catalog`/`:Dataset`/`:Column` + DQV profiling via `source_manifest_seed`, gated by `data_connectivity_mode='offline'`). Peers: `connected-estate.md` (architecture) + `estate-discovery.md` (setup). See the "Offline Extraction" pieces in `workbench/backend/CLAUDE.md`.
- **`docs/connected-estate.md`** — the **architecture deep-dive** for the **top-down** Connected Estate: DW connects to a live platform *itself*, scans the estate deterministically (provider-driven, NOT the LLM discovery skill; + volumetrics/code-assets/enrichment), and grades a catalog of desired **reference data-product specs** with a stoplight. A **SEPARATE bounded context** from the Pulse-backed estate discovery (`routers/discovery.py` + `estate.yaml`) — independent `/api/estates/*` + `/api/feasibility/*` routes, `:Estate`/`:EstateScan` graph anchors, and its own PO MCP tools (+14, catalog-scoped sources + candidates); the Pulse surface is untouched. Scoring section is current to R2 (entity/authority-aware) + R3 (composite derivations). Backend + MCP + tests implemented; see the "Connected Estate" section in `workbench/backend/CLAUDE.md`. Honest about deferred gaps (multi-catalog-in-one-scan, FK joinability, estate→project binding).
- **`docs/mcp-architecture.md`** — the **canonical MCP tool reference** (142 DE tools on `/mcp` + 68 PO tools on `/po-mcp`) + the two-front-doors auth model.
- **`research/`** is scratch — dated design docs, half-finished proposals, point-in-time analyses (formerly `notes/`). Do not trust as current; verify against the code. See `research/README.md` for the index.

## Source-aligned vs Consumer-aligned (load-bearing distinction)

| | `dpe-sa` (source-aligned) | `dpe-cf` (consumer-aligned) |
|---|---|---|
| Wizard | `NewSourceProductWizard` (idea + domain + name) | `NewProductWizard` — contract-first 10-step flow (see `dpe-cf` section in backend CLAUDE.md) |
| Authoring direction | Discovery-first: engineer profiles a source DB, PO validates names/descriptions/rules | Contract-first: PO shapes the product before binding upstream sources |
| Engineer sources | Project's own raw `:Catalog` / `:Dataset` / `:Column` | `:DProdColumn` from `:CONSUMES`'d source products (cross-project URI references) |
| ODCS spec | Synthesized from approved graph state at materialization time | Authored in the wizard, persisted to graph on each save |
| `:DataContract.productKind` | `'source'` | `'aggregate'` \| `'consumer'` (late-binding intent, decoupled from archetype) |

**`productKind` is three-valued and DECOUPLED from archetype** (data-mesh trichotomy): `source` \| `aggregate` \| `consumer`. `dpe-sa` is architecturally fixed to `source`. `aggregate` and `consumer` BOTH ride the `dpe-cf` machinery (mapping, serving, `:CONSUMES` inputs, `:DProdColumn` sources) and differ only by intent — an *aggregate* is a reusable building block meant to be consumed further (it **defaults to a materialized serving recommendation** to de-risk downstream chains — a default, not a lock); a *consumer* is a fit-for-purpose leaf. The PO declares the kind as a late-binding flag in `NewProductWizard`'s Product-Details step; it's resolved in `_resolve_product_kind` (`routers/odcs.py`, honouring an explicit `spec.productKind` over the archetype default; legacy CF saves stay `consumer`). Because aggregate IS `dpe-cf`, every `archetype == 'dpe-cf'` code path already covers it.

**A consumer (`dpe-cf`) may `:CONSUMES` ANY published product — source, aggregate, or consumer — forming multi-hop DAG chains** (A→B→C). Depth is unbounded, kept safe by a hard DAG guard (`_contract_versioning.validate_consumes_bindings`): self-consumption and any binding whose target can already reach the consumer are rejected at the single atomic-save choke point (`_save_odcs_to_graph`, shared by wizard submit, ingest `from_odcs`, and MCP `save_odcs_spec`/`ingest_odcs_spec`) with a structured 409. Rollout audit: `scripts/audit_consumes_cycles.py`.

Two sanctioned cross-project graph edges exist; everything else is project-scoped via `{project_code}` URI prefixes. Both follow the same discipline (MATCH-only against an owner-tagged, deliberately-referenceable target; a typo drops the edge rather than spawning a phantom):
- **`:CONSUMES`** — `(consumer:DataContract)-[:CONSUMES]->(:DProdDataProduct)` (any published upstream product's materialised dprod node — source / aggregate / consumer).
- **`:USES_DATASET`** — `(:CodeModule)-[:USES_DATASET {variant, access, specHash, verified}]->(:Dataset)`, linking a code-migration (`cmig`) project's code module to a `dmig` project's source/target `:Dataset` nodes. Built from the **approved** reverse-engineered spec (see `docs/code-migration.md`).

Note the enforcement is a convention, not a hard lock: the chat/MCP `run_cypher.py` choke point only requires that agent-issued queries name their project code (it is edge-type-blind). Adding a third sanctioned cross-project edge is a documented decision, not a code change to the guard.

## Project-scoped graph isolation

All Neo4j operational nodes use project-scoped URIs:
```
catalog:{project_code}:{schema}
dataset:{project_code}:{schema}.{table}
column:{project_code}:{schema}.{table}.{col}
```

Product graph nodes (`:DataContract`, `:DProdDataProduct`, `:DProdOutputDataset`, `:DProdColumn`) use a `{project_code}-contract` URI prefix — globally referenceable but tagged with the owning project, so consumer products can `:CONSUMES` any published upstream product (source / aggregate / consumer) from other projects without breaking isolation. Chat agents enforce isolation at the `run_cypher.py` choke point: any query whose text and params don't both reference the project code is rejected.

All skill scripts accept `--project-code`. All prompt templates include `{project_code}`. Backend scoped queries enter through `:Project {projectCode} → :Catalog → :Dataset` (operational) or `:Project {projectCode} → :HAS_CONTRACT → :DataContract → :MATERIALISES_AS → :DProdDataProduct` (product graph). For consumer mappings the source side joins via `:CONSUMES` and the URI prefix tells you which project the consumed columns belong to.

## Architecture

**Two-process system.** Backend (`workbench/backend/`) is FastAPI + WebSocket; SQLite via SQLModel; stages run via the Claude Agent SDK (`claude_agent_sdk`); Neo4j queries for reviews/summaries/marketplace/ODCS. CORS allows `localhost:5173` and `localhost:3000`. No auth middleware — persona-based access enforced in frontend only. Frontend (`workbench/frontend/`) is a React SPA with react-router-dom v7, no UI lib (inline CSS-in-JS), state via hooks/context, axios in `api/client.ts`, custom `useWebSocket` hook.

**Two-workbench shell split.** Same backend, two persona-scoped shells: **Product Workbench** (`/product/*`, `shells/ProductShell.tsx`) for the Data Product Owner — wizards, My Products, Ingest, Marketplace, **PoValidationPage** (`/product/validate/:projectId`) for the SA validation gate; **Engineering Workbench** (`/engineer/*`) for the Data Engineer — project list, Incoming queue, Project detail. The header's **Switch** button jumps shell-to-shell directly. `theme.ts` exposes per-shell tokens; `useTheme()` writes browser tab title and dynamic favicon (P/violet vs E/blue). Both `dpe-sa` and `dpe-cf` are **filtered out** of the engineer's "New Project" archetype list (`ConfigPanel` + `PersonaDashboard`) — source/consumer products are PO-initiated only.

**Multi-workflow project model.** Projects are containers, not workflow instances. `archetypes.py` exposes `_WF_*` workflow groups. `DEFAULT_WORKFLOW_TEMPLATES` picks defaults per archetype; `_ALL_WORKFLOW_GROUPS` is the catalog. Users add workflows via `+ Add Workflow` (`POST /api/projects/{id}/workflows`). `DEPENDENCY_GRAPH` resolves cross-workflow dependencies. `Workflow.workflow_id` is the instance key within a project. "Repeatable" means the *stages inside* can be re-run. See `workbench/backend/CLAUDE.md` for workflow groups by archetype.

## Dev cheatsheet

```bash
# Backend (FastAPI + Python 3.12)
source env/bin/activate
uvicorn workbench.backend.main:app --reload --reload-exclude 'env/*' --reload-exclude 'projects/*'
# http://localhost:8000   health: GET /api/health

# Frontend (versions in package.json)
cd workbench/frontend
npm run dev          # http://localhost:5173
npm run build        # tsc -b && vite build  (NB: tsc -b has pre-existing errors; see "Working in this repo")
npm run lint
npx tsc --noEmit     # type check only — the canonical TS gate

# Full stack via Docker Compose (how the running demo env is wired; setup: docs/getting-started.md)
docker compose up -d                              # backend :8000 · frontend :5173 · neo4j bolt :7688 (browser :7475) · postgres :5433 · gitea :3101 · mysql-hr :3307 (profiled)
docker compose build backend && docker compose up -d backend   # rebuild after BACKEND code changes — source is baked via `COPY . .`, NOT bind-mounted, so edits are invisible until rebuild (same for the frontend service)
docker compose exec -T backend python scripts/<script>.py       # run a script against the container's real workbench.db (/app/data) + neo4j service
# Neo4j creds inside compose: host `neo4j`, bolt 7687, user `neo4j`, pass `workbenchpass`; from the host use bolt://localhost:7688.

# Reset / complete a stage manually
curl -X POST http://localhost:8000/api/projects/{PID}/stages/{N}/reset?workflow_id={WF}
curl -X POST http://localhost:8000/api/projects/{PID}/stages/{N}/complete?workflow_id={WF}

# Add a Python dep (canonical list: workbench/backend/requirements.txt)
env/bin/pip install <pkg>

# Tests — run from repo ROOT so workbench.backend.* imports resolve
env/bin/python -m pytest workbench/backend/tests/
env/bin/python -m pytest workbench/backend/tests/test_acceptance_gate.py::test_guard_allows_accepted -q
```

## Working in this repo

- **Verifying TS changes.** `npx tsc --noEmit` is the **canonical TS gate** and currently passes clean — keep it that way. For quick checks prefer `npx tsc --noEmit 2>&1 | grep <YourFile>` and `npx eslint <files>`. **Do NOT gate on `npm run build` / `tsc -b`** — the stricter project-references build has ~20 *pre-existing* errors in files unrelated to most changes (e.g. `MigrationSourceTargetPanel`, `ODCSEditor`, `QualityScorePanel`); the frontend Docker image deliberately builds via `npx vite build` (transpile-only, no type-check) for exactly this reason (`workbench/frontend/Dockerfile`). If you introduce a type error, fix it before finishing; don't get sidetracked fixing the unrelated pre-existing ones unless asked.
- **Skills are vendored in-repo** at `workbench-skills/skills/<skill-name>/` (the `workbench-skills/` plugin; version-controlled, baked into the backend image). Editing a skill's `SKILL.md` / `scripts/` IS legitimate Workbench work and ships with the change that needs it. EXCEPTION: the `skill-reflector` / `chat-reflector` skills produce *proposals* into `playbook/skill_reflections/` and `playbook/chat_reflections/` — those are review artifacts; never auto-apply a reflection proposal to a skill without the user's go-ahead.
- **Git / commit policy.** Commits follow short imperative titles (see `git log --oneline`); the existing trailer convention is a single `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` line. Never push with `--force` or `--no-verify` without explicit permission.
- **The `samples/` and `research/` dirs are noisy.** `samples/products_sales/README.md` and various `research/*.md` files have pre-existing modifications in the working tree. When staging a commit, prefer explicit `git add <file>` over `git add .` so unrelated drift doesn't end up in your changeset.
- **Memory.** A persistent file-based memory at `~/.claude/projects/-home-niel-working-claudecodedash/memory/` accumulates user / feedback / project / reference notes across sessions. `MEMORY.md` there is the index; keep new memories scoped to durable preferences and gotchas — not transient task state.
