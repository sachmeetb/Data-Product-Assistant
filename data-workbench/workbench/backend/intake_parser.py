"""Isolated parser: inbound envelope → confidence-graded scaffold blueprint.

**Security boundary (hardened plan, finding 6).** The envelope is
attacker-controlled assessment text, so the parser runs with the *minimum*
capability: the SDK call is made with **no tools, no plugins, no skills** — the
model sees only the instructions + the envelope and must return one fenced JSON
block. The output is then validated against ``intake_blueprint`` and a mismatch
becomes ``parse_failed`` — we never default-fill required fields.

The parser instructions are authored as a real, versioned skill at
``workbench-skills/skills/intake-scaffold-parser/SKILL.md`` (same convention as
the other pipeline skills), but we load its **body text** here and run it
tool-less — it is NEVER invoked via the Skill tool, so untrusted content never
reaches a tool-enabled agent. ``build_instructions`` = the skill body + a
one-line scenario directive.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from . import intake_blueprint as bp
from .config import SKILLS_DIR

# Last fenced ```json block (tolerates surrounding prose the model may add).
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

_SKILL_PATH = SKILLS_DIR / "intake-scaffold-parser" / "SKILL.md"
# Strip a leading YAML frontmatter block (--- ... ---) so only the body is used.
_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)

_skill_body_cache: Optional[str] = None


def _load_skill_instructions() -> str:
    """Read and cache the parser skill's SKILL.md body (frontmatter stripped).

    Raises FileNotFoundError / ValueError if the skill is missing or empty — the
    caller turns that into ``parse_failed`` rather than fabricating a blueprint.
    """
    global _skill_body_cache
    if _skill_body_cache is not None:
        return _skill_body_cache
    text = _SKILL_PATH.read_text(encoding="utf-8")
    body = _FRONTMATTER_RE.sub("", text, count=1).strip()
    if not body:
        raise ValueError(f"intake-scaffold-parser SKILL.md has no body: {_SKILL_PATH}")
    _skill_body_cache = body
    return body


def build_instructions(scenario: str) -> str:
    """Skill body + the one-line scenario directive the skill expects."""
    body = _load_skill_instructions()
    return f"{body}\n\nSCENARIO: {scenario} — emit exactly that scenario's shape."


def _envelope_to_text(envelope: dict[str, Any]) -> str:
    """Flatten the envelope's typed content parts into a bounded prompt string."""
    parts = []
    hints = envelope.get("hints") or {}
    if hints:
        parts.append("HINTS: " + json.dumps(hints, default=str))
    for i, item in enumerate(envelope.get("content") or []):
        if not isinstance(item, dict):
            continue
        kind = item.get("kind", "text")
        title = item.get("title", f"part-{i}")
        body = item.get("body", "")
        if not isinstance(body, str):
            body = json.dumps(body, default=str)
        parts.append(f"--- CONTENT [{kind}] {title} ---\n{body}")
    return "\n\n".join(parts) if parts else "(empty envelope)"


def _extract_json(text: str) -> Optional[dict[str, Any]]:
    matches = _JSON_BLOCK_RE.findall(text or "")
    candidate = matches[-1] if matches else (text or "").strip()
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def _run_parser_llm(instructions: str, envelope_text: str) -> tuple[str, Optional[str]]:
    """One-shot, capability-minimized SDK call. Returns (transcript, error).

    Isolated: allowed_tools=[], no plugins, no skills, no preset system prompt.
    """
    try:
        from claude_agent_sdk import (  # type: ignore
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )
    except ImportError:
        return "", "claude-agent-sdk is not installed"

    from .config import BASE_DIR

    options = ClaudeAgentOptions(
        allowed_tools=[],            # no Read/Bash/Skill — nothing
        permission_mode="default",
        cwd=str(BASE_DIR),
        max_turns=1,
        system_prompt=instructions,  # plain string, NOT the claude_code preset
    )

    transcript: list[str] = []
    error: Optional[str] = None
    try:
        async for message in query(prompt=envelope_text, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        transcript.append(block.text)
            elif isinstance(message, ResultMessage):
                try:
                    from . import llm_usage
                    llm_usage.record_usage(
                        source="intake_parser",
                        usage=llm_usage.extract_usage(message),
                    )
                except Exception:
                    pass
                if getattr(message, "is_error", False):
                    error = "parser run returned an error result"
    except Exception as e:  # SDK/transport failure
        error = f"parser invocation failed: {e}"
    return "".join(transcript), error


async def parse_envelope(
    envelope: dict[str, Any], scenario: str
) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """Parse an envelope into a validated blueprint dict.

    Returns ``(blueprint_dict, meta)``. On any failure ``blueprint_dict`` is
    ``None`` and ``meta['error']`` explains why (the caller sets
    ``status='parse_failed'``). ``blueprint_dict`` is only returned after it
    passes ``intake_blueprint.parse_blueprint`` — no default-fill.
    """
    meta: dict[str, Any] = {"scenario": scenario, "parser": "intake-scaffold-parser"}
    try:
        instructions = build_instructions(scenario)
    except (OSError, ValueError) as e:
        meta["error"] = f"parser instructions unavailable: {e}"
        return None, meta
    envelope_text = _envelope_to_text(envelope)
    transcript, error = await _run_parser_llm(instructions, envelope_text)
    if error:
        meta["error"] = error
        return None, meta

    raw = _extract_json(transcript)
    if raw is None:
        meta["error"] = "parser produced no valid JSON block"
        return None, meta

    # Force the reviewed scenario so a mismatched discriminator can't slip through.
    raw["scenario"] = scenario
    try:
        model = bp.parse_blueprint(raw)
    except bp.BlueprintValidationError as e:
        meta["error"] = f"blueprint failed schema validation: {e}"
        return None, meta
    bp.normalize_ids(model)
    return model.model_dump(mode="json"), meta
