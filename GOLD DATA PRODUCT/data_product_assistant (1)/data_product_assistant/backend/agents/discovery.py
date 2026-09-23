"""
discovery.py — Data Discovery Agent — Python-driven cascade.

Uses match_requirements_to_catalog as the primary engine:
  - Takes the combined JSON from Requirement Agent + Use Case Determinator
  - Searches utility_catalog.json for matching tables and columns
  - Uses spec-authoritative confidence scores (not keyword heuristics)
  - Returns column-level KPI→column, dimension→column mappings per table
  - Builds gold / silver / bronze match arrays with cascade trace

No LLM is used for JSON assembly — the entire pipeline is deterministic Python.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from tools.catalog_tool import CatalogPlugin


def _resolve_fields(matches: list) -> list[str]:
    """Field names resolved by reuse-level matches."""
    resolved = []
    for m in matches:
        if m.get("status") == "reuse":
            for f in m.get("matched_fields", []):
                fname = (f.get("field", "") if isinstance(f, dict) else str(f))
                if fname:
                    resolved.append(fname)
    return resolved


def _norm_target(s: str) -> str:
    """Lower-case and collapse punctuation to single spaces for comparison."""
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _dedup_targets(targets: list[str]) -> list[str]:
    """Collapse noise/overlapping data points before they drive discovery.

    The requirement agent occasionally emits a bare generic token alongside the
    specific data point it belongs to — e.g. "campaign" next to "Campaign Name"/
    "Campaign ID", or a lower-case "date" next to "Date". Those extras match no
    catalog column on an exact (case-folded) compare and render as phantom
    NOT FOUND chips. This pass removes them, preserving original casing/order:

      - drop empty / whitespace-only entries
      - drop exact case-insensitive duplicates (keep first occurrence)
      - drop a target whose words are a strict subset of another target's words
        (e.g. {campaign} ⊂ {campaign, name}); single-token noise like "campaign"
        is absorbed by the multi-word data point that already covers it.
    """
    cleaned = [t for t in targets if t and t.strip()]
    word_sets = {t: frozenset(_norm_target(t).split()) for t in cleaned}
    result: list[str] = []
    seen: set[str] = set()
    for t in cleaned:
        norm = _norm_target(t)
        if not norm or norm in seen:
            continue
        words = word_sets[t]
        redundant = any(
            words < word_sets[other]
            for other in cleaned
            if _norm_target(other) != norm
        )
        if redundant:
            continue
        seen.add(norm)
        result.append(t)
    return result


# ── Discovery view (frontend card) ────────────────────────────────────────────
#
# The pipeline produces a structured `discovery_view` payload alongside the
# legacy gold/silver/bronze match arrays. It mirrors the shape the DD-V2 HTML
# mock expects: layer summary tiles, summary by data points, per-table cards
# with sample values, and a visual flow.

_COUNT_METHODS = {"count_source", "count_source_alias", "count"}


def _layer_status(matches: list) -> str:
    for m in matches:
        if any(k.get("coverage") in {"full", "partial"} for k in m.get("kpi_matches", [])):
            return "found"
        if any(d.get("coverage") == "matched" for d in m.get("dimension_matches", [])):
            return "found"
        if m.get("matched_fields"):
            return "found"
        if m.get("status") in {"reuse", "extend"}:
            return "found"
    return "not_found"


def _has_any_match(m: dict) -> bool:
    if any(k.get("coverage") in {"full", "partial"} for k in m.get("kpi_matches", [])):
        return True
    if any(d.get("coverage") == "matched" for d in m.get("dimension_matches", [])):
        return True
    if m.get("matched_fields"):
        return True
    return m.get("status") in {"reuse", "extend"}


def _table_short_name(full_name: str) -> str:
    return full_name.split(".")[-1] if full_name else ""


def _best_bronze_source(bronze_matches: list) -> str:
    """Highest-confidence bronze table with a real match — the source a
    build_new asset would be built from."""
    candidates = [m for m in bronze_matches if _has_any_match(m)]
    if not candidates:
        return ""
    best = max(
        candidates,
        key=lambda m: m.get("match_confidence", {}).get("overall_confidence", 0),
    )
    return _table_short_name(best.get("name", ""))


def _build_verdicts(
    gold_matches: list,
    silver_matches: list,
    bronze_matches: list,
    remaining_unmatched: list[str],
) -> dict:
    """
    Group discovery results into Reuse / Extend / Build-New verdicts for the
    DiscoveryResultCard banner.

    - reuse / extend: real matched tables (across any layer), using the
      catalog tool's per-match `recommended_action` as the rationale.
    - build_new: derived from requirement terms that no layer resolved
      (`remaining_unmatched`), not from low-scoring catalog tables — and the
      rationale names the Bronze source to build from when one is available.
    """
    verdicts: dict[str, list] = {"reuse": [], "extend": [], "build_new": []}
    seen: set[tuple] = set()

    for matches in (gold_matches, silver_matches, bronze_matches):
        for m in matches:
            if not _has_any_match(m):
                continue
            status = m.get("status")
            if status not in ("reuse", "extend"):
                continue
            short = _table_short_name(m.get("name", ""))
            key = (status, short)
            if not short or key in seen:
                continue
            seen.add(key)
            verdicts[status].append({
                "table": short,
                "rationale": m.get("recommended_action", ""),
            })

    if remaining_unmatched:
        bronze_src = _best_bronze_source(bronze_matches)
        dp_list = ", ".join(remaining_unmatched)
        if bronze_src:
            rationale = (
                f"no suitable Gold or Silver table exists for {dp_list}; "
                f"build a new Gold asset from Bronze source `{bronze_src}`."
            )
        else:
            rationale = (
                f"no suitable existing asset covers {dp_list}; "
                f"build a new asset from raw source data."
            )

        suggested = ""
        for matches in (gold_matches, silver_matches, bronze_matches):
            for m in matches:
                for s in m.get("suggested_names", []):
                    if s:
                        suggested = s
                        break
                if suggested:
                    break
            if suggested:
                break

        verdicts["build_new"].append({
            "table": suggested or "new_gold_asset",
            "rationale": rationale,
        })

    return verdicts


def _slug(text: str) -> str:
    return re.sub(r"\W+", "_", (text or "").lower()).strip("_")


def _build_expected_tables(
    use_case_name: str,
    layer_summary: dict,
    kpi_names: list[str],
    dim_names: list[str],
) -> list[dict]:
    """
    For each layer that yielded NO usable table, propose the table that *should*
    exist there to fulfil the requested KPIs/dimensions — a guessed name plus the
    columns it would need to carry, and what each holds.

    Deterministic, best-effort guidance (the agent can't know real table names);
    only layers whose discovery status is not "found" are proposed.
    """
    kpi_names = [k for k in kpi_names if k]
    dim_names = [d for d in dim_names if d]
    kpi_cols  = [_slug(k) for k in kpi_names]
    dim_cols  = [_slug(d) for d in dim_names]
    slug = _slug(use_case_name) or "use_case"

    date_like = any(
        any(tok in d.lower() for tok in
            ("date", "day", "dai", "hour", "week", "month", "year", "time"))
        for d in dim_names
    )

    def gold_cols() -> list[dict]:
        cols = [{"name": c, "info": "grouping dimension"} for c in dim_cols]
        if not date_like:
            cols.append({"name": "report_date", "info": "aggregation grain (e.g. daily)"})
        cols += [{"name": kc, "info": f"pre-aggregated value for `{kn}`", "kpi": kn}
                 for kc, kn in zip(kpi_cols, kpi_names)]
        return cols

    def silver_cols() -> list[dict]:
        cols = [{"name": "event_id", "info": "unique business key (one row per event)"}]
        cols += [{"name": c, "info": "conformed / standardised dimension"} for c in dim_cols]
        cols.append({"name": "event_timestamp", "info": "event time — source for date/hour grain"})
        cols += [{"name": f"{kc}_base", "info": f"cleaned base measure(s) `{kn}` is derived from", "kpi": kn}
                 for kc, kn in zip(kpi_cols, kpi_names)]
        return cols

    def bronze_cols() -> list[dict]:
        cols = [{"name": "id", "info": "raw source record id"}]
        cols += [{"name": c, "info": "raw dimension attribute as captured at source"} for c in dim_cols]
        cols.append({"name": "event_ts", "info": "raw event timestamp"})
        cols += [{"name": f"{kc}_raw", "info": f"raw field(s) ultimately underlying `{kn}`", "kpi": kn}
                 for kc, kn in zip(kpi_cols, kpi_names)]
        cols.append({"name": "ingestion_ts", "info": "load / ingestion metadata"})
        return cols

    specs = {
        "gold": (
            f"{slug}_gold_summary",
            "Consumption-ready aggregated table exposing each requested KPI, grouped by "
            "the requested dimensions — what a dashboard would query directly.",
            gold_cols,
        ),
        "silver": (
            f"{slug}_silver_conformed",
            "Cleaned, conformed, de-duplicated records with standardised dimension keys "
            "and the base measures each KPI is derived from.",
            silver_cols,
        ),
        "bronze": (
            f"raw_{slug}_events",
            "Raw, immutable source events as ingested — the lowest-level facts from "
            "which every KPI is ultimately derived.",
            bronze_cols,
        ),
    }

    proposals: list[dict] = []
    for layer in ("gold", "silver", "bronze"):
        if (layer_summary.get(layer) or {}).get("status") == "found":
            continue
        name, purpose, cols_fn = specs[layer]
        proposals.append({
            "layer": layer,
            "proposed_name": name,
            "purpose": purpose,
            "expected_columns": cols_fn(),
        })
    return proposals


def _build_table_card(m: dict) -> dict:
    """
    One row per user-requested term that this table contributed to, with the
    sample values drawn by the catalog tool. Driven by matched_fields so
    catalog-supplied alias matches (e.g. impressions → impression_id pulled
    from expected_confidence) get rendered too.
    """
    kpi_index = {k.get("kpi", "").lower(): k for k in m.get("kpi_matches", [])}
    dim_index = {d.get("dimension", "").lower(): d for d in m.get("dimension_matches", [])}

    by_term: dict[str, list[dict]] = {}
    for f in m.get("matched_fields", []) or []:
        term = (f.get("requirement_term") or "").strip()
        if not term:
            continue  # catalog enrichment with no user term — skip
        by_term.setdefault(term, []).append(f)

    rows: list[dict] = []
    for term, fields in by_term.items():
        kpi = kpi_index.get(term.lower())
        dim = dim_index.get(term.lower())

        if kpi:
            entries = kpi.get("sample_logic") or []
            if entries:
                logic_str = " + ".join(e["logic"] for e in entries if e.get("logic"))
                example_str = "; ".join(e["example"] for e in entries if e.get("example"))
            else:
                is_count = any(f.get("match_method") in _COUNT_METHODS for f in fields)
                logic_str = " + ".join(
                    f"COUNT({f.get('field','')})" if is_count else f.get("field", "")
                    for f in fields
                )
                example_str = "; ".join(
                    f"{f.get('field','')} = {f.get('sample_value','')}"
                    if f.get("sample_value")
                    else f.get("field", "")
                    for f in fields
                )
            sample_dp = kpi.get("sample_data_point") or ""
            if not sample_dp:
                sample_dp = next(
                    (f.get("sample_value", "") for f in fields if f.get("sample_value")),
                    "",
                )
            rows.append({
                "data_point": term,
                "sample_data_point_value": sample_dp,
                "matched_column_or_logic": logic_str,
                "sample_matched_value": example_str,
            })
            continue

        if dim:
            rows.append({
                "data_point": term,
                "sample_data_point_value": dim.get("sample_value") or "",
                "matched_column_or_logic": dim.get("matched_column") or fields[0].get("field", ""),
                "sample_matched_value": dim.get("sample_example") or "",
            })
            continue

        primary = fields[0]
        col = primary.get("field", "")
        sample = primary.get("sample_value", "")
        rows.append({
            "data_point": term,
            "sample_data_point_value": sample,
            "matched_column_or_logic": col,
            "sample_matched_value": (f"{col} = {sample}" if sample else col),
        })

    return {
        "table_full_name": m.get("name", ""),
        "table_short_name": _table_short_name(m.get("name", "")),
        "rows": rows,
    }


def _build_summary_by_data_points(
    gold_matches: list,
    silver_matches: list,
    bronze_matches: list,
    all_targets: list[str],
) -> list[dict]:
    layer_index = [
        ("Gold",   gold_matches),
        ("Silver", silver_matches),
        ("Bronze", bronze_matches),
    ]

    summary_rows = []
    for term in all_targets:
        term_lower = term.lower()
        found_in_layer = ""
        tables: list[str] = []
        logic = ""

        for layer_label, matches in layer_index:
            for m in matches:
                short = _table_short_name(m.get("name", ""))
                for k in m.get("kpi_matches", []):
                    if (k.get("kpi", "").lower() == term_lower
                            and k.get("coverage") in {"full", "partial"}):
                        found_in_layer = layer_label
                        tables.append(short)
                        if not logic:
                            entries = k.get("sample_logic") or []
                            logic = (
                                " + ".join(e["logic"] for e in entries if e.get("logic"))
                                or ", ".join(k.get("matched_columns", []))
                            )
                for d in m.get("dimension_matches", []):
                    if (d.get("dimension", "").lower() == term_lower
                            and d.get("coverage") == "matched"):
                        found_in_layer = found_in_layer or layer_label
                        tables.append(short)
                        if not logic:
                            logic = d.get("matched_column") or ""
                for f in m.get("matched_fields", []):
                    if f.get("requirement_term", "").lower() == term_lower:
                        found_in_layer = found_in_layer or layer_label
                        tables.append(short)
                        if not logic:
                            col = f.get("field", "")
                            logic = f"COUNT({col})" if f.get("match_method") in _COUNT_METHODS else col
            if found_in_layer:
                break

        seen, table_list = set(), []
        for t in tables:
            if t and t not in seen:
                seen.add(t)
                table_list.append(t)

        summary_rows.append({
            "data_point": term,
            "result": f"Found in {found_in_layer}" if found_in_layer else "Not found",
            "tables": table_list,
            "matched_column_or_logic": logic,
        })

    return summary_rows


def _build_discovery_view(
    use_case_name: str,
    gold_matches: list,
    silver_matches: list,
    bronze_matches: list,
    all_targets: list[str],
) -> dict:
    layer_summaries = {
        "gold":   {"status": _layer_status(gold_matches),
                   "table_count": sum(1 for m in gold_matches if _has_any_match(m))},
        "silver": {"status": _layer_status(silver_matches),
                   "table_count": sum(1 for m in silver_matches if _has_any_match(m))},
        "bronze": {"status": _layer_status(bronze_matches),
                   "table_count": sum(1 for m in bronze_matches if _has_any_match(m))},
    }

    def _layer_tables(matches: list) -> list[dict]:
        return [_build_table_card(m) for m in matches if _has_any_match(m)]

    layer_order = ["gold", "silver", "bronze"]
    found_layers = [l for l in layer_order if layer_summaries[l]["status"] == "found"]
    not_found_layers = [l for l in layer_order if layer_summaries[l]["status"] != "found"]
    if found_layers:
        found_str = ", ".join(l.capitalize() for l in found_layers)
        if not_found_layers:
            nf_str = " or ".join(l.capitalize() for l in not_found_layers)
            result_text = (
                f"The requested data points were not found in {nf_str}, "
                f"but found in {found_str}."
            )
        else:
            result_text = f"The requested data points were found in {found_str}."
    else:
        result_text = "No matching tables found across Gold, Silver, or Bronze layers."

    summary_rows = _build_summary_by_data_points(
        gold_matches, silver_matches, bronze_matches, all_targets,
    )

    # Gap analysis — requested data points not found in any layer. Derived from
    # the same per-data-point results so it stays consistent with the table.
    gap_analysis = [
        {
            "data_point": r["data_point"],
            "issue": "Not found in Gold, Silver, or Bronze — must be built or sourced.",
        }
        for r in summary_rows
        if r["result"] == "Not found"
    ]

    return {
        "use_case": use_case_name,
        "layer_summary": layer_summaries,
        "gap_analysis": gap_analysis,
        "summary_by_data_points": summary_rows,
        "tables_by_layer": {
            "gold":   _layer_tables(gold_matches),
            "silver": _layer_tables(silver_matches),
            "bronze": _layer_tables(bronze_matches),
        },
        "result_text": result_text,
    }


_LAYER_LABEL = {"gold": "Gold", "silver": "Silver", "bronze": "Bronze"}
_DOWNSTREAM = {"gold": "silver", "silver": "bronze", "bronze": None}


def _supplies_from_match(m: dict) -> list[dict]:
    """Data-point → column/logic → sample rows for an existing table (the
    'Supplies' table folded into each layer section)."""
    card = _build_table_card(m)
    out = []
    for r in card["rows"]:
        out.append({
            "data_point": r["data_point"],
            "column_or_logic": r["matched_column_or_logic"],
            "sample": r["sample_matched_value"] or r["sample_data_point_value"] or "",
        })
    return out


def _build_lineage(layers_plan: list, all_targets: list[str]) -> dict:
    """Nodes (one per table) + edges (Bronze→Silver→Gold data flow) + per-data-point
    flows (origin layer/column, or 'gap') for the lineage/cascade diagram."""
    node_ids = {"gold": [], "silver": [], "bronze": []}
    nodes = []
    for entry in layers_plan:
        lk = entry["layer"]
        for t in entry["tables"]:
            nid = f'{lk}:{t["name"]}'
            nodes.append({
                "id": nid, "layer": lk, "name": t["name"],
                "verdict": t["verdict"], "proposed": t["proposed"],
            })
            node_ids[lk].append(nid)

    edges = []
    for lower, upper in (("bronze", "silver"), ("silver", "gold")):
        for ln in node_ids[lower]:
            for un in node_ids[upper]:
                edges.append({"from": ln, "to": un})

    found_map: dict[str, tuple] = {}
    for entry in layers_plan:
        for t in entry["tables"]:
            for s in t.get("supplies", []):
                found_map.setdefault(s["data_point"], (entry["layer"], s["column_or_logic"]))

    flows = []
    for dp in all_targets:
        if dp in found_map:
            lyr, col = found_map[dp]
            flows.append({"data_point": dp, "status": "deliverable",
                          "origin_layer": lyr, "origin_column": col})
        else:
            flows.append({"data_point": dp, "status": "gap",
                          "origin_layer": None, "origin_column": None})
    return {"nodes": nodes, "edges": edges, "flows": flows}


def _build_layer_plan(
    use_case_name: str,
    gold_matches: list,
    silver_matches: list,
    bronze_matches: list,
    all_targets: list[str],
    remaining_unmatched: list[str],
    kpi_names: list[str],
    dim_names: list[str],
):
    """
    Build the merged, layer-by-layer plan that unifies the old verdict banners,
    summary tiles and expected-tables into one section per layer.

    Each layer lists its tables (existing reuse/extend + proposed build-new),
    each with verdict, description, rationale, cascade link (`derived_from`),
    a 'supplies' table (data point → column → sample) for existing tables, and
    proposed columns for build-new. Bronze never gets a build-new table; instead
    not-found points surface under `missing_from_source`.

    Returns (layers_plan, lineage, headline). This is the deterministic Option-A
    baseline; `kpi_derivation.enrich` refines the derivable-vs-missing split when
    the LLM flag is on.
    """
    matches_by_layer = {"gold": gold_matches, "silver": silver_matches, "bronze": bronze_matches}
    existing = {lk: [m for m in ms if _has_any_match(m)] for lk, ms in matches_by_layer.items()}
    layer_status = {lk: ("found" if existing[lk] else "not_found") for lk in matches_by_layer}

    proposals = {
        p["layer"]: p
        for p in _build_expected_tables(
            use_case_name,
            {lk: {"status": layer_status[lk]} for lk in matches_by_layer},
            kpi_names, dim_names,
        )
    }

    def rep_name(lk):
        if lk is None:
            return None
        if existing[lk]:
            return _table_short_name(existing[lk][0].get("name", ""))
        if lk in ("gold", "silver") and lk in proposals:
            return proposals[lk]["proposed_name"]
        return None

    layers_plan = []
    for lk in ("gold", "silver", "bronze"):
        tables = []
        for m in existing[lk]:
            tables.append({
                "name": _table_short_name(m.get("name", "")),
                "full_name": m.get("name", ""),
                # Bronze is source data — never modified — so it is always Reuse.
                "verdict": "reuse" if lk == "bronze" else m.get("status", "reuse"),
                "proposed": False,
                "description": m.get("description", ""),
                "rationale": m.get("recommended_action", ""),
                "derived_from": rep_name(_DOWNSTREAM[lk]),
                "supplies": _supplies_from_match(m),
                "proposed_columns": [],
                "delivers": [],
            })
        # build-new proposal only for Gold/Silver (never Bronze), and only when
        # that layer has no usable existing table.
        if lk in ("gold", "silver") and not existing[lk] and lk in proposals:
            p = proposals[lk]
            tables.append({
                "name": p["proposed_name"],
                "full_name": p["proposed_name"],
                "verdict": "build_new",
                "proposed": True,
                "description": p["purpose"],
                "rationale": f"No {_LAYER_LABEL[lk]} table exists yet to serve this use case.",
                "derived_from": rep_name(_DOWNSTREAM[lk]),
                "supplies": [],
                "proposed_columns": p["expected_columns"],
                "delivers": [],  # filled below from what's actually available in source
            })
        entry = {"layer": lk, "label": _LAYER_LABEL[lk], "status": layer_status[lk], "tables": tables}
        if lk == "bronze":
            entry["missing_from_source"] = []
            entry["missing_note"] = ""
        layers_plan.append(entry)

    # Which data points are actually available — matched to a real source column,
    # collected from the per-table 'supplies' — vs. not found anywhere.
    found_map: dict[str, str] = {}
    for entry in layers_plan:
        for t in entry["tables"]:
            for s in t.get("supplies", []):
                found_map.setdefault(s["data_point"], s["column_or_logic"])
    deliverable = [t for t in all_targets if t in found_map]
    gaps = [t for t in all_targets if t not in found_map]

    # Gold AND Silver build-new tables expose/conform the available data points
    # (they flow up from the lower layers). Deterministic Option-A derivation =
    # the source column/logic; the LLM refine step adds formulas and rescues gaps.
    delivers_list = [
        {"data_point": dp, "derivation": found_map[dp], "verified": True}
        for dp in deliverable
    ]
    for lk in ("gold", "silver"):
        entry = next((e for e in layers_plan if e["layer"] == lk), None)
        if not entry:
            continue
        for t in entry["tables"]:
            if t.get("proposed"):
                t["delivers"] = [dict(d) for d in delivers_list]

    # Bronze surfaces the not-found points as source gaps (Option-A: deterministic
    # 'not matched in source'; the LLM refine step may reclassify some as derivable).
    bronze_entry = next((e for e in layers_plan if e["layer"] == "bronze"), None)
    if bronze_entry is not None:
        bronze_entry["missing_from_source"] = [
            {"data_point": dp, "needs": None, "note": "Not found in any source table."}
            for dp in gaps
        ]
        bronze_entry["missing_note"] = (
            "Some of these may be derivable from existing columns — enable "
            "EXPECTED_TABLES_LLM=1 for a precise breakdown." if gaps else ""
        )

    lineage = _build_lineage(layers_plan, all_targets)
    headline = (
        f"Of {len(all_targets)} requested data point(s): "
        f"{len(deliverable)} available in existing data, "
        f"{len(gaps)} not found in source (need new data or a new build)."
    )
    return layers_plan, lineage, headline


def run(combined_input: dict, context: dict = {}) -> dict:
    """
    Run the full discovery cascade in Python.

    Args:
        combined_input: Merged dict from requirement_understanding + use_case_classification.
        context:        Unused (kept for interface compatibility).

    Returns:
        DiscoveryResult dict with:
          gold_matches / silver_matches / bronze_matches
          conflicts / cascade_trace / summary / architecture_diagram
    """
    plugin = CatalogPlugin()

    # ── Primary discovery ─────────────────────────────────────────────────────
    # match_requirements_to_catalog uses spec-authoritative confidence scores
    # and returns column-level KPI→column mappings per table per layer.
    matched = json.loads(plugin.match_requirements_to_catalog(json.dumps(combined_input)))

    gold_raw   = matched.get("gold",   [])
    silver_raw = matched.get("silver", [])
    bronze_raw = matched.get("bronze", [])

    # ── Apply close-call flagging per layer ───────────────────────────────────
    def flag_close_calls(layer_matches: list) -> list:
        if not layer_matches:
            return layer_matches
        return json.loads(plugin.check_close_calls(json.dumps(layer_matches)))

    gold_matches   = flag_close_calls(gold_raw)
    silver_matches = flag_close_calls(silver_raw)
    bronze_matches = flag_close_calls(bronze_raw)

    # ── Detect cross-layer conflicts ──────────────────────────────────────────
    conflicts = json.loads(plugin.detect_conflicts(json.dumps({
        "gold":   gold_matches,
        "silver": silver_matches,
        "bronze": bronze_matches,
    })))

    # ── Cascade trace ─────────────────────────────────────────────────────────
    gold_resolved   = _resolve_fields(gold_matches)
    silver_resolved = _resolve_fields(silver_matches)
    bronze_resolved = _resolve_fields(bronze_matches)

    # KPIs / dimensions that are NEVER matched at reuse level across all layers
    all_resolved = set(f.lower() for f in gold_resolved + silver_resolved + bronze_resolved)
    # Collect all active targets from the input for unmatched reporting.
    # Skill v2.8+ stores KPIs under `data_points` (kind == "kpi"); fall back to
    # it when the legacy `kpis`/`final_kpi_list` keys are absent, so the KPIs
    # appear in search_criteria / summary_by_data_points / gap_analysis.
    reqs = combined_input.get("requirements", combined_input)
    kpi_items = reqs.get("kpis") or reqs.get("final_kpi_list") or []
    if not kpi_items:
        kpi_items = [
            dp for dp in reqs.get("data_points", [])
            if isinstance(dp, dict) and dp.get("kind") == "kpi"
        ]
    all_targets = (
        [kpi.get("kpi_name") or kpi.get("kpi") or kpi.get("name", "")
         for kpi in kpi_items]
        + [dim.get("dimension", "") for dim in reqs.get("granularity", [])]
    )
    # Collapse overlapping/bare-token noise (e.g. "campaign" alongside
    # "Campaign Name") so it doesn't render as a phantom NOT FOUND chip.
    # Applied here, before every downstream use of all_targets (search_criteria,
    # remaining_unmatched, residual cascade, verdicts), so all stay consistent.
    all_targets = _dedup_targets(all_targets)
    remaining_unmatched = [t for t in all_targets if t and t.lower() not in all_resolved]

    # Fields forwarded from gold to silver (those not resolved at gold)
    gold_resolved_set = {f.lower() for f in gold_resolved}
    residual_to_silver = [t for t in all_targets if t and t.lower() not in gold_resolved_set]

    silver_resolved_set = gold_resolved_set | {f.lower() for f in silver_resolved}
    residual_to_bronze  = [t for t in all_targets if t and t.lower() not in silver_resolved_set]

    # ── Summary ───────────────────────────────────────────────────────────────
    all_matches     = gold_matches + silver_matches + bronze_matches
    reuse_count     = sum(1 for m in all_matches if m.get("status") == "reuse")
    extend_count    = sum(1 for m in all_matches if m.get("status") == "extend")
    build_new_count = sum(1 for m in all_matches if m.get("status") == "build_new")
    close_calls     = sum(1 for m in all_matches if m.get("close_call"))

    discovery_result: dict = {
        "session_id":       combined_input.get("session_id", ""),
        # Carry the user-stated domain through to the DDI workstream. Discovery
        # itself doesn't use it, but gold-er/gold-final must honour the user's
        # domain rather than re-inferring it from matched table names.
        "domain":           reqs.get("domain") or combined_input.get("domain", ""),
        # Carry the use-case name + classification through as well (parallel to
        # `domain`). These arrive spread into combined_input from the requirement
        # and use-case-classification agents; surfacing them lets downstream
        # consumers (DDI, frontend) use the authoritative values instead of
        # re-inferring the use case or schema pattern.
        "use_case_name":    reqs.get("use_case_name") or combined_input.get("use_case_name", ""),
        "classification": {
            "use_case_type":         combined_input.get("use_case_type", ""),
            "schema_design_pattern": combined_input.get("schema_design_pattern", ""),
            "confidence":            combined_input.get("confidence"),
            "rationale":             combined_input.get("rationale", ""),
        },
        "threshold_config": combined_input.get("threshold_config", {
            "reuse_minimum": 0.80, "extend_minimum": 0.50,
        }),
        "gold_matches":    gold_matches,
        "silver_matches":  silver_matches,
        "bronze_matches":  bronze_matches,
        "conflicts":       conflicts,
        "cascade_trace": {
            "gold": {
                "assets_searched":  len(gold_matches),
                "full_matches":     sum(1 for m in gold_matches if m.get("status") == "reuse"),
                "partial_matches":  sum(1 for m in gold_matches if m.get("status") == "extend"),
                "build_new":        sum(1 for m in gold_matches if m.get("status") == "build_new"),
                "fields_resolved":  gold_resolved,
                "residual_to_silver": residual_to_silver,
            },
            "silver": {
                "assets_searched":  len(silver_matches),
                "full_matches":     sum(1 for m in silver_matches if m.get("status") == "reuse"),
                "partial_matches":  sum(1 for m in silver_matches if m.get("status") == "extend"),
                "build_new":        sum(1 for m in silver_matches if m.get("status") == "build_new"),
                "fields_resolved":  silver_resolved,
                "residual_to_bronze": residual_to_bronze,
            },
            "bronze": {
                "assets_searched":  len(bronze_matches),
                "full_matches":     sum(1 for m in bronze_matches if m.get("status") == "reuse"),
                "partial_matches":  sum(1 for m in bronze_matches if m.get("status") == "extend"),
                "build_new":        sum(1 for m in bronze_matches if m.get("status") == "build_new"),
                "fields_resolved":  bronze_resolved,
                "remaining_unmatched": remaining_unmatched,
            },
        },
        "summary": {
            "total_matches":        len(all_matches),
            "reuse_count":          reuse_count,
            "extend_count":         extend_count,
            "build_new_count":      build_new_count,
            "search_backends_used": [
                "BigQuery (POC spec examples via utility_catalog.json)",
                "Foundry IQ (POC semantic proxy)",
            ],
            "semantic_aliases_resolved": 0,
            "close_calls_flagged":  close_calls,
            "layers_searched":      ["gold", "silver", "bronze"],
            "catalogs_searched":    ["bq_project.gold", "bq_project.silver", "bq_project.bronze"],
        },
    }

    # Architecture diagram spec
    discovery_result["architecture_diagram"] = json.loads(
        plugin.build_architecture_diagram_spec(json.dumps(discovery_result))
    )

    # Structured view payload consumed by the frontend DiscoveryResultCard.
    use_case_name = (
        combined_input.get("use_case_name")
        or combined_input.get("requirements", {}).get("use_case_name")
        or reqs.get("use_case_name", "")
    )
    discovery_result["discovery_view"] = _build_discovery_view(
        use_case_name,
        gold_matches,
        silver_matches,
        bronze_matches,
        [t for t in all_targets if t],
    )

    # Surface the user-stated domain on the view payload too, so the frontend
    # DiscoveryResultCard can display it (mirrors the top-level discovery_result
    # "domain" used for the DDI hand-off).
    discovery_result["discovery_view"]["domain"] = discovery_result["domain"]
    # Same for the use-case name + classification (discovery_view already carries
    # `use_case`; add the classification block for completeness).
    discovery_result["discovery_view"]["classification"] = discovery_result["classification"]

    # Verdict banner (Reuse / Extend / Build-New) + the data points searched.
    discovery_result["discovery_view"]["search_criteria"] = [t for t in all_targets if t]
    discovery_result["discovery_view"]["verdicts"] = _build_verdicts(
        gold_matches, silver_matches, bronze_matches, remaining_unmatched,
    )

    # Expected-but-missing tables — for each layer with no usable table, propose
    # the table (guessed name + columns) that would fulfil the requirements.
    discovery_result["discovery_view"]["expected_tables"] = _build_expected_tables(
        use_case_name,
        discovery_result["discovery_view"]["layer_summary"],
        [kpi.get("kpi_name") or kpi.get("kpi") or kpi.get("name", "") for kpi in kpi_items],
        [dim.get("dimension", "") for dim in reqs.get("granularity", [])],
    )

    # Merged layer-by-layer plan + lineage/cascade diagram + plain-language headline.
    kpi_names = [kpi.get("kpi_name") or kpi.get("kpi") or kpi.get("name", "") for kpi in kpi_items]
    dim_names = [dim.get("dimension", "") for dim in reqs.get("granularity", [])]
    layers_plan, lineage, headline = _build_layer_plan(
        use_case_name, gold_matches, silver_matches, bronze_matches,
        [t for t in all_targets if t], remaining_unmatched, kpi_names, dim_names,
    )
    discovery_result["discovery_view"]["layers_plan"] = layers_plan
    discovery_result["discovery_view"]["lineage"] = lineage
    discovery_result["discovery_view"]["headline"] = headline

    return discovery_result
