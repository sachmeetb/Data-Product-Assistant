#!/usr/bin/env python3
"""
Execute a Cypher script against a Neo4j instance.

Usage:
    python run_cypher.py [cypher_file] [options]

    cypher_file   path to the .cypher file (default: catalog.cypher in cwd)

Options:
    --host        Neo4j host (default: localhost)
    --bolt-port   Bolt port (default: 7687)
    --username    Neo4j username (default: neo4j)
    --password    Neo4j password (default: your_password)
    --database    Neo4j database name (default: neo4j)
    --dry-run     Parse and print statements without executing
"""

import sys
import os
import argparse

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


def parse_statements(cypher_text: str) -> list[str]:
    """Split a Cypher script into individual statements, stripping comments and blanks."""
    statements = []
    current = []

    for line in cypher_text.splitlines():
        stripped = line.strip()
        # Skip comment-only lines
        if stripped.startswith("//") or stripped == "":
            continue
        current.append(line)
        # A semicolon at the end of a line ends a statement
        if stripped.endswith(";"):
            stmt = "\n".join(current).strip().rstrip(";").strip()
            if stmt:
                statements.append(stmt)
            current = []

    # Catch any trailing statement without a semicolon
    if current:
        stmt = "\n".join(current).strip()
        if stmt:
            statements.append(stmt)

    return statements


def run(cypher_file: str, bolt_uri: str, username: str, password: str, database: str, dry_run: bool):
    with open(cypher_file) as f:
        cypher_text = f.read()

    statements = parse_statements(cypher_text)

    if not statements:
        print("No statements found in the Cypher file.")
        return

    print(f"Parsed {len(statements)} statement(s) from '{cypher_file}'")

    if dry_run:
        print("\n-- DRY RUN: statements that would be executed --\n")
        for i, stmt in enumerate(statements, 1):
            print(f"[{i}] {stmt[:120]}{'...' if len(stmt) > 120 else ''}")
        return

    print(f"Connecting to {bolt_uri} as '{username}' (database: {database})...")

    try:
        driver = GraphDatabase.driver(bolt_uri, auth=(username, password))
        driver.verify_connectivity()
    except ServiceUnavailable as e:
        print(f"ERROR: Cannot connect to Neo4j at {bolt_uri}\n  {e}", file=sys.stderr)
        sys.exit(1)
    except AuthError as e:
        print(f"ERROR: Authentication failed for user '{username}'\n  {e}", file=sys.stderr)
        sys.exit(1)

    success = 0
    failed = 0

    with driver.session(database=database) as session:
        for i, stmt in enumerate(statements, 1):
            label = stmt[:80].replace("\n", " ") + ("..." if len(stmt) > 80 else "")
            try:
                result = session.run(stmt)
                summary = result.consume()
                counters = summary.counters
                print(
                    f"[{i}/{len(statements)}] OK  "
                    f"nodes+={counters.nodes_created}  "
                    f"rels+={counters.relationships_created}  "
                    f"| {label}"
                )
                success += 1
            except ClientError as e:
                print(f"[{i}/{len(statements)}] FAIL | {label}\n  -> {e.message}", file=sys.stderr)
                failed += 1

    driver.close()

    print(f"\nDone. {success} succeeded, {failed} failed.")
    if failed:
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Execute a Cypher script against Neo4j.")
    parser.add_argument(
        "cypher_file",
        nargs="?",
        default=os.path.join(os.getcwd(), "catalog.cypher"),
        help="Path to the .cypher file (default: ./catalog.cypher)",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--bolt-port", type=int, default=DEFAULT_BOLT_PORT)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print statements without executing them",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.cypher_file):
        print(f"ERROR: File not found: {args.cypher_file}", file=sys.stderr)
        sys.exit(1)

    bolt_uri = f"bolt://{args.host}:{args.bolt_port}"
    run(args.cypher_file, bolt_uri, args.username, args.password, args.database, args.dry_run)


if __name__ == "__main__":
    main()
