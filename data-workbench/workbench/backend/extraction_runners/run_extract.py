#!/usr/bin/env python3
"""Client-run, offline metadata extractor — the executable entry point.

Runs entirely in the CLIENT's environment: connects to their platform with their
own ``WB_SOURCE_*`` credentials (which Data Workbench never sees), lets them pick
schemas/tables in an interactive terminal picker, pulls metadata + volumetrics +
guarded profiling, and writes a **reviewable** ``estate-manifest-<ts>.yaml`` the
client reads before uploading to Data Workbench.

  * NO ``workbench.*`` imports · NO LLM/agent · read-only introspection only.
  * The CLI *is* the selection UI — no file editing (``--all`` / ``--selection``
    are the non-interactive escapes for CI / demo / no-TTY).

Usage:
  python run.py extract [--profile/--no-profile] [--volumetrics/--no-volumetrics]
     [--code-assets] [--include-values/--no-values] [--sample-limit N] [--top-n N]
     [--max-enum-cardinality K] [--exclude-column schema.table.col ...]
     [--all | --selection selection-<ts>.yaml] [--out-dir DIR]
  python run.py list      # just print the visible inventory (no extraction)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

import _extract_core as core
import _manifest_writer as mw

try:
    from _wb_runresult import RunRecorder
except ImportError:  # dev: repo extraction_runners/ borrows the shared recorder
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "serving_runners"))
    from _wb_runresult import RunRecorder

HERE = Path(__file__).resolve().parent


def _load_config() -> dict:
    cfg_path = HERE / "manifest_config.json"
    if cfg_path.exists():
        try:
            return json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {}


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# ── selection (interactive picker + non-interactive escapes) ──────────────────

def _all_selection(extractor) -> dict[str, list[str]]:
    sel: dict[str, list[str]] = {}
    for schema in extractor.list_schemas():
        sel[schema] = [r["name"] for r in extractor.list_relations(schema)]
    return sel


def _interactive_selection(extractor) -> dict[str, list[str]]:
    """Two-level terminal picker: schemas (with select-all) → tables per schema
    (with select-all-in-schema). Falls back to select-all if questionary is
    unavailable."""
    try:
        import questionary
    except ImportError:
        print("[warn] questionary not installed — selecting ALL schemas/tables. "
              "Install requirements.txt or pass --all/--selection.", file=sys.stderr)
        return _all_selection(extractor)

    schemas = extractor.list_schemas()
    if not schemas:
        return {}
    ALL = "★ (select all schemas)"
    picked = questionary.checkbox(
        "Select schemas to extract:", choices=[ALL] + schemas).ask()
    if picked is None:
        return {}
    chosen_schemas = schemas if ALL in picked else [s for s in picked if s != ALL]

    sel: dict[str, list[str]] = {}
    for schema in chosen_schemas:
        tables = [r["name"] for r in extractor.list_relations(schema)]
        if not tables:
            continue
        ALLT = f"★ (all tables in {schema})"
        picked_t = questionary.checkbox(
            f"Tables in '{schema}':", choices=[ALLT] + tables).ask()
        if picked_t is None:
            continue
        sel[schema] = tables if ALLT in picked_t else [t for t in picked_t if t != ALLT]
    return {s: t for s, t in sel.items() if t}


def _replay_selection(path: str) -> dict[str, list[str]]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {str(s): [str(t) for t in tbls]
            for s, tbls in (data.get("selected") or {}).items()}


def _resolve_selection(args, extractor) -> dict[str, list[str]]:
    if args.selection:
        return _replay_selection(args.selection)
    if args.all:
        return _all_selection(extractor)
    if not sys.stdin.isatty():
        print("[info] no TTY — falling back to --all (every schema/table). "
              "Use --selection to replay a prior pick headlessly.", file=sys.stderr)
        return _all_selection(extractor)
    return _interactive_selection(extractor)


# ── extraction ─────────────────────────────────────────────────────────────────

def _extract(extractor, selection, args, cfg, rec) -> list[dict]:
    excludes = {e.lower() for e in (args.exclude_column or [])}
    relations: list[dict] = []
    for schema, tables in selection.items():
        want = set(tables)
        rel_meta = {r["name"]: r for r in extractor.list_relations(schema)}
        fk_by_table: dict[str, list[dict]] = {}
        if args.volumetrics or args.profile:
            for fk in extractor.list_foreign_keys(schema):
                fk_by_table.setdefault(fk["from_table"], []).append({
                    "from_column": fk["from_column"], "to_schema": fk["to_schema"],
                    "to_table": fk["to_table"], "to_column": fk["to_column"]})
        for table in tables:
            if table not in rel_meta and table not in want:
                continue
            rel = dict(rel_meta.get(table) or {"name": table, "relation_kind": "table"})
            rel["schema"] = schema
            rel["table"] = table
            cols = extractor.list_columns(schema, table)
            for c in cols:
                key = f"{schema}.{table}.{c['name']}".lower()
                if args.profile and key not in excludes:
                    prof = extractor.profile_column(
                        schema, table, c, args.sample_limit, args.top_n)
                    if prof is not None:
                        c["profile"] = prof
            rel["columns"] = cols
            rel["foreign_keys"] = fk_by_table.get(table, [])
            relations.append(rel)
            rec.log(f"extracted {schema}.{table} ({len(cols)} cols)")
    return relations


def main() -> int:
    cfg = _load_config()
    ap = argparse.ArgumentParser(description="Offline metadata extractor for Data Workbench.")
    ap.add_argument("command", nargs="?", default="extract", choices=["extract", "list"])
    ap.add_argument("--profile", dest="profile", action="store_true", default=cfg.get("profiling_default", True))
    ap.add_argument("--no-profile", dest="profile", action="store_false")
    ap.add_argument("--volumetrics", dest="volumetrics", action="store_true", default=cfg.get("volumetrics_default", True))
    ap.add_argument("--no-volumetrics", dest="volumetrics", action="store_false")
    ap.add_argument("--code-assets", dest="code_assets", action="store_true", default=False)
    ap.add_argument("--include-values", dest="include_values", action="store_true", default=cfg.get("include_values_default", True))
    ap.add_argument("--no-values", dest="include_values", action="store_false")
    ap.add_argument("--sample-limit", type=int, default=cfg.get("sample_limit", 100000))
    ap.add_argument("--top-n", type=int, default=cfg.get("top_n", 10))
    ap.add_argument("--max-enum-cardinality", type=int, default=cfg.get("max_enum_cardinality", 50))
    ap.add_argument("--max-value-length", type=int, default=cfg.get("max_value_length", 200))
    ap.add_argument("--exclude-column", action="append", default=[])
    ap.add_argument("--all", action="store_true", help="Non-interactive: every schema + table.")
    ap.add_argument("--selection", default=None, help="Replay a prior selection-<ts>.yaml.")
    ap.add_argument("--out-dir", default=".")
    args = ap.parse_args()

    ts = _ts()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rec = RunRecorder(operation="extract_manifest", mode=args.command,
                      result_path=str(out_dir / f"extract_result-{ts}.json"),
                      log_path=str(out_dir / "run.log"))

    platform, env = core.env_conn_params()
    if not env.get("dsn") and not env.get("host") and not env.get("account"):
        rec.fail("connection_error",
                 "No source connection configured. Set WB_SOURCE_* (see .env.example).")
        return 1
    catalog = cfg.get("catalog") or core.database_segment(platform, env)

    try:
        extractor = core.get_extractor(platform, env)
    except ValueError as e:
        rec.fail("unsupported_platform", str(e))
        return 1
    try:
        extractor.connect()
    except Exception as e:  # driver-specific
        rec.fail("connection_error", f"connect failed: {e}")
        return 1
    rec.log(f"connected to {platform} (catalog={catalog or '-'})")

    try:
        if args.command == "list":
            for schema in extractor.list_schemas():
                rels = extractor.list_relations(schema)
                print(f"{schema}  ({len(rels)} relations)")
                for r in rels:
                    print(f"    {r['name']}  [{r.get('relation_kind', 'table')}]")
            rec.metric("schemas_listed", 1)
            rec.finish("success")
            return 0

        selection = _resolve_selection(args, extractor)
        if not selection:
            rec.fail("empty_selection", "No schemas/tables selected — nothing to extract.")
            return 1

        # Persist the exact picks (replayable, not for hand-editing).
        sel_path = out_dir / f"selection-{ts}.yaml"
        mw.write_yaml_atomic(str(sel_path), {
            "platform": platform, "catalog": catalog, "selected": selection})

        relations = _extract(extractor, selection, args, cfg, rec)

        redcfg = mw.RedactionConfig(
            include_values=args.include_values,
            max_enum_cardinality=args.max_enum_cardinality,
            max_value_length=args.max_value_length,
            pii_tokens=tuple(cfg.get("pii_tokens") or core._DEFAULT_PII_TOKENS))
        manifest = mw.build_manifest(
            platform=platform, catalog=catalog, relations=relations, code_assets=[],
            cfg=redcfg, include_volumetrics=args.volumetrics,
            include_profiling=args.profile, include_code_assets=args.code_assets,
            generated_at=datetime.now(timezone.utc).isoformat())

        manifest_path = out_dir / f"estate-manifest-{ts}.yaml"
        mw.write_yaml_atomic(str(manifest_path), manifest)

        red = manifest["extraction"]["redaction"]
        rec.metric("schemas", len(selection))
        rec.metric("relations", len(relations))
        rec.metric("columns", sum(len(r["columns"]) for r in relations))
        rec.metric("redaction", red)
        rec.metric("manifest_file", manifest_path.name)
        rec.step("write_manifest", "success",
                 detail=f"{len(relations)} relations → {manifest_path.name}")
        rec.finish("success")
        print(f"\n✔ wrote {manifest_path}")
        print(f"  review it, then upload to Data Workbench. "
              f"Redaction: {red or 'none'}")
        return 0
    finally:
        extractor.close()


if __name__ == "__main__":
    raise SystemExit(main())
