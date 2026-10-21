"""Estate discovery — the estate, its lineage, and the hand-off to intake.

ONE router for the whole Discovery surface. "Discovery" is the neutral frame:
exploring the estate is what leads to a disposition, and the disposition may be
migrate, modernize, retire or remain. (This file previously lived as a separate
`modernization.py`, which named one outcome and so mis-framed the other three.)

What it serves:
  - the discovery table (`GET /{id}/discovery`) and the object-grain estate
    inventory + lineage the graph draws (`/discovery/inventory`)
  - reference data products + schema-DNA comparison (`/discovery/compare*`,
    `/discovery/reference-data-products`)
  - per-object detail: legacy source (`/discovery/{id}/source`) and retirement
    impact (`/discovery/{id}/retirement-impact`)
  - the hand-off: `POST /discovery/{row}/to-intake` stages a migrate or
    modernize row into the intake pipeline. Retire is analysis-only and remain
    has no task, so neither routes anywhere.

File-backed fixture data (playbook/) — staged demo content, not a live workload.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from ..config import BASE_DIR, BASE_PROJECT_DIR
from ..database import get_session
from ..models import Project
from .. import estate_ingest, schema_dna
from ..neo4j_client import neo4j_session
from .intake import ContentPart, IntakeEnvelope, submit_intake

router = APIRouter(prefix="/api/projects", tags=["discovery"])


# The demo estate: one object per physical database object, with `reads[]`
# lineage. 207 objects — 29 tagged `scope: core` (the curated demo spine) and
# 178 `scope: discovered` (the surrounding halo). The frontend uses `scope` to
# distinguish them, so this single file covers both the small and full demo.
ESTATE_PATH = BASE_DIR / "playbook" / "discovery" / "estate.yaml"

# Canonical reference data products (governed target schemas) now come from the
# Blueprint Library (published) via `_library_reference_models`; the shared
# `reference_data_products.yaml` fixture was retired.

# All demo sample source code, inlined in ONE deletable file. An estate object
# opts in with `sample_source: <key>`; see the file's own header.
SAMPLE_SOURCES_PATH = BASE_DIR / "playbook" / "discovery" / "sample_sources.yaml"

_LIBRARY_KIND = {
    "source": "source-aligned",
    "aggregate": "aggregated",
    "consumer": "consumer-aligned",
}


def _library_reference_models(session) -> list[dict[str, Any]]:
    """Project every PUBLISHED Blueprint-Library template into the Pulse
    ``models[].attributes[]`` shape ``_load_reference_models`` returns. The
    Library is the single source of truth; ``reference_data_products.yaml`` is
    retained only as a fallback for an un-seeded instance."""
    try:
        from .. import template_store
        rows = template_store.list_templates_raw(session)
    except Exception:
        return []
    models: list[dict[str, Any]] = []
    for r in rows:
        if r.get("status") != "published":
            continue
        spec = template_store._read_template_from_graph(r["id"], session)
        if not spec:
            continue
        attributes: list[dict[str, Any]] = []
        for ds in (spec.get("schema") or []):
            for p in (ds.get("properties") or []):
                nm = p.get("name") or p.get("physicalName")
                if not nm:
                    continue
                attributes.append({
                    "name": str(nm),
                    "type": p.get("physicalType") or p.get("logicalType") or "",
                    "concept": p.get("logicalName") or "",
                })
        if not attributes:
            continue
        feas = (spec.get("customProperties") or {}).get("feasibility") or {}
        composed = list((feas.get("composition") or {}).get("composed_of") or [])
        models.append({
            "id": spec.get("sourceSpecId") or r["id"],
            "name": spec.get("name", ""),
            "domain": spec.get("domain", ""),
            "description": spec.get("description", ""),
            "kind": _LIBRARY_KIND.get((spec.get("productKind") or "").lower(), "source-aligned"),
            "attributes": attributes,
            "composed_of": composed,
        })
    return models


def _load_reference_models(project_code: str | None = None, session=None) -> list[dict[str, Any]]:
    # A per-project override (written by an estate-import bundle) wins; otherwise
    # the Blueprint Library (published) is the single source of truth. (The shared
    # `playbook/discovery/reference_data_products.yaml` fixture was retired.)
    if project_code:
        pp = BASE_PROJECT_DIR / project_code / "modernization" / "reference_data_products.yaml"
        if pp.exists():
            with pp.open("r", encoding="utf-8") as fh:
                return (yaml.safe_load(fh) or {}).get("models") or []
    if session is not None:
        return _library_reference_models(session)
    return []


def _reference_model_by_id(model_id: str, project_code: str | None = None,
                           session=None) -> dict[str, Any] | None:
    for m in _load_reference_models(project_code, session=session):
        if str(m.get("id")) == str(model_id):
            return m
    return None


def _load_estate(project_code: str | None = None) -> dict[str, Any]:
    """The estate for a project.

    Resolution: the project's IMPORTED estate (projects/{code}/modernization/
    estate.yaml, written by the estate-import API) → the shared demo fixture.
    Every object carries `scope` ('core' | 'discovered'); imported estates
    predate the tag, so default to 'core'.
    """
    doc: dict[str, Any] | None = None
    if project_code:
        imported = BASE_PROJECT_DIR / project_code / "modernization" / "estate.yaml"
        if imported.exists():
            try:
                with imported.open("r", encoding="utf-8") as fh:
                    doc = yaml.safe_load(fh) or None
            except (OSError, yaml.YAMLError):
                doc = None
    if doc is None and ESTATE_PATH.exists():
        with ESTATE_PATH.open("r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or None
    doc = doc or {"scenario": "", "description": "", "objects": []}
    for o in doc.get("objects") or []:
        o.setdefault("scope", "core")
    return doc


def _inventory_object(object_id: str, project_code: str | None = None) -> dict[str, Any] | None:
    # Resolve against the FULL estate so every row the table shows resolves,
    # including discovered halo objects that carry no migration_approach.
    return next((o for o in _load_estate(project_code).get("objects") or [] if o.get("id") == object_id), None)


def _estate_owner_code(project: Project, session: Session) -> str | None:
    """The project_code whose estate an object belongs to.

    Every project owns its own estate. (The per-row child projects that used to
    borrow their parent's estate were retired when discovery moved to the intake
    bridge.) `session` is kept for signature stability.
    """
    return project.project_code


def _load_sample_sources() -> dict[str, dict[str, Any]]:
    """The inlined demo sample code. Missing file → no samples (graceful)."""
    if not SAMPLE_SOURCES_PATH.exists():
        return {}
    with SAMPLE_SOURCES_PATH.open("r", encoding="utf-8") as fh:
        return (yaml.safe_load(fh) or {}).get("samples") or {}


def _resolve_sample_source(sample_source: str, owner_code: str | None = None) -> Path:
    """Resolve an IMPORTED estate's project-relative `sample_source` path.

    Only used when the value looks like a path (contains "/"). Demo fixture
    objects instead carry a KEY into sample_sources.yaml — see
    `_load_sample_sources`.
    """
    if owner_code:
        pp = BASE_PROJECT_DIR / owner_code / sample_source
        if pp.exists():
            return pp
    return BASE_DIR / "playbook" / sample_source


# Slicer dimensions surfaced to the filter bar. Booleans map to labelled facets.
_FACET_KEYS = ["domain", "application", "instance", "recommendation", "compatibility"]


def _inventory_facets(objects: list[dict[str, Any]]) -> dict[str, list[str]]:
    facets: dict[str, list[str]] = {}
    for key in _FACET_KEYS:
        facets[key] = sorted({str(o.get(key)) for o in objects if o.get(key) is not None})
    facets["active"] = ["Active", "Inactive"]
    facets["phi_pii"] = ["Yes", "No"]
    return facets


_PUBLISHED_SOURCE_PRODUCTS_QUERY = """
MATCH (dc:DataContract)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
WHERE coalesce(dc.productKind,'') = 'source'
  AND coalesce(dc.currentLifecycleState,'') IN ['published','superseded','approved']
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)-[:DPROD_OUTPUT_DATASET]->(ds:DProdOutputDataset)
OPTIONAL MATCH (ds)-[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
RETURN dp.uri AS uri, dp.name AS name, coalesce(dc.domain,'') AS domain,
       collect(DISTINCT ds.physicalName) AS tables, count(DISTINCT pc) AS column_count
"""


def _published_source_products(project: Project) -> list[dict[str, Any]]:
    """Read published source-aligned :DProdDataProducts + their output-dataset
    physical table names from the graph. Non-fatal: returns [] on any error so
    the DAG still renders the estate."""
    try:
        with neo4j_session(project.neo4j_host, project.neo4j_port, project.neo4j_user,
                           project.neo4j_password, project.neo4j_database) as ns:
            return [dict(r) for r in ns.run(_PUBLISHED_SOURCE_PRODUCTS_QUERY)]
    except Exception:
        return []


def _product_nodes(project: Project, objects: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[list[str]]]:
    """Build DAG nodes for published source products, each matched to its source
    table (by output-dataset physicalName == estate object_name) with a
    table→product edge. Returns (product_nodes, product_edges)."""
    # TEMP (demo): source products are suppressed from the DAG for now.
    # Remove this early return to restore the table→product copy nodes.
    return [], []
    by_object_name: dict[str, dict[str, Any]] = {
        o["object_name"]: o for o in objects if o.get("type") == "table" and o.get("object_name")
    }
    nodes: list[dict[str, Any]] = []
    edges: list[list[str]] = []
    for p in _published_source_products(project):
        matched = [by_object_name[t] for t in (p.get("tables") or []) if t in by_object_name]
        if not matched:
            continue
        anchor = matched[0]
        uri = p.get("uri")
        nodes.append({
            "id": uri, "type": "product", "name": p.get("name") or uri,
            "object_name": p.get("name") or uri, "domain": p.get("domain") or "",
            "application": "Data Products", "instance": "Databricks", "database": "Unity Catalog",
            "schema": p.get("domain") or "", "object_type": "DATA PRODUCT",
            "size_gb": 0.0, "active": True, "phi_pii": False,
            "recommendation": "Published", "recommendation_reason": "—",
            "target": "Databricks · Unity Catalog product", "complexity": "—",
            "disposition": None,  # products have no 4-way disposition (UI guards on type)
            "reads": [m["id"] for m in matched],
            "team": "Governed", "status": "fresh",
            "metric": f"Data product · {p.get('column_count', 0)} cols · published",
            # x=400 is the vacated "d1" lane (source tables end at x=336, this
            # column starts at x=700 post-offset) — sits right after the
            # source tables with no overlap; d1/d2/out shifted +360 to make
            # room (see gen_inventory.py's COLS comment).
            "x": 400, "y": anchor.get("y", 0),
            "panel_rows": [
                ["Kind", "Source-aligned data product"], ["Domain", p.get("domain") or ""],
                ["Columns", str(p.get("column_count", 0))], ["Status", "Published"],
                ["Built from", ", ".join(f"{m.get('schema')}.{m.get('object_name')}" for m in matched)],
            ],
        })
        for m in matched:
            edges.append([m["id"], uri])
    return nodes, edges


@router.get("/{project_id}/discovery/inventory")
def get_estate_inventory(project_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Object-grain estate inventory + lineage edges + slicer facets, with live
    per object. Consumed by BOTH the discovery table and the lineage DAG so the
    two surfaces always reflect the same data.

    A migrate/modernize object is actionable — its row/node routes to the intake
    bridge. Retire is analysis-only and remain has no task, so neither routes
    anywhere.
    """
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    # Full unified estate (imported per-project estate when present, else the
    # demo fixture). Each object carries `scope`, which the frontend uses to
    # pick the unfocused working set.
    inv = _load_estate(project.project_code)
    objects = inv.get("objects") or []

    enriched: list[dict[str, Any]] = []
    edges: list[list[str]] = []
    for o in objects:
        r = dict(o)
        enriched.append(r)
        for src in o.get("reads") or []:
            edges.append([src, o["id"]])

    # Published source-aligned products, shown on the DAG (not the table) as
    # green product nodes hanging off their source table.
    product_nodes, product_edges = _product_nodes(project, objects)

    # A source product is a 1:1 source-aligned COPY of its table — it hangs off
    # the raw table (table → product) as a sink and does NOT sit in the
    # consumption path. Downstream pipelines keep reading the raw tables (the
    # fragmented current-state reality); we only add the table→product copy
    # edges. Products never feed other objects.
    edges.extend(product_edges)

    return {
        "scenario": inv.get("scenario", ""),
        "description": inv.get("description", ""),
        "domain": project.domain,
        "objects": enriched,
        "product_nodes": product_nodes,
        "edges": edges,
        "facets": _inventory_facets(objects),
        # Provenance discriminator: lets the UI show the "sample estate" banner
        # + import CTA over fixture data without ever making the view empty.
        "source": "imported" if estate_ingest.estate_path(project.project_code).exists() else "fixture",
    }


def _retirement_facts(obj: dict[str, Any], upstream: list[dict],
                      downstream: list[dict], all_objs: list[dict]) -> dict[str, Any]:
    """Deterministic dependency blast-radius for retiring `obj`. Ground truth —
    the skill narrates these, never recomputes them. Splits downstream consumers
    into orphaned (this is their ONLY feeder → they break) vs. served (another
    feeder survives), and flags upstream feeders that fed ONLY this object (now
    dead weight). The verdict is DERIVED here, not by the LLM."""
    oid = obj.get("id")
    # feeders[x] = ids that x reads (its upstream); used to test "any OTHER feeder".
    feeders: dict[str, list[str]] = {o.get("id"): list(o.get("reads") or []) for o in all_objs}
    by_id = {o.get("id"): o for o in all_objs}

    orphaned: list[dict[str, Any]] = []
    served: list[dict[str, Any]] = []
    for d in downstream:
        did = d.get("id")
        others = [f for f in feeders.get(did, []) if f and f != oid]
        row = {"id": did, "name": d.get("name") or did, "type": d.get("type")}
        if others:
            row["survivors"] = [by_id.get(f, {}).get("name") or f for f in others]
            served.append(row)
        else:
            orphaned.append(row)

    # Upstream feeders that fed ONLY this object → they now feed nothing.
    dead_upstream: list[dict[str, Any]] = []
    for u in upstream:
        uid = u.get("id")
        consumers = [o.get("id") for o in all_objs if uid in (o.get("reads") or [])]
        if consumers and all(c == oid for c in consumers):
            dead_upstream.append({"id": uid, "name": u.get("name") or uid, "type": u.get("type")})

    active = obj.get("active") is not False  # default-active unless explicitly False
    if not orphaned:
        verdict = "safe"          # nothing loses its only source.
    elif not active:
        verdict = "caution"       # breaks orphans, but the node is already dormant.
    else:
        verdict = "blocked"       # live node whose removal breaks a sole-fed consumer.
    return {
        "verdict": verdict,
        "active": active,
        "orphaned_consumers": orphaned,
        "served_consumers": served,
        "dead_upstream": dead_upstream,
    }


def _heuristic_retirement_impact(obj: dict[str, Any], facts: dict[str, Any],
                                 comment: str) -> dict[str, Any]:
    """Deterministic impact narrative — the fallback when the
    data-retirement-impact-analyzer skill isn't installed. Grounded in the same
    facts the skill would narrate, so the panel is actionable either way."""
    name = obj.get("name") or obj.get("id") or "this object"
    orphaned = facts["orphaned_consumers"]
    dead = facts["dead_upstream"]
    c = (comment or "").strip()
    wants_snapshot = any(k in c.lower() for k in ("snapshot", "archive", "retain", "retention", "audit", "history", "backup"))
    orphan_names = ", ".join(o["name"] for o in orphaned[:4]) + (f", +{len(orphaned) - 4} more" if len(orphaned) > 4 else "")
    dormant = "" if facts["active"] else " It is already dormant, so there is no live traffic to drain."

    if facts["verdict"] == "safe":
        summary = (f"Safe to retire {name} — "
                   + ("no downstream consumers depend on it." if not facts["served_consumers"]
                      else f"all {len(facts['served_consumers'])} downstream consumer(s) keep another feeder.")
                   + dormant)
    else:
        summary = (f"Retiring {name} orphans {len(orphaned)} downstream consumer(s) "
                   f"({orphan_names}) that have no other feeder — repoint them first." + dormant)

    steps: list[str] = []
    if orphaned:
        steps.append(f"Re-point or sunset the {len(orphaned)} consumer(s) fed only by {name}: {orphan_names} — they break on decommission.")
    if wants_snapshot:
        steps.append(f"Capture a retention snapshot of {name} before removal, per the note.")
    if dead:
        dead_names = ", ".join(d["name"] for d in dead[:4]) + (f", +{len(dead) - 4} more" if len(dead) > 4 else "")
        steps.append(f"Review upstream feeder(s) that served only {name}: {dead_names} — now unused, consider retiring them too.")
    steps.append(f"Decommission {name} and record the retention decision.")

    cautions: list[str] = []
    if orphaned:
        cautions.append(f"{len(orphaned)} downstream consumer(s) lose their only source: {orphan_names} — reconcile before cutover.")
    if facts["active"] and orphaned:
        cautions.append(f"{name} is still active — confirm no live job reads it before decommissioning.")

    return {
        "summary": summary,
        "pre_retire_steps": steps,
        "cautions": cautions,
        "source": "heuristic",
    }


async def _run_retirement_advisor_skill(context: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Invoke the ``data-retirement-impact-analyzer`` skill to narrate the impact.
    Returns ({}, error) on any failure so the caller falls back to the
    deterministic heuristic — the endpoint never hard-fails."""
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
    except ImportError:
        return {}, "claude-agent-sdk not installed"

    user = (
        "FIRST: Load the `data-retirement-impact-analyzer` skill using the Skill tool, then "
        "follow its instructions exactly. Narrate the retirement impact from the context below. "
        "The `facts` block (including the verdict) is ground truth — narrate it, do not recompute "
        "or override it. Do not write files or run shell commands.\n\n"
        f"CONTEXT (JSON):\n{json.dumps(context, ensure_ascii=False)}\n\n"
        "Output the single fenced ```json block the skill specifies."
    )
    options = ClaudeAgentOptions(
        allowed_tools=["Skill"], permission_mode="acceptEdits", cwd=str(BASE_DIR),
        max_turns=3, skills="all",
    )
    parts: list[str] = []
    try:
        async for message in query(prompt=user, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
            elif isinstance(message, ResultMessage) and message.is_error:
                return {}, "skill returned an error"
    except Exception as e:
        return {}, f"skill invocation failed: {e}"

    text = "\n".join(parts)
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S) or re.search(r"(\{.*\})", text, re.S)
    if not m:
        return {}, "no JSON in skill output"
    try:
        data = json.loads(m.group(1))
    except Exception:
        return {}, "skill JSON did not parse"
    return (data if isinstance(data, dict) else {}), None


async def _compose_retirement_impact(obj: dict[str, Any], facts: dict[str, Any],
                                     upstream: list[dict], downstream: list[dict],
                                     comment: str) -> dict[str, Any]:
    """Skill-first retirement impact. The heuristic owns the FACTS + VERDICT
    (already in `facts`); the skill only replaces the narrative (summary / steps /
    cautions). The verdict and the dependency facts are NEVER taken from the skill
    — so an LLM can't declare a breaking retirement 'safe' or hallucinate a
    consumer that isn't in the graph."""
    heur = _heuristic_retirement_impact(obj, facts, comment)
    ctx = {
        "object": {
            "id": obj.get("id"), "name": obj.get("name"), "type": obj.get("type"),
            "system": obj.get("system") or obj.get("platform") or obj.get("database"),
            "active": facts["active"],
        },
        "upstream": [{"id": u.get("id"), "name": u.get("name"), "type": u.get("type")} for u in upstream],
        "downstream": [{"id": d.get("id"), "name": d.get("name"), "type": d.get("type")} for d in downstream],
        "po_comment": comment,
        # Ground truth handed to the skill — it narrates these, never recomputes.
        "facts": {
            "verdict": facts["verdict"],
            "orphaned_consumers": [{"id": o["id"], "name": o["name"]} for o in facts["orphaned_consumers"]],
            "served_consumers": [{"id": o["id"], "name": o["name"], "survivors": o.get("survivors", [])} for o in facts["served_consumers"]],
            "dead_upstream": [{"id": d["id"], "name": d["name"]} for d in facts["dead_upstream"]],
        },
    }
    data, _err = await _run_retirement_advisor_skill(ctx)
    # The verdict + facts always come from the heuristic; only the prose is merged.
    merged = {"verdict": facts["verdict"], **heur}
    if data:
        for k in ("summary", "pre_retire_steps", "cautions"):
            v = data.get(k)
            if v:
                merged[k] = v
        merged["source"] = "skill"
    return merged


class RetirementImpactRequest(BaseModel):
    # Optional PO note typed inline in the retire disposition view — e.g. "retire
    # this, the new product replaced it, keep an audit snapshot". Steers the
    # retention step; the analysis itself is grounded in lineage, not the note.
    comment: Optional[str] = None


@router.post("/{project_id}/discovery/{object_id}/retirement-impact")
async def retirement_impact(
    project_id: int,
    object_id: str,
    body: Optional[RetirementImpactRequest] = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Blast-radius analysis for retiring ONE estate object — the retire analogue
    of ``migration-plan``. Computes the dependency facts + a safe/caution/blocked
    verdict deterministically from the real lineage, then invokes the
    ``data-retirement-impact-analyzer`` skill to narrate them (heuristic fallback
    when the skill/SDK isn't available). Recommend + analyse only — never mutates
    anything. `caution_nodes` are the backend's computed set so the DAG can badge
    the exact orphaned consumers + dead upstream deterministically."""
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    obj = _inventory_object(object_id, project.project_code)
    if not obj:
        raise HTTPException(404, f"Estate object not found: {object_id}")

    all_objs = (_load_estate(project.project_code).get("objects") or [])
    by_id = {o.get("id"): o for o in all_objs}
    upstream = [by_id[u] for u in (obj.get("reads") or []) if u in by_id]
    downstream = [o for o in all_objs if object_id in (o.get("reads") or [])]

    comment = (body.comment if body else None) or ""
    facts = _retirement_facts(obj, upstream, downstream, all_objs)
    impact = await _compose_retirement_impact(obj, facts, upstream, downstream, comment)

    return {
        "object": {"id": obj.get("id"), "name": obj.get("name"), "type": obj.get("type"),
                   "disposition": "retire", "active": facts["active"],
                   "platform": obj.get("platform") or obj.get("database")},
        "po_comment": comment,
        "impact": impact,
        # Deterministic node ids the DAG badges — the object itself, the
        # downstream consumers that would be orphaned, and the upstream feeders
        # that become dead weight. NOT string-matched from the skill's prose.
        "caution_nodes": {
            "self": obj.get("id"),
            "orphaned_downstream": [o["id"] for o in facts["orphaned_consumers"] if o.get("id")],
            "dead_upstream": [d["id"] for d in facts["dead_upstream"] if d.get("id")],
        },
    }


@router.get("/{project_id}/discovery/reference-data-products")
def get_reference_models(project_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Canonical reference data product models (Pipeline DAG V2). Governed target
    schemas that a report's attributes are matched against to suggest which data
    product a consumer aligns with. Attribute-matching itself runs client-side."""
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return {"models": _load_reference_models(project.project_code, session=session)}


# --------------------------------------------------------------------------
# Schema-DNA attribute comparison (replaces the old client-side heuristics).
# --------------------------------------------------------------------------


class CompareColumn(BaseModel):
    name: str
    type: str = ""
    description: str = ""
    concept: str = ""
    profile: dict[str, Any] | None = None


class CompareRequest(BaseModel):
    source_columns: list[CompareColumn]
    # Target is either a reference-model id OR an explicit attribute list.
    target_model_id: str | None = None
    target_columns: list[CompareColumn] | None = None
    variant: str = schema_dna.DEFAULT_VARIANT


def _to_features(cols: list[CompareColumn]) -> list[schema_dna.ColumnFeature]:
    return [schema_dna.ColumnFeature(name=c.name, type=c.type, description=c.description,
                                     concept=c.concept, profile=c.profile) for c in cols]


def _resolve_target(model_id: str | None,
                    explicit: list[CompareColumn] | None,
                    project_code: str | None = None,
                    session=None) -> list[schema_dna.ColumnFeature]:
    if explicit:
        return _to_features(explicit)
    if model_id:
        model = _reference_model_by_id(model_id, project_code, session=session)
        if not model:
            raise HTTPException(404, f"Reference model not found: {model_id}")
        return [schema_dna.column_from_dict(a) for a in (model.get("attributes") or [])]
    raise HTTPException(400, "Provide target_model_id or target_columns.")


@router.post("/{project_id}/discovery/compare")
def compare_attributes(project_id: int, body: CompareRequest,
                       session: Session = Depends(get_session)) -> dict[str, Any]:
    """Compare a source column set to a governed target (reference model or
    explicit attributes) using the schema-DNA scorer. Returns per-attribute
    matches + an aggregate in the ``ProductSimilarity`` radar shape. The
    ``feature_based`` variant fills all five axes; ``statistical``/``distribution``
    come back ``null`` ("unknown") when the columns carry no profiling data."""
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if not body.source_columns:
        raise HTTPException(400, "source_columns is required.")
    source = _to_features(body.source_columns)
    target = _resolve_target(body.target_model_id, body.target_columns, project.project_code,
                             session=session)
    return schema_dna.compare_columns(source, target, body.variant)


class RankRequest(BaseModel):
    source_columns: list[CompareColumn]
    variant: str = schema_dna.DEFAULT_VARIANT


@router.post("/{project_id}/discovery/compare/rank")
def compare_rank(project_id: int, body: RankRequest,
                 session: Session = Depends(get_session)) -> dict[str, Any]:
    """Score the source column set against EVERY reference model with the chosen
    scorer variant and return them RANKED — the schema-DNA replacement for the
    frontend's offline candidate ranking, so switching the scorer re-ranks the
    candidates (not just the selected model's detail)."""
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if not body.source_columns:
        raise HTTPException(400, "source_columns is required.")
    source = _to_features(body.source_columns)
    ranked: list[dict[str, Any]] = []
    embeddings_available = False
    for m in _load_reference_models(project.project_code, session=session):
        attrs = m.get("attributes") or []
        target = [schema_dna.column_from_dict(a) for a in attrs]
        res = schema_dna.compare_columns(source, target, body.variant)
        embeddings_available = res.get("embeddings_available", embeddings_available)
        matches = res.get("matches", [])
        matched = sum(1 for mm in matches if mm.get("matched"))
        # FIT = coverage-weighted, over the WHOLE selection: unmatched source
        # columns contribute 0. So a product that maps 11 of 13 selected columns
        # perfectly reads ~85% fit, not 100% — the two gaps count against it.
        # (aggregate.match stays match-QUALITY over matched cols only, for the
        # spider axes; fit is the ranking/"% fit" number.)
        total_src = len(source)
        quality_sum = sum(mm.get("match", 0) for mm in matches if mm.get("matched"))
        fit = round(quality_sum / total_src) if total_src else 0
        ranked.append({
            "model_id": m.get("id"),
            "model_name": m.get("name"),
            "score": fit,
            "match_quality": (res.get("aggregate") or {}).get("match", 0),
            "matched_count": matched,
            "selection_size": total_src,
            "product_coverage": (matched / len(attrs)) if attrs else 0,
            "aggregate": res.get("aggregate"),
        })
    ranked.sort(key=lambda r: (r["score"], r["matched_count"], r["product_coverage"]), reverse=True)
    return {"variant": body.variant, "embeddings_available": embeddings_available, "ranked": ranked}


class ClusterProposeRequest(BaseModel):
    object_ids: list[str]
    # Optional PO identity so a quick-request product lands in the right owner's
    # My Products (the estate container often has no owner_email of its own).
    owner_email: Optional[str] = None
    owner_name: Optional[str] = None


def _is_source_alignable(obj: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> bool:
    """Source-aligned products are only for raw tables or a DIRECT view/snapshot
    of tables (one hop from the source). Anything further down the chain — a
    view of a view, a query/job, a report, or a view reading a derived object —
    is consumer-aligned."""
    t = obj.get("type")
    if t == "table":
        return True
    if t in ("view", "snapshot"):
        reads = obj.get("reads") or []
        return bool(reads) and all(by_id.get(r, {}).get("type") == "table" for r in reads)
    return False


def _cluster_source_products(
    project: Project | None, table_object_names: set[str]
) -> list[dict[str, Any]]:
    """Resolve the cluster's upstream source tables to the PUBLISHED source
    products that wrap them — reusing the exact table→product matching the DAG
    uses in `_product_nodes` (product output-dataset physicalName == estate
    object_name). This is what makes the wizard suggest EXACTLY the green
    product nodes the DAG shows wired into the selection, rather than a
    text/domain-similarity guess. Non-fatal: returns [] on any graph error so
    propose never 500s (mirrors `_published_source_products`)."""
    if not project or not table_object_names:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in _published_source_products(project):
        if not any(t in table_object_names for t in (p.get("tables") or [])):
            continue
        uri = p.get("uri") or ""
        if not uri or uri in seen:
            continue
        seen.add(uri)
        out.append({
            "dprod_uri": uri,
            # Product URIs are `dprod:<contract_id>`; the wizard's inputs[] and
            # the CONSUMES MERGE key on these. contract_id is the URI sans prefix.
            "contract_id": uri.removeprefix("dprod:"),
            "name": p.get("name") or uri,
        })
    return out


def _resolve_selection_schema(
    selected: list[dict[str, Any]], by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Resolve the SELECTED nodes to the schema-bearing estate objects that
    define their columns — the source of truth for the wizard's *base* schema.

    Rule (matches "base = union of the schemas of the nodes I selected"):
      * A selected node that carries its own ``sample_columns`` (table / view /
        snapshot) contributes its OWN schema directly.
      * A selected node with no columns of its own (report / query) has no
        physical schema — its effective schema is whatever it reads, so we BFS
        UP its ``reads`` edges to the nearest schema-bearing ancestors and use
        those. Traversal stops at the first schema-bearing node on each path
        (we don't keep walking past it into ITS sources).

    Deliberately does NOT pull in the upstream tables of a node that already
    has its own schema — that upstream is candidate/context for the additive
    advisor layer, not part of the base. Returns schema-bearing objects in
    selection order, de-duplicated by id."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(obj: dict[str, Any]) -> None:
        oid = obj.get("id")
        if oid and oid not in seen:
            seen.add(oid)
            out.append(obj)

    for node in selected:
        if node.get("sample_columns"):
            _add(node)
            continue
        # Schema-less selection (report/query): BFS up `reads` to the nearest
        # objects that actually carry columns.
        stack = list(node.get("reads") or [])
        walked: set[str] = set()
        while stack:
            rid = stack.pop(0)
            if rid in walked:
                continue
            walked.add(rid)
            anc = by_id.get(rid)
            if not anc:
                continue
            if anc.get("sample_columns"):
                _add(anc)  # nearest schema-bearing ancestor — stop this path
            else:
                stack.extend(anc.get("reads") or [])
    return out


def _cluster_context(
    object_ids: list[str], parent_domain: str | None, project: Project | None = None
) -> dict[str, Any] | None:
    """Resolve a DAG selection into a product-framing context: the selected
    assets, their UPSTREAM source tables (candidate related data), the DOWNSTREAM
    reports that read them (the use case), and the recommended archetype
    (source-aligned only if EVERY selected object is a table or a direct view of
    tables; otherwise consumer-aligned)."""
    # Resolve against the FULL dense estate, not the curated core — the DAG (and
    # the Object Lineage graph that feeds "View in DAG") shows every estate object,
    # so a selection can be a discovered halo node. Keying on _load_estate()
    # (core 29) silently dropped those ids → empty selection → 400 on propose.
    by_id = {o["id"]: o for o in _load_estate(project.project_code if project else None).get("objects") or []}
    selected = [by_id[i] for i in object_ids if i in by_id]
    if not selected:
        return None
    sel_ids = {o["id"] for o in selected}
    upstream_ids = {r for o in selected for r in (o.get("reads") or [])} - sel_ids
    source_tables = [o for o in (selected + [by_id[i] for i in upstream_ids if i in by_id]) if o.get("type") == "table"]
    reports = [o for o in by_id.values()
               if o.get("type") == "report" and any(r in sel_ids for r in (o.get("reads") or []))]
    archetype = "dpe-sa" if all(_is_source_alignable(o, by_id) for o in selected) else "dpe-cf"

    # Transitive upstream closure of the selection (walk `reads` to the roots),
    # collecting every source TABLE the cluster ultimately draws from. This is
    # the set the DAG shows feeding the selection — resolving it to published
    # source products lets the wizard preselect those exact products (one hop
    # would miss products under a report selected two levels above the tables).
    closure_tables: set[str] = set()
    seen_ids: set[str] = set()
    stack = list(sel_ids)
    while stack:
        cur = stack.pop()
        if cur in seen_ids:
            continue
        seen_ids.add(cur)
        node = by_id.get(cur)
        if not node:
            continue
        if node.get("type") == "table" and node.get("object_name"):
            closure_tables.add(node["object_name"])
        for r in (node.get("reads") or []):
            if r not in seen_ids:
                stack.append(r)

    return {
        "domain": selected[0].get("domain") or parent_domain or "Data",
        "archetype": archetype,
        "selected_assets": [{"name": o.get("name"), "type": o.get("type")} for o in selected],
        # Base schema seed = union of the SELECTED nodes' own schemas (with
        # report/query selections resolved up to what they read). This is what
        # the consumer wizard hydrates as customColumns. Kept SEPARATE from
        # candidate_source_tables below (which stays the upstream-source view
        # feeding the LLM naming/narrative + the additive advisor layer).
        "selected_schema": [
            {"name": o.get("name"), "columns": [
                {"name": c.get("name"), "type": c.get("type")} for c in (o.get("sample_columns") or [])
            ]}
            for o in _resolve_selection_schema(selected, by_id)
        ],
        "candidate_source_tables": [
            {"name": o.get("name"), "columns": [
                {"name": c.get("name"), "type": c.get("type")} for c in (o.get("sample_columns") or [])
            ]}
            for o in source_tables
        ],
        "downstream_reports": [
            {"name": r.get("name"), "description": (r.get("panel_note") or r.get("description") or "").strip()}
            for r in reports
        ],
        # Published source products the upstream tables map to — the DAG's own
        # green product nodes. Empty when none are published yet (or graph down).
        "source_products": _cluster_source_products(project, closure_tables),
    }


_LOGICAL_TYPE = {
    "integer": "integer", "int": "integer", "bigint": "integer", "smallint": "integer",
    "numeric": "number", "decimal": "number", "float": "number", "double": "number", "real": "number",
    "boolean": "boolean", "bool": "boolean",
    "date": "date", "timestamp": "date", "datetime": "date", "time": "date",
    "varchar": "string", "char": "string", "text": "string", "string": "string",
}


def _match_reference_model(domain: str, project_code: str | None = None,
                           session=None) -> dict[str, Any] | None:
    """Closest governed reference data-product model for the cluster's domain."""
    models = _load_reference_models(project_code, session=session)
    dslug = (domain or "").strip().lower()
    for m in models:
        if (m.get("domain") or "").strip().lower() == dslug:
            return m
    for m in models:  # loose: model name contains the domain word
        if dslug and dslug in (m.get("name") or "").lower():
            return m
    return None


def _proposal_columns_from_sources(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """Product schema = the SELECTED node's OWN columns — the governed shape the PO
    picked in the DAG (e.g. team1_customer's 12 columns, INCLUDING derived ones like
    churn_risk_score) — NOT a raw union of every upstream source table. The upstream
    source tables are used ONLY to attribute each column to where it comes from
    ("Source: EDW.CUSTOMER.x"); a column that appears in no upstream source is a
    product-level derivation and is labelled "Derived". Grounding the schema in the
    upstream union instead over-included raw source columns the governed view never
    carried (pan_and_aadhaar_number, address, …) and dropped the derived ones."""
    # Origin index: column name (lower) → the upstream "schema.table" it comes from.
    origin: dict[str, str] = {}
    for t in (ctx.get("source_tables") or ctx.get("candidate_source_tables") or []):
        tname = t.get("name", "")
        for c in t.get("columns") or []:
            nm = (c.get("name") or "").strip().lower()
            if nm and nm not in origin:
                origin[nm] = tname
    # Base column set = the selected nodes' own schema. Fall back to the upstream
    # tables only when the selection carried no schema of its own (report/query
    # selections that resolved to nothing).
    base = ctx.get("selected_schema") or ctx.get("source_tables") or ctx.get("candidate_source_tables") or []
    seen: set[str] = set()
    cols: list[dict[str, Any]] = []
    for t in base:
        node_name = t.get("name", "")
        for c in t.get("columns") or []:
            n = (c.get("name") or "").strip()
            if not n or n.lower() in seen:
                continue
            seen.add(n.lower())
            ptype = (c.get("type") or "string").strip() or "string"
            low = n.lower()
            is_pk = low.endswith("_id") or low == "id" or low.endswith("_key") or low.endswith("_no")
            src = origin.get(low)
            desc = f"Source: {src}.{n}" if src else (f"Derived: {node_name}.{n}" if node_name else "Derived")
            cols.append({
                "name": n,
                "logical_type": _LOGICAL_TYPE.get(ptype.lower().split("(")[0], "string"),
                "physical_type": ptype,
                "description": desc,
                "primary_key": is_pk,
            })
            if len(cols) >= 40:
                return cols
    return cols


def _attribute_gap_note(cols: list[dict[str, Any]], ref_model: dict[str, Any] | None) -> str:
    """Which reference-model attributes the source columns DON'T cover, and which
    source columns have no governed home (esp. sensitive ones)."""
    if not ref_model:
        return "Schema is source-derived — no governed reference model matched this domain to compare against."
    col_names = {c["name"].lower() for c in cols}
    missing = [a.get("name") for a in (ref_model.get("attributes") or [])
               if (a.get("name") or "").lower() not in col_names]
    sensitive = [c["name"] for c in cols
                 if any(k in c["name"].lower() for k in ("pan", "aadhaar", "ssn", "passport"))]
    parts: list[str] = []
    if missing:
        parts.append(f"Reference model '{ref_model.get('name')}' attributes not covered by any selected source column: "
                     f"{', '.join(missing[:6])} — the engineer must derive or source these.")
    if sensitive:
        parts.append(f"Sensitive source columns with no governed home: {', '.join(sensitive)} — drop or mask.")
    if not parts:
        parts.append(f"All '{ref_model.get('name')}' attributes are covered by the selected source columns.")
    return " ".join(parts)


def _humanize(name: str) -> str:
    """`churn_risk_score` → `Churn Risk Score`, for readable concept phrases."""
    return " ".join(w.capitalize() for w in re.split(r"[_\s]+", (name or "").strip()) if w)


def _oxford(items: list[str]) -> str:
    """Human list join: [a] → 'a'; [a,b] → 'a and b'; [a,b,c] → 'a, b and c'."""
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _heuristic_proposal(ctx: dict[str, Any], ref_model: dict[str, Any] | None,
                        cols: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic, context-GROUNDED proposal — the fallback when the
    data-product-design-advisor skill isn't installed. Describes WHAT the product
    IS (the matched business concepts it carries, framed by the governed reference
    model + what the downstream reports use it for) rather than narrating HOW it
    was assembled. The lineage (which legacy tables it consolidates) is kept to a
    single closing clause, not the headline."""
    domain = ctx.get("domain") or "Data"
    dslug = _DOMAIN_SLUG.get(domain, domain.lower())
    subject = domain.lower()
    src_names = [t.get("name") for t in (ctx.get("source_tables") or ctx.get("candidate_source_tables") or [])]
    if not src_names:
        src_names = [a.get("name") for a in ctx.get("selected_assets", [])]
    src_names = [s for s in src_names if s]
    src_join = _oxford(src_names) or "the selected assets"

    # Downstream reports, WITH their descriptions — the "what it's used for".
    reports = ctx.get("downstream_reports") or []
    report_recs = [r if isinstance(r, dict) else {"name": r, "description": ""} for r in reports]
    report_names = [r.get("name") for r in report_recs if r.get("name")]
    rep_join = _oxford(report_names) or "downstream consumers"

    ref_name = ref_model.get("name") if ref_model else None
    name = ref_name or f"{domain} Core"

    # Business concepts this product carries — the matched reference-model concept
    # for each column (falls back to a humanised column name), so the description
    # reads in business terms, not physical column names. Derived columns (present
    # in no upstream source) are called out separately.
    attr_concept: dict[str, str] = {}
    for a in (ref_model.get("attributes") or []) if ref_model else []:
        nm = (a.get("name") or "").strip().lower()
        if nm:
            attr_concept[nm] = (a.get("concept") or _humanize(a.get("name") or "")).strip()
    carried: list[str] = []
    derived: list[str] = []
    for c in cols:
        concept = attr_concept.get(c["name"].lower(), _humanize(c["name"]))
        if str(c.get("description", "")).startswith("Derived"):
            derived.append(concept)
        else:
            carried.append(concept)
    n_cols = len(cols)

    # Sentence 1 — WHAT it is: a single conformed view, described by the concepts
    # it actually carries (capped so the lead stays crisp).
    shown = carried[:8]
    more = len(carried) - len(shown)
    covers = (", ".join(shown) + f" and {more} more") if more > 0 else _oxford(shown)
    what = f"A governed {domain} data product providing a single, conformed view of each {subject}"
    if covers:
        what += f" — covering {covers}"
    if derived:
        what += f", plus {'a ' if len(derived) == 1 else ''}derived {_oxford(derived)}"
    what += "."

    parts: list[str] = [what]

    # Sentence 2 — WHAT IT'S FOR: name the reports and, when known, what they do.
    if report_names:
        described = [f"{r['name']} ({r['description'].rstrip('.').lower()})" if r.get("description") else r["name"]
                     for r in report_recs if r.get("name")]
        parts.append(
            f"Built to serve {_oxford(described)}, which today re-derive this {subject} data "
            f"with ungoverned joins across {src_join}."
        )
    else:
        parts.append(
            f"Replaces the ungoverned copies of this {subject} data spread across {src_join}."
        )

    # Sentence 3 — conformance to the governed model (brief, optional).
    if ref_name:
        parts.append(f"Conforms to the governed {ref_name} reference model.")

    description = " ".join(parts)

    purpose = (
        f"Give {rep_join} a single governed, quality-checked {subject} source they can trust — "
        f"replacing the ungoverned joins each team maintains today across {src_join}"
        + (f", aligned to the {ref_name} standard" if ref_name else "") + "."
    )
    return {
        "name": name, "domain": dslug, "description": description, "purpose": purpose,
        "dataset_name": f"{dslug}_core", "columns": cols,
        "gap_note": _attribute_gap_note(cols, ref_model),
    }


async def _compose_proposal(ctx: dict[str, Any], project_code: str | None = None,
                            session=None) -> dict[str, Any]:
    """Unified product-design composer: enrich the discovery context with the
    matched reference model, invoke the design-advisor skill, and merge over a
    context-grounded heuristic (so the form is complete + specific either way).
    Returns the full wizard form incl. source_tables + gap_note."""
    ref_model = _match_reference_model(ctx.get("domain", ""), project_code, session=session)
    cols = _proposal_columns_from_sources(ctx)
    source_tables = ctx.get("source_tables") or ctx.get("candidate_source_tables") or []

    # Agentic proposal composition is temporarily disabled — compose the proposal
    # entirely from the deterministic, context-grounded heuristic (no
    # data-product-design-advisor skill / SDK call) so previews can be evaluated on
    # their own. Re-enable by restoring the two commented lines below.
    #   skill_ctx = {**ctx, "source_tables": source_tables, "reference_model": ref_model}
    #   data, _err = await _propose_product_llm(skill_ctx)
    data: dict[str, Any] | None = None
    heur = _heuristic_proposal(ctx, ref_model, cols)

    merged = {**heur, **{k: v for k, v in (data or {}).items() if v}}
    if not merged.get("columns"):
        merged["columns"] = cols
    merged["inputs"] = (data or {}).get("inputs") or ctx.get("source_products", [])
    merged["source_tables"] = source_tables
    merged["reference_model"] = ref_model.get("name") if ref_model else None
    merged["used_llm"] = bool(data)
    return merged


@router.post("/{project_id}/discovery/propose-product-from-cluster")
async def propose_product_from_cluster(
    project_id: int,
    req: ClusterProposeRequest,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Compose (via LLM, heuristic fallback) a data-product proposal from a DAG
    cluster — name + domain + idea/description — for the Product Owner to REVIEW
    in the source-product wizard before submitting. Does NOT create anything.
    """
    parent = session.get(Project, project_id)
    if not parent:
        raise HTTPException(404, "Project not found")
    ctx = _cluster_context(req.object_ids, parent.domain, parent)
    if not ctx:
        raise HTTPException(400, "No valid estate objects in the selection.")

    # data-product-design-advisor skill (heuristic fallback) composes the WHOLE
    # form from the discovery context — grounded in the real source tables,
    # downstream reports, matched reference model, and attribute gaps.
    p = await _compose_proposal(ctx, parent.project_code, session=session)

    # `idea` is the wizard's field name for the composed description. The domain
    # is returned as the catalog slug (matches the wizard <option> values + the
    # seeded source products' domains); archetype picks source- vs consumer wizard.
    return {"name": p["name"], "domain": p["domain"], "idea": p["description"], "purpose": p["purpose"],
            "dataset_name": p["dataset_name"], "columns": p["columns"],
            "inputs": p["inputs"],
            # The real upstream legacy source tables (with columns) behind this
            # cluster — surfaced so the wizard shows what the product is built from
            # even when no governed source PRODUCT is published yet.
            "source_tables": p["source_tables"],
            "reference_model": p["reference_model"],
            "gap_note": p["gap_note"],
            "archetype": ctx["archetype"], "used_llm": p["used_llm"]}


_DOMAIN_SLUG = {
    "Customer": "customer", "Transactions": "products_sales", "Product": "products_sales",
    "Finance": "finance", "Marketing": "customer", "Risk": "customer", "Compliance": "customer",
}
# Obvious primary-key columns in the mock sample schemas.
# ── Per-object legacy source ─────────────────────────────────────────────────
# Backs the MigratePanel's read-only code/schema view. Read-only: no transpile,
# no side effects, nothing persisted — the actual conversion happens in intake.
_EXT_LANG = {".sql": "sql", ".hql": "sql", ".py": "python", ".scala": "scala"}


def _read_text_safe(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 — missing/binary sample is a graceful "no source"
        return None


# Target platform label → the sqlglot write-dialect used to transpile TO it, and a
# short slug for filenames / node names. The conversion target is the plan's target
# (from the instructions), NOT presumed Databricks. Lakebridge (below) only handles
# the Databricks target; every other target goes through sqlglot's write-dialect.
_TARGET_DIALECT: dict[str, tuple[str, str]] = {
    "databricks": ("databricks", "dbx"),
    "snowflake": ("snowflake", "snf"),
    "bigquery": ("bigquery", "bq"),
    "redshift": ("redshift", "rs"),
    "postgres": ("postgres", "pg"),
    "trino": ("trino", "trino"),
    "synapse": ("tsql", "synapse"),
    "microsoft fabric": ("spark", "fabric"),
}


@router.get("/{project_id}/discovery/{object_id}/source")
def get_object_source(project_id: int, object_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Return just the legacy source code for an estate object — no conversion.
    Lets the migrate panel show the current code the moment a node is selected,
    before the (slower) Lakebridge transpile runs."""
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    owner_code = _estate_owner_code(project, session)
    obj = next((o for o in _load_estate(owner_code).get("objects") or [] if o.get("id") == object_id), None)
    if not obj:
        raise HTTPException(404, f"Estate object not found: {object_id}")
    dialect = (obj.get("legacy_platform") or obj.get("platform") or obj.get("database") or "").strip().lower()
    sample_source = obj.get("sample_source")
    if not sample_source:
        return {"object_id": object_id, "name": obj.get("name", ""), "dialect": dialect, "source": None,
                "errors": ["No legacy source captured for this object."]}
    # A KEY resolves from the single inlined demo file; a PATH (imported estate)
    # resolves on disk. Either way a miss degrades to "no source", never a 500.
    if "/" not in sample_source:
        entry = _load_sample_sources().get(sample_source)
        if not entry:
            return {"object_id": object_id, "name": obj.get("name", ""), "dialect": dialect, "source": None,
                    "errors": [f"No sample source registered under '{sample_source}'."]}
        return {
            "object_id": object_id, "name": obj.get("name", ""), "dialect": dialect,
            "source": {"filename": entry.get("filename") or f"{sample_source}.sql",
                       "language": entry.get("language") or "text",
                       "content": entry.get("code") or ""},
            "errors": [],
        }
    src_path = _resolve_sample_source(sample_source, owner_code)
    content = _read_text_safe(src_path)
    if content is None:
        return {"object_id": object_id, "name": obj.get("name", ""), "dialect": dialect, "source": None,
                "errors": [f"Legacy source file missing: {sample_source}"]}
    return {
        "object_id": object_id, "name": obj.get("name", ""), "dialect": dialect,
        "source": {"filename": src_path.name, "language": _EXT_LANG.get(src_path.suffix.lower(), "text"), "content": content},
        "errors": [],
    }


_DISPOSITION_SCENARIO = {"migrate": "migration", "modernize": "modernization"}


class ToIntakeBody(BaseModel):
    disposition: str                     # "migrate" | "modernize"
    object_ids: list[str] = []           # node(s) sent (defaults to the path row_id)
    title: str = ""                      # human label (node / cluster name)
    comment: str = ""                    # PO note, carried on submission metadata
    hints: dict[str, Any] = {}           # source_platform / target_platform / domain
    content: list[dict[str, Any]] = []   # FE-assembled envelope parts: {kind, title, body}


@router.post("/{project_id}/discovery/{row_id}/to-intake")
def send_row_to_intake(
    project_id: int,
    row_id: str,
    body: ToIntakeBody,
    session: Session = Depends(get_session),
):
    """Stage a discovery node's frontend-assembled context into the intake pipeline."""
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")

    disp = (body.disposition or "").strip().lower()
    scenario = _DISPOSITION_SCENARIO.get(disp)
    if scenario is None:
        raise HTTPException(422, "disposition must be 'migrate' or 'modernize' to send to intake.")
    if not body.content:
        raise HTTPException(422, "content[] is required — the frontend assembles the node context.")

    ids = body.object_ids or [row_id]
    # Stable idempotency key: same node(s) + disposition re-submit the same row.
    external_ref = f"{project.project_code}:{disp}:" + ",".join(sorted(ids))

    parts = [
        ContentPart(
            kind=(p.get("kind") or "text"),
            title=(p.get("title") or ""),
            body=p.get("body"),
        )
        for p in body.content
    ]
    metadata: dict[str, Any] = {
        "origin": "discovery",
        "project_code": project.project_code,
        "source_project_id": project_id,
        "disposition": disp,
        "object_ids": ids,
    }
    if body.title:
        metadata["title"] = body.title
    if body.comment:
        metadata["comment"] = body.comment

    env = IntakeEnvelope(
        scenario=scenario,
        external_ref=external_ref,
        content=parts,
        hints=body.hints or {},
        metadata=metadata,
    )
    # Call intake's /submit handler directly rather than over HTTP. It is a
    # plain function — FastAPI only resolves its `Depends(...)` defaults when it
    # is reached through the router — so passing `source_system` explicitly
    # bypasses `require_intake_principal` (this is an internal producer, it has
    # no machine token) while reusing intake's validation + idempotency as-is.
    # Same pattern as the `create_project(...)` call above. Deliberately leaves
    # routers/intake.py untouched.
    return submit_intake(env=env, source_system="discovery", session=session)
