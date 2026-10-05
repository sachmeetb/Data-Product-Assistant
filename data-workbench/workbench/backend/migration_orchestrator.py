"""Data-migration orchestration — MigrationPlan persistence + lifecycle.

Thin subsystem for the ``dmig`` archetype. Owns the durable ``MigrationPlanRow``
(SQLite) that stores a ``platform.migration_plan.MigrationPlan``, resolves the
source + target connections into the ``connection_ref`` dicts that
``serving_runtime.build_runner_env`` maps into ``WB_SOURCE_*`` / ``WB_TARGET_*``,
and advances the plan's state machine as the pipeline's ``dmig_*`` stages run.

Creates NO product/marketplace graph nodes — migration is project-keyed and
disjoint from the :DataContract / :DProdDataProduct world.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlmodel import Session, select

from .config import BASE_PROJECT_DIR
from .models import MigrationPlanRow, PlatformConnection, Project, SourceBinding
from .neo4j_client import neo4j_session
from .platform.migration_plan import (
    MigrationPlan,
    MigrationStatus,
    PhaseLogEntry,
    ReconciliationRule,
    valid_next_statuses,
)
from .platform.transfer_batch import RelationalRelationRef


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── on-disk locations ────────────────────────────────────────────────────────

def migration_dir(project_code: str) -> Path:
    """``<project>/serving/migration/`` — the migration package directory
    (parallel to the other serving modes so downloads/git reuse the same helpers)."""
    return BASE_PROJECT_DIR / project_code / "serving" / "migration"


def migration_json_path(project_code: str) -> Path:
    """Where the generator skill writes the framework-neutral contract."""
    return BASE_PROJECT_DIR / project_code / "migration" / "migration.json"


# ── connection resolution ────────────────────────────────────────────────────

def resolve_source_ref(project: Project, session: Session) -> tuple[str, dict]:
    """(platform_type, connection_ref) for the migration SOURCE (delegates to the
    shared source resolver — SourceBinding → PlatformConnection, else pg_connection)."""
    from .routers.connections import resolve_source_connection_ref
    return resolve_source_connection_ref(project, session)


def resolve_target_ref(session: Session, connection_id: Optional[int]) -> tuple[str, dict]:
    """(platform_type, connection_ref) for the migration TARGET from a registered
    PlatformConnection id. The ephemeral ``resolved_password`` rides the ref only."""
    if connection_id is None:
        return "", {}
    from .platform.secrets import resolve_secret
    conn = session.get(PlatformConnection, connection_id)
    if conn is None:
        return "", {}
    return conn.platform_type, {
        "connection_id": str(conn.id),
        "host": conn.host,
        "port": conn.port,
        "database": conn.database,
        "username": conn.username,
        "resolved_password": resolve_secret(conn.secret_ref),
        "extra_config": json.loads(conn.extra_config_json or "{}"),
    }


def _source_schema(project: Project, session: Session) -> str:
    binding = session.exec(
        select(SourceBinding).where(SourceBinding.project_id == project.id)
    ).first()
    if binding is not None and binding.default_schema:
        return binding.default_schema
    return "public"


def _target_schema_default(project: Project) -> str:
    """A per-project target schema name for the landed data (lowercased code)."""
    return (project.project_code or "migrated").lower().replace("-", "_")


# ── plan CRUD ────────────────────────────────────────────────────────────────

def get_row(session: Session, project_code: str) -> Optional[MigrationPlanRow]:
    return session.get(MigrationPlanRow, project_code)


def load_plan(session: Session, project_code: str) -> Optional[MigrationPlan]:
    row = get_row(session, project_code)
    if row is None or not row.plan_json or row.plan_json == "{}":
        return None
    try:
        return MigrationPlan.model_validate_json(row.plan_json)
    except Exception:  # noqa: BLE001 — a corrupt blob shouldn't wedge the stage
        return None


def _persist(session: Session, project_code: str, plan: MigrationPlan, row: MigrationPlanRow) -> MigrationPlanRow:
    plan.updated_at = _now()
    row.plan_json = plan.model_dump_json()
    row.status = plan.status.value
    row.updated_at = _now()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def set_status(plan: MigrationPlan, to: MigrationStatus, *, by: str = "", note: str = "") -> None:
    """Advance the plan. Uses the model's enforced ``transition()`` on the happy
    path; on a non-forward hop (e.g. an idempotent stage re-run) falls back to a
    logged direct assignment — the orchestrator drives the lifecycle
    deterministically per stage, so re-runs must never raise (the model
    explicitly permits direct assignment by an external orchestrator)."""
    if plan.status == to:
        return
    try:
        plan.transition(to, by=by, note=note)
    except ValueError:
        plan.phase_log.append(PhaseLogEntry(from_status=plan.status, to_status=to, by=by, note=note or "re-run"))
        plan.status = to
        plan.updated_at = _now()


# ── high-level operations (called by routers/migration.py) ───────────────────

def apply_plan_target(session: Session, project: Project, spec: dict) -> dict:
    """Stamp the plan's resolved target (platform / catalog / schema / write
    disposition) onto a migration spec so both the runnable package and the
    runner env agree with what was configured. Mutates + returns ``spec``."""
    row = get_row(session, project.project_code)
    if row is None:
        return spec
    _tgt_platform, _ = resolve_target_ref(session, row.target_connection_id)
    # No live connection (schema-only intent) → use the configured intent platform.
    tgt_platform = _tgt_platform or row.target_platform
    if tgt_platform:
        spec["target_platform"] = tgt_platform
    if row.target_catalog:
        spec["target_catalog"] = row.target_catalog
    if row.write_disposition:
        spec.setdefault("write_disposition", row.write_disposition)
    # Configure Migration is the source of truth for the landing namespace, so the
    # configured target_schema wins over whatever the generator defaulted to.
    plan = load_plan(session, project.project_code)
    if plan is not None:
        ts = (plan.extra or {}).get("target_schema")
        if ts:
            spec["target_schema"] = ts
    return spec


def _target_platform_instance_id(connection_id: Optional[int], platform: str) -> str:
    """A STABLE platform_instance_id for the target ref — the connection id when
    a real connection exists, else an intent placeholder keyed on the platform.
    Never ``str(None)`` (which would poison the plan's asset ref for a target
    intent with no connection yet, i.e. schema-only mode)."""
    if connection_id is not None:
        return str(connection_id)
    return f"intent:{(platform or 'unknown').strip().lower()}"


def configure(
    session: Session,
    project: Project,
    *,
    target_connection_id: Optional[int] = None,
    target_platform: str = "",
    landing_strategy: str = "raw",
    write_disposition: str = "replace",
    framework: str = "dlt",
    target_schema: str = "",
    target_catalog: str = "",
) -> MigrationPlanRow:
    """Create/refresh the project's MigrationPlan at ``draft`` and cache the
    denormalized config on the row. Idempotent — safe to re-run the configure stage.

    Target is expressed as an *intent* ``{platform, namespace, connection_id?}``:
    pass a registered ``target_connection_id`` (live) OR an explicit
    ``target_platform`` (schema-only, no connection yet). Exactly one is required.
    Execution readiness (D1) still demands a real connection — intent alone lets
    an offline project record where it *intends* to land without a live target."""
    if target_connection_id is None and not (target_platform or "").strip():
        raise ValueError("configure requires a target_connection_id or a target_platform intent")

    src_platform, _src_ref = resolve_source_ref(project, session)
    resolved_platform, _tgt_ref = resolve_target_ref(session, target_connection_id)
    # Connection wins when present; else fall back to the explicit intent platform.
    tgt_platform = resolved_platform or (target_platform or "").strip().lower()
    src_schema = _source_schema(project, session)
    tgt_schema = target_schema or _target_schema_default(project)

    source_ref = RelationalRelationRef(
        platform_instance_id=(_src_ref.get("connection_id") or "source"),
        asset_id=f"{src_schema}.*", schema=src_schema, relation="*",
        display_name=f"{project.project_code} source",
    )
    target_ref = RelationalRelationRef(
        platform_instance_id=_target_platform_instance_id(target_connection_id, tgt_platform),
        asset_id=f"{tgt_schema}.*", schema=tgt_schema, relation="*",
        display_name=f"{project.project_code} target ({tgt_platform or 'unset'})",
    )

    plan = load_plan(session, project.project_code) or MigrationPlan(
        plan_id=project.project_code,
        display_name=project.name or project.project_code,
        source_asset_ref=source_ref,
        target_asset_ref=target_ref,
    )
    plan.source_asset_ref = source_ref
    plan.target_asset_ref = target_ref
    plan.write_mode = {"replace": "full_replace", "append": "append"}.get(write_disposition, "full_replace")
    plan.rollback_target = source_ref
    plan.extra.update({
        "landing_strategy": landing_strategy,
        "framework": framework,
        "write_disposition": write_disposition,
        "target_schema": tgt_schema,
        "target_catalog": target_catalog,
        "source_platform": src_platform,
        "target_platform": tgt_platform,
    })

    row = get_row(session, project.project_code) or MigrationPlanRow(project_code=project.project_code)
    row.landing_strategy = landing_strategy
    row.target_connection_id = target_connection_id
    row.target_platform = tgt_platform
    row.write_disposition = write_disposition
    row.target_catalog = target_catalog
    row.framework = framework
    return _persist(session, project.project_code, plan, row)


def record_snapshot(session: Session, project: Project, *, ok: bool, by: str = "") -> Optional[MigrationPlan]:
    """Advance the plan around a snapshot/load run. draft→assessed→
    initial_snapshot_running→initial_snapshot_loaded on success; retryable_failed
    on failure."""
    plan = load_plan(session, project.project_code)
    if plan is None:
        return None
    set_status(plan, MigrationStatus.assessed, by=by, note="artifacts generated")
    set_status(plan, MigrationStatus.initial_snapshot_running, by=by)
    if ok:
        set_status(plan, MigrationStatus.initial_snapshot_loaded, by=by, note="load succeeded")
    else:
        set_status(plan, MigrationStatus.retryable_failed, by=by, note="load failed")
    row = get_row(session, project.project_code) or MigrationPlanRow(project_code=project.project_code)
    _persist(session, project.project_code, plan, row)
    return plan


def record_reconciliation(
    session: Session,
    project: Project,
    results: list[dict[str, Any]],
    *,
    by: str = "",
) -> Optional[MigrationPlan]:
    """Write reconciliation results onto the plan and advance to ``reconciled``
    when every rule passes (else leave it at initial_snapshot_loaded for a retry)."""
    plan = load_plan(session, project.project_code)
    if plan is None:
        return None
    rules: list[ReconciliationRule] = []
    all_pass = bool(results)
    for r in results:
        passed = r.get("result") == "pass"
        all_pass = all_pass and passed
        rules.append(ReconciliationRule(
            name=r.get("table", "unnamed"),
            kind=r.get("kind", "row_count"),
            description=f"source={r.get('source_row_count')} target={r.get('target_row_count')}",
            last_result="pass" if passed else "fail",
            last_checked_at=_now(),
        ))
    plan.reconciliation_rules = rules
    if all_pass:
        set_status(plan, MigrationStatus.reconciled, by=by, note="all reconciliation rules pass")
    row = get_row(session, project.project_code) or MigrationPlanRow(project_code=project.project_code)
    _persist(session, project.project_code, plan, row)
    # Part A: the target is now a verified fact — materialize it in the graph so
    # code-migration projects (and lineage) have a real node to anchor on.
    if all_pass:
        spec = _load_migration_spec(project.project_code)
        if spec is not None:
            apply_plan_target(session, project, spec)
            materialize_target_graph(session, project, spec)
    return plan


def status_payload(session: Session, project: Project) -> dict[str, Any]:
    """A compact status dict for the API / MCP / UI."""
    row = get_row(session, project.project_code)
    plan = load_plan(session, project.project_code)
    if row is None:
        return {"configured": False, "status": None, "landing_strategy": None}
    out: dict[str, Any] = {
        "configured": True,
        "status": row.status,
        "landing_strategy": row.landing_strategy,
        "target_connection_id": row.target_connection_id,
        "target_platform": row.target_platform,
        # True when a target PLATFORM intent is set but no live connection is bound
        # yet (schema-only) — the UI shows "intent only, not executable".
        "target_intent_only": row.target_connection_id is None and bool(row.target_platform),
        "target_catalog": row.target_catalog,
        "write_disposition": row.write_disposition,
        "framework": row.framework,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if plan is not None:
        out["reconciliation"] = [
            {"name": r.name, "kind": r.kind, "result": r.last_result}
            for r in plan.reconciliation_rules
        ]
        out["valid_next_statuses"] = sorted(s.value for s in valid_next_statuses(plan.status))
    return out


# ── target-graph materialization (Part A — the anchor code migration links to) ─
#
# A migration's target is otherwise stranded as strings in migration.json. Here we
# materialize it as first-class :Dataset:MigrationTarget nodes on an ISOLATED path
# (:Project)-[:HAS_MIGRATION_TARGET]->  — deliberately NOT under HAS_CATALOG, so the
# summary/review/quality queries that treat HAS_CATALOG datasets as *source* inputs
# never see targets. Written post-load (record_reconciliation / record_snapshot),
# when the target is a fact. Replace-set: a rerun drops obsolete targets + edges.
# Target column/type authority: introspected observations when available, else the
# source-mirror is marked verified=false (raw lift-and-shift is a 1:1 image).

def _neo4j(project: Project):
    return neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password,
        project.neo4j_database,
    )


def _target_ds_uri(code: str, schema: str, table: str) -> str:
    return f"dataset:{code}:target:{schema}.{table}"


_WIPE_TARGETS = """\
MATCH (:Project {projectCode: $code})-[:HAS_MIGRATION_TARGET]->(t:Dataset:MigrationTarget)
OPTIONAL MATCH (t)-[:HAS_COLUMN]->(c:Column)
DETACH DELETE t, c
"""

_MERGE_TARGET = """\
MATCH (prj:Project {projectCode: $code})
MERGE (t:Dataset:MigrationTarget {uri: $uri})
SET t.name = $table, t.schema = $schema, t.targetPlatform = $platform,
    t.migrationRole = 'target', t.verified = $verified
MERGE (prj)-[:HAS_MIGRATION_TARGET]->(t)
WITH t
OPTIONAL MATCH (src:Dataset {uri: $source_uri})
FOREACH (_ IN CASE WHEN src IS NULL THEN [] ELSE [1] END |
    MERGE (src)-[:MIGRATED_TO]->(t))
RETURN t.uri AS uri
"""

_MERGE_TARGET_COLUMN = """\
MATCH (t:Dataset:MigrationTarget {uri: $ds_uri})
MERGE (c:Column {uri: $col_uri})
SET c.name = $name, c.dataType = $dtype, c.verified = $verified
MERGE (t)-[:HAS_COLUMN]->(c)
"""

_SOURCE_COLUMNS = """\
MATCH (:Project {projectCode: $code})-[:HAS_CATALOG]->(:Catalog)-[:DCAT_DATASET]->(ds:Dataset)
WHERE ds.name = $table
OPTIONAL MATCH (ds)-[:HAS_COLUMN]->(col:Column)
RETURN ds.uri AS source_uri,
       collect({name: col.name, type: coalesce(col.dataType, col.data_type, col.type, '')}) AS columns
"""


def materialize_target_graph(
    session: Session, project: Project, spec: dict,
    observed: Optional[dict[str, list[dict]]] = None,
) -> dict[str, Any]:
    """Materialize the migration target as :Dataset:MigrationTarget nodes with
    :MIGRATED_TO lineage from source. Replace-set (idempotent; drops obsolete
    targets). ``observed`` maps target table → [{name,type}] from target-platform
    introspection (verified=true); when absent, columns mirror the source and are
    marked verified=false. Best-effort — never raises into the caller."""
    code = project.project_code
    target_schema = spec.get("target_schema", "") or _target_schema_default(project)
    platform = spec.get("target_platform", "") or ""
    written = 0
    try:
        with _neo4j(project) as ns:
            ns.run(_WIPE_TARGETS, code=code).consume()
            for ds in spec.get("datasets", []):
                s_schema = ds.get("source_schema", "")
                s_table = ds.get("source_table", "")
                t_table = ds.get("target_table", s_table)
                rec = ns.run(_SOURCE_COLUMNS, code=code, table=s_table).single()
                source_uri = (rec["source_uri"] if rec else "") or ""
                src_cols = [c for c in ((rec["columns"] if rec else []) or []) if c and c.get("name")]
                obs = (observed or {}).get(t_table)
                verified = obs is not None
                cols = obs if verified else [
                    {"name": c["name"], "type": c.get("type", "")} for c in src_cols
                ]
                t_uri = _target_ds_uri(code, target_schema, t_table)
                ns.run(_MERGE_TARGET, code=code, uri=t_uri, table=t_table,
                       schema=target_schema, platform=platform, verified=verified,
                       source_uri=source_uri).consume()
                for c in cols:
                    ns.run(_MERGE_TARGET_COLUMN, ds_uri=t_uri,
                           col_uri=f"column:{code}:target:{target_schema}.{t_table}.{c['name']}",
                           name=c["name"], dtype=c.get("type", ""), verified=verified).consume()
                written += 1
    except Exception:  # noqa: BLE001 — graph materialization must not fail the run
        return {"ok": False, "targets": written}
    return {"ok": True, "targets": written, "verified": observed is not None}


def _load_migration_spec(project_code: str) -> Optional[dict]:
    """Best-effort read of the migration.json (for post-load graph materialization)."""
    from . import serving_package
    candidates = [
        serving_package.package_dir(project_code, "migration") / "migration.json",
        migration_json_path(project_code),
        BASE_PROJECT_DIR / project_code / "migration.json",
    ]
    for p in candidates:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
    return None


def backfill_target_graph(session: Session, project: Project) -> dict[str, Any]:
    """Materialize target nodes for a dmig project that completed before Part A.
    Reads the persisted migration.json. Returns {ok, targets}."""
    spec = _load_migration_spec(project.project_code)
    if spec is None:
        return {"ok": False, "targets": 0, "reason": "no migration.json"}
    apply_plan_target(session, project, spec)
    return materialize_target_graph(session, project, spec)


# ── execution readiness (D1) ─────────────────────────────────────────────────

def _migration_package_present(project_code: str) -> bool:
    """True when a generated migration.json exists (the runnable package)."""
    candidates = [
        migration_dir(project_code) / "migration.json",
        migration_json_path(project_code),
        BASE_PROJECT_DIR / project_code / "migration.json",
    ]
    return any(p.exists() for p in candidates)


def migration_execution_readiness(project: Project, session: Session) -> dict[str, Any]:
    """Compute whether a migration project may actually EXECUTE (load/reconcile).

    Distinct from ``Project.data_connectivity_mode`` (which is workflow *intent*):
    execution needs BOTH ends live — a real source AND a real target — plus a
    generated package, with the mode permitting. Returns ``{ready, missing[]}``;
    the canonical backend gates (snapshot/reconcile cores, direct /complete for
    the execute stages, and the MCP execute tools) call this and return 409 with
    ``missing`` when not ready. Pure/read-only — no mutation, no subprocess.
    """
    missing: list[str] = []

    mode = (getattr(project, "data_connectivity_mode", "live") or "live").strip()
    if mode == "schema_only":
        missing.append(
            "project is in schema_only mode — no live source connectivity yet "
            "(flip to live once a source connection is available)"
        )

    # Source: a real SourceBinding (the single connection contract). The shared
    # resolver returns an unresolved empty ref when nothing is bound, so check the
    # binding directly.
    binding = session.exec(
        select(SourceBinding).where(SourceBinding.project_id == project.id)
    ).first()
    has_source = binding is not None
    if not has_source:
        missing.append("no source connection (bind a source connection first)")

    # Target: a configured plan with a resolvable target PlatformConnection.
    row = get_row(session, project.project_code)
    tgt_platform = ""
    if row is not None and row.target_connection_id:
        tgt_platform, _ = resolve_target_ref(session, row.target_connection_id)
    if not tgt_platform:
        missing.append("no target connection configured (Configure Migration)")

    if not _migration_package_present(project.project_code):
        missing.append("no generated migration package (run Generate Migration Pipeline)")

    return {"ready": not missing, "missing": missing}
