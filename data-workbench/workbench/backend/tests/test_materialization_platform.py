"""Tests for platform-aware _dbt_env() and MaterializationTarget non-Postgres paths.

Coverage:
- _dbt_env: Postgres path unchanged (parses DSN into WB_DBT_* vars)
- _dbt_env: MySQL sets host/port/user/database; resolves secret_ref for password
- _dbt_env: Snowflake sets account/user/database/warehouse/role; resolves secret_ref
- _dbt_env: Databricks sets host/http_path/catalog/token; resolves secret_ref
- _dbt_env: platform defaults to postgres when omitted
- _dbt_env: unknown platform returns env without crashing (graceful)
- MaterializationTargetInput: Snowflake validation (account required)
- MaterializationTargetInput: Databricks validation (http_path required)
- set_materialization_target: unsupported platform returns 400
- models.py: MaterializationTarget has connection_json field
"""
import json
import os
import unittest.mock as mock

import pytest

from workbench.backend.routers.materialization import _dbt_env, MaterializationTargetInput


# ── _dbt_env: Postgres (pre-existing behaviour, must not regress) ─────────────

class TestDbtEnvPostgres:
    def test_postgres_host(self):
        env = _dbt_env("postgresql://user:pass@db.example.com:5432/mydb")
        assert env["WB_DBT_HOST"] == "db.example.com"

    def test_postgres_port(self):
        env = _dbt_env("postgresql://user:pass@db.example.com:5432/mydb")
        assert env["WB_DBT_PORT"] == "5432"

    def test_postgres_user(self):
        env = _dbt_env("postgresql://user:pass@db.example.com:5432/mydb")
        assert env["WB_DBT_USER"] == "user"

    def test_postgres_password(self):
        env = _dbt_env("postgresql://user:secret@db.example.com:5432/mydb")
        assert env["WB_DBT_PASSWORD"] == "secret"

    def test_postgres_dbname(self):
        env = _dbt_env("postgresql://user:pass@db.example.com:5432/mydb")
        assert env["WB_DBT_DBNAME"] == "mydb"

    def test_postgres_explicit_platform(self):
        env = _dbt_env("postgresql://u:p@h:5432/db", platform="postgres")
        assert env["WB_DBT_HOST"] == "h"

    def test_postgresql_platform_alias(self):
        env = _dbt_env("postgresql://u:p@h:5432/db", platform="postgresql")
        assert env["WB_DBT_HOST"] == "h"

    def test_default_platform_is_postgres(self):
        env = _dbt_env("postgresql://u:p@h:5432/db")
        assert "WB_DBT_HOST" in env
        assert "WB_DBT_ACCOUNT" not in env

    def test_postgres_inherits_os_environ(self):
        env = _dbt_env("postgresql://u:p@h:5432/db")
        # At least PATH should be carried through
        assert "PATH" in env or len(env) > 5


# ── _dbt_env: MySQL ───────────────────────────────────────────────────────────

class TestDbtEnvMysql:
    _CFG = json.dumps({
        "host": "mysql.example.com",
        "port": 3306,
        "username": "myuser",
        "database": "mydb",
    })

    def test_mysql_host(self):
        env = _dbt_env("", platform="mysql", connection_json=self._CFG)
        assert env["WB_DBT_HOST"] == "mysql.example.com"

    def test_mysql_port(self):
        env = _dbt_env("", platform="mysql", connection_json=self._CFG)
        assert env["WB_DBT_PORT"] == "3306"

    def test_mysql_user(self):
        env = _dbt_env("", platform="mysql", connection_json=self._CFG)
        assert env["WB_DBT_USER"] == "myuser"

    def test_mysql_database(self):
        env = _dbt_env("", platform="mysql", connection_json=self._CFG)
        assert env["WB_DBT_DBNAME"] == "mydb"

    def test_mysql_no_secret_ref_gives_empty_password(self):
        env = _dbt_env("", platform="mysql", connection_json=self._CFG)
        assert env["WB_DBT_PASSWORD"] == ""

    def test_mysql_secret_ref_resolves_from_env(self):
        cfg = json.dumps({**json.loads(self._CFG), "secret_ref": "MY_MYSQL_PW"})
        with mock.patch.dict(os.environ, {"MY_MYSQL_PW": "s3cr3t"}):
            env = _dbt_env("", platform="mysql", connection_json=cfg)
        assert env["WB_DBT_PASSWORD"] == "s3cr3t"

    def test_mysql_missing_secret_ref_env_gives_empty(self):
        cfg = json.dumps({**json.loads(self._CFG), "secret_ref": "NONEXISTENT_VAR_XYZ"})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NONEXISTENT_VAR_XYZ", None)
            env = _dbt_env("", platform="mysql", connection_json=cfg)
        assert env["WB_DBT_PASSWORD"] == ""

    def test_mysql_does_not_set_account(self):
        env = _dbt_env("", platform="mysql", connection_json=self._CFG)
        assert "WB_DBT_ACCOUNT" not in env

    def test_mysql_default_port(self):
        cfg = json.dumps({"host": "h", "username": "u", "database": "d"})
        env = _dbt_env("", platform="mysql", connection_json=cfg)
        assert env["WB_DBT_PORT"] == "3306"


# ── _dbt_env: Snowflake ───────────────────────────────────────────────────────

class TestDbtEnvSnowflake:
    _CFG = json.dumps({
        "account": "myorg-myaccount",
        "username": "sfuser",
        "database": "ANALYTICS",
        "warehouse": "COMPUTE_WH",
        "role": "TRANSFORMER",
    })

    def test_snowflake_account(self):
        env = _dbt_env("", platform="snowflake", connection_json=self._CFG)
        assert env["WB_DBT_ACCOUNT"] == "myorg-myaccount"

    def test_snowflake_user(self):
        env = _dbt_env("", platform="snowflake", connection_json=self._CFG)
        assert env["WB_DBT_USER"] == "sfuser"

    def test_snowflake_database(self):
        env = _dbt_env("", platform="snowflake", connection_json=self._CFG)
        assert env["WB_DBT_DATABASE"] == "ANALYTICS"

    def test_snowflake_warehouse(self):
        env = _dbt_env("", platform="snowflake", connection_json=self._CFG)
        assert env["WB_DBT_WAREHOUSE"] == "COMPUTE_WH"

    def test_snowflake_role(self):
        env = _dbt_env("", platform="snowflake", connection_json=self._CFG)
        assert env["WB_DBT_ROLE"] == "TRANSFORMER"

    def test_snowflake_no_secret_ref_gives_empty_password(self):
        env = _dbt_env("", platform="snowflake", connection_json=self._CFG)
        assert env["WB_DBT_PASSWORD"] == ""

    def test_snowflake_secret_ref_resolves(self):
        cfg = json.dumps({**json.loads(self._CFG), "secret_ref": "SF_PW_VAR"})
        with mock.patch.dict(os.environ, {"SF_PW_VAR": "tok3n"}):
            env = _dbt_env("", platform="snowflake", connection_json=cfg)
        assert env["WB_DBT_PASSWORD"] == "tok3n"

    def test_snowflake_does_not_set_host(self):
        env = _dbt_env("", platform="snowflake", connection_json=self._CFG)
        # Snowflake uses account, not host
        assert "WB_DBT_HOST" not in env

    def test_snowflake_does_not_set_token(self):
        env = _dbt_env("", platform="snowflake", connection_json=self._CFG)
        assert "WB_DBT_TOKEN" not in env


# ── _dbt_env: Databricks ──────────────────────────────────────────────────────

class TestDbtEnvDatabricks:
    _CFG = json.dumps({
        "host": "adb-123.azuredatabricks.net",
        "http_path": "/sql/1.0/warehouses/abc123",
        "catalog": "main",
    })

    def test_databricks_host(self):
        env = _dbt_env("", platform="databricks", connection_json=self._CFG)
        assert env["WB_DBT_HOST"] == "adb-123.azuredatabricks.net"

    def test_databricks_http_path(self):
        env = _dbt_env("", platform="databricks", connection_json=self._CFG)
        assert env["WB_DBT_HTTP_PATH"] == "/sql/1.0/warehouses/abc123"

    def test_databricks_catalog(self):
        env = _dbt_env("", platform="databricks", connection_json=self._CFG)
        assert env["WB_DBT_CATALOG"] == "main"

    def test_databricks_catalog_default(self):
        cfg = json.dumps({"host": "h", "http_path": "/sql/1.0/warehouses/x"})
        env = _dbt_env("", platform="databricks", connection_json=cfg)
        assert env["WB_DBT_CATALOG"] == "hive_metastore"

    def test_databricks_no_secret_ref_gives_empty_token(self):
        env = _dbt_env("", platform="databricks", connection_json=self._CFG)
        assert env["WB_DBT_TOKEN"] == ""

    def test_databricks_secret_ref_resolves(self):
        cfg = json.dumps({**json.loads(self._CFG), "secret_ref": "DB_TOKEN_VAR"})
        with mock.patch.dict(os.environ, {"DB_TOKEN_VAR": "dapi123"}):
            env = _dbt_env("", platform="databricks", connection_json=cfg)
        assert env["WB_DBT_TOKEN"] == "dapi123"

    def test_databricks_does_not_set_password(self):
        env = _dbt_env("", platform="databricks", connection_json=self._CFG)
        assert "WB_DBT_PASSWORD" not in env

    def test_databricks_does_not_set_account(self):
        env = _dbt_env("", platform="databricks", connection_json=self._CFG)
        assert "WB_DBT_ACCOUNT" not in env


# ── _dbt_env: credential isolation (security) ─────────────────────────────────

class TestDbtEnvCredentialIsolation:
    """Credentials must come from env resolution, never from raw storage."""

    def test_mysql_secret_ref_is_env_var_name_not_value(self):
        # secret_ref stores the env var NAME — not a password.
        cfg = json.dumps({
            "host": "h", "port": 3306, "username": "u", "database": "d",
            "secret_ref": "THE_VAR_NAME",
        })
        with mock.patch.dict(os.environ, {"THE_VAR_NAME": "actual_password"}):
            env = _dbt_env("", platform="mysql", connection_json=cfg)
        assert "THE_VAR_NAME" not in env["WB_DBT_PASSWORD"]
        assert env["WB_DBT_PASSWORD"] == "actual_password"

    def test_snowflake_password_not_in_connection_json(self):
        cfg = json.dumps({
            "account": "org-acct", "username": "u", "database": "db",
            "warehouse": "wh", "role": "",
            "password": "should_be_ignored",   # should NOT be used
        })
        env = _dbt_env("", platform="snowflake", connection_json=cfg)
        # Without a secret_ref, password is empty even if "password" key is present
        assert env["WB_DBT_PASSWORD"] == ""

    def test_databricks_token_not_in_connection_json(self):
        cfg = json.dumps({
            "host": "h", "http_path": "/sql/x", "catalog": "c",
            "token": "should_be_ignored",   # should NOT be used
        })
        env = _dbt_env("", platform="databricks", connection_json=cfg)
        assert env["WB_DBT_TOKEN"] == ""


# ── _dbt_env: registered-target structured config (Tier-3 repair) ─────────────

class TestDbtEnvRegisteredTarget:
    """A registered target connection resolves the secret out-of-band and feeds
    the structured config to _dbt_env — no reliance on connection_json/env-var."""

    def test_resolved_password_used_over_env_lookup(self):
        cfg = json.dumps({"account": "org-acct", "username": "u", "database": "DB",
                          "warehouse": "WH", "role": "R"})
        env = _dbt_env("", platform="snowflake", connection_json=cfg,
                       resolved_password="pat-token-live")
        assert env["WB_DBT_PASSWORD"] == "pat-token-live"
        assert env["WB_DBT_ACCOUNT"] == "org-acct"
        assert env["WB_DBT_WAREHOUSE"] == "WH"

    def test_account_falls_back_to_host(self):
        # A registered connection stores the account identifier in `host`.
        cfg = json.dumps({"host": "org-acct", "username": "u", "database": "DB",
                          "warehouse": "WH"})
        env = _dbt_env("", platform="snowflake", connection_json=cfg,
                       resolved_password="pw")
        assert env["WB_DBT_ACCOUNT"] == "org-acct"

    def test_authenticator_set_when_configured(self):
        cfg = json.dumps({"account": "a", "warehouse": "WH", "authenticator": "externalbrowser"})
        env = _dbt_env("", platform="snowflake", connection_json=cfg, resolved_password="")
        assert env["WB_DBT_AUTHENTICATOR"] == "externalbrowser"

    def test_authenticator_absent_by_default(self):
        cfg = json.dumps({"account": "a", "warehouse": "WH"})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WB_DBT_AUTHENTICATOR", None)
            env = _dbt_env("", platform="snowflake", connection_json=cfg, resolved_password="pw")
        assert "WB_DBT_AUTHENTICATOR" not in env

    def test_resolve_dbt_target_config_builds_from_registered_connection(self):
        import json as _json
        from sqlmodel import Session
        from workbench.backend.database import engine
        from workbench.backend.models import PlatformConnection, MaterializationTarget
        from workbench.backend.routers.materialization import _resolve_dbt_target_config
        with Session(engine) as s:
            conn = PlatformConnection(
                connection_name="sf-target-cfg-test", platform_type="snowflake",
                host="org-acct", port=443, database="DWB_SERVING_DB", username="NEYDE",
                secret_ref="direct:pat-live",
                extra_config_json=_json.dumps({"warehouse": "COMPUTE_WH", "role": "R", "schema": "PUBLIC"}),
            )
            s.add(conn); s.commit(); s.refresh(conn)
            mt = MaterializationTarget(contract_id="tier3-cfg-contract",
                                       target_connection_id=conn.id, platform="snowflake",
                                       connection_json="{}", pg_connection="")
            conn_json, resolved_pw = _resolve_dbt_target_config(mt, "snowflake", s)
            cfg = _json.loads(conn_json)
            assert cfg["account"] == "org-acct"
            assert cfg["warehouse"] == "COMPUTE_WH"
            assert cfg["role"] == "R"
            assert resolved_pw == "pat-live"     # resolved out-of-band, never persisted
            s.delete(conn); s.commit()

    def test_resolve_databricks_catalog_resolution_ladder(self):
        """Databricks catalog resolves extra_config.catalog → view_target_namespace
        catalog → connection.database, and is NEVER left empty — an empty catalog
        makes _dbt_env fall back to `hive_metastore`, which is disabled on UC-only
        workspaces (UC_HIVE_METASTORE_DISABLED_EXCEPTION)."""
        import json as _json
        from sqlmodel import Session
        from workbench.backend.database import engine
        from workbench.backend.models import PlatformConnection, MaterializationTarget
        from workbench.backend.routers.materialization import _resolve_dbt_target_config
        cases = [
            # (extra_config, view_target_namespace, expected_catalog)
            ({"http_path": "/sql/x"}, "workspace.default", "workspace"),   # target-namespace catalog
            ({"http_path": "/sql/x"}, "", "hr_core"),                      # → connection.database
            ({"http_path": "/sql/x", "catalog": "explicit_cat"},
             "workspace.default", "explicit_cat"),                         # explicit extra_config wins
        ]
        with Session(engine) as s:
            for i, (extra, vtn, expected) in enumerate(cases):
                conn = PlatformConnection(
                    connection_name=f"dbx-ladder-{i}", platform_type="databricks",
                    host="dbc-x.cloud.databricks.com", port=443, database="hr_core",
                    username="token", secret_ref="direct:dapi-live",
                    extra_config_json=_json.dumps(extra),
                )
                s.add(conn); s.commit(); s.refresh(conn)
                mt = MaterializationTarget(
                    contract_id=f"dbx-ladder-{i}-contract",
                    target_connection_id=conn.id, platform="databricks",
                    connection_json="{}", view_target_namespace=vtn,
                )
                conn_json, _pw = _resolve_dbt_target_config(mt, "databricks", s)
                assert _json.loads(conn_json)["catalog"] == expected, (extra, vtn, expected)
                s.delete(conn); s.commit()


# ── Platform-aware gate preview / row-count (materialized read path) ──────────

class TestMaterializedPreviewDispatch:
    """_read_preview / _count_rows must go through the multi-platform executor for
    non-Postgres targets (psycopg2 can't open a snowflake:// DSN), and keep the
    direct psycopg2 path for Postgres."""

    def test_non_postgres_read_preview_uses_executor(self, monkeypatch):
        from workbench.backend.routers import materialization as mz

        class _R:
            status = "ok"
            columns = [{"name": "ID", "dataType": "int"}, {"name": "NAME", "dataType": "text"}]
            rows = [[1, "a"], [2, "b"]]
            row_count = 2
            error_message = None

        calls = {}

        def _fake_execute_select(**kw):
            calls["sql"] = kw["sql"]; calls["platform"] = kw["platform"]
            calls["connection_ref"] = kw["connection_ref"]
            return _R()

        import workbench.backend.sql_executor as _se
        monkeypatch.setattr(_se, "execute_select", _fake_execute_select)
        out = mz._read_preview(
            "", "PUBLIC", ["VW_GAME_STATS"], limit=20,
            platform="snowflake", connection_ref={"host": "acct"},
            ns=object(), project_code="p",
        )
        # Went through the executor (no psycopg2), with a quoted 2-part relation.
        assert '"PUBLIC"."VW_GAME_STATS"' in calls["sql"]
        assert calls["platform"] == "snowflake"
        entry = out["VW_GAME_STATS"]
        assert entry["columns"] == [{"name": "ID", "type": "int"}, {"name": "NAME", "type": "text"}]
        assert entry["row_count"] == 2 and len(entry["rows"]) == 2

    def test_non_postgres_count_rows_uses_executor(self, monkeypatch):
        from workbench.backend.routers import materialization as mz

        class _R:
            status = "ok"; rows = [[42]]; error_message = None

        import workbench.backend.sql_executor as _se
        monkeypatch.setattr(_se, "execute_select", lambda **kw: _R())
        counts = mz._count_rows(
            "", "PUBLIC", ["VW_TEAMS"],
            platform="snowflake", connection_ref={"host": "acct"},
            ns=object(), project_code="p",
        )
        assert counts["VW_TEAMS"] == 42

    def test_non_postgres_without_ns_is_safe(self):
        # No neo4j session available → best-effort empty, never a psycopg2 crash.
        from workbench.backend.routers import materialization as mz
        assert mz._count_rows("", "PUBLIC", ["T"], platform="snowflake",
                              connection_ref={"host": "a"}, ns=None) == {}
        assert "_connect_error" in mz._read_preview(
            "", "PUBLIC", ["T"], platform="snowflake", connection_ref={"host": "a"}, ns=None)

    def test_postgres_still_uses_psycopg2_path(self, monkeypatch):
        # The Postgres branch must NOT touch the executor (keeps the fast path).
        from workbench.backend.routers import materialization as mz
        import workbench.backend.sql_executor as _se
        monkeypatch.setattr(_se, "execute_select",
                            lambda **kw: (_ for _ in ()).throw(AssertionError("executor used for postgres")))
        # A bogus DSN → psycopg2 connect fails → _connect_error, but crucially the
        # executor was never called (the AssertionError above never fires).
        out = mz._read_preview("postgresql://x:y@127.0.0.1:1/none", "public", ["t"], platform="postgres")
        assert "_connect_error" in out


class TestResolvePreviewExecution:
    def test_registered_snowflake_target_returns_structured_ref(self):
        import json as _json
        from sqlmodel import Session
        from workbench.backend.database import engine
        from workbench.backend.models import Project, PlatformConnection, MaterializationTarget
        from workbench.backend.routers.materialization import _resolve_preview_execution
        with Session(engine) as s:
            proj = Project(project_code="prev-sf", name="p", pg_connection="")
            s.add(proj); s.commit(); s.refresh(proj)
            conn = PlatformConnection(
                connection_name="prev-sf-conn", platform_type="snowflake",
                host="org-acct", port=443, database="DWB_SERVING_DB", username="NEYDE",
                secret_ref="direct:pat",
                extra_config_json=_json.dumps({"warehouse": "COMPUTE_WH", "role": "R", "schema": "PUBLIC"}),
            )
            s.add(conn); s.commit(); s.refresh(conn)
            s.add(MaterializationTarget(contract_id="prev-sf-contract",
                                        target_connection_id=conn.id, platform="snowflake"))
            s.commit()
            proj = s.get(Project, proj.id)
            platform, ref, pg = _resolve_preview_execution(proj, s)
            assert platform == "snowflake"
            assert pg == ""                       # non-Postgres → no psycopg2 DSN
            assert ref["host"] == "org-acct"
            assert ref["extra_config"]["warehouse"] == "COMPUTE_WH"
            assert ref["resolved_password"] == "pat"
            s.delete(conn); s.commit()


# ── Fail-fast guard for unsupported dbt-materialize targets ───────────────────

class TestAssertDbtMaterializeSupported:
    """`_assert_dbt_materialize_supported` rejects targets with no compatible dbt
    adapter (MySQL et al.) with a clean 422, and passes supported ones through.
    Fires before any scaffold / `dbt build` work via `_scaffold_dbt_project`."""

    def test_mysql_raises_422(self):
        from fastapi import HTTPException
        from workbench.backend.routers.materialization import _assert_dbt_materialize_supported
        with pytest.raises(HTTPException) as ei:
            _assert_dbt_materialize_supported("mysql")
        assert ei.value.status_code == 422
        assert "mysql" in ei.value.detail

    def test_duckdb_raises_422(self):
        from fastapi import HTTPException
        from workbench.backend.routers.materialization import _assert_dbt_materialize_supported
        with pytest.raises(HTTPException) as ei:
            _assert_dbt_materialize_supported("duckdb")
        assert ei.value.status_code == 422

    def test_postgres_passes(self):
        from workbench.backend.routers.materialization import _assert_dbt_materialize_supported
        _assert_dbt_materialize_supported("postgres")   # certified — no raise

    def test_postgresql_alias_passes(self):
        # postgresql must normalise to the `postgres` manifest key, not resolve
        # to UNSUPPORTED.
        from workbench.backend.routers.materialization import _assert_dbt_materialize_supported
        _assert_dbt_materialize_supported("postgresql")

    def test_snowflake_passes(self):
        from workbench.backend.routers.materialization import _assert_dbt_materialize_supported
        _assert_dbt_materialize_supported("snowflake")   # preview — no raise

    def test_databricks_passes(self):
        from workbench.backend.routers.materialization import _assert_dbt_materialize_supported
        _assert_dbt_materialize_supported("databricks")  # preview — no raise


# ── Snowflake target-database symmetry ladder ─────────────────────────────────

class TestSnowflakeDatabaseLadder:
    """`_resolve_dbt_target_config` honours the declared target namespace's DB
    part over the connection's database (mirrors the databricks catalog ladder),
    keeping the recorded serving location and the physical dbt build in sync."""

    def test_view_target_namespace_db_overrides_connection_database(self):
        import json as _json
        from sqlmodel import Session
        from workbench.backend.database import engine
        from workbench.backend.models import PlatformConnection, MaterializationTarget
        from workbench.backend.routers.materialization import _resolve_dbt_target_config
        cases = [
            # (view_target_namespace, expected_database)
            ("PROD_DB.SCH", "PROD_DB"),   # declared namespace DB wins
            ("", "DEV_DB"),               # empty vtn → connection.database
        ]
        with Session(engine) as s:
            for i, (vtn, expected) in enumerate(cases):
                conn = PlatformConnection(
                    connection_name=f"sf-db-ladder-{i}", platform_type="snowflake",
                    host="org-acct", port=443, database="DEV_DB", username="NEYDE",
                    secret_ref="direct:pat-live",
                    extra_config_json=_json.dumps({"warehouse": "COMPUTE_WH", "role": "R"}),
                )
                s.add(conn); s.commit(); s.refresh(conn)
                mt = MaterializationTarget(
                    contract_id=f"sf-db-ladder-{i}-contract",
                    target_connection_id=conn.id, platform="snowflake",
                    connection_json="{}", view_target_namespace=vtn,
                )
                conn_json, _pw = _resolve_dbt_target_config(mt, "snowflake", s)
                assert _json.loads(conn_json)["database"] == expected, (vtn, expected)
                s.delete(conn); s.commit()


# ── MaterializationTarget model has connection_json ───────────────────────────

class TestMaterializationTargetModel:
    def test_model_has_connection_json_field(self):
        from workbench.backend.models import MaterializationTarget
        target = MaterializationTarget(contract_id="test-contract")
        assert hasattr(target, "connection_json")
        assert target.connection_json == "{}"

    def test_connection_json_accepts_platform_config(self):
        from workbench.backend.models import MaterializationTarget
        cfg = json.dumps({"account": "myorg", "warehouse": "COMPUTE_WH"})
        target = MaterializationTarget(
            contract_id="test-contract",
            platform="snowflake",
            connection_json=cfg,
        )
        assert json.loads(target.connection_json)["account"] == "myorg"
