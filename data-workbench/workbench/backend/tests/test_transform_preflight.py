"""AST-level capability validation — the authority is the artifact, not transpile.

The load-bearing property proven here: ``sqlglot.transpile`` re-emits AGE(...)
for Databricks and SPLIT_PART(...) for BigQuery/MySQL UNCHANGED (accepting
syntax without proving support), yet ``compile_expression`` FLAGS them because
it walks the AST and checks every function against the capability artifact.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlglot

from workbench.backend.dialect_sql import (
    CompileResult,
    compile_expression,
    validate_expression,
)
from workbench.backend.transform_preflight import (
    compile_result_from_diagnostics,
    compile_result_from_summary,
)

_REPO = Path(__file__).resolve().parents[3]
_GEN = _REPO / "workbench-skills" / "skills" / "data-serving-virtual-view" / "scripts" / "generate_view_ddl.py"


def _load_gv():
    spec = importlib.util.spec_from_file_location("generate_view_ddl", _GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _codes(result: CompileResult) -> set[str]:
    return {e.code for e in result.errors}


# ── the core claim: transpile success != capability ──────────────────────────

def test_transpile_emits_age_unchanged_but_compiler_flags_it():
    # sqlglot happily transpiles AGE to Databricks unchanged...
    transpiled = sqlglot.transpile("SELECT AGE(dob, hire_date)", read="postgres", write="databricks")
    assert "AGE(" in transpiled[0].upper()
    # ...but the capability-aware compiler fails closed.
    r = compile_expression("AGE(dob, hire_date)", "databricks", read="postgres")
    assert not r.ok
    assert r.sql is None
    assert "unsupported_function" in _codes(r)
    age_err = next(e for e in r.errors if e.function == "AGE")
    assert age_err.remediation  # the corpus tells the author what to do instead


def test_transpile_emits_split_part_unchanged_but_compiler_flags_bq_and_mysql():
    for target in ("bigquery", "mysql"):
        transpiled = sqlglot.transpile("SELECT SPLIT_PART(x, '-', 1)", read="postgres", write=target)
        assert "SPLIT_PART(" in transpiled[0].upper(), target
        r = compile_expression("SPLIT_PART(x, '-', 1)", target, read="postgres")
        assert not r.ok, target
        assert "unsupported_function" in _codes(r), target


# ── AGE matrix ────────────────────────────────────────────────────────────────

def test_age_native_on_postgres():
    r = compile_expression("AGE(dob, hire_date)", "postgres", read="postgres")
    assert r.ok
    assert r.sql is not None
    assert any(c.startswith("AGE@postgres") for c in r.used_capabilities)


def test_age_unsupported_on_all_non_postgres():
    for target in ("databricks", "snowflake", "bigquery", "mysql"):
        r = compile_expression("AGE(dob, hire_date)", target, read="postgres")
        assert not r.ok, target
        assert "unsupported_function" in _codes(r), target


# ── SPLIT_PART supported where native ─────────────────────────────────────────

def test_split_part_ok_where_native():
    for target in ("postgres", "databricks", "snowflake"):
        r = compile_expression("SPLIT_PART(x, '-', 1)", target, read="postgres")
        assert r.ok, target
        assert r.sql is not None


# ── portable expressions pass everywhere ──────────────────────────────────────

def test_portable_expression_clean_on_every_platform():
    expr = "UPPER(TRIM(first_name)) || ' ' || TRIM(last_name)"
    for target in ("postgres", "databricks", "snowflake", "bigquery", "mysql"):
        r = compile_expression(expr, target, read="postgres")
        assert r.ok, (target, [e.message for e in r.errors])
        assert r.sql is not None


# ── fail-closed structural guards ─────────────────────────────────────────────

def test_unknown_function_fails_closed():
    r = compile_expression("FROBNICATE(x)", "databricks", read="postgres")
    assert not r.ok
    assert "unknown_function" in _codes(r)


def test_subquery_fails_closed_once():
    r = compile_expression("x IN (SELECT id FROM y)", "databricks", read="postgres")
    assert not r.ok
    subq = [e for e in r.errors if e.code == "subquery"]
    assert len(subq) == 1  # deduped, not one per AST node


def test_multi_statement_fails_closed():
    r = compile_expression("SELECT 1; SELECT 2", "databricks", read="postgres")
    assert not r.ok
    assert "multi_statement" in _codes(r)


def test_unknown_source_dialect_routes_to_review():
    r = compile_expression("AGE(a, b)", "databricks", read="teradata")
    assert not r.ok
    assert "unknown_source_dialect" in _codes(r)


def test_unknown_target_platform_fails_closed():
    diags, used = validate_expression("UPPER(x)", "oracle", read="postgres")
    assert any(d.code == "unknown_target_platform" for d in diags)
    assert used == []


# ── result shape ──────────────────────────────────────────────────────────────

def test_compile_result_sql_only_when_clean_and_carries_catalog_version():
    ok = compile_expression("UPPER(x)", "databricks", read="postgres")
    assert ok.ok and ok.sql is not None
    assert ok.catalog_version == "v1"
    bad = compile_expression("AGE(a, b)", "databricks", read="postgres")
    assert not bad.ok and bad.sql is None
    assert bad.catalog_version == "v1"
    # to_dict is JSON-surfaceable for the preflight endpoint / MCP tool.
    d = bad.to_dict()
    assert d["ok"] is False and d["sql"] is None and d["errors"]


def test_empty_expression_is_ok():
    r = compile_expression("", "databricks", read="postgres")
    assert r.ok
    assert r.used_capabilities == []


# ── Phase 3: generate_view_ddl diagnostics accumulation (no Neo4j) ────────────

def _fake_product_cols():
    return {
        "employee_age": [{"transform_kind": "arithmetic",
                          "transform_expression": "AGE(t1.dob, t1.hire_date)",
                          "expression_dialect": "postgres",
                          "mapping_uri": "mapping:x:age"}],
        "full_name": [{"transform_kind": "concat",
                       "transform_expression": "UPPER(t1.first) || ' ' || t1.last",
                       "expression_dialect": "postgres",
                       "mapping_uri": "mapping:x:name"}],
        "emp_id": [{"transform_kind": "direct", "transform_expression": "",
                    "mapping_uri": "mapping:x:id"}],
        "ssn_hash": [{"transform_kind": "hash", "transform_expression": "",
                      "mapping_uri": "mapping:x:ssn"}],
    }


def test_gv_diagnostics_flag_age_on_databricks_skip_on_ansi():
    gv = _load_gv()
    # databricks: AGE flagged, structured kinds recorded, catalog stamped.
    d = gv._empty_transform_diagnostics()
    gv._accumulate_transform_diagnostics(d, _fake_product_cols(), gv.get_dialect("databricks"))
    assert d["validated"] is True
    assert d["catalog_version"] == "v1"
    codes = [(e["code"], e.get("product_col"), e.get("function")) for e in d["errors"]]
    assert ("unsupported_function", "employee_age", "AGE") in codes
    assert "kind:hash@databricks" in d["used_capabilities"]

    # postgres: AGE is native → no errors.
    dpg = gv._empty_transform_diagnostics()
    gv._accumulate_transform_diagnostics(dpg, _fake_product_cols(), gv.get_dialect("postgres"))
    assert dpg["errors"] == []

    # ansi (mysql native-view fallback / non-served): validation SKIPPED, not "clean".
    dan = gv._empty_transform_diagnostics()
    gv._accumulate_transform_diagnostics(dan, _fake_product_cols(), gv.get_dialect("ansi"))
    assert dan["validated"] is False
    assert dan["errors"] == []


def test_gv_aggregate_transform_diagnostics_handles_string_summaries():
    gv = _load_gv()
    per_view = [
        {"transform_diagnostics": {"errors": [{"code": "unsupported_function"}],
                                   "warnings": [], "used_capabilities": ["UPPER@databricks:native"],
                                   "validated": True, "catalog_version": "v1"}},
        "No approved mappings",  # a dataset that yielded an error string, not a dict
    ]
    agg = gv._aggregate_transform_diagnostics(per_view)
    assert len(agg["errors"]) == 1
    assert agg["validated"] is True
    assert agg["catalog_version"] == "v1"


# ── Phase 3: summary → CompileResult lift ─────────────────────────────────────

def test_compile_result_from_summary_withholds_sql_on_error():
    summary = {"transform_diagnostics": {
        "errors": [{"severity": "error", "code": "unsupported_function",
                    "message": "AGE() unsupported", "function": "AGE",
                    "product_col": "employee_age", "mapping_uri": "mapping:x:age",
                    "remediation": "decompose"}],
        "warnings": [], "used_capabilities": ["UPPER@databricks:native"],
        "validated": True, "catalog_version": "v1"}}
    r = compile_result_from_summary(summary, sql="CREATE VIEW ...")
    assert not r.ok
    assert r.sql is None
    assert r.errors[0].function == "AGE"
    assert r.errors[0].product_col == "employee_age"
    assert r.errors[0].remediation == "decompose"


def test_compile_result_from_summary_attaches_sql_when_clean():
    summary = {"transform_diagnostics": {"errors": [], "warnings": [],
               "used_capabilities": [], "validated": True, "catalog_version": "v1"}}
    r = compile_result_from_summary(summary, sql="CREATE VIEW ok ...")
    assert r.ok
    assert r.sql == "CREATE VIEW ok ..."


def test_compile_result_from_string_summary_is_empty_and_nonblocking():
    r = compile_result_from_summary("No approved mappings", sql=None)
    assert r.ok
    assert r.errors == []


def test_compile_result_from_diagnostics_none_is_ok():
    r = compile_result_from_diagnostics(None, sql="X")
    assert r.ok
    assert r.sql == "X"


# ── Phase 5: MCP tool glue (no Neo4j) ─────────────────────────────────────────

def test_mcp_get_transform_preflight_unknown_project_returns_error():
    from workbench.backend.mcp_server import get_transform_preflight
    out = get_transform_preflight("nonexistent-project-code")
    assert isinstance(out, dict)
    assert "error" in out


def test_rest_transform_preflight_unknown_project_raises_404():
    import pytest as _pytest
    from fastapi import HTTPException
    from sqlmodel import Session
    from workbench.backend.database import engine
    from workbench.backend.routers.serving import transform_preflight_check
    with Session(engine) as session:
        with _pytest.raises(HTTPException) as exc:
            transform_preflight_check(999_999, session)
        assert exc.value.status_code == 404
