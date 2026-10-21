#!/usr/bin/env python3
"""
Analyze data quality issues by comparing profiling measurements against domain rules.

Queries the Neo4j knowledge graph for:
  - Domain rules (ruleSource='domain', status='approved') and their thresholds
  - Profiling measurements for the same columns (min, max, null_rate)
  - Current quality scores

Produces:
  - remediation/analysis_report.json  (structured report)
  - remediation/analysis_report.md    (human-readable markdown)

Usage:
    python analyze_issues.py [options]

Options:
    --host        Neo4j host (default: localhost)
    --bolt-port   Bolt port (default: 7687)
    --username    Neo4j username (default: neo4j)
    --password    Neo4j password (default: your_password)
    --database    Neo4j database name (default: neo4j)
    --pg-connection  PostgreSQL connection string (for row count queries)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

try:
    from neo4j import GraphDatabase
    from neo4j.exceptions import ServiceUnavailable, AuthError
except ImportError:
    print("ERROR: neo4j driver required. Install with: pip install neo4j", file=sys.stderr)
    sys.exit(1)

DEFAULT_HOST = "localhost"
DEFAULT_BOLT_PORT = 7687
DEFAULT_USERNAME = "neo4j"
DEFAULT_PASSWORD = "your_password"
DEFAULT_DATABASE = "neo4j"


# ── Graph queries ────────────────────────────────────────────────────────────

DOMAIN_RULES_WITH_EVIDENCE = """\
MATCH (ds:Dataset)-[:HAS_SHAPE]->(:NodeShape)-[:PROPERTY]->(ps:PropertyShape)-[:ON_COLUMN]->(col:Column)
WHERE ps.ruleSource = 'domain' AND COALESCE(ps.status, 'approved') IN ['approved', 'pending_review']

// Get profiling measurements for this column
OPTIONAL MATCH (col)-[:HAS_QUALITY_MEASUREMENT]->(qm:QualityMeasurement)-[:ON_METRIC]->(m:Metric)
WITH ds, col, ps,
     [x IN collect(DISTINCT {metric: m.name, value: qm.value}) WHERE x.metric IS NOT NULL] AS measurements

// Get current quality scores for this column (latest batch)
OPTIONAL MATCH (col)-[:HAS_QUALITY_SCORE]->(qs:QualityScore)
WHERE qs.level = 'column' AND qs.dimension = 'composite'
WITH ds, col, ps, measurements, qs
ORDER BY qs.scoredAt DESC
WITH ds, col, ps, measurements, head(collect(qs)) AS latest_score

RETURN ds.uri AS dataset_uri, ds.schema AS schema, ds.name AS table_name,
       toInteger(ds.row_count) AS row_count,
       col.uri AS col_uri, col.name AS col_name, col.dataType AS data_type,
       ps.uri AS rule_uri, ps.ruleType AS rule_type,
       ps.description AS rule_description, ps.severity AS severity,
       ps.minInclusive AS rule_min, ps.maxInclusive AS rule_max,
       ps.status AS rule_status,
       measurements,
       latest_score.score AS current_composite
ORDER BY ds.schema, ds.name, col.name, ps.ruleType
"""


def meas_dict(measurements: list) -> dict:
    return {m["metric"]: m["value"] for m in measurements if m.get("metric")}


def analyze_rule(rule: dict, meas: dict, row_count: int) -> dict | None:
    """Analyze a single domain rule against profiling evidence. Returns issue dict or None."""
    rule_type = rule["rule_type"]
    col_name = rule["col_name"]
    data_type = rule.get("data_type", "")

    if rule_type == "range":
        rule_min = rule.get("rule_min")
        rule_max = rule.get("rule_max")
        actual_min = meas.get("min")
        actual_max = meas.get("max")

        if actual_min is None or actual_max is None:
            return None  # No profiling data to compare

        violations = []
        estimated_rows = 0

        # Check if actual values exceed domain bounds
        is_date = data_type and ("date" in data_type.lower() or "timestamp" in data_type.lower())

        if rule_max is not None and actual_max is not None:
            try:
                if float(str(actual_max)) > float(str(rule_max)):
                    issue_type = "future_dates" if is_date else "out_of_range"
                    violations.append(f"max value {actual_max} exceeds domain max {rule_max}")
                    # Rough estimate: assume uniform distribution beyond bounds
                    estimated_rows = max(1, int(row_count * 0.01))  # conservative 1%
            except (ValueError, TypeError):
                # String comparison for dates
                if str(actual_max) > str(rule_max):
                    issue_type = "future_dates" if is_date else "out_of_range"
                    violations.append(f"max value {actual_max} exceeds domain max {rule_max}")
                    estimated_rows = max(1, int(row_count * 0.01))

        if rule_min is not None and actual_min is not None:
            try:
                if float(str(actual_min)) < float(str(rule_min)):
                    violations.append(f"min value {actual_min} below domain min {rule_min}")
                    estimated_rows = max(estimated_rows, max(1, int(row_count * 0.01)))
            except (ValueError, TypeError):
                if str(actual_min) < str(rule_min):
                    violations.append(f"min value {actual_min} below domain min {rule_min}")
                    estimated_rows = max(estimated_rows, max(1, int(row_count * 0.01)))

        if not violations:
            return None

        issue_type = violations[0].split()[0] if "future" in violations[0] else "out_of_range"
        description = f"{col_name}: {'; '.join(violations)}"

        # Build remediation SQL
        sql_parts = []
        schema_table = f"{rule['schema']}.{rule['table_name']}"
        if rule_max is not None and any("exceeds" in v for v in violations):
            sql_parts.append(
                f"UPDATE {schema_table} SET {col_name} = NULL WHERE {col_name} > '{rule_max}';"
            )
        if rule_min is not None and any("below" in v for v in violations):
            sql_parts.append(
                f"UPDATE {schema_table} SET {col_name} = NULL WHERE {col_name} < '{rule_min}';"
            )

        return {
            "table": schema_table,
            "column": col_name,
            "data_type": data_type,
            "issue_type": issue_type,
            "description": description,
            "domain_rule": f"range {rule_min} to {rule_max}",
            "actual_min": str(actual_min) if actual_min is not None else None,
            "actual_max": str(actual_max) if actual_max is not None else None,
            "estimated_rows_affected": estimated_rows,
            "severity": rule.get("severity", "sh:Warning"),
            "remediation": {
                "action": "set_null",
                "description": f"Set {col_name} to NULL where outside domain range",
                "sql": "\n".join(sql_parts),
            },
        }

    # Check for high null rate on mandatory columns
    null_rate = meas.get("null_rate")
    if null_rate is not None and float(null_rate) > 0.2:
        estimated_nulls = int(row_count * float(null_rate))
        return {
            "table": f"{rule['schema']}.{rule['table_name']}",
            "column": col_name,
            "data_type": data_type,
            "issue_type": "high_null_rate",
            "description": f"{col_name}: {float(null_rate)*100:.1f}% null rate ({estimated_nulls:,} rows)",
            "domain_rule": rule.get("rule_description", ""),
            "null_rate": float(null_rate),
            "estimated_rows_affected": estimated_nulls,
            "severity": rule.get("severity", "sh:Warning"),
            "remediation": {
                "action": "manual_review",
                "description": "Flag for manual review — do not auto-fix nulls",
                "sql": None,
            },
        }

    return None


def generate_markdown(report: dict) -> str:
    """Generate a human-readable markdown report."""
    lines = [
        "# Data Remediation Analysis Report",
        "",
        f"**Generated:** {report['generated_at']}",
        "",
        "## Summary",
        "",
        f"- **Tables analyzed:** {report['summary']['tables_analyzed']}",
        f"- **Columns with issues:** {report['summary']['columns_with_issues']}",
        f"- **Total issues found:** {report['summary']['total_issues']}",
        "",
        "### Issues by Type",
        "",
        "| Type | Count |",
        "|------|-------|",
    ]
    for itype, count in report["summary"]["by_type"].items():
        lines.append(f"| {itype} | {count} |")

    lines += ["", "## Detailed Issues", ""]

    for i, issue in enumerate(report["issues"], 1):
        auto = issue["remediation"]["action"] != "manual_review"
        tag = "Auto-remediable" if auto else "Manual review"
        lines += [
            f"### {i}. {issue['table']}.{issue['column']}",
            "",
            f"- **Type:** {issue['issue_type']}",
            f"- **Severity:** {issue['severity']}",
            f"- **Description:** {issue['description']}",
            f"- **Domain rule:** {issue['domain_rule']}",
            f"- **Estimated rows affected:** {issue['estimated_rows_affected']:,}",
            f"- **Category:** {tag}",
        ]
        if issue["remediation"]["sql"]:
            lines += [
                "",
                "**Recommended SQL:**",
                "```sql",
                issue["remediation"]["sql"],
                "```",
            ]
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Analyze data quality issues from domain rules vs profiling evidence.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--bolt-port", type=int, default=DEFAULT_BOLT_PORT)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--pg-connection", default="", help="PostgreSQL connection string (for future use)")
    args = parser.parse_args()

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

    issues = []
    tables_seen = set()

    with driver.session(database=args.database) as session:
        rows = [dict(r) for r in session.run(DOMAIN_RULES_WITH_EVIDENCE)]

    driver.close()

    if not rows:
        print("No domain rules found in the graph. Run domain-rule-enhancement first.")
        # Still produce an empty report
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": {"tables_analyzed": 0, "columns_with_issues": 0, "total_issues": 0, "by_type": {}},
            "issues": [],
        }
    else:
        print(f"Found {len(rows)} domain rule(s) to check...")
        for row in rows:
            tables_seen.add(f"{row['schema']}.{row['table_name']}")
            meas = meas_dict(row.get("measurements") or [])
            issue = analyze_rule(row, meas, row.get("row_count") or 0)
            if issue:
                # Add current score info
                issue["current_composite"] = row.get("current_composite")
                issues.append(issue)

        # Build type counts
        by_type: dict[str, int] = {}
        cols_with_issues = set()
        for issue in issues:
            by_type[issue["issue_type"]] = by_type.get(issue["issue_type"], 0) + 1
            cols_with_issues.add(f"{issue['table']}.{issue['column']}")

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "tables_analyzed": len(tables_seen),
                "columns_with_issues": len(cols_with_issues),
                "total_issues": len(issues),
                "by_type": by_type,
            },
            "issues": issues,
        }

    # Write outputs
    out_dir = os.path.join(os.getcwd(), "remediation")
    os.makedirs(out_dir, exist_ok=True)

    json_path = os.path.join(out_dir, "analysis_report.json")
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    md_path = os.path.join(out_dir, "analysis_report.md")
    with open(md_path, "w") as f:
        f.write(generate_markdown(report))

    print(f"\nAnalysis complete:")
    print(f"  Tables analyzed: {report['summary']['tables_analyzed']}")
    print(f"  Issues found:    {report['summary']['total_issues']}")
    for itype, count in report["summary"].get("by_type", {}).items():
        print(f"    {itype}: {count}")
    print(f"\n  JSON report: {json_path}")
    print(f"  Markdown report: {md_path}")


if __name__ == "__main__":
    main()
