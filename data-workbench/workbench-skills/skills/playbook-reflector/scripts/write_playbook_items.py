#!/usr/bin/env python3
"""
Write :PlaybookItem nodes to Neo4j with PROV-O provenance, scoped to a domain.

Creates the Domain -> Playbook -> PlaybookItem graph structure if it doesn't exist.
Supports add, update, merge, and delete operations on playbook items.

Input JSON format:
  [
    {
      "phase": "enrichment",
      "operation": "add",
      "rule": "Generic rule text",
      "rationale": "Evidence-grounded explanation",
      "evidence_activities": ["prov:activity:...", ...]
    },
    {
      "phase": "enrichment",
      "operation": "update",
      "rule": "Updated rule text",
      "rationale": "Why the rule was updated",
      "predecessor_uri": "playbook-item:...:...",
      "evidence_activities": ["prov:activity:...", ...]
    }
  ]

Usage:
    python write_playbook_items.py <items_file> --domain "Human Resources" [options]

Options:
    --domain      Domain name (required)
    --host        Neo4j host (default: localhost)
    --bolt-port   Bolt port (default: 7687)
    --username    Neo4j username (default: neo4j)
    --password    Neo4j password (default: your_password)
    --database    Neo4j database name (default: neo4j)
    --dry-run     Print what would be written without making changes
"""

import sys
import os
import argparse
import json
from datetime import datetime, timezone

try:
    from neo4j import GraphDatabase
    from neo4j.exceptions import ServiceUnavailable, AuthError, ClientError
except ImportError:
    print("ERROR: neo4j driver is required. Install with: pip install neo4j", file=sys.stderr)
    sys.exit(1)

DEFAULT_HOST = "localhost"
DEFAULT_BOLT_PORT = 7687
DEFAULT_USERNAME = "neo4j"
DEFAULT_PASSWORD = "your_password"
DEFAULT_DATABASE = "neo4j"

AGENT_URI = "prov:agent:ai:playbook-reflector"


def ensure_playbook_structure(session, domain: str, phase: str):
    """Ensure Domain -> Playbook chain exists for the given domain and phase."""
    session.run("""
        MERGE (d:Domain {name: $domain})
        ON CREATE SET d.createdAt = datetime()
        MERGE (d)-[:HAS_PLAYBOOK]->(pb:Playbook {
            uri: 'playbook:' + replace($domain, ' ', '_') + ':' + $phase
        })
        ON CREATE SET pb.phase = $phase, pb.domain = $domain
    """, domain=domain, phase=phase)


def get_next_sequence(session, domain: str, phase: str) -> int:
    """Get the next available sequence number for a playbook item."""
    result = session.run("""
        MATCH (d:Domain {name: $domain})-[:HAS_PLAYBOOK]->(pb:Playbook {phase: $phase})
              -[:HAS_ITEM]->(pi:PlaybookItem)
        RETURN max(pi.sequence) AS max_seq
    """, domain=domain, phase=phase)
    record = result.single()
    max_seq = record["max_seq"] if record and record["max_seq"] is not None else 0
    return max_seq + 1


def add_item(session, domain: str, item: dict, timestamp: str, dry_run: bool) -> str:
    """Add a new playbook item."""
    phase = item["phase"]
    rule = item["rule"]
    rationale = item.get("rationale", "")
    evidence = item.get("evidence_activities", [])

    if dry_run:
        return "dry_run"

    ensure_playbook_structure(session, domain, phase)
    seq = get_next_sequence(session, domain, phase)

    domain_slug = domain.replace(" ", "_")
    item_uri = f"playbook-item:{domain_slug}:{phase}:{seq}:v1"
    activity_uri = f"prov:activity:playbook-curation:{domain_slug}:{timestamp}:{seq}"

    try:
        session.run("""
            MATCH (d:Domain {name: $domain})-[:HAS_PLAYBOOK]->(pb:Playbook {phase: $phase})
            CREATE (pi:PlaybookItem {
                uri:       $item_uri,
                rule:      $rule,
                version:   1,
                sequence:  $seq,
                isCurrent: true
            })
            CREATE (pb)-[:HAS_ITEM]->(pi)
            CREATE (act:ProvActivity {
                uri:          $activity_uri,
                activityType: 'playbook_curation',
                operation:    'add',
                rationale:    $rationale,
                occurredAt:   $timestamp
            })
            MERGE (agent:ProvAgent {uri: $agent_uri})
            ON CREATE SET agent.agentType = 'ai', agent.name = 'playbook-reflector'
            CREATE (pi)-[:PROV_WAS_GENERATED_BY]->(act)
            CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
        """,
            domain=domain, phase=phase, item_uri=item_uri, rule=rule,
            seq=seq, activity_uri=activity_uri, rationale=rationale,
            timestamp=timestamp, agent_uri=AGENT_URI,
        )

        # Link to evidence activities
        for ev_uri in evidence:
            session.run("""
                MATCH (act:ProvActivity {uri: $act_uri})
                MATCH (ev:ProvActivity {uri: $ev_uri})
                MERGE (act)-[:PROV_INFORMED_BY]->(ev)
            """, act_uri=activity_uri, ev_uri=ev_uri)

        return "added"
    except ClientError as e:
        return f"error:{e.message}"


def update_item(session, domain: str, item: dict, timestamp: str, dry_run: bool) -> str:
    """Update an existing playbook item (retire old, create new version)."""
    phase = item["phase"]
    rule = item["rule"]
    rationale = item.get("rationale", "")
    predecessor_uri = item.get("predecessor_uri")
    evidence = item.get("evidence_activities", [])

    if not predecessor_uri:
        return "error:predecessor_uri required for update"

    if dry_run:
        return "dry_run"

    # Get predecessor info
    result = session.run("""
        MATCH (pi:PlaybookItem {uri: $uri})
        RETURN pi.version AS version, pi.sequence AS sequence
    """, uri=predecessor_uri)
    record = result.single()
    if not record:
        return f"error:predecessor not found: {predecessor_uri}"

    old_version = record["version"]
    seq = record["sequence"]
    new_version = old_version + 1

    domain_slug = domain.replace(" ", "_")
    item_uri = f"playbook-item:{domain_slug}:{phase}:{seq}:v{new_version}"
    activity_uri = f"prov:activity:playbook-curation:{domain_slug}:{timestamp}:{seq}"

    try:
        session.run("""
            MATCH (old:PlaybookItem {uri: $predecessor_uri})
            SET old.isCurrent = false
            WITH old
            MATCH (pb:Playbook)-[:HAS_ITEM]->(old)
            CREATE (pi:PlaybookItem {
                uri:       $item_uri,
                rule:      $rule,
                version:   $new_version,
                sequence:  $seq,
                isCurrent: true
            })
            CREATE (pb)-[:HAS_ITEM]->(pi)
            CREATE (pi)-[:PROV_WAS_DERIVED_FROM]->(old)
            CREATE (act:ProvActivity {
                uri:          $activity_uri,
                activityType: 'playbook_curation',
                operation:    'update',
                rationale:    $rationale,
                occurredAt:   $timestamp
            })
            MERGE (agent:ProvAgent {uri: $agent_uri})
            ON CREATE SET agent.agentType = 'ai', agent.name = 'playbook-reflector'
            CREATE (pi)-[:PROV_WAS_GENERATED_BY]->(act)
            CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
        """,
            predecessor_uri=predecessor_uri, item_uri=item_uri, rule=rule,
            new_version=new_version, seq=seq, activity_uri=activity_uri,
            rationale=rationale, timestamp=timestamp, agent_uri=AGENT_URI,
        )

        for ev_uri in evidence:
            session.run("""
                MATCH (act:ProvActivity {uri: $act_uri})
                MATCH (ev:ProvActivity {uri: $ev_uri})
                MERGE (act)-[:PROV_INFORMED_BY]->(ev)
            """, act_uri=activity_uri, ev_uri=ev_uri)

        return "updated"
    except ClientError as e:
        return f"error:{e.message}"


def delete_item(session, domain: str, item: dict, timestamp: str, dry_run: bool) -> str:
    """Mark a playbook item as no longer current (soft delete with provenance)."""
    predecessor_uri = item.get("predecessor_uri")
    rationale = item.get("rationale", "")

    if not predecessor_uri:
        return "error:predecessor_uri required for delete"

    if dry_run:
        return "dry_run"

    domain_slug = domain.replace(" ", "_")
    activity_uri = f"prov:activity:playbook-curation:{domain_slug}:{timestamp}:del"

    try:
        session.run("""
            MATCH (pi:PlaybookItem {uri: $uri})
            SET pi.isCurrent = false
            CREATE (act:ProvActivity {
                uri:          $activity_uri,
                activityType: 'playbook_curation',
                operation:    'delete',
                rationale:    $rationale,
                occurredAt:   $timestamp
            })
            MERGE (agent:ProvAgent {uri: $agent_uri})
            ON CREATE SET agent.agentType = 'ai', agent.name = 'playbook-reflector'
            CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
            CREATE (act)-[:PROV_USED]->(pi)
        """,
            uri=predecessor_uri, activity_uri=activity_uri,
            rationale=rationale, timestamp=timestamp, agent_uri=AGENT_URI,
        )
        return "deleted"
    except ClientError as e:
        return f"error:{e.message}"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Write :PlaybookItem nodes to Neo4j with provenance.",
    )
    parser.add_argument("items_file", help="JSON file containing playbook items")
    parser.add_argument("--domain", required=True, help="Domain name")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--bolt-port", type=int, default=DEFAULT_BOLT_PORT)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be written without making changes")
    args = parser.parse_args()

    if not os.path.isfile(args.items_file):
        print(f"ERROR: File not found: {args.items_file}", file=sys.stderr)
        sys.exit(1)

    with open(args.items_file) as f:
        items = json.load(f)

    if not isinstance(items, list):
        print("ERROR: JSON must be a list of playbook item objects.", file=sys.stderr)
        sys.exit(1)

    bolt_uri = f"bolt://{args.host}:{args.bolt_port}"
    print(f"Connecting to {bolt_uri} as '{args.username}' (database: {args.database})...")

    try:
        driver = GraphDatabase.driver(bolt_uri, auth=(args.username, args.password))
        if not args.dry_run:
            driver.verify_connectivity()
    except ServiceUnavailable as e:
        print(f"ERROR: Cannot connect to Neo4j at {bolt_uri}\n  {e}", file=sys.stderr)
        sys.exit(1)
    except AuthError as e:
        print(f"ERROR: Authentication failed\n  {e}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("[DRY-RUN MODE -- no changes will be made]\n")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stats = {"added": 0, "updated": 0, "deleted": 0, "errors": 0}

    operations = {
        "add": add_item,
        "update": update_item,
        "merge": update_item,  # merge uses same logic as update
        "delete": delete_item,
    }

    with driver.session(database=args.database) as session:
        for i, item in enumerate(items):
            op = item.get("operation", "add")
            handler = operations.get(op)

            if not handler:
                print(f"  ERROR  item {i}: unknown operation '{op}'", file=sys.stderr)
                stats["errors"] += 1
                continue

            rule_preview = (item.get("rule", "")[:60] + "...") if len(item.get("rule", "")) > 60 else item.get("rule", "")
            outcome = handler(session, args.domain, item, timestamp, args.dry_run)

            if outcome in ("added", "updated", "deleted"):
                op_key = outcome.rstrip("d") + "d" if outcome != "deleted" else "deleted"
                # Normalize to stats keys
                if outcome == "added":
                    stats["added"] += 1
                elif outcome == "updated":
                    stats["updated"] += 1
                elif outcome == "deleted":
                    stats["deleted"] += 1
                print(f"  {outcome.upper():8s}  [{item.get('phase', '?')}] {rule_preview}")
            elif outcome == "dry_run":
                print(f"  [DRY-RUN] Would {op}: [{item.get('phase', '?')}] {rule_preview}")
                stats["added"] += 1
            else:
                print(f"  ERROR    item {i}: {outcome}", file=sys.stderr)
                stats["errors"] += 1

    driver.close()

    print(f"\nDone.  added={stats['added']}  updated={stats['updated']}  "
          f"deleted={stats['deleted']}  errors={stats['errors']}")

    if stats["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
