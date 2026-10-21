"""Deterministic functional report for a Product Assembly — markdown + mermaid.

A consolidated, socialize-ready briefing the DPO / modernization SME copies out to plan
with, then returns to scaffold. **100% deterministic** (instant, reproducible, templated
prose — NOT LLM-narrated) and **pure / graph-free**: the endpoint gathers the inputs
(score → evidence → assignment → decisions → clusters, the compiled blueprint, the theme
grouping, the estate name + scan timestamp); this module only shapes them into markdown.

It describes EXACTLY what will be scaffolded — it reuses the same
:func:`assembly_blueprint._final_attributes` per-attribute overlay and the same
:func:`assembly_blueprint.build_modernization_blueprint` output the intake handoff uses,
so the report and the scaffold can't disagree. The embedded ```mermaid block renders when
the markdown is pasted into GitHub or a markdown viewer.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from . import assembly_blueprint as ab
from . import feasibility_spec as fspec

_TIER_LABEL = {"ready": "Ready", "adaptable": "Adaptable",
               "assemblable": "Assemblable", "absent": "Absent"}
_STATUS_ORDER = ["matched", "remapped", "derived", "deferred", "excluded", "gap"]


def _cfv(x: Any) -> Any:
    """Unwrap a ConfidenceField ``{"value": ...}`` (blueprint fields are wrapped)."""
    return x.get("value") if isinstance(x, dict) and "value" in x else x


def _cell(text: Any) -> str:
    """A markdown-table-cell-safe string (escape pipes + collapse newlines)."""
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def _mm(label: str) -> str:
    """A mermaid-node-label-safe string: no quotes/brackets that break the parser."""
    return re.sub(r'["\[\]{}()<>|]', "'", str(label or "")).replace("\n", " ").strip()


def _pct(x: Any) -> str:
    try:
        return f"{round(float(x) * 100)}%"
    except (TypeError, ValueError):
        return "—"


def _attr_report_rows(
    spec: fspec.FeasibilitySpec, final: list[dict], decisions: dict[str, dict],
) -> list[dict[str, Any]]:
    """One row per spec attribute with a display status in the full taxonomy
    (matched | remapped | derived | deferred | excluded | gap) + its source/derivation.
    Reuses the ``_final_attributes`` overlay (which drops excluded + collapses matched /
    remapped into ``mapped``); this splits them back apart via the decision overlay so the
    report reads like the workspace the PO acted in."""
    final_by_name = {e["name"]: e for e in final}
    rows: list[dict[str, Any]] = []
    for i, attr in enumerate(spec.attributes):
        aid = f"#{i}:{attr.name}"
        d = decisions.get(aid) or {}
        dec = d.get("decision")
        fe = final_by_name.get(attr.name)
        if dec == "exclude":
            status, source = "excluded", ""
        elif fe is None:
            status, source = "gap", ""
        else:
            base = fe.get("status", "gap")  # mapped | derived | deferred | gap
            if base == "mapped":
                status = "remapped" if dec == "remap" else "matched"
                source = f"{fe.get('table', '')}.{fe.get('column', '')}".strip(".")
            elif base == "derived":
                status, source = "derived", fe.get("note", "")
            elif base == "deferred":
                status, source = "deferred", "map to a source later"
            else:
                status, source = "gap", fe.get("note", "unmapped — needs a source")
        rows.append({
            "name": attr.name, "status": status, "source": source,
            "type": attr.type or "string", "required": bool(attr.required),
            "is_key": bool(attr.is_key),
            "description": attr.description or attr.concept or attr.name,
        })
    return rows


def _tally(rows: list[dict]) -> dict[str, int]:
    t = {k: 0 for k in _STATUS_ORDER}
    for r in rows:
        t[r["status"]] = t.get(r["status"], 0) + 1
    return t


def _group_rows(rows: list[dict], attribute_groups: list[dict]) -> list[dict[str, Any]]:
    """Partition the attribute rows into themes (from the SpecAttributeGroups cache);
    an attribute in no theme lands in 'Other'. No cache → one 'All attributes' group."""
    if not attribute_groups:
        return [{"name": "All attributes", "rationale": "", "rows": rows}]
    groups: list[dict[str, Any]] = []
    placed: set[str] = set()
    for g in attribute_groups:
        names = set(g.get("attribute_names") or [])
        grows = [r for r in rows if r["name"] in names and r["name"] not in placed]
        for r in grows:
            placed.add(r["name"])
        groups.append({"name": g.get("name") or "Group",
                       "rationale": g.get("rationale") or "", "rows": grows})
    leftover = [r for r in rows if r["name"] not in placed]
    if leftover:
        groups.append({"name": "Other", "rationale": "", "rows": leftover})
    return [g for g in groups if g["rows"]]


def build_report_markdown(
    *,
    spec: fspec.FeasibilitySpec,
    spec_name: str,
    domain: str,
    tier: str,
    evidence: dict[str, Any],
    assignment: list[dict],
    decisions: dict[str, dict],
    blueprint: dict[str, Any],
    attribute_groups: list[dict],
    estate_name: str,
    scan_finished_at: Optional[str],
    generated_at: str,
) -> str:
    """Assemble the assembly into a deterministic markdown report (with a mermaid
    portfolio flowchart). Pure — every input is already resolved by the caller."""
    spec_name = spec_name or spec.name
    domain = domain or spec.domain
    final = ab._final_attributes(spec, assignment, decisions)
    rows = _attr_report_rows(spec, final, decisions)
    overall = _tally(rows)
    groups = _group_rows(rows, attribute_groups)

    src = blueprint.get("source_aligned") or []
    con = blueprint.get("consumer_aligned") or []
    aggregate = con[0] if con else None
    n_attrs = len(spec.attributes)
    n_required = len(spec.required_attributes)
    mapped_total = overall["matched"] + overall["remapped"]

    L: list[str] = []
    a = L.append

    # 1 — title + overview + coverage
    a(f"# Functional report — {spec_name}")
    a("")
    a(f"**Domain:** {domain or '—'} · **Feasibility tier:** "
      f"{_TIER_LABEL.get(tier, (tier or 'absent').title())} · **Generated:** {generated_at}")
    a("")
    if spec.description:
        a(spec.description)
        a("")
    a("## Overview")
    a("")
    a(f"This portfolio delivers the **{spec_name}** aggregate data product from "
      f"**{len(src)}** source-aligned product(s) built over your connected estate. "
      f"Of **{n_attrs}** template attribute(s): **{mapped_total}** mapped to real columns "
      f"({overall['matched']} matched, {overall['remapped']} remapped), "
      f"**{overall['derived']}** derived, **{overall['deferred']}** deferred, "
      f"**{overall['excluded']}** excluded from scope, and **{overall['gap']}** still a gap.")
    a("")
    a(f"- **Required coverage:** {_pct(evidence.get('required_coverage'))} "
      f"({n_required} required attribute(s))")
    a(f"- **Total coverage:** {_pct(evidence.get('total_coverage'))}")
    a("")

    # 2 — the estate & scan
    a("## The estate & scan")
    a("")
    a(f"- **Estate:** {estate_name or '—'}")
    a(f"- **Scanned:** {scan_finished_at or '—'}")
    a("")
    shortlist = [r for r in (evidence.get("schema_shortlist") or []) if r.get("included")]
    if shortlist:
        a("In-scope schemas:")
        a("")
        a("| Schema | Matched tables | Tables |")
        a("|---|---|---|")
        for r in shortlist:
            mt = [t for t in (r.get("matched_tables") or []) if t]
            sch = ".".join(x for x in (r.get("database", ""), r.get("schema", "")) if x)
            a(f"| `{_cell(sch)}` | {len(mt)} | {_cell(', '.join(mt) or '—')} |")
        a("")
    else:
        a("_Whole-estate matching (no schema shortlist) or the estate was unreachable._")
        a("")

    # 3 — the target spec-as-template
    a("## The target — spec as template")
    a("")
    a(f"The reference spec **{spec_name}** is a *template* of **{n_attrs}** attribute(s) "
      f"({n_required} required) that you curate down to what your estate actually provides.")
    a("")
    grain = [k for k in (spec.grain.keys or []) if k]
    a(f"- **Grain keys:** {', '.join(f'`{k}`' for k in grain) if grain else '—'}")
    a(f"- **History:** {spec.freshness.history.value}")
    a("")

    # 4 — curated attributes by theme
    a("## Curated attributes by theme")
    a("")
    for g in groups:
        gt = _tally(g["rows"])
        aligned = gt["matched"] + gt["remapped"] + gt["derived"]
        a(f"### {g['name']} — {len(g['rows'])} attr(s) · {aligned} aligned")
        if g["rationale"]:
            a("")
            a(f"_{g['rationale']}_")
        a("")
        a("Counts: " + " · ".join(f"{k} {gt[k]}" for k in _STATUS_ORDER if gt[k]))
        a("")
        detail = [r for r in g["rows"] if r["status"] in ("matched", "remapped", "derived")]
        if detail:
            a("| Attribute | Status | Source / derivation |")
            a("|---|---|---|")
            for r in detail:
                star = " ★" if r["required"] else ""
                a(f"| {_cell(r['name'])}{star} | {r['status']} | `{_cell(r['source'] or '—')}` |")
            a("")
        gaps = [r for r in g["rows"] if r["status"] in ("gap", "deferred")]
        if gaps:
            a("Notable gaps: "
              + ", ".join(f"**{_cell(r['name'])}** ({r['status']})" for r in gaps) + ".")
            a("")

    # 5 — the source-aligned products
    a("## Source-aligned products (build these first)")
    a("")
    if not src:
        a("_No source clusters — the aggregate draws from no in-scope tables. "
          "Open the Sources tab, refine the clusters, and Save before scaffolding._")
        a("")
    for p in src:
        datasets = p.get("datasets") or []
        a(f"### {_cfv(p.get('name'))} — {len(datasets)} table(s)")
        idea = _cfv(p.get("product_idea"))
        if idea:
            a("")
            a(str(idea))
        a("")
        a("| Table | Columns the aggregate consumes |")
        a("|---|---|")
        for ds in datasets:
            cols = [str(_cfv(c.get("name"))) for c in (ds.get("columns") or [])]
            consumed = ", ".join(cols) if cols else "*(full source discovered at build time)*"
            a(f"| `{_cell(_cfv(ds.get('name')))}` | {_cell(consumed)} |")
        a("")

    # 6 — the aggregate
    a(f"## The aggregate — {spec_name}")
    a("")
    if aggregate:
        odcs = aggregate.get("odcs") or {}
        schema = odcs.get("schema") or []
        n_out = len(schema[0].get("properties", [])) if schema else 0
        a(f"- **productKind:** `{odcs.get('productKind', 'aggregate')}`")
        a(f"- **Output columns:** {n_out}")
        input_names = [str(_cfv(pp.get("name"))) for pp in src]
        a(f"- **Inputs (:CONSUMES):** "
          f"{', '.join(input_names) if input_names else '— (bind after the sources publish)'}")
        a("")
        a("_The aggregate is authored as a native ODCS draft — schema + per-column "
          "source-intent hints — NOT wired mappings (the source `:DProdColumn` don't "
          "exist until the source products are built)._")
        a("")

    # 7 — mermaid portfolio flowchart + theme-coverage summary
    a("## Portfolio flow")
    a("")
    a("```mermaid")
    a("flowchart LR")
    for i, p in enumerate(src):
        n_tbl = len(p.get("datasets") or [])
        a(f'  s{i}["{_mm(_cfv(p.get("name")))} ({n_tbl} tables)"] --> agg')
    a(f'  agg["{_mm(spec_name)} (aggregate)"] --> uc')
    a(f'  uc(["{_mm(spec_name)} use case"])')
    a("```")
    a("")
    a("### Theme coverage")
    a("")
    a("| Theme | " + " | ".join(k.title() for k in _STATUS_ORDER) + " |")
    a("|---|" + "---|" * len(_STATUS_ORDER))
    for g in groups:
        gt = _tally(g["rows"])
        a(f"| {_cell(g['name'])} | " + " | ".join(str(gt[k]) for k in _STATUS_ORDER) + " |")
    a(f"| **Total** | " + " | ".join(f"**{overall[k]}**" for k in _STATUS_ORDER) + " |")
    a("")

    # 8 — next steps
    a("## Next steps")
    a("")
    a(f"1. **Build the {len(src)} source-aligned product(s) first** — they land in the "
      f"Engineering queue on Approve, pre-filled with each cluster's tables and the "
      f"columns this aggregate needs.")
    a(f"2. **Complete the `{spec_name}` aggregate in the wizard** — the pre-filled draft "
      f"carries the curated schema + source-intent hints; wire the mappings once the "
      f"source products publish.")
    if overall["gap"] or overall["deferred"]:
        a(f"3. **Resolve the {overall['gap']} gap(s) and {overall['deferred']} deferred "
          f"attribute(s)** — map them in the Attributes tab, or proceed and fix during "
          f"engineering.")
    a("")
    a("> Nothing is created until you **Approve** the staged intake submission — "
      "staging is reversible (reject the submission).")
    a("")

    return "\n".join(L)
