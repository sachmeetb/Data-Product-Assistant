#!/usr/bin/env python3
"""dbt build runner — the executable entry point of a dbt serving package.

Data Workbench runs this (via ``serving_runtime.execute_package_runner``) to
build the product's models; an engineer runs the identical scaffolded project
with their own ``.env`` (``python bootstrap.py`` sets up the venv then calls this).

Runs ``dbt build`` in the project dir (this file sits at the dbt project root),
parses dbt's native ``target/run_results.json``, and normalizes it into
``run_result.json`` (+ ``run.log``). Credentials come from ``WB_DBT_*`` env vars
(profiles.yml reads them via ``env_var()``); nothing is written to disk.

STDLIB ONLY (dbt itself is a separate executable invoked as a subprocess).

Usage:
  python run.py [--mode sample|full] [--target preview|prod] [--sample-limit N]
                [--dbt-bin dbt] [--timeout 1800]
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from _wb_runresult import RunRecorder


HERE = Path(__file__).resolve().parent


def _parse_run_results() -> list[dict]:
    rr = HERE / "target" / "run_results.json"
    if not rr.exists():
        return []
    try:
        data = json.loads(rr.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for r in data.get("results", []):
        uid = r.get("unique_id", "")
        out.append({
            "model": uid.split(".")[-1] if uid else uid,
            "status": r.get("status"),
            "execution_time": r.get("execution_time"),
            "message": r.get("message"),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the product's dbt models.")
    ap.add_argument("--mode", choices=["sample", "full"], default="full")
    ap.add_argument("--target", default=None, help="dbt target (default: preview for sample, prod for full).")
    ap.add_argument("--sample-limit", type=int, default=100)
    ap.add_argument("--dbt-bin", default="dbt", help="Path to the dbt executable.")
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()

    target = args.target or ("preview" if args.mode == "sample" else "prod")
    rec = RunRecorder(operation="dbt_build", mode=args.mode,
                      target={"dbt_target": target})

    cmd = [args.dbt_bin, "build", "--project-dir", str(HERE),
           "--profiles-dir", str(HERE), "--target", target]
    if args.mode == "sample":
        cmd += ["--vars", json.dumps({"sample_limit": int(args.sample_limit)})]

    rec.log(f"running: {' '.join(cmd)}")
    timed_out = False
    proc = None
    try:
        proc = subprocess.run(cmd, cwd=str(HERE), capture_output=True, text=True,
                              timeout=args.timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    except FileNotFoundError:
        rec.fail("dbt_not_found",
                 f"dbt executable not found: {args.dbt_bin}. "
                 "Run `python bootstrap.py` to create a venv with dbt installed.")
        return 1

    run_results = _parse_run_results()
    rec.metric("run_results", run_results)
    for r in run_results:
        rec.step(f"model:{r['model']}", "success" if r.get("status") in ("success", "pass") else "failed",
                 detail=r.get("message"))

    if timed_out:
        rec.fail("timeout", f"dbt build exceeded {args.timeout}s")
        return 1
    if proc is not None and proc.returncode == 0:
        rec.metric("model_count", len(run_results))
        rec.finish("success")
        return 0

    tail = ((proc.stdout or "") + (proc.stderr or ""))[-1500:] if proc else ""
    rec.fail("dbt_build_failed", tail or "dbt build failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
