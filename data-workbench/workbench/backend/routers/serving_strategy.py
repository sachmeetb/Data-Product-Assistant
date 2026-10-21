"""Serving Strategy Advisor — recommends Virtual (view) vs Materialized (dbt).

ONE recommendation engine, two callers:
  * the engineer at the serving stage (no body → reads the product's graph state), and
  * the PO in the wizard (passes in-flight `datasets` signals before anything is
    persisted).

The structured decision (`recommended_mode` / `required`) is ALWAYS the
deterministic heuristic here — the SCD2-forces-materialization rule is a hard
contract, not an LLM opinion. The `serving-strategy-advisor` skill only enriches
the narrative; it cannot flip the mode. Mirrors the advisor pattern in
routers/osi.py / routers/ingest_products.py (heuristic fallback so the endpoint
never hard-fails).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from ..config import BASE_DIR, PIPELINE_PLUGINS
from ..database import get_session
from ..models import Project
from ..neo4j_client import neo4j_session


router = APIRouter(prefix="/api/projects/{project_id}/serving-strategy", tags=["serving-strategy"])
# Unscoped sibling for the PO wizard, which has no persisted project yet in
# create mode — it passes its in-flight `datasets` signals inline. Same engine.
unscoped_router = APIRouter(prefix="/api/serving-strategy", tags=["serving-strategy"])

ADVISOR_SKILL = "serving-strategy-advisor"
ADVISOR_TIMEOUT_SECONDS = 30


_GATHER_QUERY = """
MATCH (dc:DataContract {id: $contract_id})-[:MATERIALISES_AS]->(dp:DProdDataProduct)
MATCH (dp)-[:DPROD_OUTPUT_PORT]->()-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
OPTIONAL MATCH (ods)-[:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform)
RETURN coalesce(ods.physicalName, ods.name) AS name,
       dt.scdPolicyJson      AS scd_json,
       dt.groupingKeysJson   AS grouping_json,
       dt.filterPredicate    AS filter
ORDER BY name
"""


# ── Models ───────────────────────────────────────────────────────────────────

class DatasetSignal(BaseModel):
    name: str
    scd_policy: str = ""          # 'scd2' | 'latest_only' | 'snapshot' | ''
    grouping: bool = False
    has_filter: bool = False


class AdviseBody(BaseModel):
    # When omitted, signals are read from the project's graph (engineer path).
    # The wizard passes its in-flight signals (PO path, pre-persist).
    datasets: Optional[list[DatasetSignal]] = None
    archetype: Optional[str] = None
    # 'source' | 'aggregate' | 'consumer'. An aggregate is a reusable building
    # block meant to be consumed further, so it defaults to a materialized
    # recommendation (a downstream consumer then borrows a real connection
    # rather than a fragile view-over-view). Unscoped path passes the wizard's
    # in-flight intent flag; scoped path reads dc.productKind from the graph.
    product_kind: Optional[str] = None
    cross_platform: bool = False
    # Platform hints. Scoped path derives these from the source/target
    # connections when omitted; the unscoped (wizard) path passes them inline.
    source_platform: Optional[str] = None
    target_platform: Optional[str] = None
    # Governance signal — any dataset carries a mask transform (forces the
    # transform to run pre-boundary before data leaves the source).
    has_masking: bool = False
    # Skip the (slow, ~30s) LLM narrative enrichment and return the deterministic
    # heuristic + pattern taxonomy immediately. Interactive callers (the Configure
    # Serving dialog) set this so the dialog never blocks on an LLM call.
    skip_skill: bool = False


# ── Heuristic engine (source of truth) ────────────────────────────────────────

def _heuristic_serving_strategy(
    archetype: str, datasets: list[dict], cross_platform: bool, product_kind: str = "",
) -> dict:
    """Deterministic Serving-Mode recommendation. The skill never overrides this."""
    scd2 = [d["name"] for d in datasets if d.get("scd_policy") == "scd2"]
    grouped = [d["name"] for d in datasets if d.get("grouping")]

    drivers: list[dict] = []
    required = False
    mode = "virtual"

    # Aggregate products are reusable building blocks meant to be consumed
    # further — a materialized copy lets downstream consumers borrow a real
    # connection instead of chaining views (the co-location pain). This is a
    # RECOMMENDATION (severity 'consider'), not a hard lock: a co-located
    # aggregate may still choose virtual, so it never sets `required`. A harder
    # driver below (scd2 / cross_platform) can still escalate to required.
    if (product_kind or "").strip().lower() == "aggregate":
        mode = "materialized"
        drivers.append({
            "code": "aggregate_reuse", "severity": "consider",
            "label": "Aggregate — built to be reused",
            "detail": (
                "This is an aggregate product: other data products are expected to "
                "build on it. Materializing it (dbt) gives those downstream consumers a "
                "real table to read — a downstream view-over-view only works when the "
                "whole chain is co-located on one instance. You can still choose Virtual "
                "if this aggregate and its consumers stay co-located."
            ),
            "datasets": [],
        })

    if scd2:
        required, mode = True, "materialized"
        drivers.append({
            "code": "scd2_history", "severity": "required", "label": "SCD2 history capture",
            "detail": (
                f"{', '.join(scd2)} must retain type-2 change history. A view is stateless and "
                "can't accumulate history across runs — these become dbt snapshots "
                "(valid_from / valid_to versions)."
            ),
            "datasets": scd2,
        })
    if cross_platform:
        required, mode = True, "materialized"
        drivers.append({
            "code": "cross_platform", "severity": "required", "label": "Cross-platform target",
            "detail": "The product lands on a different platform than its sources; a view can't span engines.",
            "datasets": [],
        })
    if grouped:
        drivers.append({
            "code": "heavy_aggregation", "severity": "consider", "label": "Aggregation",
            "detail": (
                f"{', '.join(grouped)} aggregate source rows. If volumes are large and reads are "
                "frequent, a materialized table avoids recomputing the aggregate on every query."
            ),
            "datasets": grouped,
        })

    if mode == "virtual":
        drivers.append({
            "code": "colocated", "severity": "info", "label": "Co-located, no history needed",
            "detail": "Sources are on the same platform and no history capture is requested — a view is sufficient and stays live.",
            "datasets": [],
        })
        drivers.append({
            "code": "future_materialize", "severity": "info", "label": "When to switch",
            "detail": "Choose Materialized if you later need SCD2 history, serve to a different platform, or hit aggregation performance limits.",
            "datasets": [],
        })

    return {
        "recommended_mode": mode,
        "required": required,
        "confidence": 90 if required else 75,
        "drivers": drivers,
        "datasets": datasets,
        "rationale": _heuristic_rationale(mode, required, scd2, grouped),
        "source": "heuristic",
    }


# ── Capability-aware pattern advisor (Axis 1 + Axis 2) ─────────────────────────
#
# Canonical serving-pattern taxonomy. The advisor feasibility-gates every pattern
# against the source×target capability matrix, recommends one feasible pattern
# (+ transform placement), and explains each alternative. Advisory only — the
# skill can enrich prose but never flip recommended_pattern or a feasibility verdict.

# Platforms treated as file/query-engine lakehouse targets.
_LAKEHOUSE_PLATFORMS = frozenset({"duckdb", "duckdb_local"})

# NB: feasibility is decided per source×target pair in _build_patterns (against the
# live capability registry), NOT by a static "unsupported" list. transfer_then_transform
# is FEASIBLE for cross-platform pairs with usable engines (DuckDB: pg/mysql→pg/duckdb;
# dlt: pg/mysql→snowflake/databricks) and only not_applicable for same-platform targets.
# federated / warehouse_native_load are the genuinely deferred (not_yet_supported) ones.

_PATTERN_LABELS = {
    "native_virtual": "Virtual view",
    "native_materialized": "Materialized (dbt)",
    "lakehouse_file": "Lakehouse (Parquet + DuckDB)",
    "transfer_then_transform": "Cross-platform transfer (dlt)",
    "federated": "Federated (live cross-system)",
    "warehouse_native_load": "Warehouse-native load",
}


def _norm_platform(p: Optional[str]) -> str:
    p = (p or "postgres").lower()
    return "postgres" if p == "postgresql" else p


def _build_patterns(
    source_platform: str,
    target_platform: str,
    *,
    scd2: bool,
    grouped: bool,
    has_masking: bool,
    recommended_mode: str,
) -> dict:
    """Return {patterns, recommended_pattern, recommended_placement}.

    Feasibility rules (fail-closed against physics/capabilities):
      * native_virtual / native_materialized require target == source platform
        (a view lives in the source DB; dbt transforms in-place through one
        connection — neither can span platforms).
      * lakehouse_file needs a file/query-engine target: feasible when the target
        is the local lakehouse OR the default same-instance case (writes local
        Parquet+DuckDB); a *different database* target is impossible here.
      * transfer_then_transform / federated / warehouse_native_load are roadmap
        (not_yet_supported) until their execution engines land.
    """
    src = _norm_platform(source_platform)
    tgt = _norm_platform(target_platform)
    same_platform = (src == tgt)
    target_is_lakehouse = tgt in _LAKEHOUSE_PLATFORMS

    reg = None
    try:
        from ..platform.registry import get_registry
        reg = get_registry()
    except Exception:  # noqa: BLE001
        reg = None

    def _native_view_ok(platform: str) -> bool:
        # We ship view emitters for these dialects; others fall back to ANSI SQL
        # which may not deploy. Treat postgres/mysql/snowflake/databricks/bigquery
        # as view-capable (the DDL generator has a dialect for each).
        return platform in {"postgres", "mysql", "snowflake", "databricks", "bigquery"}

    patterns: list[dict] = []

    # 1. native_virtual
    if not same_platform:
        patterns.append(_pat(
            "native_virtual", "impossible",
            f"A view lives inside the source database ({src}); it can't span to a "
            f"different target platform ({tgt}).",
        ))
    elif not _native_view_ok(src):
        patterns.append(_pat(
            "native_virtual", "impossible",
            f"No native-view SQL emitter for platform '{src}'.",
        ))
    else:
        patterns.append(_pat(
            "native_virtual", "feasible",
            "A CREATE VIEW over the existing tables in the source database — "
            "always live, no copy.",
        ))

    # 2. native_materialized
    if not same_platform:
        patterns.append(_pat(
            "native_materialized", "impossible",
            f"dbt transforms in-place through one connection — it can't move data "
            f"from {src} into a different platform ({tgt}). Use a cross-platform "
            "transfer instead.",
        ))
    else:
        patterns.append(_pat(
            "native_materialized", "feasible",
            "New physical tables (and SCD2 dbt snapshots) built in the same "
            "platform via dbt.",
        ))

    # 3. lakehouse_file
    lakehouse_built = True  # Phase 1 ships DuckDB + Parquet export
    lakehouse_block_reason: Optional[str] = None
    if reg is not None:
        # Gate on the target's declared lakehouse_export capability when the
        # target is an explicit lakehouse platform.
        if target_is_lakehouse and not reg.is_usable(tgt, "lakehouse_export"):
            lakehouse_built = False
            lakehouse_block_reason = f"Target '{tgt}' has no usable lakehouse/file query engine."
        # Gate on the source's declared lakehouse_source capability: the DuckDB
        # runner must be able to ATTACH the source platform.
        if lakehouse_built and not reg.is_usable(src, "lakehouse_source"):
            lakehouse_built = False
            lakehouse_block_reason = (
                f"Source platform '{src}' cannot be read by the DuckDB lakehouse runner "
                "(lakehouse_source capability is unsupported). "
                "Use a virtual view or cross-platform transfer instead."
            )
    if target_is_lakehouse or same_platform:
        if lakehouse_built:
            patterns.append(_pat(
                "lakehouse_file", "feasible",
                "Extract the product to Parquet files and query them with DuckDB — "
                "a portable file hop, no live database dependency.",
            ))
        else:
            patterns.append(_pat(
                "lakehouse_file", "impossible",
                lakehouse_block_reason or f"Target '{tgt}' has no usable lakehouse/file query engine.",
            ))
    else:
        patterns.append(_pat(
            "lakehouse_file", "impossible",
            f"Target '{tgt}' is a database, not a file/query engine — a lakehouse "
            "export can't serve into it. Use a cross-platform transfer instead.",
        ))

    # 4. transfer_then_transform — cross-platform Extract+Load (Phase 2).
    # Feasible when BOTH sides declare a usable transfer capability; else roadmap.
    transfer_ok = (
        reg is not None
        and reg.is_usable(src, "transfer_source")
        and reg.is_usable(tgt, "transfer_target")
        and not same_platform
    )
    if transfer_ok:
        patterns.append(_pat(
            "transfer_then_transform", "feasible",
            f"Cross-platform Extract+Load from {src} into {tgt} (DuckDB engine), "
            "then transform — the way to serve into a different platform.",
        ))
    elif same_platform:
        # Precondition not met (not a missing feature): transfer needs a
        # *different* target platform. Distinct code from not_yet_supported so
        # the UI can say "pick a cross-platform target" rather than "coming soon".
        patterns.append(_pat(
            "transfer_then_transform", "not_applicable",
            "Cross-platform transfer only applies when source and target are "
            "different platforms; here they match. Register and select a "
            "different-platform target (e.g. Snowflake, Databricks, or a "
            "lakehouse) to enable it.",
        ))
    else:
        patterns.append(_pat(
            "transfer_then_transform", "not_yet_supported",
            f"Cross-platform transfer from {src} into {tgt} isn't available yet — "
            "no usable transfer engine for this pair (warehouse targets land in Phase 2.1).",
        ))

    # 5-6. deferred roadmap patterns
    _reasons = {
        "federated": "Live federated querying (FDW/Trino) is deferred (roadmap).",
        "warehouse_native_load": (
            "Warehouse-native bulk load (COPY INTO / Unity) is deferred — reach "
            "Snowflake/Databricks via lakehouse_file or transfer_then_transform first."
        ),
    }
    for pid in ("federated", "warehouse_native_load"):
        patterns.append(_pat(pid, "not_yet_supported", _reasons[pid]))

    # ── Rank + recommend among feasible patterns ──────────────────────────────
    feasible = [p for p in patterns if p["feasibility"] == "feasible"]
    feasible_ids = {p["pattern"] for p in feasible}

    preferred = "native_materialized" if recommended_mode == "materialized" else "native_virtual"
    if preferred not in feasible_ids:
        # Fall back to the best feasible pattern: lakehouse, then cross-platform
        # transfer (the only options when nothing native fits, e.g. cross-platform).
        for candidate in ("lakehouse_file", "transfer_then_transform"):
            if candidate in feasible_ids:
                preferred = candidate
                break
        else:
            preferred = next(iter(feasible_ids)) if feasible_ids else None

    # Rank ordering: recommended first, then remaining feasible by a stable order.
    order = ["native_virtual", "native_materialized", "lakehouse_file", "transfer_then_transform"]
    def _rank_key(p: dict) -> tuple:
        pid = p["pattern"]
        if pid == preferred:
            return (0, 0)
        if p["feasibility"] == "feasible":
            return (1, order.index(pid) if pid in order else 99)
        if p["feasibility"] == "not_yet_supported":
            return (2, order.index(pid) if pid in order else 99)
        return (3, order.index(pid) if pid in order else 99)

    patterns.sort(key=_rank_key)
    for i, p in enumerate(patterns):
        p["rank"] = i
        p["recommended"] = (p["pattern"] == preferred)
        p["rationale"] = _pattern_rationale(p, preferred, scd2=scd2, grouped=grouped)
        # Transform placement only meaningful when data crosses a boundary.
        if p["pattern"] in ("lakehouse_file", "transfer_then_transform"):
            p["transform_placement"] = _placement(has_masking)

    return {
        "patterns": patterns,
        "recommended_pattern": preferred,
        "recommended_placement": (
            _placement(has_masking)["recommended"]
            if preferred in ("lakehouse_file", "transfer_then_transform") else None
        ),
    }


def _pat(pattern: str, feasibility: str, reason: str) -> dict:
    return {
        "pattern": pattern,
        "label": _PATTERN_LABELS.get(pattern, pattern),
        "feasibility": feasibility,
        "feasibility_reason": reason,
    }


def _placement(has_masking: bool) -> dict:
    if has_masking:
        return {
            "recommended": "transform_on_extract",
            "rationale": (
                "A masking transform is present — apply it on the source side so "
                "sensitive values never leave the boundary."
            ),
        }
    return {
        "recommended": "hybrid",
        "rationale": (
            "Push cheap volume-reducers (projection, row-filter, partition-prune) "
            "to the source; defer heavy relational ops (joins, aggregations, SCD2) "
            "to after load — do each op where it's cheapest."
        ),
    }


def _pattern_rationale(p: dict, preferred: Optional[str], *, scd2: bool, grouped: bool) -> str:
    pid = p["pattern"]
    if p["feasibility"] != "feasible":
        return p["feasibility_reason"]
    if pid == preferred:
        if pid == "native_materialized" and scd2:
            return "Recommended — SCD2 history requires physical accumulation a view can't provide."
        if pid == "native_materialized":
            return "Recommended — physical tables avoid recomputing heavy work on every read."
        if pid == "native_virtual":
            return "Recommended — co-located with sources, no history needed; a view is simplest and stays live."
        if pid == "lakehouse_file":
            return "Recommended — a portable Parquet+DuckDB copy, independent of a live database."
        if pid == "transfer_then_transform":
            return "Recommended — the only way to serve into a different platform: extract, load, then transform."
    # feasible alternative
    if pid == "native_virtual":
        return "Alternative — simplest, always live, but recomputes on every read and can't keep history."
    if pid == "native_materialized":
        return "Alternative — physical tables/snapshots; adds a build step and goes stale between runs."
    if pid == "lakehouse_file":
        return "Alternative — extract to Parquet+DuckDB for a portable copy with no live-DB dependency."
    if pid == "transfer_then_transform":
        return "Alternative — move the product to a different platform (Extract+Load, then transform)."
    return p["feasibility_reason"]


def _heuristic_rationale(mode: str, required: bool, scd2: list[str], grouped: list[str]) -> str:
    if mode == "materialized" and scd2:
        base = (
            f"Recommend **Materialized** (dbt). {', '.join(scd2)} require SCD2 history, which a virtual "
            "view cannot provide — these are built as dbt snapshots that accrue valid_from/valid_to versions on each run."
        )
    elif mode == "materialized":
        base = "Recommend **Materialized** (dbt) — a view can't satisfy the stated requirement."
    else:
        base = (
            "Recommend **Virtual** (a SQL view) — the product co-locates with its sources and needs no history "
            "capture, so a view is the simplest serving mode and always reflects live source data."
        )
    if grouped and mode == "virtual":
        base += f" Note: {', '.join(grouped)} aggregate data; if read volumes grow, consider switching to Materialized for performance."
    return base


# ── Skill narrative enrichment (optional) ──────────────────────────────────────

async def _run_advisor_skill(signals: dict, recommendation: dict) -> tuple[Optional[str], list[str], Optional[str]]:
    """Returns (rationale, considerations, error). Narrative only — never the decision."""
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
    except ImportError:
        return None, [], "claude-agent-sdk is not installed"

    inputs = {**signals, "recommendation": {
        "recommended_mode": recommendation["recommended_mode"],
        "required": recommendation["required"],
        "drivers": recommendation["drivers"],
    }}
    system_prompt = (
        f"FIRST: Load the `{ADVISOR_SKILL}` skill via the Skill tool, then follow it. Emit exactly one "
        "fenced JSON code block ({rationale, considerations}). Respect the supplied recommendation — do "
        "not flip recommended_mode or downgrade a required driver. No files, no shell, no prose outside JSON."
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
                record_usage(source="serving_strategy", usage=extract_usage(message))
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


_JSON_BLOCK_RE = __import__("re").compile(r"```(?:json)?\s*(\{.*?\})\s*```", __import__("re").DOTALL)


def _parse_skill_json(text: str) -> dict:
    matches = _JSON_BLOCK_RE.findall(text or "")
    for m in reversed(matches):
        try:
            parsed = json.loads(m)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    stripped = (text or "").strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {}


# ── Graph signal gathering ─────────────────────────────────────────────────────

def _scd_type(scd_json: Optional[str]) -> str:
    try:
        d = json.loads(scd_json) if scd_json else {}
        return d.get("type", "") if isinstance(d, dict) else ""
    except Exception:  # noqa: BLE001
        return ""


def _grouping_present(grouping_json: Optional[str]) -> bool:
    try:
        g = json.loads(grouping_json) if grouping_json else []
        return isinstance(g, list) and len(g) > 0
    except Exception:  # noqa: BLE001
        return False


def _resolve_product_kind_from_graph(project: Project) -> str:
    """Read the product's persisted ``:DataContract.productKind`` (source /
    aggregate / consumer). Empty on any error so the advisor stays best-effort."""
    contract_id = f"{project.project_code}-contract"
    try:
        with neo4j_session(
            project.neo4j_host, project.neo4j_port,
            project.neo4j_user, project.neo4j_password, project.neo4j_database,
        ) as ns:
            row = ns.run(
                "MATCH (dc:DataContract {id: $cid}) RETURN coalesce(dc.productKind, '') AS pk",
                cid=contract_id,
            ).single()
            return (row["pk"] if row else "") or ""
    except Exception:  # noqa: BLE001
        return ""


def _gather_from_graph(project: Project) -> list[dict]:
    contract_id = f"{project.project_code}-contract"
    out: list[dict] = []
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        for r in ns.run(_GATHER_QUERY, contract_id=contract_id):
            out.append({
                "name": r["name"],
                "scd_policy": _scd_type(r["scd_json"]),
                "grouping": _grouping_present(r["grouping_json"]),
                "has_filter": bool(r["filter"]),
            })
    return out


# ── Core (one engine) ─────────────────────────────────────────────────────────

async def _advise_core(
    archetype: str,
    datasets: list[dict],
    cross_platform: bool,
    *,
    product_kind: str = "",
    source_platform: str = "postgres",
    target_platform: Optional[str] = None,
    has_masking: bool = False,
    skip_skill: bool = False,
) -> dict:
    """Deterministic recommendation + optional skill narrative. Shared by both routes."""
    recommendation = _heuristic_serving_strategy(archetype, datasets, cross_platform, product_kind)
    # Feasibility-gated pattern taxonomy (Axis 1) + transform placement (Axis 2).
    scd2 = any(d.get("scd_policy") == "scd2" for d in datasets)
    grouped = any(d.get("grouping") for d in datasets)
    pattern_payload = _build_patterns(
        source_platform,
        target_platform or source_platform,
        scd2=scd2, grouped=grouped, has_masking=has_masking,
        recommended_mode=recommendation["recommended_mode"],
    )
    recommendation.update(pattern_payload)
    recommendation["source_platform"] = _norm_platform(source_platform)
    recommendation["target_platform"] = _norm_platform(target_platform or source_platform)
    # Fast path for interactive callers — skip the slow LLM narrative enrichment.
    if skip_skill:
        recommendation.setdefault("considerations", [])
        recommendation["advisor_error"] = None
        return recommendation
    signals = {"archetype": archetype, "cross_platform": cross_platform, "datasets": datasets}
    advisor_error: Optional[str] = None
    try:
        rationale, considerations, advisor_error = await asyncio.wait_for(
            _run_advisor_skill(signals, recommendation), timeout=ADVISOR_TIMEOUT_SECONDS,
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


# ── Endpoints ───────────────────────────────────────────────────────────────────

@router.post("/advise")
async def advise(
    project_id: int,
    body: AdviseBody | None = None,
    session: Session = Depends(get_session),
):
    """Engineer path: no body → read the product's persisted graph state."""
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    body = body or AdviseBody()
    archetype = body.archetype or project.archetype or ""
    datasets = (
        [d.model_dump() for d in body.datasets] if body.datasets is not None
        else _gather_from_graph(project)
    )
    # Derive source + target platforms from the bindings unless supplied inline.
    source_platform = body.source_platform or _resolve_source_platform(project, session)
    target_platform = body.target_platform or _resolve_target_platform(project, session, source_platform)
    cross_platform = body.cross_platform or (
        _norm_platform(source_platform) != _norm_platform(target_platform)
    )
    product_kind = body.product_kind or _resolve_product_kind_from_graph(project)
    return await _advise_core(
        archetype, datasets, cross_platform,
        product_kind=product_kind,
        source_platform=source_platform, target_platform=target_platform,
        has_masking=body.has_masking, skip_skill=body.skip_skill,
    )


@unscoped_router.post("/advise")
async def advise_unscoped(body: AdviseBody):
    """PO wizard path: inline signals only (no project / graph read)."""
    datasets = [d.model_dump() for d in (body.datasets or [])]
    source_platform = body.source_platform or "postgres"
    target_platform = body.target_platform or source_platform
    cross_platform = body.cross_platform or (
        _norm_platform(source_platform) != _norm_platform(target_platform)
    )
    return await _advise_core(
        body.archetype or "", datasets, cross_platform,
        product_kind=body.product_kind or "",
        source_platform=source_platform, target_platform=target_platform,
        has_masking=body.has_masking, skip_skill=body.skip_skill,
    )


def _resolve_source_platform(project: Project, session: Session) -> str:
    """Source platform for the advisor.

    Served-location-first: when the consumer CONSUMES a source that was
    materialized to a distinct platform (e.g. MySQL-origin source loaded into
    Databricks), the source *lives* on that served platform — so that, not the
    origin, is what the advisor must compare against the target. Falls back to
    the SourceBinding / :CONSUMES origin-borrow otherwise.
    """
    try:
        from ..pg_resolver import (
            resolve_consumed_source_serving,
            resolve_source_connection_for_project,
        )
        loc = resolve_consumed_source_serving(
            project, session, f"{project.project_code}-contract"
        )
        if loc is not None:
            return loc.served_platform or "postgres"
        platform, _ref, _ = resolve_source_connection_for_project(
            project, session, f"{project.project_code}-contract"
        )
        return platform or "postgres"
    except Exception:  # noqa: BLE001
        return "postgres"


def _resolve_target_platform(project: Project, session: Session, source_platform: str) -> str:
    """Target platform from the product's MaterializationTarget; else source."""
    try:
        from ..models import MaterializationTarget, PlatformConnection
        row = session.get(MaterializationTarget, f"{project.project_code}-contract")
        if row is None:
            return source_platform
        if row.target_connection_id is not None:
            tconn = session.get(PlatformConnection, row.target_connection_id)
            if tconn is not None:
                return tconn.platform_type
        # Legacy inline target (connection_json — Postgres DSN or non-PG config).
        if row.connection_json and row.connection_json != "{}":
            return row.platform or source_platform
    except Exception:  # noqa: BLE001
        pass
    return source_platform
