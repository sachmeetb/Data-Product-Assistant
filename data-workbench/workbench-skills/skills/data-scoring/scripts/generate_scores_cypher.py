#!/usr/bin/env python3
"""
Generate data quality score Cypher from an enriched Neo4j knowledge graph.

Queries the graph built by data-discovery-to-dcat-neo4j, data-profiling-to-dqv-neo4j,
and data-quality-rule-generation, then computes quality scores across six dimensions
and writes :QualityScore and :QualityDimension nodes.

Scoring dimensions:
  completeness       — 1.0 - null_rate (0.0 if not profiled)
  uniqueness         — distinct_count / row_count for PK/unique columns
  validity           — test pass rate (if :TestResult exists) else allowedValues
                       coverage or range rule presence
  consistency        — referential integrity rule coverage for FK columns
  schema_conformance — dataType + nullable + profiled (3 × 0.333)
  rule_coverage      — has any DQ rule on the column
  documentation      — 0.0 none | 0.35 pending/draft | 0.1 rejected | 1.0 approved
  grounding          — fraction of rules with ruleSource in {'domain','external'}
                       AND status='approved' (Tier 3)

Each :QualityScore node also carries an `evidence` property:
  'test'    — dimension derived from :TestResult pass rates
  'profile' — dimension derived from profiling measurements
  'rule'    — dimension derived from rule presence alone
  'meta'    — dimension derived from schema/description metadata

Usage:
    python generate_scores_cypher.py [output_file] [options]

    output_file   path for the output .cypher file (default: ./cypher_scripts/dq_scores.cypher)

Options:
    --host        Neo4j host (default: localhost)
    --bolt-port   Bolt port (default: 7687)
    --username    Neo4j username (default: neo4j)
    --password    Neo4j password (default: your_password)
    --database    Neo4j database name (default: neo4j)
"""

import sys
import os
import argparse
import json
from datetime import datetime, timezone

try:
    from neo4j import GraphDatabase
    from neo4j.exceptions import ServiceUnavailable, AuthError
except ImportError:
    print("ERROR: neo4j driver is required. Install with: pip install neo4j", file=sys.stderr)
    sys.exit(1)

# ── Dimension weights ────────────────────────────────────────────────────────

DIMENSIONS = {
    "completeness":       {"name": "Completeness",       "weight": 0.20, "description": "Measures null-free data coverage"},
    "uniqueness":         {"name": "Uniqueness",         "weight": 0.16, "description": "Measures distinct value ratios for unique columns"},
    "validity":           {"name": "Validity",           "weight": 0.16, "description": "Measures conformance to value constraints (test-driven when available)"},
    "consistency":        {"name": "Consistency",        "weight": 0.10, "description": "Measures referential integrity coverage"},
    "schema_conformance": {"name": "Schema Conformance", "weight": 0.08, "description": "Measures dataType, nullable, and profiling coverage"},
    "rule_coverage":      {"name": "Rule Coverage",      "weight": 0.08, "description": "Measures DQ rule generation coverage"},
    "documentation":      {"name": "Documentation",      "weight": 0.12, "description": "Measures column description coverage and human approval (Tier 2)"},
    "grounding":          {"name": "Grounding",          "weight": 0.10, "description": "Measures the share of rules anchored to external/domain authoritative sources and human-approved (Tier 3)"},
}

DEFAULT_HOST = "localhost"
DEFAULT_BOLT_PORT = 7687
DEFAULT_USERNAME = "neo4j"
DEFAULT_PASSWORD = "your_password"
DEFAULT_DATABASE = "neo4j"


def esc(value) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def cypher_value(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return f"'{esc(v)}'"
    if isinstance(v, float):
        return repr(v)
    return str(v)


# ── Graph queries ────────────────────────────────────────────────────────────

def get_enriched_datasets(session, project_code: str | None = None) -> list[dict]:
    if project_code:
        result = session.run("""
            MATCH (:Project {projectCode: $project_code})
                  -[:HAS_CATALOG]->(cat:Catalog)-[:DCAT_DATASET]->(ds:Dataset)
            WHERE ds.row_count IS NOT NULL
            RETURN ds.uri AS uri, ds.name AS name, ds.schema AS schema,
                   toInteger(ds.row_count) AS row_count,
                   cat.uri AS catalog_uri
            ORDER BY ds.schema, ds.name
        """, project_code=project_code)
    else:
        result = session.run("""
            MATCH (ds:Dataset)
            WHERE ds.row_count IS NOT NULL
            OPTIONAL MATCH (cat:Catalog)-[:DCAT_DATASET]->(ds)
            RETURN ds.uri AS uri, ds.name AS name, ds.schema AS schema,
                   toInteger(ds.row_count) AS row_count,
                   cat.uri AS catalog_uri
            ORDER BY ds.schema, ds.name
        """)
    return [dict(r) for r in result]


def get_columns_with_evidence(session, dataset_uri: str) -> list[dict]:
    """Fetch all columns for a dataset with their measurements, rules, descriptions, FK info, and latest test results."""
    result = session.run("""
        MATCH (ds:Dataset {uri: $uri})-[:HAS_COLUMN]->(col:Column)

        // Measurements
        OPTIONAL MATCH (col)-[:HAS_QUALITY_MEASUREMENT]->(qm:QualityMeasurement)-[:ON_METRIC]->(m:Metric)
        WITH col, ds,
             [x IN collect(DISTINCT {metric: m.name, value: qm.value}) WHERE x.metric IS NOT NULL] AS measurements

        // DQ rules on this column — capture source + status for Grounding
        OPTIONAL MATCH (ps:PropertyShape)-[:ON_COLUMN]->(col)
        WITH col, ds, measurements,
             [x IN collect(DISTINCT {
                ruleType:   ps.ruleType,
                coverage:   ps.coverage,
                ruleSource: coalesce(ps.ruleSource, 'observation'),
                status:     coalesce(ps.status, 'approved')
             }) WHERE x.ruleType IS NOT NULL] AS rules

        // Description (any state — we classify below)
        OPTIONAL MATCH (col)-[:HAS_DESCRIPTION]->(cd:ColumnDescription {isCurrent: true})
        WITH col, ds, measurements, rules, cd

        // FK relationships involving this column
        OPTIONAL MATCH (ds)-[fk:REFERENCES]->(:Dataset)
        WHERE fk.columns CONTAINS col.name
        WITH col, measurements, rules, cd,
             collect(DISTINCT fk.columns) AS fk_cols

        // Test results for this column — latest per ruleType
        OPTIONAL MATCH (tr:TestResult)-[:ON_COLUMN]->(col)
        WITH col, measurements, rules, cd, fk_cols,
             [x IN collect(DISTINCT {ruleType: tr.ruleType, passRate: tr.passRate, passed: tr.passed})
              WHERE x.ruleType IS NOT NULL] AS test_results

        RETURN col.uri        AS uri,
               col.name       AS name,
               col.dataType   AS dataType,
               col.nullable   AS nullable,
               col.primaryKey AS primaryKey,
               col.ordinal    AS ordinal,
               measurements,
               rules,
               test_results,
               CASE WHEN cd IS NULL THEN 'none'
                    WHEN cd.status = 'approved' THEN 'approved'
                    WHEN cd.status = 'rejected' THEN 'rejected'
                    ELSE 'pending'
               END AS desc_status,
               CASE WHEN size(fk_cols) > 0 THEN true ELSE false END AS is_fk
        ORDER BY col.ordinal
    """, uri=dataset_uri)
    return [dict(r) for r in result]


# ── Scoring functions ────────────────────────────────────────────────────────

def meas_dict(measurements: list) -> dict:
    return {m["metric"]: m["value"] for m in measurements if m.get("metric")}


def rules_set(rules: list) -> dict:
    """Return {ruleType: coverage_or_None}."""
    return {r["ruleType"]: r.get("coverage") for r in rules if r.get("ruleType")}


def test_results_by_rule(test_results: list) -> dict:
    """Return {ruleType: passRate} from TestResult entries."""
    by_rule: dict = {}
    for t in test_results or []:
        rt = t.get("ruleType")
        if rt:
            # If multiple per rule type, keep the most recent-ish (query returns distinct).
            by_rule[rt] = float(t.get("passRate") or 0.0)
    return by_rule


# Rule types whose pass rate feeds the Validity dimension.
_VALIDITY_RULE_TYPES = {"allowedValues", "range", "regex", "mandatory"}


def score_completeness(col: dict, meas: dict) -> float:
    null_rate = meas.get("null_rate")
    if null_rate is None:
        return 0.0  # not profiled = unknown
    return max(0.0, 1.0 - float(null_rate))


def score_uniqueness(col: dict, meas: dict, row_count: int, col_rules: dict) -> float:
    # Low-cardinality columns (have allowedValues rule) — uniqueness not applicable
    if "allowedValues" in col_rules:
        return 1.0
    # Non-PK, non-unique columns — no uniqueness expectation
    is_pk = col.get("primaryKey", False)
    has_unique_rule = "unique" in col_rules
    if not is_pk and not has_unique_rule:
        return 1.0
    # PK or unique columns — check ratio
    distinct_count = meas.get("distinct_count")
    if distinct_count is None:
        return 0.0  # not profiled
    if row_count == 0:
        return 1.0
    ratio = float(distinct_count) / float(row_count)
    return min(1.0, ratio)


def score_validity(col: dict, col_rules: dict, test_pass: dict) -> tuple[float, str]:
    """Return (score, evidence). Test pass rates win over profiling-derived coverage."""
    # Tier 1 — real test evidence for any validity rule type
    rates = [test_pass[rt] for rt in _VALIDITY_RULE_TYPES if rt in test_pass]
    if rates:
        return (sum(rates) / len(rates), "test")
    # Baseline fallback — allowedValues coverage from profiling
    if "allowedValues" in col_rules and col_rules["allowedValues"] is not None:
        return (float(col_rules["allowedValues"]), "profile")
    # Range rule exists but untested
    if "range" in col_rules:
        return (1.0, "rule")
    # No validity constraints; assume valid
    return (1.0, "rule")


def score_consistency(col: dict, col_rules: dict, test_pass: dict) -> tuple[float, str]:
    is_fk = col.get("is_fk", False)
    if not is_fk:
        return (1.0, "rule")
    if "referentialIntegrity" in test_pass:
        return (test_pass["referentialIntegrity"], "test")
    if "referentialIntegrity" in col_rules:
        return (1.0, "rule")
    return (0.5, "rule")


def score_schema_conformance(col: dict, meas: dict) -> float:
    total = 0.0
    third = 1.0 / 3.0
    if col.get("dataType"):
        total += third
    if col.get("nullable") is not None:
        total += third
    if len(meas) > 0:
        total += third
    return total


def score_rule_coverage(col: dict, col_rules: dict) -> float:
    return 1.0 if len(col_rules) > 0 else 0.0


def score_documentation(col: dict) -> float:
    status = col.get("desc_status") or "none"
    if status == "approved":
        return 1.0
    if status in ("pending", "draft"):
        return 0.35
    if status == "rejected":
        return 0.10
    return 0.0


_EXTERNAL_SOURCES = {"domain", "external"}


def score_grounding(rules: list) -> float:
    """Fraction of the column's rules that are anchored to external/domain
    authoritative sources AND human-approved. Tier 3 evidence.

    Returns 0.0 when the column has no rules or no external-grounded rules.
    """
    if not rules:
        return 0.0
    total = len(rules)
    grounded = 0
    for r in rules:
        src = (r.get("ruleSource") or "observation").lower()
        status = (r.get("status") or "approved").lower()
        if src in _EXTERNAL_SOURCES and status == "approved":
            grounded += 1
    return grounded / total


def compute_composite(dim_scores: dict) -> float:
    total = 0.0
    for dim_key, score in dim_scores.items():
        if dim_key == "composite":
            continue
        weight = DIMENSIONS[dim_key]["weight"]
        total += score * weight
    return round(total, 6)


def compute_column_scores(col: dict, row_count: int) -> tuple[dict, dict]:
    """Compute scores + per-dimension evidence for one column.

    Returns ({dimension: score}, {dimension: evidence})."""
    meas = meas_dict(col.get("measurements") or [])
    col_rules = rules_set(col.get("rules") or [])
    test_pass = test_results_by_rule(col.get("test_results") or [])

    validity, v_ev = score_validity(col, col_rules, test_pass)
    consistency, c_ev = score_consistency(col, col_rules, test_pass)

    scores = {
        "completeness": round(score_completeness(col, meas), 6),
        "uniqueness": round(score_uniqueness(col, meas, row_count, col_rules), 6),
        "validity": round(validity, 6),
        "consistency": round(consistency, 6),
        "schema_conformance": round(score_schema_conformance(col, meas), 6),
        "rule_coverage": round(score_rule_coverage(col, col_rules), 6),
        "documentation": round(score_documentation(col), 6),
        "grounding": round(score_grounding(col.get("rules") or []), 6),
    }
    scores["composite"] = compute_composite(scores)
    evidence = {
        "completeness": "profile",
        "uniqueness": "profile",
        "validity": v_ev,
        "consistency": c_ev,
        "schema_conformance": "meta",
        "rule_coverage": "rule",
        "documentation": "meta",
        "grounding": "rule",
    }
    return scores, evidence


def aggregate_scores(column_entries: list[tuple[dict, dict]]) -> tuple[dict, dict]:
    """Average column scores + roll up evidence.

    Evidence roll-up rule: 'test' wins if any column had test evidence for the
    dimension, else the most common of 'profile'/'meta'/'rule'."""
    dims = list(DIMENSIONS.keys()) + ["composite"]
    if not column_entries:
        return ({dim: 0.0 for dim in dims}, {d: "rule" for d in DIMENSIONS})

    n = len(column_entries)
    agg: dict = {}
    for dim in dims:
        agg[dim] = round(sum(cs[dim] for cs, _ in column_entries) / n, 6)

    # Evidence roll-up
    ev_agg: dict = {}
    for dim in DIMENSIONS:
        tallies: dict = {}
        for _, ev in column_entries:
            tallies[ev[dim]] = tallies.get(ev[dim], 0) + 1
        if tallies.get("test", 0) > 0:
            ev_agg[dim] = "test"
        else:
            ev_agg[dim] = max(tallies.items(), key=lambda kv: kv[1])[0]
    return agg, ev_agg


# ── Cypher generation ────────────────────────────────────────────────────────

def generate_dimension_cypher() -> list[str]:
    lines = ["// === Dimension Definitions (idempotent) ===", ""]
    for dim_key, dim in DIMENSIONS.items():
        lines.append(
            f"MERGE (d:QualityDimension {{uri: 'dim:{dim_key}'}})"
            f" SET d.name = '{esc(dim['name'])}', d.defaultWeight = {dim['weight']},"
            f" d.description = '{esc(dim['description'])}';",
        )
    lines.append("")
    return lines


def generate_column_score_cypher(col_uri: str, col_name: str, scores: dict,
                                  evidence: dict, batch_id: str, scored_at: str,
                                  schema_table: str) -> list[str]:
    lines = [f"// Column scores: {schema_table}.{col_name}"]

    for dim_key in DIMENSIONS:
        score = scores[dim_key]
        weight = DIMENSIONS[dim_key]["weight"]
        ev = evidence.get(dim_key, "rule")
        uri = f"score:{schema_table}.{col_name}.{dim_key}.{batch_id}"
        lines.append(
            f"MATCH (col:Column {{uri: '{esc(col_uri)}'}})"
            f" MATCH (dim:QualityDimension {{uri: 'dim:{dim_key}'}})"
            f" CREATE (qs:QualityScore {{uri: '{esc(uri)}', level: 'column',"
            f" dimension: '{dim_key}', score: {score}, weight: {weight},"
            f" evidence: '{esc(ev)}',"
            f" scoredAt: datetime('{scored_at}'), batchId: '{esc(batch_id)}'}})"
            f" CREATE (col)-[:HAS_QUALITY_SCORE]->(qs)"
            f" CREATE (qs)-[:SCORED_ON_DIMENSION]->(dim);",
        )

    # Composite
    composite = scores["composite"]
    uri = f"score:{schema_table}.{col_name}.composite.{batch_id}"
    lines.append(
        f"MATCH (col:Column {{uri: '{esc(col_uri)}'}})"
        f" CREATE (qs:QualityScore {{uri: '{esc(uri)}', level: 'column',"
        f" dimension: 'composite', score: {composite}, weight: 1.0,"
        f" scoredAt: datetime('{scored_at}'), batchId: '{esc(batch_id)}'}})"
        f" CREATE (col)-[:HAS_QUALITY_SCORE]->(qs);",
    )
    lines.append("")
    return lines


def generate_dataset_score_cypher(ds_uri: str, schema_table: str,
                                   scores: dict, evidence: dict,
                                   batch_id: str, scored_at: str) -> list[str]:
    lines = [f"// Dataset scores: {schema_table}"]

    for dim_key in DIMENSIONS:
        score = scores[dim_key]
        weight = DIMENSIONS[dim_key]["weight"]
        ev = evidence.get(dim_key, "rule")
        uri = f"score:{schema_table}.{dim_key}.{batch_id}"
        lines.append(
            f"MATCH (ds:Dataset {{uri: '{esc(ds_uri)}'}})"
            f" MATCH (dim:QualityDimension {{uri: 'dim:{dim_key}'}})"
            f" CREATE (qs:QualityScore {{uri: '{esc(uri)}', level: 'dataset',"
            f" dimension: '{dim_key}', score: {score}, weight: {weight},"
            f" evidence: '{esc(ev)}',"
            f" scoredAt: datetime('{scored_at}'), batchId: '{esc(batch_id)}'}})"
            f" CREATE (ds)-[:HAS_QUALITY_SCORE]->(qs)"
            f" CREATE (qs)-[:SCORED_ON_DIMENSION]->(dim);",
        )

    # Composite
    composite = scores["composite"]
    uri = f"score:{schema_table}.composite.{batch_id}"
    lines.append(
        f"MATCH (ds:Dataset {{uri: '{esc(ds_uri)}'}})"
        f" CREATE (qs:QualityScore {{uri: '{esc(uri)}', level: 'dataset',"
        f" dimension: 'composite', score: {composite}, weight: 1.0,"
        f" scoredAt: datetime('{scored_at}'), batchId: '{esc(batch_id)}'}})"
        f" CREATE (ds)-[:HAS_QUALITY_SCORE]->(qs);",
    )
    lines.append("")
    return lines


def generate_overall_score_cypher(catalog_uri: str, catalog_name: str,
                                   scores: dict, evidence: dict,
                                   batch_id: str, scored_at: str) -> list[str]:
    lines = [f"// Overall scores: {catalog_name}"]

    for dim_key in DIMENSIONS:
        score = scores[dim_key]
        weight = DIMENSIONS[dim_key]["weight"]
        ev = evidence.get(dim_key, "rule")
        uri = f"score:{catalog_name}.{dim_key}.{batch_id}"
        lines.append(
            f"MATCH (cat:Catalog {{uri: '{esc(catalog_uri)}'}})"
            f" MATCH (dim:QualityDimension {{uri: 'dim:{dim_key}'}})"
            f" CREATE (qs:QualityScore {{uri: '{esc(uri)}', level: 'overall',"
            f" dimension: '{dim_key}', score: {score}, weight: {weight},"
            f" evidence: '{esc(ev)}',"
            f" scoredAt: datetime('{scored_at}'), batchId: '{esc(batch_id)}'}})"
            f" CREATE (cat)-[:HAS_QUALITY_SCORE]->(qs)"
            f" CREATE (qs)-[:SCORED_ON_DIMENSION]->(dim);",
        )

    # Composite
    composite = scores["composite"]
    uri = f"score:{catalog_name}.composite.{batch_id}"
    lines.append(
        f"MATCH (cat:Catalog {{uri: '{esc(catalog_uri)}'}})"
        f" CREATE (qs:QualityScore {{uri: '{esc(uri)}', level: 'overall',"
        f" dimension: 'composite', score: {composite}, weight: 1.0,"
        f" scoredAt: datetime('{scored_at}'), batchId: '{esc(batch_id)}'}})"
        f" CREATE (cat)-[:HAS_QUALITY_SCORE]->(qs);",
    )
    lines.append("")
    return lines


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate data quality score Cypher from an enriched Neo4j knowledge graph.",
    )
    parser.add_argument(
        "output_file",
        nargs="?",
        default=None,
        help="Output .cypher file path (default: ./cypher_scripts/dq_scores.cypher)",
    )
    parser.add_argument("--project-code", default=None, help="Project code to scope queries to")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--bolt-port", type=int, default=DEFAULT_BOLT_PORT)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    args = parser.parse_args()

    cypher_dir = os.path.join(os.getcwd(), "cypher_scripts")
    os.makedirs(cypher_dir, exist_ok=True)
    output_file = (
        os.path.abspath(args.output_file)
        if args.output_file
        else os.path.join(cypher_dir, "dq_scores.cypher")
    )

    bolt_uri = f"bolt://{args.host}:{args.bolt_port}"
    print(f"Connecting to {bolt_uri} as '{args.username}' (database: {args.database})...")

    try:
        driver = GraphDatabase.driver(bolt_uri, auth=(args.username, args.password))
        driver.verify_connectivity()
    except ServiceUnavailable as e:
        print(f"ERROR: Cannot connect to Neo4j at {bolt_uri}\n  {e}", file=sys.stderr)
        sys.exit(1)
    except AuthError as e:
        print(f"ERROR: Authentication failed\n  {e}", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    scored_at = now.isoformat()
    batch_id = f"batch:{scored_at}"

    lines = [
        "// Data Quality Scores",
        "// Generated by the data-scoring Claude skill",
        f"// Generated at: {scored_at}",
        f"// Batch: {batch_id}",
        "",
    ]

    lines.extend(generate_dimension_cypher())

    stats = {"datasets": 0, "columns": 0, "score_nodes": 0}

    # Collect dataset scores grouped by catalog for overall aggregation
    catalog_datasets: dict[str, list[dict]] = {}  # catalog_uri -> [dataset_scores]

    pc = getattr(args, "project_code", None)
    with driver.session(database=args.database) as session:
        datasets = get_enriched_datasets(session, project_code=pc)
        if not datasets:
            print(
                "ERROR: No enriched datasets found in the graph.\n"
                "Run data-profiling-to-dqv-neo4j first.",
                file=sys.stderr,
            )
            sys.exit(1)

        print(f"Found {len(datasets)} enriched dataset(s):")
        for ds in datasets:
            print(f"  {ds['schema']}.{ds['name']} ({ds['row_count']:,} rows)")
        print()

        pc_prefix = f"{pc}:" if pc else ""
        nodes_per_level = len(DIMENSIONS) + 1  # dimensions + composite
        for ds in datasets:
            schema_table = f"{pc_prefix}{ds['schema']}.{ds['name']}"
            columns = get_columns_with_evidence(session, ds["uri"])

            lines.append(f"// === Table: {schema_table} ===")
            lines.append("")

            column_entries: list[tuple[dict, dict]] = []

            for col in columns:
                col_scores, col_evidence = compute_column_scores(col, ds["row_count"])
                column_entries.append((col_scores, col_evidence))

                lines.extend(generate_column_score_cypher(
                    col["uri"], col["name"], col_scores, col_evidence,
                    batch_id, scored_at, schema_table,
                ))
                stats["score_nodes"] += nodes_per_level

            stats["columns"] += len(columns)

            # Dataset-level aggregate
            ds_scores, ds_evidence = aggregate_scores(column_entries)
            lines.extend(generate_dataset_score_cypher(
                ds["uri"], schema_table, ds_scores, ds_evidence, batch_id, scored_at,
            ))
            stats["score_nodes"] += nodes_per_level
            stats["datasets"] += 1

            # Collect for catalog aggregation
            catalog_uri = ds.get("catalog_uri")
            if catalog_uri:
                catalog_datasets.setdefault(catalog_uri, []).append(
                    (ds_scores, ds_evidence)
                )

    # Overall scores per catalog
    for catalog_uri, ds_entries in catalog_datasets.items():
        # Extract catalog name from URI (e.g. "catalog:employees" -> "employees")
        catalog_name = catalog_uri.replace("catalog:", "") if ":" in catalog_uri else catalog_uri
        overall, overall_ev = aggregate_scores(ds_entries)
        lines.extend(generate_overall_score_cypher(
            catalog_uri, catalog_name, overall, overall_ev, batch_id, scored_at,
        ))
        stats["score_nodes"] += nodes_per_level

    driver.close()

    with open(output_file, "w") as f:
        f.write("\n".join(lines))

    print(f"Generated: {output_file}")
    print(f"  Batch ID:    {batch_id}")
    print(f"  Datasets:    {stats['datasets']}")
    print(f"  Columns:     {stats['columns']}")
    print(f"  Score nodes: {stats['score_nodes']}")
    print(f"  Catalogs:    {len(catalog_datasets)}")


if __name__ == "__main__":
    main()
