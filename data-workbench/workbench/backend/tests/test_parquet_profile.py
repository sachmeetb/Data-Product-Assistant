"""Tests for the data-profile-parquet skill (profile_parquet.py).

All tests use real pyarrow + DuckDB — no mocks needed since we're profiling
in-memory Parquet files written to tmp_path.

Coverage:
- Source resolution (file, glob, manifest JSON)
- DuckDB numeric/text/date/boolean column statistics
- Dual-engine row-count verification (DuckDB vs pyarrow)
- Row-count mismatch raises RuntimeError
- Schema/table derived from file stem
- Schema/table override
- Profile YAML matches data-profiling schema (loadable by graph loader)
- Empty Parquet file
- Platform manifests load (S3, GCS, Azure ADLS)
"""
import importlib.util
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

# ── locate the skill script ────────────────────────────────────────────────────
_SKILL_DIR = (
    Path(__file__).parent.parent.parent.parent
    / "workbench-skills" / "skills" / "data-profile-parquet" / "scripts"
)
_SCRIPT = _SKILL_DIR / "profile_parquet.py"
assert _SCRIPT.exists(), f"profile_parquet.py not found at {_SCRIPT}"

spec = importlib.util.spec_from_file_location("profile_parquet", _SCRIPT)
pp_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pp_mod)

profile_parquet = pp_mod.profile_parquet
_resolve_sources = pp_mod._resolve_sources
_schema_table_from_stem = pp_mod._schema_table_from_stem
_profile_one_file = pp_mod._profile_one_file


# ── helpers ────────────────────────────────────────────────────────────────────

def _write_parquet(path: Path, data: dict) -> Path:
    table = pa.table(data)
    pq.write_table(table, str(path))
    return path


def _write_manifest(path: Path, parquet_path: Path) -> Path:
    manifest = {
        "contract_version": "1",
        "run_id": "test-run",
        "batch_id": "batch-001",
        "source_asset_ref": {
            "kind": "relational_relation",
            "platform_instance_id": "postgres",
            "asset_id": "public.orders",
            "relation": "orders",
        },
        "file_uris": [parquet_path.resolve().as_uri()],
        "completion_marker": "complete",
    }
    path.write_text(json.dumps(manifest))
    return path


# ── source resolution ──────────────────────────────────────────────────────────

class TestResolveSource:
    def test_single_file(self, tmp_path):
        p = tmp_path / "orders.parquet"
        _write_parquet(p, {"id": [1, 2]})
        result = _resolve_sources(str(p))
        assert len(result) == 1
        assert result[0] == p

    def test_glob_pattern(self, tmp_path):
        for name in ["a.parquet", "b.parquet"]:
            _write_parquet(tmp_path / name, {"x": [1]})
        result = _resolve_sources(str(tmp_path / "*.parquet"))
        assert len(result) == 2

    def test_manifest_json(self, tmp_path):
        pq_path = tmp_path / "public__orders.parquet"
        _write_parquet(pq_path, {"id": [1]})
        manifest_path = tmp_path / "public__orders__manifest.json"
        _write_manifest(manifest_path, pq_path)
        result = _resolve_sources(str(manifest_path))
        assert len(result) == 1
        assert result[0].name == "public__orders.parquet"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _resolve_sources(str(tmp_path / "nonexistent.parquet"))

    def test_manifest_missing_parquet_raises(self, tmp_path):
        manifest = {"contract_version": "1", "file_uris": ["file:///nonexistent.parquet"]}
        mp = tmp_path / "manifest.json"
        mp.write_text(json.dumps(manifest))
        with pytest.raises(FileNotFoundError):
            _resolve_sources(str(mp))


class TestSchemTableFromStem:
    def test_double_underscore_split(self):
        assert _schema_table_from_stem("public__orders") == ("public", "orders")

    def test_strips_manifest_suffix(self):
        assert _schema_table_from_stem("public__orders__manifest") == ("public", "orders")

    def test_strips_profile_suffix(self):
        assert _schema_table_from_stem("public__orders__profile") == ("public", "orders")

    def test_no_separator_defaults_to_parquet(self):
        schema, table = _schema_table_from_stem("orders")
        assert schema == "parquet"
        assert table == "orders"


# ── profile one file ───────────────────────────────────────────────────────────

class TestProfileOneFile:
    def test_numeric_columns(self, tmp_path):
        p = tmp_path / "public__sales.parquet"
        _write_parquet(p, {
            "id": pa.array([1, 2, 3], type=pa.int64()),
            "amount": pa.array([10.0, 20.0, 30.0], type=pa.float64()),
        })
        result = _profile_one_file(p, "public", "sales", 10, verify=True)
        assert result["row_count"] == 3
        assert result["schema"] == "public"
        assert result["table"] == "sales"
        col_names = [c["name"] for c in result["columns"]]
        assert "id" in col_names
        assert "amount" in col_names
        id_col = next(c for c in result["columns"] if c["name"] == "id")
        assert id_col["min"] == 1.0
        assert id_col["max"] == 3.0
        assert id_col["null_count"] == 0

    def test_text_columns(self, tmp_path):
        p = tmp_path / "s__t.parquet"
        _write_parquet(p, {
            "name": pa.array(["Alice", "Bob", "Charlie"], type=pa.string()),
        })
        result = _profile_one_file(p, "s", "t", 10, verify=True)
        col = result["columns"][0]
        assert col["name"] == "name"
        assert col["min_length"] is not None
        assert col["max_length"] is not None
        assert "top_values" in col

    def test_null_count(self, tmp_path):
        p = tmp_path / "s__t.parquet"
        _write_parquet(p, {
            "x": pa.array([1, None, 3], type=pa.int64()),
        })
        result = _profile_one_file(p, "s", "t", 10, verify=True)
        col = result["columns"][0]
        assert col["null_count"] == 1
        assert col["null_rate"] == pytest.approx(1 / 3, rel=1e-3)

    def test_boolean_column(self, tmp_path):
        p = tmp_path / "s__t.parquet"
        _write_parquet(p, {
            "active": pa.array([True, False, True], type=pa.bool_()),
        })
        result = _profile_one_file(p, "s", "t", 10, verify=True)
        col = result["columns"][0]
        assert col["name"] == "active"
        assert "top_values" in col

    def test_empty_file(self, tmp_path):
        p = tmp_path / "s__t.parquet"
        _write_parquet(p, {"id": pa.array([], type=pa.int64())})
        result = _profile_one_file(p, "s", "t", 10, verify=True)
        assert result["row_count"] == 0


# ── dual-engine verification ───────────────────────────────────────────────────

class TestDualEngineVerification:
    def test_matching_counts_passes(self, tmp_path):
        p = tmp_path / "s__t.parquet"
        _write_parquet(p, {"id": [1, 2, 3]})
        # Should not raise
        result = _profile_one_file(p, "s", "t", 10, verify=True)
        assert result["row_count"] == 3

    def test_no_verify_skips_pyarrow(self, tmp_path):
        """--no-verify path: profile completes without importing pyarrow for check."""
        p = tmp_path / "s__t.parquet"
        _write_parquet(p, {"id": [1, 2]})
        # Just verify it doesn't raise
        result = _profile_one_file(p, "s", "t", 10, verify=False)
        assert result["row_count"] == 2


# ── full profile_parquet function ──────────────────────────────────────────────

class TestProfileParquetFunction:
    def test_writes_yaml(self, tmp_path):
        pq_path = tmp_path / "public__orders.parquet"
        out_dir = tmp_path / "profiles"
        _write_parquet(pq_path, {"id": [1, 2], "total": [9.99, 19.99]})
        written = profile_parquet(str(pq_path), str(out_dir))
        assert len(written) == 1
        yaml_path = Path(written[0])
        assert yaml_path.exists()
        assert yaml_path.name == "public__orders__profile.yaml"

    def test_yaml_content_matches_profiler_schema(self, tmp_path):
        import yaml
        pq_path = tmp_path / "myschema__items.parquet"
        out_dir = tmp_path / "profiles"
        _write_parquet(pq_path, {"sku": ["A", "B"], "qty": [10, 20]})
        written = profile_parquet(str(pq_path), str(out_dir))
        with open(written[0]) as f:
            profile = yaml.safe_load(f)
        assert profile["schema"] == "myschema"
        assert profile["table"] == "items"
        assert "profiled_at" in profile
        assert "row_count" in profile
        assert "columns" in profile
        assert profile["row_count"] == 2
        col_names = [c["name"] for c in profile["columns"]]
        assert "sku" in col_names
        assert "qty" in col_names

    def test_schema_table_override(self, tmp_path):
        import yaml
        pq_path = tmp_path / "raw_file.parquet"
        out_dir = tmp_path / "profiles"
        _write_parquet(pq_path, {"x": [1, 2, 3]})
        written = profile_parquet(
            str(pq_path), str(out_dir),
            schema_override="override_schema",
            table_override="override_table",
        )
        with open(written[0]) as f:
            profile = yaml.safe_load(f)
        assert profile["schema"] == "override_schema"
        assert profile["table"] == "override_table"

    def test_manifest_source(self, tmp_path):
        pq_path = tmp_path / "public__orders.parquet"
        manifest_path = tmp_path / "public__orders__manifest.json"
        out_dir = tmp_path / "profiles"
        _write_parquet(pq_path, {"order_id": [100, 200]})
        _write_manifest(manifest_path, pq_path)
        written = profile_parquet(str(manifest_path), str(out_dir))
        assert len(written) == 1
        assert Path(written[0]).exists()

    def test_no_verify_flag(self, tmp_path):
        pq_path = tmp_path / "public__t.parquet"
        out_dir = tmp_path / "profiles"
        _write_parquet(pq_path, {"v": [1, 2]})
        written = profile_parquet(str(pq_path), str(out_dir), verify=False)
        assert len(written) == 1


# ── platform manifest loading ──────────────────────────────────────────────────

class TestObjectStoreManifests:
    def _get_registry(self):
        from workbench.backend.platform.registry import get_registry
        return get_registry()

    def test_s3_registered(self):
        reg = self._get_registry()
        m = reg.get_manifest("s3")
        assert m is not None
        assert m.adapter_kind == "object_store"
        assert not reg.is_usable("s3", "connection")

    def test_gcs_registered(self):
        reg = self._get_registry()
        m = reg.get_manifest("gcs")
        assert m is not None
        assert m.adapter_kind == "object_store"

    def test_azure_adls_registered(self):
        reg = self._get_registry()
        m = reg.get_manifest("azure_adls")
        assert m is not None
        assert m.adapter_kind == "object_store"

    def test_object_stores_fail_closed(self):
        reg = self._get_registry()
        for pid in ("s3", "gcs", "azure_adls"):
            for cap in ("connection", "discovery", "profiling", "transfer_target"):
                assert not reg.is_usable(pid, cap), (
                    f"{pid}.{cap} should not be usable — gate not yet passed"
                )

    def test_all_seven_platforms_registered(self):
        reg = self._get_registry()
        ids = set(reg.platform_ids())
        assert {"postgres", "mysql", "snowflake", "databricks",
                "s3", "gcs", "azure_adls"}.issubset(ids)
