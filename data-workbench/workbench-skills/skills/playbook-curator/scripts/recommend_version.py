#!/usr/bin/env python3
"""Recommend baseline vs refined playbook version for a domain."""

import argparse
import json

from neo4j import GraphDatabase

PLAYBOOK_STATE = """\
MATCH (d:Domain {name: $domain})-[:HAS_PLAYBOOK]->(pb:Playbook)
OPTIONAL MATCH (pb)-[:HAS_ITEM]->(pi:PlaybookItem {isCurrent: true})
OPTIONAL MATCH (pb)-[:HAS_VERSION]->(pv:PlaybookVersion)
WITH pb,
     count(DISTINCT pi) AS item_count,
     CASE WHEN size(collect(DISTINCT pv)) > 0
          THEN reduce(max = 0, v IN collect(DISTINCT pv.version) | CASE WHEN v > max THEN v ELSE max END)
          ELSE 0 END AS latest_version,
     CASE WHEN size(collect(DISTINCT pv)) > 1 THEN true ELSE false END AS has_refined,
     [v IN collect(DISTINCT pv) WHERE v.version = reduce(max = 0, x IN collect(DISTINCT pv.version) | CASE WHEN x > max THEN x ELSE max END) | v.summary][0] AS latest_summary
RETURN
    pb.phase AS phase,
    item_count,
    latest_version,
    has_refined,
    latest_summary
"""


def recommend(driver, database, domain):
    """Analyze playbook state and produce a recommendation."""
    with driver.session(database=database) as session:
        result = [dict(r) for r in session.run(PLAYBOOK_STATE, domain=domain)]

    if not result:
        return {
            "recommendation": "baseline",
            "version": 1,
            "rationale": f"No playbooks found for domain '{domain}'. Using baseline (default) playbook.",
        }

    has_any_refined = any(r["has_refined"] for r in result)
    total_items = sum(r["item_count"] for r in result)
    max_version = max(r["latest_version"] for r in result) if result else 0

    if not has_any_refined:
        return {
            "recommendation": "baseline",
            "version": 1,
            "rationale": f"Domain '{domain}' has {total_items} playbook items but no reflection cycles have been run. Using baseline.",
        }

    summaries = [r["latest_summary"] for r in result if r.get("latest_summary")]
    summary_text = summaries[0] if summaries else f"Refined through {max_version - 1} reflection cycle(s)"

    return {
        "recommendation": "refined",
        "version": max_version,
        "rationale": (
            f"Refined playbook (v{max_version}) has {total_items} items "
            f"across {max_version - 1} reflection cycle(s). "
            f"Latest summary: {summary_text}"
        ),
    }


def main():
    parser = argparse.ArgumentParser(description="Recommend baseline vs refined playbook version")
    parser.add_argument("--domain", required=True, help="Domain name")
    parser.add_argument("--host", default="localhost", help="Neo4j host (default: localhost)")
    parser.add_argument("--bolt-port", type=int, default=7687, help="Neo4j Bolt port (default: 7687)")
    parser.add_argument("--username", default="neo4j", help="Neo4j username (default: neo4j)")
    parser.add_argument("--password", required=True, help="Neo4j password")
    parser.add_argument("--database", default="neo4j", help="Neo4j database (default: neo4j)")
    parser.add_argument("--dry-run", action="store_true", help="Print queries without executing")
    args = parser.parse_args()

    if args.dry_run:
        print(f"Domain: {args.domain}")
        print(f"\n-- PLAYBOOK_STATE\n{PLAYBOOK_STATE.strip()}")
        return

    uri = f"bolt://{args.host}:{args.bolt_port}"
    driver = GraphDatabase.driver(uri, auth=(args.username, args.password))

    try:
        rec = recommend(driver, args.database, args.domain)
        print(json.dumps(rec, indent=2))
    finally:
        driver.close()


if __name__ == "__main__":
    main()
