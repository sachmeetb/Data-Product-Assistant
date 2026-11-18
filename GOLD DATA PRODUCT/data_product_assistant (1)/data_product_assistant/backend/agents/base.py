"""
base.py — shared agent runner using Azure Databricks Model Serving (or Azure OpenAI)
via the OpenAI-compatible AsyncOpenAI client.
"""

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

from opentelemetry import trace

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
    "silver-sttm":               20000,   # high headroom to prevent truncated mappings
    "silver-transformation":     16000,
    "gold-final":                20000,
}
_DEFAULT_MAX_TOKENS = 4096

_DEFAULT_MODEL = (
    os.getenv("DATABRICKS_LLM_MODEL")
    or os.getenv("AZURE_OPENAI_DEPLOYMENT")
    or os.getenv("LLM_MODEL")
    or "databricks-meta-llama-3-3-70b-instruct"
)
_FLASH_MODEL = (
    os.getenv("DATABRICKS_FAST_LLM_MODEL")
    or _DEFAULT_MODEL
)

_SKILL_MODEL: dict[str, str] = {
    # Heavy structured JSON output
    "gold-er":                   _DEFAULT_MODEL,
    "silver-sttm":               _DEFAULT_MODEL,
    "silver-transformation":     _DEFAULT_MODEL,
    "gold-final":                _DEFAULT_MODEL,
    "pipeline-generator":        _DEFAULT_MODEL,
    # Fast classification, routing, validation
    "orchestrator":              _FLASH_MODEL,
    "requirement-understanding": _FLASH_MODEL,
    "use-case-classification":   _FLASH_MODEL,
    "challenger":                _FLASH_MODEL,
    "kpi-derivation":            _FLASH_MODEL,
    "test-agent":                _FLASH_MODEL,
    "publisher":                 _FLASH_MODEL,
}

# ── OpenAI-compatible LLM client (lazy, one per process) ──────────────────────

_llm_client = None


def _client():
    global _llm_client
    if _llm_client is None:
        from tools.databricks_auth import get_async_llm_client
        _llm_client = get_async_llm_client()
    return _llm_client


# ── Conversation history (in-process, per skill × session) ───────────────────
# {(skill_name, session_id): [{"role": "user"|"assistant", "content": "..."}, ...]}
_histories: dict[tuple[str, str], list[dict[str, str]]] = {}


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

    # Attempt truncated JSON balancing to recover objects ending mid-stream
    repaired_obj = _repair_truncated_json(text)
    if repaired_obj is not None:
        return repaired_obj

    return {"raw_output": text}


def _repair_truncated_json(text: str) -> dict | None:
    """Attempt to balance unclosed quotes, brackets, and braces in a truncated JSON string."""
    first = text.find("{")
    if first < 0:
        return None
    s = text[first:].strip()

    in_string = False
    escape_next = False
    stack: list[str] = []
    clean_chars: list[str] = []

    for ch in s:
        if escape_next:
            escape_next = False
            clean_chars.append(ch)
            continue
        if ch == "\\":
            escape_next = True
            clean_chars.append(ch)
            continue
        if ch == '"':
            in_string = not in_string
            clean_chars.append(ch)
            continue
        if in_string:
            clean_chars.append(ch)
            continue
        if ch in ("{", "["):
            stack.append(ch)
            clean_chars.append(ch)
        elif ch in ("}", "]"):
            if stack:
                top = stack[-1]
                if (ch == "}" and top == "{") or (ch == "]" and top == "["):
                    stack.pop()
                    clean_chars.append(ch)
            continue
        else:
            clean_chars.append(ch)

    repaired = "".join(clean_chars)
    if in_string:
        repaired += '"'
    import re as _re
    repaired = _re.sub(r'[,:\s]+$', '', repaired)
    for open_ch in reversed(stack):
        if open_ch == "{":
            repaired += "}"
        elif open_ch == "[":
            repaired += "]"

    for attempt in (repaired, _escape_newlines_in_strings(repaired)):
        try:
            res = json.loads(attempt)
            if isinstance(res, dict) and len(res) > 0:
                res["_truncated"] = True
                return res
        except Exception:
            pass
    return None


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
    new_user_msg = {"role": "user", "content": user_msg}
    messages = [{"role": "system", "content": system_instruction}] + history + [new_user_msg]

    with tracer.start_as_current_span(f"agent.{skill_name}") as span:
        span.set_attribute("agent.skill",     skill_name)
        span.set_attribute("agent.thread_id", session)

        try:
            response = await _client().chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tok,
                temperature=0.2,
            )
            result_text = (response.choices[0].message.content or "") if response.choices else ""

            # Persist the turn so subsequent calls have conversation history.
            _histories[history_key] = history + [new_user_msg, {"role": "assistant", "content": result_text}]

        except Exception as exc:
            logging.getLogger(__name__).error(
                "[AGENT ERROR] %s: %s: %s", skill_name, type(exc).__name__, exc,
                exc_info=True,
            )
            span.set_attribute("agent.error", str(exc))
            return {"error": f"{type(exc).__name__}: {exc}"}

    return parse_agent_output(result_text)
