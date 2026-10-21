"""Persistence for the transform-placement decision.

The Data Engineer's placement choice (ETL / hybrid / ELT) + the full per-op
decision is stored on the product's transfer `:ServingDefinition` so it can be
reconstituted (re-opened in the dialog, edited) and consumed by the transfer
pipeline generator (`transfer_execution.py`) at build time.

Kept as a standalone module (not in the router) so `transfer_execution` can read
the decision without importing a FastAPI router.
"""
from __future__ import annotations

import json
from typing import Optional

from .neo4j_client import neo4j_session

_FETCH_PRODUCT_URI = """
MATCH (dc:DataContract {id: $contract_id})-[:MATERIALISES_AS]->(dp:DProdDataProduct)
RETURN dp.uri AS product_uri
"""

# MERGE-creates the transfer :ServingDefinition if the transfer hasn't been built
# yet (the placement step runs BEFORE Build), else updates it in place.
_SAVE_DECISION = """
MATCH (dp:DProdDataProduct {uri: $product_uri})
MERGE (dp)-[:SERVED_BY]->(sd:ServingDefinition {productUri: $product_uri, servingMode: 'transfer_then_transform'})
SET sd.placement            = $placement,
    sd.placementDecisionJson = $decision_json,
    sd.placementChosenBy     = $decided_by,
    sd.placementDecidedAt    = datetime()
RETURN sd.placement AS placement
"""

_LOAD_DECISION = """
MATCH (dp:DProdDataProduct {uri: $product_uri})-[:SERVED_BY]->(sd:ServingDefinition {servingMode: 'transfer_then_transform'})
RETURN sd.placement AS placement,
       sd.placementDecisionJson AS decision_json,
       sd.placementChosenBy AS chosen_by,
       toString(sd.placementDecidedAt) AS decided_at
"""


def _contract_id(project) -> str:
    return f"{project.project_code}-contract"


def _product_uri(project) -> Optional[str]:
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        rows = list(ns.run(_FETCH_PRODUCT_URI, contract_id=_contract_id(project)))
    return rows[0]["product_uri"] if rows and rows[0].get("product_uri") else None


def save_placement_decision(project, *, placement: str, decision: dict, decided_by: Optional[str]) -> str:
    """Persist the chosen placement + full decision blob. Returns the stored placement."""
    uri = _product_uri(project)
    if not uri:
        raise RuntimeError("No :DProdDataProduct for this contract — run odcs_to_dprod first.")
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        rows = list(ns.run(
            _SAVE_DECISION, product_uri=uri, placement=placement,
            decision_json=json.dumps(decision, ensure_ascii=False),
            decided_by=decided_by,
        ))
    return rows[0]["placement"] if rows else placement


def load_placement_decision(project) -> Optional[dict]:
    """Return {placement, decision, chosen_by, decided_at} or None if unset."""
    uri = _product_uri(project)
    if not uri:
        return None
    with neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    ) as ns:
        rows = list(ns.run(_LOAD_DECISION, product_uri=uri))
    if not rows or not rows[0].get("placement"):
        return None
    r = rows[0]
    decision = None
    if r.get("decision_json"):
        try:
            decision = json.loads(r["decision_json"])
        except (json.JSONDecodeError, TypeError):
            decision = None
    return {
        "placement": r["placement"],
        "decision": decision,
        "chosen_by": r.get("chosen_by"),
        "decided_at": r.get("decided_at"),
    }
