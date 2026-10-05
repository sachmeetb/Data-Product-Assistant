# Bootstrapping, model routing & hooks — research + POC

Research from **[ringer](https://github.com/NateBJones-Projects/ringer)** (a
parallel agent-swarm orchestrator) applied to Data Workbench's MCP personas.
Niel flagged four ringer ideas: (1) prompt-based **bootstrapping**, (2) **cheaper
models when the task allows**, (3) orchestration applied **per MCP**, and (4)
**pre/post hooks + nudges** to keep an agent on-process. This doc records how each
maps to Data Workbench and what's built vs. proposed.

## 1. Bootstrapping — BUILT (POC) ✅

**ringer:** `./ringer.py install-agent` drops the orchestrator skill + two nudge
hooks at user level; config is copy-and-customize (`config.sample.toml` →
`~/.config/ringer/config.toml`). The point Niel liked: *a single entry point sets
everything up* instead of manual download/config.

**Data Workbench:** we already had per-persona curl installers (`engineer-kit`,
`po-kit`) that write `.mcp.json` + install the guide skill + `/`-commands +
Codex config. The new layer is the **prompt-based bootstrap**: one prompt the user
pastes into a *fresh* Claude Code that drives the entire setup itself.

- Endpoint: `GET /api/bootstrap` (`routers/bootstrap.py`) → returns a paste-able
  prompt with the backend URL injected. `?persona=po|de` pre-selects the kit.
- Prompt: `bootstrap/bootstrap-prompt.md`. It: asks persona → checks/records
  `WORKBENCH_TOKEN` → runs the persona's `install.sh` via curl → tells the user to
  restart → hands off with a persona-appropriate first sentence.
- Flow now: **`curl -fsSL <backend>/api/bootstrap` → paste into Claude Code →
  everything set up.** No knowing-which-installer, no manual `.mcp.json`.

Chicken-and-egg handled: a fresh session has no MCP/skill, but it has Bash — the
prompt is plain NL + one curl, so it self-installs, then the restart brings the
MCP + skill online.

**Cross-tool (Claude Code / Cursor / Codex).** ringer's `install-agent` is
Claude-Code-only and its "multiple engines" are *worker models*, not IDEs — so
cross-tool is our own extension of the idea. Rather than *detect* the tool (bash
can't reliably), the installers **write config for every client**, so whichever
one you launch reads its own:
- Claude Code → `.mcp.json` + `.claude/skills/` + `/`-commands
- Cursor → `.cursor/mcp.json` (same `{mcpServers:{…}}` shape) + `.cursor/rules/*.mdc`
- Codex → `.codex/config.toml` + `AGENTS.md`
All merge-safe (rerunning / installing both personas merges servers, never
clobbers). The bootstrap prompt additionally has the agent identify its *own*
client to give the right restart step. **Tested:** Claude Code + Codex end-to-end;
Cursor files are written in the documented format but not launch-verified here (no
Cursor in this env). Caveat: `${WORKBENCH_TOKEN}` header expansion is client-
dependent (same as Claude Code) — local open-mode ignores the token anyway; for a
real deploy a client that doesn't expand env vars needs the direct add-server path.

## 2. Cheaper models when the task allows — PROPOSED (after Wed)

**ringer:** per-task `engine`/`model` in the manifest; a cheap default
(`z-ai/glm-5.2`, ~20-30× cheaper output) for mechanical/tightly-specced work, and
evidence-based tiers (proven / probation / untested) from a local `runs.jsonl`
log — "mechanical or tightly-specced tasks on the cheap lane, gnarly ones on the
frontier engine."

**Data Workbench mapping — route per STAGE, not per task:**
- **No-LLM already:** mechanical stages (`odcs_to_dprod`, `mark_*_complete`,
  `synthesize_odcs_from_graph`, `auto_mapping_sa`) run without a model — free lane,
  already the case.
- **Cheap lane (tightly-specced LLM):** `metadata_enrichment`,
  `source_naming_recommendations`, the one-shot advisors (name/schema/rule/gap,
  filter-intent, rationale-summarizer). Deterministic-ish, low blast radius.
- **Frontier lane (open-ended reasoning):** `data_mapping`, `data_serving_*` —
  cross-product joins, transform authoring, DDL generation.
- **How:** add an optional `model` (or `model_tier`) to `STAGE_REGISTRY` entries;
  thread it into `ClaudeAgentOptions` at the `sdk_runner` call site (Foundry is
  currently pinned to `claude-opus-4-8` in `config.py` — this would let a stage
  opt down). Evidence tiering is *free*: we already log per-stage usage/outcome in
  `LlmUsageEvent` — the analog of ringer's `runs.jsonl` — so a first-try-pass-rate
  per stage×model could drive routing later.

## 3. Orchestration per MCP — PROPOSED

**ringer:** per-task workers, each isolated (own dir/log/verdict), a manifest fan-out.

**Data Workbench:** the pipeline IS the orchestration — stages fan out to data
agents server-side. "Per MCP" here means the routing/model/hook policy lives with
each control plane (`/mcp` engineer, `/po-mcp` owner) and each **stage** carries
its own engine/tier + guardrails (see #2, #4). No new orchestrator needed; the
lever is stage-level metadata + the SDK call site.

## 4. Hooks (pre/post) + nudges — PROPOSED (after Wed)

**ringer:** `install-agent` adds a **Bash hook** and an **edit-loop hook** that
**nudge ONCE per session, never block**, pointing the agent back to the
orchestrator skill ("reminders decay, so enforcement ships with the product").

**Data Workbench — Claude Code hooks in the kits' `.claude/settings.json`:**
- **PreToolUse nudge:** before a mutating MCP call (`run_stage`, `save_product_spec`,
  `submit_product_spec`, `deploy_*`), remind the agent it's in the PO/DE workflow
  and to follow the workbench guide (don't skip approval gates, don't bypass the
  process). One nudge/session, non-blocking — matches ringer.
- **PostToolUse guardrail:** after `run_stage`, remind it to poll with backoff and
  read `error_message` on failure (reinforces the fixes already shipped); after
  `data_mapping`, remind to approve mappings before serving.
- **These complement, not replace, the server-side gates** we already added
  (serving-mappings gate, required-config gate) — hooks nudge the agent; the gates
  are the hard backstop.

**Nudges** are the same mechanism aimed at drift: keep the agent from sliding into
raw `run_cypher`/manual SQL instead of the guided tools, or from reciting wizard
steps (PO). Cheap to prototype in `settings.json` hooks; high UX payoff.

## Status
- **Bootstrapping POC: shipped** (`/api/bootstrap` + `bootstrap/bootstrap-prompt.md`).
- Model routing, per-MCP policy, hooks/nudges: **designed here, deferred to after
  Wednesday** per the meeting deadlines.
