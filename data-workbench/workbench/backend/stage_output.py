"""Per-stage output artifacts — a reviewable markdown "Step Report" of what a
pipeline stage actually produced.

Post-demo direction (2026-07-07): output visibility is the next enhancement —
users (DE + PO) should see what each workflow step produced, as an inspectable
artifact, before moving on. This renders that as markdown by reusing the SAME
vetted per-card queries the dashboard + `get_stage_results` use
(`routers.summary.detail_for_project`) — no new Cypher, no schema guessing.

Extend by adding a stage_id → (title, [cards]) entry in `_STAGE_CARDS`.
"""

from __future__ import annotations

from typing import Any, Optional

from .models import Project
from .routers.summary import detail_for_project
from .neo4j_client import neo4j_session

# Stages the "columns" card can't report well (it lacks recommendedName) get a
# dedicated query so the Step Report actually shows what the stage produced.
_RECOMMENDED_NAMES_Q = (
    "MATCH (col:Column) WHERE col.uri STARTS WITH 'column:' + $pc + ':' "
    "AND col.recommendedName IS NOT NULL AND col.recommendedName <> '' "
    "RETURN col.name AS original_name, col.recommendedName AS recommended_name, "
    "coalesce(col.recommendedNameStatus, 'pending_review') AS status "
    "ORDER BY original_name"
)
_NAMING_STAGES = ("column_name_standardization", "source_naming_recommendations")

# stage_id -> (report title, [detail cards to render, in order])
_STAGE_CARDS: dict[str, tuple[str, list[str]]] = {
    "data_discovery_composite":     ("Data Discovery — discovered schema", ["datasets", "columns"]),
    "data_discovery":               ("Data Discovery — discovered schema", ["datasets", "columns"]),
    "data_profiling_composite":     ("Data Profiling — column statistics", ["profiling"]),
    "data_profiling":               ("Data Profiling — column statistics", ["profiling"]),
    "metadata_enrichment":          ("Metadata Enrichment — descriptions", ["descriptions", "datasets"]),
    "column_name_standardization":  ("Column Name Standardization — recommended names", ["columns"]),
    "source_naming_recommendations":("Column Name Standardization — recommended names", ["columns"]),
    "odcs_to_dprod":                ("ODCS → dprod — product columns", ["data_products"]),
    "synthesize_odcs_from_graph":   ("Synthesized ODCS — product columns", ["data_products"]),
    "auto_mapping_sa":              ("Auto-Mapping — column mappings", ["mappings"]),
    "data_mapping":                 ("Mapping & Transformation — column mappings", ["mappings"]),
    "serving_virtual_view":         ("Serving (Virtual View) — generated SQL", ["serving"]),
    "deploy_virtual_view":          ("Deploy Virtual View — deployed views", ["serving"]),
    "baseline_dq_rules":            ("DQ Rules", ["dq_rules"]),
    "dq_rule_generation":           ("DQ Rules", ["dq_rules"]),
    "dq_test_generation_gx":        ("DQ Tests Generated — Great Expectations", ["dq_tests"]),
    "dq_test_generation_python":    ("DQ Tests Generated — Pure Python (Pandera)", ["dq_tests"]),
    "dq_test_execution":            ("DQ Test Results", ["dq_results"]),
    "dq_failure_analysis":          ("DQ Failure Analysis", ["dq_results"]),
}

_MAX_ROWS = 40      # cap table rows — this is a review artifact, not a data dump
_MAX_CELL = 90      # truncate long cell values


def stages_with_reports() -> list[str]:
    return sorted(_STAGE_CARDS.keys())


def _cell(v: Any) -> str:
    s = "" if v is None else str(v)
    return (s.replace("|", "\\|").replace("\n", " ")[:_MAX_CELL]
            + ("…" if len(str(v or "")) > _MAX_CELL else ""))


def _md_table(rows: list[dict]) -> str:
    if not rows:
        return "_(nothing produced)_\n"
    cols: list[str] = []
    for r in rows:
        for k in r.keys():
            if k not in cols and k != "cypher":
                cols.append(k)
    lines = ["| " + " | ".join(cols) + " |",
             "| " + " | ".join("---" for _ in cols) + " |"]
    for r in rows[:_MAX_ROWS]:
        lines.append("| " + " | ".join(_cell(r.get(c)) for c in cols) + " |")
    out = "\n".join(lines)
    if len(rows) > _MAX_ROWS:
        out += f"\n\n_… {len(rows) - _MAX_ROWS} more rows (open the UI for the full set)_"
    return out + "\n"


def _render_serving(rows: list[dict]) -> str:
    """Serving card: surface the generated view DDL as a fenced SQL block +
    deploy status, since that's the reviewable artifact for this stage."""
    if not rows:
        return "_(no serving definition yet)_\n"
    out: list[str] = []
    for r in rows:
        ddl = r.get("ddl") or r.get("ddl_sql")
        view = r.get("view_names") or r.get("view_name") or r.get("primary_view_name") or ""
        status = r.get("deployment_status") or r.get("deploymentStatus") or r.get("serving_mode") or "—"
        out.append(f"**View(s):** `{_cell(view)}` · **status:** {status}\n")
        if ddl:
            out.append("```sql\n" + str(ddl)[:4000] + "\n```\n")
        else:
            out.append(_md_table([{k: v for k, v in r.items() if k != 'ddl'}]))
    return "\n".join(out)


def _recommended_names(project: Project) -> list[dict]:
    with neo4j_session(project.neo4j_host, project.neo4j_port, project.neo4j_user,
                       project.neo4j_password, project.neo4j_database) as ns:
        return [dict(r) for r in ns.run(_RECOMMENDED_NAMES_Q, pc=project.project_code)]


def build_stage_report(project: Project, stage_id: str) -> dict:
    """Return {stage_id, title, markdown, sections} — a reviewable step report."""
    # Naming stage: the "columns" card lacks recommendedName, so query it directly
    # and show original → recommended → status (what the stage actually produced).
    if stage_id in _NAMING_STAGES:
        title = "Column Name Standardization — recommended names"
        try:
            rows = _recommended_names(project)
        except Exception as e:  # noqa: BLE001
            rows = []
            err = f"_error reading recommended names: {e}_\n"
        else:
            err = ""
        md = [f"# {title}", "", f"_Project **{project.project_code}** · stage `{stage_id}`_", "",
              f"## Recommended names — {len(rows)}\n", err or _md_table(rows)]
        return {"stage_id": stage_id, "title": title, "markdown": "\n".join(md),
                "sections": [{"card": "recommended_names", "count": len(rows)}]}

    title, cards = _STAGE_CARDS.get(stage_id, (None, None))
    if not cards:
        return {
            "stage_id": stage_id,
            "title": f"Stage output — {stage_id}",
            "markdown": (
                f"# Stage output — `{stage_id}`\n\n"
                f"_No per-step report is defined for this stage yet._ "
                f"Reportable stages: {', '.join(stages_with_reports())}.\n"
            ),
            "sections": [],
        }

    md = [f"# {title}", "", f"_Project **{project.project_code}** · stage `{stage_id}`_", ""]
    sections = []
    for card in cards:
        try:
            res = detail_for_project(project, card)
            rows = res.get("rows") or []
        except Exception as e:  # noqa: BLE001
            md.append(f"## {card.replace('_', ' ').title()}\n\n_error reading {card}: {e}_\n")
            continue
        md.append(f"## {card.replace('_', ' ').title()} — {len(rows)}\n")
        md.append(_render_serving(rows) if card == "serving" else _md_table(rows))
        sections.append({"card": card, "count": len(rows)})

    return {"stage_id": stage_id, "title": title, "markdown": "\n".join(md), "sections": sections}
