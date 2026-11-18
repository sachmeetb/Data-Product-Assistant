"""
base.py — Shared agent runner for BFSI-Silver-Agent using Azure Databricks
Model Serving (or Azure OpenAI) via the OpenAI-compatible AsyncOpenAI client.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

LLM_MODEL = (
    os.environ.get("DATABRICKS_LLM_MODEL", "").strip()
    or os.environ.get("AZURE_OPENAI_DEPLOYMENT", "").strip()
    or os.environ.get("LLM_MODEL", "").strip()
    or "databricks-meta-llama-3-3-70b-instruct"
)
APP_NAME = os.environ.get("AGENT_APP_NAME", "bfsi-silver-agent")

_PROMPTS_DIR = _PROJECT_ROOT / "prompts"

# Agent prompt file registry
PROMPT_REGISTRY: dict[str, str] = {
    "bank-profile":                str(_PROMPTS_DIR / "bank_profile.md"),
    "requirement-understanding":   str(_PROMPTS_DIR / "requirement_understanding.md"),
    "domain-scoping":              str(_PROMPTS_DIR / "domain_scoping.md"),
    "silver-product-engine":       str(_PROMPTS_DIR / "silver_product_engine.md"),
    "spec-generator":              str(_PROMPTS_DIR / "spec_generator.md"),
    "validator":                   str(_PROMPTS_DIR / "validator.md"),
}

# Agent token budgets
_AGENT_MAX_TOKENS: dict[str, int] = {
    "bank-profile":               2048,
    "requirement-understanding":  4096,
    "domain-scoping":             8192,
    "silver-product-engine":     16384,
    "spec-generator":            24000,
    "validator":                  8192,
}
_DEFAULT_MAX_TOKENS = 4096

# In-memory history: (agent_name, session_id) -> list of OpenAI message dicts
_histories: dict[tuple[str, str], list[dict[str, str]]] = {}

_client_instance = None


def _get_client():
    global _client_instance
    if _client_instance is None:
        from tools.databricks_auth import get_async_llm_client

        _client_instance = get_async_llm_client()
        log.info("LLM client initialised: model=%s", LLM_MODEL)
    return _client_instance


# ── Prompt loading ────────────────────────────────────────────────────────────

def load_prompt(agent_name: str) -> str:
    """Load the system instruction for the given agent from its prompt file."""
    if agent_name not in PROMPT_REGISTRY:
        raise ValueError(
            f"Unknown agent '{agent_name}'. Registered: {list(PROMPT_REGISTRY)}"
        )
    path = PROMPT_REGISTRY[agent_name]
    with open(path, encoding="utf-8") as f:
        return f.read()


# ── Output parsing ────────────────────────────────────────────────────────────

def parse_agent_output(raw_text: str) -> dict:
    """
    Parse agent response into a structured dict.

    Tries in order:
    1. Strip code fences, parse as JSON
    2. Brace-match to extract leading JSON object
    3. Fall back to raw_output
    """
    text = (
        raw_text.strip()
        .removeprefix("```json")
        .removeprefix("```")
        .removesuffix("```")
        .strip()
    )

    # Pass 1 — direct parse
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        pass

    # Pass 2 — brace-match
    brace_depth = 0
    json_start = -1
    json_end = -1
    in_string = False
    escape_next = False

    for i, ch in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if brace_depth == 0:
                json_start = i
            brace_depth += 1
        elif ch == "}":
            brace_depth -= 1
            if brace_depth == 0:
                json_end = i + 1
                break

    if json_start >= 0 and json_end > 0:
        candidate = text[json_start:json_end]
        try:
            result = json.loads(candidate, strict=False)
            tail = text[json_end:].strip()
            if tail:
                result["display_output"] = tail
            return result
        except json.JSONDecodeError:
            pass

    # Pass 3 — regex fallback for ddl_script & specification extraction
    if "ddl_script" in text:
        extracted: dict = {}
        ddl_match = re.search(r'"ddl_script"\s*:\s*"(.*?)"\s*,\s*"specification"', text, re.DOTALL)
        if not ddl_match:
            ddl_match = re.search(r'"ddl_script"\s*:\s*"(.*?)"\s*(?:\}|$)', text, re.DOTALL)
        if ddl_match:
            ddl_val = ddl_match.group(1).replace("\\n", "\n").replace('\\"', '"')
            extracted["ddl_script"] = ddl_val

        spec_match = re.search(r'"specification"\s*:\s*(\{.*?\})\s*(?:\}|$)', text, re.DOTALL)
        if spec_match:
            try:
                extracted["specification"] = json.loads(spec_match.group(1), strict=False)
            except Exception:
                extracted["specification"] = {"summary": "Silver Schema Specification"}
        else:
            extracted["specification"] = {"summary": "Silver Schema Specification"}

        if "ddl_script" in extracted and len(extracted["ddl_script"].strip()) > 30:
            return extracted

    return {"raw_output": text}


# ── Core runner ───────────────────────────────────────────────────────────────

async def run_agent(
    agent_name: str,
    user_input: str,
    context: Optional[dict] = None,
    session_id: Optional[str] = None,
    user_id: str = "system",
) -> dict:
    """
    Invoke an LLM agent with system instructions and optional context.
    """
    if agent_name not in PROMPT_REGISTRY:
        return {"error": f"Unknown agent '{agent_name}'. Registered: {list(PROMPT_REGISTRY)}"}

    if session_id is None:
        session_id = str(uuid.uuid4())

    try:
        system_instruction = load_prompt(agent_name)
    except Exception as e:
        log.error("Failed to load prompt for %s: %s", agent_name, e)
        return {"error": f"Failed to load prompt for {agent_name}: {e}"}

    max_tokens = _AGENT_MAX_TOKENS.get(agent_name, _DEFAULT_MAX_TOKENS)

    if context:
        full_input = (
            f"<context>\n{json.dumps(context, indent=2, default=str)}\n</context>\n\n"
            f"{user_input}"
        )
    else:
        full_input = user_input

    history_key = (agent_name, session_id)
    history = _histories.get(history_key, [])
    new_user_msg = {"role": "user", "content": full_input}
    messages = [{"role": "system", "content": system_instruction}] + history + [new_user_msg]

    try:
        client = _get_client()
        response = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.2,
        )
        raw = (response.choices[0].message.content or "").strip() if response.choices else ""
        if not raw:
            return {"error": f"Agent '{agent_name}' returned empty response."}

        _histories[history_key] = history + [new_user_msg, {"role": "assistant", "content": raw}]
        return parse_agent_output(raw)

    except Exception as exc:
        log.error("Agent '%s' error: %s: %s", agent_name, type(exc).__name__, exc)
        return {"error": f"{type(exc).__name__}: {exc}"}


async def create_session(user_id: str = "system") -> str:
    """Create and return a new session ID."""
    return str(uuid.uuid4())
