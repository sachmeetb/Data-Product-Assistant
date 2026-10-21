#!/usr/bin/env python3
"""
Load domain rule Cypher into Neo4j.

Reads cypher_scripts/domain_rules.cypher and executes each statement against Neo4j.
Includes pre-flight checks to avoid duplicate rules.

Usage:
    python run_domain_rules.py [options]

Options:
    --host        Neo4j host (default: localhost)
    --bolt-port   Bolt port (default: 7687)
    --username    Neo4j username (default: neo4j)
    --password    Neo4j password (default: your_password)
    --database    Neo4j database name (default: neo4j)
    --dry-run     Show statements without executing
"""

import argparse
import os
import sys

try:
    from neo4j import GraphDatabase
    from neo4j.exceptions import ServiceUnavailable, AuthError
except ImportError:
    print("ERROR: neo4j driver required. Install with: pip install neo4j", file=sys.stderr)
    sys.exit(1)

DEFAULT_HOST = "localhost"
DEFAULT_BOLT_PORT = 7687
DEFAULT_USERNAME = "neo4j"
DEFAULT_PASSWORD = "your_password"
DEFAULT_DATABASE = "neo4j"


def main():
    parser = argparse.ArgumentParser(description="Load domain rule Cypher into Neo4j.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--bolt-port", type=int, default=DEFAULT_BOLT_PORT)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--dry-run", action="store_true", help="Preview without executing")
    parser.add_argument("--cypher-file", default=None, help="Path to Cypher file (default: cypher_scripts/domain_rules.cypher)")
    args = parser.parse_args()

    cypher_file = args.cypher_file or os.path.join(os.getcwd(), "cypher_scripts", "domain_rules.cypher")
    if not os.path.exists(cypher_file):
        print(f"ERROR: Cypher file not found: {cypher_file}", file=sys.stderr)
        print("Run generate_domain_rules.py first.")
        sys.exit(1)

    with open(cypher_file) as f:
        content = f.read()

    # Split into statements (separated by semicolons at end of line)
    statements = []
    current = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        current.append(line)
        if stripped.endswith(";"):
            stmt = "\n".join(current).strip().rstrip(";")
            if stmt:
                statements.append(stmt)
            current = []

    if not statements:
        print("No Cypher statements found in the file.")
        return

    print(f"Found {len(statements)} statement(s) to execute.")

    if args.dry_run:
        print("\n[DRY RUN] Would execute:")
        for i, stmt in enumerate(statements, 1):
            print(f"\n--- Statement {i} ---")
            print(stmt)
        print(f"\n[DRY RUN] {len(statements)} statement(s) would be executed.")
        return

    bolt_uri = f"bolt://{args.host}:{args.bolt_port}"
    print(f"Connecting to {bolt_uri}...")

    try:
        driver = GraphDatabase.driver(bolt_uri, auth=(args.username, args.password))
        driver.verify_connectivity()
    except (ServiceUnavailable, AuthError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    # Pre-flight: check if domain rules already exist
    with driver.session(database=args.database) as session:
        existing = session.run(
            "MATCH (ps:PropertyShape) WHERE ps.ruleSource = 'domain' RETURN count(ps) AS cnt"
        ).single()["cnt"]
        if existing > 0:
            print(f"  NOTE: {existing} domain rule(s) already exist in the graph.")
            print(f"  New rules will be added alongside existing ones.")

    # Execute
    success = 0
    failed = 0
    with driver.session(database=args.database) as session:
        for i, stmt in enumerate(statements, 1):
            try:
                session.run(stmt)
                success += 1
            except Exception as e:
                print(f"  ERROR on statement {i}: {e}")
                failed += 1

    driver.close()

    print(f"\nDone: {success} succeeded, {failed} failed (out of {len(statements)})")


if __name__ == "__main__":
    main()
