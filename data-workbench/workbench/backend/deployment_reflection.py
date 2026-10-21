"""Deployment Reflection — Phase 2 of the data-first roadmap.

Compares what the graph declares about a deployed data product against
what the deployed virtual view actually returns. Produces a structured
verdict + cited surprises + recommendations via the
`data-product-deployment-reflector` skill.

Single-purpose module: it gathers inputs, calls the SDK, parses output,
and persists a `:DeploymentReflection` graph node linked to the
`:DataContract`. Mirrors the shape of `routers/osi.py`'s OSI advisor
pipeline so the two read+render patterns are consistent.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Optional

from . import sql_executor
from .neo4j_client import neo4j_session
from .models import Project
from .sql_ident import quote_created_relation


ADVISOR_SKILL = "data-product-deployment-reflector"
ADVISOR_TIMEOUT_SECONDS = 120
EVALUATOR_VERSION = "deployment-reflector-v0.1.0"

PREVIEW_LIMIT = 50  # ideal rows per dataset (gets reduced when many datasets)
# Adaptive row budget: aim for ~300 rows total across all datasets so a 12-
# dataset product gets 25 rows/dataset rather than skipping six datasets
# entirely. Phase-2 v0 capped at 6 datasets which silently dropped the rest;
# the reflector then judged misalignment against a partial view of the
# product, which is worse than judging against thinner samples per view.
PREVIEW_TOTAL_ROW_BUDGET = 300
PREVIEW_MIN_ROWS_PER_DATASET = 10

# Tighten prompt input size — JSON-stringified inputs above these caps
# get truncated. The prompt is the dominant cost here so a real cap.
_MAX_GRAPH_SNAPSHOT_CHARS = 16_000
_MAX_PREVIEW_JSON_CHARS = 24_000


# ── Cypher: graph snapshot ─────────────────────────────────────────────────


_GRAPH_SNAPSHOT_QUERY = """
MATCH (dc:DataContract {id: $contract_id})
WHERE coalesce(dc.isCurrent, true) = true
MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
    -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
OPTIONAL MATCH (ods)-[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)

// Approved column descriptions (linked from the source :Column via mapping;
// for SA products the `:DProdColumn.description` is propagated already).
WITH dc, dp, ods, pc,
     coalesce(pc.description, '') AS column_description,
     coalesce(pc.transformHint, '') AS transform_hint

// Approved PropertyShape rules anchored to this :DProdColumn.
OPTIONAL MATCH (pc)-[:HAS_SHAPE]->(:NodeShape)-[:PROPERTY]->(ps:PropertyShape)
WHERE coalesce(ps.status, 'approved') = 'approved'

WITH dc, dp, ods, pc, column_description, transform_hint,
     collect(DISTINCT CASE WHEN ps IS NULL THEN NULL ELSE {
       rule_type: coalesce(ps.ruleType, ''),
       severity: coalesce(ps.severity, ''),
       threshold: coalesce(ps.threshold, ''),
       evidence: coalesce(ps.evidenceSummary, ''),
       rule_source: coalesce(ps.ruleSource, '')
     } END) AS rules_raw

WITH dc, dp, ods, pc, column_description, transform_hint,
     [r IN rules_raw WHERE r IS NOT NULL] AS column_rules

// :DatasetTransform sidecar per output dataset (declared shape).
OPTIONAL MATCH (ods)-[:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform)

WITH dc, dp, ods, dt,
     collect(DISTINCT CASE WHEN pc IS NULL THEN NULL ELSE {
       column_uri: pc.uri,
       name: pc.name,
       dataType: coalesce(pc.dataType, ''),
       isPrimaryKey: coalesce(pc.isPrimaryKey, false),
       description: column_description,
       transformHint: transform_hint,
       rules: column_rules
     } END) AS pcs

WITH dc, dp,
     collect(CASE WHEN ods IS NULL THEN NULL ELSE {
       dataset_uri: ods.uri,
       physicalName: coalesce(ods.physicalName, ods.name, ''),
       name: coalesce(ods.name, ''),
       description: coalesce(ods.description, ''),
       relationshipKind: coalesce(ods.relationshipKind, ''),
       transform: CASE WHEN dt IS NULL THEN null ELSE {
         filter: coalesce(dt.filterPredicate, ''),
         dedupeJson: coalesce(dt.dedupeJson, ''),
         scdPolicyJson: coalesce(dt.scdPolicyJson, ''),
         suppressedColumnsJson: coalesce(dt.suppressedColumnsJson, ''),
         groupingKeysJson: coalesce(dt.groupingKeysJson, ''),
         grainProse: coalesce(dt.grainProse, '')
       } END,
       columns: [pc IN pcs WHERE pc IS NOT NULL]
     } END) AS datasets_raw

// Q&A evaluation (most-recent).
OPTIONAL MATCH (dc)-[:HAS_QA_EVAL]->(qa:QAEvaluation)
WITH dc, dp, [d IN datasets_raw WHERE d IS NOT NULL] AS datasets, qa
ORDER BY qa.evaluatedAt DESC
WITH dc, dp, datasets, collect(qa)[0] AS qa_head

// OSI evaluation (most-recent).
OPTIONAL MATCH (dc)-[:HAS_OSI_EVAL]->(oe:OsiEvaluation)
WITH dc, dp, datasets, qa_head, oe
ORDER BY oe.evaluatedAt DESC
WITH dc, dp, datasets, qa_head, collect(oe)[0] AS oe_head

RETURN dp.uri AS product_uri,
       datasets,
       CASE WHEN qa_head IS NULL THEN null ELSE {
         narrative: coalesce(qa_head.narrative, ''),
         questions_json: coalesce(qa_head.questionsJson, '[]'),
         near_miss_gaps_json: coalesce(qa_head.nearMissGapsJson, '[]')
       } END AS qa,
       CASE WHEN oe_head IS NULL THEN null ELSE {
         band: oe_head.band,
         completeness: oe_head.completeness,
         conformance_pass: oe_head.conformancePass
       } END AS osi
"""


# Source-profiling stats for source columns mapped into this product's columns.
# Walks the dual-source mapping pattern (cm)-[:MAPS_SOURCE_COLUMN]->(:Column)
# and reads DQV measurements off the source :Column via :HAS_QUALITY_MEASUREMENT
# (the relationship name the data-profiling-to-dqv-neo4j skill emits — earlier
# this query guessed COMPUTED_ON / IS_MEASUREMENT_OF and silently returned 0
# rows). Best-effort: returns rows only when both the mapping and DQV
# measurements exist. Empty list is a valid input.
_SOURCE_PROFILING_QUERY = """
MATCH (dc:DataContract {id: $contract_id})-[:MATERIALISES_AS]->(:DProdDataProduct)
    -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)-[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
    -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
MATCH (pc)<-[:MAPS_TO_PRODUCT_COLUMN]-(cm:ColumnMapping)
WHERE coalesce(cm.status, 'approved') = 'approved' AND coalesce(cm.isCurrent, true) = true
MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(sc:Column)
MATCH (sc)-[:HAS_QUALITY_MEASUREMENT]->(qm:QualityMeasurement)-[:ON_METRIC]->(m:Metric)
WHERE m.name IN ['null_rate', 'distinct_count', 'min', 'max']
RETURN pc.uri AS column_uri,
       sc.name AS source_column_name,
       m.name AS metric,
       qm.value AS value
LIMIT 500
"""


def _decode_json(maybe_json: Any, default: Any) -> Any:
    if isinstance(maybe_json, (list, dict)):
        return maybe_json
    if not maybe_json:
        return default
    try:
        return json.loads(maybe_json)
    except (TypeError, ValueError):
        return default


def _flatten_qa(qa: Optional[dict]) -> Optional[dict]:
    if not qa:
        return None
    return {
        "narrative": qa.get("narrative") or "",
        "questions": _decode_json(qa.get("questions_json"), []),
        "near_miss_gaps": _decode_json(qa.get("near_miss_gaps_json"), []),
    }


def _flatten_transform(t: Optional[dict]) -> Optional[dict]:
    if not t:
        return None
    return {
        "filter": (t.get("filter") or "").strip(),
        "dedupe": _decode_json(t.get("dedupeJson"), None),
        "scd_policy": _decode_json(t.get("scdPolicyJson"), None),
        "suppressed_columns": _decode_json(t.get("suppressedColumnsJson"), []),
        "grouping_keys": _decode_json(t.get("groupingKeysJson"), []),
        "grain_prose": (t.get("grainProse") or "").strip(),
    }


# ── Preview-row fetching ───────────────────────────────────────────────────


_DEPLOYED_VIEWS_QUERY = """
MATCH (dc:DataContract {id: $contract_id})
WHERE coalesce(dc.isCurrent, true) = true
MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
MATCH (dp)-[:SERVED_BY]->(sd:ServingDefinition {servingMode: 'virtual_view'})
WITH dp, sd
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
    -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
RETURN dp.uri AS product_uri,
       coalesce(sd.deployedTo, sd.viewSchema, 'public') AS view_schema,
       coalesce(sd.deployedViewNames, sd.viewNames, '[]') AS view_names_json,
       coalesce(sd.deploymentStatus, 'pending') AS deployment_status,
       collect({uri: ods.uri, physicalName: ods.physicalName, name: ods.name,
                description: ods.description, relationshipKind: coalesce(ods.relationshipKind, '')}) AS datasets
"""


# Materialized (dbt) counterpart of _DEPLOYED_VIEWS_QUERY — physical tables in
# targetSchema, no view DDL. The reflector samples these the same way.
_DEPLOYED_TABLES_QUERY = """
MATCH (dc:DataContract {id: $contract_id})
WHERE coalesce(dc.isCurrent, true) = true
MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
MATCH (dp)-[:SERVED_BY]->(sd:ServingDefinition {servingMode: 'dbt_materialized'})
WITH dp, sd
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
    -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
RETURN dp.uri AS product_uri,
       coalesce(sd.targetSchema, 'public') AS view_schema,
       coalesce(sd.buildStatus, 'pending') AS build_status,
       coalesce(sd.modelsJson, '[]') AS models_json,
       collect({uri: ods.uri, physicalName: ods.physicalName, name: ods.name,
                description: ods.description, relationshipKind: coalesce(ods.relationshipKind, '')}) AS datasets
"""


# Cross-platform transfer counterpart — the dlt transfer loaded rows into the
# target namespace (e.g. Databricks catalog.schema). Landed table names + the
# namespace come from summaryJson; the reflector samples those tables directly.
_DEPLOYED_TRANSFER_QUERY = """
MATCH (dc:DataContract {id: $contract_id})
WHERE coalesce(dc.isCurrent, true) = true
MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
MATCH (dp)-[:SERVED_BY]->(sd:ServingDefinition {servingMode: 'transfer_then_transform'})
WITH dp, sd
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
    -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
RETURN dp.uri AS product_uri,
       coalesce(sd.deploymentStatus, 'pending') AS deployment_status,
       coalesce(sd.targetSchema, 'public') AS target_schema,
       coalesce(sd.summaryJson, '{}') AS summary_json,
       collect({uri: ods.uri, physicalName: ods.physicalName, name: ods.name,
                description: ods.description, relationshipKind: coalesce(ods.relationshipKind, '')}) AS datasets
"""


def _safe_rel(name: str) -> str:
    """Mirror generate_view_ddl._safe_name — the dbt model/table name for a
    dataset's physicalName."""
    import re as _re
    return _re.sub(r"[^a-zA-Z0-9_]", "_", name or "").lower()


def _pick_view_for_dataset(view_names: list[str], dataset_phys: Optional[str]) -> Optional[str]:
    """Match a `vw_<physical>` to the dataset's physicalName via exact
    equality. Returns ``None`` when there's no exact match — the caller
    must decide whether to fall back to the primary view or skip the
    dataset. (Earlier versions used `endswith` as a fallback and
    mis-routed `employee` to `vw_department_employee`; the reflector
    caught it.) Keep this aligned with routers/serving.py."""
    if not view_names:
        return None
    if not dataset_phys:
        return None
    expected = f"vw_{dataset_phys}"
    return next((v.rsplit(".", 1)[-1] for v in view_names
                 if v.rsplit(".", 1)[-1] == expected), None)


def gather_inputs(
    project: Project,
    contract_id: str,
    platform_type: str = "postgres",
    connection_ref: Optional[dict] = None,
) -> dict[str, Any]:
    """Pre-fetch everything the reflector skill needs. Returns a dict the
    caller serializes into the prompt. Raises only when something
    load-bearing is missing — e.g. no deployed views, no graph data.

    The caller resolves the served location (``platform_type`` +
    STRUCTURED ``connection_ref``) via
    ``pg_resolver.resolve_read_connection_for_consumer`` so consumer-aligned
    projects reflect against the source they borrow. A Postgres DSN is derived
    transiently here — the single connection contract.
    """
    source_dsn = ""
    if platform_type in ("postgres", "postgresql"):
        from .routers.connections import build_connection_string
        source_dsn = build_connection_string("postgres", connection_ref or {})
        if not source_dsn:
            raise RuntimeError(
                "No source connection available for this product "
                "(neither a direct binding nor a :CONSUMES'd source resolved)"
            )

    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        # Resolve the deployed serving artifact — a virtual view OR a
        # materialized dbt table. `_relation_for(physical)` maps a dataset's
        # physicalName to its relation name in `view_schema`; the rest of the
        # gather is identical (SELECT * FROM "schema"."relation").
        view_row = ns.run(_DEPLOYED_VIEWS_QUERY, contract_id=contract_id).single()
        if view_row:
            view = dict(view_row)
            if view.get("deployment_status") != "deployed":
                raise RuntimeError(
                    f"Product is not deployed (deployment_status={view.get('deployment_status')}). "
                    "Run the Deploy Virtual View stage first."
                )
            view_schema = view.get("view_schema") or "public"
            raw_view_names = _decode_json(view.get("view_names_json"), [])
            bare_view_names = [v.rsplit(".", 1)[-1] for v in raw_view_names if v]

            def _relation_for(physical: Optional[str]) -> Optional[str]:
                return _pick_view_for_dataset(bare_view_names, physical)
        elif (tbl_row := ns.run(_DEPLOYED_TABLES_QUERY, contract_id=contract_id).single()):
            view = dict(tbl_row)
            if view.get("build_status") != "built":
                raise RuntimeError(
                    f"Materialized product is not built (build_status={view.get('build_status')}). "
                    "Run the Materialize stage first."
                )
            view_schema = view.get("view_schema") or "public"
            _model_names = {
                m.get("model") for m in _decode_json(view.get("models_json"), [])
                if isinstance(m, dict) and m.get("model")
            }

            def _relation_for(physical: Optional[str]) -> Optional[str]:
                if not physical:
                    return None
                want = _safe_rel(physical)
                return want if want in _model_names else None
        else:
            # Cross-platform transfer — rows live in the target namespace
            # (e.g. Databricks). Namespace + landed table names come from
            # summaryJson (where the dlt load actually landed); the caller
            # resolved platform_type/connection_ref for the transfer target.
            xfer_row = ns.run(_DEPLOYED_TRANSFER_QUERY, contract_id=contract_id).single()
            if not xfer_row:
                raise RuntimeError(
                    "No serving definition (virtual view, materialized, or transfer) for this product"
                )
            view = dict(xfer_row)
            if view.get("deployment_status") != "deployed":
                raise RuntimeError(
                    f"Transfer product is not deployed (deployment_status={view.get('deployment_status')}). "
                    "Run the Run Transfer stage first."
                )
            summary = _decode_json(view.get("summary_json"), {})
            # target_namespace is "catalog.schema" (3-level) or a bare schema.
            view_schema = summary.get("target_namespace") or view.get("target_schema") or "public"
            _bare_tables = {
                str(t.get("target", "")).rsplit(".", 1)[-1]
                for t in (summary.get("tables") or [])
                if isinstance(t, dict) and t.get("target")
            }
            _bare_tables.discard("")

            def _relation_for(physical: Optional[str]) -> Optional[str]:
                if not physical:
                    return None
                want = _safe_rel(physical)
                return want if want in _bare_tables else None

        # Every dataset gets sampled — no hard cap. Per-dataset row count
        # adapts to keep total ~PREVIEW_TOTAL_ROW_BUDGET so an 8-dataset
        # product gets ~37 rows/dataset, a 12-dataset gets 25, etc. The
        # JSON truncation at _MAX_PREVIEW_JSON_CHARS is the real backstop.
        datasets = [d for d in (view.get("datasets") or []) if d.get("physicalName")]
        if datasets:
            per_dataset_limit = max(
                PREVIEW_MIN_ROWS_PER_DATASET,
                min(PREVIEW_LIMIT, PREVIEW_TOTAL_ROW_BUDGET // len(datasets)),
            )
        else:
            per_dataset_limit = PREVIEW_LIMIT

        # 1) Fetch preview rows for each dataset.
        preview_by_dataset: dict[str, Any] = {}
        deployed_views: list[dict[str, Any]] = []
        for ds in datasets:
            view_name = _relation_for(ds.get("physicalName"))
            if not view_name:
                continue
            deployed_views.append({
                "view_name": view_name,
                "view_schema": view_schema,
                "dataset_uri": ds.get("uri"),
                "dataset_physical": ds.get("physicalName"),
                "dataset_description": ds.get("description"),
                "relationship_kind": ds.get("relationshipKind"),
            })
            sql = f'SELECT * FROM {quote_created_relation(view_schema, view_name, platform_type)}'
            res = sql_executor.execute_select(
                neo4j_session=ns,
                project_code=project.project_code,
                pg_connection=source_dsn,
                sql=sql,
                executed_by=f"reflection:{project.project_code}",
                max_rows=per_dataset_limit,
                product_uri=view.get("product_uri"),
                view_schema=view_schema,
                platform=platform_type,
                connection_ref=connection_ref or None,
            )
            if res.status == "ok":
                preview_by_dataset[ds.get("uri") or view_name] = {
                    "view_name": view_name,
                    "columns": res.columns,
                    "rows": res.rows,
                    "row_count": res.row_count,
                    "truncated": res.truncated,
                }
            else:
                # Record the failure so the skill can flag it as a finding.
                preview_by_dataset[ds.get("uri") or view_name] = {
                    "view_name": view_name,
                    "error_class": res.error_class,
                    "error_message": res.error_message,
                }

        # 2) Graph snapshot (declared shape).
        snapshot_row = ns.run(_GRAPH_SNAPSHOT_QUERY, contract_id=contract_id).single()
        snapshot = dict(snapshot_row) if snapshot_row else {}

        # 3) Source-profiling stats (best-effort).
        profiling_rows = list(ns.run(_SOURCE_PROFILING_QUERY, contract_id=contract_id))
        profiling_by_col: dict[str, dict[str, Any]] = {}
        for r in profiling_rows:
            rd = dict(r)
            entry = profiling_by_col.setdefault(rd["column_uri"], {
                "column_uri": rd["column_uri"],
                "source_column_name": rd.get("source_column_name"),
            })
            metric = rd.get("metric")
            if metric:
                entry[metric] = rd.get("value")
        source_profiling = list(profiling_by_col.values())

    # Flatten the snapshot into the shape the skill expects.
    raw_datasets = snapshot.get("datasets") or []
    declared_columns: list[dict] = []
    declared_rules: list[dict] = []
    declared_transforms: list[dict] = []
    for ds in raw_datasets:
        if not isinstance(ds, dict):
            continue
        t = _flatten_transform(ds.get("transform"))
        if t:
            declared_transforms.append({**t, "dataset_uri": ds.get("dataset_uri")})
        for col in ds.get("columns") or []:
            if not isinstance(col, dict):
                continue
            declared_columns.append({
                "column_uri": col.get("column_uri"),
                "dataset_uri": ds.get("dataset_uri"),
                "name": col.get("name"),
                "dataType": col.get("dataType"),
                "isPrimaryKey": col.get("isPrimaryKey", False),
                "description": col.get("description") or "",
                "transformHint": col.get("transformHint") or "",
            })
            for r in col.get("rules") or []:
                if not isinstance(r, dict):
                    continue
                declared_rules.append({**r, "column_uri": col.get("column_uri")})

    return {
        "project_code": project.project_code,
        "contract_id": contract_id,
        "product_uri": snapshot.get("product_uri") or view.get("product_uri"),
        "view_summary": {
            "view_schema": view_schema,
            "deployed_views": deployed_views,
        },
        "preview_rows": preview_by_dataset,
        "declared_shape": {
            "columns": declared_columns,
            "rules": declared_rules,
            "dataset_transforms": declared_transforms,
            "qa_evaluation": _flatten_qa(snapshot.get("qa")),
            "osi": snapshot.get("osi"),
            "source_profiling": source_profiling,
        },
    }


# ── Advisor (LLM) ──────────────────────────────────────────────────────────


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_advisor_payload(text: str) -> dict[str, Any]:
    """Pull the structured payload out of the skill's response.

    Mirrors the OSI advisor parser. The skill is contracted to emit ONE
    fenced json block. We grab the last one in case the model produces
    intermediate explanation that ends with the final block."""
    default = {
        "verdict": "unknown",
        "narrative": "",
        "surprises": [],
        "description_alignment": [],
        "rule_alignment": [],
        "qa_alignment": [],
        "recommendations": [],
    }
    matches = _JSON_BLOCK_RE.findall(text or "")
    if not matches:
        return default
    try:
        parsed = json.loads(matches[-1])
    except json.JSONDecodeError:
        return default
    if not isinstance(parsed, dict):
        return default

    out = dict(default)
    for k in ("verdict", "narrative"):
        v = parsed.get(k)
        if isinstance(v, str):
            out[k] = v
    for k in ("surprises", "description_alignment", "rule_alignment",
              "qa_alignment", "recommendations"):
        v = parsed.get(k)
        if isinstance(v, list):
            out[k] = [x for x in v if isinstance(x, dict)]
    return out


def _validate_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Drop any surprise/recommendation that lacks a graph URI citation.

    The skill is contracted to cite every finding. This is the gate that
    keeps the output honest — same shape OSI uses to reject hallucinated
    suggestions."""
    warnings: list[str] = []
    surprises = []
    for s in payload.get("surprises", []):
        if not (s.get("column_uri") or s.get("dataset_uri")):
            warnings.append(f"Dropped surprise without column_uri/dataset_uri: {s.get('kind', '?')}")
            continue
        surprises.append(s)
    recs = []
    for r in payload.get("recommendations", []):
        related = r.get("related_columns") or []
        if not related:
            warnings.append(f"Dropped recommendation without related_columns: {r.get('action', '?')[:60]}")
            continue
        recs.append(r)
    cleaned = dict(payload)
    cleaned["surprises"] = surprises
    cleaned["recommendations"] = recs
    return cleaned, warnings


async def run_reflector(inputs: dict[str, Any]) -> tuple[dict[str, Any], Optional[str], list[str]]:
    """Invoke the deployment-reflector skill via the Claude Code SDK.

    Returns ``(payload, error, warnings)``. On any failure the payload is
    a default shape and the error string is populated; callers persist
    the deterministic part of the report anyway."""
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
    except ImportError:
        return _parse_advisor_payload(""), "claude-agent-sdk is not installed", []

    # Truncate large inputs so the prompt stays bounded.
    preview_json = json.dumps(inputs.get("preview_rows", {}), default=str)[:_MAX_PREVIEW_JSON_CHARS]
    snapshot_json = json.dumps(inputs.get("declared_shape", {}), default=str)[:_MAX_GRAPH_SNAPSHOT_CHARS]

    system_prompt = (
        f"FIRST: Load the `{ADVISOR_SKILL}` skill via the Skill tool, then follow its "
        "instructions exactly. Compare the declared graph shape against the deployed "
        "view's preview rows. Emit ONE fenced json block with `verdict`, `narrative`, "
        "`surprises`, `description_alignment`, `rule_alignment`, `qa_alignment`, "
        "`recommendations`. Every entry in `surprises` MUST cite a `column_uri` or "
        "`dataset_uri`. Do not write files. Do not run shell commands. Do not answer "
        "in prose outside the json block."
    )

    user_prompt = (
        f"FIRST: Load the {ADVISOR_SKILL} skill using the Skill tool.\n\n"
        f"Inputs:\n"
        f"- project_code: {inputs.get('project_code')}\n"
        f"- contract_id: {inputs.get('contract_id')}\n"
        f"- view_summary: {json.dumps(inputs.get('view_summary'), default=str)}\n"
        f"- preview_rows_json: {preview_json}\n"
        f"- declared_shape: {snapshot_json}\n\n"
        "Emit one fenced ```json block following the schema in the skill. "
        "Every surprise must cite a graph URI."
    )

    from .config import BASE_DIR, PIPELINE_PLUGINS
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
                from .llm_usage import extract_usage, record_usage
                record_usage(source="deployment_reflection", usage=extract_usage(message))
                if getattr(message, "is_error", False):
                    return _parse_advisor_payload(""), "Reflector skill returned an error", []
    except Exception as e:
        return _parse_advisor_payload(""), f"Reflector skill failed: {e}", []

    payload = _parse_advisor_payload("\n".join(transcript_parts))
    cleaned, warnings = _validate_payload(payload)
    return cleaned, None, warnings


# ── Persistence ────────────────────────────────────────────────────────────


_PERSIST_QUERY = """
MATCH (dc:DataContract {id: $contract_id})
CREATE (dr:DeploymentReflection {
  uri: $uri,
  projectCode: $project_code,
  contractId: $contract_id,
  contractVersion: coalesce(dc.currentVersion, 1),
  batchId: $batch_id,
  verdict: $verdict,
  narrative: $narrative,
  surprisesJson: $surprises_json,
  descriptionAlignmentJson: $description_alignment_json,
  ruleAlignmentJson: $rule_alignment_json,
  qaAlignmentJson: $qa_alignment_json,
  recommendationsJson: $recommendations_json,
  previewDatasetCount: $preview_dataset_count,
  previewRowsSampled: $preview_rows_sampled,
  advisorError: $advisor_error,
  evaluatorVersion: $evaluator_version,
  evaluatedAt: datetime(),
  triggeredBy: $triggered_by
})
CREATE (dc)-[:HAS_DEPLOY_REFL]->(dr)
RETURN dr.uri AS uri, dr.batchId AS batch_id
"""


def persist_reflection(
    project: Project, contract_id: str, payload: dict[str, Any],
    preview_inputs: dict[str, Any], advisor_error: Optional[str],
    triggered_by: str,
) -> dict[str, Any]:
    """Append-only `:DeploymentReflection` linked to :DataContract via
    `:HAS_DEPLOY_REFL`. Mirrors :OsiEvaluation's append-only pattern."""
    batch_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    uri = f"deploy_refl:{contract_id}:{batch_id}"
    preview = preview_inputs.get("preview_rows") or {}
    rows_sampled = sum(
        len(p.get("rows", []) or []) for p in preview.values() if isinstance(p, dict)
    )

    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        row = ns.run(
            _PERSIST_QUERY,
            uri=uri,
            project_code=project.project_code,
            contract_id=contract_id,
            batch_id=batch_id,
            verdict=payload.get("verdict") or "unknown",
            narrative=payload.get("narrative") or "",
            surprises_json=json.dumps(payload.get("surprises", [])),
            description_alignment_json=json.dumps(payload.get("description_alignment", [])),
            rule_alignment_json=json.dumps(payload.get("rule_alignment", [])),
            qa_alignment_json=json.dumps(payload.get("qa_alignment", [])),
            recommendations_json=json.dumps(payload.get("recommendations", [])),
            preview_dataset_count=len(preview),
            preview_rows_sampled=rows_sampled,
            advisor_error=advisor_error,
            evaluator_version=EVALUATOR_VERSION,
            triggered_by=triggered_by,
        ).single()
        return dict(row) if row else {"uri": uri, "batch_id": batch_id}


_READ_LATEST = """
MATCH (dc:DataContract {id: $contract_id})-[:HAS_DEPLOY_REFL]->(dr:DeploymentReflection)
WITH dr ORDER BY dr.evaluatedAt DESC LIMIT 1
RETURN dr.uri              AS uri,
       dr.batchId           AS batch_id,
       dr.verdict           AS verdict,
       dr.narrative         AS narrative,
       dr.surprisesJson     AS surprises_json,
       dr.descriptionAlignmentJson AS description_alignment_json,
       dr.ruleAlignmentJson AS rule_alignment_json,
       dr.qaAlignmentJson   AS qa_alignment_json,
       dr.recommendationsJson AS recommendations_json,
       dr.previewDatasetCount AS preview_dataset_count,
       dr.previewRowsSampled  AS preview_rows_sampled,
       dr.advisorError      AS advisor_error,
       dr.evaluatorVersion  AS evaluator_version,
       toString(dr.evaluatedAt) AS evaluated_at,
       dr.triggeredBy       AS triggered_by,
       dr.contractVersion   AS contract_version
"""


def read_latest(project: Project, contract_id: str) -> Optional[dict[str, Any]]:
    """Return the most recent `:DeploymentReflection` for this contract,
    or None when none exists. Used by both the engineer panel and the
    marketplace tab."""
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        row = ns.run(_READ_LATEST, contract_id=contract_id).single()
    if not row:
        return None

    rd = dict(row)
    return {
        "uri": rd.get("uri"),
        "batch_id": rd.get("batch_id"),
        "verdict": rd.get("verdict"),
        "narrative": rd.get("narrative") or "",
        "surprises": _decode_json(rd.get("surprises_json"), []),
        "description_alignment": _decode_json(rd.get("description_alignment_json"), []),
        "rule_alignment": _decode_json(rd.get("rule_alignment_json"), []),
        "qa_alignment": _decode_json(rd.get("qa_alignment_json"), []),
        "recommendations": _decode_json(rd.get("recommendations_json"), []),
        "preview_dataset_count": rd.get("preview_dataset_count"),
        "preview_rows_sampled": rd.get("preview_rows_sampled"),
        "advisor_error": rd.get("advisor_error"),
        "evaluator_version": rd.get("evaluator_version"),
        "evaluated_at": rd.get("evaluated_at"),
        "triggered_by": rd.get("triggered_by"),
        "contract_version": rd.get("contract_version"),
    }
