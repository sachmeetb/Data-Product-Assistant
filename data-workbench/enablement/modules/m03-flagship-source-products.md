# Module 3 — Flagship Pt 1: Build Two Source-Aligned Products

- **Type:** hands-on project (screenshot-driven)
- **Target length:** ~22 slides
- **Prereq:** Module 2 — stack up (`./dwb status` green), HR Postgres sample loaded + reachable, LLM key working
- **Latest-version pointer:** `[PLACEHOLDER: canonical-latest-version location — TBD]`
  *(carry on every deck until the open decision is resolved)*

> **Slide creator brief — Module 3 of 11.** House style is in **Module 0** — follow that
> format for every slide. The learner has a healthy running DW stack with the HR Postgres
> sample loaded (Module 2 complete). Data Workbench has two persona-scoped web shells:
> the **Product Workbench** (violet header, for the Data Product Owner / PO) and the
> **Engineering Workbench** (blue header, for the Data Engineer / DE); a **Switch** button
> in the header toggles between them. This module builds two source-aligned data products
> (`workforce_core` and `compensation_payroll`) from HR sample data, driving both the PO and
> DE personas through a relay of persona switches and explicit hand-off gates. The learner
> plays both roles themselves.
> **Screenshot placeholders:** insert a **gray placeholder box** at the stated size —
> **Large** = full-width, ~50–60% of slide height (primary visual) · **Medium** = ~half the
> slide, shared with text · **Small** = ~quarter-slide, accent or confirmation shot.
> Do not omit placeholder boxes — they mark where live screenshots are inserted later.

---

## Slide 1 — Flagship Pt 1: two source-aligned products, end to end
**Beat:** set the frame — this is the first real build, and it's a two-persona relay.

- You'll discover and publish **two source-aligned data products** from the HR sample:
  `hr_core` → **`workforce_core`** and `hr_comp` → **`compensation_payroll`**.
- You'll drive **both personas yourself** — Product Owner (PO) and Data Engineer (DE) —
  switching shells at every hand-off gate. That switching is the lesson, not a chore.
- At the end you'll have two **Source** (blue-chip) products live in the Marketplace, each
  with a deployed view returning real rows. Module 4 then builds an **aggregate** on top of them.

_Speaker notes:_ This is the payoff for Module 0–Module 2. Everything conceptual in Module 0 (two personas,
`productKind = source`, the graph as source of truth) becomes muscle memory here. Tell
learners to keep both hands on the keyboard — screenshots only show them what "right" looks
like.

---

## Slide 2 — Objectives
**Beat:** what you can do when this module is done.

- **Initiate** a source-aligned product from the PO shell with the 3-step
  `NewSourceProductWizard` (idea + domain + name).
- **Run** the DE discovery pipeline (accept → connect → discover → profile → enrich →
  standardize names → mark complete) and **clear** the PO 5-tab validation gate.
- **Materialize + deploy** a source product to a live, queryable view, then confirm it in the
  Marketplace with real Preview rows.
- Do all of the above **twice**, feeling the persona-switch relay deliberately.

---

## Slide 3 — Prerequisites / starting point
**Beat:** confirm you're actually at the start line before touching anything.

- **Module 2 complete:** `./dwb status` is green; UI at `http://localhost:5173`; backend health
  `GET http://localhost:8000/api/health` OK; Neo4j browser at `http://localhost:7475`.
- **HR sample loaded** into the local Postgres (host port **5433**). From inside the
  Workbench container the DB is reachable as `host.docker.internal`, not `localhost`.
- **Sample DB shows as a Quick connect** entry in the Select-Data-Source picker (written by
  `./dwb connect`). If it doesn't, re-run `./dwb connect` before proceeding.
- You know how to hit the header **Switch** button (PO shell ↔ DE shell) and the DE **role
  selector**.

> **Concept callout — one DB, two schemas, two products.** The HR sample models two
> physically separate systems of record in one Postgres instance: `hr_core` (Core HRIS) and
> `hr_comp` (Compensation/Payroll). Each becomes its **own** source product. There is no
> cross-schema foreign key — the only link is the shared `employee_id` business key, which
> matters in Module 4, not here.

> **Concept callout — graph bootstrapping: source-aligned must exist before
> consumer-aligned.** The knowledge graph starts empty — nodes are created only when a
> user commits to implementing a data product. For clients who already have existing data
> products or metadata, those would need to be ingested into the graph first. **A
> consumer-aligned product (Module 4) can only bind to a product node that already exists** — if
> the source-aligned products you're building here don't exist yet, Module 4's wizard will have
> nothing to connect to. Complete Module 3 before starting Module 4.

---

## Slide 4 — The relay: two products, one keyboard, many switches
**Beat:** name the persona-switch dance up front so nobody loses their place.

- Every source product is the **same relay**, threaded across shells:
  **PO** (create) → **Switch → DE** (accept + discover) → **Switch → PO** (validate) →
  **Switch → DE** (materialize + deploy).
- Module 3 alone is roughly **6 persona switches** (two products); the full flagship (Module 3 + Module 4) is
  ~8–10. We flag each **Switch →** explicitly in the steps.
- Slides carry a persona tag: **PO** = Product Workbench (violet header), **DE** =
  Engineering Workbench (blue header).

> **Concept callout — the friction IS the point.** The PO *shapes and validates*; the DE
> *builds and serves*. Work only moves between them at explicit gates. Feeling the switch is
> how the persona/governance model stops being an abstraction. Don't try to "skip ahead" in
> one shell — the gates won't let you, by design.

---

## Slide 5 — The HR sample at a glance (and the cryptic columns)
**Beat:** know what you're profiling — and the one deliberate stress test to watch for.

- **`hr_core`** (7 tables → `workforce_core`): `employee`, `department`, `job`, `location`,
  `job_assignment_history`, `employment_status_history`, `performance_review`.
- **`hr_comp`** (→ `compensation_payroll`): `pay_employee`, `salary_history`,
  `benefit_plan`, `benefit_enrollment`.
- **Three deliberately cryptic, uncommented columns** — their meaning must come from
  *profiling the data*, not the name:

| Column | Where | Data signal | Enriched → standardized name |
|---|---|---|---|
| `lvl` | `hr_core.job_assignment_history` | smallint 1–8 | job level → `job_level` |
| `amt` | `hr_comp.salary_history` | ~50k–156k, 2 dp | base salary → `salary_amount` |
| `rsn` | `hr_comp.salary_history` | {MERIT, PROMO, MKT, ADJ} | pay-change reason → `change_reason` |

_Speaker notes:_ Point learners at these three columns now so they *watch* the AI resolve
them during Enrichment/Standardization. It's the most convincing "the AI actually read the
data" moment in the whole flagship.

---

## Slide 6 — Time-box mitigation (optional, read before you start)
**Beat:** the full relay is long — here's how to shorten a live session honestly.

- The full flagship is **~8–10 persona switches**. In a time-boxed session that's a lot of
  hand-offs to sit through twice.
- **Mitigation:** optionally **pre-seed the first source product** (`workforce_core`) — or
  script its build via MCP (forward-ref **Module 6**, driving DW headlessly with the PO/DE tools) —
  so learners build only the **second** source product here and the **aggregate** in Module 4.
- If you pre-seed, still *walk* Slides 7–16 as a narrated demo so learners see the relay
  once, then have them **do** `compensation_payroll` (Slide 17) themselves.

> **Concept callout — pre-seeding is a teaching lever, not a shortcut in the product.** The
> product always requires the full relay; pre-seeding just moves who ran it. Be explicit with
> learners about what was done for them so the persona model still lands.

---

## Slide 7 — **PO** · Create `workforce_core` (wizard step 1: Describe)
**Beat:** the PO starts the relay with a plain-language idea.

- In the **Product Workbench** (violet header), click **New Product → Source-aligned
  product**. The 3-step `NewSourceProductWizard` opens.
- **Step 1 — Describe your idea:** paste rough, top-of-mind prose. The DE reads this to know
  what they're profiling. A good starter (from the sample's rough PO notes):
  > "One current-state roster of who works here — name, current job and department, location,
  > tenure, current pay. Mask email, hide national ID, active people only."
- This free-form text is saved as the project's `product_idea`; there is **no ODCS spec yet**
  — it's synthesized later from the approved graph.

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `sa-wizard-idea`
Caption: Step 1 of the source-aligned wizard — the free-form idea.
Must show: The `NewSourceProductWizard` on step 1, the "Describe your idea" textarea with the
rough prose typed in, and the step indicator showing 1 of 3.
Taken at: Product Workbench → New Product → Source-aligned product, step 1.

---

## Slide 8 — **PO** · Wizard steps 2–3 (Domain + Name) → Submit
**Beat:** finish the lightweight wizard and hand off to engineering.

- **Step 2 — Choose a domain:** pick the business domain (e.g. **HR**).
- **Step 3 — Name the product:** give it the consumer-facing name **`workforce_core`**.
- Click **Submit**. The request lands in the DE **Incoming** queue and the product shows on
  **My Products** as awaiting engineering.

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `sa-wizard-domain-name`
Caption: Steps 2 and 3 — domain and the consumer-facing name.
Must show: The domain selector set to HR (step 2) and the name field reading `workforce_core`
(step 3) with the Submit button; two frames or one mid-flow shot are both fine.
Taken at: `NewSourceProductWizard` steps 2 and 3, just before Submit.

_Speaker notes:_ Emphasize how *little* the PO commits here — an idea, a domain, a name. The
richness comes from the engineer profiling the real source. That's the source-aligned
contract: discovery-first, PO validates later.

---

## Slide 9 — **Switch → DE** · Accept the request (the hard gate)
**Beat:** the first switch — and the gate that silently blocks everything if you skip it.

- Hit the header **Switch** button to enter the **Engineering Workbench** (blue header). In
  the left nav, open **Incoming**.
- Find the `workforce_core` request and click **Accept**. The project now appears in your
  project list.

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `de-incoming-accept`
Caption: The DE Incoming queue with the new request and the Accept button.
Must show: The Engineering Workbench Incoming list showing the `workforce_core` request with
its PO context, and the **Accept** action highlighted.
Taken at: Engineering Workbench → Incoming, before clicking Accept.

> **Concept callout — Accept is a *blocking* gate, not a courtesy.** The PO's validation
> panel stays **locked until the engineer accepts** (`ProductRequest` `submitted → accepted`).
> Skip Accept and the PO later finds a greyed-out gate with no explanation. The engineer's
> project page flags this inline, and `get_plan_summary` (Module 6) will tell you "accept first."

---

## Slide 10 — **DE** · Select Data Source (Quick-connect Postgres)
**Beat:** connect the project to the right schema — no typing secrets.

- Open the project. The first stage is **Select Data Source** → click **Set Data Source**.
- Choose the **HR Postgres Quick connect** entry (preconfigured by `./dwb connect` — host,
  port, credentials already filled; it resolves to `host.docker.internal` inside the
  container). [VERIFY: exact quick-connect entry label in the picker]
- Set the **schema to `hr_core`** for this product. This is a config step — **no AI agent
  runs**; it just saves the connection and marks the stage complete.

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `quick-connect-postgres`
Caption: The Select-Data-Source picker with the HR Postgres Quick connect.
Must show: The data-source dialog with a Quick-connect option selected for the HR Postgres
sample and the schema field set to `hr_core`.
Taken at: Select Data Source → Set Data Source, before saving.

> **Concept callout — the schema is the product boundary.** `workforce_core` = `hr_core`;
> `compensation_payroll` = `hr_comp`. Two projects point at the **same DB, different schemas**.
> Getting the schema right here is what keeps the two products cleanly separated in the graph.

---

## Slide 11 — **DE** · Data Discovery + Data Profiling (watch it stream)
**Beat:** the AI reads the schema and the data, live.

- Click **Run** on **Data Discovery**. An agent connects, reads all `hr_core` schemas,
  tables, and columns, detects primary keys and foreign-key relationships, and writes it all
  into the knowledge graph. You see the work **stream** in real time.
- Then **Run** on **Data Profiling**: the agent samples rows and computes row counts, null
  rates, distinct counts, min/max, and most-common values — stored alongside the schema.
- A few minutes is typical for a schema this size.

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `discovery-streaming`
Caption: The Data Discovery stage streaming its work.
Must show: The pipeline stage in **running** state with live streamed agent output visible in
the stage panel (schemas/tables being read).
Taken at: Data Discovery stage, mid-run.

_Speaker notes:_ This is where "the graph is the source of truth" (Module 0) becomes literal —
everything the agent learns accretes onto the same graph that later powers lineage, the
contract, and Q&A. Nothing is thrown away between stages.

---

## Slide 12 — **DE** · Metadata Enrichment (the graph decides, not the name)
**Beat:** the cryptic-column payoff — descriptions inferred from profiled evidence.

- **Run** the **Metadata Enrichment** stage. An agent uses the profiling evidence to write
  plain-English descriptions for every table and column.
- **Watch `lvl`** on `job_assignment_history`: despite the meaningless name, its 1–8
  distribution drives a description like *"job level / grade band."*
- For source-aligned products the **PO approves these descriptions** later (in the validation
  gate) — the engineer's Reviews tab hides the descriptions panel on `dpe-sa`.

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `enrichment-cryptic`
Caption: Enrichment inferring meaning for the cryptic `lvl` column from its data.
Must show: The enriched description for `job_assignment_history.lvl` referencing its 1–8
value range, proving the description came from profiling, not the column name.
Taken at: Metadata Enrichment stage, after it completes.

> **Concept callout — evidence over naming.** The whole point of the cryptic columns is to
> prove the AI reads the *data*, not the label. This is why profiling runs *before*
> enrichment: descriptions are grounded in what the values actually look like.

---

## Slide 13 — **DE** · Column-Name Standardization → Mark Discovery Complete
**Beat:** propose clean names, then signal the PO their queue is ready.

- **Run** **Column-Name Standardization**. The agent proposes cleaner, consistent names for
  the PO to review — e.g. **`lvl` → `job_level`**. (These are *proposals*; the PO decides.)
- Then **Mark Discovery Complete**. This stamps `discovery_complete_at` and flips the PO's
  request to **"Ready for your validation"** on their My Products dashboard.
- The materialization stages stay **locked** until the PO clears the gate — the engineer's
  pipeline shows an amber **"Pending PO validation"** on the head materialization stage.

_Speaker notes:_ No new screen worth a slot here beyond the standardized-names list; if you
want a shot, capture the `lvl → job_level` recommendation. Otherwise keep moving — the next
switch is the important one.

---

## Slide 14 — **Switch → PO** · Clear the 5-tab validation gate
**Beat:** the PO's blocking review — five tabs, all must clear.

- **Switch** to the Product Workbench. On the `workforce_core` card in **My Products**, click
  **Validate** to open the five-tab panel:

| Tab | What you approve |
|---|---|
| **Names** | The engineer's standardized column names (e.g. `lvl → job_level`) |
| **Descriptions** | AI-generated column + table descriptions |
| **Tables** | Table classifications (fact / lookup / audit log / …) |
| **Relationships** | How tables relate — drives the serving view's join logic |
| **Rules** | AI-suggested quality rules from profiling evidence |

- On each tab, **Approve** or **Reject** (with a reason). When **all five** are cleared, the
  gate opens and the engineer can materialize.

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `po-validation-5tab`
Caption: The PO source-product validation gate — five tabs.
Must show: The `SourceProductValidationPanel` with all five tab labels visible (Names /
Descriptions / Tables / Relationships / Rules) and at least one item being approved.
Taken at: Product Workbench → My Products → Validate on `workforce_core`.

> **Concept callout — this gate is the SA governance checkpoint.** Unlike consumer products,
> source products are *discovery-first*: the engineer proposes, the PO validates. Nothing
> materializes until every tab is cleared. The **Table** classifications you approve here even
> feed the view-DDL bridge ranker downstream.

---

## Slide 15 — **Switch → DE** · Materialize + deploy the product
**Beat:** the final relay leg — from approved graph to a live view.

- **Switch** back to the Engineering Workbench. With the gate cleared, the materialization
  stages unlock. Run them in order:
  1. **Synthesize Contract from Graph** — assembles the ODCS contract from approved names /
     descriptions / rules (mechanical, no AI).
  2. **Import Contract** — loads it into the product graph model.
  3. **Auto-Map Source Columns** — 1:1 mappings source → product (no AI needed; the PO
     already approved the names).
  4. **Data Serving** — generates the SQL **virtual view** DDL.
  5. **Deploy View** — creates the view in the target database.
  6. **Deployment Reflection** *(optional)* — AI compares deployed view vs declared shape.
  7. **Mark Engineering Complete** — notifies the PO the product is live.

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `serve-deploy`
Caption: The Serve + Deploy step producing and deploying the view.
Must show: The Data Serving stage with the generated view DDL, and/or the Deploy View action
completing (stage flipping to complete).
Taken at: Materialization stages, at Data Serving → Deploy View.

> **Concept callout — deploy is a button, not a `psql` command.** The sample READMEs'
> "manual `psql` deploy" note is **stale**. Serving follows a uniform **Configure → Build →
> Deploy** model in the UI: for a virtual view, **Data Serving** generates the DDL and
> **Deploy View** executes the `CREATE VIEW` for you. Don't hand-run SQL.

> **Concept callout — ODCS defines the target *shape*, not transformation logic.** The
> contract this pipeline synthesizes defines what the product **looks like** — tables,
> columns, data types, descriptions, SLAs, refresh cadence, and quality agreements. It does
> **not** define transformation logic. Transformations are a separate artifact: stored as a
> platform-independent DSL in the graph, and compiled to per-platform SQL at serving time
> (Module 5). The ODCS contract is the spec; the mapping + transform DSL is the execution plan.
>
> In plain language: every data product includes a **data contract** (using the **ODCS** —
> Open Data Contract Standard — spec) that defines commitments. This can be exported as YAML
> from the Marketplace. It is the *what*, not the *how*.

---

## Slide 16 — Product 1 done: `workforce_core` is live
**Beat:** checkpoint — one source product published, relay complete once.

- `workforce_core` now appears in the **Marketplace** as a **Source** (blue-chip) product,
  with a formal ODCS contract, approved names/descriptions/rules, a deployed view, and full
  column-level lineage.
- You just ran the complete relay: **PO create → DE accept+discover → PO validate → DE
  materialize+deploy.** That's ~3 persona switches for one product.

_Speaker notes:_ Pause here. Ask learners to state which shell they're in right now (DE) and
what the next switch will be (back to PO to start product 2). If they can't, replay the relay
map from Slide 4.

---

## Slide 17 — **Repeat** · Build `compensation_payroll` (schema `hr_comp`)
**Beat:** do it again — same relay, second system of record, two more cryptic columns.

- Run the **entire relay again** for the second product, changing only:
  - **Wizard name** → `compensation_payroll`; **schema** → `hr_comp`.
  - **Cryptic columns to watch:** `amt → salary_amount` and `rsn → change_reason` on
    `salary_history` — confirm enrichment resolves both from the data.
- Persona sequence is identical: **PO** create → **Switch → DE** accept + discover/profile/
  enrich/standardize → **Switch → PO** clear the 5 tabs → **Switch → DE** materialize +
  deploy. (~3–4 more switches — that's the ~6 total for Module 3.)

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `sa-wizard-product2`
Caption: The wizard for the second product, `compensation_payroll` on `hr_comp`.
Must show: The `NewSourceProductWizard` name step reading `compensation_payroll` (and/or the
Select-Data-Source schema set to `hr_comp`).
Taken at: Second run of the relay, PO wizard + DE Select Data Source.

_Speaker notes:_ This is where learners *do it themselves* (especially if product 1 was
pre-seeded per Slide 6). Coach the switches, not the clicks — by now the mechanics should be
familiar; the persona hand-offs are what to reinforce.

---

## Slide 18 — Optional sidebar: Databricks as a discovery source
**Beat:** Postgres is the spine; Databricks is a nice-to-have, kept brief.

- DW discovers from **PostgreSQL, MySQL, Snowflake, Databricks, Oracle, SQL Server**. For
  this track, **Postgres is the spine** — everything above uses it.
- If a learner has a **free personal Databricks account**, the same relay works against a
  Databricks schema: just point **Select Data Source** at it instead of the HR Postgres
  Quick connect. Everything downstream (discover → validate → serve) is identical.
- Keep this optional — don't let a Databricks detour derail the core two-product build.

> **Concept callout — the source is pluggable, the flow isn't.** The persona relay and the
> stage pipeline are the same regardless of platform. That platform-independence is a
> first-class design property you'll see again in Module 5 (the transform DSL) and Module 7 (portable
> outputs).

---

## Slide 19 — Outcome — what "done" looks like
**Beat:** the concrete end-state for the whole module.

- **Two Source (blue-chip) products** live in the Marketplace: `workforce_core` and
  `compensation_payroll`.
- Each has: an ODCS contract, approved names + descriptions + rules, a **deployed queryable
  view**, and column-level lineage from source to product.
- Both cryptic-column resolutions held: `lvl → job_level`, `amt → salary_amount`,
  `rsn → change_reason`.

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `marketplace-source-both`
Caption: The Marketplace with the Source filter showing both products.
Must show: The Marketplace list filtered by the **Source** chip, with **both**
`workforce_core` and `compensation_payroll` cards visible, each showing a Source kind badge.
Taken at: Marketplace, Source filter selected, after both products are complete.

---

## Slide 20 — Validation — the check YOU run
**Beat:** prove success yourself, three ways, semantic-layer-free.

- **1. Marketplace filter:** the **All** chip lists both products; the **Source** chip shows
  exactly these two (no consumer products yet).
- **2. Preview returns real rows:** open each product → **Preview** tab → confirm sample rows
  from the deployed view render (real employees / salaries).
- **3. Serving shows the deployed view:** the **Serving** tab shows serving mode (virtual
  view) + connection details.
- **4. Per-product Q&A (not the domain chat):** on each product's **Q&A** tab, run a curated
  question and one free-form probe. **Use the per-product Q&A tab only** — the domain
  Semantic Q&A chat is out of scope this track (it degrades without the excluded semantic
  layer).

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `preview-rows`
Caption: The Preview tab returning real rows from the deployed view.
Must show: A product's **Preview** tab with a grid of actual sample rows (e.g. employee /
salary values) from the live view.
Taken at: Marketplace → product detail → Preview tab.

---

## Slide 21 — Troubleshooting the common snags
**Beat:** the three things that trip people up in the relay.

- **"The PO validation gate is greyed out."** The engineer didn't **Accept** the request
  (Slide 9). Accept it in Incoming, then reload the PO panel.
- **"Materialization stages are locked / show amber 'Pending PO validation'."** One of the
  five PO tabs isn't fully cleared. Re-open Validate and finish every tab.
- **"The README says run `psql` to deploy."** Ignore it — that note is **stale**. Use **Data
  Serving → Deploy View** in the UI (Slide 15).
- **"No Quick connect entry for the HR sample."** Re-run `./dwb connect` to rewrite the
  quick-connect manifest, then reopen Set Data Source.

_Speaker notes:_ Nearly every live-session hiccup is one of these four. The first two are the
persona-relay gates doing their job — reframe them as the model working, not breaking.

---

## Slide 22 — Next steps
**Beat:** close Pt 1, point at the aggregate.

- You now have **two source products** — the raw building blocks of the data mesh.
- **Next: Module 4 — Flagship Pt 2.** Build an **aggregate** (`monthly_headcount_cost`) that
  `:CONSUMES` **both** of these products, deploy it, and read the resulting **lineage** — and
  meet the two-source join gotcha (`:REFERENCES` never crosses product boundaries) as a
  feature, not a bug.
- If you're short on time, remember the Slide 6 mitigation: pre-seed / MCP-script the source
  products so Module 4 gets the spotlight.
