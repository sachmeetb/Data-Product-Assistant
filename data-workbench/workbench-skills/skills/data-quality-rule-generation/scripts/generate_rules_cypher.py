#!/usr/bin/env python3
"""
Generate SHACL-inspired data quality rule Cypher from an enriched Neo4j knowledge graph.

Queries the graph built by data-discovery-to-dcat-neo4j and data-profiling-to-dqv-neo4j,
then generates :NodeShape and :PropertyShape nodes representing data quality rules.

Rule types generated:
  mandatory            — null_rate = 0 + nullable = false
  range                — numeric/date column within observed min/max bounds
  unique               — distinct_count / row_count >= 0.99
  allowedValues        — top values cover >= 95% of data
  referentialIntegrity — FK column must reference an existing row in the target table

Thresholds are baked-in defaults stored as evidence properties on the generated rule nodes,
making them queryable and auditable directly from the graph.

Usage:
    python generate_rules_cypher.py [output_file] [options]

    output_file   path for the output .cypher file (default: ./dq_rules.cypher)

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
from datetime import datetime

try:
    from neo4j import GraphDatabase
    from neo4j.exceptions import ServiceUnavailable, AuthError
except ImportError:
    print("ERROR: neo4j driver is required. Install with: pip install neo4j", file=sys.stderr)
    sys.exit(1)

# ── Thresholds ────────────────────────────────────────────────────────────────
UNIQUENESS_RATIO_THRESHOLD = 0.99
ALLOWED_VALUES_COVERAGE_THRESHOLD = 0.95

# Column data types for which a value range rule is meaningful
NUMERIC_TYPES = {
    "bigint", "integer", "int", "int2", "int4", "int8",
    "numeric", "decimal", "real", "double precision", "float",
    "smallint", "serial", "bigserial",
}
DATE_TYPES = {
    "date", "timestamp", "timestamp without time zone", "timestamp with time zone",
}

DEFAULT_HOST = "localhost"
DEFAULT_BOLT_PORT = 7687
DEFAULT_USERNAME = "neo4j"
DEFAULT_PASSWORD = "your_password"
DEFAULT_DATABASE = "neo4j"


def esc(value: str) -> str:
    return str(value).replace("'", "\\'")


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


def is_numeric(data_type: str) -> bool:
    return (data_type or "").lower().split("(")[0].strip() in NUMERIC_TYPES


def is_date(data_type: str) -> bool:
    return (data_type or "").lower().split("(")[0].strip() in DATE_TYPES


# ── Graph queries ─────────────────────────────────────────────────────────────

def get_enriched_datasets(session, project_code: str | None = None) -> list[dict]:
    if project_code:
        result = session.run("""
            MATCH (:Project {projectCode: $project_code})
                  -[:HAS_CATALOG]->(:Catalog)-[:DCAT_DATASET]->(ds:Dataset)
            WHERE ds.row_count IS NOT NULL
            RETURN ds.uri AS uri, ds.name AS name, ds.schema AS schema,
                   toInteger(ds.row_count) AS row_count
            ORDER BY ds.schema, ds.name
        """, project_code=project_code)
    else:
        result = session.run("""
            MATCH (ds:Dataset)
            WHERE ds.row_count IS NOT NULL
            RETURN ds.uri AS uri, ds.name AS name, ds.schema AS schema,
                   toInteger(ds.row_count) AS row_count
            ORDER BY ds.schema, ds.name
        """)
    return [dict(r) for r in result]


def get_columns(session, dataset_uri: str) -> list[dict]:
    result = session.run("""
        MATCH (ds:Dataset {uri: $uri})-[:HAS_COLUMN]->(col:Column)
        OPTIONAL MATCH (col)-[:HAS_QUALITY_MEASUREMENT]->(qm:QualityMeasurement)-[:ON_METRIC]->(m:Metric)
        WITH col,
             [x IN collect({metric: m.name, value: qm.value}) WHERE x.metric IS NOT NULL] AS measurements
        OPTIONAL MATCH (col)-[:HAS_TOP_VALUE]->(tv:TopValue)
        WITH col, measurements,
             [x IN collect({value: tv.value, count: tv.count, frequency: tv.frequency})
              WHERE x.value IS NOT NULL] AS top_values
        RETURN col.uri       AS uri,
               col.name      AS name,
               col.dataType  AS dataType,
               col.nullable  AS nullable,
               col.primaryKey AS primaryKey,
               col.ordinal   AS ordinal,
               measurements,
               top_values
        ORDER BY col.ordinal
    """, uri=dataset_uri)
    return [dict(r) for r in result]


def get_fks(session, dataset_uri: str) -> list[dict]:
    result = session.run("""
        MATCH (ds:Dataset {uri: $uri})-[r:REFERENCES]->(ref:Dataset)
        RETURN r.columns           AS columns,
               r.referencedColumns AS ref_columns,
               ref.uri             AS ref_uri,
               r.constraintName    AS constraint_name
    """, uri=dataset_uri)
    return [dict(r) for r in result]


# ── Rule generation ───────────────────────────────────────────────────────────

def meas_dict(measurements: list) -> dict:
    return {m["metric"]: m["value"] for m in measurements if m.get("metric")}


def generate_rules(dataset: dict, columns: list[dict], fks: list[dict], project_code: str | None = None) -> list[dict]:
    """Derive rule descriptors from graph evidence for one table."""
    rules = []
    schema = dataset["schema"]
    table = dataset["name"]
    row_count = max(dataset["row_count"] or 1, 1)
    schema_table = f"{schema}.{table}"
    pc = project_code
    pc_prefix = f"{pc}:" if pc else ""

    for col in columns:
        col_name = col["name"]
        col_uri = col["uri"]
        data_type = col.get("dataType") or ""
        nullable = col.get("nullable", True)
        is_pk = col.get("primaryKey", False)
        meas = meas_dict(col.get("measurements") or [])
        top_values = col.get("top_values") or []

        # 1. Mandatory
        null_rate = meas.get("null_rate")
        if null_rate is not None and null_rate == 0.0 and not nullable:
            rules.append({
                "ruleType": "mandatory",
                "uri": f"rule:{pc_prefix}{schema_table}.{col_name}.mandatory",
                "col_uri": col_uri,
                "path": col_name,
                "severity": "sh:Violation",
                "confidence": 1.0,
                "description": f"{col_name} must not be null (null_rate=0, nullable=false)",
                "evidenceNullRate": 0.0,
            })

        # 2. Range (numeric and date columns only)
        min_val = meas.get("min")
        max_val = meas.get("max")
        if min_val is not None and max_val is not None and (is_numeric(data_type) or is_date(data_type)):
            rules.append({
                "ruleType": "range",
                "uri": f"rule:{pc_prefix}{schema_table}.{col_name}.range",
                "col_uri": col_uri,
                "path": col_name,
                "severity": "sh:Warning",
                "confidence": 0.95,
                "description": f"{col_name} should be between {min_val} and {max_val}",
                "minInclusive": min_val,
                "maxInclusive": max_val,
                "evidenceMin": min_val,
                "evidenceMax": max_val,
            })

        # 3. Uniqueness
        distinct_count = meas.get("distinct_count")
        if distinct_count is not None:
            ratio = distinct_count / row_count
            if ratio >= UNIQUENESS_RATIO_THRESHOLD:
                rules.append({
                    "ruleType": "unique",
                    "uri": f"rule:{pc_prefix}{schema_table}.{col_name}.unique",
                    "col_uri": col_uri,
                    "path": col_name,
                    "severity": "sh:Violation" if is_pk else "sh:Warning",
                    "confidence": round(ratio, 6),
                    "description": f"{col_name} values should be unique (ratio={ratio:.4f})",
                    "uniquenessRatio": round(ratio, 6),
                    "uniquenessThreshold": UNIQUENESS_RATIO_THRESHOLD,
                    "evidenceDistinctCount": int(distinct_count),
                    "evidenceRowCount": row_count,
                })

        # 4. Allowed values
        if top_values:
            coverage = sum(tv.get("frequency") or 0.0 for tv in top_values)
            if coverage >= ALLOWED_VALUES_COVERAGE_THRESHOLD:
                rules.append({
                    "ruleType": "allowedValues",
                    "uri": f"rule:{pc_prefix}{schema_table}.{col_name}.allowedValues",
                    "col_uri": col_uri,
                    "path": col_name,
                    "severity": "sh:Violation",
                    "confidence": round(coverage, 6),
                    "description": f"{col_name} should be one of {len(top_values)} known values",
                    "coverage": round(coverage, 6),
                    "coverageThreshold": ALLOWED_VALUES_COVERAGE_THRESHOLD,
                    "evidenceValueCount": len(top_values),
                })

    # 5. Referential integrity (one rule per FK column pair)
    for fk in fks:
        fk_cols = [c.strip() for c in (fk.get("columns") or "").split(",") if c.strip()]
        ref_cols = [c.strip() for c in (fk.get("ref_columns") or "").split(",") if c.strip()]
        ref_uri = fk.get("ref_uri") or ""
        constraint = fk.get("constraint_name") or ""

        for fk_col, ref_col in zip(fk_cols, ref_cols):
            rules.append({
                "ruleType": "referentialIntegrity",
                "uri": f"rule:{pc_prefix}{schema_table}.{fk_col}.refIntegrity",
                "col_uri": f"column:{pc_prefix}{schema_table}.{fk_col}",
                "path": fk_col,
                "severity": "sh:Violation",
                "confidence": 1.0,
                "description": f"{fk_col} must reference a valid {ref_uri} ({ref_col})",
                "referencedDatasetUri": ref_uri,
                "referencedColumn": ref_col,
                "constraintName": constraint,
            })

    return rules


# ── Cypher generation ─────────────────────────────────────────────────────────

def rule_to_cypher_lines(ns_uri: str, rule: dict) -> list[str]:
    """Render one rule descriptor as Cypher statement lines."""
    rule_type = rule["ruleType"]
    rule_uri = rule["uri"]
    col_uri = rule["col_uri"]

    # Build PropertyShape property map
    props = [
        f"uri: '{esc(rule_uri)}'",
        f"ruleType: '{rule_type}'",
        f"path: '{esc(rule['path'])}'",
        f"severity: '{rule['severity']}'",
        f"confidence: {cypher_value(rule['confidence'])}",
        f"description: '{esc(rule['description'])}'",
    ]
    if rule_type == "mandatory":
        props.append(f"evidenceNullRate: {cypher_value(rule['evidenceNullRate'])}")
    elif rule_type == "range":
        props += [
            f"minInclusive: {cypher_value(rule['minInclusive'])}",
            f"maxInclusive: {cypher_value(rule['maxInclusive'])}",
            f"evidenceMin: {cypher_value(rule['evidenceMin'])}",
            f"evidenceMax: {cypher_value(rule['evidenceMax'])}",
        ]
    elif rule_type == "unique":
        props += [
            f"uniquenessRatio: {cypher_value(rule['uniquenessRatio'])}",
            f"uniquenessThreshold: {cypher_value(rule['uniquenessThreshold'])}",
            f"evidenceDistinctCount: {rule['evidenceDistinctCount']}",
            f"evidenceRowCount: {rule['evidenceRowCount']}",
        ]
    elif rule_type == "allowedValues":
        props += [
            f"coverage: {cypher_value(rule['coverage'])}",
            f"coverageThreshold: {cypher_value(rule['coverageThreshold'])}",
            f"evidenceValueCount: {rule['evidenceValueCount']}",
        ]
    elif rule_type == "referentialIntegrity":
        props += [
            f"referencedDataset: '{esc(rule['referencedDatasetUri'])}'",
            f"referencedColumn: '{esc(rule['referencedColumn'])}'",
        ]
        if rule.get("constraintName"):
            props.append(f"constraintName: '{esc(rule['constraintName'])}'")

    props_str = ", ".join(props)
    lines = [f"// Rule: {rule['path']} — {rule_type}"]

    if rule_type == "referentialIntegrity":
        lines += [
            f"MATCH (ns:NodeShape {{uri: '{esc(ns_uri)}'}})",
            f"MATCH (col:Column {{uri: '{esc(col_uri)}'}})",
            f"MATCH (ref:Dataset {{uri: '{esc(rule['referencedDatasetUri'])}'}})",
            f"CREATE (ps:PropertyShape {{{props_str}}})",
            f"CREATE (ns)-[:PROPERTY]->(ps)",
            f"CREATE (ps)-[:ON_COLUMN]->(col)",
            f"CREATE (ps)-[:REFERENCES_DATASET]->(ref);",
        ]
    elif rule_type == "allowedValues":
        lines += [
            f"MATCH (ns:NodeShape {{uri: '{esc(ns_uri)}'}})",
            f"MATCH (col:Column {{uri: '{esc(col_uri)}'}})",
            f"CREATE (ps:PropertyShape {{{props_str}}})",
            f"CREATE (ns)-[:PROPERTY]->(ps)",
            f"CREATE (ps)-[:ON_COLUMN]->(col);",
            f"// Link allowed values for {rule['path']}",
            f"MATCH (ps:PropertyShape {{uri: '{esc(rule_uri)}'}})",
            f"MATCH (col:Column {{uri: '{esc(col_uri)}'}})-[:HAS_TOP_VALUE]->(tv:TopValue)",
            f"CREATE (ps)-[:ALLOWED_VALUE]->(tv);",
        ]
    else:
        lines += [
            f"MATCH (ns:NodeShape {{uri: '{esc(ns_uri)}'}})",
            f"MATCH (col:Column {{uri: '{esc(col_uri)}'}})",
            f"CREATE (ps:PropertyShape {{{props_str}}})",
            f"CREATE (ns)-[:PROPERTY]->(ps)",
            f"CREATE (ps)-[:ON_COLUMN]->(col);",
        ]

    lines.append("")
    return lines


def dataset_to_cypher_lines(dataset: dict, rules: list[dict], project_code: str | None = None) -> list[str]:
    schema = dataset["schema"]
    table = dataset["name"]
    schema_table = f"{schema}.{table}"
    ds_uri = dataset["uri"]
    pc_prefix = f"{project_code}:" if project_code else ""
    ns_uri = f"shape:{pc_prefix}{schema_table}"

    lines = [
        f"// === Table: {schema_table} ===",
        "",
        f"// NodeShape for {schema_table}",
        f"MATCH (ds:Dataset {{uri: '{esc(ds_uri)}'}})",
        f"CREATE (ns:NodeShape {{uri: '{esc(ns_uri)}', name: '{esc(table)}', schema: '{esc(schema)}', dcatType: 'sh:NodeShape'}})",
        f"CREATE (ds)-[:HAS_SHAPE]->(ns);",
        "",
    ]

    for rule in rules:
        lines.extend(rule_to_cypher_lines(ns_uri, rule))

    return lines


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate data quality rule Cypher from an enriched Neo4j knowledge graph.",
    )
    parser.add_argument(
        "output_file",
        nargs="?",
        default=None,
        help="Output .cypher file path (default: ./dq_rules.cypher)",
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
        else os.path.join(cypher_dir, "dq_rules.cypher")
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

    lines = [
        "// Data Quality Rules — SHACL-inspired Knowledge Graph",
        "// Generated by the data-quality-rule-generation Claude skill",
        f"// Generated at: {datetime.utcnow().replace(microsecond=0).isoformat()}",
        "// Rule types: mandatory, range, unique, allowedValues, referentialIntegrity",
        "",
    ]

    stats: dict = {"datasets": 0, "rules": 0, "by_type": {}}

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

        for ds in datasets:
            columns = get_columns(session, ds["uri"])
            fks = get_fks(session, ds["uri"])
            rules = generate_rules(ds, columns, fks, project_code=pc)

            lines.extend(dataset_to_cypher_lines(ds, rules, project_code=pc))
            lines.append("")

            stats["datasets"] += 1
            stats["rules"] += len(rules)
            for r in rules:
                rt = r["ruleType"]
                stats["by_type"][rt] = stats["by_type"].get(rt, 0) + 1

    driver.close()

    with open(output_file, "w") as f:
        f.write("\n".join(lines))

    print(f"Generated: {output_file}")
    print(f"  Datasets: {stats['datasets']}")
    print(f"  Rules:    {stats['rules']}")
    for rt, count in sorted(stats["by_type"].items()):
        print(f"    {rt}: {count}")


if __name__ == "__main__":
    main()
