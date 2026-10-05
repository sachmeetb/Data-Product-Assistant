"""Programmatic README generation for the migration package (skill + fallback).

Mirrors ``serving_docs`` — reuses its documenter harness (``_run_documenter_skill``)
to author a contextual README for the migration package via the
``migration-package-documenter`` skill, with ``serving_package``'s deterministic
fallback when the skill isn't installed or errors. Best-effort throughout.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from .serving_docs import DOC_TIMEOUT_SECONDS, _run_documenter_skill, _run_sync

MIGRATION_DOC_SKILL = "migration-package-documenter"


async def generate_migration_readme(*, project_code: str, spec: dict) -> Optional[str]:
    try:
        files = await asyncio.wait_for(
            _run_documenter_skill(MIGRATION_DOC_SKILL, {
                "kind": "migration",
                "project_code": project_code,
                "source_platform": spec.get("source_platform"),
                "target_platform": spec.get("target_platform"),
                "target_schema": spec.get("target_schema"),
                "write_disposition": spec.get("write_disposition"),
                "datasets": spec.get("datasets", []),
            }),
            timeout=DOC_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, Exception):
        return None
    readme = files.get("README.md")
    return readme if isinstance(readme, str) and readme.strip() else None


def generate_migration_readme_sync(**kw) -> Optional[str]:
    return _run_sync(generate_migration_readme(**kw))
