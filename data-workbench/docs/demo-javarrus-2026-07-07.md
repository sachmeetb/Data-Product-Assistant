# Demo — Data Workbench CLI/MCP (Javarrus review, 2026-07-07)

> **One-line pitch:** "Watch me take a data product through its whole lifecycle —
> define it, hand it to engineering, serve it, and inspect the results — without
> ever opening the browser."

**Duration:** ~15 min · **Audience:** Javarrus · **Personas shown:** Product Owner + Data Engineer

---

## What this proves (map to the meeting)

- #2 Full CLI capability incl. consumer-aligned products
- #4 PO can define products via MCP (wizard → tools)
- #5 CLI UX — triage + deep links, not a recreated UI
- The **read-backs** are the payoff: you can *see* the score and the served data in the terminal.

---

## Setup (before the meeting)

```bash
cd ~/dataworkbench-new/data-workbench
docker compose up -d                       # backend + neo4j + (frontend for the link reveal)
curl -s http://localhost:8000/api/health   # -> {"status":"ok"}
```

- Two MCP servers registered in `.mcp.json`: `workbench` (DE, `/mcp`) + `workbench-po` (PO, `/po-mcp`).
- Real data already loaded: 10 products incl. **NBA Game REsults** (`dpe-06262026-01`, 100% complete)
  — this is the **fallback** if any live step misbehaves.
- Have the UI open in a background tab (`http://localhost:5173`) for the closing link reveal.

---

## Script

### Act 1 — The PO orients (orchestration pattern)  ~2 min
> "A PO starts every session with one call that tells them what needs action."

```
get_po_summary(owner_email="sodbayar.ganbat@accenture.com")
```
Point out: the `action_items` are priority-sorted, each portfolio row has a `web_url`, and `next`
names the exact tool to run. **The agent never asks "what do you want to do" — the tool tells it.**

### Act 2 — Triage a validation gate (CLI-UX headline)  ~3 min
> "Here's the hard part everyone worries about — reviewing 39 items in a terminal. We don't dump them,
> we triage them."

```
get_pending_validations(project_code="dpe-sa-06252026-05")   # Players Compensation
```
Point out the `triage.summary`: *"17 items: 15 routine, 2 need your eyes (2 possible PII/sensitive
columns)."* The `needs_attention` list shows `salary` and `contract_total` flagged — **the agent read
17 items and surfaced the 2 a human actually cares about.** Then:

> "The routine 15 I clear in one call; the flagged 2 I decide on."

```
review_validation_item(project_code="dpe-sa-06252026-05", review_type="names",
                       action="approve", item_uri="<salary col_uri>", quality=2)
bulk_approve_source_validation(project_code="dpe-sa-06252026-05")
```

**Talking point:** the `possibly_sensitive` flag came from a *name heuristic*, not the graph — because
the graph's sensitivity field is usually empty. That's the kind of learning we're capturing as a pattern.

### Act 3 — Define a consumer product, PO-side (item #4)  ~3 min
> "A PO builds consumer products too — contract-first. That whole wizard is now MCP tools on the PO server."

```
list_marketplace(product_kind="source", domain="sports")     # find sources
start_consumer_product(owner_email=..., owner_name=..., domain="sports",
                       name="Demo Game Results", product_idea="one row per game with scores")
find_source_products(spec={"name":"Demo Game Results","domain":"sports"})
save_product_spec(project_code="<new>", spec={...})          # schema + inputs
submit_product_spec(project_code="<new>", submitted_by=...)  # hand to engineering
```
> "No UI touched. The PO shaped the contract and handed it off."

*(If time is tight, skip the live create and narrate against the existing NBA product instead.)*

### Act 4 — Engineer serves it  ~2 min
> "Switch to the engineer hat — different MCP server, engineering tools."

```
get_plan_summary(project_code="dpe-06262026-01")   # recommended_next walks the pipeline
# (already complete for NBA — narrate the sequence: mapping → bulk_approve_mappings →
#  get_join_preflight → serving → deploy)
```

### Act 5 — The payoff: SEE the results in the terminal  ~3 min
> "This is the part that makes 'no-UI' real — I can inspect what I built without the browser."

```
preview_serving_view(project_code="dpe-06262026-01", limit=5)
```
→ real game rows, right in the terminal.

```
get_osi_evaluation(project_code="dpe-06262026-01")
```
→ amber/56, and it shows **only the 3 failing criteria** (heaviest first), not the whole rubric.

> "And every one of these carries a `web_url` — so when I *do* want the rich view…"

**Closing reveal:** click the `web_url` from `get_osi_evaluation`, land on the exact OSI panel in the
UI. "Same system. The CLI is the agent surface, the UI is the human surface, and they're one backend."

---

## Fallback plan (if a live step fails)

- Any live create/serve hiccup → pivot to **NBA Game REsults** (`dpe-06262026-01`), which is fully
  built. Acts 1, 2, 5 all work against existing data and are the strongest parts anyway.
- MCP client connection hiccup → the tools also work via the REST API (`/api/*`); worst case narrate
  from `CHANGES.md` validation results.

---

## If asked "what's left?" (be honest)

- **P2 polish** tools (marketplace detail, gap logging, usage/cost, stage history) — workarounds exist.
- **Column-level transform authoring** via MCP — dataset-level shape is covered; column DSL is the
  next capability gap.
- The work's real output is **specs + patterns** (`docs/mcp-cli-ux-patterns.md`) for Niel to fold in.
