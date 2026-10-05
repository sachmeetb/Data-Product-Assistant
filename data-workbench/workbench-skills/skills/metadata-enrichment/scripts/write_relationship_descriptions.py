#!/usr/bin/env python3
"""
Write :RelationshipDescription nodes to Neo4j from a JSON descriptions
file, with W3C PROV-O provenance.

Sibling of write_table_descriptions.py — describes the FK edges
*between* :Dataset nodes rather than the datasets themselves. Each
:REFERENCES edge in the graph can carry at most one current
:RelationshipDescription.

Why a node, not properties on the edge: Neo4j relationships can't be
labelled with status / isCurrent / version the same way nodes can, and
PO review needs the same workflow (`pending_review` → `approved` /
`rejected` → versioning) as :ColumnDescription and :TableDescription.
The node hangs off the FROM :Dataset via [:HAS_RELATIONSHIP_DESCRIPTION]
and points at the TO :Dataset via [:DESCRIBES_REFERENCE_TO]. The
existing :Dataset—[:REFERENCES]—:Dataset edge stays untouched (it
carries fk_columns / referencedColumns / fk_name).

Each :RelationshipDescription carries:
  - text: a 1-2 sentence factual description of the relationship.
          Example: "Each order line belongs to exactly one order
          header; one header has many lines."
  - relationshipNature: classifier from a minimal v1 enum:

      belongs_to       — child→parent FK. Most common shape; the FROM
                         dataset's rows each refer to one row in the
                         TO dataset (order_item → order_header,
                         product → category, address → customer).
      categorises      — dimensional lookup. The TO dataset is a small
                         reference table that categorises the FROM
                         dataset's rows (product → category,
                         customer → country_code lookup).
      audit_log_for    — history / event table. The FROM dataset
                         records changes / events about the TO
                         dataset's rows (price_history → product,
                         status_history → order, audit_log → customer).
      references       — generic / unclassifiable. Default when none
                         of the more specific natures fits.

  - fromDatasetUri / toDatasetUri: redundantly recorded on the node so
    a single MATCH on (rd:RelationshipDescription) doesn't need to
    traverse the surrounding edges to reconstruct the relationship
    identity. Useful for review queues.

For each entry the script:
  - Skips relationships that already have a current :RelationshipDescription
    (no overwrite — re-run is safe)
  - Verifies the :Dataset-[:REFERENCES]->:Dataset edge actually exists
    in the graph (don't describe a non-existent edge)
  - Creates a :RelationshipDescription node (status='pending_review',
    isCurrent=true)
  - Links from the FROM :Dataset via [:HAS_RELATIONSHIP_DESCRIPTION]
    and to the TO :Dataset via [:DESCRIBES_REFERENCE_TO]
  - Records PROV-O provenance (ProvActivity 'generation' + shared
    ProvAgent)

Input JSON format:
  [
    {
      "from_dataset_uri": "dataset:<project_code>:public.order_item",
      "to_dataset_uri":   "dataset:<project_code>:public.order_header",
      "text":             "Each order line belongs to exactly one order header; one header has many lines.",
      "relationship_nature": "belongs_to"
    },
    ...
  ]

Usage:
    python write_relationship_descriptions.py <descriptions_file> [neo4j options]
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

# Minimal v1 enum. Keep this short — the value is consumed by the
# view-DDL bridge ranker, the data-mapping skill, and the
# question-analyzer / executor. Adding values requires updating each
# consumer; resist the urge to grow it unless a concrete consumer asks.
VALID_RELATIONSHIP_NATURES = {
    "belongs_to",
    "categorises",
    "audit_log_for",
    "references",
}


def _safe_segment(uri: str) -> str:
    """Strip `dataset:` prefix and any colon-separated project segment,
    return a flat URI-safe slug. Used to build readable
    :RelationshipDescription URIs that survive in logs / activity rows."""
    s = uri.replace("dataset:", "")
    return s.replace(":", "__").replace(".", "_")


def write_one_relationship_description(
    session,
    from_uri: str,
    to_uri: str,
    text: str,
    relationship_nature: str,
    timestamp: str,
    dry_run: bool,
) -> str:
    """Write one :RelationshipDescription with provenance.

    Returns one of: 'created', 'skipped', 'dry_run', 'error:<reason>'.
    """
    # Both datasets must exist
    r = session.run(
        """
        MATCH (from:Dataset {uri: $from_uri})
        MATCH (to:Dataset {uri: $to_uri})
        RETURN count(from) AS f, count(to) AS t
        """,
        from_uri=from_uri, to_uri=to_uri,
    ).single()
    if r is None or r["f"] == 0 or r["t"] == 0:
        return "error:dataset_not_found"

    # The :REFERENCES edge must actually exist — we describe real FK
    # edges, not invented ones. (Both directions are checked because
    # the FK might have been authored from either side.)
    r = session.run(
        """
        MATCH (from:Dataset {uri: $from_uri})-[:REFERENCES]->(to:Dataset {uri: $to_uri})
        RETURN count(*) AS n
        """,
        from_uri=from_uri, to_uri=to_uri,
    ).single()
    if r is None or r["n"] == 0:
        return "error:references_edge_missing"

    # Skip if a current description already exists.
    r = session.run(
        """
        MATCH (from:Dataset {uri: $from_uri})-[:HAS_RELATIONSHIP_DESCRIPTION]->(rd:RelationshipDescription)
              -[:DESCRIBES_REFERENCE_TO]->(to:Dataset {uri: $to_uri})
        WHERE rd.isCurrent = true
        RETURN count(rd) AS n
        """,
        from_uri=from_uri, to_uri=to_uri,
    ).single()
    if r is not None and r["n"] > 0:
        return "skipped"

    if dry_run:
        return "dry_run"

    if relationship_nature not in VALID_RELATIONSHIP_NATURES:
        relationship_nature = "references"

    edge_slug = f"{_safe_segment(from_uri)}__refs__{_safe_segment(to_uri)}"
    rd_uri = f"relationship_description:{edge_slug}:{timestamp}"
    activity_uri = f"prov:activity:relationship:{edge_slug}:{timestamp}"

    try:
        session.run(
            """
            MATCH (from:Dataset {uri: $from_uri})
            MATCH (to:Dataset {uri: $to_uri})
            CREATE (rd:RelationshipDescription {
                uri:                $rd_uri,
                text:                $text,
                relationshipNature:  $relationship_nature,
                fromDatasetUri:      $from_uri,
                toDatasetUri:        $to_uri,
                status:              'pending_review',
                isCurrent:           true,
                createdAt:           $timestamp
            })
            CREATE (from)-[:HAS_RELATIONSHIP_DESCRIPTION]->(rd)
            CREATE (rd)-[:DESCRIBES_REFERENCE_TO]->(to)
            CREATE (act:ProvActivity {
                uri:          $activity_uri,
                activityType: 'generation',
                occurredAt:   $timestamp
            })
            MERGE (agent:ProvAgent {uri: $agent_uri})
            ON CREATE SET agent.agentType = 'ai',
                          agent.name      = 'metadata-enrichment-skill'
            CREATE (rd)-[:PROV_WAS_GENERATED_BY]->(act)
            CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
            """,
            from_uri=from_uri,
            to_uri=to_uri,
            rd_uri=rd_uri,
            text=text,
            relationship_nature=relationship_nature,
            timestamp=timestamp,
            activity_uri=activity_uri,
            agent_uri=AI_AGENT_URI,
        )
        return "created"
    except ClientError as e:
        return f"error:{e.message}"


def main():
    parser = argparse.ArgumentParser(
        description="Write :RelationshipDescription nodes to Neo4j from a JSON file.",
    )
    parser.add_argument("descriptions_file", help="JSON file with relationship descriptions.")
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
        print("ERROR: JSON must be a list of {from_dataset_uri, to_dataset_uri, text, relationship_nature} entries.", file=sys.stderr)
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
            from_uri = (entry.get("from_dataset_uri") or "").strip()
            to_uri = (entry.get("to_dataset_uri") or "").strip()
            text = (entry.get("text") or "").strip()
            relationship_nature = (entry.get("relationship_nature") or "references").strip().lower()
            if not from_uri or not to_uri or not text:
                print(f"  SKIP (missing from/to/text): {entry}", file=sys.stderr)
                stats["errors"] += 1
                continue

            label = f"{from_uri.replace('dataset:', '')} → {to_uri.replace('dataset:', '')} [{relationship_nature}]"
            outcome = write_one_relationship_description(
                session, from_uri, to_uri, text, relationship_nature, timestamp, args.dry_run,
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
