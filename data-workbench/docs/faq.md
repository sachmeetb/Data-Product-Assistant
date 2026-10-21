# Data Workbench — Frequently Asked Questions

A plain-language guide to what Data Workbench is, how it works, and how it fits
alongside your existing data tooling. Answers here are intentionally functional
rather than deeply technical — for internals see `CLAUDE.md` and
`docs/architecture/`.

---

## 1. Fundamentals

### What is Data Workbench?
Data Workbench is a web-based orchestrator for building **data products** end to
end — from discovering and profiling a source database, through documenting and
quality-checking it, to publishing a governed, contract-backed product into a
**data marketplace** that consumers can find and reuse. It's organized around
*projects* (containers) that hold one or more *workflows* (Source Discovery,
Quality Assessment, Product Engineering, and so on). Each workflow runs as a
sequence of stages, with live streaming output, human approval gates, and a
knowledge graph recording every decision.

Think of it as the connective tissue between a raw database and a trustworthy,
well-described, reusable data product — with AI agents doing the heavy lifting
and humans approving the important calls.

### What technologies does it use?
- **Backend:** Python 3.12, FastAPI + WebSockets, SQLite (via SQLModel) for
  operational state.
- **Knowledge graph:** Neo4j.
- **AI engine:** the Claude Agent SDK (agents run server-side).
- **Frontend:** React single-page app (react-router v7), no heavy UI framework.
- **Source/serving databases:** PostgreSQL, MySQL, Snowflake, Databricks, and
  Parquet for discovery, with a dialect layer for serving (see platform
  compatibility below).

### Is it compatible with a custom / "bring your own" stack?
Partly, and by design it's extensible. The source-database side currently
discovers/profiles **PostgreSQL, MySQL, Snowflake, Databricks, and Parquet**
(with Oracle and SQL Server reachable through the migration path), and the SQL it generates
for serving can emit **PostgreSQL, Snowflake, Databricks, BigQuery, or ANSI**
dialects. The AI layer is built on Claude. Swapping in a fundamentally different
graph store or LLM provider would be a real engineering effort rather than a
config switch, but adding a new **database platform** or **SQL dialect** is a
deliberately bounded extension point (see "Adding a new data platform"). The
specific choices here are today's reference stack, not architectural locks — the
orchestrator/skill split is meant to be extended.

### What programming language and agent harness is it built on?
Python on the backend, TypeScript/React on the frontend. The agent harness is
the **Claude Agent SDK** — the same engine that powers Claude Code — invoked
server-side for each pipeline stage.

### What LLM does it use?
Claude (Anthropic), through the Claude Agent SDK. The latest and most capable
Claude models are the default. That's the reference choice the current build is
tuned around rather than a hard-wired dependency.

### How is it licensed / supported, and is there a cost?
*(Confirm with the team — not derivable from the codebase.)* It is an
internal/Accenture-supported build. Cost considerations are primarily the LLM
usage (Claude API/Foundry key — or, for a single workstation, a token from an
existing Claude Code subscription) plus hosting for the backend, Neo4j, and the
target databases.

### How is LLM cost tracked, and what drives it?
Workbench keeps a **token-accounting ledger**: every model call records a
`LlmUsageEvent`, so usage is measured, not estimated. You can roll it up
**per project** and **per Q&A**, read it over REST (`/api/usage`), pull it over
MCP (`get_usage_summary`), or see it on the UI's usage cards. The headline
`total_tokens` figure counts **working (uncached) tokens** — the input+output the
model actually processed, with cached reads tracked separately so prompt-cache
savings are visible. A **dollar estimate** is best-effort and can be null (e.g.
under Azure Foundry, where per-token pricing isn't exposed). What drives cost is
the agentic work itself: discovery/profiling over wide schemas, mapping and
serving generation, and semantic Q&A — the bigger the schema and the more stages
you run, the more tokens.

### How long did it take to build?
*(Confirm with the team — not derivable from the codebase.)*

---

## 2. Agentic architecture & human-in-the-loop

### How is it "agentic" if I can see manual steps?
It's agentic *with* humans in the loop, not instead of them. The AI agents do
the open-ended work — reading a schema, profiling data, writing column
descriptions, proposing quality rules, suggesting column mappings and SQL
transforms, drafting view DDL. Humans don't *do* that work; they **review and
approve** it at well-defined gates. The "manual steps" you see are mostly
approval gates (a Product Owner validating names and descriptions, an engineer
approving a mapping) and a few deterministic lifecycle actions (e.g. "Mark
Discovery Complete"). The design philosophy is: let agents propose, let humans
decide.

### How many agents and skills are there? What categories?
The work is delivered through **67 skills** — packaged units of agent
capability. They group into:
- **Discovery & profiling** — read a database's structure and its actual data.
  Beyond `data-discovery` / `data-profiling`, there are now platform variants for
  **PostgreSQL, MySQL, Snowflake, Databricks, and Parquet**, plus the
  graph-loading skills.
- **Documentation/enrichment** — generate column and table descriptions,
  standardize column names (`metadata-enrichment`, `column-name-standardizer`).
- **Data quality** — derive rules from profiling, generate and run tests
  (Great Expectations or Pandera), analyze failures, score, and remediate
  (`data-quality-*`, `data-scoring`, `data-remediation*`, `domain-rule-enhancement`).
- **Product engineering** — map source columns to product columns, generate
  serving views, advise on serving strategy
  (`data-mapping-neo4j`, `data-serving-virtual-view`, `serving-strategy-advisor`).
- **Serving documenters** — write the package README/docs for each serving mode
  (`data-serving-view-documenter`, `data-serving-dbt-documenter`,
  `data-serving-lakehouse-documenter`).
- **Data & code migration** — assess and generate a raw platform→platform data
  migration (`migration-assessment-advisor`, `migration-pipeline-generator-dlt`,
  `migration-package-documenter`), plus the code-migration skills that
  reverse-engineer legacy code into a reviewed spec and forward-engineer it onto
  the target platform from curated Platform-SME corpora.
- **Estate scanning & feasibility** — grade a catalog of *desired* data products
  against a live estate: metadata enrichment, entity/authority-aware column
  matching, and a reasoning-based matcher for the uncertain cases.
- **Authoring assistants** — help a Product Owner shape an idea into a spec
  (names, schema, rules, gap analysis) (`product-authoring-assistant` and its
  sub-advisors).
- **Marketplace & semantic** — natural-language Q&A over published products,
  business-concept recommendations, and a **conversational Q&A router** with
  filter-intent interpretation (`marketplace-product-chat-assistant`,
  `business-concept-advisor`, `semantic-qa-conversation-router`,
  `filter-intent-interpreter`, the question analyzer/executor).
- **Reflection / self-improvement** — analyze past runs and propose
  improvements (`skill-reflector`, `chat-reflector`, `playbook-*`).

### How is the human-in-the-loop integrated?
Through **review gates** with full provenance. When a stage finishes, it either
completes or lands in `awaiting_review`. A reviewer (in the right role) approves,
edits, rejects, or escalates each item; every action is recorded as W3C PROV-O
provenance in the graph (who did what, when, and why). Only when the queue is
clear does the stage flip to complete and the next stage unlock.

### What advising does it provide to Product Owners vs. Engineers?
- **Product Owners** get authoring help while shaping a product: name and
  description suggestions, schema-column recommendations, quality-rule coaching,
  a readiness/OSI score, a pre-flight gap check against the sources they're
  consuming, and a serving-strategy recommendation.
- **Engineers** get the discovery/profiling output, AI-proposed column mappings
  and transforms (with rationale they can inspect and override), generated view
  DDL, and serving warnings (e.g. "this join may multiply rows").

---

## 3. Access & integration

### Is the UI the only way in, or is there an API / CLI?
There are **two first-class ways in**, against the **same backend**:
1. **The web UI** — two persona-scoped shells (Product Workbench for the Product
   Owner, Engineering Workbench for the Data Engineer).
2. **MCP (Model Context Protocol)** — a standard streamable-HTTP MCP server at
   `/mcp`, so an engineer can drive the whole pipeline from their *own* AI coding
   tool without ever opening the browser.

There's also a REST/WebSocket backend the UI itself uses, but MCP is the
supported "programmatic" front door.

### Does it have an MCP endpoint?
Yes. `/mcp` is a standard streamable-HTTP MCP server authenticated with a plain
`Authorization: Bearer <token>` header (not OAuth). Tokens are scoped to the
projects an engineer is allowed to work on. It exposes **142 MCP tools** spanning
read/query (list projects, get state, get stage results, run scoped Cypher, query
the semantic layer), stage execution (set data source, run stage, complete/reset
stage, config options), interactive (pending questions / answer), review-write
(approve descriptions, mappings, domain rules, table and relationship
descriptions — each requiring a declared role), and full consumer-product
authoring + marketplace/DQ/OSI/QA reads. See `docs/mcp-architecture.md` for the
canonical per-tool reference.

A second front door for the Product Owner — `/po-mcp` (68 tools) — adds
portfolio triage, the authoring wizard, the validation gate, and publish.

### How do the engineer and Product-Owner tool surfaces differ?
`/mcp` and `/po-mcp` are two MCP front doors over the **same backend**, each
scoped to a persona's job:
- **`/mcp` — Data Engineer (142 tools).** Drives the engineering pipeline:
  read/query (projects, stage results, scoped Cypher, the semantic layer), stage
  lifecycle (set data source, run/complete/reset a stage, config options), the
  materialization gate, joins/pre-flight, mappings and unmapped columns,
  review-write (approve descriptions/mappings/domain rules in a declared role),
  dbt/OKF export, and semantic discovery.
- **`/po-mcp` — Data Product Owner (68 tools).** The PO's job: portfolio triage
  (summary, list products, product status), the contract-first authoring wizard
  (create source/consumer products, author + save the ODCS spec, bind sources,
  submit to engineering), the source-product validation gate, domain-rule review,
  the PO↔engineer feedback loops, and deploy-to-marketplace. It delegates to the
  same router handlers the web UI uses — **no raw Cypher**.

One caveat: the split is **surface separation, not a security tier**. Auth is
shared (the same bearer tokens reach both servers) and review tools take a
self-declared `role`, so any valid token can in principle reach PO actions;
binding roles to tokens is an open follow-up. Each persona also has a matching
install kit — `engineer-kit` registers `/mcp`, `po-kit` registers `/po-mcp` (see
"connect my own agent" below).

### Can it integrate with Claude Code, OpenAI Codex, GitHub Copilot, VS Code, etc.?
Because the endpoint is a **standard MCP server**, it's **client-agnostic** — any
MCP-capable client can drive it with the same URL + token:
- **Claude Code** — first-class, via a small kit plugin (a guide skill + the MCP
  server registration). Two kits: `engineer-kit` (for `/mcp`) and `po-kit` (for
  `/po-mcp`), each with a one-line installer (`/api/engineer-kit/install.sh`,
  `/api/po-kit/install.sh`). Not sure which persona? `/api/bootstrap?persona=po|de`
  serves a paste-once prompt that installs the right one and tells you to relaunch.
- **OpenAI Codex** and **Cursor** — supported; both installers also write a Codex
  `config.toml` + `AGENTS.md` and a Cursor `.cursor/mcp.json` + rule. Requires a
  recent Codex/Cursor with streamable-HTTP MCP.
- **Other clients** (Copilot, VS Code extensions, etc.) — work to the extent
  they support streamable-HTTP MCP with a static bearer token; older stdio-only
  clients need an `mcp-remote` bridge.

### How do I connect my own Claude Code (or other agent) to Workbench?
There's a one-paste **bootstrap** flow, so a brand-new user never hand-edits any
config:
1. **Fetch the bootstrap prompt** — `GET /api/bootstrap` (persona-agnostic; it
   asks which you are) or `GET /api/bootstrap?persona=po|de` to pre-pick. The
   backend injects its own URL into the prompt text.
2. **Paste the whole prompt into a fresh Claude Code session** in an empty
   folder. The prompt drives the rest itself — no MCP, skill, or repo access
   needed up front.
3. It runs the matching **kit installer** (`/api/po-kit/install.sh` or
   `/api/engineer-kit/install.sh`), which writes the MCP-server registration + a
   guide skill + slash commands for whichever client you use — Claude Code
   (`.mcp.json` + `.claude/`), Cursor (`.cursor/`), or Codex (`.codex/config.toml`
   + `AGENTS.md`).
4. **Export your token** — `WORKBENCH_TOKEN=<bearer>` (one of the backend's
   configured tokens; a local dev backend in open mode accepts any value). It
   stays in your shell env, never written to disk.
5. **Relaunch the client** — MCP servers only connect on a fresh start — then
   just say what you want to do (a PO: *"I need to create a new data product."*;
   an engineer: *"List my workbench projects."*).

From the web UI you can skip step 1: the workbench chooser screen has a **"Copy
Claude Code bootstrap prompt"** link on each card that copies the persona-specific
prompt straight to your clipboard.

### What can the CLI / MCP path do that the UI can't, and vice versa?
- **MCP excels at** driving the engineering pipeline programmatically and
  letting an engineer orchestrate Workbench *from their own automation* (see the
  next question). Discovery, profiling, mapping, serving, and even review
  approvals (with role-switching) can all be done without leaving the terminal.
- **The UI excels at** the rich review experiences and authoring wizards: the
  Product Owner validation gate, the contract-first product wizard, the
  marketplace browsing/lineage canvas, and the interactive materialization
  preview. Two-person approval gates are intentionally finished in the browser.

### Can a Data Engineer drive Workbench from their *own* automations? *(new)*
Yes — this is a core design goal. Because Workbench exposes its pipeline as MCP
tools, an engineer's own Claude Code (or other agent) can call those tools as
part of a larger automation. In other words, the engineer's agent becomes the
*orchestrator* and Data Workbench is one of the systems it orchestrates: their
automation can run discovery, poll for completion, inspect results, approve
reviews in a declared role, reset and re-run stages, and query the semantic
layer — all programmatically. Nothing about the pipeline is locked to the web UI;
the UI and an external agent are peers talking to the same server. The only
things that stay human-in-the-browser by design are the two-person approval
gates a separate role (PO/Steward) must sign.

### Can I feed in recommendations from an external assessment tool? *(new)*
Yes — that's the **inbound intake** front door. An external tool that assesses an
organization's data estate can `POST` its migration/modernization recommendations
to `/api/intake/submit` (authenticated by a scoped machine credential), and
Workbench turns them into ready-to-work projects. Because the incoming content is
usually messy and inconsistent (prose, spreadsheets, the odd contract), an
**isolated LLM parser** normalizes it into a strict, **confidence-graded
blueprint** — high-confidence fields pre-accepted, ambiguous ones flagged,
anything absent honestly marked `missing`. A practitioner then **reviews and edits
the blueprint** (an *Intake* tab in each workbench, with MCP parity), and only on
approval does an idempotent saga scaffold the right project(s): a `dmig` project
for a migration, or a `dpe-sa` + `dpe-cf` **portfolio** for a modernization.
Nothing is created that a human hasn't approved.

---

## 4. Knowledge graph & semantic layer

### What is the knowledge graph for?
It is the **system of record** for everything the pipeline learns and decides:
the database structure, profiling statistics, column/table descriptions, quality
rules, column mappings and transforms, data-product contracts, serving
definitions, and the full provenance trail of every human approval. It's what
makes lineage, the marketplace, scoring, and audit possible — the relational
SQLite DB only tracks operational run state. (Neo4j is the reference graph store
the current build standardizes on; the *model* — the ontologies below — is the
durable part, not the specific engine.)

### What ontologies are used, and why?
The graph composes several W3C / open standards, each with a job:
- **DCAT-2** — catalog the datasets and columns (the "what exists").
- **DQV (Data Quality Vocabulary)** — attach profiling measurements and quality
  metrics.
- **SHACL-inspired shapes (`:PropertyShape`)** — express data-quality rules as
  validatable constraints.
- **PROV-O** — record provenance: who approved/rejected/edited what, and why.
- **ODCS (Open Data Contract Standard)** — the data-product contract format.
- **DPROD** — the published data-product graph (the product, its output datasets
  and columns) that a contract materialises into, and that the marketplace and
  `:CONSUMES` lineage read from.

On top of these standards sit several **custom layers** — the column
mapping/transform model, the serving definitions, and the semantic
business-concept layer. Separately, a producer-side **OSI** readiness model
(`:OsiEvaluation`) scores products for AI-readiness; it's a scoring rubric, not a
graph ontology, so it lives alongside these layers rather than among them.

### What does "full data lineage" mean here?
Every product column can be traced back through its mapping and transform to the
exact source column(s) it came from — including secondary references like lookup
tables — and forward to the consumer products that depend on it (via the
`:CONSUMES` relationship between products). Because mappings, transforms, and the
serving SQL all live in the graph with provenance, lineage isn't reconstructed
after the fact; it's a first-class queryable structure, visualized in the
marketplace's lineage canvas.

### Is there an API for the semantic layer?
Yes — `/api/semantic/*` (and the MCP `query_semantic_layer` tool). It hosts
curated **business concepts** (e.g. "Customer Tier", "Order Status") with their
definitions, value trees, and column bindings, and a **natural-language Q&A**
path that resolves a question to concepts, authors SQL against the relevant
deployed views, runs it, and returns a markdown answer (text + result table +
the SQL used). It's read-only and domain-scoped.

Q&A runs in **three modes**: `full` retrieval (throw the whole domain context at
the model), `concept_guided` retrieval (narrow to the concepts a question maps to
first), and a **conversational** mode. The conversational mode adds an
ontology-grounded **router** that classifies each turn as `query | clarify |
reject | chat` and keeps **server-side session memory** (`SemanticChatSession`),
so follow-up questions ("...and just for last quarter?") resolve against the
prior turn instead of starting cold.

### Is a vector store used anywhere?
Yes, for **business-concept semantic search**. Concepts are embedded with a
**self-hosted, CPU-only** embedding model (`fastembed` — no external embedding
API), stored on the concept nodes, and queried through **Neo4j's native vector
index** (cosine similarity). If the model isn't available it falls back to a
non-embedding "full context" path, so it degrades gracefully.

### How do business concepts improve marketplace Q&A?
Business concepts are the **curated semantic layer** — named ideas like "Customer
Tier" or "Order Status", each with a definition, a value tree, and bindings to the
actual product columns that express them. They're built through a Discovery
sequence (scaffold → recommend → enrich, with a reset), assisted by the
`business-concept-advisor` skill and a human curator. At Q&A time they give the
model **grounded anchors**: a natural-language question is resolved to the relevant
concepts first (the `concept_guided` retrieval mode), so the SQL it authors targets
the right columns and deployed views instead of guessing. They also power the
record-level **value resolution** path — matching a phrase like "gold tier" to the
exact stored value — so grounded values get injected into the query rather than
invented.

---

## 5. Data products

### Source-aligned vs. consumer-aligned — what's the difference?
- **Source-aligned (`dpe-sa`)** products are *discovery-first*: an engineer
  profiles a real source database, and the product closely mirrors that source
  (cleaned, named, documented, quality-checked). The Product Owner validates the
  names, descriptions, table classifications, and rules.
- **Consumer-aligned (`dpe-cf`)** products are *contract-first*: a Product Owner
  shapes the product they *want* (schema, grain, filters, rules) in a wizard
  **before** binding upstream sources, then engineering maps it onto one or more
  existing **source-aligned products** it `:CONSUMES`.

In short: source-aligned models what you *have*; consumer-aligned models what a
consumer *needs*.

### Can products be chained — consumer-on-consumer, and what's an *aggregate*?
Yes — products chain into **multi-hop DAG chains (A→B→C), unbounded in depth**. A
consumer-aligned (`dpe-cf`) product may `:CONSUMES` **any** published product, not
just source-aligned ones. That's possible because a product's kind is a
**three-valued, data-mesh classification decoupled from its archetype**:
- **`source`** — a source-aligned (`dpe-sa`) product mirroring a real source.
- **`aggregate`** — a reusable *building block* meant to be consumed further; it
  rides the same `dpe-cf` machinery as a consumer and, to de-risk downstream
  chains, *defaults* to a materialized serving recommendation (a default, not a
  lock).
- **`consumer`** — a fit-for-purpose *leaf* product.

Both `aggregate` and `consumer` are authored through the consumer-aligned flow;
they differ only by **intent** (is this meant to be built on, or is it the end of
the line?). The Product Owner declares the kind as a late-binding flag in the
product wizard.

Chains are kept safe by a hard **DAG guard**: self-consumption, or any binding
whose target can already reach the consumer, is rejected at the single atomic
save point with a structured error — so you can't create a cycle. `:CONSUMES` is
one of **two** sanctioned cross-project graph edges (the other links a
code-migration project's code to a data-migration project's datasets); everything
else stays project-scoped.

### How are aggregated data products represented?
At the **dataset level** via a "Shape" specification (`:DatasetTransform`): you
declare a grain, grouping keys, and per-column aggregate functions (sum/count/avg
etc.), optional filters, dedupe, SCD policy, and joins. The serving SQL then
emits the appropriate `GROUP BY` / windowed CTEs. So an aggregated product is
just a product whose shape carries grouping keys + aggregate columns.

### What are the output formats — virtual views, materialized, lakehouse, or transfer?
**Four serving modes/patterns**, and a product uses one at a time (they're
mutually exclusive per product). A built-in **serving-strategy advisor**
recommends which one fits your grain, history needs, and source-vs-target
platforms:
- **Virtual view** (`native_virtual`) — a `CREATE OR REPLACE VIEW` deployed
  against the source database. Zero-copy, always fresh, no history. Fastest to
  stand up; same platform only.
- **Materialized (dbt)** (`native_materialized`) — a real, runnable dbt project
  that builds physical tables (and dbt **snapshots** for SCD2 history) on a
  target database. Use when you need history or heavy aggregation performance.
- **Lakehouse** (`lakehouse_file`) — extract the data to **Parquet** files and
  query them via **DuckDB**. A portable file hop with no live-database dependency
  — useful for handing off a snapshot or working off-warehouse.
- **Cross-platform transfer** (`transfer_then_transform`) — Extract + Load the
  data into a *different* target platform, then transform there. The only
  cross-engine mode: it's how a Postgres/MySQL source becomes a Snowflake or
  Databricks product. It's packaged as a **dlt** (data load tool) package;
  DuckDB coordinates the simpler Postgres/MySQL→Postgres/DuckDB hops while dlt
  handles Snowflake/Databricks.

The first three (and the transform half of the fourth) share one SQL core, so the
underlying view definition is identical across modes — only the wrapper/packaging
differs. The advisor *requires* a materialized or transfer mode for SCD2 history
or a cross-platform source→target.

### Can lineage be visualized from source-aligned to consumer-aligned?
Yes. The marketplace shows each product's `consumes[]` / `consumed_by[]`
cross-references and a **lineage canvas** that draws the mapping graph from
source columns through to product columns, including lookup-table references.

### How does the data mapping process work?
Mapping is the step that connects a product's columns to the actual source
columns that feed them. For each **product column**, the agent looks across the
available sources and proposes the best match, recording it as a `:ColumnMapping`
in the knowledge graph with full provenance (it skips columns that are already
mapped, so re-runs don't duplicate work). Where the sources come from depends on
the product type:
- **Source-aligned** products map the project's own **raw discovered columns**.
- **Consumer-aligned** products map columns from the **source products they
  consume** — reusing already-published, already-defined product columns rather
  than raw tables.

Crucially, the agent **proposes, it doesn't apply**: every mapping lands as
*pending review* and a human approves or adjusts it before it ever affects the
serving SQL. The matching and serving logic targets the supported SQL dialects
(PostgreSQL first) today, but the mapping capability is a self-contained skill —
so supporting other source shapes is an extension point, not a rewrite.

### How does the agent decide on a mapping and a transformation?
For the **mapping**, the agent weighs several signals together: how closely the
column names line up, how much their descriptions mean the same thing, whether
the data types are compatible, any source-column hint the Product Owner left, and
— when a foreign key could point either way — a preference for the authoritative
(owning) table. It won't invent a weak match; columns it can't confidently place
are surfaced as *unmatched* for a human to resolve.

For the **transformation** (how the source value is shaped into the product
column), the agent follows a deliberate priority order — **hint → catalog →
LLM**:
1. a transform the Product Owner declared in the contract is used as-is;
2. otherwise, a reusable template from the relevant **domain playbook** is applied
   if its rules fit;
3. otherwise, the agent proposes one itself — defaulting to a straight 1:1
   pass-through, and reaching for richer transforms (combine columns, cast a type,
   conditional logic, a lookup, or privacy treatments like bucketing/masking/
   hashing) only when the names or types call for it.

The engineer then **Approves**, **Replaces** the suggestion (an override that
always records *why*), or **Escalates** it to the Data Steward, and a downloadable
**mapping rationale report** explains each choice and the alternatives considered.
That priority order, the catalog templates, and the set of available transforms
are deliberate defaults expressed in skills and playbooks — extendable (new
transforms, new domain catalogs) without touching the orchestrator.

### Is there version control for data products?
Yes. Editing a **published** product creates a **new contract version** on first
save: v2 starts as a fresh draft, v1 is retained (marked not-current) and linked
via provenance (`:PROV_WAS_DERIVED_FROM`). Earlier versions aren't destroyed, and
the marketplace pins to the latest *deployed* version so in-flight drafts don't
leak to consumers.

### How are data products scored, and can the rubric be customized?
Products get an **OSI readiness score** (a banded score across dimensions like
grounding, documentation, and quality-rule coverage), and source data gets a
**quality score** computed from rules-vs-profiling. The scoring runs as a skill
against the graph. The rubric lives in the scoring skill and domain playbooks, so
it's adjustable — but customizing it is a skill/playbook change, not a UI toggle.
*(Confirm desired customization surface with the team.)*

### Does it validate beyond generating DDL?
Yes. Generated serving SQL is **smoke-tested** on deploy; the materialized path
has a **sample → inspect → approve → full** gate before a full build is allowed;
quality rules are turned into **executable tests** (Great Expectations / Pandera)
run against real data; and a post-deploy **reflection** step samples the live view
and checks it against the product's declared shape.

### How are DQ tests packaged and run?
Data-quality testing follows a **Configure → Build → Run** lifecycle. *Configure*
picks which rules and datasets to test; *Build* generates a self-contained,
**downloadable and git-pushable DQ package**; *Run* executes it. The package is
**self-reporting** — it writes a `run_result.json` (machine-readable pass/fail per
rule) plus a `report.md`, and it **exits non-zero on failure**, so you can drop it
straight into CI and let it gate a build. It runs in **two modes**, auto-selected
by the workflow: `catalog` mode tests discovered datasets directly, while `dprod`
mode tests a data product against its contract. Because the package is standalone,
the same tests DWB runs on demand also run unchanged in your own pipeline.

### What is Data Migration (dmig)?
`dmig` (Data Migration) is an **engineer-initiated, platform→platform** workflow —
a raw *lift-and-shift* that moves tables from one database to another with no
product graph, no mapping, and no contract. Sources can be **Oracle, SQL Server,
PostgreSQL, or MySQL**; targets can be **PostgreSQL, Snowflake, or Databricks**.
The migration is emitted as a runnable **DLT (data load tool) package**, and DQ
here means **row-count reconciliation** — the same package run with
`--mode verify` compares source and target counts rather than checking quality
rules. It's a deliberately thin subsystem: think "get the data across correctly,"
not "build a governed product." (`dmod`, data modernization, remains an
unimplemented placeholder.)

### What is Code Migration (cmig)?
`cmig` (Code Migration) is the **code sibling of `dmig`**: where data migration
moves the *tables*, code migration converts the *code that runs against them* — a
Teradata BTEQ report, an Oracle PL/SQL job, a MySQL stored report — onto a modern
target (Snowflake, Databricks). It's **engineer-initiated** and, like `dmig`,
publishes no marketplace product; it's a conversion job.

Rather than a single-shot LLM translation, it runs **spec-first**: it links to a
completed `dmig` project (so it knows the exact source→target schema), imports the
legacy code (stored immutably, never executed), **reverse-engineers** it into a
reviewed specification grounded on a curated *source* Platform-SME corpus, stops
at a **hard human review gate**, then **forward-engineers** target code from the
approved spec using a curated *target* Platform-SME corpus of patterns and
anti-patterns. The output is a downloadable package with `old/` and `new/` code
side by side, the spec, and a README — optionally auto-pushed to Git. Because the
corpora are version-controlled, a client can **fork the target corpus** to impose
their own house conventions on the generated code.

---

## 6. Connected Estate & data-product feasibility

### What is Connected Estate / Data-Product Feasibility?
It's the **top-down** counterpart to the rest of the Workbench. Most of the tool
works *bottom-up* — start from a source you have, discover it, shape a product.
Feasibility flips the direction: you start from a **shopping list of the data
products you *wish* you had**, and the Workbench tells you **how buildable each one
is today** against a live estate you connect it to.

Picture a **kitchen check against a shopping list of dishes**: you hand over the
dishes you'd like to serve (reference data-product definitions), Data Workbench
walks your **pantry** (your live databases — schemas, tables, columns) *and* your
**already-plated dishes** (products already published to the marketplace), and
grades each item with a **stoplight**:

- **`ready`** 🟢 — a published product already matches; just adopt it.
- **`adaptable`** 🔵 — a close published product exists but needs a bounded,
  allowed tweak (a rename, a currency normalization, a coarser-grain aggregation);
  author a consumer product that consumes it.
- **`assemblable`** 🟠 — no product yet, but the raw data exists in the estate and
  the pieces join; compose a modernization portfolio via Intake.
- **`absent`** ⚪ — no matching product *and* no matching raw data; a genuine gap.

(Note the deliberate colours on screen: adaptable is **blue**, and absent is a
neutral **slate** — an absence is information, not a failure.)

### How is a feasibility grade actually decided?
Honestly and conservatively, from **two evidence pools** — published products and
raw estate columns — with each required attribute assigned to **at most one**
column. The grader runs a two-stage schema-shortlist → column-match, is **entity-
and authority-aware** (it prefers the owning/authoritative table when a foreign key
could point either way), and recognises **composite derivations** (if a definition
wants `name` but the estate only has `first_name` + `last_name`, it reports `name`
as *derivable* rather than absent). Coverage thresholds plus a **grain-key gate**
decide the tier. One asymmetry is baked in: you can always roll *finer* data up to
a *coarser* grain (weekly → monthly is a legitimate aggregation), but you can't
reliably split coarse back to fine — so a definition needing *finer* grain than the
estate offers is `absent`, never `adaptable`. AI is used as a **bounded judge** for
the uncertain column matches only, with a ceiling on how far it can lift a grade.

### How do I set it up and act on the results?
An engineer registers a **connection** (Postgres, MySQL, Snowflake, or Databricks)
and attaches it as an **estate source** in the Product Workbench — a key gotcha is
that the catalog is set on the estate *source*, not the connection (see
`docs/estate-discovery.md`). You pick schemas and **scan**: a deterministic,
provider-driven crawl (not the LLM discovery skill) that produces a versioned
metadata snapshot with volumetrics and code-asset capture. **Enriching the metadata
before grading** materially improves matches; estate column vectors are then
**embedded once and persisted on the graph**, so grading reads them back instead of
recomputing. From the stoplight grid you can **adopt** a ready product, **author a
consumer product** seeded from an adaptable definition, **compose a modernization
portfolio** for an assemblable one, or **save a candidate** for later. It's a
**separate bounded context** from the older Pulse-backed discovery — its own
`/api/estates/*` + `/api/feasibility/*` routes, `:Estate` / `:EstateScan` graph
anchors, and its own PO MCP tools.

For the full treatment — including how a grade is decided in plain terms and the
tuning levers — see `docs/data-product-feasibility.md` (functional),
`docs/estate-discovery.md` (setup), and `docs/connected-estate.md` (architecture).

---

## 7. Data marketplace vs. data catalog

### I already have a data catalog — do I still need the marketplace?
They solve different problems. A **catalog** inventories what data exists and its
metadata. The Workbench **marketplace** publishes **finished, contract-backed,
quality-scored data products** with lineage, consumer cross-references, and a
natural-language Q&A layer — i.e. things ready to *consume*, not just *find*. You
can keep your catalog as the system-of-record inventory and use the marketplace
as the curated product storefront. They're complementary rather than redundant.

### What does the marketplace offer beyond a standard catalog?
- Products carry an **ODCS contract** and a **readiness/quality score**.
- **End-to-end lineage** (source → product → downstream consumers) is queryable
  and visualized — including a **layered value-flow (Sankey) view** of how data
  flows across the marketplace.
- A **semantic Q&A** layer lets a consumer ask questions in natural language and
  get SQL-backed answers against the deployed views.
- Source vs. consumer **product-kind** filtering and `:CONSUMES` cross-references.

The marketplace also has **two sub-tabs**: **Data Products** (the finished,
contract-backed products above) and **Datasets** — discovered `:Dataset` nodes
surfaced as lighter catalog entries. A dataset drops off the Datasets tab once
it's been promoted into a product, so the two never double-count the same thing.

### What are equivalent tools in the market?
*(Directional — confirm positioning with the team.)* The catalog/governance
space includes tools like Collibra, Alation, and data.world; the data-product /
mesh space includes things like data product platforms layered on Snowflake/
Databricks. Workbench's differentiator is the **agent-driven build pipeline +
contract + lineage + semantic Q&A in one loop**, not just cataloging.

### I already have products defined as ODCS contracts — reuse or rediscover?
**Reuse.** There's a dedicated **ingest** path (`/product/ingest`,
`/api/ingest-products/*`) that parses an existing ODCS spec, classifies it as
source- or consumer-aligned, matches its declared inputs against existing
marketplace source products, and commits it — no need to re-author from scratch.
The wizard's contract-first flow can also hydrate from an existing spec.

### What is Open Knowledge Format (OKF)?
OKF is a **portable, self-contained briefing bundle** of your published products —
plain markdown files with a little YAML frontmatter, cross-linked to each other by
the same `:CONSUMES` relationships that link the products themselves. It follows
the open **OKF v0.1** shape.

Its job is to be a briefing an **external AI agent or tool that can't reach our
graph or MCP server** can simply read. Think of it as one of three
complementary export formats: **ODCS** is the strict, machine-readable *contract*;
the **dbt project** is the *runnable* SQL that builds the data; and **OKF** is the
*human- and agent-readable narrative* about the products. It's a deterministic,
intentionally lossy projection — it reuses the exact same product view the
marketplace UI shows, and because OKF's links are untyped, it states each
relationship both in prose and in custom frontmatter keys (`consumes:` /
`consumed_by:` / `relationship:`) so nothing about lineage is lost. It is **never a
default pipeline stage** — purely an on-demand export.

### How is OKF used, and how do I get a bundle?
You can export at two scopes:
- a **single product** — `product.md` plus a file per dataset, a `quality.md`, and
  (if present) a deployment-`reflection.md`; or
- the **whole marketplace** — a `catalog.md` index plus a folder per product, with
  cross-product links resolved to bundle-relative paths so a reader can navigate
  between products built on one another.

Three ways to pull one:
- **Web UI** — an "Export Open Knowledge Format" button on a product's detail page
  or on the marketplace listing.
- **REST** — `GET /api/marketplace/products/{contract_id}/okf-bundle` (single) or
  `GET /api/marketplace/okf-bundle` (whole), with `?format=zip|json`.
- **MCP** — the `get_okf_bundle` tool, for an engineer driving Workbench from their
  own agent (scoped to the products they're authorized for).

Reach for OKF when you want to hand product context to an **external LLM or agent**,
snapshot the catalog for stakeholders, or bridge to a tool that reads markdown +
YAML but can't call our APIs. When the other side instead needs the strict schema,
give them the **ODCS** contract; when they need to actually build the data, give
them the **dbt project**.

### Can it push products to Git?
Yes — a product can be pushed to its **own per-product Git repository** on **Gitea
or GitHub**. The push bundles the **serving code** (the runnable package), the
**OKF docs** (`README.md`, `docs/datasets/*`), and the **`odcs.yaml`** contract,
so the repo is a complete, human-readable snapshot of the product. It's driven by
the `serving/push-to-git` and `git-status` endpoints (and the MCP `push_to_git`
tool), with an optional **auto-push** on build. Git integration is **off by
default** — you opt in by configuring a provider and token.

---

## 8. Platform compatibility & deployment

### Which database platforms work?
- **Source discovery/profiling:** **PostgreSQL, MySQL, Snowflake, Databricks, and
  Parquet** — each has its own discovery + profiling skills that produce the same
  graph-loading metadata shape.
- **Migration sources (dmig only):** additionally, **Oracle** and **SQL Server /
  Azure SQL** are reachable as *migration* sources through a SQLAlchemy
  `--reflect` path — that's a raw lift-and-shift reflection, not the full
  discovery/profiling skill set the platforms above get.
- **Serving SQL generation:** a dialect layer emits **PostgreSQL, Snowflake,
  Databricks, BigQuery, or ANSI** SQL (type casts, hashing, regex handling differ
  per dialect).
- **Materialized (dbt) / transfer target:** a per-product target connection,
  defaulting to the source connection if unset.

So the discovery footprint now spans five platforms, with Oracle and SQL Server
reachable specifically through the migration path.

### What is a serving package?
Every serving mode produces a **serving package** — and the package *is* the
execution unit. It's a self-contained, runnable directory carrying its own runner
(`serving_runners/run*.py`) alongside everything the runner needs. When Workbench
"runs" serving, it executes the package's **own runner as a subprocess** and reads
back a `run_result.json`, rather than running serving logic inline. The upshot:
what DWB runs and what you download are literally the same artifact — the package
is **downloadable via REST or MCP the moment Build completes**, and it runs the
same way on your machine or in CI as it does inside the Workbench. (The DQ and
migration packages follow the same "the package is the execution unit" model.)

### Can it run locally? On a server?
Both. Locally it's a FastAPI backend (`uvicorn`) + a Vite React dev server +
Neo4j. For shared/server use there's a containerized stack (`docker compose`)
designed so engineers connect to it as a remote service over MCP — no hardcoded
localhost assumptions, the MCP endpoint is the "front door," and source DBs on
the host are reached via `host.docker.internal`.

To make either path a one-liner there's a stdlib-only **`./dwb` launcher**: it
runs in `--mode host` (local processes) or `--mode compose` (containers) and gives
you `up` / `down` / `status` / `doctor` / `reset` verbs, profile-gated **sample
databases** and a bundled **Gitea**, and a **quick-connect** prefill so a
freshly-started stack lands with connection details already filled in.

### What would it take to add a new data platform?
- **A new serving/target dialect:** add a dialect subclass in the view-DDL
  generator (type casts, hash, regex emission) — a bounded, well-isolated change.
- **A new source platform for discovery/profiling:** add discovery/profiling
  skills for that platform (the MySQL variant is the template) that produce the
  same graph-loading metadata shape.

### Can users bring their own technology stack?
For the database/serving layer, yes within the supported dialects (and via the
extension points above). The AI engine (Claude) and graph store (Neo4j) are the
current build's reference assumptions rather than config-level pluggable today —
chosen to make a concrete, working example, not because the architecture forbids
alternatives.

---

## 9. Roles, workbenches & customization

### What are the principal roles?
Data Product Owner, Data Engineer, Data Steward, Data Quality Analyst, and
Reviewer. Each role has a defined set of stages it can *run* and review surfaces
it can *approve* (e.g. the PO owns source-product validation and marketplace
deploy; the engineer owns mapping and serving; the Steward handles transformation
escalations; the DQA owns DQ rules and scoring). The five-role set is unchanged;
what has grown is the *engineer's* stage-permission set — it now also covers the
cross-platform `deploy_transfer` step and the `dmig_*` migration stages — a
permission detail, not a new role.

### Can roles be customized?
The role→capability mapping is defined in code as the source of truth, so
changing it is a code change rather than an admin setting. The five roles shipped
today are a reference set, not a ceiling — the mapping is a single, well-isolated
place to adjust. Notably, over MCP an engineer can **declare a role** ("switch
hats") to clear a gate they're permitted to act in. *(Confirm whether configurable
roles are on the roadmap.)*

### Why are there separate workbenches for the Product Owner and the Engineer?
Because their jobs are genuinely different. The **Product Owner** thinks in terms
of *products and contracts* — shaping ideas, validating names/descriptions/rules,
browsing the marketplace. The **Engineer** thinks in terms of *pipelines and
sources* — discovery, profiling, mapping, serving. Two persona-scoped shells over
one backend keep each experience focused (and even theme the UI per persona),
with a one-click Switch between them. Product creation is intentionally
PO-initiated; engineers don't see the product-authoring wizards in their "New
Project" flow.

---

## 10. Extensibility, documentation & IP

### Is the implementation locked to how it works today?
No. What ships today is an **opinionated, concrete reference** — PostgreSQL-first, a
specific default workflow, a specific set of skills — chosen so there's a working,
end-to-end example to point at, not because the design is fixed to those choices.
The orchestrator is deliberately separated from the **skills**, the **domain
playbooks**, and the **workflow templates**, so you can plug in new data platforms,
add new domains, swap or extend transforms and quality rules, and even reshape the
core workflow if a different flow suits you better. Read the opinions throughout
this FAQ as a sensible starting point, not a ceiling.

### Can it auto-generate data documentation?
Yes — that's a core capability. The enrichment skills generate **column
descriptions** and **table descriptions** (with a relationship classification like
fact / lookup / specialization), and quality rules + scores are documented in the
graph and surfaced in the marketplace. There's also a downloadable **mapping
rationale report** explaining every column's source, transform, and reasoning.

### Can it be extended with new capabilities?
Yes — this is the point of the opinionated-reference framing above. New
capabilities are added as **skills** (packaged agent units with their own scripts)
and wired into the **stage registry** and **workflow templates**. The architecture
deliberately separates the orchestrator from the skills, so new discovery sources,
quality checks, serving dialects, domains, or advisors slot in without rewiring the
core.

### How does the system improve itself over time?
Through **reflection skills** that run *after* the work, not during it. The
`skill-reflector` and `chat-reflector` analyze past stage runs and chat sessions
and write **proposals** — suggested edits to a skill's instructions or a domain
playbook — into review folders (`playbook/skill_reflections/`,
`playbook/chat_reflections/`). Crucially these are **proposals, not auto-applied
changes**: a human reviews each one and decides whether to fold it into the skill.
So the system accumulates learned improvements to its own prompts and playbooks,
but a person always stays in the loop before anything ships.

### Can I use the agent skills on their own, outside Data Workbench?
Partly — and it's worth being precise about where the seam is, because most
skills use the **knowledge graph as their working memory**, not merely as a
place to file results. The honest split:

- The **discovery and profiling** skills (`data-discovery`, its MySQL variant,
  `data-profiling`) are genuinely portable. Give them a database connection
  string and they emit YAML/JSON files — no graph involved. These lift out
  cleanly.
- Everything downstream of that — the graph-loaders, and the mapping, enrichment,
  quality, scoring, and serving skills — **reads its inputs from the graph and
  writes its outputs back to the graph.** Run in isolation they have nothing to
  read and nowhere meaningful to write. Reusing one standalone isn't a copy-paste;
  it's an integration effort in which *you* own the two ends: supplying the graph
  state the skill expects as input, and routing its output somewhere your system
  understands.

So the skills are a real reuse surface, but the graph-coupled ones come with a
substrate assumption you'd have to satisfy first.

### Which skills are graph-dependent, and which are standalone?
Roughly five bands, from most to least portable:
- **Source-DB standalone** — `data-discovery`, `data-discovery-mysql`,
  `data-profiling`. Input: a DB connection. Output: files. No Neo4j.
- **Graph-loaders** — `data-discovery-to-dcat-neo4j`,
  `data-profiling-to-dqv-neo4j`, `odcs-to-graph`,
  `data-product-spec-to-dprod-neo4j`. These are the **adapter seam**: they take
  the file output of the discovery/profiling stage and load it into the graph
  under the ontology + URI conventions. Everything upstream is portable files;
  everything downstream assumes those nodes exist.
- **Graph-centric** — `data-mapping-neo4j`, `metadata-enrichment`, `data-scoring`,
  `data-quality-rule-generation`, and similar. Their whole job is querying graph
  state for context and MERGE-ing new nodes/edges back.
- **Hybrid (graph + source DB)** — `data-serving-virtual-view` (reads approved
  mappings from the graph, emits SQL files, writes a serving-definition node
  back), the DQ-testing skills, and the remediation skills (which also act
  against the source database).
- **Pure-prompt advisors** — the authoring, marketplace-Q&A, and reflection
  skills have no scripts at all. They reason over a payload the orchestrator
  hands them — and that payload is itself assembled by querying the graph. Used
  outside Workbench, you'd have to reproduce that query-and-inject step yourself.

### If I run a graph-dependent skill standalone, what do I have to supply?
The connection itself is the easy part — every graph-touching script takes Neo4j
host/port/user/password/database as **command-line flags** (with throwaway dev
defaults), so pointing one at your own instance is just arguments. The real work
is reconciling the data contract the skill assumes:
- **A pre-populated graph in the expected shape.** Skills read and write nodes
  keyed by specific URI conventions (e.g. `dataset:{project_code}:{schema}.{table}`)
  and the composed DCAT/DQV/SHACL/PROV-O/ODCS/DPROD ontologies. The
  `--project-code` argument (used by most skills) scopes those URIs.
- **Upstream prerequisites as graph nodes, not files.** For example, the mapping
  skill needs catalog columns, product columns, and approved descriptions to
  already exist as nodes; the serving skill needs approved mappings. Each skill's
  `SKILL.md` spells these out under "Prerequisites."
- **The orchestrator's role, for the script-less skills.** The pure-prompt
  advisors expect their context pre-fetched and injected; standalone, that
  fetch-and-inject is on you.

In practice the cleanest independent-use story is to run the portable
discovery/profiling skills, then either use the graph-loaders to stand up the
graph the rest of the pipeline expects, or adapt their output into your own
system — which is exactly the "you own both ends" seam above.

### What open-source components does it use?
Among others: FastAPI, SQLModel/SQLAlchemy, Neo4j (and its Python driver),
React + react-router, `fastembed` for embeddings, Great Expectations and Pandera
for quality testing, dbt for materialized serving, and PyYAML. The AI layer uses
the Claude Agent SDK. *(Confirm the full license inventory with the team before
external distribution.)*

### What stops someone recreating it from a screenshot?
The visible UI is the thin part. The hard, accumulated value is underneath: the
graph data model spanning six composed ontologies plus custom layers, the agent
skills and their prompts,
the human-in-the-loop review/provenance machinery, the multi-mode
serving compiler (view / dbt / lakehouse / transfer) with FK-bridge join
inference, the project-scoped isolation
model, and the semantic/lineage layers. A screenshot shows the storefront, not
the supply chain.

---

## Suggested additional questions to consider

- **Security & data residency** — Where does my data go? Does any source *data*
  (vs. metadata) leave my environment, and what does the LLM actually see?
- **Concurrency / scale** — How many projects/engineers can run at once; what are
  the stage timeouts and orphan-recovery behaviors?
- **Audit & compliance** — How complete is the provenance trail, and can it be
  exported for governance reviews?
- **Failure handling** — What happens when a stage fails or a generated view
  won't deploy? What are the recovery paths?
- **Onboarding** — What's the fastest path for a brand-new engineer or PO to ship
  their first product?
- **Offline / air-gapped operation** — What works without external network access
  (the embedding model is already self-hosted)?
