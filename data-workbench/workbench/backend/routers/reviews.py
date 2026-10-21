import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query as QParam
from fastapi.responses import Response
from pydantic import BaseModel
from sqlmodel import Session, select

from ..database import get_session
from ..graph_ops import has_project_node
from ..lookup_via import reconcile_lookup_via
from ..models import Project, StageExecution, StageRun, StageStatus, Workflow
from ..neo4j_client import neo4j_session

router = APIRouter(prefix="/api/projects/{project_id}/reviews", tags=["reviews"])


def require_review(review_type: str):
    """Dependency: enforce the PO↔Engineer boundary for a review surface.

    PO owns domain_rules + source_product_validation (+ table/relationship
    descriptions); the engineer account owns the rest. No-op when auth is
    disabled (local dev). Mirrors ``role_can_review`` so REST + MCP agree.
    """
    from ..auth import AuthUser, auth_enabled, current_user
    from ..authz import role_can_review

    def _dep(user: "AuthUser" = Depends(current_user)) -> "AuthUser":
        if auth_enabled() and not role_can_review(user.role, review_type):
            raise HTTPException(
                403,
                {
                    "message": f"Your account role '{user.role}' cannot submit "
                               f"'{review_type}' reviews.",
                    "review_type": review_type,
                    "your_role": user.role,
                },
            )
        return user

    return _dep

# ── Cypher Queries ──────────────────────────────────────────────────────────

_PRJ_DS = (
    "MATCH (:Project {projectCode: $project_code})"
    "-[:HAS_CATALOG]->(:Catalog)-[:DCAT_DATASET]->"
)

PENDING_DESCRIPTIONS_QUERY = """\
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)-[:HAS_DESCRIPTION]->(cd:ColumnDescription)
WHERE cd.status = 'pending_review' AND cd.isCurrent = true
RETURN
    ds.schema    AS schema,
    ds.name      AS table_name,
    col.uri      AS col_uri,
    col.name     AS col_name,
    col.dataType AS data_type,
    col.ordinal  AS ordinal,
    cd.uri       AS desc_uri,
    cd.text      AS description_text
ORDER BY ds.schema, ds.name, col.ordinal
"""
PENDING_DESCRIPTIONS_QUERY_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_COLUMN]->(col:Column)-[:HAS_DESCRIPTION]->(cd:ColumnDescription)
WHERE cd.status = 'pending_review' AND cd.isCurrent = true
RETURN
    ds.schema    AS schema,
    ds.name      AS table_name,
    col.uri      AS col_uri,
    col.name     AS col_name,
    col.dataType AS data_type,
    col.ordinal  AS ordinal,
    cd.uri       AS desc_uri,
    cd.text      AS description_text
ORDER BY ds.schema, ds.name, col.ordinal
"""

APPROVE_DESCRIPTION_QUERY = """\
MATCH (cd:ColumnDescription {uri: $desc_uri})
SET cd.status = 'approved'
WITH cd
CREATE (act:ProvActivity {
    uri:          'prov:activity:' + replace(cd.uri, 'description:', '') + ':review:' + $timestamp,
    activityType: 'review',
    outcome:      'approved',
    quality:      $quality,
    occurredAt:   $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:PROV_USED]->(cd)
RETURN cd.status AS status
"""

REJECT_DESCRIPTION_QUERY = """\
MATCH (col:Column {uri: $col_uri})
MATCH (orig:ColumnDescription {uri: $desc_uri})
SET orig.status = 'rejected', orig.isCurrent = false
WITH col, orig
CREATE (new:ColumnDescription {
    uri:       'description:' + replace(col.uri, 'column:', '') + ':' + $timestamp,
    text:      $corrected_text,
    status:    'approved',
    isCurrent: true
})
CREATE (col)-[:HAS_DESCRIPTION]->(new)
CREATE (new)-[:PROV_WAS_DERIVED_FROM]->(orig)
WITH orig, new
CREATE (act:ProvActivity {
    uri:          'prov:activity:' + replace(orig.uri, 'description:', '') + ':review:' + $timestamp,
    activityType: 'review',
    outcome:      'rejected',
    occurredAt:   $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:PROV_USED]->(orig)
CREATE (reason:ProvRejectionReason {
    uri:      'prov:reason:' + replace(orig.uri, 'description:', '') + ':' + $timestamp,
    category: $category,
    detail:   $detail
})
CREATE (act)-[:HAS_REJECTION_REASON]->(reason)
CREATE (new)-[:PROV_WAS_GENERATED_BY]->(act)
RETURN new.status AS status
"""

# Source side resolves either to (:Column ← :HAS_COLUMN ← :Dataset) or to
# (:DProdColumn ← :HAS_PRODUCT_COLUMN ← :DProdOutputDataset ← ... ← :DProdDataProduct)
# via the dual OPTIONAL MATCH below. _MAPPING_RETURN_FIELDS COALESCES across
# the two paths, presenting a single (source_schema, source_table, source_col*)
# tuple regardless of source kind — so the frontend doesn't care which one
# matched. For consumer-aligned mappings, source_schema is the source
# product's display name and source_table is the consumed dataset's
# physicalName.
_MAPPING_SOURCE_DUAL_OPTIONAL = """\
OPTIONAL MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(col:Column)
OPTIONAL MATCH (ds:Dataset)-[:HAS_COLUMN]->(col)
OPTIONAL MATCH (col)-[:HAS_DESCRIPTION]->(cd:ColumnDescription)
  WHERE cd.isCurrent = true
OPTIONAL MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(sc_dp:DProdColumn)
OPTIONAL MATCH (ods:DProdOutputDataset)-[:HAS_PRODUCT_COLUMN]->(sc_dp)
OPTIONAL MATCH (srcDp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
               -[:DPROD_OUTPUT_DATASET]->(ods)
CALL {
  WITH cm
  OPTIONAL MATCH (cm)<-[:PROV_USED]-(a:ProvActivity)
  WHERE a.activityType = 'transformation_authoring' AND a.priorAuthor = 'ai_suggestion'
  RETURN a AS orig_act
  ORDER BY a.occurredAt ASC LIMIT 1
}
"""

_MAPPING_RETURN_FIELDS = """\
    cm.uri                       AS mapping_uri,
    cm.similarityScore           AS similarity_score,
    cm.rationale                 AS rationale,
    cm.mappingType               AS mapping_type,
    cm.transformKind             AS transform_kind,
    cm.transformExpression       AS transform_expression,
    cm.transformInputs           AS transform_inputs_json,
    cm.transformParams           AS transform_params_json,
    cm.transformDecorators       AS transform_decorators_json,
    cm.transformAuthor           AS transform_author,
    cm.transformConfidence       AS transform_confidence,
    cm.transformEscalationReason AS transform_escalation_reason,
    coalesce(ds.schema, srcDp.name)            AS source_schema,
    coalesce(ds.name, ods.physicalName, ods.name) AS source_table,
    coalesce(col.uri, sc_dp.uri)               AS source_col_uri,
    coalesce(col.name, sc_dp.name)             AS source_col_name,
    coalesce(col.dataType, sc_dp.dataType)     AS source_col_type,
    coalesce(cd.text, sc_dp.description, '')   AS source_description,
    coalesce(col.sensitivity, sc_dp.sensitivity, 'none') AS source_sensitivity,
    dp.uri                       AS product_uri,
    dp.name                      AS product_name,
    pc.uri                       AS product_col_uri,
    pc.name                      AS product_col_name,
    pc.description               AS product_col_description,
    CASE WHEN orig_act IS NULL THEN null
         ELSE {
           transform_kind:             orig_act.priorKind,
           transform_expression:       orig_act.priorExpression,
           transform_inputs_json:      orig_act.priorInputs,
           transform_params_json:      orig_act.priorParams,
           transform_decorators_json:  orig_act.priorDecorators
         }
    END                          AS original_ai_suggestion
"""

# Anchor the query on the ColumnMapping itself, then OPTIONAL MATCH the
# source side. The previous shape entered via Dataset → Column → ColumnMapping
# which excluded literal mappings (transformKind='literal' has no
# :MAPS_SOURCE_COLUMN edge by design) AND consumer-aligned mappings whose
# source is a :DProdColumn rather than a :Column. Anchoring on the
# product column and dual-OPTIONAL-matching the source lets all three
# kinds (catalog, dprod, literal) flow through.
PENDING_MAPPINGS_QUERY = f"""\
MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
MATCH (dp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc)
WHERE cm.status = 'pending_review' AND cm.isCurrent = true
WITH cm, pc, dp
{_MAPPING_SOURCE_DUAL_OPTIONAL}
RETURN
{_MAPPING_RETURN_FIELDS}
ORDER BY pc.name
"""
PENDING_MAPPINGS_QUERY_S = f"""\
MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
MATCH (dp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc)
WHERE cm.status = 'pending_review' AND cm.isCurrent = true
  AND pc.uri STARTS WITH 'dprod:col:' + $project_code + '-contract:'
WITH cm, pc, dp
{_MAPPING_SOURCE_DUAL_OPTIONAL}
RETURN
{_MAPPING_RETURN_FIELDS},
    cm.status                    AS status
ORDER BY pc.name
"""

# Same shape as PENDING but no status filter — returns approved +
# pending + steward_review mappings. Used by the engineer dashboard's
# Mappings detail card when the engineer wants to edit a mapping AFTER
# the data_mapping stage has been approved (the post-approval edit
# path — see #3 mapping edit workflow). Status is exposed so the UI
# can render an "approved" / "pending" / "steward_review" chip.
ALL_CURRENT_MAPPINGS_QUERY_S = f"""\
MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
MATCH (dp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc)
WHERE cm.isCurrent = true
  AND pc.uri STARTS WITH 'dprod:col:' + $project_code + '-contract:'
WITH cm, pc, dp
{_MAPPING_SOURCE_DUAL_OPTIONAL}
RETURN
{_MAPPING_RETURN_FIELDS},
    cm.status                    AS status
ORDER BY pc.name
"""

# Single-mapping fetch by URI — used by the edit-mapping modal to
# populate the form with the current transform shape + source URI(s)
# without pulling the entire mapping list. Returns at most one row.
ONE_MAPPING_BY_URI_QUERY = f"""\
MATCH (cm:ColumnMapping {{uri: $mapping_uri, isCurrent: true}})
      -[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
MATCH (dp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc)
WITH cm, pc, dp
{_MAPPING_SOURCE_DUAL_OPTIONAL}
RETURN
{_MAPPING_RETURN_FIELDS},
    cm.status                    AS status
"""

# Steward escalations: same shape, filtered on status='steward_review'.
# Same dual-source resolution so consumer-aligned mappings can also be
# escalated through this surface.
PENDING_ESCALATIONS_QUERY = f"""\
MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
MATCH (dp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc)
WHERE cm.status = 'steward_review' AND cm.isCurrent = true
WITH cm, pc, dp
{_MAPPING_SOURCE_DUAL_OPTIONAL}
RETURN
{_MAPPING_RETURN_FIELDS}
ORDER BY pc.name
"""
PENDING_ESCALATIONS_QUERY_S = f"""\
MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
MATCH (dp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc)
WHERE cm.status = 'steward_review' AND cm.isCurrent = true
  AND pc.uri STARTS WITH 'dprod:col:' + $project_code + '-contract:'
WITH cm, pc, dp
{_MAPPING_SOURCE_DUAL_OPTIONAL}
RETURN
{_MAPPING_RETURN_FIELDS}
ORDER BY pc.name
"""

APPROVE_MAPPING_QUERY = """\
MATCH (cm:ColumnMapping {uri: $mapping_uri})
SET cm.status = 'approved'
WITH cm
CREATE (act:ProvActivity {
    uri:          'prov:activity:mapping-review:' + replace(cm.uri, 'mapping:', '') + ':' + $timestamp,
    activityType: 'mapping_review',
    outcome:      'approved',
    quality:      $quality,
    occurredAt:   $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:PROV_USED]->(cm)
RETURN cm.status AS status
"""

REJECT_MAPPING_QUERY = """\
MATCH (orig:ColumnMapping {uri: $mapping_uri})
SET orig.status = 'rejected', orig.isCurrent = false
WITH orig
CREATE (act:ProvActivity {
    uri:          'prov:activity:mapping-review:' + replace(orig.uri, 'mapping:', '') + ':' + $timestamp,
    activityType: 'mapping_review',
    outcome:      'rejected',
    occurredAt:   $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:PROV_USED]->(orig)
CREATE (reason:ProvRejectionReason {
    uri:      'prov:reason:mapping:' + replace(orig.uri, 'mapping:', '') + ':' + $timestamp,
    category: $category,
    detail:   $detail
})
CREATE (act)-[:HAS_REJECTION_REASON]->(reason)
RETURN orig.status AS status
"""

REMAP_MAPPING_QUERY = """\
MATCH (new_source:Column {uri: $remap_source_uri})
MATCH (orig:ColumnMapping {uri: $mapping_uri})-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
CREATE (new:ColumnMapping {
    uri:             'mapping:' + replace(new_source.uri, 'column:', '') + ':' + replace(pc.uri, 'dprod:column:', ''),
    status:          'approved',
    isCurrent:       true,
    similarityScore: 1.0,
    rationale:       'Human-corrected source mapping.',
    transformKind:   'direct',
    transformAuthor: 'engineer',
    mappingType:     'direct',
    createdAt:       $timestamp
})
CREATE (new)-[:MAPS_SOURCE_COLUMN]->(new_source)
CREATE (new)-[:MAPS_TO_PRODUCT_COLUMN]->(pc)
CREATE (new)-[:PROV_WAS_DERIVED_FROM]->(orig)
WITH new
CREATE (act:ProvActivity {
    uri:          'prov:activity:mapping:' + replace(new.uri, 'mapping:', '') + ':' + $timestamp,
    activityType: 'mapping_generation',
    occurredAt:   $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (new)-[:PROV_WAS_GENERATED_BY]->(act)
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
RETURN new.status AS status
"""

# Edit-in-place: engineer modifies transformation fields on a pending or steward_review mapping.
# Sets transformAuthor to 'engineer' and clears any escalation reason. The mapping returns to
# pending_review so the Reviewer can sign off on the engineer's revision.
#
# Phase 8.2: the PROV activity also captures prior transformKind/Expression/
# Inputs/Params/Decorators so the full edit chain is reconstructible via
# MATCH (cm)<-[:PROV_USED]-(act {activityType:'transformation_authoring'})
# ORDER BY act.occurredAt. Without this, today's activity says "someone edited"
# but loses the original expression — debugging downstream data changes
# requires reading the activity sidecar, not the (overwritten) mapping node.
#
# When the request also carries source_col_uris, the handler follows up with
# WIPE_MAPPING_SOURCE_EDGES + ATTACH_MAPPING_SOURCE_EDGE_{COLUMN,DPRODCOLUMN}
# to reconcile [:MAPS_SOURCE_COLUMN] edges to match the new list. Each URI's
# prefix selects the right node label (column: → :Column, dprod:col: → :DProdColumn).
EDIT_TRANSFORM_QUERY = """\
MATCH (cm:ColumnMapping {uri: $mapping_uri})
WITH cm,
     coalesce(cm.transformKind, '')             AS prior_kind,
     coalesce(cm.transformExpression, '')       AS prior_expression,
     coalesce(cm.transformInputs, '')           AS prior_inputs,
     coalesce(cm.transformParams, '')           AS prior_params,
     coalesce(cm.transformDecorators, '')       AS prior_decorators,
     coalesce(cm.transformAuthor, '')           AS prior_author
SET cm.transformKind             = $transform_kind,
    cm.transformExpression       = $transform_expression,
    cm.transformInputs           = $transform_inputs_json,
    cm.transformParams           = $transform_params_json,
    cm.transformDecorators       = $transform_decorators_json,
    cm.transformAuthor           = 'engineer',
    cm.mappingType               = CASE WHEN $transform_kind = 'direct' THEN 'direct' ELSE 'derived' END,
    cm.status                    = 'pending_review',
    cm.transformEscalationReason = null
WITH cm, prior_kind, prior_expression, prior_inputs, prior_params, prior_decorators, prior_author
CREATE (act:ProvActivity {
    uri:               'prov:activity:mapping-edit:' + replace(cm.uri, 'mapping:', '') + ':' + $timestamp,
    activityType:      'transformation_authoring',
    occurredAt:        $timestamp,
    priorKind:         prior_kind,
    priorExpression:   prior_expression,
    priorInputs:       prior_inputs,
    priorParams:       prior_params,
    priorDecorators:   prior_decorators,
    priorAuthor:       prior_author,
    newKind:           $transform_kind,
    newExpression:     $transform_expression,
    newInputs:         $transform_inputs_json,
    newParams:         $transform_params_json,
    newDecorators:     $transform_decorators_json
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (cm)-[:PROV_WAS_GENERATED_BY]->(act)
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:PROV_USED]->(cm)
RETURN cm.status AS status
"""

# replace_mapping: unified action — when the engineer changes any aspect of an
# AI-suggested (or any) mapping, they're declaring "I'm overriding the prior
# author's pick." This query mirrors EDIT_TRANSFORM_QUERY but ALSO creates a
# :ProvRejectionReason attached to the new activity so the audit trail records
# *why* the AI default was replaced. Without this node, edits silently
# overwrote transformAuthor → 'engineer' with no recorded rejection rationale.
#
# When the request also carries source_col_uris, the handler follows up with
# WIPE_MAPPING_SOURCE_EDGES + ATTACH_MAPPING_SOURCE_EDGE_* (same as
# edit_transform) to reconcile :MAPS_SOURCE_COLUMN edges.
REPLACE_TRANSFORM_QUERY = """\
MATCH (cm:ColumnMapping {uri: $mapping_uri})
WITH cm,
     coalesce(cm.transformKind, '')             AS prior_kind,
     coalesce(cm.transformExpression, '')       AS prior_expression,
     coalesce(cm.transformInputs, '')           AS prior_inputs,
     coalesce(cm.transformParams, '')           AS prior_params,
     coalesce(cm.transformDecorators, '')       AS prior_decorators,
     coalesce(cm.transformAuthor, '')           AS prior_author
SET cm.transformKind             = $transform_kind,
    cm.transformExpression       = $transform_expression,
    cm.transformInputs           = $transform_inputs_json,
    cm.transformParams           = $transform_params_json,
    cm.transformDecorators       = $transform_decorators_json,
    cm.transformAuthor           = 'engineer',
    cm.mappingType               = CASE WHEN $transform_kind = 'direct' THEN 'direct' ELSE 'derived' END,
    cm.status                    = 'pending_review',
    cm.transformEscalationReason = null
WITH cm, prior_kind, prior_expression, prior_inputs, prior_params, prior_decorators, prior_author
CREATE (act:ProvActivity {
    uri:               'prov:activity:mapping-replace:' + replace(cm.uri, 'mapping:', '') + ':' + $timestamp,
    activityType:      'transformation_authoring',
    outcome:           'replaced',
    occurredAt:        $timestamp,
    priorKind:         prior_kind,
    priorExpression:   prior_expression,
    priorInputs:       prior_inputs,
    priorParams:       prior_params,
    priorDecorators:   prior_decorators,
    priorAuthor:       prior_author,
    newKind:           $transform_kind,
    newExpression:     $transform_expression,
    newInputs:         $transform_inputs_json,
    newParams:         $transform_params_json,
    newDecorators:     $transform_decorators_json
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (cm)-[:PROV_WAS_GENERATED_BY]->(act)
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:PROV_USED]->(cm)
CREATE (reason:ProvRejectionReason {
    uri:      'prov:reason:mapping-replace:' + replace(cm.uri, 'mapping:', '') + ':' + $timestamp,
    category: $category,
    detail:   $detail
})
CREATE (act)-[:HAS_REJECTION_REASON]->(reason)
RETURN cm.status AS status
"""

# Source-edge reconciliation: when edit_transform's request carries
# source_col_uris, the handler runs WIPE to clear existing edges and ATTACH
# (one per URI) to rebuild them with the right label per URI prefix.
WIPE_MAPPING_SOURCE_EDGES = """\
MATCH (cm:ColumnMapping {uri: $mapping_uri})-[r:MAPS_SOURCE_COLUMN]->()
DELETE r
"""

ATTACH_MAPPING_SOURCE_EDGE_COLUMN = """\
MATCH (cm:ColumnMapping {uri: $mapping_uri})
MATCH (col:Column {uri: $source_uri})
CREATE (cm)-[:MAPS_SOURCE_COLUMN]->(col)
"""

ATTACH_MAPPING_SOURCE_EDGE_DPRODCOLUMN = """\
MATCH (cm:ColumnMapping {uri: $mapping_uri})
MATCH (col:DProdColumn {uri: $source_uri})
CREATE (cm)-[:MAPS_SOURCE_COLUMN]->(col)
"""


# Phase 8.1: read source URIs BEFORE wipe so the rebound PROV activity
# captures what the mapping used to point at.
READ_PRIOR_SOURCE_URIS_FOR_REBOUND = """\
MATCH (cm:ColumnMapping {uri: $mapping_uri})-[:MAPS_SOURCE_COLUMN]->(src)
RETURN collect(src.uri) AS prior_uris
"""


# Bulk-wipe all current :ColumnMapping rows scoped to a project. Used by the
# "Re-run mapping → Start over" affordance in MappingReviewPanel. We soft-
# delete (status='superseded', isCurrent=false) rather than DETACH DELETE so
# the PROV audit chain (prior :ProvActivity, :ProvRejectionReason, derivation
# links) survives the wipe. Pre-existing downstream filters key on
# status='approved' or isCurrent=true, so superseded rows simply disappear
# from marketplace / summary / DQ-test queries without code changes.
#
# Project scope: match on the target :DProdColumn URI prefix
# (`dprod:col:{project_code}-contract:...`), mirroring PENDING_MAPPINGS_QUERY_S
# at line 190 and UNMAPPED_COLUMNS_QUERY_S.
WIPE_PROJECT_MAPPINGS_QUERY = """\
MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
WHERE pc.uri STARTS WITH 'dprod:col:' + $project_code + '-contract:'
  AND cm.isCurrent = true
SET cm.status = 'superseded', cm.isCurrent = false
WITH collect(cm) AS wiped
CREATE (act:ProvActivity {
    uri:          'prov:activity:mapping-wipe:' + $project_code + ':' + $timestamp,
    activityType: 'mapping_wipe',
    outcome:      'superseded',
    occurredAt:   $timestamp,
    count:        size(wiped)
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
FOREACH (cm IN wiped | CREATE (act)-[:PROV_USED]->(cm))
RETURN size(wiped) AS wiped_count
"""


# Phase 8.1: PROV activity emitted whenever :MAPS_SOURCE_COLUMN edges are
# swapped. Captures prior + new URIs and the reason (rename, transform_edit,
# rebind_stale, auto_reconnect). Engineer/audit can reconstruct WHY a
# consumer's data shifted by walking the mapping's PROV chain.
EMIT_MAPPING_SOURCE_REBOUND = """\
MATCH (cm:ColumnMapping {uri: $mapping_uri})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + coalesce($actor, 'system:reconciliation')})
ON CREATE SET agent.agentType = CASE WHEN $actor IS NULL THEN 'system' ELSE 'human' END,
              agent.name = coalesce($actor, 'reconciliation')
CREATE (act:ProvActivity {
    uri:              'prov:activity:mapping-source-rebound:' + replace(cm.uri, 'mapping:', '') + ':' + toString(timestamp()),
    activityType:     'mapping_source_rebound',
    priorSourceUris:  $prior_uris,
    newSourceUris:    $new_uris,
    rebindReason:     coalesce($reason, 'unspecified'),
    occurredAt:       datetime()
})
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:PROV_USED]->(cm)
RETURN cm.uri AS uri
"""


def emit_mapping_source_rebound(
    ns,
    mapping_uri: str,
    prior_uris: list[str],
    new_uris: list[str],
    reason: str,
    actor: str | None = None,
) -> None:
    """Emit the mapping_source_rebound PROV activity. No-op when prior == new
    (idempotent re-saves don't pollute the audit trail)."""
    if sorted(prior_uris) == sorted(new_uris):
        return
    ns.run(
        EMIT_MAPPING_SOURCE_REBOUND,
        mapping_uri=mapping_uri,
        prior_uris=prior_uris,
        new_uris=new_uris,
        reason=reason,
        actor=actor,
    )


def _source_node_label(uri: str) -> str:
    """Pick the right Neo4j label to MATCH for a source URI.

    Mirrors `_source_label` from workbench-skills/skills/data-mapping-neo4j/scripts/
    write_mappings.py. Kept inline rather than imported across the skill /
    repo boundary so reviews.py stays self-contained.
    """
    return "DProdColumn" if (uri or "").startswith("dprod:col:") else "Column"


# Escalation: engineer flags a mapping for steward attention. Status flips to steward_review,
# transformEscalationReason captures the engineer's note. Steward responds via the
# transformation_escalations endpoint (answer_inline returns the mapping to pending_review).
ESCALATE_TO_STEWARD_QUERY = """\
MATCH (cm:ColumnMapping {uri: $mapping_uri})
SET cm.status                    = 'steward_review',
    cm.transformEscalationReason = $reason
WITH cm
CREATE (act:ProvActivity {
    uri:          'prov:activity:mapping-escalate:' + replace(cm.uri, 'mapping:', '') + ':' + $timestamp,
    activityType: 'transformation_escalation',
    occurredAt:   $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (cm)-[:PROV_WAS_GENERATED_BY]->(act)
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
RETURN cm.status AS status
"""

# Unmapped product columns: DProdColumn nodes that have NO current ColumnMapping.
# Surfaced to the Data Engineer so they can fill the gap manually when the
# data_mapping skill couldn't auto-propose anything (e.g. score below 0.60,
# or no plausible source).
UNMAPPED_COLUMNS_QUERY = """\
MATCH (dp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
WHERE NOT EXISTS {
    MATCH (:ColumnMapping {isCurrent: true})-[:MAPS_TO_PRODUCT_COLUMN]->(pc)
}
RETURN
    dp.uri          AS product_uri,
    dp.name         AS product_name,
    ods.physicalName AS dataset_name,
    pc.uri          AS column_uri,
    pc.name         AS column_name,
    pc.dataType     AS data_type,
    pc.logicalType  AS logical_type,
    pc.description  AS description,
    pc.transformHint AS transform_hint
ORDER BY dp.name, pc.name
"""

UNMAPPED_COLUMNS_QUERY_S = """\
MATCH (dp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
WHERE pc.uri STARTS WITH 'dprod:col:' + $project_code + '-contract:'
  AND NOT EXISTS {
    MATCH (:ColumnMapping {isCurrent: true})-[:MAPS_TO_PRODUCT_COLUMN]->(pc)
  }
RETURN
    dp.uri          AS product_uri,
    dp.name         AS product_name,
    ods.physicalName AS dataset_name,
    pc.uri          AS column_uri,
    pc.name         AS column_name,
    pc.dataType     AS data_type,
    pc.logicalType  AS logical_type,
    pc.description  AS description,
    pc.transformHint AS transform_hint
ORDER BY dp.name, pc.name
"""

# Engineer-authored manual mapping. Mirrors what write_mappings.py would do
# for a single mapping entry but lives inline so the engineer never has to
# leave the UI. Status starts at 'pending_review' so the Reviewer still gets
# the safety-net pass; transformAuthor='engineer' tags provenance.
CREATE_MANUAL_MAPPING_QUERY = """\
MATCH (pc:DProdColumn {uri: $product_col_uri})
WITH pc
WHERE NOT EXISTS {
    MATCH (:ColumnMapping {isCurrent: true})-[:MAPS_TO_PRODUCT_COLUMN]->(pc)
}
CREATE (cm:ColumnMapping {
    uri:                       $mapping_uri,
    status:                    'pending_review',
    isCurrent:                 true,
    similarityScore:           1.0,
    rationale:                 $rationale,
    mappingType:               $legacy_mapping_type,
    transformKind:             $transform_kind,
    transformExpression:       $transform_expression,
    transformInputs:           $transform_inputs_json,
    transformParams:           $transform_params_json,
    transformDecorators:       $transform_decorators_json,
    transformAuthor:           'engineer',
    transformConfidence:       null,
    transformEscalationReason: null,
    createdAt:                 $timestamp
})
CREATE (cm)-[:MAPS_TO_PRODUCT_COLUMN]->(pc)
WITH cm
CREATE (act:ProvActivity {
    uri:          'prov:activity:mapping-create:' + replace(cm.uri, 'mapping:', '') + ':' + $timestamp,
    activityType: 'mapping_generation',
    occurredAt:   $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (cm)-[:PROV_WAS_GENERATED_BY]->(act)
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
RETURN cm.uri AS mapping_uri
"""

# Deprecated single-label variant; kept only so any future call site that
# imports it doesn't break. New code should use the dual
# ATTACH_MAPPING_SOURCE_EDGE_{COLUMN,DPRODCOLUMN} pair from the edit-transform
# section above and dispatch on URI prefix via _source_node_label().
ATTACH_MAPPING_SOURCE_EDGE_QUERY = """\
MATCH (cm:ColumnMapping {uri: $mapping_uri})
MATCH (col:Column {uri: $source_uri})
CREATE (cm)-[:MAPS_SOURCE_COLUMN]->(col)
"""

# Steward "answer inline": writes a fresh transform expression and returns the mapping to
# pending_review for engineer/reviewer sign-off. transformAuthor flips to 'steward_catalog'
# so the Mappings summary card can attribute it correctly.
STEWARD_ANSWER_INLINE_QUERY = """\
MATCH (cm:ColumnMapping {uri: $mapping_uri})
SET cm.transformKind             = $transform_kind,
    cm.transformExpression       = $transform_expression,
    cm.transformInputs           = coalesce($transform_inputs_json, cm.transformInputs),
    cm.transformParams           = coalesce($transform_params_json, cm.transformParams),
    cm.transformDecorators       = coalesce($transform_decorators_json, cm.transformDecorators),
    cm.transformAuthor           = 'steward_catalog',
    cm.mappingType               = CASE WHEN $transform_kind = 'direct' THEN 'direct' ELSE 'derived' END,
    cm.status                    = 'pending_review',
    cm.transformEscalationReason = null
WITH cm
CREATE (act:ProvActivity {
    uri:          'prov:activity:steward-answer:' + replace(cm.uri, 'mapping:', '') + ':' + $timestamp,
    activityType: 'transformation_authoring',
    occurredAt:   $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (cm)-[:PROV_WAS_GENERATED_BY]->(act)
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
RETURN cm.status AS status
"""

# ── Domain Rules ───────────────────────────────────────────────────────────

PENDING_DOMAIN_RULES_QUERY = """\
MATCH (ds:Dataset)-[:HAS_SHAPE]->(ns:NodeShape)-[:PROPERTY]->(ps:PropertyShape)-[:ON_COLUMN]->(col:Column)
WHERE ps.ruleSource = 'domain' AND ps.status = 'pending_review'
RETURN
    ds.schema     AS schema,
    ds.name       AS table_name,
    col.uri       AS col_uri,
    col.name      AS col_name,
    col.dataType  AS data_type,
    ps.uri        AS rule_uri,
    ps.ruleType   AS rule_type,
    ps.severity   AS severity,
    ps.description AS description,
    ps.confidence AS confidence,
    'column'      AS source_type,
    col.ordinal   AS _ordinal
UNION
MATCH (ods:DProdOutputDataset)-[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)<-[:ON_DPROD_COLUMN]-(ps:PropertyShape)
WHERE ps.ruleSource = 'domain' AND ps.status = 'pending_review'
RETURN
    ''            AS schema,
    ods.physicalName AS table_name,
    pc.uri        AS col_uri,
    pc.name       AS col_name,
    pc.dataType   AS data_type,
    ps.uri        AS rule_uri,
    ps.ruleType   AS rule_type,
    ps.severity   AS severity,
    ps.description AS description,
    ps.confidence AS confidence,
    'dprod_column' AS source_type,
    0             AS _ordinal
"""

# Project-scoped variant: DProdColumn URIs embed the project code
# (``dprod:col:{project_code}-contract:...``) so we filter on prefix.
PENDING_DOMAIN_RULES_QUERY_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_SHAPE]->(ns:NodeShape)-[:PROPERTY]->(ps:PropertyShape)-[:ON_COLUMN]->(col:Column)
WHERE ps.ruleSource = 'domain' AND ps.status = 'pending_review'
RETURN
    ds.schema     AS schema,
    ds.name       AS table_name,
    col.uri       AS col_uri,
    col.name      AS col_name,
    col.dataType  AS data_type,
    ps.uri        AS rule_uri,
    ps.ruleType   AS rule_type,
    ps.severity   AS severity,
    ps.description AS description,
    ps.confidence AS confidence,
    'column'      AS source_type,
    col.ordinal   AS _ordinal
UNION
MATCH (ods:DProdOutputDataset)-[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)<-[:ON_DPROD_COLUMN]-(ps:PropertyShape)
WHERE ps.ruleSource = 'domain' AND ps.status = 'pending_review'
  AND pc.uri STARTS WITH 'dprod:col:' + $project_code + '-contract:'
RETURN
    ''            AS schema,
    ods.physicalName AS table_name,
    pc.uri        AS col_uri,
    pc.name       AS col_name,
    pc.dataType   AS data_type,
    ps.uri        AS rule_uri,
    ps.ruleType   AS rule_type,
    ps.severity   AS severity,
    ps.description AS description,
    ps.confidence AS confidence,
    'dprod_column' AS source_type,
    0             AS _ordinal
"""

# Idempotent: when re-approving an already-approved rule the WHERE filters
# the row out, so SET runs as a no-op and no duplicate ProvActivity stamps.
# Race-safe via the single Cypher transaction. RETURN ps.status emits 0 rows
# in the no-op case; the caller doesn't consume the result so that's fine.
APPROVE_DOMAIN_RULE_QUERY = """\
MATCH (ps:PropertyShape {uri: $rule_uri})
WHERE coalesce(ps.status, 'pending_review') <> 'approved'
SET ps.status = 'approved'
WITH ps
CREATE (act:ProvActivity {
    activityType: 'rule_review',
    outcome:      'approved',
    quality:      $quality,
    occurredAt:   $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (ps)-[:PROV_WAS_GENERATED_BY]->(act)
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
RETURN ps.status AS status
"""

# Same idempotency pattern — re-rejecting an already-rejected rule is a no-op.
REJECT_DOMAIN_RULE_QUERY = """\
MATCH (ps:PropertyShape {uri: $rule_uri})
WHERE coalesce(ps.status, 'pending_review') <> 'rejected'
SET ps.status = 'rejected'
WITH ps
CREATE (act:ProvActivity {
    activityType: 'rule_review',
    outcome:      'rejected',
    occurredAt:   $timestamp
})
CREATE (reason:ProvRejectionReason {
    category: $category,
    detail:   $detail
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (ps)-[:PROV_WAS_GENERATED_BY]->(act)
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:HAS_REJECTION_REASON]->(reason)
RETURN ps.status AS status
"""

# Dual-source: returns both catalog :Column rows AND :DProdColumn rows from
# CONSUMES'd source products. For dpe-cf projects whose sources are
# :DProdColumn URIs (dprod:col:...), the catalog half is typically empty;
# for dpe-sa / legacy dpe-cf the dprod half is empty. URI prefix tells the
# caller which side a row came from (column: vs dprod:col:).
#
# Catalog rows: `name` is `<schema>.<table>.<column>` for disambiguation.
# DProd rows:   `name` is `<source_product>.<dataset>.<column>` likewise.
SOURCE_COLUMNS_QUERY = """\
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)
OPTIONAL MATCH (col)-[:HAS_DESCRIPTION]->(cd:ColumnDescription {isCurrent: true})
OPTIONAL MATCH (ds)-[:HAS_TABLE_DESCRIPTION]->(td:TableDescription {isCurrent: true})
// Outgoing PO-approved relationship descriptions on the column's dataset.
// Surfaces "this table belongs to / categorises / is an audit log for X"
// context so the mapping skill, picker, and view-DDL bridge ranker can
// reason about FK semantics, not just FK structure. Only approved
// descriptions propagate downstream — pending / rejected stay invisible.
OPTIONAL MATCH (ds)-[:HAS_RELATIONSHIP_DESCRIPTION]->(rd:RelationshipDescription {isCurrent: true})
      -[:DESCRIBES_REFERENCE_TO]->(to_ds:Dataset)
WHERE rd.status = 'approved'
WITH col, ds, cd, td, collect(CASE WHEN rd IS NULL THEN null ELSE {
    nature:    coalesce(rd.relationshipNature, 'references'),
    to_schema: to_ds.schema,
    to_table:  to_ds.name,
    text:      rd.text
} END) AS rels_with_nulls
WITH col, ds, cd, td, [r IN rels_with_nulls WHERE r IS NOT NULL] AS outgoing_relationships
RETURN col.uri AS uri,
       ds.schema + '.' + ds.name + '.' + col.name AS name,
       ds.schema  AS table_schema,
       ds.name    AS table_name,
       col.name   AS column_name,
       col.dataType AS data_type,
       cd.text AS description,
       coalesce(col.sensitivity, 'none') AS sensitivity,
       coalesce(td.text, '') AS table_description,
       coalesce(td.relationshipKind, '') AS relationship_kind,
       outgoing_relationships
UNION
MATCH (srcDp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(dpc:DProdColumn)
RETURN dpc.uri AS uri,
       srcDp.name + '.' + coalesce(ods.physicalName, ods.name) + '.' + dpc.name AS name,
       srcDp.name                                  AS table_schema,
       coalesce(ods.physicalName, ods.name)        AS table_name,
       dpc.name                                    AS column_name,
       dpc.dataType AS data_type,
       coalesce(dpc.description, '') AS description,
       coalesce(dpc.sensitivity, 'none') AS sensitivity,
       coalesce(ods.description, '') AS table_description,
       coalesce(ods.relationshipKind, '') AS relationship_kind,
       [] AS outgoing_relationships
"""
SOURCE_COLUMNS_QUERY_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_COLUMN]->(col:Column)
OPTIONAL MATCH (col)-[:HAS_DESCRIPTION]->(cd:ColumnDescription {{isCurrent: true}})
OPTIONAL MATCH (ds)-[:HAS_TABLE_DESCRIPTION]->(td:TableDescription {{isCurrent: true}})
OPTIONAL MATCH (ds)-[:HAS_RELATIONSHIP_DESCRIPTION]->(rd:RelationshipDescription {{isCurrent: true}})
      -[:DESCRIBES_REFERENCE_TO]->(to_ds:Dataset)
WHERE rd.status = 'approved'
WITH col, ds, cd, td, collect(CASE WHEN rd IS NULL THEN null ELSE {{
    nature:    coalesce(rd.relationshipNature, 'references'),
    to_schema: to_ds.schema,
    to_table:  to_ds.name,
    text:      rd.text
}} END) AS rels_with_nulls
WITH col, ds, cd, td, [r IN rels_with_nulls WHERE r IS NOT NULL] AS outgoing_relationships
RETURN col.uri AS uri,
       ds.schema + '.' + ds.name + '.' + col.name AS name,
       ds.schema  AS table_schema,
       ds.name    AS table_name,
       col.name   AS column_name,
       col.dataType AS data_type,
       cd.text AS description,
       coalesce(col.sensitivity, 'none') AS sensitivity,
       coalesce(td.text, '') AS table_description,
       coalesce(td.relationshipKind, '') AS relationship_kind,
       outgoing_relationships
UNION
MATCH (dc:DataContract {{id: $project_code + '-contract'}})-[r_c:CONSUMES]->(srcDp:DProdDataProduct)
WHERE r_c.fromVersion <= dc.currentVersion
  AND (r_c.toVersion IS NULL OR r_c.toVersion >= dc.currentVersion)
MATCH (srcDp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(dpc:DProdColumn)
RETURN dpc.uri AS uri,
       srcDp.name + '.' + coalesce(ods.physicalName, ods.name) + '.' + dpc.name AS name,
       srcDp.name                                  AS table_schema,
       coalesce(ods.physicalName, ods.name)        AS table_name,
       dpc.name                                    AS column_name,
       dpc.dataType AS data_type,
       coalesce(dpc.description, '') AS description,
       coalesce(dpc.sensitivity, 'none') AS sensitivity,
       coalesce(ods.description, '') AS table_description,
       coalesce(ods.relationshipKind, '') AS relationship_kind,
       [] AS outgoing_relationships
"""

PRODUCT_COLUMNS_QUERY = """\
MATCH (dp:DProdDataProduct {uri: $product_uri})
MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
RETURN pc.uri AS uri, pc.name AS name, pc.description AS description
ORDER BY pc.ordinal
"""

# ── Request Models ──────────────────────────────────────────────────────────


class DescriptionReviewAction(BaseModel):
    action: str  # "approve" or "reject"
    desc_uri: str
    col_uri: Optional[str] = None
    corrected_text: Optional[str] = None
    category: Optional[str] = None
    detail: Optional[str] = None
    quality: Optional[int] = None  # 1=Acceptable, 2=Good, 3=Excellent (approvals only)
    reviewer: str = "workbench-user"


class DomainRuleReviewAction(BaseModel):
    action: str  # "approve" or "reject"
    rule_uri: str
    category: Optional[str] = None
    detail: Optional[str] = None
    quality: Optional[int] = None
    reviewer: str = "workbench-user"


class MappingReviewAction(BaseModel):
    # action: "approve" | "reject" | "edit_transform" | "replace_mapping" | "escalate_to_steward"
    #   replace_mapping unifies reject + edit_transform: a single Replace
    #   action that always records a :ProvRejectionReason (category required)
    #   alongside the transform/source change. The legacy reject and
    #   edit_transform actions remain for back-compat.
    action: str
    mapping_uri: str
    source_col_uri: Optional[str] = None
    remap_col_uri: Optional[str] = None  # legacy: remap target product column
    remap_source_uri: Optional[str] = None  # remap to different source column
    category: Optional[str] = None
    detail: Optional[str] = None
    quality: Optional[int] = None  # 1=Acceptable, 2=Good, 3=Excellent (approvals only)
    reviewer: str = "workbench-user"
    # edit_transform fields (engineer modifying the structured transform):
    transform_kind: Optional[str] = None
    transform_expression: Optional[str] = None
    transform_inputs: Optional[list] = None       # list of source-column URIs
    transform_params: Optional[dict] = None
    transform_decorators: Optional[dict] = None
    # When set, reconcile the mapping's :MAPS_SOURCE_COLUMN edges to exactly
    # this list (DELETE existing, CREATE one per URI). URI prefix dispatches
    # the matched label: column:* → :Column, dprod:col:* → :DProdColumn.
    # Omit (None) to preserve the existing source edges — the legacy edit-
    # transform behavior that updates fragment/params only.
    source_col_uris: Optional[list] = None
    # escalate_to_steward fields:
    escalation_reason: Optional[str] = None


class CreateManualMappingAction(BaseModel):
    """Engineer fills a gap left by the data_mapping skill.

    The mapping starts at status='pending_review' so the Reviewer still
    validates it via the regular Mappings review queue — same gate as
    AI-generated mappings.
    """
    product_col_uri: str
    source_col_uris: list[str]
    transform_kind: str
    transform_expression: Optional[str] = None
    transform_inputs: Optional[list] = None
    transform_params: Optional[dict] = None
    transform_decorators: Optional[dict] = None
    rationale: Optional[str] = None
    reviewer: str = "workbench-engineer"


class TransformationEscalationAction(BaseModel):
    """Steward action on a mapping in status='steward_review'.

    action: "answer_inline" — steward writes a fresh transform; mapping returns to pending_review.
            "bounce_to_po"  — flips contract lifecycle to revision_requested (Phase 6).
            "add_to_catalog" — also writes a catalog template (Phase 6).
    """
    action: str
    mapping_uri: str
    transform_kind: Optional[str] = None
    transform_expression: Optional[str] = None
    transform_inputs: Optional[list] = None
    transform_params: Optional[dict] = None
    transform_decorators: Optional[dict] = None
    note: Optional[str] = None
    reviewer: str = "workbench-steward"


# ── Helper ──────────────────────────────────────────────────────────────────

def _get_project(project_id: int, session: Session) -> Project:
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _get_project_for_write(project_id: int, session: Session) -> Project:
    """Like :func:`_get_project` but applies the acceptance gate — submitting a
    review is an engineer-owned mutation, so it's blocked (409) while the
    governing product request is still 'submitted' once WB_ENFORCE_ACCEPT_GATE
    is on (warn-only otherwise). Used by POST review handlers; GETs keep the
    plain loader so read-only inspection is never gated."""
    project = _get_project(project_id, session)
    from ..request_guard import guard_rest_mutation
    guard_rest_mutation(project, session)
    return project


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _dedup_mapping_rows(rows):
    """
    Collapse N source-column rows for the same mapping_uri into one item with a
    `sources` array. Preserves first-source flat fields (`source_schema`,
    `source_table`, `source_col_*`, `source_description`) for backward compat
    with the single-source review UI.
    """
    by_uri = {}
    for r in rows:
        d = dict(r)
        uri = d["mapping_uri"]
        src = {
            "uri": d.get("source_col_uri"),
            "schema": d.get("source_schema"),
            "table": d.get("source_table"),
            "name": d.get("source_col_name"),
            "dataType": d.get("source_col_type"),
            "description": d.get("source_description"),
        }
        if uri not in by_uri:
            d["sources"] = [src]
            by_uri[uri] = d
        else:
            by_uri[uri]["sources"].append(src)
    return list(by_uri.values())


# ── Endpoints ───────────────────────────────────────────────────────────────

def _summarize_items(review_type: str, items: list[dict]) -> list[dict]:
    """Project review rows to a terminal-friendly {uri, label, ai_proposal, status} shape.

    The detail returns stay unchanged (the UI relies on the full shape). Summary
    is opt-in via ?summary=true and only used by the CLI slash commands.
    """
    out: list[dict] = []
    for it in items:
        if review_type == "descriptions":
            label = ".".join(p for p in (it.get("schema"), it.get("table_name"), it.get("col_name")) if p)
            out.append({
                "uri": it.get("desc_uri"),
                "label": label,
                "ai_proposal": (it.get("description_text") or "")[:240],
                "status": "pending_review",
            })
        elif review_type in ("mappings", "transformation_escalations"):
            src_name = it.get("source_col_name")
            if src_name:
                src = ".".join(p for p in (it.get("source_schema"), it.get("source_table"), src_name) if p)
            else:
                src = "(literal)"
            tgt = ".".join(p for p in (it.get("product_name"), it.get("product_col_name")) if p)
            proposal = (it.get("transform_expression") or it.get("transform_kind") or "")
            out.append({
                "uri": it.get("mapping_uri"),
                "label": f"{tgt} <- {src}",
                "ai_proposal": str(proposal)[:240],
                "status": "steward_review" if review_type == "transformation_escalations" else "pending_review",
            })
        elif review_type == "unmapped_columns":
            label = ".".join(p for p in (it.get("product_name"), it.get("dataset_name"), it.get("column_name")) if p)
            out.append({
                "uri": it.get("column_uri"),
                "label": label,
                "ai_proposal": (it.get("description") or "(no description)")[:240],
                "status": "unmapped",
            })
        elif review_type == "domain_rules":
            label_parts = [it.get("schema"), it.get("table_name"), it.get("col_name")]
            label = ".".join(p for p in label_parts if p)
            rt = it.get("rule_type") or ""
            out.append({
                "uri": it.get("rule_uri"),
                "label": f"{label} :: {rt}" if rt else label,
                "ai_proposal": (it.get("description") or "")[:240],
                "status": "pending_review",
            })
        else:
            # Future-proofing: surface the unknown type so a missing case isn't
            # silently empty.
            out.append({
                "uri": next((v for v in it.values() if isinstance(v, str) and ":" in v), None),
                "label": "(unknown review_type)",
                "ai_proposal": "",
                "status": "pending_review",
            })
    return out


@router.get("/descriptions")
def get_pending_descriptions(
    project_id: int,
    summary: bool = QParam(False),
    session: Session = Depends(get_session),
):
    project = _get_project(project_id, session)
    scoped = has_project_node(project)
    pc = {"project_code": project.project_code} if scoped else {}
    q = PENDING_DESCRIPTIONS_QUERY_S if scoped else PENDING_DESCRIPTIONS_QUERY
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        result = ns.run(q, **pc)
        items = [dict(r) for r in result]
    if summary:
        items = _summarize_items("descriptions", items)
    return {"items": items, "count": len(items)}


@router.post("/descriptions")
def review_description(
    project_id: int,
    body: DescriptionReviewAction,
    session: Session = Depends(get_session),
    _user=Depends(require_review("descriptions")),
):
    project = _get_project_for_write(project_id, session)
    ts = _ts()

    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        if body.action == "approve":
            ns.run(APPROVE_DESCRIPTION_QUERY, desc_uri=body.desc_uri, reviewer=body.reviewer, timestamp=ts, quality=body.quality or 2)
        elif body.action == "reject":
            if not body.col_uri or not body.corrected_text:
                raise HTTPException(400, "col_uri and corrected_text required for rejection")
            ns.run(
                REJECT_DESCRIPTION_QUERY,
                desc_uri=body.desc_uri,
                col_uri=body.col_uri,
                corrected_text=body.corrected_text,
                category=body.category or "other",
                detail=body.detail or "",
                reviewer=body.reviewer,
                timestamp=ts,
            )
        else:
            raise HTTPException(400, "action must be 'approve' or 'reject'")

    # Check if any pending descriptions remain; if not, mark stage complete
    _check_review_complete(project, "descriptions", session)
    return {"status": "ok"}


APPROVE_ALL_DESCRIPTIONS = """\
MATCH (cd:ColumnDescription)
WHERE cd.status = 'pending_review' AND cd.isCurrent = true
SET cd.status = 'approved'
WITH cd
CREATE (act:ProvActivity {
    activityType: 'review',
    outcome:      'approved',
    quality:      $quality,
    occurredAt:   $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (cd)-[:PROV_WAS_GENERATED_BY]->(act)
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
RETURN count(cd) AS cnt
"""

APPROVE_ALL_DESCRIPTIONS_S = f"""\
{_PRJ_DS}(:Dataset)-[:HAS_COLUMN]->(:Column)-[:HAS_DESCRIPTION]->(cd:ColumnDescription)
WHERE cd.status = 'pending_review' AND cd.isCurrent = true
SET cd.status = 'approved'
WITH cd
CREATE (act:ProvActivity {{
    activityType: 'review',
    outcome:      'approved',
    quality:      $quality,
    occurredAt:   $timestamp
}})
MERGE (agent:ProvAgent {{uri: 'prov:agent:human:' + $reviewer}})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (cd)-[:PROV_WAS_GENERATED_BY]->(act)
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
RETURN count(cd) AS cnt
"""


class BulkApproveRequest(BaseModel):
    quality: int = 2
    reviewer: str = "workbench-user"


@router.post("/descriptions/approve-all")
def approve_all_descriptions(
    project_id: int,
    body: BulkApproveRequest,
    session: Session = Depends(get_session),
    _user=Depends(require_review("descriptions")),
):
    """Approve all pending descriptions at once (for demo use)."""
    project = _get_project_for_write(project_id, session)
    scoped = has_project_node(project)
    pc = {"project_code": project.project_code} if scoped else {}
    q = APPROVE_ALL_DESCRIPTIONS_S if scoped else APPROVE_ALL_DESCRIPTIONS
    ts = _ts()

    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        result = ns.run(q, quality=body.quality, reviewer=body.reviewer, timestamp=ts, **pc)
        count = result.single()["cnt"]

    _check_review_complete(project, "descriptions", session)
    return {"status": "ok", "approved": count}


# ── PII-protection recommendation (design A1) ───────────────────────────────
# When a sensitive source column is mapped into the product WITHOUT a protective
# transform, recommend one so the engineer doesn't ship raw PII. The structured
# :Column.sensitivity signal wins when set; we fall back to a tight PII
# name/description heuristic so the recommendation is useful before enrichment
# populates sensitivity. Non-binding — the engineer one-click applies or ignores.
_PROTECTIVE_KINDS = {"mask", "hash", "suppress"}
_PII_NAME_HINTS = (
    "email", "e_mail", "ssn", "social_security", "national_id", "nationalid",
    "tax_id", "passport", "phone", "mobile", "fax", "address", "addr", "postal",
    "zip", "dob", "date_of_birth", "birth", "first_name", "last_name",
    "full_name", "middle_name", "maiden", "gender", "ethnicity",
)
_MASK_PARAMS = {"algorithm": "keep_last", "keep_n": 4, "mask_char": "X", "keep_format": True}


def _looks_pii(name: str, description: str) -> bool:
    hay = f"{name or ''} {description or ''}".lower()
    return any(h in hay for h in _PII_NAME_HINTS)


def _catalog_sensitivity_map(project) -> dict:
    """name(lower) → sensitivity from the project's domain catalog — the
    authoritative, domain-declared PII classification (A2). Empty on any failure
    or when the project has no domain."""
    dom = (getattr(project, "domain", "") or "").strip().lower()
    if not dom:
        return {}
    try:
        from .domain_catalogs import get_catalog
        out = {}
        for c in get_catalog(dom).get("columns", []):
            s = (c.get("sensitivity") or "none").strip().lower()
            if s and s != "none":
                out[(c.get("name") or "").strip().lower()] = s
        return out
    except Exception:
        return {}


def _recommend_protection(item: dict, catalog_sens: Optional[dict] = None) -> Optional[dict]:
    kind = (item.get("transform_kind") or "").lower()
    if kind in _PROTECTIVE_KINDS:
        return None  # already protected
    name = item.get("source_col_name") or ""
    cat = ((catalog_sens or {}).get(name.strip().lower()) or "").lower()
    graph = (item.get("source_sensitivity") or "none").lower()
    # Authoritative order: domain-catalog declaration > structured graph field >
    # name/description heuristic. Catalog/graph are deterministic; heuristic is
    # the fallback before sensitivity is declared.
    if cat and cat != "none":
        basis, reason = "catalog", f"domain catalog classifies this column as {cat}"
    elif graph not in ("", "none"):
        basis, reason = "sensitivity", f"source flagged sensitivity='{graph}'"
    elif _looks_pii(name, item.get("source_description") or ""):
        basis, reason = "heuristic", "source name/description looks like PII"
    else:
        return None
    lname = name.lower()
    if any(t in lname for t in ("national_id", "ssn", "social_security", "passport", "tax_id")):
        rec_kind, params = "mask", dict(_MASK_PARAMS)            # sensitive identifier → partial mask
    elif lname.endswith("_id"):
        rec_kind, params = "hash", {"algorithm": "sha256"}       # surrogate/FK → hash (keeps safe joins)
    else:
        rec_kind, params = "mask", dict(_MASK_PARAMS)            # PII text → mask
    return {
        "kind": rec_kind,
        "transform_params": params,
        "transform_inputs": [item["source_col_uri"]] if item.get("source_col_uri") else [],
        "reason": reason,
        "basis": basis,
    }


@router.get("/mappings")
def get_pending_mappings(
    project_id: int,
    summary: bool = QParam(False),
    include_approved: bool = QParam(
        False,
        description=(
            "When true, return ALL current mappings (approved + pending + "
            "steward_review) instead of just pending. Used by the engineer "
            "dashboard's post-approval edit flow."
        ),
    ),
    session: Session = Depends(get_session),
):
    project = _get_project(project_id, session)
    scoped = has_project_node(project)
    pc = {"project_code": project.project_code} if scoped else {}
    if include_approved and scoped:
        q = ALL_CURRENT_MAPPINGS_QUERY_S
    else:
        q = PENDING_MAPPINGS_QUERY_S if scoped else PENDING_MAPPINGS_QUERY
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        result = ns.run(q, **pc)
        rows = [dict(r) for r in result]
    items = _dedup_mapping_rows(rows)
    catalog_sens = _catalog_sensitivity_map(project)
    for it in items:
        it["recommended_protection"] = _recommend_protection(it, catalog_sens)
    if summary:
        items = _summarize_items("mappings", items)
    return {"items": items, "count": len(items)}


def reconcile_unprotected(project) -> list[dict]:
    """Pre-serving reconciliation (design B): list sensitive source columns that
    map into the product WITHOUT a protective transform — across ALL current
    mappings (approved + pending), so it catches the already-approved case the
    A1 review-time chip can't. Reuses _recommend_protection. Read-only."""
    scoped = has_project_node(project)
    pc = {"project_code": project.project_code} if scoped else {}
    q = ALL_CURRENT_MAPPINGS_QUERY_S if scoped else PENDING_MAPPINGS_QUERY
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        rows = [dict(r) for r in ns.run(q, **pc)]
    catalog_sens = _catalog_sensitivity_map(project)
    unmet = []
    for it in _dedup_mapping_rows(rows):
        rec = _recommend_protection(it, catalog_sens)
        if rec:
            unmet.append({
                "product_col_name": it.get("product_col_name"),
                "source_col_name": it.get("source_col_name"),
                "mapping_uri": it.get("mapping_uri"),
                "recommended_kind": rec["kind"],
                "reason": rec["reason"],
                "basis": rec["basis"],
            })
    return unmet


@router.get("/mappings/reconciliation")
def get_mappings_reconciliation(project_id: int, session: Session = Depends(get_session)):
    """Sensitive-but-unprotected columns in the product (pre-serving advisory)."""
    project = _get_project(project_id, session)
    unmet = reconcile_unprotected(project)
    return {"unmet": unmet, "count": len(unmet)}


_UNFINALIZED_FILTERS_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})-[:HAS_SCHEMA]->(s:DataContractSchema)
      -[:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform)
WITH s, coalesce(dt.filterPredicate, '') AS pred, coalesce(dt.filterIntent, '') AS intent
WHERE pred <> '' OR intent <> ''
RETURN s.physicalName AS schema_name, s.name AS schema_label, pred AS predicate, intent AS intent
"""


def check_unfinalized_filters(project) -> list[dict]:
    """Pre-serving advisory: list datasets whose filter is still un-finalized —
    the compiled predicate reads like plain-language prose, or the PO authored
    an intent but no predicate was ever produced. Either case fails deploy, so
    surface it before the engineer attempts to serve. Read-only.

    Returns [{schema_name, schema_label, predicate, intent, reason}].
    """
    from ..sql_executor import looks_like_prose
    contract_id = f"{project.project_code}-contract"
    out: list[dict] = []
    try:
        with neo4j_session(
            project.neo4j_host, project.neo4j_port,
            project.neo4j_user, project.neo4j_password, project.neo4j_database,
        ) as ns:
            rows = [dict(r) for r in ns.run(_UNFINALIZED_FILTERS_QUERY, contract_id=contract_id)]
    except Exception:
        return []
    for r in rows:
        pred = (r.get("predicate") or "").strip()
        intent = (r.get("intent") or "").strip()
        if pred and looks_like_prose(pred):
            reason = "prose"
        elif not pred and intent:
            reason = "uninterpreted"
        else:
            continue
        out.append({
            "schema_name": r.get("schema_name"),
            "schema_label": r.get("schema_label") or r.get("schema_name"),
            "predicate": pred,
            "intent": intent,
            "reason": reason,
        })
    return out


@router.get("/mappings/filter-status")
def get_filter_status(project_id: int, session: Session = Depends(get_session)):
    """Un-finalized (prose / uninterpreted) dataset filters (pre-serving advisory)."""
    project = _get_project(project_id, session)
    unfinalized = check_unfinalized_filters(project)
    return {"unfinalized": unfinalized, "count": len(unfinalized)}


@router.get("/mappings/by-uri")
def get_one_mapping(
    project_id: int,
    mapping_uri: str = QParam(..., description="ColumnMapping URI"),
    session: Session = Depends(get_session),
):
    """Return a single mapping's full shape (transform_kind / expression /
    inputs / params / decorators / source URI / target product col +
    status). Used by the EditMappingModal to populate the Replace form
    when the engineer wants to edit a mapping outside the data_mapping
    stage's review queue."""
    project = _get_project(project_id, session)
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        rows = [dict(r) for r in ns.run(ONE_MAPPING_BY_URI_QUERY, mapping_uri=mapping_uri)]
    if not rows:
        raise HTTPException(404, f"Mapping {mapping_uri} not found")
    items = _dedup_mapping_rows(rows)
    return items[0] if items else rows[0]


@router.post("/mappings")
def review_mapping(
    project_id: int,
    body: MappingReviewAction,
    session: Session = Depends(get_session),
    _user=Depends(require_review("mappings")),
):
    project = _get_project_for_write(project_id, session)
    ts = _ts()

    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        if body.action == "approve":
            ns.run(APPROVE_MAPPING_QUERY, mapping_uri=body.mapping_uri, reviewer=body.reviewer, timestamp=ts, quality=body.quality or 2)
            # A manually-created lookup mapping reaches `approved` without ever
            # passing through edit/replace (the only other reconcile sites), so
            # its :LOOKUP_VIA lineage edge would never materialise — the
            # marketplace lineage canvas then silently omits the reference
            # table. Reconcile is idempotent; run it on every approve.
            reconcile_lookup_via(ns, project.project_code)
        elif body.action == "reject":
            ns.run(
                REJECT_MAPPING_QUERY,
                mapping_uri=body.mapping_uri,
                category=body.category or "other",
                detail=body.detail or "",
                reviewer=body.reviewer,
                timestamp=ts,
            )
            # If source remap requested, create new mapping with different source column
            if body.remap_source_uri:
                ns.run(
                    REMAP_MAPPING_QUERY,
                    remap_source_uri=body.remap_source_uri,
                    mapping_uri=body.mapping_uri,
                    reviewer=body.reviewer,
                    timestamp=ts,
                )
        elif body.action == "edit_transform":
            if not body.transform_kind:
                raise HTTPException(400, "transform_kind is required for edit_transform")
            import json as _json
            ns.run(
                EDIT_TRANSFORM_QUERY,
                mapping_uri=body.mapping_uri,
                transform_kind=body.transform_kind,
                transform_expression=body.transform_expression or "",
                transform_inputs_json=_json.dumps(body.transform_inputs or []),
                transform_params_json=_json.dumps(body.transform_params or {}),
                transform_decorators_json=_json.dumps(body.transform_decorators or {}),
                reviewer=body.reviewer,
                timestamp=ts,
            )
            # When the request also carries source_col_uris, reconcile the
            # mapping's :MAPS_SOURCE_COLUMN edges to exactly that set. The
            # CREATE statement dispatches per-URI on the right label.
            # Phase 8.1: capture prior source URIs before wipe + emit
            # mapping_source_rebound activity after, so audits can trace
            # WHEN/WHY a consumer's data shifted.
            if body.source_col_uris is not None:
                prior_row = ns.run(
                    READ_PRIOR_SOURCE_URIS_FOR_REBOUND,
                    mapping_uri=body.mapping_uri,
                ).single()
                prior_uris = list(prior_row["prior_uris"]) if prior_row else []
                new_uris = [u for u in body.source_col_uris if u]

                ns.run(WIPE_MAPPING_SOURCE_EDGES, mapping_uri=body.mapping_uri)
                for src_uri in new_uris:
                    label = _source_node_label(src_uri)
                    cypher = (
                        ATTACH_MAPPING_SOURCE_EDGE_DPRODCOLUMN
                        if label == "DProdColumn"
                        else ATTACH_MAPPING_SOURCE_EDGE_COLUMN
                    )
                    ns.run(cypher, mapping_uri=body.mapping_uri, source_uri=src_uri)
                emit_mapping_source_rebound(
                    ns,
                    mapping_uri=body.mapping_uri,
                    prior_uris=prior_uris,
                    new_uris=new_uris,
                    reason="transform_edit",
                    actor=body.reviewer,
                )
            # A transform edit may have turned a mapping into (or out of) a
            # lookup, or pointed it at a different lookup table — rebuild the
            # :LOOKUP_VIA edges so lineage tracks the real upstream source.
            reconcile_lookup_via(ns, project.project_code)
            # No review-complete check: edit_transform leaves the mapping in pending_review.
            return {"status": "ok"}
        elif body.action == "replace_mapping":
            # Unified Replace path. Engineer is overriding the AI's (or any
            # prior author's) suggestion — record a rejection reason alongside
            # the change. Two sub-cases:
            #   1. remap to a different source column (legacy reject + remap)
            #   2. in-place transform edit (replaces EDIT_TRANSFORM_QUERY semantics
            #      but with a rejection reason attached)
            if not body.category:
                raise HTTPException(400, "category (rejection reason) is required for replace_mapping")

            if body.remap_source_uri:
                # Reuse the existing reject + remap pair: it already creates a
                # :ProvRejectionReason on the rejected original and derives a
                # fresh approved mapping from the new source column.
                ns.run(
                    REJECT_MAPPING_QUERY,
                    mapping_uri=body.mapping_uri,
                    category=body.category,
                    detail=body.detail or "",
                    reviewer=body.reviewer,
                    timestamp=ts,
                )
                ns.run(
                    REMAP_MAPPING_QUERY,
                    remap_source_uri=body.remap_source_uri,
                    mapping_uri=body.mapping_uri,
                    reviewer=body.reviewer,
                    timestamp=ts,
                )
                _check_review_complete(project, "mappings", session)
                reconcile_lookup_via(ns, project.project_code)
                # Post-approval cascade — if the engineer edits a mapping
                # after the data_mapping stage has already passed and
                # downstream stages (serving / deploy / reflection / mark)
                # have run, those stages are now stale. Reset them so the
                # engineer is prompted to re-run.
                stages_reset = _reset_downstream_mapping_stages(project, session)
                return {"status": "ok", "stages_reset": stages_reset}

            # In-place replacement: transform edits + optional source-set edits.
            if not body.transform_kind:
                raise HTTPException(400, "transform_kind is required for replace_mapping")
            import json as _json
            ns.run(
                REPLACE_TRANSFORM_QUERY,
                mapping_uri=body.mapping_uri,
                transform_kind=body.transform_kind,
                transform_expression=body.transform_expression or "",
                transform_inputs_json=_json.dumps(body.transform_inputs or []),
                transform_params_json=_json.dumps(body.transform_params or {}),
                transform_decorators_json=_json.dumps(body.transform_decorators or {}),
                category=body.category,
                detail=body.detail or "",
                reviewer=body.reviewer,
                timestamp=ts,
            )
            if body.source_col_uris is not None:
                prior_row = ns.run(
                    READ_PRIOR_SOURCE_URIS_FOR_REBOUND,
                    mapping_uri=body.mapping_uri,
                ).single()
                prior_uris = list(prior_row["prior_uris"]) if prior_row else []
                new_uris = [u for u in body.source_col_uris if u]

                ns.run(WIPE_MAPPING_SOURCE_EDGES, mapping_uri=body.mapping_uri)
                for src_uri in new_uris:
                    label = _source_node_label(src_uri)
                    cypher = (
                        ATTACH_MAPPING_SOURCE_EDGE_DPRODCOLUMN
                        if label == "DProdColumn"
                        else ATTACH_MAPPING_SOURCE_EDGE_COLUMN
                    )
                    ns.run(cypher, mapping_uri=body.mapping_uri, source_uri=src_uri)
                emit_mapping_source_rebound(
                    ns,
                    mapping_uri=body.mapping_uri,
                    prior_uris=prior_uris,
                    new_uris=new_uris,
                    reason="replacement",
                    actor=body.reviewer,
                )
            reconcile_lookup_via(ns, project.project_code)
            # Replace leaves the mapping in pending_review for re-approval.
            # Cascade downstream resets — see comment on the remap branch above.
            stages_reset = _reset_downstream_mapping_stages(project, session)
            return {"status": "ok", "stages_reset": stages_reset}
        elif body.action == "escalate_to_steward":
            if not body.escalation_reason:
                raise HTTPException(400, "escalation_reason is required for escalate_to_steward")
            ns.run(
                ESCALATE_TO_STEWARD_QUERY,
                mapping_uri=body.mapping_uri,
                reason=body.escalation_reason,
                reviewer=body.reviewer,
                timestamp=ts,
            )
            # Escalation moves the mapping out of pending_review; check whether the
            # mappings stage should now close. The escalation queue is its own review path.
            _check_review_complete(project, "mappings", session)
            return {"status": "ok"}
        else:
            raise HTTPException(
                400,
                "action must be 'approve' | 'reject' | 'edit_transform' | 'replace_mapping' | 'escalate_to_steward'",
            )

    _check_review_complete(project, "mappings", session)
    return {"status": "ok"}


# ── Bulk wipe (engineer re-running data_mapping from scratch) ────────────────

class WipeMappingsBody(BaseModel):
    reviewer: str = "workbench-engineer"


@router.post("/mappings/wipe")
def wipe_project_mappings(
    project_id: int,
    body: WipeMappingsBody,
    session: Session = Depends(get_session),
    _user=Depends(require_review("mappings")),
):
    """Soft-wipe all current :ColumnMapping rows for this project.

    Marks each as status='superseded', isCurrent=false so the audit chain
    survives. Returns wiped_count so the UI can confirm the scope. Drives
    the "Re-run mapping → Start over" affordance in MappingReviewPanel.

    Project scope keys on the target :DProdColumn URI prefix (mirrors
    PENDING_MAPPINGS_QUERY_S / UNMAPPED_COLUMNS_QUERY_S). Projects that
    pre-date :Project nodes (no has_project_node) can't be safely scoped
    by URI prefix because they lack a project_code; we require scope here.
    """
    project = _get_project_for_write(project_id, session)
    if not has_project_node(project):
        raise HTTPException(
            400,
            "project lacks a :Project node — cannot safely scope a bulk wipe by project_code",
        )
    ts = _ts()
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        row = ns.run(
            WIPE_PROJECT_MAPPINGS_QUERY,
            project_code=project.project_code,
            reviewer=body.reviewer,
            timestamp=ts,
        ).single()
        wiped_count = int(row["wiped_count"]) if row else 0
    return {"wiped_count": wiped_count}


# ── Unmapped columns (engineer-side gap-filling) ──────────────────────────────

def _build_manual_mapping_uri(project_code: Optional[str], source_uris: list, product_uri: str) -> str:
    """Mirrors write_mappings.py's _mapping_uri helper inline so the manual
    creation path produces URIs in the same shape as the skill-driven path.
    """
    from hashlib import md5
    if not source_uris:
        # Pure target-only "placeholder" — should be rare; fall back to the product URI fragment.
        primary = product_uri.replace("dprod:", "").replace(":column:", ".")
        if project_code:
            return f"mapping:{project_code}:manual:{primary}"
        return f"mapping:manual:{primary}"

    primary_src = source_uris[0].replace("column:", "")
    if len(source_uris) > 1:
        digest = md5("|".join(sorted(source_uris)).encode()).hexdigest()[:8]
        primary_src = f"{primary_src}+{digest}"
    tgt = product_uri.replace("dprod:", "").replace(":column:", ".")
    if project_code:
        return f"mapping:{project_code}:{primary_src}:{tgt}"
    return f"mapping:{primary_src}:{tgt}"


@router.get("/unmapped_columns")
def get_unmapped_columns(
    project_id: int,
    summary: bool = QParam(False),
    session: Session = Depends(get_session),
):
    """List :DProdColumn nodes for this project that have no current ColumnMapping."""
    project = _get_project(project_id, session)
    scoped = has_project_node(project)
    pc = {"project_code": project.project_code} if scoped else {}
    q = UNMAPPED_COLUMNS_QUERY_S if scoped else UNMAPPED_COLUMNS_QUERY
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        result = ns.run(q, **pc)
        rows = []
        import json as _json
        for r in result:
            d = dict(r)
            # Decode transformHint JSON for the UI.
            th = d.pop("transform_hint", None)
            if th:
                try:
                    d["transform_hint"] = _json.loads(th)
                except (ValueError, TypeError):
                    d["transform_hint"] = th
            rows.append(d)
    if summary:
        rows = _summarize_items("unmapped_columns", rows)
    return {"items": rows, "count": len(rows)}


@router.post("/unmapped_columns")
def create_manual_mapping(
    project_id: int,
    body: CreateManualMappingAction,
    session: Session = Depends(get_session),
    _user=Depends(require_review("unmapped_columns")),
):
    """Engineer hand-creates a mapping. status='pending_review' so the Reviewer
    still signs off via the regular Mappings review queue."""
    project = _get_project_for_write(project_id, session)
    if not body.transform_kind:
        raise HTTPException(400, "transform_kind is required")
    # Literals (constants) have no source columns by design; everything else
    # must reference at least one.
    if body.transform_kind != "literal" and not body.source_col_uris:
        raise HTTPException(400, "source_col_uris is required (at least one)")
    if body.transform_kind == "literal":
        params = body.transform_params or {}
        if not str(params.get("literal_value", "")).strip():
            raise HTTPException(400, "literal kind requires transform_params.literal_value")

    ts = _ts()
    project_code = project.project_code if has_project_node(project) else None
    mapping_uri = _build_manual_mapping_uri(project_code, body.source_col_uris, body.product_col_uri)

    legacy_type = "direct" if body.transform_kind == "direct" else "derived"
    import json as _json
    inputs_payload = body.transform_inputs if body.transform_inputs is not None else body.source_col_uris

    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        # Verify the target product column exists.
        check = ns.run(
            "MATCH (pc:DProdColumn {uri: $uri}) RETURN count(pc) AS n",
            uri=body.product_col_uri,
        ).single()
        if not check or check["n"] == 0:
            raise HTTPException(404, "product column not found")

        # Verify all source columns exist. Dispatch on URI prefix so dpe-cf
        # mappings sourcing from a CONSUMES'd source product's :DProdColumn
        # are validated against the right label rather than always-:Column.
        for s in body.source_col_uris:
            label = _source_node_label(s)
            r = ns.run(
                f"MATCH (col:{label} {{uri: $uri}}) RETURN count(col) AS n",
                uri=s,
            ).single()
            if not r or r["n"] == 0:
                raise HTTPException(404, f"source column not found: {s}")

        result = ns.run(
            CREATE_MANUAL_MAPPING_QUERY,
            mapping_uri=mapping_uri,
            product_col_uri=body.product_col_uri,
            rationale=body.rationale or "Engineer-authored mapping.",
            legacy_mapping_type=legacy_type,
            transform_kind=body.transform_kind,
            transform_expression=body.transform_expression or "",
            transform_inputs_json=_json.dumps(inputs_payload),
            transform_params_json=_json.dumps(body.transform_params or {}),
            transform_decorators_json=_json.dumps(body.transform_decorators or {}),
            reviewer=body.reviewer,
            timestamp=ts,
        ).single()
        if result is None:
            # The CREATE was filtered out by the WHERE NOT EXISTS guard —
            # someone else mapped this column between GET and POST.
            raise HTTPException(409, "product column already has a current mapping")

        # Attach one MAPS_SOURCE_COLUMN edge per source URI, dispatching on
        # URI prefix so :Column and :DProdColumn sources both work.
        for src in body.source_col_uris:
            label = _source_node_label(src)
            cypher = (
                ATTACH_MAPPING_SOURCE_EDGE_DPRODCOLUMN
                if label == "DProdColumn"
                else ATTACH_MAPPING_SOURCE_EDGE_COLUMN
            )
            ns.run(cypher, mapping_uri=mapping_uri, source_uri=src)

        # Manual mappings may be lookups — build their :LOOKUP_VIA lineage
        # edges immediately (idempotent), mirroring write_mappings.py's
        # authoring-time behaviour on the AI path.
        reconcile_lookup_via(ns, project.project_code)

    # Mirror the other mapping write paths (replace_mapping / escalate): the
    # column now has a current mapping, so re-evaluate the mappings review stage
    # lifecycle. Unmapped count is query-derived and drops on the next fetch.
    _check_review_complete(project, "mappings", session)
    return {"status": "ok", "mapping_uri": mapping_uri}


# ── Transformation escalations (steward-side) ─────────────────────────────────

@router.get("/transformation_escalations")
def get_transformation_escalations(
    project_id: int,
    summary: bool = QParam(False),
    session: Session = Depends(get_session),
):
    """List mappings in status='steward_review' for this project."""
    project = _get_project(project_id, session)
    scoped = has_project_node(project)
    pc = {"project_code": project.project_code} if scoped else {}
    q = PENDING_ESCALATIONS_QUERY_S if scoped else PENDING_ESCALATIONS_QUERY
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        result = ns.run(q, **pc)
        rows = [dict(r) for r in result]
    items = _dedup_mapping_rows(rows)
    if summary:
        items = _summarize_items("transformation_escalations", items)
    return {"items": items, "count": len(items)}


@router.post("/transformation_escalations")
def respond_to_escalation(
    project_id: int,
    body: TransformationEscalationAction,
    session: Session = Depends(get_session),
    _user=Depends(require_review("transformation_escalations")),
):
    """Steward responds to an escalation. Phase 1 supports `answer_inline`;
    `add_to_catalog` and `bounce_to_po` will land in Phase 6."""
    project = _get_project_for_write(project_id, session)
    ts = _ts()
    import json as _json

    if body.action == "answer_inline":
        if not body.transform_kind:
            raise HTTPException(400, "transform_kind is required for answer_inline")
        with neo4j_session(
            project.neo4j_host, project.neo4j_port,
            project.neo4j_user, project.neo4j_password, project.neo4j_database,
        ) as ns:
            ns.run(
                STEWARD_ANSWER_INLINE_QUERY,
                mapping_uri=body.mapping_uri,
                transform_kind=body.transform_kind,
                transform_expression=body.transform_expression or "",
                transform_inputs_json=_json.dumps(body.transform_inputs) if body.transform_inputs is not None else None,
                transform_params_json=_json.dumps(body.transform_params) if body.transform_params is not None else None,
                transform_decorators_json=_json.dumps(body.transform_decorators) if body.transform_decorators is not None else None,
                reviewer=body.reviewer,
                timestamp=ts,
            )
        return {"status": "ok"}

    raise HTTPException(
        400,
        "action must be 'answer_inline' (Phase 1); 'add_to_catalog' and 'bounce_to_po' arrive in Phase 6",
    )


_GRAPH_MAPPINGS_RETURN = """\
    cm.uri                       AS mapping_uri,
    cm.status                    AS status,
    cm.isCurrent                 AS is_current,
    cm.similarityScore           AS similarity_score,
    cm.transformKind             AS transform_kind,
    cm.transformAuthor           AS transform_author,
    cm.transformExpression       AS transform_expression,
    cm.transformParams           AS transform_params_json,
    cm.transformEscalationReason AS transform_escalation_reason,
    coalesce(col.uri, sc_dp.uri)               AS source_col_uri,
    coalesce(col.name, sc_dp.name)             AS source_col_name,
    coalesce(col.dataType, sc_dp.dataType)     AS source_col_type,
    coalesce(col.ordinal, sc_dp.ordinal)       AS source_col_ordinal,
    coalesce(ds.uri, ods.uri)                  AS source_table_uri,
    coalesce(ds.schema, srcDp.name)            AS source_schema,
    coalesce(ds.name, ods.physicalName, ods.name) AS source_table,
    pc.uri                       AS product_col_uri,
    pc.name                      AS product_col_name,
    pc.ordinal                   AS product_col_ordinal,
    pc.dataType                  AS product_col_type,
    pc.primaryKey                AS product_col_primary_key,
    coalesce(pc.datasetPhysicalName, '') AS product_dataset_name,
    dp.uri                       AS product_uri,
    dp.name                      AS product_name
"""

# All discovered source datasets + columns for the project. Used so the graph
# canvas can render the full source schema (left side) even before any
# :ColumnMapping rows exist — i.e. post-discovery, post-odcs_to_dprod, but
# pre-data_mapping. Patterned on summary.py:DATASET_GRAPH_TABLES_S.
ALL_SOURCE_TABLES_QUERY = """\
MATCH (ds:Dataset)
OPTIONAL MATCH (ds)-[:HAS_COLUMN]->(col:Column)
WITH ds, col
ORDER BY ds.schema, ds.name, col.ordinal
RETURN ds.uri AS uri, ds.schema AS schema, ds.name AS table,
       collect(CASE WHEN col IS NULL THEN NULL ELSE
         {uri: col.uri, name: col.name, data_type: col.dataType, ordinal: col.ordinal}
       END) AS columns
ORDER BY ds.schema, ds.name
"""
ALL_SOURCE_TABLES_QUERY_S = """\
MATCH (:Project {projectCode: $project_code})-[:HAS_CATALOG]->(:Catalog)-[:DCAT_DATASET]->(ds:Dataset)
OPTIONAL MATCH (ds)-[:HAS_COLUMN]->(col:Column)
WITH ds, col
ORDER BY ds.schema, ds.name, col.ordinal
RETURN ds.uri AS uri, ds.schema AS schema, ds.name AS table,
       collect(CASE WHEN col IS NULL THEN NULL ELSE
         {uri: col.uri, name: col.name, data_type: col.dataType, ordinal: col.ordinal}
       END) AS columns
ORDER BY ds.schema, ds.name
"""

# Consumer-aligned variant: pull source datasets+columns from
# :DProdOutputDataset reachable via the consumer contract's :CONSUMES edges.
# Schema column displays the source product name so the lineage canvas
# labels the source table with where it came from. Used by the engineer's
# mapping graph view for dpe-cf projects.
ALL_SOURCE_DPROD_TABLES_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})-[r_c:CONSUMES]->(srcDp:DProdDataProduct)
WHERE r_c.fromVersion <= dc.currentVersion
  AND (r_c.toVersion IS NULL OR r_c.toVersion >= dc.currentVersion)
MATCH (srcDp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
OPTIONAL MATCH (ods)-[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
WITH srcDp, ods, pc
ORDER BY srcDp.name, ods.physicalName, pc.ordinal
WITH srcDp, ods,
     collect(CASE WHEN pc IS NULL THEN NULL ELSE
       {uri: pc.uri, name: pc.name, data_type: pc.dataType, ordinal: coalesce(pc.ordinal, 0)}
     END) AS columns
RETURN
    ods.uri AS uri,
    srcDp.name AS schema,
    coalesce(ods.physicalName, ods.name) AS table,
    columns
ORDER BY srcDp.name, ods.physicalName
"""

# Anchor on (cm)-[:MAPS_TO_PRODUCT_COLUMN]->(pc) and OPTIONAL MATCH the source
# side. Literal mappings (transformKind='literal') have no :MAPS_SOURCE_COLUMN
# edge by design — entering via the source side would silently drop them and
# the frontend would render their target columns as UNMAPPED.
# NOTE on WHERE positioning: a trailing WHERE after a chain of OPTIONAL
# MATCHes binds to the LAST OPTIONAL pattern (Cypher's grammar), turning it
# into a per-pattern guard rather than a row filter. With the dual OPTIONAL
# pattern below this would let cross-project mappings leak into the result.
# Push the row filter into a WITH right after the required matches and
# before the OPTIONAL chain so it acts as a true filter.
GRAPH_MAPPINGS_QUERY = f"""\
MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
MATCH (dp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc)
WHERE cm.isCurrent = true
WITH cm, pc, dp
{_MAPPING_SOURCE_DUAL_OPTIONAL}
RETURN
{_GRAPH_MAPPINGS_RETURN}
ORDER BY pc.ordinal, pc.name
"""

# Project-scoped variant. Scope on the product column URI prefix (matching
# UNMAPPED_COLUMNS_QUERY_S) rather than via :Project → :Catalog → :Dataset →
# :Column — that path forces a source-side join and would re-exclude literals
# and consumer-aligned dprod-source mappings.
GRAPH_MAPPINGS_QUERY_S = f"""\
MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
MATCH (dp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc)
WHERE cm.isCurrent = true
  AND pc.uri STARTS WITH 'dprod:col:' + $project_code + '-contract:'
WITH cm, pc, dp
{_MAPPING_SOURCE_DUAL_OPTIONAL}
RETURN
{_GRAPH_MAPPINGS_RETURN}
ORDER BY pc.ordinal, pc.name
"""

# Lookup-source edges for the mapping graph. A lookup-kind mapping reads a
# value from a separate reference table; that column hangs off the mapping via
# :LOOKUP_VIA (backend/lookup_via.py). The source column itself is already on
# the canvas (the source-tables query loads the full discovered / CONSUMES'd
# set), so here we only need the edge endpoints to draw a dashed lookup_via
# edge. Kept separate from GRAPH_MAPPINGS_QUERY so the primary row stream stays
# one-row-per-(mapping, primary source-col).
GRAPH_LOOKUP_QUERY = """\
MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
WHERE cm.isCurrent = true
MATCH (cm)-[lv:LOOKUP_VIA]->(lk)
RETURN cm.uri AS mapping_uri, cm.status AS status,
       cm.transformKind AS transform_kind, cm.transformAuthor AS transform_author,
       cm.transformExpression AS transform_expression,
       cm.transformParams AS transform_params_json,
       lv.role AS lookup_role, lv.strategy AS lookup_strategy,
       lk.uri AS source_col_uri, pc.uri AS product_col_uri
"""
GRAPH_LOOKUP_QUERY_S = """\
MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
WHERE cm.isCurrent = true
  AND pc.uri STARTS WITH 'dprod:col:' + $project_code + '-contract:'
MATCH (cm)-[lv:LOOKUP_VIA]->(lk)
RETURN cm.uri AS mapping_uri, cm.status AS status,
       cm.transformKind AS transform_kind, cm.transformAuthor AS transform_author,
       cm.transformExpression AS transform_expression,
       cm.transformParams AS transform_params_json,
       lv.role AS lookup_role, lv.strategy AS lookup_strategy,
       lk.uri AS source_col_uri, pc.uri AS product_col_uri
"""


# All :DProdColumn for the project's data product so we can render unmapped
# product columns as nodes-with-no-incoming-edge in the graph view.
# product_dataset_* lets the canvas group by output dataset so SA products
# with multiple datasets show as N boxes instead of one collapsed product.
GRAPH_PRODUCT_COLUMNS_QUERY = """\
MATCH (dp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(prod_ods:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
RETURN
    dp.uri        AS product_uri,
    dp.name       AS product_name,
    pc.uri        AS product_col_uri,
    pc.name       AS product_col_name,
    pc.ordinal    AS ordinal,
    pc.dataType   AS data_type,
    pc.primaryKey AS primary_key,
    prod_ods.uri                                       AS product_dataset_uri,
    coalesce(prod_ods.physicalName, prod_ods.name, '') AS product_dataset_name
ORDER BY product_dataset_name, pc.ordinal, pc.name
"""

GRAPH_PRODUCT_COLUMNS_QUERY_S = """\
MATCH (dp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(prod_ods:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
WHERE pc.uri STARTS WITH 'dprod:col:' + $project_code + '-contract:'
RETURN
    dp.uri        AS product_uri,
    dp.name       AS product_name,
    pc.uri        AS product_col_uri,
    pc.name       AS product_col_name,
    pc.ordinal    AS ordinal,
    pc.dataType   AS data_type,
    pc.primaryKey AS primary_key,
    prod_ods.uri                                       AS product_dataset_uri,
    coalesce(prod_ods.physicalName, prod_ods.name, '') AS product_dataset_name
ORDER BY product_dataset_name, pc.ordinal, pc.name
"""


@router.get("/mappings/graph")
def get_mapping_graph(
    project_id: int,
    session: Session = Depends(get_session),
):
    """Return a consolidated payload for the mapping graph view.

    Includes ALL current mappings regardless of status (approved /
    pending_review / steward_review / rejected) so the engineer sees the
    full picture, plus the complete product column set so unmapped
    columns render as nodes-with-no-incoming-edge.

    Source tables are the full discovered set (every :Dataset reachable
    from the project's :Catalog), so the canvas can render pre-mapping —
    showing source schema and product columns with no edges yet. The
    frontend has a "Show only sources with mappings" toggle that filters
    this back down client-side.
    """
    project = _get_project(project_id, session)
    scoped = has_project_node(project)
    pc = {"project_code": project.project_code} if scoped else {}
    map_q = GRAPH_MAPPINGS_QUERY_S if scoped else GRAPH_MAPPINGS_QUERY
    pcol_q = GRAPH_PRODUCT_COLUMNS_QUERY_S if scoped else GRAPH_PRODUCT_COLUMNS_QUERY

    # Consumer-aligned (dpe-cf) sources its lineage canvas from CONSUMES'd
    # source-product datasets, not raw :Catalog/:Dataset. dpe-sa and legacy
    # archetypes use the discovered catalog. Other archetypes fall through
    # to the catalog query as well — they don't render mappings.
    is_consumer = project.archetype == "dpe-cf"
    if is_consumer:
        src_q = ALL_SOURCE_DPROD_TABLES_QUERY
        src_params = {"contract_id": f"{project.project_code}-contract"}
    else:
        src_q = ALL_SOURCE_TABLES_QUERY_S if scoped else ALL_SOURCE_TABLES_QUERY
        src_params = pc

    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        mapping_rows = [dict(r) for r in ns.run(map_q, **pc)]
        product_rows = [dict(r) for r in ns.run(pcol_q, **pc)]
        source_rows = [dict(r) for r in ns.run(src_q, **src_params)]
        lookup_q = GRAPH_LOOKUP_QUERY_S if scoped else GRAPH_LOOKUP_QUERY
        lookup_rows = [dict(r) for r in ns.run(lookup_q, **pc)]

    # Build source_tables from the canonical discovered set. Each table's
    # columns list comes from :Dataset -[:HAS_COLUMN]-> :Column directly,
    # so it includes columns that don't (yet) participate in a mapping.
    sources_list: list[dict] = []
    valid_source_col_uris: set[str] = set()
    for r in source_rows:
        cols = [c for c in (r.get("columns") or []) if c is not None]
        for c in cols:
            uri = c.get("uri")
            if uri:
                valid_source_col_uris.add(uri)
        sources_list.append({
            "uri": r.get("uri"),
            "schema": r.get("schema"),
            "table": r.get("table"),
            "columns": cols,
        })

    # Detect orphan mappings whose source :Column isn't reachable via the
    # project's :Catalog → :Dataset → :HAS_COLUMN path. These are dropped
    # from the mapping list so the frontend doesn't render edges to a
    # source node that doesn't exist on the canvas. (Was previously
    # detected by absent source_table_uri/schema/table on the mapping row;
    # the canonical-set check is equivalent and survives the source-side
    # query change.)
    orphan_source_mapping_uris: set[str] = set()
    for r in mapping_rows:
        col_uri = r.get("source_col_uri")
        if not col_uri:
            continue  # literal mapping — no source side, not an orphan
        if col_uri not in valid_source_col_uris:
            muri = r.get("mapping_uri")
            if muri:
                orphan_source_mapping_uris.add(muri)

    # Mappings deduped by mapping_uri (source/target are 1:1 per current row).
    # For literal-kind mappings, parse transformParams (JSON string) to surface
    # literal_value so the frontend can render a "= 'USD'" chip on the target
    # product column row instead of an UNMAPPED badge. Mappings whose source
    # column is orphaned (no Dataset link) are dropped here too — without this
    # the frontend would render an edge to a non-existent source node.
    seen_mappings: set[str] = set()
    mappings: list[dict] = []
    for r in mapping_rows:
        muri = r.get("mapping_uri")
        if not muri or muri in seen_mappings:
            continue
        if muri in orphan_source_mapping_uris:
            continue
        seen_mappings.add(muri)
        literal_value: Optional[str] = None
        if r.get("transform_kind") == "literal":
            params_raw = r.get("transform_params_json")
            try:
                params = json.loads(params_raw) if isinstance(params_raw, str) else (params_raw or {})
            except (ValueError, TypeError):
                params = {}
            lv = params.get("literal_value") if isinstance(params, dict) else None
            literal_value = str(lv) if lv is not None else None
        mappings.append({
            "uri": muri,
            "status": r.get("status"),
            "source_uri": r.get("source_col_uri"),
            "target_uri": r.get("product_col_uri"),
            "transform_kind": r.get("transform_kind"),
            "transform_author": r.get("transform_author"),
            "transform_expression": r.get("transform_expression"),
            "transform_escalation_reason": r.get("transform_escalation_reason"),
            "similarity_score": r.get("similarity_score"),
            "literal_value": literal_value,
        })

    # Lookup-source edges. The lookup column is already a node on the canvas
    # (loaded by the source-tables query), so we only emit dashed lookup_via
    # edges to it — guarding on valid_source_col_uris so we never draw an edge
    # to a node that isn't rendered. Tag a source column is_lookup only when it
    # is referenced exclusively by a lookup (a column that's also a primary
    # source is already shown as mapped, so the badge would be misleading).
    primary_source_col_uris = {
        r.get("source_col_uri") for r in mapping_rows if r.get("source_col_uri")
    }
    lookup_only_col_uris: set[str] = set()
    for r in lookup_rows:
        lk_uri = r.get("source_col_uri")
        if not lk_uri or lk_uri not in valid_source_col_uris:
            continue
        role = r.get("lookup_role") or "value"
        mappings.append({
            "uri": f"{r.get('mapping_uri')}::lookup::{role}::{lk_uri}",
            "mapping_uri": r.get("mapping_uri"),
            "status": r.get("status"),
            "source_uri": lk_uri,
            "target_uri": r.get("product_col_uri"),
            "transform_kind": r.get("transform_kind"),
            "transform_author": r.get("transform_author"),
            "transform_expression": None,
            "transform_escalation_reason": None,
            "similarity_score": None,
            "literal_value": None,
            "relation": "lookup_via",
            "role": role,
            "strategy": r.get("lookup_strategy"),
        })
        if lk_uri not in primary_source_col_uris:
            lookup_only_col_uris.add(lk_uri)
    if lookup_only_col_uris:
        for tbl in sources_list:
            for c in tbl.get("columns") or []:
                if c.get("uri") in lookup_only_col_uris:
                    c["is_lookup"] = True

    # Product table — DPE-CF projects always have exactly one DProdDataProduct
    # with one DProdOutputDataset. Use the first row to identify it.
    product_uri = product_rows[0].get("product_uri") if product_rows else None
    product_name = product_rows[0].get("product_name") if product_rows else None
    product_columns = [
        {
            "uri": r.get("product_col_uri"),
            "name": r.get("product_col_name"),
            "ordinal": r.get("ordinal"),
            "data_type": r.get("data_type"),
            "primary_key": bool(r.get("primary_key")),
            "dataset_name": r.get("product_dataset_name") or None,
        }
        for r in product_rows
    ]

    # Defensive fallback: if the project-scoping path didn't resolve product
    # columns (different graph shapes across projects), recover them from the
    # mapping rows. We may miss unmapped columns this way, but at least the
    # graph isn't empty and edges have handles to attach to.
    if not product_columns and mapping_rows:
        seen_pc_uris: set[str] = set()
        for r in mapping_rows:
            pc_uri = r.get("product_col_uri")
            if not pc_uri or pc_uri in seen_pc_uris:
                continue
            seen_pc_uris.add(pc_uri)
            product_columns.append({
                "uri": pc_uri,
                "name": r.get("product_col_name"),
                "ordinal": r.get("product_col_ordinal"),
                "data_type": r.get("product_col_type"),
                "primary_key": bool(r.get("product_col_primary_key")),
                "dataset_name": r.get("product_dataset_name") or None,
            })
            if not product_uri:
                product_uri = r.get("product_uri")
            if not product_name:
                product_name = r.get("product_name")
        product_columns.sort(key=lambda c: ((c.get("ordinal") or 0), (c.get("name") or "")))

    return {
        "source_tables": sources_list,
        "product": {
            "uri": product_uri,
            "name": product_name,
            "columns": product_columns,
        },
        "mappings": mappings,
        # Number of mappings whose source :Column existed but had no parent
        # :Dataset → :HAS_COLUMN edge. Surfaced so the UI can warn and so we
        # have telemetry for diagnosing the orphan-column root cause.
        "orphan_source_mappings": len(orphan_source_mapping_uris),
    }


@router.get("/mappings/source-columns")
def get_source_columns(
    project_id: int,
    session: Session = Depends(get_session),
):
    """Return all discovered source columns for remap selection."""
    project = _get_project(project_id, session)
    scoped = has_project_node(project)
    pc = {"project_code": project.project_code} if scoped else {}
    q = SOURCE_COLUMNS_QUERY_S if scoped else SOURCE_COLUMNS_QUERY
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        result = ns.run(q, **pc)
        columns = [dict(r) for r in result]
    return {"columns": columns}


@router.get("/mappings/product-columns")
def get_product_columns(
    project_id: int,
    product_uri: str,
    session: Session = Depends(get_session),
):
    project = _get_project(project_id, session)
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        result = ns.run(PRODUCT_COLUMNS_QUERY, product_uri=product_uri)
        columns = [dict(r) for r in result]
    return {"columns": columns}


# ── Domain Rules Reviews ───────────────────────────────────────────────────

@router.get("/domain_rules")
def get_pending_domain_rules(
    project_id: int,
    summary: bool = QParam(False),
    session: Session = Depends(get_session),
):
    project = _get_project(project_id, session)
    scoped = has_project_node(project)
    pc = {"project_code": project.project_code} if scoped else {}
    q = PENDING_DOMAIN_RULES_QUERY_S if scoped else PENDING_DOMAIN_RULES_QUERY
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        result = ns.run(q, **pc)
        items = [dict(r) for r in result]
    items.sort(key=lambda r: (r.get("source_type", ""), r.get("schema", ""), r.get("table_name", ""), r.get("_ordinal", 0), r.get("rule_type", "")))
    for item in items:
        item.pop("_ordinal", None)
    if summary:
        items = _summarize_items("domain_rules", items)
    return {"count": len(items), "items": items}


@router.post("/domain_rules")
def review_domain_rule(
    project_id: int,
    body: DomainRuleReviewAction,
    session: Session = Depends(get_session),
    _user=Depends(require_review("domain_rules")),
):
    project = _get_project_for_write(project_id, session)
    ts = _ts()
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        if body.action == "approve":
            ns.run(
                APPROVE_DOMAIN_RULE_QUERY,
                rule_uri=body.rule_uri,
                quality=body.quality or 2,
                reviewer=body.reviewer,
                timestamp=ts,
            )
        elif body.action == "reject":
            ns.run(
                REJECT_DOMAIN_RULE_QUERY,
                rule_uri=body.rule_uri,
                category=body.category or "other",
                detail=body.detail or "",
                reviewer=body.reviewer,
                timestamp=ts,
            )
        else:
            raise HTTPException(400, "action must be 'approve' or 'reject'")
        # Rule status just changed — bump the schema-change marker so any
        # cached :QAEvaluation flips to stale on next marketplace open.
        ns.run(
            "MATCH (dc:DataContract {id: $contract_id}) "
            "SET dc.lastSchemaChangeVersion = coalesce(dc.currentVersion, 1)",
            contract_id=f"{project.project_code}-contract",
        )

    _check_review_complete(project, "domain_rules", session)
    return {"status": "ok"}


APPROVE_ALL_DOMAIN_RULES = """\
MATCH (ps:PropertyShape)
WHERE ps.ruleSource = 'domain' AND ps.status = 'pending_review'
SET ps.status = 'approved'
WITH ps
CREATE (act:ProvActivity {
    activityType: 'rule_review',
    outcome:      'approved',
    quality:      $quality,
    occurredAt:   $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (ps)-[:PROV_WAS_GENERATED_BY]->(act)
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
RETURN count(ps) AS cnt
"""

APPROVE_ALL_DOMAIN_RULES_S = f"""\
CALL {{
  {_PRJ_DS}(:Dataset)-[:HAS_SHAPE]->(:NodeShape)-[:PROPERTY]->(ps:PropertyShape)
  WHERE ps.ruleSource = 'domain' AND ps.status = 'pending_review'
  RETURN ps
UNION
  MATCH (pc:DProdColumn)<-[:ON_DPROD_COLUMN]-(ps:PropertyShape)
  WHERE pc.uri STARTS WITH 'dprod:col:' + $project_code + '-contract:'
    AND ps.ruleSource = 'domain' AND ps.status = 'pending_review'
  RETURN ps
}}
WITH DISTINCT ps
SET ps.status = 'approved'
CREATE (act:ProvActivity {{
    activityType: 'rule_review',
    outcome:      'approved',
    quality:      $quality,
    occurredAt:   $timestamp
}})
MERGE (agent:ProvAgent {{uri: 'prov:agent:human:' + $reviewer}})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (ps)-[:PROV_WAS_GENERATED_BY]->(act)
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
RETURN count(ps) AS cnt
"""


@router.post("/domain_rules/approve-all")
def approve_all_domain_rules(
    project_id: int,
    body: BulkApproveRequest,
    session: Session = Depends(get_session),
    _user=Depends(require_review("domain_rules")),
):
    """Approve all pending domain rules at once (for demo use)."""
    project = _get_project_for_write(project_id, session)
    scoped = has_project_node(project)
    pc = {"project_code": project.project_code} if scoped else {}
    q = APPROVE_ALL_DOMAIN_RULES_S if scoped else APPROVE_ALL_DOMAIN_RULES
    ts = _ts()

    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        result = ns.run(q, quality=body.quality, reviewer=body.reviewer, timestamp=ts, **pc)
        count = result.single()["cnt"]
        # Same staleness bump as the per-rule endpoint above.
        ns.run(
            "MATCH (dc:DataContract {id: $contract_id}) "
            "SET dc.lastSchemaChangeVersion = coalesce(dc.currentVersion, 1)",
            contract_id=f"{project.project_code}-contract",
        )

    _check_review_complete(project, "domain_rules", session)
    return {"status": "ok", "approved": count}


APPROVE_ALL_MAPPINGS_PENDING_URIS = """\
MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
WHERE cm.isCurrent = true AND cm.status = 'pending_review'
  AND pc.uri STARTS WITH 'dprod:col:' + $project_code + '-contract:'
  AND ($min_score IS NULL OR coalesce(cm.similarityScore, 0.0) >= $min_score)
RETURN cm.uri AS uri
"""


class MappingApproveAllRequest(BaseModel):
    quality: int = 2
    reviewer: str = "workbench-user"
    # Auto-approve gate: when set, only pending mappings scoring >= this (0-1) are
    # approved ("auto-approve high-confidence"); null approves every pending mapping.
    min_score: float | None = None


@router.post("/mappings/approve-all")
def approve_all_mappings(
    project_id: int,
    body: MappingApproveAllRequest,
    session: Session = Depends(get_session),
    _user=Depends(require_review("mappings")),
):
    """Approve pending :ColumnMappings for the project in one call — either all, or
    (auto-approve) only those scoring >= `min_score`.

    Loops the same APPROVE_MAPPING_QUERY the per-mapping review uses (identical
    PROV provenance + :LOOKUP_VIA reconcile), then flips the mapping stage
    complete when the queue empties. Scoped to the project's own contract."""
    project = _get_project_for_write(project_id, session)
    ts = _ts()
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        uris = [r["uri"] for r in ns.run(
            APPROVE_ALL_MAPPINGS_PENDING_URIS,
            project_code=project.project_code,
            min_score=body.min_score,
        )]
        for uri in uris:
            ns.run(APPROVE_MAPPING_QUERY, mapping_uri=uri, reviewer=body.reviewer,
                   timestamp=ts, quality=body.quality or 2)
        if uris:
            reconcile_lookup_via(ns, project.project_code)
    _check_review_complete(project, "mappings", session)
    return {"status": "ok", "approved": len(uris), "min_score": body.min_score}


# ── Source product validation (combined: names + descriptions + rules) ─────
#
# The dpe-sa flow's PO validation gate. Three buckets in one panel:
#   1. column_names   — :Column with a non-null .recommendedName whose
#                       .recommendedNameStatus is 'pending_review'.
#   2. descriptions   — same shape as the existing /descriptions surface.
#   3. rules          — :PropertyShape with ruleSource='observation' and
#                       status='pending_review' (Tier 1 baseline DQ rules).
#
# The stage advances to ``complete`` only when all three buckets are empty
# (see _check_review_complete).

PENDING_NAMES_QUERY = """\
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)
WHERE col.recommendedName IS NOT NULL
  AND col.recommendedName <> ''
  AND coalesce(col.recommendedNameStatus, 'pending_review') = 'pending_review'
RETURN
    ds.schema           AS schema,
    ds.name             AS table_name,
    col.uri             AS col_uri,
    col.name            AS current_name,
    col.recommendedName AS recommended_name,
    col.dataType        AS data_type,
    coalesce(col.sensitivity, 'none') AS sensitivity,
    col.ordinal         AS ordinal
ORDER BY ds.schema, ds.name, col.ordinal
"""

PENDING_NAMES_QUERY_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_COLUMN]->(col:Column)
WHERE col.recommendedName IS NOT NULL
  AND col.recommendedName <> ''
  AND coalesce(col.recommendedNameStatus, 'pending_review') = 'pending_review'
RETURN
    ds.schema           AS schema,
    ds.name             AS table_name,
    col.uri             AS col_uri,
    col.name            AS current_name,
    col.recommendedName AS recommended_name,
    col.dataType        AS data_type,
    coalesce(col.sensitivity, 'none') AS sensitivity,
    col.ordinal         AS ordinal
ORDER BY ds.schema, ds.name, col.ordinal
"""

PENDING_OBSERVATION_RULES_QUERY = """\
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)
MATCH (col)<-[:ON_COLUMN]-(ps:PropertyShape)
WHERE coalesce(ps.ruleSource, 'observation') = 'observation'
  AND ps.status = 'pending_review'
RETURN
    ds.schema      AS schema,
    ds.name        AS table_name,
    col.uri        AS col_uri,
    col.name       AS col_name,
    col.dataType   AS data_type,
    ps.uri         AS rule_uri,
    ps.ruleType    AS rule_type,
    ps.severity    AS severity,
    ps.description AS description,
    ps.confidence  AS confidence
ORDER BY ds.schema, ds.name, col.ordinal, ps.ruleType
"""

PENDING_OBSERVATION_RULES_QUERY_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_COLUMN]->(col:Column)
MATCH (col)<-[:ON_COLUMN]-(ps:PropertyShape)
WHERE coalesce(ps.ruleSource, 'observation') = 'observation'
  AND ps.status = 'pending_review'
RETURN
    ds.schema      AS schema,
    ds.name        AS table_name,
    col.uri        AS col_uri,
    col.name       AS col_name,
    col.dataType   AS data_type,
    ps.uri         AS rule_uri,
    ps.ruleType    AS rule_type,
    ps.severity    AS severity,
    ps.description AS description,
    ps.confidence  AS confidence
ORDER BY ds.schema, ds.name, col.ordinal, ps.ruleType
"""

# Edit-mode variants — include all statuses (pending_review / approved /
# rejected) so the PO can re-edit a previously-approved item on a
# published SA product. Each row carries `status` so the frontend can
# render approved items differently from pending. Used when the GET
# endpoint receives ?include_approved=true.
ALL_NAMES_QUERY_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_COLUMN]->(col:Column)
WHERE col.recommendedName IS NOT NULL AND col.recommendedName <> ''
RETURN
    ds.schema           AS schema,
    ds.name             AS table_name,
    col.uri             AS col_uri,
    col.name            AS current_name,
    col.recommendedName AS recommended_name,
    col.dataType        AS data_type,
    col.ordinal         AS ordinal,
    coalesce(col.recommendedNameStatus, 'pending_review') AS status
ORDER BY ds.schema, ds.name, col.ordinal
"""

ALL_DESCRIPTIONS_QUERY_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_COLUMN]->(col:Column)
      -[:HAS_DESCRIPTION]->(cd:ColumnDescription)
WHERE cd.isCurrent = true
RETURN
    ds.schema      AS schema,
    ds.name        AS table_name,
    col.uri        AS col_uri,
    col.name       AS col_name,
    col.dataType   AS data_type,
    col.ordinal    AS ordinal,
    cd.uri         AS desc_uri,
    cd.text        AS description_text,
    coalesce(cd.status, 'pending_review') AS status
ORDER BY ds.schema, ds.name, col.ordinal
"""

# Table-level descriptions reviewed by the PO alongside column names /
# descriptions / observation rules. `relationship_kind` carries the
# classifier the metadata-enrichment skill produced; the PO can approve
# the text as-is, edit it, or reject. Bridge ranker reads the approved
# classification downstream.
PENDING_TABLE_DESCRIPTIONS_QUERY_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_TABLE_DESCRIPTION]->(td:TableDescription)
WHERE td.isCurrent = true AND td.status = 'pending_review'
RETURN
    ds.schema             AS schema,
    ds.name               AS table_name,
    ds.uri                AS dataset_uri,
    td.uri                AS desc_uri,
    td.text               AS description_text,
    coalesce(td.relationshipKind, 'unknown') AS relationship_kind,
    coalesce(td.status, 'pending_review')    AS status
ORDER BY ds.schema, ds.name
"""

ALL_TABLE_DESCRIPTIONS_QUERY_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_TABLE_DESCRIPTION]->(td:TableDescription)
WHERE td.isCurrent = true
RETURN
    ds.schema             AS schema,
    ds.name               AS table_name,
    ds.uri                AS dataset_uri,
    td.uri                AS desc_uri,
    td.text               AS description_text,
    coalesce(td.relationshipKind, 'unknown') AS relationship_kind,
    coalesce(td.status, 'pending_review')    AS status
ORDER BY ds.schema, ds.name
"""

# Phase 8.3: TableDescription approve/reject now emit PROV-O activities
# matching the ColumnDescription pattern at the top of this module. Without
# this, status flips were silent — the audit chain had no record of who
# approved a table-level relationship-kind classification (which feeds the
# consumer-side bridge ranker, so it matters).
APPROVE_TABLE_DESCRIPTION_QUERY = """\
MATCH (td:TableDescription {uri: $desc_uri})
SET td.status = 'approved'
WITH td
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (act:ProvActivity {
    uri:          'prov:activity:table-description-review:' + replace(td.uri, 'desc:', '') + ':' + $timestamp,
    activityType: 'table_description_review',
    outcome:      'approved',
    occurredAt:   $timestamp
})
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:PROV_USED]->(td)
RETURN td.uri AS uri, td.status AS status
"""

REJECT_TABLE_DESCRIPTION_QUERY = """\
MATCH (td:TableDescription {uri: $desc_uri})
SET td.status = 'rejected'
WITH td
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (act:ProvActivity {
    uri:          'prov:activity:table-description-review:' + replace(td.uri, 'desc:', '') + ':' + $timestamp,
    activityType: 'table_description_review',
    outcome:      'rejected',
    occurredAt:   $timestamp
})
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:PROV_USED]->(td)
FOREACH (_ IN CASE WHEN coalesce($detail, '') = '' THEN [] ELSE [1] END |
  CREATE (reason:ProvRejectionReason {
      uri:      'prov:reason:table-description:' + replace(td.uri, 'desc:', '') + ':' + $timestamp,
      category: coalesce($category, 'other'),
      detail:   $detail
  })
  CREATE (act)-[:HAS_REJECTION_REASON]->(reason)
)
RETURN td.uri AS uri, td.status AS status
"""

# Edit-in-place: PO supplies corrected text + relationship_kind. The
# existing description is marked rejected (kept as history) and a new
# approved description is created. PROV-O records the correction.
EDIT_TABLE_DESCRIPTION_QUERY = """\
MATCH (td_old:TableDescription {uri: $desc_uri})<-[:HAS_TABLE_DESCRIPTION]-(ds:Dataset)
SET td_old.status = 'rejected',
    td_old.isCurrent = false
CREATE (td_new:TableDescription {
    uri:              $new_uri,
    text:             $text,
    relationshipKind: $relationship_kind,
    status:           'approved',
    isCurrent:        true,
    createdAt:        $timestamp
})
CREATE (ds)-[:HAS_TABLE_DESCRIPTION]->(td_new)
CREATE (td_new)-[:PROV_WAS_DERIVED_FROM]->(td_old)
RETURN td_new.uri AS uri
"""


# ── Relationship descriptions (PO-reviewed in dpe-sa validation gate) ──
#
# Mirrors the TableDescription review surface above. A
# :RelationshipDescription node hangs off the FROM :Dataset and points
# at the TO :Dataset, describing the semantic meaning of the
# :REFERENCES edge between them (`belongs_to` / `categorises` /
# `audit_log_for` / `references`). Generated by the
# metadata-enrichment skill's write_relationship_descriptions.py;
# reviewed here by the PO so downstream consumers (mapping skill,
# view-DDL bridge ranker, question-analyzer) see only PO-approved
# semantic context.

PENDING_RELATIONSHIP_DESCRIPTIONS_QUERY_S = f"""\
{_PRJ_DS}(from:Dataset)-[:HAS_RELATIONSHIP_DESCRIPTION]->(rd:RelationshipDescription)
      -[:DESCRIBES_REFERENCE_TO]->(to:Dataset)
WHERE rd.isCurrent = true AND rd.status = 'pending_review'
RETURN
    from.schema                                       AS from_schema,
    from.name                                         AS from_table,
    from.uri                                          AS from_dataset_uri,
    to.schema                                         AS to_schema,
    to.name                                           AS to_table,
    to.uri                                            AS to_dataset_uri,
    rd.uri                                            AS desc_uri,
    rd.text                                           AS description_text,
    coalesce(rd.relationshipNature, 'references')     AS relationship_nature,
    coalesce(rd.status, 'pending_review')             AS status
ORDER BY from.schema, from.name, to.name
"""

ALL_RELATIONSHIP_DESCRIPTIONS_QUERY_S = f"""\
{_PRJ_DS}(from:Dataset)-[:HAS_RELATIONSHIP_DESCRIPTION]->(rd:RelationshipDescription)
      -[:DESCRIBES_REFERENCE_TO]->(to:Dataset)
WHERE rd.isCurrent = true
RETURN
    from.schema                                       AS from_schema,
    from.name                                         AS from_table,
    from.uri                                          AS from_dataset_uri,
    to.schema                                         AS to_schema,
    to.name                                           AS to_table,
    to.uri                                            AS to_dataset_uri,
    rd.uri                                            AS desc_uri,
    rd.text                                           AS description_text,
    coalesce(rd.relationshipNature, 'references')     AS relationship_nature,
    coalesce(rd.status, 'pending_review')             AS status
ORDER BY from.schema, from.name, to.name
"""


APPROVE_RELATIONSHIP_DESCRIPTION_QUERY = """\
MATCH (rd:RelationshipDescription {uri: $desc_uri})
SET rd.status = 'approved'
WITH rd
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (act:ProvActivity {
    uri:          'prov:activity:relationship-description-review:' + replace(rd.uri, 'relationship_description:', '') + ':' + $timestamp,
    activityType: 'relationship_description_review',
    outcome:      'approved',
    occurredAt:   $timestamp
})
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:PROV_USED]->(rd)
RETURN rd.uri AS uri, rd.status AS status
"""

REJECT_RELATIONSHIP_DESCRIPTION_QUERY = """\
MATCH (rd:RelationshipDescription {uri: $desc_uri})
SET rd.status = 'rejected'
WITH rd
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (act:ProvActivity {
    uri:          'prov:activity:relationship-description-review:' + replace(rd.uri, 'relationship_description:', '') + ':' + $timestamp,
    activityType: 'relationship_description_review',
    outcome:      'rejected',
    occurredAt:   $timestamp
})
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:PROV_USED]->(rd)
FOREACH (_ IN CASE WHEN coalesce($detail, '') = '' THEN [] ELSE [1] END |
  CREATE (reason:ProvRejectionReason {
      uri:      'prov:reason:relationship-description:' + replace(rd.uri, 'relationship_description:', '') + ':' + $timestamp,
      category: coalesce($category, 'other'),
      detail:   $detail
  })
  CREATE (act)-[:HAS_REJECTION_REASON]->(reason)
)
RETURN rd.uri AS uri, rd.status AS status
"""

EDIT_RELATIONSHIP_DESCRIPTION_QUERY = """\
MATCH (from:Dataset)-[:HAS_RELATIONSHIP_DESCRIPTION]->(rd_old:RelationshipDescription {uri: $desc_uri})
      -[:DESCRIBES_REFERENCE_TO]->(to:Dataset)
SET rd_old.status = 'rejected',
    rd_old.isCurrent = false
CREATE (rd_new:RelationshipDescription {
    uri:                $new_uri,
    text:               $text,
    relationshipNature: $relationship_nature,
    fromDatasetUri:     from.uri,
    toDatasetUri:       to.uri,
    status:             'approved',
    isCurrent:          true,
    createdAt:          $timestamp
})
CREATE (from)-[:HAS_RELATIONSHIP_DESCRIPTION]->(rd_new)
CREATE (rd_new)-[:DESCRIBES_REFERENCE_TO]->(to)
CREATE (rd_new)-[:PROV_WAS_DERIVED_FROM]->(rd_old)
RETURN rd_new.uri AS uri
"""

ALL_OBSERVATION_RULES_QUERY_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_COLUMN]->(col:Column)
MATCH (col)<-[:ON_COLUMN]-(ps:PropertyShape)
WHERE coalesce(ps.ruleSource, 'observation') = 'observation'
RETURN
    ds.schema      AS schema,
    ds.name        AS table_name,
    col.uri        AS col_uri,
    col.name       AS col_name,
    col.dataType   AS data_type,
    ps.uri         AS rule_uri,
    ps.ruleType    AS rule_type,
    ps.severity    AS severity,
    ps.description AS description,
    ps.confidence  AS confidence,
    coalesce(ps.status, 'pending_review') AS status
ORDER BY ds.schema, ds.name, col.ordinal, ps.ruleType
"""

# Re-open: flip an approved/rejected item back to pending_review so the
# PO can change it. PROV-O activity records who reopened and when, so the
# audit trail still tells the full story (this isn't a destructive undo).
REOPEN_NAME_QUERY = """\
MATCH (col:Column {uri: $col_uri})
SET col.recommendedNameStatus = 'pending_review'
WITH col
CREATE (act:ProvActivity {
    uri:          'prov:activity:' + replace(col.uri, 'column:', '') + ':name-reopen:' + $timestamp,
    activityType: 'name_review',
    outcome:      'reopened',
    occurredAt:   $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:PROV_USED]->(col)
RETURN col.recommendedNameStatus AS status
"""

REOPEN_DESCRIPTION_QUERY = """\
MATCH (cd:ColumnDescription {uri: $desc_uri})
SET cd.status = 'pending_review'
WITH cd
CREATE (act:ProvActivity {
    uri:          'prov:activity:' + replace(cd.uri, 'description:', '') + ':reopen:' + $timestamp,
    activityType: 'review',
    outcome:      'reopened',
    occurredAt:   $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:PROV_USED]->(cd)
RETURN cd.status AS status
"""

REOPEN_RULE_QUERY = """\
MATCH (ps:PropertyShape {uri: $rule_uri})
SET ps.status = 'pending_review'
WITH ps
CREATE (act:ProvActivity {
    activityType: 'rule_review',
    outcome:      'reopened',
    occurredAt:   $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (ps)-[:PROV_WAS_GENERATED_BY]->(act)
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
RETURN ps.status AS status
"""

APPROVE_NAME_QUERY = """\
MATCH (col:Column {uri: $col_uri})
SET col.recommendedNameStatus = 'approved'
WITH col
CREATE (act:ProvActivity {
    uri:          'prov:activity:' + replace(col.uri, 'column:', '') + ':name-review:' + $timestamp,
    activityType: 'name_review',
    outcome:      'approved',
    quality:      coalesce($quality, 2),
    occurredAt:   $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:PROV_USED]->(col)
RETURN col.recommendedNameStatus AS status
"""

# Edit = override the recommendedName with the PO's preferred value, then
# mark approved. Same node, no derivation chain (the recommendation was
# never "real" — it was a suggestion).
EDIT_NAME_QUERY = """\
MATCH (col:Column {uri: $col_uri})
SET col.recommendedName = $new_name,
    col.recommendedNameStatus = 'approved'
WITH col
CREATE (act:ProvActivity {
    uri:          'prov:activity:' + replace(col.uri, 'column:', '') + ':name-review:' + $timestamp,
    activityType: 'name_review',
    outcome:      'edited',
    occurredAt:   $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:PROV_USED]->(col)
RETURN col.recommendedName AS final_name
"""

# Reject = clear the recommendation; downstream synthesis will use the
# original col.name when assembling the ODCS spec. Mirrors
# REJECT_DESCRIPTION_QUERY: also creates a :ProvRejectionReason node so
# the audit trail captures category + free-text detail.
REJECT_NAME_QUERY = """\
MATCH (col:Column {uri: $col_uri})
SET col.recommendedNameStatus = 'rejected'
WITH col
CREATE (act:ProvActivity {
    uri:          'prov:activity:' + replace(col.uri, 'column:', '') + ':name-review:' + $timestamp,
    activityType: 'name_review',
    outcome:      'rejected',
    occurredAt:   $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:PROV_USED]->(col)
CREATE (reason:ProvRejectionReason {
    uri:      'prov:reason:name:' + replace(col.uri, 'column:', '') + ':' + $timestamp,
    category: coalesce($category, 'other'),
    detail:   coalesce($detail, '')
})
CREATE (act)-[:HAS_REJECTION_REASON]->(reason)
RETURN col.recommendedNameStatus AS status
"""

# Edit a description in-place (PO inline edit). Mirrors the structure of
# REJECT_DESCRIPTION_QUERY but flips outcome to 'edited' and keeps the
# original as superseded.
EDIT_DESCRIPTION_QUERY = """\
MATCH (col:Column {uri: $col_uri})
MATCH (orig:ColumnDescription {uri: $desc_uri})
SET orig.status = 'rejected', orig.isCurrent = false
WITH col, orig
CREATE (new:ColumnDescription {
    uri:       'description:' + replace(col.uri, 'column:', '') + ':' + $timestamp,
    text:      $new_text,
    status:    'approved',
    isCurrent: true
})
CREATE (col)-[:HAS_DESCRIPTION]->(new)
CREATE (new)-[:PROV_WAS_DERIVED_FROM]->(orig)
WITH orig, new
CREATE (act:ProvActivity {
    uri:          'prov:activity:' + replace(orig.uri, 'description:', '') + ':edit:' + $timestamp,
    activityType: 'review',
    outcome:      'edited',
    occurredAt:   $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:PROV_USED]->(orig)
CREATE (new)-[:PROV_WAS_GENERATED_BY]->(act)
RETURN new.text AS final_text
"""

# Edit a rule's description / severity in-place. Light-touch — keeps the
# same :PropertyShape node and stamps an activity.
# Phase 8.3: capture priors so severity tightening (warning → error,
# which the classifier flags as breaking) preserves the prior value in
# the audit chain. Without this, "someone changed the severity" was
# observable but "what it used to be" was lost.
EDIT_OBSERVATION_RULE_QUERY = """\
MATCH (ps:PropertyShape {uri: $rule_uri})
WITH ps,
     coalesce(ps.description, '') AS prior_description,
     coalesce(ps.severity, '')    AS prior_severity
SET ps.description = coalesce($description, ps.description),
    ps.severity    = coalesce($severity,    ps.severity),
    ps.status      = 'approved'
WITH ps, prior_description, prior_severity
CREATE (act:ProvActivity {
    uri:              'prov:activity:rule-edit:' + replace(ps.uri, 'shape:', '') + ':' + $timestamp,
    activityType:     'rule_edit',
    outcome:          'edited',
    priorDescription: prior_description,
    priorSeverity:    prior_severity,
    newDescription:   coalesce($description, prior_description),
    newSeverity:      coalesce($severity, prior_severity),
    occurredAt:       $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + $reviewer})
ON CREATE SET agent.agentType = 'human', agent.name = $reviewer
CREATE (ps)-[:PROV_WAS_GENERATED_BY]->(act)
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:PROV_USED]->(ps)
RETURN ps.status AS status
"""


class SourceProductValidationAction(BaseModel):
    # action: approve_name | reject_name | edit_name |
    #         approve_description | reject_description | edit_description |
    #         approve_rule | reject_rule | edit_rule |
    #         approve_table | reject_table | edit_table
    action: str
    col_uri: Optional[str] = None
    desc_uri: Optional[str] = None
    rule_uri: Optional[str] = None
    new_name: Optional[str] = None
    new_text: Optional[str] = None
    description: Optional[str] = None
    severity: Optional[str] = None
    category: Optional[str] = None
    detail: Optional[str] = None
    quality: Optional[int] = None
    corrected_text: Optional[str] = None
    # Table-description specific (Tables tab):
    relationship_kind: Optional[str] = None
    # Relationship-description specific (Relationships tab). Carries the
    # PO's choice of nature when editing — same enum as the skill's
    # generator (belongs_to / categorises / audit_log_for / references).
    relationship_nature: Optional[str] = None
    reviewer: str = "workbench-user"


@router.get("/source_product_validation")
def get_source_product_validation(
    project_id: int,
    include_approved: bool = QParam(False, description="When true, also include approved/rejected items so the PO can re-edit a published SA product."),
    session: Session = Depends(get_session),
):
    """Combined validation surface for dpe-sa: names + descriptions + rules.

    Default (``include_approved=false``) returns only pending items —
    matches the original gate semantics. When ``include_approved=true``,
    returns ALL items with their current status so the edit-mode UI on a
    published SA product can render approved items with a re-edit button.
    """
    project = _get_project(project_id, session)
    scoped = has_project_node(project)
    pc = {"project_code": project.project_code} if scoped else {}

    if include_approved and scoped:
        names_q = ALL_NAMES_QUERY_S
        descs_q = ALL_DESCRIPTIONS_QUERY_S
        rules_q = ALL_OBSERVATION_RULES_QUERY_S
        tables_q = ALL_TABLE_DESCRIPTIONS_QUERY_S
        rels_q = ALL_RELATIONSHIP_DESCRIPTIONS_QUERY_S
    else:
        names_q = PENDING_NAMES_QUERY_S if scoped else PENDING_NAMES_QUERY
        descs_q = PENDING_DESCRIPTIONS_QUERY_S if scoped else PENDING_DESCRIPTIONS_QUERY
        rules_q = PENDING_OBSERVATION_RULES_QUERY_S if scoped else PENDING_OBSERVATION_RULES_QUERY
        # Table + relationship descriptions are project-scoped via
        # :Project → :Catalog → :Dataset; only the scoped variant has a
        # meaningful unscoped fallback (returns empty for non-scoped
        # projects).
        tables_q = PENDING_TABLE_DESCRIPTIONS_QUERY_S if scoped else None
        rels_q = PENDING_RELATIONSHIP_DESCRIPTIONS_QUERY_S if scoped else None

    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        column_names = [dict(r) for r in ns.run(names_q, **pc)]
        descriptions = [dict(r) for r in ns.run(descs_q, **pc)]
        rules = [dict(r) for r in ns.run(rules_q, **pc)]
        table_descriptions = (
            [dict(r) for r in ns.run(tables_q, **pc)] if tables_q else []
        )
        relationship_descriptions = (
            [dict(r) for r in ns.run(rels_q, **pc)] if rels_q else []
        )

    # If the buckets are already empty when the panel loads (e.g. PO
    # pre-approved everything before the gate was opened, or they're
    # revisiting after a previous session cleared the queue), nudge the
    # gate to complete so downstream engineer stages aren't left waiting.
    # Idempotent — does nothing when items still exist.
    _check_review_complete(project, "source_product_validation", session)

    return {
        "column_names": column_names,
        "descriptions": descriptions,
        "rules": rules,
        "table_descriptions": table_descriptions,
        "relationship_descriptions": relationship_descriptions,
        "count": (
            len(column_names) + len(descriptions) + len(rules)
            + len(table_descriptions) + len(relationship_descriptions)
        ),
    }


@router.post("/source_product_validation")
def review_source_product(
    project_id: int,
    body: SourceProductValidationAction,
    session: Session = Depends(get_session),
    _user=Depends(require_review("source_product_validation")),
):
    project = _get_project_for_write(project_id, session)
    ts = _ts()
    a = body.action

    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        if a == "approve_name":
            if not body.col_uri:
                raise HTTPException(400, "col_uri required")
            ns.run(
                APPROVE_NAME_QUERY,
                col_uri=body.col_uri,
                reviewer=body.reviewer,
                quality=body.quality or 2,
                timestamp=ts,
            )
        elif a == "reject_name":
            if not body.col_uri:
                raise HTTPException(400, "col_uri required")
            ns.run(
                REJECT_NAME_QUERY,
                col_uri=body.col_uri,
                reviewer=body.reviewer,
                category=body.category or "other",
                detail=body.detail or "",
                timestamp=ts,
            )
        elif a == "edit_name":
            if not body.col_uri or not body.new_name:
                raise HTTPException(400, "col_uri and new_name required")
            ns.run(
                EDIT_NAME_QUERY,
                col_uri=body.col_uri,
                new_name=body.new_name.strip(),
                reviewer=body.reviewer,
                timestamp=ts,
            )
        elif a == "approve_description":
            if not body.desc_uri:
                raise HTTPException(400, "desc_uri required")
            ns.run(APPROVE_DESCRIPTION_QUERY, desc_uri=body.desc_uri, reviewer=body.reviewer, timestamp=ts, quality=body.quality or 2)
        elif a == "reject_description":
            if not body.desc_uri or not body.col_uri or not body.corrected_text:
                raise HTTPException(400, "desc_uri, col_uri, and corrected_text required")
            ns.run(
                REJECT_DESCRIPTION_QUERY,
                desc_uri=body.desc_uri,
                col_uri=body.col_uri,
                corrected_text=body.corrected_text,
                category=body.category or "other",
                detail=body.detail or "",
                reviewer=body.reviewer,
                timestamp=ts,
            )
        elif a == "edit_description":
            if not body.desc_uri or not body.col_uri or not body.new_text:
                raise HTTPException(400, "desc_uri, col_uri, and new_text required")
            ns.run(
                EDIT_DESCRIPTION_QUERY,
                desc_uri=body.desc_uri,
                col_uri=body.col_uri,
                new_text=body.new_text,
                reviewer=body.reviewer,
                timestamp=ts,
            )
        elif a == "approve_rule":
            if not body.rule_uri:
                raise HTTPException(400, "rule_uri required")
            ns.run(
                APPROVE_DOMAIN_RULE_QUERY,
                rule_uri=body.rule_uri,
                quality=body.quality or 2,
                reviewer=body.reviewer,
                timestamp=ts,
            )
        elif a == "reject_rule":
            if not body.rule_uri:
                raise HTTPException(400, "rule_uri required")
            ns.run(
                REJECT_DOMAIN_RULE_QUERY,
                rule_uri=body.rule_uri,
                category=body.category or "other",
                detail=body.detail or "",
                reviewer=body.reviewer,
                timestamp=ts,
            )
        elif a == "edit_rule":
            if not body.rule_uri:
                raise HTTPException(400, "rule_uri required")
            ns.run(
                EDIT_OBSERVATION_RULE_QUERY,
                rule_uri=body.rule_uri,
                description=body.description,
                severity=body.severity,
                reviewer=body.reviewer,
                timestamp=ts,
            )
        elif a == "reopen_name":
            if not body.col_uri:
                raise HTTPException(400, "col_uri required")
            ns.run(REOPEN_NAME_QUERY, col_uri=body.col_uri, reviewer=body.reviewer, timestamp=ts)
        elif a == "reopen_description":
            if not body.desc_uri:
                raise HTTPException(400, "desc_uri required")
            ns.run(REOPEN_DESCRIPTION_QUERY, desc_uri=body.desc_uri, reviewer=body.reviewer, timestamp=ts)
        elif a == "reopen_rule":
            if not body.rule_uri:
                raise HTTPException(400, "rule_uri required")
            ns.run(REOPEN_RULE_QUERY, rule_uri=body.rule_uri, reviewer=body.reviewer, timestamp=ts)
        elif a == "approve_table":
            if not body.desc_uri:
                raise HTTPException(400, "desc_uri required")
            ns.run(
                APPROVE_TABLE_DESCRIPTION_QUERY,
                desc_uri=body.desc_uri,
                reviewer=body.reviewer,
                timestamp=ts,
            )
        elif a == "reject_table":
            if not body.desc_uri:
                raise HTTPException(400, "desc_uri required")
            ns.run(
                REJECT_TABLE_DESCRIPTION_QUERY,
                desc_uri=body.desc_uri,
                reviewer=body.reviewer,
                timestamp=ts,
                category=body.category,
                detail=body.detail,
            )
        elif a == "edit_table":
            if not body.desc_uri or not body.new_text:
                raise HTTPException(400, "desc_uri and new_text required")
            new_uri = f"{body.desc_uri}:edit:{ts}"
            ns.run(
                EDIT_TABLE_DESCRIPTION_QUERY,
                desc_uri=body.desc_uri,
                new_uri=new_uri,
                text=body.new_text,
                relationship_kind=(body.relationship_kind or "unknown").lower(),
                timestamp=ts,
            )
        elif a == "approve_relationship":
            if not body.desc_uri:
                raise HTTPException(400, "desc_uri required")
            ns.run(
                APPROVE_RELATIONSHIP_DESCRIPTION_QUERY,
                desc_uri=body.desc_uri,
                reviewer=body.reviewer,
                timestamp=ts,
            )
        elif a == "reject_relationship":
            if not body.desc_uri:
                raise HTTPException(400, "desc_uri required")
            ns.run(
                REJECT_RELATIONSHIP_DESCRIPTION_QUERY,
                desc_uri=body.desc_uri,
                reviewer=body.reviewer,
                timestamp=ts,
                category=body.category,
                detail=body.detail,
            )
        elif a == "edit_relationship":
            if not body.desc_uri or not body.new_text:
                raise HTTPException(400, "desc_uri and new_text required")
            new_uri = f"{body.desc_uri}:edit:{ts}"
            ns.run(
                EDIT_RELATIONSHIP_DESCRIPTION_QUERY,
                desc_uri=body.desc_uri,
                new_uri=new_uri,
                text=body.new_text,
                relationship_nature=(body.relationship_nature or "references").lower(),
                timestamp=ts,
            )
        else:
            raise HTTPException(400, f"Unknown action: {a}")

    _check_review_complete(project, "source_product_validation", session)
    return {"status": "ok"}


# Bucket → (payload key, per-item approve action, uri field). Drives "Validate all".
_SPV_BULK = {
    "names":         ("column_names", "approve_name", "col_uri"),
    "descriptions":  ("descriptions", "approve_description", "desc_uri"),
    "tables":        ("table_descriptions", "approve_table", "desc_uri"),
    "relationships": ("relationship_descriptions", "approve_relationship", "desc_uri"),
    "rules":         ("rules", "approve_rule", "rule_uri"),
}


class SourceProductValidationBulkApprove(BaseModel):
    tab: str = "all"   # names | descriptions | tables | relationships | rules | all
    tabs: Optional[list[str]] = None   # explicit category list (bulk-validate modal)
    quality: int = 2   # 1=Acceptable, 2=Good, 3=Excellent
    reviewer: str = "workbench-user"


@router.post("/source_product_validation/approve-all")
def approve_all_source_product(
    project_id: int,
    body: SourceProductValidationBulkApprove | None = None,
    session: Session = Depends(get_session),
    _user=Depends(require_review("source_product_validation")),
):
    """PO "Validate all" — approve every pending item in one bucket (or all
    buckets). Loops the existing per-item `review_source_product` handler so the
    PROV-O provenance + review-complete lifecycle are identical to approving each
    by hand — no special-cased shortcut that skips the audit trail."""
    project = _get_project_for_write(project_id, session)
    body = body or SourceProductValidationBulkApprove()
    if body.tabs is not None:
        tabs = body.tabs
    else:
        tabs = list(_SPV_BULK) if body.tab == "all" else [body.tab]
    bad = [t for t in tabs if t not in _SPV_BULK]
    if bad:
        raise HTTPException(400, f"unknown tab(s): {bad}")

    # NOTE: keyword args — get_source_product_validation's 2nd positional param is
    # `include_approved`, not `session`. Passing positionally silently fetched
    # approved items AND left `session` unbound (the batch-validate 500 bug).
    items = get_source_product_validation(project_id, include_approved=False, session=session)
    approved: dict[str, int] = {}
    for t in tabs:
        bucket_key, action, uri_field = _SPV_BULK[t]
        n = 0
        for it in items.get(bucket_key, []):
            uri = it.get(uri_field)
            if not uri:
                continue
            try:
                review_source_product(
                    project_id,
                    SourceProductValidationAction(
                        action=action, quality=body.quality, reviewer=body.reviewer,
                        **{uri_field: uri},
                    ),
                    session,
                )
                n += 1
            except HTTPException:
                # Item changed since the snapshot (already actioned) — skip it.
                continue
        approved[t] = n
    return {"status": "ok", "approved": approved}


# ── Legacy endpoint for simple stage-level review status ────────────────────

@router.get("")
def get_pending_reviews(project_id: int, session: Session = Depends(get_session)):
    """Return stages that are awaiting review."""
    project = _get_project(project_id, session)
    stages = session.exec(
        select(StageRun).where(
            StageRun.project_id == project_id,
            StageRun.status == StageStatus.awaiting_review,
        )
    ).all()
    return [
        {
            "id": s.id,
            "stage_number": s.stage_number,
            "stage_name": s.stage_name,
            "status": s.status,
        }
        for s in stages
    ]


# ── Helpers ─────────────────────────────────────────────────────────────────

# Downstream stages that depend on the data_mapping output. A
# post-approval edit to any :ColumnMapping invalidates these, so the
# replace_mapping handler resets them after a successful edit. Same set
# as routers/edits.py's _change_type_to_actions uses for upstream
# pushback reconciliation — kept in sync intentionally; if you add a
# new downstream stage, update both.
_MAPPING_DOWNSTREAM_STAGES = (
    "serving_virtual_view",
    "deploy_virtual_view",
    "deployment_reflection",
    "mark_engineering_complete",
)


def _reset_downstream_mapping_stages(project: Project, session: Session) -> list[dict]:
    """Reset any of ``_MAPPING_DOWNSTREAM_STAGES`` that are currently
    `complete` to `pending`. Inlines the reset logic from
    apply_reconciliation in routers/edits.py (rather than an HTTP
    self-call) so the cascade is atomic with the mapping edit.

    Returns a list of ``{workflow_id, stage_number, stage_id}`` entries
    naming the stages that were actually reset. Stages already pending
    are skipped; stages not present in any workflow are silently
    ignored. Idempotent — re-calling is a no-op.
    """
    import json as _json_mod
    target_ids = set(_MAPPING_DOWNSTREAM_STAGES)
    candidates: list[tuple[str | None, int, str]] = []
    if project.multi_workflow:
        wfs = session.exec(select(Workflow).where(Workflow.project_id == project.id)).all()
        for wf in wfs:
            if not wf.workflow_json:
                continue
            stages = [s for s in _json_mod.loads(wf.workflow_json) if s.get("enabled", True)]
            for idx, s in enumerate(stages, start=1):
                sid = s.get("stage_id")
                if sid in target_ids:
                    candidates.append((wf.workflow_id, idx, sid))
    elif project.workflow_json:
        stages = [s for s in _json_mod.loads(project.workflow_json) if s.get("enabled", True)]
        for idx, s in enumerate(stages, start=1):
            sid = s.get("stage_id")
            if sid in target_ids:
                candidates.append((None, idx, sid))

    reset_log: list[dict] = []
    for workflow_id, stage_number, stage_id in candidates:
        q = select(StageRun).where(
            StageRun.project_id == project.id,
            StageRun.stage_number == stage_number,
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
        reset_log.append({
            "workflow_id": workflow_id,
            "stage_number": stage_number,
            "stage_id": stage_id,
        })
    if reset_log:
        session.commit()
    return reset_log


def _check_review_complete(project: Project, review_type: str, session: Session):
    """If no pending items remain, mark the stage as complete."""
    # Check remaining pending items in Neo4j
    remaining = 0
    scoped = has_project_node(project)
    pc = {"project_code": project.project_code} if scoped else {}
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        if review_type == "descriptions":
            if scoped:
                q = (
                    f"{_PRJ_DS}(:Dataset)-[:HAS_COLUMN]->(:Column)"
                    "-[:HAS_DESCRIPTION]->(cd:ColumnDescription) "
                    "WHERE cd.status = 'pending_review' AND cd.isCurrent = true "
                    "RETURN count(cd) AS cnt"
                )
            else:
                q = "MATCH (cd:ColumnDescription) WHERE cd.status = 'pending_review' AND cd.isCurrent = true RETURN count(cd) AS cnt"
            remaining = ns.run(q, **pc).single()["cnt"]
        elif review_type == "mappings":
            if scoped:
                # Anchor on the project's own product columns rather than the
                # catalog source path. The catalog-only walk misses
                # consumer-aligned (:DProdColumn-source) and literal mappings,
                # so for dpe-cf projects it returned 0 — auto-flipping
                # Data Mapping to complete on the first approval even when
                # other mappings were still pending_review. Matches the shape
                # of summary.py:MAPPING_STATS_S.
                q = (
                    "MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn) "
                    "WHERE cm.status = 'pending_review' AND cm.isCurrent = true "
                    "  AND pc.uri STARTS WITH 'dprod:col:' + $project_code + '-contract:' "
                    "RETURN count(cm) AS cnt"
                )
            else:
                q = "MATCH (cm:ColumnMapping) WHERE cm.status = 'pending_review' AND cm.isCurrent = true RETURN count(cm) AS cnt"
            remaining = ns.run(q, **pc).single()["cnt"]
        elif review_type == "domain_rules":
            if scoped:
                q = (
                    f"{_PRJ_DS}(:Dataset)-[:HAS_SHAPE]->(:NodeShape)"
                    "-[:PROPERTY]->(ps:PropertyShape) "
                    "WHERE ps.ruleSource = 'domain' AND ps.status = 'pending_review' "
                    "RETURN count(DISTINCT ps) AS cnt "
                    "UNION "
                    "MATCH (pc:DProdColumn)<-[:ON_DPROD_COLUMN]-(ps:PropertyShape) "
                    "WHERE pc.uri STARTS WITH 'dprod:col:' + $project_code + '-contract:' "
                    "  AND ps.ruleSource = 'domain' AND ps.status = 'pending_review' "
                    "RETURN count(DISTINCT ps) AS cnt"
                )
            else:
                q = "MATCH (ps:PropertyShape) WHERE ps.ruleSource = 'domain' AND ps.status = 'pending_review' RETURN count(DISTINCT ps) AS cnt"
            remaining = sum(r["cnt"] for r in ns.run(q, **pc))
        elif review_type == "source_product_validation":
            # Stage stays awaiting_review until ALL FIVE PO buckets are empty:
            # column names, column descriptions, observation rules, TABLE
            # descriptions, and RELATIONSHIP descriptions. (The table/relationship
            # buckets were previously omitted, so the gate — and the MCP "validation
            # done" signal — could report complete while the PO still had table /
            # relationship descriptions pending in the UI.)
            if scoped:
                names_q = (
                    f"{_PRJ_DS}(:Dataset)-[:HAS_COLUMN]->(col:Column) "
                    "WHERE col.recommendedName IS NOT NULL AND col.recommendedName <> '' "
                    "  AND coalesce(col.recommendedNameStatus, 'pending_review') = 'pending_review' "
                    "RETURN count(col) AS cnt"
                )
                descs_q = (
                    f"{_PRJ_DS}(:Dataset)-[:HAS_COLUMN]->(:Column)"
                    "-[:HAS_DESCRIPTION]->(cd:ColumnDescription) "
                    "WHERE cd.status = 'pending_review' AND cd.isCurrent = true "
                    "RETURN count(cd) AS cnt"
                )
                rules_q = (
                    f"{_PRJ_DS}(:Dataset)-[:HAS_COLUMN]->(col:Column) "
                    "MATCH (col)<-[:ON_COLUMN]-(ps:PropertyShape) "
                    "WHERE coalesce(ps.ruleSource, 'observation') = 'observation' "
                    "  AND ps.status = 'pending_review' "
                    "RETURN count(ps) AS cnt"
                )
                tables_q = (
                    f"{_PRJ_DS}(:Dataset)-[:HAS_TABLE_DESCRIPTION]->(td:TableDescription) "
                    "WHERE td.isCurrent = true AND td.status = 'pending_review' "
                    "RETURN count(td) AS cnt"
                )
                rels_q = (
                    f"{_PRJ_DS}(:Dataset)-[:HAS_RELATIONSHIP_DESCRIPTION]->(rd:RelationshipDescription) "
                    "WHERE rd.isCurrent = true AND rd.status = 'pending_review' "
                    "RETURN count(rd) AS cnt"
                )
            else:
                names_q = (
                    "MATCH (col:Column) "
                    "WHERE col.recommendedName IS NOT NULL AND col.recommendedName <> '' "
                    "  AND coalesce(col.recommendedNameStatus, 'pending_review') = 'pending_review' "
                    "RETURN count(col) AS cnt"
                )
                descs_q = "MATCH (cd:ColumnDescription) WHERE cd.status = 'pending_review' AND cd.isCurrent = true RETURN count(cd) AS cnt"
                rules_q = (
                    "MATCH (ps:PropertyShape)-[:ON_COLUMN]->(:Column) "
                    "WHERE coalesce(ps.ruleSource, 'observation') = 'observation' "
                    "  AND ps.status = 'pending_review' "
                    "RETURN count(ps) AS cnt"
                )
                tables_q = "MATCH (td:TableDescription) WHERE td.isCurrent = true AND td.status = 'pending_review' RETURN count(td) AS cnt"
                rels_q = "MATCH (rd:RelationshipDescription) WHERE rd.isCurrent = true AND rd.status = 'pending_review' RETURN count(rd) AS cnt"
            remaining = (
                ns.run(names_q, **pc).single()["cnt"]
                + ns.run(descs_q, **pc).single()["cnt"]
                + ns.run(rules_q, **pc).single()["cnt"]
                + ns.run(tables_q, **pc).single()["cnt"]
                + ns.run(rels_q, **pc).single()["cnt"]
            )

    if remaining == 0:
        # Find stages that own this review type. We accept three statuses:
        #   - awaiting_review: the normal path — stage was kicked off, the
        #     skill produced review items, the user has now cleared them.
        #   - failed: the WebSocket may have dropped while items still
        #     existed; on retry the count check picks them up.
        #   - pending: relevant for PO-only stages like po_source_validation
        #     that have no "Run" affordance (the engineer can't run them,
        #     the PO has no run button — they just visit the panel). The
        #     gate stays pending for its whole lifetime; we still need to
        #     flip it to complete when the underlying counts hit zero so
        #     downstream engineer stages can proceed.
        awaiting_stages = session.exec(
            select(StageRun).where(
                StageRun.project_id == project.id,
                StageRun.status.in_([
                    StageStatus.awaiting_review,
                    StageStatus.failed,
                    StageStatus.pending,
                ]),
            )
        ).all()
        for stage_run in awaiting_stages:
            from ..archetypes import STAGE_REGISTRY
            import json
            stage_id = None
            # Multi-workflow: resolve from Workflow table
            if stage_run.workflow_id and project.multi_workflow:
                from ..models import Workflow as WfModel
                wf = session.exec(
                    select(WfModel).where(
                        WfModel.project_id == project.id,
                        WfModel.workflow_id == stage_run.workflow_id,
                    )
                ).first()
                if wf and wf.workflow_json:
                    wf_stages = [s for s in json.loads(wf.workflow_json) if s.get("enabled", True)]
                    if stage_run.stage_number <= len(wf_stages):
                        stage_id = wf_stages[stage_run.stage_number - 1].get("stage_id")
            elif project.workflow_json:
                workflow = [s for s in json.loads(project.workflow_json) if s.get("enabled", True)]
                if stage_run.stage_number <= len(workflow):
                    stage_id = workflow[stage_run.stage_number - 1].get("stage_id")
            stage_def = STAGE_REGISTRY.get(stage_id, {}) if stage_id else {}
            if not stage_def:
                from ..pipeline import STAGES
                stage_def = next((s for s in STAGES if s.get("number") == stage_run.stage_number), {})
            if stage_def.get("review_type") == review_type:
                stage_run.status = StageStatus.complete
                stage_run.completed_at = datetime.now(timezone.utc)
                session.add(stage_run)
        session.commit()



# ── Phase 3: Stale-mapping detection + rebind suggestions ─────────────────
#
# When an upstream source product rebuilds its dprod chain (via DPROD_WIPE
# inside _generate_dprod), the cross-project :MAPS_SOURCE_COLUMN edges from
# this consumer's :ColumnMapping nodes to the source's :DProdColumn nodes
# are deleted. The :ColumnMapping node itself survives but is orphaned —
# its source link is gone. _generate_dprod captures the prior source URI
# onto the mapping (priorSourceColumnUri / priorSourceColumnName /
# priorSourceColumnSchema) before the wipe so this endpoint can:
#
#   (a) surface every orphaned mapping in the consumer's project, and
#   (b) for renames, suggest the new :DProdColumn whose `renamedFromUri`
#       matches the captured prior URI.
#
# The engineer then chooses to rebind or reject. Approving rebinds re-creates
# the :MAPS_SOURCE_COLUMN edge and clears the cached "prior" properties.

_STALE_MAPPINGS_QUERY = """\
MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
WHERE pc.uri STARTS WITH 'dprod:col:' + $project_code + '-contract:'
  AND cm.isCurrent = true
  AND cm.priorSourceColumnUri IS NOT NULL
  AND NOT EXISTS { MATCH (cm)-[:MAPS_SOURCE_COLUMN]->() }
OPTIONAL MATCH (suggested:DProdColumn {renamedFromUri: cm.priorSourceColumnUri})
RETURN
    cm.uri                       AS mapping_uri,
    cm.status                    AS mapping_status,
    pc.name                      AS product_column_name,
    pc.uri                       AS product_column_uri,
    coalesce(pc.datasetPhysicalName, '') AS product_dataset,
    cm.priorSourceColumnUri      AS prior_source_uri,
    coalesce(cm.priorSourceColumnName, '')   AS prior_source_name,
    coalesce(cm.priorSourceColumnSchema, '') AS prior_source_schema,
    suggested.uri                AS suggested_source_uri,
    coalesce(suggested.name, '') AS suggested_source_name,
    coalesce(suggested.datasetPhysicalName, '') AS suggested_source_schema
ORDER BY product_dataset, product_column_name
"""


# Phase 7: deactivated-mapping view with PROV-O lifecycle chain. Returns
# :ColumnMapping rows that have been logically pruned (isCurrent=false)
# along with their full lifecycle event chain (mapping_review, mapping_
# deactivated, mapping_reactivated activities) ordered by occurredAt.
_DEACTIVATED_MAPPINGS_QUERY = """\
MATCH (cm:ColumnMapping)
WHERE cm.isCurrent = false
  AND cm.priorProductColumnUri IS NOT NULL
  AND cm.priorProductColumnUri STARTS WITH 'dprod:col:' + $project_code + '-contract:'
OPTIONAL MATCH (cm)<-[:PROV_USED]-(act:ProvActivity)
OPTIONAL MATCH (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent:ProvAgent)
WITH cm, act, agent ORDER BY act.occurredAt ASC
WITH cm, collect({
    activity_type: act.activityType,
    reason: act.reason,
    contract_version: act.contractVersion,
    occurred_at: toString(act.occurredAt),
    actor: coalesce(agent.name, '')
}) AS events
RETURN
    cm.uri                                       AS mapping_uri,
    cm.status                                    AS mapping_status,
    coalesce(cm.priorProductColumnUri, '')       AS prior_product_uri,
    coalesce(cm.priorProductColumnName, '')      AS prior_product_name,
    coalesce(cm.priorProductColumnSchema, '')    AS prior_product_schema,
    [e IN events WHERE e.activity_type IS NOT NULL] AS lifecycle_events
ORDER BY prior_product_schema, prior_product_name
"""


@router.get("/stale_mappings")
def get_stale_mappings(
    project_id: int,
    include_deactivated: bool = False,
    session: Session = Depends(get_session),
):
    """Return :ColumnMappings whose source link was wiped by an upstream
    source's DPROD rebuild, plus rebind suggestions when the new source
    column carries a renamedFromUri matching the captured prior URI.

    Phase 7: ``include_deactivated=true`` also returns logically-pruned
    mappings (isCurrent=false) with their PROV-O lifecycle event chain.
    Used by MappingReviewPanel's "Show deactivated mappings" toggle for
    audit / history review.
    """
    project = _get_project(project_id, session)
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        rows = [dict(r) for r in ns.run(_STALE_MAPPINGS_QUERY, project_code=project.project_code)]
        deactivated: list[dict] = []
        if include_deactivated:
            deactivated = [dict(r) for r in ns.run(_DEACTIVATED_MAPPINGS_QUERY, project_code=project.project_code)]
    return {
        "items": rows,
        "count": len(rows),
        "with_suggestion": sum(1 for r in rows if r.get("suggested_source_uri")),
        "deactivated": deactivated,
        "deactivated_count": len(deactivated),
    }


_REBIND_MAPPING = """\
MATCH (cm:ColumnMapping {uri: $mapping_uri})
MATCH (new_src:DProdColumn {uri: $new_source_uri})
MERGE (cm)-[:MAPS_SOURCE_COLUMN]->(new_src)
REMOVE cm.priorSourceColumnUri,
       cm.priorSourceColumnName,
       cm.priorSourceColumnSchema
SET cm.status = 'pending_review',
    cm.rebindNote = $rebind_note,
    cm.reboundAt = datetime()
RETURN cm.uri AS mapping_uri
"""


class StaleMappingRebindInput(BaseModel):
    mapping_uri: str
    new_source_uri: str
    rebind_note: Optional[str] = None


@router.post("/stale_mappings/rebind")
def rebind_stale_mapping(
    project_id: int,
    body: StaleMappingRebindInput,
    session: Session = Depends(get_session),
):
    """Rebind a stale mapping to a new source column. The engineer is
    accepting the rename suggestion (or manually re-pointing). After
    rebind the mapping reverts to status='pending_review' so a reviewer
    can confirm the new source is semantically equivalent.

    Phase 8.1: emits mapping_source_rebound PROV activity so the audit
    chain captures the new source URI. The prior URI is recoverable from
    the cached cm.priorSourceColumnUri before the REMOVE clears it.
    """
    project = _get_project_for_write(project_id, session)
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        # Read the cached prior source URI before REBIND_MAPPING clears it.
        prior_row = ns.run(
            "MATCH (cm:ColumnMapping {uri: $uri}) RETURN coalesce(cm.priorSourceColumnUri, '') AS prior",
            uri=body.mapping_uri,
        ).single()
        prior_uri = prior_row["prior"] if prior_row else ""
        prior_uris = [prior_uri] if prior_uri else []

        row = ns.run(
            _REBIND_MAPPING,
            mapping_uri=body.mapping_uri,
            new_source_uri=body.new_source_uri,
            rebind_note=body.rebind_note or "",
        ).single()
        if not row:
            raise HTTPException(404, f"Mapping {body.mapping_uri} or source column not found")
        emit_mapping_source_rebound(
            ns,
            mapping_uri=body.mapping_uri,
            prior_uris=prior_uris,
            new_uris=[body.new_source_uri],
            reason="rebind_stale",
            actor=None,  # rebind UI doesn't carry an explicit actor; default to system
        )
    return {"mapping_uri": row["mapping_uri"], "status": "rebound"}


# ── Phase 7: Logical-prune (deactivate) mappings whose product col was removed ──
#
# When a PO removes a product column in v(N+1), the corresponding :ColumnMapping
# rows pointing at the removed :DProdColumn are deactivated (isCurrent=false)
# instead of physically deleted, so the full lifecycle is reconstructible.
# Uses the same PROV-O pattern as the existing APPROVE/REJECT mapping queries:
# a :ProvActivity {activityType: 'mapping_deactivated'} sidecar with PROV_USED
# edge to the mapping + PROV_WAS_ASSOCIATED_WITH edge to the agent.
# Auto-reactivate (if the column is re-added later) lives in odcs.py's
# _REACTIVATE_REMATCHED_MAPPINGS — runs as part of _generate_dprod.

_DEACTIVATE_ORPHANED_PRODUCT_MAPPINGS = """\
// Match active mappings whose current :MAPS_TO_PRODUCT_COLUMN target is in
// the removed list. Reconciliation runs BEFORE odcs_to_dprod re-runs, so
// the v1 :DProdColumn nodes still exist and the edge is intact. We cache
// the URI on the mapping so the auto-reactivate path (after the engineer
// re-runs odcs_to_dprod, or if the column is re-added in a future version)
// can find this mapping again via priorProductColumnUri.
MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
WHERE cm.isCurrent = true
  AND pc.uri IN $removed_col_uris
SET cm.isCurrent = false,
    cm.priorProductColumnUri = pc.uri,
    cm.priorProductColumnName = coalesce(pc.name, ''),
    cm.priorProductColumnSchema = coalesce(pc.datasetPhysicalName, '')
WITH cm
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + coalesce($actor, 'system:reconciliation')})
  ON CREATE SET agent.agentType = CASE WHEN $actor IS NULL THEN 'system' ELSE 'human' END,
                agent.name = coalesce($actor, 'reconciliation')
CREATE (act:ProvActivity {
    uri:             'prov:activity:mapping-deactivated:' + replace(cm.uri, 'mapping:', '') + ':' + toString(timestamp()),
    activityType:    'mapping_deactivated',
    reason:          'product_column_removed_in_v' + toString($contract_version),
    contractVersion: $contract_version,
    occurredAt:      datetime()
})
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:PROV_USED]->(cm)
RETURN count(cm) AS deactivated, collect(cm.uri) AS deactivated_uris
"""


def deactivate_orphaned_product_mappings(
    project: Project,
    removed_col_uris: list[str],
    contract_version: int,
    actor: str | None = None,
) -> dict:
    """Deactivate :ColumnMapping rows whose target product column was removed.

    Returns ``{deactivated: int, deactivated_uris: list[str]}``. No-op when
    removed_col_uris is empty.
    """
    if not removed_col_uris:
        return {"deactivated": 0, "deactivated_uris": []}
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        row = ns.run(
            _DEACTIVATE_ORPHANED_PRODUCT_MAPPINGS,
            removed_col_uris=removed_col_uris,
            contract_version=contract_version,
            actor=actor,
        ).single()
    if not row:
        return {"deactivated": 0, "deactivated_uris": []}
    return {
        "deactivated": row["deactivated"] or 0,
        "deactivated_uris": list(row["deactivated_uris"] or []),
    }


# ── Per-mapping rationale report (log + on-disk artifacts, not persisted) ─
#
# Builds a user-friendly markdown summary for the current :ColumnMapping set.
# Per-mapping rationale comes from two sources:
#   1. The data-mapping skill's on-disk artifacts at
#      ``projects/<project_code>/metadata/mappings_<table>_<timestamp>.json``
#      which already carry a per-mapping ``rationale`` field plus
#      ``similarity_score`` / ``transform_author``.
#   2. Optionally enriched by a new ``data-mapping-rationale-summarizer``
#      skill (one LLM call per report). Heuristic fallback when the skill
#      isn't installed.
#
# Run highlights (the agent's table-batch narration) get pulled from the
# StageExecution log_json and rendered as a contextual block at the top.
# Nothing is written to Neo4j; re-running regenerates fresh markdown from
# whatever the on-disk artifacts + graph state currently say.


_RATIONALE_SUMMARIZER_SKILL = "data-mapping-rationale-summarizer"


def _find_mapping_stage_locator(
    project: Project, session: Session
) -> Optional[tuple[Optional[str], int]]:
    """Locate the (workflow_id, stage_number) of the data_mapping stage for
    this project. Mirrors the lookup pattern in
    ``routers/edits.py:apply_reconciliation``."""
    if project.multi_workflow:
        wfs = session.exec(
            select(Workflow).where(Workflow.project_id == project.id)
        ).all()
        for wf in wfs:
            if not wf.workflow_json:
                continue
            stages = [
                s for s in json.loads(wf.workflow_json) if s.get("enabled", True)
            ]
            for idx, s in enumerate(stages, start=1):
                if s.get("stage_id") == "data_mapping":
                    return (wf.workflow_id, idx)
    elif project.workflow_json:
        stages = [
            s for s in json.loads(project.workflow_json) if s.get("enabled", True)
        ]
        for idx, s in enumerate(stages, start=1):
            if s.get("stage_id") == "data_mapping":
                return (None, idx)
    return None


def _load_mapping_artifacts(project_code: str) -> dict:
    """Walk ``projects/<project_code>/metadata/`` for the data-mapping skill's
    on-disk artifacts and return two URI-indexed lookups:

      • ``mappings``  — keyed on ``(source_col_uri, product_col_uri)`` →
        ``{rationale, similarity_score, transform_kind, transform_author,
        transform_confidence}``. Sourced from ``mappings_<table>_*.json``.
      • ``candidates`` — keyed on ``product_col_uri`` → list of considered
        source columns from ``mapping_candidates_<table>_*.json``. Lets the
        summarizer name what the agent looked at but didn't pick.

    When the same table has multiple timestamped files (re-runs), the latest
    one wins (filename's ``YYYYMMDD_HHMMSS`` is the tiebreaker). Tolerates
    missing dirs / unparseable JSON without raising — returns empty lookups
    and lets the caller fall back to graph-only rationale.
    """
    from pathlib import Path
    from ..config import BASE_PROJECT_DIR
    meta_dir = BASE_PROJECT_DIR / project_code / "metadata"
    out: dict[str, dict] = {"mappings": {}, "candidates": {}, "files_used": []}
    if not meta_dir.is_dir():
        return out

    # Group files by table-stem; keep the latest timestamp per group.
    def _latest_per_table(prefix: str) -> dict[str, Path]:
        latest: dict[str, Path] = {}
        for p in meta_dir.glob(f"{prefix}_*.json"):
            stem = p.stem  # e.g. mappings_customer_20260519_130500
            # Strip prefix and trailing timestamp to derive the table name.
            tail = stem[len(prefix) + 1:]
            parts = tail.rsplit("_", 2)  # ['customer', '20260519', '130500']
            if len(parts) != 3:
                # Doesn't match the expected naming; skip.
                continue
            table_name = parts[0]
            existing = latest.get(table_name)
            if existing is None or p.name > existing.name:
                latest[table_name] = p
        return latest

    for table, path in _latest_per_table("mappings").items():
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        out["files_used"].append(path.name)
        for entry in data:
            if not isinstance(entry, dict):
                continue
            src = entry.get("source_column_uri") or ""
            tgt = entry.get("product_column_uri") or ""
            if not (src and tgt):
                continue
            out["mappings"][(src, tgt)] = {
                "rationale": (entry.get("rationale") or "").strip(),
                "similarity_score": entry.get("similarity_score"),
                "transform_kind": entry.get("transform_kind"),
                "transform_author": entry.get("transform_author"),
                "transform_confidence": entry.get("transform_confidence"),
                "table": table,
            }

    for table, path in _latest_per_table("mapping_candidates").items():
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        out["files_used"].append(path.name)
        source_cols = data.get("source_columns") or []
        product_cols = data.get("product_columns") or []
        for pc in product_cols:
            if not isinstance(pc, dict):
                continue
            pc_uri = pc.get("column_uri") or ""
            if not pc_uri:
                continue
            # Stash the consumer column's pool of candidates from this table.
            out["candidates"].setdefault(pc_uri, []).extend(
                {
                    "source_uri": sc.get("column_uri"),
                    "source_name": sc.get("column_name"),
                    "data_type": sc.get("data_type"),
                    "description": sc.get("description"),
                    "table": table,
                }
                for sc in source_cols
                if isinstance(sc, dict) and sc.get("column_name")
            )
    return out


def _extract_run_highlights(log_events: list[dict], max_paragraphs: int = 12) -> list[str]:
    """Pull the agent's table-batch narration out of the run log for the
    "Run highlights" section. Keeps assistant-text paragraphs that look like
    summaries — bold-emphasis openers (``**...**:``), markdown-heading openers
    (``## ``), or any block ≥120 chars that starts with strong emphasis.

    De-duplicated by a hash of the first 100 chars so a repeated paragraph
    only shows once. Chronological order preserved.
    """
    seen: set[str] = set()
    out: list[str] = []
    for ev in log_events:
        if not isinstance(ev, dict):
            continue
        if ev.get("type") != "text_delta":
            continue
        text = (ev.get("text") or "").strip()
        if not text:
            continue
        # Split text into paragraphs on blank lines so multi-paragraph
        # assistant messages contribute individual highlights.
        for para in text.split("\n\n"):
            p = para.strip()
            if not p:
                continue
            opens_emphasis = p.startswith("**") and "**:" in p[:120]
            opens_heading = p.startswith("## ") or p.startswith("### ")
            long_enough = len(p) >= 120
            if not (opens_emphasis or opens_heading or (long_enough and p.startswith("**"))):
                continue
            key = p[:100]
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
            if len(out) >= max_paragraphs:
                return out
    return out


async def _run_rationale_summarizer_skill(
    payload: dict,
) -> tuple[dict, Optional[str]]:
    """Invoke the ``data-mapping-rationale-summarizer`` skill via the Claude
    Code SDK. One call per report; the skill returns ``{rationales:
    [{column_name, rationale}]}`` as a single fenced JSON block.

    Mirrors ``_run_classifier_skill`` in routers/ingest_products.py.
    Returns ``(payload, error)`` — caller falls back to heuristic on error.
    """
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import (
            AssistantMessage,
            ResultMessage,
            TextBlock,
        )
    except ImportError:
        return {}, "claude-agent-sdk is not installed"

    from ..config import BASE_DIR, PIPELINE_PLUGINS

    system_prompt = (
        f"FIRST: Load the `{_RATIONALE_SUMMARIZER_SKILL}` skill via the Skill "
        "tool, then follow its instructions to the letter. Emit exactly one "
        "fenced JSON code block as the skill instructs. Do not write files. "
        "Do not run shell commands. Do not answer in prose outside the JSON "
        "block."
    )
    user_prompt = (
        f"FIRST: Load the {_RATIONALE_SUMMARIZER_SKILL} skill using the Skill tool.\n\n"
        f"INPUTS (JSON):\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        "Output the single fenced JSON block as instructed by the skill."
    )
    options = ClaudeAgentOptions(
        allowed_tools=["Read", "Skill"],
        permission_mode="acceptEdits",
        cwd=str(BASE_DIR),
        max_turns=4,
        skills="all",
        plugins=PIPELINE_PLUGINS,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": system_prompt,
        },
    )

    transcript_parts: list[str] = []
    try:
        async for message in query(prompt=user_prompt, options=options):
            if message is None:
                continue
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        transcript_parts.append(block.text)
            elif isinstance(message, ResultMessage):
                from ..llm_usage import extract_usage, record_usage
                record_usage(source="rationale_summarizer", usage=extract_usage(message))
                if message.is_error:
                    return {}, "Rationale summarizer returned an error"
    except Exception as e:
        return {}, f"Rationale summarizer failed: {e}"

    # Reuse the same fenced-JSON extractor pattern as ingest_products._parse_skill_json.
    import re
    matches = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", "\n".join(transcript_parts), re.DOTALL)
    parsed: dict = {}
    if matches:
        try:
            parsed = json.loads(matches[-1])
        except json.JSONDecodeError:
            pass
    if not isinstance(parsed, dict) or "rationales" not in parsed:
        return {}, "Rationale summarizer output not in the expected shape"
    return parsed, None


def _heuristic_rationale(mapping: dict, on_disk: Optional[dict]) -> str:
    """Compose a 2–3 sentence user-friendly rationale from already-known
    mapping data when the LLM summarizer skill isn't installed. Reads cleanly
    even without LLM polish.

    Three sentences, conditionally emitted:
      1. ``Header`` — by transform_author. AI suggestions with on-disk
         rationale pass it through verbatim; other authors get author-specific
         copy.
      2. ``Approach`` — describes the transform_kind in plain English.
      3. ``Status``  — approval state + escalation context if any.
    """
    author = (mapping.get("transform_author") or "").strip()
    sim = mapping.get("similarity_score")
    transform_kind = (mapping.get("transform_kind") or "direct").strip()
    expr = (mapping.get("transform_expression") or "").strip()
    status = (mapping.get("status") or "").strip()
    esc = (mapping.get("transform_escalation_reason") or "").strip()

    # ── Header sentence ──
    header: str
    on_disk_rationale = (on_disk or {}).get("rationale") if on_disk else ""
    if author == "ai_suggestion" and on_disk_rationale:
        header = on_disk_rationale.rstrip(".") + "."
    elif author == "ai_suggestion":
        pct = f" ({int(round(float(sim) * 100))}% confidence)" if isinstance(sim, (int, float)) else ""
        header = f"AI-suggested mapping based on a semantic match between the consumer column and the source column{pct}."
    elif author == "po_hint":
        header = "Mapped per a PO transform hint declared on this column in the spec."
    elif author == "steward_catalog":
        header = "Applied a steward-maintained transformation template from the domain catalog."
    elif author == "engineer":
        header = "Engineer-authored mapping created during review."
    else:
        header = "Mapping recorded by the data-mapping skill."

    # ── Approach sentence ──
    approach_by_kind: dict[str, str] = {
        "direct":    "No transformation needed — the source column is used as-is.",
        "literal":   "Populated with a constant literal value (no upstream source).",
        "cast":      "Cast from the source column's type to the consumer column's expected type.",
        "concat":    "Concatenated from multiple source columns into a single string.",
        "lookup":    "Resolved via a lookup against a reference table.",
        "case":      "Computed via a CASE expression that branches on source values.",
        "arithmetic": "Computed via arithmetic on one or more source columns.",
        "bucket":    "Bucketed from a continuous source column into a discrete category.",
        "mask":      "Masked from the source value (for privacy / sensitivity handling).",
        "hash":      "Hashed from the source value (for de-identification or stable surrogate keys).",
        "format":    "Re-formatted from the source value (e.g. date / phone normalisation).",
    }
    approach = approach_by_kind.get(transform_kind, f"Applied a `{transform_kind}` transform.")
    if expr:
        approach += f" SQL fragment: `{expr}`."

    # ── Status sentence ──
    status_phrases = {
        "approved": "Approved during review.",
        "pending_review": "Currently pending review.",
        "steward_review": "Escalated to the Data Steward for help.",
        "rejected": "Rejected during review.",
        "superseded": "Superseded by a newer mapping.",
    }
    status_sentence = status_phrases.get(status, f"Status: `{status}`.")
    if esc and status in ("steward_review", "rejected"):
        status_sentence += f" Reason: {esc}"

    return f"{header} {approach} {status_sentence}".strip()


def _build_summarizer_payload(
    project: Project,
    mappings: list[dict],
    artifacts: dict,
    run_highlights: list[str],
) -> dict:
    """Build the input payload for the rationale-summarizer skill. Each
    mapping carries source/target/transform/author/similarity plus any
    on-disk rationale + the top-3 considered alternatives from the
    candidates JSON. Run highlights provide table-batch context."""
    out_mappings: list[dict] = []
    for m in mappings:
        src_uri = m.get("source_col_uri") or ""
        pc_uri = m.get("product_col_uri") or ""
        on_disk = artifacts["mappings"].get((src_uri, pc_uri))
        on_disk_rationale = (on_disk or {}).get("rationale", "")

        # Up to 3 alternative source columns the agent considered but didn't
        # pick (excluding the chosen one) — useful signal for the skill to
        # explain WHY this source was preferred.
        considered = artifacts["candidates"].get(pc_uri, [])
        chosen_name = (m.get("source_col_name") or "").strip()
        alternatives = [
            {"source": f"{c.get('table') or '?'}.{c.get('source_name') or '?'}", "data_type": c.get("data_type")}
            for c in considered
            if c.get("source_name") and c.get("source_name") != chosen_name
        ][:3]

        out_mappings.append({
            "column_name": m.get("product_col_name") or "",
            "dataset": m.get("product_dataset_name") or m.get("product_name") or "",
            "source": (
                f"{m.get('source_schema') or m.get('source_table') or '?'}."
                f"{m.get('source_col_name') or '?'}"
                if (m.get("transform_kind") or "direct") != "literal"
                else "(literal)"
            ),
            "transform_kind": m.get("transform_kind") or "direct",
            "transform_expression": m.get("transform_expression") or "",
            "transform_author": m.get("transform_author") or "",
            "similarity_score": m.get("similarity_score"),
            "status": m.get("status") or "",
            "escalation_reason": m.get("transform_escalation_reason") or "",
            "on_disk_rationale": on_disk_rationale,
            "considered_alternatives": alternatives,
        })

    return {
        "consumer_name": project.name or project.project_code,
        "run_highlights": run_highlights,
        "mappings": out_mappings,
    }


def _render_rationale_markdown(
    project: Project,
    mappings: list[dict],
    log_events: list[dict],
    stage_meta: dict,
    artifacts: dict,
    rationales_by_column: dict[str, str],
    run_highlights: list[str],
) -> str:
    """Render the markdown report. Sectioned: header → run summary → run
    highlights → per-mapping sections grouped by output dataset."""
    lines: list[str] = []
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines.append(f"# Data mapping rationale — {project.name or project.project_code}")
    lines.append("")
    lines.append(f"*Generated {now_iso}.* Built from the latest `data_mapping` run's on-disk artifacts plus run log. Nothing here is persisted to the graph.")
    lines.append("")

    # ── Run summary ────────────────────────────────────────────────────
    lines.append("## Run summary")
    lines.append("")
    if stage_meta.get("missing"):
        lines.append("- _No `data_mapping` StageExecution row found for this project. The agent may not have run yet, or the run log was wiped._")
    else:
        if stage_meta.get("started_at"):
            lines.append(f"- **Started:** {stage_meta['started_at']}")
        if stage_meta.get("completed_at"):
            lines.append(f"- **Completed:** {stage_meta['completed_at']}")
        if stage_meta.get("status"):
            lines.append(f"- **Status:** {stage_meta['status']}")
        if stage_meta.get("cost_usd") is not None:
            lines.append(f"- **Cost (USD):** {stage_meta['cost_usd']:.4f}")
        tool_counts = stage_meta.get("tool_counts") or {}
        if tool_counts:
            joined = ", ".join(
                f"{k} ×{v}" for k, v in sorted(tool_counts.items(), key=lambda kv: -kv[1])
            )
            lines.append(f"- **Tool usage:** {joined}")
        lines.append(f"- **Total log events:** {len(log_events)}")
        artifact_files = artifacts.get("files_used") or []
        if artifact_files:
            lines.append(f"- **On-disk artifacts:** {len(artifact_files)} file(s) parsed")
    lines.append("")

    # ── Run highlights ─────────────────────────────────────────────────
    if run_highlights:
        lines.append("## Run highlights")
        lines.append("")
        for para in run_highlights:
            indented = "\n".join("> " + ln for ln in para.splitlines())
            lines.append(indented)
            lines.append("")
    elif not stage_meta.get("missing"):
        lines.append("## Run highlights")
        lines.append("")
        lines.append("_The agent didn't emit any table-batch narration the report could extract from this run._")
        lines.append("")

    # ── Per-mapping sections ───────────────────────────────────────────
    lines.append(f"## Mappings ({len(mappings)})")
    lines.append("")
    if not mappings:
        lines.append("_No `:ColumnMapping` rows are currently associated with this project. Run the data_mapping stage first._")
        return "\n".join(lines).rstrip() + "\n"

    by_dataset: dict[str, list[dict]] = {}
    for m in mappings:
        ds = m.get("product_dataset_name") or m.get("product_name") or "(unknown)"
        by_dataset.setdefault(ds, []).append(m)

    for dataset_name in sorted(by_dataset.keys()):
        ds_mappings = by_dataset[dataset_name]
        lines.append(f"### {dataset_name}")
        lines.append("")
        for m in ds_mappings:
            pc_name = m.get("product_col_name") or "(unnamed column)"
            transform_kind = (m.get("transform_kind") or "direct").strip()
            author = (m.get("transform_author") or "unknown").strip()
            status = (m.get("status") or "unknown").strip()
            sim = m.get("similarity_score")

            lines.append(f"#### `{pc_name}`")
            lines.append("")

            # Source: literal OR catalog OR dprod, depending on transform kind.
            if transform_kind == "literal":
                source_str = "_literal value (no upstream source)_"
            else:
                src_table = m.get("source_table") or "(unknown)"
                src_schema = m.get("source_schema") or ""
                src_col = m.get("source_col_name") or "(unknown)"
                if src_schema:
                    source_str = f"`{src_schema}.{src_table}.{src_col}`"
                else:
                    source_str = f"`{src_table}.{src_col}`"
            lines.append(f"- **Source:** {source_str}")

            # Approach: kind + optional expression.
            expr = (m.get("transform_expression") or "").strip()
            if expr:
                lines.append(f"- **Approach:** `{transform_kind}` — `{expr}`")
            else:
                lines.append(f"- **Approach:** `{transform_kind}`")

            # Status line with optional similarity.
            sim_str = (
                f" · similarity {float(sim):.2f}"
                if isinstance(sim, (int, float))
                else ""
            )
            lines.append(f"- **Status:** `{status}` · authored by `{author}`{sim_str}")
            lines.append("")

            # Rationale paragraph — from summarizer skill or heuristic fallback.
            paragraph = rationales_by_column.get(pc_name) or ""
            paragraph = paragraph.strip()
            if not paragraph:
                paragraph = _heuristic_rationale(
                    m,
                    artifacts["mappings"].get(
                        (m.get("source_col_uri") or "", m.get("product_col_uri") or "")
                    ),
                )
            lines.append(paragraph)
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


@router.get("/mappings/rationale-report")
def mappings_rationale_report(
    project_id: int,
    session: Session = Depends(get_session),
):
    """Generate a markdown rationale report for the current :ColumnMapping
    set. Pulls per-mapping rationale from on-disk artifacts left by the
    data-mapping skill, optionally enriches with the
    ``data-mapping-rationale-summarizer`` LLM skill, falls back to a
    deterministic heuristic when the skill isn't installed. Stateless — the
    filename embeds the project_code + UTC timestamp so each download is a
    historical snapshot the engineer can keep on disk.
    """
    project = _get_project(project_id, session)
    locator = _find_mapping_stage_locator(project, session)

    # ── Pull the latest data_mapping StageExecution + parse its log_json ─
    stage_meta: dict = {"missing": True}
    log_events: list[dict] = []
    if locator is not None:
        workflow_id, stage_number = locator
        stmt = (
            select(StageExecution)
            .where(StageExecution.project_id == project.id)
            .where(StageExecution.stage_number == stage_number)
        )
        if workflow_id is not None:
            stmt = stmt.where(StageExecution.workflow_id == workflow_id)
        stmt = stmt.order_by(StageExecution.started_at.desc()).limit(1)
        execution = session.exec(stmt).first()
        if execution is not None:
            try:
                parsed = json.loads(execution.log_json or "[]")
                if isinstance(parsed, list):
                    log_events = [e for e in parsed if isinstance(e, dict)]
            except json.JSONDecodeError:
                log_events = []
            try:
                tool_counts = json.loads(execution.tool_counts_json or "{}")
            except json.JSONDecodeError:
                tool_counts = {}
            stage_meta = {
                "missing": False,
                "started_at": execution.started_at.isoformat() if execution.started_at else None,
                "completed_at": execution.completed_at.isoformat() if execution.completed_at else None,
                "status": execution.status,
                "cost_usd": execution.cost_usd,
                "tool_counts": tool_counts,
            }

    # ── On-disk artifacts (mappings + candidates) ──────────────────────
    artifacts = _load_mapping_artifacts(project.project_code)

    # ── Run highlights from the log ────────────────────────────────────
    run_highlights = _extract_run_highlights(log_events)

    # ── Current :ColumnMapping rows from Neo4j ─────────────────────────
    scoped = has_project_node(project)
    pc_params = {"project_code": project.project_code} if scoped else {}
    q = GRAPH_MAPPINGS_QUERY_S if scoped else GRAPH_MAPPINGS_QUERY
    try:
        with neo4j_session(
            project.neo4j_host, project.neo4j_port,
            project.neo4j_user, project.neo4j_password, project.neo4j_database,
        ) as ns:
            rows = [dict(r) for r in ns.run(q, **pc_params)]
    except Exception:
        rows = []
    mappings = _dedup_mapping_rows(rows)

    # ── Invoke the rationale-summarizer skill (with heuristic fallback) ─
    rationales_by_column: dict[str, str] = {}
    if mappings:
        payload = _build_summarizer_payload(project, mappings, artifacts, run_highlights)
        import asyncio as _asyncio
        try:
            skill_out, error = _asyncio.run(_run_rationale_summarizer_skill(payload))
        except RuntimeError:
            skill_out, error = {}, "Event loop already running"
        if not error and isinstance(skill_out.get("rationales"), list):
            for entry in skill_out["rationales"]:
                if not isinstance(entry, dict):
                    continue
                col = (entry.get("column_name") or "").strip()
                text = (entry.get("rationale") or "").strip()
                if col and text:
                    rationales_by_column[col] = text
        # Any column not covered by the skill falls back to the heuristic in
        # _render_rationale_markdown.

    markdown = _render_rationale_markdown(
        project,
        mappings,
        log_events,
        stage_meta,
        artifacts,
        rationales_by_column,
        run_highlights,
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"data-mapping-rationale-{project.project_code}-{timestamp}.md"
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
