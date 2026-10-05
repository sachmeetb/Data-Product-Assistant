#!/usr/bin/env python3
"""List all non-system schemas (databases) in a MySQL server."""
import sys
import argparse
from urllib.parse import urlparse

import pymysql  # pymysql>=1.0 — declared in requirements.txt

parser = argparse.ArgumentParser(description="List all non-system schemas in a MySQL server.")
parser.add_argument("connection_string", help="MySQL connection string (mysql://user:password@host:port/database)")
args = parser.parse_args()

parsed = urlparse(args.connection_string)

SYSTEM_SCHEMAS = {"information_schema", "mysql", "performance_schema", "sys"}

try:
    conn = pymysql.connect(
        host=parsed.hostname,
        port=parsed.port or 3306,
        user=parsed.username,
        password=parsed.password or "",
    )
    cur = conn.cursor()
    cur.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA ORDER BY SCHEMA_NAME;")
    schemas = [row[0] for row in cur.fetchall() if row[0] not in SYSTEM_SCHEMAS]
    cur.close()
    conn.close()
except Exception as e:
    print(f"Connection error: {e}", file=sys.stderr)
    sys.exit(1)

print("Available schemas:")
for i, schema in enumerate(schemas, 1):
    print(f"  {i}. {schema}")
