#!/usr/bin/env python3
"""List all tables in the given schemas of a PostgreSQL database."""
import sys
import argparse

try:
    import psycopg2
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "psycopg2-binary", "-q"])
    import psycopg2

parser = argparse.ArgumentParser(description="List all tables in the given schemas of a PostgreSQL database.")
parser.add_argument("connection_string", help="PostgreSQL connection string")
parser.add_argument("schemas", nargs="+", help="Schema name(s) to list tables from")
args = parser.parse_args()

conn_str = args.connection_string
schemas = args.schemas

try:
    conn = psycopg2.connect(conn_str)
    cur = conn.cursor()
    cur.execute("""
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema = ANY(%s)
          AND table_type = 'BASE TABLE'
        ORDER BY table_schema, table_name;
    """, (schemas,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
except Exception as e:
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)

current_schema = None
for i, (schema, table) in enumerate(rows, 1):
    if schema != current_schema:
        print(f"\n[{schema}]")
        current_schema = schema
    print(f"  {i}. {table}")
