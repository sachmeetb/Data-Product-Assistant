"""Tests for the data-export-parquet skill (export_table.py).

All tests run without a live database:
- Postgres paths mock psycopg2.connect
- MySQL paths mock pymysql.connect
- pyarrow is a real import (installed in the venv)

Coverage:
- Platform detection from DSN
- Full export flow (mock connection → Parquet file + manifest JSON)
- TransferBatch manifest parses cleanly into the Pydantic model
- Schema fingerprint stability
- Failure manifest written on connection error
- CLI argument parser
"""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest

# ── locate the skill script ────────────────────────────────────────────────────
_SKILL_DIR = Path(__file__).parent.parent.parent.parent / "workbench-skills" / "skills" / "data-export-parquet" / "scripts"
_SCRIPT = _SKILL_DIR / "export_table.py"

assert _SCRIPT.exists(), f"export_table.py not found at {_SCRIPT}"

spec = importlib.util.spec_from_file_location("export_table", _SCRIPT)
export_table_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export_table_mod)

export_table = export_table_mod.export_table
_detect_platform = export_table_mod._detect_platform
_schema_fingerprint = export_table_mod._schema_fingerprint
_column_statistics = export_table_mod._column_statistics


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_mock_pg_conn(rows: list[tuple], col_names: list[str], col_type_ids: list[int] | None = None):
    """Build a mock psycopg2 connection that returns `rows` from fetchall."""
    col_type_ids = col_type_ids or [23] * len(col_names)  # 23 = int4 oid
    description = [(name, oid, None, None, None, None, None) for name, oid in zip(col_names, col_type_ids)]
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = rows
    mock_cursor.description = description
    mock_cursor.__enter__ = lambda s: s
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    return mock_conn, mock_cursor


def _make_mock_mysql_conn(rows: list[dict]):
    """Build a mock pymysql DictCursor connection that returns `rows`."""
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = rows
    if rows:
        mock_cursor.description = [(k, None, None, None, None, None, None) for k in rows[0].keys()]
    else:
        mock_cursor.description = []
    mock_cursor.__enter__ = lambda s: s
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    return mock_conn


# ── platform detection ─────────────────────────────────────────────────────────

class TestDetectPlatform:
    def test_postgres_long_scheme(self):
        assert _detect_platform("postgresql://user:pass@host/db") == "postgres"

    def test_postgres_short_scheme(self):
        assert _detect_platform("postgres://user:pass@host/db") == "postgres"

    def test_mysql(self):
        assert _detect_platform("mysql://user:pass@host:3306/db") == "mysql"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unsupported connection scheme"):
            _detect_platform("redshift://user:pass@host/db")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            _detect_platform("")


# ── schema fingerprint ─────────────────────────────────────────────────────────

class TestSchemaFingerprint:
    def test_fingerprint_is_sha256_hex(self):
        import pyarrow as pa
        table = pa.table({"id": [1, 2], "name": ["a", "b"]})
        fp, canonical = _schema_fingerprint(table)
        assert len(fp) == 64
        assert all(c in "0123456789abcdef" for c in fp)

    def test_fingerprint_is_stable(self):
        import pyarrow as pa
        table = pa.table({"id": [1], "val": [3.14]})
        fp1, _ = _schema_fingerprint(table)
        fp2, _ = _schema_fingerprint(table)
        assert fp1 == fp2

    def test_different_schema_different_fingerprint(self):
        import pyarrow as pa
        t1 = pa.table({"id": [1]})
        t2 = pa.table({"id": [1], "extra": [2]})
        fp1, _ = _schema_fingerprint(t1)
        fp2, _ = _schema_fingerprint(t2)
        assert fp1 != fp2

    def test_canonical_schema_has_type_and_nullable(self):
        import pyarrow as pa
        table = pa.table({"amount": pa.array([1.5], type=pa.float64())})
        _, canonical = _schema_fingerprint(table)
        assert "amount" in canonical
        assert "type" in canonical["amount"]
        assert "nullable" in canonical["amount"]


# ── column statistics ──────────────────────────────────────────────────────────

class TestColumnStatistics:
    def test_null_count_propagated(self):
        import pyarrow as pa
        arr = pa.array([1, None, 3])
        table = pa.table({"x": arr})
        stats = _column_statistics(table)
        assert stats[0]["column_name"] == "x"
        assert stats[0]["null_count"] == 1
        assert stats[0]["value_count"] == 3

    def test_no_nulls(self):
        import pyarrow as pa
        table = pa.table({"a": [1, 2, 3], "b": ["x", "y", "z"]})
        stats = _column_statistics(table)
        assert all(s["null_count"] == 0 for s in stats)
        assert {s["column_name"] for s in stats} == {"a", "b"}


# ── full export flow (Postgres, mocked) ───────────────────────────────────────

class TestExportTablePostgres:
    def test_writes_parquet_and_manifest(self, tmp_path):
        rows = [(1, "Alice", 100.0), (2, "Bob", 200.0)]
        col_names = ["id", "name", "amount"]
        mock_conn, _ = _make_mock_pg_conn(rows, col_names)

        with patch.object(export_table_mod, "_connect_postgres", return_value=mock_conn):
            manifest = export_table(
                "postgresql://user:pass@localhost/db",
                "public",
                "orders",
                str(tmp_path),
                run_id="test-run",
                batch_id="b1",
            )

        parquet_file = tmp_path / "public__orders.parquet"
        manifest_file = tmp_path / "public__orders__manifest.json"
        assert parquet_file.exists(), "Parquet file not written"
        assert manifest_file.exists(), "Manifest file not written"

    def test_manifest_fields(self, tmp_path):
        rows = [(1, "Alice")]
        col_names = ["id", "name"]
        mock_conn, _ = _make_mock_pg_conn(rows, col_names)

        with patch.object(export_table_mod, "_connect_postgres", return_value=mock_conn):
            manifest = export_table(
                "postgresql://user:pass@localhost/db",
                "myschema",
                "customers",
                str(tmp_path),
                run_id="run-001",
                batch_id="batch-1",
                primary_key_columns=["id"],
            )

        assert manifest["contract_version"] == "1"
        assert manifest["run_id"] == "run-001"
        assert manifest["batch_id"] == "batch-1"
        assert manifest["source_asset_ref"]["kind"] == "relational_relation"
        assert manifest["source_asset_ref"]["relation"] == "customers"
        assert manifest["completion_marker"] == "complete"
        assert manifest["row_count"] == 1
        assert manifest["byte_count"] > 0
        assert len(manifest["file_uris"]) == 1
        assert manifest["file_uris"][0].startswith("file://")
        assert len(manifest["file_checksums"]) == 1
        assert manifest["schema_fingerprint"] != ""
        assert "id" in manifest["canonical_schema"]
        assert manifest["primary_key_columns"] == ["id"]
        assert len(manifest["column_statistics"]) == 2

    def test_manifest_parseable_by_transfer_batch(self, tmp_path):
        rows = [(10, "Zebra")]
        col_names = ["id", "label"]
        mock_conn, _ = _make_mock_pg_conn(rows, col_names)

        with patch.object(export_table_mod, "_connect_postgres", return_value=mock_conn):
            manifest = export_table(
                "postgresql://user:pass@localhost/db",
                "public",
                "animals",
                str(tmp_path),
            )

        # The manifest must round-trip through the Pydantic model
        from workbench.backend.platform.transfer_batch import TransferBatch, RelationalRelationRef
        batch = TransferBatch.model_validate(manifest)
        assert batch.completion_marker.value == "complete"
        assert isinstance(batch.source_asset_ref, RelationalRelationRef)
        assert batch.source_asset_ref.relation == "animals"
        assert batch.row_count == 1

    def test_parquet_readable_by_duckdb(self, tmp_path):
        rows = [(1, 10.5), (2, 20.0), (3, 30.0)]
        col_names = ["id", "amount"]
        mock_conn, _ = _make_mock_pg_conn(rows, col_names)

        with patch.object(export_table_mod, "_connect_postgres", return_value=mock_conn):
            export_table(
                "postgresql://user:pass@localhost/db",
                "public",
                "sales",
                str(tmp_path),
            )

        import duckdb
        parquet_path = str(tmp_path / "public__sales.parquet")
        result = duckdb.query(
            f"SELECT COUNT(*) AS n FROM read_parquet('{parquet_path}')"
        ).fetchone()
        assert result[0] == 3

    def test_limit_respected(self, tmp_path):
        rows = [(1, "a"), (2, "b"), (3, "c")]
        col_names = ["id", "val"]
        mock_conn, mock_cursor = _make_mock_pg_conn(rows[:1], col_names)

        with patch.object(export_table_mod, "_connect_postgres", return_value=mock_conn):
            manifest = export_table(
                "postgresql://user:pass@localhost/db",
                "public",
                "items",
                str(tmp_path),
                limit=1,
            )

        # Verify the LIMIT clause was included in the SQL
        sql_called = mock_cursor.execute.call_args[0][0]
        assert "LIMIT 1" in sql_called
        assert manifest["row_count"] == 1

    def test_run_id_auto_generated(self, tmp_path):
        mock_conn, _ = _make_mock_pg_conn([(1,)], ["id"])
        with patch.object(export_table_mod, "_connect_postgres", return_value=mock_conn):
            m1 = export_table("postgresql://x/db", "s", "t", str(tmp_path / "a"))
            m2 = export_table("postgresql://x/db", "s", "t", str(tmp_path / "b"))
        # Each call auto-generates a distinct run_id
        assert m1["run_id"] != m2["run_id"]


# ── MySQL export (mocked) ──────────────────────────────────────────────────────

class TestExportTableMySQL:
    def test_mysql_export_writes_files(self, tmp_path):
        rows = [{"id": 1, "product": "widget"}, {"id": 2, "product": "gadget"}]
        mock_conn = _make_mock_mysql_conn(rows)

        with patch.object(export_table_mod, "_connect_mysql", return_value=mock_conn):
            manifest = export_table(
                "mysql://user:pass@localhost:3306/shop",
                "inventory",
                "products",
                str(tmp_path),
            )

        assert manifest["completion_marker"] == "complete"
        assert manifest["row_count"] == 2
        assert manifest["source_asset_ref"]["platform_instance_id"] == "mysql"
        parquet_file = tmp_path / "inventory__products.parquet"
        assert parquet_file.exists()

    def test_mysql_manifest_roundtrips_transfer_batch(self, tmp_path):
        rows = [{"order_id": 100, "total": 49.99}]
        mock_conn = _make_mock_mysql_conn(rows)

        with patch.object(export_table_mod, "_connect_mysql", return_value=mock_conn):
            manifest = export_table(
                "mysql://user:pass@host/shop",
                "orders",
                "line_items",
                str(tmp_path),
            )

        from workbench.backend.platform.transfer_batch import TransferBatch
        batch = TransferBatch.model_validate(manifest)
        assert batch.completion_marker.value == "complete"


# ── failure path ───────────────────────────────────────────────────────────────

class TestExportFailurePath:
    def test_failed_manifest_written_on_connect_error(self, tmp_path):
        def _boom(dsn):
            raise RuntimeError("Connection refused")

        with patch.object(export_table_mod, "_connect_postgres", side_effect=_boom):
            with pytest.raises(RuntimeError):
                export_table(
                    "postgresql://x/db",
                    "public",
                    "boom_table",
                    str(tmp_path),
                    run_id="fail-run",
                )

        manifest_file = tmp_path / "public__boom_table__manifest.json"
        assert manifest_file.exists(), "Failure manifest not written"
        manifest = json.loads(manifest_file.read_text())
        assert manifest["completion_marker"] == "failed"
        assert manifest["run_id"] == "fail-run"

    def test_tmp_file_cleaned_up_on_failure(self, tmp_path):
        """Interrupted writes must not leave a .parquet.tmp partial file."""
        def _boom(dsn):
            raise RuntimeError("Connection refused")

        with patch.object(export_table_mod, "_connect_postgres", side_effect=_boom):
            with pytest.raises(RuntimeError):
                export_table(
                    "postgresql://x/db",
                    "public",
                    "partial_table",
                    str(tmp_path),
                )

        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == [], f"Partial .tmp file(s) left: {tmp_files}"

    def test_successful_export_leaves_no_tmp_file(self, tmp_path):
        rows = [(1, "a"), (2, "b")]
        col_names = ["id", "val"]
        mock_conn, _ = _make_mock_pg_conn(rows, col_names)
        with patch.object(export_table_mod, "_connect_postgres", return_value=mock_conn):
            export_table("postgresql://x/db", "public", "t", str(tmp_path))
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == [], f"Stale .tmp file(s) after successful export: {tmp_files}"


# ── reconciliation ─────────────────────────────────────────────────────────────

_reconcile = export_table_mod._reconcile
_load_prior_manifest = export_table_mod._load_prior_manifest


class TestReconcileFunction:
    """Unit tests for the _reconcile() helper — no file I/O needed."""

    def test_first_run_when_prior_is_none(self):
        result = _reconcile("fp1", 10, "cs1", "b1", None)
        assert result["status"] == "first_run"
        assert result["prior_batch_id"] == ""
        assert result["prior_row_count"] is None

    def test_idempotent_when_all_match(self):
        prior = {
            "schema_fingerprint": "fp1",
            "row_count": 10,
            "file_checksums": ["cs1"],
            "batch_id": "b0",
        }
        result = _reconcile("fp1", 10, "cs1", "b1", prior)
        assert result["status"] == "idempotent"
        assert result["prior_batch_id"] == "b0"

    def test_schema_changed_when_fingerprint_differs(self):
        prior = {
            "schema_fingerprint": "old_fp",
            "row_count": 10,
            "file_checksums": ["cs1"],
            "batch_id": "b0",
        }
        result = _reconcile("new_fp", 10, "cs1", "b1", prior)
        assert result["status"] == "schema_changed"
        assert result["details"]["prior_fingerprint"] == "old_fp"
        assert result["details"]["new_fingerprint"] == "new_fp"

    def test_row_count_changed(self):
        prior = {
            "schema_fingerprint": "fp1",
            "row_count": 5,
            "file_checksums": ["cs1"],
            "batch_id": "b0",
        }
        result = _reconcile("fp1", 10, "cs1", "b1", prior)
        assert result["status"] == "row_count_changed"
        assert result["details"]["prior_row_count"] == 5
        assert result["details"]["new_row_count"] == 10

    def test_data_changed_when_checksum_differs(self):
        prior = {
            "schema_fingerprint": "fp1",
            "row_count": 10,
            "file_checksums": ["old_cs"],
            "batch_id": "b0",
        }
        result = _reconcile("fp1", 10, "new_cs", "b1", prior)
        assert result["status"] == "data_changed"

    def test_schema_change_takes_priority_over_row_count(self):
        """Schema change is detected first even when row count also differs."""
        prior = {
            "schema_fingerprint": "fp_old",
            "row_count": 1,
            "file_checksums": ["cs_old"],
            "batch_id": "b0",
        }
        result = _reconcile("fp_new", 99, "cs_new", "b1", prior)
        assert result["status"] == "schema_changed"

    def test_prior_evidence_fields_populated(self):
        prior = {
            "schema_fingerprint": "fp1",
            "row_count": 7,
            "file_checksums": ["abc123"],
            "batch_id": "prior-batch",
        }
        result = _reconcile("fp1", 7, "abc123", "new-batch", prior)
        assert result["prior_schema_fingerprint"] == "fp1"
        assert result["prior_row_count"] == 7
        assert result["prior_file_checksum"] == "abc123"
        assert result["prior_batch_id"] == "prior-batch"

    def test_missing_prior_fingerprint_not_schema_changed(self):
        """A prior manifest with no schema_fingerprint should not trigger schema_changed."""
        prior = {
            "schema_fingerprint": "",
            "row_count": 10,
            "file_checksums": ["cs1"],
            "batch_id": "b0",
        }
        result = _reconcile("fp1", 10, "cs1", "b1", prior)
        # No fingerprint to compare → falls through to row_count / checksum check
        assert result["status"] == "idempotent"

    def test_missing_prior_row_count_not_row_count_changed(self):
        """A prior manifest with no row_count should not trigger row_count_changed."""
        prior = {
            "schema_fingerprint": "fp1",
            "row_count": None,
            "file_checksums": ["cs1"],
            "batch_id": "b0",
        }
        result = _reconcile("fp1", 99, "cs1", "b1", prior)
        assert result["status"] == "idempotent"


class TestLoadPriorManifest:
    def test_returns_none_when_file_missing(self, tmp_path):
        p = tmp_path / "no__such__manifest.json"
        assert _load_prior_manifest(p) is None

    def test_returns_dict_when_valid(self, tmp_path):
        p = tmp_path / "m.json"
        p.write_text('{"batch_id": "b1", "row_count": 3}')
        result = _load_prior_manifest(p)
        assert result == {"batch_id": "b1", "row_count": 3}

    def test_returns_none_on_invalid_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("NOT JSON {{{{")
        assert _load_prior_manifest(p) is None


class TestReconciliationIntegration:
    """End-to-end: run export twice and verify reconciliation status in manifest."""

    def test_first_export_is_first_run(self, tmp_path):
        rows = [(1, "a"), (2, "b")]
        col_names = ["id", "val"]
        mock_conn, _ = _make_mock_pg_conn(rows, col_names)

        with patch.object(export_table_mod, "_connect_postgres", return_value=mock_conn):
            manifest = export_table(
                "postgresql://x/db", "s", "t", str(tmp_path), batch_id="b1"
            )

        recon = manifest["prior_run_reconciliation"]
        assert recon is not None
        assert recon["status"] == "first_run"

    def test_rerun_same_data_is_idempotent(self, tmp_path):
        rows = [(1, "a"), (2, "b")]
        col_names = ["id", "val"]

        for batch_id in ("b1", "b2"):
            mock_conn, _ = _make_mock_pg_conn(rows, col_names)
            with patch.object(export_table_mod, "_connect_postgres", return_value=mock_conn):
                manifest = export_table(
                    "postgresql://x/db", "s", "t", str(tmp_path), batch_id=batch_id
                )

        recon = manifest["prior_run_reconciliation"]
        assert recon["status"] == "idempotent"

    def test_rerun_different_row_count_is_row_count_changed(self, tmp_path):
        # First run: 2 rows
        mock_conn, _ = _make_mock_pg_conn([(1, "a"), (2, "b")], ["id", "val"])
        with patch.object(export_table_mod, "_connect_postgres", return_value=mock_conn):
            export_table("postgresql://x/db", "s", "t", str(tmp_path), batch_id="b1")

        # Second run: 3 rows
        mock_conn, _ = _make_mock_pg_conn([(1, "a"), (2, "b"), (3, "c")], ["id", "val"])
        with patch.object(export_table_mod, "_connect_postgres", return_value=mock_conn):
            manifest = export_table(
                "postgresql://x/db", "s", "t", str(tmp_path), batch_id="b2"
            )

        recon = manifest["prior_run_reconciliation"]
        assert recon["status"] == "row_count_changed"
        assert recon["details"]["prior_row_count"] == 2
        assert recon["details"]["new_row_count"] == 3

    def test_rerun_different_schema_is_schema_changed(self, tmp_path):
        # First run: {id, val}
        mock_conn, _ = _make_mock_pg_conn([(1, "a")], ["id", "val"])
        with patch.object(export_table_mod, "_connect_postgres", return_value=mock_conn):
            export_table("postgresql://x/db", "s", "t", str(tmp_path), batch_id="b1")

        # Second run: {id, val, extra}
        mock_conn, _ = _make_mock_pg_conn([(1, "a", True)], ["id", "val", "extra"])
        with patch.object(export_table_mod, "_connect_postgres", return_value=mock_conn):
            manifest = export_table(
                "postgresql://x/db", "s", "t", str(tmp_path), batch_id="b2"
            )

        recon = manifest["prior_run_reconciliation"]
        assert recon["status"] == "schema_changed"

    def test_rerun_reconciliation_parses_into_transfer_batch(self, tmp_path):
        """The reconciliation block must survive TransferBatch.model_validate()."""
        rows = [(1,)]
        col_names = ["id"]

        for bid in ("b1", "b2"):
            mock_conn, _ = _make_mock_pg_conn(rows, col_names)
            with patch.object(export_table_mod, "_connect_postgres", return_value=mock_conn):
                manifest = export_table(
                    "postgresql://x/db", "s", "t", str(tmp_path), batch_id=bid
                )

        from workbench.backend.platform.transfer_batch import TransferBatch, ReconciliationStatus
        batch = TransferBatch.model_validate(manifest)
        assert batch.prior_run_reconciliation is not None
        assert batch.prior_run_reconciliation.status == ReconciliationStatus.idempotent


# ── CLI parser ─────────────────────────────────────────────────────────────────

class TestCliParser:
    def test_required_positional_args(self):
        ns = export_table_mod._parse_args.__wrapped__ if hasattr(
            export_table_mod._parse_args, "__wrapped__"
        ) else None
        # Just test via argparse directly
        import argparse
        saved = sys.argv
        try:
            sys.argv = [
                "export_table.py",
                "postgresql://u:p@h/db",
                "public",
                "orders",
                "/tmp/out",
                "--run-id", "r1",
                "--limit", "500",
                "--primary-key", "id",
            ]
            args = export_table_mod._parse_args()
        finally:
            sys.argv = saved
        assert args.connection_string == "postgresql://u:p@h/db"
        assert args.schema == "public"
        assert args.table == "orders"
        assert args.output_dir == "/tmp/out"
        assert args.run_id == "r1"
        assert args.limit == 500
        assert args.primary_key == ["id"]
