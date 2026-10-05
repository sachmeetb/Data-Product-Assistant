#!/usr/bin/env python3
"""Emit the framework-neutral ``migration.json`` from discovered source metadata.

Reads the per-table YAML files that `data-discovery` wrote (``<schema>__<table>.yaml``
with keys ``schema`` / ``table`` / ``columns`` / ``primary_key``) and produces the
migration contract the runnable package + the backend consume:

    {
      "source_platform": "...", "target_platform": "...",
      "target_schema": "...", "write_disposition": "replace",
      "datasets": [
        {"source_schema": "...", "source_table": "...", "target_table": "...",
         "write_disposition": "replace", "primary_key": ["id"]}
      ]
    }

This is the DLT reference implementation's *config* step. It is framework-neutral
on purpose — a different framework's generator would consume the same discovery
YAML and emit the same ``migration.json`` (only the runner differs). NO dlt import
here; dlt executes only in the downloadable package's ``run.py``.

Raw / lift-and-shift landing: source names + types are preserved (target_table ==
source_table), no transformation.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - environments without pyyaml
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyyaml", "-q"])
    import yaml


def _primary_key(meta: dict) -> list:
    pk = meta.get("primary_key")
    if isinstance(pk, dict):
        return list(pk.get("columns") or [])
    if isinstance(pk, list):
        return pk
    return []


def _datasets_from_discovery(ddir: str, write_disposition: str) -> list:
    """Enumerate datasets from the per-table YAML `data-discovery` wrote."""
    out = []
    for fn in sorted(os.listdir(ddir)):
        if not fn.endswith((".yaml", ".yml")):
            continue
        try:
            with open(os.path.join(ddir, fn), encoding="utf-8") as f:
                meta = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as e:
            print(f"  skipping {fn}: {e}", file=sys.stderr)
            continue
        table = meta.get("table") or meta.get("table_name")
        if not table:
            continue
        out.append({
            "source_schema": meta.get("schema", ""),
            "source_table": table,
            "target_table": table,
            "write_disposition": write_disposition,
            "primary_key": _primary_key(meta),
        })
    return out


def _datasets_from_reflection(source_url: str, schema: str, write_disposition: str) -> list:
    """Enumerate datasets by reflecting the live source via SQLAlchemy — the path
    for THIN sources (Oracle / SQL Server) that have no data-discovery skill. dlt
    reflects the schema too; this mirrors that so migration.json can be produced
    without a Workbench DiscoveryProvider."""
    from sqlalchemy import create_engine, inspect
    engine = create_engine(source_url)
    insp = inspect(engine)
    schemas = [schema] if schema else (insp.get_schema_names() or [None])
    out = []
    for sch in schemas:
        for table in insp.get_table_names(schema=sch) or []:
            try:
                pk = (insp.get_pk_constraint(table, schema=sch) or {}).get("constrained_columns") or []
            except Exception:  # noqa: BLE001 — PK reflection is best-effort
                pk = []
            out.append({
                "source_schema": sch or "",
                "source_table": table,
                "target_table": table,
                "write_disposition": write_disposition,
                "primary_key": list(pk),
            })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate migration.json from discovery YAML.")
    ap.add_argument("--discovery-dir", default="data_discovery",
                    help="Directory holding <schema>__<table>.yaml files.")
    ap.add_argument("--output", default="migration/migration.json",
                    help="Where to write migration.json (relative to cwd).")
    ap.add_argument("--source-platform", default="postgres")
    ap.add_argument("--target-platform", required=True)
    ap.add_argument("--target-schema", default="",
                    help="Schema the migrated tables land in on the target.")
    ap.add_argument("--write-disposition", default="replace", choices=["replace", "append"])
    # Reflection mode (thin sources with no data-discovery skill, e.g. Oracle /
    # SQL Server). Provide a SQLAlchemy --source-url (or WB_SOURCE_URL env); the
    # generator enumerates tables + PKs directly. --source-schema scopes it.
    ap.add_argument("--reflect", action="store_true",
                    help="Enumerate tables by reflecting the live source via SQLAlchemy.")
    ap.add_argument("--source-url", default="", help="SQLAlchemy URL for --reflect.")
    ap.add_argument("--source-schema", default="", help="Schema to scope reflection to.")
    args = ap.parse_args()

    if args.reflect:
        source_url = args.source_url or os.environ.get("WB_SOURCE_URL", "")
        if not source_url:
            print("--reflect needs --source-url (or WB_SOURCE_URL env).", file=sys.stderr)
            return 1
        try:
            datasets = _datasets_from_reflection(source_url, args.source_schema, args.write_disposition)
        except Exception as e:  # noqa: BLE001
            print(f"reflection failed: {e}", file=sys.stderr)
            return 1
    else:
        ddir = args.discovery_dir
        if not os.path.isdir(ddir):
            print(f"discovery dir '{ddir}' not found — run data discovery first, "
                  "or use --reflect with --source-url for a thin source.", file=sys.stderr)
            return 1
        datasets = _datasets_from_discovery(ddir, args.write_disposition)

    if not datasets:
        print("no source tables found — nothing to migrate.", file=sys.stderr)
        return 1

    spec = {
        "source_platform": args.source_platform,
        "target_platform": args.target_platform,
        "target_schema": args.target_schema,
        "write_disposition": args.write_disposition,
        "datasets": datasets,
    }
    out = args.output
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2)
    print(f"Wrote {out} with {len(datasets)} dataset(s): "
          + ", ".join(d["source_table"] for d in datasets))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
