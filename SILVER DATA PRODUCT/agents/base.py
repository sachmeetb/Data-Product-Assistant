"""
base.py \u2014 Shared Google ADK agent runner for BFSI-Silver-Agent.

Provides:
  - Vertex AI initialisation
  - Agent factory (creates LlmAgent instances from prompt files)
  - Session management (InMemorySessionService for dev, Redis for production)
  - Agent runner helper with structured JSON output parsing
  - OpenTelemetry tracing (optional)
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

# \u2500\u2500 Configuration \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
GCP_LOCATION = os.environ.get("GCP_LOCATION", "us-central1")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash-001")
APP_NAME = os.environ.get("AGENT_APP_NAME", "bfsi-silver-agent")
GCP_CREDENTIALS_PATH = os.environ.get("GCP_CREDENTIALS_PATH", "")

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

# \u2500\u2500 Vertex AI initialisation \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

_vertex_initialised = False


def _ensure_vertex_ai() -> None:
    """Initialise Vertex AI once per process."""
    global _vertex_initialised
    if _vertex_initialised:
        return
    if not GCP_PROJECT_ID:
        raise EnvironmentError(
            "GCP_PROJECT_ID is not set. Add it to BFSI-Silver-Agent/.env"
        )
    from tools.gcp_auth import get_vertexai_credentials
    get_vertexai_credentials(
        credentials_path=GCP_CREDENTIALS_PATH or None,
        project_id=GCP_PROJECT_ID,
        location=GCP_LOCATION,
    )
    _vertex_initialised = True
    log.info("Vertex AI initialised: project=%s location=%s model=%s",
             GCP_PROJECT_ID, GCP_LOCATION, GEMINI_MODEL)


# \u2500\u2500 Google ADK session service \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

_session_service = None


def _get_session_service():
    """Return or create the ADK session service (InMemory for dev)."""
    global _session_service
    if _session_service is None:
        from google.adk.sessions import InMemorySessionService
        _session_service = InMemorySessionService()
    return _session_service


# \u2500\u2500 Prompt loading \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

def load_prompt(agent_name: str) -> str:
    """Load the system instruction for the given agent from its prompt file."""
    if agent_name not in PROMPT_REGISTRY:
        raise ValueError(
            f"Unknown agent '{agent_name}'. Registered: {list(PROMPT_REGISTRY)}"
        )
    path = PROMPT_REGISTRY[agent_name]
    with open(path, encoding="utf-8") as f:
        return f.read()


# \u2500\u2500 Agent factory \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

_agents_cache: dict = {}


def get_agent(agent_name: str):
    """
    Return a cached Google ADK LlmAgent for the given name.
    Creates and caches on first call.
    """
    if agent_name not in _agents_cache:
        _ensure_vertex_ai()
        from google.adk.agents import LlmAgent

        from google.genai import types as genai_types

        instruction = load_prompt(agent_name)
        max_tokens = _AGENT_MAX_TOKENS.get(agent_name, _DEFAULT_MAX_TOKENS)

        agent = LlmAgent(
            name=agent_name.replace("-", "_"),
            model=GEMINI_MODEL,
            instruction=instruction,
            generate_content_config=genai_types.GenerateContentConfig(
                max_output_tokens=max_tokens,
                temperature=0.2,
            ),
        )
        _agents_cache[agent_name] = agent
        log.info("Agent created: %s (model=%s, max_tokens=%d)",
                 agent_name, GEMINI_MODEL, max_tokens)

    return _agents_cache[agent_name]


# \u2500\u2500 Output parsing \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

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


# \u2500\u2500 Core runner \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

async def run_agent(
    agent_name: str,
    user_input: str,
    context: Optional[dict] = None,
    session_id: Optional[str] = None,
    user_id: str = "system",
) -> dict:
    """
    Invoke a Google ADK LlmAgent with an optional context dict.

    Args:
        agent_name: Key in PROMPT_REGISTRY.
        user_input: The prompt / instruction to send.
        context:    Optional dict injected as a <context> block.
        session_id: ADK session ID (creates new session if None).
        user_id:    User identifier for session tracking.

    Returns:
        Parsed structured dict from the agent's response.
    """
    from google.adk.runners import Runner
    from google.genai import types as genai_types

    agent = get_agent(agent_name)
    session_service = _get_session_service()
    runner = Runner(
        agent=agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    # Create or reuse session
    if session_id is None:
        session_id = str(uuid.uuid4())
        await session_service.create_session(
            app_name=APP_NAME,
            user_id=user_id,
            session_id=session_id,
        )
    else:
        try:
            existing = await session_service.get_session(
                app_name=APP_NAME,
                user_id=user_id,
                session_id=session_id,
            )
            if existing is None:
                await session_service.create_session(
                    app_name=APP_NAME,
                    user_id=user_id,
                    session_id=session_id,
                )
        except Exception:
            try:
                await session_service.create_session(
                    app_name=APP_NAME,
                    user_id=user_id,
                    session_id=session_id,
                )
            except Exception:
                pass

    # Inject context into the user message
    if context:
        full_input = (
            f"<context>\n{json.dumps(context, indent=2, default=str)}\n</context>\n\n"
            f"{user_input}"
        )
    else:
        full_input = user_input

    message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=full_input)],
    )

    try:
        collected_text = []
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=message,
        ):
            if event.is_final_response() and event.content and event.content.parts:
                for part in event.content.parts:
                    if hasattr(part, "text") and part.text:
                        collected_text.append(part.text)

        raw = "".join(collected_text).strip()
        if not raw:
            return {"error": f"Agent '{agent_name}' returned empty response."}

        return parse_agent_output(raw)

    except Exception as exc:
        log.error("Agent '%s' error: %s: %s", agent_name, type(exc).__name__, exc)
        return {"error": f"{type(exc).__name__}: {exc}"}


async def create_session(user_id: str = "system") -> str:
    """Create and return a new ADK session ID."""
    session_service = _get_session_service()
    session_id = str(uuid.uuid4())
    await session_service.create_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=session_id,
    )
    return session_id
