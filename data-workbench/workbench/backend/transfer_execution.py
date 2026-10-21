"""Cross-platform transfer serving — dlt package (Configure → Build → Run).

Serves a data product onto a DIFFERENT platform than its source (source ≠ target,
e.g. MySQL → Databricks). dbt can't span platforms (one connection); this path
uses **dlt**, exactly like the data-migration project type — **no DuckDB**.

Lifecycle (mirrors the other serving modes + migration):
  * ``build_transfer_package`` (the **Build** stage, no live DB) — compiles each
    output dataset's SHAPED SELECT in the SOURCE dialect (so masking/renames/joins
    run at the source, before any row leaves it), writes a ``transfer.json``
    contract, and assembles the runnable, downloadable dlt package.
  * ``run_transfer`` (the **Run/Deploy** stage) — executes the package's ``run.py``
    (``--mode load``) via ``serving_runtime.execute_package_runner``: dlt extracts
    the shaped rows from the source, stages them as Parquet, and loads the target
    (for Databricks/Snowflake it stages Parquet then runs ``COPY INTO``). Data
    Workbench runs the SAME package an engineer can download and run.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .config import BASE_PROJECT_DIR, SKILLS_DIR
from .neo4j_client import get_driver, neo4j_session
from . import serving_package
from . import serving_runtime
from . import transform_preflight

logger = logging.getLogger(__name__)

_GV_PATH = SKILLS_DIR / "data-serving-virtual-view" / "scripts" / "generate_view_ddl.py"

# dlt engine can load these targets; a same-platform target isn't a transfer.
_TRANSFER_TARGETS = frozenset({"postgres", "postgresql", "mysql", "snowflake", "databricks"})

_BUILD_TIMEOUT_S = 1800


def _load_gv():
    spec = importlib.util.spec_from_file_location("generate_view_ddl", _GV_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_FETCH_PRODUCT_URI = """
MATCH (dc:DataContract {id: $contract_id})-[:MATERIALISES_AS]->(dp:DProdDataProduct)
RETURN dp.uri AS product_uri
"""

_UPSERT_TRANSFER = """
MATCH (dp:DProdDataProduct {uri: $product_uri})
MERGE (dp)-[:SERVED_BY]->(sd:ServingDefinition {productUri: $product_uri, servingMode: 'transfer_then_transform'})
SET sd.targetPlatform  = $target_platform,
    sd.targetSchema    = $target_schema,
    sd.targetCatalog   = $target_catalog,
    sd.placement       = $placement,
    sd.manifestUri     = $manifest_uri,
    sd.tablesJson      = $tables_json,
    sd.summaryJson     = $summary_json,
    sd.buildStatus     = $build_status,
    sd.deploymentStatus = $deployment_status,
    sd.builtAt         = datetime(),
    sd.builtBy         = $built_by,
    sd.buildError      = $build_error
RETURN sd.servingMode AS serving_mode
"""


def _contract_id(project) -> str:
    return f"{project.project_code}-contract"


def _configured_namespace(project, session) -> str:
    """The product's configured target namespace (Configure Serving), if any.
    3-level warehouses store it as ``catalog.schema``; 2-level as a bare schema."""
    from .models import MaterializationTarget
    row = session.get(MaterializationTarget, _contract_id(project))
    return (getattr(row, "view_target_namespace", "") or "").strip() if row else ""


def _split_namespace(ns: str, target_platform: str, fallback_db: str) -> tuple[str, str]:
    """(catalog, schema) from a configured namespace, with sensible fallbacks.

    "catalog.schema" → both parts; a bare value → the schema (catalog from the
    connection's database when 3-level). Empty → split the connection database
    (Databricks connections often carry "catalog.schema" in their database field)
    or default to ("", "public")."""
    three_level = (target_platform or "").lower() in ("databricks", "snowflake")
    if ns:
        if "." in ns:
            cat, sch = ns.split(".", 1)
            return cat, sch or "public"
        return (fallback_db.split(".", 1)[0] if three_level and "." in fallback_db else ""), ns
    if three_level and fallback_db:
        if "." in fallback_db:
            cat, sch = fallback_db.split(".", 1)
            return cat, sch or "public"
        return fallback_db, "public"
    return "", "public"


def _resolve_source(project, session):
    """(platform, connection_ref) for the product's source (borrows via :CONSUMES
    for consumer-aligned products). Raises when nothing is reachable."""
    from .pg_resolver import resolve_source_connection_for_project
    from .routers.connections import build_connection_string
    platform, ref, _borrowed = resolve_source_connection_for_project(
        project, session, _contract_id(project)
    )
    # Platform-neutral validity check: build_connection_string handles the legacy
    # Postgres DSN, a structured Postgres SourceBinding, AND every warehouse. The
    # structured ref (host/port/…) rides through to build_runner_env unchanged.
    if not build_connection_string(platform, ref):
        raise RuntimeError("No source connection available for transfer.")
    return platform, ref


def _resolve_target(project, session):
    """(platform, connection_ref) for the transfer TARGET from the product's
    MaterializationTarget → PlatformConnection. Raises when no target connection
    is configured — a cross-platform transfer has no same-instance default."""
    from .routers.connections import resolve_materialization_target_ref

    result = resolve_materialization_target_ref(project.project_code, session)
    if result is None:
        raise RuntimeError(
            "No target connection configured. Set a serving target (Configure "
            "Serving → target connection) before a cross-platform transfer."
        )
    return result


_LANDING_SCHEMA = "wb_landing"


def _placement_inputs(project) -> tuple[str, set[str]]:
    """(chosen_placement, masked_datasets) from the persisted decision.

    Defaults to ``transform_on_extract`` when no decision exists (backwards
    compatible with transfers built before the placement step shipped)."""
    try:
        from . import transform_placement_store
        d = transform_placement_store.load_placement_decision(project)
    except Exception:  # noqa: BLE001 — a missing/broken decision must never block serving
        d = None
    if not d or not d.get("placement"):
        return "transform_on_extract", set()
    decision = d.get("decision") or {}
    masked = {o["dataset"] for o in (decision.get("forced_pre_boundary") or []) if o.get("dataset")}
    return d["placement"], masked


def _etl_datasets(models, write_disposition: str) -> list[dict]:
    """ETL: full shaped SELECT (already in the compiled dialect), no target model."""
    return [{
        "target_table": m["model_name"],
        "physical_name": m["physical_name"],
        "select_sql": m["select_body"],
        "target_model_sql": None,
        "write_disposition": write_disposition,
    } for m in models]


def _compile_datasets(project, product_uri: str, source_platform: str,
                      target_platform: str, write_disposition: str,
                      placement: str, masked_datasets: set[str]) -> tuple[list[dict], list[dict], str, str, list[str]]:
    """Compile the per-dataset transfer plan for the chosen placement.

    Returns ``(datasets, landing_relations, landing_schema, effective_placement, warnings)``.

    * ``transform_on_extract`` — shaped SELECT in the SOURCE dialect, loaded straight
      to the target (no landing, no target model).
    * ``hybrid`` / ``transfer_then_transform`` — land the base relations (projection-
      narrowed + filter-pushed) and run each dataset's shaped SELECT (TARGET dialect)
      as a post-load model reading from the landed relations (`build_elt_plan`).

    **Governance guard:** if ANY masked op is present, the whole product falls back to
    ``transform_on_extract`` — landing base relations raw would move unmasked sensitive
    values across the boundary. **Fail-safe:** any error compiling the ELT split falls
    back to ETL (source-side shaping is always correct, only less efficient)."""
    from .platform.transform_placement import build_elt_plan

    gv = _load_gv()
    driver = get_driver(project.neo4j_host, project.neo4j_port,
                        project.neo4j_user, project.neo4j_password)
    warnings: list[str] = []
    want_elt = placement in ("hybrid", "transfer_then_transform")
    try:
        src_models, model_summary = gv.generate_lakehouse_models(
            driver, project.neo4j_database, product_uri,
            view_schema="public", dialect_name=source_platform,
        )
        tgt_models = None
        if want_elt and not masked_datasets:
            tgt_models, _tgt_summary = gv.generate_lakehouse_models(
                driver, project.neo4j_database, product_uri,
                view_schema="public", dialect_name=target_platform,
            )
            # Phase 6 transform-portability gate: the ELT target compiles against
            # the real target platform, so this is where cross-platform capability
            # bites. Staged via WB_TRANSFORM_ENFORCEMENT ('warn' default logs).
            transform_preflight.raise_if_summary_blocked(_tgt_summary, context="transfer_target")
    finally:
        driver.close()
    if not src_models:
        raise RuntimeError(f"No transfer datasets generated: {model_summary}")

    # ETL path (chosen, or forced by governance masking).
    if not want_elt or masked_datasets:
        if want_elt and masked_datasets:
            warnings.append(
                "Masking present — realized as transform_on_extract so sensitive values "
                "are masked before leaving the source (an ELT push-down would move raw values)."
            )
        return _etl_datasets(src_models, write_disposition), [], _LANDING_SCHEMA, "transform_on_extract", warnings

    # ELT / hybrid split: land base relations, shape on the target.
    try:
        plan = build_elt_plan(tgt_models or [], landing_schema=_LANDING_SCHEMA)
        if not plan.get("landing_relations"):
            raise RuntimeError("no base relations resolved for landing")
        datasets = [{
            "target_table": d["model_name"],
            "physical_name": d["physical_name"],
            "select_sql": None,
            "target_model_sql": d["target_body"],
            "write_disposition": write_disposition,
        } for d in plan["datasets"]]
        return datasets, plan["landing_relations"], _LANDING_SCHEMA, placement, warnings
    except Exception as e:  # noqa: BLE001 — fail SAFE to source-side shaping
        warnings.append(f"Placement split unavailable ({e}); realized as transform_on_extract.")
        return _etl_datasets(src_models, write_disposition), [], _LANDING_SCHEMA, "transform_on_extract", warnings


def _fetch_product_uri(project) -> str:
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        rows = list(ns.run(_FETCH_PRODUCT_URI, contract_id=_contract_id(project)))
    if not rows or not rows[0].get("product_uri"):
        raise RuntimeError("No :DProdDataProduct for this contract — run odcs_to_dprod first.")
    return rows[0]["product_uri"]


def _build_spec(project, session, *, write_disposition: str) -> tuple[dict, str, dict, str, dict]:
    """Compile the transfer.json spec + resolve source/target refs.
    Returns (spec, source_platform, source_ref, target_platform, target_ref)."""
    product_uri = _fetch_product_uri(project)
    source_platform, source_ref = _resolve_source(project, session)
    target_platform, target_ref = _resolve_target(project, session)
    if target_platform not in _TRANSFER_TARGETS:
        raise RuntimeError(
            f"Transfer target platform '{target_platform}' is not supported "
            "(postgres / mysql / snowflake / databricks)."
        )
    catalog, schema = _split_namespace(
        _configured_namespace(project, session), target_platform,
        (target_ref.get("database") or ""),
    )
    placement, masked = _placement_inputs(project)
    datasets, landing_relations, landing_schema, effective_placement, warnings = _compile_datasets(
        project, product_uri, source_platform, target_platform, write_disposition,
        placement, masked,
    )
    spec = {
        "source_platform": source_platform,
        "target_platform": target_platform,
        "target_catalog": catalog,
        "target_schema": schema,
        "write_disposition": write_disposition,
        "placement": effective_placement,
        "requested_placement": placement,
        "landing_schema": landing_schema,
        "landing_relations": landing_relations,
        "placement_warnings": warnings,
        "datasets": datasets,
    }
    return spec, source_platform, source_ref, target_platform, target_ref


def build_transfer_package(project, session, *, write_disposition: str = "replace",
                           executed_by: str = "engineer") -> dict[str, Any]:
    """Build (compile + assemble) the runnable dlt transfer package — no live DB.
    The downloadable / git-pushable package appears the moment this completes;
    the actual load runs on ``run_transfer``."""
    spec, _sp, _sr, target_platform, _tr = _build_spec(
        project, session, write_disposition=write_disposition)

    pkg = serving_package.assemble_transfer_package(
        project_code=project.project_code, spec=spec,
        product_name=getattr(project, "name", None),
    )

    product_uri = _fetch_product_uri(project)
    summary = {
        "datasets": [{"target": f"{spec['target_schema']}.{d['target_table']}"} for d in spec["datasets"]],
        "dataset_count": len(spec["datasets"]),
        "source_platform": spec["source_platform"],
        "target_platform": target_platform,
        "target_namespace": (f"{spec['target_catalog']}.{spec['target_schema']}"
                             if spec["target_catalog"] else spec["target_schema"]),
        "engine": "dlt",
        "placement": spec.get("placement", "transform_on_extract"),
        "placement_warnings": spec.get("placement_warnings", []),
    }
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        ns.run(_UPSERT_TRANSFER, product_uri=product_uri,
               target_platform=target_platform, target_schema=spec["target_schema"],
               target_catalog=spec.get("target_catalog") or "",
               placement=spec.get("placement", "transform_on_extract"),
               manifest_uri="", tables_json=json.dumps([]),
               summary_json=json.dumps(summary), build_status="built",
               deployment_status="pending", built_by=executed_by, build_error=None)
    return {"status": "built", "serving_mode": "transfer_then_transform",
            "package_dir": str(pkg), "summary": summary}


def _runner_env(source_platform, source_ref, target_platform, target_ref,
                catalog: str, schema: str,
                s3_env: Optional[dict[str, str]] = None) -> dict[str, str]:
    env = serving_runtime.build_runner_env("WB_SOURCE", source_platform, source_ref)
    env.update(serving_runtime.build_runner_env("WB_TARGET", target_platform, target_ref))
    # The configured namespace is the explicit engineer choice — it overrides the
    # catalog/schema the connection carried (dataset_name = schema; catalog rides
    # the dlt destination for 3-level warehouses).
    if catalog:
        env["WB_TARGET_CATALOG"] = catalog
    env["WB_TARGET_SCHEMA"] = schema
    if s3_env:
        env.update(s3_env)
    return env


def _coerce_bool(value) -> bool:
    """Truthy coercion that also honours string flags (``"false"``/``"0"``/… → False)."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _s3_staging_env(project, session, run_id: str,
                    target_platform: str = "") -> Optional[dict[str, str]]:
    """Build WB_S3_* env vars so the transfer runner writes Parquet to the bound
    object store. Returns None when no object-store binding is set, the bound
    connection isn't an S3 platform, or bucket resolution fails — in all those
    cases the runner behaves exactly as before (DLT internal temp, no S3).

    Emits ``WB_S3_MODE`` to pick one of two topologies (ADR-14, "Transfer runner:
    reachability-gated staging"):

    * ``"staging"`` — native DLT S3 staging: the target warehouse ``COPY INTO``s
      directly from the bucket (no container-disk intermediate at scale). Chosen
      only when the store is warehouse-reachable (real AWS, or an explicit
      ``extra_config.warehouse_reachable``) AND the target is a warehouse. A cloud
      warehouse cannot read a host-local fixture, so this is never the embedded
      SeaweedFS.
    * ``"artifact"`` — the runner itself PUTs the shaped Parquet to the store as a
      browsable artifact (no STS, no warehouse reachability needed); the load runs
      via the warehouse's own managed staging or a direct relational load. This is
      the fail-safe default (embedded fixture, relational targets, anything
      ambiguous)."""
    from .models import ProjectArtifactStoreBinding, PlatformConnection
    from .object_store_publish import _resolve_bucket, is_object_store_platform
    from .platform.secrets import resolve_secret

    binding = session.get(ProjectArtifactStoreBinding, project.id)
    if binding is None:
        return None
    conn = session.get(PlatformConnection, binding.connection_id)
    if conn is None or not is_object_store_platform(conn.platform_type):
        return None
    if conn.platform_type != "s3":
        # GCS/Azure staging support is deferred; fall back silently.
        return None
    try:
        bucket = _resolve_bucket(binding, conn)
    except Exception:  # noqa: BLE001
        logger.warning("could not resolve bucket for transfer S3 staging — falling back to local staging")
        return None

    prefix = (binding.project_prefix or f"{project.project_code}/").rstrip("/")
    import json as _json
    extra = _json.loads(conn.extra_config_json or "{}")
    host = conn.host or ""
    port = conn.port
    is_aws = host.lower() in ("aws", "s3.amazonaws.com")
    use_ssl = _coerce_bool(extra.get("use_ssl", extra.get("ssl", False)))
    # Runner runs INSIDE the compose network — use the internal endpoint, not the
    # public presign endpoint. No SSL by default for fixture (SeaweedFS/RustFS).
    if host and not is_aws:
        scheme = "https" if use_ssl else "http"
        endpoint_url = f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"
    else:
        endpoint_url = ""   # real AWS — let boto3/s3fs use default AWS endpoints

    try:
        secret = resolve_secret(conn.secret_ref) or ""
    except Exception:  # noqa: BLE001
        secret = ""

    # Reachability gate: can the *target warehouse* read this object store itself?
    # Real AWS always can; a self-hosted store can be flagged reachable explicitly.
    # Everything else (the embedded fixture, any ambiguous endpoint) is treated as
    # unreachable → the safe "artifact" path, which never involves the warehouse.
    reachable = is_aws or _coerce_bool(extra.get("warehouse_reachable", False))
    is_warehouse = (target_platform or "").lower() in ("databricks", "snowflake")
    mode = "staging" if (reachable and is_warehouse) else "artifact"

    return {
        "WB_S3_ENDPOINT_URL": endpoint_url,
        "WB_S3_ACCESS_KEY": conn.username or "",
        "WB_S3_SECRET_KEY": secret,
        "WB_S3_BUCKET": bucket,
        "WB_S3_PROJECT_PREFIX": prefix,
        "WB_S3_RUN_ID": run_id,
        "WB_S3_USE_SSL": "1" if use_ssl else "0",
        "WB_S3_MODE": mode,
    }


def run_transfer(project, session, *, placement: str = "transform_on_extract",
                 write_disposition: str = "replace",
                 target_schema: str = "", executed_by: str = "engineer") -> dict[str, Any]:
    """Execute the transfer: (re)assemble the package and run it (source → Parquet
    → target) via the package's own ``run.py``. Persists the serving definition.

    When the project has a ``ProjectArtifactStoreBinding`` to an S3 connection,
    injects ``WB_S3_*`` env vars so the runner stages Parquet directly to S3 via
    DLT's filesystem destination. On success, records an ``ArtifactPublishRun``
    audit row and returns presigned download URLs (Phase 5 — native S3 staging)."""
    started = time.monotonic()
    spec, source_platform, source_ref, target_platform, target_ref = _build_spec(
        project, session, write_disposition=write_disposition)
    # Explicit override (rare — the UI relies on the configured namespace).
    if target_schema:
        spec["target_schema"] = target_schema

    pkg = serving_package.assemble_transfer_package(
        project_code=project.project_code, spec=spec,
        product_name=getattr(project, "name", None),
    )

    # Generate the run_id HERE so the backend and the runner share the same value.
    # The runner reads WB_S3_RUN_ID and falls back to its own uuid for standalone runs.
    run_id = uuid.uuid4().hex
    s3_env = _s3_staging_env(project, session, run_id, target_platform=target_platform)

    env = _runner_env(source_platform, source_ref, target_platform, target_ref,
                      spec["target_catalog"], spec["target_schema"], s3_env=s3_env)
    run = serving_runtime.execute_package_runner(
        pkg, ["--mode", "load"], env=env, timeout=_BUILD_TIMEOUT_S + 60)

    duration_ms = int((time.monotonic() - started) * 1000)
    tables = list(run.metrics.get("datasets") or [])
    manifest_uris = list(run.metrics.get("manifest_uris") or [])
    build_error = None if run.ok else (run.error.message if run.error else "transfer failed")
    build_status = "deployed" if run.ok else "failed"

    summary = {
        "tables": tables, "table_count": len(tables),
        "source_platform": source_platform, "target_platform": target_platform,
        "target_namespace": (f"{spec['target_catalog']}.{spec['target_schema']}"
                             if spec["target_catalog"] else spec["target_schema"]),
        "engine": "dlt", "placement": spec.get("placement", "transform_on_extract"),
        "placement_warnings": spec.get("placement_warnings", []),
        "duration_ms": duration_ms,
    }
    product_uri = _fetch_product_uri(project)
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        ns.run(_UPSERT_TRANSFER, product_uri=product_uri,
               target_platform=target_platform, target_schema=spec["target_schema"],
               target_catalog=spec.get("target_catalog") or "",
               placement=spec.get("placement", "transform_on_extract"),
               manifest_uri=(manifest_uris[0] if manifest_uris else ""),
               tables_json=json.dumps(tables), summary_json=json.dumps(summary),
               build_status=build_status,
               deployment_status=("deployed" if run.ok else "failed"),
               built_by=executed_by, build_error=build_error)

    if not run.ok:
        raise RuntimeError(f"Transfer failed: {build_error}")

    result: dict[str, Any] = {
        "status": "ok", "serving_mode": "transfer_then_transform",
        "source_platform": source_platform, "target_platform": target_platform,
        "target_schema": spec["target_schema"], "duration_ms": duration_ms,
        "summary": summary,
    }

    # ── Phase 5: record S3 staging as an ArtifactPublishRun + return presigned URLs
    s3_run_prefix = run.metrics.get("s3_run_prefix") or ""
    s3_file_uris = list(run.metrics.get("s3_file_uris") or [])
    if s3_env and s3_run_prefix:
        _record_s3_transfer_run(
            project=project, session=session, run_id=run_id,
            s3_env=s3_env, s3_run_prefix=s3_run_prefix, s3_file_uris=s3_file_uris,
            actor=executed_by,
        )
        presigned = _presign_s3_transfer_files(
            project=project, session=session,
            s3_env=s3_env, s3_file_uris=s3_file_uris,
        )
        result["s3_run_prefix"] = s3_run_prefix
        result["s3_mode"] = s3_env.get("WB_S3_MODE", "")
        result["presigned_urls"] = presigned

    return result


def _record_s3_transfer_run(*, project, session, run_id: str, s3_env: dict[str, str],
                             s3_run_prefix: str, s3_file_uris: list[str],
                             actor: str) -> None:
    """Record a transfer-run S3 staging result as an ArtifactPublishRun audit row.

    Uses the same audit table as the explicit push-to-storage path so
    ``GET /serving/storage-status`` and ``ObjectStoreServingRow`` already show it."""
    from .models import ArtifactPublishRun, ProjectArtifactStoreBinding, PlatformConnection
    try:
        binding = session.get(ProjectArtifactStoreBinding, project.id)
        if binding is None:
            return
        conn = session.get(PlatformConnection, binding.connection_id)
        if conn is None:
            return
        # Store the scheme-less `<prefix>/runs/<run_id>` shape to match the
        # explicit push-to-storage path (object_store_publish), so last_run() /
        # storage-status reads are consistent regardless of which path wrote the row.
        prefix = (binding.project_prefix or f"{project.project_code}/").rstrip("/")
        run_prefix = f"{prefix}/runs/{run_id}"
        session.add(ArtifactPublishRun(
            project_id=project.id,
            connection_id=conn.id,
            run_id=run_id,
            bucket=s3_env.get("WB_S3_BUCKET", ""),
            run_prefix=run_prefix,
            status="succeeded",
            object_count=len(s3_file_uris),
            bytes_uploaded=0,   # size not tracked inline; manifests carry it
            created_by=actor,
            error="",
        ))
        session.commit()
    except Exception:  # noqa: BLE001 — audit is best-effort, never blocks the transfer
        logger.warning("failed to record ArtifactPublishRun for transfer run %s", run_id, exc_info=True)
        session.rollback()


def _presign_s3_transfer_files(*, project, session, s3_env: dict[str, str],
                                s3_file_uris: list[str], ttl: int = 3600) -> dict[str, str]:
    """Generate presigned GET URLs for the transfer's S3 Parquet files.

    Re-fetches the bound connection and builds the presign ref via
    ``object_store_publish._connection_ref`` — the SAME path the explicit
    push-to-storage endpoint uses. That preserves the connection's full
    ``extra_config`` (notably ``public_endpoint_url``), so the provider signs
    against the PUBLIC endpoint and the resulting URLs open in a browser (the
    internal ``seaweedfs:8333`` endpoint the runner writes to is unreachable from
    the host). Best-effort — never raises into the transfer response."""
    from .models import ProjectArtifactStoreBinding, PlatformConnection
    from .object_store_publish import _connection_ref
    from .platform.dispatch import get_object_store_provider
    presigned: dict[str, str] = {}
    if not s3_file_uris:
        return presigned
    bucket = s3_env.get("WB_S3_BUCKET", "")
    if not bucket:
        return presigned
    try:
        binding = session.get(ProjectArtifactStoreBinding, project.id)
        conn = session.get(PlatformConnection, binding.connection_id) if binding else None
        if conn is None:
            return presigned
        provider = get_object_store_provider("s3")
        ref = _connection_ref(conn, bucket)
        for uri in s3_file_uris:
            # uri is "s3://bucket/key" — strip the scheme+bucket prefix to get the key
            prefix = f"s3://{bucket}/"
            if not uri.startswith(prefix):
                continue
            key = uri[len(prefix):]
            try:
                presigned[key] = provider.presign_get(ref, key, bucket=bucket, ttl_seconds=ttl)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001 — presigning is best-effort
        logger.warning("presign failed for transfer run S3 files", exc_info=True)
    return presigned
