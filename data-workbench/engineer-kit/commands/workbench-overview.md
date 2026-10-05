---
description: Full overview of a workbench project — PO intent/brief, pipeline status, graph headline counts, web link
argument-hint: <project-code>
allowed-tools: mcp__workbench__list_projects, mcp__workbench__get_project_state, mcp__workbench__get_stage_results, mcp__workbench__run_cypher
---

The user wants a **full overview** of a Data Workbench project — the equivalent
of the web UI's project page: what it's for (the Product Owner's intent), where
the pipeline stands, and headline numbers from the knowledge graph.

Project code (may be empty): `$ARGUMENTS`

Do this:

1. If no project code was given, call `mcp__workbench__list_projects` and ask
   which one. Otherwise use the given code.
2. Call `mcp__workbench__get_project_state`. From the response present:
   - **Why this product exists** — the `brief`: `product_idea`, owner, and the
     `latest_request` (kind / status / notes / gap_reason). This is the PO intent
     that lives in the workbench DB, not the graph — lead with it.
   - **Pipeline** — stages grouped by workflow with statuses (as in
     `/workbench-status`), and the recommended next step.
3. Add **headline counts** using `mcp__workbench__get_stage_results` (preferred —
   vetted queries, review status, no cross-join risk). Use the `count` it returns;
   don't dump rows. Pick cards by archetype:
   - source-aligned (`dpe-sa`) / discovery (`dd`): `datasets`, `columns`,
     `descriptions` (note how many are `approved` vs `pending_review`), `dq_rules`.
   - consumer-aligned (`dpe-cf`): `inputs` (consumed source products),
     `data_products` (product columns), `mappings` (approved / pending).
   Only fall back to `mcp__workbench__run_cypher` for something no card covers.
4. End with `Open in the web UI: <web_url>` (from the response).

Read-only — do not run stages or mutate the graph. Keep the overview tight:
brief first, then status, then a few graph numbers, then the link.
