"""Skills registry endpoint.

Reads ``name`` and ``description`` from the YAML frontmatter of every
in-repo ``workbench-skills/skills/*/SKILL.md`` (see config.SKILLS_DIR). Used by the chat panels to render
human-readable labels for ``Skill`` and skill-script ``Bash`` tool calls
instead of raw skill slugs and command lines.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter

router = APIRouter(prefix="/api/skills", tags=["skills"])


from ..config import SKILLS_DIR

_SKILLS_ROOT = SKILLS_DIR
_cache: list[dict[str, Any]] | None = None


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Extract the ``---``-fenced YAML block at the top of a markdown file.
    Returns ``{}`` if the file doesn't open with a frontmatter fence."""
    if not text.startswith("---"):
        return {}
    # Find the closing fence on its own line.
    rest = text[3:]
    end = rest.find("\n---")
    if end < 0:
        return {}
    block = rest[:end]
    try:
        data = yaml.safe_load(block) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _scan_skills() -> list[dict[str, Any]]:
    if not _SKILLS_ROOT.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for child in sorted(_SKILLS_ROOT.iterdir()):
        if not child.is_dir():
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
        except Exception:
            continue
        meta = _parse_frontmatter(text)
        name = (meta.get("name") or child.name).strip()
        description = (meta.get("description") or "").strip()
        out.append({
            "slug": child.name,
            "name": name,
            "description": description,
        })
    return out


@router.get("")
def list_skills(refresh: bool = False) -> dict[str, Any]:
    """Return ``{"skills": [{slug, name, description}, ...]}``.

    Cached for the process lifetime — skills rarely change while the
    server is running. Pass ``?refresh=true`` to bust the cache.
    """
    global _cache
    if refresh or _cache is None:
        _cache = _scan_skills()
    return {"skills": _cache}
