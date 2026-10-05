#!/usr/bin/env python3
"""
Load a data quality rule Cypher script into Neo4j, with pre-flight checks per table.

Pre-flight checks (done at load time):
  1. The :Dataset node must exist in the graph.
  2. The :Dataset must not already have a [:HAS_SHAPE]->(:NodeShape) relationship.
     If it does, the table is skipped — rules already loaded.

Usage:
    python run_rules_cypher.py [cypher_file] [options]

    cypher_file   path to the .cypher file (default: ./dq_rules.cypher)

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
import re
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

TABLE_SECTION_RE = re.compile(r"^// === Table: (\S+\.\S+) ===$")


def parse_file(filepath: str) -> tuple[list[str], list[dict]]:
    """
    Parse the rule Cypher file into:
      - header_stmts: statements before the first table section (none expected, but handled)
      - sections: list of {'table': str, 'dataset_uri': str, 'stmts': list[str]}
    """
    header_lines: list[str] = []
    sections: list[dict] = []
    current_section: dict | None = None
    current_lines: list[str] = []

    with open(filepath) as fh:
        for raw_line in fh:
            line = raw_line.rstrip("\n")
            m = TABLE_SECTION_RE.match(line)
            if m:
                if current_section is None:
                    header_lines = current_lines
                else:
                    current_section["stmts"] = extract_statements(current_lines)
                    sections.append(current_section)
                table = m.group(1)
                current_section = {
                    "table": table,
                    "dataset_uri": None,  # resolved from Cypher below
                    "stmts": [],
                }
                current_lines = []
            else:
                current_lines.append(line)

    if current_section is None:
        header_lines = current_lines
    else:
        current_section["stmts"] = extract_statements(current_lines)
        sections.append(current_section)

    # Resolve dataset_uri from Cypher content for each section
    ds_uri_re = re.compile(r"Dataset \{uri: '([^']+)'\}")
    for sec in sections:
        if sec["dataset_uri"] is None:
            for stmt in sec["stmts"]:
                uri_match = ds_uri_re.search(stmt)
                if uri_match:
                    sec["dataset_uri"] = uri_match.group(1)
                    break
            if sec["dataset_uri"] is None:
                sec["dataset_uri"] = f"dataset:{sec['table']}"

    return extract_statements(header_lines), sections


def extract_statements(lines: list[str]) -> list[str]:
    """Extract Cypher statements — each ends when a non-comment line ends with ';'."""
    statements: list[str] = []
    current: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("//") or stripped == "":
            continue
        current.append(line)
        if stripped.endswith(";"):
            stmt = "\n".join(current).strip().rstrip(";").strip()
            if stmt:
                statements.append(stmt)
            current = []

    if current:
        stmt = "\n".join(current).strip()
        if stmt:
            statements.append(stmt)

    return statements


def check_table(session, dataset_uri: str) -> tuple[bool, bool]:
    """
    Returns (node_exists, already_has_rules).

    node_exists       — True if a :Dataset node with this uri exists.
    already_has_rules — True if the Dataset already has a [:HAS_SHAPE]->(:NodeShape).
    """
    result = session.run(
        "MATCH (ds:Dataset {uri: $uri}) RETURN count(ds) AS n",
        uri=dataset_uri,
    )
    node_exists = result.single()["n"] > 0

    if not node_exists:
        return False, False

    result = session.run(
        "MATCH (ds:Dataset {uri: $uri})-[:HAS_SHAPE]->(:NodeShape) RETURN count(*) AS n",
        uri=dataset_uri,
    )
    already_has_rules = result.single()["n"] > 0

    return True, already_has_rules


def run_statements(session, stmts: list[str], dry_run: bool) -> tuple[int, int]:
    """Execute (or dry-run) a list of Cypher statements. Returns (succeeded, failed)."""
    succeeded = 0
    failed = 0

    for i, stmt in enumerate(stmts, 1):
        label = stmt.replace("\n", " ")[:80] + ("..." if len(stmt) > 80 else "")

        if dry_run:
            print(f"  [DRY-RUN {i}/{len(stmts)}] {label}")
            succeeded += 1
            continue

        try:
            result = session.run(stmt)
            summary = result.consume()
            c = summary.counters
            print(
                f"  [{i}/{len(stmts)}] OK  "
                f"nodes+={c.nodes_created}  "
                f"rels+={c.relationships_created}  "
                f"props+={c.properties_set}  "
                f"| {label}"
            )
            succeeded += 1
        except ClientError as e:
            print(f"  [{i}/{len(stmts)}] FAIL | {label}\n    -> {e.message}", file=sys.stderr)
            failed += 1

    return succeeded, failed


def main():
    parser = argparse.ArgumentParser(
        description="Load a data quality rule Cypher script into Neo4j with pre-flight checks.",
    )
    parser.add_argument(
        "cypher_file",
        nargs="?",
        default=os.path.join(os.getcwd(), "dq_rules.cypher"),
        help="Path to the rules .cypher file (default: ./dq_rules.cypher)",
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

    print(f"Parsing: {args.cypher_file}")
    header_stmts, sections = parse_file(args.cypher_file)
    print(f"  Header statements: {len(header_stmts)}")
    print(f"  Table sections:    {len(sections)}")
    for s in sections:
        print(f"    {s['table']}: {len(s['stmts'])} statement(s)")
    print()

    if args.dry_run:
        print("[DRY-RUN MODE — no changes will be made]\n")

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

    total_ok = 0
    total_fail = 0
    skipped: list[tuple[str, str]] = []

    with driver.session(database=args.database) as session:
        # Header (unlikely to have content, but handled for consistency)
        if header_stmts:
            print(f"Header: {len(header_stmts)} statement(s)...")
            ok, fail = run_statements(session, header_stmts, args.dry_run)
            total_ok += ok
            total_fail += fail
            print()

        for sec in sections:
            table = sec["table"]
            dataset_uri = sec["dataset_uri"]
            stmts = sec["stmts"]

            print(f"Table: {table}")

            if not args.dry_run:
                node_exists, already_has_rules = check_table(session, dataset_uri)

                if not node_exists:
                    print(
                        f"  SKIP — :Dataset node '{dataset_uri}' not found.\n"
                        "         Run data-discovery-to-dcat-neo4j first."
                    )
                    skipped.append((table, "Dataset node not found"))
                    print()
                    continue

                if already_has_rules:
                    print(
                        f"  SKIP — {table} already has rules in the graph.\n"
                        "         To regenerate: MATCH (n:PropertyShape) DETACH DELETE n; "
                        "MATCH (n:NodeShape) DETACH DELETE n;"
                    )
                    skipped.append((table, "already has rules"))
                    print()
                    continue

                print(f"  Pre-flight OK — loading {len(stmts)} statement(s)...")
            else:
                print(f"  [DRY-RUN] Would load {len(stmts)} statement(s)...")

            ok, fail = run_statements(session, stmts, args.dry_run)
            total_ok += ok
            total_fail += fail
            print()

    driver.close()

    print(f"Done. {total_ok} succeeded, {total_fail} failed, {len(skipped)} table(s) skipped.")
    if skipped:
        print("Skipped:")
        for table, reason in skipped:
            print(f"  {table}: {reason}")

    if total_fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
