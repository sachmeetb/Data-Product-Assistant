# Module 0 — Track Orientation & the Two-Persona Model

- **Type:** concepts (no hands-on)
- **Target length:** ~11 slides
- **Prereq:** none
- **Latest-version pointer:** `[PLACEHOLDER: canonical-latest-version location — TBD]`
  *(carry on every deck until the open decision is resolved)*

> **House format note (read once) + Slide creator brief.**
> Every slide is `## Slide N — Title`, opens with a one-line **Beat** (the slide's job),
> then bullets. Concept ideas get a `> **Concept callout:**` block. Images are
> `[SCREENSHOT PLACEHOLDER — SIZE: X]` blocks with Name / Caption / Must show / Taken at.
> `_Speaker notes:_` is what the presenter says — **not shown on the slide**.
> This module is the **house-style exemplar** — all other modules match this shape.
>
> **This is Module 0 of 11** in the Data Workbench enablement track (Modules 0–10). It is a
> pure orientation module (no hands-on). The learner arrives with no prior DW experience.
>
> **Screenshot placeholders:** insert a **gray placeholder box** in the slide at the stated
> size — **Large** = full-width, ~50–60% of slide height (primary visual) · **Medium** =
> ~half the slide, image shares the slide with text · **Small** = ~quarter-slide, accent or
> confirmation shot. Do not omit placeholder boxes — they mark where live screenshots will
> be inserted before final deck production.

---

## Slide 1 — Data Workbench: the hands-on enablement track
**Beat:** set the frame — this is a build-it track, not a pitch.

- You're going to **stand Data Workbench (DW) up locally, build real data products
  end-to-end, and understand what happens under the hood.**
- **Every hands-on module drives the product through the web interface at
  `http://localhost:5173`** — that is your primary tool throughout the entire track.
- Baseline assumed: comfortable with Docker + the command line. New to WSL? That's covered
  (Module 1). No DW experience assumed.
- By the end you can operate the product as a practitioner — not describe it from slides.

_Speaker notes:_ The web interface is the primary tool for every module up to Module 6.
Module 6 introduces a second, optional path — driving DW headlessly over MCP from your own
CLI — but only after you've built fluency with the UI. Emphasise this up front so learners
aren't trying to set up Claude Code in Module 1. This is also deliberately different in tone
from the client-facing solutioning decks (we contrast them explicitly on Slide 10). Here
we're ramping *doers*.

---

## Slide 2 — What is Data Workbench?
**Beat:** the one-paragraph mental model everything else hangs off.

- A **web-based platform for data product engineering**. Projects are containers that hold
  named workflows (Discovery, Quality Assessment, Product Engineering, Migration, …); each
  workflow steps through a sequence of **stages**.
- Workflows are **human-driven, AI-supported**: at each stage the platform runs AI agents to
  draft output — column descriptions, quality rules, data mappings, serving SQL — and **you
  review, approve, or change what the agents produce.** You stay in control at every gate;
  the AI handles the toil.
- Every stage writes its results into a **Neo4j knowledge graph**, which becomes the source
  of truth for lineage, contracts, quality signals, and provenance across the full product
  lifecycle.
- The primary output is **governed data products** published to a **Marketplace** — each
  carrying an ODCS contract, lineage, quality score, and a servable view or materialized
  table.
- DW also connects to **external data platforms** to scan their estate, and can evaluate an
  existing data-source catalog against a portfolio of desired product templates — surfacing
  which products are ready to build, which need adapting, and what gaps remain.

> **Concept callout — "the graph is the source of truth."** Discovery, profiling, rules,
> the contract, mappings, and serving all *accrete onto the same knowledge graph*. That's
> why lineage and Q&A just work later — nothing is thrown away between stages.

---

## Slide 3 — Stop, clarify: two different SDKs
**Beat:** kill the #1 first-day misconception before it derails anyone.

- Two separate SDKs — and they are **not the same thing**:
  - **Claude Code** (the CLI, `claude`) — used by the development team to **build and
    extend** Data Workbench. It is a developer tool for the repo's authors.
  - **Claude Agent SDK** — the headless API that the **deployed application itself** uses
    to run pipeline stages. Every stage your local DW stack runs calls this at runtime.
- Practical consequence: **you do not need a Claude Code subscription to use the platform.**
  You only need the `ANTHROPIC_FOUNDRY_API_KEY` you'll set up in Module 2 — that's what the
  Agent SDK calls. (The converse also holds: because the Agent SDK *is* the Claude Code
  runtime, someone who already has a subscription can point the stack at that instead of a
  key — Module 2, Route 3. Handy if your key hasn't been provisioned yet.)
- **"Can I modify the prompts from the sidebar?"** — No. Skills are in the repo under
  `workbench-skills/` (Module 5). **"Does it need my personal Claude Max plan?"** — No —
  though it can *use* one if you'd rather not wait on a key.

> **Concept callout — "the switch was invisible."** The platform was originally developed
> using Claude Code for authoring, then switched to a proper Anthropic API endpoint via the
> Agent SDK for production — a transition that required no re-architecture. The gap between
> "how it was built" and "how it runs" is exactly what this slide closes.

_Speaker notes:_ Pre-empt both questions explicitly: "does it need my Claude Max plan?" and
"can I modify prompts from the sidebar?" Getting this wrong in someone's first week costs 20
minutes of confusion and one false mental model that keeps surfacing. Kill it here, before
Module 2's key-setup slide.

---

## Slide 4 — What you'll be able to do (track outcomes)
**Beat:** the promise, as verbs.

- **Run it locally** — one launcher, a healthy stack, sample data connected (Modules 1–2).
- **Build data products** — two source-aligned products + an aggregate, with lineage
  (Modules 3–4). This is the core prescriptive scenario.
- **Explain it** — skills, the knowledge graph, transformations (Module 5).
- **Automate it** — drive DW headlessly over MCP from your own AI CLI (Module 6).
- **Extend it** *(optional)* — after the core scenario, bring your own data source through
  to a published consumer product with no scaffolding (Module 9).

---

## Slide 5 — Two personas, two shells, one backend
**Beat:** the load-bearing distinction of the whole product.

- **Product Workbench** (`/product/*`) — the **Data Product Owner (PO)**: wizards, My
  Products, Ingest, Marketplace, the SA validation gate.
- **Engineering Workbench** (`/engineer/*`) — the **Data Engineer (DE)**: project list, the
  Incoming queue, project detail, the pipeline stages.
- **Same backend, same graph** — two persona-scoped views onto it. The header's **Switch**
  button jumps shell-to-shell; a **role selector** sets who you are.

> **Concept callout — why the split matters.** The PO *shapes and validates* products; the
> DE *builds and serves* them. Work handed between them at explicit gates. You'll feel this
> as **persona-switching** in the flagship (Modules 3/4) — that friction is the point, not
> a bug.

> **Concept callout — why exactly two sub-workbenches, not one per role.** The two-shell
> split was a deliberate design resolution to the question "how granular should persona views
> be?" — and the answer was *two*, not four. Within the engineering workbench, roles are not
> given separate interfaces; instead, tasks are statically mapped to roles, so a reviewer
> sees only tasks assigned to their role while engineering-specific tasks are hidden. Don't
> expect a separate Data Steward workbench or a separate Reviewer workbench — the single
> engineering shell covers both.

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `two-shells-switch`
Caption: The two workbenches and the Switch button.
Must show: The DW header with the shell name (Product vs Engineering) and the **Switch**
control visible; ideally two shots side by side (PO shell + DE shell) or one shot mid-switch.
Taken at: After launch (Module 2), signed in — one shot in each shell.

---

## Slide 6 — The vocabulary: source-aligned, aggregate, consumer
**Beat:** the data-mesh trichotomy you'll build examples of.

- **Source-aligned** (`source`) — a product profiled directly from a raw database. The
  building block. *(You build two of these in Module 3.)*
- **Aggregate** (`aggregate`) — a reusable product that **consumes** other products and
  combines them; defaults to a materialized serving. *(You build one in Module 4.)*
- **Consumer** (`consumer`) — a fit-for-purpose leaf product for a specific use. *(You build
  your own in Module 9 — the optional extension.)*

> **Concept callout — `productKind` is decoupled from archetype.** It's a late-binding flag
> (`source | aggregate | consumer`), not a project type. Aggregates and consumers both ride
> the same consumer-aligned machinery; they differ only by *intent*. A consumer can consume
> ANY published product, forming multi-hop chains (A→B→C).

> **Concept callout — data mesh vs data product.** Data mesh is the *organizational
> philosophy*; a data product is the *artifact*. A plain table is not a data product. A
> table with a contract, SLA, owner, quality score, and lineage is. DW helps you produce
> the second kind — the deliverable, not the philosophy.

> **Concept callout — ODCS timing is opposite for source vs consumer.** For source-aligned
> products, the ODCS spec is generated **after** data discovery — the product owner doesn't
> have enough information about the underlying data to write a spec upfront. For
> consumer-aligned products, the ODCS is specified **upfront** as a contract-first input to
> engineering. The order of the steps is *inverted* — you'll feel this as you go from Module
> 3 to Module 4, and it's the single clearest differentiator between the two wizard flows.

_Speaker notes:_ The wizard asks learners to describe a **use case** before picking inputs.
That description isn't just documentation — it's the agent's starting point for
backward-chaining to identify which data products are needed. "We need a monthly
headcount-cost metric" → the agent identifies which source products are required, checks
which already exist, and surfaces the gaps. The business question comes first; the product
topology follows.

---

## Slide 7 — Why Data Workbench (the value narrative)
**Beat:** the practitioner's "why bother", not the sales pitch.

- **End-to-end, one place** — discovery → profiling → enrichment → rules → contract →
  mapping → serving, without stitching five tools together.
- **Governed by construction** — every product carries a contract, quality signals, and
  lineage because the graph recorded each stage.
- **AI does the toil, you decide** — agents draft names, descriptions, rules, and mappings;
  you Approve / Replace / Escalate. You stay in control at every gate.
- **Portable outputs** — generated pipelines/packages run on their own, with no DW at
  runtime (you'll prove this in Module 7).

> **Concept callout — most real environments are brownfield.** The track runs a greenfield
> demo (no pre-existing products), but the platform is designed for brownfield: clients who
> already have partial data products, existing metadata, or specs to bring in. Three
> brownfield scenarios it's built for: (1) importing and enhancing an existing ODCS spec via
> the contract import path; (2) building a consumer product whose source-aligned dependencies
> are only partially built — the gap-surfacing surfaces what's missing; (3) starting from
> products that already exist in the graph from a prior session or team. On real engagements,
> scenarios (1)–(3) are the norm, not the exception.

---

## Slide 8 — How the track is sequenced
**Beat:** the road ahead, three arcs.

- **Set up (Modules 0–2):** orientation → WSL2 prep → install & launch locally.
- **Do (Modules 3–4):** the flagship — two source products, then an aggregate with lineage.
  This is the core prescriptive scenario every learner completes.
- **Understand & Extend (Modules 5–10):** under the hood → MCP → DQ/pipelines → advisory →
  bring-your-own-data *(optional extension)* → feedback & roadmap.

[SCREENSHOT PLACEHOLDER — SIZE: Large (authored diagram — not a product screenshot)]
Name: `track-roadmap`
Caption: The Module 0–10 module map (three arcs).
Must show: A simple roadmap graphic of the eleven modules grouped into Set up / Do /
Understand & Extend, with Module 9 labelled as optional. *(Author as a diagram in
production — excalidraw/svg-architect — not a product screenshot.)*
Taken at: N/A — authored graphic.

---

## Slide 9 — How to use this track
**Beat:** set expectations for the format — understand *what* before *how*.

- **The learning principle: understand what before how.** Learn the platform's *capabilities*
  at a high level first — enough to operate and demo it — before diving into the
  *implementation*. The flagship (Modules 3/4) comes before the under-the-hood module
  (Module 5) for exactly this reason.
- **Primary tool: the web interface.** Every lab drives the product through the web UI at
  `http://localhost:5173`. Module 6 introduces a second path — headless over MCP from your
  own CLI — but that comes *after* you've built fluency with the UI. Don't reach for the
  CLI path before Module 6.
- **Hands-on modules follow one shape:** Objectives → Prerequisites → Steps → Outcome →
  Validation → Next steps. Every hands-on module ends with a check *you* run.
- **Do it live.** The value is in your hands on the keyboard; screenshots show you what
  "right" looks like.
- **Persona-switching is a real beat** — we flag each Switch so you never lose track of
  which shell you're in.
- **Concept interludes** appear inline where you hit them and are collected in Module 5.

> **Concept callout — scope exclusion.** The **semantic layer** (Steward Concepts /
> business concepts) is **out of this initial track** — a future chapter. Consequence: for
> validation we use each product's **own Q&A tab**, not the domain-wide Semantic Q&A chat.

_Speaker notes:_ The "understand what before how" principle shapes the entire module
sequence. This is a deliberate choice: the platform is an accelerator with capabilities that
can be added, removed, or modified per client engagement. Learners who understand *what*
first can demo it and adapt it; learners who dive into *how* first often lose sight of the
bigger picture.

---

## Slide 10 — This track vs the solutioning decks
**Beat:** don't confuse the two bodies of material.

| | This enablement track | The solutioning / positioning decks |
|---|---|---|
| Audience | Innovation-team practitioners | Clients / stakeholders |
| Purpose | *Operate* the product end-to-end | *Explain and position* capabilities |
| Voice | Procedural, first-person-doer | Maturity-matrix, "menu not monolith" |
| Content | Hands-on exercises with a running stack | Capability overview, no exercises |
| Output | A working stack + real products you built | A decision to engage |

- The solutioning and positioning decks give clients an **overview of what DW can do** —
  they cover capability areas, comparisons, and value narrative. They are deliberately
  outward-facing and do **not** walk through hands-on exercises. You will not learn to
  *operate* DW from them.
- Use the solutioning decks as **reference for facts** when building a client narrative.
  Use **this track** to build the hands-on fluency that makes that narrative credible.
- **The two bodies are complementary, not interchangeable** — a practitioner needs both,
  for different conversations.

---

## Slide 11 — Where to get the latest version + self-check
**Beat:** close orientation; point to the canonical source; confirm understanding.

- **Latest version lives at:** `[PLACEHOLDER: canonical-latest-version location — TBD]`
  *(open decision — every deck carries this until it's set)*.
- **Self-check (answer before moving on):**
  1. Which shell does the **PO** work in, and which does the **DE** work in?
  2. Name the three `productKind` values and which one "consumes" others.
  3. Why does lineage "just work" later? *(hint: the graph)*
  4. Which validation surface do we use in this track, and which do we avoid — and why?
  5. What does "the Claude Agent SDK" do, and how is it different from Claude Code?
- **Next:** Module 1 — get a WSL2 environment ready to run DW.

_Speaker notes:_ If a learner can't answer #1 and #2, replay Slides 5–6 before Module 1. If
they can't answer #5, replay Slide 3 — the SDK misconception is load-bearing.
