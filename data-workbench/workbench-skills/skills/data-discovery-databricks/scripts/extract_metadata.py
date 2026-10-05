#!/usr/bin/env python3
"""Extract full metadata for specified Databricks tables and write one YAML file per table.

Works with both Unity Catalog (information_schema) and legacy Hive Metastore (DESCRIBE TABLE).

Output files are named <schema>__<table>.yaml and written to output_dir/data_discovery/.
"""
import sys
import os
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
import re
from urllib.parse import urlparse, parse_qs, unquote

import databricks.sql
import yaml

parser = argparse.ArgumentParser(
    description="Extract full metadata for specified Databricks tables and write one YAML file per table.",
    epilog="Output files are named <schema>__<table>.yaml and written to output_dir/data_discovery/.",
)
parser.add_argument("connection_string", help="Databricks connection string (databricks://token:TOKEN@HOST?http_path=PATH&catalog=CATALOG)")
parser.add_argument("output_dir", help="Directory to write YAML files")
parser.add_argument("tables", nargs="+", metavar="schema.table", help="Tables to extract (format: schema.table)")
args = parser.parse_args()

output_dir = args.output_dir
table_args = args.tables

tables = []
for t in table_args:
    parts = t.split(".", 1)
    if len(parts) != 2:
        print(f"Invalid table spec '{t}' — expected schema.table", file=sys.stderr)
        sys.exit(1)
    tables.append((parts[0], parts[1]))


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


def connect_db(dsn_kwargs: dict):
    return databricks.sql.connect(
        server_hostname=dsn_kwargs["server_hostname"],
        http_path=dsn_kwargs["http_path"],
        access_token=dsn_kwargs["access_token"],
    )


try:
    dsn_kwargs = parse_dsn(args.connection_string)
    conn = connect_db(dsn_kwargs)
    cursor = conn.cursor()

    catalog = dsn_kwargs.get("catalog")
    if catalog:
        try:
            cursor.execute(f"USE CATALOG `{catalog}`")
        except Exception:
            pass
except Exception as e:
    print(f"Connection error: {e}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Unity Catalog helpers (information_schema path)
# ---------------------------------------------------------------------------

def get_columns_unity(cursor, schema: str, table: str) -> list:
    cursor.execute(
        """
        SELECT
            COLUMN_NAME,
            ORDINAL_POSITION,
            DATA_TYPE,
            CHARACTER_MAXIMUM_LENGTH,
            NUMERIC_PRECISION,
            NUMERIC_SCALE,
            IS_NULLABLE,
            COLUMN_DEFAULT,
            COMMENT
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
        """,
        (schema, table),
    )
    return [
        {
            "name": r[0],
            "ordinal": r[1],
            "type": r[2],
            "character_maximum_length": r[3],
            "numeric_precision": r[4],
            "numeric_scale": r[5],
            "nullable": r[6] == "YES",
            "default": r[7],
            "comment": r[8] if r[8] else None,
        }
        for r in cursor.fetchall()
    ]


def get_table_comment_unity(cursor, schema: str, table: str):
    cursor.execute(
        "SELECT COMMENT FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
        (schema, table),
    )
    row = cursor.fetchone()
    if row and row[0]:
        return row[0]
    return None


def get_primary_key_unity(cursor, schema: str, table: str):
    try:
        cursor.execute(
            """
            SELECT tc.CONSTRAINT_NAME, kcu.COLUMN_NAME
            FROM information_schema.TABLE_CONSTRAINTS tc
            JOIN information_schema.KEY_COLUMN_USAGE kcu
                ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
                AND tc.TABLE_NAME = kcu.TABLE_NAME
            WHERE tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME = %s
              AND tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
            ORDER BY kcu.ORDINAL_POSITION
            """,
            (schema, table),
        )
        rows = cursor.fetchall()
        if not rows:
            return None
        return {"constraint_name": rows[0][0], "columns": [r[1] for r in rows]}
    except Exception:
        return None


def get_foreign_keys_unity(cursor, schema: str, table: str) -> list:
    try:
        cursor.execute(
            """
            SELECT
                kcu.CONSTRAINT_NAME,
                kcu.COLUMN_NAME,
                kcu.ORDINAL_POSITION,
                rcu.TABLE_SCHEMA AS REF_SCHEMA,
                rcu.TABLE_NAME AS REF_TABLE,
                rcu.COLUMN_NAME AS REF_COLUMN,
                rc.DELETE_RULE,
                rc.UPDATE_RULE
            FROM information_schema.KEY_COLUMN_USAGE kcu
            JOIN information_schema.REFERENTIAL_CONSTRAINTS rc
                ON kcu.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
                AND kcu.TABLE_SCHEMA = rc.CONSTRAINT_SCHEMA
            JOIN information_schema.KEY_COLUMN_USAGE rcu
                ON rc.UNIQUE_CONSTRAINT_NAME = rcu.CONSTRAINT_NAME
                AND rc.UNIQUE_CONSTRAINT_SCHEMA = rcu.TABLE_SCHEMA
                AND kcu.ORDINAL_POSITION = rcu.ORDINAL_POSITION
            WHERE kcu.TABLE_SCHEMA = %s AND kcu.TABLE_NAME = %s
            ORDER BY kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
            """,
            (schema, table),
        )
        fks: dict = {}
        for r in cursor.fetchall():
            name = r[0]
            if name not in fks:
                fks[name] = {
                    "constraint_name": name,
                    "columns": [],
                    "referenced_schema": r[3],
                    "referenced_table": r[4],
                    "referenced_columns": [],
                    "on_delete": r[6] or "NO ACTION",
                    "on_update": r[7] or "NO ACTION",
                }
            fks[name]["columns"].append(r[1])
            fks[name]["referenced_columns"].append(r[5])
        return list(fks.values())
    except Exception:
        return []


def get_unique_constraints_unity(cursor, schema: str, table: str) -> list:
    try:
        cursor.execute(
            """
            SELECT kcu.CONSTRAINT_NAME, kcu.COLUMN_NAME
            FROM information_schema.KEY_COLUMN_USAGE kcu
            JOIN information_schema.TABLE_CONSTRAINTS tc
                ON kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
                AND kcu.TABLE_SCHEMA = tc.TABLE_SCHEMA
                AND kcu.TABLE_NAME = tc.TABLE_NAME
            WHERE kcu.TABLE_SCHEMA = %s AND kcu.TABLE_NAME = %s
              AND tc.CONSTRAINT_TYPE = 'UNIQUE'
            ORDER BY kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
            """,
            (schema, table),
        )
        ucs: dict = {}
        for r in cursor.fetchall():
            ucs.setdefault(r[0], {"constraint_name": r[0], "columns": []})["columns"].append(r[1])
        return list(ucs.values())
    except Exception:
        return []


def get_check_constraints_unity(cursor, schema: str, table: str) -> list:
    try:
        cursor.execute(
            """
            SELECT tc.CONSTRAINT_NAME, cc.CHECK_CLAUSE
            FROM information_schema.TABLE_CONSTRAINTS tc
            JOIN information_schema.CHECK_CONSTRAINTS cc
                ON tc.CONSTRAINT_NAME = cc.CONSTRAINT_NAME
                AND tc.TABLE_SCHEMA = cc.CONSTRAINT_SCHEMA
            WHERE tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME = %s
              AND tc.CONSTRAINT_TYPE = 'CHECK'
            ORDER BY tc.CONSTRAINT_NAME
            """,
            (schema, table),
        )
        return [{"constraint_name": r[0], "definition": r[1]} for r in cursor.fetchall()]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Legacy Hive Metastore helpers (DESCRIBE TABLE path)
# ---------------------------------------------------------------------------

def get_columns_hive(cursor, schema: str, table: str) -> list:
    """
    Parse DESCRIBE TABLE `schema`.`table` output.

    DESCRIBE returns rows of (col_name, data_type, comment).
    Rows after an empty col_name or '# Partition Information' are metadata, not columns.
    """
    cursor.execute(f"DESCRIBE TABLE `{schema}`.`{table}`")
    rows = cursor.fetchall()
    columns = []
    ordinal = 1
    for row in rows:
        col_name = str(row[0]).strip() if row[0] else ""
        data_type = str(row[1]).strip() if len(row) > 1 and row[1] else ""
        comment = str(row[2]).strip() if len(row) > 2 and row[2] else None

        # Stop at partition/metadata section markers
        if not col_name or col_name.startswith("#") or col_name.startswith("--"):
            break

        columns.append({
            "name": col_name,
            "ordinal": ordinal,
            "type": data_type,
            "character_maximum_length": None,
            "numeric_precision": None,
            "numeric_scale": None,
            "nullable": True,  # Hive Metastore doesn't track nullability precisely
            "default": None,
            "comment": comment if comment else None,
        })
        ordinal += 1
    return columns


def get_table_comment_hive(cursor, schema: str, table: str):
    """Parse table comment from DESCRIBE TABLE EXTENDED output."""
    try:
        cursor.execute(f"DESCRIBE TABLE EXTENDED `{schema}`.`{table}`")
        rows = cursor.fetchall()
        for row in rows:
            if row[0] and str(row[0]).strip().lower() == "comment":
                val = str(row[1]).strip() if row[1] else None
                return val if val else None
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Per-table extractor (tries Unity Catalog, falls back to Hive)
# ---------------------------------------------------------------------------

def extract_table(cursor, schema: str, table: str, catalog: str | None) -> dict:
    """Extract metadata for one table, auto-detecting Unity Catalog vs Hive Metastore."""
    # Try Unity Catalog path first
    if catalog:
        try:
            cols = get_columns_unity(cursor, schema, table)
            if cols:
                return {
                    "schema": schema,
                    "table": table,
                    "comment": get_table_comment_unity(cursor, schema, table),
                    "columns": cols,
                    "primary_key": get_primary_key_unity(cursor, schema, table),
                    "foreign_keys": get_foreign_keys_unity(cursor, schema, table),
                    "unique_constraints": get_unique_constraints_unity(cursor, schema, table),
                    "indexes": [],
                    "check_constraints": get_check_constraints_unity(cursor, schema, table),
                }
        except Exception:
            pass  # fall through to Hive path

    # Hive Metastore path
    return {
        "schema": schema,
        "table": table,
        "comment": get_table_comment_hive(cursor, schema, table),
        "columns": get_columns_hive(cursor, schema, table),
        "primary_key": None,
        "foreign_keys": [],
        "unique_constraints": [],
        "indexes": [],
        "check_constraints": [],
    }


# ---------------------------------------------------------------------------
# Main extraction loop
# ---------------------------------------------------------------------------

output_dir = os.path.join(output_dir, "data_discovery")
os.makedirs(output_dir, exist_ok=True)
written = []
failed = []

for schema, table in tables:
    try:
        metadata = extract_table(cursor, schema, table, catalog)
        filename = f"{schema}__{table}.yaml"
        filepath = os.path.join(output_dir, filename)
        with open(filepath, "w") as f:
            yaml.dump(metadata, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        written.append(filename)
        print(f"  Written: {filename}")
    except Exception as e:
        print(f"  WARNING: Failed {schema}.{table}: {e}", file=sys.stderr)
        failed.append(f"{schema}.{table}")

cursor.close()
conn.close()

print(f"\nDone. {len(written)} file(s) written to {output_dir}")
if failed:
    print(f"Failed: {', '.join(failed)}", file=sys.stderr)
    sys.exit(1)
