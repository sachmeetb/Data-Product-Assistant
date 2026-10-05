"""Dump a markdown context bundle describing recent embedded-chat sessions,
so Claude Code can analyze them and propose evidence-backed revisions.

Sibling to scripts/reflect_on_skills.py — same shape, different corpus.
This one reads ChatSession / ChatMessage rows from workbench.db, parses each
assistant turn's tool_events_json, and writes one markdown document that
contains:

  - the current project-chat-assistant SKILL.md
  - the chat_runner._build_system_prompt source (the load-bearing chat system prompt)
  - the frontend suggestedPrompts.ts (suggestion chip prefills)
  - aggregate metrics across collected sessions
  - per-session summaries (turns, tool sequences, content head/tail,
    heuristic wandering / forbidden-behavior / retry signals, and a
    workflow context snapshot inferred by timestamp)

A Claude Code session (driven by the chat-reflector SKILL) reads that
output, identifies patterns, and writes proposed revisions under
playbook/chat_reflections/. The script is the data-gathering half; Claude
Code is the reasoning half.

Usage:
    env/bin/python scripts/reflect_on_chats.py
        # prints the markdown context bundle to stdout (cross-project)

    env/bin/python scripts/reflect_on_chats.py --project dpe-04222026-01
        # scope to one project

    env/bin/python scripts/reflect_on_chats.py --since 2026-04-23 --limit 10
        # date / count filters

    env/bin/python scripts/reflect_on_chats.py --list
        # session counts per project
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional, TextIO

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sqlmodel import Session, select  # noqa: E402

from workbench.backend.database import engine  # noqa: E402
from workbench.backend.models import (  # noqa: E402
    ChatMessage,
    ChatSession,
    Project,
    StageExecution,
    StageRun,
)

SKILLS_ROOT = REPO_ROOT / "workbench-skills" / "skills"
CHAT_RUNNER_PATH = REPO_ROOT / "workbench" / "backend" / "chat_runner.py"
SUGGESTED_PROMPTS_PATH = (
    REPO_ROOT / "workbench" / "frontend" / "src" / "components" / "chat"
    / "suggestedPrompts.ts"
)
CHAT_ASSISTANT_SKILL = "project-chat-assistant"

WANDERING_BASH = re.compile(r"^\s*(git|ls|pwd|find|tree|cat|cd|echo)\b")
SELF_CLARIFY = [
    re.compile(r"\blet me (first|check|verify|look|see|explore|make sure)\b", re.I),
    re.compile(r"\bI (should|need to|'ll|will) (check|verify|look|see|explore|make sure)\b", re.I),
    re.compile(r"\bto be safe\b", re.I),
    re.compile(r"\bdouble[- ]check\b", re.I),
    re.compile(r"\bfirst,? let\b", re.I),
]
USER_CORRECTION = [
    re.compile(r"^\s*no[,.\s]", re.I),
    re.compile(r"\bthat('?s| is) not what i\b", re.I),
    re.compile(r"\bi mean[t]?\b", re.I),
    re.compile(r"^\s*actually[,.\s]", re.I),
    re.compile(r"\byou misunderstood\b", re.I),
    re.compile(r"\bnot (quite|exactly)\b", re.I),
]
# Commands chat_runner's system prompt explicitly forbids — seeing these in
# the event log is a strong signal the prompt needs reinforcement.
FORBIDDEN_SQLITE_DB = re.compile(
    r"(sqlite3\s+\S*workbench\.db|sqlite3\.connect\(['\"][^'\"]*workbench\.db)"
)
FORBIDDEN_PASSWORD_HUNT = re.compile(
    r"(NEO4J_PASSWORD|grep\s+-r[^\n]*(password|NEO4J))", re.I
)
META_FILES = {"CLAUDE.md", "README.md", ".gitignore"}
RUN_CYPHER_CALL = re.compile(r"run_cypher\.py")
RUN_CYPHER_PROJECT_CODE = re.compile(r"--project-code")


# ── loading ─────────────────────────────────────────────────────────────────


def load_sessions(session: Session, project_code: Optional[str],
                  since: Optional[datetime], limit: int
                  ) -> list[tuple[ChatSession, Project, list[ChatMessage]]]:
    projects = {p.id: p for p in session.exec(select(Project)).all()}
    if project_code:
        projects = {pid: p for pid, p in projects.items()
                    if p.project_code == project_code}
    if not projects:
        return []
    q = (select(ChatSession)
         .where(ChatSession.project_id.in_(list(projects.keys())))
         .order_by(ChatSession.created_at.desc()))
    if since is not None:
        q = q.where(ChatSession.created_at >= since)
    sessions = session.exec(q).all()
    out: list[tuple[ChatSession, Project, list[ChatMessage]]] = []
    for s in sessions:
        msgs = session.exec(
            select(ChatMessage)
            .where(ChatMessage.session_id == s.id)
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        ).all()
        if not msgs:
            continue
        out.append((s, projects[s.project_id], msgs))
        if len(out) >= limit:
            break
    return out


def workflow_context_snapshot(session: Session, project_id: int,
                              as_of: datetime) -> dict:
    """Nearest-in-time workflow state for a project at the moment a chat
    session started. Lets the reasoning half say 'the user asked this right
    after stage X completed'."""
    stage_runs = session.exec(
        select(StageRun).where(StageRun.project_id == project_id)
    ).all()
    last_exec = session.exec(
        select(StageExecution)
        .where(StageExecution.project_id == project_id)
        .where(StageExecution.started_at <= as_of)
        .order_by(StageExecution.started_at.desc())
    ).first()
    run_summaries = []
    for sr in stage_runs:
        completed_at = sr.completed_at.isoformat() if sr.completed_at else None
        run_summaries.append({
            "workflow_id": sr.workflow_id,
            "stage_number": sr.stage_number,
            "stage_name": sr.stage_name,
            "status": sr.status.value if hasattr(sr.status, "value") else str(sr.status),
            "completed_at": completed_at,
        })
    last_exec_summary = None
    if last_exec:
        last_exec_summary = {
            "run_id": last_exec.run_id,
            "workflow_id": last_exec.workflow_id,
            "stage_number": last_exec.stage_number,
            "started_at": last_exec.started_at.isoformat() if last_exec.started_at else None,
            "status": last_exec.status,
        }
    return {
        "stage_runs": run_summaries,
        "last_execution_before_session": last_exec_summary,
    }


# ── per-session summarization ───────────────────────────────────────────────


def _compact_tool_sequence(events: list[dict], max_entries: int = 40) -> str:
    lines: list[str] = []
    total = sum(1 for e in events if e.get("type") == "tool_use")
    shown = 0
    for e in events:
        if e.get("type") != "tool_use":
            continue
        tool = e.get("tool")
        inp = e.get("input")
        if isinstance(inp, dict):
            arg = (inp.get("file_path") or inp.get("command") or inp.get("pattern")
                   or inp.get("query") or inp.get("skill") or inp.get("path") or "")
            if isinstance(arg, str) and len(arg) > 200:
                arg = arg[:197] + "..."
        else:
            arg = str(inp)[:200]
        lines.append(f"{tool}({arg})")
        shown += 1
        if shown >= max_entries:
            if total > max_entries:
                lines.append(f"... ({total - max_entries} more tool calls in this turn)")
            break
    return "\n".join(lines) if lines else "(no tool calls)"


def _tool_counts(events: list[dict]) -> dict:
    c: Counter = Counter()
    for e in events:
        if e.get("type") == "tool_use":
            c[e.get("tool") or "unknown"] += 1
    return dict(c)


def _compute_heuristics(all_turns: list[dict], project_code: str) -> dict:
    """Per-session heuristic tallies across every turn's content + tool_events."""
    bash_expl = 0
    outside_reads = 0
    repeat_reads: Counter = Counter()
    user_corrections = 0
    self_clarify = 0
    forbidden_sqlite = 0
    forbidden_password_hunt = 0
    cypher_calls_total = 0
    cypher_calls_unscoped = 0
    cypher_retries = 0
    prev_cypher_query: Optional[str] = None
    for turn in all_turns:
        role = turn["role"]
        content = turn["content"] or ""
        events = turn["events"]
        if role == "user":
            for pat in USER_CORRECTION:
                if pat.search(content):
                    user_corrections += 1
                    break
            continue
        # assistant turn
        for pat in SELF_CLARIFY:
            self_clarify += len(pat.findall(content))
        for e in events:
            if e.get("type") != "tool_use":
                continue
            tool = e.get("tool")
            inp = e.get("input") if isinstance(e.get("input"), dict) else {}
            cmd = inp.get("command", "") or ""
            if tool == "Bash":
                if WANDERING_BASH.match(cmd):
                    bash_expl += 1
                if FORBIDDEN_SQLITE_DB.search(cmd):
                    forbidden_sqlite += 1
                if FORBIDDEN_PASSWORD_HUNT.search(cmd):
                    forbidden_password_hunt += 1
                if RUN_CYPHER_CALL.search(cmd):
                    cypher_calls_total += 1
                    if not RUN_CYPHER_PROJECT_CODE.search(cmd):
                        cypher_calls_unscoped += 1
                    # Retry heuristic: consecutive run_cypher.py calls with
                    # different --query argument suggest the first attempt
                    # didn't satisfy the agent.
                    m = re.search(r"--query\s+['\"]([^'\"]+)", cmd)
                    q = m.group(1) if m else None
                    if q and prev_cypher_query and q != prev_cypher_query:
                        cypher_retries += 1
                    if q:
                        prev_cypher_query = q
            elif tool == "Read":
                p = inp.get("file_path", "") or ""
                if p:
                    repeat_reads[p] += 1
                if os.path.basename(p) in META_FILES and f"/projects/{project_code}/" not in p:
                    outside_reads += 1
            elif tool in ("Glob", "Grep"):
                p = inp.get("path", "") or ""
                if p and "/projects/" not in p and not p.startswith("."):
                    outside_reads += 1
    repeated = sum(c - 1 for c in repeat_reads.values() if c > 1)
    return {
        "bash_exploration": bash_expl,
        "outside_reads": outside_reads,
        "self_clarify_hits": self_clarify,
        "repeat_reads": repeated,
        "user_corrections": user_corrections,
        "forbidden_sqlite_db_hits": forbidden_sqlite,
        "forbidden_password_hunt_hits": forbidden_password_hunt,
        "cypher_calls_total": cypher_calls_total,
        "cypher_calls_unscoped": cypher_calls_unscoped,
        "cypher_retries": cypher_retries,
    }


def summarize_session(chat_session: ChatSession, project: Project,
                      messages: list[ChatMessage], snapshot: dict,
                      include_tool_outputs: bool) -> dict:
    # include_tool_outputs is accepted for CLI parity; chat_runner only
    # persists tool_use events today (no tool_result bodies), so the flag
    # is effectively a no-op but kept for forward compatibility.
    _ = include_tool_outputs
    turns: list[dict] = []
    content_chars_total = 0
    tool_use_total = 0
    for m in messages:
        try:
            events = json.loads(m.tool_events_json or "[]")
        except json.JSONDecodeError:
            events = []
        content = m.content or ""
        content_chars_total += len(content)
        tool_use_total += sum(1 for e in events if e.get("type") == "tool_use")
        head = content[:300]
        tail = content[-300:] if len(content) > 800 else ""
        turns.append({
            "message_id": m.id,
            "role": m.role,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "content": content,  # kept for heuristics pass; pruned before rendering
            "events": events,
            "content_head": head,
            "content_tail": tail,
            "tool_counts": _tool_counts(events),
            "tool_sequence": _compact_tool_sequence(events, max_entries=40),
        })
    heuristics = _compute_heuristics(turns, project.project_code)
    duration_ms = None
    if len(messages) >= 2 and messages[0].created_at and messages[-1].created_at:
        duration_ms = int(
            (messages[-1].created_at - messages[0].created_at).total_seconds() * 1000
        )
    return {
        "session_id": chat_session.id,
        "project_code": project.project_code,
        "project_archetype": project.archetype,
        "project_domain": project.domain,
        "title": chat_session.title,
        "created_at": chat_session.created_at.isoformat() if chat_session.created_at else None,
        "updated_at": chat_session.updated_at.isoformat() if chat_session.updated_at else None,
        "message_count": len(messages),
        "user_turn_count": sum(1 for m in messages if m.role == "user"),
        "assistant_turn_count": sum(1 for m in messages if m.role == "assistant"),
        "tool_use_total": tool_use_total,
        "content_chars_total": content_chars_total,
        "duration_ms": duration_ms,
        "workflow_context": snapshot,
        "turns": turns,
        "heuristics": heuristics,
    }


# ── aggregation ─────────────────────────────────────────────────────────────


def _percentile(values: list, q: float):
    if not values:
        return None
    vs = sorted(values)
    k = int(round(q * (len(vs) - 1)))
    return vs[k]


def aggregate_metrics(summaries: list[dict]) -> dict:
    durations = [s["duration_ms"] for s in summaries if s["duration_ms"] is not None]
    msg_counts = [s["message_count"] for s in summaries]
    tool_totals = [s["tool_use_total"] for s in summaries]
    tool_counter: Counter = Counter()
    tool_lists: dict[str, list[int]] = defaultdict(list)
    for s in summaries:
        per_session: Counter = Counter()
        for turn in s["turns"]:
            for t, n in turn["tool_counts"].items():
                per_session[t] += n
        for t, n in per_session.items():
            tool_counter[t] += n
            tool_lists[t].append(n)
    tool_medians = {t: statistics.median(v) for t, v in tool_lists.items() if v}
    # Title phrase frequency (token trigrams, lowercased) — surfaces
    # recurring question shapes across sessions.
    trigrams: Counter = Counter()
    for s in summaries:
        title = (s["title"] or "").lower().strip()
        title = re.sub(r"[^a-z0-9\s]", " ", title)
        toks = [t for t in title.split() if len(t) > 2]
        for i in range(len(toks) - 2):
            trigrams[" ".join(toks[i:i + 3])] += 1
    common_trigrams = [t for t in trigrams.most_common(15) if t[1] >= 2]
    # Heuristic aggregates.
    h_keys = [
        "bash_exploration", "outside_reads", "self_clarify_hits", "repeat_reads",
        "user_corrections", "forbidden_sqlite_db_hits", "forbidden_password_hunt_hits",
        "cypher_calls_total", "cypher_calls_unscoped", "cypher_retries",
    ]
    h_totals = {k: sum(s["heuristics"][k] for s in summaries) for k in h_keys}
    # Sessions containing any hit of each heuristic (not counts, so rarity is visible).
    h_sessions = {k: sum(1 for s in summaries if s["heuristics"][k] > 0) for k in h_keys}
    return {
        "sessions": len(summaries),
        "projects": sorted({s["project_code"] for s in summaries}),
        "date_range": {
            "earliest": min((s["created_at"] for s in summaries if s["created_at"]),
                            default=None),
            "latest": max((s["created_at"] for s in summaries if s["created_at"]),
                           default=None),
        },
        "message_count": {"p50": _percentile(msg_counts, 0.5),
                          "p90": _percentile(msg_counts, 0.9)},
        "duration_ms": {"p50": _percentile(durations, 0.5),
                        "p90": _percentile(durations, 0.9)},
        "tool_use_total": {"p50": _percentile(tool_totals, 0.5),
                           "p90": _percentile(tool_totals, 0.9)},
        "tool_totals": dict(tool_counter.most_common()),
        "tool_medians_per_session": tool_medians,
        "heuristic_totals": h_totals,
        "heuristic_session_counts": h_sessions,
        "recurring_title_trigrams": common_trigrams,
    }


# ── artifact loaders ────────────────────────────────────────────────────────


def load_chat_skill_md() -> tuple[str, Path]:
    path = SKILLS_ROOT / CHAT_ASSISTANT_SKILL / "SKILL.md"
    if path.exists():
        return path.read_text(), path
    return f"(SKILL.md NOT FOUND at {path})", path


def load_chat_runner_system_prompt() -> str:
    if not CHAT_RUNNER_PATH.exists():
        return f"(chat_runner.py NOT FOUND at {CHAT_RUNNER_PATH})"
    text = CHAT_RUNNER_PATH.read_text()
    # Pull out the _build_system_prompt function — that's the load-bearing
    # credentials + guardrails block referenced in CLAUDE.md.
    m = re.search(r"def _build_system_prompt\b.*?(?=\n(?:def |class )|\Z)",
                  text, re.DOTALL)
    return m.group(0).strip() if m else "(could not locate _build_system_prompt in chat_runner.py)"


def load_suggested_prompts() -> str:
    if not SUGGESTED_PROMPTS_PATH.exists():
        return f"(suggestedPrompts.ts NOT FOUND at {SUGGESTED_PROMPTS_PATH})"
    return SUGGESTED_PROMPTS_PATH.read_text()


# ── markdown rendering ──────────────────────────────────────────────────────


def render_context(summaries: list[dict], metrics: dict,
                   chat_skill_md: str, chat_skill_md_path: Path,
                   chat_runner_prompt: str, suggested_prompts: str) -> str:
    parts: list[str] = []
    scope = (", ".join(metrics["projects"]) if metrics["projects"] else "(none)")
    parts.append("# Reflection context: embedded chat sessions")
    parts.append(f"Generated: {date.today().isoformat()}")
    parts.append(f"Sessions analyzed: {metrics['sessions']} across "
                 f"{len(metrics['projects'])} project(s) ({scope})")
    parts.append(f"Date range: {metrics['date_range']['earliest']} → "
                 f"{metrics['date_range']['latest']}")
    parts.append("")
    parts.append("## Aggregate metrics")
    parts.append("```json")
    parts.append(json.dumps(metrics, indent=2, default=str))
    parts.append("```")
    parts.append("")
    parts.append(f"## Current project-chat-assistant SKILL.md (`{chat_skill_md_path}`)")
    parts.append("```markdown")
    parts.append(chat_skill_md)
    parts.append("```")
    parts.append("")
    parts.append("## Current chat_runner._build_system_prompt (`workbench/backend/chat_runner.py`)")
    parts.append("```python")
    parts.append(chat_runner_prompt)
    parts.append("```")
    parts.append("")
    parts.append(f"## Current suggestedPrompts.ts (`{SUGGESTED_PROMPTS_PATH}`)")
    parts.append("```typescript")
    parts.append(suggested_prompts)
    parts.append("```")
    parts.append("")
    parts.append("## Per-session summaries")
    parts.append("Heuristic flags are local pattern counts, not conclusions. "
                 "Forbidden-hit flags reflect behaviors banned by the "
                 "chat_runner system prompt — if they fire, the prompt or "
                 "SKILL.md guardrails were not effective for that session.")
    parts.append("")
    for s in summaries:
        parts.append(f"### Session `{s['session_id']}` — {s['project_code']}")
        parts.append(f"- Title: `{(s['title'] or '').strip()[:120]}`")
        parts.append(f"- Archetype: `{s['project_archetype']}`, "
                     f"domain: `{s['project_domain']}`")
        parts.append(f"- Created: {s['created_at']}, updated: {s['updated_at']}, "
                     f"duration_ms: {s['duration_ms']}")
        parts.append(f"- Turns: {s['user_turn_count']} user / "
                     f"{s['assistant_turn_count']} assistant "
                     f"({s['message_count']} messages, {s['tool_use_total']} tool calls, "
                     f"{s['content_chars_total']} content chars)")
        h = s["heuristics"]
        parts.append(
            f"- heuristics: bash_exploration={h['bash_exploration']}, "
            f"self_clarify_hits={h['self_clarify_hits']}, "
            f"repeat_reads={h['repeat_reads']}, "
            f"outside_reads={h['outside_reads']}, "
            f"user_corrections={h['user_corrections']}, "
            f"forbidden_sqlite_db={h['forbidden_sqlite_db_hits']}, "
            f"forbidden_password_hunt={h['forbidden_password_hunt_hits']}, "
            f"cypher_calls_total={h['cypher_calls_total']}, "
            f"cypher_calls_unscoped={h['cypher_calls_unscoped']}, "
            f"cypher_retries={h['cypher_retries']}"
        )
        parts.append("")
        wctx = s["workflow_context"]
        parts.append("**Workflow context at session start (timestamp-inferred):**")
        parts.append("```json")
        parts.append(json.dumps(wctx, indent=2, default=str))
        parts.append("```")
        parts.append("")
        for turn in s["turns"]:
            parts.append(f"#### Turn — message `{turn['message_id']}` "
                         f"({turn['role']}, {turn['created_at']})")
            if turn["role"] == "user":
                parts.append("**User content:**")
                parts.append("```")
                parts.append(turn["content_head"])
                if turn["content_tail"]:
                    parts.append("... [truncated] ...")
                    parts.append(turn["content_tail"])
                parts.append("```")
            else:
                parts.append(f"- tool_counts: `{turn['tool_counts']}`")
                parts.append("**Tool sequence:**")
                parts.append("```")
                parts.append(turn["tool_sequence"])
                parts.append("```")
                parts.append("**Assistant content (first 300 chars):**")
                parts.append("```")
                parts.append(turn["content_head"])
                parts.append("```")
                if turn["content_tail"]:
                    parts.append("**Assistant content (last 300 chars):**")
                    parts.append("```")
                    parts.append(turn["content_tail"])
                    parts.append("```")
            parts.append("")
    return "\n".join(parts)


def render_listing() -> str:
    with Session(engine) as session:
        projects = {p.id: p for p in session.exec(select(Project)).all()}
        rows = session.exec(select(ChatSession)).all()
        per_project: Counter = Counter()
        for s in rows:
            p = projects.get(s.project_id)
            if p:
                per_project[p.project_code] += 1
        msgs_per_project: Counter = Counter()
        for m in session.exec(select(ChatMessage)).all():
            sess = session.get(ChatSession, m.session_id)
            if sess and sess.project_id in projects:
                msgs_per_project[projects[sess.project_id].project_code] += 1
    lines = ["Chat session counts by project (cross-project):"]
    if not per_project:
        lines.append("  (no chat sessions)")
    for code, n in per_project.most_common():
        lines.append(f"  {n:>3} sessions / {msgs_per_project.get(code, 0):>3} messages  {code}")
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────────────


def _parse_since(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise SystemExit(f"Could not parse --since value: {raw!r}")


def run(project_code: Optional[str], since: Optional[datetime], limit: int,
        min_sessions: int, include_tool_outputs: bool, out: TextIO) -> int:
    with Session(engine) as session:
        tuples = load_sessions(session, project_code, since, limit)
        if len(tuples) < min_sessions:
            print(f"Only {len(tuples)} chat session(s) match the filter "
                  f"(need >= {min_sessions}).", file=sys.stderr)
            return 3
        summaries: list[dict] = []
        for chat_sess, project, msgs in tuples:
            started = chat_sess.created_at or datetime.utcnow()
            snapshot = workflow_context_snapshot(session, project.id, started)
            summaries.append(
                summarize_session(chat_sess, project, msgs, snapshot,
                                  include_tool_outputs)
            )
    metrics = aggregate_metrics(summaries)
    chat_skill_md, chat_skill_md_path = load_chat_skill_md()
    chat_runner_prompt = load_chat_runner_system_prompt()
    suggested_prompts = load_suggested_prompts()
    # Prune raw content from per-turn dicts before rendering to avoid dumping
    # the full message body twice (head/tail are already in the render).
    for s in summaries:
        for turn in s["turns"]:
            turn.pop("content", None)
            turn.pop("events", None)
    markdown = render_context(summaries, metrics, chat_skill_md, chat_skill_md_path,
                              chat_runner_prompt, suggested_prompts)
    out.write(markdown)
    if not markdown.endswith("\n"):
        out.write("\n")
    print(f"Rendered {metrics['sessions']} chat session(s) "
          f"({len(markdown)} chars).", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dump a markdown context bundle for recent embedded-chat "
                    "sessions. Claude Code (via the chat-reflector skill) "
                    "consumes this output and writes the actual proposals."
    )
    parser.add_argument("--project", help="Scope to one project_code")
    parser.add_argument("--since", help="Only sessions created >= this date "
                                        "(YYYY-MM-DD or ISO timestamp)")
    parser.add_argument("--limit", type=int, default=30,
                        help="Max sessions to include (default 30, newest first)")
    parser.add_argument("--min-sessions", type=int, default=1,
                        help="Exit non-zero if fewer than this many sessions match")
    parser.add_argument("--include-tool-outputs", action="store_true",
                        help="Include tool_result bodies if persisted (currently "
                             "no-op; chat_runner only stores tool_use events)")
    parser.add_argument("--output", help="Write to this file instead of stdout")
    parser.add_argument("--list", action="store_true",
                        help="Print chat session counts per project")
    args = parser.parse_args()

    if args.list:
        print(render_listing())
        return 0

    since = _parse_since(args.since)

    if args.output and args.output != "-":
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w") as f:
            rc = run(args.project, since, args.limit, args.min_sessions,
                     args.include_tool_outputs, f)
        if rc == 0:
            print(f"Wrote {out_path}", file=sys.stderr)
        return rc

    return run(args.project, since, args.limit, args.min_sessions,
               args.include_tool_outputs, sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
