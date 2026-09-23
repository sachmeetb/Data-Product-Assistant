"""
server.py — FastAPI backend combining the SQL Pipeline and DPB Agent flows.

SQL Pipeline state machine (/chat):
  initial                → orchestrator gathers spec OR JSON spec detected
  awaiting_test_approval → PG + Test done; waiting for user to Proceed or Regenerate
  awaiting_approval      → Publisher analysis done; waiting for Approve or Cancel
  complete               → published to BigQuery

DPB Agent state machine (/dpb/chat):
  initial              → requirement_understanding gathers requirement
  clarifying           → multi-turn clarification
  phase_b_clarifying   → server-enforced technical questions
  confirm_requirement  → human gate 1: Confirm / Edit
  confirm_classification → human gate 2: Confirm / Override
  confirm_discovery    → final gate: Confirm → data product
  complete             → data product generated
"""

import asyncio
import json
import os
import re
import sys
import uuid
import yaml
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
from agents import (
    orchestrator, pipeline_generator, test_agent, publisher_agent,
    requirement_understanding, use_case_classification, discovery, challenger,
    data_product, ddi_pipeline, gold_layer_agent, silver_layer_agent,
    kpi_derivation,
)
from agents.base import create_thread
from tools.pdf_report import (
    generate_requirements_pdf,
    generate_classification_pdf,
    generate_discovery_pdf,
    generate_challenger_pdf,
)
from tools.domain_scope import classify_domain, out_of_scope_message
from tools.file_extractor import extract_file
from tools.gcs_file_store import _store as _gcs_files
from session_store import SessionStore
from requirements_gate import (
    decide_next_step,
    get_blocking_mandatories as _gate_blocking_mandatories,
    ACTION_SHOW_CARD,
    ACTION_CLARIFY_WITH_ESCAPE,
)

app = FastAPI(title="AI Retail Data Agent API", version="4.0.0")

_default_origins = "http://localhost:5173,https://dataagents3-ui.azurewebsites.net"
_cors_origins = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", _default_origins).split(",")
    if o.strip()
]

# Cloud Run serves every service under two URL formats simultaneously:
#   https://<svc>-<hash>-<regioncode>.a.run.app   (legacy)
#   https://<svc>-<projectnum>.<region>.run.app    (new)
# The browser Origin can be either, so match any *.run.app frontend host by
# regex to avoid CORS breaking whenever the caller uses the other format.
_cors_origin_regex = r"https://[a-z0-9-]+(\.[a-z0-9-]+)?\.run\.app"

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=_cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_sessions = SessionStore(prefix="dp:session:")     # Redis-backed; survives restarts + shared across workers
_file_store: dict = {}   # {file_id: {name, content, media_type}}
MAX_TEST_ITERATIONS = 5

_APPROVE_KEYWORDS = {
    "approve", "yes", "publish", "confirm", "go ahead",
    "looks good", "proceed", "ok", "okay", "do it", "approved",
}
_REGENERATE_KEYWORDS = {
    "regenerate", "no", "retry", "redo", "reject", "again",
    "change", "different", "new pipeline", "rerun",
}

async def _classify_gate_intent(message: str, gate: str, session_id: str, session: dict) -> str:
    """Classify the user's typed reply at a confirm gate as 'confirm' or 'reject'.

    Routes the message through the orchestrator agent (Mode D — gate_intent in
    prompts/orchestrator/SKILL.md). All intent rules live in that SKILL.md;
    no keywords are hardcoded here. A FRESH orchestrator session is used so
    relay history does not bleed in and confuse the classification.
    Falls back to 'reject' so the edit/override branch handles ambiguous replies safely.
    """
    fresh_session = create_thread("orchestrator", thread_id=f"gate_{session_id}")
    try:
        result = await orchestrator.run(
            message,
            context={"mode": "gate_intent", "gate": gate},
            session=fresh_session,
        )
        intent_val = (result.get("intent") or "").strip().lower()
        if intent_val == "confirm":
            return "confirm"
        if intent_val == "reject":
            return "reject"
        # Fallback: scan raw output in case intent field was omitted
        raw = (result.get("raw_output") or "").lower()
        if "confirm" in raw and "reject" not in raw:
            return "confirm"
        return "reject"
    except Exception:
        return "reject"


async def _dispatch_via_orchestrator(
    previous_agent: str,
    previous_output: dict,
    session_id: str,
    session: dict,
    flow_track: str = "full",
    user_action: str = "confirm",
) -> tuple[str | None, dict]:
    """Ask the orchestrator (Mode E — dispatch) which agent runs next and
    what payload to pass. Returns (next_agent_name, input_payload).
    Returns (None, {}) on chain-complete, upstream-blocked, or any failure.

    All sequencing knowledge lives in prompts/orchestrator/SKILL.md — server.py
    only resolves the returned agent name to a Python function and executes it.
    """
    fresh_session = create_thread("orchestrator", thread_id=f"dispatch_{session_id}")
    try:
        result = await orchestrator.run(
            "Decide the next agent and prepare its input payload.",
            context={
                "mode": "dispatch",
                "previous_agent": previous_agent,
                "previous_output": previous_output,
                "flow_track": flow_track,
                "user_action": user_action,
            },
            session=fresh_session,
        )
        next_agent = result.get("next_agent")
        if not next_agent or str(next_agent).lower() in ("null", "none", ""):
            return None, {}
        payload = result.get("input_payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        print(f"[DISPATCH] {previous_agent} → {next_agent} (reason: {result.get('reasoning', '?')})")
        return str(next_agent), payload
    except Exception as exc:
        print(f"[DISPATCH] orchestrator dispatch failed: {exc!r}")
        return None, {}


_OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
os.makedirs(_OUTPUT_DIR, exist_ok=True)


def _save_output_json(filename: str, data: dict) -> None:
    """Persist an agent output JSON to backend/output/ for later inspection."""
    path = os.path.join(_OUTPUT_DIR, filename)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        print(f"[OUTPUT] wrote {path} ({len(json.dumps(data, default=str))} bytes)")
    except Exception as exc:
        print(f"[OUTPUT] FAILED to write {path}: {exc!r}")


_CLS_TYPE_LABELS: dict[str, str] = {
    "analytics":      "Analytics",
    "data_science":   "Data Science",
    "genai":          "GenAI",
    "digital_nosql":  "Digital / NoSQL",
    "conformed_data": "Conformed Data",
    "agentic":        "Agentic",
}

_CLS_SCHEMA_LABELS: dict[str, str] = {
    "star_schema":              "Star-schema pipeline (fact + dim)",
    "wide_flat_feature_table":  "Wide flat feature table",
    "flat_denormalised":        "Flat denormalised store",
    "event_schema":             "Event-schema pipeline",
    "entity_schema":            "Entity schema (golden record)",
}


def _format_classification_view(cls_result: dict) -> dict:
    """Build a frontend-ready classification card payload (no confidence)."""
    use_case_type  = cls_result.get("use_case_type", "")
    schema_pattern = cls_result.get("schema_design_pattern", "")
    raw_signals    = cls_result.get("signals_matched") or []
    signals = [
        s["signal"] if isinstance(s, dict) else str(s)
        for s in raw_signals
    ]
    return {
        "use_case_type":  use_case_type,
        "use_case_label": _CLS_TYPE_LABELS.get(
            use_case_type,
            use_case_type.replace("_", " ").title(),
        ),
        "schema_pattern": schema_pattern,
        "schema_label":   _CLS_SCHEMA_LABELS.get(
            schema_pattern,
            schema_pattern.replace("_", " ").title(),
        ),
        "signals":   signals,
        "rationale": cls_result.get("rationale", ""),
    }


_DATE_WORDS = frozenset({
    "date", "day", "week", "month", "year", "time", "period", "quarter",
    "timestamp", "created", "updated", "dt",
})


def _build_glossary(data_points: list[dict]) -> dict:
    """Build a business-glossary payload from a RequirementsOutput data_points list."""
    entries = []
    for dp in data_points:
        name = dp.get("name", "")
        description = dp.get("description", "")
        kind = dp.get("kind", "attribute")
        is_derived = dp.get("is_derived")

        if kind == "kpi" and is_derived:
            type_label = "Measure · derived"
            type_color = "yellow"
            sql_type = "DECIMAL"
        elif kind == "kpi":
            type_label = "Measure · additive"
            type_color = "green"
            sql_type = "BIGINT"
        else:
            type_label = "Dimension"
            type_color = "blue"
            name_words = set(name.lower().replace("-", " ").replace("_", " ").split())
            sql_type = "DATE" if name_words & _DATE_WORDS else "STRING"

        entries.append({
            "name": name,
            "description": description,
            "type_label": type_label,
            "type_color": type_color,
            "sql_type": sql_type,
        })

    return {"entries": entries, "column_count": len(entries)}


def _build_file_metrics_message(
    extraction: dict,
    user_context: str = "",
) -> tuple[list[str], list[str], list[str], str]:
    """
    For a structured file (Excel/JSON) with numeric columns, build a
    natural-language confirmation message and dynamic chip options.

    Returns (primary_metrics, secondary_metrics, chips, message_text).
    primary_metrics  — up to 3 numeric columns surfaced as the main metrics.
    secondary_metrics — remaining numeric columns offered as add-ons.
    chips            — ["Yeah, those are enough", "Add <col>", ...].
    message_text     — conversational agent message shown to the user.
    """
    preview = extraction.get("preview", {})
    cols = preview.get("columns", [])
    col_types = preview.get("col_types", [])

    numeric_cols = [c for c, t in zip(cols, col_types) if t == "number"]

    if not numeric_cols:
        return [], [], [], ""

    primary = numeric_cols[:3]
    secondary = numeric_cols[3:]

    if len(primary) == 1:
        primary_str = primary[0]
    elif len(primary) == 2:
        primary_str = f"{primary[0]} and {primary[1]}"
    else:
        primary_str = ", ".join(primary[:-1]) + f", and {primary[-1]}"

    n = len(primary)
    msg = (
        f"Perfect, that helps a lot. I can see {n} number{'s' if n > 1 else ''} in "
        f"there — {primary_str}. My hunch is {'those are' if n > 1 else 'that is'} the "
        f"{'ones' if n > 1 else 'one'} that matter most"
        + (f" for {user_context.strip()}" if user_context.strip() else "")
        + ", but if you also want "
    )
    if secondary:
        sec_sample = ", ".join(secondary[:3])
        msg += f"{sec_sample}, say the word and we'll fold {'them' if len(secondary) > 1 else 'it'} in."
    else:
        msg = (
            f"Perfect, that helps a lot. I can see {n} number{'s' if n > 1 else ''} in "
            f"there — {primary_str}. Those are the only numeric measures in the file — "
            "does that cover what you need?"
        )

    chips = ["Yeah, those are enough"]
    for col in secondary[:3]:
        chips.append(f"Add {col}")

    return primary, secondary, chips, msg


_STARTING_POINT_MESSAGE = (
    "## Where would you like to start?\n\n"
    "**1. DPI — Help me find the data** — Discover existing data products and catalog tables that match your requirements.\n\n"
    "**2. DDI — Help me design it** — Paste a Discovery output JSON; I'll build the ER + STTM + gold catalog.\n\n"
    "**3. DPB — Just build it** — Have a pipeline spec already? Paste it as JSON and I'll generate the SQL directly.\n\n"
    "Or describe your requirement directly and I'll route you to the right place."
)

# Chip label prefixes (lower-cased) → pipeline_type for direct routing without LLM
_WELCOME_CHIP_ROUTES: dict[str, str] = {
    "help me find the data": "dpi",
    "help me design it": "ddi",
    "just build it": "sql",
}

_WELCOME_CHIP_REPLIES: dict[str, str] = {
    "dpi": (
        "Got it — let's find your data.\n\n"
        "Tell me what you're looking for: describe your use case, the KPIs you need, "
        "or the business question you're trying to answer."
    ),
    "ddi": (
        "Let's design the gold layer from a discovery output.\n\n"
        "Paste a **valid JSON** matching the Discovery agent's final output — it must contain at "
        "least one of `gold_matches`, `silver_matches`, or `bronze_matches`. I'll then run "
        "gold-er → silver-sttm → gold-final, update `utility_catalog.json`, and hand off to the "
        "pipeline builder (DPB)."
    ),
    "sql": (
        "Ready to build!\n\n"
        "Paste your pipeline spec as JSON (source tables, STTM, domain, pipeline type) "
        "and I'll generate the SQL for you."
    ),
}


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str
    action: Optional[str] = None  # "confirm" | "edit" | "override" | "use_file"
    file_ref_id: Optional[str] = None  # set when action == "use_file"


# ── DPB request / response models ────────────────────────────────────────────

class DPBChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str
    action: Optional[str] = None  # "confirm" | "edit" | "override"


class RequirementRequest(BaseModel):
    requirement: str


class ClarificationResponse(BaseModel):
    answer: str
    original_input: str = ""
    agent_history: list[dict] = Field(default_factory=list)
    clarification_pass: int = 0


class ConfirmRequirementRequest(BaseModel):
    requirement_output: dict
    action: str = "confirm"
    original_input: str = ""
    agent_history: list[dict] = Field(default_factory=list)


class ClassifyRequest(BaseModel):
    confirmed_requirement: dict


class ConfirmClassificationRequest(BaseModel):
    classification_output: dict
    action: str = "confirm"


# ── DPB helpers ───────────────────────────────────────────────────────────────

def _build_agent_context(session: dict, extra: Optional[dict] = None) -> dict:
    ctx = {
        "original_input": session.get("original_input", ""),
        "conversation_history": session.get("agent_history", []),
        "clarification_pass": session.get("clarification_pass", 0),
    }
    if extra:
        ctx.update(extra)
    return ctx


def _last_agent_question(session: dict) -> str:
    convo = session.get("conversation_history", [])
    for msg in reversed(convo):
        if msg.get("role") == "assistant":
            return msg.get("content", "")
    return ""


_PHASE_B_OPTIONALS = ("consumer_role", "data_freshness", "granularity", "data_sources")

_PHASE_B_KEYWORDS = {
    "consumer_role": ("consumer", "who will use", "primary user", "consumer role", "who consumes"),
    "data_freshness": ("freshness", "how current", "real-time", "real time", "daily", "weekly", "monthly", "cadence", "refresh"),
    "granularity": ("broken down by", "dimensions", "granularity", "slice and group", "per "),
    "data_sources": ("source system", "source", "where the data lives", "crm", "salesforce", "sap"),
}

_PHASE_B_QUESTIONS = {
    "consumer_role": "Who is the primary consumer of this data — for example, a Data Analyst, Data Scientist, App Developer, or a specific team or role?",
    "data_freshness": "How current does the data need to be — real-time, daily, weekly, or monthly?",
    "granularity": "Do you need this broken down by any dimensions (like country, product, region, or business unit)?",
    "data_sources": "Which source systems should this come from (Salesforce, SAP, others — or 'unknown' if you're not sure yet)?",
}


def _was_field_asked(field: str, agent_history: list) -> bool:
    keywords = _PHASE_B_KEYWORDS.get(field, ())
    if not keywords:
        return False
    for entry in agent_history:
        question = (entry.get("agent_question") or "").lower()
        if any(kw in question for kw in keywords):
            return True
    return False


def _field_is_resolved(field: str, output: dict) -> bool:
    fs = output.get("field_status", {}) or {}
    for key in ("confirmed", "inferred", "unknown_per_user"):
        if field in (fs.get(key) or []):
            return True
    return False


def _phase_b_owed_fields(output: dict, agent_history: list) -> list[str]:
    owed = []
    for field in _PHASE_B_OPTIONALS:
        if _field_is_resolved(field, output):
            continue
        if _was_field_asked(field, agent_history):
            continue
        owed.append(field)
    return owed


def _compose_phase_b_question(owed_fields: list[str]) -> str:
    if not owed_fields:
        return ""
    lines = ["Great, that covers the business side. Now a few quick technical questions:\n"]
    for i, field in enumerate(owed_fields, start=1):
        lines.append(f"{i}. {_PHASE_B_QUESTIONS[field]}")
    lines.append("\nSay 'unknown' for anything you're not sure about.")
    return "\n".join(lines)


def _on_agent_complete(
    session_id: str,
    session: dict,
    result: dict,
    conversation_history: list,
    agent_history: list,
    clarification_pass: int,
    extra_session: Optional[dict] = None,
) -> tuple[str, list, dict]:
    owed = _phase_b_owed_fields(result, agent_history)

    base_session = {
        **session,
        "data": result,
        "conversation_history": conversation_history,
        "agent_history": agent_history,
        "clarification_pass": clarification_pass,
    }
    if extra_session:
        base_session.update(extra_session)

    if owed:
        phase_b_text = _compose_phase_b_question(owed)
        if conversation_history and conversation_history[-1].get("role") == "assistant":
            conversation_history[-1] = {"role": "assistant", "content": phase_b_text}
        else:
            conversation_history.append({"role": "assistant", "content": phase_b_text})
        base_session.update({
            "step": "phase_b_clarifying",
            "conversation_history": conversation_history,
            "phase_b_owed": owed,
            "phase_b_question": phase_b_text,
        })
        return phase_b_text, [], base_session

    base_session["step"] = "confirm_requirement"
    summary_text = result.get("display_output", "Requirements gathered. Please confirm.")
    return summary_text, ["Confirm", "Edit"], base_session


VALID_USE_CASE_TYPES = (
    "analytics", "data_science", "genai",
    "digital_nosql", "conformed_data", "agentic",
)


def _resolve_use_case_type(user_text: str) -> Optional[str]:
    if not user_text:
        return None
    text = user_text.strip().lower()
    if text in VALID_USE_CASE_TYPES:
        return text
    tokens = set(text.replace("_", " ").replace("-", " ").split())
    for vt in VALID_USE_CASE_TYPES:
        vt_tokens = vt.replace("_", " ").split()
        if all(t in tokens for t in vt_tokens):
            return vt
    synonyms = {
        "analytics": ("analytic", "reporting", "dashboard", "bi", "business intelligence"),
        "data_science": ("ml", "machine learning", "model", "modeling", "predictive", "ai model"),
        "genai": ("generative ai", "generative", "llm", "chatbot", "gpt"),
        "digital_nosql": ("nosql", "document store", "key value", "operational"),
        "conformed_data": ("conformed", "master data", "reference data"),
        "agentic": ("agent", "agents", "autonomous"),
    }
    for canonical, syns in synonyms.items():
        if any(syn in text for syn in syns):
            return canonical
    return None


def _fmt_score(conf_val) -> str:
    try:
        return f"{float(conf_val):.2f}"
    except (TypeError, ValueError):
        return str(conf_val)


def _fmt_status(status: str) -> str:
    return {"reuse": "REUSE", "extend": "EXTEND", "build_new": "BUILD NEW"}.get(status, status.upper())


def _render_architecture_diagram(diagram: dict) -> str:
    if not diagram or not diagram.get("lanes"):
        return ""
    lines = ["\n---\n### Architecture Diagram\n", "```"]
    lanes = diagram.get("lanes", [])
    width = 72
    for i, lane in enumerate(lanes):
        label = lane.get("label", lane.get("layer", "").upper())
        cards = lane.get("cards", [])
        lines.append("┌" + "─" * width + "┐")
        lines.append("│  " + label.ljust(width - 2) + "│")
        lines.append("│" + " " * width + "│")
        if not cards:
            lines.append("│  (no matches)" + " " * (width - 14) + "│")
        else:
            for card in cards:
                score  = f"{card.get('confidence', 0.0):.2f}"
                name   = card.get("label", card.get("full_name", ""))[-42:]
                status_label = {"reuse": "REUSE", "extend": "EXTEND", "build_new": "BUILD-NEW"}
                status = status_label.get(card.get("status", ""), card.get("status", ""))
                cc     = "  [close call]" if card.get("close_call") else ""
                gaps   = f"  [{card.get('missing_count', 0)} gap(s)]" if card.get("missing_count") else ""
                row    = f"  {name}   {status} | {score}{cc}{gaps}"
                lines.append("│" + row.ljust(width) + "│")
        lines.append("│" + " " * width + "│")
        lines.append("└" + "─" * width + "┘")
        if i < len(lanes) - 1:
            lines.append(" " * (width // 2 - 1) + "^")
    conflicts = diagram.get("conflict_markers", [])
    if conflicts:
        lines.append("")
        lines.append("Conflicts (human review required):")
        for c in conflicts:
            layers = ", ".join(c.get("layers", []))
            lines.append(f"  * {c.get('field', '?')}  found in: {layers}")
    lines.append("")
    lines.append("Legend:  REUSE = use as-is   EXTEND = needs additions   BUILD-NEW = build from scratch")
    lines.append("```")
    return "\n".join(lines)


def _format_discovery_output(result: dict) -> str:
    """Render discovery agent output as clean markdown."""
    if "raw_output" in result and not any(
        k in result for k in (
            "gold_matches", "silver_matches", "bronze_matches",
            "consumption_matches", "aggregated_matches", "source_matches",
            "matches_found",
        )
    ):
        return "## Discovery Results\n\n" + result["raw_output"] + "\n\nPlease confirm to continue."

    lines = ["## Discovery Results\n"]

    s = result.get("summary", {})
    if isinstance(s, dict) and s.get("total_matches") is not None:
        total  = s.get("total_matches", 0)
        reuse  = s.get("reuse_count", 0)
        extend = s.get("extend_count", 0)
        build  = s.get("build_new_count", 0)
        cc     = s.get("close_calls_flagged", 0)
        lines.append(
            f"**{total} candidates** across 3 layers — "
            f"{reuse} reuse, {extend} extend, {build} build new"
            + (f" | {cc} close call(s)" if cc else "")
        )
        lines.append("")

    # Verdict recommendation (Reuse / Extend / Build New) — mirrors the
    # discovery card's verdict banner.
    verdicts = result.get("discovery_view", {}).get("verdicts", {})
    if isinstance(verdicts, dict) and any(verdicts.get(k) for k in ("reuse", "extend", "build_new")):
        lines.append("### Recommendation\n")
        for vkey, vlabel in (("reuse", "Reuse"), ("extend", "Extend"), ("build_new", "Build New")):
            items = verdicts.get(vkey) or []
            if not items:
                continue
            lines.append(f"**{vlabel}**")
            for it in items:
                tbl = it.get("table", "")
                rationale = it.get("rationale", "")
                lines.append(f"- `{tbl}` — {rationale}" if rationale else f"- `{tbl}`")
            lines.append("")

    v3_keys = [("gold_matches", "Gold Layer"), ("silver_matches", "Silver Layer"), ("bronze_matches", "Bronze Layer")]
    v2_keys = [("consumption_matches", "Gold Layer"), ("aggregated_matches", "Silver Layer"), ("source_matches", "Bronze Layer")]
    v3 = any(result.get(k) for k, _ in v3_keys)
    v2 = any(result.get(k) for k, _ in v2_keys)
    layer_keys = v3_keys if v3 else v2_keys if v2 else None

    if not layer_keys:
        matches = result.get("matches_found", [])
        if matches:
            lines.append("| Item | Path | Score | Decision |")
            lines.append("|------|------|-------|----------|")
            for m in matches:
                lines.append(f"| {m.get('requested_item','')} | `{m.get('bigquery_path') or m.get('unity_catalog_path', '—')}` | {m.get('confidence','')} | {m.get('match_type','')} |")
        lines.append("\n---\nConfirm to complete the pipeline, or type feedback to adjust.")
        return "\n".join(lines)

    all_gaps: list[str] = []
    all_suggested: list[str] = []

    for key, layer_label in layer_keys:
        layer_matches = result.get(key, [])
        if not layer_matches:
            continue

        def _has_kpi_match(m):
            return any(k.get("coverage") in ("full", "partial") for k in m.get("kpi_matches", []))

        relevant = [m for m in layer_matches if _has_kpi_match(m)]
        other    = [m for m in layer_matches if not _has_kpi_match(m)]

        # NOTE (Fix D): Previously, if `relevant` was empty we promoted the
        # highest-scoring table to keep the layer non-empty. That contradicted
        # the "render blank rather than force a wrong match" rule and produced
        # bogus headlines like "tenure → etl_load_timestamp (partial)" being
        # surfaced as a real match. The fallback is removed — empty layers
        # now render an honest "no matches" line.

        lines.append(f"### {layer_label}\n")

        if not relevant:
            # No table in this layer has a real KPI match. Be honest about it.
            if other:
                other_list = ", ".join(f"`{m.get('name','').split('.')[-1]}`" for m in other)
                lines.append(
                    f"*No tables in this layer match the requested KPIs. "
                    f"{len(other)} candidate(s) inspected: {other_list}*\n"
                )
            else:
                lines.append("*No matches in this layer.*\n")
            continue

        for m in relevant:
            name_full  = m.get("name", "—")
            name_short = name_full.split(".")[-1]
            conf_val   = m.get("match_confidence", {})
            if isinstance(conf_val, dict):
                conf_val = conf_val.get("overall_confidence", 0)
            score      = _fmt_score(conf_val)
            status_str = _fmt_status(m.get("status", ""))
            cc_note    = "  [close call]" if m.get("close_call") else ""

            # KPI coverage counts (Fix C):
            # Previously every non-"none" coverage value (including "partial"
            # and "description_only") was counted toward the headline "X/Y
            # matched" — which produced the misleading "2/2 matched" the user
            # saw when both rows were partial guesses. Now we count only
            # `full` matches in the headline, and surface the partial count
            # separately so it's visible without being conflated.
            kpi_matches = m.get("kpi_matches", [])
            dim_matches = m.get("dimension_matches", [])
            kpi_full      = sum(1 for k in kpi_matches if k.get("coverage") == "full")
            kpi_partial  = sum(1 for k in kpi_matches if k.get("coverage") == "partial")
            kpi_total    = len(kpi_matches)
            dim_hit       = sum(1 for d in dim_matches if d.get("coverage") == "matched")
            dim_total    = len(dim_matches)

            lines.append(f"**{name_short}**  |  Score: {score}  |  Decision: {status_str}{cc_note}")
            lines.append(f"`{name_full}`")
            lines.append("")

            if kpi_matches or dim_matches:
                if kpi_matches:
                    if kpi_partial:
                        kpi_header = (
                            f"KPI coverage ({kpi_full}/{kpi_total} matched, "
                            f"{kpi_partial} partial):"
                        )
                    else:
                        kpi_header = f"KPI coverage ({kpi_full}/{kpi_total} matched):"
                    lines.append(kpi_header)
                    lines.append("| KPI | Matched Columns | Result |")
                    lines.append("|-----|-----------------|--------|")
                    for k in kpi_matches:
                        cols_str = ", ".join(f"`{c}`" for c in k.get("matched_columns", [])) or "—"
                        cov = k.get("coverage", "none")
                        result_label = {"full": "matched", "partial": "partial", "description_only": "description only", "none": "no match"}.get(cov, cov)
                        lines.append(f"| {k.get('kpi','?')} | {cols_str} | {result_label} |")
                    lines.append("")

                if dim_matches:
                    lines.append(f"Dimension coverage ({dim_hit}/{dim_total} matched):")
                    lines.append("| Dimension | Matched Column | Result |")
                    lines.append("|-----------|----------------|--------|")
                    for d in dim_matches:
                        col = f"`{d['matched_column']}`" if d.get("matched_column") else "—"
                        result_label = "matched" if d.get("coverage") == "matched" else "no match"
                        lines.append(f"| {d.get('dimension','?')} | {col} | {result_label} |")
                    lines.append("")

            for info in m.get("missing_information", []):
                if info and info not in all_gaps:
                    all_gaps.append(info)
            for sug in m.get("suggested_names", []):
                if sug and sug not in all_suggested:
                    all_suggested.append(sug)

            lines.append("---")

        if other:
            other_list = ", ".join(f"`{m.get('name','').split('.')[-1]}`" for m in other)
            lines.append(f"*{len(other)} other candidate(s) with no direct KPI match: {other_list}*\n")

        lines.append("")

    if all_gaps:
        lines.append("### Information Gaps\n")
        for gap in all_gaps:
            lines.append(f"- {gap}")
        if all_suggested:
            lines.append("")
            lines.append(f"Suggested new columns: {', '.join(f'`{s}`' for s in all_suggested)}")
        lines.append("")

    conflicts = result.get("conflicts", [])
    if conflicts:
        lines.append("### Conflicts — Human Review Required\n")
        for c in conflicts:
            lines.append(f"- **{c.get('requested_item','?')}** — {c.get('reason','')}")
        lines.append("")

    diagram = result.get("architecture_diagram")
    if diagram:
        lines.append(_render_architecture_diagram(diagram))

    lines.append("\n---\nConfirm to complete the pipeline, or type feedback to adjust.")
    return "\n".join(lines)


# ── DPB session store ─────────────────────────────────────────────────────────
_dpb_sessions = SessionStore(prefix="dpb:session:")   # Redis-backed; survives restarts + shared across workers


def _get_or_create_adk_session(session: dict, skill_name: str, thread_id: str) -> str:
    """
    Return the ADK session_id string for skill_name within this HTTP session,
    creating it on first access.  The thread_id (= HTTP session_id) is used as
    the ADK session_id so all OpenTelemetry spans share the same identifier.
    """
    adk_sessions = session.setdefault("adk_sessions", {})
    if skill_name not in adk_sessions:
        adk_sessions[skill_name] = create_thread(skill_name, thread_id=thread_id)
    return adk_sessions[skill_name]


def _coerce_req_complete(result: dict) -> dict:
    """
    Some skill versions return structured RequirementsOutput fields alongside
    raw_output (e.g. a 'locked and confirmed' confirmation summary mixed into
    the JSON response). is_complete() rejects any result that contains raw_output,
    so the flow would stall forever.

    If all mandatory fields are present and no error occurred, strip raw_output
    so is_complete() recognises the result as finished and advances the flow.
    """
    if (
        "raw_output" in result
        and "use_case_name" in result
        and ("data_points" in result or "kpis" in result)
        and "error" not in result
    ):
        return {k: v for k, v in result.items() if k != "raw_output"}
    return result


# Fields the Edit-requirements form knows how to render. Used to seed the form
# with whatever partial data we have when we surface the manual-edit escape
# hatch mid-clarification (see the requirements dead-end fix in _handle_dpi_chat).
_REQ_SNAPSHOT_FIELDS = (
    "use_case_name", "domain", "consumer_role", "data_freshness",
    "data_points", "kpis", "granularity", "data_sources", "filters",
    "field_status",
)


def _partial_req_snapshot(result: dict) -> dict:
    """
    Build a best-effort RequirementsOutput-shaped dict from a possibly-incomplete
    agent result so the frontend Edit form can open and let the user fill in the
    missing mandatory fields by hand. Never raises; returns {} when there's
    nothing usable (the form then opens blank).
    """
    if not isinstance(result, dict):
        return {}
    return {k: result[k] for k in _REQ_SNAPSHOT_FIELDS if k in result and result[k] is not None}


# ── File store helpers ────────────────────────────────────────────────────────

def _save_file(
    name: str,
    content: str | bytes,
    media_type: str = "text/plain",
    session_id: str | None = None,
    is_upload: bool = False,
) -> str:
    file_id = str(uuid.uuid4())[:12]
    _file_store[file_id] = {"name": name, "content": content, "media_type": media_type}
    _gcs_files.put(file_id, name, content, media_type, session_id=session_id, is_upload=is_upload)
    return file_id


def _get_file(file_id: str) -> dict | None:
    """Return a stored file entry, falling back to GCS when this instance's
    in-process store doesn't have it (files uploaded to another instance)."""
    entry = _file_store.get(file_id)
    if entry is not None:
        return entry
    entry = _gcs_files.get(file_id)
    if entry is not None:
        _file_store[file_id] = entry  # cache locally
    return entry


def _attach_pdf(files: list, name: str, pdf_bytes: bytes, label: str) -> None:
    """Append a PDF file entry to `files` after persisting it in the file store."""
    pdf_id = _save_file(name, pdf_bytes, "application/pdf")
    files.append({"id": pdf_id, "name": name, "label": label})


def _save_requirement_files(result: dict, session_id: str) -> list[dict]:
    """Save a RequirementsOutput as JSON + PDF and return file descriptors for both."""
    short = session_id[:8]
    file_entries = []

    json_id = _save_file(
        f"requirement-summary-{short}.json",
        json.dumps(result, indent=2, ensure_ascii=False, default=str),
        "application/json",
    )
    file_entries.append({
        "id": json_id,
        "name": f"requirement-summary-{short}.json",
        "label": "Requirement Summary (JSON)",
    })

    try:
        pdf_bytes = generate_requirements_pdf(result)
        pdf_id = _save_file(
            f"requirement-summary-{short}.pdf",
            pdf_bytes,
            "application/pdf",
        )
        file_entries.append({
            "id": pdf_id,
            "name": f"requirement-summary-{short}.pdf",
            "label": "Requirement Summary (PDF)",
        })
    except Exception:
        pass

    return file_entries


def _bigquery_console_url() -> str:
    """Build BigQuery Console URL from env vars."""
    project = os.environ.get("BQ_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    if not project:
        return ""
    return f"https://console.cloud.google.com/bigquery?project={project}"


# ── SQL parsing helpers ───────────────────────────────────────────────────────

def _parse_tables_from_sql(sql_code: str) -> dict[str, list]:
    """Return {table_name: [col_dicts]} parsed from CREATE TABLE statements."""
    tables: dict[str, list] = {}
    current: str | None = None
    cols: list = []
    in_create = False

    for line in sql_code.splitlines():
        s = line.strip()
        m = re.match(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([`'\"\w.]+)",
            s, re.IGNORECASE,
        )
        if m:
            if current is not None:
                tables[current] = cols
            current = m.group(1).strip("`'\"")
            cols = []
            in_create = True
            continue

        if in_create:
            if re.match(r"\)\s*(USING|TBLPROPERTIES|PARTITIONED|;|$)", s, re.IGNORECASE) or s == ");":
                if current is not None:
                    tables[current] = cols
                current = None
                cols = []
                in_create = False
                continue
            cm = re.match(
                r"`?(\w+)`?\s+(STRING|INT|BIGINT|DOUBLE|FLOAT|BOOLEAN|DATE|TIMESTAMP|DECIMAL|LONG|BINARY)(\W|$)",
                s, re.IGNORECASE,
            )
            if cm:
                cols.append({
                    "name": cm.group(1),
                    "type": cm.group(2).upper(),
                    "not_null": "NOT NULL" in s.upper(),
                })
    return tables


def _parse_sql_values(values_str: str) -> list[str]:
    """Parse comma-separated SQL values respecting single-quote boundaries."""
    result: list[str] = []
    current: list[str] = []
    in_quote = False
    for char in values_str:
        if char == "'" and not in_quote:
            in_quote = True
        elif char == "'" and in_quote:
            in_quote = False
        elif char == "," and not in_quote:
            result.append("".join(current).strip())
            current = []
            continue
        else:
            current.append(char)
    if current:
        result.append("".join(current).strip())
    return result


def _extract_sample_rows(sql_code: str, limit: int = 3) -> dict:
    """
    Return {table_name: {columns: [...], rows: [...]}} for INSERT INTO/OVERWRITE … VALUES blocks.
    Handles explicit column lists: INSERT OVERWRITE table (col1, col2, ...) VALUES (...)
    Only captures the first `limit` rows per table.
    """
    tables = _parse_tables_from_sql(sql_code)
    result: dict = {}

    insert_pat = re.compile(
        r"INSERT\s+(?:INTO|OVERWRITE)\s+([\w.`]+)\s*(\([^)]*\))?\s*VALUES\s*([\s\S]+?)(?=\s*;)",
        re.IGNORECASE,
    )
    for match in insert_pat.finditer(sql_code):
        table       = match.group(1).strip("`")
        col_list    = match.group(2)   # "(col1, col2, ...)" or None
        values_str  = match.group(3)

        row_tuples = re.findall(r"\(([^)]+)\)", values_str)[:limit]
        if not row_tuples:
            continue

        # Prefer explicit column list from INSERT; fall back to CREATE TABLE definition
        if col_list:
            col_names = [c.strip() for c in col_list.strip("()").split(",")]
        else:
            col_names = [c["name"] for c in tables.get(table, [])]

        rows = [_parse_sql_values(rt) for rt in row_tuples]
        result[table] = {"columns": col_names, "rows": rows}

    return result


# ── Per-agent message formatters ──────────────────────────────────────────────

def _format_pg_message(generated_code: str, iterations: int, iterations_detail: list) -> str:
    """Pipeline Generator bubble: structured stats summary + table schemas + full SQL."""
    tables      = _parse_tables_from_sql(generated_code)
    merges      = len(re.findall(r"\bMERGE\s+INTO\b", generated_code, re.IGNORECASE))
    constraints = len(re.findall(r"\bADD\s+CONSTRAINT\b", generated_code, re.IGNORECASE))
    schemas     = len(re.findall(r"\bCREATE\s+SCHEMA\b", generated_code, re.IGNORECASE))
    creates     = len(re.findall(r"\bCREATE\s+TABLE\b", generated_code, re.IGNORECASE))
    alters      = len(re.findall(r"\bALTER\s+TABLE\b", generated_code, re.IGNORECASE))
    inserts     = len(re.findall(r"\bINSERT\s+(?:OVERWRITE|INTO)\b", generated_code, re.IGNORECASE))
    stmt_count  = len([s for s in generated_code.split(";") if s.strip()])

    lines: list[str] = [
        "### Pipeline Generation Summary",
        "",
        f"- **Iterations:** {iterations}",
        f"- **Total statements:** {stmt_count}",
        f"- **Schemas created:** {schemas}",
        f"- **Tables (CREATE):** {creates}",
    ]
    if alters:
        lines.append(f"- **Table changes (ALTER):** {alters}")
    lines += [
        f"- **STTM merges:** {merges}",
        f"- **Seed inserts:** {inserts}",
        f"- **Quality constraints:** {constraints}",
        "",
    ]

    if iterations > 1:
        corrections = [d for d in iterations_detail if d.get("status") == "failed"]
        if corrections:
            lines += [f"*{len(corrections)} auto-correction(s) applied.*", ""]

    silver_tables = {k: v for k, v in tables.items() if "_silver" in k}
    gold_tables   = {k: v for k, v in tables.items() if "_gold" in k}

    def _tbl(name: str, cols: list, label: str) -> list[str]:
        out = [f"#### `{name}`", f"{label} | {len(cols)} columns", ""]
        if cols:
            out += ["| Column | Type | Not Null |", "|--------|------|:--------:|"]
            for col in cols:
                nn = "✓" if col.get("not_null") else ""
                out.append(f"| `{col['name']}` | `{col['type']}` | {nn} |")
        out.append("")
        return out

    if silver_tables:
        lines += ["**Input Tables (Silver)**", ""]
        for n, c in silver_tables.items():
            lines += _tbl(n, c, "Silver / Input")

    if gold_tables:
        lines += ["**Output Tables (Gold)**", ""]
        for n, c in gold_tables.items():
            lines += _tbl(n, c, "Gold / Aggregated")

    lines += ["---", "**Generated SQL**", "", "```sql", generated_code, "```"]
    return "\n".join(lines)


def _format_test_message(test_report: dict) -> str:
    """Test Agent bubble: check results + simulated sample query output."""
    test_status   = test_report.get("test_status", "unknown")
    passed_checks = test_report.get("passed_checks", [])
    failures      = test_report.get("failures", [])
    test_summary  = test_report.get("summary", "")

    lines: list[str] = []
    if test_status == "passed":
        lines.append(f"**Final Status: All checks passed** ({len(passed_checks)} checks)")
    else:
        lines.append(f"**Final Status: Passed with {len(failures)} warning(s)**")

    if test_summary:
        lines += ["", f"*{test_summary}*"]

    if passed_checks:
        lines += ["", "**Checks passed:**"]
        for c in passed_checks:
            lines.append(f"- {c}")

    if failures:
        lines += ["", "**Warnings (non-blocking):**"]
        for f in failures:
            sev = f.get("severity", "WARN")
            lines.append(f"- `{f.get('check', '')}` [{sev}]: {f.get('detail', '')}")
            if f.get("suggestion"):
                lines.append(f"  - *{f['suggestion']}*")

    # ── Sample Query simulation ───────────────────────────────────────────────
    sqr = test_report.get("sample_query_result", {})
    if sqr:
        lines += ["", "---", "### Sample Query — How Your Data Will Look", ""]

        def _render_tbl(label: str, tbl: dict) -> None:
            nonlocal lines
            cols = tbl.get("columns", [])
            rows = tbl.get("rows", [])
            if not cols or not rows:
                return
            lines += [
                f"**{label}**",
                "",
                "| " + " | ".join(cols) + " |",
                "| " + " | ".join(["---"] * len(cols)) + " |",
            ]
            for row in rows[:5]:
                lines.append("| " + " | ".join(str(v) if v is not None else "" for v in row) + " |")
            lines.append("")

        input_tbl = sqr.get("input_table", {})
        if input_tbl:
            _render_tbl(f"Input: `{input_tbl.get('table_name', 'silver')}`", input_tbl)

        for out_tbl in sqr.get("output_tables", []):
            layer = out_tbl.get("layer", "output").title()
            _render_tbl(f"Output: `{out_tbl.get('table_name', '')}` ({layer})", out_tbl)

    return "\n".join(lines)


def _format_publisher_message(
    transformations: list,
    joins: list,
    aggregations: list,
    gold_output_tables: list,
    gold_column_descriptions: list | None = None,
) -> str:
    """Publisher Agent bubble: transformations, joins, aggregations, gold sample, column descriptions."""
    lines: list[str] = []

    if transformations:
        lines += ["### Transformations Applied", ""]
        for tr in transformations:
            lines += [
                f"**`{tr.get('from_table', '')}` → `{tr.get('to_table', '')}`**",
                "",
                "| Source Column | Expression | Target Column | Notes |",
                "|--------------|-----------|--------------|-------|",
            ]
            for m in tr.get("mappings", []):
                lines.append(
                    f"| `{m.get('source_col', '')}` "
                    f"| `{m.get('expression', '')}` "
                    f"| `{m.get('target_col', '')}` "
                    f"| {m.get('notes', '')} |"
                )
            lines.append("")

    if joins:
        lines += ["### Joins Used", ""]
        for j in joins:
            lines += [
                f"**{j.get('join_type', 'JOIN')}:** `{j.get('left_table', '')}` + `{j.get('right_table', '')}`",
                f"Output: `{j.get('output_table', '')}` | On: `{j.get('join_condition', '')}`",
                j.get("purpose", ""),
                "",
            ]

    if aggregations:
        lines += ["### Gold Aggregations", ""]
        for agg in aggregations:
            group_cols = ", ".join(f"`{c}`" for c in agg.get("group_by_cols", []))
            lines += [
                f"**`{agg.get('table', '')}`** — GROUP BY: {group_cols}",
                "",
                "**Measures:**",
            ]
            for m in agg.get("measures", []):
                lines.append(f"- `{m}`")
            lines.append("")

    if gold_output_tables:
        lines += ["### Gold Layer — Sample Output", ""]
        for tbl in gold_output_tables:
            cols = tbl.get("columns", [])
            rows = tbl.get("rows", [])
            if not cols or not rows:
                continue
            lines += [
                f"**`{tbl.get('table_name', 'gold')}`**", "",
                "| " + " | ".join(cols) + " |",
                "| " + " | ".join(["---"] * len(cols)) + " |",
            ]
            for row in rows[:5]:
                lines.append("| " + " | ".join(str(v) if v is not None else "" for v in row) + " |")
            lines.append("")

    if gold_column_descriptions:
        lines += ["---", "### Gold Layer — Column Descriptions", ""]
        lines += ["| Column | Type | Description |", "|--------|------|-------------|"]
        for cd in gold_column_descriptions:
            lines.append(
                f"| `{cd.get('column', '')}` "
                f"| `{cd.get('type', '')}` "
                f"| {cd.get('description', '')} |"
            )
        lines.append("")

    return "\n".join(lines) if lines else "Analysis complete."


def _format_publish_result(publish_report: dict) -> str:
    """Post-approval bubble: published tables + actual BigQuery row data."""
    status      = publish_report.get("publish_status", "unknown")
    tables      = publish_report.get("published_tables", [])
    summary     = publish_report.get("summary", "")
    failed      = publish_report.get("failed_statements", [])
    executed    = publish_report.get("executed_statements", [])
    actual_data = publish_report.get("actual_table_data", {})

    if status not in ("published", "partial", "dry_run"):
        return f"**Publishing Failed**\n\n**Error:** {summary}"

    title_map = {
        "published": "Published Successfully",
        "partial": "Partially Published",
        "dry_run": "Dry Run Complete",
    }
    lines: list[str] = [f"**{title_map[status]}**", ""]

    def _detect_layer(fqn: str) -> str:
        """Detect layer from the dataset part of a BQ FQN (project.dataset.table)."""
        low = fqn.lower()
        if ".gold." in low or low.endswith(".gold"):
            return "gold"
        if ".silver." in low or low.endswith(".silver"):
            return "silver"
        if ".bronze." in low or low.endswith(".bronze"):
            return "bronze"
        # Fallback: old convention of _gold/_silver in table name
        if "_gold" in low:
            return "gold"
        return "silver"

    layer_order = {"bronze": 0, "silver": 1, "gold": 2}
    if tables:
        sorted_tables = sorted(tables, key=lambda t: layer_order.get(_detect_layer(t), 1))
        for t in sorted_tables:
            layer = _detect_layer(t)
            lines.append(f"- **`{t}`** — {layer} layer")
        lines.append("")

    lines += [
        f"**{summary}**",
        f"Executed: {len(executed)} statements | Skipped (already exist): {len(failed)}",
        "",
    ]

    if actual_data:
        lines += ["---", "### Table Data (from BigQuery)", ""]
        bronze_keys = sorted(k for k in actual_data if _detect_layer(k) == "bronze")
        silver_keys = sorted(k for k in actual_data if _detect_layer(k) == "silver")
        gold_keys   = sorted(k for k in actual_data if _detect_layer(k) == "gold")

        for table_key in bronze_keys + silver_keys + gold_keys:
            layer = _detect_layer(table_key).capitalize()
            tdata = actual_data[table_key]
            cols  = tdata.get("columns", [])
            rows  = tdata.get("rows", [])

            if tdata.get("error"):
                lines += [f"#### `{table_key}` ({layer})", f"*Could not query: {tdata['error']}*", ""]
                continue
            if not rows:
                lines += [f"#### `{table_key}` ({layer})", "*No rows returned.*", ""]
                continue

            lines += [
                f"#### `{table_key}` ({layer} — {len(rows)} rows)",
                "",
                "| " + " | ".join(cols) + " |",
                "| " + " | ".join(["---"] * len(cols)) + " |",
            ]
            for row in rows:
                lines.append("| " + " | ".join(str(v) if v is not None else "" for v in row) + " |")
            lines.append("")

    bq_url = _bigquery_console_url()
    if bq_url:
        lines += ["---", f"Tables are now live. [Open BigQuery Console →]({bq_url})"]
    else:
        lines += ["---", "Tables are now live in **BigQuery**."]
    return "\n".join(lines)


# ── Pipeline runner ───────────────────────────────────────────────────────────

async def _run_generate_and_test(spec: dict, session_id: str, user_feedback: str | None = None) -> dict:
    """
    Run Pipeline Generator + Test Agent loop (MAF sessions for both).
    Returns generated_code + test_report on success.

    `user_feedback` (optional): free-text correction from the user after seeing
    a previous test result they were not satisfied with. Forwarded as
    `feedback.user_correction` to the pipeline-generator on the first attempt
    of this run so the agent can apply the requested change.
    """
    # ── BigQuery project alignment ─────────────────────────────────────────────
    # Inject the runtime BQ project into the spec so pipeline-generator and
    # test-agent agree on the target project/datasets without re-reading env vars.
    bq_project = (
        os.environ.get("BQ_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT") or ""
    ).strip()
    if bq_project:
        spec["bq_project"] = bq_project
        spec["bq_location"] = os.environ.get("BQ_LOCATION", "us-central1")
        spec["bq_datasets"] = {"gold": "gold", "silver": "silver", "bronze": "bronze"}

    # Shared in-memory dict so both helpers can lazily create MAF sessions
    _sess: dict = {}

    generated: dict = {}
    test_report: dict = {}
    feedback: dict | None = (
        {"user_correction": user_feedback.strip(), "iteration": 0}
        if user_feedback and user_feedback.strip()
        else None
    )
    iterations_detail: list = []

    for i in range(1, MAX_TEST_ITERATIONS + 1):
        pg_session = _get_or_create_adk_session(_sess, "pipeline-generator", session_id)
        generated  = await pipeline_generator.run(spec, feedback, session=pg_session)
        if "error" in generated or "raw_output" in generated:
            return {
                "status": "failed",
                "error": f"Pipeline Generator failed (iteration {i}): "
                         + generated.get("error", generated.get("raw_output", "unknown")),
                "iterations": i,
            }

        code = generated.get("generated_code", "")
        if not code:
            return {"status": "failed", "error": f"No SQL code generated on iteration {i}.", "iterations": i}

        ta_session  = _get_or_create_adk_session(_sess, "test-agent", session_id)
        test_report = await test_agent.run(code, spec, session=ta_session)
        _save_output_json(f"test_agent_output_{session_id[:8]}.json", test_report)
        # Surface the test verdict + failing checks to stdout (Cloud Run logs) —
        # the test_report JSON is otherwise only in the ephemeral container FS.
        print(f"[TEST AGENT] iter={i} session={session_id[:8]} "
              f"status={test_report.get('test_status')!r} "
              f"failures={[(f.get('check') or f.get('detail') or f.get('message') or '?') for f in (test_report.get('failures') or [])]}")
        if "raw_output" in test_report:
            print(f"[TEST AGENT] UNPARSED raw_output[:600]={test_report['raw_output'][:600]!r}")
        if "error" in test_report:
            return {
                "status": "failed",
                "error": f"Test Agent failed (iteration {i}): "
                         + test_report.get("error", test_report.get("raw_output", "unknown")),
                "iterations": i,
            }

        if test_agent.is_passing(test_report):
            iterations_detail.append({"iteration": i, "status": "passed"})
            return {
                "status": "success",
                "generated_code": code,
                "test_report": test_report,
                "iterations": i,
                "iterations_detail": iterations_detail,
            }

        failures = test_agent.get_failures(test_report)
        iterations_detail.append({
            "iteration": i,
            "status": "failed",
            "fixed": [f.get("detail", f.get("check", "")) for f in failures[:3]],
        })
        feedback = {"failures": failures, "summary": test_report.get("summary", ""), "iteration": i}

    return {
        "status": "failed",
        "error": f"Code did not pass validation after {MAX_TEST_ITERATIONS} attempts.",
        "test_report": test_report,
        "last_failures": test_agent.get_failures(test_report),
        "iterations": MAX_TEST_ITERATIONS,
    }


def _format_test_failures(pg_result: dict) -> str:
    """
    Surface the actual test-agent failures from a failed _run_generate_and_test
    result so the user can see specifically what the SQL got wrong instead of
    just 'did not pass validation after N attempts'.
    """
    iterations = pg_result.get("iterations", 0)
    failures = pg_result.get("last_failures", []) or []
    err = pg_result.get("error", "Pipeline build failed.")

    lines = [
        f"⚠️ **Pipeline build failed.** {err}",
        "",
    ]

    if failures:
        lines.append(f"**Last-iteration failures ({len(failures)}):**\n")
        for i, f in enumerate(failures[:10], 1):
            check = f.get("check", "unknown_check")
            severity = (f.get("severity") or "MEDIUM").upper()
            detail = f.get("detail", "(no detail)")
            suggestion = f.get("suggestion", "")
            lines.append(f"{i}. **{check}** ({severity})")
            lines.append(f"   - {detail}")
            if suggestion:
                lines.append(f"   - Suggested fix: {suggestion}")
            lines.append("")
        if len(failures) > 10:
            lines.append(f"_…and {len(failures) - 10} more failures._")
    else:
        lines.append("_No detailed failure list available — check the backend logs._")

    lines.append("")
    lines.append(
        f"Tried {iterations} regenerate-and-retest iteration(s). "
        f"Click **Regenerate** to start a fresh SQL generation pass, or **Cancel** to stop."
    )
    return "\n".join(lines)


def _build_test_messages(session_id: str, result: dict) -> list[dict]:
    """Gate 1: PG + Test + human gate (Proceed / Regenerate)."""
    generated_code    = result["generated_code"]
    iterations        = result["iterations"]
    iterations_detail = result.get("iterations_detail", [])

    pg_text   = _format_pg_message(generated_code, iterations, iterations_detail)
    test_text = _format_test_message(result["test_report"])
    gate_text = (
        "The SQL pipeline has passed all validation checks. "
        "Reply **Proceed** to run publisher analysis, "
        "or **Regenerate** to produce a new pipeline."
    )

    sql_file_id = _save_file(
        f"pipeline-{session_id[:8]}.sql",
        generated_code,
        "text/plain",
    )

    return [
        {
            "agent": "Pipeline Generator",
            "step": 1,
            "text": pg_text,
            "chips": [],
            "files": [{"id": sql_file_id, "name": f"pipeline-{session_id[:8]}.sql", "label": "Pipeline SQL"}],
        },
        {
            "agent": "Test Agent",
            "step": 2,
            "text": test_text,
            "chips": [],
            "files": [],
        },
        {
            "agent": "Data Product Assistant",
            "step": 3,
            "text": gate_text,
            "chips": ["Proceed", "Regenerate"],
            "files": [],
        },
    ]


def _build_publisher_messages(session_id: str, analysis: dict, test_report: dict) -> list[dict]:
    """Gate 2: Publisher analysis + final Approve / Cancel prompt."""
    gold_output_tables = test_report.get("sample_query_result", {}).get("output_tables", [])
    pub_text = _format_publisher_message(
        analysis.get("transformations", []),
        analysis.get("joins", []),
        analysis.get("aggregations", []),
        gold_output_tables,
        analysis.get("gold_column_descriptions", []),
    )
    approval_text = (
        "Everything looks good. Reply **Approve** to write these tables to BigQuery, "
        "or **Cancel** to abort without writing anything."
    )

    analysis_file_id = _save_file(
        f"analysis-{session_id[:8]}.json",
        json.dumps({
            "transformations": analysis.get("transformations", []),
            "joins": analysis.get("joins", []),
            "aggregations": analysis.get("aggregations", []),
        }, indent=2),
        "application/json",
    )

    return [
        {
            "agent": "Publisher Agent",
            "step": 1,
            "text": pub_text,
            "chips": [],
            "files": [{"id": analysis_file_id, "name": f"analysis-{session_id[:8]}.json", "label": "Pipeline Analysis"}],
        },
        {
            "agent": "Data Product Assistant",
            "step": 2,
            "text": approval_text,
            "chips": ["Approve", "Cancel"],
            "files": [],
        },
    ]


# ── Orchestrator relay helper ─────────────────────────────────────────────────

async def _relay(
    session_id: str,
    session: dict,
    sub_agent: str,
    sub_agent_result: dict,
    chips: list[str],
    dpi_step: str,
    fallback: str = "",
) -> str:
    """
    Pass a sub-agent result through the orchestrator so ALL user-facing text
    exits through a single gateway.

    We pass the human-readable content directly as the message — not buried in
    JSON context — so the orchestrator can't go off-script and generate its own
    narrative about the pipeline state.
    Falls back to `fallback` if the orchestrator call fails.
    """
    # Pull the human-readable part out of the result
    display = (
        sub_agent_result.get("display_output")
        or sub_agent_result.get("raw_output")
        or fallback
    )

    # The orchestrator only needs to know that chips are present so it doesn't
    # invent a "next steps" closing line. It MUST NOT mention the chip labels
    # in its reply — the frontend already renders the chips as clickable
    # buttons below the message, so naming them in prose is redundant noise.
    chip_hint = (
        f"\n\n(Note for orchestrator: {len(chips)} chip button(s) will be "
        f"rendered by the frontend. Do NOT mention them in your reply.)"
        if chips else ""
    )

    # Give the orchestrator the rendered content directly, not raw JSON
    message = (
        f"Present the following output from the '{sub_agent}' agent to the user. "
        f"Do NOT add commentary about pipeline state or suggest anything is stalled. "
        f"Present the content as-is. Do NOT add an 'Available actions' line, "
        f"a 'Next steps' section, or any prompt directing the user to a chip."
        f"{chip_hint}\n\n---\n\n{display}"
    )

    orch_maf = _get_or_create_adk_session(session, "orchestrator", session_id)
    try:
        orch_result = await orchestrator.run(
            message,
            context={"mode": "relay", "sub_agent": sub_agent, "dpi_step": dpi_step},
            session=orch_maf,
        )
        return orch_result.get("reply") or display
    except Exception:
        return display


# ── DDI chat handler — standalone entry via "Help me design it" card ────────

# Chip labels for the staged DDI gates. Kept as constants so the frontend can
# match against them exactly.
_DDI_CHIP_DESIGN_OK   = "Design looks right - generate STTM"
_DDI_CHIP_ADJUST_ER   = "Adjust the model"
_DDI_CHIP_STTM_OK     = "STTM looks correct - lock it"
_DDI_CHIP_TWEAK_STTM  = "Tweak the mapping"
_DDI_CHIP_PROCEED_PG  = "Proceed to Pipeline"
_DDI_CHIP_CANCEL      = "Cancel"


def _format_er_summary(gold_er: dict) -> str:
    """Compact markdown summary of the Gold ER for the chat bubble."""
    tables = gold_er.get("tables") or []
    fact = next((t for t in tables if t.get("type") == "fact"), {})
    dims = [t for t in tables if t.get("type") == "dimension"]
    mermaid = (gold_er.get("mermaid") or "").strip()

    lines = [
        "**Gold ER — Build 1 · Data Designer**",
        "",
        f"- Use case: **{gold_er.get('use_case', '(unknown)')}**",
        f"- Domain: **{gold_er.get('domain', '(unknown)')}**",
        f"- Fact table: `{fact.get('name', '(none)')}` "
        + (f"_(grain: {fact['grain']})_" if fact.get("grain") else ""),
        f"- Dimensions: " + (", ".join(f"`{d['name']}`" for d in dims) or "(none)"),
    ]
    if mermaid:
        lines += ["", "**Gold-layer ER diagram:**", "", "```mermaid", mermaid, "```"]
    lines += [
        "",
        "Review the ER above. Click **Design looks right - generate STTM** to "
        "produce the Silver → Gold STTM, or **Adjust the model** to refine it.",
    ]
    return "\n".join(lines)


def _format_ddi_summary(blueprint: dict) -> str:
    """Legacy human-readable summary of a fully-assembled DDI run."""
    gf = blueprint.get("gold_final") or {}
    tables = gf.get("tables") or []
    fact = next((t for t in tables if t.get("type") == "fact"), {})
    dims = [t for t in tables if t.get("type") == "dimension"]
    mappings = blueprint.get("silver_sttm", {}).get("mappings") or []
    validation_status = (gf.get("validation") or {}).get("status", "unknown")
    mermaid = (gf.get("mermaid") or "").strip()

    lines = [
        "**DDI complete — gold layer designed.**",
        "",
        f"- Use case: **{gf.get('use_case', '(unknown)')}**",
        f"- Domain: **{gf.get('domain', '(unknown)')}**",
        f"- Fact table: `{fact.get('name', '(none)')}` "
        + (f"_(grain: {fact['grain']})_" if fact.get("grain") else ""),
        f"- Dimensions: " + (", ".join(f"`{d['name']}`" for d in dims) or "(none)"),
        f"- STTM mappings: **{len(mappings)}**",
        f"- Validation: **{validation_status}**",
    ]
    if mermaid:
        lines += ["", "**Gold-layer ER diagram:**", "", "```mermaid", mermaid, "```"]
    lines += [
        "",
        "I've updated `data/utility_catalog.json` with the new gold catalog and "
        "prepared a pipeline spec. Reply **Proceed** to generate the SQL pipeline "
        "(DPB), or **Cancel** to stop here.",
    ]
    return "\n".join(lines)


def _build_gold_sttm_view(gold_sttm: dict, gold_er: dict) -> dict:
    """Shape the Silver→Gold STTM for the frontend STTM card."""
    mappings = gold_sttm.get("mappings") or []
    silver_sources, gold_targets = set(), set()
    derived_count = 0
    for m in mappings:
        src = (m.get("source") or "").split(".")
        if len(src) >= 2:
            silver_sources.add(".".join(src[-2:-1]) if len(src) >= 2 else src[0])
        tgt_table = (m.get("target") or "").split(".")[0]
        if tgt_table:
            gold_targets.add(tgt_table)
        if (m.get("kind") == "derived") and (m.get("source") in (None, "")):
            derived_count += 1
    # Fallback to gold ER tables when target column wasn't fully qualified.
    if not gold_targets:
        gold_targets = {t.get("name") for t in (gold_er.get("tables") or []) if t.get("name")}
    derived = [
        m for m in mappings
        if str(m.get("kind", "")).lower() == "derived" and not m.get("source")
    ]
    return {
        "title":         "Gold STTM Generator",
        "step_label":    "Step 2 of 5 · Build 2 · Data Designer",
        "summary":       (
            f"Source-to-Target Mapping generated · {len(mappings)} column-level "
            f"mappings · {len(derived)} derived measure · 0 unresolved sources."
        ),
        "silver_sources": sorted(silver_sources) or sorted({
            (m.get("source") or "").split(".")[-2]
            for m in mappings
            if m.get("source") and "." in (m.get("source") or "")
        }),
        "gold_targets":  sorted(gold_targets),
        "derived":       [
            {"target": m.get("target"), "transform": m.get("transform"), "notes": m.get("notes")}
            for m in derived
        ],
        "header":        "STTM · Silver → Gold",
        "mappings":      [
            {
                "source_column": m.get("source") or "(derived)",
                "transform":     m.get("transform") or "",
                "target_column": (m.get("target") or "").split(".")[-1],
                "target_table":  (m.get("target") or "").split(".")[0],
            }
            for m in mappings
        ],
        "mapping_count": len(mappings),
    }


def _build_silver_transform_view(silver_transform: dict) -> dict:
    """Shape the Bronze→Silver transformation output for the frontend card."""
    mappings = silver_transform.get("mappings") or []
    dq_rules = silver_transform.get("dq_rules") or []
    return {
        "title":         "Silver Transformation Agent",
        "step_label":    "Step 3 of 5 · Build 2 · Data Designer",
        "narrative":     silver_transform.get("narrative", ""),
        "lineage_summary": silver_transform.get("lineage_summary") or [],
        "silver_tables": silver_transform.get("silver_tables") or [],
        "header":        "STTM · Silver tables (already conformed)",
        "mappings":      [
            {
                "source_column": m.get("source") or "",
                "transform":     m.get("transform") or m.get("notes") or "",
                "target_column": m.get("target") or "",
                "target_table":  m.get("target_table") or "",
                "notes":         m.get("notes") or "",
            }
            for m in mappings
        ],
        "mapping_count": len(mappings),
        "dq_rules":      [
            {
                "table":          r.get("table") or "",
                "column":         r.get("column") or "",
                "check":          r.get("check") or "",
                "action_on_fail": r.get("action_on_fail") or "REJECT_ROW",
                "note":           r.get("note") or "",
            }
            for r in dq_rules
        ],
        "dq_rule_count": len(dq_rules),
    }


def _build_gold_data_contract_view(
    gold_final: dict,
    discovery_input: dict | None = None,
    silver_transform: dict | None = None,
) -> dict | None:
    """
    Shape the gold-final `data_catalog` into an OpenDataContract-style view for
    the frontend DataContractCard (design ported from the SILVER product).

    Returns None when there is no gold-layer catalog to render, so the caller
    can simply skip attaching `data_contract_view` to the message.
    """
    if not isinstance(gold_final, dict):
        return None
    catalog = gold_final.get("data_catalog") or {}
    layers = catalog.get("layers") or {}
    gold_entries = [e for e in (layers.get("gold") or []) if isinstance(e, dict)]
    if not gold_entries:
        return None

    discovery_input = discovery_input or {}
    domain = (
        gold_final.get("domain")
        or discovery_input.get("domain")
        or "gold"
    )
    use_case = (
        gold_final.get("use_case")
        or discovery_input.get("use_case_name")
        or f"{str(domain).replace('_', ' ').title()} Gold Layer"
    )

    # ── Models & fields (one entry per gold table) ────────────────────────────
    models: dict = {}
    for entry in gold_entries:
        full_name = entry.get("full_name") or entry.get("table_name") or ""
        tname = (
            entry.get("table_name")
            or (full_name.split(".")[-1] if full_name else "gold_table")
        )
        fields: dict = {}
        for c in entry.get("columns") or []:
            if not isinstance(c, dict):
                continue
            cname = c.get("name") or ""
            if not cname:
                continue
            fentry: dict = {
                "type":        str(c.get("data_type") or c.get("type") or "STRING").lower(),
                "description": c.get("description", ""),
                "nullable":    bool(c.get("nullable", True)),
            }
            if c.get("is_pk") or c.get("primary_key"):
                fentry["primary"] = True
                fentry["nullable"] = False
            fk = c.get("fk_ref") or c.get("references") or c.get("fk_to")
            if fk:
                fentry["references"] = fk
            tags = c.get("tags") if isinstance(c.get("tags"), dict) else {}
            is_pii = bool(c.get("pii")) or bool(tags.get("pii")) or \
                str(tags.get("classification", "")).upper() == "PII"
            if is_pii:
                fentry["pii"] = True
                fentry["classification"] = "PII"
            fields[cname] = fentry
        models[tname] = {
            "description":  entry.get("description", f"Gold conformed entity {tname}"),
            "type":         "table",
            "physicalName": full_name or tname,
            "fields":       fields,
        }

    # ── Quality assertions (derive PK/FK rules; fold in silver dq_rules) ──────
    quality: list = []
    seen: set = set()

    def _add_rule(description: str, must_be: str) -> None:
        key = (description, must_be)
        if must_be and key not in seen:
            seen.add(key)
            quality.append({"type": "custom", "description": description, "mustBe": must_be})

    for entry in gold_entries:
        for c in entry.get("columns") or []:
            if not isinstance(c, dict):
                continue
            cname = c.get("name") or ""
            if not cname:
                continue
            if c.get("is_pk") or c.get("primary_key"):
                _add_rule(f"Primary key `{cname}` must be unique and non-null",
                          f"{cname} IS NOT NULL")
            fk = c.get("fk_ref") or c.get("references") or c.get("fk_to")
            if fk:
                _add_rule(f"`{cname}` must reference a valid {fk} key",
                          f"{cname} IN (SELECT key FROM {fk})")
    for r in (silver_transform or {}).get("dq_rules") or []:
        if isinstance(r, dict) and r.get("check"):
            col = r.get("column") or r.get("table") or ""
            desc = r.get("note") or (f"Quality rule on `{col}`" if col else "Data quality rule")
            _add_rule(desc, r.get("check"))
    quality = quality[:8]

    # ── Governance standards ──────────────────────────────────────────────────
    raw_standards = (
        discovery_input.get("regulatory_drivers")
        or discovery_input.get("data_standards")
        or catalog.get("standards")
        or []
    )
    if isinstance(raw_standards, str):
        raw_standards = [raw_standards]
    standards = [str(s) for s in raw_standards if s] or ["BigQuery Native", "Knowledge Catalog"]

    first = gold_entries[0]
    target_dataset = first.get("schema_name") or "gold"
    owner = first.get("owner") or f"{str(domain).replace('_', ' ').title()} Data Engineering"
    refresh = first.get("refresh_cadence") or "Daily (04:00 UTC)"

    contract = {
        "dataContractSpecification": "0.9.3",
        "id": f"urn:datacontract:gold:{domain}",
        "info": {
            "title":          f"{use_case} — Data Contract",
            "version":        "1.0.0",
            "status":         "ACTIVE",
            "description":    catalog.get("description")
                              or f"Official Gold Layer Data Contract for {use_case}.",
            "domain":         str(domain),
            "owner":          owner,
            "standards":      standards,
            "target_dataset": target_dataset,
        },
        "servicelevels": {
            "freshness":    {"cron": "0 4 * * *", "maxLag": "24h", "schedule": refresh},
            "availability": {"percentage": "99.9%"},
            "retention":    {"period": "7 years", "policy": "Compliant Archival"},
        },
        "models":  models,
        "quality": quality,
    }
    try:
        contract["yaml_text"] = yaml.dump(
            contract, sort_keys=False, default_flow_style=False, allow_unicode=True,
        )
    except Exception:
        contract["yaml_text"] = json.dumps(contract, indent=2, default=str)
    return contract


def _build_synthetic_blueprint(
    discovery_output: dict,
    gold_er: dict,
    gold_sttm: dict,
    gold_final: dict,
    silver_transform: dict,
) -> dict:
    """Bundle the staged DDI outputs into the same shape that
    `ddi_pipeline.run` would have returned, so existing downstream code
    (extract_pipeline_spec) keeps working unchanged."""
    return {
        "blueprint_id":      str(uuid.uuid4()),
        "pipeline":          "gold-er -> silver-sttm -> silver-transformation -> gold-final",
        "discovery_input":   discovery_output,
        "gold_er":           gold_er,
        "silver_sttm":       gold_sttm,
        "silver_transformation": silver_transform,
        "gold_final":        gold_final,
        "status":            "completed",
    }


def _is_cancel_msg(message: str) -> bool:
    return any(
        kw in message.strip().lower()
        for kw in {"cancel", "stop", "abort", "reject"}
    )


async def _handle_ddi_chat(
    session_id: str,
    session: dict,
    message: str,
    action: Optional[str] = None,
) -> dict:
    """
    Staged DDI flow (standalone "Help me design it" entry point):

      ddi_awaiting_json     → user pastes Discovery JSON → run gold-er
      ddi_review_er         → "Design looks right - generate STTM" runs
                              silver-sttm; "Adjust the model" + tweak_er action
                              re-runs gold-er with user feedback.
      ddi_review_gold_sttm  → "STTM looks correct - lock it" runs
                              silver-transformation + gold-final + writes
                              utility_catalog.json; "Tweak the mapping" +
                              tweak_sttm action re-runs silver-sttm.
      ddi_review_silver_xform→ "Proceed to Pipeline" hands off to DPB; "Cancel"
                              ends the session.
    """
    step    = session.get("step", "initial")
    history = list(session.get("conversation_history", []))
    history.append({"role": "user", "content": message})
    msg_lower = (message or "").strip().lower()
    print(f"[DDI] turn — session={session_id[:8]} step={step!r} "
          f"action={action!r} msg={message[:80]!r}")

    def _save_session(**overrides) -> dict:
        merged = {**session, "conversation_history": history, **overrides}
        _sessions[session_id] = merged
        return merged

    def _reply(reply_text: str, *, agent: str = "DDI", chips=None, files=None,
               extra_msg_fields=None, is_complete=False, append_history=True):
        if append_history:
            history.append({"role": "assistant", "content": reply_text})
        msg = {
            "agent":  agent,
            "step":   1,
            "text":   reply_text,
            "chips":  chips or [],
            "files":  files or [],
        }
        if extra_msg_fields:
            msg.update(extra_msg_fields)
        return {
            "session_id":  session_id,
            "messages":    [msg],
            "is_complete": is_complete,
            "text":        reply_text,
            "chips":       chips or [],
        }

    # ── Step 1: awaiting JSON → run gold-er ──────────────────────────────────
    if step in ("initial", "ddi_awaiting_json"):
        try:
            payload = json.loads(message)
        except (json.JSONDecodeError, ValueError):
            prompt = (
                "I need a **valid JSON** that looks like the Discovery agent's "
                "final output (must contain at least one of `gold_matches`, "
                "`silver_matches`, `bronze_matches`). Paste it as a single message."
            )
            _save_session(step="ddi_awaiting_json")
            return _reply(prompt)

        err = ddi_pipeline.validate_discovery_payload(payload)
        if err:
            _save_session(step="ddi_awaiting_json")
            return _reply(err)

        ctx = {"session_id": session_id}
        gold_er = await gold_layer_agent.build_er(payload, context=ctx)
        if "error" in gold_er or not gold_layer_agent.is_er_complete(gold_er):
            err_text = f"**Gold ER step failed.**\n\n{gold_er.get('error', 'Incomplete ER output.')}"
            _save_session(step="ddi_awaiting_json")
            return _reply(err_text)

        reply = _format_er_summary(gold_er)
        _save_session(
            step="ddi_review_er",
            discovery_input=payload,
            gold_er=gold_er,
        )
        return _reply(
            reply,
            chips=[_DDI_CHIP_DESIGN_OK, _DDI_CHIP_ADJUST_ER],
        )

    # ── Step 2: review ER → generate Gold STTM (or adjust ER) ────────────────
    if step == "ddi_review_er":
        ctx = {"session_id": session_id}
        discovery_input = session.get("discovery_input") or {}
        gold_er         = session.get("gold_er") or {}

        # Adjust the model → re-run gold-er with user feedback
        if action == "tweak_er" or _DDI_CHIP_ADJUST_ER.lower() in msg_lower:
            feedback = message.strip()
            if not feedback or feedback.lower() == _DDI_CHIP_ADJUST_ER.lower():
                prompt = (
                    "Tell me what you'd like to change about the ER and I'll "
                    "refine the design (e.g. \"split dim_campaign into dim_campaign + "
                    "dim_audience\", \"drop the year column on dim_date\")."
                )
                _save_session(step="ddi_review_er")
                return _reply(prompt, chips=[_DDI_CHIP_DESIGN_OK, _DDI_CHIP_ADJUST_ER])

            new_er = await gold_layer_agent.build_er(
                discovery_input, context=ctx, feedback=feedback, previous_er=gold_er,
            )
            if "error" in new_er or not gold_layer_agent.is_er_complete(new_er):
                err_text = f"**ER refinement failed.**\n\n{new_er.get('error', 'Incomplete ER output.')}"
                _save_session(step="ddi_review_er")
                return _reply(err_text, chips=[_DDI_CHIP_DESIGN_OK, _DDI_CHIP_ADJUST_ER])

            _save_session(step="ddi_review_er", gold_er=new_er)
            return _reply(
                _format_er_summary(new_er),
                chips=[_DDI_CHIP_DESIGN_OK, _DDI_CHIP_ADJUST_ER],
            )

        if _is_cancel_msg(message):
            _save_session(step="complete")
            return _reply(
                "Stopped. The ER has been generated but not persisted. Refresh to start a new session.",
                is_complete=True,
            )

        # Approve → run silver-sttm to produce the Gold STTM
        print(f"[DDI] running silver-sttm for session={session_id[:8]} ...")
        gold_sttm = await silver_layer_agent.build_sttm(
            discovery_input, gold_er, context=ctx,
        )
        print(f"[DDI] silver-sttm returned keys={sorted(gold_sttm.keys())[:12]} "
              f"is_complete={silver_layer_agent.is_complete(gold_sttm)}")
        if "error" in gold_sttm or not silver_layer_agent.is_complete(gold_sttm):
            detail = gold_sttm.get("error")
            if not detail and "raw_output" in gold_sttm:
                detail = (
                    "The Silver STTM agent did not return valid JSON. Raw output:\n\n"
                    f"```\n{(gold_sttm.get('raw_output') or '')[:1500]}\n```"
                )
            err_text = f"**Silver STTM step failed.**\n\n{detail or 'Incomplete STTM output.'}"
            _save_session(step="ddi_review_er")
            return _reply(err_text, chips=[_DDI_CHIP_DESIGN_OK, _DDI_CHIP_ADJUST_ER])

        gold_sttm_view = _build_gold_sttm_view(gold_sttm, gold_er)
        _save_session(
            step="ddi_review_gold_sttm",
            gold_sttm=gold_sttm,
            gold_sttm_view=gold_sttm_view,
        )
        intro = (
            f"**{gold_sttm_view['title']}**\n\n"
            f"_{gold_sttm_view['step_label']}_\n\n"
            f"{gold_sttm_view['summary']}"
        )
        return _reply(
            intro,
            agent="Gold STTM Generator",
            chips=[_DDI_CHIP_STTM_OK, _DDI_CHIP_TWEAK_STTM],
            extra_msg_fields={"sttm_view": gold_sttm_view},
        )

    # ── Step 3: review Gold STTM → lock it (or tweak it) ─────────────────────
    if step == "ddi_review_gold_sttm":
        ctx = {"session_id": session_id}
        discovery_input = session.get("discovery_input") or {}
        gold_er         = session.get("gold_er") or {}
        gold_sttm       = session.get("gold_sttm") or {}

        if action == "tweak_sttm" or _DDI_CHIP_TWEAK_STTM.lower() in msg_lower:
            feedback = message.strip()
            if not feedback or feedback.lower() == _DDI_CHIP_TWEAK_STTM.lower():
                prompt = (
                    "Tell me what to change in the STTM (e.g. \"rename ctr to "
                    "click_through_rate\", \"add a SUM measure for revenue\")."
                )
                _save_session(step="ddi_review_gold_sttm")
                return _reply(prompt, chips=[_DDI_CHIP_STTM_OK, _DDI_CHIP_TWEAK_STTM])

            new_sttm = await silver_layer_agent.build_sttm(
                discovery_input, gold_er, context=ctx,
                feedback=feedback, previous_sttm=gold_sttm,
            )
            if "error" in new_sttm or not silver_layer_agent.is_complete(new_sttm):
                err_text = f"**STTM refinement failed.**\n\n{new_sttm.get('error', 'Incomplete STTM output.')}"
                _save_session(step="ddi_review_gold_sttm")
                return _reply(err_text, chips=[_DDI_CHIP_STTM_OK, _DDI_CHIP_TWEAK_STTM])

            view = _build_gold_sttm_view(new_sttm, gold_er)
            _save_session(step="ddi_review_gold_sttm", gold_sttm=new_sttm, gold_sttm_view=view)
            intro = (
                f"**{view['title']} — refined**\n\n_{view['step_label']}_\n\n{view['summary']}"
            )
            return _reply(
                intro,
                agent="Gold STTM Generator",
                chips=[_DDI_CHIP_STTM_OK, _DDI_CHIP_TWEAK_STTM],
                extra_msg_fields={"sttm_view": view},
            )

        if _is_cancel_msg(message):
            _save_session(step="complete")
            return _reply(
                "Stopped. The STTM has been generated but not persisted. Refresh to start a new session.",
                is_complete=True,
            )

        # Lock it → silver-transformation + gold-final + utility_catalog write.
        # These two LLM calls are independent (neither consumes the other's
        # output — the blueprint is assembled from both afterward), so run them
        # concurrently. Sequentially they can exceed the Azure App Service ~230s
        # gateway timeout (surfaces in the browser as "Failed to fetch").
        silver_xform, gold_final = await asyncio.gather(
            silver_layer_agent.build_silver_transformation(
                discovery_input, gold_er, gold_sttm, context=ctx,
            ),
            gold_layer_agent.finalize(gold_er, gold_sttm, context=ctx),
        )
        if "error" in silver_xform or not silver_layer_agent.is_silver_transformation_complete(silver_xform):
            detail = silver_xform.get("error")
            if not detail and "raw_output" in silver_xform:
                detail = (
                    "The Silver Transformation agent did not return valid JSON. Raw output:\n\n"
                    f"```\n{(silver_xform.get('raw_output') or '')[:1500]}\n```"
                )
            err_text = (
                f"**Silver Transformation step failed.**\n\n"
                f"{detail or 'Incomplete silver-transformation output.'}"
            )
            _save_session(step="ddi_review_gold_sttm")
            return _reply(err_text, chips=[_DDI_CHIP_STTM_OK, _DDI_CHIP_TWEAK_STTM])

        if "error" in gold_final or not gold_layer_agent.is_final_complete(gold_final):
            detail = gold_final.get("error")
            if not detail and "raw_output" in gold_final:
                detail = (
                    "The Gold Final agent did not return valid JSON (likely a "
                    "truncated / token-limited response). Raw output:\n\n"
                    f"```\n{(gold_final.get('raw_output') or '')[:1500]}\n```"
                )
            err_text = (
                f"**Gold-final validation failed.**\n\n"
                f"{detail or 'Incomplete gold-final output.'}"
            )
            _save_session(step="ddi_review_gold_sttm")
            return _reply(err_text, chips=[_DDI_CHIP_STTM_OK, _DDI_CHIP_TWEAK_STTM])

        blueprint = _build_synthetic_blueprint(
            discovery_input, gold_er, gold_sttm, gold_final, silver_xform,
        )

        blueprint_file_id = _save_file(
            f"ddi-blueprint-{session_id[:8]}.json",
            json.dumps(blueprint, indent=2, ensure_ascii=False, default=str),
            "application/json",
        )
        catalog_file_id = _save_file(
            f"utility_catalog-{session_id[:8]}.json",
            json.dumps(
                {"data_catalog": (gold_final or {}).get("data_catalog") or {}},
                indent=2, ensure_ascii=False, default=str,
            ),
            "application/json",
        )

        spec = ddi_pipeline.extract_pipeline_spec(blueprint)
        view = _build_silver_transform_view(silver_xform)
        contract_view = _build_gold_data_contract_view(
            gold_final, discovery_input, silver_transform=silver_xform,
        )

        _save_session(
            step="ddi_review_silver_xform",
            silver_transformation=silver_xform,
            silver_transform_view=view,
            data_contract_view=contract_view,
            gold_final=gold_final,
            ddi_blueprint=blueprint,
            spec=spec,
        )

        intro_lines = [
            f"**{view['title']}** completed.",
            "",
            f"_{view['step_label']}_",
            "",
            view["narrative"],
        ]
        if view["lineage_summary"]:
            intro_lines += [""] + [f"- {b}" for b in view["lineage_summary"]]
        intro_lines += [
            "",
            "`data/utility_catalog.json` has been updated. Click **Proceed to Pipeline** "
            "to generate the SQL pipeline, or **Cancel** to stop here.",
        ]
        return _reply(
            "\n".join(intro_lines),
            agent="Silver Transformation Agent",
            chips=[_DDI_CHIP_PROCEED_PG, _DDI_CHIP_CANCEL],
            files=[
                {"id": blueprint_file_id, "name": f"ddi-blueprint-{session_id[:8]}.json", "label": "DDI Blueprint"},
                {"id": catalog_file_id,   "name": f"utility_catalog-{session_id[:8]}.json", "label": "Utility Catalog"},
            ],
            extra_msg_fields={"silver_transform_view": view, "data_contract_view": contract_view},
        )

    # ── Step 4: review Silver Transformation → proceed to DPB ────────────────
    if step in ("ddi_review_silver_xform", "ddi_complete"):
        if _is_cancel_msg(message):
            cancel_text = (
                "DDI cancelled — the gold catalog is still saved at "
                "`data/utility_catalog.json` and the blueprint is downloadable. "
                "Refresh to start a new session."
            )
            _save_session(step="complete")
            return _reply(cancel_text, is_complete=True)

        if any(kw in msg_lower for kw in _APPROVE_KEYWORDS) or _DDI_CHIP_PROCEED_PG.lower() in msg_lower:
            spec = session.get("spec") or {}
            if not spec:
                _save_session(step="ddi_awaiting_json")
                return _reply(
                    "DDI blueprint was lost from the session. Please re-paste the discovery JSON."
                )

            result = await _run_generate_and_test(spec, session_id)
            if result["status"] == "failed":
                err_text = f"**Pipeline failed.**\n\n{result.get('error', 'Pipeline failed.')}"
                _save_session(step="ddi_review_silver_xform")
                return _reply(err_text, agent="DPB", chips=["Retry"])

            messages = _build_test_messages(session_id, result)
            gate_text = messages[-1]["text"]
            history.append({"role": "assistant", "content": gate_text})
            _sessions[session_id] = {
                **session,
                "pipeline_type": "sql",
                "step": "awaiting_test_approval",
                "spec": spec,
                "pipeline_result": result,
                "conversation_history": history,
                "pipeline_state": {"status": "awaiting_test_approval"},
            }
            return {
                "session_id": session_id,
                "messages": messages,
                "is_complete": False,
                "text": gate_text,
                "chips": ["Proceed", "Regenerate"],
            }

        prompt = "Reply **Proceed to Pipeline** to build the SQL pipeline, or **Cancel** to stop."
        _save_session(step="ddi_review_silver_xform")
        return _reply(prompt, chips=[_DDI_CHIP_PROCEED_PG, _DDI_CHIP_CANCEL])

    # Fallback
    _save_session(step="initial")
    return _reply("Unknown DDI state — refresh and try again.")


# ── DPI chat handler (embedded DPB state machine for /chat) ──────────────────

async def _handle_dpi_chat(
    session_id: str,
    session: dict,
    message: str,
    action: Optional[str] = None,
    file_ref_id: Optional[str] = None,
) -> dict:
    """
    DPI/Design flow embedded in /chat.
    Mirrors dpb_chat_endpoint but stores state in _sessions (not _dpb_sessions)
    and uses dpi_* step names to avoid collision with the SQL steps.

    Steps: initial → dpi_clarifying* → [dpi_phase_b]? →
           dpi_confirm_req → dpi_confirm_cls → dpi_confirm_disc → dpi_complete
    """
    step = session.get("step", "initial")
    print(f"[DPI] turn — session={session_id[:8]} step={step!r} "
          f"action={action!r} msg={(message or '')[:80]!r}")
    reply = ""
    chips: list[str] = []
    files: list[dict] = []
    is_complete = False
    discovery_view: Optional[dict] = None
    requirement_snapshot: Optional[dict] = None  # set when showing confirmation summary
    glossary: Optional[dict] = None              # set alongside requirement_snapshot
    classification_view: Optional[dict] = None   # set when showing classification card
    challenger_view: Optional[dict] = None       # set when showing challenger review

    def _remap(s: str) -> str:
        return {"phase_b_clarifying": "dpi_phase_b", "confirm_requirement": "dpi_confirm_req"}.get(s, s)

    def _respond(sess_update: dict) -> dict:
        _sessions[session_id] = sess_update
        msg = {"agent": "Orchestrator", "step": 1, "text": reply, "chips": chips, "files": files}
        if discovery_view is not None:
            msg["discovery_view"] = discovery_view
        if requirement_snapshot is not None:
            msg["requirement_data"] = requirement_snapshot
        if glossary is not None:
            msg["glossary"] = glossary
        if classification_view is not None:
            msg["classification_view"] = classification_view
        if challenger_view is not None:
            msg["challenger_view"] = challenger_view
        out = {
            "session_id": session_id,
            "messages": [msg],
            "is_complete": is_complete,
            "text": reply,
            "chips": chips,
            "current_step": sess_update.get("step", "initial"),
        }
        if discovery_view is not None:
            out["discovery_view"] = discovery_view
        return out

    # ── Requirement understanding (initial + clarifying + phase_b) ─────────────

    if step in ("initial", "dpi_clarifying", "dpi_phase_b"):
        # ── File injection: "use_file" action replaces the message with extracted summary
        if action == "use_file" and file_ref_id:
            stored = _get_file(file_ref_id)
            if not stored or stored.get("media_type") != "application/x-file-extraction":
                raise HTTPException(status_code=404, detail="File reference not found or expired.")

            extraction = json.loads(stored["content"])
            preview = extraction.get("preview", {})
            _numeric = [
                c for c, t in zip(preview.get("columns", []), preview.get("col_types", []))
                if t == "number"
            ]

            # Structured file on the initial step → show metrics confirmation first
            if step == "initial" and _numeric:
                _primary, _secondary, _file_chips, _msg_text = _build_file_metrics_message(
                    extraction, message
                )
                _sessions[session_id] = {
                    **session,
                    "step": "dpi_file_metrics",
                    "pipeline_type": "dpi",
                    "_file_summary": extraction.get("summary", ""),
                    "_file_primary": _primary,
                    "_file_secondary": _secondary,
                    "_file_numeric_cols": _numeric,
                    "original_input": message or extraction.get("summary", ""),
                    "conversation_history": [{"role": "assistant", "content": _msg_text}],
                }
                return {
                    "session_id": session_id,
                    "messages": [
                        {"agent": "Requirements Agent", "step": 1, "text": _msg_text,
                         "chips": _file_chips, "files": []}
                    ],
                    "is_complete": False,
                    "text": _msg_text,
                    "chips": _file_chips,
                }

            # Word doc, file with no numeric columns, or mid-flow upload → replace message
            message = extraction.get("summary", message)

        # Build agent history & conversation
        if step == "initial":
            session["original_input"] = message
            ah: list[dict] = []
            conv = [{"role": "user", "content": message}]
            clarification_pass = 0
            extra_sess: dict = {}
        elif step == "dpi_clarifying":
            prev_q = _last_agent_question(session)
            ah = list(session.get("agent_history", []))
            if prev_q:
                ah.append({"agent_question": prev_q, "user_answer": message})
            conv = list(session.get("conversation_history", []))
            conv.append({"role": "user", "content": message})
            clarification_pass = session.get("clarification_pass", 0) + 1
            extra_sess = {}
        else:  # dpi_phase_b
            pb_q = session.get("phase_b_question", "")
            ah = list(session.get("agent_history", []))
            ah.append({"agent_question": pb_q or "(Phase B technical questions)", "user_answer": message})
            conv = list(session.get("conversation_history", []))
            conv.append({"role": "user", "content": message})
            clarification_pass = session.get("clarification_pass", 0) + 1
            extra_sess = {"phase_b_owed": [], "phase_b_question": ""}

        req_maf = _get_or_create_adk_session(session, "requirement-understanding", session_id)
        ctx = _build_agent_context(
            {**session, "agent_history": ah},
            extra={"prior_output": session["data"]} if step == "dpi_phase_b" else None,
        )
        result = await requirement_understanding.run(message, ctx, session=req_maf)
        _save_output_json(f"requirement_output_{session_id[:8]}.json", result)

        # ── DEBUG ─────────────────────────────────────────────────────────
        print("=" * 80)
        print(f"[DPI DEBUG] step={step}  message={message[:80]!r}")
        print(f"[DPI DEBUG] result keys: {list(result.keys())}")
        print(f"[DPI DEBUG] has raw_output: {'raw_output' in result}")
        print(f"[DPI DEBUG] has use_case_name: {'use_case_name' in result}")
        print(f"[DPI DEBUG] has data_points: {'data_points' in result}")
        print(f"[DPI DEBUG] has kpis: {'kpis' in result}")
        print(f"[DPI DEBUG] has error: {'error' in result}")
        print(f"[DPI DEBUG] is_complete (pre-coerce): {requirement_understanding.is_complete(result)}")
        if "raw_output" in result:
            print(f"[DPI DEBUG] raw_output preview: {result['raw_output'][:300]!r}")
        print("=" * 80)
        # ──────────────────────────────────────────────────────────────────

        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"])
        result = _coerce_req_complete(result)

        # Advance to the confirm gate as soon as the JSON shape is complete
        # (has the mandatory keys), REGARDLESS of the agent's handoff_ready flag.
        #
        # Why not also require handoff_ready? Because that created a hard
        # dead-end: when the model returned a complete shape but set
        # handoff_ready=false (one or two fields left as needs_clarification /
        # unknown_per_user — exactly what happens when a user doesn't answer a
        # question cleanly), we fell into the else-branch, emitted chips=[], and
        # relayed the JSON as if it were a clarification. The user then saw a
        # message with NO question, NO card, and NO chip — nothing to click and
        # nothing to answer. The only code that explains a block
        # (get_blocking_mandatories) lives behind THIS confirm card, so it was
        # unreachable in precisely the case it was built for.
        #
        # By gating on is_complete alone we always render the summary card with
        # "Yep, that reads right" / "Let me tweak this". If a mandatory is still
        # missing, the confirm handler below blocks with a clear message + an
        # Edit chip; the user can always tweak/fill and move on. Any Phase-B
        # optional that wasn't captured is enriching, not blocking, so cutting
        # it short here is the correct trade-off (progress over a perfect brief).
        # This matches the file-metrics path, which already gates on is_complete
        # alone, and the skill's own contract (Pass 2+ emits JSON regardless and
        # expects the server to warn about missing mandatories at this gate).
        #
        # The branch decision itself lives in the pure, unit-tested
        # requirements_gate.decide_next_step() so it can never silently regress.
        decision = decide_next_step(result, clarification_pass)
        if decision["action"] == ACTION_SHOW_CARD:
            # Requirement complete — domain scope check before showing summary
            _tier, _canonical, _note = classify_domain(result.get("domain", ""))
            if _tier == "red":
                reply = out_of_scope_message(result.get("domain", ""))
                chips = ["Edit"]
                su = {
                    **session,
                    "step": "dpi_clarifying",
                    "data": result,
                    "conversation_history": conv,
                    "agent_history": ah,
                    "clarification_pass": clarification_pass,
                    **extra_sess,
                }
            else:
                files.extend(_save_requirement_files(result, session_id))
                chips = ["Yep, that reads right", "Let me tweak this"]
                reply = await _relay(
                    session_id, session, "requirement-understanding", result, chips,
                    "dpi_confirm_req",
                    fallback=result.get("display_output", "Requirements gathered. Please confirm."),
                )
                if _tier == "amber":
                    reply = f"> ⚠ **Note:** {_note}\n\n{reply}"
                conv.append({"role": "assistant", "content": reply})
                requirement_snapshot = result
                nonlocal_dp = result.get("data_points") or result.get("kpis") or []
                glossary = _build_glossary(nonlocal_dp)
                su = {
                    **session,
                    "step": "dpi_confirm_req",
                    "data": result,
                    "conversation_history": conv,
                    "agent_history": ah,
                    "clarification_pass": clarification_pass,
                    **extra_sess,
                }
        else:
            # The shape isn't complete yet — the agent is legitimately asking a
            # question, and the text box is itself a way forward. But NEVER let
            # this become an endless loop: after a couple of rounds where the
            # user still hasn't produced a complete brief (e.g. they keep giving
            # vague or off-topic answers), surface an explicit escape hatch so
            # they can open the Edit form and fill the mandatory fields by hand.
            chips = []
            if decision["action"] == ACTION_CLARIFY_WITH_ESCAPE:
                chips = ["Let me tweak this"]
                requirement_snapshot = _partial_req_snapshot(result)
                _dp_for_gloss = (
                    requirement_snapshot.get("data_points")
                    or requirement_snapshot.get("kpis")
                    or []
                )
                glossary = _build_glossary(_dp_for_gloss)
                _escape_note = (
                    "\n\n> If it's easier, tap **Let me tweak this** and fill in the "
                    "details directly — I just need a use-case name, a business domain, "
                    "and the data points / attributes you care about."
                )
            else:
                _escape_note = ""
            reply = await _relay(
                session_id, session, "requirement-understanding", result, chips, "dpi_clarifying",
                fallback=result.get("raw_output", result.get("display_output", "")),
            )
            reply = f"{reply}{_escape_note}"
            conv.append({"role": "assistant", "content": reply})
            su = {
                **session,
                "step": "dpi_clarifying",
                "data": result,
                "conversation_history": conv,
                "agent_history": ah,
                "clarification_pass": clarification_pass,
                **extra_sess,
            }

        return _respond(su)

    # ── File metrics confirmation → run requirements agent with confirmed metrics ─

    elif step == "dpi_file_metrics":
        file_summary = session.get("_file_summary", "")
        primary = session.get("_file_primary", [])
        secondary = session.get("_file_secondary", [])
        numeric_cols = session.get("_file_numeric_cols", [])

        msg_lower = message.strip().lower()

        # Determine which metrics the user confirmed
        if any(kw in msg_lower for kw in ("enough", "those are", "that's", "yes", "fine", "good")):
            confirmed_metrics = primary
        elif "all" in msg_lower:
            confirmed_metrics = numeric_cols
        else:
            # Try to match mentioned column names; fall back to adding all secondary
            confirmed_metrics = list(primary)
            matched_extra = [col for col in secondary if col.lower() in msg_lower]
            confirmed_metrics += matched_extra if matched_extra else secondary

        enriched_msg = (
            f"{file_summary}\n\n"
            f"The user has confirmed they want the following metrics/KPIs: "
            f"{', '.join(confirmed_metrics)}."
        )

        ah_fm: list[dict] = []
        conv_fm = [{"role": "user", "content": message}]
        req_maf_fm = _get_or_create_adk_session(session, "requirement-understanding", session_id)
        ctx_fm = _build_agent_context({
            **session,
            "agent_history": ah_fm,
            "original_input": enriched_msg,
        })
        result = await requirement_understanding.run(enriched_msg, ctx_fm, session=req_maf_fm)
        _save_output_json(f"requirement_output_{session_id[:8]}.json", result)

        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"])
        result = _coerce_req_complete(result)

        if requirement_understanding.is_complete(result):
            _tier, _canonical, _note = classify_domain(result.get("domain", ""))
            if _tier == "red":
                reply = out_of_scope_message(result.get("domain", ""))
                chips = ["Edit"]
                return _respond({
                    **session, "step": "dpi_clarifying", "data": result,
                    "conversation_history": conv_fm, "agent_history": ah_fm,
                    "clarification_pass": 0,
                })

            files.extend(_save_requirement_files(result, session_id))
            chips = ["Yep, that reads right", "Let me tweak this"]
            reply = await _relay(
                session_id, session, "requirement-understanding", result, chips,
                "dpi_confirm_req",
                fallback=result.get("display_output", "Requirements gathered. Please confirm."),
            )
            if _tier == "amber":
                reply = f"> ⚠ **Note:** {_note}\n\n{reply}"
            requirement_snapshot = result
            nonlocal_dp = result.get("data_points") or result.get("kpis") or []
            glossary = _build_glossary(nonlocal_dp)
            conv_fm.append({"role": "assistant", "content": reply})
            return _respond({
                **session,
                "step": "dpi_confirm_req",
                "data": result,
                "conversation_history": conv_fm,
                "agent_history": ah_fm,
                "clarification_pass": 0,
            })
        else:
            chips = []
            reply = await _relay(
                session_id, session, "requirement-understanding", result, chips, "dpi_clarifying",
                fallback=result.get("raw_output", result.get("display_output", "")),
            )
            conv_fm.append({"role": "assistant", "content": reply})
            return _respond({
                **session,
                "step": "dpi_clarifying",
                "data": result,
                "conversation_history": conv_fm,
                "agent_history": ah_fm,
                "clarification_pass": 1,
            })

    # ── Confirm requirement → run classification ───────────────────────────────

    elif step == "dpi_confirm_req":
        act = (action or "").strip().lower()
        if not act:
            _msg_l = message.strip().lower()
            if _msg_l in ("yep, that reads right", "yep that reads right", "that reads right"):
                act = "confirm"
            elif _msg_l in ("let me tweak this", "let me tweak one", "tweak this", "tweak one", "let me tweak"):
                act = "reject"
            else:
                act = await _classify_gate_intent(message, "requirement_review", session_id, session)

        if act == "confirm":
            # Handoff gate — block if mandatory fields are missing (no bypass allowed).
            # Uses the same pure, unit-tested logic as the advance decision above.
            blocking = _gate_blocking_mandatories(session["data"])
            if blocking:
                blockers_str = ", ".join(blocking)
                reply = (
                    f"The requirement is missing essential information: **{blockers_str}**.\n\n"
                    f"Please fill in the missing fields before proceeding."
                )
                chips = ["Edit"]
                return _respond({**session, "step": "dpi_confirm_req"})

            # Orchestrator (Mode E — Dispatch) decides the next agent + payload.
            # Server.py only resolves the returned agent name to a function call.
            next_agent, dispatch_payload = await _dispatch_via_orchestrator(
                previous_agent="requirement-understanding",
                previous_output=session["data"],
                session_id=session_id,
                session=session,
                flow_track=session.get("flow_track", "full"),
                user_action="confirm",
            )
            # Fallback to the canonical payload if dispatch failed or missing fields
            confirmed = dispatch_payload if dispatch_payload else {**session["data"], "confirmed_by_user": True}
            if "confirmed_by_user" not in confirmed:
                confirmed["confirmed_by_user"] = True
            # next_agent should be "use-case-classification" per the chain; defend in depth
            if next_agent and next_agent != "use-case-classification":
                print(f"[DISPATCH WARN] unexpected next_agent {next_agent!r}; running use-case-classification anyway")
            cls_maf = _get_or_create_adk_session(session, "use-case-classification", session_id)
            cls_result = await use_case_classification.run(confirmed, {}, session=cls_maf)
            _save_output_json(f"classification_output_{session_id[:8]}.json", cls_result)
            if "error" in cls_result:
                raise HTTPException(status_code=500, detail=cls_result["error"])

            cls_file_id = _save_file(
                f"classification-result-{session_id[:8]}.json",
                json.dumps(cls_result, indent=2, ensure_ascii=False, default=str),
                "application/json",
            )
            files.append({
                "id": cls_file_id,
                "name": f"classification-result-{session_id[:8]}.json",
                "label": "Classification Result",
            })
            chips = ["Confirm", "Override"]
            reply = await _relay(
                session_id, session, "use-case-classification", cls_result, chips, "dpi_confirm_cls",
                fallback=cls_result.get("display_output", "Classification complete. Please confirm."),
            )
            classification_view = _format_classification_view(cls_result)
            return _respond({
                **session,
                "step": "dpi_confirm_cls",
                "data": cls_result,
                "requirement": confirmed,
                "handoff_override": False,
            })

        else:
            # Reject path — orchestrator dispatches a rerun of the SAME agent with the user's correction.
            ah = list(session.get("agent_history", []))
            ah.append({
                "agent_question": "User correction after summary review",
                "user_answer": message,
            })
            conv = list(session.get("conversation_history", []))
            conv.append({"role": "user", "content": message})

            # Ask orchestrator (Mode E — dispatch) what to do on reject.
            # Expected: next_agent = "requirement-understanding" with user_correction in payload.
            next_agent, dispatch_payload = await _dispatch_via_orchestrator(
                previous_agent="requirement-understanding",
                previous_output=session["data"],
                session_id=session_id,
                session=session,
                flow_track=session.get("flow_track", "full"),
                user_action="reject",
            )
            if next_agent and next_agent != "requirement-understanding":
                print(f"[DISPATCH WARN] reject expected rerun of requirement-understanding; got {next_agent!r}")

            req_maf = _get_or_create_adk_session(session, "requirement-understanding", session_id)
            ctx = _build_agent_context(
                {**session, "agent_history": ah},
                extra={
                    "prior_output": session["data"],
                    **(dispatch_payload or {}),  # orchestrator may add user_correction etc.
                },
            )
            result = await requirement_understanding.run(message, ctx, session=req_maf)
            if "error" in result:
                raise HTTPException(status_code=500, detail=result["error"])
            result = _coerce_req_complete(result)

            if requirement_understanding.is_complete(result):
                _tier, _canonical, _note = classify_domain(result.get("domain", ""))
                if _tier == "red":
                    reply = out_of_scope_message(result.get("domain", ""))
                    chips = ["Edit"]
                    return _respond({
                        **session,
                        "step": "dpi_clarifying",
                        "data": result,
                        "conversation_history": conv,
                        "agent_history": ah,
                    })
                files.extend(_save_requirement_files(result, session_id))
                chips = ["Yep, that reads right", "Let me tweak this"]
                reply = await _relay(
                    session_id, session, "requirement-understanding", result, chips,
                    "dpi_confirm_req",
                    fallback=result.get("display_output", "Requirement updated. Please confirm."),
                )
                if _tier == "amber":
                    reply = f"> ⚠ **Note:** {_note}\n\n{reply}"
                requirement_snapshot = result
                nonlocal_dp = result.get("data_points") or result.get("kpis") or []
                glossary = _build_glossary(nonlocal_dp)
                return _respond({
                    **session,
                    "step": "dpi_confirm_req",
                    "data": result,
                    "conversation_history": conv,
                    "agent_history": ah,
                })
            else:
                chips = []
                reply = await _relay(
                    session_id, session, "requirement-understanding", result, chips,
                    "dpi_clarifying",
                    fallback=result.get("raw_output", result.get("display_output", "")),
                )
                return _respond({
                    **session,
                    "step": "dpi_clarifying",
                    "data": result,
                    "conversation_history": conv,
                    "agent_history": ah,
                })

    
    
    # ── Confirm classification → run discovery ─────────────────────────────────

    elif step == "dpi_confirm_cls":
        act = (action or "").strip().lower()
        if not act:
            msg_lower = message.strip().lower()
            if any(kw in msg_lower for kw in _APPROVE_KEYWORDS):
                act = "confirm"
            else:
                act = await _classify_gate_intent(message, "classification_review", session_id, session)
                if act == "reject":
                    act = "override"

        override_applied: Optional[str] = None

        if act == "confirm":
            classification_data = session["data"]
        else:
            resolved_type = _resolve_use_case_type(message)
            if resolved_type is None:
                err_result = {"error": f"Unknown classification type: '{message.strip()}'",
                              "valid_types": list(VALID_USE_CASE_TYPES)}
                chips = ["Confirm", "Override"]
                reply = await _relay(
                    session_id, session, "use-case-classification", err_result, chips, step,
                    fallback=(
                        f"I couldn't match '{message.strip()}' to a valid type. "
                        f"Valid: {', '.join(f'`{t}`' for t in VALID_USE_CASE_TYPES)}. "
                        f"Or click **Confirm** to keep the current classification."
                    ),
                )
                _sessions[session_id] = session
                return {
                    "session_id": session_id,
                    "messages": [{"agent": "Orchestrator", "step": 1, "text": reply, "chips": chips, "files": []}],
                    "is_complete": False, "text": reply, "chips": chips,
                }

            # Reject path — dispatch through orchestrator to apply the override (rerun classification with user_message)
            next_agent, _dispatch_payload = await _dispatch_via_orchestrator(
                previous_agent="use-case-classification",
                previous_output=session["data"],
                session_id=session_id,
                session=session,
                flow_track=session.get("flow_track", "full"),
                user_action="reject",
            )
            if next_agent and next_agent != "use-case-classification":
                print(f"[DISPATCH WARN] reject expected rerun of use-case-classification; got {next_agent!r}")

            classification_data = use_case_classification.apply_override(session["data"], resolved_type)
            if "error" in classification_data:
                chips = ["Confirm", "Override"]
                reply = await _relay(
                    session_id, session, "use-case-classification", classification_data, chips, step,
                    fallback=f"Override failed: {classification_data['error']}. Try again or click **Confirm**.",
                )
                _sessions[session_id] = session
                return {
                    "session_id": session_id,
                    "messages": [{"agent": "Orchestrator", "step": 1, "text": reply, "chips": chips, "files": []}],
                    "is_complete": False, "text": reply, "chips": chips,
                }

            prev_t = session["data"].get("use_case_type", "unknown")
            override_applied = (
                f"kept as **{resolved_type}**" if prev_t == resolved_type
                else f"changed from **{prev_t}** to **{resolved_type}**"
            )

        # ── Analytics-only gate ───────────────────────────────────────────────
        # Only the Analytics path is wired end-to-end today. Reusing the
        # captured non-Analytics work would produce the wrong artefacts
        # (e.g. dimensional dashboards for a Data Science feature store), so
        # the accept path RESETS the session and asks for a fresh Analytics
        # requirement rather than overriding the current classification.
        if classification_data.get("use_case_type") != "analytics":
            type_label = _CLS_TYPE_LABELS.get(
                classification_data.get("use_case_type", ""),
                str(classification_data.get("use_case_type", "")).replace("_", " ").title(),
            )
            gate_text = (
                f"Your use case is classified as **{type_label}**. Full support "
                f"for non-Analytics types is coming soon. For now, we can only "
                f"take **Analytics** use cases (dashboards, KPI reports, BI "
                f"summaries) through the full Discovery and Design pipeline.\n\n"
                f"If you'd like, you can try a separate Analytics use case in "
                f"the meantime (a dashboard, KPI report, or weekly summary) and "
                f"we'll take that one end-to-end."
            )
            chips = ["Yes, start an Analytics use case", "No, end session"]
            _sessions[session_id] = {
                **session,
                "step": "dpi_non_analytics_gate",
                "data": classification_data,
                "requirement": session.get("requirement", {}),
            }
            return {
                "session_id": session_id,
                "messages": [{
                    "agent": "Orchestrator",
                    "step": 1,
                    "text": gate_text,
                    "chips": chips,
                    "files": [],
                }],
                "is_complete": False,
                "text": gate_text,
                "chips": chips,
                "current_step": "dpi_non_analytics_gate",
            }

        req_for_handoff = dict(session.get("requirement", {}))
        if "data_points" in req_for_handoff and "kpis" not in req_for_handoff:
            req_for_handoff["kpis"] = [
                {"kpi_name": dp.get("name", ""), "description": dp.get("description", ""), "is_derived": dp.get("is_derived")}
                for dp in (req_for_handoff.get("data_points") or []) if isinstance(dp, dict)
            ]

        # PDF for the classification the user just approved
        try:
            _attach_pdf(
                files,
                f"classification-summary-{session_id[:8]}.pdf",
                generate_classification_pdf(classification_data),
                "Classification Summary (PDF)",
            )
        except Exception as exc:
            print(f"[PDF] classification PDF failed: {exc!r}")

        combined = {"session_id": session_id, **req_for_handoff, **classification_data}
        # Orchestrator (Mode E — Dispatch) decides the next agent + payload.
        next_agent, dispatch_payload = await _dispatch_via_orchestrator(
            previous_agent="use-case-classification",
            previous_output=classification_data,
            session_id=session_id,
            session=session,
            flow_track=session.get("flow_track", "full"),
            user_action=act,
        )
        if dispatch_payload:
            # merge orchestrator-prepared payload over the canonical combined
            combined = {**combined, **dispatch_payload}
        if next_agent and next_agent != "discovery":
            print(f"[DISPATCH WARN] unexpected next_agent {next_agent!r}; running discovery anyway")
        disc_result = await asyncio.to_thread(discovery.run, combined, {})
        if os.environ.get("EXPECTED_TABLES_LLM", "0") == "1" and "error" not in disc_result:
            deriv_session = _get_or_create_adk_session(session, "kpi-derivation", session_id)
            await kpi_derivation.enrich(disc_result, session=deriv_session)
        _save_output_json(f"discovery_output_{session_id[:8]}.json", disc_result)

        chips = ["Confirm", "Skip"] if "error" in disc_result else ["Confirm", "Continue"]
        relay_result = dict(disc_result)
        if override_applied:
            relay_result["override_note"] = f"Classification {override_applied}."
        if "error" not in disc_result:
            relay_result["display_output"] = _format_discovery_output(disc_result)
            discovery_view = disc_result.get("discovery_view")
            _attach_domain_framework(disc_result, classification_data)

        reply = await _relay(
            session_id, session, "discovery", relay_result, chips, "dpi_confirm_disc",
            fallback=relay_result.get("display_output", str(disc_result.get("error", ""))),
        )
        return _respond({**session, "step": "dpi_confirm_disc", "data": disc_result,
                         "classification": classification_data})

    # ── Analytics-only gate: user just saw the "coming soon" prompt ───────────
    # Two outcomes:
    #   accept → reset the session to a clean DPI/initial state and ask the
    #            user to describe a fresh Analytics use case. The prior
    #            (non-Analytics) requirement is intentionally discarded — its
    #            data points / domain don't transfer cleanly to a dimensional
    #            schema, so a clean start is more honest.
    #   decline → end the session cleanly.

    elif step == "dpi_non_analytics_gate":
        msg_l = message.strip().lower()
        ACCEPT_KEYWORDS = (
            "yes", "start", "try", "continue", "proceed",
            "okay", "ok", "sure", "let's", "analytics",
        )
        DECLINE_KEYWORDS = (
            "no", "end", "stop", "cancel", "pause", "later", "not now",
        )

        accepted = any(kw in msg_l for kw in ACCEPT_KEYWORDS) and not any(
            kw in msg_l for kw in DECLINE_KEYWORDS
        )

        if accepted:
            # Fresh start — discard the captured non-Analytics requirement,
            # classification, history, and any cached data. Keep the DPI
            # pipeline_type so the user stays on this flow.
            prompt_text = (
                "Sounds good — what's your Analytics use case? Tell me what "
                "you'd like to track (a dashboard, KPI report, or weekly "
                "summary), the business domain it belongs to, and the data "
                "points or attributes you need."
            )
            _sessions[session_id] = {
                "pipeline_type": "dpi",
                "step": "initial",
                "flow_track": session.get("flow_track", "full"),
                "conversation_history": [],
                "agent_history": [],
            }
            return {
                "session_id": session_id,
                "messages": [{
                    "agent": "Orchestrator", "step": 1,
                    "text": prompt_text, "chips": [], "files": [],
                }],
                "is_complete": False,
                "text": prompt_text,
                "chips": [],
                "current_step": "initial",
            }

        # Decline path
        end_text = (
            "Understood. We'll stop here for now. "
            "**Refresh the page to start a new session** whenever you're ready, "
            "and we'll have support for other use case types soon."
        )
        _sessions[session_id] = {**session, "step": "dpi_complete"}
        return {
            "session_id": session_id,
            "messages": [{"agent": "Orchestrator", "step": 1, "text": end_text, "chips": [], "files": []}],
            "is_complete": True, "text": end_text, "chips": [],
        }

    # ── Gate 1: Confirm discovery → run Challenger review ─────────────────────
    # User confirms discovery → Challenger Agent evaluates DPI consistency,
    # then the user decides whether to proceed to design.

    elif step == "dpi_confirm_disc":
        act = (action or "").strip().lower()
        if not act:
            act = await _classify_gate_intent(message, "discovery_review", session_id, session)

        if act not in ("confirm", "continue", "skip"):
            chips = ["Confirm"]
            err_result = {"message": "typed feedback not supported at this stage, please use chips"}
            reply = await _relay(
                session_id, session, "discovery", err_result, chips, step,
                fallback="Click **Confirm** to continue, or refresh to start over.",
            )
            _sessions[session_id] = session
            return {
                "session_id": session_id,
                "messages": [{"agent": "Orchestrator", "step": 1, "text": reply, "chips": chips, "files": []}],
                "is_complete": False, "text": reply, "chips": chips,
            }

        # PDF for the discovery output the user just approved
        try:
            _attach_pdf(
                files,
                f"discovery-summary-{session_id[:8]}.pdf",
                generate_discovery_pdf(session.get("data", {})),
                "Discovery Summary (PDF)",
            )
        except Exception as exc:
            print(f"[PDF] discovery PDF failed: {exc!r}")

        # Run Challenger Agent automatically
        ch_maf = _get_or_create_adk_session(session, "challenger", session_id)
        ch_result = await challenger.run(
            discovery_output=session.get("data", {}),
            requirements=session.get("requirement", {}),
            classification=session.get("classification", {}),
            session=ch_maf,
        )
        _save_output_json(f"challenger_output_{session_id[:8]}.json", ch_result)

        if challenger.is_complete(ch_result):
            challenger_view = challenger.build_challenger_view(ch_result)
            # Narrative is the display_output after the JSON block (or fallback)
            ch_narrative = ch_result.get("display_output", ch_result.get("summary", "Challenger review complete."))
        else:
            # Challenger failed — build a minimal fallback view and proceed
            ch_narrative = ch_result.get("raw_output", ch_result.get("error", "Challenger review could not be completed."))
            challenger_view = {
                "verdict": "concerns",
                "checks": [],
                "summary": ch_narrative,
                "design_queue": {"curated": [], "enriched": []},
            }

        chips = ["Proceed to Design", "Let me think — I'll come back"]
        _sessions[session_id] = {
            **session,
            "step": "dpi_challenger_review",
            "challenger_result": ch_result,
        }
        # Single message: the agent's narrative (its own "Before we move on..."
        # opener) plus the structured review card. Previously this emitted a
        # separate hardcoded intro message whose opener duplicated the narrative.
        return {
            "session_id": session_id,
            "messages": [
                {
                    "agent": "Challenger Agent",
                    "step": 1,
                    "text": ch_narrative,
                    "challenger_view": challenger_view,
                    "chips": chips,
                    "files": files,
                },
            ],
            "is_complete": False,
            "text": ch_narrative,
            "chips": chips,
            "current_step": "dpi_challenger_review",
        }

    # ── Gate 1b: Challenger review → run Gold ER (design) ─────────────────────

    elif step == "dpi_challenger_review":
        msg_lower = message.strip().lower()
        # "Let me think" or any hold/pause phrasing → preserve session, tell user
        if any(kw in msg_lower for kw in ("think", "come back", "later", "pause", "hold", "wait", "no")):
            hold_text = (
                "No problem — your session is saved. Come back whenever you're ready "
                "and click **Proceed to Design** to continue to design."
            )
            chips = ["Proceed to Design"]
            reply = hold_text
            return _respond({**session, "step": "dpi_challenger_review"})

        # PDF for the challenger review the user just approved
        ch_view = challenger.build_challenger_view(session.get("challenger_result", {}))
        try:
            _attach_pdf(
                files,
                f"challenger-review-{session_id[:8]}.pdf",
                generate_challenger_pdf(ch_view),
                "Challenger Review (PDF)",
            )
        except Exception as exc:
            print(f"[PDF] challenger PDF failed: {exc!r}")

        # Proceed → skip the data-product summary and go straight to design.
        # The DDI design phase consumes the discovery output directly, so the
        # data-product card was a review-only screen that fed nothing forward.
        # Route the DPI→DDI handoff through the orchestrator, then run Gold ER.
        disc_payload = session.get("data") or {}
        next_agent, dispatch_payload = await _dispatch_via_orchestrator(
            previous_agent="discovery",
            previous_output=disc_payload,
            session_id=session_id,
            session=session,
            flow_track=session.get("flow_track", "full"),
            user_action="confirm",
        )
        # Orchestrator should route us into the DDI workstream (next_agent = "gold-er" or "data-product")
        if next_agent and next_agent not in {"gold-er", "data-product"}:
            print(f"[DISPATCH WARN] DPI→DDI expected gold-er/data-product; got {next_agent!r}")
        if dispatch_payload:
            disc_payload = {**disc_payload, **dispatch_payload}

        # Stage 1 (Gold ER) — run only build_er, defer silver-sttm to user gate
        gold_er = await gold_layer_agent.build_er(
            disc_payload, context={"session_id": session_id},
        )
        print(f"[gold-er] result keys={sorted(gold_er.keys())} "
              f"complete={gold_layer_agent.is_er_complete(gold_er)}")
        if "error" in gold_er or not gold_layer_agent.is_er_complete(gold_er):
            # Surface the real failure reason — don't hide raw_output behind a generic string.
            err = gold_er.get("error")
            if not err and "raw_output" in gold_er:
                preview = (gold_er.get("raw_output") or "")[:1200]
                err = (
                    "The Gold ER agent returned unparseable output (likely truncated JSON). "
                    f"Raw output preview:\n\n```\n{preview}\n```"
                )
            if not err:
                missing = [k for k in ("style", "tables", "relationships", "mermaid")
                           if k not in gold_er]
                err = f"Incomplete ER output — missing fields: {missing}. Keys returned: {sorted(gold_er.keys())}"
            print(f"[gold-er] FAILED session={session_id}: {err[:200]}")
            err_text = (
                f"**Gold ER design failed — please try again.**\n\n{err}\n\n"
                f"Your session and discovery results are preserved."
            )
            # Keep session alive at challenger_review so the user can retry
            # without losing their discovery work.
            _sessions[session_id] = {**session, "step": "dpi_challenger_review"}
            return {
                "session_id": session_id,
                "messages": [{"agent": "DDI", "step": 1, "text": err_text,
                              "chips": ["Proceed to Design"], "files": []}],
                "is_complete": False, "text": err_text, "chips": ["Proceed to Design"],
            }

        gate_text = _format_er_summary(gold_er)
        _sessions[session_id] = {
            **session,
            "step": "dpi_review_er",
            "discovery_input": disc_payload,
            "gold_er": gold_er,
        }
        return {
            "session_id": session_id,
            "messages": [{
                "agent": "Gold ER Builder",
                "step": 1,
                "text": gate_text,
                "chips": [_DDI_CHIP_DESIGN_OK, _DDI_CHIP_ADJUST_ER],
                "files": files,
            }],
            "is_complete": False,
            "text": gate_text,
            "chips": [_DDI_CHIP_DESIGN_OK, _DDI_CHIP_ADJUST_ER],
        }

    # ── Gate 2a: Review ER → generate Gold STTM (or adjust ER) ───────────────

    elif step == "dpi_review_er":
        ml = message.strip().lower()
        discovery_input = session.get("discovery_input") or session.get("data") or {}
        gold_er         = session.get("gold_er") or {}

        # Adjust the model → re-run gold-er with user feedback
        if action == "tweak_er" or _DDI_CHIP_ADJUST_ER.lower() in ml:
            feedback = message.strip()
            if not feedback or feedback.lower() == _DDI_CHIP_ADJUST_ER.lower():
                prompt = (
                    "Tell me what you'd like to change about the ER and I'll "
                    "refine the design (e.g. \"split dim_campaign into dim_campaign + "
                    "dim_audience\", \"drop the year column on dim_date\")."
                )
                _sessions[session_id] = {**session, "step": "dpi_review_er"}
                return {
                    "session_id": session_id,
                    "messages": [{
                        "agent": "Gold ER Builder", "step": 1, "text": prompt,
                        "chips": [_DDI_CHIP_DESIGN_OK, _DDI_CHIP_ADJUST_ER], "files": [],
                    }],
                    "is_complete": False, "text": prompt,
                    "chips": [_DDI_CHIP_DESIGN_OK, _DDI_CHIP_ADJUST_ER],
                }
            new_er = await gold_layer_agent.build_er(
                discovery_input, context={"session_id": session_id},
                feedback=feedback, previous_er=gold_er,
            )
            if "error" in new_er or not gold_layer_agent.is_er_complete(new_er):
                err_text = f"**ER refinement failed.**\n\n{new_er.get('error', 'Incomplete ER output.')}"
                _sessions[session_id] = {**session, "step": "dpi_review_er"}
                return {
                    "session_id": session_id,
                    "messages": [{"agent": "Gold ER Builder", "step": 1, "text": err_text,
                                  "chips": [_DDI_CHIP_DESIGN_OK, _DDI_CHIP_ADJUST_ER], "files": []}],
                    "is_complete": False, "text": err_text,
                    "chips": [_DDI_CHIP_DESIGN_OK, _DDI_CHIP_ADJUST_ER],
                }
            gate_text = _format_er_summary(new_er)
            _sessions[session_id] = {**session, "step": "dpi_review_er", "gold_er": new_er}
            return {
                "session_id": session_id,
                "messages": [{
                    "agent": "Gold ER Builder", "step": 1, "text": gate_text,
                    "chips": [_DDI_CHIP_DESIGN_OK, _DDI_CHIP_ADJUST_ER], "files": [],
                }],
                "is_complete": False, "text": gate_text,
                "chips": [_DDI_CHIP_DESIGN_OK, _DDI_CHIP_ADJUST_ER],
            }

        if any(kw in ml for kw in {"cancel", "stop", "abort"}):
            cancel_text = "Stopped. The ER has been generated but not persisted. Refresh to start a new session."
            _sessions[session_id] = {**session, "step": "dpi_complete"}
            return {
                "session_id": session_id,
                "messages": [{"agent": "DDI", "step": 1, "text": cancel_text, "chips": [], "files": []}],
                "is_complete": True, "text": cancel_text, "chips": [],
            }

        # Approve → run silver-sttm to produce the Gold STTM
        print(f"[DPI] running silver-sttm for session={session_id[:8]} ...")
        gold_sttm = await silver_layer_agent.build_sttm(
            discovery_input, gold_er, context={"session_id": session_id},
        )
        if "error" in gold_sttm or not silver_layer_agent.is_complete(gold_sttm):
            detail = gold_sttm.get("error")
            if not detail and "raw_output" in gold_sttm:
                detail = (
                    "The Silver STTM agent did not return valid JSON. Raw output:\n\n"
                    f"```\n{(gold_sttm.get('raw_output') or '')[:1500]}\n```"
                )
            err_text = f"**Silver STTM step failed.**\n\n{detail or 'Incomplete STTM output.'}"
            _sessions[session_id] = {**session, "step": "dpi_review_er"}
            return {
                "session_id": session_id,
                "messages": [{"agent": "Gold STTM Generator", "step": 1, "text": err_text,
                              "chips": [_DDI_CHIP_DESIGN_OK, _DDI_CHIP_ADJUST_ER], "files": []}],
                "is_complete": False, "text": err_text,
                "chips": [_DDI_CHIP_DESIGN_OK, _DDI_CHIP_ADJUST_ER],
            }

        view = _build_gold_sttm_view(gold_sttm, gold_er)
        _sessions[session_id] = {
            **session,
            "step": "dpi_review_gold_sttm",
            "discovery_input": discovery_input,
            "gold_er": gold_er,
            "gold_sttm": gold_sttm,
            "gold_sttm_view": view,
        }
        intro = (
            f"**{view['title']}**\n\n_{view['step_label']}_\n\n{view['summary']}"
        )
        return {
            "session_id": session_id,
            "messages": [{
                "agent": "Gold STTM Generator",
                "step": 1,
                "text": intro,
                "chips": [_DDI_CHIP_STTM_OK, _DDI_CHIP_TWEAK_STTM],
                "files": [],
                "sttm_view": view,
            }],
            "is_complete": False,
            "text": intro,
            "chips": [_DDI_CHIP_STTM_OK, _DDI_CHIP_TWEAK_STTM],
        }

    # ── Gate 2b: Review Gold STTM → lock it (or tweak it) ────────────────────

    elif step == "dpi_review_gold_sttm":
        ml = message.strip().lower()
        discovery_input = session.get("discovery_input") or session.get("data") or {}
        gold_er         = session.get("gold_er") or {}
        gold_sttm       = session.get("gold_sttm") or {}

        if action == "tweak_sttm" or _DDI_CHIP_TWEAK_STTM.lower() in ml:
            feedback = message.strip()
            if not feedback or feedback.lower() == _DDI_CHIP_TWEAK_STTM.lower():
                prompt = (
                    "Tell me what to change in the STTM (e.g. \"rename ctr to "
                    "click_through_rate\", \"add a SUM measure for revenue\")."
                )
                _sessions[session_id] = {**session, "step": "dpi_review_gold_sttm"}
                return {
                    "session_id": session_id,
                    "messages": [{
                        "agent": "Gold STTM Generator", "step": 1, "text": prompt,
                        "chips": [_DDI_CHIP_STTM_OK, _DDI_CHIP_TWEAK_STTM], "files": [],
                    }],
                    "is_complete": False, "text": prompt,
                    "chips": [_DDI_CHIP_STTM_OK, _DDI_CHIP_TWEAK_STTM],
                }
            new_sttm = await silver_layer_agent.build_sttm(
                discovery_input, gold_er, context={"session_id": session_id},
                feedback=feedback, previous_sttm=gold_sttm,
            )
            if "error" in new_sttm or not silver_layer_agent.is_complete(new_sttm):
                err_text = f"**STTM refinement failed.**\n\n{new_sttm.get('error', 'Incomplete STTM output.')}"
                _sessions[session_id] = {**session, "step": "dpi_review_gold_sttm"}
                return {
                    "session_id": session_id,
                    "messages": [{"agent": "Gold STTM Generator", "step": 1, "text": err_text,
                                  "chips": [_DDI_CHIP_STTM_OK, _DDI_CHIP_TWEAK_STTM], "files": []}],
                    "is_complete": False, "text": err_text,
                    "chips": [_DDI_CHIP_STTM_OK, _DDI_CHIP_TWEAK_STTM],
                }
            view = _build_gold_sttm_view(new_sttm, gold_er)
            _sessions[session_id] = {
                **session, "step": "dpi_review_gold_sttm",
                "gold_sttm": new_sttm, "gold_sttm_view": view,
            }
            intro = (
                f"**{view['title']} — refined**\n\n_{view['step_label']}_\n\n{view['summary']}"
            )
            return {
                "session_id": session_id,
                "messages": [{
                    "agent": "Gold STTM Generator", "step": 1, "text": intro,
                    "chips": [_DDI_CHIP_STTM_OK, _DDI_CHIP_TWEAK_STTM], "files": [],
                    "sttm_view": view,
                }],
                "is_complete": False, "text": intro,
                "chips": [_DDI_CHIP_STTM_OK, _DDI_CHIP_TWEAK_STTM],
            }

        if any(kw in ml for kw in {"cancel", "stop", "abort"}):
            cancel_text = "Stopped. The STTM has been generated but not persisted. Refresh to start a new session."
            _sessions[session_id] = {**session, "step": "dpi_complete"}
            return {
                "session_id": session_id,
                "messages": [{"agent": "DDI", "step": 1, "text": cancel_text, "chips": [], "files": []}],
                "is_complete": True, "text": cancel_text, "chips": [],
            }

        # Lock it → silver-transformation + gold-final + utility_catalog write.
        # Run the two independent LLM calls concurrently; sequentially they can
        # exceed the Azure App Service ~230s gateway timeout ("Failed to fetch").
        print(f"[DPI] running silver-transformation + gold-final (concurrently) for session={session_id[:8]} ...")
        silver_xform, gold_final = await asyncio.gather(
            silver_layer_agent.build_silver_transformation(
                discovery_input, gold_er, gold_sttm, context={"session_id": session_id},
            ),
            gold_layer_agent.finalize(
                gold_er, gold_sttm, context={"session_id": session_id},
            ),
        )
        if "error" in silver_xform or not silver_layer_agent.is_silver_transformation_complete(silver_xform):
            detail = silver_xform.get("error")
            if not detail and "raw_output" in silver_xform:
                detail = (
                    "The Silver Transformation agent did not return valid JSON. Raw output:\n\n"
                    f"```\n{(silver_xform.get('raw_output') or '')[:1500]}\n```"
                )
            err_text = f"**Silver Transformation step failed.**\n\n{detail or 'Incomplete silver-transformation output.'}"
            _sessions[session_id] = {**session, "step": "dpi_review_gold_sttm"}
            return {
                "session_id": session_id,
                "messages": [{"agent": "Silver Transformation Agent", "step": 1, "text": err_text,
                              "chips": [_DDI_CHIP_STTM_OK, _DDI_CHIP_TWEAK_STTM], "files": []}],
                "is_complete": False, "text": err_text,
                "chips": [_DDI_CHIP_STTM_OK, _DDI_CHIP_TWEAK_STTM],
            }

        if "error" in gold_final or not gold_layer_agent.is_final_complete(gold_final):
            detail = gold_final.get("error")
            if not detail and "raw_output" in gold_final:
                detail = (
                    "The Gold Final agent did not return valid JSON (likely a "
                    "truncated / token-limited response). Raw output:\n\n"
                    f"```\n{(gold_final.get('raw_output') or '')[:1500]}\n```"
                )
            err_text = (
                f"**Gold-final validation failed.**\n\n"
                f"{detail or 'Incomplete gold-final output.'}"
            )
            _sessions[session_id] = {**session, "step": "dpi_review_gold_sttm"}
            return {
                "session_id": session_id,
                "messages": [{"agent": "Silver Transformation Agent", "step": 1, "text": err_text,
                              "chips": [_DDI_CHIP_STTM_OK, _DDI_CHIP_TWEAK_STTM], "files": []}],
                "is_complete": False, "text": err_text,
                "chips": [_DDI_CHIP_STTM_OK, _DDI_CHIP_TWEAK_STTM],
            }

        blueprint = _build_synthetic_blueprint(
            discovery_input, gold_er, gold_sttm, gold_final, silver_xform,
        )
        _save_output_json(f"ddi_output_{session_id[:8]}.json", blueprint)

        ddi_blueprint_file_id = _save_file(
            f"ddi-blueprint-{session_id[:8]}.json",
            json.dumps(blueprint, indent=2, ensure_ascii=False, default=str),
            "application/json",
        )
        catalog_file_id = _save_file(
            f"utility_catalog-{session_id[:8]}.json",
            json.dumps(
                {"data_catalog": (gold_final or {}).get("data_catalog") or {}},
                indent=2, ensure_ascii=False, default=str,
            ),
            "application/json",
        )
        spec = ddi_pipeline.extract_pipeline_spec(blueprint)
        view = _build_silver_transform_view(silver_xform)
        df = _detect_and_load_domain_framework(discovery_input, silver_xform=silver_xform)
        if df:
            view["domain_framework"] = df

        contract_view = _build_gold_data_contract_view(
            gold_final, discovery_input, silver_transform=silver_xform,
        )

        _sessions[session_id] = {
            **session,
            "step": "dpi_review_silver_xform",
            "silver_transformation": silver_xform,
            "silver_transform_view": view,
            "data_contract_view": contract_view,
            "gold_final": gold_final,
            "ddi_blueprint": blueprint,
            "spec": spec,
        }

        intro_lines = [
            f"**{view['title']}** completed.",
            "",
            f"_{view['step_label']}_",
            "",
            view["narrative"],
        ]
        if view["lineage_summary"]:
            intro_lines += [""] + [f"- {b}" for b in view["lineage_summary"]]
        intro_lines += [
            "",
            "`data/utility_catalog.json` has been updated. Click **Proceed to Pipeline** "
            "to generate the SQL pipeline, or **Cancel** to stop here.",
        ]
        intro = "\n".join(intro_lines)
        return {
            "session_id": session_id,
            "messages": [{
                "agent": "Silver Transformation Agent",
                "step": 1,
                "text": intro,
                "chips": [_DDI_CHIP_PROCEED_PG, _DDI_CHIP_CANCEL],
                "files": [
                    {"id": ddi_blueprint_file_id, "name": f"ddi-blueprint-{session_id[:8]}.json", "label": "DDI Blueprint"},
                    {"id": catalog_file_id,       "name": f"utility_catalog-{session_id[:8]}.json", "label": "Utility Catalog"},
                ],
                "silver_transform_view": view,
                "data_contract_view": contract_view,
            }],
            "is_complete": False,
            "text": intro,
            "chips": [_DDI_CHIP_PROCEED_PG, _DDI_CHIP_CANCEL],
        }

    # ── Gate 3: Review Silver Transformation → run pipeline-generator ────────

    elif step in ("dpi_review_silver_xform", "dpi_review_ddi"):
        ml = message.strip().lower()
        if any(kw in ml for kw in {"cancel", "stop", "abort", "no"}):
            cancel_text = "Stopped. DDI artefacts are saved. Refresh to start a new session."
            _sessions[session_id] = {**session, "step": "dpi_complete"}
            return {
                "session_id": session_id,
                "messages": [{"agent": "Data Product Assistant", "step": 1, "text": cancel_text, "chips": [], "files": []}],
                "is_complete": True, "text": cancel_text, "chips": [],
            }

        # Proceed to Pipeline → run pipeline_generator only (no test loop yet)
        spec = session.get("spec") or {}
        if not spec:
            err_text = "Pipeline spec missing from session. Please restart the flow."
            return {
                "session_id": session_id,
                "messages": [{"agent": "Pipeline Generator", "step": 1, "text": err_text, "chips": ["Cancel"], "files": []}],
                "is_complete": False, "text": err_text, "chips": ["Cancel"],
            }

        pg_session = _get_or_create_adk_session(session, "pipeline-generator", session_id)
        pg_result = await pipeline_generator.run(spec, None, session=pg_session)
        _save_output_json(f"pipeline_generator_output_{session_id[:8]}.json", pg_result)
        if "error" in pg_result or "raw_output" in pg_result:
            err_text = (
                f"**Pipeline Generator failed.**\n\n"
                f"{pg_result.get('error', pg_result.get('raw_output', 'Unknown error.'))}"
            )
            _sessions[session_id] = {**session, "step": step}
            return {
                "session_id": session_id,
                "messages": [{"agent": "Pipeline Generator", "step": 1, "text": err_text, "chips": ["Retry", "Cancel"], "files": []}],
                "is_complete": False, "text": err_text, "chips": ["Retry", "Cancel"],
            }

        generated_code = pg_result.get("generated_code", "")
        sql_file_id = _save_file(f"pipeline-{session_id[:8]}.sql", generated_code, "text/plain")

        gate_text = (
            f"**Pipeline Generator — SQL produced.**\n\n"
            f"```sql\n{generated_code}\n```\n\n"
            f"Review the SQL above. Click **Proceed to Test** to run validation, "
            f"**Regenerate** to produce a new version, or **Cancel** to stop."
        )
        _sessions[session_id] = {
            **session,
            "step": "dpi_review_pg",
            "spec": spec,
            "generated_code": generated_code,
            "pg_first_result": pg_result,
        }
        return {
            "session_id": session_id,
            "messages": [{
                "agent": "Pipeline Generator",
                "step": 1,
                "text": gate_text,
                "chips": ["Proceed to Test", "Regenerate", "Cancel"],
                "files": [{"id": sql_file_id, "name": f"pipeline-{session_id[:8]}.sql", "label": "Pipeline SQL"}],
            }],
            "is_complete": False,
            "text": gate_text,
            "chips": ["Proceed to Test", "Regenerate", "Cancel"],
        }

    # ── Gate 4: Review pipeline SQL → run test loop on Proceed ─────────────────

    elif step == "dpi_review_pg":
        ml = message.strip().lower()
        if any(kw in ml for kw in {"cancel", "stop", "abort"}):
            cancel_text = "Stopped. Pipeline SQL is saved. Refresh to start a new session."
            _sessions[session_id] = {**session, "step": "dpi_complete"}
            return {
                "session_id": session_id,
                "messages": [{"agent": "Data Product Assistant", "step": 1, "text": cancel_text, "chips": [], "files": []}],
                "is_complete": True, "text": cancel_text, "chips": [],
            }

        # Regenerate → re-run pipeline_generator with no test feedback
        if "regenerate" in ml or "retry" in ml:
            spec = session.get("spec") or {}
            pg_session = _get_or_create_adk_session(session, "pipeline-generator", session_id)
            pg_result = await pipeline_generator.run(spec, None, session=pg_session)
            _save_output_json(f"pipeline_generator_output_{session_id[:8]}.json", pg_result)
            if "error" in pg_result or "raw_output" in pg_result:
                err_text = f"**Pipeline Generator regenerate failed.**\n\n{pg_result.get('error', pg_result.get('raw_output', '?'))}"
                return {
                    "session_id": session_id,
                    "messages": [{"agent": "Pipeline Generator", "step": 1, "text": err_text, "chips": ["Retry", "Cancel"], "files": []}],
                    "is_complete": False, "text": err_text, "chips": ["Retry", "Cancel"],
                }
            generated_code = pg_result.get("generated_code", "")
            sql_file_id = _save_file(f"pipeline-{session_id[:8]}.sql", generated_code, "text/plain")
            gate_text = (
                f"**Pipeline Generator — regenerated SQL.**\n\n```sql\n{generated_code}\n```\n\n"
                f"Click **Proceed to Test**, **Regenerate** again, or **Cancel**."
            )
            _sessions[session_id] = {**session, "step": "dpi_review_pg", "generated_code": generated_code, "pg_first_result": pg_result}
            return {
                "session_id": session_id,
                "messages": [{
                    "agent": "Pipeline Generator", "step": 1, "text": gate_text,
                    "chips": ["Proceed to Test", "Regenerate", "Cancel"],
                    "files": [{"id": sql_file_id, "name": f"pipeline-{session_id[:8]}.sql", "label": "Pipeline SQL"}],
                }],
                "is_complete": False, "text": gate_text, "chips": ["Proceed to Test", "Regenerate", "Cancel"],
            }

        # Proceed to Test → run the test loop (uses _run_generate_and_test which
        # regenerates on test-agent feedback up to MAX_TEST_ITERATIONS).
        spec = session.get("spec") or {}
        result = await _run_generate_and_test(spec, session_id)

        if result.get("status") == "failed":
            failure_text = _format_test_failures(result)
            _sessions[session_id] = {**session, "step": "dpi_review_pg"}
            return {
                "session_id": session_id,
                "messages": [{
                    "agent": "Test Agent", "step": 1, "text": failure_text,
                    "chips": ["Regenerate", "Cancel"], "files": [],
                }],
                "is_complete": False, "text": failure_text, "chips": ["Regenerate", "Cancel"],
            }

        # Test passed → transition into existing awaiting_test_approval gate
        test_messages = _build_test_messages(session_id, result)
        _sessions[session_id] = {
            **session,
            "pipeline_type": "sql",
            "step": "awaiting_test_approval",
            "spec": spec,
            "pipeline_result": result,
            "pipeline_state": {"status": "awaiting_test_approval"},
        }
        return {
            "session_id": session_id,
            "messages": test_messages,
            "is_complete": False,
            "text": test_messages[-1]["text"],
            "chips": test_messages[-1]["chips"],
        }

    elif step == "dpi_complete":
        reply = "This conversation is already complete. Please refresh to start a new session."
        is_complete = True
        return _respond(session)

    else:
        reply = "Unknown DPI session state. Please refresh and try again."
        return _respond(session)


# ── Endpoints ─────────────────────────────────────────────────────────────────

_DOMAIN_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

_DOMAIN_FRAMEWORK_FILES = {
    "campaign":         "campaign_silver_domain.json",
    "sales":            "sales_silver_domain.json",
    "loyalty":          "loyalty_silver_domain.json",
    "trade_promotions": "trade_promotions_silver_domain.json",
    "pricing":          "pricing_silver_domain.json",
    "digital_commerce": "digital_commerce_silver_domain.json",
}

_DOMAIN_REGISTRY_PATH = os.path.join(_DOMAIN_DATA_DIR, "domain_registry.json")
_domain_registry_cache = None

# Keyword sets for domain detection from free-text use-case / requirement fields.
# classification_data has no "domain" key, so we rely on vocabulary matching.
_DOMAIN_DETECTION_KEYWORDS: dict = {
    "campaign": frozenset({
        "campaign", "impression", "impressions", "click", "clicks",
        "conversion", "conversions", "spend", "ad spend", "ctr", "cpc",
        "cpa", "roas", "advertising", "adtech", "ad-tech", "programmatic",
        "dv360", "google ads", "meta ads", "facebook ads", "display ad",
        "creative", "placement", "attribution", "landing page", "line item",
    }),
    "sales": frozenset({
        "sales", "order", "orders", "revenue", "sell-through",
        "sell through", "commercial", "crm",
    }),
    "loyalty": frozenset({
        "loyalty", "reward", "rewards", "points", "member", "members",
        "tier", "redemption", "earn", "burn",
    }),
    "trade_promotions": frozenset({
        "trade promotion", "trade spend", "tpm", "promotional spend",
        "promotion uplift",
    }),
    "pricing": frozenset({
        "price management", "price optimisation", "price optimization",
        "price index", "margin percentage",
    }),
    "digital_commerce": frozenset({
        "ecommerce", "e-commerce", "shopify", "cart abandonment",
        "bounce rate", "add to cart", "checkout",
    }),
}


def _load_domain_registry() -> dict:
    global _domain_registry_cache
    if _domain_registry_cache is None:
        try:
            with open(_DOMAIN_REGISTRY_PATH, encoding="utf-8") as f:
                _domain_registry_cache = json.load(f)
        except Exception:
            _domain_registry_cache = {}
    return _domain_registry_cache


def _build_corpus(data: dict) -> str:
    """Flatten use_case, kpis, data_points into a single lowercase string for keyword matching.

    discovery_input may be either the pre-discovery combined dict (top-level keys)
    or disc_result (where vocabulary lives inside the nested discovery_view sub-dict).
    We check both so detection works regardless of which structure is passed in.
    """
    dv = data.get("discovery_view") or {}
    all_kpis = list(data.get("kpis") or []) + list(dv.get("kpis") or [])
    all_dp   = list(data.get("data_points") or []) + list(dv.get("dimensions") or [])
    parts = [
        data.get("use_case", ""),
        dv.get("use_case", ""),
        dv.get("result_text", ""),
        dv.get("headline", ""),
        " ".join(str(k.get("kpi_name", "") if isinstance(k, dict) else k) for k in all_kpis),
        " ".join(str(dp.get("name", "") if isinstance(dp, dict) else dp) for dp in all_dp),
        " ".join(str(dp.get("description", "") if isinstance(dp, dict) else "") for dp in all_dp),
    ]
    return " ".join(filter(None, parts)).lower()


def _match_domain_entry(text_lower: str) -> dict | None:
    """Return the first domain registry entry whose keyword set overlaps the text, or None."""
    registry = _load_domain_registry()
    all_domains = registry.get("domains", [])

    for domain_name, keywords in _DOMAIN_DETECTION_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            for entry in all_domains:
                if entry.get("name") == domain_name:
                    return entry
    return None


def _load_hierarchy(matched: dict) -> list:
    fw_file = matched.get("framework_file")
    if not fw_file:
        return []
    try:
        with open(os.path.join(_DOMAIN_DATA_DIR, fw_file), encoding="utf-8") as f:
            fw = json.load(f)
        return fw.get("hierarchy", [])
    except Exception:
        return []


def _build_domain_framework_payload(matched: dict) -> dict:
    return {
        "name":            matched.get("name"),
        "display_name":    matched.get("display_name"),
        "summary":         matched.get("summary"),
        "entity_count":    matched.get("entity_count"),
        "dimension_count": matched.get("dimension_count"),
        "event_count":     matched.get("event_count"),
        "aggregate_count": matched.get("aggregate_count"),
        "key_metrics":     matched.get("key_metrics", []),
        "standards":       matched.get("standards", []),
        "color":           matched.get("color"),
        "hierarchy":       _load_hierarchy(matched),
    }


def _detect_and_load_domain_framework(
    discovery_input: dict,
    silver_xform: dict | None = None,
) -> dict | None:
    """Detect domain and return a domain_framework payload dict, or None.

    Detection order (first match wins):
    1. silver_layer_agent._is_campaign_domain — already proven to work (drives LLM injection)
    2. Keyword matching across discovery_input corpus + silver_xform narrative/tables
    """
    registry = _load_domain_registry()
    all_domains = registry.get("domains", [])

    def _entry(name: str) -> dict | None:
        return next((d for d in all_domains if d.get("name") == name), None)

    # ── Signal 1: reuse the proven campaign detector from silver_layer_agent ──
    try:
        if silver_layer_agent._is_campaign_domain(discovery_input):
            entry = _entry("campaign")
            if entry:
                return _build_domain_framework_payload(entry)
    except Exception:
        pass

    # ── Signal 2: keyword matching on discovery_input + silver_xform content ──
    corpus = _build_corpus(discovery_input)
    if silver_xform:
        extra = " ".join(filter(None, [
            silver_xform.get("narrative", ""),
            " ".join(str(t) for t in (silver_xform.get("silver_tables") or [])),
            " ".join(str(m.get("target_table", "")) for m in (silver_xform.get("mappings") or [])),
        ]))
        corpus = corpus + " " + extra.lower()

    matched = _match_domain_entry(corpus)
    if matched:
        return _build_domain_framework_payload(matched)

    return None


def _attach_domain_framework(disc_result: dict, _classification_data) -> None:
    """Enrich disc_result['discovery_view'] with domain_framework via keyword detection."""
    dv = disc_result.get("discovery_view")
    if not isinstance(dv, dict):
        return

    # _build_corpus now covers both top-level keys and the nested discovery_view sub-dict
    matched = _match_domain_entry(_build_corpus(disc_result))

    # Fallback: slv_* silver table names in the discovery output
    if not matched:
        slv_names: set = set()
        for t in (disc_result.get("tables_by_layer") or {}).get("silver", []):
            short = t.get("table_short_name") or ""
            if short.startswith("slv_"):
                slv_names.add(short)
        for t in (dv.get("tables_by_layer") or {}).get("silver", []):
            short = t.get("table_short_name") or ""
            if short.startswith("slv_"):
                slv_names.add(short)

        if slv_names:
            registry = _load_domain_registry()
            for entry in registry.get("domains", []):
                fw_file = entry.get("framework_file")
                if not fw_file:
                    continue
                try:
                    with open(os.path.join(_DOMAIN_DATA_DIR, fw_file), encoding="utf-8") as f:
                        fw = json.load(f)
                    if set(fw.get("entities", {}).keys()) & slv_names:
                        matched = entry
                        break
                except Exception:
                    continue

    if not matched:
        return

    dv["domain_framework"] = _build_domain_framework_payload(matched)


@app.get("/catalog/domains")
async def get_catalog_domains():
    """
    Return the full domain registry — all subject areas with their silver layer
    framework summary (entity counts, standards, key metrics, status).
    Used by the Domain Framework browser in the UI.
    """
    registry_path = os.path.join(_DOMAIN_DATA_DIR, "domain_registry.json")
    try:
        with open(registry_path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Domain registry unavailable: {exc}")


@app.get("/catalog/domains/{domain_name}")
async def get_catalog_domain(domain_name: str):
    """
    Return the full silver layer framework for a specific domain.
    Includes all slv_* entities, column schemas, FK references, derived metrics,
    and source platform mappings.
    """
    filename = _DOMAIN_FRAMEWORK_FILES.get(domain_name.lower())
    if not filename:
        raise HTTPException(status_code=404, detail=f"No framework found for domain '{domain_name}'.")
    framework_path = os.path.join(_DOMAIN_DATA_DIR, filename)
    try:
        with open(framework_path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Framework file unavailable: {exc}")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "4.1.0",
        "agents": [
            "orchestrator", "pipeline-generator", "test-agent", "publisher",
            "requirement-understanding", "use-case-classification", "discovery", "data-product",
        ],
    }


@app.post("/upload")
async def upload_file(file: UploadFile = File(...), session_id: str = Form("")):
    """
    Parse an uploaded Excel, JSON, or Word file and return a preview + ref_id.
    The ref_id can be sent back with action=use_file to inject the extracted
    summary into the Requirements Agent as the initial input.

    The raw file is also persisted to gs://{GCS_UPLOADS_BUCKET}/sessions/{session_id}/
    so uploads survive across Cloud Run instances.
    """
    filename = file.filename or "upload"
    file_bytes = await file.read()

    result = extract_file(file_bytes, filename)
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])

    sid = session_id.strip() or "no-session"

    # Persist the raw uploaded file in the per-session folder
    _save_file(
        filename,
        file_bytes,
        file.content_type or "application/octet-stream",
        session_id=sid,
        is_upload=True,
    )

    # Store the extraction so the chat handler can retrieve it by ref_id
    ref_id = _save_file(
        filename,
        json.dumps(result, ensure_ascii=False),
        "application/x-file-extraction",
        session_id=sid,
    )

    return {
        "ref_id": ref_id,
        "file_name": filename,
        "file_type": result["file_type"],
        "preview": result["preview"],
    }


@app.get("/files/{file_id}")
async def download_file(file_id: str):
    """Serve a generated file by ID (SQL script, JSON report, etc.)."""
    entry = _get_file(file_id)
    if not entry:
        raise HTTPException(status_code=404, detail="File not found")
    return Response(
        content=entry["content"],
        media_type=entry["media_type"],
        headers={"Content-Disposition": f'attachment; filename="{entry["name"]}"'},
    )


# ── DDI direct REST endpoint ─────────────────────────────────────────────────

class DDIRunRequest(BaseModel):
    discovery_output: dict


@app.post("/ddi/run")
async def ddi_run_endpoint(req: DDIRunRequest):
    """
    Run the DDI pipeline (gold-er → silver-sttm → gold-final) directly on a
    pasted discovery-output JSON. Returns the assembled blueprint dict.

    The DDI flow never modifies `backend/data/utility_catalog.json`: it is the
    read-only POC seed catalog and is left exactly as-is before and after a run.
    All DDI output is persisted to the blueprint artifact only.
    """
    err = ddi_pipeline.validate_discovery_payload(req.discovery_output)
    if err:
        raise HTTPException(status_code=400, detail=err)

    try:
        blueprint = await ddi_pipeline.run(req.discovery_output)
    except Exception as exc:  # network / agent / Anthropic failure
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")

    if blueprint.get("status") != "completed":
        raise HTTPException(status_code=500, detail=blueprint.get("error", "DDI failed."))

    return blueprint


@app.post("/chat")
async def chat_endpoint(req: ChatRequest):
    """
    Unified conversational endpoint.

    Returns a `messages` array where each entry is one agent's response.
    Also returns top-level `text` + `chips` for backward compatibility.
    """
    session_id = req.session_id or str(uuid.uuid4())
    session = _sessions.get(session_id, {
        "pipeline_type": None,     # "dpi" | "design" | "sql" | None
        "pipeline_state": None,
        "conversation_history": [],
        "step": "initial",
    })

    step = session.get("step", "initial")
    pipeline_type = session.get("pipeline_type")

    # ── Route active DDI sessions ────────────────────────────────────────────
    if pipeline_type == "ddi" or step.startswith("ddi_"):
        return await _handle_ddi_chat(session_id, session, req.message, req.action)

    # ── Route active DPI/Design sessions ─────────────────────────────────────
    if pipeline_type in ("dpi", "design") or step.startswith("dpi_"):
        return await _handle_dpi_chat(session_id, session, req.message, req.action, req.file_ref_id)

    # ── Complete ──────────────────────────────────────────────────────────────
    if step == "complete":
        msg = "This session is complete. Refresh to start a new pipeline."
        return {
            "session_id": session_id,
            "messages": [{"agent": "Data Product Assistant", "step": 1, "text": msg, "chips": [], "files": []}],
            "is_complete": True,
            "text": msg,
            "chips": [],
        }

    # ── Awaiting test approval (Gate 1: Proceed / Regenerate) ────────────────
    if step == "awaiting_test_approval":
        pipeline_result = session.get("pipeline_result", {})
        spec            = session.get("spec", {})
        history         = list(session.get("conversation_history", []))
        history.append({"role": "user", "content": req.message})

        # Orchestrator (Mode D) classifies the user's reply at this gate.
        gate_decision = await _classify_gate_intent(
            req.message, "test_approval", session_id, session
        )

        if gate_decision == "confirm":
            # Run publisher analysis now that user approved the SQL
            code       = pipeline_result.get("generated_code", "")
            pub_session = _get_or_create_adk_session(session, "publisher", session_id)
            analysis   = await publisher_agent.analyze(code, spec, session=pub_session)
            messages   = _build_publisher_messages(session_id, analysis, pipeline_result.get("test_report", {}))
            approval_text = messages[-1]["text"]
            history.append({"role": "assistant", "content": approval_text})

            _sessions[session_id] = {
                **session,
                "step": "awaiting_approval",
                "pipeline_result": {
                    **pipeline_result,
                    "transformations": analysis.get("transformations", []),
                    "joins": analysis.get("joins", []),
                    "aggregations": analysis.get("aggregations", []),
                },
                "conversation_history": history,
            }
            return {
                "session_id": session_id,
                "messages": messages,
                "is_complete": False,
                "text": approval_text,
                "chips": ["Approve", "Cancel"],
            }

        else:
            # gate_decision == "reject" — user is not satisfied with the test result.
            # Dispatch via orchestrator (Mode E reject): next_agent should be pipeline-generator
            # with the user's free-text reason folded into the payload as user_correction.
            next_agent, dispatch_payload = await _dispatch_via_orchestrator(
                previous_agent="test-agent",
                previous_output=pipeline_result.get("test_report", {}),
                session_id=session_id,
                session=session,
                flow_track=session.get("flow_track", "full"),
                user_action="reject",
            )
            if next_agent and next_agent != "pipeline-generator":
                print(f"[DISPATCH WARN] test-reject expected pipeline-generator; got {next_agent!r}")

            # Resolve which free-text correction to forward to pipeline-generator.
            # Priority:
            #   1. orchestrator-built user_correction (Mode E reject payload)
            #   2. current user message (if it's an actual correction, not a chip word)
            #   3. last_correction stored on the session (so "Regenerate" clicked AFTER
            #      a typed correction still picks up that correction text)
            _GENERIC_REGEN_WORDS = {"regenerate", "retry", "no", "redo", "reject", "again", "rerun"}
            current_msg = (req.message or "").strip()
            is_generic_regen = current_msg.lower() in _GENERIC_REGEN_WORDS

            user_feedback_text = dispatch_payload.get("user_correction") if dispatch_payload else None
            if not user_feedback_text:
                if current_msg and not is_generic_regen:
                    user_feedback_text = current_msg
                else:
                    user_feedback_text = session.get("last_correction") or None

            # Persist the correction so a follow-up "Regenerate" still uses it.
            if user_feedback_text and not is_generic_regen:
                session = {**session, "last_correction": user_feedback_text}

            result = await _run_generate_and_test(spec, session_id, user_feedback=user_feedback_text)
            if result["status"] == "failed":
                error_text = f"**Pipeline regeneration failed.**\n\n{result.get('error', 'Unknown error.')}"
                history.append({"role": "assistant", "content": error_text})
                _sessions[session_id] = {**session, "conversation_history": history}
                return {
                    "session_id": session_id,
                    "messages": [{"agent": "Data Product Assistant", "step": 1, "text": error_text, "chips": ["Retry"], "files": []}],
                    "is_complete": False,
                    "text": error_text,
                    "chips": ["Retry"],
                }

            messages   = _build_test_messages(session_id, result)
            gate_text  = messages[-1]["text"]
            history.append({"role": "assistant", "content": gate_text})
            _sessions[session_id] = {
                **session,
                "step": "awaiting_test_approval",
                "pipeline_result": result,
                "conversation_history": history,
            }
            return {
                "session_id": session_id,
                "messages": messages,
                "is_complete": False,
                "text": gate_text,
                "chips": ["Proceed", "Regenerate"],
            }

    # ── Awaiting approval (Gate 2: Approve & Publish / Cancel) ───────────────
    if step == "awaiting_approval":
        msg_lower        = req.message.strip().lower()
        pipeline_result  = session.get("pipeline_result", {})
        spec             = session.get("spec", {})
        history          = list(session.get("conversation_history", []))
        history.append({"role": "user", "content": req.message})

        if any(kw in msg_lower for kw in _APPROVE_KEYWORDS):
            publish_report = await asyncio.to_thread(
                publisher_agent.execute,
                pipeline_result.get("generated_code", ""),
                spec,
                pipeline_result.get("test_report", {}),
            )
            _save_output_json(f"dpb_output_{session_id[:8]}.json", publish_report)
            publish_text_raw = _format_publish_result(publish_report)
            # Route through the orchestrator agent so the user-facing reply
            # comes from an LLM, not a server.py template.
            publish_text = await _relay(
                session_id, session, "publisher",
                {"display_output": publish_text_raw, **publish_report},
                [], "complete",
                fallback=publish_text_raw,
            )

            report_file_id = _save_file(
                f"publish-report-{session_id[:8]}.json",
                json.dumps(publish_report, indent=2, default=str),
                "application/json",
            )

            messages = [{
                "agent": "Publisher Agent",
                "step": 1,
                "text": publish_text,
                "chips": [],
                "files": [{"id": report_file_id, "name": f"publish-report-{session_id[:8]}.json", "label": "Publish Report"}],
            }]

            history.append({"role": "assistant", "content": publish_text})
            _sessions[session_id] = {**session, "step": "complete", "conversation_history": history}

            return {
                "session_id": session_id,
                "messages": messages,
                "is_complete": True,
                "text": publish_text,
                "chips": [],
            }

        elif any(kw in msg_lower for kw in {"cancel", "abort", "stop", "no", "reject"}):
            cancel_text_raw = "Pipeline cancelled. Nothing was written to BigQuery. Send a new spec whenever you're ready."
            cancel_text = await _relay(
                session_id, session, "publisher",
                {"display_output": cancel_text_raw, "status": "cancelled"},
                [], "initial",
                fallback=cancel_text_raw,
            )
            history.append({"role": "assistant", "content": cancel_text})
            _sessions[session_id] = {
                **session,
                "step": "initial",
                "pipeline_result": None,
                "conversation_history": history,
            }
            return {
                "session_id": session_id,
                "messages": [{"agent": "Data Product Assistant", "step": 1, "text": cancel_text, "chips": [], "files": []}],
                "is_complete": False,
                "text": cancel_text,
                "chips": [],
            }

        else:
            prompt_text_raw = "Reply **Approve** to publish these tables to BigQuery, or **Cancel** to abort."
            prompt_text = await _relay(
                session_id, session, "publisher",
                {"display_output": prompt_text_raw, "awaiting": "approval"},
                ["Approve", "Cancel"], "awaiting_approval",
                fallback=prompt_text_raw,
            )
            history.append({"role": "assistant", "content": prompt_text})
            _sessions[session_id] = {**session, "conversation_history": history}
            return {
                "session_id": session_id,
                "messages": [{"agent": "Data Product Assistant", "step": 1, "text": prompt_text, "chips": [], "files": []}],
                "is_complete": False,
                "text": prompt_text,
                "chips": [],
            }

    # ── Initial — welcome on first visit, then orchestrator routes ───────────────
    history = list(session.get("conversation_history", []))
    history.append({"role": "user", "content": req.message})

    # ── Routing (pipeline_type is None) ──────────────────────────────────────
    # Check for chip clicks first — works on both the first turn AND subsequent
    # turns so the frontend's starting-point card click is handled immediately.
    if pipeline_type is None:
        msg_norm = req.message.strip().lower()
        direct_route = next(
            (route for chip, route in _WELCOME_CHIP_ROUTES.items()
             if msg_norm.startswith(chip)),
            None,
        )
        if direct_route:
            route_reply = _WELCOME_CHIP_REPLIES[direct_route]
            history.append({"role": "assistant", "content": route_reply})
            _sessions[session_id] = {
                **session,
                "pipeline_type": direct_route,
                "step": "initial",
                "conversation_history": history,
            }
            return {
                "session_id": session_id,
                "messages": [{"agent": "Data Product Assistant", "step": 1,
                              "text": route_reply, "chips": [], "files": []}],
                "is_complete": False,
                "text": route_reply,
                "chips": [],
            }

        # Free-form first message → show welcome menu
        if not session.get("conversation_history"):
            welcome = _STARTING_POINT_MESSAGE
            _sessions[session_id] = {
                **session,
                "conversation_history": history + [{"role": "assistant", "content": welcome}],
            }
            return {
                "session_id": session_id,
                "messages": [{"agent": "Data Product Assistant", "step": 1, "text": welcome,
                              "chips": ["Help me find the data", "Help me design it", "Just build it"], "files": []}],
                "is_complete": False,
                "text": welcome,
                "chips": ["Help me find the data", "Help me design it", "Just build it"],
            }

        # Free-form message after welcome already shown → orchestrator decides route
        orch_session = _get_or_create_adk_session(session, "orchestrator", session_id)
        orch_result = await orchestrator.run(
            req.message,
            session.get("pipeline_state"),
            context={
                "instruction": (
                    "The user has already seen the starting point menu. "
                    "Their message is their routing selection. "
                    "Return route_dpi, route_design, or route_sql immediately. "
                    "Do NOT show the menu options again."
                )
            },
            session=orch_session,
        )

        if "error" in orch_result or "raw_output" in orch_result:
            raise HTTPException(
                status_code=500,
                detail=orch_result.get("error", orch_result.get("raw_output", "Orchestrator error")),
            )

        action         = orch_result.get("action", "ask_user")
        reply          = orch_result.get("reply", "")
        pipeline_state = orch_result.get("pipeline_state") or session.get("pipeline_state") or {}
        history.append({"role": "assistant", "content": reply})

        if action == "route_dpi":
            new_sess = {**session, "pipeline_type": "dpi", "step": "initial",
                        "pipeline_state": pipeline_state, "conversation_history": history}
            _sessions[session_id] = new_sess
            return {
                "session_id": session_id,
                "messages": [{"agent": "Data Product Assistant", "step": 1, "text": reply, "chips": [], "files": []}],
                "is_complete": False, "text": reply, "chips": [],
            }

        if action == "route_design":
            new_sess = {**session, "pipeline_type": "design", "step": "initial",
                        "pipeline_state": pipeline_state, "conversation_history": history}
            _sessions[session_id] = new_sess
            return {
                "session_id": session_id,
                "messages": [{"agent": "Data Product Assistant", "step": 1, "text": reply, "chips": [], "files": []}],
                "is_complete": False, "text": reply, "chips": [],
            }

        if action == "route_sql":
            new_sess = {**session, "pipeline_type": "sql", "step": "initial",
                        "pipeline_state": pipeline_state, "conversation_history": history}
            _sessions[session_id] = new_sess
            return {
                "session_id": session_id,
                "messages": [{"agent": "Data Product Assistant", "step": 1, "text": reply, "chips": [], "files": []}],
                "is_complete": False, "text": reply, "chips": [],
            }

        if action == "ask_user":
            _sessions[session_id] = {**session, "pipeline_state": pipeline_state,
                                     "conversation_history": history}
            return {
                "session_id": session_id,
                "messages": [{"agent": "Data Product Assistant", "step": 1, "text": reply,
                               "chips": orch_result.get("chips", []), "files": []}],
                "is_complete": False, "text": reply, "chips": orch_result.get("chips", []),
            }

        # start_pipeline — orchestrator extracted a complete spec, fall through
        if action != "start_pipeline":
            # report_success / report_failure / report_progress
            is_complete = action == "report_success"
            _sessions[session_id] = {**session, "pipeline_state": pipeline_state,
                                     "conversation_history": history,
                                     "step": "complete" if is_complete else "initial"}
            return {
                "session_id": session_id,
                "messages": [{"agent": "Data Product Assistant", "step": 1, "text": reply, "chips": [], "files": []}],
                "is_complete": is_complete, "text": reply, "chips": [],
            }

        # ── start_pipeline from orchestrator routing turn ──────────────────
        spec = orch_result.get("extracted_spec")
        if not spec:
            raise HTTPException(status_code=400, detail="Orchestrator returned start_pipeline but no extracted_spec.")

        result = await _run_generate_and_test(spec, session_id)
        if result["status"] == "failed":
            error_text = f"**Pipeline failed.**\n\n{result.get('error', 'Pipeline failed.')}"
            history.append({"role": "assistant", "content": error_text})
            _sessions[session_id] = {**session, "conversation_history": history, "step": "initial"}
            return {
                "session_id": session_id,
                "messages": [{"agent": "Data Product Assistant", "step": 1, "text": error_text, "chips": [], "files": []}],
                "is_complete": False, "text": error_text, "chips": [],
            }

        messages  = _build_test_messages(session_id, result)
        gate_text = messages[-1]["text"]
        history.append({"role": "assistant", "content": gate_text})
        _sessions[session_id] = {
            **session, "step": "awaiting_test_approval", "spec": spec,
            "pipeline_result": result, "conversation_history": history,
            "pipeline_state": {**pipeline_state, "status": "awaiting_test_approval"},
            "pipeline_type": "sql",
        }
        return {
            "session_id": session_id, "messages": messages, "is_complete": False,
            "text": gate_text, "chips": ["Proceed", "Regenerate"],
        }

    # ── pipeline_type == "sql": direct JSON spec or orchestrator spec gathering ─
    spec: dict | None = None
    try:
        parsed = json.loads(req.message)
        if isinstance(parsed, dict):
            spec = parsed
    except (json.JSONDecodeError, ValueError):
        pass

    # ── Retry — re-run the pipeline with the previously saved spec ───────────
    if req.message.strip().lower() == "retry" and session.get("spec"):
        spec = session["spec"]

    if spec is not None:
        result = await _run_generate_and_test(spec, session_id)

        if result["status"] == "failed":
            error_text = f"**Pipeline failed.**\n\n{result.get('error', 'Pipeline failed.')}\n\nPlease check your spec and try again."
            history.append({"role": "assistant", "content": error_text})
            # Keep the spec so subsequent Retry clicks still work
            _sessions[session_id] = {**session, "conversation_history": history,
                                     "step": "initial", "spec": spec}
            return {
                "session_id": session_id,
                "messages": [{"agent": "Data Product Assistant", "step": 1, "text": error_text, "chips": ["Retry"], "files": []}],
                "is_complete": False,
                "text": error_text,
                "chips": ["Retry"],
            }

        messages   = _build_test_messages(session_id, result)
        gate_text  = messages[-1]["text"]
        history.append({"role": "assistant", "content": gate_text})

        _sessions[session_id] = {
            **session,
            "step": "awaiting_test_approval",
            "spec": spec,
            "pipeline_result": result,
            "conversation_history": history,
            "pipeline_state": {"status": "awaiting_test_approval"},
        }

        return {
            "session_id": session_id,
            "messages": messages,
            "is_complete": False,
            "text": gate_text,
            "chips": ["Proceed", "Regenerate"],
        }

    # ── SQL spec gathering — orchestrator continues collecting until spec complete
    # Reached only when pipeline_type == "sql" and no JSON was detected above.
    orch_session = _get_or_create_adk_session(session, "orchestrator", session_id)
    orch_result  = await orchestrator.run(
        req.message,
        session.get("pipeline_state"),
        session=orch_session,
    )

    if "error" in orch_result or "raw_output" in orch_result:
        raise HTTPException(
            status_code=500,
            detail=orch_result.get("error", orch_result.get("raw_output", "Orchestrator error")),
        )

    action         = orch_result.get("action", "ask_user")
    reply          = orch_result.get("reply", "")
    pipeline_state = orch_result.get("pipeline_state") or session.get("pipeline_state") or {}
    history.append({"role": "assistant", "content": reply})

    if action == "ask_user":
        _sessions[session_id] = {
            **session,
            "conversation_history": history,
            "pipeline_state": pipeline_state,
            "step": "initial",
        }
        return {
            "session_id": session_id,
            "messages": [{"agent": "Data Product Assistant", "step": 1, "text": reply, "chips": orch_result.get("chips", []), "files": []}],
            "is_complete": False,
            "text": reply,
            "chips": orch_result.get("chips", []),
        }

    if action == "start_pipeline":
        spec = orch_result.get("extracted_spec", {})
        if not spec:
            raise HTTPException(status_code=400, detail="Orchestrator returned start_pipeline but no extracted_spec.")

        result = await _run_generate_and_test(spec, session_id)

        if result["status"] == "failed":
            error_text = f"**Pipeline failed.**\n\n{result.get('error', 'Pipeline failed.')}"
            _sessions[session_id] = {**session, "conversation_history": history, "step": "initial"}
            return {
                "session_id": session_id,
                "messages": [{"agent": "Data Product Assistant", "step": 1, "text": error_text, "chips": [], "files": []}],
                "is_complete": False,
                "text": error_text,
                "chips": [],
            }

        messages  = _build_test_messages(session_id, result)
        gate_text = messages[-1]["text"]
        history.append({"role": "assistant", "content": gate_text})

        _sessions[session_id] = {
            **session,
            "step": "awaiting_test_approval",
            "spec": spec,
            "pipeline_result": result,
            "conversation_history": history,
            "pipeline_state": {**pipeline_state, "status": "awaiting_test_approval"},
        }

        return {
            "session_id": session_id,
            "messages": messages,
            "is_complete": False,
            "text": gate_text,
            "chips": ["Proceed", "Regenerate"],
        }

    # report_success / report_failure / report_progress
    is_complete = action == "report_success"
    _sessions[session_id] = {
        **session,
        "conversation_history": history,
        "pipeline_state": pipeline_state,
        "step": "complete" if is_complete else "initial",
    }
    return {
        "session_id": session_id,
        "messages": [{"agent": "Data Product Assistant", "step": 1, "text": reply, "chips": [], "files": []}],
        "is_complete": is_complete,
        "text": reply,
        "chips": [],
    }


# ── DPB endpoints ─────────────────────────────────────────────────────────────

@app.post("/dpb/chat")
async def dpb_chat_endpoint(req: DPBChatRequest):
    """
    DPB conversational endpoint.
    State machine: initial → clarifying* → [phase_b_clarifying]? →
    confirm_requirement → confirm_classification → confirm_discovery → complete
    """
    session_id = req.session_id or str(uuid.uuid4())
    session = _dpb_sessions.get(session_id, {
        "step": "initial",
        "data": {},
        "original_input": "",
        "agent_history": [],
        "conversation_history": [],
        "clarification_pass": 0,
    })

    step = session["step"]
    reply = ""
    chips: list[str] = []
    files: list[str] = []
    is_complete = False
    discovery_view: Optional[dict] = None
    classification_view: Optional[dict] = None

    # ── Requirement understanding (initial + clarifying + phase_b) ─────────────

    if step in ("initial", "clarifying", "phase_b_clarifying"):
        if step == "initial":
            session["original_input"] = req.message
            ah: list[dict] = []
            conv = [{"role": "user", "content": req.message}]
            clarification_pass = 0
            extra_sess: dict = {}
        elif step == "clarifying":
            prev_q = _last_agent_question(session)
            ah = list(session.get("agent_history", []))
            if prev_q:
                ah.append({"agent_question": prev_q, "user_answer": req.message})
            conv = list(session.get("conversation_history", []))
            conv.append({"role": "user", "content": req.message})
            clarification_pass = session.get("clarification_pass", 0) + 1
            extra_sess = {}
        else:  # phase_b_clarifying
            pb_q = session.get("phase_b_question", "")
            ah = list(session.get("agent_history", []))
            ah.append({"agent_question": pb_q or "(Phase B)", "user_answer": req.message})
            conv = list(session.get("conversation_history", []))
            conv.append({"role": "user", "content": req.message})
            clarification_pass = session.get("clarification_pass", 0) + 1
            extra_sess = {"phase_b_owed": [], "phase_b_question": ""}

        req_session = _get_or_create_adk_session(session, "requirement-understanding", session_id)
        result = await requirement_understanding.run(
            req.message,
            _build_agent_context({**session, "agent_history": ah}),
            session=req_session,
        )
        _save_output_json(f"requirement_output_{session_id[:8]}.json", result)
        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"])
        result = _coerce_req_complete(result)

        if requirement_understanding.is_complete(result):
            # Domain scope check before auto-proceeding
            _tier, _canonical, _note = classify_domain(result.get("domain", ""))
            if _tier == "red":
                reply = out_of_scope_message(result.get("domain", ""))
                chips = ["Edit"]
                return _respond({**session, "step": "dpi_clarifying", "data": result})

            # Requirement done → auto-run classification immediately (no user gate)
            files.extend(_save_requirement_files(result, session_id))
            if _tier == "amber":
                reply_prefix = f"> ⚠ **Note:** {_note}\n\n"
            else:
                reply_prefix = ""

            # Orchestrator (Mode E — Dispatch) decides the next agent + payload.
            next_agent, dispatch_payload = await _dispatch_via_orchestrator(
                previous_agent="requirement-understanding",
                previous_output=result,
                session_id=session_id,
                session=session,
                flow_track=session.get("flow_track", "full"),
                user_action="confirm",
            )
            confirmed = dispatch_payload if dispatch_payload else {**result, "confirmed_by_user": True}
            if "confirmed_by_user" not in confirmed:
                confirmed["confirmed_by_user"] = True
            if next_agent and next_agent != "use-case-classification":
                print(f"[DISPATCH WARN] unexpected next_agent {next_agent!r}; running use-case-classification anyway")
            cls_session = _get_or_create_adk_session(session, "use-case-classification", session_id)
            cls_result = await use_case_classification.run(confirmed, {}, session=cls_session)
            _save_output_json(f"classification_output_{session_id[:8]}.json", cls_result)
            if "error" in cls_result:
                raise HTTPException(status_code=500, detail=cls_result["error"])

            cls_file_id = _save_file(
                f"classification-result-{session_id[:8]}.json",
                json.dumps(cls_result, indent=2, ensure_ascii=False, default=str),
                "application/json",
            )
            files.append({
                "id": cls_file_id,
                "name": f"classification-result-{session_id[:8]}.json",
                "label": "Classification Result",
            })

            classification_view = _format_classification_view(cls_result)
            chips = ["Confirm", "Override"]
            reply = await _relay(
                session_id, session, "use-case-classification", cls_result, chips,
                "confirm_classification",
                fallback=cls_result.get("display_output", "Classification complete. Please confirm."),
            )
            conv.append({"role": "assistant", "content": reply})
            _dpb_sessions[session_id] = {
                **session, "step": "confirm_classification", "data": cls_result,
                "requirement": confirmed, "conversation_history": conv,
                "agent_history": ah, "clarification_pass": clarification_pass,
            }
        else:
            chips = []
            reply = await _relay(
                session_id, session, "requirement-understanding", result, chips,
                "clarifying", fallback=result.get("raw_output", result.get("display_output", "")),
            )
            conv.append({"role": "assistant", "content": reply})
            _dpb_sessions[session_id] = {
                **session, "step": "clarifying", "data": result,
                "conversation_history": conv, "agent_history": ah,
                "clarification_pass": clarification_pass, **extra_sess,
            }

    # ── Confirm classification → run discovery ─────────────────────────────────

    elif step == "confirm_classification":
        action = (req.action or "").strip().lower()
        if not action:
            msg_lower = req.message.strip().lower()
            if any(kw in msg_lower for kw in _APPROVE_KEYWORDS):
                action = "confirm"
            else:
                _intent = await _classify_gate_intent(req.message, "classification_review", session_id, session)
                action = "confirm" if _intent == "confirm" else "override"

        override_applied: Optional[str] = None

        if action == "confirm":
            classification_data = session["data"]
        else:
            resolved_type = _resolve_use_case_type(req.message)
            if resolved_type is None:
                chips = ["Confirm", "Override"]
                reply = await _relay(
                    session_id, session, "use-case-classification",
                    {"error": f"Unknown type: '{req.message.strip()}'", "valid_types": list(VALID_USE_CASE_TYPES)},
                    chips, step,
                    fallback=(
                        f"I couldn't match '{req.message.strip()}' to a valid type. "
                        f"Valid: {', '.join(f'`{t}`' for t in VALID_USE_CASE_TYPES)}. "
                        f"Or click **Confirm** to keep the current classification."
                    ),
                )
                _dpb_sessions[session_id] = session
                return {"session_id": session_id, "text": reply, "chips": chips, "files": files, "is_complete": False}

            classification_data = use_case_classification.apply_override(session["data"], resolved_type)
            if "error" in classification_data:
                chips = ["Confirm", "Override"]
                reply = await _relay(
                    session_id, session, "use-case-classification", classification_data, chips, step,
                    fallback=f"Override failed: {classification_data['error']}. Try again or click **Confirm**.",
                )
                _dpb_sessions[session_id] = session
                return {"session_id": session_id, "text": reply, "chips": chips, "files": files, "is_complete": False}

            previous_type = session["data"].get("use_case_type", "unknown")
            override_applied = (
                f"kept as **{resolved_type}**" if previous_type == resolved_type
                else f"changed from **{previous_type}** to **{resolved_type}**"
            )

        requirement_for_handoff = dict(session.get("requirement", {}))
        if "data_points" in requirement_for_handoff and "kpis" not in requirement_for_handoff:
            requirement_for_handoff["kpis"] = [
                {"kpi_name": dp.get("name", ""), "description": dp.get("description", ""), "is_derived": dp.get("is_derived")}
                for dp in (requirement_for_handoff.get("data_points") or []) if isinstance(dp, dict) and dp.get("kind") == "kpi"
            ]
            requirement_for_handoff["attributes"] = [
                {
                    "attribute_name": dp.get("name", ""),
                    "description": dp.get("description", "")
                }
    for dp in (requirement_for_handoff.get("data_points") or [])
    if isinstance(dp, dict) and dp.get("kind") == "attribute"
]

        combined = {"session_id": session_id, **requirement_for_handoff, **classification_data}
        # Orchestrator (Mode E — Dispatch) decides the next agent + payload.
        next_agent, dispatch_payload = await _dispatch_via_orchestrator(
            previous_agent="use-case-classification",
            previous_output=classification_data,
            session_id=session_id,
            session=session,
            flow_track=session.get("flow_track", "full"),
            user_action=action,
        )
        if dispatch_payload:
            combined = {**combined, **dispatch_payload}
        if next_agent and next_agent != "discovery":
            print(f"[DISPATCH WARN] unexpected next_agent {next_agent!r}; running discovery anyway")
        disc_result = await asyncio.to_thread(discovery.run, combined, {})
        if os.environ.get("EXPECTED_TABLES_LLM", "0") == "1" and "error" not in disc_result:
            deriv_session = _get_or_create_adk_session(session, "kpi-derivation", session_id)
            await kpi_derivation.enrich(disc_result, session=deriv_session)
        _save_output_json(f"discovery_output_{session_id[:8]}.json", disc_result)

        chips = ["Confirm", "Skip"] if "error" in disc_result else ["Confirm", "Continue"]
        relay_disc = dict(disc_result)
        if override_applied:
            relay_disc["override_note"] = f"Classification {override_applied}."
        if "error" not in disc_result:
            relay_disc["display_output"] = _format_discovery_output(disc_result)
            discovery_view = disc_result.get("discovery_view")
            _attach_domain_framework(disc_result, classification_data)

        reply = await _relay(
            session_id, session, "discovery", relay_disc, chips, "confirm_discovery",
            fallback=relay_disc.get("display_output", str(disc_result.get("error", ""))),
        )
        _dpb_sessions[session_id] = {
            **session, "step": "confirm_discovery", "data": disc_result, "classification": classification_data,
        }

    # ── Confirm discovery → generate data product ──────────────────────────────

    elif step == "confirm_discovery":
        action = (req.action or "").strip().lower()
        if not action:
            action = await _classify_gate_intent(req.message, "discovery_review", session_id, session)

        if action not in ("confirm", "continue", "skip"):
            chips = ["Confirm"]
            reply = await _relay(
                session_id, session, "discovery",
                {"message": "typed feedback not supported here, please use chips"},
                chips, step,
                fallback="Click **Confirm** to generate the data product, or refresh to start over.",
            )
            _dpb_sessions[session_id] = session
            return {"session_id": session_id, "text": reply, "chips": chips, "files": files, "is_complete": False}

        dp = data_product.generate(session.get("data", {}), session.get("requirement", {}), session.get("classification", {}))
        dp_result = {"display_output": data_product.format_as_markdown(dp), **dp}
        chips = []
        is_complete = True
        reply = await _relay(
            session_id, session, "data-product", dp_result, chips, "complete",
            fallback=dp_result.get("display_output", "Data product generated."),
        )
        _dpb_sessions[session_id] = {**session, "step": "complete"}

    elif step == "complete":
        reply = "This conversation is already complete. Please refresh to start a new session."
        is_complete = True

    else:
        reply = "Unknown session state. Please refresh and try again."

    out = {
        "session_id": session_id,
        "text": reply,
        "chips": chips,
        "files": files,
        "is_complete": is_complete,
    }
    if discovery_view is not None:
        out["discovery_view"] = discovery_view
    if classification_view is not None:
        out["classification_view"] = classification_view
    return out


@app.post("/dpb/run-pipeline")
async def dpb_run_pipeline(req: RequirementRequest):
    if not req.requirement.strip():
        raise HTTPException(status_code=400, detail="Requirement cannot be empty.")

    result = await requirement_understanding.run(
        req.requirement,
        {"original_input": req.requirement, "conversation_history": [], "clarification_pass": 0},
    )
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    return {
        "step": "requirement-understanding",
        "complete": requirement_understanding.is_complete(result),
        "needs_clarification": "raw_output" in result,
        "output": result,
    }


@app.post("/dpb/clarify")
async def dpb_clarify(req: ClarificationResponse):
    result = await requirement_understanding.run(
        req.answer,
        {
            "original_input": req.original_input,
            "conversation_history": req.agent_history,
            "clarification_pass": req.clarification_pass,
        },
    )
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    return {
        "step": "requirement-understanding",
        "complete": requirement_understanding.is_complete(result),
        "needs_clarification": "raw_output" in result,
        "clarification_pass": req.clarification_pass + 1,
        "output": result,
    }


@app.post("/dpb/confirm-requirement")
async def dpb_confirm_requirement(req: ConfirmRequirementRequest):
    if req.action == "confirm":
        req.requirement_output["confirmed_by_user"] = True
        return {"step": "requirement-understanding", "confirmed": True, "output": req.requirement_output}
    else:
        agent_history = list(req.agent_history)
        agent_history.append({
            "agent_question": "User correction after summary review",
            "user_answer": req.action,
        })
        result = await requirement_understanding.run(
            req.action,
            {
                "original_input": req.original_input,
                "conversation_history": agent_history,
                "clarification_pass": 0,
                "prior_output": req.requirement_output,
            },
        )
        return {
            "step": "requirement-understanding",
            "confirmed": False,
            "complete": requirement_understanding.is_complete(result),
            "output": result,
        }


@app.post("/dpb/classify")
async def dpb_classify(req: ClassifyRequest):
    if not req.confirmed_requirement.get("confirmed_by_user"):
        raise HTTPException(status_code=400, detail="Requirement must be confirmed before classification.")

    result = await use_case_classification.run(req.confirmed_requirement, {})
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    return {
        "step": "use-case-classification",
        "classified": use_case_classification.is_classified(result),
        "output": result,
    }


@app.post("/dpb/confirm-classification")
async def dpb_confirm_classification(req: ConfirmClassificationRequest):
    if req.action == "confirm":
        return {"step": "use-case-classification", "confirmed": True, "output": req.classification_output}
    else:
        updated = use_case_classification.apply_override(req.classification_output, req.action)
        if "error" in updated:
            raise HTTPException(status_code=400, detail=updated["error"])
        return {"step": "use-case-classification", "confirmed": True, "overridden": True, "output": updated}
