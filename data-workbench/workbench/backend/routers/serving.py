"""Deploy and preview endpoints for virtual-view serving.

Pairs with the `deploy_virtual_view` non-LLM stage. The Pipeline.tsx
NON_LLM_ACTIONS entry POSTs to `/api/projects/{id}/serving/deploy`, this
router runs the persisted DDL against the project's PostgreSQL, smoke-
tests every promised view, updates `:ServingDefinition.deployedAt/...`,
and returns success — then the frontend posts to `/stages/{n}/complete`
to flip stage status. Side-effect lives here, not in stages.py, mirroring
the odcs_to_dprod pattern.

Engineer-side preview also lives here. Marketplace-side preview (with
lifecycleState pinning) is in routers/marketplace.py — it shares the
same sql_executor.execute_select path.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Response

from ..authz import require_role
from pydantic import BaseModel
from sqlmodel import Session, select

from .. import deployment_reflection as reflection_engine
from .. import join_preflight
from .. import serving_docs
from .. import serving_package
from .. import serving_runtime
from .. import sql_executor
from .. import transform_preflight
from ..auth import AuthUser, current_user
from ..database import engine, get_session
from ..models import AppSettings, Project
from ..neo4j_client import get_driver, neo4j_session
from .connections import build_connection_string
from ..pg_resolver import (
    resolve_source_connection_for_project,
    resolve_read_connection_for_consumer,
)

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/projects/{project_id}/serving", tags=["serving"])

# Sibling router for the deployment-reflection endpoints. Separate prefix so the
# URL reads as a peer concept ("reflection on the deployment") rather than a
# sub-resource of /serving. main.py registers both.
reflection_router = APIRouter(prefix="/api/projects/{project_id}/reflection", tags=["reflection"])


# ── Helpers ────────────────────────────────────────────────────────────────


def _get_project(project_id: int, session: Session) -> Project:
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def _get_project_for_write(project_id: int, session: Session) -> Project:
    """_get_project + acceptance gate (warn-only by default; 409 when enforced).
    Used by the view-deploy mutation; preview/read paths keep the plain loader."""
    project = _get_project(project_id, session)
    from ..request_guard import guard_rest_mutation
    guard_rest_mutation(project, session)
    return project


def _neo4j(project: Project):
    return neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    )


# Resolve the project's primary :ServingDefinition. A project has at most
# one published :DataContract → one :DProdDataProduct → at most one virtual
# :ServingDefinition. We pin on isCurrent rather than lifecycleState here
# because the engineer-side deploy is allowed before the PO publishes
# (deploy is part of the engineering workflow, publish flips marketplace
# visibility afterwards). The marketplace-side preview endpoint pins on
# published/superseded — see routers/marketplace.py.
_FETCH_SERVING_DEFINITION = """
MATCH (dc:DataContract {id: $contract_id})
WHERE coalesce(dc.isCurrent, true) = true
MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
MATCH (dp)-[:SERVED_BY]->(sd:ServingDefinition {servingMode: 'virtual_view'})
RETURN dp.uri AS product_uri,
       sd.ddl AS ddl,
       sd.viewSchema AS view_schema,
       sd.viewNames AS view_names_json,
       sd.viewName AS primary_view_name,
       sd.targetPlatform AS target_platform,
       coalesce(sd.deploymentStatus, 'pending') AS deployment_status
"""


_SET_DEPLOY_SUCCESS = """
MATCH (sd:ServingDefinition {productUri: $product_uri, servingMode: 'virtual_view'})
SET sd.deployedAt = datetime(),
    sd.deployedBy = $executed_by,
    sd.deployedTo = $view_schema,
    sd.deploymentStatus = 'deployed',
    sd.deployedViewNames = $qualified_view_names_json,
    sd.deploymentError = null,
    sd.deploymentDurationMs = $duration_ms
"""


_SET_DEPLOY_FAILURE = """
MATCH (sd:ServingDefinition {productUri: $product_uri, servingMode: 'virtual_view'})
SET sd.deployedAt = datetime(),
    sd.deployedBy = $executed_by,
    sd.deploymentStatus = 'failed',
    sd.deploymentError = $error_class,
    sd.deploymentErrorMessage = $error_message,
    sd.deploymentDurationMs = $duration_ms
"""


def _parse_view_names(raw: Any) -> list[str]:
    """`:ServingDefinition.viewNames` is persisted as a JSON string."""
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if isinstance(parsed, list):
        return [str(x) for x in parsed if x]
    return []


# ── Models ─────────────────────────────────────────────────────────────────


class DeployBody(BaseModel):
    # Reserved for future: lets the engineer override the schema the DDL
    # is deployed under. v1 deploys to whatever schema the DDL itself
    # qualifies (the serving stage's --view-schema arg, default 'public').
    # Plumbed through but unused at execution time — see _resolve_view_schema.
    view_schema: Optional[str] = None


class PreviewBody(BaseModel):
    dataset_uri: Optional[str] = None
    limit: int = sql_executor.DEFAULT_PREVIEW_LIMIT


def _resolve_view_schema(stored: str | None, override: str | None) -> str:
    """v1: the DDL is fully qualified with whatever schema the serving
    stage emitted, so an override would need a DDL rewrite to be honored.
    For now we accept the override on the API surface for forward-compat
    but always return the stored schema. Future work: rewrite the DDL."""
    return (stored or "public").strip() or "public"


# Helper lives in workbench.backend.pg_resolver (imported above) so the
# marketplace router + reflection engine can borrow the same fallback logic.


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.post("/deploy")
def deploy_virtual_view(
    project_id: int,
    body: DeployBody | None = None,
    session: Session = Depends(get_session),
    user: str = "Data Engineer",
    _role=Depends(require_role("engineer")),
):
    """Execute the persisted :ServingDefinition.ddl against pg_connection.

    Returns 409 if no virtual :ServingDefinition exists (serving stage
    not run) or if pg_connection is empty (project has no data source
    configured). On execution failure the endpoint returns 200 with
    `status='failed'` and an `error_class` — the frontend surfaces the
    error and refuses to mark the stage complete.
    """
    project = _get_project_for_write(project_id, session)
    body = body or DeployBody()

    contract_id = f"{project.project_code}-contract"

    # Resolve where to deploy the view against. Served-location-first: a consumer
    # over a source materialized to a distinct platform (e.g. Databricks) deploys
    # against that served location; else SourceBinding, then the legacy Postgres
    # :CONSUMES origin-borrow.
    _source_platform, _conn_ref, borrowed_from = resolve_read_connection_for_consumer(
        project, session, contract_id
    )
    # One connection contract: the runner env carries the STRUCTURED connection_ref
    # (a Postgres DSN is derived transiently inside build_runner_env). Guard only
    # that a Postgres source actually resolved to a usable connection.
    if _source_platform in ("postgres", "postgresql") and not build_connection_string(
        _source_platform, _conn_ref
    ):
        raise HTTPException(
            409,
            "No source connection available. Source-aligned products: bind a "
            "connection via the data-source step. Consumer-aligned products: this "
            "project must :CONSUMES at least one source product whose own project "
            "has a bound connection.",
        )

    with _neo4j(project) as ns:
        rows = list(ns.run(_FETCH_SERVING_DEFINITION, contract_id=contract_id))
        if not rows:
            raise HTTPException(409, "No virtual-view serving definition found — run the Data Serving stage first")
        row = dict(rows[0])
        product_uri = row["product_uri"]
        ddl = row["ddl"] or ""
        view_schema = _resolve_view_schema(row.get("view_schema"), body.view_schema)
        view_names = _parse_view_names(row.get("view_names_json"))
        if not ddl.strip():
            raise HTTPException(409, "Serving definition has no DDL")
        if not view_names:
            # Fall back to the primary view name if viewNames JSON wasn't
            # populated (older serving definitions pre-multi-view support).
            primary = row.get("primary_view_name")
            if primary:
                view_names = [primary]

        # Phase 6 transform-portability gate: refuse to deploy transforms that
        # won't compile on the RESOLVED target platform (staged via
        # WB_TRANSFORM_ENFORCEMENT). Default 'warn' logs but never blocks; 'block'
        # raises a clean 422 with per-mapping remediation. Platform + schema are
        # resolved exactly like the serving stage + preflight endpoint.
        if transform_preflight.enforcement_enabled():
            from ..stage_execution import _resolve_platform_context
            _ctx = _resolve_platform_context("serving_virtual_view", project, session) or {}
            _gate_driver = get_driver(
                project.neo4j_host, project.neo4j_port, project.neo4j_user, project.neo4j_password
            )
            try:
                _gate_result = transform_preflight.preflight_product(
                    _gate_driver, project.neo4j_database, product_uri,
                    view_schema=(_ctx.get("view_target_namespace") or view_schema),
                    platform=(_ctx.get("dialect") or "postgres"),
                    source_served_map=_ctx.get("source_served_map"),
                )
            finally:
                _gate_driver.close()
            transform_preflight.raise_if_blocked(_gate_result, context="deploy_virtual_view")

        # Assemble the downloadable, runnable view package and deploy by running
        # its OWN run.py — the same artifact an engineer downloads. There is no
        # separate internal deploy path; run_result.json is the feedback we read.
        runner_env = serving_runtime.build_runner_env(
            "WB_TARGET", _source_platform, _conn_ref
        )
        pkg = serving_package.assemble_view_package(
            project.project_code, ddl=ddl, view_schema=view_schema,
            view_names=view_names, platform=_source_platform,
            product_name=getattr(project, "name", None),
            readme_provider=lambda: serving_docs.generate_view_readme_sync(
                product_name=getattr(project, "name", None) or project.project_code,
                description=getattr(project, "product_idea", "") or "",
                platform=_source_platform, view_schema=view_schema,
                view_names=view_names),
        )
        run = serving_runtime.execute_package_runner(
            pkg, ["--apply"], env=runner_env, timeout=300
        )
        duration_ms = run.duration_ms or 0

        # Audit parity with the retired execute_deploy path.
        sql_executor._write_query_run(
            ns, project_code=project.project_code, kind="deploy", text=ddl,
            executed_by=user, status=("deployed" if run.ok else "failed"),
            duration_ms=duration_ms,
            error_class=(None if run.ok else (run.error.cls if run.error else "unknown")),
            product_uri=product_uri, view_schema=view_schema,
        )

        qualified_names = [f"{view_schema}.{n.rsplit('.', 1)[-1]}" for n in view_names]

        if run.ok:
            ns.run(
                _SET_DEPLOY_SUCCESS,
                product_uri=product_uri,
                executed_by=user,
                view_schema=view_schema,
                qualified_view_names_json=json.dumps(qualified_names),
                duration_ms=duration_ms,
            ).consume()
            # The view is now live — reflect the deployment target into an ODCS
            # server entry so the contract is self-describing (no PO input).
            # Best-effort; a derivation hiccup must not fail a good deploy.
            try:
                from ..server_reflect import reflect_server_from_deployment
                reflect_server_from_deployment(
                    ns, session, project, f"{project.project_code}-contract"
                )
            except Exception:
                pass
            # Auto-push serving artifacts to git if enabled (fire-and-forget).
            _maybe_auto_push(project_id, user)
            return {
                "status": "deployed",
                "view_schema": view_schema,
                "qualified_view_names": qualified_names,
                "duration_ms": duration_ms,
                "statements_executed": int(run.metrics.get("statements_executed", 0) or 0),
                "smoke_test_count": int(run.metrics.get("view_count", 0) or 0),
                "borrowed_pg_from": borrowed_from,
                "package_path": str(pkg),
            }
        else:
            err_class = run.error.cls if run.error else "unknown"
            err_msg = run.error.message if run.error else "deploy failed"
            ns.run(
                _SET_DEPLOY_FAILURE,
                product_uri=product_uri,
                executed_by=user,
                error_class=err_class or "unknown",
                error_message=(err_msg or "")[:1000],
                duration_ms=duration_ms,
            ).consume()
            return {
                "status": "failed",
                "error_class": err_class,
                "error_message": err_msg,
                "duration_ms": duration_ms,
            }


def _ensure_view_package(project: Project, session: Session):
    """Assemble the virtual-view serving package on disk from the persisted
    :ServingDefinition DDL, if a DDL exists. Returns the package dir (Path) or
    ``None`` when there's no servable DDL yet (the Data Serving stage hasn't run).

    This is the single on-demand assembly point shared by the download endpoint
    AND the git-push path — so a completed Data Serving stage is downloadable and
    pushable even BEFORE deploy (deploy is what would otherwise first materialise
    the package). Keeps download and push consistent."""
    contract_id = f"{project.project_code}-contract"
    platform, _conn_ref, _borrowed = resolve_read_connection_for_consumer(
        project, session, contract_id)
    with _neo4j(project) as ns:
        rows = list(ns.run(_FETCH_SERVING_DEFINITION, contract_id=contract_id))
    if not rows:
        return None
    row = dict(rows[0])
    ddl = (row.get("ddl") or "").strip()
    if not ddl:
        return None
    view_schema = _resolve_view_schema(row.get("view_schema"), None)
    view_names = _parse_view_names(row.get("view_names_json"))
    if not view_names and row.get("primary_view_name"):
        view_names = [row["primary_view_name"]]
    return serving_package.assemble_view_package(
        project.project_code, ddl=ddl, view_schema=view_schema,
        view_names=view_names, platform=platform,
        product_name=getattr(project, "name", None))


@router.get("/view-package")
def get_view_package(
    project_id: int,
    format: str = "zip",
    session: Session = Depends(get_session),
):
    """Download the self-contained virtual-view deploy package — the same package
    Data Workbench runs to deploy (view.sql + run.py + .env.example + README).
    ``format=zip`` (default) streams a zip; ``format=json`` returns the text files.
    Assembled on demand from the persisted :ServingDefinition DDL."""
    project = _get_project(project_id, session)
    pkg = _ensure_view_package(project, session)
    if pkg is None:
        raise HTTPException(404, "No virtual-view serving definition — run the Data Serving stage first.")
    files = serving_package.collect_package_files(pkg)
    if format == "json":
        return {"project_code": project.project_code,
                "files": {k: (v if isinstance(v, str) else "<binary>")
                          for k, v in files.items()}}
    return serving_package.zip_response(
        files, f"{project.project_code}-view.zip",
        root_prefix=f"{project.project_code}-view")


# ── Git integration ──────────────────────────────────────────────────────────
# Push a data product's serving artifacts (view/dbt/lakehouse code) + OKF docs +
# ODCS spec to a per-product repo (Gitea/GitHub). One repo per product, named
# after project_code; each push is a commit tagged with the contract version.

_SET_GIT_REPO = """
MATCH (sd:ServingDefinition {productUri: $product_uri})
SET sd.gitRepoUrl = $url, sd.gitLastPushedAt = $ts
"""

_READ_CONTRACT_VERSION = """
MATCH (dc:DataContract {id: $contract_id})
RETURN coalesce(dc.currentVersion, 1) AS version
"""

_READ_GIT_STATUS = """
MATCH (sd:ServingDefinition {productUri: $product_uri})
WHERE sd.gitRepoUrl IS NOT NULL
RETURN sd.gitRepoUrl AS repo_url, sd.gitLastPushedAt AS last_pushed_at
ORDER BY sd.gitLastPushedAt DESC
LIMIT 1
"""


class PushToGitBody(BaseModel):
    # Reserved: which package to push ("view"/"dbt"/"lakehouse"). v1 pushes every
    # available artifact — collect_for_git gathers whatever is on disk.
    mode: Optional[str] = None


def _app_settings(session: Session) -> AppSettings:
    return session.exec(select(AppSettings)).first() or AppSettings()


def _push_product_to_git(project: Project, settings: AppSettings,
                         actor_name: str, actor_email: str,
                         session: Optional[Session] = None) -> dict:
    """Core push: assemble the product's files, ensure the repo, commit + tag,
    and record ``gitRepoUrl`` on the :ServingDefinition. Raises GitProviderError
    on a misconfigured/failing provider; HTTP mapping is the caller's job."""
    from ..git_provider import get_provider, browse_url, GitProviderError

    provider = get_provider(settings)  # raises GitProviderError if present-but-invalid
    if provider is None:
        raise GitProviderError("Git integration is not configured (Settings → Git Integration).")

    # Materialise the virtual-view package on demand (same as the download path) so
    # a product with a completed Data Serving stage pushes the full runnable
    # package even before it's deployed. Best-effort — docs/ODCS still push if this
    # can't run (e.g. dbt/lakehouse-only products, or no session/DDL).
    if session is not None:
        try:
            _ensure_view_package(project, session)
        except Exception:
            logger.warning("view-package assembly for git-push failed for %s",
                           project.project_code, exc_info=True)

    # Data-migration (dmig): assemble the runnable migration package on demand from
    # the generated migration.json so a push right after Generate carries the full
    # package (not just the raw spec). Best-effort.
    if getattr(project, "archetype", "") == "dmig":
        try:
            from .migration import _load_spec
            from ..migration_orchestrator import apply_plan_target
            spec = _load_spec(project.project_code)
            if spec:
                apply_plan_target(session, project, spec)
                serving_package.assemble_migration_package(
                    project_code=project.project_code, spec=spec, product_name=project.name)
        except Exception:
            logger.warning("migration-package assembly for git-push failed for %s",
                           project.project_code, exc_info=True)

    # Code-migration (cmig): assemble the old/+new/+conversion package on demand so a
    # push carries the full package even if the Package stage wasn't run first.
    if getattr(project, "archetype", "") == "cmig":
        try:
            serving_package.assemble_code_migration_package(
                project_code=project.project_code, product_name=project.name)
        except Exception:
            logger.warning("code-migration-package assembly for git-push failed for %s",
                           project.project_code, exc_info=True)

    files = serving_package.collect_for_git(project, settings)
    if not files:
        raise GitProviderError(
            "No deployable artifacts found — deploy or materialize the product first.")

    contract_id = f"{project.project_code}-contract"
    product_uri = f"dprod:{contract_id}"

    # Data-migration (dmig) projects have NO ODCS contract — version/tag comes
    # from the MigrationPlanRow's push counter, and the repo URL is recorded on
    # the row (there is no :ServingDefinition to write). Detected by an existing
    # MigrationPlanRow.
    # Thin subsystems (dmig / cmig) have NO ODCS contract — version/tag comes from
    # their own plan row's push counter, and the repo URL is recorded on that row
    # (there is no :ServingDefinition to write).
    mig_row = None
    thin_kind = None
    if session is not None and (getattr(project, "archetype", "") == "dmig"):
        try:
            from ..migration_orchestrator import get_row as _get_mig_row
            mig_row = _get_mig_row(session, project.project_code)
            thin_kind = "migration"
        except Exception:
            mig_row = None
    elif session is not None and (getattr(project, "archetype", "") == "cmig"):
        try:
            from ..code_migration_orchestrator import get_row as _get_cmig_row
            mig_row = _get_cmig_row(session, project.project_code)
            thin_kind = "code-migration"
        except Exception:
            mig_row = None

    version = None
    if mig_row is None:
        try:
            with _neo4j(project) as ns:
                row = ns.run(_READ_CONTRACT_VERSION, contract_id=contract_id).single()
                version = row["version"] if row else None
        except Exception:
            version = None
    else:
        version = mig_row.git_push_count + 1

    tag = f"v{version}" if version else None
    kind = thin_kind or "serving"
    message = (f"chore: publish {project.project_code} {kind} artifacts"
               + (f" (v{version})" if version else ""))
    author = {"name": actor_name or "Data Workbench", "email": actor_email or "workbench@local"}

    org = (settings.git_org or "dataworkbench").strip()
    repo = project.project_code
    provider.ensure_repo(org, repo)
    commit_sha = provider.push_files(org, repo, files, message, tag=tag, author=author)
    repo_url = provider.repo_url(org, repo)

    # Record the repo. Migration → on the MigrationPlanRow (no :ServingDefinition);
    # products → on every :ServingDefinition (best-effort).
    if mig_row is not None and session is not None:
        try:
            mig_row.git_repo_url = repo_url
            mig_row.git_last_pushed_at = datetime.now(timezone.utc)
            mig_row.git_push_count = (mig_row.git_push_count or 0) + 1
            session.add(mig_row)
            session.commit()
        except Exception:
            logger.warning("failed to persist git repo on MigrationPlanRow for %s",
                           project.project_code, exc_info=True)
    else:
        try:
            with _neo4j(project) as ns:
                ns.run(_SET_GIT_REPO, product_uri=product_uri, url=repo_url,
                       ts=datetime.now(timezone.utc).isoformat()).consume()
        except Exception:
            logger.warning("failed to persist gitRepoUrl for %s", project.project_code, exc_info=True)

    # Return the browser-facing URL for the UI; the graph keeps the backend URL.
    return {"repo_url": browse_url(repo_url, settings), "commit_sha": commit_sha, "tag": tag,
            "pushed_files": len(files), "org": org, "repo": repo}


@router.post("/push-to-git")
def push_to_git(
    project_id: int,
    body: PushToGitBody | None = None,
    session: Session = Depends(get_session),
    user: AuthUser = Depends(current_user),
    _role=Depends(require_role("engineer")),
):
    """Push the product's serving artifacts + docs + ODCS spec to the configured
    git repository (one repo per product). Returns the repo URL + commit sha."""
    from ..git_provider import GitProviderError

    project = _get_project(project_id, session)
    settings = _app_settings(session)
    try:
        return _push_product_to_git(project, settings, user.name, user.email, session=session)
    except GitProviderError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001 — surface provider/HTTP failures cleanly
        raise HTTPException(502, f"Git push failed: {exc}")


@router.get("/git-status")
def git_status(project_id: int, session: Session = Depends(get_session)):
    """Whether git is configured + this product's repo URL (if it's been pushed).
    Drives the frontend Push-to-Git / View-in-Git controls."""
    from ..git_provider import browse_url

    project = _get_project(project_id, session)
    settings = _app_settings(session)
    configured = bool(settings.git_provider)
    repo_url = None
    last_pushed_at = None
    if getattr(project, "archetype", "") == "dmig":
        # Migration repo URL lives on the MigrationPlanRow, not a :ServingDefinition.
        try:
            from ..migration_orchestrator import get_row as _get_mig_row
            mrow = _get_mig_row(session, project.project_code)
            if mrow and mrow.git_repo_url:
                repo_url = mrow.git_repo_url
                last_pushed_at = mrow.git_last_pushed_at.isoformat() if mrow.git_last_pushed_at else None
        except Exception:
            pass
    else:
        try:
            with _neo4j(project) as ns:
                row = ns.run(_READ_GIT_STATUS,
                             product_uri=f"dprod:{project.project_code}-contract").single()
                if row:
                    repo_url = row["repo_url"]
                    last_pushed_at = row["last_pushed_at"]
        except Exception:
            pass
    return {"configured": configured, "provider": settings.git_provider,
            "auto_push": bool(getattr(settings, "git_auto_push", False)),
            "repo_url": browse_url(repo_url, settings), "last_pushed_at": last_pushed_at}


# ── object-store publish (ADR-14) ───────────────────────────────────────────────

@router.post("/push-to-storage")
def push_to_storage(
    project_id: int,
    session: Session = Depends(get_session),
    user: AuthUser = Depends(current_user),
    _role=Depends(require_role("engineer")),
):
    """Publish this project's generated data artifacts (Parquet + manifests) to
    its bound object store, under an immutable run prefix (ADR-14). Sibling of
    push-to-git — git carries the recipe (text), the object store carries data.
    Returns the run summary + presigned GET URLs."""
    from .. import object_store_publish

    project = _get_project(project_id, session)
    try:
        return object_store_publish.publish_project_artifacts(project, session, actor=user.name)
    except object_store_publish.PublishError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001 — surface provider failures cleanly
        raise HTTPException(502, f"Object-store publish failed: {exc}")


@router.get("/storage-status")
def storage_status(project_id: int, session: Session = Depends(get_session)):
    """Whether an object-store publish target is configured + the last run.
    Drives the frontend Publish-to-Object-Store control."""
    from .. import object_store_publish
    from ..models import PlatformConnection

    project = _get_project(project_id, session)
    binding = object_store_publish.get_binding(session, project.id)
    if binding is None:
        return {"configured": False, "project_id": project_id}
    conn = session.get(PlatformConnection, binding.connection_id)
    lr = object_store_publish.last_run(session, project.id)
    return {
        "configured": True,
        "project_id": project_id,
        "connection_id": binding.connection_id,
        "connection_name": conn.connection_name if conn else None,
        "platform_type": conn.platform_type if conn else None,
        "bucket": binding.bucket,
        "project_prefix": binding.project_prefix or f"{project.project_code}/",
        "auto_publish": binding.auto_publish,
        "last_run": None if lr is None else {
            "run_id": lr.run_id, "status": lr.status,
            "object_count": lr.object_count, "bytes_uploaded": lr.bytes_uploaded,
            "run_prefix": lr.run_prefix,
            "created_at": lr.created_at.isoformat() if lr.created_at else None,
            "error": lr.error or None,
        },
    }


def _maybe_auto_push(project_id: int, actor_name: str) -> None:
    """Fire-and-forget git push after a successful deploy/build, gated on
    ``git_auto_push``. Runs in a daemon thread with its own Session so a slow or
    failing provider never touches the deploy response."""
    import threading

    def _work():
        try:
            with Session(engine) as s:
                project = s.get(Project, project_id)
                settings = _app_settings(s)
                if not project or not getattr(settings, "git_auto_push", False) \
                        or not settings.git_provider:
                    return
                _push_product_to_git(project, settings, actor_name or "Data Workbench", "",
                                     session=s)
        except Exception:
            logger.warning("auto git-push failed for project %s", project_id, exc_info=True)

    threading.Thread(target=_work, daemon=True).start()


@router.get("/join-preflight")
def join_preflight_check(project_id: int, session: Session = Depends(get_session)):
    """Check that each output dataset's mapped base tables form one FK-connected
    component (so the view-DDL can build a single FROM clause).

    Catches the cross-source-product gap — a consumer mixing tables from two
    source products with no connecting FK — BEFORE serving/materialize fails
    with a no-FK-path error. Each gap carries a ``recommended_joins`` payload
    shaped exactly like the ``PUT /dataset-transform/joins`` body, so the
    engineer can apply the proposed bridge in one click.
    """
    project = _get_project(project_id, session)
    product_uri = f"dprod:{project.project_code}-contract"
    with _neo4j(project) as ns:
        findings = join_preflight.analyze_product_connectivity(ns, product_uri)
    gaps = [f for f in findings if not f.get("connected")]
    return {"product_uri": product_uri, "ok": not gaps, "datasets": findings, "gaps": gaps}


@router.get("/transform-preflight")
def transform_preflight_check(project_id: int, session: Session = Depends(get_session)):
    """Read-only capability preflight for the product's transforms against the
    resolved serving platform (Phase 5 of transform-portability.md).

    Compiles every mapping's transform expression through the shared
    parse→validate→render→re-validate pipeline and returns the aggregated
    `CompileResult` — `errors[]` (with per-mapping `product_col` + `remediation`),
    `warnings[]`, and `used_capabilities[]`. Surfaces the AGE-style
    "unsupported on <platform>" finding at author time instead of at deploy.
    No blocking here — this is diagnostics only; the Phase-6 gate consumes the
    same result.

    Platform + view schema are resolved exactly like the serving stage
    (`_resolve_platform_context`), so the preflight compiles against the same
    dialect the deploy will use (e.g. Databricks for a consumer over a
    materialized source). `validated=false` means validation was SKIPPED — this
    covers BOTH a non-served dialect that genuinely has no capability profile
    (`ansi` / `duckdb`) AND a profiled platform where there was nothing
    compilable to validate yet (no approved/resolvable mappings). It is never
    "clean". `platform_has_profile` tells the two apart for messaging.
    """
    from ..stage_execution import _resolve_platform_context

    project = _get_project(project_id, session)
    product_uri = f"dprod:{project.project_code}-contract"

    ctx = _resolve_platform_context("serving_virtual_view", project, session) or {}
    platform = ctx.get("dialect") or "postgres"
    view_schema = ctx.get("view_target_namespace") or "public"
    served_map = ctx.get("source_served_map")

    driver = get_driver(
        project.neo4j_host, project.neo4j_port, project.neo4j_user, project.neo4j_password
    )
    try:
        result = transform_preflight.preflight_product(
            driver, project.neo4j_database, product_uri,
            view_schema=view_schema, platform=platform, source_served_map=served_map,
        )
    finally:
        driver.close()

    # Does the resolved platform actually have a capability profile? The
    # artifact reader is the sole backend authority on the served-platform set
    # (never imports the skill). Fail-quiet to False so this advisory endpoint
    # never throws on a missing/corrupt artifact.
    from ..platform import transform_capabilities as _tc
    try:
        _profiled = {p.lower() for p in _tc.get_capabilities().platforms}
    except Exception:
        _profiled = set()

    payload = result.to_dict()
    payload.update({
        "product_uri": product_uri,
        "platform": platform,
        "view_schema": view_schema,
        "platform_has_profile": platform.lower() in _profiled,
    })
    return payload


# Look up the deployed view name for a given output dataset. Falls back to
# the first deployed view if no dataset_uri is provided. We MATCH on the
# output dataset and find its corresponding view by matching the physical
# name against the bare component of each deployedViewNames entry.
_RESOLVE_VIEW_QUERY = """
MATCH (dc:DataContract {id: $contract_id})
WHERE coalesce(dc.isCurrent, true) = true
MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
MATCH (dp)-[:SERVED_BY]->(sd:ServingDefinition {servingMode: 'virtual_view'})
WITH dp, sd
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
WITH dp, sd, collect({
    uri: ods.uri,
    physicalName: ods.physicalName,
    description: ods.description,
    relationshipKind: ods.relationshipKind
}) AS datasets
RETURN dp.uri AS product_uri,
       coalesce(sd.deployedTo, sd.viewSchema, 'public') AS view_schema,
       coalesce(sd.deployedViewNames, sd.viewNames, '[]') AS view_names_json,
       sd.viewName AS primary_view_name,
       coalesce(sd.deploymentStatus, 'pending') AS deployment_status,
       datasets
"""

# Per-dataset column descriptions. Returned as a {columnName: description}
# map so the engineer's preview shows the same descriptions surfaced in
# the marketplace and metadata-enrichment review surfaces.
_COLUMN_DESCRIPTIONS_QUERY = """
MATCH (ods:DProdOutputDataset {uri: $dataset_uri})
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
WHERE coalesce(pc.description, '') <> ''
RETURN pc.name AS name, pc.description AS description
"""


def _pick_view_for_dataset(view_names: list[str], primary: str | None,
                           datasets: list[dict], dataset_uri: str | None) -> str | None:
    """Return the bare view name for the requested dataset_uri.

    Resolution order: exact ``vw_<physical>`` match, then fall back to the
    primary view. We deliberately do NOT use suffix matching anymore —
    `endswith("_employee")` mis-routes the `employee` dataset to
    `vw_department_employee` on multi-table products, which the
    deployment reflector caught in Phase 2 smoke-testing. If the exact
    match doesn't fire, the caller's request is unresolvable and we
    surface the primary view rather than guessing."""
    if not view_names and primary:
        return primary
    if dataset_uri and datasets:
        target = next((d for d in datasets if d.get("uri") == dataset_uri), None)
        if target and target.get("physicalName"):
            physical = target["physicalName"]
            expected = f"vw_{physical}"
            for v in view_names:
                bare = v.rsplit(".", 1)[-1]
                if bare == expected:
                    return bare
            # No exact match — return the primary (or first) view rather than
            # a fuzzy-match wrong-view. The marketplace tab will show *some*
            # view, but the engineer can tell from the view name shown that
            # something is off.
    if primary:
        return primary
    if view_names:
        return view_names[0].rsplit(".", 1)[-1]
    return None


@router.post("/preview")
def preview_engineer(
    project_id: int,
    body: PreviewBody | None = None,
    session: Session = Depends(get_session),
):
    """Engineer-side preview against a deployed view.

    Returns `{columns, rows, truncated, durationMs}` or `{error: 'not_deployed'}`
    when the serving definition exists but hasn't been deployed yet.
    """
    project = _get_project(project_id, session)
    body = body or PreviewBody()
    contract_id = f"{project.project_code}-contract"

    # Resolve where to read the preview from. Served-location-first (a consumer
    # over a materialized source previews against the served tables, e.g.
    # Databricks); else non-Postgres SourceBinding / Postgres origin-borrow.
    _preview_platform, _preview_conn_ref, _ = resolve_read_connection_for_consumer(
        project, session, contract_id
    )
    if _preview_platform not in ("postgres", "postgresql"):
        source_dsn = ""
        _preview_conn_json = json.dumps(
            {k: v for k, v in _preview_conn_ref.items() if k != "resolved_password"}
        )
    else:
        source_dsn = build_connection_string(_preview_platform, _preview_conn_ref)
        _preview_conn_json = "{}"
        # NOTE: don't raise "no source connection" here — a cross-platform
        # transfer product has no origin/Postgres connection (its rows live in
        # the served target, e.g. Databricks) and is previewed via
        # _transfer_preview below. The Postgres guard is deferred to the
        # virtual-view path that actually needs pg_connection.

    with _neo4j(project) as ns:
        rows = list(ns.run(_RESOLVE_VIEW_QUERY, contract_id=contract_id))
        if not rows:
            # Materialized (dbt) product — no virtual view. Reuse the
            # marketplace materialized-preview path (same response shape) so the
            # engineer dashboard PreviewTab works for physical tables too.
            from .marketplace import _materialized_preview, _transfer_preview
            mat = _materialized_preview(ns, sql_executor, contract_id, project.project_code, source_dsn, body)
            if mat is not None:
                return mat
            # ... or a cross-platform transfer product (rows live in the target).
            xfer = _transfer_preview(ns, sql_executor, session, project, contract_id, body)
            if xfer is not None:
                return xfer
            raise HTTPException(409, "No serving definition found")
        row = dict(rows[0])
        if row.get("deployment_status") != "deployed":
            return {"error": "not_deployed", "deployment_status": row.get("deployment_status")}

        # Virtual-view preview runs against the Postgres source — this path
        # genuinely needs the connection (the transfer/materialized fallbacks
        # above resolve their own targets and were already tried).
        if _preview_platform in ("postgres", "postgresql") and not source_dsn:
            raise HTTPException(409, "No source connection available for preview")

        view_names = _parse_view_names(row.get("view_names_json"))
        bare_names = [v.rsplit(".", 1)[-1] for v in view_names]
        all_datasets = row.get("datasets") or []
        view = _pick_view_for_dataset(bare_names, row.get("primary_view_name"),
                                       all_datasets, body.dataset_uri)
        if not view:
            raise HTTPException(409, "Could not resolve a deployed view for this dataset")

        # Identify which dataset corresponds to the resolved view so the UI
        # can render the table description + relationshipKind alongside the
        # rows. Match on physicalName since that's how the view name was built.
        picked_physical = view.removeprefix("vw_")
        picked_dataset = next(
            (d for d in all_datasets if d.get("physicalName") == picked_physical),
            None,
        )

        view_schema = row.get("view_schema") or "public"
        # Virtual views are created unquoted → UPPER on Snowflake; fold the read
        # to match (identity on Postgres/MySQL/Databricks). Routes through the
        # shared helper so quoting stays consistent with the marketplace path.
        from ..sql_ident import quote_created_relation
        sql = f"SELECT * FROM {quote_created_relation(view_schema, view, _preview_platform)}"
        result = sql_executor.execute_select(
            neo4j_session=ns,
            project_code=project.project_code,
            pg_connection=source_dsn,
            sql=sql,
            executed_by=f"engineer:{project.project_code}",
            max_rows=max(1, min(int(body.limit or sql_executor.DEFAULT_PREVIEW_LIMIT),
                                sql_executor.DEFAULT_MAX_ROWS)),
            product_uri=row.get("product_uri"),
            view_schema=view_schema,
            platform=_preview_platform,
            connection_json=_preview_conn_json,
            connection_ref=(
                _preview_conn_ref
                if _preview_platform not in ("postgres", "postgresql")
                else None
            ),
        )

        # Every response carries the available_datasets list so the engineer-
        # side PreviewTab can populate its dataset-switcher dropdown without
        # a separate round-trip. (Marketplace mode passes datasets via props.)
        # description / relationshipKind ride along so the dropdown can
        # render the picked-table header without a second lookup.
        available_datasets = [
            {
                "dataset_uri": d.get("uri"),
                "physicalName": d.get("physicalName"),
                "view_name": f"vw_{d['physicalName']}" if d.get("physicalName") else None,
                "description": d.get("description"),
                "relationshipKind": d.get("relationshipKind"),
            }
            for d in all_datasets
            if d.get("physicalName") and f"vw_{d['physicalName']}" in bare_names
        ]

        # Keyed case-insensitively: a Snowflake column comes back UPPER while the
        # :DProdColumn name is logical (lower), so fold both sides to attach the
        # description (no-op on Postgres/Databricks).
        col_descriptions: dict[str, str] = {}
        if result.status == "ok" and picked_dataset and picked_dataset.get("uri"):
            for d in ns.run(
                _COLUMN_DESCRIPTIONS_QUERY,
                dataset_uri=picked_dataset["uri"],
            ):
                col_descriptions[(d["name"] or "").lower()] = d["description"]

        active_dataset = {
            "uri": picked_dataset.get("uri") if picked_dataset else None,
            "physicalName": picked_dataset.get("physicalName") if picked_dataset else None,
            "description": picked_dataset.get("description") if picked_dataset else None,
            "relationshipKind": picked_dataset.get("relationshipKind") if picked_dataset else None,
        }

        if result.status != "ok":
            return {
                "status": "failed",
                "error_class": result.error_class,
                "error_message": result.error_message,
                "duration_ms": result.duration_ms,
                "view_name": view,
                "view_schema": view_schema,
                "available_datasets": available_datasets,
                "active_dataset": active_dataset,
            }
        enriched_columns = [
            {**c, "description": col_descriptions.get((c.get("name") or "").lower()) or None}
            for c in result.columns
        ]
        return {
            "status": "ok",
            "columns": enriched_columns,
            "rows": result.rows,
            "truncated": result.truncated,
            "row_count": result.row_count,
            "duration_ms": result.duration_ms,
            "view_name": view,
            "view_schema": view_schema,
            "available_datasets": available_datasets,
            "active_dataset": active_dataset,
        }


# ── Deployment reflection endpoints ────────────────────────────────────────


class _RunReflectionBody(BaseModel):
    # 'manual' when the engineer clicks Run on the stage; 'pipeline' when
    # automated; 'rerun' when the panel's Re-run button fires.
    trigger: str = "manual"


@reflection_router.post("/run")
async def run_reflection(
    project_id: int,
    body: _RunReflectionBody | None = None,
    session: Session = Depends(get_session),
):
    """Gather inputs → call the deployment-reflector skill → persist
    `:DeploymentReflection`. Always returns 200 with the persisted payload
    even if the LLM call itself fails — the deterministic preview+snapshot
    portion still lands on the graph, and the advisor_error field tells
    the panel why the narrative is empty."""
    project = _get_project(project_id, session)
    body = body or _RunReflectionBody()
    contract_id = f"{project.project_code}-contract"

    # Resolve the connection the deployed artifact lives on using the same
    # served-location-first resolver as deploy/preview — a consumer over a
    # materialized source reflects against the served tables (e.g. Databricks).
    platform_type, conn_ref, _ = resolve_read_connection_for_consumer(
        project, session, contract_id
    )
    reflection_conn_ref = conn_ref
    if platform_type in ("postgres", "postgresql") and not build_connection_string(
        platform_type, conn_ref
    ):
        # A cross-platform transfer product has no Postgres origin — its rows
        # live on the transfer target (e.g. Databricks). Resolve that target
        # so reflection samples the served tables instead of 409'ing.
        from .. import transfer_execution
        try:
            platform_type, reflection_conn_ref = transfer_execution._resolve_target(
                project, session
            )
        except Exception:
            raise HTTPException(409, "No database connection available for reflection")

    try:
        inputs = reflection_engine.gather_inputs(
            project, contract_id,
            platform_type=platform_type,
            connection_ref=reflection_conn_ref,
        )
    except RuntimeError as e:
        raise HTTPException(409, str(e))

    try:
        payload, advisor_error, warnings = await asyncio.wait_for(
            reflection_engine.run_reflector(inputs),
            timeout=reflection_engine.ADVISOR_TIMEOUT_SECONDS + 10,
        )
    except asyncio.TimeoutError:
        # Advisor hung past the skill's own timeout — persist the deterministic
        # part of the report (preview + snapshot were already gathered) so the
        # panel renders something with an explanation.
        from ..deployment_reflection import _parse_advisor_payload  # noqa: PLC0415
        payload = _parse_advisor_payload("")
        advisor_error = f"Reflector timed out after {reflection_engine.ADVISOR_TIMEOUT_SECONDS}s"
        warnings = []

    persisted = reflection_engine.persist_reflection(
        project, contract_id, payload, inputs, advisor_error, body.trigger,
    )

    return {
        "status": "ok",
        "uri": persisted.get("uri"),
        "batch_id": persisted.get("batch_id"),
        "verdict": payload.get("verdict"),
        "narrative": payload.get("narrative"),
        "surprises": payload.get("surprises", []),
        "description_alignment": payload.get("description_alignment", []),
        "rule_alignment": payload.get("rule_alignment", []),
        "qa_alignment": payload.get("qa_alignment", []),
        "recommendations": payload.get("recommendations", []),
        "advisor_error": advisor_error,
        "validation_warnings": warnings,
        "preview_dataset_count": len(inputs.get("preview_rows") or {}),
    }


@reflection_router.get("/latest")
def latest_reflection(
    project_id: int,
    session: Session = Depends(get_session),
):
    """Return the most recent `:DeploymentReflection` for this project,
    or `{reflection: null}` (200) when none exists yet."""
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"
    reflection = reflection_engine.read_latest(project, contract_id)
    return {"reflection": reflection}
