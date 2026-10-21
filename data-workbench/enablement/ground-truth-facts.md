# Enablement Track — Ground-Truth Fact Sheet

**Purpose.** A single locked reference of the load-bearing facts the enablement content
depends on, each traceable to a source in the repo. This is the anti-drift sheet: the
Phase-2 content-authoring phase draws numbers from **here**, not from older decks or docs
that have gone stale. Verified **2026-08-25** against the working tree.

> When a number here disagrees with a doc, deck, or README, **this sheet wins** until it is
> itself re-verified. Re-verify the whole sheet before each authoring pass.

---

## Ports & services (local Docker deployment)

Source: `docker-compose.yml`, `CLAUDE.md` dev cheatsheet, `cli/` launcher.

| Service | Host port(s) | Container | Default state | Notes |
|---|---|---|---|---|
| Frontend (SPA) | **5173** | 80 | on | `http://localhost:5173` |
| Backend (FastAPI + WS) | **8000** | 8000 | on | health: `GET /api/health` |
| Neo4j browser | **7475** | 7474 | on | knowledge graph UI |
| Neo4j bolt | **7688** | 7687 | on | `bolt://localhost:7688`; in-compose creds `neo4j/workbenchpass` |
| Postgres (HR sample) | **5433** | 5432 | profile-gated | the enablement **spine** — `--with postgres` |
| MySQL (HR sample) | **3307** | 3306 | profile-gated | alternate/optional source |
| Gitea | **3101** | 3000 | **on by default** | git provider for pushable packages |
| SeaweedFS (object store) | **9000 / 8888 / 9333** | — | **opt-in, off** | `--with storage`; object storage is *optional* for this track |

**Rule to bake in:** recommend **Postgres-only** for enablement
(`./dwb up --with postgres`). MySQL/Databricks are alternate/optional sources; the object
store is optional and off by default.

---

## The `dwb` launcher

Source: `./dwb`, `cli/__main__.py`, `cli/{doctor,compose,samples,reset,state}.py`,
`docs/getting-started.md`.

```bash
./dwb doctor                     # preflight: docker, ports, secrets
./dwb up --with postgres         # RECOMMENDED enablement default: core stack + HR Postgres + Gitea
./dwb up --with postgres,mysql   # + HR MySQL
./dwb up --with postgres,mysql,storage   # + S3 object store (SeaweedFS)
./dwb status                     # resolved app + dependency health
./dwb logs                       # tail logs
./dwb down                       # graceful stop  (--volumes wipes everything)
./dwb reset                      # interactive soft/hard reset
./dwb connect                    # (re)write the quick-connect manifest
./dwb restart                    # restart app procs
```

- `--with` opt-in items: `postgres`, `mysql`, `storage` (alias `objectstore`).
- Gitea comes up by default (`--gitea` on); disable explicitly if needed.
- Equivalent raw compose (what `--with postgres,mysql,storage` does):
  `docker compose --profile postgres --profile mysql --profile gitea --profile storage up -d --build`.

---

## MCP tool counts (the number that keeps going stale)

Source: `docs/mcp-architecture.md`, `engineer-kit/README.md`, `po-kit/README.md`,
`workbench/backend/{mcp_server.py,po_mcp_server.py}`.

| Front door | Persona | Tool count | Endpoint |
|---|---|---|---|
| `/mcp` | Data Engineer | **141** | `workbench/backend/mcp_server.py` |
| `/po-mcp` | Product Owner | **57** | `workbench/backend/po_mcp_server.py` |

**Stale numbers to never propagate:** the 2026-08-07 deck pack says **136 DE / 38 PO** —
wrong. Use **141 / 57**.

---

## Skills

Source: `ls workbench-skills/skills/` (filesystem, authoritative).

- **69 vendored skills** as of 2026-08-25 (filesystem count).
- **Drift to reconcile:** `docs/faq.md` says **67**; this enablement plan's early draft
  said **59**. Use **69** and note the count is a moving target — re-run
  `ls workbench-skills/skills/ | wc -l` at authoring time and cite the date.
- Skills are vendored in-repo at `workbench-skills/skills/<name>/` (the `workbench-skills/`
  plugin), each = `SKILL.md` + `scripts/`, all accept `--project-code`.

---

## LLM access

Source: `.env` (present, gitignored), `docs/getting-started.md`, project setup.

- LLM key via **Azure AI Foundry**: `ANTHROPIC_FOUNDRY_API_KEY` (+ `ANTHROPIC_FOUNDRY_RESOURCE`,
  required, no default) in a **gitignored `.env`**. Direct-Anthropic (`ANTHROPIC_API_KEY`) is
  the fallback path.
- **Third route, verified working:** `CLAUDE_CODE_OAUTH_TOKEN` — a 1-year token from
  `claude setup-token` — lets a workstation that already has **Claude Code** run the stack
  with no API key at all, in compose mode as well as host mode. Works because the Agent SDK
  *is* Claude Code (it spawns the bundled CLI with the process env). It is a **personal
  credential billed to that individual's subscription** → workstation only, never a shared
  or client deployment. Precedence: Foundry > `ANTHROPIC_API_KEY` > `CLAUDE_CODE_OAUTH_TOKEN`.
  `./dwb doctor` reports it, and nudges anyone with a local Claude Code login but no key.
- A live-looking Foundry key **is present** in the working-tree `.env` today → the
  secret-hygiene guidance ("never screenshot `.env`", rotate) is real, not hypothetical.
- **Net-new to author:** the team-provisions-the-key process + the **30-day rotation /
  expiry governance policy** (no existing doc).

---

## Confirmed-stale docs (fix or avoid before a claim lands in a deck)

| Item | Where | Status | Correct version |
|---|---|---|---|
| Tool counts 136 / 38 | 2026-08-07 deck pack | stale | **141 / 57** |
| PO tool count "50" in body | `po-kit/README.md:74` (header says 57) | stale | **57** |
| Skill count 67 | `docs/faq.md:96` | drifting | **69** (filesystem, 2026-08-25) |
| "manual `psql` deploy" | `samples/*/README.md` | stale | **Configure → Build → Deploy** |
| "Optional services" MySQL/Gitea plain-compose + manual Gitea | `docs/deployment-guide.md` | stale | profile-gated `dwb` model |

---

## Net-new content (nothing in repo — author fresh in Phase 2)

1. **Hardware sizing** — 32 GB RAM min + CPU/disk + the *why* (many containers + JVM Neo4j
   + embedding model + a Claude subprocess per stage). (M2)
2. **Concurrency/scale note** — only a placeholder question exists in `docs/faq.md:908`. (M2)
3. **WSL2 / Ubuntu primer** — no existing doc; all repo hits incidental. (M1)
4. **LLM key provisioning + 30-day rotation governance policy/runbook.** (M2)
5. **Secret-hygiene guidance** — `.env` holds a live-looking Foundry key on disk. (M2)
6. **Feedback-loop channel + template.** (M10)
7. **Canonical latest-version location** — open decision; placeholder until set. (M0)
