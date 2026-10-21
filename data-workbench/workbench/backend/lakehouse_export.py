"""Lakehouse (Parquet + DuckDB) serving — the export worker.

The third serving path next to virtual views (routers/serving.py) and dbt
materialization (routers/materialization.py). Non-LLM, backend-driven:

  1. compile one DuckDB SELECT per output dataset via the SHARED SQL core
     (generate_view_ddl.generate_lakehouse_models — same compiler as the view
     and dbt emitters),
  2. ATTACH the live source database into DuckDB and run each SELECT via
     `COPY (<body>) TO '<dir>/<physical>.parquet' (FORMAT PARQUET)`,
  3. write a TransferBatch-v1-compatible manifest per file (with reconciliation
     against any prior run) and register a persistent DuckDB catalog view
     (`CREATE OR REPLACE VIEW <physical> AS SELECT * FROM read_parquet(...)`),
  4. verify with a DuckDB + pyarrow dual-engine row-count check (raises on
     mismatch — the same gate `data-profile-parquet` uses),
  5. persist a :ServingDefinition {servingMode:'lakehouse_local'}.

Phase 1 assumes a Postgres or MySQL source reachable via DuckDB's `postgres` /
`mysql` extensions. The canonical type system records lossy/ambiguous warnings
on the exported schema.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from .config import BASE_PROJECT_DIR, SKILLS_DIR
from .neo4j_client import get_driver, neo4j_session

_GV_PATH = SKILLS_DIR / "data-serving-virtual-view" / "scripts" / "generate_view_ddl.py"


def _load_gv():
    """Import generate_view_ddl.py by path (it lives in the skills plugin, not a
    package). Mirrors how generate_dbt_project.py imports it as a sibling."""
    spec = importlib.util.spec_from_file_location("generate_view_ddl", _GV_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_FETCH_PRODUCT_URI = """
MATCH (dc:DataContract {id: $contract_id})-[:MATERIALISES_AS]->(dp:DProdDataProduct)
RETURN dp.uri AS product_uri
"""

# Keyed on (productUri, servingMode) so it coexists with virtual/dbt defs.
_UPSERT_LAKEHOUSE = """
MATCH (dp:DProdDataProduct {uri: $product_uri})
MERGE (dp)-[:SERVED_BY]->(sd:ServingDefinition {productUri: $product_uri, servingMode: 'lakehouse_local'})
SET sd.format         = 'parquet',
    sd.fileUris       = $file_uris,
    sd.manifestUri    = $manifest_uri,
    sd.catalogRef     = $catalog_ref,
    sd.targetDir      = $target_dir,
    sd.summaryJson    = $summary_json,
    sd.buildStatus    = $build_status,
    sd.builtAt        = datetime(),
    sd.builtBy        = $built_by,
    sd.buildError     = $build_error
RETURN sd.servingMode AS serving_mode
"""


def _contract_id(project) -> str:
    return f"{project.project_code}-contract"


def resolve_export_target(project, session) -> Path:
    """Resolve the local DuckDB/Parquet directory for a product's lakehouse.

    Reads the target from the product's MaterializationTarget when its
    target_connection_id points at a `duckdb_local` connection (the `database`
    or extra_config `dir` is the base path). Falls back to a scratch dir under
    the project when unset — today's zero-config default.
    """
    from .models import MaterializationTarget, PlatformConnection

    row = session.get(MaterializationTarget, _contract_id(project))
    if row is not None and row.target_connection_id is not None:
        tconn = session.get(PlatformConnection, row.target_connection_id)
        if tconn is not None and (tconn.platform_type or "").lower() in ("duckdb", "duckdb_local"):
            try:
                extra = json.loads(tconn.extra_config_json or "{}")
            except (TypeError, ValueError):
                extra = {}
            base = extra.get("dir") or tconn.database or tconn.host
            if base:
                return Path(base) / project.project_code
    return Path(BASE_PROJECT_DIR) / project.project_code / "lakehouse"


def _duckdb_attach_source(con, platform: str, dsn: str) -> str:
    """ATTACH the live source into DuckDB and `USE` it so the compiled SELECT's
    bare `"schema"."table"` references resolve. Returns the attach alias."""
    plat = (platform or "postgres").lower()
    alias = "wb_src"
    if plat in ("postgres", "postgresql"):
        con.execute("INSTALL postgres")
        con.execute("LOAD postgres")
        p = urlparse(dsn)
        parts = [
            f"host={p.hostname or 'localhost'}",
            f"port={p.port or 5432}",
            f"dbname={(p.path or '/').lstrip('/')}",
        ]
        if p.username:
            parts.append(f"user={unquote(p.username)}")
        if p.password:
            parts.append(f"password={unquote(p.password)}")
        con.execute(f"ATTACH '{' '.join(parts)}' AS {alias} (TYPE postgres, READ_ONLY)")
        con.execute(f"USE {alias}")
        return alias
    if plat == "mysql":
        con.execute("INSTALL mysql")
        con.execute("LOAD mysql")
        p = urlparse(dsn)
        parts = [
            f"host={p.hostname or 'localhost'}",
            f"port={p.port or 3306}",
            f"database={(p.path or '/').lstrip('/')}",
        ]
        if p.username:
            parts.append(f"user={unquote(p.username)}")
        if p.password:
            parts.append(f"password={unquote(p.password)}")
        con.execute(f"ATTACH '{' '.join(parts)}' AS {alias} (TYPE mysql, READ_ONLY)")
        con.execute(f"USE {alias}")
        return alias
    raise RuntimeError(
        f"Lakehouse export from platform '{platform}' is not supported yet "
        "(Phase 1: Postgres or MySQL source)."
    )


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _type_warnings(arrow_schema, platform: str) -> list[dict]:
    """Record lossy/ambiguous type warnings using the canonical type system."""
    warnings: list[dict] = []
    try:
        from .platform.type_system import get_profile
        profile = get_profile(platform if platform != "postgresql" else "postgres")
    except Exception:
        return warnings
    for field in arrow_schema:
        # Map the arrow type name back through the profile; record any warnings.
        try:
            result = profile.map_to_canonical(str(field.type))
            for w in getattr(result, "warnings", []) or []:
                warnings.append({"column": field.name, "warning": str(w)})
        except Exception:
            continue
    return warnings


def run_lakehouse_export(
    project,
    session,
    *,
    mode: str = "full",
    target_dir: Optional[str] = None,
    compression: str = "snappy",
    sample_limit: int = 100,
    executed_by: str = "engineer",
) -> dict[str, Any]:
    """Run the lakehouse export for a product. Returns a summary dict.

    The DuckDB export itself runs inside the packaged ``run.py`` (subprocess);
    this function compiles the SELECT bodies, assembles the package, invokes the
    runner, and persists the resulting :ServingDefinition."""
    from .routers.connections import build_connection_string
    from .pg_resolver import resolve_source_connection_for_project
    from .stage_execution import ensure_serving_filters_compiled

    contract_id = _contract_id(project)
    started = time.monotonic()

    # 0. Compile any plain-language dataset filter (filterIntent → predicate)
    # before generate_lakehouse_models reads it — the SDK serving stage does this
    # via start_stage_run, but the lakehouse path bypasses it. Raises on an
    # uncompilable filter (surfaced by the router callers).
    ensure_serving_filters_compiled(project, session)

    # 1. product_uri
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        rows = list(ns.run(_FETCH_PRODUCT_URI, contract_id=contract_id))
    if not rows or not rows[0].get("product_uri"):
        raise RuntimeError("No :DProdDataProduct for this contract — run odcs_to_dprod first.")
    product_uri = rows[0]["product_uri"]

    # 2. source connection (borrows via :CONSUMES for dpe-cf)
    platform, conn_ref, _borrowed = resolve_source_connection_for_project(
        project, session, contract_id
    )
    # Uniform across platforms: build_connection_string handles the legacy Postgres
    # DSN, a structured Postgres SourceBinding, and every warehouse from one ref.
    dsn = build_connection_string(platform, conn_ref)
    if not dsn:
        raise RuntimeError("No source connection available for lakehouse export.")

    # 3. compile DuckDB SELECT bodies (shared SQL core)
    gv = _load_gv()
    driver = get_driver(project.neo4j_host, project.neo4j_port,
                        project.neo4j_user, project.neo4j_password)
    try:
        models, model_summary = gv.generate_lakehouse_models(
            driver, project.neo4j_database, product_uri,
            view_schema="public", dialect_name="duckdb",
        )
    finally:
        driver.close()
    if not models:
        raise RuntimeError(f"No lakehouse models generated: {model_summary}")

    # 4. assemble the runnable lakehouse package (parquet lands in <dir>/data/).
    #    The package's own run.py is what performs the export — the same artifact
    #    an engineer downloads and runs; there is no separate internal export path.
    from . import serving_docs, serving_package, serving_runtime

    out_dir = Path(target_dir) if target_dir else resolve_export_target(project, session)
    catalog_path = out_dir / "catalog.duckdb"
    compiled = [{"physical_name": m["physical_name"], "model_name": m["model_name"],
                 "select_body": m["select_body"]} for m in models]
    pkg = serving_package.assemble_lakehouse_package(
        out_dir, project_code=project.project_code, models=compiled,
        product_name=getattr(project, "name", None), source_platform=platform,
        readme_provider=lambda: serving_docs.generate_lakehouse_readme_sync(
            product_name=getattr(project, "name", None) or project.project_code,
            description=getattr(project, "product_idea", "") or "",
            models=compiled, source_platform=platform),
    )

    # 5. run the package's run.py → parquet + TransferBatch v1 manifest + catalog.
    runner_env = {"WB_SOURCE_PLATFORM": platform, "WB_SOURCE_DSN": dsn,
                  "WB_SOURCE_VIEW_SCHEMA": "public"}
    runner_args = ["--mode", "export", "--compression", (compression or "snappy")]
    if mode == "sample":
        runner_args += ["--sample", "--sample-limit", str(int(sample_limit))]
    run = serving_runtime.execute_package_runner(
        pkg, runner_args, env=runner_env, timeout=1800)

    if run.ok:
        file_uris = list(run.metrics.get("file_uris") or [])
        manifest_uris = list(run.metrics.get("manifest_uris") or [])
        catalog_ref = run.metrics.get("catalog_ref") or str(catalog_path)
        per_model = list(run.metrics.get("models") or [])
        build_status, build_error = "built", None
    else:
        file_uris, manifest_uris, per_model = [], [], []
        catalog_ref = str(catalog_path)
        build_status = "failed"
        # Surface the subprocess's stderr/stdout tail — the runner can die (e.g. a
        # native segfault in the DuckDB mysql extension) without writing a
        # structured error, so run.error alone reads as "runner produced no
        # run_result.json". Mirror the /materialize diagnostic pattern.
        raw = (run.error.message if run.error else None) or "lakehouse export failed"
        tail = (run.stderr_tail or "").strip() or (run.stdout_tail or "").strip()
        build_error = f"{raw} | {tail[-500:]}" if tail else raw

    duration_ms = int((time.monotonic() - started) * 1000)
    summary = {
        "models": per_model,
        "model_count": len(per_model),
        "dialect": model_summary.get("dialect") if isinstance(model_summary, dict) else "duckdb",
        "compression": (compression or "snappy").lower(),
        "mode": mode,
        "package_dir": str(out_dir),
    }

    # 6. persist :ServingDefinition
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        ns.run(
            _UPSERT_LAKEHOUSE,
            product_uri=product_uri,
            file_uris=json.dumps(file_uris),
            manifest_uri=json.dumps(manifest_uris),
            catalog_ref=catalog_ref,
            target_dir=str(out_dir),
            summary_json=json.dumps(summary),
            build_status=build_status,
            built_by=executed_by,
            build_error=build_error,
        )

    if build_error is not None:
        raise RuntimeError(f"Lakehouse export failed: {build_error}")

    return {
        "status": "ok",
        "serving_mode": "lakehouse_local",
        "target_dir": str(out_dir),
        "catalog_ref": catalog_ref,
        "file_uris": file_uris,
        "manifest_uris": manifest_uris,
        "duration_ms": duration_ms,
        "summary": summary,
    }


def build_lakehouse_package(
    project,
    session,
    *,
    target_dir: Optional[str] = None,
    compression: str = "snappy",
    executed_by: str = "engineer",
) -> dict[str, Any]:
    """Assemble the runnable lakehouse package (models.json + run.py + query.py +
    README) and persist a :ServingDefinition {buildStatus:'built'} WITHOUT running
    the export against the live source.

    This is the *Build* half of the Configure → Build → Deploy lifecycle: it needs
    nothing from the source database, so it works fully offline and makes the
    downloadable / git-pushable package appear the moment Build completes.
    ``run_lakehouse_export`` (backing ``deploy_lakehouse``) is the *Deploy* half —
    it re-assembles idempotently, then runs the package against the real source.
    """
    from .pg_resolver import resolve_source_connection_for_project
    from .stage_execution import ensure_serving_filters_compiled

    contract_id = _contract_id(project)
    started = time.monotonic()

    # 0. Compile any plain-language dataset filter (filterIntent → predicate)
    # before generate_lakehouse_models reads it (see run_lakehouse_export).
    ensure_serving_filters_compiled(project, session)

    # 1. product_uri
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        rows = list(ns.run(_FETCH_PRODUCT_URI, contract_id=contract_id))
    if not rows or not rows[0].get("product_uri"):
        raise RuntimeError("No :DProdDataProduct for this contract — run odcs_to_dprod first.")
    product_uri = rows[0]["product_uri"]

    # 2. source platform — for dialect + README framing ONLY. No DSN resolved and
    #    no live connection opened; a resolution failure defaults to postgres.
    try:
        platform, _conn_ref, _borrowed = resolve_source_connection_for_project(
            project, session, contract_id
        )
    except Exception:  # noqa: BLE001
        platform = "postgres"

    # 3. compile DuckDB SELECT bodies (shared SQL core — reads Neo4j, not the source)
    gv = _load_gv()
    driver = get_driver(project.neo4j_host, project.neo4j_port,
                        project.neo4j_user, project.neo4j_password)
    try:
        models, model_summary = gv.generate_lakehouse_models(
            driver, project.neo4j_database, product_uri,
            view_schema="public", dialect_name="duckdb",
        )
    finally:
        driver.close()
    if not models:
        raise RuntimeError(f"No lakehouse models generated: {model_summary}")

    # 4. assemble the runnable package (no runner invocation).
    from . import serving_docs, serving_package

    out_dir = Path(target_dir) if target_dir else resolve_export_target(project, session)
    catalog_path = out_dir / "catalog.duckdb"
    compiled = [{"physical_name": m["physical_name"], "model_name": m["model_name"],
                 "select_body": m["select_body"]} for m in models]
    serving_package.assemble_lakehouse_package(
        out_dir, project_code=project.project_code, models=compiled,
        product_name=getattr(project, "name", None), source_platform=platform,
        readme_provider=lambda: serving_docs.generate_lakehouse_readme_sync(
            product_name=getattr(project, "name", None) or project.project_code,
            description=getattr(project, "product_idea", "") or "",
            models=compiled, source_platform=platform),
    )

    summary = {
        "models": [],
        "model_count": len(compiled),
        "dialect": model_summary.get("dialect") if isinstance(model_summary, dict) else "duckdb",
        "compression": (compression or "snappy").lower(),
        "mode": "build",
        "package_dir": str(out_dir),
    }

    # 5. persist :ServingDefinition — the package is built; the export (files) runs
    #    on deploy. Parquet/manifest lists stay empty until then.
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        ns.run(
            _UPSERT_LAKEHOUSE,
            product_uri=product_uri,
            file_uris=json.dumps([]),
            manifest_uri=json.dumps([]),
            catalog_ref=str(catalog_path),
            target_dir=str(out_dir),
            summary_json=json.dumps(summary),
            build_status="built",
            built_by=executed_by,
            build_error=None,
        )

    return {
        "status": "ok",
        "serving_mode": "lakehouse_local",
        "built": True,
        "target_dir": str(out_dir),
        "package_dir": str(out_dir),
        "catalog_ref": str(catalog_path),
        "model_count": len(compiled),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "summary": summary,
    }


def _load_prior_manifest(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _reconcile(prior: Optional[dict], row_count: int, schema_fp: str, checksum: str) -> dict:
    """ReconciliationEvidence-shaped comparison against the prior run. Because a
    product SELECT transforms in-flight, we compare against the *prior export*'s
    post-transform counts, not raw source parity."""
    if not prior:
        return {"has_prior": False}
    prior_rows = prior.get("row_count")
    return {
        "has_prior": True,
        "prior_batch_id": prior.get("batch_id", ""),
        "prior_schema_fingerprint": prior.get("schema_fingerprint", ""),
        "row_count_delta": (row_count - prior_rows) if isinstance(prior_rows, int) else None,
        "schema_changed": prior.get("schema_fingerprint", "") != schema_fp,
        "file_checksum_changed": (
            (prior.get("files") or [{}])[0].get("checksum_sha256", "") != checksum
        ),
    }
