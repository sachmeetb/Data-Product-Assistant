"""Chat-attachment helper shared by Engineering and Product chat routers.

Workflow:
1. Frontend picks a file (text/json/csv only this iteration), reads it
   as base64, and sends it on the WS ``send_message`` payload under
   ``attachments: [{filename, content_base64, mime?}]``.
2. The router calls :func:`materialise_attachments` which validates each
   entry, writes the bytes to a per-session scratch directory under the
   project's CWD (or BASE_DIR for pre-project chats), and returns the
   list of materialised file paths plus a short head() snippet per file.
3. The router calls :func:`format_attachment_preamble` to produce a
   markdown block prepended to the prompt sent to the SDK. The agent is
   instructed to read the files via the Read tool when it needs the
   full content.

Files persist for the life of the chat process (temp dir cleanup is
deferred — small overhead, simpler).
"""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import Any, Optional


ALLOWED_EXTENSIONS = {".txt", ".json", ".csv"}
MAX_BYTES_PER_FILE = 2 * 1024 * 1024  # 2 MB
MAX_FILES_PER_TURN = 10
HEAD_BYTES = 600  # bytes of preview included inline in the prompt
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class AttachmentError(ValueError):
    """Raised when an attachment is malformed or violates the size/type
    limits. Routers translate this into a WS ``error`` event."""


def _safe_filename(filename: str) -> str:
    name = SAFE_FILENAME_RE.sub("_", (filename or "").strip())
    if not name:
        name = "attachment.bin"
    # Collapse leading dots so the file isn't hidden / treated as
    # directory traversal.
    while name.startswith("."):
        name = name[1:] or "attachment.bin"
    return name[:120]


def _scratch_dir(scope_dir: str | Path, session_token: str) -> Path:
    base = Path(scope_dir) / "chat-attachments" / session_token
    base.mkdir(parents=True, exist_ok=True)
    return base


def materialise_attachments(
    raw: Any,
    scope_dir: str | Path,
    session_token: str,
) -> list[dict[str, str]]:
    """Validate + write each attachment to disk. Returns a list of dicts
    with ``path``, ``filename``, ``size`` and ``snippet`` keys ready for
    :func:`format_attachment_preamble`. Empty list when nothing was
    supplied. Raises :class:`AttachmentError` on validation failure.
    """
    if not raw:
        return []
    if not isinstance(raw, list):
        raise AttachmentError("attachments must be a list")
    if len(raw) > MAX_FILES_PER_TURN:
        raise AttachmentError(f"Too many attachments (max {MAX_FILES_PER_TURN}/turn)")

    out: list[dict[str, str]] = []
    target_dir = _scratch_dir(scope_dir, _safe_filename(session_token) or "default")

    for entry in raw:
        if not isinstance(entry, dict):
            raise AttachmentError("each attachment must be an object")
        filename = _safe_filename(str(entry.get("filename") or "attachment.txt"))
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise AttachmentError(
                f"Unsupported file extension '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}."
            )
        b64 = entry.get("content_base64") or ""
        if not isinstance(b64, str):
            raise AttachmentError(f"{filename}: content_base64 must be a string")
        try:
            data = base64.b64decode(b64, validate=True)
        except Exception as e:
            raise AttachmentError(f"{filename}: failed to decode base64 ({e})")
        if len(data) > MAX_BYTES_PER_FILE:
            raise AttachmentError(f"{filename}: file too large (max {MAX_BYTES_PER_FILE // 1024 // 1024} MB)")

        path = target_dir / filename
        path.write_bytes(data)

        snippet = _make_snippet(data)
        out.append({
            "path": str(path),
            "filename": filename,
            "size": str(len(data)),
            "snippet": snippet,
        })

    return out


def _make_snippet(data: bytes) -> str:
    """Return a UTF-8 text preview of the first HEAD_BYTES bytes,
    falling back to a placeholder for binary content."""
    chunk = data[:HEAD_BYTES]
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError:
        return "(binary or non-UTF-8 content; agent should Read the file directly)"
    text = text.strip("﻿").rstrip()
    if len(data) > HEAD_BYTES:
        text += "\n…(truncated; full content available at the path above)"
    return text


def format_attachment_preamble(materialised: list[dict[str, str]]) -> Optional[str]:
    """Produce a markdown block to prepend to the prompt. Returns None
    when there are no attachments."""
    if not materialised:
        return None
    lines: list[str] = ["## Attached files", ""]
    lines.append(
        "The user attached the following files for context. Read them via the "
        "**Read** tool when you need the full content. Each file's first "
        "few hundred bytes is shown below for quick context."
    )
    for f in materialised:
        lines.append("")
        lines.append(f"### `{f['filename']}` ({f['size']} bytes)")
        lines.append(f"Path: `{f['path']}`")
        lines.append("")
        lines.append("```")
        lines.append(f["snippet"])
        lines.append("```")
    return "\n".join(lines)
