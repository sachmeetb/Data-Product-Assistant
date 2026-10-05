---
name: chat-reflector
description: Analyzes persisted embedded-chat sessions from the Data Workbench (workbench.db ChatSession + ChatMessage rows) and writes a triage report naming evidence-backed candidate improvements across the chat stack — the project-chat-assistant SKILL.md, the chat_runner.py system prompt, suggestion-chip prefills, and any data/graph coverage gaps the chat exposed. Use this skill when the user wants to "reflect on chats", "mine the chat logs", "see what the chat agent is struggling with", or "find graph gaps the chat is exposing". Sibling to the skill-reflector skill (which works on pipeline StageExecution rows); chat patterns frequently point at a specific pipeline skill to re-reflect on — cross-reference, do not duplicate. Requires the claudecodedash repo to be the current working directory so the dumper script can read workbench.db.
---

## What this skill does

Turns "the chat agent keeps wandering into workbench.db" or "users keep asking the same question across projects" into a concrete, evidence-backed markdown report naming the exact SKILL.md paragraph, chat_runner system-prompt clause, or suggestion chip to change — with verbatim message-tagged citations.

**Inputs (read by the dumper script):**
- `ChatSession` and `ChatMessage` rows in `workbench.db` — full content, persisted `tool_events_json`, timestamps, per-project scope
- `~/.claude/skills/project-chat-assistant/SKILL.md` (the chat agent's instructions)
- `_build_system_prompt` in `workbench/backend/chat_runner.py` (the load-bearing system prompt that carries Neo4j credentials + guardrails)
- `workbench/frontend/src/components/chat/suggestedPrompts.ts` (suggestion chip prefills)
- For each session: a timestamp-inferred workflow context snapshot (which `StageRun`s were complete for that project at the time)

**Output (written by Claude, not the script):**
- One markdown file under `playbook/chat_reflections/<YYYY-MM-DD>.md` (suffixed `-2`, `-3`, ... if today's file already exists)

This skill does NOT edit SKILL.md files, `chat_runner.py`, or `suggestedPrompts.ts`. The user reviews the report and applies changes manually.

## When to use

- User asks to "reflect on chats", "mine the chat logs", "look at chat sessions for improvements".
- User suspects the chat agent is wandering, misreading graph state, or being prompted imprecisely.
- Before tightening `project-chat-assistant/SKILL.md`, the chat system prompt, or suggestion chips, to ground the change in actual session behavior.
- User wants to know what graph coverage gaps have surfaced across recent chat activity.

Do NOT use this skill for:
- Reflecting on a specific pipeline skill — use `skill-reflector` instead, which reads `StageExecution` rows for one skill.
- Domain-playbook rule generation (the PROV-O / review-outcomes reflector).
- Editing skill or runner files directly — this skill only writes reports.

## Prerequisites

- Current working directory is the claudecodedash repo root.
- The venv at `env/` has the repo's Python deps installed.
- At least one `ChatSession` with messages exists. Typically you want ≥5 sessions for meaningful pattern signal; the script works with fewer and will say so.

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/reflect_on_chats.py` | Cross-project context dumper. Pulls recent chat sessions, parses `tool_events_json`, computes wandering / forbidden-behavior / retry heuristics, dumps one markdown bundle. No LLM call. |

All flags documented via `--help`:
```
env/bin/python scripts/reflect_on_chats.py --help
env/bin/python scripts/reflect_on_chats.py --list                       # counts per project
env/bin/python scripts/reflect_on_chats.py                              # dump to stdout
env/bin/python scripts/reflect_on_chats.py --output ctx.md              # dump to file
env/bin/python scripts/reflect_on_chats.py --project <code>             # single-project scope
env/bin/python scripts/reflect_on_chats.py --since 2026-04-20           # date filter
env/bin/python scripts/reflect_on_chats.py --limit 50                   # cap sessions
```

## Workflow

Follow these steps literally.

### Step 1 — Decide scope

If the user named a project or date range, use it. If vague ("reflect on the chats"), default to cross-project with the script's default limit and say so when reporting results. Do not try to reflect on one session in isolation — patterns require at least a handful.

If the user wants to see what's there first:
```bash
env/bin/python scripts/reflect_on_chats.py --list
```

### Step 2 — Dump the bundle

```bash
env/bin/python scripts/reflect_on_chats.py --output /tmp/reflect-chats.md
# or with filters:
env/bin/python scripts/reflect_on_chats.py --project <code> --since <YYYY-MM-DD> \
    --output /tmp/reflect-chats-<scope>.md
```

If the script reports fewer than ~5 sessions, tell the user the signal is thin and the report will be tentative. Do not invent patterns from nothing.

### Step 3 — Read the bundle

```
Read /tmp/reflect-chats.md (or the scoped path you chose)
```

Sections, in order:
1. **Aggregate metrics (JSON)** — session count, project scope, date range, message/tool/duration percentiles, tool totals, heuristic totals (across all sessions) and heuristic session counts (how many of N sessions registered any hit), and recurring title-trigram frequencies.
2. **Current project-chat-assistant SKILL.md** — primary revision target for chat-agent behavior.
3. **Current chat_runner._build_system_prompt** — the load-bearing prompt carrying Neo4j creds + `## Forbidden` clauses. Revise only when forbidden-hit flags fire.
4. **Current suggestedPrompts.ts** — revise when suggestion chips lead to user-correction follow-ups.
5. **Per-session summaries** — each has session_id, project_code, title, turn counts, duration, heuristic flags, a per-session workflow-context snapshot, and per-turn tool sequences + content head/tail.

The heuristic flags are HINTS. Cross-check against the actual tool sequences and content excerpts before drawing a conclusion:
- `bash_exploration` — regex-matches `^(git|ls|pwd|find|tree|cat|cd|echo)\b`. Legitimate sometimes; flag only if it's *before* the skill is loaded or *instead of* the canonical query path.
- `self_clarify_hits` — assistant phrases like "let me check first". Noise at low counts, signal at high counts.
- `repeat_reads` — same file read more than once within a session. Usually a wasted turn.
- `outside_reads` — Read/Glob/Grep on meta files or paths outside the project working dir.
- `user_corrections` — user messages starting with "no,", "actually,", "that's not what I meant", "I mean", "not quite". Each hit is a UX ambiguity signal.
- `forbidden_sqlite_db_hits` — Bash commands hitting `workbench.db` directly. Explicitly banned by the chat_runner prompt; any hit is a prompt-adherence failure.
- `forbidden_password_hunt_hits` — grep for `NEO4J_PASSWORD` or similar. Banned. Same.
- `cypher_calls_total` / `cypher_calls_unscoped` — calls to `run_cypher.py` and how many lacked `--project-code`. Unscoped calls are cross-project-leak candidates; `run_cypher.py` itself refuses them at runtime, but the prompt should have prevented the attempt.
- `cypher_retries` — consecutive `run_cypher.py` calls whose `--query` argument differs. Soft signal for "first query didn't answer the question" — often points at a graph coverage gap or a phrasing mismatch.

### Step 4 — Identify patterns, grouped by theme

Patterns MUST be grouped into these five themes. Each observation cites at least one verbatim quote with `session_id` and `message_id`.

**Theme 1 — Chat-agent wandering or tool-location imprecision.** Examples: calling `sqlite3 workbench.db` despite the ban; trying a skill slug shortform; `ls` before a known canonical path; reading `SKILL.md` repeatedly mid-turn. Target: `project-chat-assistant/SKILL.md` or `chat_runner.py` system prompt.

**Theme 2 — Graph / data coverage gaps.** Examples: `cypher_retries > 0` where the retry reshapes the pattern (user asked for mappings, first query matched `(:Dataset)-[:HAS_COLUMN]`, retry added a disjunction; or queries returning zero where the user's question implies data should be present). Target: observation for the user — list the missing node/relationship/property. Do NOT propose code changes for Theme 2 without explicit user direction; tag proposals in this theme `Requires user decision`.

**Theme 3 — Workflow / skill defects leaking into chat.** The chat investigated and concluded something upstream is wrong — "profiling was miscounting nulls", "the DQ test range was wrong", "this failure is a false positive". Routing depends on whether the implicated skill is LLM-driven or backend-driven: check by running `env/bin/python scripts/reflect_on_skills.py --list`. If the skill appears in the list it's LLM-driven — cross-reference to `skill-reflector` ("run `env/bin/python scripts/reflect_on_skills.py --skill <skill>`") and do not propose the fix directly. If the skill is absent (backend-driven — the workbench runs its Python scripts as subprocesses, so no `StageExecution` transcripts exist and the reflector has nothing to analyze), the Theme 3 Implication should point at the generator source directly — e.g., `~/.claude/skills/<skill>/scripts/<script>.py:<line>` — and describe the code-level behavior to look for, so the reader can review the script without detouring through the reflector.

**Theme 4 — UX / suggestion-chip ambiguity.** User correction immediately after a chip-prefilled prompt, or after a chat answer. Target: `suggestedPrompts.ts` wording, or a chip-specific canonical query in `project-chat-assistant/SKILL.md`.

**Theme 5 — Recurring cross-project questions.** The aggregate metrics include `recurring_title_trigrams` with frequency ≥ 2. If a phrase appears in sessions across ≥ 2 projects, the canonical answer (and likely the canonical query) should be in `project-chat-assistant/SKILL.md` so every project gets consistent handling. Target: add a query recipe to `project-chat-assistant/SKILL.md`.

If a theme has no evidence in this bundle, write "No significant patterns observed in this theme." Do not fabricate.

### Step 5 — Write the report

Path: `playbook/chat_reflections/<YYYY-MM-DD>.md`. If it exists, suffix `-2`, `-3`. Create the directory if needed.

Use this exact structure:

```markdown
# Chat Reflection: <scope, e.g. "cross-project, 2026-04-20 → 2026-04-23">
Generated: <YYYY-MM-DD>
Sessions analyzed: N across M project(s): <list>

<optional one-liner noting thin signal if N < 5>

## Aggregate metrics
- Sessions / messages: ...
- Duration (p50 / p90): ...
- Tool totals: ...
- Forbidden hits (sqlite / password): X / Y across Z session(s)
- Cypher: A calls, B unscoped, C retries
- Recurring title trigrams: ...

## Observations

### Theme 1 — Chat-agent wandering or tool-location imprecision
#### Observation: <short name>
Evidence: X of N sessions (session_ids: ...)
Quotes:
> "verbatim Bash command or content excerpt" (session <sid>, message <mid>)
> "another verbatim excerpt" (session <sid>, message <mid>)
Implication: <which artifact needs tightening>

(Repeat per observation. If nothing: "No significant patterns observed in this theme.")

### Theme 2 — Graph / data coverage gaps
#### Observation: <short name>
Evidence: ...
Quotes: ...
Implication: **Requires user decision.** <describe the apparent gap, e.g.
"no `:Dataset`-[:PROFILED_BY]->`:BatchRun` relationship despite queries asking for it">

### Theme 3 — Workflow / skill defects surfaced via chat
#### Observation: <short name>
Evidence: ...
Quotes: ...
Implication: If the implicated skill appears in `env/bin/python scripts/reflect_on_skills.py --list`, run `env/bin/python scripts/reflect_on_skills.py --skill <skill>` to follow up. If it does not (backend-driven: the workbench invokes the skill's Python scripts as subprocesses, no agent transcripts exist), point at the specific generator script and line range — e.g., `~/.claude/skills/<skill>/scripts/<script>.py:<lines>` — for a direct code review.

### Theme 4 — UX / suggestion-chip ambiguity
#### Observation: <short name>
Evidence: ...
Quotes: ...
Implication: <exact chip or SKILL.md query to revise>

### Theme 5 — Recurring cross-project questions
#### Observation: <short name>
Evidence: ...
Quotes: ...
Implication: <canonical query / recipe to add>

## Candidate changes

### Change 1 — <target>: <short title>
Target: project-chat-assistant/SKILL.md | chat_runner.py (_build_system_prompt) | suggestedPrompts.ts
Current:
> <exact excerpt from the provided artifact>
Proposed:
> <exact proposed text>
Rationale: <tie to a specific observation above, cite session_id + message_id>

(0–5 changes total. If zero: "No changes proposed — observations stand on their own pending user review.")
```

### Step 6 — Summarize for the user

Report back, in <150 words:
- Output path.
- Themes with significant findings (skip themes with nothing).
- Count of candidate changes.
- If Theme 3 fired, list the specific skills flagged for `skill-reflector` follow-up.

Remind the user that changes are manual — they pick which to apply.

## Hard rules

1. **Every observation cites ≥ 1 verbatim quote tagged with session_id + message_id.** No paraphrase, no reconstruction.
2. **One target per candidate change.** Don't propose "update SKILL.md and the system prompt" in one change — split them.
3. **Smallest diff.** Prefer adding or rewriting one sentence over restructuring a section.
4. **Theme 2 observations never auto-propose code changes.** Data/graph gaps need domain decisions; tag `Requires user decision` and stop.
5. **Theme 3 observations cross-reference to skill-reflector.** Do not propose edits to SKILL.md files of pipeline skills here — that's the sibling skill's job.
6. **Do not edit `~/.claude/skills/<*>/SKILL.md`, `workbench/backend/chat_runner.py`, or `suggestedPrompts.ts` directly.** Reports only.
7. **If signal is thin (< 5 sessions or heuristics all zero), say so.** Tentative reports are valid; fabricated ones are not.

## Notes on interpretation

- The `forbidden_*` heuristics fire on behavior explicitly banned in `chat_runner.py`'s `## Forbidden` block. Any hit is a prompt-adherence failure — the SKILL.md or system prompt did not prevent the behavior, so tightening one of them is usually the right response. Verify the agent actually executed the banned command (not just mentioned it in text).
- `cypher_retries` only detects adjacent-different-query patterns. A retry that got the same result as the first query won't show. If a user follow-up says "still nothing?" consider that in the same observation.
- The `workflow_context.last_execution_before_session` field lets you say things like "the user asked about profiling false-positives 3 minutes after `data_profiling_composite` completed" — strong context for Theme 3 observations.
- `recurring_title_trigrams` is a cheap clustering signal on session titles (which are the first user message truncated). If a trigram appears in sessions across multiple projects, that's Theme 5 territory.
- The chat runner persists only `tool_use` events in `tool_events_json` — no `tool_result` bodies. You cannot verify what a Cypher query returned; you can only see that it was retried with a different shape. Note this limitation in observations that depend on result-shape assumptions.
