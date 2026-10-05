#!/usr/bin/env python3
"""List all tables and views in the given schemas of a Snowflake database."""
import os
import sys
import subprocess
import importlib


def _resolve_dsn(value: str) -> str:
    """Prefer the CLI arg; fall back to the WB_SOURCE_DSN env var (Tier-0
    credential containment) when the arg is empty or an unexpanded reference."""
    v = (value or "").strip()
    if not v or v == "$WB_SOURCE_DSN" or v.startswith("env:"):
        return os.environ.get("WB_SOURCE_DSN", v)
    return v


def ensure_deps():
    for pkg, imp in [("snowflake-connector-python", "snowflake.connector"), ("pyyaml", "yaml")]:
        try:
            importlib.import_module(imp)
        except ImportError:
            print(f"Installing {pkg}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])


ensure_deps()

import argparse
from urllib.parse import urlparse, parse_qs, unquote

import snowflake.connector

parser = argparse.ArgumentParser(description="List all tables in the given schemas of a Snowflake database.")
parser.add_argument("connection_string", help="Snowflake DSN, or '$WB_SOURCE_DSN' to read from env")
parser.add_argument("schemas", nargs="+", help="Schema name(s) to list tables from")
args = parser.parse_args()


def parse_dsn(dsn: str) -> dict:
    parsed = urlparse(dsn)
    qs = parse_qs(parsed.query)
    kwargs = {
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "account": parsed.hostname or "",
        "database": (parsed.path or "").lstrip("/") or None,
    }
    warehouse = qs.get("warehouse", [None])[0]
    role = qs.get("role", [None])[0]
    if warehouse:
        kwargs["warehouse"] = warehouse
    if role:
        kwargs["role"] = role
    return kwargs


schemas = args.schemas

try:
    connect_kwargs = parse_dsn(_resolve_dsn(args.connection_string))
    conn = snowflake.connector.connect(**connect_kwargs)
    cur = conn.cursor()

    placeholders = ", ".join(["%s"] * len(schemas))
    # Enumerate BASE TABLE *and* VIEW — both are discoverable, mappable sources.
    cur.execute(
        f"""
        SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA IN ({placeholders})
          AND TABLE_TYPE IN ('BASE TABLE', 'VIEW')
        ORDER BY TABLE_SCHEMA, TABLE_NAME
        """,
        schemas,
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
except Exception as e:
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)

current_schema = None
for i, (schema, table, table_type) in enumerate(rows, 1):
    if schema != current_schema:
        print(f"\n[{schema}]")
        current_schema = schema
    suffix = " (view)" if table_type == "VIEW" else ""
    print(f"  {i}. {table}{suffix}")
