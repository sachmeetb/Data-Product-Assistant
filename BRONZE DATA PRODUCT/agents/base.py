"""
base.py — Shared agent runner for BFSI-Bronze-Agent using google.genai directly (Vertex AI).

Provides:
  - Vertex AI initialisation via google.genai.Client
  - Direct Gemini model execution bypassing ADK
  - In-memory conversation history per session
  - Robust structured JSON output parsing
  - OpenTelemetry tracing
"""

from __future__ import annotations

import asyncio
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

GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
GCP_LOCATION = os.environ.get("GCP_LOCATION", "us-central1")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
APP_NAME = os.environ.get("AGENT_APP_NAME", "bfsi-bronze-agent")
GCP_CREDENTIALS_PATH = os.environ.get("GCP_CREDENTIALS_PATH", "")

_PROMPTS_DIR = _PROJECT_ROOT / "prompts"

# Agent prompt file registry
PROMPT_REGISTRY: dict[str, str] = {
    "bank-profile":              str(_PROMPTS_DIR / "bank_profile.md"),
    "requirement-understanding": str(_PROMPTS_DIR / "requirement_understanding.md"),
    "source-scoping":            str(_PROMPTS_DIR / "source_scoping.md"),
    "bronze-product-engine":     str(_PROMPTS_DIR / "bronze_product_engine.md"),
    "spec-generator":            str(_PROMPTS_DIR / "spec_generator.md"),
    "validator":                 str(_PROMPTS_DIR / "validator.md"),
}

# Agent token budgets
_AGENT_MAX_TOKENS: dict[str, int] = {
    "bank-profile":              4096,
    "requirement-understanding": 8000,
    "source-scoping":            12000,
    "bronze-product-engine":     16000,
    "spec-generator":            24000,
    "validator":                 8000,
}
_DEFAULT_MAX_TOKENS = 4096

# In-memory history: (agent_name, session_id) -> list of Content
_histories: dict[tuple[str, str], list] = {}

# ── google.genai Client ───────────────────────────────────────────────────────

import google.genai as genai
from google.genai.types import Content, GenerateContentConfig, Part

_client_instance: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client_instance
    if _client_instance is None:
        if GCP_PROJECT_ID and not os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"):
            os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "1"
        _client_instance = genai.Client()
        log.info(
            "google.genai Client initialised: project=%s location=%s model=%s",
            GCP_PROJECT_ID,
            GCP_LOCATION,
            GEMINI_MODEL,
        )
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

def _escape_newlines_in_strings(text: str) -> str:
    result = []
    in_string = False
    escape_next = False
    for ch in text:
        if escape_next:
            escape_next = False
            result.append(ch)
        elif ch == "\\" and in_string:
            escape_next = True
            result.append(ch)
        elif ch == '"':
            in_string = not in_string
            result.append(ch)
        elif ch == "\n" and in_string:
            result.append("\\n")
        elif ch == "\r" and in_string:
            result.append("\\r")
        else:
            result.append(ch)
    return "".join(result)


def parse_agent_output(raw_text: str) -> dict:
    """
    Parse agent response into a structured dict.
    Handles markdown fences, embedded JSON, and trailing commentary.
    """
    text = (
        raw_text.strip()
        .removeprefix("```json")
        .removeprefix("```")
        .removesuffix("```")
        .strip()
    )

    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        pass

    try:
        return json.loads(_escape_newlines_in_strings(text), strict=False)
    except json.JSONDecodeError:
        pass

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
        if ch == '"' and not escape_next:
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
        for attempt in (candidate, _escape_newlines_in_strings(candidate)):
            try:
                result = json.loads(attempt, strict=False)
                tail = text[json_end:].strip()
                if tail:
                    result["display_output"] = tail
                return result
            except json.JSONDecodeError:
                pass

    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        greedy = text[first:last + 1]
        greedy = re.sub(r",(\s*[}\]])", r"\1", greedy)
        for attempt in (greedy, _escape_newlines_in_strings(greedy)):
            try:
                return json.loads(attempt, strict=False)
            except json.JSONDecodeError:
                pass

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
                extracted["specification"] = {"summary": "Bronze Schema Specification"}
        else:
            extracted["specification"] = {"summary": "Bronze Schema Specification"}

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
    Invoke a Google Gemini agent with system instructions and optional context.

    Args:
        agent_name: Key in PROMPT_REGISTRY.
        user_input: The prompt / instruction to send.
        context:    Optional dict injected as a <context> block.
        session_id: Session ID for multi-turn history.
        user_id:    User identifier for session tracking.

    Returns:
        Parsed structured dict from the agent's response.
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

    max_tok = _AGENT_MAX_TOKENS.get(agent_name, _DEFAULT_MAX_TOKENS)

    if context:
        user_msg = (
            f"<context>\n{json.dumps(context, indent=2, default=str)}\n</context>\n\n"
            f"{user_input}"
        )
    else:
        user_msg = user_input

    history_key = (agent_name, session_id)
    history = _histories.get(history_key, [])
    new_user_content = Content(role="user", parts=[Part(text=user_msg)])
    contents = history + [new_user_content]

    try:
        client = _get_client()
        cfg_kwargs = {
            "system_instruction": system_instruction,
            "max_output_tokens": max_tok,
            "temperature": 0.2,
        }

        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=GenerateContentConfig(**cfg_kwargs),
        )
        result_text = response.text or ""

        # Persist conversation turn
        model_content = Content(role="model", parts=[Part(text=result_text)])
        _histories[history_key] = history + [new_user_content, model_content]

        return parse_agent_output(result_text)

    except Exception as exc:
        log.error("Agent '%s' execution error: %s: %s", agent_name, type(exc).__name__, exc, exc_info=True)
        return {"error": f"{type(exc).__name__}: {exc}"}


async def create_session(user_id: str = "system") -> str:
    """Create and return a new session ID."""
    return str(uuid.uuid4())
