#!/usr/bin/env python3
"""Check pipeline progress by querying Neo4j for completion statistics."""

import argparse
import json
import sys

from neo4j import GraphDatabase

# ── Queries ────────────────────────────────────────────────────────────────

DATASET_COUNT = "MATCH (ds:Dataset) RETURN count(ds) AS cnt"
COLUMN_COUNT = "MATCH (col:Column) RETURN count(col) AS cnt"
GRAPH_NODE_COUNT = "MATCH (n) RETURN count(n) AS cnt"
GRAPH_REL_COUNT = "MATCH ()-[r]->() RETURN count(r) AS cnt"

DESCRIPTION_STATS = """\
MATCH (cd:ColumnDescription) WHERE cd.isCurrent = true
RETURN
    count(cd) AS total,
    count(CASE WHEN cd.status = 'approved' THEN 1 END) AS approved,
    count(CASE WHEN cd.status = 'rejected' THEN 1 END) AS rejected,
    count(CASE WHEN cd.status = 'pending_review' THEN 1 END) AS pending
"""

FINAL_DESCRIPTIONS_COUNT = """\
MATCH (col:Column)-[:HAS_DESCRIPTION]->(cd:ColumnDescription {isCurrent: true})
RETURN count(cd) AS cnt
"""

MAPPING_STATS = """\
MATCH (cm:ColumnMapping) WHERE cm.isCurrent = true
RETURN
    count(cm) AS total,
    count(CASE WHEN cm.status = 'approved' THEN 1 END) AS approved,
    count(CASE WHEN cm.status = 'rejected' THEN 1 END) AS rejected,
    count(CASE WHEN cm.status = 'pending_review' THEN 1 END) AS pending
"""

DQ_RULE_COUNT = "MATCH (ps:PropertyShape) RETURN count(ps) AS cnt"

ALLOWED_VALUES_COUNT = """\
MATCH (col:Column)-[:HAS_TOP_VALUE]->(tv:TopValue)
WITH col, collect(tv.value) AS vals, sum(tv.frequency) AS coverage
WHERE coverage > 0.95
RETURN count(col) AS cnt
"""

PLAYBOOK_COUNT = """\
MATCH (d:Domain {name: $domain})-[:HAS_PLAYBOOK]->(pb:Playbook)-[:HAS_ITEM]->(pi:PlaybookItem)
WHERE pi.isCurrent = true
RETURN count(pi) AS cnt
"""

LEARNING_HISTORY_COUNT = """\
MATCH (d:Domain {name: $domain})-[:HAS_PLAYBOOK]->(pb:Playbook)-[:HAS_VERSION]->(pv:PlaybookVersion)
RETURN count(pv) AS cnt
"""


def check_progress(driver, database, domain=None, threshold=0.95):
    """Query Neo4j and compute pipeline progress stats."""
    stats = {}

    with driver.session(database=database) as session:
        stats["datasets"] = session.run(DATASET_COUNT).single()["cnt"]
        stats["columns"] = session.run(COLUMN_COUNT).single()["cnt"]
        stats["graph_nodes"] = session.run(GRAPH_NODE_COUNT).single()["cnt"]
        stats["graph_relationships"] = session.run(GRAPH_REL_COUNT).single()["cnt"]

        desc = session.run(DESCRIPTION_STATS).single()
        desc_total = desc["total"]
        desc_approved = desc["approved"]
        stats["descriptions"] = {
            "total": desc_total,
            "approved": desc_approved,
            "rejected": desc["rejected"],
            "pending": desc["pending"],
            "approval_pct": round(desc_approved / desc_total, 4) if desc_total > 0 else 0.0,
        }

        stats["final_descriptions"] = session.run(FINAL_DESCRIPTIONS_COUNT).single()["cnt"]

        mapping = session.run(MAPPING_STATS).single()
        map_total = mapping["total"]
        map_approved = mapping["approved"]
        stats["mappings"] = {
            "total": map_total,
            "approved": map_approved,
            "rejected": mapping["rejected"],
            "pending": mapping["pending"],
            "approval_pct": round(map_approved / map_total, 4) if map_total > 0 else 0.0,
        }

        stats["dq_rules"] = session.run(DQ_RULE_COUNT).single()["cnt"]
        stats["allowed_values"] = session.run(ALLOWED_VALUES_COUNT).single()["cnt"]

        if domain:
            stats["playbook_items"] = session.run(PLAYBOOK_COUNT, domain=domain).single()["cnt"]
            stats["learning_cycles"] = session.run(LEARNING_HISTORY_COUNT, domain=domain).single()["cnt"]
        else:
            stats["playbook_items"] = 0
            stats["learning_cycles"] = 0

    # Compute readiness
    blockers = []
    desc_pct = stats["descriptions"]["approval_pct"]
    map_pct = stats["mappings"]["approval_pct"]

    if stats["descriptions"]["total"] > 0 and desc_pct < threshold:
        blockers.append(f"descriptions approval at {desc_pct:.0%} (threshold: {threshold:.0%})")
    if stats["mappings"]["total"] > 0 and map_pct < threshold:
        blockers.append(f"mappings approval at {map_pct:.0%} (threshold: {threshold:.0%})")
    if stats["descriptions"]["pending"] > 0:
        blockers.append(f"{stats['descriptions']['pending']} descriptions pending review")
    if stats["mappings"]["pending"] > 0:
        blockers.append(f"{stats['mappings']['pending']} mappings pending review")

    stats["threshold"] = threshold
    stats["ready_for_next_stage"] = len(blockers) == 0
    stats["blockers"] = blockers

    return stats


def main():
    parser = argparse.ArgumentParser(description="Check pipeline progress from Neo4j knowledge graph")
    parser.add_argument("--host", default="localhost", help="Neo4j host (default: localhost)")
    parser.add_argument("--bolt-port", type=int, default=7687, help="Neo4j Bolt port (default: 7687)")
    parser.add_argument("--username", default="neo4j", help="Neo4j username (default: neo4j)")
    parser.add_argument("--password", required=True, help="Neo4j password")
    parser.add_argument("--database", default="neo4j", help="Neo4j database (default: neo4j)")
    parser.add_argument("--domain", default=None, help="Domain name for playbook stats")
    parser.add_argument("--threshold", type=float, default=0.95, help="Approval threshold for readiness (default: 0.95)")
    parser.add_argument("--output", default=None, help="Output JSON file path (default: stdout)")
    parser.add_argument("--dry-run", action="store_true", help="Print queries without executing")
    args = parser.parse_args()

    if args.dry_run:
        print("Queries that would be executed:")
        for name, q in [
            ("DATASET_COUNT", DATASET_COUNT), ("COLUMN_COUNT", COLUMN_COUNT),
            ("DESCRIPTION_STATS", DESCRIPTION_STATS), ("MAPPING_STATS", MAPPING_STATS),
            ("DQ_RULE_COUNT", DQ_RULE_COUNT), ("ALLOWED_VALUES_COUNT", ALLOWED_VALUES_COUNT),
        ]:
            print(f"\n-- {name}\n{q.strip()}")
        if args.domain:
            print(f"\n-- PLAYBOOK_COUNT (domain={args.domain})\n{PLAYBOOK_COUNT.strip()}")
        return

    uri = f"bolt://{args.host}:{args.bolt_port}"
    driver = GraphDatabase.driver(uri, auth=(args.username, args.password))

    try:
        stats = check_progress(driver, args.database, args.domain, args.threshold)
        output = json.dumps(stats, indent=2)
        if args.output:
            with open(args.output, "w") as f:
                f.write(output)
            print(f"Progress report written to {args.output}")
        else:
            print(output)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
