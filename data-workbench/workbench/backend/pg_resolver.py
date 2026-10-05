"""Resolve which database connection a project should deploy / read against.

Every project binds its source via a ``SourceBinding`` → ``PlatformConnection``
(the single connection contract). Consumer-aligned (dpe-cf) projects don't bind
a source of their own (they're defined as consumers of other products, not
direct DB readers) so we borrow the connection from the first ``:CONSUMES``'d
source. That's also where the source views were deployed, so the consumer view's
JOINs resolve naturally.

Single shared helper used by serving deploy, serving preview, marketplace
preview, and deployment-reflection. Lives at the backend package root so the
routers + the reflection engine can both import it without a circular layering
through ``routers.serving``.

Public entry points (all return STRUCTURED refs — never a ``pg_connection`` DSN
shape; a DSN is produced transiently via ``build_connection_string`` only when a
driver needs one):
  - ``resolve_source_connection_for_project`` — ``(platform, connection_ref,
    borrowed_from)``; SourceBinding first, else the recursive ``:CONSUMES`` borrow.
  - ``resolve_read_connection_for_consumer`` — served-location-first wrapper.
  - ``resolve_consumed_source_serving`` — the merged served-relation map.
  - ``ResolvedConnection`` / ``ConnectionUnresolved`` — the typed contract.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional, Tuple

from sqlmodel import Session, select

from .models import Project
from .neo4j_client import neo4j_session


# ── typed connection contract ────────────────────────────────────────────────
#
# One typed connection contract for the whole codebase. A resolved source is a
# platform + a STRUCTURED connection_ref (host/port/database/username/
# resolved_password/extra_config/default_schema). The connection_ref NEVER
# carries a legacy DSN-in-ref shape — a DSN is only ever a transient value
# produced from the structured ref (via ``build_connection_string``) at the
# moment a driver/skill needs one.


@dataclass(frozen=True)
class ResolvedConnection:
    """A resolved source connection: platform + structured ``connection_ref``.

    ``borrowed_from`` is the DIRECT upstream ``project_code`` when the connection
    was borrowed via ``:CONSUMES`` (a derived dpe-cf product), else ``None``.
    The ``resolved_password`` inside ``connection_ref`` is ephemeral — never log
    or persist it.
    """
    platform: str
    connection_ref: dict
    borrowed_from: Optional[str] = None


class ConnectionUnresolved(Exception):
    """A project has no reachable source connection.

    The explicit, typed replacement for the legacy empty-ref give-up sentinel:
    callers convert it to an actionable 4xx instead of silently proceeding with
    an empty DSN. ``borrowed_from`` records the last upstream that was walked (if
    any) so the message can point at the broken hop.
    """

    def __init__(
        self,
        message: str = "",
        *,
        project_code: Optional[str] = None,
        borrowed_from: Optional[str] = None,
    ):
        super().__init__(message or "No source connection is bound to this project.")
        self.project_code = project_code
        self.borrowed_from = borrowed_from


_FIND_CONSUMED_SOURCE_QUERY = """
MATCH (dc:DataContract {id: $contract_id})-[r:CONSUMES]->(src_dp:DProdDataProduct)
WHERE r.toVersion IS NULL
RETURN src_dp.uri AS source_dp_uri
ORDER BY src_dp.uri
LIMIT 1
"""

# Every DIRECT, currently-active upstream a consumer :CONSUMES, deterministic
# order. Used by the recursive connection borrow to walk a multi-hop chain
# (C→B→A) past intermediate consumers to the product whose data physically
# lives somewhere readable. Ordered so a multi-input consumer resolves the same
# upstream every time (the single-hop LIMIT 1 was nondeterministic).
_FIND_ACTIVE_UPSTREAMS_QUERY = """
MATCH (dc:DataContract {id: $contract_id})-[r:CONSUMES]->(:DProdDataProduct)
      <-[:MATERIALISES_AS]-(up:DataContract)
WHERE r.toVersion IS NULL
RETURN DISTINCT up.id AS upstream_contract_id
ORDER BY up.id
"""

# Serving modes where the product's data was physically moved to a distinct
# target (origin != served location). A consumer reading such a source must bind
# to the SERVED location, not the origin. Virtual-view sources are excluded: they
# stay queryable at their origin, which the origin-borrow already resolves
# correctly (a Databricks-origin virtual view resolves to Databricks via its
# SourceBinding — no served-location indirection needed).
_MATERIALIZED_SERVING_MODES = ("transfer_then_transform", "dbt_materialized")

# Walk a CONSUMER contract → its CONSUMES'd source product → the source's
# serving definition, to learn WHERE the source product's data physically lives.
# The source's tables must actually EXIST, and the "exists" signal differs by mode:
#   * transfer_then_transform — deploymentStatus='deployed' (a build-only package
#     has deploymentStatus='pending' and has loaded no rows).
#   * dbt_materialized — buildStatus='built' (a full dbt build physically creates
#     the tables; there is no separate deploy step / deploymentStatus).
# Returns EVERY deployed materialized/transfer source the consumer CONSUMES —
# NOT limited to one. A consumer that reads from multiple materialized sources
# (e.g. Employee Master + Compensation Master, both transferred to the same
# Databricks schema) must map ALL their served relations; a LIMIT 1 here mapped
# only the newest-built source and left the other's datasets to fall back to a
# non-existent `vw_<name>`, breaking deploy. Each source's datasets are collected
# in the same row so the served-map is built in one pass. Newest build first so a
# rare `_safe_name` collision resolves first-wins to the freshest source.
_FIND_CONSUMED_SOURCE_SERVING_QUERY = """
MATCH (dc:DataContract {id: $contract_id})-[r_c:CONSUMES]->(src_dp:DProdDataProduct)
      -[:SERVED_BY]->(sd:ServingDefinition)
WHERE r_c.toVersion IS NULL
  AND sd.servingMode IN $serving_modes
  AND (
        (sd.servingMode = 'transfer_then_transform' AND sd.deploymentStatus = 'deployed')
     OR (sd.servingMode = 'dbt_materialized' AND sd.buildStatus = 'built')
  )
OPTIONAL MATCH (src_dp)-[:DPROD_OUTPUT_PORT]->()-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
// Aggregate datasets per (src_dp, sd) in a WITH so `sd` stays in scope for the
// ORDER BY — an aggregating RETURN can't ORDER BY a non-projected sd.builtAt.
WITH src_dp, sd, collect(coalesce(ods.physicalName, ods.name)) AS dataset_physicals
RETURN src_dp.uri        AS source_dp_uri,
       sd.servingMode    AS serving_mode,
       sd.targetPlatform AS target_platform,
       sd.targetCatalog  AS target_catalog,
       sd.targetSchema   AS target_schema,
       sd.summaryJson    AS summary_json,
       sd.deploymentStatus AS deployment_status,
       dataset_physicals
ORDER BY sd.builtAt DESC
"""

# Per source :DProdOutputDataset physical name — the materialized table name in
# the target is `_safe_name(physicalName)` (no `vw_` prefix; dlt/dbt load real
# tables), matching generate_view_ddl.generate_lakehouse_models' model_name.
_FIND_CONSUMED_SOURCE_DATASETS_QUERY = """
MATCH (src_dp:DProdDataProduct {uri: $source_dp_uri})
      -[:DPROD_OUTPUT_PORT]->()-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
RETURN coalesce(ods.physicalName, ods.name) AS physical_name
"""


# Direct, active upstreams served as a DEPLOYED virtual view — the targeted
# multi-hop topology. Such an upstream's data lives as `vw_<safe(dataset)>`
# views in its own deployed `viewSchema`, physically on ITS read connection
# (recursively resolved). Emitting the served-relation map for these makes the
# consumer's FROM point at the upstream's ACTUAL deployed schema instead of the
# co-located same-schema guess.
_FIND_CONSUMED_VIRTUAL_SERVING_QUERY = """
MATCH (dc:DataContract {id: $contract_id})-[r_c:CONSUMES]->(src_dp:DProdDataProduct)
      -[:SERVED_BY]->(sd:ServingDefinition)
WHERE r_c.toVersion IS NULL
  AND sd.servingMode = 'virtual_view'
  AND sd.deploymentStatus = 'deployed'
OPTIONAL MATCH (src_dp)-[:DPROD_OUTPUT_PORT]->()-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
WITH src_dp, sd, collect(coalesce(ods.physicalName, ods.name)) AS dataset_physicals
RETURN src_dp.uri     AS source_dp_uri,
       sd.viewSchema  AS view_schema,
       dataset_physicals
ORDER BY sd.deployedAt DESC
"""


def _safe_name(name: str) -> str:
    """Mirror generate_view_ddl.py:_safe_name exactly — the served-relation map
    keys must match the skill's dprod-dataset lookup keys."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name or "").lower()


def connection_identity(platform: str, ref: dict) -> tuple:
    """A hashable STABLE identity for a resolved connection — enough to decide
    whether two required relations live on the SAME physical instance/database
    (a single view can't span two instances even of the same engine).

    Compares (platform, host, port, database, account, http_path) from the
    STRUCTURED ref; the last two disambiguate Snowflake accounts / Databricks
    warehouses carried in ``extra_config``.
    """
    p = (platform or "").strip().lower()
    if p in ("postgresql",):
        p = "postgres"
    ref = ref or {}
    extra = ref.get("extra_config") or {}
    return (
        p,
        str(ref.get("host") or ""),
        str(ref.get("port") or ""),
        str(ref.get("database") or ""),
        str(extra.get("account") or ""),
        str(extra.get("http_path") or extra.get("httpPath") or ""),
    )


class PlacementError(Exception):
    """A consumer's required relations don't all live on ONE execution
    connection (a chain that spans instances/engines), so no single view can
    read them. Carries the unreachable relations so the caller can tell the
    engineer to materialize the intermediate (or co-locate)."""

    def __init__(self, message: str, unreachable: list[str] | None = None):
        super().__init__(message)
        self.unreachable = unreachable or []


@dataclass
class SourceServedLocation:
    """Where a CONSUMES'd source product's data physically lives after it was
    materialized/transferred to a distinct target (e.g. Databricks Unity Catalog).

    ``relations`` maps ``_safe_name(source_dataset_physical)`` → the fully-qualified
    served relation (``"catalog.schema.relation"`` for 3-level platforms, else
    ``"schema.relation"``) — the value threaded into the view-DDL skill's
    ``--source-served-map`` so the consumer view FROMs the source's real tables.
    """
    source_project_code: str
    source_contract_id: str
    served_platform: str
    namespace: str
    connection_ref: dict
    relations: dict = field(default_factory=dict)
    deployment_status: str = ""
    serving_mode: str = ""


def _served_namespace(target_catalog: str, target_schema: str, summary_json: str) -> str:
    """Best-effort full ``catalog.schema`` (or bare ``schema``) for the served
    tables. Prefers the first-class graph properties (Stage 4); falls back to the
    ``summaryJson.target_namespace`` blob for sources materialized before those
    properties were persisted."""
    cat = (target_catalog or "").strip()
    sch = (target_schema or "").strip()
    if cat and sch:
        return f"{cat}.{sch}"
    if summary_json:
        try:
            ns = (json.loads(summary_json) or {}).get("target_namespace", "")
        except (TypeError, ValueError):
            ns = ""
        if ns:
            return str(ns).strip()
    return sch


def resolve_consumed_source_serving(
    project: Project,
    session: Session,
    contract_id: Optional[str] = None,
) -> Optional[SourceServedLocation]:
    """Where does each CONSUMES'd upstream product's data physically live?

    Builds a merged served-relation map across ALL directly-consumed upstreams
    that resolve to the SAME physical connection (stable-identity compared):
      * a MATERIALIZED/transfer upstream → its real tables at its target
        connection (namespace = target catalog.schema), and
      * a DEPLOYED VIRTUAL-VIEW upstream → its ``vw_<safe>`` views in its own
        deployed ``viewSchema``, physically on its resolved read connection
        (recursed for a consumer-of-consumer chain).
    The PRIMARY (first resolvable) upstream fixes the read connection; upstreams
    on a different physical instance are skipped (a single view can't federate).

    Returns ``None`` — the caller falls back to the recursive origin-borrow —
    when the project has no ``:CONSUMES`` edge or no upstream resolves to a
    deployed, reachable location.
    """
    _contract_id = contract_id or f"{project.project_code}-contract"

    try:
        with neo4j_session(
            project.neo4j_host, project.neo4j_port,
            project.neo4j_user, project.neo4j_password, project.neo4j_database,
        ) as ns:
            rows = list(ns.run(
                _FIND_CONSUMED_SOURCE_SERVING_QUERY,
                contract_id=_contract_id,
                serving_modes=list(_MATERIALIZED_SERVING_MODES),
            ))
    except Exception:
        rows = []
    # NB: do NOT early-return on empty `rows` — a pure virtual-view chain has no
    # materialized upstream but still resolves via the virtual-view pass below.

    from .routers.connections import resolve_materialization_target_ref

    # Merge the served relations of ALL consumed materialized sources (newest
    # first). The PRIMARY source (first resolvable one) fixes the read
    # connection; sources on a DIFFERENT physical connection are skipped (a
    # single view can't span two instances — even of the same engine), compared
    # by STABLE IDENTITY (host/db/instance), not merely platform type.
    primary: Optional[SourceServedLocation] = None
    primary_identity: Optional[tuple] = None
    merged_relations: dict = {}

    for row in rows:
        source_uri = row.get("source_dp_uri") or ""
        if not source_uri.startswith("dprod:"):
            continue
        source_contract_id = source_uri[len("dprod:"):]
        if not source_contract_id.endswith("-contract"):
            continue
        source_project_code = source_contract_id[: -len("-contract")]

        # The connection to READ the served tables is the SOURCE product's
        # registered target connection. No distinct target ⇒ served in-place ⇒
        # skip (origin-borrow handles it; not a materialized-elsewhere relation).
        target = resolve_materialization_target_ref(source_project_code, session)
        if target is None:
            continue
        served_platform, connection_ref = target
        identity = connection_identity(served_platform, connection_ref)

        if primary_identity is not None and identity != primary_identity:
            continue  # different physical instance than the primary — can't federate

        namespace = _served_namespace(
            row.get("target_catalog") or "", row.get("target_schema") or "",
            row.get("summary_json") or "",
        )

        for phys in (row.get("dataset_physicals") or []):
            if not phys:
                continue
            rel = _safe_name(phys)
            # first-wins across sources (rows are newest-first) — a duplicate
            # dataset name keeps the freshest source's location.
            merged_relations.setdefault(rel, f"{namespace}.{rel}" if namespace else rel)

        if primary is None:
            primary_identity = identity
            primary = SourceServedLocation(
                source_project_code=source_project_code,
                source_contract_id=source_contract_id,
                served_platform=served_platform,
                namespace=namespace,
                connection_ref=connection_ref,
                relations={},  # filled below
                deployment_status=row.get("deployment_status") or "",
                serving_mode=row.get("serving_mode") or "",
            )

    # ── Virtual-view-served upstreams (the multi-hop target topology) ──────────
    # An upstream served as a DEPLOYED virtual view keeps its data at
    # `vw_<safe>` in its own deployed viewSchema, on its RESOLVED read
    # connection (which for a chained consumer is itself borrowed further up).
    # Include those relations too so the consumer's FROM points at the real
    # deployed schema. Same stable-identity guard — a virtual upstream on a
    # different physical instance than the primary can't be federated into one
    # view, so it's skipped (surfaced via unresolved, not silently wrong).
    try:
        with neo4j_session(
            project.neo4j_host, project.neo4j_port,
            project.neo4j_user, project.neo4j_password, project.neo4j_database,
        ) as ns:
            vrows = list(ns.run(
                _FIND_CONSUMED_VIRTUAL_SERVING_QUERY, contract_id=_contract_id
            ))
    except Exception:
        vrows = []

    for row in vrows:
        source_uri = row.get("source_dp_uri") or ""
        if not source_uri.startswith("dprod:"):
            continue
        up_contract_id = source_uri[len("dprod:"):]
        if not up_contract_id.endswith("-contract"):
            continue
        up_code = up_contract_id[: -len("-contract")]
        up_project = session.exec(
            select(Project).where(Project.project_code == up_code)
        ).first()
        if up_project is None:
            continue

        # Where do this upstream's views physically live? Its own resolved read
        # connection (recurses the chain for a consumer-of-consumer).
        up_platform, up_ref, _ = resolve_source_connection_for_project(
            up_project, session, up_contract_id
        )
        if up_platform in ("postgres", "postgresql") and not up_ref.get("host"):
            continue  # unresolved connection — leave to the fallback / preflight
        identity = connection_identity(up_platform, up_ref)
        if primary_identity is not None and identity != primary_identity:
            continue  # different physical instance than the primary — can't federate

        view_schema = (row.get("view_schema") or "").strip()
        for phys in (row.get("dataset_physicals") or []):
            if not phys:
                continue
            rel = _safe_name(phys)
            served = f"vw_{rel}"
            merged_relations.setdefault(rel, f"{view_schema}.{served}" if view_schema else served)

        if primary is None:
            primary_identity = identity
            primary = SourceServedLocation(
                source_project_code=up_code,
                source_contract_id=up_contract_id,
                served_platform=up_platform,
                namespace=view_schema,
                connection_ref=up_ref,
                relations={},  # filled below
                deployment_status="deployed",
                serving_mode="virtual_view",
            )

    if primary is None:
        return None
    primary.relations = merged_relations
    return primary


def resolve_read_connection_for_consumer(
    project: Project,
    session: Session,
    contract_id: Optional[str] = None,
) -> Tuple[str, dict, str | None]:
    """Single choke-point for "where does a consumer READ its source from?".

    Served-location-first: if the CONSUMES'd source was materialized to a distinct
    target, return that ``(platform, connection_ref, source_project_code)``; else
    fall back to the origin-borrow (``resolve_source_connection_for_project``).
    Same return shape as the origin-borrow so callers stay uniform.
    """
    loc = resolve_consumed_source_serving(project, session, contract_id)
    if loc is not None:
        return loc.served_platform, loc.connection_ref, loc.source_project_code
    return resolve_source_connection_for_project(
        project, session, contract_id or f"{project.project_code}-contract"
    )


def resolve_source_connection_for_project(
    project: Project,
    session: Session,
    contract_id: str | None = None,
    _seen: set | None = None,
) -> Tuple[str, dict, str | None]:
    """Resolve ``(platform_type, connection_ref, borrowed_from_project_code)``.

    Priority for finding the connection:
      1. ``SourceBinding`` row on the project → ``PlatformConnection`` (every
         platform — the single connection contract)
      2. For DERIVED projects (dpe-cf: aggregate/consumer, no direct binding):
         walk ``:CONSUMES`` — RECURSIVELY, since an upstream may itself be a
         consumer with no direct connection — to the first product that has a
         real connection OR a materialized target, and borrow that.

    Step 3 is a multi-hop walk: for C→B→A where B is itself a virtual-view
    consumer with no connection, we recurse into B and land on A's connection
    (where B's views physically live). At each hop, a materialized upstream is a
    boundary — read from its target, don't recurse past it. This replaces the
    old single-hop ``LIMIT 1`` borrow that carried the (now-false) comment
    "source is always source-aligned; no recursion needed" and returned an empty
    connection for a chained consumer (the 409-on-deploy bug).

    ``borrowed_from_project_code`` is the DIRECT upstream's ``project_code`` when
    borrowed via ``:CONSUMES``, else ``None``.

    Returns ``("postgres", {}, None)`` when no connection is reachable — an
    empty STRUCTURED ref (no ``host``), which callers detect as unresolved and
    convert to a 4xx (no legacy DSN-in-ref sentinel shape). The
    ``resolved_password`` in the returned ``connection_ref`` is ephemeral; never
    log or persist it.
    """
    # Lazy import avoids a circular dependency (connections imports from models,
    # models has no back-reference to pg_resolver).
    from .routers.connections import (
        resolve_materialization_target_ref,
        resolve_source_connection_ref,
    )

    # Step 1: direct resolution via the project's SourceBinding.
    platform, conn_ref = resolve_source_connection_ref(project, session)

    # Non-Postgres SourceBinding found — done.
    if platform not in ("postgres", "postgresql"):
        return platform, conn_ref, None

    # Direct Postgres connection resolved (structured SourceBinding →
    # PlatformConnection, host/port/username/resolved_password) — done.
    if conn_ref.get("host"):
        return platform, conn_ref, None

    # No direct binding: a derived (dpe-cf) project. Walk :CONSUMES to borrow the
    # connection where its upstreams' data physically lives. Open a Neo4j session
    # with THIS project's credentials (all projects share the instance the
    # :CONSUMES edges live on).
    _contract_id = contract_id or f"{project.project_code}-contract"
    _seen = _seen if _seen is not None else set()
    if _contract_id in _seen:  # DAG guard makes this impossible, but be safe.
        return "postgres", {}, None
    _seen.add(_contract_id)

    try:
        with neo4j_session(
            project.neo4j_host, project.neo4j_port,
            project.neo4j_user, project.neo4j_password, project.neo4j_database,
        ) as ns:
            upstream_ids = [
                r["upstream_contract_id"]
                for r in ns.run(_FIND_ACTIVE_UPSTREAMS_QUERY, contract_id=_contract_id)
                if r["upstream_contract_id"]
            ]
    except Exception:
        return "postgres", {}, None

    for up_contract_id in upstream_ids:
        if not up_contract_id.endswith("-contract"):
            continue
        up_code = up_contract_id[: -len("-contract")]

        # Materialized boundary: if this upstream was materialized/transferred to
        # a distinct target, its data lives THERE — read it, don't recurse past.
        target = resolve_materialization_target_ref(up_code, session)
        if target is not None:
            return target[0], target[1], up_code

        up_project = session.exec(
            select(Project).where(Project.project_code == up_code)
        ).first()
        if up_project is None:
            continue

        # Direct connection on the upstream? Use it.
        up_platform, up_ref = resolve_source_connection_ref(up_project, session)
        if up_platform not in ("postgres", "postgresql"):
            return up_platform, up_ref, up_code
        if up_ref.get("host"):
            return up_platform, up_ref, up_code

        # Upstream is itself a derived product with no direct connection — recurse
        # to where ITS data lives (a virtual-view consumer of a consumer).
        rec_platform, rec_ref, _rec_from = resolve_source_connection_for_project(
            up_project, session, up_contract_id, _seen=_seen,
        )
        if rec_platform not in ("postgres", "postgresql") or rec_ref.get("host"):
            return rec_platform, rec_ref, up_code

    # No upstream yielded a reachable connection — give up (caller raises 4xx).
    return "postgres", {}, None
