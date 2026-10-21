#!/usr/bin/env python3
"""Query playbook versions and items for a domain from Neo4j."""

import argparse
import json

from neo4j import GraphDatabase

PLAYBOOK_SUMMARY = """\
MATCH (d:Domain {name: $domain})-[:HAS_PLAYBOOK]->(pb:Playbook)
OPTIONAL MATCH (pb)-[:HAS_ITEM]->(pi:PlaybookItem {isCurrent: true})
OPTIONAL MATCH (pb)-[:HAS_VERSION]->(pv:PlaybookVersion)
WITH pb, count(DISTINCT pi) AS item_count, collect(DISTINCT pv {
    .version, .summary, .createdAt, .itemsAdded, .itemsUpdated, .itemsRemoved
}) AS versions
RETURN
    pb.phase AS phase,
    item_count,
    CASE WHEN size(versions) > 0
         THEN reduce(max = 0, v IN versions | CASE WHEN v.version > max THEN v.version ELSE max END)
         ELSE 0 END AS latest_version,
    CASE WHEN size(versions) > 1 THEN true ELSE false END AS has_refined,
    versions
ORDER BY pb.phase
"""


def query_versions(driver, database, domain):
    """Query all playbook versions for a domain."""
    with driver.session(database=database) as session:
        result = session.run(PLAYBOOK_SUMMARY, domain=domain)
        phases = []
        for record in result:
            versions = []
            for v in record["versions"]:
                entry = {
                    "version": v.get("version", 0),
                    "summary": v.get("summary", ""),
                    "items_added": v.get("itemsAdded", 0),
                    "items_updated": v.get("itemsUpdated", 0),
                    "items_removed": v.get("itemsRemoved", 0),
                }
                created = v.get("createdAt")
                entry["created_at"] = str(created) if created else None
                versions.append(entry)
            # Sort by version descending
            versions.sort(key=lambda x: x["version"], reverse=True)
            phases.append({
                "phase": record["phase"],
                "item_count": record["item_count"],
                "latest_version": record["latest_version"],
                "has_refined": record["has_refined"],
                "versions": versions,
            })
        return {"domain": domain, "phases": phases}


def main():
    parser = argparse.ArgumentParser(description="Query playbook versions for a domain")
    parser.add_argument("--domain", required=True, help="Domain name")
    parser.add_argument("--host", default="localhost", help="Neo4j host (default: localhost)")
    parser.add_argument("--bolt-port", type=int, default=7687, help="Neo4j Bolt port (default: 7687)")
    parser.add_argument("--username", default="neo4j", help="Neo4j username (default: neo4j)")
    parser.add_argument("--password", required=True, help="Neo4j password")
    parser.add_argument("--database", default="neo4j", help="Neo4j database (default: neo4j)")
    parser.add_argument("--output", default=None, help="Output JSON file (default: stdout)")
    parser.add_argument("--dry-run", action="store_true", help="Print queries without executing")
    args = parser.parse_args()

    if args.dry_run:
        print(f"Domain: {args.domain}")
        print(f"\n-- PLAYBOOK_SUMMARY\n{PLAYBOOK_SUMMARY.strip()}")
        return

    uri = f"bolt://{args.host}:{args.bolt_port}"
    driver = GraphDatabase.driver(uri, auth=(args.username, args.password))

    try:
        data = query_versions(driver, args.database, args.domain)
        output = json.dumps(data, indent=2, default=str)
        if args.output:
            with open(args.output, "w") as f:
                f.write(output)
            print(f"Playbook status written to {args.output}")
        else:
            print(output)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
