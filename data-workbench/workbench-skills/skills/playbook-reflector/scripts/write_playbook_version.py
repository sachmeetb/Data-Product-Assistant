#!/usr/bin/env python3
"""Write a PlaybookVersion node to Neo4j after a reflection cycle.

Reads a reflection_summary.json file, determines the next version number,
and creates a :PlaybookVersion node linked via [:HAS_VERSION] to each
:Playbook that had changes.
"""

import argparse
import json
import sys

from neo4j import GraphDatabase

CREATE_VERSION = """\
MATCH (d:Domain {name: $domain})-[:HAS_PLAYBOOK]->(pb:Playbook)
OPTIONAL MATCH (pb)-[:HAS_VERSION]->(existing:PlaybookVersion)
WITH pb, COALESCE(MAX(existing.version), 0) + 1 AS nextVersion
CREATE (pv:PlaybookVersion {
    version: nextVersion,
    summary: $summary,
    createdAt: datetime(),
    itemsAdded: $items_added,
    itemsUpdated: $items_updated,
    itemsRemoved: $items_removed
})
CREATE (pb)-[:HAS_VERSION]->(pv)
RETURN pb.phase AS phase, nextVersion AS version
"""


def write_version(driver, database, domain, summary_path):
    """Read reflection summary and create PlaybookVersion nodes."""
    with open(summary_path) as f:
        summary = json.load(f)

    narrative = summary.get("narrative", "Reflection cycle completed")
    items_added = summary.get("items_added", 0)
    items_updated = summary.get("items_updated", 0)
    items_removed = summary.get("items_removed", 0)

    with driver.session(database=database) as session:
        result = session.run(
            CREATE_VERSION,
            domain=domain,
            summary=narrative,
            items_added=items_added,
            items_updated=items_updated,
            items_removed=items_removed,
        )
        versions = [dict(r) for r in result]

    return versions


def main():
    parser = argparse.ArgumentParser(
        description="Write PlaybookVersion node to Neo4j from reflection_summary.json"
    )
    parser.add_argument("summary_file", help="Path to reflection_summary.json")
    parser.add_argument("--domain", required=True, help="Domain name")
    parser.add_argument("--host", default="localhost", help="Neo4j host (default: localhost)")
    parser.add_argument("--bolt-port", type=int, default=7687, help="Neo4j Bolt port (default: 7687)")
    parser.add_argument("--username", default="neo4j", help="Neo4j username (default: neo4j)")
    parser.add_argument("--password", required=True, help="Neo4j password")
    parser.add_argument("--database", default="neo4j", help="Neo4j database (default: neo4j)")
    parser.add_argument("--dry-run", action="store_true", help="Print query without executing")
    args = parser.parse_args()

    if args.dry_run:
        with open(args.summary_file) as f:
            summary = json.load(f)
        print(f"Domain: {args.domain}")
        print(f"Summary: {summary}")
        print(f"\n-- CREATE_VERSION\n{CREATE_VERSION.strip()}")
        return

    uri = f"bolt://{args.host}:{args.bolt_port}"
    driver = GraphDatabase.driver(uri, auth=(args.username, args.password))

    try:
        versions = write_version(driver, args.database, args.domain, args.summary_file)
        if versions:
            for v in versions:
                print(f"Created PlaybookVersion v{v['version']} for phase: {v['phase']}")
        else:
            print("No playbook nodes found for domain — version not created")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
