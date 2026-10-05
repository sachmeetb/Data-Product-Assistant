"""Skill-based Q&A executor — Phase 3a evolution.

Takes a curated question from the latest :QAEvaluation, gathers the
product's deployed-view metadata + a small sample of rows per view,
hands them to the data-product-question-executor skill, validates the
returned SQL against an allow-list of view references, and executes it
through the same sql_executor.execute_select path Phase 1 preview uses.

Three safety layers on the LLM output (see plan §"Safety boundary"):

  1. Skill is prompted with the allow-list and refuses cleanly when
     the question requires anything outside it.
  2. Backend re-walks the SQL via sqlparse, extracts table references,
     rejects if any isn't in :ServingDefinition.deployedViewNames.
  3. sql_executor.execute_select enforces single-SELECT + statement
     timeout + row cap + audit logging via :QueryRun.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import sqlparse
from sqlparse.tokens import Name

from . import sql_executor
from .models import Project
from .neo4j_client import neo4j_session
from .sql_ident import quote_created_relation


ADVISOR_SKILL = "data-product-question-executor"
ADVISOR_TIMEOUT_SECONDS = 60
EVALUATOR_VERSION = "question-executor-v0.1.0"

SAMPLE_ROWS_PER_VIEW = 10
MAX_SAMPLE_VIEWS = 6
DEFAULT_RESULT_LIMIT = 100
MAX_SCHEMA_JSON_CHARS = 12_000
MAX_SAMPLE_JSON_CHARS = 8_000


# ── Inputs ─────────────────────────────────────────────────────────────────


_GRAPH_VIEWS_QUERY = """
MATCH (dc:DataContract {id: $contract_id})
WHERE coalesce(dc.isCurrent, true) = true
MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
MATCH (dp)-[:SERVED_BY]->(sd:ServingDefinition {servingMode: 'virtual_view'})
WITH dp, sd
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
    -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
OPTIONAL MATCH (ods)-[:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform)
WITH dp, sd,
     collect(CASE WHEN ods IS NULL THEN NULL ELSE {
       dataset_uri: ods.uri,
       dataset_name: coalesce(ods.physicalName, ods.name, ''),
       physical_name: ods.physicalName,
       description: coalesce(ods.description, ''),
       relationship_kind: coalesce(ods.relationshipKind, ''),
       grain_prose: coalesce(dt.grainProse, ''),
       filter: coalesce(dt.filterPredicate, ''),
       scd_policy_json: coalesce(dt.scdPolicyJson, ''),
       suppressed_columns_json: coalesce(dt.suppressedColumnsJson, ''),
       grouping_keys_json: coalesce(dt.groupingKeysJson, '')
     } END) AS datasets_raw
RETURN dp.uri AS product_uri,
       coalesce(sd.deployedTo, sd.viewSchema, 'public') AS view_schema,
       coalesce(sd.deployedViewNames, sd.viewNames, '[]') AS view_names_json,
       coalesce(sd.deploymentStatus, 'pending') AS deployment_status,
       sd.targetPlatform AS target_platform,
       [d IN datasets_raw WHERE d IS NOT NULL] AS datasets
"""


# Cross-platform transfer serving — no virtual view; rows were loaded into the
# TARGET platform (postgres/mysql/snowflake/databricks). Mirrors the view query's
# dataset-shape collection so the executor skill still gets grain/filter/scd
# context. The landed namespace + table names come from the serving def's
# summaryJson (authoritative — the live MaterializationTarget namespace may have
# drifted since the run); only the target connection is resolved live.
_GRAPH_TRANSFER_QUERY = """
MATCH (dc:DataContract {id: $contract_id})
WHERE coalesce(dc.isCurrent, true) = true
MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
MATCH (dp)-[:SERVED_BY]->(sd:ServingDefinition {servingMode: 'transfer_then_transform'})
WITH dp, sd
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
    -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
OPTIONAL MATCH (ods)-[:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform)
WITH dp, sd,
     collect(CASE WHEN ods IS NULL THEN NULL ELSE {
       dataset_uri: ods.uri,
       dataset_name: coalesce(ods.physicalName, ods.name, ''),
       physical_name: ods.physicalName,
       description: coalesce(ods.description, ''),
       relationship_kind: coalesce(ods.relationshipKind, ''),
       grain_prose: coalesce(dt.grainProse, ''),
       filter: coalesce(dt.filterPredicate, ''),
       scd_policy_json: coalesce(dt.scdPolicyJson, ''),
       suppressed_columns_json: coalesce(dt.suppressedColumnsJson, ''),
       grouping_keys_json: coalesce(dt.groupingKeysJson, '')
     } END) AS datasets_raw
RETURN dp.uri AS product_uri,
       coalesce(sd.deploymentStatus, 'pending') AS deployment_status,
       coalesce(sd.targetSchema, 'public') AS target_schema,
       coalesce(sd.summaryJson, '{}') AS summary_json,
       coalesce(sd.targetPlatform, 'postgres') AS target_platform,
       [d IN datasets_raw WHERE d IS NOT NULL] AS datasets
"""


# Materialized (dbt) serving — physical tables built by `dbt build`, no view DDL.
# The rows live in REAL tables named `_safe_name(physicalName)` (NO `vw_` prefix)
# in `targetSchema` on the served connection (the caller resolves it
# served-location-first, exactly as the marketplace preview does). Readiness is
# signalled by `buildStatus='built'` — there is no separate deploy step /
# deploymentStatus. Mirrors the view query's dataset-shape collection so the
# executor skill still gets grain/filter/scd context.
_GRAPH_MATERIALIZED_QUERY = """
MATCH (dc:DataContract {id: $contract_id})
WHERE coalesce(dc.isCurrent, true) = true
MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
MATCH (dp)-[:SERVED_BY]->(sd:ServingDefinition {servingMode: 'dbt_materialized'})
WITH dp, sd
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
    -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
OPTIONAL MATCH (ods)-[:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform)
WITH dp, sd,
     collect(CASE WHEN ods IS NULL THEN NULL ELSE {
       dataset_uri: ods.uri,
       dataset_name: coalesce(ods.physicalName, ods.name, ''),
       physical_name: ods.physicalName,
       description: coalesce(ods.description, ''),
       relationship_kind: coalesce(ods.relationshipKind, ''),
       grain_prose: coalesce(dt.grainProse, ''),
       filter: coalesce(dt.filterPredicate, ''),
       scd_policy_json: coalesce(dt.scdPolicyJson, ''),
       suppressed_columns_json: coalesce(dt.suppressedColumnsJson, ''),
       grouping_keys_json: coalesce(dt.groupingKeysJson, '')
     } END) AS datasets_raw
RETURN dp.uri AS product_uri,
       coalesce(sd.targetSchema, 'public') AS target_schema,
       coalesce(sd.buildStatus, 'pending') AS build_status,
       coalesce(sd.modelsJson, '[]') AS models_json,
       coalesce(sd.targetPlatform, '') AS target_platform,
       [d IN datasets_raw WHERE d IS NOT NULL] AS datasets
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


def _list_view_columns(
    connection_ref: dict,
    schema: str,
    view_names: list[str],
    platform: str = "postgres",
) -> dict[str, list[dict[str, Any]]]:
    """Return ``{view_name: [{name, data_type}, ...]}`` for the deployed views in
    ``schema``, via the platform's ``DiscoveryProvider.list_columns`` — schema
    hints work for EVERY served engine, not Postgres only. An unknown platform or
    a failed probe degrades to an empty dict rather than raising."""
    out: dict[str, list[dict[str, Any]]] = {}
    if not view_names:
        return out
    from .platform.dispatch import get_discovery_provider, UnknownPlatform
    from .platform.interfaces import NamespaceRef, RelationSummary
    try:
        provider = get_discovery_provider(platform)
    except UnknownPlatform:
        return out
    ns = NamespaceRef(platform_instance_id="", parts=[schema])
    for view in view_names:
        rel = RelationSummary(namespace=ns, name=view, relation_kind="view")
        try:
            cols = provider.list_columns(connection_ref or {}, rel)
        except Exception:
            cols = []
        if cols:
            out[view] = [{"name": c.name, "data_type": c.data_type} for c in cols]
    return out


def _safe_name(name: str) -> str:
    """Mirror generate_view_ddl.py:_safe_name / pg_resolver._safe_name exactly — a
    dbt-materialized table is named ``_safe_name(physicalName)`` in targetSchema,
    so matching model/table names back to their :DProdOutputDataset must use the
    same normalisation."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name or "").lower()


def _source_dsn(platform: str, ref: Optional[dict]) -> str:
    """Transient Postgres DSN produced from a STRUCTURED connection_ref, only at
    the moment ``execute_select`` needs one. Empty for non-Postgres (those ride
    ``connection_ref``). The password rides the returned string — ephemeral."""
    if (platform or "").lower() not in ("postgres", "postgresql"):
        return ""
    from .routers.connections import build_connection_string
    return build_connection_string("postgres", ref or {})


@dataclass
class ExecuteInputs:
    project_code: str
    contract_id: str
    product_uri: str
    view_schema: str
    deployed_view_names: list[str]  # bare names, no schema prefix
    deployed_views: list[dict[str, Any]]  # rich view metadata for the skill
    sample_rows: dict[str, list[dict[str, Any]]]  # view_name -> rows
    target_platform: str = "postgres"
    # The connection the FINAL authored SQL should run against. For virtual_view
    # this mirrors the source (the view lives on the source engine); for a
    # transfer product it points at the TARGET engine where the rows landed. The
    # endpoint reads these back so it doesn't hard-code the source connection.
    # STRUCTURED ref only — the endpoint derives a Postgres DSN transiently.
    exec_platform: str = "postgres"
    exec_connection_ref: Optional[dict] = None


def gather_inputs(
    project: Project,
    contract_id: str,
    platform_type: str = "postgres",
    connection_ref: Optional[dict] = None,
    session=None,
) -> ExecuteInputs:
    """Pre-fetch view metadata + sample rows for the executor skill.

    Three serving modes are supported. A **virtual_view** product is read on the
    SOURCE engine (the caller resolves ``platform_type`` / ``connection_ref`` for
    it). A **dbt_materialized** product has no view — its rows live in REAL tables
    (``_safe_name(physicalName)``, no ``vw_`` prefix) in ``targetSchema`` on the
    served connection the caller already resolved (served-location-first, exactly
    as the marketplace preview does); readiness is ``buildStatus='built'``. A
    **transfer** product (``transfer_then_transform``) has no view either — its
    rows landed in the TARGET engine, so we resolve the target connection live via
    ``transfer_execution._resolve_target`` (needs ``session``) and read the landed
    namespace + table names from the serving def's summaryJson.

    The returned ``exec_*`` fields tell the endpoint which connection to run the
    final authored SQL against (a Postgres DSN is derived transiently there).
    """
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        row = ns.run(_GRAPH_VIEWS_QUERY, contract_id=contract_id).single()
        materialized_row = None
        transfer_row = None
        # Fall through virtual → materialized → transfer, mirroring the
        # marketplace preview dispatcher (routers/marketplace.py:1338-1351).
        if not row:
            materialized_row = ns.run(_GRAPH_MATERIALIZED_QUERY, contract_id=contract_id).single()
        if not row and not materialized_row:
            transfer_row = ns.run(_GRAPH_TRANSFER_QUERY, contract_id=contract_id).single()
        if not row and not materialized_row and not transfer_row:
            raise RuntimeError("No serving definition for this product")

        materialized_mode = False
        if transfer_row is not None:
            # ── Transfer product: read the TARGET engine ──────────────────────
            view = dict(transfer_row)
            if view.get("deployment_status") != "deployed":
                raise RuntimeError(
                    f"Product is not deployed (deployment_status={view.get('deployment_status')}). "
                    "Run the transfer first."
                )
            from . import transfer_execution
            exec_platform, tgt_ref = transfer_execution._resolve_target(project, session)
            summary = _decode_json(view.get("summary_json"), {})
            view_schema = summary.get("target_namespace") or view.get("target_schema") or "public"
            bare_view_names = [
                str(t.get("target", "")).rsplit(".", 1)[-1]
                for t in (summary.get("tables") or [])
                if isinstance(t, dict) and t.get("target")
            ]
            bare_view_names = [v for v in bare_view_names if v]
            # One connection contract: the STRUCTURED target ref (Postgres too).
            # A Postgres DSN is derived transiently at the execute boundary.
            exec_connection_ref = tgt_ref
            target_platform = exec_platform
        elif materialized_row is not None:
            # ── Materialized (dbt) product: read the built physical tables ────
            # The rows live in REAL tables (`_safe_name(physicalName)`, no `vw_`
            # prefix) in `targetSchema`, on the served connection the caller
            # already resolved (served-location-first, same as the preview path).
            # Readiness is `buildStatus='built'` — there is no deploy step.
            materialized_mode = True
            if not connection_ref and platform_type in ("postgres", "postgresql"):
                raise RuntimeError("No source connection available")
            view = dict(materialized_row)
            if view.get("build_status") != "built":
                # "not deployed" phrasing so the endpoint maps it to the shared
                # {error: 'not_deployed'} empty state (marketplace.py:1562).
                raise RuntimeError(
                    f"Product is not deployed (build_status={view.get('build_status')}). "
                    "Run the Deploy dbt (Materialize) stage first."
                )
            view_schema = view.get("target_schema") or "public"
            models = _decode_json(view.get("models_json"), [])
            bare_view_names = [
                m.get("model") for m in models
                if isinstance(m, dict) and m.get("model")
            ]
            bare_view_names = [v for v in bare_view_names if v]
            exec_platform = platform_type
            exec_connection_ref = connection_ref or {}
            # sd.targetPlatform may be persisted empty (a known write default);
            # the resolved served platform is the reliable dialect hint.
            target_platform = view.get("target_platform") or exec_platform or "postgres"
        else:
            # ── Virtual-view product: read the SOURCE engine (unchanged) ──────
            if not connection_ref and platform_type in ("postgres", "postgresql"):
                raise RuntimeError("No source connection available")
            view = dict(row)
            if view.get("deployment_status") != "deployed":
                raise RuntimeError(
                    f"Product is not deployed (deployment_status={view.get('deployment_status')}). "
                    "Run the Deploy Virtual View stage first."
                )
            view_schema = view.get("view_schema") or "public"
            raw_view_names = _decode_json(view.get("view_names_json"), [])
            bare_view_names = [v.rsplit(".", 1)[-1] for v in raw_view_names if v]
            exec_platform = platform_type
            exec_connection_ref = connection_ref or {}
            target_platform = view.get("target_platform") or "postgres"

        # Transient Postgres DSN for the sample-row + column probes (empty for
        # non-Postgres, which ride exec_connection_ref into the executor).
        exec_source_dsn = _source_dsn(exec_platform, exec_connection_ref)

        # Fetch column metadata for every deployed view/table via the platform's
        # DiscoveryProvider (works for every served engine, not Postgres only).
        columns_by_view = _list_view_columns(
            exec_connection_ref, view_schema, bare_view_names, platform=exec_platform
        )

        # Build the rich view list for the skill prompt — pair each deployed
        # view with its DProdOutputDataset metadata (relationship_kind,
        # grain, transforms) so the skill can author shape-aware SQL.
        datasets_by_phys = {d.get("physical_name"): d for d in view.get("datasets") or []
                             if d.get("physical_name")}
        deployed_views: list[dict[str, Any]] = []
        for vn in bare_view_names:
            if materialized_mode:
                # A materialized table is named `_safe_name(physicalName)` (no
                # `vw_` prefix), so match the dataset on its safe-name.
                ds = next(
                    (d for d in view.get("datasets") or []
                     if _safe_name(d.get("physical_name") or "") == vn),
                    {},
                )
                phys = ds.get("physical_name") or vn
            else:
                # vw_<physicalName> by convention; strip the vw_ to find the
                # dataset metadata.
                phys = vn[3:] if vn.startswith("vw_") else vn
                ds = datasets_by_phys.get(phys, {})
            deployed_views.append({
                "view_schema": view_schema,
                "view_name": vn,
                "dataset_uri": ds.get("dataset_uri"),
                "dataset_name": ds.get("dataset_name") or phys,
                "physical_name": phys,
                "relationship_kind": ds.get("relationship_kind") or "unknown",
                "description": ds.get("description") or "",
                "grain_prose": ds.get("grain_prose") or "",
                "filter": ds.get("filter") or "",
                "scd_policy": _decode_json(ds.get("scd_policy_json"), None),
                "suppressed_columns": _decode_json(ds.get("suppressed_columns_json"), []),
                "grouping_keys": _decode_json(ds.get("grouping_keys_json"), []),
                "columns": columns_by_view.get(vn, []),
            })

        # Sample rows from each view (capped). The grounding helps the LLM
        # pick the right column when the schema's column names are similar.
        sample_views = bare_view_names[:MAX_SAMPLE_VIEWS]
        sample_rows: dict[str, list[dict[str, Any]]] = {}
        for vn in sample_views:
            sql = f'SELECT * FROM {quote_created_relation(view_schema, vn, exec_platform)}'
            res = sql_executor.execute_select(
                neo4j_session=ns,
                project_code=project.project_code,
                pg_connection=exec_source_dsn,
                sql=sql,
                executed_by=f"qa-execute-prep:{project.project_code}",
                max_rows=SAMPLE_ROWS_PER_VIEW,
                product_uri=view.get("product_uri"),
                view_schema=view_schema,
                platform=exec_platform,
                connection_ref=exec_connection_ref or None,
            )
            if res.status != "ok":
                continue
            col_names = [c["name"] for c in res.columns]
            sample_rows[vn] = [
                {col_names[i]: r[i] for i in range(len(col_names))}
                for r in res.rows
            ]

    return ExecuteInputs(
        project_code=project.project_code,
        contract_id=contract_id,
        product_uri=view["product_uri"],
        view_schema=view_schema,
        deployed_view_names=bare_view_names,
        deployed_views=deployed_views,
        sample_rows=sample_rows,
        target_platform=target_platform,
        exec_platform=exec_platform,
        exec_connection_ref=exec_connection_ref,
    )


# ── Advisor (LLM) ──────────────────────────────────────────────────────────


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


@dataclass
class ExecutorPayload:
    sql: Optional[str] = None
    explanation: str = ""
    aggregation_kind: str = "raw"
    confidence: str = "medium"
    refused_reason: Optional[str] = None


def _parse_executor_payload(text: str) -> ExecutorPayload:
    """Pull the structured payload out of the skill's response.
    Mirrors the OSI / reflector parsers."""
    out = ExecutorPayload()
    matches = _JSON_BLOCK_RE.findall(text or "")
    if not matches:
        return out
    try:
        parsed = json.loads(matches[-1])
    except json.JSONDecodeError:
        return out
    if not isinstance(parsed, dict):
        return out
    if isinstance(parsed.get("sql"), str):
        out.sql = parsed["sql"]
    if isinstance(parsed.get("explanation"), str):
        out.explanation = parsed["explanation"]
    if isinstance(parsed.get("aggregation_kind"), str):
        out.aggregation_kind = parsed["aggregation_kind"]
    if isinstance(parsed.get("confidence"), str):
        out.confidence = parsed["confidence"]
    if isinstance(parsed.get("refused_reason"), str):
        out.refused_reason = parsed["refused_reason"]
    return out


async def run_executor(
    *, question: dict[str, Any], inputs: ExecuteInputs,
) -> tuple[ExecutorPayload, Optional[str]]:
    """Invoke the data-product-question-executor skill via the Claude
    Code SDK. Returns ``(payload, error)``. Mirrors the reflector pattern."""
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
    except ImportError:
        return ExecutorPayload(), "claude-agent-sdk is not installed"

    payload = {
        "question": {
            "text": question.get("text") or "",
            "category": question.get("category") or "",
            "supporting_columns": question.get("supporting_columns") or [],
            "supporting_rules": question.get("supporting_rules") or [],
            "confidence": question.get("confidence") or "",
        },
        "deployed_views": inputs.deployed_views,
        "sample_rows": inputs.sample_rows,
        "dialect": inputs.target_platform or "postgres",
    }

    schema_json = json.dumps(payload, default=str)
    if len(schema_json) > MAX_SCHEMA_JSON_CHARS + MAX_SAMPLE_JSON_CHARS:
        # Trim sample rows first — schema is load-bearing.
        payload["sample_rows"] = {
            k: v[:3] for k, v in (payload.get("sample_rows") or {}).items()
        }
        schema_json = json.dumps(payload, default=str)[: MAX_SCHEMA_JSON_CHARS + MAX_SAMPLE_JSON_CHARS]

    system_prompt = (
        f"FIRST: Load the `{ADVISOR_SKILL}` skill via the Skill tool, then follow its "
        "instructions exactly. Read the question + deployed_views + sample_rows in the "
        "user message, author ONE single-SELECT statement that answers the question "
        "against the allow-listed views, and emit ONE fenced json block with `sql` + "
        "`explanation` + `aggregation_kind` + `confidence`. If the question can't be "
        "answered with the deployed views, emit `refused_reason` instead of `sql`. "
        "Do not write files. Do not run shell commands. Do not answer in prose outside "
        "the json block."
    )

    user_prompt = (
        f"FIRST: Load the {ADVISOR_SKILL} skill using the Skill tool.\n\n"
        f"```json\n{schema_json}\n```\n\n"
        "Emit one fenced ```json block with `sql` (or `refused_reason`) + `explanation` + "
        "`aggregation_kind` + `confidence`. Every table referenced in `sql` MUST be in "
        "`deployed_views[]`."
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
                record_usage(source="qa_executor", usage=extract_usage(message))
                if getattr(message, "is_error", False):
                    return ExecutorPayload(), "Executor skill returned an error"
    except Exception as e:
        return ExecutorPayload(), f"Executor skill failed: {e}"

    return _parse_executor_payload("\n".join(transcript_parts)), None


# ── Allow-list defense ─────────────────────────────────────────────────────


# Match `[schema].view_or_table` references that aren't quoted-as-identifier.
# For our purposes the skill quotes consistently, so we lean on sqlparse to
# find Name tokens and reconstruct dotted references.
@dataclass
class AllowListResult:
    ok: bool
    used_views: list[str] = field(default_factory=list)
    rejected_refs: list[str] = field(default_factory=list)


_CTE_NAME_RE = re.compile(
    # `WITH name AS (`, `WITH RECURSIVE name AS (`, `, name AS (`. The name
    # captured here is added to the allow-list dynamically so the skill can
    # author CTE-based SQL without my validator flagging the CTE reference.
    r"(?:\bWITH\s+(?:RECURSIVE\s+)?|,\s*)"
    r'("?[\w$]+"?)\s+AS\s*\(',
    re.IGNORECASE,
)


# One namespace part: a quoted or bare identifier.
_REF_PART = r'(?:"[\w$]+"|[\w$]+)'

_FROM_JOIN_RE = re.compile(
    # Match `FROM` or `JOIN`, optional whitespace, then a dot-separated
    # reference of ANY depth — `view`, `schema.view`, or a 3-level
    # `catalog.schema.view` (Databricks / Snowflake Unity Catalog), quoted or
    # bare. Capping at two parts truncated a 3-level name to its first two
    # segments and made the allow-list reject a legitimately-deployed view.
    r"\b(?:FROM|JOIN)\s+(" + _REF_PART + r"(?:\s*\.\s*" + _REF_PART + r")*)",
    re.IGNORECASE,
)


def _mask_function_parens(sql: str) -> str:
    """Replace the direct contents of function-call parentheses with
    spaces of equal length so the FROM/JOIN matcher doesn't trip on
    ``FROM`` keywords that are part of SQL function syntax
    (``EXTRACT(YEAR FROM x)``, ``SUBSTRING(x FROM 1)``,
    ``TRIM(BOTH ' ' FROM x)``, ``OVERLAY(x PLACING y FROM 1)``).

    A paren counts as a function call when it is immediately adjacent
    (no whitespace) to an identifier-ish character. That distinguishes
    function calls (``EXTRACT(`` / ``COUNT(``) from CTEs and subqueries
    (``AS (`` / ``FROM (`` — always have whitespace before the paren),
    so CTE bodies and subqueries keep their FROM/JOIN refs visible to
    the matcher. Nested subqueries inside a function paren are not
    masked: we check only the innermost paren's classification.

    Quoted identifiers and string literals are passed through intact so
    the surrounding layout is preserved.
    """
    out: list[str] = []
    paren_stack: list[bool] = []  # True = innermost paren is a function call
    in_str: str | None = None     # active quote char or None
    i = 0
    while i < len(sql):
        ch = sql[i]
        if in_str is not None:
            out.append(ch)
            if ch == in_str:
                # Postgres doubles a quote to escape it (''/"").
                if i + 1 < len(sql) and sql[i + 1] == in_str:
                    out.append(sql[i + 1])
                    i += 2
                    continue
                in_str = None
            i += 1
            continue
        if ch in ("'", '"'):
            in_str = ch
            out.append(ch)
            i += 1
            continue
        if ch == "(":
            # Use the original SQL char at i-1 (not out[-1]) so nested
            # function calls like FLOOR(EXTRACT(...)) still classify the
            # inner ( as a function. By the time we see EXTRACT's (, the
            # outer FLOOR has masked the letters of EXTRACT in `out` to
            # spaces, which would otherwise misclassify EXTRACT's paren
            # as a subquery and leak its FROM keyword through.
            prev_orig = sql[i - 1] if i > 0 else ""
            is_fn = bool(prev_orig) and (prev_orig.isalnum() or prev_orig in ("_", "$"))
            paren_stack.append(is_fn)
            out.append(ch)
            i += 1
            continue
        if ch == ")":
            if paren_stack:
                paren_stack.pop()
            out.append(ch)
            i += 1
            continue
        if paren_stack and paren_stack[-1]:
            out.append(" ")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _extract_table_refs(sql: str) -> list[str]:
    """Return every table reference following a FROM or JOIN keyword.

    Comments are stripped first via sqlparse so an SQL injection attempt
    that puts `JOIN secret` inside a `--` comment can't fool us. Then
    function-call paren contents are masked to prevent false positives
    on FROM keywords that belong to SQL function syntax — see
    ``_mask_function_parens``. Aliases that follow the reference (with
    or without ``AS``) are not captured; the regex stops at the first
    identifier-ish token.
    """
    if not sql:
        return []
    stripped = sqlparse.format(sql, strip_comments=True)
    masked = _mask_function_parens(stripped)
    return [m.group(1).strip() for m in _FROM_JOIN_RE.finditer(masked)]


def _normalise_ref(ref: str) -> tuple[str | None, str]:
    """Split a table reference into ``(container_namespace, view_name)``.

    The last dotted segment is the relation; everything before it is the
    container namespace joined back with dots — so this is namespace-depth
    agnostic and matches how ``allowed_pairs`` are built (view_schema is the
    full container, e.g. Databricks ``"workspace.default"``):
      ``"workspace"."default"."vw_x"`` → ``("workspace.default", "vw_x")``
      ``"public"."vw_x"`` / ``public.vw_x`` → ``("public", "vw_x")``
      ``vw_x`` → ``(None, "vw_x")``
    """
    parts = [p.strip().strip('"').strip("'").strip() for p in str(ref).split(".")]
    parts = [p for p in parts if p]
    if not parts:
        return None, ""
    if len(parts) == 1:
        return None, parts[0]
    return ".".join(parts[:-1]) or None, parts[-1]


def validate_sql_allowlist(
    sql: str, allowed_schema: str, allowed_views: list[str],
) -> AllowListResult:
    """Check that every FROM/JOIN table reference in ``sql`` resolves to
    a view in ``allowed_views`` or a CTE defined within the SQL itself.

    Schema is checked case-insensitively when present in the reference.
    Bare table names (no schema) are allowed since the skill quotes
    consistently and view names are project-scoped."""
    allowed_set = {v.lower() for v in allowed_views}
    # CTE names defined in this SQL are dynamically allowed.
    stripped = sqlparse.format(sql or "", strip_comments=True)
    for m in _CTE_NAME_RE.finditer(stripped):
        name = m.group(1).strip().strip('"').strip("'")
        if name:
            allowed_set.add(name.lower())
    refs = _extract_table_refs(sql)
    rejected: list[str] = []
    used: list[str] = []
    for r in refs:
        schema, view = _normalise_ref(r)
        if schema is not None and schema.lower() != allowed_schema.lower():
            rejected.append(r)
            continue
        if view.lower() not in allowed_set:
            rejected.append(r)
            continue
        used.append(view.lower())
    return AllowListResult(
        ok=(len(rejected) == 0 and len(used) > 0),
        used_views=sorted(set(used)),
        rejected_refs=rejected,
    )


def canonicalize_namespace_quoting(
    sql: str, allowed_pairs: list[tuple[str, str]],
) -> str:
    """Rewrite a multi-part namespace the LLM quoted as ONE identifier
    (``"catalog.schema"``) into per-part quoting (``"catalog"."schema"``).

    The NL→SQL skill is handed ``view_schema`` as a dotted string and sometimes
    quotes it whole — malformed for a 3-level engine (Databricks/Snowflake) both
    for the allow-list regex AND at execution (``"a.b"`` is read as a single
    identifier literally named "a.b"). We know the correct decomposition from
    ``allowed_pairs``, so this is a precise, deterministic replacement scoped to
    the exact dotted schemas on the allow-list — idempotent, and a no-op for
    already-correct or 2-level SQL. Runs BEFORE the allow-list + dialect render.
    """
    if not sql:
        return sql
    for schema, _view in allowed_pairs:
        if "." in schema:
            whole = f'"{schema}"'
            if whole in sql:
                split = ".".join(f'"{p}"' for p in schema.split("."))
                sql = sql.replace(whole, split)
    return sql


def validate_sql_allowlist_pairs(
    sql: str, allowed_pairs: list[tuple[str, str]],
) -> AllowListResult:
    """Multi-schema variant of ``validate_sql_allowlist``.

    Accepts ``allowed_pairs`` as a list of ``(schema, view_name)``
    tuples. Used by the marketplace chat where a domain may span
    multiple products' deployed views (each product's views live in
    one schema, but a domain crosses products). Existing single-schema
    callers keep using ``validate_sql_allowlist``."""
    allowed_set = {(s.lower(), v.lower()) for s, v in allowed_pairs}
    # Schema-less names allowed when their bare view name matches any
    # allowed view across schemas (the skill quotes consistently but
    # might still emit a bare reference inside a CTE body).
    bare_allowed = {v for _, v in allowed_set}
    cte_names: set[str] = set()
    stripped = sqlparse.format(sql or "", strip_comments=True)
    for m in _CTE_NAME_RE.finditer(stripped):
        name = m.group(1).strip().strip('"').strip("'")
        if name:
            cte_names.add(name.lower())

    refs = _extract_table_refs(sql)
    rejected: list[str] = []
    used: list[str] = []
    for r in refs:
        schema, view = _normalise_ref(r)
        v_lower = view.lower()
        if v_lower in cte_names:
            used.append(v_lower)
            continue
        if schema is None:
            if v_lower in bare_allowed:
                used.append(v_lower)
                continue
            rejected.append(r)
            continue
        pair = (schema.lower(), v_lower)
        if pair in allowed_set:
            used.append(v_lower)
            continue
        rejected.append(r)
    return AllowListResult(
        ok=(len(rejected) == 0 and len(used) > 0),
        used_views=sorted(set(used)),
        rejected_refs=rejected,
    )
