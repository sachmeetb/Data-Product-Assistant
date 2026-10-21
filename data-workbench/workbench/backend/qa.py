"""Question Generation & Gap Analysis for data products.

Sibling of ``osi.py``. The deterministic half walks the producer-side graph
(``:DataContract`` → schema/properties/quality + :DProdOutputDataset + the
schema's :DatasetTransform + :CONSUMES'd source products + FK neighbours)
and builds a structured context payload. The LLM half (lives in
``routers/qa.py``) feeds that payload to the ``data-product-question-analyzer``
skill, which returns either:

- generate mode → a curated list of natural-language questions + near-miss
  gaps + a short narrative.
- probe mode → a verdict (answerable / partially / no / out_of_scope) +
  named gaps for one consumer-supplied question.

Persistence (generate mode only) appends a ``:QAEvaluation`` sidecar to the
contract via ``:HAS_QA_EVAL`` — mirrors :OsiEvaluation. Probe results are
ephemeral.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any, Optional

from .models import Project
from .neo4j_client import neo4j_session


# ── Constants ────────────────────────────────────────────────────────────

QA_ANALYZER_VERSION = "qa-v0.1.0"


# ── Neo4j helpers ────────────────────────────────────────────────────────


def _neo4j(project: Project):
    return neo4j_session(
        project.neo4j_host,
        project.neo4j_port,
        project.neo4j_user,
        project.neo4j_password,
        project.neo4j_database,
    )


# ── Context build ────────────────────────────────────────────────────────


# Reads the contract head + descriptive metadata. Mirrors osi.py:READ_CONTRACT_HEAD
# but folds in productKind + the latest snapshot fields so the analyzer has
# the version-pinned name/description/purpose without a second walk.
READ_CONTRACT_HEAD = """\
MATCH (dc:DataContract {id: $contract_id})
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv:ContractVersion {version: dc.currentVersion})
RETURN dc.id                                            AS contract_id,
       dc.currentVersion                                AS current_version,
       coalesce(cv.lifecycleState, dc.currentLifecycleState) AS lifecycle_state,
       coalesce(cv.snapshotName, dc.name)               AS name,
       coalesce(cv.snapshotDescription, dc.description) AS description,
       coalesce(cv.snapshotPurpose, dc.purpose)         AS purpose,
       coalesce(dc.productKind, '')                     AS product_kind,
       coalesce(dc.domain, '')                          AS domain,
       coalesce(dc.lastSchemaChangeVersion, dc.currentVersion) AS last_schema_change_version
"""


# Per-dataset walk. Pulls :DProdOutputDataset (descriptions + relationshipKind),
# its :DatasetTransform (shape — grain/filter/dedupe/joins/grouping/scd/window/
# suppressed), and its :DProdColumns. Single query, one row per dataset, with
# columns collected to keep the result compact.
READ_DATASETS = """\
MATCH (dp:DProdDataProduct {uri: 'dprod:' + $contract_id})
      -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
OPTIONAL MATCH (ods)-[:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform)
OPTIONAL MATCH (ods)-[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
WITH ods, dt, pc
ORDER BY ods.physicalName, coalesce(pc.ordinal, 0), pc.name
WITH ods, dt,
     collect(CASE WHEN pc IS NULL THEN NULL ELSE {
       name: pc.name,
       logical_name: coalesce(pc.logicalName, ''),
       logical_type: coalesce(pc.logicalType, ''),
       physical_type: coalesce(pc.dataType, ''),
       description: coalesce(pc.description, ''),
       is_primary_key: coalesce(pc.isPrimaryKey, false),
       sensitivity: coalesce(pc.sensitivity, 'none')
     } END) AS columns_raw
RETURN ods.uri                                  AS dataset_uri,
       ods.name                                 AS dataset_name,
       coalesce(ods.physicalName, ods.name, '') AS physical_name,
       coalesce(ods.description, '')            AS description,
       coalesce(ods.relationshipKind, '')       AS relationship_kind,
       [c IN columns_raw WHERE c IS NOT NULL]   AS columns,
       dt.filterPredicate                       AS filter_predicate,
       dt.dedupeJson                            AS dedupe_json,
       dt.joinsJson                             AS joins_json,
       dt.groupingKeysJson                      AS grouping_keys_json,
       dt.windowSpecsJson                       AS window_specs_json,
       dt.scdPolicyJson                         AS scd_policy_json,
       dt.suppressedColumnsJson                 AS suppressed_columns_json,
       dt.grainProse                            AS grain_prose
ORDER BY ods.physicalName
"""


# FK neighbours discovered between this product's :DProdOutputDatasets (FK
# propagation already runs at _generate_dprod time per CLAUDE.md). Lets the
# analyzer reason about which datasets can be joined.
READ_DPROD_FKS = """\
MATCH (src:DProdOutputDataset)-[r:REFERENCES]->(dst:DProdOutputDataset)
WHERE src.uri STARTS WITH 'dprod:ods:' + $contract_id + ':'
  AND dst.uri STARTS WITH 'dprod:ods:' + $contract_id + ':'
RETURN coalesce(src.physicalName, src.name, '') AS from_dataset,
       coalesce(dst.physicalName, dst.name, '') AS to_dataset,
       coalesce(r.columns, '[]')                AS from_columns_json,
       coalesce(r.referencedColumns, '[]')      AS to_columns_json
"""


# DQ rules attached to the contract via :HAS_QUALITY_RULE (the spec rules)
# plus :PropertyShape rows on :DProdColumn for domain / user / observation
# variants. Mirrors marketplace.py's PRODUCT_DETAIL approach (status='approved'
# for non-spec sources) but does not filter on cv.version pinning — we read
# from the head so the analyzer always sees the current state. Spec rules are
# included because they constrain answerable questions even though the
# marketplace de-dupes them against :PropertyShape spec twins.
READ_QUALITY_RULES = """\
MATCH (dc:DataContract {id: $contract_id})
OPTIONAL MATCH (dc)-[:HAS_QUALITY_RULE]->(q:DataContractQuality)
WITH dc, collect(CASE WHEN q IS NULL THEN NULL ELSE {
  rule: coalesce(q.rule, ''),
  name: coalesce(q.name, ''),
  description: coalesce(q.description, ''),
  severity: coalesce(q.severity, 'warning'),
  dimension: coalesce(q.dimension, ''),
  column: coalesce(q.column, ''),
  dataset: coalesce(q.dataset, ''),
  source: 'contract'
} END) AS contract_rules
OPTIONAL MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
      -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
      <-[:ON_DPROD_COLUMN]-(ps:PropertyShape)
WHERE ps.ruleSource IN ['domain', 'user', 'observation']
  AND coalesce(ps.status, 'pending_review') = 'approved'
WITH contract_rules, collect(CASE WHEN ps IS NULL THEN NULL ELSE {
  rule: coalesce(ps.ruleType, ''),
  name: coalesce(ps.ruleType, ''),
  description: coalesce(ps.description, ''),
  severity: coalesce(ps.severity, 'warning'),
  dimension: coalesce(ps.dimension, 'domain'),
  column: coalesce(pc.name, ''),
  dataset: coalesce(ods.physicalName, ods.name, ''),
  source: coalesce(ps.ruleSource, 'domain')
} END) AS ps_rules
WITH [r IN contract_rules WHERE r IS NOT NULL] +
     [r IN ps_rules WHERE r IS NOT NULL] AS all_rules
RETURN all_rules
"""


# CONSUMES'd source products. Cross-project URIs are fine — these are
# externally-referenceable and the analyzer just needs name + domain to
# reason about "this product can extend via the consumed source".
READ_CONSUMES = """\
MATCH (dc:DataContract {id: $contract_id})-[r:CONSUMES]->(src_dp:DProdDataProduct)
WHERE r.toVersion IS NULL
OPTIONAL MATCH (src_dp)<-[:MATERIALISES_AS]-(src_dc:DataContract)
RETURN src_dp.uri                       AS uri,
       coalesce(src_dp.name, '')        AS name,
       coalesce(src_dc.domain, '')      AS domain,
       coalesce(src_dp.productKind, '') AS product_kind
"""


def _parse_json(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return default
    s = raw.strip()
    if not s:
        return default
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return default


def _shape_dataset(row: dict, fks_by_dataset: dict[str, list[dict]]) -> dict:
    """Normalise one :DProdOutputDataset row into the analyzer's input shape."""
    physical = row["physical_name"] or row["dataset_name"] or ""
    dedupe = _parse_json(row.get("dedupe_json"), None)
    if isinstance(dedupe, dict) and not dedupe.get("keys"):
        dedupe = None
    scd = _parse_json(row.get("scd_policy_json"), None)
    if isinstance(scd, dict) and not scd.get("type"):
        scd = None
    joins = _parse_json(row.get("joins_json"), [])
    if not isinstance(joins, list):
        joins = []
    grouping = _parse_json(row.get("grouping_keys_json"), [])
    if not isinstance(grouping, list):
        grouping = []
    suppressed = _parse_json(row.get("suppressed_columns_json"), [])
    if not isinstance(suppressed, list):
        suppressed = []
    window_specs = _parse_json(row.get("window_specs_json"), {})
    if not isinstance(window_specs, dict):
        window_specs = {}

    columns = []
    for c in row.get("columns") or []:
        if not isinstance(c, dict):
            continue
        if not (c.get("name") or "").strip():
            continue
        columns.append({
            "name": c.get("name") or "",
            "logical_type": c.get("logical_type") or "",
            "physical_type": c.get("physical_type") or "",
            "description": c.get("description") or "",
            "is_primary_key": bool(c.get("is_primary_key")),
            "sensitivity": c.get("sensitivity") or "none",
        })

    return {
        "name": physical,
        "logical_name": row["dataset_name"] or physical,
        "description": row.get("description") or "",
        "relationship_kind": row.get("relationship_kind") or "",
        "grain_prose": row.get("grain_prose") or "",
        "filter": row.get("filter_predicate") or "",
        "dedupe": dedupe,
        "joins": joins,
        "grouping_keys": grouping,
        "scd_policy": scd,
        "suppressed_columns": suppressed,
        "window_specs": window_specs,
        "columns": columns,
        "fk_neighbors": fks_by_dataset.get(physical, []),
    }


def build_qa_context(project: Project, contract_id: str) -> Optional[dict]:
    """Walk the contract graph and assemble the analyzer's input payload.

    Returns ``None`` when the contract isn't materialised yet (no
    :DProdDataProduct). The caller should surface that as a 409 so the UI
    can render an "analyze after publish" empty state.
    """
    with _neo4j(project) as ns:
        head = ns.run(READ_CONTRACT_HEAD, contract_id=contract_id).single()
        if not head:
            return None

        dataset_rows = list(ns.run(READ_DATASETS, contract_id=contract_id))
        if not dataset_rows:
            # Contract exists but dprod hasn't been generated. The caller
            # decides whether to fail loud or render an empty-state.
            return {
                "product": {
                    "name": head["name"] or "",
                    "domain": head["domain"] or "",
                    "description": head["description"] or "",
                    "purpose": head["purpose"] or "",
                    "product_kind": head["product_kind"] or "",
                    "lifecycle_state": head["lifecycle_state"] or "",
                },
                "datasets": [],
                "quality_rules": [],
                "consumes": [],
                "_meta": {
                    "contract_id": contract_id,
                    "current_version": head["current_version"],
                    "last_schema_change_version": head["last_schema_change_version"],
                },
            }

        fk_rows = list(ns.run(READ_DPROD_FKS, contract_id=contract_id))
        fks_by_dataset: dict[str, list[dict]] = {}
        for r in fk_rows:
            src = r["from_dataset"]
            dst = r["to_dataset"]
            from_cols = _parse_json(r["from_columns_json"], [])
            to_cols = _parse_json(r["to_columns_json"], [])
            if not (src and dst and isinstance(from_cols, list) and isinstance(to_cols, list)):
                continue
            fks_by_dataset.setdefault(src, []).append({
                "to_dataset": dst,
                "from_columns": [str(c) for c in from_cols],
                "to_columns": [str(c) for c in to_cols],
            })

        datasets = [_shape_dataset(dict(row), fks_by_dataset) for row in dataset_rows]

        rules_row = ns.run(READ_QUALITY_RULES, contract_id=contract_id).single()
        all_rules = []
        if rules_row and rules_row["all_rules"]:
            seen: set[tuple] = set()
            for r in rules_row["all_rules"]:
                if not isinstance(r, dict):
                    continue
                key = (
                    r.get("rule", ""),
                    r.get("column", ""),
                    r.get("dataset", ""),
                    r.get("source", ""),
                )
                if key in seen:
                    continue
                seen.add(key)
                all_rules.append({
                    "rule": r.get("rule", ""),
                    "name": r.get("name", ""),
                    "description": r.get("description", ""),
                    "severity": r.get("severity", "warning"),
                    "dimension": r.get("dimension", ""),
                    "column": r.get("column", ""),
                    "dataset": r.get("dataset", ""),
                    "source": r.get("source", ""),
                })

        consumes_rows = list(ns.run(READ_CONSUMES, contract_id=contract_id))
        consumes = [
            {
                "uri": r["uri"],
                "name": r["name"] or "",
                "domain": r["domain"] or "",
                "product_kind": r["product_kind"] or "",
            }
            for r in consumes_rows
            if r["uri"]
        ]

    return {
        "product": {
            "name": head["name"] or "",
            "domain": head["domain"] or "",
            "description": head["description"] or "",
            "purpose": head["purpose"] or "",
            "product_kind": head["product_kind"] or "",
            "lifecycle_state": head["lifecycle_state"] or "",
        },
        "datasets": datasets,
        "quality_rules": all_rules,
        "consumes": consumes,
        "_meta": {
            "contract_id": contract_id,
            "current_version": head["current_version"],
            "last_schema_change_version": head["last_schema_change_version"],
        },
    }


# ── Persistence ──────────────────────────────────────────────────────────


PERSIST_EVAL_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})
CREATE (qa:QAEvaluation {
    uri:                  'qa:eval:' + $contract_id + ':' + $batch_id,
    mode:                 'generate',
    questionsJson:        $questions_json,
    nearMissGapsJson:     $near_miss_gaps_json,
    narrative:            $narrative,
    generatedForVersion:  $generated_for_version,
    triggeredBy:          $triggered_by,
    analyzerVersion:      $analyzer_version,
    advisorError:         $advisor_error,
    evaluatedAt:          datetime(),
    batchId:              $batch_id
})
CREATE (dc)-[:HAS_QA_EVAL]->(qa)
RETURN qa.uri AS uri
"""


def _new_batch_id() -> str:
    return f"{int(time.time() * 1000)}-{secrets.token_hex(4)}"


def persist_qa_evaluation(
    project: Project,
    contract_id: str,
    payload: dict,
    triggered_by: str,
    generated_for_version: int,
    advisor_error: Optional[str] = None,
    batch_id: Optional[str] = None,
) -> dict:
    """Append a :QAEvaluation sidecar to the contract. ``payload`` is the
    skill's output JSON; we serialise ``questions`` and ``near_miss_gaps``
    as JSON blobs on the node.

    Returns ``{batch_id, uri}``. When the contract isn't found the URI is
    ``None`` and the caller still gets a stable batch_id for client-side
    correlation.
    """
    if not contract_id:
        return {"batch_id": batch_id or _new_batch_id(), "uri": None}
    bid = batch_id or _new_batch_id()
    questions = payload.get("questions") or []
    near_miss = payload.get("near_miss_gaps") or []
    narrative = payload.get("narrative") or ""

    with _neo4j(project) as ns:
        row = ns.run(
            PERSIST_EVAL_QUERY,
            contract_id=contract_id,
            questions_json=json.dumps(questions if isinstance(questions, list) else []),
            near_miss_gaps_json=json.dumps(near_miss if isinstance(near_miss, list) else []),
            narrative=narrative if isinstance(narrative, str) else "",
            generated_for_version=int(generated_for_version or 1),
            triggered_by=triggered_by,
            analyzer_version=QA_ANALYZER_VERSION,
            advisor_error=advisor_error or "",
            batch_id=bid,
        ).single()
    return {"batch_id": bid, "uri": row["uri"] if row else None}


# ── Read latest eval (helper used by the router's GET handler) ───────────


READ_LATEST_EVAL = """\
MATCH (dc:DataContract {id: $contract_id})-[:HAS_QA_EVAL]->(qa:QAEvaluation)
WITH qa, dc ORDER BY qa.evaluatedAt DESC LIMIT 1
RETURN qa.uri                  AS uri,
       qa.mode                 AS mode,
       qa.questionsJson        AS questions_json,
       qa.nearMissGapsJson     AS near_miss_gaps_json,
       qa.narrative            AS narrative,
       qa.generatedForVersion  AS generated_for_version,
       qa.triggeredBy          AS triggered_by,
       qa.analyzerVersion      AS analyzer_version,
       qa.advisorError         AS advisor_error,
       qa.evaluatedAt          AS evaluated_at,
       qa.batchId              AS batch_id,
       coalesce(dc.lastSchemaChangeVersion, dc.currentVersion) AS current_change_version
"""


def read_latest_evaluation(project: Project, contract_id: str) -> Optional[dict]:
    """Return the most recent :QAEvaluation for ``contract_id`` shaped for
    the API. Includes a ``stale`` flag computed against
    ``:DataContract.lastSchemaChangeVersion``.
    """
    with _neo4j(project) as ns:
        row = ns.run(READ_LATEST_EVAL, contract_id=contract_id).single()
    if not row:
        return None
    questions = _parse_json(row["questions_json"], [])
    near_miss = _parse_json(row["near_miss_gaps_json"], [])
    generated_for = row["generated_for_version"]
    current_change = row["current_change_version"]
    stale = (
        generated_for is not None
        and current_change is not None
        and generated_for < current_change
    )
    evaluated_at = row["evaluated_at"]
    return {
        "uri": row["uri"],
        "mode": row["mode"],
        "questions": questions if isinstance(questions, list) else [],
        "near_miss_gaps": near_miss if isinstance(near_miss, list) else [],
        "narrative": row["narrative"] or "",
        "generated_for_version": generated_for,
        "current_change_version": current_change,
        "stale": stale,
        "triggered_by": row["triggered_by"],
        "analyzer_version": row["analyzer_version"],
        "advisor_error": row["advisor_error"] or None,
        "evaluated_at": evaluated_at.isoformat() if evaluated_at else None,
        "batch_id": row["batch_id"],
    }
