"""
gcs_file_store.py — durable, id-addressable file store backed by GCS.

The in-process dict in server.py only lives inside a single Cloud Run instance,
so an upload handled by one instance is invisible to a later request routed to
another instance (Cloud Run scales to multiple instances). This module persists
every file to GCS so it can be retrieved from any instance.

Layout in gs://{GCS_UPLOADS_BUCKET}/:
  filestore/{file_id}                    id-addressable copy — source of truth for reads
  sessions/{session_id}/{filename}       per-session folder copy for uploaded files

Enabled only when GCS_UPLOADS_BUCKET is set; otherwise every call is a no-op and
the caller falls back to its in-process dict (fine for local dev).
"""

import logging
import os
from typing import Optional

_logger = logging.getLogger(__name__)


class GCSFileStore:
    def __init__(self) -> None:
        self._bucket_name: str = os.environ.get("GCS_UPLOADS_BUCKET", "")
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(self._bucket_name)

    def _bucket(self):
        if self._client is None:
            from google.cloud import storage  # lazy import — avoids hard dep in local dev
            self._client = storage.Client()
        return self._client.bucket(self._bucket_name)

    def put(
        self,
        file_id: str,
        name: str,
        content,
        media_type: str,
        session_id: Optional[str] = None,
        is_upload: bool = False,
    ) -> None:
        """Persist a file. Best-effort: logs and swallows errors so a GCS outage
        never breaks the request (the in-process dict still serves it locally)."""
        if not self.enabled:
            return
        data = content.encode("utf-8") if isinstance(content, str) else content
        try:
            bucket = self._bucket()
            blob = bucket.blob(f"filestore/{file_id}")
            blob.metadata = {"name": name, "media_type": media_type}
            blob.upload_from_string(data, content_type=media_type)

            if is_upload and session_id:
                folder_blob = bucket.blob(f"sessions/{session_id}/{name}")
                folder_blob.upload_from_string(data, content_type=media_type)
            _logger.info("[gcs-file-store] stored %s (session=%s upload=%s)", file_id, session_id, is_upload)
        except Exception as exc:
            _logger.warning("[gcs-file-store] put failed for %s: %s", file_id, exc)

    def get(self, file_id: str) -> Optional[dict]:
        """Return {name, content(bytes), media_type} or None if absent/unavailable."""
        if not self.enabled:
            return None
        try:
            blob = self._bucket().blob(f"filestore/{file_id}")
            if not blob.exists():
                return None
            content = blob.download_as_bytes()
            blob.reload()
            meta = blob.metadata or {}
            return {
                "name": meta.get("name", file_id),
                "content": content,
                "media_type": meta.get("media_type", "application/octet-stream"),
            }
        except Exception as exc:
            _logger.warning("[gcs-file-store] get failed for %s: %s", file_id, exc)
            return None


_store = GCSFileStore()
