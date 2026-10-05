# Data Workbench — engineer kit

A Claude Code **plugin** that lets a data engineer drive Data Workbench projects
from their own Claude Code, in any folder. It bundles:

- the **`workbench` MCP server** (the workbench's control-plane API), and
- the **`workbench-guide` skill** that teaches Claude Code the project lifecycle
  and which MCP tools to call.

This is a **thin client**: the data-engineering work (discovery, mapping,
serving, DQ…) runs on the workbench server. You don't install or run those
skills, and you don't need database credentials — you orchestrate via MCP.

## Prerequisites

- Claude Code installed.
- From your workbench admin: the **MCP URL** (e.g. `http://localhost:8000/mcp`,
  or your team's container/host) and a **bearer token** scoped to your projects.

## Install — one-line installer (recommended)

The backend serves an installer that drops the MCP connection + skill into any
folder:

```bash
export WORKBENCH_TOKEN=<your-bearer-token>   # any value for a local dev backend in open mode
curl -fsSL http://localhost:8000/api/engineer-kit/install.sh | bash
```

Swap `http://localhost:8000` for your workbench host. The installer writes
`.mcp.json` (Claude Code), `.cursor/mcp.json` + `.cursor/rules/*.mdc` (Cursor),
and `.codex/config.toml` + `AGENTS.md` (Codex), and installs the skill under
`.claude/`. The token stays in your shell env — never written to disk. (A fresh
Claude Code session attaches MCP on launch, so relaunch after installing.)

## Install — as a plugin

```bash
# 1) point Claude Code at the workbench marketplace (this repo)
/plugin marketplace add <your-org>/claudecodedash

# 2) install the engineer kit (user scope = available in every folder)
/plugin install workbench-engineer@data-workbench
```

## Configure the URL + token

The bundled `.mcp.json` reads two environment variables:

```bash
export WORKBENCH_MCP_URL="http://localhost:8000/mcp"   # your workbench MCP endpoint
export WORKBENCH_TOKEN="<your-bearer-token>"
```

Set these in your shell profile (or your team's secrets tooling) **before**
launching Claude Code, then restart Claude Code (or `/reload-plugins`).

> **If your Claude Code version doesn't expand `${VAR}` inside `.mcp.json`
> headers**, register the server directly instead (the token is saved to
> `~/.claude.json`):
>
> ```bash
> claude mcp add --transport http workbench "$WORKBENCH_MCP_URL" \
>   --header "Authorization: Bearer $WORKBENCH_TOKEN"
> ```
>
> A purely local dev workbench may run with auth disabled — but that now
> requires the operator to explicitly opt in (`WB_MCP_ALLOW_INSECURE=1` with no
> `WB_MCP_TOKENS`); otherwise the server refuses all requests (see "Auth is
> fail-closed by design" below). In open mode no token is needed and any value
> works.

## Verify

In Claude Code, ask: **"List my workbench projects."** You should see the
`workbench-guide` skill engage and `mcp__workbench__list_projects` return your
projects. From there: *"What's the state of project X?"*, *"Run discovery on
X"*, *"Show me the mappings for X"*.

## The tool surface (142 MCP tools)

The server exposes **142** MCP tools (`workbench/backend/mcp_server.py`). The
`workbench-guide` skill now allowlists the **full** surface, and any client
(OpenAI Codex, a raw MCP client) auto-discovers all 142. The **canonical,
always-current per-tool reference** is
**[../docs/mcp-architecture.md](../docs/mcp-architecture.md)**; this kit's
`reference.md` documents the tools reached for most (the migration, lakehouse/
transfer, connections, and intake tools are taught by flow in `SKILL.md`). In
brief, grouped (the DE server also drives the full consumer-aligned product
lifecycle, **data migration (`dmig`)**, **code migration (`cmig`)**, lakehouse/
transfer serving, connections, inbound intake, and the marketplace/DQ/OSI/QA read
surface — see the canonical reference for the complete list):

- **Read / query** — `list_projects`, `get_project_state` (stages + statuses +
  PO brief + `web_url`; each stage's `execution_kind` tells the driver which tool
  to use), `get_plan_summary`, `get_stage_results` (read stage output via vetted
  queries — prefer over hand-written Cypher), `run_cypher` (read-only,
  project-scoped), `get_dbt_project`, `get_dataset_filter`.
- **Stage execution** — `set_data_source` (configure the source DB; required
  before Data Discovery), `run_stage` (server-side; returns a `run_id`, poll for
  status), `get_stage_config_options`, `complete_stage` (mechanical lifecycle
  stages: Mark Discovery Complete, ODCS→dprod, Publish, …), `reset_stage`.
- **Mechanical / lifecycle** — `set_serving_mode` (virtual ↔ materialized),
  `set_materialization_target`, `set_dataset_filter`, `accept_request` (unlock
  the dpe-sa PO validation gate).
- **Interactive** — `get_pending_questions`, `answer_question` (for stages that
  pause mid-run to ask, e.g. Data Discovery's "which tables?").
- **Agentic** — `run_stage` (above), `query_semantic_layer` (NL question over a
  domain's deployed views; returns markdown).
- **Review-write** (require a declared `role`) — `review_description`,
  `review_mapping`, `review_domain_rule`, `review_table_description`,
  `review_relationship_description`. These sign off a human review gate from your
  own Claude Code; you declare the `role` you're acting as and the server
  enforces it (so an engineer can "switch hats" to a Steward/DQA/PO to clear a
  gate). Writes are attributed to your token's principal.
- **Consumer-product authoring** — drive a consumer-aligned (`dpe-cf`) product
  end-to-end: `create_consumer_product`, `save_odcs_spec`, `submit_product_spec`,
  `match_inputs`, `run_gap_analysis`, `bulk_approve_mappings`,
  `deploy_virtual_view`, `trigger_osi_score`, …
- **Marketplace / DQ / OSI / QA reads** — `list_marketplace_products`,
  `get_marketplace_detail`, `get_marketplace_lineage`, `get_odcs_spec`,
  `get_osi_evaluation`, `get_dq_score`, `preview_serving_view`,
  `run_readonly_sql`, `get_product_report`, `probe_qa_question`,
  `execute_qa_question`, …

The data-engineering work itself runs server-side as data agents (Claude Agent
SDK); these tools **drive and observe** it.

## Auth is fail-closed by design

The `/mcp` endpoint is the remote trust boundary. The server reads
`WB_MCP_TOKENS` (comma-separated bearer tokens; each entry is `token`,
`token:principal`, or `token:principal:proj1|proj2`):

- **No token configured + no opt-out → the server REFUSES every request.** A
  container/remote deploy that simply forgot `WB_MCP_TOKENS` exposes nothing.
- For **local dev only**, set `WB_MCP_ALLOW_INSECURE=1` to run `/mcp` open (no
  token). Never do this on a shared/remote deployment.
- `principal` is used for PROV-O attribution on review writes. Project ACL: `:*`
  or omitted = all projects; an explicit `proj1|proj2` list scopes the token to
  those projects and the server denies the rest.
- `run_cypher` is read-only at the database level (runs in a Neo4j READ
  transaction); scoping and role checks are all enforced server-side regardless
  of what the client sends.
- For a **shared/remote deploy**, also set `WB_PUBLIC_BASE_URL` to the backend's
  public origin so the served installer registers the MCP endpoint against a
  trusted origin instead of the incoming `Host` header.
