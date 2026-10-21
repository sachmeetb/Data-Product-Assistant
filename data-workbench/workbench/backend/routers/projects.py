import json
import shutil
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..archetypes import (
    ARCHETYPE_REGISTRY,
    EXCLUSIVE_GROUP_DEPENDENTS,
    STAGE_REGISTRY,
    enrich_workflow,
    get_default_workflow,
    get_workflow_catalog,
    get_workflow_templates,
    get_workflow_template_by_id,
    stage_is_agentic,
    validate_workflow,
)
from ..config import BASE_PROJECT_DIR
from ..database import get_session
from ..models import (
    AppSettings,
    ChatMessage,
    ChatSession,
    DQTestRun,
    ProductChatMessage,
    ProductChatSession,
    ProductRequest,
    Project,
    StageExecution,
    StageRun,
    StageStatus,
    Workflow,
)
from ..pipeline import STAGES

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    name: str
    # The source is bound after creation via the data-source picker
    # (set_data_source → PlatformConnection + SourceBinding), not at create time.
    archetype: str = "dd"
    domain: Optional[str] = None
    # Free-form idea text from the SA wizard. Persisted on Project so the
    # engineer sees it as a banner without round-tripping through the
    # ProductRequest notes blob.
    product_idea: Optional[str] = None
    # PO email — flows into synthesized ODCS owners[] for SA products so
    # the product surfaces in the PO's My Products list. Consumer wizard
    # authors owners directly in the spec, so it doesn't need this hint.
    owner_email: Optional[str] = None
    # PO display name — paired with owner_email; sa_pipeline writes it onto
    # :DataContractOwner.name so the marketplace owner chip renders the
    # real name instead of the email prefix.
    owner_name: Optional[str] = None
    workflow: Optional[list[dict]] = None
    workflow_ids: Optional[list[str]] = None  # which workflow templates to include (default: all)
    # live | offline — `offline` (dpe-sa) swaps live discovery+profiling for the
    # deterministic data_discovery_offline stage (client uploads a metadata manifest).
    data_connectivity_mode: str = "live"
    # Empty/zero defaults so create_project falls back to AppSettings (which is
    # env-overridable via WB_NEO4J_*). Hardcoding "localhost" here pinned new
    # projects to localhost even in a container that reaches Neo4j by service
    # name (the `body.neo4j_host or gs.neo4j_host` fallback never engaged).
    neo4j_host: str = ""
    neo4j_port: int = 0
    neo4j_user: str = ""
    neo4j_password: str = ""
    neo4j_database: str = ""
    role_assignments: Optional[dict] = None


class ProjectResponse(BaseModel):
    id: int
    project_code: str
    name: str
    archetype: str
    domain: Optional[str] = None
    product_idea: Optional[str] = None
    parent_intake_submission_id: Optional[int] = None
    data_connectivity_mode: str = "live"
    discovery_complete_at: Optional[str] = None
    neo4j_host: str
    neo4j_port: int
    neo4j_user: str
    neo4j_password: str
    neo4j_database: str
    multi_workflow: bool = False
    current_stage: int
    role_assignments: str
    created_at: str
    stages: list[dict] = []
    workflows: list[dict] = []


def generate_project_code(session: Session, prefix: str = "dd") -> str:
    today = date.today()
    date_str = today.strftime("%m%d%Y")
    code_prefix = f"{prefix}-{date_str}-"
    statement = select(Project).where(Project.project_code.startswith(code_prefix))
    existing = session.exec(statement).all()

    # Track the max sequence ever used, not the count — ensures deleted IDs are
    # never re-issued (which would surface stale Neo4j/filesystem state in a
    # newly created project).
    used_seqs: set[int] = set()
    for p in existing:
        try:
            used_seqs.add(int(p.project_code.rsplit("-", 1)[-1]))
        except (ValueError, IndexError):
            continue

    next_seq = max(used_seqs, default=0) + 1
    # Also skip any sequence whose directory still exists on disk — guards
    # against lingering artifacts from a prior incomplete deletion.
    while (BASE_PROJECT_DIR / f"{code_prefix}{next_seq:02d}").exists():
        next_seq += 1
    return f"{code_prefix}{next_seq:02d}"


@router.post("", response_model=ProjectResponse)
def create_project(body: ProjectCreate, session: Session = Depends(get_session)):
    # Validate archetype
    archetype_def = ARCHETYPE_REGISTRY.get(body.archetype)
    if not archetype_def:
        raise HTTPException(400, f"Unknown archetype: {body.archetype}")
    if not archetype_def["implemented"]:
        raise HTTPException(400, f"Archetype '{archetype_def['name']}' is not yet implemented.")

    project_code = generate_project_code(session, archetype_def["prefix"])
    project_dir = BASE_PROJECT_DIR / project_code
    project_dir.mkdir(parents=True, exist_ok=True)

    role_json = json.dumps(body.role_assignments or {})

    # Use global Neo4j settings as defaults
    global_settings = session.exec(select(AppSettings)).first()
    gs = global_settings or AppSettings()

    # Get workflow templates for multi-workflow project
    templates = get_workflow_templates(body.archetype)
    # Offline dpe-sa (Phase 2 of Offline Extraction): a client that can't grant a
    # live connection uploads a metadata manifest instead. Swap the live discovery+
    # profiling groups for the single deterministic data_discovery_offline stage;
    # everything after discovery is unchanged.
    conn_mode = (body.data_connectivity_mode or "live").strip().lower()
    if body.archetype == "dpe-sa" and conn_mode == "offline":
        import copy as _copy
        from ..archetypes import DPE_SA_OFFLINE_WORKFLOW_TEMPLATES
        templates = _copy.deepcopy(DPE_SA_OFFLINE_WORKFLOW_TEMPLATES)
    # Filter by selected workflow_ids if provided. A requested id that isn't a
    # default template (e.g. migration_schema_only) is pulled from the full
    # catalog so a non-default pipeline can be selected AT creation — this is how
    # a schema-only migration project is scaffolded.
    if body.workflow_ids is not None and templates:
        default_ids = {t["workflow_id"] for t in templates}
        selected = [t for t in templates if t["workflow_id"] in body.workflow_ids]
        for wid in body.workflow_ids:
            if wid not in default_ids:
                extra = get_workflow_template_by_id(wid)
                if extra is not None:
                    # Catalog groups carry no top-level `order` (only defaults do,
                    # via _with_order); assign one so the Workflow row is valid.
                    extra.setdefault("order", len(selected) + 1)
                    selected.append(extra)
        templates = selected

    # Build a flat workflow for backward compatibility (workflow_json on Project)
    flat_workflow = body.workflow if body.workflow else get_default_workflow(body.archetype)
    errors = validate_workflow(flat_workflow)
    if errors:
        raise HTTPException(400, {"message": "Workflow validation failed", "errors": errors})

    workflow_str = json.dumps(flat_workflow, indent=2)
    (project_dir / "workflow.json").write_text(workflow_str)

    project = Project(
        project_code=project_code,
        name=body.name,
        archetype=body.archetype,
        domain=body.domain,
        product_idea=(body.product_idea or "").strip() or None,
        owner_email=(body.owner_email or "").strip() or None,
        owner_name=(body.owner_name or "").strip() or None,
        workflow_json=workflow_str,
        multi_workflow=bool(templates),
        data_connectivity_mode=conn_mode,
        neo4j_host=body.neo4j_host or gs.neo4j_host,
        neo4j_port=body.neo4j_port or gs.neo4j_port,
        neo4j_user=body.neo4j_user or gs.neo4j_user,
        neo4j_password=body.neo4j_password or gs.neo4j_password,
        neo4j_database=body.neo4j_database or gs.neo4j_database,
        role_assignments=role_json,
    )
    session.add(project)
    session.commit()
    session.refresh(project)

    # SQLite recycles integer primary keys after row deletion. If this
    # project.id was previously used by a project whose cascade cleanup
    # was bypassed (backend killed mid-delete, pre-cascade code paths,
    # direct DB edits), stale child rows still point at it. Purge them
    # now, before scaffolding the real StageRun/Workflow rows below.
    for row in session.exec(
        select(StageExecution).where(StageExecution.project_id == project.id)
    ).all():
        session.delete(row)
    for row in session.exec(
        select(DQTestRun).where(DQTestRun.project_id == project.id)
    ).all():
        session.delete(row)
    for row in session.exec(
        select(StageRun).where(StageRun.project_id == project.id)
    ).all():
        session.delete(row)
    for row in session.exec(
        select(Workflow).where(Workflow.project_id == project.id)
    ).all():
        session.delete(row)
    session.commit()

    # Create :Project node in Neo4j for graph-level scoping
    try:
        from ..graph_ops import ensure_project_node
        ensure_project_node(project)
    except Exception:
        pass  # Neo4j may not be reachable at creation time; linked later

    if templates:
        # ── Multi-workflow: create Workflow rows and StageRuns per workflow ──
        for tmpl in templates:
            stages_json = json.dumps(tmpl["stages"], indent=2)
            wf = Workflow(
                project_id=project.id,
                workflow_id=tmpl["workflow_id"],
                name=tmpl["name"],
                description=tmpl.get("description"),
                workflow_json=stages_json,
                order=tmpl["order"],
                repeatable=tmpl.get("repeatable", False),
            )
            session.add(wf)
            session.flush()

            enabled_stages = [s for s in tmpl["stages"] if s.get("enabled", True)]
            for i, step in enumerate(enabled_stages, 1):
                stage_def = STAGE_REGISTRY.get(step["stage_id"], {})
                stage_run = StageRun(
                    project_id=project.id,
                    workflow_id=tmpl["workflow_id"],
                    stage_number=i,
                    stage_name=stage_def.get("name", step["stage_id"]),
                    status=StageStatus.complete if step["stage_id"] == "initiate" else StageStatus.pending,
                )
                session.add(stage_run)
    else:
        # ── Legacy single-workflow: StageRuns without workflow_id ──
        enabled_workflow = [s for s in flat_workflow if s.get("enabled", True)]
        for i, step in enumerate(enabled_workflow, 1):
            stage_def = STAGE_REGISTRY.get(step["stage_id"], {})
            stage_run = StageRun(
                project_id=project.id,
                stage_number=i,
                stage_name=stage_def.get("name", step["stage_id"]),
                status=StageStatus.complete if step["stage_id"] == "initiate" else StageStatus.pending,
            )
            session.add(stage_run)

    session.commit()
    return _project_response(project, session)


@router.get("", response_model=list[ProjectResponse])
def list_projects(session: Session = Depends(get_session)):
    projects = session.exec(select(Project).order_by(Project.created_at.desc())).all()
    return [_project_response(p, session) for p in projects]


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(project_id: int, session: Session = Depends(get_session)):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return _project_response(project, session)


@router.get("/by-code/{project_code}", response_model=ProjectResponse)
def get_project_by_code(project_code: str, session: Session = Depends(get_session)):
    """Resolve a project by its directory-name code (e.g. `dpe-sa-05112026-01`).

    The CLI side of the workbench learns project_code from `cwd` — the int id
    isn't visible from inside the project directory. This endpoint is the
    other-side entry point for the same ProjectResponse the by-id route returns.
    """
    project = session.exec(
        select(Project).where(Project.project_code == project_code)
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_code}")
    return _project_response(project, session)


# Reads the deployed view names off the project's own :ServingDefinition so we
# can drop exactly this project's views — never a blanket `vw_%` sweep, which
# would nuke views co-deployed by other products on the same (possibly shared or
# borrowed) Postgres.
_FETCH_DEPLOYED_VIEWS = """
MATCH (dc:DataContract {id: $contract_id})
WHERE coalesce(dc.isCurrent, true) = true
MATCH (dc)-[:MATERIALISES_AS]->(:DProdDataProduct)-[:SERVED_BY]->(sd:ServingDefinition {servingMode: 'virtual_view'})
RETURN sd.deployedViewNames AS deployed_json,
       sd.deployedTo AS deployed_schema,
       sd.viewNames AS view_names_json,
       sd.viewName AS primary_view_name,
       sd.viewSchema AS view_schema
"""


def _drop_deployed_views(project: "Project", session: Session) -> None:
    """Drop this project's deployed views from its source Postgres.

    Must run BEFORE ``delete_project_node`` — the graph delete removes the
    :ServingDefinition that records the view names. Precise per-project drop
    (enumerated from ``deployedViewNames``), CASCADE because the project is
    going away. Best-effort: a missing/unreachable DB must never wedge the
    delete, so every failure is swallowed. For consumer products the DSN is
    *borrowed* from a source; we only drop this project's own view names, so
    the borrowed source's views are untouched.
    """
    try:
        from ..neo4j_client import neo4j_session
        from ..pg_resolver import resolve_read_connection_for_consumer
        from .connections import build_connection_string

        contract_id = f"{project.project_code}-contract"
        _platform, _conn_ref, _borrowed = resolve_read_connection_for_consumer(
            project, session, contract_id
        )
        # Postgres-only cleanup (the drop below is a psycopg2 DROP VIEW). A
        # non-Postgres served product's view cleanup is a follow-up.
        if _platform not in ("postgres", "postgresql"):
            return
        dsn = build_connection_string("postgres", _conn_ref)
        if not dsn:
            return
        with neo4j_session(
            project.neo4j_host, project.neo4j_port, project.neo4j_user,
            project.neo4j_password, project.neo4j_database,
        ) as ns:
            rows = list(ns.run(_FETCH_DEPLOYED_VIEWS, contract_id=contract_id))
        if not rows:
            return
        row = dict(rows[0])
        schema = (row.get("deployed_schema") or row.get("view_schema") or "public").strip() or "public"
        raw = row.get("deployed_json") or row.get("view_names_json")
        names: list[str] = []
        if isinstance(raw, list):
            names = [str(x) for x in raw if x]
        elif raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    names = [str(x) for x in parsed if x]
            except (TypeError, ValueError):
                names = []
        if not names and row.get("primary_view_name"):
            names = [str(row["primary_view_name"])]
        if not names:
            return

        import psycopg2

        conn = None
        try:
            conn = psycopg2.connect(dsn)
            conn.autocommit = True
            with conn.cursor() as cur:
                for raw_name in names:
                    bare = raw_name.rsplit(".", 1)[-1].strip('"')
                    if not bare:
                        continue
                    cur.execute(f'DROP VIEW IF EXISTS "{schema}"."{bare}" CASCADE')
        finally:
            if conn is not None:
                conn.close()
    except Exception:
        pass


@router.delete("/{project_id}")
def delete_project(
    project_id: int,
    owner_email: str,
    force: bool = False,
    session: Session = Depends(get_session),
):
    """Owner-initiated teardown for a data product project.

    Requires the caller to identify themselves via ``owner_email`` and to
    appear either in ``Project.owner_email`` or as a ``:DataContractOwner``
    on the project's contract. When the project's :DProdDataProduct is
    consumed by other projects, the first call returns 409 with the
    consumer list; the caller can retry with ``?force=true`` to override.
    """
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # ── Owner authorization ───────────────────────────────────────────
    caller = (owner_email or "").strip().lower()
    if not caller:
        raise HTTPException(status_code=403, detail="owner_email is required")
    try:
        from ..graph_ops import project_owner_emails
        owners = project_owner_emails(project)
    except Exception:
        owners = set()
    project_email = (project.owner_email or "").strip().lower()
    if project_email:
        owners.add(project_email)
    if caller not in owners:
        raise HTTPException(
            status_code=403,
            detail="Only a listed owner of this product may delete it.",
        )

    # ── Cross-project consumer check (skipped on force=true) ──────────
    if not force:
        try:
            from ..graph_ops import check_consumers
            consumers = check_consumers(project)
        except Exception:
            consumers = []
        if consumers:
            # FastAPI wraps `detail` under a top-level "detail" key, so the
            # JSON body is {"detail": {"reason": ..., "consumers": [...]}}.
            raise HTTPException(
                status_code=409,
                detail={"reason": "consumers_exist", "consumers": consumers},
            )

        # A dmig project is the source→target anchor for any code-migration
        # projects linked to it (the :USES_DATASET edge). Refuse to orphan them.
        if getattr(project, "archetype", "") == "dmig":
            try:
                from ..models import CodeMigrationPlanRow
                linked = session.exec(
                    select(CodeMigrationPlanRow).where(
                        CodeMigrationPlanRow.linked_dmig_project_code == project.project_code)
                ).all()
            except Exception:
                linked = []
            if linked:
                raise HTTPException(
                    status_code=409,
                    detail={"reason": "code_migrations_linked",
                            "code_migrations": [r.project_code for r in linked]},
                )

    # ── Cancel any running SDK tasks for this project ─────────────────
    # _active_tasks is keyed by run_id; StageExecution.run_id is the bridge
    # back to project_id. Pop+cancel each so the SDK loop releases its
    # resources before we tear down the rows it would write to.
    try:
        from .websocket import _active_tasks
        running_run_ids = session.exec(
            select(StageExecution.run_id).where(
                StageExecution.project_id == project_id,
                StageExecution.status == "running",
            )
        ).all()
        for run_id in running_run_ids:
            task = _active_tasks.pop(run_id, None)
            if task is not None and not task.done():
                task.cancel()
    except Exception:
        pass

    # ── Drop deployed Postgres views BEFORE the graph delete ──────────
    # delete_project_node removes the :ServingDefinition that records the
    # view names, so the views must be dropped while that record still
    # exists. Precise per-project drop; best-effort.
    _drop_deployed_views(project, session)

    # ── Remove :Project node + scoped subgraph from Neo4j ─────────────
    try:
        from ..graph_ops import delete_project_node
        delete_project_node(project)
    except Exception:
        pass

    # ── Cascade cleanup of project-scoped SQLite rows ────────────────
    # Order matters: children before parents. StageExecution rows hold
    # streamed event logs; DQTestRun rows hold per-framework summaries;
    # Chat/ProductChat sessions hold message history; ProductRequest
    # rows hold the wizard's submission record. Without each of these a
    # recycled project_id would surface stale history.

    # Chat messages → sessions (engineer chat).
    chat_session_ids = session.exec(
        select(ChatSession.id).where(ChatSession.project_id == project_id)
    ).all()
    if chat_session_ids:
        for row in session.exec(
            select(ChatMessage).where(ChatMessage.session_id.in_(chat_session_ids))
        ).all():
            session.delete(row)
    for row in session.exec(
        select(ChatSession).where(ChatSession.project_id == project_id)
    ).all():
        session.delete(row)

    # ProductChat sessions are owner-scoped, but rows tied to this project
    # via the optional ``project_id`` field belong to this project's
    # wizard run; delete them and their messages.
    pchat_session_ids = session.exec(
        select(ProductChatSession.id).where(
            ProductChatSession.project_id == project_id
        )
    ).all()
    if pchat_session_ids:
        for row in session.exec(
            select(ProductChatMessage).where(
                ProductChatMessage.session_id.in_(pchat_session_ids)
            )
        ).all():
            session.delete(row)
    for row in session.exec(
        select(ProductChatSession).where(
            ProductChatSession.project_id == project_id
        )
    ).all():
        session.delete(row)

    # Handoff queue entries (new / edit / ingest / pushback) keyed to
    # this project.
    for row in session.exec(
        select(ProductRequest).where(ProductRequest.project_id == project_id)
    ).all():
        session.delete(row)

    # Pipeline state.
    for row in session.exec(
        select(StageExecution).where(StageExecution.project_id == project_id)
    ).all():
        session.delete(row)

    for row in session.exec(
        select(DQTestRun).where(DQTestRun.project_id == project_id)
    ).all():
        session.delete(row)

    for s in session.exec(select(StageRun).where(StageRun.project_id == project_id)).all():
        session.delete(s)

    for w in session.exec(select(Workflow).where(Workflow.project_id == project_id)).all():
        session.delete(w)

    project_dir = BASE_PROJECT_DIR / project.project_code
    if project_dir.exists():
        shutil.rmtree(project_dir)

    session.delete(project)
    session.commit()
    return {"status": "deleted", "project_code": project.project_code}


class DataSourceInput(BaseModel):
    platform: str = "postgres"
    host: str
    port: int = 5432
    database: str
    username: str
    password: str
    schema_name: Optional[str] = None  # Avoids shadowing pydantic.BaseModel.schema


@router.get("/{project_id}/data-source")
def get_data_source(project_id: int, session: Session = Depends(get_session)):
    """Return the project's source connection as form fields for the
    DataSourceDialog's edit-after-completion flow.

    Reads the structured ``SourceBinding`` → ``PlatformConnection`` (the single
    connection contract). Returns {} when no source is bound yet. Password is
    never returned — the form treats it as write-only; users re-enter it when
    editing an existing connection.
    """
    from ..models import PlatformConnection, SourceBinding

    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    binding = session.exec(
        select(SourceBinding).where(SourceBinding.project_id == project_id)
    ).first()
    if binding is None:
        return {}
    conn = session.get(PlatformConnection, binding.connection_id)
    if conn is None:
        return {}
    return {
        "platform": (conn.platform_type or "postgres").replace("postgresql", "postgres"),
        "host": conn.host or "",
        "port": conn.port or 5432,
        "database": conn.database or "",
        "username": conn.username or "",
        "password": "",
        "schema_name": binding.default_schema or None,
    }


@router.put("/{project_id}/data-source")
def set_data_source(project_id: int, body: DataSourceInput, session: Session = Depends(get_session)):
    """Register the selected data source as a structured
    ``PlatformConnection`` + ``SourceBinding`` — the SAME path every platform
    uses (there is no privileged Postgres trunk). The simple host/port/db/user/
    pw form is parsed ONCE here into a structured connection; a DSN never
    becomes an app-level shape. Downstream discovery / profiling / serving
    stages resolve the source via the binding.
    """
    from ..models import PlatformConnection, SourceBinding
    from ..platform.dispatch import canonical_platform
    from ..platform.registry import get_registry
    from ..request_guard import guard_rest_mutation
    from .connections import _normalize_stored_secret
    from datetime import datetime

    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    guard_rest_mutation(project, session)

    platform = canonical_platform(body.platform) or "postgres"
    if get_registry().get_manifest(platform) is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown platform '{body.platform}'. "
                   f"Known: {get_registry().platform_ids()}",
        )

    host = body.host.strip()
    database = body.database.strip()
    # Store the credential contained (never a plaintext echo). A blank password
    # on an edit means "leave the existing secret unchanged".
    new_secret = _normalize_stored_secret(None, body.password or None)

    # Reuse the connection already bound to this project (edit-after-completion),
    # else the deterministically-named one, else create a fresh row.
    binding = session.exec(
        select(SourceBinding).where(SourceBinding.project_id == project_id)
    ).first()
    conn: Optional[PlatformConnection] = None
    if binding is not None:
        conn = session.get(PlatformConnection, binding.connection_id)
    conn_name = f"{project.project_code}-source"
    if conn is None:
        conn = session.exec(
            select(PlatformConnection).where(
                PlatformConnection.connection_name == conn_name
            )
        ).first()

    if conn is None:
        conn = PlatformConnection(
            connection_name=conn_name,
            platform_type=platform,
            host=host,
            port=body.port,
            database=database,
            username=body.username,
            secret_ref=new_secret or "",
            connection_roles_json=json.dumps(["source"]),
        )
        session.add(conn)
        session.commit()
        session.refresh(conn)
    else:
        conn.platform_type = platform
        conn.host = host
        conn.port = body.port
        conn.database = database
        conn.username = body.username
        if new_secret is not None:
            conn.secret_ref = new_secret
        conn.updated_at = datetime.utcnow()
        session.add(conn)
        session.commit()
        session.refresh(conn)

    if binding is None:
        binding = SourceBinding(
            project_id=project_id,
            connection_id=conn.id,
            default_schema=(body.schema_name or ""),
        )
    else:
        binding.connection_id = conn.id
        binding.default_schema = (body.schema_name or "")
        binding.updated_at = datetime.utcnow()
    session.add(binding)
    session.commit()
    return {
        "status": "saved",
        "platform": platform,
        "host": host,
        "port": body.port,
        "database": database,
        "schema": body.schema_name,
        "connection_id": conn.id,
    }


# ── CLI bootstrap ──────────────────────────────────────────────────────────
# Materializes a project-local CLAUDE.md + .claude/commands/ so an engineer
# can `cd projects/{code}/ && claude --add-dir .` and drive the same backend
# the web UI uses. The web UI is unchanged; CLAUDE.md just describes the
# existing endpoints to the engineer's AI session.

# Mirrors ROLE_STAGE_PERMISSIONS_BY_ID["Data Engineer"] in
# workbench/frontend/src/types.ts:173-189. Duplicated rather than imported
# across the TS/Py boundary; when you add an engineer stage, edit both.
_ENGINEER_STAGE_IDS = {
    "odcs_to_dprod", "select_data_source",
    "data_discovery", "data_discovery_composite",
    "load_schema",
    "data_profiling", "data_profiling_composite", "load_profiles",
    "metadata_enrichment",
    "data_mapping",
    "serving_virtual_view", "serving_physical_copy",
    "mark_engineering_complete",
    "column_name_standardization",
    "mark_discovery_complete",
    "synthesize_odcs_from_graph",
    "auto_mapping_sa",
}


@router.post("/{project_id}/cli-bootstrap")
def cli_bootstrap(project_id: int, session: Session = Depends(get_session)):
    """Render the project-local CLAUDE.md + slash-command files.

    Idempotent — overwrites whatever was there. The engineer triggers this
    explicitly (via `/refresh-context` from inside their CLI session, or by
    POSTing this endpoint once at setup time) so they pull a fresh snapshot
    on demand. We do NOT auto-rewrite on every stage transition.
    """
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Load templates from the bundled directory.
    # __file__ is workbench/backend/routers/projects.py → templates lives at
    # workbench/backend/templates.
    from pathlib import Path
    templates_dir = Path(__file__).resolve().parent.parent / "templates"
    if not templates_dir.exists():
        raise HTTPException(500, f"templates dir missing: {templates_dir}")
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(enabled_extensions=()),
        keep_trailing_newline=True,
    )

    archetype_def = ARCHETYPE_REGISTRY.get(project.archetype, {})
    archetype_name = archetype_def.get("name", project.archetype)

    # Engineer-runnable stages with their metadata (filtered to Data Engineer
    # role). Anything not in this set is gated to another role and will be
    # disabled in the UI's Run button too.
    engineer_stages: list[dict] = []
    seen_stage_ids: set[str] = set()
    for stage_id in _ENGINEER_STAGE_IDS:
        reg = STAGE_REGISTRY.get(stage_id)
        if not reg:
            continue
        if stage_id in seen_stage_ids:
            continue
        seen_stage_ids.add(stage_id)
        engineer_stages.append({
            "stage_id": stage_id,
            "name": reg.get("name", stage_id),
            "skill": reg.get("skill"),
            "has_review": reg.get("has_review", False),
            "review_type": reg.get("review_type"),
        })
    engineer_stages.sort(key=lambda s: s["name"])

    # Live pipeline snapshot — reuse the same shape /api/projects/{id} returns
    # so the rendered status table matches what the UI sees.
    resp = _project_response(project, session)
    workflows = resp.get("workflows") or []
    # Legacy single-workflow projects: synthesize a single pseudo-workflow
    # entry from the flat stages list so the template renders something.
    if not workflows and resp.get("stages"):
        workflows = [{
            "workflow_id": "default",
            "name": "Default workflow",
            "stages": resp["stages"],
        }]
    # Coerce enum statuses to their .value strings so the Jinja template
    # renders 'complete' instead of 'StageStatus.complete'. _project_response
    # returns the SQLModel enum directly (FastAPI's response serialization
    # would handle this, but we're calling it as a plain Python function).
    for wf in workflows:
        for s in wf.get("stages", []):
            status = s.get("status")
            if hasattr(status, "value"):
                s["status"] = status.value

    # Service URLs. Hardcoded for the single-tenant dev workbench — same
    # convention as CORS in main.py (localhost:8000 backend, localhost:5173
    # frontend). If we ever break this out for a hosted environment, route
    # both through AppSettings.
    backend_base_url = "http://localhost:8000"
    frontend_base_url = "http://localhost:5173"

    # The agent_ask helper script the SDK runner invokes for human-in-the-loop
    # questions. Same path the WebSocket session injects via sdk_runner.py.
    from ..config import BASE_DIR
    agent_ask_script = str(BASE_DIR / "scripts" / "agent_ask.py")

    context = {
        "project": project,
        "archetype_name": archetype_name,
        "engineer_stages": engineer_stages,
        "workflows": workflows,
        "backend_base_url": backend_base_url,
        "frontend_base_url": frontend_base_url,
        "agent_ask_script": agent_ask_script,
    }

    # Render the project-level CLAUDE.md
    claude_md = env.get_template("project_claude_md.j2").render(**context)

    project_dir = BASE_PROJECT_DIR / project.project_code
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "CLAUDE.md").write_text(claude_md)

    # Render each slash command into projects/{code}/.claude/commands/*.md.
    # The `.claude/commands/` directory under the cwd is the canonical slot
    # Claude Code looks in for project-scoped slash commands.
    commands_dir = project_dir / ".claude" / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for tmpl_path in sorted((templates_dir / "commands").glob("*.md.j2")):
        # `pipeline-status.md.j2` → command name `pipeline-status`, output
        # `pipeline-status.md`. The leading slash is implicit in Claude Code.
        cmd_name = tmpl_path.name[:-len(".md.j2")]
        rendered = env.get_template(f"commands/{tmpl_path.name}").render(**context)
        (commands_dir / f"{cmd_name}.md").write_text(rendered)
        written.append(cmd_name)

    return {
        "status": "ok",
        "project_code": project.project_code,
        "project_dir": str(project_dir),
        "claude_md": str(project_dir / "CLAUDE.md"),
        "commands": written,
        "commands_dir": str(commands_dir),
    }


@router.post("/{project_id}/migrate-graph")
def migrate_project_graph(project_id: int, session: Session = Depends(get_session)):
    """Create :Project node and link existing Catalog/Contract nodes for a legacy project."""
    from ..graph_ops import (
        ensure_project_node,
        invalidate_cache,
        link_catalogs_to_project,
        link_contract_to_project,
    )

    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    ensure_project_node(project)
    catalogs_linked = link_catalogs_to_project(project)

    contract_id = f"{project.project_code}-contract"
    contract_linked = False
    try:
        link_contract_to_project(project, contract_id)
        contract_linked = True
    except Exception:
        pass

    invalidate_cache(project.project_code)

    return {
        "project_code": project.project_code,
        "catalogs_linked": catalogs_linked,
        "contract_linked": contract_linked,
    }


@router.get("/{project_id}/workflows")
def list_workflows(project_id: int, session: Session = Depends(get_session)):
    """List all workflows for a multi-workflow project."""
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not project.multi_workflow:
        raise HTTPException(400, "This project uses a single-workflow model.")

    workflows = session.exec(
        select(Workflow)
        .where(Workflow.project_id == project_id)
        .order_by(Workflow.order)
    ).all()

    result = []
    for wf in workflows:
        wf_stages = session.exec(
            select(StageRun)
            .where(StageRun.project_id == project_id, StageRun.workflow_id == wf.workflow_id)
            .order_by(StageRun.stage_number)
        ).all()
        stages_data = json.loads(wf.workflow_json) if wf.workflow_json else []
        enabled_stages = [s for s in stages_data if s.get("enabled", True)]
        stage_by_order = {i + 1: step for i, step in enumerate(enabled_stages)}

        result.append({
            "workflow_id": wf.workflow_id,
            "name": wf.name,
            "description": wf.description,
            "order": wf.order,
            "repeatable": wf.repeatable,
            "stages": [
                _stage_run_dict(sr, stage_by_order.get(sr.stage_number))
                for sr in wf_stages
            ],
        })
    return result


@router.get("/{project_id}/workflows/{workflow_id}")
def get_workflow(project_id: int, workflow_id: str, session: Session = Depends(get_session)):
    """Get a single workflow with its stage statuses."""
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    wf = session.exec(
        select(Workflow)
        .where(Workflow.project_id == project_id, Workflow.workflow_id == workflow_id)
    ).first()
    if not wf:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")

    wf_stages = session.exec(
        select(StageRun)
        .where(StageRun.project_id == project_id, StageRun.workflow_id == workflow_id)
        .order_by(StageRun.stage_number)
    ).all()
    stages_data = json.loads(wf.workflow_json) if wf.workflow_json else []
    enabled_stages = [s for s in stages_data if s.get("enabled", True)]
    stage_by_order = {i + 1: step for i, step in enumerate(enabled_stages)}

    return {
        "workflow_id": wf.workflow_id,
        "name": wf.name,
        "description": wf.description,
        "order": wf.order,
        "repeatable": wf.repeatable,
        "stages": [
            _stage_run_dict(sr, stage_by_order.get(sr.stage_number))
            for sr in wf_stages
        ],
    }


class AddWorkflowRequest(BaseModel):
    workflow_id: str


def _create_workflow_and_stages(project, tmpl: dict, order: int, session: Session) -> None:
    """Create one Workflow row + StageRun records for its enabled stages.

    Does NOT commit — caller owns the transaction boundary.
    """
    stages_json = json.dumps(tmpl["stages"], indent=2)
    wf = Workflow(
        project_id=project.id,
        workflow_id=tmpl["workflow_id"],
        name=tmpl["name"],
        description=tmpl.get("description"),
        workflow_json=stages_json,
        order=order,
        repeatable=tmpl.get("repeatable", False),
    )
    session.add(wf)
    session.flush()

    enabled_stages = [s for s in tmpl["stages"] if s.get("enabled", True)]
    for i, step in enumerate(enabled_stages, 1):
        stage_def = STAGE_REGISTRY.get(step["stage_id"], {})
        stage_run = StageRun(
            project_id=project.id,
            workflow_id=tmpl["workflow_id"],
            stage_number=i,
            stage_name=stage_def.get("name", step["stage_id"]),
            status=StageStatus.complete if step["stage_id"] == "initiate" else StageStatus.pending,
        )
        session.add(stage_run)


@router.post("/{project_id}/workflows")
def add_workflow(project_id: int, body: AddWorkflowRequest, session: Session = Depends(get_session)):
    """Add a workflow from the catalog to an existing project.

    When ``workflow_id`` is ``"dq_testing"``, the ``baseline_dq_rules`` workflow is
    automatically added first (if absent) so the engineer gets a complete, runnable
    DQ add-on in a single call.
    """
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not project.multi_workflow:
        raise HTTPException(400, "This project uses a single-workflow model.")

    # Check the workflow_id isn't already in this project
    existing = session.exec(
        select(Workflow).where(
            Workflow.project_id == project_id, Workflow.workflow_id == body.workflow_id,
        )
    ).first()
    if existing:
        raise HTTPException(400, f"Workflow '{body.workflow_id}' already exists in this project.")

    # Find the template in the archetype-scoped catalog — so a workflow hidden
    # for this archetype (e.g. catalog-side DQ on a consumer product, or
    # product_dq_testing on a dataset project) can't be added out of band.
    catalog = get_workflow_catalog(project.archetype)
    tmpl = next((t for t in catalog if t["workflow_id"] == body.workflow_id), None)
    if not tmpl:
        raise HTTPException(400, f"Unknown workflow template: {body.workflow_id}")

    # Determine next order number
    max_order = session.exec(
        select(Workflow.order)
        .where(Workflow.project_id == project_id)
        .order_by(Workflow.order.desc())
    ).first()
    next_order = (max_order or 0) + 1

    added = []

    # dq_testing depends on dq_rule_generation (from baseline_dq_rules). Auto-add
    # baseline_dq_rules first when it isn't already present so a single add_workflow
    # call bootstraps the complete DQ add-on without the engineer needing to know
    # the dependency ordering.
    if body.workflow_id == "dq_testing":
        has_rules = session.exec(
            select(Workflow).where(
                Workflow.project_id == project_id,
                Workflow.workflow_id == "baseline_dq_rules",
            )
        ).first()
        if not has_rules:
            rules_tmpl = next((t for t in catalog if t["workflow_id"] == "baseline_dq_rules"), None)
            if rules_tmpl:
                _create_workflow_and_stages(project, rules_tmpl, next_order, session)
                added.append({"workflow_id": "baseline_dq_rules", "name": rules_tmpl["name"]})
                next_order += 1

    _create_workflow_and_stages(project, tmpl, next_order, session)
    added.append({"workflow_id": tmpl["workflow_id"], "name": tmpl["name"]})

    session.commit()
    if len(added) == 1:
        return {"status": "added", "workflow_id": tmpl["workflow_id"], "name": tmpl["name"]}
    return {"status": "added", "workflows": added}


class SwitchExclusiveGroupRequest(BaseModel):
    group: str
    select_stage_id: str


@router.post("/{project_id}/workflows/{workflow_id}/exclusive-group")
def switch_exclusive_group(
    project_id: int,
    workflow_id: str,
    body: SwitchExclusiveGroupRequest,
    session: Session = Depends(get_session),
):
    """Switch the active member of an exclusive_group (e.g. serving:
    virtual_view <-> physical_copy). Flips enabled flags in workflow_json,
    carries any EXCLUSIVE_GROUP_DEPENDENTS along, validates the result, then
    reconciles StageRun rows.

    StageRun has no stage_id and its identity is positional (stage_number ->
    enabled-list index), so survivors are identified by the stage_id they
    currently resolve to (via the PRE-EDIT order) and renumbered in a two-pass
    offset->finalize within one transaction. Completed stages keep their
    status/results; the deselected member's row is deleted; the newly-selected
    member gets a fresh pending row.
    """
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    from ..request_guard import guard_rest_mutation
    guard_rest_mutation(project, session)
    wf = session.exec(
        select(Workflow).where(
            Workflow.project_id == project_id, Workflow.workflow_id == workflow_id,
        )
    ).first()
    if not wf:
        raise HTTPException(404, f"Workflow '{workflow_id}' not found")

    steps = json.loads(wf.workflow_json) if wf.workflow_json else []
    members = [s for s in steps if s.get("exclusive_group") == body.group]
    if not members:
        raise HTTPException(400, f"No exclusive group '{body.group}' in this workflow")
    member_ids = {s["stage_id"] for s in members}
    if body.select_stage_id not in member_ids:
        raise HTTPException(400, f"'{body.select_stage_id}' is not a member of group '{body.group}'")

    # Already the active member → no-op.
    if [s["stage_id"] for s in members if s.get("enabled", True)] == [body.select_stage_id]:
        return get_workflow(project_id, workflow_id, session)

    # Capability pre-flight for lakehouse serving selection.
    if body.select_stage_id == "serving_lakehouse_export":
        try:
            from ..pg_resolver import resolve_source_connection_for_project
            from ..platform.registry import get_registry
            platform, _, _ = resolve_source_connection_for_project(project, session)
            if not get_registry().is_usable(platform, "lakehouse_source"):
                raise HTTPException(
                    400,
                    f"Platform '{platform}' does not support lakehouse export as a source "
                    "(lakehouse_source capability is unsupported). "
                    "Use a virtual view or cross-platform transfer instead.",
                )
        except HTTPException:
            raise
        except Exception:
            pass  # resolution failures surface during the actual export

    # Pre-edit positional map — used to identify surviving StageRuns by stage_id.
    old_enabled = [s for s in steps if s.get("enabled", True)]
    old_stage_by_order = {i + 1: st for i, st in enumerate(old_enabled)}

    # Edit enabled flags: the chosen group member on, others off; carry dependents.
    deps = EXCLUSIVE_GROUP_DEPENDENTS.get(body.group, {})
    dependent_owner = {dep: owner for owner, dep_list in deps.items() for dep in dep_list}
    for s in steps:
        sid = s["stage_id"]
        if s.get("exclusive_group") == body.group:
            s["enabled"] = (sid == body.select_stage_id)
        elif sid in dependent_owner:
            s["enabled"] = (dependent_owner[sid] == body.select_stage_id)

    # The only invariant a switch can break is exclusive-group single-enable,
    # which we just satisfied by construction. We deliberately do NOT run
    # validate_workflow here — it validates a single workflow in isolation and
    # flags this group's CROSS-workflow deps (e.g. serving -> data_mapping in
    # another workflow, or auto_mapping_sa for SA) as false errors. Cross-
    # workflow dependency enforcement happens at run time (check_stage_dependencies).
    enabled_members = [s["stage_id"] for s in members if s.get("enabled", True)]
    if len(enabled_members) != 1:
        raise HTTPException(500, f"switch produced {len(enabled_members)} enabled members in group '{body.group}'")

    wf.workflow_json = json.dumps(steps, indent=2)
    session.add(wf)

    # Reconcile StageRuns.
    new_ids = [s["stage_id"] for s in steps if s.get("enabled", True)]
    existing = session.exec(
        select(StageRun)
        .where(StageRun.project_id == project_id, StageRun.workflow_id == workflow_id)
        .order_by(StageRun.stage_number)
    ).all()
    existing_by_stage_id = {}
    for sr in existing:
        step = old_stage_by_order.get(sr.stage_number)
        if step:
            existing_by_stage_id[step["stage_id"]] = sr

    survivors = {sid: existing_by_stage_id[sid] for sid in new_ids if sid in existing_by_stage_id}
    # Delete the deselected member's row (and any orphan rows we couldn't map).
    for sr in existing:
        if sr not in survivors.values():
            session.delete(sr)
    session.flush()
    # Two-pass renumber to avoid transient stage_number collisions among survivors.
    for sr in survivors.values():
        sr.stage_number += 1000
        session.add(sr)
    session.flush()
    for idx, sid in enumerate(new_ids, 1):
        reg = STAGE_REGISTRY.get(sid, {})
        sr = survivors.get(sid)
        if sr is not None:
            sr.stage_number = idx
            sr.stage_name = reg.get("name", sid)
            session.add(sr)
        else:
            session.add(StageRun(
                project_id=project.id,
                workflow_id=workflow_id,
                stage_number=idx,
                stage_name=reg.get("name", sid),
                status=StageStatus.complete if sid == "initiate" else StageStatus.pending,
            ))
    session.commit()
    return get_workflow(project_id, workflow_id, session)


@router.delete("/{project_id}/workflows/{workflow_id}")
def remove_workflow(project_id: int, workflow_id: str, session: Session = Depends(get_session)):
    """Remove a workflow from a project. Only allowed if no stages are running or complete."""
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    wf = session.exec(
        select(Workflow).where(
            Workflow.project_id == project_id, Workflow.workflow_id == workflow_id,
        )
    ).first()
    if not wf:
        raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")

    # Check if any stages have been started
    stage_runs = session.exec(
        select(StageRun).where(
            StageRun.project_id == project_id, StageRun.workflow_id == workflow_id,
        )
    ).all()
    started = [s for s in stage_runs if s.status not in (StageStatus.pending,)]
    # Allow removal if only "initiate" stages are complete (auto-completed on creation)
    meaningful_started = [s for s in started if s.stage_name != "Initiate"]
    if meaningful_started:
        raise HTTPException(
            400,
            f"Cannot remove workflow '{wf.name}' — it has {len(meaningful_started)} started/completed stage(s).",
        )

    # Delete stage runs and workflow
    for sr in stage_runs:
        session.delete(sr)
    session.delete(wf)
    session.commit()
    return {"status": "removed", "workflow_id": workflow_id}


# ── Workflow catalog (not project-scoped) ──────────────────────────────────

catalog_router = APIRouter(prefix="/api/workflow-catalog", tags=["workflow-catalog"])


@catalog_router.get("/archetype/{archetype}")
def list_archetype_templates(archetype: str):
    """Return the default workflow templates for an archetype."""
    templates = get_workflow_templates(archetype)
    return [
        {
            "workflow_id": t["workflow_id"],
            "name": t["name"],
            "description": t.get("description", ""),
            "order": t["order"],
            "repeatable": t.get("repeatable", False),
            "stage_count": len([s for s in t["stages"] if s.get("enabled", True)]),
        }
        for t in templates
    ]


@catalog_router.get("")
def list_catalog(project_id: int | None = None, session: Session = Depends(get_session)):
    """Return available workflow templates.

    If project_id is provided, includes readiness info: which dependencies
    are already satisfied by completed stages in that project, and filters the
    DQ add-ons to the project's archetype (product-side DQ for products,
    catalog-side DQ for dataset archetypes).
    """
    _arch = None
    if project_id:
        _p = session.get(Project, project_id)
        _arch = _p.archetype if _p else None
    catalog = get_workflow_catalog(_arch)

    # Enrich each stage with registry metadata (name / description / owner_role)
    # so the frontend can render addable-capability cards identically to
    # already-added stage cards instead of falling back to the raw stage_id.
    for item in catalog:
        for stage in item.get("stages", []):
            reg = STAGE_REGISTRY.get(stage.get("stage_id"), {})
            stage["name"] = reg.get("name")
            stage["description"] = reg.get("description")
            stage["owner_role"] = reg.get("owner_role")

    if project_id:
        project = session.get(Project, project_id)
        if project:
            # Get all completed stage_ids across all workflows in this project
            all_runs = session.exec(
                select(StageRun).where(StageRun.project_id == project_id)
            ).all()

            # Get workflow definitions to resolve stage_id from stage_number
            wf_rows = session.exec(
                select(Workflow).where(Workflow.project_id == project_id)
            ).all()
            wf_defs = {}
            for wf in wf_rows:
                stages = json.loads(wf.workflow_json) if wf.workflow_json else []
                enabled = [s for s in stages if s.get("enabled", True)]
                wf_defs[wf.workflow_id] = {i + 1: s["stage_id"] for i, s in enumerate(enabled)}

            completed_ids = set()
            existing_wf_ids = {wf.workflow_id for wf in wf_rows}
            for sr in all_runs:
                if sr.status in (StageStatus.complete, StageStatus.awaiting_review):
                    if sr.workflow_id and sr.workflow_id in wf_defs:
                        sid = wf_defs[sr.workflow_id].get(sr.stage_number)
                        if sid:
                            completed_ids.add(sid)

            for item in catalog:
                reqs = item.get("required_stage_ids", [])
                item["deps_met"] = [sid for sid in reqs if sid in completed_ids]
                item["deps_unmet"] = [sid for sid in reqs if sid not in completed_ids]
                item["all_deps_met"] = len(item["deps_unmet"]) == 0
                item["already_added"] = item["workflow_id"] in existing_wf_ids

    return catalog


def _stage_run_dict(sr: StageRun, step: dict | None) -> dict:
    """Build a stage dict from a StageRun and its workflow step definition."""
    stage_id = step.get("stage_id") if step else None
    reg = STAGE_REGISTRY.get(stage_id, {}) if stage_id else {}
    return {
        "id": sr.id,
        "stage_number": sr.stage_number,
        "stage_name": sr.stage_name,
        "stage_id": stage_id,
        "workflow_id": sr.workflow_id,
        "status": sr.status,
        "started_at": sr.started_at.isoformat() if sr.started_at else None,
        "completed_at": sr.completed_at.isoformat() if sr.completed_at else None,
        "error_message": sr.error_message,
        "cost_usd": sr.cost_usd,
        "owner_role": reg.get("owner_role"),
        "has_review": reg.get("has_review", False),
        "review_type": reg.get("review_type"),
        "config_fields": reg.get("config_fields", []),
        "description": reg.get("description"),
        # Drives the agentic/deterministic indicator on the capability card.
        "requires_llm": reg.get("requires_llm", False),
        # True when the stage invokes the Claude Agent SDK / makes LLM calls —
        # a superset of requires_llm (adds deployment_reflection). Only agentic
        # stages get the discreet ✦ marker in the UI.
        "agentic": stage_is_agentic(stage_id),
    }


def _get_project_workflow(project: Project) -> list[dict]:
    """Get the project's workflow, falling back to old STAGES list for legacy projects."""
    if project.workflow_json:
        workflow = json.loads(project.workflow_json)
        return [s for s in workflow if s.get("enabled", True)]
    # Legacy fallback — synthesize from STAGES constant
    return [
        {"stage_id": f"legacy_{s['number']}", "order": s["number"], "enabled": True}
        for s in STAGES
    ]


def _project_response(project: Project, session: Session) -> dict:
    # ── Build stages list (for legacy single-workflow compat) ──
    stages = session.exec(
        select(StageRun)
        .where(StageRun.project_id == project.id)
        .order_by(StageRun.stage_number)
    ).all()

    workflow = _get_project_workflow(project)
    workflow_by_order = {i + 1: step for i, step in enumerate(workflow)}

    def _stage_meta(stage_number: int, key: str, default=None):
        step = workflow_by_order.get(stage_number)
        if step:
            stage_id = step.get("stage_id", "")
            reg = STAGE_REGISTRY.get(stage_id, {})
            if key in reg:
                return reg[key]
        return next((sd.get(key, default) for sd in STAGES if sd["number"] == stage_number), default)

    # For multi-workflow projects, only include non-workflow-scoped stages
    # in the flat stages list (backward compat). Workflow stages go in workflows[].
    if project.multi_workflow:
        flat_stages = [s for s in stages if not s.workflow_id]
    else:
        flat_stages = stages

    # ── Build workflows list ──
    workflows_data = []
    if project.multi_workflow:
        wf_rows = session.exec(
            select(Workflow)
            .where(Workflow.project_id == project.id)
            .order_by(Workflow.order)
        ).all()
        for wf in wf_rows:
            wf_stages = [s for s in stages if s.workflow_id == wf.workflow_id]
            wf_stages.sort(key=lambda s: s.stage_number)
            wf_stage_defs = json.loads(wf.workflow_json) if wf.workflow_json else []
            enabled = [s for s in wf_stage_defs if s.get("enabled", True)]
            wf_step_by_order = {i + 1: step for i, step in enumerate(enabled)}

            workflows_data.append({
                "workflow_id": wf.workflow_id,
                "name": wf.name,
                "description": wf.description,
                "order": wf.order,
                "repeatable": wf.repeatable,
                "stages": [
                    _stage_run_dict(sr, wf_step_by_order.get(sr.stage_number))
                    for sr in wf_stages
                ],
            })

    return {
        "id": project.id,
        "project_code": project.project_code,
        "name": project.name,
        "archetype": project.archetype,
        "domain": project.domain,
        "product_idea": project.product_idea,
        "parent_intake_submission_id": project.parent_intake_submission_id,
        "data_connectivity_mode": getattr(project, "data_connectivity_mode", "live") or "live",
        "discovery_complete_at": project.discovery_complete_at.isoformat() if project.discovery_complete_at else None,
        "neo4j_host": project.neo4j_host,
        "neo4j_port": project.neo4j_port,
        "neo4j_user": project.neo4j_user,
        "neo4j_password": project.neo4j_password,
        "neo4j_database": project.neo4j_database,
        "multi_workflow": project.multi_workflow,
        "current_stage": project.current_stage,
        "role_assignments": project.role_assignments,
        "created_at": project.created_at.isoformat(),
        "stages": [
            {
                "id": s.id,
                "stage_number": s.stage_number,
                "stage_name": s.stage_name,
                "stage_id": workflow_by_order.get(s.stage_number, {}).get("stage_id"),
                "status": s.status,
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "completed_at": s.completed_at.isoformat() if s.completed_at else None,
                "error_message": s.error_message,
                "cost_usd": s.cost_usd,
                "owner_role": _stage_meta(s.stage_number, "owner_role"),
                "has_review": _stage_meta(s.stage_number, "has_review", False),
                "review_type": _stage_meta(s.stage_number, "review_type"),
                "config_fields": _stage_meta(s.stage_number, "config_fields", []),
                "description": _stage_meta(s.stage_number, "description"),
            }
            for s in flat_stages
        ],
        "workflows": workflows_data,
    }
