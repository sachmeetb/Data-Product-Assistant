#!/usr/bin/env python3
"""List all non-system schemas in a Databricks database.

Works with both Unity Catalog (uses information_schema.schemata) and
legacy Hive Metastore (uses SHOW SCHEMAS).
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

parser = argparse.ArgumentParser(description="List all non-system schemas in a Databricks database.")
parser.add_argument("connection_string", help="Databricks connection string (databricks://token:TOKEN@HOST?http_path=PATH&catalog=CATALOG)")
args = parser.parse_args()

SYSTEM_SCHEMAS = {"information_schema", "sys"}


def parse_dsn(dsn: str) -> dict:
    """Parse databricks://token:TOKEN@HOST?http_path=PATH&catalog=CATALOG."""
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
    kwargs = {
        "server_hostname": dsn_kwargs["server_hostname"],
        "http_path": dsn_kwargs["http_path"],
        "access_token": dsn_kwargs["access_token"],
    }
    return databricks.sql.connect(**kwargs)


try:
    dsn_kwargs = parse_dsn(args.connection_string)
    conn = connect(dsn_kwargs)
    cursor = conn.cursor()

    catalog = dsn_kwargs.get("catalog")

    # Try Unity Catalog path first (information_schema available when catalog is set)
    schemas = []
    used_unity = False
    if catalog:
        try:
            cursor.execute(f"USE CATALOG `{catalog}`")
            cursor.execute(
                "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA ORDER BY SCHEMA_NAME"
            )
            schemas = [
                row[0] for row in cursor.fetchall()
                if row[0].lower() not in SYSTEM_SCHEMAS
            ]
            used_unity = True
        except Exception:
            pass  # fall through to SHOW SCHEMAS

    if not used_unity:
        # Legacy Hive Metastore path
        cursor.execute("SHOW SCHEMAS")
        rows = cursor.fetchall()
        # SHOW SCHEMAS returns (databaseName,) or (namespace,) depending on DBR version
        schemas = [
            row[0] for row in rows
            if row[0].lower() not in SYSTEM_SCHEMAS
        ]

    cursor.close()
    conn.close()
except Exception as e:
    print(f"Connection error: {e}", file=sys.stderr)
    sys.exit(1)

print("Available schemas:")
for i, schema in enumerate(schemas, 1):
    print(f"  {i}. {schema}")
