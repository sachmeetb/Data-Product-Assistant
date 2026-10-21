"""S3-compatible object-store provider (ADR-14).

The first concrete ``ObjectStoreProvider`` — boto3 against any S3-API endpoint
(SeaweedFS / RustFS / AWS S3 / MinIO). GCS and Azure ADLS become sibling
providers behind the same Protocol later.

Conventions validated by the ADR-14 conformance spike (docs/architecture/object-store.md):
  * path-style addressing + signature_version s3v4 (works for both the fixture
    and AWS; AWS also accepts virtual-hosted — override via extra_config),
  * ``probe`` does head_bucket + a tiny put/delete round-trip — NOT list_buckets,
    which usually needs account-wide permission a scoped key won't have,
  * presigned GET URLs are signed for the PUBLIC endpoint (the browser can't
    resolve the internal compose hostname) — see the internal/public split.

boto3 is imported lazily inside method bodies so this module (and the dispatch
table) import cleanly in environments without the driver.
"""
from __future__ import annotations

import io
import logging
import uuid
from typing import Any, Optional

from ..interfaces import (
    CapabilityEvidence,
    CapabilityLevel,
    ObjectRef,
    ValidationReport,
)

logger = logging.getLogger(__name__)

PLATFORM_TYPE = "s3"


# ── connection_ref → boto3 shape ──────────────────────────────────────────────

def _extra(connection_ref: dict[str, Any]) -> dict[str, Any]:
    return connection_ref.get("extra_config") or {}


def _endpoint_url(connection_ref: dict[str, Any], *, public: bool = False) -> Optional[str]:
    """Resolve the S3 endpoint URL.

    ``public=True`` returns the presign endpoint (what a browser hits); it falls
    back to the internal endpoint when unset. An explicit ``endpoint_url`` wins;
    otherwise build ``<scheme>://<host>:<port>`` from the connection fields.
    Empty (AWS) → None, which lets boto3 use the real AWS endpoints.
    """
    extra = _extra(connection_ref)
    if public:
        ep = extra.get("public_endpoint_url") or extra.get("endpoint_url")
        if ep:
            return ep
    else:
        ep = extra.get("endpoint_url")
        if ep:
            return ep
    host = connection_ref.get("host") or ""
    if not host or host.lower() in ("aws", "s3.amazonaws.com"):
        return None
    use_ssl = _use_ssl(connection_ref)
    scheme = "https" if use_ssl else "http"
    port = connection_ref.get("port")
    netloc = f"{host}:{port}" if port else host
    return f"{scheme}://{netloc}"


def _use_ssl(connection_ref: dict[str, Any]) -> bool:
    extra = _extra(connection_ref)
    val = extra.get("use_ssl", extra.get("ssl"))
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    if val is None:
        # Default off for a bare host:port fixture, on for real AWS (no host).
        return not (connection_ref.get("host") or "")
    return bool(val)


def _region(connection_ref: dict[str, Any]) -> str:
    return _extra(connection_ref).get("region") or "us-east-1"


def _addressing_style(connection_ref: dict[str, Any]) -> str:
    # path-style is the safe default for S3-compatible fixtures; AWS can set
    # "virtual" or "auto" in extra_config.
    return _extra(connection_ref).get("addressing_style") or "path"


def _bucket(connection_ref: dict[str, Any], override: Optional[str]) -> str:
    b = override or _extra(connection_ref).get("bucket") or connection_ref.get("database") or ""
    if not b:
        raise ValueError("no bucket configured (pass bucket=, or set extra_config.bucket / database)")
    return b


def _client(connection_ref: dict[str, Any], *, public: bool = False):
    """Build a boto3 S3 client from a connection_ref."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=_endpoint_url(connection_ref, public=public),
        aws_access_key_id=connection_ref.get("username") or None,
        aws_secret_access_key=connection_ref.get("resolved_password") or None,
        region_name=_region(connection_ref),
        use_ssl=_use_ssl(connection_ref),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": _addressing_style(connection_ref)},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def _s3_uri(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


# ── provider ──────────────────────────────────────────────────────────────────

class S3ObjectStoreProvider:
    """S3-compatible ObjectStoreProvider (boto3)."""

    # -- config / probe --------------------------------------------------------

    def validate_config(
        self, public_config: dict[str, Any], secret_ref: str
    ) -> ValidationReport:
        errors: list[str] = []
        warnings: list[str] = []
        extra = public_config.get("extra_config") or {}
        bucket = extra.get("bucket") or public_config.get("database")
        if not bucket:
            errors.append("a bucket is required (extra_config.bucket or database)")
        if not public_config.get("username"):
            warnings.append("no access-key id (username) set — relying on the ambient AWS credential chain")
        if not secret_ref:
            warnings.append("no secret_ref set — relying on the ambient AWS credential chain")
        endpoint = extra.get("endpoint_url") or public_config.get("host")
        if endpoint and extra.get("public_endpoint_url") is None:
            warnings.append(
                "no public_endpoint_url set — presigned URLs will use the internal "
                "endpoint and may be unreachable from a browser"
            )
        return ValidationReport(valid=not errors, errors=errors, warnings=warnings)

    def probe(self, connection_ref: dict[str, Any]) -> CapabilityEvidence:
        """head_bucket + a tiny put/delete round-trip (never list_buckets)."""
        evidence = CapabilityEvidence(
            platform_type=PLATFORM_TYPE,
            instance_id=connection_ref.get("connection_id", "unknown"),
        )
        try:
            bucket = _bucket(connection_ref, None)
        except ValueError as exc:
            evidence.warnings.append(str(exc))
            return evidence
        try:
            client = _client(connection_ref)
        except Exception as exc:  # noqa: BLE001 — driver missing / bad config
            evidence.warnings.append(f"cannot build S3 client: {exc}")
            return evidence
        try:
            try:
                client.head_bucket(Bucket=bucket)
            except Exception:  # bucket may not exist yet — that's fine, try to create
                client.create_bucket(Bucket=bucket)
                evidence.warnings.append(f"bucket {bucket!r} did not exist — created it")
            # write-capability round-trip on a throwaway key
            probe_key = f".wb_probe/{uuid.uuid4().hex}"
            client.put_object(Bucket=bucket, Key=probe_key, Body=b"ok")
            client.delete_object(Bucket=bucket, Key=probe_key)
            evidence.capabilities = {"artifact_publish": CapabilityLevel.PREVIEW}
        except Exception as exc:  # noqa: BLE001
            evidence.warnings.append(f"object-store probe failed for bucket {bucket!r}: {exc}")
        return evidence

    # -- writes ----------------------------------------------------------------

    def ensure_bucket(
        self, connection_ref: dict[str, Any], bucket: Optional[str] = None
    ) -> None:
        b = _bucket(connection_ref, bucket)
        client = _client(connection_ref)
        try:
            client.head_bucket(Bucket=b)
        except Exception:
            client.create_bucket(Bucket=b)

    def upload_file(
        self, connection_ref: dict[str, Any], local_path: str, key: str,
        *, bucket: Optional[str] = None, content_type: Optional[str] = None,
    ) -> str:
        b = _bucket(connection_ref, bucket)
        client = _client(connection_ref)
        extra_args = {"ContentType": content_type} if content_type else None
        # upload_file transparently switches to multipart for large objects.
        client.upload_file(local_path, b, key, ExtraArgs=extra_args)
        return _s3_uri(b, key)

    def put_bytes(
        self, connection_ref: dict[str, Any], data: bytes, key: str,
        *, bucket: Optional[str] = None, content_type: Optional[str] = None,
    ) -> str:
        b = _bucket(connection_ref, bucket)
        client = _client(connection_ref)
        kwargs: dict[str, Any] = {"Bucket": b, "Key": key, "Body": io.BytesIO(data)}
        if content_type:
            kwargs["ContentType"] = content_type
        client.put_object(**kwargs)
        return _s3_uri(b, key)

    # -- reads -----------------------------------------------------------------

    def list_prefix(
        self, connection_ref: dict[str, Any], prefix: str,
        *, bucket: Optional[str] = None,
    ) -> list[ObjectRef]:
        b = _bucket(connection_ref, bucket)
        client = _client(connection_ref)
        out: list[ObjectRef] = []
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=b, Prefix=prefix):
            for obj in page.get("Contents", []):
                lm = obj.get("LastModified")
                out.append(ObjectRef(
                    key=obj["Key"],
                    size=int(obj.get("Size", 0)),
                    etag=(obj.get("ETag") or "").strip('"') or None,
                    last_modified=lm.isoformat() if lm is not None else None,
                ))
        return out

    def presign_get(
        self, connection_ref: dict[str, Any], key: str,
        *, bucket: Optional[str] = None, ttl_seconds: int = 3600,
    ) -> str:
        b = _bucket(connection_ref, bucket)
        # Sign against the PUBLIC endpoint so a browser can open the URL.
        client = _client(connection_ref, public=True)
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": b, "Key": key},
            ExpiresIn=ttl_seconds,
        )
