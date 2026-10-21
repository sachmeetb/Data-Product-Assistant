"""dpe-sa pipeline helpers: ODCS synthesis from approved graph state and
1:1 auto-mapping of source columns to the materialized DProdColumns.

These are the load-bearing backend handlers for the
``synthesize_odcs_from_graph`` and ``auto_mapping_sa`` stages, both
non-LLM and triggered via ``POST /stages/{n}/complete``.

Design notes:
  - Synthesis only ingests columns whose recommendedNameStatus is
    ``approved``, ``rejected``, or unset. ``rejected`` falls back to the
    original column name; ``approved`` uses the recommendedName. Pending
    items are a bug at this point — the po_source_validation gate
    should not have closed.
  - Descriptions and observation rules must be ``approved`` to flow
    through. Rejected items are dropped silently.
  - Auto-mapping happens after odcs_to_dprod has materialized
    :DProdColumn nodes; we match by (dataset physicalName, column
    physicalName) since the URI shape is project-scoped.
"""

import json
from datetime import datetime

from ..models import Project
from ..neo4j_client import neo4j_session
from .odcs import _save_odcs_to_graph


# ─────────────────────────────────────────────────────────────────────
# Synthesis: graph → ODCS dict → _save_odcs_to_graph
# ─────────────────────────────────────────────────────────────────────

# Pulls every column the discovery + naming pass produced, including its
# resolved description (the most recent approved one) and the dataset
# context. recommendedNameStatus='rejected' falls back to the original
# col.name; otherwise the recommendedName wins. Rules are loaded
# separately (one query per column tree to keep the result rectangular).
_READ_PROJECT_COLUMNS = """\
MATCH (:Project {projectCode: $project_code})
      -[:HAS_CATALOG]->(cat:Catalog)
      -[:DCAT_DATASET]->(ds:Dataset)
      -[:HAS_COLUMN]->(col:Column)
OPTIONAL MATCH (col)-[:HAS_DESCRIPTION]->(cd:ColumnDescription {status: 'approved', isCurrent: true})
WITH ds, col, head(collect(cd)) AS cd
OPTIONAL MATCH (col)<-[:ON_COLUMN]-(ps:PropertyShape)
WHERE coalesce(ps.ruleSource, 'observation') = 'observation' AND ps.status = 'approved'
WITH ds, col, cd, collect({
    rule_uri: ps.uri,
    rule_type: coalesce(ps.ruleType, ''),
    severity: coalesce(ps.severity, 'sh:Warning'),
    description: coalesce(ps.description, '')
}) AS rules
RETURN
    ds.schema     AS schema,
    ds.name       AS table_name,
    coalesce(ds.row_count, -1) AS row_count,
    col.uri       AS col_uri,
    col.name      AS source_name,
    col.dataType  AS data_type,
    col.ordinal   AS ordinal,
    coalesce(col.primaryKey, false) AS primary_key,
    coalesce(col.nullable, true)    AS nullable,
    col.recommendedName       AS recommended_name,
    coalesce(col.recommendedNameStatus, '') AS name_status,
    coalesce(cd.text, '')     AS description,
    coalesce(col.sensitivity, 'none') AS sensitivity,
    [r IN rules WHERE r.rule_uri IS NOT NULL] AS rules
ORDER BY ds.schema, ds.name, col.ordinal
"""


def _resolve_physical_name(row: dict, warnings: list | None = None) -> str:
    """Pick the column's physical name in the synthesized ODCS spec.

    approved → recommendedName. rejected/unset → original col.name. The
    PO can have explicitly chosen the original by editing or rejecting
    the recommendation.

    When a non-empty recommendation exists but hasn't been approved (and
    wasn't explicitly rejected — that's a deliberate PO choice), the raw
    source name is used *silently*. If ``warnings`` is provided, record
    that fallback so the caller can surface "these columns kept raw names
    because their recommendation is still pending".
    """
    rec = (row.get("recommended_name") or "").strip()
    status = (row.get("name_status") or "").strip()
    if status == "approved" and rec:
        return rec
    raw = (row.get("source_name") or "").strip()
    if warnings is not None and rec and status not in ("approved", "rejected"):
        warnings.append({
            "col_uri": row.get("col_uri") or "",
            "source_name": raw,
            "recommended_name": rec,
            "name_status": status or "pending",
        })
    return raw


def _severity_label(raw: str) -> str:
    """ODCS expects 'error' / 'warning'; PropertyShape stores SHACL labels."""
    raw = (raw or "").lower()
    if raw in ("sh:violation", "violation", "error"):
        return "error"
    return "warning"


def _build_spec(project: Project, rows: list[dict], warnings: list | None = None) -> dict:
    """Group flat column rows into ODCS schema[].properties[] + quality[]."""
    contract_id = f"{project.project_code}-contract"
    by_table: dict[str, dict] = {}
    quality: list[dict] = []

    for row in rows:
        phys = _resolve_physical_name(row, warnings)
        if not phys:
            continue
        table = row.get("table_name") or ""
        schema = row.get("schema") or ""
        if not table:
            continue
        # Schema-qualified physical name avoids cross-schema collisions.
        table_key = f"{schema}.{table}" if schema else table
        # Synthesize a simple table description until the metadata-enrichment
        # skill writes a richer one. Includes the schema-qualified source
        # ("hr.employee") and the profiled row count when available so the
        # marketplace's Schema tab has something useful to render. Future
        # work: extend metadata-enrichment to LLM-generate per-table prose.
        row_count = row.get("row_count")
        ds_desc_bits = []
        if schema:
            ds_desc_bits.append(f"Mirrors source table {schema}.{table}")
        else:
            ds_desc_bits.append(f"Mirrors source table {table}")
        if isinstance(row_count, int) and row_count >= 0:
            ds_desc_bits.append(f"~{row_count:,} rows at profile time")
        ds_desc = ". ".join(ds_desc_bits) + "."
        bucket = by_table.setdefault(table_key, {
            "name": table,
            "physicalName": table,
            "physicalType": "table",
            "description": ds_desc,
            "properties": [],
        })
        # required = !nullable AND not primary key. PKs are implicitly required
        # in ODCS but the canonicaliser uses the explicit `required` field.
        is_pk = bool(row.get("primary_key"))
        nullable = bool(row.get("nullable", True))
        prop = {
            "name": phys,
            "physicalName": phys,
            "logicalType": (row.get("data_type") or "").lower(),
            "physicalType": row.get("data_type") or "",
            "description": row.get("description") or "",
            "primaryKey": is_pk,
            "required": is_pk or (not nullable),
            # Stable provenance link back to the exact source :Column. Persisted
            # on :DataContractProperty → :DProdColumn so auto-mapping pairs by
            # identity (not a name that can drift between rename/approval runs),
            # and so a column can never be silently dropped from the product.
            "sourceColumnUri": row.get("col_uri") or "",
        }
        sens = (row.get("sensitivity") or "none").lower()
        if sens != "none":
            prop["sensitivity"] = sens
            # Keep the legacy pii bool consistent so DPROD_CREATE_COLUMN's
            # fallback (CASE WHEN p.pii THEN 'pii') matches the explicit
            # sensitivity even if someone reads only one of the two fields.
            if sens in ("pii", "phi"):
                prop["pii"] = True
        bucket["properties"].append(prop)

        for r in row.get("rules") or []:
            quality.append({
                "name": r.get("rule_type") or "observation",
                "rule": r.get("rule_type") or "observation",
                "description": r.get("description") or "",
                "severity": _severity_label(r.get("severity") or ""),
                "dimension": "validity",
                "column": phys,
                "dataset": table,
            })

    # Populate owners[] from the project's owner_email so the synthesized
    # contract gets a :DataContractOwner the marketplace's owned_by filter
    # can match. Without this, the SA product wouldn't surface in the PO's
    # My Products list (the consumer wizard authors owners directly in the
    # spec; SA synthesis didn't, so the contract had zero owners).
    owners_list: list[dict] = []
    po_email = (getattr(project, "owner_email", None) or "").strip()
    po_name = (getattr(project, "owner_name", None) or "").strip()
    if po_email:
        owners_list.append({
            "username": po_email,
            "name": po_name or (po_email.split("@")[0] if "@" in po_email else po_email),
            "role": "Data Product Owner",
            "email": po_email,
        })

    spec = {
        "apiVersion": "v3.1.0",
        "kind": "DataContract",
        "id": contract_id,
        "name": project.name or contract_id,
        "version": "1.0.0",
        "status": "draft",
        "domain": (project.domain or "").lower(),
        "dataProduct": project.name or "",
        "description": (
            (project.product_idea or "").strip()
            or f"Source-aligned mirror of the {(project.domain or 'general').lower()} domain."
        ),
        "purpose": "Source-aligned mirror — consumers can build downstream products on top.",
        "owners": owners_list,
        "schema": list(by_table.values()),
        "quality": quality,
        "customProperties": [
            {"property": "productKind", "value": "source"},
        ],
    }
    return spec


def synthesize_odcs_from_graph(
    project: Project,
    submitted_by: str = "dpe-sa-synthesizer",
    change_kind: str = "auto",
    revision_notes: str = "",
    collect_warnings: list | None = None,
) -> str:
    """Build an ODCS dict from approved :Column / :ColumnDescription /
    :PropertyShape nodes in the project graph and persist it via
    _save_odcs_to_graph.

    Phase 2 hooks:
      - ``submitted_by``: defaults to the synthesizer agent, but Phase 2
        source-edit endpoints pass the PO's email so :ProvActivity nodes
        attribute the change correctly.
      - ``change_kind``: 'auto' lets _save_odcs_to_graph's classifier
        decide between cosmetic patch / schema branch / breaking branch.
        Explicit values let the caller force a mode.
      - ``revision_notes``: rides on the :ContractVersion sidecar (branch)
        or :ProvActivity ContractPatch (cosmetic). Surfaces to consumers
        via the marketplace revision timeline and upstream-drift banner.

    Returns the contract_id. Raises if the graph is empty or the save fails.
    """
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        rows = [dict(r) for r in ns.run(_READ_PROJECT_COLUMNS, project_code=project.project_code)]
        # Materialize rule lists fully — Neo4j list values come back as
        # neo4j.graph.Node etc otherwise.
        for r in rows:
            r["rules"] = [dict(x) for x in (r.get("rules") or [])]

    if not rows:
        raise RuntimeError(
            f"No :Column nodes for project {project.project_code} — run discovery first."
        )

    spec = _build_spec(project, rows, collect_warnings)
    return _save_odcs_to_graph(
        spec, project,
        submitted_by=submitted_by,
        change_kind=change_kind,
        revision_notes=revision_notes,
    ).contract_id


# ─────────────────────────────────────────────────────────────────────
# Auto-mapping: 1:1 :ColumnMapping per discovered column
# ─────────────────────────────────────────────────────────────────────

# Find every :Column and its corresponding :DProdColumn in the same
# project. The synthesis step uses the dataset physicalName = ds.name and
# the property physicalName = approved-recommendedName-or-source-name, so
# we re-derive the same key here. :DProdColumn URIs are
# 'dprod:col:{contract_id}:{schema_physical_name}:{property_physical_name}'.
# WHERE clause excludes columns the synth step skipped (e.g. blank
# physical name), and skips pairs that already have a current mapping.
# Walk the product's materialized columns and resolve each back to its source
# :Column via the provenance link (pc.sourceColumnUri) stamped at synthesis.
# Pairing by this stable identity — NOT by reconstructing a URI from the
# (rename/approval-drift-prone) product name — is what guarantees no column is
# silently dropped. `matched_col` is null when the link is missing or points at
# a non-existent :Column, which the caller turns into a loud failure.
_DPROD_COLUMNS_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})-[:MATERIALISES_AS]->(:DProdDataProduct)
      -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
OPTIONAL MATCH (c:Column {uri: pc.sourceColumnUri})
RETURN pc.uri                          AS product_col_uri,
       pc.name                         AS product_phys,
       ods.physicalName                AS table_name,
       coalesce(pc.sourceColumnUri, '') AS source_col_uri,
       c.uri                           AS matched_col
ORDER BY table_name, product_phys
"""

_WRITE_AUTO_MAPPING = """\
MATCH (col:Column {uri: $col_uri})
MATCH (pc:DProdColumn {uri: $product_col_uri})
WITH col, pc
WHERE NOT EXISTS {
    MATCH (:ColumnMapping {isCurrent: true})-[:MAPS_TO_PRODUCT_COLUMN]->(pc)
}
CREATE (cm:ColumnMapping {
    uri:                       $mapping_uri,
    status:                    'approved',
    isCurrent:                 true,
    similarityScore:           1.0,
    rationale:                 $rationale,
    mappingType:               $mapping_type,
    transformKind:             $transform_kind,
    transformExpression:       $transform_expression,
    transformInputs:           $transform_inputs_json,
    transformParams:           '',
    transformDecorators:       '',
    transformAuthor:           'engineer',
    transformConfidence:       null,
    transformEscalationReason: null,
    aggregateFunction:         '',
    groupingKey:               false,
    createdAt:                 $timestamp
})
CREATE (cm)-[:MAPS_SOURCE_COLUMN]->(col)
CREATE (cm)-[:MAPS_TO_PRODUCT_COLUMN]->(pc)
WITH cm
CREATE (act:ProvActivity {
    uri:          'prov:activity:mapping-auto:' + replace(cm.uri, 'mapping:', '') + ':' + $timestamp,
    activityType: 'mapping_generation',
    occurredAt:   $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:system:dpe-sa-auto-mapper'})
  ON CREATE SET agent.agentType = 'system', agent.name = 'dpe-sa-auto-mapper'
CREATE (cm)-[:PROV_WAS_GENERATED_BY]->(act)
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
RETURN cm.uri AS mapping_uri
"""


def write_auto_mappings(project: Project) -> int:
    """For every materialized :DProdColumn, write a 1:1 :ColumnMapping back to
    its source :Column, paired by the stable `sourceColumnUri` provenance link
    (stamped at synthesis) rather than by reconstructing a name-based URI.

    transformKind is always 'direct' — the view-DDL generator's outer SELECT
    builder turns `<src_alias>.<source_col>` into `<src_alias>.<source_col> AS
    <safe_name(product_phys)>`, so any rename happens automatically when the
    product column's name (the approved recommendedName) differs from source_col.

    Idempotent: product columns that already have a current mapping are skipped
    (the WHERE NOT EXISTS clause in _WRITE_AUTO_MAPPING). FAILS LOUD: if any
    product column has no resolvable source column, raises ValueError listing the
    offenders instead of silently dropping them from the served product.

    Returns the number of mappings created.
    """
    contract_id = f"{project.project_code}-contract"
    timestamp = datetime.utcnow().isoformat() + "Z"
    written = 0
    unmapped: list[str] = []

    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        rows = [dict(r) for r in ns.run(_DPROD_COLUMNS_QUERY, contract_id=contract_id)]

        for row in rows:
            product_col_uri = row.get("product_col_uri")
            table_name = (row.get("table_name") or "").strip()
            product_phys = (row.get("product_phys") or "").strip()
            source_col_uri = (row.get("source_col_uri") or "").strip()

            # No provenance link, or it points at a :Column that doesn't exist →
            # this product column cannot be sourced. Never silently skip.
            if not source_col_uri or not row.get("matched_col"):
                unmapped.append(f"{table_name}.{product_phys or product_col_uri}")
                continue

            mapping_uri = (
                f"mapping:{project.project_code}:{table_name}:{product_phys}:auto"
            )
            result = ns.run(
                _WRITE_AUTO_MAPPING,
                col_uri=source_col_uri,
                product_col_uri=product_col_uri,
                mapping_uri=mapping_uri,
                rationale="Auto-generated 1:1 mapping for source-aligned product",
                mapping_type="direct",
                transform_kind="direct",
                transform_expression="",
                transform_inputs_json=json.dumps([source_col_uri]),
                timestamp=timestamp,
            ).single()
            if result:
                written += 1

    if unmapped:
        shown = ", ".join(unmapped[:10]) + ("…" if len(unmapped) > 10 else "")
        raise ValueError(
            f"{len(unmapped)} product column(s) have no resolvable source column "
            f"({shown}). The product was likely materialized before its source "
            "columns existed or were approved. Re-run Synthesize ODCS from Graph "
            "and ODCS → dprod, then re-run Auto-Map Source Columns."
        )

    return written
