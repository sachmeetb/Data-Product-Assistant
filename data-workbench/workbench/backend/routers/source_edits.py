"""Source-aligned product edit endpoints (Phase 2).

For deployed dpe-sa products, the PO can change metadata / per-column
sensitivity / column descriptions WITHOUT a full engineer round-trip.
Each write mutates the upstream graph (`:DataContract` / `:Column` /
`:ColumnDescription`) then re-runs `synthesize_odcs_from_graph` so the
change flows through Phase 1's classifier in `_save_odcs_to_graph`:

- Cosmetic patches (description tweak, etc.) land as :ProvActivity
  ContractPatch attached to the current :ContractVersion. No version bump.
- Schema / breaking changes branch a new :ContractVersion via PROV_WAS_DERIVED_FROM.

For changes that require re-running discovery (e.g. the PO wants to pull
in new tables that aren't in the catalog yet), there's a dedicated
``request-rediscovery`` endpoint that creates a :ProductRequest of kind
``source-rediscovery``; the engineer accepts it and re-runs the discovery
workflow.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from ..database import get_session
from ..models import (
    Project,
    ProductRequest,
    ProductRequestKind,
    ProductRequestStatus,
)
from ..neo4j_client import neo4j_session
from .sa_pipeline import synthesize_odcs_from_graph


router = APIRouter(prefix="/api/projects/{project_id}/source-edits", tags=["source-edits"])


def _get_project(project_id: int, session: Session) -> Project:
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if project.archetype != "dpe-sa":
        raise HTTPException(
            400,
            f"Source edits only apply to dpe-sa projects (got archetype={project.archetype}).",
        )
    return project


def _neo4j(project: Project):
    return neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    )


# ── Metadata edit (name / description / purpose) ─────────────────────────


class MetadataEditInput(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    purpose: Optional[str] = None
    submitted_by: Optional[str] = None
    revision_notes: Optional[str] = ""


# Stamp the chosen metadata onto the :DataContract directly. The next
# synthesize pass will pick these values up (sa_pipeline._build_spec
# uses project.name / project.product_idea for the spec's name /
# description fallback, but the synthesized spec is then merged with
# whatever's on the stable :DataContract via _save_odcs_to_graph).
_WRITE_METADATA = """\
MATCH (dc:DataContract {id: $contract_id})
SET dc.metadataOverrideName = $name,
    dc.metadataOverrideDescription = $description,
    dc.metadataOverridePurpose = $purpose
RETURN dc.id AS id
"""


@router.post("/metadata")
def edit_metadata(
    project_id: int,
    body: MetadataEditInput,
    session: Session = Depends(get_session),
):
    """Update product name / description / purpose on a deployed dpe-sa
    product. Re-runs synthesis after the write so the change flows
    through the classifier (typically classified as ``cosmetic`` and
    patched in place).
    """
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"
    with _neo4j(project) as ns:
        ns.run(
            _WRITE_METADATA,
            contract_id=contract_id,
            name=body.name or "",
            description=body.description or "",
            purpose=body.purpose or "",
        )

    # Also stamp the Project row's product_idea so the next synthesize
    # picks up the new description via _build_spec's fallback chain.
    if body.description:
        project.product_idea = body.description
        session.add(project)
        session.commit()
    if body.name:
        project.name = body.name
        session.add(project)
        session.commit()

    # Re-run synthesis to flow the change through the classifier. The
    # synthesized spec carries the new project.name / product_idea; the
    # classifier compares against the prior deployed view.
    try:
        synthesize_odcs_from_graph(
            project,
            submitted_by=body.submitted_by or "po-source-edit",
            change_kind="auto",
            revision_notes=body.revision_notes or "",
        )
    except Exception as e:
        raise HTTPException(500, f"Re-synthesis after metadata edit failed: {e}")
    return {"contract_id": contract_id, "status": "saved"}


# ── Sensitivity edits (per-column enum) ──────────────────────────────────


class SensitivityChange(BaseModel):
    col_uri: str
    sensitivity: str  # 'none' | 'internal' | 'confidential' | 'pii' | 'phi'


class SensitivityEditInput(BaseModel):
    changes: list[SensitivityChange]
    submitted_by: Optional[str] = None
    revision_notes: Optional[str] = ""


_ALLOWED_SENSITIVITY = {"none", "internal", "confidential", "pii", "phi"}


# Phase 8.3: capture prior sensitivity + emit sensitivity_change PROV
# activity. Compliance/audit lever: every PII flag toggle is now in the
# graph's PROV chain (was a silent property write before).
_WRITE_COLUMN_SENSITIVITY = """\
MATCH (col:Column {uri: $col_uri})
WITH col, coalesce(col.sensitivity, 'none') AS prior_sensitivity
SET col.sensitivity = $sensitivity
WITH col, prior_sensitivity
FOREACH (_ IN CASE WHEN prior_sensitivity <> $sensitivity THEN [1] ELSE [] END |
  MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + coalesce($actor, 'po-source-edit')})
    ON CREATE SET agent.agentType = 'human', agent.name = coalesce($actor, 'po-source-edit')
  CREATE (act:ProvActivity {
      uri:               'prov:activity:sensitivity-change:' + replace(col.uri, 'column:', '') + ':' + toString(timestamp()),
      activityType:      'sensitivity_change',
      priorSensitivity:  prior_sensitivity,
      newSensitivity:    $sensitivity,
      occurredAt:        datetime()
  })
  CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
  CREATE (act)-[:PROV_USED]->(col)
)
RETURN col.uri AS uri, prior_sensitivity AS prior_sensitivity
"""


@router.post("/sensitivity")
def edit_sensitivity(
    project_id: int,
    body: SensitivityEditInput,
    session: Session = Depends(get_session),
):
    """Bulk-update per-column sensitivity on the catalog :Column nodes.

    New values propagate through synthesis → :DataContractProperty →
    :DProdColumn (DPROD_CREATE_COLUMN's coalesce + PII fallback). The
    classifier flags PII-add as ``schema`` by default; if any consumer
    has a 1:1 mapping pointing at the column without a masking transform,
    the impact-preview endpoint upgrades that to ``breaking``.
    """
    project = _get_project(project_id, session)

    for c in body.changes:
        sens = (c.sensitivity or "none").strip().lower()
        if sens not in _ALLOWED_SENSITIVITY:
            raise HTTPException(400, f"Invalid sensitivity value: {c.sensitivity!r}")

    with _neo4j(project) as ns:
        for c in body.changes:
            ns.run(
                _WRITE_COLUMN_SENSITIVITY,
                col_uri=c.col_uri,
                sensitivity=(c.sensitivity or "none").strip().lower(),
                actor=body.submitted_by,
            )

    try:
        synthesize_odcs_from_graph(
            project,
            submitted_by=body.submitted_by or "po-source-edit",
            change_kind="auto",
            revision_notes=body.revision_notes or "",
        )
    except Exception as e:
        raise HTTPException(500, f"Re-synthesis after sensitivity edit failed: {e}")
    return {"updated": len(body.changes), "status": "saved"}


# ── Column description edits ─────────────────────────────────────────────


class DescriptionChange(BaseModel):
    col_uri: str
    description: str


class DescriptionEditInput(BaseModel):
    changes: list[DescriptionChange]
    submitted_by: Optional[str] = None
    revision_notes: Optional[str] = ""


# Update the current approved :ColumnDescription text for each column.
# Defensive: only touch isCurrent=true approved descriptions; pending
# review descriptions are owned by the engineer-side review queue, not
# the PO source-edit panel.
_WRITE_COLUMN_DESCRIPTION = """\
MATCH (col:Column {uri: $col_uri})-[:HAS_DESCRIPTION]->(cd:ColumnDescription)
WHERE cd.isCurrent = true AND cd.status = 'approved'
SET cd.text = $description,
    cd.updatedAt = datetime()
RETURN col.uri AS uri
"""


@router.post("/column-descriptions")
def edit_column_descriptions(
    project_id: int,
    body: DescriptionEditInput,
    session: Session = Depends(get_session),
):
    """Update per-column descriptions. The next synthesis picks up the
    new text from :ColumnDescription via sa_pipeline._READ_PROJECT_COLUMNS.
    Almost always classified as ``cosmetic`` and patched in place.
    """
    project = _get_project(project_id, session)
    with _neo4j(project) as ns:
        updated = 0
        for c in body.changes:
            row = ns.run(
                _WRITE_COLUMN_DESCRIPTION,
                col_uri=c.col_uri,
                description=c.description or "",
            ).single()
            if row:
                updated += 1
    try:
        synthesize_odcs_from_graph(
            project,
            submitted_by=body.submitted_by or "po-source-edit",
            change_kind="auto",
            revision_notes=body.revision_notes or "",
        )
    except Exception as e:
        raise HTTPException(500, f"Re-synthesis after description edit failed: {e}")
    return {"updated": updated, "status": "saved"}


# ── Request re-discovery (engineer round-trip) ───────────────────────────


class RediscoveryRequestInput(BaseModel):
    reason: str
    submitted_by: Optional[str] = None


@router.post("/request-rediscovery")
def request_rediscovery(
    project_id: int,
    body: RediscoveryRequestInput,
    session: Session = Depends(get_session),
):
    """Create a :ProductRequest of kind='source-rediscovery' so the
    engineer's Incoming queue picks it up. Use this when the PO needs
    column add/remove (which requires re-running the discovery skill
    against the source database) — descriptions / sensitivity / metadata
    don't need this.
    """
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"
    req = ProductRequest(
        project_id=project.id,
        contract_id=contract_id,
        kind=ProductRequestKind.source_rediscovery,
        status=ProductRequestStatus.submitted,
        submitted_by=body.submitted_by or "po-source-edit",
        notes=body.reason,
    )
    session.add(req)
    session.commit()
    return {"request_id": req.id, "status": "submitted"}


# ── Read endpoints powering the panel ────────────────────────────────────


_READ_SOURCE_COLUMNS = """\
MATCH (:Project {projectCode: $project_code})
      -[:HAS_CATALOG]->(:Catalog)
      -[:DCAT_DATASET]->(ds:Dataset)
      -[:HAS_COLUMN]->(col:Column)
OPTIONAL MATCH (col)-[:HAS_DESCRIPTION]->(cd:ColumnDescription {status: 'approved', isCurrent: true})
WITH ds, col, head(collect(cd)) AS cd
RETURN
    ds.schema    AS schema,
    ds.name      AS table_name,
    col.uri      AS col_uri,
    col.name     AS source_name,
    coalesce(col.recommendedName, col.name) AS effective_name,
    col.dataType AS data_type,
    coalesce(col.recommendedNameStatus, '') AS name_status,
    coalesce(cd.text, '') AS description,
    coalesce(col.sensitivity, 'none') AS sensitivity,
    col.ordinal AS ordinal
ORDER BY ds.schema, ds.name, col.ordinal
"""


_READ_METADATA = """\
MATCH (dc:DataContract {id: $contract_id})
RETURN dc.name AS name,
       coalesce(dc.metadataOverrideName, dc.name, '') AS override_name,
       dc.description AS description,
       coalesce(dc.metadataOverrideDescription, dc.description, '') AS override_description,
       dc.purpose AS purpose,
       coalesce(dc.metadataOverridePurpose, dc.purpose, '') AS override_purpose,
       dc.currentLifecycleState AS lifecycle_state,
       dc.currentVersion AS current_version
"""


@router.get("")
def get_source_edit_state(
    project_id: int,
    session: Session = Depends(get_session),
):
    """Bundle the panel's initial state: metadata + per-column rows. The
    frontend's tabs read from this single endpoint to avoid N round-trips
    on open.
    """
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"
    with _neo4j(project) as ns:
        meta_row = ns.run(_READ_METADATA, contract_id=contract_id).single()
        cols = [dict(r) for r in ns.run(_READ_SOURCE_COLUMNS, project_code=project.project_code)]
    meta = dict(meta_row) if meta_row else {}
    return {
        "project": {
            "id": project.id,
            "project_code": project.project_code,
            "name": project.name,
            "archetype": project.archetype,
        },
        "metadata": meta,
        "columns": cols,
    }
