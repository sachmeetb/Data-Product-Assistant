"""Data Marketplace — consumer-facing view of published data products."""

import json
import re
from typing import Any, Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query as QParam, Response
from pydantic import BaseModel
from sqlmodel import Session, select

from ..database import get_session
from ..flow_payload import FlowBuilder, bucket_product_kind
from ..lookup_via import merge_lookup_graph_rows
from ..sql_ident import quote_ident, quote_created_relation
from ..models import (
    Project, AppSettings, MarketplaceGap,
    SemanticChatSession, SemanticChatMessage,
)
from ..neo4j_client import neo4j_session

router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])

# ── Queries ────────────────────────────────────────────────────────────────

# Marketplace pinning under the stable-:DataContract / temporal-edge model:
#
# - :DataContract is ONE stable node per product (keyed on id).
# - :ContractVersion sidecars carry per-version state via :HAS_VERSION.
# - Substructure (owners, schemas, properties, rules) is keyed on logical
#   identity URIs with temporal validity (fromVersion / toVersion).
#
# To "pin to latest deployed", we find the :ContractVersion with the highest
# `version` whose lifecycleState is in {published, superseded}. If none exists,
# we fall back to the contract's currentVersion (so PO's draft-only products
# still show up in their My Products list). The `has_inflight_edit` flag
# fires when a deployed cv exists AND the current cv has a different version
# in a drafty state — signals the UI to render "Deploy update" instead of
# "Deploy".

ALL_PRODUCTS = """\
MATCH (dp:DProdDataProduct)
OPTIONAL MATCH (dp)<-[:MATERIALISES_AS]-(dc:DataContract)
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv_pub:ContractVersion)
WHERE cv_pub.lifecycleState IN ['published','superseded']
WITH dp, dc, cv_pub ORDER BY cv_pub.version DESC
WITH dp, dc, head(collect(cv_pub)) AS cv_deployed
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv_cur:ContractVersion {version: dc.currentVersion})
WITH dp, dc, coalesce(cv_deployed, cv_cur) AS cv, cv_deployed, cv_cur
WITH dp, dc, cv,
     cv_deployed IS NOT NULL
       AND cv_cur IS NOT NULL
       AND cv_cur.version <> cv_deployed.version
       AND cv_cur.lifecycleState IN ['draft','submitted','in_engineering','approved','rejected']
       AS has_inflight_edit,
     (cv_cur IS NOT NULL
       AND cv_cur.lifecycleState IN ['draft','ingesting']
       AND coalesce(dc.currentVersion, 1) > 1) AS discardable_draft
WHERE $owned_by IS NULL OR EXISTS {
  MATCH (dc)-[r_o:HAS_OWNER]->(o:DataContractOwner)
  WHERE r_o.fromVersion <= cv.version
    AND (r_o.toVersion IS NULL OR r_o.toVersion >= cv.version)
    AND toLower(o.email) = toLower($owned_by)
}
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->()-[:DPROD_OUTPUT_DATASET]->()-[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
WITH dp, dc, cv, has_inflight_edit, discardable_draft, count(DISTINCT pc) AS column_count
OPTIONAL MATCH (dc)-[r_o:HAS_OWNER]->(owner:DataContractOwner)
WHERE r_o.fromVersion <= cv.version
  AND (r_o.toVersion IS NULL OR r_o.toVersion >= cv.version)
WITH dp, dc, cv, has_inflight_edit, discardable_draft, column_count, collect(owner)[0] AS first_owner
OPTIONAL MATCH (dc)-[:HAS_OSI_EVAL]->(oe_all:OsiEvaluation)
WITH dp, dc, cv, has_inflight_edit, discardable_draft, column_count, first_owner, oe_all
ORDER BY oe_all.evaluatedAt DESC
WITH dp, dc, cv, has_inflight_edit, discardable_draft, column_count, first_owner, head(collect(oe_all)) AS oe
RETURN
    dp.uri AS uri,
    dp.name AS name,
    dp.status AS status,
    coalesce(cv.lifecycleState, 'draft') AS lifecycle_state,
    dc.id AS contract_id,
    cv.version AS lifecycle_version,
    has_inflight_edit AS has_inflight_edit,
    discardable_draft AS discardable_draft,
    coalesce(dc.productKind, '') AS product_kind,
    dp.publishedAt AS published_at,
    dp.publishedBy AS published_by,
    dp.createdAt AS created_at,
    coalesce(cv.snapshotDescription, dc.description) AS description,
    coalesce(cv.snapshotPurpose, dc.purpose) AS purpose,
    COALESCE(first_owner.name, first_owner.username) AS owner_name,
    first_owner.role AS owner_role,
    coalesce(cv.snapshotDomain, dc.domain) AS domain,
    column_count,
    oe.band            AS osi_band,
    oe.completeness    AS osi_completeness,
    oe.conformancePass AS osi_conformance_pass,
    oe.evaluatedAt     AS osi_evaluated_at,
    coalesce(dc.scoringRubric, 'osi') AS scoring_rubric,
    coalesce(oe.rubric, dc.scoringRubric, 'osi') AS osi_rubric,
    coalesce(oe.rubricLabel, '') AS osi_rubric_label,
    coalesce(dc.tags, []) AS tags
ORDER BY
    CASE dp.status WHEN 'published' THEN 0 ELSE 1 END,
    dp.publishedAt DESC,
    dp.createdAt DESC
"""

PRODUCT_DETAIL = """\
// Stable-:DataContract version-pin: find latest deployed :ContractVersion
// (published or superseded), fall back to current cv when none exists.
// All substructure walks filter the temporal edge by cv.version so the
// pinned view reflects which entities were active at that version.
MATCH (dp:DProdDataProduct {uri: $uri})
OPTIONAL MATCH (dp)<-[:MATERIALISES_AS]-(dc:DataContract)
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv_pub:ContractVersion)
WHERE cv_pub.lifecycleState IN ['published','superseded']
WITH dp, dc, cv_pub ORDER BY cv_pub.version DESC
WITH dp, dc, head(collect(cv_pub)) AS cv_deployed
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv_cur:ContractVersion {version: dc.currentVersion})
WITH dp, dc, coalesce(cv_deployed, cv_cur) AS cv, cv_deployed, cv_cur
WITH dp, dc, cv,
     cv_deployed IS NOT NULL
       AND cv_cur IS NOT NULL
       AND cv_cur.version <> cv_deployed.version
       AND cv_cur.lifecycleState IN ['draft','submitted','in_engineering','approved','rejected']
       AS has_inflight_edit
OPTIONAL MATCH (dc)-[:HAS_OSI_EVAL]->(oe_all:OsiEvaluation)
WITH dp, dc, cv, has_inflight_edit, oe_all
ORDER BY oe_all.evaluatedAt DESC
WITH dp, dc, cv, has_inflight_edit, head(collect(oe_all)) AS oe
// Latest :QAEvaluation (append-only, like OsiEvaluation). Carries the
// generated question set + near-miss gaps for the marketplace QA tab.
OPTIONAL MATCH (dc)-[:HAS_QA_EVAL]->(qa_all:QAEvaluation)
WITH dp, dc, cv, has_inflight_edit, oe, qa_all
ORDER BY qa_all.evaluatedAt DESC
WITH dp, dc, cv, has_inflight_edit, oe, head(collect(qa_all)) AS qa
OPTIONAL MATCH (dc)-[r_t:HAS_TERMS]->(terms:DataContractTerms)
WHERE r_t.fromVersion <= cv.version AND (r_t.toVersion IS NULL OR r_t.toVersion >= cv.version)
WITH dp, dc, cv, terms, has_inflight_edit, oe, qa

OPTIONAL MATCH (dc)-[r_o:HAS_OWNER]->(o:DataContractOwner)
WHERE r_o.fromVersion <= cv.version AND (r_o.toVersion IS NULL OR r_o.toVersion >= cv.version)
WITH dp, dc, cv, terms, has_inflight_edit, oe, qa,
     collect({username: o.username, name: o.name, role: o.role, email: o.email}) AS owners

OPTIONAL MATCH (dc)-[r_s:HAS_STEWARD]->(st:DataContractSteward)
WHERE r_s.fromVersion <= cv.version AND (r_s.toVersion IS NULL OR r_s.toVersion >= cv.version)
WITH dp, dc, cv, terms, has_inflight_edit, oe, qa, owners,
     collect({name: st.name, email: st.email, role: st.role, username: st.username}) AS stewards

OPTIONAL MATCH (dc)-[r_tm:HAS_TEAM_MEMBER]->(tm:DataContractTeamMember)
WHERE r_tm.fromVersion <= cv.version AND (r_tm.toVersion IS NULL OR r_tm.toVersion >= cv.version)
WITH dp, dc, cv, terms, has_inflight_edit, oe, qa, owners, stewards,
     collect({username: tm.username, role: tm.role, name: tm.name, email: tm.email}) AS team

OPTIONAL MATCH (dc)-[r_rl:HAS_ROLE]->(rl:DataContractRole)
WHERE r_rl.fromVersion <= cv.version AND (r_rl.toVersion IS NULL OR r_rl.toVersion >= cv.version)
WITH dp, dc, cv, terms, has_inflight_edit, oe, qa, owners, stewards, team,
     collect({role: rl.role, description: rl.description, access: rl.access, datasets: rl.datasets}) AS roles

OPTIONAL MATCH (dc)-[r_srv:HAS_SERVER]->(srv:DataContractServer)
WHERE r_srv.fromVersion <= cv.version AND (r_srv.toVersion IS NULL OR r_srv.toVersion >= cv.version)
WITH dp, dc, cv, terms, has_inflight_edit, oe, qa, owners, stewards, team, roles,
     collect({name: srv.name, environment: srv.environment, type: srv.type, account: srv.account, database: srv.database, schema: srv.schema, datasets: srv.datasets}) AS servers

OPTIONAL MATCH (dc)-[r_sla:HAS_SLA_PROPERTY]->(sla:DataContractSLAProperty)
WHERE r_sla.fromVersion <= cv.version AND (r_sla.toVersion IS NULL OR r_sla.toVersion >= cv.version)
WITH dp, dc, cv, terms, has_inflight_edit, oe, qa, owners, stewards, team, roles, servers,
     collect({property: sla.property, value: sla.value, unit: sla.unit}) AS slas

OPTIONAL MATCH (dc)-[r_q:HAS_QUALITY_RULE]->(q:DataContractQuality)
WHERE r_q.fromVersion <= cv.version AND (r_q.toVersion IS NULL OR r_q.toVersion >= cv.version)
WITH dp, dc, cv, terms, has_inflight_edit, oe, qa, owners, stewards, team, roles, servers, slas,
     collect({
       rule: q.rule, name: q.name, description: q.description,
       severity: q.severity, dimension: q.dimension,
       businessImpact: q.businessImpact,
       source: 'contract',
       column_name: coalesce(q.column, ''),
       dataset_name: coalesce(q.dataset, '')
     }) AS contract_quality_rules

// Also pull PO-approved domain and user-authored rules from :PropertyShape
// attached to :DProdColumn — these originate in the Product Workbench Rule
// Coach (catalog suggestions and chat-driven rule_create) and have no
// :DataContractQuality twin. ruleSource is preserved so the marketplace UI
// can render distinct badges.
//
// 'spec' PropertyShapes are intentionally excluded: they are SHACL-shaped
// duplicates of the :DataContractQuality rows already collected above as
// source='contract' (materialised by _materialise_spec_rules during
// _generate_dprod with severity translated to sh:Violation / sh:Warning).
// Engineer-facing surfaces (summary.py, reviews.py, DQ-test-gen skills)
// still see them — only the consumer marketplace projection hides them.
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(ods_q:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc_q:DProdColumn)
      <-[:ON_DPROD_COLUMN]-(ps_q:PropertyShape)
WHERE ps_q.ruleSource IN ['domain', 'user'] AND ps_q.status = 'approved'
WITH dp, dc, cv, terms, has_inflight_edit, oe, qa, owners, stewards, team, roles, servers, slas, contract_quality_rules,
     collect({
       rule: ps_q.ruleType,
       name: ps_q.ruleType,
       description: ps_q.description,
       severity: ps_q.severity,
       dimension: coalesce(ps_q.dimension, 'domain'),
       businessImpact: coalesce(ps_q.businessImpact, ''),
       source: ps_q.ruleSource,
       column_name: pc_q.name,
       dataset_name: coalesce(ods_q.physicalName, ods_q.name, '')
     }) AS propertyshape_quality_rules
WITH dp, dc, cv, terms, has_inflight_edit, oe, qa, owners, stewards, team, roles, servers, slas,
     contract_quality_rules + propertyshape_quality_rules AS quality_rules

OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
// Pull the per-dataset :DatasetTransform sidecar (shape — grain prose,
// filter, dedupe, joins, grouping, scd policy, suppressed columns, window
// specs). Lets the marketplace + QA analyzer surface the dataset's
// analytical shape, not just its columns.
OPTIONAL MATCH (ods)-[:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform)
OPTIONAL MATCH (ods)-[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
WITH dp, dc, cv, terms, has_inflight_edit, oe, qa, owners, stewards, team, roles, servers, slas, quality_rules, ods, dt,
     collect({name: pc.name, logicalName: pc.logicalName, logicalType: pc.logicalType, physicalType: pc.dataType, description: pc.description, primaryKey: pc.isPrimaryKey, sensitivity: coalesce(pc.sensitivity, 'none')}) AS ds_columns
WITH dp, dc, cv, terms, has_inflight_edit, oe, qa, owners, stewards, team, roles, servers, slas, quality_rules,
     collect({
       uri: ods.uri,
       name: ods.name,
       physicalName: ods.physicalName,
       description: ods.description,
       relationshipKind: coalesce(ods.relationshipKind, ''),
       transform: CASE WHEN dt IS NULL THEN null ELSE {
         filter: coalesce(dt.filterPredicate, ''),
         dedupeJson: coalesce(dt.dedupeJson, ''),
         joinsJson: coalesce(dt.joinsJson, ''),
         groupingKeysJson: coalesce(dt.groupingKeysJson, ''),
         windowSpecsJson: coalesce(dt.windowSpecsJson, ''),
         scdPolicyJson: coalesce(dt.scdPolicyJson, ''),
         suppressedColumnsJson: coalesce(dt.suppressedColumnsJson, ''),
         grainProse: coalesce(dt.grainProse, '')
       } END,
       columns: ds_columns
     }) AS datasets

OPTIONAL MATCH (dp)-[:SERVED_BY]->(sd:ServingDefinition)
WITH dp, dc, cv, terms, has_inflight_edit, oe, qa, owners, stewards, team, roles, servers, slas, quality_rules, datasets,
     collect({servingMode: sd.servingMode, viewName: sd.viewName, viewSchema: sd.viewSchema,
              targetPlatform: sd.targetPlatform, ddl: sd.ddl,
              summaryJson: coalesce(sd.summaryJson, ''),
              deploymentStatus: coalesce(sd.deploymentStatus, 'pending'),
              deployedAt: sd.deployedAt,
              deployedTo: sd.deployedTo,
              // Materialized (dbt) serving fields — null on virtual_view rows.
              dbtMaterialization: sd.dbtMaterialization,
              targetSchema: sd.targetSchema,
              modelsJson: coalesce(sd.modelsJson, ''),
              buildStatus: sd.buildStatus,
              builtAt: sd.builtAt,
              buildError: sd.buildError}) AS serving

// Cross-product relationships, pinned to the contract version's view of
// :CONSUMES edges. The :CONSUMES edge is temporal in the stable model
// (fromVersion/toVersion) — substructure walk filters apply.
//   consumes[]    — source products this contract :CONSUMES at cv.version.
//   consumed_by[] — consumer contracts whose current view :CONSUMES this dp.
OPTIONAL MATCH (dc)-[r_c:CONSUMES]->(src_dp:DProdDataProduct)
WHERE r_c.fromVersion <= cv.version AND (r_c.toVersion IS NULL OR r_c.toVersion >= cv.version)
WITH dp, dc, cv, terms, has_inflight_edit, oe, qa, owners, stewards, team, roles, servers, slas,
     quality_rules, datasets, serving,
     collect(DISTINCT CASE WHEN src_dp IS NULL THEN NULL ELSE
       {uri: src_dp.uri, name: coalesce(src_dp.name, ''),
        product_kind: coalesce(src_dp.productKind, '')}
     END) AS consumes_raw

// Consumer side: find consumer contracts whose currentVersion view includes
// this dp via an active :CONSUMES edge.
OPTIONAL MATCH (cons_dc:DataContract)-[r_cc:CONSUMES]->(dp)
WHERE r_cc.fromVersion <= cons_dc.currentVersion
  AND (r_cc.toVersion IS NULL OR r_cc.toVersion >= cons_dc.currentVersion)
  AND cons_dc.currentLifecycleState IN ['published','superseded','approved','submitted','in_engineering']
OPTIONAL MATCH (cons_dc)-[:MATERIALISES_AS]->(cons_dp:DProdDataProduct)
WITH dp, dc, cv, terms, has_inflight_edit, oe, qa, owners, stewards, team, roles, servers, slas,
     quality_rules, datasets, serving, consumes_raw,
     collect(DISTINCT CASE WHEN cons_dp IS NULL THEN NULL ELSE
       {uri: cons_dp.uri, name: coalesce(cons_dp.name, ''),
        product_kind: coalesce(cons_dp.productKind, ''),
        lifecycle_state: coalesce(cons_dc.currentLifecycleState, '')}
     END) AS consumed_by_raw

WITH dp, dc, cv, terms, has_inflight_edit, oe, qa, owners, stewards, team, roles, servers, slas,
     quality_rules, datasets, serving,
     [c IN consumes_raw    WHERE c IS NOT NULL] AS consumes,
     [c IN consumed_by_raw WHERE c IS NOT NULL] AS consumed_by

RETURN
    dp.uri AS uri,
    dp.name AS name,
    dp.status AS status,
    coalesce(cv.lifecycleState, 'draft') AS lifecycle_state,
    dc.id AS contract_id,
    cv.version AS lifecycle_version,
    has_inflight_edit AS has_inflight_edit,
    coalesce(dc.productKind, '') AS product_kind,
    dp.publishedAt AS published_at,
    dp.publishedBy AS published_by,
    coalesce(cv.snapshotName, dc.name) AS title,
    coalesce(cv.snapshotDescription, dc.description) AS description,
    coalesce(cv.snapshotPurpose, dc.purpose) AS purpose,
    dc.limitations AS limitations,
    dc.domain AS domain,
    dc.dataProduct AS data_product,
    dc.tags AS tags,
    dc.support AS support_json,
    dc.customProperties AS custom_properties_json,
    owners,
    stewards,
    team,
    roles,
    servers,
    terms.usage AS terms_usage,
    terms.limitations AS terms_limitations,
    terms.billing AS terms_billing,
    terms.noticePeriod AS terms_notice_period,
    slas,
    quality_rules,
    datasets,
    serving,
    consumes,
    consumed_by,
    oe.band            AS osi_band,
    oe.completeness    AS osi_completeness,
    oe.conformancePass AS osi_conformance_pass,
    oe.evaluatedAt     AS osi_evaluated_at,
    coalesce(dc.scoringRubric, 'osi') AS scoring_rubric,
    coalesce(oe.rubric, dc.scoringRubric, 'osi') AS osi_rubric,
    coalesce(oe.rubricLabel, '') AS osi_rubric_label,
    qa.uri                  AS qa_uri,
    qa.mode                 AS qa_mode,
    qa.questionsJson        AS qa_questions_json,
    qa.nearMissGapsJson     AS qa_near_miss_gaps_json,
    qa.narrative            AS qa_narrative,
    qa.generatedForVersion  AS qa_generated_for_version,
    qa.analyzerVersion      AS qa_analyzer_version,
    qa.advisorError         AS qa_advisor_error,
    qa.evaluatedAt          AS qa_evaluated_at,
    coalesce(dc.lastSchemaChangeVersion, dc.currentVersion) AS qa_current_change_version
"""

# :ColumnMapping has its own lifecycle (isCurrent flag) that's INDEPENDENT
# of contract versioning — :ColumnMapping isn't versioned alongside the
# stable contract today. Phase 3 of the change-management plan introduces
# mapping continuity across versions; until then, just walk current
# mapping rows.
LINEAGE_QUERY = """\
MATCH (dp:DProdDataProduct {uri: $uri})
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->()-[:DPROD_OUTPUT_DATASET]->()
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
      <-[:MAPS_TO_PRODUCT_COLUMN]-(cm:ColumnMapping {isCurrent: true})
// Source-side: catalog (:Column ← :HAS_COLUMN ← :Dataset ← :DCAT_DATASET ← :Catalog)
// or consumer-aligned (:DProdColumn ← :HAS_PRODUCT_COLUMN ← :DProdOutputDataset ← ... ← :DProdDataProduct).
// The OPTIONAL MATCH chains are independent so a row matching one path leaves
// the other null; coalesce in the RETURN picks whichever resolved.
OPTIONAL MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(sc:Column)
               <-[:HAS_COLUMN]-(ds:Dataset)<-[:DCAT_DATASET]-(cat:Catalog)
OPTIONAL MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(sc_dp:DProdColumn)
OPTIONAL MATCH (ods:DProdOutputDataset)-[:HAS_PRODUCT_COLUMN]->(sc_dp)
OPTIONAL MATCH (srcDp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
               -[:DPROD_OUTPUT_DATASET]->(ods)
WITH dp,
     coalesce(cat.name, srcDp.name)                AS source_system_resolved,
     coalesce(ds.schema, srcDp.name)               AS source_schema_resolved,
     coalesce(ds.name, ods.physicalName, ods.name) AS source_table_resolved,
     count(DISTINCT cm)                            AS mapped_columns,
     count(DISTINCT coalesce(sc, sc_dp))           AS source_columns
WHERE source_table_resolved IS NOT NULL
RETURN
    source_system_resolved AS source_system,
    source_schema_resolved AS source_schema,
    source_table_resolved  AS source_table,
    mapped_columns,
    source_columns
ORDER BY source_schema, source_table
"""

# Lookup reference tables feeding this product via :LOOKUP_VIA — surfaced as
# additional Source-Tables rows tagged is_lookup (no X/Y "mapped" ratio; a
# lookup table isn't mapped 1:1, its columns are read by lookup transforms).
LINEAGE_LOOKUP_QUERY = """\
MATCH (dp:DProdDataProduct {uri: $uri})
MATCH (dp)-[:DPROD_OUTPUT_PORT]->()-[:DPROD_OUTPUT_DATASET]->()
      -[:HAS_PRODUCT_COLUMN]->(:DProdColumn)
      <-[:MAPS_TO_PRODUCT_COLUMN]-(cm:ColumnMapping {isCurrent: true})
MATCH (cm)-[:LOOKUP_VIA]->(lk)
OPTIONAL MATCH (lk_ds:Dataset)-[:HAS_COLUMN]->(lk)
OPTIONAL MATCH (lk_cat:Catalog)-[:DCAT_DATASET]->(lk_ds)
OPTIONAL MATCH (lk_ods:DProdOutputDataset)-[:HAS_PRODUCT_COLUMN]->(lk)
OPTIONAL MATCH (lk_srcDp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
               -[:DPROD_OUTPUT_DATASET]->(lk_ods)
WITH coalesce(lk_cat.name, lk_srcDp.name)               AS source_system_resolved,
     coalesce(lk_ds.schema, lk_srcDp.name)              AS source_schema_resolved,
     coalesce(lk_ds.name, lk_ods.physicalName, lk_ods.name) AS source_table_resolved,
     count(DISTINCT lk)                                 AS lookup_columns
WHERE source_table_resolved IS NOT NULL
RETURN
    source_system_resolved AS source_system,
    source_schema_resolved AS source_schema,
    source_table_resolved  AS source_table,
    lookup_columns
ORDER BY source_schema, source_table
"""

LINEAGE_STATS = """\
// Quality-rule count comes from the deployed contract version's view (or
// current draft if never deployed). Mapping count walks current
// :ColumnMapping rows (independent lifecycle, see LINEAGE_QUERY comment).
MATCH (dp:DProdDataProduct {uri: $uri})
OPTIONAL MATCH (dp)<-[:MATERIALISES_AS]-(dc:DataContract)
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv_pub:ContractVersion)
WHERE cv_pub.lifecycleState IN ['published','superseded']
WITH dp, dc, cv_pub ORDER BY cv_pub.version DESC
WITH dp, dc, head(collect(cv_pub)) AS cv_deployed
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv_cur:ContractVersion {version: dc.currentVersion})
WITH dp, dc, coalesce(cv_deployed, cv_cur) AS cv
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->()-[:DPROD_OUTPUT_DATASET]->()
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
OPTIONAL MATCH (cm:ColumnMapping {isCurrent: true})-[:MAPS_TO_PRODUCT_COLUMN]->(pc)
OPTIONAL MATCH (dc)-[r_q:HAS_QUALITY_RULE]->(q:DataContractQuality)
WHERE r_q.fromVersion <= cv.version AND (r_q.toVersion IS NULL OR r_q.toVersion >= cv.version)
RETURN
    count(DISTINCT pc) AS total_columns,
    count(DISTINCT cm) AS total_mappings,
    count(DISTINCT q) AS total_quality_rules
"""


def _get_settings(session: Session) -> AppSettings:
    settings = session.get(AppSettings, 1)
    if not settings:
        settings = AppSettings()
    return settings


def _neo4j_from_settings(settings: AppSettings):
    return neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    )


# ── Endpoints ──────────────────────────────────────────────────────────────

def _attach_project_id(row: dict, session: Session) -> None:
    """Derive project_id from contract_id so the frontend can link into
    /product/edit/{projectId} without a second round-trip. Contract ids are
    always ``{project_code}-contract``; Project lookup is by project_code."""
    contract_id = row.get("contract_id") or ""
    if not contract_id or not contract_id.endswith("-contract"):
        row["project_id"] = None
        return
    project_code = contract_id[: -len("-contract")]
    project = session.exec(select(Project).where(Project.project_code == project_code)).first()
    row["project_id"] = project.id if project else None


@router.get("")
def list_published(
    session: Session = Depends(get_session),
    owned_by: Optional[str] = QParam(None, description="Filter to products with an owner email matching this value (case-insensitive)."),
    product_kind: Optional[str] = QParam(None, description="Filter to a productKind: 'source' or 'consumer'."),
    tag: Optional[str] = QParam(None, description="Filter to products carrying this tag (case-insensitive)."),
):
    """List published data products. ``owned_by`` filters to products the given email appears on as an owner. ``product_kind`` filters by source-aligned vs consumer-aligned tagging. ``tag`` filters to products carrying the given tag (case-insensitive)."""
    settings = _get_settings(session)
    # Robust to an in-process caller that omits `product_kind`/`tag`: FastAPI's
    # default is a Query object (not None), so guard on isinstance(str) before
    # .strip() — otherwise an in-process call raises AttributeError before any
    # graph access.
    kind_filter = (
        product_kind.strip().lower() or None
        if isinstance(product_kind, str) else None
    )
    tag_filter = (
        tag.strip().lower() or None
        if isinstance(tag, str) else None
    )
    rubric_short_labels = _rubric_short_label_map()
    try:
        with _neo4j_from_settings(settings) as ns:
            rows = [dict(r) for r in ns.run(ALL_PRODUCTS, owned_by=owned_by)]
            if kind_filter:
                rows = [r for r in rows if (r.get("product_kind") or "").lower() == kind_filter]
            if tag_filter:
                rows = [
                    r for r in rows
                    if any((t or "").strip().lower() == tag_filter for t in (r.get("tags") or []))
                ]
            for row in rows:
                row["tags"] = list(row.get("tags") or [])
                for dt_field in ("published_at", "created_at", "osi_evaluated_at"):
                    if row.get(dt_field):
                        row[dt_field] = str(row[dt_field])
                # Stamp the chip-ready short label from the rubric catalog
                # (e.g. 'osi' → 'OSI', 'ai_ready' → 'AI-Ready'). Falls back
                # to 'OSI' so pre-rubric contracts keep rendering correctly.
                rubric_id = (row.get("scoring_rubric") or "osi").lower()
                row["rubric_short_label"] = rubric_short_labels.get(rubric_id, "OSI")
                _attach_project_id(row, session)
            return {"products": rows, "count": len(rows)}
    except Exception:
        return {"products": [], "count": 0}


# ── Datasets sub-tab (discovered :Dataset nodes) ───────────────────────────
# A discovered dataset is a lighter marketplace citizen than a data product: it
# has no contract, so it carries a SUBSET (schema.table, row count, columns,
# profiling, FK/ERD, description) plus its owning project's provenance. A dataset
# that has been PROMOTED (its columns feed a :ColumnMapping into a product) is
# excluded here — it shows under Data Products instead, so promotion moves it
# between tabs automatically. Datasets are global (cross-project) by design, the
# agreed visibility change; each row is tagged with its owning project_code
# (parsed from the `dataset:{project_code}:{schema}.{table}` URI).

ALL_DATASETS = """\
MATCH (ds:Dataset)
WHERE NOT EXISTS {
    MATCH (ds)-[:HAS_COLUMN]->(:Column)<-[:MAPS_SOURCE_COLUMN]-(:ColumnMapping)
}
OPTIONAL MATCH (ds)-[:HAS_TABLE_DESCRIPTION]->(td:TableDescription)
WHERE td.isCurrent = true
WITH ds, head(collect(td)) AS td
OPTIONAL MATCH (ds)-[:HAS_COLUMN]->(col:Column)
WITH ds, td, count(col) AS column_count
RETURN ds.uri AS uri, ds.name AS name, ds.schema AS schema,
       ds.row_count AS row_count, column_count,
       coalesce(td.text, '') AS description,
       coalesce(td.relationshipKind, '') AS relationship_kind
ORDER BY ds.schema, ds.name
"""

DATASET_DETAIL = """\
MATCH (ds:Dataset {uri: $uri})
OPTIONAL MATCH (ds)-[:HAS_TABLE_DESCRIPTION]->(td:TableDescription)
WHERE td.isCurrent = true
WITH ds, head(collect(td)) AS td
OPTIONAL MATCH (ds)-[:HAS_COLUMN]->(col:Column)
OPTIONAL MATCH (col)-[:HAS_DESCRIPTION]->(cd:ColumnDescription)
WHERE cd.isCurrent = true
OPTIONAL MATCH (col)-[:HAS_QUALITY_MEASUREMENT]->(qm:QualityMeasurement)-[:ON_METRIC]->(m:Metric)
WITH ds, td, col, head(collect(DISTINCT cd)) AS cd,
     collect(DISTINCT CASE WHEN m IS NULL THEN NULL ELSE {metric: m.name, value: qm.value} END) AS measures
WITH ds, td,
     collect(CASE WHEN col IS NULL THEN NULL ELSE {
        uri: col.uri, name: col.name, type: col.dataType, nullable: col.nullable,
        primary_key: col.primaryKey, ordinal: col.ordinal,
        description: coalesce(cd.text, ''),
        profiling: [x IN measures WHERE x IS NOT NULL]
     } END) AS columns_raw
OPTIONAL MATCH (ds)-[rout:REFERENCES]->(dso:Dataset)
WITH ds, td, columns_raw,
     collect(DISTINCT CASE WHEN dso IS NULL THEN NULL ELSE {
        table: dso.name, schema: dso.schema, dataset_uri: dso.uri,
        columns: rout.columns, referenced_columns: rout.referencedColumns
     } END) AS refs_out
OPTIONAL MATCH (dsi:Dataset)-[rin:REFERENCES]->(ds)
WITH ds, td, columns_raw, refs_out,
     collect(DISTINCT CASE WHEN dsi IS NULL THEN NULL ELSE {
        table: dsi.name, schema: dsi.schema, dataset_uri: dsi.uri,
        columns: rin.columns, referenced_columns: rin.referencedColumns
     } END) AS refs_in
RETURN ds.uri AS uri, ds.name AS name, ds.schema AS schema, ds.row_count AS row_count,
       coalesce(td.text, '') AS description, coalesce(td.relationshipKind, '') AS relationship_kind,
       [c IN columns_raw WHERE c IS NOT NULL] AS columns,
       [r IN refs_out WHERE r IS NOT NULL] AS references_out,
       [r IN refs_in WHERE r IS NOT NULL] AS references_in,
       EXISTS {
         MATCH (ds)-[:HAS_COLUMN]->(:Column)<-[:MAPS_SOURCE_COLUMN]-(:ColumnMapping)
       } AS promoted
"""


def _project_code_from_dataset_uri(uri: str) -> str:
    """`dataset:{project_code}:{schema}.{table}` → project_code (middle segment).
    Project codes contain no ':'; schema.table uses '.', so split(':', 2)[1] is safe."""
    parts = (uri or "").split(":", 2)
    return parts[1] if len(parts) >= 3 and parts[0] == "dataset" else ""


def _project_provenance_map(session: Session) -> dict[str, dict]:
    """{project_code: {name, archetype, id}} for stamping dataset rows."""
    out: dict[str, dict] = {}
    for p in session.exec(select(Project)).all():
        out[p.project_code] = {"project_name": p.name, "archetype": p.archetype, "project_id": p.id}
    return out


@router.get("/datasets")
def list_datasets(
    session: Session = Depends(get_session),
    archetype: Optional[str] = QParam(None, description="Filter to one archetype (e.g. 'dmig', 'dd'); default all."),
    project_code: Optional[str] = QParam(None, description="Filter to one project's datasets."),
):
    """List discovered datasets (the marketplace 'Datasets' sub-tab). Global across
    projects; excludes datasets already backing a data product (those appear under
    Data Products). Each row is tagged with its owning project's provenance."""
    settings = _get_settings(session)
    prov = _project_provenance_map(session)
    kind = (archetype or "").strip().lower() or None
    code = (project_code or "").strip() or None
    try:
        with _neo4j_from_settings(settings) as ns:
            rows = [dict(r) for r in ns.run(ALL_DATASETS)]
    except Exception:
        return {"datasets": [], "count": 0}
    out = []
    for row in rows:
        pc = _project_code_from_dataset_uri(row.get("uri", ""))
        meta = prov.get(pc, {})
        row["project_code"] = pc
        row["project_name"] = meta.get("project_name")
        row["archetype"] = meta.get("archetype")
        row["project_id"] = meta.get("project_id")
        if code and pc != code:
            continue
        if kind and (row.get("archetype") or "").lower() != kind:
            continue
        out.append(row)
    return {"datasets": out, "count": len(out)}


@router.get("/datasets/detail")
def get_dataset_detail(
    uri: str = QParam(..., description="Dataset URI (dataset:{project_code}:{schema}.{table})"),
    session: Session = Depends(get_session),
):
    """Detail for one discovered dataset — the subset a dataset can offer (columns,
    profiling, FK/ERD, description) plus project provenance and a `promoted` flag."""
    settings = _get_settings(session)
    try:
        with _neo4j_from_settings(settings) as ns:
            row = ns.run(DATASET_DETAIL, uri=uri).single()
    except Exception as e:
        raise HTTPException(500, f"Neo4j query failed: {e}")
    if not row:
        raise HTTPException(404, "Dataset not found")
    shaped = dict(row)
    pc = _project_code_from_dataset_uri(uri)
    meta = _project_provenance_map(session).get(pc, {})
    shaped["project_code"] = pc
    shaped["project_name"] = meta.get("project_name")
    shaped["archetype"] = meta.get("archetype")
    shaped["project_id"] = meta.get("project_id")
    # sort columns by ordinal for a stable schema view
    cols = shaped.get("columns") or []
    shaped["columns"] = sorted(cols, key=lambda c: (c.get("ordinal") is None, c.get("ordinal") or 0))
    return shaped


# Cached rubric id → short_label map, used by the marketplace list + detail
# endpoints to stamp the chip label per product. Resolved lazily on first
# call; the rubric YAML loader caches across calls anyway.
_RUBRIC_SHORT_LABEL_CACHE: dict[str, str] | None = None


def _rubric_short_label_map() -> dict[str, str]:
    global _RUBRIC_SHORT_LABEL_CACHE
    if _RUBRIC_SHORT_LABEL_CACHE is not None:
        return _RUBRIC_SHORT_LABEL_CACHE
    try:
        from .. import osi as osi_engine
        out: dict[str, str] = {}
        for r in osi_engine.list_rubrics():
            rid = (r.get("id") or "").lower()
            if rid:
                out[rid] = r.get("short_label") or r.get("label") or "OSI"
        # Always cover the default even when no YAMLs are found.
        out.setdefault("osi", "OSI")
        _RUBRIC_SHORT_LABEL_CACHE = out
        return out
    except Exception:
        _RUBRIC_SHORT_LABEL_CACHE = {"osi": "OSI"}
        return _RUBRIC_SHORT_LABEL_CACHE


def _parse_json(v: Any) -> Any:
    """Parse a stringified JSON field stored on the graph. Returns {} / [] /
    None for empty/invalid values so the frontend doesn't have to."""
    if v in (None, ""):
        return None
    if not isinstance(v, str):
        return v
    try:
        return json.loads(v)
    except Exception:
        return v


def _not_empty(d: dict, key_fields: tuple[str, ...]) -> bool:
    """True if any key_field on the dict has a non-empty value. Cypher's
    ``collect()`` on an OPTIONAL MATCH that didn't match still emits one map
    full of nulls; this filters those out."""
    return any(d.get(k) not in (None, "") for k in key_fields)


def _shape_detail(row: dict) -> dict:
    """Normalize the PRODUCT_DETAIL row into the shape the frontend expects."""
    result = dict(row)

    if result.get("published_at"):
        result["published_at"] = str(result["published_at"])
    if result.get("osi_evaluated_at"):
        result["osi_evaluated_at"] = str(result["osi_evaluated_at"])
    if result.get("qa_evaluated_at"):
        result["qa_evaluated_at"] = str(result["qa_evaluated_at"])

    # Stamp rubric short_label so the readiness chip on the detail page
    # renders against the contract's actual rubric (e.g. 'AI-Ready'
    # instead of 'OSI').
    rubric_id = (result.get("scoring_rubric") or "osi").lower()
    result["rubric_short_label"] = _rubric_short_label_map().get(rubric_id, "OSI")

    # Filter empty collection rows emitted by OPTIONAL MATCH + collect.
    result["owners"] = [o for o in (result.get("owners") or []) if _not_empty(o, ("name", "username", "email"))]
    result["stewards"] = [s for s in (result.get("stewards") or []) if _not_empty(s, ("name", "username", "email"))]
    result["team"] = [t for t in (result.get("team") or []) if _not_empty(t, ("username", "name", "email"))]
    result["roles"] = [r for r in (result.get("roles") or []) if _not_empty(r, ("role",))]

    servers = []
    for srv in (result.get("servers") or []):
        if not _not_empty(srv, ("name", "type", "environment", "database")):
            continue
        srv = dict(srv)
        srv["datasets"] = _parse_json(srv.get("datasets")) or []
        servers.append(srv)
    result["servers"] = servers

    # OPTIONAL MATCH + collect() emits one null-valued map when no rows
    # matched — filter those so the Quality tab count matches what the
    # tab actually renders.
    result["quality_rules"] = [
        q for q in (result.get("quality_rules") or [])
        if _not_empty(q, ("rule", "name", "description"))
    ]
    result["slas"] = [s for s in (result.get("slas") or []) if _not_empty(s, ("property", "value"))]

    # Stringify deployedAt on each serving entry — it's a Neo4j datetime in
    # the row, which FastAPI's default JSON encoder can't serialize. Same
    # treatment published_at / osi_evaluated_at get a few lines up.
    cleaned_serving = []
    for srv in (result.get("serving") or []):
        if not isinstance(srv, dict):
            continue
        if not srv.get("servingMode"):
            continue  # empty row from the OPTIONAL MATCH (no serving definition)
        srv = dict(srv)
        for dt_field in ("deployedAt", "builtAt"):
            if srv.get(dt_field) is not None:
                srv[dt_field] = str(srv[dt_field])
        cleaned_serving.append(srv)
    result["serving"] = cleaned_serving
    # Each dataset's columns array can also carry empty rows from an outer
    # OPTIONAL MATCH where no :DProdColumn existed.
    datasets = []
    for ds in (result.get("datasets") or []):
        if not _not_empty(ds, ("name", "physicalName")):
            continue
        ds = dict(ds)
        ds["columns"] = [c for c in (ds.get("columns") or []) if _not_empty(c, ("name", "physicalType"))]
        # Parse the per-dataset :DatasetTransform sidecar (when present)
        # into the same snake_case shape the engineer-side endpoint returns
        # — `joins`, `dedupe`, `grouping_keys`, `scd_policy`, etc.
        raw_xform = ds.get("transform")
        if isinstance(raw_xform, dict):
            ds["transform"] = {
                "filter": (raw_xform.get("filter") or "").strip(),
                "dedupe": _parse_json(raw_xform.get("dedupeJson")) or None,
                "joins": _parse_json(raw_xform.get("joinsJson")) or [],
                "grouping_keys": _parse_json(raw_xform.get("groupingKeysJson")) or [],
                "window_specs": _parse_json(raw_xform.get("windowSpecsJson")) or {},
                "scd_policy": _parse_json(raw_xform.get("scdPolicyJson")) or None,
                "suppressed_columns": _parse_json(raw_xform.get("suppressedColumnsJson")) or [],
                "grain_prose": (raw_xform.get("grainProse") or "").strip(),
            }
            # Collapse to None when every field is empty so the UI can skip
            # rendering an empty "Shape" panel instead of showing nothing.
            t = ds["transform"]
            if (
                not t["filter"] and not t["dedupe"] and not t["joins"]
                and not t["grouping_keys"] and not t["window_specs"]
                and not t["scd_policy"] and not t["suppressed_columns"]
                and not t["grain_prose"]
            ):
                ds["transform"] = None
        else:
            ds["transform"] = None
        datasets.append(ds)
    result["datasets"] = datasets

    # QA evaluation — pull the questions + near-miss gaps out of the
    # stringified JSON blobs and compute `qa_stale` from the version
    # comparison so the UI can render a "regenerate" badge.
    qa_uri = result.pop("qa_uri", None)
    qa_questions = _parse_json(result.pop("qa_questions_json", None)) or []
    qa_near_miss = _parse_json(result.pop("qa_near_miss_gaps_json", None)) or []
    qa_generated_for = result.pop("qa_generated_for_version", None)
    qa_current_change = result.pop("qa_current_change_version", None)
    qa_narrative = result.pop("qa_narrative", None) or ""
    qa_mode = result.pop("qa_mode", None) or ""
    qa_analyzer_version = result.pop("qa_analyzer_version", None) or ""
    qa_advisor_error = result.pop("qa_advisor_error", None) or None
    qa_evaluated_at = result.pop("qa_evaluated_at", None)
    if qa_uri:
        qa_stale = (
            qa_generated_for is not None
            and qa_current_change is not None
            and qa_generated_for < qa_current_change
        )
        result["qa_evaluation"] = {
            "uri": qa_uri,
            "mode": qa_mode,
            "questions": qa_questions if isinstance(qa_questions, list) else [],
            "near_miss_gaps": qa_near_miss if isinstance(qa_near_miss, list) else [],
            "narrative": qa_narrative,
            "generated_for_version": qa_generated_for,
            "current_change_version": qa_current_change,
            "stale": qa_stale,
            "analyzer_version": qa_analyzer_version,
            "advisor_error": qa_advisor_error,
            "evaluated_at": qa_evaluated_at,
        }
    else:
        result["qa_evaluation"] = None

    # Back-compat primary-owner fields the existing overview card already renders.
    primary = result["owners"][0] if result["owners"] else {}
    result["owner_name"] = primary.get("name") or primary.get("username") or None
    result["owner_role"] = primary.get("role") or None
    result["owner_email"] = primary.get("email") or None

    result["tags"] = list(result.get("tags") or [])
    result["support"] = _parse_json(result.pop("support_json", None)) or None
    result["custom_properties"] = _parse_json(result.pop("custom_properties_json", None)) or None

    return result


# Walk the version sidecar timeline for a product. Used both inline by the
# default detail endpoint (so the UI can render a version dropdown) and by
# the standalone /revisions endpoint (for the timeline panel).
_AVAILABLE_VERSIONS_QUERY = """\
MATCH (dp:DProdDataProduct {uri: $uri})<-[:MATERIALISES_AS]-(dc:DataContract)
MATCH (dc)-[:HAS_VERSION]->(cv:ContractVersion)
RETURN cv.version       AS version,
       cv.lifecycleState AS lifecycle_state,
       cv.changeKind    AS change_kind,
       cv.revisionNotes AS revision_notes,
       cv.publishedAt   AS published_at,
       cv.occurredAt    AS occurred_at,
       cv.actor         AS actor,
       (cv.version = dc.currentVersion) AS is_current
ORDER BY cv.version DESC
"""


# Historical view: render the product as it looked at a specific version.
# We re-run the same PRODUCT_DETAIL walk but pin cv to the requested version
# instead of auto-selecting "latest deployed". Substructure walks reuse the
# same temporal-edge filtering — they were already version-aware via cv.version.
_HISTORICAL_PRODUCT_DETAIL = PRODUCT_DETAIL.replace(
    """OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv_pub:ContractVersion)
WHERE cv_pub.lifecycleState IN ['published','superseded']
WITH dp, dc, cv_pub ORDER BY cv_pub.version DESC
WITH dp, dc, head(collect(cv_pub)) AS cv_deployed
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv_cur:ContractVersion {version: dc.currentVersion})
WITH dp, dc, coalesce(cv_deployed, cv_cur) AS cv, cv_deployed, cv_cur""",
    """MATCH (dc)-[:HAS_VERSION]->(cv:ContractVersion {version: $pin_version})
WITH dp, dc, cv, cv AS cv_deployed, cv AS cv_cur""",
)


# Historical column list — walks :DataContractProperty (which IS versioned)
# rather than :DProdColumn (which is always current). When the marketplace
# detail is pinned to a historical version, we overlay these columns over
# the response so the user sees the schema as-of that version.
_HISTORICAL_COLUMNS_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})-[r1:HAS_SCHEMA]->(s:DataContractSchema)
WHERE r1.fromVersion <= $version AND (r1.toVersion IS NULL OR r1.toVersion >= $version)
OPTIONAL MATCH (dc)-[r2:HAS_PROPERTY]->(p:DataContractProperty)
WHERE r2.fromVersion <= $version AND (r2.toVersion IS NULL OR r2.toVersion >= $version)
  AND p.schemaName = s.physicalName
WITH s, p
WHERE p IS NOT NULL
RETURN
    coalesce(s.physicalName, s.name, '') AS dataset_physical_name,
    coalesce(s.name, '')                 AS dataset_name,
    coalesce(s.description, '')          AS dataset_description,
    collect({
        name: coalesce(p.name, ''),
        logicalName: coalesce(p.logicalName, ''),
        logicalType: coalesce(p.logicalType, ''),
        physicalType: coalesce(p.physicalType, ''),
        description: coalesce(p.description, ''),
        primaryKey: coalesce(p.primaryKey, false),
        sensitivity: coalesce(p.sensitivity, 'none')
    }) AS columns
ORDER BY dataset_name
"""


def _overlay_historical_columns(ns, shaped: dict, contract_id: str, version: int) -> None:
    """Replace shaped['datasets'][*]['columns'] with the version-pinned set
    from :DataContractProperty. Mutates ``shaped`` in place."""
    rows = list(ns.run(_HISTORICAL_COLUMNS_QUERY, contract_id=contract_id, version=version))
    historical_by_phys = {
        r["dataset_physical_name"]: {
            "name": r["dataset_name"],
            "physicalName": r["dataset_physical_name"],
            "description": r["dataset_description"],
            "columns": list(r["columns"]),
        }
        for r in rows
    }
    if not historical_by_phys:
        return  # nothing to overlay
    # Replace datasets entirely with the historical set so columns that
    # existed at v(n) but not at v(current) still surface.
    shaped["datasets"] = [
        {
            **historical_by_phys[ds_phys],
            # Preserve relationshipKind from the current dprod chain if we
            # had it; the historical schema doesn't carry it.
            "relationshipKind": next(
                (d.get("relationshipKind") for d in (shaped.get("datasets") or [])
                 if d.get("physicalName") == ds_phys),
                "",
            ),
        }
        for ds_phys in historical_by_phys
    ]


@router.get("/detail")
def get_product_detail(
    uri: str = QParam(..., description="Data product URI"),
    version: Optional[int] = QParam(None, description="Pin to a specific contract version (historical view)"),
    session: Session = Depends(get_session),
):
    """Get detailed info for a single published data product.

    Default (no ``version`` param) auto-pins to the latest deployed
    :ContractVersion (published or superseded), falling back to the current
    draft when nothing's been deployed yet. ``?version=N`` re-renders the
    product as it looked at that historical version — used by the
    marketplace's version dropdown.
    """
    settings = _get_settings(session)
    try:
        with _neo4j_from_settings(settings) as ns:
            if version is not None:
                row = ns.run(_HISTORICAL_PRODUCT_DETAIL, uri=uri, pin_version=version).single()
            else:
                row = ns.run(PRODUCT_DETAIL, uri=uri).single()
            if not row:
                raise HTTPException(404, "Product not found")
            shaped = _shape_detail(dict(row))
            # Available versions sidecar — drives the version dropdown.
            versions = [dict(r) for r in ns.run(_AVAILABLE_VERSIONS_QUERY, uri=uri)]
            for v in versions:
                # datetime objects → ISO strings for JSON-serializability.
                for k in ("published_at", "occurred_at"):
                    if v.get(k) is not None:
                        v[k] = str(v[k])
            shaped["available_versions"] = versions
            # If the caller pinned a version explicitly, surface that too so
            # the UI can render a "Viewing v1 (superseded)" callout.
            shaped["viewing_version"] = version
            # The dprod operational chain is current-only by design (rebuilt
            # by _generate_dprod from whatever is dc.currentVersion). So
            # whenever the rendered cv.version differs from currentVersion
            # we overlay the version-pinned column set from
            # :DataContractProperty (which IS versioned). This covers two
            # cases:
            #   (a) explicit ?version=N pin (historical view)
            #   (b) auto-pin to latest deployed when the source has an
            #       in-flight draft beyond it (has_inflight_edit==true)
            shown_version = shaped.get("lifecycle_version")
            current_version = next(
                (v["version"] for v in versions if v.get("is_current")),
                shown_version,
            )
            if shaped.get("contract_id") and shown_version is not None and shown_version != current_version:
                _overlay_historical_columns(ns, shaped, shaped["contract_id"], shown_version)
            # Same project_id resolution the list endpoint does, so the PO's
            # Deploy button on the detail page can POST /projects/{id}/odcs/publish
            # without a second round-trip to find the project.
            _attach_project_id(shaped, session)
            return shaped
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Query failed: {e}")


# ── Marketplace preview ───────────────────────────────────────────────────
#
# Phase 1: SELECT 50 rows from a deployed virtual view, returned as a
# {columns, rows, truncated, durationMs} envelope. Inherits no lifecycle
# pinning beyond MATCHing the contract by id — the :DProdDataProduct and
# :ServingDefinition are current-only by design (rebuilt by _generate_dprod
# from whatever is dc.currentVersion). When a PO edits a published
# product, NewProductWizard.ensureProjectAndSaveSpec skips /generate-dprod
# so the deployed dprod stays pinned to the published version, which
# means preview against an in-flight edit still shows the deployed v1.

_MARKETPLACE_RESOLVE_VIEW_QUERY = """
MATCH (dc:DataContract {id: $contract_id})
MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
MATCH (dp)-[:SERVED_BY]->(sd:ServingDefinition {servingMode: 'virtual_view'})
WITH dp, sd
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
WITH dp, sd, collect({
    uri: ods.uri,
    physicalName: ods.physicalName,
    description: ods.description,
    relationshipKind: ods.relationshipKind
}) AS datasets
RETURN dp.uri AS product_uri,
       coalesce(sd.deployedTo, sd.viewSchema, 'public') AS view_schema,
       coalesce(sd.deployedViewNames, sd.viewNames, '[]') AS view_names_json,
       sd.viewName AS primary_view_name,
       coalesce(sd.deploymentStatus, 'pending') AS deployment_status,
       datasets
"""

# Per-dataset contract columns for the picked output dataset. Returns EVERY
# :DProdColumn name (with its description, which may be empty) so the preview
# can both (a) show the same header tooltip the marketplace Datasets tab
# surfaces and (b) diff the physical columns against the published schema:
# a physical column absent from this set is a load-framework "system column"
# (e.g. dlt's _dlt_id / _dlt_load_id) hidden by default in the Preview. The
# full name set is load-bearing for that diff — a real column with no
# description must NOT be flagged, so we can't filter on description here.
# Keyed on the dataset URI so multi-dataset products don't cross views.
_MARKETPLACE_COLUMN_DESCRIPTIONS_QUERY = """
MATCH (ods:DProdOutputDataset {uri: $dataset_uri})
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
RETURN pc.name AS name, pc.description AS description
"""


def _enrich_preview_columns(result_columns, meta_rows):
    """Attach ``description`` + ``in_schema`` to each physical preview column.

    ``in_schema`` is False for a physical column whose name has no matching
    :DProdColumn for the selected output dataset — i.e. a column added by the
    load/EL framework (dlt's ``_dlt_id`` / ``_dlt_load_id``, a future
    ``_airbyte_*`` / ``_fivetran_*``, …) that isn't part of the product's
    published schema. The UI hides these by default behind a "System columns"
    toggle. Detection is framework-agnostic (a name diff, not a hard-coded
    prefix list) and **fails open**: when the contract column set can't be
    resolved (``meta_rows == []`` — no picked dataset / graph miss) every
    column is treated as ``in_schema: True`` so we never hide a column we
    couldn't classify (e.g. a virtual-view product whose SELECT is already
    contract-shaped).

    Matching is CASE-INSENSITIVE: a physical column comes back UPPER on Snowflake
    (objects created by unquoted DDL fold to UPPER) while the :DProdColumn schema
    stores the logical (lower) name, so a case-sensitive compare would flag every
    real column as a system column and hide it. Folding both sides is a no-op on
    Postgres/Databricks (physical name already equals the logical one)."""
    descriptions = {
        (r.get("name") or "").lower(): r["description"]
        for r in meta_rows
        if (r.get("description") or "").strip()
    }
    contract_names = {(r.get("name") or "").lower() for r in meta_rows}
    flag = bool(contract_names)  # fail open when we couldn't resolve the schema
    return [
        {
            **c,
            "description": descriptions.get((c.get("name") or "").lower()) or None,
            "in_schema": ((c.get("name") or "").lower() in contract_names) if flag else True,
        }
        for c in result_columns
    ]


# Materialized (dbt) serving — physical tables, no view DDL. Preview SELECTs
# straight from the built table in targetSchema. Table name == the dbt model
# name == _safe_name(physicalName).
_MARKETPLACE_RESOLVE_MATERIALIZED_QUERY = """
MATCH (dc:DataContract {id: $contract_id})
MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
MATCH (dp)-[:SERVED_BY]->(sd:ServingDefinition {servingMode: 'dbt_materialized'})
WITH dp, sd
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
WITH dp, sd, collect({
    uri: ods.uri, physicalName: ods.physicalName,
    description: ods.description, relationshipKind: ods.relationshipKind
}) AS datasets
RETURN dp.uri AS product_uri,
       coalesce(sd.targetSchema, 'public') AS target_schema,
       coalesce(sd.buildStatus, 'pending') AS build_status,
       coalesce(sd.modelsJson, '[]') AS models_json,
       datasets
"""


# Cross-platform transfer serving — the dlt transfer loaded rows into the
# TARGET platform (postgres/mysql/snowflake/databricks). Preview SELECTs from
# the target table recorded in the serving definition's summaryJson (the
# authoritative record of WHERE the data landed at run time — the live
# MaterializationTarget namespace may have drifted since). Only the target
# connection is resolved live (that row is stable).
_MARKETPLACE_RESOLVE_TRANSFER_QUERY = """
MATCH (dc:DataContract {id: $contract_id})
MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
MATCH (dp)-[:SERVED_BY]->(sd:ServingDefinition {servingMode: 'transfer_then_transform'})
WITH dp, sd
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
WITH dp, sd, collect({
    uri: ods.uri, physicalName: ods.physicalName,
    description: ods.description, relationshipKind: ods.relationshipKind
}) AS datasets
RETURN dp.uri AS product_uri,
       coalesce(sd.deploymentStatus, 'pending') AS deployment_status,
       coalesce(sd.targetSchema, 'public') AS target_schema,
       coalesce(sd.summaryJson, '{}') AS summary_json,
       datasets
"""


def _safe_table_name(name: str) -> str:
    import re as _re
    return _re.sub(r"[^a-zA-Z0-9_]", "_", name or "").lower()


def _materialized_preview(ns, sql_executor, contract_id, project_code, pg_connection, body,
                          platform: str = "postgres", connection_ref: dict | None = None):
    """Preview a materialized product's physical table. Returns the same shape
    as the view preview, or None when there's no materialized serving def."""
    rows = list(ns.run(_MARKETPLACE_RESOLVE_MATERIALIZED_QUERY, contract_id=contract_id))
    if not rows:
        return None
    row = dict(rows[0])
    if row.get("build_status") != "built":
        return {"error": "not_deployed", "deployment_status": row.get("build_status")}

    schema = row.get("target_schema") or "public"
    try:
        models = json.loads(row.get("models_json") or "[]")
    except (TypeError, ValueError):
        models = []
    model_names = [m.get("model") for m in models if isinstance(m, dict) and m.get("model")]

    picked_dataset = None
    table = None
    if body.dataset_uri:
        ds = next((d for d in row.get("datasets") or [] if d.get("uri") == body.dataset_uri), None)
        if ds and ds.get("physicalName"):
            want = _safe_table_name(ds["physicalName"])
            table = next((m for m in model_names if m == want), None)
            if table:
                picked_dataset = ds
    if not table:
        table = model_names[0] if model_names else None
        if table and not picked_dataset:
            picked_dataset = next(
                (d for d in row.get("datasets") or [] if _safe_table_name(d.get("physicalName") or "") == table),
                None,
            )
    if not table:
        raise HTTPException(409, "Could not resolve a materialized table")

    # dbt-materialized tables are CREATEd by unquoted DDL → UPPER on Snowflake;
    # fold the read so it addresses the real physical object.
    sql = f'SELECT * FROM {quote_created_relation(schema, table, platform)}'
    result = sql_executor.execute_select(
        neo4j_session=ns, project_code=project_code, pg_connection=pg_connection, sql=sql,
        executed_by="anonymous-marketplace",
        max_rows=max(1, min(int(body.limit or 50), sql_executor.DEFAULT_MAX_ROWS)),
        product_uri=row.get("product_uri"), view_schema=schema,
        platform=platform, connection_ref=connection_ref,
    )
    meta_rows: list[dict] = []
    if result.status == "ok" and picked_dataset and picked_dataset.get("uri"):
        meta_rows = [dict(d) for d in ns.run(
            _MARKETPLACE_COLUMN_DESCRIPTIONS_QUERY, dataset_uri=picked_dataset["uri"]
        )]
    active_dataset = {
        "uri": picked_dataset.get("uri") if picked_dataset else None,
        "physicalName": picked_dataset.get("physicalName") if picked_dataset else None,
        "description": picked_dataset.get("description") if picked_dataset else None,
        "relationshipKind": picked_dataset.get("relationshipKind") if picked_dataset else None,
    }
    if result.status != "ok":
        return {
            "status": "failed", "error_class": result.error_class,
            "error_message": result.error_message, "duration_ms": result.duration_ms,
            "view_name": table, "view_schema": schema, "serving_mode": "dbt_materialized",
            "active_dataset": active_dataset,
        }
    enriched_columns = _enrich_preview_columns(result.columns, meta_rows)
    return {
        "status": "ok", "columns": enriched_columns, "rows": result.rows,
        "truncated": result.truncated, "row_count": result.row_count,
        "duration_ms": result.duration_ms, "view_name": table, "view_schema": schema,
        "serving_mode": "dbt_materialized", "active_dataset": active_dataset,
    }


def _transfer_preview(ns, sql_executor, session, project, contract_id, body):
    """Preview a cross-platform transfer product's rows from the TARGET platform.
    Returns the same shape as the view/materialized previews, or None when
    there's no transfer serving def (caller falls through to its 404/409).

    Namespace + table names come from the serving definition's ``summaryJson``
    (where the dlt load actually landed); only the target connection is resolved
    live via ``transfer_execution._resolve_target`` (the connection row is stable,
    the configured namespace can drift after a run)."""
    from .. import transfer_execution

    rows = list(ns.run(_MARKETPLACE_RESOLVE_TRANSFER_QUERY, contract_id=contract_id))
    if not rows:
        return None
    row = dict(rows[0])
    if row.get("deployment_status") != "deployed":
        return {"error": "not_deployed", "deployment_status": row.get("deployment_status")}

    try:
        summary = json.loads(row.get("summary_json") or "{}")
    except (TypeError, ValueError):
        summary = {}
    # target_namespace is "catalog.schema" (3-level) or a bare schema (2-level);
    # fall back to targetSchema when summary predates the namespace field.
    namespace = summary.get("target_namespace") or row.get("target_schema") or "public"
    tables = summary.get("tables") or []
    # Each table entry records its landed target as "schema.table"; the bare
    # table name is the last segment.
    bare_tables = [
        str(t.get("target", "")).rsplit(".", 1)[-1]
        for t in tables if isinstance(t, dict) and t.get("target")
    ]
    bare_tables = [t for t in bare_tables if t]

    picked_dataset = None
    bare_table = None
    if body.dataset_uri:
        ds = next((d for d in row.get("datasets") or [] if d.get("uri") == body.dataset_uri), None)
        if ds and ds.get("physicalName"):
            want = _safe_table_name(ds["physicalName"])
            bare_table = next((b for b in bare_tables if b == want), None)
            if bare_table:
                picked_dataset = ds
    if not bare_table:
        bare_table = bare_tables[0] if bare_tables else None
        if bare_table and not picked_dataset:
            picked_dataset = next(
                (d for d in row.get("datasets") or []
                 if _safe_table_name(d.get("physicalName") or "") == bare_table),
                None,
            )
    if not bare_table:
        raise HTTPException(409, "Could not resolve a transferred table")

    try:
        platform, connection_ref = transfer_execution._resolve_target(project, session)
    except Exception as e:  # no target connection / connection row missing
        return {
            "status": "failed", "error_class": "no_target_connection",
            "error_message": str(e), "view_name": bare_table, "view_schema": namespace,
            "serving_mode": "transfer_then_transform",
        }

    # dlt loads rows via unquoted DDL → objects are UPPER on Snowflake; fold the
    # read to match the real physical table (the reported preview `sql_error`).
    sql = f"SELECT * FROM {quote_created_relation(namespace, bare_table, platform)}"
    result = sql_executor.execute_select(
        neo4j_session=ns, project_code=project.project_code, pg_connection="", sql=sql,
        executed_by="anonymous-marketplace",
        max_rows=max(1, min(int(body.limit or 50), sql_executor.DEFAULT_MAX_ROWS)),
        product_uri=row.get("product_uri"), view_schema=namespace,
        platform=platform, connection_ref=connection_ref,
    )
    meta_rows: list[dict] = []
    if result.status == "ok" and picked_dataset and picked_dataset.get("uri"):
        meta_rows = [dict(d) for d in ns.run(
            _MARKETPLACE_COLUMN_DESCRIPTIONS_QUERY, dataset_uri=picked_dataset["uri"]
        )]
    active_dataset = {
        "uri": picked_dataset.get("uri") if picked_dataset else None,
        "physicalName": picked_dataset.get("physicalName") if picked_dataset else None,
        "description": picked_dataset.get("description") if picked_dataset else None,
        "relationshipKind": picked_dataset.get("relationshipKind") if picked_dataset else None,
    }
    if result.status != "ok":
        return {
            "status": "failed", "error_class": result.error_class,
            "error_message": result.error_message, "duration_ms": result.duration_ms,
            "view_name": bare_table, "view_schema": namespace,
            "serving_mode": "transfer_then_transform", "active_dataset": active_dataset,
        }
    enriched_columns = _enrich_preview_columns(result.columns, meta_rows)
    return {
        "status": "ok", "columns": enriched_columns, "rows": result.rows,
        "truncated": result.truncated, "row_count": result.row_count,
        "duration_ms": result.duration_ms, "view_name": bare_table, "view_schema": namespace,
        "serving_mode": "transfer_then_transform", "active_dataset": active_dataset,
    }


class _MarketplacePreviewBody(BaseModel):
    dataset_uri: Optional[str] = None
    limit: int = 50


@router.post("/products/{contract_id}/preview")
def marketplace_preview(
    contract_id: str,
    body: _MarketplacePreviewBody | None = None,
    session: Session = Depends(get_session),
):
    """Return a row preview for a deployed product view.

    Returns ``{error: 'not_deployed'}`` (200) when the product has a
    :ServingDefinition but hasn't been deployed yet — the UI surfaces
    an empty state. 404 only on missing contract / serving definition.
    """
    from .. import sql_executor

    body = body or _MarketplacePreviewBody()

    # Resolve the project that owns this contract so we can use its
    # pg_connection. Contract ids are always ``{project_code}-contract``.
    if not contract_id.endswith("-contract"):
        raise HTTPException(404, "Contract not found")
    project_code = contract_id[: -len("-contract")]
    project = session.exec(select(Project).where(Project.project_code == project_code)).first()
    if not project:
        raise HTTPException(404, "Project not found for contract")

    # Resolve where to preview from. Served-location-first (a consumer over a
    # materialized source previews against the served tables, e.g. Databricks);
    # else SourceBinding / Postgres :CONSUMES origin-borrow.
    from ..pg_resolver import resolve_read_connection_for_consumer
    from .connections import build_connection_string
    _platform, _conn_ref, _ = resolve_read_connection_for_consumer(
        project, session, contract_id
    )
    # Transient Postgres DSN from the structured ref (empty for non-Postgres,
    # which ride connection_ref) — the single connection contract.
    pg_connection = (build_connection_string(_platform, _conn_ref)
                     if _platform in ("postgres", "postgresql") else "")
    # NOTE: don't raise "no source connection" here — a cross-platform transfer
    # product has no origin/Postgres connection at all (its rows live in the
    # served target, e.g. Databricks) and is previewed via _transfer_preview
    # below. The Postgres guard is deferred to the virtual-view path that
    # actually needs pg_connection.

    settings = _get_settings(session)
    with _neo4j_from_settings(settings) as ns:
        rows = list(ns.run(_MARKETPLACE_RESOLVE_VIEW_QUERY, contract_id=contract_id))
        if not rows:
            # No virtual view — try the materialized (dbt) serving definition
            # and preview the physical table instead.
            mat = _materialized_preview(
                ns, sql_executor, contract_id, project_code,
                pg_connection, body, platform=_platform,
                connection_ref=_conn_ref if _platform not in ("postgres", "postgresql") else None,
            )
            if mat is not None:
                return mat
            # ... or a cross-platform transfer product (rows live in the target).
            xfer = _transfer_preview(ns, sql_executor, session, project, contract_id, body)
            if xfer is not None:
                return xfer
            raise HTTPException(404, "No serving definition found for this product")
        row = dict(rows[0])
        if row.get("deployment_status") != "deployed":
            return {"error": "not_deployed", "deployment_status": row.get("deployment_status")}

        # Virtual-view preview runs against the Postgres source — this path
        # genuinely needs the connection (the transfer/materialized fallbacks
        # above resolve their own targets and were already tried).
        if _platform in ("postgres", "postgresql") and not pg_connection:
            raise HTTPException(409, "No source connection available for preview")

        # Same view-name parsing the engineer endpoint uses.
        raw = row.get("view_names_json")
        if isinstance(raw, list):
            view_names = [str(x) for x in raw if x]
        else:
            try:
                parsed = json.loads(raw) if raw else []
                view_names = [str(x) for x in parsed if x] if isinstance(parsed, list) else []
            except (TypeError, ValueError):
                view_names = []
        bare_names = [v.rsplit(".", 1)[-1] for v in view_names]
        primary = row.get("primary_view_name")

        target_view = None
        picked_dataset = None
        if body.dataset_uri:
            ds = next(
                (d for d in row.get("datasets") or []
                 if d.get("uri") == body.dataset_uri),
                None,
            )
            if ds and ds.get("physicalName"):
                # Exact match only. The `endswith` fallback used to mis-route
                # `employee` → `vw_department_employee` on multi-table products
                # — the deployment reflector caught it in Phase 2 smoke testing.
                expected = f"vw_{ds['physicalName']}"
                target_view = next((b for b in bare_names if b == expected), None)
                if target_view:
                    picked_dataset = ds
        if not target_view:
            target_view = primary or (bare_names[0] if bare_names else None)
            if target_view and not picked_dataset:
                picked_physical = target_view.removeprefix("vw_")
                picked_dataset = next(
                    (d for d in row.get("datasets") or []
                     if d.get("physicalName") == picked_physical),
                    None,
                )
        if not target_view:
            raise HTTPException(409, "Could not resolve a deployed view")

        view_schema = row.get("view_schema") or "public"
        # Virtual views are created unquoted → UPPER on Snowflake (Tier 2f); fold
        # the read to match (no-op on non-upper-folding platforms).
        sql = f'SELECT * FROM {quote_created_relation(view_schema, target_view, _platform)}'
        result = sql_executor.execute_select(
            neo4j_session=ns,
            project_code=project_code,
            pg_connection=pg_connection,
            sql=sql,
            executed_by="anonymous-marketplace",
            max_rows=max(1, min(int(body.limit or 50), sql_executor.DEFAULT_MAX_ROWS)),
            product_uri=row.get("product_uri"),
            view_schema=view_schema,
            platform=_platform,
            connection_ref=_conn_ref if _platform not in ("postgres", "postgresql") else None,
        )

        # Contract columns for the picked dataset — only worth a graph
        # round-trip when the underlying query succeeded. Drives both the
        # header tooltip and the system-column diff (see
        # _enrich_preview_columns).
        meta_rows: list[dict] = []
        if result.status == "ok" and picked_dataset and picked_dataset.get("uri"):
            meta_rows = [dict(d) for d in ns.run(
                _MARKETPLACE_COLUMN_DESCRIPTIONS_QUERY,
                dataset_uri=picked_dataset["uri"],
            )]

        active_dataset = {
            "uri": picked_dataset.get("uri") if picked_dataset else None,
            "physicalName": picked_dataset.get("physicalName") if picked_dataset else None,
            "description": picked_dataset.get("description") if picked_dataset else None,
            "relationshipKind": picked_dataset.get("relationshipKind") if picked_dataset else None,
        }

        if result.status != "ok":
            return {
                "status": "failed",
                "error_class": result.error_class,
                "error_message": result.error_message,
                "duration_ms": result.duration_ms,
                "view_name": target_view,
                "view_schema": view_schema,
                "active_dataset": active_dataset,
            }
        enriched_columns = _enrich_preview_columns(result.columns, meta_rows)
        return {
            "status": "ok",
            "columns": enriched_columns,
            "rows": result.rows,
            "truncated": result.truncated,
            "row_count": result.row_count,
            "duration_ms": result.duration_ms,
            "view_name": target_view,
            "view_schema": view_schema,
            "active_dataset": active_dataset,
        }


# ── Marketplace Q&A execution (Phase 3a, skill-based) ─────────────────────
#
# The data-product-question-executor skill authors the SQL; the backend
# validates it against an allow-list of the product's deployed views and
# runs it through sql_executor's SELECT-only gate. Three safety layers:
# (1) skill prompted with allow-list and refusal protocol, (2) backend
# allow-list parse, (3) sql_executor SELECT-only regex gate.


class _ExecuteQaBody(BaseModel):
    # Question source: question_index picks from the latest
    # :QAEvaluation.questions[]. inline_supporting_columns is a test-only
    # path that bypasses the skill and runs a bare SELECT — used by
    # smoke tests; not surfaced in the UI.
    question_index: Optional[int] = None
    inline_supporting_columns: Optional[list[str]] = None
    question_text: Optional[str] = None
    limit: int = 100


def _resolve_question_from_index(
    project: Project, contract_id: str, body: _ExecuteQaBody,
) -> dict[str, Any]:
    """Pull a question dict (with text + supporting_columns + category etc.)
    out of the latest :QAEvaluation by index. Raises HTTPException on miss."""
    from .. import qa as qa_engine
    eval_payload = qa_engine.read_latest_evaluation(project, contract_id)
    if not eval_payload:
        raise HTTPException(409, "No QA evaluation found — generate questions first")
    questions = eval_payload.get("questions") or []
    idx = body.question_index if body.question_index is not None else 0
    if idx < 0 or idx >= len(questions):
        raise HTTPException(404, f"Question index {idx} out of range (have {len(questions)})")
    q = questions[idx] or {}
    return q


@router.post("/products/{contract_id}/qa/execute")
async def marketplace_qa_execute(
    contract_id: str,
    body: _ExecuteQaBody | None = None,
    session: Session = Depends(get_session),
):
    """Execute a curated question by handing it to the question-executor
    skill, then validating + running the returned SQL.

    Returns ``{status, sql, columns, rows, truncated, row_count,
    duration_ms, view_name, view_schema, explanation, aggregation_kind,
    confidence, question_text}`` or ``{error: 'not_deployed', ...}`` or
    ``{status: 'failed', error_class, error_message, ...}`` or
    ``{status: 'refused', refused_reason, ...}``.
    """
    import asyncio
    from .. import sql_executor
    from .. import qa_execute as qe
    from .. import dialect_sql
    from ..pg_resolver import resolve_read_connection_for_consumer

    body = body or _ExecuteQaBody()
    if not contract_id.endswith("-contract"):
        raise HTTPException(404, "Contract not found")
    project_code = contract_id[: -len("-contract")]
    project = session.exec(select(Project).where(Project.project_code == project_code)).first()
    if not project:
        raise HTTPException(404, "Project not found for contract")

    _platform, _conn_ref, _ = resolve_read_connection_for_consumer(
        project, session, contract_id
    )
    # A Postgres source with no resolvable structured connection is a clean 409;
    # non-Postgres rides connection_ref into the executor. The DSN itself is
    # derived transiently by qa_execute at the execute boundary.
    if _platform in ("postgres", "postgresql") and not qe._source_dsn(_platform, _conn_ref):
        raise HTTPException(409, "No source connection available")

    # ── Inline-supporting-columns test path: bypass the skill ─────────────
    # Smoke tests pass `inline_supporting_columns` directly. Bare bones —
    # no aggregation, no JOIN. Identifier check kept lax (dots OK) since
    # the skill-based path is what the UI uses.
    if body.inline_supporting_columns:
        return await _execute_inline_path(
            project=project,
            project_code=project_code,
            contract_id=contract_id,
            body=body,
            platform=_platform,
            connection_ref=_conn_ref,
            session=session,
        )

    # ── Skill-based path ──────────────────────────────────────────────────
    question = _resolve_question_from_index(project, contract_id, body)
    question_text = body.question_text or question.get("text") or ""

    # Pre-gather: deployed view metadata + sample rows. Raises RuntimeError
    # cleanly when not deployed.
    try:
        inputs = qe.gather_inputs(project, contract_id,
                                   platform_type=_platform, connection_ref=_conn_ref,
                                   session=session)
    except RuntimeError as e:
        msg = str(e)
        if "not deployed" in msg.lower():
            return {"error": "not_deployed", "deployment_status": "pending"}
        raise HTTPException(409, msg)

    # Call the skill via the SDK. Hard timeout — the executor should be
    # quick (~10-30s); if it stalls we surface a clean failure.
    try:
        payload, advisor_error = await asyncio.wait_for(
            qe.run_executor(question=question, inputs=inputs),
            timeout=qe.ADVISOR_TIMEOUT_SECONDS + 10,
        )
    except asyncio.TimeoutError:
        payload = qe.ExecutorPayload()
        advisor_error = f"Executor timed out after {qe.ADVISOR_TIMEOUT_SECONDS}s"

    if advisor_error:
        return {
            "status": "failed",
            "error_class": "advisor_error",
            "error_message": advisor_error,
            "question_text": question_text,
        }

    # The skill refused.
    if payload.refused_reason:
        return {
            "status": "refused",
            "refused_reason": payload.refused_reason,
            "aggregation_kind": payload.aggregation_kind,
            "confidence": payload.confidence,
            "question_text": question_text,
            "explanation": payload.explanation,
        }

    if not payload.sql:
        return {
            "status": "failed",
            "error_class": "no_sql",
            "error_message": "Executor returned neither SQL nor a refusal reason.",
            "question_text": question_text,
        }

    # Fix a multi-part namespace the executor quoted whole ("catalog.schema" →
    # "catalog"."schema") before allow-list + render, so a 3-level target
    # (Databricks/Snowflake) isn't rejected or mis-executed. Deterministic; a
    # no-op for 2-level / already-correct SQL.
    payload.sql = qe.canonicalize_namespace_quoting(
        payload.sql, [(inputs.view_schema, "")]
    )

    # Allow-list defense: every table reference must be in the product's
    # deployed views (or a CTE defined in the SQL itself).
    allow = qe.validate_sql_allowlist(
        payload.sql, inputs.view_schema, inputs.deployed_view_names,
    )
    if not allow.ok:
        return {
            "status": "failed",
            "sql": payload.sql,
            "error_class": "unsafe_table",
            "error_message": (
                "Executor referenced tables outside the deployed allow-list: "
                f"{allow.rejected_refs}. Allowed views: {inputs.deployed_view_names}."
            ),
            "explanation": payload.explanation,
            "aggregation_kind": payload.aggregation_kind,
            "confidence": payload.confidence,
            "question_text": question_text,
        }

    # Render the standard SQL the skill emitted to the target engine's native
    # dialect BEFORE execution — the allow-list above ran on the standard text
    # (its regex expects ANSI double-quoted / bare identifiers), so render must
    # follow it. Fail-open: on a parse/render error execute the original.
    # Render + execute against the connection `gather_inputs` resolved for this
    # serving mode: the SOURCE engine for a virtual view, the TARGET engine for a
    # cross-platform transfer product (where the rows actually landed).
    rendered = dialect_sql.render_for_platform(payload.sql, inputs.exec_platform)
    executed_sql = rendered.sql
    dialect_note = None if rendered.ok else f"dialect render skipped: {rendered.error}"

    # Execute through the shared gate. SELECT-only enforcement + timeout +
    # row cap + audit logging via :QueryRun all happen here.
    settings = _get_settings(session)
    with _neo4j_from_settings(settings) as ns:
        result = sql_executor.execute_select(
            neo4j_session=ns,
            project_code=project_code,
            pg_connection=qe._source_dsn(inputs.exec_platform, inputs.exec_connection_ref),
            sql=executed_sql,
            executed_by="qa-execute-skill",
            max_rows=max(1, min(int(body.limit or 100), sql_executor.DEFAULT_MAX_ROWS)),
            product_uri=inputs.product_uri,
            view_schema=inputs.view_schema,
            platform=inputs.exec_platform,
            # Non-Postgres platforms need host/port/credentials from connection_ref;
            # without it the driver falls back to localhost. Postgres reads the
            # transient DSN derived above, so connection_ref is redundant there.
            connection_ref=inputs.exec_connection_ref or None,
        )

    if result.status != "ok":
        return {
            "status": "failed",
            "sql": executed_sql,
            "standard_sql": payload.sql,
            "dialect_note": dialect_note,
            "error_class": result.error_class,
            "error_message": result.error_message,
            "duration_ms": result.duration_ms,
            "view_schema": inputs.view_schema,
            "view_name": ", ".join(allow.used_views) or None,
            "explanation": payload.explanation,
            "aggregation_kind": payload.aggregation_kind,
            "confidence": payload.confidence,
            "question_text": question_text,
        }

    return {
        "status": "ok",
        "sql": executed_sql,
        "standard_sql": payload.sql,
        "dialect_note": dialect_note,
        "columns": result.columns,
        "rows": result.rows,
        "truncated": result.truncated,
        "row_count": result.row_count,
        "duration_ms": result.duration_ms,
        "view_schema": inputs.view_schema,
        "view_name": ", ".join(allow.used_views),
        "explanation": payload.explanation,
        "aggregation_kind": payload.aggregation_kind,
        "confidence": payload.confidence,
        "question_text": question_text,
    }


async def _execute_inline_path(
    *, project: Project, project_code: str, contract_id: str,
    body: _ExecuteQaBody,
    platform: str = "postgres", connection_ref: dict | None = None,
    session=None,
) -> dict[str, Any]:
    """Test-only path: bypass the skill, run a bare SELECT against the
    first deployed view that has all the inline columns. Useful for unit-
    testing the allow-list and the executor pipeline without paying for an
    LLM call. Not surfaced in the UI.
    """
    from .. import sql_executor
    from .. import qa_execute as qe

    cols = [c for c in (body.inline_supporting_columns or []) if isinstance(c, str)]
    # Allow dotted dataset.column form; strip the dataset prefix.
    cols = [c.split(".", 1)[1] if "." in c else c for c in cols]
    safe_ident = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    for c in cols:
        if not safe_ident.match(c):
            raise HTTPException(400, f"Unsafe column identifier: {c!r}")

    try:
        inputs = qe.gather_inputs(
            project, contract_id,
            platform_type=platform, connection_ref=connection_ref,
            session=session,
        )
    except RuntimeError as e:
        msg = str(e)
        if "not deployed" in msg.lower():
            return {"error": "not_deployed", "deployment_status": "pending"}
        raise HTTPException(409, msg)

    needed = set(cols)
    chosen: str | None = None
    for v in inputs.deployed_views:
        view_cols = {c["name"] for c in v.get("columns") or []}
        if needed.issubset(view_cols):
            chosen = v["view_name"]
            break
    if not chosen:
        raise HTTPException(409, f"No single deployed view contains all columns {sorted(needed)}")

    # Quote/execute against the connection gather_inputs resolved (source view or
    # transfer target) rather than the source platform passed in.
    col_list = ", ".join(quote_ident(c, inputs.exec_platform) for c in cols)
    sql = f'SELECT {col_list} FROM {quote_created_relation(inputs.view_schema, chosen, inputs.exec_platform)}'

    # Reuse the AppSettings-based Neo4j connection for audit + executor.
    from ..models import AppSettings as _AppSettings
    with _neo4j_from_settings(_AppSettings()) as ns:
        result = sql_executor.execute_select(
            neo4j_session=ns,
            project_code=project_code,
            pg_connection=qe._source_dsn(inputs.exec_platform, inputs.exec_connection_ref),
            sql=sql,
            executed_by="qa-execute-inline",
            max_rows=max(1, min(int(body.limit or 100), sql_executor.DEFAULT_MAX_ROWS)),
            product_uri=inputs.product_uri,
            view_schema=inputs.view_schema,
            platform=inputs.exec_platform,
            connection_ref=inputs.exec_connection_ref or None,
        )

    if result.status != "ok":
        return {
            "status": "failed",
            "sql": sql,
            "error_class": result.error_class,
            "error_message": result.error_message,
            "duration_ms": result.duration_ms,
        }
    return {
        "status": "ok",
        "sql": sql,
        "columns": result.columns,
        "rows": result.rows,
        "truncated": result.truncated,
        "row_count": result.row_count,
        "duration_ms": result.duration_ms,
        "view_name": chosen,
        "view_schema": inputs.view_schema,
        "explanation": f"Inline test path — bare SELECT against {inputs.view_schema}.{chosen}.",
        "aggregation_kind": "raw",
        "confidence": "n/a",
        "question_text": body.question_text or "",
    }


# Sibling project-scoped wrapper. Engineer surfaces have a project_id, not
# a contract_id, so they post here instead. Just re-derives the contract_id
# and delegates to the marketplace handler so the two paths stay aligned.
project_qa_router = APIRouter(prefix="/api/projects/{project_id}", tags=["marketplace"])


@project_qa_router.post("/qa/execute")
async def project_qa_execute(
    project_id: int,
    body: _ExecuteQaBody | None = None,
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    contract_id = f"{project.project_code}-contract"
    return await marketplace_qa_execute(contract_id=contract_id, body=body, session=session)


# ── Marketplace free-form chat (Phase 3b) ─────────────────────────────────


class _MarketplaceChatBody(BaseModel):
    domain: str
    contract_id: Optional[str] = None
    conversation: list[dict] = []  # list of {role, content}
    user_message: str
    limit: int = 100
    # 'full' = whole-domain concept dump (default, preserves prior behaviour);
    # 'concept_guided' = embedding-retrieved concept subset + 1-hop neighbours.
    retrieval_mode: str = "full"
    # The user's disambiguation pick, echoed back on re-submit: a list of
    # {column_name, view_name, view_schema, value, mention_text}. When present the
    # pipeline skips value extraction/probing and grounds these verbatim. For an
    # attribute-choice pick, `value` is "" (pins the column; the pipeline re-probes
    # only that column).
    resolved_values: Optional[list[dict]] = None


@router.post("/chat")
async def marketplace_chat(
    body: _MarketplaceChatBody,
    session: Session = Depends(get_session),
):
    """Free-form NL→SQL chat scoped to a domain (and optionally a product).

    Calls the marketplace-product-chat-assistant skill, validates the
    returned SQL against an allow-list of deployed views in the scope,
    and runs it through sql_executor. Stateless: client sends history
    each turn. Returns ``{message, sql, columns, rows, ..., refused_reason?,
    error_class?, suggested_questions[]}``.
    """
    from .. import marketplace_chat as mc

    settings = _get_settings(session)
    if not body.domain or not body.domain.strip():
        raise HTTPException(400, "domain is required")
    if not body.user_message or not body.user_message.strip():
        raise HTTPException(400, "user_message is required")

    retrieval_mode = "concept_guided" if body.retrieval_mode == "concept_guided" else "full"
    # Single shared pipeline (also used by the MCP query_semantic_layer tool and
    # the conversational endpoint). Returns the normalized status dict with the
    # full retrieval_meta + trace + token_usage. See marketplace_chat.answer_question.
    result = await mc.answer_question(
        settings, body.domain, body.user_message,
        contract_id=body.contract_id, retrieval_mode=retrieval_mode,
        conversation=body.conversation or [], limit=body.limit,
        executed_by="anonymous-marketplace-chat",
        resolved_values=body.resolved_values,
    )
    if result.get("error_class") == "no_products":
        raise HTTPException(409, result.get("error_message") or
                            f"No deployed products found for domain={body.domain!r}")
    return result


# ── Conversational Semantic Q&A (server-side sessions, the mode "above") ───────
#
# A stateful orchestrator that sits above Full / Concept-Guided. Per turn it runs
# an ontology-grounded router pass (marketplace_chat.route_turn) that decides
# query / clarify / reject / chat, then — only on `query` — invokes the SAME
# NL→SQL pipeline (answer_question) the /chat route uses. Memory is the
# SemanticChatSession.session_context_json blob (deliberate rolling state), not a
# whole-thread dump. New session = cleared memory.

_CONVO_HISTORY_TURNS = 12  # transcript tail handed to the router (last N messages)


class _ConversationBody(BaseModel):
    session_id: Optional[int] = None
    domain: str
    contract_id: Optional[str] = None
    user_message: str
    # Retrieval strategy the agent uses WHEN it queries.
    retrieval_submode: str = "concept_guided"
    limit: int = 100


def _serialize_semantic_session(s) -> dict:
    try:
        ctx = json.loads(s.session_context_json or "{}")
    except (TypeError, ValueError):
        ctx = {}
    return {
        "id": s.id,
        "domain": s.domain,
        "contract_id": s.contract_id,
        "retrieval_submode": s.retrieval_submode,
        "title": s.title,
        "session_context": ctx,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def _serialize_semantic_message(m) -> dict:
    try:
        payload = json.loads(m.payload_json or "{}")
    except (TypeError, ValueError):
        payload = {}
    return {
        "id": m.id,
        "session_id": m.session_id,
        "role": m.role,
        "content": m.content,
        "payload": payload,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


@router.get("/conversation/sessions")
def list_semantic_sessions(
    domain: Optional[str] = None, contract_id: Optional[str] = None,
    session: Session = Depends(get_session),
):
    q = select(SemanticChatSession)
    if domain:
        q = q.where(SemanticChatSession.domain == domain)
    if contract_id:
        q = q.where(SemanticChatSession.contract_id == contract_id)
    rows = session.exec(q.order_by(SemanticChatSession.updated_at.desc())).all()
    return [_serialize_semantic_session(s) for s in rows]


@router.post("/conversation/sessions")
def create_semantic_session(body: dict, session: Session = Depends(get_session)):
    domain = (body or {}).get("domain")
    if not domain:
        raise HTTPException(400, "domain is required")
    row = SemanticChatSession(
        domain=domain,
        contract_id=(body or {}).get("contract_id") or None,
        retrieval_submode=(body or {}).get("retrieval_submode") or "concept_guided",
        title=(body or {}).get("title") or "",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _serialize_semantic_session(row)


@router.get("/conversation/sessions/{session_id}/messages")
def list_semantic_messages(session_id: int, session: Session = Depends(get_session)):
    sess = session.get(SemanticChatSession, session_id)
    if not sess:
        raise HTTPException(404, "Session not found")
    rows = session.exec(
        select(SemanticChatMessage)
        .where(SemanticChatMessage.session_id == session_id)
        .order_by(SemanticChatMessage.created_at.asc(), SemanticChatMessage.id.asc())
    ).all()
    return {
        "session": _serialize_semantic_session(sess),
        "messages": [_serialize_semantic_message(m) for m in rows],
    }


@router.delete("/conversation/sessions/{session_id}")
def delete_semantic_session(session_id: int, session: Session = Depends(get_session)):
    sess = session.get(SemanticChatSession, session_id)
    if not sess:
        raise HTTPException(404, "Session not found")
    msgs = session.exec(
        select(SemanticChatMessage).where(SemanticChatMessage.session_id == session_id)
    ).all()
    for m in msgs:
        session.delete(m)
    session.delete(sess)
    session.commit()
    return {"ok": True}


def _sum_token_usage(*usages: dict) -> dict:
    """Combine per-pass token_usage dicts into one per-turn total (router pass +
    query pass). Uses the same accumulator the SDK passes feed."""
    from .. import llm_usage
    acc = llm_usage.UsageAccumulator()
    for u in usages:
        if u:
            acc.add(u)
    return acc.total


def _best_label_match(user_message: str, options: list[dict], *, threshold: float = 78.0):
    """Match a user's reply against stored disambiguation options — exact label
    (case-insensitive), exact value, else fuzzy. Returns the option or None."""
    from .. import value_resolution as vr
    msg = (user_message or "").strip().lower()
    if not msg or not options:
        return None
    for opt in options:
        if str(opt.get("label") or "").strip().lower() == msg:
            return opt
        if str(opt.get("value") or "").strip().lower() == msg:
            return opt
    best, best_score = None, 0.0
    for opt in options:
        score = vr._fuzz_score(msg, str(opt.get("label") or ""))
        if score > best_score:
            best, best_score = opt, score
    return best if best_score >= threshold else None


_AFFIRMATIVE_RE = re.compile(
    r"^\s*(yes|yep|yeah|yup|sure|correct|right|exactly|ok|okay|that'?s? (her|him|the one|it)|"
    r"that one|the one|she'?s? the one|he'?s? the one|her|him|confirm(ed)?)\b",
    re.IGNORECASE,
)
_ORDINALS = {
    "first": 0, "1st": 0, "one": 0, "second": 1, "2nd": 1, "two": 1,
    "third": 2, "3rd": 2, "three": 2, "fourth": 3, "4th": 3, "fifth": 4, "5th": 4,
}


def _is_affirmative(msg: str) -> bool:
    return bool(_AFFIRMATIVE_RE.match(msg or "")) and len((msg or "").split()) <= 6


def _ordinal_index(msg: str, n: int):
    """Map "the first one" / "#2" / "last" to a 0-based index into n candidates."""
    m = (msg or "").lower()
    if re.search(r"\blast\b", m):
        return n - 1
    num = re.search(r"#?\s*(\d+)\b", m)
    if num:
        return int(num.group(1)) - 1
    for word, idx in _ORDINALS.items():
        if re.search(rf"\b{word}\b", m):
            return idx
    return None


def _match_pending_pick(user_message: str, pending: dict):
    """Resolve a follow-up turn against a stored ``pending_disambiguation``.
    Handles an explicit label/value, an ordinal ("the first one", "#2"), and an
    affirmative ("yes that's her") when there's a single candidate. Returns
    ``{resolved_values, resolved_question}`` on a match, else None (the router
    then handles the turn fresh)."""
    if not isinstance(pending, dict):
        return None
    already = pending.get("already_grounded") or []
    question = pending.get("resolved_question") or ""
    mention = pending.get("mention_text") or ""

    def _record_rv(chosen):
        return {"resolved_values": list(already) + [{
            "mention_text": mention, "value": chosen.get("value"),
            "column_name": chosen.get("column_name"), "view_name": chosen.get("view_name"),
            "view_schema": chosen.get("view_schema")}],
            "resolved_question": question}

    if pending.get("kind") == "attribute":
        chosen = _best_label_match(user_message, pending.get("attribute_options") or [])
        if chosen is None:
            return None
        return {"resolved_values": list(already) + [{
            "mention_text": mention, "value": "", "column_name": chosen.get("column_name"),
            "view_name": chosen.get("view_name"), "view_schema": chosen.get("view_schema")}],
            "resolved_question": question}

    cands = pending.get("candidates") or []
    # Ordinal pick ("the first one", "#2").
    idx = _ordinal_index(user_message, len(cands))
    if idx is not None and 0 <= idx < len(cands):
        return _record_rv(cands[idx])
    # Affirmative confirm of a single candidate ("yes that's her").
    if len(cands) == 1 and _is_affirmative(user_message):
        return _record_rv(cands[0])
    # Explicit label / value match.
    chosen = _best_label_match(user_message, cands)
    if chosen is not None:
        return _record_rv(chosen)
    return None


def _disambiguation_to_clarify(result: dict, routing_step: dict, submode: str,
                               session_context: dict, asked_question: str) -> dict:
    """Map a ``needs_disambiguation`` query result into a `clarify` turn: store
    the pending state in session_context (so the next pick reconciles) and return
    the response dict with candidate/attribute labels as clarification chips."""
    dis = result.get("disambiguation") or {}
    kind = dis.get("kind")
    if kind == "attribute":
        options = [o.get("label") for o in (dis.get("attribute_options") or [])]
    else:
        options = [c.get("label") for c in (dis.get("candidates") or [])]
    session_context["pending_disambiguation"] = {
        "mention_text": dis.get("mention_text"),
        "kind": kind,
        "resolved_question": asked_question,
        "candidates": dis.get("candidates") or [],
        "attribute_options": dis.get("attribute_options") or [],
        "already_grounded": dis.get("already_grounded") or [],
    }
    trace = result.get("trace") or {"retrieval_mode": submode, "steps": []}
    trace = {**trace, "steps": [routing_step, *(trace.get("steps") or [])]}
    return {
        "action": "clarify",
        "reply": dis.get("prompt") or "Which one did you mean?",
        "clarification_options": [o for o in options if o][:4],
        "payload": None,
        "trace": trace,
        "token_usage": result.get("token_usage") or {},
    }


@router.post("/conversation")
async def marketplace_conversation(
    body: _ConversationBody,
    session: Session = Depends(get_session),
):
    """One conversational turn. Runs the ontology-grounded router; on a `query`
    decision invokes the shared NL→SQL pipeline. Persists both messages + the
    rolling session_context. Returns ``{session_id, action, reply, payload?,
    trace, token_usage, session_context, ...}``."""
    from datetime import datetime
    from .. import marketplace_chat as mc
    from .. import llm_usage

    settings = _get_settings(session)
    if not body.domain or not body.domain.strip():
        raise HTTPException(400, "domain is required")
    if not body.user_message or not body.user_message.strip():
        raise HTTPException(400, "user_message is required")

    submode = "concept_guided" if body.retrieval_submode == "concept_guided" else "full"

    # Resolve / create the session (new session = cleared memory).
    sess = session.get(SemanticChatSession, body.session_id) if body.session_id else None
    if sess is None:
        sess = SemanticChatSession(
            domain=body.domain, contract_id=body.contract_id or None,
            retrieval_submode=submode,
            title=body.user_message.strip()[:60],
        )
        session.add(sess)
        session.commit()
        session.refresh(sess)
    else:
        sess.retrieval_submode = submode

    try:
        session_context = json.loads(sess.session_context_json or "{}")
        if not isinstance(session_context, dict):
            session_context = {}
    except (TypeError, ValueError):
        session_context = {}

    # Recent transcript tail (role/content) for reference resolution.
    history_rows = session.exec(
        select(SemanticChatMessage)
        .where(SemanticChatMessage.session_id == sess.id)
        .order_by(SemanticChatMessage.created_at.asc(), SemanticChatMessage.id.asc())
    ).all()
    conversation = [{"role": m.role, "content": m.content} for m in history_rows][-_CONVO_HISTORY_TURNS:]

    # Persist the user turn up front.
    session.add(SemanticChatMessage(session_id=sess.id, role="user", content=body.user_message.strip()))
    session.commit()

    response: dict

    # ── Follow-up pick reconciliation. If the prior turn asked a disambiguation
    # question (value/attribute), try to resolve THIS reply against it before
    # routing — intent is already established, so skip the router. ───────────────
    pending = session_context.get("pending_disambiguation") if isinstance(session_context, dict) else None
    reconciled = _match_pending_pick(body.user_message, pending) if pending else None
    if reconciled is not None:
        session_context.pop("pending_disambiguation", None)
        asked = reconciled["resolved_question"] or body.user_message
        routing_step = {
            "key": "routing", "label": "Conversation routing",
            "detail": {"action": "query", "matched_concepts": [],
                       "notes": f"Resolved your selection for “{(pending or {}).get('mention_text', '')}”.",
                       "resolved_question": asked, "ontology_concepts": 0},
        }
        result = await mc.answer_question(
            settings, body.domain, asked,
            contract_id=body.contract_id, retrieval_mode=submode,
            conversation=conversation, limit=body.limit,
            executed_by="anonymous-semantic-conversation",
            resolved_values=reconciled["resolved_values"],
        )
        if result.get("status") == "needs_disambiguation":
            response = _disambiguation_to_clarify(result, routing_step, submode, session_context, asked)
        else:
            trace = result.get("trace") or {"retrieval_mode": submode, "steps": []}
            trace = {**trace, "steps": [routing_step, *(trace.get("steps") or [])]}
            result["trace"] = trace
            response = {
                "action": "query",
                "reply": result.get("synthesized_answer") or result.get("message") or "",
                "payload": result, "trace": trace,
                "token_usage": result.get("token_usage") or {},
            }
    else:
        # ── Router pass (ontology-grounded). Its own usage scope so we can report
        # the per-turn total = router + (optional) query. ───────────────────────
        if pending:
            session_context.pop("pending_disambiguation", None)  # reply didn't match — drop stale state
        router_acc = mc.begin_qa_usage()
        decision, ontology = await mc.route_turn(
            settings, body.domain, body.user_message, conversation, session_context,
        )
        router_tokens = dict(router_acc.total)

        routing_step = {
            "key": "routing", "label": "Conversation routing",
            "detail": {
                "action": decision.action,
                "matched_concepts": (decision.grounding or {}).get("matched_concepts", []),
                "notes": (decision.grounding or {}).get("notes", ""),
                "resolved_question": decision.resolved_question,
                "ontology_concepts": len(ontology or []),
            },
        }

        # Merge router's memory update into the rolling session_context.
        if decision.session_context_update:
            session_context.update(decision.session_context_update)

        # Routing overhead is recorded under 'conversational' in every branch.
        llm_usage.record_usage(source="semantic_qa", usage=router_tokens,
                               domain=body.domain, contract_id=body.contract_id,
                               retrieval_mode="conversational")

        if decision.action == "query":
            asked = decision.resolved_question or body.user_message
            result = await mc.answer_question(
                settings, body.domain, asked,
                contract_id=body.contract_id, retrieval_mode=submode,
                conversation=conversation, limit=body.limit,
                executed_by="anonymous-semantic-conversation",
            )
            if result.get("error_class") == "no_products":
                # Surface as a turn (not a hard 409) so the conversation continues.
                result = {
                    "status": "failed", "error_class": "no_products",
                    "message": result.get("error_message") or "No deployed products for this domain yet.",
                    "trace": {"retrieval_mode": submode, "steps": []},
                }
            if result.get("status") == "needs_disambiguation":
                # A named record couldn't be uniquely resolved — turn into a
                # clarify turn (chips), reconciled on the next pick.
                response = _disambiguation_to_clarify(result, routing_step, submode, session_context, asked)
                response["token_usage"] = _sum_token_usage(router_tokens, result.get("token_usage") or {})
            else:
                # Prepend the routing step so every turn explains itself.
                trace = result.get("trace") or {"retrieval_mode": submode, "steps": []}
                trace = {**trace, "steps": [routing_step, *(trace.get("steps") or [])]}
                result["trace"] = trace
                turn_tokens = _sum_token_usage(router_tokens, result.get("token_usage") or {})
                result["token_usage"] = turn_tokens
                response = {
                    "action": "query",
                    "reply": result.get("synthesized_answer") or result.get("message") or "",
                    "payload": result, "trace": trace, "token_usage": turn_tokens,
                }
        else:
            # clarify / reject / chat — no query. Light trace (routing only).
            response = {
                "action": decision.action,
                "reply": decision.reply,
                "clarification_options": decision.clarification_options,
                "payload": None,
                "trace": {"retrieval_mode": "conversational", "steps": [routing_step]},
                "token_usage": router_tokens,
            }

    # Persist the assistant turn + the updated rolling memory.
    sess.session_context_json = json.dumps(session_context, default=str)
    sess.updated_at = datetime.utcnow()
    session.add(sess)
    session.add(SemanticChatMessage(
        session_id=sess.id, role="assistant",
        content=response.get("reply") or "",
        payload_json=json.dumps(response, default=str),
    ))
    session.commit()

    response["session_id"] = sess.id
    response["session_context"] = session_context
    response["retrieval_submode"] = submode
    return response


# ── Gap log (Semantic Q&A refusals) ───────────────────────────────────────


class _GapCreateBody(BaseModel):
    domain: str
    question: str
    refused_reason: Optional[str] = None
    analysis: Optional[str] = None
    concepts_used: Optional[list] = None
    retrieval_meta: Optional[dict] = None
    product_uri: Optional[str] = None
    contract_id: Optional[str] = None
    audience: Optional[str] = None
    created_by: Optional[str] = None


class _GapResolveBody(BaseModel):
    status: str  # open|triaged|resolved|dismissed
    audience: Optional[str] = None
    notes: Optional[str] = None
    resolved_by: Optional[str] = None


def _gap_to_dict(g: MarketplaceGap) -> dict:
    return {
        "id": g.id, "domain": g.domain, "contract_id": g.contract_id,
        "product_uri": g.product_uri, "question": g.question,
        "refused_reason": g.refused_reason, "analysis": g.analysis,
        "concepts_used": json.loads(g.concepts_used_json) if g.concepts_used_json else [],
        "status": g.status, "audience": g.audience,
        "suggested_action": g.suggested_action,
        "created_by": g.created_by,
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "resolved_by": g.resolved_by,
        "resolved_at": g.resolved_at.isoformat() if g.resolved_at else None,
        "notes": g.notes,
    }


@router.post("/gaps")
def create_gap(body: _GapCreateBody, session: Session = Depends(get_session)):
    """Log a Semantic Q&A gap (a question the semantic layer couldn't answer)."""
    if not body.domain.strip() or not body.question.strip():
        raise HTTPException(400, "domain and question are required")
    gap = MarketplaceGap(
        domain=body.domain.strip(),
        contract_id=body.contract_id,
        product_uri=body.product_uri,
        question=body.question.strip(),
        refused_reason=body.refused_reason,
        analysis=body.analysis,
        concepts_used_json=json.dumps(body.concepts_used) if body.concepts_used else None,
        retrieval_meta_json=json.dumps(body.retrieval_meta) if body.retrieval_meta else None,
        audience=(body.audience or "triage"),
        created_by=(body.created_by or "anonymous-marketplace-chat"),
    )
    session.add(gap)
    session.commit()
    session.refresh(gap)
    return {"id": gap.id, "status": gap.status}


@router.get("/gaps")
def list_gaps(
    status: Optional[str] = None, audience: Optional[str] = None,
    domain: Optional[str] = None, session: Session = Depends(get_session),
):
    """List gaps (newest first). Default excludes dismissed. Includes open_count
    for the nav badge."""
    q = select(MarketplaceGap)
    if status:
        q = q.where(MarketplaceGap.status == status)
    else:
        # Default view = the active backlog (open + triaged); hide resolved/dismissed.
        q = q.where(MarketplaceGap.status.notin_(["resolved", "dismissed"]))
    if audience:
        q = q.where(MarketplaceGap.audience == audience)
    if domain:
        q = q.where(MarketplaceGap.domain == domain)
    rows = session.exec(q.order_by(MarketplaceGap.id.desc())).all()
    open_count = len(session.exec(
        select(MarketplaceGap.id).where(MarketplaceGap.status == "open")
    ).all())
    return {"gaps": [_gap_to_dict(g) for g in rows], "open_count": open_count}


@router.post("/gaps/{gap_id}/resolve")
def resolve_gap(gap_id: int, body: _GapResolveBody, session: Session = Depends(get_session)):
    """Triage / resolve / dismiss a gap (and optionally retag audience + note)."""
    if body.status not in ("open", "triaged", "resolved", "dismissed"):
        raise HTTPException(400, "invalid status")
    gap = session.get(MarketplaceGap, gap_id)
    if not gap:
        raise HTTPException(404, "gap not found")
    gap.status = body.status
    if body.audience:
        gap.audience = body.audience
    if body.notes is not None:
        gap.notes = body.notes
    if body.status in ("resolved", "dismissed"):
        from datetime import datetime as _dt
        gap.resolved_by = body.resolved_by or "Data Product Owner"
        gap.resolved_at = _dt.utcnow()
    session.add(gap)
    session.commit()
    return {"id": gap_id, "status": gap.status, "audience": gap.audience}


# ── Marketplace reflection ────────────────────────────────────────────────


@router.get("/products/{contract_id}/reflection/latest")
def marketplace_reflection_latest(
    contract_id: str,
    session: Session = Depends(get_session),
):
    """Return the most recent `:DeploymentReflection` for the product,
    keyed by contract_id. `{reflection: null}` (200) when none exists.
    Read-only view; consumer-side, no auth gating beyond what the rest of
    the marketplace already does (none in v1)."""
    from .. import deployment_reflection as reflection_engine
    if not contract_id.endswith("-contract"):
        raise HTTPException(404, "Contract not found")
    project_code = contract_id[: -len("-contract")]
    project = session.exec(select(Project).where(Project.project_code == project_code)).first()
    if not project:
        raise HTTPException(404, "Project not found for contract")
    reflection = reflection_engine.read_latest(project, contract_id)
    return {"reflection": reflection}


# Full revision timeline: every :ContractVersion sidecar plus any cosmetic
# patches attached via :HAS_PATCH. Returns chronologically newest-first.
_REVISIONS_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})-[:HAS_VERSION]->(cv:ContractVersion)
OPTIONAL MATCH (cv)-[:HAS_PATCH]->(patch:ProvActivity)
WITH cv, collect(patch) AS patches
RETURN cv.version       AS version,
       cv.lifecycleState AS lifecycle_state,
       cv.changeKind    AS change_kind,
       cv.revisionNotes AS revision_notes,
       cv.publishedAt   AS published_at,
       cv.occurredAt    AS occurred_at,
       cv.actor         AS actor,
       [p IN patches WHERE p IS NOT NULL | {
           occurred_at: p.occurredAt,
           change_kind: p.changeKind,
           revision_notes: p.revisionNotes,
           actor: p.actor
       }] AS patches
ORDER BY cv.version DESC
"""


@router.get("/{contract_id}/revisions")
def get_product_revisions(
    contract_id: str,
    session: Session = Depends(get_session),
):
    """Return the contract's full revision history.

    One row per :ContractVersion sidecar, with the version's cosmetic
    patches (:ProvActivity 'ContractPatch' attached via :HAS_PATCH)
    inlined as a ``patches`` array. Newest-first.
    """
    project = _resolve_project_for_contract(contract_id, session)
    if not project:
        raise HTTPException(404, f"No project found for contract_id={contract_id}")
    try:
        with neo4j_session(
            project.neo4j_host, project.neo4j_port,
            project.neo4j_user, project.neo4j_password, project.neo4j_database,
        ) as ns:
            rows = []
            for r in ns.run(_REVISIONS_QUERY, contract_id=contract_id):
                row = dict(r)
                for k in ("published_at", "occurred_at"):
                    if row.get(k) is not None:
                        row[k] = str(row[k])
                for p in row.get("patches") or []:
                    if p.get("occurred_at") is not None:
                        p["occurred_at"] = str(p["occurred_at"])
                rows.append(row)
            return {"contract_id": contract_id, "versions": rows}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Revisions query failed: {e}")


@router.get("/lineage")
def get_lineage(
    uri: str = QParam(..., description="Data product URI"),
    session: Session = Depends(get_session),
):
    """Get consumer-friendly lineage for a data product."""
    settings = _get_settings(session)
    try:
        with _neo4j_from_settings(settings) as ns:
            sources = [dict(r) for r in ns.run(LINEAGE_QUERY, uri=uri)]
            stats_row = ns.run(LINEAGE_STATS, uri=uri).single()
            stats = dict(stats_row) if stats_row else {}
            # Append lookup reference tables, tagged so the UI shows a "lookup"
            # marker instead of an X/Y mapped ratio. De-dup against primary
            # source tables already listed (a table can be both).
            primary_keys = {(s.get("source_schema"), s.get("source_table")) for s in sources}
            for lk in ns.run(LINEAGE_LOOKUP_QUERY, uri=uri):
                lk = dict(lk)
                if (lk.get("source_schema"), lk.get("source_table")) in primary_keys:
                    continue
                lk["is_lookup"] = True
                sources.append(lk)
            return {
                "uri": uri,
                "sources": sources,
                "stats": stats,
            }
    except Exception as e:
        raise HTTPException(500, f"Lineage query failed: {e}")


# ── Product-level lineage (marketplace-wide DAG) ───────────────────────────
#
# A COARSE, product-to-product dependency graph for the marketplace's Lineage
# view: nodes are whole data products, edges are :CONSUMES relationships. No
# tables/columns/mappings (that's the column-grain MappingGraphView). The node
# set + version pin mirror ALL_PRODUCTS exactly so the graph and the Products
# listing never disagree about which products exist.

PRODUCT_LINEAGE_NODES = """\
MATCH (dp:DProdDataProduct)
OPTIONAL MATCH (dp)<-[:MATERIALISES_AS]-(dc:DataContract)
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv_pub:ContractVersion)
WHERE cv_pub.lifecycleState IN ['published','superseded']
WITH dp, dc, cv_pub ORDER BY cv_pub.version DESC
WITH dp, dc, head(collect(cv_pub)) AS cv_deployed
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv_cur:ContractVersion {version: dc.currentVersion})
WITH dp, dc, coalesce(cv_deployed, cv_cur) AS cv
RETURN
    dp.uri AS uri,
    dp.name AS name,
    coalesce(dc.productKind, '') AS product_kind,
    coalesce(cv.snapshotDomain, dc.domain, '') AS domain,
    coalesce(dc.tags, []) AS tags,
    coalesce(cv.lifecycleState, 'draft') AS lifecycle_state
"""

# Active :CONSUMES edges only (toVersion IS NULL) — the "currently active"
# convention used across the codebase (pg_resolver, qa.READ_CONSUMES). The
# emitted edge runs in DATA-FLOW direction (upstream source -> downstream
# consumer), i.e. the reverse of the :CONSUMES arrow.
PRODUCT_LINEAGE_EDGES = """\
MATCH (consumer_dc:DataContract)-[r:CONSUMES]->(src_dp:DProdDataProduct)
WHERE r.toVersion IS NULL
MATCH (consumer_dc)-[:MATERIALISES_AS]->(consumer_dp:DProdDataProduct)
RETURN DISTINCT src_dp.uri AS source_uri, consumer_dp.uri AS target_uri
"""


@router.get("/product-lineage")
def get_product_lineage(session: Session = Depends(get_session)):
    """Marketplace-wide product dependency graph.

    Returns ``{nodes, edges}`` where each node is a data product (same set +
    version pin as the product listing) and each edge is a currently-active
    ``:CONSUMES`` relationship rendered as a data-flow arrow (upstream source
    → downstream consumer). Coarse by design — products only.
    """
    settings = _get_settings(session)
    try:
        with _neo4j_from_settings(settings) as ns:
            nodes = [dict(r) for r in ns.run(PRODUCT_LINEAGE_NODES)]
            for n in nodes:
                n["tags"] = list(n.get("tags") or [])
            node_uris = {n["uri"] for n in nodes}
            edges: list[dict] = []
            seen: set[tuple] = set()
            for e in ns.run(PRODUCT_LINEAGE_EDGES):
                src = e["source_uri"]
                tgt = e["target_uri"]
                # Defensive: drop edges whose endpoints aren't both in the node
                # set (e.g. a consumer whose dprod node was never materialised).
                if src not in node_uris or tgt not in node_uris or src == tgt:
                    continue
                key = (src, tgt)
                if key in seen:
                    continue
                seen.add(key)
                edges.append({"source": src, "target": tgt, "kind": "consumes"})
            return {"nodes": nodes, "edges": edges}
    except Exception as e:
        raise HTTPException(500, f"Product lineage query failed: {e}")


# ── Layered value-flow ("Sankey") view ─────────────────────────────────────
#
# A LEFT→RIGHT layered flow across five fixed columns:
#
#   Source Schemas → Source-Aligned → Aggregate/Derived → Consumer-Aligned → Use Cases
#
# ADDITIVE — the existing product-lineage + mapping-graph views are untouched.
# The payload is a context-agnostic FlowBuilder dict (see flow_payload.py); the
# Estate/Feasibility context reuses the exact same shape + frontend component.
#
# The MIDDLE THREE columns + the `consumes` links come from the same
# product-to-product `:CONSUMES` DAG the /product-lineage endpoint reads, but
# with a REQUIRED row-level lifecycle filter added (FLOW_PRODUCT_NODES below):
# PRODUCT_LINEAGE_NODES leaks never-published drafts because its lifecycle guard
# lives inside an OPTIONAL MATCH and falls back to `dc.currentVersion` (which
# can be a draft) — the consumer-facing flow must not show those.
#
# The LEFTMOST column ("Source Schemas") is a marketplace-wide generalization of
# the per-URI LINEAGE_QUERY's catalog arm: per source-aligned product, walk its
# mappings back to the raw `:Catalog` feeding them and group by the
# PROJECT-SCOPED catalog node (URI `catalog:{project_code}:{schema}`), NOT the
# bare schema name — so a Postgres `public` shows once PER PROJECT
# ("sales · public") instead of collapsing into one global node. Honest gaps:
#   - A marketplace "source system" == a catalog/schema feeding a source-aligned
#     product. It's a SYNTHESIZED grouping (meta.synthesized=true), NOT a real
#     business system. Real named systems are the Estate view's job (fast-follow).
#   - A source-aligned product with no `isCurrent` `:ColumnMapping` contributes
#     no source node (mapping lifecycle is independent of the contract; same
#     caveat as LINEAGE_QUERY).
#
# The RIGHTMOST column ("Use Cases") is a PURE PLACEHOLDER in v1: the live
# `_generate_dprod` path never writes `keyUseCases`, so a read would always be
# empty. No use-case nodes/links are emitted; the frontend renders one ghost
# "use cases coming soon" node for the empty column.

# PRODUCT_LINEAGE_NODES + the REQUIRED row-level lifecycle guard the flow needs
# (see comment above). Kept as a SEPARATE constant rather than mutating
# PRODUCT_LINEAGE_NODES so the legacy /product-lineage endpoint is byte-unchanged.
FLOW_PRODUCT_NODES = """\
MATCH (dp:DProdDataProduct)
OPTIONAL MATCH (dp)<-[:MATERIALISES_AS]-(dc:DataContract)
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv_pub:ContractVersion)
WHERE cv_pub.lifecycleState IN ['published','superseded']
WITH dp, dc, cv_pub ORDER BY cv_pub.version DESC
WITH dp, dc, head(collect(cv_pub)) AS cv_deployed
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv_cur:ContractVersion {version: dc.currentVersion})
WITH dp, dc, coalesce(cv_deployed, cv_cur) AS cv
WHERE cv.lifecycleState IN ['published','superseded']
RETURN
    dp.uri AS uri,
    dp.name AS name,
    coalesce(dc.productKind, '') AS product_kind,
    coalesce(cv.snapshotDomain, dc.domain, '') AS domain,
    coalesce(dc.tags, []) AS tags,
    coalesce(cv.lifecycleState, 'draft') AS lifecycle_state
"""

# Marketplace-wide generalization of LINEAGE_QUERY's catalog arm: for every
# PUBLISHED/SUPERSEDED source-aligned product, walk its current mappings back to
# the raw `:Catalog` feeding them and group by the project-scoped catalog node.
# The walk is a REQUIRED MATCH (not OPTIONAL) — a source product with no
# `isCurrent` mapping simply produces no rows (the honest gap above). Only the
# `:Column` (catalog) source arm is followed; dprod-sourced mappings (which
# resolve to `:DProdColumn`) never match `:Column`, so a consumer/aggregate
# never appears here.
FLOW_SOURCE_SYSTEMS = """\
MATCH (dp:DProdDataProduct)<-[:MATERIALISES_AS]-(dc:DataContract)
WHERE coalesce(dc.productKind, '') = 'source'
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv_pub:ContractVersion)
WHERE cv_pub.lifecycleState IN ['published','superseded']
WITH dp, dc, cv_pub ORDER BY cv_pub.version DESC
WITH dp, dc, head(collect(cv_pub)) AS cv_deployed
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv_cur:ContractVersion {version: dc.currentVersion})
WITH dp, dc, coalesce(cv_deployed, cv_cur) AS cv
WHERE cv.lifecycleState IN ['published','superseded']
MATCH (dp)-[:DPROD_OUTPUT_PORT]->()-[:DPROD_OUTPUT_DATASET]->()
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
      <-[:MAPS_TO_PRODUCT_COLUMN]-(cm:ColumnMapping {isCurrent: true})
MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(sc:Column)
      <-[:HAS_COLUMN]-(ds:Dataset)<-[:DCAT_DATASET]-(cat:Catalog)
RETURN
    dp.uri AS product_uri,
    cat.uri AS catalog_uri,
    cat.name AS schema,
    coalesce(cv.snapshotDomain, dc.domain, '') AS domain,
    count(DISTINCT sc) AS source_columns
"""


def _project_code_from_catalog_uri(uri: str) -> str:
    """`catalog:{project_code}:{schema}` → project_code (middle segment).

    Falls back to '' when the URI isn't the expected 3-part project-scoped
    shape (a legacy unscoped `catalog:{schema}`), matching the tolerant parsing
    style of _project_code_from_dataset_uri.
    """
    parts = (uri or "").split(":")
    return parts[1] if len(parts) >= 3 else ""


@router.get("/flow")
def get_marketplace_flow(
    domain: Optional[str] = QParam(
        None, description="Optional domain scope — restrict the flow to products in this domain."
    ),
    session: Session = Depends(get_session),
):
    """Layered value-flow ("Sankey") payload for the marketplace.

    Returns the context-agnostic ``FlowBuilder`` dict: ALWAYS 5 columns
    (source_system, source_aligned, aggregate, consumer, use_case) + ``links``
    (``consumes`` + ``source_binding``). See the section comment above for the
    column semantics, the draft-leak fix, the project-scoped source grouping,
    and the use-case placeholder.

    Count semantics: a source-schema node's ``count`` = # distinct
    source-aligned products it feeds; a product node's ``count`` = # direct
    downstream ``:CONSUMES`` consumers.
    """
    settings = _get_settings(session)
    # In the marketplace the leftmost column is a synthesized per-catalog
    # grouping, not real named systems → relabel it "Source Schemas".
    builder = FlowBuilder(column_labels={"source_system": "Source Schemas"})
    dom = (domain or "").strip() or None
    try:
        with _neo4j_from_settings(settings) as ns:
            product_rows = [dict(r) for r in ns.run(FLOW_PRODUCT_NODES)]
            edge_rows = [dict(r) for r in ns.run(PRODUCT_LINEAGE_EDGES)]
            source_rows = [dict(r) for r in ns.run(FLOW_SOURCE_SYSTEMS)]
    except Exception as e:
        raise HTTPException(500, f"Marketplace flow query failed: {e}")

    # Optional Python domain scope over products; the builder then drops any
    # link whose endpoint was filtered out.
    if dom is not None:
        product_rows = [r for r in product_rows if (r.get("domain") or "") == dom]
    product_uris = {r["uri"] for r in product_rows}

    # `consumes` edges — active, deduped, dangling/self dropped (defensive; the
    # builder also drops dangling links, this keeps the downstream count honest).
    consumes: list[tuple[str, str]] = []
    seen_edges: set[tuple[str, str]] = set()
    for e in edge_rows:
        src, tgt = e.get("source_uri"), e.get("target_uri")
        if src not in product_uris or tgt not in product_uris or src == tgt:
            continue
        if (src, tgt) in seen_edges:
            continue
        seen_edges.add((src, tgt))
        consumes.append((src, tgt))
    downstream_count: dict[str, int] = {}
    for src, _tgt in consumes:
        downstream_count[src] = downstream_count.get(src, 0) + 1

    # Source-schema rows scoped to surviving products; group per catalog node.
    products_per_catalog: dict[str, set[str]] = {}
    catalog_meta: dict[str, dict[str, Any]] = {}
    binding_weight: dict[tuple[str, str], int] = {}
    for r in source_rows:
        prod = r.get("product_uri")
        cat = r.get("catalog_uri")
        if prod not in product_uris or not cat:
            continue
        products_per_catalog.setdefault(cat, set()).add(prod)
        if cat not in catalog_meta:
            catalog_meta[cat] = {
                "schema": r.get("schema") or "",
                "domain": r.get("domain") or "",
                "project_code": _project_code_from_catalog_uri(cat),
            }
        binding_weight[(cat, prod)] = int(r.get("source_columns") or 0)

    # ── Nodes: products (middle three) ──────────────────────────────────────
    for r in product_rows:
        col = bucket_product_kind(r.get("product_kind"))
        lifecycle = r.get("lifecycle_state") or "draft"
        builder.add_node(
            col,
            r["uri"],
            r.get("name") or r["uri"],
            kind=r.get("product_kind") or None,
            status=lifecycle,
            count=downstream_count.get(r["uri"], 0),
            meta={
                "domain": r.get("domain") or "",
                "tags": list(r.get("tags") or []),
                "lifecycle_state": lifecycle,
            },
        )
    for src, tgt in consumes:
        builder.add_link(src, tgt, "consumes")

    # ── Nodes: source schemas (leftmost) ────────────────────────────────────
    for cat, prods in products_per_catalog.items():
        m = catalog_meta[cat]
        builder.add_node(
            "source_system",
            cat,
            m["schema"] or cat,
            kind="source_system",
            count=len(prods),
            meta={
                "synthesized": True,
                "project_code": m["project_code"],
                "domain": m["domain"],
                "schema": m["schema"],
            },
        )
    for (cat, prod), weight in binding_weight.items():
        builder.add_link(cat, prod, "source_binding", weight=weight)

    # Use-case column: pure placeholder — no nodes/links (see section comment).
    return builder.build()


# ── Mapping graph ─────────────────────────────────────────────────────────
#
# Column-level graph payload for the marketplace product detail's Lineage
# tab. Same shape as the engineer-side /reviews/mappings/graph endpoint but
# scoped by product URI (not project_id) since the marketplace doesn't
# carry project context. Read-only — there's no editing flow over here.

MARKETPLACE_MAPPING_GRAPH_QUERY = """\
MATCH (dp:DProdDataProduct {uri: $uri})
MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(prod_ods:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
      <-[:MAPS_TO_PRODUCT_COLUMN]-(cm:ColumnMapping)
WHERE cm.isCurrent = true
WITH dp, pc, cm, prod_ods
// Source side resolves to either :Column (catalog) or :DProdColumn
// (consumer-aligned) — same dual-OPTIONAL pattern as the engineer's
// reviews.py:_MAPPING_SOURCE_DUAL_OPTIONAL. WITH separator above is
// load-bearing: trailing WHERE after OPTIONAL MATCH binds to the last
// pattern instead of filtering rows, leaking cross-project mappings.
OPTIONAL MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(col:Column)
OPTIONAL MATCH (ds:Dataset)-[:HAS_COLUMN]->(col)
OPTIONAL MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(sc_dp:DProdColumn)
OPTIONAL MATCH (ods:DProdOutputDataset)-[:HAS_PRODUCT_COLUMN]->(sc_dp)
OPTIONAL MATCH (srcDp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
               -[:DPROD_OUTPUT_DATASET]->(ods)
RETURN
    cm.uri                       AS mapping_uri,
    cm.status                    AS status,
    cm.similarityScore           AS similarity_score,
    cm.transformKind             AS transform_kind,
    cm.transformAuthor           AS transform_author,
    cm.transformExpression       AS transform_expression,
    cm.transformParams           AS transform_params_json,
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
    // Output dataset that this product column belongs to. The lineage
    // canvas groups by this so SA products with multiple datasets render
    // as N right-side boxes instead of a single collapsed product.
    prod_ods.uri                                AS product_dataset_uri,
    coalesce(prod_ods.physicalName, prod_ods.name, '') AS product_dataset_name,
    dp.uri                       AS product_uri,
    dp.name                      AS product_name
ORDER BY product_dataset_name, pc.ordinal, pc.name
"""

# Lookup sources for the same product. A `lookup`-kind mapping reads a value
# from a separate reference table (often a DIFFERENT CONSUMES'd source product
# than the mapping's primary source column); those columns hang off the mapping
# via :LOOKUP_VIA (see backend/lookup_via.py). This query emits one row per
# lookup edge so the handler can add the lookup table/column as a distinct
# left-side source + a dashed "lookup_via" edge to the product column. Kept
# separate from MARKETPLACE_MAPPING_GRAPH_QUERY so the primary-source row stream
# stays one-row-per-(mapping, source-col).
MARKETPLACE_LOOKUP_GRAPH_QUERY = """\
MATCH (dp:DProdDataProduct {uri: $uri})
MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
      <-[:MAPS_TO_PRODUCT_COLUMN]-(cm:ColumnMapping)
WHERE cm.isCurrent = true
WITH pc, cm
MATCH (cm)-[lv:LOOKUP_VIA]->(lk)
OPTIONAL MATCH (lk_ds:Dataset)-[:HAS_COLUMN]->(lk)
OPTIONAL MATCH (lk_ods:DProdOutputDataset)-[:HAS_PRODUCT_COLUMN]->(lk)
OPTIONAL MATCH (lk_srcDp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
               -[:DPROD_OUTPUT_DATASET]->(lk_ods)
RETURN
    cm.uri                       AS mapping_uri,
    cm.status                    AS status,
    cm.transformKind             AS transform_kind,
    cm.transformAuthor           AS transform_author,
    cm.transformExpression       AS transform_expression,
    cm.transformParams           AS transform_params_json,
    lv.role                      AS lookup_role,
    lv.strategy                  AS lookup_strategy,
    lk.uri                       AS source_col_uri,
    lk.name                      AS source_col_name,
    coalesce(lk.dataType, '')    AS source_col_type,
    coalesce(lk.ordinal, 0)      AS source_col_ordinal,
    coalesce(lk_ds.uri, lk_ods.uri)                AS source_table_uri,
    coalesce(lk_ds.schema, lk_srcDp.name)          AS source_schema,
    coalesce(lk_ds.name, lk_ods.physicalName, lk_ods.name) AS source_table,
    pc.uri                       AS product_col_uri
"""

MARKETPLACE_PRODUCT_COLUMNS_QUERY = """\
MATCH (dp:DProdDataProduct {uri: $uri})-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
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

# ── Optional multi-hop lineage (opt-in) ────────────────────────────────────
#
# Two extra hops the marketplace Lineage tab can request on demand (never on
# the default load, never for the engineer paths):
#   • UPSTREAM  — for a consumer-aligned product, the raw catalog behind its
#                 CONSUMES'd source products (one hop past the source cards).
#   • DOWNSTREAM — the data products that CONSUME this one.
# Both run only when their query param is true; the default response is
# byte-identical to before.

# From each dprod source column THIS product's current mappings resolve to,
# walk one hop up into the source product's OWN :ColumnMapping and resolve the
# raw catalog :Column/:Dataset behind it. `MATCH (cm)-[:MAPS_SOURCE_COLUMN]->
# (sc_dp:DProdColumn)` is deliberately NON-OPTIONAL: a source-aligned product's
# sources are raw :Column (not :DProdColumn), so it yields ZERO rows and the
# upstream toggle correctly no-ops. Cypher gotchas honoured: a WITH separator
# sits before the trailing OPTIONAL chain (a bare trailing WHERE would bind to
# the last OPTIONAL pattern), and every new binding is renamed (up_cm/raw/
# raw_ds/up_srcDp/up_ods/up_dp) so none collide with the primary query's
# cm/col/ds/srcDp/ods (variable reuse forces equality).
MARKETPLACE_UPSTREAM_GRAPH_QUERY = """\
MATCH (dp:DProdDataProduct {uri: $uri})
MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
      <-[:MAPS_TO_PRODUCT_COLUMN]-(cm:ColumnMapping)
WHERE cm.isCurrent = true
MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(sc_dp:DProdColumn)
// The source product that owns this source column — labels the upstream table.
OPTIONAL MATCH (up_dp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
               -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
               -[:HAS_PRODUCT_COLUMN]->(sc_dp)
// One hop up: the source product's own current mapping onto this source column.
MATCH (sc_dp)<-[:MAPS_TO_PRODUCT_COLUMN]-(up_cm:ColumnMapping)
WHERE up_cm.isCurrent = true
WITH sc_dp, up_cm, up_dp
// Resolve the raw catalog column/table behind the source product's mapping.
// Dual-OPTIONAL + coalesce so a deeper consumer chain (dprod source) resolves
// too, mirroring the primary query's source-side idiom.
OPTIONAL MATCH (up_cm)-[:MAPS_SOURCE_COLUMN]->(raw:Column)
OPTIONAL MATCH (raw_ds:Dataset)-[:HAS_COLUMN]->(raw)
OPTIONAL MATCH (up_cm)-[:MAPS_SOURCE_COLUMN]->(up_srcDp:DProdColumn)
OPTIONAL MATCH (up_ods:DProdOutputDataset)-[:HAS_PRODUCT_COLUMN]->(up_srcDp)
RETURN DISTINCT
    sc_dp.uri                                       AS target_col_uri,
    coalesce(raw.uri, up_srcDp.uri)                 AS up_col_uri,
    coalesce(raw.name, up_srcDp.name)               AS up_col_name,
    coalesce(raw.dataType, up_srcDp.dataType)       AS up_col_type,
    coalesce(raw.ordinal, up_srcDp.ordinal)         AS up_col_ordinal,
    coalesce(raw_ds.uri, up_ods.uri)                AS up_table_uri,
    coalesce(raw_ds.schema, '')                     AS up_schema,
    coalesce(raw_ds.name, up_ods.physicalName, up_ods.name) AS up_table,
    up_dp.uri                                       AS up_source_product_uri,
    up_dp.name                                      AS up_source_product_name,
    up_cm.status                                    AS status
LIMIT 2000
"""

# Companion of MARKETPLACE_UPSTREAM_GRAPH_QUERY for the LOOKUP side. A lookup
# transform reads a reference table via :LOOKUP_VIA (a *separate* edge from the
# primary :MAPS_SOURCE_COLUMN) — so the primary upstream query never walks it and
# the lookup reference tables (rendered at tier 0 via merge_lookup_graph_rows)
# dangle with no upstream. This mirrors the primary query's one-hop-up walk
# exactly, swapping the starting edge to :LOOKUP_VIA and binding lk_dp in sc_dp's
# role, so _fold_upstream consumes it unchanged. Kept as a SEPARATE query (not
# bolted onto the primary dual-OPTIONAL chain) per the CLAUDE.md :LOOKUP_VIA rule
# — avoids row multiplication + coalesce misclassification. Same gotcha
# discipline: MATCH (non-OPTIONAL) start so non-lookup products yield 0 rows;
# WITH separator before the trailing OPTIONAL chain; renamed bindings; DISTINCT.
MARKETPLACE_UPSTREAM_LOOKUP_QUERY = """\
MATCH (dp:DProdDataProduct {uri: $uri})
MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(:DProdColumn)
      <-[:MAPS_TO_PRODUCT_COLUMN]-(cm:ColumnMapping)
WHERE cm.isCurrent = true
MATCH (cm)-[:LOOKUP_VIA]->(lk_dp:DProdColumn)
// The source product that owns this lookup reference column — labels the raw table.
OPTIONAL MATCH (up_dp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
               -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
               -[:HAS_PRODUCT_COLUMN]->(lk_dp)
// One hop up: the source product's own current mapping onto the lookup ref column.
MATCH (lk_dp)<-[:MAPS_TO_PRODUCT_COLUMN]-(up_cm:ColumnMapping)
WHERE up_cm.isCurrent = true
WITH lk_dp, up_cm, up_dp
OPTIONAL MATCH (up_cm)-[:MAPS_SOURCE_COLUMN]->(raw:Column)
OPTIONAL MATCH (raw_ds:Dataset)-[:HAS_COLUMN]->(raw)
OPTIONAL MATCH (up_cm)-[:MAPS_SOURCE_COLUMN]->(up_srcDp:DProdColumn)
OPTIONAL MATCH (up_ods:DProdOutputDataset)-[:HAS_PRODUCT_COLUMN]->(up_srcDp)
RETURN DISTINCT
    lk_dp.uri                                       AS target_col_uri,
    coalesce(raw.uri, up_srcDp.uri)                 AS up_col_uri,
    coalesce(raw.name, up_srcDp.name)               AS up_col_name,
    coalesce(raw.dataType, up_srcDp.dataType)       AS up_col_type,
    coalesce(raw.ordinal, up_srcDp.ordinal)         AS up_col_ordinal,
    coalesce(raw_ds.uri, up_ods.uri)                AS up_table_uri,
    coalesce(raw_ds.schema, '')                     AS up_schema,
    coalesce(raw_ds.name, up_ods.physicalName, up_ods.name) AS up_table,
    up_dp.uri                                       AS up_source_product_uri,
    up_dp.name                                      AS up_source_product_name,
    up_cm.status                                    AS status
LIMIT 2000
"""

# The data products that CONSUME this one. Mirrors PRODUCT_DETAIL's consumed_by
# logic verbatim — same temporal :CONSUMES filter + lifecycle set — so the
# downstream tier can never disagree with the Overview tab's "Consumed by" list.
MARKETPLACE_DOWNSTREAM_QUERY = """\
MATCH (dp:DProdDataProduct {uri: $uri})
MATCH (cons_dc:DataContract)-[r_cc:CONSUMES]->(dp)
WHERE r_cc.fromVersion <= cons_dc.currentVersion
  AND (r_cc.toVersion IS NULL OR r_cc.toVersion >= cons_dc.currentVersion)
  AND cons_dc.currentLifecycleState IN ['published','superseded','approved','submitted','in_engineering']
MATCH (cons_dc)-[:MATERIALISES_AS]->(cons_dp:DProdDataProduct)
RETURN DISTINCT
    cons_dp.uri                                 AS consumer_uri,
    coalesce(cons_dp.name, '')                  AS consumer_name,
    coalesce(cons_dp.productKind, '')           AS consumer_product_kind,
    cons_dc.id                                  AS consumer_contract_id,
    coalesce(cons_dc.currentLifecycleState, '') AS consumer_lifecycle
"""


def _fold_upstream(rows: list) -> dict:
    """Fold raw upstream rows into ``{tables, edges}`` for the −1 tier.

    Dedups tables by ``up_table_uri`` and columns by ``up_col_uri`` (mirrors
    ``merge_lookup_graph_rows``' dedup style). Every emitted edge targets the
    ``target_col_uri`` — the existing source :DProdColumn URI already rendered
    on the canvas — so the frontend wires it straight to the source card's row.
    Rows with no parent dataset (orphan raw columns) are skipped, same as the
    primary loop's phantom-table guard.
    """
    tables: dict[str, dict] = {}
    seen_cols: set[str] = set()
    seen_edges: set[tuple] = set()
    edges: list[dict] = []
    # Track which raw tables fed a primary vs a lookup edge, so a table badges as
    # lookup only when it EXCLUSIVELY feeds lookups (a raw table feeding both a
    # primary mapping and a lookup stays a plain source card).
    primary_tbl_uris: set[str] = set()
    lookup_tbl_uris: set[str] = set()
    for r in rows:
        up_col_uri = r.get("up_col_uri")
        target_col_uri = r.get("target_col_uri")
        tbl_uri = r.get("up_table_uri")
        if not up_col_uri or not target_col_uri or not tbl_uri:
            continue
        is_lookup = bool(r.get("is_lookup"))
        (lookup_tbl_uris if is_lookup else primary_tbl_uris).add(tbl_uri)
        if tbl_uri not in tables:
            tables[tbl_uri] = {
                "uri": tbl_uri,
                "schema": r.get("up_schema"),
                "table": r.get("up_table"),
                "source_product_uri": r.get("up_source_product_uri"),
                "source_product_name": r.get("up_source_product_name"),
                "columns": [],
            }
        if up_col_uri not in seen_cols:
            seen_cols.add(up_col_uri)
            tables[tbl_uri]["columns"].append({
                "uri": up_col_uri,
                "name": r.get("up_col_name"),
                "data_type": r.get("up_col_type"),
                "ordinal": r.get("up_col_ordinal"),
            })
        edge_key = (up_col_uri, target_col_uri)
        if edge_key not in seen_edges:
            seen_edges.add(edge_key)
            edges.append({
                "source_uri": up_col_uri,
                "target_uri": target_col_uri,
                "status": r.get("status"),
                # Drives the frontend's dashed-violet styling + card badge so a
                # lookup-reference upstream reads distinctly from primary lineage.
                "is_lookup": is_lookup,
            })
    for tbl in tables.values():
        tbl["columns"].sort(key=lambda c: (c.get("ordinal") or 0, c.get("name") or ""))
        tbl["is_lookup"] = tbl["uri"] in lookup_tbl_uris and tbl["uri"] not in primary_tbl_uris
    tables_list = sorted(
        tables.values(),
        key=lambda t: ((t.get("schema") or ""), (t.get("table") or "")),
    )
    return {"tables": tables_list, "edges": edges}


def _fold_downstream(rows: list, product_uri: str) -> dict:
    """Fold consumer rows into ``{consumers, edges}`` for the +2 tier.

    Dedups consumers by URI; each carries the seam field ``consumer_kind =
    'data_product'`` (future report/software-component kinds slot in here). One
    aggregate edge per consumer anchors on ``product_uri``.
    """
    consumers: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        uri = r.get("consumer_uri")
        if not uri or uri in seen:
            continue
        seen.add(uri)
        consumers.append({
            "uri": uri,
            "name": r.get("consumer_name"),
            "product_kind": r.get("consumer_product_kind"),
            "contract_id": r.get("consumer_contract_id"),
            "lifecycle_state": r.get("consumer_lifecycle"),
            "consumer_kind": "data_product",
        })
        edges.append({
            "source_product_uri": product_uri,
            "target_product_uri": uri,
            "kind": "consumes",
        })
    return {"consumers": consumers, "edges": edges}


@router.get("/mapping-graph")
def get_mapping_graph(
    uri: str = QParam(..., description="Data product URI"),
    include_upstream: bool = QParam(
        False, description="Also return the raw catalog one hop past the source products (opt-in)."
    ),
    include_downstream: bool = QParam(
        False, description="Also return the data products that consume this one (opt-in)."
    ),
    session: Session = Depends(get_session),
):
    """Column-level graph payload for the marketplace Lineage tab.

    ``include_upstream`` / ``include_downstream`` are strictly additive: when
    both are false (the default, and the only mode the engineer paths ever hit)
    the response keys are byte-identical to the legacy shape.
    """
    settings = _get_settings(session)
    try:
        with _neo4j_from_settings(settings) as ns:
            mapping_rows = [dict(r) for r in ns.run(MARKETPLACE_MAPPING_GRAPH_QUERY, uri=uri)]
            product_rows = [dict(r) for r in ns.run(MARKETPLACE_PRODUCT_COLUMNS_QUERY, uri=uri)]
            lookup_rows = [dict(r) for r in ns.run(MARKETPLACE_LOOKUP_GRAPH_QUERY, uri=uri)]
            upstream_rows = []
            if include_upstream:
                upstream_rows = [dict(r) for r in ns.run(MARKETPLACE_UPSTREAM_GRAPH_QUERY, uri=uri)]
                # Companion lookup-reference upstream (tagged so the fold marks it
                # dashed-violet). Same one-hop-up walk from the :LOOKUP_VIA edge.
                for r in ns.run(MARKETPLACE_UPSTREAM_LOOKUP_QUERY, uri=uri):
                    row = dict(r)
                    row["is_lookup"] = True
                    upstream_rows.append(row)
            downstream_rows = (
                [dict(r) for r in ns.run(MARKETPLACE_DOWNSTREAM_QUERY, uri=uri)]
                if include_downstream else []
            )
    except Exception as e:
        raise HTTPException(500, f"Mapping graph query failed: {e}")

    # Group source-column rows into source tables (same logic as the engineer
    # endpoint — keep the dedup flow consistent). Skip literal-kind rows
    # entirely (no source side). Also skip rows where the :Column has no
    # parent :Dataset → :HAS_COLUMN edge (orphans from interrupted /
    # cross-project discovery loads); without this guard they bucket into a
    # phantom "None.None" table that grows across projects.
    source_tables: dict[str, dict] = {}
    seen_source_cols: set[str] = set()
    orphan_source_mapping_uris: set[str] = set()
    for r in mapping_rows:
        if not r.get("source_col_uri"):
            continue
        if (
            not r.get("source_table_uri")
            and not r.get("source_schema")
            and not r.get("source_table")
        ):
            muri = r.get("mapping_uri")
            if muri:
                orphan_source_mapping_uris.add(muri)
            continue
        tbl_uri = r.get("source_table_uri") or f"{r.get('source_schema')}.{r.get('source_table')}"
        if tbl_uri not in source_tables:
            source_tables[tbl_uri] = {
                "uri": tbl_uri,
                "schema": r.get("source_schema"),
                "table": r.get("source_table"),
                "columns": [],
            }
        col_uri = r.get("source_col_uri")
        if col_uri not in seen_source_cols:
            seen_source_cols.add(col_uri)
            source_tables[tbl_uri]["columns"].append({
                "uri": col_uri,
                "name": r.get("source_col_name"),
                "data_type": r.get("source_col_type"),
                "ordinal": r.get("source_col_ordinal"),
            })

    for tbl in source_tables.values():
        tbl["columns"].sort(key=lambda c: (c.get("ordinal") or 0, c.get("name") or ""))
    sources_list = sorted(
        source_tables.values(),
        key=lambda t: ((t.get("schema") or ""), (t.get("table") or "")),
    )

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
            # The hover tooltip renders the params block (lookup config, mask
            # format, bucket boundaries…) — without this the marketplace canvas
            # showed kind + expression only.
            "transform_params_json": r.get("transform_params_json"),
            "transform_escalation_reason": None,
            "similarity_score": r.get("similarity_score"),
            "literal_value": literal_value,
        })

    # Fold in lookup sources (reference tables a lookup-kind mapping reads from,
    # often a different CONSUMES'd product). Adds left-side source tables tagged
    # is_lookup + dashed lookup_via edges. Run after the primary loops so the
    # source-col dedup set is already populated.
    merge_lookup_graph_rows(lookup_rows, source_tables, seen_source_cols, mappings)
    for tbl in source_tables.values():
        tbl["columns"].sort(key=lambda c: (c.get("ordinal") or 0, c.get("name") or ""))
    sources_list = sorted(
        source_tables.values(),
        key=lambda t: ((t.get("schema") or ""), (t.get("table") or "")),
    )

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

    result = {
        "source_tables": sources_list,
        "product": {
            "uri": product_uri or uri,
            "name": product_name,
            "columns": product_columns,
        },
        "mappings": mappings,
        "orphan_source_mappings": len(orphan_source_mapping_uris),
    }
    # Attach the optional tiers only when requested, so the default response
    # (and every engineer render) is byte-identical to the legacy shape.
    if include_upstream:
        result["upstream"] = _fold_upstream(upstream_rows)
    if include_downstream:
        result["downstream"] = _fold_downstream(downstream_rows, product_uri or uri)
    return result


# ── Product report ────────────────────────────────────────────────────────
#
# Renders a Markdown report for one published or in-flight :DataContract.
# Walks the same queries the marketplace detail / lineage / mapping-graph
# routes use, plus a small :REFERENCES query for the per-dataset ERD,
# and stitches them into a Jinja template with Mermaid blocks for the
# erDiagram and lineage graph.
#
# Snapshot semantics: every fact in the rendered Markdown is true *at
# generation time only*. The header carries a UTC timestamp + disclaimer
# so a stale copy is self-evidently stale. No server-side persistence —
# every GET is a fresh walk of the graph.

# FK edges mirrored onto the product's output datasets by DPROD_PROPAGATE_FK
# (see CLAUDE.md:226). Used to draw relationship lines in the ERD section.
_REFERENCES_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})
      -[:MATERIALISES_AS]->(dp:DProdDataProduct)
      -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(src_ods:DProdOutputDataset)
      -[r:REFERENCES]->(tgt_ods:DProdOutputDataset)
RETURN
    src_ods.physicalName AS from_physical,
    tgt_ods.physicalName AS to_physical,
    r.columns            AS columns,
    r.referencedColumns  AS referenced_columns
"""


def _resolve_project_for_contract(contract_id: str, session: Session) -> "Project | None":
    """Mirror of ``_attach_project_id`` but returns the Project model.

    ``contract_id`` is always ``{project_code}-contract``; the owning
    project's Neo4j credentials live on the Project row.
    """
    if not contract_id or not contract_id.endswith("-contract"):
        return None
    project_code = contract_id[: -len("-contract")]
    return session.exec(
        select(Project).where(Project.project_code == project_code)
    ).first()


@router.get("/{contract_id}/report")
def generate_product_report(
    contract_id: str,
    session: Session = Depends(get_session),
):
    """Return a Markdown report describing one data product.

    The report renders fresh on every call. Sections are atomic — a contract
    with no mappings/quality/serving emits no Lineage/Quality/Serving section
    (not an empty one).
    """
    from datetime import datetime, timezone
    from pathlib import Path
    from fastapi import Response
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    from .odcs import _read_odcs_from_graph  # local import — avoids a top-level cycle
    from ..report_render import build_erd_for_dataset, build_lineage_graph

    # Project resolution — gives us the right Neo4j to query against, and
    # the archetype/project_code we surface in the header.
    project = _resolve_project_for_contract(contract_id, session)
    if not project:
        raise HTTPException(404, f"No project found for contract_id={contract_id}")

    # Spec dict (~80% of the report content). Returns None if the contract
    # doesn't exist on this project's graph.
    spec = _read_odcs_from_graph(contract_id, project)
    if not spec:
        raise HTTPException(404, f"Contract {contract_id} not found in graph")

    dprod_uri = f"dprod:{contract_id}"

    # Detail row (OSI / serving / lifecycle state / people).
    # PRODUCT_DETAIL pins to the deployed contract, so the detail surface
    # always reflects what's "live in the marketplace"; the spec from
    # _read_odcs_from_graph reads the current view (dc.currentVersion), which
    # may be an in-flight draft.
    # That divergence is fine — the header notes when an in-flight edit
    # exists, and the report's schema section reflects the spec (what the
    # PO is authoring), while quality scores + serving DDL reflect what
    # consumers actually see today.
    try:
        with neo4j_session(
            project.neo4j_host, project.neo4j_port,
            project.neo4j_user, project.neo4j_password, project.neo4j_database,
        ) as ns:
            detail_row = ns.run(PRODUCT_DETAIL, uri=dprod_uri).single()
            detail = _shape_detail(dict(detail_row)) if detail_row else {}

            mapping_rows = [dict(r) for r in ns.run(MARKETPLACE_MAPPING_GRAPH_QUERY, uri=dprod_uri)]
            ref_rows = [dict(r) for r in ns.run(_REFERENCES_QUERY, contract_id=contract_id)]
    except Exception as e:
        raise HTTPException(500, f"Graph query failed: {e}")

    # Per-dataset ERD blocks, keyed by physicalName so the template can
    # look them up alongside the spec's schema entries.
    refs_by_dataset: dict[str, list[dict]] = {}
    for ref in ref_rows:
        src = ref.get("from_physical") or ""
        if not src:
            continue
        refs_by_dataset.setdefault(src, []).append(ref)

    erd_by_dataset: dict[str, str] = {}
    for ds in spec.get("schema") or []:
        phys = ds.get("physicalName") or ""
        if not phys:
            continue
        erd_by_dataset[phys] = build_erd_for_dataset(
            ds, refs_by_dataset.get(phys, [])
        )

    # Dedup + enrich mappings for the per-column table. The lineage Mermaid
    # block uses the same row list so we don't render the same edge twice.
    seen_mappings: set[str] = set()
    mappings: list[dict] = []
    for r in mapping_rows:
        muri = r.get("mapping_uri")
        if not muri or muri in seen_mappings:
            continue
        seen_mappings.add(muri)
        # Extract literal value the same way the /mapping-graph endpoint does
        # so literal rows render uniformly in the report.
        literal_value: str | None = None
        if (r.get("transform_kind") or "").lower() == "literal":
            params_raw = r.get("transform_params_json")
            try:
                params = json.loads(params_raw) if isinstance(params_raw, str) else (params_raw or {})
            except (ValueError, TypeError):
                params = {}
            lv = params.get("literal_value") if isinstance(params, dict) else None
            literal_value = str(lv) if lv is not None else None
        # Pre-compute source_label so the template doesn't have to branch.
        if (r.get("transform_kind") or "").lower() == "literal":
            source_label = ""  # rendered separately as ``(literal: <val>)``
        else:
            parts = [p for p in (r.get("source_schema"), r.get("source_table"), r.get("source_col_name")) if p]
            source_label = ".".join(parts) if parts else "—"
        mappings.append({
            "uri": muri,
            "status": r.get("status"),
            "product_col_name": r.get("product_col_name"),
            "product_col_uri": r.get("product_col_uri"),
            "product_dataset_name": r.get("product_dataset_name"),
            "source_col_uri": r.get("source_col_uri"),
            "source_col_name": r.get("source_col_name"),
            "source_schema": r.get("source_schema"),
            "source_table": r.get("source_table"),
            "transform_kind": r.get("transform_kind"),
            "transform_author": r.get("transform_author"),
            "transform_expression": r.get("transform_expression"),
            "literal_value": literal_value,
            "source_label": source_label,
        })

    # Build the lineage Mermaid block from the deduped mapping list.
    lineage_graph = build_lineage_graph(mappings)

    # Group quality rules by source bucket for the Quality section.
    # ``_shape_detail`` already filtered out empty PROV-style rows from
    # PRODUCT_DETAIL's collect(); we just need to bucket what's left.
    quality_by_source: dict[str, list[dict]] = {}
    for q in detail.get("quality_rules") or []:
        src = q.get("source") or "contract"
        quality_by_source.setdefault(src, []).append(q)
    # Stable section ordering — group buckets so readers see the same shape
    # whether the rules came from one source or four.
    _ORDER = ["spec", "domain", "user", "observation", "contract"]
    quality_by_source = {
        k: quality_by_source[k]
        for k in sorted(
            quality_by_source.keys(),
            key=lambda s: (_ORDER.index(s) if s in _ORDER else len(_ORDER), s),
        )
    }

    osi_block: dict | None = None
    if detail.get("osi_band") or detail.get("osi_evaluated_at"):
        osi_block = {
            "osi_band": detail.get("osi_band"),
            "osi_completeness": detail.get("osi_completeness"),
            "osi_conformance_pass": detail.get("osi_conformance_pass"),
            "osi_evaluated_at": detail.get("osi_evaluated_at"),
        }

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # Frontend URL — same convention as the CLI bootstrap. Single-tenant
    # demo: hardcoded localhost. Move to AppSettings if hosting changes.
    frontend_base_url = "http://localhost:5173"

    # Render through the Jinja template. Templates live next to the
    # cli-bootstrap templates so we reuse the same loader pattern.
    templates_dir = Path(__file__).resolve().parent.parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(enabled_extensions=()),
        keep_trailing_newline=True,
    )
    md = env.get_template("product_report.md.j2").render(
        contract_id=contract_id,
        spec=spec,
        detail=detail,
        project=project,
        mappings=mappings,
        lineage_graph=lineage_graph,
        erd_by_dataset=erd_by_dataset,
        quality_by_source=quality_by_source,
        osi_block=osi_block,
        generated_at_utc=generated_at,
        frontend_base_url=frontend_base_url,
    )

    # Filename: contract_id + compact timestamp so successive downloads
    # don't overwrite each other on disk.
    ts_compact = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{contract_id}-{ts_compact}.md"
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{contract_id}/odcs.yaml")
def download_product_odcs_yaml(
    contract_id: str,
    download: bool = False,
    version: Optional[int] = QParam(None, description="Pin to a historical version"),
    session: Session = Depends(get_session),
):
    """Return the ODCS v3.1 YAML for one marketplace data product.

    Stable URL pattern keyed on the marketplace contract_id. Programmatic
    callers (curl, CI, downstream catalog importers) get the raw YAML body.
    Browsers hitting the URL directly see the YAML inline; ?download=1 sets
    Content-Disposition: attachment so the browser saves rather than renders.
    The marketplace UI uses the Blob + a.download pattern so its filename
    is deterministic regardless of the disposition header.

    Default (no ``version``) reads the current view (dc.currentVersion), so
    an in-flight PO edit will be reflected. ``?version=N`` re-materializes
    the spec as it looked at that historical version.
    """
    from .odcs import _read_odcs_from_graph

    project = _resolve_project_for_contract(contract_id, session)
    if not project:
        raise HTTPException(404, f"No project found for contract_id={contract_id}")

    spec = _read_odcs_from_graph(contract_id, project, version=version)
    if not spec:
        raise HTTPException(404, f"Contract {contract_id} not found in graph")

    yaml_str = yaml.dump(
        spec,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )

    product_name = spec.get("name") or contract_id
    version = spec.get("version") or "v0"
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(product_name)).strip("_") or "product"
    filename = f"{safe}-{version}.odcs.yaml"

    disposition_type = "attachment" if download else "inline"
    return Response(
        content=yaml_str,
        media_type="application/x-yaml; charset=utf-8",
        headers={
            "Content-Disposition": f'{disposition_type}; filename="{filename}"',
            "Cache-Control": "no-cache",
        },
    )


# ── Consumer-facing QA probe ─────────────────────────────────────────────


class _MarketplaceProbeBody(BaseModel):
    uri: str
    question: str


@router.post("/qa/probe")
async def probe_product_question(
    body: _MarketplaceProbeBody,
    session: Session = Depends(get_session),
):
    """Probe whether a free-form consumer question can be answered by the
    product at ``uri``.

    Consumer-facing entry point — no project_id in the request. The owning
    project is resolved from the dprod URI (``dprod:{project_code}-contract``)
    so the analyzer can run against the right Neo4j. Mirrors the engineer
    endpoint at ``POST /api/projects/{id}/qa/probe`` but never leaks
    ``project_id`` over the wire. Never persists — purely ephemeral.
    """
    from .qa import _run_qa_analyzer, _parse_qa_payload, QA_TIMEOUT_SECONDS
    from .. import qa as qa_engine
    import asyncio as _asyncio

    question = (body.question or "").strip()
    if not question:
        raise HTTPException(400, "question is required")
    uri = (body.uri or "").strip()
    if not uri.startswith("dprod:"):
        raise HTTPException(400, "uri must be a dprod URI")
    contract_id = uri[len("dprod:"):]
    if not contract_id:
        raise HTTPException(400, "uri is missing the contract id portion")

    project = _resolve_project_for_contract(contract_id, session)
    if not project:
        raise HTTPException(404, f"No project found for contract_id={contract_id}")

    context = qa_engine.build_qa_context(project, contract_id)
    if context is None or not (context.get("datasets") or []):
        raise HTTPException(
            409,
            "This product hasn't been materialised yet — nothing to probe against.",
        )

    try:
        payload, advisor_error = await _asyncio.wait_for(
            _run_qa_analyzer(context, mode="probe", question=question),
            timeout=QA_TIMEOUT_SECONDS,
        )
    except _asyncio.TimeoutError:
        payload = _parse_qa_payload("", "probe")
        advisor_error = f"Analyzer timed out after {QA_TIMEOUT_SECONDS}s"

    return {
        "uri": uri,
        "question": question,
        "verdict": payload.get("verdict", "no"),
        "confidence": payload.get("confidence", "low"),
        "reasoning": payload.get("reasoning", ""),
        "supporting_columns": payload.get("supporting_columns", []),
        "supporting_rules": payload.get("supporting_rules", []),
        "gaps": payload.get("gaps", []),
        "advisor_error": advisor_error,
    }
