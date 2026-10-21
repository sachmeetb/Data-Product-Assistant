"""Phase 4 portfolio audit — pure aggregation + markdown render (no Neo4j)."""
from __future__ import annotations

from workbench.backend.dialect_sql import CompileResult, TransformDiagnostic
from workbench.backend.transform_audit import build_audit_report, render_markdown


def _err(function, product_col):
    return TransformDiagnostic(
        "error", "unsupported_function",
        f"Function {function}() is unsupported", function=function,
        product_col=product_col, mapping_uri=f"mapping:{product_col}",
        remediation="decompose to a date_difference op",
    )


def _result(errors=(), catalog="v1"):
    errs = list(errors)
    return CompileResult(
        sql=None if errs else "CREATE VIEW ...",
        catalog_version=catalog,
        errors=errs,
    )


def _entries():
    return [
        # product A: clean on postgres, AGE error on databricks
        ("proj1", "dprod:A", "postgres", _result()),
        ("proj1", "dprod:A", "databricks", _result([_err("AGE", "employee_age")])),
        # product B: clean everywhere
        ("proj2", "dprod:B", "postgres", _result()),
        ("proj2", "dprod:B", "databricks", _result()),
        # product C: two errors on bigquery
        ("proj3", "dprod:C", "bigquery",
         _result([_err("SPLIT_PART", "team"), _err("AGE", "age")])),
    ]


def test_build_audit_report_counts_and_flags():
    report = build_audit_report(_entries())
    s = report["summary"]
    assert s["products"] == 3
    assert s["products_with_errors"] == 2  # A and C
    assert s["total_errors"] == 3
    assert report["clean"] is False
    assert report["catalog_version"] == "v1"
    # per-platform rollup
    assert s["by_platform"]["databricks"]["with_errors"] == 1
    assert s["by_platform"]["bigquery"]["errors"] == 2
    assert s["by_platform"]["postgres"]["errors"] == 0


def test_products_sorted_by_error_count_desc():
    report = build_audit_report(_entries())
    # C has 2 errors, A has 1, B has 0 → C, A, B
    uris = [p["product_uri"] for p in report["products"]]
    assert uris == ["dprod:C", "dprod:A", "dprod:B"]


def test_clean_portfolio_flag():
    entries = [
        ("proj1", "dprod:A", "postgres", _result()),
        ("proj1", "dprod:A", "databricks", _result()),
    ]
    report = build_audit_report(entries)
    assert report["clean"] is True
    assert report["summary"]["total_errors"] == 0
    md = render_markdown(report)
    assert "No failing products" in md


def test_render_markdown_lists_failing_mapping_with_remediation():
    report = build_audit_report(_entries())
    md = render_markdown(report)
    assert "Transform portability — portfolio audit" in md
    assert "dprod:C" in md
    assert "SPLIT_PART" in md
    assert "decompose to a date_difference op" in md
    # the clean/dirty verdict line
    assert "remediate before enforcing" in md
