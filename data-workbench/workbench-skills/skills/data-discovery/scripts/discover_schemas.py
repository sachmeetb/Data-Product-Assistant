#!/usr/bin/env python3
"""List all non-system schemas in a PostgreSQL database."""
import sys
import argparse

try:
    import psycopg2
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "psycopg2-binary", "-q"])
    import psycopg2

parser = argparse.ArgumentParser(description="List all non-system schemas in a PostgreSQL database.")
parser.add_argument("connection_string", help="PostgreSQL connection string")
args = parser.parse_args()

conn_str = args.connection_string

try:
    conn = psycopg2.connect(conn_str)
    cur = conn.cursor()
    cur.execute("""
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name NOT IN ('information_schema', 'pg_catalog', 'pg_toast')
          AND schema_name NOT LIKE 'pg_%'
        ORDER BY schema_name;
    """)
    schemas = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()
except Exception as e:
    print(f"Connection error: {e}", file=sys.stderr)
    sys.exit(1)

print("Available schemas:")
for i, schema in enumerate(schemas, 1):
    print(f"  {i}. {schema}")
