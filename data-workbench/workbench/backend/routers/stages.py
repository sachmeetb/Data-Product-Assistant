import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

import json as json_mod
from datetime import datetime, timezone

from ..archetypes import STAGE_REGISTRY
from ..config import SKILLS_DIR
from ..database import get_session
from ..graph_ops import has_project_node
from ..models import Project, StageRun, StageStatus, Workflow
from ..neo4j_client import neo4j_session
from ..pipeline import STAGES
from .connections import (
    DISCOVERY_SKILL_BY_PLATFORM,
    build_connection_string,
    resolve_source_connection_ref,
)
from ..stage_execution import _PLATFORM_DIALECT_MAP as _PLATFORM_DIALECT_MAP_EXEC

# How long a stage can be "running" before we check if it's orphaned
ORPHAN_TIMEOUT_SECONDS = 300  # 5 minutes


def _parse_list_output(output: str) -> list[dict]:
    """Parse numbered list output from discover_schemas.py into config options.

    Expected format:
        Available schemas:
          1. public
          2. employees
    """
    items = []
    for line in output.strip().splitlines():
        m = re.match(r"\s*\d+\.\s+(.+)", line.strip())
        if m:
            name = m.group(1).strip()
            items.append({"value": name, "label": name})
    return items


def _parse_table_output(output: str) -> list[dict]:
    """Parse grouped table output from discover_tables.py into config options.

    Expected format:
        [employees]
          1. departments
          2. salaries
        [public]
          3. users
    """
    items = []
    current_schema = ""
    for line in output.strip().splitlines():
        schema_match = re.match(r"\[(.+)]", line.strip())
        if schema_match:
            current_schema = schema_match.group(1)
            continue
        m = re.match(r"\s*\d+\.\s+(.+)", line.strip())
        if m:
            table = m.group(1).strip()
            items.append({
                "value": f"{current_schema}.{table}",
                "label": table,
                "description": current_schema,
            })
    return items

def _discovery_scope_defaults(
    discovery_scope_json: str | None, discovery_tables: list[dict]
) -> dict | None:
    """Pre-checked Data Discovery defaults from a project's carried scope.

    Intersects the stored schema-qualified ``"schema.table"`` scope (carried from a
    Connected-Estate assembly scaffold) with the LIVE ``discovery_tables`` option
    values — so a renamed/dropped table can't produce a phantom pre-check — and
    returns ``{discovery_tables, discovery_schemas}`` for the frontend to seed the
    multiselects. ``None`` when there's no scope or nothing survives intersection.

    Multiselect defaults are emitted as ``", "``-joined STRINGS, matching the
    frontend's multiselect serialization contract (``configValues`` is
    ``Record<string, string>`` and reads via ``value.split(", ")``). Returning bare
    lists here crashed the config dialog (``.trim`` is not a function on an array)."""
    if not discovery_scope_json:
        return None
    try:
        scoped = set(json_mod.loads(discovery_scope_json) or [])
    except (TypeError, ValueError):
        return None
    live_values = {o.get("value") for o in (discovery_tables or [])}
    picked = sorted(v for v in scoped if v in live_values)
    if not picked:
        return None
    schemas = sorted({v.split(".", 1)[0] for v in picked if "." in v})
    return {
        "discovery_tables": ", ".join(picked),
        "discovery_schemas": ", ".join(schemas),
    }


router = APIRouter(prefix="/api/projects/{project_id}/stages", tags=["stages"])


def _find_stage_run(
    session: Session, project_id: int, stage_number: int, workflow_id: str | None = None
) -> StageRun | None:
    """Find a StageRun, optionally scoped by workflow_id."""
    q = select(StageRun).where(
        StageRun.project_id == project_id,
        StageRun.stage_number == stage_number,
    )
    if workflow_id:
        q = q.where(StageRun.workflow_id == workflow_id)
    return session.exec(q).first()


def _resolve_stage_id(
    project: Project, stage_number: int, workflow_id: str | None, session: Session
) -> str | None:
    """Resolve the stage_id for a given stage_number from the workflow definition."""
    if workflow_id and project.multi_workflow:
        wf = session.exec(
            select(Workflow).where(
                Workflow.project_id == project.id, Workflow.workflow_id == workflow_id
            )
        ).first()
        if wf and wf.workflow_json:
            stages = [s for s in json_mod.loads(wf.workflow_json) if s.get("enabled", True)]
            if stage_number <= len(stages):
                return stages[stage_number - 1].get("stage_id")
        return None

    # Legacy flat workflow
    if project.workflow_json:
        workflow = [s for s in json_mod.loads(project.workflow_json) if s.get("enabled", True)]
        if stage_number <= len(workflow):
            return workflow[stage_number - 1].get("stage_id")
    return None


def _resolve_orphaned_stages(project: Project, session: Session):
    """Detect stages stuck in 'running' with no active SDK process and resolve them.

    This handles the case where the server restarted (uvicorn --reload) or the
    asyncio background task was lost. Checks if the stage has been running longer
    than ORPHAN_TIMEOUT_SECONDS, then resolves based on Neo4j state.
    """
    from .websocket import _active_tasks

    running_stages = session.exec(
        select(StageRun).where(
            StageRun.project_id == project.id,
            StageRun.status == StageStatus.running,
        )
    ).all()

    now = datetime.now(timezone.utc)
    for stage_run in running_stages:
        if not stage_run.started_at:
            continue
        started = stage_run.started_at.replace(tzinfo=timezone.utc) if stage_run.started_at.tzinfo is None else stage_run.started_at
        elapsed = (now - started).total_seconds()
        if elapsed < ORPHAN_TIMEOUT_SECONDS:
            continue

        # Check if there's still an active background task for this stage
        has_active_task = any(not t.done() for t in _active_tasks.values())
        if has_active_task:
            continue

        # Stage is orphaned — resolve it based on what's in the graph
        stage_id = None
        if project.workflow_json:
            workflow = [s for s in json_mod.loads(project.workflow_json) if s.get("enabled", True)]
            if stage_run.stage_number <= len(workflow):
                stage_id = workflow[stage_run.stage_number - 1].get("stage_id")

        stage_def = STAGE_REGISTRY.get(stage_id, {}) if stage_id else {}
        review_type = stage_def.get("review_type")

        # code_spec review lives in SQLite (CodeMigrationPlanRow), not Neo4j — an
        # unrecognized review type would fall through and be marked complete. It's
        # complete only when the reverse-engineered spec has been approved.
        if review_type == "code_spec":
            from .. import code_migration_orchestrator as cmo
            row = cmo.get_row(session, project.project_code)
            approved = bool(row and row.approved_spec_hash)
            stage_run.status = StageStatus.complete if approved else StageStatus.awaiting_review
            stage_run.completed_at = now
            stage_run.error_message = None
            session.add(stage_run)
            continue

        if review_type:
            # Check Neo4j for pending review items
            pending = 0
            scoped = has_project_node(project)
            pc = {"project_code": project.project_code} if scoped else {}
            _prj_ds = (
                "MATCH (:Project {projectCode: $project_code})"
                "-[:HAS_CATALOG]->(:Catalog)-[:DCAT_DATASET]->"
            )
            try:
                with neo4j_session(
                    project.neo4j_host, project.neo4j_port,
                    project.neo4j_user, project.neo4j_password, project.neo4j_database,
                ) as ns:
                    if review_type == "descriptions":
                        if scoped:
                            q = f"{_prj_ds}(:Dataset)-[:HAS_COLUMN]->(:Column)-[:HAS_DESCRIPTION]->(cd:ColumnDescription) WHERE cd.status = 'pending_review' AND cd.isCurrent = true RETURN count(cd) AS cnt"
                        else:
                            q = "MATCH (cd:ColumnDescription) WHERE cd.status = 'pending_review' AND cd.isCurrent = true RETURN count(cd) AS cnt"
                        pending = ns.run(q, **pc).single()["cnt"]
                    elif review_type == "mappings":
                        if scoped:
                            # Anchor on this project's product columns instead
                            # of the catalog source path — same fix as
                            # _check_review_complete in reviews.py. Catalog-only
                            # walk silently returns 0 for dpe-cf projects whose
                            # mappings source :DProdColumn, leaving orphans
                            # to mark Data Mapping complete when it should
                            # have flipped to awaiting_review.
                            q = (
                                "MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn) "
                                "WHERE cm.status = 'pending_review' AND cm.isCurrent = true "
                                "  AND pc.uri STARTS WITH 'dprod:col:' + $project_code + '-contract:' "
                                "RETURN count(cm) AS cnt"
                            )
                        else:
                            q = "MATCH (cm:ColumnMapping) WHERE cm.status = 'pending_review' AND cm.isCurrent = true RETURN count(cm) AS cnt"
                        pending = ns.run(q, **pc).single()["cnt"]
            except Exception:
                pass

            if pending > 0:
                stage_run.status = StageStatus.awaiting_review
                stage_run.completed_at = now
                stage_run.error_message = None
            else:
                # No pending items — maybe it completed fully, or maybe it failed
                stage_run.status = StageStatus.complete
                stage_run.completed_at = now
                stage_run.error_message = None
        else:
            # Non-review stage — mark as complete (agent produced artifacts)
            stage_run.status = StageStatus.complete
            stage_run.completed_at = now
            stage_run.error_message = None

        session.add(stage_run)

    session.commit()


@router.get("")
def list_stages(project_id: int, session: Session = Depends(get_session)):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Auto-resolve orphaned running stages before listing
    _resolve_orphaned_stages(project, session)

    stages = session.exec(
        select(StageRun)
        .where(StageRun.project_id == project_id)
        .order_by(StageRun.stage_number)
    ).all()

    return [
        {
            "id": s.id,
            "stage_number": s.stage_number,
            "stage_name": s.stage_name,
            "status": s.status,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "completed_at": s.completed_at.isoformat() if s.completed_at else None,
            "error_message": s.error_message,
            "cost_usd": s.cost_usd,
            "owner_role": next(
                (st["owner_role"] for st in STAGES if st["number"] == s.stage_number),
                None,
            ),
            "requires_llm": next(
                (st["requires_llm"] for st in STAGES if st["number"] == s.stage_number),
                None,
            ),
            # Legacy STAGES has no deployment_reflection, so agentic == requires_llm
            # here (kept for payload-shape parity with the workflow serializer).
            "agentic": next(
                (st["requires_llm"] for st in STAGES if st["number"] == s.stage_number),
                None,
            ),
            "has_review": next(
                (st["has_review"] for st in STAGES if st["number"] == s.stage_number),
                None,
            ),
            "review_type": next(
                (st["review_type"] for st in STAGES if st["number"] == s.stage_number),
                None,
            ),
        }
        for s in stages
    ]


@router.get("/{stage_number}")
def get_stage(
    project_id: int,
    stage_number: int,
    workflow_id: str | None = None,
    session: Session = Depends(get_session),
):
    stage_run = _find_stage_run(session, project_id, stage_number, workflow_id)
    if not stage_run:
        raise HTTPException(status_code=404, detail="Stage not found")
    return stage_run


@router.post("/{stage_number}/complete")
def complete_stage(
    project_id: int,
    stage_number: int,
    workflow_id: str | None = None,
    session: Session = Depends(get_session),
):
    """Mark a non-LLM stage as complete (e.g. ODCS specification, publish).

    A handful of dpe-sa stages have backend side-effects that fire on
    completion — the request body of the work itself happens here, not in
    a separate endpoint, so the frontend's "I'm done" gesture is enough:
      - mark_discovery_complete → stamps Project.discovery_complete_at
        so the PO dashboard surfaces "Ready to validate".
      - synthesize_odcs_from_graph → walks approved graph state into an
        ODCS dict and calls _save_odcs_to_graph.
      - auto_mapping_sa → writes 1:1 :ColumnMapping rows from each
        :Column to its corresponding :DProdColumn.
    """
    from ..models import StageStatus
    from datetime import datetime, timezone

    stage_run = _find_stage_run(session, project_id, stage_number, workflow_id)
    if not stage_run:
        raise HTTPException(status_code=404, detail="Stage not found")
    if stage_run.status not in (StageStatus.pending, StageStatus.running):
        raise HTTPException(status_code=400, detail=f"Stage is {stage_run.status}, cannot complete")

    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Acceptance gate (warn-only by default; 409 when WB_ENFORCE_ACCEPT_GATE is on).
    from ..request_guard import guard_rest_mutation
    guard_rest_mutation(project, session)

    stage_id = _resolve_stage_id(project, stage_number, workflow_id or stage_run.workflow_id, session)

    # D1 execution gate: the migration execute stages may not be marked complete
    # (a manual bypass of the /snapshot,/reconcile run) unless the project can
    # actually execute — mode permitting + real source + target + package.
    if stage_id in ("dmig_execute_transfer", "dmig_reconcile"):
        from .migration import require_execution_ready
        require_execution_ready(project, session)

    if stage_id == "mark_discovery_complete":
        # Guard the gate: completing it stamps discovery_complete_at (→ the PO sees
        # "ready to validate"), so it must not run before the engineer has RUN the
        # enrichment/naming that produce the descriptions and names the PO validates.
        # "Run" = complete OR awaiting_review: metadata_enrichment has a review gate
        # and (for dpe-sa) sits at awaiting_review until the PO validates LATER, so
        # requiring `complete` here would wrongly block the normal flow. We only
        # block when an upstream is still pending/running/failed (not yet run), and
        # only for upstreams PRESENT in this project's workflow.
        _present: set[str] = set()
        _ran: set[str] = set()
        for _r in session.exec(select(StageRun).where(StageRun.project_id == project_id)).all():
            _sid = _resolve_stage_id(project, _r.stage_number, _r.workflow_id, session)
            if _sid:
                _present.add(_sid)
                if _r.status in (StageStatus.complete, StageStatus.awaiting_review):
                    _ran.add(_sid)
        _unmet = [STAGE_REGISTRY.get(d, {}).get("name", d)
                  for d in ("metadata_enrichment", "column_name_standardization")
                  if d in _present and d not in _ran]
        if _unmet:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot mark discovery complete — run these first: {', '.join(_unmet)}.",
            )

    # Extra fields folded into the completion response (e.g. synthesis warnings).
    _completion_extra: dict = {}

    if stage_id == "mark_discovery_complete":
        project.discovery_complete_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.add(project)
    elif stage_id == "synthesize_odcs_from_graph":
        from .sa_pipeline import synthesize_odcs_from_graph
        try:
            _name_fallbacks: list = []
            synthesize_odcs_from_graph(project, collect_warnings=_name_fallbacks)
            if _name_fallbacks:
                # The PO's recommended names for these columns weren't approved,
                # so the raw source names were used. Surface it instead of the
                # old silent fallback.
                _completion_extra["warnings"] = {
                    "raw_name_fallbacks": len(_name_fallbacks),
                    "columns": _name_fallbacks,
                }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"ODCS synthesis failed: {e}")
    elif stage_id == "odcs_to_dprod":
        # The dprod materialization is the WORK of this stage — generate the
        # :DProdDataProduct / :DProdOutputDataset / :DProdColumn nodes here, not
        # just flip status. Previously only the frontend's NON_LLM_ACTIONS post-
        # action POSTed /odcs/generate-dprod before /complete, so completing via
        # the API (MCP complete_stage) or any path that didn't replay that call
        # left the stage green with zero dprod output and blocked serving. Mirror
        # the generate_dprod endpoint; _generate_dprod is idempotent (wipe+rebuild).
        from .odcs import _generate_dprod
        contract_id = f"{project.project_code}-contract"
        try:
            result = _generate_dprod(contract_id, project)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"dprod generation failed: {e}")
        if not result:
            raise HTTPException(
                status_code=400,
                detail="No ODCS contract to transform — run Synthesize ODCS first.",
            )
    elif stage_id == "auto_mapping_sa":
        from .sa_pipeline import write_auto_mappings
        try:
            write_auto_mappings(project)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Auto-mapping failed: {e}")
    elif stage_id == "deploy_virtual_view":
        # Deploying executes the view DDL against Postgres + stamps the
        # deployment status — real work, not a flip. Run it here (mirrors the
        # frontend, which POSTs /serving/deploy before /complete) so any caller
        # actually deploys. A failed deploy raises, so the stage does NOT go
        # green with deployment_status still 'pending'.
        from .serving import deploy_virtual_view as _deploy
        try:
            deploy_result = _deploy(project.id, None, session)
        except HTTPException:
            raise  # 409: no connection / no serving definition — surface as-is
        if isinstance(deploy_result, dict) and deploy_result.get("status") == "failed":
            raise HTTPException(
                status_code=500,
                detail=(
                    f"View deployment failed ({deploy_result.get('error_class')}): "
                    f"{deploy_result.get('error_message')}"
                ),
            )
    elif stage_id == "serving_physical_copy":
        # BUILD-only (Configure → Build → Deploy): scaffold + assemble the runnable
        # dbt project — no `dbt build`. The frontend POSTs /serving/dbt/build-package
        # before /complete; only scaffold here when the project isn't on disk yet
        # (MCP / API callers that complete directly). The `dbt build` runs in the
        # coupled deploy_physical_copy stage.
        from .materialization import (
            build_dbt_package as _build_dbt, DbtBuildPackageBody, collect_dbt_files,
        )
        if not collect_dbt_files(project.project_code):
            _build_dbt(project.id, DbtBuildPackageBody(), session)
    elif stage_id == "deploy_physical_copy":
        # DEPLOY: `dbt build` physical tables (+ SCD2 snapshots). The frontend drives
        # the sample→approve gate dialog (which POSTs /serving/materialize then
        # /complete), so only build here when no successful build exists yet (MCP /
        # API callers that complete directly) — avoids a redundant, slow rebuild.
        from .materialization import (
            materialize as _materialize,
            MaterializeBody,
            has_successful_materialization,
        )
        if not has_successful_materialization(project):
            # force=True: completing the stage via API/MCP IS the approval — the
            # interactive UI path goes through the sample→approve gate instead.
            result = _materialize(project.id, MaterializeBody(mode="full", force=True), session)
            if isinstance(result, dict) and result.get("status") != "built":
                raise HTTPException(
                    status_code=500,
                    detail=f"Materialize failed: {result.get('error')}",
                )
    elif stage_id == "serving_lakehouse_export":
        # BUILD-only: assemble the runnable lakehouse package (no live source). The
        # frontend POSTs /serving/lakehouse/build before /complete; only build here
        # when no serving definition exists yet (MCP / API direct-complete) so we
        # never clobber a deployed export's file list. The export runs in the
        # coupled deploy_lakehouse stage.
        from .export import get_lakehouse_status as _lh_status
        from .. import lakehouse_export as _lh
        if not _lh_status(project.id, session).get("configured"):
            _lh.build_lakehouse_package(project, session, executed_by="Data Engineer")
    elif stage_id == "deploy_lakehouse":
        # DEPLOY: run the built package against the live source → Parquet + a DuckDB
        # catalog. The frontend POSTs /serving/export before /complete; run here
        # (idempotent) when no files have been produced yet so MCP / API direct-
        # complete also exports. A failed export raises (build_error surfaced).
        from .export import get_lakehouse_status as _lh_status
        from .. import lakehouse_export as _lh
        if not _lh_status(project.id, session).get("file_uris"):
            try:
                _lh.run_lakehouse_export(project, session, mode="full", executed_by="Data Engineer")
            except RuntimeError as e:
                raise HTTPException(status_code=422, detail=str(e))
    elif stage_id == "mark_engineering_complete":
        # Publish/lifecycle flip mirrors the frontend's mark_engineering_complete
        # action (which POSTs /product-requests/{id}/complete before /complete).
        # Doing it here means completing via ANY path (UI/API/MCP) actually
        # publishes the contract — without this, the view/table deploys but the
        # contract stays draft and the marketplace shows it as draft.
        from .product_requests import complete_product_request, CompleteRequestBody
        from ..models import ProductRequest, ProductRequestStatus
        latest = session.exec(
            select(ProductRequest)
            .where(ProductRequest.project_id == project_id)
            .where(ProductRequest.status != ProductRequestStatus.rejected)
            .order_by(ProductRequest.submitted_at.desc())
        ).first()
        # Idempotent: complete_product_request only accepts submitted/accepted,
        # so a request the UI already completed is skipped (no double-publish).
        if latest and latest.status in (ProductRequestStatus.accepted, ProductRequestStatus.submitted):
            try:
                complete_product_request(latest.id, CompleteRequestBody(), session)
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Publish on engineering-complete failed: {e}")
    elif stage_id == "publish":
        # Same pattern: the UI POSTs /odcs/publish before /complete, so completing
        # via the API alone skipped the publish. Run it here.
        from .odcs import publish_data_product
        try:
            publish_data_product(project.id, session)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Publish failed: {e}")

    stage_run.status = StageStatus.complete
    stage_run.completed_at = datetime.now(timezone.utc)
    if not stage_run.started_at:
        stage_run.started_at = datetime.now(timezone.utc)
    session.add(stage_run)
    session.commit()
    return {"status": "complete", "stage_number": stage_number, **_completion_extra}


@router.post("/{stage_number}/recover")
def recover_stage(
    project_id: int,
    stage_number: int,
    workflow_id: str | None = None,
    session: Session = Depends(get_session),
):
    """Recover a failed stage that has pending review items in Neo4j.

    If the stage failed (e.g., WebSocket disconnect) but the agent actually
    completed its work and created review items, this transitions the stage
    to awaiting_review so the user can proceed with reviews.
    """
    from ..models import StageStatus
    from datetime import datetime, timezone

    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    stage_run = _find_stage_run(session, project_id, stage_number, workflow_id)
    if not stage_run:
        raise HTTPException(status_code=404, detail="Stage not found")
    if stage_run.status != StageStatus.failed:
        return {"status": stage_run.status, "recovered": False, "reason": "Stage is not in failed state"}

    stage_id = _resolve_stage_id(project, stage_number, workflow_id or stage_run.workflow_id, session)
    stage_def = STAGE_REGISTRY.get(stage_id, {}) if stage_id else {}
    review_type = stage_def.get("review_type")

    if not review_type:
        # Not a review stage — just offer to mark complete if work was done
        stage_run.status = StageStatus.complete
        stage_run.completed_at = datetime.now(timezone.utc)
        stage_run.error_message = None
        session.add(stage_run)
        session.commit()
        return {"status": "complete", "recovered": True, "reason": "Stage recovered as complete"}

    # code_spec review is tracked in SQLite (CodeMigrationPlanRow), not Neo4j —
    # recover to complete only when the spec has been approved, else awaiting_review.
    if review_type == "code_spec":
        from .. import code_migration_orchestrator as cmo
        row = cmo.get_row(session, project.project_code)
        approved = bool(row and row.approved_spec_hash)
        stage_run.status = StageStatus.complete if approved else StageStatus.awaiting_review
        stage_run.completed_at = datetime.now(timezone.utc)
        stage_run.error_message = None
        session.add(stage_run)
        session.commit()
        return {"status": stage_run.status.value if hasattr(stage_run.status, "value") else stage_run.status,
                "recovered": True,
                "reason": "code_spec approved" if approved else "awaiting spec approval"}

    # Check Neo4j for pending review items — scoped to this project when a
    # :Project node exists (mirrors _resolve_orphaned_stages). The unscoped
    # query is a legacy fallback only; without scoping, pending items in ANY
    # project would recover an unrelated project into awaiting_review.
    pending = 0
    scoped = has_project_node(project)
    pc = {"project_code": project.project_code} if scoped else {}
    _prj_ds = (
        "MATCH (:Project {projectCode: $project_code})"
        "-[:HAS_CATALOG]->(:Catalog)-[:DCAT_DATASET]->"
    )
    try:
        with neo4j_session(
            project.neo4j_host, project.neo4j_port,
            project.neo4j_user, project.neo4j_password, project.neo4j_database,
        ) as ns:
            if review_type == "descriptions":
                if scoped:
                    q = f"{_prj_ds}(:Dataset)-[:HAS_COLUMN]->(:Column)-[:HAS_DESCRIPTION]->(cd:ColumnDescription) WHERE cd.status = 'pending_review' AND cd.isCurrent = true RETURN count(cd) AS cnt"
                else:
                    q = "MATCH (cd:ColumnDescription) WHERE cd.status = 'pending_review' AND cd.isCurrent = true RETURN count(cd) AS cnt"
                pending = ns.run(q, **pc).single()["cnt"]
            elif review_type == "mappings":
                if scoped:
                    # Anchor on this project's product columns — same fix as
                    # _check_review_complete / _resolve_orphaned_stages. A
                    # catalog-only walk silently returns 0 for dpe-cf projects
                    # whose mappings source :DProdColumn.
                    q = (
                        "MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn) "
                        "WHERE cm.status = 'pending_review' AND cm.isCurrent = true "
                        "  AND pc.uri STARTS WITH 'dprod:col:' + $project_code + '-contract:' "
                        "RETURN count(cm) AS cnt"
                    )
                else:
                    q = "MATCH (cm:ColumnMapping) WHERE cm.status = 'pending_review' AND cm.isCurrent = true RETURN count(cm) AS cnt"
                pending = ns.run(q, **pc).single()["cnt"]
    except Exception:
        pass

    if pending > 0:
        stage_run.status = StageStatus.awaiting_review
        stage_run.error_message = None
        session.add(stage_run)
        session.commit()
        return {"status": "awaiting_review", "recovered": True, "pending_items": pending}
    else:
        return {"status": "failed", "recovered": False, "reason": "No pending review items found in Neo4j"}


@router.post("/{stage_number}/reset")
def reset_stage(
    project_id: int,
    stage_number: int,
    workflow_id: str | None = None,
    session: Session = Depends(get_session),
):
    """Reset a stuck stage back to pending."""
    from ..models import StageStatus
    stage_run = _find_stage_run(session, project_id, stage_number, workflow_id)
    if not stage_run:
        raise HTTPException(status_code=404, detail="Stage not found")
    project = session.get(Project, project_id)
    if project:
        from ..request_guard import guard_rest_mutation
        guard_rest_mutation(project, session)
    stage_run.status = StageStatus.pending
    stage_run.started_at = None
    stage_run.completed_at = None
    stage_run.error_message = None
    session.add(stage_run)
    session.commit()
    return {"status": "reset", "stage_number": stage_number}


# ── Neo4j queries for config options ────────────────────────────────────────

_PRJ_DS = (
    "MATCH (:Project {projectCode: $project_code})"
    "-[:HAS_CATALOG]->(:Catalog)-[:DCAT_DATASET]->"
)
_PRJ_CONTRACT = (
    "MATCH (:Project {projectCode: $project_code})"
    "-[:HAS_CONTRACT]->(:DataContract)-[:MATERIALISES_AS]->"
)

UNMAPPED_DATA_PRODUCTS = """\
MATCH (dp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
OPTIONAL MATCH (cm:ColumnMapping {isCurrent: true, status: 'approved'})-[:MAPS_TO_PRODUCT_COLUMN]->(pc)
WITH dp, count(pc) AS total_cols, count(cm) AS mapped_cols
WHERE mapped_cols < total_cols
RETURN dp.uri AS uri, dp.name AS name, total_cols, mapped_cols
ORDER BY dp.name
"""
UNMAPPED_DATA_PRODUCTS_S = f"""\
{_PRJ_CONTRACT}(dp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
OPTIONAL MATCH (cm:ColumnMapping {{isCurrent: true, status: 'approved'}})-[:MAPS_TO_PRODUCT_COLUMN]->(pc)
WITH dp, count(pc) AS total_cols, count(cm) AS mapped_cols
WHERE mapped_cols < total_cols
RETURN dp.uri AS uri, dp.name AS name, total_cols, mapped_cols
ORDER BY dp.name
"""

UNMAPPED_SOURCE_DATASETS = """\
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)
OPTIONAL MATCH (cm:ColumnMapping {isCurrent: true, status: 'approved'})-[:MAPS_SOURCE_COLUMN]->(col)
WITH ds, count(col) AS total_cols, count(cm) AS mapped_cols
WHERE mapped_cols < total_cols
RETURN ds.uri AS uri, ds.schema + '.' + ds.name AS name, total_cols, mapped_cols
ORDER BY ds.schema, ds.name
"""
UNMAPPED_SOURCE_DATASETS_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_COLUMN]->(col:Column)
OPTIONAL MATCH (cm:ColumnMapping {{isCurrent: true, status: 'approved'}})-[:MAPS_SOURCE_COLUMN]->(col)
WITH ds, count(col) AS total_cols, count(cm) AS mapped_cols
WHERE mapped_cols < total_cols
RETURN ds.uri AS uri, ds.schema + '.' + ds.name AS name, total_cols, mapped_cols
ORDER BY ds.schema, ds.name
"""

# Consumer-aligned: source datasets are :DProdOutputDataset rows from
# products this contract :CONSUMES. Used by the data_mapping config-options
# endpoint when the project's contract has any :CONSUMES edges.
#
# The :ColumnMapping filter is scoped to mappings whose target :DProdColumn
# belongs to THIS consumer's contract — source-side :DProdColumn nodes are
# shared across every consumer of the SA product, so an unscoped count
# silently hides a source dataset from consumer B as soon as consumer A
# has fully mapped it. The pre-aggregation WITH groups by c so total_cols
# counts distinct columns and mapped_cols doesn't fan out when a single
# source column feeds multiple consumer product columns.
UNMAPPED_CONSUMED_DATASETS = """\
MATCH (dc:DataContract {id: $contract_id})-[r:CONSUMES]->(srcDp:DProdDataProduct)
WHERE r.toVersion IS NULL
MATCH (srcDp)
      -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(c:DProdColumn)
OPTIONAL MATCH (cm:ColumnMapping {isCurrent: true, status: 'approved'})-[:MAPS_SOURCE_COLUMN]->(c)
WHERE cm IS NULL OR EXISTS {
  MATCH (cm)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
  WHERE pc.uri STARTS WITH 'dprod:col:' + $contract_id + ':'
}
WITH srcDp, ods, c, count(cm) AS c_maps
WITH srcDp, ods,
     count(c) AS total_cols,
     sum(CASE WHEN c_maps > 0 THEN 1 ELSE 0 END) AS mapped_cols
WHERE mapped_cols < total_cols
RETURN ods.uri  AS uri,
       coalesce(srcDp.name, '') + ' / ' + coalesce(ods.physicalName, ods.name, '') AS name,
       total_cols, mapped_cols
ORDER BY srcDp.name, ods.physicalName
"""


PLAYBOOK_OPTIONS_QUERY = """\
MATCH (d:Domain {name: $domain})-[:HAS_PLAYBOOK]->(pb:Playbook)
OPTIONAL MATCH (pb)-[:HAS_VERSION]->(pv:PlaybookVersion)
WITH pb, pv ORDER BY pv.version DESC
WITH pb, collect(pv) AS versions
OPTIONAL MATCH (pb)-[:HAS_ITEM]->(pi:PlaybookItem {isCurrent: true})
WITH pb, versions, count(pi) AS item_count
RETURN
    pb.phase AS phase,
    item_count,
    CASE WHEN size(versions) > 0 THEN versions[0].version ELSE 1 END AS latest_version,
    CASE WHEN size(versions) > 0 THEN versions[0].summary ELSE null END AS latest_summary,
    CASE WHEN size(versions) > 0 THEN versions[0].createdAt ELSE null END AS latest_updated,
    CASE WHEN size(versions) > 1 THEN true ELSE false END AS has_refined
"""


@router.get("/{stage_number}/playbook-options")
def get_playbook_options(
    project_id: int,
    stage_number: int,
    session: Session = Depends(get_session),
):
    """Return available playbook versions (baseline vs refined) for this stage."""
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    domain = project.domain or ""
    if not domain:
        return {"baseline": {"version": 1, "summary": "Default playbook (no domain set)", "item_count": 0}, "refined": None}

    try:
        with neo4j_session(
            project.neo4j_host, project.neo4j_port,
            project.neo4j_user, project.neo4j_password, project.neo4j_database,
        ) as ns:
            rows = [dict(r) for r in ns.run(PLAYBOOK_OPTIONS_QUERY, domain=domain)]
    except Exception:
        rows = []

    if not rows:
        return {
            "baseline": {"version": 1, "summary": f"Standard playbook for {domain}", "item_count": 0},
            "refined": None,
        }

    total_items = sum(r["item_count"] for r in rows)
    has_refined = any(r["has_refined"] for r in rows)
    latest_version = max(r["latest_version"] for r in rows) if rows else 1
    summaries = [r["latest_summary"] for r in rows if r["latest_summary"]]

    baseline = {
        "version": 1,
        "summary": f"Original standard playbook for {domain} domain",
        "item_count": total_items if not has_refined else None,
    }

    refined = None
    if has_refined:
        refined = {
            "version": latest_version,
            "summary": summaries[0] if summaries else f"Refined through {latest_version - 1} reflection cycle(s)",
            "item_count": total_items,
            "updated_at": str(rows[0].get("latest_updated", "")),
        }

    return {"baseline": baseline, "refined": refined}


@router.get("/{stage_number}/config-options")
def get_config_options(
    project_id: int,
    stage_number: int,
    workflow_id: str | None = None,
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    result: dict = {}

    # Resolve stage_id from project workflow or workflow_id
    stage_id = _resolve_stage_id(project, stage_number, workflow_id, session)

    # Data Discovery — run the skill's scripts to list schemas and tables.
    # Routes through SourceBinding when a non-Postgres platform is bound so the
    # table picker shows schemas/tables from MySQL (or future platforms) rather
    # than always querying the legacy pg_connection.
    if stage_id in ("data_discovery", "data_discovery_composite") or (stage_id is None and stage_number == 2):
        # Resolve the effective platform type + connection string.
        platform_type, conn_ref = resolve_source_connection_ref(project, session)
        effective_conn_str = build_connection_string(platform_type, conn_ref)

        # No source DB configured yet — return an EXPLICIT signal.
        if not effective_conn_str.strip():
            return {
                "needs_data_source": True,
                "error": (
                    "No source database is configured for this project. Set it "
                    "first — the web UI's 'Select Data Source', or the MCP "
                    "set_data_source tool — then re-fetch options."
                ),
            }

        # Pick the discovery skill for the bound platform.
        # For composite stages, honour the first discovery-named sub-stage skill,
        # then fall back to the registry's own skill, then the platform override.
        skill_name: str | None = None
        if stage_id:
            stage_reg = STAGE_REGISTRY.get(stage_id, {})
            skill_name = stage_reg.get("skill")
            if not skill_name and "sub_stages" in stage_reg:
                for sub_id in stage_reg["sub_stages"]:
                    sub_skill = STAGE_REGISTRY.get(sub_id, {}).get("skill")
                    if sub_skill and "discovery" in sub_skill:
                        skill_name = sub_skill
                        break
        if not skill_name:
            skill_name = "data-discovery"
        # Platform override wins: mysql → data-discovery-mysql, etc.
        skill_name = DISCOVERY_SKILL_BY_PLATFORM.get(platform_type, skill_name)

        skill_dir = SKILLS_DIR / skill_name / "scripts"
        schemas_script = skill_dir / "discover_schemas.py"
        tables_script = skill_dir / "discover_tables.py"

        if schemas_script.exists():
            try:
                import subprocess, sys
                schemas_proc = subprocess.run(
                    [sys.executable, str(schemas_script), effective_conn_str],
                    capture_output=True, text=True, timeout=30,
                )
                if schemas_proc.returncode == 0:
                    result["discovery_schemas"] = _parse_list_output(schemas_proc.stdout)

                    if result.get("discovery_schemas") and tables_script.exists():
                        all_schemas = [s["value"] for s in result["discovery_schemas"]]
                        tables_proc = subprocess.run(
                            [sys.executable, str(tables_script), effective_conn_str] + all_schemas,
                            capture_output=True, text=True, timeout=30,
                        )
                        if tables_proc.returncode == 0:
                            result["discovery_tables"] = _parse_table_output(tables_proc.stdout)
                            # Pre-check the cluster's tables when this project was
                            # scaffolded from a Connected-Estate assembly (the
                            # frontend seeds checked values from `defaults`).
                            defaults = _discovery_scope_defaults(
                                project.discovery_scope_json, result["discovery_tables"])
                            if defaults:
                                result["defaults"] = defaults
                else:
                    err = (schemas_proc.stderr or schemas_proc.stdout or "").strip()
                    result["error"] = (
                        "Could not enumerate the source catalog — the connection "
                        f"may be unreachable or the credentials wrong. Details: {err[:400]}"
                    )
            except Exception as e:
                raise HTTPException(500, f"Discovery script failed: {e}")

    if stage_id == "data_mapping" or (stage_id is None and stage_number == 8):  # Data Mapping
        # The project's own data product has a predictable URI
        project_dprod_uri = f"dprod:{project.project_code}-contract"
        try:
            with neo4j_session(
                project.neo4j_host, project.neo4j_port,
                project.neo4j_user, project.neo4j_password, project.neo4j_database,
            ) as ns:
                scoped = has_project_node(project)
                pc = {"project_code": project.project_code} if scoped else {}
                q_prod = UNMAPPED_DATA_PRODUCTS_S if scoped else UNMAPPED_DATA_PRODUCTS
                q_ds = UNMAPPED_SOURCE_DATASETS_S if scoped else UNMAPPED_SOURCE_DATASETS

                products = ns.run(q_prod, **pc)
                product_list = []
                default_product = None
                for r in products:
                    entry = {
                        "value": r["name"],
                        "label": f"{r['name']} ({r['mapped_cols']}/{r['total_cols']} mapped)",
                        "uri": r["uri"],
                    }
                    product_list.append(entry)
                    if r["uri"] == project_dprod_uri:
                        default_product = r["name"]
                result["data_product"] = product_list
                if default_product:
                    result["defaults"] = {"data_product": default_product}

                # Post source/consumer split: archetype is the dispatch.
                # dpe-cf == consumer-aligned (wizard Step 2 enforces :CONSUMES);
                # everything else uses the project's raw catalog.
                if project.archetype == "dpe-cf":
                    datasets = ns.run(
                        UNMAPPED_CONSUMED_DATASETS,
                        contract_id=f"{project.project_code}-contract",
                    )
                else:
                    datasets = ns.run(q_ds, **pc)
                result["source_tables"] = [
                    {
                        "value": r["name"],
                        "label": f"{r['name']} ({r['mapped_cols']}/{r['total_cols']} mapped)",
                        "uri": r["uri"],
                    }
                    for r in datasets
                ]
        except Exception as e:
            raise HTTPException(500, f"Neo4j query failed: {e}")

    if stage_id == "data_remediation_planning":
        result["remediation_action"] = [
            {"value": "apply_all", "label": "Apply all recommended remediations"},
            {"value": "scripts_only", "label": "Generate SQL scripts only (review first)"},
            {"value": "skip", "label": "Skip remediation"},
        ]

    # Phase 7: dialect picker for serving_virtual_view. Mirrors generate_view_ddl.py's
    # _DIALECTS registry; aliases like 'postgresql' are intentionally omitted so the
    # UI picker is uncluttered. Default is auto-derived from the project's SourceBinding
    # platform when set, falling back to "postgres".
    if stage_id == "serving_virtual_view":
        result["dialect"] = [
            {"value": "postgres",   "label": "PostgreSQL"},
            {"value": "mysql",      "label": "MySQL"},
            {"value": "snowflake",  "label": "Snowflake"},
            {"value": "databricks", "label": "Databricks"},
            {"value": "bigquery",   "label": "BigQuery"},
            {"value": "ansi",       "label": "ANSI SQL (portable fallback)"},
        ]
        defaults = result.setdefault("defaults", {})
        # Derive dialect default from SourceBinding platform, then target_dialect override.
        from ..models import SourceBinding as _SourceBinding, PlatformConnection as _PlatformConnection
        _binding = session.exec(
            select(_SourceBinding).where(_SourceBinding.project_id == project_id)
        ).first()
        if _binding:
            _conn = session.get(_PlatformConnection, _binding.connection_id)
            if _conn:
                _target = (_binding.target_dialect or "").strip()
                defaults["dialect"] = _target or _PLATFORM_DIALECT_MAP_EXEC.get(_conn.platform_type.lower(), "postgres")
            else:
                defaults["dialect"] = "postgres"
        else:
            defaults["dialect"] = "postgres"

    # Lakehouse: Parquet compression codec picker (DuckDB COPY option). The
    # compression config field lives on the deploy_lakehouse (Run Export) stage;
    # serving_lakehouse_export (Build) is kept here for back-compat.
    if stage_id in ("serving_lakehouse_export", "deploy_lakehouse"):
        result["parquet_compression"] = [
            {"value": "snappy", "label": "Snappy (default)"},
            {"value": "zstd",   "label": "ZSTD (smaller)"},
            {"value": "gzip",   "label": "GZIP"},
            {"value": "uncompressed", "label": "Uncompressed"},
        ]
        defaults = result.setdefault("defaults", {})
        defaults["compression"] = "snappy"

    # Cross-platform transfer: write-disposition picker (Build + Run stages).
    if stage_id in ("serving_transfer", "deploy_transfer"):
        result["write_disposition"] = [
            {"value": "replace", "label": "Replace"},
            {"value": "append",  "label": "Append"},
        ]
        defaults = result.setdefault("defaults", {})
        defaults["write_disposition"] = "replace"

    return result


# ── Phase 5+ follow-up: bulk reset stages affected by an edit ────────────
#
# When a PO edits a deployed product, /edit-diff returns a
# `suggested_rerun_stages` list (stage_ids). The engineer needs those
# specific :StageRun rows reset to 'pending' so the pipeline re-runs.
# Previously this required per-stage curl calls; this endpoint takes the
# list and walks the project's workflows to reset matching rows.

from pydantic import BaseModel as _ResetBaseModel


class ResetStagesByIdInput(_ResetBaseModel):
    """List of stage_ids to reset (e.g. ['data_mapping','serving_virtual_view']).
    Empty list resets nothing (no-op). Matching is exact on stage_id and
    walks every enabled workflow in the project."""
    stage_ids: list[str]


@router.post("/reset-by-id")
def reset_stages_by_id(
    project_id: int,
    body: ResetStagesByIdInput,
    session: Session = Depends(get_session),
):
    """Bulk-reset every :StageRun row whose stage_id is in the input list.

    Walks all workflows (multi-workflow or legacy flat) for the project,
    finds the stage_number for each matching stage_id, then resets the
    corresponding :StageRun. Returns the reset list so the caller can
    surface which stages got reset.
    """
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    target_ids = set(body.stage_ids or [])
    if not target_ids:
        return {"reset": [], "count": 0}

    # Build (workflow_id, stage_number, stage_id) tuples for every enabled
    # stage in every workflow whose stage_id matches the requested set.
    candidates: list[tuple[str | None, int, str]] = []
    if project.multi_workflow:
        wfs = session.exec(select(Workflow).where(Workflow.project_id == project.id)).all()
        for wf in wfs:
            if not wf.workflow_json:
                continue
            stages = [s for s in json_mod.loads(wf.workflow_json) if s.get("enabled", True)]
            for idx, s in enumerate(stages, start=1):
                sid = s.get("stage_id")
                if sid in target_ids:
                    candidates.append((wf.workflow_id, idx, sid))
    elif project.workflow_json:
        stages = [s for s in json_mod.loads(project.workflow_json) if s.get("enabled", True)]
        for idx, s in enumerate(stages, start=1):
            sid = s.get("stage_id")
            if sid in target_ids:
                candidates.append((None, idx, sid))

    reset_rows: list[dict] = []
    for workflow_id, stage_number, stage_id in candidates:
        stage_run = _find_stage_run(session, project_id, stage_number, workflow_id)
        if not stage_run:
            continue
        # Skip rows already pending — no-op + cleaner audit trail.
        if stage_run.status == StageStatus.pending:
            continue
        stage_run.status = StageStatus.pending
        stage_run.started_at = None
        stage_run.completed_at = None
        stage_run.error_message = None
        session.add(stage_run)
        reset_rows.append({
            "workflow_id": workflow_id,
            "stage_number": stage_number,
            "stage_id": stage_id,
        })
    session.commit()
    return {"reset": reset_rows, "count": len(reset_rows)}
