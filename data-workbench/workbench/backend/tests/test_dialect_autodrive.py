"""Tests for Phase 4 dialect auto-derivation from SourceBinding platform.

Coverage:
- _PLATFORM_DIALECT_MAP maps all known platforms to correct dialect strings
- _resolve_platform_context: Snowflake/Databricks/BigQuery/MySQL bindings inject
  "dialect" into the returned context for serving_virtual_view
- _resolve_platform_context: Postgres binding does NOT inject dialect (no-op)
- _resolve_platform_context: non-serving stages do NOT inject dialect
- _resolve_platform_context: serving_virtual_view skips connection_string build
- build_prompt: platform-derived dialect overrides the "postgres" default
- build_prompt: explicit non-default dialect in stage_config is preserved
- build_prompt: no dialect override when platform_context has no "dialect"
"""
from __future__ import annotations

import types
import unittest.mock as mock

import pytest

from workbench.backend.stage_execution import _PLATFORM_DIALECT_MAP, _PLATFORM_ROUTED_STAGES
from workbench.backend.pipeline import build_prompt


# ── _PLATFORM_DIALECT_MAP ─────────────────────────────────────────────────────


class TestPlatformDialectMap:
    def test_postgres_maps_to_postgres(self):
        assert _PLATFORM_DIALECT_MAP["postgres"] == "postgres"

    def test_postgresql_maps_to_postgres(self):
        assert _PLATFORM_DIALECT_MAP["postgresql"] == "postgres"

    def test_snowflake_maps_to_snowflake(self):
        assert _PLATFORM_DIALECT_MAP["snowflake"] == "snowflake"

    def test_databricks_maps_to_databricks(self):
        assert _PLATFORM_DIALECT_MAP["databricks"] == "databricks"

    def test_bigquery_maps_to_bigquery(self):
        assert _PLATFORM_DIALECT_MAP["bigquery"] == "bigquery"

    def test_mysql_maps_to_mysql(self):
        # Phase 7 reconcile: MySQL now routes to the real MySQLDialect (was "ansi").
        assert _PLATFORM_DIALECT_MAP["mysql"] == "mysql"

    def test_serving_virtual_view_in_platform_routed_stages(self):
        assert "serving_virtual_view" in _PLATFORM_ROUTED_STAGES


# ── _resolve_platform_context: dialect injection ──────────────────────────────


def _make_binding_and_conn(platform_type: str, project_id: int = 1):
    """Return mock (binding, conn) objects for a given platform."""
    conn = mock.MagicMock()
    conn.platform_type = platform_type
    conn.host = "h"
    conn.port = 5432
    conn.database = "db"
    conn.username = "u"
    conn.secret_ref = "TEST_SECRET"
    conn.extra_config_json = "{}"
    conn.id = 42

    binding = mock.MagicMock()
    binding.project_id = project_id
    binding.connection_id = 42
    binding.target_dialect = ""  # no explicit override; dialect is derived from platform_type
    binding.view_target_namespace = ""  # no deploy namespace; keeps postgres a no-op

    return binding, conn


def _call_resolve(stage_id: str, platform_type: str) -> dict | None:
    """Call _resolve_platform_context with a mocked session."""
    from workbench.backend.stage_execution import _resolve_platform_context

    project = mock.MagicMock()
    project.id = 1

    binding, conn = _make_binding_and_conn(platform_type)

    session = mock.MagicMock()
    session.exec.return_value.first.return_value = binding
    # session.get is called for BOTH the MaterializationTarget (no target row here)
    # and the PlatformConnection. Distinguish by model so view_target_namespace
    # stays empty — otherwise a truthy mock namespace defeats the postgres no-op.
    from workbench.backend.models import MaterializationTarget as _MT
    session.get.side_effect = lambda model, _key=None: None if model is _MT else conn

    # resolve_secret is a local import inside _resolve_platform_context —
    # patch it at its source module.  _build_conn_str is a module-level alias
    # and can be patched on stage_execution directly.
    with (
        mock.patch(
            "workbench.backend.platform.secrets.resolve_secret",
            return_value="pw",
        ),
        mock.patch(
            "workbench.backend.stage_execution._build_conn_str",
            return_value="mocked://conn",
        ),
    ):
        return _resolve_platform_context(stage_id, project, session)


# --- serving_virtual_view: dialect injected for non-Postgres platforms --------


class TestResolveContextDialectInjection:

    def test_snowflake_serving_injects_dialect_snowflake(self):
        ctx = _call_resolve("serving_virtual_view", "snowflake")
        assert ctx is not None
        assert ctx.get("dialect") == "snowflake"

    def test_databricks_serving_injects_dialect_databricks(self):
        ctx = _call_resolve("serving_virtual_view", "databricks")
        assert ctx["dialect"] == "databricks"

    def test_bigquery_serving_injects_dialect_bigquery(self):
        ctx = _call_resolve("serving_virtual_view", "bigquery")
        assert ctx["dialect"] == "bigquery"

    def test_mysql_serving_injects_dialect_mysql(self):
        # Phase 7 reconcile: MySQL serving now emits the MySQLDialect (was "ansi").
        ctx = _call_resolve("serving_virtual_view", "mysql")
        assert ctx["dialect"] == "mysql"

    def test_postgres_serving_does_not_inject_dialect(self):
        """Postgres SourceBinding must not inject dialect — it's already the default."""
        ctx = _call_resolve("serving_virtual_view", "postgres")
        # ctx may be None (no useful override) or not contain "dialect"
        assert ctx is None or "dialect" not in ctx

    def test_postgresql_alias_serving_does_not_inject_dialect(self):
        ctx = _call_resolve("serving_virtual_view", "postgresql")
        assert ctx is None or "dialect" not in ctx

    def test_serving_context_contains_platform_type(self):
        ctx = _call_resolve("serving_virtual_view", "snowflake")
        assert ctx["platform_type"] == "snowflake"

    def test_serving_context_does_not_contain_connection_string(self):
        """serving_virtual_view reads from Neo4j — no connection string needed."""
        ctx = _call_resolve("serving_virtual_view", "snowflake")
        assert "connection_string" not in (ctx or {})

    # --- non-serving stages: no dialect key ----------------------------------

    def test_discovery_stage_no_dialect(self):
        ctx = _call_resolve("data_discovery", "snowflake")
        # discovery may return a context (with connection_string), but no dialect
        assert ctx is None or "dialect" not in ctx

    def test_profiling_stage_no_dialect(self):
        ctx = _call_resolve("data_profiling", "databricks")
        assert ctx is None or "dialect" not in ctx

    def test_non_routed_stage_returns_none(self):
        ctx = _call_resolve("data_mapping", "snowflake")
        assert ctx is None

    # --- no SourceBinding: returns None --------------------------------------

    def test_no_binding_returns_none(self):
        from workbench.backend.stage_execution import _resolve_platform_context

        project = mock.MagicMock()
        project.id = 1
        project.project_code = "cf-proj"
        project.pg_connection = ""  # genuinely no connection anywhere

        session = mock.MagicMock()
        session.exec.return_value.first.return_value = None  # no binding
        session.get.return_value = None  # no MaterializationTarget row

        # Borrow walk resolves to Postgres (default) → no useful dialect override.
        with mock.patch(
            "workbench.backend.pg_resolver.resolve_source_connection_for_project",
            return_value=("postgres", {"pg_connection": ""}, None),
        ):
            result = _resolve_platform_context("serving_virtual_view", project, session)
        assert result is None


# ── dpe-cf: no local SourceBinding, dialect borrowed via :CONSUMES ─────────────


def _call_resolve_no_binding(stage_id: str, borrowed_platform: str) -> dict | None:
    """_resolve_platform_context for a consumer project with no SourceBinding,
    where the :CONSUMES borrow resolves to ``borrowed_platform``."""
    from workbench.backend.stage_execution import _resolve_platform_context

    project = mock.MagicMock()
    project.id = 7
    project.project_code = "cf-proj"

    session = mock.MagicMock()
    session.exec.return_value.first.return_value = None  # no SourceBinding
    session.get.return_value = None  # no MaterializationTarget row

    with mock.patch(
        "workbench.backend.pg_resolver.resolve_source_connection_for_project",
        return_value=(borrowed_platform, {"pg_connection": ""}, "src-proj"),
    ):
        return _resolve_platform_context(stage_id, project, session)


class TestConsumerBorrowedDialect:
    def test_mysql_source_borrowed_as_mysql(self):
        # Phase 7 reconcile: a consumer borrowing a MySQL source serves via the
        # MySQLDialect (was "ansi").
        ctx = _call_resolve_no_binding("serving_virtual_view", "mysql")
        assert ctx is not None
        assert ctx.get("dialect") == "mysql"

    def test_snowflake_source_borrowed(self):
        ctx = _call_resolve_no_binding("serving_virtual_view", "snowflake")
        assert ctx["dialect"] == "snowflake"

    def test_databricks_source_borrowed(self):
        ctx = _call_resolve_no_binding("serving_virtual_view", "databricks")
        assert ctx["dialect"] == "databricks"

    def test_postgres_source_borrowed_is_noop(self):
        """Consumer borrowing a Postgres source → no override (postgres default)."""
        ctx = _call_resolve_no_binding("serving_virtual_view", "postgres")
        assert ctx is None

    def test_lakehouse_export_also_borrows_dialect(self):
        ctx = _call_resolve_no_binding("serving_lakehouse_export", "mysql")
        assert ctx is not None
        assert ctx.get("dialect") == "mysql"


# ── quote_relation helper ─────────────────────────────────────────────────────


class TestQuoteRelation:
    def test_postgres_double_quotes(self):
        from workbench.backend.sql_ident import quote_relation
        assert quote_relation("public", "vw_x", "postgres") == '"public"."vw_x"'

    def test_mysql_backticks(self):
        from workbench.backend.sql_ident import quote_relation
        assert quote_relation("public", "vw_x", "mysql") == "`public`.`vw_x`"

    def test_databricks_backticks(self):
        from workbench.backend.sql_ident import quote_relation
        assert quote_relation("db", "t", "databricks") == "`db`.`t`"

    def test_snowflake_double_quotes(self):
        from workbench.backend.sql_ident import quote_relation
        assert quote_relation("s", "t", "snowflake") == '"s"."t"'

    def test_ansi_and_unknown_default_to_double_quotes(self):
        from workbench.backend.sql_ident import quote_relation
        assert quote_relation("s", "t", "ansi") == '"s"."t"'
        assert quote_relation("s", "t", None) == '"s"."t"'

    def test_bare_relation_when_no_schema(self):
        from workbench.backend.sql_ident import quote_relation
        assert quote_relation("", "t", "postgres") == '"t"'
        assert quote_relation(None, "t", "mysql") == "`t`"

    def test_quote_ident(self):
        from workbench.backend.sql_ident import quote_ident
        assert quote_ident("x", "postgres") == '"x"'
        assert quote_ident("x", "mysql") == "`x`"


# ── dialect_sql.render_for_platform: whole-statement neutral→target render ─────
#
# quote_relation (above) quotes a pre-built (schema, view) pair. render_for_platform
# is the complementary helper: it re-renders a whole LLM-generated standard-SQL
# statement to the target engine before execution.


class TestRenderForPlatform:
    def test_standard_to_mysql_backticks(self):
        from workbench.backend import dialect_sql
        r = dialect_sql.render_for_platform('SELECT COUNT(*) FROM "public"."vw_x"', "mysql")
        assert r.ok
        assert "`public`.`vw_x`" in r.sql

    def test_postgres_noop_keeps_double_quotes(self):
        from workbench.backend import dialect_sql
        r = dialect_sql.render_for_platform('SELECT "a" FROM "s"."t"', "postgres")
        assert r.ok
        assert '"s"."t"' in r.sql

    def test_unknown_platform_fails_closed(self):
        from workbench.backend import dialect_sql
        with pytest.raises(KeyError):
            dialect_sql.render_for_platform('SELECT 1', "oracle")


# ── build_prompt: dialect injection via platform_context ─────────────────────


def _minimal_stage(stage_id: str = "serving_virtual_view") -> dict:
    """A minimal stage dict with just enough for build_prompt."""
    return {
        "stage_id": stage_id,
        "prompt_template": "Run DDL with --dialect {dialect}.",
        "skill": None,
    }


def _minimal_project():
    p = mock.MagicMock()
    p.pg_connection = "postgresql://u:p@h:5432/db"
    p.neo4j_host = "localhost"
    p.neo4j_port = 7687
    p.neo4j_user = "neo4j"
    p.neo4j_password = "test"
    p.neo4j_database = "neo4j"
    p.domain = "Test"
    p.project_code = "test-proj"
    p.archetype = "dpe-sa"
    return p


class TestBuildPromptDialectOverride:

    def test_platform_context_snowflake_overrides_default(self):
        prompt = build_prompt(
            _minimal_stage(),
            _minimal_project(),
            stage_config={"dialect": "postgres"},   # default value
            platform_context={"dialect": "snowflake"},
        )
        assert "snowflake" in prompt
        assert "postgres" not in prompt.split("--dialect")[1].split(".")[0]

    def test_platform_context_databricks_overrides_default(self):
        prompt = build_prompt(
            _minimal_stage(),
            _minimal_project(),
            stage_config={"dialect": "postgres"},
            platform_context={"dialect": "databricks"},
        )
        assert "databricks" in prompt

    def test_explicit_non_default_dialect_not_overridden(self):
        """If the engineer explicitly chose 'snowflake', keep it even if platform_context says 'databricks'."""
        prompt = build_prompt(
            _minimal_stage(),
            _minimal_project(),
            stage_config={"dialect": "snowflake"},
            platform_context={"dialect": "databricks"},
        )
        # stage_config "snowflake" != "postgres" → platform_context.dialect skipped
        assert "snowflake" in prompt
        assert "databricks" not in prompt

    def test_no_platform_context_uses_stage_config(self):
        prompt = build_prompt(
            _minimal_stage(),
            _minimal_project(),
            stage_config={"dialect": "bigquery"},
            platform_context=None,
        )
        assert "bigquery" in prompt

    def test_no_platform_context_no_stage_config_defaults_to_postgres(self):
        prompt = build_prompt(
            _minimal_stage(),
            _minimal_project(),
            stage_config=None,
            platform_context=None,
        )
        assert "postgres" in prompt

    def test_platform_context_without_dialect_key_no_change(self):
        """platform_context with only connection_string leaves dialect alone."""
        prompt = build_prompt(
            _minimal_stage(),
            _minimal_project(),
            stage_config={"dialect": "postgres"},
            platform_context={"connection_string": "mysql://h/db"},  # no "dialect" key
        )
        assert "postgres" in prompt

    def test_platform_context_ansi_dialect_overrides_default(self):
        prompt = build_prompt(
            _minimal_stage(),
            _minimal_project(),
            stage_config={"dialect": "postgres"},
            platform_context={"dialect": "ansi"},
        )
        assert "ansi" in prompt
