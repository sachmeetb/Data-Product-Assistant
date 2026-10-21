#!/usr/bin/env python3
"""
Patch individual data quality rules in the Neo4j knowledge graph.

Supports targeted deletion of specific :PropertyShape rules without requiring
a full regeneration of the rule set. Useful for removing or correcting rules
that were derived incorrectly from sampled profiling data.

Usage:
    python patch_rules.py <command> [options]

Commands:
    list                  List all rules in the graph, optionally filtered by table
    delete-rule           Delete a single :PropertyShape rule by URI
    delete-table-rules    Delete all rules (NodeShape + PropertyShapes) for a table

Examples:
    python patch_rules.py list
    python patch_rules.py list --table employees.salary
    python patch_rules.py delete-rule rule:employees.salary.employee_id.range
    python patch_rules.py delete-table-rules employees.salary
    python patch_rules.py delete-rule rule:employees.salary.employee_id.range --dry-run

Options:
    --host        Neo4j host (default: localhost)
    --bolt-port   Bolt port (default: 7687)
    --username    Neo4j username (default: neo4j)
    --password    Neo4j password (default: your_password)
    --database    Neo4j database name (default: neo4j)
    --dry-run     Show what would be changed without executing
"""

import sys
import argparse

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


def connect(args):
    bolt_uri = f"bolt://{args.host}:{args.bolt_port}"
    try:
        driver = GraphDatabase.driver(bolt_uri, auth=(args.username, args.password))
        if not getattr(args, "dry_run", False):
            driver.verify_connectivity()
        print(f"Connected to {bolt_uri} as '{args.username}' (database: {args.database})")
        return driver
    except ServiceUnavailable as e:
        print(f"ERROR: Cannot connect to Neo4j at {bolt_uri}\n  {e}", file=sys.stderr)
        sys.exit(1)
    except AuthError as e:
        print(f"ERROR: Authentication failed for user '{args.username}'\n  {e}", file=sys.stderr)
        sys.exit(1)


def cmd_list(args):
    """List rules in the graph, optionally filtered by table."""
    driver = connect(args)
    with driver.session(database=args.database) as session:
        if args.table:
            dataset_uri = f"dataset:{args.table}"
            result = session.run(
                """
                MATCH (ds:Dataset {uri: $uri})-[:HAS_SHAPE]->(ns:NodeShape)-[:PROPERTY]->(ps:PropertyShape)
                OPTIONAL MATCH (ps)-[:ON_COLUMN]->(col:Column)
                RETURN ds.uri AS dataset, ps.uri AS rule_uri, ps.ruleType AS rule_type,
                       ps.path AS column, ps.severity AS severity, ps.confidence AS confidence,
                       ps.description AS description
                ORDER BY ps.ruleType, ps.path
                """,
                uri=dataset_uri,
            )
            rows = list(result)
            if not rows:
                print(f"No rules found for table '{args.table}'. "
                      f"Check the table name or run data-quality-rule-generation first.")
                driver.close()
                return
            print(f"\nRules for {args.table} ({len(rows)} total):\n")
            for row in rows:
                print(f"  {row['rule_uri']}")
                print(f"    type={row['rule_type']}  column={row['column']}  "
                      f"severity={row['severity']}  confidence={row['confidence']}")
                print(f"    {row['description']}")
                print()
        else:
            result = session.run(
                """
                MATCH (ds:Dataset)-[:HAS_SHAPE]->(ns:NodeShape)-[:PROPERTY]->(ps:PropertyShape)
                RETURN ds.uri AS dataset, ps.ruleType AS rule_type, count(ps) AS cnt
                ORDER BY ds.uri, ps.ruleType
                """
            )
            rows = list(result)
            if not rows:
                print("No rules found in the graph. Run data-quality-rule-generation first.")
                driver.close()
                return
            current_ds = None
            total = 0
            for row in rows:
                if row["dataset"] != current_ds:
                    current_ds = row["dataset"]
                    print(f"\n{current_ds}")
                print(f"  {row['rule_type']:25s} {row['cnt']} rule(s)")
                total += row["cnt"]
            print(f"\nTotal: {total} rule(s) across {len(set(r['dataset'] for r in rows))} table(s)")
            print("\nTip: use --table <schema.table> to see individual rule URIs")
    driver.close()


def cmd_delete_rule(args):
    """Delete a single :PropertyShape by URI."""
    rule_uri = args.rule_uri
    driver = connect(args)
    with driver.session(database=args.database) as session:
        # Confirm it exists first
        check = session.run(
            "MATCH (ps:PropertyShape {uri: $uri}) RETURN ps.ruleType AS rt, ps.path AS path, ps.description AS desc",
            uri=rule_uri,
        )
        row = check.single()
        if not row:
            print(f"ERROR: No :PropertyShape found with uri='{rule_uri}'", file=sys.stderr)
            print("Use 'list --table <schema.table>' to see valid rule URIs.")
            driver.close()
            sys.exit(1)

        print(f"\nRule found:")
        print(f"  uri:         {rule_uri}")
        print(f"  rule_type:   {row['rt']}")
        print(f"  column:      {row['path']}")
        print(f"  description: {row['desc']}")

        if args.dry_run:
            print("\n[DRY-RUN] Would delete this :PropertyShape and its relationships.")
            driver.close()
            return

        result = session.run(
            "MATCH (ps:PropertyShape {uri: $uri}) DETACH DELETE ps RETURN count(ps) AS deleted",
            uri=rule_uri,
        )
        deleted = result.single()["deleted"]
        print(f"\nDeleted {deleted} :PropertyShape node(s).")
    driver.close()


def cmd_delete_table_rules(args):
    """Delete all rules (NodeShape + PropertyShapes) for a table."""
    table = args.table
    dataset_uri = f"dataset:{table}"
    driver = connect(args)
    with driver.session(database=args.database) as session:
        # Confirm dataset exists and has rules
        check = session.run(
            """
            MATCH (ds:Dataset {uri: $uri})-[:HAS_SHAPE]->(ns:NodeShape)-[:PROPERTY]->(ps:PropertyShape)
            RETURN count(ps) AS rule_count
            """,
            uri=dataset_uri,
        )
        rule_count = check.single()["rule_count"]
        if rule_count == 0:
            print(f"No rules found for table '{table}'. Nothing to delete.")
            driver.close()
            return

        print(f"\nFound {rule_count} rule(s) for {table}.")

        if args.dry_run:
            print(f"[DRY-RUN] Would delete {rule_count} :PropertyShape node(s) and 1 :NodeShape node.")
            driver.close()
            return

        # Delete PropertyShapes first, then NodeShape
        ps_result = session.run(
            """
            MATCH (ds:Dataset {uri: $uri})-[:HAS_SHAPE]->(ns:NodeShape)-[:PROPERTY]->(ps:PropertyShape)
            DETACH DELETE ps
            RETURN count(ps) AS deleted
            """,
            uri=dataset_uri,
        )
        ps_deleted = ps_result.single()["deleted"]

        ns_result = session.run(
            """
            MATCH (ds:Dataset {uri: $uri})-[:HAS_SHAPE]->(ns:NodeShape)
            DETACH DELETE ns
            RETURN count(ns) AS deleted
            """,
            uri=dataset_uri,
        )
        ns_deleted = ns_result.single()["deleted"]

        print(f"Deleted {ps_deleted} :PropertyShape node(s) and {ns_deleted} :NodeShape node(s) for {table}.")
        print(f"Run data-quality-rule-generation to regenerate rules for this table.")
    driver.close()


def add_connection_args(parser):
    parser.add_argument("--host", default=DEFAULT_HOST, help="Neo4j host (default: %(default)s)")
    parser.add_argument("--bolt-port", type=int, default=DEFAULT_BOLT_PORT, help="Bolt port (default: %(default)s)")
    parser.add_argument("--username", default=DEFAULT_USERNAME, help="Neo4j username (default: %(default)s)")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="Neo4j password (default: %(default)s)")
    parser.add_argument("--database", default=DEFAULT_DATABASE, help="Neo4j database name (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be changed without executing")


def main():
    parser = argparse.ArgumentParser(
        description="Patch individual data quality rules in the Neo4j knowledge graph.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python patch_rules.py list
  python patch_rules.py list --table employees.salary
  python patch_rules.py delete-rule rule:employees.salary.employee_id.range
  python patch_rules.py delete-rule rule:employees.salary.employee_id.range --dry-run
  python patch_rules.py delete-table-rules employees.salary
        """,
    )

    subparsers = parser.add_subparsers(dest="command", metavar="command")
    subparsers.required = True

    # list
    p_list = subparsers.add_parser("list", help="List rules in the graph")
    p_list.add_argument("--table", metavar="SCHEMA.TABLE", help="Filter to a specific table")
    add_connection_args(p_list)

    # delete-rule
    p_del = subparsers.add_parser("delete-rule", help="Delete a single rule by URI")
    p_del.add_argument("rule_uri", metavar="RULE_URI", help="URI of the :PropertyShape to delete (e.g. rule:employees.salary.employee_id.range)")
    add_connection_args(p_del)

    # delete-table-rules
    p_del_tbl = subparsers.add_parser("delete-table-rules", help="Delete all rules for a table")
    p_del_tbl.add_argument("table", metavar="SCHEMA.TABLE", help="Table to clear rules for (e.g. employees.salary)")
    add_connection_args(p_del_tbl)

    args = parser.parse_args()

    if args.command == "list":
        cmd_list(args)
    elif args.command == "delete-rule":
        cmd_delete_rule(args)
    elif args.command == "delete-table-rules":
        cmd_delete_table_rules(args)


if __name__ == "__main__":
    main()
