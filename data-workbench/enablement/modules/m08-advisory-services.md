# Module 8 — Advisory Services for DE & PO

- **Type:** concepts + demo
- **Target length:** ~11 slides
- **Prereq:** Module 5 (skills / knowledge graph / transforms) — the Ask panel reasons over the graph
  you learned to read there
- **Latest-version pointer:** `[PLACEHOLDER: canonical-latest-version location — TBD]`
  *(carry on every deck until the open decision is resolved)*

> **Slide creator brief — Module 8 of 11.** House style is in **Module 0** — follow that
> format for every slide. The learner understands the graph model (Module 5). Data Workbench
> has two in-app coaching surfaces: the **DE "Ask" panel** (a collapsible right-hand drawer
> in the Engineering Workbench, backed by a project-scoped read-only AI agent that issues
> Cypher queries against the knowledge graph before making any factual claim) and the **PO
> "Guide me" panel** (in the Product Workbench wizard, emits structured `suggestion` blocks
> the PO can Apply with a button click). Both are advisory and read-only with respect to the
> graph. This module also contains a critical **HONESTY FLAG slide**: a per-run learning
> coach described in `docs/advisor-models.md` is **proposed, not shipped** — it must never
> be presented as a live feature.
> **Screenshot placeholders:** insert a **gray placeholder box** at the stated size —
> **Large** = full-width, ~50–60% of slide height (primary visual) · **Medium** = ~half the
> slide, shared with text · **Small** = ~quarter-slide, accent or confirmation shot.
> Do not omit placeholder boxes — they mark where live screenshots are inserted later.

> **House format note.** This module follows the Module 0 exemplar shape: `## Slide N — Title`, a
> one-line **Beat**, bullets, `> **Concept callout:**` for ideas, `[SCREENSHOT PLACEHOLDER — SIZE: X]`
> slots with Name / Caption / Must show / Taken at, and `_Speaker notes:_` for the presenter.
> Concept module with a short demo tail — the payoff is a graph-backed answer and one applied
> suggestion.

---

## Slide 1 — Two coaching surfaces, one honest scope
**Beat:** set the frame — DW has in-app assistants, and knowing their *limits* matters as much as using them.

- DW ships **two persona-scoped coaching surfaces:** the **DE "Ask" panel** (helps the engineer
  understand the project graph) and the **PO "Guide me" panel** (helps the owner author a
  product in the wizard).
- Both are **advisory** — they reason and suggest; **you** stay in control. Neither one silently
  changes your data.
- The most important thing you'll leave with: **what each surface can and cannot do**, and a
  standing instruction to **verify a surface functionally before you demo it** — because at least
  one much-discussed advisor is *proposed, not shipped* (Slide 8).

> **Concept callout — why a practitioner cares.** These panels are where "AI does the toil, you
> decide" (Module 0 Slide 6) becomes tactile. But an enablement presenter who demos a surface that
> isn't wired will lose the room — so scope-honesty is a working skill here, not a footnote.

_Speaker notes:_ Semantic-layer Q&A (the domain-wide marketplace chat) is **out of scope** for
this track — don't reach for it. The DE Ask panel here is graph/Cypher-backed, which is fine.

---

## Slide 2 — Objectives & starting point
**Beat:** what you'll be able to do, and where you must be to start.

- **Objectives — after this module you can:**
  1. Use the **DE Ask panel** to get a **graph-backed** answer about a project.
  2. Use the **PO Guide me panel** to get a suggestion and **Apply** it into the wizard.
  3. State each surface's **scope and read-only-ness**, and the Apply protocol in one sentence.
  4. Correctly say which advisor is **not shipped** and why you'd verify before demoing.
- **Prerequisites / starting point:**
  - A project you built in Module 3/Module 4 with real graph state (descriptions, rules, scores) — so the
    Ask panel has something to reason over.
  - The PO wizard open on any in-flight product (or start a fresh one) for the Guide-me demo.

---

## Slide 3 — The DE "Ask" panel: project-scoped, read-only, graph-backed
**Beat:** the engineer's assistant — what it is and the three properties that define it.

- It's a collapsible **right-hand drawer** in the Engineering Workbench, backed by the
  **`project-chat-assistant`** skill over the Claude Agent SDK (WebSocket
  `/ws/chat/{project_id}`).
- **Three defining properties:**
  - **Project-scoped** — every graph query is forced to this project's `projectCode`; a query
    that could match other projects is refused (isolation at the `run_cypher.py` choke point).
  - **Read-only** — it never issues `CREATE`/`MERGE`/`DELETE`/`SET`, never approves reviews,
    never triggers stages. If you ask it to *do* something, it tells you which UI page does it.
  - **Evidence-led** — it must run a Cypher query before making any factual claim about your
    data, and cite the count / batch id / column URI that backs the claim.
- It reasons across the **whole project graph** — catalog, profiling, rules, descriptions,
  mappings, tests, scores, contracts — and can **trace lineage** (source column → product column).

> **Concept callout — a narrow tool surface on purpose.** The Ask agent only gets
> `Read / Bash / Grep / Glob / Skill`, and its one real move is a **scoped** `run_cypher.py`.
> That narrowness is what makes it safe to point at a live project graph.

---

## Slide 4 — How "Ask" answers a question (and where it stops)
**Beat:** the mechanics — so you can trust the answer and explain a refusal.

- The turn's system prompt hands the agent the **Neo4j credentials verbatim** plus a pre-filled
  `run_cypher.py` invocation — so it never hunts for secrets (a `## Forbidden` section bans
  reading `.env`/`workbench.db`, grepping for passwords, or brute-forcing).
- It leads with the answer, then the evidence, then optionally a **concrete next action** and a
  follow-up you might ask. For scores it knows the rubric: it filters to the **latest `batchId`**
  (scores are append-only) and reads the dimension's evidence.
- When the graph doesn't hold a fact, it **says so** ("no `:ColumnDescription` nodes exist yet —
  run Metadata Enrichment") rather than inventing one.

> **Concept callout — "next-step context."** The DE tooling exposes a `get_plan_summary` capability
> that computes the recommended next action / blockers for a project (it's a first-class **MCP
> tool** on the `/mcp` front door, used by the request guard). `[VERIFY: whether the *interactive*
> Ask drawer calls get_plan_summary directly — the chat runner's system prompt instructs the agent
> to "propose a concrete next action when relevant" but does not itself invoke get_plan_summary;
> confirm the wiring before claiming it in-panel.]`

_Speaker notes:_ This is the one place to be precise: `get_plan_summary` is real and it's how DW
computes next steps, but I could not confirm the Ask *drawer* pulls it. Present next-step help as
"the panel proposes a next action"; attribute `get_plan_summary` to MCP.

---

## Slide 5 — The PO "Guide me" panel: wizard-scoped, suggestion-emitting
**Beat:** the owner's co-author — different persona, different shape.

- In the Product Workbench, the PO clicks **Guide me** next to a wizard field (idea, name,
  dataset name, description, purpose, schema, rules, shape). The client pre-fills a contextual
  prompt; the **`product-authoring-assistant`** skill reads the wizard state and responds.
- It's **wizard-scoped**, not project-scoped: it works off the wizard state handed to it in the
  turn (`idea`, `domain`, `selected_columns`, `custom_columns`, `name`, …) — it even works
  **before a project exists** (session keyed to the owner, `/ws/product-chat/{owner_email}`).
- It **calls specialist sub-advisors** for focused work — e.g. `data-product-name-advisor`
  (names), `data-product-schema-advisor` (columns), `data-product-osi-advisor`
  (AI-readiness cards on the Readiness step), `data-product-question-analyzer`,
  `serving-strategy-advisor`.
- It is **read-only with respect to the graph** — its only effect is the message + suggestion it
  sends back for *you* to apply.

> **Concept callout — pre-project by design.** Because Guide-me rides the owner's own chat
> session (not a project), a PO can shape an idea into a scoped request *before* engineering ever
> sees it — which is exactly the contract-first ordering Module 0 described.

---

## Slide 6 — The Apply protocol: suggestions you click to accept
**Beat:** how a suggestion becomes a change — the PO always makes the final move.

- When Guide-me has a concrete value you could drop in, it ends its reply with a hidden
  ` ```suggestion ` JSON block tagged with an `applies_to` field. The wizard renders it as an
  **Apply card** — you see a clean button, not the JSON.
- `applies_to` values include: `idea` / `name` / `dataset_name` / `description` / `purpose`,
  `schema_add_columns` / `schema_pick_columns`, `rule_decisions` / `rule_create`,
  `column_transform_set`, `shape_set`, and the OSI cards (`osi_metric_create`, …).
- **Apply is async and reversible in spirit:** "Applying…" → "Applied" / "Failed"; the value only
  lands in wizard state when you click. Nothing is written to the graph behind your back — a
  `rule_create`, for instance, only persists when the wizard mints the rule server-side on Apply.

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `guide-me-apply-suggestion`
Caption: The PO Guide-me panel emitting an Apply card next to a wizard field.
Must show: The wizard open with the Guide-me drawer showing an answer plus an **Apply** card (a
name / description / schema suggestion), with the target field visible.
Taken at: In `NewProductWizard` / `NewSourceProductWizard`, after clicking Guide me on a field.

> **Concept callout — the exact `applies_to` string is load-bearing.** The wizard silently
> discards a block whose tag it doesn't recognize (`shape_set`, not `shape`;
> `schema_add_columns`, not `add_columns`). Good to know if a suggestion "doesn't show up."

---

## Slide 7 — Two personas, two scopes (side by side)
**Beat:** consolidate — one table you can put on a wall.

| | DE "Ask" panel | PO "Guide me" panel |
|---|---|---|
| Shell | Engineering Workbench | Product Workbench |
| Skill | `project-chat-assistant` | `product-authoring-assistant` (+ sub-advisors) |
| Scope | One **project's graph** (scoped Cypher) | The **wizard state** (works pre-project) |
| Reads | The whole project knowledge graph; traces lineage | Wizard fields + domain catalogs + attachments |
| Writes | **Nothing** (read-only) | **Nothing** to the graph — emits Apply suggestions you accept |
| You act by | Reading the evidence-backed answer | Clicking **Apply** on a suggestion card |

- Both are advisory. Neither replaces a gate: the DE still runs stages in the pipeline; the PO
  still submits/validates in the wizard.

---

## Slide 8 — HONESTY FLAG: the per-run learning coach is NOT shipped
**Beat:** the scope-honesty slide — say clearly what exists only on paper.

- `docs/advisor-models.md` describes an **"advisor model"**: a coach step that, before each
  pipeline run, reads a domain's correction history and injects a few run-specific tips into the
  stage prompt (an `{advice}` channel in `build_prompt()` + a `mapping-advisor` skill, starting
  at the data-mapping stage).
- **This is a proposal, not a feature.** The doc's own status line reads *"decision pending"*; it
  lays out a staged plan (**Phase 0** measure baselines → Phase 1 pilot → Phase 2 advice store →
  Phase 3 train a model) whose **Phase 0 hasn't been green-lit**. There is **no shipped per-run
  learning coach** in DW today.
- **Do NOT present it as live.** If you mention it at all, frame it as *"a proposed direction
  under evaluation,"* and never demo it.

> **Concept callout — presenter's standing order.** **Verify each advisory surface *functionally*
> before you demo it.** Open the Ask drawer and get a real graph-backed answer; open Guide-me and
> apply a real card. If a surface (like the per-run coach) can't be exercised live, it doesn't go
> in the live demo — it goes on a roadmap slide (Module 10).

_Speaker notes:_ The two surfaces on Slides 3–6 ARE shipped and demoable. The advisor *model* on
this slide is NOT. That contrast is the point of the module — coaching that exists vs coaching
that's proposed.

---

## Slide 9 — Demo/validation 1: a graph-backed answer from Ask
**Beat:** prove the DE surface — ask a real question, get real evidence.

- **Do it live:**
  1. In the Engineering Workbench, open a project from Module 3/Module 4 and open the **Ask** drawer.
  2. Ask: **"why is my documentation score low?"**
  3. Watch it run a scoped `run_cypher.py`, pull the **latest `batchId`**, and answer with the
     documentation dimension's evidence — e.g. "N columns lack an approved `:ColumnDescription`"
     — **citing the count/URIs**.
- **Validation (the green check):** the answer names specific graph facts (a count, a batch id,
  column names) — not a generic essay. If it cites evidence, the surface is genuinely
  graph-backed.

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `ask-panel-graph-backed`
Caption: The DE Ask panel answering "why is my documentation score low?" with graph evidence.
Must show: The Ask drawer with the question and an answer that cites concrete numbers/URIs (e.g.
"X of Y columns have no approved description; latest batch <id>").
Taken at: Engineering Workbench → project → Ask drawer, on a scored project.

> **Concept callout — you can grade the answer.** Because it cites evidence, you can spot-check it
> in the Neo4j browser (`:7475`). An assistant you can *audit* is one you can *trust* on a client
> engagement.

---

## Slide 10 — Demo/validation 2: apply a Guide-me suggestion
**Beat:** prove the PO surface — get a suggestion and land it in the wizard.

- **Do it live:**
  1. In the Product Workbench, open the product wizard and click **Guide me** on a field — e.g.
     **description** (it produces a consumer-facing and a governance-facing candidate) or **name**
     (it invokes `data-product-name-advisor`).
  2. Read the options; find the **Apply** card for its top pick.
  3. Click **Apply** — watch "Applying…" → "Applied" and confirm the wizard field now holds the
     suggested value.
- **Validation (the green check):** the wizard field visibly changed to the applied value, and the
  panel reported "Applied." You made the decision; the assistant only offered it.

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `guide-me-applied`
Caption: A Guide-me suggestion applied — the wizard field now shows the accepted value.
Must show: Before/after (or an "Applied" card + the populated field) so it's clear the click
changed the field.
Taken at: Immediately after clicking Apply in the wizard's Guide-me drawer.

---

## Slide 11 — Self-check + next steps
**Beat:** close the module; confirm the ideas; hand off to Module 9.

- **Self-check (answer before moving on):**
  1. Which panel is **project-scoped** and which is **wizard-scoped** — and which can run *before*
     a project exists?
  2. Are either of these panels allowed to write to the graph? How does each surface its effect?
  3. What must the Ask panel do before it states a fact about your data?
  4. Is the **per-run learning coach** from `advisor-models.md` shipped? What's the one-line honest
     framing?
  5. What's the presenter's standing order before demoing any advisory surface?
- **Latest version lives at:** `[PLACEHOLDER: canonical-latest-version location — TBD]`.
- **Next:** **Module 9 — Self-Directed Extension: Bring Your Own Data → a Consumer-Aligned Product.**
  You'll take everything from Module 2–Module 5 (and lean on these panels as you go) to run your *own* sample
  data through to a published consumer product with no scaffolding.

_Speaker notes:_ If a learner treats the panels as authorities rather than advisors, replay
Slides 3 + 5 (read-only) and Slide 8 (scope honesty) before Module 9.
