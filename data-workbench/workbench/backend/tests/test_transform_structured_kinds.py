"""Phase 7 structured-kind compilers — dialect-portable emission.

Asserts each structured kind (cast/substring/split/concat + the neutral
date_difference op) emits per-platform-correct SQL that RE-PARSES under the
target dialect (golden strings alone can't prove validity), and that the
hardcoded Postgres-isms in mask/hash are gone.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import sqlglot

_REPO = Path(__file__).resolve().parents[3]
_GEN = _REPO / "workbench-skills" / "skills" / "data-serving-virtual-view" / "scripts" / "generate_view_ddl.py"

_SQLGLOT = {"postgres": "postgres", "databricks": "databricks", "snowflake": "snowflake",
            "bigquery": "bigquery", "mysql": "mysql"}
SERVED = list(_SQLGLOT)


def _load_gv():
    spec = importlib.util.spec_from_file_location("generate_view_ddl", _GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gv = _load_gv()
ALIAS = {"hr.emp": "t1"}


def _mapping(kind, params, col="c"):
    return {"transform_kind": kind, "source_schema": "hr", "source_table": "emp",
            "source_col": col, "transform_params_json": json.dumps(params), "mapping_uri": "m:x"}


def _reparses(sql, platform):
    try:
        sqlglot.parse_one(f"SELECT {sql} FROM t", read=_SQLGLOT[platform])
        return True
    except Exception as e:  # noqa: BLE001
        return f"PARSE-FAIL: {e}"


# ── date_difference ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("platform", SERVED)
@pytest.mark.parametrize("semantics", ["completed_units", "boundary_count"])
def test_date_difference_renders_and_reparses(platform, semantics):
    m = _mapping("date_difference", {"unit": "year", "semantics": semantics}, col="dob")
    d = gv.get_dialect(platform)
    sql = gv._compile_structured_kind("date_difference", m, [m], ALIAS, d)
    assert sql
    assert _reparses(sql, platform) is True


def test_date_difference_semantics_differ_across_platforms():
    m = _mapping("date_difference", {"semantics": "completed_units"}, col="dob")
    pg = gv._compile_structured_kind("date_difference", m, [m], ALIAS, gv.get_dialect("postgres"))
    my = gv._compile_structured_kind("date_difference", m, [m], ALIAS, gv.get_dialect("mysql"))
    dbx = gv._compile_structured_kind("date_difference", m, [m], ALIAS, gv.get_dialect("databricks"))
    assert "AGE(" in pg
    assert "TIMESTAMPDIFF(YEAR" in my
    assert "MONTHS_BETWEEN" in dbx
    # boundary_count is genuinely different from completed_units (the discriminator works)
    mb = _mapping("date_difference", {"semantics": "boundary_count"}, col="dob")
    pg_b = gv._compile_structured_kind("date_difference", mb, [mb], ALIAS, gv.get_dialect("postgres"))
    assert pg_b != pg


def test_symbolic_interval_fails_closed_on_non_postgres():
    for platform in ("databricks", "snowflake", "bigquery", "mysql"):
        with pytest.raises(gv.ViewGenerationError):
            gv.get_dialect(platform).date_difference("t1.dob", "CURRENT_DATE",
                                                      semantics="symbolic_interval")
    # Postgres CAN express it.
    assert "AGE(" in gv.get_dialect("postgres").date_difference(
        "t1.dob", "CURRENT_DATE", semantics="symbolic_interval")


# ── concat: the MySQL `||`-as-OR bug is fixed ─────────────────────────────────

def test_concat_uses_concat_function_where_pipe_is_wrong():
    first = _mapping("concat", {"separator": " "}, col="first")
    last = _mapping("concat", {}, col="last")
    rows = [first, last]
    # MySQL / BigQuery: must be CONCAT(...), NOT `a || ' ' || b` (|| is OR / invalid).
    for platform in ("mysql", "bigquery", "databricks"):
        sql = gv._compile_structured_kind("concat", rows[0], rows, ALIAS, gv.get_dialect(platform))
        assert sql.startswith("CONCAT(")
        assert "||" not in sql
        assert _reparses(sql, platform) is True
    # Postgres keeps the idiomatic ||.
    pg = gv._compile_structured_kind("concat", rows[0], rows, ALIAS, gv.get_dialect("postgres"))
    assert "||" in pg


# ── concat / date_difference: parts order follows transform_inputs, not row order ──
# Regression for the "Rodriguez Noah" bug — a full_name mapping whose authored
# transformInputs correctly said [first_name, last_name] emitted last-first because
# the emitter iterated the (unordered) Neo4j rows directly instead of the input URIs.

def _rows_out_of_order(kind, cols, params=None):
    """Build mappings_for_pc whose row order is the REVERSE of the authored
    transformInputs order — the shape the Neo4j query can legitimately return."""
    params = params or {}
    ms = [_mapping(kind, params, col=c) for c in cols]
    inputs_json = json.dumps([gv._source_input_uri(m) for m in ms])
    for m in ms:
        m["transform_inputs_json"] = inputs_json
    return list(reversed(ms))  # rows reversed vs. authored input order


def test_ordered_mappings_by_inputs_reorders_to_uri_order():
    rows = _rows_out_of_order("concat", ["first", "last"], {"separator": " "})
    ordered = gv._ordered_mappings_by_inputs(rows)
    assert [m["source_col"] for m in ordered] == ["first", "last"]


def test_ordered_mappings_by_inputs_stable_without_inputs_json():
    first = _mapping("concat", {"separator": " "}, col="first")
    last = _mapping("concat", {}, col="last")
    rows = [last, first]  # no transform_inputs_json → order preserved verbatim
    assert gv._ordered_mappings_by_inputs(rows) is rows


def test_concat_parts_follow_transform_inputs_order_not_row_order():
    rows = _rows_out_of_order("concat", ["first", "last"], {"separator": " "})
    pg = gv._compile_structured_kind("concat", rows[0], rows, ALIAS, gv.get_dialect("postgres"))
    # first name precedes last name despite the reversed row order
    assert pg == "t1.first || ' ' || t1.last"


def test_date_difference_respects_transform_inputs_order():
    params = {"unit": "year", "semantics": "completed_units"}
    rows = _rows_out_of_order("date_difference", ["hire_date", "term_date"], params)
    d = gv.get_dialect("postgres")
    # A canonically-ordered reference [start, end]:
    start, end = rows[1], rows[0]
    ref = gv._compile_structured_kind("date_difference", start, [start, end], ALIAS, d)
    # The reversed rows must reorder via transform_inputs to produce the same SQL.
    fixed = gv._compile_structured_kind("date_difference", rows[0], rows, ALIAS, d)
    assert fixed == ref
    assert "t1.hire_date" in fixed and "t1.term_date" in fixed


# ── split: native vs emulated ─────────────────────────────────────────────────

@pytest.mark.parametrize("platform", SERVED)
def test_split_renders_and_reparses(platform):
    m = _mapping("split", {"delimiter": "-", "index": 2}, col="code")
    sql = gv._compile_structured_kind("split", m, [m], ALIAS, gv.get_dialect(platform))
    assert _reparses(sql, platform) is True
    if platform in ("postgres", "databricks", "snowflake"):
        assert "SPLIT_PART(" in sql
    if platform == "bigquery":
        assert "SPLIT(" in sql and "SAFE_OFFSET" in sql
    if platform == "mysql":
        assert "SUBSTRING_INDEX(" in sql


# ── substring: portable SUBSTR, no ANSI FROM/FOR ──────────────────────────────

@pytest.mark.parametrize("platform", SERVED)
def test_substring_portable_and_reparses(platform):
    m = _mapping("substring", {"start": 2, "length": 4}, col="s")
    sql = gv._compile_structured_kind("substring", m, [m], ALIAS, gv.get_dialect(platform))
    assert "SUBSTR(" in sql and "FROM" not in sql.upper()
    assert _reparses(sql, platform) is True


# ── cast ──────────────────────────────────────────────────────────────────────

def test_cast_uses_pg_shorthand_vs_standard():
    m = _mapping("cast", {"target_type": "text"}, col="x")
    pg = gv._compile_structured_kind("cast", m, [m], ALIAS, gv.get_dialect("postgres"))
    dbx = gv._compile_structured_kind("cast", m, [m], ALIAS, gv.get_dialect("databricks"))
    assert pg == "t1.x::text"
    assert dbx == "CAST(t1.x AS text)"


def test_cast_without_target_falls_back_to_expression():
    m = _mapping("cast", {}, col="x")  # no target_type
    assert gv._compile_structured_kind("cast", m, [m], ALIAS, gv.get_dialect("postgres")) is None


# ── mask: hardcoded Postgres-isms are gone ────────────────────────────────────

@pytest.mark.parametrize("platform", SERVED)
def test_mask_emits_portable_substr_and_concat(platform):
    m = _mapping("mask", {"algorithm": "keep_last", "keep_n": 4}, col="ssn")
    sql = gv._compile_mask(m, ALIAS, dialect=gv.get_dialect(platform))
    # No ANSI SUBSTRING(... FROM ... FOR ...) — that breaks BigQuery.
    assert "FROM" not in sql.upper()
    assert "SUBSTR(" in sql
    if platform in ("mysql", "bigquery", "databricks"):
        assert "||" not in sql  # `||` is not portable string concat
    assert _reparses(sql, platform) is True


def test_hash_salt_concat_is_portable_on_mysql():
    m = _mapping("hash", {"algorithm": "sha256", "salt": "ns"}, col="email")
    sql = gv._compile_hash(m, ALIAS, dialect=gv.get_dialect("mysql"))
    assert "||" not in sql  # salted concat must not use `||` on MySQL
    assert "CONCAT(" in sql


# ── composable bucket: band over a nested neutral value-op (tenure_band) ──────

_BUCKET_DATEDIFF = {
    "value": {"op": "date_difference", "unit": "year", "semantics": "completed_units"},
    "boundaries": [1, 3, 7],
    "labels": ["under_1y", "1_to_3y", "3_to_7y", "7y_plus"],
}

_DATEDIFF_IDIOM = {
    "postgres": "AGE(", "mysql": "TIMESTAMPDIFF(", "databricks": "MONTHS_BETWEEN(",
    "snowflake": "MONTHS_BETWEEN(", "bigquery": "DATE_DIFF(",
}


@pytest.mark.parametrize("platform", SERVED)
def test_bucket_over_date_difference_renders_and_reparses(platform):
    m = _mapping("bucket", _BUCKET_DATEDIFF, col="hr_hire_date")
    sql = gv._compile_bucket(m, [m], ALIAS, gv.get_dialect(platform))
    assert sql.startswith("CASE")
    assert _DATEDIFF_IDIOM[platform] in sql           # per-platform date-diff idiom
    for lab in ("under_1y", "1_to_3y", "3_to_7y", "7y_plus"):
        assert f"'{lab}'" in sql                       # all four band labels
    assert _reparses(sql, platform), f"{platform}: {sql}"


def test_bucket_band_thresholds_in_order():
    m = _mapping("bucket", _BUCKET_DATEDIFF, col="hr_hire_date")
    sql = gv._compile_bucket(m, [m], ALIAS, gv.get_dialect("postgres"))
    # thresholds appear in ascending order and map to the right labels
    assert sql.index("< 1 THEN 'under_1y'") < sql.index("< 3 THEN '1_to_3y'") < sql.index("< 7 THEN '3_to_7y'")
    assert sql.rstrip().endswith("ELSE '7y_plus' END")


def test_bucket_symbolic_interval_fails_closed_off_postgres():
    params = {**_BUCKET_DATEDIFF, "value": {"op": "date_difference", "semantics": "symbolic_interval"}}
    for platform in ("databricks", "snowflake", "bigquery", "mysql"):
        m = _mapping("bucket", params, col="hr_hire_date")
        with pytest.raises(gv.ViewGenerationError):
            gv._compile_bucket(m, [m], ALIAS, gv.get_dialect(platform))
    # Postgres CAN express symbolic_interval as the banded value.
    m = _mapping("bucket", params, col="hr_hire_date")
    assert "AGE(" in gv._compile_bucket(m, [m], ALIAS, gv.get_dialect("postgres"))


def test_bucket_unknown_value_op_fails_closed():
    params = {**_BUCKET_DATEDIFF, "value": {"op": "frobnicate"}}
    m = _mapping("bucket", params, col="hr_hire_date")
    with pytest.raises(gv.ViewGenerationError):
        gv._compile_bucket(m, [m], ALIAS, gv.get_dialect("postgres"))


def test_plain_bucket_without_value_unchanged():
    m = _mapping("bucket", {"boundaries": [60000, 100000], "labels": ["A", "B", "C"]}, col="salary")
    sql = gv._compile_bucket(m, [m], ALIAS, gv.get_dialect("databricks"))
    assert sql == "CASE WHEN t1.salary < 60000 THEN 'A' WHEN t1.salary < 100000 THEN 'B' ELSE 'C' END"


# ── format: case-fold now reads params.case (fixes email lowercasing) ─────────

def test_format_case_lower_emits_lower():
    m = _mapping("format", {"case": "lower"}, col="email")
    assert gv._compile_structured_kind("format", m, [m], ALIAS, gv.get_dialect("databricks")) == "LOWER(t1.email)"


def test_format_case_upper_emits_upper():
    m = _mapping("format", {"case": "upper"}, col="name")
    assert gv._compile_structured_kind("format", m, [m], ALIAS, gv.get_dialect("postgres")) == "UPPER(t1.name)"


def test_format_no_case_param_falls_back():
    # no case/format param → structured returns None (caller falls back / decorators apply)
    m = _mapping("format", {}, col="email")
    assert gv._compile_structured_kind("format", m, [m], ALIAS, gv.get_dialect("postgres")) is None
