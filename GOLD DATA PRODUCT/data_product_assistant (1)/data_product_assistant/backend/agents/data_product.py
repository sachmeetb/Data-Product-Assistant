"""
data_product.py — Data Product generator.

Called after the user confirms discovery results. Produces a structured
Data Product definition JSON covering:
  - KPI definitions with source columns and computation hints
  - Dimension mappings
  - Recommended table per Medallion layer
  - Data lineage (which bronze/silver/gold tables feed the product)
  - Gaps (missing KPIs/dimensions) with suggested new columns
  - Extend actions (tables that need new columns added)
"""

import re
from datetime import datetime, timezone


# ── KPI computation hint templates ───────────────────────────────────────────

def _suggest_computation(kpi_name: str, description: str, source_columns: list) -> str:
    """Return a formula/computation hint for a KPI given its name and description.

    Matching is done on whole-word tokens (not substrings) — substring matching
    previously misfired (e.g. "du`ratio`n" matched the ratio/rate branch, so
    duration KPIs got a percentage formula).
    """
    name_lower = kpi_name.lower()
    tokens     = set(re.findall(r"[a-z]+", name_lower))
    best_table = source_columns[0]["table"].split(".")[-1] if source_columns else "source table"
    best_cols  = source_columns[0].get("columns", []) if source_columns else []
    cols_str   = ", ".join(f"`{c}`" for c in best_cols[:4]) if best_cols else "available columns"

    # Distinct counts (e.g. Unique User Count) — before the generic count branch.
    if tokens & {"unique", "distinct"}:
        return (
            f"COUNT(DISTINCT key_column) GROUP BY dimensions  "
            f"— count distinct entities using {cols_str} from `{best_table}`"
        )

    # Rates / ratios / proportions / splits.
    if tokens & {"rate", "ratio", "percentage", "pct", "split", "share"} or "%" in name_lower:
        return (
            f"COUNT(condition_met) / COUNT(total) × 100  "
            f"— use {cols_str} from `{best_table}` to build numerator and denominator"
        )

    # Averages — including the average of a duration/time metric (AVG of the
    # metric column, NOT a date-diff).
    if tokens & {"average", "avg", "mean", "median"}:
        return (
            f"AVG(metric_column) GROUP BY dimensions  "
            f"— use {cols_str} from `{best_table}`"
        )

    # Elapsed-time metrics expressed as the gap between two events (no "average").
    if tokens & {"days", "time", "duration", "latency", "lag", "age", "tenure"}:
        return (
            f"DATEDIFF(end_event, start_event)  "
            f"— requires a start-event and an end-event timestamp; "
            f"nearest available columns: {cols_str} in `{best_table}`"
        )

    # Totals / counts / volumes.
    if tokens & {"total", "sum", "volume", "count", "number"}:
        return (
            f"SUM/COUNT({cols_str}) GROUP BY dimensions  "
            f"— aggregate from `{best_table}`"
        )

    # Composite scores.
    if tokens & {"score", "index", "rating"}:
        return (
            f"Composite metric — derive from {cols_str} in `{best_table}` "
            f"using a weighted formula defined by business rules"
        )

    # Generic fallback — only when we actually have columns to point at.
    if best_cols:
        return f"Derive from {cols_str} in `{best_table}` — business formula required"
    return None


def _get_conf(m: dict) -> float:
    conf = m.get("match_confidence", {})
    if isinstance(conf, dict):
        return float(conf.get("overall_confidence", 0.0))
    return float(conf or 0.0)


# ── Main generator ────────────────────────────────────────────────────────────

def generate(discovery_result: dict, requirements: dict, classification: dict) -> dict:
    """
    Build a Data Product definition from a confirmed DiscoveryResult.

    Args:
        discovery_result: Output of discovery.run()
        requirements:     Merged requirement-understanding output
        classification:   Use-case-classification output

    Returns:
        data_product dict ready for JSON serialisation
    """
    session_id     = discovery_result.get("session_id", "")
    use_case_name  = requirements.get("use_case_name", "Unnamed Use Case")
    domain         = requirements.get("domain", "")
    use_case_type  = classification.get("use_case_type", "analytics")

    # All match records across layers
    all_matches: list[dict] = []
    for key in ("gold_matches", "silver_matches", "bronze_matches"):
        all_matches.extend(discovery_result.get(key, []))

    # ── Recommended table per layer (highest-scoring reuse/extend) ───────────
    recommended: dict = {}
    for key, layer in (("gold_matches", "gold"), ("silver_matches", "silver"), ("bronze_matches", "bronze")):
        candidates = [m for m in discovery_result.get(key, []) if m.get("status") in ("reuse", "extend")]
        if not candidates:
            candidates = discovery_result.get(key, [])
        if candidates:
            best = max(candidates, key=_get_conf)
            recommended[layer] = {
                "table":    best.get("name", ""),
                "decision": best.get("status", ""),
                "score":    round(_get_conf(best), 3),
                "columns":  best.get("columns", []),
            }

    # ── KPI definitions ───────────────────────────────────────────────────────
    # Skill v2.8+ renamed `kpis` → `data_points` (each tagged with a `kind`).
    # Prefer the legacy keys when present, else derive KPIs from the
    # `data_points` entries whose kind == "kpi" (mirrors catalog_tool).
    raw_kpis = requirements.get("kpis") or requirements.get("final_kpi_list") or []
    if not raw_kpis:
        raw_kpis = [
            dp for dp in requirements.get("data_points", [])
            if isinstance(dp, dict) and dp.get("kind") == "kpi"
        ]
    kpi_defs: list[dict] = []

    for kpi in raw_kpis:
        kpi_name = kpi.get("kpi_name") or kpi.get("name") or kpi.get("kpi", "")
        kpi_desc = kpi.get("description", "")
        if not kpi_name:
            continue

        # Collect source columns from every matched table (reuse or extend)
        source_columns: list[dict] = []
        for m in all_matches:
            if m.get("status") not in ("reuse", "extend"):
                continue
            for km in m.get("kpi_matches", []):
                if km.get("kpi") == kpi_name and km.get("matched_columns"):
                    source_columns.append({
                        "table":    m.get("name", ""),
                        "layer":    m.get("layer", ""),
                        "columns":  km.get("matched_columns", []),
                        "coverage": km.get("coverage", ""),
                    })

        if source_columns:
            status = "computable"
            hint   = _suggest_computation(kpi_name, kpi_desc, source_columns)
        else:
            status = "build_new"
            hint   = None
            # Try to give a hint based on what IS available in any table
            nearby = []
            for m in all_matches:
                for km in m.get("kpi_matches", []):
                    if km.get("kpi") == kpi_name and km.get("coverage") == "description_only":
                        nearby.append({
                            "table":   m.get("name", ""),
                            "layer":   m.get("layer", ""),
                            "columns": m.get("columns", [])[:6],
                            "coverage": "description_only",
                        })
            if nearby:
                status = "needs_new_columns"
                hint   = _suggest_computation(kpi_name, kpi_desc, nearby)

        kpi_defs.append({
            "name":                 kpi_name,
            "description":          kpi_desc,
            "status":               status,
            "computation_hint":     hint,
            "source_columns":       source_columns[:4],
            "suggested_new_columns": (
                [re.sub(r"\W+", "_", kpi_name.lower()).strip("_")]
                if status == "build_new" else []
            ),
        })

    # ── Dimension definitions ─────────────────────────────────────────────────
    dim_defs: list[dict] = []
    for dim in requirements.get("granularity", requirements.get("granularity_level_required", [])):
        dim_name = dim.get("dimension", "")
        if not dim_name:
            continue
        source = None
        for m in all_matches:
            if m.get("status") not in ("reuse", "extend"):
                continue
            for dm in m.get("dimension_matches", []):
                if dm.get("dimension") == dim_name and dm.get("matched_column"):
                    source = {
                        "table":  m.get("name", ""),
                        "layer":  m.get("layer", ""),
                        "column": dm["matched_column"],
                    }
                    break
            if source:
                break
        dim_defs.append({
            "name":   dim_name,
            "status": "available" if source else "missing",
            "source": source,
            "suggested_new_column": (
                re.sub(r"\W+", "_", dim_name.lower()).strip("_")
                if not source else None
            ),
        })

    # ── Gaps ──────────────────────────────────────────────────────────────────
    gaps: list[dict] = []
    for k in kpi_defs:
        if k["status"] in ("build_new", "needs_new_columns"):
            gaps.append({
                "type":               "missing_kpi",
                "name":               k["name"],
                "description":        k["description"],
                "suggested_columns":  k["suggested_new_columns"],
                "computation_hint":   k.get("computation_hint"),
            })
    for d in dim_defs:
        if d["status"] == "missing":
            gaps.append({
                "type":              "missing_dimension",
                "name":              d["name"],
                "suggested_columns": [d["suggested_new_column"]] if d["suggested_new_column"] else [],
            })

    # ── Extend actions ────────────────────────────────────────────────────────
    build_actions: list[dict] = []
    for key, layer in (("gold_matches", "gold"), ("silver_matches", "silver"), ("bronze_matches", "bronze")):
        for m in discovery_result.get(key, []):
            if m.get("status") == "extend" and m.get("suggested_names"):
                build_actions.append({
                    "action":      "extend_table",
                    "table":       m.get("name", ""),
                    "layer":       layer,
                    "add_columns": m.get("suggested_names", []),
                    "reason":      m.get("missing_information", []),
                })

    # ── Data lineage (tables that feed this product) ──────────────────────────
    lineage: dict = {}
    for key, layer in (("gold_matches", "gold"), ("silver_matches", "silver"), ("bronze_matches", "bronze")):
        lineage[layer] = [
            m.get("name") for m in discovery_result.get(key, [])
            if m.get("status") in ("reuse", "extend")
        ]

    # ── Assemble product ──────────────────────────────────────────────────────
    dp_id = "dp-" + re.sub(r"\W+", "-", use_case_name.lower()).strip("-")
    if session_id:
        dp_id = f"dp-{session_id}"

    return {
        "data_product_id":    dp_id,
        "name":               use_case_name,
        "version":            "1.0",
        "status":             "draft",
        "domain":             domain,
        "use_case_type":      use_case_type,
        "session_id":         session_id,
        "created_at":         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kpis":               kpi_defs,
        "dimensions":         dim_defs,
        "recommended_tables": recommended,
        "data_lineage":       lineage,
        "gaps":               gaps,
        "build_actions":      build_actions,
        "discovery_summary":  discovery_result.get("summary", {}),
    }


def format_as_markdown(dp: dict) -> str:
    """Render a Data Product dict as readable markdown for the chat pane."""
    lines = [
        f"## Data Product: {dp['name']}\n",
        f"**ID:** `{dp['data_product_id']}`  |  "
        f"**Domain:** {dp.get('domain', '—')}  |  "
        f"**Type:** {dp.get('use_case_type', '—')}  |  "
        f"**Status:** {dp.get('status', 'draft')}\n",
    ]

    # KPIs
    lines.append("### KPIs\n")
    lines.append("| KPI | Status | Source Table | Key Columns | Computation |")
    lines.append("|-----|--------|-------------|-------------|-------------|")
    for k in dp.get("kpis", []):
        src = k.get("source_columns", [])
        tbl = src[0]["table"].split(".")[-1] if src else "—"
        cols = ", ".join(f"`{c}`" for c in src[0].get("columns", [])[:3]) if src else "—"
        hint = k.get("computation_hint") or ("Add new column(s): " + ", ".join(f"`{c}`" for c in k.get("suggested_new_columns", []))) or "—"
        status_label = {"computable": "Ready", "needs_new_columns": "Add columns", "build_new": "Build new"}.get(k["status"], k["status"])
        lines.append(f"| {k['name']} | {status_label} | `{tbl}` | {cols} | {hint} |")
    lines.append("")

    # Dimensions
    lines.append("### Dimensions\n")
    lines.append("| Dimension | Status | Source Table | Column |")
    lines.append("|-----------|--------|-------------|--------|")
    for d in dp.get("dimensions", []):
        src = d.get("source")
        tbl = src["table"].split(".")[-1] if src else "—"
        col = f"`{src['column']}`" if src else f"missing — add `{d.get('suggested_new_column','?')}`"
        lines.append(f"| {d['name']} | {d['status']} | `{tbl}` | {col} |")
    lines.append("")

    # Recommended tables
    lines.append("### Recommended Tables\n")
    lines.append("| Layer | Table | Decision | Score |")
    lines.append("|-------|-------|----------|-------|")
    for layer in ("gold", "silver", "bronze"):
        rec = dp.get("recommended_tables", {}).get(layer)
        if rec:
            lines.append(f"| {layer.capitalize()} | `{rec['table']}` | {rec['decision'].upper()} | {rec['score']:.2f} |")
    lines.append("")

    # Build actions
    actions = dp.get("build_actions", [])
    if actions:
        lines.append("### Actions Required\n")
        for a in actions:
            cols = ", ".join(f"`{c}`" for c in a.get("add_columns", []))
            lines.append(f"- **Extend** `{a['table']}` — add columns: {cols}")
        lines.append("")

    # Gaps
    gaps = dp.get("gaps", [])
    if gaps:
        lines.append("### Gaps\n")
        for g in gaps:
            if g["type"] == "missing_kpi":
                hint = f"  Hint: {g['computation_hint']}" if g.get("computation_hint") else ""
                lines.append(f"- **{g['name']}** (KPI) — no source columns found.{hint}")
            else:
                lines.append(f"- **{g['name']}** (dimension) — not found in any matched table")
        lines.append("")

    return "\n".join(lines)
