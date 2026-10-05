---
name: skill-reflector
description: Analyzes persisted pipeline stage transcripts from the Data Workbench (workbench.db StageExecution rows) and proposes evidence-backed revisions to a skill's SKILL.md, the stage prompt_template in archetypes.py, or the anti-exploration system prompt in sdk_runner.py. Use this skill when the user wants to "reflect on" a skill's agent behavior, reduce wandering, tighten prompts, or understand why a skill is expensive / slow / error-prone. Also trigger on phrases like "reflect on skill X", "improve the X skill", "why does X wander", "tune the prompt for stage Y", or "analyze stage transcripts". Requires the claudecodedash repo to be the current working directory so the dumper script can read workbench.db and the local archetypes.py.
---

## What this skill does

Turns "the metadata-enrichment skill feels slow and noisy" into a concrete, evidence-backed markdown proposal naming the exact `SKILL.md` paragraph or `prompt_template` phrase to change, with verbatim transcript citations.

**Inputs (read by the dumper script):**
- `StageExecution` rows in `workbench.db` (full event stream, tool counts, cost, duration, status, truncation flag — one row per stage run)
- The target skill's current `SKILL.md` at `~/.claude/skills/<skill>/SKILL.md`
- Current `prompt_template` strings in `workbench/backend/archetypes.py` for every stage that uses the skill
- The anti-exploration system prompt in `workbench/backend/sdk_runner.py`

**Output (written by Claude, not the script):**
- One markdown file under `playbook/skill_reflections/<skill>_<YYYY-MM-DD>.md` (suffixed `-2`, `-3`, ... if a file for today already exists)

This skill does NOT edit `SKILL.md` files or `archetypes.py`. The user reviews the proposal and applies edits manually.

## When to use

- User asks to "reflect on" a skill, "improve" a skill, "reduce wandering", or "tune the prompt" for a stage.
- A recent batch of runs for one skill looks expensive / slow / error-prone, and the user wants to know why.
- Before proposing a change to a SKILL.md or a stage prompt_template, to ground the change in actual agent behavior rather than intuition.

Do NOT use this skill for:
- Domain-specific playbook rule generation (use `playbook-reflector` instead — that works on review outcomes, not stage transcripts).
- Editing skill files directly (this skill only writes proposals).

## Prerequisites

- Current working directory is the claudecodedash repo root (the script reads `workbench.db` relative to the repo).
- The venv at `env/` has the repo's Python dependencies installed.
- At least one `StageExecution` row exists for a stage that uses the target skill. Typically you want ≥5 runs for meaningful pattern signal, but the script will work with fewer.

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/reflect_on_skills.py` | Cross-project context dumper. Pulls recent transcripts for one skill, resolves each to a stage_id, summarizes tool sequences / assistant text / heuristic "wandering" flags, and prints one markdown bundle. No LLM call. |

All flags documented via `--help`:
```
env/bin/python scripts/reflect_on_skills.py --help
env/bin/python scripts/reflect_on_skills.py --list                 # skills with run counts
env/bin/python scripts/reflect_on_skills.py --skill <name>         # dump to stdout
env/bin/python scripts/reflect_on_skills.py --skill <name> --output ctx.md
```

## Workflow

Follow these steps literally. Do NOT skip steps or invent alternatives.

### Step 1 — Identify the target skill

If the user named a skill, use it. If they named a stage or workflow group, resolve it to the skill (every LLM-driven stage in `workbench/backend/archetypes.py` has a `"skill"` field; composites chain sub_stages, each of which has its own skill).

If the user was vague ("reflect on the pipeline"), run the listing:
```bash
env/bin/python scripts/reflect_on_skills.py --list
```
Present the list to the user and ask which skill to reflect on. Do not try to reflect on all of them at once — one skill per reflection.

### Step 2 — Dump the context bundle

```bash
env/bin/python scripts/reflect_on_skills.py --skill <name> --limit 20 --output /tmp/reflect-<name>.md
```

Defaults: 20 runs max, all projects, no thinking excerpts. Add `--include-thinking` only if the user wants to inspect reasoning; thinking can double the bundle size.

If the script exits non-zero with "no transcripts" or "only N transcript(s)", tell the user and stop. Do not invent patterns from nothing.

### Step 3 — Read the context bundle

```
Read /tmp/reflect-<name>.md
```

The bundle has these sections, in order:
1. Aggregate metrics (JSON) — runs, projects, p50/p90 duration/cost/events, failure/truncation/agent-question rates, tool usage.
2. Current SKILL.md for the target skill — the primary candidate for revision.
3. Current stage prompt_templates that use the skill — secondary revision target.
4. Current anti-exploration system prompt from sdk_runner.py — only revise this if the pattern is truly global.
5. Per-run summaries — each has the run_id, metadata, tool_counts, heuristic flags, compact tool sequence (first ~50 calls), first/last 500 chars of assistant text, optional thinking excerpts.

The heuristic flags (`bash_exploration`, `outside_reads`, `self_clarify_hits`, `repeat_reads`, `agent_questions`) are HINTS, not conclusions. Cross-check them against the actual tool sequence and text before drawing a conclusion.

### Step 4 — Identify patterns with verbatim evidence

For each candidate pattern:
- Count how many of the N runs exhibit it (e.g., "7 of 12 runs call `ls` on the output directory before running the skill script").
- Collect at least one verbatim quote from the bundle showing the behavior. A "quote" is a verbatim excerpt from either a tool's input (e.g., a Bash command) or the assistant text. Do NOT paraphrase.
- Tag the quote with its run_id so the reader can trace it.
- Decide whether the fix belongs in SKILL.md (skill-level), a specific prompt_template (stage-level), or the sdk_runner.py system prompt (global).

Typical patterns worth flagging:
- Wrong-path retry loops (agent tries one path, falls back to `ls`, retries with correct path)
- Pre-work exploration (`ls`, `pwd`, `find`, `tree`, `cat` on files the SKILL.md already describes)
- Self-clarification ("let me first check", "I should verify") where the prompt is already explicit
- Repeated reads of the same file within one run
- Bash invocations of `python ~/.claude/skills/<wrong-skill-slug>/...` indicating the skill-loader didn't get the right name
- Truncated transcripts (log hit the 2MB cap) — strong "runaway" signal
- High agent_question rate for a skill whose prompt is supposed to be self-contained

If nothing meaningful surfaces, write that conclusion explicitly — do not fabricate problems.

### Step 5 — Write the proposal file

Output path: `playbook/skill_reflections/<skill>_<YYYY-MM-DD>.md`. If that file already exists, suffix `-2`, `-3`, and so on. Create the `playbook/skill_reflections/` directory if needed.

Use this exact structure:

```markdown
# Skill Reflection: <skill>
Generated: <YYYY-MM-DD>
Runs analyzed: N across M project(s): <list>

## Aggregate metrics
- Duration (p50 / p90): ...
- Cost (p50 / p90): ...
- Event count (p50 / p90): ...
- Failure rate / truncation rate / agent-question rate: ...
- Top tools: ...

## Observed patterns
### Pattern: <short name>
Evidence: X of N runs (cite run_ids)
Quotes:
> "verbatim excerpt" (run <run_id>)
Rationale: <why this is wandering / inefficiency / ambiguity>

(Repeat per pattern. If none significant: write "No significant patterns observed — current prompts appear effective." and move on.)

## Proposed changes

### Change 1 — <target>: <short title>
Target: SKILL.md (<skill>) | archetypes.py prompt_template (<stage_id>) | sdk_runner.py system prompt
Current:
> <exact excerpt from the provided artifact>
Proposed:
> <exact proposed text>
Rationale: <tie to a specific pattern above with run_id citations>

(0–5 changes total. If zero: write "No changes proposed.")
```

### Step 6 — Summarize for the user

Report: the output path, the number of patterns found, and a one-line summary of each proposed change. Remind the user that applying the changes is a manual step (edit SKILL.md and/or archetypes.py, then run a fresh project to validate).

## Hard rules

1. **Every proposed change cites a run_id and quotes at least one verbatim excerpt from the bundle.** No vague "improve clarity" — show the behavior that motivated the change.
2. **Quotes are verbatim, copied from the bundle.** Do not paraphrase or reconstruct. If you cannot find a verbatim excerpt that shows the pattern, weaken or drop the proposal.
3. **One target per change.** A single proposal edits one artifact (SKILL.md OR a specific prompt_template OR the system prompt), not several at once.
4. **Smallest diff that addresses the pattern.** Prefer adding or rewriting one sentence over restructuring a section.
5. **Do not edit `~/.claude/skills/<skill>/SKILL.md` or `workbench/backend/archetypes.py` directly.** This skill only writes proposals under `playbook/skill_reflections/`.
6. **If evidence is thin, say so.** "Only 2 runs available — patterns are tentative" is a valid report.

## Notes on interpretation

- `bash_exploration` counts Bash commands matching `^(git|ls|pwd|find|tree|cat|cd|echo)\b`. Sometimes these are legitimate (e.g., `ls` on a generated output directory to confirm files landed). Always cross-check with the surrounding tool sequence before labeling it wandering.
- `outside_reads` flags Read/Glob/Grep on paths outside the project working directory or on meta files (`CLAUDE.md`, `README.md`). Some skills legitimately read `SKILL.md` at runtime; that's a `Read` inside `~/.claude/skills/<skill>/` and won't be flagged.
- `truncated: true` means the event log hit the 2MB cap. Cost/duration/tool counts survive truncation, but the text tail is missing — note this when proposing patterns that depend on final-output text.
- `agent_questions > 0` means the agent parked via `scripts/agent_ask.py`. For skills that are supposed to "do all of them without asking", this is a red flag.
