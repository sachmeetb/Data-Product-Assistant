"""OSI score endpoints for the Product Workbench.

Three groups of endpoints:

1. **Evaluation:** POST ``/evaluate`` runs translate + validate + score and
   (when not invoked from a fast lifecycle hook) the LLM advisor for the
   narrative analysis. GET ``/evaluation`` returns the most recent eval
   for the current contract head.

2. **Advisor-only:** POST ``/advise`` runs the data-product-osi-advisor
   skill without persisting an eval. The wizard chat panel uses this when
   the PO wants more Apply cards without a fresh eval row.

3. **Apply card persistence:** POST ``/metrics``, ``/relationships``,
   ``/ai-context`` materialise the advisor's net-new constructs so the
   next translate pass picks them up. Mirrors the persist-user-rules
   pattern from the wizard's Rule Coach.

The deterministic translator + validator + scorer + persister live in
``workbench/backend/osi.py``; this router is the HTTP layer + the LLM
advisor invocation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException

from ..authz import require_role
from pydantic import BaseModel
from sqlmodel import Session, select

from .. import osi as osi_engine
from ..config import BASE_DIR, PIPELINE_PLUGINS
from ..database import get_session
from ..models import Project
from ..neo4j_client import neo4j_session


router = APIRouter(tags=["osi"])


ADVISOR_SKILL = "data-product-osi-advisor"
ADVISOR_TIMEOUT_SECONDS = 60


# ── Helpers ──────────────────────────────────────────────────────────────


def _get_project(project_id: int, session: Session) -> Project:
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def _neo4j(project: Project):
    return neo4j_session(
        project.neo4j_host,
        project.neo4j_port,
        project.neo4j_user,
        project.neo4j_password,
        project.neo4j_database,
    )


def _contract_id(project: Project) -> str:
    return f"{project.project_code}-contract"


# ── Models ───────────────────────────────────────────────────────────────


class EvaluateBody(BaseModel):
    trigger: str = "manual"  # 'submit' | 'signoff' | 'manual'
    skip_advisor: bool = False  # set True from lifecycle hooks for fast eval


class AdviseBody(BaseModel):
    # Reuses the most recent translate output to avoid a redundant graph walk;
    # if absent the advisor endpoint runs a fresh translate before invoking.
    osi: Optional[dict] = None
    score: Optional[dict] = None


class OsiMetricBody(BaseModel):
    name: str
    expression: str
    dialect: str = "ANSI_SQL"
    description: Optional[str] = None


class OsiRelationshipBody(BaseModel):
    name: str
    from_dataset: str
    to_dataset: str
    from_columns: list[str]
    to_columns: list[str]


class OsiAiContextBody(BaseModel):
    instructions: Optional[str] = None
    synonyms: list[str] = []
    examples: list[str] = []


# ── Advisor (LLM) ────────────────────────────────────────────────────────


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_SUGGESTION_BLOCK_RE = re.compile(r"```suggestion\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_advisor_payload(text: str) -> dict[str, Any]:
    """The advisor emits a single fenced ``json`` block with
    ``{narrative, suggestions: [...]}``. Suggestions are also fenced
    individually (``suggestion`` blocks) to plug into the existing Apply
    protocol — pull both out so the caller has options."""
    payload: dict[str, Any] = {"narrative": "", "suggestions": []}

    # Primary path — one big json block.
    matches = _JSON_BLOCK_RE.findall(text or "")
    if matches:
        try:
            parsed = json.loads(matches[-1])
            if isinstance(parsed, dict):
                if isinstance(parsed.get("narrative"), str):
                    payload["narrative"] = parsed["narrative"]
                if isinstance(parsed.get("suggestions"), list):
                    payload["suggestions"] = [
                        s for s in parsed["suggestions"] if isinstance(s, dict)
                    ]
        except json.JSONDecodeError:
            pass

    # Secondary — individual fenced suggestion blocks (the Apply protocol's
    # native shape). Only used if the primary path didn't yield any.
    if not payload["suggestions"]:
        for raw in _SUGGESTION_BLOCK_RE.findall(text or ""):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    payload["suggestions"].append(parsed)
            except json.JSONDecodeError:
                continue

    return payload


async def _run_osi_advisor(osi_dict: dict, score: dict) -> tuple[str, list[dict], Optional[str]]:
    """Invoke the data-product-osi-advisor skill via the Claude Code SDK.

    Returns ``(narrative, suggestions, error)``. On any failure the
    narrative is empty and the error string is populated; callers persist
    the deterministic part of the eval anyway.
    """
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import (
            AssistantMessage,
            ResultMessage,
            TextBlock,
        )
    except ImportError:
        return "", [], "claude-agent-sdk is not installed"

    spec_path = BASE_DIR / "playbook" / "osi" / "spec_v0.1.1.md"
    osi_yaml_preview = json.dumps(osi_dict, indent=2)[:8000]
    score_summary = {
        "band": score.get("band"),
        "completeness": score.get("completeness"),
        "conformance_pass": score.get("conformance_pass"),
        "checklist": score.get("checklist"),
        "errors": score.get("errors"),
    }

    system_prompt = (
        f"FIRST: Load the `{ADVISOR_SKILL}` skill via the Skill tool, then follow its "
        "instructions exactly. Read the OSI spec at the path the user gives you, "
        "study the OSI dict + validation report + checklist, and emit ONE fenced "
        "json block with `narrative` (markdown string, 2-3 paragraphs) and "
        "`suggestions` (array of Apply card objects). Do not write files. Do not "
        "run shell commands. Do not answer in prose outside the json block."
    )

    user_prompt = (
        f"FIRST: Load the {ADVISOR_SKILL} skill using the Skill tool.\n\n"
        f"Inputs:\n"
        f"- spec_path: {spec_path}\n"
        f"- osi_dict (JSON): {osi_yaml_preview}\n"
        f"- score (JSON): {json.dumps(score_summary, indent=2)}\n\n"
        "Emit one fenced ```json block with `narrative` + `suggestions[]`. "
        "Suggestions use the existing Apply protocol; valid `applies_to` values "
        "for OSI are: `osi_metric_create`, `osi_relationship_create`, "
        "`osi_ai_context_set`."
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
                record_usage(source="osi_advisor", usage=extract_usage(message))
                if message.is_error:
                    return "", [], "OSI advisor returned an error"
    except Exception as e:
        return "", [], f"OSI advisor failed: {e}"

    payload = _parse_advisor_payload("\n".join(transcript_parts))
    return payload.get("narrative", ""), payload.get("suggestions", []), None


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post("/api/projects/{project_id}/osi/evaluate")
async def evaluate(
    project_id: int,
    body: EvaluateBody,
    session: Session = Depends(get_session),
    _role=Depends(require_role("owner")),
):
    """Run a readiness evaluation against the contract's selected rubric.

    Flow: read scoringRubric (default 'osi') → load rubric config → translate
    (if requires_translation) → validate (if requires_translation) → score
    via the rubric's predicates → advisor (only when the rubric ships one
    AND skip_advisor is false) → persist. Always succeeds (advisor failures
    don't fail the request — the deterministic part persists with a null
    narrative)."""
    project = _get_project(project_id, session)
    contract_id = _contract_id(project)

    rubric_name = osi_engine.read_contract_rubric(project, contract_id)
    rubric = osi_engine.load_rubric(rubric_name)

    if rubric.get("requires_translation", True):
        osi_dict = osi_engine.translate_to_osi(project, contract_id)
        errors = osi_engine.validate_osi(osi_dict)
    else:
        osi_dict = {"version": osi_engine.OSI_VERSION, "semantic_model": []}
        errors = []

    score = osi_engine.score_rubric(
        osi_dict, errors, rubric, project=project, contract_id=contract_id
    )

    narrative: Optional[str] = None
    suggestions: list[dict] = []
    advisor_error: Optional[str] = None

    # Advisor is rubric-specific. OSI ships data-product-osi-advisor; other
    # rubrics opt out by setting advisor: null in their YAML. Future rubrics
    # can wire their own skill the same way.
    advisor_config = rubric.get("advisor") or None
    if advisor_config and not body.skip_advisor:
        import asyncio
        try:
            narrative, suggestions, advisor_error = await asyncio.wait_for(
                _run_osi_advisor(osi_dict, score),
                timeout=ADVISOR_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            advisor_error = f"Advisor timed out after {ADVISOR_TIMEOUT_SECONDS}s"

    # Resolve versioned id, persist eval, write artifact.
    versioned_id: Optional[str] = None
    with _neo4j(project) as ns:
        row = ns.run(
            "MATCH (dc:DataContract {id: $cid}) "
            "RETURN (dc.id + ':v' + toString(dc.currentVersion)) AS vid",
            cid=contract_id,
        ).single()
        if row:
            versioned_id = row["vid"]

    persisted = osi_engine.persist_evaluation(
        project, contract_id, versioned_id, score, narrative, body.trigger, rubric=rubric
    )
    artifact_path = osi_engine.write_analysis_artifact(
        project.project_code, score, narrative, rubric=rubric
    )

    return {
        "band": score["band"],
        "completeness": score["completeness"],
        "conformance_pass": score["conformance_pass"],
        "checklist": score["checklist"],
        "errors": score["errors"],
        "narrative": narrative,
        "suggestions": suggestions,
        "advisor_error": advisor_error,
        "batch_id": persisted["batch_id"],
        "evaluation_uri": persisted["uri"],
        "versioned_id": versioned_id,
        "artifact_path": str(artifact_path),
        "trigger": body.trigger,
        "rubric": rubric.get("id"),
        "rubric_label": rubric.get("label"),
        "rubric_short_label": rubric.get("short_label"),
        "evaluator_version": rubric.get("evaluator_version"),
    }


READ_LATEST_EVAL = """\
MATCH (dc:DataContract {id: $contract_id})-[:HAS_OSI_EVAL]->(oe:OsiEvaluation)
WITH oe, coalesce(dc.scoringRubric, 'osi') AS contract_rubric
ORDER BY oe.evaluatedAt DESC LIMIT 1
RETURN oe.uri              AS uri,
       oe.band             AS band,
       oe.completeness     AS completeness,
       oe.conformancePass  AS conformance_pass,
       oe.errors           AS errors,
       oe.checklist        AS checklist,
       oe.narrative        AS narrative,
       oe.triggeredBy      AS triggered_by,
       oe.evaluatorVersion AS evaluator_version,
       oe.evaluatedAt      AS evaluated_at,
       oe.batchId          AS batch_id,
       coalesce(oe.rubric, contract_rubric, 'osi') AS rubric,
       coalesce(oe.rubricLabel, '') AS rubric_label,
       contract_rubric                    AS current_rubric
"""


@router.get("/api/projects/{project_id}/osi/evaluation")
def get_latest_evaluation(project_id: int, session: Session = Depends(get_session)):
    """Return the most recent :OsiEvaluation for the current contract head.
    Returns ``{evaluation: null}`` (200) when none exists yet — the wizard
    Step 6 + My Products View Analysis surfaces use this to decide whether
    to render an empty state."""
    project = _get_project(project_id, session)
    contract_id = _contract_id(project)
    with _neo4j(project) as ns:
        row = ns.run(READ_LATEST_EVAL, contract_id=contract_id).single()
    if not row:
        # No eval yet — still surface the contract's current rubric so the
        # empty-state copy can match (e.g. "No AI-Ready score yet").
        try:
            rubric_name = osi_engine.read_contract_rubric(project, contract_id)
            rubric = osi_engine.load_rubric(rubric_name)
        except Exception:
            rubric = osi_engine.load_rubric(osi_engine.DEFAULT_RUBRIC)
        return {
            "evaluation": None,
            "rubric": rubric.get("id"),
            "rubric_label": rubric.get("label"),
            "rubric_short_label": rubric.get("short_label"),
            "rubric_description": rubric.get("description"),
            "rubric_empty_state_note": rubric.get("empty_state_note"),
            "rubric_band_headlines": rubric.get("band_headlines") or {},
        }

    def _parse(s):
        if not s:
            return [] if s == "" else None
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return None

    evaluated_at = row["evaluated_at"]
    # The eval may have been written under a different rubric than the
    # contract currently has (rubric was switched after this eval landed).
    # Always render against the rubric the eval was *scored under* so the
    # checklist labels and band headlines stay coherent with the data.
    try:
        rubric = osi_engine.load_rubric(row["rubric"] or "osi")
    except Exception:
        rubric = osi_engine.load_rubric(osi_engine.DEFAULT_RUBRIC)
    current_rubric_name = row["current_rubric"]
    return {
        "evaluation": {
            "uri": row["uri"],
            "band": row["band"],
            "completeness": row["completeness"],
            "conformance_pass": row["conformance_pass"],
            "errors": _parse(row["errors"]) or [],
            "checklist": _parse(row["checklist"]) or [],
            "narrative": row["narrative"] or None,
            "triggered_by": row["triggered_by"],
            "evaluator_version": row["evaluator_version"],
            "evaluated_at": evaluated_at.isoformat() if evaluated_at else None,
            "batch_id": row["batch_id"],
            "rubric": row["rubric"],
            "rubric_label": row["rubric_label"] or rubric.get("label"),
        },
        "rubric": rubric.get("id"),
        "rubric_label": rubric.get("label"),
        "rubric_short_label": rubric.get("short_label"),
        "rubric_description": rubric.get("description"),
        "rubric_empty_state_note": rubric.get("empty_state_note"),
        "rubric_band_headlines": rubric.get("band_headlines") or {},
        # When current_rubric != row["rubric"], the contract was switched
        # since this eval — the UI can render a "Rubric changed" footnote.
        "rubric_changed_since_eval": (current_rubric_name != row["rubric"]),
        "current_rubric": current_rubric_name,
    }


@router.post("/api/projects/{project_id}/osi/advise")
async def advise(
    project_id: int,
    body: AdviseBody,
    session: Session = Depends(get_session),
):
    """Run the advisor without persisting a new evaluation. Useful from the
    wizard chat panel when the PO wants fresh Apply cards but doesn't want
    a new :OsiEvaluation row."""
    project = _get_project(project_id, session)
    contract_id = _contract_id(project)

    osi_dict = body.osi or osi_engine.translate_to_osi(project, contract_id)
    if body.score is not None:
        score = body.score
    else:
        errors = osi_engine.validate_osi(osi_dict)
        score = osi_engine.score_osi(osi_dict, errors)

    narrative, suggestions, advisor_error = await _run_osi_advisor(osi_dict, score)
    return {
        "narrative": narrative,
        "suggestions": suggestions,
        "advisor_error": advisor_error,
    }


# ── Apply-card persistence ───────────────────────────────────────────────


PERSIST_OSI_METRIC = """\
MATCH (dc:DataContract {id: $contract_id})
MERGE (m:OsiMetric {uri: 'osi:metric:' + $contract_id + ':' + $name})
SET m.name = $name,
    m.expression = $expression,
    m.dialect = $dialect,
    m.description = $description,
    m.updatedAt = datetime()
MERGE (dc)-[:HAS_METRIC]->(m)
RETURN m.uri AS uri
"""


PERSIST_OSI_RELATIONSHIP = """\
MATCH (dc:DataContract {id: $contract_id})
MERGE (r:OsiRelationship {uri: 'osi:rel:' + $contract_id + ':' + $name})
SET r.name = $name,
    r.fromDataset = $from_dataset,
    r.toDataset = $to_dataset,
    r.fromColumns = $from_columns,
    r.toColumns = $to_columns,
    r.updatedAt = datetime()
MERGE (dc)-[:HAS_RELATIONSHIP]->(r)
RETURN r.uri AS uri
"""


SET_AI_CONTEXT = """\
MATCH (dc:DataContract {id: $contract_id})
SET dc.aiContextJson = $ai_context_json
RETURN (dc.id + ':v' + toString(dc.currentVersion)) AS versioned_id
"""


@router.post("/api/projects/{project_id}/osi/metrics")
def upsert_metric(
    project_id: int,
    body: OsiMetricBody,
    session: Session = Depends(get_session),
):
    project = _get_project(project_id, session)
    contract_id = _contract_id(project)
    name = body.name.strip()
    expression = body.expression.strip()
    if not name or not expression:
        raise HTTPException(400, "name and expression are required")
    with _neo4j(project) as ns:
        row = ns.run(
            PERSIST_OSI_METRIC,
            contract_id=contract_id,
            name=name,
            expression=expression,
            dialect=(body.dialect or "ANSI_SQL").upper(),
            description=body.description or "",
        ).single()
    if not row:
        raise HTTPException(404, "No current contract for this project")
    return {"uri": row["uri"]}


@router.post("/api/projects/{project_id}/osi/relationships")
def upsert_relationship(
    project_id: int,
    body: OsiRelationshipBody,
    session: Session = Depends(get_session),
):
    project = _get_project(project_id, session)
    contract_id = _contract_id(project)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name is required")
    if not body.from_columns or not body.to_columns:
        raise HTTPException(400, "from_columns and to_columns are required")
    if len(body.from_columns) != len(body.to_columns):
        raise HTTPException(400, "from_columns and to_columns must have equal length")
    with _neo4j(project) as ns:
        row = ns.run(
            PERSIST_OSI_RELATIONSHIP,
            contract_id=contract_id,
            name=name,
            from_dataset=body.from_dataset,
            to_dataset=body.to_dataset,
            from_columns=body.from_columns,
            to_columns=body.to_columns,
        ).single()
    if not row:
        raise HTTPException(404, "No current contract for this project")
    return {"uri": row["uri"]}


@router.post("/api/projects/{project_id}/osi/ai-context")
def set_ai_context(
    project_id: int,
    body: OsiAiContextBody,
    session: Session = Depends(get_session),
):
    project = _get_project(project_id, session)
    contract_id = _contract_id(project)
    payload: dict[str, Any] = {}
    if body.instructions:
        payload["instructions"] = body.instructions
    if body.synonyms:
        payload["synonyms"] = list(body.synonyms)
    if body.examples:
        payload["examples"] = list(body.examples)
    if not payload:
        raise HTTPException(400, "At least one of instructions/synonyms/examples is required")
    with _neo4j(project) as ns:
        row = ns.run(
            SET_AI_CONTEXT,
            contract_id=contract_id,
            ai_context_json=json.dumps(payload),
        ).single()
    if not row:
        raise HTTPException(404, "No current contract for this project")
    return {"versioned_id": row["versioned_id"]}


@router.get("/api/projects/{project_id}/osi/translate")
def translate_only(project_id: int, session: Session = Depends(get_session)):
    """Return the translated OSI dict (and rendered YAML) without scoring.
    Used by the wizard's "Download OSI YAML" button."""
    import yaml as _yaml
    project = _get_project(project_id, session)
    contract_id = _contract_id(project)
    osi_dict = osi_engine.translate_to_osi(project, contract_id)
    return {
        "dict": osi_dict,
        "yaml": _yaml.safe_dump(osi_dict, sort_keys=False, default_flow_style=False),
    }


# ── Rubric catalog (rubric picker + hint payload) ────────────────────────


def _public_rubric(rubric: dict) -> dict:
    """Strip the predicate name from each criterion before returning to the
    frontend. Predicates are an implementation detail of the backend
    registry; the wizard only needs id / label / weight / hint."""
    return {
        "id": rubric.get("id"),
        "label": rubric.get("label"),
        "short_label": rubric.get("short_label"),
        "description": rubric.get("description"),
        "empty_state_note": rubric.get("empty_state_note"),
        "evaluator_version": rubric.get("evaluator_version"),
        "band_thresholds": rubric.get("band_thresholds") or {},
        "band_headlines": rubric.get("band_headlines") or {},
        "require_osi_conformance": bool(rubric.get("require_osi_conformance", False)),
        "has_advisor": bool(rubric.get("advisor")),
        "criteria": [
            {
                "id": c.get("id"),
                "label": c.get("label"),
                "weight": c.get("weight"),
                "hint": c.get("hint") or {},
            }
            for c in (rubric.get("criteria") or [])
        ],
    }


@router.get("/api/scoring-rubrics")
def list_scoring_rubrics():
    """Return all available rubrics. Used by the wizard's rubric picker
    (Step 1) and by OsiAnalysisPanel for hint copy."""
    return {"rubrics": [_public_rubric(r) for r in osi_engine.list_rubrics()]}


@router.get("/api/scoring-rubrics/{rubric_id}")
def get_scoring_rubric(rubric_id: str):
    """Return a single rubric's config. Falls back to OSI if unknown."""
    rubric = osi_engine.load_rubric(rubric_id)
    return _public_rubric(rubric)
