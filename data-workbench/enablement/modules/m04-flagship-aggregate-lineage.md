# Module 4 — Flagship Pt 2: Build an Aggregate & See Lineage

- **Type:** hands-on project (screenshot-driven)
- **Target length:** ~19 slides
- **Prereq:** Module 3 (two source-aligned products live in the Marketplace)
- **Latest-version pointer:** `[PLACEHOLDER: canonical-latest-version location — TBD]`
  *(carry on every deck until the open decision is resolved)*

> **Slide creator brief — Module 4 of 11.** House style is in **Module 0** — follow that
> format for every slide. The learner has completed Module 3 and has two published
> source-aligned products in the Marketplace: `workforce_core` (from HR schema `hr_core`)
> and `compensation_payroll` (from `hr_comp`). Data Workbench has two persona-scoped web
> shells — **Product Workbench** (violet, PO) and **Engineering Workbench** (blue, DE) —
> toggled by a **Switch** button. This module builds one aggregate product
> (`monthly_headcount_cost`) that `:CONSUMES` both source products, deploys it, and reads
> the resulting column-level and product-level lineage. The two-source join gotcha
> (`:REFERENCES` does not cross product boundaries, so the first serve fails) is a
> deliberate teaching moment, not a bug.
> **Screenshot placeholders:** insert a **gray placeholder box** at the stated size —
> **Large** = full-width, ~50–60% of slide height (primary visual) · **Medium** = ~half the
> slide, shared with text · **Small** = ~quarter-slide, accent or confirmation shot.
> Do not omit placeholder boxes — they mark where live screenshots are inserted later.

---

## Slide 1 — Flagship Part 2: one aggregate, three-product chain
**Beat:** the frame — you now stitch Module 3's two source products into one aggregate and read the lineage.

- In Module 3 you built **two source-aligned products** from the HR sample:
  `workforce_core` (from schema `hr_core`) and `compensation_payroll` (from schema `hr_comp`).
- Here you build **one aggregate product — `monthly_headcount_cost` — that `:CONSUMES` both**,
  deploy it, and read the lineage the graph recorded along the way.
- **Objectives (what you can do at the end):**
  1. Author an **aggregate** in the 10-step `NewProductWizard` (contract-first) and bind it to
     two upstream products.
  2. Hit — and **resolve** — the two-source join gotcha as the DE.
  3. Read a 3-product chain across the Marketplace **Lineage**, **Sankey**, and Overview surfaces.

_Speaker notes:_ This is the payoff module — it's where the persona model, the `:CONSUMES` graph,
and the "lineage just works" promise from Module 0 all become concrete. Keep the HR sample loaded from Module 2.
**Lab timing benchmark:** end-to-end creation of two source-aligned products + one consumer-aligned
product (the Module 3 + Module 4 arc) takes approximately **45 minutes to 1 hour** with pre-staged data and
human review gates. That benchmark is from a live facilitated session with an experienced practitioner
— actual time varies by LLM latency, review depth, and whether source products were pre-seeded.

---

## Slide 2 — Prerequisites & starting line
**Beat:** confirm you're actually at the start line before you build.

- **Stack up and green:** `./dwb status` all healthy; UI at `http://localhost:5173`.
- **Module 3 complete:** open the Marketplace and confirm **both** source products show with the
  **Source** (blue) chip, each with a **Preview** tab that returns rows and a deployed **Serving** view.
- You'll be **switching personas repeatedly** — PO (Product Workbench, violet) authors and
  binds; DE (Engineering Workbench, blue) maps and serves. We flag every switch inline.

> **Concept callout — why an aggregate here.** An *aggregate* is a reusable building block meant
> to be consumed further, so it **defaults to a materialized serving recommendation** to de-risk
> the downstream chains that will read it. `monthly_headcount_cost` (one row per department ×
> month) is exactly that shape — a snapshot other products and dashboards build on.

---

## Slide 3 — What you're building: `monthly_headcount_cost`
**Beat:** the target, as a picture, before any clicks.

- One aggregate that combines **headcount** (from `workforce_core`) and **cost**
  (from `compensation_payroll`), grouped by **department × month**.
- The chain you'll end up with:

```
 workforce_core ─────┐
 (source, blue)      ├──:CONSUMES──▶ monthly_headcount_cost (aggregate, purple)
 compensation_payroll┘
 (source, blue)
```

- Grain: **one row per `department_id` × `month_of`**; measures like `headcount`,
  `total_compensation`, `avg_tenure_years` (the HR sample's product C schema).

> **Concept callout — `productKind` is decoupled from archetype.** `aggregate` is a late-binding
> flag (`source | aggregate | consumer`), not a project type. Aggregate rides the exact same
> consumer-aligned (`dpe-cf`) machinery as a consumer product — it differs only by *intent*
> (reusable block vs. fit-for-purpose leaf). Every `dpe-cf` code path already covers it.

---

## Slide 4 — PO: open NewProductWizard — the 10-step contract-first flow
**Beat:** map the whole wizard, then take the first two steps.

- **PO** — Product Workbench → **New Product → Consumer-aligned product**. The 10 steps
  (contract-first ordering; the wizard's `STEP_LABELS` is the source of truth):
  1 Describe & Domain · 2 Shape · 3 Suggest sources *(opt)* · 4 Shape the Schema ·
  5 Product Details · 6 Operations & Support · 7 Rule Coach · 8 Readiness *(opt)* ·
  9 Confirm sources *(required gate)* · 10 Submitted.
- **Step 1 — Describe & Choose Domain.** Paste the PO's rough "monthly headcount + cost per
  department, month over month, that builds up over time" idea; domain = **HR**.
- **Step 2 — Shape.** Describe the **grain** ("one row per department per month"), optionally a
  filter, and an SCD policy (leave *Latest only* / accumulating snapshot for this product).

> **Concept callout — use-case-driven discovery.** The wizard asks the PO to describe a
> **use case** before picking inputs. That description isn't just documentation — it's the
> agent's starting point for backward-chaining: identify which source-aligned products are
> required, check which already exist in the graph, and surface the gaps. The preferred flow
> starts from a business question ("monthly headcount + cost per department"), not a product
> type. "What do we need to answer this?" is a better first question than "what tables do we
> have?"

_Speaker notes:_ "Contract-first" is the load-bearing contrast with Module 3's discovery-first SA flow:
the PO shapes the product *before* any upstream binding. No AI stage runs in the wizard itself.

---

## Slide 5 — PO Step 3: suggest candidate sources (optional)
**Beat:** pre-select the two Module 3 products so the schema advisor ranks their columns.

- **PO** — Step 3 auto-fires a match against your partial spec and lists published products you
  can pre-select. **Pick both `workforce_core` and `compensation_payroll`.**
- This is **skippable** (you can bind at Step 9 instead) — but picking here feeds the two
  source contracts into Step 4's schema advisor so *their* columns rank higher for you.

> **Concept callout — an aggregate can consume anything published.** The upstream picker offers
> **source, aggregate, or consumer** products — a consumer/aggregate may build on any of them,
> forming multi-hop DAG chains (A→B→C). Here you pick two sources; the machinery is the same at
> any depth.

---

## Slide 6 — PO Step 4: Shape the Schema — grouping keys
**Beat:** declare the columns and mark the grain keys that make this an aggregate.

- **PO** — add the product's columns (`department_id`, `department_name`, `month_of`,
  `headcount`, `total_compensation`, `avg_tenure_years`, …).
- Mark **`department_id`** and **`month_of`** as **Group by** keys. Per-column controls also
  offer **Suppressed** (drop from the view, keep in contract/lineage) and a **Derive** editor.

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `m4-schema-grouping-keys`
Caption: Shape the Schema with two Group-by keys marked.
Must show: The Step-4 schema table with `department_id` and `month_of` flagged as **Group by**,
and the "N grouping keys marked — engineering aggregates non-key columns at materialization" note.
Taken at: PO wizard, Step 4 (Shape the Schema), after adding columns and toggling the two keys.

> **Concept callout — grouping keys are a contract decision.** Marking a column **Group by**
> writes `:DatasetTransform.grouping_keys`; at serving time the engineer's non-key columns carry
> an `aggregateFunction` and the compiler emits a `GROUP BY`. The PO declares *what* aggregates;
> the DE implements *how*.

---

## Slide 7 — PO Step 5: Product Details — declare `productKind = aggregate`
**Beat:** the crux PO decision — this is where the product becomes an aggregate.

- **PO** — Step 5 sets name / description / purpose / dataset physical name, **and** the
  **Product kind** selector (Aggregate | Consumer).
- Because you bound **≥2 distinct sources**, the wizard **auto-suggests `Aggregate`** (until you
  touch it). Confirm it. The panel notes that **aggregates default to a materialized serving
  mode** so downstream products read a stable table, not a live multi-join.

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `m4-product-details-aggregate`
Caption: Product Details with `productKind = aggregate` auto-suggested.
Must show: The Aggregate/Consumer selector with **Aggregate** highlighted, the auto-suggest hint
("suggested because you selected 2+ sources"), and the "Aggregates default to a **materialized**
serving mode" note.
Taken at: PO wizard, Step 5 (Product Details), with two sources bound.

_Speaker notes:_ The auto-suggest is a hint, not a lock — the PO can override to Consumer. It sets
`spec.productKind`, resolved server-side in `_resolve_product_kind`.

---

## Slide 8 — PO Steps 6–8: Ops, Rule Coach, Readiness
**Beat:** the three describe-and-check steps — mostly optional, move fast.

- **PO** — **Step 6 Operations & Support:** SLA / contacts / roles — all optional; inherited
  from the bound sources where available.
- **Step 7 Rule Coach:** review AI-suggested quality rules for your schema; **Approve** the ones
  that match expectations (e.g. "a department-month with `headcount = 0` must have
  `total_compensation = 0`"), reject the rest. Approved rules ship in the contract.
- **Step 8 Readiness Review** *(optional):* fire the OSI readiness score + a sample of consumer
  questions to sanity-check before submitting.

---

## Slide 9 — PO Step 9: Confirm sources — the `:CONSUMES` gate + gap check
**Beat:** the required submit gate — no aggregate ships without confirmed upstreams.

- **PO** — Step 9 pre-populates the two sources from Step 3. This is the **required submission
  gate**: submit is blocked until every schema slot is matched to a confirmed source.
- Run the **Pre-flight gap check** — it flags each column as *covered* / *derivable* /
  *ambiguous* / *gap*. Fix gaps (add/adjust a source) or explicitly acknowledge them.

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `m4-confirm-sources-consumes-gate`
Caption: Confirm sources — both upstreams bound, gap check run.
Must show: `workforce_core` and `compensation_payroll` confirmed as `:CONSUMES` inputs, and the
Pre-flight gap-check results panel showing per-column covered/derivable/gap statuses.
Taken at: PO wizard, Step 9 (Confirm candidate sources), after clicking Pre-flight gap check.

> **Concept callout — the DAG guard is a hard rule.** On submit the backend MERGEs a
> `:CONSUMES` edge per confirmed source and runs `validate_consumes_bindings` in one atomic
> write: self-consumption or any binding that would create a cycle is rejected with a **409** and
> the contract is left byte-unchanged. Chains stay acyclic by construction.

---

## Slide 10 — PO Step 10 → Switch → DE: Accept & Import
**Beat:** the first persona hand-off of Part 2.

- **PO** — **Step 10 Submit.** The request lands in the engineer's **Incoming** queue.
- **Switch → DE** — Engineering Workbench → **Incoming** → **Accept** the request. The project
  appears in your list.
- **DE** — run **Import Contract** (`odcs_to_dprod`) — a mechanical, no-AI stage that reads the
  PO's ODCS contract and creates the product/output-dataset/column nodes in the graph.

_Speaker notes:_ Accept is a real gate — until the DE accepts, the downstream stages stay locked.
This is the persona-switch friction from Module 0, working as intended.

---

## Slide 11 — DE: Mapping & Transformation (`--source-mode dprod`)
**Beat:** the AI maps product columns to the consumed sources; you adjudicate.

- **DE** — run **Mapping & Transformation** (`data_mapping`). Because the archetype is `dpe-cf`,
  the agent runs with **`--source-mode dprod --target-contract <id>`** — it maps to `:DProdColumn`
  from the `:CONSUMES`'d source products, **not** raw catalog tables.
- Open the **Mappings** review queue. Per row, act:

| Action | When |
|---|---|
| **Approve** | Mapping is right. Rate quality (Acceptable / Good / Excellent). |
| **Replace** | Change source column / edit the transform / remap. Requires a reason; the original AI suggestion is preserved. |
| **Escalate** | Correct transform needs domain knowledge you lack → sends it to the Data Steward. |

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `m4-mapping-review-approve-replace-escalate`
Caption: The Mapping & Transformation review with Approve / Replace / Escalate.
Must show: A pending mapping row for a `monthly_headcount_cost` column, its source `:DProdColumn`
and author badge, and the three action controls (Approve / Replace / Escalate).
Taken at: DE, Mapping & Transformation stage → Reviews tab, after the mapping agent runs.

> **Concept callout — one recipe, different stove.** Mappings are stored as a
> **platform-independent DSL** (`transformKind` + params) in the graph — no SQL dialect decided
> here. The dialect is chosen at *serving* time and a hand-written emitter compiles the same
> recipe to Postgres / Snowflake / Databricks SQL. Module 5 goes under the hood.

---

## Slide 12 — The two-source join gotcha (this is a feature)
**Beat:** the highlight of the module — the first serve fails, and that's the lesson.

- `monthly_headcount_cost` mixes tables from **two different source products**, joined on the
  shared `employee_id`. But **`:REFERENCES` (the FK edge) never crosses product boundaries** —
  FK propagation is single-product-scoped.
- So the first serve has **no FK path** to connect the two sources, and you get:
  `ViewGenerationError: source table X is referenced by mappings but no FK path connects it to
  other tables`. Under materialization this surfaces as a clean **422**, not an opaque 500.

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `m4-viewgen-no-fk-path-error`
Caption: The expected no-FK-path error on the first serve of a two-source aggregate.
Must show: The `ViewGenerationError` / 422 banner naming the disconnected table and "no FK path",
surfaced in the serving or materialization step.
Taken at: DE, first run of the serving/materialization stage before authoring a bridge.

> **Concept callout — why this is a feature, not a bug.** The graph is telling you the truth:
> two independent products have no shared FK, so the join is *your* declaration to make. The
> product refuses to guess a cross-product join and silently produce wrong numbers.

---

## Slide 13 — Resolve it: join-preflight bridge or `:DatasetTransform.joins[]`
**Beat:** two sanctioned fixes — one-click, or hand-authored.

- **Path A — one-click bridge.** `GET /api/projects/{id}/serving/join-preflight`
  (`join_preflight.py`) detects the disconnected components and returns a **`recommended_joins`**
  payload — apply it in one click via **`PUT /dataset-transform/joins`**. It chains transitively
  over shared identity keys (`employee_id`) and can pull in a junction dataset as a pure bridge;
  a tie is surfaced as `junction_candidates`, never guessed.
- **Path B — author it explicitly.** Set `:DatasetTransform.joins[]` yourself (the explicit-join
  path **bypasses FK inference and always wins**) — an `employee_id` join across the two sources.
- Re-run serving → it now succeeds.

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `m4-join-preflight-fix`
Caption: Applying the join-preflight bridge (or the `:DatasetTransform.joins[]` editor).
Must show: The recommended-joins panel with the `employee_id` bridge and an **Apply** control
(or the equivalent `:DatasetTransform.joins[]` join editor with the cross-product join authored).
Taken at: DE, after the no-FK-path error, resolving via join-preflight or the join editor.

_Speaker notes:_ The `data-mapping-neo4j` skill runs the same preflight while authoring, so a
scripted (MCP) run can recommend the bridge up front instead of dead-ending. Either path lands
the same explicit join.

---

## Slide 14 — DE: Configure Serving (materialized) → Build → Deploy → Complete
**Beat:** finish the engineering — serve the aggregate as a materialized table.

- **DE** — **Configure Serving.** For an aggregate the recommended mode is **Materialized (dbt)**
  — best for grouped snapshots and downstream reuse. (Virtual view / Lakehouse are the other two.)
- **Build** produces a **self-contained package** (no live DB needed to build); materialized dbt
  applies a **sample → review → approve → full-build** verification gate.
- **Deploy** executes it in the target DB; run **Deployment Reflection** *(optional)* and
  **Mark Engineering Complete**.

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `m4-configure-serving-materialized`
Caption: Configure Serving with the materialized (dbt) mode selected.
Must show: The serving-mode picker with **Materialized (dbt)** chosen and the Build/Deploy split
(the materialization gate dialog is fine).
Taken at: DE, Configure Serving stage for `monthly_headcount_cost`.

> **Concept callout — the SCD policy drives the serving mode.** The clean decision rule:
> **SCD2** (tracking history — accumulating rows over time, like compensation history) →
> must **materialize** (impossible to serve history via a live view). **SCD1** (latest state
> only) → a **virtual view** is fine. If a product accumulates rows over time, materialization
> isn't optional — the serving step will tell you. Asking "SCD1 or SCD2?" is how you
> mechanically decide whether to materialize before touching the UI.

> **Concept callout — same platform vs different platform.** The quick decision rule: if the
> product stays on the **same platform** as its sources, DW can materialize it via a
> **virtual view** (a pass-through). If it needs to **move to a different platform**, DW
> auto-generates an extract-and-load package with version control and a downloadable
> artifact. Same recipe, different stove — the serving mode is the only thing that changes.

_Speaker notes:_ "Materialized default" is a recommendation, not a lock — you can serve an
aggregate as a virtual view if you want; the PO's hint just steers you to the safe choice.

---

## Slide 15 — Read the lineage I: column canvas + product-to-product DAG
**Beat:** open the aggregate in the Marketplace and read where it came from.

- **PO or DE** — Marketplace → open `monthly_headcount_cost`.
- **Lineage tab** — the **column-level canvas** (`MappingGraphView`): product columns on the
  right, their source columns on the left, mapping edges between. Multiple source datasets render
  as separate source boxes (not one collapsed blob).
- **Lineage sub-tab** — the **product-to-product `:CONSUMES` DAG** (`ProductLineageView`): the
  coarse view showing `monthly_headcount_cost` fed by both source products.

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `m4-lineage-column-canvas`
Caption: Column-level lineage canvas for the aggregate.
Must show: The `MappingGraphView` canvas with `monthly_headcount_cost` columns on the right and
source columns from **both** `workforce_core` and `compensation_payroll` on the left, edges drawn.
Taken at: Marketplace → `monthly_headcount_cost` → Lineage tab.

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `m4-lineage-consumes-dag`
Caption: Product-to-product `:CONSUMES` DAG.
Must show: A node graph with the two source products both pointing (via `:CONSUMES`) into the
`monthly_headcount_cost` aggregate node.
Taken at: Marketplace → `monthly_headcount_cost` → Lineage → the Lineage sub-tab.

---

## Slide 16 — Read the lineage II: Sankey view + Overview cross-refs
**Beat:** the wide value-flow, and the plain-text cross-references.

- **Sankey view** sub-tab — the **5-column** layered value flow: Source Schemas → Source-Aligned
  → Aggregate/Derived → Consumer-Aligned → Use Cases. Your aggregate sits in the middle column,
  fed by the two source-aligned products (Use Cases renders a "coming soon" ghost — expected in v1).
- **Overview tab** — read the **consumes / consumed_by** cross-references: the aggregate's
  Overview shows `consumes = [workforce_core, compensation_payroll]`; opening each source shows
  `consumed_by = [monthly_headcount_cost]`. Click-through pivots between them.

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `m4-sankey-5-column-flow`
Caption: The 5-column Sankey value-flow with the aggregate in the middle.
Must show: The fixed 5-column layout (Source Schemas → Source-Aligned → Aggregate → Consumer →
Use Cases) with the two sources flowing into `monthly_headcount_cost` in the Aggregate column.
Taken at: Marketplace → Sankey view sub-tab.

---

## Slide 17 — Outcome — a 3-product chain with visible lineage
**Beat:** what "done" looks like.

- The Marketplace lists **three** products: two **Source** (blue) and one **Aggregate**
  (purple), with a live `:CONSUMES` chain between them.
- `monthly_headcount_cost` has a deployed materialized table, real rows, and lineage that traces
  every product column back to a source column across two source products.

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `m4-outcome-marketplace-chain`
Caption: The finished 3-product chain in the Marketplace.
Must show: The Marketplace list (or filter chips All / Source / Aggregate / Consumer) with the
two blue Source products and the purple **Aggregate** `monthly_headcount_cost` visible.
Taken at: Marketplace, after Mark Engineering Complete.

---

## Slide 18 — Validation (run this yourself — semantic-layer-free)
**Beat:** the green check you perform to prove success.

- **1 — Kind chip:** `monthly_headcount_cost` shows the **Aggregate (purple)** chip; the
  filter's **Aggregate** chip lists exactly it.
- **2 — Cross-refs:** its Overview shows `consumes = [workforce_core, compensation_payroll]`;
  each source shows `consumed_by = [monthly_headcount_cost]`.
- **3 — Preview:** the **Preview** tab returns real department × month rows.
- **4 — Per-product Q&A:** on the aggregate's **Q&A tab**, answer a **curated** question and run
  a **free-form probe** (e.g. "total compensation for department X last month?").

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `m4-per-product-qa-answer`
Caption: The per-product Q&A tab answering a question about the aggregate.
Must show: The `monthly_headcount_cost` **Q&A** tab with a curated question executed (rows/answer)
and the free-form probe box.
Taken at: Marketplace → `monthly_headcount_cost` → Q&A tab.

> **Concept callout — do NOT use the domain Semantic Q&A chat here.** The domain-scoped Semantic
> Q&A chat depends on the **semantic layer**, which is **out of scope for this track** — without
> it, that chat degrades to Full-Context-only and is not a reliable validation surface. Validate
> product understanding **only** via the self-contained **per-product Q&A tab**.

---

## Slide 19 — Next steps
**Beat:** close Part 2 and point forward.

- You built a **3-product chain** (two sources → one aggregate) with **visible column-level and
  product-level lineage** — and turned the two-source join gotcha into a deliberate, authored join.
- **Self-check (answer before moving on):**
  1. Why did the *first* serve of the aggregate fail, and what are the *two* ways to fix it?
  2. What auto-suggested `productKind = aggregate`, and what does that default the serving mode to?
  3. Which validation surface do you use for the aggregate — and which must you avoid, and why?
- **Latest version lives at:** `[PLACEHOLDER: canonical-latest-version location — TBD]`.
- **Next:** **Module 5 — Under the Hood** — skills, the knowledge graph, and how the transform DSL
  compiles to per-platform SQL. Everything you just did, explained.
