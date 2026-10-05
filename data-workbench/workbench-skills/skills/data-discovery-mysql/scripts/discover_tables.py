#!/usr/bin/env python3
"""List all tables in the given schemas (databases) of a MySQL server."""
import sys
import argparse
from urllib.parse import urlparse

import pymysql  # pymysql>=1.0 — declared in requirements.txt

parser = argparse.ArgumentParser(description="List all tables in the given schemas of a MySQL server.")
parser.add_argument("connection_string", help="MySQL connection string")
parser.add_argument("schemas", nargs="+", help="Schema (database) name(s) to list tables from")
args = parser.parse_args()

parsed = urlparse(args.connection_string)
schemas = args.schemas

try:
    conn = pymysql.connect(
        host=parsed.hostname,
        port=parsed.port or 3306,
        user=parsed.username,
        password=parsed.password or "",
    )
    cur = conn.cursor()
    placeholders = ", ".join(["%s"] * len(schemas))
    cur.execute(f"""
        SELECT TABLE_SCHEMA, TABLE_NAME
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA IN ({placeholders})
          AND TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_SCHEMA, TABLE_NAME;
    """, schemas)
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
