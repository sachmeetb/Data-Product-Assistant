#!/usr/bin/env python3
"""
Estimate the score impact of candidate remediations.

Reads the analysis_report.json produced by analyze_issues.py, queries current
scores from Neo4j, and calculates estimated scores if remediations are applied.
Appends a score_estimation section to the JSON report and updates the markdown.

Usage:
    python estimate_scores.py --report remediation/analysis_report.json [options]

Options:
    --host        Neo4j host (default: localhost)
    --bolt-port   Bolt port (default: 7687)
    --username    Neo4j username (default: neo4j)
    --password    Neo4j password (default: your_password)
    --database    Neo4j database name (default: neo4j)
    --report      Path to analysis_report.json
"""

import argparse
import json
import os
import sys

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

# Dimension weights (must match the scoring skill)
WEIGHTS = {
    "completeness": 0.25,
    "uniqueness": 0.20,
    "validity": 0.20,
    "consistency": 0.15,
    "schema_conformance": 0.10,
    "rule_coverage": 0.10,
}

CURRENT_SCORES_QUERY = """\
MATCH (ds:Dataset)-[:HAS_QUALITY_SCORE]->(qs:QualityScore)
WHERE qs.level = 'dataset'
WITH ds, qs.batchId AS batchId, max(qs.scoredAt) AS latest
ORDER BY latest DESC
WITH ds, head(collect(batchId)) AS latestBatch
MATCH (ds)-[:HAS_QUALITY_SCORE]->(qs:QualityScore {batchId: latestBatch})
WHERE qs.level = 'dataset'
RETURN ds.uri AS dataset_uri, ds.schema + '.' + ds.name AS dataset_name,
       qs.dimension AS dimension, qs.score AS score
ORDER BY ds.schema, ds.name, qs.dimension
"""

OVERALL_SCORES_QUERY = """\
MATCH (cat:Catalog)-[:HAS_QUALITY_SCORE]->(qs:QualityScore)
WHERE qs.level = 'overall'
WITH cat, qs.batchId AS batchId, max(qs.scoredAt) AS latest
ORDER BY latest DESC
WITH cat, head(collect(batchId)) AS latestBatch
MATCH (cat)-[:HAS_QUALITY_SCORE]->(qs:QualityScore {batchId: latestBatch})
WHERE qs.level = 'overall'
RETURN qs.dimension AS dimension, qs.score AS score
"""


def estimate_impact(issues: list, current_scores: dict) -> dict:
    """Estimate score improvements from applying remediations.

    For each dataset with issues, calculate how the validity and completeness
    dimensions would change if the remediation SQL is applied.
    """
    by_dataset: dict[str, dict] = {}

    for issue in issues:
        table = issue["table"]
        if table not in by_dataset:
            by_dataset[table] = {
                "issues": [],
                "current": current_scores.get(table, {}),
            }
        by_dataset[table]["issues"].append(issue)

    estimation = {"by_dataset": []}

    for table, data in by_dataset.items():
        current = data["current"]
        current_composite = current.get("composite", 0.0)
        current_validity = current.get("validity", 0.0)
        current_completeness = current.get("completeness", 1.0)

        # Estimate validity improvement: each fixed out-of-range/future-date issue
        # improves validity proportionally
        remediable_issues = [i for i in data["issues"] if i["remediation"]["action"] != "manual_review"]
        total_issues = len(data["issues"])
        remediable_count = len(remediable_issues)

        if total_issues > 0 and current_validity < 1.0:
            # Assume validity improves by the proportion of issues fixed
            validity_gap = 1.0 - current_validity
            fix_fraction = remediable_count / max(total_issues, 1)
            est_validity = current_validity + validity_gap * fix_fraction * 0.8  # conservative 80% improvement
        else:
            est_validity = current_validity

        # Completeness may decrease slightly (setting bad values to NULL)
        total_rows_nulled = sum(i.get("estimated_rows_affected", 0) for i in remediable_issues)
        row_count = max(1, sum(i.get("estimated_rows_affected", 0) * 100 for i in remediable_issues))  # rough total rows
        if current_completeness > 0 and total_rows_nulled > 0:
            # Small completeness decrease from nulling bad values
            null_fraction = total_rows_nulled / max(row_count, 1)
            est_completeness = max(0.0, current_completeness - null_fraction * 0.5)
        else:
            est_completeness = current_completeness

        # Recalculate estimated composite with updated dimensions
        est_dims = dict(current)
        est_dims["validity"] = round(min(1.0, est_validity), 4)
        est_dims["completeness"] = round(est_completeness, 4)
        est_composite = sum(est_dims.get(dim, 0.0) * w for dim, w in WEIGHTS.items())
        est_composite = round(est_composite, 4)

        delta = est_composite - current_composite
        estimation["by_dataset"].append({
            "dataset": table,
            "current_composite": round(current_composite, 4),
            "estimated_composite": est_composite,
            "delta": f"+{delta*100:.1f}%" if delta > 0 else f"{delta*100:.1f}%",
            "current_validity": round(current_validity, 4),
            "estimated_validity": round(est_dims["validity"], 4),
            "current_completeness": round(current_completeness, 4),
            "estimated_completeness": round(est_dims["completeness"], 4),
            "remediable_issues": remediable_count,
            "manual_review_issues": total_issues - remediable_count,
        })

    # Overall estimation
    if estimation["by_dataset"]:
        avg_current = sum(d["current_composite"] for d in estimation["by_dataset"]) / len(estimation["by_dataset"])
        avg_estimated = sum(d["estimated_composite"] for d in estimation["by_dataset"]) / len(estimation["by_dataset"])
        overall_delta = avg_estimated - avg_current
        estimation["current_overall"] = round(avg_current, 4)
        estimation["estimated_overall"] = round(avg_estimated, 4)
        estimation["delta"] = f"+{overall_delta*100:.1f}%" if overall_delta > 0 else f"{overall_delta*100:.1f}%"
    else:
        estimation["current_overall"] = 0.0
        estimation["estimated_overall"] = 0.0
        estimation["delta"] = "+0.0%"

    return estimation


def append_markdown_estimation(md_path: str, estimation: dict):
    """Append score estimation section to the markdown report."""
    lines = [
        "",
        "## Score Impact Estimation",
        "",
        f"**Current overall score:** {estimation['current_overall']*100:.1f}%",
        f"**Estimated after remediation:** {estimation['estimated_overall']*100:.1f}%",
        f"**Expected improvement:** {estimation['delta']}",
        "",
        "### Per Dataset",
        "",
        "| Dataset | Current | Estimated | Delta | Remediable | Manual Review |",
        "|---------|---------|-----------|-------|------------|---------------|",
    ]
    for ds in estimation["by_dataset"]:
        lines.append(
            f"| {ds['dataset']} | {ds['current_composite']*100:.1f}% | "
            f"{ds['estimated_composite']*100:.1f}% | {ds['delta']} | "
            f"{ds['remediable_issues']} | {ds['manual_review_issues']} |"
        )
    lines.append("")

    with open(md_path, "a") as f:
        f.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description="Estimate score impact of candidate remediations.")
    parser.add_argument("--report", required=True, help="Path to analysis_report.json")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--bolt-port", type=int, default=DEFAULT_BOLT_PORT)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    args = parser.parse_args()

    # Load analysis report
    with open(args.report) as f:
        report = json.load(f)

    if not report.get("issues"):
        print("No issues in report — nothing to estimate.")
        report["score_estimation"] = {
            "current_overall": 0.0, "estimated_overall": 0.0, "delta": "+0.0%", "by_dataset": [],
        }
        with open(args.report, "w") as f:
            json.dump(report, f, indent=2, default=str)
        return

    # Query current scores from Neo4j
    bolt_uri = f"bolt://{args.host}:{args.bolt_port}"
    print(f"Querying current scores from {bolt_uri}...")

    try:
        driver = GraphDatabase.driver(bolt_uri, auth=(args.username, args.password))
        driver.verify_connectivity()
    except (ServiceUnavailable, AuthError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    current_scores: dict[str, dict] = {}  # dataset_name -> {dimension: score}
    with driver.session(database=args.database) as session:
        for row in session.run(CURRENT_SCORES_QUERY):
            ds_name = row["dataset_name"]
            if ds_name not in current_scores:
                current_scores[ds_name] = {}
            current_scores[ds_name][row["dimension"]] = row["score"]

    driver.close()

    # Estimate impact
    estimation = estimate_impact(report["issues"], current_scores)
    report["score_estimation"] = estimation

    # Write updated report
    with open(args.report, "w") as f:
        json.dump(report, f, indent=2, default=str)

    # Append to markdown
    md_path = args.report.replace(".json", ".md")
    if os.path.exists(md_path):
        append_markdown_estimation(md_path, estimation)

    print(f"\nScore Impact Estimation:")
    print(f"  Current overall:   {estimation['current_overall']*100:.1f}%")
    print(f"  Estimated overall: {estimation['estimated_overall']*100:.1f}%")
    print(f"  Expected delta:    {estimation['delta']}")
    for ds in estimation["by_dataset"]:
        print(f"    {ds['dataset']}: {ds['current_composite']*100:.1f}% -> {ds['estimated_composite']*100:.1f}% ({ds['delta']})")


if __name__ == "__main__":
    main()
