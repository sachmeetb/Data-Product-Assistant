#!/usr/bin/env python3
"""Virtual-view deploy runner — the executable entry point of a view package.

Data Workbench runs this (via ``serving_runtime.execute_package_runner``) to
deploy a product's views; an engineer runs the identical script from the
downloaded package with their own ``.env``. Reads:

  * ``package.json`` (next to this file) — ``{platform, view_schema, view_names[],
    ddl_file}`` written by ``serving_package.assemble_view_package``.
  * the DDL from ``ddl_file`` (default ``view.sql``).
  * connection from ``WB_TARGET_*`` env vars (see ``.env.example``):
      Postgres: ``WB_TARGET_DSN`` OR ``WB_TARGET_{HOST,PORT,USER,PASSWORD,DBNAME}``
      MySQL:    ``WB_TARGET_{HOST,PORT,USER,PASSWORD,DBNAME}``
      Snowflake/Databricks: ``+ WB_TARGET_{ACCOUNT,WAREHOUSE,ROLE,HTTP_PATH,TOKEN}``

Writes ``run_result.json`` + ``run.log`` (the feedback DWB parses).

Usage: ``python run.py [--apply] [--ddl-file view.sql]``
STDLIB + sqlparse + the target's DB driver only (see requirements.txt).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from _wb_runresult import RunRecorder
import _deploy_core


def _conn_params_from_env() -> tuple[str, dict]:
    """Read WB_TARGET_* → (platform, conn_params) for _deploy_core.apply_ddl."""
    g = os.environ.get
    platform = (g("WB_TARGET_PLATFORM") or "postgres").lower()
    params = {
        "dsn": g("WB_TARGET_DSN"),
        "host": g("WB_TARGET_HOST"),
        "port": g("WB_TARGET_PORT"),
        "user": g("WB_TARGET_USER"),
        "password": g("WB_TARGET_PASSWORD"),
        "dbname": g("WB_TARGET_DBNAME"),
        "schema": g("WB_TARGET_SCHEMA"),
        "warehouse": g("WB_TARGET_WAREHOUSE"),
        "role": g("WB_TARGET_ROLE"),
        "http_path": g("WB_TARGET_HTTP_PATH"),
        "token": g("WB_TARGET_TOKEN"),
    }
    return platform, {k: v for k, v in params.items() if v not in (None, "")}


def main() -> int:
    ap = argparse.ArgumentParser(description="Deploy the product's virtual views.")
    ap.add_argument("--apply", action="store_true",
                    help="Apply the DDL (default action; present for symmetry).")
    ap.add_argument("--ddl-file", default=None, help="Override the DDL file path.")
    args = ap.parse_args()

    here = Path(__file__).resolve().parent
    manifest = {}
    mf = here / "package.json"
    if mf.exists():
        manifest = json.loads(mf.read_text(encoding="utf-8"))

    ddl_file = args.ddl_file or manifest.get("ddl_file") or "view.sql"
    view_schema = manifest.get("view_schema") or "public"
    view_names = manifest.get("view_names") or []
    # Serialised platform NamespaceModel — drives quoting / session-setup /
    # create-namespace generically for 3-level platforms (absent → per-platform
    # defaults kick in inside _deploy_core, back-compat with older packages).
    namespace = manifest.get("namespace")
    env_platform, conn_params = _conn_params_from_env()
    platform = manifest.get("platform") or env_platform

    rec = RunRecorder(operation="deploy_view", mode="apply",
                      target={"platform": platform, "schema": view_schema,
                              "view_count": len(view_names)})

    ddl_path = here / ddl_file
    if not ddl_path.exists():
        rec.fail("ddl_missing", f"DDL file not found: {ddl_file}")
        return 1
    ddl = ddl_path.read_text(encoding="utf-8")

    if not conn_params.get("dsn") and not conn_params.get("host"):
        rec.fail("connection_error",
                 "No target connection configured. Set WB_TARGET_DSN or "
                 "WB_TARGET_HOST/PORT/USER/PASSWORD/DBNAME (see .env.example).")
        return 1

    rec.log(f"deploying {len(view_names)} view(s) to {platform}:{view_schema}")
    result = _deploy_core.apply_ddl(ddl, platform, conn_params, view_schema, view_names,
                                    namespace=namespace)

    if result["status"] == "deployed":
        rec.step("apply_ddl", "success",
                 detail=f"{result['statements_executed']} statement(s), "
                        f"{result['smoke_test_count']} smoke-test(s)"
                        + (" (view-recreate recovery used)" if result["recovery_used"] else ""))
        rec.metric("view_count", result["smoke_test_count"])
        rec.metric("statements_executed", result["statements_executed"])
        rec.metric("recovery_used", result["recovery_used"])
        rec.finish("success")
        return 0

    rec.step("apply_ddl", "failed", detail=result.get("error_message"))
    rec.fail(result.get("error_class") or "sql_error",
             result.get("error_message") or "deploy failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
