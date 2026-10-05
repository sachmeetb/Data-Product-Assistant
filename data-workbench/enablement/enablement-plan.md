# Data Workbench — Innovation-Team Enablement Plan

**Status:** Phase 1 (this plan) — approved and checked in.
**Facts verified:** 2026-08-25 against the working tree — see
[`ground-truth-facts.md`](./ground-truth-facts.md).
**This is the plan only.** It is the input to a later **content-authoring phase** (per-slide
markdown + screenshot-slot specs), which is the input to a **final deck-production phase**.
**We are not producing decks now.**

---

## Context

The Innovation team needs to ramp up on **Data Workbench (DW)** quickly. This is a **new,
hands-on enablement track**, distinct in purpose and tone from the existing
**stage-zero / solutioning / positioning** material (which is outward, client-persuasion
content — treat it as *reference*, never as a template). The goal of this track is a
practitioner who can **stand the product up locally, build data products end-to-end, drive
it over MCP, and understand what's happening under the hood** — not a slide audience being
pitched to.

**Decisions already made (confirmed with the requester):**

- **Packaging:** ~10 focused, single-objective decks in a teachable sequence
  (plus a Module 0 orientation → ~11 decks total).
- **Audience baseline:** comfortable with Docker/CLI, **new to WSL** → keep Linux basics
  light; the WSL primer focuses on WSL2 specifics.
- **Canonical "latest version" location:** *to be decided later* — tracked as an open
  decision below; a placeholder slide/section carries it in every deck.
- **Deck-production tool:** *to be decided later* → author content **tool-agnostic**
  (markdown + explicit screenshot slots) so acnpptx / python-pptx / other is a late bind.

---

## Content strategy (meta)

- **~11 decks** (a Module 0 orientation + ten content decks), **≈130–170 slides total**,
  **3 hands-on projects** (Flagship greenfield, MCP scenario, Bring-Your-Own-Data
  extension) plus a guided setup lab.
- **Every hands-on module uses one Activity Template** (see
  [`activity-template.md`](./activity-template.md)), applied consistently:
  **Objectives → Prerequisites/starting point → Steps (screenshot-driven) → Outcome
  ("done" looks like) → Validation/confirmation → Next steps.** Concept "interludes" are
  woven inline *and* collected in the dedicated concepts deck (Module 5).
- **Tone contract:** procedural + explanatory ramp-up, first-person-doer voice. Explicitly
  *not* the maturity-matrix / "menu not monolith" positioning voice of the solutioning
  decks.
- **Content-first workflow (the three phases):**
  1. **This plan** (module map + scope) — *now*.
  2. **Per-slide content** authored as markdown per module: slide titles, bullet content,
     concept callouts, and **`[SCREENSHOT: …]` placeholder slots** with a caption + what
     the shot must show. Reviewed until each deck's story + screenshot list is agreed.
  3. **Deck generation** from the approved markdown (tool TBD).
- **Scope exclusion (hard):** the **semantic layer** (Steward Concepts → Discovery
  sequence, `:BusinessConcept`s) is **out of this initial track** — it becomes its own
  future chapter. Consequence to bake in: the domain-scoped **Semantic Q&A chat** only
  runs in "Full Context" mode without it, so **validation uses the self-contained
  per-product Q&A tab**, not the semantic chat (see Module 4).
- **Placeholders** for not-yet-ready topics are explicitly marked (Module 10) — proposing a
  future topic without building it is fine.

---

## Module map (the ~10-deck sequence)

Three arcs: **Set up (Module 0–Module 2) → Do (Module 3–Module 4) → Understand & Extend (Module 5–Module 10).**

| # | Title | Type | ~Slides | Prereq |
|---|---|---|---|---|
| Module 0 | Track Orientation & the Two-Persona Model | concepts | 8–12 | none |
| Module 1 | Windows → WSL2 Environment Prep | hands-on setup | 10–14 | Module 0 |
| Module 2 | Install, Configure & Launch Locally | hands-on setup | 14–18 | Module 1 |
| Module 3 | Flagship Pt 1: Build Two Source-Aligned Products | hands-on project | 18–24 | Module 2 |
| Module 4 | Flagship Pt 2: Build an Aggregate & See Lineage | hands-on project | 16–20 | Module 3 |
| Module 5 | Under the Hood: Skills, Knowledge Graph, Transformations | concepts | 14–18 | Module 3–Module 4 |
| Module 6 | Driving DW over MCP | hands-on project | 12–16 | Module 2 |
| Module 7 | Data Quality & Pipelines: Independently-Executable Outputs | concepts + light hands-on | 12–16 | Module 4/Module 5 |
| Module 8 | Advisory Services for DE & PO | concepts + demo | 8–12 | Module 5 |
| Module 9 | Self-Directed Extension: BYO Data → Consumer-Aligned Product | hands-on project | 10–14 | Module 3–Module 5 |
| Module 10 | Feedback Loop, Roadmap & Placeholders | meta | 6–8 | none |

---

### Module 0 — Track Orientation & the Two-Persona Model  *(concepts, no hands-on · ~8–12 slides)*

- **Objective:** learner understands what DW is, the **Product Workbench (PO)** vs
  **Engineering Workbench (DE)** split, the value narrative, how this track is sequenced,
  and where to get the latest version.
- **Scope:** what DW is; the two shells + **Switch** button + role selector; the
  data-product mesh vocabulary (source-aligned / aggregate / consumer); how to use this
  track; **canonical-latest-version location (placeholder — TBD)**; one slide positioning
  this track vs the solutioning decks.
- **Prereq:** none. **Outcome:** learner can navigate the two shells conceptually and
  knows the road ahead. **Validation:** short orientation quiz / self-check.

### Module 1 — Windows → WSL2 Environment Prep  *(hands-on setup · ~10–14 slides)*

- **Objective:** a Windows learner has a working **WSL2 Ubuntu** environment with Docker,
  and the project code inside it.
- **Scope (audience = comfortable CLI, new to WSL → WSL2-specific):** *no supported path
  outside WSL* on Windows; what WSL2 is (1 slide); install/start WSL + Ubuntu image;
  **install Docker Engine in Ubuntu** (convenience script + docker-group post-install +
  starting the daemon each session); `host.docker.internal` on WSL; getting the code in
  — **two paths: read-only repo clone** or **zip transfer** — with WSL file-system mechanics
  (`\\wsl$`, keep the repo on the **Linux** fs not `/mnt/c` for perf); practical
  smooth-running tips.
- **Prereq:** Module 0. **Outcome:** `wsl -l -v` shows a running Ubuntu; `docker info` works
  inside it; repo present on the Linux fs. **Validation:** `docker info` succeeds; repo
  `ls` from inside WSL. **Net-new content** (nothing in repo — author fresh).

### Module 2 — Install, Configure & Launch Locally  *(hands-on setup, screenshot-driven · ~14–18 slides)*

- **Objective:** learner has DW running locally with the **Postgres samples** and a working
  LLM key, verified healthy.
- **Scope:** local = one of several deployment archetypes but the **easiest for
  enablement**; **machine requirements (author fresh: 32 GB RAM min, disk, why — many
  containers + JVM Neo4j + embedding model + a Claude subprocess per stage)**; the container
  run-model + the **`dwb` launcher**; the **services + ports** table; the **recommended
  default** = Postgres samples (`./dwb doctor` → `./dwb up --with postgres`); **LLM access** =
  Azure AI Foundry key (`ANTHROPIC_FOUNDRY_API_KEY` in a gitignored `.env`; direct-Anthropic
  fallback; or `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` for a workstation that
  already has a Claude Code subscription — personal credential, laptop only), the **team
  provisions the key**, plus the **30-day token rotation/expiry
  governance policy (author fresh)** + **secret-hygiene** ("never screenshot `.env`");
  health/status/logs/reset.
- **Prereq:** Module 1. **Outcome:** `curl :8000/api/health` OK; UI at `:5173`; sample DBs show as
  **Quick connect**. **Validation:** `./dwb status` green; open UI; Neo4j browser at `:7475`.
  **Next:** Module 3.
- **Facts to lock (from [`ground-truth-facts.md`](./ground-truth-facts.md)):** frontend
  5173, backend 8000, Neo4j 7475 (browser) / 7688 (bolt), postgres 5433, mysql 3307, gitea
  3101 (on by default), seaweedfs 9000/8888/9333 (opt-in, **off** — object storage is
  *optional* for this track). Recommend **Postgres-only** default; note MySQL/Databricks are
  alternate/optional sources.

### Module 3 — Flagship Pt 1: Build **Two Source-Aligned Products**  *(hands-on project, screenshot-driven · ~18–24 slides)*

- **Objective:** discover + materialize **two** source-aligned data products from the **HR
  sample** (`hr_core`→`workforce_core`, `hr_comp`→`compensation_payroll`).
- **Scope / steps (per product, persona-switch-aware):**
  **PO** — `NewSourceProductWizard` (3-step idea+domain+name). **Switch → DE** — Accept the
  request (hard gate) → Select Data Source (Quick-connect Postgres) → Data Discovery → Data
  Profiling → Metadata Enrichment → Column-Name Standardization → Mark Discovery Complete.
  **Switch → PO** — clear the **5-tab validation gate** (Names / Descriptions / Tables /
  Relationships / Rules). **Switch → DE** — Synthesize Contract → Import → Auto-Map → Serve
  (virtual view) → **Deploy** → Mark Engineering Complete.
- **Persona-switching is a deliberate exercise beat** (flag the friction; it reinforces the
  persona model). **Mitigation for time-boxed sessions (call out):** the full flagship is
  ~8–10 switches — optionally pre-seed the first source product (or script it via MCP, Module 6)
  so learners build only the second source + the aggregate.
- **Prereq:** Module 2. **Outcome:** two **Source** (blue-chip) products live in the Marketplace.
  **Validation:** Marketplace **All/Source** filter shows both; each product's **Preview**
  tab returns real rows; **Serving** tab shows the deployed view.
- **Teaching flags:** sample READMEs' "manual `psql` deploy" note is **stale** — teach
  **Configure → Build → Deploy**. Databricks is an *optional* discovery source (free personal
  account) — keep it an optional sidebar, Postgres is the spine.

### Module 4 — Flagship Pt 2: Build an **Aggregate** & See **Lineage**  *(hands-on project, screenshot-driven · ~16–20 slides)*

- **Objective:** create one **aggregate** product (`monthly_headcount_cost`) that
  `:CONSUMES` both source products, deploy it, and read the resulting lineage.
- **Scope / steps:** **PO** — `NewProductWizard` (10-step contract-first): Describe/Domain →
  Shape → *(opt)* Suggest sources → Shape the Schema (grouping keys) → **Product Details =
  declare `productKind = aggregate`** (auto-suggested when ≥2 sources; note aggregates
  *default* to materialized serving) → Ops → Rule Coach → Readiness → **Confirm sources
  (required `:CONSUMES` gate + pre-flight gap check)** → Submitted. **Switch → DE** — Accept →
  Import → **Mapping & Transformation** (`--source-mode dprod`; Approve/Replace/Escalate) →
  **Configure Serving (materialized recommended) → Build → Deploy** → Complete.
- **Teach the two-source join gotcha as a feature:** `:REFERENCES` never crosses product
  boundaries, so first serve of a two-source aggregate hits `ViewGenerationError: no FK path`
  → apply the **join-preflight** bridge or author `:DatasetTransform.joins[]`. This is
  expected and is a highlight, not a bug.
- **Lineage viewing (Marketplace product detail):** **Lineage tab** (column-level canvas),
  **Lineage sub-tab** (product-to-product `:CONSUMES` DAG), **Sankey view** (5-column value
  flow), and **consumes/consumed_by** cross-refs on Overview.
- **Validation (semantic-layer-free):** aggregate shows **Aggregate** (purple) chip;
  Overview `consumes=[A,B]` and each source `consumed_by=[aggregate]`; Preview returns rows;
  **per-product Q&A tab** answers curated questions + a free-form probe. *(Do not use the
  domain Semantic Q&A chat — it degrades to Full-Context-only without the excluded semantic
  layer.)*
- **Prereq:** Module 3. **Outcome:** a 3-product chain with visible lineage. **Next:** Module 5.

### Module 5 — Under the Hood: Skills, the Knowledge Graph, Transformations  *(concepts · ~14–18 slides)*

- **Objective:** learner can explain *how* the flagship worked. Referenced inline from
  Module 3/Module 4 and collected here.
- **Scope:**
  - **(a) Agent skills** — `SKILL.md` + `scripts/`, `--project-code` isolation, vendored
    in-repo (**69 skills** as of 2026-08-25 — re-count at authoring time, see
    [`ground-truth-facts.md`](./ground-truth-facts.md)), three run-shapes (pipeline stage /
    advisory-programmatic / chat), invoked as stages vs behind endpoints vs in chat.
  - **(b) the Neo4j knowledge graph** — project-scoped URIs, the six ontology layers
    (DCAT-2 / DQV / SHACL / PROV-O / DPROD / ODCS), how
    discovery→profiling→enrichment→rules→ODCS→DPROD→mapping→serving accretes, the two
    sanctioned cross-project edges (`:CONSUMES`, `:USES_DATASET`), where it's stored (Neo4j
    at `:7475`/`:7688`).
  - **(c) transformations/mappings** — AI drafts a mapping by semantic similarity, the
    **four `transformAuthor` sources + priority cascade** (po_hint → steward_catalog →
    engineer → ai_suggestion), engineer **Approve/Replace/Escalate**, and the
    **platform-independent DSL → target SQL** via the **Dialect emitter** ("one recipe,
    different stove") with fail-closed capability validation.
- **Prereq:** Module 3–Module 4 (concrete referent). **Validation:** learner traces one column's lineage
  in the graph via the DE **Ask** panel.

### Module 6 — Driving DW over MCP  *(hands-on project · ~12–16 slides)*

- **Objective:** learner drives DW headlessly from their own Claude Code (or Codex/Cursor)
  over MCP.
- **Scope:** the **two front doors** — `/mcp` (**141** DE tools) and `/po-mcp` (**57** PO
  tools); the **token model** (`WB_MCP_TOKENS` = `token[:principal[:projects[:role]]]`);
  **one-liner installers** + `WORKBENCH_MCP_URL`/`WORKBENCH_TOKEN`; the **`/api/bootstrap`
  paste-once onboarding** prompt; example prompts ("list my projects", "run discovery on X",
  poll `get_project_state`). **Honest orchestration framing:** there is **no pub/sub** in the
  product — the real primitives are **async fire-and-forget `run_stage` + poll**, the
  **SQL-leased intake/estate/feasibility workers**, and the **`/api/intake/submit` REST
  ingress**; "queue work from pub/sub, drive from separate Claude sessions" is presented as
  **illustrative/future**, wired on top of those real primitives (multiple sessions can poll
  the same async run).
- **Prereq:** Module 2 (running stack + token). **Outcome:** learner lists projects + kicks off a
  stage from their own CLI. **Validation:** a stage started via MCP shows `running` →
  `complete` in the UI. Use the correct current tool counts (**141 / 57**) — older decks say
  136/38, which are **stale**.

### Module 7 — Data Quality & Pipelines: Independently-Executable Outputs  *(concepts + light hands-on · ~12–16 slides)*

- **Objective:** learner understands the DQ narrative and that generated pipelines have **no
  DW runtime dependency**.
- **Scope:** DQ lifecycle **Configure → Build (self-contained package, no live DB needed to
  build) → Run → Failure Analysis**; catalog-mode vs dprod-mode; the **"package IS the
  execution unit"** doctrine (stdlib-only `run.py`, exits non-zero → drops into CI, writes
  `run_result.json` + `report.md`, no graph connection); pipelines serve **both**
  modernization (`dmig`/`cmig`) and DQ; the **two adaptation paths** — (a) client adapts the
  generated output, (b) DW **configured/customized** to match client conventions (dialect
  picker, `MaterializationTarget`, git provider/naming, fork+rebuild the capability artifact)
  — introduced as **configuration/customization**, underpinned by **templates/archetypes**
  (project archetypes scaffold workflow groups; ODCS/domain/transformation catalogs in
  `playbook/`).
- **Prereq:** Module 4/Module 5. **Validation:** download a DQ or serving package and run its `run.py`
  outside DW; inspect `run_result.json`.

### Module 8 — Advisory Services for DE & PO  *(concepts + demo · ~8–12 slides)*

- **Objective:** learner can use the coaching surfaces and knows their scope.
- **Scope:** **DE "Ask" panel** (`project-chat-assistant`, project-scoped, **read-only**,
  reasons across the whole project graph + traces lineage, `get_plan_summary` for next-step
  context); **PO "Guide me" panel** (`product-authoring-assistant` + sub-advisors,
  wizard-scoped, emits Apply suggestions). **Honesty flag:** `docs/advisor-models.md`
  describes a *proposed* per-run learning coach — **not shipped**; do **not** present it as
  live. Verify each advisory surface functionally before it goes in a deck.
- **Prereq:** Module 5. **Validation:** ask the DE panel "why is my documentation score low?" and
  get a graph-backed answer; apply one Guide-me suggestion in the wizard.

### Module 9 — Self-Directed Extension: Bring Your Own Data → a Consumer-Aligned Product  *(hands-on project · ~10–14 slides)*

- **Objective:** learner synthesizes their own sample data (one table up to a small
  multi-table set), loads it into Postgres, and runs it through DW to discover, profile, and
  build a **consumer-aligned** product.
- **Scope:** synthesize data; load into the local Postgres (or a new DB); register a source;
  run discovery/profiling; author a `dpe-cf` **consumer** product; deploy; validate. Reuses
  Module 2–Module 5 skills with no scaffolding.
- **Prereq:** Module 3–Module 5. **Outcome:** a learner-authored consumer product live in the
  Marketplace. **Validation:** Preview rows + per-product Q&A on their own product.

### Module 10 — Feedback Loop, Roadmap & Placeholders  *(meta · ~6–8 slides)*

- **Objective:** close the loop and set expectations for what's deferred.
- **Scope:** the **feedback mechanism** — how the Innovation team feeds experience + change
  proposals back to the DW team (channel/template — **define; likely a GitHub/Gitea issue
  template or a structured form; author fresh**); **explicit placeholders** — **semantic
  layer = its own future chapter**, plus other future topics (advanced
  serving/lakehouse/object-store, migration `dmig`/`cmig`, Connected-Estate feasibility,
  auth/RBAC hardening) named-but-not-built.
- **Prereq:** none (capstone).

---

## Net-new content register (nothing in the repo — must author fresh)

See [`ground-truth-facts.md`](./ground-truth-facts.md#net-new-content-nothing-in-repo--author-fresh-in-phase-2)
for the authoritative list. In brief:

1. **Hardware sizing** — 32 GB RAM minimum + CPU/disk + the *why* (concurrency). (Module 2)
2. **Concurrency/scale note** — currently only a placeholder question in `docs/faq.md`. (Module 2)
3. **WSL2 / Ubuntu primer** — no existing doc; all repo hits incidental. (Module 1)
4. **LLM key provisioning + 30-day rotation governance policy/runbook.** (Module 2)
5. **Secret-hygiene guidance** — `.env` holds a live-looking Foundry key + real tokens on
   disk (gitignored but present); "rotate + never screenshot." (Module 2)
6. **Feedback-loop channel + template.** (Module 10)
7. **Canonical latest-version location** — *open decision* (see below); placeholder until
   set.

## Stale-doc reconciliation (fix or avoid before it lands in a deck)

See [`ground-truth-facts.md`](./ground-truth-facts.md#confirmed-stale-docs-fix-or-avoid-before-a-claim-lands-in-a-deck)
for the verified table. In brief:

- `docs/deployment-guide.md` "Optional services" describes MySQL/Gitea as plain-compose +
  manual Gitea — **stale** vs the profile-gated `dwb` model.
- `samples/*/README.md` "manual `psql` deploy" note — **stale**; deploy is Configure → Build
  → Deploy.
- `po-kit/README.md:74` says "50" in body vs **57** in header — 57 is correct.
- The 2026-08-07 deck pack says **136 DE / 38 PO** tools — **stale**; use **141 / 57**.
- `docs/faq.md:96` says **67 skills** — drift; use **69** (filesystem, 2026-08-25).

## Reference-only material (draw facts from; do NOT template — different purpose/tone)

- `research/2026-08-07-deck-content-overview-and-capabilities.md` (deck content pack),
  `research/2026-08-07-consumption-and-deployment-positioning.md`,
  `docs/presentation/index.html` + `multi-platform-onboarding.html` (reveal.js positioning
  decks), `docs/diagrams/arc*` graphics, `docs/demo-javarrus-2026-07-07.md` (5-act demo
  script — closest existing hands-on scaffold).

## Reuse for authoring (accurate sources to lift from)

- **Setup:** `docs/getting-started.md`, `docs/deployment-guide.md`, the `dwb`/`cli/*`
  launcher.
- **Flagship + concepts:** `docs/userguide.md`, `docs/mapping-and-transformation.md`,
  `docs/agent-skills-reference.md`,
  `docs/architecture/{ontologies,marketplace,transform-portability}.md`,
  `samples/hr/README.md`.
- **MCP:** `docs/engineer-guide.md`, `docs/mcp-architecture.md`, `engineer-kit/`, `po-kit/`,
  `bootstrap/bootstrap-prompt.md`.
- **DQ:** `docs/data-quality-testing.md`; `serving_runners/` (the stdlib runners).
- **Diagrams:** `docs/diagrams/*.excalidraw|*.svg` (+ the excalidraw/svg-architect skills
  for new ones); deck build later via acnpptx / python-pptx (tool TBD).

## Open decisions to resolve before/at authoring

- **Canonical latest-version distribution location** (repo `enablement/` vs external space
  vs both) — requester to decide; blocks nothing but the Module 0 placeholder.
- **Deck-production tool** — decide at the production phase; content stays tool-agnostic.
- **Feedback-loop channel** — pick issue template vs form (Module 10).

---

## Verification (how to prove the plan is executable)

1. **Fact-accuracy pass:** every module's claims are traceable to the ground-truth sources
   above; the stale-fact items are corrected, not propagated. *(Done for the load-bearing
   facts — see [`ground-truth-facts.md`](./ground-truth-facts.md), verified 2026-08-25.)*
2. **Flagship dry-run rehearsal (the critical gate):** on a clean machine, `./dwb doctor` →
   `./dwb up --with postgres`, then run **Module 3 + Module 4 end-to-end on the HR sample** — build both
   source products, the `monthly_headcount_cost` aggregate, hit and resolve the two-source
   join gotcha, deploy, and confirm **lineage + per-product Q&A + Preview rows**. Capture the
   exact screenshots the deck markdown calls out. Time the run and the persona-switch count
   to validate the time-box + pre-seed mitigation.
3. **MCP dry-run:** connect a second Claude Code session via the installer, list projects,
   kick a stage, confirm `running → complete` in the UI (validates Module 6, incl. 141/57 counts).
4. **Independence check:** download one serving/DQ package and run its `run.py` with the
   stack **down** — proves the Module 7 "no runtime dependency" claim.
5. **Content-phase exit criteria:** each deck's markdown has a complete slide list + a
   resolved `[SCREENSHOT: …]` slot list, reviewed and signed off, before any deck is
   generated.

> Items 2–4 require a running stack + LLM key and are **rehearsal gates for the
> content-authoring phase** (they are where the screenshots come from), not part of this
> Phase-1 planning deliverable.
