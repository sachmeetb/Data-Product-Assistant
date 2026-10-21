"""Persisted business-concept ontology — Phase 3b foundation.

A small additive layer on top of the existing graph:

  (:BusinessConcept {uri, name, definition, domain, level, value_token?,
                     predicate_template?, status, createdBy, createdAt,
                     promotedFromRecommendationUri?})
    -[:HAS_VALUE]-> (:BusinessConcept)         // super → value tree
    -[:REPRESENTED_BY]-> (:DProdColumn)         // many-to-many binding

Single new label, two new edge types. No existing label or property is
modified — every existing read path keeps working whether 0 or N
concepts exist. Domain is REQUIRED on every concept; the marketplace
chat agent only sees concepts whose `domain` matches the user's selected
scope. `status='deprecated'` is the soft-delete state; readers exclude
deprecated by default.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Literal, Optional

from . import embeddings
from .models import AppSettings
from .neo4j_client import neo4j_session


def _slugify(text: str) -> str:
    """Filesystem-safe lowercase slug for the URI."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()
    return s or "concept"


def _build_uri(domain: str, name: str) -> str:
    # Tail random nonce keeps the URI unique even when the same name is
    # promoted into the same domain (deprecating an old concept + adding
    # a new one with the same human name shouldn't collide).
    return f"concept:{_slugify(domain)}:{_slugify(name)}:{int(time.time() * 1000) % 1_000_000:06d}"


# Constraints + indexes — idempotent, safe to re-apply.
_CONSTRAINT_QUERIES = [
    "CREATE CONSTRAINT business_concept_uri IF NOT EXISTS FOR (c:BusinessConcept) REQUIRE c.uri IS UNIQUE",
    "CREATE INDEX business_concept_domain_status IF NOT EXISTS FOR (c:BusinessConcept) ON (c.domain, c.status)",
    # Native vector index for Concept-Guided retrieval (Neo4j 5.13+). Cosine
    # over the 384-dim bge-small embedding stored on :BusinessConcept.embedding.
    # Swallowed on older servers by ensure_constraints — retrieval just falls
    # back to Full Context if the index isn't there.
    (
        "CREATE VECTOR INDEX business_concept_embedding IF NOT EXISTS "
        "FOR (c:BusinessConcept) ON c.embedding "
        "OPTIONS {indexConfig: {`vector.dimensions`: %d, `vector.similarity_function`: 'cosine'}}"
        % embeddings.EMBED_DIM
    ),
]

VECTOR_INDEX_NAME = "business_concept_embedding"


def ensure_constraints(settings: AppSettings) -> None:
    """Best-effort. Idempotent. Called lazily from create paths."""
    try:
        with neo4j_session(
            settings.neo4j_host, settings.neo4j_port,
            settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
        ) as ns:
            for q in _CONSTRAINT_QUERIES:
                try:
                    ns.run(q).consume()
                except Exception:
                    # Older Neo4j versions or permission issues — swallow.
                    pass
    except Exception:
        pass


# ── Create / promote ───────────────────────────────────────────────────────


class DuplicateConceptError(ValueError):
    """Raised when a non-deprecated concept with the same (domain, name)
    already exists. Cross-domain duplicates are allowed; within a domain
    they create ambiguity in chat resolution + view-DDL bindings."""

    def __init__(self, existing_uri: str, existing_name: str, domain: str):
        super().__init__(
            f"A concept named {existing_name!r} already exists in domain {domain!r} "
            f"(uri={existing_uri}). Deprecate it first or pick a distinct name."
        )
        self.existing_uri = existing_uri
        self.existing_name = existing_name
        self.domain = domain


_DUP_CHECK = """
MATCH (c:BusinessConcept)
WHERE c.domain = $domain
  AND toLower(c.name) = toLower($name)
  AND coalesce(c.status, 'active') <> 'deprecated'
  AND c.uri <> $exclude_uri
RETURN c.uri AS uri, c.name AS name, coalesce(c.level, 'attribute') AS level LIMIT 1
"""

# Concept tier ranking (entity > attribute > value). A collision resolution must
# NEVER deprecate a HIGHER-tier concept for a lower-tier one — that's how the
# deterministic entity scaffold got clobbered by same-named LLM proposals in the
# recommend step (an 'Employee' attribute deprecating the 'Employee' entity).
_TIER = {"value": 1, "attribute": 2, "entity": 3}


def _singularize(w: str) -> str:
    """Naive English singulariser (word-level). Deliberately conservative so it
    only folds obvious plurals: employees→employee, addresses→address,
    categories→category; leaves 'class'/'status'/'address' alone."""
    if len(w) <= 3:
        return w
    if w.endswith(("us", "ss", "is")):
        return w  # status, class, address, analysis — not plurals
    if w.endswith("ies"):
        return w[:-3] + "y"
    if w.endswith(("shes", "ches", "xes", "zes", "ses")):
        return w[:-2]
    if w.endswith("s"):
        return w[:-1]
    return w


def _norm_name(s: str) -> str:
    """Normalise a concept name for singular/plural + case-insensitive coreference
    (used ONLY to protect a higher tier — never to trigger a deprecation)."""
    words = [w for w in re.split(r"\s+", (s or "").strip().lower()) if w]
    return " ".join(_singularize(w) for w in words)


# Candidate collisions in a domain (active only). Python-side matching layers an
# exact case-insensitive check over a normalized (singular/plural) fallback.
_DUP_CANDIDATES = """
MATCH (c:BusinessConcept)
WHERE c.domain = $domain
  AND coalesce(c.status, 'active') <> 'deprecated'
  AND c.uri <> $exclude_uri
RETURN c.uri AS uri, c.name AS name, coalesce(c.level, 'attribute') AS level
"""


def _find_collision(ns, domain: str, name: str, exclude_uri: str, new_level: str):
    """Return ``(dup_dict_or_None, exact_match_bool)``. Exact match uses the full
    tier logic; a normalized-only match is returned ONLY when it protects a
    HIGHER tier (so normalization can never cause a deprecation — see _norm_name)."""
    cands = [dict(r) for r in ns.run(_DUP_CANDIDATES, domain=domain, exclude_uri=exclude_uri)]
    low = (name or "").lower()
    exact = next((c for c in cands if (c.get("name") or "").lower() == low), None)
    if exact:
        return {"uri": exact["uri"], "name": exact["name"],
                "level": exact.get("level") or "attribute"}, True
    nn = _norm_name(name)
    higher = [c for c in cands
              if _norm_name(c.get("name") or "") == nn
              and _TIER.get(c.get("level") or "attribute", 2) > _TIER.get(new_level, 2)]
    if higher:
        best = max(higher, key=lambda c: _TIER.get(c.get("level") or "attribute", 2))
        return {"uri": best["uri"], "name": best["name"],
                "level": best.get("level") or "attribute"}, False
    return None, False


def _collision_action(dup_level: str, new_level: str, if_exists: str) -> str:
    """Decide how a same-name (per-domain) collision resolves, tier-aware. Pure —
    unit-tested in test_semantic_scaffold_collision.py.

    Returns one of:
      - 'coreference'  : a HIGHER-tier concept owns the name → the incoming
                         concept IS that concept; never deprecate it, create nothing.
      - 'replace'      : same tier → deprecate the existing and recreate (migrating
                         children) when if_exists='deprecate_existing'.
      - 'promote_over' : a LOWER-tier concept holds the name and we're creating a
                         higher tier (legitimate upgrade) → deprecate the lower one
                         (no child migration across tiers) when replacing.
      - 'fail'         : collision but if_exists='fail'.
    """
    du, nu = _TIER.get(dup_level, 2), _TIER.get(new_level, 2)
    if du > nu:
        return "coreference"          # protect the higher tier unconditionally
    if if_exists != "deprecate_existing":
        return "fail"
    return "replace" if du == nu else "promote_over"


# Auto-deprecate path: stamp the existing concept as deprecated and link
# the new concept to it via :PROV_WAS_DERIVED_FROM so audit history stays
# intact. Fires only when ``if_exists='deprecate_existing'`` and a
# non-deprecated collision is detected.
_DEPRECATE_EXISTING_FOR_REPLACE = """
MATCH (old:BusinessConcept {uri: $old_uri})
SET old.status = 'deprecated',
    old.deprecatedAt = datetime(),
    old.deprecatedReason = coalesce(old.deprecatedReason,
        'auto-deprecated: superseded by new concept with the same name on accept')
RETURN old.uri AS uri
"""


# Re-parent VALUE children when a SUPER is replaced. Without this, the
# new SUPER inherits no children and the VALUEs become orphan roots in
# get_tree (the tree-builder excludes deprecated parents from by_uri).
# Idempotent: MERGE guards against duplicate edges, DELETE clears the
# old fan-out.
_MIGRATE_HAS_VALUE_EDGES = """
MATCH (old:BusinessConcept {uri: $old_uri})-[r:HAS_ATTRIBUTE|HAS_VALUE]->(child:BusinessConcept)
MATCH (new:BusinessConcept {uri: $new_uri})
FOREACH (_ IN CASE WHEN type(r) = 'HAS_ATTRIBUTE' THEN [1] ELSE [] END | MERGE (new)-[:HAS_ATTRIBUTE]->(child))
FOREACH (_ IN CASE WHEN type(r) = 'HAS_VALUE'     THEN [1] ELSE [] END | MERGE (new)-[:HAS_VALUE]->(child))
DELETE r
RETURN count(child) AS migrated
"""


_CREATE_CONCEPT = """
MERGE (c:BusinessConcept {uri: $uri})
ON CREATE SET c.createdAt = datetime()
SET c.name = $name,
    c.definition = $definition,
    c.domain = $domain,
    c.level = $level,
    c.value_token = $value_token,
    c.predicate_template = $predicate_template,
    c.synonymsJson = $synonyms_json,
    c.status = coalesce(c.status, 'active'),
    c.createdBy = coalesce(c.createdBy, $created_by),
    c.promotedFromRecommendationUri = coalesce(c.promotedFromRecommendationUri, $rec_uri)
WITH c
// Parent edge (optional). Single-parent for v1 — wipe any prior edge (either
// hierarchy type) first, then recreate the one named by $parent_edge:
//   entity -[:HAS_ATTRIBUTE]-> attribute -[:HAS_VALUE]-> value
OPTIONAL MATCH (c)<-[old_parent:HAS_ATTRIBUTE|HAS_VALUE]-(:BusinessConcept)
DELETE old_parent
WITH c
FOREACH (_ IN CASE WHEN $parent_uri IS NULL OR $parent_uri = '' OR $parent_edge <> 'HAS_ATTRIBUTE' THEN [] ELSE [1] END |
  MERGE (p:BusinessConcept {uri: $parent_uri})
  MERGE (p)-[:HAS_ATTRIBUTE]->(c)
)
FOREACH (_ IN CASE WHEN $parent_uri IS NULL OR $parent_uri = '' OR $parent_edge <> 'HAS_VALUE' THEN [] ELSE [1] END |
  MERGE (p:BusinessConcept {uri: $parent_uri})
  MERGE (p)-[:HAS_VALUE]->(c)
)
WITH c
// Representation edges are polymorphic: entity ⟶ :DProdOutputDataset,
// attribute ⟶ :DProdColumn. Wipe all, then recreate from the canonical sets.
// CALL unit-subqueries keep c's row alive even when a binding list is empty
// (a bare UNWIND [] would collapse the row and drop the RETURN).
OPTIONAL MATCH (c)-[old_rep:REPRESENTED_BY]->()
DELETE old_rep
WITH c
CALL {
  WITH c
  UNWIND coalesce($represented_by_uris, []) AS col_uri
  OPTIONAL MATCH (col:DProdColumn {uri: col_uri})
  FOREACH (_ IN CASE WHEN col IS NULL THEN [] ELSE [1] END |
    MERGE (c)-[:REPRESENTED_BY]->(col)
  )
}
CALL {
  WITH c
  UNWIND coalesce($represented_by_dataset_uris, []) AS ds_uri
  OPTIONAL MATCH (ds:DProdOutputDataset {uri: ds_uri})
  FOREACH (_ IN CASE WHEN ds IS NULL THEN [] ELSE [1] END |
    MERGE (c)-[:REPRESENTED_BY]->(ds)
  )
}
RETURN c.uri AS uri, c.status AS status, c.name AS name
"""


def create_concept(
    settings: AppSettings,
    *,
    name: str,
    definition: str,
    domain: str,
    level: str = "attribute",
    value_token: Optional[str] = None,
    predicate_template: Optional[str] = None,
    parent_uri: Optional[str] = None,
    represented_by_uris: Optional[list[str]] = None,
    represented_by_dataset_uris: Optional[list[str]] = None,
    synonyms: Optional[list[str]] = None,
    created_by: str = "Data Steward",
    rec_uri: Optional[str] = None,
    uri: Optional[str] = None,
    if_exists: Literal["fail", "deprecate_existing"] = "fail",
) -> dict[str, Any]:
    """Create (or upsert) a :BusinessConcept node + its parent + rep edges.

    URI defaults to ``concept:<slug-domain>:<slug-name>:<6-digit-nonce>``.
    When the caller passes ``uri`` explicitly the same URI is upserted
    (useful for the from-recommendation path: it reuses the recommendation's
    URI suffix so the promotion is traceable).

    ``if_exists`` controls name-collision handling within the same domain:
      - ``'fail'`` (default): raise :class:`DuplicateConceptError` matching
        the historical contract — kept for callers that want explicit
        Steward confirmation before overwriting.
      - ``'deprecate_existing'``: auto-deprecate the colliding concept
        first (status='deprecated', deprecatedAt stamped) and then create
        the new one. Used by the Accept-as-concept UI path (single + bulk)
        so the Steward isn't forced through a manual two-step.
    """
    ensure_constraints(settings)
    if not name or not definition or not domain:
        raise ValueError("name, definition, and domain are required")
    # Legacy 'super' is tolerated as an alias for 'attribute' (pre-3-tier rows).
    if level == "super":
        level = "attribute"
    if level not in ("entity", "attribute", "value"):
        raise ValueError("level must be 'entity', 'attribute', or 'value'")
    if level == "value" and not value_token and not predicate_template:
        # Values match a literal in the bound column; an attribute binds to a
        # column (no token) and an entity binds to a table (no token).
        raise ValueError("value-level concepts require value_token or predicate_template")
    # Parent-edge type is implied by the child's level:
    #   attribute's parent is an entity (:HAS_ATTRIBUTE); value's is an attribute (:HAS_VALUE).
    parent_edge = {"attribute": "HAS_ATTRIBUTE", "value": "HAS_VALUE"}.get(level, "")

    new_uri = uri or _build_uri(domain, name)
    deprecated_uri: Optional[str] = None
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        dup, exact_match = _find_collision(ns, domain, name, new_uri, level)
        collision_action: Optional[str] = None
        if dup:
            # Exact name match runs the full tier logic; a normalized-only match
            # (employees→Employee) can ONLY coreference a higher tier.
            collision_action = (
                _collision_action(dup["level"], level, if_exists)
                if exact_match else "coreference"
            )
            if collision_action == "coreference":
                # A HIGHER-tier concept already owns this name (e.g. an 'Employee'
                # entity while we create an 'Employee' attribute). Recognise
                # coreference: the incoming concept IS that concept. Create/deprecate
                # nothing — this is the fix for the recommend step clobbering the
                # deterministic entity scaffold.
                return {"uri": dup["uri"], "name": dup["name"],
                        "level": dup.get("level") or "attribute",
                        "status": "active", "coreferenced": True, "created": False}
            if collision_action == "fail":
                raise DuplicateConceptError(dup["uri"], dup["name"], domain)
            # 'replace' (same tier) or 'promote_over' (creating a higher tier) →
            # deprecate the colliding node.
            ns.run(_DEPRECATE_EXISTING_FOR_REPLACE, old_uri=dup["uri"]).consume()
            deprecated_uri = dup["uri"]
        cleaned_synonyms = [str(s).strip() for s in (synonyms or []) if str(s).strip()][:12]
        row = ns.run(
            _CREATE_CONCEPT,
            uri=new_uri,
            name=name,
            definition=definition,
            domain=domain,
            level=level,
            value_token=value_token or "",
            predicate_template=predicate_template or "",
            parent_uri=parent_uri or "",
            parent_edge=parent_edge,
            represented_by_uris=represented_by_uris or [],
            represented_by_dataset_uris=represented_by_dataset_uris or [],
            synonyms_json=json.dumps(cleaned_synonyms),
            created_by=created_by,
            rec_uri=rec_uri,
        ).single()
        migrated_children = 0
        # Only migrate children on a SAME-tier replace — never across tiers (a
        # deprecated attribute's values must not be re-homed under a new entity).
        if deprecated_uri and collision_action == "replace" and level in ("entity", "attribute"):
            mig = ns.run(
                _MIGRATE_HAS_VALUE_EDGES,
                old_uri=deprecated_uri,
                new_uri=new_uri,
            ).single()
            migrated_children = (mig or {}).get("migrated", 0) or 0
        # Embed the concept for Concept-Guided retrieval. Best-effort: if the
        # model isn't available the node simply carries no embedding and is
        # invisible to vector search (Full Context still sees it).
        _embed_and_store(ns, new_uri, name, definition, cleaned_synonyms)
    result = dict(row) if row else {"uri": new_uri, "status": "active", "name": name}
    if deprecated_uri:
        result["deprecated_existing_uri"] = deprecated_uri
        if migrated_children:
            result["migrated_children"] = migrated_children
    return result


# ── Embeddings (Concept-Guided retrieval) ───────────────────────────────────

_SET_EMBEDDING = """
MATCH (c:BusinessConcept {uri: $uri})
SET c.embedding = $vec, c.embeddedAt = datetime()
"""


def _embed_and_store(ns, uri: str, name: str, definition: str, synonyms: list[str]) -> bool:
    """Compute + persist the concept embedding within an open session.
    Returns True on success. Never raises — embedding is always optional."""
    if not embeddings.available():
        return False
    try:
        text = embeddings.concept_embedding_text(name, definition, synonyms)
        vecs = embeddings.embed_documents([text])
        if not vecs:
            return False
        ns.run(_SET_EMBEDDING, uri=uri, vec=vecs[0]).consume()
        return True
    except Exception:
        return False


_VECTOR_SEARCH = """
CALL db.index.vector.queryNodes($index, $fetch_k, $vec) YIELD node, score
WHERE node.domain = $domain
  AND coalesce(node.status, 'active') = 'active'
  AND coalesce(node.level, 'attribute') IN ['entity', 'attribute']
  AND score >= $min_score
RETURN node.uri AS uri, node.name AS name, coalesce(node.level, 'attribute') AS level, score
ORDER BY score DESC
LIMIT $k
"""


def search_concepts_by_vector(
    settings: AppSettings, domain: str, query_vec: list[float],
    k: int = 5, min_score: float = 0.0,
) -> list[dict[str, Any]]:
    """Vector-search active super-concepts in ``domain`` for ``query_vec``.
    Returns ``[{uri, name, score}]`` best-first. Empty list if the index is
    missing or the search errors (caller falls back to Full Context)."""
    if not query_vec:
        return []
    try:
        with neo4j_session(
            settings.neo4j_host, settings.neo4j_port,
            settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
        ) as ns:
            # Over-fetch from the index then filter — deprecated concepts stay
            # in the vector index, so the raw nearest-k can be dead duplicates.
            rows = list(ns.run(
                _VECTOR_SEARCH,
                index=VECTOR_INDEX_NAME, k=max(1, k), fetch_k=max(20, k * 6),
                vec=query_vec, domain=domain, min_score=min_score,
            ))
        return [dict(r) for r in rows]
    except Exception:
        return []


_NEIGHBOR_QUERY = """
UNWIND $uris AS u
MATCH (c:BusinessConcept {uri: u})-[r:RELATES_TO]-(n:BusinessConcept)
WHERE coalesce(r.status, 'pending') = 'active'
  AND coalesce(n.status, 'active') = 'active'
  AND coalesce(n.level, 'entity') = 'entity'
RETURN DISTINCT n.uri AS uri
"""


_RESOLVE_ENTITY = """
UNWIND $uris AS u
MATCH (c:BusinessConcept {uri: u})
OPTIONAL MATCH (c)<-[:HAS_ATTRIBUTE]-(e1:BusinessConcept)
OPTIONAL MATCH (c)<-[:HAS_VALUE]-(:BusinessConcept)<-[:HAS_ATTRIBUTE]-(e2:BusinessConcept)
WITH coalesce(
       CASE WHEN coalesce(c.level, 'attribute') = 'entity' THEN c.uri ELSE null END,
       e1.uri, e2.uri) AS entity_uri
WHERE entity_uri IS NOT NULL
RETURN DISTINCT entity_uri AS uri
"""


def resolve_entities(settings: AppSettings, uris: list[str]) -> list[str]:
    """Map any concept URIs to their owning entity URIs: an entity resolves to
    itself, an attribute to its parent entity, a value to its grandparent
    entity. Used so Concept-Guided retrieval (which may match attributes) hands
    the chat entity-shaped concepts. Errors degrade to []."""
    if not uris:
        return []
    try:
        with neo4j_session(
            settings.neo4j_host, settings.neo4j_port,
            settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
        ) as ns:
            rows = list(ns.run(_RESOLVE_ENTITY, uris=list(uris)))
        return [r["uri"] for r in rows]
    except Exception:
        return []


_RESOLVE_ENTITY_MAP = """
UNWIND $uris AS u
MATCH (c:BusinessConcept {uri: u})
OPTIONAL MATCH (c)<-[:HAS_ATTRIBUTE]-(e1:BusinessConcept)
OPTIONAL MATCH (c)<-[:HAS_VALUE]-(:BusinessConcept)<-[:HAS_ATTRIBUTE]-(e2:BusinessConcept)
WITH u AS source_uri, coalesce(
       CASE WHEN coalesce(c.level, 'attribute') = 'entity' THEN c.uri ELSE null END,
       e1.uri, e2.uri) AS entity_uri
WHERE entity_uri IS NOT NULL
RETURN DISTINCT source_uri, entity_uri
"""


def resolve_entity_map(settings: AppSettings, uris: list[str]) -> dict[str, str]:
    """Like ``resolve_entities`` but keeps the origin: ``{source_uri: entity_uri}``.
    Lets a caller carry a matched hit's similarity score onto its owning entity
    even when an *attribute* (not the entity) was the vector match. First entity
    wins per source. Errors degrade to ``{}``."""
    if not uris:
        return {}
    try:
        with neo4j_session(
            settings.neo4j_host, settings.neo4j_port,
            settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
        ) as ns:
            rows = list(ns.run(_RESOLVE_ENTITY_MAP, uris=list(uris)))
        out: dict[str, str] = {}
        for r in rows:
            out.setdefault(r["source_uri"], r["entity_uri"])
        return out
    except Exception:
        return {}


def expand_neighbors(settings: AppSettings, uris: list[str]) -> list[str]:
    """Return URIs of super-concepts one active :RELATES_TO hop from ``uris``
    (either direction). Excludes the input set. Empty until Phase 4 edges
    exist; errors degrade to []."""
    if not uris:
        return []
    try:
        with neo4j_session(
            settings.neo4j_host, settings.neo4j_port,
            settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
        ) as ns:
            rows = list(ns.run(_NEIGHBOR_QUERY, uris=list(uris)))
        seen = set(uris)
        return [r["uri"] for r in rows if r["uri"] not in seen]
    except Exception:
        return []


# ── Concept-to-concept relationships ────────────────────────────────────────

_RESOLVE_CONCEPT_BY_NAME = """
MATCH (c:BusinessConcept)
WHERE c.domain = $domain
  AND toLower(c.name) = toLower($name)
  AND coalesce(c.status, 'active') = 'active'
  AND coalesce(c.level, 'attribute') = 'entity'
RETURN c.uri AS uri LIMIT 1
"""

# Directed edge, but we MERGE only one direction and traverse both ways in
# expand_neighbors. Re-seeding an existing edge never downgrades an already
# active/rejected status back to pending.
_MERGE_RELATIONSHIP = """
MATCH (a:BusinessConcept {uri: $from_uri})
MATCH (b:BusinessConcept {uri: $to_uri})
MERGE (a)-[r:RELATES_TO {kind: $kind}]->(b)
ON CREATE SET r.status = $status, r.createdBy = $created_by, r.createdAt = datetime()
SET r.via_column = coalesce($via_column, r.via_column)
RETURN coalesce(r.status, 'pending') AS status
"""


def seed_relationships_from_recommendation(
    settings: AppSettings, from_uri: str, domain: str,
    suggested: list[dict[str, Any]], created_by: str = "Data Steward",
) -> list[dict[str, Any]]:
    """For each ``{to_concept, kind}`` suggestion whose target name resolves to
    an existing active concept in ``domain``, MERGE a *pending* :RELATES_TO
    edge. Unresolved targets are stashed as ``relationshipSuggestionsJson`` on
    the source concept for later resolution. Returns the seeded edges."""
    if not suggested:
        return []
    seeded: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        for rel in suggested:
            if not isinstance(rel, dict):
                continue
            to_name = (rel.get("to_concept") or "").strip()
            kind = (rel.get("kind") or "related_to").strip() or "related_to"
            if not to_name:
                continue
            row = ns.run(_RESOLVE_CONCEPT_BY_NAME, domain=domain, name=to_name).single()
            if not row or row["uri"] == from_uri:
                unresolved.append({"to_concept": to_name, "kind": kind})
                continue
            ns.run(
                _MERGE_RELATIONSHIP,
                from_uri=from_uri, to_uri=row["uri"], kind=kind,
                status="pending", created_by=created_by, via_column=None,
            ).consume()
            seeded.append({"to_uri": row["uri"], "to_concept": to_name, "kind": kind})
        if unresolved:
            ns.run(
                "MATCH (c:BusinessConcept {uri: $uri}) SET c.relationshipSuggestionsJson = $json",
                uri=from_uri, json=json.dumps(unresolved),
            ).consume()
    return seeded


_LIST_RELATIONSHIPS = """
MATCH (a:BusinessConcept)-[r:RELATES_TO]->(b:BusinessConcept)
WHERE a.domain = $domain
  AND coalesce(a.status, 'active') = 'active'
  AND coalesce(b.status, 'active') = 'active'
  AND ($include_rejected OR coalesce(r.status, 'pending') <> 'rejected')
RETURN a.uri AS from_uri, a.name AS from_name,
       b.uri AS to_uri, b.name AS to_name,
       r.kind AS kind, coalesce(r.status, 'pending') AS status,
       r.via_column AS via_column
ORDER BY status, from_name, to_name
"""


def list_relationships(
    settings: AppSettings, domain: str, include_rejected: bool = False,
) -> list[dict[str, Any]]:
    """List :RELATES_TO edges within ``domain`` (pending + active by default)."""
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        rows = list(ns.run(_LIST_RELATIONSHIPS, domain=domain, include_rejected=include_rejected))
    return [dict(r) for r in rows]


_SET_RELATIONSHIP_STATUS = """
MATCH (a:BusinessConcept {uri: $from_uri})-[r:RELATES_TO {kind: $kind}]->(b:BusinessConcept {uri: $to_uri})
SET r.status = $status, r.reviewedAt = datetime()
RETURN coalesce(r.status, 'pending') AS status
"""


def set_relationship_status(
    settings: AppSettings, from_uri: str, to_uri: str, kind: str, status: str,
    via_column: Optional[str] = None,
) -> dict[str, Any]:
    """Approve ('active'), reject ('rejected'), or reset ('pending') an edge.
    Creates the edge if it doesn't exist yet — supports steward-authored and
    scaffolder reference relationships (the latter pass ``via_column``)."""
    if status not in ("active", "rejected", "pending"):
        raise ValueError("status must be 'active', 'rejected', or 'pending'")
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        # MERGE first so via_column is set even when the edge already exists.
        ns.run(
            _MERGE_RELATIONSHIP,
            from_uri=from_uri, to_uri=to_uri, kind=kind,
            status=status, created_by="Data Steward", via_column=via_column,
        ).consume()
        ns.run(
            _SET_RELATIONSHIP_STATUS,
            from_uri=from_uri, to_uri=to_uri, kind=kind, status=status,
        ).consume()
    return {"from_uri": from_uri, "to_uri": to_uri, "kind": kind, "status": status, "via_column": via_column}


def backfill_embeddings(settings: AppSettings, domain: Optional[str] = None) -> dict[str, Any]:
    """Embed every active concept (optionally scoped to ``domain``) that the
    model can handle. Idempotent — re-runs overwrite the vector. Used to embed
    concepts created before this shipped, via the API endpoint / CLI script."""
    ensure_constraints(settings)
    if not embeddings.available():
        return {"embedded": 0, "skipped": 0, "available": False}
    q = (
        "MATCH (c:BusinessConcept) "
        "WHERE coalesce(c.status, 'active') = 'active' "
        + ("AND c.domain = $domain " if domain else "")
        + "RETURN c.uri AS uri, c.name AS name, coalesce(c.definition,'') AS definition, "
          "c.synonymsJson AS synonyms_json"
    )
    embedded = 0
    skipped = 0
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        rows = list(ns.run(q, domain=domain) if domain else ns.run(q))
        for r in rows:
            rd = dict(r)
            try:
                syn = json.loads(rd.get("synonyms_json") or "[]") or []
            except (TypeError, ValueError):
                syn = []
            if _embed_and_store(ns, rd["uri"], rd.get("name") or "", rd.get("definition") or "", syn):
                embedded += 1
            else:
                skipped += 1
    return {"embedded": embedded, "skipped": skipped, "available": True}


_READ_RECOMMENDATION = """
MATCH (bcr:BusinessConceptRecommendation {uri: $rec_uri})
OPTIONAL MATCH (br:RecommendationBatch)-[:HAS_RECOMMENDATION]->(bcr)
RETURN bcr.conceptName AS name,
       bcr.definition AS definition,
       bcr.evidenceColumnsJson AS evidence_columns_json,
       bcr.synonymsJson AS synonyms_json,
       bcr.valuesJson AS values_json,
       bcr.suggestedRelationshipsJson AS suggested_relationships_json,
       br.uri AS batch_uri
"""


_MARK_RECOMMENDATION_ACCEPTED = """
MATCH (bcr:BusinessConceptRecommendation {uri: $rec_uri})
SET bcr.acceptedAsConcept = $concept_uri,
    bcr.status = 'accepted',
    bcr.acceptedAt = datetime()
"""


def accept_from_recommendation(
    settings: AppSettings,
    *,
    rec_uri: str,
    name: Optional[str] = None,
    definition: Optional[str] = None,
    domain: str,                                 # caller-supplied (the recommender doesn't tag domain per concept)
    level: str = "attribute",
    value_token: Optional[str] = None,
    predicate_template: Optional[str] = None,
    parent_uri: Optional[str] = None,
    represented_by_uris: Optional[list[str]] = None,
    synonyms: Optional[list[str]] = None,
    created_by: str = "Data Steward",
    if_exists: Literal["fail", "deprecate_existing"] = "fail",
) -> dict[str, Any]:
    """Read a :BusinessConceptRecommendation, promote it to a real
    :BusinessConcept, and mark the recommendation as ``accepted`` with
    a back-link to the concept URI."""
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        row = ns.run(_READ_RECOMMENDATION, rec_uri=rec_uri).single()
        if not row:
            raise ValueError(f"Recommendation {rec_uri!r} not found")
        rec = dict(row)

    eff_name = (name or rec.get("name") or "").strip()
    eff_def = (definition or rec.get("definition") or "").strip()
    if not eff_name or not eff_def:
        raise ValueError("Promoted concept needs both name and definition")

    if represented_by_uris is None:
        # Default to the recommendation's evidence columns.
        try:
            ev = json.loads(rec.get("evidence_columns_json") or "[]") or []
        except (TypeError, ValueError):
            ev = []
        represented_by_uris = [e.get("column_uri") for e in ev if isinstance(e, dict) and e.get("column_uri")]

    if synonyms is None:
        # Default to the recommendation's synonyms — caller can override.
        try:
            synonyms = json.loads(rec.get("synonyms_json") or "[]") or []
        except (TypeError, ValueError):
            synonyms = []

    result = create_concept(
        settings,
        name=eff_name, definition=eff_def, domain=domain, level=level,
        value_token=value_token, predicate_template=predicate_template,
        parent_uri=parent_uri, represented_by_uris=represented_by_uris,
        synonyms=synonyms,
        created_by=created_by, rec_uri=rec_uri,
        if_exists=if_exists,
    )

    # Materialise any value-concepts the recommender proposed as children of
    # this attribute. Only attributes carry values; entities/values don't nest
    # values. Dedup-guard catches names already present in the same domain.
    # Skip entirely when the promotion coreferenced an existing higher-tier
    # concept (an entity) — we must not graft values onto an entity, and the
    # concept already exists.
    values_created: list[dict[str, Any]] = []
    if level == "attribute" and not result.get("coreferenced"):
        try:
            rec_values = json.loads(rec.get("values_json") or "[]") or []
        except (TypeError, ValueError):
            rec_values = []
        for v in rec_values:
            if not isinstance(v, dict):
                continue
            v_name = (v.get("name") or "").strip()
            v_token = (v.get("value_token") or "").strip()
            v_def = (v.get("definition") or "").strip() or f"{v_name} — value of {eff_name}."
            if not v_name or not v_token:
                continue
            try:
                child = create_concept(
                    settings,
                    name=v_name, definition=v_def, domain=domain, level="value",
                    value_token=v_token,
                    parent_uri=result["uri"],
                    created_by=created_by,
                )
                values_created.append({"uri": child["uri"], "name": v_name, "value_token": v_token})
            except DuplicateConceptError:
                # Don't fail the whole accept just because one value is a dup.
                # The Steward can review the rest; this one is already in the graph.
                continue

    # Link the recommendation back to the concept.
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        ns.run(_MARK_RECOMMENDATION_ACCEPTED,
               rec_uri=rec_uri, concept_uri=result["uri"]).consume()

    # Seed pending :RELATES_TO edges from the recommendation's suggested
    # relationships (entity-level only — relationships connect entities).
    if level == "entity":
        try:
            rels = json.loads(rec.get("suggested_relationships_json") or "[]") or []
        except (TypeError, ValueError):
            rels = []
        seeded = seed_relationships_from_recommendation(
            settings, result["uri"], domain, rels, created_by=created_by,
        )
        if seeded:
            result["relationships_seeded"] = seeded

    if values_created:
        result["values_created"] = values_created
    return result


# ── Bulk accept (all pending in latest batch) ─────────────────────────────


# Pull every pending recommendation from the LATEST :RecommendationBatch
# along with its evidence_columns_json. We also resolve the dominant
# source-product domain for each rec's evidence columns: walk
# :DProdColumn → :DProdOutputDataset → :DProdOutputPort → :DProdDataProduct
# ← :DataContract, take :DataContract.domain. The result is grouped per
# rec; ``inferred_domain`` is the modal domain across evidence (ties
# break alphabetically); empty when no evidence column resolves.
_READ_PENDING_FOR_BULK = """
MATCH (br:RecommendationBatch)
WITH br ORDER BY br.evaluatedAt DESC LIMIT 1
MATCH (br)-[:HAS_RECOMMENDATION]->(bcr:BusinessConceptRecommendation)
WHERE coalesce(bcr.status, 'pending') = 'pending'
WITH br, bcr
OPTIONAL MATCH (bcr)-[:EVIDENCED_BY]->(pc:DProdColumn)
      <-[:HAS_PRODUCT_COLUMN]-(:DProdOutputDataset)
      <-[:DPROD_OUTPUT_DATASET]-(:DProdOutputPort)
      <-[:DPROD_OUTPUT_PORT]-(:DProdDataProduct)
      <-[:MATERIALISES_AS]-(dc:DataContract)
WITH br, bcr, collect(dc.domain) AS domains_raw
RETURN br.uri AS batch_uri,
       bcr.uri AS rec_uri,
       bcr.conceptName AS name,
       bcr.definition AS definition,
       [d IN domains_raw WHERE d IS NOT NULL AND trim(d) <> ''] AS domains
ORDER BY bcr.confidence DESC, bcr.conceptName
"""


def _modal_domain(domains: list[str]) -> Optional[str]:
    """Pick the most-cited domain across a rec's evidence columns.

    Ties break alphabetically so the choice is deterministic across
    re-runs. Returns ``None`` when no evidence column resolved to a
    domain (the caller treats this as "skip — no inferrable domain")."""
    if not domains:
        return None
    counts: dict[str, int] = {}
    for d in domains:
        if not d:
            continue
        counts[d] = counts.get(d, 0) + 1
    if not counts:
        return None
    # max by (count, alphabetical fallback). sort gives deterministic ties.
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def accept_all_pending(
    settings: AppSettings,
    *,
    created_by: str = "Data Steward",
    domain_overrides: Optional[dict[str, str]] = None,
    fallback_domain: Optional[str] = None,
) -> dict[str, Any]:
    """Accept every pending recommendation in the latest :RecommendationBatch.

    Per-rec domain is inferred from evidence-column lineage; callers can
    supply ``domain_overrides`` keyed by rec_uri to override the inferred
    value, and ``fallback_domain`` for recs whose evidence couldn't be
    resolved. Each accept uses ``if_exists='deprecate_existing'`` so name
    collisions auto-deprecate the prior concept instead of failing the
    batch.

    Returns ``{accepted: [...], skipped: [...], failures: [...]}`` so the
    UI can surface a triage summary without re-querying.
    """
    overrides = domain_overrides or {}
    fallback = (fallback_domain or "").strip() or None

    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        rows = [dict(r) for r in ns.run(_READ_PENDING_FOR_BULK)]

    accepted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for row in rows:
        rec_uri = row["rec_uri"]
        chosen = overrides.get(rec_uri) or _modal_domain(row.get("domains") or []) or fallback
        if not chosen:
            skipped.append({
                "rec_uri": rec_uri,
                "name": row.get("name"),
                "reason": "no_inferrable_domain",
            })
            continue
        try:
            result = accept_from_recommendation(
                settings,
                rec_uri=rec_uri,
                domain=chosen,
                created_by=created_by,
                if_exists="deprecate_existing",
            )
            accepted.append({
                "rec_uri": rec_uri,
                "name": row.get("name"),
                "concept_uri": result.get("uri"),
                "domain": chosen,
                "deprecated_existing_uri": result.get("deprecated_existing_uri"),
            })
        except Exception as e:
            failures.append({
                "rec_uri": rec_uri,
                "name": row.get("name"),
                "domain": chosen,
                "error": str(e),
            })

    return {"accepted": accepted, "skipped": skipped, "failures": failures}


# ── Read / tree ────────────────────────────────────────────────────────────


_TREE_QUERY = """
MATCH (c:BusinessConcept)
WHERE ($domain IS NULL OR c.domain = $domain)
  AND coalesce(c.status, 'active') <> 'deprecated'
// Parent is via either hierarchy edge: entity-[:HAS_ATTRIBUTE]->attribute-[:HAS_VALUE]->value
OPTIONAL MATCH (c)<-[:HAS_ATTRIBUTE|HAS_VALUE]-(parent:BusinessConcept)
OPTIONAL MATCH (c)-[:REPRESENTED_BY]->(col:DProdColumn)
OPTIONAL MATCH (c)-[:REPRESENTED_BY]->(ds:DProdOutputDataset)
WITH c, parent.uri AS parent_uri,
     collect(DISTINCT CASE WHEN col IS NULL THEN NULL ELSE {
       column_uri: col.uri, column_name: col.name
     } END) AS reps_raw,
     collect(DISTINCT CASE WHEN ds IS NULL THEN NULL ELSE {
       dataset_uri: ds.uri, physical_name: ds.physicalName,
       relationship_kind: coalesce(ds.relationshipKind, 'unknown')
     } END) AS ds_raw
RETURN c.uri AS uri,
       c.name AS name,
       c.definition AS definition,
       CASE WHEN c.level = 'super' THEN 'attribute' ELSE coalesce(c.level, 'attribute') END AS level,
       c.value_token AS value_token,
       c.predicate_template AS predicate_template,
       c.domain AS domain,
       coalesce(c.status, 'active') AS status,
       toString(c.createdAt) AS created_at,
       c.createdBy AS created_by,
       c.promotedFromRecommendationUri AS promoted_from,
       c.synonymsJson AS synonyms_json,
       parent_uri,
       [r IN reps_raw WHERE r IS NOT NULL] AS represented_by,
       [d IN ds_raw WHERE d IS NOT NULL] AS represented_by_datasets
ORDER BY CASE coalesce(c.level,'attribute') WHEN 'entity' THEN 0 WHEN 'attribute' THEN 1 ELSE 2 END, c.name
"""


def get_tree(settings: AppSettings, domain: Optional[str] = None) -> list[dict[str, Any]]:
    """Return the active concept tree, nested as
    ``[{...super, children: [...values]}, ...]``.

    When ``domain`` is None or empty, returns concepts across every
    domain in one flat list (each concept retains its ``domain`` field
    so the UI can chip it). Deprecated concepts are excluded. Values
    without a parent are included as orphan super-concepts at the top
    level so the Steward can fix them up rather than losing them
    silently."""
    domain_param = domain if (domain and domain.strip()) else None
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        rows = [dict(r) for r in ns.run(_TREE_QUERY, domain=domain_param)]

    by_uri: dict[str, dict[str, Any]] = {}
    for r in rows:
        try:
            r["synonyms"] = json.loads(r.pop("synonyms_json", None) or "[]") or []
        except (TypeError, ValueError):
            r["synonyms"] = []
        r["children"] = []
        by_uri[r["uri"]] = r

    roots: list[dict[str, Any]] = []
    for r in rows:
        parent = r.get("parent_uri")
        if parent and parent in by_uri:
            by_uri[parent]["children"].append(r)
        else:
            roots.append(r)
    return roots


# ── Cross-domain CONSUMES (for the all-domains ontology diagram) ─────────────


# Keep only the temporal-currency window (the edge is live in the consumer's
# current contract version). Unlike the marketplace consumer-side query we do
# NOT gate on currentLifecycleState — this is an internal steward/ontology view
# and SHOULD surface in-flight (draft) cross-domain consumption too.
_CROSS_DOMAIN_CONSUMES = """
MATCH (cons_dc:DataContract)-[r:CONSUMES]->(prod:DProdDataProduct)<-[:MATERIALISES_AS]-(src_dc:DataContract)
WHERE r.fromVersion <= cons_dc.currentVersion
  AND (r.toVersion IS NULL OR r.toVersion >= cons_dc.currentVersion)
WITH coalesce(cons_dc.domain, '') AS consumer_domain,
     coalesce(src_dc.domain, '')  AS source_domain,
     coalesce(prod.name, '')      AS product_name
WHERE consumer_domain <> '' AND source_domain <> '' AND consumer_domain <> source_domain
RETURN DISTINCT consumer_domain, source_domain, product_name
ORDER BY consumer_domain, source_domain
"""


def list_cross_domain_consumes(settings: AppSettings) -> list[dict[str, Any]]:
    """Cross-domain :CONSUMES edges, where a consumer-aligned product in one
    domain consumes a source product in another. Used only by the all-domains
    ontology diagram to draw domain→domain "consumes" arrows (the amalgamation
    story). Same temporal/lifecycle filter as the marketplace consumer side.
    Self-domain edges are excluded."""
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        rows = list(ns.run(_CROSS_DOMAIN_CONSUMES))
    return [dict(r) for r in rows]


# ── Deprecate / status ─────────────────────────────────────────────────────


_DEPRECATE = """
MATCH (c:BusinessConcept {uri: $uri})
SET c.status = 'deprecated',
    c.deprecatedAt = datetime()
REMOVE c.embedding
RETURN c.uri AS uri, c.status AS status
"""


# Count + list active VALUE children before a manual deprecate. We refuse
# rather than silently orphaning them — the Steward should explicitly
# move or deprecate the children first. The active-replacement path
# (if_exists='deprecate_existing' in create_concept) migrates children
# automatically and bypasses this guard.
_ACTIVE_CHILDREN_CHECK = """
MATCH (c:BusinessConcept {uri: $uri})-[:HAS_VALUE]->(v:BusinessConcept)
WHERE coalesce(v.status, 'active') <> 'deprecated'
RETURN collect({uri: v.uri, name: v.name}) AS children
"""


class ConceptHasActiveChildrenError(Exception):
    """Raised when trying to deprecate a SUPER that still has active children.

    The router translates this to HTTP 409 with the child list so the
    Steward can move or deprecate them first.
    """

    def __init__(self, uri: str, children: list[dict[str, str]]):
        self.uri = uri
        self.children = children
        names = ", ".join(c.get("name", "?") for c in children[:5])
        suffix = "" if len(children) <= 5 else f" (+{len(children) - 5} more)"
        super().__init__(
            f"Concept {uri!r} has {len(children)} active child VALUE concept(s): "
            f"{names}{suffix}. Deprecate or re-parent them first."
        )


def deprecate_concept(settings: AppSettings, uri: str) -> dict[str, Any]:
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        check = ns.run(_ACTIVE_CHILDREN_CHECK, uri=uri).single()
        children = (check or {}).get("children", []) if check else []
        if children:
            raise ConceptHasActiveChildrenError(uri, children)
        row = ns.run(_DEPRECATE, uri=uri).single()
    return dict(row) if row else {"status": "not_found"}


_DEPRECATE_DOMAIN = """
MATCH (c:BusinessConcept)
WHERE c.domain = $domain AND coalesce(c.status, 'active') <> 'deprecated'
SET c.status = 'deprecated',
    c.deprecatedAt = datetime(),
    c.deprecatedReason = coalesce(c.deprecatedReason, $reason)
REMOVE c.embedding
RETURN count(c) AS deprecated
"""


_UPDATE_ENRICHMENT = """
MATCH (c:BusinessConcept {uri: $uri})
SET c.name = coalesce($name, c.name),
    c.definition = coalesce($definition, c.definition),
    c.synonymsJson = coalesce($synonyms_json, c.synonymsJson),
    c.value_token = coalesce($value_token, c.value_token),
    c.predicate_template = coalesce($predicate_template, c.predicate_template),
    c.enrichedAt = datetime()
RETURN c.uri AS uri, c.name AS name, coalesce(c.definition,'') AS definition,
       coalesce(c.synonymsJson, '[]') AS synonyms_json
"""


def update_concept_enrichment(
    settings: AppSettings, uri: str, *,
    name: Optional[str] = None, definition: Optional[str] = None,
    synonyms: Optional[list[str]] = None,
    value_token: Optional[str] = None, predicate_template: Optional[str] = None,
) -> bool:
    """In-place edit/enrich a concept's name/definition/synonyms (and, for value
    concepts, value_token/predicate_template) and re-embed — WITHOUT touching
    its :REPRESENTED_BY / :HAS_ATTRIBUTE edges (unlike create_concept, which
    wipes bindings). Only non-None fields change. Returns True on success."""
    syn_json = None
    if synonyms is not None:
        cleaned = [str(s).strip() for s in synonyms if str(s).strip()][:12]
        syn_json = json.dumps(cleaned)
    try:
        with neo4j_session(
            settings.neo4j_host, settings.neo4j_port,
            settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
        ) as ns:
            row = ns.run(
                _UPDATE_ENRICHMENT, uri=uri,
                name=(name.strip() if name and name.strip() else None),
                definition=(definition.strip() if definition and definition.strip() else None),
                synonyms_json=syn_json,
                value_token=(value_token.strip() if value_token and value_token.strip() else None),
                predicate_template=(predicate_template if predicate_template is not None else None),
            ).single()
            if not row:
                return False
            try:
                syn = json.loads(row["synonyms_json"] or "[]") or []
            except (TypeError, ValueError):
                syn = []
            _embed_and_store(ns, uri, row["name"], row["definition"], syn)
        return True
    except Exception:
        return False


def deprecate_domain_concepts(
    settings: AppSettings, domain: str,
    reason: str = "start-fresh: superseded by entity scaffolding",
) -> int:
    """Bulk-deprecate every active concept in a domain. Used by the
    entity-scaffold Apply (start-fresh) before re-creating the tree. Soft —
    nodes stay for provenance but drop out of tree/chat reads. Idempotent."""
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        row = ns.run(_DEPRECATE_DOMAIN, domain=domain, reason=reason).single()
    return (row or {}).get("deprecated", 0) or 0


# ── Chat-prep helper ───────────────────────────────────────────────────────


# Returns the agent-ready ENTITY-shaped payload: one row per active entity in
# the domain, each carrying its table binding(s), its attributes (each with
# value children + bound columns), and its active entity-to-entity
# relationships (the join graph). Built with nested pattern comprehensions so
# the whole tree comes back in one row per entity. When `contract_id` is set,
# attribute column bindings are narrowed to that product (URI prefix match).
_CHAT_CONCEPTS_QUERY = """
MATCH (e:BusinessConcept)
WHERE coalesce(e.status, 'active') = 'active'
  AND coalesce(e.level, 'attribute') = 'entity'
  // Full Context: all entities in the domain. By-uris (Concept-Guided): the
  // explicit URI set is the authorization, so cross-domain 'shared' reference
  // entities (e.g. Country) pulled in as neighbours are included.
  AND ( ($uris IS NULL AND e.domain = $domain)
        OR ($uris IS NOT NULL AND e.uri IN $uris) )
RETURN e.uri AS uri,
       e.name AS name,
       coalesce(e.definition, '') AS definition,
       e.synonymsJson AS synonyms_json,
       [ (e)-[:REPRESENTED_BY]->(ds:DProdOutputDataset) |
         {dataset_uri: ds.uri, physical_name: ds.physicalName,
          relationship_kind: coalesce(ds.relationshipKind, 'unknown')} ] AS table_bindings,
       [ (e)-[:HAS_ATTRIBUTE]->(a:BusinessConcept)
         WHERE coalesce(a.status, 'active') = 'active' |
         {
           uri: a.uri, name: a.name, definition: coalesce(a.definition, ''),
           represented_by_columns: [ (a)-[:REPRESENTED_BY]->(col:DProdColumn)
             WHERE $contract_id IS NULL OR $contract_id = '' OR col.uri CONTAINS $contract_id |
             {column_uri: col.uri, column_name: col.name} ],
           values: [ (a)-[:HAS_VALUE]->(v:BusinessConcept)
             WHERE coalesce(v.status, 'active') = 'active' |
             {uri: v.uri, name: v.name, value_token: coalesce(v.value_token, ''),
              predicate_template: coalesce(v.predicate_template, '')} ]
         } ] AS attributes,
       [ (e)-[r:RELATES_TO]-(e2:BusinessConcept)
         WHERE coalesce(r.status, 'pending') = 'active'
           AND coalesce(e2.status, 'active') = 'active'
           AND coalesce(e2.level, 'attribute') = 'entity' |
         {kind: r.kind, to_uri: e2.uri, to_name: e2.name, via_column: r.via_column} ] AS relationships
ORDER BY e.name
"""


def list_concepts_for_chat(
    settings: AppSettings, domain: str, contract_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return the concept payload the marketplace-chat skill expects.

    Filters to active super-concepts in ``domain`` with their active
    children. When ``contract_id`` is set, narrows the
    ``represented_by_columns`` list to columns whose URI references
    that contract (the dprod URI naming scheme tags every column with
    its contract id)."""
    return _run_chat_concepts_query(settings, domain, contract_id, uris=None)


def list_concepts_for_chat_by_uris(
    settings: AppSettings, domain: str, uris: list[str], contract_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Same payload as :func:`list_concepts_for_chat` but restricted to a
    specific set of super-concept URIs (the Concept-Guided retrieval result:
    vector-matched concepts ∪ their 1-hop neighbours). Returns ``[]`` when
    ``uris`` is empty so the caller can fall back."""
    if not uris:
        return []
    return _run_chat_concepts_query(settings, domain, contract_id, uris=list(uris))


def _run_chat_concepts_query(
    settings: AppSettings, domain: str, contract_id: Optional[str], uris: Optional[list[str]],
) -> list[dict[str, Any]]:
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        rows = list(ns.run(_CHAT_CONCEPTS_QUERY, domain=domain, contract_id=contract_id, uris=uris))
    out: list[dict[str, Any]] = []
    for r in rows:
        rd = dict(r)
        try:
            syn = json.loads(rd.get("synonyms_json") or "[]") or []
        except (TypeError, ValueError):
            syn = []
        out.append({
            "uri": rd.get("uri"),
            "name": rd.get("name"),
            "definition": rd.get("definition") or "",
            "level": "entity",
            "synonyms": syn,
            "table_bindings": rd.get("table_bindings") or [],
            "attributes": rd.get("attributes") or [],
            "relationships": rd.get("relationships") or [],
        })
    return out


def ontology_for_grounding(settings: AppSettings, domain: str) -> list[dict[str, Any]]:
    """Project a domain's :BusinessConcept tree to an ONTOLOGY-ONLY shape for a
    conversational router to reason about — entities → attributes → values with
    their definitions, synonyms, value tokens, and entity↔entity relationships,
    but WITHOUT any data-product bindings (no ``table_bindings``, no
    ``represented_by_columns``, no ``via_column``). This is what grounds the
    agent's judgment about whether a question is in-domain / answerable /
    ambiguous BEFORE any deployed view or column is touched. Reuses
    :func:`list_concepts_for_chat` (domain-wide, contract-agnostic) so it tracks
    the same concept set the query pipeline sees."""
    entities = list_concepts_for_chat(settings, domain, contract_id=None)
    grounded: list[dict[str, Any]] = []
    for e in entities:
        attrs = []
        for a in e.get("attributes") or []:
            attrs.append({
                "name": a.get("name"),
                "definition": a.get("definition") or "",
                "values": [
                    {"name": v.get("name"), "value_token": v.get("value_token") or ""}
                    for v in (a.get("values") or [])
                ],
            })
        rels = [
            {"kind": r.get("kind"), "to_name": r.get("to_name")}
            for r in (e.get("relationships") or [])
            if r.get("to_name")
        ]
        grounded.append({
            "name": e.get("name"),
            "definition": e.get("definition") or "",
            "synonyms": e.get("synonyms") or [],
            "attributes": attrs,
            "relationships": rels,
        })
    return grounded
