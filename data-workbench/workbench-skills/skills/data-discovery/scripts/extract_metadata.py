#!/usr/bin/env python3
"""Extract full metadata for specified PostgreSQL tables and write one YAML file per table.

Output files are named <schema>__<table>.yaml and written to output_dir.
"""
import sys
import os
import argparse

try:
    import psycopg2
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "psycopg2-binary", "-q"])
    import psycopg2

try:
    import yaml
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyyaml", "-q"])
    import yaml

parser = argparse.ArgumentParser(
    description="Extract full metadata for specified PostgreSQL tables and write one YAML file per table.",
    epilog="Output files are named <schema>__<table>.yaml and written to output_dir.",
)
parser.add_argument("connection_string", help="PostgreSQL connection string")
parser.add_argument("output_dir", help="Directory to write YAML files")
parser.add_argument("tables", nargs="+", metavar="schema.table", help="Tables to extract (format: schema.table)")
args = parser.parse_args()

conn_str = args.connection_string
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
    conn = psycopg2.connect(conn_str)
    cur = conn.cursor()
except Exception as e:
    print(f"Connection error: {e}", file=sys.stderr)
    sys.exit(1)


def get_table_comment(cur, schema, table):
    cur.execute("""
        SELECT obj_description(c.oid)
        FROM pg_class c
        JOIN pg_namespace n ON c.relnamespace = n.oid
        WHERE n.nspname = %s AND c.relname = %s;
    """, (schema, table))
    row = cur.fetchone()
    return row[0] if row else None


def get_columns(cur, schema, table):
    cur.execute("""
        SELECT
            c.column_name,
            c.ordinal_position,
            c.data_type,
            c.character_maximum_length,
            c.numeric_precision,
            c.numeric_scale,
            c.is_nullable,
            c.column_default,
            pgd.description
        FROM information_schema.columns c
        LEFT JOIN pg_catalog.pg_statio_all_tables st
            ON c.table_schema = st.schemaname AND c.table_name = st.relname
        LEFT JOIN pg_catalog.pg_description pgd
            ON pgd.objoid = st.relid AND pgd.objsubid = c.ordinal_position
        WHERE c.table_schema = %s AND c.table_name = %s
        ORDER BY c.ordinal_position;
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
            "comment": r[8],
        }
        for r in cur.fetchall()
    ]


def get_primary_key(cur, schema, table):
    cur.execute("""
        SELECT tc.constraint_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
            AND tc.table_schema = kcu.table_schema
        WHERE tc.table_schema = %s AND tc.table_name = %s
          AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY kcu.ordinal_position;
    """, (schema, table))
    rows = cur.fetchall()
    if not rows:
        return None
    return {"constraint_name": rows[0][0], "columns": [r[1] for r in rows]}


def get_foreign_keys(cur, schema, table):
    cur.execute("""
        SELECT
            tc.constraint_name,
            kcu.column_name,
            ccu.table_schema,
            ccu.table_name,
            ccu.column_name,
            rc.delete_rule,
            rc.update_rule
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
            AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
            ON tc.constraint_name = ccu.constraint_name
            AND tc.table_schema = ccu.table_schema
        JOIN information_schema.referential_constraints rc
            ON tc.constraint_name = rc.constraint_name
            AND tc.table_schema = rc.constraint_schema
        WHERE tc.table_schema = %s AND tc.table_name = %s
          AND tc.constraint_type = 'FOREIGN KEY'
        ORDER BY tc.constraint_name, kcu.ordinal_position;
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
        SELECT tc.constraint_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
            AND tc.table_schema = kcu.table_schema
        WHERE tc.table_schema = %s AND tc.table_name = %s
          AND tc.constraint_type = 'UNIQUE'
        ORDER BY tc.constraint_name, kcu.ordinal_position;
    """, (schema, table))
    ucs = {}
    for r in cur.fetchall():
        ucs.setdefault(r[0], {"constraint_name": r[0], "columns": []})["columns"].append(r[1])
    return list(ucs.values())


def get_indexes(cur, schema, table):
    cur.execute("""
        SELECT
            i.relname,
            ix.indisunique,
            am.amname,
            array_agg(a.attname ORDER BY k.n)
        FROM pg_class t
        JOIN pg_namespace n ON t.relnamespace = n.oid
        JOIN pg_index ix ON t.oid = ix.indrelid
        JOIN pg_class i ON i.oid = ix.indexrelid
        JOIN pg_am am ON i.relam = am.oid
        JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, n) ON TRUE
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
        WHERE n.nspname = %s AND t.relname = %s AND NOT ix.indisprimary
        GROUP BY i.relname, ix.indisunique, am.amname
        ORDER BY i.relname;
    """, (schema, table))
    return [{"name": r[0], "unique": r[1], "type": r[2], "columns": list(r[3])} for r in cur.fetchall()]


def get_check_constraints(cur, schema, table):
    cur.execute("""
        SELECT tc.constraint_name, cc.check_clause
        FROM information_schema.table_constraints tc
        JOIN information_schema.check_constraints cc
            ON tc.constraint_name = cc.constraint_name
            AND tc.table_schema = cc.constraint_schema
        WHERE tc.table_schema = %s AND tc.table_name = %s
          AND tc.constraint_type = 'CHECK'
          AND cc.check_clause NOT LIKE '%%IS NOT NULL%%'
        ORDER BY tc.constraint_name;
    """, (schema, table))
    return [{"constraint_name": r[0], "definition": r[1]} for r in cur.fetchall()]


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
