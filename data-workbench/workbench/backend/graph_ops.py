"""Project-node graph operations for Neo4j project-level scoping.

Provides functions to create/delete :Project nodes and link them to
:Catalog and :DataContract subgraphs, enabling query-level isolation
when multiple projects share a single Neo4j database.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .config import BASE_PROJECT_DIR
from .neo4j_client import neo4j_session

if TYPE_CHECKING:
    from .models import Project


# ── Cypher statements ──────────────────────────────────────────────────────

_ENSURE_INDEX = (
    "CREATE INDEX project_code_idx IF NOT EXISTS "
    "FOR (prj:Project) ON (prj.projectCode)"
)

_MERGE_PROJECT = """\
MERGE (prj:Project {projectCode: $project_code})
SET prj.name = $name,
    prj.domain = $domain
RETURN prj.projectCode AS project_code
"""

_LINK_CATALOG = """\
MATCH (prj:Project {projectCode: $project_code})
MATCH (cat:Catalog {uri: $catalog_uri})
MERGE (prj)-[:HAS_CATALOG]->(cat)
"""

_LINK_CONTRACT = """\
MATCH (prj:Project {projectCode: $project_code})
MATCH (dc:DataContract {id: $contract_id})
MERGE (prj)-[:HAS_CONTRACT]->(dc)
"""

_CHECK_EXISTS = """\
MATCH (prj:Project {projectCode: $project_code})
RETURN count(prj) > 0 AS exists
"""

_DELETE_PROJECT = """\
MATCH (prj:Project {projectCode: $project_code})
OPTIONAL MATCH (prj)-[:HAS_CATALOG]->(cat:Catalog)
OPTIONAL MATCH (cat)-[:DCAT_DATASET]->(ds:Dataset)
OPTIONAL MATCH (ds)-[:HAS_COLUMN]->(col:Column)
OPTIONAL MATCH (col)-[:HAS_DESCRIPTION]->(cd:ColumnDescription)
OPTIONAL MATCH (ds)-[:HAS_TABLE_DESCRIPTION]->(td:TableDescription)
OPTIONAL MATCH (col)-[:HAS_QUALITY_MEASUREMENT]->(qm:QualityMeasurement)
OPTIONAL MATCH (col)-[:HAS_TOP_VALUE]->(tv:TopValue)
OPTIONAL MATCH (col)-[:HAS_QUALITY_SCORE]->(cqs:QualityScore)
OPTIONAL MATCH (ds)-[:HAS_QUALITY_SCORE]->(dqs:QualityScore)
OPTIONAL MATCH (ds)-[:HAS_SHAPE]->(ns:NodeShape)-[:PROPERTY]->(ps:PropertyShape)
OPTIONAL MATCH (col)-[:HAS_REMEDIATION]->(ra:RemediationAction)
OPTIONAL MATCH (cat)-[:HAS_QUALITY_SCORE]->(oqs:QualityScore)
OPTIONAL MATCH (col)<-[:MAPS_SOURCE_COLUMN]-(cm:ColumnMapping)
OPTIONAL MATCH (prj)-[:HAS_TEST_RUN]->(tr:TestRun)
OPTIONAL MATCH (tr)-[:PRODUCED]->(res:TestResult)
OPTIONAL MATCH (prj)-[:HAS_CONTRACT]->(dc:DataContract)
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv:ContractVersion)
OPTIONAL MATCH (dc)-[:HAS_SCHEMA]->(dcs:DataContractSchema)
OPTIONAL MATCH (dcs)-[:HAS_DATASET_TRANSFORM]->(dt_schema:DatasetTransform)
OPTIONAL MATCH (dc)-[:HAS_PROPERTY]->(dcp:DataContractProperty)
OPTIONAL MATCH (dc)-[:HAS_OWNER]->(dco:DataContractOwner)
OPTIONAL MATCH (dc)-[:HAS_STEWARD]->(dcst:DataContractSteward)
OPTIONAL MATCH (dc)-[:HAS_TEAM_MEMBER]->(dctm:DataContractTeamMember)
OPTIONAL MATCH (dc)-[:HAS_ROLE]->(dcr:DataContractRole)
OPTIONAL MATCH (dc)-[:HAS_QUALITY_RULE]->(dcq:DataContractQuality)
OPTIONAL MATCH (dc)-[:HAS_SLA_PROPERTY]->(dcsla:DataContractSLAProperty)
OPTIONAL MATCH (dc)-[:HAS_TERMS]->(dct:DataContractTerms)
OPTIONAL MATCH (dc)-[:HAS_SERVER]->(dcsrv:DataContractServer)
OPTIONAL MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->(port:DProdOutputPort)
OPTIONAL MATCH (port)-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
OPTIONAL MATCH (ods)-[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
OPTIONAL MATCH (ods)-[:HAS_DATASET_TRANSFORM]->(dt_ods:DatasetTransform)
OPTIONAL MATCH (dp)-[:SERVED_BY]->(sd:ServingDefinition)
DETACH DELETE prj, cat, ds, col, cd, td, qm, tv, cqs, dqs, ns, ps, ra, oqs,
              cm, tr, res, dc, cv, dcs, dt_schema, dcp, dco, dcst, dctm, dcr,
              dcq, dcsla, dct, dcsrv, dp, port, ods, pc, dt_ods, sd
"""

# Safety-net sweep: every project-scoped URI embeds the project_code
# (e.g. ``catalog:{project_code}:{schema}`` or ``testrun:{project_code}:...``).
# This catches any orphan that wasn't reachable through the relationship
# traversal above — without it, stale nodes can bleed into a fresh project
# when the project_code is ever recycled.
_DELETE_PROJECT_ORPHANS = """\
MATCH (n) WHERE n.uri CONTAINS $uri_marker
DETACH DELETE n
"""

# Under the stable-node + temporal-edge model, contracts have currentVersion
# and currentLifecycleState set on initial CREATE by save_contract_head().
# Any :DataContract that exists without these fields is from a pre-stable
# model load and must be cleaned slate (per Phase 0 of the change-management
# refactor). The backfill is intentionally minimal — a no-op for new
# contracts, and a defensive default for any legacy stragglers.
_BACKFILL_CONTRACT_VERSIONING = """\
MATCH (dc:DataContract)
WHERE dc.currentVersion IS NULL
SET dc.currentVersion = 1,
    dc.currentLifecycleState = coalesce(dc.currentLifecycleState, 'draft')
RETURN count(dc) AS backfilled
"""


# ── Per-process cache ──────────────────────────────────────────────────────

_project_node_cache: dict[str, bool] = {}
_contract_versioning_cache: dict[str, bool] = {}


def invalidate_cache(project_code: str | None = None) -> None:
    """Clear the has_project_node cache (all entries or a single key)."""
    if project_code is None:
        _project_node_cache.clear()
        _contract_versioning_cache.clear()
    else:
        _project_node_cache.pop(project_code, None)
        _contract_versioning_cache.pop(project_code, None)


# ── Helper ─────────────────────────────────────────────────────────────────

def _neo4j(project: Project):
    return neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password,
        project.neo4j_database,
    )


# ── Public API ─────────────────────────────────────────────────────────────

def ensure_project_node(project: Project) -> None:
    """Create or update the :Project node.  Idempotent (uses MERGE)."""
    with _neo4j(project) as ns:
        ns.run(_ENSURE_INDEX)
        ns.run(
            _MERGE_PROJECT,
            project_code=project.project_code,
            name=project.name,
            domain=getattr(project, "domain", None) or "",
        )
    _project_node_cache[project.project_code] = True


def link_catalogs_to_project(project: Project) -> int:
    """Link Catalog nodes to the Project node.

    Discovers catalog URIs by reading data_discovery YAML filenames in the
    project directory (``schema__table.yaml`` → ``catalog:{schema}``).
    Returns the number of catalogs linked.
    """
    discovery_dir = BASE_PROJECT_DIR / project.project_code / "data_discovery"
    if not discovery_dir.is_dir():
        return 0

    schemas: set[str] = set()
    for yaml_file in discovery_dir.glob("*.yaml"):
        # Exclude profile YAML files
        if yaml_file.stem.endswith("__profile"):
            continue
        # Filename format: schema__table.yaml
        parts = yaml_file.stem.split("__", 1)
        if parts:
            schemas.add(parts[0])

    if not schemas:
        return 0

    linked = 0
    with _neo4j(project) as ns:
        for schema in sorted(schemas):
            catalog_uri = f"catalog:{project.project_code}:{schema}"
            result = ns.run(
                _LINK_CATALOG,
                project_code=project.project_code,
                catalog_uri=catalog_uri,
            )
            result.consume()
            linked += 1

    return linked


def link_contract_to_project(project: Project, contract_id: str) -> None:
    """Link a DataContract node to the Project node.

    Blueprint-Library templates (``template:`` ids) are project-independent by
    construction and must NEVER be linked to a project — a defensive no-op here
    keeps that invariant even if a stray caller passes a template id."""
    if (contract_id or "").startswith("template:"):
        return
    with _neo4j(project) as ns:
        ns.run(
            _LINK_CONTRACT,
            project_code=project.project_code,
            contract_id=contract_id,
        )


def has_project_node(project: Project) -> bool:
    """Check whether a :Project node exists (cached per-process)."""
    pc = project.project_code
    if pc in _project_node_cache:
        return _project_node_cache[pc]
    try:
        with _neo4j(project) as ns:
            record = ns.run(_CHECK_EXISTS, project_code=pc).single()
            exists = record["exists"] if record else False
    except Exception:
        exists = False
    _project_node_cache[pc] = exists
    return exists


def ensure_contract_versioning(project: Project) -> int:
    """Backfill lifecycleVersion / isCurrent / versionedId on existing contracts.

    Idempotent at the Cypher level (``WHERE lifecycleVersion IS NULL``) and
    cached per-process per project_code so repeat calls on the hot save/read
    paths don't round-trip to Neo4j. Returns the number of contracts touched
    on the wire call; 0 when the cache short-circuits.
    """
    pc = project.project_code
    if _contract_versioning_cache.get(pc):
        return 0
    try:
        with _neo4j(project) as ns:
            record = ns.run(_BACKFILL_CONTRACT_VERSIONING).single()
            backfilled = int(record["backfilled"]) if record else 0
        _contract_versioning_cache[pc] = True
        return backfilled
    except Exception:
        # Tolerate unreachable Neo4j at startup — individual save/read paths
        # will retry when they run their own ensure_contract_versioning call.
        return 0


def delete_project_node(project: Project) -> None:
    """Delete the :Project node and all project-scoped subgraphs.

    Cascades through Catalog → Dataset → Column, the DataContract/DProd
    subgraph, TestRun/TestResult, ColumnMapping, and ServingDefinition
    nodes linked to this project. A final URI-pattern sweep removes any
    orphans whose URI still embeds the project_code. Failure of the
    orphan sweep is tolerated so the primary delete is not blocked.
    """
    uri_marker = f":{project.project_code}"
    with _neo4j(project) as ns:
        ns.run(_DELETE_PROJECT, project_code=project.project_code)
        try:
            ns.run(_DELETE_PROJECT_ORPHANS, uri_marker=uri_marker)
        except Exception:
            pass
    _project_node_cache.pop(project.project_code, None)


_CHECK_CONSUMERS = """\
MATCH (dp:DProdDataProduct {uri: $dprod_uri})
OPTIONAL MATCH (cons_dc:DataContract)-[r:CONSUMES]->(dp)
WHERE r.fromVersion <= cons_dc.currentVersion
  AND (r.toVersion IS NULL OR r.toVersion >= cons_dc.currentVersion)
  AND cons_dc.currentLifecycleState IN
      ['draft','submitted','in_engineering','approved','published','superseded']
RETURN
    cons_dc.id                       AS contract_id,
    coalesce(cons_dc.name, '')       AS name,
    coalesce(cons_dc.currentLifecycleState, '') AS lifecycle_state,
    coalesce(cons_dc.domain, '')     AS domain
"""


def check_consumers(project: Project) -> list[dict]:
    """List external DataContracts that :CONSUMES this project's product.

    Each entry is ``{contract_id, name, lifecycle_state, domain}``. Empty
    list means no live consumers; the delete can proceed without warning.
    Returns [] on Neo4j failures so a transient outage doesn't masquerade
    as a green light — the caller logs / surfaces the error before
    deciding to force.
    """
    dprod_uri = f"dprod:{project.project_code}-contract"
    try:
        with _neo4j(project) as ns:
            records = ns.run(_CHECK_CONSUMERS, dprod_uri=dprod_uri).data()
    except Exception:
        return []
    return [
        {
            "contract_id": r.get("contract_id"),
            "name": r.get("name") or r.get("contract_id") or "(unnamed)",
            "lifecycle_state": r.get("lifecycle_state") or "",
            "domain": r.get("domain") or "",
        }
        for r in records
        if r.get("contract_id")
    ]


_CHECK_DC_OWNER = """\
MATCH (prj:Project {projectCode: $project_code})-[:HAS_CONTRACT]->(dc:DataContract)
OPTIONAL MATCH (dc)-[:HAS_OWNER]->(o:DataContractOwner)
RETURN collect(DISTINCT toLower(coalesce(o.email, ''))) AS emails
"""


def project_owner_emails(project: Project) -> set[str]:
    """Return lowercased owner emails for the project.

    Combines ``Project.owner_email`` (the SA-wizard hint) with every
    ``:DataContractOwner.email`` linked to the project's :DataContract via
    :HAS_OWNER. Used by the delete handler to authorize a teardown.
    """
    emails: set[str] = set()
    project_email = (getattr(project, "owner_email", None) or "").strip().lower()
    if project_email:
        emails.add(project_email)
    try:
        with _neo4j(project) as ns:
            record = ns.run(_CHECK_DC_OWNER, project_code=project.project_code).single()
            if record:
                for e in record["emails"] or []:
                    if e:
                        emails.add(e)
    except Exception:
        pass
    return emails
