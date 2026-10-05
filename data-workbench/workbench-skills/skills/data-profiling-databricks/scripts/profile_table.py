#!/usr/bin/env python3
"""
profile_table.py — Data profiling script for Databricks tables.

Usage:
    python profile_table.py <connection_string> <output_dir> <yaml_file1> [yaml_file2 ...] \
        [--limit N] [--top-n N]

Connection string format: databricks://token:ACCESS_TOKEN@HOSTNAME?http_path=PATH&catalog=CATALOG

For each discovery YAML file provided, runs profiling queries against the
corresponding Databricks table and writes a profile YAML to output_dir.

Output files are named: <schema>__<table>__profile.yaml

Percentiles use PERCENTILE_APPROX — approximate but highly efficient on Delta tables.
Sampling uses WHERE RAND() < fraction (Bernoulli row sampling).
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
import math
import os
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlparse, parse_qs, unquote

import databricks.sql
import yaml


# ---------------------------------------------------------------------------
# DSN parser
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Identifier quoting
# ---------------------------------------------------------------------------

def bt(name: str) -> str:
    """Backtick-quote a Databricks/Spark SQL identifier."""
    return "`" + name.replace("`", "``") + "`"


# ---------------------------------------------------------------------------
# Type classification
# ---------------------------------------------------------------------------

NUMERIC_TYPES = {
    "bigint", "int", "integer", "smallint", "tinyint", "long",
    "float", "double", "real",
    "decimal", "numeric",
    "byte", "short",
}
TEXT_TYPES = {"string", "varchar", "char", "text"}
DATE_TYPES = {"date", "timestamp", "timestamp_ntz"}
BOOLEAN_TYPES = {"boolean", "bool"}
COMPLEX_TYPES = {"array", "map", "struct", "binary"}


def classify_column(col_type: str) -> str:
    t = col_type.lower().strip()
    # Strip length/precision qualifier: decimal(10,2) → decimal, varchar(255) → varchar
    base = t.split("(")[0].split("<")[0].strip()
    if base in NUMERIC_TYPES:
        return "numeric"
    if base in TEXT_TYPES:
        return "text"
    if base in DATE_TYPES:
        return "date"
    if base in BOOLEAN_TYPES:
        return "boolean"
    if base in COMPLEX_TYPES:
        return "complex"
    return "other"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
            selects.append(f"AVG(CAST({col_q} AS DOUBLE)) AS {pfx}__mean")
            selects.append(f"STDDEV(CAST({col_q} AS DOUBLE)) AS {pfx}__stddev")
            selects.append(f"PERCENTILE_APPROX(CAST({col_q} AS DOUBLE), 0.25) AS {pfx}__p25")
            selects.append(f"PERCENTILE_APPROX(CAST({col_q} AS DOUBLE), 0.50) AS {pfx}__p50")
            selects.append(f"PERCENTILE_APPROX(CAST({col_q} AS DOUBLE), 0.75) AS {pfx}__p75")

        elif kind == "text":
            selects.append(f"MIN(LENGTH({col_q})) AS {pfx}__min_length")
            selects.append(f"MAX(LENGTH({col_q})) AS {pfx}__max_length")
            selects.append(f"ROUND(AVG(LENGTH({col_q})), 2) AS {pfx}__avg_length")

        elif kind == "date":
            selects.append(f"MIN({col_q}) AS {pfx}__min")
            selects.append(f"MAX({col_q}) AS {pfx}__max")

    selects_sql = ",\n    ".join(selects)
    return f"SELECT\n    {selects_sql}\n{from_clause}"


def build_exact_range_query(schema: str, table: str, columns: list) -> str | None:
    """Build a full-table MIN/MAX query for numeric and date columns (used when sampling)."""
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


def get_top_values(cursor, col_name: str, from_clause: str, total_rows: int, top_n: int) -> list:
    col_q = bt(col_name)
    cursor.execute(
        f"""
        SELECT CAST({col_q} AS STRING) AS val, COUNT(*) AS cnt
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

def profile_table(conn, discovery_yaml: str, output_dir: str, limit: int, top_n: int,
                  catalog: str | None) -> str:
    with open(discovery_yaml) as f:
        meta = yaml.safe_load(f)

    schema = meta["schema"]
    table = meta["table"]
    columns = meta["columns"]

    print(f"  Profiling {schema}.{table}...")

    schema_q = bt(schema)
    table_q = bt(table)

    cursor = conn.cursor()

    if catalog:
        try:
            cursor.execute(f"USE CATALOG `{catalog}`")
        except Exception:
            pass

    # Actual total row count
    cursor.execute(f"SELECT COUNT(*) FROM {schema_q}.{table_q}")
    actual_row_count = cursor.fetchone()[0]

    # Decide sample size and build the FROM clause.
    # Databricks: use WHERE RAND() < fraction for Bernoulli sampling.
    if limit and actual_row_count > limit:
        sample_size = limit
        fraction = min(limit / actual_row_count * 1.05, 1.0)  # slight over-sample for RAND jitter
        from_clause = f"FROM (SELECT * FROM {schema_q}.{table_q} WHERE RAND() < {fraction:.6f}) _sample"
    else:
        sample_size = actual_row_count
        from_clause = f"FROM {schema_q}.{table_q}"

    # ---- Main stats query (single pass over sample) -----------------------
    main_query = build_main_stats_query(schema, table, columns, from_clause)
    cursor.execute(main_query)
    main_row = cursor.fetchone()
    col_names = [d[0].lower() for d in cursor.description]
    main_dict = dict(zip(col_names, main_row))
    sample_rows_scanned = main_dict.get("_row_count", 0)

    # ---- Exact MIN/MAX from full table (only when sampling) ---------------
    exact_range_dict: dict = {}
    if limit and actual_row_count > limit:
        range_query = build_exact_range_query(schema, table, columns)
        if range_query:
            cursor.execute(range_query)
            range_row = cursor.fetchone()
            if range_row:
                range_col_names = [d[0].lower() for d in cursor.description]
                exact_range_dict = dict(zip(range_col_names, range_row))

    # ---- Build per-column profiles ----------------------------------------
    col_profiles = []

    for i, col in enumerate(columns):
        name = col["name"]
        col_type = col.get("type", "")
        kind = classify_column(col_type)
        pfx = f"c{i}"

        non_null = main_dict.get(f"{pfx}__non_null") or 0
        distinct_count = main_dict.get(f"{pfx}__distinct") or 0
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
            profile["min"] = to_yaml_safe(
                exact_range_dict.get(f"{pfx}__min") if exact_range_dict else main_dict.get(f"{pfx}__min")
            )
            profile["max"] = to_yaml_safe(
                exact_range_dict.get(f"{pfx}__max") if exact_range_dict else main_dict.get(f"{pfx}__max")
            )
            profile["mean"] = safe_float(main_dict.get(f"{pfx}__mean"))
            profile["stddev"] = safe_float(main_dict.get(f"{pfx}__stddev"))
            profile["percentile_25"] = safe_float(main_dict.get(f"{pfx}__p25"))
            profile["percentile_50"] = safe_float(main_dict.get(f"{pfx}__p50"))
            profile["percentile_75"] = safe_float(main_dict.get(f"{pfx}__p75"))

        elif kind == "text":
            profile["min_length"] = main_dict.get(f"{pfx}__min_length")
            profile["max_length"] = main_dict.get(f"{pfx}__max_length")
            profile["avg_length"] = safe_float(main_dict.get(f"{pfx}__avg_length"))

        elif kind == "date":
            profile["min"] = to_yaml_safe(
                exact_range_dict.get(f"{pfx}__min") if exact_range_dict else main_dict.get(f"{pfx}__min")
            )
            profile["max"] = to_yaml_safe(
                exact_range_dict.get(f"{pfx}__max") if exact_range_dict else main_dict.get(f"{pfx}__max")
            )

        col_profiles.append((col, profile, kind))

    # ---- Top values (separate per-column query) ---------------------------
    for col, profile, kind in col_profiles:
        name = col["name"]
        distinct_count = profile["distinct_count"]

        wants_top_values = kind in ("text", "boolean", "enum", "other") or (
            kind == "numeric" and distinct_count <= top_n * 2
        )

        if wants_top_values:
            try:
                profile["top_values"] = get_top_values(
                    cursor, name, from_clause, sample_rows_scanned, top_n
                )
            except Exception as e:
                print(f"    Warning: could not get top values for {name}: {e}")

    # ---- Assemble output --------------------------------------------------
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

    cursor.close()
    print(f"    Written: {out_file}")
    return out_file


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Profile Databricks tables using discovery YAMLs.")
    parser.add_argument("connection_string", help="Databricks connection string")
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

    dsn_kwargs = parse_dsn(args.connection_string)

    try:
        conn = databricks.sql.connect(
            server_hostname=dsn_kwargs["server_hostname"],
            http_path=dsn_kwargs["http_path"],
            access_token=dsn_kwargs["access_token"],
        )
    except Exception as e:
        print(f"Connection failed: {e}", file=sys.stderr)
        sys.exit(1)

    catalog = dsn_kwargs.get("catalog")
    written = []
    errors = []

    for yaml_file in args.yaml_files:
        if not os.path.exists(yaml_file):
            print(f"Warning: file not found, skipping: {yaml_file}")
            errors.append(yaml_file)
            continue
        try:
            out = profile_table(conn, yaml_file, args.output_dir, limit, args.top_n, catalog)
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
