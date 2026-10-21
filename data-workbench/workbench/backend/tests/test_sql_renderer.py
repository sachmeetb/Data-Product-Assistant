"""Tests for the per-platform SQL expression renderers (platform/sql_renderer.py).

Coverage:
- Registry: get_renderer / list_renderers / fail-closed on unknown platform
- Quoting: double-quote for Postgres + Snowflake, backtick for MySQL + Databricks
- 83 golden-path conformance cases (GOLDEN_PATH_FIXTURES × platform)
- NotImplementedError for unimplemented transform_kind / algorithm
- Literal: string, integer, typed NULL
- Arithmetic: simple operator and safe-divide
- Bucket: label assignment algorithm
- CASE WHEN: column-ref substitution in branch conditions
"""
import pytest

from workbench.backend.platform.sql_renderer import (
    BaseSqlRenderer,
    DatabricksSqlRenderer,
    MysqlSqlRenderer,
    PostgresSqlRenderer,
    SnowflakeSqlRenderer,
    get_renderer,
    list_renderers,
)
from workbench.backend.platform.transform_fixtures import (
    GOLDEN_PATH_FIXTURES,
    get_fixture,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _substitute_expected(template: str, cols: list[str], platform_id: str) -> str:
    """Substitute {col}/{col2} placeholders in a fixture expected_sql template.

    The fixture templates already carry the platform-specific quoting characters
    around the placeholder (e.g. ``"{col}"`` for Postgres, `` `{col}` `` for
    MySQL), so we substitute only the bare column name — not a pre-quoted form.
    """
    result = template
    if cols:
        result = result.replace("{col}", cols[0])
    if len(cols) > 1:
        result = result.replace("{col2}", cols[1])
    return result


PG = get_renderer("postgres")
MY = get_renderer("mysql")
SF = get_renderer("snowflake")
DB = get_renderer("databricks")


# ── registry ───────────────────────────────────────────────────────────────────

class TestRendererRegistry:
    def test_all_four_platforms_registered(self):
        assert list_renderers() == ["databricks", "mysql", "postgres", "snowflake"]

    def test_get_renderer_postgres(self):
        r = get_renderer("postgres")
        assert isinstance(r, PostgresSqlRenderer)

    def test_get_renderer_mysql(self):
        assert isinstance(get_renderer("mysql"), MysqlSqlRenderer)

    def test_get_renderer_snowflake(self):
        assert isinstance(get_renderer("snowflake"), SnowflakeSqlRenderer)

    def test_get_renderer_databricks(self):
        assert isinstance(get_renderer("databricks"), DatabricksSqlRenderer)

    def test_unknown_platform_raises_key_error(self):
        with pytest.raises(KeyError, match="No SQL renderer registered"):
            get_renderer("oracle")

    def test_no_fallback_to_postgres(self):
        with pytest.raises(KeyError):
            get_renderer("redshift")

    def test_list_renderers_sorted(self):
        result = list_renderers()
        assert result == sorted(result)

    def test_renderers_are_base_sql_renderer(self):
        for pid in list_renderers():
            assert isinstance(get_renderer(pid), BaseSqlRenderer)


# ── identifier quoting ─────────────────────────────────────────────────────────

class TestIdentifierQuoting:
    def test_postgres_double_quotes(self):
        result = PG.render("cast", {"target_canonical_type": "int32"}, ["my_col"])
        assert '"my_col"' in result

    def test_snowflake_double_quotes(self):
        result = SF.render("cast", {"target_canonical_type": "int32"}, ["my_col"])
        assert '"my_col"' in result

    def test_mysql_backticks(self):
        result = MY.render("cast", {"target_canonical_type": "int32"}, ["my_col"])
        assert "`my_col`" in result

    def test_databricks_backticks(self):
        result = DB.render("cast", {"target_canonical_type": "int32"}, ["my_col"])
        assert "`my_col`" in result


# ── golden-path conformance matrix ────────────────────────────────────────────

@pytest.mark.parametrize(
    "fixture_name,platform_id",
    [
        (f.name, pid)
        for f in GOLDEN_PATH_FIXTURES
        for pid in f.platforms()
    ],
)
def test_renderer_matches_fixture(fixture_name: str, platform_id: str) -> None:
    fix = get_fixture(fixture_name)
    renderer = get_renderer(platform_id)
    result = renderer.render(
        fix.transform_kind,
        fix.transform_params,
        fix.source_columns,
    )
    expected = _substitute_expected(
        fix.expected_sql[platform_id],
        fix.source_columns,
        platform_id,
    )
    assert result == expected, (
        f"\nFixture: {fixture_name!r}  Platform: {platform_id!r}\n"
        f"  expected: {expected!r}\n"
        f"  got:      {result!r}"
    )


# ── error cases ────────────────────────────────────────────────────────────────

class TestNotImplemented:
    def test_unknown_transform_kind_raises(self):
        with pytest.raises(NotImplementedError, match="No renderer for transform_kind"):
            PG.render("window", {}, ["col"])

    def test_mysql_sha256_raises(self):
        with pytest.raises(NotImplementedError):
            MY.render("hash", {"algorithm": "sha256"}, ["email"])

    def test_unknown_mask_kind_raises(self):
        with pytest.raises(NotImplementedError, match="mask_kind"):
            PG.render("mask", {"mask_kind": "phone"}, ["phone_num"])


# ── literal edge cases ────────────────────────────────────────────────────────

class TestLiteralRenderer:
    def test_string_literal_same_on_all_platforms(self):
        for pid in list_renderers():
            r = get_renderer(pid)
            assert r.render("literal", {"value": "X"}, []) == "'X'"

    def test_integer_literal_same_on_all_platforms(self):
        for pid in list_renderers():
            r = get_renderer(pid)
            assert r.render("literal", {"value": 42, "canonical_type": "int32"}, []) == "42"

    def test_null_uses_cast_sql_type(self):
        result = MY.render("literal", {"value": None, "canonical_type": "string"}, [])
        assert result == "CAST(NULL AS CHAR)"

    def test_null_postgres_string(self):
        assert PG.render("literal", {"value": None, "canonical_type": "string"}, []) == "CAST(NULL AS TEXT)"

    def test_null_snowflake_string(self):
        assert SF.render("literal", {"value": None, "canonical_type": "string"}, []) == "CAST(NULL AS VARCHAR)"

    def test_null_databricks_string(self):
        assert DB.render("literal", {"value": None, "canonical_type": "string"}, []) == "CAST(NULL AS STRING)"


# ── arithmetic ─────────────────────────────────────────────────────────────────

class TestArithmeticRenderer:
    def test_multiply(self):
        r = PG.render("arithmetic", {"operator": "*"}, ["qty", "price"])
        assert r == '"qty" * "price"'

    def test_safe_divide_postgres(self):
        r = PG.render(
            "arithmetic",
            {"operator": "/", "safe_divide": True},
            ["num", "denom"],
        )
        assert r == 'CASE WHEN "denom" = 0 THEN NULL ELSE "num" / NULLIF("denom", 0) END'

    def test_safe_divide_mysql(self):
        r = MY.render(
            "arithmetic",
            {"operator": "/", "safe_divide": True},
            ["num", "denom"],
        )
        assert r == "CASE WHEN `denom` = 0 THEN NULL ELSE `num` / NULLIF(`denom`, 0) END"


# ── bucket ─────────────────────────────────────────────────────────────────────

class TestBucketRenderer:
    def test_bucket_label_assignment(self):
        result = PG.render(
            "bucket",
            {"boundaries": [0, 100], "labels": ["low", "high"]},
            ["score"],
        )
        assert result == (
            "CASE WHEN \"score\" < 0 THEN 'low' "
            "WHEN \"score\" < 100 THEN 'low' "
            "ELSE 'high' END"
        )

    def test_bucket_three_bands(self):
        result = DB.render(
            "bucket",
            {"boundaries": [0, 100, 1000], "labels": ["low", "medium", "high"]},
            ["order_value"],
        )
        assert result == (
            "CASE "
            "WHEN `order_value` < 0 THEN 'low' "
            "WHEN `order_value` < 100 THEN 'low' "
            "WHEN `order_value` < 1000 THEN 'medium' "
            "ELSE 'high' END"
        )


# ── concat ─────────────────────────────────────────────────────────────────────

class TestConcatRenderer:
    def test_postgres_uses_pipe_operator(self):
        r = PG.render(
            "concat",
            {"separator": "", "null_as_empty": False},
            ["a", "b"],
        )
        assert "||" in r
        assert "CONCAT" not in r

    def test_mysql_uses_concat_function(self):
        r = MY.render(
            "concat",
            {"separator": "", "null_as_empty": False},
            ["a", "b"],
        )
        assert r.startswith("CONCAT(")

    def test_snowflake_uses_concat_function(self):
        r = SF.render(
            "concat",
            {"separator": "", "null_as_empty": False},
            ["a", "b"],
        )
        assert r.startswith("CONCAT(")

    def test_separator_included_postgres(self):
        r = PG.render(
            "concat",
            {"separator": "-", "null_as_empty": True},
            ["a", "b"],
        )
        assert "'-'" in r
        assert "||" in r

    def test_separator_included_snowflake(self):
        r = SF.render(
            "concat",
            {"separator": "-", "null_as_empty": True},
            ["a", "b"],
        )
        assert "'-'" in r
        assert "CONCAT" in r


# ── CASE WHEN col-ref substitution ────────────────────────────────────────────

class TestCaseRenderer:
    def test_col_ref_substituted_double_quotes(self):
        r = PG.render(
            "case",
            {
                "branches": [{"when": "{col} = 'Y'", "then": "'yes'"}],
                "else_value": "'no'",
            },
            ["flag"],
        )
        assert r == "CASE WHEN \"flag\" = 'Y' THEN 'yes' ELSE 'no' END"

    def test_col_ref_substituted_backtick(self):
        r = MY.render(
            "case",
            {
                "branches": [{"when": "{col} = 'Y'", "then": "'yes'"}],
                "else_value": "'no'",
            },
            ["flag"],
        )
        assert r == "CASE WHEN `flag` = 'Y' THEN 'yes' ELSE 'no' END"

    def test_multiple_branches(self):
        r = PG.render(
            "case",
            {
                "branches": [
                    {"when": "{col} = 1", "then": "'one'"},
                    {"when": "{col} = 2", "then": "'two'"},
                ],
                "else_value": "'other'",
            },
            ["n"],
        )
        assert r == "CASE WHEN \"n\" = 1 THEN 'one' WHEN \"n\" = 2 THEN 'two' ELSE 'other' END"


# ── MySQL cast type overrides ──────────────────────────────────────────────────

class TestMysqlCastTypes:
    """MySQL uses a restricted CAST() type vocabulary that differs from storage types."""

    def test_int32_uses_signed(self):
        r = MY.render("cast", {"target_canonical_type": "int32"}, ["c"])
        assert "SIGNED" in r

    def test_int64_uses_unsigned(self):
        r = MY.render("cast", {"target_canonical_type": "int64"}, ["c"])
        assert "UNSIGNED" in r

    def test_string_uses_char(self):
        r = MY.render("cast", {"target_canonical_type": "string"}, ["c"])
        assert "CHAR" in r
        assert "TEXT" not in r

    def test_timestamp_us_uses_datetime_not_datetime6(self):
        r = MY.render("cast", {"target_canonical_type": "timestamp_us"}, ["c"])
        assert "DATETIME)" in r   # must end with ) not (6)
        assert "DATETIME(6)" not in r

    def test_decimal_with_precision_and_scale(self):
        r = MY.render(
            "cast",
            {"target_canonical_type": "decimal", "precision": 10, "scale": 3},
            ["c"],
        )
        assert "DECIMAL(10,3)" in r


# ── Snowflake transform-capability conformance (Tier 5) ─────────────────────────
# A parse → canonical-name → validate → render table for the common Snowflake
# authoring functions. Validation keys on the sqlglot CANONICAL AST name
# (dialect_sql._func_name), NOT the author-facing spelling — so IFF/ZEROIFNULL →
# IF, DATEADD → DATE_ADD, LISTAGG → GROUP_CONCAT, DECODE → DECODE_CASE, etc.

from workbench.backend.dialect_sql import compile_expression, _func_name  # noqa: E402
from workbench.backend.platform.transform_capabilities import get_capabilities  # noqa: E402
import sqlglot  # noqa: E402


# (author_sql, expected canonical AST name that the artifact must cover)
_SNOWFLAKE_CONFORMANCE = [
    ("IFF(x > 0, a, b)", "IF"),                                  # portable IF
    ("NVL(a, b)", "COALESCE"),                                   # portable COALESCE
    ("NVL2(a, b, c)", "NVL2"),
    ("ZEROIFNULL(a)", "IF"),
    ("NULLIFZERO(a)", "IF"),
    ("DECODE(a, 1, 'x', 'z')", "DECODE_CASE"),
    ("DATEADD('day', 1, d)", "DATE_ADD"),
    ("CONVERT_TIMEZONE('UTC', 'America/Los_Angeles', ts)", "CONVERT_TIMEZONE"),
    ("TO_DATE(s, 'YYYY-MM-DD')", "TS_OR_DS_TO_DATE"),
    ("TO_TIMESTAMP(s)", "TO_TIMESTAMP"),
    ("TO_TIMESTAMP_NTZ(s)", "TO_TIMESTAMP_NTZ"),
    ("TRY_CAST(s AS INT)", "TRY_CAST"),
    ("TRY_TO_NUMBER(s)", "TO_NUMBER"),
    ("PARSE_JSON(s)", "PARSE_JSON"),
    ("LISTAGG(a, ',')", "GROUP_CONCAT"),
]


class TestSnowflakeTransformConformance:
    @pytest.mark.parametrize("author_sql,canonical", _SNOWFLAKE_CONFORMANCE)
    def test_parse_yields_expected_canonical_name(self, author_sql, canonical):
        root = sqlglot.parse_one(author_sql, read="snowflake")
        names = {_func_name(n) for n in root.walk() if _func_name(n)}
        assert canonical in names, f"{author_sql!r} → {sorted(names)}, expected {canonical}"

    @pytest.mark.parametrize("author_sql,canonical", _SNOWFLAKE_CONFORMANCE)
    def test_canonical_name_is_supported_by_artifact(self, author_sql, canonical):
        caps = get_capabilities()
        support = caps.function_support(canonical, "snowflake")
        assert support in ("native", "emulated"), (
            f"{canonical} support on snowflake is {support!r} — expected native/emulated"
        )

    @pytest.mark.parametrize("author_sql,canonical", _SNOWFLAKE_CONFORMANCE)
    def test_compiles_clean_not_unknown_function(self, author_sql, canonical):
        # The end-to-end gate: parse(read=snowflake) → validate → render →
        # re-validate. Must NOT surface unknown_function / unsupported_function.
        r = compile_expression(author_sql, "snowflake", read="snowflake")
        codes = [d.code for d in r.errors]
        assert r.ok, f"{author_sql!r} failed to compile: {codes}"
        assert "unknown_function" not in codes and "unsupported_function" not in codes
        assert r.sql  # a rendered Snowflake expression was produced
