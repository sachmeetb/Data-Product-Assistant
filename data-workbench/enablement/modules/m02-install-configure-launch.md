# Module 2 — Install, Configure & Launch Locally

- **Type:** hands-on setup
- **Target length:** ~17 slides
- **Prereq:** Module 1 (WSL2 Ubuntu with Docker + the repo on the Linux fs)
- **Latest-version pointer:** `[PLACEHOLDER: canonical-latest-version location — TBD]`
  *(carry on every deck until the open decision is resolved)*

> **Slide creator brief — Module 2 of 11.** House style is in **Module 0** — follow that
> format for every slide. The learner has a working WSL2 Ubuntu with Docker and the repo on
> the Linux filesystem (Module 1 complete). This module installs, configures, and launches
> the Data Workbench stack locally via the `./dwb` launcher (a bash/Python wrapper around
> Docker Compose), then verifies all services are healthy. Key concepts introduced here:
> hardware sizing, LLM key governance, secret hygiene, and the services+ports map.
> **Screenshot placeholders:** insert a **gray placeholder box** at the stated size —
> **Large** = full-width, ~50–60% of slide height (primary visual) · **Medium** = ~half the
> slide, shared with text · **Small** = ~quarter-slide, accent or confirmation shot.
> Do not omit placeholder boxes — they mark where live screenshots are inserted later.

> **Author note.** Ports, `dwb` commands, and services are locked from
> `enablement/ground-truth-facts.md` (verified 2026-08-25). Hardware sizing, the
> concurrency note, the LLM-key governance policy, and secret-hygiene guidance are
> **net-new** — marked `[NET-NEW: …]` inline.

---

## Slide 1 — Bring DW up locally, healthy, with sample data
**Beat:** frame the payoff — a running stack you can build on in Module 3.

- You'll finish Module 2 with **DW running locally**, the **Postgres HR samples connected**, a
  **working LLM key**, and every service **verified healthy**.
- One launcher does the heavy lifting: **`./dwb doctor`** then **`./dwb up --with
  postgres`**. The rest of this module is what those commands set up and how to prove it.
- **Screenshot-driven** — this is where the "what right looks like" shots for the track
  come from.

_Speaker notes:_ Everyone runs this live from their Ubuntu shell (Module 1). By the last slide
they should have a green `./dwb status` and the UI open.

---

## Slide 2 — Objectives & starting point
**Beat:** the end-state verbs and the exact start line.

- **By the end you can:** (1) launch DW with the recommended default; (2) verify health
  via `curl :8000/api/health`, `./dwb status`, and the UI at `:5173`; (3) explain what
  each service is and why the box is heavy.
- **Starting point (from Module 1):** WSL2 Ubuntu running, `docker info` works inside it, and
  the **repo is on the Linux fs** (`~/working/data-workbench`). All commands run from the
  **Ubuntu shell** in that repo dir.
- **Prereq:** Module 1 complete.

---

## Slide 3 — Local is one deployment archetype (the easiest for enablement)
**Beat:** set expectations — this is a teaching topology, not the only one.

- DW can be deployed several ways (shared team server, secured multi-user, etc.). We use
  **local Docker Compose** because it's the **fastest, self-contained path** and every
  learner gets an identical stack.
- **Same topology, one machine:** the exact three-service core (frontend, backend, Neo4j)
  you'd run on a shared box — just pointed at `localhost` and driven by the `dwb` launcher.
- Auth, public URLs, and hardening (`WB_AUTH_SECRET`, `WB_PUBLIC_BASE_URL`) are **out of
  scope for local** — they belong to the shared-deployment story, not this lab.

> **Concept callout — why local first.** Everything you learn about services, ports,
> health, and the launcher transfers directly to a team deployment. Local just removes the
> networking + auth ceremony so you can focus on *operating* the product.

---

## Slide 4 — Size the machine first
**Beat:** the net-new hardware reality — check this before you launch.

- `[NET-NEW: hardware sizing]` **32 GB RAM minimum.** 16 GB will thrash or OOM once the
  stack, Neo4j's JVM, the embedding model, and a pipeline stage are all live at once.
- **CPU:** a modern **multi-core** processor (target **8 cores / logical CPUs**); pipeline
  stages are compute-heavy. `[VERIFY: exact core floor not repo-specified — 8 is a working
  recommendation]`
- **Disk:** generous **SSD headroom** — the images, named volumes (`neo4j_data`,
  `wb_data`, `wb_projects`), and the **~130 MB `bge-small` embedding model baked into the
  backend image** add up. Budget **~30–40 GB free**. `[VERIFY: figure is a net-new
  estimate, not a repo-published number]`
- On WSL2, remember RAM is shared with Windows — cap/raise it via `.wslconfig` (Module 1).

---

## Slide 5 — Why it's heavy (the concurrency note)
**Beat:** explain the sizing so it isn't arbitrary — and set scale expectations.

- `[NET-NEW: concurrency/scale note]` DW runs **many containers at once**, and two things
  dominate memory:
  - **Neo4j is a JVM** with a fixed, non-trivial heap footprint that's always resident.
  - **Each pipeline stage spawns a Claude Agent SDK subprocess** — run stages back-to-back
    or across projects and the CPU/RAM cost **multiplies per concurrent stage**.
  - The **embedding model** loads into memory for semantic features.
- **Scale implication:** the local box is comfortable for **one practitioner running one
  thing at a time**. Heavy parallelism (many concurrent stages / multiple users) wants a
  bigger box or a shared server — not this laptop. `[VERIFY: no hard concurrency limit is
  published in-repo; framed as guidance]`

_Speaker notes:_ This slide is why Slide 4 says 32 GB. Tie them together explicitly.

---

## Slide 6 — The run-model + the `dwb` launcher
**Beat:** meet the one tool you drive everything with.

- The stack is **Docker Compose**; **`./dwb`** is a stdlib-Python launcher (a thin bash
  shim) that wraps compose with preflight checks, sample-DB loading, and quick-connect
  wiring — so you never hand-craft `docker compose --profile …` lines.
- The commands you'll use:
  ```bash
  ./dwb doctor                 # preflight: docker, ports, secrets
  ./dwb up --with postgres     # RECOMMENDED default: core stack + HR Postgres + Gitea
  ./dwb status                 # resolved app + dependency health
  ./dwb logs [backend|frontend|neo4j|postgres|gitea|all]
  ./dwb down                   # graceful stop  (--volumes wipes everything)
  ./dwb reset                  # interactive soft/hard reset
  ```
- **Opt-in extras** ride `--with`: `postgres`, `mysql`, `storage`. **Gitea is on by
  default** (disable with `--no-gitea`).

---

## Slide 7 — Services & ports (know what's listening)
**Beat:** the map of everything the stack exposes on `localhost`.

- Lift this table verbatim (from the ground-truth fact sheet):

| Service | Host port(s) | Container | Default state | Notes |
|---|---|---|---|---|
| Frontend (SPA) | **5173** | 80 | on | `http://localhost:5173` |
| Backend (FastAPI + WS) | **8000** | 8000 | on | health: `GET /api/health` |
| Neo4j browser | **7475** | 7474 | on | knowledge graph UI |
| Neo4j bolt | **7688** | 7687 | on | `bolt://localhost:7688`; creds `neo4j/workbenchpass` |
| Postgres (HR sample) | **5433** | 5432 | profile-gated | the enablement **spine** — `--with postgres` |
| MySQL (HR sample) | **3307** | 3306 | profile-gated | alternate/optional source |
| Gitea | **3101** | 3000 | **on by default** | git provider for pushable packages |
| SeaweedFS (object store) | **9000 / 8888 / 9333** | — | **opt-in, off** | `--with storage`; optional for this track |

> **Concept callout — remapped Neo4j ports on purpose.** Neo4j is on **7475/7688**, not
> the standard 7474/7687, so it won't clash with a dev Neo4j. Inside the compose network
> the backend still reaches it at `neo4j:7687` — those host ports are only for *you*.

> **Concept callout — pluggable connectors: adding a new platform.** DW currently ships
> connectors for PostgreSQL, MySQL, Snowflake, Databricks, Oracle, and SQL Server via a
> pluggable connector architecture. With AI-accelerated development, adding a connector for
> a new platform is roughly **2–3 days of work** — the "what about our platform?" question
> from a client has a concrete, quantified answer. `[VERIFY: re-confirm current connector
> list and effort estimate before citing this in a live session]`

---

## Slide 8 — LLM access: the model credential (three routes)
**Beat:** the one credential the pipeline can't run without — and the three ways to get it.

- DW runs its agentic stages through **Anthropic Claude**. In our environment the route is
  **Azure AI Foundry**: set **`ANTHROPIC_FOUNDRY_API_KEY`** in a **gitignored `.env`** at
  the repo root (next to `docker-compose.yml`).
  Foundry also needs the **resource name** — the Azure resource your model is deployed
  on. It has **no default**; `./dwb doctor` fails hard if you set the key without it.
  ```bash
  # .env  (gitignored — never commit; start from the committed .env.example)
  ANTHROPIC_FOUNDRY_API_KEY=<your-foundry-key>
  ANTHROPIC_FOUNDRY_RESOURCE=<your-azure-foundry-resource>
  ```
- **Direct-Anthropic is the fallback:** if you have a direct key, set `ANTHROPIC_API_KEY`
  instead — the SDK picks it up automatically, in both compose and host mode. Foundry
  takes precedence when both are set.
- **Third route — your own Claude Code subscription** (for a workstation with neither a
  Foundry key nor API credit). Run `claude setup-token` on your machine and paste the
  printed token into `.env` as `CLAUDE_CODE_OAUTH_TOKEN`; it's valid for a year and works
  in compose mode too. It's a **personal credential billed to your subscription** — your
  laptop only, never a shared or client deployment. Lowest precedence of the three.
  ```bash
  CLAUDE_CODE_OAUTH_TOKEN=<token printed by `claude setup-token`>
  ```
- **Without a key the app still boots** — but any stage that calls the model **fails**.
  `./dwb doctor` flags a missing key as a warning (next slide but one).

> **Concept callout — only one external call.** The entire DW stack runs locally in
> containers — Neo4j, the backend, the frontend, the sample databases. The **only thing that
> leaves your machine** is the Anthropic LLM call. Everything else is contained.
>
> **Concept callout — Claude Code is optional.** You do **not** need Claude Code to run the
> platform. The Foundry key (or direct Anthropic key) is all that's required. Claude Code is
> useful for those who want to explore or modify the codebase (Module 6), but it is not a
> dependency to install or subscribe to before Module 2. The flip side: if you *do* already
> have a Claude Code subscription, that's sufficient on its own — `claude setup-token` gets
> you a credential the container stack can use, so you don't need to wait on a key to
> finish this module.

_Speaker notes:_ Don't paste real keys on a shared screen. The next two slides are the
governance + hygiene rules — cover them before anyone types a key live.

---

## Slide 9 — Key governance: provisioning + rotation
**Beat:** the net-new policy — where the key comes from and how long it lives.

- `[NET-NEW: provisioning]` **The team provisions the Foundry key** — you do **not** mint
  your own. Request it through the team's onboarding channel; you'll receive a scoped key
  to drop into your local `.env`.
- `[NET-NEW: rotation/expiry policy]` Keys are governed by a **30-day rotation / expiry
  policy**: assume any key **expires after 30 days** and must be **rotated**. Symptom of an
  expired key = stages that fail to reach the model even though the stack is healthy.
- **On rotation:** replace the value in `.env`, then `./dwb restart backend` (or
  `./dwb down && ./dwb up --with postgres`) so the backend re-reads it. Old keys are
  revoked — don't keep them around.
- **The subscription token is governed differently.** `CLAUDE_CODE_OAUTH_TOKEN` is **yours**,
  not the team's: you mint it, it lives **a year**, and it bills **your** subscription. The
  30-day policy doesn't apply, but everything on the next slide does — and because it carries
  your identity, it must never travel to a shared or client environment. Revoke it from your
  Claude account settings if it's ever exposed.

---

## Slide 10 — Secret hygiene (non-negotiable)
**Beat:** the net-new rules that keep a live key out of the wrong place.

- `[NET-NEW: secret hygiene]` **Never screenshot `.env`** and never put a key in a slide,
  a chat message, a ticket, or a commit. The `.env` file is **gitignored for a reason** —
  keep it that way; don't `git add -f` it.
- Treat the key as **live**: a real, working Foundry key sits in `.env` on disk once you're
  set up. If a key is exposed, **rotate immediately** (Slide 9) — don't wait for the 30-day
  cycle. Same rule for a `CLAUDE_CODE_OAUTH_TOKEN` — a year-long credential on your personal
  subscription is *more* worth protecting, not less.
- When you capture the Module 2 screenshots for the deck, **frame terminals to exclude the key**
  — `./dwb doctor` reports the key as present/absent without printing its value, so shoot
  that instead of the file.

> **Concept callout — the doctor never leaks.** `./dwb doctor` tells you the key is set
> ("LLM key (ANTHROPIC_FOUNDRY_API_KEY → Azure Foundry)", or "LLM key (CLAUDE_CODE_OAUTH_TOKEN
> → Claude Code subscription)") **without echoing the secret** — exactly what you want on a
> shared screen.

---

## Slide 11 — Preflight with `./dwb doctor`
**Beat:** the gate that catches problems before they waste a build.

- Run it first, every time:
  ```bash
  ./dwb doctor
  ```
- It checks, with actionable fixes: **Docker CLI + daemon** running, **`docker compose`
  v2** present, the **ports** you'll need are **free**, and **secrets** (`.env` LLM key +
  MCP tokens) are set.
- **Hard failures abort `up`; everything else is an advisory warning.** A missing LLM key
  or MCP token shows as a **warning**, not a blocker — the stack still comes up.

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `dwb-doctor`
Caption: `./dwb doctor` preflight output.
Must show: The preflight run with green ✓ lines for the docker daemon, compose v2, free
ports, and the LLM-key line — **no secret value visible**.
Taken at: In the Ubuntu shell at the repo root, before the first `up`.

---

## Slide 12 — Launch: `./dwb up --with postgres`
**Beat:** the recommended default — one command, whole stack + samples.

- The one you'll actually run for this track:
  ```bash
  ./dwb up --with postgres
  ```
- This brings up the **core stack** (frontend, backend, Neo4j), the **HR Postgres
  samples**, and **Gitea** (on by default), then **loads the sample databases** and waits
  for health. The **first build is slower** — it bakes the ~130 MB embedding model — and
  is cached after.
- **MySQL, the object store, and Databricks are alternate/optional** — Postgres is the
  **spine**. Only reach for `--with postgres,mysql` or `--with postgres,mysql,storage` if a
  later exercise calls for them.

> **Concept callout — "Quick connect" is auto-wired.** `up` writes a quick-connect
> manifest of the loaded sample DBs, so they appear as **prefills** in the connection
> forms later (Module 3) — no hand-typing host/port/credentials.

---

## Slide 13 — Everyday lifecycle
**Beat:** the handful of commands you'll use day to day.

- **Status:** `./dwb status` — resolved app + per-container health.
- **Logs:** `./dwb logs backend` (or `frontend` / `neo4j` / `postgres` / `all`) — tails
  the last lines and follows.
- **Stop:** `./dwb down` keeps your data (named volumes persist); **`./dwb down --volumes`
  wipes everything** for a clean slate.
- **Reset:** `./dwb reset` — interactive soft/hard reset. **Restart a proc:** `./dwb
  restart backend`.

_Speaker notes:_ Emphasise the `down` vs `down --volumes` distinction — the second one
deletes the graph and SQLite. People conflate them and lose work.

---

## Slide 14 — Outcome — what "done" looks like
**Beat:** the concrete green end-state, with the money shots.

- `curl http://localhost:8000/api/health` returns OK.
- **`./dwb status`** shows the stack up and every container **running/healthy**.
- The **UI loads at `http://localhost:5173`** — the persona chooser (Product vs
  Engineering Workbench) is visible.

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `dwb-status-green`
Caption: `./dwb status` — all services green.
Must show: The status output with the container list and **running/healthy** states in
green for backend, frontend, neo4j, postgres, gitea.
Taken at: After `./dwb up --with postgres` completes, run `./dwb status`.

[SCREENSHOT PLACEHOLDER — SIZE: Large]
Name: `ui-landing-5173`
Caption: The DW web UI at `http://localhost:5173`.
Must show: The landing/persona-chooser page with the DW header and the choice of Product
Workbench vs Engineering Workbench; URL bar showing `localhost:5173`.
Taken at: Open `http://localhost:5173` in the browser after the stack is healthy.

---

## Slide 15 — Verify the sample DBs: Quick connect
**Beat:** confirm the HR samples are loaded and offered as prefills.

- The Postgres samples loaded by `--with postgres` (**hr**, **banking**,
  **products_sales**) surface as a **Quick connect** prefill in the connection forms — no
  coordinates to type.
- The one that matters for the flagship is **`hr-postgres`** (database `hr`, schema
  `hr_core`) — you'll build the two source products from it in **Module 3**.
- If they don't appear, re-run **`./dwb connect`** to rewrite the quick-connect manifest,
  then reload the form.

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `quick-connect-list`
Caption: Sample DBs offered as Quick connect prefills.
Must show: The "Select Data Source" / connection form with the Quick-connect list
including **hr-postgres** (and the other loaded samples) as selectable prefills.
Taken at: In the UI, Engineering Workbench → a source-connection form, after `up`.

---

## Slide 16 — Peek at the graph: Neo4j browser
**Beat:** confirm the knowledge-graph service is reachable — the DW backbone.

- Open **`http://localhost:7475`** and log in with **`neo4j` / `workbenchpass`** (bolt is
  on `7688`).
- Right now the graph is nearly empty — that's expected; DW **accretes** nodes as you run
  discovery/profiling/etc. in Module 3+. Confirming you can reach the browser is the point.
- You won't query it by hand in this track — the DE **Ask** panel (Module 8) reasons over it for
  you — but knowing where it lives makes lineage (Module 4) and the under-the-hood module (Module 5)
  concrete.

[SCREENSHOT PLACEHOLDER — SIZE: Medium]
Name: `neo4j-browser-7475`
Caption: The Neo4j browser at `:7475`, logged in.
Must show: The Neo4j Browser UI at `localhost:7475`, signed in as `neo4j` (the query
prompt / empty result is fine) — proving the graph service is reachable.
Taken at: Open `http://localhost:7475`, log in with `neo4j` / `workbenchpass`.

---

## Slide 17 — Validation & next steps
**Beat:** the check you run yourself, then hand off to the flagship.

- **Validation (run these):**
  1. `curl http://localhost:8000/api/health` → OK.
  2. `./dwb status` → all containers **green**.
  3. Open **`http://localhost:5173`** → the UI loads; open **`http://localhost:7475`** →
     the Neo4j browser logs in.
  4. A source-connection form shows **hr-postgres** as a Quick connect prefill.
- **Self-check:** Why 32 GB RAM? Where does the LLM key live, and how often is it rotated?
  What's the difference between `./dwb down` and `./dwb down --volumes`?
- **Next:** **Module 3 — Flagship Pt 1: Build Two Source-Aligned Products** — connect
  `hr-postgres` and run the discovery → serve pipeline for real.
