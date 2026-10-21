# Module 9 — Self-Directed Extension: BYO Data → a Consumer-Aligned Product

- **Type:** hands-on project (no scaffolding — this is the capstone build)
- **Target length:** ~14 slides
- **Prereq:** Module 3–Module 5 (you've built source + aggregate products and understand skills / graph /
  transforms) and a running stack from Module 2
- **Latest-version pointer:** `[PLACEHOLDER: canonical-latest-version location — TBD]`
  *(carry on every deck until the open decision is resolved)*

> **Slide creator brief — Module 9 of 11 (optional extension).** House style is in **Module
> 0** — follow that format for every slide. The learner completed Modules 3–5 and can
> operate DW with no scaffolding. This is the **capstone build** — no guidance, no
> pre-staged data. The learner synthesizes their own small relational dataset (a worked
> example is provided: a `retail` schema with `customer` and `sales_order` tables), loads it
> into the local Docker Compose Postgres service, registers it as a DW source, builds a
> source-aligned product, then authors and deploys a consumer-aligned product on top of it.
> A consumer-aligned product uses the 10-step `NewProductWizard` (contract-first, PO authors
> the spec upfront) and must `:CONSUMES` an upstream product. The learner validates using the
> per-product **Preview** tab and the per-product **Q&A** tab (not the domain Semantic Q&A
> chat, which is out of scope for this track).
> **Screenshot placeholders:** insert a **gray placeholder box** at the stated size —
> **Large** = full-width, ~50–60% of slide height (primary visual) · **Medium** = ~half the
> slide, shared with text · **Small** = ~quarter-slide, accent or confirmation shot.
> Do not omit placeholder boxes — they mark where live screenshots are inserted later.

---

## Slide 1 — Bring your own data: the guide rail comes off
**Beat:** set the frame — this is the exam, not another guided lab.

- In Module 3/Module 4 we told you which tables, which columns, which joins. **Here you decide all of it.**
- You'll **synthesize your own small dataset, load it into the local Postgres, publish a
  source-aligned product from it, then build a consumer-aligned product on top** — and validate
  it yourself.
- Every move in this module is one **you've already done** in Module 2–Module 5. What's new is that
  **nobody is telling you the answers.**

_Speaker notes:_ Frame this as graduation. If a learner can drive this cold, they can operate
DW as a practitioner. Resist the urge to hand-hold — point them back at the earlier modules.

---

## Slide 2 — What you'll be able to do (objectives)
**Beat:** the promise, as verbs.

- **Synthesize** a small relational dataset (one table up to a couple of related tables) and
  **load** it into the local Postgres with the `docker … psql` pattern.
- **Publish** a source-aligned product from your data, then **author** a consumer-aligned
  product (`dpe-cf`, `productKind = consumer`) that consumes it.
- **Validate** it yourself — Preview rows + the **per-product Q&A tab** on *your own* product.

---

## Slide 3 — Where you should be standing (prerequisites)
**Beat:** confirm you're at the start line before you begin.

- **Stack up:** `./dwb status` is green; the Postgres profile is running
  (`./dwb up --with postgres`). Backend healthy at `:8000`, UI at `:5173`.
- **Skills in hand:** Module 3 (source products), Module 4 (consumer/aggregate machinery + lineage), Module 5
  (skills / graph / transforms). You will *reuse* these with **no scaffolding**.
- **Mindset:** you've done every one of these moves before — now there's no guide rail.

> **Concept callout — a consumer product must consume something.** A consumer-aligned product
> doesn't read a raw database directly; it `:CONSUMES` an upstream **product**. So you'll do
> this in two passes: **(1)** publish a quick *source-aligned* product from your data, then
> **(2)** build the *consumer* product on top of it. Same source→consumer split you lived in
> Module 3→Module 4 — now on data you invented.

> **Concept callout — connecting to a client's existing stack ("Lego" approach).** The
> connectors in this module are pluggable and removable per client environment — codebases
> stay separate but interoperable, sharing ideas and components rather than merging. When a
> client asks "can this work with our existing catalog / data warehouse / git provider?", the
> honest answer is: yes, the connectors and output targets are designed to be substituted per
> environment. The local stack you're using here (Postgres, Gitea) is one configuration;
> replacing either with a client's own system is a settings change, not a fork. Practise
> answering "what about our platform?" with a specific, confident answer about what's
> pluggable — that pre-empts the most common client objection.

---

## Slide 4 — Design a consumer-shaped dataset
**Beat:** pick data that has a real consumer story, not just rows.

- **The worked example — a tiny retail domain**, schema `retail`, two related tables:
  - `customer` (id, name, email, country, signup_date, status)
  - `sales_order` (id, **customer_id → customer**, order_date, amount, status)
- **Why two tables with an FK:** the `customer ↔ sales_order` join lives **inside one source
  product**, so it's auto-discovered — you deliberately sidestep the cross-product "no FK path"
  gotcha from Module 4 to keep this build clean.
- **The consumer story:** *"Sales wants one row per active customer — total spend, order count,
  last order — with PII kept safe."* That fit-for-purpose leaf is exactly a `productKind = consumer`.

> **Concept callout — source vs consumer, restated on your data.** The source product
> (`retail_core`) is the faithful *raw material*. The consumer product
> (`active_customer_spend`) is the *tailored delivery* — you declare what Sales needs and let
> the engineer implement it. Same distinction you built across Module 3→Module 4.

---

## Slide 5 — Synthesize the SQL
**Beat:** write a small, deterministic schema + seed — no `random()`, so reloads are repeatable.

- Keep it tiny (a dozen rows) and make the consumer story visible in the data — some `churned`
  customers to filter out, a `refunded` order to exclude from spend.

```sql
CREATE SCHEMA retail;

CREATE TABLE retail.customer (
  customer_id  int  PRIMARY KEY,
  full_name    text NOT NULL,
  email        text,
  country      text,
  signup_date  date,
  status       text            -- 'active' | 'churned'
);

CREATE TABLE retail.sales_order (
  order_id     int  PRIMARY KEY,
  customer_id  int  REFERENCES retail.customer(customer_id),
  order_date   date,
  amount       numeric(10,2),
  status       text            -- 'paid' | 'refunded' | 'pending'
);

INSERT INTO retail.customer VALUES
  (1,'Ada Lovelace','ada@example.com','UK','2023-01-15','active'),
  (2,'Grace Hopper','grace@example.com','US','2023-03-02','active'),
  (3,'Alan Turing','alan@example.com','UK','2022-11-20','churned'),
  (4,'Katherine Johnson','kj@example.com','US','2024-02-10','active');

INSERT INTO retail.sales_order VALUES
  (101,1,'2024-05-01',120.00,'paid'),
  (102,1,'2024-06-14', 80.00,'paid'),
  (103,2,'2024-06-20',250.00,'paid'),
  (104,2,'2024-07-01', 40.00,'refunded'),
  (105,3,'2024-01-05', 60.00,'paid'),
  (106,4,'2024-07-15',300.00,'paid');
```

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `m9-synth-sql`
Caption: The learner's synthesized dataset — two related tables in `retail`.
Must show: The `retail.sql` file open in an editor with both `CREATE TABLE` statements and the
seed `INSERT`s visible.
Taken at: After writing the file (before loading).

---

## Slide 6 — Load it into the local Postgres
**Beat:** teach the `docker … psql` load pattern from the HR sample, pointed at the compose Postgres.

- Your stack already runs a Postgres service (from `--with postgres`). Create a database, then
  **pipe your file in over stdin** — the same move `samples/hr` uses, aimed at the compose service:

```bash
# create a throwaway database on the running compose Postgres
docker compose exec -T postgres psql -U workbench -d postgres -c 'CREATE DATABASE retail_demo;'

# load your schema + seed into it (stdin pipe, like samples/hr's docker exec … < file)
docker compose exec -T postgres psql -U workbench -d retail_demo < samples/retail/retail.sql

# verify it landed
docker compose exec -T postgres psql -U workbench -d retail_demo -c '\dt retail.*'
```

- Compose Postgres credentials are fixed: user `workbench`, password `workbenchpass`, maintenance
  DB `postgres` (host port `5433` → container `5432`).

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `m9-load-postgres`
Caption: Loading the synthesized data and verifying the tables exist.
Must show: The `docker compose exec … psql` load command and the `\dt retail.*` output listing
`customer` and `sales_order`.
Taken at: After running the load + verify commands.

---

## Slide 7 — Register your data as a source (a new connection)
**Beat:** connect DW to your own DB — this one isn't a sample, so you register it by hand.

- **PO** — create a source-aligned project with `NewSourceProductWizard` (idea + domain + name,
  e.g. `retail_core`).
- **Switch → DE** — accept the request (hard gate), open **Select Data Source → Set Data
  Source**. Your DB isn't in Quick-connect (that only lists loaded samples), so fill the form:
  - **host** `postgres` (the compose **service name** — the same way the sample sources are
    wired), **port** `5432`, **database** `retail_demo`, **user** `workbench`, **password**
    `workbenchpass`, **schema** `retail`.
  - *(Host-side DB instead of the compose one? Use `host.docker.internal` and the published port
    `5433`.)*

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `m9-new-source`
Caption: A brand-new source connection registered against the learner's `retail_demo`.
Must show: The Set Data Source form filled with host `postgres`, port `5432`, database
`retail_demo`, schema `retail` — saved / stage marked complete.
Taken at: After saving the connection on the source project.

---

## Slide 8 — Discover, profile, and publish the source product
**Beat:** run the pipeline you know cold — on data you made — with nobody guiding you.

- **DE** — **Data Discovery → Data Profiling → Metadata Enrichment → Column-Name
  Standardization → Mark Discovery Complete.** Watch the agent describe your columns from the
  profiled sample.
- **Switch → PO** — clear the **5-tab validation gate** (Names / Descriptions / Tables /
  Relationships / Rules).
- **Switch → DE** — **Synthesize Contract → Import → Auto-Map → Serve (virtual view) → Deploy →
  Mark Engineering Complete.** `retail_core` is now a **Source** product in the Marketplace.
- This is **Module 3 with the scaffolding removed** — every step is one you've run before.

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `m9-discovery-running`
Caption: Data Discovery streaming live against the learner's own tables.
Must show: The Discovery stage running with streamed agent output referencing `retail.customer`
/ `retail.sales_order` (or the profiling summary cards populated for them).
Taken at: During/after Discovery + Profiling on the source project.

---

## Slide 9 — Author the consumer product (the 10-step wizard)
**Beat:** now shape *what Sales needs*, top-down — the Module 4 machinery on your data.

- **PO** — **Product Workbench → New Product → Consumer-aligned product.** Walk the wizard:
  - **Describe & Domain** — `active_customer_spend`.
  - **Shape** — grain = *one row per active customer*; **filter** = `status = 'active'`.
  - **Shape the Schema** — mark `customer_id` a **Group by** key; add `full_name`, `country`;
    **Derive** the aggregates `total_spend` (SUM of paid `amount`), `order_count` (COUNT),
    `last_order_date` (MAX); **mask** or **suppress** `email`.
  - **Product Details** — declare **`productKind = consumer`** (a fit-for-purpose leaf).
  - **Rule Coach** — approve a rule or two (e.g. `total_spend ≥ 0`).

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `m9-consumer-wizard`
Caption: The consumer product taking shape in the New Product wizard.
Must show: The Shape-the-Schema (or Product Details) step with the group-by key + derived
columns visible and `productKind = consumer` selected.
Taken at: Mid-wizard, before the source-binding gate.

> **Concept callout — consumer ≠ aggregate.** Both ride the same `dpe-cf` machinery, but a
> **consumer is a leaf** and does **not** default to a materialized serving (that default is the
> *aggregate*). A virtual `GROUP BY` view is perfectly fine here.

---

## Slide 10 — Confirm sources + the pre-flight gap check
**Beat:** the required `:CONSUMES` gate — bind the consumer to your source product.

- **Step 9 — Confirm candidate sources:** bind to **`retail_core`**. This records the
  `:CONSUMES` edge in the graph (the same relationship you saw power lineage in Module 4).
- **Run the Pre-flight gap check:** every schema column is flagged **covered / derivable /
  ambiguous / gap**. Fix any gaps (adjust the schema or the source) or acknowledge them, then
  **Submit** — the request lands in the engineer's Incoming queue.
- Because you consume a **single** source product, the `customer ↔ sales_order` join is
  **intra-product and auto-discovered** — no "no FK path" error to resolve here.

---

## Slide 11 — Switch → DE: map, serve, deploy
**Beat:** implement and ship it — the Module 4 engineering moves, unscaffolded.

- **DE** — **Accept** from Incoming → **Import contract** → **Mapping & Transformation** (the
  agent maps each product column to `retail_core`'s columns; **Approve / Replace / Escalate** in
  the Mappings review) → **Configure Serving** (**Virtual view**) → **Data Serving** (generate
  DDL) → **Deploy View** → *(Deployment Reflection)* → **Mark Engineering Complete.**

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `m9-mapping-serve`
Caption: Reviewing the AI mappings before serving the consumer product.
Must show: The Mappings review queue with the derived aggregate columns mapped to
`retail_core` sources (or the Configure-Serving step set to Virtual view).
Taken at: During the DE integration pipeline for the consumer product.

---

## Slide 12 — Outcome: your product is live
**Beat:** what "done" looks like.

- `active_customer_spend` appears in the **Marketplace** with a **Consumer** chip.
- On **Overview**, `consumes = [retail_core]`; open `retail_core` and its
  `consumed_by = [active_customer_spend]` — your two-product chain, built end-to-end from data
  that didn't exist an hour ago.

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `m9-marketplace-live`
Caption: The learner's consumer product live in the Marketplace.
Must show: The Marketplace card for `active_customer_spend` with the Consumer chip, and the
Overview `consumes = [retail_core]` cross-reference.
Taken at: After Mark Engineering Complete, in the Marketplace.

---

## Slide 13 — Validation you run yourself
**Beat:** prove it — semantic-layer-free, on your own product.

- **Preview tab** → real rows from your deployed view: one row per **active** customer
  (Ada, Grace, Katherine — churned Alan excluded), spend totals that skip the refunded order.
- **Q&A tab** → the **per-product** Q&A: try the curated questions, then a free-form probe
  (e.g. *"which customer spent the most?"*).
- **Use this tab, not the domain Semantic Q&A chat** — the semantic layer is out of scope for
  this track, so the per-product Q&A is the surface we validate on.

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `m9-preview-qa`
Caption: Preview rows + the per-product Q&A tab confirming the product answers real questions.
Must show: The Preview tab returning aggregated rows for the active customers **and** the
per-product Q&A tab answering a free-form probe about the learner's data.
Taken at: On the `active_customer_spend` product detail, after deploy.

---

## Slide 14 — You graduated — now close the loop
**Beat:** wrap the build; point at the capstone.

- If you drove this **cold** — synthesize, load, register, discover, author, deploy, validate —
  you can operate Data Workbench as a practitioner. That was the whole track's promise.
- **Reflect before you move on:** Where did you hesitate? Which step felt harder than Module 3/Module 4 said
  it would? What would you change about the product? *(Hold that thought — it's the raw material
  for the next module.)*
- **Next:** Module 10 — the **feedback loop, roadmap, and what's deliberately deferred**. Your friction
  is the DW team's roadmap input.

_Speaker notes:_ Encourage learners to keep the retail project around — Module 10's reflection prompts
land better with a fresh build to point at.
