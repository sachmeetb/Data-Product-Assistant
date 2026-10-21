# workbench-guide — tool reference

Detailed arguments + examples for the `workbench` MCP tools the kit drives.
Loaded on demand; keep the brief playbook in SKILL.md.

> The server exposes **142** MCP tools. This reference details the ones you reach
> for most; the migration (`dmig` / `cmig`), lakehouse/transfer serving,
> connections/source-binding, and inbound-intake tools are covered **by flow** in
> SKILL.md's "Other project types" and "Serving beyond virtual views" sections. The
> **canonical, always-current per-tool reference for the full surface** is
> **`docs/mcp-architecture.md`** in the workbench repo. Document new tools here, but
> never let this file contradict the canonical reference.

## `list_projects()`
No arguments. Returns the projects your token may access as an envelope:
`{"projects": [{project_code, name, archetype, domain, owner_email, multi_workflow}], "count": N}`
(was a bare list before the 2026-07 expansion — clients that indexed the raw
list must read `.projects`). A project-scoped token returns only its permitted projects.

## `get_project_state(project_code)`
Returns `{project_code, name, archetype, domain, discovery_complete_at,
brief, web_url, stages:[{workflow_id, stage_number, stage_id, stage_name,
status, execution_kind}]}`.
`status` ∈ `pending | running | awaiting_review | complete | failed`.
`execution_kind` tells you which tool drives the stage: `llm`/`backend` →
`run_stage`, `mechanical` → `complete_stage`, `review_gate` → `review_*` / UI.
No probe-then-fallback — read it and call the right tool.
For multi-workflow projects, stages are grouped by `workflow_id` — pass that
`workflow_id` to `run_stage` / `reset_stage`.
- `brief` = the Product Owner's intent (lives in the workbench DB, not the
  graph): `{product_idea, owner_email, owner_name, latest_request:{kind, status,
  submitted_by, submitted_at, notes, gap_reason, gap_column_uri}}`. Read this to
  understand *why* the product exists before running anything.
- `web_url` = deep link to the project's web dashboard — hand it to the user
  so they can open the same state in the browser.
- Statuses match the web UI exactly: this call runs the same orphaned-`running`
  resolution the dashboard does, so a stage stranded after a restart reads the
  same here as there.

Two slash commands wrap this for quick use: **`/workbench-status <code>`**
(pipeline status + recommended next + link) and **`/workbench-overview <code>`**
(brief + status + headline graph counts + link).

## `run_stage(project_code, stage_number, workflow_id=None)`
Starts the stage **server-side** and returns immediately:
`{ok, run_id, status:"started", note}`. Then poll `get_project_state`.
- Returns `{error: ...}` if the stage isn't runnable here — e.g. a mechanical
  lifecycle stage that completes via `complete_stage` / the UI's Complete action,
  or a bad stage number.
- Stages needing config (e.g. Data Discovery) → fetch options first (below) and
  pass `config`. Stages that ask mid-run → see the question tools below.

## `set_data_source(project_code, host, database, username, password, port=5432, schema_name=None, platform="postgres")`
Configure the project's **source database connection** — the MCP equivalent of
the web UI's "Select Data Source". **Do this before Data Discovery**: discovery
enumerates schemas/tables from this connection. Also completes the
`select_data_source` stage. Postgres only. From inside the workbench container a
source DB on the host is reachable as host **`host.docker.internal`** (not
`localhost`). Returns `{ok, host, port, database, schema, select_data_source_completed}`.
After setting, call `get_stage_config_options` for the discovery stage to list
schemas/tables (or get a clear connection error).

## `get_stage_config_options(project_code, stage_number, workflow_id=None)`
Returns the config choices a stage needs before running. For Data Discovery:
`{discovery_schemas:[{value,label}], discovery_tables:[{value,label}]}` (the
table `value`s are `schema.table`). Pick from these and pass to `run_stage`'s
`config` (e.g. `config={"discovery_tables": "products_sales.customer, products_sales.order_item"}`).
- `{needs_data_source: true, error: ...}` → no source DB set yet; call
  `set_data_source` first.
- `{error: ...}` (with no schemas) → the connection is set but unreachable / bad
  creds; surface the reason to the user, don't treat it as "no tables".
- `{}` → the stage needs no config.

## `get_pending_questions(run_id)`
Returns `{run_id, project_code, questions:[{question_id, message_type, prompt,
context, options, default_value, timeout_seconds}]}`. Poll it alongside
`get_project_state` after `run_stage`. `questions` is empty while the stage isn't
waiting. `message_type` ∈ `free_text | yes_no | multiple_choice | checklist |
notification`. Each `option` is `{value, label, description?}`.

## `answer_question(run_id, question_id, value)`
Unblocks the stage with the user's answer. `value` formatting by `message_type`:
free text → the text; `yes_no` → `"yes"`/`"no"`; `multiple_choice` → one
option `value`; `checklist` → comma-separated option values
(e.g. `"public.orders, public.customers"`). Returns `{ok, answered}` or an error
if the question already resolved/timed out.

## `reset_stage(project_code, stage_number, workflow_id=None)`
Sets the stage back to `pending` (same as the UI's Reset). Returns
`{ok, status:"reset"}`. Use before re-running a stage.

## `complete_stage(project_code, stage_number, workflow_id=None)`
Completes a mechanical **lifecycle** stage (no data agent runs) — the MCP equivalent of the UI's
"Complete" button (Mark Discovery Complete, Mark Engineering Complete,
ODCS→dprod, Publish, Synthesize ODCS, Auto-Map). Fires the same backend
side-effects as the UI (e.g. stamps `discovery_complete_at`); poll
`get_project_state` to see the flip. Companion to `run_stage`/`reset_stage`;
no `role` needed (mechanical transition, project-authz only). Refuses with
guidance for data-agent stages (→ `run_stage`), DQ stages (→ `run_stage`), and review
gates (→ `review_*` / UI). For stages with countable output it returns a
`produced` summary (e.g. `synthesize_odcs_from_graph` → `{schemas, properties}`,
`odcs_to_dprod` → `{data_products, output_datasets, product_columns}`,
`auto_mapping_sa` → `{mappings}`, `deploy_virtual_view` → `{deployment_status}`)
so you can confirm it produced something. Stages that execute real backend work
(odcs_to_dprod, auto_mapping_sa, **deploy_virtual_view**, publish) actually run it
here and FAIL (not silently complete) if it errors — e.g. deploy returns an error
if there's no source connection or the DDL won't execute.

## `accept_request(project_code, engineer="Data Engineer")`
Accept the project's latest **submitted** product request (`submitted →
accepted`) — the MCP equivalent of the engineer's "Accept" on the Incoming
queue. Unlocks the dpe-sa PO validation gate and clears the dpe-cf `integration`
block (those stages stay locked until the request is accepted). Returns the
updated request status.

## `set_serving_mode(project_code, mode, workflow_id=None)`
Choose how the product is served: `mode="virtual"` (a SQL view) or
`"materialized"` (physical tables built by dbt) — the MCP equivalent of the
Virtual | Materialized toggle. Swaps the serving exclusive-group member, so
completed stages keep their results, the deselected member's StageRun is dropped,
and the newly-selected member gets a fresh `pending` row. Re-fetch
`get_project_state` afterward to see the new stage list.

## `set_materialization_target(project_code, host, database, username, password, port=5432, ...)`
Set the per-product Postgres connection that **dbt materializes into** (defaults
to the source connection when unset). Only relevant on the materialized serving
path. Postgres only; from inside the container use `host.docker.internal` for a
host DB.

## `get_dbt_project(project_code)`
Pull a materialized product's generated dbt project as a
`{files: {relative_path: content}}` map — `dbt_project.yml`, `profiles.yml`,
`macros/`, `models/*.sql` (+ `schema.yml`), `snapshots/*.sql` — **excluding**
`target/` and `logs/`. Write each file to a local directory to run/maintain it
in your own dbt / CI. Read-only.

## `get_dataset_filter(project_code)`
Read the dataset-level row filter(s) on a product — the MCP equivalent of the
engineer's Filter Review panel on the serving card. For each output dataset:
the PO's plain-language `filter_intent`, the compiled SQL `predicate` the view
uses, an `output_dataset_uri` to pass to `set_dataset_filter`, and a `valid`
flag + `message` (the same prose/SQL safety check run at deploy). A predicate
that reads like plain language shows `valid=false` — finalize it before serving.

## `set_dataset_filter(project_code, output_dataset_uri, filter)`
Finalize an output dataset's row filter — engineer-owned SQL. `filter` is a SQL
boolean expression with **no** `WHERE` keyword (e.g.
`employment_status = 'active'`). `output_dataset_uri` comes from
`get_dataset_filter`. Runs the SAME safety gate as the UI: a plain-language /
malformed predicate is **refused** with a clear message and nothing is written.
After a successful set, re-run the serving stage so the view picks it up.

## `query_semantic_layer(domain, question, retrieval_mode="concept_guided")`
Ask the marketplace semantic layer a natural-language question and get a
**markdown answer**. Runs the Concept-Guided pipeline — resolves the question to
business entities/attributes/values (incl. shared references like Country),
authors + executes SQL against the domain's deployed views, and returns
synthesized text + a result table + the SQL. Read-only; **domain-scoped** (no
`project_code` — it spans the domain's deployed products).

## `get_stage_results(project_code, card, table=None, column=None, severity=None, source=None)`
**Prefer this over hand-written Cypher** for reviewing stage output. Runs the
same vetted query the web dashboard uses and returns `{card, count, rows}` with
review `status` on each row — no schema guessing, no cross-join risk. `card` ∈
`datasets · columns · descriptions · final_descriptions · relationships ·
mappings · serving · profiling · dq_rules · allowed_values · data_products ·
inputs · contract · playbook · learning_history`. Map the common asks:

| Question | `card` |
|---|---|
| review metadata enrichment | `descriptions` (+ `datasets` for table descriptions) |
| approved/final descriptions | `final_descriptions` |
| review the materialized/synthesized contract | `contract` (header + schema/property/owner counts + whether it materialised to dprod) |
| profiling stats | `profiling` |
| column mappings / lineage | `mappings` |
| DQ rules (filter `severity` / `source`) | `dq_rules` |
| discovered schema | `datasets` / `columns` |
| product columns (after dprod materialization) | `data_products` |
| consumed source products | `inputs` |

Note: `mappings` / `serving` / `data_products` are empty until materialization
actually produces them (check the `contract` card's `materialised_products` —
if 0, odcs_to_dprod hasn't generated the dprod nodes yet).

Pass `table="<name>"` (and optionally `column=`) to scope to a single dataset —
the clean way to answer "review the enrichment for the orders table" in one call.

Rows carry the identifiers the **review-write** tools need, so read→approve
composes without extra Cypher: `descriptions`/`final_descriptions` → `desc_uri`
+ `col_uri`; `mappings` → `mapping_uri`; `dq_rules` → `rule_uri`;
`datasets` → `table_desc_uri` (→ review_table_description); `relationships` →
`desc_uri` (→ review_relationship_description).

## Review-write tools (mutating; require a declared `role`)
These sign off the human review gate and are attributed to your token principal
as a human reviewer. They wrap the same handlers the web UI POSTs to, so the
PROV-O audit and auto stage-completion are identical. Read the queue first with
`get_stage_results`.

- `review_description(project_code, action, desc_uri, role, corrected_text?, col_uri?, category?, detail?, quality?)`
  — `action="approve"` accepts the text; `action="reject"` + `corrected_text`
  (+`col_uri`) replaces and approves it. `role` ∈ Data Steward / Reviewer / PO.
- `review_mapping(project_code, action, mapping_uri, role, quality?, category?, source_col_uris?, remap_source_uri?, transform_kind?, transform_expression?, transform_inputs?, transform_params?, transform_decorators?, escalation_reason?)`
  — `action` ∈ approve / replace_mapping (needs `category`) / escalate_to_steward.
  `role` ∈ Data Engineer / Reviewer.
- `review_domain_rule(project_code, action, rule_uri, role, category?, detail?, quality?)`
  — approve / reject. `role` ∈ Data Quality Analyst / PO / Reviewer.
- `review_table_description(project_code, action, desc_uri, role, new_text?, relationship_kind?, category?, detail?)`
  — approve / reject / edit a TABLE description. `desc_uri` = the `datasets`
  card's `table_desc_uri`. `role` ∈ PO / Data Steward / Reviewer.
- `review_relationship_description(project_code, action, desc_uri, role, new_text?, relationship_nature?, category?, detail?)`
  — approve / reject / edit a RELATIONSHIP description. `desc_uri` = the
  `relationships` card's `desc_uri`. `role` ∈ PO / Data Steward / Reviewer.

Read→write round-trips (read card → write tool, all via `desc_uri`):
`descriptions` → `review_description`; `datasets.table_desc_uri` →
`review_table_description`; `relationships` → `review_relationship_description`.

**Role switching:** if the declared `role` can't act on a surface, the tool
returns the required roles — re-call with one of them (e.g. switch from
`Data Engineer` to `Data Steward` to approve descriptions). `quality`:
1=Acceptable / 2=Good / 3=Excellent.

## `run_cypher(project_code, query)`
For anything `get_stage_results` doesn't cover. Read-only, project-scoped Cypher.
The query text MUST reference the project code (cross-project isolation), write
clauses are rejected, results cap at 200 rows. Useful starting points (substitute
the real code):

```cypher
// catalogs + datasets discovered
MATCH (:Project {projectCode:'dpe-06032026-01'})-[:HAS_CATALOG]->(c:Catalog)
      -[:DCAT_DATASET]->(d:Dataset)
RETURN c.name AS catalog, d.name AS dataset

// columns + their approved descriptions
MATCH (:Project {projectCode:'dpe-06032026-01'})-[:HAS_CATALOG]->(:Catalog)
      -[:DCAT_DATASET]->(d:Dataset)-[:HAS_COLUMN]->(col:Column)
OPTIONAL MATCH (col)-[:HAS_DESCRIPTION]->(cd:ColumnDescription {isCurrent:true})
RETURN d.name AS table, col.name AS column, cd.text AS description LIMIT 100

// column mappings for the product
MATCH (cm:ColumnMapping {isCurrent:true})-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
WHERE pc.uri STARTS WITH 'dprod:col:dpe-06032026-01-contract:'
RETURN pc.name AS product_column, cm.status AS status, cm.transformKind AS transform
LIMIT 100
```

## Graph schema & safe traversals (read before authoring run_cypher)

Project-scoped entry: `(:Project {projectCode:'<code>'})-[:HAS_CATALOG]->(:Catalog)-[:DCAT_DATASET]->(:Dataset)`.

Core operational nodes + edges (direction matters):

| From | Edge | To | Notes |
|---|---|---|---|
| `Dataset` | `-[:HAS_COLUMN]->` | `Column` | **direct** edge — always use this, never a variable-length path |
| `Column` | `-[:HAS_DESCRIPTION]->` | `ColumnDescription` | text on `.text`, review state on `.status`, current row `.isCurrent=true` |
| `Dataset` | `-[:HAS_TABLE_DESCRIPTION]->` | `TableDescription` | `.text` + `.relationshipKind` |
| `Dataset` | `-[:HAS_RELATIONSHIP_DESCRIPTION]->` | `RelationshipDescription` | FK/join narrative |
| `Column` | `-[:HAS_QUALITY_MEASUREMENT]->` | `QualityMeasurement` / `TopValue` | profiling stats |
| `Column` / `DProdColumn` | `<-[:MAPS_SOURCE_COLUMN]-` | `ColumnMapping` | mapping lineage; `.status`, `.isCurrent` |
| `ColumnMapping` | `-[:MAPS_TO_PRODUCT_COLUMN]->` | `DProdColumn` | product side |
| `Column` / `DProdColumn` | `<-[:appliesTo*]-`(varies) | `PropertyShape` | DQ rules; `.ruleSource`, `.status` |

`status` lifecycle: `pending_review → approved / rejected`; current rows carry `isCurrent=true`.

**Cross-join trap — important:** do NOT use a variable-length path
(e.g. `(d1:Dataset)-[*1..2]-(col:Column)`) to reach a dataset's columns. It will
match columns of *other* datasets too and silently CROSS-JOIN them, returning
wrong-looking rows. Always anchor with the **direct** `(:Dataset)-[:HAS_COLUMN]->(:Column)`
edge, and filter by `ds.name`. Better still: use `get_stage_results` (it already
does this correctly).

## Notes
- The server is the trust boundary: scoping, read-only enforcement, and
  per-project authorization are all enforced server-side regardless of what you
  send.
- Everything you do is attributed to your token's principal.
