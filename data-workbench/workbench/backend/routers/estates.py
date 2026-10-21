"""Connected-Estate API — `/api/estates/*`.

A SEPARATE bounded context from the Pulse-backed `routers/discovery.py`. Here the
PO self-services: create an Estate + an EstateSource (a registered connection +
namespace policy), browse the live namespace tree, launch an async scan, review
scan status + per-namespace outcomes, pick candidate datasets, and (engineer-
gated) run the deeper profiling pass. Scans run on the leased ``estate_worker``.

The Pulse routes/files/UI are NOT touched — this is an independent composition.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlmodel import Session, func, select

from .. import estate as estate_mod
from .. import estate_enrich
from .. import estate_ingest
from .. import estate_manifest
from .. import estate_scan
from ..auth import AuthUser, current_user
from ..authz import require_role
from ..database import get_session
from ..models import (
    Estate,
    EstateScan,
    EstateScanNamespace,
    EstateSource,
    FeasibilityRun,
    FeasibilityScore,
    PlatformConnection,
)
from ..platform.dispatch import UnknownPlatform, get_discovery_provider
from ..platform.namespace import get_namespace_model

router = APIRouter(prefix="/api/estates", tags=["estates"])


# ── request/response shapes ───────────────────────────────────────────────────

class EstateCreate(BaseModel):
    name: str
    domain: Optional[str] = None
    description: str = ""


class SourceCreate(BaseModel):
    connection_id: Optional[int] = None   # required for live; omitted for offline
    name: str = ""
    catalog: str = ""   # Unity Catalog / database container (3-level platforms)
    ingest_mode: str = "live"             # live | offline
    platform: str = ""                    # required for offline (no connection to derive from)
    namespace_policy: dict = {"mode": "all", "namespaces": []}


class SourceUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    namespace_policy: Optional[dict] = None   # the saved schema selection ("tags")
    catalog: Optional[str] = None             # rejected once the source has scans


class ScanCreate(BaseModel):
    source_id: int
    depth: str = "metadata"                    # metadata | profiled
    namespace_policy: Optional[dict] = None    # optional per-scan override


class ProfileRequest(BaseModel):
    dataset_uris: list[str]


def _estate_row(e: Estate) -> dict:
    return {
        "id": e.id, "name": e.name, "domain": e.domain, "description": e.description,
        "status": e.status, "created_by": e.created_by,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


def _source_row(s: EstateSource) -> dict:
    return {
        "id": s.id, "estate_id": s.estate_id, "name": s.name, "platform": s.platform,
        "connection_id": s.connection_id, "catalog": getattr(s, "catalog", "") or "",
        "ingest_mode": getattr(s, "ingest_mode", "live") or "live",
        "enabled": s.enabled,
        "namespace_policy": json.loads(s.namespace_policy_json or "{}"),
    }


def _tolerant_json(raw: Optional[str]) -> dict:
    """Parse a JSON-object column defensively — a malformed value (e.g. a partial
    progress write) yields ``{}`` rather than 500-ing the whole scan listing."""
    try:
        val = json.loads(raw or "{}")
        return val if isinstance(val, dict) else {}
    except (TypeError, ValueError):
        return {}


def _scan_row(s: EstateScan) -> dict:
    return {
        "id": s.id, "estate_id": s.estate_id, "source_id": s.source_id,
        "scan_version": s.scan_version, "depth": s.depth, "state": s.state,
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "finished_at": s.finished_at.isoformat() if s.finished_at else None,
        "stats": json.loads(s.stats_json or "{}"),
        "error": json.loads(s.error_json or "{}"),
        "scan_progress": _tolerant_json(getattr(s, "scan_progress_json", None)),
        "initiated_by": s.initiated_by,
        "enrichment_state": getattr(s, "enrichment_state", None),
        "enriched_at": (getattr(s, "enriched_at", None) or None) and
                       getattr(s, "enriched_at").isoformat() if getattr(s, "enriched_at", None) else None,
        "enrichment_progress": json.loads(getattr(s, "enrichment_progress_json", None) or "{}"),
        "enrichment_summary": json.loads(getattr(s, "enrichment_summary_json", None) or "{}"),
    }


# ── estate CRUD ────────────────────────────────────────────────────────────────

@router.post("")
def create_estate(
    body: EstateCreate,
    session: Session = Depends(get_session),
    user: AuthUser = Depends(current_user),
    _role=Depends(require_role("owner")),
):
    estate = Estate(name=body.name, domain=body.domain, description=body.description,
                    created_by=user.email)
    session.add(estate)
    session.commit()
    session.refresh(estate)
    return _estate_row(estate)


@router.get("")
def list_estates(session: Session = Depends(get_session)):
    estates = session.exec(select(Estate).order_by(Estate.created_at.desc())).all()  # type: ignore[union-attr]
    return {"estates": [_estate_row(e) for e in estates], "count": len(estates)}


@router.get("/{estate_id}")
def get_estate(estate_id: int, session: Session = Depends(get_session)):
    estate = session.get(Estate, estate_id)
    if estate is None:
        raise HTTPException(404, f"Estate {estate_id} not found")
    sources = session.exec(select(EstateSource).where(EstateSource.estate_id == estate_id)).all()
    scans = session.exec(
        select(EstateScan).where(EstateScan.estate_id == estate_id).order_by(EstateScan.id.desc())  # type: ignore[union-attr]
    ).all()
    return {
        **_estate_row(estate),
        "sources": [_source_row(s) for s in sources],
        "scans": [_scan_row(s) for s in scans],
        "latest_completed_scan": next(
            (_scan_row(s) for s in scans if s.state == "completed"), None
        ),
    }


@router.delete("/{estate_id}")
def archive_estate(estate_id: int, session: Session = Depends(get_session),
                   _role=Depends(require_role("owner"))):
    """Archive an estate and cascade-clean its children: every source's scans,
    per-namespace rows, feasibility runs/scores, and the whole graph subtree are
    removed so nothing is orphaned. The ``Estate`` SQL row is kept, flagged
    ``archived`` (soft estate, hard-clean children)."""
    estate = session.get(Estate, estate_id)
    if estate is None:
        raise HTTPException(404, f"Estate {estate_id} not found")
    active = session.exec(
        select(EstateScan).where(
            EstateScan.estate_id == estate_id,
            EstateScan.state.in_(["queued", "running"]),  # type: ignore[attr-defined]
        )
    ).first()
    if active is not None:
        raise HTTPException(409, {"message": "A scan is still running in this estate — "
                                             "wait for it to finish before archiving.",
                                  "scan_id": active.id})
    sources = session.exec(
        select(EstateSource).where(EstateSource.estate_id == estate_id)
    ).all()
    _cascade_delete_sources(session, estate_id, list(sources))
    try:
        estate_mod.delete_estate_graph(session, estate_id)
    except Exception:  # noqa: BLE001 — best-effort graph cleanup
        pass
    estate.status = "archived"
    estate.updated_at = datetime.utcnow()
    session.add(estate)
    session.commit()
    return {"archived": True, "id": estate_id}


# ── sources ────────────────────────────────────────────────────────────────────

@router.post("/{estate_id}/sources")
def add_source(estate_id: int, body: SourceCreate, session: Session = Depends(get_session),
               _role=Depends(require_role("owner"))):
    estate = session.get(Estate, estate_id)
    if estate is None:
        raise HTTPException(404, f"Estate {estate_id} not found")
    catalog = (body.catalog or "").strip()
    mode = (body.ingest_mode or "live").strip().lower()

    if mode == "offline":
        # No connection / creds — the client runs the extraction kit and uploads a
        # manifest. A named source tied to a platform (+ optional catalog) is all we
        # need; discovery + scan happen in the client's environment.
        platform = (body.platform or "").strip().lower()
        if not platform:
            raise HTTPException(422, {"message": "offline source needs a platform "
                                                 "(e.g. postgres, snowflake)."})
        source = EstateSource(
            estate_id=estate_id, connection_id=None, ingest_mode="offline",
            name=body.name or (f"{platform} · {catalog}" if catalog else f"{platform} (offline)"),
            platform=platform, catalog=catalog,
            namespace_policy_json=json.dumps(body.namespace_policy or {"mode": "all", "namespaces": []}),
        )
    else:
        if body.connection_id is None:
            raise HTTPException(422, {"message": "a live source needs a connection_id."})
        conn = session.get(PlatformConnection, body.connection_id)
        if conn is None:
            raise HTTPException(404, f"Connection {body.connection_id} not found")
        source = EstateSource(
            estate_id=estate_id, connection_id=body.connection_id, ingest_mode="live",
            name=body.name or (f"{conn.connection_name} · {catalog}" if catalog else conn.connection_name),
            platform=conn.platform_type, catalog=catalog,
            namespace_policy_json=json.dumps(body.namespace_policy or {"mode": "all", "namespaces": []}),
        )
    session.add(source)
    session.commit()
    session.refresh(source)
    return _source_row(source)


@router.patch("/{estate_id}/sources/{source_id}")
def update_source(estate_id: int, source_id: int, body: SourceUpdate,
                  session: Session = Depends(get_session),
                  _role=Depends(require_role("owner"))):
    """Edit a source's name / enabled flag / saved schema selection. ``catalog`` is
    immutable once the source has any scan (changing it would orphan the graph
    identities that embed the old catalog — delete and re-add instead)."""
    source = session.get(EstateSource, source_id)
    if source is None or source.estate_id != estate_id:
        raise HTTPException(404, f"Source {source_id} not found for estate {estate_id}")
    if body.name is not None:
        source.name = body.name
    if body.enabled is not None:
        source.enabled = body.enabled
    if body.namespace_policy is not None:
        source.namespace_policy_json = json.dumps(body.namespace_policy)
    if body.catalog is not None and (body.catalog or "").strip() != (source.catalog or ""):
        has_scans = session.exec(
            select(EstateScan.id).where(EstateScan.source_id == source_id)
        ).first()
        if has_scans is not None:
            raise HTTPException(409, {
                "message": "Catalog is immutable once a source has been scanned — "
                           "delete this source and add a new one for the other catalog."})
        source.catalog = (body.catalog or "").strip()
    source.updated_at = datetime.utcnow()
    session.add(source)
    session.commit()
    session.refresh(source)
    return _source_row(source)


@router.delete("/{estate_id}/sources/{source_id}")
def delete_source(estate_id: int, source_id: int,
                  session: Session = Depends(get_session),
                  _role=Depends(require_role("owner"))):
    """Hard-delete a source and cascade: its graph subtree (scans + datasets +
    columns), its SQL scan rows, and any feasibility runs whose evidence included
    one of its scans (that evidence is now invalidated)."""
    source = session.get(EstateSource, source_id)
    if source is None or source.estate_id != estate_id:
        raise HTTPException(404, f"Source {source_id} not found for estate {estate_id}")
    active = session.exec(
        select(EstateScan).where(
            EstateScan.source_id == source_id,
            EstateScan.state.in_(["queued", "running"]),  # type: ignore[attr-defined]
        )
    ).first()
    if active is not None:
        raise HTTPException(409, {"message": "A scan is still running for this source — "
                                             "wait for it to finish before deleting.",
                                  "scan_id": active.id})
    _cascade_delete_sources(session, estate_id, [source])
    return {"deleted": True, "id": source_id}


def _cascade_delete_sources(session: Session, estate_id: int,
                            sources: list[EstateSource]) -> None:
    """Shared cascade for source-delete and estate-archive. Removes each source's
    graph subtree, its feasibility runs/scores, its scan + namespace rows, and the
    source row itself."""
    for source in sources:
        scan_ids = [row for row in session.exec(
            select(EstateScan.id).where(EstateScan.source_id == source.id)
        ).all()]
        # Feasibility runs whose evidence set intersects this source's scans.
        if scan_ids:
            scan_id_set = set(scan_ids)
            for run in session.exec(
                select(FeasibilityRun).where(FeasibilityRun.estate_id == estate_id)
            ).all():
                run_scans = set(_run_scan_ids(run))
                if run_scans & scan_id_set:
                    for score in session.exec(
                        select(FeasibilityScore).where(FeasibilityScore.run_id == run.id)
                    ).all():
                        session.delete(score)
                    session.delete(run)
        # SQL scan rows + per-namespace outcomes.
        for sid in scan_ids:
            for nsrow in session.exec(
                select(EstateScanNamespace).where(EstateScanNamespace.scan_id == sid)
            ).all():
                session.delete(nsrow)
            scan = session.get(EstateScan, sid)
            if scan is not None:
                session.delete(scan)
        session.commit()
        # Graph subtree (best-effort — never block SQL cleanup on a graph error).
        try:
            estate_mod.delete_source_graph(session, estate_id, source.id)
        except Exception:  # noqa: BLE001
            pass
        session.delete(source)
        session.commit()


def _run_scan_ids(run: FeasibilityRun) -> list[int]:
    """The full scan set a feasibility run read (multi-catalog), falling back to
    the legacy single ``scan_id`` for runs created before scan_ids_json shipped."""
    try:
        ids = json.loads(getattr(run, "scan_ids_json", "") or "[]")
        if ids:
            return [int(x) for x in ids]
    except (TypeError, ValueError):
        pass
    return [run.scan_id] if run.scan_id else []


@router.get("/{estate_id}/connections/{connection_id}/catalogs")
def list_connection_catalogs(estate_id: int, connection_id: int,
                             session: Session = Depends(get_session),
                             _role=Depends(require_role("owner"))):
    """Enumerate a connection's Unity Catalog / database catalogs so the add-source
    form offers a picker instead of free text. Only 3-level platforms (a ``catalog``
    container part, e.g. Databricks/Snowflake) return options; 2-level platforms
    report ``supported: false``. Best-effort — a probe failure returns empty."""
    conn = session.get(PlatformConnection, connection_id)
    if conn is None:
        raise HTTPException(404, f"Connection {connection_id} not found")
    platform = conn.platform_type
    try:
        model = get_namespace_model(platform)
    except KeyError:
        return {"supported": False, "platform": platform, "catalogs": []}
    if "catalog" not in model.container_parts:
        return {"supported": False, "platform": platform, "catalogs": []}
    from ..platform.secrets import resolve_secret
    connection_ref = {
        "connection_id": str(conn.id), "host": conn.host, "port": conn.port,
        "database": conn.database, "username": conn.username,
        "resolved_password": resolve_secret(conn.secret_ref),
        "extra_config": json.loads(conn.extra_config_json or "{}"),
    }
    raw: list[dict] = []
    try:
        provider = get_discovery_provider(platform)
        if hasattr(provider, "list_catalogs_and_schemas"):
            raw = provider.list_catalogs_and_schemas(connection_ref) or []
    except Exception:  # noqa: BLE001 — best-effort; UI falls back to free text
        raw = []
    # Normalize into per-catalog {catalog, schemas, schemas_enumerated}. NEVER treat
    # an empty `schemas` list as "couldn't enumerate": the providers overload `[]`
    # (Snowflake with no connected DB, a Databricks `SHOW SCHEMAS` failure), so a
    # non-empty list is the only positive signal. Unknown → schemas:null +
    # schemas_enumerated:false so the UI shows "all schemas (not enumerated)" and the
    # scan defaults to mode:all — honest, never a silent scan-everything.
    catalogs: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        cat = entry.get("catalog")
        if not cat:
            continue
        schemas = entry.get("schemas")
        if isinstance(schemas, list) and schemas:
            catalogs.append({
                "catalog": cat,
                "schemas": sorted(str(x) for x in schemas),
                "schemas_enumerated": True,
            })
        else:
            catalogs.append({"catalog": cat, "schemas": None, "schemas_enumerated": False})
    return {"supported": True, "platform": platform, "catalogs": catalogs}


@router.get("/{estate_id}/sources")
def list_sources(estate_id: int, session: Session = Depends(get_session)):
    sources = session.exec(select(EstateSource).where(EstateSource.estate_id == estate_id)).all()
    return {"sources": [_source_row(s) for s in sources], "count": len(sources)}


@router.get("/{estate_id}/sources/{source_id}/namespaces")
def list_source_namespaces(estate_id: int, source_id: int,
                           with_counts: bool = Query(False),
                           session: Session = Depends(get_session)):
    """Browse the source's live namespace tree (PO self-service metadata)."""
    source = session.get(EstateSource, source_id)
    if source is None or source.estate_id != estate_id:
        raise HTTPException(404, f"Source {source_id} not found for estate {estate_id}")
    platform, connection_ref = estate_mod.resolve_source_connection_ref(session, source)
    try:
        provider = get_discovery_provider(platform)
    except UnknownPlatform as exc:
        raise HTTPException(422, str(exc))
    namespaces = provider.list_namespaces(connection_ref) or []
    out = []
    for ns in namespaces:
        entry: dict[str, Any] = {"name": ".".join(p for p in (ns.parts or []) if p),
                                 "parts": ns.parts}
        if with_counts:
            try:
                entry["relation_count"] = len(provider.list_relations(connection_ref, ns) or [])
            except Exception:
                entry["relation_count"] = None
        out.append(entry)
    return {"platform": platform, "namespaces": out, "count": len(out)}


@router.get("/{estate_id}/sources/{source_id}/namespaces/{namespace}/relations")
def list_namespace_relations(estate_id: int, source_id: int, namespace: str,
                             session: Session = Depends(get_session)):
    source = session.get(EstateSource, source_id)
    if source is None or source.estate_id != estate_id:
        raise HTTPException(404, f"Source {source_id} not found for estate {estate_id}")
    platform, connection_ref = estate_mod.resolve_source_connection_ref(session, source)
    try:
        provider = get_discovery_provider(platform)
    except UnknownPlatform as exc:
        raise HTTPException(422, str(exc))
    from ..platform.interfaces import NamespaceRef
    ns = NamespaceRef(platform_instance_id=str(source.connection_id),
                      parts=[p for p in namespace.split(".") if p])
    rels = provider.list_relations(connection_ref, ns) or []
    return {"relations": [{"name": r.name, "kind": r.relation_kind} for r in rels],
            "count": len(rels)}


# ── scans ──────────────────────────────────────────────────────────────────────

@router.post("/{estate_id}/scans")
def launch_scan(estate_id: int, body: ScanCreate, session: Session = Depends(get_session),
                user: AuthUser = Depends(current_user),
                _role=Depends(require_role("owner"))):
    estate = session.get(Estate, estate_id)
    if estate is None:
        raise HTTPException(404, f"Estate {estate_id} not found")
    source = session.get(EstateSource, body.source_id)
    if source is None or source.estate_id != estate_id:
        raise HTTPException(404, f"Source {body.source_id} not found for estate {estate_id}")

    # One active scan per source (idempotency / concurrent-run guard).
    active = session.exec(
        select(EstateScan).where(
            EstateScan.source_id == body.source_id,
            EstateScan.state.in_(["queued", "running"]),  # type: ignore[attr-defined]
        )
    ).first()
    if active is not None:
        raise HTTPException(409, {"message": "A scan is already active for this source.",
                                  "scan_id": active.id, "state": active.state})

    # Optional per-scan namespace-policy override (persisted onto the source).
    if body.namespace_policy is not None:
        source.namespace_policy_json = json.dumps(body.namespace_policy)
        source.updated_at = datetime.utcnow()
        session.add(source)

    max_v = session.exec(
        select(func.max(EstateScan.scan_version)).where(
            EstateScan.estate_id == estate_id, EstateScan.source_id == body.source_id
        )
    ).one()
    next_version = (max_v or 0) + 1

    scan = EstateScan(estate_id=estate_id, source_id=body.source_id,
                      scan_version=next_version, depth=body.depth, state="queued",
                      initiated_by=user.email)
    session.add(scan)
    session.commit()
    session.refresh(scan)
    return _scan_row(scan)


@router.get("/{estate_id}/scans")
def list_scans(estate_id: int, session: Session = Depends(get_session)):
    scans = session.exec(
        select(EstateScan).where(EstateScan.estate_id == estate_id).order_by(EstateScan.id.desc())  # type: ignore[union-attr]
    ).all()
    return {"scans": [_scan_row(s) for s in scans], "count": len(scans)}


@router.get("/scans/{scan_id}")
def get_scan(scan_id: int, session: Session = Depends(get_session)):
    scan = session.get(EstateScan, scan_id)
    if scan is None:
        raise HTTPException(404, f"Scan {scan_id} not found")
    namespaces = session.exec(
        select(EstateScanNamespace).where(EstateScanNamespace.scan_id == scan_id)
    ).all()
    return {
        **_scan_row(scan),
        "namespace_outcomes": [
            {"namespace": n.namespace, "outcome": n.outcome,
             "relation_count": n.relation_count, "column_count": n.column_count,
             "detail": n.detail}
            for n in namespaces
        ],
    }


@router.get("/scans/{scan_id}/datasets")
def scan_datasets(scan_id: int, session: Session = Depends(get_session)):
    """The raw datasets + columns observed by a scan — candidate-selection surface."""
    scan = session.get(EstateScan, scan_id)
    if scan is None:
        raise HTTPException(404, f"Scan {scan_id} not found")
    datasets = estate_mod.read_scan_datasets(session, scan_id)
    return {"scan_id": scan_id, "datasets": datasets, "count": len(datasets)}


@router.get("/scans/{scan_id}/assets")
def scan_assets(scan_id: int, session: Session = Depends(get_session)):
    """Code assets (tasks/notebooks/pipelines) observed by a scan."""
    scan = session.get(EstateScan, scan_id)
    if scan is None:
        raise HTTPException(404, f"Scan {scan_id} not found")
    assets = estate_mod.read_scan_assets(session, scan_id)
    return {"scan_id": scan_id, "assets": assets, "count": len(assets)}


@router.post("/scans/{scan_id}/profile")
def profile_scan_datasets(scan_id: int, body: ProfileRequest,
                          session: Session = Depends(get_session),
                          _role=Depends(require_role("engineer"))):
    """Deeper pass: PII-safe profiling of PO-selected datasets (engineer-gated —
    this reads live data)."""
    scan = session.get(EstateScan, scan_id)
    if scan is None:
        raise HTTPException(404, f"Scan {scan_id} not found")
    result = estate_scan.profile_datasets(session, scan_id, body.dataset_uris)
    if result.get("error"):
        raise HTTPException(422, result)
    return result


# ── offline extraction (no live connection) ──────────────────────────────────

# Hardened upload policy (the manifest is client-authored, untrusted input).
_MANIFEST_ALLOWED_EXTS = {".yaml", ".yml"}
_MANIFEST_MAX_BYTES = 25 * 1024 * 1024  # 25 MB — a big estate manifest is text-heavy


def _read_uploaded_manifest(file: UploadFile, data: bytes) -> str:
    """Fail-closed multipart read (allowlist + size cap + traversal/NUL/UTF-8),
    mirroring ``routers/code_migration.import_code``. Returns the decoded YAML."""
    import os
    raw_name = file.filename or "manifest.yaml"
    name = os.path.basename(raw_name.replace("\\", "/"))
    if not name or name.startswith(".") or "/" in name:
        raise HTTPException(422, f"unsafe filename: {raw_name!r}")
    ext = os.path.splitext(name)[1].lower()
    if ext not in _MANIFEST_ALLOWED_EXTS:
        raise HTTPException(422, f"disallowed file type {ext!r} (expected .yaml/.yml)")
    if len(data) > _MANIFEST_MAX_BYTES:
        raise HTTPException(422, f"{name} exceeds the {_MANIFEST_MAX_BYTES} byte limit")
    if b"\x00" in data:
        raise HTTPException(422, f"{name} looks binary (NUL byte) — only text YAML is accepted")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(422, f"{name} is not valid UTF-8 text")


def _get_source(source_id: int, session: Session) -> tuple[EstateSource, Estate]:
    source = session.get(EstateSource, source_id)
    if source is None:
        raise HTTPException(404, f"Source {source_id} not found")
    estate = session.get(Estate, source.estate_id)
    if estate is None:
        raise HTTPException(404, f"Estate {source.estate_id} not found")
    return source, estate


@router.get("/sources/{source_id}/extraction-package")
def get_extraction_package(source_id: int, format: str = Query("zip"),
                           session: Session = Depends(get_session),
                           _role=Depends(require_role("owner"))):
    """Download the tailored, client-run extraction kit (a ZIP) for an offline
    source. ``format=json`` returns the file map instead of a zip attachment."""
    from .. import extraction_package as ep
    source, _ = _get_source(source_id, session)
    catalog = getattr(source, "catalog", "") or ""
    if format == "json":
        return {"files": ep.extraction_package_files(
            source_id, source.platform, source_name=source.name, catalog=catalog)}
    return ep.extraction_zip_response(
        source_id, source.platform, source_name=source.name, catalog=catalog)


@router.post("/sources/{source_id}/import-manifest/preview")
async def import_manifest_preview(source_id: int, file: UploadFile = File(...),
                                  session: Session = Depends(get_session),
                                  _role=Depends(require_role("owner"))):
    """Parse + validate + summarize an uploaded manifest with NO graph write —
    the confirm-before-commit preview (counts, per-schema breakdown, redaction
    summary, PII-flagged columns, warnings)."""
    _get_source(source_id, session)
    raw = _read_uploaded_manifest(file, await file.read())
    try:
        return estate_ingest.preview_estate_manifest(raw)
    except estate_manifest.EstateManifestValidationError as e:
        raise HTTPException(422, {"message": "manifest failed validation", "detail": str(e)})


@router.post("/sources/{source_id}/import-manifest")
async def import_manifest(source_id: int, file: UploadFile = File(...),
                          session: Session = Depends(get_session),
                          user: AuthUser = Depends(current_user),
                          _role=Depends(require_role("owner"))):
    """Confirm the import: replay the validated manifest into a real ``:EstateScan``
    (identical shape to a live scan). Hardened multipart upload + fail-closed
    validation; a malformed manifest is rejected before anything touches the graph."""
    source, estate = _get_source(source_id, session)
    raw = _read_uploaded_manifest(file, await file.read())
    try:
        manifest = estate_manifest.load_manifest(raw)
    except estate_manifest.EstateManifestValidationError as e:
        raise HTTPException(422, {"message": "manifest failed validation", "detail": str(e)})
    # Guard: manifest platform should match the source's declared platform.
    if source.platform and manifest.platform != source.platform.lower():
        raise HTTPException(422, {
            "message": f"manifest platform '{manifest.platform}' does not match "
                       f"the source platform '{source.platform}'."})
    result = estate_ingest.import_estate_manifest(
        session, estate=estate, source=source, manifest=manifest, initiated_by=user.email)
    return {"imported": True, **result}


@router.post("/{estate_id}/enrich")
def enrich_estate(estate_id: int, session: Session = Depends(get_session),
                  _role=Depends(require_role("owner"))):
    """Queue metadata enrichment for the latest completed/partial scan of the estate.

    Enrichment runs LLM-generated descriptions over all estate columns + tables
    (one call per schema-group) and writes them back to the graph. This improves
    feasibility matching accuracy by giving the schema_dna S3 semantic axis real
    descriptions instead of bare column names.

    Returns ``{scan_id, enrichment_state: 'enriching'}`` immediately; the
    estate_worker drains the enrichment queue alongside scans.
    """
    estate = session.get(Estate, estate_id)
    if estate is None:
        raise HTTPException(404, f"Estate {estate_id} not found")

    sources = session.exec(
        select(EstateSource).where(EstateSource.estate_id == estate_id,
                                   EstateSource.enabled == True)  # noqa: E712
    ).all()
    if not sources:
        raise HTTPException(422, {"message": "No enabled sources in this estate."})

    # Find the latest completed|partial scan across all enabled sources.
    target_scan: Optional[EstateScan] = None
    for src in sources:
        scan = session.exec(
            select(EstateScan).where(
                EstateScan.source_id == src.id,
                EstateScan.state.in_(["completed", "partial"]),  # type: ignore[attr-defined]
            ).order_by(EstateScan.scan_version.desc())  # type: ignore[union-attr]
        ).first()
        if scan is not None:
            if target_scan is None or scan.id > target_scan.id:
                target_scan = scan

    if target_scan is None:
        raise HTTPException(422, {
            "message": "No completed scan found for this estate — run a scan first."
        })

    if getattr(target_scan, "enrichment_state", None) == "enriching":
        return {"scan_id": target_scan.id, "enrichment_state": "enriching",
                "message": "Enrichment already in progress."}

    target_scan.enrichment_state = "enriching"
    target_scan.updated_at = datetime.utcnow()
    session.add(target_scan)
    session.commit()
    return {"scan_id": target_scan.id, "enrichment_state": "enriching"}
