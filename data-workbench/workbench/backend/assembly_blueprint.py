"""Compile a Product Assembly into an enriched modernization ``Blueprint``.

The bridge from the interactive "Work this product" workspace to the existing intake
scaffold saga. The assembly overlay (per-attribute decisions + the source cluster plan)
is compiled — **structurally, with no LLM** — into a ``ModernizationBlueprint`` that
carries the detail the classic parser-driven path leaves empty:

- **source_aligned** — one candidate **per source cluster** (each a dpe-sa product to
  build first), with ``datasets[].columns[]`` = the tables + the columns the aggregate
  actually consumes.
- **consumer_aligned** — the aggregate (dpe-cf), carrying a **native ODCS** draft: the
  curated (non-excluded) attributes as output columns, each with a prose *source-intent*
  hint (which estate column / derivation / cast) so the engineer's later mapping pass is
  strongly guided. ``productKind='aggregate'``.
- **dependencies** — aggregate → each source cluster (bound later, once the sources
  publish, via ``IntakePendingDependency``).

Honest partial pre-fill: the aggregate ODCS is schema + intent, NOT wired source-column
mappings (the SA ``:DProdColumn`` don't exist until the sources are built).

Pure (graph-free) so it's unit-testable; the endpoint gathers the inputs.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from . import feasibility_spec as fspec
from . import intake_blueprint as ibp


def _slug(text: str, fallback: str = "x") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s or fallback


def _safe_name(text: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", (text or "").strip().lower()).strip("_")
    return s or "product"


def _cf(value: Any, why: str = "") -> dict[str, Any]:
    """A high-confidence, pre-confirmed ConfidenceField — the assembly's decisions are
    explicit, not a graded parse guess."""
    return {"value": value, "confidence": "high", "why": why, "review_state": "confirmed"}


def _final_attributes(
    spec: fspec.FeasibilitySpec, assignment: list[dict], decisions: dict[str, dict],
) -> list[dict[str, Any]]:
    """Apply the plan overlay to each spec attribute → the curated column list for the
    aggregate. Excluded attrs drop; the rest carry a status + a source-intent hint."""
    by_aid = {r.get("attr_id"): r for r in (assignment or [])}
    out: list[dict[str, Any]] = []
    for i, attr in enumerate(spec.attributes):
        aid = f"#{i}:{attr.name}"
        d = decisions.get(aid) or {}
        dec = d.get("decision")
        if dec == "exclude":
            continue
        row = by_aid.get(aid)
        e: dict[str, Any] = {
            "name": attr.name, "type": attr.type or "string", "required": attr.required,
            "is_key": attr.is_key, "description": attr.description or attr.concept or attr.name,
            "status": "gap", "note": "",
        }
        if dec == "defer":
            e["status"] = "deferred"
            e["note"] = "deferred — map to a source later"
        elif dec == "derive":
            cols = d.get("source_cols") or []
            e["status"] = "derived"
            e["note"] = f"derive from {', '.join(cols)}" + (f": {d['prose_hint']}" if d.get("prose_hint") else "")
        elif dec == "remap":
            e["status"] = "mapped"
            e["table"] = d.get("table_ref", "")
            e["column"] = d.get("column_ref", "")
            e["note"] = f"source: {e['table']}.{e['column']}"
        elif row and row.get("match_kind") == "composite":
            # A derived/composite match ("first_name + last_name") is NOT a directly-
            # sourced column — carry it as a derivation hint, and don't inject a fake
            # source column ("a + b") into a source product's dataset.
            comp = row.get("composite") or {}
            parts = [c.get("column") for c in comp.get("components", []) if c.get("column")]
            e["status"] = "derived"
            e["note"] = (f"derive ({comp.get('kind', 'composite')}) from " + ", ".join(parts)
                         + (f" on {comp.get('table')}" if comp.get("table") else ""))
        elif row:  # accepted deterministic / matcher direct pick
            e["status"] = "mapped"
            e["table"] = row.get("dataset_table", "")
            e["column"] = row.get("column", "")
            e["note"] = f"source: {e['table']}.{e['column']}"
            ch = row.get("cast_hint") or {}
            if ch.get("needed"):
                e["note"] += f" [cast {ch['from']}→{ch['to']}]"
        else:
            e["note"] = "unmapped — needs a source"
        out.append(e)
    return out


def _aggregate_odcs(
    spec: fspec.FeasibilitySpec, spec_name: str, domain: str, final: list[dict],
    owner_email: str = "",
) -> dict[str, Any]:
    """A minimal, valid ODCS v3.1 draft for the aggregate — the curated attributes as
    one output dataset, each property carrying its source-intent hint in the
    description. ``productKind='aggregate'`` (honoured by ``_resolve_product_kind``)."""
    props = []
    for e in final:
        desc = (e["description"] or "").strip()
        if e.get("note"):
            desc = (desc + f" [{e['note']}]").strip()
        props.append({
            "name": e["name"], "physicalName": _safe_name(e["name"]),
            "logicalType": (e["type"] or "string").lower(),
            "description": desc,
            "primaryKey": bool(e["is_key"]), "required": bool(e["required"]),
        })
    ds_name = _safe_name(spec_name)
    owners = [{"username": owner_email, "email": owner_email, "role": "Data Product Owner",
               "name": owner_email.split("@")[0] if "@" in owner_email else owner_email}] if owner_email else []
    return {
        "apiVersion": "v3.1.0", "kind": "DataContract",
        "name": spec_name, "version": "1.0.0", "status": "draft",
        "domain": (domain or "").lower(), "dataProduct": spec_name,
        "productKind": "aggregate",
        "description": spec.description or f"Aggregate product '{spec_name}' composed from source-aligned products.",
        "purpose": spec.description or f"Fit-for-purpose aggregate over the {domain} domain.",
        "owners": owners,
        "schema": [{
            "name": ds_name, "physicalName": ds_name, "physicalType": "table",
            "description": f"Output of the '{spec_name}' aggregate.",
            "properties": props,
        }],
        "customProperties": [{"property": "productKind", "value": "aggregate"}],
    }


def build_modernization_blueprint(
    spec: fspec.FeasibilitySpec,
    assignment: list[dict],
    decisions: dict[str, dict],
    clusters: list[dict],
    *,
    spec_name: str = "",
    domain: str = "",
    owner_email: str = "",
    provenance_note: str = "",
) -> dict[str, Any]:
    """Compile the assembly into a validated ``ModernizationBlueprint`` dict (fail-closed
    via :func:`intake_blueprint.parse_blueprint`)."""
    spec_name = spec_name or spec.name
    domain = domain or spec.domain
    final = _final_attributes(spec, assignment, decisions)

    # Which columns the aggregate consumes from each cluster's tables.
    table_to_cluster: dict[str, str] = {}
    for c in clusters:
        for t in c.get("table_refs", []):
            table_to_cluster[t] = c.get("name", "")
    consumed: dict[str, dict[str, set]] = {}  # cluster -> table -> {cols}
    col_type: dict[tuple[str, str], str] = {}
    for e in final:
        if e.get("status") == "mapped" and e.get("table") and e.get("column"):
            cl = table_to_cluster.get(e["table"])
            if cl:
                consumed.setdefault(cl, {}).setdefault(e["table"], set()).add(e["column"])
                col_type[(e["table"], e["column"])] = e["type"]

    # source_aligned: one candidate per cluster the aggregate actually draws from.
    # A source-aligned product IS the WHOLE cluster — every table in the cluster's
    # `table_refs`, not just the tables the aggregate happens to map a column from.
    # The consumed tables carry the columns the aggregate needs (source-intent hints);
    # the rest of the cluster's tables are listed with `columns: []` so the engineer
    # discovers the full source at build time (a 5-table Customers cluster the aggregate
    # draws two columns from is a 5-table product, not a 1-table one).
    # De-duped by (name, FULL table set) so a duplicate schema still in scope (e.g.
    # tpcds_sf1 + tpcds_sf1000 both selected) doesn't produce two identical source
    # products — the honest fix is to drop the duplicate schema, but be robust here.
    source_aligned: list[dict[str, Any]] = []
    dep_ids: list[str] = []
    seen_sig: set[tuple] = set()
    for i, c in enumerate(clusters):
        name = c.get("name", "") or f"Source {i}"
        consumed_tables = consumed.get(name) or {}
        if not consumed_tables:
            continue  # cluster not consumed by this aggregate → no SA product needed
        all_tables = sorted(c.get("table_refs", []))
        if not all_tables:  # robustness: a cluster with no recorded table_refs
            all_tables = sorted(consumed_tables.keys())
        sig = (name, tuple(all_tables))
        if sig in seen_sig:
            continue
        seen_sig.add(sig)
        cid = f"src-{i}-{_slug(name, str(i))}"
        dep_ids.append(cid)
        datasets = []
        for tbl in all_tables:
            cols = [{"name": _cf(col), "data_type": _cf(col_type.get((tbl, col), ""))}
                    for col in sorted(consumed_tables.get(tbl, set()))]
            datasets.append({"name": _cf(tbl), "columns": cols,
                             "review_state": "confirmed"})
        source_aligned.append({
            "candidate_id": cid,
            "name": _cf(name, "assembly source cluster"),
            "domain": _cf(domain, "spec domain"),
            "product_idea": (f"Source-align the '{name}' tables ({', '.join(all_tables)}) "
                             f"to support the '{spec_name}' product. The '{spec_name}' aggregate "
                             f"consumes: " + "; ".join(f"{t}({', '.join(sorted(cs))})"
                                                       for t, cs in sorted(consumed_tables.items())) + "."),
            "datasets": datasets,
            "confidence": "high", "review_state": "confirmed",
        })

    # consumer_aligned: the aggregate, with a native ODCS draft.
    con_id = f"con-0-{_slug(spec.spec_id, 'product')}"
    consumer_aligned = [{
        "candidate_id": con_id,
        "name": _cf(spec_name, "reference spec"),
        "domain": _cf(domain, "spec domain"),
        "purpose": spec.description or f"Aggregate '{spec_name}'.",
        "odcs": _aggregate_odcs(spec, spec_name, domain, final, owner_email),
        "confidence": "high", "review_state": "confirmed",
    }]
    dependencies = [{
        "dependency_id": f"dep-{i}", "from_candidate_id": con_id,
        "to_candidate_id": sid, "confidence": "high", "review_state": "confirmed",
    } for i, sid in enumerate(dep_ids)]

    n_mapped = sum(1 for e in final if e.get("status") == "mapped")
    n_derived = sum(1 for e in final if e.get("status") == "derived")
    n_gap = sum(1 for e in final if e.get("status") == "gap")
    # A FORWARD-looking summary (the intake-review SUMMARY): what this portfolio IS and
    # what to do next — NOT where it came from (that provenance rides raw_payload_json +
    # external_ref). `provenance_note` stays a param for back-compat but is no longer fed
    # the feasibility run/spec string.
    src_names = [p["name"]["value"] for p in source_aligned]  # ConfidenceField-wrapped
    portfolio = (", ".join(f"'{n}'" for n in src_names) + f" → the '{spec_name}' aggregate"
                 if src_names else f"the '{spec_name}' aggregate (no source clusters)")
    rationale = (
        (provenance_note + " ") if provenance_note else ""
    ) + (
        f"Product Assembly portfolio: {len(source_aligned)} source-aligned product(s) "
        f"({portfolio}). Coverage: {n_mapped} attribute(s) mapped, {n_derived} derived, "
        f"{n_gap} still a gap. Next: build the source-aligned products first, then "
        f"complete the '{spec_name}' aggregate in the wizard."
    )
    blueprint = {
        "scenario": "modernization", "overall_confidence": "high",
        "source_aligned": source_aligned, "consumer_aligned": consumer_aligned,
        "dependencies": dependencies, "rationale": rationale,
    }
    model = ibp.parse_blueprint(blueprint)  # fail-closed
    ibp.normalize_ids(model)
    return model.model_dump(mode="json")
