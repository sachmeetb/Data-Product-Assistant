#!/usr/bin/env python3
"""
profile_table.py — Data profiling script for PostgreSQL tables.

Usage:
    python profile_table.py <connection_string> <output_dir> <yaml_file1> [yaml_file2 ...] \
        [--limit N] [--top-n N]

For each discovery YAML file provided, runs profiling queries against the
corresponding table and writes a profile YAML to output_dir.

Output files are named: <schema>__<table>__profile.yaml
"""

import sys
import subprocess
import importlib


def ensure_deps():
    for pkg, imp in [("psycopg2-binary", "psycopg2"), ("pyyaml", "yaml")]:
        try:
            importlib.import_module(imp)
        except ImportError:
            print(f"Installing {pkg}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])


ensure_deps()

import argparse
import math
import os
from datetime import date, datetime
from decimal import Decimal

import psycopg2
import yaml

# ---------------------------------------------------------------------------
# Type classification
# ---------------------------------------------------------------------------

NUMERIC_TYPES = {
    "integer", "bigint", "smallint", "numeric", "decimal",
    "real", "double precision", "serial", "bigserial", "smallserial",
    "money", "int2", "int4", "int8", "float4", "float8", "oid",
}
TEXT_TYPES = {
    "character varying", "varchar", "text", "char", "character",
    "name", "citext", "bpchar",
}
DATE_TYPES = {
    "date", "timestamp", "timestamp without time zone",
    "timestamp with time zone", "timestamptz",
    "time", "time without time zone", "time with time zone", "interval",
}
BOOLEAN_TYPES = {"boolean", "bool"}


def classify_column(col_type: str) -> str:
    t = col_type.lower().strip()
    if t in NUMERIC_TYPES:
        return "numeric"
    if t in TEXT_TYPES:
        return "text"
    if t in DATE_TYPES:
        return "date"
    if t in BOOLEAN_TYPES:
        return "boolean"
    return "other"  # USER-DEFINED enums, json, arrays, etc.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def quote_ident(name: str) -> str:
    """Double-quote a PostgreSQL identifier to handle reserved words / special chars."""
    return '"' + name.replace('"', '""') + '"'


def safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, 6)


def to_yaml_safe(val):
    """Convert a value to something PyYAML can serialize cleanly."""
    if val is None:
        return None
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return None
    return val


def rows_as_dicts(cursor) -> list[dict]:
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Query builders
# ---------------------------------------------------------------------------

def build_exact_range_query(schema: str, table: str, columns: list) -> str | None:
    """
    Build a single SELECT that computes true MIN/MAX from the full table for all
    numeric and date columns. Used to override sampled range bounds.
    Returns None if there are no range columns.
    """
    selects = []
    schema_q = quote_ident(schema)
    table_q = quote_ident(table)

    for i, col in enumerate(columns):
        kind = classify_column(col.get("type", ""))
        if kind in ("numeric", "date"):
            col_q = quote_ident(col["name"])
            pfx = f"c{i}"
            selects.append(f"MIN({col_q}) AS {pfx}__min")
            selects.append(f"MAX({col_q}) AS {pfx}__max")

    if not selects:
        return None
    selects_sql = ",\n    ".join(selects)
    return f"SELECT\n    {selects_sql}\nFROM {schema_q}.{table_q}"


def build_main_stats_query(schema: str, table: str, columns: list, from_clause: str) -> str:
    """
    Build a single SELECT that computes all basic column stats in one table scan.
    Uses index-based alias prefixes (c0__, c1__, ...) to avoid conflicts with
    column names that might contain double-underscores.
    """
    selects = ["COUNT(*) AS _row_count"]

    for i, col in enumerate(columns):
        name = col["name"]
        col_type = col.get("type", "")
        kind = classify_column(col_type)
        col_q = quote_ident(name)
        pfx = f"c{i}"

        selects.append(f"COUNT({col_q}) AS {pfx}__non_null")
        selects.append(f"COUNT(DISTINCT {col_q}) AS {pfx}__distinct")

        if kind == "numeric":
            selects.append(f"MIN({col_q}) AS {pfx}__min")
            selects.append(f"MAX({col_q}) AS {pfx}__max")
            selects.append(f"AVG({col_q}::numeric) AS {pfx}__mean")
            selects.append(f"STDDEV({col_q}::numeric) AS {pfx}__stddev")
            selects.append(
                f"PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY {col_q}) AS {pfx}__p25"
            )
            selects.append(
                f"PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY {col_q}) AS {pfx}__p50"
            )
            selects.append(
                f"PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY {col_q}) AS {pfx}__p75"
            )

        elif kind == "text":
            selects.append(f"MIN(LENGTH({col_q})) AS {pfx}__min_length")
            selects.append(f"MAX(LENGTH({col_q})) AS {pfx}__max_length")
            selects.append(
                f"ROUND(AVG(LENGTH({col_q}))::numeric, 2) AS {pfx}__avg_length"
            )

        elif kind == "date":
            selects.append(f"MIN({col_q}) AS {pfx}__min")
            selects.append(f"MAX({col_q}) AS {pfx}__max")

    selects_sql = ",\n    ".join(selects)
    return f"SELECT\n    {selects_sql}\n{from_clause}"


def get_top_values(cursor, col_name: str, from_clause: str, total_rows: int, top_n: int) -> list:
    col_q = quote_ident(col_name)
    cursor.execute(
        f"""
        SELECT {col_q}::text AS val, COUNT(*) AS cnt
        {from_clause}
        WHERE {col_q} IS NOT NULL
        GROUP BY {col_q}
        ORDER BY cnt DESC
        LIMIT {top_n}
        """
    )
    rows = cursor.fetchall()
    denominator = total_rows or 1
    return [
        {
            "value": row[0],
            "count": row[1],
            "frequency": round(row[1] / denominator, 6),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Core profiler
# ---------------------------------------------------------------------------

def profile_table(conn, discovery_yaml: str, output_dir: str, limit: int, top_n: int) -> str:
    with open(discovery_yaml) as f:
        meta = yaml.safe_load(f)

    schema = meta["schema"]
    table = meta["table"]
    columns = meta["columns"]

    print(f"  Profiling {schema}.{table}...")

    schema_q = quote_ident(schema)
    table_q = quote_ident(table)

    with conn.cursor() as cur:
        # Actual total row count (always full scan for this)
        cur.execute(f"SELECT COUNT(*) FROM {schema_q}.{table_q}")
        actual_row_count = cur.fetchone()[0]

        # Decide sample size and build the FROM clause used for profiling queries
        if limit and actual_row_count > limit:
            sample_size = limit
            # Subquery with LIMIT gives a deterministic (if arbitrary) sample without sorting cost
            from_clause = (
                f"FROM (SELECT * FROM {schema_q}.{table_q} LIMIT {limit}) AS _sample"
            )
        else:
            sample_size = actual_row_count
            from_clause = f"FROM {schema_q}.{table_q}"

        # ---- Main stats query (single pass over sample) --------------------
        main_query = build_main_stats_query(schema, table, columns, from_clause)
        cur.execute(main_query)
        main_row = rows_as_dicts(cur)[0]
        sample_rows_scanned = main_row["_row_count"]

        # ---- Exact MIN/MAX from full table (only needed when sampling) -----
        exact_range_row: dict = {}
        if limit and actual_row_count > limit:
            range_query = build_exact_range_query(schema, table, columns)
            if range_query:
                cur.execute(range_query)
                exact_range_row = rows_as_dicts(cur)[0]

        # ---- Build per-column profiles -------------------------------------
        col_profiles = []

        for i, col in enumerate(columns):
            name = col["name"]
            col_type = col.get("type", "")
            kind = classify_column(col_type)
            pfx = f"c{i}"

            non_null = main_row.get(f"{pfx}__non_null") or 0
            distinct_count = main_row.get(f"{pfx}__distinct") or 0
            null_count = sample_rows_scanned - non_null
            null_rate = round(null_count / sample_rows_scanned, 6) if sample_rows_scanned else 0.0

            profile = {
                "name": name,
                "type": col_type,
                "null_count": int(null_count),
                "null_rate": null_rate,
                "distinct_count": int(distinct_count),
            }

            if kind == "numeric":
                # Use exact full-table min/max if available, else sampled
                profile["min"] = to_yaml_safe(
                    exact_range_row.get(f"{pfx}__min") if exact_range_row else main_row.get(f"{pfx}__min")
                )
                profile["max"] = to_yaml_safe(
                    exact_range_row.get(f"{pfx}__max") if exact_range_row else main_row.get(f"{pfx}__max")
                )
                profile["mean"] = safe_float(main_row.get(f"{pfx}__mean"))
                profile["stddev"] = safe_float(main_row.get(f"{pfx}__stddev"))
                profile["percentile_25"] = to_yaml_safe(main_row.get(f"{pfx}__p25"))
                profile["percentile_50"] = to_yaml_safe(main_row.get(f"{pfx}__p50"))
                profile["percentile_75"] = to_yaml_safe(main_row.get(f"{pfx}__p75"))

            elif kind == "text":
                profile["min_length"] = main_row.get(f"{pfx}__min_length")
                profile["max_length"] = main_row.get(f"{pfx}__max_length")
                profile["avg_length"] = safe_float(main_row.get(f"{pfx}__avg_length"))

            elif kind == "date":
                # Use exact full-table min/max if available, else sampled
                profile["min"] = to_yaml_safe(
                    exact_range_row.get(f"{pfx}__min") if exact_range_row else main_row.get(f"{pfx}__min")
                )
                profile["max"] = to_yaml_safe(
                    exact_range_row.get(f"{pfx}__max") if exact_range_row else main_row.get(f"{pfx}__max")
                )

            col_profiles.append((col, profile, kind))

        # ---- Top values (separate per-column query) ------------------------
        # Top values for: text, boolean, user-defined/other, and numeric columns
        # where cardinality is low enough to be meaningful.
        for col, profile, kind in col_profiles:
            name = col["name"]
            distinct_count = profile["distinct_count"]

            wants_top_values = kind in ("text", "boolean", "other") or (
                kind == "numeric" and distinct_count <= top_n * 2
            )

            if wants_top_values:
                try:
                    profile["top_values"] = get_top_values(
                        cur, name, from_clause, sample_rows_scanned, top_n
                    )
                except Exception as e:
                    print(f"    Warning: could not get top values for {name}: {e}")
                    conn.rollback()

        # ---- Assemble output -----------------------------------------------
        output = {
            "schema": schema,
            "table": table,
            "profiled_at": datetime.utcnow().replace(microsecond=0).isoformat(),
            "row_count": actual_row_count,
            "sample_size": sample_size,
            "columns": [profile for _, profile, _ in col_profiles],
        }

        os.makedirs(output_dir, exist_ok=True)
        out_file = os.path.join(output_dir, f"{schema}__{table}__profile.yaml")
        with open(out_file, "w") as f:
            yaml.dump(output, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        print(f"    Written: {out_file}")
        return out_file


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Profile PostgreSQL tables using discovery YAMLs.")
    parser.add_argument("connection_string", help="PostgreSQL connection string")
    parser.add_argument("output_dir", help="Directory to write profile YAMLs")
    parser.add_argument("yaml_files", nargs="+", help="Discovery YAML files to profile")
    parser.add_argument(
        "--limit",
        type=int,
        default=100000,
        help="Max rows to sample per table (0 = full scan, default: 100000)",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=10,
        dest="top_n",
        help="Number of top frequent values to report per column (default: 10)",
    )
    args = parser.parse_args()

    limit = args.limit if args.limit > 0 else None

    os.makedirs(args.output_dir, exist_ok=True)

    try:
        conn = psycopg2.connect(args.connection_string)
        conn.autocommit = True
    except Exception as e:
        print(f"Connection failed: {e}", file=sys.stderr)
        sys.exit(1)

    written = []
    errors = []

    for yaml_file in args.yaml_files:
        if not os.path.exists(yaml_file):
            print(f"Warning: file not found, skipping: {yaml_file}")
            errors.append(yaml_file)
            continue
        try:
            out = profile_table(conn, yaml_file, args.output_dir, limit, args.top_n)
            written.append(out)
        except Exception as e:
            print(f"Error profiling {yaml_file}: {e}", file=sys.stderr)
            errors.append(yaml_file)

    conn.close()

    print(f"\nDone. {len(written)} profile(s) written.")
    if errors:
        print(f"Skipped/failed: {len(errors)} file(s): {', '.join(errors)}")

    sys.exit(1 if errors and not written else 0)


if __name__ == "__main__":
    main()
