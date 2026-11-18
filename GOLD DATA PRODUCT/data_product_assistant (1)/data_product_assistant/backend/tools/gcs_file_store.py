"""
gcs_file_store.py — durable, id-addressable file store backed by Azure Databricks
Unity Catalog Volumes (replacing GCS).

Enabled when UC_UPLOADS_VOLUME is set (e.g. "/Volumes/main/default/uploads");
otherwise every call is a no-op and the caller falls back to its in-process dict.
"""

import io
import json
import logging
import os
from typing import Optional

_logger = logging.getLogger(__name__)


class GCSFileStore:
    """Durable file store backed by Databricks Unity Catalog Volumes."""

    def __init__(self) -> None:
        self._volume_path: str = os.environ.get("UC_UPLOADS_VOLUME", "").rstrip("/")
        self._wc = None

    @property
    def enabled(self) -> bool:
        return bool(self._volume_path)

    def _client(self):
        if self._wc is None:
            from tools.databricks_auth import get_workspace_client
            self._wc = get_workspace_client()
        return self._wc

    def put(
        self,
        file_id: str,
        name: str,
        content,
        media_type: str,
        session_id: Optional[str] = None,
        is_upload: bool = False,
    ) -> None:
        if not self.enabled:
            return
        data = content.encode("utf-8") if isinstance(content, str) else content
        try:
            wc = self._client()
            wc.files.upload(f"{self._volume_path}/filestore/{file_id}", io.BytesIO(data), overwrite=True)
            meta = json.dumps({"name": name, "media_type": media_type}).encode("utf-8")
            wc.files.upload(f"{self._volume_path}/filestore/{file_id}.meta.json", io.BytesIO(meta), overwrite=True)
            if is_upload and session_id:
                wc.files.upload(f"{self._volume_path}/sessions/{session_id}/{name}", io.BytesIO(data), overwrite=True)
            _logger.info("[uc-volume-store] stored %s (session=%s upload=%s)", file_id, session_id, is_upload)
        except Exception as exc:
            _logger.warning("[uc-volume-store] put failed for %s: %s", file_id, exc)

    def get(self, file_id: str) -> Optional[dict]:
        if not self.enabled:
            return None
        try:
            wc = self._client()
            resp = wc.files.download(f"{self._volume_path}/filestore/{file_id}")
            content = resp.contents.read()
            meta = {}
            try:
                m_resp = wc.files.download(f"{self._volume_path}/filestore/{file_id}.meta.json")
                meta = json.loads(m_resp.contents.read().decode("utf-8"))
            except Exception:
                pass
            return {
                "name": meta.get("name", file_id),
                "content": content,
                "media_type": meta.get("media_type", "application/octet-stream"),
            }
        except Exception as exc:
            _logger.warning("[uc-volume-store] get failed for %s: %s", file_id, exc)
            return None


UCVolumeFileStore = GCSFileStore
