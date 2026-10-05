# Getting Started with Data Workbench

A fast, practical path from "I was handed a folder (or zip) of Data Workbench" to "I have a
running instance and the UI open in my browser."

The **recommended** way to run Data Workbench is the **container stack** (Docker Compose). It
brings up everything the app needs — backend, knowledge graph, and web UI — with one command,
and it's the same topology you'd use to share an instance with engineers over MCP. The
local-dev (no-container) path exists too and is documented at the end, but for a quick start,
use containers.

---

## 1. What you were handed

If you have a **zip**, unzip it. If you have a **directory**, `cd` into it. Either way you should
see this at the top level:

```
docker-compose.yml      # the stack definition (start here)
Dockerfile              # backend image (FastAPI + MCP + baked-in skills + embedding model)
requirements.txt        # backend Python deps (installed into the image)
workbench/              # backend (FastAPI) + frontend (React/Vite, has its own Dockerfile)
workbench-skills/       # the agent skills plugin (the pipeline's brains — see §6)
playbook/               # domain catalogs + scoring rubrics
projects/               # per-project scratch (created/persisted at runtime)
```

If `docker-compose.yml` and `Dockerfile` are present, you have everything required to build and
run — no source DB ships in the box (that's intentional, see §5).

---

## 2. Prerequisites (container path)

That's the whole list for this path:

- **Docker Engine** (20.10+)
- **Docker Compose v2** (the `docker compose` subcommand — bundled with modern Docker Desktop / `docker-compose-plugin`)
- Host ports **8000** (backend) and **5173** (frontend) free, plus **7475 / 7688** for Neo4j. If a local dev stack is already using 8000/5173, stop it first.
- **An LLM credential** — what the backend uses to run the agentic pipeline stages. Any *one* of three: an **Azure AI Foundry** key + resource, a **direct Anthropic API key**, or — if you already run **Claude Code** on this machine — a token from `claude setup-token` (see Route 3 in step 1). Without one, the app boots but stages that call the model won't run.

You do **not** need Python, Node, Neo4j, or a Postgres install on the host for this path —
Compose pulls/builds all of that. The only thing Docker pulls from a registry is the base
images; the Workbench images are built locally from the `Dockerfile`s.

---

## 3. The containers in the stack

`docker-compose.yml` defines **three** services:

| Service | Image | Source | Host ports | Purpose |
|---|---|---|---|---|
| `neo4j` | `neo4j:5-community` | pulled from Docker Hub | `7475→7474` (browser), `7688→7687` (bolt) | Knowledge graph — DCAT/DQV/ODCS/dprod nodes, mappings, semantic layer. Data persists in the `neo4j_data` volume. |
| `backend` | built from `./Dockerfile` | **built locally** | `8000→8000` | FastAPI + WebSocket + the MCP server at `/mcp`. Runs pipeline stages via the Claude Agent SDK. SQLite + project scratch persist in `wb_data` / `wb_projects` volumes. |
| `frontend` | built from `./workbench/frontend/Dockerfile` | **built locally** | `5173→80` | The React/Vite web UI, served by nginx. |

**What is *not* in the stack:** **PostgreSQL — the source data you actually profile.** Data
Workbench is the orchestrator, not the data store. You point each project at wherever its source
DB already lives (see §5). This keeps the box small and avoids baking sample data into the image.

Internally the backend reaches Neo4j at `neo4j:7687` over the Compose network; the `7475/7688`
host mappings are only so *you* can open the Neo4j Browser and avoid clashing with a dev Neo4j on
the standard ports.

---

## 4. Build, deploy, and access the UI

### Step 1 — Create the secrets file

Compose reads a **gitignored `.env`** sitting next to `docker-compose.yml`. Create it:

```bash
cat > .env <<'EOF'
# The LLM credential. Route 1 — Azure Foundry: the backend routes through
# Foundry when the key is set. The RESOURCE names your own Azure resource and
# is REQUIRED (no default); `./dwb doctor` fails hard if you omit it.
ANTHROPIC_FOUNDRY_API_KEY=<your-foundry-key>
ANTHROPIC_FOUNDRY_RESOURCE=<your-azure-foundry-resource>
# The deployed model name on that resource. Optional (default claude-opus-4-8).
# ANTHROPIC_FOUNDRY_MODEL=claude-opus-4-8

# Route 2 — Anthropic direct. Leave the Foundry vars unset and set this instead;
# the Claude Agent SDK picks it up with no further configuration.
# ANTHROPIC_API_KEY=<your-anthropic-key>

# Route 3 — your own Claude Code subscription (no API key needed). Run
# `claude setup-token` on this machine and paste the printed token here; it is
# valid for a year and works in compose mode too. Personal credential that bills
# your subscription — laptop only, never a shared box.
# CLAUDE_CODE_OAUTH_TOKEN=<token from `claude setup-token`>

# Bearer token(s) for the MCP front doors (/mcp engineer + /po-mcp PO). Format:
#   token  |  token:principal  |  token:principal:proj1|proj2  ("*" or omitted = all projects)
# Comma-separated. REQUIRED for any shared/remote deployment. Both servers share
# this token pool.
WB_MCP_TOKENS=<your-token>

# Local-only escape hatch: run the MCP servers open with NO token. NEVER set this
# on a shared/remote box. If you set this, you can omit WB_MCP_TOKENS.
# WB_MCP_ALLOW_INSECURE=1

# On a shared/remote deploy, set the backend's public origin so the served kit
# installers register the MCP endpoint against a trusted origin instead of the
# incoming Host header (which a proxy could forge). Unset = fall back to the
# request Host (fine for local dev).
# WB_PUBLIC_BASE_URL=https://workbench.example.com
EOF
```

> `.env` is gitignored — never commit it. Rotate the Foundry key periodically.

### The fast path: `dwb`

Once `.env` exists, the `dwb` launcher does Steps 2–3 for you **and** brings up
opt-in sample databases + Gitea (auto-bootstrapped for Push-to-Git):

```bash
./dwb doctor                              # preflight: docker, ports, secrets
./dwb up --with postgres,mysql            # core stack + HR Postgres + HR MySQL + Gitea
./dwb up --with postgres,mysql,storage    # ...also the S3 object store (SeaweedFS) for publishing data artifacts
./dwb up --with observability             # ...also the reference OpenTelemetry collector (Grafana otel-lgtm → http://localhost:3111)
./dwb status                              # resolved app + dependency health
./dwb down                                # graceful stop  (--volumes wipes everything)
./dwb reset                               # interactive soft/hard reset
```

Observability is **opt-in** (see the Deployment Guide → "Observability"): `--with observability` launches a bundled collector and wires the stack to it; without it — or without an `OTEL_EXPORTER_OTLP_ENDPOINT` in `.env` — no telemetry is emitted.

The launched sample DBs show up as a **Quick connect** prefill in the connection
forms (Step 5 below) — no hand-typing coordinates. `dwb` is stdlib-only Python;
run `./dwb --help` for the full surface (host mode, `--pg-sample`, `--no-gitea`,
`logs`, `restart`, `connect`). The rest of this section documents the equivalent
**manual** Compose commands `dwb` runs under the hood.

### Step 2 — Build the images and start the stack

```bash
# The sample DBs + Gitea + object store are profile-gated — pass their profiles
# explicitly (this is exactly what `dwb up --with postgres,mysql,storage` does):
docker compose --profile postgres --profile mysql --profile gitea --profile storage up -d --build
```

This builds the **backend** and **frontend** images and pulls **neo4j**. The first build is
slower: the backend `Dockerfile` installs the Python deps and **bakes the `bge-small` embedding
model (~130 MB)** into the image so the first Concept-Guided semantic chat doesn't pay a download
at runtime. Subsequent builds are cached and fast.

### Step 3 — Verify it's up

```bash
curl http://localhost:8000/api/health
docker compose ps          # all three services should be "running"/"healthy"
docker compose logs -f backend   # tail backend logs if something's off
```

### Step 4 — Open the UI

| Open this | What it is |
|---|---|
| **http://localhost:5173** | **The web UI** — start here. The persona chooser lets you enter the Product Workbench or the Engineering Workbench; the header **Switch** button toggles between them. |
| http://localhost:8000 | Backend REST + WebSocket; MCP server mounted at `/mcp`. |
| http://localhost:7475 | Neo4j Browser — login `neo4j` / `workbenchpass` (bolt on `7688`). Optional; for poking at the graph. |

### Everyday lifecycle

```bash
docker compose up -d --build backend frontend   # rebuild after code changes
docker compose logs -f backend                  # tail logs
docker compose down                             # stop the stack (named volumes persist)
docker compose down -v                          # stop AND wipe all data (fresh start)
```

State lives in the named volumes `neo4j_data`, `wb_data` (SQLite), and `wb_projects`
(per-project scratch). A plain `down` keeps them; `down -v` deletes them.

---

## 5. Connect a source database (first real task)

Once the UI is open, point a project at a source DB to profile:

1. In the **Engineering Workbench → Settings**, set the **Neo4j** connection — it's already
   configured for the in-stack Neo4j (`neo4j:7687`, `neo4j` / `workbenchpass`), so usually
   no change needed.
2. Create a project (or product) and give it a **PostgreSQL** source connection.

**Reaching a Postgres that runs on your host machine:** the backend is inside a container, so
`localhost` points at the container, not your laptop. Use **`host.docker.internal`** as the host
in the project's connection string instead of `localhost`. (The Compose file already wires
`host.docker.internal` to the host gateway for the backend service.) A Postgres running in
another container or on a remote server just uses its normal hostname/IP.

---

## 6. Data platform support — skills are Postgres-first

**Important:** the Workbench pipeline is driven by **agent skills**, and the current skill set is
designed against **PostgreSQL**. Discovery, profiling, and the serving/view-deployment path all
assume Postgres as the source (and serving) platform. Supporting another data platform
(Snowflake, Databricks, BigQuery, SQL Server, etc.) means **authoring new agent skills** (or
platform variants of existing ones) for at least the discovery/profiling/serving stages.

There's already precedent for this in the box:

- `data-discovery` (Postgres) and **`data-discovery-mysql`** (MySQL) show the variant pattern — a new platform typically gets its own `data-discovery-<platform>` skill.
- The view-DDL generator has a **dialect picker** (Postgres / Snowflake / Databricks / BigQuery / ANSI) for *emitting* SQL, but the deploy path (`CREATE OR REPLACE VIEW` against the source) is still Postgres-centric.

### Where the skills live

All agent skills are vendored in-repo as a Claude Code plugin:

```
workbench-skills/skills/<skill-name>/
    SKILL.md          # the skill's instructions (what the agent reads)
    scripts/          # optional Python helpers (all accept --project-code)
```

Each skill is a directory with a `SKILL.md`. They're baked into the backend image (`COPY . .` in
the `Dockerfile`) and loaded at runtime via the plugin mechanism. To **add platform support**,
copy the closest existing skill (e.g. `data-discovery`), adapt its `SKILL.md` + `scripts/` to the
new platform's driver/SQL, rebuild the backend image, and the pipeline picks it up. Editing a
skill is normal Workbench work — it ships with the change that needs it.

> Exception: the `skill-reflector` / `chat-reflector` skills emit *proposals* into
> `playbook/skill_reflections/` and `playbook/chat_reflections/` — those are review artifacts,
> not auto-applied changes.

---

## 7. Giving an engineer or PO remote access (MCP)

A defining feature of the container topology: a user can drive the Workbench from **their own
Claude Code** over MCP, treating your running backend as a "remote server." There are **two
front doors** on the same backend, sharing the same `WB_MCP_TOKENS` pool:

- **`/mcp`** — the **Data Engineer** surface (142 tools; the `engineer-kit`).
- **`/po-mcp`** — the **Data Product Owner** surface (68 tools; the `po-kit`).

**Fastest path — the one-line installers.** Hand the user a bearer token and the right
installer; it drops the MCP connection + skill into any folder (writing config for Claude
Code, Cursor, and Codex):

```bash
export WORKBENCH_TOKEN=<token>
# Data Engineer:
curl -fsSL http://<host>:8000/api/engineer-kit/install.sh | bash
# Data Product Owner:
curl -fsSL http://<host>:8000/api/po-kit/install.sh | bash
```

MCP attaches on launch, so **relaunch Claude Code after installing** (with
`--strict-mcp-config` if you want only the kit's server in scope). Not sure which persona?
`curl http://<host>:8000/api/bootstrap` (optionally `?persona=po|de`) returns a paste-once
prompt that asks, runs the right installer, and tells you to relaunch.

**Manual alternative.** The bundled config mirrors `.mcp.json.example` in the repo — one entry
per server:

```json
{
  "mcpServers": {
    "workbench":    { "type": "http", "url": "http://localhost:8000/mcp",    "headers": { "Authorization": "Bearer REPLACE_WITH_YOUR_WB_MCP_TOKEN" } },
    "workbench-po": { "type": "http", "url": "http://localhost:8000/po-mcp", "headers": { "Authorization": "Bearer REPLACE_WITH_YOUR_WB_MCP_TOKEN" } }
  }
}
```

For a shared/remote deployment, **always set `WB_MCP_TOKENS`**, **never set
`WB_MCP_ALLOW_INSECURE=1`** (it runs the MCP servers open), and set **`WB_PUBLIC_BASE_URL`** to
the backend's public origin so the served installers register against a trusted origin instead
of the incoming `Host` header. See `docs/engineer-guide.md` and `docs/mcp-architecture.md` for
the full tool surface and auth model.

---

## 8. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `curl /api/health` refused | Backend still building (first build bakes the model) or crashed — `docker compose logs backend`. |
| Port already in use on `up` | A dev stack is on 8000/5173/7687 — stop `uvicorn` / `npm run dev` / a local Neo4j first. |
| Stages start but the model never responds | LLM credential missing/invalid in `.env` — `ANTHROPIC_FOUNDRY_API_KEY` **and** `ANTHROPIC_FOUNDRY_RESOURCE` (both required together, no default resource), `ANTHROPIC_API_KEY` for the direct route, or `CLAUDE_CODE_OAUTH_TOKEN` for the Claude Code subscription route. Run `./dwb doctor`. |
| MCP client gets "auth unsupported" / 401 | `WB_MCP_TOKENS` not set (and `WB_MCP_ALLOW_INSECURE` not set), or wrong token. |
| Project can't reach your host Postgres | Use `host.docker.internal` (not `localhost`) as the DB host. |
| Want a clean slate | `docker compose down -v` wipes all volumes, then `up -d --build`. For a demo reset that keeps source tables + settings, `scripts/reset_demo_env.py` (dry-run by default; applying requires `WB_ALLOW_DEMO_RESET=1` **and** a demo-looking target DSN, so it can't run against a shared DB). |

---

## 9. Local dev (no containers)

If you'd rather run the pieces directly (for backend/frontend development), you need **Python
3.12+**, **Node 20+**, a **Neo4j** instance, a **Postgres** source, and the Claude Code CLI. See
the **Quick Start (local dev, without containers)** section of [`README.md`](../README.md) for
the exact commands. For everything else, the container path above is recommended.

---

### Where to go next

- [`README.md`](../README.md) — feature-level overview of the two workbenches, wizards, and marketplace.
- [`docs/userguide.md`](userguide.md) — end-user walkthrough for both shells.
- [`docs/engineer-guide.md`](engineer-guide.md) — driving a project from your own Claude Code over MCP.
- [`docs/mcp-architecture.md`](mcp-architecture.md) — the canonical MCP tool reference + auth model.
- [`CLAUDE.md`](../CLAUDE.md) — canonical source of truth for system internals.
