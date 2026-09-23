"""
base.py — shared agent runner using google.genai directly (Vertex AI).

Replaces the ADK LlmAgent/Runner stack. ADK 0.5.x's instruction template
engine applies format_map(session.state) to every instruction string — even
callable-wrapped ones — causing KeyError on SKILL.md files that contain JSON
examples with bare {field} notation. Bypassing ADK eliminates the problem.

Thread model
------------
  1. Server creates ONE conversation thread per agent-skill per HTTP session
     via create_thread(skill_name, thread_id=<http_session_id>).
  2. The thread_id (plain string) is passed to run_agent() on every call.
  3. Multi-turn history is stored in _histories[(skill_name, thread_id)]
     as a list of Content objects. This lives in process memory — identical
     to InMemorySessionService but without the template-engine overhead.
  4. With --min-instances=1 on Cloud Run, one warm instance holds all sessions
     and history survives across requests. Scale-out would need Redis-backed
     history (future work).
"""

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

# ── Bootstrap: load .env and set GCP env vars before any google imports ───────
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

_GCP_PROJECT  = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
_GCP_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
# Gemini model calls use their own location, decoupled from the data/BQ region.
# Gemini 3.x models (e.g. gemini-3.8-flash) are only served from the "global"
# Vertex location; gemini-2.5-pro is available there too, so route all model
# traffic to global while BigQuery/GCS stay in GOOGLE_CLOUD_LOCATION.
_GENAI_LOCATION = os.environ.get("GOOGLE_GENAI_LOCATION", "global")

# google.genai reads this env var when the Client is constructed to decide
# whether to route to Vertex AI or Google AI Studio.
if _GCP_PROJECT and not os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"):
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "1"

# ── Google packages (after env vars are set) ──────────────────────────────────
import google.genai as genai
from google.genai.types import Content, GenerateContentConfig, Part
try:
    # Available in google-genai >= 1.x; guarded so an older SDK can't crash the
    # runner at import time (we simply skip the thinking cap if unavailable).
    from google.genai.types import ThinkingConfig
except ImportError:  # pragma: no cover
    ThinkingConfig = None
from opentelemetry import trace
from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

if _GCP_PROJECT:
    _trace_provider = TracerProvider()
    _trace_provider.add_span_processor(
        BatchSpanProcessor(CloudTraceSpanExporter(project_id=_GCP_PROJECT))
    )
    trace.set_tracer_provider(_trace_provider)

tracer = trace.get_tracer("agents")

# ── Skill registry ────────────────────────────────────────────────────────────

SKILL_REGISTRY = {
    "orchestrator":              str(_PROJECT_ROOT / "prompts/orchestrator/SKILL.md"),
    # DPB agents
    "pipeline-generator":        str(_PROJECT_ROOT / "prompts/DPB/pipeline-generator/SKILL.md"),
    "test-agent":                str(_PROJECT_ROOT / "prompts/DPB/test-agent/SKILL.md"),
    "publisher":                 str(_PROJECT_ROOT / "prompts/DPB/publisher/SKILL.md"),
    # DPI agents
    "requirement-understanding": str(_PROJECT_ROOT / "prompts/DPI/requirement-understanding/SKILL.md"),
    "use-case-classification":   str(_PROJECT_ROOT / "prompts/DPI/use-case-classification/SKILL.md"),
    "challenger":                str(_PROJECT_ROOT / "prompts/DPI/challenger/SKILL.md"),
    "kpi-derivation":            str(_PROJECT_ROOT / "prompts/DPI/kpi-derivation/SKILL.md"),
    # DDI agents
    "gold-er":                   str(_PROJECT_ROOT / "prompts/DDI/gold-er/SKILL.md"),
    "silver-sttm":               str(_PROJECT_ROOT / "prompts/DDI/silver-sttm/SKILL.md"),
    "silver-transformation":     str(_PROJECT_ROOT / "prompts/DDI/silver-transformation/SKILL.md"),
    "gold-final":                str(_PROJECT_ROOT / "prompts/DDI/gold-final/SKILL.md"),
}

_SKILL_MAX_TOKENS: dict[str, int] = {
    "pipeline-generator":        16000,
    "test-agent":                12000,   # large sample_query_result JSON — needs headroom
    "challenger":                8000,    # 5 checks + summary + design_queue (was default 4096 → truncated → empty PDF)
    "publisher":                 8000,
    "requirement-understanding": 8000,
    "gold-er":                   12000,
    "silver-sttm":               10000,
    "silver-transformation":     16000,
    "gold-final":                20000,
}
_DEFAULT_MAX_TOKENS = 4096

_DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
_FLASH_MODEL   = os.getenv("GEMINI_FLASH_MODEL", "gemini-3.8-flash")

_SKILL_MODEL: dict[str, str] = {
    # Pro: heavy structured JSON output
    "gold-er":                   _DEFAULT_MODEL,
    "silver-sttm":               _DEFAULT_MODEL,
    "silver-transformation":     _DEFAULT_MODEL,
    "gold-final":                _DEFAULT_MODEL,
    "pipeline-generator":        _DEFAULT_MODEL,
    # Flash: classification, routing, validation
    "orchestrator":              _FLASH_MODEL,
    "requirement-understanding": _FLASH_MODEL,
    "use-case-classification":   _FLASH_MODEL,
    "challenger":                _FLASH_MODEL,
    "kpi-derivation":            _FLASH_MODEL,
    "test-agent":                _FLASH_MODEL,
    "publisher":                 _FLASH_MODEL,
}
if os.getenv("GEMINI_MODEL_TIER") == "flash":
    _SKILL_MODEL = {k: _FLASH_MODEL for k in _SKILL_MODEL}

# ── Thinking budget ───────────────────────────────────────────────────────────
# The flash model has "thinking" ON by default, and thinking tokens are drawn
# from the SAME max_output_tokens budget as the answer. For agents that must emit
# a large structured JSON (Test Agent, Challenger, …), unbounded thinking can
# consume the budget and truncate the JSON mid-object → the response fails to
# parse → run_agent returns {"raw_output": ...}. A PASSING test then looks like a
# failure ("Pipeline build failed"), and the Challenger review PDF comes out
# empty. Capping the thinking budget guarantees room for the actual answer.
# Applied only to flash agents; pro models manage their own budget. Tunable via
# the FLASH_THINKING_BUDGET env var (set to 0 to disable thinking entirely).
_FLASH_THINKING_BUDGET = int(os.getenv("FLASH_THINKING_BUDGET", "2048"))


def _thinking_config_for(skill_name: str):
    """Return a ThinkingConfig for flash agents (bounded budget), else None."""
    if ThinkingConfig is None:
        return None
    if _SKILL_MODEL.get(skill_name) != _FLASH_MODEL:
        return None
    try:
        return ThinkingConfig(thinking_budget=_FLASH_THINKING_BUDGET)
    except Exception:  # pragma: no cover - defensive against SDK field changes
        return None

# ── google.genai client (lazy, one per process) ───────────────────────────────

_genai_client: genai.Client | None = None


def _client() -> genai.Client:
    global _genai_client
    if _genai_client is None:
        _genai_client = genai.Client(
            vertexai=True,
            project=_GCP_PROJECT,
            location=_GENAI_LOCATION,
        )
    return _genai_client


# ── Conversation history (in-process, per skill × session) ───────────────────
# {(skill_name, session_id): [Content(role="user"|"model", ...), ...]}
_histories: dict[tuple[str, str], list[Content]] = {}


# ── Skill loading ─────────────────────────────────────────────────────────────

def load_skill(name: str) -> str:
    if name not in SKILL_REGISTRY:
        raise ValueError(f"Unknown agent '{name}'. Registered: {list(SKILL_REGISTRY)}")
    with open(SKILL_REGISTRY[name], encoding="utf-8") as f:
        return f.read()


# ── Thread / session management ───────────────────────────────────────────────

def create_thread(skill_name: str, thread_id: str | None = None) -> str:
    """
    Return a session ID string for the given skill. History is created lazily
    on the first run_agent() call — no pre-creation needed.

    Returns:
        A plain string session_id. Pass this to run_agent() on every subsequent
        call within the same HTTP session to maintain conversation history.
    """
    return thread_id or str(uuid.uuid4())


# ── Output parsing helpers ────────────────────────────────────────────────────

def _escape_newlines_in_strings(text: str) -> str:
    result: list[str] = []
    in_string   = False
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
    text = (
        raw_text.strip()
        .removeprefix("```json")
        .removeprefix("```")
        .removesuffix("```")
        .strip()
    )

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    try:
        return json.loads(_escape_newlines_in_strings(text))
    except json.JSONDecodeError:
        pass

    brace_depth = 0
    json_start  = -1
    json_end    = -1
    in_string   = False
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
                result = json.loads(attempt)
                tail = text[json_end:].strip()
                if tail:
                    result["display_output"] = tail
                return result
            except json.JSONDecodeError:
                pass

    # Greedy recovery: some models (esp. gemini-flash) emit minor JSON defects —
    # trailing commas, a stray sentence after the closing brace, or a single
    # unbalanced brace. Try the first '{' .. last '}' slice with trailing commas
    # stripped. This rescues otherwise-valid verdicts (e.g. the Test Agent's
    # {"test_status":"passed",...}) that would otherwise be dropped to raw_output
    # and wrongly reported as a pipeline failure.
    import re as _re
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        greedy = text[first:last + 1]
        greedy = _re.sub(r",(\s*[}\]])", r"\1", greedy)  # remove trailing commas
        for attempt in (greedy, _escape_newlines_in_strings(greedy)):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                pass

    if '"generated_code"' in text:
        sql = _extract_generated_code(text)
        if sql:
            return {
                "generated_code": sql,
                "pipeline_type":  "sql",
                "target_tables":  [],
                "statement_count": sql.count(";"),
                "_truncated":     True,
            }

    return {"raw_output": text}


def _extract_generated_code(text: str) -> str:
    import re as _re
    m = _re.search(r'"generated_code"\s*:\s*"', text)
    if not m:
        return ""

    pos        = m.end()
    chars: list[str] = []
    escape_next = False
    found_end   = False

    while pos < len(text):
        ch = text[pos]
        if escape_next:
            if   ch == "n":  chars.append("\n")
            elif ch == "r":  chars.append("\r")
            elif ch == "t":  chars.append("\t")
            elif ch == '"':  chars.append('"')
            elif ch == "\\": chars.append("\\")
            else:
                chars.append("\\")
                chars.append(ch)
            escape_next = False
        elif ch == "\\":
            escape_next = True
        elif ch == '"':
            found_end = True
            break
        else:
            chars.append(ch)
        pos += 1

    sql = "".join(chars)

    if not found_end:
        last_semi = sql.rfind(";")
        if last_semi >= 0:
            sql = sql[: last_semi + 1]

    return sql.strip()


# ── Agent runner ──────────────────────────────────────────────────────────────

async def run_agent(
    skill_name: str,
    user_input: str,
    context: dict | None = None,
    session: str | None = None,
    _conversation_history: list[dict] | None = None,
) -> dict:
    """
    Invoke an agent via google.genai directly (Vertex AI).

    No ADK template processing — SKILL.md files are passed as system_instruction
    verbatim, so JSON examples with {field} notation are safe.

    Args:
        skill_name:  Key in SKILL_REGISTRY.
        user_input:  The prompt/instruction to send.
        context:     Optional dict injected as a <context> block in the user turn.
        session:     Session ID for this HTTP session × skill pair.
                     If None, a single-use session is created.
        _conversation_history: Ignored — kept for call-site backward compat.
    """
    if skill_name not in SKILL_REGISTRY:
        return {"error": f"Unknown agent '{skill_name}'. Registered: {list(SKILL_REGISTRY)}"}

    if session is None:
        session = str(uuid.uuid4())

    system_instruction = load_skill(skill_name)
    model    = _SKILL_MODEL.get(skill_name, _DEFAULT_MODEL)
    max_tok  = _SKILL_MAX_TOKENS.get(skill_name, _DEFAULT_MAX_TOKENS)

    if context:
        user_msg = f"<context>\n{json.dumps(context, indent=2)}\n</context>\n\n{user_input}"
    else:
        user_msg = user_input

    history_key = (skill_name, session)
    history = _histories.get(history_key, [])
    new_user_content = Content(role="user", parts=[Part(text=user_msg)])
    contents = history + [new_user_content]

    with tracer.start_as_current_span(f"agent.{skill_name}") as span:
        span.set_attribute("agent.skill",     skill_name)
        span.set_attribute("agent.thread_id", session)

        try:
            _cfg_kwargs = {
                "system_instruction": system_instruction,
                "max_output_tokens": max_tok,
            }
            _tcfg = _thinking_config_for(skill_name)
            if _tcfg is not None:
                _cfg_kwargs["thinking_config"] = _tcfg
            response = await _client().aio.models.generate_content(
                model=model,
                contents=contents,
                config=GenerateContentConfig(**_cfg_kwargs),
            )
            result_text = response.text

            # Persist the turn so subsequent calls have conversation history.
            model_content = Content(role="model", parts=[Part(text=result_text)])
            _histories[history_key] = history + [new_user_content, model_content]

        except Exception as exc:
            logging.getLogger(__name__).error(
                "[AGENT ERROR] %s: %s: %s", skill_name, type(exc).__name__, exc,
                exc_info=True,
            )
            span.set_attribute("agent.error", str(exc))
            return {"error": f"{type(exc).__name__}: {exc}"}

    return parse_agent_output(result_text)
