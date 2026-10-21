"""Handoff queue between the Product and Engineering workbenches.

A :class:`ProductRequest` row represents a Data Product Owner submitting a
new product, an edit to a published product, or an ingestion of an
existing-but-undocumented product. Engineering reads ``status='submitted'``
rows in the Incoming queue, accepts them (which flips the companion
:DataContract.lifecycleState to ``in_engineering``), runs the pipeline, and
marks the request complete when the spec is ready for the PO to formalize.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..database import get_session
from ..models import (
    IntakePendingDependency,
    Project,
    ProductRequest,
    ProductRequestKind,
    ProductRequestStatus,
    StageRun,
    StageStatus,
    Workflow,
)
from ..neo4j_client import neo4j_session
from ..request_guard import enforcement_enabled
from .. import osi as osi_engine


import json as _json


def _mark_stage_complete(
    session: Session,
    project_id: int,
    workflow_id: str,
    stage_id: str,
) -> None:
    """Find the StageRun for (project, workflow, stage_id) and mark it
    complete. Looks up stage_number by iterating the workflow's stored
    workflow_json since StageRun doesn't carry stage_id directly.
    No-op if the workflow or stage row isn't found — keeps the submit
    path robust against archetype/workflow mismatches."""
    workflow = session.exec(
        select(Workflow)
        .where(Workflow.project_id == project_id)
        .where(Workflow.workflow_id == workflow_id)
    ).first()
    if not workflow:
        return
    try:
        stages = _json.loads(workflow.workflow_json or "[]")
    except Exception:
        return
    enabled = [s for s in stages if s.get("enabled", True)]
    stage_number = next(
        (i for i, s in enumerate(enabled, 1) if s.get("stage_id") == stage_id),
        None,
    )
    if stage_number is None:
        return
    run = session.exec(
        select(StageRun)
        .where(StageRun.project_id == project_id)
        .where(StageRun.workflow_id == workflow_id)
        .where(StageRun.stage_number == stage_number)
    ).first()
    if not run or run.status == StageStatus.complete:
        return
    run.status = StageStatus.complete
    run.completed_at = datetime.utcnow()
    session.add(run)


router = APIRouter(tags=["product-requests"])


# ── Models ────────────────────────────────────────────────────────────────


class SubmitRequestBody(BaseModel):
    kind: ProductRequestKind
    submitted_by: str
    notes: Optional[str] = None
    # Optional cross-link back to a consumer-aligned ingest draft whose
    # "Create now" gap launched this source-product wizard. Lets the engineer
    # and the resume page render "spawned for ingest #X" context.
    parent_ingest_draft_id: Optional[int] = None


class AcceptRequestBody(BaseModel):
    engineer: str


class CompleteRequestBody(BaseModel):
    completion_notes: Optional[str] = None


class RejectRequestBody(BaseModel):
    reason: str
    category: Optional[str] = None
    engineer: Optional[str] = None


REJECTION_CATEGORIES = {
    "missing_context": "Missing domain context",
    "too_broad": "Schema too broad",
    "too_narrow": "Schema too narrow",
    "unclear_purpose": "Unclear purpose",
    "unclear_quality_rules": "Data quality requirements unclear",
    "duplicate": "Duplicate of existing product",
    "out_of_scope": "Out of scope",
    "other": "Other",
}


WRITE_REJECTION_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $engineer})
  ON CREATE SET agent.agentType = 'human', agent.name = $engineer
CREATE (act:ProvActivity {
  uri: 'prov:activity:contract-reject:' + coalesce((dc.id + ':v' + toString(dc.currentVersion)), dc.id) + ':' + toString($occurred_at_ms),
  activityType: 'ContractReject',
  category: $category,
  reason: $reason,
  occurredAt: datetime()
})
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:PROV_USED]->(dc)
SET dc.currentLifecycleState = 'rejected',
    dc.lastRejectionCategory = $category,
    dc.lastRejectionReason = $reason,
    dc.lastRejectedBy = $engineer,
    dc.lastRejectedAt = datetime()
RETURN (dc.id + ':v' + toString(dc.currentVersion)) AS versioned_id
"""


def _persist_rejection_provenance(
    project: Project,
    contract_id: str,
    engineer: str,
    category: str,
    reason: str,
) -> Optional[str]:
    """Mark the contract rejected + drop a PROV-O activity. Returns the
    contract's versionedId on success, None if the Neo4j write fails."""
    import time
    try:
        with _neo4j(project) as ns:
            row = ns.run(
                WRITE_REJECTION_QUERY,
                contract_id=contract_id,
                engineer=engineer,
                category=category,
                reason=reason,
                occurred_at_ms=int(time.time() * 1000),
            ).single()
            return row["versioned_id"] if row else None
    except Exception:
        return None


# ── Helpers ───────────────────────────────────────────────────────────────


def _get_project(project_id: int, session: Session) -> Project:
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def _neo4j(project: Project):
    return neo4j_session(
        project.neo4j_host,
        project.neo4j_port,
        project.neo4j_user,
        project.neo4j_password,
        project.neo4j_database,
    )


def _set_lifecycle_state(project: Project, contract_id: str, state: str) -> Optional[str]:
    """Flip the head contract's lifecycleState and return its versionedId.

    Keeps the current :ContractVersion sidecar's lifecycleState in sync —
    without this, transitions like submitted → in_engineering → approved
    only land on :DataContract.currentLifecycleState, leaving the cv stuck
    at the value it had when the version was branched (usually 'draft').
    The marketplace's deployed-version pin filters cv.lifecycleState, so
    a stale cv breaks /edit-diff and /upstream-drift.
    """
    try:
        with _neo4j(project) as ns:
            row = ns.run(
                "MATCH (dc:DataContract {id: $contract_id}) "
                "SET dc.currentLifecycleState = $state "
                "WITH dc "
                "OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv:ContractVersion {version: dc.currentVersion}) "
                "SET cv.lifecycleState = $state "
                "RETURN (dc.id + ':v' + toString(dc.currentVersion)) AS versioned_id",
                contract_id=contract_id,
                state=state,
            ).single()
            return row["versioned_id"] if row else None
    except Exception:
        return None


def _trigger_osi_evaluation(project: Project, contract_id: str, trigger: str) -> None:
    """Fire a deterministic OSI evaluation on a lifecycle transition.

    The advisor is intentionally skipped here — it can take 30-60s and we
    don't want a slow LLM call holding up the submit/approve transaction.
    The deterministic eval (band/checklist/conformance) lands immediately;
    the PO can re-run with the advisor from the wizard's Score now button
    to fill in the narrative.

    Failures are swallowed: an unreachable graph or partial data must not
    fail the lifecycle transition itself.
    """
    try:
        osi_engine.evaluate(project, contract_id, triggered_by=trigger, narrative=None)
    except Exception:
        pass


def _request_payload(request: ProductRequest, project: Optional[Project] = None) -> dict:
    return {
        "id": request.id,
        "project_id": request.project_id,
        "project_code": project.project_code if project else None,
        "project_name": project.name if project else None,
        "archetype": project.archetype if project else None,
        "contract_id": request.contract_id,
        "versioned_id": request.contract_versioned_id,
        "kind": request.kind.value,
        "status": request.status.value,
        "submitted_by": request.submitted_by,
        "submitted_at": request.submitted_at.isoformat() if request.submitted_at else None,
        "accepted_by": request.accepted_by,
        "accepted_at": request.accepted_at.isoformat() if request.accepted_at else None,
        "completed_at": request.completed_at.isoformat() if request.completed_at else None,
        "engineer_assigned": request.engineer_assigned,
        "notes": request.notes,
    }


# ── Submit ────────────────────────────────────────────────────────────────


@router.post("/api/projects/{project_id}/product-requests/submit")
def submit_product_request(
    project_id: int,
    body: SubmitRequestBody,
    session: Session = Depends(get_session),
):
    """Submit a new/edit request from the Product Workbench.

    Ingest requests are created by the dedicated ``ingest-existing-product``
    endpoint in odcs.py — they seed both the contract shell and the queue
    entry atomically.
    """
    if body.kind == ProductRequestKind.ingest:
        raise HTTPException(
            400,
            "Ingest requests must go through POST /odcs/ingest-existing-product",
        )

    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"
    versioned_id = _set_lifecycle_state(project, contract_id, "submitted")

    # PO has finished their part by the time they submit — close out the
    # odcs_specification stage so the engineer sees a clean handoff when
    # they expand the Product Owner context banner.
    _mark_stage_complete(session, project_id, "product_definition", "odcs_specification")

    # Snapshot the producer-side OSI score at handoff so the engineer's
    # marketplace and the PO's My Products list both see a fresh band.
    _trigger_osi_evaluation(project, contract_id, "submit")

    request = ProductRequest(
        project_id=project_id,
        contract_id=contract_id,
        contract_versioned_id=versioned_id,
        kind=body.kind,
        submitted_by=body.submitted_by,
        notes=body.notes,
        parent_ingest_draft_id=body.parent_ingest_draft_id,
    )
    session.add(request)
    session.commit()
    session.refresh(request)
    return _request_payload(request, project)


# ── List (engineer queue) ─────────────────────────────────────────────────


@router.get("/api/product-requests")
def list_product_requests(
    status: Optional[ProductRequestStatus] = None,
    kind: Optional[ProductRequestKind] = None,
    session: Session = Depends(get_session),
):
    """List product requests across all projects, newest first."""
    stmt = select(ProductRequest).order_by(ProductRequest.submitted_at.desc())
    if status:
        stmt = stmt.where(ProductRequest.status == status)
    if kind:
        stmt = stmt.where(ProductRequest.kind == kind)
    rows = session.exec(stmt).all()

    project_cache: dict[int, Project] = {}
    payloads = []
    for r in rows:
        project = project_cache.get(r.project_id)
        if project is None:
            project = session.get(Project, r.project_id)
            if project:
                project_cache[r.project_id] = project
        payloads.append(_request_payload(r, project))
    return {"requests": payloads}


@router.get("/api/my-products/in-flight")
def list_in_flight_products(
    owner_email: str,
    session: Session = Depends(get_session),
):
    """In-flight SA product requests for a PO's dashboard.

    Marketplace listings cover *published* products. SA requests live in a
    pre-contract state (no :DataContract until materialization), so the PO
    has no surface to see them otherwise. Returns one card per submitted /
    accepted ProductRequest belonging to the caller, with a derived
    status_label keyed off ``Project.discovery_complete_at``:

      submitted      → "Awaiting engineer acceptance"
      accepted, no discovery_complete_at  → "In discovery"
      accepted, discovery_complete_at set → "Ready for your validation"

    ``complete`` and ``rejected`` rows are filtered out — completed SA
    requests are visible on the published marketplace listing; rejected
    ones reopen via the PO's wizard rejection banner.
    """
    email = (owner_email or "").strip().lower()
    if not email:
        return {"requests": []}

    rows = session.exec(
        select(ProductRequest)
        .where(ProductRequest.status.in_([
            ProductRequestStatus.submitted,
            ProductRequestStatus.accepted,
        ]))
        .order_by(ProductRequest.submitted_at.desc())
    ).all()

    out: list[dict] = []
    for r in rows:
        project = session.get(Project, r.project_id)
        if not project or project.archetype != "dpe-sa":
            continue
        # A product is "mine" if I submitted the request OR I own the project.
        # Intake-scaffolded SA products carry the PO on Project.owner_email (the
        # request's submitted_by may be a machine principal, e.g. intake:*), so
        # match either — keeping this surface consistent with PO-MCP list_my_products.
        owns = (
            (r.submitted_by or "").strip().lower() == email
            or (project.owner_email or "").strip().lower() == email
        )
        if not owns:
            continue

        if r.status == ProductRequestStatus.submitted:
            status_label = "Awaiting engineer acceptance"
            ready_for_validation = False
        elif project.discovery_complete_at is not None:
            status_label = "Ready for your validation"
            ready_for_validation = True
        else:
            status_label = "In discovery"
            ready_for_validation = False

        out.append({
            "request_id": r.id,
            "project_id": project.id,
            "project_code": project.project_code,
            "name": project.name,
            "domain": project.domain,
            "product_idea": project.product_idea,
            "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
            "discovery_complete_at": (
                project.discovery_complete_at.isoformat() if project.discovery_complete_at else None
            ),
            "status": r.status.value,
            "status_label": status_label,
            "ready_for_validation": ready_for_validation,
        })
    return {"requests": out}


# Lifecycle states that mean "a saved contract exists but isn't published yet".
# (DISTINCT from _graph_helpers.DRAFT_STATES, which includes None for "no contract";
# here None means the project has no contract at all → exclude it.)
_DRAFT_LIFECYCLE_STATES = {"draft", "ingesting"}

_READ_CONTRACT_LIFECYCLE = """\
MATCH (dc:DataContract {id: $contract_id})
RETURN coalesce(dc.currentLifecycleState, 'draft') AS lifecycle_state
"""


def _read_contract_lifecycle(project: Project, session: Session) -> Optional[str]:
    """Best-effort read of a project's contract lifecycle state from the graph.

    Returns the lifecycle string (e.g. 'draft' / 'published'), or None when no
    :DataContract exists yet OR the graph is unreachable (offline tests). Isolated
    + monkeypatchable so list_derived_drafts is testable without Neo4j."""
    contract_id = f"{project.project_code}-contract"
    try:
        with neo4j_session(
            project.neo4j_host, project.neo4j_port, project.neo4j_user,
            project.neo4j_password, project.neo4j_database,
        ) as ns:
            row = ns.run(_READ_CONTRACT_LIFECYCLE, contract_id=contract_id).single()
            return row["lifecycle_state"] if row else None
    except Exception:
        return None


@router.get("/api/my-products/derived-drafts")
def list_derived_drafts(owner_email: str, session: Session = Depends(get_session)):
    """Owner-scoped consumer-aligned (dpe-cf) products still in DRAFT — the PO's home
    for a derived/aggregate product scaffolded from intake (or saved mid-wizard) that
    isn't published yet.

    A dpe-cf draft has no ProductRequest, no :DProdDataProduct, and isn't on the
    marketplace, so none of the other My Products lists surface it. Each row carries
    upstream source-dependency readiness (from IntakePendingDependency + each source's
    Project.published_at) so the PO knows whether its sources have published — the
    signal for "ready to wire the mappings in the wizard". Published/submitted dpe-cf
    products are excluded (they appear on the marketplace list instead)."""
    email = (owner_email or "").strip().lower()
    if not email:
        return {"drafts": []}

    projects = session.exec(
        select(Project).where(Project.archetype == "dpe-cf")
    ).all()

    out: list[dict] = []
    for p in projects:
        if (p.owner_email or "").strip().lower() != email:
            continue
        lifecycle = _read_contract_lifecycle(p, session)
        if lifecycle not in _DRAFT_LIFECYCLE_STATES:
            continue  # no saved contract, or already published/submitted

        deps = session.exec(
            select(IntakePendingDependency).where(
                IntakePendingDependency.consumer_project_id == p.id
            )
        ).all()
        sources: list[dict] = []
        ready = 0
        bound = 0
        for d in deps:
            src = session.get(Project, d.source_project_id) if d.source_project_id else None
            is_ready = bool(src and src.published_at is not None)
            if is_ready:
                ready += 1
            # 'bound' = this dependency's :CONSUMES edge has been wired (flipped
            # by resolve_pending_dependencies on a consumer contract save). This
            # is DISTINCT from 'ready': ready = the source has published (can be
            # wired); bound = it actually has been.
            is_bound = d.status == "bound"
            if is_bound:
                bound += 1
            sources.append({
                "name": src.name if src else (d.source_candidate_id or d.source_external_uri or "source"),
                "project_code": src.project_code if src else None,
                "ready": is_ready,
                "bound": is_bound,
            })

        out.append({
            "project_id": p.id,
            "project_code": p.project_code,
            "name": p.name,
            "domain": p.domain,
            "product_idea": p.product_idea,
            "lifecycle_state": lifecycle,
            "from_intake": p.parent_intake_submission_id is not None,
            "dependencies_total": len(deps),
            "dependencies_ready": ready,
            "dependencies_bound": bound,
            "sources": sources,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        })
    return {"drafts": out}


@router.get("/api/projects/{project_id}/product-requests/latest")
def latest_product_request(
    project_id: int,
    session: Session = Depends(get_session),
):
    """Most recent non-rejected ProductRequest for a project.

    Used by the engineer's `mark_engineering_complete` stage handler so the
    frontend doesn't have to keep the request id around — it just asks "for
    this project, which request am I working on?". Excludes rejected rows so
    a stale rejection doesn't get marked complete by accident. Returns 404
    if no eligible request exists (e.g. project never had a submission).
    """
    project = _get_project(project_id, session)
    request = session.exec(
        select(ProductRequest)
        .where(ProductRequest.project_id == project_id)
        .where(ProductRequest.status != ProductRequestStatus.rejected)
        .order_by(ProductRequest.submitted_at.desc())
    ).first()
    if not request:
        raise HTTPException(404, "No active product request for this project")
    return _request_payload(request, project)


# ── Transitions ───────────────────────────────────────────────────────────


def _get_request(request_id: int, session: Session) -> ProductRequest:
    request = session.get(ProductRequest, request_id)
    if not request:
        raise HTTPException(404, "Product request not found")
    return request


@router.post("/api/product-requests/{request_id}/accept")
def accept_product_request(
    request_id: int,
    body: AcceptRequestBody,
    session: Session = Depends(get_session),
):
    request = _get_request(request_id, session)
    project = _get_project(request.project_id, session)
    # Idempotent: re-accepting an already-accepted request is a no-op success
    # rather than a 400 — the MCP wrapper already behaves this way, and callers
    # (wizard re-submit, retried automation) shouldn't have to special-case it.
    # Only genuinely terminal states (complete/rejected) refuse.
    if request.status == ProductRequestStatus.accepted:
        payload = _request_payload(request, project)
        payload["note"] = "already accepted — no action taken."
        return payload
    if request.status != ProductRequestStatus.submitted:
        raise HTTPException(400, f"Cannot accept request in status={request.status.value}")
    _set_lifecycle_state(project, request.contract_id, "in_engineering")
    request.status = ProductRequestStatus.accepted
    request.accepted_by = body.engineer
    request.engineer_assigned = body.engineer
    request.accepted_at = datetime.utcnow()
    session.add(request)
    session.commit()
    session.refresh(request)

    # Phase 7: when accepting an 'edit' request, auto-run reconciliation —
    # deactivate orphaned mappings for removed columns and reset only the
    # stages that genuinely need re-running. The engineer no longer has to
    # click "Reset affected stages" manually for the common path. Failures
    # are non-fatal so the accept itself always succeeds; the engineer can
    # trigger reconciliation manually via the banner button if needed.
    reconciliation_summary = None
    if request.kind == ProductRequestKind.edit:
        try:
            from .edits import apply_reconciliation
            reconciliation_summary = apply_reconciliation(project, session)
        except Exception:
            reconciliation_summary = None

    payload = _request_payload(request, project)
    if reconciliation_summary is not None:
        payload["reconciliation"] = reconciliation_summary
    return payload


@router.post("/api/product-requests/{request_id}/complete")
def complete_product_request(
    request_id: int,
    body: CompleteRequestBody,
    session: Session = Depends(get_session),
):
    request = _get_request(request_id, session)
    if request.status not in (ProductRequestStatus.accepted, ProductRequestStatus.submitted):
        raise HTTPException(400, f"Cannot complete request in status={request.status.value}")
    # Terminal engineering completion should require an *accepted* request — a
    # product was never explicitly picked up if it's still 'submitted'. Under
    # warn-only rollout we still allow it (the mark_engineering_complete stage
    # auto-fires this); once WB_ENFORCE_ACCEPT_GATE is on, completing from
    # 'submitted' is refused so acceptance can't be skipped end-to-end.
    if request.status == ProductRequestStatus.submitted and enforcement_enabled():
        raise HTTPException(
            409,
            "Cannot complete engineering work while the request is still "
            "'submitted' — accept it first (required_action: accept_request).",
        )
    project = _get_project(request.project_id, session)
    # dpe-sa products skip the PO Deploy gesture: the PO already validated
    # names/descriptions/rules upstream, so engineer "I'm done" auto-
    # publishes. dpe-cf still parks at 'approved' and waits for the PO to
    # click Deploy on the marketplace.
    if (project.archetype or "").strip() == "dpe-sa":
        _set_lifecycle_state(project, request.contract_id, "published")
        try:
            with neo4j_session(
                project.neo4j_host, project.neo4j_port,
                project.neo4j_user, project.neo4j_password, project.neo4j_database,
            ) as ns:
                ns.run(
                    "MATCH (dp:DProdDataProduct) "
                    "WHERE dp.uri STARTS WITH 'dprod:' + $project_code "
                    "SET dp.status = 'published', "
                    "    dp.publishedAt = datetime(), "
                    "    dp.publishedBy = $user",
                    project_code=project.project_code,
                    user="dpe-sa-engineer",
                )
        except Exception:
            pass
        try:
            project.published_at = datetime.utcnow()
            session.add(project)
        except Exception:
            pass
    else:
        _set_lifecycle_state(project, request.contract_id, "approved")
    # Re-score after engineering — engineer-side enrichment (descriptions
    # approved, mappings landed, transforms expressed) typically lifts the
    # band by the time the PO gets the product back.
    _trigger_osi_evaluation(project, request.contract_id, "signoff")
    request.status = ProductRequestStatus.complete
    request.completed_at = datetime.utcnow()
    if body.completion_notes:
        request.notes = ((request.notes or "") + f"\n---\nCompletion: {body.completion_notes}").strip()
    session.add(request)
    session.commit()
    session.refresh(request)
    return _request_payload(request, project)


@router.post("/api/product-requests/{request_id}/reject")
def reject_product_request(
    request_id: int,
    body: RejectRequestBody,
    session: Session = Depends(get_session),
):
    request = _get_request(request_id, session)
    if request.status == ProductRequestStatus.complete:
        raise HTTPException(400, "Cannot reject a completed request")
    project = _get_project(request.project_id, session)

    category = body.category if body.category in REJECTION_CATEGORIES else "other"
    engineer = (body.engineer or request.engineer_assigned or "unknown").strip() or "unknown"
    reason = (body.reason or "").strip()

    # Persist rejection as PROV-O on the contract and flip lifecycleState
    # to 'rejected' (distinct from 'draft' so the PO's My Products view
    # surfaces it clearly). Falls back to draft if the graph write fails.
    versioned_id = _persist_rejection_provenance(
        project, request.contract_id, engineer, category, reason
    )
    if versioned_id is None:
        _set_lifecycle_state(project, request.contract_id, "rejected")

    request.status = ProductRequestStatus.rejected
    label = REJECTION_CATEGORIES.get(category, category)
    note_line = f"Rejected [{label}] by {engineer}: {reason or '(no further detail)'}"
    request.notes = ((request.notes or "") + f"\n---\n{note_line}").strip()
    session.add(request)
    session.commit()
    session.refresh(request)
    return _request_payload(request, project)


@router.get("/api/product-requests/rejection-categories")
def list_rejection_categories():
    """Reject dialog populates its dropdown from here so backend is the
    source of truth on categorical options."""
    return {"categories": [{"value": k, "label": v} for k, v in REJECTION_CATEGORIES.items()]}
