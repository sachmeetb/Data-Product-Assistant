---
description: Show a workbench project's pipeline status (stages + statuses, recommended next, web link)
argument-hint: <project-code>
allowed-tools: mcp__workbench__list_projects, mcp__workbench__get_project_state
---

The user wants the current pipeline **status** of a Data Workbench project.

Project code (may be empty): `$ARGUMENTS`

Do this:

1. If no project code was given, call `mcp__workbench__list_projects` and ask the
   user which one (show code + name + archetype). Otherwise use the given code.
2. Call `mcp__workbench__get_project_state` with the project code.
3. Present a **concise status**, not a data dump:
   - One header line: `<name> (<project_code>) · <archetype> · <domain>`.
   - The stages **grouped by workflow**, each as `<stage_name> — <status>`. Use
     clear status glyphs (✅ complete · 🟡 awaiting_review · ▶️ running · ⚪ pending · ❌ failed).
   - Call out the **recommended next step** = the first `pending`/`failed` stage
     whose earlier stages are all complete/awaiting_review. Note explicitly that
     the engineer may run steps in **any order** and re-run completed ones — the
     recommendation is guidance, not a gate.
   - If any stage is `failed`, surface its name prominently.
4. End with the **web link** from the response's `web_url` so the user can open
   the dashboard, e.g. `Open in the web UI: <web_url>`.

Keep it short and scannable. Do not run any stage or mutate anything — this is read-only.
