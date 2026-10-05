"""Phase 6 staged enforcement gate (WB_TRANSFORM_ENFORCEMENT).

The gate lives in the shared result (transform_preflight), not per-router, so
every build/deploy path enforces identically. Default mode is 'warn' — nothing
blocks until an operator sets 'block' after a clean portfolio audit.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from workbench.backend import config, transform_preflight
from workbench.backend.dialect_sql import CompileResult, TransformDiagnostic


def _err_result():
    return CompileResult(
        sql=None, catalog_version="v1",
        errors=[TransformDiagnostic(
            "error", "unsupported_function", "AGE() is unsupported on databricks",
            function="AGE", product_col="employee_age", remediation="use date_difference",
        )],
        validated=True,
    )


def _clean_result():
    return CompileResult(sql="CREATE VIEW ...", catalog_version="v1", validated=True)


# ── mode resolution ───────────────────────────────────────────────────────────

def test_default_mode_is_warn(monkeypatch):
    monkeypatch.delenv("WB_TRANSFORM_ENFORCEMENT", raising=False)
    assert config.transform_enforcement() == "warn"
    assert transform_preflight.enforcement_mode() == "warn"
    assert transform_preflight.enforcement_enabled() is True


def test_off_mode_disables_gate(monkeypatch):
    monkeypatch.setenv("WB_TRANSFORM_ENFORCEMENT", "off")
    assert transform_preflight.enforcement_enabled() is False


def test_garbage_mode_falls_back_to_warn(monkeypatch):
    monkeypatch.setenv("WB_TRANSFORM_ENFORCEMENT", "banana")
    assert config.transform_enforcement() == "warn"


# ── is_blocked ────────────────────────────────────────────────────────────────

def test_is_blocked_only_block_mode_with_errors(monkeypatch):
    monkeypatch.setenv("WB_TRANSFORM_ENFORCEMENT", "block")
    assert transform_preflight.is_blocked(_err_result()) is True
    assert transform_preflight.is_blocked(_clean_result()) is False
    monkeypatch.setenv("WB_TRANSFORM_ENFORCEMENT", "warn")
    assert transform_preflight.is_blocked(_err_result()) is False
    monkeypatch.setenv("WB_TRANSFORM_ENFORCEMENT", "off")
    assert transform_preflight.is_blocked(_err_result()) is False


# ── raise_if_blocked ──────────────────────────────────────────────────────────

def test_block_mode_raises_422_with_remediation(monkeypatch):
    monkeypatch.setenv("WB_TRANSFORM_ENFORCEMENT", "block")
    with pytest.raises(HTTPException) as exc:
        transform_preflight.raise_if_blocked(_err_result(), context="deploy_virtual_view")
    assert exc.value.status_code == 422
    detail = exc.value.detail
    assert detail["error"] == "transform_capability_block"
    assert detail["context"] == "deploy_virtual_view"
    assert "employee_age" in detail["message"]
    # the full diagnostics ride along for the UI
    assert detail["errors"][0]["function"] == "AGE"
    assert detail["errors"][0]["remediation"] == "use date_difference"


def test_warn_mode_never_raises_even_with_errors(monkeypatch):
    monkeypatch.setenv("WB_TRANSFORM_ENFORCEMENT", "warn")
    transform_preflight.raise_if_blocked(_err_result(), context="deploy")  # no raise


def test_off_mode_never_raises(monkeypatch):
    monkeypatch.setenv("WB_TRANSFORM_ENFORCEMENT", "off")
    transform_preflight.raise_if_blocked(_err_result(), context="deploy")  # no raise


def test_block_mode_clean_result_does_not_raise(monkeypatch):
    monkeypatch.setenv("WB_TRANSFORM_ENFORCEMENT", "block")
    transform_preflight.raise_if_blocked(_clean_result(), context="deploy")  # no raise


def test_none_result_never_raises(monkeypatch):
    monkeypatch.setenv("WB_TRANSFORM_ENFORCEMENT", "block")
    transform_preflight.raise_if_blocked(None, context="deploy")  # no raise


# ── raise_if_summary_blocked ──────────────────────────────────────────────────

def test_summary_gate_blocks_on_error_diagnostics(monkeypatch):
    monkeypatch.setenv("WB_TRANSFORM_ENFORCEMENT", "block")
    summary = {"transform_diagnostics": {
        "errors": [{"severity": "error", "code": "unsupported_function",
                    "message": "AGE() unsupported", "function": "AGE",
                    "product_col": "age"}],
        "warnings": [], "used_capabilities": [], "validated": True, "catalog_version": "v1"}}
    with pytest.raises(HTTPException) as exc:
        transform_preflight.raise_if_summary_blocked(summary, context="materialize")
    assert exc.value.status_code == 422


def test_summary_gate_string_summary_is_noop(monkeypatch):
    monkeypatch.setenv("WB_TRANSFORM_ENFORCEMENT", "block")
    transform_preflight.raise_if_summary_blocked("No approved mappings", context="materialize")


def test_summary_gate_clean_is_noop(monkeypatch):
    monkeypatch.setenv("WB_TRANSFORM_ENFORCEMENT", "block")
    summary = {"transform_diagnostics": {"errors": [], "warnings": [],
               "used_capabilities": [], "validated": True, "catalog_version": "v1"}}
    transform_preflight.raise_if_summary_blocked(summary, context="materialize")
