#!/usr/bin/env python3
"""Data-migration runner — DLT source→target loader (reference implementation).

Data Workbench runs this (``--mode load``) to migrate a source to a target
platform; an engineer downloads the identical package and runs it to reproduce
the migration. STDLIB + ``dlt`` + a SQLAlchemy driver only (see requirements.txt);
NO ``workbench.*`` imports — the package is self-contained.

This is the ONE place ``dlt`` executes for migration; the backend never imports
``dlt`` on a request path (framework isolation — a different framework's package
would ship a different ``run.py``).

Reads:
  * ``migration.json`` — the framework-neutral migration contract compiled by the
    ``migration-pipeline-generator-dlt`` skill:
        {
          "source_platform": "postgres",
          "target_platform": "snowflake",
          "target_schema": "MIGRATED",
          "write_disposition": "replace",
          "datasets": [
            {"source_schema": "public", "source_table": "employees",
             "target_table": "employees", "write_disposition": "replace",
             "primary_key": ["id"], "incremental_cursor": "updated_at"}
          ]
        }
  * ``WB_SOURCE_*`` / ``WB_TARGET_*`` env (see ``.env.example``) — the live source
    to read and the target to load. Secrets ride the env only, never disk.

Writes a **TransferBatch v1** manifest per dataset into ``manifests/`` plus
``run_result.json`` + ``run.log``.

Usage:
  python run.py --mode load       # extract from source, load into target
  python run.py --mode verify     # count source vs target rows (reconciliation)
  python run.py --mode plan       # print what would run, touch nothing
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

from _wb_runresult import RunRecorder

HERE = Path(__file__).resolve().parent
MANIFEST_DIR = HERE / "manifests"


# ── connection env → SQLAlchemy source URL ──────────────────────────────────

def _source_sqlalchemy_url(platform: str) -> str:
    """Build a SQLAlchemy URL for the source from WB_SOURCE_* env.

    Honors a full ``WB_SOURCE_DSN`` when present; otherwise assembles from the
    discrete WB_SOURCE_{HOST,PORT,USER,PASSWORD,DBNAME} parts.
    """
    plat = (platform or "postgres").lower()
    dsn = os.environ.get("WB_SOURCE_DSN", "").strip()
    if dsn:
        # Normalize the bare postgres/mysql scheme to a SQLAlchemy driver scheme.
        if dsn.startswith("postgres://") or dsn.startswith("postgresql://"):
            return "postgresql+psycopg2://" + dsn.split("://", 1)[1]
        if dsn.startswith("mysql://"):
            return "mysql+pymysql://" + dsn.split("://", 1)[1]
        return dsn

    host = os.environ.get("WB_SOURCE_HOST", "localhost")
    user = os.environ.get("WB_SOURCE_USER", "")
    pw = os.environ.get("WB_SOURCE_PASSWORD", "")
    db = os.environ.get("WB_SOURCE_DBNAME", "")
    auth = f"{quote_plus(user)}:{quote_plus(pw)}@" if user else ""
    if plat in ("postgres", "postgresql"):
        port = os.environ.get("WB_SOURCE_PORT", "5432")
        return f"postgresql+psycopg2://{auth}{host}:{port}/{db}"
    if plat == "mysql":
        port = os.environ.get("WB_SOURCE_PORT", "3306")
        return f"mysql+pymysql://{auth}{host}:{port}/{db}"
    if plat == "snowflake":
        # snowflake-sqlalchemy: snowflake://user:pw@account/database/schema?warehouse=..&role=..
        # A Snowflake PAT authenticates as a plain password.
        account = os.environ.get("WB_SOURCE_ACCOUNT", "") or host
        warehouse = os.environ.get("WB_SOURCE_WAREHOUSE", "")
        role = os.environ.get("WB_SOURCE_ROLE", "")
        schema = os.environ.get("WB_SOURCE_SCHEMA", "")
        path = f"/{db}/{schema}" if (db and schema) else (f"/{db}" if db else "")
        params = []
        if warehouse:
            params.append(f"warehouse={quote_plus(warehouse)}")
        if role:
            params.append(f"role={quote_plus(role)}")
        query = ("?" + "&".join(params)) if params else ""
        return f"snowflake://{auth}{account}{path}{query}"
    if plat == "oracle":
        # oracledb (thin) — the DBNAME is the service name.
        port = os.environ.get("WB_SOURCE_PORT", "1521")
        tail = f"/?service_name={db}" if db else ""
        return f"oracle+oracledb://{auth}{host}:{port}{tail}"
    if plat in ("sqlserver", "mssql", "azure_sql"):
        port = os.environ.get("WB_SOURCE_PORT", "1433")
        driver = os.environ.get("WB_SOURCE_ODBC_DRIVER", "ODBC Driver 18 for SQL Server")
        return (f"mssql+pyodbc://{auth}{host}:{port}/{db}"
                f"?driver={quote_plus(driver)}&TrustServerCertificate=yes")
    raise RuntimeError(
        f"source platform '{platform}' is not supported by this DLT runner "
        "(postgres / mysql / snowflake / oracle / sqlserver in this reference implementation).")


# ── connection env → dlt destination ────────────────────────────────────────

def _build_destination(dlt, platform: str):
    """Construct a dlt destination from WB_TARGET_* env.

    The dlt destination-config surface is deliberately explicit here so the
    package is self-documenting; per-deployment tuning may be needed for
    warehouse targets (see the DLT reference docs bundled in the skill).
    """
    plat = (platform or "postgres").lower()

    def _env(key, default=""):
        return os.environ.get(f"WB_TARGET_{key}", default)

    if plat in ("postgres", "postgresql"):
        dsn = _env("DSN")
        if not dsn:
            auth = ""
            if _env("USER"):
                auth = f"{quote_plus(_env('USER'))}:{quote_plus(_env('PASSWORD'))}@"
            dsn = (f"postgresql://{auth}{_env('HOST', 'localhost')}:"
                   f"{_env('PORT', '5432')}/{_env('DBNAME')}")
        return dlt.destinations.postgres(credentials=dsn)

    if plat == "mysql":
        # dlt reaches MySQL via the SQLAlchemy destination.
        auth = ""
        if _env("USER"):
            auth = f"{quote_plus(_env('USER'))}:{quote_plus(_env('PASSWORD'))}@"
        url = (f"mysql+pymysql://{auth}{_env('HOST', 'localhost')}:"
               f"{_env('PORT', '3306')}/{_env('DBNAME')}")
        return dlt.destinations.sqlalchemy(credentials=url)

    if plat == "snowflake":
        creds = {
            "host": _env("ACCOUNT") or _env("HOST"),   # Snowflake account identifier
            "username": _env("USER"),
            "password": _env("PASSWORD"),
            "database": _env("DBNAME"),
            "warehouse": _env("WAREHOUSE"),
            "role": _env("ROLE"),
        }
        return dlt.destinations.snowflake(credentials={k: v for k, v in creds.items() if v})

    if plat == "databricks":
        creds = {
            "server_hostname": _env("HOST"),
            "http_path": _env("HTTP_PATH"),
            "access_token": _env("TOKEN") or _env("PASSWORD"),
            "catalog": _env("CATALOG"),
        }
        return dlt.destinations.databricks(credentials={k: v for k, v in creds.items() if v})

    if plat in ("sqlserver", "mssql", "azure_sql"):
        # dlt native mssql destination (dlt[mssql]).
        driver = _env("ODBC_DRIVER") or "ODBC Driver 18 for SQL Server"
        auth = ""
        if _env("USER"):
            auth = f"{quote_plus(_env('USER'))}:{quote_plus(_env('PASSWORD'))}@"
        url = (f"mssql://{auth}{_env('HOST', 'localhost')}:{_env('PORT', '1433')}/"
               f"{_env('DBNAME')}?driver={quote_plus(driver)}")
        return dlt.destinations.mssql(credentials=url)

    if plat == "oracle":
        # dlt reaches Oracle via its SQLAlchemy destination (oracle+oracledb).
        auth = ""
        if _env("USER"):
            auth = f"{quote_plus(_env('USER'))}:{quote_plus(_env('PASSWORD'))}@"
        tail = f"/?service_name={_env('DBNAME')}" if _env("DBNAME") else ""
        url = f"oracle+oracledb://{auth}{_env('HOST', 'localhost')}:{_env('PORT', '1521')}{tail}"
        return dlt.destinations.sqlalchemy(credentials=url)

    raise RuntimeError(
        f"target platform '{platform}' is not supported by this DLT runner "
        "(postgres / mysql / snowflake / databricks / sqlserver / oracle).")


def _target_schema(spec: dict) -> str:
    return spec.get("target_schema") or os.environ.get("WB_TARGET_SCHEMA") or "migrated"


# ── manifest ────────────────────────────────────────────────────────────────

def _write_manifest(run_id: str, ds: dict, src_platform: str, row_count, disposition: str) -> Path:
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    schema = ds.get("source_schema", "")
    table = ds["source_table"]
    safe = f"{schema}__{table}" if schema else table
    op = {"replace": "full_load", "append": "cdc_append",
          "merge": "upsert"}.get(disposition, "full_load")
    manifest = {
        "contract_version": "1",
        "run_id": run_id,
        "batch_id": f"{run_id}:{safe}",
        "source_asset_ref": {
            "kind": "relational_relation",
            "platform_instance_id": src_platform,
            "asset_id": f"{schema}.{table}" if schema else table,
            "schema": schema,
            "relation": table,
            "relation_kind": "table",
        },
        "schema_version": 1,
        "schema_fingerprint": hashlib.sha256(f"{schema}.{table}".encode()).hexdigest(),
        "file_format": "parquet",   # placeholder — direct DB→DB, no file payload
        "file_uris": [],
        "file_checksums": [],
        "row_count": int(row_count) if row_count is not None else None,
        "operation_encoding": op,
        "primary_key_columns": ds.get("primary_key", []),
        "completion_marker": "complete",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "prior_run_reconciliation": None,
        "lineage_evidence": {"serving_mode": "migration", "framework": "dlt",
                             "target_relation": ds.get("target_table", table)},
    }
    path = MANIFEST_DIR / f"{safe}__manifest.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


# ── source row counts (SQLAlchemy) ──────────────────────────────────────────

def _count_source_rows(engine, schema: str, table: str) -> int | None:
    from sqlalchemy import text
    rel = f'"{schema}"."{table}"' if schema else f'"{table}"'
    try:
        with engine.connect() as conn:
            return int(conn.execute(text(f"SELECT count(*) FROM {rel}")).scalar())
    except Exception:  # noqa: BLE001 — count is best-effort evidence
        return None


# ── modes ───────────────────────────────────────────────────────────────────

def _load(rec: RunRecorder, spec: dict, run_id: str, dlt_dir: str) -> int:
    try:
        import dlt
        from dlt.sources.sql_database import sql_table
        from sqlalchemy import create_engine
    except ImportError as e:
        rec.fail("engine_unavailable",
                 f"dlt (+ SQLAlchemy driver) not installed: {e}. "
                 "pip install -r requirements.txt")
        return 1

    src_platform = spec.get("source_platform", "postgres")
    tgt_platform = spec.get("target_platform", "postgres")
    datasets = spec.get("datasets", [])
    default_disp = spec.get("write_disposition", "replace")
    schema = _target_schema(spec)

    try:
        src_url = _source_sqlalchemy_url(src_platform)
        engine = create_engine(src_url)
        destination = _build_destination(dlt, tgt_platform)
    except Exception as e:  # noqa: BLE001
        rec.fail("connection_error", str(e))
        return 1

    # Per-run pipeline_name + pipelines_dir so two concurrent migrations (even to the
    # same target schema) never share dlt's HOME-based working state (~/.dlt/pipelines/
    # <name>), which is keyed only on the name. dataset_name stays = schema.
    pipeline = dlt.pipeline(
        pipeline_name=f"wb_migration_{schema}_{run_id}",
        destination=destination,
        dataset_name=schema,
        pipelines_dir=dlt_dir,
        progress=None,
    )

    per_ds, manifest_uris = [], []
    for ds in datasets:
        s_schema = ds.get("source_schema", "")
        s_table = ds["source_table"]
        disp = ds.get("write_disposition", default_disp)
        pk = ds.get("primary_key") or None
        try:
            resource = sql_table(
                credentials=engine,
                schema=s_schema or None,
                table=s_table,
                write_disposition=disp,
            )
            if pk:
                resource.apply_hints(primary_key=pk)
            if ds.get("target_table"):
                resource.apply_hints(table_name=ds["target_table"])
            info = pipeline.run(resource)
            src_rows = _count_source_rows(engine, s_schema, s_table)
            mpath = _write_manifest(run_id, ds, src_platform, src_rows, disp)
            manifest_uris.append(str(mpath))
            per_ds.append({"source": f"{s_schema}.{s_table}" if s_schema else s_table,
                           "target": f"{schema}.{ds.get('target_table', s_table)}",
                           "source_row_count": src_rows,
                           "write_disposition": disp,
                           "load_id": (info.loads_ids[0] if getattr(info, "loads_ids", None) else "")})
            rec.step(f"load:{s_table}", "success",
                     detail=f"{src_rows if src_rows is not None else '?'} source rows → {schema}")
        except Exception as e:  # noqa: BLE001
            rec.step(f"load:{s_table}", "failed", detail=str(e))
            rec.fail("load_error", f"failed loading {s_table}: {e}")
            return 1

    rec.metric("datasets", per_ds)
    rec.metric("dataset_count", len(per_ds))
    rec.metric("manifest_uris", manifest_uris)
    rec.metric("target_schema", schema)
    rec.metric("target_platform", tgt_platform)
    rec.finish("success")
    return 0


def _verify(rec: RunRecorder, spec: dict, run_id: str, dlt_dir: str) -> int:
    """Reconcile source↔target row counts (the runner-side reconciliation)."""
    try:
        import dlt  # noqa: F401 — target client comes via the pipeline
        from sqlalchemy import create_engine, text
    except ImportError as e:
        rec.fail("engine_unavailable", f"dlt / SQLAlchemy not installed: {e}")
        return 1

    src_platform = spec.get("source_platform", "postgres")
    schema = _target_schema(spec)
    try:
        engine = create_engine(_source_sqlalchemy_url(src_platform))
    except Exception as e:  # noqa: BLE001
        rec.fail("connection_error", str(e))
        return 1

    results, all_pass = [], True
    for ds in spec.get("datasets", []):
        s_schema = ds.get("source_schema", "")
        s_table = ds["source_table"]
        t_table = ds.get("target_table", s_table)
        src_rows = _count_source_rows(engine, s_schema, s_table)
        tgt_rows = None
        try:
            import dlt as _dlt
            pipe = _dlt.pipeline(pipeline_name=f"wb_migration_{schema}_{run_id}",
                                 destination=_build_destination(_dlt, spec.get("target_platform", "postgres")),
                                 dataset_name=schema, pipelines_dir=dlt_dir)
            with pipe.sql_client() as client:
                with client.execute_query(f'SELECT count(*) FROM {client.make_qualified_table_name(t_table)}') as cur:
                    tgt_rows = int(cur.fetchone()[0])
        except Exception as e:  # noqa: BLE001
            rec.log(f"verify:{t_table}: could not count target ({e})")
        status = "pass" if (src_rows is not None and src_rows == tgt_rows) else "fail"
        all_pass = all_pass and status == "pass"
        results.append({"table": t_table, "kind": "row_count",
                        "source_row_count": src_rows, "target_row_count": tgt_rows,
                        "result": status})
        rec.step(f"verify:{t_table}", "success" if status == "pass" else "failed",
                 detail=f"source={src_rows} target={tgt_rows}")

    rec.metric("reconciliation", results)
    rec.metric("all_pass", all_pass)
    rec.finish("success")   # verify always completes; per-rule pass/fail is in metrics
    return 0


def _plan(rec: RunRecorder, spec: dict) -> int:
    schema = _target_schema(spec)
    plan = [{"source": f"{d.get('source_schema','')}.{d['source_table']}".lstrip("."),
             "target": f"{schema}.{d.get('target_table', d['source_table'])}",
             "write_disposition": d.get("write_disposition", spec.get("write_disposition", "replace"))}
            for d in spec.get("datasets", [])]
    rec.metric("plan", plan)
    rec.metric("dataset_count", len(plan))
    rec.finish("success")
    return 0


def _dlt_scope() -> tuple[str, str]:
    """Per-run dlt identity: a unique run_id + its own working dir under ``HERE``.

    dlt's default pipeline state dir is HOME-based (``~/.dlt/pipelines/<name>``) and
    keyed only on the pipeline name, so two concurrent migrations sharing a name would
    corrupt each other. A per-run ``pipelines_dir`` (+ run-scoped name) isolates them.
    """
    run_id = os.environ.get("WB_S3_RUN_ID", "").strip() or uuid.uuid4().hex
    return run_id, str(HERE / ".dlt" / run_id)


def main() -> int:
    ap = argparse.ArgumentParser(description="DLT data-migration runner.")
    ap.add_argument("--mode", choices=["load", "verify", "plan"], default="load")
    args = ap.parse_args()

    spec_path = HERE / "migration.json"
    if not spec_path.exists():
        rec = RunRecorder(operation="migration", mode=args.mode, target={})
        rec.fail("missing_spec", "migration.json not found in package.")
        return 1
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    rec = RunRecorder(operation="migration", mode=args.mode,
                      target={"platform": spec.get("target_platform", ""),
                              "schema": _target_schema(spec)})
    if args.mode == "plan":
        return _plan(rec, spec)
    run_id, dlt_dir = _dlt_scope()
    try:
        if args.mode == "load":
            return _load(rec, spec, run_id, dlt_dir)
        return _verify(rec, spec, run_id, dlt_dir)
    finally:
        shutil.rmtree(dlt_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
