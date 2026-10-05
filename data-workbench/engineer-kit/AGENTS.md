# Data Workbench — driving this project over MCP

You are helping a **data engineer** drive a Data Workbench project through the
**`workbench` MCP server** (registered in this project). The data-engineering
work (discovery, profiling, mapping, serving, DQ) runs **server-side** — you
orchestrate it via MCP tools. Don't try to run pipeline skills locally; you
don't have them and don't need DB credentials. Always pass the **project code**
and scope `run_cypher` to it.

This file is the Codex / generic-MCP-client analogue of the Claude Code
`workbench-guide` skill. (Claude Code reads `.claude/skills/`; this `AGENTS.md`
gives the same orientation to clients that don't.)

## Tools (auto-discovered from the server)

Codex (and any raw MCP client) sees the **full surface — all 142 tools**
(`workbench/backend/mcp_server.py`; a raw MCP client auto-discovers every one). The
**canonical per-tool reference** is **`docs/mcp-architecture.md`** in the workbench
repo; the groups below are the orientation, not a substitute (the DE server also drives
the full consumer-aligned product lifecycle, **data migration (`dmig`)**, **code
migration (`cmig`)**, lakehouse/transfer serving, connections, inbound intake, and the
marketplace/DQ/OSI/QA read surface — see the reference). Pick the tool by each stage's
`execution_kind`.

- **Orient / read** — `list_projects`; `get_project_state` (stages + statuses
  grouped by workflow, PO brief, web_url; each stage's `execution_kind` tells
  you the tool: `llm`/`backend` → `run_stage`, `mechanical` → `complete_stage`,
  `review_gate` → `review_*`); `get_plan_summary` (compact digest — see Status
  cadence); `get_stage_results` (structured stage output by `card`); `run_cypher`
  (read-only, project-scoped); `get_dbt_project` (a materialized product's dbt
  files); `get_dataset_filter` (a product's row filter + compiled predicate).
- **Run stages** — `set_data_source` (before Data Discovery); `run_stage`
  (async — returns a `run_id` and keeps running; poll `get_project_state`);
  `get_stage_config_options`; `complete_stage` (mechanical lifecycle steps);
  `reset_stage`.
- **Serving / lifecycle config** — `set_serving_mode` (virtual ↔ materialized);
  `set_materialization_target` (the dbt target Postgres); `set_dataset_filter`
  (finalize an output dataset's row filter; SQL only, runs the deploy safety
  gate); `accept_request` (accept the PO's product request — unlocks the dpe-sa
  PO validation gate / the dpe-cf `integration` workflow).
- **Interactive** — `get_pending_questions` / `answer_question` for stages that
  pause to ask (e.g. Data Discovery's "which tables?").
- **Review writes** (require a declared `role`) — `review_description`,
  `review_mapping`, `review_domain_rule`, `review_table_description`,
  `review_relationship_description`.
- **Semantic Q&A** — `query_semantic_layer(domain, question)` for NL questions
  over a domain's deployed views (domain-scoped; returns markdown).
- **Serving beyond virtual/dbt** — `get_serving_advice`; lakehouse
  (`build_lakehouse_package` → `export_lakehouse`, `get_lakehouse_status`/`_package`);
  cross-platform transfer (`get_placement_advice` / `set_transform_placement` →
  `run_transfer`, `get_transfer_status`); packages + git (`build_dbt_package`,
  `get_view_package`, `push_to_git`); `get_transform_preflight`;
  `run_deployment_reflection` (runs the reflector — `complete_stage` only marks it).
- **Data migration (`dmig`)** — `configure_migration` → `run_stage` assess/generate →
  `run_migration_snapshot` → `run_migration_reconcile`; `get_migration_status` /
  `get_migration_package`; schema-only via `set_intake_execution_mode`,
  `get_physical_schema` / `confirm_physical_schema`, `seed_migration_schema`,
  `flip_migration_to_live`.
- **Code migration (`cmig`)** — `list_eligible_dmig` → `link_code_migration` →
  `import_code` → `configure_code_migration` → `run_stage cmig_reverse_engineer`
  (review via `get_code_spec` / `update_code_spec` / `approve_code_spec` /
  `reopen_code_spec`) → `run_stage cmig_forward_engineer` → `get_code_migration_package`;
  `get_code_migration_status`.
- **Connections / sources** — `list_platforms` / `get_platform_capabilities`;
  `list_connections` / `create_connection` / `test_connection`; `get_source_binding` /
  `set_source_binding`; `list_source_namespaces` / `list_source_tables`.
- **Inbound intake (migration)** — `list_intake_submissions` / `get_intake_submission`
  / `approve_intake_submission` / `reject_intake_submission`; `set_intake_execution_mode`.

## Status cadence — keep the engineer oriented

Report status automatically, not on request. Call `get_plan_summary {project_code}`
and render a **short** digest:

- when you start working on a project,
- after every state change (a stage finishes, a `complete_stage`, a review write,
  a workflow add/remove),
- on each poll while watching a `run_stage` (report transitions, not noise).

Keep it tight, e.g.:

```
Customer Master — 12/14 (86%) · deployed
  ✓ done: discovery, profiling, enrichment, naming, materialize, deploy …
  ▶ running: —
  → next: Mark Engineering Complete  → complete_stage
  ⚠ blocked: —
  ↗ http://…/engineer/projects/1
```

Lead with `recommended_next` (+ the `tool` to run it) so the engineer always
knows the one next move. If it's null, the plan is complete. Don't dump the raw
stage list unless asked — `get_plan_summary` is the digest.

## Lifecycle loop

1. **Orient** — `list_projects`, then `get_project_state` / `get_plan_summary`.
2. **Run the next stage** — `run_stage` (config-gated stages: call
   `get_stage_config_options` first, get the user's choice, pass `config`).
3. **Watch + answer** — poll `get_project_state` until it leaves `running`; on
   each poll also `get_pending_questions {run_id}` and relay any question to the
   user, then `answer_question`. Questions time out (~300s) to a default.
4. **Inspect** — `get_stage_results` (preferred) or scoped `run_cypher`.
5. **Fix + re-run** — `reset_stage` then `run_stage`.
6. **Hand off** — `awaiting_review` / `complete` stages are approved by the
   PO/steward in the web UI (or via the `review_*` tools if that's your role).

`run_stage` is **async** — never assume it finished when it returns; poll.
