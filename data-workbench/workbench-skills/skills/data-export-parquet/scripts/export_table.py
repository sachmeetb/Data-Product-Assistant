#!/usr/bin/env python3
"""export_table.py — Export one relational table as Parquet + TransferBatch v1 manifest.

Usage:
    python export_table.py <connection_string> <schema> <table> <output_dir>
        [--run-id RUN_ID]
        [--batch-id BATCH_ID]
        [--limit N]
        [--compression CODEC]
        [--primary-key COL ...]

Output:
    <output_dir>/<schema>__<table>.parquet
    <output_dir>/<schema>__<table>__manifest.json

The manifest is a TransferBatch v1 JSON document parseable by
workbench.backend.platform.transfer_batch.TransferBatch.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── driver import helpers ──────────────────────────────────────────────────────

def _import_pyarrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
        return pa, pq
    except ImportError:
        sys.exit(
            "pyarrow is required for Parquet export.  "
            "Install it with: pip install 'pyarrow>=19.0,<20.0'"
        )


def _import_psycopg2():
    try:
        import psycopg2
        import psycopg2.extras
        return psycopg2
    except ImportError:
        sys.exit(
            "psycopg2 is required for PostgreSQL export.  "
            "Install it with: pip install psycopg2-binary"
        )


def _import_pymysql():
    try:
        import pymysql
        import pymysql.cursors
        return pymysql
    except ImportError:
        sys.exit(
            "pymysql is required for MySQL export.  "
            "Install it with: pip install pymysql"
        )


# ── DSN parsing ────────────────────────────────────────────────────────────────

def _detect_platform(dsn: str) -> str:
    lower = dsn.lower()
    if lower.startswith("postgresql://") or lower.startswith("postgres://"):
        return "postgres"
    if lower.startswith("mysql://"):
        return "mysql"
    raise ValueError(
        f"Unsupported connection scheme in DSN: {dsn!r}. "
        "Expected 'postgresql://', 'postgres://', or 'mysql://'."
    )


# ── connection factories ───────────────────────────────────────────────────────

def _connect_postgres(dsn: str):
    pg = _import_psycopg2()
    return pg.connect(dsn)


def _connect_mysql(dsn: str):
    """Parse a mysql://user:pass@host:port/db DSN and connect."""
    pymysql = _import_pymysql()
    # Strip scheme
    rest = dsn[len("mysql://"):]
    userinfo, _, hostpath = rest.rpartition("@")
    user, _, password = userinfo.partition(":")
    hostport, _, database = hostpath.partition("/")
    host, _, port_str = hostport.partition(":")
    port = int(port_str) if port_str else 3306
    database = database.split("?")[0]  # drop query params
    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        cursorclass=pymysql.cursors.DictCursor,
    )


# ── query and convert ──────────────────────────────────────────────────────────

def _fetch_as_arrow(conn, platform: str, schema: str, table: str, limit: int):
    """Run SELECT against the table and return (pa.Table, column_meta).

    column_meta is a list of {name, type_str} dicts.
    """
    pa, _ = _import_pyarrow()

    if platform == "postgres":
        fqt = f'"{schema}"."{table}"'
        sql = f"SELECT * FROM {fqt}"
        if limit > 0:
            sql += f" LIMIT {limit}"
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            col_names = [d[0] for d in cur.description]
            col_types = [str(d[1]) for d in cur.description]
        data = {name: [r[i] for r in rows] for i, name in enumerate(col_names)}
    else:
        # MySQL — DictCursor rows
        with conn.cursor() as cur:
            bt = lambda n: f"`{n}`"  # backtick quoting
            fqt = f"{bt(schema)}.{bt(table)}"
            sql = f"SELECT * FROM {fqt}"
            if limit > 0:
                sql += f" LIMIT {limit}"
            cur.execute(sql)
            rows = cur.fetchall()
            if not rows:
                col_names = [d[0] for d in cur.description] if cur.description else []
                col_types = ["unknown"] * len(col_names)
                data = {name: [] for name in col_names}
            else:
                col_names = list(rows[0].keys())
                col_types = ["unknown"] * len(col_names)
                data = {name: [r[name] for r in rows] for name in col_names}

    arrow_table = pa.table(data)
    column_meta = [
        {"name": name, "type_str": col_types[i] if i < len(col_types) else "unknown"}
        for i, name in enumerate(col_names)
    ]
    return arrow_table, column_meta


# ── schema fingerprint ─────────────────────────────────────────────────────────

def _schema_fingerprint(arrow_table) -> tuple[str, dict[str, Any]]:
    """Return (hex_fingerprint, canonical_schema_dict)."""
    canonical: dict[str, Any] = {}
    for field in arrow_table.schema:
        canonical[field.name] = {
            "type": str(field.type),
            "nullable": field.nullable,
        }
    raw = json.dumps(canonical, sort_keys=True)
    fingerprint = hashlib.sha256(raw.encode()).hexdigest()
    return fingerprint, canonical


# ── column statistics ──────────────────────────────────────────────────────────

def _column_statistics(arrow_table) -> list[dict[str, Any]]:
    stats = []
    for i, name in enumerate(arrow_table.schema.names):
        col = arrow_table.column(i)
        null_count = col.null_count
        value_count = len(col)
        stats.append({
            "column_name": name,
            "null_count": null_count,
            "value_count": value_count,
            "distinct_count": None,  # would require a pass over data; skipped for v1
            "lower_bound": None,
            "upper_bound": None,
        })
    return stats


# ── file checksum ──────────────────────────────────────────────────────────────

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── reconciliation ─────────────────────────────────────────────────────────────

def _load_prior_manifest(manifest_path: Path) -> dict[str, Any] | None:
    """Load a prior manifest if it exists and is valid JSON; return None otherwise."""
    if not manifest_path.exists():
        return None
    try:
        return json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _reconcile(
    fingerprint: str,
    row_count: int,
    checksum: str,
    batch_id: str,
    prior: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Compare current export metrics against the prior manifest.

    Returns a ``prior_run_reconciliation`` dict (matches ReconciliationEvidence
    in transfer_batch.py) or None when there is no prior run to compare.
    """
    if prior is None:
        return {
            "status": "first_run",
            "prior_batch_id": "",
            "prior_schema_fingerprint": "",
            "prior_row_count": None,
            "prior_file_checksum": "",
            "details": {},
        }

    prior_fp = prior.get("schema_fingerprint", "")
    prior_rc = prior.get("row_count")
    prior_cs = (prior.get("file_checksums") or [""])[0]
    prior_bid = prior.get("batch_id", "")

    if prior_fp and prior_fp != fingerprint:
        status = "schema_changed"
        details = {"prior_fingerprint": prior_fp, "new_fingerprint": fingerprint}
    elif prior_rc is not None and prior_rc != row_count:
        status = "row_count_changed"
        details = {"prior_row_count": prior_rc, "new_row_count": row_count}
    elif prior_cs and prior_cs != checksum:
        status = "data_changed"
        details = {"prior_checksum": prior_cs[:16], "new_checksum": checksum[:16]}
    else:
        status = "idempotent"
        details = {}

    return {
        "status": status,
        "prior_batch_id": prior_bid,
        "prior_schema_fingerprint": prior_fp,
        "prior_row_count": prior_rc,
        "prior_file_checksum": prior_cs,
        "details": details,
    }


# ── manifest helpers ───────────────────────────────────────────────────────────

def _build_manifest(
    *,
    run_id: str,
    batch_id: str,
    platform: str,
    schema: str,
    table: str,
    parquet_path: Path,
    fingerprint: str,
    canonical_schema: dict,
    column_stats: list[dict],
    row_count: int,
    compression: str,
    primary_key_columns: list[str],
    completion_marker: str,
    prior_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checksum = _sha256_file(parquet_path)
    byte_count = parquet_path.stat().st_size
    file_uri = parquet_path.resolve().as_uri()

    reconciliation = _reconcile(
        fingerprint, row_count, checksum, batch_id, prior_manifest
    )

    return {
        "contract_version": "1",
        "run_id": run_id,
        "batch_id": batch_id,
        "source_asset_ref": {
            "kind": "relational_relation",
            "platform_instance_id": platform,
            "asset_id": f"{schema}.{table}",
            "display_name": f"{schema}.{table}",
            "schema": schema,
            "relation": table,
            "relation_kind": "table",
        },
        "source_snapshot_boundary": {
            "kind": "full_table",
            "value": datetime.now(timezone.utc).isoformat(),
        },
        "incremental_state_before": None,
        "incremental_state_after": None,
        "schema_version": 1,
        "schema_fingerprint": fingerprint,
        "canonical_schema": canonical_schema,
        "file_format": "parquet",
        "compression": compression,
        "file_uris": [file_uri],
        "file_checksums": [checksum],
        "row_count": row_count,
        "byte_count": byte_count,
        "column_statistics": column_stats,
        "encryption_context": {},
        "operation_encoding": "full_load",
        "primary_key_columns": primary_key_columns,
        "sequence_column": "",
        "rejected_record_summary": None,
        "prior_run_reconciliation": reconciliation,
        "completion_marker": completion_marker,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "retention_until": None,
        "lineage_evidence": {
            "source_schema": schema,
            "source_table": table,
            "platform": platform,
        },
    }


# ── main export ────────────────────────────────────────────────────────────────

def export_table(
    connection_string: str,
    schema: str,
    table: str,
    output_dir: str,
    *,
    run_id: str | None = None,
    batch_id: str = "batch-001",
    limit: int = 0,
    compression: str = "zstd",
    primary_key_columns: list[str] | None = None,
) -> dict[str, Any]:
    """Export one table to Parquet and write a TransferBatch v1 manifest.

    Returns the manifest dict.  Also writes two files to output_dir:
      - <schema>__<table>.parquet
      - <schema>__<table>__manifest.json
    """
    pa, pq = _import_pyarrow()
    platform = _detect_platform(connection_string)
    run_id = run_id or str(uuid.uuid4())
    primary_key_columns = primary_key_columns or []

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{schema}__{table}"
    parquet_path = out / f"{stem}.parquet"
    # Write to a temp file first, then atomic-rename.  This prevents a
    # partial file from being observed if the process is interrupted mid-write.
    parquet_tmp = out / f"{stem}.parquet.tmp"
    manifest_path = out / f"{stem}__manifest.json"

    # Load any prior manifest before overwriting — used for reconciliation.
    prior_manifest = _load_prior_manifest(manifest_path)

    def _failure_manifest() -> dict[str, Any]:
        return {
            "contract_version": "1",
            "run_id": run_id,
            "batch_id": batch_id,
            "source_asset_ref": {
                "kind": "relational_relation",
                "platform_instance_id": platform,
                "asset_id": f"{schema}.{table}",
                "relation": table,
            },
            "completion_marker": "failed",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    conn = None
    manifest: dict[str, Any] = {}
    try:
        if platform == "postgres":
            conn = _connect_postgres(connection_string)
        else:
            conn = _connect_mysql(connection_string)

        arrow_table, _ = _fetch_as_arrow(conn, platform, schema, table, limit)
        fingerprint, canonical_schema = _schema_fingerprint(arrow_table)
        col_stats = _column_statistics(arrow_table)
        row_count = len(arrow_table)

        # Write to temp, then atomic rename — interrupted writes stay as .tmp
        pq.write_table(
            arrow_table,
            str(parquet_tmp),
            compression=compression if compression != "none" else None,
        )
        parquet_tmp.rename(parquet_path)  # atomic on POSIX; best-effort on Windows

        manifest = _build_manifest(
            run_id=run_id,
            batch_id=batch_id,
            platform=platform,
            schema=schema,
            table=table,
            parquet_path=parquet_path,
            fingerprint=fingerprint,
            canonical_schema=canonical_schema,
            column_stats=col_stats,
            row_count=row_count,
            compression=compression,
            primary_key_columns=primary_key_columns,
            completion_marker="complete",
            prior_manifest=prior_manifest,
        )
    except Exception:
        # Clean up partial temp file so it's never mistaken for a complete export
        if parquet_tmp.exists():
            parquet_tmp.unlink(missing_ok=True)
        manifest = _failure_manifest()
        manifest_path.write_text(json.dumps(manifest, indent=2))
        raise
    finally:
        if conn is not None:
            conn.close()

    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest


# ── CLI ────────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Export a relational table as Parquet + TransferBatch v1 manifest."
    )
    p.add_argument("connection_string", help="DSN: postgresql://... or mysql://...")
    p.add_argument("schema", help="Source schema name")
    p.add_argument("table", help="Source table name")
    p.add_argument("output_dir", help="Directory for output files")
    p.add_argument("--run-id", default=None)
    p.add_argument("--batch-id", default="batch-001")
    p.add_argument(
        "--limit", type=int, default=0,
        help="Max rows to export (0 = full table)",
    )
    p.add_argument(
        "--compression", default="zstd",
        choices=["zstd", "snappy", "none"],
    )
    p.add_argument(
        "--primary-key", dest="primary_key", action="append", default=[],
        metavar="COL",
        help="Primary key column (repeatable)",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    print(
        f"Exporting {args.schema}.{args.table} from {args.connection_string[:30]}…"
        f"  limit={args.limit or 'full'}"
    )
    manifest = export_table(
        args.connection_string,
        args.schema,
        args.table,
        args.output_dir,
        run_id=args.run_id,
        batch_id=args.batch_id,
        limit=args.limit,
        compression=args.compression,
        primary_key_columns=args.primary_key,
    )
    print(f"  rows       : {manifest.get('row_count')}")
    print(f"  bytes      : {manifest.get('byte_count')}")
    print(f"  fingerprint: {manifest.get('schema_fingerprint', '')[:16]}…")
    print(f"  checksum   : {manifest.get('file_checksums', [''])[0][:16]}…")
    print(f"  status     : {manifest.get('completion_marker')}")
    print(f"  manifest   → {args.output_dir}/{args.schema}__{args.table}__manifest.json")


if __name__ == "__main__":
    main()
