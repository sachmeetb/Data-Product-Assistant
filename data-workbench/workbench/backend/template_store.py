"""Blueprint Library store layer — thin wrappers over the ODCS
read/write/versioning machinery for the project-independent
``:DataContract:ProductTemplate`` namespace.

The Library reuses ``_contract_versioning`` verbatim (every MERGE/READ there is
project-agnostic) and the ODCS canonicaliser + reader from ``routers.odcs``.
Templates differ from ordinary contracts in three ways only, all handled here:

- they live on the **global AppSettings graph** (``estate.estate_graph_session``),
  never linked to a ``:Project``;
- they skip the ``:CONSUMES`` DAG guard + edge sync (templates never consume);
- after the head + substructure save they get an extra ``:ProductTemplate``
  label + the template head props (status / origin / owner / domain /
  sourceSpecId / clonedFrom), and clones get a ``:PROV_WAS_DERIVED_FROM`` edge.

``_generate_dprod`` and ``link_contract_to_project`` are **never** called on a
template — that (plus the ``template:`` id prefix + the ``:ProductTemplate``
label) is what keeps templates invisible to marketplace / lineage / feasibility
/ project queries by construction.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlmodel import Session

from . import feasibility_map as fm
from ._contract_versioning import (
    discard_draft_version,
    save_contract_head,
    save_contract_substructure,
)
from .estate import estate_graph_session
from .routers.odcs import (
    SaveResult,
    _canonicalize_v3_1,
    _odcs_diff,
    _read_odcs_from_graph,
    _to_json_str,
    LIST_VERSIONS_QUERY,
)

# Template-only head props the ODCS canonicaliser would otherwise sweep into
# ``extras``. We strip them before canonicalising (they are set as columns via
# the metadata SET, not carried in the spec proper) — except sourceSpecId +
# productKind which the canonicaliser DOES sweep and we re-surface.
_TEMPLATE_META_KEYS = (
    "isTemplate", "templateStatus", "templateOrigin", "templateOwnerEmail",
    "templateDomain", "clonedFrom",
)

VALID_STATUS = ("draft", "published")
VALID_ORIGIN = ("seed", "clone", "import", "authored")


class TemplateReadOnly(ValueError):
    """Raised when a save/delete would mutate a read-only ``seed`` template."""


# ── Cypher (global AppSettings graph) ────────────────────────────────────────

READ_TEMPLATE_ORIGIN = """\
MATCH (dc:DataContract {id: $contract_id})
RETURN dc.templateOrigin AS origin, dc.isTemplate AS is_template
"""

TEMPLATE_METADATA_SET = """\
MATCH (dc:DataContract {id: $contract_id})
SET dc:ProductTemplate,
    dc.isTemplate = true,
    dc.templateStatus = $status,
    dc.templateOrigin = $origin,
    dc.templateOwnerEmail = $owner_email,
    dc.templateDomain = $domain,
    dc.sourceSpecId = $source_spec_id
"""

TEMPLATE_SET_CLONED_FROM = """\
MATCH (dc:DataContract {id: $contract_id}) SET dc.clonedFrom = $cloned_from
"""

# Reuse the existing provenance-edge convention between the two template nodes.
TEMPLATE_PROV_EDGE = """\
MATCH (dst:DataContract {id: $contract_id})
MATCH (src:DataContract {id: $cloned_from})
MERGE (dst)-[:PROV_WAS_DERIVED_FROM]->(src)
"""

TEMPLATE_SET_STATUS = """\
MATCH (dc:DataContract:ProductTemplate {id: $contract_id})
SET dc.templateStatus = $status, dc.status = $status
RETURN dc.templateOrigin AS origin
"""

# Head-only browse projection. Filtering (visibility / domain / q) is done in
# Python so the ``q`` semantic ranking can reuse the domain_catalogs embeddings.
LIST_TEMPLATES = """\
MATCH (dc:DataContract:ProductTemplate)
RETURN dc.id AS id,
       dc.name AS name,
       coalesce(dc.templateDomain, dc.domain, '') AS domain,
       dc.description AS description,
       dc.templateStatus AS status,
       dc.templateOrigin AS origin,
       coalesce(dc.templateOwnerEmail, '') AS owner_email,
       coalesce(dc.productKind, '') AS product_kind,
       dc.clonedFrom AS cloned_from,
       coalesce(dc.currentVersion, 1) AS version,
       coalesce(dc.tags, []) AS tags,
       coalesce(dc.dataProduct, '') AS data_product,
       coalesce(dc.sourceSpecId, '') AS source_spec_id
ORDER BY coalesce(dc.templateDomain, dc.domain, ''), dc.name
"""

# Isolated cascade delete — templates have no :CONSUMES / :MATERIALISES_AS /
# :Project links, so a depth-bounded sweep of everything hanging off the head
# is safe (mirrors estate.delete_source_graph's DETACH-DELETE discipline).
DELETE_TEMPLATE = """\
MATCH (dc:DataContract:ProductTemplate {id: $contract_id})
OPTIONAL MATCH (dc)-[*1..2]->(n)
WITH dc, collect(DISTINCT n) AS owned
FOREACH (x IN owned | DETACH DELETE x)
DETACH DELETE dc
"""


# ── canonicalisation ─────────────────────────────────────────────────────────

def canonicalize_template(raw: dict[str, Any]) -> dict[str, Any]:
    """``_canonicalize_v3_1`` for a template spec, preserving the template-only
    fields (``productKind`` / ``sourceSpecId``) the canonicaliser sweeps into
    ``extras`` and dropping the derived head-prop keys a read re-emits."""
    src = {k: v for k, v in raw.items() if k not in _TEMPLATE_META_KEYS}
    product_kind = src.get("productKind")
    source_spec_id = src.get("sourceSpecId")
    canon = _canonicalize_v3_1(src)
    # _canonicalize_v3_1 sweeps these two unknown top-level keys into `extras`;
    # lift them back to top level AND drop the extras residue so the result is
    # byte-stable under repeated canonicalisation (idempotence).
    extras = canon.get("extras") or {}
    extras.pop("productKind", None)
    extras.pop("sourceSpecId", None)
    if extras:
        canon["extras"] = extras
    else:
        canon.pop("extras", None)
    if product_kind is not None:
        canon["productKind"] = product_kind
    if source_spec_id is not None:
        canon["sourceSpecId"] = source_spec_id
    return canon


def spec_id_from_template_id(contract_id: str) -> str:
    """Derive a corpus-safe ``spec_id`` from a template id's last segment."""
    seg = contract_id.split(":")[-1] if contract_id else "spec"
    return seg.replace("-", "_") or "spec"


# ── save / read ──────────────────────────────────────────────────────────────

def _save_template_to_graph(
    spec: dict[str, Any],
    session: Session,
    *,
    owner_email: str,
    status: str,
    origin: str,
    change_kind: str = "auto",
    revision_notes: str = "",
    cloned_from: Optional[str] = None,
) -> SaveResult:
    """Persist a canonical ODCS template spec to the global graph.

    Mirrors ``routers.odcs._save_odcs_to_graph`` but: global-graph session; no
    archetype (``productKind`` read straight off the spec); no ``:CONSUMES``
    validate/sync; no project link; plus the ``:ProductTemplate`` label +
    metadata SET. The seed read-only guard lives HERE (central choke point) so a
    stray caller can never mutate a seed node.
    """
    contract_id = spec.get("id")
    if not contract_id:
        raise ValueError("template spec must carry an id")
    if status not in VALID_STATUS:
        raise ValueError(f"invalid template status: {status}")
    if origin not in VALID_ORIGIN:
        raise ValueError(f"invalid template origin: {origin}")

    product_kind = (spec.get("productKind") or "consumer").strip().lower() or "consumer"
    domain = (spec.get("templateDomain") or spec.get("domain") or "").strip()
    source_spec_id = (spec.get("sourceSpecId") or "").strip() or spec_id_from_template_id(contract_id)

    contract_props = dict(
        name=spec.get("name", ""),
        version=spec.get("version", "1.0.0"),
        # Keep the ODCS `status` field in lockstep with the template gate so an
        # exported template reads coherently.
        status=status,
        apiVersion=spec.get("apiVersion", "v3.1.0"),
        kind=spec.get("kind", "DataContract"),
        domain=spec.get("domain", ""),
        dataProduct=spec.get("dataProduct", ""),
        description=spec.get("description", ""),
        purpose=spec.get("purpose", ""),
        limitations=spec.get("limitations", ""),
        productKind=product_kind,
        scoringRubric=(spec.get("scoringRubric") or "osi").strip().lower() or "osi",
        tags=[str(t) for t in (spec.get("tags") or [])],
        support=_to_json_str(spec.get("support") or {}),
        customProperties=_to_json_str(spec.get("customProperties") or {}),
        extras=_to_json_str(spec.get("extras") or {}),
    )

    with estate_graph_session(session) as ns:
        tx = ns.begin_transaction()
        try:
            row = tx.run(READ_TEMPLATE_ORIGIN, contract_id=contract_id).single()
            existing_origin = row["origin"] if row else None
            if existing_origin == "seed" and origin != "seed":
                raise TemplateReadOnly(
                    f"Template {contract_id} is a read-only seed — clone it to edit."
                )

            save_mode, save_version = save_contract_head(
                tx,
                contract_id=contract_id,
                contract_props=contract_props,
                submitted_by=owner_email or "",
                change_kind=change_kind,
                revision_notes=revision_notes,
            )
            if save_mode != "patch":
                save_contract_substructure(
                    tx,
                    contract_id=contract_id,
                    save_version=save_version,
                    spec=spec,
                    to_json_str=_to_json_str,
                )
            tx.run(
                TEMPLATE_METADATA_SET,
                contract_id=contract_id,
                status=status,
                origin=origin,
                owner_email=owner_email or "",
                domain=domain,
                source_spec_id=source_spec_id,
            )
            if cloned_from:
                tx.run(TEMPLATE_SET_CLONED_FROM, contract_id=contract_id, cloned_from=cloned_from)
                tx.run(TEMPLATE_PROV_EDGE, contract_id=contract_id, cloned_from=cloned_from)
            tx.commit()
        except Exception:
            tx.rollback()
            raise

    return SaveResult(contract_id, save_mode, save_version)


def _read_template_from_graph(
    contract_id: str, session: Session, version: Optional[int] = None
) -> Optional[dict]:
    """Read a template spec from the global graph — the ODCS reader with the
    global session injected. Zero query duplication."""
    return _read_odcs_from_graph(
        contract_id,
        project=None,
        version=version,
        ns_factory=lambda: estate_graph_session(session),
    )


def list_templates_raw(session: Session) -> list[dict]:
    """Head-only rows for every template (all statuses/origins)."""
    with estate_graph_session(session) as ns:
        return [dict(r) for r in ns.run(LIST_TEMPLATES)]


def list_template_versions(session: Session, contract_id: str) -> list[dict]:
    with estate_graph_session(session) as ns:
        return [dict(r) for r in ns.run(LIST_VERSIONS_QUERY, contract_id=contract_id)]


def set_template_status(session: Session, contract_id: str, status: str) -> Optional[str]:
    """Flip templateStatus (+ mirror ODCS status). Returns the origin, or None
    if the template doesn't exist. Rejects a seed."""
    if status not in VALID_STATUS:
        raise ValueError(f"invalid template status: {status}")
    with estate_graph_session(session) as ns:
        row = ns.run(READ_TEMPLATE_ORIGIN, contract_id=contract_id).single()
        if not row or not row["is_template"]:
            return None
        if row["origin"] == "seed":
            raise TemplateReadOnly(f"Template {contract_id} is a read-only seed.")
        res = ns.run(TEMPLATE_SET_STATUS, contract_id=contract_id, status=status).single()
        return res["origin"] if res else None


def delete_template(session: Session, contract_id: str) -> bool:
    """Cascade-delete a template. Rejects a seed. Returns True if deleted."""
    with estate_graph_session(session) as ns:
        row = ns.run(READ_TEMPLATE_ORIGIN, contract_id=contract_id).single()
        if not row or not row["is_template"]:
            return False
        if row["origin"] == "seed":
            raise TemplateReadOnly(f"Template {contract_id} is a read-only seed.")
        ns.run(DELETE_TEMPLATE, contract_id=contract_id)
        return True


def discard_template_draft(session: Session, contract_id: str):
    with estate_graph_session(session) as ns:
        return discard_draft_version(ns, contract_id)


def template_diff(old: dict, new: dict) -> dict:
    return _odcs_diff(old, new)
