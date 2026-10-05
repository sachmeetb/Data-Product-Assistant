"""Inbound-intake REST surface (`/api/intake/*`).

Two trust tiers:
  • ``POST /submit`` — the external machine's only entry point. Authenticated by
    a **scoped machine credential** (``WB_INTAKE_TOKENS``), NOT a user JWT; the
    path is allow-listed from the JWT middleware in ``main.py`` and does its own
    bearer check here. ``source_system`` is derived from the token, never trusted
    from the body. Idempotent on ``(source_system, external_ref)``.
  • everything else — practitioner review/approve; ordinary user JWT +
    read-only rules apply via the global middleware.

Submission only stages a row; the leased worker (``intake_worker``) parses it
asynchronously into a blueprint. Approval runs the resumable scaffold saga
(``intake_scaffold``).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from .. import config
from .. import intake_blueprint as bp
from .. import intake_platform
from .. import intake_scaffold
from ..auth import AuthUser, current_user
from ..authz import require_role
from ..database import get_session
from ..models import (
    FeasibilityRun, FeasibilityScore, IntakeEvent, IntakeSpawn, IntakeSubmission,
    ProductAssembly,
)

router = APIRouter(prefix="/api/intake", tags=["intake"])


# ── auth: scoped machine credential ─────────────────────────────────────────

def require_intake_principal(authorization: str = Header(default="")) -> str:
    """Resolve the external caller's ``source_system`` from its bearer token."""
    tokens = config.intake_tokens()
    if not tokens:
        raise HTTPException(
            503, "Inbound intake is not configured (set WB_INTAKE_TOKENS)."
        )
    tok = authorization[7:].strip() if authorization[:7].lower() == "bearer " else ""
    source_system = tokens.get(tok)
    if not source_system:
        raise HTTPException(
            401, "Invalid intake credential.", headers={"WWW-Authenticate": "Bearer"}
        )
    return source_system


# ── request models ───────────────────────────────────────────────────────────

class ContentPart(BaseModel):
    kind: str = "text"          # text | table | odcs | json
    title: str = ""
    media_type: Optional[str] = None
    body: Any = ""


class IntakeEnvelope(BaseModel):
    scenario: str               # V1: explicit "migration" | "modernization"
    external_ref: str           # idempotency key (with the token's source_system)
    content: list[ContentPart] = []
    hints: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
    # Any source_system in the body is IGNORED — provenance is the token's.


class BlueprintPatch(BaseModel):
    expected_revision: int
    blueprint: dict[str, Any]


class ExecutionModeBody(BaseModel):
    execution_mode: str  # live | schema_only


class ApproveBody(BaseModel):
    expected_revision: int


class RejectBody(BaseModel):
    reason: str = ""


# ── helpers ──────────────────────────────────────────────────────────────────

def _event(session: Session, sid: int, actor: str, to: str, detail: str = "", frm: str = "") -> None:
    session.add(
        IntakeEvent(intake_submission_id=sid, actor=actor, from_status=frm or None, to_status=to, detail=detail)
    )


def _blueprint_of(sub: IntakeSubmission) -> Optional[dict[str, Any]]:
    if not sub.blueprint_json:
        return None
    try:
        return json.loads(sub.blueprint_json)
    except (json.JSONDecodeError, ValueError):
        return None


def _serialise(sub: IntakeSubmission) -> dict[str, Any]:
    meta = None
    if sub.parse_meta_json:
        try:
            meta = json.loads(sub.parse_meta_json)
        except (json.JSONDecodeError, ValueError):
            meta = None
    return {
        "id": sub.id,
        "source_system": sub.source_system,
        "external_ref": sub.external_ref,
        "scenario": sub.scenario,
        "status": sub.status,
        "execution_mode": getattr(sub, "execution_mode", "live"),
        "ingestion_mode": getattr(sub, "ingestion_mode", "parsed"),
        "blueprint": _blueprint_of(sub),
        "blueprint_revision": sub.blueprint_revision,
        "parse_meta": meta,
        "reviewed_by": sub.reviewed_by,
        "created_at": sub.created_at.isoformat() if sub.created_at else None,
        "updated_at": sub.updated_at.isoformat() if sub.updated_at else None,
    }


def _approval_blockers(blueprint: Optional[dict[str, Any]]) -> list[str]:
    """Reasons the blueprint may not yet be scaffolded (mirrors the UI gate)."""
    if not blueprint:
        return ["no blueprint yet"]
    problems: list[str] = []
    if blueprint.get("gaps"):
        problems.append(f"{len(blueprint['gaps'])} unresolved gap(s)")
    scenario = blueprint.get("scenario")
    if scenario == "migration":
        name = (blueprint.get("project_name") or {}).get("value")
        if not name:
            problems.append("project_name is missing")
    elif scenario == "modernization":
        if not (blueprint.get("source_aligned") or blueprint.get("consumer_aligned")):
            problems.append("no product candidates to scaffold")
    return problems


def _current_user_email(request_user: Any) -> str:
    return getattr(request_user, "email", None) or "workbench-user"


# ── endpoints ──────────────────────────────────────────────────────────────

@router.post("/submit")
def submit_intake(
    env: IntakeEnvelope,
    source_system: str = Depends(require_intake_principal),
    session: Session = Depends(get_session),
):
    scenario = (env.scenario or "").strip().lower()
    if scenario not in ("migration", "modernization"):
        raise HTTPException(422, "scenario must be 'migration' or 'modernization'.")
    external_ref = (env.external_ref or "").strip()
    if not external_ref:
        raise HTTPException(422, "external_ref is required (idempotency key).")
    if not env.content:
        raise HTTPException(422, "content[] must be non-empty.")

    payload = env.model_dump()
    payload["source_system"] = source_system  # record derived provenance

    existing = session.exec(
        select(IntakeSubmission).where(
            IntakeSubmission.source_system == source_system,
            IntakeSubmission.external_ref == external_ref,
        )
    ).first()
    if existing is not None:
        if existing.status in ("approved", "scaffolding", "scaffolded"):
            raise HTTPException(409, f"submission already {existing.status}; cannot re-submit.")
        existing.raw_payload_json = json.dumps(payload, default=str)
        existing.scenario = scenario
        existing.status = "received"
        existing.lease_owner = None
        existing.lease_expires_at = None
        existing.updated_at = datetime.utcnow()
        session.add(existing)
        _event(session, existing.id, source_system, "received", "re-submitted (idempotent)")
        session.commit()
        return {"intake_id": existing.id, "status": existing.status, "resubmitted": True}

    sub = IntakeSubmission(
        source_system=source_system,
        external_ref=external_ref,
        scenario=scenario,
        status="received",
        raw_payload_json=json.dumps(payload, default=str),
    )
    session.add(sub)
    session.commit()
    session.refresh(sub)
    _event(session, sub.id, source_system, "received", "submitted")
    session.commit()
    return {"intake_id": sub.id, "status": sub.status}


@router.get("")
def list_intake(
    scenario: Optional[str] = None,
    status: Optional[str] = None,
    session: Session = Depends(get_session),
):
    stmt = select(IntakeSubmission).order_by(IntakeSubmission.updated_at.desc())  # type: ignore[arg-type]
    if scenario:
        stmt = stmt.where(IntakeSubmission.scenario == scenario)
    if status:
        stmt = stmt.where(IntakeSubmission.status == status)
    rows = session.exec(stmt).all()
    return {"submissions": [_serialise(s) for s in rows], "count": len(rows)}


@router.get("/origin/by-project/{project_id}")
def intake_origin_for_project(project_id: int, session: Session = Depends(get_session)):
    """Provenance + parsed inventory for a project scaffolded from intake.

    404 when the project wasn't intake-originated. The parsed inventory is the
    *pre-discovery* expectation from the external assessment — real discovery
    remains authoritative and populates the graph; this panel just keeps the
    input visible instead of stranding it on the submission.
    """
    spawn = session.exec(
        select(IntakeSpawn).where(IntakeSpawn.child_project_id == project_id)
    ).first()
    if spawn is None:
        raise HTTPException(404, "Project was not scaffolded from intake.")
    sub = session.get(IntakeSubmission, spawn.intake_submission_id)
    if sub is None:
        raise HTTPException(404, "Originating intake submission not found.")

    # Partial-scaffold repair: IntakeSpawn is the source of truth, so if the
    # denormalized Project marker is missing/stale, backfill it on read.
    from ..models import Project

    project = session.get(Project, project_id)
    if project is not None and project.parent_intake_submission_id != sub.id:
        project.parent_intake_submission_id = sub.id
        session.add(project)
        session.commit()

    blueprint = _blueprint_of(sub) or {}

    # Candidate-scoped inventory: migration counts top-level datasets; a
    # modernization child (dpe-sa / dpe-cf) must count ONLY its own candidate's
    # datasets, resolved via IntakeSpawn.candidate_id — otherwise a portfolio
    # child reports zero (top-level modernization has no `datasets`).
    rationale = blueprint.get("rationale") or ""
    datasets: list[dict] = []
    if sub.scenario == "modernization":
        pools = (blueprint.get("source_aligned") or []) + (blueprint.get("consumer_aligned") or [])
        candidate = next(
            (c for c in pools if c.get("candidate_id") == spawn.candidate_id), None
        )
        if candidate is not None:
            datasets = candidate.get("datasets") or []
    else:
        datasets = blueprint.get("datasets") or []

    dataset_count = len(datasets)
    column_count = sum(len(ds.get("columns") or []) for ds in datasets)
    return {
        "intake_id": sub.id,
        "source_system": sub.source_system,
        "external_ref": sub.external_ref,
        "scenario": sub.scenario,
        "rationale": rationale,
        "spawn_kind": spawn.kind,
        "candidate_id": spawn.candidate_id,
        "dataset_count": dataset_count,
        "column_count": column_count,
        # Candidate-scoped datasets (the inventory the panel renders) plus the
        # full blueprint for any richer view.
        "datasets": datasets,
        "blueprint": blueprint,
        "submitted_at": sub.created_at.isoformat() if sub.created_at else None,
    }


@router.get("/{intake_id}")
def get_intake(intake_id: int, session: Session = Depends(get_session)):
    sub = session.get(IntakeSubmission, intake_id)
    if sub is None:
        raise HTTPException(404, "Intake submission not found.")
    blueprint = _blueprint_of(sub)
    payload = _serialise(sub)
    payload["approval_blockers"] = _approval_blockers(blueprint)
    payload["platform_advisories"] = intake_platform.platform_advisories(blueprint)
    return payload


@router.patch("/{intake_id}/blueprint")
def patch_blueprint(
    intake_id: int, body: BlueprintPatch, session: Session = Depends(get_session)
):
    sub = session.get(IntakeSubmission, intake_id)
    if sub is None:
        raise HTTPException(404, "Intake submission not found.")
    if sub.status in ("approved", "scaffolding", "scaffolded", "rejected"):
        raise HTTPException(409, f"submission is {sub.status}; blueprint is frozen.")
    if body.expected_revision != sub.blueprint_revision:
        raise HTTPException(
            409,
            {
                "message": "stale blueprint_revision; reload before editing.",
                "current_revision": sub.blueprint_revision,
            },
        )
    # Edits must stay schema-valid (a practitioner can't save a broken blueprint).
    try:
        model = bp.parse_blueprint(body.blueprint)
    except bp.BlueprintValidationError as e:
        raise HTTPException(422, f"edited blueprint failed validation: {e}")
    bp.normalize_ids(model)
    sub.blueprint_json = json.dumps(model.model_dump(mode="json"), default=str)
    sub.blueprint_revision += 1
    sub.status = "reviewing"
    sub.updated_at = datetime.utcnow()
    session.add(sub)
    _event(session, sub.id, "reviewer", "reviewing", "blueprint edited")
    session.commit()
    return {"blueprint_revision": sub.blueprint_revision, "status": sub.status}


@router.patch("/{intake_id}/execution-mode")
def set_execution_mode(
    intake_id: int, body: ExecutionModeBody, session: Session = Depends(get_session)
):
    """Set the migration-only execution intent (live | schema_only) captured at
    review. `approve_and_scaffold` propagates it to Project.data_connectivity_mode.
    Migration-only; frozen once the submission is approved/scaffolded/rejected."""
    sub = session.get(IntakeSubmission, intake_id)
    if sub is None:
        raise HTTPException(404, "Intake submission not found.")
    mode = (body.execution_mode or "").strip()
    if mode not in ("live", "schema_only"):
        raise HTTPException(422, "execution_mode must be 'live' or 'schema_only'.")
    if sub.scenario != "migration":
        raise HTTPException(422, "execution_mode applies to migration submissions only.")
    if sub.status in ("approved", "scaffolding", "scaffolded", "rejected"):
        raise HTTPException(409, f"submission is {sub.status}; execution_mode is frozen.")
    sub.execution_mode = mode
    sub.updated_at = datetime.utcnow()
    session.add(sub)
    _event(session, sub.id, "reviewer", sub.status, f"execution_mode={mode}")
    session.commit()
    return {"execution_mode": sub.execution_mode}


@router.post("/{intake_id}/reparse")
def reparse(intake_id: int, session: Session = Depends(get_session)):
    sub = session.get(IntakeSubmission, intake_id)
    if sub is None:
        raise HTTPException(404, "Intake submission not found.")
    if sub.status in ("scaffolding", "scaffolded", "approved"):
        raise HTTPException(409, f"submission is {sub.status}; cannot reparse.")
    sub.status = "received"
    sub.lease_owner = None
    sub.lease_expires_at = None
    sub.updated_at = datetime.utcnow()
    session.add(sub)
    _event(session, sub.id, "reviewer", "received", "reparse requested")
    session.commit()
    return {"status": sub.status}


@router.post("/{intake_id}/reject")
def reject(intake_id: int, body: RejectBody, session: Session = Depends(get_session)):
    sub = session.get(IntakeSubmission, intake_id)
    if sub is None:
        raise HTTPException(404, "Intake submission not found.")
    if sub.status in ("scaffolded",):
        raise HTTPException(409, "submission already scaffolded; cannot reject.")
    sub.status = "rejected"
    sub.updated_at = datetime.utcnow()
    session.add(sub)
    _event(session, sub.id, "reviewer", "rejected", body.reason or "")
    session.commit()
    return {"status": sub.status}


@router.post("/{intake_id}/approve")
def approve(intake_id: int, body: ApproveBody, session: Session = Depends(get_session)):
    sub = session.get(IntakeSubmission, intake_id)
    if sub is None:
        raise HTTPException(404, "Intake submission not found.")
    if sub.status == "scaffolded":
        return {"status": "scaffolded", "already": True}
    if sub.status not in ("proposed", "reviewing", "scaffolding"):
        raise HTTPException(409, f"submission is {sub.status}; not approvable.")
    if body.expected_revision != sub.blueprint_revision:
        raise HTTPException(
            409,
            {
                "message": "stale blueprint_revision; reload before approving.",
                "current_revision": sub.blueprint_revision,
            },
        )
    blockers = _approval_blockers(_blueprint_of(sub))
    if blockers:
        raise HTTPException(422, {"message": "blueprint not ready to scaffold", "blockers": blockers})

    # Move to a resumable 'scaffolding' state, then run the idempotent saga.
    sub.status = "scaffolding"
    sub.updated_at = datetime.utcnow()
    session.add(sub)
    _event(session, sub.id, "reviewer", "scaffolding", "approved")
    session.commit()

    try:
        result = intake_scaffold.approve_and_scaffold(session, sub)
    except intake_scaffold.ScaffoldNotImplemented as e:
        raise HTTPException(501, str(e))
    except Exception as e:
        # Leave the row in 'scaffolding' so a re-approve resumes idempotently.
        _event(session, sub.id, "system", "scaffolding", f"scaffold error: {e}")
        session.commit()
        raise HTTPException(500, f"scaffold failed (resumable): {e}")

    sub.status = "scaffolded"
    sub.updated_at = datetime.utcnow()
    session.add(sub)
    _event(session, sub.id, "reviewer", "scaffolded", json.dumps(result, default=str)[:500])
    session.commit()
    return {"status": "scaffolded", "result": result}


def _cluster_for_candidate(candidate_id: str, cand: dict, clusters: list[dict]) -> Optional[dict]:
    """Resolve the source cluster a compiled ``source_aligned`` candidate came from.

    ``assembly_blueprint`` mints ``candidate_id = f"src-{i}-{slug}"`` where ``i`` is
    the candidate's index into ``clusters`` — the authoritative link. Fall back to
    matching the candidate's name against a cluster name if the id is unparseable."""
    m = re.match(r"^src-(\d+)-", candidate_id or "")
    if m:
        idx = int(m.group(1))
        if 0 <= idx < len(clusters):
            return clusters[idx]
    name = ((cand.get("name") or {}).get("value") or "").strip()
    if name:
        for c in clusters:
            if (c.get("name") or "").strip() == name:
                return c
    return None


def _compute_source_scope(session: Session, clusters: list[dict],
                          source_aligned: list[dict]) -> dict[str, dict]:
    """Derive the pre-selected source scope for each ``source_aligned`` candidate
    from its cluster's estate dataset URIs — the connection to bind + the tables to
    pre-check in Data Discovery. Returns ``{candidate_id: {connection_id,
    default_schema, tables:["schema.table", ...]}}`` (only candidates whose cluster
    resolves to a live estate source). Pure over the clusters + a source→connection
    lookup; writes nothing."""
    from .. import estate as estate_mod
    from ..models import EstateSource

    conn_by_source: dict[int, Optional[int]] = {}
    scope: dict[str, dict] = {}
    for cand in source_aligned:
        cid = cand.get("candidate_id") or ""
        cluster = _cluster_for_candidate(cid, cand, clusters)
        if cluster is None:
            continue
        # Parse the cluster's estate dataset URIs → tables grouped by owning source.
        tables_by_source: dict[int, list[tuple[str, str]]] = {}
        for uri in cluster.get("uris") or []:
            parsed = estate_mod.parse_dataset_uri(uri)
            if parsed is None:
                continue
            tables_by_source.setdefault(parsed["source_id"], []).append(
                (parsed["schema"], parsed["table"]))
        if not tables_by_source:
            continue
        # A single SourceBinding points at ONE connection; a cluster is normally
        # single-source, but if it spans sources bind to the dominant one and scope
        # discovery to that source's tables (so the scope matches the bound source).
        dominant_sid = max(tables_by_source, key=lambda s: len(tables_by_source[s]))
        if dominant_sid not in conn_by_source:
            es = session.get(EstateSource, dominant_sid)
            conn_by_source[dominant_sid] = es.connection_id if es is not None else None
        connection_id = conn_by_source[dominant_sid]
        if not connection_id:
            continue  # the estate source vanished — leave this candidate unbound
        pairs = tables_by_source[dominant_sid]
        tables = sorted({f"{sch}.{tbl}" if sch else tbl for sch, tbl in pairs})
        schemas = [sch for sch, _ in pairs if sch]
        default_schema = max(set(schemas), key=schemas.count) if schemas else ""
        scope[cid] = {"connection_id": connection_id,
                      "default_schema": default_schema, "tables": tables}
    return scope


class FromAssemblyBody(BaseModel):
    assembly_id: int


@router.post("/from-assembly")
def from_assembly(body: FromAssemblyBody, session: Session = Depends(get_session),
                  user: AuthUser = Depends(current_user), _role=Depends(require_role("owner"))):
    """The STRUCTURED intake ingestion path (Product Assembly → scaffold). Compiles the
    assembly's decisions + source-cluster plan into an enriched ``ModernizationBlueprint``
    (native ODCS for the aggregate; per-cluster source products with the consumed columns)
    and stages it as a ``structured`` ``IntakeSubmission`` at ``proposed`` — NO LLM parse,
    NO confidence grading. It lands in the intake queue as a light confirm, then the
    existing scaffold saga runs on Approve."""
    from .. import assembly_blueprint as ab
    from .assembly import gather_assembly_inputs

    a = session.get(ProductAssembly, body.assembly_id)
    if a is None:
        raise HTTPException(404, "assembly not found")
    if a.status == "scaffolded":
        raise HTTPException(409, "assembly already scaffolded")
    g = gather_assembly_inputs(session, a)
    score, spec = g["score"], g["spec"]

    # Provenance rides raw_payload_json + external_ref (below), NOT the SUMMARY — the
    # rationale is a forward-looking portfolio description, no feasibility-run noise.
    validated = ab.build_modernization_blueprint(
        spec, g["assignment"], g["decisions"], g["clusters"],
        spec_name=score.spec_name or spec.name, domain=score.domain or spec.domain,
        owner_email=user.email)

    external_ref = f"assembly:{a.id}"
    provenance = {"assembly_id": a.id, "estate_id": a.estate_id,
                  "run_id": a.run_id, "spec_id": a.spec_id}
    # Minimalistic import: carry the estate connection + the cluster's table scope
    # per source-aligned candidate so the scaffold can pre-bind the source and
    # pre-scope Data Discovery. NO graph seed, NO enrichment carry-over — live
    # discovery stays authoritative. Keyed by candidate_id (stable across edits).
    source_scope = _compute_source_scope(
        session, g["clusters"], validated.get("source_aligned") or [])
    if source_scope:
        provenance["source_scope"] = source_scope
    raw_payload = json.dumps({"source": "assembly", "provenance": provenance}, default=str)
    existing = session.exec(select(IntakeSubmission).where(
        IntakeSubmission.source_system == "connected-estate",
        IntakeSubmission.external_ref == external_ref)).first()
    if existing is not None:
        existing.blueprint_json = json.dumps(validated, default=str)
        existing.status = "proposed"
        existing.ingestion_mode = "structured"
        existing.blueprint_revision += 1
        existing.raw_payload_json = raw_payload
        existing.reviewed_by = user.email
        existing.updated_at = datetime.utcnow()
        session.add(existing)
        _event(session, existing.id, user.email, "proposed", "structured (assembly, re-staged)")
        sub_id = existing.id
    else:
        sub = IntakeSubmission(
            source_system="connected-estate", external_ref=external_ref,
            scenario="modernization", status="proposed", ingestion_mode="structured",
            raw_payload_json=raw_payload, blueprint_json=json.dumps(validated, default=str),
            blueprint_revision=1, reviewed_by=user.email)
        session.add(sub)
        session.commit()
        session.refresh(sub)
        _event(session, sub.id, user.email, "proposed", "structured (assembly)")
        sub_id = sub.id
    a.intake_submission_id = sub_id
    a.status = "committed"
    a.updated_at = datetime.utcnow()
    session.add(a)
    session.commit()
    return {"intake_id": sub_id, "review_path": f"/product/intake/{sub_id}",
            "source_product_count": len(validated.get("source_aligned", [])),
            "blueprint_revision": (existing.blueprint_revision if existing else 1)}
