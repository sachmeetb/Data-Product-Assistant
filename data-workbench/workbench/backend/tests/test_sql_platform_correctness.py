"""Smoke tests for multi-platform SQL correctness.

These tests cover bugs discovered during the MySQL HR sample audit and the
multi-platform expansion (Snowflake, Databricks, MySQL).  They are written
against the DESIRED post-fix behaviour and are marked ``xfail`` for every
fix that has not yet been applied to the source.

Tests that are already correct in the current codebase run as normal tests.

Bugs tracked:
  - Bug #3:  Databricks CREATE SCHEMA unquoted identifier
  - Bug #4:  ESCAPE clause cross-platform safety (backslash → !)
  - Bug #5:  _quote_ident platform-aware identifier quoting
  - Bug #6:  normalize_whitespace decorator dialect dispatch
  - Bug #7:  multi-source concat dialect dispatch (str_concat)
  - Bug #8:  _compile_hash TEXT cast dialect dispatch
  - Issue #9:  ILIKE vs LIKE for Snowflake (regexp_replace already correct)
  - Issue #10: NULLS LAST omitted for MySQL (nulls_last property)
  - Bug #14: MySQL DDL header rewrite handles unquoted identifiers

No live database is required — all tests are pure Python logic / SQL string
generation checks.
"""

from __future__ import annotations

import importlib.util
import inspect
import pathlib
import re

import pytest

# ---------------------------------------------------------------------------
# Load generate_view_ddl.py as a standalone module (same pattern as
# test_view_ddl_bridges.py — it's a script, not a package module).
# ---------------------------------------------------------------------------
_GEN = pathlib.Path(__file__).resolve().parents[3] / (
    "workbench-skills/skills/data-serving-virtual-view/scripts/generate_view_ddl.py"
)
_spec = importlib.util.spec_from_file_location("generate_view_ddl", _GEN)
_g = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_g)

# ---------------------------------------------------------------------------
# value_resolution and sql_executor are ordinary package imports — the
# conftest.py already boots the backend with a test SQLite DB so these
# import cleanly.
# ---------------------------------------------------------------------------
from workbench.backend import value_resolution as vr  # noqa: E402
from workbench.backend import sql_executor             # noqa: E402


# ===========================================================================
# Group 1 — sql_executor._rewrite_ddl_header_for_mysql  (Bug #14)
# ===========================================================================

@pytest.mark.xfail(
    reason="Bug #14: _rewrite_ddl_header_for_mysql not yet implemented in sql_executor",
    strict=False,
)
def test_mysql_ddl_rewrite_unquoted():
    """Unquoted schema.view identifiers must be backtick-quoted for MySQL."""
    result = sql_executor._rewrite_ddl_header_for_mysql(
        "CREATE OR REPLACE VIEW hr_product.vw_employees AS SELECT 1"
    )
    assert "`hr_product`.`vw_employees`" in result, (
        f"Expected backtick quoting of unquoted schema.view: {result}"
    )


@pytest.mark.xfail(
    reason="Bug #14: _rewrite_ddl_header_for_mysql not yet implemented in sql_executor",
    strict=False,
)
def test_mysql_ddl_rewrite_double_quoted():
    """Double-quoted Postgres-style identifiers must be rewritten to backticks."""
    result = sql_executor._rewrite_ddl_header_for_mysql(
        'CREATE OR REPLACE VIEW "hr_product"."vw_employees" AS SELECT 1'
    )
    assert "`hr_product`.`vw_employees`" in result, (
        f"Expected backtick quoting of double-quoted schema.view: {result}"
    )


# ===========================================================================
# Group 2 — value_resolution._quote_ident  (Bug #5)
# ===========================================================================

@pytest.mark.xfail(
    reason="Bug #5: _quote_ident does not accept a platform argument yet",
    strict=False,
)
def test_quote_ident_postgres():
    assert vr._quote_ident("my_view", "postgres") == '"my_view"'


@pytest.mark.xfail(
    reason="Bug #5: _quote_ident does not accept a platform argument yet",
    strict=False,
)
def test_quote_ident_snowflake():
    assert vr._quote_ident("MY_VIEW", "snowflake") == '"MY_VIEW"'


@pytest.mark.xfail(
    reason="Bug #5: _quote_ident does not accept a platform argument yet; "
           "MySQL should use backticks",
    strict=False,
)
def test_quote_ident_mysql():
    assert vr._quote_ident("my_view", "mysql") == "`my_view`"


@pytest.mark.xfail(
    reason="Bug #5: _quote_ident does not accept a platform argument yet; "
           "Databricks should use backticks",
    strict=False,
)
def test_quote_ident_databricks():
    assert vr._quote_ident("my_view", "databricks") == "`my_view`"


@pytest.mark.xfail(
    reason="Bug #5: _quote_ident does not accept a platform argument yet; "
           "backtick in identifier name must be doubled",
    strict=False,
)
def test_quote_ident_backtick_escape():
    assert vr._quote_ident("my`view", "mysql") == "`my``view`"


# ===========================================================================
# Group 3 — value_resolution._ilike_pattern_literal  (Bug #4)
# ===========================================================================

def test_ilike_pattern_literal_basic():
    """Core contract: returns a %token% pattern literal.  Passes now."""
    result = vr._ilike_pattern_literal("John")
    assert "%John%" in result, f"Expected %John% in: {result}"


@pytest.mark.xfail(
    reason="Bug #4: _ilike_pattern_literal still uses backslash as ESCAPE char; "
           "% in token should be escaped with ! not \\",
    strict=False,
)
def test_ilike_pattern_literal_escapes_percent():
    result = vr._ilike_pattern_literal("100%")
    assert "!%" in result, f"Expected !% escape in result: {result}"


@pytest.mark.xfail(
    reason="Bug #4: _ilike_pattern_literal still uses backslash as ESCAPE char; "
           "_ in token should be escaped with ! not \\",
    strict=False,
)
def test_ilike_pattern_literal_escapes_underscore():
    result = vr._ilike_pattern_literal("first_name")
    assert "!_" in result, f"Expected !_ escape in result: {result}"


@pytest.mark.xfail(
    reason="Bug #4: ! in token not yet doubled to !! for the ! escape scheme",
    strict=False,
)
def test_ilike_pattern_literal_escapes_exclamation():
    result = vr._ilike_pattern_literal("hey!")
    assert "!!" in result, f"Expected !! in result: {result}"


# ===========================================================================
# Group 4a — generate_view_ddl Dialect basics (already correct)
# ===========================================================================

def test_get_dialect_known_names_return_correct_instances():
    """get_dialect returns the right subclass for the four supported platforms."""
    assert _g.get_dialect("postgres").name == "postgres"
    assert _g.get_dialect("snowflake").name == "snowflake"
    assert _g.get_dialect("databricks").name == "databricks"
    # postgresql alias must also work
    assert _g.get_dialect("postgresql").name == "postgres"


def test_postgres_regexp_replace_global_has_g_flag():
    """Postgres REGEXP_REPLACE must carry the explicit 'g' flag."""
    d = _g.get_dialect("postgres")
    result = d.regexp_replace_global("col", r"'\\s+'", "' '")
    assert "'g'" in result, f"Postgres REGEXP_REPLACE missing 'g' flag: {result}"


def test_snowflake_regexp_replace_global_no_g_flag():
    """Snowflake REGEXP_REPLACE is global by default — must NOT include 'g'."""
    d = _g.get_dialect("snowflake")
    result = d.regexp_replace_global("col", r"'\\s+'", "' '")
    assert "'g'" not in result, f"Snowflake REGEXP_REPLACE must not have 'g': {result}"
    assert "REGEXP_REPLACE" in result


def test_databricks_regexp_replace_global_no_g_flag():
    """Databricks REGEXP_REPLACE is global by default — must NOT include 'g'."""
    d = _g.get_dialect("databricks")
    result = d.regexp_replace_global("col", r"'\\s+'", "' '")
    assert "'g'" not in result, f"Databricks REGEXP_REPLACE must not have 'g': {result}"
    assert "REGEXP_REPLACE" in result


def test_normalize_whitespace_default_produces_postgres_style():
    """Without a dialect param, _apply_decorators emits the Postgres 'g' form.

    This tests the CURRENT behavior (no regression after the dialect param is
    added — the no-arg form must still default to Postgres style).
    """
    result = _g._apply_decorators(
        "col", '{"standardization": ["normalize_whitespace"]}'
    )
    assert "REGEXP_REPLACE" in result
    assert "'g'" in result, f"Default (Postgres) normalize_whitespace missing 'g': {result}"


# ===========================================================================
# Group 4b — Dialect.string_type  (Bug #8 / Issue: platform cast type)
# ===========================================================================

@pytest.mark.xfail(
    reason="string_type attribute not yet defined on Dialect classes",
    strict=False,
)
def test_dialect_string_type_postgres():
    d = _g.get_dialect("postgres")
    assert d.string_type == "TEXT"


@pytest.mark.xfail(
    reason="string_type attribute not yet defined on Dialect classes",
    strict=False,
)
def test_dialect_string_type_snowflake():
    d = _g.get_dialect("snowflake")
    # Snowflake prefers VARCHAR; TEXT is not a native type name there.
    assert d.string_type in ("VARCHAR", "TEXT"), (
        f"Snowflake string_type should be VARCHAR, got: {d.string_type}"
    )


@pytest.mark.xfail(
    reason="string_type attribute not yet defined on Dialect classes",
    strict=False,
)
def test_dialect_string_type_databricks():
    d = _g.get_dialect("databricks")
    assert d.string_type == "STRING"


# ===========================================================================
# Group 4c — Dialect.nulls_last  (Issue #10)
# ===========================================================================

@pytest.mark.xfail(
    reason="nulls_last attribute not yet defined on Dialect classes",
    strict=False,
)
def test_dialect_nulls_last_postgres():
    d = _g.get_dialect("postgres")
    assert "NULLS LAST" in d.nulls_last


@pytest.mark.xfail(
    reason="nulls_last attribute not yet defined on Dialect classes; "
           "MySQL does not support NULLS LAST so value should be empty string",
    strict=False,
)
def test_dialect_nulls_last_mysql():
    # After the fix a MysqlDialect will be registered; until then get_dialect
    # falls back to PostgresDialect, so the test fails on the nulls_last value.
    d = _g.get_dialect("mysql")
    assert d.nulls_last == "", (
        f"MySQL must have empty nulls_last (unsupported syntax), got: '{d.nulls_last}'"
    )


# ===========================================================================
# Group 4d — _compile_hash dialect-aware TEXT cast  (Bug #8)
# ===========================================================================

def _hash_mapping():
    """Minimal mapping dict suitable for _compile_hash."""
    return {
        "source_schema": "hr",
        "source_table": "employees",
        "source_col": "last_name",
        "transform_params_json": '{"algorithm": "md5"}',
        "transform_decorators_json": None,
    }


@pytest.mark.xfail(
    reason="Bug #8: _compile_hash still casts to TEXT even for Snowflake; "
           "should cast to VARCHAR",
    strict=False,
)
def test_compile_hash_snowflake_uses_varchar_not_text():
    d = _g.get_dialect("snowflake")
    result = _g._compile_hash(
        _hash_mapping(), {"hr.employees": "t1"}, dialect=d
    )
    assert "TEXT" not in result, (
        f"Snowflake _compile_hash must not cast to TEXT: {result}"
    )
    # After the fix at least one of these will be present.
    assert "VARCHAR" in result or "STRING" in result or "md5" in result.lower()


@pytest.mark.xfail(
    reason="Bug #8: _compile_hash still casts to TEXT even for Databricks; "
           "should cast to STRING",
    strict=False,
)
def test_compile_hash_databricks_uses_string_not_text():
    d = _g.get_dialect("databricks")
    result = _g._compile_hash(
        _hash_mapping(), {"hr.employees": "t1"}, dialect=d
    )
    assert "TEXT" not in result, (
        f"Databricks _compile_hash must not cast to TEXT: {result}"
    )


# ===========================================================================
# Group 4e — _apply_decorators dialect dispatch  (Bug #6)
# ===========================================================================

@pytest.mark.xfail(
    reason="Bug #6: _apply_decorators does not yet accept a dialect keyword arg",
    strict=False,
)
def test_normalize_whitespace_postgres_dialect_has_g_flag():
    d = _g.get_dialect("postgres")
    result = _g._apply_decorators(
        "col", '{"standardization": ["normalize_whitespace"]}', dialect=d
    )
    assert "'g'" in result, (
        f"Postgres normalize_whitespace must emit 'g' flag: {result}"
    )


@pytest.mark.xfail(
    reason="Bug #6: _apply_decorators does not yet accept a dialect keyword arg",
    strict=False,
)
def test_normalize_whitespace_databricks_dialect_no_g_flag():
    d = _g.get_dialect("databricks")
    result = _g._apply_decorators(
        "col", '{"standardization": ["normalize_whitespace"]}', dialect=d
    )
    assert "'g'" not in result, (
        f"Databricks normalize_whitespace must NOT emit 'g' flag: {result}"
    )
    assert "REGEXP_REPLACE" in result


@pytest.mark.xfail(
    reason="Bug #6: _apply_decorators does not yet accept a dialect keyword arg",
    strict=False,
)
def test_normalize_whitespace_snowflake_dialect_no_g_flag():
    d = _g.get_dialect("snowflake")
    result = _g._apply_decorators(
        "col", '{"standardization": ["normalize_whitespace"]}', dialect=d
    )
    assert "'g'" not in result, (
        f"Snowflake normalize_whitespace must NOT emit 'g' flag: {result}"
    )


# ===========================================================================
# Group 4f — Dialect.str_concat  (Bug #7)
# ===========================================================================

@pytest.mark.xfail(
    reason="Bug #7: str_concat method not yet defined on Dialect classes",
    strict=False,
)
def test_str_concat_postgres_uses_pipe_operator():
    d = _g.get_dialect("postgres")
    result = d.str_concat(["t1.a", "t2.b"], sep=" ")
    assert "||" in result, f"Postgres concat must use || operator: {result}"
    assert "CONCAT" not in result, (
        f"Postgres concat must not use CONCAT() function: {result}"
    )


@pytest.mark.xfail(
    reason="Bug #7: str_concat method not yet defined on Dialect classes",
    strict=False,
)
def test_str_concat_databricks_uses_concat_function():
    d = _g.get_dialect("databricks")
    result = d.str_concat(["t1.a", "t2.b"], sep=" ")
    assert "CONCAT" in result, f"Databricks concat must use CONCAT() function: {result}"
    assert "||" not in result, (
        f"Databricks concat must not use || operator: {result}"
    )


@pytest.mark.xfail(
    reason="Bug #7: str_concat method not yet defined on Dialect classes; "
           "MysqlDialect not yet registered",
    strict=False,
)
def test_str_concat_mysql_uses_concat_function():
    d = _g.get_dialect("mysql")
    result = d.str_concat(["t1.a", "t2.b"], sep=" ")
    assert "CONCAT" in result, f"MySQL concat must use CONCAT() function: {result}"


# ===========================================================================
# Group 5 — sql_executor Databricks CREATE SCHEMA quoting  (Bug #3)
# ===========================================================================

def test_databricks_schema_create_uses_backtick_quoting():
    """The Databricks deployment provider must backtick-quote the CREATE SCHEMA
    statement (execution moved from sql_executor into the provider in Phase 4).

    Unquoted identifiers are case-folded by Databricks and can silently target
    the wrong schema when the project code contains uppercase letters.
    """
    from workbench.backend.platform.providers.databricks import DatabricksDeploymentProvider
    src = inspect.getsource(DatabricksDeploymentProvider.deploy_views)
    assert "CREATE SCHEMA IF NOT EXISTS `" in src, (
        "Databricks deploy_views must use backtick-quoted CREATE SCHEMA"
    )
