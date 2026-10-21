#!/usr/bin/env python3
"""
Query Neo4j for rejected-then-corrected description pairs that can serve as
few-shot examples during metadata enrichment. Returns examples of what was
rejected and what the human preferred instead, for columns similar to the
ones being described.

This enables runtime learning: the LLM sees concrete examples of past
corrections before generating new descriptions.

Output:
  JSON array of rejection examples with original text, corrected text,
  rejection category, column metadata, and the table context.

Usage:
    python query_rejection_context.py [options]

Options:
    --tables      Comma-separated dataset URIs to find similar column rejections for
    --limit       Max examples to return (default: 20)
    --output      Output JSON file path (default: metadata/rejection_context_<timestamp>.json)
    --host        Neo4j host (default: localhost)
    --bolt-port   Bolt port (default: 7687)
    --username    Neo4j username (default: neo4j)
    --password    Neo4j password (default: your_password)
    --database    Neo4j database name (default: neo4j)
"""

import sys
import os
import argparse
import json
from datetime import datetime, timezone

try:
    from neo4j import GraphDatabase
    from neo4j.exceptions import ServiceUnavailable, AuthError
except ImportError:
    print("ERROR: neo4j driver is required. Install with: pip install neo4j", file=sys.stderr)
    sys.exit(1)

DEFAULT_HOST = "localhost"
DEFAULT_BOLT_PORT = 7687
DEFAULT_USERNAME = "neo4j"
DEFAULT_PASSWORD = "your_password"
DEFAULT_DATABASE = "neo4j"


# ── Queries ──────────────────────────────────────────────────────────────────

# Get all rejected descriptions with their corrections (global — across all tables)
ALL_REJECTIONS_QUERY = """\
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)-[:HAS_DESCRIPTION]->(orig:ColumnDescription)
MATCH (act:ProvActivity {activityType: 'review', outcome: 'rejected'})-[:PROV_USED]->(orig)
OPTIONAL MATCH (act)-[:HAS_REJECTION_REASON]->(reason:ProvRejectionReason)
OPTIONAL MATCH (corrected:ColumnDescription)-[:PROV_WAS_DERIVED_FROM]->(orig)
WHERE corrected.isCurrent = true
RETURN
    ds.schema         AS schema,
    ds.name           AS table_name,
    col.name          AS column_name,
    col.dataType      AS data_type,
    col.primaryKey    AS primary_key,
    col.nullable      AS nullable,
    orig.text         AS original_text,
    corrected.text    AS corrected_text,
    reason.category   AS rejection_category,
    reason.detail     AS rejection_detail
ORDER BY ds.name, col.name
LIMIT $limit
"""

# Get rejections for columns in the same tables being enriched
SAME_TABLE_REJECTIONS_QUERY = """\
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)-[:HAS_DESCRIPTION]->(orig:ColumnDescription)
WHERE ds.uri IN $table_uris
MATCH (act:ProvActivity {activityType: 'review', outcome: 'rejected'})-[:PROV_USED]->(orig)
OPTIONAL MATCH (act)-[:HAS_REJECTION_REASON]->(reason:ProvRejectionReason)
OPTIONAL MATCH (corrected:ColumnDescription)-[:PROV_WAS_DERIVED_FROM]->(orig)
WHERE corrected.isCurrent = true
RETURN
    ds.schema         AS schema,
    ds.name           AS table_name,
    col.name          AS column_name,
    col.dataType      AS data_type,
    col.primaryKey    AS primary_key,
    col.nullable      AS nullable,
    orig.text         AS original_text,
    corrected.text    AS corrected_text,
    reason.category   AS rejection_category,
    reason.detail     AS rejection_detail
ORDER BY ds.name, col.name
"""

# Get high-quality approved descriptions (quality=3) as positive examples
EXEMPLAR_DESCRIPTIONS_QUERY = """\
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)-[:HAS_DESCRIPTION]->(cd:ColumnDescription)
WHERE cd.status = 'approved' AND cd.isCurrent = true
MATCH (act:ProvActivity {activityType: 'review', outcome: 'approved', quality: 3})-[:PROV_USED]->(cd)
RETURN
    ds.schema         AS schema,
    ds.name           AS table_name,
    col.name          AS column_name,
    col.dataType      AS data_type,
    col.primaryKey    AS primary_key,
    col.nullable      AS nullable,
    cd.text           AS description_text
ORDER BY ds.name, col.name
LIMIT $limit
"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Query Neo4j for rejected description examples to improve enrichment.",
    )
    parser.add_argument("--tables", type=str, default=None,
                        help="Comma-separated dataset URIs to find same-table rejections")
    parser.add_argument("--limit", type=int, default=20,
                        help="Max examples to return (default: 20)")
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--bolt-port", type=int, default=DEFAULT_BOLT_PORT)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    args = parser.parse_args()

    bolt_uri = f"bolt://{args.host}:{args.bolt_port}"
    print(f"Connecting to {bolt_uri} as '{args.username}' (database: {args.database})...")

    try:
        driver = GraphDatabase.driver(bolt_uri, auth=(args.username, args.password))
        driver.verify_connectivity()
    except ServiceUnavailable as e:
        print(f"ERROR: Cannot connect to Neo4j at {bolt_uri}\n  {e}", file=sys.stderr)
        sys.exit(1)
    except AuthError as e:
        print(f"ERROR: Authentication failed\n  {e}", file=sys.stderr)
        sys.exit(1)

    metadata_dir = os.path.join(os.getcwd(), "metadata")
    os.makedirs(metadata_dir, exist_ok=True)

    with driver.session(database=args.database) as session:
        same_table = []
        if args.tables:
            table_uris = [t.strip() for t in args.tables.split(",") if t.strip()]
            result = session.run(SAME_TABLE_REJECTIONS_QUERY, table_uris=table_uris)
            same_table = [dict(r) for r in result]

        # Get global rejections (across all tables)
        result = session.run(ALL_REJECTIONS_QUERY, limit=args.limit)
        all_rejections = [dict(r) for r in result]

        # Get exemplar descriptions
        result = session.run(EXEMPLAR_DESCRIPTIONS_QUERY, limit=args.limit)
        exemplars = [dict(r) for r in result]

    driver.close()

    output = {
        "same_table_rejections": same_table,
        "all_rejections": all_rejections,
        "exemplar_descriptions": exemplars,
        "statistics": {
            "same_table_rejection_count": len(same_table),
            "global_rejection_count": len(all_rejections),
            "exemplar_count": len(exemplars),
        },
    }

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = args.output or os.path.join(metadata_dir, f"rejection_context_{ts}.json")

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"Written: {output_path}")
    print(f"  Same-table rejections: {len(same_table)}")
    print(f"  Global rejections:     {len(all_rejections)}")
    print(f"  Exemplar descriptions: {len(exemplars)}")


if __name__ == "__main__":
    main()
