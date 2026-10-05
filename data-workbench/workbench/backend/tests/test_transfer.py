"""Tests for cross-platform transfer (transfer_then_transform serving).

The transfer path is a **dlt package** (like the migration project type) — no
DuckDB. Pure/wiring coverage here; the actual Extract→Parquet→Load needs a live
source + target (deferred to the docker e2e), same as the migration runner.
"""
from __future__ import annotations

import os
import shutil

import pytest


from types import SimpleNamespace

_FAKE_PROJECT = SimpleNamespace(
    neo4j_host="h", neo4j_port=7687, neo4j_user="u", neo4j_password="p", neo4j_database="db",
)


class TestPlacementAwareCompile:
    """_compile_datasets branches on the placement decision (ETL / ELT / governance)."""

    def _patch(self, monkeypatch, *, src_bodies, tgt_bodies=None, elt_plan=None):
        import workbench.backend.transfer_execution as te

        class _FakeGV:
            def generate_lakehouse_models(self, driver, db, uri, view_schema, dialect_name):
                bodies = (tgt_bodies if (tgt_bodies and dialect_name != "postgres") else src_bodies)
                models = [{"model_name": n, "physical_name": n, "select_body": b,
                           "summary": {"tables": ["public.t"]}} for n, b in bodies]
                return models, "ok"

        monkeypatch.setattr(te, "_load_gv", lambda: _FakeGV())
        monkeypatch.setattr(te, "get_driver", lambda *a, **k: type("D", (), {"close": lambda self: None})())
        if elt_plan is not None:
            monkeypatch.setattr("workbench.backend.platform.transform_placement.build_elt_plan",
                                lambda models, landing_schema="wb_landing": elt_plan)
        return te

    def test_transform_on_extract_uses_source_shaped_select(self, monkeypatch):
        te = self._patch(monkeypatch, src_bodies=[("orders", "SELECT * FROM src")])
        ds, landing, _schema, eff, warns = te._compile_datasets(
            _FAKE_PROJECT, "uri", "postgres", "snowflake", "replace", "transform_on_extract", set())
        assert eff == "transform_on_extract"
        assert landing == []
        assert ds[0]["select_sql"] == "SELECT * FROM src" and ds[0]["target_model_sql"] is None

    def test_masking_forces_etl_even_when_hybrid_requested(self, monkeypatch):
        te = self._patch(monkeypatch, src_bodies=[("customer", "SELECT mask(ssn) FROM src")])
        ds, landing, _schema, eff, warns = te._compile_datasets(
            _FAKE_PROJECT, "uri", "postgres", "snowflake", "replace", "hybrid", {"customer"})
        assert eff == "transform_on_extract"
        assert landing == []
        assert any("Masking present" in w for w in warns)
        assert ds[0]["target_model_sql"] is None

    def test_hybrid_split_emits_landing_and_target_model(self, monkeypatch):
        plan = {
            "landing_schema": "wb_landing",
            "landing_relations": [{"source_schema": "public", "source_table": "t",
                                   "landing_table": "public_t", "projection": None, "filter": None}],
            "datasets": [{"physical_name": "orders", "model_name": "orders",
                          "target_body": 'SELECT * FROM "wb_landing"."public_t"'}],
        }
        te = self._patch(monkeypatch,
                         src_bodies=[("orders", "src")],
                         tgt_bodies=[("orders", 'SELECT * FROM "public"."t"')],
                         elt_plan=plan)
        ds, landing, _schema, eff, warns = te._compile_datasets(
            _FAKE_PROJECT, "uri", "postgres", "snowflake", "replace", "hybrid", set())
        assert eff == "hybrid"
        assert landing and landing[0]["landing_table"] == "public_t"
        assert ds[0]["select_sql"] is None
        assert 'wb_landing' in ds[0]["target_model_sql"]

    def test_elt_failure_falls_back_to_etl(self, monkeypatch):
        te = self._patch(monkeypatch,
                         src_bodies=[("orders", "SELECT * FROM src")],
                         tgt_bodies=[("orders", "backtick-quoted body")],
                         elt_plan={"landing_relations": [], "datasets": []})  # no landings → fallback
        ds, landing, _schema, eff, warns = te._compile_datasets(
            _FAKE_PROJECT, "uri", "postgres", "databricks", "replace", "transfer_then_transform", set())
        assert eff == "transform_on_extract"
        assert ds[0]["select_sql"] == "SELECT * FROM src"
        assert any("realized as transform_on_extract" in w for w in warns)


class TestTransferProviderPlan:
    def test_provider_satisfies_protocol(self):
        from workbench.backend.platform.interfaces import TransferExecutionProvider
        from workbench.backend.platform.providers.transfer import DuckDBTransferProvider
        assert isinstance(DuckDBTransferProvider(), TransferExecutionProvider)

    def test_plan_transfer_builds_datasets(self):
        from workbench.backend.platform.providers.transfer import DuckDBTransferProvider
        from workbench.backend.platform.interfaces import TransferSpec
        spec = TransferSpec(
            product_uri="dprod:x", source_platform="postgres",
            target_platform="duckdb_local", target_schema="analytics", placement="hybrid",
        )
        plan = DuckDBTransferProvider().plan_transfer(
            spec,
            compiled_models=[{"physical_name": "orders", "model_name": "orders", "select_body": "SELECT 1"}],
            model_summary={"views": [{"view_name": "orders", "used_filter": True}]},
        )
        assert len(plan.datasets) == 1
        assert plan.datasets[0].target_relation == "analytics.orders"
        assert plan.placement == "transform_on_extract"
        assert any("realized as" in w for w in plan.warnings)

    def test_execute_transfer_steers_to_worker(self):
        from workbench.backend.platform.providers.transfer import DuckDBTransferProvider
        from workbench.backend.platform.interfaces import TransferPlan, TransferSpec
        plan = TransferPlan(spec=TransferSpec(
            product_uri="x", source_platform="postgres", target_platform="duckdb_local"))
        with pytest.raises(NotImplementedError):
            DuckDBTransferProvider().execute_transfer(plan, "key")


class TestTransferDispatch:
    def test_get_transfer_provider_for_supported_targets(self):
        from workbench.backend.routers.connections import _get_transfer_provider
        for pid in ("postgres", "mysql", "duckdb_local"):
            assert _get_transfer_provider(pid) is not None

    def test_get_transfer_provider_for_warehouses(self):
        from workbench.backend.routers.connections import _get_transfer_provider
        assert _get_transfer_provider("snowflake") is not None
        assert _get_transfer_provider("databricks") is not None

    def test_get_transfer_provider_none_for_object_stores(self):
        from workbench.backend.routers.connections import _get_transfer_provider
        assert _get_transfer_provider("s3") is None


class TestTransferCapabilities:
    def test_manifest_transfer_capabilities(self):
        from workbench.backend.platform.registry import get_registry
        reg = get_registry()
        assert reg.is_usable("postgres", "transfer_source")
        assert reg.is_usable("postgres", "transfer_target")
        assert reg.is_usable("duckdb_local", "transfer_target")
        assert reg.is_usable("mysql", "transfer_source")
        assert reg.is_usable("snowflake", "transfer_target")
        assert reg.is_usable("databricks", "transfer_target")
        assert not reg.is_usable("s3", "transfer_target")
        # Snowflake is now a transfer SOURCE too (preview) — dlt reads it via
        # snowflake-sqlalchemy (run_transfer/run_migration _source_sqlalchemy_url).
        assert reg.is_usable("snowflake", "transfer_source")


class TestTransferRunner:
    """The dlt runner is a self-contained package script (stdlib + dlt + driver;
    no workbench.* imports, no duckdb)."""

    def test_runner_exists_and_parses(self):
        import ast
        from workbench.backend.config import BASE_DIR
        p = BASE_DIR / "workbench" / "backend" / "serving_runners" / "run_transfer.py"
        assert p.exists(), "run_transfer.py runner missing"
        src = p.read_text()
        ast.parse(src)
        # dlt only — no DuckDB engine in the transfer runner.
        assert "import duckdb" not in src
        # No backend imports — the package is self-contained.
        assert "from workbench" not in src and "import workbench" not in src
        assert "import dlt" in src

    @staticmethod
    def _load_runner():
        import importlib.util, sys
        from workbench.backend.config import BASE_DIR
        runners = BASE_DIR / "workbench" / "backend" / "serving_runners"
        if str(runners) not in sys.path:
            sys.path.insert(0, str(runners))
        spec = importlib.util.spec_from_file_location(
            "run_transfer_under_test", runners / "run_transfer.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_snowflake_source_sqlalchemy_url(self, monkeypatch):
        rt = self._load_runner()
        monkeypatch.delenv("WB_SOURCE_DSN", raising=False)
        monkeypatch.setenv("WB_SOURCE_ACCOUNT", "org-acct")
        monkeypatch.setenv("WB_SOURCE_USER", "NEYDE")
        monkeypatch.setenv("WB_SOURCE_PASSWORD", "pat/tok")
        monkeypatch.setenv("WB_SOURCE_DBNAME", "DWB_SERVING_DB")
        monkeypatch.setenv("WB_SOURCE_SCHEMA", "PUBLIC")
        monkeypatch.setenv("WB_SOURCE_WAREHOUSE", "COMPUTE_WH")
        monkeypatch.setenv("WB_SOURCE_ROLE", "R")
        url = rt._source_sqlalchemy_url("snowflake")
        assert url.startswith("snowflake://NEYDE:")          # PAT rides as the password
        assert "@org-acct/DWB_SERVING_DB/PUBLIC" in url       # account/database/schema path
        assert "warehouse=COMPUTE_WH" in url and "role=R" in url

    def test_snowflake_target_accepts_pat_via_token(self, monkeypatch):
        rt = self._load_runner()
        captured = {}

        class _FakeDests:
            def snowflake(self, credentials):
                captured.update(credentials)
                return "SF_DEST"
        fake_dlt = SimpleNamespace(destinations=_FakeDests())
        monkeypatch.setenv("WB_TARGET_ACCOUNT", "org-acct")
        monkeypatch.setenv("WB_TARGET_USER", "u")
        monkeypatch.delenv("WB_TARGET_PASSWORD", raising=False)
        monkeypatch.setenv("WB_TARGET_TOKEN", "pat-token")
        monkeypatch.setenv("WB_TARGET_DBNAME", "DB")
        monkeypatch.setenv("WB_TARGET_WAREHOUSE", "WH")
        assert rt._build_destination(fake_dlt, "snowflake") == "SF_DEST"
        assert captured["password"] == "pat-token"

    def test_dlt_scope_is_per_run_isolated(self, monkeypatch):
        """Two concurrent runs must NOT share a dlt working dir or pipeline identity —
        the root cause of the concurrent-transfer collision. Without a backend run_id
        each invocation gets a fresh, unique scope; the dir lives under a `.dlt/` tree."""
        rt = self._load_runner()
        monkeypatch.delenv("WB_S3_RUN_ID", raising=False)
        rid1, dir1 = rt._dlt_scope()
        rid2, dir2 = rt._dlt_scope()
        assert rid1 != rid2 and dir1 != dir2            # per-run unique
        assert dir1.endswith(rid1) and dir2.endswith(rid2)
        assert f"{os.sep}.dlt{os.sep}" in dir1
        # A backend-supplied run_id is honoured (keeps S3 prefix / audit row aligned).
        monkeypatch.setenv("WB_S3_RUN_ID", "fixed-run-abc")
        rid3, dir3 = rt._dlt_scope()
        assert rid3 == "fixed-run-abc" and dir3.endswith("fixed-run-abc")

    def test_namespace_split(self):
        from workbench.backend.transfer_execution import _split_namespace
        # 3-level "catalog.schema" splits into both parts.
        assert _split_namespace("workspace.default", "databricks", "samples.bakehouse") == ("workspace", "default")
        # Bare schema for a 2-level target.
        assert _split_namespace("analytics", "postgres", "") == ("", "analytics")
        # Empty namespace on a 3-level target falls back to the connection database.
        assert _split_namespace("", "databricks", "samples.bakehouse") == ("samples", "bakehouse")


class TestTransferPackage:
    def test_snowflake_source_package_bundles_sqlalchemy_dialect(self):
        from workbench.backend import serving_package
        spec = {
            "source_platform": "snowflake", "target_platform": "postgres",
            "target_schema": "public", "write_disposition": "replace",
            "datasets": [{"target_table": "t", "select_sql": "SELECT 1"}],
        }
        dest = serving_package.assemble_transfer_package(project_code="sf-src-pkg", spec=spec)
        try:
            reqs = (dest / "requirements.txt").read_text()
            assert "snowflake-sqlalchemy" in reqs   # the source driver dialect
            env = (dest / ".env.example").read_text()
            assert "WB_SOURCE_ACCOUNT" in env and "WB_SOURCE_WAREHOUSE" in env
        finally:
            shutil.rmtree(dest, ignore_errors=True)

    def test_assemble_writes_runnable_dlt_package(self):
        from workbench.backend import serving_package
        code = "test-transfer-pkg"
        spec = {
            "source_platform": "mysql", "target_platform": "databricks",
            "target_catalog": "workspace", "target_schema": "default",
            "write_disposition": "replace",
            "datasets": [{"target_table": "employees",
                          "select_sql": "SELECT employee_id FROM hr_core.employee",
                          "write_disposition": "replace"}],
        }
        dest = serving_package.assemble_transfer_package(project_code=code, spec=spec,
                                                         product_name="Customer Master")
        try:
            assert (dest / "run.py").exists()
            assert (dest / "transfer.json").exists()
            assert (dest / "requirements.txt").exists()
            assert (dest / ".env.example").exists()
            assert (dest / "README.md").exists()
            import json
            saved = json.loads((dest / "transfer.json").read_text())
            assert saved["target_platform"] == "databricks"
            assert saved["datasets"][0]["select_sql"].startswith("SELECT")
            # dlt runner, no duckdb engine.
            assert "import duckdb" not in (dest / "run.py").read_text()
            reqs = (dest / "requirements.txt").read_text()
            assert "dlt" in reqs
        finally:
            shutil.rmtree(dest.parent.parent, ignore_errors=True)


class TestTransferWiring:
    def test_stage_registered_non_llm(self):
        from workbench.backend.archetypes import STAGE_REGISTRY
        s = STAGE_REGISTRY["serving_transfer"]
        assert s["requires_llm"] is False and s["has_review"] is False

    def test_depends_on_configure_serving(self):
        from workbench.backend.archetypes import DEPENDENCY_GRAPH
        # Build Transfer depends on Configure Serving AND the placement config
        # (so the placement decision exists before the pipeline is generated).
        assert DEPENDENCY_GRAPH["serving_transfer"] == [
            "configure_serving", "configure_transfer_placement",
        ]

    def test_serving_exclusive_group_member(self):
        from workbench.backend.archetypes import _WF_INTEGRATION, _WF_PRODUCT_MATERIALIZATION_SA
        for wf in (_WF_INTEGRATION, _WF_PRODUCT_MATERIALIZATION_SA):
            m = next((s for s in wf["stages"] if s["stage_id"] == "serving_transfer"), None)
            assert m is not None and m["exclusive_group"] == "serving" and m["enabled"] is False

    def test_transfer_has_coupled_deploy(self):
        """Configure → Build → Run: the Build member (serving_transfer) carries a
        coupled Run/Deploy (deploy_transfer), like the other serving modes."""
        from workbench.backend.archetypes import (
            EXCLUSIVE_GROUP_DEPENDENTS, DEPENDENCY_GRAPH, STAGE_REGISTRY,
        )
        # deploy_transfer (Run) + configure_transfer_placement (secondary config)
        # both ride the transfer Build member in the exclusive group.
        assert EXCLUSIVE_GROUP_DEPENDENTS["serving"]["serving_transfer"] == [
            "deploy_transfer", "configure_transfer_placement",
        ]
        assert DEPENDENCY_GRAPH["deploy_transfer"] == ["serving_transfer"]
        d = STAGE_REGISTRY["deploy_transfer"]
        assert d["requires_llm"] is False and d["name"] == "Run Transfer"

    def test_placement_stage_gated_to_transfer(self):
        """configure_transfer_placement rides serving_transfer (cross-platform gate
        for free) and sits before Build in both split templates."""
        from workbench.backend.archetypes import (
            EXCLUSIVE_GROUP_DEPENDENTS, STAGE_REGISTRY,
            _WF_INTEGRATION, _WF_PRODUCT_MATERIALIZATION_SA,
        )
        assert "configure_transfer_placement" in (
            EXCLUSIVE_GROUP_DEPENDENTS["serving"]["serving_transfer"]
        )
        p = STAGE_REGISTRY["configure_transfer_placement"]
        assert p["requires_llm"] is False and p["skill"] is None
        for wf in (_WF_INTEGRATION, _WF_PRODUCT_MATERIALIZATION_SA):
            ids = [s["stage_id"] for s in wf["stages"]]
            assert ids.index("configure_transfer_placement") < ids.index("serving_transfer")
            row = next(s for s in wf["stages"] if s["stage_id"] == "configure_transfer_placement")
            assert row["enabled"] is False

    def test_deploy_transfer_in_split_templates(self):
        from workbench.backend.archetypes import _WF_INTEGRATION, _WF_PRODUCT_MATERIALIZATION_SA
        for wf in (_WF_INTEGRATION, _WF_PRODUCT_MATERIALIZATION_SA):
            ids = [s["stage_id"] for s in wf["stages"]]
            assert "deploy_transfer" in ids
