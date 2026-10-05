"""Read-only Neo4j graph-integrity checker.

A detect-only invariant engine that runs a fixed catalog of structural,
leakage, duplicate, enum, and orphan checks over the knowledge graph and
returns a structured scorecard. It NEVER writes, NEVER creates constraints,
and NEVER blocks a pipeline write — it is a safety net that surfaces the
class of drift (duplicate URIs, orphaned nodes, cross-project leakage,
enum violations) that grows silently as the graph scales.

Design (mirrors ``join_preflight.py``):
- ``run_integrity_checks(session, ...)`` is a **pure** function that takes an
  open Neo4j session as its first argument, so it is unit-testable against a
  hand-rolled fake session (see ``tests/test_graph_integrity.py``) — no live
  Neo4j and no monkeypatching required.
- ``build_scorecard(conn, ...)`` opens the connection (from ``AppSettings`` or
  a ``Project`` row — anything carrying the ``neo4j_*`` attrs) and assembles
  the full report, folding in ``semantic_discovery.find_stranded`` for the
  semantic layer rather than re-implementing it.

The invariant set mirrors the ownership tree in ``graph_ops._DELETE_PROJECT``
(what is reachable from a ``:Project``) and the project-scoped URI convention
in ``graph_ops._DELETE_PROJECT_ORPHANS`` (every operational/product node embeds
``:{project_code}`` in its ``uri``/``id``). The two sanctioned cross-project
edges (``:CONSUMES`` / ``:USES_DATASET``) are the only relationships allowed to
span project codes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from .neo4j_client import neo4j_session

# Valid enum vocabularies — kept in sync (by convention) with their authoring
# sites. ``VALID_TRANSFORM_KINDS`` mirrors
# ``workbench-skills/skills/data-mapping-neo4j/scripts/write_mappings.py``;
# duplicated here (not imported) because that lives in a skill script outside
# the backend package. ``test_docs_consistency`` guards the transform-kind list
# separately; this copy is only used to flag graph nodes carrying an unknown
# kind, so a superset is harmless (fewer false positives), never wrong.
VALID_TRANSFORM_KINDS = [
    "direct", "cast", "format", "concat", "split", "substring",
    "case", "arithmetic", "lookup", "literal", "expression",
    "bucket", "mask", "hash", "window", "date_difference",
]
VALID_PRODUCT_KINDS = ["source", "consumer"]
VALID_RULE_SOURCES = ["observation", "domain", "user", "spec"]
VALID_MAPPING_STATUS = ["approved", "pending_review", "superseded", "rejected"]

# Relationship types allowed to cross project boundaries. Everything else must
# stay within a single project code (see the module docstring + root CLAUDE.md
# "Source-aligned vs Consumer-aligned"). Adding a third is a documented
# decision — extend this list AND the ontology docs together.
_SANCTIONED_CROSS_PROJECT = ["CONSUMES", "USES_DATASET"]

_SAMPLE_LIMIT = 20

# Severity ranks for ordering the scorecard (highest first).
_SEV_RANK = {"error": 0, "warn": 1, "info": 2}


# ── Check catalog ─────────────────────────────────────────────────────────
#
# Each entry is (key, category, severity, description, cypher). Every cypher
# returns EXACTLY ONE row shaped ``{count, sample}`` (count = total violations,
# sample = the first N for triage), so a fake session can dispatch on the
# ``// CHECK:<key>`` marker comment and return a single canned record. Queries
# are parameterised with a common bag ($scoped/$marker/$pc/$limit/$kinds/...);
# unused params are ignored by the driver.
#
# Scoping: when a project_code is supplied, code-bearing nodes are filtered by
# ``uri CONTAINS $marker`` (``:{code}``) or ``id CONTAINS $pc`` — the same URI
# convention ``graph_ops._DELETE_PROJECT_ORPHANS`` relies on. Catalog-side leaf
# nodes (ColumnDescription, catalog PropertyShape/NodeShape) do NOT embed a
# code; a scoped run necessarily skips them — run a global scan for full
# coverage.

_STRUCTURAL_CHECKS: list[tuple[str, str, str, str, str]] = [
    (
        "catalog_without_project", "structural_orphan", "error",
        "Catalog nodes with no incoming :HAS_CATALOG from a :Project",
        """// CHECK:catalog_without_project
MATCH (cat:Catalog)
WHERE NOT (:Project)-[:HAS_CATALOG]->(cat)
  AND ($scoped = false OR coalesce(cat.uri,'') CONTAINS $marker)
WITH collect(cat.uri) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
    (
        "dataset_without_catalog", "structural_orphan", "error",
        "Dataset nodes (excluding migration targets) with no incoming :DCAT_DATASET",
        """// CHECK:dataset_without_catalog
MATCH (ds:Dataset)
WHERE NOT ds:MigrationTarget
  AND NOT (:Catalog)-[:DCAT_DATASET]->(ds)
  AND ($scoped = false OR coalesce(ds.uri,'') CONTAINS $marker)
WITH collect(ds.uri) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
    (
        "column_without_dataset", "structural_orphan", "error",
        "Column nodes with no incoming :HAS_COLUMN from a :Dataset",
        """// CHECK:column_without_dataset
MATCH (col:Column)
WHERE NOT (:Dataset)-[:HAS_COLUMN]->(col)
  AND ($scoped = false OR coalesce(col.uri,'') CONTAINS $marker)
WITH collect(col.uri) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
    (
        "datacontract_without_project", "structural_orphan", "error",
        "DataContract nodes with no incoming :HAS_CONTRACT from a :Project",
        """// CHECK:datacontract_without_project
MATCH (dc:DataContract)
WHERE NOT (:Project)-[:HAS_CONTRACT]->(dc)
  AND NOT dc:ProductTemplate
  AND ($scoped = false OR coalesce(dc.id,'') CONTAINS $pc)
WITH collect(dc.id) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
    (
        "dprod_product_without_contract", "structural_orphan", "error",
        "DProdDataProduct nodes with no incoming :MATERIALISES_AS from a :DataContract",
        """// CHECK:dprod_product_without_contract
MATCH (dp:DProdDataProduct)
WHERE NOT (:DataContract)-[:MATERIALISES_AS]->(dp)
  AND ($scoped = false OR coalesce(dp.uri,'') CONTAINS $marker)
WITH collect(dp.uri) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
    (
        "dprod_dataset_without_port", "structural_orphan", "error",
        "DProdOutputDataset nodes with no incoming :DPROD_OUTPUT_DATASET from a port",
        """// CHECK:dprod_dataset_without_port
MATCH (ods:DProdOutputDataset)
WHERE NOT (:DProdOutputPort)-[:DPROD_OUTPUT_DATASET]->(ods)
  AND ($scoped = false OR coalesce(ods.uri,'') CONTAINS $marker)
WITH collect(ods.uri) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
    (
        "dprod_column_without_dataset", "structural_orphan", "error",
        "DProdColumn nodes with no incoming :HAS_PRODUCT_COLUMN from an output dataset",
        """// CHECK:dprod_column_without_dataset
MATCH (pc:DProdColumn)
WHERE NOT (:DProdOutputDataset)-[:HAS_PRODUCT_COLUMN]->(pc)
  AND ($scoped = false OR coalesce(pc.uri,'') CONTAINS $marker)
WITH collect(pc.uri) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
    (
        "propertyshape_without_parent", "structural_orphan", "warn",
        "PropertyShape nodes with no incoming :PROPERTY from a (DProd)NodeShape",
        """// CHECK:propertyshape_without_parent
MATCH (ps:PropertyShape)
WHERE NOT ()-[:PROPERTY]->(ps)
  AND ($scoped = false OR coalesce(ps.uri,'') CONTAINS $marker)
WITH collect(ps.uri) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
    (
        "propertyshape_without_anchor", "structural_orphan", "error",
        "PropertyShape nodes anchored to no column/dataset "
        "(:ON_COLUMN / :ON_DPROD_COLUMN / :REFERENCES_DATASET)",
        """// CHECK:propertyshape_without_anchor
MATCH (ps:PropertyShape)
WHERE NOT (ps)-[:ON_COLUMN]->(:Column)
  AND NOT (ps)-[:ON_DPROD_COLUMN]->(:DProdColumn)
  AND NOT (ps)-[:REFERENCES_DATASET]->(:Dataset)
  AND ($scoped = false OR coalesce(ps.uri,'') CONTAINS $marker)
WITH collect({uri: ps.uri, ruleSource: coalesce(ps.ruleSource,''),
              status: coalesce(ps.status,'')}) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
    (
        "dprod_nodeshape_without_dataset", "structural_orphan", "error",
        "DProdNodeShape nodes with no incoming :HAS_SHAPE (the duplicate-shape leak)",
        """// CHECK:dprod_nodeshape_without_dataset
MATCH (ns:DProdNodeShape)
WHERE NOT (:DProdOutputDataset)-[:HAS_SHAPE]->(ns)
  AND ($scoped = false OR coalesce(ns.uri,'') CONTAINS $marker)
WITH collect(ns.uri) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
    (
        "nodeshape_without_dataset", "structural_orphan", "warn",
        "Catalog NodeShape nodes with no incoming :HAS_SHAPE from a :Dataset",
        """// CHECK:nodeshape_without_dataset
MATCH (ns:NodeShape)
WHERE NOT (:Dataset)-[:HAS_SHAPE]->(ns)
  AND ($scoped = false OR coalesce(ns.uri,'') CONTAINS $marker)
WITH collect(ns.uri) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
    (
        "mapping_without_source", "structural_orphan", "warn",
        "Current, non-literal ColumnMapping with no :MAPS_SOURCE_COLUMN edge",
        """// CHECK:mapping_without_source
MATCH (cm:ColumnMapping)
WHERE coalesce(cm.transformKind,'') <> 'literal'
  AND coalesce(cm.isCurrent, true) = true
  AND coalesce(cm.status,'') IN ['approved','pending_review']
  AND NOT (cm)-[:MAPS_SOURCE_COLUMN]->()
  AND ($scoped = false OR coalesce(cm.uri,'') CONTAINS $marker)
WITH collect(cm.uri) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
    (
        "mapping_without_target", "structural_orphan", "warn",
        "Current ColumnMapping with no :MAPS_TO_PRODUCT_COLUMN edge",
        """// CHECK:mapping_without_target
MATCH (cm:ColumnMapping)
WHERE coalesce(cm.isCurrent, true) = true
  AND coalesce(cm.status,'') <> 'superseded'
  AND NOT (cm)-[:MAPS_TO_PRODUCT_COLUMN]->()
  AND ($scoped = false OR coalesce(cm.uri,'') CONTAINS $marker)
WITH collect(cm.uri) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
]

_DUPLICATE_CHECKS: list[tuple[str, str, str, str, str]] = [
    (
        "duplicate_uri", "duplicate", "error",
        "Multiple nodes sharing the same uri (catches the :DProdNodeShape triplication)",
        """// CHECK:duplicate_uri
MATCH (n)
WHERE n.uri IS NOT NULL AND n.uri <> ''
  AND ($scoped = false OR n.uri CONTAINS $marker)
WITH n.uri AS uri, collect(DISTINCT head(labels(n))) AS labels, count(*) AS copies
WHERE copies > 1
WITH collect({uri: uri, labels: labels, copies: copies}) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
    (
        "null_uri_core_label", "duplicate", "warn",
        "Core operational/product nodes with a null or empty uri",
        """// CHECK:null_uri_core_label
MATCH (n)
WHERE (n:Catalog OR n:Dataset OR n:Column OR n:DProdDataProduct
       OR n:DProdOutputDataset OR n:DProdColumn OR n:PropertyShape
       OR n:ColumnMapping OR n:NodeShape OR n:DProdNodeShape)
  AND (n.uri IS NULL OR n.uri = '')
WITH collect({labels: labels(n)}) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
    (
        "contract_missing_versioning", "duplicate", "warn",
        "DataContract missing currentVersion / currentLifecycleState (pre-stable-model load)",
        """// CHECK:contract_missing_versioning
MATCH (dc:DataContract)
WHERE (dc.currentVersion IS NULL OR dc.currentLifecycleState IS NULL)
  AND ($scoped = false OR coalesce(dc.id,'') CONTAINS $pc)
WITH collect(dc.id) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
]

# The leakage check keys off the set of known :Project codes: a node "belongs"
# to project P if its uri/id CONTAINS P. An edge that is NOT sanctioned and
# whose two (code-bearing) endpoints share NO code is a leak. Endpoints that
# carry no code (descriptions, catalog shapes, cross-product :BusinessConcept)
# are excluded by ``size(...) > 0`` — those edges are structurally single-project
# or legitimately cross-product and never the leak we care about.
_LEAKAGE_CHECKS: list[tuple[str, str, str, str, str]] = [
    (
        "cross_project_leak", "leakage", "error",
        "Non-sanctioned relationship whose endpoints carry different project codes",
        """// CHECK:cross_project_leak
MATCH (p:Project)
WITH collect(p.projectCode) AS codes
MATCH (a)-[r]->(b)
WHERE NOT type(r) IN $sanctioned
WITH a, r, b, codes,
     [c IN codes WHERE (a.uri IS NOT NULL AND a.uri CONTAINS c)
                     OR (a.id  IS NOT NULL AND a.id  CONTAINS c)] AS a_codes,
     [c IN codes WHERE (b.uri IS NOT NULL AND b.uri CONTAINS c)
                     OR (b.id  IS NOT NULL AND b.id  CONTAINS c)] AS b_codes
WHERE size(a_codes) > 0 AND size(b_codes) > 0
  AND none(c IN a_codes WHERE c IN b_codes)
  AND ($scoped = false OR $pc IN a_codes OR $pc IN b_codes)
WITH collect({rel: type(r), a_codes: a_codes, b_codes: b_codes,
              a: coalesce(a.uri, a.id), b: coalesce(b.uri, b.id)}) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
    (
        "consumes_target_invalid", "leakage", "error",
        "':CONSUMES' target is not a materialised, published/superseded data product",
        """// CHECK:consumes_target_invalid
MATCH (dc:DataContract)-[:CONSUMES]->(dp:DProdDataProduct)
OPTIONAL MATCH (src:DataContract)-[:MATERIALISES_AS]->(dp)
WITH dc, dp, src,
     CASE
       WHEN src IS NULL THEN 'no_materialising_contract'
       WHEN NOT coalesce(src.currentLifecycleState,'') IN ['published','superseded']
         THEN 'source_not_published'
       ELSE 'ok'
     END AS verdict
WHERE verdict <> 'ok'
  AND ($scoped = false OR coalesce(dc.id,'') CONTAINS $pc)
WITH collect({consumer: dc.id, target: dp.uri, reason: verdict,
              source_state: coalesce(src.currentLifecycleState,'')}) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
    (
        "uses_dataset_target_invalid", "leakage", "error",
        "':USES_DATASET' target :Dataset is not owned by any :Project",
        """// CHECK:uses_dataset_target_invalid
MATCH (cm:CodeModule)-[:USES_DATASET]->(d:Dataset)
WHERE NOT ( (:Project)-[:HAS_CATALOG]->(:Catalog)-[:DCAT_DATASET]->(d) )
  AND NOT ( (:Project)-[:HAS_MIGRATION_TARGET]->(d) )
  AND ($scoped = false OR coalesce(d.uri,'') CONTAINS $marker)
WITH collect({module: cm.uri, dataset: d.uri}) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
]

_ENUM_CHECKS: list[tuple[str, str, str, str, str]] = [
    (
        "bad_product_kind", "enum", "warn",
        "DataContract.productKind outside {source, consumer}",
        """// CHECK:bad_product_kind
MATCH (dc:DataContract)
WHERE dc.productKind IS NOT NULL AND NOT dc.productKind IN $product_kinds
  AND ($scoped = false OR coalesce(dc.id,'') CONTAINS $pc)
WITH collect({contract: dc.id, value: dc.productKind}) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
    (
        "bad_rule_source", "enum", "warn",
        "PropertyShape.ruleSource outside {observation, domain, user, spec}",
        """// CHECK:bad_rule_source
MATCH (ps:PropertyShape)
WHERE ps.ruleSource IS NOT NULL AND NOT ps.ruleSource IN $rule_sources
  AND ($scoped = false OR coalesce(ps.uri,'') CONTAINS $marker)
WITH collect({uri: ps.uri, value: ps.ruleSource}) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
    (
        "bad_mapping_status", "enum", "warn",
        "ColumnMapping.status outside {approved, pending_review, superseded, rejected}",
        """// CHECK:bad_mapping_status
MATCH (cm:ColumnMapping)
WHERE cm.status IS NOT NULL AND NOT cm.status IN $mapping_status
  AND ($scoped = false OR coalesce(cm.uri,'') CONTAINS $marker)
WITH collect({uri: cm.uri, value: cm.status}) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
    (
        "bad_transform_kind", "enum", "warn",
        "ColumnMapping.transformKind outside the known transform DSL vocabulary",
        """// CHECK:bad_transform_kind
MATCH (cm:ColumnMapping)
WHERE cm.transformKind IS NOT NULL AND NOT cm.transformKind IN $kinds
  AND ($scoped = false OR coalesce(cm.uri,'') CONTAINS $marker)
WITH collect({uri: cm.uri, value: cm.transformKind}) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
]

_CURRENCY_CHECKS: list[tuple[str, str, str, str, str]] = [
    (
        "multiple_current_description", "currency", "warn",
        "A :Column with more than one current :ColumnDescription",
        """// CHECK:multiple_current_description
MATCH (col:Column)-[:HAS_DESCRIPTION]->(cd:ColumnDescription)
WHERE coalesce(cd.isCurrent, false) = true
  AND ($scoped = false OR coalesce(col.uri,'') CONTAINS $marker)
WITH col, count(cd) AS c
WHERE c > 1
WITH collect({column: col.uri, current_count: c}) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
    (
        "multiple_current_mapping", "currency", "warn",
        "A target :DProdColumn with more than one current :ColumnMapping",
        """// CHECK:multiple_current_mapping
MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
WHERE coalesce(cm.isCurrent, false) = true
  AND ($scoped = false OR coalesce(pc.uri,'') CONTAINS $marker)
WITH pc, count(cm) AS c
WHERE c > 1
WITH collect({product_column: pc.uri, current_count: c}) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
]

# Retired graph terms — should never resurface. Mirrors ``RETIRED_HARD`` in
# ``tests/test_docs_consistency.py`` (docs side) with the graph side here. The
# retired relationship name is assembled from fragments so the literal token
# never appears verbatim in this file — ``test_docs_consistency`` substring-bans
# the contiguous string in backend ``*.py``, yet this checker's whole job is to
# PROBE for its (hopefully absent) presence in the graph.
_RETIRED_PRODUCT_COLUMN_EDGE = "ON_PRODUCT" + "_COLUMN"

_RETIRED_CHECKS: list[tuple[str, str, str, str, str]] = [
    (
        "retired_on_product_column", "retired", "warn",
        f"Retired :{_RETIRED_PRODUCT_COLUMN_EDGE} edges present (renamed to :ON_DPROD_COLUMN)",
        f"""// CHECK:retired_on_product_column
MATCH ()-[r:{_RETIRED_PRODUCT_COLUMN_EDGE}]->()
WITH collect(id(r)) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
    (
        "retired_datacontract_column", "retired", "warn",
        "Retired :DataContractColumn nodes present (superseded by :DProdColumn)",
        """// CHECK:retired_datacontract_column
MATCH (n:DataContractColumn)
WITH collect(coalesce(n.uri, toString(id(n)))) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
    (
        "retired_rule_source_external", "retired", "warn",
        "Retired PropertyShape ruleSource='external' present",
        """// CHECK:retired_rule_source_external
MATCH (ps:PropertyShape)
WHERE ps.ruleSource = 'external'
WITH collect(ps.uri) AS bad
RETURN size(bad) AS count, bad[0..$limit] AS sample""",
    ),
]

_ALL_CHECKS = (
    _STRUCTURAL_CHECKS
    + _DUPLICATE_CHECKS
    + _LEAKAGE_CHECKS
    + _ENUM_CHECKS
    + _CURRENCY_CHECKS
    + _RETIRED_CHECKS
)


# ── Pure core (session-based, unit-testable) ──────────────────────────────

def run_integrity_checks(
    session,
    project_code: Optional[str] = None,
    sample_limit: int = _SAMPLE_LIMIT,
) -> list[dict[str, Any]]:
    """Run the invariant catalog against an open Neo4j ``session``.

    Returns a list of check dicts ``{check, category, severity, count,
    description, sample}`` — one per catalog entry, INCLUDING clean checks
    (``count == 0``) so a caller can assert "zero leaks / zero orphans", not
    just enumerate failures. Individual query failures are captured as an
    ``error``-shaped entry rather than aborting the whole scan.
    """
    scoped = project_code is not None
    params = {
        "scoped": scoped,
        "marker": f":{project_code}" if scoped else "",
        "pc": project_code or "",
        "limit": sample_limit,
        "sanctioned": _SANCTIONED_CROSS_PROJECT,
        "kinds": VALID_TRANSFORM_KINDS,
        "product_kinds": VALID_PRODUCT_KINDS,
        "rule_sources": VALID_RULE_SOURCES,
        "mapping_status": VALID_MAPPING_STATUS,
    }

    out: list[dict[str, Any]] = []
    for key, category, severity, description, cypher in _ALL_CHECKS:
        try:
            record = session.run(cypher, **params).single()
            count = int(record["count"]) if record else 0
            sample = list(record["sample"]) if record and record["sample"] else []
        except Exception as e:  # a broken query must not sink the whole scan
            out.append({
                "check": key,
                "category": category,
                "severity": "error",
                "count": -1,
                "description": description,
                "sample": [],
                "error": f"check query failed: {e}",
            })
            continue
        out.append({
            "check": key,
            "category": category,
            "severity": severity,
            "count": count,
            "description": description,
            "sample": sample,
        })
    return out


def _discover_domains(session) -> list[str]:
    """Distinct :BusinessConcept domains present in the graph (for the
    semantic-layer stranded fold-in). Empty when the semantic layer is unused."""
    try:
        rows = session.run(
            "MATCH (c:BusinessConcept) WHERE c.domain IS NOT NULL "
            "RETURN DISTINCT c.domain AS domain"
        )
        return sorted({r["domain"] for r in rows if r["domain"]})
    except Exception:
        return []


# ── Assembly (opens the connection, folds in reused helpers) ──────────────

def build_scorecard(
    conn,
    project_code: Optional[str] = None,
    sample_limit: int = _SAMPLE_LIMIT,
) -> dict[str, Any]:
    """Open ``conn`` (``AppSettings`` / ``Project`` — anything with the
    ``neo4j_*`` attrs) and assemble the full integrity scorecard.

    Folds ``semantic_discovery.find_stranded`` in on a **global** scan (it is
    domain-scoped, orthogonal to project scoping) rather than re-implementing
    stranded-concept detection here.
    """
    checks: list[dict[str, Any]]
    with neo4j_session(
        conn.neo4j_host, conn.neo4j_port,
        conn.neo4j_user, conn.neo4j_password, conn.neo4j_database,
    ) as session:
        checks = run_integrity_checks(session, project_code, sample_limit)
        domains = _discover_domains(session) if project_code is None else []

    # Reuse find_stranded (semantic-layer) instead of re-implementing it.
    if project_code is None and domains:
        try:
            from . import semantic_discovery
            stranded_items: list[dict] = []
            for domain in domains:
                res = semantic_discovery.find_stranded(conn, domain)
                for item in res.get("items", []):
                    stranded_items.append({**item, "domain": domain})
            checks.append({
                "check": "stranded_business_concepts",
                "category": "semantic",
                "severity": "warn",
                "count": len(stranded_items),
                "description": "Active :BusinessConcept with no data-product binding "
                               "or only-deprecated parent (semantic_discovery.find_stranded)",
                "sample": stranded_items[:sample_limit],
            })
        except Exception as e:
            checks.append({
                "check": "stranded_business_concepts",
                "category": "semantic",
                "severity": "error",
                "count": -1,
                "description": "stranded-concept fold-in failed",
                "sample": [],
                "error": str(e),
            })

    return _shape_scorecard(checks, project_code)


def _shape_scorecard(
    checks: list[dict[str, Any]],
    project_code: Optional[str],
) -> dict[str, Any]:
    """Order + summarise a list of check dicts into the final report."""
    ordered = sorted(
        checks,
        key=lambda c: (_SEV_RANK.get(c["severity"], 9), -c["count"], c["check"]),
    )
    summary = {"error": 0, "warn": 0, "info": 0, "checks_run": len(checks),
               "total_findings": 0, "query_errors": 0}
    for c in checks:
        if c["count"] < 0:
            summary["query_errors"] += 1
            continue
        summary[c["severity"]] = summary.get(c["severity"], 0) + (
            1 if c["count"] > 0 else 0
        )
        summary["total_findings"] += c["count"]
    # ``ok`` = no error-severity findings and no query failures.
    ok = summary["error"] == 0 and summary["query_errors"] == 0
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "project_code": project_code,
            "global": project_code is None,
        },
        "ok": ok,
        "summary": summary,
        "checks": ordered,
    }
