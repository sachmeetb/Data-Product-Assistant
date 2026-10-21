"""Engineer-kit installer — serves a one-line `curl | bash` setup from the backend.

A data engineer can stand up the Workbench MCP connection + the `workbench-guide`
skill + `/workbench-*` slash commands in their own project without git access:

    curl -fsSL <backend>/api/engineer-kit/install.sh | WORKBENCH_TOKEN=<tok> bash -s -- [dir]

The kit files ship in the image (`engineer-kit/` via the Dockerfile `COPY . .`).
Both endpoints are unauthenticated: they expose only public repo assets (the
script + skill/command markdown). The bearer token is supplied by the engineer
at runtime and never embedded here. The MCP endpoint itself (`/mcp`) stays
token-gated.
"""

from __future__ import annotations

import io
import tarfile

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from ..config import BASE_DIR, public_base_url

router = APIRouter(prefix="/api/engineer-kit", tags=["engineer-kit"])

_KIT_DIR = BASE_DIR / "engineer-kit"
_PLACEHOLDER = "__WORKBENCH_BASE_URL__"


@router.get("/install.sh", response_class=PlainTextResponse)
def install_script(request: Request) -> PlainTextResponse:
    """Return the installer with this backend's base URL injected, so the
    script knows where to fetch the bundle + which MCP URL to register."""
    script_path = _KIT_DIR / "install.sh"
    if not script_path.exists():
        raise HTTPException(500, "engineer-kit installer not found on the server")
    base = public_base_url(request)
    body = script_path.read_text().replace(_PLACEHOLDER, base)
    return PlainTextResponse(
        body,
        media_type="text/x-shellscript",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/bundle.tar.gz")
def bundle() -> Response:
    """Tar.gz of the kit's skills/ + commands/ (arcnames rooted so it extracts
    straight into a project's .claude/). Excludes .mcp.json — the installer
    writes that itself with the concrete URL."""
    buf = io.BytesIO()
    added = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for sub in ("skills", "commands"):
            src = _KIT_DIR / sub
            if src.is_dir():
                tar.add(src, arcname=sub)
                added += 1
        # AGENTS.md at the tar root — the installer places it at the project
        # root (Codex / generic MCP clients read it; Claude Code ignores it).
        agents = _KIT_DIR / "AGENTS.md"
        if agents.is_file():
            tar.add(agents, arcname="AGENTS.md")
            added += 1
    if added == 0:
        raise HTTPException(500, "engineer-kit assets not found on the server")
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/gzip",
        headers={
            "Content-Disposition": 'attachment; filename="engineer-kit-bundle.tar.gz"',
            "Cache-Control": "no-store",
        },
    )
