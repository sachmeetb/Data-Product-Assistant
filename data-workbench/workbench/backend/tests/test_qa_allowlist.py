"""Allow-list SQL validator must be namespace-depth agnostic.

The Semantic Q&A / Q&A safety gate parses FROM/JOIN references out of
LLM-generated SQL and checks them against the deployed-view allow-list. It must
handle 3-level names (Databricks/Snowflake ``catalog.schema.view``) as well as
2-level (Postgres ``schema.view``) — capping at two parts truncated a 3-level
name and rejected a legitimately-deployed view ("unsafe_table"). The
security-negative case (an unauthorised table) must STILL be rejected.
"""
import pytest

from workbench.backend.qa_execute import (
    _extract_table_refs,
    _normalise_ref,
    canonicalize_namespace_quoting,
    validate_sql_allowlist_pairs,
)


def test_canonicalize_whole_quoted_namespace():
    # The LLM quoted a 3-level namespace as ONE identifier — normalise it.
    sql = ('SELECT "s" AS state, COUNT(*) FROM "workspace.default"."vw_sales_customers" '
           'GROUP BY 1')
    pairs = [("workspace.default", "vw_sales_customers")]
    fixed = canonicalize_namespace_quoting(sql, pairs)
    assert '"workspace"."default"."vw_sales_customers"' in fixed
    assert '"workspace.default"' not in fixed
    # …and the fixed SQL now passes the allow-list.
    assert validate_sql_allowlist_pairs(fixed, pairs).ok


def test_canonicalize_is_idempotent_and_noop():
    pairs = [("workspace.default", "vw_x")]
    correct = 'SELECT * FROM "workspace"."default"."vw_x"'
    assert canonicalize_namespace_quoting(correct, pairs) == correct        # already correct
    twice = canonicalize_namespace_quoting(
        canonicalize_namespace_quoting('SELECT * FROM "workspace.default"."vw_x"', pairs), pairs)
    assert twice == 'SELECT * FROM "workspace"."default"."vw_x"'            # idempotent
    pg = 'SELECT * FROM "public"."vw_x"'
    assert canonicalize_namespace_quoting(pg, [("public", "vw_x")]) == pg    # 2-level untouched


def test_normalise_ref_depth_agnostic():
    assert _normalise_ref('"workspace"."default"."vw_x"') == ("workspace.default", "vw_x")
    assert _normalise_ref('"public"."vw_x"') == ("public", "vw_x")
    assert _normalise_ref("public.vw_x") == ("public", "vw_x")
    assert _normalise_ref("vw_x") == (None, "vw_x")


def test_extract_three_level_ref():
    sql = 'SELECT COUNT(*) FROM "workspace"."default"."vw_sales_customers"'
    assert _extract_table_refs(sql) == ['"workspace"."default"."vw_sales_customers"']


def test_three_level_view_allowed():
    sql = 'SELECT COUNT(*) AS n FROM "workspace"."default"."vw_sales_customers"'
    res = validate_sql_allowlist_pairs(sql, [("workspace.default", "vw_sales_customers")])
    assert res.ok
    assert res.used_views == ["vw_sales_customers"]
    assert res.rejected_refs == []


def test_two_level_still_works():
    sql = 'SELECT * FROM "public"."vw_x" JOIN public.vw_y ON 1=1'
    res = validate_sql_allowlist_pairs(sql, [("public", "vw_x"), ("public", "vw_y")])
    assert res.ok
    assert set(res.used_views) == {"vw_x", "vw_y"}


def test_cte_reference_allowed():
    sql = ('WITH c AS (SELECT * FROM "workspace"."default"."vw_sales_customers") '
           'SELECT * FROM c')
    res = validate_sql_allowlist_pairs(sql, [("workspace.default", "vw_sales_customers")])
    assert res.ok


def test_unauthorised_three_level_still_rejected():
    # Security-negative: a fully-qualified table NOT on the allow-list must be
    # rejected — the depth fix must not weaken the guard.
    sql = 'SELECT ssn FROM "secret"."pii"."ssn_table"'
    res = validate_sql_allowlist_pairs(sql, [("workspace.default", "vw_sales_customers")])
    assert not res.ok
    assert res.rejected_refs == ['"secret"."pii"."ssn_table"']


def test_wrong_catalog_rejected():
    # Right view name, wrong catalog namespace → reject (can't read another
    # catalog's same-named view).
    sql = 'SELECT * FROM "other".  "default"."vw_sales_customers"'
    res = validate_sql_allowlist_pairs(sql, [("workspace.default", "vw_sales_customers")])
    assert not res.ok
