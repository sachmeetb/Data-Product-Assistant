#!/usr/bin/env bash
# Data Workbench — product-owner kit installer.
#
# Registers the Workbench PO MCP connection and drops the `workbench-po-guide`
# skill + `/product-*` slash commands into a project's .claude/ — all from the
# remote backend, no git/repo access needed.
#
#   curl -fsSL <backend>/api/po-kit/install.sh | WORKBENCH_TOKEN=<tok> bash -s -- [project-dir]
#
# Env:
#   WORKBENCH_TOKEN        bearer token from the workbench admin (one of WB_MCP_TOKENS).
#   WORKBENCH_PO_MCP_URL   override the PO MCP endpoint (defaults to this backend's /po-mcp).
set -euo pipefail

# Injected by the backend when it serves this script (request base URL).
BASE_URL="__WORKBENCH_BASE_URL__"

PROJECT_DIR="${1:-.}"
MCP_URL="${WORKBENCH_PO_MCP_URL:-${BASE_URL}/po-mcp}"
TOKEN="${WORKBENCH_TOKEN:-}"

# BASE_URL is injected at serve time; if it's still unsubstituted (script run
# without going through the backend) it won't be a real http(s) URL.
case "$BASE_URL" in
  http://*|https://*) ;;
  *)
    echo "error: fetch this from the workbench backend: curl -fsSL <backend>/api/po-kit/install.sh | bash" >&2
    exit 1
    ;;
esac

mkdir -p "$PROJECT_DIR/.claude"
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"
echo "→ Installing the Data Workbench product-owner kit into: $PROJECT_DIR"
echo "  PO MCP endpoint: $MCP_URL"

# 1) Fetch the kit bundle → place skills/commands under .claude/, AGENTS.md at root.
echo "→ Fetching skills + slash commands…"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fsSL "${BASE_URL}/api/po-kit/bundle.tar.gz" | tar -xzf - -C "$TMP"
[ -d "$TMP/skills" ] && cp -R "$TMP/skills" "$PROJECT_DIR/.claude/"
[ -d "$TMP/commands" ] && cp -R "$TMP/commands" "$PROJECT_DIR/.claude/"
echo "  ✓ .claude/skills/workbench-po-guide + .claude/commands/ (Claude Code)"

# AGENTS.md at the project root — Codex / non-Claude MCP clients read it for the
# same PO experience guidance the Claude skill carries.
if [ -f "$TMP/AGENTS.md" ]; then
  AGENTS_DST="$PROJECT_DIR/AGENTS.md"
  MARKER="<!-- data-workbench:po-kit -->"
  if [ -f "$AGENTS_DST" ] && grep -qF "$MARKER" "$AGENTS_DST" 2>/dev/null; then
    : # already has our block — leave it (idempotent)
  elif [ -f "$AGENTS_DST" ]; then
    { printf '\n%s\n' "$MARKER"; cat "$TMP/AGENTS.md"; } >> "$AGENTS_DST"
    echo "  ✓ appended workbench PO guidance to existing AGENTS.md (Codex)"
  else
    { printf '%s\n' "$MARKER"; cat "$TMP/AGENTS.md"; } > "$AGENTS_DST"
    echo "  ✓ AGENTS.md (Codex / generic MCP clients)"
  fi
fi

# 2) MCP server → project-local .mcp.json (token stays in the WORKBENCH_TOKEN env;
#    never written to disk). Merge-safe: preserve any other servers already there.
MCP_FILE="$PROJECT_DIR/.mcp.json"
echo "→ Registering the 'workbench-po' MCP server in .mcp.json…"
if [ -f "$MCP_FILE" ] && command -v python3 >/dev/null 2>&1; then
  MCP_FILE="$MCP_FILE" MCP_URL="$MCP_URL" python3 - <<'PY'
import json, os
path, url = os.environ["MCP_FILE"], os.environ["MCP_URL"]
try:
    data = json.load(open(path))
except Exception:
    data = {}
data.setdefault("mcpServers", {})["workbench-po"] = {
    "type": "http", "url": url,
    "headers": {"Authorization": "Bearer ${WORKBENCH_TOKEN}"},
}
json.dump(data, open(path, "w"), indent=2)
PY
  echo "  ✓ merged 'workbench-po' into existing .mcp.json"
else
  if [ -f "$MCP_FILE" ]; then
    cp "$MCP_FILE" "$MCP_FILE.bak"
    echo "  ! existing .mcp.json backed up to .mcp.json.bak (python3 not available to merge)"
  fi
  cat > "$MCP_FILE" <<JSON
{
  "mcpServers": {
    "workbench-po": {
      "type": "http",
      "url": "$MCP_URL",
      "headers": {
        "Authorization": "Bearer \${WORKBENCH_TOKEN}"
      }
    }
  }
}
JSON
  echo "  ✓ wrote .mcp.json"
fi

# 3) Codex MCP server → project-local .codex/config.toml. Codex doesn't read
#    .mcp.json — it loads mcp_servers from config.toml. Idempotent + merge-safe
#    without a TOML parser: skip if our table is already present, else append.
CODEX_DIR="$PROJECT_DIR/.codex"
CODEX_FILE="$CODEX_DIR/config.toml"
echo "→ Registering the 'workbench-po' MCP server in .codex/config.toml (Codex)…"
mkdir -p "$CODEX_DIR"
if [ -f "$CODEX_FILE" ] && grep -qE '^\[mcp_servers\.workbench-po\]' "$CODEX_FILE" 2>/dev/null; then
  echo "  ✓ .codex/config.toml already has [mcp_servers.workbench-po] (left as-is)"
else
  { [ -f "$CODEX_FILE" ] && printf '\n'
    cat <<TOML
[mcp_servers.workbench-po]
url = "$MCP_URL"
bearer_token_env_var = "WORKBENCH_TOKEN"
enabled = true
startup_timeout_sec = 20
TOML
  } >> "$CODEX_FILE"
  echo "  ✓ wrote [mcp_servers.workbench-po] to .codex/config.toml"
fi

# 3b) Cursor → project-local .cursor/mcp.json (Cursor's MCP config; same
#     {mcpServers:{…}} shape as Claude's .mcp.json) + a rule mirroring the
#     guidance. Written so a Cursor user gets the same wiring — no tool detection
#     needed; whichever client you launch reads its own config.
CURSOR_DIR="$PROJECT_DIR/.cursor"
CURSOR_MCP="$CURSOR_DIR/mcp.json"
echo "→ Registering the 'workbench-po' MCP server in .cursor/mcp.json (Cursor)…"
mkdir -p "$CURSOR_DIR/rules"
if [ -f "$CURSOR_MCP" ] && command -v python3 >/dev/null 2>&1; then
  CURSOR_MCP="$CURSOR_MCP" MCP_URL="$MCP_URL" python3 - <<'PY'
import json, os
path, url = os.environ["CURSOR_MCP"], os.environ["MCP_URL"]
try:
    data = json.load(open(path))
except Exception:
    data = {}
data.setdefault("mcpServers", {})["workbench-po"] = {
    "url": url, "headers": {"Authorization": "Bearer ${WORKBENCH_TOKEN}"},
}
json.dump(data, open(path, "w"), indent=2)
PY
  echo "  ✓ merged 'workbench-po' into existing .cursor/mcp.json"
else
  cat > "$CURSOR_MCP" <<JSON
{
  "mcpServers": {
    "workbench-po": {
      "url": "$MCP_URL",
      "headers": {
        "Authorization": "Bearer \${WORKBENCH_TOKEN}"
      }
    }
  }
}
JSON
  echo "  ✓ wrote .cursor/mcp.json"
fi
if [ -f "$TMP/AGENTS.md" ]; then
  { printf -- '---\ndescription: Data Workbench product-owner workflow guidance (thin MCP client)\nalwaysApply: true\n---\n\n'; cat "$TMP/AGENTS.md"; } > "$CURSOR_DIR/rules/workbench-po.mdc"
  echo "  ✓ .cursor/rules/workbench-po.mdc (Cursor guidance)"
fi

# 4) Token guidance
echo
if [ -z "$TOKEN" ]; then
  echo "⚠  WORKBENCH_TOKEN is not set. Export it before launching Claude Code:"
  echo "     export WORKBENCH_TOKEN=<your-bearer-token>"
  echo "   (A local dev workbench in open mode accepts any value.)"
else
  echo "✓  WORKBENCH_TOKEN detected. Make sure it's exported in the shell that launches Claude Code:"
  echo "     export WORKBENCH_TOKEN=<your-bearer-token>"
fi

cat <<EOF

Done. Installed for whichever client you use:
  • Claude Code — .mcp.json + .claude/ (skill + commands)
  • Cursor      — .cursor/mcp.json + .cursor/rules/workbench-po.mdc
  • Codex       — .codex/config.toml + AGENTS.md

Next:
  1) export WORKBENCH_TOKEN=<token>      # both clients read it; put it in your shell profile
  2) cd "$PROJECT_DIR"
  3a) Claude Code — launch it, then say what you want to do, e.g.:
      "I need to create a new data product." or
      "I need to continue working on a data product."
  3b) Codex — run \`codex\`, TRUST this folder when prompted, then say the same.

Notes:
  • The PO MCP endpoint ($MCP_URL) must be reachable from where the client runs.
    'localhost' only works on the host running the backend; from another host or
    a container use that host's address.
  • If your Claude Code build doesn't expand \${WORKBENCH_TOKEN} in .mcp.json:
      claude mcp add --transport http workbench-po "$MCP_URL" \\
        --header "Authorization: Bearer \$WORKBENCH_TOKEN"
EOF
