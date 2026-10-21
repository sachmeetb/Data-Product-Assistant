#!/usr/bin/env python3
"""
Execute remediation SQL scripts against PostgreSQL and record provenance in Neo4j.

Reads SQL files from remediation/sql/, executes them against the target database,
and creates :RemediationAction nodes in Neo4j with full provenance.

Usage:
    python apply_remediation.py --pg-connection "postgresql://..." [options]

Options:
    --pg-connection  PostgreSQL connection string
    --sql-dir        Directory containing SQL files (default: remediation/sql)
    --dry-run        Show what would be executed without running
    --host           Neo4j host (default: localhost)
    --bolt-port      Bolt port (default: 7687)
    --username       Neo4j username (default: neo4j)
    --password       Neo4j password (default: your_password)
    --database       Neo4j database name (default: neo4j)
"""

import argparse
import glob
import os
import sys
from datetime import datetime, timezone

try:
    import psycopg2
except ImportError:
    psycopg2 = None

try:
    from neo4j import GraphDatabase
    from neo4j.exceptions import ServiceUnavailable, AuthError
except ImportError:
    print("ERROR: neo4j driver required.", file=sys.stderr)
    sys.exit(1)

DEFAULT_HOST = "localhost"
DEFAULT_BOLT_PORT = 7687
DEFAULT_USERNAME = "neo4j"
DEFAULT_PASSWORD = "your_password"
DEFAULT_DATABASE = "neo4j"

RECORD_REMEDIATION = """\
MATCH (col:Column {uri: $col_uri})
CREATE (ra:RemediationAction {
    uri: $uri,
    actionType: $action_type,
    description: $description,
    sql: $sql,
    rowsAffected: $rows_affected,
    appliedAt: datetime($applied_at),
    batchId: $batch_id
})
CREATE (col)-[:HAS_REMEDIATION]->(ra)
CREATE (act:ProvActivity {
    activityType: 'remediation',
    outcome: 'applied',
    occurredAt: $applied_at
})
MERGE (agent:ProvAgent {uri: 'prov:agent:system:data-remediation'})
ON CREATE SET agent.agentType = 'system', agent.name = 'data-remediation skill'
CREATE (ra)-[:PROV_WAS_GENERATED_BY]->(act)
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
RETURN ra.uri AS uri
"""


def main():
    parser = argparse.ArgumentParser(description="Apply remediation SQL and record provenance.")
    parser.add_argument("--pg-connection", required=True, help="PostgreSQL connection string")
    parser.add_argument("--sql-dir", default="remediation/sql", help="Directory with SQL files")
    parser.add_argument("--dry-run", action="store_true", help="Preview without executing")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--bolt-port", type=int, default=DEFAULT_BOLT_PORT)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    args = parser.parse_args()

    sql_dir = os.path.join(os.getcwd(), args.sql_dir)
    sql_files = sorted(glob.glob(os.path.join(sql_dir, "*.sql")))

    if not sql_files:
        print(f"No SQL files found in {sql_dir}")
        return

    print(f"Found {len(sql_files)} SQL file(s) to apply:")
    for f in sql_files:
        print(f"  {os.path.basename(f)}")

    if args.dry_run:
        print("\n[DRY RUN] Would execute the following:")
        for sql_file in sql_files:
            with open(sql_file) as f:
                content = f.read()
            print(f"\n--- {os.path.basename(sql_file)} ---")
            print(content)
        print("\n[DRY RUN] No changes made.")
        return

    if psycopg2 is None:
        print("ERROR: psycopg2 is required for SQL execution. Install with: pip install psycopg2-binary", file=sys.stderr)
        print("SQL files are available for manual execution in:", sql_dir)
        sys.exit(1)

    # Execute SQL
    now = datetime.now(timezone.utc)
    batch_id = f"remediation:{now.isoformat()}"
    applied_at = now.isoformat()

    try:
        conn = psycopg2.connect(args.pg_connection)
        conn.autocommit = False
    except Exception as e:
        print(f"ERROR: Cannot connect to PostgreSQL: {e}", file=sys.stderr)
        sys.exit(1)

    results = []
    for sql_file in sql_files:
        with open(sql_file) as f:
            content = f.read()

        table_name = os.path.basename(sql_file).replace("_remediation.sql", "").replace("_", ".")
        print(f"\nApplying {os.path.basename(sql_file)}...")

        try:
            cur = conn.cursor()
            cur.execute(content)
            rows_affected = cur.rowcount if cur.rowcount >= 0 else 0
            conn.commit()
            print(f"  Applied successfully. Rows affected: {rows_affected}")
            results.append({
                "file": os.path.basename(sql_file),
                "table": table_name,
                "rows_affected": rows_affected,
                "status": "applied",
                "sql": content,
            })
        except Exception as e:
            conn.rollback()
            print(f"  ERROR: {e}")
            results.append({
                "file": os.path.basename(sql_file),
                "table": table_name,
                "rows_affected": 0,
                "status": "failed",
                "error": str(e),
            })

    conn.close()

    # Record provenance in Neo4j
    bolt_uri = f"bolt://{args.host}:{args.bolt_port}"
    print(f"\nRecording provenance in Neo4j ({bolt_uri})...")

    try:
        driver = GraphDatabase.driver(bolt_uri, auth=(args.username, args.password))
        driver.verify_connectivity()
    except (ServiceUnavailable, AuthError) as e:
        print(f"WARNING: Cannot connect to Neo4j for provenance: {e}")
        driver = None

    if driver:
        with driver.session(database=args.database) as session:
            for result in results:
                if result["status"] != "applied":
                    continue
                table = result["table"]
                uri = f"remediation:{table}:{batch_id}"
                try:
                    session.run(
                        RECORD_REMEDIATION,
                        col_uri=f"column:{table}",  # approximate — links to table-level
                        uri=uri,
                        action_type="sql_execution",
                        description=f"Applied remediation to {table}",
                        sql=result["sql"][:1000],  # truncate for graph storage
                        rows_affected=result["rows_affected"],
                        applied_at=applied_at,
                        batch_id=batch_id,
                    )
                except Exception as e:
                    print(f"  WARNING: Could not record provenance for {table}: {e}")
        driver.close()

    # Summary
    applied = [r for r in results if r["status"] == "applied"]
    failed = [r for r in results if r["status"] == "failed"]
    total_rows = sum(r["rows_affected"] for r in applied)

    print(f"\nRemediation Summary:")
    print(f"  Applied: {len(applied)}/{len(results)} files")
    print(f"  Total rows affected: {total_rows}")
    if failed:
        print(f"  Failed: {len(failed)}")
        for f in failed:
            print(f"    {f['file']}: {f.get('error', 'unknown')}")


if __name__ == "__main__":
    main()
