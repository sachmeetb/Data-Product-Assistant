"""Code-migration (cmig) endpoints — link / import / configure / spec / package.

Pairs with the non-LLM ``cmig_*`` stages (Pipeline.tsx NON_LLM_ACTIONS POST here,
then POST ``/stages/{n}/complete``) and the two LLM stages (run via the shared
runner, which enforces ``require_forward_ready``). The spec review gate is a
first-class surface here (GET/PUT /spec, approve, reopen) backed by the canonical
DB spec in ``CodeMigrationPlanRow`` — NOT the Neo4j review machinery.

Thin, project-keyed subsystem: the only graph writes are a single :CodeModule
node + a :USES_DATASET cross-project edge (built on spec approval).
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlmodel import Session, select

from .. import code_migration_orchestrator as orch
from ..auth import AuthUser, current_user
from ..authz import require_role
from ..database import get_session
from ..models import Project, StageRun, StageStatus, Workflow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects/{project_id}/code-migration", tags=["code-migration"])

# Secure-upload policy (imported code is untrusted DATA — never executed).
_ALLOWED_EXTS = {".sql", ".py", ".scala", ".r", ".java", ".js", ".ts",
                 ".hql", ".sh", ".txt", ".ipynb", ".pig", ".ksh"}
_MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MB per file


def _get_project(project_id: int, session: Session) -> Project:
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if getattr(project, "archetype", "") != "cmig":
        raise HTTPException(400, "Not a code-migration project.")
    return project


def _set_review_stage(session: Session, project: Project, *, complete: bool) -> None:
    """Flip the cmig_reverse_engineer StageRun to complete (on approve) or back to
    awaiting_review (on reopen). The spec review is tracked in SQLite, so the
    stage transition must be driven here (not by reviews.py's Neo4j auto-flip)."""
    wfs = session.exec(select(Workflow).where(Workflow.project_id == project.id)).all()
    target_status = StageStatus.complete if complete else StageStatus.awaiting_review
    for wf in wfs:
        if not wf.workflow_json:
            continue
        stages = [s for s in json.loads(wf.workflow_json) if s.get("enabled", True)]
        for idx, s in enumerate(stages, start=1):
            if s.get("stage_id") == "cmig_reverse_engineer":
                sr = session.exec(
                    select(StageRun).where(
                        StageRun.project_id == project.id,
                        StageRun.stage_number == idx,
                        StageRun.workflow_id == wf.workflow_id,
                    )
                ).first()
                if sr and sr.status in (StageStatus.awaiting_review, StageStatus.complete):
                    sr.status = target_status
                    session.add(sr)
    session.commit()


# ── endpoints ────────────────────────────────────────────────────────────────

@router.get("/status")
def get_status(project_id: int, session: Session = Depends(get_session)):
    project = _get_project(project_id, session)
    return orch.status_payload(session, project)


@router.get("/eligible-dmig")
def eligible_dmig(project_id: int, session: Session = Depends(get_session)):
    """The data-migration projects this cmig project may link to (with an
    eligible/verified flag) — drives the Link selector."""
    _get_project(project_id, session)
    return {"projects": orch.eligible_dmig_projects(session)}


class LinkBody(BaseModel):
    dmig_project_code: str


@router.post("/link")
def link(
    project_id: int,
    body: LinkBody,
    session: Session = Depends(get_session),
    user: AuthUser = Depends(current_user),
    _role=Depends(require_role("engineer")),
):
    """Bind to a completed dmig project: snapshot the schema mapping, lock the
    target platform, record the linked-migration hash. (Dual-project authorization
    for per-token scoping is enforced on the MCP path.)"""
    project = _get_project(project_id, session)
    try:
        orch.link(session, project, body.dmig_project_code.strip())
    except ValueError as e:
        raise HTTPException(422, str(e))
    return orch.status_payload(session, project)


@router.post("/import-code")
async def import_code(
    project_id: int,
    files: list[UploadFile] = File(...),
    session: Session = Depends(get_session),
    user: AuthUser = Depends(current_user),
    _role=Depends(require_role("engineer")),
):
    """Bounded multipart upload of the legacy code into an IMMUTABLE source/ dir
    with a SHA-256 manifest. Rejects disallowed extensions, oversize, binary, and
    path-traversal names. Imported code is untrusted data — never executed."""
    import os

    project = _get_project(project_id, session)
    src = orch.source_dir(project.project_code)
    # Re-import replaces the immutable set: clear then rewrite.
    if src.exists():
        for p in sorted(src.rglob("*"), reverse=True):
            try:
                p.unlink() if p.is_file() else p.rmdir()
            except OSError:
                pass
    src.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, str] = {}
    for f in files:
        raw_name = f.filename or "unnamed"
        # Normalize to a safe basename — reject traversal / absolute paths.
        name = os.path.basename(raw_name.replace("\\", "/"))
        if not name or name.startswith(".") or "/" in name:
            raise HTTPException(422, f"unsafe filename: {raw_name!r}")
        ext = os.path.splitext(name)[1].lower()
        if ext not in _ALLOWED_EXTS:
            raise HTTPException(422, f"disallowed file type {ext!r} (allowed: {sorted(_ALLOWED_EXTS)})")
        data = await f.read()
        if len(data) > _MAX_UPLOAD_BYTES:
            raise HTTPException(422, f"{name} exceeds the {_MAX_UPLOAD_BYTES} byte limit")
        if b"\x00" in data:
            raise HTTPException(422, f"{name} looks binary (NUL byte) — only text code is accepted")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(422, f"{name} is not valid UTF-8 text")
        dest = src / name
        dest.write_bytes(data)
        manifest[name] = hashlib.sha256(data.hex().encode("utf-8")).hexdigest()

    if not manifest:
        raise HTTPException(422, "no files uploaded")
    orch.record_import(session, project, manifest)
    return {"imported": sorted(manifest.keys()), "status": orch.status_payload(session, project)}


class ConfigureBody(BaseModel):
    target_runtime: str = ""
    output_language: str = "sql"
    framework: str = ""
    artifact_kind: str = "sql_script"
    source_platform: str = ""
    source_platform_version: str = ""
    # {platform, version, files:{path:sha256}} recorded per corpus (kept separate).
    source_corpus: Optional[dict] = None
    target_corpus: Optional[dict] = None


@router.post("/configure")
def configure(
    project_id: int,
    body: ConfigureBody,
    session: Session = Depends(get_session),
    user: AuthUser = Depends(current_user),
    _role=Depends(require_role("engineer")),
):
    project = _get_project(project_id, session)
    orch.configure(
        session, project,
        target_runtime=body.target_runtime, output_language=body.output_language,
        framework=body.framework, artifact_kind=body.artifact_kind,
        source_platform=body.source_platform, source_platform_version=body.source_platform_version,
        source_corpus=body.source_corpus, target_corpus=body.target_corpus,
    )
    return orch.status_payload(session, project)


@router.get("/spec")
def get_spec(project_id: int, session: Session = Depends(get_session)):
    """The reverse-engineered use-case spec + its review/approval state."""
    project = _get_project(project_id, session)
    return orch.get_spec(session, project.project_code)


class SpecBody(BaseModel):
    spec: dict


@router.put("/spec")
def put_spec(
    project_id: int,
    body: SpecBody,
    session: Session = Depends(get_session),
    user: AuthUser = Depends(current_user),
    _role=Depends(require_role("engineer")),
):
    """Edit the spec — re-opens review, voids any prior approval, stales edges."""
    project = _get_project(project_id, session)
    orch.update_spec(session, project, body.spec)
    _set_review_stage(session, project, complete=False)
    return orch.get_spec(session, project.project_code)


@router.post("/spec/approve")
def approve_spec(
    project_id: int,
    session: Session = Depends(get_session),
    user: AuthUser = Depends(current_user),
    _role=Depends(require_role("engineer")),
):
    """Approve the current spec: pin its hash, rebuild :USES_DATASET, unblock
    forward-engineering, and flip the review stage to complete."""
    project = _get_project(project_id, session)
    try:
        orch.approve_spec(session, project, by=(user.email or user.name or ""))
    except ValueError as e:
        raise HTTPException(422, str(e))
    _set_review_stage(session, project, complete=True)
    return orch.get_spec(session, project.project_code)


@router.post("/spec/reopen")
def reopen_spec(
    project_id: int,
    session: Session = Depends(get_session),
    user: AuthUser = Depends(current_user),
    _role=Depends(require_role("engineer")),
):
    """Send the spec back for edits (voids approval + stales edges)."""
    project = _get_project(project_id, session)
    try:
        orch.reopen_spec(session, project)
    except ValueError as e:
        raise HTTPException(422, str(e))
    _set_review_stage(session, project, complete=False)
    return orch.get_spec(session, project.project_code)


def _assemble(project: Project, session: Session):
    from .. import serving_package
    return serving_package.assemble_code_migration_package(
        project_code=project.project_code, product_name=project.name)


@router.post("/package")
def build_package(
    project_id: int,
    session: Session = Depends(get_session),
    user: AuthUser = Depends(current_user),
    _role=Depends(require_role("engineer")),
):
    """Assemble the downloadable package (old/ + new/ + spec + README) and, when
    git auto-push is on, push it. Advances the plan to packaged."""
    project = _get_project(project_id, session)
    _assemble(project, session)
    orch.record_packaged(session, project)
    actor = user.email or user.name or ""
    try:
        from .serving import _maybe_auto_push
        _maybe_auto_push(project.id, actor)
    except Exception:
        logger.warning("cmig auto git-push wiring failed for %s", project.project_code, exc_info=True)
    return orch.status_payload(session, project)


@router.get("/package")
def get_package(
    project_id: int,
    format: str = "zip",
    session: Session = Depends(get_session),
):
    """Download the self-contained code-migration package."""
    from .. import serving_package
    project = _get_project(project_id, session)
    pkg = _assemble(project, session)
    files = serving_package.collect_package_files(pkg)
    if format == "json":
        return {"project_code": project.project_code,
                "files": {k: (v if isinstance(v, str) else "<binary>") for k, v in files.items()}}
    return serving_package.zip_response(
        files, f"{project.project_code}-code-migration.zip",
        root_prefix=f"{project.project_code}-code-migration")
