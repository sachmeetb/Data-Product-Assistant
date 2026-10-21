"""Product Assembly API — `/api/assembly/*`.

The interactive "Work this product" workspace over one ``FeasibilityScore``. A
``ProductAssembly`` is a thin, durable **overlay** on the immutable score evidence:
``plan_json`` holds only the PO's decisions (per-attribute overrides, the source
cluster plan, any schema-scope override) — never a copy of the evidence — so
re-opening replays ``FeasibilityScore.evidence_json`` + the overlay.

Phase 0 ships the persistence shell + entry (create-from-score / get / patch-plan /
list / archive). The rich attribute-mapping and clustering editors (Phase 1/2) and
the commit → structured-intake handoff (Phase 3) layer on top of this model.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

import threading

from ..auth import AuthUser, current_user
from ..authz import require_role
from ..database import engine, get_session
from ..models import (
    FeasibilityRun, FeasibilityScore, ProductAssembly, SpecAttributeGroups,
)

router = APIRouter(prefix="/api/assembly", tags=["assembly"])

# An empty overlay — the shape every plan_json conforms to.
_EMPTY_PLAN: dict[str, Any] = {"attributes": {}, "clusters": [], "shortlist_override": {}}


class CreateAssemblyRequest(BaseModel):
    run_id: int
    spec_id: str


class PatchPlanRequest(BaseModel):
    # Optimistic concurrency: the client echoes the plan_revision it last read; a
    # mismatch means a concurrent write (or a re-evaluation) landed → 409.
    expected_revision: int
    plan: dict[str, Any]


def _plan_of(a: ProductAssembly) -> dict[str, Any]:
    try:
        plan = json.loads(a.plan_json or "{}")
    except (TypeError, ValueError):
        plan = {}
    # Backfill any missing top-level keys so callers can rely on the shape.
    return {**_EMPTY_PLAN, **(plan if isinstance(plan, dict) else {})}


def _assembly_row(a: ProductAssembly) -> dict[str, Any]:
    return {
        "id": a.id, "estate_id": a.estate_id, "run_id": a.run_id, "score_id": a.score_id,
        "spec_id": a.spec_id, "spec_name": a.spec_name, "domain": a.domain,
        "status": a.status, "owner_email": a.owner_email,
        "plan": _plan_of(a), "plan_revision": a.plan_revision,
        "intake_submission_id": a.intake_submission_id,
        "report_generated_at": a.report_generated_at.isoformat() if a.report_generated_at else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


def gather_assembly_inputs(session: Session, a: ProductAssembly) -> dict[str, Any]:
    """Resolve everything the blueprint compiler AND the report builder need from one
    assembly: the score + parsed evidence, the reference spec, the per-attribute
    assignment, the PO's decision overlay, and the source clusters (from the saved plan,
    else a deterministic fallback over the in-scope datasets).

    The single source of cluster resolution so the report and the scaffold can't drift.
    Shared by intake ``from_assembly`` (imported into ``routers/intake.py``) and the
    report endpoint. Raises ``HTTPException`` when the score or spec is missing."""
    from .. import estate as estate_mod
    from .. import feasibility as feas
    from .. import feasibility_clusters as fclust
    from .. import template_corpus

    score = session.exec(select(FeasibilityScore).where(
        FeasibilityScore.run_id == a.run_id, FeasibilityScore.spec_id == a.spec_id)).first()
    if score is None:
        raise HTTPException(404, "feasibility score not found")
    corpus = (template_corpus.load_specs_from_graph(session, score.domain)
              or template_corpus.load_specs_from_graph(session))
    spec = next((s for s in corpus if s.spec_id == a.spec_id), None)
    if spec is None:
        raise HTTPException(404, f"spec {a.spec_id} not found in the corpus")
    try:
        ev = json.loads(score.evidence_json or "{}")
    except (TypeError, ValueError):
        ev = {}
    assignment = (ev.get("raw_candidates") or {}).get("assignment") or []
    try:
        plan = json.loads(a.plan_json or "{}")
    except (TypeError, ValueError):
        plan = {}
    decisions = plan.get("attributes") or {}
    clusters = plan.get("clusters") or []
    if not clusters:  # user never edited/saved the Sources tab → compute deterministically
        try:
            run = session.get(FeasibilityRun, a.run_id)
            scan_ids = feas._run_scan_ids(run) if run is not None else []
            included = {(r.get("database", ""), r.get("schema", ""))
                        for r in (ev.get("schema_shortlist") or []) if r.get("included")}
            datasets = estate_mod.read_estate_datasets(session, scan_ids) if scan_ids else []
            if included:
                datasets = [d for d in datasets
                            if (d.get("database", ""), d.get("schema", "")) in included]
            fk = estate_mod.read_estate_fk_edges(session, scan_ids) if scan_ids else []
            clusters = fclust.cluster_datasets(datasets, fk)
        except Exception:
            clusters = []
    return {"score": score, "spec": spec, "ev": ev, "assignment": assignment,
            "decisions": decisions, "clusters": clusters}


@router.post("")
def create_assembly(body: CreateAssemblyRequest, session: Session = Depends(get_session),
                    user: AuthUser = Depends(current_user), _role=Depends(require_role("owner"))):
    """Open (or re-open) the assembly for a score. Idempotent per (score, owner) —
    'Work this product' twice returns the same workspace."""
    score = session.exec(
        select(FeasibilityScore).where(
            FeasibilityScore.run_id == body.run_id, FeasibilityScore.spec_id == body.spec_id
        )
    ).first()
    if score is None:
        raise HTTPException(404, f"No score for spec {body.spec_id} in run {body.run_id}")
    existing = session.exec(
        select(ProductAssembly).where(
            ProductAssembly.score_id == score.id,
            ProductAssembly.owner_email == user.email,
        )
    ).first()
    if existing is not None:
        # Re-opening an archived assembly reactivates it as a draft.
        if existing.status == "archived":
            existing.status = "draft"
            existing.updated_at = datetime.utcnow()
            session.add(existing)
            session.commit()
            session.refresh(existing)
        return _assembly_row(existing)
    a = ProductAssembly(
        estate_id=score.estate_id, run_id=score.run_id, score_id=score.id,
        spec_id=score.spec_id, spec_name=score.spec_name, domain=score.domain,
        status="draft", owner_email=user.email,
        plan_json=json.dumps(_EMPTY_PLAN),
    )
    session.add(a)
    session.commit()
    session.refresh(a)
    return _assembly_row(a)


@router.get("/{assembly_id}")
def get_assembly(assembly_id: int, session: Session = Depends(get_session),
                 user: AuthUser = Depends(current_user)):
    """The assembly overlay + the score evidence it overlays (the workspace read)."""
    a = session.get(ProductAssembly, assembly_id)
    if a is None:
        raise HTTPException(404, f"Assembly {assembly_id} not found")
    row = _assembly_row(a)
    score = session.exec(
        select(FeasibilityScore).where(
            FeasibilityScore.run_id == a.run_id, FeasibilityScore.spec_id == a.spec_id
        )
    ).first()
    if score is not None:
        row["tier"] = score.tier
        row["evaluation_state"] = score.evaluation_state
        row["required_coverage"] = score.required_coverage
        row["evidence"] = json.loads(score.evidence_json or "{}")
        row["matched"] = json.loads(score.matched_json or "[]")
        row["gaps"] = json.loads(score.gaps_json or "[]")
    return row


@router.patch("/{assembly_id}/plan")
def patch_plan(assembly_id: int, body: PatchPlanRequest, session: Session = Depends(get_session),
               user: AuthUser = Depends(current_user), _role=Depends(require_role("owner"))):
    """Persist the workspace's decision overlay with optimistic concurrency. The
    client PATCHes the full plan; the server bumps ``plan_revision`` on success."""
    a = session.get(ProductAssembly, assembly_id)
    if a is None:
        raise HTTPException(404, f"Assembly {assembly_id} not found")
    if a.status == "scaffolded":
        raise HTTPException(409, {"error": "frozen", "message": "assembly already scaffolded"})
    if body.expected_revision != a.plan_revision:
        raise HTTPException(409, {"error": "revision_conflict",
                                  "current_revision": a.plan_revision})
    merged = {**_EMPTY_PLAN, **(body.plan if isinstance(body.plan, dict) else {})}
    a.plan_json = json.dumps(merged, default=str)
    a.plan_revision += 1
    # A plan edit keeps the assembly a draft (commit is a separate action, Phase 3).
    a.updated_at = datetime.utcnow()
    session.add(a)
    session.commit()
    session.refresh(a)
    return _assembly_row(a)


@router.get("")
def list_assemblies(session: Session = Depends(get_session),
                    user: AuthUser = Depends(current_user)):
    """The current owner's non-archived assemblies (their in-flight workspaces)."""
    rows = session.exec(
        select(ProductAssembly).where(
            ProductAssembly.owner_email == user.email,
            ProductAssembly.status != "archived",
        ).order_by(ProductAssembly.id.desc())  # type: ignore[union-attr]
    ).all()
    return {"assemblies": [_assembly_row(r) for r in rows], "count": len(rows)}


@router.get("/{assembly_id}/columns")
def list_scope_columns(assembly_id: int, q: Optional[str] = None,
                       session: Session = Depends(get_session),
                       user: AuthUser = Depends(current_user)):
    """The in-scope estate columns for 'browse & map' — every column in the score's
    shortlisted schemas, so a PO can map an attribute to a column the ranker didn't
    surface. Bounded + best-effort (a graph read; returns [] if the estate isn't
    reachable). Optional ``q`` filters on column/table name substring."""
    from .. import estate as estate_mod
    from .. import feasibility as feas

    a = session.get(ProductAssembly, assembly_id)
    if a is None:
        raise HTTPException(404, f"Assembly {assembly_id} not found")
    score = session.exec(
        select(FeasibilityScore).where(
            FeasibilityScore.run_id == a.run_id, FeasibilityScore.spec_id == a.spec_id
        )
    ).first()
    included: set[tuple[str, str]] = set()
    if score is not None:
        try:
            ev = json.loads(score.evidence_json or "{}")
        except (TypeError, ValueError):
            ev = {}
        included = {(r.get("database", ""), r.get("schema", ""))
                    for r in (ev.get("schema_shortlist") or []) if r.get("included")}
    run = session.get(FeasibilityRun, a.run_id)
    out: list[dict[str, Any]] = []
    try:
        scan_ids = feas._run_scan_ids(run) if run is not None else []
        datasets = estate_mod.read_estate_datasets(session, scan_ids) if scan_ids else []
    except Exception:
        datasets = []
    needle = (q or "").strip().lower()
    for d in datasets:
        key = (d.get("database", ""), d.get("schema", ""))
        if included and key not in included:
            continue
        tbl = d.get("table", "")
        for c in d.get("columns", []) or []:
            name = c.get("name", "")
            if needle and needle not in name.lower() and needle not in tbl.lower():
                continue
            out.append({
                "schema": d.get("schema", ""), "table": tbl, "column": name,
                "type": c.get("data_type", ""), "description": (c.get("description") or "")[:300],
            })
            if len(out) >= 500:
                break
        if len(out) >= 500:
            break
    return {"columns": out, "count": len(out), "truncated": len(out) >= 500}


@router.get("/{assembly_id}/clusters")
def get_clusters(assembly_id: int, refine: bool = False,
                 session: Session = Depends(get_session),
                 user: AuthUser = Depends(current_user)):
    """Source clustering for the Sources tab: the deterministic 'seams' over the
    score's in-scope datasets (FK edges + selectivity-guarded shared keys), plus any
    user-edited cluster plan saved on the assembly. Best-effort graph read.

    ``refine=true`` runs the tool-less ``data-product-boundary-advisor`` once to refine
    the clusters by reasoning over the tables' generated descriptions (the name-based
    pass can't, e.g. per-table-prefixed star-schema columns); it falls back to the
    deterministic clusters on any failure or an invalid (non-partition) result."""
    from .. import estate as estate_mod
    from .. import feasibility as feas
    from .. import feasibility_clusters as fclust

    a = session.get(ProductAssembly, assembly_id)
    if a is None:
        raise HTTPException(404, f"Assembly {assembly_id} not found")
    score = session.exec(
        select(FeasibilityScore).where(
            FeasibilityScore.run_id == a.run_id, FeasibilityScore.spec_id == a.spec_id
        )
    ).first()
    included: set[tuple[str, str]] = set()
    schema_shortlist: list = []
    if score is not None:
        try:
            ev = json.loads(score.evidence_json or "{}")
        except (TypeError, ValueError):
            ev = {}
        schema_shortlist = ev.get("schema_shortlist") or []
        included = {(r.get("database", ""), r.get("schema", ""))
                    for r in schema_shortlist if r.get("included")}
    run = session.get(FeasibilityRun, a.run_id)
    auto: list = []
    all_datasets: list = []
    fk_edges: list = []
    try:
        scan_ids = feas._run_scan_ids(run) if run is not None else []
        all_datasets = estate_mod.read_estate_datasets(session, scan_ids) if scan_ids else []
        fk_edges = estate_mod.read_estate_fk_edges(session, scan_ids) if scan_ids else []
    except Exception:
        all_datasets = []
        fk_edges = []
    # In-scope datasets (for the clustering) vs ALL (for the overlap/duplication check).
    datasets = [d for d in all_datasets
                if (not included) or (d.get("database", ""), d.get("schema", "")) in included]
    try:
        auto = fclust.cluster_datasets(datasets, fk_edges)
    except Exception:
        auto = []

    # Per-table metadata for the Sources tab: which schema(s) carry each in-scope table
    # (a table name present in MORE than one in-scope schema is ambiguous — the tell-tale
    # of duplicate schemas like tpcds_sf1 vs tpcds_sf1000) + a generated description.
    table_meta: dict[str, dict[str, Any]] = {}
    for d in datasets:
        t = d.get("table", "")
        if not t:
            continue
        m = table_meta.setdefault(t, {"schemas": [], "description": ""})
        sch = d.get("schema", "")
        if sch and sch not in m["schemas"]:
            m["schemas"].append(sch)
        if not m["description"] and (d.get("description") or "").strip():
            m["description"] = (d.get("description") or "").strip()[:300]
    for m in table_meta.values():
        m["schemas"] = sorted(m["schemas"])
        m["ambiguous"] = len(m["schemas"]) > 1

    # Schema overlap/duplication: annotate every shortlisted schema with the OTHER
    # in-scope schemas it shares table names with (so the PO can drop a duplicate).
    tables_by_schema: dict[tuple[str, str], set[str]] = {}
    for d in all_datasets:
        tables_by_schema.setdefault(
            (d.get("database", ""), d.get("schema", "")), set()).add(d.get("table", ""))
    for row in schema_shortlist:
        key = (row.get("database", ""), row.get("schema", ""))
        mine = tables_by_schema.get(key, set())
        overlaps = []
        for okey, otabs in tables_by_schema.items():
            if okey == key or not mine:
                continue
            shared = len(mine & otabs)
            if shared and shared >= max(1, int(0.5 * min(len(mine), len(otabs)))):
                overlaps.append({"schema": ".".join(x for x in okey if x) or okey[1],
                                 "shared": shared})
        if overlaps:
            row["overlaps"] = sorted(overlaps, key=lambda x: -x["shared"])[:4]

    refined_by_ai = False
    if refine and auto and datasets:
        try:
            import asyncio
            payload = fclust.boundary_advisor_input(auto, datasets, fk_edges)
            all_tables = [t["table_ref"] for t in payload.get("tables", [])]
            raw = asyncio.run(feas._run_boundary_advisor_skill(payload))
            validated = fclust.validate_clusters(raw, all_tables) if raw else None
            if validated:
                auto = validated
                refined_by_ai = True
        except Exception:
            pass  # refinement is best-effort; the deterministic clusters stand
    user_clusters = _plan_of(a).get("clusters") or []
    return {"auto": auto, "user": user_clusters,
            "schema_shortlist": schema_shortlist, "table_meta": table_meta,
            "edited": bool(user_clusters), "refined_by_ai": refined_by_ai}


def _spec_attr_signature(spec) -> str:
    """A short content signature over a spec's attribute names — the AI
    attribute-grouping cache key. Replaces the retired file ``corpus_version``
    stamp (now that specs are read live from the graph): the cache still
    invalidates when the template's columns change."""
    import hashlib
    payload = "|".join(a.name for a in spec.attributes)
    return "g:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _compute_attribute_groups_bg(spec_id: str, payload: dict, all_names: list, corpus_ver: str) -> None:
    """Background worker: run the (slow, 200+ attr) grouper skill and write the
    validated groups to the per-spec cache. Best-effort; failure marks the row failed."""
    from .. import feasibility as feas
    from .. import feasibility_clusters as fclust
    groups = None
    try:
        import asyncio
        raw = asyncio.run(feas._run_attribute_grouper_skill(payload))
        groups = fclust.validate_attribute_groups(raw, all_names) if raw else None
    except Exception:
        groups = None
    try:
        with Session(engine) as s:
            row = s.get(SpecAttributeGroups, spec_id)
            if row is None:
                return
            if groups:
                row.status = "ready"
                row.groups_json = json.dumps(groups, default=str)
            else:
                row.status = "failed"
            row.updated_at = datetime.utcnow()
            s.add(row)
            s.commit()
    except Exception:
        pass


@router.get("/{assembly_id}/attribute-groups")
def get_attribute_groups(assembly_id: int, force: bool = False,
                         session: Session = Depends(get_session),
                         user: AuthUser = Depends(current_user)):
    """Partition the spec's attributes into intuitive themes (Identity, Contact,
    Marketing, …) via the tool-less ``data-product-attribute-grouper``. The grouping is
    per-SPEC and the LLM call is slow (200+ attrs), so it's cached and computed in the
    BACKGROUND: returns ``ready`` (with groups) instantly on a cache hit, else kicks off
    the compute and returns ``computing`` for the client to poll."""
    from .. import template_corpus

    a = session.get(ProductAssembly, assembly_id)
    if a is None:
        raise HTTPException(404, f"Assembly {assembly_id} not found")
    score = session.exec(select(FeasibilityScore).where(
        FeasibilityScore.run_id == a.run_id, FeasibilityScore.spec_id == a.spec_id)).first()
    corpus = ((template_corpus.load_specs_from_graph(session, score.domain) if score else None)
              or template_corpus.load_specs_from_graph(session))
    spec = next((s for s in corpus if s.spec_id == a.spec_id), None)
    if spec is None:
        return {"status": "unavailable", "groups": [], "grouped_by_ai": False}

    corpus_ver = _spec_attr_signature(spec)
    cache = session.get(SpecAttributeGroups, spec.spec_id)
    if cache and cache.status == "ready" and cache.corpus_version == corpus_ver and not force:
        try:
            groups = json.loads(cache.groups_json or "[]")
        except (TypeError, ValueError):
            groups = []
        return {"status": "ready", "groups": groups, "grouped_by_ai": bool(groups)}
    # A fresh in-flight compute (within 5 min) — tell the client to keep polling.
    fresh = cache and cache.status == "computing" and cache.corpus_version == corpus_ver \
        and cache.updated_at and (datetime.utcnow() - cache.updated_at).total_seconds() < 300
    if fresh and not force:
        return {"status": "computing", "groups": [], "grouped_by_ai": False}
    # A failed compute stays failed until the client explicitly retries (?force=true) —
    # so a poll loop doesn't re-kick the LLM on every tick.
    if cache and cache.status == "failed" and cache.corpus_version == corpus_ver and not force:
        return {"status": "failed", "groups": [], "grouped_by_ai": False}

    # Kick off (or restart) the background compute.
    if cache is None:
        cache = SpecAttributeGroups(spec_id=spec.spec_id, status="computing", corpus_version=corpus_ver)
    else:
        cache.status = "computing"
        cache.corpus_version = corpus_ver
        cache.updated_at = datetime.utcnow()
    session.add(cache)
    session.commit()
    payload = {"attributes": [{"name": at.name, "concept": at.concept or at.name,
                               "description": (at.description or "")[:120]}
                              for at in spec.attributes]}
    all_names = [at.name for at in spec.attributes]
    threading.Thread(target=_compute_attribute_groups_bg,
                     args=(spec.spec_id, payload, all_names, corpus_ver), daemon=True).start()
    return {"status": "computing", "groups": [], "grouped_by_ai": False}


@router.get("/{assembly_id}/table")
def get_table_detail(assembly_id: int, table: str, schema: Optional[str] = None,
                     session: Session = Depends(get_session),
                     user: AuthUser = Depends(current_user)):
    """Full detail for one estate table (for the Sources-tab table popup): its
    description, columns (name/type/description), and its FK relationships to/from
    other tables — so the PO can confirm a table belongs in its cluster. Best-effort."""
    from .. import estate as estate_mod
    from .. import feasibility as feas

    a = session.get(ProductAssembly, assembly_id)
    if a is None:
        raise HTTPException(404, f"Assembly {assembly_id} not found")
    run = session.get(FeasibilityRun, a.run_id)
    try:
        scan_ids = feas._run_scan_ids(run) if run is not None else []
        datasets = estate_mod.read_estate_datasets(session, scan_ids) if scan_ids else []
        fk_edges = estate_mod.read_estate_fk_edges(session, scan_ids) if scan_ids else []
    except Exception:
        datasets, fk_edges = [], []
    match = next((d for d in datasets if d.get("table") == table
                  and (schema is None or d.get("schema") == schema)), None)
    if match is None:
        match = next((d for d in datasets if d.get("table") == table), None)
    if match is None:
        return {"table": table, "schema": schema, "description": "", "columns": [],
                "references_out": [], "references_in": [], "found": False}
    cols = [{"name": c.get("name", ""), "type": c.get("data_type", ""),
             "description": (c.get("description") or "")} for c in (match.get("columns") or [])]
    refs_out = [{"table": e.get("tgt_table", ""), "on": e.get("columns") or []}
                for e in fk_edges if e.get("src_table") == table]
    refs_in = [{"table": e.get("src_table", ""), "on": e.get("columns") or []}
               for e in fk_edges if e.get("tgt_table") == table]
    return {
        "table": table, "schema": match.get("schema", ""),
        "database": match.get("database", ""), "description": match.get("description") or "",
        "row_count": match.get("row_count"), "columns": cols,
        "references_out": refs_out, "references_in": refs_in, "found": True,
    }


@router.get("/{assembly_id}/report")
def get_report(assembly_id: int, regenerate: bool = False,
               session: Session = Depends(get_session),
               user: AuthUser = Depends(current_user)):
    """The deterministic functional report (markdown + mermaid), cached on the assembly.

    ``regenerate=true`` rebuilds + re-persists; otherwise returns the saved report,
    building (and persisting) one on first ask. Pure builder — this endpoint only gathers
    the inputs (the SAME shared gather + blueprint the scaffold uses, so the report can't
    drift from what gets created) and shapes them via ``assembly_report``."""
    from .. import assembly_blueprint as ab
    from .. import assembly_report as arep
    from .. import feasibility as feas
    from ..models import Estate, EstateScan

    a = session.get(ProductAssembly, assembly_id)
    if a is None:
        raise HTTPException(404, f"Assembly {assembly_id} not found")
    if a.report_md and not regenerate:
        return {"markdown": a.report_md, "cached": True,
                "generated_at": a.report_generated_at.isoformat() if a.report_generated_at else None}

    g = gather_assembly_inputs(session, a)
    score, spec = g["score"], g["spec"]
    spec_name = score.spec_name or spec.name
    domain = score.domain or spec.domain

    # The SAME blueprint the intake handoff compiles — so the report describes exactly
    # what will be scaffolded (source products + aggregate).
    blueprint = ab.build_modernization_blueprint(
        spec, g["assignment"], g["decisions"], g["clusters"],
        spec_name=spec_name, domain=domain, owner_email=a.owner_email)

    # Themes from the per-spec grouping cache (validated full partition); [] → ungrouped.
    attribute_groups: list = []
    cache = session.get(SpecAttributeGroups, a.spec_id)
    if cache and cache.status == "ready":
        try:
            attribute_groups = json.loads(cache.groups_json or "[]")
        except (TypeError, ValueError):
            attribute_groups = []

    estate = session.get(Estate, a.estate_id)
    estate_name = estate.name if estate else ""
    scan_finished_at: Optional[str] = None
    try:
        run = session.get(FeasibilityRun, a.run_id)
        scan_ids = feas._run_scan_ids(run) if run is not None else []
        finished = [sc.finished_at for sid in scan_ids
                    if (sc := session.get(EstateScan, sid)) is not None and sc.finished_at]
        if finished:
            scan_finished_at = max(finished).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        scan_finished_at = None

    now = datetime.utcnow()
    markdown = arep.build_report_markdown(
        spec=spec, spec_name=spec_name, domain=domain, tier=score.tier or "absent",
        evidence=g["ev"], assignment=g["assignment"], decisions=g["decisions"],
        blueprint=blueprint, attribute_groups=attribute_groups,
        estate_name=estate_name, scan_finished_at=scan_finished_at,
        generated_at=now.strftime("%Y-%m-%d %H:%M UTC"))

    a.report_md = markdown
    a.report_generated_at = now
    a.updated_at = now
    session.add(a)
    session.commit()
    return {"markdown": markdown, "cached": False, "generated_at": now.isoformat()}


@router.delete("/{assembly_id}", status_code=200)
def archive_assembly(assembly_id: int, session: Session = Depends(get_session),
                     user: AuthUser = Depends(current_user), _role=Depends(require_role("owner"))):
    """Soft-archive an assembly (status → archived). Re-opening via create reactivates it."""
    a = session.get(ProductAssembly, assembly_id)
    if a is None:
        raise HTTPException(404, f"Assembly {assembly_id} not found")
    a.status = "archived"
    a.updated_at = datetime.utcnow()
    session.add(a)
    session.commit()
    return {"ok": True}
