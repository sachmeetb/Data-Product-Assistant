"""Tests for the lakehouse (Parquet + DuckDB) serving path (Part 3).

Covers:
- DuckDBDialect emission (Postgres-like cast/concat, native sha256).
- The third emitter (generate_lakehouse_models) shares the SQL core with the dbt
  emitter — byte-identical select_body for the same dialect.
- Manifest reconciliation + type-warning capture (pure helpers).
- A real DuckDB Parquet round-trip (COPY → read_parquet catalog view).
- Exclusive-group wiring (serving member, depends on configure_serving,
  selecting lakehouse disables deploy_virtual_view).
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

_GEN = pathlib.Path(__file__).resolve().parents[3] / (
    "workbench-skills/skills/data-serving-virtual-view/scripts/generate_view_ddl.py"
)
_spec = importlib.util.spec_from_file_location("generate_view_ddl", _GEN)
g = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(g)


# ── DuckDBDialect ─────────────────────────────────────────────────────────────

class TestDuckDBDialect:
    def test_registered(self):
        d = g.get_dialect("duckdb")
        assert d.name == "duckdb"

    def test_cast_postgres_style(self):
        d = g.get_dialect("duckdb")
        assert d.cast("x", "INTEGER") == "x::INTEGER"

    def test_concat_uses_double_pipe(self):
        d = g.get_dialect("duckdb")
        assert d.str_concat(["a", "b"], sep="") == "a || b"

    def test_sha256_native(self):
        d = g.get_dialect("duckdb")
        assert d.hash_sha("x", "sha256") == "sha256(x)"

    def test_sha1_degrades_to_md5(self):
        d = g.get_dialect("duckdb")
        out = d.hash_sha("x", "sha1")
        assert out.startswith("md5(x)")

    def test_regexp_replace_global_flag(self):
        d = g.get_dialect("duckdb")
        assert d.regexp_replace_global("h", "'p'", "'r'").endswith(", 'g')")


# ── Emitter parity (byte-identical select_body for the same dialect) ───────────

class _FakeSession:
    def __init__(self, datasets):
        self._datasets = datasets

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, query, **params):
        if "DPROD_OUTPUT_DATASET" in query or "OUTPUT_DATASETS" in query or "ods" in query:
            return list(self._datasets)
        return []


class _FakeDriver:
    def __init__(self, datasets):
        self._datasets = datasets

    def session(self, database=None):
        return _FakeSession(self._datasets)

    def close(self):
        pass


def test_lakehouse_and_dbt_emitters_byte_identical(monkeypatch):
    """Both emitters delegate to the same per-dataset compiler; for one dialect
    the returned select_body must be byte-identical (proves one SQL core)."""
    datasets = [{"uri": "ods:1", "physical_name": "orders"}]

    calls = {"n": 0}

    def _fake_gen(session, product_uri, ds_uri, physical, view_schema, dialect):
        calls["n"] += 1
        body = f"SELECT * FROM {view_schema}.{physical} /* {dialect.name} */"
        return (f"CREATE VIEW x AS {body};", f"{view_schema}.vw_{physical}", {"ok": True}, body)

    monkeypatch.setattr(g, "_generate_ddl_for_dataset", _fake_gen)

    dbt_models, _ = g.generate_dbt_models(
        _FakeDriver(datasets), "neo4j", "dprod:x", view_schema="public", dialect_name="postgres"
    )
    lake_models, _ = g.generate_lakehouse_models(
        _FakeDriver(datasets), "neo4j", "dprod:x", view_schema="public", dialect_name="postgres"
    )
    assert dbt_models[0]["select_body"] == lake_models[0]["select_body"]


def test_lakehouse_defaults_to_duckdb_dialect(monkeypatch):
    datasets = [{"uri": "ods:1", "physical_name": "orders"}]

    def _fake_gen(session, product_uri, ds_uri, physical, view_schema, dialect):
        return (f"CREATE VIEW x AS SELECT 1;", "public.vw_orders", {}, f"body[{dialect.name}]")

    monkeypatch.setattr(g, "_generate_ddl_for_dataset", _fake_gen)
    models, summary = g.generate_lakehouse_models(_FakeDriver(datasets), "neo4j", "dprod:x")
    assert models[0]["select_body"] == "body[duckdb]"
    assert summary["dialect"] == "duckdb"


# ── Manifest reconciliation + type warnings (pure helpers) ────────────────────

class TestManifestHelpers:
    def test_reconcile_no_prior(self):
        from workbench.backend.lakehouse_export import _reconcile
        assert _reconcile(None, 10, "fp", "cs") == {"has_prior": False}

    def test_reconcile_detects_row_delta_and_schema_change(self):
        from workbench.backend.lakehouse_export import _reconcile
        prior = {
            "batch_id": "b1", "row_count": 8, "schema_fingerprint": "old",
            "files": [{"checksum_sha256": "oldcs"}],
        }
        r = _reconcile(prior, 10, "new", "newcs")
        assert r["has_prior"] is True
        assert r["row_count_delta"] == 2
        assert r["schema_changed"] is True
        assert r["file_checksum_changed"] is True

    def test_load_prior_manifest_missing(self, tmp_path):
        from workbench.backend.lakehouse_export import _load_prior_manifest
        assert _load_prior_manifest(tmp_path / "nope.json") is None

    def test_load_prior_manifest_roundtrip(self, tmp_path):
        from workbench.backend.lakehouse_export import _load_prior_manifest
        p = tmp_path / "m.json"
        p.write_text(json.dumps({"batch_id": "b1"}))
        assert _load_prior_manifest(p)["batch_id"] == "b1"


# ── Real DuckDB Parquet round-trip + type system activation ───────────────────

def test_duckdb_parquet_catalog_roundtrip(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    pq = pytest.importorskip("pyarrow.parquet")
    parquet = tmp_path / "orders.parquet"
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT 1 AS id, 'a' AS name UNION ALL SELECT 2, 'b') "
        f"TO '{parquet}' (FORMAT PARQUET)"
    )
    # DuckDB + pyarrow dual-engine row-count check (the verify gate).
    duck_rows = con.execute(f"SELECT count(*) FROM read_parquet('{parquet}')").fetchone()[0]
    pa_rows = pq.ParquetFile(str(parquet)).metadata.num_rows
    assert duck_rows == pa_rows == 2

    # Persistent catalog view over the Parquet.
    catalog = tmp_path / "catalog.duckdb"
    cat = duckdb.connect(str(catalog))
    cat.execute(f"CREATE OR REPLACE VIEW orders AS SELECT * FROM read_parquet('{parquet}')")
    assert cat.execute("SELECT count(*) FROM orders").fetchone()[0] == 2
    cat.close()
    con.close()


def test_type_warnings_captures_from_arrow_schema():
    pa = pytest.importorskip("pyarrow")
    from workbench.backend.lakehouse_export import _type_warnings
    schema = pa.schema([("id", pa.int32()), ("name", pa.string())])
    # Should not raise; returns a list (possibly empty depending on profile mapping).
    warnings = _type_warnings(schema, "postgres")
    assert isinstance(warnings, list)


# ── Exclusive-group wiring ────────────────────────────────────────────────────

class TestLakehouseWiring:
    def test_registered_in_stage_registry(self):
        from workbench.backend.archetypes import STAGE_REGISTRY
        s = STAGE_REGISTRY["serving_lakehouse_export"]
        assert s["requires_llm"] is False
        assert s["has_review"] is False

    def test_depends_on_configure_serving(self):
        from workbench.backend.archetypes import DEPENDENCY_GRAPH
        assert DEPENDENCY_GRAPH["serving_lakehouse_export"] == ["configure_serving"]

    def test_is_serving_exclusive_group_member(self):
        from workbench.backend.archetypes import _WF_INTEGRATION, _WF_PRODUCT_MATERIALIZATION_SA
        for wf in (_WF_INTEGRATION, _WF_PRODUCT_MATERIALIZATION_SA):
            member = next(
                (s for s in wf["stages"] if s["stage_id"] == "serving_lakehouse_export"), None
            )
            assert member is not None
            assert member["exclusive_group"] == "serving"
            assert member["enabled"] is False

    def test_selecting_lakehouse_pulls_deploy_and_profile_parquet(self):
        from workbench.backend.archetypes import EXCLUSIVE_GROUP_DEPENDENTS
        serving = EXCLUSIVE_GROUP_DEPENDENTS["serving"]
        # Uniform Build → Deploy: lakehouse Build now pulls its coupled Run Export
        # deploy alongside the profile_parquet dual-engine verify.
        assert serving["serving_lakehouse_export"] == ["deploy_lakehouse", "profile_parquet"]
        # deploy_virtual_view rides only with the virtual member — so switching to
        # lakehouse (which doesn't list it) disables it.
        assert "deploy_virtual_view" not in serving["serving_lakehouse_export"]
        assert serving["serving_virtual_view"] == ["deploy_virtual_view"]
