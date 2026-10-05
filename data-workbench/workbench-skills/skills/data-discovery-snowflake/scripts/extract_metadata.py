#!/usr/bin/env python3
"""Extract full metadata for specified Snowflake tables and write one YAML file per table.

Output files are named <schema>__<table>.yaml and written to output_dir/data_discovery/.

Identifiers are used against INFORMATION_SCHEMA *exactly as passed* (no forced
uppercase) — the schema/table names come from discover_tables.py, which returns
the identifiers exactly as Snowflake stores them (UPPER for unquoted objects,
verbatim for quoted mixed-case). Forcing uppercase here would fail to resolve
quoted mixed-case objects.

Performance: Snowflake's INFORMATION_SCHEMA is ~0.8s per query, so the metadata
is fetched **once per schema** (all requested tables at once, grouped in-process)
rather than 6 queries per table — a per-table fan-out dominates discovery time
and scales badly with table count.
"""
import sys
import os
import subprocess
import importlib


def ensure_deps():
    for pkg, imp in [("snowflake-connector-python", "snowflake.connector"), ("pyyaml", "yaml")]:
        try:
            importlib.import_module(imp)
        except ImportError:
            print(f"Installing {pkg}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])


def _resolve_dsn(value: str) -> str:
    """Prefer the CLI arg; fall back to the WB_SOURCE_DSN env var (Tier-0
    credential containment) when the arg is empty or an unexpanded reference."""
    v = (value or "").strip()
    if not v or v == "$WB_SOURCE_DSN" or v.startswith("env:"):
        return os.environ.get("WB_SOURCE_DSN", v)
    return v


ensure_deps()

import argparse
from urllib.parse import urlparse, parse_qs, unquote

import snowflake.connector
import yaml

parser = argparse.ArgumentParser(
    description="Extract full metadata for specified Snowflake tables and write one YAML file per table.",
    epilog="Output files are named <schema>__<table>.yaml and written to output_dir/data_discovery/.",
)
parser.add_argument("connection_string", help="Snowflake connection string (snowflake://user:password@account/database?warehouse=X&role=Y)")
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


try:
    connect_kwargs = parse_dsn(_resolve_dsn(args.connection_string))
    conn = snowflake.connector.connect(**connect_kwargs)
    cur = conn.cursor()
except Exception as e:
    print(f"Connection error: {e}", file=sys.stderr)
    sys.exit(1)


def dq(name: str) -> str:
    """Double-quote a Snowflake identifier."""
    return '"' + name.replace('"', '""') + '"'


def _in(n: int) -> str:
    return ", ".join(["%s"] * n)


# ── batched INFORMATION_SCHEMA fetchers (one query per schema, grouped by table) ──

def get_table_comments(cur, schema, tbls) -> dict:
    cur.execute(
        f"SELECT TABLE_NAME, COMMENT FROM INFORMATION_SCHEMA.TABLES "
        f"WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN ({_in(len(tbls))})",
        (schema, *tbls),
    )
    return {r[0]: (r[1] or None) for r in cur.fetchall()}


def get_columns(cur, schema, tbls) -> dict:
    cur.execute(
        f"""
        SELECT TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, DATA_TYPE,
               CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE,
               IS_NULLABLE, COLUMN_DEFAULT, COMMENT
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN ({_in(len(tbls))})
        ORDER BY TABLE_NAME, ORDINAL_POSITION
        """,
        (schema, *tbls),
    )
    out: dict = {}
    for r in cur.fetchall():
        out.setdefault(r[0], []).append({
            "name": r[1], "ordinal": r[2], "type": r[3],
            "character_maximum_length": r[4], "numeric_precision": r[5],
            "numeric_scale": r[6], "nullable": r[7] == "YES",
            "default": r[8], "comment": r[9] if r[9] else None,
        })
    return out


def get_primary_keys(cur, schema, tbls) -> dict:
    cur.execute(
        f"""
        SELECT tc.TABLE_NAME, tc.CONSTRAINT_NAME, kcu.COLUMN_NAME
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
            ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
            AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
            AND tc.TABLE_NAME = kcu.TABLE_NAME
        WHERE tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME IN ({_in(len(tbls))})
          AND tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
        ORDER BY tc.TABLE_NAME, kcu.ORDINAL_POSITION
        """,
        (schema, *tbls),
    )
    out: dict = {}
    for r in cur.fetchall():
        entry = out.setdefault(r[0], {"constraint_name": r[1], "columns": []})
        entry["columns"].append(r[2])
    return out


def get_foreign_keys(cur, schema, tbls) -> dict:
    cur.execute(
        f"""
        SELECT
            kcu.TABLE_NAME, kcu.CONSTRAINT_NAME, kcu.COLUMN_NAME, kcu.ORDINAL_POSITION,
            rcu.TABLE_SCHEMA AS REF_SCHEMA, rcu.TABLE_NAME AS REF_TABLE,
            rcu.COLUMN_NAME AS REF_COLUMN, rc.DELETE_RULE, rc.UPDATE_RULE
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
        JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
            ON kcu.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
            AND kcu.TABLE_SCHEMA = rc.CONSTRAINT_SCHEMA
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE rcu
            ON rc.UNIQUE_CONSTRAINT_NAME = rcu.CONSTRAINT_NAME
            AND rc.UNIQUE_CONSTRAINT_SCHEMA = rcu.TABLE_SCHEMA
            AND kcu.ORDINAL_POSITION = rcu.ORDINAL_POSITION
        WHERE kcu.TABLE_SCHEMA = %s AND kcu.TABLE_NAME IN ({_in(len(tbls))})
        ORDER BY kcu.TABLE_NAME, kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
        """,
        (schema, *tbls),
    )
    grouped: dict = {}  # {table: {constraint_name: fk}}
    for r in cur.fetchall():
        tbl, name = r[0], r[1]
        by_name = grouped.setdefault(tbl, {})
        if name not in by_name:
            by_name[name] = {
                "constraint_name": name, "columns": [],
                "referenced_schema": r[4], "referenced_table": r[5],
                "referenced_columns": [], "on_delete": r[7] or "NO ACTION",
                "on_update": r[8] or "NO ACTION",
            }
        by_name[name]["columns"].append(r[2])
        by_name[name]["referenced_columns"].append(r[6])
    return {t: list(d.values()) for t, d in grouped.items()}


def get_unique_constraints(cur, schema, tbls) -> dict:
    cur.execute(
        f"""
        SELECT kcu.TABLE_NAME, kcu.CONSTRAINT_NAME, kcu.COLUMN_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
        JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
            ON kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
            AND kcu.TABLE_SCHEMA = tc.TABLE_SCHEMA
            AND kcu.TABLE_NAME = tc.TABLE_NAME
        WHERE kcu.TABLE_SCHEMA = %s AND kcu.TABLE_NAME IN ({_in(len(tbls))})
          AND tc.CONSTRAINT_TYPE = 'UNIQUE'
        ORDER BY kcu.TABLE_NAME, kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
        """,
        (schema, *tbls),
    )
    grouped: dict = {}  # {table: {constraint_name: uc}}
    for r in cur.fetchall():
        by_name = grouped.setdefault(r[0], {})
        by_name.setdefault(r[1], {"constraint_name": r[1], "columns": []})["columns"].append(r[2])
    return {t: list(d.values()) for t, d in grouped.items()}


def get_check_constraints(cur, schema, tbls) -> dict:
    try:
        cur.execute(
            f"""
            SELECT tc.TABLE_NAME, tc.CONSTRAINT_NAME, cc.CHECK_CLAUSE
            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
            JOIN INFORMATION_SCHEMA.CHECK_CONSTRAINTS cc
                ON tc.CONSTRAINT_NAME = cc.CONSTRAINT_NAME
                AND tc.TABLE_SCHEMA = cc.CONSTRAINT_SCHEMA
            WHERE tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME IN ({_in(len(tbls))})
              AND tc.CONSTRAINT_TYPE = 'CHECK'
            ORDER BY tc.TABLE_NAME, tc.CONSTRAINT_NAME
            """,
            (schema, *tbls),
        )
        out: dict = {}
        for r in cur.fetchall():
            out.setdefault(r[0], []).append({"constraint_name": r[1], "definition": r[2]})
        return out
    except Exception:
        return {}


output_dir = os.path.join(output_dir, "data_discovery")
os.makedirs(output_dir, exist_ok=True)
written = []
failed = []

# Group requested tables by schema → one round of 6 batched queries per schema.
by_schema: dict = {}
for schema, table in tables:
    by_schema.setdefault(schema, []).append(table)

for schema, sch_tables in by_schema.items():
    try:
        comments = get_table_comments(cur, schema, sch_tables)
        columns = get_columns(cur, schema, sch_tables)
        pks = get_primary_keys(cur, schema, sch_tables)
        fks = get_foreign_keys(cur, schema, sch_tables)
        uniques = get_unique_constraints(cur, schema, sch_tables)
        checks = get_check_constraints(cur, schema, sch_tables)
    except Exception as e:
        print(f"  WARNING: Failed schema {schema}: {e}", file=sys.stderr)
        failed.extend(f"{schema}.{t}" for t in sch_tables)
        continue

    for table in sch_tables:
        try:
            metadata = {
                "schema": schema,
                "table": table,
                "comment": comments.get(table),
                "columns": columns.get(table, []),
                "primary_key": pks.get(table),
                "foreign_keys": fks.get(table, []),
                "unique_constraints": uniques.get(table, []),
                "indexes": [],  # Snowflake does not expose user-visible index metadata via INFORMATION_SCHEMA
                "check_constraints": checks.get(table, []),
            }
            filename = f"{schema}__{table}.yaml"
            filepath = os.path.join(output_dir, filename)
            with open(filepath, "w") as f:
                yaml.dump(metadata, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            written.append(filename)
            print(f"  Written: {filename}")
        except Exception as e:
            print(f"  WARNING: Failed {schema}.{table}: {e}", file=sys.stderr)
            failed.append(f"{schema}.{table}")

cur.close()
conn.close()

print(f"\nDone. {len(written)} file(s) written to {output_dir}")
if failed:
    print(f"Failed: {', '.join(failed)}", file=sys.stderr)
    sys.exit(1)
