<!-- Data Workbench bootstrap prompt. Served by GET /api/bootstrap (BASE_URL injected).
     A brand-new user pastes this whole message into a FRESH Claude Code session
     (in an empty folder). No MCP, no skill, no repo access needed up front — this
     prompt drives the entire setup itself, ringer-style. -->

You are bootstrapping a **Data Workbench** workspace for me from scratch. The
workbench backend is at **__WORKBENCH_BASE_URL__**. Do the whole setup yourself —
don't make me run commands manually. Follow these steps in order:

## 1. Which persona am I?
Ask me one question if you don't already know: **am I a Data Product Owner (I
create/own data products in business terms) or a Data Engineer (I build/deploy
them)?** Pick the kit:
- Product Owner → PO kit at `__WORKBENCH_BASE_URL__/api/po-kit/install.sh`
- Data Engineer → engineer kit at `__WORKBENCH_BASE_URL__/api/engineer-kit/install.sh`

## 2. Token
Check whether `WORKBENCH_TOKEN` is set in my env (`printenv WORKBENCH_TOKEN`). If
not, ask me for my bearer token (tell me a local dev backend in open mode accepts
any value, e.g. `devtoken`). Keep it in the shell env — never write it to a file.

## 3. Install the kit (this is the bootstrap)
Run the persona's installer with my token, into the current folder:

```bash
export WORKBENCH_TOKEN="<the token>"
curl -fsSL __WORKBENCH_BASE_URL__/api/<po-kit|engineer-kit>/install.sh | bash
```

The installer writes config for **every common client**, so it works no matter
which tool I'm using — you don't need to detect it:
- **Claude Code** → `.mcp.json` + `.claude/skills/` (guide skill) + `/`-commands
- **Cursor** → `.cursor/mcp.json` + `.cursor/rules/` (guidance)
- **Codex** → `.codex/config.toml` + `AGENTS.md`

Show me the installer output.

## 4. Verify + hand off
The MCP server only connects on a **fresh launch**. Figure out which client I'm
running in (you know what you are) and give me the matching restart instruction:

- **Claude Code** → "Restart Claude Code in this folder (exit + relaunch, or
  `claude --mcp-config .mcp.json --strict-mcp-config`), with `WORKBENCH_TOKEN`
  exported."
- **Cursor** → "Reload Cursor (or toggle the `workbench` server in Settings →
  MCP) so it picks up `.cursor/mcp.json`, with `WORKBENCH_TOKEN` in your env."
- **Codex** → "Restart `codex` and TRUST this folder so it loads
  `.codex/config.toml`."
- **Something else** → tell me your tool; if it's an MCP client, point it at the
  server URL below with an `Authorization: Bearer $WORKBENCH_TOKEN` header:
  `__WORKBENCH_BASE_URL__/<po-mcp|mcp>`.

Then: "with `WORKBENCH_TOKEN` exported, just tell me what you want to do."

Give me a concrete first thing to say after restart, tailored to my persona:
- Product Owner → *"I need to create a new data product."* or *"I need to
  continue working on a data product."* (the assistant lists what you own, then
  either shapes a new product or resumes an in-flight one).
- Data Engineer → *"List my workbench projects"* or *"accept the incoming project
  and run it end to end"*.

## Rules
- Do the mechanical setup silently; narrate in plain terms what you're doing and
  why (one line per step), not raw output dumps.
- Don't ask me to download anything or edit config by hand — the installer does it.
- If the curl can't reach `__WORKBENCH_BASE_URL__`, tell me the endpoint isn't
  reachable from here (localhost only works on the backend host) and ask for the
  right host/URL.
