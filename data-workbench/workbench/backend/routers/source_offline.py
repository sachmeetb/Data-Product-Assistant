"""Offline source-aligned (dpe-sa) discovery — upload a metadata manifest + seed.

Phase 2 of Offline Extraction (user guide: ``docs/offline-extraction.md``). When a
client can't grant a live connection, the PO uploads the reviewable offline
extraction manifest to a ``dpe-sa`` project (``data_connectivity_mode='offline'``);
the deterministic ``data_discovery_offline`` stage then seeds the project catalog +
DQV profiling graph from it (``source_manifest_seed``) — no live source, no LLM.

Two endpoints mirror the migration ``seed-schema`` pattern: upload+preview (stores
the manifest, no graph write), then seed (materializes it). The UI marks the stage
complete via ``/stages/{n}/complete`` ONLY when the load verified.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlmodel import Session

from .. import estate_ingest, estate_manifest, source_manifest_seed
from ..auth import AuthUser, current_user
from ..authz import require_role
from ..config import BASE_PROJECT_DIR
from ..database import get_session
from ..models import Project
from .estates import _read_uploaded_manifest

router = APIRouter(prefix="/api/projects/{project_id}/discovery", tags=["source-offline"])


def _get_offline_project(project_id: int, session: Session) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    if getattr(project, "archetype", "") != "dpe-sa":
        raise HTTPException(400, "Offline manifest import is a source-aligned (dpe-sa) capability.")
    return project


def _manifest_path(project_code: str) -> Path:
    return BASE_PROJECT_DIR / project_code / "offline" / "source-manifest.yaml"


@router.post("/upload-manifest")
async def upload_manifest(project_id: int, file: UploadFile = File(...),
                          session: Session = Depends(get_session),
                          user: AuthUser = Depends(current_user),
                          _role=Depends(require_role("engineer"))):
    """Validate + store an offline extraction manifest on the project (NO graph
    write). Returns the preview summary (counts / redaction / PII / warnings). The
    stored manifest is seeded by ``seed-offline``."""
    project = _get_offline_project(project_id, session)
    raw = _read_uploaded_manifest(file, await file.read())
    try:
        summary = estate_ingest.preview_estate_manifest(raw)   # parse + validate
    except estate_manifest.EstateManifestValidationError as e:
        raise HTTPException(422, {"message": "manifest failed validation", "detail": str(e)})
    path = _manifest_path(project.project_code)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(raw, encoding="utf-8")
    tmp.replace(path)
    return {"stored": True, **summary}


@router.post("/seed-offline")
def seed_offline(project_id: int, session: Session = Depends(get_session),
                 user: AuthUser = Depends(current_user),
                 _role=Depends(require_role("engineer"))):
    """Deterministically seed the project catalog + profiling graph from the stored
    manifest (``source_manifest_seed``). Returns the load result; ``ok`` is True only
    when node counts verify. The UI marks ``data_discovery_offline`` complete on ``ok``."""
    project = _get_offline_project(project_id, session)
    path = _manifest_path(project.project_code)
    if not path.exists():
        raise HTTPException(409, {"message": "No manifest uploaded yet — upload one first."})
    try:
        manifest = estate_manifest.load_manifest(path.read_text(encoding="utf-8"))
    except estate_manifest.EstateManifestValidationError as e:
        raise HTTPException(422, {"message": "stored manifest failed validation", "detail": str(e)})
    try:
        result = source_manifest_seed.seed_graph_from_manifest(project, manifest)
    except source_manifest_seed.SeedError as e:
        raise HTTPException(422, {"message": str(e)})
    return result
