"""Transform-Placement Advisor — recommends WHERE each transform runs.

For a **cross-platform** data product served via `transfer_then_transform`, each
compiled transform op can run on the **extract side** (source, before the move —
ETL) or the **target side** (after load — ELT). This advisor analyzes the
product's mappings + dataset transforms, assigns each op a side via the pure
`platform.transform_placement` classifier, and returns a per-op recommendation
with rationale so the Data Engineer can decide.

Mirrors `routers/serving_strategy.py` exactly:
  * a DETERMINISTIC heuristic owns the recommendation + governance-`required`
    drivers (mask → extract is a hard governance contract, never an LLM opinion),
  * an optional `transform-placement-advisor` skill enriches the narrative but can
    never flip the chosen placement nor move a governance-forced op off extract,
  * the endpoint never hard-fails (skill wrapped in a timeout + try/except).

The engineer's decision is persisted separately (see the `/decision` endpoints) and
consumed by the transfer pipeline generator.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from ..config import BASE_DIR, PIPELINE_PLUGINS
from ..database import get_session
from ..models import Project
from ..neo4j_client import neo4j_session
from ..platform.transform_placement import (
    _DEFER_KINDS,
    _GOVERNANCE_KINDS,
    _PUSHDOWN_KINDS,
    _VALID_PLACEMENTS,
    plan_placement,
)
from .serving_strategy import (
    _norm_platform,
    _resolve_source_platform,
    _resolve_target_platform,
    _parse_skill_json,
)


router = APIRouter(
    prefix="/api/projects/{project_id}/transform-placement", tags=["transform-placement"]
)
# Unscoped sibling (parity with serving-strategy) — callers that pass ops inline.
unscoped_router = APIRouter(prefix="/api/transform-placement", tags=["transform-placement"])

ADVISOR_SKILL = "transform-placement-advisor"
ADVISOR_TIMEOUT_SECONDS = 30

_PLACEMENT_LABELS = {
    "transform_on_extract": "Transform on extract (ETL)",
    "hybrid": "Hybrid (EtLT — recommended default)",
    "transfer_then_transform": "Transfer then transform (ELT)",
}
_PLACEMENT_DESCRIPTIONS = {
    "transform_on_extract": (
        "Shape everything on the source before the move; only shaped data crosses the "
        "boundary. Best when volumes are small, or governance/masking dominates."
    ),
    "hybrid": (
        "Push cheap volume-reducers (projection, row-filter) and any required masking to "
        "the source; defer heavy relational ops (joins, aggregations, SCD2, windows) to "
        "the target's elastic compute."
    ),
    "transfer_then_transform": (
        "Move raw data first, then shape it entirely on the target. Best when the target "
        "is much more elastic than the source and no governance masking is required."
    ),
}


# ── Graph reads ────────────────────────────────────────────────────────────────

# Per-column op enumeration (dprod product columns → the mapping's transformKind).
# Anchored on the product column so catalog/dprod/literal sources all flow through.
_GATHER_COLUMN_OPS_QUERY = """
MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
MATCH (dp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc)
WHERE cm.isCurrent = true
  AND pc.uri STARTS WITH 'dprod:col:' + $project_code + '-contract:'
RETURN coalesce(ods.physicalName, ods.name) AS dataset,
       toLower(coalesce(cm.transformKind, 'direct')) AS kind,
       pc.name AS column
ORDER BY dataset, column
"""

# Dataset-level shape ops synthesized from :DatasetTransform.
_GATHER_DATASET_OPS_QUERY = """
MATCH (dc:DataContract {id: $contract_id})-[:MATERIALISES_AS]->(dp:DProdDataProduct)
MATCH (dp)-[:DPROD_OUTPUT_PORT]->()-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
OPTIONAL MATCH (ods)-[:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform)
RETURN coalesce(ods.physicalName, ods.name) AS dataset,
       dt.scdPolicyJson    AS scd_json,
       dt.groupingKeysJson AS grouping_json,
       dt.filterPredicate  AS filter,
       dt.joinsJson        AS joins_json,
       dt.dedupeJson       AS dedupe_json
ORDER BY dataset
"""


def _json_nonempty(raw: Optional[str]) -> bool:
    try:
        v = json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return bool(raw)
    if isinstance(v, (list, dict)):
        return len(v) > 0
    return bool(v)


def _scd_is_scd2(raw: Optional[str]) -> bool:
    try:
        d = json.loads(raw) if raw else {}
        return isinstance(d, dict) and d.get("type") == "scd2"
    except Exception:  # noqa: BLE001
        return False


def _gather_ops_from_graph(project: Project) -> list[dict]:
    """Enumerate every transform op as {kind, column?, dataset} for classify/plan."""
    contract_id = f"{project.project_code}-contract"
    ops: list[dict] = []
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        for r in ns.run(_GATHER_COLUMN_OPS_QUERY, project_code=project.project_code):
            ops.append({"kind": r["kind"], "column": r["column"], "dataset": r["dataset"]})
        for r in ns.run(_GATHER_DATASET_OPS_QUERY, contract_id=contract_id):
            ds = r["dataset"]
            if _scd_is_scd2(r["scd_json"]):
                ops.append({"kind": "scd2", "dataset": ds})
            if _json_nonempty(r["grouping_json"]):
                ops.append({"kind": "grouping", "dataset": ds})
            if r["filter"]:
                ops.append({"kind": "filter", "dataset": ds})
            if _json_nonempty(r["joins_json"]):
                ops.append({"kind": "join", "dataset": ds})
            if _json_nonempty(r["dedupe_json"]):
                ops.append({"kind": "dedup", "dataset": ds})
    return ops


# ── Deterministic recommendation (source of truth) ─────────────────────────────

def _recommend_placement(ops: list[dict]) -> str:
    """Pick a sensible default placement from the op mix.

    * No heavy relational ops to defer → shape on extract (ETL): everything is a
      cheap reducer/passthrough, nothing gains from the target's compute.
    * Otherwise → hybrid: push reducers + governance to the source, defer the
      heavy relational work to the target. (Pure ELT is offered but not defaulted:
      it forgoes cheap volume reduction at the source.)
    """
    kinds = {(o.get("kind") or "").lower() for o in ops}
    has_defer = bool(kinds & _DEFER_KINDS)
    return "hybrid" if has_defer else "transform_on_extract"


def _heuristic_placement(ops: list[dict], chosen: str) -> dict:
    """Per-op split + governance-`required` drivers. The skill never overrides this."""
    decision = plan_placement(chosen, ops)
    kinds = {(o.get("kind") or "").lower() for o in ops}

    drivers: list[dict] = []
    if kinds & _GOVERNANCE_KINDS:
        masked_cols = sorted({o.get("column") for o in ops
                              if (o.get("kind") or "").lower() in _GOVERNANCE_KINDS and o.get("column")})
        drivers.append({
            "code": "governance_masking", "severity": "required", "side": "extract",
            "label": "Masking runs at the source",
            "detail": (
                "A masking transform is present" + (f" ({', '.join(masked_cols)})" if masked_cols else "")
                + " — sensitive values must be masked before any row leaves the source, "
                "so this stays extract-side regardless of the chosen placement."
            ),
        })
    if any((o.get("kind") or "").lower() == "scd2" for o in ops):
        drivers.append({
            "code": "scd2_history", "severity": "required", "side": "target",
            "label": "SCD2 history persists on the target",
            "detail": "Type-2 history must accumulate in a persistent target relation; a "
                      "stateless extract pass can't build it.",
        })
    if kinds & {"join", "lookup"}:
        drivers.append({
            "code": "relational_join", "severity": "consider", "side": "target",
            "label": "Joins/lookups favor the target",
            "detail": "Relational joins are heavy and may span sources; the target's elastic "
                      "compute is usually the cheaper, safer place to run them.",
        })
    if kinds & {"aggregate", "grouping", "window"}:
        drivers.append({
            "code": "heavy_compute", "severity": "consider", "side": "target",
            "label": "Aggregations/windows favor the target",
            "detail": "Aggregations and window functions are compute-heavy; offloading them "
                      "keeps load off an operational source.",
        })
    if kinds & {"filter", "projection", "direct", "partition_prune"}:
        drivers.append({
            "code": "volume_reduction", "severity": "consider", "side": "extract",
            "label": "Volume-reducers favor the source",
            "detail": "Row-filters and column projection shrink the data before the move, "
                      "cutting network egress and target-ingest cost.",
        })

    extract = [asdict(o) for o in decision.extract_ops]
    target = [asdict(o) for o in decision.target_ops]
    forced = [asdict(o) for o in decision.forced_pre_boundary]
    return {
        "chosen_placement": chosen,
        "effective_placement": decision.effective_placement,
        "recommended_placement": None,   # filled by caller
        "per_op_assignments": extract + target,
        "extract_ops": extract,
        "target_ops": target,
        "forced_pre_boundary": forced,
        "warnings": list(decision.warnings),
        "drivers": drivers,
        "placements": [
            {"id": pid, "label": _PLACEMENT_LABELS[pid], "description": _PLACEMENT_DESCRIPTIONS[pid]}
            for pid in _VALID_PLACEMENTS
        ],
        "rationale": _heuristic_rationale(chosen, drivers, forced),
        "source": "heuristic",
    }


def _heuristic_rationale(chosen: str, drivers: list[dict], forced: list[dict]) -> str:
    label = _PLACEMENT_LABELS.get(chosen, chosen)
    base = f"Recommend **{label}**. "
    if chosen == "transform_on_extract":
        base += ("Every transform is a cheap reducer or passthrough with nothing heavy to "
                 "defer, so shaping on the source keeps the move simple and correct.")
    elif chosen == "hybrid":
        base += ("Push cheap volume-reducers and any required masking to the source, and defer "
                 "the heavy relational work (joins, aggregations, SCD2) to the target's compute.")
    else:
        base += ("Move raw data first and shape entirely on the target — best when the target "
                 "compute is far more elastic than the source.")
    if forced:
        base += (" Governance note: masking is forced to the source regardless of this choice.")
    return base


# ── Skill enrichment (narrative only) ──────────────────────────────────────────

async def _run_placement_skill(ops: list[dict], recommendation: dict) -> tuple[Optional[str], list[str], Optional[str]]:
    """Returns (rationale, considerations, error). Narrative only — never the decision."""
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
    except ImportError:
        return None, [], "claude-agent-sdk is not installed"

    inputs = {
        "source_platform": recommendation.get("source_platform"),
        "target_platform": recommendation.get("target_platform"),
        "per_op_assignments": recommendation["per_op_assignments"],
        "drivers": recommendation["drivers"],
        "recommendation": {"chosen_placement": recommendation["chosen_placement"]},
    }
    system_prompt = (
        f"FIRST: Load the `{ADVISOR_SKILL}` skill via the Skill tool, then follow it. Emit exactly one "
        "fenced JSON code block ({rationale, per_op_notes, considerations}). Respect the supplied "
        "recommendation — do not flip chosen_placement and never move a governance-forced (mask) op off "
        "the extract side. No files, no shell, no prose outside JSON."
    )
    user_prompt = (
        f"FIRST: Load the {ADVISOR_SKILL} skill using the Skill tool.\n\n"
        f"INPUTS (JSON):\n{json.dumps(inputs, ensure_ascii=False)}\n\n"
        "Output the single fenced JSON block as instructed."
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

    parts: list[str] = []
    try:
        async for message in query(prompt=user_prompt, options=options):
            if message is None:
                continue
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
            elif isinstance(message, ResultMessage):
                from ..llm_usage import extract_usage, record_usage
                record_usage(source="transform_placement", usage=extract_usage(message))
                if message.is_error:
                    return None, [], "Advisor skill returned an error"
    except Exception as e:  # noqa: BLE001
        return None, [], f"Advisor skill failed: {e}"

    payload = _parse_skill_json("\n".join(parts))
    rationale = payload.get("rationale") if isinstance(payload, dict) else None
    considerations = payload.get("considerations") if isinstance(payload, dict) else None
    if not isinstance(considerations, list):
        considerations = []
    return (rationale if isinstance(rationale, str) and rationale.strip() else None), considerations, None


# ── Core (one engine) ──────────────────────────────────────────────────────────

async def _advise_core(
    ops: list[dict],
    *,
    source_platform: str = "postgres",
    target_platform: Optional[str] = None,
    requested_placement: Optional[str] = None,
    skip_skill: bool = False,
) -> dict:
    """Deterministic per-op recommendation + optional skill narrative."""
    recommended = _recommend_placement(ops)
    chosen = requested_placement if requested_placement in _VALID_PLACEMENTS else recommended
    recommendation = _heuristic_placement(ops, chosen)
    recommendation["recommended_placement"] = recommended
    recommendation["source_platform"] = _norm_platform(source_platform)
    recommendation["target_platform"] = _norm_platform(target_platform or source_platform)

    if skip_skill:
        recommendation.setdefault("considerations", [])
        recommendation["advisor_error"] = None
        return recommendation

    advisor_error: Optional[str] = None
    try:
        rationale, considerations, advisor_error = await asyncio.wait_for(
            _run_placement_skill(ops, recommendation), timeout=ADVISOR_TIMEOUT_SECONDS,
        )
        if rationale:
            recommendation["rationale"] = rationale
            recommendation["source"] = "skill"
        recommendation["considerations"] = considerations
    except asyncio.TimeoutError:
        advisor_error = f"Advisor timed out after {ADVISOR_TIMEOUT_SECONDS}s"
        recommendation.setdefault("considerations", [])
    except Exception as e:  # noqa: BLE001
        advisor_error = f"Advisor error: {e}"
        recommendation.setdefault("considerations", [])
    recommendation["advisor_error"] = advisor_error
    return recommendation


# ── Models ─────────────────────────────────────────────────────────────────────

class OpSignal(BaseModel):
    kind: str
    column: Optional[str] = None
    dataset: Optional[str] = None


class AdviseBody(BaseModel):
    # When omitted (scoped path), ops are read from the product's graph state.
    ops: Optional[list[OpSignal]] = None
    source_platform: Optional[str] = None
    target_platform: Optional[str] = None
    # Recompute the split for a specific placement (e.g. when the DE toggles the
    # select in the dialog). Defaults to the deterministic recommendation.
    placement: Optional[str] = None
    skip_skill: bool = False


class DecisionBody(BaseModel):
    placement: str
    decided_by: Optional[str] = None


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/advise")
async def advise(
    project_id: int,
    body: AdviseBody | None = None,
    session: Session = Depends(get_session),
):
    """Engineer path: no ops → read the product's persisted graph state."""
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    body = body or AdviseBody()
    ops = (
        [o.model_dump() for o in body.ops] if body.ops is not None
        else _gather_ops_from_graph(project)
    )
    source_platform = body.source_platform or _resolve_source_platform(project, session)
    target_platform = body.target_platform or _resolve_target_platform(project, session, source_platform)
    return await _advise_core(
        ops, source_platform=source_platform, target_platform=target_platform,
        requested_placement=body.placement, skip_skill=body.skip_skill,
    )


@unscoped_router.post("/advise")
async def advise_unscoped(body: AdviseBody):
    """Inline path: ops passed directly (no project / graph read)."""
    ops = [o.model_dump() for o in (body.ops or [])]
    source_platform = body.source_platform or "postgres"
    target_platform = body.target_platform or source_platform
    return await _advise_core(
        ops, source_platform=source_platform, target_platform=target_platform,
        requested_placement=body.placement, skip_skill=body.skip_skill,
    )


@router.put("/decision")
def save_decision(
    project_id: int,
    body: DecisionBody,
    session: Session = Depends(get_session),
):
    """Persist the engineer's placement choice + the full per-op decision to the graph."""
    from .. import transform_placement_store

    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if body.placement not in _VALID_PLACEMENTS:
        raise HTTPException(400, f"placement must be one of {list(_VALID_PLACEMENTS)}")
    # Recompute the deterministic per-op decision for the chosen placement from the
    # product's current graph state, so what's stored matches what's shown.
    ops = _gather_ops_from_graph(project)
    decision = _heuristic_placement(ops, body.placement)
    decision["recommended_placement"] = _recommend_placement(ops)
    stored = transform_placement_store.save_placement_decision(
        project, placement=body.placement, decision=decision, decided_by=body.decided_by,
    )
    return {"placement": stored, "decision": decision}


@router.get("/decision")
def get_decision(project_id: int, session: Session = Depends(get_session)):
    """Read back the persisted placement decision (for reconstitution / editing)."""
    from .. import transform_placement_store

    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    result = transform_placement_store.load_placement_decision(project)
    return result or {"placement": None, "decision": None}
