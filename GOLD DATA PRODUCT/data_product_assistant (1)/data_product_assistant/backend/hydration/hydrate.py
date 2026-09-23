"""
hydrate.py — CLI to hydrate the catalog's tables into BigQuery.

SEPARATE from the running app: nothing in server.py / agents imports this. Run
it by hand when you want to (re)create the medallion datasets + tables in
BigQuery from a catalog file.

Examples
--------
  # Dry run (default) — print the DDL for the current catalog, touches nothing:
  python hydration/hydrate.py

  # Dry run against a specific catalog, write the SQL to a file:
  python hydration/hydrate.py --catalog data/utility_catalog_pre_ddi.json --out gold_ddl.sql

  # Include the runtime gold star schema alongside the base catalog:
  python hydration/hydrate.py --catalog data/utility_catalog.json data/utility_catalog_pre_ddi.json

  # LIVE — actually create datasets + tables in BigQuery (needs credentials):
  python hydration/hydrate.py --project my-gcp-project --live

Defaults keep it safe: dry-run, CREATE ... IF NOT EXISTS (idempotent, never
drops data). Use --replace only when you intend to CREATE OR REPLACE.
"""

import argparse
import os
import sys
from pathlib import Path

# Make the sibling hydration modules importable whether run as a script or -m.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from catalog_loader import load_all_tables       # noqa: E402
from bigquery_hydrator import hydrate             # noqa: E402

# backend/  (parent of hydration/) — used to resolve the default catalog path.
_BACKEND_ROOT = _HERE.parent
_DEFAULT_CATALOG = _BACKEND_ROOT / "data" / "utility_catalog.json"


def _default_project() -> str:
    return os.environ.get("BQ_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT") or ""


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hydrate",
        description="Hydrate catalog tables into BigQuery datasets (bronze/silver/gold).",
    )
    p.add_argument(
        "--catalog", nargs="+", default=[str(_DEFAULT_CATALOG)],
        help="One or more catalog JSON files (default: data/utility_catalog.json).",
    )
    p.add_argument(
        "--project", default=_default_project(),
        help="BigQuery project id (default: $BQ_PROJECT or $GOOGLE_CLOUD_PROJECT).",
    )
    p.add_argument(
        "--location", default=os.environ.get("BQ_LOCATION", "us-central1"),
        help="BigQuery location for created datasets (default: us-central1).",
    )
    p.add_argument(
        "--layers", nargs="+", default=None, choices=["bronze", "silver", "gold"],
        help="Restrict to these medallion layers (default: all present).",
    )
    p.add_argument("--replace", action="store_true",
                   help="CREATE OR REPLACE tables (DESTRUCTIVE — drops existing data).")
    p.add_argument("--load-sample-data", action="store_true",
                   help="Also emit INSERTs for any table that has sample_data rows.")
    p.add_argument("--live", action="store_true",
                   help="Actually execute against BigQuery (default: dry-run).")
    p.add_argument("--out", default=None,
                   help="Write the generated SQL to this file.")
    return p


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    if not args.project:
        # Dry-run still needs a project id to build fully-qualified names; use a
        # clearly-placeholder value so nobody mistakes the output for live SQL.
        if args.live:
            print("ERROR: --project (or $BQ_PROJECT/$GOOGLE_CLOUD_PROJECT) is required for --live.",
                  file=sys.stderr)
            return 2
        args.project = "YOUR_PROJECT"
        print("[hydrate] no project set — using placeholder 'YOUR_PROJECT' for dry-run SQL.\n",
              file=sys.stderr)

    layers = tuple(args.layers) if args.layers else None
    tables = load_all_tables(args.catalog, layers=layers)
    if not tables:
        print("[hydrate] no tables found in the given catalog(s).", file=sys.stderr)
        return 1

    result = hydrate(
        tables,
        project=args.project,
        dry_run=not args.live,
        replace=args.replace,
        location=args.location,
        load_sample_data=args.load_sample_data,
    )

    header = (
        f"-- {'LIVE' if args.live else 'DRY-RUN'} · project={result['project']} "
        f"· {result['table_count']} tables in {result['dataset_count']} datasets"
        f" ({result['insert_count']} inserts)\n"
    )
    sql = header + result["sql"]

    if args.out:
        Path(args.out).write_text(sql, encoding="utf-8")
        print(f"[hydrate] wrote SQL to {args.out}")
    else:
        print(sql)

    if args.live:
        errors = [r for r in result.get("execution", []) if r["status"] == "error"]
        print(f"\n[hydrate] executed {len(result['execution'])} statements, "
              f"{len(errors)} error(s).", file=sys.stderr)
        for e in errors:
            print(f"  ERROR: {e['error']}\n    in: {e['statement'][:120]}…", file=sys.stderr)
        return 1 if errors else 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
