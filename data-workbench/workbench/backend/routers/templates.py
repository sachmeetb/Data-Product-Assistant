"""Blueprint Library — a project-independent, ODCS-native template catalogue.

The single source of truth for data-product **spec templates**: browse / search /
clone / edit / import / export / version, gated by ``templateStatus``
(draft | published) and tagged by ``templateOrigin`` (seed | clone | import |
authored). Every workflow-facing consumer (feasibility scan, the ``dpe-cf``
wizard clone list, Pulse reference models) reads only ``published`` templates.

Storage + the ODCS reuse discipline live in ``template_store``. Feasibility reads
the published templates **live from the graph** (``template_corpus.load_specs_from_graph``),
so publish/retract just flip ``templateStatus`` — no corpus regeneration. Named
"Blueprint Library" (never "catalog", which collides with the estate ``:Catalog``
/ workflow catalog).
"""
from __future__ import annotations

import re
from typing import Any, Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session

from ..auth import current_user
from ..database import get_session
from .. import embeddings
from .. import feasibility_map as fm
from .. import feasibility_spec as fs
from .. import template_store
from ..template_store import (
    TemplateReadOnly,
    canonicalize_template,
    spec_id_from_template_id,
)
from .._contract_versioning import DiscardNotAllowed

router = APIRouter(prefix="/api/templates", tags=["templates"])


# ── request bodies ─────────────────────────────────────────────────────────

class TemplateSpecBody(BaseModel):
    spec: dict[str, Any]
    change_kind: Optional[str] = "auto"
    revision_notes: Optional[str] = ""


class TemplateImportBody(BaseModel):
    content: str
    source: Optional[str] = None  # "yaml" | "json" | None (auto-detect)


# ── helpers ────────────────────────────────────────────────────────────────

def _caller_email(request: Request) -> str:
    try:
        return (current_user(request).email or "").strip()
    except Exception:
        return ""


def _existing_ids(session: Session) -> set[str]:
    return {r["id"] for r in template_store.list_templates_raw(session)}


def _unique_id(session: Session, base_id: str) -> str:
    existing = _existing_ids(session)
    if base_id not in existing:
        return base_id
    i = 2
    while f"{base_id}-{i}" in existing:
        i += 1
    return f"{base_id}-{i}"


def _summary(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row.get("name") or "",
        "domain": row.get("domain") or "",
        "description": row.get("description") or "",
        "status": row.get("status") or "draft",
        "origin": row.get("origin") or "authored",
        "owner_email": row.get("owner_email") or "",
        "product_kind": row.get("product_kind") or "",
        "cloned_from": row.get("cloned_from"),
        "version": row.get("version") or 1,
        "tags": list(row.get("tags") or []),
        "sub_domain": row.get("data_product") or "",
        "source_spec_id": row.get("source_spec_id") or "",
    }


def _read_or_404(contract_id: str, session: Session, version: Optional[int] = None) -> dict:
    spec = template_store._read_template_from_graph(contract_id, session, version=version)
    if not spec or not spec.get("isTemplate"):
        raise HTTPException(404, f"Template not found: {contract_id}")
    return spec


def _visible_rows(session: Session, caller: str) -> list[dict]:
    """Rows visible to the caller: ``published ∪ (caller's own drafts)``."""
    out = []
    for r in template_store.list_templates_raw(session):
        st = r.get("status") or "draft"
        if st == "published" or (caller and r.get("owner_email") == caller):
            out.append(r)
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _template_completeness(spec: dict) -> dict:
    """Design-completeness ("what's missing") for a template spec. Operational
    ODCS fields (SLA / servers / support / owners) are reported informationally
    and do NOT count against the template — they're set at instantiation."""
    schemas = spec.get("schema") or []
    all_props = [p for s in schemas for p in (s.get("properties") or [])]
    n = len(all_props)
    described = sum(1 for p in all_props if (p.get("description") or "").strip())
    typed = sum(1 for p in all_props
                if (p.get("physicalType") or p.get("logicalType") or "").strip())
    pks = [p for p in all_props if p.get("primaryKey")]
    required = [p for p in all_props if p.get("required")]
    feas = (spec.get("customProperties") or {}).get("feasibility") or {}
    grain_keys = feas.get("grain_keys") or (
        (schemas[0].get("transform") or {}).get("grouping_keys") if schemas else []
    ) or []
    dataset_described = bool(schemas) and all(
        (s.get("description") or "").strip() for s in schemas
    )

    checks: list[dict] = []

    def add(cid, label, ok, detail=""):
        checks.append({"id": cid, "label": label,
                       "status": "set" if ok else "missing", "detail": detail})

    add("product_description", "Product description", bool((spec.get("description") or "").strip()))
    add("purpose", "Purpose / intended use", bool((spec.get("purpose") or "").strip()))
    add("has_dataset", "At least one dataset", len(schemas) > 0)
    add("dataset_description", "Dataset descriptions", dataset_described)
    add("has_columns", "At least one column", n > 0)
    add("field_descriptions", "Field descriptions", n > 0 and described == n,
        f"{described}/{n} described")
    add("primary_key", "Primary / grain key declared", bool(pks) or bool(grain_keys))
    add("required_flags", "Required-flag decisions", n > 0 and len(required) > 0,
        f"{len(required)}/{n} required")
    add("rich_types", "Column types", n > 0 and typed == n, f"{typed}/{n} typed")

    set_count = sum(1 for c in checks if c["status"] == "set")
    completeness = round(100 * set_count / len(checks)) if checks else 0
    band = "green" if completeness >= 80 else "amber" if completeness >= 50 else "red"

    operational = [
        {"id": "owners", "label": "Owners", "status": "set" if spec.get("owners") else "empty"},
        {"id": "sla", "label": "SLA properties", "status": "set" if spec.get("slaProperties") else "empty"},
        {"id": "servers", "label": "Servers", "status": "set" if spec.get("servers") else "empty"},
        {"id": "support", "label": "Support contacts", "status": "set" if spec.get("support") else "empty"},
        {"id": "roles", "label": "Access roles", "status": "set" if spec.get("roles") else "empty"},
    ]
    col_gaps = [
        {"name": p.get("name", ""),
         "missing_description": not (p.get("description") or "").strip(),
         "missing_type": not (p.get("physicalType") or p.get("logicalType") or "").strip()}
        for p in all_props
    ]
    return {
        "band": band,
        "completeness": completeness,
        "design": {"checklist": checks,
                   "missing": [c["id"] for c in checks if c["status"] == "missing"]},
        "columns": {"total": n, "described": described, "typed": typed,
                    "gaps": [g for g in col_gaps
                             if g["missing_description"] or g["missing_type"]]},
        "operational": {
            "checklist": operational,
            "note": ("Operational fields are set when the template is instantiated "
                     "into a product; they do not count against template completeness."),
        },
    }


# ── list / domains ──────────────────────────────────────────────────────────

@router.get("/domains")
def list_domains(session: Session = Depends(get_session)):
    """Domains present across the corpus (feasibility) ∪ the Library templates."""
    corpus = set(fs.list_domains())
    lib = {r.get("domain") for r in template_store.list_templates_raw(session) if r.get("domain")}
    return {"domains": sorted(corpus | lib)}


@router.get("")
@router.get("/")
def list_templates(
    request: Request,
    domain: Optional[str] = None,
    subdomain: Optional[str] = None,
    status: Optional[str] = None,
    origin: Optional[str] = None,
    product_kind: Optional[str] = None,
    owner_email: Optional[str] = None,
    q: Optional[str] = None,
    session: Session = Depends(get_session),
):
    """Browse templates. Visibility = ``published ∪ (caller's own drafts)``;
    every other filter narrows within that visible set."""
    caller = _caller_email(request)
    rows = _visible_rows(session, caller)
    out: list[dict] = []
    ql = (q or "").strip().lower()
    for r in rows:
        st = r.get("status") or "draft"
        if status and st != status:
            continue
        if domain and (r.get("domain") or "").lower() != domain.lower():
            continue
        if origin and (r.get("origin") or "") != origin:
            continue
        if product_kind and (r.get("product_kind") or "") != product_kind:
            continue
        if owner_email and (r.get("owner_email") or "") != owner_email:
            continue
        if subdomain:
            hay = f"{r.get('data_product') or ''} {' '.join(r.get('tags') or [])}".lower()
            if subdomain.lower() not in hay:
                continue
        if ql:
            hay = f"{r.get('name') or ''} {r.get('description') or ''} {r.get('domain') or ''}".lower()
            if ql not in hay:
                continue
        out.append(_summary(r))
    return {"templates": out, "count": len(out)}


@router.get("/search")
def search_templates(
    request: Request,
    q: str,
    limit: int = 25,
    session: Session = Depends(get_session),
):
    """Rank visible templates against a free-text query — bge embeddings over
    ``name + description + domain`` (token-Jaccard fallback when embeddings are
    unavailable). Head-only; no per-template full read."""
    caller = _caller_email(request)
    rows = _visible_rows(session, caller)
    query = (q or "").strip()
    if not query or not rows:
        return {"templates": [], "count": 0, "mode": "none"}

    texts = [f"{r.get('name') or ''}. {r.get('description') or ''}. "
             f"{r.get('domain') or ''}" for r in rows]
    scores: Optional[list[float]] = None
    mode = "token"
    if embeddings.available():
        qv = embeddings.embed_query(query)
        dvs = embeddings.embed_documents(texts)
        if qv and len(dvs) == len(texts):
            scores = [_cosine(qv, dv) for dv in dvs]
            mode = "semantic"
    if scores is None:
        qt = set(re.findall(r"[a-z0-9]+", query.lower()))
        scores = []
        for t in texts:
            tt = set(re.findall(r"[a-z0-9]+", t.lower()))
            scores.append(len(qt & tt) / len(qt | tt) if qt and tt else 0.0)

    ranked = sorted(zip(rows, scores), key=lambda x: -x[1])
    out = [{**_summary(r), "score": round(float(sc), 4)}
           for r, sc in ranked if sc > 0][:max(1, limit)]
    return {"templates": out, "count": len(out), "mode": mode}


# ── read / export / versions ─────────────────────────────────────────────────

@router.get("/{template_id}/completeness")
def template_completeness(template_id: str, session: Session = Depends(get_session)):
    """Design-completeness ("what's missing") panel for a template."""
    return _template_completeness(_read_or_404(template_id, session))


@router.get("/{template_id}/versions")
def list_versions(template_id: str, session: Session = Depends(get_session)):
    _read_or_404(template_id, session)
    return {"versions": template_store.list_template_versions(session, template_id),
            "contract_id": template_id}


@router.get("/{template_id}/versions/{version}")
def get_version(template_id: str, version: int, session: Session = Depends(get_session)):
    spec = template_store._read_template_from_graph(template_id, session, version=version)
    if not spec:
        raise HTTPException(404, f"Version {version} not found for {template_id}")
    return {"spec": spec, "version": version, "contract_id": template_id}


@router.get("/{template_id}/diff")
def diff_versions(
    template_id: str,
    from_version: int,
    to_version: int,
    session: Session = Depends(get_session),
):
    old = template_store._read_template_from_graph(template_id, session, version=from_version)
    new = template_store._read_template_from_graph(template_id, session, version=to_version)
    if not old or not new:
        raise HTTPException(404, "One or both versions not found")
    return {"diff": template_store.template_diff(old, new),
            "from_version": from_version, "to_version": to_version}


@router.get("/{template_id}/export")
def export_template(template_id: str, session: Session = Depends(get_session)):
    """Export a template as ODCS YAML (ODCS out)."""
    spec = _read_or_404(template_id, session)
    # Drop the Library-only head props from the exported ODCS proper.
    export_spec = {k: v for k, v in spec.items() if k not in template_store._TEMPLATE_META_KEYS}
    filename = f"{spec.get('sourceSpecId') or spec_id_from_template_id(template_id)}.yaml"
    return {
        "filename": filename,
        "yaml": yaml.safe_dump(export_spec, sort_keys=False, allow_unicode=True),
        "spec": export_spec,
    }


@router.get("/{template_id}")
def get_template(template_id: str, session: Session = Depends(get_session)):
    return {"spec": _read_or_404(template_id, session), "contract_id": template_id}


# ── create / import / clone ──────────────────────────────────────────────────

def persist_new_template(
    session: Session, owner_email: str, canon: dict, *, origin: str,
    cloned_from: Optional[str] = None,
) -> dict:
    """Assign a unique id + sourceSpecId and persist a new draft. Shared by the
    router (owner from the request) and the PO MCP tools (owner from the arg)."""
    domain = (canon.get("domain") or "general").strip() or "general"
    name = (canon.get("name") or "Untitled template").strip()
    base = fm.template_id(domain, canon.get("sourceSpecId") or name)
    contract_id = _unique_id(session, base)
    canon["id"] = contract_id
    canon["sourceSpecId"] = spec_id_from_template_id(contract_id)
    template_store._save_template_to_graph(
        canon, session,
        owner_email=owner_email,
        status="draft", origin=origin, cloned_from=cloned_from,
    )
    return {"spec": _read_or_404(contract_id, session), "contract_id": contract_id}


def clone_canon_from(src: dict) -> dict:
    """Build the canonical draft spec for a clone of ``src`` (strips the source's
    id / template head props; names it '<name> (copy)')."""
    stripped = {k: v for k, v in src.items() if k not in template_store._TEMPLATE_META_KEYS}
    stripped.pop("id", None)
    stripped.pop("sourceSpecId", None)
    stripped["name"] = f"{src.get('name') or 'Template'} (copy)"
    stripped["sourceSpecId"] = f"{src.get('sourceSpecId') or 'spec'}-copy"
    return canonicalize_template(stripped)


@router.post("")
@router.post("/")
def create_template(
    body: TemplateSpecBody, request: Request, session: Session = Depends(get_session)
):
    """Author a new (blank/prose) template → a draft."""
    return persist_new_template(
        session, _caller_email(request), canonicalize_template(body.spec), origin="authored"
    )


@router.post("/import")
def import_template(
    body: TemplateImportBody, request: Request, session: Session = Depends(get_session)
):
    """Import an uploaded ODCS spec (YAML/JSON) → a draft (ODCS in)."""
    from .ingest_products import _parse_raw
    raw = _parse_raw(body.content, body.source)
    return persist_new_template(
        session, _caller_email(request), canonicalize_template(raw), origin="import"
    )


@router.post("/{template_id}/clone")
def clone_template(
    template_id: str, request: Request, session: Session = Depends(get_session)
):
    """Clone any template (incl. a seed) → a fresh editable draft with lineage."""
    src = _read_or_404(template_id, session)
    return persist_new_template(
        session, _caller_email(request), clone_canon_from(src),
        origin="clone", cloned_from=template_id,
    )


# ── edit / delete ────────────────────────────────────────────────────────────

@router.put("/{template_id}")
def update_template(
    template_id: str, body: TemplateSpecBody, session: Session = Depends(get_session)
):
    """Edit a template (rejects a read-only seed)."""
    existing = _read_or_404(template_id, session)
    if (existing.get("templateOrigin") or "") == "seed":
        raise HTTPException(403, "Seed templates are read-only — clone to edit.")
    raw = dict(body.spec)
    raw["id"] = template_id
    canon = canonicalize_template(raw)
    canon["id"] = template_id
    resolved_kind = body.change_kind or "auto"
    if resolved_kind == "auto":
        from ._change_classify import classify_diff
        resolved_kind = classify_diff(existing, canon)["kind"]
    try:
        saved = template_store._save_template_to_graph(
            canon, session,
            owner_email=existing.get("templateOwnerEmail") or "",
            status=existing.get("templateStatus") or "draft",
            origin=existing.get("templateOrigin") or "authored",
            change_kind=resolved_kind,
            revision_notes=body.revision_notes or "",
            cloned_from=existing.get("clonedFrom"),
        )
    except TemplateReadOnly as e:
        raise HTTPException(403, str(e))
    return {
        "spec": _read_or_404(template_id, session),
        "contract_id": template_id,
        "save_mode": saved.save_mode,
        "new_version": saved.save_version,
        "change_kind": resolved_kind,
    }


@router.delete("/{template_id}")
def delete_template(template_id: str, session: Session = Depends(get_session)):
    try:
        deleted = template_store.delete_template(session, template_id)
    except TemplateReadOnly as e:
        raise HTTPException(403, str(e))
    if not deleted:
        raise HTTPException(404, f"Template not found: {template_id}")
    return {"deleted": True, "contract_id": template_id}


@router.post("/{template_id}/discard-draft")
def discard_draft(template_id: str, session: Session = Depends(get_session)):
    _read_or_404(template_id, session)
    try:
        result = template_store.discard_template_draft(session, template_id)
    except DiscardNotAllowed as e:
        raise HTTPException(400, str(e))
    return {
        "contract_id": template_id,
        "discarded_version": result.discarded_version,
        "restored_version": result.restored_version,
        "restored_lifecycle": result.restored_lifecycle,
    }


# ── publish / retract (flip templateStatus; feasibility reads the graph live) ──

def _validate_publishable(spec: dict) -> None:
    """A template must project to a valid (or attribute-less) feasibility spec
    before it can be published (feasibility reads it live from the graph)."""
    raw = fm.odcs_to_feasibility_spec(spec)
    if raw.get("attributes"):
        try:
            fs.parse_spec(raw)
        except fs.FeasibilitySpecValidationError as e:
            raise HTTPException(
                422, f"Template cannot be published — invalid feasibility spec: {e}"
            )


@router.post("/{template_id}/publish")
def publish_template(template_id: str, session: Session = Depends(get_session)):
    """Publish a draft (author-gated): flip draft→published. Feasibility reads the
    published Library live from the graph, so the template appears in Feasibility
    immediately — no corpus regeneration (the baked corpus is a fresh-instance seed
    only)."""
    spec = _read_or_404(template_id, session)
    _validate_publishable(spec)
    try:
        origin = template_store.set_template_status(session, template_id, "published")
    except TemplateReadOnly as e:
        raise HTTPException(403, str(e))
    if origin is None:
        raise HTTPException(404, f"Template not found: {template_id}")
    return {"contract_id": template_id, "status": "published"}


@router.post("/{template_id}/retract")
def retract_template(template_id: str, session: Session = Depends(get_session)):
    """Retract a published template back to draft — it drops out of the live
    feasibility read immediately (no corpus regeneration)."""
    _read_or_404(template_id, session)
    try:
        origin = template_store.set_template_status(session, template_id, "draft")
    except TemplateReadOnly as e:
        raise HTTPException(403, str(e))
    if origin is None:
        raise HTTPException(404, f"Template not found: {template_id}")
    return {"contract_id": template_id, "status": "draft"}
