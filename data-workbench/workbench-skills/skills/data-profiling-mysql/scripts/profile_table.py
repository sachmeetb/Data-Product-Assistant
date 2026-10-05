#!/usr/bin/env python3
"""
profile_table.py — Data profiling script for MySQL tables.

Usage:
    python profile_table.py <connection_string> <output_dir> <yaml_file1> [yaml_file2 ...] \
        [--limit N] [--top-n N]

Connection string format: mysql://user:password@host:port/database

For each discovery YAML file provided, runs profiling queries against the
corresponding table and writes a profile YAML to output_dir.

Output files are named: <schema>__<table>__profile.yaml

Note: percentile metrics (p25/p50/p75) are not produced — MySQL 8.0 does not
support PERCENTILE_CONT as an aggregate function.  The output format is
otherwise identical to the PostgreSQL profiler and is compatible with all
downstream pipeline skills.
"""

import sys
import subprocess
import importlib


def ensure_deps():
    for pkg, imp in [("pymysql", "pymysql"), ("pyyaml", "yaml")]:
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
from urllib.parse import urlparse, unquote

import pymysql
import yaml


# ---------------------------------------------------------------------------
# DSN parser
# ---------------------------------------------------------------------------

def parse_dsn(dsn: str) -> dict:
    """Parse mysql://user:password@host:port/database into pymysql kwargs."""
    parsed = urlparse(dsn)
    kwargs = {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": (parsed.path or "").lstrip("/") or None,
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
    }
    if not kwargs["database"]:
        kwargs.pop("database")
    return kwargs


# ---------------------------------------------------------------------------
# Type classification
# ---------------------------------------------------------------------------

NUMERIC_TYPES = {
    "int", "tinyint", "smallint", "mediumint", "bigint",
    "decimal", "numeric", "float", "double", "double precision", "real",
    "bit",
}
TEXT_TYPES = {
    "varchar", "char", "tinytext", "text", "mediumtext", "longtext",
    "binary", "varbinary",
}
DATE_TYPES = {
    "date", "datetime", "timestamp", "time", "year",
}
ENUM_TYPES = {"enum", "set"}


def classify_column(col_type: str) -> str:
    t = col_type.lower().strip()
    # Strip length qualifier: varchar(255) → varchar
    base = t.split("(")[0].strip()
    if base in NUMERIC_TYPES:
        return "numeric"
    if base in TEXT_TYPES:
        return "text"
    if base in DATE_TYPES:
        return "date"
    if base in ENUM_TYPES:
        return "enum"
    return "other"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def bt(name: str) -> str:
    """Backtick-quote a MySQL identifier."""
    return "`" + name.replace("`", "``") + "`"


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
    if isinstance(val, (bytes, bytearray)):
        return val.hex()
    return val


# ---------------------------------------------------------------------------
# Query builders
# ---------------------------------------------------------------------------

def build_main_stats_query(schema: str, table: str, columns: list, from_clause: str) -> str:
    """Build a single SELECT computing all basic column stats in one table scan."""
    selects = ["COUNT(*) AS _row_count"]

    for i, col in enumerate(columns):
        name = col["name"]
        col_type = col.get("type", "")
        kind = classify_column(col_type)
        col_q = bt(name)
        pfx = f"c{i}"

        selects.append(f"COUNT({col_q}) AS {pfx}__non_null")
        selects.append(f"COUNT(DISTINCT {col_q}) AS {pfx}__distinct")

        if kind == "numeric":
            selects.append(f"MIN({col_q}) AS {pfx}__min")
            selects.append(f"MAX({col_q}) AS {pfx}__max")
            # CAST to DECIMAL avoids integer-only truncation for AVG/STDDEV.
            selects.append(
                f"AVG(CAST({col_q} AS DECIMAL(65, 6))) AS {pfx}__mean"
            )
            selects.append(
                f"STDDEV(CAST({col_q} AS DECIMAL(65, 6))) AS {pfx}__stddev"
            )

        elif kind == "text":
            selects.append(f"MIN(CHAR_LENGTH({col_q})) AS {pfx}__min_length")
            selects.append(f"MAX(CHAR_LENGTH({col_q})) AS {pfx}__max_length")
            selects.append(
                f"ROUND(AVG(CHAR_LENGTH({col_q})), 2) AS {pfx}__avg_length"
            )

        elif kind == "date":
            selects.append(f"MIN({col_q}) AS {pfx}__min")
            selects.append(f"MAX({col_q}) AS {pfx}__max")

    selects_sql = ",\n    ".join(selects)
    return f"SELECT\n    {selects_sql}\n{from_clause}"


def build_exact_range_query(schema: str, table: str, columns: list) -> str | None:
    """Build a full-table MIN/MAX query for numeric and date columns."""
    selects = []
    schema_q = bt(schema)
    table_q = bt(table)

    for i, col in enumerate(columns):
        kind = classify_column(col.get("type", ""))
        if kind in ("numeric", "date"):
            col_q = bt(col["name"])
            pfx = f"c{i}"
            selects.append(f"MIN({col_q}) AS {pfx}__min")
            selects.append(f"MAX({col_q}) AS {pfx}__max")

    if not selects:
        return None
    selects_sql = ",\n    ".join(selects)
    return f"SELECT\n    {selects_sql}\nFROM {schema_q}.{table_q}"


def get_top_values(cur, col_name: str, from_clause: str, total_rows: int, top_n: int) -> list:
    col_q = bt(col_name)
    # MySQL doesn't support ::text; use CAST(col AS CHAR) to stringify values
    # including enums, sets, and numerics so the GROUP BY works uniformly.
    cur.execute(
        f"""
        SELECT CAST({col_q} AS CHAR) AS val, COUNT(*) AS cnt
        {from_clause}
        WHERE {col_q} IS NOT NULL
        GROUP BY {col_q}
        ORDER BY cnt DESC
        LIMIT {top_n}
        """
    )
    rows = cur.fetchall()
    denominator = total_rows or 1
    return [
        {
            "value": row["val"],
            "count": row["cnt"],
            "frequency": round(row["cnt"] / denominator, 6),
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

    schema_q = bt(schema)
    table_q = bt(table)

    with conn.cursor() as cur:
        # Actual total row count (full scan — no way around it in MySQL without
        # relying on information_schema which can be stale for InnoDB).
        cur.execute(f"SELECT COUNT(*) AS cnt FROM {schema_q}.{table_q}")
        actual_row_count = cur.fetchone()["cnt"]

        # Decide sample size and build the FROM clause for profiling queries.
        # MySQL requires a subquery alias; use _sample.
        if limit and actual_row_count > limit:
            sample_size = limit
            from_clause = (
                f"FROM (SELECT * FROM {schema_q}.{table_q} LIMIT {limit}) AS _sample"
            )
        else:
            sample_size = actual_row_count
            from_clause = f"FROM {schema_q}.{table_q}"

        # ---- Main stats query (single pass over sample) --------------------
        main_query = build_main_stats_query(schema, table, columns, from_clause)
        cur.execute(main_query)
        main_row = cur.fetchone()
        sample_rows_scanned = main_row["_row_count"]

        # ---- Exact MIN/MAX from full table (only when sampling) ------------
        exact_range_row: dict = {}
        if limit and actual_row_count > limit:
            range_query = build_exact_range_query(schema, table, columns)
            if range_query:
                cur.execute(range_query)
                exact_range_row = cur.fetchone() or {}

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

            profile: dict = {
                "name": name,
                "type": col_type,
                "null_count": int(null_count),
                "null_rate": null_rate,
                "distinct_count": int(distinct_count),
            }

            if kind == "numeric":
                # Prefer exact full-table range when we sampled.
                profile["min"] = to_yaml_safe(
                    exact_range_row.get(f"{pfx}__min") if exact_range_row else main_row.get(f"{pfx}__min")
                )
                profile["max"] = to_yaml_safe(
                    exact_range_row.get(f"{pfx}__max") if exact_range_row else main_row.get(f"{pfx}__max")
                )
                profile["mean"] = safe_float(main_row.get(f"{pfx}__mean"))
                profile["stddev"] = safe_float(main_row.get(f"{pfx}__stddev"))
                # Percentiles not available in MySQL 8.0 (no PERCENTILE_CONT aggregate).

            elif kind == "text":
                profile["min_length"] = main_row.get(f"{pfx}__min_length")
                profile["max_length"] = main_row.get(f"{pfx}__max_length")
                profile["avg_length"] = safe_float(main_row.get(f"{pfx}__avg_length"))

            elif kind == "date":
                profile["min"] = to_yaml_safe(
                    exact_range_row.get(f"{pfx}__min") if exact_range_row else main_row.get(f"{pfx}__min")
                )
                profile["max"] = to_yaml_safe(
                    exact_range_row.get(f"{pfx}__max") if exact_range_row else main_row.get(f"{pfx}__max")
                )

            col_profiles.append((col, profile, kind))

        # ---- Top values (separate per-column query) ------------------------
        for col, profile, kind in col_profiles:
            name = col["name"]
            distinct_count = profile["distinct_count"]

            wants_top_values = kind in ("text", "boolean", "enum", "other") or (
                kind == "numeric" and distinct_count <= top_n * 2
            )

            if wants_top_values:
                try:
                    profile["top_values"] = get_top_values(
                        cur, name, from_clause, sample_rows_scanned, top_n
                    )
                except Exception as e:
                    print(f"    Warning: could not get top values for {name}: {e}")

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
    parser = argparse.ArgumentParser(description="Profile MySQL tables using discovery YAMLs.")
    parser.add_argument("connection_string", help="MySQL connection string (mysql://user:pass@host:port/db)")
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
        connect_kwargs = parse_dsn(args.connection_string)
        conn = pymysql.connect(**connect_kwargs)
        conn.autocommit(True)
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
