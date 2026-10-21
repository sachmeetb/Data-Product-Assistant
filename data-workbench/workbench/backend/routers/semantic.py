"""Semantic-layer endpoints (`/api/semantic`).

Concept CRUD + the cross-product recommender + the deterministic scaffold +
the consolidated **discovery sequence** (`/discovery/{status,run,reset}` —
scaffold → recommend → enrich, with run-tracking, staleness, and stranded
detection in `semantic_discovery.py`). These DO create + mutate
`:BusinessConcept` graph nodes (recommend auto-promotes proposals into bound
concepts); `:BusinessConceptRecommendation` is the staging node the Review
Queue triages. See `docs/architecture/semantic-layer.md`.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlmodel import Session

from .. import business_concepts as concepts
from .. import semantic_discovery as discovery
from .. import semantic_recommender as recommender
from ..database import get_session
from ..models import AppSettings


router = APIRouter(prefix="/api/semantic", tags=["semantic"])


def _settings(session: Session) -> AppSettings:
    s = session.get(AppSettings, 1)
    if not s:
        s = AppSettings()
    return s


class _RecommendBody(BaseModel):
    trigger: str = "manual"  # 'manual' | 'scheduled' | 'rerun'


@router.post("/concepts/recommend")
async def recommend(
    body: _RecommendBody | None = None,
    session: Session = Depends(get_session),
):
    """Gather cross-project graph snapshot → call the business-concept-
    advisor skill → persist a `:RecommendationBatch` + per-concept
    `:BusinessConceptRecommendation` nodes. Always returns 200 with the
    persisted payload, even if the LLM call failed (advisor_error is
    populated, concepts list is empty)."""
    body = body or _RecommendBody()
    settings = _settings(session)
    try:
        return await discovery.run_recommender_pass(settings, body.trigger)
    except Exception as e:
        raise HTTPException(500, f"recommend failed: {e}")


@router.get("/concepts/latest")
def latest(session: Session = Depends(get_session)):
    """Return the most-recent batch with all its concepts expanded.
    `{batch: null}` (200) when no batch has been run yet."""
    settings = _settings(session)
    batch = recommender.read_latest(settings)
    return {"batch": batch}


class _RejectBody(BaseModel):
    reason: str = ""
    # Match the categories used elsewhere in the workbench for
    # ProductRequest rejections, with one extra for this surface.
    category: str = "other"


@router.post("/concepts/{concept_id}/reject")
def reject(
    concept_id: str,
    body: _RejectBody,
    session: Session = Depends(get_session),
):
    """Reject a recommendation. `concept_id` is the URI suffix after the
    batch_uri (e.g. ``semrec:batch:...:concept:00``). The Cypher
    matches on the full URI, so callers should pass the full URI."""
    settings = _settings(session)
    res = recommender.reject_concept(
        settings, concept_uri=concept_id, reason=body.reason, category=body.category,
    )
    if res.get("status") == "not_found":
        raise HTTPException(404, "Recommendation not found")
    return res


class _EditBody(BaseModel):
    concept_name: Optional[str] = None
    definition: Optional[str] = None


@router.patch("/concepts/{concept_id}")
def edit(
    concept_id: str,
    body: _EditBody,
    session: Session = Depends(get_session),
):
    settings = _settings(session)
    res = recommender.edit_concept(
        settings, concept_uri=concept_id,
        concept_name=body.concept_name, definition=body.definition,
    )
    if res.get("status") == "not_found":
        raise HTTPException(404, "Recommendation not found")
    return res


@router.get("/concepts/export.yaml")
def export_yaml(session: Session = Depends(get_session)):
    """Return the latest batch as a downloadable YAML document.
    Rejected recommendations are excluded; edited ones use their updated
    name + definition."""
    settings = _settings(session)
    batch = recommender.read_latest(settings)
    if not batch:
        raise HTTPException(404, "No recommendation batch found")
    yaml_text = recommender.to_export_yaml(batch)
    return Response(
        content=yaml_text,
        media_type="application/x-yaml",
        headers={
            "Content-Disposition": (
                f'attachment; filename="business-concepts-{batch.get("batch_id") or "latest"}.yaml"'
            ),
        },
    )


# ── Business Concept persistence (Phase 3b foundation) ───────────────────


class _CreateConceptBody(BaseModel):
    name: str
    definition: str
    domain: str
    level: str = "attribute"  # 'entity' | 'attribute' | 'value'
    value_token: Optional[str] = None
    predicate_template: Optional[str] = None
    parent_uri: Optional[str] = None
    represented_by_uris: Optional[list[str]] = None          # attribute ⟶ :DProdColumn
    represented_by_dataset_uris: Optional[list[str]] = None  # entity ⟶ :DProdOutputDataset
    synonyms: Optional[list[str]] = None
    created_by: Optional[str] = None


@router.post("/concepts")
def create_concept_endpoint(
    body: _CreateConceptBody,
    session: Session = Depends(get_session),
):
    """Create a :BusinessConcept manually (super or value, with optional
    parent + column bindings). Idempotent on URI when one is reused via
    the from-recommendation path; this manual path always mints a fresh
    URI."""
    settings = _settings(session)
    try:
        return concepts.create_concept(
            settings,
            name=body.name, definition=body.definition, domain=body.domain,
            level=body.level, value_token=body.value_token,
            predicate_template=body.predicate_template,
            parent_uri=body.parent_uri,
            represented_by_uris=body.represented_by_uris,
            represented_by_dataset_uris=body.represented_by_dataset_uris,
            synonyms=body.synonyms,
            created_by=body.created_by or "Data Steward",
        )
    except concepts.DuplicateConceptError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


class _AcceptFromRecBody(BaseModel):
    name: Optional[str] = None
    definition: Optional[str] = None
    domain: str  # REQUIRED — recommendations don't tag domain per concept
    level: str = "super"
    value_token: Optional[str] = None
    predicate_template: Optional[str] = None
    parent_uri: Optional[str] = None
    represented_by_uris: Optional[list[str]] = None
    synonyms: Optional[list[str]] = None
    created_by: Optional[str] = None
    # 'fail' preserves the historical contract (HTTP 409 on collision so
    # callers that want explicit confirmation still get it). The Steward
    # UI sends 'deprecate_existing' so re-accepting a same-named concept
    # auto-deprecates the prior one and proceeds.
    if_exists: str = "fail"


@router.post("/concepts/from-recommendation/{rec_uri:path}")
def accept_from_recommendation_endpoint(
    rec_uri: str,
    body: _AcceptFromRecBody,
    session: Session = Depends(get_session),
):
    """Promote a :BusinessConceptRecommendation to a real :BusinessConcept.
    The recommendation gets back-linked via ``acceptedAsConcept`` so the
    UI can show "Promoted" instead of the Accept button on subsequent loads."""
    settings = _settings(session)
    if body.if_exists not in ("fail", "deprecate_existing"):
        raise HTTPException(400, "if_exists must be 'fail' or 'deprecate_existing'")
    try:
        return concepts.accept_from_recommendation(
            settings,
            rec_uri=rec_uri,
            name=body.name, definition=body.definition,
            domain=body.domain, level=body.level,
            value_token=body.value_token, predicate_template=body.predicate_template,
            parent_uri=body.parent_uri,
            represented_by_uris=body.represented_by_uris,
            synonyms=body.synonyms,
            created_by=body.created_by or "Data Steward",
            if_exists=body.if_exists,  # type: ignore[arg-type]
        )
    except concepts.DuplicateConceptError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


class _AcceptAllPendingBody(BaseModel):
    created_by: Optional[str] = None
    # Per-rec domain overrides keyed by rec_uri. When omitted, each rec's
    # domain is inferred from its evidence-column lineage (most-cited
    # source-product domain wins; ties break alphabetically).
    domain_overrides: Optional[dict[str, str]] = None
    # Domain to use for recs whose evidence couldn't be resolved (no
    # :DProdColumn matched, or matched columns sit on a contract with no
    # domain set). When None, such recs are reported in ``skipped``.
    fallback_domain: Optional[str] = None


@router.post("/recommendations/accept-all")
def accept_all_pending_recommendations_endpoint(
    body: _AcceptAllPendingBody | None = None,
    session: Session = Depends(get_session),
):
    """Bulk-promote every ``status='pending'`` recommendation in the
    latest :RecommendationBatch. Each accept uses
    ``if_exists='deprecate_existing'`` so name collisions don't fail the
    batch — the colliding active concept is auto-deprecated and the new
    one created. Returns a triage summary ``{accepted, skipped, failures}``."""
    body = body or _AcceptAllPendingBody()
    settings = _settings(session)
    return concepts.accept_all_pending(
        settings,
        created_by=body.created_by or "Data Steward",
        domain_overrides=body.domain_overrides,
        fallback_domain=body.fallback_domain,
    )


@router.get("/concepts/tree")
def concepts_tree(domain: Optional[str] = None, session: Session = Depends(get_session)):
    """Return the concept tree as nested JSON.

    When ``domain`` is omitted (or empty), returns concepts across every
    domain. Each concept node retains its own ``domain`` field so the
    UI can chip it per row.
    """
    settings = _settings(session)
    return {"tree": concepts.get_tree(settings, domain)}


@router.get("/relationships")
def list_relationships_endpoint(
    domain: str,
    include_rejected: bool = False,
    session: Session = Depends(get_session),
):
    """List concept-to-concept :RELATES_TO edges in a domain (pending + active
    by default; pass include_rejected=true to also return rejected ones)."""
    settings = _settings(session)
    return {"relationships": concepts.list_relationships(settings, domain, include_rejected)}


class _RelationshipBody(BaseModel):
    from_uri: str
    to_uri: str
    kind: str = "related_to"
    status: str = "active"  # 'active' (approve) | 'rejected' | 'pending'


@router.post("/relationships")
def set_relationship_endpoint(
    body: _RelationshipBody,
    session: Session = Depends(get_session),
):
    """Approve / reject / (re)create a concept relationship. Approved (active)
    edges are what Concept-Guided retrieval expands as 1-hop neighbours."""
    settings = _settings(session)
    try:
        return concepts.set_relationship_status(
            settings, body.from_uri, body.to_uri, body.kind, body.status,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


class _EditConceptBody(BaseModel):
    uri: str
    name: Optional[str] = None
    definition: Optional[str] = None
    synonyms: Optional[list[str]] = None
    value_token: Optional[str] = None
    predicate_template: Optional[str] = None


# Body-based (uri in payload) to avoid colliding with the recommendation-edit
# route PATCH /concepts/{concept_id}.
@router.post("/concepts/update")
def edit_concept_endpoint(
    body: _EditConceptBody,
    session: Session = Depends(get_session),
):
    """In-place edit of a persisted :BusinessConcept's name / definition /
    synonyms (and, for a value, its token / predicate). Bindings + hierarchy
    edges are untouched."""
    settings = _settings(session)
    ok = concepts.update_concept_enrichment(
        settings, body.uri,
        name=body.name, definition=body.definition, synonyms=body.synonyms,
        value_token=body.value_token, predicate_template=body.predicate_template,
    )
    if not ok:
        raise HTTPException(404, "Concept not found or update failed")
    return {"uri": body.uri, "status": "updated"}


class _ConceptSearchBody(BaseModel):
    domain: str
    query: str
    k: int = 10


@router.post("/concepts/search")
def search_concepts_endpoint(
    body: _ConceptSearchBody,
    session: Session = Depends(get_session),
):
    """Semantic search over a domain's entities + attributes (vector index).
    Demonstrates that attributes resolve individually. Returns ranked matches
    with their level + owning entity; empty when embeddings are unavailable."""
    from .. import embeddings
    settings = _settings(session)
    if not body.query.strip():
        return {"matches": []}
    vec = embeddings.embed_query(body.query.strip())
    if not vec:
        return {"matches": []}
    hits = concepts.search_concepts_by_vector(settings, body.domain, vec, k=max(1, body.k))
    # Map each hit to its owning entity name (entities map to themselves).
    ent_name_by_uri = {
        e["uri"]: e["name"]
        for e in concepts.get_tree(settings, body.domain) if e.get("level") == "entity"
    }
    matches = []
    for h in hits:
        owning = concepts.resolve_entities(settings, [h["uri"]])
        matches.append({
            "uri": h["uri"], "name": h["name"], "level": h.get("level"),
            "score": round(float(h.get("score") or 0.0), 4),
            "entity_name": ent_name_by_uri.get(owning[0]) if owning else None,
        })
    return {"matches": matches}


@router.get("/concepts/diagram")
def concepts_diagram(
    domain: str, include_data: bool = True, session: Session = Depends(get_session),
):
    """Return a layered SVG of the domain's concept ontology (concept layer vs
    data-product layer). Pass ``domain=__all__`` for the cross-domain view
    (every domain as a lane + shared references + :CONSUMES arrows). Pass
    ``include_data=false`` to collapse the data layer (tables + columns)."""
    from .. import concept_svg

    settings = _settings(session)
    if not domain or not domain.strip():
        raise HTTPException(400, "domain is required")
    d = domain.strip()
    svg = (
        concept_svg.build_all_domains_svg(settings, include_data=include_data)
        if d == "__all__"
        else concept_svg.build_concept_svg(settings, d, include_data=include_data)
    )
    return {"svg": svg}


class _ScaffoldBody(BaseModel):
    domain: str
    dry_run: bool = True


@router.post("/entities/scaffold")
def scaffold_entities_endpoint(
    body: _ScaffoldBody,
    session: Session = Depends(get_session),
):
    """Derive a 3-tier entity/attribute/value model + FK-grounded relationships
    from the domain's schema. dry_run=true returns a preview (no writes);
    dry_run=false start-fresh deprecates the domain's concepts and persists the
    model (entities/attributes/values active, relationships pending)."""
    from .. import entity_scaffolding
    settings = _settings(session)
    if not body.domain or not body.domain.strip():
        raise HTTPException(400, "domain is required")
    return entity_scaffolding.scaffold(settings, body.domain.strip(), dry_run=body.dry_run)


@router.post("/entities/enrich")
async def enrich_entities_endpoint(
    body: _ScaffoldBody,
    session: Session = Depends(get_session),
):
    """LLM-enrich the domain's entity/attribute names, definitions, and
    synonyms (and re-embed). Best-effort — no-op when the SDK is unavailable."""
    from .. import entity_scaffolding
    settings = _settings(session)
    if not body.domain or not body.domain.strip():
        raise HTTPException(400, "domain is required")
    return await entity_scaffolding.enrich(settings, body.domain.strip())


class _BackfillEmbeddingsBody(BaseModel):
    domain: Optional[str] = None


@router.post("/concepts/backfill-embeddings")
def backfill_embeddings_endpoint(
    body: _BackfillEmbeddingsBody | None = None,
    session: Session = Depends(get_session),
):
    """Embed every active concept (optionally scoped to a domain) for
    Concept-Guided retrieval. Idempotent. Returns counts + whether the
    local embedding model is available."""
    settings = _settings(session)
    domain = (body.domain if body else None) or None
    return concepts.backfill_embeddings(settings, domain)


# ── Discovery sequence (consolidated concept-building) ───────────────────────
#
# One front door for the three concept-building steps + clear-out + status, so
# the UI's Discovery tab and the MCP tools share identical behaviour. The data-
# product layer is READ-ONLY throughout; every write targets :BusinessConcept
# (+ edges) or the :SemanticDiscoveryRun sidecar.


@router.get("/discovery/status")
def discovery_status(domain: str, session: Session = Depends(get_session)):
    """Per-step run state (has_run / last_run_at / stats / stale) for a domain,
    plus current concept counts and stranded (unbound) concepts."""
    settings = _settings(session)
    if not domain or not domain.strip():
        raise HTTPException(400, "domain is required")
    return discovery.get_status(settings, domain.strip())


class _DiscoveryResetBody(BaseModel):
    domain: str


@router.post("/discovery/reset")
def discovery_reset(
    body: _DiscoveryResetBody, session: Session = Depends(get_session),
):
    """Clear out the domain's concept layer (soft-deprecate every active concept
    + its edges to data products) so the sequence can be re-run from a clean
    slate. Audit history survives; readers exclude deprecated."""
    settings = _settings(session)
    if not body.domain or not body.domain.strip():
        raise HTTPException(400, "domain is required")
    return discovery.reset_domain(settings, body.domain.strip(), triggered_by="ui")


class _DiscoveryRunBody(BaseModel):
    domain: str
    step: str  # 'scaffold' | 'recommend' | 'enrich'
    dry_run: bool = False  # scaffold-only preview
    confidence_threshold: Optional[float] = None  # recommend-only auto-promote gate


@router.post("/discovery/run")
async def discovery_run(
    body: _DiscoveryRunBody, session: Session = Depends(get_session),
):
    """Run one discovery step for a domain, recording a :SemanticDiscoveryRun.

    - ``scaffold``: deterministic entity/attribute/value spine from schema.
      ``dry_run=true`` returns a preview WITHOUT writing or recording a run.
    - ``recommend``: cross-product advisor pass, then auto-promote proposals at
      or above ``confidence_threshold`` (default 0.7) into bound + parented
      attributes; lower-confidence proposals stay queued for review.
    - ``enrich``: LLM polish of names / definitions / synonyms (re-embed).
    """
    settings = _settings(session)
    try:
        return await discovery.run_step(
            settings, body.domain, body.step,
            dry_run=body.dry_run, confidence_threshold=body.confidence_threshold,
            triggered_by="ui",
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"{body.step} failed: {e}")


@router.delete("/concepts/{concept_uri:path}")
def deprecate_concept_endpoint(
    concept_uri: str,
    session: Session = Depends(get_session),
):
    """Soft-delete (deprecate) a concept. Edges remain so any provenance
    reads still resolve, but tree fetches exclude it.

    Refuses with HTTP 409 when the concept still has active VALUE
    children — the Steward must move or deprecate them first, otherwise
    they fall out of the tree as orphan roots (the bug the guard exists
    to prevent). The if_exists='deprecate_existing' accept path bypasses
    this because it migrates children to the replacement concept.
    """
    settings = _settings(session)
    try:
        res = concepts.deprecate_concept(settings, concept_uri)
    except concepts.ConceptHasActiveChildrenError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "has_active_children",
                "message": str(e),
                "uri": e.uri,
                "children": e.children,
            },
        )
    if res.get("status") == "not_found":
        raise HTTPException(404, "Concept not found")
    return res
