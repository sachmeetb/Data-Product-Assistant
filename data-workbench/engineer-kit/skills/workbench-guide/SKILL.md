---
name: workbench-guide
description: >-
  Drive a Data Workbench project from your own Claude Code. Use whenever the
  user wants to work on a workbench-managed data product/project — list their
  projects, inspect a project's pipeline state, run pipeline stages
  (server-side), query the project's knowledge graph, or understand the data
  product lifecycle. The actual data-engineering skills run on the workbench
  server; you orchestrate them via the `workbench` MCP tools.
allowed-tools: mcp__workbench__accept_request, mcp__workbench__add_workflow, mcp__workbench__answer_question, mcp__workbench__approve_code_spec, mcp__workbench__approve_intake_submission, mcp__workbench__approve_materialization_full, mcp__workbench__backfill_migration_targets, mcp__workbench__build_dbt_package, mcp__workbench__build_lakehouse_package, mcp__workbench__build_materialization_sample, mcp__workbench__bulk_approve_mappings, mcp__workbench__complete_product_request, mcp__workbench__complete_stage, mcp__workbench__configure_code_migration, mcp__workbench__configure_migration, mcp__workbench__confirm_physical_schema, mcp__workbench__create_connection, mcp__workbench__create_consumer_product, mcp__workbench__deploy_virtual_view, mcp__workbench__discard_draft_version, mcp__workbench__execute_qa_question, mcp__workbench__export_lakehouse, mcp__workbench__flip_migration_to_live, mcp__workbench__get_assignment, mcp__workbench__get_available_actions, mcp__workbench__get_code_migration_package, mcp__workbench__get_code_migration_status, mcp__workbench__get_code_spec, mcp__workbench__get_dataset_filter, mcp__workbench__get_dataset_transform, mcp__workbench__get_dbt_project, mcp__workbench__get_deployment_reflection, mcp__workbench__get_dq_package, mcp__workbench__get_dq_rules, mcp__workbench__get_dq_score, mcp__workbench__get_dq_test_runs, mcp__workbench__get_edit_diff, mcp__workbench__get_intake_submission, mcp__workbench__get_join_preflight, mcp__workbench__get_lakehouse_package, mcp__workbench__get_lakehouse_status, mcp__workbench__get_mapping_graph, mcp__workbench__get_mapping_rationale_report, mcp__workbench__get_marketplace_detail, mcp__workbench__get_marketplace_lineage, mcp__workbench__get_materialization_status, mcp__workbench__get_migration_package, mcp__workbench__get_migration_status, mcp__workbench__get_odcs_spec, mcp__workbench__get_okf_bundle, mcp__workbench__get_osi_evaluation, mcp__workbench__get_pending_questions, mcp__workbench__get_physical_schema, mcp__workbench__get_placement_advice, mcp__workbench__get_plan_summary, mcp__workbench__get_platform_capabilities, mcp__workbench__get_product_report, mcp__workbench__get_project_state, mcp__workbench__get_provenance, mcp__workbench__get_semantic_discovery_status, mcp__workbench__get_serving_advice, mcp__workbench__get_source_binding, mcp__workbench__get_stage_config_options, mcp__workbench__get_stage_executions, mcp__workbench__get_stage_output, mcp__workbench__get_stage_results, mcp__workbench__get_stale_mappings, mcp__workbench__get_transfer_batch_schema, mcp__workbench__get_transfer_status, mcp__workbench__get_transform_preflight, mcp__workbench__get_unmapped_columns, mcp__workbench__get_upstream_drift, mcp__workbench__get_usage_summary, mcp__workbench__get_view_package, mcp__workbench__import_code, mcp__workbench__ingest_odcs_spec, mcp__workbench__interpret_filter_intent, mcp__workbench__link_code_migration, mcp__workbench__list_assignments, mcp__workbench__list_available_workflows, mcp__workbench__list_connections, mcp__workbench__list_eligible_dmig, mcp__workbench__list_ingest_drafts, mcp__workbench__list_intake_submissions, mcp__workbench__list_marketplace_gaps, mcp__workbench__list_marketplace_products, mcp__workbench__list_platforms, mcp__workbench__list_projects, mcp__workbench__list_rejection_categories, mcp__workbench__list_source_namespaces, mcp__workbench__list_source_tables, mcp__workbench__log_marketplace_gap, mcp__workbench__match_inputs, mcp__workbench__preview_serving_view, mcp__workbench__probe_qa_question, mcp__workbench__push_to_git, mcp__workbench__query_semantic_layer, mcp__workbench__rebind_stale_mapping, mcp__workbench__reject_intake_submission, mcp__workbench__reject_materialization_sample, mcp__workbench__reject_product_request, mcp__workbench__reject_request, mcp__workbench__remove_workflow, mcp__workbench__reopen_code_spec, mcp__workbench__request_source_candidates, mcp__workbench__reset_semantic_discovery, mcp__workbench__reset_stage, mcp__workbench__review_description, mcp__workbench__review_domain_rule, mcp__workbench__review_mapping, mcp__workbench__review_relationship_description, mcp__workbench__review_table_description, mcp__workbench__run_cypher, mcp__workbench__run_deployment_reflection, mcp__workbench__run_gap_analysis, mcp__workbench__run_migration_reconcile, mcp__workbench__run_migration_snapshot, mcp__workbench__run_readonly_sql, mcp__workbench__run_semantic_discovery_step, mcp__workbench__run_stage, mcp__workbench__run_transfer, mcp__workbench__save_odcs_spec, mcp__workbench__seed_migration_schema, mcp__workbench__select_exclusive_group, mcp__workbench__send_upstream_pushback, mcp__workbench__set_data_source, mcp__workbench__set_dataset_filter, mcp__workbench__set_dataset_joins, mcp__workbench__set_intake_execution_mode, mcp__workbench__set_materialization_target, mcp__workbench__set_serving_mode, mcp__workbench__set_source_binding, mcp__workbench__set_transform_placement, mcp__workbench__submit_product_spec, mcp__workbench__suggest_domain_rules, mcp__workbench__test_connection, mcp__workbench__trigger_osi_score, mcp__workbench__update_code_spec
---

# Data Workbench — engineer guide

You are helping a **data engineer** work on a project managed by the **Data
Workbench**, a remote service you reach through the `workbench` MCP server.
**Execution is server-side**: discovery, profiling, mapping, serving, DQ, etc.
all run on the workbench server using its own skills. Your job is to **drive and
observe** that pipeline through the MCP tools — never to run those skills
locally.

## The MCP tools

| Tool | What it does |
|---|---|
| `list_projects` | The projects your token can access (code, name, archetype, domain). |
| `get_project_state` | A project's pipeline stages + their status (pending / running / awaiting_review / complete / failed), grouped by workflow, plus the PO `brief` and a `web_url`. Each stage carries an **`execution_kind`** (`llm`/`backend` → `run_stage`, `mechanical` → `complete_stage`, `review_gate` → `review_*`/UI) so you pick the right tool first try. Your primary "where are things?" tool. |
| `get_plan_summary` | A compact status digest: `percent_complete`, `completed` / `running` / `awaiting_review` / `blocked` step lists, and the single `recommended_next` step (with the `tool` to run it). De-duped by stage in lifecycle order. **Use this for status updates** (see "Status cadence" below) instead of re-deriving from `get_project_state`. |
| `get_stage_results` | **Reach for this first** to review a stage's output ("review the enrichment results", "show the mappings/DQ rules/profiling", "review the materialized contract"). Returns the structured result with review `status` via the SAME vetted query the web UI uses — no hand-written Cypher, no cross-join risk. Pick a `card` (descriptions / final_descriptions / profiling / mappings / dq_rules / datasets / columns / allowed_values / inputs / serving / data_products / **contract**); pass `table=` to scope to one dataset. |
| `run_stage` | Start a pipeline stage **running on the server**. Returns a `run_id` and `status:"started"` immediately — it does NOT block. Poll `get_project_state`. Some stages need a `config` (see `get_stage_config_options`). |
| `get_stage_config_options` | The config a stage needs before running. For **Data Discovery** it returns the available `discovery_schemas` / `discovery_tables` to pick from. If it returns `{needs_data_source: true}` the source DB isn't configured yet → call `set_data_source` first; if it returns an `error`, the connection is set but unreachable (surface the reason to the user). |
| `run_cypher` | Read-only, project-scoped Cypher against the project's Neo4j graph. Scope every query to the project (e.g. `(:Project {projectCode:'<code>'})`). Inspect catalogs, columns, descriptions, mappings, DQ rules, contracts, data products. |
| `set_data_source` | Configure the project's **source DB connection** (host/port/database/username/password/schema) — the MCP equivalent of "Select Data Source". **Required before Data Discovery.** Also completes the `select_data_source` stage. From the container, a host DB is `host.docker.internal`. |
| `reset_stage` | Set a stage back to `pending` so it can be re-run. |
| `complete_stage` | Complete a mechanical **lifecycle** stage (no data agent runs) — the MCP equivalent of the UI's "Complete" button (Mark Discovery Complete, Mark Engineering Complete, ODCS→dprod, Publish, …). Mechanical transition; same side-effects as the UI (e.g. stamps `discovery_complete_at`). Returns a `produced` summary for stages with countable output (e.g. `{schemas, properties}`, `{data_products, product_columns}`, `{mappings}`) so you can confirm it did something. Refuses data-agent stages (use `run_stage`), DQ stages, and review gates (use `review_*`). |
| `list_assignments` | The incoming product-request queue your token can see (filter by `project_code` / `status`). **Start every project session here** — if an assignment is `submitted`, accept or reject it before any engineering work. |
| `get_assignment` | One product request by `request_id` with full context (kind, status, submitter, any engineer→PO gap reason). |
| `accept_request` | Accept a `ProductRequest` (`submitted → accepted`). **UNIVERSAL — do this before any mutation, for BOTH `dpe-sa` and `dpe-cf`.** Optional `request_id` targets a specific request (default: latest non-rejected). Idempotent. For `dpe-sa` it also unlocks the PO source-validation gate. |
| `reject_request` | Reject a request back to the PO with a structured `category` (see `list_rejection_categories`) + `reason`. A valid pre-acceptance decision. |
| `list_rejection_categories` | Valid `category` values for `reject_request`. |
| `set_serving_mode` | Switch the product's serving mode between `virtual_view` and `dbt_materialized` (the exclusive "serving" group). Re-keys StageRun rows so completed stages keep their results. |
| `set_materialization_target` | Set / clear the per-product dbt **target** Postgres connection. Falls back to the source connection when unset. |
| `get_dbt_project` | Fetch the scaffolded dbt project (`format=zip\|json`) for a materialized product. No secrets — `profiles.yml` reads `WB_DBT_*` via `env_var()`. |
| `get_dataset_filter` | Read a dataset's row-filter — both the plain-language `filterIntent` and the compiled SQL `filterPredicate`. |
| `set_dataset_filter` | Author a dataset's row-filter: pass plain-language `filterIntent` (compiled to grounded SQL) or a raw `filterPredicate`. |
| `query_semantic_layer` | Ask a free-form natural-language question across a domain's deployed views, grounded in curated `:BusinessConcept`s. Returns a SELECT + answer (the marketplace Semantic Q&A surface). |
| `get_semantic_discovery_status` | Per-step state of a **domain's** semantic-layer discovery (scaffold / recommend / enrich): has it run, when, its stats, and whether it's **stale** (sources changed or an upstream step re-ran). Also returns concept counts + **stranded** concepts (no data-product binding). Read-only. Domain-scoped. |
| `run_semantic_discovery_step` | Run one discovery step for a domain (`scaffold` → `recommend` → `enrich`, in that order) and record it. `dry_run=true` previews scaffold counts without writing. `recommend` auto-promotes proposals ≥ `confidence_threshold` (default 0.7) into bound + parented attributes and queues the rest. Needs `role` ∈ Data Steward / Data Engineer / PO + a token scoped to the domain. |
| `reset_semantic_discovery` | Clear a domain's concept layer (soft-deprecate every concept + its data-product bindings) for a clean rebuild. Audit history survives. Needs `role` ∈ Data Steward / Data Engineer / PO. |
| `get_pending_questions` | Questions a running stage is waiting on (interactive stages like Data Discovery ask "which tables?"). Poll this after `run_stage`. |
| `answer_question` | Answer a pending question — unblocks the stage. |
| `review_description` | Approve a pending column description, or edit it (action="reject" + `corrected_text`). Clears the enrichment review. Needs `role` ∈ Data Steward / Reviewer / PO. |
| `review_mapping` | Approve / replace / escalate a pending column mapping. Needs `role` ∈ Data Engineer / Reviewer. |
| `review_domain_rule` | Approve or reject a pending domain DQ rule. Needs `role` ∈ Data Quality Analyst / PO / Reviewer. |
| `review_table_description` | Approve / reject / edit a pending TABLE description (`desc_uri` from the `datasets` card's `table_desc_uri`). Needs `role` ∈ PO / Data Steward / Reviewer. |
| `review_relationship_description` | Approve / reject / edit a pending RELATIONSHIP description (`desc_uri` from the `relationships` card). Needs `role` ∈ PO / Data Steward / Reviewer. |

### More tools you'll reach for

| Tool | What it does |
|---|---|
| `get_available_actions` | Dependency-aware menu: `runnable` now (in any order) vs `locked` (each with `blocked_by`). The flexible companion to `get_plan_summary`'s single `recommended_next` — use it when you want to run targeted work, not the whole line. |
| `list_available_workflows` / `add_workflow` / `remove_workflow` | The workflow catalog + compose it. Add DQ, discovery, etc. onto a project (e.g. add quality checks to a consumer's own composed view). |
| `get_join_preflight` | **Run before mapping a consumer that spans multiple source products.** Detects when mapped base tables split into disconnected components (the classic "two source products, no FK across them" case) and returns a `recommended_joins` bridge payload. |
| `set_dataset_joins` | Author an explicit join graph (`:DatasetTransform.joins[]`) — apply the preflight's recommended bridge, or hand-author FROM/JOIN when FK inference can't. |
| `get_mapping_graph` | The mapping **lineage** graph (source columns → transform → output columns). Use to review lineage and to render a diagram. |
| `get_mapping_rationale_report` | Downloadable markdown explaining every mapping's source, transform, author, and rationale — the mapping documentation artifact. |
| `get_stage_output` | A readable **"Step Report"** of what a stage produced (mappings, generated view SQL, deployed views, DQ rules). The human-friendly review view, distinct from `get_stage_results`' structured cards. |
| `get_okf_bundle` | Portable markdown product docs (Open Knowledge Format) for a published product — a briefing bundle for humans/agents that can't reach MCP. |
| `get_upstream_drift` | Whether a consumer's bound sources changed under it (schema/contract drift) — check before re-running a consumer that's been live. |
| `get_deployment_reflection` | The post-deploy reflection verdict (surprises, alignment checks, recommendations) once `deployment_reflection` has run. |
| `preview_serving_view` | Sample rows from the generated/deployed view — sanity-check the output shape before handing back. |
| `get_marketplace_detail` / `get_marketplace_lineage` / `list_marketplace_products` | Marketplace listing, cross-product lineage, and catalog — for "where did this land / what consumes it" and UI deep links. |
| `get_materialization_status` / `build_materialization_sample` / `approve_materialization_full` / `reject_materialization_sample` | The dbt-materialized serving **gate**: sample build → inspect → approve → full build. Only for `dbt_materialized` products. |
| `get_dq_score` / `get_dq_test_runs` / `get_osi_evaluation` / `trigger_osi_score` | Quality score, DQ test results, and AI-readiness (OSI) scoring. |
| `get_provenance` / `get_stage_executions` / `get_usage_summary` | Audit trail (PROV-O), per-stage run history, and LLM token usage. |

This isn't the whole surface — it's what you reach for most. When in doubt about
what a project needs next, call `get_available_actions` and pick a `runnable` step.

### Approving reviews & switching role
The review tools **write** (they sign off the human review gate) and are
attributed to your token principal as a human reviewer. Each requires you to
**declare the `role` you are acting as**, and the server enforces it per surface.
If you try to approve something your role can't (e.g. approving descriptions as a
Data Engineer), the tool tells you which role is required — **re-call with that
role** (e.g. `role="Data Steward"`). That is how you "switch hats" to clear
steward- or DQA-gated items from your own Claude Code. Workflow: read the queue
with `get_stage_results` (cards `descriptions` / `mappings` / `dq_rules`), then
approve/edit with the matching review tool, then confirm the stage flipped via
`get_project_state`.

All tools are scoped to the projects your token is authorized for; you'll be
denied on others.

### What the DE side CAN'T approve — column names (a PO action)
There is **no DE tool to approve column-name recommendations** (the `sports_`-style
standardized names on a source product), and that's by design — **name approval
lives in the PO source-validation gate**, alongside descriptions/tables/rules. Do
NOT go hunting for a DE/Steward name tool, and do NOT try to unlock it by
resetting `mark_discovery_complete` or re-running the naming stage (re-running only
re-computes the suggestion; it does not approve it). Instead, **direct the PO** to
approve them:

- PO's own Claude Code: `bulk_approve_source_validation(tab="names")` (or
  `review_validation_item(review_type="names", action="approve")` per column).
- Web UI: the **Source Product Validation** page → **Names** tab.

This matters most when you **re-discover a source that added columns**: the new
columns' names land at `pending_review`, and **`synthesize_odcs_from_graph` will
silently fall back to the RAW column name** (no `sports_` prefix) for any name not
yet approved. So the correct order after a re-discovery is: discovery → enrichment
→ naming → **PO approves the new names (+ descriptions) in the validation gate** →
synthesize → dprod → auto-map → serve → deploy. If you synthesize before the names
are approved, you'll get inconsistent naming and have to re-synthesize.

### Approve mappings BEFORE serving — always
After `data_mapping`, the AI-suggested mappings land at **`status='pending_review'`**.
The serving-view generator **only emits DDL from `approved` mappings** — so running
`serving_virtual_view` with pending mappings produces partial or **zero** views and
**fails** (and the reason lives in the server-side skill log, not the stage result —
you'll be tempted to go log-diving). Don't. The flow is:

1. Review the mappings with the user (`get_stage_results` card=`mappings`, or
   `get_stage_output`).
2. **When they're happy, RECORD the approval** — `bulk_approve_mappings` (one call,
   approves all pending) or `review_mapping` per row. **"Looks good" from the user
   is NOT an approval until you've called the tool.** This is the #1 cause of a
   failed serving run.
3. Optionally `get_join_preflight` (for cross-product joins), then run
   `serving_virtual_view`.

`run_stage serving_virtual_view` now refuses up front if any mappings are still
pending (it tells you to `bulk_approve_mappings` first) — so you won't waste an LLM
run, but don't rely on the guard: approve as the natural step after review.

## How a data product is built (the lifecycle)

Projects have an **archetype** that determines the stage flow:

- **`dpe-sa` (source-aligned)** — discovery-first. The engineer profiles a
  source database; the PO validates names/descriptions/rules. Rough flow:
  `data_discovery → metadata_enrichment → source_naming_recommendations →
  mark_discovery_complete → (PO validation) → product materialization`.
- **`dpe-cf` (consumer-aligned)** — contract-first. The PO shapes the product;
  the engineer binds it to upstream source products and maps columns. Rough
  flow: `product_definition → odcs_to_dprod → data_mapping →
  serving_virtual_view → mark_engineering_complete`.
- `dq` / `dd` — discovery + data-quality stacks.
- **`dmig` (data migration)** and **`cmig` (code migration)** — engineer-initiated
  platform/code moves. These are fully drivable over MCP too — see **"Other project
  types the workbench drives"** below for their tools + flow.

**Two-person gates.** Some stages flip to `awaiting_review` and are cleared by a
Product Owner or Data Steward **in the web UI** — that's by design. Your job as
the engineer is the engineering work (run discovery, fix mappings, generate the
view); the PO/steward side stays in the UI. "`awaiting_review`" means *you're
done with that stage; it's waiting on a human approver*, not that you're blocked
from the next engineering step.

**Ordering is enforced, but overridable — and archetype-aware.** Stages have a
recommended order because a later stage needs an earlier one's graph output.
`run_stage` / `complete_stage` check dependencies and will **refuse** a step whose
prerequisites aren't met (they return `blocked_by` plus a `force=true` escape
hatch), so you can't silently produce a thin/empty result. Dependencies only
count stages that actually exist in **this** project's pipeline — so a stage a
project doesn't have never blocks. In particular, **consumer-aligned (`dpe-cf`)
products do NOT require metadata enrichment before mapping**: a consumer inherits
descriptions/DQ context from the source products it CONSUMES, so `data_mapping` is
runnable straight after `odcs_to_dprod`. If you ever see mapping "blocked by
metadata enrichment" on a consumer, that's a bug — report it. Use
`get_available_actions` to see what's runnable vs locked, and `get_project_state`
for full status.

## Building the semantic layer (concept discovery)

The semantic layer (`:BusinessConcept` entities → attributes → values, bound to
data products) is built **per domain**, not per project, with three steps run in
order. It powers `query_semantic_layer` and the marketplace Semantic Q&A.

1. **`scaffold`** — deterministic. Builds the entity/attribute/value spine from
   the domain's data-product schema, binding entities to tables, attributes to
   columns, and seeding FK relationships. **Run first.** `dry_run=true` previews
   the counts. Re-running deprecates the current concepts and rebuilds.
2. **`recommend`** — LLM. Finds business concepts that span multiple products in
   the domain (+ synonyms). Proposals ≥ `confidence_threshold` (default 0.7) are
   auto-promoted — bound to evidence columns and parented under the matching
   entity; lower-confidence ones stay queued for human review in the web UI's
   Review Queue. **Run after scaffold** so it can attach to the entity spine.
3. **`enrich`** — LLM. Polishes names / definitions / synonyms in place. **Run last.**

Drive it with `get_semantic_discovery_status {domain}` (what's run, when, stale?),
then `run_semantic_discovery_step {domain, step, role}` for each step. To start
clean, `reset_semantic_discovery {domain, role}` first. After the sequence, re-check
status: a healthy layer has **0 stranded** concepts (everything bound to a data
product). Re-running an upstream step marks the downstream ones **stale** — re-run
them to stay consistent. The data-product layer is read-only throughout; only
concepts are written.

## Other project types the workbench drives

Besides the product archetypes, the same MCP surface drives two **engineer-initiated
migration** archetypes end-to-end. Recognise them by the project `archetype` in
`get_project_state`.

### Data Migration (`dmig`) — lift-and-shift a source platform to a target
A raw platform-to-platform move (e.g. Oracle → Snowflake): discover the source,
configure a target + landing strategy, generate an executable pipeline (DLT), run it,
and reconcile source↔target row counts.

1. **Discover the source** — `set_data_source` → `run_stage` discovery/profiling (as usual).
2. **`configure_migration {project_code, target_connection_id | target_platform, …}`** —
   pick the target + landing strategy (raw lift-and-shift). Creates the migration plan.
   Read state any time with **`get_migration_status`**.
3. `run_stage dmig_assess_plan` then `run_stage dmig_generate_pipeline` (LLM — assess
   keys/incremental cursors + flag lossy type casts, then emit `migration.json` + DLT
   artifacts).
4. **`run_migration_snapshot`** — assemble the package and RUN the migration
   (extract → load); completes the execute stage. **`get_migration_package`** downloads
   the runnable package as `{files}`.
5. **`run_migration_reconcile`** — verify source↔target row counts and close the reconcile stage.

**Offline / schema-only** migration (no live source): `set_intake_execution_mode`
(`schema_only`), review the physical schema (`get_physical_schema` /
`confirm_physical_schema`), `seed_migration_schema` to materialize it, then
`flip_migration_to_live` when a live source arrives.

### Code Migration (`cmig`) — convert legacy code to a target platform
Spec-first reverse → review → forward conversion, anchored to a `dmig` project for the
source→target schema. **The spec review is a HARD gate** — forward engineering is blocked
server-side until the reverse-engineered spec is approved (`force=true` does NOT bypass it).

1. **`list_eligible_dmig`** → **`link_code_migration {project_code, dmig_project_code}`**
   (binds the schema source; completes `cmig_link`).
2. **`import_code`** — upload the legacy code (`{filename, content_utf8}`); it lands in an
   immutable sandbox. **`configure_code_migration`** — pick the conversion target.
3. `run_stage cmig_reverse_engineer` (LLM) — produces the use-case spec; finishes at a
   **`code_spec` review gate**.
4. **Review the spec:** `get_code_spec` to read, `update_code_spec` to edit, then
   **`approve_code_spec`** (or `reopen_code_spec` to send it back). Forward stays blocked
   until this passes.
5. `run_stage cmig_forward_engineer` (LLM) — converts against the target using the curated
   corpora. Then **`get_code_migration_package`** for the `old/` + `new/` + conversion-report
   package. `get_code_migration_status` shows the blocker list.

## Serving beyond virtual views — lakehouse, transfer, and the package model

Virtual-view and dbt-materialized aren't the only serving modes. Every mode follows
**Configure → Build → Deploy**, and the **Build** step assembles a runnable, downloadable
package with **no live DB** — so you can hand off the artifact even before deploy.

- **Advice first** — `get_serving_advice {project_code}` recommends virtual vs materialized
  vs lakehouse/transfer from the product's shape (SCD history + cross-platform source→target
  force materialization/transfer).
- **Lakehouse** (Parquet + DuckDB) — `set_serving_mode` / `select_exclusive_group` to the
  lakehouse member → **`build_lakehouse_package`** (assemble, no DB) → **`export_lakehouse`**
  (Deploy — runs the export). Read `get_lakehouse_status`; download `get_lakehouse_package`.
- **Cross-platform transfer** (source engine → target engine) — `get_placement_advice` /
  `set_transform_placement` decide per-op ETL/ELT/hybrid placement, then **`run_transfer`**
  executes; `get_transfer_status` reads state; `get_transfer_batch_schema` is the versioned
  data-plane contract.
- **Packages & git** — `build_dbt_package` / `get_view_package` / `get_dbt_project` /
  `get_lakehouse_package` fetch the runnable package; **`push_to_git`** pushes serving
  artifacts + docs to the product's repo. **`get_transform_preflight`** checks every mapping
  expression compiles on the resolved serving platform BEFORE you run serving.
- **Post-deploy** — **`run_deployment_reflection`** generates a fresh AI reflection headlessly
  (the reflector RUN; `complete_stage` only *marks* the stage done), then
  `get_deployment_reflection` reads the verdict. `get_dq_package` / `get_dq_rules` fetch the
  DQ suite + rule breakdown.

## Multi-platform sources — connections & source binding

A project's source isn't only its legacy `pg_connection`. For Postgres / MySQL / Snowflake /
Databricks sources: `list_platforms` / `get_platform_capabilities` show what's supported;
`list_connections` / `create_connection` / `test_connection` register + verify a connection;
**`set_source_binding {project_code, connection_id}`** binds the project to it, then
`list_source_namespaces` / `list_source_tables` browse the bound source. `get_source_binding`
reads the current binding.

## Inbound intake review (migration submissions)

An external assessment tool can POST migration/modernization recommendations that get parsed
into a reviewable blueprint. The engineer reviews **migration** submissions headlessly:
`list_intake_submissions` → `get_intake_submission` → **`approve_intake_submission`** (runs the
idempotent scaffold saga that creates the `dmig` project) or `reject_intake_submission`.
`set_intake_execution_mode` toggles live vs schema-only before approving. (The PO reviews
MODERNIZATION submissions on their own `/po-mcp` surface.)

## Typical session

1. **Orient + check assignments** — `list_projects`, then `list_assignments` and
   `get_project_state {project_code}` to see the archetype + where each stage
   stands. **If the project's request is `submitted`, accept it first**
   (`accept_request {project_code}`) — or reject it (`reject_request`) — before
   any engineering work. This is universal: `dpe-sa` AND `dpe-cf`.
2. **Run the next stage** — `run_stage {project_code, stage_number, workflow_id}`.
   It returns a `run_id` and starts server-side. Keep the `run_id`.
   **If a stage needs configuration** (e.g. **Data Discovery** requires which
   tables): first call `get_stage_config_options {project_code, stage_number}`,
   show the user the available schemas/tables, get their selection, then call
   `run_stage` with `config` (e.g.
   `config={"discovery_tables": "public.orders, public.customers"}`). If you call
   `run_stage` without a required config value, it returns an error naming the
   missing key.
3. **Watch it — poll with PROGRESSIVE backoff, not a fixed timer.** Re-poll
   `get_project_state` until the stage leaves `running` — but don't burn a flat
   ~50s wait between every check (that wastes time when a stage finishes early and
   hammers when it's slow). Instead **back off geometrically**: check almost
   immediately (~3–5s), then roughly double each interval — 5s → 10s → 20s → 40s —
   and cap around 60s. Short stages (mechanical/ODCS) resolve on the first or
   second check; long LLM stages (mapping, serving) settle into the ~60s cap. This
   catches completion fast without idle waiting. Report only transitions, not every
   poll.
   **On each poll also call `get_pending_questions {run_id}`**: if it returns a
   question (e.g. Data Discovery's "which tables?"), **present its `prompt` +
   `options` to the user**, get their choice, then call `answer_question {run_id,
   question_id, value}` (for a checklist, comma-separate the chosen option values).
   That unblocks the stage. Answer promptly — questions time out
   (`timeout_seconds`, default 300s) and fall back to a default. This is how
   **interactive** stages run from Claude Code.
   If a stage flips to `failed`, read its `error_message` (now on the stage in
   `get_project_state`, and in `get_stage_executions`) — don't go log-diving.
4. **Inspect the result** — `run_cypher` to look at what landed (e.g. the
   discovered columns, the mappings, the DQ rules). Always scope to the project.
5. **Fix + re-run if needed** — `reset_stage` then `run_stage` again.
6. **Hand off** — when stages are `awaiting_review`/`complete`, the PO/steward
   approves in the web UI.

## Status cadence — keep the engineer oriented

The engineer is driving blind unless you keep them updated. Make status reporting
automatic, not on-request. Call `get_plan_summary {project_code}` and render a
**short** digest:

- **When you start working on a project** (right after you orient).
- **After every state change** — a stage finishes, a `complete_stage`, a review
  write, a workflow add/remove.
- **On each poll** while watching a `run_stage` — report transitions, not noise.

Render it tight, e.g.:

```
Customer Master — 12/14 (86%) · deployed
  ✓ done: discovery, profiling, enrichment, naming, materialize, deploy …
  ▶ running: —
  → next: Mark Engineering Complete  → complete_stage
  ⚠ blocked: —
  ↗ http://…/engineer/projects/1
```

Lead with `recommended_next` (and the `tool` to run it) so the engineer always
knows the one next move. If `recommended_next` is null, say the plan is complete.
Don't dump the raw stage list unless asked — `get_plan_summary` is the digest.

**Never answer a status/update request from memory.** Every time the user asks
for status, an update, or "what's it doing now", call the tool FRESH
(`get_plan_summary {project_code}`, plus `get_project_state` if they want
detail) — do **not** reuse a result from earlier in the conversation. Server-side
state changes between turns: a stage finishes, a review clears, a build
completes. A cached answer is wrong by default. If the fresh result differs from
what you last reported, say so plainly — e.g. _"The state has changed since my
earlier reply — here's the fresh backend status:"_ — then show the new digest.

## Wrapping up — reports, lineage, and UI links

When the engineering work is done (view generated / deployed, or the materialized
build approved), don't stop at "complete." Offer to produce the artifacts and
point the engineer to where the product now lives:

- **Reports / documentation** — the process shouldn't live only in this chat.
  Offer the real artifacts:
  - **`get_product_report` → the full deployment report (markdown) with a Mermaid
    ERD + Mermaid lineage, schema, mappings, quality, OSI, and serving. This is
    the one to lead with after a deploy** — same as the UI's "Generate Report".
  - `get_mapping_rationale_report` → markdown of every mapping's source, transform, and rationale.
  - `get_okf_bundle` → portable product docs for a published product.
  - `get_stage_output` → a readable Step Report per stage (mappings, generated view SQL, DQ rules).
  - `get_mapping_graph` → lineage you can render as a **mermaid** diagram (source → transform → output).
  - Compose these into a short deployment summary (what was built, key transforms, sources, warnings).
- **Verify the output** — `preview_serving_view` for sample rows; `get_deployment_reflection` for the post-deploy verdict.
- **UI deep links** — surface the `web_url` the tools return, and the marketplace
  views (`get_marketplace_detail` / `get_marketplace_lineage`). The engineer drives
  from here in Claude Code, but the UI is where they *see* — send them to the
  project page, the marketplace listing, and the lineage view rather than making
  them hunt for them.

## Rules

- **Relay retrieved data verbatim.** When the user asks to *see* workbench data —
  column descriptions, mappings, DQ rules, profiling stats, contract fields,
  `run_cypher` rows — present exactly what the tool returned (the real values),
  not a summary or paraphrase. They want what's in the workbench, not your gloss
  of it. Quote/tabulate the actual rows; add interpretation only after, clearly
  separated, and only if asked. (The status *digest* is the one deliberate
  exception — that you render tight.)
- **Re-fetch, don't cache.** A read tool's result is valid only for the current
  turn. Never reuse a prior result to answer a later question about current
  state — call the tool again (see "Status cadence").
- **Warn before running a non-recommended step.** If the user asks to run a
  stage that is NOT `get_plan_summary.recommended_next`, don't just run it.
  First tell them it isn't the recommended next step (name the recommended one),
  say WHY theirs is ahead of order — typically a prerequisite from the
  dependency graph isn't complete yet, so the result may be thin/empty — and ask
  them to confirm (Accept / Cancel). Only call `run_stage` / `complete_stage`
  after they accept. Running the recommended step needs no confirmation.
- **Accept before you mutate.** A `submitted` product request is an unaccepted
  assignment. Don't run/complete/reset stages, set the data source, change
  serving mode, override filters, or submit reviews until you've accepted it
  (`accept_request`) or rejected it (`reject_request`). This holds for BOTH
  `dpe-sa` and `dpe-cf`. The server enforces this (warn-only today; a hard
  block once `WB_ENFORCE_ACCEPT_GATE` is on), so a blocked mutation returns a
  `required_action: accept_request` — accept, then retry.
- **Be transparent about what you're doing.** Before running a stage or a batch
  of tool calls, say in one plain line what you're doing and why ("Running the
  mapping stage — this matches each output column to a source column…",
  "Checking the join path across the two source products before mapping…"). The
  engineer should be able to follow what's happening and why without reading raw
  tool output. Middle ground: narrate intent + report transitions; don't dump
  full logs or tool JSON, and don't go silent for a long operation.
- **Always pass the project code** the user is working on; scope `run_cypher`
  to it.
- **`run_stage` is async** — never assume it finished when it returns; poll
  `get_project_state`. A server-side stage can take minutes.
- **Interactive stages** — while polling, always also `get_pending_questions
  {run_id}`. A stage that needs a decision (which tables, which schema, a
  yes/no) will wait on a question; surface it to the user and relay their answer
  with `answer_question`. If you ignore it, the question times out to a default.
- Don't try to run the server-side pipeline skills (data-discovery, mapping,
  etc.) locally — you don't have them and don't need them. Drive via MCP.

See `reference.md` (in this skill's directory) for the full per-tool argument
detail and example Cypher.
