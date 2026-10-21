"""Lakehouse (Parquet + DuckDB) serving endpoints.

Parallel to routers/materialization.py (dbt) and routers/serving.py (virtual
views). The `serving_lakehouse_export` non-LLM stage's Pipeline.tsx action POSTs
to `/api/projects/{id}/serving/export`; this router runs the export worker
(backend/lakehouse_export.py), which compiles the product SELECT with the shared
SQL core, extracts to Parquet via DuckDB, and registers a DuckDB catalog view.
"""
from __future__ import annotations

import json

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..authz import require_role
from ..database import get_session
from ..models import Project
from ..neo4j_client import neo4j_session
from .. import lakehouse_export
from .. import serving_package
from .. import transfer_execution


router = APIRouter(prefix="/api/projects/{project_id}/serving", tags=["lakehouse"])


class ExportBody(BaseModel):
    mode: str = "full"          # 'full' | 'sample'
    target_dir: str = ""        # override the resolved export dir
    compression: str = "snappy"
    sample_limit: int = 100


class TransferBody(BaseModel):
    placement: str = "hybrid"           # transform_on_extract | hybrid | transfer_then_transform
    write_disposition: str = "replace"  # replace | append
    # Empty = defer to the configured namespace (MaterializationTarget
    # .view_target_namespace, resolved in transfer_execution). A non-empty value
    # is a genuine explicit override — never default it to "public" or the
    # configured target schema (e.g. hr_core) gets silently clobbered.
    target_schema: str = ""


def _get_project(project_id: int, session: Session) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    return project


_STATUS_QUERY = """
MATCH (dc:DataContract {id: $contract_id})-[:MATERIALISES_AS]->(dp:DProdDataProduct)
MATCH (dp)-[:SERVED_BY]->(sd:ServingDefinition {servingMode: 'lakehouse_local'})
RETURN sd.buildStatus AS build_status, sd.targetDir AS target_dir,
       sd.catalogRef AS catalog_ref, sd.fileUris AS file_uris,
       sd.manifestUri AS manifest_uri, sd.summaryJson AS summary_json,
       sd.buildError AS build_error, toString(sd.builtAt) AS built_at
"""


class BuildBody(BaseModel):
    target_dir: str = ""        # override the resolved package dir
    compression: str = "snappy"


@router.post("/lakehouse/build")
def build_lakehouse(
    project_id: int,
    body: BuildBody | None = None,
    session: Session = Depends(get_session),
    _role=Depends(require_role("engineer")),
):
    """Build the runnable lakehouse package (models.json + run.py + README) WITHOUT
    running the export. This is the *Build* stage of Configure → Build → Deploy:
    it needs no live source connection, so the downloadable / git-pushable package
    appears as soon as it completes. The export itself runs on Deploy (/export)."""
    project = _get_project(project_id, session)
    body = body or BuildBody()
    try:
        result = lakehouse_export.build_lakehouse_package(
            project, session,
            target_dir=body.target_dir or None,
            compression=body.compression,
            executed_by="Data Engineer",
        )
    except RuntimeError as e:
        raise HTTPException(422, str(e))
    return result


@router.post("/export")
def export_lakehouse(
    project_id: int,
    body: ExportBody | None = None,
    session: Session = Depends(get_session),
    _role=Depends(require_role("engineer")),
):
    """Run the lakehouse export: compile → COPY to Parquet → DuckDB catalog →
    verify → persist :ServingDefinition. Synchronous (bounded by dataset size)."""
    project = _get_project(project_id, session)
    body = body or ExportBody()
    # Pre-flight: confirm the source platform supports being read by the DuckDB runner.
    try:
        from ..pg_resolver import resolve_source_connection_for_project
        from ..platform.registry import get_registry, CapabilityUnavailable
        platform, _, _ = resolve_source_connection_for_project(project, session)
        reg = get_registry()
        reg.assert_usable(platform, "lakehouse_source")
    except CapabilityUnavailable as e:
        raise HTTPException(400, str(e))
    except Exception:
        pass  # resolution failures surface during the actual export
    try:
        result = lakehouse_export.run_lakehouse_export(
            project, session,
            mode=body.mode,
            target_dir=body.target_dir or None,
            compression=body.compression,
            sample_limit=body.sample_limit,
            executed_by="Data Engineer",
        )
    except RuntimeError as e:
        raise HTTPException(422, str(e))
    return result


@router.get("/export/status")
def get_lakehouse_status(project_id: int, session: Session = Depends(get_session)):
    """Return the persisted lakehouse serving definition (build status, files,
    catalog ref) for the product, or {configured: False} when none exists."""
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"
    try:
        with neo4j_session(
            project.neo4j_host, project.neo4j_port,
            project.neo4j_user, project.neo4j_password, project.neo4j_database,
        ) as ns:
            row = ns.run(_STATUS_QUERY, contract_id=contract_id).single()
    except Exception:
        row = None
    if not row:
        return {"configured": False}

    def _decode(s, default):
        try:
            return json.loads(s) if s else default
        except (TypeError, ValueError):
            return default

    return {
        "configured": True,
        "build_status": row.get("build_status"),
        "target_dir": row.get("target_dir"),
        "catalog_ref": row.get("catalog_ref"),
        "file_uris": _decode(row.get("file_uris"), []),
        "manifest_uris": _decode(row.get("manifest_uri"), []),
        "summary": _decode(row.get("summary_json"), {}),
        "build_error": row.get("build_error"),
        "built_at": row.get("built_at"),
    }


@router.get("/lakehouse-package")
def get_lakehouse_package(
    project_id: int,
    format: str = "zip",
    session: Session = Depends(get_session),
):
    """Download the self-contained lakehouse package — the same package Data
    Workbench ran to export (run.py + query.py + explore.sql + data/*.parquet +
    catalog.duckdb + manifests + README). ``format=zip`` streams a zip (Parquet
    included as bytes); ``format=json`` returns only the text files (Parquet
    listed by path — use the zip for the data)."""
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"
    try:
        with neo4j_session(
            project.neo4j_host, project.neo4j_port,
            project.neo4j_user, project.neo4j_password, project.neo4j_database,
        ) as ns:
            row = ns.run(_STATUS_QUERY, contract_id=contract_id).single()
    except Exception:
        row = None
    target_dir = (row.get("target_dir") if row else None) or ""
    if not target_dir or not Path(target_dir).is_dir():
        raise HTTPException(404, "No lakehouse export found — run the Export to Lakehouse stage first.")

    files = serving_package.collect_package_files(Path(target_dir))
    if format == "json":
        return {"project_code": project.project_code,
                "files": {k: (v if isinstance(v, str) else f"<binary {len(v)} bytes>")
                          for k, v in files.items()}}
    return serving_package.zip_response(
        files, f"{project.project_code}-lakehouse.zip",
        root_prefix=f"{project.project_code}-lakehouse")


# ── Cross-platform transfer (transfer_then_transform) ─────────────────────────

_TRANSFER_STATUS_QUERY = """
MATCH (dc:DataContract {id: $contract_id})-[:MATERIALISES_AS]->(dp:DProdDataProduct)
MATCH (dp)-[:SERVED_BY]->(sd:ServingDefinition {servingMode: 'transfer_then_transform'})
RETURN sd.buildStatus AS build_status, sd.targetPlatform AS target_platform,
       sd.targetSchema AS target_schema, sd.placement AS placement,
       sd.manifestUri AS manifest_uri, sd.tablesJson AS tables_json,
       sd.summaryJson AS summary_json, sd.buildError AS build_error,
       toString(sd.builtAt) AS built_at
"""


@router.post("/transfer/build")
def build_transfer(
    project_id: int,
    body: TransferBody | None = None,
    session: Session = Depends(get_session),
    _role=Depends(require_role("engineer")),
):
    """Build (compile + assemble) the runnable dlt transfer package WITHOUT running
    it — the *Build* stage of Configure → Build → Run. Needs no live DB, so the
    downloadable / git-pushable package appears the moment it completes; the actual
    source→Parquet→target load runs on Deploy (POST /transfer)."""
    project = _get_project(project_id, session)
    body = body or TransferBody()
    try:
        return transfer_execution.build_transfer_package(
            project, session,
            write_disposition=body.write_disposition,
            executed_by="Data Engineer",
        )
    except RuntimeError as e:
        raise HTTPException(422, str(e))


@router.post("/transfer")
def run_transfer(
    project_id: int,
    body: TransferBody | None = None,
    session: Session = Depends(get_session),
    _role=Depends(require_role("engineer")),
):
    """Run a cross-platform transfer: execute the built dlt package — extract each
    dataset's shaped output from the source, stage Parquet, and load the configured
    target platform. Requires a target connection (Configure Serving)."""
    project = _get_project(project_id, session)
    body = body or TransferBody()
    try:
        return transfer_execution.run_transfer(
            project, session,
            placement=body.placement,
            write_disposition=body.write_disposition,
            target_schema=body.target_schema,
            executed_by="Data Engineer",
        )
    except RuntimeError as e:
        raise HTTPException(422, str(e))


@router.get("/transfer-package")
def get_transfer_package(
    project_id: int,
    format: str = "zip",
    session: Session = Depends(get_session),
):
    """Download the self-contained dlt transfer package (the same package DWB
    runs). ``format=zip`` streams a zip; ``format=json`` returns the text files."""
    project = _get_project(project_id, session)
    pkg = serving_package.package_dir(project.project_code, "transfer")
    if not pkg.is_dir() or not (pkg / "run.py").exists():
        raise HTTPException(404, "No transfer package — run the Build Transfer Pipeline stage first.")
    files = serving_package.collect_package_files(pkg)
    if format == "json":
        return {"project_code": project.project_code,
                "files": {k: (v if isinstance(v, str) else f"<binary {len(v)} bytes>")
                          for k, v in files.items()}}
    return serving_package.zip_response(
        files, f"{project.project_code}-transfer.zip",
        root_prefix=f"{project.project_code}-transfer")


@router.get("/transfer/status")
def get_transfer_status(project_id: int, session: Session = Depends(get_session)):
    """Return the persisted cross-platform transfer serving definition, or
    {configured: False} when none exists."""
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"
    try:
        with neo4j_session(
            project.neo4j_host, project.neo4j_port,
            project.neo4j_user, project.neo4j_password, project.neo4j_database,
        ) as ns:
            row = ns.run(_TRANSFER_STATUS_QUERY, contract_id=contract_id).single()
    except Exception:
        row = None
    if not row:
        return {"configured": False}

    def _dec(s, default):
        try:
            return json.loads(s) if s else default
        except (TypeError, ValueError):
            return default

    return {
        "configured": True,
        "build_status": row.get("build_status"),
        "target_platform": row.get("target_platform"),
        "target_schema": row.get("target_schema"),
        "placement": row.get("placement"),
        "manifest_uri": row.get("manifest_uri"),
        "tables": _dec(row.get("tables_json"), []),
        "summary": _dec(row.get("summary_json"), {}),
        "build_error": row.get("build_error"),
        "built_at": row.get("built_at"),
    }
