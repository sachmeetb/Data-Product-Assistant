#!/usr/bin/env python3
"""Extract full metadata for specified MySQL tables and write one YAML file per table.

Output files are named <schema>__<table>.yaml and written to output_dir.
"""
import sys
import os
import argparse
from urllib.parse import urlparse

import pymysql  # pymysql>=1.0 — declared in requirements.txt
import yaml     # pyyaml>=6.0 — declared in requirements.txt

parser = argparse.ArgumentParser(
    description="Extract full metadata for specified MySQL tables and write one YAML file per table.",
    epilog="Output files are named <schema>__<table>.yaml and written to output_dir.",
)
parser.add_argument("connection_string", help="MySQL connection string (mysql://user:password@host:port/database)")
parser.add_argument("output_dir", help="Directory to write YAML files")
parser.add_argument("tables", nargs="+", metavar="schema.table", help="Tables to extract (format: schema.table)")
args = parser.parse_args()

parsed_url = urlparse(args.connection_string)
output_dir = args.output_dir
table_args = args.tables

tables = []
for t in table_args:
    parts = t.split(".", 1)
    if len(parts) != 2:
        print(f"Invalid table spec '{t}' — expected schema.table", file=sys.stderr)
        sys.exit(1)
    tables.append((parts[0], parts[1]))

try:
    conn = pymysql.connect(
        host=parsed_url.hostname,
        port=parsed_url.port or 3306,
        user=parsed_url.username,
        password=parsed_url.password or "",
    )
    cur = conn.cursor()
except Exception as e:
    print(f"Connection error: {e}", file=sys.stderr)
    sys.exit(1)


def get_table_comment(cur, schema, table):
    cur.execute("""
        SELECT TABLE_COMMENT
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s;
    """, (schema, table))
    row = cur.fetchone()
    if row and row[0]:
        return row[0]
    return None


def get_columns(cur, schema, table):
    cur.execute("""
        SELECT
            COLUMN_NAME,
            ORDINAL_POSITION,
            DATA_TYPE,
            CHARACTER_MAXIMUM_LENGTH,
            NUMERIC_PRECISION,
            NUMERIC_SCALE,
            IS_NULLABLE,
            COLUMN_DEFAULT,
            COLUMN_COMMENT
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION;
    """, (schema, table))
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
        for r in cur.fetchall()
    ]


def get_primary_key(cur, schema, table):
    cur.execute("""
        SELECT 'PRIMARY', COLUMN_NAME
        FROM information_schema.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
          AND CONSTRAINT_NAME = 'PRIMARY'
        ORDER BY ORDINAL_POSITION;
    """, (schema, table))
    rows = cur.fetchall()
    if not rows:
        return None
    return {"constraint_name": "PRIMARY", "columns": [r[1] for r in rows]}


def get_foreign_keys(cur, schema, table):
    cur.execute("""
        SELECT
            kcu.CONSTRAINT_NAME,
            kcu.COLUMN_NAME,
            kcu.REFERENCED_TABLE_SCHEMA,
            kcu.REFERENCED_TABLE_NAME,
            kcu.REFERENCED_COLUMN_NAME,
            rc.DELETE_RULE,
            rc.UPDATE_RULE
        FROM information_schema.KEY_COLUMN_USAGE kcu
        JOIN information_schema.REFERENTIAL_CONSTRAINTS rc
            ON kcu.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
            AND kcu.TABLE_SCHEMA = rc.CONSTRAINT_SCHEMA
        WHERE kcu.TABLE_SCHEMA = %s AND kcu.TABLE_NAME = %s
          AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
        ORDER BY kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION;
    """, (schema, table))
    fks = {}
    for r in cur.fetchall():
        name = r[0]
        if name not in fks:
            fks[name] = {
                "constraint_name": name,
                "columns": [],
                "referenced_schema": r[2],
                "referenced_table": r[3],
                "referenced_columns": [],
                "on_delete": r[5],
                "on_update": r[6],
            }
        fks[name]["columns"].append(r[1])
        fks[name]["referenced_columns"].append(r[4])
    return list(fks.values())


def get_unique_constraints(cur, schema, table):
    cur.execute("""
        SELECT kcu.CONSTRAINT_NAME, kcu.COLUMN_NAME
        FROM information_schema.KEY_COLUMN_USAGE kcu
        JOIN information_schema.TABLE_CONSTRAINTS tc
            ON kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
            AND kcu.TABLE_SCHEMA = tc.TABLE_SCHEMA
            AND kcu.TABLE_NAME = tc.TABLE_NAME
        WHERE kcu.TABLE_SCHEMA = %s AND kcu.TABLE_NAME = %s
          AND tc.CONSTRAINT_TYPE = 'UNIQUE'
        ORDER BY kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION;
    """, (schema, table))
    ucs = {}
    for r in cur.fetchall():
        ucs.setdefault(r[0], {"constraint_name": r[0], "columns": []})["columns"].append(r[1])
    return list(ucs.values())


def get_indexes(cur, schema, table):
    cur.execute("""
        SELECT
            INDEX_NAME,
            NON_UNIQUE,
            INDEX_TYPE,
            GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX SEPARATOR ',')
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
          AND INDEX_NAME != 'PRIMARY'
        GROUP BY INDEX_NAME, NON_UNIQUE, INDEX_TYPE
        ORDER BY INDEX_NAME;
    """, (schema, table))
    return [
        {
            "name": r[0],
            "unique": r[1] == 0,
            "type": r[2],
            "columns": r[3].split(",") if r[3] else [],
        }
        for r in cur.fetchall()
    ]


def get_check_constraints(cur, schema, table):
    try:
        cur.execute("""
            SELECT tc.CONSTRAINT_NAME, cc.CHECK_CLAUSE
            FROM information_schema.TABLE_CONSTRAINTS tc
            JOIN information_schema.CHECK_CONSTRAINTS cc
                ON tc.CONSTRAINT_NAME = cc.CONSTRAINT_NAME
                AND tc.CONSTRAINT_SCHEMA = cc.CONSTRAINT_SCHEMA
            WHERE tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME = %s
              AND tc.CONSTRAINT_TYPE = 'CHECK'
            ORDER BY tc.CONSTRAINT_NAME;
        """, (schema, table))
        return [{"constraint_name": r[0], "definition": r[1]} for r in cur.fetchall()]
    except Exception:
        # CHECK_CONSTRAINTS table not available in older MySQL versions
        return []


output_dir = os.path.join(output_dir, "data_discovery")
os.makedirs(output_dir, exist_ok=True)
written = []
failed = []

for schema, table in tables:
    try:
        metadata = {
            "schema": schema,
            "table": table,
            "comment": get_table_comment(cur, schema, table),
            "columns": get_columns(cur, schema, table),
            "primary_key": get_primary_key(cur, schema, table),
            "foreign_keys": get_foreign_keys(cur, schema, table),
            "unique_constraints": get_unique_constraints(cur, schema, table),
            "indexes": get_indexes(cur, schema, table),
            "check_constraints": get_check_constraints(cur, schema, table),
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
