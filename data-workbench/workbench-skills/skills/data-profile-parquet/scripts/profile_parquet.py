#!/usr/bin/env python3
"""profile_parquet.py — Profile Parquet files with DuckDB + pyarrow cross-check.

Usage:
    python profile_parquet.py <source> <output_dir>
        [--schema SCHEMA]
        [--table TABLE]
        [--top-n N]
        [--no-verify]

<source> may be:
  - a single Parquet file path
  - a glob pattern (e.g. exports/*.parquet)
  - a TransferBatch manifest JSON path (*__manifest.json)

Output: one <schema>__<table>__profile.yaml per source.

The profile YAML matches the format produced by data-profiling and
data-profiling-mysql so it can be loaded by data-profiling-to-dqv-neo4j.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── dependency guards ──────────────────────────────────────────────────────────

def _import_duckdb():
    try:
        import duckdb
        return duckdb
    except ImportError:
        sys.exit(
            "duckdb is required for Parquet profiling.  "
            "Install it with: pip install 'duckdb>=1.1.0,<2.0'"
        )


def _import_pyarrow():
    try:
        import pyarrow.parquet as pq
        return pq
    except ImportError:
        sys.exit(
            "pyarrow is required for Parquet verification.  "
            "Install it with: pip install 'pyarrow>=19.0,<20.0'"
        )


def _import_yaml():
    try:
        import yaml
        return yaml
    except ImportError:
        sys.exit(
            "pyyaml is required.  Install it with: pip install pyyaml"
        )


# ── source resolution ──────────────────────────────────────────────────────────

def _resolve_sources(source: str) -> list[Path]:
    """Expand source to a list of Parquet file paths.

    Accepts a single file, a glob, or a TransferBatch manifest JSON.
    """
    p = Path(source)

    # TransferBatch manifest: extract file_uris
    if p.suffix == ".json" and p.exists():
        with open(p) as f:
            manifest = json.load(f)
        uris = manifest.get("file_uris", [])
        if not uris:
            raise ValueError(f"Manifest {p} has no file_uris")
        paths = []
        for uri in uris:
            # Strip file:// scheme
            file_path = Path(uri[len("file://"):] if uri.startswith("file://") else uri)
            if not file_path.exists():
                raise FileNotFoundError(f"Parquet file not found: {file_path}")
            paths.append(file_path)
        return paths

    # Glob pattern or single file
    matches = glob.glob(source)
    if not matches:
        raise FileNotFoundError(f"No Parquet files found for source: {source!r}")
    return [Path(m) for m in sorted(matches)]


def _schema_table_from_stem(stem: str) -> tuple[str, str]:
    """Derive (schema, table) from a file stem like 'public__orders'."""
    if "__" in stem:
        # Strip trailing __profile if present (manifest naming)
        clean = stem.replace("__manifest", "").replace("__profile", "")
        parts = clean.split("__", 1)
        return parts[0], parts[1]
    return "parquet", stem


# ── DuckDB profiling ───────────────────────────────────────────────────────────

_ARROW_KIND_MAP = {
    "int8": "numeric", "int16": "numeric", "int32": "numeric", "int64": "numeric",
    "uint8": "numeric", "uint16": "numeric", "uint32": "numeric", "uint64": "numeric",
    "float": "numeric", "double": "numeric",
    "decimal": "numeric",
    "date32": "date", "date64": "date",
    "timestamp": "date",
    "time32": "date", "time64": "date",
    "bool": "boolean",
    "large_utf8": "text", "utf8": "text", "string": "text",
}


def _duckdb_kind(duckdb_type: str) -> str:
    t = duckdb_type.lower()
    if any(t.startswith(n) for n in ("int", "tinyint", "smallint", "bigint", "hugeint",
                                      "float", "double", "decimal", "numeric", "real")):
        return "numeric"
    if t in ("boolean", "bool"):
        return "boolean"
    if any(t.startswith(n) for n in ("date", "timestamp", "time", "interval")):
        return "date"
    if any(t.startswith(n) for n in ("varchar", "text", "char", "string", "blob")):
        return "text"
    return "other"


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else round(f, 6)
    except (TypeError, ValueError):
        return None


def _profile_one_file(
    parquet_path: Path,
    schema_name: str,
    table_name: str,
    top_n: int,
    verify: bool,
) -> dict[str, Any]:
    duckdb = _import_duckdb()
    con = duckdb.connect()
    fp = str(parquet_path)
    quoted = fp.replace("'", "''")  # simple SQL-safe escape

    # ── row count (DuckDB) ────────────────────────────────────────────────────
    row_count: int = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{quoted}')"
    ).fetchone()[0]

    # ── column types (from DuckDB describe) ───────────────────────────────────
    desc_rows = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{quoted}')"
    ).fetchall()
    # desc_rows: [(column_name, column_type, null, key, default, extra), ...]
    columns_meta = [(r[0], r[1]) for r in desc_rows]

    # ── per-column stats ──────────────────────────────────────────────────────
    col_profiles = []
    for col_name, col_type in columns_meta:
        kind = _duckdb_kind(col_type)
        safe_col = f'"{col_name}"'

        # Base: null_count, distinct_count
        base = con.execute(f"""
            SELECT
                COUNT(*) - COUNT({safe_col}) AS null_count,
                COUNT(DISTINCT {safe_col})   AS distinct_count
            FROM read_parquet('{quoted}')
        """).fetchone()
        null_count = base[0] or 0
        distinct_count = base[1] or 0
        null_rate = round(null_count / row_count, 6) if row_count > 0 else 0.0

        profile: dict[str, Any] = {
            "name": col_name,
            "dtype": col_type.lower(),
            "null_count": null_count,
            "null_rate": null_rate,
            "distinct_count": distinct_count,
        }

        if kind == "numeric":
            agg = con.execute(f"""
                SELECT
                    MIN({safe_col})  AS min_val,
                    MAX({safe_col})  AS max_val,
                    AVG({safe_col})  AS mean_val,
                    STDDEV({safe_col}) AS stddev_val
                FROM read_parquet('{quoted}')
            """).fetchone()
            profile["min"] = _safe_float(agg[0])
            profile["max"] = _safe_float(agg[1])
            profile["mean"] = _safe_float(agg[2])
            profile["stddev"] = _safe_float(agg[3])

        elif kind == "text":
            agg = con.execute(f"""
                SELECT
                    MIN(LENGTH({safe_col})) AS min_len,
                    MAX(LENGTH({safe_col})) AS max_len,
                    AVG(LENGTH({safe_col})) AS avg_len
                FROM read_parquet('{quoted}')
                WHERE {safe_col} IS NOT NULL
            """).fetchone()
            profile["min_length"] = agg[0]
            profile["max_length"] = agg[1]
            profile["avg_length"] = _safe_float(agg[2])

        elif kind == "date":
            agg = con.execute(f"""
                SELECT MIN({safe_col}), MAX({safe_col})
                FROM read_parquet('{quoted}')
            """).fetchone()
            profile["min"] = str(agg[0]) if agg[0] is not None else None
            profile["max"] = str(agg[1]) if agg[1] is not None else None

        # Top values for text, boolean, other; numeric when cardinality is low
        if kind in ("text", "boolean", "other") or (
            kind == "numeric" and distinct_count <= top_n * 2
        ):
            tv_rows = con.execute(f"""
                SELECT CAST({safe_col} AS VARCHAR) AS val, COUNT(*) AS cnt
                FROM read_parquet('{quoted}')
                WHERE {safe_col} IS NOT NULL
                GROUP BY {safe_col}
                ORDER BY cnt DESC
                LIMIT {top_n}
            """).fetchall()
            profile["top_values"] = [
                {"value": str(r[0]), "count": r[1]} for r in tv_rows
            ]

        col_profiles.append(profile)

    con.close()

    # ── dual-engine verification (pyarrow) ────────────────────────────────────
    if verify:
        pq = _import_pyarrow()
        arrow_table = pq.read_table(str(parquet_path))
        pa_row_count = len(arrow_table)
        if pa_row_count != row_count:
            raise RuntimeError(
                f"Dual-engine row count mismatch on {parquet_path.name}: "
                f"DuckDB={row_count}, pyarrow={pa_row_count}.  "
                "The file may be corrupt or partially written."
            )
        print(
            f"    [verify] DuckDB row_count={row_count}, "
            f"pyarrow row_count={pa_row_count} ✓"
        )

    return {
        "schema": schema_name,
        "table": table_name,
        "profiled_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "row_count": row_count,
        "sample_size": row_count,  # DuckDB reads the full file
        "columns": col_profiles,
    }


# ── main ───────────────────────────────────────────────────────────────────────

def profile_parquet(
    source: str,
    output_dir: str,
    schema_override: str | None = None,
    table_override: str | None = None,
    top_n: int = 10,
    verify: bool = True,
) -> list[str]:
    """Profile one or more Parquet files.  Returns list of written YAML paths."""
    yaml = _import_yaml()
    parquet_paths = _resolve_sources(source)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    for pp in parquet_paths:
        stem = pp.stem.replace("__manifest", "").replace("__profile", "")
        schema_name, table_name = _schema_table_from_stem(stem)
        if schema_override:
            schema_name = schema_override
        if table_override:
            table_name = table_override

        print(f"  Profiling {pp.name} ({schema_name}.{table_name}) …")
        profile = _profile_one_file(pp, schema_name, table_name, top_n, verify)
        out_file = out / f"{schema_name}__{table_name}__profile.yaml"
        with open(out_file, "w") as f:
            yaml.dump(profile, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        print(f"    Written: {out_file}")
        written.append(str(out_file))

    return written


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Profile Parquet files with DuckDB + pyarrow cross-check."
    )
    p.add_argument("source", help="Parquet file, glob pattern, or manifest JSON path")
    p.add_argument("output_dir", help="Directory for profile YAML output")
    p.add_argument("--schema", default=None, help="Override schema name in profile")
    p.add_argument("--table", default=None, help="Override table name in profile")
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the pyarrow dual-engine row count check",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    written = profile_parquet(
        args.source,
        args.output_dir,
        schema_override=args.schema,
        table_override=args.table,
        top_n=args.top_n,
        verify=not args.no_verify,
    )
    print(f"Profiled {len(written)} file(s).")


if __name__ == "__main__":
    main()
