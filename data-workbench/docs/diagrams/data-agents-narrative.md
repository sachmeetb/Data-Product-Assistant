# Data Agents — Architecture Narrative

A companion to the four `.excalidraw` files in this folder. Use it as a
talking-points cheatsheet when you walk someone through the diagrams, or hand
it to a colleague who needs to pick up the story without you in the room.

## How to use the set

The four diagrams are a deliberate zoom story — each one steps closer to the
metal:

| Diagram | Audience | What it answers |
|---|---|---|
| `data-agents-business.excalidraw` | exec, BD, sponsor, "why does this matter" | What are data agents, where do they apply, and why do I care? |
| `data-agents-conceptual.excalidraw` | architect, new joiner, cross-functional reviewer | What *is* a data agent and what world does it live in? |
| `data-agents-logical.excalidraw` | solution architect, agent designer | What components do I need and how do they relate? |
| `data-agents-technical.excalidraw` | engineer, implementer | What does this look like wired up? (claudecodedash example) |

If you only get five minutes, show the **business** view and say one sentence
about each of the other three. If you have a half-hour, walk all four in
order; the colors mean the same thing across all of them, so the listener
should feel like they're zooming in on the same idea.

## The story in one paragraph

> A *data agent* is an LLM-powered, autonomous-or-semi-autonomous system that
> connects to data, decomposes a request, and acts. We treat agents as
> reusable **building blocks** for data product engineering, semantic-layer
> work, data engineering, data quality, and data modernization. Each agent is
> grounded in a **knowledge graph that is the system's system of record**, not
> just another data source — every action it takes is read from and written
> back to the graph. The same capability can ship in three forms (a
> standalone agent, an MCP endpoint, or a Claude Code agent skill — "Skill-Agent
> Duality"), and can be operated in three modes (one-shot standalone, fixed
> deterministic workflow, or autonomous reasoning loop), with **human-in-the-loop
> available in all three modes**. Our `claudecodedash` workbench is one
> instantiation: it uses the *skill* form, runs them in *deterministic
> workflows* (archetypes), and grounds everything in a project-scoped Neo4j
> knowledge graph.

That paragraph is your elevator pitch. The diagrams exist to make each clause
visible.

---

## Diagram 1 — Business View

**Audience.** Executive, sponsor, BD lead, anyone who needs the *why*. No
implementation detail.

**30-second pitch.** "We build data work as agents — small, governed,
reusable. Each agent stands on a knowledge graph that records every action it
takes, so the whole system is auditable end-to-end. We can package each
capability as an agent, an MCP endpoint, or a skill — same logic, different
delivery — and run it as a one-off, a pipeline, or a fully autonomous workflow,
with humans in the loop wherever you want them."

**Walk order on the canvas.**

1. Start at the **top blue band** — *Where data agents land*. Five domains:
   data product engineering, semantic layer & governance, data engineering,
   data modernization, data quality & observability. Anchor the listener in a
   domain they already care about.
2. Drop down to **Building Blocks** — ten capability tiles (Discovery,
   Profiling, Mapping, Quality Rules, etc.). Point: these are reusable
   *across* the domains above. Same Discovery agent serves modernization and
   data product engineering.
3. Move to the **center amber cylinder — Knowledge Graph as System of
   Record**. This is the punch line. Spend 60 seconds here.
4. Then split left/right: left column = **Three Modes of Use**, right column =
   **Three Materialization Forms**. Show that these are orthogonal — a single
   capability can be a *skill in a workflow* or a *standalone agent in
   autonomous mode*.
5. Bottom: **Source ↔ KG ↔ Consumers** flow. KG sits between sources and the
   semantic layer that BI tools, apps, and other agents consume.
6. Finish on the **claudecodedash badge** — point at it and say "and here's a
   working instance".

**Five key messages.**

1. **Data agents are reusable.** Build once, apply across domains. Don't
   confuse "agent that does X for project A" with "agent that does X" —
   factor the latter.
2. **The knowledge graph is the system of record, not a data source.** Every
   agent action is grounded in DCAT / DQV / SHACL / PROV-O. That's what makes
   the system *auditable* and what lets you run agents over each other's
   output without losing provenance.
3. **Three modes, not one.** Standalone for one-shots, deterministic
   workflow for predictable pipelines, autonomous for genuinely exploratory
   work. Don't reach for autonomous when an `if` statement would do —
   "agentic bias" is a real failure mode.
4. **Three materialization forms, non-exclusive.** The same capability ships
   as a skill (low-latency, in-process), as an MCP endpoint (any IDE /
   workbench / CLI), or as a standalone agent (independent process). Pick
   the form by where you need the capability to run, not by religion.
5. **Human-in-the-loop applies in all three modes.** Review, approve, edit,
   *escalate*. Provenance attaches to every human decision so the next
   agent run is smarter.

**Likely questions.**

- *"Isn't this just a data pipeline?"* No — a pipeline runs fixed code; an
  agent makes decisions inside the step. We can run *agents in pipelines*
  (mode 2) or as one-offs (mode 1) or in reasoning loops (mode 3). The
  pipeline metaphor only covers one of the three modes.
- *"Why a knowledge graph instead of just a database?"* Because the graph
  encodes both the *artifacts* (columns, rules, scores) and the
  *relationships* (what column maps to what product field, who reviewed
  what, what derived from what) in a single substrate that's queryable by
  the next agent in line. PROV-O makes the audit free.
- *"How is this different from Tool X?"* Most "agentic" tools pick one
  materialization form and one mode and lock you in. The Skill-Agent
  Duality framing lets you start cheap (skill in IDE) and graduate to
  autonomous remote agents only when the cost is justified.

---

## Diagram 2 — Conceptual View

**Audience.** Architect, new joiner, anyone who needs to know what a data
agent *is* before talking about how it's built.

**30-second pitch.** "A data agent has internal anatomy — task management
plus intelligence — and lives in a four-plane world: it's controlled,
executes against tools, talks to a data plane, and is observed. Most agents
work in a 3-tier hierarchy: an Orchestrator routes, a Super Agent decomposes
the goal, and Utility Agents do the focused work. The knowledge graph is
their system of record."

**Walk order on the canvas.**

1. Read the **header definition** out loud — vault-sourced, one sentence.
2. Drop to the **3-tier hierarchy in the center** (indigo). Orchestrator →
   Super → five Utility Agents. Note the rose dashed *Human review gate*
   between the tiers — that's the HITL touchpoint.
3. Pull out the **right-side zoom callout** — *Inside any Utility Agent*.
   Two pillars: Task Management (plan + execute) and Intelligence (LLM,
   SLM, memory, tools). This is the "anatomy" everyone forgets.
4. Drop to the **amber Data & Knowledge band**. Point at the middle
   cylinder — Knowledge Graph (System of Record). Note the heavier border —
   that's intentional. The other two cylinders (Data Products / Source
   Systems) are *adjacent* to the graph, not equivalent.
5. Right vertical strip — **Observability & Governance**. Conversation
   ledger, agent metrics, quality scores, HITL reviews, reflection loop.
   Every tier of agent emits into this strip.
6. Bottom-center: paired insets — **Three Modes of Use** and **Three
   Materialization Forms**. Land here so the listener leaves with the
   business-view vocabulary too.

**Five key messages.**

1. **An agent is not a chat box.** It has Task Management and Intelligence
   pillars. If your "agent" only has Intelligence, it's an LLM call.
2. **The 3-tier model scales.** Orchestrator handles "what next", Super
   handles "what does this mean", Utility does the work. Don't put
   reasoning in the Orchestrator or routing in the Utility.
3. **The knowledge graph is the centerpiece of the data plane**, not a
   peer of the source systems. The diagram makes this visible with the
   border weight.
4. **Observability is a first-class plane**, not an afterthought. The
   reflection loop (skill-reflector / chat-reflector) reads from
   Observability and feeds Control — that's how the system improves.
5. **HITL is structural**, not bolted on. The diamond between Super and
   Utility is the architectural acknowledgment that humans are part of
   the runtime, not just the design loop.

---

## Diagram 3 — Logical View

**Audience.** Solution architect or agent designer about to start a build.
This is the "what do I need to provision" diagram.

**30-second pitch.** "Four planes laid out as swim-lanes — Control,
Execution, Data & Knowledge, Observability. User surfaces feed in from the
left, human-in-the-loop reviews drop out the right. Skills are portable
across harnesses. MCP is the universal tool bus. Each lane has a concrete
claudecodedash instantiation tag pinned beneath it."

**Walk order on the canvas.**

1. **Read the lanes top to bottom.** Control → Execution → Data &
   Knowledge → Observability.
2. In **Control** (indigo): Orchestrator, Archetype Registry, Stage
   Registry, Dependency Graph, Run-state Store. Point at Stage Registry —
   that's where the agent <-> work binding happens.
3. In **Execution** (emerald): Agent Harness, Tool Surface, LLM, the
   Skills Library hex grid, and the **MCP bus** at the bottom. Show that
   skills live above the harness *and* can be exposed below it via MCP —
   the same skill, two delivery modes.
4. In **Data & Knowledge** (amber): four cylinders. The leftmost — *Knowledge
   Graph — System of Record* — has the heavy border. The Data Products,
   Source Systems, and Playbook cylinders are *adjacent* knowledge stores
   that the agent reads but isn't grounded in.
5. In **Observability** (slate): Stage Execution Ledger, Chat Session
   Ledger, Quality Scores, Provenance, Reflection / Learning Loop. The
   *learning loop* is a curved feedback arrow that goes back up to
   Control — show this as the long bent line.
6. Right gutter — **HITL queues** (descriptions, mappings, domain rules,
   escalations, unmapped columns) plus structured rejection categories.
7. Top-right corner — paired **Modes / Materialization Forms** insets.
8. Bottom strip — claudecodedash instantiation tags pinned under each
   lane (`archetypes.py` … `sdk_runner.py` … Neo4j … `workbench.db`).

**Five key messages.**

1. **Four planes, not two or three.** Control vs Execution vs Data vs
   Observability. Most "agent architectures" merge Control and Execution,
   which is why they're hard to reason about.
2. **MCP is a bus, not an agent.** It connects skills to external systems.
   It is *not* the orchestration protocol. Don't try to use MCP for
   agent-to-agent.
3. **Skills are portable.** A skill defined as SKILL.md + scripts + refs
   can run inside Claude Code today and ship as an MCP server tomorrow
   without rewriting the logic. The Execution lane shows the dual
   placement.
4. **Observability feeds back into Control.** The reflection loop is what
   turns logs into prompt and skill improvements — that's the long
   curved arrow.
5. **Every lane has a working instance** — the green tags pinned beneath
   each lane prove it.

---

## Diagram 4 — Technical View (claudecodedash worked example)

**Audience.** Engineer about to read or modify the code.

**30-second pitch.** "Three tiers — Client, Backend & Agent harness, State
& Data. A bold purple ① → ⑩ trace shows one stage execution end-to-end.
Two runners (`sdk_runner` for stages, `chat_runner` for the Ask drawer)
sit side by side; both invoke the Claude Code SDK with different tool
allowlists. Skills live outside the repo at `~/.claude/skills/`. Neo4j is
stamped as the *system of record*. There's a reflection loop in the data
tier."

**Walk order on the canvas.**

1. **Trace the numbered path 1 through 10.** Read out:
   - ① UI click (Engineer Workbench)
   - ② WebSocket `run_stage`
   - ③ FastAPI router → Pipeline orchestrator looks up STAGE_REGISTRY
   - ④ `build_prompt()` injects connection details + skill-load directive
   - ⑤ `sdk_runner.run_stage_streaming` → `claude_code_sdk.query()` (LLM)
   - ⑥ Agent loads SKILL.md from `~/.claude/skills/{skill}`
   - ⑦ Skill scripts hit Postgres / MySQL / Neo4j (via `run_cypher.py`)
   - ⑧ Streaming events flow back through SDK → WebSocket → UI
   - ⑨ Persisted to `StageExecution.log_json` and Neo4j
   - ⑩ Later: reflection loop reads StageExecution, the `skill-reflector`
     skill writes proposals to `playbook/skill_reflections/`

2. **Highlight the two runners.** `sdk_runner` (full tool allowlist,
   `acceptEdits`, max 100 turns, anti-exploration system prompt) versus
   `chat_runner` (read-only, max 15 turns, embedded credentials, an
   explicit *Forbidden* list to prevent the agent from reading
   `workbench.db` or grepping for secrets — both observed failure
   modes).

3. **Highlight the Skill cluster on the right.** All 24 skills used by
   the workbench — pipeline skills, reflection skills, advisor skills,
   chat assistant skills. Point: they all live outside the repo, so
   they're shareable across other Claude Code projects.

4. **Drop to the data tier.** `workbench.db` (SQLite — operational
   metadata), Neo4j (system of record), source DBs, filesystem. Note the
   reflection-loop inset — it's the closing of the loop from
   Observability back into the agent system.

5. **Materialization note** in the footer: claudecodedash uses the
   *skill* form, but the same capabilities could ship as MCP endpoints
   or standalone agents — Skill-Agent Duality.

**Five key messages.**

1. **Two runners, two safety profiles.** Stage execution gets full tools;
   chat is read-only with a hostile-prompt-resistant system message. The
   asymmetry is deliberate.
2. **Skills are repo-external on purpose.** They live in `~/.claude/skills/`
   so they're not tied to claudecodedash — they can be loaded by any
   Claude Code session.
3. **Project isolation is enforced at the script layer.** Every Cypher
   call goes through `run_cypher.py --project-code <code>` which refuses
   queries that don't reference the project code. That's the choke point
   for cross-project safety.
4. **Streaming is bidirectional.** Agent events flow up to the UI; user
   responses flow back down via `agent_ask.py` (parked HTTP, relayed via
   WebSocket). The agent can ask the human a question mid-run.
5. **The reflection loop is what makes this a learning system.** Without
   it, the workbench would just be a clever pipeline. With it, every
   stage run feeds prompt and skill improvements.

---

## Common questions across the whole set

- **"Does the agent have memory?"** Yes — three kinds. Inside one run, the
  Claude Code SDK keeps conversation context. Across runs, the
  knowledge graph is the persistent memory (system of record). Across
  *projects*, the playbook (domain catalogs, ODCS templates,
  reflections) is the shared memory.
- **"Where does the LLM choice show up?"** It's host-config in
  claudecodedash — the SDK doesn't hard-code a model. The conceptual
  diagram doesn't put it in the front because the model choice is a
  knob, not a structural element.
- **"How do you stop the agent from going off the rails?"** Three
  mechanisms visible in the diagrams: (1) tool allowlists per runner,
  (2) anti-exploration system prompts, (3) project-scoped script
  choke points like `run_cypher.py`. Plus HITL gates wherever the
  business needs them.
- **"Why two workbench shells (Product / Engineer) if it's the same
  backend?"** Persona scoping is enforced in the frontend; the same
  backend serves both. Different role, different surfaces, same data.

## Glossary (one-line definitions for diagram labels)

- **Archetype**: a named template of workflows (e.g. `dpe-cf` =
  data-product-engineering contract-first).
- **Workflow group**: a named set of stages (e.g. `metadata_enrichment`)
  that can be repeated.
- **Stage**: an atomic unit of work in a workflow, almost always backed
  by a skill.
- **Skill**: SKILL.md + scripts + reference materials, living outside
  the repo at `~/.claude/skills/`.
- **MCP**: Model Context Protocol — open standard for exposing tools to
  any client (IDE, workbench, CLI). "Universal USB cable for tools."
- **HITL**: Human-in-the-Loop. Review, approve, edit, escalate.
- **PROV-O**: W3C provenance ontology. Every artifact in the graph
  carries who-derived-what-from-what.
- **DCAT / DQV / SHACL / DPROD / ODCS**: data-domain ontologies the
  knowledge graph uses for schema, quality, validation, products, and
  contracts respectively.

## Sources

Vault citations are inline in each diagram's footer. Primary anchors:

- *Building and Evaluating Data Agents.md* — definition
- *The Anatomy of an Autonomous Agent.md* — anatomy + properties
- *AI Refinery Overview April 2025.md* — 3-tier hierarchy
- *Niel & Faheem 4-30-2026* — knowledge graph as system of record
- *NTSF Skills & Code-Gen Demo 3-10-2026* — skill / agent / MCP duality
- *From Claude Code Skills to Standalone Agents* — Skill-Agent Duality
- *Niel & Alfredo 6-9-2025*, *Agentic AI Bias* — three modes of use
- *Project Colorado 9-10-2025*, *Google planning 2-10-2026*, *Dell
  Workshop 5-1-2026* — HITL across modes
- *Everything a Developer Needs to Know About MCP* — MCP framing

Code citations:
`workbench/backend/{archetypes.py, sdk_runner.py, chat_runner.py,
pipeline.py, main.py}`, `CLAUDE.md`, `docs/architecture.md`, `~/.claude/skills/`.

---

*Rev: 2026-05-08. Companion to `data-agents-business.excalidraw`,
`data-agents-conceptual.excalidraw`, `data-agents-logical.excalidraw`,
`data-agents-technical.excalidraw` in this folder.*
