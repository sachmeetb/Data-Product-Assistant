"""Object-store publish core (ADR-14, docs/architecture/object-store.md).

Publishes a project's generated data artifacts (Parquet + paired manifests) to
an S3-compatible object store via the ObjectStoreProvider. The transactional
run-prefix protocol (D6): upload every data object first, write the run
``manifest.json`` last, then flip ``latest.json`` to point at the run — so a
reader that follows ``latest.json`` never sees a half-published snapshot and a
retry is idempotent (new run_id each time).

This is the v1 PUBLISH path: it reads finished local artifacts and pushes them.
The packaged runners are NOT modified — that native-`s3://`-I/O rewrite is Phase 5.

Peer of ``git_provider`` / ``_push_product_to_git`` for the git side.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from . import serving_package
from .models import (
    ArtifactPublishRun,
    PlatformConnection,
    Project,
    ProjectArtifactStoreBinding,
)
from .platform.dispatch import (
    ProviderUnavailable,
    UnknownPlatform,
    get_object_store_provider,
)
from .platform.registry import get_registry
from .platform.secrets import resolve_secret

logger = logging.getLogger(__name__)


class PublishError(Exception):
    """Actionable publish failure — routers map it to a 400/409."""


# ── resolution ─────────────────────────────────────────────────────────────────

def get_binding(session: Session, project_id: int) -> Optional[ProjectArtifactStoreBinding]:
    return session.get(ProjectArtifactStoreBinding, project_id)


def is_object_store_platform(platform_type: str) -> bool:
    m = get_registry().get_manifest(platform_type)
    return m is not None and m.adapter_kind == "object_store"


def _connection_ref(conn: PlatformConnection, bucket_override: Optional[str]) -> dict:
    """Build the provider connection_ref (mirrors the connection-test endpoint):
    the resolved secret rides ``resolved_password`` and never leaves this call."""
    extra = json.loads(conn.extra_config_json or "{}")
    if bucket_override:
        extra = {**extra, "bucket": bucket_override}
    return {
        "connection_id": str(conn.id),
        "host": conn.host,
        "port": conn.port,
        "database": conn.database,
        "username": conn.username,
        "resolved_password": resolve_secret(conn.secret_ref),
        "extra_config": extra,
    }


def _resolve_bucket(binding: ProjectArtifactStoreBinding, conn: PlatformConnection) -> str:
    extra = json.loads(conn.extra_config_json or "{}")
    bucket = binding.bucket or extra.get("bucket") or conn.database or ""
    if not bucket:
        raise PublishError(
            "no bucket configured — set it on the artifact-store binding or the "
            "connection's extra_config.bucket / database"
        )
    return bucket


def _new_run_id() -> str:
    return f"{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


# ── publish ────────────────────────────────────────────────────────────────────

def publish_project_artifacts(
    project: Project,
    session: Session,
    *,
    actor: str = "",
    presign_ttl: int = 3600,
) -> dict:
    """Publish the project's allowlisted artifacts under an immutable run prefix.

    Raises ``PublishError`` when unconfigured / no provider / no bucket. Returns a
    summary ``{run_id, bucket, run_prefix, object_count, bytes_uploaded, objects,
    presigned_urls, latest_json_uri}``. An empty artifact set is a clean no-op
    result (status 'empty'), not an error.
    """
    binding = get_binding(session, project.id)
    if binding is None:
        raise PublishError(
            "no object-store binding for this project — configure one "
            "(PUT /api/projects/{id}/artifact-store-binding) before publishing"
        )
    conn = session.get(PlatformConnection, binding.connection_id)
    if conn is None:
        raise PublishError(f"bound connection {binding.connection_id} no longer exists")
    if not is_object_store_platform(conn.platform_type):
        raise PublishError(
            f"connection '{conn.connection_name}' is platform '{conn.platform_type}', "
            "not an object store"
        )
    try:
        provider = get_object_store_provider(conn.platform_type)
    except (UnknownPlatform, ProviderUnavailable) as exc:
        raise PublishError(str(exc))

    bucket = _resolve_bucket(binding, conn)
    ref = _connection_ref(conn, bucket)
    prefix = (binding.project_prefix or f"{project.project_code}/").strip("/")
    run_id = _new_run_id()
    run_prefix = f"{prefix}/runs/{run_id}"

    artifacts = serving_package.collect_for_object_store(project.project_code)
    if not artifacts:
        _record(session, project, conn, run_id, bucket, run_prefix,
                status="empty", object_count=0, bytes_uploaded=0, actor=actor)
        return {
            "run_id": run_id, "bucket": bucket, "run_prefix": run_prefix,
            "object_count": 0, "bytes_uploaded": 0, "objects": [],
            "presigned_urls": {}, "latest_json_uri": None,
            "status": "empty",
            "message": "no publishable data artifacts found "
                       "(run a lakehouse export or a Parquet export first)",
        }

    try:
        provider.ensure_bucket(ref, bucket)
        objects: list[dict] = []
        total_bytes = 0
        # 1. data objects first
        for a in artifacts:
            key = f"{run_prefix}/{a['rel_path']}"
            uri = provider.upload_file(
                ref, str(a["local_path"]), key,
                bucket=bucket, content_type=a["content_type"],
            )
            total_bytes += int(a["size"])
            objects.append({"key": key, "uri": uri, "size": a["size"], "kind": a["kind"]})

        # 2. run manifest — written AFTER all data objects
        run_manifest = {
            "schema": "wb.object_store.run/v1",
            "run_id": run_id,
            "project_code": project.project_code,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "created_by": actor,
            "object_count": len(objects),
            "bytes": total_bytes,
            "objects": [{"key": o["key"], "size": o["size"], "kind": o["kind"]} for o in objects],
        }
        manifest_key = f"{run_prefix}/manifest.json"
        provider.put_bytes(ref, json.dumps(run_manifest, indent=2).encode(),
                           manifest_key, bucket=bucket, content_type="application/json")

        # 3. latest.json pointer — flipped LAST
        latest = {
            "schema": "wb.object_store.latest/v1",
            "run_id": run_id,
            "run_prefix": run_prefix,
            "manifest_key": manifest_key,
            "object_count": len(objects),
            "bytes": total_bytes,
            "created_at": run_manifest["created_at"],
        }
        latest_key = f"{prefix}/latest.json"
        latest_uri = provider.put_bytes(
            ref, json.dumps(latest, indent=2).encode(),
            latest_key, bucket=bucket, content_type="application/json",
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean failure + audit it
        _record(session, project, conn, run_id, bucket, run_prefix,
                status="failed", object_count=0, bytes_uploaded=0,
                actor=actor, error=str(exc))
        raise PublishError(f"object-store publish failed: {exc}")

    # presigned GET URLs for the data objects + latest.json (signed for the
    # PUBLIC endpoint so a browser can open them).
    presigned: dict[str, str] = {}
    try:
        for o in objects:
            presigned[o["key"]] = provider.presign_get(ref, o["key"], bucket=bucket, ttl_seconds=presign_ttl)
        presigned[latest_key] = provider.presign_get(ref, latest_key, bucket=bucket, ttl_seconds=presign_ttl)
    except Exception:  # noqa: BLE001 — presigning is a convenience, never fatal
        logger.warning("presign failed for run %s (upload succeeded)", run_id, exc_info=True)

    _record(session, project, conn, run_id, bucket, run_prefix,
            status="succeeded", object_count=len(objects),
            bytes_uploaded=total_bytes, actor=actor)

    return {
        "run_id": run_id, "bucket": bucket, "run_prefix": run_prefix,
        "object_count": len(objects), "bytes_uploaded": total_bytes,
        "objects": objects, "presigned_urls": presigned,
        "latest_json_uri": latest_uri, "status": "succeeded",
    }


def _record(session, project, conn, run_id, bucket, run_prefix, *,
            status, object_count, bytes_uploaded, actor, error="") -> None:
    try:
        session.add(ArtifactPublishRun(
            project_id=project.id, connection_id=conn.id, run_id=run_id,
            bucket=bucket, run_prefix=run_prefix, status=status,
            object_count=object_count, bytes_uploaded=bytes_uploaded,
            created_by=actor, error=error[:2000],
        ))
        session.commit()
    except Exception:  # noqa: BLE001 — audit is best-effort, never blocks a publish
        logger.warning("failed to record ArtifactPublishRun for %s", run_id, exc_info=True)
        session.rollback()


def last_run(session: Session, project_id: int) -> Optional[ArtifactPublishRun]:
    return session.exec(
        select(ArtifactPublishRun)
        .where(ArtifactPublishRun.project_id == project_id)
        .order_by(ArtifactPublishRun.created_at.desc())
    ).first()
