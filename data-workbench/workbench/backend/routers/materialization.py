"""Materialize a data product as physical tables via dbt.

The materialized counterpart to routers/serving.py (virtual views). The
`serving_physical_copy` non-LLM stage's Pipeline.tsx action POSTs to
`/api/projects/{id}/serving/materialize`; this router:

  1. scaffolds a dbt project under <project>/dbt (generate_dbt_project.py,
     which reuses the SAME compiler as virtual-view serving),
  2. runs `dbt build` against the project's PostgreSQL (secrets injected via
     the subprocess env, never written to disk),
  3. records a :MaterializationRun and upserts a
     :ServingDefinition {servingMode:'dbt_materialized'},
  4. returns per-model status + verification row counts.

Phase 1: `mode='full'` (full refresh, dbt target `prod`). `mode='sample'`
caps rows via the dbt `sample_limit` var into a `<schema>_preview` schema —
wired here, exercised by the Phase-2 verification gate.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import psycopg2
from psycopg2.extensions import parse_dsn
from fastapi import APIRouter, Depends, HTTPException, Response

from ..authz import require_role
from pydantic import BaseModel
from sqlmodel import Session

from ..config import BASE_PROJECT_DIR, SKILLS_DIR
from ..database import get_session
from ..models import Project, MaterializationTarget
from ..neo4j_client import neo4j_session
from ..platform.registry import get_registry, CapabilityUnavailable
from .. import serving_docs
from .. import serving_package
from .. import serving_runtime
from .. import transform_preflight


router = APIRouter(prefix="/api/projects/{project_id}/serving", tags=["materialization"])


def _contract_id(project: Project) -> str:
    return f"{project.project_code}-contract"


_PLATFORM_TO_DIALECT = {
    "postgres": "postgres", "postgresql": "postgres", "mysql": "mysql",
    "snowflake": "snowflake", "databricks": "databricks",
}


def resolve_target_dialect(project: Project, session: Session) -> str:
    """The effective SQL dialect the dbt build must target, resolved from the
    PERSISTED serving config — the UI triggers Build/Deploy with no dialect, so
    trusting the request body (which defaults to postgres) silently produced
    postgres-dialect SQL + a "postgres target" README even for MySQL→Databricks.

    Priority: explicit per-product target platform (`MaterializationTarget`) →
    its target connection's platform → a `SourceBinding` dialect override →
    the resolved source platform → postgres.
    """
    from sqlmodel import select
    from ..models import PlatformConnection, SourceBinding

    plat = ""
    row = session.get(MaterializationTarget, _contract_id(project))
    if row and row.platform:
        plat = row.platform
    elif row and row.target_connection_id is not None:
        tconn = session.get(PlatformConnection, row.target_connection_id)
        plat = tconn.platform_type if tconn else ""

    if not plat:
        # Source-is-target: a SourceBinding dialect override wins, else infer the
        # source platform (a legacy Project.pg_connection can point at MySQL/etc.).
        binding = session.exec(
            select(SourceBinding).where(SourceBinding.project_id == project.id)
        ).first()
        if binding and binding.target_dialect:
            return binding.target_dialect.lower()
        try:
            # Served-location-first: a consumer over a source materialized to a
            # distinct platform (e.g. Databricks) has its data on that platform —
            # the dbt target dialect defaults to it, not the MySQL/Postgres origin.
            from ..pg_resolver import resolve_read_connection_for_consumer
            src_plat, _, _ = resolve_read_connection_for_consumer(
                project, session, _contract_id(project)
            )
            plat = src_plat or ""
        except Exception:  # noqa: BLE001 — best-effort; fall through to postgres
            plat = ""

    plat = (plat or "postgres").lower()
    return _PLATFORM_TO_DIALECT.get(plat, plat)


def _guard_not_cross_platform(project: Project, session: Session, target_dialect: str) -> None:
    """Refuse a dbt build/materialize when the target platform differs from the
    source — dbt transforms in-place through ONE connection and cannot move data
    across a platform boundary. The Cross-platform transfer serving mode
    (Extract+Load via DuckDB→dlt) is the correct path. Defense-in-depth behind the
    UI's feasibility gate, and it also protects the MCP path (no dialog)."""
    from ..pg_resolver import resolve_read_connection_for_consumer
    try:
        # Served-location-first: a consumer whose source was materialized to the
        # SAME platform it targets (e.g. Databricks) is NOT cross-platform, so
        # dbt must be allowed — comparing against the origin would wrongly block it.
        src_plat, _, _ = resolve_read_connection_for_consumer(
            project, session, _contract_id(project)
        )
    except Exception:  # noqa: BLE001 — can't resolve source; let downstream handle
        return
    src = _PLATFORM_TO_DIALECT.get((src_plat or "").lower(), (src_plat or "").lower())
    tgt = _PLATFORM_TO_DIALECT.get((target_dialect or "").lower(), (target_dialect or "").lower())
    if src and tgt and src != tgt:
        raise HTTPException(
            422,
            f"dbt materialization can't move data from {src} to a different platform "
            f"({tgt}) — dbt transforms in place through one connection. Use the "
            f"Cross-platform transfer serving mode (Configure Serving → Cross-platform "
            f"transfer), which Extract+Loads {src}→{tgt} before transforming.",
        )


def resolve_materialization_connection(project: Project, session: Session):
    """Resolve the connection dbt should build into, per data product.

    Returns (connection, origin) where origin is 'target' (an explicit
    per-product MaterializationTarget), 'source'/'borrowed:<code>' (the default
    fall-back to the source connection — same-instance behavior), or
    (None, None) when nothing is configured.

    Phase A: the target must contain the source tables/views (dbt reads + writes
    through one connection). Phase B (load_strategy != 'direct') will relax that.
    """
    from .connections import build_connection_string
    row = session.get(MaterializationTarget, _contract_id(project))
    # Registered-connection target (role "target") — resolve to a DSN.
    if row and row.target_connection_id is not None:
        from ..models import PlatformConnection
        from ..platform.secrets import resolve_secret
        tconn = session.get(PlatformConnection, row.target_connection_id)
        if tconn is not None:
            ref = {
                "host": tconn.host, "port": tconn.port, "database": tconn.database,
                "username": tconn.username,
                "resolved_password": resolve_secret(tconn.secret_ref),
            }
            dsn = build_connection_string(tconn.platform_type, ref)
            if dsn:
                return dsn, "target"
    # Legacy inline target: a transient Postgres DSN stored under
    # connection_json["dsn"] (no pg_connection field). Non-Postgres inline targets
    # carry public config in connection_json and are resolved by _dbt_env directly.
    if row and row.connection_json:
        try:
            _cfg = json.loads(row.connection_json)
        except (TypeError, ValueError):
            _cfg = {}
        if _cfg.get("dsn"):
            return _cfg["dsn"], "target"
    # No distinct target → fall back to the source connection (same-instance
    # dbt). Postgres only — a non-Postgres target must use a registered target
    # connection (resolved above).
    from ..pg_resolver import resolve_read_connection_for_consumer
    platform, conn_ref, borrowed_from = resolve_read_connection_for_consumer(
        project, session, _contract_id(project)
    )
    if platform not in ("postgres", "postgresql"):
        return None, None
    dsn = build_connection_string("postgres", conn_ref)
    if not dsn:
        return None, None
    return dsn, (f"borrowed:{borrowed_from}" if borrowed_from else "source")


def _resolve_preview_execution(
    project: Project, session: Session
) -> tuple[str, Optional[dict], str]:
    """Resolve how to READ the built tables for the verification gate — the
    preview + row-count paths — platform-aware.

    Returns ``(platform, connection_ref, pg_connection)``:
      * Postgres → ``("postgres", None, dsn)`` — the psycopg2 fast path is kept.
      * Non-Postgres (Snowflake/Databricks/MySQL) → ``(platform, structured_ref, "")``
        so the read goes through the multi-platform ``sql_executor.execute_select``
        (the same path the marketplace preview uses) instead of psycopg2 — a
        registered Snowflake target otherwise built a ``snowflake://`` DSN that
        ``psycopg2.connect`` can't open, so the gate preview + verification counts
        silently failed.

    Mirrors ``_dbt_env``'s target resolution: registered/inline target first
    (``_resolve_dbt_target_config``), else source-is-target
    (``resolve_source_connection_ref``)."""
    from .connections import resolve_source_connection_ref
    mat = session.get(MaterializationTarget, _contract_id(project))
    platform = ((mat.platform if mat else None) or resolve_target_dialect(project, session) or "postgres").lower()
    if platform in ("postgres", "postgresql"):
        dsn, _ = resolve_materialization_connection(project, session)
        return "postgres", None, dsn or ""

    # Non-Postgres: build a structured connection_ref. Prefer the configured
    # target (registered connection or inline connection_json + ephemeral secret);
    # fall back to the source connection when the product is materialized in-place.
    conn_json, resolved_pw = _resolve_dbt_target_config(mat, platform, session)
    cfg = json.loads(conn_json or "{}")
    if cfg.get("host") or cfg.get("account"):
        ref = {
            "host": cfg.get("host") or cfg.get("account", ""),
            "port": cfg.get("port"),
            "database": cfg.get("database", ""),
            "username": cfg.get("username", ""),
            "resolved_password": resolved_pw,
            "extra_config": {
                k: cfg.get(k, "") for k in ("warehouse", "role", "schema", "http_path", "catalog")
            },
        }
        return platform, ref, ""
    # Source-is-target (no distinct target configured).
    src_platform, src_ref = resolve_source_connection_ref(project, session)
    return (src_platform or platform).lower(), src_ref, ""


_SCAFFOLD_SCRIPT = (
    SKILLS_DIR / "data-serving-virtual-view" / "scripts" / "generate_dbt_project.py"
)
_DBT_BIN = Path(sys.executable).parent / "dbt"

_SCAFFOLD_TIMEOUT_S = 180
_BUILD_TIMEOUT_FULL_S = 1800
_BUILD_TIMEOUT_SAMPLE_S = 300


# ── Cypher ───────────────────────────────────────────────────────────────────

_FETCH_PRODUCT_URI = """
MATCH (dc:DataContract {id: $contract_id})-[:MATERIALISES_AS]->(dp:DProdDataProduct)
RETURN dp.uri AS product_uri
"""

# Upsert the materialized serving definition. Keyed on (productUri, servingMode)
# so it coexists with a virtual_view :ServingDefinition on the same product —
# the engineer can have authored a view and then switched to materialization.
_UPSERT_MATERIALIZED = """
MATCH (dp:DProdDataProduct {uri: $product_uri})
MERGE (dp)-[:SERVED_BY]->(sd:ServingDefinition {productUri: $product_uri, servingMode: 'dbt_materialized'})
SET sd.targetPlatform     = $platform,
    sd.dbtMaterialization  = $materialization,
    sd.targetSchema        = $target_schema,
    sd.targetCatalog       = $target_catalog,
    sd.dbtProjectPath      = $dbt_project_path,
    sd.modelsJson          = $models_json,
    sd.summaryJson         = $summary_json,
    sd.buildStatus         = $build_status,
    sd.builtAt             = datetime(),
    sd.builtBy             = $built_by,
    sd.buildDurationMs     = $duration_ms,
    sd.buildError          = $build_error
RETURN sd.servingMode AS serving_mode
"""

_WRITE_RUN = """
MATCH (p:Project {projectCode: $project_code})
CREATE (mr:MaterializationRun {
    uri: $uri,
    projectCode: $project_code,
    productUri: $product_uri,
    mode: $mode,
    targetSchema: $target_schema,
    materialization: $materialization,
    status: $status,
    durationMs: $duration_ms,
    modelsJson: $models_json,
    error: $error,
    gateApproved: $gate_approved,
    executedBy: $executed_by,
    executedAt: datetime()
})
CREATE (p)-[:HAS_MATERIALIZATION_RUN]->(mr)
"""

# The verification gate's state: the most recent sample run (its status gates
# the full build) and the most recent full run (the durable result). Ordered
# DESC + LIMIT 1 each.
_LATEST_SAMPLE = """
MATCH (p:Project {projectCode: $project_code})-[:HAS_MATERIALIZATION_RUN]->(mr:MaterializationRun {mode: 'sample'})
RETURN mr.status AS status, mr.targetSchema AS schema, mr.modelsJson AS models_json,
       toString(mr.executedAt) AS executed_at, mr.error AS error, mr.uri AS uri
ORDER BY mr.executedAt DESC LIMIT 1
"""

_LATEST_FULL = """
MATCH (p:Project {projectCode: $project_code})-[:HAS_MATERIALIZATION_RUN]->(mr:MaterializationRun {mode: 'full'})
RETURN mr.status AS status, mr.targetSchema AS schema, mr.modelsJson AS models_json,
       toString(mr.executedAt) AS executed_at
ORDER BY mr.executedAt DESC LIMIT 1
"""

# Reject the most recent sample run — flips its status so the gate re-blocks
# the full build until a fresh sample passes.
_REJECT_SAMPLE = """
MATCH (p:Project {projectCode: $project_code})-[:HAS_MATERIALIZATION_RUN]->(mr:MaterializationRun {mode: 'sample'})
WITH mr ORDER BY mr.executedAt DESC LIMIT 1
SET mr.status = 'rejected', mr.rejectReason = $reason, mr.rejectedBy = $rejected_by,
    mr.rejectedAt = datetime()
RETURN mr.uri AS uri
"""


# ── Helpers ──────────────────────────────────────────────────────────────────


def _get_project(project_id: int, session: Session) -> Project:
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def _get_project_for_write(project_id: int, session: Session) -> Project:
    """_get_project + acceptance gate (warn-only by default; 409 when enforced).
    Used by the materialize build, reject, and target CRUD mutations; status
    reads keep the plain loader."""
    project = _get_project(project_id, session)
    from ..request_guard import guard_rest_mutation
    guard_rest_mutation(project, session)
    return project


def _neo4j(project: Project):
    return neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    )


def _assert_dbt_materialize_supported(dialect: str) -> None:
    """Fail fast (HTTP 422) when the resolved target platform has no compatible
    dbt adapter — e.g. MySQL (`dbt_materialized: unsupported`; the only dbt-mysql
    pins dbt-core~=1.7, incompatible with our pinned dbt-core). Without this a
    MySQL target reaches `dbt build` and dies with an opaque adapter error instead
    of a clean, actionable rejection. Reuses the capability registry (ADR-9);
    postgres/postgresql normalise to the `postgres` manifest key (an unnormalised
    `postgresql` would wrongly resolve to UNSUPPORTED)."""
    plat = "postgres" if dialect in ("postgres", "postgresql") else dialect
    try:
        get_registry().assert_usable(plat, "dbt_materialized")
    except CapabilityUnavailable:
        raise HTTPException(
            422,
            f"'{plat}' is not a supported dbt-materialization target "
            f"(no compatible dbt adapter). Choose a virtual-view serving mode, "
            f"or a Postgres/Snowflake/Databricks target.",
        )


def _resolve_dbt_target_config(
    mat_target: Optional[MaterializationTarget], platform: str, session: Session
) -> tuple[str, str]:
    """Return ``(connection_json, resolved_password)`` for ``_dbt_env`` on a
    non-Postgres target.

    Tier-3 repair: a **registered** target connection (``target_connection_id``)
    deliberately clears ``connection_json`` (creds are resolved at run time), which
    starved ``_dbt_env`` of the Snowflake/MySQL/Databricks config → an empty dbt
    profile. When a registered connection is present, build the structured config
    from it — account/user/database + ``extra_config.{warehouse,role,schema,
    http_path,catalog}`` — and resolve the secret HERE (ephemeral, never persisted
    into ``connection_json``). Falls back to the stored ``connection_json`` +
    env-var secret model for the legacy inline-config path.
    """
    if mat_target is None:
        return "{}", ""
    if mat_target.target_connection_id is not None:
        from ..models import PlatformConnection
        from ..platform.secrets import resolve_secret
        tconn = session.get(PlatformConnection, mat_target.target_connection_id)
        if tconn is not None:
            try:
                extra = json.loads(tconn.extra_config_json or "{}")
            except (TypeError, ValueError):
                extra = {}
            # Databricks writes into a Unity Catalog `catalog`; it MUST resolve to a
            # real catalog or _dbt_env falls back to `hive_metastore`, which is
            # disabled on UC-only workspaces (UC_HIVE_METASTORE_DISABLED_EXCEPTION).
            # Prefer an explicit extra_config.catalog, then the declared target
            # namespace's catalog ("catalog.schema" — what the :ServingDefinition
            # records as targetCatalog), then the connection's `database` (a
            # Databricks connection stores its UC catalog there, not extra_config).
            resolved_catalog = extra.get("catalog", "")
            if platform == "databricks" and not resolved_catalog:
                _vtn = (getattr(mat_target, "view_target_namespace", "") or "").strip()
                _vtn_catalog = _vtn.split(".", 1)[0].strip() if _vtn else ""
                resolved_catalog = _vtn_catalog or (tconn.database or "")
            # Snowflake target-DB symmetry (mirrors the databricks catalog ladder):
            # the declared target namespace's DB part (the picker's explicit choice,
            # recorded as :ServingDefinition.targetCatalog) drives WB_DBT_DATABASE →
            # the Snowflake profile `database:`, so the recorded serving location and
            # the physical build agree. Falls back to the connection's database.
            resolved_database = tconn.database or ""
            if platform == "snowflake":
                _vtn = (getattr(mat_target, "view_target_namespace", "") or "").strip()
                _vtn_db = _vtn.split(".", 1)[0].strip() if _vtn else ""
                resolved_database = _vtn_db or (tconn.database or "")
            cfg = {
                "host": tconn.host,
                "port": tconn.port,
                "username": tconn.username,
                "database": resolved_database,
                # Snowflake uses `account` (== host); keep both so the platform
                # branches in _dbt_env find what they need.
                "account": tconn.host,
                "warehouse": extra.get("warehouse", ""),
                "role": extra.get("role", ""),
                "schema": extra.get("schema", ""),
                "http_path": extra.get("http_path", ""),
                "catalog": resolved_catalog,
            }
            return json.dumps(cfg), resolve_secret(tconn.secret_ref)
    return (getattr(mat_target, "connection_json", "{}") or "{}"), ""


def _dbt_env(pg_connection: str, platform: str = "postgres", connection_json: str = "{}",
             resolved_password: str = "") -> dict:
    """Build subprocess env carrying platform credentials to dbt (never written to disk).

    Postgres path: parses the ``pg_connection`` DSN — pre-existing behaviour unchanged.
    Other platforms: reads public config from ``connection_json``. The credential is
    either ``resolved_password`` (already resolved from a registered target
    connection — Tier-3 path) or, for the legacy inline-config path, resolved by
    looking up the ``secret_ref`` env var name stored in that JSON. Credentials are
    therefore NEVER stored in ``connection_json`` itself.
    """
    env = dict(os.environ)
    platform = (platform or "postgres").lower()

    def _secret(cfg: dict) -> str:
        if resolved_password:
            return resolved_password
        secret_ref = cfg.get("secret_ref", "")
        return os.environ.get(secret_ref, "") if secret_ref else ""

    if platform in ("postgres", "postgresql"):
        parsed = parse_dsn(pg_connection) if pg_connection else {}
        env["WB_DBT_HOST"] = parsed.get("host", "localhost")
        env["WB_DBT_PORT"] = str(parsed.get("port", "5432"))
        env["WB_DBT_USER"] = parsed.get("user", "postgres")
        env["WB_DBT_PASSWORD"] = parsed.get("password", "")
        env["WB_DBT_DBNAME"] = parsed.get("dbname", "postgres")

    elif platform == "mysql":
        cfg = json.loads(connection_json or "{}")
        env["WB_DBT_HOST"] = cfg.get("host", "localhost")
        env["WB_DBT_PORT"] = str(cfg.get("port", 3306))
        env["WB_DBT_USER"] = cfg.get("username", "")
        env["WB_DBT_DBNAME"] = cfg.get("database", "")
        env["WB_DBT_PASSWORD"] = _secret(cfg)

    elif platform == "snowflake":
        cfg = json.loads(connection_json or "{}")
        env["WB_DBT_ACCOUNT"] = cfg.get("account", "") or cfg.get("host", "")
        env["WB_DBT_USER"] = cfg.get("username", "")
        env["WB_DBT_DATABASE"] = cfg.get("database", "")
        env["WB_DBT_WAREHOUSE"] = cfg.get("warehouse", "")
        env["WB_DBT_ROLE"] = cfg.get("role", "")
        # Optional SSO/OAuth authenticator (default is PAT/password — no value set).
        _auth = cfg.get("authenticator", "") or os.environ.get("WB_DBT_AUTHENTICATOR", "")
        if _auth:
            env["WB_DBT_AUTHENTICATOR"] = _auth
        env["WB_DBT_PASSWORD"] = _secret(cfg)

    elif platform == "databricks":
        cfg = json.loads(connection_json or "{}")
        env["WB_DBT_HOST"] = cfg.get("host", "")
        env["WB_DBT_HTTP_PATH"] = cfg.get("http_path", "")
        env["WB_DBT_CATALOG"] = cfg.get("catalog", "") or "hive_metastore"
        env["WB_DBT_TOKEN"] = _secret(cfg)

    return env


def _safe_ident(name: str) -> str:
    """Reduce a name to safe SQL-identifier chars. Schema/table names are
    interpolated into DROP/COUNT/SELECT f-strings, so any caller-supplied value
    must pass through here before it touches SQL."""
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in (name or ""))


def _sanitized_schema(project_code: str) -> str:
    return "dp_" + _safe_ident(project_code)


def _count_rows(pg_connection: str, schema: str, tables: list[str],
                *, platform: str = "postgres", connection_ref: Optional[dict] = None,
                ns=None, project_code: str = "") -> dict:
    """Verification: SELECT COUNT(*) per built table. Best-effort.

    Postgres uses a direct psycopg2 read (unchanged). Non-Postgres platforms
    (Snowflake/Databricks/MySQL) dispatch through the multi-platform
    ``sql_executor.execute_select`` — psycopg2 can't open their DSNs. ``ns`` is a
    neo4j session for the executor's audit write; when absent the count is skipped
    (best-effort, never fatal to a build)."""
    plat = (platform or "postgres").lower()
    if plat not in ("postgres", "postgresql"):
        return _count_rows_via_executor(schema, tables, plat, connection_ref, ns, project_code)
    counts: dict = {}
    try:
        conn = psycopg2.connect(pg_connection)
        conn.set_session(readonly=True, autocommit=True)
        try:
            with conn.cursor() as cur:
                for t in tables:
                    try:
                        cur.execute(f'SELECT COUNT(*) FROM "{schema}"."{t}"')
                        counts[t] = cur.fetchone()[0]
                    except Exception as e:  # noqa: BLE001
                        counts[t] = f"error: {e}"
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        return {"_connect_error": str(e)}
    return counts


def _count_rows_via_executor(schema, tables, platform, connection_ref, ns, project_code) -> dict:
    """Per-table COUNT(*) through the multi-platform SELECT executor."""
    from .. import sql_executor
    from ..sql_ident import quote_created_relation
    if ns is None or connection_ref is None:
        return {}
    counts: dict = {}
    for t in tables:
        # dbt-materialized tables are created unquoted → UPPER on Snowflake; fold
        # the read to match (identity on non-upper-folding platforms).
        sql = f"SELECT COUNT(*) AS n FROM {quote_created_relation(schema, t, platform)}"
        r = sql_executor.execute_select(
            neo4j_session=ns, project_code=project_code, pg_connection="", sql=sql,
            executed_by="materialization-gate", max_rows=1,
            platform=platform, connection_ref=connection_ref, view_schema=schema,
        )
        if r.status == "ok" and r.rows:
            counts[t] = r.rows[0][0]
        else:
            counts[t] = f"error: {r.error_message}" if r.status != "ok" else 0
    return counts


def _reconcile_snapshot_targets(pg_connection: str, schema: str, snapshot_models: list[str],
                                *, drop_existing_snapshots: bool = False) -> list[str]:
    """Drop stale relations sitting where a snapshot must land.

    dbt refuses to snapshot over an existing relation that lacks the snapshot
    meta columns (dbt_scd_id/dbt_valid_from/dbt_valid_to) — e.g. a plain table
    left by an earlier full build before the dataset became scd2. Such a table
    carries NO history, so dropping it (letting the first snapshot run seed
    fresh) is safe.

    An EXISTING snapshot (has dbt_scd_id) is normally left untouched so its
    accumulated history is preserved. `drop_existing_snapshots=True` overrides
    that — used for the disposable `<schema>_preview` sample builds, whose
    history is meaningless and whose stale column types (e.g. an `updated_at`
    that was DATETIME in a prior build but is now DATETIMETZ) otherwise make
    dbt's timestamp strategy refuse to snapshot. Returns the names dropped.
    """
    dropped: list[str] = []
    try:
        conn = psycopg2.connect(pg_connection)
        conn.autocommit = True
    except Exception:  # noqa: BLE001
        return dropped
    try:
        with conn.cursor() as cur:
            for m in snapshot_models:
                cur.execute(
                    "SELECT table_type FROM information_schema.tables "
                    "WHERE table_schema=%s AND table_name=%s", (schema, m),
                )
                row = cur.fetchone()
                if not row:
                    continue
                cur.execute(
                    "SELECT 1 FROM information_schema.columns WHERE table_schema=%s "
                    "AND table_name=%s AND column_name='dbt_scd_id'", (schema, m),
                )
                if cur.fetchone() is not None and not drop_existing_snapshots:
                    continue  # already a snapshot — preserve its history
                obj = "VIEW" if row[0] == "VIEW" else "TABLE"
                # NON-CASCADE on purpose (mirrors sql_executor's view-recreate
                # recovery): if a downstream object depends on this relation,
                # let the DROP fail loudly rather than silently destroy it. The
                # caller surfaces the error; the engineer resolves the dependency.
                cur.execute(f'DROP {obj} IF EXISTS "{schema}"."{m}" RESTRICT')
                dropped.append(m)
    finally:
        conn.close()
    return dropped


def _parse_run_results(dbt_dir: Path) -> list[dict]:
    """Read dbt's target/run_results.json into a compact per-model list."""
    rr = dbt_dir / "target" / "run_results.json"
    if not rr.exists():
        return []
    try:
        data = json.loads(rr.read_text())
    except Exception:  # noqa: BLE001
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


_HAS_BUILD = """
MATCH (dc:DataContract {id: $contract_id})-[:MATERIALISES_AS]->(dp:DProdDataProduct)
MATCH (dp)-[:SERVED_BY]->(sd:ServingDefinition {servingMode: 'dbt_materialized'})
WHERE sd.buildStatus = 'built'
RETURN count(sd) AS n
"""


def has_successful_materialization(project: Project) -> bool:
    """True if a full dbt build already succeeded for this product.

    Lets complete_stage skip a redundant `dbt build` when the frontend already
    materialized via POST /serving/materialize before posting /complete, while
    still building for MCP/API callers that complete the stage directly.
    """
    contract_id = f"{project.project_code}-contract"
    with _neo4j(project) as ns:
        row = ns.run(_HAS_BUILD, contract_id=contract_id).single()
        return bool(row and row["n"])


def _latest_sample_passed(ns, project_code: str) -> bool:
    """The verification gate: True iff the most recent sample build succeeded
    (status='built') and hasn't been rejected. A full build is blocked until
    this holds (unless force=True)."""
    row = ns.run(_LATEST_SAMPLE, project_code=project_code).single()
    return bool(row and row["status"] == "built")


def _read_preview(pg_connection: str, schema: str, models: list[str], limit: int = 20,
                  *, platform: str = "postgres", connection_ref: Optional[dict] = None,
                  ns=None, project_code: str = "") -> dict:
    """Read the sample tables for verification: column name+type + first rows.

    Postgres uses a direct psycopg2 read (unchanged — fast, exact COUNT). Non-
    Postgres platforms (Snowflake/Databricks/MySQL) dispatch through the multi-
    platform ``sql_executor.execute_select`` (psycopg2 can't open their DSNs);
    ``row_count`` is then the capped sample count, which the UI already labels
    "(capped)"."""
    plat = (platform or "postgres").lower()
    if plat not in ("postgres", "postgresql"):
        return _read_preview_via_executor(schema, models, limit, plat, connection_ref, ns, project_code)
    out: dict = {}
    try:
        conn = psycopg2.connect(pg_connection)
        conn.set_session(readonly=True, autocommit=True)
    except Exception as e:  # noqa: BLE001
        return {"_connect_error": str(e)}
    try:
        with conn.cursor() as cur:
            for t in models:
                entry: dict = {"columns": [], "rows": [], "row_count": None}
                try:
                    cur.execute(
                        "SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                        (schema, t),
                    )
                    entry["columns"] = [{"name": r[0], "type": r[1]} for r in cur.fetchall()]
                    cur.execute(f'SELECT COUNT(*) FROM "{schema}"."{t}"')
                    entry["row_count"] = cur.fetchone()[0]
                    cur.execute(f'SELECT * FROM "{schema}"."{t}" LIMIT {int(limit)}')
                    entry["rows"] = [
                        [None if v is None else str(v) for v in row] for row in cur.fetchall()
                    ]
                except Exception as e:  # noqa: BLE001
                    entry["error"] = str(e)
                out[t] = entry
    finally:
        conn.close()
    return out


def _read_preview_via_executor(schema, models, limit, platform, connection_ref, ns, project_code) -> dict:
    """Per-model preview (columns + first rows) through the multi-platform SELECT
    executor, for non-Postgres materialization targets."""
    from .. import sql_executor
    from ..sql_ident import quote_created_relation
    if ns is None or connection_ref is None:
        return {"_connect_error": "no target connection available for preview"}
    out: dict = {}
    for t in models:
        entry: dict = {"columns": [], "rows": [], "row_count": None}
        # dbt-materialized tables are created unquoted → UPPER on Snowflake; fold
        # the read to match (identity on non-upper-folding platforms).
        sql = f"SELECT * FROM {quote_created_relation(schema, t, platform)}"
        r = sql_executor.execute_select(
            neo4j_session=ns, project_code=project_code, pg_connection="", sql=sql,
            executed_by="materialization-gate", max_rows=int(limit),
            platform=platform, connection_ref=connection_ref, view_schema=schema,
        )
        if r.status == "ok":
            # execute_select reports column types under `dataType`; the gate UI
            # (PreviewTable) reads `type` — map it.
            entry["columns"] = [{"name": c.get("name"), "type": c.get("dataType")} for c in r.columns]
            entry["rows"] = [[None if v is None else str(v) for v in row] for row in r.rows]
            entry["row_count"] = r.row_count
        else:
            entry["error"] = r.error_message
        out[t] = entry
    return out


def _scaffold_dbt_project(
    project: Project, dbt_dir: Path, product_uri: str, target_schema: str,
    dialect: str, source_view_schema: str, session: Session,
) -> dict:
    """Run generate_dbt_project.py → a scaffolded dbt project on disk (pure
    structure, no secrets, no `dbt build`). Returns the parsed scaffold output
    (`{models, summary}`). Raises HTTPException on failure. Shared by the
    `/materialize` deploy path and the `/dbt/build-package` build path so both
    scaffold identically."""
    # Fail fast on an unsupported dbt target (e.g. MySQL) BEFORE any scaffold /
    # `dbt build` work. This is the single choke point shared by both dbt entry
    # points (/serving/materialize + /serving/dbt/build-package), so one call
    # covers both.
    _assert_dbt_materialize_supported(dialect)
    # Compile any plain-language dataset filter (filterIntent → filterPredicate)
    # BEFORE the scaffold subprocess runs generate_view_ddl — the SDK
    # serving_virtual_view stage does this via start_stage_run's pre-run hook,
    # but the dbt build/deploy paths bypass it. Centralised here so both dbt
    # callers (build_dbt_package + materialize) are covered.
    from ..stage_execution import ensure_serving_filters_compiled
    try:
        ensure_serving_filters_compiled(project, session)
    except ValueError as e:
        raise HTTPException(422, str(e))
    # Resolve each CONSUMES'd upstream's REAL served relation (schema- or
    # catalog.schema-qualified) so the model FROMs the source's actual tables —
    # mirrors what the virtual-view path does (routers/serving.py +
    # stage_execution.py). Without this the dbt scaffold falls back to
    # `{source_view_schema}.vw_<name>` (a Postgres/`public` assumption) which
    # breaks on 3-level platforms (Databricks/Snowflake: no `public`, needs
    # catalog.schema) and on any Postgres upstream served outside `public`.
    source_served_map: dict = {}
    try:
        from ..pg_resolver import resolve_consumed_source_serving
        _loc = resolve_consumed_source_serving(project, session, _contract_id(project))
        if _loc is not None and getattr(_loc, "relations", None):
            source_served_map = dict(_loc.relations)
    except Exception:  # noqa: BLE001 — best-effort; empty map = co-located fallback
        source_served_map = {}
    scaffold_cmd = [
        sys.executable, str(_SCAFFOLD_SCRIPT),
        "--product-uri", product_uri,
        "--project-code", project.project_code,
        "--output-dir", str(dbt_dir),
        "--target-schema", target_schema,
        "--materialization", "table",
        "--source-view-schema", source_view_schema,
        "--source-served-map", json.dumps(source_served_map),
        "--dialect", dialect,
        "--host", project.neo4j_host,
        "--bolt-port", str(project.neo4j_port),
        "--username", project.neo4j_user,
        "--password", project.neo4j_password,
        "--database", project.neo4j_database,
    ]
    try:
        scaffold_proc = subprocess.run(
            scaffold_cmd, capture_output=True, text=True, timeout=_SCAFFOLD_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, f"dbt scaffold timed out after {_SCAFFOLD_TIMEOUT_S}s")
    if scaffold_proc.returncode != 0:
        # Prefer a structured, user-actionable error the scaffold script emits for
        # known modeling problems (ViewGenerationError: no FK path between mapped
        # tables / declare an explicit join, etc.) — surface its `message`
        # verbatim as a 422 rather than an opaque 500 with a truncated traceback.
        err_msg = None
        for line in reversed((scaffold_proc.stdout or "").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except (ValueError, TypeError):
                break
            if isinstance(payload, dict) and payload.get("status") in ("error", "failed"):
                err_msg = payload.get("message") or payload.get("error")
            break
        if err_msg:
            raise HTTPException(422, f"View generation failed: {err_msg}")
        detail = ((scaffold_proc.stdout or "") + (scaffold_proc.stderr or "")).strip()
        raise HTTPException(500, f"dbt scaffold failed: {detail[-1500:]}")
    try:
        return json.loads((scaffold_proc.stdout or "").strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        return {}


class MaterializeBody(BaseModel):
    mode: str = "full"            # 'full' | 'sample'
    sample_limit: int = 1000
    target_schema: Optional[str] = None
    # Empty → resolve the dialect server-side from the persisted serving target
    # (resolve_target_dialect). An explicit value overrides (Phase-7 dialect pick).
    dialect: str = ""
    source_view_schema: str = "public"
    # Verification gate: a full build requires a passed sample first. force=True
    # bypasses the gate (used by complete_stage's MCP/API path, where completing
    # the stage IS the approval gesture).
    force: bool = False


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.post("/materialize")
def materialize(
    project_id: int,
    body: MaterializeBody | None = None,
    session: Session = Depends(get_session),
    user: str = "Data Engineer",
    _role=Depends(require_role("engineer")),
):
    project = _get_project_for_write(project_id, session)
    body = body or MaterializeBody()
    if body.mode not in ("full", "sample"):
        raise HTTPException(400, "mode must be 'full' or 'sample'")

    contract_id = f"{project.project_code}-contract"
    # The connection dbt builds into: an explicit per-product target if set,
    # else the source connection (same-instance default).
    pg_connection, borrowed_from = resolve_materialization_connection(project, session)
    # The UI posts no dialect — resolve from the persisted serving target so the
    # scaffolded SQL / adapter / README / dbt env all agree on the REAL target.
    dialect = body.dialect or resolve_target_dialect(project, session)
    _guard_not_cross_platform(project, session, dialect)
    # For non-Postgres targets pg_connection is empty but connection_json carries
    # the platform config; check both to decide whether a connection is available.
    _mat_target = session.get(MaterializationTarget, _contract_id(project))
    _target_platform = ((_mat_target.platform if _mat_target else None) or dialect or "postgres").lower()
    # Structured dbt config + ephemeral resolved secret. For a registered target
    # connection this is built from the PlatformConnection (Tier-3 repair); for the
    # legacy inline path it's the stored connection_json + env-var secret.
    _conn_json, _resolved_pw = _resolve_dbt_target_config(_mat_target, _target_platform, session)
    _has_non_pg_target = (
        _mat_target is not None
        and _target_platform not in ("postgres", "postgresql")
        and (_conn_json != "{}" or _mat_target.target_connection_id is not None)
    )
    if not pg_connection and not _has_non_pg_target:
        raise HTTPException(
            409,
            "No materialization connection available. Set a per-product target via "
            "the materialize stage, or configure the source connection. Consumer-aligned "
            "products otherwise inherit the :CONSUMES'd source's connection.",
        )

    # Sanitize a caller-supplied schema — it flows into DROP/COUNT/SELECT
    # identifier f-strings downstream (model/table names are already safe-named).
    target_schema = _safe_ident(body.target_schema) if body.target_schema else _sanitized_schema(project.project_code)
    # Target catalog (3-level platforms, e.g. Databricks/Snowflake) for the
    # self-describing :ServingDefinition — lets a downstream consumer resolve WHERE
    # this product's tables live without parsing summaryJson. Empty for 2-level.
    _mt_ns = (getattr(_mat_target, "view_target_namespace", "") or "").strip() if _mat_target else ""
    target_catalog = (
        _mt_ns.split(".", 1)[0]
        if ("." in _mt_ns and dialect in ("databricks", "snowflake")) else ""
    )
    dbt_dir = BASE_PROJECT_DIR / project.project_code / "dbt"
    dbt_dir.mkdir(parents=True, exist_ok=True)

    with _neo4j(project) as ns:
        rows = list(ns.run(_FETCH_PRODUCT_URI, contract_id=contract_id))
        if not rows or not rows[0].get("product_uri"):
            raise HTTPException(
                409,
                "No materialised data product found — run odcs_to_dprod (and the "
                "mapping stage) before materializing.",
            )
        product_uri = rows[0]["product_uri"]

        # Verification gate: a full build requires a passed sample build first,
        # unless explicitly forced. Keeps an engineer from writing a full table
        # before eyeballing capped output.
        if body.mode == "full" and not body.force and not _latest_sample_passed(ns, project.project_code):
            raise HTTPException(
                409,
                "Verification gate: run and approve a sample build before the full "
                "materialization. (POST with mode='sample' first, inspect the preview, "
                "then approve — or pass force=true to bypass.)",
            )

        # 1. Scaffold the dbt project (pure structure, no secrets).
        scaffold_out = _scaffold_dbt_project(
            project, dbt_dir, product_uri, target_schema,
            dialect, body.source_view_schema, session,
        )
        # Phase 6 transform-portability gate: the scaffold summary carries the
        # per-view capability diagnostics; refuse to build when a transform won't
        # compile on the dbt target dialect (staged via WB_TRANSFORM_ENFORCEMENT —
        # 'warn' default logs, 'block' raises a clean 422 with remediation).
        transform_preflight.raise_if_summary_blocked(
            scaffold_out.get("summary"), context="materialize"
        )
        scaffold_models = scaffold_out.get("models", [])
        model_names = [m["model_name"] for m in scaffold_models]
        kind_by_model = {m["model_name"]: m.get("kind", "table") for m in scaffold_models}
        body_by_model = {m["model_name"]: m.get("select_body") for m in scaffold_models}
        summary_json = json.dumps(scaffold_out.get("summary", {}), default=str)

        # 2. dbt build.
        target = "preview" if body.mode == "sample" else "prod"
        build_schema = f"{target_schema}_preview" if body.mode == "sample" else target_schema

        # Reconcile stale non-snapshot relations where snapshots now land
        # (table→snapshot transition) — dbt won't snapshot over a plain table.
        snapshot_models = [n for n in model_names if kind_by_model.get(n) == "snapshot"]
        if snapshot_models and pg_connection:
            # Preview (sample) builds reseed their disposable snapshot relations
            # so a stale column type from a prior build can't block dbt; full
            # builds preserve accumulated history.  Skip for non-Postgres targets
            # where pg_connection is empty — dbt itself handles the snapshot target.
            _reconcile_snapshot_targets(
                pg_connection, build_schema, snapshot_models,
                drop_existing_snapshots=(body.mode == "sample"),
            )
        # Assemble the scaffold into a runnable, downloadable dbt package and
        # build by running its OWN run.py (dbt build + run_result.json) — the same
        # artifact an engineer downloads; there is no separate internal build path.
        serving_package.assemble_dbt_package(
            dbt_dir, project_code=project.project_code, platform=dialect,
            model_names=model_names, product_name=getattr(project, "name", None),
            materialization="table", has_snapshots=bool(snapshot_models),
            # Enrich the README only on the definitive full build — keep the fast
            # sample-gate build snappy (the preserve-existing logic keeps it).
            readme_provider=(
                (lambda: serving_docs.generate_dbt_readme_sync(
                    product_name=getattr(project, "name", None) or project.project_code,
                    description=getattr(project, "product_idea", "") or "",
                    platform=dialect, model_names=model_names,
                    materialization="table", has_snapshots=bool(snapshot_models)))
                if body.mode == "full" else None),
        )
        timeout_s = _BUILD_TIMEOUT_SAMPLE_S if body.mode == "sample" else _BUILD_TIMEOUT_FULL_S
        started = time.time()
        run = serving_runtime.execute_package_runner(
            dbt_dir,
            ["--mode", body.mode, "--target", target,
             "--sample-limit", str(int(body.sample_limit)),
             "--dbt-bin", str(_DBT_BIN), "--timeout", str(timeout_s)],
            env=_dbt_env(pg_connection, _target_platform, _conn_json, resolved_password=_resolved_pw),
            timeout=timeout_s + 60,
        )
        duration_ms = int((time.time() - started) * 1000)

        run_results = list(run.metrics.get("run_results") or [])
        build_ok = run.ok
        timed_out = (not build_ok) and run.error is not None and run.error.cls == "timeout"
        build_status = "built" if build_ok else ("timeout" if timed_out else "failed")
        build_error = None if build_ok else (
            (run.error.message if run.error else None) or "dbt build failed")

        # 3. Verification row counts (full builds, on success). Platform-aware:
        # Postgres reads directly; Snowflake/Databricks/MySQL go through the
        # multi-platform executor (psycopg2 can't open their DSNs).
        row_counts = {}
        if build_ok and body.mode == "full":
            _cnt_plat, _cnt_ref, _cnt_pg = _resolve_preview_execution(project, session)
            if _cnt_pg or _cnt_ref is not None:
                row_counts = _count_rows(
                    _cnt_pg, build_schema, model_names,
                    platform=_cnt_plat, connection_ref=_cnt_ref,
                    ns=ns, project_code=project.project_code,
                )

        models_payload = []
        rr_by_model = {r["model"]: r for r in run_results}
        for name in model_names:
            r = rr_by_model.get(name, {})
            kind = kind_by_model.get(name, "table")
            select_body = body_by_model.get(name)
            # Human-readable DDL for the Serving tab (mirrors the view's CREATE
            # VIEW). For a snapshot the real object is a dbt-managed SCD2 table;
            # show the compiled query with a note rather than a literal CREATE.
            ddl = None
            if select_body:
                if kind == "snapshot":
                    ddl = (f"-- dbt snapshot → SCD2 history table {build_schema}.{name}\n"
                           f"-- (dbt manages dbt_valid_from/to; query below is the snapshot source)\n{select_body}")
                else:
                    ddl = f'CREATE TABLE "{build_schema}"."{name}" AS\n{select_body}'
            models_payload.append({
                "model": name,
                "kind": kind,
                "status": r.get("status"),
                "execution_time": r.get("execution_time"),
                "rows": row_counts.get(name),
                "ddl": ddl,
            })
        models_json = json.dumps(models_payload, default=str)

        # 4. Persist :MaterializationRun + upsert :ServingDefinition.
        ns.run(
            _WRITE_RUN,
            uri=f"matrun:{project.project_code}:{int(started*1000)}:{uuid.uuid4().hex[:8]}",
            project_code=project.project_code,
            product_uri=product_uri,
            mode=body.mode,
            target_schema=build_schema,
            materialization="table",
            status=build_status,
            duration_ms=duration_ms,
            models_json=models_json,
            error=build_error,
            gate_approved=(body.mode == "full"),
            executed_by=user,
        ).consume()

        # Only the full (prod) build updates the durable serving definition;
        # a sample build is a throwaway verification artifact.
        if body.mode == "full":
            ns.run(
                _UPSERT_MATERIALIZED,
                product_uri=product_uri,
                # The resolved dialect (line ~791), NOT body.dialect — the UI
                # posts none (body.dialect defaults to ""), so binding body.dialect
                # here persisted an empty sd.targetPlatform.
                platform=dialect,
                materialization="table",
                target_schema=target_schema,
                target_catalog=target_catalog,
                dbt_project_path=str(dbt_dir),
                models_json=models_json,
                summary_json=summary_json,
                build_status=build_status,
                built_by=user,
                duration_ms=duration_ms,
                build_error=build_error,
            ).consume()

    # Auto-push serving artifacts to git after a successful full build (gated on
    # git_auto_push; fire-and-forget so a slow provider never blocks the build).
    if body.mode == "full" and build_ok:
        try:
            from .serving import _maybe_auto_push
            _maybe_auto_push(project_id, user)
        except Exception:
            pass

    status = "built" if build_ok else "failed"
    return {
        "status": status,
        "mode": body.mode,
        "target_schema": build_schema,
        "models": models_payload,
        "duration_ms": duration_ms,
        "borrowed_pg_from": borrowed_from,
        "error": build_error,
    }


class DbtBuildPackageBody(BaseModel):
    target_schema: Optional[str] = None
    # Empty → resolve server-side from the persisted serving target (the UI posts
    # no dialect). An explicit value overrides.
    dialect: str = ""
    source_view_schema: str = "public"


@router.post("/dbt/build-package")
def build_dbt_package(
    project_id: int,
    body: DbtBuildPackageBody | None = None,
    session: Session = Depends(get_session),
    user: str = "Data Engineer",
    _role=Depends(require_role("engineer")),
):
    """Build (scaffold + assemble) the runnable dbt project on disk WITHOUT running
    `dbt build`.

    The *Build* half of Configure → Build → Deploy for the dbt serving mode: it
    needs no live target connection, so the downloadable / git-pushable dbt project
    appears the moment Build completes. The actual `dbt build` (with the sample→full
    verification gate) runs on Deploy — `/materialize`, backing deploy_physical_copy.
    The package is produced on disk; `GET /serving/dbt-project` streams it (no graph
    :ServingDefinition is written here, so nothing signals a materialized table set
    that doesn't exist yet)."""
    project = _get_project_for_write(project_id, session)
    body = body or DbtBuildPackageBody()
    contract_id = _contract_id(project)
    # The UI posts no dialect — resolve it from the persisted serving target so
    # the scaffolded SQL, dbt adapter, and README all reflect the REAL target
    # (e.g. Databricks), not the postgres default.
    dialect = body.dialect or resolve_target_dialect(project, session)
    _guard_not_cross_platform(project, session, dialect)
    target_schema = (
        _safe_ident(body.target_schema) if body.target_schema
        else _sanitized_schema(project.project_code)
    )
    dbt_dir = BASE_PROJECT_DIR / project.project_code / "dbt"
    dbt_dir.mkdir(parents=True, exist_ok=True)

    with _neo4j(project) as ns:
        rows = list(ns.run(_FETCH_PRODUCT_URI, contract_id=contract_id))
        if not rows or not rows[0].get("product_uri"):
            raise HTTPException(
                409,
                "No materialised data product found — run odcs_to_dprod (and the "
                "mapping stage) before building the dbt project.",
            )
        product_uri = rows[0]["product_uri"]

    scaffold_out = _scaffold_dbt_project(
        project, dbt_dir, product_uri, target_schema,
        dialect, body.source_view_schema, session,
    )
    # Phase 6 transform-portability gate (staged via WB_TRANSFORM_ENFORCEMENT).
    transform_preflight.raise_if_summary_blocked(
        scaffold_out.get("summary"), context="build_dbt_package"
    )
    scaffold_models = scaffold_out.get("models", [])
    model_names = [m["model_name"] for m in scaffold_models]
    kind_by_model = {m["model_name"]: m.get("kind", "table") for m in scaffold_models}
    has_snapshots = any(k == "snapshot" for k in kind_by_model.values())

    serving_package.assemble_dbt_package(
        dbt_dir, project_code=project.project_code, platform=dialect,
        model_names=model_names, product_name=getattr(project, "name", None),
        materialization="table", has_snapshots=has_snapshots,
        readme_provider=(lambda: serving_docs.generate_dbt_readme_sync(
            product_name=getattr(project, "name", None) or project.project_code,
            description=getattr(project, "product_idea", "") or "",
            platform=dialect, model_names=model_names,
            materialization="table", has_snapshots=has_snapshots)),
    )
    return {
        "status": "built",
        "package_dir": str(dbt_dir),
        "models": model_names,
        "model_count": len(model_names),
        "has_snapshots": has_snapshots,
    }


@router.get("/materialization/status")
def materialization_status(
    project_id: int,
    preview_limit: int = 20,
    session: Session = Depends(get_session),
):
    """Verification-gate state for the materialize stage.

    Returns the latest sample + full runs, a derived `gate_state`
    (`no_sample` | `awaiting_approval` | `built` | `rejected` | `sample_failed`),
    and — when a sample passed — a per-model preview (column types + first rows
    + row count) read from the `<schema>_preview` tables so the engineer can
    inspect the capped output before approving the full build.
    """
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"
    # Preview reads the built tables, which live in the materialization target —
    # platform-aware (Postgres DSN fast path; else a structured ref for the
    # multi-platform executor).
    prev_platform, prev_ref, prev_pg = _resolve_preview_execution(project, session)

    with _neo4j(project) as ns:
        sample = ns.run(_LATEST_SAMPLE, project_code=project.project_code).single()
        full = ns.run(_LATEST_FULL, project_code=project.project_code).single()

        sample = dict(sample) if sample else None
        full = dict(full) if full else None

        if not sample:
            gate_state = "no_sample"
        elif sample["status"] == "built":
            gate_state = "awaiting_approval"
        elif sample["status"] == "rejected":
            gate_state = "rejected"
        else:
            gate_state = "sample_failed"
        # A successful full build is terminal ONLY when it's at least as recent as
        # the latest sample — otherwise a newer sample (re-materializing an edited
        # product) means the engineer is mid-gate again and the sample state wins.
        # Neo4j datetime toString() is ISO-8601, so lexical compare matches chrono.
        if full and full["status"] == "built" and (
            not sample or (full.get("executed_at") or "") >= (sample.get("executed_at") or "")
        ):
            gate_state = "built"

        # The preview read stays INSIDE the neo4j session so the non-Postgres
        # executor path has a session for its audit write.
        preview = {}
        if sample and sample["status"] == "built" and (prev_pg or prev_ref is not None):
            try:
                sample_models = json.loads(sample.get("models_json") or "[]")
            except Exception:  # noqa: BLE001
                sample_models = []
            model_names = [m["model"] for m in sample_models]
            kind_by_model = {m["model"]: m.get("kind", "table") for m in sample_models}
            preview = _read_preview(
                prev_pg, sample["schema"], model_names, limit=preview_limit,
                platform=prev_platform, connection_ref=prev_ref,
                ns=ns, project_code=project.project_code,
            )
        for name, entry in preview.items():
            entry["kind"] = kind_by_model.get(name, "table")

    return {
        "gate_state": gate_state,
        "sample": sample,
        "full": full,
        "preview": preview,
    }


class RejectBody(BaseModel):
    reason: str = ""


@router.post("/materialization/reject")
def reject_sample(
    project_id: int,
    body: RejectBody | None = None,
    session: Session = Depends(get_session),
    user: str = "Data Engineer",
):
    """Reject the latest sample build — re-blocks the full build until a fresh
    sample passes. The engineer typically rejects, fixes mappings/transforms,
    then re-samples."""
    project = _get_project_for_write(project_id, session)
    body = body or RejectBody()
    with _neo4j(project) as ns:
        row = ns.run(
            _REJECT_SAMPLE,
            project_code=project.project_code,
            reason=(body.reason or "")[:1000],
            rejected_by=user,
        ).single()
        if not row:
            raise HTTPException(409, "No sample build to reject — run a sample build first.")
    return {"status": "rejected", "uri": row["uri"]}


# ── Per-product materialization target connection ───────────────────────────
#
# Mirrors the discovery data-source prompt (routers/projects.py:set_data_source),
# but stored per data product (keyed by contract_id) and used as the connection
# dbt materializes into. When unset, materialization falls back to the source
# connection (resolve_materialization_connection) — today's same-instance default.

class MaterializationTargetInput(BaseModel):
    # Preferred path: point at a registered PlatformConnection (role "target").
    # When set, the DSN form fields below are ignored — platform + config are
    # read from the connection. This is the serving target for ALL modes.
    target_connection_id: Optional[int] = None
    platform: str = "postgres"
    # Postgres / MySQL
    host: str = ""
    port: int = 5432
    database: str = ""
    username: str = ""
    password: str = ""   # Postgres only — stored in DSN (pre-existing); non-PG use secret_ref
    # Snowflake-specific
    account: str = ""    # Snowflake account identifier, e.g. "myorg-myaccount"
    warehouse: str = ""
    role: str = ""
    # Databricks-specific
    http_path: str = ""  # e.g. "/sql/1.0/warehouses/abc123"
    catalog: str = ""
    # Non-Postgres credential reference (env var name, never the value)
    secret_ref: str = ""


@router.get("/materialization-target")
def get_materialization_target(project_id: int, session: Session = Depends(get_session)):
    """Return the per-product target as form fields, plus the effective default.

    `configured` is False when no explicit target is set — the UI then shows the
    source connection as the (default) materialization destination.
    """
    project = _get_project(project_id, session)
    row = session.get(MaterializationTarget, _contract_id(project))
    _src, origin = resolve_materialization_connection(project, session)
    _conn_json = getattr(row, "connection_json", "{}") or "{}"
    try:
        _cfg = json.loads(_conn_json)
    except (TypeError, ValueError):
        _cfg = {}
    _pg_dsn = _cfg.get("dsn", "")  # transient Postgres inline DSN (no pg_connection field)
    _has_non_pg = bool(row) and _conn_json != "{}" and not _pg_dsn
    _tconn_id = getattr(row, "target_connection_id", None) if row else None
    out = {
        "configured": bool(row and (_conn_json != "{}" or _tconn_id is not None)),
        "effective_origin": origin,
        "target_connection_id": _tconn_id,
    }
    # Registered-connection target — return its derived platform + dialect.
    if _tconn_id is not None:
        from ..models import PlatformConnection
        from ..stage_execution import _PLATFORM_DIALECT_MAP
        tconn = session.get(PlatformConnection, _tconn_id)
        if tconn is not None:
            out["target"] = {
                "platform": tconn.platform_type,
                "connection_name": tconn.connection_name,
                "host": tconn.host,
                "database": tconn.database,
                "dialect": _PLATFORM_DIALECT_MAP.get(tconn.platform_type.lower(), "ansi"),
            }
        return out
    if _pg_dsn:
        from urllib.parse import urlparse, unquote
        try:
            p = urlparse(_pg_dsn)
            out["target"] = {
                "platform": (p.scheme or "postgres").replace("postgresql", "postgres"),
                "host": p.hostname or "",
                "port": p.port or 5432,
                "database": (p.path or "/").lstrip("/"),
                "username": unquote(p.username or ""),
                "password": "",  # never returned; re-enter in form
            }
        except Exception:  # noqa: BLE001
            out["target"] = None
    elif _has_non_pg:
        try:
            cfg = _cfg
            platform = (row.platform or "postgres").lower()
            target: dict = {"platform": platform, "secret_ref": cfg.get("secret_ref", "")}
            if platform == "snowflake":
                target.update({
                    "account": cfg.get("account", ""),
                    "username": cfg.get("username", ""),
                    "database": cfg.get("database", ""),
                    "warehouse": cfg.get("warehouse", ""),
                    "role": cfg.get("role", ""),
                })
            elif platform == "databricks":
                target.update({
                    "host": cfg.get("host", ""),
                    "http_path": cfg.get("http_path", ""),
                    "catalog": cfg.get("catalog", "hive_metastore"),
                })
            elif platform == "mysql":
                target.update({
                    "host": cfg.get("host", ""),
                    "port": cfg.get("port", 3306),
                    "username": cfg.get("username", ""),
                    "database": cfg.get("database", ""),
                })
            out["target"] = target
        except Exception:  # noqa: BLE001
            out["target"] = None
    return out


@router.put("/materialization-target")
def set_materialization_target(
    project_id: int,
    body: MaterializationTargetInput,
    session: Session = Depends(get_session),
):
    """Persist a per-product serving target connection (all modes)."""
    project = _get_project_for_write(project_id, session)

    # Preferred path: a registered PlatformConnection (role "target").
    if body.target_connection_id is not None:
        from ..models import PlatformConnection
        tconn = session.get(PlatformConnection, body.target_connection_id)
        if tconn is None:
            raise HTTPException(404, f"PlatformConnection {body.target_connection_id} not found")
        try:
            roles = json.loads(tconn.connection_roles_json or '["source"]')
        except (TypeError, ValueError):
            roles = ["source"]
        if "target" not in roles:
            raise HTTPException(
                400,
                f"Connection '{tconn.connection_name}' is not registered as a target "
                "(add \"target\" to its connection_roles).",
            )
        cid = _contract_id(project)
        row = session.get(MaterializationTarget, cid)
        if row is None:
            row = MaterializationTarget(contract_id=cid)
        row.target_connection_id = tconn.id
        row.platform = tconn.platform_type
        # Inline config left empty — the connection is resolved at run time.
        row.connection_json = "{}"
        row.updated_at = datetime.utcnow()
        session.add(row)
        session.commit()
        return {
            "status": "saved",
            "configured": True,
            "target_connection_id": tconn.id,
            "platform": tconn.platform_type,
        }

    platform = (body.platform or "postgres").lower()

    conn_json = "{}"

    if platform in ("postgres", "postgresql"):
        from urllib.parse import quote
        user = quote(body.username, safe="")
        password = quote(body.password, safe="")
        dsn = f"postgresql://{user}:{password}@{body.host.strip()}:{body.port}/{body.database.strip()}"
        # A transient Postgres DSN under connection_json["dsn"] — no pg_connection
        # shape. resolve_materialization_connection reads it back.
        conn_json = json.dumps({"dsn": dsn})

    elif platform == "mysql":
        cfg: dict = {
            "host": body.host.strip(),
            "port": body.port,
            "username": body.username,
            "database": body.database.strip(),
        }
        if body.secret_ref:
            cfg["secret_ref"] = body.secret_ref
        conn_json = json.dumps(cfg)

    elif platform == "snowflake":
        if not body.account.strip():
            raise HTTPException(400, "Snowflake target requires 'account' (e.g. 'myorg-myaccount').")
        cfg = {
            "account": body.account.strip(),
            "username": body.username,
            "database": body.database.strip(),
            "warehouse": body.warehouse.strip(),
            "role": body.role,
        }
        if body.secret_ref:
            cfg["secret_ref"] = body.secret_ref
        conn_json = json.dumps(cfg)

    elif platform == "databricks":
        if not body.http_path.strip():
            raise HTTPException(400, "Databricks target requires 'http_path' (e.g. '/sql/1.0/warehouses/…').")
        cfg = {
            "host": body.host.strip(),
            "http_path": body.http_path.strip(),
            "catalog": body.catalog or "hive_metastore",
        }
        if body.secret_ref:
            cfg["secret_ref"] = body.secret_ref
        conn_json = json.dumps(cfg)

    else:
        raise HTTPException(
            400,
            f"Unsupported platform '{platform}'. Supported: postgres, mysql, snowflake, databricks.",
        )

    cid = _contract_id(project)
    row = session.get(MaterializationTarget, cid)
    if row is None:
        row = MaterializationTarget(contract_id=cid)
    row.connection_json = conn_json
    row.platform = platform
    row.target_connection_id = None  # inline path clears any registered-connection pointer
    row.updated_at = datetime.utcnow()
    session.add(row)
    session.commit()
    return {"status": "saved", "configured": True}


class ViewTargetNamespaceInput(BaseModel):
    view_target_namespace: str = ""


@router.get("/target-namespace-options")
def get_target_namespace_options(
    project_id: int,
    target_connection_id: int | None = None,
    session: Session = Depends(get_session),
):
    """Enumerate the deploy target's container namespaces so the UI can offer a
    picker instead of free-text. Namespace-model-driven: only 3-level platforms
    (a `catalog` container part, e.g. Databricks) return options; 2-level
    platforms report `supported: false`. Best-effort — a probe failure returns
    empty `catalogs` and the UI falls back to the free-text field.

    When ``target_connection_id`` is supplied (the engineer picked a separate
    target connection in Configure Serving), probe THAT connection — mirrors the
    migration flow, so a cross-platform serve (e.g. MySQL source → Databricks
    target) gets the target's catalog/schema picker, not the source's. Omitted =
    the source-is-target case; resolve the project's own source connection.
    """
    project = _get_project(project_id, session)
    from ..pg_resolver import resolve_read_connection_for_consumer
    from ..platform.namespace import get_namespace_model

    if target_connection_id is not None:
        from ..migration_orchestrator import resolve_target_ref
        platform, conn_ref = resolve_target_ref(session, target_connection_id)
        if not platform:
            return {"supported": False, "platform": "", "container_parts": [],
                    "catalogs": [], "schemas": []}
    else:
        # Source-is-target: served-location-first so a consumer over a materialized
        # source (e.g. Databricks) gets that platform's catalog/schema picker.
        platform, conn_ref, _ = resolve_read_connection_for_consumer(
            project, session, _contract_id(project)
        )
    try:
        model = get_namespace_model(platform)
    except KeyError:
        return {"supported": False, "platform": platform, "container_parts": [],
                "catalogs": [], "schemas": []}

    from ..routers.connections import _get_discovery_provider
    provider = _get_discovery_provider(platform)
    containers = model.container_parts

    # 3-level (catalog + schema): dependent catalog→schema picker.
    if "catalog" in containers:
        catalogs: list[dict] = []
        if provider is not None and hasattr(provider, "list_catalogs_and_schemas"):
            try:
                catalogs = provider.list_catalogs_and_schemas(conn_ref)
            except Exception:  # noqa: BLE001
                catalogs = []
        return {"supported": True, "required": True, "platform": platform,
                "container_parts": containers, "catalogs": catalogs, "schemas": []}

    # 2-level (schema only): single schema picker — Postgres schemas / MySQL
    # databases. Reuse the provider's existing list_namespaces (already keeps the
    # writable defaults like `public` and filters system schemas). Optional — a
    # blank choice falls back to the platform default ("public").
    schemas: list[str] = []
    if provider is not None and hasattr(provider, "list_namespaces"):
        try:
            for ns in provider.list_namespaces(conn_ref):
                if getattr(ns, "parts", None):
                    schemas.append(ns.parts[0])
        except Exception:  # noqa: BLE001
            schemas = []
    return {"supported": True, "required": False, "platform": platform,
            "container_parts": containers, "catalogs": [], "schemas": sorted(set(schemas))}


@router.get("/view-target-namespace")
def get_view_target_namespace(project_id: int, session: Session = Depends(get_session)):
    """The deploy-target namespace ("catalog.schema") for 3-level platforms.

    Keyed by contract_id so it works for BOTH archetypes — dpe-cf products have
    no SourceBinding. Stored on MaterializationTarget (a namespace-only row is
    NOT treated as a configured target connection)."""
    project = _get_project(project_id, session)
    row = session.get(MaterializationTarget, _contract_id(project))
    return {"view_target_namespace": (getattr(row, "view_target_namespace", "") or "") if row else ""}


@router.put("/view-target-namespace")
def set_view_target_namespace(
    project_id: int,
    body: ViewTargetNamespaceInput,
    session: Session = Depends(get_session),
):
    """Persist the deploy-target namespace independently of the connection-target
    flow (so the source-is-target path can't wipe it). Upserts a
    MaterializationTarget row carrying ONLY the namespace when none exists."""
    project = _get_project_for_write(project_id, session)
    cid = _contract_id(project)
    row = session.get(MaterializationTarget, cid)
    if row is None:
        row = MaterializationTarget(contract_id=cid)
    row.view_target_namespace = (body.view_target_namespace or "").strip()
    row.updated_at = datetime.utcnow()
    session.add(row)
    session.commit()
    return {"status": "saved", "view_target_namespace": row.view_target_namespace}


@router.delete("/materialization-target")
def clear_materialization_target(project_id: int, session: Session = Depends(get_session)):
    """Clear the per-product target — materialization reverts to the source connection.

    Preserves ``view_target_namespace`` (a distinct deploy setting): only the
    connection target is cleared. The row is deleted outright only when no
    namespace remains, so the source-is-target path can't wipe a set namespace."""
    project = _get_project_for_write(project_id, session)
    row = session.get(MaterializationTarget, _contract_id(project))
    if row is not None:
        row.target_connection_id = None
        row.connection_json = "{}"
        row.platform = ""
        if (getattr(row, "view_target_namespace", "") or "").strip():
            row.updated_at = datetime.utcnow()
            session.add(row)
        else:
            session.delete(row)
        session.commit()
    return {"status": "cleared", "configured": False}


# ── Retrieve the generated dbt project ────────────────────────────────────────

# Build/log dirs are ephemeral — never part of the deliverable source.
_DBT_EXCLUDE_DIRS = {"target", "logs", "dbt_packages", ".venv", "__pycache__"}
# Per-run artifacts + secrets are excluded by name.
_DBT_EXCLUDE_NAMES = {"run_result.json", "run.log", ".env"}


def collect_dbt_files(project_code: str) -> dict[str, str]:
    """Read the source files of a generated dbt project (excluding build dirs).
    Returns {relative_path: text_content}. Empty when the project hasn't been
    scaffolded yet."""
    dbt_dir = BASE_PROJECT_DIR / project_code / "dbt"
    files: dict[str, str] = {}
    if not dbt_dir.is_dir():
        return files
    for path in sorted(dbt_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(dbt_dir)
        if any(part in _DBT_EXCLUDE_DIRS for part in rel.parts):
            continue
        if path.name in _DBT_EXCLUDE_NAMES:
            continue
        try:
            files[str(rel)] = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
    return files


@router.get("/dbt-project")
def get_dbt_project(
    project_id: int,
    format: str = "zip",
    session: Session = Depends(get_session),
):
    """Retrieve the generated dbt project for a materialized product, so an
    engineer can run/maintain it themselves. `format=zip` (default) streams a
    zip; `format=json` returns a {files: {path: content}} map. Excludes
    target/logs/dbt_packages; profiles.yml is safe (env_var refs, no secrets)."""
    project = _get_project(project_id, session)
    files = collect_dbt_files(project.project_code)
    if not files:
        raise HTTPException(404, "No dbt project found — materialize this product first.")
    if format == "json":
        return {"project_code": project.project_code, "files": files}
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, content in files.items():
            zf.writestr(f"{project.project_code}-dbt/{rel}", content)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{project.project_code}-dbt.zip"'},
    )
