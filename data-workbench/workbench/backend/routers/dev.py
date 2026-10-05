"""Local-dev helper endpoints.

Currently exposes the **quick-connect** manifest the `dwb` launcher CLI writes
when it brings up a sample database. The frontend connection forms
(`DataSourceDialog`, `ConnectionsPage`) fetch it to offer a one-click prefill so
a launched HR-Postgres / HR-MySQL is wired into a project without hand-typing
host/port/db/user/password every demo.

This is deliberately unauthenticated and read-only — it exposes ONLY the
throwaway demo credentials the CLI itself provisioned, never anything from the
graph or the app database. On a read-only instance (a hosted/shared demo box)
it returns an empty list so the "Quick connect" UI simply doesn't appear.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter

from .. import config

router = APIRouter(prefix="/api/dev", tags=["dev"])


@router.get("/provisioned-sources")
def provisioned_sources():
    """Return the CLI-provisioned sample-DB coordinates for form prefill.

    Shape: ``{"sources": [{name, platform, host, port, database, username,
    password, schema, sample, mode}, ...]}``. Empty list when no manifest exists
    or the instance is globally read-only. Never raises — a malformed manifest
    degrades to an empty list rather than 500-ing the connection screen.
    """
    if config.read_only():
        return {"sources": []}

    path = Path(config.PROVISIONED_SOURCES_PATH)
    if not path.is_file():
        return {"sources": []}

    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"sources": []}

    # Accept either a bare list or a wrapped {"sources": [...]}.
    if isinstance(data, dict):
        data = data.get("sources", [])
    if not isinstance(data, list):
        return {"sources": []}

    return {"sources": data}
