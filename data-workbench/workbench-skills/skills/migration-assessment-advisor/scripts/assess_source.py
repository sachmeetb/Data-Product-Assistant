#!/usr/bin/env python3
"""Assess a discovered source for migration — emit ``assessment.json``.

Reads the per-table YAML the `data-discovery` skill wrote and produces a compact,
deterministic assessment the engineer (and the generator) can act on:

  * per-table column count + primary key
  * incremental-cursor candidates (monotonic PK, ``updated_at`` / timestamp cols)
  * a recommended write mode (``append`` when a cursor exists, else ``full_replace``)

Type-conversion loss is reasoned about by the SKILL agent using the bundled
per-platform corpus (``reference/platforms/``) and the canonical type system —
this script stays standalone (no ``workbench.*`` import) and structural.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyyaml", "-q"])
    import yaml

# Column-name hints for an incremental cursor (case-insensitive substring match).
_CURSOR_NAME_HINTS = ("updated_at", "modified", "last_modified", "changed_at",
                      "created_at", "_ts", "timestamp", "load_date", "etl_")
_TS_TYPE_HINTS = ("timestamp", "datetime", "date")


def _cursor_candidates(meta: dict, pk_cols: list) -> list:
    out = []
    for col in meta.get("columns") or []:
        name = str(col.get("name", ""))
        ctype = str(col.get("type", "")).lower()
        low = name.lower()
        if any(h in low for h in _CURSOR_NAME_HINTS) or any(h in ctype for h in _TS_TYPE_HINTS):
            out.append({"column": name, "type": col.get("type"), "reason": "timestamp-like"})
    # A single integer PK is a monotonic cursor candidate.
    if len(pk_cols) == 1:
        for col in meta.get("columns") or []:
            if col.get("name") == pk_cols[0] and "int" in str(col.get("type", "")).lower():
                out.append({"column": pk_cols[0], "type": col.get("type"), "reason": "monotonic-int-pk"})
    return out


def _primary_key(meta: dict) -> list:
    pk = meta.get("primary_key")
    if isinstance(pk, dict):
        return list(pk.get("columns") or [])
    if isinstance(pk, list):
        return pk
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description="Assess discovered source for migration.")
    ap.add_argument("--discovery-dir", default="data_discovery")
    ap.add_argument("--output", default="assessment.json")
    ap.add_argument("--target-platform", default="")
    args = ap.parse_args()

    if not os.path.isdir(args.discovery_dir):
        print(f"discovery dir '{args.discovery_dir}' not found — run discovery first.", file=sys.stderr)
        return 1

    tables = []
    for fn in sorted(os.listdir(args.discovery_dir)):
        if not fn.endswith((".yaml", ".yml")):
            continue
        try:
            with open(os.path.join(args.discovery_dir, fn), encoding="utf-8") as f:
                meta = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            continue
        table = meta.get("table") or meta.get("table_name")
        if not table:
            continue
        pk = _primary_key(meta)
        cursors = _cursor_candidates(meta, pk)
        tables.append({
            "schema": meta.get("schema", ""),
            "table": table,
            "column_count": len(meta.get("columns") or []),
            "primary_key": pk,
            "has_primary_key": bool(pk),
            "incremental_cursor_candidates": cursors,
            "recommended_write_mode": "append" if cursors else "full_replace",
            "foreign_key_count": len(meta.get("foreign_keys") or []),
        })

    assessment = {
        "target_platform": args.target_platform,
        "landing_strategy": "raw",
        "table_count": len(tables),
        "tables": tables,
        "notes": (
            "Raw / lift-and-shift landing: preserve original names + types. "
            "Review the per-platform type caveats in the skill's reference corpus and "
            "flag any lossy / ambiguous conversions for the target before running."
        ),
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(assessment, f, indent=2)
    print(f"Wrote {args.output} — assessed {len(tables)} table(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
