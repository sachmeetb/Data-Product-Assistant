"""Marketplace free-form chat — Phase 3b.

Stateless single-turn endpoint: client sends conversation history each
call, backend gathers the domain's deployed views + curated concepts +
sample rows, invokes the marketplace-product-chat-assistant skill,
validates the returned SQL against an allow-list of deployed views,
runs it through sql_executor.

Structure mirrors qa_execute.py — same skill-invocation pattern, same
three-layer safety: skill-side allow-list, backend allow-list pair-check,
sql_executor SELECT-only gate.
"""

from __future__ import annotations

import contextvars
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from . import business_concepts as bc
from . import dialect_sql
from . import sql_executor
from . import llm_usage
from .models import AppSettings, Project
from .neo4j_client import neo4j_session
from .sql_ident import quote_created_relation


# Per-request token accumulator. Each Semantic-Q&A question runs 2-3 SDK passes
# (decompose → run_chat → synthesize); the low-level loops note their usage into
# the ambient accumulator so the orchestrator can report a single per-question
# total. Set by answer_question / the HTTP handler; None outside a Q&A request.
_qa_usage: contextvars.ContextVar[Optional[llm_usage.UsageAccumulator]] = (
    contextvars.ContextVar("qa_usage", default=None)
)


def _note_usage(message: Any) -> None:
    """Add one SDK ResultMessage's usage to the ambient Q&A accumulator (no-op
    when not inside a Q&A request)."""
    acc = _qa_usage.get()
    if acc is not None:
        acc.add(llm_usage.extract_usage(message))


def begin_qa_usage() -> llm_usage.UsageAccumulator:
    """Start a per-question token-usage scope and return the accumulator. The
    SDK passes (run_chat / _run_inline_json) note into it via the contextvar;
    callers embed ``accumulator.total`` (a live dict) in their response so the
    final per-question total is reported. Safe without reset — each request runs
    in its own asyncio task context."""
    acc = llm_usage.UsageAccumulator()
    _qa_usage.set(acc)
    return acc


ADVISOR_SKILL = "marketplace-product-chat-assistant"
ADVISOR_TIMEOUT_SECONDS = 90
EVALUATOR_VERSION = "marketplace-chat-v0.1.0"

SAMPLE_ROWS_PER_VIEW = 8
MAX_SAMPLE_VIEWS = 8
DEFAULT_RESULT_LIMIT = 100
# Soft ceiling on the JSON payload sent to the skill. Above this we trim
# bulky context (sample rows → concept bindings → view columns) in priority
# order. The previous 32k value was paranoid for current models AND the
# truncation was destructive (sliced the JSON mid-string, dropping
# user_message). Now we preserve JSON validity at all costs.
_MAX_INPUT_JSON_CHARS = 180_000


# ── Inputs ─────────────────────────────────────────────────────────────────


_DOMAIN_PRODUCTS_QUERY = """
MATCH (p:Project {domain: $domain})-[:HAS_CONTRACT]->(dc:DataContract)
WHERE coalesce(dc.isCurrent, true) = true
  AND ($contract_id IS NULL OR $contract_id = '' OR dc.id = $contract_id)
MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
// Any serving — virtual_view OR dbt_materialized. Usability (deployed view vs
// built materialized table) is decided in Python so a built materialized
// product is queryable too, not just deployed virtual views. One row per
// (product, serving definition); a product may carry both.
MATCH (dp)-[:SERVED_BY]->(sd:ServingDefinition)
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
    -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
OPTIONAL MATCH (ods)-[:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform)
WITH p, dc, dp, sd,
     collect(CASE WHEN ods IS NULL THEN NULL ELSE {
       dataset_uri: ods.uri,
       dataset_name: coalesce(ods.physicalName, ods.name, ''),
       physical_name: ods.physicalName,
       description: coalesce(ods.description, ''),
       relationship_kind: coalesce(ods.relationshipKind, 'unknown'),
       grain_prose: coalesce(dt.grainProse, ''),
       filter: coalesce(dt.filterPredicate, ''),
       scd_policy_json: coalesce(dt.scdPolicyJson, ''),
       suppressed_columns_json: coalesce(dt.suppressedColumnsJson, '')
     } END) AS datasets_raw
RETURN dp.uri AS product_uri,
       p.projectCode AS project_code,
       dc.id AS contract_id,
       coalesce(dc.productKind, 'unknown') AS product_kind,
       coalesce(dp.name, dc.name, '') AS product_name,
       sd.servingMode AS serving_mode,
       coalesce(sd.deploymentStatus, 'pending') AS deployment_status,
       coalesce(sd.buildStatus, '') AS build_status,
       coalesce(sd.deployedTo, sd.viewSchema, 'public') AS view_schema,
       coalesce(sd.deployedViewNames, sd.viewNames, '[]') AS view_names_json,
       coalesce(sd.targetSchema, '') AS target_schema,
       coalesce(sd.modelsJson, '[]') AS models_json,
       coalesce(sd.summaryJson, '{}') AS summary_json,
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


def _make_view_entry(
    view_schema: str, view_name: str, physical_name: str,
    prod: dict[str, Any], ds: dict[str, Any], *,
    serving_mode: str, is_snapshot: bool, platform: str = "postgres",
) -> dict[str, Any]:
    """One ``deployed_views`` entry. Shared by the virtual-view and
    dbt-materialized branches — ``serving_mode`` / ``is_snapshot`` let the SQL
    skill treat a materialized snapshot table differently (filter to current
    rows). Columns are hydrated later by ``_hydrate_view_schema``."""
    return {
        "view_schema": view_schema,
        "view_name": view_name,
        "dataset_uri": ds.get("dataset_uri"),
        "dataset_name": ds.get("dataset_name") or physical_name,
        "physical_name": physical_name,
        "product_name": prod.get("product_name") or "",
        "product_kind": prod.get("product_kind") or "unknown",
        "contract_id": prod.get("contract_id"),
        "relationship_kind": ds.get("relationship_kind") or "unknown",
        "description": ds.get("description") or "",
        "grain_prose": ds.get("grain_prose") or "",
        "filter": ds.get("filter") or "",
        "scd_policy": _decode_json(ds.get("scd_policy_json"), None),
        "suppressed_columns": _decode_json(ds.get("suppressed_columns_json"), []),
        "serving_mode": serving_mode,
        "is_snapshot": is_snapshot,
        # Platform this view is served on (informative for the skill; the
        # execution connection is derived from the used views, not from here —
        # see ChatInputs.view_connections). NEVER put connection_ref here: this
        # dict is serialized into the skill prompt and connection_ref carries the
        # resolved password.
        "platform": platform,
        "columns": [],  # hydrated by _hydrate_view_schema
    }


def _normalize_platform(p: Optional[str]) -> str:
    """Collapse the postgres/postgresql alias so the cross-platform guard treats
    them as one engine; lower-case everything else."""
    v = (p or "").lower()
    return "postgres" if v in ("postgres", "postgresql") else v


def _source_dsn(platform: str, connection_ref: Optional[dict]) -> str:
    """Transient Postgres DSN produced from a STRUCTURED connection_ref, only at
    the moment ``execute_select`` needs one. Empty for non-Postgres platforms
    (they ride ``connection_ref`` into the executor) and for an unresolved ref.
    The password rides the returned string — ephemeral; never log or persist it."""
    if _normalize_platform(platform) != "postgres":
        return ""
    from .routers.connections import build_connection_string
    return build_connection_string("postgres", connection_ref or {})


def _same_pg_instance(a: Optional[str], b: Optional[str]) -> bool:
    """True when two Postgres DSNs point at the same instance + database (host,
    port, dbname) — ignoring credentials and query options like search_path.
    The chat fully-qualifies every table as ``"schema"."name"``, so a differing
    default search_path doesn't affect whether a materialized product's tables
    are reachable on the chat's single connection; only host/port/db matter."""
    if not a or not b:
        return True  # unknown/unset → assume reachable (source fallback case)
    from urllib.parse import urlsplit
    try:
        ua, ub = urlsplit(a), urlsplit(b)
        return (ua.hostname, ua.port, ua.path) == (ub.hostname, ub.port, ub.path)
    except Exception:
        return a == b


def _views_for_product(
    prod: dict[str, Any], source_dsn: str, mat_conn: Optional[str],
    platform: str = "postgres",
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Resolve one product's *active* serving into ``(deployed_view_entries,
    allowed_pairs)``. A deployed ``virtual_view`` wins; otherwise a built
    ``dbt_materialized`` table set — but only when its physical tables sit on the
    SAME Postgres the chat connects to (``mat_conn`` equals ``source_dsn``, or
    ``mat_conn`` is unset → source fallback). Returns ``([], [])`` when there's
    no usable serving (pending/building/none, or cross-instance materialized).
    Pure function — no I/O — so it's unit-testable. (exclusive_group means a
    product usually has just one serving, but if both exist the view wins.)"""
    datasets = prod.get("datasets") or []
    datasets_by_phys = {d.get("physical_name"): d for d in datasets if d.get("physical_name")}
    servings = prod.get("servings") or []
    views: list[dict[str, Any]] = []
    pairs: list[tuple[str, str]] = []

    virtual = next((s for s in servings
                    if s.get("serving_mode") == "virtual_view"
                    and s.get("deployment_status") == "deployed"), None)
    materialized = next((s for s in servings
                         if s.get("serving_mode") == "dbt_materialized"
                         and s.get("build_status") == "built"), None)
    transfer = next((s for s in servings
                     if s.get("serving_mode") == "transfer_then_transform"
                     and s.get("deployment_status") == "deployed"), None)

    if virtual:
        view_schema = virtual.get("view_schema") or "public"
        for vn in (v.rsplit(".", 1)[-1] for v in _decode_json(virtual.get("view_names_json"), []) if v):
            phys = vn[3:] if vn.startswith("vw_") else vn
            ds = datasets_by_phys.get(phys, {})
            views.append(_make_view_entry(view_schema, vn, phys, prod, ds,
                                          serving_mode="virtual_view", is_snapshot=False,
                                          platform=platform))
            pairs.append((view_schema, vn))
    elif materialized:
        # Cross-instance materialized (a separate target DB) isn't reachable on
        # the chat's single connection — skip it (v1 single-instance limit). A
        # differing search_path is fine (tables are fully-qualified).
        if not _same_pg_instance(mat_conn, source_dsn):
            return [], []
        target_schema = materialized.get("target_schema") or "public"
        for m in _decode_json(materialized.get("models_json"), []):
            if not isinstance(m, dict) or m.get("status") != "success":
                continue
            model_name = m.get("model")
            if not model_name:
                continue
            ds = datasets_by_phys.get(model_name, {})
            views.append(_make_view_entry(target_schema, model_name, model_name, prod, ds,
                                          serving_mode="dbt_materialized",
                                          is_snapshot=(m.get("kind") == "snapshot"),
                                          platform=platform))
            pairs.append((target_schema, model_name))
    elif transfer:
        # Cross-platform transfer: rows landed in the TARGET engine. The
        # namespace + table names come from the serving def's summaryJson (where
        # the dlt load actually landed — authoritative over a drifted
        # MaterializationTarget). `platform` here is the TARGET platform (the
        # caller resolves the target binding for a transfer product), so the
        # per-view connection points at the target engine.
        summary = _decode_json(transfer.get("summary_json"), {})
        namespace = summary.get("target_namespace") or transfer.get("target_schema") or "public"
        for t in (summary.get("tables") or []):
            if not isinstance(t, dict) or not t.get("target"):
                continue
            table = str(t["target"]).rsplit(".", 1)[-1]
            if not table:
                continue
            ds = datasets_by_phys.get(table, {})
            views.append(_make_view_entry(namespace, table, table, prod, ds,
                                          serving_mode="transfer_then_transform",
                                          is_snapshot=False, platform=platform))
            pairs.append((namespace, table))
    return views, pairs


def _list_view_columns(
    connection_ref: dict,
    schema: str,
    view_names: list[str],
    platform: str = "postgres",
) -> dict[str, list[dict[str, Any]]]:
    """Return ``{view_name: [{name, data_type}, ...]}`` for the views in
    ``schema``, via the platform's ``DiscoveryProvider.list_columns`` — so
    schema hints work for EVERY served engine, not Postgres only. An unknown
    platform or a failed probe degrades to an empty dict (the skill just gets
    no hints) rather than raising."""
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


@dataclass
class ChatInputs:
    domain: str
    scoped_contract_id: Optional[str]
    scoped_product_name: Optional[str]
    deployed_views: list[dict[str, Any]] = field(default_factory=list)
    allowed_pairs: list[tuple[str, str]] = field(default_factory=list)
    sample_rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    concepts: list[dict[str, Any]] = field(default_factory=list)
    platform_type: str = "postgres"
    # The single connection contract: platform + STRUCTURED connection_ref. A
    # Postgres DSN is derived transiently via _source_dsn at the execute_select
    # boundary — never stored here. (resolved_password inside connection_ref is
    # ephemeral — NEVER log, persist, or return in responses.)
    connection_ref: dict = field(default_factory=dict)
    product_uri: Optional[str] = None  # for :QueryRun audit when single-product
    project_code: str = ""  # owning project of the first product (sample-row audit)
    # Whether deployed_views have had their columns + sample_rows hydrated from
    # Postgres yet. Full mode hydrates eagerly in gather_inputs; concept-guided
    # defers until the view subset is resolved, then ensure_hydrated() fills it.
    hydrated: bool = False
    # Retrieval metadata (echoed to the UI). 'full' = whole-domain concept
    # dump; 'concept_guided' = embedding-retrieved subset + neighbours.
    retrieval_mode: str = "full"
    matched_concepts: list[dict[str, Any]] = field(default_factory=list)
    concept_fallback_reason: Optional[str] = None
    # Concept-first resolution trace (concept_guided only): the phrases the
    # question decomposed into, and the deployed views the matched concepts
    # resolved to. Feed the UI "Explain" panel; empty in full mode.
    decomposition_phrases: list[str] = field(default_factory=list)
    resolved_views: list[dict[str, Any]] = field(default_factory=list)
    # Value resolution (all modes): the record-level value mentions the question
    # references ("John Doe") and the VERIFIED exact literals they resolved to
    # against live data — injected into run_chat so the skill filters on the real
    # value instead of guessing. Empty for purely analytic questions. See
    # value_resolution.py.
    value_mentions: list[dict[str, Any]] = field(default_factory=list)
    grounded_values: list[dict[str, Any]] = field(default_factory=list)
    # The UNNARROWED domain view + concept sets (full mode: identical to the
    # active ones). Value resolution searches these — a record's identifying
    # column (a person name) often lives in a view the concept retrieval narrowed
    # away. allowed_pairs already stays full, so probing them is allow-list-safe.
    all_deployed_views: list[dict[str, Any]] = field(default_factory=list)
    all_concepts: list[dict[str, Any]] = field(default_factory=list)
    # Per-view execution binding, keyed by LOWERCASED bare view name →
    # {"platform", "connection_ref"}. The execution
    # platform/connection is derived from the views a query ACTUALLY references
    # (not "first product's") so an all-MySQL / all-Snowflake domain executes
    # against the right engine, and a question spanning >1 platform is rejected
    # cleanly instead of hitting the wrong connection. NOT serialized to the
    # skill — carries the ephemeral resolved_password. This is the seam Phase-2
    # cross-platform federation plugs into.
    view_connections: dict[str, dict[str, Any]] = field(default_factory=dict)


def _gather_views_and_concepts(
    settings: AppSettings, domain: str, contract_id: Optional[str] = None,
    *, retrieval_mode: str = "full",
) -> ChatInputs:
    """Cheap graph-only pass: deployed-view *skeleton* (metadata, no columns),
    the allow-list pair set, the resolved source connection, and the full
    business concept set. No Postgres round-trips — column/sample hydration is deferred
    to ``_hydrate_view_schema`` so concept-guided mode can hydrate only the
    views its matched concepts resolve to.

    Pg_connection is resolved per-product; for multi-product domain scope we use
    the first product's connection (v1 limit: all products in the same domain
    must share a Postgres instance for the chat to work end-to-end. The
    allow-list still rejects cross-product mistakes)."""
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        rows = list(ns.run(_DOMAIN_PRODUCTS_QUERY,
                            domain=domain, contract_id=contract_id))
    if not rows:
        raise RuntimeError(
            f"No deployed products found for domain={domain!r}"
            + (f", contract={contract_id!r}" if contract_id else "")
        )

    # One row per (product, serving definition) — a product can carry both a
    # virtual_view and a dbt_materialized serving. Group by product so we can
    # pick the active/usable one (prefer a deployed virtual view; else a built
    # materialized table set).
    by_product: dict[str, dict[str, Any]] = {}
    for r in (dict(x) for x in rows):
        uri = r.get("product_uri")
        prod = by_product.setdefault(uri, {
            "product_uri": uri,
            "project_code": r.get("project_code"),
            "contract_id": r.get("contract_id"),
            "product_kind": r.get("product_kind") or "unknown",
            "product_name": r.get("product_name") or "",
            "datasets": r.get("datasets") or [],
            "servings": [],
        })
        prod["servings"].append({
            "serving_mode": r.get("serving_mode"),
            "deployment_status": r.get("deployment_status") or "pending",
            "build_status": r.get("build_status") or "",
            "view_schema": r.get("view_schema") or "public",
            "view_names_json": r.get("view_names_json"),
            "target_schema": r.get("target_schema") or "",
            "models_json": r.get("models_json"),
            "summary_json": r.get("summary_json") or "",
        })
    products = list(by_product.values())

    # Resolve the chat's single source connection from the first product's
    # project, and — for materialized products — each product's materialization
    # connection so we can keep only same-instance ones. SQLModel session here
    # avoids circular imports (late import). v1 limit: all queryable products in
    # the domain must share one Postgres instance.
    from sqlmodel import Session, select as _sel
    from .database import engine as sql_engine  # shared engine; honors WB_DATABASE_URL
    from .pg_resolver import resolve_read_connection_for_consumer
    from .routers.materialization import resolve_materialization_connection
    chat_source_dsn = ""   # transient Postgres DSN for the reachability check only
    _first_platform_type = "postgres"
    _first_connection_ref: dict = {}
    mat_conn_by_uri: dict[str, Optional[str]] = {}
    # Per-product resolved binding — {product_uri: {platform, connection_ref}}.
    # The execution connection is chosen per query from the views it references
    # (change #5), so we keep every product's binding. A DSN is never stored — it
    # is derived transiently from connection_ref via _source_dsn when a driver
    # needs one.
    binding_by_uri: dict[str, dict[str, Any]] = {}
    with Session(sql_engine) as sess:
        for prod in products:
            project = sess.exec(
                _sel(Project).where(Project.project_code == prod["project_code"])
            ).first()
            if not project:
                continue
            # Detect platform type for this project's source. Use the borrowing
            # resolver: a consumer (dpe-cf) product has no SourceBinding of its
            # own — it borrows the source's connection via :CONSUMES. Without the
            # borrow a MySQL consumer wrongly resolves to a localhost Postgres
            # socket (the QA-executor path already uses this resolver).
            # A deployed cross-platform transfer product is queried on its TARGET
            # engine (where the dlt load landed), not the source. Resolve the
            # target connection so the binding — and the sample/execute path —
            # point at the right engine; fall back to source resolution on any
            # failure so a mis-configured target degrades to "no views".
            is_transfer = any(
                s.get("serving_mode") == "transfer_then_transform"
                and s.get("deployment_status") == "deployed"
                for s in (prod.get("servings") or [])
            )
            _plt = _cref = None
            if is_transfer:
                try:
                    from . import transfer_execution
                    # Structured target ref (Postgres too) — the DSN is derived
                    # transiently by _source_dsn, never stored on the binding.
                    _plt, _cref = transfer_execution._resolve_target(project, sess)
                except Exception:
                    _plt = None
            if _plt is None:
                # Served-location-first: a consumer served as a virtual view over
                # a materialized source has its own views on the borrowed source
                # target (e.g. Databricks); else Postgres origin-borrow.
                _plt, _cref, _borrowed = resolve_read_connection_for_consumer(
                    project, sess, prod["contract_id"])
            binding_by_uri[prod["product_uri"]] = {
                "platform": _plt,
                "connection_ref": _cref or {},
            }
            _prod_dsn = _source_dsn(_plt, _cref)
            if not _first_connection_ref:
                _first_platform_type = _plt
                _first_connection_ref = _cref or {}
            if not chat_source_dsn and _prod_dsn:
                chat_source_dsn = _prod_dsn
            # Where do this product's materialized physical tables live?
            try:
                mconn, _origin = resolve_materialization_connection(project, sess)
            except Exception:
                mconn = None
            mat_conn_by_uri[prod["product_uri"]] = mconn

    if not chat_source_dsn and _first_platform_type in ("postgres", "postgresql"):
        raise RuntimeError("No PostgreSQL connection available for this domain's products.")

    # Build deployed_views — one entry per queryable (view or materialized
    # table) per product. Columns hydrated later (see _hydrate_view_schema).
    # Record each view's execution binding in view_connections (keyed by
    # lowercased bare view name) so the executor picks the right engine.
    deployed_views: list[dict[str, Any]] = []
    allowed_pairs: list[tuple[str, str]] = []
    view_connections: dict[str, dict[str, Any]] = {}
    for prod in products:
        binding = binding_by_uri.get(prod["product_uri"], {
            "platform": "postgres", "connection_ref": {},
        })
        bind_dsn = _source_dsn(binding["platform"], binding["connection_ref"]) or chat_source_dsn
        views, pairs = _views_for_product(
            prod, bind_dsn,
            mat_conn_by_uri.get(prod["product_uri"]),
            platform=binding["platform"])
        deployed_views.extend(views)
        allowed_pairs.extend(pairs)
        for _schema, _vn in pairs:
            view_connections[_vn.lower()] = {
                "platform": binding["platform"],
                "connection_ref": binding["connection_ref"],
            }

    if not deployed_views:
        raise RuntimeError(
            f"No queryable (deployed or built) products for domain={domain!r}"
            + (f", contract={contract_id!r}" if contract_id else "")
        )

    # Business concepts for this domain (scoped to product when set). We
    # always gather the full set here; Concept-Guided narrowing happens in
    # the async router via apply_concept_guided_retrieval (it needs an LLM
    # decompose pass, which the sync gather path can't await).
    concepts = bc.list_concepts_for_chat(settings, domain, contract_id=contract_id)

    first_product = products[0]
    return ChatInputs(
        domain=domain,
        scoped_contract_id=contract_id,
        scoped_product_name=first_product.get("product_name") if contract_id else None,
        deployed_views=deployed_views,
        allowed_pairs=allowed_pairs,
        concepts=concepts,
        platform_type=_first_platform_type,
        connection_ref=_first_connection_ref,
        product_uri=first_product.get("product_uri"),
        project_code=first_product.get("project_code") or "",
        retrieval_mode=retrieval_mode,
        # Preserve the full sets before any concept-guided narrowing mutates the
        # active deployed_views / concepts (value resolution needs the full set).
        all_deployed_views=list(deployed_views),
        all_concepts=list(concepts),
        view_connections=view_connections,
    )


def _hydrate_view_schema(
    settings: AppSettings, inputs: ChatInputs, view_subset: list[dict[str, Any]],
) -> None:
    """Fill in ``columns`` (information_schema) + ``sample_rows`` (capped
    ``SELECT *``) for the given subset of ``inputs.deployed_views``, mutating
    the view dicts in place. The Postgres-heavy half of input gathering; scoped
    to a subset so concept-guided mode pays only for the views it resolved to."""
    if not view_subset:
        return
    # information_schema.columns — one query per (schema, names) batch.
    bare_names_by_schema: dict[str, list[str]] = {}
    for v in view_subset:
        bare_names_by_schema.setdefault(v["view_schema"], []).append(v["view_name"])
    cols_by_view: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for schema, names in bare_names_by_schema.items():
        if not names:
            continue
        per_view = _list_view_columns(
            inputs.connection_ref, schema, list(set(names)),
            platform=inputs.platform_type,
        )
        for vn, cols in per_view.items():
            cols_by_view[(schema, vn)] = cols
    for v in view_subset:
        v["columns"] = cols_by_view.get((v["view_schema"], v["view_name"]), [])

    # Sample rows — capped total to keep prompt bounded. Audited as :QueryRun.
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns_for_audit:
        for v in view_subset[:MAX_SAMPLE_VIEWS]:
            sql = f'SELECT * FROM {quote_created_relation(v["view_schema"], v["view_name"], inputs.platform_type)}'
            res = sql_executor.execute_select(
                neo4j_session=ns_for_audit,
                project_code=inputs.project_code or "marketplace",
                pg_connection=_source_dsn(inputs.platform_type, inputs.connection_ref),
                sql=sql,
                executed_by=f"marketplace-chat-prep:{inputs.domain}",
                max_rows=SAMPLE_ROWS_PER_VIEW,
                product_uri=v.get("contract_id"),
                view_schema=v["view_schema"],
                platform=inputs.platform_type,
                connection_ref=inputs.connection_ref or None,
            )
            if res.status != "ok":
                continue
            col_names = [c["name"] for c in res.columns]
            inputs.sample_rows[v["view_name"]] = [
                {col_names[i]: r[i] for i in range(len(col_names))}
                for r in res.rows
            ]


def _hydrate_columns_only(inputs: ChatInputs, views: list[dict[str, Any]]) -> None:
    """Fill ``columns`` (information_schema) for views that lack them — NO sample
    rows. Cheap (one query per schema). Used to give value resolution the full
    deployed-view set's columns without paying for sample-row hydration on every
    view in the domain."""
    pending = [v for v in (views or []) if not v.get("columns")]
    if not pending:
        return
    by_schema: dict[str, list[str]] = {}
    for v in pending:
        by_schema.setdefault(v["view_schema"], []).append(v["view_name"])
    for schema, names in by_schema.items():
        per_view = _list_view_columns(
            inputs.connection_ref, schema, list(set(names)),
            platform=inputs.platform_type,
        )
        for v in pending:
            if v["view_schema"] == schema:
                v["columns"] = per_view.get(v["view_name"], v.get("columns") or [])


def ensure_hydrated(settings: AppSettings, inputs: ChatInputs) -> None:
    """Idempotently hydrate whatever ``inputs.deployed_views`` currently holds
    (the full set in full mode, the resolved subset in concept-guided). No-op if
    already hydrated. Call right before run_chat so the LLM always gets columns
    + samples for exactly the views in scope — including the timeout/fallback
    path where concept narrowing never ran."""
    if inputs.hydrated:
        return
    _hydrate_view_schema(settings, inputs, inputs.deployed_views)
    inputs.hydrated = True


def gather_inputs(
    settings: AppSettings, domain: str, contract_id: Optional[str] = None,
    *, retrieval_mode: str = "full",
) -> ChatInputs:
    """Gather chat inputs for the domain scope. Full mode hydrates every view
    eagerly (behaviorally identical to the pre-split path); concept-guided mode
    returns the un-hydrated skeleton so the caller can narrow to a view subset
    (apply_concept_guided_retrieval) before hydrating via ensure_hydrated."""
    inputs = _gather_views_and_concepts(
        settings, domain, contract_id, retrieval_mode=retrieval_mode,
    )
    if retrieval_mode != "concept_guided":
        ensure_hydrated(settings, inputs)
    return inputs


def _views_for_concepts(
    inputs: ChatInputs, concepts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The deployed views the given concepts bind to, matched on physical name.
    Each concept carries ``table_bindings[].physical_name`` (the :DProdOutputDataset
    physicalName), which equals ``deployed_views[].physical_name`` — so no graph
    re-query is needed. Returns references to the same view dicts in
    ``inputs.deployed_views`` (preserves identity for hydration)."""
    wanted: set[str] = set()
    for c in concepts or []:
        for tb in c.get("table_bindings") or []:
            pn = (tb.get("physical_name") or "").strip().lower()
            if pn:
                wanted.add(pn)
    if not wanted:
        return []
    return [
        v for v in inputs.deployed_views
        if (v.get("physical_name") or "").strip().lower() in wanted
    ]


def _build_resolved_views(
    concepts: list[dict[str, Any]], view_subset: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Trace payload: one entry per resolved view with the concept names that
    pointed at it (attribution). ``concepts`` empty → no attribution (fallback)."""
    by_phys: dict[str, list[str]] = {}
    for c in concepts or []:
        name = c.get("name") or ""
        for tb in c.get("table_bindings") or []:
            pn = (tb.get("physical_name") or "").strip().lower()
            if pn and name:
                by_phys.setdefault(pn, []).append(name)
    out: list[dict[str, Any]] = []
    for v in view_subset:
        phys = (v.get("physical_name") or "").strip().lower()
        out.append({
            "physical_name": v.get("physical_name"),
            "view_name": v.get("view_name"),
            "product_name": v.get("product_name"),
            "product_kind": v.get("product_kind"),
            "from_concepts": sorted(set(by_phys.get(phys, []))),
        })
    return out


# ── Advisor (LLM) ──────────────────────────────────────────────────────────


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


@dataclass
class ChatPayload:
    message: str = ""
    sql: Optional[str] = None
    aggregation_kind: str = "raw"
    confidence: str = "medium"
    explanation: str = ""
    refused_reason: Optional[str] = None
    suggested_questions: list[str] = field(default_factory=list)
    concepts_used: list[dict[str, Any]] = field(default_factory=list)
    fallback_reason: Optional[str] = None


def _parse_chat_payload(text: str) -> ChatPayload:
    out = ChatPayload()
    matches = _JSON_BLOCK_RE.findall(text or "")
    if not matches:
        return out
    try:
        parsed = json.loads(matches[-1])
    except json.JSONDecodeError:
        return out
    if not isinstance(parsed, dict):
        return out
    for k in ("message", "explanation", "aggregation_kind", "confidence"):
        v = parsed.get(k)
        if isinstance(v, str):
            setattr(out, k, v)
    if isinstance(parsed.get("sql"), str):
        out.sql = parsed["sql"]
    if isinstance(parsed.get("refused_reason"), str):
        out.refused_reason = parsed["refused_reason"]
    sq = parsed.get("suggested_questions")
    if isinstance(sq, list):
        out.suggested_questions = [str(s) for s in sq if isinstance(s, str)][:3]
    cu = parsed.get("concepts_used")
    if isinstance(cu, list):
        cleaned: list[dict[str, Any]] = []
        for entry in cu:
            if not isinstance(entry, dict):
                continue
            cb_raw = entry.get("columns_bound") or []
            columns_bound: list[dict[str, str]] = []
            for cb in cb_raw:
                if isinstance(cb, str):
                    # Back-compat: skill emitted plain strings before fallback ranking landed.
                    columns_bound.append({"ref": cb, "product_kind": "unknown"})
                elif isinstance(cb, dict):
                    ref = str(cb.get("ref") or cb.get("column") or "")
                    if not ref:
                        continue
                    columns_bound.append({
                        "ref": ref,
                        "product_kind": str(cb.get("product_kind") or "unknown"),
                    })
            cleaned.append({
                "concept_uri": str(entry.get("concept_uri") or ""),
                "concept_name": str(entry.get("concept_name") or ""),
                "values_used": [str(v) for v in (entry.get("values_used") or []) if isinstance(v, (str, int, float))],
                "columns_bound": columns_bound,
                "role": str(entry.get("role") or ""),
            })
        out.concepts_used = cleaned
    fr = parsed.get("fallback_reason")
    if isinstance(fr, str) and fr.strip():
        out.fallback_reason = fr.strip()
    return out


# ── Derivation fallback ────────────────────────────────────────────────────


_IDENT_RE = re.compile(r'"([^"]+)"|\b([a-zA-Z_][a-zA-Z0-9_]*)\b')
_STRING_LITERAL_RE = re.compile(r"'([^']*)'")


def derive_concepts_used(sql: str, concepts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Approximate which concepts a SQL statement touched by intersecting
    identifier/literal tokens with each concept's ``represented_by_columns``
    + value ``value_token`` bindings. Used as a fallback when the skill
    omits ``concepts_used`` — won't surface concepts the skill considered
    but didn't bind to a column."""
    if not sql or not concepts:
        return []
    idents: set[str] = set()
    for m in _IDENT_RE.finditer(sql):
        idents.add((m.group(1) or m.group(2) or "").lower())
    literals = {m.group(1).lower() for m in _STRING_LITERAL_RE.finditer(sql)}

    # Entity-shaped payload: each concept is an entity with attributes[],
    # each attribute carrying represented_by_columns + values. An entity is
    # "used" when any of its attributes' columns/values appear in the SQL.
    derived: list[dict[str, Any]] = []
    for entity in concepts:
        bound: list[dict[str, str]] = []
        values_used: list[str] = []
        for attr in entity.get("attributes") or []:
            for rep in attr.get("represented_by_columns") or []:
                cn = (rep.get("column_name") or "").lower()
                if cn and cn in idents:
                    bound.append({"ref": rep["column_name"], "product_kind": "unknown"})
            for value in attr.get("values") or []:
                tok = (value.get("value_token") or "").lower()
                if tok and tok in literals:
                    values_used.append(value["value_token"])
        if not bound and not values_used:
            continue
        derived.append({
            "concept_uri": entity.get("uri") or "",
            "concept_name": entity.get("name") or "",
            "values_used": values_used,
            "columns_bound": bound,
            "role": "filter" if values_used else "select",
        })
    return derived


async def run_chat(
    *,
    inputs: ChatInputs,
    conversation: list[dict[str, str]],
    user_message: str,
    dialect: str = "postgres",
) -> tuple[ChatPayload, Optional[str]]:
    """Invoke the marketplace-product-chat-assistant skill via the SDK.
    Mirrors qa_execute.run_executor."""
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
    except ImportError:
        return ChatPayload(), "claude-agent-sdk is not installed"

    # Put critical fields first in the JSON so they stay legible even when
    # truncated by some intermediary. user_message + scope must never be
    # dropped — they're the question and the answer's allow-list.
    payload = {
        "user_message": user_message,
        "domain": inputs.domain,
        "scoped_product": (
            {"contract_id": inputs.scoped_contract_id, "name": inputs.scoped_product_name}
            if inputs.scoped_contract_id else None
        ),
        "dialect": dialect,
        "conversation": conversation or [],
        "deployed_views": inputs.deployed_views,
        "concepts": inputs.concepts,
        "sample_rows": inputs.sample_rows,
        # VERIFIED exact cell values resolved against live data for record-level
        # mentions ("John Doe" → employee_name = 'John Doe'). The skill MUST filter
        # on these verbatim instead of guessing. Empty for analytic questions.
        "grounded_values": inputs.grounded_values,
    }
    payload_json = json.dumps(payload, default=str)

    # If oversized, trim bulky context in priority order. Never slice the
    # JSON string itself — that would silently drop fields and break parse.
    if len(payload_json) > _MAX_INPUT_JSON_CHARS:
        # 1. sample_rows: cap rows-per-view to 3 (from default 8)
        payload["sample_rows"] = {
            k: v[:3] for k, v in (payload.get("sample_rows") or {}).items()
        }
        payload_json = json.dumps(payload, default=str)
    if len(payload_json) > _MAX_INPUT_JSON_CHARS:
        # 2. sample_rows: drop entirely
        payload["sample_rows"] = {}
        payload_json = json.dumps(payload, default=str)
    if len(payload_json) > _MAX_INPUT_JSON_CHARS:
        # 3. entity concepts: cap each attribute's column bindings to 3
        for c in payload.get("concepts") or []:
            for attr in c.get("attributes") or []:
                reps = attr.get("represented_by_columns") or []
                if len(reps) > 3:
                    attr["represented_by_columns"] = reps[:3]
        payload_json = json.dumps(payload, default=str)
    if len(payload_json) > _MAX_INPUT_JSON_CHARS:
        # 4. deployed_views[].columns: cap to first 25 each (PKs already
        # tagged so the skill keeps enough signal to pick views)
        for v in payload.get("deployed_views") or []:
            cols = v.get("columns") or []
            if len(cols) > 25:
                v["columns"] = cols[:25]
        payload_json = json.dumps(payload, default=str)

    system_prompt = (
        f"FIRST: Load the `{ADVISOR_SKILL}` skill via the Skill tool, then follow its "
        "instructions exactly. Read the domain + deployed_views + concepts + conversation in "
        "the user message, answer the user_message with ONE single-SELECT statement against "
        "allow-listed views (or refuse), and emit ONE fenced json block with `message` + "
        "`sql` (or `refused_reason`) + `aggregation_kind` + `confidence` + `explanation` + "
        "`suggested_questions`. `concepts` is an ENTITY model: each entry is a business "
        "entity with `table_bindings` (the deployed view(s) it maps to), `attributes` "
        "(each with `represented_by_columns` + categorical `values`), and `relationships` "
        "(typed `:RELATES_TO` edges to other entities). Use `table_bindings` to pick FROM "
        "tables, `attributes.represented_by_columns` to resolve which column a question term "
        "means, `values.value_token` for filter literals, and `relationships` as the JOIN "
        "graph between entities. A relationship that has a `via_column` points at a SHARED "
        "reference concept (e.g. Country): to filter by one of that concept's values, do NOT "
        "join — apply the matching value's `value_token` to the owning entity's `via_column` "
        "(e.g. WHERE <owner_table>.<via_column> = '<value_token>'); the shared concept's "
        "`attributes[].values` carry the human label (name) ↔ token mapping. Each "
        "deployed_view also carries `serving_mode` ('virtual_view' or 'dbt_materialized') and "
        "`is_snapshot`: when `is_snapshot` is true the view is a dbt SCD2 snapshot table that "
        "holds ALL historical versions (plus dbt_valid_from/dbt_valid_to/dbt_scd_id columns), "
        "so for a current-state question you MUST filter to the live rows — `WHERE is_current "
        "= true` if that column exists, else `WHERE dbt_valid_to IS NULL` — otherwise an "
        "aggregate double-counts history; only skip the filter when the user explicitly asks "
        "for history/over-time. When `grounded_values` is non-empty, each entry is a VERIFIED "
        "exact cell value resolved against live data for a record the user named — filter with "
        "it verbatim (`WHERE \"<view_name>\".\"<column_name>\" = '<value>'`); do NOT fuzzy-match, "
        "ILIKE, or alter the literal, and do NOT invent your own value for that mention. Do not "
        "write files. Do not run shell commands. Do not answer in prose outside the json block."
    )

    user_prompt = (
        f"FIRST: Load the {ADVISOR_SKILL} skill using the Skill tool.\n\n"
        f"```json\n{payload_json}\n```\n\n"
        "Emit one fenced ```json block with `message` + `sql` (or `refused_reason`) + the "
        "other fields. Every table referenced in `sql` MUST be in `deployed_views[]`."
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
                _note_usage(message)
                if getattr(message, "is_error", False):
                    return ChatPayload(), "Chat skill returned an error"
    except Exception as e:
        return ChatPayload(), f"Chat skill failed: {e}"

    return _parse_chat_payload("\n".join(transcript_parts)), None


# ── Inline SDK passes (no external skill) ────────────────────────────────────
#
# decompose_question + synthesize_answer are small, self-contained LLM passes
# that carry their full contract in the system prompt (no Skill load needed).
# Both mirror run_chat's invocation shape.


async def _run_inline_json(system_append: str, user_prompt: str, max_turns: int = 2) -> Optional[str]:
    """Run a one-shot inline LLM pass; return the transcript text or None on
    any failure (SDK missing, error, exception). Callers degrade gracefully."""
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
    except ImportError:
        return None
    from .config import BASE_DIR, PIPELINE_PLUGINS
    options = ClaudeAgentOptions(
        allowed_tools=["Read"],
        permission_mode="acceptEdits",
        cwd=str(BASE_DIR),
        max_turns=max_turns,
        skills="all",
        plugins=PIPELINE_PLUGINS,
        system_prompt={"type": "preset", "preset": "claude_code", "append": system_append},
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
                _note_usage(message)
                if getattr(message, "is_error", False):
                    return None
    except Exception:
        return None
    return "\n".join(parts)


_DECOMPOSE_SYSTEM = (
    "You analyze a data question for a Q&A system over a business domain. Return ONLY "
    "one fenced json block with two keys:\n"
    "  \"phrases\": 1 to 5 short noun phrases naming the concepts/dimensions/measures the "
    "question is about (e.g. \"customer\", \"customer segment\", \"country\", \"order total\").\n"
    "  \"value_mentions\": the specific RECORD-level values the question references that are "
    "NOT analytic dimensions — a person's name, a place, a company, an id, a product name, "
    "etc. Each entry is {\"text\": <the literal as written by the user>, \"type_hint\": <what "
    "kind of thing it is: person name|city|country|company|department|product|email|id|...>}. "
    "Use [] when the question is purely analytic (counts, trends, group-bys) with no specific "
    "named record.\n"
    "Examples: \"where does John Doe live\" → {\"phrases\": [\"employee location\"], "
    "\"value_mentions\": [{\"text\": \"John Doe\", \"type_hint\": \"person name\"}]}. "
    "\"orders by month\" → {\"phrases\": [\"order\", \"month\"], \"value_mentions\": []}. "
    "No SQL, no prose outside the json block."
)


def _clean_value_mentions(raw: Any) -> list[dict[str, str]]:
    """Normalize the LLM's ``value_mentions`` to ``[{text, type_hint}]`` (≤5)."""
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        out.append({"text": text, "type_hint": str(entry.get("type_hint") or "").strip()})
    return out[:5]


async def decompose_and_extract(
    user_message: str, conversation: list[dict[str, str]],
) -> tuple[list[str], list[dict[str, str]]]:
    """Single inline LLM pass: split the question into concept phrases AND extract
    record-level value mentions. One round-trip feeds both concept-guided
    retrieval (phrases) and value resolution (mentions). Falls back to
    ``([user_message], [])`` when the SDK is unavailable or returns nothing."""
    if not user_message.strip():
        return [], []
    convo_tail = conversation[-4:] if conversation else []
    user_prompt = (
        "Analyze this question.\n\n"
        f"```json\n{json.dumps({'user_message': user_message, 'conversation': convo_tail}, default=str)}\n```\n\n"
        "Emit one fenced ```json block: {\"phrases\": [...], \"value_mentions\": [...] }."
    )
    text = await _run_inline_json(_DECOMPOSE_SYSTEM, user_prompt, max_turns=2)
    fallback = [user_message.strip()]
    if not text:
        return fallback, []
    matches = _JSON_BLOCK_RE.findall(text)
    if not matches:
        return fallback, []
    try:
        parsed = json.loads(matches[-1])
    except json.JSONDecodeError:
        return fallback, []
    phrases = [str(p).strip() for p in (parsed.get("phrases") or []) if str(p).strip()][:5]
    mentions = _clean_value_mentions(parsed.get("value_mentions"))
    return (phrases or fallback), mentions


async def decompose_question(user_message: str, conversation: list[dict[str, str]]) -> list[str]:
    """Back-compat shim: concept phrases only (see ``decompose_and_extract``)."""
    phrases, _mentions = await decompose_and_extract(user_message, conversation)
    return phrases


async def extract_value_mentions(
    user_message: str, conversation: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Record-level value mentions only — used by `full` mode (and the
    embeddings-down concept_guided path), where no decompose pass otherwise runs.
    Caller should gate with ``value_resolution.should_resolve_values`` first."""
    _phrases, mentions = await decompose_and_extract(user_message, conversation)
    return mentions


async def apply_concept_guided_retrieval(
    settings: AppSettings, inputs: ChatInputs, user_message: str,
    conversation: list[dict[str, str]], k_per_phrase: int = 4,
) -> None:
    """Concept-first narrowing (graph + embeddings only — no Postgres). In place:
    narrows ``inputs.concepts`` to the embedding-retrieved subset (+ 1-hop
    neighbours), then narrows ``inputs.deployed_views`` to the views those
    concepts bind to (so the SQL pass sees a smaller context) while leaving
    ``inputs.allowed_pairs`` at the full domain set (FK-bridge safety — a needed
    bridge table the LLM picks is still execute-allowed). Records the decompose
    phrases + resolved views for the trace. On any miss leaves the full concept
    AND view sets untouched and records ``concept_fallback_reason``. Does NOT
    hydrate — the caller invokes ensure_hydrated() afterward."""
    narrowed = False
    if not bc.embeddings.available():
        inputs.concept_fallback_reason = "embedding model unavailable — used full concept set"
    else:
        # One inline pass yields BOTH the concept phrases (below) and the
        # record-level value mentions (consumed later by value resolution).
        inputs.decomposition_phrases, inputs.value_mentions = await decompose_and_extract(
            user_message, conversation)
        matched_uris: dict[str, dict[str, Any]] = {}
        for phrase in inputs.decomposition_phrases:
            vec = bc.embeddings.embed_query(phrase)
            if not vec:
                continue
            for hit in bc.search_concepts_by_vector(
                settings, inputs.domain, vec, k=k_per_phrase, min_score=0.0,
            ):
                uri = hit.get("uri")
                if uri and (uri not in matched_uris or hit.get("score", 0) > matched_uris[uri].get("score", 0)):
                    matched_uris[uri] = hit

        if not matched_uris:
            inputs.concept_fallback_reason = "no concept matched the question — used full concept set"
        else:
            # Hits may be entities OR attributes — resolve each up to its owning
            # entity, since the chat payload is entity-shaped (entity → attributes
            # → values). Keep the origin map so an attribute-match's score reaches
            # the entity (else those entities show a bare "match" with no score).
            origin_map = bc.resolve_entity_map(settings, list(matched_uris.keys()))
            entity_uris = list(dict.fromkeys(origin_map.values()))
            entity_score: dict[str, float] = {}
            for hit_uri, ent_uri in origin_map.items():
                sc = matched_uris.get(hit_uri, {}).get("score")
                if isinstance(sc, (int, float)):
                    entity_score[ent_uri] = max(entity_score.get(ent_uri, sc), sc)
            if not entity_uris:
                inputs.concept_fallback_reason = "matched concepts resolved to no entity — used full concept set"
            else:
                # 1-hop related entities (the join graph) widen the context.
                neighbor_uris = bc.expand_neighbors(settings, entity_uris)
                all_uris = list(dict.fromkeys(entity_uris + neighbor_uris))
                subset = bc.list_concepts_for_chat_by_uris(
                    settings, inputs.domain, all_uris, contract_id=inputs.scoped_contract_id,
                )
                if not subset:
                    inputs.concept_fallback_reason = "matched entities had no chat payload — used full concept set"
                else:
                    inputs.concepts = subset
                    matched_set = set(entity_uris)
                    # Carry the cosine similarity onto each matched entity (via
                    # entity_score, which propagates an attribute-match's score up
                    # to its owning entity) so the UI shows a number rather than a
                    # bare "match" tag. Neighbours have no direct score → None.
                    inputs.matched_concepts = (
                        [{"uri": e["uri"], "name": e.get("name"), "via": "match",
                          "score": entity_score.get(e["uri"])}
                         for e in subset if e["uri"] in matched_set]
                        + [{"uri": e["uri"], "name": e.get("name"), "via": "neighbor",
                            "score": None}
                           for e in subset if e["uri"] not in matched_set]
                    )
                    narrowed = True

    # Resolve the deployed views the (narrowed) concepts bind to and narrow the
    # LLM context to them. allowed_pairs stays full so a needed bridge table is
    # never rejected at execute. On fallback or no view match, keep all views.
    if narrowed:
        # Only narrow when the DIRECTLY-matched entities (not the 1-hop
        # neighbours) bind to at least one deployed view. If the matched concepts
        # map to NO deployed view — their product isn't deployed/queryable, or
        # they're mis-bound — narrowing to whatever tangential neighbour view
        # remains makes the skill confidently refuse against the wrong view (the
        # "only vw_location is deployed" bug). In that case keep the FULL deployed
        # set so any answer/refusal reasons over everything actually queryable.
        matched_uris = {mc["uri"] for mc in (inputs.matched_concepts or [])
                        if mc.get("via") == "match"}
        primary_concepts = [c for c in inputs.concepts if c.get("uri") in matched_uris]
        primary_views = _views_for_concepts(inputs, primary_concepts)
        if not primary_views:
            inputs.concept_fallback_reason = (
                inputs.concept_fallback_reason
                or "matched concepts bind to no deployed view (their product isn't "
                   "deployed or is mis-bound) — used the full deployed-view set"
            )
            inputs.resolved_views = _build_resolved_views([], inputs.deployed_views)
        else:
            view_subset = _views_for_concepts(inputs, inputs.concepts)
            inputs.resolved_views = _build_resolved_views(inputs.concepts, view_subset)
            inputs.deployed_views = view_subset
    else:
        inputs.resolved_views = _build_resolved_views([], inputs.deployed_views)


# ── Answer synthesis + chart (second LLM pass over executed rows) ────────────


@dataclass
class SynthPayload:
    answer_markdown: str = ""
    chart_spec: Optional[dict[str, Any]] = None
    chart_reason: str = ""


def _parse_synth_payload(text: str) -> SynthPayload:
    out = SynthPayload()
    matches = _JSON_BLOCK_RE.findall(text or "")
    if not matches:
        return out
    try:
        parsed = json.loads(matches[-1])
    except json.JSONDecodeError:
        return out
    if not isinstance(parsed, dict):
        return out
    if isinstance(parsed.get("answer_markdown"), str):
        out.answer_markdown = parsed["answer_markdown"].strip()
    chart = parsed.get("chart")
    if isinstance(chart, dict):
        spec = chart.get("vega_lite_spec")
        if isinstance(spec, dict) and spec:
            out.chart_spec = spec
        if isinstance(chart.get("reason"), str):
            out.chart_reason = chart["reason"].strip()
    return out


_SYNTH_SYSTEM = (
    "You write a short grounded answer to a data question using the ACTUAL query "
    "result rows provided, and optionally a Vega-Lite chart spec. Return ONLY one "
    "fenced json block: {\"answer_markdown\": \"...\", \"chart\": {\"vega_lite_spec\": {...}|null, "
    "\"reason\": \"...\"}}. Rules: answer_markdown is 1-3 sentences citing the real numbers "
    "from the rows (never invent values). Include a vega_lite_spec ONLY when the data suits a "
    "chart — at least 2 rows with a categorical or temporal dimension plus a numeric measure; "
    "otherwise set vega_lite_spec to null. The spec must be a valid Vega-Lite v5 object with "
    "an inline \"data\": {\"values\": [...]} drawn from the rows, an appropriate mark "
    "(bar/line/arc/point), and encodings referencing the column names exactly. Keep it minimal. "
    "No prose outside the json block."
)

_SYNTH_MAX_ROWS = 50


async def synthesize_answer(
    *, user_message: str, columns: list[dict[str, Any]], rows: list[list[Any]],
    aggregation_kind: str = "raw", dialect: str = "postgres",
) -> SynthPayload:
    """Second LLM pass: turn the executed result into a grounded textual answer
    + optional Vega-Lite chart. Returns an empty payload on any failure (the
    caller still has the table + the first-pass message)."""
    if not columns or not rows:
        return SynthPayload()
    col_names = [c.get("name") for c in columns]
    # Row dicts are friendlier for the model + for inline Vega-Lite data.
    row_dicts = [
        {col_names[i]: (r[i] if i < len(r) else None) for i in range(len(col_names))}
        for r in rows[:_SYNTH_MAX_ROWS]
    ]
    payload = {
        "user_message": user_message,
        "aggregation_kind": aggregation_kind,
        "dialect": dialect,
        "columns": columns,
        "row_count": len(rows),
        "rows_truncated": len(rows) > _SYNTH_MAX_ROWS,
        "rows": row_dicts,
    }
    user_prompt = (
        "Answer the question from these result rows and optionally chart them.\n\n"
        f"```json\n{json.dumps(payload, default=str)}\n```\n\n"
        "Emit one fenced ```json block with `answer_markdown` + `chart`."
    )
    text = await _run_inline_json(_SYNTH_SYSTEM, user_prompt, max_turns=2)
    if not text:
        return SynthPayload()
    return _parse_synth_payload(text)


# ── Conversational router (the agent "above" the retrieval modes) ─────────────
#
# A deliberate per-turn pass grounded ONLY on the domain ontology (concepts /
# attributes / values — no data products). It decides whether the latest turn
# should QUERY the semantic layer, CLARIFY (ask a follow-up), REJECT (out of
# domain / unanswerable by the ontology), or CHAT (meta / smalltalk). When it
# decides to query, it hands a fully-resolved question to answer_question(); the
# existing NL→SQL pipeline is treated as a tool the orchestrator invokes only
# once it has enough. Clarification is a normal chat turn — no blocking modal.

ROUTER_SKILL = "semantic-qa-conversation-router"
_ROUTER_ACTIONS = {"query", "clarify", "reject", "chat"}


@dataclass
class RouterDecision:
    action: str = "query"
    reply: str = ""  # assistant text for clarify / reject / chat
    resolved_question: str = ""  # fully-specified question for the query pass
    clarification_options: list[str] = field(default_factory=list)
    grounding: dict[str, Any] = field(default_factory=dict)  # {matched_concepts, notes}
    session_context_update: dict[str, Any] = field(default_factory=dict)


def _parse_router_decision(text: str) -> Optional[RouterDecision]:
    matches = _JSON_BLOCK_RE.findall(text or "")
    if not matches:
        return None
    try:
        parsed = json.loads(matches[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    action = str(parsed.get("action") or "").strip().lower()
    if action not in _ROUTER_ACTIONS:
        return None
    out = RouterDecision(action=action)
    if isinstance(parsed.get("reply"), str):
        out.reply = parsed["reply"].strip()
    if isinstance(parsed.get("resolved_question"), str):
        out.resolved_question = parsed["resolved_question"].strip()
    co = parsed.get("clarification_options")
    if isinstance(co, list):
        out.clarification_options = [str(c).strip() for c in co if str(c).strip()][:4]
    g = parsed.get("grounding")
    if isinstance(g, dict):
        mc_raw = g.get("matched_concepts")
        matched = [str(m).strip() for m in mc_raw if str(m).strip()] if isinstance(mc_raw, list) else []
        out.grounding = {"matched_concepts": matched, "notes": str(g.get("notes") or "").strip()}
    scu = parsed.get("session_context_update")
    if isinstance(scu, dict):
        out.session_context_update = scu
    return out


def _heuristic_route(user_message: str, ontology: list[dict[str, Any]]) -> RouterDecision:
    """Fallback when the router skill/SDK is unavailable. Conservative: default
    to QUERY so behaviour degrades to today's always-query path (never silently
    blocks a real question). Only the most obvious greetings route to CHAT."""
    msg = (user_message or "").strip()
    low = msg.lower()
    greetings = {"hi", "hello", "hey", "thanks", "thank you", "ok", "okay"}
    if low in greetings or (len(low) <= 4 and low.isalpha()):
        return RouterDecision(
            action="chat",
            reply="Hi! Ask me a question about this domain's data and I'll look it up.",
            grounding={"matched_concepts": [], "notes": "heuristic: greeting"},
        )
    names: list[str] = []
    for e in ontology or []:
        if e.get("name"):
            names.append(str(e["name"]))
        for a in e.get("attributes") or []:
            if a.get("name"):
                names.append(str(a["name"]))
    matched = [n for n in names if n and n.lower() in low][:6]
    return RouterDecision(
        action="query",
        resolved_question=msg,
        grounding={"matched_concepts": matched, "notes": "heuristic: routed to query"},
    )


_ROUTER_SYSTEM = (
    f"FIRST: Load the `{ROUTER_SKILL}` skill via the Skill tool, then follow it exactly. "
    "You are the conversational orchestrator for a domain's Semantic Q&A. You are grounded "
    "ONLY on the domain ONTOLOGY provided in the user message (business entities → attributes "
    "→ values + entity relationships); you do NOT see data products, tables, or columns. "
    "Read `session_context` (what's been established so far) and the recent `conversation` to "
    "resolve references in the latest `user_message`. Decide ONE action: `query` (the turn is "
    "a complete, in-domain data request — emit a fully self-contained `resolved_question` that "
    "folds in any context the user relied on from prior turns), `clarify` (in-domain but "
    "ambiguous or under-specified — ask ONE concise follow-up in `reply`, optionally with "
    "`clarification_options`), `reject` (not answerable from this ontology / out of domain — "
    "explain briefly in `reply`, grounded in what the domain DOES cover), or `chat` (greeting / "
    "meta — answer in `reply`). Emit ONE fenced ```json block with `action`, `reply`, "
    "`resolved_question`, `clarification_options`, `grounding` {matched_concepts:[names], notes}, "
    "and `session_context_update` (keys to merge into session memory). Do not write files, run "
    "shell commands, or emit prose outside the json block."
)


async def route_turn(
    settings: AppSettings, domain: str, user_message: str,
    conversation: list[dict[str, str]], session_context: dict[str, Any],
) -> tuple[RouterDecision, list[dict[str, Any]]]:
    """Run the ontology-grounded router pass. Returns (decision, ontology). On any
    SDK failure falls back to ``_heuristic_route`` so the conversation never dead-ends."""
    try:
        ontology = bc.ontology_for_grounding(settings, domain)
    except Exception:
        ontology = []
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
    except ImportError:
        return _heuristic_route(user_message, ontology), ontology

    payload = {
        "user_message": user_message,
        "domain": domain,
        "conversation": (conversation or [])[-6:],
        "session_context": session_context or {},
        "ontology": ontology,
    }
    user_prompt = (
        f"FIRST: Load the {ROUTER_SKILL} skill using the Skill tool.\n\n"
        f"```json\n{json.dumps(payload, default=str)}\n```\n\n"
        "Emit one fenced ```json block with `action` + `reply` + `resolved_question` + "
        "`clarification_options` + `grounding` + `session_context_update`."
    )
    from .config import BASE_DIR, PIPELINE_PLUGINS
    options = ClaudeAgentOptions(
        allowed_tools=["Read", "Skill"],
        permission_mode="acceptEdits",
        cwd=str(BASE_DIR),
        max_turns=4,
        skills="all",
        plugins=PIPELINE_PLUGINS,
        system_prompt={"type": "preset", "preset": "claude_code", "append": _ROUTER_SYSTEM},
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
                _note_usage(message)
                if getattr(message, "is_error", False):
                    return _heuristic_route(user_message, ontology), ontology
    except Exception:
        return _heuristic_route(user_message, ontology), ontology

    decision = _parse_router_decision("\n".join(parts))
    if decision is None:
        return _heuristic_route(user_message, ontology), ontology
    # A query decision with no resolved_question still needs something to send.
    if decision.action == "query" and not decision.resolved_question.strip():
        decision.resolved_question = (user_message or "").strip()
    return decision, ontology


# ── Explain trace (assembled into every response) ────────────────────────────


def build_trace(
    inputs: ChatInputs, *,
    payload: Optional[ChatPayload] = None,
    used_views: Optional[list[str]] = None,
    concepts_used: Optional[list[dict[str, Any]]] = None,
    result: Any = None,
    executed_sql: Optional[str] = None,
    ground_values: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Assemble the ordered, adaptive resolution trace that drives the UI
    "Explain" panel. Concept-guided adds the decompose → match → resolve steps;
    both modes add generate_sql / execute once those have run. Pure assembly of
    values already computed — no new work. Stateless: rides the response, not
    persisted."""
    steps: list[dict[str, Any]] = []
    if inputs.retrieval_mode == "concept_guided":
        if inputs.decomposition_phrases:
            steps.append({
                "key": "decompose", "label": "Decomposed question",
                "detail": {"phrases": list(inputs.decomposition_phrases)},
            })
        steps.append({
            "key": "match_concepts", "label": "Matched concepts",
            "detail": {"concepts": inputs.matched_concepts},
        })
        steps.append({
            "key": "resolve_views", "label": "Resolved data products",
            "detail": {
                "views": inputs.resolved_views,
                "fallback_reason": inputs.concept_fallback_reason,
            },
        })
    # Which of the candidate products the SQL ACTUALLY queried, and why (the
    # concepts that product provides). Derived from used_views ∩ the deployed
    # views — no extra LLM pass. Only when we know what ran (ok / exec branches).
    if used_views:
        view_meta = {
            (v.get("view_name") or "").lower(): v for v in inputs.deployed_views
        }
        concepts_by_view = {
            (rv.get("view_name") or "").lower(): rv.get("from_concepts") or []
            for rv in inputs.resolved_views
        }
        selected: list[dict[str, Any]] = []
        for name in used_views:
            v = view_meta.get(name.lower())
            if not v:  # CTE or otherwise not a real deployed view
                continue
            selected.append({
                "view_name": v.get("view_name"),
                "product_name": v.get("product_name"),
                "product_kind": v.get("product_kind"),
                "from_concepts": concepts_by_view.get((v.get("view_name") or "").lower(), []),
            })
        steps.append({
            "key": "select_product", "label": "Queried data product",
            "detail": {
                "selected": selected,
                "candidate_count": len(inputs.resolved_views or inputs.deployed_views),
            },
        })
    # Record-level value resolution (CHESS-style): which named mentions were
    # probed against live data and the verified literals they bound to. Sits
    # between product selection and SQL generation.
    if ground_values:
        steps.append({
            "key": "ground_values", "label": "Resolved record values",
            "detail": {"mentions": ground_values},
        })
    if payload is not None:
        steps.append({
            "key": "generate_sql", "label": "Generated SQL",
            "detail": {
                "sql": payload.sql,
                "explanation": payload.explanation,
                "aggregation_kind": payload.aggregation_kind,
                "confidence": payload.confidence,
                "concepts_used": concepts_used or [],
            },
        })
    if result is not None:
        steps.append({
            "key": "execute", "label": "Executed query",
            "detail": {
                "status": getattr(result, "status", None),
                "row_count": getattr(result, "row_count", None),
                "duration_ms": getattr(result, "duration_ms", None),
                "truncated": getattr(result, "truncated", None),
                "used_views": used_views or [],
                # The native-dialect SQL that ACTUALLY ran (rendered from the
                # standard SQL in generate_sql). Differs on non-Postgres targets.
                "executed_sql": executed_sql,
            },
        })
    return {"retrieval_mode": inputs.retrieval_mode, "steps": steps}


# ── Single-call orchestration (shared by the MCP tool) ───────────────────────


async def answer_question(
    settings: AppSettings, domain: str, user_message: str, *,
    contract_id: Optional[str] = None, retrieval_mode: str = "concept_guided",
    conversation: Optional[list[dict[str, str]]] = None, limit: int = 100,
    executed_by: str = "mcp-semantic-layer",
    resolved_values: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Run the full marketplace-chat pipeline end to end and return a normalized
    dict (status ok/refused/failed + fields). Mirrors the /chat route's guards
    (allow-list + sql_executor) so callers outside the request path — the MCP
    tool — get the same safety. Best-effort synthesis."""
    import asyncio
    from . import qa_execute as qe
    from . import sql_executor
    from . import value_resolution as vr
    from sqlmodel import Session, select as _sel
    from .database import engine as _engine
    from .models import Project

    if not domain or not domain.strip():
        return {"status": "failed", "error_message": "domain is required"}
    if not user_message or not user_message.strip():
        return {"status": "failed", "error_message": "question is required"}

    retrieval_mode = "concept_guided" if retrieval_mode == "concept_guided" else "full"

    # Accumulate token usage across this question's SDK passes. `_usage.total` is a
    # live dict embedded in `meta` below, so every return branch reports the final
    # per-question total once the passes have run.
    _usage = begin_qa_usage()

    try:
        inputs = gather_inputs(settings, domain, contract_id, retrieval_mode=retrieval_mode)
    except RuntimeError as e:
        return {"status": "failed", "error_class": "no_products", "error_message": str(e)}

    if retrieval_mode == "concept_guided":
        try:
            await asyncio.wait_for(
                apply_concept_guided_retrieval(settings, inputs, user_message, conversation or []),
                timeout=ADVISOR_TIMEOUT_SECONDS + 15,
            )
        except asyncio.TimeoutError:
            inputs.concept_fallback_reason = "concept retrieval timed out — used full concept set"

    # Hydrate columns + samples for the final view set (subset in concept-guided,
    # full otherwise; the timeout path above leaves the full set, hydrated here).
    ensure_hydrated(settings, inputs)

    # ── Instance/value resolution (ephemeral; all modes) ──────────────────────
    # Resolve record-level value mentions ("John Doe") against live data and bind
    # the VERIFIED literal for the NL→SQL skill, or short-circuit with a
    # disambiguation prompt. Skipped entirely when the caller echoed back the
    # user's pick via `resolved_values` (deterministic re-submit).
    ground_trace: Optional[list[dict[str, Any]]] = None
    inputs.grounded_values = []
    # Value resolution probes the FULL view set — make sure its columns are
    # hydrated (the concept-guided path only hydrated the narrowed subset).
    if resolved_values or inputs.value_mentions or (
        not resolved_values and vr.should_resolve_values(user_message)
    ):
        try:
            _hydrate_columns_only(inputs, inputs.all_deployed_views or inputs.deployed_views)
        except Exception:
            pass
    if resolved_values:
        # Re-submit after the user picked a candidate. Entries with a non-empty
        # `value` are verified records → ground verbatim; entries with an empty
        # `value` are attribute-choice pins → re-probe just that column.
        pre_grounded = [rv for rv in resolved_values if str(rv.get("value") or "")]
        pins = [rv for rv in resolved_values if not str(rv.get("value") or "")]
        inputs.grounded_values = list(pre_grounded)
        if pins:
            try:
                outcome = vr.resolve_pins(settings, inputs, pins)
            except Exception:
                outcome = None
            if outcome is not None:
                ground_trace = outcome.trace or None
                inputs.grounded_values = list(pre_grounded) + list(outcome.grounded_values)
                if outcome.disambiguation:
                    outcome.disambiguation["already_grounded"] = inputs.grounded_values
                    return {
                        "status": "needs_disambiguation",
                        "disambiguation": outcome.disambiguation,
                        "message": "", "concepts_used": [],
                        "retrieval_mode": retrieval_mode,
                        "matched_concepts": inputs.matched_concepts,
                        "concept_fallback_reason": inputs.concept_fallback_reason,
                        "concepts_in_context": len(inputs.concepts or []),
                        "token_usage": _usage.total,
                        "trace": build_trace(inputs, ground_values=ground_trace),
                    }
    else:
        if not inputs.value_mentions and vr.should_resolve_values(user_message):
            # full mode / embeddings-down: no decompose pass ran — extract now.
            try:
                inputs.value_mentions = await asyncio.wait_for(
                    extract_value_mentions(user_message, conversation or []),
                    timeout=ADVISOR_TIMEOUT_SECONDS + 15,
                )
            except (asyncio.TimeoutError, Exception):
                inputs.value_mentions = []
        if inputs.value_mentions:
            try:
                outcome = vr.resolve_mentions(settings, inputs, inputs.value_mentions)
            except Exception:
                outcome = None
            if outcome is not None:
                ground_trace = outcome.trace or None
                inputs.grounded_values = outcome.grounded_values
                if outcome.disambiguation:
                    return {
                        "status": "needs_disambiguation",
                        "disambiguation": outcome.disambiguation,
                        "message": "", "concepts_used": [],
                        "retrieval_mode": retrieval_mode,
                        "matched_concepts": inputs.matched_concepts,
                        "concept_fallback_reason": inputs.concept_fallback_reason,
                        "concepts_in_context": len(inputs.concepts or []),
                        "token_usage": _usage.total,
                        "trace": build_trace(inputs, ground_values=ground_trace),
                    }

    meta = {
        "retrieval_mode": retrieval_mode,
        "matched_concepts": inputs.matched_concepts,
        "concept_fallback_reason": inputs.concept_fallback_reason,
        "concepts_in_context": len(inputs.concepts or []),
        "token_usage": _usage.total,  # live reference — filled as passes complete
        "trace": build_trace(inputs, ground_values=ground_trace),  # base (fuller on ok/exec)
    }

    try:
        payload, advisor_error = await asyncio.wait_for(
            run_chat(inputs=inputs, conversation=conversation or [], user_message=user_message,
                     dialect=inputs.platform_type),
            timeout=ADVISOR_TIMEOUT_SECONDS + 15,
        )
    except asyncio.TimeoutError:
        return {"status": "failed", "error_class": "timeout",
                "error_message": f"chat timed out after {ADVISOR_TIMEOUT_SECONDS}s", **meta}
    if advisor_error:
        return {"status": "failed", "message": payload.message or "",
                "error_class": "advisor_error", "error_message": advisor_error, **meta}

    concepts_used = payload.concepts_used or (
        derive_concepts_used(payload.sql or "", inputs.concepts) if payload.sql else []
    )
    # Fields the skill emits regardless of branch — surfaced on every return so
    # the UI (refusal/error cards + the generate_sql trace step) has them.
    skill_fields = {
        "aggregation_kind": payload.aggregation_kind,
        "confidence": payload.confidence,
        "explanation": payload.explanation,
        "fallback_reason": payload.fallback_reason,
        "suggested_questions": payload.suggested_questions,
    }
    if payload.refused_reason:
        return {"status": "refused", "message": payload.message or "",
                "refused_reason": payload.refused_reason, "concepts_used": concepts_used,
                **skill_fields, **meta}
    if not payload.sql:
        return {"status": "failed", "message": payload.message or "", "error_class": "no_sql",
                "error_message": "Chat skill returned neither SQL nor a refusal reason.",
                "concepts_used": concepts_used, **skill_fields, **meta}

    # Deterministically fix a multi-part namespace the skill quoted whole
    # (`"catalog.schema"` → `"catalog"."schema"`) before anything downstream sees
    # it — the LLM is nondeterministic about quoting a dotted view_schema, and a
    # whole-quoted namespace is malformed for both the allow-list and execution.
    payload.sql = qe.canonicalize_namespace_quoting(payload.sql, inputs.allowed_pairs)

    allow = qe.validate_sql_allowlist_pairs(payload.sql, inputs.allowed_pairs)
    if not allow.ok:
        return {"status": "failed", "message": payload.message or "",
                "error_class": "unsafe_table", "sql": payload.sql,
                "error_message": (
                    "Chat skill referenced tables outside the deployed allow-list: "
                    f"{allow.rejected_refs}. Allowed views (schema.view): "
                    f"{[f'{s}.{v}' for s, v in inputs.allowed_pairs]}."
                ),
                "concepts_used": concepts_used, **skill_fields, **meta,
                "trace": build_trace(inputs, payload=payload, concepts_used=concepts_used,
                                     ground_values=ground_trace)}

    # Derive the execution platform/connection from the views the query ACTUALLY
    # references (change #5) — not "first product's". A question whose used views
    # span >1 platform is rejected cleanly (no crash, no wrong connection); this
    # is the seam Phase-2 cross-platform federation plugs into. CTE names + any
    # ref not in view_connections are ignored (not real deployed views).
    exec_platform = inputs.platform_type
    exec_connection_ref = inputs.connection_ref
    if inputs.view_connections:
        used_bindings = [inputs.view_connections[v] for v in allow.used_views
                         if v in inputs.view_connections]
        platforms = {_normalize_platform(b["platform"]) for b in used_bindings}
        if len(platforms) > 1:
            return {"status": "cross_platform_unsupported", "message": (
                        "This question draws on data products served on different "
                        "platforms. Ask within one platform for now."),
                    "sql": payload.sql, "used_views": allow.used_views,
                    "platforms": sorted(platforms), "concepts_used": concepts_used,
                    **skill_fields, **meta,
                    "trace": build_trace(inputs, payload=payload, used_views=allow.used_views,
                                         concepts_used=concepts_used, ground_values=ground_trace)}
        if used_bindings:
            b = used_bindings[0]
            exec_platform = b["platform"]
            exec_connection_ref = b["connection_ref"]
    # Transient Postgres DSN, produced from the structured ref only now that the
    # executor needs one (non-Postgres rides connection_ref).
    exec_source_dsn = _source_dsn(exec_platform, exec_connection_ref)

    # Render the standard/neutral SQL the skill emitted to the target engine's
    # native dialect (backticks for MySQL, dialect-specific casts/functions)
    # BEFORE execution. The allow-list above ran on the STANDARD text — its regex
    # recognises ANSI double-quoted / bare identifiers, so it must precede render.
    # Fail-open: on a parse/render error execute the original text unchanged.
    rendered = dialect_sql.render_for_platform(payload.sql, exec_platform)
    executed_sql = rendered.sql
    dialect_note = None if rendered.ok else f"dialect render skipped: {rendered.error}"

    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        result = sql_executor.execute_select(
            neo4j_session=ns, project_code=f"marketplace:{domain}",
            pg_connection=exec_source_dsn, sql=executed_sql,
            executed_by=executed_by,
            max_rows=max(1, min(int(limit or 100), sql_executor.DEFAULT_MAX_ROWS)),
            product_uri=inputs.product_uri,
            view_schema=inputs.deployed_views[0]["view_schema"] if inputs.deployed_views else None,
            platform=exec_platform,
            # Non-Postgres platforms (MySQL/Snowflake/Databricks) get their host /
            # port / credentials from connection_ref; without it the driver falls
            # back to localhost and the connection is refused. The schema-hydration
            # path already threads this — the execute path must too.
            connection_ref=exec_connection_ref or None,
        )
    if result.status != "ok":
        return {"status": "failed", "message": payload.message or "",
                "error_class": result.error_class, "sql": executed_sql,
                "standard_sql": payload.sql, "dialect_note": dialect_note,
                "error_message": result.error_message, "duration_ms": result.duration_ms,
                "concepts_used": concepts_used, **skill_fields, **meta,
                "trace": build_trace(inputs, payload=payload, used_views=allow.used_views,
                                     concepts_used=concepts_used, result=result,
                                     executed_sql=executed_sql, ground_values=ground_trace)}

    synthesized_answer, chart_spec = "", None
    try:
        synth = await asyncio.wait_for(
            synthesize_answer(user_message=user_message, columns=result.columns,
                              rows=result.rows, aggregation_kind=payload.aggregation_kind),
            timeout=ADVISOR_TIMEOUT_SECONDS + 15,
        )
        synthesized_answer, chart_spec = synth.answer_markdown, synth.chart_spec
    except (asyncio.TimeoutError, Exception):
        pass

    _ = (Session, _sel, _engine, Project)  # reserved for future project-scoped audit
    llm_usage.record_usage(
        source="semantic_qa", usage=_usage.total, domain=domain,
        contract_id=contract_id, retrieval_mode=retrieval_mode,
    )
    return {
        "status": "ok", "message": payload.message or "", "sql": executed_sql,
        "standard_sql": payload.sql, "dialect_note": dialect_note,
        "columns": result.columns, "rows": result.rows, "row_count": result.row_count,
        "truncated": result.truncated, "duration_ms": result.duration_ms,
        "used_views": allow.used_views, "concepts_used": concepts_used,
        "synthesized_answer": synthesized_answer, "chart_spec": chart_spec,
        **skill_fields, **meta,
        "trace": build_trace(inputs, payload=payload, used_views=allow.used_views,
                             concepts_used=concepts_used, result=result,
                             executed_sql=executed_sql, ground_values=ground_trace),
    }
