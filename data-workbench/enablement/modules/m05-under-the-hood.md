# Module 5 — Under the Hood: Skills, the Knowledge Graph, Transformations

- **Type:** concepts (no hands-on)
- **Target length:** ~17 slides
- **Prereq:** Module 3–Module 4 (you need the products you actually built as the concrete referent)
- **Latest-version pointer:** `[PLACEHOLDER: canonical-latest-version location — TBD]`
  *(carry on every deck until the open decision is resolved)*

> **Slide creator brief — Module 5 of 11.** House style is in **Module 0** — follow that
> format for every slide. The learner has completed the flagship build (Modules 3–4) and
> now needs to understand how it worked. This is a **pure concepts module — no hands-on
> build**. It opens the three machines behind the flagship: **(a) agent skills** (self-
> contained versioned instruction sets in `workbench-skills/skills/`, vendored in the repo);
> **(b) the Neo4j knowledge graph** (Neo4j LPG running locally, browser at `:7475`, adapting
> six ontology standards: DCAT-2, DQV, SHACL, PROV-O, DPROD, ODCS); and **(c) the
> platform-independent transform DSL** (a neutral recipe compiled to per-platform SQL by a
> dialect emitter with fail-closed capability checking). Two slides use authored diagrams
> (not product screenshots) — see SIZE notes on those slots.
> **Screenshot placeholders:** insert a **gray placeholder box** at the stated size —
> **Large** = full-width, ~50–60% of slide height (primary visual) · **Medium** = ~half the
> slide, shared with text · **Small** = ~quarter-slide, accent or confirmation shot.
> Do not omit placeholder boxes — they mark where live screenshots are inserted later.

> **Module note (read once).** This is a **concepts** module — no new hands-on build. Its job
> is to explain *how* the flagship you ran in Module 3/Module 4 actually worked. Every idea is tied back to
> something you already did ("remember when you ran Discovery in Module 3? here's what happened"). We
> follow the house slide format from Module 0 (Beat → bullets → `> Concept callout` →
> `[SCREENSHOT PLACEHOLDER — SIZE: X]` slots). It closes with a self-check you run in the real UI,
> not a command lab.

---

## Slide 1 — Under the hood: what actually happened in Module 3/Module 4
**Beat:** set the frame — you built it; now you'll understand it.

- In Module 3 you profiled two raw HR databases into **source-aligned products**; in Module 4 you built
  an **aggregate** that consumed both and saw lineage. It *felt* like magic. It isn't.
- Three machines did that work, and this module opens all three:
  **(a) agent skills** — the units of AI work each stage runs;
  **(b) the Neo4j knowledge graph** — where everything you approved was written down;
  **(c) transformations & mappings** — how "first name + last name" became real SQL.
- Goal by the end: you can **explain the flagship to a colleague** — no slides needed.

_Speaker notes:_ Keep pointing back at Module 3/Module 4. The whole reason Module 5 sits *after* the flagship is
that concepts land far better with a referent the learner built themselves. Frame the skills as
the real IP: the workbench is roughly one month old as a UI, but the agent skills underneath it
are the core of what a team takes away — roughly half the skill library is directly applicable
to data-workbench pipelines, the rest to adjacent capabilities (migration, modernization) that
share the same library. Skills are platform-specific but designed to be copied and adapted: a
Postgres-oriented skill can be adapted for Snowflake or Azure SQL rather than rewritten from
scratch. The UI is the demonstration surface; the skills are what a team reuses.

---

## Slide 2 — Three moving parts, one loop
**Beat:** the mental model everything else hangs off.

- **Skills do the work.** Each pipeline stage (Discovery, Profiling, Enrichment, Mapping,
  Serving) launches an **AI agent running a skill** — instructions plus helper scripts.
- **The graph remembers the work.** Every result a skill produces — and every value you
  Approved at a gate — is written into the **same Neo4j knowledge graph**.
- **Transforms describe the work.** When you serve a product, the mapping recipe stored in the
  graph is compiled into SQL for whatever platform the product lands on.

> **Concept callout — "the graph is the source of truth."** (You met this in Module 0.) Skills read
> from the graph and write back to it; nothing is thrown away between stages. That single fact
> is *why* lineage, Q&A, and portable serving all work later — they're just reading an
> accreted graph.

---

## Slide 3 — Part A · What an agent skill is
**Beat:** define the unit of AI work you ran five times in Module 3.

- A **skill** is a self-contained, versioned unit of agent instruction. Each is a directory:
  - **`SKILL.md`** — the natural-language instructions the agent reads (purpose, triggers,
    the exact steps, the output contract).
  - optional **`scripts/`** — deterministic Python helpers the agent runs (DB queries, Neo4j
    loaders, file writers) so the *reasoning* stays in the model and the *plumbing* stays in code.
- Skills are **vendored in-repo** at `workbench-skills/skills/<skill-name>/` (the
  `workbench-skills/` plugin) — version-controlled and baked into the backend image, not
  downloaded at runtime.
- **69 skills** as of 2026-08-25. *(This count moves — re-run
  `ls workbench-skills/skills/ | wc -l` at authoring time and cite the date.)*

> **Concept callout — skill lazy-loading: front matter first, full file second.** The SDK
> reads only the **front matter** (name + description) from each skill's markdown file
> initially, feeds that list to the model, which decides which skill(s) are needed, then
> only loads the **full markdown file** for the selected skill(s). This is both a performance
> and a correctness design. More information in context means more room for the model to
> hallucinate — the simpler and more selective the context, the more deterministic the
> agent's output. The tool allow-list per run-shape (Slide 6) enforces the same principle:
> the model gets exactly the tools it needs for the run-shape, no more.

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `skill-md-structure`
Caption: A skill is just `SKILL.md` + `scripts/`.
Must show: A file-tree (or editor) view of one skill directory under
`workbench-skills/skills/` — e.g. `data-discovery/` — with `SKILL.md` and the `scripts/`
folder visible, and `SKILL.md` open showing its purpose/steps/output-contract headings.
Taken at: N/A — a repo/editor screenshot, not the running app. Pick a skill the learner
actually ran in Module 3 (`data-discovery`, `metadata-enrichment`, or `column-name-standardizer`).

---

## Slide 4 — Which skills ran your flagship
**Beat:** make it concrete — name the skills behind Module 3/Module 4's stage buttons.

- **Module 3 · Data Discovery** → `data-discovery` (Postgres). **Data Profiling** → `data-profiling`.
  **Metadata Enrichment** → `metadata-enrichment`. **Column-Name Standardization** →
  `column-name-standardizer`.
- **Module 4 · Mapping & Transformation** → `data-mapping-neo4j`. **Serving** →
  `data-serving-virtual-view`.
- The graph loaders you never saw as buttons did the writing: `data-discovery-to-dcat-neo4j`
  and `data-profiling-to-dqv-neo4j` turned the discovery/profiling YAML into graph nodes.

> **Concept callout — one stage, many source variants.** "Data Discovery" is really a family:
> `data-discovery` (Postgres), `-mysql`, `-snowflake`, `-databricks`, `-parquet`. The backend
> picks the right variant for the bound source, so the *same* Discovery stage transparently
> ran the Postgres skill for you. That's why the track's spine is Postgres but the product
> isn't Postgres-only.

---

## Slide 5 — `--project-code`: how skills stay in their lane
**Beat:** the isolation rule that keeps two learners' work from colliding.

- **Every skill script accepts `--project-code`**, and every prompt template carries the
  project code. A skill only ever reads and writes nodes tagged with *its* project.
- The graph enforces this too: operational nodes are **project-scoped by URI** (next slide),
  and chat/MCP agents are rejected at a choke point if a query doesn't name the project.
- Consequence for you: the discovery you ran for `workforce_core` could never accidentally
  read or clobber the `compensation_payroll` project's graph — even though both live in one
  Neo4j.

> **Concept callout — isolation is a convention *and* a guard.** URIs carry the project code
> so a typo drops the edge rather than spawning a phantom, and the agent choke point
> (`run_cypher.py`) rejects any query whose text and params don't both reference the project
> code. Skills can't reach outside their project by accident.

---

## Slide 6 — Three run-shapes: stage, advisory, chat
**Beat:** the same skill machinery shows up in three places you touched.

- The Workbench runs a skill by launching a Claude agent with a **locked tool allow-list**.
  There are exactly **three run-shapes**, by where the skill runs and what it's trusted with:

| Run-shape | Tools it gets | Where you met it |
|---|---|---|
| **Pipeline stage** | `Read, Write, Edit, Bash, Glob, Grep, Skill` | The Module 3/Module 4 stage buttons — discovery, profiling, mapping, serving (the skills that *do work*) |
| **Advisory / programmatic** | `Read, Skill` only | Fired behind an endpoint on a button-press — the PO wizard advisors, the serving-strategy advisor, README documenters (pure-text reasoners) |
| **Chat** | `Read, Bash, Grep, Glob, Skill` | The DE **"Ask"** panel — the project chat assistant (needs `Bash` to run its scoped Cypher) |

> **Concept callout — "keys to the graph" vs "spoon-fed."** Stage/chat skills get connection
> info and query Neo4j and the databases *themselves*. Advisory skills get **no database
> access** — the backend runs the queries first and injects a compact snapshot; the skill only
> reasons over what it was handed and returns text. That's why an advisor can't leak or corrupt
> your data: it never had a connection.

_Speaker notes:_ **Why no LangChain/LangGraph?** Heavier agent frameworks like LangGraph or
CrewAI were evaluated and judged to "add complexity without much benefit now that code
generation makes refactoring cheap." The traditional argument for a heavier framework — easier
to restructure agent logic later — is weaker when an LLM can directly rewrite the code. The
stated preference is the native Anthropic Agent SDK for least friction. Skills are also
designed to be portable across harnesses — Anthropic's, OpenAI's, and others — which matters
because a client's existing AI stack shouldn't dictate whether the accelerator works.

---

## Slide 7 — Part B · Where "the graph" lives
**Beat:** make the abstract knowledge graph a concrete, clickable thing.

- The knowledge graph is a **Neo4j** database running in your local stack. You can open it:
  - **Neo4j browser** at `http://localhost:7475` — a UI to run Cypher and see nodes/edges.
  - **bolt** at `bolt://localhost:7688` — the wire protocol tools connect on.
  - In-compose credentials: user `neo4j`, password `workbenchpass`.
- Everything Module 3/Module 4 produced that *wasn't* a control-plane record (projects, stage runs, chat
  history — those live in SQLite) is a node or edge in here.

> **Concept callout — two stores, not one.** Neo4j holds the **knowledge** (catalog, quality,
> contracts, mappings, lineage). SQLite holds the **operational plumbing** (projects,
> workflows, stage executions, chat sessions). When we say "the graph," we mean Neo4j.

> **Concept callout — "all roads lead to the graph."** Why Neo4j and not a relational
> database? A relational DB could model this — but the graph offers deterministic lineage
> traversal that relational joins can't express as naturally. More importantly: we don't want
> an LLM to interpret blobs of text to understand lineage. We want a highly deterministic,
> structured representation. Graph queries return **exact, typed, structured facts**; RAG
> over fragmented text returns semantically similar passages. For lineage and provenance,
> exact is what you need.

> **Concept callout — graph-aware skills vs RAG.** Skills are "graph-aware" — they read
> grounding context from the knowledge graph and write their outputs back into it, producing
> full data lineage automatically as a side effect of normal operation. This is explicitly
> preferred over RAG/vector search: a skill issues a Cypher query that returns exact,
> structured context rather than relying on semantic similarity over fragmented text. The
> advisory skill run-shape (Slide 6) refines this further — the backend runs the queries
> first and hands the skill a compact, pre-fetched snapshot, so the skill only reasons over
> what it was given.

---

## Slide 8 — Project-scoped URIs: how a node knows who it belongs to
**Beat:** the naming scheme that makes isolation (Slide 5) real.

- Every operational node carries the owning project's code in its **URI**:

```
catalog:{project_code}:{schema}                         # e.g. catalog:hr-…-01:hr_core
dataset:{project_code}:{schema}.{table}                 # e.g. dataset:hr-…-01:hr_core.employees
column:{project_code}:{schema}.{table}.{col}            # e.g. …employees.employee_id
```

- Product-graph nodes (the contract, the DPROD product, its columns) use a
  **`{project_code}-contract`** prefix — globally referenceable, but still tagged with the
  owner, so one product can consume another across projects without breaking isolation.
- The scoping root is a thin `(:Project {projectCode})` node; scoped reads enter through it:
  `:Project → :HAS_CATALOG → :Catalog → :Dataset → :Column`.

_Speaker notes:_ Tie this straight to Slide 5 — the URI *is* the isolation mechanism. If you
run the Slide 16 lineage trace, you'll see these exact prefixes in the node names.

---

## Slide 9 — The six ontology layers
**Beat:** the graph isn't ad-hoc — it adapts six recognised standards.

- The graph is organised into **six ontology layers**, each an established vocabulary lowered
  into the property graph:

| Layer | Standard | What it captures | Written by (a skill you ran) |
|---|---|---|---|
| **DCAT-2** | W3C Data Catalog Vocab | catalogs, datasets, columns, keys | `data-discovery-to-dcat-neo4j` |
| **DQV** | W3C Data Quality Vocab | per-column profile stats / measurements | `data-profiling-to-dqv-neo4j` |
| **SHACL** *(-inspired)* | W3C Shapes Constraint Lang | data-quality rules as shapes | `data-quality-rule-generation` |
| **PROV-O** | W3C Provenance Ontology | who/what generated each fact | `metadata-enrichment`, `data-mapping-neo4j` |
| **DPROD** | EKGF Data Product Ontology | the product: ports, datasets, columns, lineage | `data-product-spec-to-dprod-neo4j` |
| **ODCS** | Open Data Contract Standard | the versioned data contract | `odcs-to-graph` |

> **Concept callout — why five separate standards, not one unified model?** Each standard
> covers a *distinct concern*: **DCAT** = catalog/discovery (what sources exist and where);
> **DQV** = data quality (per-column measurements — null rates, distributions); **SHACL** =
> shape constraints (quality rules as validation shapes); **DPROD** = data product structure
> (ports, datasets, columns, lineage edges); **ODCS** = the exportable data contract
> (versioned agreement between producer and consumer). Using established standards means
> tooling, governance frameworks, and partner systems can interoperate with the graph without
> custom translation.

> **Concept callout — this is a Neo4j LPG model "inspired by" these standards, not a
> literal RDF implementation.** The graph is a **Neo4j Labeled Property Graph (LPG)** that
> borrows vocabulary and structure from DCAT, DPROD, PROV-O, SHACL, DQV, and ODCS — but it
> is **not a strict DCAT-conformant RDF graph**. Someone expecting to query it with SPARQL or
> to find URI-addressable RDF resources will be looking for something that isn't there yet. A
> more formal RDF metadata ontology aligned to these standards more rigorously is a separate,
> in-progress effort (targeted at deployments on AWS Neptune and similar). The current Neo4j
> model is the practical, shipping implementation.

[SCREENSHOT PLACEHOLDER — SIZE: Large (authored diagram — not a product screenshot)]
Name: `ontology-layers-stack`
Caption: Six standards, one accreting graph.
Must show: An authored stack/layer diagram of the six ontology layers (DCAT-2 · DQV · SHACL ·
PROV-O · DPROD · ODCS), each labelled with the standard and the one thing it captures, drawn
as accreting layers on a shared `:Project` root.
Taken at: N/A — **authored diagram** (excalidraw / svg-architect in production), not a product
screenshot.

---

## Slide 10 — How the graph *accretes* stage by stage
**Beat:** the payoff concept — every Module 3/Module 4 stage added to the same graph.

- Each stage you ran didn't produce a throwaway file — it **added a layer onto the same
  graph**, in this order:

```
discovery → profiling → enrichment → rules → ODCS → DPROD → mapping → serving
 (DCAT-2)   (DQV)       (+PROV-O)     (SHACL)  (ODCS)  (DPROD)  (+PROV-O)  (ServingDefinition)
```

- So by the time you deployed in Module 4, one project's graph held: the raw schema, the profile
  stats, the descriptions you validated, the DQ rules, the contract, the product, the mappings,
  and the served view — **all connected**.
- **This is why lineage "just worked" in Module 4.** Nobody re-derived it; the Lineage tab just walked
  edges that discovery, mapping, and serving had already written.

> **Concept callout — two disconnected clusters → one connected graph.** A useful visual for
> the lineage concept: at the start of a new project, the graph has two disconnected blobs —
> the left cluster holds the raw source data (tables and columns discovered from the
> database); the right cluster holds the target data product (the pre-defined spec of what
> the output should look like). The data engineering pipeline builds the bridge. The moment
> the two clusters connect, the lineage is complete — and it is visible as a single connected
> subgraph in the Neo4j browser.

> **Concept callout — provenance stores the workflow, not just the value.** The graph
> doesn't store only the final approved value for each metadata item. It stores the **entire
> workflow** that produced that value: AI generated a column description → a reviewer
> rejected it with a reason → a human edited it → the edit was approved. All four states are
> nodes and relationships in the graph. The provenance record injects itself between the
> column node and the description node, not alongside it.
>
> The payoff: when a reviewer rejects a description with a reason (e.g. "too specific"),
> the system reflects on those provenance records and generates an updated playbook — a set
> of refined guidelines stored in the graph, tagged to a specific domain (HR, Finance, etc.).
> Future enrichment runs for that domain consult this playbook. Human feedback feeds back
> into AI quality automatically.

**Each pipeline stage's contribution to the graph:**

| Step | What accretes |
|---|---|
| Data Discovery | Source datasets and columns loaded as DCAT nodes |
| Data Profiling | Statistical metadata (null rates, distributions, most-common values) via DQV |
| Metadata Enrichment | AI-generated descriptions + human review decisions + PROV-O provenance |
| DQ Rule Generation | Quality rules as SHACL shapes, with severity levels |
| Data Mapping | Source-to-target column mappings with transform recipe + PROV-O |

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `neo4j-accreted-graph`
Caption: The flagship's graph, all layers at once.
Must show: The Neo4j browser at `:7475` after Module 4, showing a rendered subgraph for one flagship
project — e.g. `:Project → :Catalog → :Dataset → :Column` plus `:DataContract` /
`:DProdDataProduct` / `:ColumnMapping` nodes — so the accretion is visible as connected nodes.
Taken at: After Module 4 deploy, in the Neo4j browser (run a project-scoped `MATCH` and expand).

---

## Slide 11 — The two edges allowed to cross project lines
**Beat:** isolation has exactly two sanctioned exceptions — and you used one.

- Isolation is strict: nearly every edge stays inside one project's URI prefix. **Two edges are
  deliberately allowed to cross project boundaries:**
  - **`:CONSUMES`** — `(consumer :DataContract) → (:DProdDataProduct)`: a product consuming
    another published product (source / aggregate / consumer). **This is the edge you created in
    Module 4** when your aggregate consumed both source products.
  - **`:USES_DATASET`** — `(:CodeModule) → (:Dataset)`: links a **code-migration** project's
    code to a data-migration project's tables. (Code migration is a deferred topic — see Module 10 —
    but the edge is the second sanctioned exception.)
- Both follow the same discipline: a MATCH against a deliberately-referenceable, owner-tagged
  target, so a typo *drops* the edge rather than inventing a phantom link.

> **Concept callout — why only two.** Adding a third cross-project edge is a **documented
> decision**, not a quiet code change. That's how the mesh grows multi-hop chains (A→B→C via
> `:CONSUMES`) without the isolation guarantee eroding.

---

## Slide 12 — Part C · How the AI drafts a mapping
**Beat:** unpack the Module 4 stage that felt most magical.

- In Module 4's **Mapping & Transformation** stage, `data-mapping-neo4j` matched each product column
  to its source column(s) by **semantic similarity of their descriptions** — the descriptions
  you validated back in Module 3's enrichment gate.
- The similarity is **model-reasoned**, not vector-computed — the skill reads the descriptions
  and reasons about the best fit; it doesn't run an embedding library.
- Each draft lands as a `:ColumnMapping` node carrying a **structured transform recipe** (a
  *kind* + params), not a blob of SQL, plus PROV-O provenance and a confidence + rationale.

> **Concept callout — a recipe, not a dish.** The mapping stored in the graph says *what* to do
> ("concat first + last with a space"), not *how* in any one SQL dialect. Holding it neutral is
> what makes Slide 15 ("one recipe, different stove") possible.

---

## Slide 13 — Four authors, one priority cascade
**Beat:** the AI draft is only one of four voices — and it loses ties.

- Every mapping records a **`transformAuthor`** — where its recipe came from. There are four
  sources, and they resolve in a fixed **priority cascade**:

```
po_hint  →  steward_catalog  →  engineer  →  ai_suggestion
(PO's hint) (domain catalog)   (you, the DE) (the AI draft)
```

- Higher-priority sources win: a PO hint beats a catalog default, which beats an engineer edit,
  which beats the AI's suggestion. The **AI is the lowest-priority voice** — a starting point,
  not the decider.
- This mirrors the four-source DQ-rules model exactly (`po_hint`↔spec, `steward_catalog`↔domain,
  `engineer`↔user, `ai_suggestion`↔observation), so the pattern is consistent across the product.

_Speaker notes:_ The cascade is the antidote to "the AI decided." Anyone with more authority
than the model can override a draft, and the graph records who did.

---

## Slide 14 — Your three moves at the gate: Approve / Replace / Escalate
**Beat:** the human-in-the-loop step you performed in Module 4.

- Every mapping is **review-gated**. As the DE in Module 4 you had three moves per column:
  - **Approve** — accept the drafted recipe as-is.
  - **Replace** — author your own recipe (this stamps `transformAuthor = engineer` and wins
    over the AI draft per the cascade).
  - **Escalate** — kick it to steward review with a reason, when the right answer is a
    domain/governance call, not an engineering one.
- Nothing serves until a mapping is `approved`. The AI drafts; **you decide**.

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `mapping-review-transformauthor`
Caption: A reviewed mapping, showing its author source.
Must show: The Mapping Review panel for one Module 4 aggregate column, with the transform recipe
visible and the **`transformAuthor`** source labelled (e.g. `ai_suggestion` vs `engineer`),
plus the Approve / Replace / Escalate controls.
Taken at: Module 4 · Switch → DE · Mapping & Transformation review queue (before you deployed).

---

## Slide 15 — One recipe, different stove
**Beat:** how a neutral recipe becomes correct SQL on any platform — safely.

- At serving time the neutral recipe is compiled to real SQL by the **Dialect emitter**
  (`generate_view_ddl.py`). The *same* `full_name = concat(first, ' ', last)` recipe becomes:
  - Postgres → `COALESCE(first,'') || ' ' || COALESCE(last,'')`
  - Snowflake → `CONCAT(COALESCE(first,''), ' ', COALESCE(last,''))`
- **Same recipe, different stove.** The platform is bound to the *product*, not the transform,
  so the same graph state can be re-served on a different engine tomorrow with no re-authoring.
- A **fail-closed capability check** guards it: the compiler walks the compiled SQL against a
  per-platform **capability artifact** and refuses to hand back deployable SQL if any function
  is unsupported on the target.

> **Concept callout — "transpile success ≠ capability."** A naive translator will happily emit
> Postgres's `AGE(...)` for Databricks even though Databricks can't run it. The Workbench
> doesn't trust the transpile — it checks every function against what each engine *actually*
> supports and **fails at author time**, not at deploy. That's the difference between hoping the
> recipe ports and guaranteeing it.

_Speaker notes:_ **"Technology-unopinionated" design philosophy.** dbt and dlt are reference
implementations in this platform, not fixed dependencies — the architecture is deliberately
designed to adapt to client tech stacks. When a client asks "are we locked into dbt?" the
answer is: dbt is the reference materializer for one serving mode. A client with a different
tool in that role can replace it. "Reference implementation, not fixed dependency" is the
precise phrase.

[SCREENSHOT PLACEHOLDER — SIZE: Large (authored diagram — not a product screenshot)]
Name: `one-recipe-different-stove`
Caption: One neutral recipe → per-platform SQL, gated by capability.
Must show: An authored diagram — a single "recipe" box (neutral DSL) fanning out to
Postgres / Snowflake / Databricks / BigQuery SQL, with a "capability check → fail closed on
unsupported" gate on the fan-out.
Taken at: N/A — **authored diagram** (excalidraw / svg-architect in production).

---

## Slide 16 — Self-check ① · Trace a column's lineage yourself
**Beat:** prove the "graph is the source of truth" claim with your own hands.

- Open the **Engineering Workbench**, your Module 4 aggregate project, and the **"Ask"** panel
  (the project chat assistant — project-scoped, **read-only**, and every claim it makes is
  backed by a scoped Cypher query against the graph).
- Ask it to **trace the lineage of one aggregate column back to its source columns** — for
  example: *"Where does `total_monthly_cost` come from? Show me the source columns and the
  transform."*
- **What "right" looks like:** the answer names the upstream `:Column`(s), the `:ColumnMapping`
  recipe, and the `:CONSUMES` hop to the source product — the exact edges Slides 10–12 said were
  written during Module 4. You're reading the accreted graph, not a cached report.

> **Concept callout — Ask panel ≠ Semantic Q&A.** The DE "Ask" panel reasons over one
> project's graph and is what we use in this track. The domain-wide **Semantic Q&A chat** is
> **out of scope** (it depends on the excluded semantic layer). Validate here, not there.

> **Concept callout — on the per-product Q&A tab.** The per-product Q&A tab (which you use
> for validation in Module 3/Module 4/Module 5) is a self-contained feature — it generates a domain ontology
> reverse-engineered from the ingested product and answers questions about it. It is a
> **validation tool**, not a finished enterprise semantic layer. Treat it as a useful
> sanity-check surface within the scope of a single product; do not position it to clients
> as the semantic query interface. `[VERIFY: confirm this framing is still accurate for the
> current release before using it in a client-facing session]`

---

## Slide 17 — Self-check ② · Explain it back + Next
**Beat:** close the module; confirm the learner can teach it.

- **Explain-it-back (answer before moving on):**
  1. Name the three things a skill directory contains and the one that makes a skill run *code*.
     *(hint: `SKILL.md` + `scripts/`)*
  2. What does `--project-code` protect, and how does the URI scheme back it up?
  3. Name three of the six ontology layers and the stage that writes each.
  4. Why did **lineage "just work"** in Module 4? *(hint: accretion onto one graph)*
  5. Which of the four `transformAuthor` sources has the **lowest** priority — and what does
     that tell you about who's really in control?
  6. What does "transpile success ≠ capability" mean, and when does the check fire?
- **Next:** Module 6 — stop clicking stages and **drive DW headlessly over MCP** from your own
  Claude Code (the 141 DE / 57 PO tool front doors).

_Speaker notes:_ If a learner can't answer #4 and #5, replay Slides 10 and 13 before Module 6 —
those two ideas (accretion + the author cascade) are the load-bearing takeaways of Module 5.
