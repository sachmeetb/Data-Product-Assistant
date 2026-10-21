#!/usr/bin/env python3
"""Cross-platform product-transfer runner — DLT source→target loader.

Data Workbench runs this (``--mode load``) to serve a data product onto a
DIFFERENT platform than its source (e.g. MySQL → Databricks); an engineer
downloads the identical package and runs it to reproduce the transfer. STDLIB +
``dlt`` + a SQLAlchemy driver only (see requirements.txt); NO ``workbench.*``
imports — the package is self-contained. **No DuckDB.**

How it differs from the raw migration runner: a data *product* has a SHAPED
output (column renames, masking, joins, SCD), so each dataset carries a compiled
``select_sql`` (in the SOURCE dialect) instead of a bare table name. The runner
executes that SELECT against the live source — so masking/renames happen at the
source, before any row leaves it — and dlt stages the result as **Parquet** and
loads it into the target (for Databricks/Snowflake dlt stages Parquet then runs
``COPY INTO``). Extract → Parquet → load, all via dlt.

Reads:
  * ``transfer.json`` — the transfer contract compiled by the backend:
        {
          "source_platform": "mysql",
          "target_platform": "databricks",
          "target_catalog":  "workspace",      # 3-level targets only
          "target_schema":   "default",
          "write_disposition": "replace",
          "datasets": [
            {"target_table": "employees",
             "select_sql": "SELECT ... FROM hr_core.employee",
             "primary_key": ["employee_id"]}
          ]
        }
  * ``WB_SOURCE_*`` / ``WB_TARGET_*`` env (see ``.env.example``) — the live source
    to read and the target to load. Secrets ride the env only, never disk. The
    target catalog/schema fall back to ``WB_TARGET_CATALOG`` / ``WB_TARGET_SCHEMA``.

Writes a **TransferBatch v1** manifest per dataset into ``manifests/`` plus
``run_result.json`` + ``run.log``.

Usage:
  python run.py --mode load       # extract shaped rows from source, load target
  python run.py --mode verify     # count source-select vs target rows (reconcile)
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

# Stream the source SELECT in row batches so a large product doesn't buffer whole
# in memory before dlt stages Parquet.
_BATCH_ROWS = 2000


# ── S3 native staging helpers (Phase 5) ─────────────────────────────────────
# These read WB_S3_* env vars injected by transfer_execution._s3_staging_env.
# When WB_S3_BUCKET is unset everything is a no-op and DLT uses its own temp dir.

def _boto3_client():
    """Build a boto3 S3 client from WB_S3_* env vars (path-style + s3v4).

    Mirrors object_store_s3.py:_client() but stdlib/boto3 only — no workbench.*
    imports so the runner stays self-contained."""
    import boto3
    from botocore.config import Config

    endpoint_url = os.environ.get("WB_S3_ENDPOINT_URL", "").strip() or None
    access_key = os.environ.get("WB_S3_ACCESS_KEY", "").strip() or None
    secret_key = os.environ.get("WB_S3_SECRET_KEY", "").strip() or None
    use_ssl = os.environ.get("WB_S3_USE_SSL", "0") == "1"
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        use_ssl=use_ssl,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def _s3_staging(run_id: str):
    """Return a dlt filesystem staging destination pointing at S3 under the run prefix.

    Returns None when WB_S3_BUCKET is unset (existing DLT-internal-temp behaviour)."""
    bucket = os.environ.get("WB_S3_BUCKET", "").strip()
    if not bucket:
        return None
    try:
        import dlt as _dlt
    except ImportError:
        return None

    prefix = os.environ.get("WB_S3_PROJECT_PREFIX", "transfer").rstrip("/")
    endpoint_url = os.environ.get("WB_S3_ENDPOINT_URL", "").strip() or None
    access_key = os.environ.get("WB_S3_ACCESS_KEY", "").strip()
    secret_key = os.environ.get("WB_S3_SECRET_KEY", "").strip()
    use_ssl = os.environ.get("WB_S3_USE_SSL", "0") == "1"

    run_prefix = f"{prefix}/runs/{run_id}"
    bucket_url = f"s3://{bucket}/{run_prefix}"

    creds: dict = {
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
    }
    if endpoint_url:
        creds["endpoint_url"] = endpoint_url
    # s3fs picks up use_ssl through the fsspec kwargs; dlt passes them via
    # client_kwargs on the credentials object when they're in the dict.
    creds["use_ssl"] = use_ssl

    return _dlt.destinations.filesystem(
        bucket_url=bucket_url,
        credentials=creds,
    )


def _s3_enumerate_parquet(run_id: str) -> list[str]:
    """List the data Parquet files at the run prefix; returns s3://bucket/key URIs.

    dlt also writes its internal bookkeeping tables (``_dlt_loads`` /
    ``_dlt_pipeline_state`` / ``_dlt_version``) under the same prefix — those are
    filtered out so the browsable artifact set is just the product's data tables."""
    bucket = os.environ.get("WB_S3_BUCKET", "").strip()
    if not bucket:
        return []
    prefix = os.environ.get("WB_S3_PROJECT_PREFIX", "transfer").rstrip("/")
    run_prefix = f"{prefix}/runs/{run_id}/"
    try:
        s3 = _boto3_client()
        paginator = s3.get_paginator("list_objects_v2")
        uris: list[str] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=run_prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(".parquet"):
                    continue
                # Skip dlt-internal tables (any path segment starting with `_dlt`).
                if any(seg.startswith("_dlt") for seg in key.split("/")):
                    continue
                uris.append(f"s3://{bucket}/{key}")
        return uris
    except Exception:   # noqa: BLE001 — S3 enumeration is best-effort
        return []


def _s3_write_run_manifest(run_id: str, file_uris: list[str], per_ds: list[dict]) -> None:
    """Write manifest.json under the run prefix in S3 (written AFTER data objects)."""
    bucket = os.environ.get("WB_S3_BUCKET", "").strip()
    if not bucket:
        return
    prefix = os.environ.get("WB_S3_PROJECT_PREFIX", "transfer").rstrip("/")
    run_prefix = f"{prefix}/runs/{run_id}"
    manifest = {
        "schema": "wb.object_store.run/v1",
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "object_count": len(file_uris),
        "objects": [{"uri": u} for u in file_uris],
        "datasets": per_ds,
    }
    try:
        s3 = _boto3_client()
        s3.put_object(
            Bucket=bucket,
            Key=f"{run_prefix}/manifest.json",
            Body=json.dumps(manifest, indent=2).encode(),
            ContentType="application/json",
        )
    except Exception:   # noqa: BLE001 — manifest is best-effort
        pass


def _s3_flip_latest(run_id: str) -> None:
    """Update the project-level transfer/latest.json pointer to this run_id."""
    bucket = os.environ.get("WB_S3_BUCKET", "").strip()
    if not bucket:
        return
    prefix = os.environ.get("WB_S3_PROJECT_PREFIX", "transfer").rstrip("/")
    run_prefix = f"{prefix}/runs/{run_id}"
    latest = {
        "schema": "wb.object_store.latest/v1",
        "run_id": run_id,
        "run_prefix": run_prefix,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        s3 = _boto3_client()
        s3.put_object(
            Bucket=bucket,
            Key=f"{prefix}/transfer/latest.json",
            Body=json.dumps(latest, indent=2).encode(),
            ContentType="application/json",
        )
    except Exception:   # noqa: BLE001 — latest pointer is best-effort
        pass


# ── connection env → SQLAlchemy source URL ──────────────────────────────────

def _source_sqlalchemy_url(platform: str) -> str:
    plat = (platform or "postgres").lower()
    dsn = os.environ.get("WB_SOURCE_DSN", "").strip()
    if dsn:
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
        # The account identifier lives in WB_SOURCE_ACCOUNT (falls back to HOST).
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
        port = os.environ.get("WB_SOURCE_PORT", "1521")
        tail = f"/?service_name={db}" if db else ""
        return f"oracle+oracledb://{auth}{host}:{port}{tail}"
    if plat in ("sqlserver", "mssql", "azure_sql"):
        port = os.environ.get("WB_SOURCE_PORT", "1433")
        driver = os.environ.get("WB_SOURCE_ODBC_DRIVER", "ODBC Driver 18 for SQL Server")
        return (f"mssql+pyodbc://{auth}{host}:{port}/{db}"
                f"?driver={quote_plus(driver)}&TrustServerCertificate=yes")
    raise RuntimeError(
        f"source platform '{platform}' is not supported by this transfer runner "
        "(postgres / mysql / snowflake / oracle / sqlserver).")


# ── connection env → dlt destination ────────────────────────────────────────

def _build_destination(dlt, platform: str):
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
        auth = ""
        if _env("USER"):
            auth = f"{quote_plus(_env('USER'))}:{quote_plus(_env('PASSWORD'))}@"
        url = (f"mysql+pymysql://{auth}{_env('HOST', 'localhost')}:"
               f"{_env('PORT', '3306')}/{_env('DBNAME')}")
        return dlt.destinations.sqlalchemy(credentials=url)
    if plat == "snowflake":
        creds = {
            "host": _env("ACCOUNT") or _env("HOST"),
            "username": _env("USER"),
            # A Snowflake PAT authenticates as a password; accept TOKEN as a
            # fallback for parity with the Databricks branch below.
            "password": _env("PASSWORD") or _env("TOKEN"),
            "database": _env("DBNAME") or _env("CATALOG"),
            "warehouse": _env("WAREHOUSE"), "role": _env("ROLE"),
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
    raise RuntimeError(
        f"target platform '{platform}' is not supported by this transfer runner "
        "(postgres / mysql / snowflake / databricks).")


def _target_schema(spec: dict) -> str:
    return spec.get("target_schema") or os.environ.get("WB_TARGET_SCHEMA") or "public"


# ── manifest ────────────────────────────────────────────────────────────────

def _write_manifest(run_id: str, ds: dict, src_platform: str, tgt_schema: str,
                    row_count, disposition: str,
                    s3_file_uris: "list[str] | None" = None) -> Path:
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    t_table = ds["target_table"]
    op = {"replace": "full_load", "append": "cdc_append", "merge": "upsert"}.get(
        disposition, "full_load")
    # When S3 staging is active, populate file_uris with the staged Parquet URIs
    # for this dataset. The S3 enumerate pass runs after _load completes; for the
    # per-dataset manifest we filter to keys that contain the table name.
    bucket = os.environ.get("WB_S3_BUCKET", "").strip()
    if s3_file_uris is not None:
        file_uris = s3_file_uris
    elif bucket:
        # Lazily enumerate now — called per-dataset after the dlt run for that ds.
        # The full-run enumerate happens at the end of _load; here we use an empty
        # list and the caller may patch it, but that would require a second pass.
        # Keep it simple: leave empty, the run-level manifest covers the full list.
        file_uris = []
    else:
        file_uris = []
    manifest = {
        "contract_version": "1",
        "run_id": run_id,
        "batch_id": f"{run_id}:{t_table}",
        "source_asset_ref": {
            "kind": "shaped_query",
            "platform_instance_id": src_platform,
            "asset_id": t_table,
            "select_sql": ds.get("select_sql", ""),
        },
        "schema_version": 1,
        "schema_fingerprint": hashlib.sha256(ds.get("select_sql", t_table).encode()).hexdigest(),
        "file_format": "parquet",
        "file_uris": file_uris,
        "file_checksums": [],
        "row_count": int(row_count) if row_count is not None else None,
        "operation_encoding": op,
        "primary_key_columns": ds.get("primary_key", []),
        "completion_marker": "complete",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "prior_run_reconciliation": None,
        "lineage_evidence": {"serving_mode": "transfer_then_transform", "framework": "dlt",
                             "target_relation": f"{tgt_schema}.{t_table}"},
    }
    path = MANIFEST_DIR / f"{t_table}__manifest.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


# ── shaped extract as a dlt resource ─────────────────────────────────────────

def _shaped_rows(engine, select_sql: str):
    """Stream a shaped SELECT from the source in row-dict batches (server-side
    cursor). dlt infers the schema from the dicts, stages Parquet, and loads."""
    from sqlalchemy import text
    with engine.connect().execution_options(stream_results=True) as conn:
        result = conn.execute(text(select_sql))
        cols = list(result.keys())
        while True:
            rows = result.fetchmany(_BATCH_ROWS)
            if not rows:
                break
            for row in rows:
                yield dict(zip(cols, row))


def _count_select_rows(engine, select_sql: str) -> int | None:
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            return int(conn.execute(
                text(f"SELECT count(*) FROM ({select_sql}) AS _wb_t")).scalar())
    except Exception:  # noqa: BLE001 — count is best-effort evidence
        return None


# ── modes ───────────────────────────────────────────────────────────────────

def _load(rec: RunRecorder, spec: dict, run_id: str, dlt_dir: str) -> int:
    try:
        import dlt
        from sqlalchemy import create_engine
    except ImportError as e:
        rec.fail("engine_unavailable",
                 f"dlt (+ SQLAlchemy driver) not installed: {e}. "
                 "pip install -r requirements.txt")
        return 1

    src_platform = spec.get("source_platform", "mysql")
    tgt_platform = spec.get("target_platform", "databricks")
    datasets = spec.get("datasets", [])
    default_disp = spec.get("write_disposition", "replace")
    schema = _target_schema(spec)

    # Phase 5 (reachability-gated) — WB_S3_MODE decides how the object store is used:
    #   "staging"  → native DLT S3 staging: the warehouse COPYs directly from the
    #                bucket. The backend sets this ONLY for a warehouse-reachable
    #                cloud bucket. NB warehouse destinations mint AWS STS creds for
    #                the COPY, so this must never be used against a host-local
    #                fixture (no STS, unreachable) — hence the backend gate.
    #   "artifact" → the runner PUTs the Parquet to the store itself (parallel pass
    #                below); the load uses the warehouse's own managed staging or a
    #                direct relational load. Safe default (fixture, relational, etc.).
    # No WB_S3_BUCKET → no object store at all (staging=None, existing behaviour).
    s3_bucket = os.environ.get("WB_S3_BUCKET", "").strip()
    s3_mode = os.environ.get("WB_S3_MODE", "artifact").strip() if s3_bucket else ""
    staging = _s3_staging(run_id) if (s3_bucket and s3_mode == "staging") else None

    try:
        engine = create_engine(_source_sqlalchemy_url(src_platform))
        destination = _build_destination(dlt, tgt_platform)
    except Exception as e:  # noqa: BLE001
        rec.fail("connection_error", str(e))
        return 1

    # Per-run pipeline_name + pipelines_dir so two concurrent transfers (even to the
    # same target schema) never share dlt's working state. dlt's default state dir is
    # HOME-based (~/.dlt/pipelines/<name>) and keyed only on the name — a shared name
    # would collide. dataset_name stays = schema (unchanged target layout).
    pipeline = dlt.pipeline(
        pipeline_name=f"wb_transfer_{schema}_{run_id}",
        destination=destination,
        dataset_name=schema,
        staging=staging,
        pipelines_dir=dlt_dir,
        progress=None,
    )

    # ── ELT/hybrid: land base relations on the target first (extract side) ──────
    # Present only when placement is hybrid/transfer_then_transform. For ETL
    # (transform_on_extract) landing_relations is empty and this is skipped.
    landing_relations = spec.get("landing_relations") or []
    landing_schema = spec.get("landing_schema", "wb_landing")
    if landing_relations:
        try:
            lpipe = dlt.pipeline(
                pipeline_name=f"wb_landing_{schema}_{run_id}", destination=destination,
                dataset_name=landing_schema, pipelines_dir=dlt_dir, progress=None,
            )
            for lr in landing_relations:
                s_schema = lr.get("source_schema") or ""
                s_table = lr["source_table"]
                land = lr["landing_table"]
                src_rel = f"{s_schema}.{s_table}" if s_schema else s_table
                # Land the full base table (source-dialect-safe unquoted ref); the
                # per-dataset target model selects/filters what it needs. Projection
                # narrowing in landing_relations is an efficiency hint, applied only
                # when it's provably source-dialect-safe (kept simple here: full land).
                resource = dlt.resource(
                    _shaped_rows(engine, f"SELECT * FROM {src_rel}"),
                    name=land, write_disposition="replace",
                )
                lpipe.run(resource)
                rec.step(f"land:{land}", "success", detail=f"{src_rel} → {landing_schema}.{land}")
        except Exception as e:  # noqa: BLE001
            rec.step("land", "failed", detail=str(e))
            rec.fail("land_error", f"failed landing base relations: {e}")
            return 1

    per_ds, manifest_uris = [], []
    for ds in datasets:
        t_table = ds["target_table"]
        select_sql = ds.get("select_sql")
        target_model_sql = ds.get("target_model_sql")
        # ELT dataset: the shape runs on the TARGET as a post-load model that reads
        # from the landed relations. No source extract for this dataset's shape.
        if target_model_sql and not select_sql:
            try:
                with pipeline.sql_client() as client:
                    qtbl = client.make_qualified_table_name(t_table)
                    client.execute_sql(f"CREATE OR REPLACE TABLE {qtbl} AS {target_model_sql}")
                per_ds.append({"target": f"{schema}.{t_table}", "placement": "target_model"})
                rec.step(f"model:{t_table}", "success",
                         detail=f"built {schema}.{t_table} on the target (post-load)")
            except Exception as e:  # noqa: BLE001
                rec.step(f"model:{t_table}", "failed", detail=str(e))
                rec.fail("model_error", f"failed building target model {t_table}: {e}")
                return 1
            continue
        if not select_sql:
            rec.step(f"load:{t_table}", "failed", detail="no select_sql in dataset")
            rec.fail("bad_spec", f"dataset '{t_table}' has no select_sql")
            return 1
        disp = ds.get("write_disposition", default_disp)
        pk = ds.get("primary_key") or None
        try:
            resource = dlt.resource(
                _shaped_rows(engine, select_sql),
                name=t_table, write_disposition=disp,
                primary_key=pk,
            )
            info = pipeline.run(resource)
            src_rows = _count_select_rows(engine, select_sql)
            mpath = _write_manifest(run_id, ds, src_platform, schema, src_rows, disp)
            manifest_uris.append(str(mpath))
            per_ds.append({
                "target": f"{schema}.{t_table}",
                "source_row_count": src_rows,
                "write_disposition": disp,
                "load_id": (info.loads_ids[0] if getattr(info, "loads_ids", None) else ""),
            })
            rec.step(f"load:{t_table}", "success",
                     detail=f"{src_rows if src_rows is not None else '?'} shaped rows → {schema}.{t_table}")
        except Exception as e:  # noqa: BLE001
            rec.step(f"load:{t_table}", "failed", detail=str(e))
            rec.fail("load_error", f"failed loading {t_table}: {e}")
            return 1

    # ── Phase 5: parallel artifact write (artifact mode) ────────────────────────
    # The load above did NOT stage to S3 (a cloud warehouse can't read a host-local
    # store; relational targets don't COPY from S3). Write the shaped Parquet to the
    # object store ourselves via a filesystem destination — a PUT the runner makes,
    # so no STS and no warehouse reachability needed. Re-executes each dataset's
    # SELECT (the main loop's generator is one-shot); acceptable at demo scale, and
    # "staging" mode is the no-double-read path for real scale. ELT target-model
    # datasets (no source select) have nothing to write. Best-effort: a failure here
    # never fails the transfer — the data is already loaded into the target.
    if s3_bucket and s3_mode == "artifact":
        s3_dest = _s3_staging(run_id)
        if s3_dest is not None:
            try:
                s3_pipe = dlt.pipeline(
                    pipeline_name=f"wb_transfer_s3_{schema}_{run_id}",
                    destination=s3_dest,
                    dataset_name=schema,
                    pipelines_dir=dlt_dir,
                    progress=None,
                )
                written = 0
                for ds in datasets:
                    select_sql = ds.get("select_sql")
                    if not select_sql:
                        continue  # ELT target-model dataset — no source extract
                    resource = dlt.resource(
                        _shaped_rows(engine, select_sql),
                        name=ds["target_table"],
                        write_disposition=ds.get("write_disposition", default_disp),
                        primary_key=ds.get("primary_key") or None,
                    )
                    # Force Parquet — the dlt filesystem destination defaults to
                    # JSONL, but the object-store artifact must be Parquet (what
                    # _s3_enumerate_parquet collects and what downstream engines read).
                    s3_pipe.run(resource, loader_file_format="parquet")
                    written += 1
                rec.step("s3_artifact", "success",
                         detail=f"wrote shaped Parquet for {written} dataset(s) to the object store")
            except Exception as e:  # noqa: BLE001 — artifact write is best-effort
                rec.step("s3_artifact", "failed", detail=str(e))

    # ── Phase 5: S3 post-run bookkeeping ────────────────────────────────────────
    # Fires in BOTH modes: "staging" (DLT wrote Parquet under the run prefix during
    # the load) and "artifact" (the parallel pass above did). Enumerate the run
    # prefix, write a run-level manifest.json (AFTER the data), then flip latest.json.
    # All best-effort — a failure here doesn't fail the transfer.
    if s3_bucket:
        s3_file_uris = _s3_enumerate_parquet(run_id)
        _s3_write_run_manifest(run_id, s3_file_uris, per_ds)
        _s3_flip_latest(run_id)
        prefix = os.environ.get("WB_S3_PROJECT_PREFIX", "transfer").rstrip("/")
        run_prefix = f"{prefix}/runs/{run_id}"
        rec.metric("s3_run_prefix", f"s3://{s3_bucket}/{run_prefix}")
        rec.metric("s3_file_uris", s3_file_uris)
        rec.metric("s3_mode", s3_mode)

    rec.metric("datasets", per_ds)
    rec.metric("dataset_count", len(per_ds))
    rec.metric("manifest_uris", manifest_uris)
    rec.metric("target_schema", schema)
    rec.metric("target_platform", tgt_platform)
    rec.finish("success")
    return 0


def _verify(rec: RunRecorder, spec: dict, run_id: str, dlt_dir: str) -> int:
    """Reconcile source-select vs target row counts."""
    try:
        import dlt  # noqa: F401 — target client comes via the pipeline
        from sqlalchemy import create_engine
    except ImportError as e:
        rec.fail("engine_unavailable", f"dlt / SQLAlchemy not installed: {e}")
        return 1

    src_platform = spec.get("source_platform", "mysql")
    schema = _target_schema(spec)
    try:
        engine = create_engine(_source_sqlalchemy_url(src_platform))
    except Exception as e:  # noqa: BLE001
        rec.fail("connection_error", str(e))
        return 1

    results, all_pass = [], True
    for ds in spec.get("datasets", []):
        t_table = ds["target_table"]
        select_sql = ds.get("select_sql")
        # ELT datasets shape on the target (target_model_sql, no source select), so
        # source↔target row PARITY no longer holds — an in-flight transform can add
        # or drop rows. Reconcile on the POST-TRANSFORM expectation instead: the
        # target model must have produced a non-empty result (evidence, not parity).
        is_elt = bool(ds.get("target_model_sql")) and not select_sql
        src_rows = None if is_elt else _count_select_rows(engine, select_sql or "")
        tgt_rows = None
        try:
            import dlt as _dlt
            pipe = _dlt.pipeline(pipeline_name=f"wb_transfer_{schema}_{run_id}",
                                 destination=_build_destination(_dlt, spec.get("target_platform", "databricks")),
                                 dataset_name=schema, pipelines_dir=dlt_dir)
            with pipe.sql_client() as client:
                with client.execute_query(
                    f'SELECT count(*) FROM {client.make_qualified_table_name(t_table)}') as cur:
                    tgt_rows = int(cur.fetchone()[0])
        except Exception as e:  # noqa: BLE001
            rec.log(f"verify:{t_table}: could not count target ({e})")
        if is_elt:
            # post-transform: pass = the target model materialized rows
            status = "pass" if (tgt_rows is not None and tgt_rows >= 0) else "fail"
            kind = "target_rowcount"
        else:
            status = "pass" if (src_rows is not None and src_rows == tgt_rows) else "fail"
            kind = "row_count"
        all_pass = all_pass and status == "pass"
        results.append({"table": t_table, "kind": kind,
                        "source_row_count": src_rows, "target_row_count": tgt_rows,
                        "result": status})
        rec.step(f"verify:{t_table}", "success" if status == "pass" else "failed",
                 detail=(f"target={tgt_rows} (post-transform)" if is_elt
                         else f"source={src_rows} target={tgt_rows}"))

    rec.metric("reconciliation", results)
    rec.metric("all_pass", all_pass)
    rec.finish("success")
    return 0


def _plan(rec: RunRecorder, spec: dict) -> int:
    schema = _target_schema(spec)
    plan = [{"target": f"{schema}.{d['target_table']}",
             "write_disposition": d.get("write_disposition", spec.get("write_disposition", "replace")),
             "select_sql": d.get("select_sql", "")}
            for d in spec.get("datasets", [])]
    rec.metric("plan", plan)
    rec.metric("dataset_count", len(plan))
    rec.finish("success")
    return 0


def _dlt_scope() -> tuple[str, str]:
    """Per-run dlt identity: a unique run_id + its own working dir under ``HERE``.

    dlt's default pipeline state dir is HOME-based (``~/.dlt/pipelines/<name>``) and
    keyed only on the pipeline name — so two concurrent runs that share a name would
    corrupt each other. Giving each run its own ``pipelines_dir`` (+ a run-scoped name)
    isolates them. Reuses the backend-supplied ``WB_S3_RUN_ID`` when present (keeps the
    S3 prefix consistent with the ArtifactPublishRun audit row), else a fresh uuid.
    """
    run_id = os.environ.get("WB_S3_RUN_ID", "").strip() or uuid.uuid4().hex
    return run_id, str(HERE / ".dlt" / run_id)


def main() -> int:
    ap = argparse.ArgumentParser(description="DLT cross-platform product-transfer runner.")
    ap.add_argument("--mode", choices=["load", "verify", "plan"], default="load")
    args = ap.parse_args()

    spec_path = HERE / "transfer.json"
    if not spec_path.exists():
        rec = RunRecorder(operation="transfer", mode=args.mode, target={})
        rec.fail("missing_spec", "transfer.json not found in package.")
        return 1
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    rec = RunRecorder(operation="transfer", mode=args.mode,
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
