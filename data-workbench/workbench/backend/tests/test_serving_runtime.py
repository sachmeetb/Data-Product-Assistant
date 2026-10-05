"""Tests for the serving-package execution foundation (serving_runtime.py).

Covers the RunResult parse, the RunRecorder → run_result.json contract, the
subprocess invoker (success / runner-failure / missing-result / timeout), and
the connection_ref → WB_* env mapping.
"""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from workbench.backend import serving_runtime as sr


# ── RunResult model ────────────────────────────────────────────────────────────


def test_runresult_parses_contract_with_error_alias():
    data = {
        "contract_version": "1", "operation": "deploy_view", "mode": "apply",
        "status": "failed", "duration_ms": 12, "target": {"platform": "mysql"},
        "metrics": {"view_count": 0},
        "steps": [{"name": "apply_ddl", "status": "failed", "detail": "boom"}],
        "error": {"class": "sql_error", "message": "bad ddl"},
        "log_file": "run.log",
    }
    r = sr.RunResult.model_validate(data)
    assert r.status == "failed"
    assert r.ok is False
    assert r.error.cls == "sql_error"          # "class" alias → cls
    assert r.steps[0].name == "apply_ddl"
    assert r.target["platform"] == "mysql"


def test_runresult_ok_property():
    r = sr.RunResult(status="success")
    assert r.ok is True


# ── RunRecorder → invoker roundtrip ─────────────────────────────────────────────


def _write_runner(dir_path: Path, body: str) -> None:
    """Drop the RunRecorder helper + a run.py that uses it into dir_path."""
    helper = sr.RUNRESULT_HELPER.read_text(encoding="utf-8")
    (dir_path / "_wb_runresult.py").write_text(helper, encoding="utf-8")
    (dir_path / "run.py").write_text(textwrap.dedent(body), encoding="utf-8")


def test_execute_runner_success(tmp_path):
    _write_runner(tmp_path, """
        from _wb_runresult import RunRecorder
        rec = RunRecorder(operation="deploy_view", mode="apply",
                          target={"platform": "postgres"})
        rec.step("apply_ddl", "success", detail="1 view")
        rec.metric("view_count", 1)
        rec.finish("success")
    """)
    res = sr.execute_package_runner(tmp_path, ["--apply"])
    assert res.ok
    assert res.operation == "deploy_view"
    assert res.metrics["view_count"] == 1
    assert res.steps[0].status == "success"
    # The runner wrote a real run.log.
    assert (tmp_path / "run.log").exists()


def test_execute_runner_records_failure(tmp_path):
    _write_runner(tmp_path, """
        from _wb_runresult import RunRecorder
        rec = RunRecorder(operation="deploy_view", mode="apply")
        rec.fail("sql_error", "syntax error near VIEW")
    """)
    res = sr.execute_package_runner(tmp_path, [])
    assert res.status == "failed"
    assert res.error.cls == "sql_error"
    assert "syntax error" in res.error.message


def test_execute_runner_missing_result(tmp_path):
    # Runner exits without writing run_result.json.
    (tmp_path / "run.py").write_text("print('did nothing')\n", encoding="utf-8")
    res = sr.execute_package_runner(tmp_path, [])
    assert res.status == "failed"
    assert res.error.cls == "no_result"
    assert "did nothing" in res.stdout_tail


def test_execute_runner_missing_runner(tmp_path):
    res = sr.execute_package_runner(tmp_path, [])
    assert res.status == "failed"
    assert res.error.cls == "runner_missing"


def test_execute_runner_timeout(tmp_path):
    (tmp_path / "run.py").write_text(
        "import time\ntime.sleep(5)\n", encoding="utf-8")
    res = sr.execute_package_runner(tmp_path, [], timeout=1)
    assert res.status == "failed"
    assert res.error.cls == "timeout"


def test_execute_runner_clears_stale_result(tmp_path):
    # A prior run's verdict must not leak when the new run fails to write one.
    (tmp_path / "run_result.json").write_text('{"status":"success"}', encoding="utf-8")
    (tmp_path / "run.py").write_text("print('no verdict')\n", encoding="utf-8")
    res = sr.execute_package_runner(tmp_path, [])
    assert res.status == "failed"
    assert res.error.cls == "no_result"


# ── build_runner_env ────────────────────────────────────────────────────────────


def test_build_env_postgres_dsn():
    # Postgres derives the DSN transiently from the STRUCTURED ref (no
    # pg_connection shape) — the single connection contract.
    env = sr.build_runner_env("WB_TARGET", "postgres", {
        "host": "h", "port": 5432, "username": "u",
        "resolved_password": "p", "database": "db",
    })
    assert env["WB_TARGET_PLATFORM"] == "postgres"
    assert env["WB_TARGET_DSN"] == "postgresql://u:p@h:5432/db"


def test_build_env_structured_mysql():
    env = sr.build_runner_env("WB_SOURCE", "mysql", {
        "host": "mysql-hr", "port": 3306, "username": "root",
        "resolved_password": "secret", "database": "hr_core",
    })
    assert env["WB_SOURCE_PLATFORM"] == "mysql"
    assert env["WB_SOURCE_HOST"] == "mysql-hr"
    assert env["WB_SOURCE_PORT"] == "3306"
    assert env["WB_SOURCE_USER"] == "root"
    assert env["WB_SOURCE_PASSWORD"] == "secret"
    assert env["WB_SOURCE_DBNAME"] == "hr_core"


def test_build_env_passes_extra_config():
    env = sr.build_runner_env("WB_TARGET", "snowflake", {
        "username": "u", "resolved_password": "p", "database": "d",
        "extra_config": {"account": "acct", "warehouse": "wh", "role": "r"},
    })
    assert env["WB_TARGET_ACCOUNT"] == "acct"
    assert env["WB_TARGET_WAREHOUSE"] == "wh"
    assert env["WB_TARGET_ROLE"] == "r"


def test_build_env_empty_ref():
    env = sr.build_runner_env("WB_TARGET", "postgres", None)
    assert env == {"WB_TARGET_PLATFORM": "postgres"}
