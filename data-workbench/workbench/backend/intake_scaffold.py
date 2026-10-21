"""Approve-time scaffold saga: a reviewed blueprint → real project(s).

Resumable + idempotent by construction: each child is tracked by an
:class:`IntakeSpawn` row keyed on the blueprint's stable ``candidate_id`` with a
compare-and-set status, so a retried/resumed approval never creates a duplicate
project (``create_project`` commits filesystem + SQL before best-effort Neo4j,
so partial failure is real). Scaffolding writes NO speculative graph nodes — the
parsed dataset/column inventory stays in the blueprint for a later
parsed-vs-discovered comparison; the real discovery pipeline remains
authoritative.

V1: migration branch implemented (create a dmig project; leave Configure for the
engineer to pick a target connection). Modernization branch lands in Phase 3.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlmodel import Session, select

from . import intake_blueprint as bp
from .models import (
    AppUser,
    IntakePendingDependency,
    IntakeSpawn,
    IntakeSubmission,
    PlatformConnection,
    Project,
    ProductRequest,
    ProductRequestKind,
    ProductRequestStatus,
    SourceBinding,
    StageRun,
    StageStatus,
)


def _mark_project_origin(session: Session, project_id: int, submission_id: int) -> None:
    """Stamp the denormalized forward marker on a scaffolded child project.

    :class:`IntakeSpawn.child_project_id` stays the source of truth; this column
    is a convenience for API/UI reads (and the alignment-reflection hook).
    """
    project = session.get(Project, project_id)
    if project is not None and project.parent_intake_submission_id != submission_id:
        project.parent_intake_submission_id = submission_id
        session.add(project)
        session.commit()


class ScaffoldNotImplemented(Exception):
    """Raised for a scenario branch not yet built (router → HTTP 501)."""


def _get_or_create_spawn(session: Session, sid: int, candidate_id: str, kind: str) -> IntakeSpawn:
    row = session.exec(
        select(IntakeSpawn).where(
            IntakeSpawn.intake_submission_id == sid,
            IntakeSpawn.candidate_id == candidate_id,
        )
    ).first()
    if row is None:
        row = IntakeSpawn(intake_submission_id=sid, candidate_id=candidate_id, kind=kind)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def _scaffold_migration(
    session: Session, submission: IntakeSubmission, model: bp.MigrationBlueprint
) -> dict[str, Any]:
    # One project per migration submission; candidate id is stable so a resumed
    # approve is a no-op once created.
    candidate_id = "dmig-project"
    spawn = _get_or_create_spawn(session, submission.id, candidate_id, kind="dmig")
    if spawn.status == "created" and spawn.child_project_id:
        return {"project_id": spawn.child_project_id, "already_scaffolded": True}

    # Imported lazily to avoid a circular import (routers import models/db).
    from .routers.projects import ProjectCreate, create_project

    name = (model.project_name.value or "Imported Migration").strip()
    domain = (model.domain.value or "").strip().lower() or None
    # schema_only → the offline template (Import Provided Schema instead of live
    # discovery). Live → workflow_ids=None → archetype DEFAULT (migration_raw).
    # NEVER [] — an empty list filters out every template → legacy flat workflow.
    schema_only = (getattr(submission, "execution_mode", "live") or "live").strip() == "schema_only"
    workflow_ids = ["migration_schema_only"] if schema_only else None
    project_create = ProjectCreate(
        name=name, archetype="dmig", domain=domain, workflow_ids=workflow_ids
    )
    # create_project returns a dict (_project_response); response_model only
    # serializes at the HTTP layer, not on a direct in-process call.
    resp = create_project(project_create, session)
    project_id = resp["id"]
    project_code = resp.get("project_code")
    _mark_project_origin(session, project_id, submission.id)
    # Propagate the reviewer's migration-only execution intent (live | schema_only)
    # onto the scaffolded project. Only migration supports schema_only in V1.
    mode = (getattr(submission, "execution_mode", "live") or "live").strip()
    if mode == "schema_only":
        proj = session.get(Project, project_id)
        if proj is not None:
            proj.data_connectivity_mode = "schema_only"
            session.add(proj)
            session.commit()

    spawn.status = "created"
    spawn.child_project_id = project_id
    spawn.detail = f"dmig project {project_code or ''}"
    spawn.updated_at = datetime.utcnow()
    session.add(spawn)
    session.commit()

    # NOTE (finding 2): Configure Migration needs a real target_connection_id —
    # a platform string is insufficient — so we intentionally DO NOT call
    # migration_orchestrator.configure() here. The engineer resolves the target
    # connection via ConfigureMigrationDialog; the parsed platform hints +
    # dataset inventory remain in submission.blueprint_json for reference.
    return {
        "project_id": project_id,
        "project_code": project_code,
        "note": "dmig project scaffolded; engineer selects target connection in Configure Migration.",
    }


# ── modernization portfolio ─────────────────────────────────────────────────

def _submitter(submission: IntakeSubmission) -> str:
    return submission.reviewed_by or f"intake:{submission.source_system}"


def _owner_of(session: Session, submission: IntakeSubmission) -> tuple[str | None, str | None]:
    """The PO who owns a scaffolded product — ``(owner_email, owner_name)``.

    The PO's email rides ``IntakeSubmission.reviewed_by`` (set by
    ``from_assembly`` to the committing PO's ``user.email``). Only an email-shaped
    value is an owner — a machine principal (external-tool intake with no reviewer,
    e.g. ``intake:connected-estate``) must NOT become a fake owner. ``owner_name``
    is a best-effort ``AppUser`` display-name lookup (``None`` when auth is off /
    the account isn't seeded; ``sa_pipeline`` then falls back to the email local
    part). Threading this into ``ProjectCreate`` is what lets the PO find and
    validate the product (PO My Products + PO-MCP both key on ``owner_email``)."""
    email = (submission.reviewed_by or "").strip()
    if "@" not in email:
        return None, None
    user = session.get(AppUser, email)
    return email, (user.name if user else None)


def _included(candidate: bp.ProductCandidate) -> bool:
    return candidate.review_state != bp.ReviewState.excluded


def _source_scope_for(submission: IntakeSubmission, candidate_id: str) -> dict | None:
    """The pre-selected source scope for a candidate, from the assembly provenance
    breadcrumb (Connected-Estate Bridge B, stored under
    ``raw_payload_json.provenance.source_scope[candidate_id]``). Returns ``None``
    for the external-tool intake path (no such breadcrumb) — behavior unchanged."""
    try:
        payload = json.loads(submission.raw_payload_json or "{}")
    except (TypeError, ValueError):
        return None
    prov = payload.get("provenance") if isinstance(payload, dict) else None
    if not isinstance(prov, dict):
        return None
    scope = (prov.get("source_scope") or {}).get(candidate_id)
    return scope if isinstance(scope, dict) else None


def _apply_source_scope(session: Session, project_id: int, scope: dict) -> None:
    """Carry the estate connection + cluster table scope onto a freshly-scaffolded
    dpe-sa project: bind the source connection, mark ``select_data_source`` complete,
    and stash the Data Discovery table scope on the project.

    Writes NO graph nodes and does NOT touch the discovery/enrichment stages — the
    engineer still runs the real, deeper pipeline (keys, FKs, profiling-informed
    descriptions) live. This only removes the 'pick the source + figure out which
    tables' redundancy, so 'discovery authoritative' is preserved."""
    from .routers.stages import _resolve_stage_id  # lazy: avoids an import cycle

    connection_id = scope.get("connection_id")
    if not connection_id:
        return
    conn = session.get(PlatformConnection, connection_id)
    if conn is None:
        return  # the estate connection vanished — leave the project unbound

    # a) bind the source connection (one binding per project; upsert).
    default_schema = scope.get("default_schema") or ""
    binding = session.exec(
        select(SourceBinding).where(SourceBinding.project_id == project_id)
    ).first()
    if binding is None:
        binding = SourceBinding(
            project_id=project_id, connection_id=connection_id,
            default_schema=default_schema,
        )
    else:
        binding.connection_id = connection_id
        binding.default_schema = default_schema
        binding.updated_at = datetime.utcnow()
    session.add(binding)

    # b) flip select_data_source → complete (mirror mcp_server.set_data_source).
    project = session.get(Project, project_id)
    if project is not None:
        now = datetime.utcnow()
        for r in session.exec(
            select(StageRun).where(StageRun.project_id == project_id)
        ).all():
            if _resolve_stage_id(project, r.stage_number, r.workflow_id, session) == "select_data_source" \
                    and r.status != StageStatus.complete:
                r.status = StageStatus.complete
                r.completed_at = now
                if not r.started_at:
                    r.started_at = now
                session.add(r)
        # c) pre-scope Data Discovery to the cluster's tables (config-options
        # intersects with the live options before pre-checking).
        tables = [t for t in (scope.get("tables") or []) if t]
        if tables:
            project.discovery_scope_json = json.dumps(tables)
            session.add(project)
    session.commit()


def _scaffold_source_aligned(
    session: Session, submission: IntakeSubmission, cand: bp.ProductCandidate
) -> int:
    """New source-aligned candidate → a normal discovery-first dpe-sa project +
    an Incoming-queue ProductRequest. ODCS is NOT synthesized/published here —
    the engineer runs discovery and the PO validates (finding 4: sources must
    not auto-publish)."""
    spawn = _get_or_create_spawn(session, submission.id, cand.candidate_id, kind="dpe-sa")
    if spawn.status == "created" and spawn.child_project_id:
        return spawn.child_project_id

    from .routers.projects import ProjectCreate, create_project

    name = (cand.name.value or "Imported Source Product").strip()
    domain = (cand.domain.value or "").strip().lower() or None
    idea = cand.product_idea or cand.description or ""
    owner_email, owner_name = _owner_of(session, submission)
    # No live connection → scaffold offline (the deterministic data_discovery_offline
    # stage seeds from an uploaded manifest). Reuses the migration execution_mode
    # signal (schema_only|offline) so a modernization submitted without live access
    # lands the source-aligned children offline, parallel to _scaffold_migration.
    exec_mode = (getattr(submission, "execution_mode", "live") or "live").strip().lower()
    conn_mode = "offline" if exec_mode in ("schema_only", "offline") else "live"
    resp = create_project(
        ProjectCreate(
            name=name, archetype="dpe-sa", domain=domain,
            product_idea=idea, workflow_ids=None,
            owner_email=owner_email, owner_name=owner_name,
            data_connectivity_mode=conn_mode,
        ),
        session,
    )
    project_id = resp["id"]
    code = resp.get("project_code")
    _mark_project_origin(session, project_id, submission.id)
    req = ProductRequest(
        project_id=project_id,
        contract_id=f"{code}-contract",
        kind=ProductRequestKind.new,
        status=ProductRequestStatus.submitted,
        submitted_by=_submitter(submission),
        parent_intake_submission_id=submission.id,
        notes="Scaffolded from inbound modernization intake.",
    )
    session.add(req)
    session.commit()
    session.refresh(req)

    spawn.status = "created"
    spawn.child_project_id = project_id
    spawn.child_request_id = req.id
    spawn.detail = f"dpe-sa {code}"
    spawn.updated_at = datetime.utcnow()
    session.add(spawn)
    session.commit()

    # Connected-Estate Bridge B: pre-bind the source connection + pre-scope
    # discovery when the assembly provenance carries a scope for this candidate.
    # Best-effort — an unbound project still works (the engineer picks the source),
    # so a failure here never breaks the scaffold. Absent for external-tool intake.
    scope = _source_scope_for(submission, cand.candidate_id)
    if scope:
        try:
            _apply_source_scope(session, project_id, scope)
        except Exception:
            pass
    return project_id


def _scaffold_consumer_aligned(
    session: Session, submission: IntakeSubmission, cand: bp.ProductCandidate
) -> int:
    """Consumer-aligned candidate → a dpe-cf DRAFT project. No ProductRequest
    submit yet: its sources aren't materialized, so :CONSUMES can't bind (finding
    4). The PO finishes it in the wizard once dependencies are confirmed."""
    spawn = _get_or_create_spawn(session, submission.id, cand.candidate_id, kind="dpe-cf")
    if spawn.status == "created" and spawn.child_project_id:
        return spawn.child_project_id

    from .routers.projects import ProjectCreate, create_project

    name = (cand.name.value or "Imported Product").strip()
    domain = (cand.domain.value or "").strip().lower() or None
    idea = cand.purpose or cand.description or ""
    owner_email, owner_name = _owner_of(session, submission)
    resp = create_project(
        ProjectCreate(
            name=name, archetype="dpe-cf", domain=domain,
            product_idea=idea, workflow_ids=None,
            owner_email=owner_email, owner_name=owner_name,
        ),
        session,
    )
    project_id = resp["id"]
    _mark_project_origin(session, project_id, submission.id)
    # Structured ingestion (Product Assembly): a candidate carries a pre-authored
    # native ODCS draft → write it so the CF wizard opens PRE-FILLED (curated schema +
    # source-intent hints + productKind). Best-effort; the classic parsed path carries
    # no odcs and keeps the empty-draft behavior. The wizard/PO wires the real source
    # mappings once the source products publish.
    if cand.odcs:
        try:
            from .models import Project
            from .routers.odcs import _save_odcs_to_graph
            proj = session.get(Project, project_id)
            if proj is not None:
                _save_odcs_to_graph(cand.odcs, proj, submitted_by=(submission.reviewed_by or ""),
                                    change_kind="auto",
                                    revision_notes="Pre-filled from the Product Assembly workspace.")
        except Exception:
            pass  # a failed pre-fill leaves an empty draft; never breaks the scaffold
    spawn.status = "created"
    spawn.child_project_id = project_id
    spawn.detail = f"dpe-cf {resp.get('project_code')}"
    spawn.updated_at = datetime.utcnow()
    session.add(spawn)
    session.commit()
    return project_id


def _record_pending_dependency(
    session: Session,
    submission: IntakeSubmission,
    dep: bp.Dependency,
    consumer_project_id: int,
    source_project_id: int | None,
) -> None:
    """Record a consumer→source dependency to be bound once the source
    materializes. Idempotent: matches on (submission, consumer, source-ref)."""
    existing = session.exec(
        select(IntakePendingDependency).where(
            IntakePendingDependency.intake_submission_id == submission.id,
            IntakePendingDependency.consumer_candidate_id == dep.from_candidate_id,
        )
    ).all()
    for row in existing:
        if row.source_candidate_id == dep.to_candidate_id and row.source_external_uri == dep.to_external_uri:
            if source_project_id and not row.source_project_id:
                row.source_project_id = source_project_id
                row.updated_at = datetime.utcnow()
                session.add(row)
                session.commit()
            return
    session.add(
        IntakePendingDependency(
            intake_submission_id=submission.id,
            consumer_project_id=consumer_project_id,
            consumer_candidate_id=dep.from_candidate_id,
            source_candidate_id=dep.to_candidate_id,
            source_external_uri=dep.to_external_uri,
            source_project_id=source_project_id,
            status="pending",
        )
    )
    session.commit()


def _bound_source_project_ids(session: Session, inputs: Any) -> set[int]:
    """Resolve the set of source ``Project.id``s a consumer's ``inputs[]`` binds.

    Each input names an upstream product's contract via a ``{project_code}-contract``
    id — extracted tolerant of the alias keys the ODCS canonicaliser accepts
    (``contract_id``/``contractId``, or the ``dprod:`` prefix stripped off
    ``dprod_uri``/``dprodUri``). Strip the ``-contract`` suffix → source
    ``project_code`` → ``Project.id``. Unknown / free-form external URIs (no
    enforced convention) simply don't resolve and are skipped."""
    if not isinstance(inputs, list):
        return set()
    codes: set[str] = set()
    for entry in inputs:
        if not isinstance(entry, dict):
            continue
        cid = (entry.get("contract_id") or entry.get("contractId") or "").strip()
        if not cid:
            uri = (entry.get("dprod_uri") or entry.get("dprodUri") or "").strip()
            if uri.startswith("dprod:"):
                cid = uri[len("dprod:"):].strip()
        if cid.endswith("-contract"):
            codes.add(cid[: -len("-contract")])
    if not codes:
        return set()
    rows = session.exec(select(Project).where(Project.project_code.in_(codes))).all()  # type: ignore[attr-defined]
    return {p.id for p in rows if p.id is not None}


def resolve_pending_dependencies(
    session: Session, consumer_project: Project, inputs: Any
) -> int:
    """Flip a consumer's still-``pending`` intake dependencies to ``bound`` when its
    contract save wires the matching ``:CONSUMES`` edge (the inverse of
    ``_record_pending_dependency``). 'bound' means the consumer→source edge now
    exists — the correct trigger is a consumer contract save, where edges sync,
    NOT source publish.

    No-op unless the project is ``dpe-cf``. Returns the number of rows flipped.
    Best-effort: callers wrap it so it never breaks a save. Deferred: deps whose
    source is an already-published EXTERNAL product (``source_project_id`` unset,
    only ``source_external_uri``) won't match — the external URI is free-form."""
    if (consumer_project.archetype or "").strip() != "dpe-cf":
        return 0
    bound_ids = _bound_source_project_ids(session, inputs)
    if not bound_ids:
        return 0
    rows = session.exec(
        select(IntakePendingDependency).where(
            IntakePendingDependency.consumer_project_id == consumer_project.id,
            IntakePendingDependency.status == "pending",
            IntakePendingDependency.source_project_id.in_(bound_ids),  # type: ignore[attr-defined]
        )
    ).all()
    flipped = 0
    for row in rows:
        row.status = "bound"
        row.updated_at = datetime.utcnow()
        session.add(row)
        flipped += 1
    if flipped:
        session.commit()
    return flipped


def _scaffold_modernization(
    session: Session, submission: IntakeSubmission, model: bp.ModernizationBlueprint
) -> dict[str, Any]:
    # Sources first so consumers can reference their project ids.
    src_map: dict[str, int] = {}
    for cand in model.source_aligned:
        if _included(cand) and cand.candidate_id:
            src_map[cand.candidate_id] = _scaffold_source_aligned(session, submission, cand)

    con_map: dict[str, int] = {}
    for cand in model.consumer_aligned:
        if _included(cand) and cand.candidate_id:
            con_map[cand.candidate_id] = _scaffold_consumer_aligned(session, submission, cand)

    pending = 0
    for dep in model.dependencies:
        if dep.review_state == bp.ReviewState.excluded:
            continue
        if not dep.to_candidate_id and not dep.to_external_uri:
            continue  # malformed edge
        consumer_pid = con_map.get(dep.from_candidate_id)
        if consumer_pid is None:
            continue  # references an excluded/unknown consumer
        # A dependency's target may be a source-aligned OR another consumer-aligned
        # candidate (a modernization blueprint can express consumer→consumer
        # chains now that multi-hop consumption is supported) — resolve against
        # both maps so the pending dependency records the real upstream project id
        # instead of source_project_id=None.
        source_pid = None
        if dep.to_candidate_id:
            source_pid = src_map.get(dep.to_candidate_id) or con_map.get(dep.to_candidate_id)
        _record_pending_dependency(session, submission, dep, consumer_pid, source_pid)
        pending += 1

    return {
        "scenario": "modernization",
        "source_projects": list(src_map.values()),
        "consumer_projects": list(con_map.values()),
        "pending_dependencies": pending,
        "note": (
            "Source products scaffolded for discovery (drafts, not published); "
            "consumer drafts hold pending dependencies until sources materialize."
        ),
    }


def approve_and_scaffold(session: Session, submission: IntakeSubmission) -> dict[str, Any]:
    """Scaffold real project(s) from a reviewed submission's blueprint.

    Re-validates the (possibly edited) blueprint before acting. Idempotent +
    resumable via IntakeSpawn per-child compare-and-set.
    """
    model = bp.parse_blueprint(json.loads(submission.blueprint_json or "{}"))
    if isinstance(model, bp.MigrationBlueprint):
        result = _scaffold_migration(session, submission, model)
        result["scenario"] = "migration"
        return result
    return _scaffold_modernization(session, submission, model)
