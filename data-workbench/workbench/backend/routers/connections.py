"""Connection management API.

CRUD for PlatformConnection + SourceBinding, and a live-probe (test)
endpoint.  All responses are credential-safe: secret_ref is returned
as-is (an opaque pointer), passwords are NEVER returned.

Endpoints
---------
GET    /api/connections                  list all connections
POST   /api/connections                  register a new connection
GET    /api/connections/{id}             get one connection
PUT    /api/connections/{id}             update a connection
DELETE /api/connections/{id}             delete (forbidden if bound to a project)
POST   /api/connections/{id}/test        probe a live connection

GET    /api/projects/{pid}/source-binding       get the project's source binding
PUT    /api/projects/{pid}/source-binding       upsert the source binding
DELETE /api/projects/{pid}/source-binding       remove the source binding
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..database import get_session
from ..models import (
    PlatformConnection, ExecutionProfile, SourceBinding, Project,
    ProjectArtifactStoreBinding,
)
from ..platform.registry import get_registry
from ..platform.secrets import resolve_secret

router = APIRouter(prefix="/api/connections", tags=["connections"])
project_binding_router = APIRouter(prefix="/api/projects", tags=["connections"])


# ── request / response shapes ─────────────────────────────────────────────────

class ConnectionCreate(BaseModel):
    connection_name: str
    platform_type: str
    host: str
    port: int = 5432
    database: str
    username: str = ""
    secret_ref: str = ""
    # One-shot password — stored as "direct:<password>" in secret_ref when
    # secret_ref is blank.  Never logged or returned in API responses.
    # Never passed to an LLM call — used only for live DB connection probes
    # and skill-script invocations.
    password: Optional[str] = None
    extra_config: dict = {}
    connection_roles: list[str] = ["source"]


class ConnectionUpdate(BaseModel):
    connection_name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    username: Optional[str] = None
    secret_ref: Optional[str] = None
    # One-shot password update — converts to "direct:<password>" in secret_ref.
    password: Optional[str] = None
    extra_config: Optional[dict] = None
    connection_roles: Optional[list[str]] = None


class TestConnectionRequest(BaseModel):
    # Optional one-shot password override — used once, never stored.
    # Callers may omit this if the secret_ref already resolves via env.
    password_override: Optional[str] = None


class SourceBindingUpsert(BaseModel):
    connection_id: int
    default_schema: str = ""
    # None → leave the existing value untouched on update (the serving workflow,
    # via configure_serving, is the authoritative owner of the dialect; the
    # data-source picker no longer sets it, so an omitted value must not wipe it).
    target_dialect: Optional[str] = None
    # Optional deploy-target namespace ("catalog.schema") for platforms whose
    # write target can't be inferred from a read-only source (e.g. Databricks).
    # None → leave the existing value untouched on update.
    view_target_namespace: Optional[str] = None


class ArtifactStoreBindingUpsert(BaseModel):
    # Where a project PUBLISHES data artifacts to an object store (ADR-14).
    connection_id: int
    bucket: str = ""            # overrides the connection's extra_config.bucket
    project_prefix: str = ""    # key namespace; blank → "<project_code>/"
    auto_publish: bool = False  # reserved (v2)


# Secret-ref prefixes that are *references* (safe to echo) rather than a stored
# secret value.  A literal password is stored as "direct:<pw>"; anything that
# isn't one of these prefixes is treated as a raw literal and masked.
_SECRET_REF_PREFIXES = ("env:", "vault:", "ssm:", "asm:")


def _looks_like_secret_ref(value: str) -> bool:
    return any(value.startswith(p) for p in _SECRET_REF_PREFIXES)


def _normalize_stored_secret(secret_ref: Optional[str], password: Optional[str]) -> Optional[str]:
    """Return the value to persist in ``PlatformConnection.secret_ref``.

    Tier-0 credential containment: a literal password (the dedicated ``password``
    field, OR a non-reference value mistakenly posted to ``secret_ref``) is stored
    as ``direct:<pw>`` so it is never mistaken for a reference and always masked on
    read.  An ``env:``/``vault:``/… reference is stored verbatim.  Returns ``None``
    to mean "not supplied — leave the existing value unchanged" (update path).
    """
    if password:
        return f"direct:{password}"
    if secret_ref is None:
        return None
    if secret_ref == "" or _looks_like_secret_ref(secret_ref) or secret_ref.startswith("direct:"):
        return secret_ref
    # A non-reference value in secret_ref is a literal credential → contain it.
    return f"direct:{secret_ref}"


def _connection_row(c: PlatformConnection) -> dict:
    # Tier-0 containment: NEVER return a stored secret value. An env:/vault:/…
    # reference is a safe pointer (echoed as-is); a stored password (direct:…)
    # or any other raw literal is masked. Callers key on `has_password`.
    raw = c.secret_ref or ""
    if not raw:
        secret_ref_display = ""
    elif _looks_like_secret_ref(raw):
        secret_ref_display = raw
    else:
        secret_ref_display = "***"
    return {
        "id": c.id,
        "connection_name": c.connection_name,
        "platform_type": c.platform_type,
        "host": c.host,
        "port": c.port,
        "database": c.database,
        "username": c.username,
        "secret_ref": secret_ref_display,
        "has_password": bool(c.secret_ref),
        "extra_config": json.loads(c.extra_config_json or "{}"),
        "connection_roles": json.loads(c.connection_roles_json or '["source"]'),
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


# ── connection CRUD ───────────────────────────────────────────────────────────

@router.get("")
def list_connections(session: Session = Depends(get_session)):
    conns = session.exec(select(PlatformConnection)).all()
    return {"connections": [_connection_row(c) for c in conns], "count": len(conns)}


@router.post("")
def create_connection(
    body: ConnectionCreate, session: Session = Depends(get_session)
):
    # Validate the platform is registered.
    reg = get_registry()
    if reg.get_manifest(body.platform_type) is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown platform_type '{body.platform_type}'. "
                   f"Known: {reg.platform_ids()}",
        )
    # Unique name check.
    existing = session.exec(
        select(PlatformConnection).where(
            PlatformConnection.connection_name == body.connection_name
        )
    ).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"A connection named '{body.connection_name}' already exists.",
        )
    secret_ref = _normalize_stored_secret(body.secret_ref, body.password) or ""
    conn = PlatformConnection(
        connection_name=body.connection_name,
        platform_type=body.platform_type,
        host=body.host,
        port=body.port,
        database=body.database,
        username=body.username,
        secret_ref=secret_ref,
        extra_config_json=json.dumps(body.extra_config),
        connection_roles_json=json.dumps(body.connection_roles or ["source"]),
    )
    session.add(conn)
    session.commit()
    session.refresh(conn)
    return _connection_row(conn)


@router.get("/{connection_id}")
def get_connection(connection_id: int, session: Session = Depends(get_session)):
    conn = session.get(PlatformConnection, connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id} not found")
    return _connection_row(conn)


@router.put("/{connection_id}")
def update_connection(
    connection_id: int,
    body: ConnectionUpdate,
    session: Session = Depends(get_session),
):
    conn = session.get(PlatformConnection, connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id} not found")
    if body.connection_name is not None:
        conn.connection_name = body.connection_name
    if body.host is not None:
        conn.host = body.host
    if body.port is not None:
        conn.port = body.port
    if body.database is not None:
        conn.database = body.database
    if body.username is not None:
        conn.username = body.username
    normalized_secret = _normalize_stored_secret(body.secret_ref, body.password)
    if normalized_secret is not None:
        conn.secret_ref = normalized_secret
    if body.extra_config is not None:
        conn.extra_config_json = json.dumps(body.extra_config)
    if body.connection_roles is not None:
        conn.connection_roles_json = json.dumps(body.connection_roles)
    conn.updated_at = datetime.utcnow()
    session.add(conn)
    session.commit()
    session.refresh(conn)
    return _connection_row(conn)


@router.delete("/{connection_id}")
def delete_connection(connection_id: int, session: Session = Depends(get_session)):
    conn = session.get(PlatformConnection, connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id} not found")
    # Refuse if any project is bound to this connection.
    bound = session.exec(
        select(SourceBinding).where(SourceBinding.connection_id == connection_id)
    ).first()
    if bound:
        raise HTTPException(
            status_code=409,
            detail=f"Connection {connection_id} is bound to project_id {bound.project_id}. "
                   "Remove the source binding first.",
        )
    session.delete(conn)
    session.commit()
    return {"deleted": True, "id": connection_id}


@router.post("/{connection_id}/test")
def test_connection(
    connection_id: int,
    body: TestConnectionRequest,
    session: Session = Depends(get_session),
):
    """Probe a live connection.  Returns server_version + capabilities on success."""
    conn = session.get(PlatformConnection, connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id} not found")

    reg = get_registry()
    manifest = reg.get_manifest(conn.platform_type)
    # Object stores (adapterKind: object_store, ADR-14) have no SQL "connection"
    # capability — they gate on artifact_publish and use the ObjectStoreProvider.
    is_object_store = manifest is not None and manifest.adapter_kind == "object_store"
    gate_cap = "artifact_publish" if is_object_store else "connection"
    if not reg.is_usable(conn.platform_type, gate_cap):
        level = reg.get_capability(conn.platform_type, gate_cap).value
        return {
            "ok": False,
            "error": f"Platform '{conn.platform_type}' {gate_cap} capability is '{level}' "
                     f"— not usable yet.",
        }

    password = body.password_override or resolve_secret(conn.secret_ref)
    connection_ref = {
        "connection_id": str(conn.id),
        "host": conn.host,
        "port": conn.port,
        "database": conn.database,
        "username": conn.username,
        "resolved_password": password,
        # extra_config carries platform-specific keys (e.g. Databricks http_path,
        # or an object store's endpoint_url / bucket) that the provider needs.
        "extra_config": json.loads(conn.extra_config_json or "{}"),
    }

    provider = (
        _get_object_store_provider(conn.platform_type)
        if is_object_store
        else _get_connection_provider(conn.platform_type)
    )
    if provider is None:
        return {"ok": False, "error": f"No provider for '{conn.platform_type}'."}

    evidence = provider.probe(connection_ref)
    return {
        "ok": not bool(evidence.warnings),
        "platform_type": conn.platform_type,
        "connection_id": connection_id,
        "server_version": evidence.server_version,
        "capabilities": {k: v.value for k, v in evidence.capabilities.items()},
        "warnings": evidence.warnings,
    }


# ── source binding ─────────────────────────────────────────────────────────────

@project_binding_router.get("/{project_id}/source-binding")
def get_source_binding(project_id: int, session: Session = Depends(get_session)):
    binding = session.exec(
        select(SourceBinding).where(SourceBinding.project_id == project_id)
    ).first()
    if binding is None:
        return {"bound": False, "project_id": project_id}
    conn = session.get(PlatformConnection, binding.connection_id)
    return {
        "bound": True,
        "project_id": project_id,
        "connection_id": binding.connection_id,
        "connection_name": conn.connection_name if conn else None,
        "platform_type": conn.platform_type if conn else None,
        "default_schema": binding.default_schema,
        "target_dialect": binding.target_dialect or "",
        "view_target_namespace": binding.view_target_namespace or "",
        "updated_at": binding.updated_at.isoformat() if binding.updated_at else None,
    }


@project_binding_router.put("/{project_id}/source-binding")
def upsert_source_binding(
    project_id: int,
    body: SourceBindingUpsert,
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    conn = session.get(PlatformConnection, body.connection_id)
    if conn is None:
        raise HTTPException(
            status_code=404,
            detail=f"Connection {body.connection_id} not found",
        )

    binding = session.exec(
        select(SourceBinding).where(SourceBinding.project_id == project_id)
    ).first()
    if binding is None:
        binding = SourceBinding(
            project_id=project_id,
            connection_id=body.connection_id,
            default_schema=body.default_schema,
            target_dialect=(body.target_dialect or ""),
            view_target_namespace=(body.view_target_namespace or ""),
        )
    else:
        binding.connection_id = body.connection_id
        binding.default_schema = body.default_schema
        # None = "leave untouched" so a partial PUT (e.g. the data-source picker,
        # which no longer sets a dialect) can't silently wipe a dialect the
        # serving workflow set. Same guard as view_target_namespace below.
        if body.target_dialect is not None:
            binding.target_dialect = body.target_dialect
        if body.view_target_namespace is not None:
            binding.view_target_namespace = body.view_target_namespace
        binding.updated_at = datetime.utcnow()

    session.add(binding)
    session.commit()
    session.refresh(binding)
    return {
        "bound": True,
        "project_id": project_id,
        "connection_id": binding.connection_id,
        "connection_name": conn.connection_name,
        "platform_type": conn.platform_type,
        "default_schema": binding.default_schema,
        "target_dialect": binding.target_dialect or "",
        "view_target_namespace": binding.view_target_namespace or "",
    }


@project_binding_router.delete("/{project_id}/source-binding")
def delete_source_binding(project_id: int, session: Session = Depends(get_session)):
    binding = session.exec(
        select(SourceBinding).where(SourceBinding.project_id == project_id)
    ).first()
    if binding is None:
        raise HTTPException(
            status_code=404,
            detail=f"No source binding for project {project_id}",
        )
    session.delete(binding)
    session.commit()
    return {"deleted": True, "project_id": project_id}


# ── artifact-store binding (object-store publish target, ADR-14) ────────────────

def _artifact_binding_row(binding: ProjectArtifactStoreBinding, conn: Optional[PlatformConnection]) -> dict:
    return {
        "bound": True,
        "project_id": binding.project_id,
        "connection_id": binding.connection_id,
        "connection_name": conn.connection_name if conn else None,
        "platform_type": conn.platform_type if conn else None,
        "bucket": binding.bucket,
        "project_prefix": binding.project_prefix,
        "auto_publish": binding.auto_publish,
        "updated_at": binding.updated_at.isoformat() if binding.updated_at else None,
    }


@project_binding_router.get("/{project_id}/artifact-store-binding")
def get_artifact_store_binding(project_id: int, session: Session = Depends(get_session)):
    binding = session.get(ProjectArtifactStoreBinding, project_id)
    if binding is None:
        return {"bound": False, "project_id": project_id}
    conn = session.get(PlatformConnection, binding.connection_id)
    return _artifact_binding_row(binding, conn)


@project_binding_router.put("/{project_id}/artifact-store-binding")
def upsert_artifact_store_binding(
    project_id: int,
    body: ArtifactStoreBindingUpsert,
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    conn = session.get(PlatformConnection, body.connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail=f"Connection {body.connection_id} not found")
    # The bound connection must be an object store (adapterKind: object_store).
    manifest = get_registry().get_manifest(conn.platform_type)
    if manifest is None or manifest.adapter_kind != "object_store":
        raise HTTPException(
            status_code=422,
            detail=f"Connection '{conn.connection_name}' is platform "
                   f"'{conn.platform_type}', not an object store.",
        )
    binding = session.get(ProjectArtifactStoreBinding, project_id)
    if binding is None:
        binding = ProjectArtifactStoreBinding(project_id=project_id, connection_id=body.connection_id)
    binding.connection_id = body.connection_id
    binding.bucket = body.bucket
    binding.project_prefix = body.project_prefix
    binding.auto_publish = body.auto_publish
    binding.updated_at = datetime.utcnow()
    session.add(binding)
    session.commit()
    session.refresh(binding)
    return _artifact_binding_row(binding, conn)


@project_binding_router.delete("/{project_id}/artifact-store-binding")
def delete_artifact_store_binding(project_id: int, session: Session = Depends(get_session)):
    binding = session.get(ProjectArtifactStoreBinding, project_id)
    if binding is None:
        raise HTTPException(status_code=404, detail=f"No artifact-store binding for project {project_id}")
    session.delete(binding)
    session.commit()
    return {"deleted": True, "project_id": project_id}


# ── provider dispatch ─────────────────────────────────────────────────────────

# Provider *selection* is owned by the central lookup in ``platform/dispatch.py``
# (outside any router) so every code path resolves a provider identically and
# fails closed on unknown platforms (ADR-9). These thin wrappers preserve the
# router's historic "return None on unknown" contract for callers that branch on
# ``is None`` (test_connection, the namespace browser, transfer target checks).

def _get_connection_provider(platform_type: str):
    """Return the ConnectionProvider instance for `platform_type`, or None."""
    from ..platform.dispatch import get_connection_provider, UnknownPlatform
    try:
        return get_connection_provider(platform_type)
    except UnknownPlatform:
        return None


def _get_discovery_provider(platform_type: str):
    """Return the DiscoveryProvider instance for `platform_type`, or None."""
    from ..platform.dispatch import get_discovery_provider, UnknownPlatform
    try:
        return get_discovery_provider(platform_type)
    except UnknownPlatform:
        return None


def _get_object_store_provider(platform_type: str):
    """Return the ObjectStoreProvider for `platform_type`, or None (ADR-14).

    None for an unknown platform OR a known object store whose provider isn't
    built yet (gcs/azure_adls) — the caller returns a clean 'no provider' result.
    """
    from ..platform.dispatch import (
        get_object_store_provider, UnknownPlatform, ProviderUnavailable,
    )
    try:
        return get_object_store_provider(platform_type)
    except (UnknownPlatform, ProviderUnavailable):
        return None


def _get_transfer_provider(platform_type: str):
    """Return the TransferExecutionProvider for a TARGET platform, or None.

    The DuckDB engine handles postgres / mysql / duckdb targets; the dlt engine
    handles warehouse targets (snowflake / databricks). Object stores etc. are
    unknown → None (callers branch on that).
    """
    from ..platform.dispatch import get_transfer_provider, UnknownPlatform
    try:
        return get_transfer_provider(platform_type)
    except UnknownPlatform:
        return None


# ── platform routing helpers ──────────────────────────────────────────────────

# Maps platform_type → the discovery skill that handles schema/table enumeration
# and data discovery for that platform.  A missing entry means use the stage's
# default skill (i.e. no override needed — typically Postgres).
DISCOVERY_SKILL_BY_PLATFORM: dict[str, str] = {
    "mysql": "data-discovery-mysql",
    "snowflake": "data-discovery-snowflake",
    "databricks": "data-discovery-databricks",
    "duckdb": "data-discovery-parquet",
    "duckdb_local": "data-discovery-parquet",
}

# Same mapping for profiling skills.
PROFILING_SKILL_BY_PLATFORM: dict[str, str] = {
    "mysql": "data-profiling-mysql",
    "snowflake": "data-profiling-snowflake",
    "databricks": "data-profiling-databricks",
    # Parquet profiling reuses the existing DuckDB+pyarrow profiler skill.
    "duckdb": "data-profile-parquet",
    "duckdb_local": "data-profile-parquet",
}


def build_connection_string(platform_type: str, ref: dict) -> str:
    """Build a platform connection DSN from a *connection_ref* dict.

    The ref is the STRUCTURED connection (host/port/database/username/
    ``resolved_password``/``default_schema``/``extra_config``) resolved from a
    ``SourceBinding`` → ``PlatformConnection``.  Returns the full DSN ready for a
    skill script or driver.  Passwords in the returned string are resolved at call
    time and must be treated as ephemeral — never persist or log the return value.
    A transient ``dsn`` key (threaded by the sql_executor facade) short-circuits
    to that exact string.
    """
    if ref.get("dsn"):
        return ref["dsn"]

    if platform_type in ("postgres", "postgresql"):
        # Build the DSN from host/port/database/username/resolved_password, with
        # the binding's default_schema encoded as a search_path option (mirrors
        # projects.py:set_data_source so both entry points produce the same DSN).
        host = ref.get("host", "")
        database = ref.get("database", "")
        if not host or not database:
            return ""
        from urllib.parse import quote
        username = ref.get("username", "")
        password = ref.get("resolved_password", "")
        user_part = f"{username}:{quote(password, safe='')}@" if username else ""
        dsn = f"postgresql://{user_part}{host}:{ref.get('port', 5432)}/{database}"
        schema = ref.get("default_schema", "")
        if schema:
            dsn += f"?options=-csearch_path%3D{quote(schema, safe='')}"
        return dsn

    username = ref.get("username", "")
    password = ref.get("resolved_password", "")

    # Databricks SQL Warehouse: the discovery scripts (data-discovery-databricks)
    # parse a query-string DSN — databricks://token:TOKEN@HOST?http_path=..&catalog=..&schema=..
    # http_path is MANDATORY and lives in extra_config; catalog/schema come from
    # extra_config when set, else are derived by splitting the `database` field
    # (Unity Catalog "catalog.schema", e.g. "samples.bakehouse").  Without this the
    # generic path below drops http_path entirely and the connect fails silently.
    if platform_type == "databricks":
        from urllib.parse import quote
        from ..platform.namespace import get_namespace_model
        extra = ref.get("extra_config", {}) or {}
        http_path = extra.get("http_path", "")
        # catalog/schema: explicit extra_config wins; otherwise parse the
        # `database` field ("catalog.schema") via the platform namespace model
        # so the level semantics live in ONE place, not an inline partition.
        parsed = get_namespace_model(platform_type).parse(ref.get("database", "") or "")
        catalog = extra.get("catalog") or parsed.get("catalog", "")
        schema = extra.get("schema") or parsed.get("schema", "")
        user_part = f"{username}:{quote(password, safe='')}@" if username else ""
        params = []
        if http_path:
            params.append(f"http_path={quote(http_path, safe='')}")
        if catalog:
            params.append(f"catalog={quote(catalog, safe='')}")
        if schema:
            params.append(f"schema={quote(schema, safe='')}")
        query = ("?" + "&".join(params)) if params else ""
        return f"databricks://{user_part}{ref.get('host', 'localhost')}:{ref.get('port', 443)}{query}"

    # Snowflake: the discovery/profiling scripts + snowflake-sqlalchemy parse a
    # query-string DSN — snowflake://user:PW@ACCOUNT/DATABASE?warehouse=..&role=..&schema=..
    # The account identifier lives in `host`; warehouse/role/schema live in
    # extra_config (schema falls back to the binding's default_schema).  Without
    # this branch the generic fallback below drops warehouse+role entirely and the
    # connect fails silently (no warehouse → no compute).
    if platform_type == "snowflake":
        from urllib.parse import quote
        extra = ref.get("extra_config", {}) or {}
        account = ref.get("host", "")
        database = ref.get("database", "")
        user_part = f"{username}:{quote(password, safe='')}@" if username else ""
        params = []
        for key in ("warehouse", "role", "schema"):
            val = extra.get(key)
            if not val and key == "schema":
                val = ref.get("default_schema", "")
            if val:
                params.append(f"{key}={quote(str(val), safe='')}")
        query = ("?" + "&".join(params)) if params else ""
        return f"snowflake://{user_part}{account}/{database}{query}"

    user_part = ""
    if username:
        user_part = f"{username}:{password}@"
    db_path = f"/{ref['database']}" if ref.get("database") else ""
    return f"{platform_type}://{user_part}{ref.get('host', 'localhost')}:{ref.get('port', 0)}{db_path}"


def resolve_materialization_target_ref(
    project_code: str,
    session: Session,
) -> Optional[tuple[str, dict]]:
    """Resolve a product's serving TARGET to a (platform_type, connection_ref) pair.

    Reads ``MaterializationTarget.target_connection_id`` (keyed by
    ``{project_code}-contract``) → ``PlatformConnection``. Returns ``None`` when
    no distinct target connection is configured — the signal that the product is
    served in-place (same-instance) rather than materialized onto a separate
    platform. The ``resolved_password`` in the returned ref is ephemeral; never
    log or persist it.

    Shared by the cross-platform transfer path (``transfer_execution._resolve_target``)
    and the consumer served-location resolver (``pg_resolver.resolve_consumed_source_serving``)
    so both resolve a serving target identically.
    """
    from ..models import MaterializationTarget

    row = session.get(MaterializationTarget, f"{project_code}-contract")
    if row is None or row.target_connection_id is None:
        return None
    tconn = session.get(PlatformConnection, row.target_connection_id)
    if tconn is None:
        return None
    try:
        extra = json.loads(tconn.extra_config_json or "{}")
    except (TypeError, ValueError):
        extra = {}
    ref = {
        "host": tconn.host,
        "port": tconn.port,
        "database": tconn.database,
        "username": tconn.username,
        "resolved_password": resolve_secret(tconn.secret_ref),
        "extra_config": extra,
    }
    return (tconn.platform_type or "").lower(), ref


def resolve_source_connection_ref(
    project: Project,
    session: Session,
) -> tuple[str, dict]:
    """Resolve a project's source to a (platform_type, connection_ref) pair.

    Resolution: the project's ``SourceBinding`` → ``PlatformConnection`` — the
    single connection contract. Returns ``(platform_type, connection_ref)`` where
    ``connection_ref`` is a STRUCTURED dict (host/port/database/username/
    resolved_password/default_schema/extra_config) suitable for a provider or
    ``build_connection_string``.

    Returns ``("postgres", {})`` — an empty STRUCTURED ref (no ``host``) — when
    the project has no source binding yet; callers detect that as unresolved
    (no legacy DSN-in-ref sentinel).
    """
    binding = session.exec(
        select(SourceBinding).where(SourceBinding.project_id == project.id)
    ).first()

    if binding is not None:
        conn = session.get(PlatformConnection, binding.connection_id)
        if conn is not None:
            password = resolve_secret(conn.secret_ref)
            return conn.platform_type, {
                "connection_id": str(conn.id),
                "host": conn.host,
                "port": conn.port,
                "database": conn.database,
                "username": conn.username,
                "resolved_password": password,
                "default_schema": binding.default_schema,
                "extra_config": json.loads(conn.extra_config_json or "{}"),
            }

    # No source binding yet — an unresolved (empty structured) ref.
    return "postgres", {}
