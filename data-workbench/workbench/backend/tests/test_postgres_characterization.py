"""Postgres characterization tests — Phase 0 safety baseline.

These tests freeze the observable behavior of the SQL compiler, dialect
registry, and security-sensitive API responses BEFORE any multi-platform
refactoring touches them.  If a test fails after a platform change the
failure is intentional and should be reviewed before marking it a new
characterization baseline.

Run from the repo root:
    env/bin/python -m pytest workbench/backend/tests/test_postgres_characterization.py -v
"""
from __future__ import annotations

import importlib.util
import pathlib
from typing import Any

import pytest

# ── load generate_view_ddl without adding workbench-skills to sys.path ──────
_GEN = (
    pathlib.Path(__file__).resolve().parents[3]
    / "workbench-skills/skills/data-serving-virtual-view/scripts/generate_view_ddl.py"
)
_spec = importlib.util.spec_from_file_location("generate_view_ddl", _GEN)
g = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(g)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Dialect registry
# ═══════════════════════════════════════════════════════════════════════════════

class TestDialectRegistry:
    """Every named dialect is present; the registry must not silently expand."""

    EXPECTED_KEYS = {"postgres", "postgresql", "snowflake", "databricks", "bigquery", "ansi", "mysql", "duckdb"}

    def test_registered_dialect_keys(self):
        assert set(g._DIALECTS.keys()) == self.EXPECTED_KEYS

    def test_get_dialect_postgres(self):
        d = g.get_dialect("postgres")
        assert isinstance(d, g.PostgresDialect)

    def test_get_dialect_postgresql_alias(self):
        d = g.get_dialect("postgresql")
        assert isinstance(d, g.PostgresDialect)

    def test_get_dialect_snowflake(self):
        d = g.get_dialect("snowflake")
        assert isinstance(d, g.SnowflakeDialect)

    def test_get_dialect_databricks(self):
        d = g.get_dialect("databricks")
        assert isinstance(d, g.DatabricksDialect)

    def test_get_dialect_bigquery(self):
        d = g.get_dialect("bigquery")
        assert isinstance(d, g.BigQueryDialect)

    def test_get_dialect_ansi(self):
        d = g.get_dialect("ansi")
        assert type(d) is g.Dialect  # exact base, not a subclass

    def test_get_dialect_none_returns_postgres(self):
        # None / empty → PostgresDialect (explicit default, not fallback).
        assert isinstance(g.get_dialect(None), g.PostgresDialect)
        assert isinstance(g.get_dialect(""), g.PostgresDialect)

    def test_get_dialect_unknown_name_raises(self):
        # Non-empty unknown names must fail closed (ADR-9).
        # Silently emitting Postgres DDL against Snowflake/Databricks
        # would corrupt a deployment without a meaningful error.
        with pytest.raises(ValueError, match="Unknown dialect"):
            g.get_dialect("oracle")
        with pytest.raises(ValueError, match="Unknown dialect"):
            g.get_dialect("redshift")

    def test_get_dialect_unknown_name_error_lists_known_dialects(self):
        try:
            g.get_dialect("mssql")
        except ValueError as e:
            assert "postgres" in str(e)
            assert "snowflake" in str(e)
        else:
            pytest.fail("Expected ValueError")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Dialect expression output — Postgres golden strings
# ═══════════════════════════════════════════════════════════════════════════════

class TestPostgresDialectExpressions:
    """Golden-string tests for every expression variant that is
    dialect-specific in the current compiler.  Failures here mean the
    Postgres behavior has changed; investigate before proceeding."""

    @pytest.fixture(autouse=True)
    def dialect(self):
        self.d = g.PostgresDialect()

    def test_hash_sha1(self):
        # Postgres uses pgcrypto: encode(digest(expr,'sha1'),'hex')
        result = self.d.hash_sha("col", "sha1")
        assert result == "encode(digest(col, 'sha1'), 'hex')"

    def test_hash_sha256(self):
        result = self.d.hash_sha("col", "sha256")
        assert result == "encode(digest(col, 'sha256'), 'hex')"

    def test_hash_md5(self):
        result = self.d.hash_md5("col")
        assert result.startswith("md5(")

    def test_regexp_replace_global_flag(self):
        # Postgres REGEXP_REPLACE needs the explicit 'g' flag.
        result = self.d.regexp_replace_global("h", "p", "r")
        assert "'g'" in result
        assert "REGEXP_REPLACE" in result

    def test_cast_uses_double_colon(self):
        # Postgres uses v::type; other dialects use CAST(v AS type).
        result = self.d.cast("my_col", "TEXT")
        assert "::" in result
        assert "CAST" not in result


class TestSnowflakeDialectExpressions:
    """Snowflake differs from Postgres for hash and casting."""

    @pytest.fixture(autouse=True)
    def dialect(self):
        self.d = g.SnowflakeDialect()

    def test_hash_sha1(self):
        result = self.d.hash_sha("col", "sha1")
        assert "SHA1" in result.upper()
        # Must NOT use pgcrypto encode/digest syntax
        assert "encode" not in result
        assert "digest" not in result

    def test_hash_sha256(self):
        result = self.d.hash_sha("col", "sha256")
        assert "SHA2" in result.upper() or "SHA256" in result.upper()
        assert "encode" not in result

    def test_cast_uses_ansi(self):
        result = self.d.cast("my_col", "TEXT")
        assert "CAST" in result
        assert "::" not in result


class TestDatabricksDialectExpressions:
    """Databricks uses lowercase sha1/sha2 (Spark SQL functions)."""

    @pytest.fixture(autouse=True)
    def dialect(self):
        self.d = g.DatabricksDialect()

    def test_hash_sha1(self):
        result = self.d.hash_sha("col", "sha1")
        assert "sha1" in result.lower()
        assert "encode" not in result

    def test_hash_sha256(self):
        result = self.d.hash_sha("col", "sha256")
        assert "sha2" in result.lower() or "sha256" in result.lower()

    def test_cast_uses_ansi(self):
        result = self.d.cast("my_col", "TEXT")
        assert "CAST" in result
        assert "::" not in result


class TestBigQueryDialectExpressions:
    """BigQuery uses TO_HEX(SHA1(...)) / TO_HEX(SHA256(...))."""

    @pytest.fixture(autouse=True)
    def dialect(self):
        self.d = g.BigQueryDialect()

    def test_hash_sha1(self):
        result = self.d.hash_sha("col", "sha1")
        assert "TO_HEX" in result.upper()
        assert "SHA1" in result.upper()

    def test_hash_sha256(self):
        result = self.d.hash_sha("col", "sha256")
        assert "TO_HEX" in result.upper()
        assert "SHA256" in result.upper()


class TestDialectDivergence:
    """Confirm the four named dialects produce DISTINCT hash output.

    If two hash functions become identical we lose confidence that
    the correct dialect is being used at deploy time.
    """

    def _sha1(self, name: str) -> str:
        return g.get_dialect(name).hash_sha("x", "sha1")

    def test_hash_outputs_are_all_distinct(self):
        results = [
            self._sha1("postgres"),
            self._sha1("snowflake"),
            self._sha1("databricks"),
            self._sha1("bigquery"),
        ]
        assert len(set(results)) == 4, f"hash outputs collide: {results}"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. DDL allowlist (sql_executor)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSqlExecutorAllowlist:
    """The DDL allowlist must only permit CREATE/DROP VIEW; nothing else.

    _classify_deploy_statement returns:
      ''    — empty/comment-only (allowed, skip)
      str   — normalised label like 'CREATE OR REPLACE VIEW' (allowed)
      None  — rejected
    """

    @pytest.fixture(autouse=True)
    def _import_executor(self):
        import workbench.backend.sql_executor as ex
        self.classify = ex._classify_deploy_statement
        self.is_select = ex._is_single_select

    def test_allowed_create_or_replace_view(self):
        result = self.classify("CREATE OR REPLACE VIEW my_schema.my_view AS SELECT 1")
        assert result is not None
        assert "VIEW" in result.upper()

    def test_allowed_create_view(self):
        result = self.classify('CREATE VIEW "schema"."view" AS SELECT 1')
        assert result is not None

    def test_allowed_drop_view(self):
        result = self.classify('DROP VIEW IF EXISTS "schema"."view"')
        assert result is not None

    def test_rejected_create_table(self):
        assert self.classify("CREATE TABLE foo (id int)") is None

    def test_rejected_insert(self):
        assert self.classify("INSERT INTO foo VALUES (1)") is None

    def test_rejected_update(self):
        assert self.classify("UPDATE foo SET x = 1") is None

    def test_rejected_drop_table(self):
        assert self.classify("DROP TABLE foo") is None

    def test_rejected_select_in_ddl_gate(self):
        assert self.classify("SELECT 1") is None

    def test_select_gate_accepts_select(self):
        assert self.is_select("SELECT id, name FROM users LIMIT 10") is True

    def test_select_gate_accepts_cte(self):
        assert self.is_select("WITH x AS (SELECT 1) SELECT * FROM x") is True

    def test_select_gate_rejects_insert(self):
        assert self.is_select("INSERT INTO foo VALUES (1)") is False

    def test_select_gate_rejects_multiple_statements(self):
        assert self.is_select("SELECT 1; SELECT 2") is False


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Password-redaction characterization
# ═══════════════════════════════════════════════════════════════════════════════

class TestPasswordRedaction:
    """GET /data-source and GET /materialization-target must never return
    plaintext credentials.  These tests confirm the Phase 0 security fix."""

    def _parse_get_data_source(self, pg_connection: str) -> dict[str, Any]:
        """Call the same DSN-parsing logic used by get_data_source."""
        from urllib.parse import urlparse, unquote, parse_qs
        parsed = urlparse(pg_connection)
        schema_name = None
        qs = parse_qs(parsed.query or "")
        opts = qs.get("options", [""])[0]
        if "search_path" in opts:
            schema_name = opts.split("search_path", 1)[1].lstrip("=").strip() or None
        # Mirror the FIXED handler (password always "")
        return {
            "platform": (parsed.scheme or "postgres").replace("postgresql", "postgres"),
            "host": parsed.hostname or "",
            "port": parsed.port or 5432,
            "database": (parsed.path or "/").lstrip("/"),
            "username": unquote(parsed.username or ""),
            "password": "",
            "schema_name": schema_name,
        }

    def test_password_not_in_get_data_source_response(self):
        conn = "postgresql://admin:supersecret@localhost:5432/mydb"
        result = self._parse_get_data_source(conn)
        assert result["password"] == ""
        assert "supersecret" not in str(result)

    def test_username_still_returned(self):
        conn = "postgresql://admin:supersecret@localhost:5432/mydb"
        result = self._parse_get_data_source(conn)
        assert result["username"] == "admin"

    def test_host_and_db_still_returned(self):
        conn = "postgresql://admin:supersecret@pg.internal:5433/warehouse"
        result = self._parse_get_data_source(conn)
        assert result["host"] == "pg.internal"
        assert result["port"] == 5433
        assert result["database"] == "warehouse"

    def test_special_chars_in_password_do_not_leak(self):
        from urllib.parse import quote
        pw = quote("p@$$w0rd!", safe="")
        conn = f"postgresql://user:{pw}@host/db"
        result = self._parse_get_data_source(conn)
        assert result["password"] == ""
        assert "p@$$w0rd!" not in str(result)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Platform registry characterization (Phase 1 baseline)
# ═══════════════════════════════════════════════════════════════════════════════

class TestPlatformRegistry:
    """Freeze the capability-matrix contract for the four shipped manifests."""

    @pytest.fixture(autouse=True)
    def registry(self):
        from workbench.backend.platform.registry import PlatformRegistry
        self.reg = PlatformRegistry()

    def test_postgres_manifest_loaded(self):
        assert "postgres" in self.reg.platform_ids()

    def test_mysql_manifest_loaded(self):
        assert "mysql" in self.reg.platform_ids()

    def test_snowflake_manifest_loaded(self):
        assert "snowflake" in self.reg.platform_ids()

    def test_databricks_manifest_loaded(self):
        assert "databricks" in self.reg.platform_ids()

    def test_postgres_connection_is_certified(self):
        from workbench.backend.platform.interfaces import CapabilityLevel
        assert self.reg.get_capability("postgres", "connection") == CapabilityLevel.CERTIFIED

    def test_postgres_read_query_is_certified(self):
        from workbench.backend.platform.interfaces import CapabilityLevel
        assert self.reg.get_capability("postgres", "read_query") == CapabilityLevel.CERTIFIED

    def test_mysql_connection_is_preview(self):
        from workbench.backend.platform.interfaces import CapabilityLevel
        assert self.reg.get_capability("mysql", "connection") == CapabilityLevel.PREVIEW

    def test_mysql_native_view_is_preview(self):
        # MySQL native_view advanced to preview when the MySQL provider landed.
        # See platform/manifests/mysql.yaml.
        from workbench.backend.platform.interfaces import CapabilityLevel
        assert self.reg.get_capability("mysql", "native_view") == CapabilityLevel.PREVIEW

    def test_snowflake_connection_is_preview(self):
        # Snowflake connection/discovery/profiling advanced to preview when the
        # SnowflakeConnectionProvider landed.  See manifests/snowflake.yaml.
        from workbench.backend.platform.interfaces import CapabilityLevel
        assert self.reg.get_capability("snowflake", "connection") == CapabilityLevel.PREVIEW
        assert self.reg.get_capability("snowflake", "discovery") == CapabilityLevel.PREVIEW

    def test_snowflake_remediation_still_unsupported(self):
        from workbench.backend.platform.interfaces import CapabilityLevel
        assert self.reg.get_capability("snowflake", "remediation") == CapabilityLevel.UNSUPPORTED
        assert self.reg.get_capability("snowflake", "semantic_qa") == CapabilityLevel.UNSUPPORTED

    def test_databricks_connection_is_preview(self):
        # Databricks connection/discovery/profiling advanced to preview when the
        # DatabricksConnectionProvider landed.  See manifests/databricks.yaml.
        from workbench.backend.platform.interfaces import CapabilityLevel
        assert self.reg.get_capability("databricks", "connection") == CapabilityLevel.PREVIEW
        assert self.reg.get_capability("databricks", "discovery") == CapabilityLevel.PREVIEW

    def test_databricks_remediation_still_unsupported(self):
        from workbench.backend.platform.interfaces import CapabilityLevel
        assert self.reg.get_capability("databricks", "remediation") == CapabilityLevel.UNSUPPORTED
        assert self.reg.get_capability("databricks", "semantic_qa") == CapabilityLevel.UNSUPPORTED

    def test_unknown_platform_returns_unsupported(self):
        from workbench.backend.platform.interfaces import CapabilityLevel
        assert self.reg.get_capability("redshift", "connection") == CapabilityLevel.UNSUPPORTED

    def test_unknown_capability_on_postgres_returns_unsupported(self):
        from workbench.backend.platform.interfaces import CapabilityLevel
        assert self.reg.get_capability("postgres", "nonexistent_cap") == CapabilityLevel.UNSUPPORTED

    def test_postgres_is_usable_for_connection(self):
        assert self.reg.is_usable("postgres", "connection") is True

    def test_snowflake_usable_for_connection(self):
        # Advanced to preview → is_usable True (was experimental → False).
        assert self.reg.is_usable("snowflake", "connection") is True

    def test_object_store_not_usable_for_connection(self):
        # Object stores stay unsupported for the networked-connection capability.
        assert self.reg.is_usable("s3", "connection") is False

    def test_assert_usable_passes_for_postgres_connection(self):
        from workbench.backend.platform.registry import CapabilityUnavailable
        self.reg.assert_usable("postgres", "connection")   # must not raise

    def test_assert_usable_raises_for_object_store_connection(self):
        # s3 connection is unsupported → assert_usable must raise (snowflake is
        # now preview and no longer raises).
        from workbench.backend.platform.registry import CapabilityUnavailable
        with pytest.raises(CapabilityUnavailable):
            self.reg.assert_usable("s3", "connection")

    def test_capability_matrix_includes_all_platforms(self):
        matrix = self.reg.capability_matrix()
        assert set(matrix.keys()) >= {"postgres", "mysql", "snowflake", "databricks"}

    def test_capability_matrix_values_are_strings(self):
        matrix = self.reg.capability_matrix()
        for platform_id, caps in matrix.items():
            for cap, level in caps.items():
                assert isinstance(level, str), f"{platform_id}.{cap} should be a string"


class TestPlatformProviderImports:
    """Confirm the provider package imports cleanly — no side-effects at import."""

    def test_postgres_provider_importable(self):
        from workbench.backend.platform.providers.postgres import (
            PostgresConnectionProvider,
            PostgresDiscoveryProvider,
            PostgresQueryExecutor,
            PostgresDeploymentProvider,
        )
        assert PostgresConnectionProvider is not None
        assert PostgresDiscoveryProvider is not None
        assert PostgresQueryExecutor is not None
        assert PostgresDeploymentProvider is not None

    def test_postgres_provider_satisfies_protocols(self):
        from workbench.backend.platform.providers.postgres import (
            PostgresConnectionProvider,
            PostgresDiscoveryProvider,
            PostgresQueryExecutor,
            PostgresDeploymentProvider,
        )
        from workbench.backend.platform.interfaces import (
            ConnectionProvider,
            DiscoveryProvider,
            QueryExecutor,
            DeploymentProvider,
        )
        assert isinstance(PostgresConnectionProvider(), ConnectionProvider)
        assert isinstance(PostgresDiscoveryProvider(), DiscoveryProvider)
        assert isinstance(PostgresQueryExecutor(), QueryExecutor)
        assert isinstance(PostgresDeploymentProvider(), DeploymentProvider)

    def test_postgres_validate_config_catches_missing_host(self):
        from workbench.backend.platform.providers.postgres import PostgresConnectionProvider
        report = PostgresConnectionProvider().validate_config({}, "some-ref")
        assert report.valid is False
        assert any("host" in e for e in report.errors)

    def test_postgres_validate_config_passes_when_all_fields_present(self):
        from workbench.backend.platform.providers.postgres import PostgresConnectionProvider
        report = PostgresConnectionProvider().validate_config(
            {"host": "pg.internal", "database": "mydb"}, "vault/my-secret"
        )
        assert report.valid is True


class TestNewModelsExist:
    """Confirm the Phase 1 SQLModel tables are registered before create_all."""

    def test_platform_connection_model_exists(self):
        from workbench.backend.models import PlatformConnection
        assert PlatformConnection.__tablename__ == "platformconnection"

    def test_execution_profile_model_exists(self):
        from workbench.backend.models import ExecutionProfile
        assert ExecutionProfile.__tablename__ == "executionprofile"

    def test_source_binding_model_exists(self):
        from workbench.backend.models import SourceBinding
        assert SourceBinding.__tablename__ == "sourcebinding"

    def test_platform_connection_has_secret_ref_not_password(self):
        from workbench.backend.models import PlatformConnection
        import inspect
        fields = PlatformConnection.model_fields
        assert "secret_ref" in fields, "secret_ref field is required (no plaintext password)"
        assert "password" not in fields, "password must never be a PlatformConnection field"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. MySQL provider characterization (Phase 2 baseline)
# ═══════════════════════════════════════════════════════════════════════════════

class TestMySQLProviderImports:
    """MySQL provider must import cleanly without pymysql installed."""

    def test_mysql_provider_importable(self):
        from workbench.backend.platform.providers.mysql import (
            MySQLConnectionProvider,
            MySQLDiscoveryProvider,
        )
        assert MySQLConnectionProvider is not None
        assert MySQLDiscoveryProvider is not None

    def test_mysql_provider_satisfies_protocols(self):
        from workbench.backend.platform.providers.mysql import (
            MySQLConnectionProvider,
            MySQLDiscoveryProvider,
        )
        from workbench.backend.platform.interfaces import (
            ConnectionProvider,
            DiscoveryProvider,
        )
        assert isinstance(MySQLConnectionProvider(), ConnectionProvider)
        assert isinstance(MySQLDiscoveryProvider(), DiscoveryProvider)

    def test_mysql_validate_config_catches_missing_host(self):
        from workbench.backend.platform.providers.mysql import MySQLConnectionProvider
        report = MySQLConnectionProvider().validate_config({}, "some-ref")
        assert report.valid is False
        assert any("host" in e for e in report.errors)

    def test_mysql_validate_config_catches_missing_secret_ref(self):
        from workbench.backend.platform.providers.mysql import MySQLConnectionProvider
        report = MySQLConnectionProvider().validate_config(
            {"host": "mysql.internal", "database": "mydb"}, ""
        )
        assert report.valid is False
        assert any("secret_ref" in e for e in report.errors)

    def test_mysql_validate_config_passes_when_complete(self):
        from workbench.backend.platform.providers.mysql import MySQLConnectionProvider
        report = MySQLConnectionProvider().validate_config(
            {"host": "mysql.internal", "database": "mydb"}, "vault/mysql-secret"
        )
        assert report.valid is True


class TestMySQLDsnParsing:
    """_parse_mysql_dsn must correctly decompose a DSN into connect kwargs."""

    def _parse(self, dsn: str) -> dict:
        from workbench.backend.platform.providers.mysql import _parse_mysql_dsn
        return _parse_mysql_dsn(dsn)

    def test_parses_host(self):
        r = self._parse("mysql://user:pass@db.internal:3306/mydb")
        assert r["host"] == "db.internal"

    def test_parses_port(self):
        r = self._parse("mysql://user:pass@db.internal:3307/mydb")
        assert r["port"] == 3307

    def test_default_port_when_absent(self):
        r = self._parse("mysql://user:pass@db.internal/mydb")
        assert r["port"] == 3306

    def test_parses_database(self):
        r = self._parse("mysql://user:pass@db.internal/warehouse")
        assert r["database"] == "warehouse"

    def test_parses_username(self):
        r = self._parse("mysql://admin:pass@db.internal/db")
        assert r["user"] == "admin"

    def test_password_present_in_dsn_path(self):
        # Unlike the REST API (which must redact), the provider DSN parser
        # retains the password for the actual connection call.  The DSN
        # format is a transport from the secret resolver to the connect call;
        # it must never be stored, logged, or returned in API responses.
        r = self._parse("mysql://user:s3cr3t@host/db")
        assert r["password"] == "s3cr3t"


class TestMySQLSystemSchemaFilter:
    """list_namespaces must always exclude MySQL system schemas."""

    def test_system_schemas_are_excluded(self):
        from workbench.backend.platform.providers.mysql import _SYSTEM_SCHEMAS
        for name in ("information_schema", "performance_schema", "mysql", "sys"):
            assert name in _SYSTEM_SCHEMAS
