#!/usr/bin/env python3
"""List all tables in the given schemas of a Databricks database.

Works with both Unity Catalog (uses information_schema.tables) and
legacy Hive Metastore (uses SHOW TABLES IN schema).
"""
import sys
import subprocess
import importlib


def ensure_deps():
    for pkg, imp in [("databricks-sql-connector", "databricks.sql"), ("pyyaml", "yaml")]:
        try:
            importlib.import_module(imp)
        except ImportError:
            print(f"Installing {pkg}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])


ensure_deps()

import argparse
from urllib.parse import urlparse, parse_qs, unquote

import databricks.sql

parser = argparse.ArgumentParser(description="List all tables in the given schemas of a Databricks database.")
parser.add_argument("connection_string", help="Databricks connection string")
parser.add_argument("schemas", nargs="+", help="Schema name(s) to list tables from")
args = parser.parse_args()


def parse_dsn(dsn: str) -> dict:
    parsed = urlparse(dsn)
    qs = parse_qs(parsed.query)
    return {
        "server_hostname": parsed.hostname or "",
        "access_token": unquote(parsed.password or ""),
        "http_path": qs.get("http_path", [None])[0],
        "catalog": qs.get("catalog", [None])[0],
        "schema": qs.get("schema", [None])[0],
    }


def connect(dsn_kwargs: dict):
    return databricks.sql.connect(
        server_hostname=dsn_kwargs["server_hostname"],
        http_path=dsn_kwargs["http_path"],
        access_token=dsn_kwargs["access_token"],
    )


schemas = args.schemas
rows_out = []  # list of (schema, table)

try:
    dsn_kwargs = parse_dsn(args.connection_string)
    conn = connect(dsn_kwargs)
    cursor = conn.cursor()

    catalog = dsn_kwargs.get("catalog")

    # Try Unity Catalog path (information_schema.tables)
    used_unity = False
    if catalog:
        try:
            cursor.execute(f"USE CATALOG `{catalog}`")
            placeholders = ", ".join(["%s"] * len(schemas))
            cursor.execute(
                f"""
                SELECT TABLE_SCHEMA, TABLE_NAME
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA IN ({placeholders})
                  AND TABLE_TYPE = 'BASE TABLE'
                ORDER BY TABLE_SCHEMA, TABLE_NAME
                """,
                tuple(schemas),
            )
            rows_out = [(row[0], row[1]) for row in cursor.fetchall()]
            used_unity = True
        except Exception:
            pass

    if not used_unity:
        # Legacy Hive Metastore: SHOW TABLES IN schema
        for schema in schemas:
            cursor.execute(f"SHOW TABLES IN `{schema}`")
            for row in cursor.fetchall():
                # SHOW TABLES returns (database, tableName, isTemporary) or similar
                table_name = row[1] if len(row) > 1 else row[0]
                rows_out.append((schema, table_name))

    cursor.close()
    conn.close()
except Exception as e:
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)

current_schema = None
for i, (schema, table) in enumerate(rows_out, 1):
    if schema != current_schema:
        print(f"\n[{schema}]")
        current_schema = schema
    print(f"  {i}. {table}")
