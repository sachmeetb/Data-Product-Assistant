"""Programmatic README generation for serving packages (skill + fallback).

The deterministic package files (runner, requirements, .env.example, dbt scaffold,
explore.sql) are written by ``serving_package``. This module adds the *contextual
documentation layer* — a README that names the product's actual views/models and
explains what it is — by calling a documenter skill (JSON ``{files}`` verdict,
same programmatic pattern as ``serving_strategy._run_advisor_skill``). Every
generator is **best-effort**: on any failure it returns ``None`` and the caller
falls back to ``serving_package``'s deterministic README. Knowledge about a good
README lives in the skill; the fallback guarantees a baseline.

Generators return a README markdown string (or ``None``). They are async (SDK
``query``); ``generate_*_readme_sync`` wrappers run them from sync call sites.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Optional


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

VIEW_SKILL = "data-serving-view-documenter"
DBT_SKILL = "data-serving-dbt-documenter"
LAKEHOUSE_SKILL = "data-serving-lakehouse-documenter"
DOC_TIMEOUT_SECONDS = 60


def _parse_files(text: str) -> dict[str, str]:
    """Pull ``{"files": {...}}`` (or a bare ``{"README.md": ...}``) from the skill
    output. Returns a ``{path: content}`` map (possibly empty)."""
    matches = _JSON_BLOCK_RE.findall(text or "")
    for m in reversed(matches):
        try:
            parsed = json.loads(m)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            files = parsed.get("files")
            if isinstance(files, dict):
                return {k: str(v) for k, v in files.items() if isinstance(v, str)}
            # tolerate a bare README field
            if isinstance(parsed.get("README.md"), str):
                return {"README.md": parsed["README.md"]}
    return {}


async def _run_documenter_skill(skill_name: str, payload: dict[str, Any]) -> dict[str, str]:
    """Invoke a documenter skill and return its ``{path: content}`` files map."""
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
    except ImportError:
        return {}
    from .config import BASE_DIR, PIPELINE_PLUGINS

    system_prompt = (
        f"FIRST: Load the `{skill_name}` skill via the Skill tool, then follow it "
        "exactly. Emit exactly ONE fenced json block of the form "
        '{"files": {"README.md": "<markdown>", ...}}. Do not write files. Do not run '
        "shell commands. No prose outside the json block."
    )
    user_prompt = (
        f"FIRST: Load the {skill_name} skill using the Skill tool.\n\n"
        f"INPUTS (JSON):\n{json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
        "Output the single fenced ```json block with a `files` map."
    )
    options = ClaudeAgentOptions(
        allowed_tools=["Read", "Skill"],
        permission_mode="acceptEdits",
        cwd=str(BASE_DIR),
        max_turns=4,
        skills="all",
        plugins=PIPELINE_PLUGINS,
        system_prompt={"type": "preset", "preset": "claude_code", "append": system_prompt},
    )
    parts: list[str] = []
    try:
        async for message in query(prompt=user_prompt, options=options):
            if message is None:
                continue
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
            elif isinstance(message, ResultMessage):
                try:
                    from .llm_usage import extract_usage, record_usage
                    record_usage(source="serving_docs", usage=extract_usage(message))
                except Exception:
                    pass
                if getattr(message, "is_error", False):
                    return {}
    except Exception:
        return {}
    return _parse_files("\n".join(parts))


async def generate_view_readme(
    *, product_name: str, description: str, platform: str,
    view_schema: str, view_names: list[str],
) -> Optional[str]:
    try:
        files = await asyncio.wait_for(
            _run_documenter_skill(VIEW_SKILL, {
                "kind": "view", "product_name": product_name, "description": description,
                "target_platform": platform, "view_schema": view_schema,
                "views": view_names,
            }),
            timeout=DOC_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, Exception):
        return None
    readme = files.get("README.md")
    return readme if isinstance(readme, str) and readme.strip() else None


async def generate_dbt_readme(
    *, product_name: str, description: str, platform: str, model_names: list[str],
    materialization: str = "table", has_snapshots: bool = False,
) -> Optional[str]:
    try:
        files = await asyncio.wait_for(
            _run_documenter_skill(DBT_SKILL, {
                "kind": "dbt", "product_name": product_name, "description": description,
                "target_platform": platform, "models": model_names,
                "materialization": materialization, "has_snapshots": has_snapshots,
            }),
            timeout=DOC_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, Exception):
        return None
    readme = files.get("README.md")
    return readme if isinstance(readme, str) and readme.strip() else None


async def generate_lakehouse_readme(
    *, product_name: str, description: str, models: list[dict],
    source_platform: Optional[str] = None,
) -> Optional[str]:
    try:
        files = await asyncio.wait_for(
            _run_documenter_skill(LAKEHOUSE_SKILL, {
                "kind": "lakehouse", "product_name": product_name,
                "description": description, "models": models,
                "source_platform": source_platform,
            }),
            timeout=DOC_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, Exception):
        return None
    readme = files.get("README.md")
    return readme if isinstance(readme, str) and readme.strip() else None


def _run_sync(coro) -> Optional[str]:
    """Run an async generator from a sync call site. Returns None if a loop is
    already running (caller should await the async form instead)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.run(coro)
        except Exception:
            return None
    coro.close()
    return None


def generate_view_readme_sync(**kw) -> Optional[str]:
    return _run_sync(generate_view_readme(**kw))


def generate_dbt_readme_sync(**kw) -> Optional[str]:
    return _run_sync(generate_dbt_readme(**kw))


def generate_lakehouse_readme_sync(**kw) -> Optional[str]:
    return _run_sync(generate_lakehouse_readme(**kw))
