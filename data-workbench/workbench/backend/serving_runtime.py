"""Backend side of the serving-package execution unit.

The three serving modes (virtual view / dbt / lakehouse) are each executed by
running a **packaged runner** (``run.py`` inside the assembled package dir) as a
subprocess. That same package is what an engineer downloads and runs. Data
Workbench never deploys/serves through a *separate* internal code path — it kicks
off the package's own runner and reads the structured ``run_result.json`` the
runner writes. This module owns:

* ``RunResult`` — the Pydantic model that parses/validates ``run_result.json``
  (the v1 contract emitted by ``serving_runners/_wb_runresult.RunRecorder``).
* ``execute_package_runner`` — the subprocess invoker (capture, timeout, parse,
  surface the ``run.log`` tail on failure).
* ``build_runner_env`` — maps a resolved connection_ref into the ``WB_*`` env
  vars the runner reads. Secrets travel in the subprocess env only, never to disk.

The runner scripts themselves live in ``serving_runners/`` (stdlib + driver, no
``workbench.*`` imports) and are copied verbatim into each package by
``serving_package.assemble_*``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


SERVING_RUNNERS_DIR = Path(__file__).resolve().parent / "serving_runners"
# The shared recorder copied alongside every runner.
RUNRESULT_HELPER = SERVING_RUNNERS_DIR / "_wb_runresult.py"

DEFAULT_RUNNER_TIMEOUT = 1800  # matches the dbt full-build ceiling


class RunStep(BaseModel):
    name: str
    status: str
    detail: Optional[str] = None


class RunError(BaseModel):
    cls: str = Field(default="", alias="class")
    message: str = ""

    model_config = {"populate_by_name": True}


class RunResult(BaseModel):
    """Parsed ``run_result.json`` (v1). Mirrors ``_wb_runresult.RunRecorder``."""
    contract_version: str = "1"
    operation: str = ""
    mode: str = ""
    status: str = "failed"
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    target: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    steps: list[RunStep] = Field(default_factory=list)
    error: Optional[RunError] = None
    log_file: str = "run.log"

    # Non-contract diagnostics filled by the invoker (not written by the runner).
    stdout_tail: str = ""
    stderr_tail: str = ""
    log_tail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "success"


def _tail(text: str, n: int = 4000) -> str:
    if not text:
        return ""
    return text if len(text) <= n else text[-n:]


def execute_package_runner(
    package_dir: str | Path,
    args: list[str],
    *,
    env: Optional[dict[str, str]] = None,
    timeout: int = DEFAULT_RUNNER_TIMEOUT,
    runner_name: str = "run.py",
) -> RunResult:
    """Run ``<package_dir>/<runner_name> <args>`` as a subprocess and return the
    parsed ``run_result.json``.

    The runner is invoked with ``cwd=package_dir`` so it writes ``run_result.json``
    + ``run.log`` there. ``env`` (the ``WB_*`` credentials from ``build_runner_env``)
    is merged over ``os.environ``. On timeout, a non-zero exit with no result file,
    or a parse failure, a synthesized ``status='failed'`` ``RunResult`` is returned
    (never raises) with the captured stdout/stderr/log tails attached.
    """
    pkg = Path(package_dir)
    runner = pkg / runner_name
    result_path = pkg / "run_result.json"
    log_path = pkg / "run.log"
    # Remove any stale verdict so we never read a previous run's result.
    try:
        result_path.unlink()
    except FileNotFoundError:
        pass

    if not runner.exists():
        return RunResult(status="failed",
                         error=RunError(**{"class": "runner_missing",
                                           "message": f"runner not found: {runner}"}))

    full_env = {**os.environ, **(env or {})}
    proc_out, proc_err, timed_out = "", "", False
    try:
        proc = subprocess.run(
            [sys.executable, str(runner), *args],
            cwd=str(pkg), env=full_env,
            capture_output=True, text=True, timeout=timeout,
        )
        proc_out, proc_err = proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as e:
        timed_out = True
        proc_out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        proc_err = (e.stderr or "") if isinstance(e.stderr, str) else ""

    log_tail = ""
    if log_path.exists():
        try:
            log_tail = _tail(log_path.read_text(encoding="utf-8"))
        except OSError:
            pass

    if timed_out:
        return RunResult(status="failed",
                         error=RunError(**{"class": "timeout",
                                           "message": f"runner exceeded {timeout}s"}),
                         stdout_tail=_tail(proc_out), stderr_tail=_tail(proc_err),
                         log_tail=log_tail)

    if not result_path.exists():
        return RunResult(status="failed",
                         error=RunError(**{"class": "no_result",
                                           "message": "runner produced no run_result.json"}),
                         stdout_tail=_tail(proc_out), stderr_tail=_tail(proc_err),
                         log_tail=log_tail)

    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
        result = RunResult.model_validate(data)
    except Exception as e:  # malformed JSON / schema mismatch → fail cleanly
        return RunResult(status="failed",
                         error=RunError(**{"class": "bad_result",
                                           "message": f"unparseable run_result.json: {e}"}),
                         stdout_tail=_tail(proc_out), stderr_tail=_tail(proc_err),
                         log_tail=log_tail)

    result.stdout_tail = _tail(proc_out)
    result.stderr_tail = _tail(proc_err)
    result.log_tail = log_tail
    return result


def build_runner_env(prefix: str, platform: str, connection_ref: dict | None) -> dict[str, str]:
    """Map a resolved ``connection_ref`` into ``WB_*`` env vars for a runner.

    ``prefix`` is the var namespace: ``"WB_TARGET"`` (view deploy target),
    ``"WB_DBT"`` (dbt), ``"WB_SOURCE"`` (lakehouse export source). A Postgres
    connection maps to ``<prefix>_DSN`` (derived transiently from the structured
    ref); every other platform maps to ``<prefix>_{HOST,PORT,USER,PASSWORD,
    DBNAME,...}``. The ephemeral ``resolved_password`` never touches disk — it
    rides the subprocess env only. Extra platform config (Snowflake account/
    warehouse, Databricks http_path/token) is passed through under the same prefix.
    """
    cref = connection_ref or {}
    env: dict[str, str] = {f"{prefix}_PLATFORM": (platform or "postgres")}

    if (platform or "postgres").lower() in ("postgres", "postgresql"):
        # Derive the Postgres DSN from the STRUCTURED ref only now, at the moment
        # the runner needs one — the single connection contract, no stored shape.
        from .routers.connections import build_connection_string
        dsn = build_connection_string("postgres", cref)
        if dsn:
            env[f"{prefix}_DSN"] = dsn
            return env

    def put(key: str, val: Any) -> None:
        if val is not None and val != "":
            env[f"{prefix}_{key}"] = str(val)

    put("HOST", cref.get("host"))
    put("PORT", cref.get("port"))
    put("USER", cref.get("username"))
    put("PASSWORD", cref.get("resolved_password"))
    put("DBNAME", cref.get("database"))
    put("SCHEMA", cref.get("default_schema"))
    extra = cref.get("extra_config") or {}
    if isinstance(extra, dict):
        for k in ("account", "warehouse", "role", "http_path", "catalog", "token"):
            put(k.upper(), extra.get(k))
    return env
