"""Tests for dialect_sql.render_for_platform — the neutral-AST → per-engine
render helper for the semantic-layer query path.

Verifies:
- standard SQL → MySQL yields backtick-quoted identifiers
- snowflake / databricks / bigquery render correctly (quote style)
- postgres is an effective no-op (double-quotes preserved)
- unparseable SQL fails OPEN (returns original + ok=False + error)
- unknown platform fails CLOSED (KeyError)
- empty / whitespace SQL is a safe no-op
"""
from __future__ import annotations

import pytest

from workbench.backend import dialect_sql


# The exact failing query from the plan's motivating bug.
_FAILING = 'SELECT COUNT(*) FROM "public"."vw_employees"'


class TestRenderForPlatform:
    def test_mysql_uses_backticks(self):
        r = dialect_sql.render_for_platform(_FAILING, "mysql")
        assert r.ok
        assert "`public`.`vw_employees`" in r.sql
        assert '"public"' not in r.sql

    def test_databricks_uses_backticks(self):
        r = dialect_sql.render_for_platform(_FAILING, "databricks")
        assert r.ok
        assert "`public`.`vw_employees`" in r.sql

    def test_bigquery_uses_backticks(self):
        r = dialect_sql.render_for_platform(_FAILING, "bigquery")
        assert r.ok
        assert "`public`.`vw_employees`" in r.sql

    def test_snowflake_double_quotes_and_upper_folds(self):
        # Snowflake's NORMALIZATION_STRATEGY is UPPERCASE: identifiers are folded
        # to UPPER so a quoted read addresses the object dlt/dbt/the view path
        # created via unquoted DDL (which Snowflake stores UPPER). sqlglot alone
        # PRESERVES quoted-identifier case, so this is the fix for the motivating
        # "sql_error" (a lowercase-quoted read missing the UPPER physical object).
        r = dialect_sql.render_for_platform(_FAILING, "snowflake")
        assert r.ok
        assert '"PUBLIC"."VW_EMPLOYEES"' in r.sql
        assert '"public"' not in r.sql
        assert "`" not in r.sql

    def test_snowflake_preserves_string_literals(self):
        # Only identifiers fold — a string literal in a WHERE keeps its value.
        r = dialect_sql.render_for_platform(
            "SELECT status FROM party WHERE status = 'active'", "snowflake"
        )
        assert r.ok
        assert "STATUS" in r.sql and "PARTY" in r.sql
        assert "'active'" in r.sql

    def test_postgres_is_noop(self):
        r = dialect_sql.render_for_platform(_FAILING, "postgres")
        assert r.ok
        assert '"public"."vw_employees"' in r.sql

    def test_postgresql_alias(self):
        r = dialect_sql.render_for_platform(_FAILING, "postgresql")
        assert r.ok
        assert '"public"."vw_employees"' in r.sql

    def test_cast_renders_to_mysql_vocabulary(self):
        # Standard CAST(x AS INTEGER) → MySQL uses SIGNED for integer casts.
        r = dialect_sql.render_for_platform(
            'SELECT CAST("x" AS INT) FROM "s"."t"', "mysql"
        )
        assert r.ok
        assert "`s`.`t`" in r.sql

    def test_unparseable_fails_open(self):
        bad = "SELECT FROM"          # a genuine parse error (sqlglot is otherwise lenient)
        r = dialect_sql.render_for_platform(bad, "mysql")
        assert r.ok is False
        assert r.sql == bad          # ORIGINAL returned unchanged (never regress)
        assert r.error               # a soft note explaining the skip

    def test_unknown_platform_fails_closed(self):
        with pytest.raises(KeyError):
            dialect_sql.render_for_platform(_FAILING, "oracle")

    def test_empty_sql_is_noop(self):
        r = dialect_sql.render_for_platform("", "mysql")
        assert r.ok
        assert r.sql == ""

    def test_whitespace_sql_is_noop(self):
        r = dialect_sql.render_for_platform("   ", "mysql")
        assert r.ok


class TestSqlglotDialectFor:
    def test_known_platforms(self):
        assert dialect_sql.sqlglot_dialect_for("mysql") == "mysql"
        assert dialect_sql.sqlglot_dialect_for("postgres") == "postgres"
        assert dialect_sql.sqlglot_dialect_for("postgresql") == "postgres"
        assert dialect_sql.sqlglot_dialect_for("snowflake") == "snowflake"
        assert dialect_sql.sqlglot_dialect_for("databricks") == "databricks"
        assert dialect_sql.sqlglot_dialect_for("bigquery") == "bigquery"
        assert dialect_sql.sqlglot_dialect_for("duckdb") == "duckdb"

    def test_case_insensitive(self):
        assert dialect_sql.sqlglot_dialect_for("MySQL") == "mysql"

    def test_unknown_raises(self):
        with pytest.raises(KeyError):
            dialect_sql.sqlglot_dialect_for("oracle")
