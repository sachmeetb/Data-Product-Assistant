"""Stable-node + temporal-edge persistence for :DataContract.

Replaces the legacy clone-per-version persistence in routers/odcs.py. The
data model:

- ONE :DataContract per logical product (stable identity by `id`). Carries
  denormalized current values (name, description, etc.) for hot-path reads
  plus `currentVersion` and `currentLifecycleState` pointers.

- :ContractVersion {version, lifecycleState, publishedAt, occurredAt,
  revisionNotes, changeKind, actor, snapshotName, snapshotDescription, ...}
  sidecar per version, attached via :HAS_VERSION. Provenance chains here:
  :ContractVersion -[:PROV_WAS_DERIVED_FROM]-> predecessor :ContractVersion.

- Substructure (:DataContractOwner, :DataContractSchema, :DataContractProperty,
  :DataContractQuality, etc.) consists of stable nodes keyed on logical
  identity URIs. Edges from the contract carry validity ranges:
  :DataContract -[:HAS_PROPERTY {fromVersion: int, toVersion: int|null}]-> :DataContractProperty.

This module exposes a single entry point — :func:`save_contract_to_graph` —
that handles the full save flow: figure out which version to write to (in-
place draft edit vs new-version branch), update :DataContract current values,
upsert :ContractVersion sidecar, sync substructure via the diff-aware
helpers below.
"""

from __future__ import annotations

from typing import Any, Iterable, NamedTuple

from ._graph_helpers import (
    DRAFT_STATES,
    contract_version_uri,
    current_version_filter,
    owner_uri,
    property_uri,
    quality_uri,
    role_uri,
    schema_uri,
    server_uri,
    sla_property_uri,
    steward_uri,
    team_member_uri,
    terms_uri,
    dataset_transform_uri,
)


# ── Helpers ───────────────────────────────────────────────────────────────


def _parse_initial_version(version_string: str | None) -> int:
    """Extract the major integer from a semver string like '2.3.0' → 2.

    Used on initial contract save so an ingested ODCS spec carrying
    ``version: "2.3.0"`` lands at ``currentVersion=2`` instead of resetting
    the lifecycle counter to 1. Defaults to 1 for missing, blank, or
    unparseable input (matching the legacy hardcoded behavior).
    """
    if not version_string:
        return 1
    head = str(version_string).strip().lstrip("vV").split(".", 1)[0]
    try:
        parsed = int(head)
        return parsed if parsed >= 1 else 1
    except (TypeError, ValueError):
        return 1


# ── Cypher templates ──────────────────────────────────────────────────────

# Read the contract's current state. Empty if no contract exists yet.
READ_HEAD_STATE = """\
MATCH (dc:DataContract {id: $contract_id})
RETURN coalesce(dc.currentVersion, 1) AS current_version,
       coalesce(dc.currentLifecycleState, 'draft') AS current_state
"""


# Initial save: create the stable :DataContract + its first :ContractVersion.
CREATE_INITIAL_CONTRACT = """\
MERGE (dc:DataContract {id: $contract_id})
ON CREATE SET dc.createdAt = datetime()
SET dc.currentVersion = $initial_version,
    dc.currentLifecycleState = $status,
    dc.name = $name,
    dc.version = $version,
    dc.status = $status,
    dc.apiVersion = $apiVersion,
    dc.kind = $kind,
    dc.domain = $domain,
    dc.dataProduct = $dataProduct,
    dc.description = $description,
    dc.purpose = $purpose,
    dc.limitations = $limitations,
    dc.productKind = $productKind,
    dc.scoringRubric = $scoringRubric,
    dc.tags = $tags,
    dc.support = $support,
    dc.customProperties = $customProperties,
    dc.extras = $extras,
    dc.updatedAt = datetime()
MERGE (cv:ContractVersion {uri: $contract_version_uri})
ON CREATE SET cv.version = $initial_version,
              cv.occurredAt = datetime()
SET cv.lifecycleState = $status,
    cv.snapshotName = $name,
    cv.snapshotDescription = $description,
    cv.snapshotPurpose = $purpose,
    cv.changeKind = 'initial',
    cv.revisionNotes = $revision_notes,
    cv.actor = $submitted_by
MERGE (dc)-[:HAS_VERSION]->(cv)
RETURN dc.currentVersion AS current_version
"""


# In-place update of the current version's values (draft edits — no version
# bump). Re-runs the SET portion of CREATE_INITIAL_CONTRACT against the
# existing stable :DataContract.
UPDATE_CONTRACT_IN_PLACE = """\
MATCH (dc:DataContract {id: $contract_id})
SET dc.currentLifecycleState = $status,
    dc.name = $name,
    dc.version = $version,
    dc.status = $status,
    dc.apiVersion = $apiVersion,
    dc.kind = $kind,
    dc.domain = $domain,
    dc.dataProduct = $dataProduct,
    dc.description = $description,
    dc.purpose = $purpose,
    dc.limitations = $limitations,
    dc.productKind = $productKind,
    dc.scoringRubric = $scoringRubric,
    dc.tags = $tags,
    dc.support = $support,
    dc.customProperties = $customProperties,
    dc.extras = $extras,
    dc.updatedAt = datetime()
WITH dc
MATCH (dc)-[:HAS_VERSION]->(cv:ContractVersion {version: dc.currentVersion})
SET cv.lifecycleState = $status,
    cv.snapshotName = $name,
    cv.snapshotDescription = $description,
    cv.snapshotPurpose = $purpose
RETURN dc.currentVersion AS current_version
"""


# Cosmetic patch on a non-draft contract: update the :DataContract's
# current values + the current :ContractVersion's snapshot fields, and
# attach a :ProvActivity {activityType: 'ContractPatch'} for audit. The
# lifecycleState is NOT changed (a published contract stays published);
# the revision_notes + changeKind ride on the patch activity, not the cv.
# Multiple cosmetic patches per version are supported — each emits its
# own :ProvActivity sidecar with a time-suffixed URI.
PATCH_CONTRACT_IN_PLACE = """\
MATCH (dc:DataContract {id: $contract_id})
SET dc.name = $name,
    dc.version = $version,
    dc.status = $status,
    dc.apiVersion = $apiVersion,
    dc.kind = $kind,
    dc.domain = $domain,
    dc.dataProduct = $dataProduct,
    dc.description = $description,
    dc.purpose = $purpose,
    dc.limitations = $limitations,
    dc.productKind = $productKind,
    dc.scoringRubric = $scoringRubric,
    dc.tags = $tags,
    dc.support = $support,
    dc.customProperties = $customProperties,
    dc.extras = $extras,
    dc.updatedAt = datetime()
WITH dc
MATCH (dc)-[:HAS_VERSION]->(cv:ContractVersion {version: dc.currentVersion})
SET cv.snapshotName = $name,
    cv.snapshotDescription = $description,
    cv.snapshotPurpose = $purpose
MERGE (agent:ProvAgent {uri: 'prov:agent:human:' + coalesce($submitted_by, 'unknown')})
  ON CREATE SET agent.agentType = 'human', agent.name = coalesce($submitted_by, 'unknown')
CREATE (act:ProvActivity {
    uri: 'prov:activity:contract-patch:' + dc.id + ':v' + toString(cv.version) + ':' + toString(timestamp()),
    activityType: 'ContractPatch',
    occurredAt: datetime(),
    changeKind: 'cosmetic',
    revisionNotes: $revision_notes,
    actor: coalesce($submitted_by, 'unknown')
})
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (cv)-[:HAS_PATCH]->(act)
RETURN dc.currentVersion AS current_version
"""


# Branch a new lifecycle version. The stable :DataContract stays; a new
# :ContractVersion sidecar is created and linked to the prior version via
# :PROV_WAS_DERIVED_FROM. currentVersion is incremented. The new version
# starts in 'draft' state regardless of the prior state.
BRANCH_NEW_VERSION = """\
MATCH (dc:DataContract {id: $contract_id})
WITH dc, dc.currentVersion AS prior_v, dc.currentVersion + 1 AS new_v
MATCH (dc)-[:HAS_VERSION]->(prior_cv:ContractVersion {version: prior_v})
SET dc.currentVersion = new_v,
    dc.currentLifecycleState = 'draft',
    dc.name = $name,
    dc.version = $version,
    dc.status = $status,
    dc.apiVersion = $apiVersion,
    dc.kind = $kind,
    dc.domain = $domain,
    dc.dataProduct = $dataProduct,
    dc.description = $description,
    dc.purpose = $purpose,
    dc.limitations = $limitations,
    dc.productKind = $productKind,
    dc.scoringRubric = $scoringRubric,
    dc.tags = $tags,
    dc.support = $support,
    dc.customProperties = $customProperties,
    dc.extras = $extras,
    dc.updatedAt = datetime()
CREATE (cv:ContractVersion {
    uri: $contract_version_uri,
    version: new_v,
    lifecycleState: 'draft',
    occurredAt: datetime(),
    snapshotName: $name,
    snapshotDescription: $description,
    snapshotPurpose: $purpose,
    changeKind: $change_kind,
    revisionNotes: $revision_notes,
    actor: $submitted_by
})
CREATE (dc)-[:HAS_VERSION]->(cv)
CREATE (cv)-[:PROV_WAS_DERIVED_FROM]->(prior_cv)
RETURN dc.currentVersion AS current_version
"""


# ── Substructure sync — generic stable-node + temporal-edge helpers ───────

# Reading the set of substructure URIs currently active at a given version
# is the foundation for the diff. The query is parameterized by the edge
# label and target label so the same shape works for owners, schemas,
# properties, rules, etc.

READ_ACTIVE_SUBSTRUCTURE_TEMPLATE = """\
MATCH (dc:DataContract {{id: $contract_id}})-[r:{rel_label}]->(n:{node_label})
WHERE r.fromVersion <= $version AND (r.toVersion IS NULL OR r.toVersion >= $version)
RETURN n.uri AS uri, r.fromVersion AS from_version
"""


def _active_substructure_query(rel_label: str, node_label: str) -> str:
    return READ_ACTIVE_SUBSTRUCTURE_TEMPLATE.format(
        rel_label=rel_label, node_label=node_label
    )


# Expire a substructure edge in two passes to keep each query simple:
#  1. If the edge was added at the current save version (fromVersion == save_v),
#     just delete it — the entity never made it into a frozen version's history.
#  2. Otherwise (added in an earlier version), mark its toVersion = save_v - 1
#     so it remains visible when reading history at older versions.
EXPIRE_EDGE_SAME_VERSION_TEMPLATE = """\
MATCH (dc:DataContract {{id: $contract_id}})-[r:{rel_label}]->(n:{node_label} {{uri: $uri}})
WHERE r.toVersion IS NULL AND r.fromVersion = $version
DELETE r
"""

EXPIRE_EDGE_PRIOR_VERSION_TEMPLATE = """\
MATCH (dc:DataContract {{id: $contract_id}})-[r:{rel_label}]->(n:{node_label} {{uri: $uri}})
WHERE r.toVersion IS NULL AND r.fromVersion < $version
SET r.toVersion = $version - 1
"""


def _expire_edge_queries(rel_label: str, node_label: str) -> tuple[str, str]:
    return (
        EXPIRE_EDGE_SAME_VERSION_TEMPLATE.format(rel_label=rel_label, node_label=node_label),
        EXPIRE_EDGE_PRIOR_VERSION_TEMPLATE.format(rel_label=rel_label, node_label=node_label),
    )


# After a dropped substructure URI has had its contract-side edge expired,
# clean up any node that is now truly orphaned so reset-loop re-synthesis at the
# SAME version doesn't accumulate stale nodes. Two passes:
#  1. Clear any *leftover* same-version, still-open edge into the node that the
#     contract-side expiry above doesn't touch — notably the denormalized
#     (:DataContractSchema)-[:HAS_PROPERTY]->(prop) edge, which is created at
#     MERGE time but not lifecycle-synced. Only same-version (fromVersion ==
#     save_version, toVersion IS NULL) edges are cleared; a frozen prior-version
#     edge (toVersion set) is deliberately preserved for historical reads.
#  2. Delete the node only if NOTHING points at it anymore. Any surviving edge
#     (a frozen prior-version interval) means the node is still part of some
#     version's history and must be kept.
CLEAR_LEFTOVER_SAME_VERSION_EDGE_TEMPLATE = """\
MATCH (n:{node_label} {{uri: $uri}})
OPTIONAL MATCH ()-[stale:{rel_label}]->(n)
WHERE stale.toVersion IS NULL AND stale.fromVersion = $version
DELETE stale
"""

SWEEP_ORPHAN_NODE_TEMPLATE = """\
MATCH (n:{node_label} {{uri: $uri}})
WHERE NOT ( ()-[:{rel_label}]->(n) )
DETACH DELETE n
"""


def _orphan_sweep_queries(rel_label: str, node_label: str) -> tuple[str, str]:
    return (
        CLEAR_LEFTOVER_SAME_VERSION_EDGE_TEMPLATE.format(rel_label=rel_label, node_label=node_label),
        SWEEP_ORPHAN_NODE_TEMPLATE.format(rel_label=rel_label, node_label=node_label),
    )


def _sync_active_set(
    ns,
    contract_id: str,
    save_version: int,
    rel_label: str,
    node_label: str,
    incoming_uris: set[str],
) -> None:
    """Expire any active edge whose target URI isn't in the incoming set.

    Called after the caller has MERGEd the incoming substructure with active
    edges. This pass walks the active set at this version, and for any URI
    not in `incoming_uris`, expires its edge.
    """
    active_rows = list(
        ns.run(
            _active_substructure_query(rel_label, node_label),
            contract_id=contract_id,
            version=save_version,
        )
    )
    same_v_q, prior_v_q = _expire_edge_queries(rel_label, node_label)
    clear_leftover_q, sweep_q = _orphan_sweep_queries(rel_label, node_label)
    for row in active_rows:
        uri = row["uri"]
        if uri in incoming_uris:
            continue
        ns.run(same_v_q, contract_id=contract_id, uri=uri, version=save_version)
        ns.run(prior_v_q, contract_id=contract_id, uri=uri, version=save_version)
        # Delete the node if this drop left it fully orphaned (same-version-only
        # entity). Frozen prior-version edges survive both passes and keep the
        # node alive for historical reads. Prevents stale :DataContractProperty
        # accumulation when reset_stage + re-synthesize changes a physical name
        # at the same draft version (the URI embeds the name → a new node).
        ns.run(clear_leftover_q, uri=uri, version=save_version)
        ns.run(sweep_q, uri=uri)


# ── Per-entity MERGE templates ────────────────────────────────────────────

MERGE_OWNER = """\
MATCH (dc:DataContract {id: $contract_id})
MERGE (o:DataContractOwner {uri: $uri})
SET o.contractId = $contract_id,
    o.username = $username,
    o.name = $name,
    o.role = $role,
    o.email = $email
MERGE (dc)-[r:HAS_OWNER]->(o)
ON CREATE SET r.fromVersion = $version, r.toVersion = null
"""


MERGE_STEWARD = """\
MATCH (dc:DataContract {id: $contract_id})
MERGE (st:DataContractSteward {uri: $uri})
SET st.contractId = $contract_id,
    st.username = $username,
    st.name = $name,
    st.role = $role,
    st.email = $email
MERGE (dc)-[r:HAS_STEWARD]->(st)
ON CREATE SET r.fromVersion = $version, r.toVersion = null
"""


MERGE_TEAM_MEMBER = """\
MATCH (dc:DataContract {id: $contract_id})
MERGE (tm:DataContractTeamMember {uri: $uri})
SET tm.contractId = $contract_id,
    tm.username = $username,
    tm.name = $name,
    tm.role = $role,
    tm.email = $email
MERGE (dc)-[r:HAS_TEAM_MEMBER]->(tm)
ON CREATE SET r.fromVersion = $version, r.toVersion = null
"""


MERGE_ROLE = """\
MATCH (dc:DataContract {id: $contract_id})
MERGE (ro:DataContractRole {uri: $uri})
SET ro.contractId = $contract_id,
    ro.role = $role,
    ro.description = $description,
    ro.access = $access,
    ro.datasets = $datasets
MERGE (dc)-[r:HAS_ROLE]->(ro)
ON CREATE SET r.fromVersion = $version, r.toVersion = null
"""


MERGE_SERVER = """\
MATCH (dc:DataContract {id: $contract_id})
MERGE (srv:DataContractServer {uri: $uri})
SET srv.contractId = $contract_id,
    srv.name = $name,
    srv.environment = $environment,
    srv.type = $type,
    srv.account = $account,
    srv.database = $database,
    srv.schema = $schema,
    srv.datasets = $datasets
MERGE (dc)-[r:HAS_SERVER]->(srv)
ON CREATE SET r.fromVersion = $version, r.toVersion = null
"""


MERGE_SCHEMA = """\
MATCH (dc:DataContract {id: $contract_id})
MERGE (s:DataContractSchema {uri: $uri})
SET s.contractId = $contract_id,
    s.physicalName = $physicalName,
    s.name = $name,
    s.description = $description,
    s.physicalType = $physicalType,
    s.foreignKeys = $foreignKeys
MERGE (dc)-[r:HAS_SCHEMA]->(s)
ON CREATE SET r.fromVersion = $version, r.toVersion = null
"""


MERGE_PROPERTY = """\
MATCH (dc:DataContract {id: $contract_id})
MATCH (s:DataContractSchema {uri: $schema_uri})
MERGE (p:DataContractProperty {uri: $uri})
SET p.contractId = $contract_id,
    p.schemaName = $schemaName,
    p.physicalName = $physicalName,
    p.name = $name,
    p.logicalName = $logicalName,
    p.logicalType = $logicalType,
    p.physicalType = $physicalType,
    p.description = $description,
    p.primaryKey = $primaryKey,
    p.required = $required,
    p.criticalDataElement = $criticalDataElement,
    p.pii = $pii,
    p.classification = $classification,
    p.sensitivity = $sensitivity,
    p.examples = $examples,
    p.logicalTypeOptions = $logicalTypeOptions,
    p.transformHint = $transformHint,
    p.sourceColumnUri = $sourceColumnUri
// Multi-interval validity: if the property was previously expired
// (toVersion is set on the prior edge), CREATE a new edge for the new
// validity window instead of reusing the expired one. This preserves
// the prior interval (so historical queries at v=N still correctly
// exclude the column when it was removed) while marking the property
// active in the current version. MERGE alone would reuse the expired
// edge — its ON CREATE wouldn't fire, leaving toVersion stale.
WITH dc, s, p
OPTIONAL MATCH (dc)-[r_active:HAS_PROPERTY]->(p) WHERE r_active.toVersion IS NULL
WITH dc, s, p, count(r_active) AS active_count
FOREACH (_ IN CASE WHEN active_count = 0 THEN [1] ELSE [] END |
  CREATE (dc)-[:HAS_PROPERTY {fromVersion: $version, toVersion: null}]->(p)
)
WITH dc, s, p
OPTIONAL MATCH (s)-[r2_active:HAS_PROPERTY]->(p) WHERE r2_active.toVersion IS NULL
WITH dc, s, p, count(r2_active) AS s_active_count
FOREACH (_ IN CASE WHEN s_active_count = 0 THEN [1] ELSE [] END |
  CREATE (s)-[:HAS_PROPERTY {fromVersion: $version, toVersion: null}]->(p)
)
"""


MERGE_QUALITY = """\
MATCH (dc:DataContract {id: $contract_id})
MERGE (q:DataContractQuality {uri: $uri})
SET q.contractId = $contract_id,
    q.rule = $rule,
    q.name = $name,
    q.description = $description,
    q.severity = $severity,
    q.dimension = $dimension,
    q.businessImpact = $businessImpact,
    q.column = $column,
    q.dataset = $dataset
MERGE (dc)-[r:HAS_QUALITY_RULE]->(q)
ON CREATE SET r.fromVersion = $version, r.toVersion = null
"""


MERGE_SLA = """\
MATCH (dc:DataContract {id: $contract_id})
MERGE (sla:DataContractSLAProperty {uri: $uri})
SET sla.contractId = $contract_id,
    sla.property = $property,
    sla.value = $value,
    sla.unit = $unit
MERGE (dc)-[r:HAS_SLA_PROPERTY]->(sla)
ON CREATE SET r.fromVersion = $version, r.toVersion = null
"""


MERGE_TERMS = """\
MATCH (dc:DataContract {id: $contract_id})
MERGE (t:DataContractTerms {uri: $uri})
SET t.contractId = $contract_id,
    t.usage = $usage,
    t.limitations = $limitations,
    t.billing = $billing,
    t.noticePeriod = $noticePeriod
MERGE (dc)-[r:HAS_TERMS]->(t)
ON CREATE SET r.fromVersion = $version, r.toVersion = null
"""


MERGE_DATASET_TRANSFORM = """\
MATCH (s:DataContractSchema {uri: $schema_uri})
MERGE (dt:DatasetTransform {uri: $uri})
SET dt.contractId = $contract_id,
    dt.schemaPhysicalName = $schema_name,
    dt.filterPredicate = $filterPredicate,
    dt.filterIntent = $filterIntent,
    dt.dedupeJson = $dedupeJson,
    dt.joinsJson = $joinsJson,
    dt.groupingKeysJson = $groupingKeysJson,
    dt.windowSpecsJson = $windowSpecsJson,
    dt.scdPolicyJson = $scdPolicyJson,
    dt.suppressedColumnsJson = $suppressedColumnsJson,
    dt.grainProse = $grainProse
MERGE (s)-[r:HAS_DATASET_TRANSFORM]->(dt)
ON CREATE SET r.fromVersion = $version, r.toVersion = null
"""


# ── :CONSUMES edges — temporal, like substructure ─────────────────────────

MERGE_CONSUMES_EDGE = """\
MATCH (dc:DataContract {id: $contract_id})
MATCH (src:DProdDataProduct {uri: $dprod_uri})
// Snapshot the source contract's currentVersion at edge-create time. The
// pin survives the diff-aware soft-expire sync on consumer save (we MERGE the
// edge so an already-active edge's consumedVersion is preserved). The drift
// endpoint compares this pin to the source's live currentVersion to decide
// whether the consumer is on a stale view.
OPTIONAL MATCH (src_dc:DataContract)-[:MATERIALISES_AS]->(src)
WITH dc, src, coalesce(src_dc.currentVersion, 1) AS src_v
MERGE (dc)-[r:CONSUMES]->(src)
ON CREATE SET r.fromVersion = $version,
              r.toVersion = null,
              r.consumedVersion = src_v
// Reactivation: a previously-expired edge (toVersion set) that's been re-added
// must be reopened, not left dead — MERGE reuses the expired edge so ON CREATE
// won't fire. Re-pin fromVersion/consumedVersion and clear toVersion. An
// already-active edge (toVersion IS NULL) is untouched (a same-version re-sync
// stays a true no-op, preserving its consumedVersion pin).
WITH r, src_v
FOREACH (_ IN CASE WHEN r.toVersion IS NOT NULL THEN [1] ELSE [] END |
  SET r.toVersion = null,
      r.fromVersion = $version,
      r.consumedVersion = src_v
)
"""


READ_ACTIVE_CONSUMES = """\
MATCH (dc:DataContract {id: $contract_id})-[r:CONSUMES]->(src:DProdDataProduct)
WHERE r.fromVersion <= $version AND (r.toVersion IS NULL OR r.toVersion >= $version)
RETURN src.uri AS uri, r.fromVersion AS from_version,
       r.consumedVersion AS consumed_version
"""


EXPIRE_CONSUMES_EDGE_SAME_V = """\
MATCH (dc:DataContract {id: $contract_id})-[r:CONSUMES]->(src:DProdDataProduct {uri: $dprod_uri})
WHERE r.toVersion IS NULL AND r.fromVersion = $version
DELETE r
"""

EXPIRE_CONSUMES_EDGE_PRIOR_V = """\
MATCH (dc:DataContract {id: $contract_id})-[r:CONSUMES]->(src:DProdDataProduct {uri: $dprod_uri})
WHERE r.toVersion IS NULL AND r.fromVersion < $version
SET r.toVersion = $version - 1
"""


# ── DAG guard: self / cycle / eligibility validation (Phase 0 keystone) ─────
#
# A consumer (`dpe-cf`) may :CONSUMES any published product — source, aggregate,
# or another consumer — forming multi-hop chains. The graph must stay a strict
# DAG: self-consumption and any binding whose target can already reach the
# consumer are hard-rejected at this single choke point (shared by wizard
# submit, ingest `from_odcs`, and the MCP save/ingest tools), BEFORE any
# mutation, so a rejected save leaves the contract head + edges byte-unchanged.

# A product is *consumable* (bindable as an upstream) iff it has at least one
# published/superseded :ContractVersion — the same "is deployed" test the
# marketplace listing pins to. Head lifecycle alone is insufficient: a source
# mid-edit-cycle has a `draft` head yet a still-deployed published version.
CONSUMABLE_LIFECYCLE_STATES = ("published", "superseded")

# The three data-mesh product kinds any consumer may build on.
ALLOWED_UPSTREAM_PRODUCT_KINDS = ("source", "aggregate", "consumer")


class ConsumesBindingError(ValueError):
    """Raised when an incoming :CONSUMES binding is refused.

    Carries a machine-readable ``reason`` and the offending ``dprod_uri`` so the
    API layer can surface a structured 4xx the wizard/MCP can render. Reasons:
    ``self`` (a product cannot consume itself), ``cycle`` (target already reaches
    this consumer), ``unknown_target`` (no such :DProdDataProduct),
    ``not_published`` (target has no published/superseded version), and
    ``unsupported_kind`` (target's productKind is outside the allowed set).
    """

    def __init__(self, reason: str, message: str, offending_uri: str = ""):
        super().__init__(message)
        self.reason = reason
        self.offending_uri = offending_uri


def would_create_cycle(neighbors_fn, start_contract: str, target_contract: str) -> bool:
    """Would binding ``start_contract`` → (product of) ``target_contract`` cycle?

    ``neighbors_fn(contract_id) -> Iterable[str]`` returns the contract ids that
    ``contract_id`` already consumes (its direct upstream dependencies). We BFS
    from ``target_contract`` over that dependency direction; reaching
    ``start_contract`` means the new edge would close a cycle. Also treats a
    self-binding (start == target) as a cycle.

    Pure + deterministic — all graph access is behind ``neighbors_fn`` — so it
    unit-tests with a plain dict of neighbors and no Neo4j.
    """
    if start_contract == target_contract:
        return True
    visited: set[str] = set()
    stack: list[str] = [target_contract]
    while stack:
        node = stack.pop()
        if node == start_contract:
            return True
        if node in visited:
            continue
        visited.add(node)
        for nb in neighbors_fn(node):
            if nb and nb not in visited:
                stack.append(nb)
    return False


# One upstream hop over the ALTERNATING dependency shape
# (DataContract-[:CONSUMES]->DProdDataProduct<-[:MATERIALISES_AS]-DataContract),
# using the UNION of currently-active edges (toVersion IS NULL) and edges active
# at the contract's latest deployed (published/superseded) version. Conservative
# on purpose: a draft that drops an edge the deployed view still uses must not
# open a cycle window.
CONSUMES_UPSTREAM_NEIGHBORS = """\
MATCH (dc:DataContract {id: $contract_id})
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv_pub:ContractVersion)
  WHERE cv_pub.lifecycleState IN $consumable_states
WITH dc, max(cv_pub.version) AS deployed_v
MATCH (dc)-[r:CONSUMES]->(dp:DProdDataProduct)<-[:MATERIALISES_AS]-(up:DataContract)
WHERE r.toVersion IS NULL
   OR (deployed_v IS NOT NULL
       AND r.fromVersion <= deployed_v
       AND (r.toVersion IS NULL OR r.toVersion >= deployed_v))
RETURN DISTINCT up.id AS upstream_contract_id
"""


# Batch eligibility for the complete incoming input set. For each requested
# dprod URI, report whether the product exists, its owning contract's kind, and
# how many published/superseded versions it has (0 ⇒ not consumable).
CONSUMES_ELIGIBILITY = """\
UNWIND $dprod_uris AS want_uri
OPTIONAL MATCH (dp:DProdDataProduct {uri: want_uri})
OPTIONAL MATCH (owner:DataContract)-[:MATERIALISES_AS]->(dp)
WITH want_uri, dp, owner
OPTIONAL MATCH (owner)-[:HAS_VERSION]->(cv:ContractVersion)
  WHERE cv.lifecycleState IN $consumable_states
WITH want_uri, dp, owner, count(cv) AS publishable_versions
RETURN want_uri AS uri,
       dp IS NOT NULL AS dp_exists,
       owner.id AS owner_contract_id,
       coalesce(owner.productKind, '') AS product_kind,
       publishable_versions AS publishable_versions
"""


# Serialise :CONSUMES topology mutations across processes (uvicorn workers).
# A MERGE+SET on a singleton sentinel takes a write-lock held until the enclosing
# transaction commits, so two reciprocal binds (A→B ‖ B→A) can't both pass
# validation and commit a cycle. An in-process asyncio.Lock wouldn't cross
# workers; this does.
ACQUIRE_TOPOLOGY_LOCK = """\
MERGE (l:GraphTopologyLock {id: 'consumes'})
SET l.lockedAt = timestamp()
RETURN l.id AS id
"""


def _target_contract_id(dprod_uri: str) -> str:
    """`dprod:<contract_id>` → `<contract_id>` (empty when the shape is wrong)."""
    prefix = "dprod:"
    if not dprod_uri.startswith(prefix):
        return ""
    return dprod_uri[len(prefix):]


def acquire_topology_lock(ns) -> None:
    """Take the cross-process topology write-lock for the current transaction.

    Best-effort: a driver/permissions hiccup on the lock node must never block
    a save (single-writer deployments don't need it), so failures are swallowed.
    """
    try:
        ns.run(ACQUIRE_TOPOLOGY_LOCK).consume()
    except Exception:
        pass


def validate_consumes_bindings(ns, contract_id: str, inputs: Iterable[Any]) -> None:
    """Validate the COMPLETE incoming :CONSUMES set before any mutation.

    Raises :class:`ConsumesBindingError` on the first offending input — a
    self-loop, a binding that would close a cycle, an unknown target, a target
    with no published/superseded version, or a target whose productKind is
    unsupported. Read-only (no writes), so a rejected save rolls back cleanly.
    """
    # Collect + de-dup the incoming URIs, preserving order.
    seen: set[str] = set()
    uris: list[str] = []
    for inp in inputs or []:
        if not isinstance(inp, dict):
            continue
        u = (inp.get("dprod_uri") or "").strip()
        if u and u not in seen:
            seen.add(u)
            uris.append(u)
    if not uris:
        return

    self_uri = f"dprod:{contract_id}"

    rows = {
        r["uri"]: r
        for r in ns.run(
            CONSUMES_ELIGIBILITY,
            dprod_uris=uris,
            consumable_states=list(CONSUMABLE_LIFECYCLE_STATES),
        )
    }

    def neighbors(cid: str) -> set[str]:
        return {
            r["upstream_contract_id"]
            for r in ns.run(
                CONSUMES_UPSTREAM_NEIGHBORS,
                contract_id=cid,
                consumable_states=list(CONSUMABLE_LIFECYCLE_STATES),
            )
            if r["upstream_contract_id"]
        }

    for u in uris:
        if u == self_uri:
            raise ConsumesBindingError(
                "self", "A data product cannot consume itself.", u
            )
        row = rows.get(u)
        if row is None or not row["dp_exists"]:
            raise ConsumesBindingError(
                "unknown_target",
                f"Upstream product '{u}' does not exist and cannot be consumed.",
                u,
            )
        if (row["publishable_versions"] or 0) < 1:
            raise ConsumesBindingError(
                "not_published",
                f"Upstream product '{u}' has no published version yet — publish it "
                "before consuming it.",
                u,
            )
        pk = (row["product_kind"] or "").strip().lower()
        if pk not in ALLOWED_UPSTREAM_PRODUCT_KINDS:
            raise ConsumesBindingError(
                "unsupported_kind",
                f"Upstream product '{u}' has productKind "
                f"'{pk or 'unset'}', which cannot be consumed.",
                u,
            )
        target_contract = _target_contract_id(u)
        if would_create_cycle(neighbors, contract_id, target_contract):
            raise ConsumesBindingError(
                "cycle",
                f"Consuming '{u}' would create a dependency cycle — that product "
                "already depends on this one, directly or transitively.",
                u,
            )


# ── Public entry point ────────────────────────────────────────────────────


def determine_save_mode(current_state: str | None, change_kind: str = "auto") -> str:
    """Return 'initial' | 'in_place' | 'patch' | 'branch' based on the head's
    lifecycle state and the resolved change kind.

    - No contract exists yet → ``initial``.
    - Head is in a drafty state → ``in_place`` (today's behavior; same
      version, current values overwritten).
    - Head is non-draft AND change_kind=='cosmetic' → ``patch``: update
      current values + write :ProvActivity ContractPatch on the current cv.
      No version bump.
    - Head is non-draft AND change_kind in {'schema', 'breaking', 'auto'} →
      ``branch``: increment currentVersion, create a new :ContractVersion
      sidecar, run substructure sync against the new version.
    """
    if current_state is None:
        return "initial"
    if current_state in DRAFT_STATES:
        return "in_place"
    if change_kind == "cosmetic":
        return "patch"
    return "branch"


def save_contract_substructure(
    ns,
    contract_id: str,
    save_version: int,
    spec: dict,
    to_json_str,
) -> None:
    """Sync the substructure (owners, schemas, properties, etc.) for save_version.

    Every entity in `spec` is MERGEd as a stable node with an active edge at
    save_version. Any entity that was active before but isn't in `spec` gets
    its edge expired (deleted if added in this same version, otherwise
    set toVersion = save_version - 1).
    """

    # ── Owners ──
    owner_uris: set[str] = set()
    for i, owner in enumerate(spec.get("owners") or []):
        if not isinstance(owner, dict):
            continue
        key = owner.get("email") or owner.get("username") or f"idx-{i}"
        uri = owner_uri(contract_id, key)
        owner_uris.add(uri)
        ns.run(
            MERGE_OWNER,
            contract_id=contract_id,
            uri=uri,
            version=save_version,
            username=owner.get("username", ""),
            name=owner.get("name", ""),
            role=owner.get("role", ""),
            email=owner.get("email", ""),
        )
    _sync_active_set(
        ns, contract_id, save_version, "HAS_OWNER", "DataContractOwner", owner_uris
    )

    # ── Stewards ──
    steward_uris: set[str] = set()
    for i, st in enumerate(spec.get("stewards") or []):
        if not isinstance(st, dict):
            continue
        key = st.get("email") or st.get("username") or f"idx-{i}"
        uri = steward_uri(contract_id, key)
        steward_uris.add(uri)
        ns.run(
            MERGE_STEWARD,
            contract_id=contract_id,
            uri=uri,
            version=save_version,
            username=st.get("username", ""),
            name=st.get("name", ""),
            role=st.get("role", ""),
            email=st.get("email", ""),
        )
    _sync_active_set(
        ns, contract_id, save_version,
        "HAS_STEWARD", "DataContractSteward", steward_uris,
    )

    # ── Team members ──
    tm_uris: set[str] = set()
    for i, tm in enumerate(spec.get("team") or []):
        if not isinstance(tm, dict):
            continue
        key = tm.get("email") or tm.get("username") or f"idx-{i}"
        uri = team_member_uri(contract_id, key)
        tm_uris.add(uri)
        ns.run(
            MERGE_TEAM_MEMBER,
            contract_id=contract_id,
            uri=uri,
            version=save_version,
            username=tm.get("username", ""),
            name=tm.get("name", ""),
            role=tm.get("role", ""),
            email=tm.get("email", ""),
        )
    _sync_active_set(
        ns, contract_id, save_version,
        "HAS_TEAM_MEMBER", "DataContractTeamMember", tm_uris,
    )

    # ── Roles ──
    role_uris: set[str] = set()
    for i, r in enumerate(spec.get("roles") or []):
        if not isinstance(r, dict):
            continue
        key = r.get("role") or f"idx-{i}"
        uri = role_uri(contract_id, key)
        role_uris.add(uri)
        ns.run(
            MERGE_ROLE,
            contract_id=contract_id,
            uri=uri,
            version=save_version,
            role=r.get("role", ""),
            description=r.get("description", ""),
            access=r.get("access", ""),
            datasets=[str(d) for d in (r.get("datasets") or [])],
        )
    _sync_active_set(
        ns, contract_id, save_version, "HAS_ROLE", "DataContractRole", role_uris
    )

    # ── Servers ──
    server_uris: set[str] = set()
    for i, sv in enumerate(spec.get("servers") or []):
        if not isinstance(sv, dict):
            continue
        key = sv.get("name") or f"idx-{i}"
        uri = server_uri(contract_id, key)
        server_uris.add(uri)
        ns.run(
            MERGE_SERVER,
            contract_id=contract_id,
            uri=uri,
            version=save_version,
            name=sv.get("name", ""),
            environment=sv.get("environment", ""),
            type=sv.get("type", ""),
            account=sv.get("account", ""),
            database=sv.get("database", ""),
            schema=sv.get("schema", ""),
            datasets=to_json_str(sv.get("datasets") or []),
        )
    _sync_active_set(
        ns, contract_id, save_version, "HAS_SERVER", "DataContractServer", server_uris
    )

    # ── Schemas + properties + dataset transforms ──
    schema_uris: set[str] = set()
    all_property_uris: set[str] = set()
    schema_to_property_uris: dict[str, set[str]] = {}
    transform_uris: set[str] = set()

    for schema in spec.get("schema") or []:
        if not isinstance(schema, dict):
            continue
        physical_name = schema.get("physicalName") or schema.get("name", "")
        s_uri = schema_uri(contract_id, physical_name)
        schema_uris.add(s_uri)
        ns.run(
            MERGE_SCHEMA,
            contract_id=contract_id,
            uri=s_uri,
            version=save_version,
            physicalName=physical_name,
            name=schema.get("name", ""),
            description=schema.get("description", ""),
            physicalType=schema.get("physicalType", "table"),
            foreignKeys=to_json_str(schema.get("foreignKeys") or []),
        )

        # Dataset transform (only when non-empty)
        ds_xform = schema.get("transform")
        if isinstance(ds_xform, dict) and ds_xform:
            dt_uri = dataset_transform_uri(contract_id, physical_name)
            transform_uris.add(dt_uri)
            ns.run(
                MERGE_DATASET_TRANSFORM,
                schema_uri=s_uri,
                uri=dt_uri,
                version=save_version,
                contract_id=contract_id,
                schema_name=physical_name,
                filterPredicate=ds_xform.get("filter") or "",
                filterIntent=ds_xform.get("filter_intent") or "",
                dedupeJson=to_json_str(ds_xform.get("dedupe") or {}),
                joinsJson=to_json_str(ds_xform.get("joins") or []),
                groupingKeysJson=to_json_str(ds_xform.get("grouping_keys") or []),
                windowSpecsJson=to_json_str(ds_xform.get("window_specs") or {}),
                scdPolicyJson=to_json_str(ds_xform.get("scd_policy") or {}),
                suppressedColumnsJson=to_json_str(
                    ds_xform.get("suppressed_columns") or []
                ),
                grainProse=ds_xform.get("grain_prose") or "",
            )

        prop_uri_set: set[str] = set()
        for prop in schema.get("properties") or []:
            if not isinstance(prop, dict):
                continue
            phys = prop.get("physicalName") or prop.get("name", "")
            p_uri = property_uri(contract_id, physical_name, phys)
            prop_uri_set.add(p_uri)
            all_property_uris.add(p_uri)
            examples = prop.get("examples") or []
            if not isinstance(examples, list):
                examples = [examples]
            transform_hint = prop.get("transform")
            transform_hint_json = (
                to_json_str(transform_hint) if transform_hint else ""
            )
            # Sensitivity: first-class enum (none|internal|confidential|pii|phi).
            # If the spec didn't set it explicitly but pii=true, default to
            # 'pii' so the two fields stay consistent without forcing every
            # PO authoring path to set both. Defaults to 'none' otherwise.
            raw_sens = prop.get("sensitivity")
            if isinstance(raw_sens, str) and raw_sens.strip():
                sensitivity = raw_sens.strip().lower()
            elif prop.get("pii"):
                sensitivity = "pii"
            else:
                sensitivity = "none"
            ns.run(
                MERGE_PROPERTY,
                contract_id=contract_id,
                schema_uri=s_uri,
                uri=p_uri,
                version=save_version,
                schemaName=physical_name,
                physicalName=phys,
                name=prop.get("name", ""),
                logicalName=prop.get("logicalName", ""),
                logicalType=prop.get("logicalType", ""),
                physicalType=prop.get("physicalType", ""),
                description=prop.get("description", ""),
                primaryKey=bool(prop.get("primaryKey", False)),
                required=bool(prop.get("required", False)),
                criticalDataElement=bool(prop.get("criticalDataElement", False)),
                pii=bool(prop.get("pii", False)),
                classification=prop.get("classification", ""),
                sensitivity=sensitivity,
                examples=[str(x) for x in examples],
                logicalTypeOptions=to_json_str(prop.get("logicalTypeOptions") or {}),
                transformHint=transform_hint_json,
                sourceColumnUri=prop.get("sourceColumnUri", "") or "",
            )
        schema_to_property_uris[s_uri] = prop_uri_set

    _sync_active_set(
        ns, contract_id, save_version, "HAS_SCHEMA", "DataContractSchema", schema_uris
    )
    _sync_active_set(
        ns, contract_id, save_version,
        "HAS_PROPERTY", "DataContractProperty", all_property_uris,
    )

    # Expire dataset transforms not in incoming. The :HAS_DATASET_TRANSFORM
    # edge is on the schema, not the contract — handle separately.
    _sync_schema_dataset_transforms(ns, contract_id, save_version, transform_uris)

    # Schema→property edges share the temporal-edge pattern; the sync runs
    # against the active set scoped per-schema. Currently we just rely on
    # the contract-side HAS_PROPERTY sync to drive lifecycle — the
    # schema-side HAS_PROPERTY edge is denormalized for traversal speed and
    # follows the contract edge.

    # ── Quality rules ──
    quality_uris: set[str] = set()
    for i, q in enumerate(spec.get("quality") or []):
        if not isinstance(q, dict):
            continue
        # Stable identity for rules: hash of (rule + column + dataset + name)
        key = q.get("name") or f"{q.get('rule', '')}::{q.get('column', '')}::{q.get('dataset', '')}::{i}"
        q_uri = quality_uri(contract_id, key)
        quality_uris.add(q_uri)
        ns.run(
            MERGE_QUALITY,
            contract_id=contract_id,
            uri=q_uri,
            version=save_version,
            rule=q.get("rule", ""),
            name=q.get("name", ""),
            description=q.get("description", ""),
            severity=q.get("severity", "warning"),
            dimension=q.get("dimension", ""),
            businessImpact=q.get("businessImpact", ""),
            column=q.get("column", ""),
            dataset=q.get("dataset", ""),
        )
    _sync_active_set(
        ns, contract_id, save_version,
        "HAS_QUALITY_RULE", "DataContractQuality", quality_uris,
    )

    # ── SLA properties ──
    sla_uris: set[str] = set()
    for i, sla in enumerate(spec.get("slaProperties") or []):
        if not isinstance(sla, dict):
            continue
        key = sla.get("property") or f"idx-{i}"
        s_uri = sla_property_uri(contract_id, key)
        sla_uris.add(s_uri)
        ns.run(
            MERGE_SLA,
            contract_id=contract_id,
            uri=s_uri,
            version=save_version,
            property=sla.get("property", ""),
            value=str(sla.get("value", "")),
            unit=sla.get("unit", ""),
        )
    _sync_active_set(
        ns, contract_id, save_version,
        "HAS_SLA_PROPERTY", "DataContractSLAProperty", sla_uris,
    )

    # ── Terms (singleton) ──
    terms = spec.get("terms") or {}
    if isinstance(terms, dict) and any(v for v in terms.values()):
        t_uri = terms_uri(contract_id)
        ns.run(
            MERGE_TERMS,
            contract_id=contract_id,
            uri=t_uri,
            version=save_version,
            usage=terms.get("usage", ""),
            limitations=terms.get("limitations", ""),
            billing=terms.get("billing", ""),
            noticePeriod=terms.get("noticePeriod", ""),
        )
        _sync_active_set(
            ns, contract_id, save_version,
            "HAS_TERMS", "DataContractTerms", {t_uri},
        )
    else:
        _sync_active_set(
            ns, contract_id, save_version,
            "HAS_TERMS", "DataContractTerms", set(),
        )

    # Schema, rules, or shape just moved — bump the contract's schema-change
    # marker so the marketplace's :QAEvaluation staleness check fires. Reads
    # use coalesce(dc.lastSchemaChangeVersion, dc.currentVersion), so the
    # first-ever bump leaves older deployments unaffected.
    ns.run(
        "MATCH (dc:DataContract {id: $contract_id}) "
        "SET dc.lastSchemaChangeVersion = $version",
        contract_id=contract_id,
        version=save_version,
    )


READ_ACTIVE_DATASET_TRANSFORMS = """\
MATCH (dc:DataContract {id: $contract_id})-[:HAS_SCHEMA]->(s:DataContractSchema)
      -[r:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform)
WHERE r.fromVersion <= $version AND (r.toVersion IS NULL OR r.toVersion >= $version)
RETURN dt.uri AS uri, r.fromVersion AS from_version
"""


EXPIRE_DATASET_TRANSFORM_SAME_V = """\
MATCH (s:DataContractSchema)-[r:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform {uri: $uri})
WHERE r.toVersion IS NULL AND r.fromVersion = $version
DELETE r
"""

EXPIRE_DATASET_TRANSFORM_PRIOR_V = """\
MATCH (s:DataContractSchema)-[r:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform {uri: $uri})
WHERE r.toVersion IS NULL AND r.fromVersion < $version
SET r.toVersion = $version - 1
"""


def _sync_schema_dataset_transforms(
    ns, contract_id: str, save_version: int, incoming_uris: set[str]
) -> None:
    """Same active-set diff pattern, but the temporal edge lives on the schema."""
    active_rows = list(
        ns.run(
            READ_ACTIVE_DATASET_TRANSFORMS,
            contract_id=contract_id,
            version=save_version,
        )
    )
    for row in active_rows:
        uri = row["uri"]
        if uri in incoming_uris:
            continue
        ns.run(EXPIRE_DATASET_TRANSFORM_SAME_V, uri=uri, version=save_version)
        ns.run(EXPIRE_DATASET_TRANSFORM_PRIOR_V, uri=uri, version=save_version)


def sync_consumes_edges(
    ns,
    contract_id: str,
    save_version: int,
    inputs: Iterable[Any],
) -> None:
    """Same diff-aware sync for the :CONSUMES edges on consumer-aligned products."""
    incoming_dprod_uris: set[str] = set()
    for inp in inputs or []:
        if not isinstance(inp, dict):
            continue
        dprod_uri = (inp.get("dprod_uri") or "").strip()
        if not dprod_uri:
            continue
        incoming_dprod_uris.add(dprod_uri)
        ns.run(
            MERGE_CONSUMES_EDGE,
            contract_id=contract_id,
            dprod_uri=dprod_uri,
            version=save_version,
        )

    active_rows = list(
        ns.run(
            READ_ACTIVE_CONSUMES, contract_id=contract_id, version=save_version
        )
    )
    for row in active_rows:
        uri = row["uri"]
        if uri in incoming_dprod_uris:
            continue
        ns.run(
            EXPIRE_CONSUMES_EDGE_SAME_V,
            contract_id=contract_id, dprod_uri=uri, version=save_version,
        )
        ns.run(
            EXPIRE_CONSUMES_EDGE_PRIOR_V,
            contract_id=contract_id, dprod_uri=uri, version=save_version,
        )


def save_contract_head(
    ns,
    contract_id: str,
    contract_props: dict,
    submitted_by: str = "",
    change_kind: str = "auto",
    revision_notes: str = "",
) -> tuple[str, int]:
    """Persist the :DataContract head + :ContractVersion sidecar.

    Returns ``(save_mode, save_version)`` where save_mode is one of
    ``"initial"`` / ``"in_place"`` / ``"patch"`` / ``"branch"`` so the
    caller knows whether to run the full substructure sync (initial /
    in_place / branch) or skip it (patch — no substructure changes).
    """
    row = ns.run(READ_HEAD_STATE, contract_id=contract_id).single()
    current_state = row["current_state"] if row else None
    current_version = row["current_version"] if row else None
    save_mode = determine_save_mode(current_state, change_kind=change_kind)

    if save_mode == "initial":
        save_version = _parse_initial_version(contract_props.get("version"))
        ns.run(
            CREATE_INITIAL_CONTRACT,
            contract_id=contract_id,
            contract_version_uri=contract_version_uri(contract_id, save_version),
            initial_version=save_version,
            submitted_by=submitted_by,
            revision_notes=revision_notes,
            **contract_props,
        )
    elif save_mode == "in_place":
        save_version = current_version
        ns.run(
            UPDATE_CONTRACT_IN_PLACE,
            contract_id=contract_id,
            **contract_props,
        )
    elif save_mode == "patch":
        save_version = current_version
        ns.run(
            PATCH_CONTRACT_IN_PLACE,
            contract_id=contract_id,
            submitted_by=submitted_by,
            revision_notes=revision_notes,
            **contract_props,
        )
    else:
        save_version = (current_version or 0) + 1
        ns.run(
            BRANCH_NEW_VERSION,
            contract_id=contract_id,
            contract_version_uri=contract_version_uri(contract_id, save_version),
            submitted_by=submitted_by,
            change_kind=change_kind,
            revision_notes=revision_notes,
            **contract_props,
        )

    return save_mode, save_version


# ── Discard an unintended draft branch (inverse of BRANCH_NEW_VERSION) ──────
#
# The versioning machinery is otherwise forward-only. This is the ONE reverse
# operation: it discards a draft `:ContractVersion` that was branched from a
# prior version and restores the prior version as head. It is deliberately
# narrow — it only ever removes a *draft* head — so it can never destroy
# published/approved history. Reuses the temporal edge model: the branch to
# version N added edges `{fromVersion:N, toVersion:null}` and closed replaced
# edges with `toVersion = N-1`; the inverse deletes the former and reopens the
# latter. Codifies the manually-verified Customer 360 rollback recipe.

# All (rel_label, node_label) substructure edges hung off :DataContract.
SUBSTRUCTURE_ENTITIES: list[tuple[str, str]] = [
    ("HAS_OWNER", "DataContractOwner"),
    ("HAS_TEAM_MEMBER", "DataContractTeamMember"),
    ("HAS_ROLE", "DataContractRole"),
    ("HAS_SERVER", "DataContractServer"),
    ("HAS_SCHEMA", "DataContractSchema"),
    ("HAS_PROPERTY", "DataContractProperty"),
    ("HAS_QUALITY_RULE", "DataContractQuality"),
    ("HAS_SLA_PROPERTY", "DataContractSLAProperty"),
]

# Head states we allow discarding. A submitted/approved/published head is NOT
# discardable here — those carry downstream state (engineer queue, deploys).
_DISCARDABLE_HEAD_STATES = {"draft", "ingesting"}

READ_DISCARD_STATE = """\
MATCH (dc:DataContract {id: $contract_id})
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(head:ContractVersion {version: dc.currentVersion})
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(prior:ContractVersion {version: dc.currentVersion - 1})
RETURN dc.currentVersion AS current_version,
       head.lifecycleState AS head_state,
       prior.version AS prior_version,
       prior.lifecycleState AS prior_state,
       prior.snapshotName AS prior_name,
       prior.snapshotDescription AS prior_description,
       prior.snapshotPurpose AS prior_purpose,
       dc.name AS cur_name, dc.description AS cur_description, dc.purpose AS cur_purpose
"""


class DiscardNotAllowed(ValueError):
    """Raised when the head is not a discardable draft branch (→ HTTP 4xx)."""


class DiscardResult(NamedTuple):
    discarded_version: int
    restored_version: int
    restored_lifecycle: str
    removed_property_names: list[str]
    active_property_names: list[str]
    scalar_warning: str | None


def discard_draft_version(ns, contract_id: str) -> DiscardResult:
    """Discard the head draft `:ContractVersion` and restore the prior version.

    Guardrails: `currentVersion > 1`, a prior `:ContractVersion` exists, and the
    head version's `lifecycleState ∈ {draft, ingesting}`. Raises
    `DiscardNotAllowed` otherwise. Does NOT touch dprod (`:DProdColumn`) — the
    caller reconciles that from `active_property_names` (kept out of here so this
    module stays contract-structure only).
    """
    row = ns.run(READ_DISCARD_STATE, contract_id=contract_id).single()
    if row is None or row["current_version"] is None:
        raise DiscardNotAllowed(f"No contract '{contract_id}'.")
    n = row["current_version"]
    head_state = (row["head_state"] or "").lower()
    prior_version = row["prior_version"]
    if n <= 1 or prior_version is None:
        raise DiscardNotAllowed(
            f"currentVersion is {n} — no prior version to roll back to."
        )
    if head_state not in _DISCARDABLE_HEAD_STATES:
        raise DiscardNotAllowed(
            f"Head version v{n} is '{head_state or 'unknown'}', not a draft — "
            "refusing to discard a non-draft version (published/approved history "
            "is never destroyed)."
        )
    pv = prior_version  # == n - 1
    prior_state = row["prior_state"] or "approved"

    # Property names added at version N (for the report), captured before delete.
    removed_property_names = sorted(
        r["name"]
        for r in ns.run(
            "MATCH (dc:DataContract {id:$cid})-[r:HAS_PROPERTY]->(p:DataContractProperty) "
            "WHERE r.fromVersion = $n AND r.toVersion IS NULL RETURN p.name AS name",
            cid=contract_id, n=n,
        )
    )

    # Reverse each substructure entity: delete vN additions (+ sweep orphaned
    # nodes), then reopen edges the branch closed at toVersion == N-1.
    for rel, node in SUBSTRUCTURE_ENTITIES:
        add_uris = {
            r["uri"]
            for r in ns.run(
                f"MATCH (dc:DataContract {{id:$cid}})-[r:{rel}]->(x:{node}) "
                f"WHERE r.fromVersion = $n AND r.toVersion IS NULL RETURN x.uri AS uri",
                cid=contract_id, n=n,
            )
        }
        ns.run(
            f"MATCH (dc:DataContract {{id:$cid}})-[r:{rel}]->(x:{node}) "
            f"WHERE r.fromVersion = $n AND r.toVersion IS NULL DELETE r",
            cid=contract_id, n=n,
        )
        # DataContractProperty also carries a denormalized schema-side HAS_PROPERTY
        # edge — delete those vN additions too so the orphan sweep can fire.
        if rel == "HAS_PROPERTY":
            add_uris |= {
                r["uri"]
                for r in ns.run(
                    "MATCH (s:DataContractSchema {contractId:$cid})-[r:HAS_PROPERTY]->(x:DataContractProperty) "
                    "WHERE r.fromVersion = $n AND r.toVersion IS NULL RETURN x.uri AS uri",
                    cid=contract_id, n=n,
                )
            }
            ns.run(
                "MATCH (s:DataContractSchema {contractId:$cid})-[r:HAS_PROPERTY]->(x:DataContractProperty) "
                "WHERE r.fromVersion = $n AND r.toVersion IS NULL DELETE r",
                cid=contract_id, n=n,
            )
        for uri in add_uris:
            ns.run(
                f"MATCH (x:{node} {{uri:$uri}}) WHERE NOT ( ()-[:{rel}]->(x) ) DETACH DELETE x",
                uri=uri,
            )
        # Reopen edges the branch to N closed (toVersion == N-1) so the restored
        # version's substructure is the current/open set again.
        ns.run(
            f"MATCH (dc:DataContract {{id:$cid}})-[r:{rel}]->(x:{node}) "
            f"WHERE r.toVersion = $pv SET r.toVersion = null",
            cid=contract_id, pv=pv,
        )

    # Property names active at the restored version (drives dprod reconcile).
    active_property_names = sorted(
        r["name"]
        for r in ns.run(
            "MATCH (dc:DataContract {id:$cid})-[r:HAS_PROPERTY]->(p:DataContractProperty) "
            "WHERE r.fromVersion <= $pv AND (r.toVersion IS NULL OR r.toVersion >= $pv) "
            "RETURN DISTINCT p.name AS name",
            cid=contract_id, pv=pv,
        )
    )

    # Delete the discarded draft :ContractVersion (takes HAS_VERSION +
    # PROV_WAS_DERIVED_FROM with it).
    ns.run(
        "MATCH (dc:DataContract {id:$cid})-[:HAS_VERSION]->(cv:ContractVersion {version:$n}) "
        "DETACH DELETE cv",
        cid=contract_id, n=n,
    )

    # Restore the head scalars. Denormalized name/description/purpose come from
    # the prior version's snapshot; other scalars (tags/support/…) are not
    # snapshotted, so warn if the draft had changed metadata.
    scalar_warning = None
    if (row["cur_name"], row["cur_description"], row["cur_purpose"]) != (
        row["prior_name"], row["prior_description"], row["prior_purpose"]
    ):
        scalar_warning = (
            "The discarded draft had changed product metadata. Name/description/purpose "
            "were restored from the prior version snapshot, but other non-snapshotted "
            "scalars (tags, support, customProperties) may still reflect the draft — "
            "re-check them if used."
        )
    ns.run(
        "MATCH (dc:DataContract {id:$cid}) "
        "SET dc.currentVersion = $pv, "
        "    dc.currentLifecycleState = $prior_state, "
        "    dc.lastSchemaChangeVersion = CASE WHEN dc.lastSchemaChangeVersion > $pv "
        "                                      THEN $pv ELSE dc.lastSchemaChangeVersion END, "
        "    dc.name = coalesce($prior_name, dc.name), "
        "    dc.description = coalesce($prior_description, dc.description), "
        "    dc.purpose = coalesce($prior_purpose, dc.purpose), "
        "    dc.updatedAt = datetime()",
        cid=contract_id, pv=pv, prior_state=prior_state,
        prior_name=row["prior_name"], prior_description=row["prior_description"],
        prior_purpose=row["prior_purpose"],
    )

    return DiscardResult(
        discarded_version=n,
        restored_version=pv,
        restored_lifecycle=prior_state,
        removed_property_names=removed_property_names,
        active_property_names=active_property_names,
        scalar_warning=scalar_warning,
    )
