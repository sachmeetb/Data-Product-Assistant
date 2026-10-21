"""Interpret a Product Owner's plain-language dataset filter into grounded SQL.

The Shape step of the consumer-aligned wizard lets the PO say which rows the
product should include in plain words ("active employees only"). Trusting that
text verbatim produced invalid SQL like `WHERE employee status is active` that
only failed at deploy time. This endpoint interprets the prose into a real,
grounded SQL predicate (against the product's own columns + the CONSUMES'd
source columns' profiled values) and hands back a plain-language READBACK the
PO can confirm — the PO never sees the SQL; the engineer finalizes it later.

Project-less by design (mirrors `domain_catalogs._run_schema_advisor`): the
wizard may not have created a project yet, so the caller passes inline context.
When `project_id` IS supplied we additionally enrich the grounding context with
the source columns + their TopValues from the graph.

The heavy lifting is an out-of-repo skill (`filter-intent-interpreter`) invoked
via the Claude Code SDK, with a deterministic heuristic fallback so the system
stays usable when the skill isn't installed.
"""

from __future__ import annotations

import json as _json
import re
from typing import Any, Optional

try:  # sqlglot is a pinned dep; guard so a missing lib degrades to a no-op
    import sqlglot as _sqlglot
    from sqlglot import expressions as _exp
    _HAS_SQLGLOT = True
except Exception:  # pragma: no cover
    _HAS_SQLGLOT = False

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session

from ..config import BASE_DIR, PIPELINE_PLUGINS
from ..database import get_session
from ..models import Project
from ..neo4j_client import neo4j_session


router = APIRouter(prefix="/api/filter-intent", tags=["filter-intent"])


INTERPRETER_SKILL = "filter-intent-interpreter"

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


# ── request / response models ───────────────────────────────────────────────


class ColSpec(BaseModel):
    name: str
    type: str = ""
    top_values: list[str] = []


class InterpretRequest(BaseModel):
    intent: str
    columns: list[ColSpec] = []          # the product's own columns
    source_columns: list[ColSpec] = []   # CONSUMES'd source columns (inline; enriched if project_id)
    dialect: str = "postgres"
    project_id: Optional[int] = None
    output_dataset_uri: Optional[str] = None  # scopes grounding to THIS dataset's mappings
    notes: Optional[str] = None


class InterpretResponse(BaseModel):
    readback: str
    predicate: str
    confidence: int
    warnings: list[str] = []
    grounded_columns: list[str] = []
    _fallback: bool = False


# ── skill JSON parse (shared shape with ingest_products) ─────────────────────


def _parse_skill_json(text: str) -> dict[str, Any]:
    matches = _JSON_BLOCK_RE.findall(text or "")
    payload: dict[str, Any] = {}
    if matches:
        try:
            parsed = _json.loads(matches[-1])
            if isinstance(parsed, dict):
                payload = parsed
        except _json.JSONDecodeError:
            pass
    if not payload:
        stripped = (text or "").strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = _json.loads(stripped)
                if isinstance(parsed, dict):
                    payload = parsed
            except _json.JSONDecodeError:
                pass
    return payload


async def _run_interpreter_skill(inputs: dict[str, Any]) -> tuple[dict[str, Any], Optional[str]]:
    """Invoke the `filter-intent-interpreter` skill. Returns (payload, error).

    Mirrors `ingest_products._run_classifier_skill` exactly in structure. On any
    SDK/import/parse failure, `error` is a short string and `payload` is {} so
    the caller falls back to the heuristic.
    """
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
    except ImportError:
        return {}, "claude-agent-sdk is not installed"

    system_prompt = (
        f"FIRST: Load the `{INTERPRETER_SKILL}` skill via the Skill tool, then follow its "
        "instructions to interpret the plain-language filter into a grounded SQL predicate. "
        "Emit exactly one fenced JSON code block as the skill instructs. Do not write files. "
        "Do not run shell commands. Do not answer in prose outside the JSON block."
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
                record_usage(source="filter_intent", usage=extract_usage(message))
                if message.is_error:
                    return {}, "Interpreter returned an error"
    except Exception as e:
        return {}, f"Interpreter failed: {e}"

    payload = _parse_skill_json("\n".join(transcript_parts))
    if not isinstance(payload, dict) or "predicate" not in payload:
        return {}, "Interpreter output not in the expected shape"
    return payload, None


# ── deterministic heuristic fallback ────────────────────────────────────────

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _norm_tokens(s: str) -> list[str]:
    return _TOKEN_RE.findall((s or "").lower())


def _heuristic_interpret(inputs: dict[str, Any]) -> dict[str, Any]:
    """Cheap deterministic fallback. NEVER emits the raw prose as the predicate.

    Strategy: tokenize the prose; find a column whose name tokens overlap the
    prose; if that column carries top_values, pick the value whose lowercased
    form appears in the prose and emit `<col> = '<value>'`. Recognise a couple
    of structural patterns ("not deleted" → `<col> IS NULL`). Otherwise return
    an empty predicate with low confidence so the engineer finalizes it.
    """
    intent = (inputs.get("intent") or "").strip()
    prose_tokens = set(_norm_tokens(intent))
    prose_lc = intent.lower()
    all_cols = list(inputs.get("columns") or []) + list(inputs.get("source_columns") or [])

    warnings: list[str] = ["Heuristic interpretation — an engineer should verify the grounded SQL."]

    if not intent:
        return {
            "readback": "No filter — every row is included.",
            "predicate": "", "confidence": 100, "warnings": [],
            "grounded_columns": [], "_fallback": True,
        }

    def _cname(c):
        return (c.get("name") if isinstance(c, dict) else getattr(c, "name", "")) or ""

    def _ctvs(c):
        return (c.get("top_values") if isinstance(c, dict) else getattr(c, "top_values", [])) or []

    # "not deleted" / "exclude deleted" → a *_deleted_at / deleted column IS NULL
    if any(w in prose_lc for w in ("not deleted", "exclude deleted", "non-deleted", "without deleted")):
        for c in all_cols:
            if "delete" in _cname(c).lower():
                cname = _cname(c)
                return {
                    "readback": f"only rows where {cname} is empty (not deleted)",
                    "predicate": f"{cname} IS NULL", "confidence": 55, "warnings": warnings,
                    "grounded_columns": [cname], "_fallback": True,
                }

    # Strongest signal: a column's profiled top-value appears in the prose
    # (e.g. "active" → employment_status='active'). This catches cases the
    # column NAME doesn't token-match ("employees" vs "employment_status").
    for c in all_cols:
        cname = _cname(c)
        for tv in _ctvs(c):
            tv_toks = set(_norm_tokens(str(tv)))
            if tv_toks and tv_toks.issubset(prose_tokens):
                val = str(tv).replace("'", "''")
                return {
                    "readback": f"only rows where {cname} is {tv}",
                    "predicate": f"{cname} = '{val}'", "confidence": 60,
                    "warnings": warnings, "grounded_columns": [cname], "_fallback": True,
                }

    # Next: column-name token overlap with the prose.
    best = None
    best_score = 0
    for c in all_cols:
        score = len(set(_norm_tokens(_cname(c))) & prose_tokens)
        if score > best_score:
            best, best_score = c, score

    if best and best_score > 0:
        cname = _cname(best)
        for tv in _ctvs(best):
            if str(tv).lower() in prose_lc:
                val = str(tv).replace("'", "''")
                return {
                    "readback": f"only rows where {cname} is {tv}",
                    "predicate": f"{cname} = '{val}'", "confidence": 60,
                    "warnings": warnings, "grounded_columns": [cname], "_fallback": True,
                }
        # Matched a column but no value to ground against.
        warnings.append(
            f"Matched the column “{cname}” but couldn't ground a value — the engineer must pick one."
        )
        return {
            "readback": f"a condition on {cname} (value needs confirmation)",
            "predicate": "", "confidence": 30, "warnings": warnings,
            "grounded_columns": [cname], "_fallback": True,
        }

    return {
        "readback": "Could not confidently interpret this filter; an engineer will finalize it.",
        "predicate": "", "confidence": 15,
        "warnings": warnings + ["No matching column found for the described filter."],
        "grounded_columns": [], "_fallback": True,
    }


# ── grounding (when project_id present) ──────────────────────────────────────

# Grounding is scoped to the SPECIFIC output dataset being filtered: we walk
# only THAT dataset's current column mappings back to their source columns,
# then reach the profiled :TopValue nodes. This is load-bearing on two counts:
#   1. :TopValue only ever hangs off the catalog :Column (profiling never writes
#      it onto :DProdColumn), so a consumed source-product column reaches its
#      observed values via ITS OWN current mapping back to the catalog :Column
#      (one lineage hop). A catalog source column carries :TopValue directly.
#   2. Scoping by output_dataset_uri stops same-named columns from OTHER datasets
#      of the same product cross-contaminating this dataset's filter grounding.
# `top_values` are OBSERVED top-N profile values (not an authoritative allow-list).
_GROUNDING_QUERY = """\
MATCH (ods:DProdOutputDataset {uri: $output_dataset_uri})-[:HAS_PRODUCT_COLUMN]->(:DProdColumn)
      <-[:MAPS_TO_PRODUCT_COLUMN]-(:ColumnMapping {isCurrent: true})
      -[:MAPS_SOURCE_COLUMN]->(src)
WHERE src:Column OR src:DProdColumn
OPTIONAL MATCH (src)-[:HAS_TOP_VALUE]->(tvd:TopValue)
OPTIONAL MATCH (src)<-[:MAPS_TO_PRODUCT_COLUMN]-(:ColumnMapping {isCurrent: true})
      -[:MAPS_SOURCE_COLUMN]->(:Column)-[:HAS_TOP_VALUE]->(tvl:TopValue)
WITH src,
     coalesce(src.name, '') AS name,
     coalesce(src.logicalType, src.physicalType, '') AS type,
     collect(DISTINCT tvd.value) + collect(DISTINCT tvl.value) AS raw_vals
RETURN name, type,
       [v IN raw_vals WHERE v IS NOT NULL][..20] AS top_values,
       src.uri AS source_column_uri
"""


def _enrich_source_columns(
    project: Project, output_dataset_uri: Optional[str]
) -> tuple[list[dict], bool]:
    """Pull the source columns feeding a specific output dataset + their observed
    profile values, for filter grounding.

    Scoped by `output_dataset_uri` (walks that dataset's current mappings only),
    so unrelated datasets' same-named columns can't leak in. Returns
    (columns, any_top_values); `any_top_values` is False when no profiled values
    are reachable so the caller can warn that value-level grounding is
    unavailable. When `output_dataset_uri` is falsy (e.g. wizard authoring before
    the product graph exists) returns ([], False) — grounding then relies on any
    inline source_columns the caller passed.
    """
    if not output_dataset_uri:
        return [], False
    cols: list[dict] = []
    any_tv = False
    try:
        with neo4j_session(
            project.neo4j_host, project.neo4j_port,
            project.neo4j_user, project.neo4j_password, project.neo4j_database,
        ) as sess:
            for row in sess.run(_GROUNDING_QUERY, output_dataset_uri=output_dataset_uri):
                tvs = [str(v) for v in (row["top_values"] or [])]
                if tvs:
                    any_tv = True
                cols.append({
                    "name": row["name"],
                    "type": row["type"] or "",
                    "top_values": tvs,
                    "source_column_uri": row.get("source_column_uri") or "",
                })
    except Exception:
        return [], False
    return cols, any_tv


# ── value grounding / case-robust rendering (type-safe, AST-based) ───────────

# Type tokens that mark a column as text — the only kind we dare wrap in LOWER().
_TEXT_TYPE_TOKENS = ("char", "text", "string", "clob", "enum", "uuid", "citext")


def _is_text_type(type_str: str) -> bool:
    t = (type_str or "").lower()
    return any(tok in t for tok in _TEXT_TYPE_TOKENS)


def _sg_dialect(dialect: str) -> Optional[str]:
    """Map the interpreter's dialect name to a sqlglot dialect, or None for the
    dialect-neutral grammar (covers 'ansi' and anything sqlglot doesn't model)."""
    try:
        from ..dialect_sql import sqlglot_dialect_for
        return sqlglot_dialect_for(dialect)
    except Exception:
        return None


def _build_col_evidence(columns: list[dict], source_columns: list[dict]) -> dict[str, dict]:
    """Merge product + source columns into {lower(name): {values, type}}.

    `values` are OBSERVED profile values; `type` is the first non-empty declared
    type. Same-named columns are merged (rare within one dataset after B1's
    output-scoping); values union, first type wins.
    """
    evidence: dict[str, dict] = {}
    for c in list(columns or []) + list(source_columns or []):
        name = (c.get("name") or "").strip().lower()
        if not name:
            continue
        ent = evidence.setdefault(name, {"values": [], "type": ""})
        ent["values"].extend(str(v) for v in (c.get("top_values") or []))
        typ = (c.get("type") or "").strip()
        if typ and not ent["type"]:
            ent["type"] = typ
    return evidence


def _exact_observed(values: list[str], literal: str) -> Optional[str]:
    """Return the observed value that case-insensitively equals `literal` (its
    EXACT stored casing), or None."""
    low = literal.lower()
    for v in values:
        if v.lower() == low:
            return v
    return None


def _ground_predicate_literals(
    predicate: str, col_evidence: dict[str, dict], dialect: str = "postgres"
) -> tuple[str, list[str]]:
    """Reconcile string-equality literals in a SQL predicate against observed
    profile values — type-safely, via a real SQL parse (never regex).

    For each ``col = 'lit'`` / ``col <> 'lit'`` / ``col IN ('a', 'b')`` whose
    literal is a STRING:
      * literal case-folds to an observed value → rewrite to that value's EXACT
        stored casing, keep plain equality (index-friendly). Fixes 'Active'→'active'.
      * else, only for a known TEXT column → wrap both sides in ``LOWER()`` so a
        casing mismatch can't silently return zero rows.
      * non-text / unknown-type columns are left untouched (``LOWER(date_col)``
        would be invalid SQL) with a warning.
    Comparisons already wrapped in a function (e.g. ``LOWER(col) = …``) are left
    alone. On any parse/generate failure the predicate is returned unchanged with
    a warning — we never emit SQL we couldn't round-trip.
    """
    warnings: list[str] = []
    predicate = (predicate or "").strip()
    if not predicate or not _HAS_SQLGLOT:
        return predicate, warnings
    sg = _sg_dialect(dialect)
    try:
        tree = _sqlglot.parse_one(predicate, read=sg)
    except Exception:
        return predicate, [
            "Could not parse the filter predicate to ground its values; left as authored."
        ]

    def _sides(node) -> tuple[Any, Any]:
        left, right = node.this, node.expression
        col = left if isinstance(left, _exp.Column) else (right if isinstance(right, _exp.Column) else None)
        lit = right if isinstance(right, _exp.Literal) else (left if isinstance(left, _exp.Literal) else None)
        return col, lit

    for cmp in list(tree.find_all(_exp.EQ, _exp.NEQ)):
        col, lit = _sides(cmp)
        if col is None or lit is None or not lit.is_string:
            continue
        ev = col_evidence.get(col.name.lower())
        if ev is None:
            continue
        raw = lit.this
        exact = _exact_observed(ev["values"], raw)
        if exact is not None:
            lit.set("this", exact)                       # exact casing; keep equality
        elif _is_text_type(ev["type"]):
            cmp.set("this", _exp.Lower(this=col.copy()))
            cmp.set("expression", _exp.Lower(this=lit.copy()))
        else:
            warnings.append(
                f"Filter value '{raw}' on column '{col.name}' could not be matched to "
                "an observed value and the column isn't text — verify it matches stored data."
            )

    for in_node in list(tree.find_all(_exp.In)):
        col = in_node.this if isinstance(in_node.this, _exp.Column) else None
        if col is None:
            continue
        ev = col_evidence.get(col.name.lower())
        if ev is None:
            continue
        str_lits = [e for e in (in_node.expressions or []) if isinstance(e, _exp.Literal) and e.is_string]
        if not str_lits:
            continue
        unmatched = False
        for e in str_lits:
            exact = _exact_observed(ev["values"], e.this)
            if exact is not None:
                e.set("this", exact)
            else:
                unmatched = True
        if unmatched and _is_text_type(ev["type"]):
            in_node.set("this", _exp.Lower(this=col.copy()))
            for e in str_lits:
                e.replace(_exp.Lower(this=e.copy()))

    try:
        return tree.sql(dialect=sg), warnings
    except Exception:
        return predicate, warnings


# ── endpoint ─────────────────────────────────────────────────────────────────


async def interpret_filter_intent(
    *,
    intent: str,
    columns: list[dict] | None = None,
    source_columns: list[dict] | None = None,
    dialect: str = "postgres",
    project: Project | None = None,
    output_dataset_uri: str | None = None,
    notes: str = "",
) -> InterpretResponse:
    """Core interpret logic, callable internally (no FastAPI request needed).

    When `project` AND `output_dataset_uri` are given, enriches the grounding
    context with the source columns feeding THAT output dataset (via its current
    mappings) + their observed profile values from the graph. Runs the skill,
    falling back to the deterministic heuristic. Never echoes raw prose as the
    predicate. Used by both the HTTP endpoint and the serving-stage auto-compile
    hook.
    """
    warnings: list[str] = []
    src_cols = list(source_columns or [])

    if project is not None and output_dataset_uri:
        enriched, any_tv = _enrich_source_columns(project, output_dataset_uri)
        if enriched:
            src_cols = enriched
            if not any_tv:
                warnings.append(
                    "Value-level grounding is unavailable for the source columns; "
                    "the engineer should verify the exact value."
                )

    inputs = {
        "intent": intent,
        "columns": list(columns or []),
        "source_columns": src_cols,
        "dialect": dialect,
        "notes": notes or "",
    }

    payload, err = await _run_interpreter_skill(inputs)
    if err or not payload:
        payload = _heuristic_interpret(inputs)

    # Type-safe value grounding over the compiled predicate: fix casing to the
    # exact observed value (index-friendly equality) or LOWER()-wrap an unmatched
    # TEXT value so it can't silently return zero rows. No-op when no evidence.
    grounded_predicate = str(payload.get("predicate") or "").strip()
    if grounded_predicate:
        col_evidence = _build_col_evidence(list(columns or []), src_cols)
        grounded_predicate, ground_warnings = _ground_predicate_literals(
            grounded_predicate, col_evidence, dialect
        )
        payload["predicate"] = grounded_predicate
        warnings.extend(ground_warnings)

    return InterpretResponse(
        readback=str(payload.get("readback") or "").strip()
        or "Could not interpret this filter; an engineer will finalize it.",
        predicate=str(payload.get("predicate") or "").strip(),
        confidence=int(payload.get("confidence") or 0),
        warnings=list(payload.get("warnings") or []) + warnings,
        grounded_columns=list(payload.get("grounded_columns") or []),
        _fallback=bool(payload.get("_fallback", False)),
    )


@router.post("/interpret", response_model=InterpretResponse)
async def interpret_filter(
    body: InterpretRequest,
    session: Session = Depends(get_session),
):
    """Interpret a plain-language dataset filter into a grounded SQL predicate.

    Returns a plain-language `readback` (shown to the PO), the compiled
    `predicate` (stored silently / finalized by the engineer), a confidence,
    and warnings. Never echoes the raw prose as the predicate.
    """
    project = session.get(Project, body.project_id) if body.project_id is not None else None
    return await interpret_filter_intent(
        intent=body.intent,
        columns=[c.model_dump() for c in body.columns],
        source_columns=[c.model_dump() for c in body.source_columns],
        dialect=body.dialect,
        project=project,
        output_dataset_uri=body.output_dataset_uri,
        notes=body.notes or "",
    )
