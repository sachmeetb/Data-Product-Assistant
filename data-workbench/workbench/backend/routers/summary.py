import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query as QParam
from sqlmodel import Session, select

from ..database import get_session
from ..graph_ops import has_project_node
from ..models import DQTestRun, Project
from ..neo4j_client import neo4j_session

router = APIRouter(prefix="/api/projects/{project_id}/summary", tags=["summary"])

# ── Scoping prefixes (used when :Project node exists) ─────────────────────

_PRJ_DS = (
    "MATCH (:Project {projectCode: $project_code})"
    "-[:HAS_CATALOG]->(:Catalog)-[:DCAT_DATASET]->"
)
_PRJ_CONTRACT = (
    "MATCH (:Project {projectCode: $project_code})"
    "-[:HAS_CONTRACT]->(:DataContract)-[:MATERIALISES_AS]->"
)

# ── Count Queries (unscoped / scoped pairs) ───────────────────────────────

DATASET_COUNT = "MATCH (ds:Dataset) RETURN count(ds) AS cnt"
DATASET_COUNT_S = f"{_PRJ_DS}(ds:Dataset) RETURN count(ds) AS cnt"

COLUMN_COUNT = "MATCH (col:Column) RETURN count(col) AS cnt"
COLUMN_COUNT_S = f"{_PRJ_DS}(:Dataset)-[:HAS_COLUMN]->(col:Column) RETURN count(col) AS cnt"

GRAPH_NODE_COUNT = "MATCH (n) RETURN count(n) AS cnt"
GRAPH_REL_COUNT = "MATCH ()-[r]->() RETURN count(r) AS cnt"

# Count the contract + DPROD subgraph reachable from the Project node. Used
# for DPE-CF projects whose pre-discovery state contains no :Dataset /
# :Column nodes — the wizard-driven contract, schemas, properties, the
# materialised DProd nodes, and any PropertyShape rules attached to them.
CONTRACT_SUBGRAPH_COUNT_S = """\
MATCH (prj:Project {projectCode: $project_code})-[:HAS_CONTRACT]->(dc:DataContract)
OPTIONAL MATCH (dc)-[*1..5]->(n)
WHERE n IS NOT NULL AND any(lbl IN labels(n) WHERE
  lbl IN ['DataContractSchema','DataContractProperty','DataContractOwner',
          'DataContractSteward','DataContractTeamMember','DataContractRole',
          'DataContractServer','DataContractSLAProperty','DataContractQuality',
          'DataContractTerms','DProdDataProduct','DProdOutputPort',
          'DProdOutputDataset','DProdColumn','DProdNodeShape','PropertyShape']
)
RETURN count(DISTINCT dc) + count(DISTINCT n) AS cnt
"""

DESCRIPTION_STATS = """\
MATCH (cd:ColumnDescription) WHERE cd.isCurrent = true
RETURN
    count(cd) AS total,
    count(CASE WHEN cd.status = 'approved' THEN 1 END) AS approved,
    count(CASE WHEN cd.status = 'rejected' THEN 1 END) AS rejected,
    count(CASE WHEN cd.status = 'pending_review' THEN 1 END) AS pending
"""
DESCRIPTION_STATS_S = f"""\
{_PRJ_DS}(:Dataset)-[:HAS_COLUMN]->(:Column)-[:HAS_DESCRIPTION]->(cd:ColumnDescription)
WHERE cd.isCurrent = true
RETURN
    count(cd) AS total,
    count(CASE WHEN cd.status = 'approved' THEN 1 END) AS approved,
    count(CASE WHEN cd.status = 'rejected' THEN 1 END) AS rejected,
    count(CASE WHEN cd.status = 'pending_review' THEN 1 END) AS pending
"""

FINAL_DESCRIPTIONS_COUNT = """\
MATCH (col:Column)-[:HAS_DESCRIPTION]->(cd:ColumnDescription {isCurrent: true})
RETURN count(cd) AS cnt
"""
FINAL_DESCRIPTIONS_COUNT_S = f"""\
{_PRJ_DS}(:Dataset)-[:HAS_COLUMN]->(col:Column)-[:HAS_DESCRIPTION]->(cd:ColumnDescription {{isCurrent: true}})
RETURN count(cd) AS cnt
"""

# Relationship descriptions are project-scoped via :Project → :Catalog →
# :Dataset → :HAS_RELATIONSHIP_DESCRIPTION → :RelationshipDescription.
# No unscoped variant — pre-:Project legacy projects don't have FK
# descriptions to count.
RELATIONSHIP_STATS_S = f"""\
{_PRJ_DS}(:Dataset)-[:HAS_RELATIONSHIP_DESCRIPTION]->(rd:RelationshipDescription)
WHERE rd.isCurrent = true
RETURN
    count(rd) AS total,
    count(CASE WHEN rd.status = 'approved' THEN 1 END) AS approved,
    count(CASE WHEN rd.status = 'rejected' THEN 1 END) AS rejected,
    count(CASE WHEN rd.status = 'pending_review' THEN 1 END) AS pending
"""

MAPPING_STATS = """\
MATCH (cm:ColumnMapping) WHERE cm.isCurrent = true
RETURN
    count(cm) AS total,
    count(CASE WHEN cm.status = 'approved' THEN 1 END) AS approved,
    count(CASE WHEN cm.status = 'rejected' THEN 1 END) AS rejected,
    count(CASE WHEN cm.status = 'pending_review' THEN 1 END) AS pending
"""

# Anchor on the project's own :DProdColumn instead of source :Dataset so the
# count includes consumer-aligned mappings (whose source is :DProdColumn,
# not :Column) AND literal mappings (which have no source edge at all).
MAPPING_STATS_S = """\
MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
WHERE cm.isCurrent = true
  AND pc.uri STARTS WITH 'dprod:col:' + $project_code + '-contract:'
RETURN
    count(DISTINCT cm) AS total,
    count(DISTINCT CASE WHEN cm.status = 'approved' THEN cm END) AS approved,
    count(DISTINCT CASE WHEN cm.status = 'rejected' THEN cm END) AS rejected,
    count(DISTINCT CASE WHEN cm.status = 'pending_review' THEN cm END) AS pending
"""

PROFILING_STATS = """\
MATCH (col:Column)
OPTIONAL MATCH (col)-[:HAS_QUALITY_MEASUREMENT]->(qm:QualityMeasurement)
RETURN
    count(DISTINCT col) AS total_columns,
    count(DISTINCT CASE WHEN qm IS NOT NULL THEN col END) AS profiled_columns,
    count(qm) AS total_metrics
"""
PROFILING_STATS_S = f"""\
{_PRJ_DS}(:Dataset)-[:HAS_COLUMN]->(col:Column)
OPTIONAL MATCH (col)-[:HAS_QUALITY_MEASUREMENT]->(qm:QualityMeasurement)
RETURN
    count(DISTINCT col) AS total_columns,
    count(DISTINCT CASE WHEN qm IS NOT NULL THEN col END) AS profiled_columns,
    count(qm) AS total_metrics
"""

PROFILING_DETAIL = """\
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)-[:HAS_QUALITY_MEASUREMENT]->(qm:QualityMeasurement)-[:ON_METRIC]->(m:Metric)
WHERE ($table IS NULL OR ds.name = $table)
  AND ($column IS NULL OR col.name = $column)
RETURN ds.schema AS schema, ds.name AS table_name, col.name AS column_name,
       m.name AS metric, qm.value AS value, m.unit AS unit
ORDER BY ds.schema, ds.name, col.ordinal, m.name
"""
PROFILING_DETAIL_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_COLUMN]->(col:Column)-[:HAS_QUALITY_MEASUREMENT]->(qm:QualityMeasurement)-[:ON_METRIC]->(m:Metric)
WHERE ($table IS NULL OR ds.name = $table)
  AND ($column IS NULL OR col.name = $column)
RETURN ds.schema AS schema, ds.name AS table_name, col.name AS column_name,
       m.name AS metric, qm.value AS value, m.unit AS unit
ORDER BY ds.schema, ds.name, col.ordinal, m.name
"""

SERVING_QUERY = """\
MATCH (dp:DProdDataProduct)-[:SERVED_BY]->(sd:ServingDefinition)
RETURN sd.servingMode AS serving_mode, sd.viewName AS view_name,
       coalesce(sd.viewNames, '[]') AS view_names_json,
       sd.targetPlatform AS platform, sd.ddl AS ddl, sd.viewSchema AS view_schema,
       coalesce(sd.summaryJson, '') AS summary_json,
       coalesce(sd.deploymentStatus, 'pending') AS deployment_status,
       toString(sd.deployedAt) AS deployed_at,
       sd.deployedTo AS deployed_to,
       sd.deploymentError AS deployment_error,
       sd.dbtMaterialization AS dbt_materialization,
       sd.targetSchema AS target_schema,
       coalesce(sd.buildStatus, '') AS build_status,
       toString(sd.builtAt) AS built_at,
       coalesce(sd.modelsJson, '') AS models_json,
       sd.buildError AS build_error
"""
SERVING_QUERY_S = f"""\
{_PRJ_CONTRACT}(dp:DProdDataProduct)-[:SERVED_BY]->(sd:ServingDefinition)
RETURN sd.servingMode AS serving_mode, sd.viewName AS view_name,
       coalesce(sd.viewNames, '[]') AS view_names_json,
       sd.targetPlatform AS platform, sd.ddl AS ddl, sd.viewSchema AS view_schema,
       coalesce(sd.summaryJson, '') AS summary_json,
       coalesce(sd.deploymentStatus, 'pending') AS deployment_status,
       toString(sd.deployedAt) AS deployed_at,
       sd.deployedTo AS deployed_to,
       sd.deploymentError AS deployment_error,
       sd.dbtMaterialization AS dbt_materialization,
       sd.targetSchema AS target_schema,
       coalesce(sd.buildStatus, '') AS build_status,
       toString(sd.builtAt) AS built_at,
       coalesce(sd.modelsJson, '') AS models_json,
       sd.buildError AS build_error
"""

SERVING_COUNT = """\
MATCH (:DProdDataProduct)-[:SERVED_BY]->(sd:ServingDefinition)
RETURN count(sd) AS cnt
"""
SERVING_COUNT_S = f"""\
{_PRJ_CONTRACT}(:DProdDataProduct)-[:SERVED_BY]->(sd:ServingDefinition)
RETURN count(sd) AS cnt
"""

# PO serving preference, stored as JSON on :DataContract.customProperties by
# the wizard (po_serving_preference / po_serving_reason).
PO_SERVING_QUERY = """\
MATCH (prj:Project {projectCode: $project_code})-[:HAS_CONTRACT]->(dc:DataContract)
RETURN dc.customProperties AS cp LIMIT 1
"""

DQ_RULE_COUNT = """\
MATCH (ps:PropertyShape)
RETURN count(ps) AS cnt,
       count(CASE WHEN ps.ruleSource = 'domain' THEN 1 END) AS domain_cnt,
       count(CASE WHEN ps.ruleSource IS NULL OR ps.ruleSource = 'observation' THEN 1 END) AS observed_cnt,
       count(CASE WHEN ps.ruleSource = 'user' THEN 1 END) AS user_cnt,
       count(CASE WHEN ps.ruleSource = 'spec' THEN 1 END) AS spec_cnt,
       count(CASE WHEN coalesce(ps.status, 'approved') = 'pending_review' THEN 1 END) AS pending_cnt,
       count(CASE WHEN coalesce(ps.status, 'approved') = 'approved' THEN 1 END) AS approved_cnt,
       count(CASE WHEN coalesce(ps.status, 'approved') = 'rejected' THEN 1 END) AS rejected_cnt
"""
DQ_RULE_COUNT_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_SHAPE]->(:NodeShape)-[:PROPERTY]->(ps:PropertyShape)
RETURN count(ps) AS cnt,
       count(CASE WHEN ps.ruleSource = 'domain' THEN 1 END) AS domain_cnt,
       count(CASE WHEN ps.ruleSource IS NULL OR ps.ruleSource = 'observation' THEN 1 END) AS observed_cnt,
       count(CASE WHEN ps.ruleSource = 'user' THEN 1 END) AS user_cnt,
       count(CASE WHEN ps.ruleSource = 'spec' THEN 1 END) AS spec_cnt,
       count(CASE WHEN coalesce(ps.status, 'approved') = 'pending_review' THEN 1 END) AS pending_cnt,
       count(CASE WHEN coalesce(ps.status, 'approved') = 'approved' THEN 1 END) AS approved_cnt,
       count(CASE WHEN coalesce(ps.status, 'approved') = 'rejected' THEN 1 END) AS rejected_cnt
"""

# ── Data product (DProd) counts ────────────────────────────────────────────
# All scoped via the project's HAS_CONTRACT edge — wizard-driven products
# always have a :Project node so an unscoped variant isn't needed.

DPROD_PRODUCTS_COUNT_S = """\
MATCH (:Project {projectCode: $project_code})-[:HAS_CONTRACT]->(:DataContract)
      -[:MATERIALISES_AS]->(dp:DProdDataProduct)
RETURN count(DISTINCT dp) AS cnt
"""

DPROD_COLUMNS_COVERAGE_S = """\
MATCH (:Project {projectCode: $project_code})-[:HAS_CONTRACT]->(:DataContract)
      -[:MATERIALISES_AS]->(:DProdDataProduct)
      -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
OPTIONAL MATCH (pc)<-[:MAPS_TO_PRODUCT_COLUMN]-(cm:ColumnMapping)
WITH pc, count(DISTINCT cm) AS mapping_count
RETURN count(pc) AS total,
       sum(CASE WHEN mapping_count > 0 THEN 1 ELSE 0 END) AS mapped
"""

# Consumer-aligned: count :CONSUMES'd source products + their datasets +
# their columns. dpe-cf only — for source-aligned and other archetypes the
# call site skips this query and the counts default to 0. The :CONSUMES
# edge is temporal in the stable model; filter to active at currentVersion.
INPUTS_STATS_S = """\
MATCH (:Project {projectCode: $project_code})-[:HAS_CONTRACT]->(dc:DataContract)
OPTIONAL MATCH (dc)-[r:CONSUMES]->(srcDp:DProdDataProduct)
WHERE r.fromVersion <= dc.currentVersion
  AND (r.toVersion IS NULL OR r.toVersion >= dc.currentVersion)
OPTIONAL MATCH (srcDp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
               -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
OPTIONAL MATCH (ods)-[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
RETURN
    count(DISTINCT srcDp) AS source_products,
    count(DISTINCT ods)   AS source_datasets,
    count(DISTINCT pc)    AS source_columns
"""

# Per-source-product breakdown for the Inputs card detail. Each row is one
# consumed source product with its dataset + column counts so the dashboard
# can deep-link to the marketplace detail page.
INPUTS_DETAIL_S = """\
MATCH (:Project {projectCode: $project_code})-[:HAS_CONTRACT]->(dc:DataContract)
MATCH (dc)-[r:CONSUMES]->(srcDp:DProdDataProduct)
WHERE r.fromVersion <= dc.currentVersion
  AND (r.toVersion IS NULL OR r.toVersion >= dc.currentVersion)
OPTIONAL MATCH (srcDp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
               -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
OPTIONAL MATCH (ods)-[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
WITH srcDp,
     count(DISTINCT ods) AS dataset_count,
     count(DISTINCT pc)  AS column_count
RETURN
    srcDp.uri  AS source_product_uri,
    coalesce(srcDp.name, '') AS source_product_name,
    coalesce(srcDp.productKind, '') AS product_kind,
    dataset_count,
    column_count
ORDER BY source_product_name
"""

DPROD_COLUMNS_DETAIL_S = """\
MATCH (:Project {projectCode: $project_code})-[:HAS_CONTRACT]->(:DataContract)
      -[:MATERIALISES_AS]->(dp:DProdDataProduct)
      -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
WHERE ($table IS NULL OR ods.physicalName = $table OR ods.name = $table)
  AND ($column IS NULL OR pc.name = $column)
OPTIONAL MATCH (pc)<-[:MAPS_TO_PRODUCT_COLUMN]-(cm:ColumnMapping)
WITH dp, ods, pc, count(DISTINCT cm) AS mapping_count
RETURN
    dp.name AS product_name,
    coalesce(ods.physicalName, ods.name, '') AS dataset_name,
    pc.name AS column_name,
    coalesce(pc.logicalType, '') AS logical_type,
    coalesce(pc.dataType, '') AS physical_type,
    CASE WHEN coalesce(pc.isPrimaryKey, false) THEN 'PK' ELSE '' END AS primary_key,
    coalesce(pc.description, '') AS description,
    coalesce(pc.sensitivity, 'none') AS sensitivity,
    CASE WHEN mapping_count > 0 THEN 'mapped' ELSE 'unmapped' END AS mapping_status
ORDER BY dp.name, dataset_name, column_name
"""


# The synthesized/authored contract header — the headline materialization output.
# Matched via :Project-[:HAS_CONTRACT] because :DataContract.uri is null (the node
# is keyed by .id = '{project_code}-contract'); one row per contract version with
# schema/property/owner counts and whether it has materialised to a :DProdDataProduct.
CONTRACT_DETAIL_S = """\
MATCH (:Project {projectCode: $project_code})-[:HAS_CONTRACT]->(dc:DataContract)
OPTIONAL MATCH (dc)-[:HAS_SCHEMA]->(sc:DataContractSchema)
OPTIONAL MATCH (sc)-[:HAS_PROPERTY]->(prop:DataContractProperty)
OPTIONAL MATCH (dc)-[:HAS_OWNER]->(o:DataContractOwner)
OPTIONAL MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
RETURN dc.id AS contract_id,
       coalesce(dc.name, '') AS name,
       coalesce(dc.productKind, '') AS product_kind,
       coalesce(dc.lifecycleState, dc.currentLifecycleState, dc.status) AS lifecycle_state,
       coalesce(toString(dc.version), dc.lifecycleVersion, '') AS version,
       count(DISTINCT sc) AS schemas,
       count(DISTINCT prop) AS properties,
       count(DISTINCT o) AS owners,
       count(DISTINCT dp) AS materialised_products
ORDER BY contract_id
"""


SCORING_STATS = """\
MATCH (ds:Dataset)-[:HAS_QUALITY_SCORE]->(qs:QualityScore)
WHERE qs.level = 'dataset' AND qs.dimension = 'composite'
WITH qs.batchId AS batchId, max(qs.scoredAt) AS latest
ORDER BY latest DESC LIMIT 1
WITH batchId
MATCH (ds:Dataset)-[:HAS_QUALITY_SCORE]->(qs:QualityScore {batchId: batchId})
WHERE qs.level = 'dataset' AND qs.dimension = 'composite'
RETURN count(qs) AS scored_datasets, avg(qs.score) AS avg_composite
"""
SCORING_STATS_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_QUALITY_SCORE]->(qs:QualityScore)
WHERE qs.level = 'dataset' AND qs.dimension = 'composite'
WITH qs.batchId AS batchId, max(qs.scoredAt) AS latest
ORDER BY latest DESC LIMIT 1
WITH batchId
MATCH (:Project {{projectCode: $project_code}})-[:HAS_CATALOG]->(:Catalog)-[:DCAT_DATASET]->(ds:Dataset)-[:HAS_QUALITY_SCORE]->(qs:QualityScore {{batchId: batchId}})
WHERE qs.level = 'dataset' AND qs.dimension = 'composite'
RETURN count(qs) AS scored_datasets, avg(qs.score) AS avg_composite
"""

ALLOWED_VALUES_COUNT = """\
MATCH (col:Column)-[:HAS_TOP_VALUE]->(tv:TopValue)
WITH col, collect(tv.value) AS vals, sum(tv.frequency) AS coverage
WHERE coverage > 0.95
RETURN count(col) AS cnt
"""
ALLOWED_VALUES_COUNT_S = f"""\
{_PRJ_DS}(:Dataset)-[:HAS_COLUMN]->(col:Column)-[:HAS_TOP_VALUE]->(tv:TopValue)
WITH col, collect(tv.value) AS vals, sum(tv.frequency) AS coverage
WHERE coverage > 0.95
RETURN count(col) AS cnt
"""

# ── Playbook / Learning queries (already domain-scoped, no Project needed) ─

PLAYBOOK_COUNT = """\
MATCH (d:Domain {name: $domain})-[:HAS_PLAYBOOK]->(pb:Playbook)-[:HAS_ITEM]->(pi:PlaybookItem)
WHERE pi.isCurrent = true
RETURN count(pi) AS cnt
"""

PLAYBOOK_DETAIL = """\
MATCH (d:Domain {name: $domain})-[:HAS_PLAYBOOK]->(pb:Playbook)-[:HAS_ITEM]->(pi:PlaybookItem)
WHERE pi.isCurrent = true
OPTIONAL MATCH (pi)-[:PROV_WAS_GENERATED_BY]->(act:ProvActivity)
RETURN
    pb.phase       AS phase,
    pi.rule        AS rule,
    pi.version     AS version,
    act.rationale  AS rationale,
    act.occurredAt AS updated_at,
    act.operation  AS operation
ORDER BY pb.phase, pi.version DESC
"""

LEARNING_HISTORY_QUERY = """\
MATCH (d:Domain {name: $domain})-[:HAS_PLAYBOOK]->(pb:Playbook)-[:HAS_VERSION]->(pv:PlaybookVersion)
RETURN
    pv.version     AS version,
    pv.summary     AS summary,
    pv.createdAt   AS created_at,
    pv.itemsAdded  AS items_added,
    pv.itemsUpdated AS items_updated,
    pv.itemsRemoved AS items_removed,
    pb.phase       AS phase
ORDER BY pv.version DESC
"""

LEARNING_HISTORY_COUNT = """\
MATCH (d:Domain {name: $domain})-[:HAS_PLAYBOOK]->(pb:Playbook)-[:HAS_VERSION]->(pv:PlaybookVersion)
RETURN count(pv) AS cnt
"""

# ── Detail Queries (all support $table / $column filters) ──────────────────

DATASETS_DETAIL = """\
MATCH (ds:Dataset)
WHERE ($table IS NULL OR ds.name = $table)
OPTIONAL MATCH (ds)-[:HAS_COLUMN]->(col:Column)
OPTIONAL MATCH (ds)-[:HAS_TABLE_DESCRIPTION]->(td:TableDescription)
WHERE td.isCurrent = true
RETURN ds.schema AS schema, ds.name AS name, ds.uri AS uri,
       count(col) AS column_count,
       coalesce(td.text, '') AS description,
       coalesce(td.relationshipKind, '') AS relationship_kind,
       td.uri AS table_desc_uri
ORDER BY schema, name
"""
DATASETS_DETAIL_S = f"""\
{_PRJ_DS}(ds:Dataset)
WHERE ($table IS NULL OR ds.name = $table)
OPTIONAL MATCH (ds)-[:HAS_COLUMN]->(col:Column)
OPTIONAL MATCH (ds)-[:HAS_TABLE_DESCRIPTION]->(td:TableDescription)
WHERE td.isCurrent = true
RETURN ds.schema AS schema, ds.name AS name, ds.uri AS uri,
       count(col) AS column_count,
       coalesce(td.text, '') AS description,
       coalesce(td.relationshipKind, '') AS relationship_kind,
       td.uri AS table_desc_uri
ORDER BY schema, name
"""

COLUMNS_DETAIL = """\
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)
WHERE ($table IS NULL OR ds.name = $table)
  AND ($column IS NULL OR col.name = $column)
RETURN col.uri AS col_uri, ds.schema AS schema, ds.name AS table_name,
       col.name AS column_name, col.dataType AS data_type, col.ordinal AS ordinal
ORDER BY ds.schema, ds.name, col.ordinal
"""
COLUMNS_DETAIL_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_COLUMN]->(col:Column)
WHERE ($table IS NULL OR ds.name = $table)
  AND ($column IS NULL OR col.name = $column)
RETURN col.uri AS col_uri, ds.schema AS schema, ds.name AS table_name,
       col.name AS column_name, col.dataType AS data_type, col.ordinal AS ordinal
ORDER BY ds.schema, ds.name, col.ordinal
"""

DESCRIPTIONS_DETAIL = """\
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)-[:HAS_DESCRIPTION]->(cd:ColumnDescription)
WHERE cd.isCurrent = true
  AND ($table IS NULL OR ds.name = $table)
  AND ($column IS NULL OR col.name = $column)
RETURN ds.schema AS schema, ds.name AS table_name, col.name AS column_name,
       cd.text AS description, cd.status AS status,
       cd.uri AS desc_uri, col.uri AS col_uri
ORDER BY ds.schema, ds.name, col.ordinal
"""
DESCRIPTIONS_DETAIL_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_COLUMN]->(col:Column)-[:HAS_DESCRIPTION]->(cd:ColumnDescription)
WHERE cd.isCurrent = true
  AND ($table IS NULL OR ds.name = $table)
  AND ($column IS NULL OR col.name = $column)
RETURN ds.schema AS schema, ds.name AS table_name, col.name AS column_name,
       cd.text AS description, cd.status AS status,
       cd.uri AS desc_uri, col.uri AS col_uri
ORDER BY ds.schema, ds.name, col.ordinal
"""

FINAL_DESCRIPTIONS_DETAIL = """\
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)-[:HAS_DESCRIPTION]->(cd:ColumnDescription {isCurrent: true})
WHERE ($table IS NULL OR ds.name = $table)
  AND ($column IS NULL OR col.name = $column)
RETURN ds.schema AS schema, ds.name AS table_name, col.name AS column_name,
       col.uri AS col_uri, cd.uri AS desc_uri, cd.text AS description, cd.status AS status
ORDER BY ds.schema, ds.name, col.name
"""
FINAL_DESCRIPTIONS_DETAIL_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_COLUMN]->(col:Column)-[:HAS_DESCRIPTION]->(cd:ColumnDescription {{isCurrent: true}})
WHERE ($table IS NULL OR ds.name = $table)
  AND ($column IS NULL OR col.name = $column)
RETURN ds.schema AS schema, ds.name AS table_name, col.name AS column_name,
       col.uri AS col_uri, cd.uri AS desc_uri, cd.text AS description, cd.status AS status
ORDER BY ds.schema, ds.name, col.name
"""

# Relationship descriptions detail — one row per :RelationshipDescription
# scoped to the project. The $table filter applies to the FROM-side
# dataset's name so the dashboard can drill into "show me the
# relationships starting from order_item". $column is unused here (FK
# columns live on the :REFERENCES edge, not the description node — pass
# null and ignore the filter when this card is active).
RELATIONSHIPS_DETAIL_S = f"""\
{_PRJ_DS}(from_ds:Dataset)-[:HAS_RELATIONSHIP_DESCRIPTION]->(rd:RelationshipDescription)
      -[:DESCRIBES_REFERENCE_TO]->(to_ds:Dataset)
WHERE rd.isCurrent = true
  AND ($table IS NULL OR from_ds.name = $table OR to_ds.name = $table)
RETURN from_ds.schema                                   AS schema,
       from_ds.name                                     AS table_name,
       to_ds.schema                                     AS to_schema,
       to_ds.name                                       AS to_table,
       coalesce(rd.relationshipNature, 'references')    AS relationship_nature,
       rd.text                                          AS description,
       coalesce(rd.status, 'pending_review')            AS status,
       rd.uri                                           AS desc_uri
ORDER BY from_ds.schema, from_ds.name, to_ds.name
"""

MAPPINGS_DETAIL = """\
MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
WHERE cm.isCurrent = true
OPTIONAL MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(col:Column)<-[:HAS_COLUMN]-(ds:Dataset)
OPTIONAL MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(sc_dp:DProdColumn)
               <-[:HAS_PRODUCT_COLUMN]-(ods:DProdOutputDataset)
               <-[:DPROD_OUTPUT_DATASET]-(:DProdOutputPort)
               <-[:DPROD_OUTPUT_PORT]-(srcDp:DProdDataProduct)
WITH cm, pc, col, ds, sc_dp, ods, srcDp,
     coalesce(ds.schema, srcDp.name)            AS schema_resolved,
     coalesce(ds.name, ods.physicalName, ods.name) AS table_resolved,
     coalesce(col.name, sc_dp.name, '(literal)') AS source_col_resolved
WHERE ($table IS NULL OR table_resolved = $table)
  AND ($column IS NULL OR source_col_resolved = $column)
RETURN schema_resolved AS schema, table_resolved AS table_name,
       source_col_resolved AS source_column,
       pc.name AS product_column, cm.uri AS mapping_uri,
       cm.similarityScore AS score, cm.status AS status,
       cm.rationale AS rationale, cm.transformKind AS transform_kind,
       cm.transformAuthor AS transform_author,
       cm.transformExpression AS transform_expression,
       cm.transformParams AS transform_params,
       cm.transformInputs AS transform_inputs,
       cm.transformDecorators AS transform_decorators,
       // Lookup reference tables a lookup-kind mapping reads from (often a
       // different CONSUMES'd product). Pattern comprehension so we don't
       // multiply rows. See backend/lookup_via.py.
       [ (cm)-[:LOOKUP_VIA]->(lk) |
           coalesce(
             head([ (lkds:Dataset)-[:HAS_COLUMN]->(lk) | lkds.name ]),
             head([ (lkods:DProdOutputDataset)-[:HAS_PRODUCT_COLUMN]->(lk) | coalesce(lkods.physicalName, lkods.name) ])
           )
       ] AS lookup_tables
ORDER BY schema_resolved, table_resolved, source_col_resolved
"""
# Anchor on the project's own :DProdColumn so the result set includes both
# catalog-source and dprod-source mappings + literals.
MAPPINGS_DETAIL_S = """\
MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
WHERE cm.isCurrent = true
  AND pc.uri STARTS WITH 'dprod:col:' + $project_code + '-contract:'
OPTIONAL MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(col:Column)<-[:HAS_COLUMN]-(ds:Dataset)
OPTIONAL MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(sc_dp:DProdColumn)
               <-[:HAS_PRODUCT_COLUMN]-(ods:DProdOutputDataset)
               <-[:DPROD_OUTPUT_DATASET]-(:DProdOutputPort)
               <-[:DPROD_OUTPUT_PORT]-(srcDp:DProdDataProduct)
WITH cm, pc, col, ds, sc_dp, ods, srcDp,
     coalesce(ds.schema, srcDp.name)            AS schema_resolved,
     coalesce(ds.name, ods.physicalName, ods.name) AS table_resolved,
     coalesce(col.name, sc_dp.name, '(literal)') AS source_col_resolved
WHERE ($table IS NULL OR table_resolved = $table)
  AND ($column IS NULL OR source_col_resolved = $column)
RETURN schema_resolved AS schema, table_resolved AS table_name,
       source_col_resolved AS source_column,
       pc.name AS product_column, cm.uri AS mapping_uri,
       cm.similarityScore AS score, cm.status AS status,
       cm.rationale AS rationale, cm.transformKind AS transform_kind,
       cm.transformAuthor AS transform_author,
       cm.transformExpression AS transform_expression,
       cm.transformParams AS transform_params,
       cm.transformInputs AS transform_inputs,
       cm.transformDecorators AS transform_decorators,
       [ (cm)-[:LOOKUP_VIA]->(lk) |
           coalesce(
             head([ (lkds:Dataset)-[:HAS_COLUMN]->(lk) | lkds.name ]),
             head([ (lkods:DProdOutputDataset)-[:HAS_PRODUCT_COLUMN]->(lk) | coalesce(lkods.physicalName, lkods.name) ])
           )
       ] AS lookup_tables
ORDER BY schema_resolved, table_resolved, source_col_resolved
"""

DQ_RULES_DETAIL = """\
MATCH (ds:Dataset)-[:HAS_SHAPE]->(ns:NodeShape)-[:PROPERTY]->(ps:PropertyShape)-[:ON_COLUMN]->(col:Column)
WHERE ($table IS NULL OR ds.name = $table)
  AND ($column IS NULL OR col.name = $column)
  AND ($severity IS NULL OR ps.severity = $severity)
  AND ($source IS NULL OR COALESCE(ps.ruleSource, 'observation') = $source)
RETURN ds.name AS table_name, col.name AS column_name,
       ps.uri AS rule_uri,
       ps.ruleType AS rule_type, ps.severity AS severity, ps.description AS description,
       COALESCE(ps.ruleSource, 'observation') AS rule_source,
       COALESCE(ps.status, 'approved') AS rule_status
ORDER BY ds.name, col.name, ps.ruleType
"""
DQ_RULES_DETAIL_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_SHAPE]->(ns:NodeShape)-[:PROPERTY]->(ps:PropertyShape)-[:ON_COLUMN]->(col:Column)
WHERE ($table IS NULL OR ds.name = $table)
  AND ($column IS NULL OR col.name = $column)
  AND ($severity IS NULL OR ps.severity = $severity)
  AND ($source IS NULL OR COALESCE(ps.ruleSource, 'observation') = $source)
RETURN ds.name AS table_name, col.name AS column_name,
       ps.uri AS rule_uri,
       ps.ruleType AS rule_type, ps.severity AS severity, ps.description AS description,
       COALESCE(ps.ruleSource, 'observation') AS rule_source,
       COALESCE(ps.status, 'approved') AS rule_status
ORDER BY ds.name, col.name, ps.ruleType
"""

# Product (dprod) DQ rules — contract rules on :DProdColumn (spec/domain/user),
# keyed to the product's output datasets. For consumer-aligned products (no
# :Column catalog) this is the ONLY place rules live; get_dq_rules + the UI card
# branch to it by archetype. Same RETURN aliases as the catalog variants so the
# renderers are unchanged. Project-scoped via the contract-id prefix.
DQ_RULES_DETAIL_DPROD = """\
MATCH (dc:DataContract)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
WHERE dc.id = $project_code + '-contract'
MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)<-[:ON_DPROD_COLUMN]-(ps:PropertyShape)
WHERE ($table IS NULL OR coalesce(ods.physicalName, ods.name) = $table)
  AND ($column IS NULL OR pc.name = $column)
  AND ($severity IS NULL OR ps.severity = $severity)
  AND ($source IS NULL OR coalesce(ps.ruleSource, 'spec') = $source)
RETURN coalesce(ods.physicalName, ods.name) AS table_name, pc.name AS column_name,
       ps.uri AS rule_uri,
       ps.ruleType AS rule_type, ps.severity AS severity, ps.description AS description,
       coalesce(ps.ruleSource, 'spec') AS rule_source,
       coalesce(ps.status, 'approved') AS rule_status
ORDER BY table_name, column_name, ps.ruleType
"""

ALLOWED_VALUES_DETAIL = """\
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)-[:HAS_TOP_VALUE]->(tv:TopValue)
WHERE ($table IS NULL OR ds.name = $table)
  AND ($column IS NULL OR col.name = $column)
WITH ds, col, collect(tv.value) AS allowed_values, sum(tv.frequency) AS coverage
WHERE coverage > 0.95
RETURN ds.schema AS schema, ds.name AS table_name, col.name AS column_name,
       allowed_values, round(coverage * 100) / 100 AS coverage, size(allowed_values) AS value_count
ORDER BY size(allowed_values), ds.name, col.name
"""
ALLOWED_VALUES_DETAIL_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_COLUMN]->(col:Column)-[:HAS_TOP_VALUE]->(tv:TopValue)
WHERE ($table IS NULL OR ds.name = $table)
  AND ($column IS NULL OR col.name = $column)
WITH ds, col, collect(tv.value) AS allowed_values, sum(tv.frequency) AS coverage
WHERE coverage > 0.95
RETURN ds.schema AS schema, ds.name AS table_name, col.name AS column_name,
       allowed_values, round(coverage * 100) / 100 AS coverage, size(allowed_values) AS value_count
ORDER BY size(allowed_values), ds.name, col.name
"""

# ── Provenance (drill-down by specific URI — no project scoping needed) ────

PROVENANCE_QUERY = """\
MATCH (col:Column {uri: $col_uri})-[:HAS_DESCRIPTION]->(cd:ColumnDescription)
MATCH (cd)-[:PROV_WAS_GENERATED_BY]->(act:ProvActivity)-[:PROV_WAS_ASSOCIATED_WITH]->(agent:ProvAgent)
OPTIONAL MATCH (act)-[:HAS_REJECTION_REASON]->(reason:ProvRejectionReason)
RETURN
    act.activityType     AS activity_type,
    act.outcome          AS outcome,
    act.occurredAt       AS occurred_at,
    agent.name           AS agent,
    agent.agentType      AS agent_type,
    cd.text              AS description,
    cd.status            AS status,
    reason.category      AS rejection_category

UNION

MATCH (col:Column {uri: $col_uri})-[:HAS_DESCRIPTION]->(cd:ColumnDescription)
MATCH (act:ProvActivity)-[:PROV_USED]->(cd)
MATCH (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent:ProvAgent)
OPTIONAL MATCH (act)-[:HAS_REJECTION_REASON]->(reason:ProvRejectionReason)
RETURN
    act.activityType     AS activity_type,
    act.outcome          AS outcome,
    act.occurredAt       AS occurred_at,
    agent.name           AS agent,
    agent.agentType      AS agent_type,
    cd.text              AS description,
    cd.status            AS status,
    reason.category      AS rejection_category
"""

MAPPING_PROVENANCE_QUERY = """\
MATCH (cm:ColumnMapping {uri: $mapping_uri})
MATCH (cm)-[:PROV_WAS_GENERATED_BY]->(act:ProvActivity)-[:PROV_WAS_ASSOCIATED_WITH]->(agent:ProvAgent)
OPTIONAL MATCH (act)-[:HAS_REJECTION_REASON]->(reason:ProvRejectionReason)
RETURN
    act.activityType     AS activity_type,
    act.outcome          AS outcome,
    act.occurredAt       AS occurred_at,
    agent.name           AS agent,
    agent.agentType      AS agent_type,
    cm.rationale         AS description,
    cm.status            AS status,
    reason.category      AS rejection_category

UNION

MATCH (cm:ColumnMapping {uri: $mapping_uri})
MATCH (act:ProvActivity)-[:PROV_USED]->(cm)
MATCH (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent:ProvAgent)
OPTIONAL MATCH (act)-[:HAS_REJECTION_REASON]->(reason:ProvRejectionReason)
RETURN
    act.activityType     AS activity_type,
    act.outcome          AS outcome,
    act.occurredAt       AS occurred_at,
    agent.name           AS agent,
    agent.agentType      AS agent_type,
    cm.rationale         AS description,
    cm.status            AS status,
    reason.category      AS rejection_category
"""

# ── Filter options ─────────────────────────────────────────────────────────

FILTER_OPTIONS = """\
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)
RETURN collect(DISTINCT ds.name) AS tables, collect(DISTINCT col.name) AS columns
"""
FILTER_OPTIONS_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_COLUMN]->(col:Column)
RETURN collect(DISTINCT ds.name) AS tables, collect(DISTINCT col.name) AS columns
"""

DQ_SEVERITY_OPTIONS = """\
MATCH (ps:PropertyShape)
RETURN collect(DISTINCT ps.severity) AS severities
"""
DQ_SEVERITY_OPTIONS_S = f"""\
{_PRJ_DS}(:Dataset)-[:HAS_SHAPE]->(:NodeShape)-[:PROPERTY]->(ps:PropertyShape)
RETURN collect(DISTINCT ps.severity) AS severities
"""


# ── Helpers ─────────────────────────────────────────────────────────────────

def _get_project(project_id: int, session: Session) -> Project:
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _neo4j(project: Project):
    return neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    )


def _pick(scoped: bool, q_scoped: str, q_global: str) -> str:
    """Choose the scoped or unscoped query variant."""
    return q_scoped if scoped else q_global


def _run_query(project, query, **params):
    with _neo4j(project) as ns:
        result = ns.run(query, **params)
        return [dict(r) for r in result]


def _display_query(query: str, params: dict) -> str:
    """Build a human-readable Cypher string with parameters substituted."""
    display = query.strip()
    # Remove parameterized NULL-check filter lines when param is None
    import re
    for key, val in params.items():
        if val is None:
            # Remove lines like "AND ($table IS NULL OR ds.name = $table)"
            display = re.sub(
                rf"\s*AND\s*\(\${key}\s+IS\s+NULL\s+OR\s+[^)]+\)", "", display
            )
            # Remove lines like "WHERE ($table IS NULL OR ds.name = $table)"
            # but keep WHERE if other conditions follow
            display = re.sub(
                rf"WHERE\s*\(\${key}\s+IS\s+NULL\s+OR\s+[^)]+\)\s*\n",
                "WHERE ", display
            )
        else:
            # Substitute parameter with quoted value
            display = display.replace(f"${key}", f"'{val}'")
    # Remove project_code parameter from display (internal implementation detail)
    display = re.sub(r"\{projectCode:\s*'[^']*'\}", "{...}", display)
    # Clean up orphan WHERE with no conditions
    display = re.sub(r"WHERE\s*\n", "\n", display)
    display = re.sub(r"WHERE\s*$", "", display, flags=re.MULTILINE)
    # Clean up extra blank lines
    display = re.sub(r"\n{3,}", "\n\n", display)
    return display.strip()


# ── Endpoints ───────────────────────────────────────────────────────────────

@router.get("")
def get_summary(project_id: int, session: Session = Depends(get_session)):
    project = _get_project(project_id, session)
    scoped = has_project_node(project)
    pc = {"project_code": project.project_code} if scoped else {}

    stats = {
        # Tag the response with the project's archetype so the dashboard
        # can render an archetype-appropriate card set without a follow-up
        # round trip to /api/projects/{id}.
        "archetype": project.archetype,
        # PO serving preference (from the wizard's Serving Strategy) — surfaced
        # to the engineer as a recommendation banner. None when unset.
        "po_serving_preference": None,
        "po_serving_reason": None,
        "po_source_platform": None,
        "po_target_platform": None,
        "datasets": 0,
        "columns": 0,
        "graph_nodes": 0,
        "graph_relationships": 0,
        "descriptions_total": 0,
        "descriptions_approved": 0,
        "descriptions_rejected": 0,
        "descriptions_pending": 0,
        "final_descriptions": 0,
        # Relationship descriptions (PO-reviewed FK semantics). Only
        # populated for project-scoped (post-multi-project) projects;
        # legacy unscoped projects stay at 0.
        "relationships_total": 0,
        "relationships_approved": 0,
        "relationships_rejected": 0,
        "relationships_pending": 0,
        "mappings_total": 0,
        "mappings_approved": 0,
        "mappings_rejected": 0,
        "mappings_pending": 0,
        "dq_rules": 0,
        # Latest DQ test-run rollup (SQLite DQTestRun — reliable for any served
        # platform, unlike the Neo4j :TestResult load). All None/0 until a run
        # exists, which is what the dashboard keys the card's visibility on.
        "dq_tests_last_run_at": None,
        "dq_tests_framework": None,
        "dq_tests_status": None,
        "dq_tests_total": 0,
        "dq_tests_passed": 0,
        "dq_tests_failed": 0,
        "dq_tests_tables": 0,
        "dq_tests_run_count": 0,
        "dq_rules_observed": 0,
        "dq_rules_domain": 0,
        "allowed_values": 0,
        "serving_definitions": 0,
        "profiled_columns": 0,
        "profiling_metrics": 0,
        "playbook_items": 0,
        "learning_cycles": 0,
        "quality_composite": None,
        "scored_datasets": 0,
        "data_products": 0,
        "dprod_columns_total": 0,
        "dprod_columns_mapped": 0,
        "dprod_columns_unmapped": 0,
        # Consumer-aligned only — count of CONSUMES'd source products and
        # their datasets/columns. Stays 0 for non-consumer archetypes.
        "inputs_source_products": 0,
        "inputs_datasets": 0,
        "inputs_columns": 0,
    }

    try:
        with _neo4j(project) as ns:
            stats["datasets"] = ns.run(_pick(scoped, DATASET_COUNT_S, DATASET_COUNT), **pc).single()["cnt"]
            stats["columns"] = ns.run(_pick(scoped, COLUMN_COUNT_S, COLUMN_COUNT), **pc).single()["cnt"]

            if scoped:
                # Compute graph_nodes / graph_relationships as sums of scoped counts
                # (global MATCH (n) would count all projects' data)
                pass  # filled in below after all counts are collected
            else:
                stats["graph_nodes"] = ns.run(GRAPH_NODE_COUNT).single()["cnt"]
                stats["graph_relationships"] = ns.run(GRAPH_REL_COUNT).single()["cnt"]

            desc = ns.run(_pick(scoped, DESCRIPTION_STATS_S, DESCRIPTION_STATS), **pc).single()
            if desc:
                stats["descriptions_total"] = desc["total"]
                stats["descriptions_approved"] = desc["approved"]
                stats["descriptions_rejected"] = desc["rejected"]
                stats["descriptions_pending"] = desc["pending"]

            stats["final_descriptions"] = ns.run(_pick(scoped, FINAL_DESCRIPTIONS_COUNT_S, FINAL_DESCRIPTIONS_COUNT), **pc).single()["cnt"]

            # Relationship descriptions — project-scoped only. The
            # unscoped branch keeps the default zero (legacy projects
            # don't carry :HAS_RELATIONSHIP_DESCRIPTION edges).
            if scoped:
                rel = ns.run(RELATIONSHIP_STATS_S, **pc).single()
                if rel:
                    stats["relationships_total"] = rel["total"]
                    stats["relationships_approved"] = rel["approved"]
                    stats["relationships_rejected"] = rel["rejected"]
                    stats["relationships_pending"] = rel["pending"]

            mapping = ns.run(_pick(scoped, MAPPING_STATS_S, MAPPING_STATS), **pc).single()
            if mapping:
                stats["mappings_total"] = mapping["total"]
                stats["mappings_approved"] = mapping["approved"]
                stats["mappings_rejected"] = mapping["rejected"]
                stats["mappings_pending"] = mapping["pending"]

            prof = ns.run(_pick(scoped, PROFILING_STATS_S, PROFILING_STATS), **pc).single()
            if prof:
                stats["profiled_columns"] = prof["profiled_columns"]
                stats["profiling_metrics"] = prof["total_metrics"]

            stats["serving_definitions"] = ns.run(_pick(scoped, SERVING_COUNT_S, SERVING_COUNT), **pc).single()["cnt"]
            if scoped:
                cp_row = ns.run(PO_SERVING_QUERY, **pc).single()
                if cp_row and cp_row["cp"]:
                    try:
                        cp = json.loads(cp_row["cp"])
                        stats["po_serving_preference"] = cp.get("po_serving_preference")
                        stats["po_serving_reason"] = cp.get("po_serving_reason")
                        stats["po_source_platform"] = cp.get("po_source_platform")
                        stats["po_target_platform"] = cp.get("po_target_platform")
                    except Exception:
                        pass
            dq_row = ns.run(_pick(scoped, DQ_RULE_COUNT_S, DQ_RULE_COUNT), **pc).single()
            stats["dq_rules"] = dq_row["cnt"]
            stats["dq_rules_observed"] = dq_row["observed_cnt"]
            stats["dq_rules_domain"] = dq_row["domain_cnt"]
            stats["dq_rules_user"] = dq_row["user_cnt"]
            stats["dq_rules_spec"] = dq_row["spec_cnt"]
            stats["dq_rules_pending"] = dq_row["pending_cnt"]
            stats["dq_rules_approved"] = dq_row["approved_cnt"]
            stats["dq_rules_rejected"] = dq_row["rejected_cnt"]
            stats["allowed_values"] = ns.run(_pick(scoped, ALLOWED_VALUES_COUNT_S, ALLOWED_VALUES_COUNT), **pc).single()["cnt"]

            try:
                scoring = ns.run(_pick(scoped, SCORING_STATS_S, SCORING_STATS), **pc).single()
                if scoring and scoring["scored_datasets"]:
                    stats["scored_datasets"] = scoring["scored_datasets"]
                    stats["quality_composite"] = round(scoring["avg_composite"], 4) if scoring["avg_composite"] is not None else None
            except Exception:
                pass  # scoring nodes may not exist yet

            if project.domain:
                stats["playbook_items"] = ns.run(PLAYBOOK_COUNT, domain=project.domain).single()["cnt"]
                stats["learning_cycles"] = ns.run(LEARNING_HISTORY_COUNT, domain=project.domain).single()["cnt"]

            # Data product counts (only meaningful when a :Project node exists)
            if scoped:
                try:
                    stats["data_products"] = ns.run(DPROD_PRODUCTS_COUNT_S, **pc).single()["cnt"]
                    cov = ns.run(DPROD_COLUMNS_COVERAGE_S, **pc).single()
                    if cov:
                        total = int(cov["total"] or 0)
                        mapped = int(cov["mapped"] or 0)
                        stats["dprod_columns_total"] = total
                        stats["dprod_columns_mapped"] = mapped
                        stats["dprod_columns_unmapped"] = max(total - mapped, 0)
                except Exception:
                    pass

                # Consumer-aligned only: count :CONSUMES'd source products.
                if project.archetype == "dpe-cf":
                    try:
                        inp = ns.run(INPUTS_STATS_S, **pc).single()
                        if inp:
                            stats["inputs_source_products"] = int(inp["source_products"] or 0)
                            stats["inputs_datasets"] = int(inp["source_datasets"] or 0)
                            stats["inputs_columns"] = int(inp["source_columns"] or 0)
                    except Exception:
                        pass

        # Compute approximate graph totals from scoped sub-counts.
        # Includes the contract + DPROD subgraph so DPE-CF projects that
        # have only made it through ODCS→DPROD (no :Dataset / :Column yet)
        # still report a non-zero count.
        if scoped:
            try:
                with _neo4j(project) as ns:
                    contract_row = ns.run(CONTRACT_SUBGRAPH_COUNT_S, **pc).single()
                    contract_subgraph = contract_row["cnt"] if contract_row else 0
            except Exception:
                contract_subgraph = 0
            stats["graph_nodes"] = (
                stats["datasets"] + stats["columns"]
                + stats["descriptions_total"] + stats["mappings_total"]
                + stats["dq_rules"] + stats["profiling_metrics"]
                + contract_subgraph
            )
            stats["graph_relationships"] = stats["graph_nodes"]  # rough approximation
    except Exception:
        pass

    # Latest DQ test-run rollup — SQLite, independent of Neo4j so it stays
    # accurate for warehouse-served products (Databricks/Snowflake/…) whose
    # :TestResult graph load may be a graceful skip. Drives the DQ results card.
    try:
        runs = session.exec(
            select(DQTestRun)
            .where(DQTestRun.project_id == project_id)
            .order_by(DQTestRun.started_at.desc())
        ).all()
        if runs:
            latest = runs[0]
            stats["dq_tests_run_count"] = len(runs)
            stats["dq_tests_framework"] = latest.framework
            stats["dq_tests_status"] = latest.status
            stats["dq_tests_total"] = latest.total_expectations
            stats["dq_tests_passed"] = latest.successful
            stats["dq_tests_failed"] = latest.unsuccessful
            stats["dq_tests_tables"] = latest.tables_tested
            ran_at = latest.completed_at or latest.started_at
            stats["dq_tests_last_run_at"] = ran_at.isoformat() if ran_at else None
    except Exception:
        pass

    return stats


@router.get("/filters")
def get_filters(project_id: int, session: Session = Depends(get_session)):
    """Return available table/column/severity filter options."""
    project = _get_project(project_id, session)
    scoped = has_project_node(project)
    pc = {"project_code": project.project_code} if scoped else {}
    result = {"tables": [], "columns": [], "severities": []}
    try:
        with _neo4j(project) as ns:
            r = ns.run(_pick(scoped, FILTER_OPTIONS_S, FILTER_OPTIONS), **pc).single()
            if r:
                result["tables"] = sorted(r["tables"])
                result["columns"] = sorted(r["columns"])
            sev = ns.run(_pick(scoped, DQ_SEVERITY_OPTIONS_S, DQ_SEVERITY_OPTIONS), **pc).single()
            if sev:
                result["severities"] = sorted(sev["severities"])
    except Exception:
        pass
    return result


# Map card names to (unscoped, scoped) query pairs
_DETAIL_QUERIES: dict[str, tuple[str, str]] = {
    "datasets": (DATASETS_DETAIL, DATASETS_DETAIL_S),
    "columns": (COLUMNS_DETAIL, COLUMNS_DETAIL_S),
    "descriptions": (DESCRIPTIONS_DETAIL, DESCRIPTIONS_DETAIL_S),
    "final_descriptions": (FINAL_DESCRIPTIONS_DETAIL, FINAL_DESCRIPTIONS_DETAIL_S),
    # Relationship descriptions are project-scoped only (legacy unscoped
    # projects have no :RelationshipDescription rows). Unscoped slot
    # reuses the scoped query — returns empty for legacy projects, which
    # is correct.
    "relationships": (RELATIONSHIPS_DETAIL_S, RELATIONSHIPS_DETAIL_S),
    "mappings": (MAPPINGS_DETAIL, MAPPINGS_DETAIL_S),
    "serving": (SERVING_QUERY, SERVING_QUERY_S),
    "profiling": (PROFILING_DETAIL, PROFILING_DETAIL_S),
    "dq_rules": (DQ_RULES_DETAIL, DQ_RULES_DETAIL_S),
    "allowed_values": (ALLOWED_VALUES_DETAIL, ALLOWED_VALUES_DETAIL_S),
    "playbook": (PLAYBOOK_DETAIL, PLAYBOOK_DETAIL),  # already domain-scoped
    "learning_history": (LEARNING_HISTORY_QUERY, LEARNING_HISTORY_QUERY),  # already domain-scoped
    # Project-only — wizard-driven products always have a :Project node, so
    # the unscoped slot reuses the scoped query (will return empty for
    # legacy unscoped projects, which is correct).
    "data_products": (DPROD_COLUMNS_DETAIL_S, DPROD_COLUMNS_DETAIL_S),
    # Consumer-aligned only. Returns one row per CONSUMES'd source product.
    # Non-consumer projects calling this card get an empty result by
    # construction (the MATCH on :CONSUMES yields nothing).
    "inputs": (INPUTS_DETAIL_S, INPUTS_DETAIL_S),
    # The synthesized/authored DataContract header — the materialization output
    # for source-aligned (and the authored spec for consumer-aligned) products.
    "contract": (CONTRACT_DETAIL_S, CONTRACT_DETAIL_S),
}


# The set of valid cards, exposed for callers (e.g. the MCP get_stage_results
# tool) that want to validate / enumerate without reaching into the private map.
DETAIL_CARDS: list[str] = list(_DETAIL_QUERIES.keys())


def detail_for_project(
    project: Project,
    card: str,
    *,
    table: Optional[str] = None,
    column: Optional[str] = None,
    severity: Optional[str] = None,
    source: Optional[str] = None,
) -> dict:
    """Run the vetted detail query for a card against an already-resolved project.

    Single source of truth shared by the GET /detail route and the MCP
    `get_stage_results` tool, so both surfaces use the SAME correct,
    single-dataset-scopable Cypher (the direct :HAS_COLUMN edge — no hand-authored
    traversal, no variable-length cross-join). Returns
    `{card, rows, count, cypher}`. Raises ValueError for an unknown card; lets
    Neo4j errors propagate to the caller.
    """
    scoped = has_project_node(project)

    pair = _DETAIL_QUERIES.get(card)
    if not pair:
        raise ValueError(f"Unknown card: {card}. Must be one of {DETAIL_CARDS}")

    query = pair[1] if scoped else pair[0]

    # Consumer-aligned products carry NO :Column catalog — their DQ rules live on
    # :DProdColumn (spec/domain/user on the contract). Point the dq_rules card at
    # the dprod variant so the card isn't empty (matches get_dq_rules + the UI).
    if card == "dq_rules" and (getattr(project, "archetype", "") or "") == "dpe-cf":
        query = DQ_RULES_DETAIL_DPROD

    params: dict = {"table": table or None, "column": column or None, "severity": severity or None, "source": source or None}

    # Add project_code for scoped queries
    if scoped and card not in ("playbook", "learning_history"):
        params["project_code"] = project.project_code

    # Serving query has no table/column filters
    if card == "serving":
        params = {"project_code": project.project_code} if scoped else {}

    # Playbook and learning queries need domain instead of table/column filters
    if card in ("playbook", "learning_history"):
        params = {"domain": project.domain or ""}

    # Inputs takes only project_code — no table/column filters.
    if card == "inputs":
        params = {"project_code": project.project_code} if scoped else {}

    # Contract is matched via :Project-[:HAS_CONTRACT]; always pass the code
    # (the query returns empty for a legacy project with no :Project node).
    if card == "contract":
        params = {"project_code": project.project_code}

    rows = _run_query(project, query, **params)

    # Serving card: one row per view (viewNames JSON array → multiple rows).
    if card == "serving":
        import json as _json
        expanded: list[dict] = []
        for r in rows:
            raw = r.get("view_names_json") or "[]"
            try:
                names = _json.loads(raw) if isinstance(raw, str) else (raw or [])
            except Exception:
                names = []
            # Strip the schema qualifier; keep only the bare view name.
            bare_names = [n.rsplit(".", 1)[-1] for n in names if n] if names else []
            if bare_names:
                for vn in bare_names:
                    expanded.append({**r, "view_name": vn})
            else:
                # Fallback: single viewName property (old data or dbt mode)
                expanded.append(r)
        rows = expanded

    return {
        "card": card,
        "rows": rows,
        "count": len(rows),
        "cypher": _display_query(query, params),
    }


@router.get("/detail")
def get_detail(
    project_id: int,
    card: str = QParam(..., description="Card name"),
    table: Optional[str] = QParam(None, description="Filter by table name"),
    column: Optional[str] = QParam(None, description="Filter by column name"),
    severity: Optional[str] = QParam(None, description="Filter by severity (dq_rules only)"),
    source: Optional[str] = QParam(None, description="Filter by rule source: observation or domain (dq_rules only)"),
    session: Session = Depends(get_session),
):
    project = _get_project(project_id, session)
    try:
        return detail_for_project(
            project, card, table=table, column=column, severity=severity, source=source
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Neo4j query failed: {e}")


@router.get("/provenance")
def get_provenance(
    project_id: int,
    col_uri: Optional[str] = QParam(None, description="Column URI (for descriptions)"),
    mapping_uri: Optional[str] = QParam(None, description="Mapping URI (for mappings)"),
    session: Session = Depends(get_session),
):
    project = _get_project(project_id, session)
    if not col_uri and not mapping_uri:
        raise HTTPException(400, "Provide col_uri or mapping_uri")

    try:
        if col_uri:
            rows = _run_query(project, PROVENANCE_QUERY, col_uri=col_uri)
            cypher = _display_query(PROVENANCE_QUERY, {"col_uri": col_uri})
            return {"col_uri": col_uri, "rows": rows, "count": len(rows), "cypher": cypher}
        else:
            rows = _run_query(project, MAPPING_PROVENANCE_QUERY, mapping_uri=mapping_uri)
            cypher = _display_query(MAPPING_PROVENANCE_QUERY, {"mapping_uri": mapping_uri})
            return {"mapping_uri": mapping_uri, "rows": rows, "count": len(rows), "cypher": cypher}
    except Exception as e:
        raise HTTPException(500, f"Neo4j query failed: {e}")


# ── Graph view queries (ERD + column neighborhood) ──────────────────────────

DATASET_GRAPH_TABLES_S = f"""\
{_PRJ_DS}(ds:Dataset)
OPTIONAL MATCH (ds)-[:HAS_COLUMN]->(col:Column)
WITH ds, col
ORDER BY ds.schema, ds.name, col.ordinal
RETURN ds.uri AS uri, ds.schema AS schema, ds.name AS table,
       collect(CASE WHEN col IS NULL THEN NULL ELSE
         {{uri: col.uri, name: col.name, type: col.dataType,
           primary_key: col.primaryKey, ordinal: col.ordinal}}
       END) AS columns
ORDER BY ds.schema, ds.name
"""
DATASET_GRAPH_TABLES = """\
MATCH (ds:Dataset)
OPTIONAL MATCH (ds)-[:HAS_COLUMN]->(col:Column)
WITH ds, col
ORDER BY ds.schema, ds.name, col.ordinal
RETURN ds.uri AS uri, ds.schema AS schema, ds.name AS table,
       collect(CASE WHEN col IS NULL THEN NULL ELSE
         {uri: col.uri, name: col.name, type: col.dataType,
          primary_key: col.primaryKey, ordinal: col.ordinal}
       END) AS columns
ORDER BY ds.schema, ds.name
"""

# Both source and target datasets must be in the project's catalog set so
# we don't surface FKs that point at unrelated graph data.
DATASET_GRAPH_REFERENCES_S = """\
MATCH (p:Project {projectCode: $project_code})-[:HAS_CATALOG]->(:Catalog)-[:DCAT_DATASET]->(ds1:Dataset)
MATCH (p)-[:HAS_CATALOG]->(:Catalog)-[:DCAT_DATASET]->(ds2:Dataset)
MATCH (ds1)-[r:REFERENCES]->(ds2)
RETURN ds1.uri AS src_dataset_uri, ds2.uri AS dst_dataset_uri,
       r.constraintName AS fk_name,
       r.columns AS columns_csv, r.referencedColumns AS ref_columns_csv,
       r.onDelete AS on_delete, r.onUpdate AS on_update
"""
DATASET_GRAPH_REFERENCES = """\
MATCH (ds1:Dataset)-[r:REFERENCES]->(ds2:Dataset)
RETURN ds1.uri AS src_dataset_uri, ds2.uri AS dst_dataset_uri,
       r.constraintName AS fk_name,
       r.columns AS columns_csv, r.referencedColumns AS ref_columns_csv,
       r.onDelete AS on_delete, r.onUpdate AS on_update
"""


@router.get("/dataset_graph")
def get_dataset_graph(project_id: int, session: Session = Depends(get_session)):
    """ERD payload: discovered datasets + columns + FK relationships."""
    project = _get_project(project_id, session)
    scoped = has_project_node(project)
    pc = {"project_code": project.project_code} if scoped else {}

    try:
        with _neo4j(project) as ns:
            tables_q = DATASET_GRAPH_TABLES_S if scoped else DATASET_GRAPH_TABLES
            tables_rows = [dict(r) for r in ns.run(tables_q, **pc)]
            refs_q = DATASET_GRAPH_REFERENCES_S if scoped else DATASET_GRAPH_REFERENCES
            refs_rows = [dict(r) for r in ns.run(refs_q, **pc)]
    except Exception as e:
        raise HTTPException(500, f"Neo4j query failed: {e}")

    datasets = []
    for row in tables_rows:
        cols = [c for c in (row.get("columns") or []) if c is not None]
        # Dataset URI -> {schema, table} for resolving column URIs from FK CSVs
        datasets.append({
            "uri": row["uri"],
            "schema": row["schema"],
            "table": row["table"],
            "columns": cols,
        })

    # Build a lookup so we can resolve "col_name" → full column URI per dataset.
    cols_by_dataset_uri: dict[str, dict[str, str]] = {}
    for ds in datasets:
        cols_by_dataset_uri[ds["uri"]] = {c["name"]: c["uri"] for c in ds["columns"]}

    references = []
    for r in refs_rows:
        src_cols = [c.strip() for c in (r.get("columns_csv") or "").split(",") if c.strip()]
        dst_cols = [c.strip() for c in (r.get("ref_columns_csv") or "").split(",") if c.strip()]
        src_lookup = cols_by_dataset_uri.get(r["src_dataset_uri"], {})
        dst_lookup = cols_by_dataset_uri.get(r["dst_dataset_uri"], {})
        column_pairs = []
        for s, d in zip(src_cols, dst_cols):
            column_pairs.append({
                "src_col_name": s,
                "dst_col_name": d,
                "src_col_uri": src_lookup.get(s),
                "dst_col_uri": dst_lookup.get(d),
            })
        references.append({
            "src_dataset_uri": r["src_dataset_uri"],
            "dst_dataset_uri": r["dst_dataset_uri"],
            "fk_name": r.get("fk_name"),
            "on_delete": r.get("on_delete"),
            "on_update": r.get("on_update"),
            "columns": column_pairs,
        })

    return {"datasets": datasets, "references": references}


# Column neighborhood: focus column + lineage outward (sources, products,
# rules, test results, descriptions). Source-column focus only for v1 — the
# Columns card on ProjectDashboard lists source columns, which is the natural
# entry point. Adding DProdColumn focus is a small follow-up.
COLUMN_NEIGHBORHOOD_FOCUS = """\
MATCH (col:Column {uri: $col_uri})
OPTIONAL MATCH (ds:Dataset)-[:HAS_COLUMN]->(col)
RETURN col.uri AS uri, col.name AS name, col.dataType AS data_type,
       col.primaryKey AS primary_key, col.nullable AS nullable,
       ds.schema AS schema, ds.name AS table, ds.uri AS dataset_uri
"""

COLUMN_NEIGHBORHOOD_MAPPINGS = """\
MATCH (col:Column {uri: $col_uri})<-[:MAPS_SOURCE_COLUMN]-(cm:ColumnMapping {isCurrent: true})
OPTIONAL MATCH (cm)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
OPTIONAL MATCH (pc)<-[:HAS_COLUMN]-(pds:DProdOutputDataset)
RETURN cm.uri AS mapping_uri, cm.status AS status,
       cm.transformKind AS transform_kind, cm.transformAuthor AS transform_author,
       cm.transformExpression AS transform_expression,
       cm.similarityScore AS similarity_score,
       pc.uri AS pc_uri, pc.name AS pc_name,
       pds.name AS pds_name, pds.uri AS pds_uri
"""

COLUMN_NEIGHBORHOOD_RULES = """\
MATCH (col:Column {uri: $col_uri})<-[:ON_COLUMN]-(ps:PropertyShape)
RETURN ps.uri AS uri, ps.ruleType AS rule_type, ps.severity AS severity,
       ps.ruleSource AS rule_source, ps.status AS status,
       ps.path AS path
"""

COLUMN_NEIGHBORHOOD_TEST_RESULTS = """\
MATCH (col:Column {uri: $col_uri})<-[:ON_COLUMN]-(res:TestResult)<-[:PRODUCED]-(tr:TestRun)
RETURN res.uri AS uri, res.expectationType AS expectation_type,
       res.success AS success, res.failedCount AS failed_count,
       res.observedValue AS observed_value,
       tr.uri AS run_uri, tr.framework AS framework, tr.startedAt AS started_at
ORDER BY tr.startedAt DESC
LIMIT 25
"""

COLUMN_NEIGHBORHOOD_DESCRIPTION = """\
MATCH (col:Column {uri: $col_uri})-[:HAS_DESCRIPTION]->(cd:ColumnDescription {isCurrent: true})
RETURN cd.uri AS uri, cd.text AS text, cd.status AS status
"""


@router.get("/column_neighborhood")
def get_column_neighborhood(
    project_id: int,
    col_uri: str = QParam(..., description="Source column URI"),
    session: Session = Depends(get_session),
):
    """Lineage payload for a single source column: mappings, rules, tests."""
    project = _get_project(project_id, session)

    try:
        with _neo4j(project) as ns:
            focus_row = ns.run(COLUMN_NEIGHBORHOOD_FOCUS, col_uri=col_uri).single()
            if not focus_row:
                raise HTTPException(404, f"Column not found: {col_uri}")
            focus = dict(focus_row)

            mapping_rows = [dict(r) for r in ns.run(COLUMN_NEIGHBORHOOD_MAPPINGS, col_uri=col_uri)]
            rule_rows = [dict(r) for r in ns.run(COLUMN_NEIGHBORHOOD_RULES, col_uri=col_uri)]
            tr_rows = [dict(r) for r in ns.run(COLUMN_NEIGHBORHOOD_TEST_RESULTS, col_uri=col_uri)]
            desc_row = ns.run(COLUMN_NEIGHBORHOOD_DESCRIPTION, col_uri=col_uri).single()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Neo4j query failed: {e}")

    mappings = []
    products = []
    seen_pc: set[str] = set()
    for r in mapping_rows:
        mappings.append({
            "uri": r["mapping_uri"],
            "status": r["status"],
            "transform_kind": r.get("transform_kind"),
            "transform_author": r.get("transform_author"),
            "transform_expression": r.get("transform_expression"),
            "similarity_score": r.get("similarity_score"),
            "src_col_uri": col_uri,
            "dst_col_uri": r.get("pc_uri"),
        })
        pc_uri = r.get("pc_uri")
        if pc_uri and pc_uri not in seen_pc:
            seen_pc.add(pc_uri)
            products.append({
                "uri": pc_uri,
                "name": r.get("pc_name"),
                "dataset_uri": r.get("pds_uri"),
                "dataset_name": r.get("pds_name"),
            })

    test_results = [r for r in tr_rows if r.get("uri")]

    return {
        "focus": {
            "uri": focus["uri"],
            "name": focus["name"],
            "data_type": focus.get("data_type"),
            "primary_key": focus.get("primary_key"),
            "nullable": focus.get("nullable"),
            "schema": focus.get("schema"),
            "table": focus.get("table"),
            "dataset_uri": focus.get("dataset_uri"),
            "role": "source",
        },
        "description": dict(desc_row) if desc_row else None,
        "products": products,
        "mappings": mappings,
        "rules": rule_rows,
        "test_results": test_results,
    }
