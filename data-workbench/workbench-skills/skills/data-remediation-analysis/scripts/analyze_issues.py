#!/usr/bin/env python3
"""
Analyze data quality issues across all columns in a project.

Queries the Neo4j knowledge graph for:
  - ALL columns with their profiling measurements, rules, and descriptions
  - Compares actual data against domain rules and data quality expectations
  - Identifies completeness, validity, rule coverage, and schema conformance issues

Issue types detected:
  out_of_range     — value exceeds domain rule bounds (auto-remediable)
  future_dates     — timestamp in the future (auto-remediable)
  high_null_rate   — column has >20% nulls (manual review)
  invalid_values   — values outside allowed set (informational)
  no_rule_coverage — column has no DQ rules (informational)
  missing_description — column lacks approved description (informational)

Produces:
  - remediation/analysis_report.json  (structured report)
  - remediation/analysis_report.md    (human-readable markdown)

Usage:
    python analyze_issues.py [options]

Options:
    --host            Neo4j host (default: localhost)
    --bolt-port       Bolt port (default: 7687)
    --username        Neo4j username (default: neo4j)
    --password        Neo4j password (default: your_password)
    --database        Neo4j database name (default: neo4j)
    --project-code    Project code to scope queries to
    --pg-connection   PostgreSQL connection string (for future use)
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

NULL_RATE_THRESHOLD = 0.20  # 20%+ null rate = issue

# ── Graph queries ────────────────────────────────────────────────────────────

ALL_COLUMNS_QUERY = """\
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)
OPTIONAL MATCH (col)-[:HAS_QUALITY_MEASUREMENT]->(qm:QualityMeasurement)-[:ON_METRIC]->(m:Metric)
WITH ds, col,
     [x IN collect(DISTINCT {metric: m.name, value: qm.value}) WHERE x.metric IS NOT NULL] AS measurements
OPTIONAL MATCH (ps:PropertyShape)-[:ON_COLUMN]->(col)
WITH ds, col, measurements,
     [x IN collect(DISTINCT {ruleType: ps.ruleType, ruleSource: ps.ruleSource,
      coverage: ps.coverage, minInclusive: ps.minInclusive, maxInclusive: ps.maxInclusive,
      status: ps.status, description: ps.description, severity: ps.severity})
      WHERE x.ruleType IS NOT NULL] AS rules
OPTIONAL MATCH (col)-[:HAS_DESCRIPTION]->(cd:ColumnDescription {isCurrent: true, status: 'approved'})
RETURN ds.uri AS dataset_uri, ds.schema AS schema, ds.name AS table_name,
       toInteger(ds.row_count) AS row_count,
       col.uri AS col_uri, col.name AS col_name, col.dataType AS data_type,
       col.nullable AS nullable,
       measurements, rules,
       CASE WHEN cd IS NOT NULL THEN true ELSE false END AS has_approved_desc
ORDER BY ds.schema, ds.name, col.name
"""

ALL_COLUMNS_QUERY_SCOPED = """\
MATCH (:Project {projectCode: $project_code})
      -[:HAS_CATALOG]->(:Catalog)-[:DCAT_DATASET]->(ds:Dataset)
      -[:HAS_COLUMN]->(col:Column)
OPTIONAL MATCH (col)-[:HAS_QUALITY_MEASUREMENT]->(qm:QualityMeasurement)-[:ON_METRIC]->(m:Metric)
WITH ds, col,
     [x IN collect(DISTINCT {metric: m.name, value: qm.value}) WHERE x.metric IS NOT NULL] AS measurements
OPTIONAL MATCH (ps:PropertyShape)-[:ON_COLUMN]->(col)
WITH ds, col, measurements,
     [x IN collect(DISTINCT {ruleType: ps.ruleType, ruleSource: ps.ruleSource,
      coverage: ps.coverage, minInclusive: ps.minInclusive, maxInclusive: ps.maxInclusive,
      status: ps.status, description: ps.description, severity: ps.severity})
      WHERE x.ruleType IS NOT NULL] AS rules
OPTIONAL MATCH (col)-[:HAS_DESCRIPTION]->(cd:ColumnDescription {isCurrent: true, status: 'approved'})
RETURN ds.uri AS dataset_uri, ds.schema AS schema, ds.name AS table_name,
       toInteger(ds.row_count) AS row_count,
       col.uri AS col_uri, col.name AS col_name, col.dataType AS data_type,
       col.nullable AS nullable,
       measurements, rules,
       CASE WHEN cd IS NOT NULL THEN true ELSE false END AS has_approved_desc
ORDER BY ds.schema, ds.name, col.name
"""


def meas_dict(measurements: list) -> dict:
    return {m["metric"]: m["value"] for m in measurements if m.get("metric")}


def analyze_column(col: dict) -> list[dict]:
    """Analyze a single column for all issue types. Returns list of issues."""
    issues = []
    schema_table = f"{col['schema']}.{col['table_name']}"
    col_name = col["col_name"]
    data_type = col.get("data_type", "")
    row_count = col.get("row_count") or 0
    meas = meas_dict(col.get("measurements") or [])
    rules = col.get("rules") or []
    has_desc = col.get("has_approved_desc", False)
    nullable = col.get("nullable", True)

    domain_rules = [r for r in rules if r.get("ruleSource") == "domain"]
    all_rule_types = {r["ruleType"] for r in rules}

    # ── 1. Range violations (domain rules) ──────────────────────────────────
    for rule in domain_rules:
        if rule["ruleType"] != "range":
            continue
        rule_min = rule.get("minInclusive")
        rule_max = rule.get("maxInclusive")
        actual_min = meas.get("min")
        actual_max = meas.get("max")

        if actual_min is None and actual_max is None:
            continue

        is_date = data_type and ("date" in data_type.lower() or "timestamp" in data_type.lower())
        violations = []

        if rule_max is not None and actual_max is not None:
            try:
                exceeds = float(str(actual_max)) > float(str(rule_max))
            except (ValueError, TypeError):
                exceeds = str(actual_max) > str(rule_max)
            if exceeds:
                violations.append(f"max {actual_max} exceeds domain max {rule_max}")

        if rule_min is not None and actual_min is not None:
            try:
                below = float(str(actual_min)) < float(str(rule_min))
            except (ValueError, TypeError):
                below = str(actual_min) < str(rule_min)
            if below:
                violations.append(f"min {actual_min} below domain min {rule_min}")

        if violations:
            issue_type = "future_dates" if is_date and "exceeds" in violations[0] else "out_of_range"
            sql_parts = []
            if any("exceeds" in v for v in violations):
                sql_parts.append(f"UPDATE {schema_table} SET {col_name} = NULL WHERE {col_name} > '{rule_max}';")
            if any("below" in v for v in violations):
                sql_parts.append(f"UPDATE {schema_table} SET {col_name} = NULL WHERE {col_name} < '{rule_min}';")

            issues.append({
                "table": schema_table, "column": col_name, "data_type": data_type,
                "issue_type": issue_type,
                "description": f"{col_name}: {'; '.join(violations)}",
                "domain_rule": f"range {rule_min} to {rule_max}",
                "severity": rule.get("severity", "sh:Warning"),
                "estimated_rows_affected": max(1, int(row_count * 0.01)),
                "remediation": {
                    "action": "set_null",
                    "description": f"Set {col_name} to NULL where outside domain range",
                    "sql": "\n".join(sql_parts),
                },
            })

    # ── 2. Completeness issues (high null rate) ─────────────────────────────
    null_rate = meas.get("null_rate")
    if null_rate is not None:
        null_rate_f = float(null_rate)
        if null_rate_f >= NULL_RATE_THRESHOLD:
            estimated_nulls = int(row_count * null_rate_f)
            issues.append({
                "table": schema_table, "column": col_name, "data_type": data_type,
                "issue_type": "high_null_rate",
                "description": f"{col_name}: {null_rate_f*100:.1f}% null rate ({estimated_nulls:,} rows)",
                "domain_rule": "completeness expectation",
                "severity": "sh:Warning" if not nullable else "sh:Info",
                "estimated_rows_affected": estimated_nulls,
                "remediation": {
                    "action": "manual_review",
                    "description": "Flag for manual review — do not auto-fix nulls",
                    "sql": None,
                },
            })

    # ── 3. AllowedValues violations ─────────────────────────────────────────
    for rule in domain_rules:
        if rule["ruleType"] != "allowedValues":
            continue
        coverage = rule.get("coverage")
        if coverage is not None and float(coverage) < 0.95:
            issues.append({
                "table": schema_table, "column": col_name, "data_type": data_type,
                "issue_type": "invalid_values",
                "description": f"{col_name}: only {float(coverage)*100:.1f}% of values match allowed set",
                "domain_rule": rule.get("description", "allowedValues constraint"),
                "severity": rule.get("severity", "sh:Info"),
                "estimated_rows_affected": int(row_count * (1 - float(coverage))) if row_count else 0,
                "remediation": {
                    "action": "manual_review",
                    "description": "Review values not in the allowed set",
                    "sql": None,
                },
            })

    # ── 4. No rule coverage ─────────────────────────────────────────────────
    if not rules:
        issues.append({
            "table": schema_table, "column": col_name, "data_type": data_type,
            "issue_type": "no_rule_coverage",
            "description": f"{col_name}: no DQ rules defined for this column",
            "domain_rule": "",
            "severity": "sh:Info",
            "estimated_rows_affected": 0,
            "remediation": {
                "action": "informational",
                "description": "Consider adding domain rules for this column",
                "sql": None,
            },
        })

    # ── 5. Missing description ──────────────────────────────────────────────
    if not has_desc:
        issues.append({
            "table": schema_table, "column": col_name, "data_type": data_type,
            "issue_type": "missing_description",
            "description": f"{col_name}: no approved column description",
            "domain_rule": "",
            "severity": "sh:Info",
            "estimated_rows_affected": 0,
            "remediation": {
                "action": "informational",
                "description": "Run metadata enrichment to generate a description",
                "sql": None,
            },
        })

    return issues


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
        f"- **Columns analyzed:** {report['summary']['columns_analyzed']}",
        f"- **Total issues found:** {report['summary']['total_issues']}",
        f"- **Auto-remediable:** {report['summary']['auto_remediable']}",
        f"- **Manual review:** {report['summary']['manual_review']}",
        f"- **Informational:** {report['summary']['informational']}",
        "",
        "### Issues by Type",
        "",
        "| Type | Count | Category |",
        "|------|-------|----------|",
    ]
    categories = {
        "out_of_range": "Auto-remediable",
        "future_dates": "Auto-remediable",
        "high_null_rate": "Manual review",
        "invalid_values": "Manual review",
        "no_rule_coverage": "Informational",
        "missing_description": "Informational",
    }
    for itype, count in report["summary"]["by_type"].items():
        lines.append(f"| {itype} | {count} | {categories.get(itype, '')} |")

    # Auto-remediable issues first
    auto_issues = [i for i in report["issues"] if i["remediation"]["action"] not in ("manual_review", "informational")]
    manual_issues = [i for i in report["issues"] if i["remediation"]["action"] == "manual_review"]
    info_issues = [i for i in report["issues"] if i["remediation"]["action"] == "informational"]

    if auto_issues:
        lines += ["", "## Auto-Remediable Issues", ""]
        for i, issue in enumerate(auto_issues, 1):
            lines += [
                f"### {i}. {issue['table']}.{issue['column']}",
                "",
                f"- **Type:** {issue['issue_type']}",
                f"- **Severity:** {issue['severity']}",
                f"- **Description:** {issue['description']}",
                f"- **Domain rule:** {issue['domain_rule']}",
                f"- **Estimated rows:** {issue['estimated_rows_affected']:,}",
                "",
                "**Recommended SQL:**",
                "```sql",
                issue["remediation"]["sql"],
                "```",
                "",
            ]

    if manual_issues:
        lines += ["", "## Issues Requiring Manual Review", ""]
        for i, issue in enumerate(manual_issues, 1):
            lines += [
                f"### {i}. {issue['table']}.{issue['column']}",
                "",
                f"- **Type:** {issue['issue_type']}",
                f"- **Severity:** {issue['severity']}",
                f"- **Description:** {issue['description']}",
                f"- **Estimated rows:** {issue['estimated_rows_affected']:,}",
                "",
            ]

    if info_issues:
        lines += ["", "## Informational (Governance Gaps)", ""]
        # Group by type for cleaner display
        by_type: dict[str, list] = {}
        for issue in info_issues:
            by_type.setdefault(issue["issue_type"], []).append(issue)

        for itype, items in by_type.items():
            lines += [f"### {itype} ({len(items)} columns)", ""]
            for item in items[:10]:  # Show first 10
                lines.append(f"- {item['table']}.{item['column']}: {item['description']}")
            if len(items) > 10:
                lines.append(f"- ... and {len(items) - 10} more")
            lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Analyze data quality issues from graph evidence.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--bolt-port", type=int, default=DEFAULT_BOLT_PORT)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--project-code", default=None, help="Project code to scope queries to")
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

    pc = getattr(args, "project_code", None)
    with driver.session(database=args.database) as session:
        if pc:
            print(f"Scoping to project: {pc}")
            columns = [dict(r) for r in session.run(ALL_COLUMNS_QUERY_SCOPED, project_code=pc)]
        else:
            columns = [dict(r) for r in session.run(ALL_COLUMNS_QUERY)]
    driver.close()

    if not columns:
        print("No columns found in the graph.")
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": {"tables_analyzed": 0, "columns_analyzed": 0, "total_issues": 0,
                         "auto_remediable": 0, "manual_review": 0, "informational": 0, "by_type": {}},
            "issues": [],
        }
    else:
        tables_seen = set()
        all_issues = []

        print(f"Analyzing {len(columns)} column(s)...")
        for col in columns:
            tables_seen.add(f"{col['schema']}.{col['table_name']}")
            col_issues = analyze_column(col)
            all_issues.extend(col_issues)

        by_type: dict[str, int] = {}
        auto_count = 0
        manual_count = 0
        info_count = 0
        for issue in all_issues:
            by_type[issue["issue_type"]] = by_type.get(issue["issue_type"], 0) + 1
            action = issue["remediation"]["action"]
            if action == "manual_review":
                manual_count += 1
            elif action == "informational":
                info_count += 1
            else:
                auto_count += 1

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "tables_analyzed": len(tables_seen),
                "columns_analyzed": len(columns),
                "total_issues": len(all_issues),
                "auto_remediable": auto_count,
                "manual_review": manual_count,
                "informational": info_count,
                "by_type": by_type,
            },
            "issues": all_issues,
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

    s = report["summary"]
    print(f"\nAnalysis complete:")
    print(f"  Tables analyzed:  {s['tables_analyzed']}")
    print(f"  Columns analyzed: {s['columns_analyzed']}")
    print(f"  Total issues:     {s['total_issues']}")
    print(f"    Auto-remediable: {s['auto_remediable']}")
    print(f"    Manual review:   {s['manual_review']}")
    print(f"    Informational:   {s['informational']}")
    for itype, count in s.get("by_type", {}).items():
        print(f"      {itype}: {count}")
    print(f"\n  JSON report: {json_path}")
    print(f"  Markdown report: {md_path}")


if __name__ == "__main__":
    main()
