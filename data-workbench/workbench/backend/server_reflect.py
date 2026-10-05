"""Auto-derive an ODCS ``servers[]`` entry from a product's real deployment.

The Workbench creates the virtual views for every product, so once a product
is deployed we already know exactly where it's served — there's no reason to
make the Product Owner type it in. This module reads the deployment facts off
the ``:ServingDefinition`` plus the resolved Postgres connection and writes a
single, deterministic ``:DataContractServer`` that conforms to the existing
ODCS server field set (name / environment / type / account / database /
schema / datasets).

v1 is derive-only and read-only in the UI; manual server authoring is a
future enhancement. The node is keyed by a fixed URI per contract so
re-running on every publish just refreshes the props (idempotent — no
proliferation of server rows).
"""

from __future__ import annotations

import json
from sqlmodel import Session

from .models import Project
from .pg_resolver import resolve_read_connection_for_consumer


# Read the deployed virtual-view facts for the current contract. Mirrors
# serving.py:_FETCH_SERVING_DEFINITION but also pulls deployedViewNames (the
# qualified names recorded after a successful deploy) and the contract's
# currentVersion so we can stamp the HAS_SERVER edge correctly.
_READ_DEPLOYMENT = """
MATCH (dc:DataContract {id: $contract_id})
WHERE coalesce(dc.isCurrent, true) = true
MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
MATCH (dp)-[:SERVED_BY]->(sd:ServingDefinition {servingMode: 'virtual_view'})
RETURN coalesce(dc.currentVersion, 1)              AS current_version,
       sd.viewSchema                               AS view_schema,
       sd.deployedViewNames                        AS deployed_view_names_json,
       sd.viewNames                                AS view_names_json,
       sd.targetPlatform                           AS target_platform,
       coalesce(sd.deploymentStatus, 'pending')    AS deployment_status
"""


# Deterministic single server per contract — re-running SET-overwrites props
# rather than spawning new rows. ON CREATE pins the version edge; the props
# refresh every run so the server always reflects the latest deployment.
_MERGE_DERIVED_SERVER = """
MATCH (dc:DataContract {id: $contract_id})
MERGE (srv:DataContractServer {uri: $uri})
SET srv.contractId  = $contract_id,
    srv.name        = $name,
    srv.environment = $environment,
    srv.type        = $type,
    srv.account     = $account,
    srv.database    = $database,
    srv.schema      = $schema,
    srv.datasets    = $datasets,
    srv.derived     = true
MERGE (dc)-[r:HAS_SERVER]->(srv)
ON CREATE SET r.fromVersion = $version, r.toVersion = null
RETURN srv.uri AS uri
"""


def reflect_server_from_deployment(
    ns,
    session: Session,
    project: Project,
    contract_id: str,
) -> dict | None:
    """Derive + persist a ``:DataContractServer`` from the deployed view.

    ``ns`` is an open Neo4j session; ``session`` is the SQLModel session used
    to resolve the (possibly consumer-borrowed) Postgres connection. Returns
    the server dict that was written, or ``None`` when there's nothing to
    reflect yet (not deployed, or no resolvable connection). Never raises for
    expected-missing data — callers treat this as best-effort.
    """
    row = ns.run(_READ_DEPLOYMENT, contract_id=contract_id).single()
    if not row or row["deployment_status"] != "deployed":
        return None

    view_schema = row["view_schema"] or ""
    # deployedViewNames is the authoritative post-deploy list; fall back to the
    # pre-deploy viewNames if for some reason it wasn't stamped.
    raw_views = row["deployed_view_names_json"] or row["view_names_json"]
    datasets: list[str] = []
    if raw_views:
        try:
            parsed = json.loads(raw_views) if isinstance(raw_views, str) else raw_views
            if isinstance(parsed, list):
                # Keep just the bare view name (strip any schema qualifier).
                datasets = [str(n).rsplit(".", 1)[-1] for n in parsed]
        except (ValueError, TypeError):
            datasets = []

    # Resolve the connection (consumer products borrow from the first
    # CONSUMES'd source — that's also where their views live). Read host/db from
    # the STRUCTURED ref — the single connection contract, no DSN parse.
    _platform, _conn_ref, _borrowed = resolve_read_connection_for_consumer(
        project, session, contract_id
    )
    host = str(_conn_ref.get("host") or "")
    database = str(_conn_ref.get("database") or "")

    platform = (row["target_platform"] or "postgres").strip() or "postgres"

    server = {
        "name": f"{project.project_code}-deployment",
        "environment": "production",
        "type": platform,
        # The ODCS server model here has no dedicated host field; we surface
        # the deployment host via `account` rather than adding a graph field.
        "account": host,
        "database": database,
        "schema": view_schema,
        "datasets": datasets,
    }

    ns.run(
        _MERGE_DERIVED_SERVER,
        contract_id=contract_id,
        uri=f"server:{contract_id}:deployment",
        version=row["current_version"],
        name=server["name"],
        environment=server["environment"],
        type=server["type"],
        account=server["account"],
        database=server["database"],
        schema=server["schema"],
        datasets=json.dumps(server["datasets"]),
    )
    return server
