"""Interpret an engineer's plain-language column derivation into a structured transform.

The `data_mapping` review surface lets an engineer fix an AI-suggested mapping, but
today the only AI help ("Guide me") answers in prose the engineer must hand-transcribe
back into the transform form. This endpoint is the column-level sibling of
`filter_intent.py`: it turns a plain-language description of a column's derivation
("combine first and last name with a space", "mask all but the last 4 of ssn",
"age in years from date_of_birth") into a **structured transform DSL payload** the UI
can apply in one click — the same `{transform_kind, transform_inputs, transform_params,
transform_decorators, transform_expression}` shape the mapping stage already writes.

It authors ONLY the existing DSL — no new kinds, no new param shapes. Source columns are
named in the response and resolved to URIs here; anything the model can't express falls
back to the raw `expression` escape hatch, and a raw expression is portability-checked via
`dialect_sql.compile_expression` before it's returned, so the AI path can never one-click
a transform the compiler can't emit (the deploy-time enforcement gate stays authoritative).

The heavy lifting is the `transform-intent-interpreter` skill invoked via the Claude Code
SDK, with a deterministic heuristic fallback so the system stays usable when the skill
isn't installed.
"""

from __future__ import annotations

import json as _json
from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session

from ..config import BASE_DIR, PIPELINE_PLUGINS
from ..database import get_session
from ..models import Project

# Reuse filter_intent's proven helpers verbatim (same SDK-call + parse + grounding shapes).
from .filter_intent import (
    _parse_skill_json,
    _enrich_source_columns,
    _norm_tokens,
)


router = APIRouter(prefix="/api/transform-intent", tags=["transform-intent"])


INTERPRETER_SKILL = "transform-intent-interpreter"

# The canonical column-transform kinds (mirrors the skill's VALID_TRANSFORM_KINDS and
# the frontend TransformKind union). Kept as a local constant — the backend never imports
# the skill. Anything outside this set coerces to `expression` (the raw escape hatch).
VALID_TRANSFORM_KINDS = {
    "direct", "cast", "format", "concat", "split", "substring",
    "case", "arithmetic", "lookup", "literal", "expression",
    "bucket", "mask", "hash", "window", "date_difference",
}


# ── request / response models ───────────────────────────────────────────────


class TransformColSpec(BaseModel):
    name: str
    type: str = ""
    top_values: list[str] = []
    uri: str = ""  # the source column's graph URI, so inputs resolve to URIs


class TransformInterpretRequest(BaseModel):
    intent: str
    target_column: str = ""                       # the product column being derived
    source_columns: list[TransformColSpec] = []   # bound source columns (name + uri)
    lookup_tables: list[str] = []                 # discovered reference tables (schema.table)
    dialect: str = "postgres"
    project_id: Optional[int] = None
    output_dataset_uri: Optional[str] = None      # grounds source cols to THIS dataset
    mapping_uri: Optional[str] = None             # the mapping being edited, if any
    notes: Optional[str] = None


class TransformInterpretResponse(BaseModel):
    readback: str
    transform_kind: str = ""
    transform_inputs: list[str] = []      # resolved source-column URIs (ordered)
    transform_params: dict = {}
    transform_decorators: dict = {}
    transform_expression: str = ""
    confidence: int = 0
    warnings: list[str] = []
    grounded_columns: list[str] = []      # source column names referenced
    _fallback: bool = False


# ── skill invocation ─────────────────────────────────────────────────────────


async def _run_interpreter_skill(inputs: dict[str, Any]) -> tuple[dict[str, Any], Optional[str]]:
    """Invoke the `transform-intent-interpreter` skill. Returns (payload, error).

    Mirrors `filter_intent._run_interpreter_skill` exactly. On any SDK/import/parse
    failure, `error` is a short string and `payload` is {} so the caller falls back to
    the heuristic.
    """
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
    except ImportError:
        return {}, "claude-agent-sdk is not installed"

    system_prompt = (
        f"FIRST: Load the `{INTERPRETER_SKILL}` skill via the Skill tool, then follow its "
        "instructions to interpret the plain-language column derivation into a structured "
        "transform. Emit exactly one fenced JSON code block as the skill instructs. Do not "
        "write files. Do not run shell commands. Do not answer in prose outside the JSON block."
    )
    user_prompt = (
        f"FIRST: Load the {INTERPRETER_SKILL} skill using the Skill tool.\n\n"
        f"INPUTS (JSON):\n{_json.dumps(inputs, ensure_ascii=False)}\n\n"
        "Output the single fenced JSON block as instructed by the skill."
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

    transcript_parts: list[str] = []
    try:
        async for message in query(prompt=user_prompt, options=options):
            if message is None:
                continue
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        transcript_parts.append(block.text)
            elif isinstance(message, ResultMessage):
                from ..llm_usage import extract_usage, record_usage
                record_usage(source="transform_intent", usage=extract_usage(message))
                if message.is_error:
                    return {}, "Interpreter returned an error"
    except Exception as e:
        return {}, f"Interpreter failed: {e}"

    payload = _parse_skill_json("\n".join(transcript_parts))
    if not isinstance(payload, dict) or "transform_kind" not in payload:
        return {}, "Interpreter output not in the expected shape"
    return payload, None


# ── deterministic heuristic fallback ────────────────────────────────────────


def _cname(c: Any) -> str:
    return (c.get("name") if isinstance(c, dict) else getattr(c, "name", "")) or ""


def _best_matches(intent: str, target: str, cols: list[Any], n: int = 2) -> list[str]:
    """Return up to `n` source column NAMES whose tokens best overlap the intent/target."""
    prose = set(_norm_tokens(intent)) | set(_norm_tokens(target))
    scored: list[tuple[int, str]] = []
    for c in cols:
        nm = _cname(c)
        if not nm:
            continue
        score = len(set(_norm_tokens(nm)) & prose)
        if score > 0:
            scored.append((score, nm))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [nm for _, nm in scored[:n]]


def _heuristic_interpret(inputs: dict[str, Any]) -> dict[str, Any]:
    """Cheap deterministic fallback. NEVER emits raw prose as an expression.

    Maps common phrasings to a structured kind + obvious params, picking inputs by
    name-token overlap. When it can't confidently map, it returns an empty kind + low
    confidence so the engineer finalises it in the editor — the same discipline as the
    filter heuristic.
    """
    intent = (inputs.get("intent") or "").strip()
    target = inputs.get("target_column") or ""
    cols = list(inputs.get("source_columns") or [])
    lc = intent.lower()
    warn = ["Heuristic interpretation — verify the transform in the editor."]

    if not intent:
        return {
            "readback": "No derivation described.",
            "transform_kind": "", "transform_inputs": [], "transform_params": {},
            "transform_decorators": {}, "transform_expression": "",
            "confidence": 0, "warnings": [], "grounded_columns": [], "_fallback": True,
        }

    def _out(kind, ins, params=None, deco=None, expr="", read="", conf=55):
        return {
            "readback": read or f"a {kind} transform",
            "transform_kind": kind, "transform_inputs": ins,
            "transform_params": params or {}, "transform_decorators": deco or {},
            "transform_expression": expr, "confidence": conf, "warnings": warn,
            "grounded_columns": ins, "_fallback": True,
        }

    # concat — combine/join/glue multiple columns
    if any(w in lc for w in ("combine", "concat", "glue", "join together", "full name", "together")):
        ins = _best_matches(intent, target, cols, n=3)
        sep = " " if ("space" in lc or "full name" in lc or " with a " in lc) else ""
        return _out("concat", ins, {"separator": sep},
                    read=f"join {', '.join(ins) or 'columns'} with a separator", conf=60 if ins else 30)

    # mask — redact keeping a tail
    if any(w in lc for w in ("mask", "redact", "obfuscate", "hide all but", "hide the")):
        ins = _best_matches(intent, target, cols, n=1)
        keep_n = 4
        for tok in _norm_tokens(intent):
            if tok.isdigit():
                keep_n = int(tok)
                break
        return _out("mask", ins, {"algorithm": "keep_last", "keep_n": keep_n, "mask_char": "*"},
                    read=f"mask {ins[0] if ins else 'the value'} keeping the last {keep_n}", conf=60 if ins else 30)

    # hash — irreversible digest
    if any(w in lc for w in ("hash", "anonymi", "pseudonym", "digest")):
        ins = _best_matches(intent, target, cols, n=1)
        algo = "sha256"
        for a in ("md5", "sha1", "sha256"):
            if a in lc:
                algo = a
        return _out("hash", ins, {"algorithm": algo},
                    read=f"hash {ins[0] if ins else 'the value'} ({algo})", conf=60 if ins else 30)

    # format — case / trim
    if any(w in lc for w in ("uppercase", "upper case", "lowercase", "lower case", "trim", "strip")):
        ins = _best_matches(intent, target, cols, n=1)
        case = "upper" if "upper" in lc else ("lower" if "lower" in lc else "trim")
        return _out("format", ins, {"case": case},
                    read=f"{case} {ins[0] if ins else 'the value'}", conf=60 if ins else 30)

    # date_difference — age / days-between
    if any(w in lc for w in ("age", "years between", "days between", "months between", "difference between")):
        ins = _best_matches(intent, target, cols, n=2)
        unit = "year" if ("age" in lc or "year" in lc) else ("month" if "month" in lc else "day")
        return _out("date_difference", ins, {"unit": unit, "semantics": "completed_units"},
                    read=f"{unit}s between the dates", conf=55 if ins else 30)

    # cast — convert type
    if any(w in lc for w in ("cast", "convert to", "as a ", "as an ", "to a number", "to text", "to date")):
        ins = _best_matches(intent, target, cols, n=1)
        tt = "TEXT"
        for word, sqltype in (("date", "DATE"), ("timestamp", "TIMESTAMP"), ("integer", "INTEGER"),
                              ("number", "NUMERIC"), ("numeric", "NUMERIC"), ("text", "TEXT"),
                              ("string", "VARCHAR(255)"), ("boolean", "BOOLEAN")):
            if word in lc:
                tt = sqltype
                break
        return _out("cast", ins, {"target_type": tt},
                    read=f"cast {ins[0] if ins else 'the value'} to {tt}", conf=55 if ins else 30)

    # direct — pass through / same as
    if any(w in lc for w in ("same as", "copy of", "directly", "pass through", "unchanged", "as-is")):
        ins = _best_matches(intent, target, cols, n=1)
        return _out("direct", ins, read=f"pass {ins[0] if ins else 'the column'} through unchanged",
                    conf=60 if ins else 25)

    # couldn't map
    return {
        "readback": "Couldn't confidently interpret this derivation — pick a kind in the editor.",
        "transform_kind": "", "transform_inputs": [], "transform_params": {},
        "transform_decorators": {}, "transform_expression": "",
        "confidence": 15,
        "warnings": warn + ["No transform kind matched the description."],
        "grounded_columns": [], "_fallback": True,
    }


# ── portability check (fail-open) ────────────────────────────────────────────


def _portability_check(kind: str, expression: str, dialect: str) -> list[str]:
    """Validate a raw `expression`-kind transform against the target dialect.

    Structured kinds are compiled by the dialect emitter and are portable by
    construction; only a raw expression can be non-portable. Fail-OPEN — the deploy-time
    enforcement gate stays authoritative, so a check error never blocks interpretation.
    """
    expr = (expression or "").strip()
    if not expr:
        return []
    try:
        from ..dialect_sql import compile_expression
        result = compile_expression(expr, dialect, read=dialect)
        errs = getattr(result, "errors", None) or []
        out = []
        for e in errs:
            msg = getattr(e, "message", None) or str(e)
            out.append(f"Expression may not compile for {dialect}: {msg}")
        return out
    except Exception:
        return []


def _normalize_kind(raw: Any) -> str:
    k = (str(raw or "")).strip().lower()
    if not k:
        return ""
    if k not in VALID_TRANSFORM_KINDS:
        return "expression"
    return k


# ── core + endpoint ──────────────────────────────────────────────────────────


async def interpret_transform_intent(
    *,
    intent: str,
    target_column: str = "",
    source_columns: list[dict] | None = None,
    lookup_tables: list[str] | None = None,
    dialect: str = "postgres",
    project: Project | None = None,
    output_dataset_uri: str | None = None,
    mapping_uri: str | None = None,
    notes: str = "",
) -> TransformInterpretResponse:
    """Core interpret logic, callable internally (no FastAPI request needed).

    Uses the inline `source_columns` (each carrying a `uri`) for grounding + URI
    resolution; when `project` AND `output_dataset_uri` are given but no inline sources
    were passed, enriches from the graph (the headless/MCP path). Runs the skill, falling
    back to the deterministic heuristic. Resolves the returned input NAMES to source-column
    URIs, and portability-checks any raw expression.
    """
    warnings: list[str] = []
    src_cols: list[dict] = [dict(c) for c in (source_columns or [])]

    # Headless path: no inline sources but we can reach the graph.
    if not src_cols and project is not None and output_dataset_uri:
        enriched, _ = _enrich_source_columns(project, output_dataset_uri)
        # _enrich_source_columns returns name/type/top_values/source_column_uri
        for c in enriched:
            src_cols.append({
                "name": c.get("name") or "",
                "type": c.get("type") or "",
                "top_values": c.get("top_values") or [],
                "uri": c.get("source_column_uri") or "",
            })

    # name(lower) -> uri, for resolving the model's input NAMES to URIs.
    name_to_uri: dict[str, str] = {}
    for c in src_cols:
        nm = (c.get("name") or "").strip().lower()
        uri = (c.get("uri") or c.get("source_column_uri") or "").strip()
        if nm and uri:
            name_to_uri.setdefault(nm, uri)

    inputs = {
        "intent": intent,
        "target_column": target_column,
        "source_columns": [
            {"name": c.get("name") or "", "type": c.get("type") or "",
             "top_values": c.get("top_values") or []}
            for c in src_cols
        ],
        "lookup_tables": list(lookup_tables or []),
        "dialect": dialect,
        "notes": notes or "",
    }

    payload, err = await _run_interpreter_skill(inputs)
    if err or not payload:
        payload = _heuristic_interpret(inputs)

    kind = _normalize_kind(payload.get("transform_kind"))

    # Resolve input NAMES → URIs (literal takes no inputs).
    raw_inputs = payload.get("transform_inputs") or []
    resolved: list[str] = []
    unresolved: list[str] = []
    grounded_names: list[str] = []
    for nm in raw_inputs:
        nm_s = str(nm).strip()
        if not nm_s:
            continue
        grounded_names.append(nm_s)
        uri = name_to_uri.get(nm_s.lower())
        if uri:
            resolved.append(uri)
        else:
            unresolved.append(nm_s)
    if unresolved and kind != "literal":
        warnings.append(
            f"Couldn't resolve source column(s) {unresolved} to the bound sources — "
            "pick them in the editor."
        )

    params = payload.get("transform_params") or {}
    decorators = payload.get("transform_decorators") or {}
    expression = str(payload.get("transform_expression") or "").strip()

    # Portability: a raw expression must compile for the target dialect (fail-open).
    port_warnings = _portability_check(kind, expression, dialect)
    warnings.extend(port_warnings)

    confidence = int(payload.get("confidence") or 0)
    if unresolved or port_warnings:
        confidence = min(confidence, 60)

    return TransformInterpretResponse(
        readback=str(payload.get("readback") or "").strip()
        or "Couldn't interpret this derivation; finalise it in the editor.",
        transform_kind=kind,
        transform_inputs=resolved,
        transform_params=params if isinstance(params, dict) else {},
        transform_decorators=decorators if isinstance(decorators, dict) else {},
        transform_expression=expression,
        confidence=confidence,
        warnings=list(payload.get("warnings") or []) + warnings,
        grounded_columns=list(payload.get("grounded_columns") or []) or grounded_names,
        _fallback=bool(payload.get("_fallback", False)),
    )


@router.post("/interpret", response_model=TransformInterpretResponse)
async def interpret_transform(
    body: TransformInterpretRequest,
    session: Session = Depends(get_session),
):
    """Interpret a plain-language column derivation into a structured transform payload.

    Returns the DSL fields (`transform_kind` / `transform_inputs` (resolved to source
    URIs) / `transform_params` / `transform_decorators` / `transform_expression`), a
    plain-language `readback`, a confidence, and warnings — ready for one-click apply into
    the transform editor. Never invents a kind outside the 16-kind DSL; a raw expression
    is portability-checked before return.
    """
    project = session.get(Project, body.project_id) if body.project_id is not None else None
    return await interpret_transform_intent(
        intent=body.intent,
        target_column=body.target_column,
        source_columns=[c.model_dump() for c in body.source_columns],
        lookup_tables=list(body.lookup_tables or []),
        dialect=body.dialect,
        project=project,
        output_dataset_uri=body.output_dataset_uri,
        mapping_uri=body.mapping_uri,
        notes=body.notes or "",
    )
