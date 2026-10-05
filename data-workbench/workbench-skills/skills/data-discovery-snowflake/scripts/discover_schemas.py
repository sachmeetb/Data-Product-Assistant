#!/usr/bin/env python3
"""List all non-system schemas in a Snowflake database."""
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

parser = argparse.ArgumentParser(description="List all non-system schemas in a Snowflake database.")
parser.add_argument("connection_string", nargs="?", default="",
                    help="Snowflake DSN (snowflake://user:password@account/database?warehouse=X&role=Y). "
                         "Optional — defaults to the WB_SOURCE_DSN env var.")
args = parser.parse_args()

# PUBLIC is a normal user schema in Snowflake — keep it; only INFORMATION_SCHEMA
# is system.
SYSTEM_SCHEMAS = {"INFORMATION_SCHEMA"}


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
    schema = qs.get("schema", [None])[0]
    if warehouse:
        kwargs["warehouse"] = warehouse
    if role:
        kwargs["role"] = role
    if schema:
        kwargs["schema"] = schema
    return kwargs


try:
    connect_kwargs = parse_dsn(_resolve_dsn(args.connection_string))
    conn = snowflake.connector.connect(**connect_kwargs)
    cur = conn.cursor()
    cur.execute(
        "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA ORDER BY SCHEMA_NAME"
    )
    schemas = [row[0] for row in cur.fetchall() if row[0] not in SYSTEM_SCHEMAS]
    cur.close()
    conn.close()
except Exception as e:
    print(f"Connection error: {e}", file=sys.stderr)
    sys.exit(1)

print("Available schemas:")
for i, schema in enumerate(schemas, 1):
    print(f"  {i}. {schema}")
