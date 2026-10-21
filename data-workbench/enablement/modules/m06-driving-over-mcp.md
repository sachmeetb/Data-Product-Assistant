# Module 6 — Driving Data Workbench over MCP

- **Type:** hands-on project
- **Target length:** ~15 slides
- **Prereq:** Module 2 (a running local stack + a bearer token)
- **Latest-version pointer:** `[PLACEHOLDER: canonical-latest-version location — TBD]`
  *(carry on every deck until the open decision is resolved)*

> **Slide creator brief — Module 6 of 11.** House style is in **Module 0** — follow that
> format for every slide. The learner has a running stack with a bearer token (Module 2).
> **MCP (Model Context Protocol)** is a standard protocol that lets an AI CLI (such as
> Claude Code, Cursor, or Codex) call tools on a remote server using HTTP; the tool list is
> served from the workbench's `/mcp` (141 DE tools) or `/po-mcp` (57 PO tools) endpoints.
> This module drives DW headlessly over MCP — no browser — from the learner's own CLI.
> The key idea: same backend, same graph, same execution as the web UI, accessed through a
> second front door. Screenshots include both terminal output (CLI) and the web UI confirming
> that MCP-started work shows up in the browser.
> **Screenshot placeholders:** insert a **gray placeholder box** at the stated size —
> **Large** = full-width, ~50–60% of slide height (primary visual) · **Medium** = ~half the
> slide, shared with text · **Small** = ~quarter-slide, accent or confirmation shot.
> Do not omit placeholder boxes — they mark where live screenshots are inserted later.

---

## Slide 1 — Driving DW from your own AI CLI
**Beat:** the frame — same backend, a second front door, no browser.

- Everything you did in Module 3/Module 4 by clicking through the two shells, you can also do
  **headlessly from your own Claude Code** (or Codex/Cursor) over **MCP** — no UI, no repo,
  no database credentials on your machine.
- **What you'll do by the end of this module:** install the MCP kit into an empty folder,
  point your CLI at your Module 2 stack, **list your projects**, and **kick off a pipeline stage**
  — then watch it go `running` → `complete` in the web UI.
- This is the same execution path as the UI. Running a stage from MCP and running it from the
  browser land in the *same* server code, load the *same* skill, write the *same* graph.

_Speaker notes:_ Emphasise "same execution, different door" up front — it's the load-bearing
idea of the whole module. The only real difference is where the events go (browser socket vs.
you polling).

---

## Slide 2 — Prerequisites & the thin-client mental model
**Beat:** confirm the start line, then the one picture to hold in your head.

- **You are here (from Module 2):** `./dwb status` is green, the UI is up at
  `http://localhost:5173`, the backend answers `GET http://localhost:8000/api/health`, and
  your admin gave you a **bearer token** scoped to your projects.
- **Your machine is a thin client.** Discovery, profiling, mapping, serving, DQ — all run
  **on the workbench server**, using the server's own skills. You don't install those skills
  and you need **no DB credentials**.

```
Your Claude Code  ──(MCP over HTTP, bearer token)──►  Workbench backend :8000
  + workbench-guide skill                               runs the pipeline
  + workbench MCP tools                                  (Neo4j + Postgres)
```

> **Concept callout — "two doors, one backend."** The web UI and the MCP endpoint are two
> front doors onto the *same* server and the *same* graph. Anything you can do in a shell you
> can do over MCP; the reverse is mostly true too. Pick whichever fits the moment.

> **Concept callout — MCP is a governance boundary, not just a CLI alternative.** Exposing
> tools over MCP wasn't done merely for convenience — it's the mechanism that **enforces
> governance**. External agents (including a data engineer's own Claude Code session) never
> get direct access to the knowledge graph. They only get coarse-grained, governed API calls
> through the workbench's own workflows. When a data engineer initializes a session via the
> install script, a `workbench-guide` skill is loaded locally to teach the agent how to drive
> the workbench tools correctly — but the internal skills that implement the workbench's own
> logic remain opaque. The engineer only ever sees the MCP tool surface, never the
> implementation behind it. This was explicitly validated as "the right architecture" rather
> than treated as an incidental side effect.

---

## Slide 3 — The two front doors
**Beat:** which endpoint is which, and the tool counts that keep going stale.

- **`/mcp` — the Data Engineer door: 141 tools** (`mcp_server.py`). Discovery, profiling,
  mapping, serving, DQ, migrations, the marketplace/OSI/QA read surface, review-write gates.
- **`/po-mcp` — the Product Owner door: 57 tools** (`po_mcp_server.py`). Portfolio triage,
  the contract-first authoring wizard, the source-validation gate, publish.
- A token sees **only one persona's tools** — an engineer token gets the 141, a PO token gets
  the 57. Same auth middleware behind both.

> **Concept callout — use the current numbers.** It's **141 DE / 57 PO**. Older decks say
> **136/38** and one README body says **50** — those are **stale**. If you cite a count in a
> demo, cite 141/57.

> **Concept callout — two endpoints enforce the persona boundary at the *protocol* level.**
> The two MCP endpoints are not two versions of the same thing — they enforce different
> governance scopes at the protocol boundary, not just in UI navigation. A token from the
> engineer endpoint cannot perform product-owner actions, by design. Someone who sees two
> install commands (`engineer-kit` and `po-kit`) may assume one is just newer or more
> complete — the correct frame is that they are different governance scopes. Install the one
> that matches the persona you're operating as.

---

## Slide 4 — The token model
**Beat:** one env var carries who you are, what you can touch, and what you can do.

- The backend reads **`WB_MCP_TOKENS`** — a comma-separated list where each entry is
  **`token[:principal[:projects[:role]]]`**. Your admin sets this on the server; you only ever
  hold the `token` part.

| Form | Meaning |
|---|---|
| `token` | principal `engineer`, all projects, no bound role |
| `token:alice` | principal `alice`, all projects |
| `token:alice:p1\|p2` | restricted to projects `p1`, `p2` |
| `token:alice:*:engineer` | all projects, account role `engineer` |
| `token:bob:p1:owner` | project `p1`, account role `owner` |
| `token:carol:*:viewer` | **read-only** — reads pass, writes refuse |

> **Concept callout — the 4th field is a real security tier, not a label.** `role ∈ {owner,
> engineer, viewer}`. When a token carries a bound role the server uses it and **ignores** any
> self-declared role on review tools — an `owner` token can't reach engineer reviews and vice
> versa. A missing 4th field is back-compatible (unrestricted). **Auth is fail-closed:** no
> `WB_MCP_TOKENS` and no explicit `WB_MCP_ALLOW_INSECURE=1` → the server refuses every request.

---

## Slide 5 — Install: the one-liner
**Beat:** one `curl` wires up every client — no git, no plugin dance.

- **PO** — install the PO kit into the current folder:
  ```bash
  export WORKBENCH_TOKEN="<your-token>"     # local dev in open mode accepts any value, e.g. devtoken
  curl -fsSL http://localhost:8000/api/po-kit/install.sh | bash
  ```
- **Switch → DE** — same shape, engineer kit:
  ```bash
  export WORKBENCH_TOKEN="<your-token>"
  curl -fsSL http://localhost:8000/api/engineer-kit/install.sh | bash
  # target a specific folder instead: ... | bash -s -- ~/path/to/your-project
  ```
- The installer writes **`.mcp.json`** (Claude Code) + `.claude/` skill & commands, plus
  **`.cursor/`** (Cursor) and **`.codex/config.toml` + `AGENTS.md`** (Codex) — every client in
  one shot. **The token stays in your shell env; it's never written to disk.**

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `mcp-installer-run`
Caption: The one-liner installer provisioning a folder.
Must show: A terminal running `curl -fsSL …/api/engineer-kit/install.sh | bash` with
`WORKBENCH_TOKEN` exported, and the installer's output listing the files it wrote
(`.mcp.json`, `.claude/…`, `.codex/config.toml`).
Taken at: Run in an empty folder after the stack is up (Module 2).

---

## Slide 6 — Configure the URL + token (and the client-agnostic path)
**Beat:** the two env vars, plus proof this isn't Claude-Code-only.

- The bundled config reads **two environment variables** — set them in your shell profile
  **before** launching your CLI, then restart it:
  ```bash
  export WORKBENCH_MCP_URL="http://localhost:8000/mcp"        # DE door
  # (PO kit reads WORKBENCH_PO_MCP_URL="http://localhost:8000/po-mcp")
  export WORKBENCH_TOKEN="<your-bearer-token>"
  ```
- **`/mcp` is a standard streamable-HTTP MCP server** authenticated by a plain
  `Authorization: Bearer <token>` header (a static token, **not** OAuth). Any MCP-capable
  client — Codex, Cursor, a raw client — drives it with the **same two values**.
- The only client-installed piece is one **orchestration** skill, **`workbench-guide`** (Codex
  gets an `AGENTS.md` analogue). It teaches your CLI the lifecycle and which tool to reach for;
  the *execution* skills stay on the server.

> **Concept callout — orchestration vs. execution.** `workbench-guide` doesn't do
> data-engineering work. It tells your CLI *how to drive*: pass the project code, scope
> `run_cypher`, poll async runs, re-fetch instead of caching, warn before running a
> non-recommended step. The heavy lifting always runs server-side.

---

## Slide 7 — The paste-once alternative: `/api/bootstrap`
**Beat:** a brand-new user can skip all of the above with one paste.

- `GET http://localhost:8000/api/bootstrap` returns a **self-driving onboarding prompt**.
  Paste the whole thing into a **fresh** CLI session in an empty folder and it does the setup
  *for you* — asks your persona, checks/collects `WORKBENCH_TOKEN`, runs the right installer,
  then tells you exactly how to restart and what to say first.
- Persona-scope it with `?persona=po` or `?persona=de` if you already know which door you want.
- It's the friendliest on-ramp for a teammate who's never touched the kit: **no commands to
  memorise**, the prompt narrates each step in plain terms.

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `mcp-bootstrap-paste`
Caption: The `/api/bootstrap` prompt pasted into a fresh CLI session.
Must show: The bootstrap prompt text pasted in, and the assistant's first response beginning
the guided setup (asking persona / checking `WORKBENCH_TOKEN`).
Taken at: `curl http://localhost:8000/api/bootstrap` (or `?persona=de`), pasted into a new
Claude Code session.

---

## Slide 8 — Verify: the client sees the DW tools
**Beat:** prove the connection before you try to do work.

- **Restart your CLI in the folder** (a fresh launch attaches MCP), with `WORKBENCH_TOKEN`
  exported. The `workbench` MCP server should now be connected.
- Confirm the tools are discoverable — the DE door advertises all **141**, the PO door all
  **57**. If you get *no* tools, re-check the install + URL; if you get an **auth error**,
  re-check the token.

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `mcp-tools-listed`
Caption: The MCP client with the Data Workbench tool surface attached.
Must show: The CLI's MCP/tools view listing `workbench` (or `workbench-po`) tools —
`list_projects`, `get_project_state`, `run_stage`, etc. — proving the server is connected.
Taken at: After restarting the CLI with the kit installed and `WORKBENCH_TOKEN` exported.

---

## Slide 9 — First prompt: "list my projects"
**Beat:** the smoke test — plain language, real output.

- You don't call tools by name. **Talk to the CLI**; the `workbench-guide` skill picks the
  tool. Type:
  > **List my workbench projects.**
- Behind the scenes it calls `list_projects`, which returns an envelope of the projects your
  token can access (`code`, `name`, `archetype`, `domain`). This one call proves the plugin,
  the MCP connection, and your token scope all work.
- Try a couple more: *"What's the state of `<project_code>`?"* (`get_project_state`) and
  *"Show me the datasets and a few column descriptions for it."* (scoped `run_cypher`).

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `mcp-list-projects`
Caption: "List my workbench projects" and the returned list.
Must show: The prompt typed into the CLI and the tool result — a list of projects with their
codes/names/archetypes.
Taken at: First prompt after the connection verifies (Slide 8).

---

## Slide 10 — Kick off a stage from your CLI
**Beat:** the real test — start server-side work over MCP.

- A **fresh** project needs its source configured first. *"Set the data source for
  `<project_code>` to host …, database …, user …"* → `set_data_source`. **From inside the
  container use `host.docker.internal`, not `localhost`.**
- Then just say: **"Run data discovery on `<project_code>`."** Discovery needs *which tables*,
  so the CLI lists them (`get_stage_config_options`), you pick, and it calls **`run_stage`**.
- **`run_stage` is async, fire-and-forget:** it returns a `run_id` immediately and keeps
  running on the server. It does **not** block your session.

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `mcp-stage-kicked-off`
Caption: A pipeline stage started over MCP.
Must show: The prompt "run data discovery on <project>" and the CLI reporting the stage
**started** with a `run_id` (async).
Taken at: After `set_data_source`, running the stage from the CLI.

---

## Slide 11 — Poll for status (the `execution_kind` map)
**Beat:** how you watch async work finish, and pick the right tool every time.

- Because `run_stage` doesn't block, you **poll**: *"Is it done yet?"* → the CLI calls
  `get_project_state` (or the compact `get_plan_summary`). A **new read every turn** — never
  answer a status question from memory.
- Each stage carries an **`execution_kind`** that tells the driver which tool it takes:
  - `llm` / `backend` → **`run_stage`**
  - `mechanical` → **`complete_stage`** (Mark Discovery Complete, Publish, …)
  - `review_gate` → **`review_*`** (or the UI)
- A stage that finishes as **`awaiting_review`** is a two-person gate: you're done; a PO or
  Steward approves it (in the UI, or over MCP via a role-gated `review_*` call).

> **Concept callout — poll, don't cache.** Async runs take minutes. The `workbench-guide`
> skill re-fetches state on every status question and, on each poll, also checks
> `get_pending_questions` so an interactive stage doesn't time out (~300s) to a default.

---

## Slide 12 — See it land in the UI
**Beat:** the cross-door payoff — MCP work is UI work.

- Open the web UI at `http://localhost:5173` and watch the same project. The stage you started
  from your CLI moves **`running` → `complete`** (or `awaiting_review`) in the browser — because
  it's the *same* execution writing the *same* graph.
- This is the moment that makes the "two doors, one backend" idea concrete: nothing about the
  UI knows or cares that MCP started the run.

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `mcp-ui-running-to-complete`
Caption: The stage started over MCP, transitioning in the web UI.
Must show: The Engineering Workbench project view for the same project, with the stage showing
`running` (and/or the after-shot showing `complete`/`awaiting_review`).
Taken at: In the browser at `:5173`, watching the project while the MCP-started stage runs.

---

## Slide 13 — Honest orchestration framing (read this carefully)
**Beat:** separate what's shipped from what's illustrative.

- **There is NO pub/sub in the product.** If you've heard "queue work off a message bus and
  drive it from separate Claude sessions" — that's an **illustrative / future** pattern, not a
  shipped feature. Present it as such.
- **The REAL primitives it would sit on top of, all shipped today:**
  1. **Async fire-and-forget `run_stage` + poll** — the server runs the stage in a background
     task and persists its `StageRun`/`StageExecution` regardless of whether any client stays
     connected. **Multiple sessions can poll the same async run** — that's the grain of truth
     behind the illustrative idea.
  2. **SQL-leased background workers** — the **intake**, **estate-scan/enrich**, and
     **feasibility** workers each claim queued rows via a compare-and-set DB lease (crash-safe:
     stale leases are reclaimed). Started once from the FastAPI lifespan.
  3. **The `/api/intake/submit` REST ingress** — an external tool POSTs recommendations with a
     scoped **machine** credential (`WB_INTAKE_TOKENS`), which stages a submission the leased
     intake worker then parses. *Submission is REST-only — never an MCP token.*

> **Concept callout — build on the real primitives, label the rest.** You *can* legitimately
> have two CLI sessions poll one async run, or wire an external system into `/api/intake/submit`.
> A pub/sub fan-out on top is a design you could add — say "illustrative, not shipped" whenever
> you sketch it, so nobody demos vapor.

---

## Slide 14 — Outcome: what "done" looks like
**Beat:** the concrete end-state you should be sitting on.

- Your own CLI (Claude Code / Codex / Cursor), in an empty folder, is **connected to your Module 2
  stack over MCP** with the right persona door and a scoped token.
- You **listed your projects** and **started a real pipeline stage** — server-side, async —
  from that CLI, without touching the browser to launch it.
- You confirmed the run is not a local trick: it shows up and progresses in the **web UI**.

---

## Slide 15 — Validation & next steps
**Beat:** the green check you run yourself, then where you go next.

- **Validation (run it yourself):**
  1. From your CLI: *"List my workbench projects"* returns your scoped list. ✅
  2. From your CLI: start a stage on a project (`run_stage`), get back a `run_id`. ✅
  3. Poll *"is it done?"* (`get_project_state`) and watch it move `running` → `complete`
     (or `awaiting_review`). ✅
  4. **Cross-check in the browser:** the same stage shows that transition in the UI at
     `http://localhost:5173`. ✅
  - *(Optional isolation check: ask for a project your token isn't scoped to → expect
    "Not authorized for project …".)*
- **Next:** **Module 7 — Data Quality & Pipelines: Independently-Executable Outputs.** You'll see
  that the packages DW generates carry **no DW runtime dependency** — download one and run its
  `run.py` with the stack down.

_Speaker notes:_ If a learner's stage never leaves `running`, have them keep polling (server
stages take minutes) and check `get_pending_questions` — an unanswered interactive prompt is
the usual culprit. If they get 401s, it's the token; if they get *no* tools, it's the restart.
