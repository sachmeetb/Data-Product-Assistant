"""Structured run-result recorder for serving package runners.

STDLIB ONLY — this file is copied verbatim into every downloadable serving
package next to ``run.py`` and must run on a bare Python 3.8+ with no third-party
imports beyond what the package's ``requirements.txt`` installs for the DB driver.
It must NOT import anything from ``workbench.*``.

Every runner (``run_deploy.py`` / ``run_dbt.py`` / ``run_lakehouse.py``) uses a
``RunRecorder`` to accumulate steps + metrics and, on ``finish()``, write two
files into the package/working dir:

  * ``run_result.json`` — the machine-readable verdict Data Workbench parses
    (schema = the ``run_result.json`` v1 contract; see ``serving_runtime.RunResult``).
  * ``run.log`` — durable JSONL, one event per line, appended as the run proceeds.

The same files are what an engineer inspects after running the package by hand.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone


CONTRACT_VERSION = "1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunRecorder:
    """Accumulates a run's steps/metrics and writes the v1 result + JSONL log.

    Usage in a runner::

        rec = RunRecorder(operation="deploy_view", mode="apply",
                          target={"platform": "mysql", "schema": "public"})
        rec.log("connecting")
        rec.step("apply_ddl", "success", detail="2 views")
        rec.metric("view_count", 2)
        rec.finish("success")          # writes run_result.json + run.log

    On an exception, call ``rec.fail(error_class, message)`` (or
    ``finish("failed", error={...})``) so the verdict is still written.
    """

    def __init__(self, *, operation: str, mode: str,
                 target: dict | None = None,
                 result_path: str = "run_result.json",
                 log_path: str = "run.log",
                 echo: bool = True) -> None:
        self.operation = operation
        self.mode = mode
        self.target = target or {}
        self.result_path = result_path
        self.log_path = log_path
        self.echo = echo
        self.started_at = _now_iso()
        self._start_perf = datetime.now(timezone.utc)
        self.steps: list[dict] = []
        self.metrics: dict = {}
        # Truncate/replace any stale log from a prior run in the same dir.
        try:
            with open(self.log_path, "w", encoding="utf-8"):
                pass
        except OSError:
            pass

    # ── logging ──────────────────────────────────────────────────────────────
    def log(self, msg: str, level: str = "info", **extra) -> None:
        event = {"ts": _now_iso(), "level": level, "msg": msg}
        if extra:
            event.update(extra)
        line = json.dumps(event, default=str)
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        if self.echo:
            stream = sys.stderr if level in ("error", "warning") else sys.stdout
            print(f"[{level}] {msg}", file=stream, flush=True)

    def step(self, name: str, status: str, detail: str | None = None) -> None:
        self.steps.append({"name": name, "status": status, "detail": detail})
        self.log(f"step {name}: {status}" + (f" — {detail}" if detail else ""),
                 level="info" if status == "success" else "warning")

    def metric(self, key: str, value) -> None:
        self.metrics[key] = value

    # ── finalize ───────────────────────────────────────────────────────────────
    def _payload(self, status: str, error: dict | None) -> dict:
        finished = _now_iso()
        duration_ms = int(
            (datetime.now(timezone.utc) - self._start_perf).total_seconds() * 1000
        )
        return {
            "contract_version": CONTRACT_VERSION,
            "operation": self.operation,
            "mode": self.mode,
            "status": status,
            "started_at": self.started_at,
            "finished_at": finished,
            "duration_ms": duration_ms,
            "target": self.target,
            "metrics": self.metrics,
            "steps": self.steps,
            "error": error,
            "log_file": self.log_path,
        }

    def finish(self, status: str, error: dict | None = None) -> dict:
        payload = self._payload(status, error)
        with open(self.result_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        self.log(f"finished: {status}", level="info" if status == "success" else "error")
        return payload

    def fail(self, error_class: str, message: str) -> dict:
        return self.finish("failed", error={"class": error_class, "message": message})
