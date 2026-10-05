"""Dump a markdown context bundle describing a skill's recent transcripts,
so Claude Code can analyze it and propose evidence-backed revisions.

This script does NOT call any LLM. It reads StageExecution rows from
workbench.db (cross-project), resolves each to a skill, summarizes the
agent's tool calls / assistant text / heuristic "wandering" signals, and
writes one markdown document that contains:

  - the current SKILL.md for the target skill
  - current stage prompt_templates that use the skill
  - current anti-exploration system prompt (from sdk_runner.py)
  - aggregate metrics across collected runs
  - per-run summaries (tool sequence, text head/tail, heuristic flags)

A Claude Code session (driven by the skill-reflector SKILL) reads that
output, identifies patterns, and writes proposed revisions under
playbook/skill_reflections/. The script is the data-gathering half; Claude
Code is the reasoning half.

Usage:
    env/bin/python scripts/reflect_on_skills.py --skill metadata-enrichment
        # prints the markdown context bundle to stdout

    env/bin/python scripts/reflect_on_skills.py --skill X --output ctx.md
        # writes to a file instead

    env/bin/python scripts/reflect_on_skills.py --list
        # prints skills with persisted transcripts and their run counts
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Optional, TextIO

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sqlmodel import Session, select  # noqa: E402

from workbench.backend.archetypes import STAGE_REGISTRY  # noqa: E402
from workbench.backend.database import engine  # noqa: E402
from workbench.backend.models import Project, StageExecution, Workflow  # noqa: E402

SKILLS_ROOT = REPO_ROOT / "workbench-skills" / "skills"
SDK_RUNNER_PATH = REPO_ROOT / "workbench" / "backend" / "sdk_runner.py"

WANDERING_BASH = re.compile(r"^\s*(git|ls|pwd|find|tree|cat|cd|echo)\b")
SELF_CLARIFY = [
    re.compile(r"\blet me (first|check|verify|look|see|explore|make sure)\b", re.I),
    re.compile(r"\bI (should|need to|'ll|will) (check|verify|look|see|explore|make sure)\b", re.I),
    re.compile(r"\bto be safe\b", re.I),
    re.compile(r"\bdouble[- ]check\b", re.I),
    re.compile(r"\bfirst,? let\b", re.I),
]
META_FILES = {"CLAUDE.md", "README.md", ".gitignore"}


# ── skill / stage resolution ────────────────────────────────────────────────


def resolve_skill_stages(skill: str) -> list[str]:
    """Stage_ids whose transcripts reflect on this skill. Includes composites
    whose sub_stages use the skill. Excludes non-LLM stages."""
    out: list[str] = []
    for sid, meta in STAGE_REGISTRY.items():
        if not meta.get("requires_llm"):
            continue
        if meta.get("skill") == skill:
            out.append(sid)
            continue
        for sub_id in meta.get("sub_stages") or []:
            if STAGE_REGISTRY.get(sub_id, {}).get("skill") == skill:
                out.append(sid)
                break
    return out


def list_llm_skills() -> list[str]:
    skills: set[str] = set()
    for meta in STAGE_REGISTRY.values():
        if not meta.get("requires_llm"):
            continue
        if meta.get("skill"):
            skills.add(meta["skill"])
        for sub_id in meta.get("sub_stages") or []:
            sub = STAGE_REGISTRY.get(sub_id, {})
            if sub.get("skill"):
                skills.add(sub["skill"])
    return sorted(skills)


def _workflow_stages_cache(session: Session) -> dict[tuple[int, Optional[str]], list[str]]:
    cache: dict[tuple[int, Optional[str]], list[str]] = {}
    for w in session.exec(select(Workflow)).all():
        try:
            stages = json.loads(w.workflow_json or "[]")
            cache[(w.project_id, w.workflow_id)] = [s.get("stage_id") for s in stages]
        except (json.JSONDecodeError, TypeError):
            continue
    for p in session.exec(select(Project)).all():
        if p.workflow_json:
            try:
                stages = json.loads(p.workflow_json)
                cache[(p.id, None)] = [s.get("stage_id") for s in stages]
            except (json.JSONDecodeError, TypeError):
                continue
    return cache


def resolve_stage_id(project_id: int, workflow_id: Optional[str],
                     stage_number: int, cache: dict) -> Optional[str]:
    stages = cache.get((project_id, workflow_id))
    if not stages:
        return None
    idx = stage_number - 1
    return stages[idx] if 0 <= idx < len(stages) else None


def load_transcripts(skill: str, limit: int, session: Session,
                     projects_filter: Optional[list[str]] = None
                     ) -> list[tuple[StageExecution, Project, str]]:
    relevant = set(resolve_skill_stages(skill))
    if not relevant:
        return []
    cache = _workflow_stages_cache(session)
    projects = {p.id: p for p in session.exec(select(Project)).all()}
    if projects_filter:
        projects = {pid: p for pid, p in projects.items()
                    if p.project_code in projects_filter}
    if not projects:
        return []
    q = (select(StageExecution)
         .where(StageExecution.project_id.in_(list(projects.keys())))
         .order_by(StageExecution.started_at.desc()))
    out: list[tuple[StageExecution, Project, str]] = []
    for ex in session.exec(q).all():
        sid = resolve_stage_id(ex.project_id, ex.workflow_id, ex.stage_number, cache)
        if sid in relevant:
            out.append((ex, projects[ex.project_id], sid))
            if len(out) >= limit:
                break
    return out


# ── per-run summarization ───────────────────────────────────────────────────


def compute_wandering(events: list[dict], project_code: str) -> dict:
    bash_expl = 0
    outside_reads = 0
    file_reads: Counter = Counter()
    agent_qs = 0
    for e in events:
        t = e.get("type")
        if t == "tool_use":
            tool = e.get("tool")
            inp = e.get("input") if isinstance(e.get("input"), dict) else {}
            if tool == "Bash":
                cmd = inp.get("command", "") or ""
                if WANDERING_BASH.match(cmd):
                    bash_expl += 1
            elif tool == "Read":
                p = inp.get("file_path", "") or ""
                if p:
                    file_reads[p] += 1
                if os.path.basename(p) in META_FILES and f"/projects/{project_code}/" not in p:
                    outside_reads += 1
            elif tool in ("Glob", "Grep"):
                p = inp.get("path", "") or ""
                if p and "/projects/" not in p and not p.startswith("."):
                    outside_reads += 1
        elif t == "agent_question":
            agent_qs += 1
    repeat_reads = sum(c - 1 for c in file_reads.values() if c > 1)
    text = "".join(e.get("text", "") for e in events if e.get("type") == "text_delta")
    self_clarify = sum(len(p.findall(text)) for p in SELF_CLARIFY)
    return {
        "bash_exploration": bash_expl,
        "outside_reads": outside_reads,
        "self_clarify_hits": self_clarify,
        "repeat_reads": repeat_reads,
        "agent_questions": agent_qs,
    }


def compact_tool_sequence(events: list[dict], max_entries: int = 50) -> str:
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
                   or inp.get("query") or inp.get("path") or "")
            if isinstance(arg, str) and len(arg) > 200:
                arg = arg[:197] + "..."
        else:
            arg = str(inp)[:200]
        lines.append(f"{tool}({arg})")
        shown += 1
        if shown >= max_entries:
            if total > max_entries:
                lines.append(f"... ({total - max_entries} more tool calls)")
            break
    return "\n".join(lines) if lines else "(no tool calls)"


def summarize_run(ex: StageExecution, project: Project, stage_id: str,
                  include_thinking: bool) -> dict:
    try:
        events = json.loads(ex.log_json or "[]")
    except json.JSONDecodeError:
        events = []
    try:
        tool_counts = json.loads(ex.tool_counts_json or "{}")
    except json.JSONDecodeError:
        tool_counts = {}
    text = "".join(e.get("text", "") for e in events if e.get("type") == "text_delta")
    duration_ms = None
    if ex.completed_at and ex.started_at:
        duration_ms = int((ex.completed_at - ex.started_at).total_seconds() * 1000)
    thinking: list[str] = []
    if include_thinking:
        thinking = [e.get("text", "") for e in events if e.get("type") == "thinking"]
    return {
        "run_id": ex.run_id,
        "project_code": project.project_code,
        "stage_id": stage_id,
        "stage_number": ex.stage_number,
        "workflow_id": ex.workflow_id,
        "started_at": ex.started_at.isoformat() if ex.started_at else None,
        "duration_ms": duration_ms,
        "cost_usd": ex.cost_usd,
        "status": ex.status,
        "error_message": ex.error_message,
        "event_count": ex.event_count,
        "truncated": ex.truncated,
        "tool_counts": tool_counts,
        "text_head": text[:500],
        "text_tail": text[-500:] if len(text) > 1000 else "",
        "tool_sequence": compact_tool_sequence(events, max_entries=50),
        "heuristics": compute_wandering(events, project.project_code),
        "thinking": thinking,
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
    costs = [s["cost_usd"] for s in summaries if s["cost_usd"] is not None]
    events = [s["event_count"] for s in summaries if s["event_count"]]
    failed = sum(1 for s in summaries if s["status"] == "failed")
    truncated = sum(1 for s in summaries if s["truncated"])
    aqs = sum(1 for s in summaries if s["heuristics"]["agent_questions"] > 0)
    tool_counter: Counter = Counter()
    tool_lists: dict[str, list[int]] = defaultdict(list)
    for s in summaries:
        for tool, n in (s["tool_counts"] or {}).items():
            tool_counter[tool] += n
            tool_lists[tool].append(n)
    tool_medians = {t: statistics.median(v) for t, v in tool_lists.items()}
    n = max(len(summaries), 1)
    return {
        "runs": len(summaries),
        "projects": sorted({s["project_code"] for s in summaries}),
        "duration_ms": {"p50": _percentile(durations, 0.5),
                        "p90": _percentile(durations, 0.9)},
        "cost_usd": {"p50": _percentile(costs, 0.5),
                     "p90": _percentile(costs, 0.9)},
        "event_count": {"p50": _percentile(events, 0.5),
                        "p90": _percentile(events, 0.9)},
        "failure_rate": round(failed / n, 3),
        "truncation_rate": round(truncated / n, 3),
        "agent_question_rate": round(aqs / n, 3),
        "tool_totals": dict(tool_counter.most_common()),
        "tool_medians_per_run": tool_medians,
    }


# ── artifact loaders ────────────────────────────────────────────────────────


def load_skill_md(skill: str) -> tuple[str, Path]:
    path = SKILLS_ROOT / skill / "SKILL.md"
    if path.exists():
        return path.read_text(), path
    return f"(SKILL.md NOT FOUND at {path})", path


def load_prompt_templates(stage_ids: list[str]) -> list[tuple[str, str]]:
    return [(sid, STAGE_REGISTRY.get(sid, {}).get("prompt_template") or "(none)")
            for sid in stage_ids]


def load_anti_exploration_prompt() -> str:
    text = SDK_RUNNER_PATH.read_text()
    m = re.search(r'system_prompt\s*=\s*\(\s*(.*?)\n\s*\)', text, re.DOTALL)
    return m.group(1).strip() if m else "(could not locate)"


# ── markdown rendering ──────────────────────────────────────────────────────


def render_context(skill: str, skill_md: str, skill_md_path: Path,
                   prompt_templates: list[tuple[str, str]],
                   anti_exploration: str, metrics: dict,
                   summaries: list[dict]) -> str:
    parts: list[str] = []
    parts.append(f"# Reflection context: `{skill}`")
    parts.append(f"Generated: {date.today().isoformat()}")
    parts.append(f"Runs analyzed: {metrics['runs']} across "
                 f"{len(metrics['projects'])} project(s) "
                 f"({', '.join(metrics['projects'])})")
    parts.append("")
    parts.append("## Aggregate metrics")
    parts.append("```json")
    parts.append(json.dumps(metrics, indent=2, default=str))
    parts.append("```")
    parts.append("")
    parts.append(f"## Current SKILL.md (`{skill_md_path}`)")
    parts.append("```markdown")
    parts.append(skill_md)
    parts.append("```")
    parts.append("")
    parts.append("## Current stage prompt_templates using this skill")
    parts.append("(from `workbench/backend/archetypes.py` — `STAGE_REGISTRY[<stage_id>].prompt_template`)")
    for sid, tpl in prompt_templates:
        parts.append(f"\n### `{sid}`")
        parts.append("```")
        parts.append(tpl)
        parts.append("```")
    parts.append("")
    parts.append("## Current anti-exploration system prompt (sdk_runner.py)")
    parts.append("```python")
    parts.append(anti_exploration)
    parts.append("```")
    parts.append("")
    parts.append("## Per-run summaries")
    parts.append("Heuristic flags are local pattern counts, not conclusions.")
    parts.append("")
    for s in summaries:
        h = s["heuristics"]
        parts.append(f"### Run `{s['run_id']}`")
        parts.append(f"- Project: `{s['project_code']}`, stage_id: `{s['stage_id']}`, "
                     f"stage_number: {s['stage_number']}, workflow_id: `{s['workflow_id']}`")
        parts.append(f"- Started: {s['started_at']}, duration_ms: {s['duration_ms']}, "
                     f"cost_usd: {s['cost_usd']}, status: {s['status']}")
        parts.append(f"- event_count: {s['event_count']}, truncated: {s['truncated']}, "
                     f"error: `{s['error_message'] or ''}`")
        parts.append(f"- tool_counts: `{s['tool_counts']}`")
        parts.append(f"- heuristics: bash_exploration={h['bash_exploration']}, "
                     f"outside_reads={h['outside_reads']}, "
                     f"self_clarify_hits={h['self_clarify_hits']}, "
                     f"repeat_reads={h['repeat_reads']}, "
                     f"agent_questions={h['agent_questions']}")
        parts.append("")
        parts.append("**Tool sequence (first ~50):**")
        parts.append("```")
        parts.append(s["tool_sequence"])
        parts.append("```")
        parts.append("**Assistant text (first 500 chars):**")
        parts.append("```")
        parts.append(s["text_head"])
        parts.append("```")
        if s["text_tail"]:
            parts.append("**Assistant text (last 500 chars):**")
            parts.append("```")
            parts.append(s["text_tail"])
            parts.append("```")
        if s["thinking"]:
            joined = "\n---\n".join(s["thinking"])
            if len(joined) > 1500:
                joined = joined[:1500] + "..."
            parts.append("**Thinking excerpts:**")
            parts.append("```")
            parts.append(joined)
            parts.append("```")
        parts.append("")
    return "\n".join(parts)


def render_listing() -> str:
    """List skills with persisted transcripts and their run counts."""
    with Session(engine) as session:
        cache = _workflow_stages_cache(session)
        projects = {p.id: p for p in session.exec(select(Project)).all()}
        counts: Counter = Counter()
        for ex in session.exec(select(StageExecution)).all():
            if ex.project_id not in projects:
                continue
            sid = resolve_stage_id(ex.project_id, ex.workflow_id,
                                   ex.stage_number, cache)
            if not sid:
                continue
            meta = STAGE_REGISTRY.get(sid, {})
            if not meta.get("requires_llm"):
                continue
            skills: set[str] = set()
            if meta.get("skill"):
                skills.add(meta["skill"])
            for sub_id in meta.get("sub_stages") or []:
                sub = STAGE_REGISTRY.get(sub_id, {})
                if sub.get("skill"):
                    skills.add(sub["skill"])
            for sk in skills:
                counts[sk] += 1
    lines = ["Skill transcript counts (cross-project):"]
    if not counts:
        lines.append("  (no persisted transcripts)")
    for sk, n in counts.most_common():
        lines.append(f"  {n:>3}  {sk}")
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────────────


def run(skill: str, limit: int, min_runs: int,
        projects_filter: Optional[list[str]], include_thinking: bool,
        out: TextIO) -> int:
    stage_ids = resolve_skill_stages(skill)
    if not stage_ids:
        print(f"No LLM-driven stages use skill '{skill}'.", file=sys.stderr)
        return 2
    with Session(engine) as session:
        tuples = load_transcripts(skill, limit, session, projects_filter)
    if len(tuples) < min_runs:
        print(f"Only {len(tuples)} transcript(s) for '{skill}' "
              f"(need >= {min_runs}).", file=sys.stderr)
        return 3
    summaries = [summarize_run(ex, p, sid, include_thinking)
                 for ex, p, sid in tuples]
    metrics = aggregate_metrics(summaries)
    skill_md, skill_md_path = load_skill_md(skill)
    prompt_templates = load_prompt_templates(stage_ids)
    anti_exploration = load_anti_exploration_prompt()
    markdown = render_context(skill, skill_md, skill_md_path,
                              prompt_templates, anti_exploration,
                              metrics, summaries)
    out.write(markdown)
    if not markdown.endswith("\n"):
        out.write("\n")
    print(f"Rendered {metrics['runs']} run(s) for '{skill}' "
          f"({len(markdown)} chars).", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dump a markdown context bundle for one skill's recent "
                    "transcripts. Claude Code (via the skill-reflector SKILL) "
                    "consumes this output and writes the actual proposals."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--skill", help="Skill name, e.g. metadata-enrichment")
    group.add_argument("--list", action="store_true",
                       help="Print skills with persisted transcripts and their run counts")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--min-runs", type=int, default=1)
    parser.add_argument("--projects", help="Comma-separated project_codes to filter")
    parser.add_argument("--include-thinking", action="store_true",
                        help="Include thinking excerpts in per-run summaries")
    parser.add_argument("--output", help="Write to this file instead of stdout")
    args = parser.parse_args()

    if args.list:
        print(render_listing())
        return 0

    projects_filter = ([p.strip() for p in args.projects.split(",") if p.strip()]
                       if args.projects else None)

    if args.output and args.output != "-":
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w") as f:
            rc = run(args.skill, args.limit, args.min_runs, projects_filter,
                     args.include_thinking, f)
        if rc == 0:
            print(f"Wrote {out_path}", file=sys.stderr)
        return rc

    return run(args.skill, args.limit, args.min_runs, projects_filter,
               args.include_thinking, sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
