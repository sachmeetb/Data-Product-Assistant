"""Non-LLM executor for DQ test stages.

Runs the generated validation script (from a prior generation stage)
as a subprocess, streams stdout line-by-line so the UI can show progress,
then invokes the load_test_results_to_graph.py loader. Finally records a
DQTestRun row via the existing helper.

Auto-detects framework from the project directory: prefers dq_tests_gx/
if present, falls back to dq_tests_python/.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Optional

# Cap a single DQ execution run so a runaway subprocess can't spin the UI
# forever. Generous — most real runs finish in seconds-to-minutes.
_EXEC_TIMEOUT_SECONDS = 30 * 60

from sqlmodel import Session

from .config import BASE_PROJECT_DIR, SKILLS_DIR
from .database import engine
from .dq_test_runs import record_dq_test_run
from .models import Project


_FRAMEWORKS = [
    {
        "framework": "gx",
        "subdir": "dq_tests_gx",
        "script": "run_gx_validations.py",
        "results_subdir": "results",
    },
    {
        "framework": "pandera",
        "subdir": "dq_tests_python",
        "script": "run_all.py",
        "results_subdir": "results",
    },
]


def dq_subdir(base: str, source_mode: str) -> str:
    """On-disk subdir for a DQ suite of a given framework + mode.

    dprod suites get a ``_dprod`` suffix so a `dpe-sa` product can hold BOTH a
    catalog (source pre-check) and a dprod (deployed-product) suite without their
    Builds colliding on the same directory — and so the executor's framework
    auto-detect can tell the two suites apart. Catalog mode is byte-identical to
    the historic path (no suffix). Shared by ``dq_test_generator`` (write side),
    ``dq_package`` (download side), and ``stage_execution`` (failure-analysis)."""
    return f"{base}_dprod" if source_mode == "dprod" else base


@dataclass
class ExecutionContext:
    framework: str
    script_path: Path
    results_dir: Path
    project_dir: Path


def _detect_framework(project_dir: Path, source_mode: str = "catalog") -> Optional[ExecutionContext]:
    for fw in _FRAMEWORKS:
        subdir = dq_subdir(fw["subdir"], source_mode)
        script = project_dir / subdir / fw["script"]
        if script.exists():
            return ExecutionContext(
                framework=fw["framework"],
                script_path=script,
                results_dir=project_dir / subdir / fw["results_subdir"],
                project_dir=project_dir,
            )
    return None


def _loader_script_path(framework: str) -> Optional[Path]:
    skill = {
        "gx": "data-quality-testing-gx",
        "pandera": "data-quality-testing-python",
    }.get(framework)
    if not skill:
        return None
    p = SKILLS_DIR / skill / "scripts" / "load_test_results_to_graph.py"
    return p if p.exists() else None


async def _stream_subprocess(
    cmd: list[str],
    cwd: Path,
    label: str,
) -> AsyncGenerator[dict, None]:
    """Run a subprocess and yield text_delta events for each stdout line.

    Final yield is a dict with exit_code (no stage_complete — caller decides).
    """
    # Force unbuffered stdout on the child — otherwise Python block-buffers
    # ~8KB when stdout is a pipe and the UI stays blank for minutes.
    py_cmd = [cmd[0], "-u", *cmd[1:]]
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    yield {"type": "text_delta", "text": f"\n▸ {label}\n  $ {' '.join(py_cmd)}\n"}
    proc = await asyncio.create_subprocess_exec(
        *py_cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    assert proc.stdout is not None
    try:
        while True:
            line = await asyncio.wait_for(
                proc.stdout.readline(), timeout=_EXEC_TIMEOUT_SECONDS
            )
            if not line:
                break
            yield {"type": "text_delta", "text": line.decode(errors="replace")}
        exit_code = await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        yield {
            "type": "_exit",
            "exit_code": -1,
            "timeout": True,
        }
        return
    yield {"type": "_exit", "exit_code": exit_code}


# Served warehouse platforms the generated GX runner can execute against, mapped
# to the SQLAlchemy dialect entrypoint(s) the runner may load (via `<scheme>://`)
# and the pip package that provides it. Multiple dialect names cover version skew
# — databricks-sqlalchemy 1.x registered `databricks+connector`, 2.x registers
# plain `databricks`. Postgres/psycopg2 is always present in the backend env, so
# it's handled directly (no preflight) in _resolve_dprod_conn_string.
_DIALECT_PREFLIGHT = {
    "mysql": (("mysql.pymysql",), "pymysql"),
    "snowflake": (("snowflake",), "snowflake-sqlalchemy"),
    "databricks": (("databricks", "databricks.connector"), "databricks-sqlalchemy"),
}


def _preflight_sqlalchemy_dialect(platform: str) -> Optional[str]:
    """Return an actionable error if the SQLAlchemy dialect the generated runner
    needs for ``platform`` can't be loaded, else ``None``. Fail-closed on unknown
    platforms so we never hand a DSN to a runner that can't speak it."""
    entry = _DIALECT_PREFLIGHT.get(platform)
    if entry is None:
        return (f"Product DQ execution doesn't support the served platform "
                f"'{platform}' yet (supported: postgres, "
                f"{', '.join(sorted(_DIALECT_PREFLIGHT))}).")
    dialect_names, pip_pkg = entry
    from sqlalchemy.dialects import registry
    for name in dialect_names:
        try:
            registry.load(name)
            return None
        except Exception:  # noqa: BLE001 — NoSuchModuleError / import-time failure
            continue
    return (f"The SQLAlchemy driver for '{platform}' isn't available on the "
            f"Workbench backend — install it with `pip install {pip_pkg}` "
            f"(or download the DQ package and run it against the deployed views "
            f"yourself).")


# Guard: the deployed virtual-view serving definition must be live before a dprod
# DQ suite can run — the tests reference the vw_<name> views the deploy created.
_DEPLOY_STATUS_QUERY = """
MATCH (dc:DataContract {id: $contract_id})-[:MATERIALISES_AS]->(dp:DProdDataProduct)
MATCH (dp)-[:SERVED_BY]->(sd:ServingDefinition {servingMode: 'virtual_view'})
RETURN coalesce(sd.deploymentStatus, 'pending') AS deployment_status
"""


def _resolve_dprod_conn_string(project: Project, target_contract: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Resolve the connection a dprod DQ suite runs against (the deployed product
    target) + enforce the pre-flight guards. Returns (conn_string, error).

    Uses the SAME resolver serving.py:deploy_virtual_view uses, so it handles an
    SA product's own connection AND a CF product's :CONSUMES-borrowed connection.
    Postgres + the warehouse platforms the generated runner speaks (Databricks /
    Snowflake / MySQL) are supported — gated on the SQLAlchemy dialect being
    installed (preflighted) — and require the product's virtual view to be
    deployed first (the suite tests the vw_<name> relations)."""
    from sqlmodel import Session as _Session
    from .pg_resolver import resolve_read_connection_for_consumer
    from .routers.connections import build_connection_string
    from .neo4j_client import neo4j_session

    contract_id = target_contract or f"{project.project_code}-contract"
    with _Session(engine) as session:
        prj = session.get(Project, project.id) or project
        try:
            # Served-location-first: a consumer over a materialized source tests
            # the served tables (e.g. Databricks) — reports the true served
            # platform to the dialect preflight below, not the origin.
            platform, conn_ref, _borrowed = resolve_read_connection_for_consumer(
                prj, session, contract_id)
        except Exception as e:  # noqa: BLE001
            return None, f"Could not resolve the product's connection: {e}"
        if platform in ("postgres", "postgresql"):
            # Transient DSN from the STRUCTURED ref (the single connection contract).
            dsn = build_connection_string("postgres", conn_ref)
        else:
            # Warehouse targets (Databricks / Snowflake / MySQL): the generated GX
            # runner is multi-platform (SQLAlchemy URL mapping + per-dialect
            # identifier quoting), so build the served-target DSN the same way the
            # deploy path does. Preflight the dialect first so a missing driver
            # fails with an actionable "pip install X" instead of a cryptic crash
            # inside the runner subprocess.
            drv_err = _preflight_sqlalchemy_dialect(platform)
            if drv_err:
                return None, drv_err
            dsn = build_connection_string(platform, conn_ref)
        if not dsn:
            return None, "No connection available for the deployed product (deploy it first)."

    # Deploy guard — the vw_<name> views must exist.
    try:
        with neo4j_session(
            project.neo4j_host, project.neo4j_port,
            project.neo4j_user, project.neo4j_password, project.neo4j_database,
        ) as ns:
            row = ns.run(_DEPLOY_STATUS_QUERY, contract_id=contract_id).single()
        status = (row and row.get("deployment_status")) or "pending"
    except Exception:  # noqa: BLE001
        status = "pending"
    if status != "deployed":
        return None, (
            "Deploy the product first — Product DQ tests validate the deployed views "
            f"(virtual-view deployment status is '{status}', expected 'deployed').")
    return dsn, None


def _resolve_catalog_conn_string(project: Project) -> tuple[Optional[str], Optional[str]]:
    """Resolve the connection a CATALOG DQ suite runs against (the SOURCE DB),
    platform-aware. A registered SourceBinding builds the source DSN via
    ``build_connection_string`` (the single connection contract); non-Postgres
    additionally preflights the SQLAlchemy dialect (actionable 'pip install X' on
    a missing driver). Catalog tests read RAW source tables, so there is no
    deploy guard. Returns (conn_string, error)."""
    from sqlmodel import Session as _Session
    from .routers.connections import resolve_source_connection_ref, build_connection_string

    if project.id is None:
        return None, None
    with _Session(engine) as session:
        prj = session.get(Project, project.id) or project
        try:
            platform, conn_ref = resolve_source_connection_ref(prj, session)
        except Exception:  # noqa: BLE001 — no resolvable source
            return None, None
    plat = (platform or "postgres").lower()
    if plat in ("postgres", "postgresql"):
        return (build_connection_string(plat, conn_ref) or None), None
    drv_err = _preflight_sqlalchemy_dialect(plat)
    if drv_err:
        return None, drv_err
    dsn = build_connection_string(plat, conn_ref)
    if not dsn:
        return None, f"No source connection available for platform '{plat}'."
    return dsn, None


async def execute_dq_tests(
    project: Project,
    stage_run_id: Optional[int],
    fail_fast: str = "no",
    sample_limit: str = "",
    framework: Optional[str] = None,
    source_mode: str = "catalog",
    target_contract: Optional[str] = None,
) -> AsyncGenerator[dict, None]:
    """Execute the generated DQ test suite and load results into the graph.

    Yields streaming events (text_delta) and ends with stage_complete/error.
    Records a DQTestRun row on success.

    Pass `framework` explicitly (from the workflow's enabled exclusive_group stage)
    to avoid stale-dir ambiguity when both dq_tests_gx/ and dq_tests_python/ exist
    after a framework switch.

    ``source_mode='catalog'`` (default) runs against the SOURCE DB, resolved
    platform-aware from the project's SourceBinding (a registered Snowflake/MySQL/
    Databricks/Postgres source builds its DSN + preflights the SQLAlchemy
    dialect). ``source_mode='dprod'`` runs against the DEPLOYED product target
    (resolved via the shared connection resolver), gated on the served platform +
    deployment status.
    """
    project_dir = BASE_PROJECT_DIR / project.project_code
    # Prefer explicit framework over disk detection to avoid stale-dir ambiguity.
    # The on-disk subdir is mode-aware (dprod → `_dprod` suffix) so an SA product
    # holding both a catalog and a dprod suite resolves the right one.
    if framework is not None:
        fw_meta = next((fw for fw in _FRAMEWORKS if fw["framework"] == framework), None)
        if fw_meta:
            subdir = dq_subdir(fw_meta["subdir"], source_mode)
            script = project_dir / subdir / fw_meta["script"]
            ctx: Optional[ExecutionContext] = ExecutionContext(
                framework=framework,
                script_path=script,
                results_dir=project_dir / subdir / fw_meta["results_subdir"],
                project_dir=project_dir,
            ) if script.exists() else None
        else:
            ctx = _detect_framework(project_dir, source_mode)
    else:
        ctx = _detect_framework(project_dir, source_mode)
    if ctx is None:
        yield {
            "type": "error",
            "message": (
                f"No generated test script found under {project_dir}. "
                "Run the Generate DQ Tests stage first."
            ),
        }
        return

    # Resolve the connection: catalog → source DB (platform-aware via the
    # SourceBinding); dprod → deployed product target (guarded: served platform +
    # deployed). Either resolver failure is a clean stage error.
    if source_mode == "dprod":
        conn_string, err = _resolve_dprod_conn_string(project, target_contract)
    else:
        conn_string, err = _resolve_catalog_conn_string(project)
    if err:
        yield {"type": "error", "message": err}
        return

    started_at = datetime.now(timezone.utc)
    ctx.results_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: run validations
    run_cmd = [sys.executable, str(ctx.script_path), "--conn-string", conn_string]
    if sample_limit:
        try:
            int(sample_limit)
            run_cmd += ["--sample", sample_limit]
        except ValueError:
            pass
    run_cmd += ["--output", str(ctx.results_dir)]

    run_exit = 0
    run_timeout = False
    run_tail = ""  # keep the last chunk of runner output to explain a hard crash
    async for ev in _stream_subprocess(run_cmd, ctx.project_dir, f"Running {ctx.framework.upper()} validations"):
        if ev["type"] == "_exit":
            run_exit = ev["exit_code"]
            run_timeout = bool(ev.get("timeout"))
        else:
            if ev.get("type") == "text_delta":
                run_tail = (run_tail + ev.get("text", ""))[-2000:]
            yield ev

    if run_timeout:
        yield {
            "type": "error",
            "message": f"Validation run exceeded {_EXEC_TIMEOUT_SECONDS // 60} min timeout; process killed.",
        }
        return

    # A legitimate run always writes a results JSON — even when every expectation
    # fails. No results file means the runner crashed before validating (e.g. a
    # bad connection, an unresolvable relation, or a malformed generated script),
    # which the non-zero-exit path below would otherwise swallow as ordinary test
    # failures and complete green. Surface it as a real stage error instead.
    # Compare against started_at so a stale results file from a PRIOR run can't
    # mask this run's crash (the recorder's own freshness window would otherwise
    # re-record the old file).
    produced_results = any(
        f.stat().st_mtime >= started_at.timestamp() for f in ctx.results_dir.glob("*.json")
    )
    if not produced_results:
        tail = run_tail.strip()
        tail_msg = f"\n\nLast output:\n{tail[-1200:]}" if tail else ""
        yield {
            "type": "error",
            "message": (
                f"The DQ validation run produced no results (runner exited with code "
                f"{run_exit}). The suite did not execute against the target — check the "
                f"connection and the generated {ctx.framework.upper()} script.{tail_msg}"
            ),
        }
        return

    # run_gx_validations.py exits non-zero when any expectation fails — that's
    # still a valid run. Only treat fail_fast='yes' or missing-output as fatal.
    test_failures = run_exit != 0
    if test_failures and fail_fast == "yes":
        yield {"type": "error", "message": f"Validation run exited with code {run_exit}; stopping per fail_fast=yes."}
        return

    # Step 2: load results into graph
    loader = _loader_script_path(ctx.framework)
    if loader is None:
        yield {
            "type": "text_delta",
            "text": f"\n⚠ No loader script available for framework '{ctx.framework}'; skipping graph load.\n",
        }
    else:
        load_cmd = [
            sys.executable, str(loader),
            "--results-dir", str(ctx.results_dir),
            "--project-code", project.project_code,
            "--framework", ctx.framework,
            "--host", project.neo4j_host,
            "--bolt-port", str(project.neo4j_port),
            "--username", project.neo4j_user,
            "--password", project.neo4j_password,
            "--database", project.neo4j_database,
        ]
        # dprod results anchor to :DProdColumn (the meta.columnUri emitted by the
        # generator is a dprod URI); the loader switches its column match + link
        # relationship on this flag. Catalog mode omits it → byte-identical path.
        if source_mode == "dprod":
            load_cmd += ["--source-mode", "dprod"]
        load_exit = 0
        async for ev in _stream_subprocess(load_cmd, ctx.project_dir, "Loading :TestRun / :TestResult nodes"):
            if ev["type"] == "_exit":
                load_exit = ev["exit_code"]
            else:
                yield ev
        if load_exit != 0:
            yield {"type": "text_delta", "text": f"\n⚠ Loader exited with code {load_exit}.\n"}

    # Step 3: record DQTestRun row
    completed_at = datetime.now(timezone.utc)
    try:
        skill_name = {
            "gx": "data-quality-testing-gx",
            "pandera": "data-quality-testing-python",
        }[ctx.framework]
        with Session(engine) as s:
            record_dq_test_run(
                session=s,
                project_id=project.id,
                project_code=project.project_code,
                stage_run_id=stage_run_id,
                skill_name=skill_name,
                started_at=started_at,
                completed_at=completed_at,
                # Mode-aware: dprod results live under the `_dprod` suffix. Pass
                # the exact dir the runner wrote to so dprod runs get recorded.
                results_dir=ctx.results_dir,
            )
    except Exception as e:
        yield {"type": "text_delta", "text": f"\n⚠ Failed to record DQTestRun: {e}\n"}

    yield {
        "type": "stage_complete",
        "cost_usd": 0.0,
        "duration_ms": int((completed_at - started_at).total_seconds() * 1000),
        "session_id": None,
        "is_error": False,
    }
