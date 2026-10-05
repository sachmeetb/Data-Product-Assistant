"""Bootstrap prompt — the ringer-style prompt-based onboarding.

Instead of a new user learning which curl installer to run, they fetch ONE
prompt and paste it into a fresh Claude Code; the prompt drives the whole setup
(persona → token → install kit → verify → hand off) itself.

    curl -fsSL <backend>/api/bootstrap            # persona-agnostic (asks)
    curl -fsSL <backend>/api/bootstrap?persona=po # or pre-pick the persona

The prompt file ships in the image (`bootstrap/` via the Dockerfile `COPY . .`).
Unauthenticated: it exposes only the public prompt markdown. The MCP endpoints it
points at stay token-gated. Pairs with engineer_kit.py + po_kit.py.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from ..config import BASE_DIR, public_base_url

router = APIRouter(prefix="/api", tags=["bootstrap"])

_PROMPT_PATH = BASE_DIR / "bootstrap" / "bootstrap-prompt.md"
_PLACEHOLDER = "__WORKBENCH_BASE_URL__"


@router.get("/bootstrap", response_class=PlainTextResponse)
def bootstrap_prompt(request: Request, persona: str = "") -> PlainTextResponse:
    """Return the paste-into-Claude-Code bootstrap prompt with this backend's base
    URL injected. Optional `persona` (po|de) pre-selects the kit so step 1 is
    skipped."""
    if not _PROMPT_PATH.exists():
        raise HTTPException(500, "bootstrap prompt not found on the server")
    base = public_base_url(request)
    body = _PROMPT_PATH.read_text().replace(_PLACEHOLDER, base)

    persona = (persona or "").strip().lower()
    if persona in ("po", "product-owner", "owner"):
        body = ("<!-- persona pre-selected: PRODUCT OWNER — skip step 1, use the "
                "po-kit installer -->\n\n") + body
    elif persona in ("de", "engineer", "data-engineer"):
        body = ("<!-- persona pre-selected: DATA ENGINEER — skip step 1, use the "
                "engineer-kit installer -->\n\n") + body

    return PlainTextResponse(
        body,
        media_type="text/markdown; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )
