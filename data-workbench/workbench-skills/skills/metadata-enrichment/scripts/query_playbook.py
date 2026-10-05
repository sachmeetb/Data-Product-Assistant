#!/usr/bin/env python3
"""
Query Neo4j for domain-scoped playbook items to inject into the metadata
enrichment prompt. Returns the current playbook rules for the 'enrichment'
phase that should guide description generation.

Output:
  JSON object with playbook items and a formatted text block suitable
  for inclusion in an LLM prompt.

Usage:
    python query_playbook.py --domain "Human Resources" [options]

Options:
    --domain      Domain name to scope the query (required)
    --output      Output JSON file path (default: metadata/playbook_guidance.json)
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


PLAYBOOK_QUERY = """\
MATCH (d:Domain {name: $domain})-[:HAS_PLAYBOOK]->(pb:Playbook {phase: 'enrichment'})
      -[:HAS_ITEM]->(pi:PlaybookItem)
WHERE pi.isCurrent = true
RETURN
    pi.rule    AS rule,
    pi.version AS version
ORDER BY pi.sequence
"""


def main():
    parser = argparse.ArgumentParser(
        description="Query Neo4j for domain playbook items for metadata enrichment.",
    )
    parser.add_argument("--domain", required=True, help="Domain name")
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

    with driver.session(database=args.database) as session:
        result = session.run(PLAYBOOK_QUERY, domain=args.domain)
        items = [dict(r) for r in result]

    driver.close()

    # Build a formatted guidance text block for LLM injection
    if items:
        lines = [f"Domain Playbook for '{args.domain}' — Enrichment Guidelines:"]
        for i, item in enumerate(items, 1):
            lines.append(f"  {i}. {item['rule']}")
        guidance_text = "\n".join(lines)
    else:
        guidance_text = ""

    output = {
        "domain": args.domain,
        "phase": "enrichment",
        "items": items,
        "item_count": len(items),
        "guidance_text": guidance_text,
    }

    metadata_dir = os.path.join(os.getcwd(), "metadata")
    os.makedirs(metadata_dir, exist_ok=True)
    output_path = args.output or os.path.join(metadata_dir, "playbook_guidance.json")

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"Written: {output_path}")
    print(f"  Playbook items: {len(items)}")
    if items:
        print(f"\n{guidance_text}")
    else:
        print(f"  No playbook items found for domain '{args.domain}'.")


if __name__ == "__main__":
    main()
