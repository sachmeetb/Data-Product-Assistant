#!/usr/bin/env python3
"""discover_parquet.py — extract schema metadata from a directory of Parquet
files via DuckDB, emitting one YAML file per Parquet file into
``<output_dir>/data_discovery/``.

Output shape matches the relational discovery skills (schema/table/columns/…)
so ``data-discovery-to-dcat-neo4j`` consumes it unchanged. Files carry no
relational constraints, so primary_key / foreign_keys / indexes are empty.

Self-installs duckdb + pyyaml if missing.
"""
from __future__ import annotations

import argparse
import glob as _glob
import os
import subprocess
import sys
from pathlib import Path


def _ensure(pkg: str, import_name: str | None = None):
    try:
        __import__(import_name or pkg)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", pkg])


def main() -> int:
    ap = argparse.ArgumentParser(description="Discover Parquet file schemas via DuckDB.")
    ap.add_argument("--dir", required=True, help="Base directory of Parquet files")
    ap.add_argument("--glob", default="*.parquet", help="File glob (default *.parquet)")
    ap.add_argument("--output-dir", default=".", help="Where to write data_discovery/")
    ap.add_argument("--project-code", default="", help="Project code (for scoping; informational)")
    args = ap.parse_args()

    _ensure("duckdb")
    _ensure("pyyaml", "yaml")
    import duckdb
    import yaml

    base = Path(args.dir)
    if not base.exists():
        print(f"ERROR: directory not found: {base}", file=sys.stderr)
        return 2

    files = sorted(_glob.glob(str(base / args.glob)))
    if not files:
        print(f"No Parquet files matching {args.glob!r} under {base}", file=sys.stderr)
        return 1

    out_dir = Path(args.output_dir) / "data_discovery"
    out_dir.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    written = []
    for path in files:
        p = Path(path)
        schema = base.name or "parquet"
        table = p.stem
        try:
            cols = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()
            row_count = con.execute(
                f"SELECT count(*) FROM read_parquet('{path}')"
            ).fetchone()[0]
        except Exception as e:  # noqa: BLE001
            print(f"  WARNING: failed {p.name}: {e}", file=sys.stderr)
            continue
        metadata = {
            "schema": schema,
            "table": table,
            "comment": "",
            "source_file": str(p),
            "row_count": int(row_count),
            "columns": [
                {
                    "name": c[0],
                    "ordinal": i + 1,
                    "type": c[1],
                    "nullable": (c[2] == "YES") if len(c) > 2 else True,
                    "default": None,
                    "comment": None,
                }
                for i, c in enumerate(cols)
            ],
            "primary_key": None,
            "foreign_keys": [],
            "unique_constraints": [],
            "indexes": [],
            "check_constraints": [],
        }
        filename = f"{schema}__{table}.yaml"
        with (out_dir / filename).open("w") as f:
            yaml.dump(metadata, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        written.append(filename)
        print(f"  Written: {filename} ({len(metadata['columns'])} columns, {row_count} rows)")

    con.close()
    print(f"\nDiscovered {len(written)} Parquet file(s) into {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
