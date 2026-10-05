"""Question Generation & Probe endpoints for data products.

Three endpoints:

1. POST ``/api/projects/{id}/qa/evaluate`` — generate mode. Walks the
   contract graph, builds the analyzer payload, invokes the
   ``data-product-question-analyzer`` skill, parses the JSON, and (by
   default) persists the result as a :QAEvaluation sidecar appended to
   the contract via :HAS_QA_EVAL. ``?persist=false`` returns the payload
   without writing to the graph — used by the wizard's pre-publish
   "preview" flow.

2. GET ``/api/projects/{id}/qa/evaluation`` — return the most recent
   :QAEvaluation for the contract head, with a ``stale`` flag derived
   from :DataContract.lastSchemaChangeVersion vs generatedForVersion.

3. POST ``/api/projects/{id}/qa/probe`` — probe mode. Same graph walk,
   different skill prompt, returns a verdict + gaps. Never persists.

The deterministic context-build + persist live in
``workbench/backend/qa.py``; this router is the HTTP layer + the skill
invocation. Patterned after ``routers/osi.py``.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query as QParam
from pydantic import BaseModel
from sqlmodel import Session

from .. import qa as qa_engine
from ..config import BASE_DIR, PIPELINE_PLUGINS
from ..database import get_session
from ..models import Project


router = APIRouter(tags=["qa"])


QA_SKILL = "data-product-question-analyzer"
QA_TIMEOUT_SECONDS = 90


# ── Helpers ──────────────────────────────────────────────────────────────


def _get_project(project_id: int, session: Session) -> Project:
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def _contract_id(project: Project) -> str:
    return f"{project.project_code}-contract"


# ── Models ───────────────────────────────────────────────────────────────


class EvaluateBody(BaseModel):
    trigger: str = "manual"  # 'manual' | 'edit' | 'publish' | 'wizard_preview'


class ProbeBody(BaseModel):
    question: str


# ── Skill invocation ─────────────────────────────────────────────────────


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_qa_payload(text: str, mode: str) -> dict[str, Any]:
    """Pull the final fenced ```json``` block out of the skill transcript.

    Returns a dict whose shape depends on ``mode``. Always returns a
    well-formed dict — missing keys default to empty so downstream code
    can index without guarding.
    """
    if mode == "generate":
        payload: dict[str, Any] = {
            "narrative": "",
            "questions": [],
            "near_miss_gaps": [],
        }
    else:  # probe
        payload = {
            "verdict": "no",
            "confidence": "low",
            "reasoning": "",
            "supporting_columns": [],
            "supporting_rules": [],
            "gaps": [],
        }

    matches = _JSON_BLOCK_RE.findall(text or "")
    if not matches:
        return payload

    try:
        parsed = json.loads(matches[-1])
    except json.JSONDecodeError:
        return payload
    if not isinstance(parsed, dict):
        return payload

    if mode == "generate":
        if isinstance(parsed.get("narrative"), str):
            payload["narrative"] = parsed["narrative"]
        if isinstance(parsed.get("questions"), list):
            payload["questions"] = [q for q in parsed["questions"] if isinstance(q, dict)]
        if isinstance(parsed.get("near_miss_gaps"), list):
            payload["near_miss_gaps"] = [
                g for g in parsed["near_miss_gaps"] if isinstance(g, dict)
            ]
    else:
        verdict = (parsed.get("verdict") or "").strip().lower()
        if verdict in {"answerable", "partially", "no", "out_of_scope"}:
            payload["verdict"] = verdict
        if isinstance(parsed.get("confidence"), str):
            payload["confidence"] = parsed["confidence"].lower()
        if isinstance(parsed.get("reasoning"), str):
            payload["reasoning"] = parsed["reasoning"]
        for k in ("supporting_columns", "supporting_rules"):
            if isinstance(parsed.get(k), list):
                payload[k] = [str(x) for x in parsed[k] if x]
        if isinstance(parsed.get("gaps"), list):
            payload["gaps"] = [g for g in parsed["gaps"] if isinstance(g, dict)]

    return payload


async def _run_qa_analyzer(
    context: dict,
    mode: str,
    question: Optional[str] = None,
) -> tuple[dict, Optional[str]]:
    """Invoke the data-product-question-analyzer skill via the Claude Code SDK.

    Returns ``(payload, error)``. On any failure ``payload`` is the
    well-formed empty shape and ``error`` is a string the caller can
    surface to the UI. Same async-iterator + transcript-accumulate pattern
    as ``osi.py:_run_osi_advisor``.
    """
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import (
            AssistantMessage,
            ResultMessage,
            TextBlock,
        )
    except ImportError:
        return _parse_qa_payload("", mode), "claude-agent-sdk is not installed"

    # The skill expects ``mode`` + the full context dict; in probe mode it
    # also needs ``question``. Strip our internal _meta key — the skill
    # doesn't need it and it would just be noise.
    ctx_for_skill = {k: v for k, v in context.items() if not k.startswith("_")}
    ctx_for_skill["mode"] = mode
    if mode == "probe":
        ctx_for_skill["question"] = (question or "").strip()

    ctx_json = json.dumps(ctx_for_skill, indent=2, default=str)
    # Keep the prompt within budget — the analyzer is invoked synchronously
    # and most products have far less than this.
    if len(ctx_json) > 60_000:
        ctx_json = ctx_json[:60_000] + "\n... [truncated]"

    if mode == "generate":
        instruction = (
            "Emit ONE fenced ```json block with `narrative` + `questions[]` + "
            "`near_miss_gaps[]`. Follow the skill's generate-mode output spec "
            "exactly. No prose outside the json block."
        )
    else:
        instruction = (
            "Emit ONE fenced ```json block with `verdict` + `confidence` + "
            "`reasoning` + `supporting_columns[]` + `supporting_rules[]` + "
            "`gaps[]`. Follow the skill's probe-mode output spec exactly. "
            "No prose outside the json block."
        )

    system_prompt = (
        f"FIRST: Load the `{QA_SKILL}` skill via the Skill tool, then follow its "
        "instructions exactly. The user message contains one fenced ```json``` "
        "block carrying the analyzer's input context. Do not write files. Do not "
        "run shell commands. Do not answer in prose outside the json block."
    )

    user_prompt = (
        f"FIRST: Load the {QA_SKILL} skill using the Skill tool.\n\n"
        f"Input context (json):\n```json\n{ctx_json}\n```\n\n"
        f"{instruction}"
    )

    options = ClaudeAgentOptions(
        allowed_tools=["Read", "Skill"],
        permission_mode="acceptEdits",
        cwd=str(BASE_DIR),
        max_turns=4,
        skills="all",
        plugins=PIPELINE_PLUGINS,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": system_prompt,
        },
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
                record_usage(source="qa_analyzer", usage=extract_usage(message))
                if message.is_error:
                    return _parse_qa_payload(
                        "\n".join(transcript_parts), mode
                    ), "QA analyzer returned an error"
    except Exception as e:
        return _parse_qa_payload("\n".join(transcript_parts), mode), f"QA analyzer failed: {e}"

    payload = _parse_qa_payload("\n".join(transcript_parts), mode)
    return payload, None


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post("/api/projects/{project_id}/qa/evaluate")
async def evaluate(
    project_id: int,
    body: EvaluateBody,
    persist: bool = QParam(True, description="Persist a :QAEvaluation sidecar; set false for wizard preview."),
    session: Session = Depends(get_session),
):
    """Generate a curated question set for the project's data product.

    Always returns the structured payload. When ``persist=true`` (default)
    a :QAEvaluation sidecar is appended to :DataContract. ``persist=false``
    runs the analyzer without writing — useful from the wizard before the
    contract is even saved.
    """
    project = _get_project(project_id, session)
    contract_id = _contract_id(project)

    context = qa_engine.build_qa_context(project, contract_id)
    if context is None:
        raise HTTPException(
            409,
            "Contract not found in the graph yet — publish the product first.",
        )
    if not (context.get("datasets") or []):
        raise HTTPException(
            409,
            "Data product hasn't been materialised yet — run odcs_to_dprod (or "
            "publish for the first time) before generating questions.",
        )

    try:
        payload, advisor_error = await asyncio.wait_for(
            _run_qa_analyzer(context, mode="generate"),
            timeout=QA_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        payload = _parse_qa_payload("", "generate")
        advisor_error = f"Analyzer timed out after {QA_TIMEOUT_SECONDS}s"

    meta = context.get("_meta") or {}
    generated_for_version = (
        meta.get("last_schema_change_version")
        or meta.get("current_version")
        or 1
    )

    persisted: dict[str, Any] = {"batch_id": None, "uri": None}
    if persist:
        persisted = qa_engine.persist_qa_evaluation(
            project,
            contract_id,
            payload,
            triggered_by=body.trigger,
            generated_for_version=generated_for_version,
            advisor_error=advisor_error,
        )

    return {
        "narrative": payload.get("narrative", ""),
        "questions": payload.get("questions", []),
        "near_miss_gaps": payload.get("near_miss_gaps", []),
        "advisor_error": advisor_error,
        "generated_for_version": generated_for_version,
        "current_change_version": meta.get("last_schema_change_version"),
        "stale": False,
        "trigger": body.trigger,
        "batch_id": persisted.get("batch_id"),
        "evaluation_uri": persisted.get("uri"),
        "persisted": persist and persisted.get("uri") is not None,
    }


@router.get("/api/projects/{project_id}/qa/evaluation")
def get_latest_evaluation(project_id: int, session: Session = Depends(get_session)):
    """Return the most recent :QAEvaluation for the project's contract head,
    or ``{evaluation: null}`` when none exists yet. The ``stale`` flag is
    computed against :DataContract.lastSchemaChangeVersion so the UI knows
    whether to surface a Regenerate prompt."""
    project = _get_project(project_id, session)
    contract_id = _contract_id(project)
    evaluation = qa_engine.read_latest_evaluation(project, contract_id)
    return {"evaluation": evaluation}


@router.post("/api/projects/{project_id}/qa/probe")
async def probe(
    project_id: int,
    body: ProbeBody,
    session: Session = Depends(get_session),
):
    """Probe whether a free-form question can be answered by this product.

    Never persists — purely ephemeral. Returns the analyzer's verdict +
    gaps structure verbatim. Empty ``question`` is a 400.
    """
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(400, "question is required")

    project = _get_project(project_id, session)
    contract_id = _contract_id(project)

    context = qa_engine.build_qa_context(project, contract_id)
    if context is None or not (context.get("datasets") or []):
        raise HTTPException(
            409,
            "Data product hasn't been materialised yet — can't probe a non-existent product.",
        )

    try:
        payload, advisor_error = await asyncio.wait_for(
            _run_qa_analyzer(context, mode="probe", question=question),
            timeout=QA_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        payload = _parse_qa_payload("", "probe")
        advisor_error = f"Analyzer timed out after {QA_TIMEOUT_SECONDS}s"

    return {
        "question": question,
        "verdict": payload.get("verdict", "no"),
        "confidence": payload.get("confidence", "low"),
        "reasoning": payload.get("reasoning", ""),
        "supporting_columns": payload.get("supporting_columns", []),
        "supporting_rules": payload.get("supporting_rules", []),
        "gaps": payload.get("gaps", []),
        "advisor_error": advisor_error,
    }
