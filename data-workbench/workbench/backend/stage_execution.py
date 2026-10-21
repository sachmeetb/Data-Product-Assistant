"""Reusable pipeline-stage execution, decoupled from the WebSocket transport.

Extracted (verbatim) from ``routers/websocket.py`` so BOTH the WebSocket handler
and the MCP ``run_stage`` tool can start a stage. The ONLY parameterization is
``event_sink`` — an async callable ``(msg: dict) -> Any`` that receives streamed
events. The WebSocket handler passes a socket-send wrapper; the headless MCP
path passes a no-op. All persistence (StageRun status, StageExecution row, graph
linking) runs in a background task and is transport-independent — it "always
runs even if the caller is gone", exactly as before.
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, Optional

from sqlmodel import Session, select

from .archetypes import STAGE_REGISTRY
from .config import BASE_PROJECT_DIR
from .database import engine
from .message_queue import message_queue
from .models import Project, StageExecution, StageRun, StageStatus, Workflow
from .pipeline import build_prompt, get_stage
from .sdk_runner import run_stage_streaming

# Track active background stage tasks so they survive caller (WS) disconnects.
_active_tasks: dict[str, asyncio.Task] = {}

# Cap persisted log_json size to keep SQLite rows bounded.
LOG_CAP_BYTES = 2_000_000

EventSink = Callable[[dict], Awaitable[Any]]

# run_id -> project_code, so the MCP question tools can authorize answers by
# project. Populated by start_stage_run; cleared when the run's task ends.
_run_project: dict[str, str] = {}


def project_for_run(run_id: str) -> Optional[str]:
    """Return the project_code that owns an active run (or None if not active)."""
    return _run_project.get(run_id)


_PENDING_FILTERS_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})-[:MATERIALISES_AS]->(:DProdDataProduct)
      -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
      -[:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform)
WHERE coalesce(dt.filterIntent, '') <> '' AND coalesce(dt.filterPredicate, '') = ''
RETURN ods.uri AS output_dataset_uri, coalesce(dt.filterIntent, '') AS filter_intent
"""


async def _autocompile_filters_for_serving(project: Project, session: Session) -> None:
    """Compile any plain-language dataset filter (filterIntent) into a grounded
    SQL predicate just before serving generates the view.

    For each output dataset that has a `filterIntent` but no `filterPredicate`:
    interpret (grounded against the bound sources), then persist via the
    standard filter-write path (which re-runs the safety gate). Datasets whose
    predicate is already set (engineer override / prior compile) are skipped —
    we never clobber. Raises ValueError if a declared intent cannot be compiled
    to a valid predicate, so the stage fails loudly rather than silently
    serving every row.
    """
    from .routers.filter_intent import interpret_filter_intent
    from .routers.dataset_transform import update_dataset_transform_filter, FilterUpdate
    from .neo4j_client import neo4j_session
    from fastapi import HTTPException

    contract_id = f"{project.project_code}-contract"
    try:
        with neo4j_session(
            project.neo4j_host, project.neo4j_port,
            project.neo4j_user, project.neo4j_password, project.neo4j_database,
        ) as ns:
            pending = [
                {"uri": r["output_dataset_uri"], "intent": (r["filter_intent"] or "").strip()}
                for r in ns.run(_PENDING_FILTERS_QUERY, contract_id=contract_id)
            ]
    except Exception:
        # Graph unreachable — let the stage proceed; the generator's
        # intent-without-predicate gate is the backstop.
        return

    for pf in pending:
        intent = pf["intent"]
        if not intent:
            continue
        result = await interpret_filter_intent(
            intent=intent, project=project, output_dataset_uri=pf["uri"]
        )
        predicate = (result.predicate or "").strip()
        if not predicate:
            raise ValueError(
                f"Could not compile the row filter '{intent}' into SQL for "
                f"{pf['uri']}. Finalize it in the Filter Review panel (or via "
                f"set_dataset_filter) before serving."
            )
        try:
            update_dataset_transform_filter(
                project.id,
                FilterUpdate(output_dataset_uri=pf["uri"], filter=predicate),
                session,
            )
        except HTTPException as e:
            detail = e.detail
            msg = detail.get("message") if isinstance(detail, dict) else str(detail)
            raise ValueError(
                f"Auto-compiled filter for {pf['uri']} was rejected by the safety gate: {msg}"
            )


def ensure_serving_filters_compiled(project: Project, session: Session) -> None:
    """Sync entry point for the serving BUILD/DEPLOY paths (dbt + lakehouse),
    which bypass ``start_stage_run``'s async pre-run hook and call
    ``generate_view_ddl`` in-process. Compiles any plain-language dataset filter
    (filterIntent → filterPredicate) BEFORE the generator reads it — exactly what
    the ``serving_virtual_view`` stage already does — so a dbt/lakehouse build of
    a product with a Shape-step filter doesn't trip the generator's
    intent-without-predicate guard.

    Idempotent (skips already-compiled predicates, never clobbers). Propagates
    the hook's ``ValueError`` on a genuinely uncompilable filter so the caller
    fails loudly with a clear message rather than silently serving every row.
    """
    import asyncio
    try:
        asyncio.run(_autocompile_filters_for_serving(project, session))
    except RuntimeError as e:
        # Defensive: a running event loop in a sync caller is not expected here
        # (these are sync FastAPI/MCP handlers), but if one exists, run the
        # coroutine to completion on a dedicated one-shot thread.
        if "running event loop" not in str(e):
            raise
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(
                lambda: asyncio.run(_autocompile_filters_for_serving(project, session))
            ).result()


# Stages that read directly from the source database and may need a different
# skill + connection DSN when a non-Postgres SourceBinding is present.
#
# Skill overrides are sourced from routers.connections to keep a single source
# of truth for platform → skill mappings used by both execution and config-options.
from .routers.connections import (  # noqa: E402
    DISCOVERY_SKILL_BY_PLATFORM as _DISC_SKILLS,
    PROFILING_SKILL_BY_PLATFORM as _PROF_SKILLS,
    build_connection_string as _build_conn_str,
)

_PLATFORM_SKILL_OVERRIDES: dict[str, dict[str, str]] = {
    "data_discovery": _DISC_SKILLS,
    "data_profiling": _PROF_SKILLS,
    # Composite variants — the actual stage IDs used in default workflows.
    # skill_override replaces the FIRST sub-stage skill (the platform-routable
    # one); remaining sub-skills (load_schema, load_profiles) are kept as-is.
    "data_discovery_composite": _DISC_SKILLS,
    "data_profiling_composite": _PROF_SKILLS,
    # file_export uses the same skill regardless of platform but needs the
    # right connection string routed from the SourceBinding (not pg_connection).
    # Empty dict = route connection only, no skill override.
    "file_export": {},
    # serving_virtual_view only needs dialect derived from the platform —
    # the DDL generator reads from Neo4j, not the source DB directly.
    "serving_virtual_view": {},
    # serving_lakehouse_export always compiles the DuckDB dialect; it still
    # routes through the platform context so the borrowed source platform is
    # available (for reading the live source via *_scan or exported Parquet).
    "serving_lakehouse_export": {},
}
_PLATFORM_ROUTED_STAGES = frozenset(_PLATFORM_SKILL_OVERRIDES)

# Serving stages that only need the SQL *dialect* derived from the platform
# (they read the shape from Neo4j, not the source DB, so no connection string).
# For these, a consumer-aligned (dpe-cf) project with no local SourceBinding
# borrows the dialect from its :CONSUMES'd source — the same borrow the
# connection path already does.
_SERVING_DIALECT_STAGES = frozenset({"serving_virtual_view", "serving_lakehouse_export"})

# Maps SourceBinding platform_type → generate_view_ddl.py dialect name.
# Phase 7 MySQL reconcile (transform-portability.md): MySQL now routes to the
# existing generate_view_ddl.MySQLDialect (backtick quoting, CHAR casts, no NULLS
# LAST, portable SUBSTR/CONCAT/split emulation) instead of the "ansi" fallback,
# so MySQL-served views emit MySQL-valid SQL AND the capability preflight
# validates them (mysql is a served native-view platform in the artifact).
_PLATFORM_DIALECT_MAP: dict[str, str] = {
    "postgres":   "postgres",
    "postgresql": "postgres",
    "snowflake":  "snowflake",
    "databricks": "databricks",
    "bigquery":   "bigquery",
    "mysql":      "mysql",
}


def _resolve_platform_context(
    stage_id: str | None,
    project,
    session,
) -> dict | None:
    """Return a platform_context dict for build_prompt() when the project has a
    SourceBinding that changes which skill / connection string / dialect to use.

    Returns ``None`` when:
    - the stage isn't in _PLATFORM_ROUTED_STAGES, or
    - the project has no SourceBinding (legacy Postgres path), or
    - the bound platform is Postgres and provides no useful override.

    The returned dict may contain:
    - ``connection_string`` — DSN threaded via ``$WB_SOURCE_DSN`` for ``{source_dsn}``
    - ``skill_override``    — skill name to substitute in the preamble
    - ``dialect``           — SQL dialect for serving_virtual_view (derived from
                              platform_type; only set when it differs from the
                              "postgres" default so no-op for Postgres bindings)
    - ``platform_type``     — informational
    """
    if stage_id not in _PLATFORM_ROUTED_STAGES:
        return None

    from sqlmodel import select as _select
    from .models import SourceBinding, PlatformConnection
    from .platform.secrets import resolve_secret

    binding = session.exec(
        _select(SourceBinding).where(SourceBinding.project_id == project.id)
    ).first()

    # ── serving stages: only the SQL dialect matters (shape read from Neo4j) ──
    if stage_id in _SERVING_DIALECT_STAGES:
        platform_type: str | None = None
        target_dialect = ""
        # Per-source served relations (dpe-cf consuming a materialized source):
        # {_safe_name(source_dataset_physical): "catalog.schema.relation"}. Threaded
        # into the view-DDL skill so the FROM references the source's real tables.
        source_served_map: dict | None = None
        # Deploy-target namespace: prefer the per-contract MaterializationTarget
        # (universal — works for dpe-cf which has no SourceBinding), fall back to
        # the SourceBinding (legacy dpe-sa rows set before the move).
        view_target_namespace = ""
        try:
            from .models import MaterializationTarget as _MatTarget
            _mt = session.get(_MatTarget, f"{project.project_code}-contract")
            if _mt is not None:
                view_target_namespace = (getattr(_mt, "view_target_namespace", "") or "").strip()
        except Exception:
            pass
        if binding is not None:
            conn = session.get(PlatformConnection, binding.connection_id)
            if conn is not None:
                platform_type = conn.platform_type
                target_dialect = (
                    (binding.target_dialect or "").strip()
                    if hasattr(binding, "target_dialect") else ""
                )
                if not view_target_namespace:
                    view_target_namespace = (
                        (binding.view_target_namespace or "").strip()
                        if hasattr(binding, "view_target_namespace") else ""
                    )
        if platform_type is None:
            # dpe-cf: no local SourceBinding. Prefer the SERVED location of the
            # :CONSUMES'd source (materialized target, e.g. Databricks) over the
            # origin — so the emitted view uses the right dialect AND its FROM
            # references the source's real materialized tables (source_served_map).
            # Falls back to the origin-borrow when the source isn't materialized.
            try:
                from .pg_resolver import (
                    resolve_consumed_source_serving,
                    resolve_source_connection_for_project,
                )
                loc = resolve_consumed_source_serving(
                    project, session, f"{project.project_code}-contract"
                )
                if loc is not None:
                    platform_type = loc.served_platform
                    source_served_map = loc.relations or None
                else:
                    borrowed_platform, _ref, _from = resolve_source_connection_for_project(
                        project, session, f"{project.project_code}-contract"
                    )
                    platform_type = borrowed_platform
            except Exception:
                platform_type = None
        if not platform_type:
            # No source binding and no :CONSUMES borrow resolved. Assume Postgres
            # ONLY when a target namespace was chosen — otherwise there's nothing
            # to override and the Postgres default path stays a no-op.
            if view_target_namespace:
                platform_type = "postgres"
            else:
                return None
        # Prefer target_dialect (set by configure_serving) over the platform-map
        # inference. Surface a context when the effective dialect differs from the
        # "postgres" default OR a target namespace was chosen (2-level products can
        # now pick a non-'public' target schema) — else Postgres stays a no-op.
        derived_dialect = _PLATFORM_DIALECT_MAP.get(platform_type.lower(), "ansi")
        effective_dialect = target_dialect or derived_dialect
        # A served map must reach the skill even for a Postgres source served to a
        # DIFFERENT Postgres instance (dbt-materialized) — don't no-op it away.
        if effective_dialect == "postgres" and not view_target_namespace and not source_served_map:
            return None
        return {
            "platform_type": platform_type,
            "dialect": effective_dialect,
            "view_target_namespace": view_target_namespace,
            "source_served_map": source_served_map,
        }

    # ── non-serving stages (discovery/profiling/export): need connection string ──
    if binding is None:
        return None
    conn = session.get(PlatformConnection, binding.connection_id)
    if conn is None:
        return None

    platform_type = conn.platform_type
    skill_override = _PLATFORM_SKILL_OVERRIDES.get(stage_id, {}).get(platform_type)

    password = resolve_secret(conn.secret_ref)
    connection_ref = {
        "host": conn.host,
        "port": conn.port,
        "database": conn.database,
        "username": conn.username,
        "resolved_password": password,
        # Forward provider extras (file globs, warehouse role/warehouse, etc.)
        # so downstream file/warehouse stages have them available.
        "extra_config": json.loads(conn.extra_config_json or "{}"),
    }
    ctx: dict = {
        "platform_type": platform_type,
        "connection_string": _build_conn_str(platform_type, connection_ref),
    }
    if skill_override:
        ctx["skill_override"] = skill_override
    return ctx


def _resolve_source_dsn(project, session) -> str:
    """The credential-bearing source DSN for a stage, resolved from the project's
    ``SourceBinding`` for EVERY platform (the single connection contract). Threaded
    into the SDK subprocess env as ``WB_SOURCE_DSN`` so ``build_prompt``'s
    ``{source_dsn}`` (rendered as ``$WB_SOURCE_DSN``) never inlines the credential.
    Empty when the project has no resolvable source binding."""
    from .routers.connections import resolve_source_connection_ref
    try:
        platform, conn_ref = resolve_source_connection_ref(project, session)
    except Exception:
        return ""
    return _build_conn_str(platform, conn_ref) or ""


def _validate_cmig_conversion(project_code: str) -> tuple[bool, bool]:
    """Fail-closed check that forward-engineering produced real output. Returns
    (ok, has_actions): ok iff a conversion.json exists AND at least one converted
    file landed in code_migration/target/; has_actions iff any construct was
    bucketed manual_action / unsupported (→ converted_with_actions)."""
    import json as _json
    from . import code_migration_orchestrator as cmo
    conv_p = cmo.conversion_path(project_code)
    tgt = cmo.target_dir(project_code)
    if not conv_p.exists():
        return False, False
    try:
        conv = _json.loads(conv_p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False, False
    # At least one converted code file (anything other than the reports).
    produced = any(
        p.is_file() and p.name not in ("conversion.json", "design.md")
        for p in tgt.rglob("*")
    ) if tgt.is_dir() else False
    if not produced:
        return False, False
    constructs = conv.get("constructs") or conv.get("items") or []
    has_actions = any(
        isinstance(c, dict) and c.get("status") in ("manual_action", "unsupported")
        for c in constructs
    )
    return True, has_actions


def resolve_dq_source_mode(project, workflow_id: Optional[str]) -> str:
    """Catalog vs dprod for a DQ stage, keyed off the workflow it runs in.

    The `product_dq_testing` workflow group carries the dprod stages (contract
    rules on :DProdColumn, tested against the deployed product views); every other
    DQ workflow (`dq_testing`, legacy flat) is catalog mode (source :Column). This
    single choke-point mirrors data-mapping-neo4j's --source-mode selection."""
    return "dprod" if workflow_id == "product_dq_testing" else "catalog"


def _dq_failure_context(project, workflow_id: Optional[str], session: Session) -> dict:
    """Return {dq_tests_dir, analysis_output} for the dq_failure_analysis stage.

    Reads the active exclusive_group stage from the project's DQ workflow to
    determine which framework (GX vs Pandera) is active. Honors the ACTUAL
    workflow_id the stage runs in (dq_testing OR product_dq_testing), so a dprod
    suite resolves its own framework. GX defaults are used when the workflow isn't
    found or no exclusive_group is set.
    """
    # dprod flags for the analyze_failures.py invocation — empty in catalog mode,
    # so the source-side report is byte-identical. Product-DQ (product_dq_testing)
    # gets --source-mode dprod so column context comes from :DProdColumn.
    from .dq_test_executor import dq_subdir
    _mode = resolve_dq_source_mode(project, workflow_id)
    _source_flags = (
        f"--source-mode dprod --target-contract {project.project_code}-contract"
        if _mode == "dprod" else ""
    )
    # dprod suites live in a `_dprod`-suffixed dir (see dq_subdir) so the failure
    # analysis reads the right suite when an SA product holds both.
    _gx_dir = dq_subdir("dq_tests_gx", _mode)
    _gx_defaults = {
        "dq_tests_dir": _gx_dir,
        "analysis_output": f"{_gx_dir}/failure_analysis.md",
        "dq_source_flags": _source_flags,
    }
    try:
        dq_wf = session.exec(
            select(Workflow).where(
                Workflow.project_id == project.id,
                Workflow.workflow_id == (workflow_id or "dq_testing"),
            )
        ).first()
        if dq_wf is None or not dq_wf.workflow_json:
            return _gx_defaults
        stages = json.loads(dq_wf.workflow_json)
        active = next(
            (s["stage_id"] for s in stages
             if s.get("exclusive_group") == "dq_test_gen" and s.get("enabled", True)),
            None,
        )
        if active == "dq_test_generation_python":
            _py_dir = dq_subdir("dq_tests_python", _mode)
            return {
                "dq_tests_dir": _py_dir,
                "analysis_output": f"{_py_dir}/failure_analysis.md",
                "dq_source_flags": _source_flags,
            }
    except Exception:
        pass
    return _gx_defaults


async def start_stage_run(
    project_id: int,
    stage_number: int,
    *,
    workflow_id: Optional[str] = None,
    stage_config: Optional[dict] = None,
    event_sink: EventSink,
    acting_role: Optional[str] = None,
) -> Optional[str]:
    """Start a pipeline stage in a background task; return its run_id.

    Returns ``None`` if the stage isn't runnable here — either the project/stage
    wasn't found, or it's a non-LLM/non-backend stage that completes via the
    ``/stages/{n}/complete`` endpoint rather than executing. Streamed events go
    to ``event_sink``; the StageExecution/StageRun persistence happens in the
    background task regardless of the sink.

    ``acting_role`` is the caller's **account role** (``owner`` | ``engineer``).
    When provided (WS + MCP paths thread it through), the PO↔Engineer boundary is
    enforced here — a shared choke point so both front doors agree. ``None``
    means "unauthenticated / auth disabled" and skips the check (local dev).
    """
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if not project:
            await event_sink({"type": "error", "message": "Project not found"})
            return None

        # Acceptance gate: running a pipeline stage is an engineer mutation, so
        # it requires the governing product request to be accepted. Warn-only by
        # default (WB_ENFORCE_ACCEPT_GATE off) — emit a warning and continue;
        # hard-block when the gate is enforced. Projects without a governing
        # request (dq/dd) are never gated. Shared with the REST mutation routes.
        from .request_guard import check_engineer_mutation_allowed
        guard = check_engineer_mutation_allowed(project, session)
        if guard.blocked:
            if not guard.allowed:
                await event_sink({
                    "type": "error",
                    "message": guard.message,
                    "required_action": guard.required_action,
                    "request_id": guard.request_id,
                })
                return None
            await event_sink({"type": "warning", "message": guard.message})

        stage_def = None
        stage_id = None

        # Resolve stage definition from workflow
        if workflow_id and project.multi_workflow:
            wf = session.exec(
                select(Workflow).where(
                    Workflow.project_id == project_id, Workflow.workflow_id == workflow_id,
                )
            ).first()
            if wf and wf.workflow_json:
                stages = [s for s in json.loads(wf.workflow_json) if s.get("enabled", True)]
                if stage_number <= len(stages):
                    stage_id = stages[stage_number - 1]["stage_id"]
                    stage_def = STAGE_REGISTRY.get(stage_id)
        elif project.workflow_json:
            workflow = [s for s in json.loads(project.workflow_json) if s.get("enabled", True)]
            if stage_number <= len(workflow):
                stage_id = workflow[stage_number - 1]["stage_id"]
                stage_def = STAGE_REGISTRY.get(stage_id)

        if not stage_def:
            stage_def = get_stage(stage_number)

        # Coarse role gate (PO↔Engineer boundary). Shared by the WS + MCP paths
        # via ``acting_role``. Refuse when the caller's account role can't run a
        # stage of this owner_role (e.g. an owner trying to run an engineer stage).
        if acting_role is not None and stage_def:
            from .authz import role_can_run_stage
            if not role_can_run_stage(acting_role, stage_def.get("owner_role")):
                await event_sink({
                    "type": "error",
                    "message": (
                        f"Your role '{acting_role}' cannot run this stage "
                        f"(owned by '{stage_def.get('owner_role')}')."
                    ),
                })
                return None

        # Code-migration forward-engineering readiness gate (the REAL blocking of
        # the spec review — has_review alone doesn't block). Enforced at this
        # shared choke point so WS + MCP agree; the CLI + recovery/orphan paths
        # call the same code_migration_orchestrator.require_forward_ready.
        if stage_id == "cmig_forward_engineer":
            from . import code_migration_orchestrator as cmo
            ready = cmo.require_forward_ready(project, session)
            if not ready["ready"]:
                await event_sink({
                    "type": "error",
                    "message": "forward-engineering is not ready: " + "; ".join(ready["missing"]),
                    "missing": ready["missing"],
                })
                return None

        # DQ testing stages are non-LLM but backend-executed — run them through
        # a subprocess path instead of the Claude SDK. Other non-LLM stages
        # (initiate, publish) are completed via /api/.../stages/.../complete.
        is_dq_exec = stage_id == "dq_test_execution"
        is_dq_gen = stage_id in ("dq_test_generation_gx", "dq_test_generation_python")
        is_backend_driven = is_dq_exec or is_dq_gen
        if not stage_def.get("requires_llm") and not is_backend_driven:
            # Non-LLM stages complete through their OWN action (/stages/{n}/complete
            # or a dedicated endpoint), not run_stage. Emit a terminal, non-alarming
            # signal before returning so any caller that streams events (WS today,
            # others later) clears its `runningStage` and refetches rather than
            # stranding the card at "Running…". The frontend treats
            # stage_status_changed as a clear-and-refetch trigger. Defense-in-depth:
            # the primary fix (Pipeline.tsx routes non-LLM reruns through their own
            # action) means the UI no longer reaches this path.
            _sr = session.exec(
                select(StageRun).where(
                    StageRun.project_id == project_id,
                    StageRun.stage_number == stage_number,
                    *([StageRun.workflow_id == workflow_id] if workflow_id else []),
                )
            ).first()
            await event_sink({
                "type": "stage_status_changed",
                "stage_number": stage_number,
                "workflow_id": workflow_id,
                "status": _sr.status if _sr else None,
                "message": "This stage completes through its own action, not run_stage.",
            })
            return None

        # Serving pre-run hook: auto-compile any plain-language dataset filter
        # (filterIntent → filterPredicate), grounded against the now-bound
        # sources. Runs BEFORE the generator reads the predicate. No human
        # review (per design); the SQL-validity gate + the generator's
        # intent-without-predicate gate are the safety nets. Runs on BOTH the UI
        # and MCP paths since both enter through start_stage_run. Raises on an
        # uncompilable filter so the stage fails cleanly with a clear message.
        if stage_id == "serving_virtual_view":
            await _autocompile_filters_for_serving(project, session)

        # Build the prompt FIRST — before marking the stage running — so a bad
        # or missing required config (e.g. discovery_tables) surfaces as a clean
        # error to the caller instead of orphaning the stage at 'running'.
        platform_ctx = None if is_backend_driven else _resolve_platform_context(stage_id, project, session)
        # dq_failure_analysis needs framework-specific paths injected so the
        # prompt references the correct test directory and output file.
        effective_config = dict(stage_config or {})
        if stage_id == "dq_failure_analysis":
            effective_config.update(_dq_failure_context(project, workflow_id, session))
        prompt = "" if is_backend_driven else build_prompt(stage_def, project, effective_config, platform_ctx)
        project_dir = str(BASE_PROJECT_DIR / project.project_code)
        project_code = project.project_code

        # Tier-0 credential containment: thread a registered source connection's
        # credential-bearing DSN into the SDK subprocess env (never the prompt).
        # build_prompt rendered the placeholder as "$WB_SOURCE_DSN" — the skill's
        # bash invocation expands it at run time from this env.
        from .pipeline import WB_SOURCE_DSN_ENV, source_dsn_from_platform_context
        _source_dsn = source_dsn_from_platform_context(platform_ctx)
        if not _source_dsn and not is_backend_driven:
            # Not a platform-routed stage (or no routing ctx), but the template may
            # still reference {source_dsn}. Resolve the source DSN from the binding
            # for every platform so the credential is contained in env, never inline.
            _source_dsn = _resolve_source_dsn(project, session)
        sdk_env: dict[str, str] = {WB_SOURCE_DSN_ENV: _source_dsn} if _source_dsn else {}

        # Find the StageRun record and mark it running.
        q = select(StageRun).where(
            StageRun.project_id == project_id,
            StageRun.stage_number == stage_number,
        )
        if workflow_id:
            q = q.where(StageRun.workflow_id == workflow_id)
        stage_run = session.exec(q).first()
        if stage_run:
            stage_run.status = StageStatus.running
            stage_run.started_at = datetime.now(timezone.utc)
            session.add(stage_run)
            session.commit()

    run_id = str(uuid.uuid4())
    _run_project[run_id] = project_code

    # The event sink doubles as the agent-ask relay (mid-stage questions).
    message_queue.register_run(run_id, event_sink)

    started_at = datetime.now(timezone.utc)
    stage_started_event = {
        "type": "stage_started",
        "stage_number": stage_number,
        "stage_name": stage_def["name"],
        "run_id": run_id,
        "workflow_id": workflow_id,
        "started_at": started_at.isoformat(),
    }
    await event_sink(stage_started_event)

    async def _event_source():
        if is_dq_gen:
            from .dq_test_generator import generate_dq_tests
            framework = "pandera" if stage_id == "dq_test_generation_python" else "gx"
            # Catalog (source :Column) vs dprod (product contract) — keyed off the
            # workflow this stage runs in (product_dq_testing → dprod).
            dq_mode = resolve_dq_source_mode(project, workflow_id)
            with Session(engine) as s:
                prj = s.get(Project, project_id)
            async for ev in generate_dq_tests(
                project=prj, framework=framework, source_mode=dq_mode,
                target_contract=(f"{prj.project_code}-contract" if dq_mode == "dprod" else None),
            ):
                yield ev
        elif is_dq_exec:
            from .dq_test_executor import execute_dq_tests
            with Session(engine) as s:
                prj = s.get(Project, project_id)
                q2 = select(StageRun).where(
                    StageRun.project_id == project_id,
                    StageRun.stage_number == stage_number,
                )
                if workflow_id:
                    q2 = q2.where(StageRun.workflow_id == workflow_id)
                sr = s.exec(q2).first()
                sr_id = sr.id if sr else None
                # Resolve active framework from workflow state to avoid stale-dir
                # ambiguity when both dq_tests_gx/ and dq_tests_python/ exist
                # after a framework switch.
                exec_framework = _dq_failure_context(prj, workflow_id, s).get("dq_tests_dir", "dq_tests_gx")
                exec_framework = "pandera" if "python" in exec_framework else "gx"
            cfg = stage_config or {}
            dq_mode = resolve_dq_source_mode(project, workflow_id)
            async for ev in execute_dq_tests(
                project=prj,
                stage_run_id=sr_id,
                fail_fast=str(cfg.get("fail_fast", "no")),
                sample_limit=str(cfg.get("sample_limit", "")),
                framework=exec_framework,
                source_mode=dq_mode,
                target_contract=(f"{prj.project_code}-contract" if dq_mode == "dprod" else None),
            ):
                yield ev
        else:
            from .sdk_runner import CODE_MIGRATION_TOOLS
            _tools = CODE_MIGRATION_TOOLS if stage_id in (
                "cmig_reverse_engineer", "cmig_forward_engineer") else None
            async for ev in run_stage_streaming(prompt, project_dir, run_id, allowed_tools=_tools, env=sdk_env):
                yield ev

    # Run the work in a background task so it survives caller disconnects
    async def _run_sdk():
        final_event = None
        events: list[dict] = [stage_started_event]
        tool_counts: dict[str, int] = {}
        total_bytes = len(json.dumps(stage_started_event))
        truncated = False

        def _capture(ev: dict):
            nonlocal total_bytes, truncated
            if truncated:
                return
            try:
                encoded = json.dumps(ev)
            except Exception:
                encoded = json.dumps({"type": ev.get("type", "unknown")})
            size = len(encoded)
            if total_bytes + size > LOG_CAP_BYTES:
                truncated = True
                events.append({"type": "log_truncated", "at_event": len(events)})
                return
            events.append(ev)
            total_bytes += size
            if ev.get("type") == "tool_use":
                name = ev.get("tool") or "unknown"
                tool_counts[name] = tool_counts.get(name, 0) + 1

        try:
            async for event in _event_source():
                if event.get("type") in ("stage_complete", "error"):
                    final_event = event
                else:
                    _capture(event)
                    await event_sink(event)
        except Exception as e:
            final_event = {"type": "error", "message": str(e)}
        finally:
            message_queue.cleanup_run(run_id)
            _active_tasks.pop(run_id, None)
            _run_project.pop(run_id, None)

        if final_event is None:
            final_event = {"type": "stage_complete", "is_error": False, "cost_usd": None, "session_id": None}

        _capture(final_event)

        completed_at = datetime.now(timezone.utc)

        # Update DB — this always runs even if the caller (WS) is gone
        new_status = None
        with Session(engine) as session:
            q = select(StageRun).where(
                StageRun.project_id == project_id,
                StageRun.stage_number == stage_number,
            )
            if workflow_id:
                q = q.where(StageRun.workflow_id == workflow_id)
            stage_run = session.exec(q).first()
            if stage_run and final_event:
                if final_event["type"] == "stage_complete" and not final_event.get("is_error"):
                    if stage_def.get("has_review"):
                        stage_run.status = StageStatus.awaiting_review
                        new_status = "awaiting_review"
                    else:
                        stage_run.status = StageStatus.complete
                        new_status = "complete"
                    stage_run.cost_usd = final_event.get("cost_usd")
                    stage_run.session_id = final_event.get("session_id")
                else:
                    stage_run.status = StageStatus.failed
                    stage_run.error_message = final_event.get("message", "Unknown error")
                    new_status = "failed"

                # Serving backstop: a "complete" serving_virtual_view must have
                # persisted a non-empty view DDL. If the generator raised its
                # validation gate (nothing stored), don't let the stage report
                # complete — flip to failed so the failure is visible instead of
                # surfacing only at deploy. Best-effort; never blocks completion.
                if new_status in ("complete", "awaiting_review") and stage_id == "serving_virtual_view":
                    try:
                        from .routers.summary import _run_query, SERVING_QUERY_S
                        proj = session.get(Project, project_id)
                        rows = _run_query(proj, SERVING_QUERY_S, project_code=proj.project_code)
                        produced = any(
                            (r.get("serving_mode") == "virtual_view") and (r.get("ddl") or "").strip()
                            for r in rows
                        )
                        if not produced:
                            stage_run.status = StageStatus.failed
                            stage_run.error_message = (
                                "Data Serving completed without persisting a valid view "
                                "(generation gate failed). Re-run after fixing the mapping transform."
                            )
                            new_status = "failed"
                    except Exception:
                        pass  # best-effort — a check hiccup must not block completion

                # Code-migration fail-closed post-run validation. An SDK success
                # event otherwise completes a stage with no artifact — prove it
                # exists + is schema-valid before flipping status, and advance the
                # CodeMigrationPlanRow state machine.
                if new_status in ("complete", "awaiting_review") and stage_id in (
                    "cmig_reverse_engineer", "cmig_forward_engineer"):
                    try:
                        from . import code_migration_orchestrator as cmo
                        proj = session.get(Project, project_id)
                        if stage_id == "cmig_reverse_engineer":
                            spec = cmo.ingest_reverse_engineered_spec(session, proj)
                            if not spec:
                                stage_run.status = StageStatus.failed
                                stage_run.error_message = (
                                    "Reverse-engineering completed without a valid codespec.json.")
                                new_status = "failed"
                        else:  # cmig_forward_engineer
                            ok, has_actions = _validate_cmig_conversion(proj.project_code)
                            cmo.record_conversion(session, proj, ok=ok, has_actions=has_actions)
                            if not ok:
                                stage_run.status = StageStatus.failed
                                stage_run.error_message = (
                                    "Forward-engineering produced no valid converted code / conversion.json.")
                                new_status = "failed"
                    except Exception:
                        pass  # best-effort — a check hiccup must not wedge the row

                stage_run.completed_at = completed_at
                session.add(stage_run)
                session.commit()
                session.refresh(stage_run)

                session.add(StageExecution(
                    stage_run_id=stage_run.id,
                    project_id=project_id,
                    workflow_id=workflow_id,
                    stage_number=stage_number,
                    run_id=run_id,
                    started_at=started_at,
                    completed_at=completed_at,
                    status=new_status or "failed",
                    cost_usd=final_event.get("cost_usd"),
                    session_id=final_event.get("session_id"),
                    error_message=(final_event.get("message") if final_event.get("type") == "error" else None),
                    event_count=len(events),
                    tool_counts_json=json.dumps(tool_counts),
                    log_json=json.dumps(events),
                    truncated=truncated,
                ))
                session.commit()

                # Token-usage ledger row (best-effort) for global/per-product rollups.
                try:
                    from . import llm_usage
                    proj = session.get(Project, project_id)
                    llm_usage.record_usage(
                        source="stage",
                        usage=final_event.get("usage"),
                        project_code=proj.project_code if proj else None,
                        run_id=run_id,
                        session=session,
                    )
                except Exception:
                    pass

        # Link graph nodes to :Project after discovery stages complete
        if new_status in ("complete", "awaiting_review") and stage_id in (
            "data_discovery_composite", "load_schema",
        ):
            try:
                from .graph_ops import ensure_project_node, link_catalogs_to_project
                with Session(engine) as gs:
                    prj = gs.get(Project, project_id)
                    if prj:
                        ensure_project_node(prj)
                        link_catalogs_to_project(prj)
            except Exception:
                pass

        # dmig: auto-push the migration package to git as soon as it's generated —
        # the package is ready once migration.json exists. Gated on git_auto_push
        # inside _maybe_auto_push (fire-and-forget daemon thread; assembles the
        # package before pushing). Mirrors the post-deploy auto-push for products.
        if new_status in ("complete", "awaiting_review") and stage_id == "dmig_generate_pipeline":
            try:
                from .routers.serving import _maybe_auto_push
                _maybe_auto_push(project_id, "engineer")
            except Exception:
                pass

        # Try to notify the caller — if the sink is dead, that's fine
        if final_event:
            await event_sink(final_event)
        if new_status:
            await event_sink({
                "type": "stage_status_changed",
                "stage_number": stage_number,
                "workflow_id": workflow_id,
                "status": new_status,
            })

    task = asyncio.create_task(_run_sdk())
    _active_tasks[run_id] = task
    return run_id
