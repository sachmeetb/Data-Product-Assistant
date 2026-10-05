"""Edit-context diff endpoint for the engineer-side banner.

When a PO edits a previously-deployed product, the wizard branches a new
:DataContract version (v2 draft) while v1 stays at lifecycleState='published'.
The engineer needs to know what changed between deployed and current head so
they can pick which stages to rerun (mapping, serving, DQ tests, etc.).

This endpoint computes that diff server-side and returns the same shape the
wizard's EditDiffPanel uses, so the two surfaces stay visually consistent.

Known limitation (out of scope for now): :DProdColumn and :ColumnMapping
nodes are NOT versioned alongside :DataContract, so they're shared across
versions. The diff here works off :DataContractProperty (which IS versioned
via :HAS_SCHEMA → :HAS_PROPERTY scoped to the dc), so the diff is accurate
even though the dprod surface may visually leak after the engineer reruns
odcs_to_dprod. See plan follow-ups.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..database import get_session
from ..models import Project
from ..neo4j_client import neo4j_session


router = APIRouter(tags=["edits"])


def _get_project(project_id: int, session: Session) -> Project:
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def _neo4j(project: Project):
    return neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    )


# Pull the deployed and current versions in one round-trip. "deployed" is
# the highest-version :ContractVersion sidecar whose lifecycleState is
# 'published' or 'superseded'; "current" is the cv at dc.currentVersion.
# They may be the same cv when no edit has been started post-deploy.
# Contract metadata (name/description/purpose) for each version comes from
# the cv's snapshot fields; the stable :DataContract carries the latest
# current draft's values, which match cv_current when no edit is in flight.
DEPLOYED_VS_CURRENT_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv_pub:ContractVersion)
WHERE cv_pub.lifecycleState IN ['published','superseded']
WITH dc, cv_pub ORDER BY cv_pub.version DESC
WITH dc, head(collect(cv_pub)) AS deployed
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(current:ContractVersion {version: dc.currentVersion})
RETURN
    deployed.uri                AS deployed_versioned_id,
    deployed.version            AS deployed_version,
    deployed.snapshotName       AS deployed_name,
    deployed.snapshotDescription AS deployed_description,
    deployed.snapshotPurpose    AS deployed_purpose,
    current.uri                 AS current_versioned_id,
    current.version             AS current_version,
    current.lifecycleState      AS current_state,
    current.snapshotName        AS current_name,
    current.snapshotDescription AS current_description,
    current.snapshotPurpose     AS current_purpose
"""


# Properties for a specific contract version. Walks the contract→property
# edge (which IS temporally synced) and filters by p.schemaName since the
# property carries its own schema. Same pattern as READ_PROPERTIES_TEMPLATE
# in odcs.py — we don't keep the schema→property edge in temporal sync,
# so walking through it returns stale "still active" rows.
PROPERTIES_FOR_VERSION_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})-[r:HAS_PROPERTY]->(p:DataContractProperty)
WHERE r.fromVersion <= $version AND (r.toVersion IS NULL OR r.toVersion >= $version)
RETURN
    p.schemaName    AS schema_name,
    p.name          AS name,
    p.physicalName  AS physical_name,
    p.physicalType  AS physical_type,
    p.logicalType   AS logical_type,
    p.description   AS description,
    p.primaryKey    AS primary_key
"""


# :CONSUMES edges for a specific contract version. The :CONSUMES edge carries
# fromVersion/toVersion in the stable model, so per-version filtering works
# without cloning the contract.
INPUTS_FOR_VERSION_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})-[r:CONSUMES]->(src:DProdDataProduct)
WHERE r.fromVersion <= $version AND (r.toVersion IS NULL OR r.toVersion >= $version)
RETURN
    src.uri                          AS dprod_uri,
    coalesce(src.name, '')           AS name,
    coalesce(src.productKind, '')    AS product_kind
ORDER BY src.uri
"""


def _norm(v) -> str:
    return (v or "").strip() if isinstance(v, str) else ""


def _diff_columns(deployed: list[dict], current: list[dict]) -> dict:
    """Same shape as editDiffImpact.ts ColumnDiff."""
    by_name_deployed = {c["name"]: c for c in deployed if c.get("name")}
    by_name_current = {c["name"]: c for c in current if c.get("name")}

    added = sorted(n for n in by_name_current if n not in by_name_deployed)
    removed = sorted(n for n in by_name_deployed if n not in by_name_current)

    type_changed = []
    description_changed = []
    for name, col in by_name_current.items():
        if name not in by_name_deployed:
            continue
        old = by_name_deployed[name]
        old_type = _norm(old.get("physical_type")) or _norm(old.get("logical_type"))
        new_type = _norm(col.get("physical_type")) or _norm(col.get("logical_type"))
        if old_type and new_type and old_type != new_type:
            type_changed.append({"name": name, "from": old_type, "to": new_type})
        old_desc = _norm(old.get("description"))
        new_desc = _norm(col.get("description"))
        if old_desc != new_desc:
            description_changed.append({"name": name, "from": old_desc, "to": new_desc})

    return {
        "added": added,
        "removed": removed,
        "type_changed": type_changed,
        "description_changed": description_changed,
    }


def _diff_metadata(deployed: dict, current: dict) -> dict:
    pairs = [
        ("name", "Product name"),
        ("description", "Description"),
        ("purpose", "Purpose"),
    ]
    changed = []
    for key, label in pairs:
        before = _norm(deployed.get(key))
        after = _norm(current.get(key))
        if before != after:
            changed.append({"field": label, "from": before, "to": after})
    return {"changed_fields": changed}


def _diff_inputs(deployed: list[dict], current: list[dict]) -> dict:
    """Diff :CONSUMES edges between deployed and current versions of a
    consumer-aligned contract. Same shape as the column diff so the
    frontend can render a consistent added/removed pattern.
    """
    by_uri_deployed = {i["dprod_uri"]: i for i in deployed if i.get("dprod_uri")}
    by_uri_current = {i["dprod_uri"]: i for i in current if i.get("dprod_uri")}
    added = sorted(
        ({"dprod_uri": uri, "name": by_uri_current[uri].get("name", "")} for uri in by_uri_current if uri not in by_uri_deployed),
        key=lambda x: x["dprod_uri"],
    )
    removed = sorted(
        ({"dprod_uri": uri, "name": by_uri_deployed[uri].get("name", "")} for uri in by_uri_deployed if uri not in by_uri_current),
        key=lambda x: x["dprod_uri"],
    )
    return {"added": list(added), "removed": list(removed)}


# The serving exclusive-group members. Reset/impact logic historically named
# only serving_virtual_view; serving is an exclusive group (one active member),
# and the reset applier skips stages that aren't `enabled`, so expanding to all
# members means whichever serving mode is active (materialized / lakehouse /
# cross-platform transfer) gets reset on an edit — a no-op for the inactive ones.
_SERVING_GROUP_MEMBERS = (
    "serving_virtual_view", "serving_physical_copy",
    "serving_lakehouse_export", "serving_transfer",
)


def _expand_serving_group(stages: list[str]) -> list[str]:
    """If any serving-group member is scheduled, include them all (exclusive
    group → only the active one actually resets)."""
    if any(s in stages for s in _SERVING_GROUP_MEMBERS):
        for s in _SERVING_GROUP_MEMBERS:
            if s not in stages:
                stages.append(s)
    return stages


def _suggested_rerun_stages(col_diff: dict, inputs_diff: dict | None = None) -> list[str]:
    # ANY schema change requires odcs_to_dprod to rebuild :DProdColumn
    # nodes from the new v(n+1) contract first, since the consumer wizard's
    # ensureProjectAndSaveSpec deliberately skips generate-dprod when
    # branching off a deployed product (to keep the marketplace pinned to
    # v1 until v2 is accepted). Without odcs_to_dprod rerun, the mapping
    # skill walks v1's stale :DProdColumn set and can't see the new shape.
    # Order matters — odcs_to_dprod first, then mapping, then serving.
    stages: list[str] = []
    has_schema_change = bool(
        col_diff["added"] or col_diff["removed"] or col_diff["type_changed"]
    )
    if has_schema_change:
        stages.append("odcs_to_dprod")
    if col_diff["added"]:
        for s in ("data_mapping", "metadata_enrichment", "serving_virtual_view"):
            if s not in stages:
                stages.append(s)
    if col_diff["removed"]:
        for s in ("data_mapping", "serving_virtual_view"):
            if s not in stages:
                stages.append(s)
    if col_diff["type_changed"]:
        for s in ("data_mapping", "dq_test_generation_gx"):
            if s not in stages:
                stages.append(s)
    # Consumer-aligned: changing the input set means the mapping skill must
    # re-discover candidates (different :DProdColumn population) and the
    # serving DDL must recompose its FROM clause from the new view set.
    if inputs_diff and (inputs_diff.get("added") or inputs_diff.get("removed")):
        for s in ("odcs_to_dprod", "data_mapping", "serving_virtual_view"):
            if s not in stages:
                stages.append(s)
    return _expand_serving_group(stages)


def _impact_summary(col_diff: dict, meta_diff: dict, inputs_diff: dict | None = None) -> list[str]:
    out: list[str] = []
    n_added = len(col_diff["added"])
    n_removed = len(col_diff["removed"])
    n_type = len(col_diff["type_changed"])
    n_desc = len(col_diff["description_changed"])
    n_inp_added = len(inputs_diff.get("added", [])) if inputs_diff else 0
    n_inp_removed = len(inputs_diff.get("removed", [])) if inputs_diff else 0
    if n_added:
        out.append(f"{n_added} column{'s' if n_added != 1 else ''} added — engineering must rerun Mapping and the serving DDL.")
    if n_removed:
        out.append(f"{n_removed} column{'s' if n_removed != 1 else ''} removed — engineering must regenerate the virtual view DDL.")
    if n_type:
        out.append(f"{n_type} column{'s' if n_type != 1 else ''} with type changes — DQ tests for affected columns may need to be regenerated.")
    if n_inp_added or n_inp_removed:
        bits = []
        if n_inp_added:
            bits.append(f"{n_inp_added} input{'s' if n_inp_added != 1 else ''} added")
        if n_inp_removed:
            bits.append(f"{n_inp_removed} input{'s' if n_inp_removed != 1 else ''} removed")
        out.append(", ".join(bits) + " — engineering must rerun ODCS→dprod, Mapping, and the serving DDL.")
    if not (n_added or n_removed or n_type or n_inp_added or n_inp_removed) and n_desc:
        out.append("Only descriptions changed — no engineering rerun needed.")
    if not (n_added or n_removed or n_type or n_desc or n_inp_added or n_inp_removed) and meta_diff["changed_fields"]:
        out.append("Only metadata changed — no engineering rerun needed.")
    return out


@router.get("/api/projects/{project_id}/edit-diff")
def get_edit_diff(
    project_id: int,
    session: Session = Depends(get_session),
):
    """Diff between the deployed contract version and the current head.

    Returns ``{has_edit: false}`` when the project has never been deployed
    OR when no in-flight edit exists (deployed version == current head).
    Otherwise returns the structured schema/metadata diff plus a suggested
    list of stages the engineer should rerun.
    """
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"

    try:
        with _neo4j(project) as ns:
            head = ns.run(DEPLOYED_VS_CURRENT_QUERY, contract_id=contract_id).single()
            if not head:
                return {"has_edit": False, "reason": "no_contract"}
            deployed_version: Optional[int] = head.get("deployed_version")
            current_version: Optional[int] = head.get("current_version")
            # No deployed version yet — there's nothing to diff against.
            if deployed_version is None:
                return {"has_edit": False, "reason": "never_deployed"}
            # Deployed and current point at the same cv → no in-flight edit.
            if deployed_version == current_version:
                return {"has_edit": False, "reason": "in_sync"}

            deployed_props = [dict(r) for r in ns.run(
                PROPERTIES_FOR_VERSION_QUERY, contract_id=contract_id, version=deployed_version
            )]
            current_props = [dict(r) for r in ns.run(
                PROPERTIES_FOR_VERSION_QUERY, contract_id=contract_id, version=current_version
            )]
            # :CONSUMES edges only matter for consumer-aligned contracts;
            # source-aligned and dq/dd archetypes never have them, so the
            # query just returns empty for those.
            deployed_inputs = [dict(r) for r in ns.run(
                INPUTS_FOR_VERSION_QUERY, contract_id=contract_id, version=deployed_version
            )]
            current_inputs = [dict(r) for r in ns.run(
                INPUTS_FOR_VERSION_QUERY, contract_id=contract_id, version=current_version
            )]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Edit-diff query failed: {e}")

    col_diff = _diff_columns(deployed_props, current_props)
    meta_diff = _diff_metadata(
        {"name": head.get("deployed_name"), "description": head.get("deployed_description"), "purpose": head.get("deployed_purpose")},
        {"name": head.get("current_name"), "description": head.get("current_description"), "purpose": head.get("current_purpose")},
    )
    inputs_diff = _diff_inputs(deployed_inputs, current_inputs)

    return {
        "has_edit": True,
        "deployed_version": head.get("deployed_version"),
        "current_version": head.get("current_version"),
        "current_state": head.get("current_state"),
        "schema": col_diff,
        "metadata": meta_diff,
        "inputs": inputs_diff,
        "suggested_rerun_stages": _suggested_rerun_stages(col_diff, inputs_diff),
        "impact_summary": _impact_summary(col_diff, meta_diff, inputs_diff),
    }


# ── Cross-project edit impact ─────────────────────────────────────────────
#
# Given an in-flight edit on a source-aligned product, return the per-consumer
# impact: which consumer-aligned contracts CONSUMES this product and how
# many of their :ColumnMapping rows reference columns this edit removed,
# renamed, or PII-flagged. Drives the wizard's ImpactPreviewPanel so the PO
# can see, before publishing v2, who downstream will break.

# Walk consumers of this product (inbound :CONSUMES, filtered to active at
# the consumer's currentVersion), then count the consumer's :ColumnMapping
# rows whose source :DProdColumn URI is in the list of columns the diff
# targets (removed / type-changed / now-PII).
_EDIT_IMPACT_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})-[:MATERIALISES_AS]->(dp:DProdDataProduct)
OPTIONAL MATCH (cons_dc:DataContract)-[r_c:CONSUMES]->(dp)
WHERE r_c.fromVersion <= cons_dc.currentVersion
  AND (r_c.toVersion IS NULL OR r_c.toVersion >= cons_dc.currentVersion)
WITH cons_dc, dp
WHERE cons_dc IS NOT NULL
OPTIONAL MATCH (cons_dc)-[:MATERIALISES_AS]->(cons_dp:DProdDataProduct)
OPTIONAL MATCH (cons_dc)-[r_o:HAS_OWNER]->(o:DataContractOwner)
WHERE r_o.fromVersion <= cons_dc.currentVersion
  AND (r_o.toVersion IS NULL OR r_o.toVersion >= cons_dc.currentVersion)
WITH cons_dc, cons_dp, dp,
     head(collect(o.email)) AS owner_email
// Source dprod-column URIs the diff targets — match cons-side mapping rows
// whose :MAPS_SOURCE_COLUMN points at one of these.
OPTIONAL MATCH (cons_dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
      <-[:MAPS_TO_PRODUCT_COLUMN]-(cm:ColumnMapping)
      -[:MAPS_SOURCE_COLUMN]->(src_col:DProdColumn)
WHERE cm.isCurrent = true
  AND src_col.uri IN $affected_source_col_uris
WITH cons_dc, cons_dp, owner_email,
     count(DISTINCT cm) AS affected_mappings,
     collect(DISTINCT src_col.uri) AS hit_uris
RETURN
    cons_dp.uri  AS consumer_uri,
    coalesce(cons_dp.name, cons_dc.name, '') AS consumer_name,
    coalesce(owner_email, '')               AS owner_email,
    coalesce(cons_dc.currentLifecycleState, 'draft') AS consumer_state,
    affected_mappings,
    size(hit_uris)                          AS affected_columns
ORDER BY affected_mappings DESC, consumer_name
"""


def _build_affected_source_col_uris(contract_id: str, schema_columns: list[dict]) -> list[str]:
    """Build the list of :DProdColumn URIs we want to flag as 'affected'.

    Format: ``dprod:col:{contract_id}:{schema}:{physical_name}``. The
    list captures any column the edit removed, type-changed, or now-flagged
    PII — basically anything that could break a 1:1 consumer mapping.
    """
    return [
        f"dprod:col:{contract_id}:{c['schema']}:{c['name']}"
        for c in schema_columns
        if c.get("schema") and c.get("name")
    ]


@router.get("/api/projects/{project_id}/edit-impact")
def get_edit_impact(
    project_id: int,
    session: Session = Depends(get_session),
):
    """Per-consumer impact preview for the project's in-flight edit.

    Runs the classifier against (deployed view, current view), then walks
    consumer contracts that :CONSUMES this product and counts the columns
    affected by the diff. Returns:

    {
      has_impact: bool,                # True when at least one consumer would be touched
      change_kind: 'cosmetic'|'schema'|'breaking',
      classifier_reasons: [...],
      affected_columns: [...],         # source-side column URIs flagged
      consumers: [{
        consumer_uri, consumer_name, owner_email, consumer_state,
        affected_mappings, affected_columns, severity
      }],
    }
    """
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"

    # 1) Read deployed + current spec for the classifier.
    from .odcs import _read_odcs_from_graph
    from ._change_classify import classify_diff

    try:
        with _neo4j(project) as ns:
            head = ns.run(DEPLOYED_VS_CURRENT_QUERY, contract_id=contract_id).single()
            if not head:
                return {"has_impact": False, "reason": "no_contract"}
            deployed_version: Optional[int] = head.get("deployed_version")
            current_version: Optional[int] = head.get("current_version")
            if deployed_version is None:
                return {"has_impact": False, "reason": "never_deployed"}
            if deployed_version == current_version:
                return {"has_impact": False, "reason": "in_sync"}

            deployed_spec = _read_odcs_from_graph(contract_id, project, version=deployed_version)
            current_spec = _read_odcs_from_graph(contract_id, project, version=current_version)
            classification = classify_diff(deployed_spec, current_spec)

            # 2) Build list of source-column URIs the diff is "affecting"
            # (anything that could break a downstream 1:1 mapping).
            cols = classification["diff"]["columns"]
            affected_schema_cols = (
                cols["removed"] + cols["type_changed"] + cols["pii_added"]
            )
            affected_uris = _build_affected_source_col_uris(contract_id, affected_schema_cols)

            # 3) Walk consumers; count affected mappings per consumer.
            rows = []
            if affected_uris:
                rows = [dict(r) for r in ns.run(
                    _EDIT_IMPACT_QUERY,
                    contract_id=contract_id,
                    affected_source_col_uris=affected_uris,
                )]

            # Per-consumer severity: 'breaking' when affected_mappings > 0,
            # else 'schema' (consumer's contract sees a v2 of source but
            # nothing of theirs maps to the affected columns).
            consumers = []
            for r in rows:
                consumers.append({
                    **r,
                    "severity": "breaking" if (r.get("affected_mappings") or 0) > 0 else "schema",
                })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Edit-impact query failed: {e}")

    return {
        "has_impact": True,
        "deployed_version": deployed_version,
        "current_version": current_version,
        "change_kind": classification["kind"],
        "classifier_reasons": classification["reasons"],
        "affected_columns": affected_schema_cols,
        "affected_source_col_uris": affected_uris,
        "consumers": consumers,
        "schema_diff": classification["diff"]["columns"],
        "metadata_diff": classification["diff"]["metadata"],
        "inputs_diff": classification["diff"]["inputs"],
        "quality_diff": classification["diff"]["quality"],
    }


# ── Consumer-side upstream drift signal ───────────────────────────────────
#
# For each source product the consumer :CONSUMES, compare the consumer's
# pinned `consumedVersion` against the source's live `currentVersion`. When
# they differ, the consumer is on a stale view of the source. Returns the
# classifier diff between the pinned version and the latest deployed view
# of the source, plus revision notes from any :ContractVersion sidecars in
# the gap. Frontend's upstream-changed banner consumes this.

_UPSTREAM_DRIFT_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})-[r:CONSUMES]->(src_dp:DProdDataProduct)
WHERE r.fromVersion <= dc.currentVersion
  AND (r.toVersion IS NULL OR r.toVersion >= dc.currentVersion)
OPTIONAL MATCH (src_dc:DataContract)-[:MATERIALISES_AS]->(src_dp)
RETURN
    src_dp.uri              AS source_dprod_uri,
    coalesce(src_dp.name, '') AS source_name,
    coalesce(src_dp.productKind, '') AS source_product_kind,
    src_dc.id               AS source_contract_id,
    coalesce(r.consumedVersion, 1) AS consumed_version,
    coalesce(src_dc.currentVersion, 1) AS source_current_version,
    coalesce(src_dc.currentLifecycleState, 'draft') AS source_current_state
"""


_REVISION_NOTES_SINCE_QUERY = """\
MATCH (dc:DataContract {id: $source_contract_id})-[:HAS_VERSION]->(cv:ContractVersion)
WHERE cv.version > $consumed_version AND cv.version <= $latest_version
OPTIONAL MATCH (cv)-[:HAS_PATCH]->(patch:ProvActivity)
WITH cv, collect(patch) AS patches
RETURN cv.version       AS version,
       cv.lifecycleState AS lifecycle_state,
       cv.changeKind    AS change_kind,
       cv.revisionNotes AS revision_notes,
       cv.publishedAt   AS published_at,
       cv.actor         AS actor,
       [p IN patches WHERE p IS NOT NULL | {
           occurred_at: p.occurredAt,
           change_kind: p.changeKind,
           revision_notes: p.revisionNotes,
           actor: p.actor
       }] AS patches
ORDER BY cv.version ASC
"""


@router.get("/api/projects/{project_id}/upstream-drift")
def get_upstream_drift(
    project_id: int,
    session: Session = Depends(get_session),
):
    """Drift summary for every source product this consumer CONSUMES.

    For each consumed source, returns:
      - consumed_version: what the consumer pinned at edge-create time
      - source_current_version: the source's live currentVersion
      - drifted: True when consumed_version < source_current_version
      - classifier: diff between pinned view and current source view
      - revision_notes_since: ordered :ContractVersion entries (newest at end)
        with revision_notes + cosmetic patches in the gap

    Used by the consumer's "upstream changed" banner and the engineer's
    Inputs card drift chip.
    """
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"

    from .odcs import _read_odcs_from_graph
    from ._change_classify import classify_diff

    try:
        with _neo4j(project) as ns:
            rows = [dict(r) for r in ns.run(_UPSTREAM_DRIFT_QUERY, contract_id=contract_id)]
    except Exception as e:
        raise HTTPException(500, f"Drift query failed: {e}")

    sources_out = []
    for row in rows:
        src_cid: Optional[str] = row.get("source_contract_id")
        consumed: int = int(row.get("consumed_version") or 1)
        latest: int = int(row.get("source_current_version") or 1)
        drifted = src_cid is not None and latest > consumed

        entry = {
            "source_dprod_uri": row.get("source_dprod_uri"),
            "source_name": row.get("source_name"),
            "source_product_kind": row.get("source_product_kind"),
            "source_contract_id": src_cid,
            "consumed_version": consumed,
            "source_current_version": latest,
            "source_current_state": row.get("source_current_state"),
            "drifted": drifted,
        }

        if drifted and src_cid:
            # Run the classifier against the consumed view vs the source's
            # current view. The consumer (this project) doesn't own the
            # source; we need its Project record to point _read_odcs_from_graph
            # at the right Neo4j. For v1 we assume single-graph (all projects
            # share one Neo4j) — same as ALL_PRODUCTS in marketplace.py.
            # Reuse this project's connection.
            try:
                consumed_spec = _read_odcs_from_graph(src_cid, project, version=consumed)
                latest_spec = _read_odcs_from_graph(src_cid, project, version=latest)
                classification = classify_diff(consumed_spec, latest_spec)
                entry["classifier"] = {
                    "kind": classification["kind"],
                    "reasons": classification["reasons"],
                    "columns_added": [c["name"] for c in classification["diff"]["columns"]["added"]],
                    "columns_removed": [c["name"] for c in classification["diff"]["columns"]["removed"]],
                    "columns_type_changed": [c["name"] for c in classification["diff"]["columns"]["type_changed"]],
                    "columns_pii_added": [c["name"] for c in classification["diff"]["columns"]["pii_added"]],
                }
                # Pull revision notes from the :ContractVersion sidecars
                # in the gap (newest revision notes carry the most context).
                with _neo4j(project) as ns:
                    notes = []
                    for n in ns.run(
                        _REVISION_NOTES_SINCE_QUERY,
                        source_contract_id=src_cid,
                        consumed_version=consumed,
                        latest_version=latest,
                    ):
                        d = dict(n)
                        for k in ("published_at",):
                            if d.get(k) is not None:
                                d[k] = str(d[k])
                        for p in (d.get("patches") or []):
                            if p.get("occurred_at") is not None:
                                p["occurred_at"] = str(p["occurred_at"])
                        notes.append(d)
                    entry["revision_notes_since"] = notes
            except Exception as e:
                entry["classifier_error"] = str(e)

        sources_out.append(entry)

    drifted_count = sum(1 for s in sources_out if s.get("drifted"))
    return {
        "project_id": project_id,
        "drifted_count": drifted_count,
        "sources": sources_out,
    }


# ── Consumer pushback (Phase 5) ───────────────────────────────────────────
#
# When a consumer sees upstream drift they consider breaking, they can
# push back to the source PO via a :ProductRequest of kind
# 'consumer-pushback'. The source PO sees these in MyProductsDashboard's
# incoming-pushbacks surface and decides whether to revise v2 or keep it.

class UpstreamPushbackInput(BaseModel):
    """Free-text reason + optional severity. The source PO reads this
    inline in their My Products dashboard and decides how to respond
    (revise v2, leave it, or invite a discussion)."""
    source_contract_id: str
    reason: str
    severity: Optional[str] = "schema"   # 'cosmetic' | 'schema' | 'breaking'
    submitted_by: Optional[str] = None


def _resolve_source_project(contract_id: str, session: Session) -> Optional[Project]:
    """Look up the source project by contract_id. contract_id always
    follows '{project_code}-contract' so the project_code is the prefix
    up to the suffix.
    """
    if not contract_id or not contract_id.endswith("-contract"):
        return None
    project_code = contract_id[: -len("-contract")]
    from sqlmodel import select
    return session.exec(select(Project).where(Project.project_code == project_code)).first()


@router.post("/api/projects/{project_id}/upstream-pushback")
def submit_upstream_pushback(
    project_id: int,
    body: UpstreamPushbackInput,
    session: Session = Depends(get_session),
):
    """Consumer engineer pushes back on an upstream source change.

    Creates a :ProductRequest of kind='consumer-pushback' targeting the
    source project (resolved from source_contract_id). The request rides
    on the source's incoming queue so the source PO sees the feedback
    alongside their own work; the reason text surfaces inline.
    """
    consumer = _get_project(project_id, session)
    source = _resolve_source_project(body.source_contract_id, session)
    if not source:
        raise HTTPException(404, f"Source project for contract {body.source_contract_id!r} not found")

    from ..models import ProductRequest, ProductRequestKind, ProductRequestStatus
    notes = f"From consumer '{consumer.name or consumer.project_code}' ({consumer.project_code}): {body.reason}"
    req = ProductRequest(
        project_id=source.id,
        contract_id=body.source_contract_id,
        kind=ProductRequestKind.consumer_pushback,
        status=ProductRequestStatus.submitted,
        submitted_by=body.submitted_by or f"engineer:{consumer.project_code}",
        notes=notes,
    )
    session.add(req)
    session.commit()
    return {
        "request_id": req.id,
        "source_project_id": source.id,
        "source_project_code": source.project_code,
        "status": "submitted",
    }


_INCOMING_PUSHBACKS_QUERY = None  # SQL via SQLModel; defined inline in the endpoint


@router.get("/api/my-products/incoming-pushbacks")
def list_incoming_pushbacks(
    owner_email: Optional[str] = None,
    session: Session = Depends(get_session),
):
    """List pending consumer-pushback :ProductRequest rows.

    Filters to status='submitted' by default. If owner_email is provided,
    further filters to source projects whose contract owner email matches
    (which requires walking the graph — for v1 we filter by submitted_by
    via fuzzy match instead since per-project owner lookup would require
    Neo4j roundtrips we'd rather avoid in the dashboard listing).
    """
    from sqlmodel import select
    from ..models import ProductRequest, ProductRequestKind, ProductRequestStatus

    rows = session.exec(
        select(ProductRequest, Project)
        .join(Project, Project.id == ProductRequest.project_id)
        .where(ProductRequest.kind == ProductRequestKind.consumer_pushback)
        .where(ProductRequest.status == ProductRequestStatus.submitted)
        .order_by(ProductRequest.submitted_at.desc())
    ).all()

    out = []
    for req, project in rows:
        out.append({
            "request_id": req.id,
            "source_project_id": project.id,
            "source_project_code": project.project_code,
            "source_project_name": project.name,
            "contract_id": req.contract_id,
            "submitted_by": req.submitted_by,
            "submitted_at": str(req.submitted_at) if req.submitted_at else None,
            "notes": req.notes,
        })
    return {"items": out, "count": len(out)}


class PushbackResolveInput(BaseModel):
    request_id: int
    action: str  # 'accept' | 'dismiss'
    resolution_note: Optional[str] = None


@router.post("/api/my-products/incoming-pushbacks/resolve")
def resolve_pushback(body: PushbackResolveInput, session: Session = Depends(get_session)):
    """Source PO marks a pushback as accepted (will revise) or dismissed
    (kept as-is). Updates the :ProductRequest status accordingly."""
    from ..models import ProductRequest, ProductRequestStatus

    req = session.get(ProductRequest, body.request_id)
    if not req:
        raise HTTPException(404, "Pushback not found")
    if body.action == "accept":
        req.status = ProductRequestStatus.accepted
    elif body.action == "dismiss":
        req.status = ProductRequestStatus.rejected
    else:
        raise HTTPException(400, f"Unknown action: {body.action}")
    if body.resolution_note:
        req.notes = (req.notes or "") + f"\n\n[resolution] {body.resolution_note}"
    session.add(req)
    session.commit()
    return {"request_id": req.id, "status": req.status.value}


# ── Source candidates requested by engineer ───────────────────────────────
#
# When the consumer-aligned data engineer cannot pick source tables (empty
# picker on data_mapping stage) or hits an unmappable column during review,
# they escalate a :ProductRequest of kind 'source-candidates-needed' to the
# consumer's PO. The PO sees these in MyProductsDashboard alongside
# consumer-pushbacks (same orange-card pattern) and Acknowledges by opening
# the consumer wizard in edit mode to refine the candidate-sources list.

class SourceCandidatesNeededInput(BaseModel):
    engineer: str
    notes: Optional[str] = None
    # Set when the request is escalated from a column-level surface
    # (MappingReviewPanel, UnmappedColumnsPanel). Empty when triggered from
    # the data_mapping stage's empty multiselect picker.
    gap_column_uri: Optional[str] = None
    gap_reason: Optional[str] = None


@router.post("/api/projects/{project_id}/product-requests/source-candidates-needed")
def submit_source_candidates_needed(
    project_id: int,
    body: SourceCandidatesNeededInput,
    session: Session = Depends(get_session),
):
    """Engineer asks the consumer's PO to identify candidate source-aligned
    data products. Creates a :ProductRequest that lands on the PO's
    MyProductsDashboard incoming-requests surface.

    Three trigger surfaces share this endpoint:
      • data_mapping stage's empty source_tables picker (no gap context)
      • MappingReviewPanel per-row escalation (gap_column_uri set)
      • UnmappedColumnsPanel per-row escalation (gap_column_uri set)
    """
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"

    from ..models import ProductRequest, ProductRequestKind, ProductRequestStatus
    notes = body.notes or "Engineer cannot proceed — source-aligned data product candidates needed."
    req = ProductRequest(
        project_id=project.id,
        contract_id=contract_id,
        kind=ProductRequestKind.source_candidates_needed,
        status=ProductRequestStatus.submitted,
        submitted_by=body.engineer,
        engineer_assigned=body.engineer,
        notes=notes,
        gap_column_uri=body.gap_column_uri,
        gap_reason=body.gap_reason,
    )
    session.add(req)
    session.commit()
    return {
        "request_id": req.id,
        "project_id": project.id,
        "project_code": project.project_code,
        "status": "submitted",
    }


@router.get("/api/my-products/source-candidate-requests")
def list_source_candidate_requests(
    owner_email: Optional[str] = None,
    session: Session = Depends(get_session),
):
    """List pending source-candidates-needed :ProductRequest rows.

    The PO surface filters to status='submitted'. When owner_email is
    provided, filters to projects whose Project.owner_email matches —
    `dpe-cf` projects always stamp owner_email at creation time, so this
    is reliable for the consumer-aligned products that originate these
    requests.
    """
    from sqlmodel import select
    from ..models import ProductRequest, ProductRequestKind, ProductRequestStatus

    stmt = (
        select(ProductRequest, Project)
        .join(Project, Project.id == ProductRequest.project_id)
        .where(ProductRequest.kind == ProductRequestKind.source_candidates_needed)
        .where(ProductRequest.status == ProductRequestStatus.submitted)
        .order_by(ProductRequest.submitted_at.desc())
    )
    if owner_email:
        stmt = stmt.where(Project.owner_email == owner_email)
    rows = session.exec(stmt).all()

    out = []
    for req, project in rows:
        out.append({
            "request_id": req.id,
            "project_id": project.id,
            "project_code": project.project_code,
            "project_name": project.name,
            "contract_id": req.contract_id,
            "engineer": req.submitted_by,
            "submitted_at": str(req.submitted_at) if req.submitted_at else None,
            "notes": req.notes,
            "gap_column_uri": req.gap_column_uri,
            "gap_reason": req.gap_reason,
        })
    return {"items": out, "count": len(out)}


class SourceCandidateRequestResolveInput(BaseModel):
    request_id: int
    action: str  # 'accept' | 'dismiss'
    resolution_note: Optional[str] = None


@router.post("/api/my-products/source-candidate-requests/resolve")
def resolve_source_candidate_request(
    body: SourceCandidateRequestResolveInput,
    session: Session = Depends(get_session),
):
    """PO acknowledges (will refine candidates) or dismisses (no action) an
    engineer's source-candidates-needed request. Updates :ProductRequest
    status; the wizard's saveDraft path bumps to 'complete' once the PO
    actually saves new candidates."""
    from ..models import ProductRequest, ProductRequestStatus

    req = session.get(ProductRequest, body.request_id)
    if not req:
        raise HTTPException(404, "Source-candidate request not found")
    if body.action == "accept":
        req.status = ProductRequestStatus.accepted
    elif body.action == "complete":
        # Wizard saveDraft path flips accepted → complete once new candidates
        # have been persisted into the spec.
        req.status = ProductRequestStatus.complete
    elif body.action == "dismiss":
        req.status = ProductRequestStatus.rejected
    else:
        raise HTTPException(400, f"Unknown action: {body.action}")
    if body.resolution_note:
        req.notes = (req.notes or "") + f"\n\n[resolution] {body.resolution_note}"
    session.add(req)
    session.commit()
    return {"request_id": req.id, "status": req.status.value}


# ── Phase 7: Change-aware reconciliation ─────────────────────────────────
#
# When an edit ProductRequest is accepted, run surgical graph actions for
# each detected change type and reset only the stages that genuinely need
# to re-run. Replaces the old "reset everything the classifier suggested"
# pattern with a more focused approach:
#
#   removed columns → deactivate orphaned :ColumnMapping rows (PROV-O event);
#                     reset odcs_to_dprod + serving_virtual_view ONLY
#                     (skip data_mapping — nothing new to map)
#   added columns   → reset odcs_to_dprod + data_mapping + metadata_enrichment
#                     + serving_virtual_view (skill handles incremental)
#   type changed    → reset data_mapping + dq_test_generation_gx
#   inputs changed  → reset odcs_to_dprod + data_mapping + serving_virtual_view
#
# Auto-fires on engineer's accept (product_requests.py). Manual trigger
# available via POST /api/projects/{id}/edits/apply-reconciliation.


def _change_type_to_actions(
    col_diff: dict,
    inputs_diff: dict | None,
    quality_diff: dict | None = None,
    shape_diff: bool = False,
    sensitivity_diff: dict | None = None,
) -> tuple[list[str], list[dict]]:
    """Return (stages_to_reset, surgical_actions) for a given diff.

    Phase 8.4: extended to dispatch on quality / shape / sensitivity diffs
    in addition to col_diff + inputs_diff. Each change type has its own
    surgical action + stage-reset combination. Surgical actions are
    described as dicts ``{kind: '...', ...payload}`` and interpreted by
    the reconciliation helper.
    """
    stages: list[str] = []
    actions: list[dict] = []

    has_schema_change = bool(
        col_diff.get("added") or col_diff.get("removed") or col_diff.get("type_changed")
    )
    if has_schema_change:
        stages.append("odcs_to_dprod")

    # Column removals: deactivate orphaned mappings; skip data_mapping.
    if col_diff.get("removed"):
        actions.append({
            "kind": "deactivate_orphaned_mappings",
            "columns": col_diff["removed"],
        })
        for s in ("serving_virtual_view",):
            if s not in stages:
                stages.append(s)

    # Column additions: incremental mapping pass (skill skip-semantics
    # handles the already-mapped subset).
    if col_diff.get("added"):
        for s in ("data_mapping", "metadata_enrichment", "serving_virtual_view"):
            if s not in stages:
                stages.append(s)

    if col_diff.get("type_changed"):
        for s in ("data_mapping", "dq_test_generation_gx", "serving_virtual_view"):
            if s not in stages:
                stages.append(s)

    if inputs_diff and (inputs_diff.get("added") or inputs_diff.get("removed")):
        for s in ("odcs_to_dprod", "data_mapping", "serving_virtual_view"):
            if s not in stages:
                stages.append(s)

    # Phase 8.4: quality rule diff. Severity tightening (warning → error)
    # may fail tests that pass today → reset dq_test_generation_gx AND
    # flag affected mappings for review. Severity loosening + rule add/
    # remove just reset dq_test_generation_gx.
    if quality_diff:
        # _change_classify.diff_quality_rules returns these keys
        rules_added = quality_diff.get("added") or []
        rules_removed = quality_diff.get("removed") or []
        severity_changes = quality_diff.get("severity_changed") or []
        if rules_added or rules_removed or severity_changes:
            if "dq_test_generation_gx" not in stages:
                stages.append("dq_test_generation_gx")
        # Tightening tracker — emit a surgical action so the engineer
        # sees which columns/rules need follow-up review.
        _SEV_RANK = {"info": 0, "sh:Info": 0, "warning": 1, "sh:Warning": 1,
                     "error": 2, "sh:Violation": 2, "critical": 3, "sh:Critical": 3}
        tightenings = [
            s for s in severity_changes
            if _SEV_RANK.get(s.get("to", ""), 0) > _SEV_RANK.get(s.get("from", ""), 0)
        ]
        if tightenings:
            actions.append({
                "kind": "flag_mappings_for_severity_review",
                "rules": tightenings,
            })

    # Phase 8.4: shape (dataset transform) diff — filter/dedupe/scd/etc.
    # Existing mappings stay valid (just the SELECT/WHERE/CTE structure
    # changes), so we only need to regenerate the serving DDL.
    if shape_diff:
        if "serving_virtual_view" not in stages:
            stages.append("serving_virtual_view")

    # Phase 8.4: sensitivity diff (PII flag toggled). Flag mappings for
    # masking review + regenerate serving DDL (it may add masking expressions).
    if sensitivity_diff:
        pii_added = sensitivity_diff.get("pii_added") or []
        if pii_added:
            actions.append({
                "kind": "flag_mappings_for_pii_review",
                "columns": pii_added,
            })
            if "serving_virtual_view" not in stages:
                stages.append("serving_virtual_view")

    # Expand to the whole serving exclusive-group so the ACTIVE serving mode
    # (virtual / materialized / lakehouse / transfer) is reset, not just the view.
    _expand_serving_group(stages)

    # When ANY engineering re-work is needed, also reset the terminal
    # gestures that gate the engineer→PO handoff. Without this, stages like
    # `deploy_virtual_view` and `mark_engineering_complete` stay at
    # ``status='complete'`` from the prior engineering cycle, so the
    # engineer has no way to redeploy the regenerated view or signal "I'm
    # done with the edit". Order in the list intentionally matches the
    # pipeline order so the UI's stage list flows naturally.
    if stages:
        for s in (
            # Each serving mode's Deploy stage — only the active mode's deploy is
            # enabled/present, so resetting all three touches just the live one.
            "deploy_virtual_view",
            "deploy_physical_copy",
            "deploy_lakehouse",
            "deployment_reflection",
            "mark_engineering_complete",
        ):
            if s not in stages:
                stages.append(s)

    return stages, actions


# Phase 8.4: surgical helpers for flagging mappings without deactivating
# them. Used when a rule severity tightens or an upstream column gets
# flagged PII — the mapping itself stays valid, but the engineer should
# re-review whether the current transform/source choice still satisfies
# the new constraint. Adds cm.status='pending_review' + a note string.

_FLAG_MAPPINGS_BY_TARGET_URI = """\
MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
WHERE cm.isCurrent = true
  AND pc.uri IN $target_uris
SET cm.status = 'pending_review',
    cm.reviewNote = $reason,
    cm.reviewFlaggedAt = datetime()
RETURN count(cm) AS flagged
"""

_FLAG_MAPPINGS_BY_COL_NAME = """\
MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
WHERE cm.isCurrent = true
  AND pc.name IN $col_names
SET cm.status = 'pending_review',
    cm.reviewNote = $reason,
    cm.reviewFlaggedAt = datetime()
RETURN count(cm) AS flagged
"""


def _flag_mappings_for_review(project: Project, target_col_uris: list[str], reason: str) -> int:
    if not target_col_uris:
        return 0
    with _neo4j(project) as ns:
        row = ns.run(
            _FLAG_MAPPINGS_BY_TARGET_URI,
            target_uris=target_col_uris,
            reason=reason,
        ).single()
    return (row["flagged"] if row else 0) or 0


def _flag_mappings_by_product_col_name(project: Project, col_names: list[str], reason: str) -> int:
    if not col_names:
        return 0
    with _neo4j(project) as ns:
        row = ns.run(
            _FLAG_MAPPINGS_BY_COL_NAME,
            col_names=col_names,
            reason=reason,
        ).single()
    return (row["flagged"] if row else 0) or 0


def apply_reconciliation(project: Project, session: Session) -> dict:
    """Run change-aware reconciliation for the project's current edit.

    Reads /edit-diff, dispatches surgical actions per change type, then
    bulk-resets the affected stages via /reset-by-id internally. Returns a
    summary the UI can render: which mappings got deactivated, which
    stages got reset, which actions were skipped.

    Idempotent: if no edit is in flight, returns ``{has_edit: False}``.
    """
    contract_id = f"{project.project_code}-contract"
    with _neo4j(project) as ns:
        head = ns.run(DEPLOYED_VS_CURRENT_QUERY, contract_id=contract_id).single()
        if not head:
            return {"has_edit": False, "reason": "no_contract"}
        deployed_version: Optional[int] = head.get("deployed_version")
        current_version: Optional[int] = head.get("current_version")
        if deployed_version is None:
            return {"has_edit": False, "reason": "never_deployed"}
        if deployed_version == current_version:
            return {"has_edit": False, "reason": "in_sync"}

        deployed_props = [dict(r) for r in ns.run(
            PROPERTIES_FOR_VERSION_QUERY, contract_id=contract_id, version=deployed_version
        )]
        current_props = [dict(r) for r in ns.run(
            PROPERTIES_FOR_VERSION_QUERY, contract_id=contract_id, version=current_version
        )]
        deployed_inputs = [dict(r) for r in ns.run(
            INPUTS_FOR_VERSION_QUERY, contract_id=contract_id, version=deployed_version
        )]
        current_inputs = [dict(r) for r in ns.run(
            INPUTS_FOR_VERSION_QUERY, contract_id=contract_id, version=current_version
        )]

    col_diff = _diff_columns(deployed_props, current_props)
    inputs_diff = _diff_inputs(deployed_inputs, current_inputs)

    # Phase 8.4: also feed quality / shape / sensitivity diffs through the
    # action map. classify_diff returns these alongside the column / inputs
    # diffs; we use it as the source of truth instead of re-computing.
    quality_diff: dict | None = None
    shape_diff_flag = False
    sensitivity_diff: dict | None = None
    try:
        from .odcs import _read_odcs_from_graph
        from ._change_classify import classify_diff
        deployed_spec = _read_odcs_from_graph(contract_id, project, version=deployed_version)
        current_spec = _read_odcs_from_graph(contract_id, project, version=current_version)
        classification = classify_diff(deployed_spec, current_spec)
        quality_diff = classification["diff"].get("quality")
        # Sensitivity changes are captured by classify_diff inside the
        # columns bucket (pii_added / pii_removed list of {schema, name}
        # entries — already shaped as we need).
        sensitivity_diff = {
            "pii_added": classification["diff"]["columns"].get("pii_added") or [],
            "pii_removed": classification["diff"]["columns"].get("pii_removed") or [],
        }
        # Shape diff isn't in classify_diff today; detect by comparing the
        # schema.transform sub-block between specs.
        def _shape_of(spec):
            out = []
            for s in spec.get("schema") or []:
                if isinstance(s.get("transform"), dict):
                    out.append((s.get("physicalName") or s.get("name"), s["transform"]))
            return out
        if _shape_of(deployed_spec or {}) != _shape_of(current_spec or {}):
            shape_diff_flag = True
    except Exception:
        # Reconciliation is best-effort; if the classifier read fails the
        # core col/inputs path still works as before.
        pass

    stages_to_reset, surgical_actions = _change_type_to_actions(
        col_diff,
        inputs_diff,
        quality_diff=quality_diff,
        shape_diff=shape_diff_flag,
        sensitivity_diff=sensitivity_diff,
    )

    # Map removed-column names → :DProdColumn URIs the deactivate query expects.
    # The contract_id + schema (from PROPERTIES_FOR_VERSION_QUERY's schema_name)
    # + physical_name uniquely identify each :DProdColumn URI.
    removed_uris: list[str] = []
    if col_diff.get("removed"):
        deployed_by_name = {p["name"]: p for p in deployed_props if p.get("name")}
        for name in col_diff["removed"]:
            row = deployed_by_name.get(name) or {}
            schema = row.get("schema_name", "")
            phys = row.get("physical_name") or name
            removed_uris.append(f"dprod:col:{contract_id}:{schema}:{phys}")

    # Execute surgical actions.
    from .reviews import deactivate_orphaned_product_mappings
    actions_performed: list[dict] = []
    for action in surgical_actions:
        if action["kind"] == "deactivate_orphaned_mappings":
            result = deactivate_orphaned_product_mappings(
                project,
                removed_col_uris=removed_uris,
                contract_version=current_version,
                actor=None,  # reconciliation runs server-side; no specific actor
            )
            actions_performed.append({
                "kind": "deactivate_orphaned_mappings",
                "removed_columns": action["columns"],
                "deactivated_count": result["deactivated"],
                "deactivated_mapping_uris": result["deactivated_uris"],
            })
        elif action["kind"] == "flag_mappings_for_pii_review":
            # Phase 8.4: when a column is newly flagged PII, mark any mapping
            # whose target product column is in the flagged set as
            # pending_review with a note explaining why. Engineer sees the
            # mapping reappear in the review queue + can choose to add
            # masking/hashing decorators.
            pii_cols = action.get("columns") or []
            pii_uris = [
                f"dprod:col:{contract_id}:{c['schema']}:{c['name']}"
                for c in pii_cols if c.get("schema") and c.get("name")
            ]
            flagged = _flag_mappings_for_review(
                project,
                target_col_uris=pii_uris,
                reason="upstream_pii_flag_added",
            )
            actions_performed.append({
                "kind": "flag_mappings_for_pii_review",
                "pii_columns": [c.get("name") for c in pii_cols],
                "flagged_count": flagged,
            })
        elif action["kind"] == "flag_mappings_for_severity_review":
            # Phase 8.4: when a rule's severity tightens, flag any mapping
            # whose product column the rule targets. Engineer reviews
            # whether the mapping's existing transform still satisfies the
            # tighter rule (e.g. NOT NULL upgraded from warning to error
            # might require a coalesce in the transform).
            tightenings = action.get("rules") or []
            target_col_names = [t.get("column") for t in tightenings if t.get("column")]
            # Build :DProdColumn URIs by walking the current property set
            # for matching column names. We don't know the schema per rule
            # without joining; tag mappings whose pc.name matches.
            flagged = _flag_mappings_by_product_col_name(
                project,
                col_names=target_col_names,
                reason="upstream_rule_severity_tightened",
            )
            actions_performed.append({
                "kind": "flag_mappings_for_severity_review",
                "tightened_rules": [t.get("name") for t in tightenings],
                "flagged_count": flagged,
            })

    # Reset the affected stages via the bulk-reset endpoint's internals.
    # Inline the logic instead of an HTTP self-call.
    from ..models import StageStatus, Workflow
    import json as json_mod
    stages_reset: list[dict] = []
    target_ids = set(stages_to_reset)
    if target_ids:
        candidates: list[tuple[str | None, int, str]] = []
        if project.multi_workflow:
            wfs = session.exec(select(Workflow).where(Workflow.project_id == project.id)).all()
            for wf in wfs:
                if not wf.workflow_json:
                    continue
                stages = [s for s in json_mod.loads(wf.workflow_json) if s.get("enabled", True)]
                for idx, s in enumerate(stages, start=1):
                    sid = s.get("stage_id")
                    if sid in target_ids:
                        candidates.append((wf.workflow_id, idx, sid))
        elif project.workflow_json:
            stages = [s for s in json_mod.loads(project.workflow_json) if s.get("enabled", True)]
            for idx, s in enumerate(stages, start=1):
                sid = s.get("stage_id")
                if sid in target_ids:
                    candidates.append((None, idx, sid))

        from ..models import StageRun
        for workflow_id, stage_number, stage_id in candidates:
            q = select(StageRun).where(
                StageRun.project_id == project.id, StageRun.stage_number == stage_number
            )
            if workflow_id:
                q = q.where(StageRun.workflow_id == workflow_id)
            stage_run = session.exec(q).first()
            if not stage_run or stage_run.status == StageStatus.pending:
                continue
            stage_run.status = StageStatus.pending
            stage_run.started_at = None
            stage_run.completed_at = None
            stage_run.error_message = None
            session.add(stage_run)
            stages_reset.append({
                "workflow_id": workflow_id, "stage_number": stage_number, "stage_id": stage_id,
            })
        session.commit()

    return {
        "has_edit": True,
        "deployed_version": deployed_version,
        "current_version": current_version,
        "actions_performed": actions_performed,
        "stages_reset": stages_reset,
        "stages_planned": list(stages_to_reset),
        "summary": _reconciliation_summary(actions_performed, stages_reset, stages_to_reset),
    }


def _reconciliation_summary(actions: list[dict], stages_reset: list[dict], stages_planned: list[str]) -> list[str]:
    """One-line human-readable summary lines for the UI."""
    lines: list[str] = []
    for action in actions:
        if action["kind"] == "deactivate_orphaned_mappings":
            n = action["deactivated_count"]
            cols = action["removed_columns"]
            if n > 0:
                lines.append(
                    f"Deactivated {n} mapping{'s' if n != 1 else ''} "
                    f"(target columns removed: {', '.join(cols)})"
                )
        elif action["kind"] == "flag_mappings_for_pii_review":
            n = action.get("flagged_count", 0)
            cols = action.get("pii_columns", [])
            if n > 0:
                lines.append(
                    f"Flagged {n} mapping{'s' if n != 1 else ''} for PII review "
                    f"(columns now flagged: {', '.join(cols)})"
                )
        elif action["kind"] == "flag_mappings_for_severity_review":
            n = action.get("flagged_count", 0)
            rules = action.get("tightened_rules", [])
            if n > 0:
                lines.append(
                    f"Flagged {n} mapping{'s' if n != 1 else ''} for severity-tightening review "
                    f"(rules tightened: {', '.join(r for r in rules if r)})"
                )
    reset_ids = [s["stage_id"] for s in stages_reset]
    skipped = [s for s in stages_planned if s not in reset_ids]
    if reset_ids:
        lines.append(f"Reset stages: {', '.join(reset_ids)}")
    if skipped:
        lines.append(f"Skipped (already pending): {', '.join(skipped)}")
    if not lines:
        lines.append("No reconciliation actions needed.")
    return lines


@router.post("/api/projects/{project_id}/edits/apply-reconciliation")
def apply_reconciliation_endpoint(
    project_id: int,
    session: Session = Depends(get_session),
):
    """Manual trigger for reconciliation. Auto-fires on accept too."""
    project = _get_project(project_id, session)
    return apply_reconciliation(project, session)
