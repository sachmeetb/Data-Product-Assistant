"""Top-down feasibility API — `/api/feasibility/*`.

Lists the vendored reference-spec corpus, queues a feasibility evaluation against
a pinned EstateScan (runs on the leased ``estate_worker``), and serves the
stoplight grid + per-spec drill-down. Independent of the Pulse surface.
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from .. import feasibility as feas
from .. import template_corpus
from ..auth import AuthUser, current_user
from ..authz import require_role
from ..database import get_session
from ..models import Estate, EstateScan, FeasibilityCandidate, FeasibilityRun, FeasibilityScore

router = APIRouter(prefix="/api/feasibility", tags=["feasibility"])


class EvaluateRequest(BaseModel):
    estate_id: int
    # None → estate-wide: span the latest scan of every enabled source (all
    # catalogs). An explicit id → legacy single-scan targeting (back-compat).
    scan_id: Optional[int] = None
    domain: Optional[str] = None   # None = all domains
    spec_ids: Optional[list[str]] = None  # None / [] = no filter (evaluate all)
    # Tiered schema-scoping levers (Stage 1: product → schema shortlist). Defaults
    # applied when omitted; scope_to_schemas=False is the whole-estate escape hatch.
    scope_to_schemas: bool = True
    schema_relevance_floor: Optional[float] = None
    schema_shortlist_threshold: Optional[float] = None
    max_schemas_per_spec: Optional[int] = None
    # Explicit per-schema overrides (each [database, schema]) — force a below-floor
    # schema IN (the data may live there) or a scored one OUT (a duplicate). Wins over
    # the score-based shortlist.
    schema_include_override: Optional[list[tuple[str, str]]] = None
    schema_exclude_override: Optional[list[tuple[str, str]]] = None


class RecommendRequest(BaseModel):
    estate_id: int
    domain: Optional[str] = None   # None = all domains
    recommend_floor: Optional[float] = None


# ── reference specs ────────────────────────────────────────────────────────────

@router.get("/index")
def get_spec_index(domain: Optional[str] = Query(None), session: Session = Depends(get_session)):
    """Lightweight spec index (id, name, domain, counts) — powers the UI spec picker.
    Read live from the published Blueprint Library (no baked corpus)."""
    specs = template_corpus.load_index_from_graph(session, domain)
    return {"specs": specs, "count": len(specs),
            "corpus_version": template_corpus.GRAPH_CORPUS_MARKER}


@router.get("/specs")
def list_specs(domain: Optional[str] = Query(None), session: Session = Depends(get_session)):
    specs = template_corpus.load_specs_from_graph(session, domain)
    return {
        "corpus_version": template_corpus.GRAPH_CORPUS_MARKER,
        "domains": template_corpus.list_domains_from_graph(session),
        "specs": [
            {
                "spec_id": s.spec_id, "name": s.name, "domain": s.domain,
                "product_kind": s.product_kind.value, "description": s.description,
                "required_attribute_count": len(s.required_attributes),
                "total_attribute_count": len(s.attributes),
                "grain_keys": s.grain.keys, "freshness": s.freshness.history.value,
                "classification": s.classification.value,
            }
            for s in specs
        ],
        "count": len(specs),
    }


@router.get("/specs/{spec_id}")
def get_spec(spec_id: str, session: Session = Depends(get_session)):
    for s in template_corpus.load_specs_from_graph(session):
        if s.spec_id == spec_id:
            return s.model_dump(mode="json")
    raise HTTPException(404, f"Spec {spec_id} not found in corpus")


@router.get("/specs/{spec_id}/odcs")
def get_spec_odcs(spec_id: str, session: Session = Depends(get_session)):
    """A **project-bound** consumer ODCS draft built from the reference spec's
    attributes — the seed the `adaptable`-verdict deep-link (`?from_spec=<id>`) hands
    the CF wizard so Step 4 confirms the agreed schema instead of the advisor's
    generic catalog columns. Template identity (`id`/`sourceSpecId`) is stripped and
    `status` is `draft` (see `feasibility.sanitized_consumer_odcs`)."""
    for s in template_corpus.load_specs_from_graph(session):
        if s.spec_id == spec_id:
            return feas.sanitized_consumer_odcs(s)
    raise HTTPException(404, f"Spec {spec_id} not found in corpus")


# ── evaluation ─────────────────────────────────────────────────────────────────

@router.post("/evaluate")
def evaluate(body: EvaluateRequest, session: Session = Depends(get_session),
             user: AuthUser = Depends(current_user),
             _role=Depends(require_role("owner"))):
    estate = session.get(Estate, body.estate_id)
    if estate is None:
        raise HTTPException(404, f"Estate {body.estate_id} not found")

    # Resolve the scan set: an explicit scan_id (legacy single-catalog) or the
    # estate-wide latest-scan-per-enabled-source (multi-catalog default).
    if body.scan_id is not None:
        scan = session.get(EstateScan, body.scan_id)
        if scan is None or scan.estate_id != body.estate_id:
            raise HTTPException(404, f"Scan {body.scan_id} not found for estate {body.estate_id}")
        if scan.state in ("queued", "running"):
            raise HTTPException(409, {"message": "The scan is still running — wait for it to finish.",
                                      "scan_state": scan.state})
        if scan.state == "failed":
            raise HTTPException(422, {"message": "Cannot evaluate a failed scan — rescan first.",
                                      "scan_state": scan.state})
        scan_ids = [scan.id]
    else:
        scan_ids = feas.latest_scans_for_estate(session, body.estate_id)
        if not scan_ids:
            raise HTTPException(422, {"message": "No completed scan to evaluate — "
                                                 "scan at least one source first."})

    # De-dupe: one active run per (estate, domain).
    active = session.exec(
        select(FeasibilityRun).where(
            FeasibilityRun.estate_id == body.estate_id,
            FeasibilityRun.state.in_(["queued", "running"]),  # type: ignore[attr-defined]
        )
    ).all()
    for r in active:
        if (r.domain or None) == (body.domain or None):
            raise HTTPException(409, {"message": "An evaluation is already running for this estate/domain.",
                                      "run_id": r.id})

    scoping = {
        "enabled": bool(body.scope_to_schemas),
        "floor": body.schema_relevance_floor if body.schema_relevance_floor is not None
                 else feas.SCHEMA_RELEVANCE_FLOOR,
        "threshold": body.schema_shortlist_threshold if body.schema_shortlist_threshold is not None
                     else feas.SCHEMA_SHORTLIST_THRESHOLD,
        "cap": body.max_schemas_per_spec if body.max_schemas_per_spec is not None
               else feas.MAX_SCHEMAS_PER_SPEC,
        "include": [list(k) for k in (body.schema_include_override or [])],
        "exclude": [list(k) for k in (body.schema_exclude_override or [])],
    }
    run = FeasibilityRun(estate_id=body.estate_id, scan_id=max(scan_ids),
                         scan_ids_json=json.dumps(sorted(scan_ids)),
                         spec_ids_json=json.dumps(body.spec_ids or []),
                         schema_scoping_json=json.dumps(scoping),
                         domain=body.domain, state="queued", created_by=user.email)
    session.add(run)
    session.commit()
    session.refresh(run)
    return _run_row(run, session, include_scores=False)


@router.post("/recommend-specs")
async def recommend_specs(body: RecommendRequest, session: Session = Depends(get_session),
                          user: AuthUser = Depends(current_user),
                          _role=Depends(require_role("owner"))):
    """Read the enriched estate and recommend which reference product definitions
    to evaluate. LLM skill with an always-available embedding-heuristic fallback;
    every spec is banded strong/tentative — the strong band is pre-checked in the
    UI (the PO can still edit)."""
    estate = session.get(Estate, body.estate_id)
    if estate is None:
        raise HTTPException(404, f"Estate {body.estate_id} not found")
    scan_ids = feas.latest_scans_for_estate(session, body.estate_id)
    if not scan_ids:
        raise HTTPException(422, {"message": "No completed scan to read — "
                                             "scan at least one source first."})
    scans = [s for s in (session.get(EstateScan, sid) for sid in scan_ids) if s is not None]
    estate_text = feas.build_estate_description(session, scans)
    specs = template_corpus.load_index_from_graph(session, body.domain)
    floor = body.recommend_floor if body.recommend_floor is not None else feas.RECOMMEND_FLOOR
    result = await feas.recommend_specs(estate_text, specs, floor=floor)
    result["spec_count"] = len(specs)
    return result


@router.get("/runs")
def list_runs(estate_id: Optional[int] = Query(None), session: Session = Depends(get_session)):
    q = select(FeasibilityRun).order_by(FeasibilityRun.id.desc())  # type: ignore[union-attr]
    if estate_id is not None:
        q = select(FeasibilityRun).where(FeasibilityRun.estate_id == estate_id).order_by(FeasibilityRun.id.desc())  # type: ignore[union-attr]
    runs = session.exec(q).all()
    return {"runs": [_run_row(r, session, include_scores=False) for r in runs], "count": len(runs)}


@router.get("/runs/{run_id}")
def get_run(run_id: int, session: Session = Depends(get_session)):
    run = session.get(FeasibilityRun, run_id)
    if run is None:
        raise HTTPException(404, f"Run {run_id} not found")
    return _run_row(run, session, include_scores=True)


@router.get("/runs/{run_id}/scores/{spec_id}")
def get_score_detail(run_id: int, spec_id: str, session: Session = Depends(get_session)):
    score = session.exec(
        select(FeasibilityScore).where(
            FeasibilityScore.run_id == run_id, FeasibilityScore.spec_id == spec_id
        )
    ).first()
    if score is None:
        raise HTTPException(404, f"No score for spec {spec_id} in run {run_id}")
    return _score_row(score, detail=True)


class ReevaluateRequest(BaseModel):
    # Each entry is [database, schema]. include forces a schema in-scope; exclude drops one.
    schema_include: Optional[list[tuple[str, str]]] = None
    schema_exclude: Optional[list[tuple[str, str]]] = None


@router.post("/runs/{run_id}/scores/{spec_id}/reevaluate")
def reevaluate(run_id: int, spec_id: str, body: ReevaluateRequest,
               session: Session = Depends(get_session),
               user: AuthUser = Depends(current_user), _role=Depends(require_role("owner"))):
    """Re-score ONE spec with a per-schema include/exclude override and write it back —
    the Assembly 'toggle a schema, re-score' action (deterministic/heuristic)."""
    result = feas.reevaluate_one_score(session, run_id, spec_id,
                                       body.schema_include, body.schema_exclude)
    if result.get("error"):
        raise HTTPException(404, result)
    return result


# ── act on a verdict (tier-differentiated) ────────────────────────────────────

@router.get("/runs/{run_id}/scores/{spec_id}/action")
def get_action(run_id: int, spec_id: str, session: Session = Depends(get_session)):
    """The tier-appropriate next action for a score (adopt / adapt / assemble / gap)."""
    result = feas.build_action(session, run_id, spec_id)
    if result.get("error"):
        raise HTTPException(404, result)
    return result


@router.post("/scores/{score_id}/candidate")
def save_candidate(score_id: int, body: dict, session: Session = Depends(get_session),
                   user: AuthUser = Depends(current_user), _role=Depends(require_role("owner"))):
    """Save a feasibility score as a candidate for future work (de-duped by score+user)."""
    score = session.get(FeasibilityScore, score_id)
    if score is None:
        raise HTTPException(404, f"Score {score_id} not found")
    notes = body.get("notes", "") if isinstance(body, dict) else ""
    existing = session.exec(
        select(FeasibilityCandidate).where(
            FeasibilityCandidate.score_id == score_id,
            FeasibilityCandidate.saved_by == user.email,
        )
    ).first()
    from datetime import datetime as _dt
    if existing:
        existing.notes = notes
        existing.status = "saved"
        existing.updated_at = _dt.utcnow()
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return _candidate_row(existing)
    cand = FeasibilityCandidate(
        score_id=score_id, run_id=score.run_id, estate_id=score.estate_id,
        spec_id=score.spec_id, spec_name=score.spec_name, domain=score.domain,
        tier=score.tier, required_coverage=score.required_coverage,
        confidence=score.confidence, rationale=score.rationale,
        adaptation_notes=score.adaptation_notes, notes=notes, saved_by=user.email,
    )
    session.add(cand)
    session.commit()
    session.refresh(cand)
    return _candidate_row(cand)


@router.delete("/scores/{score_id}/candidate", status_code=200)
def dismiss_candidate(score_id: int, session: Session = Depends(get_session),
                      user: AuthUser = Depends(current_user), _role=Depends(require_role("owner"))):
    """Soft-dismiss a saved candidate (status → dismissed)."""
    from datetime import datetime as _dt
    cand = session.exec(
        select(FeasibilityCandidate).where(
            FeasibilityCandidate.score_id == score_id,
            FeasibilityCandidate.saved_by == user.email,
        )
    ).first()
    if cand is None:
        raise HTTPException(404, f"No saved candidate for score {score_id}")
    cand.status = "dismissed"
    cand.updated_at = _dt.utcnow()
    session.add(cand)
    session.commit()
    return {"ok": True}


@router.get("/candidates")
def list_candidates(owner_email: Optional[str] = Query(None), session: Session = Depends(get_session),
                    user: AuthUser = Depends(current_user)):
    """List saved (not dismissed) candidates for an owner email (defaults to current user)."""
    email = owner_email or user.email
    rows = session.exec(
        select(FeasibilityCandidate).where(
            FeasibilityCandidate.saved_by == email,
            FeasibilityCandidate.status == "saved",
        ).order_by(FeasibilityCandidate.id.desc())  # type: ignore[union-attr]
    ).all()
    return {"candidates": [_candidate_row(r) for r in rows], "count": len(rows)}


@router.post("/runs/{run_id}/scores/{spec_id}/act")
def act(run_id: int, spec_id: str, session: Session = Depends(get_session),
        user: AuthUser = Depends(current_user), _role=Depends(require_role("owner"))):
    """Execute the action for a score. For ``assemblable`` this composes a
    modernization portfolio and stages it for review in the Intake surface (which
    runs the existing scaffold saga on approval). ``ready``/``adaptable`` are
    navigation actions — this returns their descriptor unchanged."""
    action = feas.build_action(session, run_id, spec_id)
    if action.get("error"):
        raise HTTPException(404, action)
    if action.get("tier") == "assemblable":
        result = feas.compose_modernization_submission(session, run_id, spec_id, user.email)
        if result.get("error"):
            raise HTTPException(422, result)
        return result
    return action


# ── serialization ──────────────────────────────────────────────────────────────

def _run_row(run: FeasibilityRun, session: Session, include_scores: bool) -> dict:
    try:
        scan_ids = json.loads(run.scan_ids_json or "[]") or ([run.scan_id] if run.scan_id else [])
    except (TypeError, ValueError):
        scan_ids = [run.scan_id] if run.scan_id else []
    try:
        spec_ids = json.loads(run.spec_ids_json or "[]") or []
    except (TypeError, ValueError):
        spec_ids = []
    try:
        schema_scoping = json.loads(getattr(run, "schema_scoping_json", "") or "{}") or {}
    except (TypeError, ValueError):
        schema_scoping = {}
    try:
        progress = json.loads(getattr(run, "progress_json", "") or "{}") or {}
    except (TypeError, ValueError):
        progress = {}
    try:
        summary = json.loads(run.summary_json or "{}")
    except (TypeError, ValueError):
        summary = {}
    row = {
        "id": run.id, "estate_id": run.estate_id, "scan_id": run.scan_id,
        "scan_ids": scan_ids, "spec_ids": spec_ids, "schema_scoping": schema_scoping,
        "domain": run.domain, "state": run.state, "used_skill": run.used_skill,
        "corpus_version": run.corpus_version, "evaluator_version": run.evaluator_version,
        "skill_version": run.skill_version, "embedding_model": run.embedding_model,
        # Reproducibility (G-lite): the R2 scoring levers, surfaced from the summary.
        "scoring": summary.get("scoring", {}) if isinstance(summary, dict) else {},
        "progress": progress,
        "summary": summary,
        "error": json.loads(run.error_json or "{}"),
        "created_by": run.created_by,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }
    if include_scores:
        scores = session.exec(
            select(FeasibilityScore).where(FeasibilityScore.run_id == run.id)
        ).all()
        row["scores"] = [_score_row(s, detail=False) for s in scores]
    return row


def _score_row(s: FeasibilityScore, detail: bool) -> dict:
    row = {
        "score_id": s.id,
        "spec_id": s.spec_id, "spec_name": s.spec_name, "domain": s.domain,
        "tier": s.tier, "evaluation_state": s.evaluation_state,
        "confidence": s.confidence, "required_coverage": s.required_coverage,
        "total_coverage": s.total_coverage, "best_product_uri": s.best_product_uri,
        "best_product_version": s.best_product_version,
        "adaptation_notes": s.adaptation_notes, "rationale": s.rationale,
    }
    if detail:
        row.update({
            "matched": json.loads(s.matched_json or "[]"),
            "gaps": json.loads(s.gaps_json or "[]"),
            "derivations": json.loads(s.derivations_json or "[]"),
            "join_plan": json.loads(s.join_plan_json or "{}"),
            "evidence": json.loads(s.evidence_json or "{}"),
        })
    return row


def _candidate_row(c: FeasibilityCandidate) -> dict:
    return {
        "id": c.id, "score_id": c.score_id, "run_id": c.run_id, "estate_id": c.estate_id,
        "spec_id": c.spec_id, "spec_name": c.spec_name, "domain": c.domain,
        "tier": c.tier, "required_coverage": c.required_coverage,
        "confidence": c.confidence, "rationale": c.rationale,
        "adaptation_notes": c.adaptation_notes, "notes": c.notes,
        "status": c.status, "saved_by": c.saved_by,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }
