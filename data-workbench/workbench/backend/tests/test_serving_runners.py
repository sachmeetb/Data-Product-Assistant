"""Tests for the deploy runner + its portable core (_deploy_core.py).

- classify_ddl accepts CREATE/DROP VIEW, rejects anything else
- the runner's MySQL header rewrite is byte-identical to sql_executor's (divergence guard)
- apply_ddl fast-fails unknown platforms + gated DDL
- the Postgres view-recreate recovery fires on the exact drift phrases (fake driver)
- run_deploy.py surfaces connection/ddl-missing failures cleanly via run_result.json
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
import types
from pathlib import Path

import pytest

from workbench.backend import serving_runtime as sr
from workbench.backend import sql_executor


RUNNERS = sr.SERVING_RUNNERS_DIR


def _load_deploy_core():
    spec = importlib.util.spec_from_file_location(
        "_dc_under_test", RUNNERS / "_deploy_core.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dc = _load_deploy_core()


# ── DDL gate ────────────────────────────────────────────────────────────────


def test_classify_accepts_create_and_drop_view():
    assert dc.classify_ddl('CREATE OR REPLACE VIEW "public"."vw_x" AS SELECT 1') is None
    assert dc.classify_ddl('DROP VIEW IF EXISTS "public"."vw_x"') is None


def test_classify_rejects_non_view():
    assert dc.classify_ddl("DROP TABLE users") is not None
    assert dc.classify_ddl("") is not None


# ── MySQL header rewrite parity ────────────────────────────────────────────────


@pytest.mark.parametrize("ddl", [
    'CREATE VIEW "public"."vw_employees" AS SELECT 1',
    'CREATE OR REPLACE VIEW public.vw_x AS SELECT 1',
    'CREATE VIEW `db`.`t` AS SELECT 1',
])
def test_mysql_header_rewrite_matches_sql_executor(ddl):
    assert dc.rewrite_ddl_header_for_mysql(ddl) == \
        sql_executor._rewrite_ddl_header_for_mysql(ddl)


# ── apply_ddl guards ────────────────────────────────────────────────────────


def test_apply_unknown_platform():
    r = dc.apply_ddl("CREATE VIEW public.v AS SELECT 1", "oracle", {}, "public", ["v"])
    assert r["status"] == "failed"
    assert r["error_class"] == "platform_not_supported"


def test_apply_gate_rejects():
    r = dc.apply_ddl("DROP TABLE t", "postgres", {"host": "h"}, "public", [])
    assert r["status"] == "failed"
    assert r["error_class"] == "ddl_rejected"


# ── Postgres view-recreate recovery (fake driver) ──────────────────────────────


def _install_fake_psycopg2(monkeypatch, *, drift_on_first: bool):
    """A minimal fake psycopg2 whose first connection raises the view-drift
    error on the main DDL, and whose second (recovery) connection succeeds."""
    state = {"conn_count": 0}

    class FakeError(Exception):
        pass

    class OperationalError(FakeError):
        pass

    class InsufficientPrivilege(FakeError):
        pass

    class QueryCanceled(FakeError):
        pass

    class DependentObjectsStillExist(FakeError):
        pass

    class FakeCursor:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql):
            # Fail only on the big CREATE VIEW of the first connection.
            if (self.conn.idx == 1 and drift_on_first
                    and sql.strip().upper().startswith("CREATE")
                    and "VIEW" in sql.upper() and "SCHEMA" not in sql.upper()):
                raise FakeError("cannot change data type of view column \"x\"")

    class FakeConn:
        def __init__(self, idx):
            self.idx = idx
            self.autocommit = True

        def cursor(self):
            return FakeCursor(self)

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    def connect(*a, **k):
        state["conn_count"] += 1
        return FakeConn(state["conn_count"])

    fake = types.ModuleType("psycopg2")
    fake.Error = FakeError
    fake.OperationalError = OperationalError
    fake.connect = connect
    errs = types.ModuleType("psycopg2.errors")
    errs.InsufficientPrivilege = InsufficientPrivilege
    errs.QueryCanceled = QueryCanceled
    errs.DependentObjectsStillExist = DependentObjectsStillExist
    fake.errors = errs
    monkeypatch.setitem(sys.modules, "psycopg2", fake)
    monkeypatch.setitem(sys.modules, "psycopg2.errors", errs)
    return state


def test_postgres_recovery_fires_on_drift(monkeypatch):
    _install_fake_psycopg2(monkeypatch, drift_on_first=True)
    r = dc.apply_ddl(
        'CREATE OR REPLACE VIEW "public"."vw_x" AS SELECT 1',
        "postgres", {"host": "h", "dbname": "d"}, "public", ["vw_x"])
    assert r["status"] == "deployed"
    assert r["recovery_used"] is True
    assert r["smoke_test_count"] == 1


def test_postgres_happy_path_no_recovery(monkeypatch):
    _install_fake_psycopg2(monkeypatch, drift_on_first=False)
    r = dc.apply_ddl(
        'CREATE OR REPLACE VIEW "public"."vw_x" AS SELECT 1',
        "postgres", {"dsn": "postgresql://x"}, "public", ["vw_x"])
    assert r["status"] == "deployed"
    assert r["recovery_used"] is False


# ── Snowflake native-view apply (fake connector) ────────────────────────────────

_SNOWFLAKE_NS = {
    "platform": "snowflake", "parts": ["catalog", "schema", "relation"],
    "quote_style": "double", "session_catalog_statement": "USE DATABASE {catalog}",
}


def _install_fake_snowflake(monkeypatch, *, fail_views=None):
    """A minimal fake snowflake.connector recording executed SQL; CREATE VIEW
    statements for a name in `fail_views` raise (to exercise partial deploys)."""
    fail_views = set(fail_views or [])
    executed: list[str] = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql):
            executed.append(sql)
            up = sql.strip().upper()
            if up.startswith("CREATE") and "VIEW" in up:
                for v in fail_views:
                    if f'"{v}"' in sql:
                        raise RuntimeError(f"boom creating {v}")

        def close(self):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

        def close(self):
            pass

    connector = types.SimpleNamespace(connect=lambda **kw: FakeConn())
    fake_sf = types.ModuleType("snowflake")
    fake_sf.connector = connector
    monkeypatch.setitem(sys.modules, "snowflake", fake_sf)
    monkeypatch.setitem(sys.modules, "snowflake.connector", connector)
    return executed


def test_view_target_from_stmt_handles_copy_grants():
    assert dc._view_target_from_stmt(
        'CREATE OR REPLACE VIEW "DB"."S"."vw_x" COPY GRANTS AS SELECT 1') == "vw_x"


def test_snowflake_apply_quotes_three_part_namespace(monkeypatch):
    executed = _install_fake_snowflake(monkeypatch)
    ddl = (
        'CREATE OR REPLACE VIEW "DWB_SERVING_DB"."PUBLIC"."vw_a" COPY GRANTS AS SELECT 1;\n\n'
        'CREATE OR REPLACE VIEW "DWB_SERVING_DB"."PUBLIC"."vw_b" COPY GRANTS AS SELECT 2;'
    )
    r = dc.apply_ddl(ddl, "snowflake",
                     {"host": "acct", "user": "u", "password": "pat", "dbname": "DWB_SERVING_DB",
                      "warehouse": "WH"},
                     "DWB_SERVING_DB.PUBLIC", ["vw_a", "vw_b"], namespace=_SNOWFLAKE_NS)
    assert r["status"] == "deployed"
    assert r["smoke_test_count"] == 2
    # Each namespace part is quoted independently (not one dotted identifier).
    assert 'CREATE SCHEMA IF NOT EXISTS "DWB_SERVING_DB"."PUBLIC"' in "\n".join(executed)
    assert any('SELECT 1 FROM "DWB_SERVING_DB"."PUBLIC"."vw_a"' in s for s in executed)


def test_snowflake_requires_database_in_target_namespace(monkeypatch):
    _install_fake_snowflake(monkeypatch)
    r = dc.apply_ddl('CREATE OR REPLACE VIEW "PUBLIC"."vw_a" AS SELECT 1',
                     "snowflake", {"host": "acct"}, "PUBLIC", ["vw_a"], namespace=_SNOWFLAKE_NS)
    assert r["status"] == "failed"
    assert r["error_class"] == "target_namespace_required"


def test_snowflake_partial_deploy_reports_per_view(monkeypatch):
    _install_fake_snowflake(monkeypatch, fail_views={"vw_b"})
    ddl = (
        'CREATE OR REPLACE VIEW "DWB_SERVING_DB"."PUBLIC"."vw_a" COPY GRANTS AS SELECT 1;\n\n'
        'CREATE OR REPLACE VIEW "DWB_SERVING_DB"."PUBLIC"."vw_b" COPY GRANTS AS SELECT 2;'
    )
    r = dc.apply_ddl(ddl, "snowflake",
                     {"host": "acct", "dbname": "DWB_SERVING_DB"},
                     "DWB_SERVING_DB.PUBLIC", ["vw_a", "vw_b"], namespace=_SNOWFLAKE_NS)
    assert r["status"] == "failed"
    assert r["error_class"] == "partial_deploy"
    assert r["details"]["deployed"] == ["vw_a"]
    assert [f["view"] for f in r["details"]["failed"]] == ["vw_b"]


def test_snowflake_accepts_pat_via_token_key(monkeypatch):
    # A PAT registered as extra_config.token must be accepted (parity w/ Databricks).
    captured = {}
    executed = _install_fake_snowflake(monkeypatch)
    import snowflake.connector as _sf  # the fake

    def _connect(**kw):
        captured.update(kw)
        class C:
            def cursor(self):
                class Cur:
                    def __enter__(self): return self
                    def __exit__(self, *a): return False
                    def execute(self, sql): executed.append(sql)
                    def close(self): pass
                return Cur()
            def commit(self): pass
            def close(self): pass
        return C()
    monkeypatch.setattr(_sf, "connect", _connect)
    r = dc.apply_ddl('CREATE OR REPLACE VIEW "DB"."PUBLIC"."vw_a" COPY GRANTS AS SELECT 1',
                     "snowflake", {"host": "acct", "token": "pat-token", "dbname": "DB"},
                     "DB.PUBLIC", ["vw_a"], namespace=_SNOWFLAKE_NS)
    assert r["status"] == "deployed"
    assert captured["password"] == "pat-token"


# ── run_deploy.py end-to-end (no DB) ───────────────────────────────────────────


def _lay_out_view_package(dir_path: Path, ddl: str | None, view_names):
    for f in ("run_deploy.py", "_wb_runresult.py", "_deploy_core.py"):
        dest = dir_path / ("run.py" if f == "run_deploy.py" else f)
        shutil.copyfile(RUNNERS / f, dest)
    import json
    (dir_path / "package.json").write_text(json.dumps({
        "mode": "virtual_view", "platform": "postgres", "view_schema": "public",
        "view_names": view_names, "ddl_file": "view.sql",
    }), encoding="utf-8")
    if ddl is not None:
        (dir_path / "view.sql").write_text(ddl, encoding="utf-8")


def test_run_deploy_no_connection(tmp_path):
    _lay_out_view_package(tmp_path, 'CREATE VIEW "public"."vw_x" AS SELECT 1', ["vw_x"])
    res = sr.execute_package_runner(tmp_path, ["--apply"], env={"WB_TARGET_PLATFORM": "postgres"})
    assert res.status == "failed"
    assert res.error.cls == "connection_error"


def test_run_deploy_missing_ddl(tmp_path):
    _lay_out_view_package(tmp_path, None, ["vw_x"])
    res = sr.execute_package_runner(
        tmp_path, ["--apply"],
        env={"WB_TARGET_PLATFORM": "postgres", "WB_TARGET_DSN": "postgresql://x"})
    assert res.status == "failed"
    assert res.error.cls == "ddl_missing"


# ── run_dbt.py (fake dbt executable, no real dbt needed) ───────────────────────


def _lay_out_dbt_package(dir_path: Path) -> Path:
    for f in ("run_dbt.py", "_wb_runresult.py"):
        shutil.copyfile(RUNNERS / f, dir_path / ("run.py" if f == "run_dbt.py" else f))
    # A fake `dbt` executable that writes a dbt-shaped run_results.json and exits 0.
    fake = dir_path / "fake_dbt.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        "os.makedirs('target', exist_ok=True)\n"
        "json.dump({'results': [{'unique_id': 'model.p.vw_x', 'status': 'success', "
        "'execution_time': 0.1}]}, open('target/run_results.json', 'w'))\n",
        encoding="utf-8")
    fake.chmod(0o755)
    return fake


def test_run_dbt_success(tmp_path):
    # The fake dbt is an executable (shebang + chmod +x); run_dbt invokes it as
    # the `dbt` binary, it writes target/run_results.json, run_dbt normalizes it.
    fake = _lay_out_dbt_package(tmp_path)
    res = sr.execute_package_runner(tmp_path, ["--mode", "full", "--dbt-bin", str(fake)])
    assert res.ok, res.error
    assert res.metrics["run_results"][0]["model"] == "vw_x"
    assert res.metrics["model_count"] == 1


def test_run_dbt_not_found(tmp_path):
    _lay_out_dbt_package(tmp_path)
    res = sr.execute_package_runner(
        tmp_path, ["--mode", "full", "--dbt-bin", "/nonexistent/dbt"])
    assert res.status == "failed"
    assert res.error.cls == "dbt_not_found"
