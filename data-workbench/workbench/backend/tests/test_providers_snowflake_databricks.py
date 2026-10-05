"""Tests for Snowflake and Databricks platform providers.

Covers:
  - SnowflakeConnectionProvider.validate_config: required fields
  - DatabricksConnectionProvider.validate_config: http_path required
  - SnowflakeConnectionProvider.probe: mock snowflake.connector.connect
  - DatabricksConnectionProvider.probe: mock databricks.sql.connect
  - resolve_source_connection_ref with Snowflake binding: extra_config present
  - DISCOVERY_SKILL_BY_PLATFORM / PROFILING_SKILL_BY_PLATFORM contain new platforms
  - stage_execution _PLATFORM_SKILL_OVERRIDES contains new platform entries
"""
from __future__ import annotations

import sys
import types
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from workbench.backend.main import app
from workbench.backend.database import engine
from workbench.backend.models import PlatformConnection, SourceBinding, Project

_client = TestClient(app)


# ── helpers: inject stub modules for optional connectors ─────────────────────

def _make_snowflake_stub(mock_connect):
    """Return a fake snowflake package hierarchy with a mocked connector.connect."""
    snowflake_pkg = types.ModuleType("snowflake")
    connector_mod = types.ModuleType("snowflake.connector")
    connector_mod.connect = mock_connect
    snowflake_pkg.connector = connector_mod
    return {"snowflake": snowflake_pkg, "snowflake.connector": connector_mod}


def _make_databricks_stub(mock_connect):
    """Return a fake databricks.sql module with a mocked connect."""
    databricks_pkg = types.ModuleType("databricks")
    sql_mod = types.ModuleType("databricks.sql")
    sql_mod.connect = mock_connect
    databricks_pkg.sql = sql_mod
    return {"databricks": databricks_pkg, "databricks.sql": sql_mod}


@pytest.fixture(scope="module")
def client():
    return _client


@pytest.fixture
def session():
    with Session(engine) as s:
        yield s


@pytest.fixture(autouse=True)
def _clean_tables():
    """Remove PlatformConnection + SourceBinding rows before each test."""
    with Session(engine) as s:
        for row in s.exec(select(SourceBinding)).all():
            s.delete(row)
        for row in s.exec(select(PlatformConnection)).all():
            s.delete(row)
        s.commit()
    yield


def _make_project(session, code: str) -> Project:
    p = session.exec(
        select(Project).where(Project.project_code == code)
    ).first()
    if p is None:
        p = Project(
            project_code=code,
            name="Test Project",
            pg_connection="postgresql://user:pass@localhost/testdb",
        )
        session.add(p)
        session.commit()
        session.refresh(p)
    return p


# ══════════════════════════════════════════════════════════════════════════════
# 1. SnowflakeConnectionProvider.validate_config
# ══════════════════════════════════════════════════════════════════════════════

class TestSnowflakeValidateConfig:
    def _provider(self):
        from workbench.backend.platform.providers.snowflake import SnowflakeConnectionProvider
        return SnowflakeConnectionProvider()

    def test_valid_config(self):
        p = self._provider()
        report = p.validate_config(
            {
                "host": "myorg.us-east-1.snowflakecomputing.com",
                "database": "MYDB",
                "extra_config": {"warehouse": "COMPUTE_WH"},
            },
            "env:SNOWFLAKE_PASSWORD",
        )
        assert report.valid is True
        assert report.errors == []

    def test_missing_host(self):
        p = self._provider()
        report = p.validate_config(
            {
                "database": "MYDB",
                "extra_config": {"warehouse": "WH"},
            },
            "env:SNOWFLAKE_PASSWORD",
        )
        assert report.valid is False
        assert any("host" in e for e in report.errors)

    def test_missing_database(self):
        p = self._provider()
        report = p.validate_config(
            {
                "host": "myaccount.snowflakecomputing.com",
                "extra_config": {"warehouse": "WH"},
            },
            "env:SNOWFLAKE_PASSWORD",
        )
        assert report.valid is False
        assert any("database" in e for e in report.errors)

    def test_missing_warehouse(self):
        p = self._provider()
        report = p.validate_config(
            {
                "host": "myaccount.snowflakecomputing.com",
                "database": "MYDB",
                "extra_config": {},
            },
            "env:SNOWFLAKE_PASSWORD",
        )
        assert report.valid is False
        assert any("warehouse" in e for e in report.errors)

    def test_missing_secret_ref(self):
        p = self._provider()
        report = p.validate_config(
            {
                "host": "myaccount.snowflakecomputing.com",
                "database": "MYDB",
                "extra_config": {"warehouse": "WH"},
            },
            "",  # empty secret_ref
        )
        assert report.valid is False
        assert any("secret_ref" in e for e in report.errors)

    def test_multiple_errors_reported(self):
        p = self._provider()
        report = p.validate_config({}, "")
        assert report.valid is False
        assert len(report.errors) >= 3   # host, database, warehouse, secret_ref


# ══════════════════════════════════════════════════════════════════════════════
# 2. DatabricksConnectionProvider.validate_config
# ══════════════════════════════════════════════════════════════════════════════

class TestDatabricksValidateConfig:
    def _provider(self):
        from workbench.backend.platform.providers.databricks import DatabricksConnectionProvider
        return DatabricksConnectionProvider()

    def test_valid_config(self):
        p = self._provider()
        report = p.validate_config(
            {
                "host": "adb-1234567890.12.azuredatabricks.net",
                "extra_config": {"http_path": "/sql/1.0/warehouses/abc123"},
            },
            "env:DATABRICKS_TOKEN",
        )
        assert report.valid is True
        assert report.errors == []

    def test_missing_host(self):
        p = self._provider()
        report = p.validate_config(
            {"extra_config": {"http_path": "/sql/1.0/warehouses/abc123"}},
            "env:DATABRICKS_TOKEN",
        )
        assert report.valid is False
        assert any("host" in e for e in report.errors)

    def test_missing_http_path(self):
        p = self._provider()
        report = p.validate_config(
            {
                "host": "adb-1234567890.12.azuredatabricks.net",
                "extra_config": {},
            },
            "env:DATABRICKS_TOKEN",
        )
        assert report.valid is False
        assert any("http_path" in e for e in report.errors)

    def test_missing_secret_ref(self):
        p = self._provider()
        report = p.validate_config(
            {
                "host": "adb-1234567890.12.azuredatabricks.net",
                "extra_config": {"http_path": "/sql/1.0/warehouses/abc123"},
            },
            "",
        )
        assert report.valid is False
        assert any("secret_ref" in e for e in report.errors)

    def test_multiple_errors_reported(self):
        p = self._provider()
        report = p.validate_config({}, "")
        assert report.valid is False
        assert len(report.errors) >= 2   # host, http_path, secret_ref


# ══════════════════════════════════════════════════════════════════════════════
# 3. SnowflakeConnectionProvider.probe (mocked connector)
# ══════════════════════════════════════════════════════════════════════════════

class TestSnowflakeProbe:
    def test_successful_probe_returns_ok(self):
        from workbench.backend.platform.providers.snowflake import SnowflakeConnectionProvider

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = ("8.10.0",)
        mock_conn.cursor.return_value = mock_cur
        mock_connect = MagicMock(return_value=mock_conn)

        with patch.dict(sys.modules, _make_snowflake_stub(mock_connect)):
            provider = SnowflakeConnectionProvider()
            connection_ref = {
                "connection_id": "test-sf-1",
                "host": "myaccount.snowflakecomputing.com",
                "username": "testuser",
                "resolved_password": "s3cr3t",
                "database": "MYDB",
                "extra_config": {"warehouse": "COMPUTE_WH"},
            }
            evidence = provider.probe(connection_ref)

        # Probe succeeded — no warnings.
        assert evidence.warnings == []
        assert evidence.server_version == "8.10.0"
        assert evidence.platform_type == "snowflake"
        assert evidence.instance_id == "test-sf-1"

        # The connector must be called with account= (not host=).
        call_kwargs = mock_connect.call_args[1]
        assert call_kwargs["account"] == "myaccount.snowflakecomputing.com"
        assert call_kwargs["user"] == "testuser"
        assert call_kwargs["warehouse"] == "COMPUTE_WH"

        # resolved_password maps to "password" in kwargs —
        # verify the key name so we can assert it never appears in log calls.
        assert "password" in call_kwargs
        assert call_kwargs["password"] == "s3cr3t"

        # Connection must be closed.
        mock_conn.close.assert_called_once()

    def test_probe_connection_failure_returns_warning(self):
        from workbench.backend.platform.providers.snowflake import SnowflakeConnectionProvider

        mock_connect = MagicMock(side_effect=Exception("Auth failed"))
        with patch.dict(sys.modules, _make_snowflake_stub(mock_connect)):
            provider = SnowflakeConnectionProvider()
            evidence = provider.probe({
                "connection_id": "sf-bad",
                "host": "bad.snowflakecomputing.com",
                "username": "u",
                "resolved_password": "p",
                "database": "DB",
                "extra_config": {"warehouse": "WH"},
            })

        assert len(evidence.warnings) == 1
        assert "probe failed" in evidence.warnings[0]
        assert "Auth failed" in evidence.warnings[0]

    def test_probe_resolved_password_not_logged(self, caplog):
        """resolved_password must not appear in any logger.warning output."""
        import logging
        from workbench.backend.platform.providers.snowflake import SnowflakeConnectionProvider

        mock_connect = MagicMock(side_effect=Exception("network error"))
        with patch.dict(sys.modules, _make_snowflake_stub(mock_connect)):
            with caplog.at_level(logging.WARNING, logger="workbench.backend.platform.providers.snowflake"):
                provider = SnowflakeConnectionProvider()
                provider.probe({
                    "connection_id": "sf-nolog",
                    "host": "h",
                    "username": "u",
                    "resolved_password": "SUPER_SECRET_TOKEN_XYZ",
                    "database": "DB",
                    "extra_config": {"warehouse": "WH"},
                })

        # The secret must not appear in any log record.
        for record in caplog.records:
            assert "SUPER_SECRET_TOKEN_XYZ" not in record.getMessage()

    def test_probe_role_optional_extra(self):
        """role extra_config is passed to the connector when present."""
        from workbench.backend.platform.providers.snowflake import SnowflakeConnectionProvider

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = ("8.10.0",)
        mock_conn.cursor.return_value = mock_cur
        mock_connect = MagicMock(return_value=mock_conn)

        with patch.dict(sys.modules, _make_snowflake_stub(mock_connect)):
            provider = SnowflakeConnectionProvider()
            provider.probe({
                "connection_id": "sf-role",
                "host": "acct.snowflakecomputing.com",
                "username": "u",
                "resolved_password": "p",
                "database": "DB",
                "extra_config": {"warehouse": "WH", "role": "ANALYST"},
            })

        call_kwargs = mock_connect.call_args[1]
        assert call_kwargs.get("role") == "ANALYST"

    def test_probe_no_role_extra_omitted(self):
        """role must be absent from kwargs when not provided (no empty-string injection)."""
        from workbench.backend.platform.providers.snowflake import SnowflakeConnectionProvider

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = ("8.10.0",)
        mock_conn.cursor.return_value = mock_cur
        mock_connect = MagicMock(return_value=mock_conn)

        with patch.dict(sys.modules, _make_snowflake_stub(mock_connect)):
            provider = SnowflakeConnectionProvider()
            provider.probe({
                "connection_id": "sf-norole",
                "host": "acct.snowflakecomputing.com",
                "username": "u",
                "resolved_password": "p",
                "database": "DB",
                "extra_config": {"warehouse": "WH"},
            })

        call_kwargs = mock_connect.call_args[1]
        assert "role" not in call_kwargs


# ══════════════════════════════════════════════════════════════════════════════
# 4. DatabricksConnectionProvider.probe (mocked connector)
# ══════════════════════════════════════════════════════════════════════════════

class TestDatabricksProbe:
    def _make_cursor(self):
        mock_cur = MagicMock()
        mock_cur.__enter__ = lambda s: s
        mock_cur.__exit__ = MagicMock(return_value=False)
        return mock_cur

    def test_successful_probe_returns_ok(self):
        from workbench.backend.platform.providers.databricks import DatabricksConnectionProvider

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = self._make_cursor()
        mock_connect = MagicMock(return_value=mock_conn)

        with patch.dict(sys.modules, _make_databricks_stub(mock_connect)):
            provider = DatabricksConnectionProvider()
            connection_ref = {
                "connection_id": "test-dbx-1",
                "host": "adb-1234567890.12.azuredatabricks.net",
                "resolved_password": "dapi_TOKEN_XYZ",
                "extra_config": {"http_path": "/sql/1.0/warehouses/abc123"},
            }
            evidence = provider.probe(connection_ref)

        assert evidence.warnings == []
        assert evidence.platform_type == "databricks"
        assert evidence.instance_id == "test-dbx-1"

        call_kwargs = mock_connect.call_args[1]
        assert call_kwargs["server_hostname"] == "adb-1234567890.12.azuredatabricks.net"
        assert call_kwargs["http_path"] == "/sql/1.0/warehouses/abc123"
        assert call_kwargs["access_token"] == "dapi_TOKEN_XYZ"

        mock_conn.close.assert_called_once()

    def test_probe_connection_failure_returns_warning(self):
        from workbench.backend.platform.providers.databricks import DatabricksConnectionProvider

        mock_connect = MagicMock(side_effect=Exception("Token expired"))
        with patch.dict(sys.modules, _make_databricks_stub(mock_connect)):
            provider = DatabricksConnectionProvider()
            evidence = provider.probe({
                "connection_id": "dbx-bad",
                "host": "bad.azuredatabricks.net",
                "resolved_password": "dapi_expired",
                "extra_config": {"http_path": "/sql/1.0/warehouses/xyz"},
            })

        assert len(evidence.warnings) == 1
        assert "probe failed" in evidence.warnings[0]
        assert "Token expired" in evidence.warnings[0]

    def test_probe_resolved_password_not_logged(self, caplog):
        """access_token (resolved_password) must not appear in any logger.warning output."""
        import logging
        from workbench.backend.platform.providers.databricks import DatabricksConnectionProvider

        mock_connect = MagicMock(side_effect=Exception("net error"))
        with patch.dict(sys.modules, _make_databricks_stub(mock_connect)):
            with caplog.at_level(logging.WARNING, logger="workbench.backend.platform.providers.databricks"):
                provider = DatabricksConnectionProvider()
                provider.probe({
                    "connection_id": "dbx-nolog",
                    "host": "h.azuredatabricks.net",
                    "resolved_password": "DATABRICKS_SECRET_TOKEN_ABC",
                    "extra_config": {"http_path": "/sql/1.0/warehouses/x"},
                })

        for record in caplog.records:
            assert "DATABRICKS_SECRET_TOKEN_ABC" not in record.getMessage()

    def test_probe_catalog_optional_extra(self):
        """catalog extra_config is passed to the connector when present."""
        from workbench.backend.platform.providers.databricks import DatabricksConnectionProvider

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = self._make_cursor()
        mock_connect = MagicMock(return_value=mock_conn)

        with patch.dict(sys.modules, _make_databricks_stub(mock_connect)):
            provider = DatabricksConnectionProvider()
            provider.probe({
                "connection_id": "dbx-cat",
                "host": "h.azuredatabricks.net",
                "resolved_password": "tok",
                "extra_config": {
                    "http_path": "/sql/1.0/warehouses/abc",
                    "catalog": "unity_prod",
                },
            })

        call_kwargs = mock_connect.call_args[1]
        assert call_kwargs.get("catalog") == "unity_prod"

    def test_probe_no_catalog_omitted(self):
        """catalog must be absent from kwargs when not provided."""
        from workbench.backend.platform.providers.databricks import DatabricksConnectionProvider

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = self._make_cursor()
        mock_connect = MagicMock(return_value=mock_conn)

        with patch.dict(sys.modules, _make_databricks_stub(mock_connect)):
            provider = DatabricksConnectionProvider()
            provider.probe({
                "connection_id": "dbx-nocat",
                "host": "h.azuredatabricks.net",
                "resolved_password": "tok",
                "extra_config": {"http_path": "/sql/1.0/warehouses/abc"},
            })

        call_kwargs = mock_connect.call_args[1]
        assert "catalog" not in call_kwargs


# ══════════════════════════════════════════════════════════════════════════════
# 5. resolve_source_connection_ref with Snowflake binding — extra_config present
# ══════════════════════════════════════════════════════════════════════════════

class TestResolveSourceConnectionRefSnowflake:
    def test_snowflake_binding_includes_extra_config(self, client, session):
        from workbench.backend.routers.connections import resolve_source_connection_ref

        project = _make_project(session, "sf-resolve-proj")

        cr = client.post("/api/connections", json={
            "connection_name": "sf-resolve-conn",
            "platform_type": "snowflake",
            "host": "myorg.us-east-1.snowflakecomputing.com",
            "database": "MYDB",
            "username": "myuser",
            "secret_ref": "env:SF_PASSWORD",
            "extra_config": {
                "warehouse": "COMPUTE_WH",
                "role": "ANALYST",
                "schema": "PUBLIC",
            },
        })
        assert cr.status_code == 200, cr.text
        conn_id = cr.json()["id"]

        client.put(f"/api/projects/{project.id}/source-binding", json={
            "connection_id": conn_id,
            "default_schema": "PUBLIC",
        })

        session.expire_all()
        project = session.get(Project, project.id)
        platform_type, ref = resolve_source_connection_ref(project, session)

        assert platform_type == "snowflake"
        assert ref["host"] == "myorg.us-east-1.snowflakecomputing.com"
        assert ref["database"] == "MYDB"
        assert "extra_config" in ref
        assert ref["extra_config"]["warehouse"] == "COMPUTE_WH"
        assert ref["extra_config"]["role"] == "ANALYST"
        assert ref["extra_config"]["schema"] == "PUBLIC"
        # Password must not be returned under a "password" key — resolved_password only.
        assert "password" not in ref
        assert "resolved_password" in ref

    def test_databricks_binding_includes_extra_config(self, client, session):
        from workbench.backend.routers.connections import resolve_source_connection_ref

        project = _make_project(session, "dbx-resolve-proj")

        cr = client.post("/api/connections", json={
            "connection_name": "dbx-resolve-conn",
            "platform_type": "databricks",
            "host": "adb-1234567890.12.azuredatabricks.net",
            "database": "",
            "username": "",
            "secret_ref": "env:DBX_TOKEN",
            "extra_config": {
                "http_path": "/sql/1.0/warehouses/abc123",
                "catalog": "unity_prod",
            },
        })
        assert cr.status_code == 200, cr.text
        conn_id = cr.json()["id"]

        client.put(f"/api/projects/{project.id}/source-binding", json={
            "connection_id": conn_id,
        })

        session.expire_all()
        project = session.get(Project, project.id)
        platform_type, ref = resolve_source_connection_ref(project, session)

        assert platform_type == "databricks"
        assert "extra_config" in ref
        assert ref["extra_config"]["http_path"] == "/sql/1.0/warehouses/abc123"
        assert ref["extra_config"]["catalog"] == "unity_prod"

    def test_mysql_binding_extra_config_also_present(self, client, session):
        """Regression: existing MySQL path still returns extra_config (empty dict)."""
        from workbench.backend.routers.connections import resolve_source_connection_ref

        project = _make_project(session, "mysql-extra-proj")

        cr = client.post("/api/connections", json={
            "connection_name": "mysql-extra-conn",
            "platform_type": "mysql",
            "host": "mysql.internal",
            "port": 3306,
            "database": "mydb",
            "username": "reader",
        })
        conn_id = cr.json()["id"]
        client.put(f"/api/projects/{project.id}/source-binding",
                   json={"connection_id": conn_id})

        session.expire_all()
        project = session.get(Project, project.id)
        platform_type, ref = resolve_source_connection_ref(project, session)

        assert platform_type == "mysql"
        assert "extra_config" in ref
        assert isinstance(ref["extra_config"], dict)


# ══════════════════════════════════════════════════════════════════════════════
# 6. Skill maps contain Snowflake and Databricks
# ══════════════════════════════════════════════════════════════════════════════

class TestSkillMaps:
    def test_discovery_skill_map_snowflake(self):
        from workbench.backend.routers.connections import DISCOVERY_SKILL_BY_PLATFORM
        assert DISCOVERY_SKILL_BY_PLATFORM.get("snowflake") == "data-discovery-snowflake"

    def test_discovery_skill_map_databricks(self):
        from workbench.backend.routers.connections import DISCOVERY_SKILL_BY_PLATFORM
        assert DISCOVERY_SKILL_BY_PLATFORM.get("databricks") == "data-discovery-databricks"

    def test_profiling_skill_map_snowflake(self):
        from workbench.backend.routers.connections import PROFILING_SKILL_BY_PLATFORM
        assert PROFILING_SKILL_BY_PLATFORM.get("snowflake") == "data-profiling-snowflake"

    def test_profiling_skill_map_databricks(self):
        from workbench.backend.routers.connections import PROFILING_SKILL_BY_PLATFORM
        assert PROFILING_SKILL_BY_PLATFORM.get("databricks") == "data-profiling-databricks"

    def test_stage_execution_overrides_snowflake(self):
        from workbench.backend.stage_execution import _PLATFORM_SKILL_OVERRIDES
        assert _PLATFORM_SKILL_OVERRIDES["data_discovery"].get("snowflake") == "data-discovery-snowflake"
        assert _PLATFORM_SKILL_OVERRIDES["data_profiling"].get("snowflake") == "data-profiling-snowflake"

    def test_stage_execution_overrides_databricks(self):
        from workbench.backend.stage_execution import _PLATFORM_SKILL_OVERRIDES
        assert _PLATFORM_SKILL_OVERRIDES["data_discovery"].get("databricks") == "data-discovery-databricks"
        assert _PLATFORM_SKILL_OVERRIDES["data_profiling"].get("databricks") == "data-profiling-databricks"


class TestClassifyError:
    """A missing/mis-cased relation must classify as `relation_not_found`, not a
    blanket `sql_error`, so the preview UI can explain it (the motivating bug:
    a lowercase-quoted read of an UPPER unquoted-created Snowflake object)."""

    def _sf(self):
        from workbench.backend.platform.providers.snowflake import SnowflakeQueryExecutor
        return SnowflakeQueryExecutor()

    def _dbx(self):
        from workbench.backend.platform.providers.databricks import DatabricksQueryExecutor
        return DatabricksQueryExecutor()

    def test_snowflake_object_does_not_exist(self):
        e = Exception("002003 (42S02): SQL compilation error:\n"
                      "Object 'DWB_SERVING_DB.PUBLIC.PARTY' does not exist or not authorized.")
        assert self._sf().classify_error(e) == "relation_not_found"

    def test_snowflake_insufficient_privileges(self):
        e = Exception("Insufficient privileges to operate on schema 'PUBLIC'")
        assert self._sf().classify_error(e) == "permission_denied"

    def test_snowflake_other_stays_sql_error(self):
        assert self._sf().classify_error(Exception("syntax error near 'FROM'")) == "sql_error"

    def test_databricks_table_or_view_not_found(self):
        e = Exception("[TABLE_OR_VIEW_NOT_FOUND] The table or view `public`.`party` cannot be found.")
        assert self._dbx().classify_error(e) == "relation_not_found"

    def test_databricks_other_stays_sql_error(self):
        assert self._dbx().classify_error(Exception("PARSE_SYNTAX_ERROR")) == "sql_error"
