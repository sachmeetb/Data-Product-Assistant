#!/usr/bin/env python3
"""
Write :ColumnDescription nodes to Neo4j from a JSON descriptions file,
with W3C PROV-O provenance.

For each entry in the input file:
  - Skips columns that already have a :ColumnDescription (no overwrite).
  - Creates a :ColumnDescription node with status 'pending_review' and
    isCurrent: true, linked to the :Column via [:HAS_DESCRIPTION].
  - Creates a :ProvActivity {activityType: 'generation'} node and a shared
    :ProvAgent for the AI agent.
  - Links: (cd)-[:PROV_WAS_GENERATED_BY]->(act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)

Input JSON format:
  [
    {
      "column_uri": "column:employees.employee.id",
      "description": "Unique identifier for each employee record."
    },
    ...
  ]

New nodes:
  (:ColumnDescription {
    uri:       "description:employees.employee.id:<timestamp>",
    text:      "...",
    status:    "pending_review",
    isCurrent: true
  })
  (:ProvActivity {
    uri:          "prov:activity:employees.employee.id:<timestamp>",
    activityType: "generation",
    occurredAt:   "<ISO timestamp>"
  })
  (:ProvAgent {
    uri:       "prov:agent:ai:metadata-enrichment-skill",
    agentType: "ai",
    name:      "metadata-enrichment-skill"
  })

New relationships:
  (:Column)-[:HAS_DESCRIPTION]->(:ColumnDescription)
  (:ColumnDescription)-[:PROV_WAS_GENERATED_BY]->(:ProvActivity)
  (:ProvActivity)-[:PROV_WAS_ASSOCIATED_WITH]->(:ProvAgent)

Usage:
    python write_descriptions.py <descriptions_file> [options]

Options:
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

AI_AGENT_URI = "prov:agent:ai:metadata-enrichment-skill"


# ── Write logic ───────────────────────────────────────────────────────────────

def write_one_description(
    session,
    column_uri: str,
    description: str,
    timestamp: str,
    dry_run: bool,
) -> str:
    """
    Attempt to write one :ColumnDescription with provenance.
    Returns one of: 'created', 'skipped', 'dry_run', 'error:<reason>'.
    """
    # Column must exist
    r = session.run(
        "MATCH (col:Column {uri: $uri}) RETURN count(col) AS n",
        uri=column_uri,
    )
    if r.single()["n"] == 0:
        return "error:column_not_found"

    # Skip if a description already exists (no overwrite by design)
    r = session.run(
        "MATCH (col:Column {uri: $uri})-[:HAS_DESCRIPTION]->(:ColumnDescription) RETURN count(*) AS n",
        uri=column_uri,
    )
    if r.single()["n"] > 0:
        return "skipped"

    if dry_run:
        return "dry_run"

    bare = column_uri.replace("column:", "")
    desc_uri = f"description:{bare}:{timestamp}"
    activity_uri = f"prov:activity:{bare}:{timestamp}"

    try:
        session.run(
            """
            MATCH (col:Column {uri: $col_uri})
            CREATE (cd:ColumnDescription {
                uri:       $desc_uri,
                text:      $text,
                status:    'pending_review',
                isCurrent: true
            })
            CREATE (col)-[:HAS_DESCRIPTION]->(cd)

            CREATE (act:ProvActivity {
                uri:          $activity_uri,
                activityType: 'generation',
                occurredAt:   $timestamp
            })

            MERGE (agent:ProvAgent {uri: $agent_uri})
            ON CREATE SET
                agent.agentType = 'ai',
                agent.name      = 'metadata-enrichment-skill'

            CREATE (cd)-[:PROV_WAS_GENERATED_BY]->(act)
            CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
            """,
            col_uri=column_uri,
            desc_uri=desc_uri,
            text=description,
            activity_uri=activity_uri,
            timestamp=timestamp,
            agent_uri=AI_AGENT_URI,
        )
        return "created"
    except ClientError as e:
        return f"error:{e.message}"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Write :ColumnDescription nodes to Neo4j from a JSON file.",
    )
    parser.add_argument(
        "descriptions_file",
        help="JSON file containing [{column_uri, description}, ...]",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--bolt-port", type=int, default=DEFAULT_BOLT_PORT)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without making changes",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.descriptions_file):
        print(f"ERROR: File not found: {args.descriptions_file}", file=sys.stderr)
        sys.exit(1)

    with open(args.descriptions_file) as f:
        descriptions = json.load(f)

    if not isinstance(descriptions, list):
        print("ERROR: JSON must be a list of {column_uri, description} objects.", file=sys.stderr)
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
        for entry in descriptions:
            col_uri = entry.get("column_uri", "").strip()
            text = entry.get("description", "").strip()

            if not col_uri or not text:
                print(
                    f"  SKIP (missing column_uri or description): {entry}",
                    file=sys.stderr,
                )
                stats["errors"] += 1
                continue

            short_name = col_uri.replace("column:", "")
            outcome = write_one_description(session, col_uri, text, timestamp, args.dry_run)

            if outcome == "created":
                print(f"  CREATED  {short_name}")
                stats["created"] += 1
            elif outcome == "skipped":
                print(f"  SKIP     {short_name}  (already has description — no overwrite)")
                stats["skipped"] += 1
            elif outcome == "dry_run":
                print(f"  [DRY-RUN] Would create description for {short_name}")
                stats["created"] += 1
            else:
                print(f"  ERROR    {short_name}  ({outcome})", file=sys.stderr)
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
