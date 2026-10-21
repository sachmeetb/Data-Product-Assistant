"""Data-migration (dmig) endpoints — configure / snapshot / reconcile / status.

Pairs with the non-LLM ``dmig_*`` stages. The Pipeline.tsx NON_LLM_ACTIONS
entries POST here, then POST ``/stages/{n}/complete`` to flip the stage (mirrors
the serving deploy pattern). The executable work is a downloadable package run via
``serving_runtime.execute_package_runner`` — Data Workbench never runs the
migration through a separate internal path; it kicks off the package's ``run.py``
and reads ``run_result.json``.

Migration is a thin, project-keyed subsystem: it creates NO :DataContract /
:DProdDataProduct / marketplace nodes. State lives in a ``MigrationPlanRow``
advanced by ``migration_orchestrator``.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from .. import migration_orchestrator as orch
from .. import serving_package
from .. import serving_runtime
from ..auth import AuthUser, current_user
from ..authz import require_role
from ..config import BASE_PROJECT_DIR
from ..database import get_session
from ..models import Project

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects/{project_id}/migration", tags=["migration"])

_LOAD_TIMEOUT_S = 1800
_VERIFY_TIMEOUT_S = 600


def _get_project(project_id: int, session: Session) -> Project:
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if getattr(project, "archetype", "") != "dmig":
        raise HTTPException(400, "Not a data-migration project.")
    return project


def _load_spec(project_code: str) -> Optional[dict]:
    """Read the framework-neutral migration.json the generate stage produced.
    Looks in the package dir first, then the skill's default output dirs."""
    candidates = [
        serving_package.package_dir(project_code, "migration") / "migration.json",
        orch.migration_json_path(project_code),
        BASE_PROJECT_DIR / project_code / "migration.json",
    ]
    for p in candidates:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
    return None


def _runner_env(project: Project, session: Session, spec: dict) -> dict[str, str]:
    """Merge WB_SOURCE_* + WB_TARGET_* env for the package runner (secrets ride
    the subprocess env only). Target schema is passed both in the spec and env."""
    src_platform, src_ref = orch.resolve_source_ref(project, session)
    row = orch.get_row(session, project.project_code)
    tgt_platform, tgt_ref = orch.resolve_target_ref(
        session, row.target_connection_id if row else None)
    env = serving_runtime.build_runner_env("WB_SOURCE", src_platform, src_ref)
    env.update(serving_runtime.build_runner_env("WB_TARGET", tgt_platform, tgt_ref))
    # Stamp the configured catalog/schema/platform from the plan onto the spec so
    # the runner and the resolved connection agree (build_runner_env pulls catalog
    # from connection.extra_config, but the per-migration catalog lives on the plan).
    orch.apply_plan_target(session, project, spec)
    env["WB_TARGET_SCHEMA"] = spec.get("target_schema", "")
    if spec.get("target_catalog"):
        env["WB_TARGET_CATALOG"] = spec["target_catalog"]
    spec["source_platform"] = src_platform or spec.get("source_platform", "postgres")
    spec["target_platform"] = tgt_platform or spec.get("target_platform", "postgres")
    return env


def _assemble(project, spec: dict):
    """Assemble the runnable migration package (best-effort README via the
    documenter skill)."""
    def _readme():
        try:
            from ..migration_docs import generate_migration_readme_sync
            return generate_migration_readme_sync(project_code=project.project_code, spec=spec)
        except Exception:
            return None
    return serving_package.assemble_migration_package(
        project_code=project.project_code, spec=spec,
        product_name=project.name, readme_provider=_readme)


# ── shared cores (called by the FastAPI endpoints AND the MCP tools) ─────────

def require_execution_ready(project: Project, session: Session) -> None:
    """Canonical execution gate (D1). Refuse load/reconcile until the migration
    can actually run: mode permitting AND a real source AND a real target AND a
    generated package. Raises HTTPException(409, {message, missing[]}) so every
    caller (REST endpoints, direct /complete, MCP execute tools) reports exactly
    what's unmet. This is the enforcement layer — Pipeline.tsx only reflects it."""
    readiness = orch.migration_execution_readiness(project, session)
    if not readiness["ready"]:
        raise HTTPException(
            409,
            {"message": "migration is not ready to execute", "missing": readiness["missing"]},
        )


def snapshot_core(project: Project, session: Session, actor: str) -> dict:
    """Assemble + run the migration package (load), advance the plan, auto-push.
    Returns a compact run summary. Raises HTTPException(422) if no migration.json,
    or 409 (with missing[]) if the project isn't execution-ready (D1)."""
    require_execution_ready(project, session)
    spec = _load_spec(project.project_code)
    if spec is None:
        raise HTTPException(
            422, "No migration.json found — run the Generate Migration Pipeline stage first.")
    env = _runner_env(project, session, spec)
    pkg = _assemble(project, spec)
    run = serving_runtime.execute_package_runner(
        pkg, ["--mode", "load"], env=env, timeout=_LOAD_TIMEOUT_S)
    orch.record_snapshot(session, project, ok=run.ok, by=actor)
    if run.ok:
        try:
            from .serving import _maybe_auto_push
            _maybe_auto_push(project.id, actor)
        except Exception:
            logger.warning("auto git-push wiring failed for %s", project.project_code, exc_info=True)
    payload = {
        "ok": run.ok,
        "status": run.status,
        "datasets": run.metrics.get("datasets", []),
        "dataset_count": run.metrics.get("dataset_count", 0),
        "target_schema": run.metrics.get("target_schema", spec.get("target_schema")),
        "plan": orch.status_payload(session, project),
    }
    if not run.ok:
        payload["error"] = (run.error.message if run.error else "") or run.stderr_tail or run.log_tail
    return payload


def reconcile_core(project: Project, session: Session, actor: str) -> dict:
    """Run the package in verify mode and record reconciliation on the plan.
    409 (with missing[]) if the project isn't execution-ready (D1)."""
    require_execution_ready(project, session)
    spec = _load_spec(project.project_code)
    if spec is None:
        raise HTTPException(422, "No migration.json found — run the earlier stages first.")
    env = _runner_env(project, session, spec)
    pkg = _assemble(project, spec)
    run = serving_runtime.execute_package_runner(
        pkg, ["--mode", "verify"], env=env, timeout=_VERIFY_TIMEOUT_S)
    results = run.metrics.get("reconciliation", []) if run.ok else []
    orch.record_reconciliation(session, project, results, by=actor)
    return {
        "ok": run.ok,
        "all_pass": run.metrics.get("all_pass", False),
        "reconciliation": results,
        "plan": orch.status_payload(session, project),
        "error": None if run.ok else ((run.error.message if run.error else "") or run.stderr_tail),
    }


# ── request bodies ───────────────────────────────────────────────────────────

class ConfigureBody(BaseModel):
    # Target intent (D2): a live connection id OR a platform-only intent for a
    # schema-only project with no target connection yet. Exactly one required.
    target_connection_id: Optional[int] = None
    target_platform: str = ""
    landing_strategy: str = "raw"
    write_disposition: str = "replace"
    framework: str = "dlt"
    target_schema: str = ""
    # Catalog half of the landing namespace for 3-level targets (Databricks/
    # Snowflake), e.g. "workspace". Empty for 2-level platforms.
    target_catalog: str = ""


# ── endpoints ────────────────────────────────────────────────────────────────

@router.get("/status")
def get_status(project_id: int, session: Session = Depends(get_session)):
    """The migration plan status (configured?, state, target, reconciliation).
    Drives the Pipeline migration panel + the MCP get_migration_status tool."""
    project = _get_project(project_id, session)
    return orch.status_payload(session, project)


# Read-only system catalogs to hide from the writable target picker (best-effort).
_READONLY_CATALOGS = {
    "databricks": {"samples", "system", "information_schema"},
    "snowflake": {"snowflake"},
}


@router.get("/target-namespace-options")
def target_namespace_options(
    project_id: int,
    connection_id: int,
    session: Session = Depends(get_session),
):
    """Enumerate the migration TARGET's landing namespaces so Configure Migration
    can offer a catalog→schema picker instead of free text. 3-level platforms
    (Databricks/Snowflake) return `catalogs`; 2-level (Postgres/MySQL) return
    `schemas`. Read-only system catalogs are filtered out (writable-only, best-
    effort). Best-effort — a probe failure returns empty options and the UI falls
    back to the free-text schema field."""
    _get_project(project_id, session)
    tgt_platform, conn_ref = orch.resolve_target_ref(session, connection_id)
    if not tgt_platform:
        return {"supported": False, "platform": "", "container_parts": [], "catalogs": [], "schemas": []}
    from ..platform.namespace import get_namespace_model
    from .connections import _get_discovery_provider
    try:
        model = get_namespace_model(tgt_platform)
    except KeyError:
        return {"supported": False, "platform": tgt_platform, "container_parts": [], "catalogs": [], "schemas": []}
    provider = _get_discovery_provider(tgt_platform)
    containers = model.container_parts
    ro = _READONLY_CATALOGS.get((tgt_platform or "").lower(), set())

    if "catalog" in containers:
        catalogs: list[dict] = []
        if provider is not None and hasattr(provider, "list_catalogs_and_schemas"):
            try:
                catalogs = provider.list_catalogs_and_schemas(conn_ref)
            except Exception:  # noqa: BLE001
                catalogs = []
        catalogs = [c for c in catalogs if (c.get("catalog") or "").lower() not in ro]
        return {"supported": True, "required": True, "platform": tgt_platform,
                "container_parts": containers, "catalogs": catalogs, "schemas": []}

    schemas: list[str] = []
    if provider is not None and hasattr(provider, "list_namespaces"):
        try:
            for ns in provider.list_namespaces(conn_ref):
                if getattr(ns, "parts", None):
                    schemas.append(ns.parts[0])
        except Exception:  # noqa: BLE001
            schemas = []
    return {"supported": True, "required": False, "platform": tgt_platform,
            "container_parts": containers, "catalogs": [], "schemas": sorted(set(schemas))}


@router.post("/configure")
def configure(
    project_id: int,
    body: ConfigureBody,
    session: Session = Depends(get_session),
    user: AuthUser = Depends(current_user),
    _role=Depends(require_role("engineer")),
):
    """Choose the migration target + landing strategy + write disposition and
    create/refresh the MigrationPlan at ``draft``. Idempotent."""
    project = _get_project(project_id, session)
    if body.landing_strategy != "raw":
        raise HTTPException(400, "Only 'raw' landing is supported in this phase.")
    try:
        orch.configure(
            session, project,
            target_connection_id=body.target_connection_id,
            target_platform=body.target_platform,
            landing_strategy=body.landing_strategy,
            write_disposition=body.write_disposition,
            framework=body.framework,
            target_schema=body.target_schema,
            target_catalog=body.target_catalog,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    return orch.status_payload(session, project)


@router.post("/snapshot")
def run_snapshot(
    project_id: int,
    session: Session = Depends(get_session),
    user: AuthUser = Depends(current_user),
    _role=Depends(require_role("engineer")),
):
    """Assemble the migration package and run it (source→target load). Advances
    the plan and, when git auto-push is on, pushes the package. Returns a compact
    run summary; the frontend then POSTs /stages/{n}/complete."""
    project = _get_project(project_id, session)
    return snapshot_core(project, session, actor=(user.email or user.name or ""))


@router.post("/reconcile")
def run_reconcile(
    project_id: int,
    session: Session = Depends(get_session),
    user: AuthUser = Depends(current_user),
    _role=Depends(require_role("engineer")),
):
    """Reconcile source↔target row counts by running the package in ``verify``
    mode, and record the results on the plan (advancing to ``reconciled`` when all
    pass)."""
    project = _get_project(project_id, session)
    return reconcile_core(project, session, actor=(user.email or user.name or ""))


@router.get("/package")
def get_migration_package(
    project_id: int,
    format: str = "zip",
    session: Session = Depends(get_session),
):
    """Download the self-contained migration package (the same package DWB runs).
    ``format=zip`` streams a zip; ``format=json`` returns the text files.
    Assembled on demand from the persisted migration.json spec."""
    project = _get_project(project_id, session)
    spec = _load_spec(project.project_code)
    if spec is None:
        raise HTTPException(404, "No migration pipeline generated yet — run the earlier stages first.")
    orch.apply_plan_target(session, project, spec)  # stamp catalog/schema into the packaged migration.json
    pkg = _assemble(project, spec)
    files = serving_package.collect_package_files(pkg)
    if format == "json":
        return {"project_code": project.project_code,
                "files": {k: (v if isinstance(v, str) else "<binary>") for k, v in files.items()}}
    return serving_package.zip_response(
        files, f"{project.project_code}-migration.zip",
        root_prefix=f"{project.project_code}-migration")


# ── Confirm Physical Schema (D3) ─────────────────────────────────────────────

def _serialise_phys(row) -> dict:
    import json as _json

    try:
        schema = _json.loads(row.physical_schema_json or "{}")
    except (ValueError, TypeError):
        schema = {}
    return {
        "status": row.status,
        "schema": schema,
        "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else None,
        "confirmed_by": row.confirmed_by,
        "source_intake_submission_id": row.source_intake_submission_id,
    }


class PhysicalSchemaBody(BaseModel):
    schema: dict  # a physical_schema.PhysicalSchema payload


@router.get("/physical-schema")
def get_physical_schema(project_id: int, session: Session = Depends(get_session)):
    """The reviewed physical schema for a schema-only migration project. On first
    read it's pre-filled (best-effort) from the originating intake blueprint and
    returned as a draft (not yet persisted) with ``prefilled: true``."""
    from .. import physical_schema as ps

    project = _get_project(project_id, session)
    row, prefilled = ps.get_or_prefill(session, project)
    out = _serialise_phys(row)
    out["prefilled"] = prefilled
    return out


@router.put("/physical-schema")
def put_physical_schema(
    project_id: int,
    body: PhysicalSchemaBody,
    session: Session = Depends(get_session),
    user: AuthUser = Depends(current_user),
    _role=Depends(require_role("engineer")),
):
    """Save edits as a draft (re-opens a previously-confirmed schema)."""
    from .. import physical_schema as ps

    project = _get_project(project_id, session)
    schema = ps.PhysicalSchema.model_validate(body.schema)
    row = ps.save_draft(session, project, schema)
    return _serialise_phys(row)


@router.post("/physical-schema/confirm")
def confirm_physical_schema(
    project_id: int,
    body: PhysicalSchemaBody,
    session: Session = Depends(get_session),
    user: AuthUser = Depends(current_user),
    _role=Depends(require_role("engineer")),
):
    """Validate + persist as ``confirmed``. 422 with ``errors[]`` when a name is
    path-unsafe/colliding or an included column is missing a physical type."""
    from .. import physical_schema as ps

    project = _get_project(project_id, session)
    schema = ps.PhysicalSchema.model_validate(body.schema)
    try:
        row = ps.confirm(session, project, schema, by=(user.email or user.name or ""))
    except ps.PhysicalSchemaValidationError as e:
        raise HTTPException(422, {"message": "physical schema not confirmable", "errors": e.errors})
    return _serialise_phys(row)


@router.post("/seed-schema")
def seed_schema(
    project_id: int,
    session: Session = Depends(get_session),
    user: AuthUser = Depends(current_user),
    _role=Depends(require_role("engineer")),
):
    """Deterministically materialize the CONFIRMED physical schema as the project
    graph catalog (D4) — no LLM, no live source. Reuses the discovery YAML +
    loader. 409 if not confirmed; returns the load result (``ok`` only when graph
    counts match the confirmed schema — the caller completes the import stage
    only on ok)."""
    from .. import intake_schema_seed as seeder

    project = _get_project(project_id, session)
    try:
        return seeder.seed_graph_from_confirmed_schema(session, project)
    except seeder.SeedError as e:
        raise HTTPException(409, str(e))


@router.post("/flip-to-live")
def flip_to_live(
    project_id: int,
    session: Session = Depends(get_session),
    user: AuthUser = Depends(current_user),
    _role=Depends(require_role("engineer")),
):
    """Transition a schema-only migration project to live (D5+D6). Requires BOTH
    a real source and a target connection (409 with missing[] otherwise). Flips
    the mode, attaches live discovery (select_data_source + data_discovery), and
    performs the seed→live reconciliation: deletes the intake-seeded catalog so
    real discovery loads fresh, and invalidates the downstream artifacts built on
    the seeded graph (import/enrichment/assess/generate → pending)."""
    from ..models import SourceBinding
    from .. import intake_schema_seed as seeder
    from .projects import _create_workflow_and_stages, get_workflow_catalog
    from ..models import Workflow
    from .stages import reset_stages_by_id, ResetStagesByIdInput

    project = _get_project(project_id, session)
    if (getattr(project, "data_connectivity_mode", "live") or "live") != "schema_only":
        raise HTTPException(400, "Project is not in schema_only mode.")

    # Both ends required for a live migration (mirrors the D1 execution ends).
    missing: list[str] = []
    has_source = (
        session.exec(select(SourceBinding).where(SourceBinding.project_id == project.id)).first() is not None
    )
    if not has_source:
        missing.append("no source connection (bind a live source)")
    row = orch.get_row(session, project.project_code)
    if row is None or not row.target_connection_id:
        missing.append("no target connection (Configure Migration with a live connection)")
    if missing:
        raise HTTPException(409, {"message": "cannot flip to live", "missing": missing})

    # 1) flip intent
    project.data_connectivity_mode = "live"
    session.add(project)
    session.commit()

    # 2) attach live discovery (select_data_source + data_discovery_composite) if absent
    added_discovery = False
    if not session.exec(
        select(Workflow).where(Workflow.project_id == project.id, Workflow.workflow_id == "data_discovery")
    ).first():
        catalog = get_workflow_catalog(project.archetype)
        tmpl = next((t for t in catalog if t["workflow_id"] == "data_discovery"), None)
        if tmpl is not None:
            max_order = session.exec(
                select(Workflow.order).where(Workflow.project_id == project.id).order_by(Workflow.order.desc())
            ).first()
            _create_workflow_and_stages(project, tmpl, (max_order or 0) + 1, session)
            session.commit()
            added_discovery = True

    # 3) seed→live reconciliation: drop the seeded catalog + invalidate downstream
    seeder.clear_seeded_catalog(project)
    reset = reset_stages_by_id(
        project.id,
        ResetStagesByIdInput(stage_ids=[
            "dmig_import_schema", "metadata_enrichment", "dmig_assess_plan", "dmig_generate_pipeline",
        ]),
        session,
    )
    return {
        "status": "live",
        "added_discovery_workflow": added_discovery,
        "invalidated": reset.get("reset", []),
        "note": "Run live discovery, then re-run assess/generate against the discovered catalog.",
    }
