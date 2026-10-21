#!/usr/bin/env python3
"""Lakehouse (Parquet + DuckDB) runner — producer + reader.

Data Workbench runs this (``--mode export``) to serve a product as Parquet; an
engineer runs the identical package to reproduce the export or (``--mode query``)
to explore the data. STDLIB + ``duckdb`` + ``pyarrow`` only (see requirements.txt);
NO ``workbench.*`` imports.

Reads:
  * ``models.json`` — ``[{physical_name, model_name, select_body}]`` — the
    per-dataset DuckDB SELECT bodies compiled by Data Workbench's shared SQL core
    (``generate_view_ddl.generate_lakehouse_models``) and written into the package.
  * ``WB_SOURCE_PLATFORM`` + ``WB_SOURCE_DSN`` env (see ``.env.example``) — the
    live source DuckDB ATTACHes for ``--mode export``.

Writes Parquet + a **TransferBatch v1** manifest per model into ``data/`` and a
``catalog.duckdb`` with a view per model, plus ``run_result.json`` + ``run.log``.

Usage:
  python run.py --mode export [--compression snappy] [--sample-limit N --sample]
  python run.py --mode query  [--limit 5]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from _wb_runresult import RunRecorder


HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
CATALOG = HERE / "catalog.duckdb"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _attach_source(con, platform: str, dsn: str) -> None:
    """ATTACH the live source into DuckDB and USE it (ports lakehouse_export)."""
    plat = (platform or "postgres").lower()
    p = urlparse(dsn)
    if plat in ("postgres", "postgresql"):
        con.execute("INSTALL postgres"); con.execute("LOAD postgres")
        parts = [f"host={p.hostname or 'localhost'}", f"port={p.port or 5432}",
                 f"dbname={(p.path or '/').lstrip('/')}"]
        if p.username:
            parts.append(f"user={unquote(p.username)}")
        if p.password:
            parts.append(f"password={unquote(p.password)}")
        con.execute(f"ATTACH '{' '.join(parts)}' AS wb_src (TYPE postgres, READ_ONLY)")
        con.execute("USE wb_src")
        return
    if plat == "mysql":
        try:
            con.execute("INSTALL mysql")
        except Exception:
            try:
                con.execute("INSTALL mysql FROM community")
            except Exception as install_err:
                raise RuntimeError(
                    "DuckDB mysql extension unavailable — run "
                    "'INSTALL mysql FROM community' in a DuckDB session to "
                    "pre-install it, or ensure network access for auto-install."
                ) from install_err
        con.execute("LOAD mysql")
        parts = [f"host={p.hostname or 'localhost'}", f"port={p.port or 3306}",
                 f"database={(p.path or '/').lstrip('/')}"]
        if p.username:
            parts.append(f"user={unquote(p.username)}")
        if p.password:
            parts.append(f"password={unquote(p.password)}")
        con.execute(f"ATTACH '{' '.join(parts)}' AS wb_src (TYPE mysql, READ_ONLY)")
        con.execute("USE wb_src")
        return
    raise RuntimeError(f"lakehouse export from platform '{platform}' not supported "
                       "(Postgres or MySQL source only).")


def _reconcile(prior: dict | None, batch_id, row_count, schema_fp, checksum) -> dict | None:
    """A ReconciliationEvidence-shaped dict vs the prior manifest (TransferBatch v1)."""
    if not prior:
        return None
    prior_checks = prior.get("file_checksums") or []
    prior_check = prior_checks[0] if prior_checks else ""
    prior_rows = prior.get("row_count")
    prior_fp = prior.get("schema_fingerprint", "")
    if prior_fp != schema_fp:
        status = "schema_changed"
    elif isinstance(prior_rows, int) and prior_rows != row_count:
        status = "row_count_changed"
    elif prior_check and prior_check != checksum:
        status = "data_changed"
    else:
        status = "idempotent"
    return {"status": status, "prior_batch_id": prior.get("batch_id", ""),
            "prior_schema_fingerprint": prior_fp, "prior_row_count": prior_rows,
            "prior_file_checksum": prior_check, "details": {}}


def _load_prior(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _export(rec: RunRecorder, args) -> int:
    # Everything that can raise — imports, the models.json read, and both
    # duckdb.connect() calls — lives INSIDE the try that owns rec.fail(), so a
    # failure ALWAYS writes run_result.json (the backend keys the real error off
    # it). con/catalog_con are pre-bound to None so the finally can close them
    # defensively whether or not connect() got that far.
    con = None
    catalog_con = None
    try:
        import duckdb
        import pyarrow.parquet as pq

        models = json.loads((HERE / "models.json").read_text(encoding="utf-8"))
        platform = os.environ.get("WB_SOURCE_PLATFORM", "postgres")
        dsn = os.environ.get("WB_SOURCE_DSN", "")
        view_schema = os.environ.get("WB_SOURCE_VIEW_SCHEMA", "public")
        if not dsn:
            rec.fail("connection_error",
                     "No source connection. Set WB_SOURCE_DSN (see .env.example).")
            return 1

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        batch_id = uuid.uuid4().hex
        limit = f" LIMIT {int(args.sample_limit)}" if args.sample else ""
        comp = (args.compression or "snappy").upper()

        file_uris, manifest_uris, per_model = [], [], []
        con = duckdb.connect()
        catalog_con = duckdb.connect(str(CATALOG))
        _attach_source(con, platform, dsn)
        for m in models:
            physical, safe, body = m["physical_name"], m["model_name"], m["select_body"]
            parquet_path = DATA_DIR / f"{safe}.parquet"
            tmp = DATA_DIR / f".{safe}.parquet.tmp"
            con.execute(f"COPY (SELECT * FROM ({body}{limit}) _wb) TO '{tmp}' "
                        f"(FORMAT PARQUET, COMPRESSION '{comp}')")
            tmp.replace(parquet_path)

            duck_rows = con.execute(
                f"SELECT count(*) FROM read_parquet('{parquet_path}')").fetchone()[0]
            pqf = pq.ParquetFile(str(parquet_path))
            if duck_rows != pqf.metadata.num_rows:
                raise RuntimeError(
                    f"row-count mismatch for {physical}: duckdb={duck_rows} "
                    f"pyarrow={pqf.metadata.num_rows}")
            arrow_schema = pqf.schema_arrow
            checksum = _sha256_file(parquet_path)
            schema_fp = hashlib.sha256(
                str([(f.name, str(f.type)) for f in arrow_schema]).encode()).hexdigest()

            manifest_path = DATA_DIR / f"{safe}__manifest.json"
            prior = _load_prior(manifest_path)
            manifest = {
                "contract_version": "1",
                "run_id": batch_id, "batch_id": f"{batch_id}:{safe}",
                "source_asset_ref": {
                    "kind": "relational_relation", "platform_instance_id": platform,
                    "asset_id": f"{view_schema}.{physical}", "schema": view_schema,
                    "relation": physical, "relation_kind": "view",
                },
                "schema_version": 1, "schema_fingerprint": schema_fp,
                "canonical_schema": {f.name: {"type": str(f.type), "nullable": True}
                                     for f in arrow_schema},
                "file_format": "parquet", "compression": comp.lower(),
                "file_uris": [parquet_path.resolve().as_uri()],
                "file_checksums": [checksum],
                "row_count": int(duck_rows),
                "byte_count": parquet_path.stat().st_size,
                "column_statistics": [{"column_name": f.name} for f in arrow_schema],
                "operation_encoding": "full_load",
                "completion_marker": "complete",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "prior_run_reconciliation": _reconcile(
                    prior, batch_id, int(duck_rows), schema_fp, checksum),
                "lineage_evidence": {"serving_mode": "lakehouse_local",
                                     "product_relation": physical, "mode": args.mode},
            }
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            catalog_con.execute(
                f'CREATE OR REPLACE VIEW "{safe}" AS '
                f"SELECT * FROM read_parquet('{parquet_path}')")

            file_uris.append(str(parquet_path))
            manifest_uris.append(str(manifest_path))
            per_model.append({"physical_name": physical, "model": safe,
                              "row_count": int(duck_rows), "file": str(parquet_path)})
            rec.step(f"export:{safe}", "success", detail=f"{duck_rows} rows")
    except Exception as e:  # noqa: BLE001
        rec.fail("export_error", str(e))
        return 1
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass
        if catalog_con is not None:
            try:
                catalog_con.close()
            except Exception:  # noqa: BLE001
                pass

    rec.metric("file_uris", file_uris)
    rec.metric("manifest_uris", manifest_uris)
    rec.metric("catalog_ref", str(CATALOG))
    rec.metric("models", per_model)
    rec.metric("model_count", len(per_model))
    rec.finish("success")
    return 0


def _query(rec: RunRecorder, args) -> int:
    import duckdb
    if not CATALOG.exists():
        rec.fail("no_catalog", "catalog.duckdb not found — run `python run.py --mode export` first.")
        return 1
    con = duckdb.connect(str(CATALOG), read_only=True)
    try:
        views = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_type='VIEW' ORDER BY table_name").fetchall()]
        rec.metric("views", views)
        samples = {}
        for v in views:
            rows = con.execute(f'SELECT * FROM "{v}" LIMIT {int(args.limit)}').fetchall()
            samples[v] = len(rows)
            rec.log(f'{v}: {len(rows)} sample row(s)')
        rec.metric("sample_counts", samples)
        rec.finish("success")
        return 0
    except Exception as e:  # noqa: BLE001
        rec.fail("query_error", str(e))
        return 1
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Lakehouse export / query runner.")
    ap.add_argument("--mode", choices=["export", "query"], default="export")
    ap.add_argument("--compression", default="snappy")
    ap.add_argument("--sample", action="store_true", help="Cap rows (sample build).")
    ap.add_argument("--sample-limit", type=int, default=100)
    ap.add_argument("--limit", type=int, default=5, help="Rows per view in query mode.")
    args = ap.parse_args()

    operation = "lakehouse_export" if args.mode == "export" else "lakehouse_query"
    rec = RunRecorder(operation=operation, mode=args.mode,
                      target={"platform": os.environ.get("WB_SOURCE_PLATFORM", "postgres")})
    return _export(rec, args) if args.mode == "export" else _query(rec, args)


if __name__ == "__main__":
    raise SystemExit(main())
