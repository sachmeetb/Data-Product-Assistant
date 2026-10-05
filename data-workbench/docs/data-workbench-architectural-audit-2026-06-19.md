# Data Workbench Architectural Audit

Generated: 2026-06-19

Scope: static architectural review of the Data Workbench repository, with emphasis on backend structure, implementation strategy, functional completeness, ontology/Neo4j storage, project organization, MCP services, and design-driven failure modes. I did not run the application or execute end-to-end tests for this report.

## Executive Summary

Data Workbench has a coherent core architecture: a FastAPI backend coordinates pipeline state in SQLite, stores product/catalog semantics in Neo4j, and drives data-agent stages through a shared execution path used by both the browser and MCP. The most mature parts are the ODCS-to-DPROD materialization model, stable DataContract versioning, project/workflow scaffolding, and the MCP execution surface for engineer-driven lifecycle work.

The main architectural risk is that several parts of the system are still demo/trusted-local assumptions while the product now exposes remote/client-facing capability. REST has no authentication boundary, connection secrets are stored and returned in plaintext, project isolation in Neo4j is convention-based, and some fallback paths become unscoped when a `:Project` node is missing or a graph check fails. MCP is substantially better guarded than REST, but still has notable gaps: declared roles are not identity-bound, semantic-layer queries are domain-scoped rather than project-scoped, and client parity is incomplete for product authoring, log access, and several review/workflow surfaces.

The second major risk is workflow correctness. Stage templates use durable `stage_id`, but persisted stage runs are keyed by workflow plus stage number. Dependencies are modeled but not generally enforced by the backend. That makes the workflow engine flexible, but it allows green stages with missing prerequisites, stale logs after template edits, and hard-to-diagnose failures when MCP clients run stages out of order.

The third major risk is partial migration to the stable temporal contract model. The write path for `:DataContract` and `:ContractVersion` is strong, and ODCS reads largely respect temporal edge ranges. A few summary/MCP read paths still walk `HAS_SCHEMA` and `HAS_PROPERTY` without `fromVersion`/`toVersion` filters, so dashboards and completion summaries can over-count expired schemas/properties after contract edits.

## Architecture Observed

### Repository Structure

The system is organized into clear application layers:

- `workbench/backend/`: FastAPI app, routers, SQLite models, Neo4j graph operations, MCP server, stage execution, ODCS/DPROD persistence, materialization, semantic/marketplace services.
- `workbench/frontend/`: React/Vite persona shells for Product Workbench and Engineering Workbench.
- `.claude/skills/`: versioned data-agent skills used by backend stage execution.
- `docs/`: maintained and scratch architecture/user docs, including MCP architecture, engineer guide, and subsystem notes.
- `projects/`: runtime per-project work directories.
- `docker-compose.yml`: local/deployable stack with backend, frontend, and Neo4j.

The main FastAPI app mounts MCP at `/mcp` and then includes all REST routers directly in `workbench/backend/main.py:43-112`.

### Runtime Model

Data Workbench uses three persistence/control planes:

- SQLite stores control-plane state: `Project`, `Workflow`, `StageRun`, `StageExecution`, product requests, chat sessions, and materialization targets. The core SQLModel definitions are in `workbench/backend/models.py:44-145`.
- Neo4j stores the data/product knowledge graph: DCAT catalog/dataset/column metadata, DQV/SHACL quality measures and rules, ODCS contracts, DPROD products, mappings, serving definitions, review artifacts, provenance, and cross-product consumption edges.
- Per-project filesystem directories hold generated artifacts, skill outputs, dbt projects, and transient files.

The backend uses a stage registry and workflow templates in `workbench/backend/archetypes.py`. The registry describes implemented archetypes (`dd`, `dq`, `dpe-cf`, `dpe-sa`) and placeholder archetypes (`dmod`, `dmig`) in `workbench/backend/archetypes.py:5-42`.

### Implementation Strategy

The execution strategy is generally sound:

- UI and MCP stage execution share `stage_execution.start_stage_run(...)`, which was extracted to decouple execution from WebSocket transport (`workbench/backend/stage_execution.py:1-10`).
- Stage runs persist `StageRun` status and `StageExecution` logs asynchronously, with a 2 MB log cap (`workbench/backend/stage_execution.py:29-35`, `workbench/backend/stage_execution.py:352-368`).
- MCP `run_stage` is headless: it starts server-side execution and expects clients to poll state/results.
- Non-agent stages are completed through backend handlers such as `complete_stage`; some have real side effects, including ODCS synthesis, ODCS-to-DPROD generation, publish, and materialization checks (`workbench/backend/routers/stages.py:266-345`).
- DQ generation/execution has backend-driven paths instead of relying only on LLM prompts.

There is one important duplication risk: `workbench/backend/routers/cli.py` still mirrors older stage execution logic over SSE instead of delegating to `stage_execution.start_stage_run(...)` (`workbench/backend/routers/cli.py:1-10`). This is a drift point because the new shared path contains fixes such as serving filter precompilation and backend DQ handling.

## Ontology and Neo4j Storage

### Intended Ontology Stack

The project uses a pragmatic hybrid graph vocabulary:

- DCAT-2 for catalogs, datasets, and columns.
- DQV for quality measurements and scores.
- SHACL-like `NodeShape` and `PropertyShape` nodes for quality rules.
- PROV-O-style activity/attribution edges for review and lifecycle provenance.
- ODCS DataContract concepts for contract authoring and versioning.
- DPROD concepts for deployable data products, output ports, output datasets, and product columns.
- OSI/custom score concepts for product scoring.

The architecture docs explicitly describe Neo4j as storing DCAT-2, DQV, SHACL, PROV-O, ODCS, and DPROD (`README.md:134-136`).

### Strong Points

1. Stable DataContract identity is a good design choice. `_contract_versioning.py` defines one stable `:DataContract` per logical product, sidecar `:ContractVersion` nodes, and temporal validity ranges on substructure edges (`workbench/backend/_contract_versioning.py:1-24`).

2. The helper module clearly defines how current-version and pinned-version reads should be written (`workbench/backend/_graph_helpers.py:1-18`, `workbench/backend/_graph_helpers.py:100-113`). This is the right abstraction to prevent query drift.

3. ODCS save/read logic uses the stable model. `_save_odcs_to_graph` delegates to `save_contract_head`, synchronizes substructure, syncs `CONSUMES` edges, and links contracts back to `:Project` (`workbench/backend/routers/odcs.py:662-742`).

4. DPROD generation is more careful than a simple rebuild. `_generate_dprod` snapshots prior DPROD columns, caches prior mapping/rule URIs, wipes and rebuilds DPROD, detects likely renames, reconnects unchanged mappings, reactivates rematched mappings, reconnects active mapping edges, and reconnects rules where possible (`workbench/backend/routers/odcs.py:1568-1705`).

5. `:CONSUMES` is treated as the only intended cross-project graph edge in docs (`README.md:225`), and contract consumption is temporally filtered in some summary paths (`workbench/backend/routers/summary.py:249-261`).

### Main Ontology/Graph Risks

1. Temporal versioning is not consistently applied.

   The model says substructure must be filtered by `fromVersion`/`toVersion`, but `CONTRACT_DETAIL_S` walks `dc-[:HAS_SCHEMA]->sc`, `sc-[:HAS_PROPERTY]->prop`, and `dc-[:HAS_OWNER]->o` without temporal filters (`workbench/backend/routers/summary.py:314-329`). MCP completion summary for `synthesize_odcs_from_graph` has the same issue (`workbench/backend/mcp_server.py:1511-1517`). After a contract edit removes or renames schemas/properties, these counts can include expired edges and make dashboards/reporting look correct when they are not.

2. DQ rule summaries do not cover all rule anchors.

   The scoped DQ rule count follows `Project -> Catalog -> Dataset -> NodeShape -> PropertyShape` (`workbench/backend/routers/summary.py:211-221`). DPROD-anchored rules on `:DProdColumn` can be missed even though DPROD generation and wizard flows materialize spec/domain/user rules against product columns. This can under-report product-authored rules for consumer/source product flows.

3. Project isolation in Neo4j is convention-based, not a hard tenancy boundary.

   `run_cypher` explicitly documents that project isolation is by convention on a shared Neo4j database and not true multi-tenant isolation (`workbench/backend/mcp_server.py:703-706`). Most graph queries choose scoped or unscoped variants based on `has_project_node(project)`. If the project node check fails, `has_project_node` caches `False` (`workbench/backend/graph_ops.py:207-219`), and many routers fall back to unscoped queries. That is safe for legacy single-project demos, but risky for shared deployments.

4. Project deletion can miss graph data and can proceed through some graph failures.

   `delete_project_node` primarily deletes reachable project subgraphs and then sweeps nodes whose `uri` contains `:{project_code}` (`workbench/backend/graph_ops.py:51-100`, `workbench/backend/graph_ops.py:250-260`). Nodes without `uri`, nodes not linked to `:Project`, or nodes using only `id` may survive. Separately, `check_consumers` returns `[]` on Neo4j failure (`workbench/backend/graph_ops.py:279-293`), and the delete route treats an empty list as no consumers (`workbench/backend/routers/projects.py:320-333`). A transient graph outage can therefore allow deletion to proceed without detecting dependent products.

5. DPROD is current-state materialization, while contracts are versioned.

   This is a reasonable design, but it creates a historical-query split: contract history can be read from `:ContractVersion` plus temporal contract substructure, while `:DProdDataProduct`/`:DProdColumn` represents the current rebuilt product graph. Any marketplace or lineage feature that claims to show a historical deployed product must be careful to read contract-version substructure, not current DPROD nodes.

6. There is no explicit Neo4j schema migration layer.

   The project creates only a project-code index in `graph_ops.py:22-25`. There are no central constraints for contract IDs, DPROD URIs, dataset/column URIs, mapping identity, or version sidecars. This increases the chance of duplicate logical nodes after failed/retried saves or hand-authored Cypher.

## Backend Project Organization

### Strengths

- Projects are first-class SQLite rows with project codes, archetypes, workflow JSON, owner hints, source connections, Neo4j connection info, and lifecycle timestamps (`workbench/backend/models.py:44-80`).
- New projects create `Workflow` and `StageRun` rows from enabled workflow templates (`workbench/backend/routers/projects.py:209-249`).
- Project codes avoid reusing deleted sequences and lingering directories (`workbench/backend/routers/projects.py:98-120`).
- The archetype registry cleanly marks implemented vs placeholder product lines (`workbench/backend/archetypes.py:5-42`).
- Multi-workflow support is present: projects can have multiple named workflows, repeatable workflow groups, exclusive serving choices, and a workflow catalog.

### Main Backend Risks

1. REST has no authentication or authorization boundary.

   The app includes all REST routers directly after mounting MCP (`workbench/backend/main.py:73-112`). Frontend roles are context/UI state (`workbench/frontend/src/RoleContext.tsx:11-17`) and permission tables (`workbench/frontend/src/types.ts:294-359`), not backend authorization. This is acceptable for a local demo, but not for a shared or remote service.

2. Secrets are stored and returned in plaintext.

   `Project` stores `pg_connection` and Neo4j password fields directly (`workbench/backend/models.py:44-57`). `AppSettings` stores Neo4j password (`workbench/backend/models.py:83-93`). `ProjectResponse` includes `neo4j_password` (`workbench/backend/routers/projects.py:76-89`). `/api/settings` returns `neo4j_password` on GET and PUT (`workbench/backend/routers/settings.py:32-60`). `/api/projects/{id}/data-source` returns the source DB password and comments that this is a single-tenant demo workbench (`workbench/backend/routers/projects.py:447-478`). Materialization target GET returns the target DB password (`workbench/backend/routers/materialization.py:673-698`).

3. SQLite schema evolution is unmanaged.

   `_migrate()` is a list of raw `ALTER TABLE`/`UPDATE` statements, each wrapped in `try/except Exception` with rollback and no logging (`workbench/backend/database.py:13-52`). There is no schema version table, Alembic migration history, downgrade path, or alert when a migration silently fails.

4. Stage identity is positional.

   `StageRun` stores `workflow_id` and `stage_number`, but no durable `stage_id` (`workbench/backend/models.py:113-126`). The exclusive-group switch code explicitly says `StageRun` identity is positional and must reconcile rows by resolving pre-edit `stage_id` from the enabled-list order (`workbench/backend/routers/projects.py:864-875`). This can mis-associate status/logs/results if workflow templates are edited, stage order changes, or repeatable workflows evolve.

5. Dependencies are modeled but not generally enforced.

   `DEPENDENCY_GRAPH` is defined in `archetypes.py` (`workbench/backend/archetypes.py:768-806`) and `check_stage_dependencies` exists (`workbench/backend/archetypes.py:1212-1228`). However, repository search shows it is not used by the stage execution or completion paths. `complete_stage` has a special guard only for `mark_discovery_complete` (`workbench/backend/routers/stages.py:300-324`). `start_stage_run` resolves and starts stages without checking dependency completion (`workbench/backend/stage_execution.py:115-184`). MCP clients can therefore run/complete stages out of order unless the caller follows `get_plan_summary`.

6. Some recovery/orphan logic can turn unknown state into green state.

   `_resolve_orphaned_stages` marks non-review orphaned stages complete after timeout with the comment "agent produced artifacts" (`workbench/backend/routers/stages.py:106-205`). `recover_stage` also marks failed non-review stages complete without artifact validation (`workbench/backend/routers/stages.py:441-479`). This helps demos recover from reloads, but can hide real stage failures.

7. Error handling often uses best-effort `except: pass`.

   Examples include project node creation on project create (`workbench/backend/routers/projects.py:202-207`), contract linking (`workbench/backend/routers/odcs.py:735-740`), serving artifact backstop checks (`workbench/backend/stage_execution.py:323-345`), publish server reflection (`workbench/backend/routers/odcs.py:2506-2515`), and graph deletion (`workbench/backend/routers/projects.py:354-359`). Best-effort behavior is useful for demo continuity, but it makes correctness dependent on logs that may not exist.

## MCP Service Completeness

### Current MCP Surface

The implemented MCP server has 24 tools in `workbench/backend/mcp_server.py`:

- Read: `list_projects`, `get_project_state`, `get_plan_summary`, `get_stage_results`, `run_cypher`, `get_dbt_project`, `get_dataset_filter`.
- Mechanical state changes: `reset_stage`, `set_data_source`, `set_serving_mode`, `set_dataset_filter`, `accept_request`, `set_materialization_target`, `complete_stage`.
- Agentic/interactive execution: `run_stage`, `get_stage_config_options`, `get_pending_questions`, `answer_question`, `query_semantic_layer`.
- Review writes: `review_description`, `review_mapping`, `review_domain_rule`, `review_table_description`, `review_relationship_description`.

The MCP auth model is significantly stronger than REST:

- Bearer tokens are parsed from `WB_MCP_TOKENS` with optional principal and project scopes (`workbench/backend/mcp_server.py:56-82`).
- MCP fails closed when no token is configured unless `WB_MCP_ALLOW_INSECURE=1` is set (`workbench/backend/mcp_server.py:204-232`).
- Project-scoped tools call `_deny_project`, which checks serving mode and token project access (`workbench/backend/mcp_server.py:179-192`).
- `run_cypher` uses Neo4j read transactions, rejects write clauses, requires the project code in the stripped query text, and caps returned rows (`workbench/backend/mcp_server.py:691-759`).

### MCP Gaps and Risks

1. MCP documentation is stale.

   `docs/mcp-architecture.md` says there are 22 tools (`docs/mcp-architecture.md:27`, `docs/mcp-architecture.md:77`). `docs/engineer-guide.md` says 18 tools. The code currently exposes 24. This matters because MCP clients rely on current tool affordances and correct client instructions.

2. MCP role gating is declared-role based, not identity-authorized.

   Review tools require a `role`, and `_role_guard` checks whether that role can approve the review surface (`workbench/backend/mcp_server.py:145-164`). The token principal is used for provenance, but token scopes do not bind a principal to allowed roles. Any holder of a project-scoped token can declare `Data Product Owner`, `Data Steward`, or `Reviewer` where the review surface allows it.

3. `query_semantic_layer` is not project-scoped.

   It only calls `_serving_guard`, validates `domain`/`question`, loads global app settings, and calls marketplace chat by domain (`workbench/backend/mcp_server.py:1625-1656`). A token scoped to one project can query any marketplace domain if it knows the domain slug. This is the clearest MCP authorization gap.

4. `run_cypher` scoping is useful but not sufficient for hard isolation.

   The query must reference the project code, but that is a textual convention, not a query rewriter. A query can reference the project code and still read unrelated unscoped nodes in the same read transaction. The code acknowledges this limitation (`workbench/backend/mcp_server.py:703-706`).

5. MCP is strong for engineer control-plane work but incomplete for full product-workbench parity.

   Missing or partial surfaces include: creating/submitting/editing product wizard specs end to end, ingesting existing ODCS/product specs through MCP, marketplace publish/deploy governance beyond lifecycle completion, source-candidate/gap-resolution workflows, stale mapping rebind UX, full transformation editor parity, and product owner validation flows at the same richness as the browser.

6. MCP does not expose execution transcripts/logs.

   REST exposes stage execution summaries and event logs through `stage_executions.py` (`workbench/backend/routers/stage_executions.py:35-70`), but MCP has no equivalent stage-log tool. MCP clients can inspect status/results but not debug the actual event stream unless they also call REST.

7. MCP completion can report success from side-effectful stages without hard artifact contracts.

   `complete_stage` returns best-effort produced counts using `_COMPLETION_SUMMARY_QUERIES` (`workbench/backend/mcp_server.py:1509-1545`), but those count queries are not a substitute for per-stage artifact validation. One of them also misses temporal filters.

## Functional Completeness

### Implemented and Relatively Complete

- Data discovery and profiling flows for Postgres-backed source systems.
- Data quality baseline: profiling, rule generation, DQ test generation/execution, score computation, remediation planning pieces.
- Source-aligned data product engineering (`dpe-sa`): discovery-first workflow, PO validation gate, ODCS synthesis, DPROD materialization, auto-mapping, serving.
- Consumer/contract-first data product engineering (`dpe-cf`): product wizard, source product binding through `CONSUMES`, mapping, serving/materialization.
- Marketplace browsing, product detail, semantic Q&A, and product scoring.
- Materialization into dbt/Postgres with sample/full build gate and materialization target configuration.
- MCP-driven engineer lifecycle for many project stages.

### Partial or Placeholder

- `dmod` and `dmig` are explicit placeholders (`workbench/backend/archetypes.py:30-41`, `README.md:222-223`).
- Multi-platform source support is not implemented. UI lists Snowflake and Databricks as disabled (`workbench/frontend/src/components/DataSourceDialog.tsx:36-40`), and backend rejects non-Postgres data sources (`workbench/backend/routers/projects.py:483-496`).
- Materialization targets are Postgres-only (`workbench/backend/routers/materialization.py:701-716`).
- Serving DDL generation supports multiple dialect labels (`docs/architecture.md:503`), but source discovery and materialization are still mostly Postgres.
- Browser product authoring and MCP engineer operation are not functionally equivalent.
- Documentation contains multiple stale statements: skills in `~/.claude/skills` versus repo `.claude/skills`, browser-only review claims despite MCP review tools, and MCP tool-count drift (`README.md:31-32`, `README.md:210`, `docs/mcp-architecture.md:77`).

## High-Priority Findings

| Severity | Finding | Why It Matters |
|---|---|---|
| Critical | REST has no backend auth/authorization and returns plaintext secrets. | Any network-exposed backend can leak DB credentials and mutate projects outside persona rules. |
| Critical | Neo4j project isolation is convention/fallback based. | Shared Neo4j deployments can cross-contaminate results when project nodes are missing, checks fail, or clients use broad read queries. |
| High | Stage dependencies are advisory, not enforced. | MCP/UI clients can run stages out of order and produce green but incomplete pipelines. |
| High | `StageRun` identity is positional. | Workflow edits can mis-associate status, logs, and results with the wrong logical stage. |
| High | Temporal contract model is not consistently applied in summaries/completion counts. | Dashboards and MCP produced counts can show expired schemas/properties as current. |
| High | MCP semantic layer is not project-scoped. | Project-scoped tokens can query marketplace domains outside their project scope. |
| High | Deletion consumer checks fail open on Neo4j errors. | A source product can be deleted while consumers exist if the graph check fails. |
| Medium | SQLite migrations silently swallow failures. | Schema drift can remain hidden until a later endpoint fails or corrupts state. |
| Medium | DQ summary only covers catalog-anchored rules. | Product-authored/spec/domain rules can be under-counted. |
| Medium | Old CLI SSE execution duplicates shared stage execution. | Fixes in the shared UI/MCP path can drift away from CLI behavior. |
| Medium | MCP lacks transcript/log and full product authoring parity. | Remote clients cannot fully diagnose or complete some workflows without REST/browser fallback. |
| Medium | Docs are stale in several high-use areas. | Engineers and MCP clients can follow incorrect operational instructions. |

## Failure Modes to Watch

1. A project is created while Neo4j is unavailable. `ensure_project_node` is swallowed, `has_project_node` may later cache `False`, and summary/review queries can use unscoped legacy variants.

2. An MCP client follows a bad plan or old docs and runs `serving_virtual_view` before mappings are complete. The backend does not generally enforce dependency prerequisites, so success/failure depends on stage-specific generators.

3. A contract removes or renames a schema/property. The stable temporal graph stores that correctly, but unfiltered summary/completion queries can still count the old schema/property.

4. A stage process dies after partial writes. Orphan/recover logic can mark it complete without proving artifacts exist.

5. A source product with consumers is deleted during a Neo4j outage. `check_consumers` returns `[]`, so the route may proceed unless another error blocks deletion.

6. A project-scoped MCP token calls `query_semantic_layer` for an unrelated domain. The tool checks global serving mode, not project authorization.

7. Workflow order changes or exclusive groups are switched repeatedly. Positional `StageRun` reconciliation can preserve status/logs for the wrong logical stage if a mapping edge case is missed.

8. A SQLite migration fails on startup. The exception is swallowed, and the service keeps running with an unexpected schema.

9. DPROD rebuild succeeds but historical marketplace/detail pages mix current DPROD nodes with versioned contract state. Historical views can drift unless every query explicitly chooses the correct source of truth.

10. An MCP client needs to debug a failed stage. It can poll state and results, but cannot fetch the stored execution event log through MCP.

## Top 10 Vetted Feature and Platform Improvements

I vetted these recommendations against four criteria:

- Risk reduction: does it remove a current correctness/security failure mode?
- User/client value: does it make browser and MCP workflows more complete or reliable?
- Architectural leverage: does it create a foundation for multiple future features?
- Sequencing: is it a prerequisite for safe multi-user/remote deployment?

I deliberately demoted lower-leverage ideas such as dashboard polish, adding more charts, new archetypes before hardening the current ones, extra ontology labels without constraints, and additional SQL dialect labels before source/materialization connectors exist.

| Rank | Recommendation | Why This Made the Top 10 |
|---:|---|---|
| 1 | Add real backend authentication and authorization for REST, WebSocket, and MCP, with role claims bound to principals. | This is the prerequisite for any shared deployment. It closes the largest security gap and makes persona gates enforceable outside the browser. |
| 2 | Replace plaintext connection storage/returns with a secret store or encrypted credentials plus redacted API responses. | Source DB, Neo4j, and materialization passwords currently flow through SQLite and REST responses. This must be fixed before remote use. |
| 3 | Persist durable `stage_id` on `StageRun` and `StageExecution`, then migrate execution/completion APIs to stage identity instead of position. | This removes an entire class of workflow-edit bugs and simplifies exclusive-group/repeatable workflow logic. |
| 4 | Enforce dependency checks in backend `run_stage` and `complete_stage`, with an explicit admin/engineer override that is audited. | The dependency graph already exists. Enforcing it turns advisory UI guidance into actual workflow correctness. |
| 5 | Finish temporal-query migration and add tests/helpers that require `fromVersion`/`toVersion` filters for contract substructure reads. | The stable contract model is valuable only if every read path respects it. This directly fixes wrong dashboard/MCP counts. |
| 6 | Add Neo4j schema management: constraints, indexes, versioned graph migrations, and an integrity checker. | Constraints on IDs/URIs/version sidecars plus migration history will prevent duplicate/corrupt graph state and make deploys repeatable. |
| 7 | Expand MCP parity: product spec create/edit/ingest, stage transcript logs, stale mapping rebind, source-candidate workflows, product publish/deploy governance, and current generated tool docs. | MCP is already central to the architecture; these close the biggest gaps for non-browser clients. |
| 8 | Harden stage execution with per-stage artifact contracts, durable job state, cancellation/retry semantics, and no auto-complete without evidence. | Stage state should reflect produced artifacts, not only process lifecycle. This improves trust in green pipeline states. |
| 9 | Make project isolation a hard boundary: per-project Neo4j database/RBAC or query rewriting with mandatory project anchors, and project-scoped semantic-layer authorization. | This makes remote/multi-tenant use credible and directly fixes the `query_semantic_layer` authorization gap. |
| 10 | Productize multi-platform support end to end: source connectors, profiling/discovery skills, materialization targets, dialect tests, and UI/MCP configuration for each platform. | The UI/docs already suggest multi-platform direction, but implementation is Postgres-first. This turns an implied feature into a reliable one. |

### Challenged Candidates Not in the Top 10

- New `dmod`/`dmig` features: valuable, but should wait until project isolation, secrets, stage identity, and migrations are hardened.
- More ontology classes/properties: useful only after constraints, temporal query consistency, and integrity checks are in place.
- More marketplace visualizations: lower priority than making marketplace/semantic access project-authorized and version-correct.
- More LLM prompt tuning: beneficial, but backend artifact contracts and dependency enforcement will prevent more failure modes.
- More SQL dialect labels: lower priority than actual connector/materialization support and dialect test fixtures.

## Recommended Near-Term Remediation Plan

1. Security baseline:
   - Add auth middleware/dependencies to REST and WebSocket.
   - Redact `neo4j_password`, source DB passwords, and materialization target passwords from all GET/list responses.
   - Bind roles to authenticated principals instead of trusting frontend context or MCP-declared role.

2. Workflow correctness:
   - Add `stage_id` to `StageRun` and `StageExecution`.
   - Enforce `DEPENDENCY_GRAPH` in `start_stage_run` and `complete_stage`.
   - Add per-stage artifact checks before setting `complete`/`awaiting_review`.

3. Graph correctness:
   - Replace all direct contract substructure reads with helper-composed temporal filters.
   - Add Neo4j constraints/indexes for key node identities.
   - Build a graph integrity endpoint/report: orphan nodes, missing project links, duplicate URIs, expired edges counted as current, DPROD/current contract mismatch, stale mapping state.

4. MCP hardening:
   - Add MCP `get_stage_execution_log` or equivalent.
   - Project-scope `query_semantic_layer`.
   - Generate MCP docs/tool inventory from `mcp_server.py` to avoid tool-count drift.
   - Add parity tests for a full `dpe-sa` and `dpe-cf` lifecycle through MCP.

5. Operational maturity:
   - Replace raw SQLite migrations with Alembic or a versioned migration runner.
   - Consolidate duplicate execution paths by routing CLI SSE through `stage_execution.start_stage_run`.
   - Update docs to reflect repo-local skills, actual SDK packages, actual MCP tools, and current review capabilities.

## Bottom Line

Data Workbench is architecturally promising and already contains several advanced pieces: a real ODCS/DPROD graph model, temporal contract versioning, product-source lineage, review provenance, and a substantial MCP control plane. The system is not yet hardened for shared/remote multi-user operation. The highest-leverage work is to make trust boundaries real, make workflow state durable by logical identity, enforce dependencies/artifact contracts in the backend, and finish the temporal graph migration across all read paths.
