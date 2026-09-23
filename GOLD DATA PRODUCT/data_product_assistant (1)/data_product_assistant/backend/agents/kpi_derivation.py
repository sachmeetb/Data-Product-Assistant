"""
kpi_derivation.py — grounded, validated LLM enrichment of the discovery
"Expected Tables" view.

The deterministic skeleton (which layers, table names, structure) is built in
`discovery._build_expected_tables`. This module *optionally* enriches the
KPI-derived columns of that skeleton with real, validated derivations:

  1. Gather the columns that actually exist in the discovered catalog tables.
  2. Ask the `kpi-derivation` LLM skill, grounded in those real columns, how
     each KPI is computed (formula + source columns + any new columns needed).
  3. VALIDATE: drop any cited column that does not exist in the catalog (the
     anti-hallucination guard) — moving it to `needs_new_columns` instead.
  4. MERGE the validated derivation into the skeleton columns.

`enrich()` never raises — on any failure it leaves the deterministic columns
untouched, so the feature is safe to gate behind a flag.
"""

import json

from .base import run_agent


def _available_columns(discovery_result: dict):
    """
    Collect the real columns from every matched catalog table.

    Returns (tables, names):
      tables — [{"table", "layer", "columns"}] for grounding the prompt
      names  — set of all existing column names, for validation
    """
    tables: list[dict] = []
    names: set[str] = set()
    for key, layer in (
        ("gold_matches", "gold"),
        ("silver_matches", "silver"),
        ("bronze_matches", "bronze"),
    ):
        for m in discovery_result.get(key, []):
            cols = m.get("columns") or []
            if not cols:
                continue
            tables.append({
                "table": (m.get("name", "") or "").split(".")[-1],
                "layer": layer,
                "columns": cols,
            })
            names.update(cols)
    return tables, names


async def derive(kpis: list[dict], available_tables: list[dict], session=None) -> dict:
    """One LLM call → {kpi_name: derivation_dict}. Empty dict on failure."""
    payload = {"kpis": kpis, "available_columns": available_tables}
    result = await run_agent("kpi-derivation", json.dumps(payload, indent=2), session=session)
    if not isinstance(result, dict) or "error" in result:
        return {}
    out: dict[str, dict] = {}
    for d in (result.get("derivations") or []):
        name = d.get("kpi")
        if name:
            out[name] = d
    return out


def validate(deriv_by_kpi: dict, available_names: set) -> dict:
    """
    Anti-hallucination guard: any `source_columns` entry that is not a real
    catalog column is removed and re-classified as a `needs_new_columns` entry.
    """
    avail = set(available_names)
    for d in deriv_by_kpi.values():
        src = d.get("source_columns") or []
        existing = [c for c in src if c in avail]
        missing = [c for c in src if c not in avail]
        d["source_columns"] = existing
        if missing:
            nn = list(d.get("needs_new_columns") or [])
            for m in missing:
                if m not in nn:
                    nn.append(m)
            d["needs_new_columns"] = nn
            d["complete"] = False
    return deriv_by_kpi


def _merge(expected_tables: list[dict], deriv_by_kpi: dict) -> None:
    """Rewrite the KPI-tagged columns of each expected table in place using the
    validated derivations. Gold KPI columns gain the formula; Silver/Bronze
    placeholder columns are replaced by the real source (and new) columns."""
    for table in expected_tables:
        layer = table.get("layer")
        new_cols: list[dict] = []
        src_accum: dict[str, dict] = {}      # name -> {"kpis": [...], "new": bool}
        accum_order: list[str] = []

        for col in table.get("expected_columns", []):
            kpi = col.get("kpi")
            deriv = deriv_by_kpi.get(kpi) if kpi else None
            if not kpi or not deriv:
                new_cols.append(col)          # structural/dimension or un-derived → keep
                continue

            if layer == "gold":
                formula = deriv.get("formula") or "business formula required"
                info = f"computed as: {formula}"
                if not deriv.get("complete", True):
                    nn = deriv.get("needs_new_columns") or []
                    if nn:
                        info += " (needs new column(s): " + ", ".join(f"`{c}`" for c in nn) + ")"
                note = deriv.get("note")
                if note:
                    info += f" — {note}"
                new_cols.append({"name": col["name"], "info": info, "kpi": kpi})
            else:
                # Silver/Bronze: expand placeholder into real source + new columns.
                for src in deriv.get("source_columns", []):
                    e = src_accum.setdefault(src, {"kpis": [], "new": False})
                    if src not in accum_order:
                        accum_order.append(src)
                    if kpi not in e["kpis"]:
                        e["kpis"].append(kpi)
                for nw in deriv.get("needs_new_columns", []):
                    e = src_accum.setdefault(nw, {"kpis": [], "new": True})
                    e["new"] = True
                    if nw not in accum_order:
                        accum_order.append(nw)
                    if kpi not in e["kpis"]:
                        e["kpis"].append(kpi)

        # Append accumulated source columns (Silver/Bronze), skipping names that
        # already exist as a structural/dimension column.
        existing_names = {c["name"] for c in new_cols}
        for name in accum_order:
            if name in existing_names:
                continue
            meta = src_accum[name]
            kpis_str = ", ".join(f"`{k}`" for k in meta["kpis"])
            info = (f"NEW — must be added; feeds {kpis_str}" if meta["new"]
                    else f"feeds {kpis_str}")
            new_cols.append({"name": name, "info": info, "kpi": (meta["kpis"][0] if meta["kpis"] else None)})

        table["expected_columns"] = new_cols


_LAYER_RANK = {"bronze": 0, "silver": 1, "gold": 2}


def _refine_layer_plan(view: dict, deriv_by_kpi: dict, col_to_layer: dict | None = None) -> None:
    """
    Refine the merged layer plan using validated derivations: split the Gold
    build-new table's (unverified) `delivers` into verified-derivable points
    (kept, with formula) vs. missing-from-source points (moved to Bronze's
    `missing_from_source`). Also re-mark lineage flows.

    `col_to_layer` maps each real catalog column to the layer of the matched
    table it lives in, so a rescued (derivable) point is stamped with the layer
    its source columns actually come from — NOT a hardcoded "bronze", which
    mislabels points sourced from Silver and contradicts the (empty) Bronze card.
    """
    col_to_layer = col_to_layer or {}
    layers = view.get("layers_plan") or []
    gold = next((e for e in layers if e.get("layer") == "gold"), None)
    bronze = next((e for e in layers if e.get("layer") == "bronze"), None)
    if not gold or not bronze:
        return

    # Layers that actually have a matched/proposed table — fallback origin when a
    # derivation's source columns can't be resolved to a specific layer.
    present_layers = [e["layer"] for e in layers if e.get("tables")]

    def _origin_layer(dp: str) -> str:
        der = deriv_by_kpi.get(dp) or {}
        found = [col_to_layer[c] for c in (der.get("source_columns") or []) if c in col_to_layer]
        if found:
            # most-upstream layer the source columns physically live in
            return min(found, key=lambda l: _LAYER_RANK.get(l, 9))
        if present_layers:
            return min(present_layers, key=lambda l: _LAYER_RANK.get(l, 9))
        return "bronze"

    proposed_builds = [
        t for e in layers if e.get("layer") in ("gold", "silver")
        for t in e.get("tables", []) if t.get("proposed")
    ]

    # Re-examine the deterministic source gaps: the LLM may RESCUE some as
    # derivable from existing columns (→ Gold/Silver delivers), or confirm them
    # as true source gaps (→ stay in Bronze, annotated with what they'd need).
    rescued, still_missing = [], []
    for gap in bronze.get("missing_from_source", []):
        dp = gap.get("data_point")
        der = deriv_by_kpi.get(dp)
        if der and der.get("complete", True) and der.get("source_columns"):
            rescued.append({"data_point": dp, "derivation": der.get("formula", ""), "verified": True})
        elif der:  # derivation exists but needs columns we don't have → real source gap
            needs = ", ".join(der.get("needs_new_columns", []) or [])
            note = der.get("note", "") or "Requires source data not currently captured."
            still_missing.append({"data_point": dp, "needs": needs or None, "note": note})
        else:      # no derivation info (e.g. a dimension) → keep the Option-A note
            still_missing.append(gap)

    bronze["missing_from_source"] = still_missing
    bronze["missing_note"] = ""  # precise split now
    if rescued:
        for t in proposed_builds:
            t["delivers"] = (t.get("delivers") or []) + [dict(d) for d in rescued]

    # Re-mark lineage flows AND recompute the headline so the top summary and
    # the bottom flow markers always agree (point: they were drifting apart).
    # Rescued (derivable) points are stamped with the layer their source columns
    # actually live in (via _origin_layer), so the lineage flows agree with the
    # per-layer table cards instead of always claiming "bronze".
    missing_dps = {m["data_point"] for m in still_missing}
    rescued_map = {d["data_point"]: d.get("derivation", "") for d in rescued}
    flows = (view.get("lineage") or {}).get("flows", [])
    for fl in flows:
        dp = fl.get("data_point")
        if dp in missing_dps:
            fl["status"] = "gap"
            fl["origin_layer"] = None
            fl["origin_column"] = None
        else:
            fl["status"] = "deliverable"
            if dp in rescued_map and not fl.get("origin_layer"):
                fl["origin_layer"] = _origin_layer(dp)
                fl["origin_column"] = rescued_map[dp]
    total = len(flows)
    gaps_n = len(missing_dps)
    deliv_n = total - gaps_n
    view["headline"] = (
        f"Of {total} requested data point(s): {deliv_n} available in existing data, "
        f"{gaps_n} not found in source (need new data or a new build)."
    )


async def enrich(discovery_result: dict, session=None) -> bool:
    """
    Enrich `discovery_result["discovery_view"]["expected_tables"]` with grounded,
    validated KPI derivations. Returns True if enrichment was applied.

    Never raises — on any failure the deterministic columns are left intact.
    """
    try:
        view = discovery_result.get("discovery_view") or {}
        expected = view.get("expected_tables") or []
        if not expected:
            return False

        kpi_names: list[str] = []
        for t in expected:
            for c in t.get("expected_columns", []):
                k = c.get("kpi")
                if k and k not in kpi_names:
                    kpi_names.append(k)
        if not kpi_names:
            return False

        tables, names = _available_columns(discovery_result)
        if not names:
            return False  # nothing real to ground against → keep deterministic

        # Map each real catalog column to the most-upstream layer it lives in, so
        # rescued derivations are attributed to the correct source layer.
        col_to_layer: dict[str, str] = {}
        for t in tables:
            lyr = t["layer"]
            for c in t.get("columns", []):
                if c not in col_to_layer or _LAYER_RANK.get(lyr, 9) < _LAYER_RANK.get(col_to_layer[c], 9):
                    col_to_layer[c] = lyr

        kpis = [{"name": k, "description": ""} for k in kpi_names]
        deriv = await derive(kpis, tables, session=session)
        if not deriv:
            return False

        deriv = validate(deriv, names)
        _merge(expected, deriv)
        _refine_layer_plan(view, deriv, col_to_layer)
        view["enrichment"] = "llm"
        return True
    except Exception as exc:  # belt-and-braces — enrichment must never break discovery
        print(f"[kpi-derivation] enrichment failed ({type(exc).__name__}: {exc}); "
              f"keeping deterministic columns.")
        return False
