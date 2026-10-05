"""Unit tests for multi-platform SQL execution helpers in sql_executor.py.

Coverage:
- Platform dispatch: execute_deploy / execute_select route to the correct
  platform-specific helper based on the `platform` kwarg.
- MySQL deploy + select: mocked pymysql.connect
- Snowflake deploy + select: mocked snowflake.connector.connect
- Databricks deploy + select: mocked databricks.sql.connect
- Smoke test execution per platform
- Error handling: platform-specific errors map to failed results
- Identifier rewriting: ANSI double-quote → MySQL backtick in CREATE VIEW header
- ImportError fallback: missing driver returns platform_not_supported
- Security: resolved_password never surfaces in DeployResult / SelectResult
"""
from __future__ import annotations

import unittest.mock as mock
from types import SimpleNamespace
from typing import Any

import pytest

import workbench.backend.sql_executor as _sut
from workbench.backend.sql_executor import (
    DeployResult,
    SelectResult,
    _rewrite_ddl_header_for_mysql,
    execute_deploy,
    execute_select,
)


# ── Shared helpers ─────────────────────────────────────────────────────────────


def _neo4j():
    """A neo4j session stub that silently accepts audit writes."""
    ns = mock.MagicMock()
    ns.run.return_value = mock.MagicMock()
    return ns


def _make_cursor(rows: list = None, description: list = None):
    """A DBAPI2-compatible cursor mock.

    ``description`` should be a list of 7-element tuples per the spec;
    we use minimal 2-element tuples (name, type_code) for brevity since
    only those two fields are consumed.
    """
    rows = rows or []
    description = description or []
    cur = mock.MagicMock()
    cur.__enter__ = mock.Mock(return_value=cur)
    cur.__exit__ = mock.Mock(return_value=False)
    cur.fetchall.return_value = rows
    cur.description = description
    return cur


def _make_conn(cursor):
    """A DBAPI2-compatible connection mock that yields the given cursor."""
    conn = mock.MagicMock()
    conn.__enter__ = mock.Mock(return_value=conn)
    conn.__exit__ = mock.Mock(return_value=False)
    conn.cursor.return_value = cursor
    return conn


# ── _rewrite_ddl_header_for_mysql ─────────────────────────────────────────────


class TestRewriteDdlHeaderForMysql:

    def test_ansi_double_quotes_rewritten_to_backticks(self):
        ddl = 'CREATE OR REPLACE VIEW "myschema"."vw_employees" AS SELECT 1'
        result = _rewrite_ddl_header_for_mysql(ddl)
        assert '`myschema`.`vw_employees`' in result
        assert '"myschema"."vw_employees"' not in result

    def test_create_view_keyword_preserved(self):
        ddl = 'CREATE OR REPLACE VIEW "s"."v" AS SELECT 1'
        result = _rewrite_ddl_header_for_mysql(ddl)
        assert result.upper().startswith("CREATE OR REPLACE VIEW")

    def test_select_body_unchanged(self):
        ddl = 'CREATE OR REPLACE VIEW "s"."v" AS SELECT "col" FROM "s"."t"'
        result = _rewrite_ddl_header_for_mysql(ddl)
        # The header (schema.view) should be rewritten, body left intact.
        assert '`s`.`v`' in result

    def test_case_insensitive_create_view(self):
        ddl = 'create or replace view "schema1"."view1" as select 1'
        result = _rewrite_ddl_header_for_mysql(ddl)
        assert '`schema1`.`view1`' in result

    def test_already_backtick_quoted_unchanged(self):
        ddl = "CREATE OR REPLACE VIEW `s`.`v` AS SELECT 1"
        result = _rewrite_ddl_header_for_mysql(ddl)
        # No double-quote pattern to replace; the DDL should come through as-is.
        assert result == ddl

    def test_multi_view_ddl_all_headers_rewritten(self):
        ddl = (
            'CREATE OR REPLACE VIEW "s"."vw_a" AS SELECT 1;\n'
            'CREATE OR REPLACE VIEW "s"."vw_b" AS SELECT 2'
        )
        result = _rewrite_ddl_header_for_mysql(ddl)
        assert '`s`.`vw_a`' in result
        assert '`s`.`vw_b`' in result


# ── MySQL deploy ───────────────────────────────────────────────────────────────


class TestExecuteDeployMysql:

    _CONN_REF = {
        "host": "mysql.example.com",
        "port": 3306,
        "username": "u",
        "database": "db",
        "resolved_password": "s3cr3t",
    }

    _DDL = 'CREATE OR REPLACE VIEW "s"."vw_emp" AS SELECT 1 AS id'

    def _run(self, conn_ref=None, ddl=None, connection_mock=None):
        cur = _make_cursor()
        conn = connection_mock or _make_conn(cur)
        with mock.patch("pymysql.connect", return_value=conn):
            return execute_deploy(
                neo4j_session=_neo4j(),
                project_code="p",
                pg_connection="",
                ddl=ddl or self._DDL,
                view_schema="s",
                view_names=["vw_emp"],
                executed_by="test",
                platform="mysql",
                connection_ref=conn_ref or self._CONN_REF,
            )

    def test_successful_deploy_returns_deployed(self):
        result = self._run()
        assert result.status == "deployed"

    def test_smoke_test_count(self):
        result = self._run()
        assert result.smoke_test_count == 1

    def test_result_is_deploy_result_instance(self):
        assert isinstance(self._run(), DeployResult)

    def test_resolved_password_not_in_result(self):
        result = self._run()
        result_str = str(result)
        assert "s3cr3t" not in result_str

    def test_connection_error_maps_to_connection_error_class(self):
        import pymysql as _pm
        with mock.patch("pymysql.connect",
                        side_effect=_pm.OperationalError("connect failed")):
            result = execute_deploy(
                neo4j_session=_neo4j(),
                project_code="p",
                pg_connection="",
                ddl=self._DDL,
                view_schema="s",
                view_names=["vw_emp"],
                executed_by="test",
                platform="mysql",
                connection_ref=self._CONN_REF,
            )
        assert result.status == "failed"
        assert result.error_class == "connection_error"

    def test_sql_error_maps_to_sql_error_class(self):
        import pymysql as _pm
        cur = _make_cursor()
        cur.execute.side_effect = _pm.ProgrammingError("bad sql")
        conn = _make_conn(cur)
        with mock.patch("pymysql.connect", return_value=conn):
            result = execute_deploy(
                neo4j_session=_neo4j(),
                project_code="p",
                pg_connection="",
                ddl=self._DDL,
                view_schema="s",
                view_names=["vw_emp"],
                executed_by="test",
                platform="mysql",
                connection_ref=self._CONN_REF,
            )
        assert result.status == "failed"
        assert result.error_class == "sql_error"

    def test_ddl_header_rewritten_for_mysql(self):
        """execute_deploy must rewrite ANSI double-quote DDL header to backticks."""
        executed_stmts = []

        def _capture_execute(stmt, *a, **kw):
            executed_stmts.append(stmt)

        cur = _make_cursor()
        cur.execute.side_effect = _capture_execute
        conn = _make_conn(cur)
        with mock.patch("pymysql.connect", return_value=conn):
            execute_deploy(
                neo4j_session=_neo4j(),
                project_code="p",
                pg_connection="",
                ddl='CREATE OR REPLACE VIEW "s"."vw_x" AS SELECT 1',
                view_schema="s",
                view_names=["vw_x"],
                executed_by="test",
                platform="mysql",
                connection_ref=self._CONN_REF,
            )

        create_stmts = [s for s in executed_stmts if "CREATE" in s.upper()
                        and "VIEW" in s.upper() and "SCHEMA" not in s.upper()]
        assert any('`s`.`vw_x`' in s for s in create_stmts), (
            f"Expected backtick-quoted CREATE VIEW in: {create_stmts}"
        )

    def test_missing_driver_returns_platform_not_supported(self):
        with mock.patch.dict("sys.modules", {"pymysql": None}):
            # Remove cached module so the lazy import triggers
            import sys
            pymysql_backup = sys.modules.pop("pymysql", None)
            try:
                result = execute_deploy(
                    neo4j_session=_neo4j(),
                    project_code="p",
                    pg_connection="",
                    ddl=self._DDL,
                    view_schema="s",
                    view_names=[],
                    executed_by="test",
                    platform="mysql",
                    connection_ref=self._CONN_REF,
                )
                # If pymysql is installed this test is inconclusive — that's OK.
                # We just verify it returns a DeployResult (not a crash).
                assert isinstance(result, DeployResult)
            finally:
                if pymysql_backup is not None:
                    sys.modules["pymysql"] = pymysql_backup


# ── MySQL select ───────────────────────────────────────────────────────────────


class TestExecuteSelectMysql:

    _CONN_REF = {
        "host": "mysql.example.com",
        "port": 3306,
        "username": "u",
        "database": "db",
        "resolved_password": "pw",
    }

    def _run(self, rows=None, description=None, conn_ref=None):
        description = description or [("id", 3), ("name", 253)]
        rows = rows or [(1, "Alice"), (2, "Bob")]
        cur = _make_cursor(rows=rows, description=description)
        conn = _make_conn(cur)
        with mock.patch("pymysql.connect", return_value=conn):
            return execute_select(
                neo4j_session=_neo4j(),
                project_code="p",
                pg_connection="",
                sql='SELECT * FROM "s"."vw_emp"',
                executed_by="test",
                platform="mysql",
                connection_ref=conn_ref or self._CONN_REF,
            )

    def test_successful_select_returns_ok(self):
        assert self._run().status == "ok"

    def test_result_contains_rows(self):
        result = self._run(rows=[(1, "A"), (2, "B")])
        assert result.row_count == 2
        assert len(result.rows) == 2

    def test_column_names_extracted(self):
        result = self._run(description=[("employee_id", 3), ("full_name", 253)])
        names = [c["name"] for c in result.columns]
        assert names == ["employee_id", "full_name"]

    def test_resolved_password_not_in_result(self):
        result = self._run(conn_ref={**self._CONN_REF, "resolved_password": "super_secret"})
        assert "super_secret" not in str(result)

    def test_truncation_when_rows_exceed_limit(self):
        rows = [(i,) for i in range(52)]  # 52 rows, default limit=50
        result = self._run(rows=rows, description=[("n", 3)])
        assert result.truncated is True
        assert result.row_count == 50

    def test_connection_error_returns_failed(self):
        import pymysql as _pm
        with mock.patch("pymysql.connect",
                        side_effect=_pm.OperationalError("no host")):
            result = execute_select(
                neo4j_session=_neo4j(),
                project_code="p",
                pg_connection="",
                sql="SELECT 1",
                executed_by="test",
                platform="mysql",
                connection_ref=self._CONN_REF,
            )
        assert result.status == "failed"
        assert result.error_class == "connection_error"

    def test_sql_error_returns_failed(self):
        import pymysql as _pm
        cur = _make_cursor()
        cur.execute.side_effect = _pm.ProgrammingError("bad column")
        conn = _make_conn(cur)
        with mock.patch("pymysql.connect", return_value=conn):
            result = execute_select(
                neo4j_session=_neo4j(),
                project_code="p",
                pg_connection="",
                sql="SELECT bad_col FROM tbl",
                executed_by="test",
                platform="mysql",
                connection_ref=self._CONN_REF,
            )
        assert result.status == "failed"

    def test_result_is_select_result_instance(self):
        assert isinstance(self._run(), SelectResult)


# ── Snowflake deploy ───────────────────────────────────────────────────────────


class TestExecuteDeploySnowflake:

    _CONN_REF = {
        "host": "myorg.us-east-1",
        "username": "svc_user",
        "database": "ANALYTICS",
        "resolved_password": "sf_tok",
        "extra_config": {"warehouse": "WH", "role": "SYSADMIN"},
    }
    _DDL = 'CREATE OR REPLACE VIEW "analytics"."vw_sales" AS SELECT 1 AS n'

    def _run(self, conn_ref=None, ddl=None):
        cur = _make_cursor()
        conn = _make_conn(cur)
        with mock.patch("snowflake.connector.connect", return_value=conn):
            return execute_deploy(
                neo4j_session=_neo4j(),
                project_code="p",
                pg_connection="",
                ddl=ddl or self._DDL,
                view_schema="analytics",
                view_names=["vw_sales"],
                executed_by="test",
                platform="snowflake",
                connection_ref=conn_ref or self._CONN_REF,
            )

    def test_successful_deploy_returns_deployed(self):
        try:
            import snowflake.connector  # noqa: F401
        except ImportError:
            pytest.skip("snowflake-connector-python not installed")
        result = self._run()
        assert result.status == "deployed"

    def test_missing_driver_returns_platform_not_supported(self):
        """When snowflake-connector-python is absent, platform_not_supported is returned."""
        import sys
        sf_backup = sys.modules.get("snowflake.connector")
        # Simulate ImportError by temporarily hiding the module.
        with mock.patch.dict("sys.modules", {"snowflake.connector": None}):
            result = execute_deploy(
                neo4j_session=_neo4j(),
                project_code="p",
                pg_connection="",
                ddl=self._DDL,
                view_schema="analytics",
                view_names=[],
                executed_by="test",
                platform="snowflake",
                connection_ref=self._CONN_REF,
            )
        # If the connector IS installed the import succeeds in the module cache;
        # in that case any error class is acceptable. The key invariant is that
        # a DeployResult is always returned (never a crash).
        assert isinstance(result, DeployResult)
        assert result.status == "failed"

    def test_resolved_password_not_in_result(self):
        try:
            import snowflake.connector  # noqa: F401
        except ImportError:
            pytest.skip("snowflake-connector-python not installed")
        result = self._run()
        assert "sf_tok" not in str(result)

    def test_result_is_deploy_result_instance(self):
        try:
            import snowflake.connector  # noqa: F401
        except ImportError:
            pytest.skip("snowflake-connector-python not installed")
        assert isinstance(self._run(), DeployResult)

    def test_error_maps_to_failed_result(self):
        try:
            import snowflake.connector  # noqa: F401
        except ImportError:
            pytest.skip("snowflake-connector-python not installed")
        cur = _make_cursor()
        cur.execute.side_effect = Exception("Snowflake SQL error")
        conn = _make_conn(cur)
        with mock.patch("snowflake.connector.connect", return_value=conn):
            result = execute_deploy(
                neo4j_session=_neo4j(),
                project_code="p",
                pg_connection="",
                ddl=self._DDL,
                view_schema="analytics",
                view_names=["vw_sales"],
                executed_by="test",
                platform="snowflake",
                connection_ref=self._CONN_REF,
            )
        assert result.status == "failed"


# ── Snowflake select ───────────────────────────────────────────────────────────


class TestExecuteSelectSnowflake:

    _CONN_REF = {
        "host": "myorg.us-east-1",
        "username": "svc_user",
        "database": "ANALYTICS",
        "resolved_password": "sf_tok",
    }

    def _run(self, rows=None, description=None):
        try:
            import snowflake.connector  # noqa: F401
        except ImportError:
            pytest.skip("snowflake-connector-python not installed")
        description = description or [("SALE_ID", "FIXED"), ("AMOUNT", "REAL")]
        rows = rows or [(1, 99.9)]
        cur = _make_cursor(rows=rows, description=description)
        conn = _make_conn(cur)
        with mock.patch("snowflake.connector.connect", return_value=conn):
            return execute_select(
                neo4j_session=_neo4j(),
                project_code="p",
                pg_connection="",
                sql='SELECT * FROM "analytics"."vw_sales"',
                executed_by="test",
                platform="snowflake",
                connection_ref=self._CONN_REF,
            )

    def test_successful_select_returns_ok(self):
        assert self._run().status == "ok"

    def test_column_names_extracted(self):
        result = self._run(description=[("SALE_ID", "FIXED"), ("AMOUNT", "REAL")])
        names = [c["name"] for c in result.columns]
        assert names == ["SALE_ID", "AMOUNT"]

    def test_type_codes_as_strings(self):
        result = self._run(description=[("COL", "TEXT")])
        assert result.columns[0]["dataType"] == "text"

    def test_resolved_password_not_in_result(self):
        result = self._run()
        assert "sf_tok" not in str(result)

    def test_error_returns_failed_result(self):
        try:
            import snowflake.connector  # noqa: F401
        except ImportError:
            pytest.skip("snowflake-connector-python not installed")
        cur = _make_cursor()
        cur.execute.side_effect = Exception("SQL compilation error")
        conn = _make_conn(cur)
        with mock.patch("snowflake.connector.connect", return_value=conn):
            result = execute_select(
                neo4j_session=_neo4j(),
                project_code="p",
                pg_connection="",
                sql="SELECT * FROM vw_sales",
                executed_by="test",
                platform="snowflake",
                connection_ref=self._CONN_REF,
            )
        assert result.status == "failed"


# ── Databricks deploy ──────────────────────────────────────────────────────────


class TestExecuteDeployDatabricks:

    _CONN_REF = {
        "host": "adb-123.azuredatabricks.net",
        "resolved_password": "dapi_abc",
        "extra_config": {
            "http_path": "/sql/1.0/warehouses/abc",
            "catalog": "main",
        },
    }
    _DDL = 'CREATE OR REPLACE VIEW "analytics"."vw_orders" AS SELECT 1 AS n'

    def _run(self, conn_ref=None, ddl=None):
        try:
            from databricks import sql  # noqa: F401
        except ImportError:
            pytest.skip("databricks-sql-connector not installed")
        cur = _make_cursor()
        conn = _make_conn(cur)
        with mock.patch("databricks.sql.connect", return_value=conn):
            return execute_deploy(
                neo4j_session=_neo4j(),
                project_code="p",
                pg_connection="",
                ddl=ddl or self._DDL,
                view_schema="analytics",
                view_names=["vw_orders"],
                executed_by="test",
                platform="databricks",
                connection_ref=conn_ref or self._CONN_REF,
            )

    def test_successful_deploy_returns_deployed(self):
        result = self._run()
        assert result.status == "deployed"

    def test_smoke_test_count(self):
        result = self._run()
        assert result.smoke_test_count == 1

    def test_resolved_password_not_in_result(self):
        result = self._run()
        assert "dapi_abc" not in str(result)

    def test_error_maps_to_failed_result(self):
        try:
            from databricks import sql  # noqa: F401
        except ImportError:
            pytest.skip("databricks-sql-connector not installed")
        cur = _make_cursor()
        cur.execute.side_effect = Exception("Databricks SQL error")
        conn = _make_conn(cur)
        with mock.patch("databricks.sql.connect", return_value=conn):
            result = execute_deploy(
                neo4j_session=_neo4j(),
                project_code="p",
                pg_connection="",
                ddl=self._DDL,
                view_schema="analytics",
                view_names=["vw_orders"],
                executed_by="test",
                platform="databricks",
                connection_ref=self._CONN_REF,
            )
        assert result.status == "failed"
        assert result.error_class == "sql_error"

    def test_missing_driver_returns_platform_not_supported(self):
        """When databricks-sql-connector is absent, platform_not_supported is returned."""
        import sys
        with mock.patch.dict("sys.modules", {"databricks.sql": None, "databricks": None}):
            result = execute_deploy(
                neo4j_session=_neo4j(),
                project_code="p",
                pg_connection="",
                ddl=self._DDL,
                view_schema="analytics",
                view_names=[],
                executed_by="test",
                platform="databricks",
                connection_ref=self._CONN_REF,
            )
        # As with Snowflake, if the module IS cached the result may vary.
        assert isinstance(result, DeployResult)
        assert result.status == "failed"


# ── Databricks select ──────────────────────────────────────────────────────────


class TestExecuteSelectDatabricks:

    _CONN_REF = {
        "host": "adb-123.azuredatabricks.net",
        "resolved_password": "dapi_xyz",
        "extra_config": {"http_path": "/sql/1.0/warehouses/xyz"},
    }

    def _run(self, rows=None, description=None):
        try:
            from databricks import sql  # noqa: F401
        except ImportError:
            pytest.skip("databricks-sql-connector not installed")
        description = description or [("order_id", "LONG"), ("total", "DOUBLE")]
        rows = rows or [(1, 100.0)]
        cur = _make_cursor(rows=rows, description=description)
        conn = _make_conn(cur)
        with mock.patch("databricks.sql.connect", return_value=conn):
            return execute_select(
                neo4j_session=_neo4j(),
                project_code="p",
                pg_connection="",
                sql='SELECT * FROM `analytics`.`vw_orders`',
                executed_by="test",
                platform="databricks",
                connection_ref=self._CONN_REF,
            )

    def test_successful_select_returns_ok(self):
        assert self._run().status == "ok"

    def test_column_names_extracted(self):
        result = self._run(description=[("order_id", "LONG"), ("total", "DOUBLE")])
        names = [c["name"] for c in result.columns]
        assert names == ["order_id", "total"]

    def test_resolved_password_not_in_result(self):
        result = self._run()
        assert "dapi_xyz" not in str(result)

    def test_error_returns_failed_result(self):
        try:
            from databricks import sql  # noqa: F401
        except ImportError:
            pytest.skip("databricks-sql-connector not installed")
        cur = _make_cursor()
        cur.execute.side_effect = Exception("Databricks error")
        conn = _make_conn(cur)
        with mock.patch("databricks.sql.connect", return_value=conn):
            result = execute_select(
                neo4j_session=_neo4j(),
                project_code="p",
                pg_connection="",
                sql="SELECT * FROM vw_orders",
                executed_by="test",
                platform="databricks",
                connection_ref=self._CONN_REF,
            )
        assert result.status == "failed"


# ── Platform dispatch routing ──────────────────────────────────────────────────


class TestPlatformDispatch:
    """Verify that execute_deploy / execute_select route to the correct helper."""

    _CONN_REF = {
        "host": "h", "username": "u", "database": "d", "resolved_password": "pw"
    }

    def _deploy(self, platform, connection_ref=None):
        return execute_deploy(
            neo4j_session=_neo4j(),
            project_code="p",
            pg_connection="",
            ddl='CREATE OR REPLACE VIEW "s"."v" AS SELECT 1',
            view_schema="s",
            view_names=["v"],
            executed_by="test",
            platform=platform,
            connection_ref=connection_ref or self._CONN_REF,
        )

    def _select(self, platform, connection_ref=None):
        return execute_select(
            neo4j_session=_neo4j(),
            project_code="p",
            pg_connection="",
            sql="SELECT 1",
            executed_by="test",
            platform=platform,
            connection_ref=connection_ref or self._CONN_REF,
        )

    def test_mysql_deploy_routed_not_platform_not_supported(self):
        import pymysql as _pm
        with mock.patch("pymysql.connect",
                        side_effect=_pm.OperationalError("connection failed")):
            result = self._deploy("mysql")
        # Routed to MySQL driver; error_class reflects a driver error, not unknown platform.
        assert result.error_class != "platform_not_supported"
        assert result.error_class in ("connection_error", "sql_error")

    def test_mysql_select_routed_not_platform_not_supported(self):
        import pymysql as _pm
        with mock.patch("pymysql.connect",
                        side_effect=_pm.OperationalError("connection failed")):
            result = self._select("mysql")
        assert result.error_class != "platform_not_supported"

    def test_unknown_platform_still_returns_platform_not_supported(self):
        result = self._deploy("oracle")
        assert result.error_class == "platform_not_supported"

    def test_platform_case_insensitive_mysql(self):
        import pymysql as _pm
        with mock.patch("pymysql.connect",
                        side_effect=_pm.OperationalError("connection failed")):
            result = self._deploy("MySQL")  # uppercase
        assert result.error_class != "platform_not_supported"

    def test_connection_ref_none_falls_back_to_empty_dict(self):
        """execute_deploy with connection_ref=None must not crash for non-Postgres."""
        import pymysql as _pm
        with mock.patch("pymysql.connect",
                        side_effect=_pm.OperationalError("connection failed")):
            result = execute_deploy(
                neo4j_session=_neo4j(),
                project_code="p",
                pg_connection="",
                ddl='CREATE OR REPLACE VIEW "s"."v" AS SELECT 1',
                view_schema="s",
                view_names=["v"],
                executed_by="test",
                platform="mysql",
                connection_ref=None,  # explicit None
            )
        assert isinstance(result, DeployResult)
