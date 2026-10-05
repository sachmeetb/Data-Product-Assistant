#!/usr/bin/env python3
"""Load playbook items JSON into Neo4j as PlaybookItem nodes linked to a Domain.

Usage:
  python scripts/write_playbook_items.py playbook/playbook_items.json \
      --domain 'Human Resources' --host localhost --bolt-port 7687 \
      --username neo4j --password pw --database neo4j
"""

import argparse
import json
import sys
from datetime import datetime, timezone

from neo4j import GraphDatabase


# Ensure Domain and Playbook nodes exist
ENSURE_DOMAIN_PLAYBOOK = """\
MERGE (d:Domain {name: $domain})
MERGE (d)-[:HAS_PLAYBOOK]->(pb:Playbook {phase: $phase})
ON CREATE SET pb.createdAt = datetime()
RETURN pb
"""

# Add a new PlaybookItem
ADD_ITEM = """\
MATCH (d:Domain {name: $domain})-[:HAS_PLAYBOOK]->(pb:Playbook {phase: $phase})
CREATE (pi:PlaybookItem {
    uri:       $uri,
    rule:      $rule,
    version:   1,
    isCurrent: true,
    createdAt: datetime()
})
CREATE (pb)-[:HAS_ITEM]->(pi)
CREATE (act:ProvActivity {
    uri:          $act_uri,
    activityType: 'playbook_curation',
    operation:    'add',
    rationale:    $rationale,
    occurredAt:   $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:ai:reflector-curator'})
ON CREATE SET agent.agentType = 'ai', agent.name = 'reflector-curator'
CREATE (pi)-[:PROV_WAS_GENERATED_BY]->(act)
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
RETURN pi.uri AS uri
"""

# Link evidence activities to a curation ProvActivity
LINK_EVIDENCE = """\
MATCH (act:ProvActivity {uri: $act_uri})
MATCH (ev:ProvActivity {uri: $ev_uri})
CREATE (act)-[:PROV_INFORMED_BY]->(ev)
"""

# Update an existing PlaybookItem (retire old, create new version)
UPDATE_ITEM = """\
MATCH (pi_old:PlaybookItem {uri: $predecessor_uri, isCurrent: true})
SET pi_old.isCurrent = false
WITH pi_old
MATCH (pb:Playbook)-[:HAS_ITEM]->(pi_old)
CREATE (pi_new:PlaybookItem {
    uri:       $uri,
    rule:      $rule,
    version:   pi_old.version + 1,
    isCurrent: true,
    createdAt: datetime()
})
CREATE (pb)-[:HAS_ITEM]->(pi_new)
CREATE (pi_new)-[:PROV_WAS_DERIVED_FROM]->(pi_old)
CREATE (act:ProvActivity {
    uri:          $act_uri,
    activityType: 'playbook_curation',
    operation:    'update',
    rationale:    $rationale,
    occurredAt:   $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:ai:reflector-curator'})
ON CREATE SET agent.agentType = 'ai', agent.name = 'reflector-curator'
CREATE (pi_new)-[:PROV_WAS_GENERATED_BY]->(act)
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
RETURN pi_new.uri AS uri
"""

# Delete (retire) an existing PlaybookItem
DELETE_ITEM = """\
MATCH (pi:PlaybookItem {uri: $predecessor_uri, isCurrent: true})
SET pi.isCurrent = false
WITH pi
CREATE (act:ProvActivity {
    uri:          $act_uri,
    activityType: 'playbook_curation',
    operation:    'delete',
    rationale:    $rationale,
    occurredAt:   $timestamp
})
MERGE (agent:ProvAgent {uri: 'prov:agent:ai:reflector-curator'})
ON CREATE SET agent.agentType = 'ai', agent.name = 'reflector-curator'
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:PROV_USED]->(pi)
RETURN pi.uri AS uri
"""


def main():
    parser = argparse.ArgumentParser(description="Write playbook items to Neo4j")
    parser.add_argument("input_file", help="JSON file with playbook items")
    parser.add_argument("--domain", required=True, help="Domain name")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--bolt-port", type=int, default=7687)
    parser.add_argument("--username", default="neo4j")
    parser.add_argument("--password", required=True)
    parser.add_argument("--database", default="neo4j")
    args = parser.parse_args()

    with open(args.input_file) as f:
        items = json.load(f)

    uri = f"bolt://{args.host}:{args.bolt_port}"
    driver = GraphDatabase.driver(uri, auth=(args.username, args.password))
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

    counts = {"add": 0, "update": 0, "delete": 0}

    try:
        with driver.session(database=args.database) as session:
            # Ensure playbook nodes exist for each phase
            phases = set(item["phase"] for item in items)
            for phase in phases:
                session.run(ENSURE_DOMAIN_PLAYBOOK, domain=args.domain, phase=phase)

            for i, item in enumerate(items):
                op = item["operation"]
                phase = item["phase"]
                rule = item.get("rule", "")
                rationale = item.get("rationale", "")
                evidence = item.get("evidence_activities", [])
                predecessor = item.get("predecessor_uri")

                item_uri = f"playbook:item:{args.domain.lower().replace(' ', '-')}:{phase}:{ts}:{i}"
                act_uri = f"prov:activity:playbook-curation:{ts}:{i}"

                if op == "add":
                    session.run(ADD_ITEM,
                                domain=args.domain, phase=phase,
                                uri=item_uri, act_uri=act_uri,
                                rule=rule, rationale=rationale,
                                timestamp=ts)
                    counts["add"] += 1

                elif op == "update":
                    if not predecessor:
                        print(f"WARNING: Update item {i} missing predecessor_uri, treating as add")
                        session.run(ADD_ITEM,
                                    domain=args.domain, phase=phase,
                                    uri=item_uri, act_uri=act_uri,
                                    rule=rule, rationale=rationale,
                                    timestamp=ts)
                        counts["add"] += 1
                    else:
                        session.run(UPDATE_ITEM,
                                    domain=args.domain, phase=phase,
                                    uri=item_uri, act_uri=act_uri,
                                    rule=rule, rationale=rationale,
                                    predecessor_uri=predecessor,
                                    timestamp=ts)
                        counts["update"] += 1

                elif op == "delete":
                    if not predecessor:
                        print(f"WARNING: Delete item {i} missing predecessor_uri, skipping")
                        continue
                    session.run(DELETE_ITEM,
                                predecessor_uri=predecessor,
                                act_uri=act_uri,
                                rationale=rationale,
                                timestamp=ts)
                    counts["delete"] += 1

                else:
                    print(f"WARNING: Unknown operation '{op}' for item {i}, skipping")

                # Link evidence activities to the curation ProvActivity
                for ev_uri in evidence:
                    session.run(LINK_EVIDENCE, act_uri=act_uri, ev_uri=ev_uri)

        print(f"\nPlaybook update complete for domain '{args.domain}':")
        print(f"  Added:   {counts['add']}")
        print(f"  Updated: {counts['update']}")
        print(f"  Deleted: {counts['delete']}")
        print(f"  Total:   {sum(counts.values())}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
