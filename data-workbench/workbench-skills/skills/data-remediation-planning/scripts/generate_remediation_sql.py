#!/usr/bin/env python3
"""
Generate per-table SQL remediation scripts from the analysis report.

Reads analysis_report.json and creates SQL files in remediation/sql/ for each
table that has auto-remediable issues. Manual-review issues are skipped.

Usage:
    python generate_remediation_sql.py --report remediation/analysis_report.json [--issues 0,1,3]

Options:
    --report   Path to analysis_report.json
    --issues   Comma-separated list of issue indices to include (default: all remediable)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone


def main():
    parser = argparse.ArgumentParser(description="Generate SQL remediation scripts from analysis report.")
    parser.add_argument("--report", required=True, help="Path to analysis_report.json")
    parser.add_argument("--issues", default=None, help="Comma-separated issue indices to include (default: all remediable)")
    args = parser.parse_args()

    with open(args.report) as f:
        report = json.load(f)

    issues = report.get("issues", [])
    if not issues:
        print("No issues in report — nothing to generate.")
        return

    # Filter to selected issues or all remediable
    if args.issues:
        selected_indices = {int(i.strip()) for i in args.issues.split(",")}
        selected = [issues[i] for i in selected_indices if i < len(issues)]
    else:
        selected = [i for i in issues if i["remediation"]["action"] != "manual_review"]

    if not selected:
        print("No remediable issues selected.")
        return

    # Group by table
    by_table: dict[str, list[dict]] = {}
    for issue in selected:
        table = issue["table"]
        by_table.setdefault(table, []).append(issue)

    sql_dir = os.path.join(os.path.dirname(args.report), "sql")
    os.makedirs(sql_dir, exist_ok=True)

    generated_at = datetime.now(timezone.utc).isoformat()
    files_created = []

    for table, table_issues in by_table.items():
        safe_name = table.replace(".", "_")
        filename = f"{safe_name}_remediation.sql"
        filepath = os.path.join(sql_dir, filename)

        lines = [
            f"-- Remediation SQL for {table}",
            f"-- Generated: {generated_at}",
            f"-- Issues addressed: {len(table_issues)}",
            "",
            "BEGIN;",
            "",
        ]

        for issue in table_issues:
            sql = issue["remediation"].get("sql")
            if sql:
                lines += [
                    f"-- Issue: {issue['description']}",
                    f"-- Type: {issue['issue_type']}",
                    f"-- Estimated rows: {issue['estimated_rows_affected']}",
                    sql,
                    "",
                ]

        lines += [
            "COMMIT;",
            "",
        ]

        with open(filepath, "w") as f:
            f.write("\n".join(lines))

        files_created.append(filepath)
        print(f"  Created: {filepath} ({len(table_issues)} fixes)")

    print(f"\nGenerated {len(files_created)} SQL file(s) in {sql_dir}/")
    for f in files_created:
        print(f"  {f}")


if __name__ == "__main__":
    main()
