---
description: Status of one of your data products — what it is, where it stands, and what's next for you
argument-hint: <product-code>
allowed-tools: mcp__workbench-po__list_my_products, mcp__workbench-po__get_product_status, mcp__workbench-po__get_product_spec, mcp__workbench-po__get_stage_output
---

The user wants the current status of one of their data products, in plain terms.

Product code (may be empty): `$ARGUMENTS`

Do this:

1. If no product code was given, call `mcp__workbench-po__list_my_products` and
   ask which one (show name + status). Otherwise use the given code.
2. Call `mcp__workbench-po__get_product_status`. Present a **concise** picture:
   - Header: `<name> (<code>) · <domain> · <status>`.
   - **What it is** — one line on its purpose (from the spec /
     `get_product_spec` if helpful): the columns it exposes and, briefly, which
     are transformed vs passthrough, and the sources it consumes.
   - **Where it stands** — the lifecycle stage in plain language (drafting /
     submitted to engineering / being built / awaiting your review / live).
   - **What's next for you** — the single most useful action the owner can take
     now (approve a validation, resolve a pushback, deploy, or nothing-just-wait).
3. If a stage has finished and there's engineering output worth seeing, offer to
   summarize it via `mcp__workbench-po__get_stage_output` (a readable report) —
   don't dump raw output unless asked.
4. End with the web link from the response's `web_url` (product detail /
   marketplace).

Read-only — do not submit, approve, or deploy here; just report and offer the
next step. Business language, no tool names.
