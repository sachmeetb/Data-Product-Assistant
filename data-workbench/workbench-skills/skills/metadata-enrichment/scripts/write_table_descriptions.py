#!/usr/bin/env python3
"""
Write :TableDescription nodes to Neo4j from a JSON descriptions file,
with W3C PROV-O provenance.

Sibling of write_descriptions.py — same lifecycle / review semantics, but the
target is :Dataset (not :Column) and the rel type is :HAS_TABLE_DESCRIPTION.

Each :TableDescription carries:
  - text: a 1–2 sentence factual description of what the table represents
  - relationshipKind: classifier for how this table participates in the FK
    graph. Drives downstream auto-bridge disambiguation in the view-DDL
    generator. Values:
       fact                — primary entity (employee, customer, product, order)
       lookup_dimension    — small reference table (country codes, status enum)
       general_membership  — junction table for an M:N relationship
                             (dept_employee — every employee × every department
                             they're assigned to). Auto-bridge prefers these
                             when picking between candidate junctions.
       specialization      — role-specific subset of a relationship
                             (dept_manager — only employees who manage a dept).
                             Auto-bridge demotes these.
       audit_log           — history/event log
       configuration       — system / app settings
       unknown             — generator couldn't classify

For each entry the script:
  - Skips datasets that already have a current :TableDescription (no overwrite)
  - Creates a :TableDescription node (status='pending_review', isCurrent=true)
  - Links via [:HAS_TABLE_DESCRIPTION] from :Dataset
  - Records PROV-O provenance (ProvActivity 'generation' + shared ProvAgent)

Input JSON format:
  [
    {
      "dataset_uri":       "dataset:<project_code>:employees.department_employee",
      "description":       "Records each employee's assignments to departments over time. One row per (employee, department, period).",
      "relationship_kind": "general_membership"
    },
    ...
  ]

Usage:
    python write_table_descriptions.py <descriptions_file> [neo4j options]
"""

import argparse
import json
import os
import sys
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

AI_AGENT_URI = "prov:agent:ai:metadata-enrichment-skill"

VALID_RELATIONSHIP_KINDS = {
    "fact",
    "lookup_dimension",
    "general_membership",
    "specialization",
    "audit_log",
    "configuration",
    "unknown",
}


def write_one_table_description(
    session,
    dataset_uri: str,
    description: str,
    relationship_kind: str,
    timestamp: str,
    dry_run: bool,
) -> str:
    """Write one :TableDescription with provenance.

    Returns one of: 'created', 'skipped', 'dry_run', 'error:<reason>'.
    """
    r = session.run(
        "MATCH (ds:Dataset {uri: $uri}) RETURN count(ds) AS n",
        uri=dataset_uri,
    )
    if r.single()["n"] == 0:
        return "error:dataset_not_found"

    r = session.run(
        """
        MATCH (ds:Dataset {uri: $uri})-[:HAS_TABLE_DESCRIPTION]->(td:TableDescription)
        WHERE td.isCurrent = true
        RETURN count(td) AS n
        """,
        uri=dataset_uri,
    )
    if r.single()["n"] > 0:
        return "skipped"

    if dry_run:
        return "dry_run"

    bare = dataset_uri.replace("dataset:", "")
    td_uri = f"table_description:{bare}:{timestamp}"
    activity_uri = f"prov:activity:table:{bare}:{timestamp}"

    if relationship_kind not in VALID_RELATIONSHIP_KINDS:
        relationship_kind = "unknown"

    try:
        session.run(
            """
            MATCH (ds:Dataset {uri: $dataset_uri})
            CREATE (td:TableDescription {
                uri:              $td_uri,
                text:             $text,
                relationshipKind: $relationship_kind,
                status:           'pending_review',
                isCurrent:        true,
                createdAt:        $timestamp
            })
            CREATE (ds)-[:HAS_TABLE_DESCRIPTION]->(td)
            CREATE (act:ProvActivity {
                uri:          $activity_uri,
                activityType: 'generation',
                occurredAt:   $timestamp
            })
            MERGE (agent:ProvAgent {uri: $agent_uri})
            ON CREATE SET agent.agentType = 'ai',
                          agent.name      = 'metadata-enrichment-skill'
            CREATE (td)-[:PROV_WAS_GENERATED_BY]->(act)
            CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
            """,
            dataset_uri=dataset_uri,
            td_uri=td_uri,
            text=description,
            relationship_kind=relationship_kind,
            timestamp=timestamp,
            activity_uri=activity_uri,
            agent_uri=AI_AGENT_URI,
        )
        return "created"
    except ClientError as e:
        return f"error:{e.message}"


def main():
    parser = argparse.ArgumentParser(
        description="Write :TableDescription nodes to Neo4j from a JSON file.",
    )
    parser.add_argument("descriptions_file", help="JSON file with table descriptions.")
    parser.add_argument("--host",      default=DEFAULT_HOST)
    parser.add_argument("--bolt-port", type=int, default=DEFAULT_BOLT_PORT)
    parser.add_argument("--username",  default=DEFAULT_USERNAME)
    parser.add_argument("--password",  default=DEFAULT_PASSWORD)
    parser.add_argument("--database",  default=DEFAULT_DATABASE)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be written without making changes")
    args = parser.parse_args()

    if not os.path.isfile(args.descriptions_file):
        print(f"ERROR: File not found: {args.descriptions_file}", file=sys.stderr)
        sys.exit(1)

    with open(args.descriptions_file) as f:
        entries = json.load(f)

    if not isinstance(entries, list):
        print("ERROR: JSON must be a list of {dataset_uri, description, relationship_kind} entries.", file=sys.stderr)
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
        print(f"ERROR: Authentication failed for user '{args.username}'\n  {e}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("[DRY-RUN MODE — no changes will be made]\n")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stats = {"created": 0, "skipped": 0, "errors": 0}

    with driver.session(database=args.database) as session:
        for entry in entries:
            if not isinstance(entry, dict):
                print(f"  SKIP (not a dict): {entry}", file=sys.stderr)
                stats["errors"] += 1
                continue
            dataset_uri = (entry.get("dataset_uri") or "").strip()
            description = (entry.get("description") or "").strip()
            relationship_kind = (entry.get("relationship_kind") or "unknown").strip().lower()
            if not dataset_uri or not description:
                print(f"  SKIP (missing dataset_uri/description): {entry}", file=sys.stderr)
                stats["errors"] += 1
                continue

            label = f"{dataset_uri.replace('dataset:', '')} [{relationship_kind}]"
            outcome = write_one_table_description(
                session, dataset_uri, description, relationship_kind, timestamp, args.dry_run,
            )

            if outcome == "created":
                print(f"  CREATED  {label}")
                stats["created"] += 1
            elif outcome == "skipped":
                print(f"  SKIP     {label}  (current description already present)")
                stats["skipped"] += 1
            elif outcome == "dry_run":
                print(f"  [DRY-RUN] Would create: {label}")
                stats["created"] += 1
            else:
                print(f"  ERROR    {label}  ({outcome})", file=sys.stderr)
                stats["errors"] += 1

    driver.close()

    print(
        f"\nDone.  created={stats['created']}  "
        f"skipped={stats['skipped']}  errors={stats['errors']}"
    )
    if stats["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
