"""Tests for multi-platform dispatch in sql_executor.py.

Coverage:
- execute_deploy: platform defaults to postgres (existing behaviour unchanged)
- execute_deploy: non-Postgres routes to platform-specific driver (or returns
  platform_not_supported when the driver package is not installed)
- execute_deploy: platform case-insensitive ("PostgreSQL" == "postgres")
- execute_select: same platform dispatch rules
- validate_predicate: non-Postgres skips dry-run, passes prose-free predicates
- validate_predicate: non-Postgres still rejects prose (parse-only)
- platform support is resolved through the central provider lookup (dispatch)
"""
import unittest.mock as mock

import pytest

from workbench.backend.sql_executor import (
    DeployResult,
    SelectResult,
    ValidationResult,
    execute_deploy,
    execute_select,
    validate_predicate,
)


# ── platform support (via the central provider lookup) ──────────────────────────

class TestPlatformSupport:
    """Each shipped platform resolves both a query executor AND a deployment
    provider through platform.dispatch (the single choke point); unknown
    platforms fail closed. This replaces the old _SUPPORTED_PLATFORMS frozenset."""

    @pytest.mark.parametrize("platform", ["postgres", "postgresql", "mysql", "snowflake", "databricks"])
    def test_platform_resolves_executors(self, platform):
        from workbench.backend.platform.dispatch import (
            get_query_executor, get_deployment_provider,
        )
        assert get_query_executor(platform) is not None
        assert get_deployment_provider(platform) is not None

    def test_unknown_platform_fails_closed(self):
        from workbench.backend.platform.dispatch import get_query_executor, UnknownPlatform
        with pytest.raises(UnknownPlatform):
            get_query_executor("redshift")


# ── shared stub neo4j session ──────────────────────────────────────────────────

def _stub_neo4j():
    """A neo4j session stub that silently accepts audit writes."""
    ns = mock.MagicMock()
    ns.run.return_value = mock.MagicMock()
    return ns


# ── execute_deploy ─────────────────────────────────────────────────────────────

class TestExecuteDeployPlatform:
    """Use mock.patch on psycopg2.connect to avoid real TCP connections."""

    def _run_pg_deploy(self, platform=None, pg_fail_with=None):
        """Helper: run execute_deploy against the Postgres path with mocked connect."""
        import psycopg2 as _pg
        exc = pg_fail_with or _pg.OperationalError("mocked connection error")
        # Execution lives in the PostgresDeploymentProvider now (it does
        # `import psycopg2; psycopg2.connect(...)`), so patch the driver's connect
        # directly — the provider classifies the real psycopg2 exception.
        with mock.patch("psycopg2.connect", side_effect=exc):
            kwargs = dict(
                neo4j_session=_stub_neo4j(),
                project_code="test",
                pg_connection="postgresql://mocked:5432/db",
                ddl='CREATE OR REPLACE VIEW "s"."v" AS SELECT 1 AS col',
                view_schema="s",
                view_names=["v"],
                executed_by="test",
            )
            if platform is not None:
                kwargs["platform"] = platform
            return execute_deploy(**kwargs)

    def test_postgres_default_not_platform_not_supported(self):
        result = self._run_pg_deploy()
        assert result.status == "failed"
        assert result.error_class != "platform_not_supported"

    def test_postgres_explicit_not_platform_not_supported(self):
        result = self._run_pg_deploy(platform="postgres")
        assert result.error_class != "platform_not_supported"

    def test_postgresql_alias_treated_as_postgres(self):
        result = self._run_pg_deploy(platform="postgresql")
        assert result.error_class != "platform_not_supported"

    def test_platform_case_insensitive(self):
        result = self._run_pg_deploy(platform="PostgreSQL")
        assert result.error_class != "platform_not_supported"

    def test_snowflake_returns_failed_result(self):
        # Snowflake routes to the real driver. When snowflake-connector-python is
        # not installed the driver returns platform_not_supported; when it is
        # installed it returns connection_error / sql_error.  Either way the
        # result must be a failed DeployResult, never a live connection success.
        result = execute_deploy(
            neo4j_session=_stub_neo4j(),
            project_code="test",
            pg_connection="",
            ddl='CREATE OR REPLACE VIEW "s"."v" AS SELECT 1',
            view_schema="s",
            view_names=["v"],
            executed_by="test",
            platform="snowflake",
        )
        assert result.status == "failed"
        assert result.error_class in (
            "platform_not_supported", "sql_error", "connection_error",
        )

    def test_databricks_returns_failed_result(self):
        result = execute_deploy(
            neo4j_session=_stub_neo4j(),
            project_code="test",
            pg_connection="",
            ddl="CREATE OR REPLACE VIEW s.v AS SELECT 1",
            view_schema="s",
            view_names=["v"],
            executed_by="test",
            platform="databricks",
        )
        assert result.status == "failed"
        assert result.error_class in (
            "platform_not_supported", "sql_error", "connection_error",
        )

    def test_mysql_returns_failed_result(self):
        # pymysql IS installed; it will attempt to connect and fail.
        # Mock pymysql.connect to avoid network activity.
        import pymysql as _pymysql
        with mock.patch("pymysql.connect",
                        side_effect=_pymysql.OperationalError("mocked connection")):
            result = execute_deploy(
                neo4j_session=_stub_neo4j(),
                project_code="test",
                pg_connection="",
                ddl='CREATE OR REPLACE VIEW "s"."v" AS SELECT 1',
                view_schema="s",
                view_names=["v"],
                executed_by="test",
                platform="mysql",
            )
        assert result.status == "failed"
        assert result.error_class in ("connection_error", "sql_error")

    def test_unknown_platform_returns_platform_not_supported(self):
        result = execute_deploy(
            neo4j_session=_stub_neo4j(),
            project_code="test",
            pg_connection="",
            ddl="CREATE OR REPLACE VIEW s.v AS SELECT 1",
            view_schema="s",
            view_names=["v"],
            executed_by="test",
            platform="oracle",
        )
        assert result.error_class == "platform_not_supported"

    def test_unknown_platform_not_supported_message_mentions_dbt(self):
        # "oracle" is a truly unknown platform — always gets the fast-fail message.
        result = execute_deploy(
            neo4j_session=_stub_neo4j(),
            project_code="test",
            pg_connection="",
            ddl="SELECT 1",
            view_schema="s",
            view_names=[],
            executed_by="test",
            platform="oracle",
        )
        assert "dbt" in (result.error_message or "").lower()

    def test_non_postgres_still_writes_audit_node(self):
        ns = _stub_neo4j()
        execute_deploy(
            neo4j_session=ns,
            project_code="p",
            pg_connection="",
            ddl="SELECT 1",
            view_schema="s",
            view_names=[],
            executed_by="test",
            platform="snowflake",
        )
        ns.run.assert_called()

    def test_result_is_deploy_result_instance(self):
        result = execute_deploy(
            neo4j_session=_stub_neo4j(),
            project_code="p",
            pg_connection="",
            ddl="SELECT 1",
            view_schema="s",
            view_names=[],
            executed_by="test",
            platform="databricks",
        )
        assert isinstance(result, DeployResult)


# ── execute_select ─────────────────────────────────────────────────────────────

class TestExecuteSelectPlatform:
    def _run_pg_select(self, platform=None):
        import psycopg2 as _pg
        # Execution lives in the PostgresQueryExecutor now — patch the driver's
        # connect directly; the provider raises, the facade classifies.
        with mock.patch("psycopg2.connect", side_effect=_pg.OperationalError("mocked")):
            kwargs = dict(
                neo4j_session=_stub_neo4j(),
                project_code="test",
                pg_connection="postgresql://mocked:5432/db",
                sql="SELECT 1 AS n",
                executed_by="test",
            )
            if platform is not None:
                kwargs["platform"] = platform
            return execute_select(**kwargs)

    def test_postgres_default_hits_connection_not_platform_error(self):
        result = self._run_pg_select()
        assert result.status == "failed"
        assert result.error_class != "platform_not_supported"

    def test_snowflake_returns_failed_result(self):
        result = execute_select(
            neo4j_session=_stub_neo4j(),
            project_code="test",
            pg_connection="",
            sql="SELECT 1 AS n",
            executed_by="test",
            platform="snowflake",
        )
        assert result.status == "failed"
        assert result.error_class in (
            "platform_not_supported", "sql_error", "connection_error",
        )

    def test_databricks_returns_failed_result(self):
        result = execute_select(
            neo4j_session=_stub_neo4j(),
            project_code="test",
            pg_connection="",
            sql="SELECT 1",
            executed_by="test",
            platform="databricks",
        )
        assert result.status == "failed"
        assert result.error_class in (
            "platform_not_supported", "sql_error", "connection_error",
        )

    def test_mysql_returns_failed_result(self):
        # pymysql IS installed; mock connect to avoid real network calls.
        import pymysql as _pymysql
        with mock.patch("pymysql.connect",
                        side_effect=_pymysql.OperationalError("mocked")):
            result = execute_select(
                neo4j_session=_stub_neo4j(),
                project_code="test",
                pg_connection="",
                sql="SELECT 1",
                executed_by="test",
                platform="mysql",
            )
        assert result.status == "failed"
        assert result.error_class in ("connection_error", "sql_error")

    def test_postgresql_alias_not_platform_not_supported(self):
        result = self._run_pg_select(platform="postgresql")
        assert result.error_class != "platform_not_supported"

    def test_platform_case_insensitive(self):
        result = self._run_pg_select(platform="POSTGRES")
        assert result.error_class != "platform_not_supported"

    def test_non_postgres_still_writes_audit_node(self):
        ns = _stub_neo4j()
        execute_select(
            neo4j_session=ns,
            project_code="p",
            pg_connection="",
            sql="SELECT 1",
            executed_by="test",
            platform="snowflake",
        )
        ns.run.assert_called()

    def test_result_is_select_result_instance(self):
        result = execute_select(
            neo4j_session=_stub_neo4j(),
            project_code="p",
            pg_connection="",
            sql="SELECT 1",
            executed_by="test",
            platform="databricks",
        )
        assert isinstance(result, SelectResult)


# ── validate_predicate ────────────────────────────────────────────────────────

class TestValidatePredicatePlatform:
    def test_postgres_default_no_connection_returns_ok(self):
        # Parse-only (no pg_connection): operator-bearing predicate is ok
        result = validate_predicate("status = 'active'")
        assert result.ok is True

    def test_non_postgres_valid_predicate_ok(self):
        result = validate_predicate("status = 'active'", platform="snowflake")
        assert result.ok is True

    def test_non_postgres_prose_predicate_rejected(self):
        result = validate_predicate(
            "employee status is active", platform="snowflake"
        )
        assert result.ok is False
        assert result.error_class == "predicate_prose"

    def test_non_postgres_prose_predicate_rejected_databricks(self):
        result = validate_predicate(
            "only include active employees", platform="databricks"
        )
        assert result.ok is False
        assert result.error_class == "predicate_prose"

    def test_non_postgres_skips_dry_run_even_with_connection(self):
        # For non-Postgres, pg_connection + sample_from should NOT trigger
        # a psycopg2 dry-run (the driver doesn't exist).
        result = validate_predicate(
            "region = 'EMEA'",
            pg_connection="postgresql://ignored:5432/db",
            sample_from="public.employees",
            platform="snowflake",
        )
        # Should return ok (no psycopg2 exception) because the dry-run is skipped
        assert result.ok is True

    def test_empty_predicate_always_ok_regardless_of_platform(self):
        for plat in ("postgres", "snowflake", "databricks", "mysql"):
            assert validate_predicate("", platform=plat).ok is True
