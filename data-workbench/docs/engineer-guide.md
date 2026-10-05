# Data Workbench — Data Engineer Guide (Claude Code access)

Work on Data Workbench projects from **your own Claude Code**, in any folder.
This guide covers setup, basic usage, and a guided first session. It doubles as
the walkthrough for validating the MCP access end-to-end.

---

## 1. Mental model (read this first)

The Data Workbench is a **remote service**. Your machine is a **thin client**:

- The data-engineering work — discovery, profiling, mapping, serving, data
  quality — **runs on the workbench server**, using the server's own skills.
- **You don't install those skills and you don't need any database
  credentials.** You drive the pipeline through a small set of **MCP tools**.
- The only thing that runs in *your* Claude Code is one **guide skill**
  (`workbench-guide`) that teaches Claude Code the lifecycle and which tools to
  call.

```
Your Claude Code  ──(MCP over HTTPS, bearer token)──►  Workbench server
  + workbench-guide skill                                 runs the pipeline
  + workbench MCP tools                                    (Neo4j + Postgres)
```

Both you and the workbench's own web UI talk to the **same** server — you just
reach it through MCP instead of the browser.

---

## 2. Prerequisites

1. **Claude Code** installed (`claude` in your terminal, or the VS Code
   extension).
2. From your workbench admin, two values:
   - **MCP URL** — e.g. `http://localhost:8000/mcp` (local), or your team's
     host/container URL.
   - **Bearer token** — scoped to the projects you're allowed to work on.

> You do **not** need: the workbench repo, the pipeline skills, Neo4j/Postgres
> credentials, or Python. Just Claude Code + the URL + the token.

---

## 3. Setup

### Quick install (one-liner, from the backend)

If your admin's workbench backend is reachable, install everything — the MCP
connection **and** the `workbench-guide` skill + `/workbench-*` slash commands —
straight into a project, no git/plugin steps:

```bash
curl -fsSL http://<backend-host>:8000/api/engineer-kit/install.sh \
  | WORKBENCH_TOKEN=<your-token> bash -s -- ~/path/to/your-project
```

It writes `<project>/.mcp.json` (the `workbench` server, pointing at that
backend's `/mcp`; your token stays in the `WORKBENCH_TOKEN` env, never on disk)
and drops the skill + commands into `<project>/.claude/`. Then `cd` into the
project, make sure `WORKBENCH_TOKEN` is exported, launch Claude Code, and ask
*"List my workbench projects."* Re-run any time (idempotent). The manual plugin
route below is the alternative if you'd rather install the kit globally.

### 3a. Install the engineer kit plugin

The kit is one Claude Code plugin that registers the `workbench` MCP server and
installs the `workbench-guide` skill.

In Claude Code, add the marketplace and install the plugin:

```
/plugin marketplace add <your-workbench-repo>
/plugin install workbench-engineer@data-workbench
```

- `<your-workbench-repo>` is the Data Workbench repo (your admin will give you
  the exact value — a git URL, an `owner/repo`, or a local path for testing).
- After install, restart Claude Code (or run `/reload-plugins`).

### 3b. Configure the URL + token

The plugin reads two environment variables. Set them in your shell profile
(`~/.bashrc`, `~/.zshrc`, …) **before** launching Claude Code:

```bash
export WORKBENCH_MCP_URL="http://localhost:8000/mcp"   # from your admin
export WORKBENCH_TOKEN="<your-bearer-token>"           # from your admin
```

Then start Claude Code (or `/reload-plugins`).

> **If your Claude Code version doesn't expand `${VAR}` inside the plugin's
> `.mcp.json` headers**, register the server directly instead (token is saved to
> `~/.claude.json`):
>
> ```bash
> claude mcp add --transport http workbench "$WORKBENCH_MCP_URL" \
>   --header "Authorization: Bearer $WORKBENCH_TOKEN"
> ```

### 3c. Use a different MCP client (Codex, etc.)

The engineer kit (§3a) is a **Claude Code convenience wrapper**, but the
Workbench itself is **client-agnostic**: `/mcp` is a standard **streamable-HTTP
MCP server** authenticated with an `Authorization: Bearer <token>` header. Any
MCP-capable client can drive it with the **same two values** — the MCP URL and a
token from `WB_MCP_TOKENS`. The bearer is a **plain static token**, not OAuth —
the server advertises no protected-resource metadata, so a static-bearer client
connects directly (if you ever saw Codex report `Auth: Unsupported`, that was the
old OAuth advertisement and is fixed).

**The one-liner installer handles Codex too.** `curl … | install.sh` (the
Quick-install at the top of §3) provisions both clients in one shot:
- Claude Code → `<project>/.mcp.json` + `<project>/.claude/{skills,commands}`
- **Codex → `<project>/.codex/config.toml`** (the `workbench` MCP server) **+
  `<project>/AGENTS.md`** (the lifecycle + **status-cadence** guidance — Codex's
  analogue of the `workbench-guide` skill; it auto-reports plan progress via
  `get_plan_summary`).

Each client ignores the other's files. The control plane is identical — all §4
tools (incl. `query_semantic_layer` and `get_plan_summary`) work regardless of
client. **Three gotchas to know with Codex:**
- **Trust the folder.** Codex loads a project-scoped `.codex/config.toml` only
  for **trusted** projects — approve it when `codex` prompts, or the `workbench`
  server won't appear (`list_mcp_resources` returns empty).
- **Export the token.** Codex reads it via `bearer_token_env_var` — `export
  WORKBENCH_TOKEN=<token>` in the shell that launches `codex`.
- **Reachability.** The MCP URL must be reachable from where `codex` runs;
  `localhost:8000` only works on the host running the backend (use the host's
  address otherwise). Codex's bubblewrap sandbox blocks an ad-hoc `curl` test,
  but its MCP client connects outside that sandbox.

**Manual / global alternative.** To set Codex up by hand (or register the server
globally for every folder), add it to `~/.codex/config.toml` instead and copy
`engineer-kit/AGENTS.md` into the project:

```bash
export WORKBENCH_TOKEN="<your-bearer-token>"   # from your admin
```

```toml
# ~/.codex/config.toml  (or <project>/.codex/config.toml — the installer writes the latter)
[mcp_servers.workbench]
url = "http://localhost:8000/mcp"          # your admin's MCP URL
bearer_token_env_var = "WORKBENCH_TOKEN"
enabled = true
startup_timeout_sec = 20
```

> Requires a recent Codex with **streamable-HTTP MCP** support
> ([openai/codex#4317](https://github.com/openai/codex/pull/4317)); older builds
> are stdio-only and need an `mcp-remote` stdio→HTTP bridge pointed at the same
> URL + header.

### 3d. Verify the connection

In Claude Code, type:

```
List my workbench projects.
```

You should see Claude use the `workbench-guide` skill and call
`mcp__workbench__list_projects`, then list the projects your token can access.
If you get an auth error, re-check the token; if you get *no* tools, re-check
the install + the URL (see Troubleshooting).

---

## 4. The tools

The server exposes **142** MCP tools (`workbench/backend/mcp_server.py`). You
rarely call these by name — just describe what you want and the
`workbench-guide` skill picks the right tool for each stage's `execution_kind`.

This guide does **not** restate the full surface — the canonical, always-current
per-tool reference lives in **[mcp-architecture.md](mcp-architecture.md)**
(every tool, its arguments, and how the agentic tools run as server-side
sub-agents). At a glance the tools fall into execution classes (a representative
selection — see the canonical reference for all 142):

- **Read** (never mutate) — `list_projects`, `get_project_state`,
  `get_plan_summary`, `get_stage_results`, `run_cypher`, `get_dataset_filter`,
  `get_dataset_transform`, `get_dbt_project`, `get_okf_bundle`.
- **Mapping / serving diagnostics** (read) — `get_mapping_graph`,
  `get_unmapped_columns`, `get_stale_mappings`, `get_mapping_rationale_report`,
  `get_join_preflight`, `get_upstream_drift`.
- **Interactive** (a paired read + write that drives a mid-run question) —
  `get_pending_questions`, `answer_question`.
- **Mechanical / lifecycle** (mutate state directly, no agent) —
  `set_data_source`, `get_stage_config_options`, `reset_stage`,
  `complete_stage`, `set_serving_mode`, `select_exclusive_group`,
  `set_materialization_target`, `set_dataset_filter`, `set_dataset_joins`,
  `rebind_stale_mapping`.
- **Workflow catalog** — `list_available_workflows`, `add_workflow`,
  `remove_workflow`.
- **Materialization gate** — `get_materialization_status`,
  `build_materialization_sample`, `approve_materialization_full`,
  `reject_materialization_sample`.
- **Requests / assignments** (the PO ↔ engineer loop) — `list_assignments`,
  `get_assignment`, `accept_request`, `reject_request`,
  `list_rejection_categories`, `request_source_candidates`,
  `send_upstream_pushback`.
- **Agentic** (start a single-skill sub-agent) — `run_stage`,
  `query_semantic_layer`.
- **Semantic discovery** (domain-scoped, role-gated) —
  `get_semantic_discovery_status`, `run_semantic_discovery_step`,
  `reset_semantic_discovery`.
- **Review-write** (clear a human review gate; role-gated, PROV-O attributed —
  see §4a) — `review_description`, `review_mapping`, `review_domain_rule`,
  `review_table_description`, `review_relationship_description`.

The handful you reach for constantly while driving a project from your own
Claude Code:

- `get_project_state` — your "where are things?" tool. Each stage carries an
  **`execution_kind`** (`llm`/`backend` → `run_stage`, `mechanical` →
  `complete_stage`, `review_gate` → `review_*`/UI) so the driver picks the right
  tool on the first try.
- `get_plan_summary` — the compact "what's done / what's next" digest; the
  `workbench-guide` skill calls it automatically to keep you posted on progress.
- `get_stage_results` — **reach for this first** to review a stage's output (the
  same vetted query the web dashboard uses — no hand-written Cypher, no
  cross-join risk). Pick a `card`; pass `table=` to scope to one dataset.
- `set_data_source` — configure the source DB connection; **required before
  Data Discovery**. From inside the container, a source DB on the host is
  `host.docker.internal`.
- `run_stage` — start a pipeline stage **running on the server**; returns a
  `run_id` and keeps running. **Poll `get_project_state`** to watch it finish.
- `complete_stage` — the MCP equivalent of the UI's "Complete" button for
  mechanical lifecycle stages (Mark Discovery/Engineering Complete, ODCS→dprod,
  Publish, Synthesize ODCS, Auto-Map). Refuses data-agent/DQ/review stages with
  guidance.
- `query_semantic_layer` — ask the **marketplace semantic layer** an NL question
  (`domain`, `question`); returns a markdown answer (text + result table + SQL).
  Read-only; domain-scoped.

### 4a. Approving reviews & switching role

The five `review_*` tools **write** — they sign off a human review gate, wrapping
the same handlers the web UI POSTs to, so the PROV-O audit trail and the
automatic stage-flip are identical. Two things to know:

- **Attribution.** Writes are recorded as your token's **principal** (the
  engineer's email/label), acting as a human reviewer.
- **Declared role / "switching hats".** Each `review_*` call requires you to
  declare the **`role`** you are acting as, and the server enforces it per
  surface (the role table in `../CLAUDE.md`). If you try to approve something
  your role can't — e.g. approving descriptions as a `Data Engineer` — the tool
  tells you which role is required; **re-call with that role**
  (e.g. `role="Data Steward"`). That is how an engineer clears steward- or
  DQA-gated items from their own Claude Code without leaving the terminal.

Workflow: read the queue with `get_stage_results` (cards `descriptions` /
`mappings` / `dq_rules` / `datasets` / `relationships`), approve/edit with the
matching `review_*` tool, then confirm the stage flipped via `get_project_state`.
`get_stage_results` rows already carry the `desc_uri` / `mapping_uri` / `rule_uri`
the write tools need, so read→approve composes with no extra Cypher.

Key behaviors:

- **`run_stage` is asynchronous.** It does not block. A stage can take minutes;
  re-ask "what's the status of project X?" to poll.
- **Stages that need configuration.** Some stages need choices *up front* —
  notably **Data Discovery**, which needs *which tables* to ingest. Fetch the
  options with `get_stage_config_options`, then pass your selection to
  `run_stage` (`config={"discovery_tables": "schema.t1, schema.t2"}`). Claude
  Code handles this for you when you say *"run discovery on X"* — it lists the
  tables, you pick, it runs. So **discovery works fully from Claude Code** — no
  UI needed.
- **Stages that ask mid-run.** If a stage pauses with a question, Claude Code
  sees it via `get_pending_questions`, shows you the prompt + options, and
  relays your answer with `answer_question`. Answer promptly — questions time
  out (~300s) to a default.
- **Two-person gates.** Some stages finish as `awaiting_review`. That means
  *you're done; a Product Owner or Steward approves it in the web UI.* That's by
  design — the approver side stays in the browser.

---

## 5. Basic usage

Just talk to Claude Code in plain language. Examples:

- "What projects am I working on?"
- "What's the state of `dpe-06032026-01`? Which stage is next?"
- "Run data discovery on `dpe-06032026-01`." → then: "Is it done yet?"
- "Show me the columns and descriptions for that project."
- "Show me the column mappings and their status."
- "Reset the mapping stage so I can re-run it, then run it again."

A typical loop:

1. **Orient** — "what's the state of project X?" → `get_project_state` (read
   each stage's `execution_kind` to know which tool drives it).
2. **Configure the source** (fresh project, before discovery) — "set the data
   source for X to …" → `set_data_source`.
3. **Run the next stage** — "run `<stage>` on X" → `run_stage` (data-agent /
   backend stages). For a mechanical lifecycle stage (Mark Discovery Complete,
   ODCS→dprod, Publish, …) Claude uses `complete_stage` instead.
4. **Watch** — "is it done?" → Claude polls `get_project_state`.
5. **Inspect** — "show me what it produced" → Claude uses `get_stage_results`
   (preferred) or `run_cypher`.
6. **Approve a review** (in a role you can act in) — "approve the descriptions as
   a Data Steward" → `review_*` tool; then confirm the stage flipped (§4a).
7. **Fix + re-run** if needed → `reset_stage` + `run_stage`.
8. **Hand off** — remaining two-person gates can be cleared by the PO/Steward in
   the web UI, or by you via `review_*` if you're acting in the required role.

---

## 6. Guided first session (validation walkthrough)

Do this once to confirm everything works end-to-end. **Run these in a separate
Claude Code session, in any empty folder** (you are acting as the engineer).

**Step 1 — Setup.** Complete §3 (install plugin, set `WORKBENCH_MCP_URL` +
`WORKBENCH_TOKEN`, restart Claude Code).

**Step 2 — Connectivity.** Ask: *"List my workbench projects."*
✅ Expect: a list of projects (code, name, archetype). This proves the plugin,
MCP connection, and your token all work.

**Step 3 — Inspect a project.** Ask: *"What's the current state of
`<project_code>`? Show each stage and its status."*
✅ Expect: the stages grouped by workflow with statuses.

**Step 4 — Query the graph.** Ask: *"Show me the datasets and a few column
descriptions for `<project_code>`."*
✅ Expect: rows from the knowledge graph (proves read-only scoped `run_cypher`).

**Step 4a — A fresh project needs a data source first.** If `<project_code>` is
brand new and you intend to run **Data Discovery**, the source DB must be
configured first. Ask: *"Set the data source for `<project_code>` to host …,
database …, user …"* — Claude calls `set_data_source` (which also completes the
`select_data_source` stage). Discovery enumerates tables from that connection, so
`get_stage_config_options` returns `{needs_data_source: true}` until it is set.
From inside the container, use `host.docker.internal` (not `localhost`) for a
source DB running on the host.
✅ Expect: `set_data_source` returns `ok`, and asking for the discovery stage's
config options now lists schemas/tables.

**Step 5 — Run a stage (the real test).** Ask: *"Run `<stage_name>` on
`<project_code>`."*
✅ Expect: Claude reports the stage **started** with a run id. Then ask *"Is it
done yet?"* a few times.
✅ Expect: the stage moves `running` → `complete` (or `awaiting_review`). This
proves server-side execution via MCP.

**Step 6 — Confirm the result.** Ask: *"Show me what that stage produced."*
✅ Expect: the new graph state (e.g. descriptions, mappings) via `run_cypher`.

**Step 7 — Isolation check (optional).** Ask Claude to query a project your
token is **not** scoped to.
✅ Expect: *Not authorized for project …* — the server enforces per-engineer
scope.

If all steps pass, the engineer access path is validated.

---

## 7. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| "No workbench tools available" | Plugin not installed or not reloaded. Re-run `/plugin install`, then `/reload-plugins` or restart Claude Code. |
| Auth / 401 errors | Token missing or wrong. Re-check `WORKBENCH_TOKEN`; if `${VAR}` isn't expanding in the plugin config, use the `claude mcp add --header` form in §3b. |
| `list_projects` returns empty | Your token may be scoped to projects that don't exist yet, or the URL points at an empty/different workbench. Confirm `WORKBENCH_MCP_URL` with your admin. |
| "Not authorized for project X" | Expected if your token isn't scoped to X. Ask your admin to widen your token's project scope. |
| `run_stage` says "not runnable" | That stage isn't a data-agent stage — it completes via `complete_stage` (a mechanical lifecycle step) or is a review gate, or the stage number/workflow is wrong. Check the stage's **`execution_kind`** in `get_project_state` and pick the matching tool: `llm`/`backend` → `run_stage`, `mechanical` → `complete_stage`, `review_gate` → `review_*`. |
| "Role X cannot approve … reviews" | A `review_*` call declared a `role` that can't act on that surface. Re-call with one of the roles the error names (e.g. switch from `Data Engineer` to `Data Steward` to approve descriptions). See §4a. |
| `get_stage_config_options` returns `{needs_data_source: true}` | The source DB isn't configured. Call `set_data_source` first (§4a / Step 4a). If it instead returns an `error`, the connection is set but unreachable — fix the host/creds. |
| A stage seems stuck in `running` | Server-side stages take minutes; keep polling. The server auto-recovers genuinely orphaned runs after a timeout. |

---

## Appendix — running a workbench backend for the engineer to point at (admin)

For the engineer test to be meaningful, `WORKBENCH_MCP_URL` must point at a
backend that **has projects** and is reachable. To run one locally with auth on:

```bash
cd <workbench-repo>
export ANTHROPIC_FOUNDRY_API_KEY="<foundry-key>"        # the engine's auth
export ANTHROPIC_FOUNDRY_RESOURCE="<resource>"           # required with the key
# ...or instead: ANTHROPIC_API_KEY, or CLAUDE_CODE_OAUTH_TOKEN from
# `claude setup-token` if this box already has a Claude Code subscription.
export WB_MCP_TOKENS="<token>:<engineer-label>:*"        # MCP bearer token(s)
env/bin/uvicorn workbench.backend.main:app --port 8000
```

- `WB_MCP_TOKENS` format: `token`, `token:principal`, or
  `token:principal:proj1|proj2` (`:*` or omitted = all projects). Give the
  engineer the `token` part as their `WORKBENCH_TOKEN`.
- The containerized stack (`docker compose up`) is a *fresh, empty* environment
  (its own volumes) — fine for a deployment smoke test, but for the engineer
  walkthrough point at a backend that already holds your projects.
- For the plugin marketplace during local testing, engineers can
  `/plugin marketplace add <path-to-this-repo>` (a local path works).

### Containerized stack: reaching the source Postgres + the validated flow

When the backend runs in a container, a project's `pg_connection` must use
**`host.docker.internal`** (not `localhost`) to reach a Postgres on the host,
and the compose file gives the backend that route via
`extra_hosts: ["host.docker.internal:host-gateway"]`. Example connection a PO/
engineer enters in the UI:

```
postgresql://USER:PW@host.docker.internal:5433/your_source_db
```

New projects created against the container automatically point at the compose
Neo4j (the `ProjectCreate` Neo4j defaults fall back to `AppSettings`, which is
env-driven), so no manual Neo4j wiring is needed.

**Validated end-to-end flow (container stack):**
1. **UI** — create a project, set `pg_connection` to `host.docker.internal:5433/…`,
   run **Select Data Source → Data Discovery** and pick the tables (interactive).
   → graph populated in the compose Neo4j.
2. **Engineer's Claude Code (MCP)** — `run_stage metadata_enrichment` →
   ran server-side on the engine → wrote one `:ColumnDescription` per column →
   stage landed in `awaiting_review` for the PO/Steward. Confirmed via
   `run_cypher`.
