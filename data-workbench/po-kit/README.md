# Data Workbench — product-owner kit

A Claude Code **plugin** that lets a data product owner create and manage Data
Workbench products from their own Claude Code, in any folder. It bundles:

- the **`workbench-po` MCP server** (the workbench's PO control-plane API), and
- the **`workbench-po-guide` skill** that runs the conversational, business-first
  product-authoring experience,
- plus **`/my-products` + `/product-status`** slash commands.

This is a **thin client**: the engineering work (profiling, mapping, serving, DQ…)
runs on the workbench server. You describe what you need in business terms and
hand a well-formed product to engineering — no database credentials, no pipeline
skills to install.

## Prerequisites

- Claude Code installed.
- From your workbench admin: the **PO MCP URL** (e.g.
  `http://localhost:8000/po-mcp`, or your team's Azure/host deployment) and a
  **bearer token**.

## Install — one-line installer (recommended)

The backend serves an installer that drops the MCP connection + skill + slash
commands into any folder:

```bash
export WORKBENCH_TOKEN=<your-bearer-token>   # any value for a local dev backend in open mode
curl -fsSL http://localhost:8000/api/po-kit/install.sh | bash
```

Swap `http://localhost:8000` for your workbench host. The installer writes
`.mcp.json` (Claude Code), `.cursor/mcp.json` + `.cursor/rules/*.mdc` (Cursor),
and `.codex/config.toml` + `AGENTS.md` (Codex), and installs the skill under
`.claude/`. The token stays in your shell env — never written to disk.

## Install — as a plugin

```bash
# 1) point Claude Code at the workbench marketplace (this repo)
/plugin marketplace add <your-org>/claudecodedash

# 2) install the product-owner kit (user scope = available in every folder)
/plugin install workbench-product-owner@data-workbench
```

The bundled `.mcp.json` reads two environment variables:

```bash
export WORKBENCH_PO_MCP_URL="http://localhost:8000/po-mcp"   # your workbench PO MCP endpoint
export WORKBENCH_TOKEN="<your-bearer-token>"
```

Set these **before** launching Claude Code, then restart it (or `/reload-plugins`).

> If your Claude Code build doesn't expand `${VAR}` inside `.mcp.json` headers,
> register the server directly instead:
>
> ```bash
> claude mcp add --transport http workbench-po "$WORKBENCH_PO_MCP_URL" \
>   --header "Authorization: Bearer $WORKBENCH_TOKEN"
> ```

## Verify

In Claude Code, just say what you want to do: **"I need to create a new data
product."** or **"I need to continue working on a data product."** The
`workbench-po-guide` skill should engage, check what already exists, and start
shaping the product with you. Or run `/my-products` to see what you own.

## The tool surface (68 PO MCP tools)

The server exposes **50** MCP tools (`workbench/backend/po_mcp_server.py`),
mounted at `/po-mcp` — completely separate from the engineer MCP at `/mcp`. The
skill now allowlists the full surface; any client (Codex, a raw MCP client)
auto-discovers all 50. Grouped: orient/reuse (`get_po_summary`, `list_my_products`,
`list_marketplace`, `find_source_products`, `get_discovery_inventory`, …); author
(`start_consumer_product`, `create_source_product`, `recommend_schema`,
`save_product_spec`, `discard_draft_version`, …); quality/ops
(`suggest_domain_rules`, `suggest_sla`, `run_gap_analysis`, `trigger_osi`,
`get_osi_evaluation`, …); submit/track (`submit_product_spec`, `get_product_status`,
`get_product_report`, `deploy_product`); review (`get_pending_validations`,
`review_validation_item`, `resolve_pushback`, …); inbound intake
(`list_intake_submissions`, `approve_intake_submission`, `reject_intake_submission`).

## Auth is fail-closed by design

The `/po-mcp` endpoint is token-gated by the same bearer middleware as `/mcp`
(`WB_MCP_TOKENS`; each entry is `token`, `token:principal`, or
`token:principal:proj1|proj2`). No token configured + no opt-out → the server
refuses every request. For **local dev only**, `WB_MCP_ALLOW_INSECURE=1` runs it
open (no token). `principal` is used for PROV-O attribution on writes.

For a **shared/remote deploy**, also set `WB_PUBLIC_BASE_URL` to the backend's
public origin (e.g. `https://workbench.example.com`) so the served installer
registers the MCP endpoint against a trusted origin instead of the incoming
`Host` header.
