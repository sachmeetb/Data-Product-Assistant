"""Join-connectivity preflight for data products.

After ``data_mapping``, the base source tables a product's mappings resolve to
must form ONE FK-connected component so the view-DDL can assemble a single
``FROM`` clause. A consumer-aligned product that mixes tables from MULTIPLE
source products has no cross-product ``:REFERENCES`` edge (by design — the only
cross-project edge is ``:CONSUMES``), so the FK graph splits into disconnected
components and serving fails late with ``ViewGenerationError: … no FK path …``.

This module detects that gap up front and proposes the explicit join
(``:DatasetTransform.joins[]``) that bridges the components on a shared key —
turning a serving-time error into an actionable, one-click-appliable
recommendation. It is consumed by:
  - the ``/serving/join-preflight`` endpoint (engineer-facing safety net), and
  - the ``data-mapping-neo4j`` skill, which runs the same check while authoring
    so it RECOMMENDS the bridge instead of emitting a dead-end mapping.

Pure read-only graph analysis; the recommended ``joins`` payload is shaped
exactly like the ``PUT /dataset-transform/joins`` body so the caller can apply
it verbatim.
"""

from __future__ import annotations

import re
from typing import Any

# Base tables = the FROM-side anchors the mappings read from (via
# :MAPS_SOURCE_COLUMN). Lookup reference tables live in transformParams (a
# separate LEFT JOIN keyed on a base column) and are intentionally NOT counted
# here — they don't need a base-table FK path. We also pull each base table's
# columns + owning source product so we can (a) union-find on FK edges and
# (b) propose a shared-key bridge predicate.
_BASE_TABLES_QUERY = """
MATCH (dp:DProdDataProduct {uri:$product_uri})-[:DPROD_OUTPUT_PORT]->()
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset {uri:$output_dataset_uri})
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
MATCH (cm:ColumnMapping {isCurrent:true, status:'approved'})-[:MAPS_TO_PRODUCT_COLUMN]->(pc)
MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(sc:DProdColumn)<-[:HAS_PRODUCT_COLUMN]-(sods:DProdOutputDataset)
      <-[:DPROD_OUTPUT_DATASET]-(:DProdOutputPort)<-[:DPROD_OUTPUT_PORT]-(sdp:DProdDataProduct)
WITH sods, sdp, count(DISTINCT pc) AS mapped_cols
MATCH (sods)-[:HAS_PRODUCT_COLUMN]->(c:DProdColumn)
RETURN sods.uri          AS uri,
       coalesce(sods.physicalName, sods.name) AS phys,
       sdp.uri           AS product_uri,
       sdp.name          AS product_name,
       mapped_cols       AS mapped_cols,
       collect(DISTINCT c.name) AS cols
"""

# :REFERENCES edges (propagated from catalog FKs during _generate_dprod) among
# the in-scope base tables. Used to union the FK-connected components.
_FK_AMONG_QUERY = """
MATCH (a:DProdOutputDataset)-[r:REFERENCES]->(b:DProdOutputDataset)
WHERE a.uri IN $uris AND b.uri IN $uris
RETURN a.uri AS from_uri, b.uri AS to_uri
"""

_OUTPUT_DATASETS_QUERY = """
MATCH (dp:DProdDataProduct {uri:$product_uri})-[:DPROD_OUTPUT_PORT]->()
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
RETURN ods.uri AS uri, coalesce(ods.physicalName, ods.name) AS phys
"""

# An engineer-declared explicit join graph already bridges the components — the
# view-DDL uses it verbatim, so connectivity is no longer the FK graph's job.
# Also carries the SCD policy so the recommendation can show an as-of predicate
# for history targets (matching what the generator auto-emits).
_EXISTING_JOINS_QUERY = """
MATCH (ods:DProdOutputDataset {uri:$output_dataset_uri})-[:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform)
RETURN dt.joinsJson AS joins_json, dt.scdPolicyJson AS scd_policy_json,
       dt.dedupeJson AS dedupe_json, dt.groupingKeysJson AS grouping_keys_json
"""

# Gap-B junction candidates: every OTHER output dataset of the source products
# this consumer :CONSUMES (the governance/access boundary — a dataset in a
# product the consumer never subscribed to is deliberately out of scope).
# A candidate that shares DIFFERENT identity keys with two disconnected
# components (transaction: account_id ↔ customer_id) can bridge them as a
# pure join-only table even though no mapping reads from it.
_CONSUMED_DATASETS_QUERY = """
MATCH (dp:DProdDataProduct {uri:$product_uri})<-[:MATERIALISES_AS]-(dc:DataContract)
MATCH (dc)-[:CONSUMES]->(src:DProdDataProduct)
MATCH (src)-[:DPROD_OUTPUT_PORT]->()-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
WHERE NOT ods.uri IN $exclude_uris
MATCH (ods)-[:HAS_PRODUCT_COLUMN]->(c:DProdColumn)
OPTIONAL MATCH (src)-[:SERVED_BY]->(sd:ServingDefinition)
WITH ods, src, collect(DISTINCT c.name) AS cols,
     collect(DISTINCT sd.deploymentStatus) AS dep
RETURN ods.uri AS uri,
       coalesce(ods.physicalName, ods.name) AS phys,
       coalesce(ods.relationshipKind, '') AS relationship_kind,
       src.uri AS product_uri, src.name AS product_name,
       CASE WHEN 'deployed' IN dep THEN 'deployed'
            ELSE coalesce(dep[0], 'pending') END AS src_deployment_status,
       cols
"""

# Junction-candidate ranking mirrors the generator's `_rank_bridge_paths`
# weights (generate_view_ddl.py) — keep in sync.
_RELATIONSHIP_KIND_WEIGHTS = {
    "general_membership": 3,
    "lookup_dimension":   1,
    "fact":               1,
    "audit_log":          0,
    "configuration":      0,
    "specialization":     -1,
    "unknown":            0,
}

# Effective-dating markers — used to detect a history bridge so the
# recommendation can show an as-of (interval-overlap) predicate for SCD-2
# targets instead of a plain equi-join. Mirrors the generator's detection.
_EFF_FROM_MARKERS = ("effective_from", "valid_from", "from_date", "start_date", "effective_date")
_EFF_TO_MARKERS = ("effective_to", "valid_to", "to_date", "end_date", "expiration", "expiry")


def _pick_marker(cols, markers):
    for m in markers:
        for c in cols or []:
            if c and m in c.lower():
                return c
    return None

# Column-name suffixes that mark a plausible join key. A shared key between two
# otherwise-unconnected components is what we bridge on.
_KEY_SUFFIXES = ("_id", "_code", "_key", "_no", "_number")

_KEY_STEM_RE = re.compile(r"(_id|_code|_key|_no|_number)$")


def _looks_like_key(col: str) -> bool:
    c = (col or "").lower()
    return c == "id" or any(c.endswith(s) for s in _KEY_SUFFIXES)


def _shared_identity_keys(cols_a, cols_b, preferred=None) -> list[str]:
    """Key-looking columns present in BOTH column sets, preferred (grain
    natural key) first, then alphabetical. Mirrors the generator's
    `_shared_key_columns` (generate_view_ddl.py) — keep in sync."""
    a = {c.lower(): c for c in (cols_a or []) if c}
    b = {c.lower(): c for c in (cols_b or []) if c}
    shared = [a[k] for k in a if k in b and _looks_like_key(a[k])]
    shared.sort(key=lambda c: c.lower())
    if preferred:
        pl = preferred.lower()
        shared.sort(key=lambda c: 0 if c.lower() == pl else 1)
    return shared


def _name_token(phys: str) -> str:
    t = (phys or "").lower()
    return t[3:] if t.startswith("vw_") else t


def _score_chain_key(key, stuck_phys, partner_phys, preferred=None) -> int:
    """Rank a candidate (partner, key) pair for transitive chain bridging.
    +2 when the key's stem names either endpoint table (`customer_id` into
    `customer`), +1 when it is the declared grain natural key. Mirrors the
    generator's `_score_chain_key` — keep in sync."""
    score = 0
    stem = _KEY_STEM_RE.sub("", (key or "").lower())
    if stem:
        for phys in (stuck_phys, partner_phys):
            token = _name_token(phys)
            if token and (stem in token or token in stem):
                score += 2
                break
    if preferred and key.lower() == preferred.lower():
        score += 1
    return score


def _alias_for(phys: str, taken: set[str]) -> str:
    """Short, readable, collision-free alias from a physical name.

    `job_assignment_history` -> `jah`, `employee` -> `emp`/`e`. Falls back to a
    numbered suffix on collision so predicates stay unambiguous.
    """
    words = [w for w in re.split(r"[^a-zA-Z0-9]+", phys or "") if w]
    if not words:
        base = "t"
    elif len(words) == 1:
        base = words[0][:3].lower()
    else:
        base = "".join(w[0] for w in words).lower()
    alias = base
    i = 1
    while alias in taken:
        i += 1
        alias = f"{base}{i}"
    taken.add(alias)
    return alias


def _rank_junction_candidates(candidates, joined, tables, stuck_uri,
                              preferred_key=None):
    """Rank consumed-but-unmapped datasets that could bridge `stuck_uri` onto
    the already-joined set as a pure junction.

    A candidate is valid only when it shares DIFFERENT identity keys with the
    two sides (`transaction`: `account_id` with the joined side, `customer_id`
    with the stuck side). Same-key sharing is the shared-dimension case the
    transitive chain already handles.

    Score mirrors the generator's `_rank_bridge_paths`: endpoint-name token
    containment in the candidate's name (0..2) × 10 + relationshipKind weight.
    Returns list of dicts sorted by score desc; caller applies the
    strict-winner-or-don't-guess policy.
    """
    stuck = tables[stuck_uri]
    stuck_token = _name_token(stuck["phys"])
    ranked = []
    for cand in candidates:
        cand_cols = cand.get("cols") or []
        keys_to_stuck = _shared_identity_keys(cand_cols, stuck["cols"], preferred=preferred_key)
        if not keys_to_stuck:
            continue
        # Best joined-side partner: walk in join order (anchor first); prefer
        # a (partner, key) whose key stem names the partner table.
        best_partner = None  # (rank_tuple, partner_uri, kJ)
        for order_idx, j_uri in enumerate(joined):
            j = tables[j_uri]
            for k in _shared_identity_keys(cand_cols, j["cols"], preferred=preferred_key):
                sc = _score_chain_key(k, cand["phys"], j["phys"], preferred_key)
                rank = (sc, -order_idx)
                if best_partner is None or rank > best_partner[0]:
                    best_partner = (rank, j_uri, k)
        if best_partner is None:
            continue
        _, partner_uri, k_joined = best_partner
        # DIFFERENT keys on the two sides — pick the stuck-side key that
        # differs from the joined-side one, best stem match first.
        k_stuck = None
        for k in sorted(keys_to_stuck,
                        key=lambda c: -_score_chain_key(c, cand["phys"], stuck["phys"], preferred_key)):
            if k.lower() != k_joined.lower():
                k_stuck = k
                break
        if k_stuck is None:
            continue
        partner_token = _name_token(tables[partner_uri]["phys"])
        cand_token = _name_token(cand["phys"])
        primary = int(bool(stuck_token) and stuck_token in cand_token) + \
                  int(bool(partner_token) and partner_token in cand_token)
        kind = (cand.get("relationship_kind") or "").lower()
        secondary = _RELATIONSHIP_KIND_WEIGHTS.get(kind, 0)
        score = primary * 10 + secondary
        rationale = (
            f"links `{tables[partner_uri]['phys']}` (`{k_joined}`) to "
            f"`{stuck['phys']}` (`{k_stuck}`)"
            + (f"; relationship_kind = {kind}" if kind else "")
        )
        ranked.append({
            "cand": cand, "partner_uri": partner_uri,
            "key_joined": k_joined, "key_stuck": k_stuck,
            "score": score, "rationale": rationale,
        })
    ranked.sort(key=lambda r: (-r["score"], r["cand"]["phys"]))
    return ranked


class _UnionFind:
    def __init__(self, items):
        self.parent = {x: x for x in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        self.parent[self.find(a)] = self.find(b)


def analyze_dataset_connectivity(session, product_uri: str, output_dataset_uri: str,
                                 view_schema: str = "public") -> dict[str, Any]:
    """Analyze one output dataset's base-table join connectivity.

    Returns a dict with ``connected`` plus, when disconnected, a
    ``recommended_joins`` payload (shaped like the dataset-transform/joins body)
    that bridges the components on a shared key.
    """
    tables: dict[str, dict] = {}
    for r in session.run(_BASE_TABLES_QUERY, product_uri=product_uri,
                         output_dataset_uri=output_dataset_uri):
        tables[r["uri"]] = {
            "uri":          r["uri"],
            "phys":         r["phys"],
            "product_uri":  r["product_uri"],
            "product_name": r["product_name"],
            "mapped_cols":  r["mapped_cols"],
            "cols":         set(r["cols"] or []),
        }

    base = {
        "output_dataset_uri": output_dataset_uri,
        "base_table_count":   len(tables),
    }

    # An explicit joins[] override already declares the FROM/JOIN graph — the
    # view-DDL builds from it verbatim, so the FK graph's connectivity is moot.
    import json as _json
    jrow = session.run(_EXISTING_JOINS_QUERY, output_dataset_uri=output_dataset_uri).single()
    if jrow and jrow.get("joins_json"):
        try:
            declared = _json.loads(jrow["joins_json"])
        except (ValueError, TypeError):
            declared = []
        if declared:
            return {**base, "connected": True, "bridged_via": "explicit_joins",
                    "components": [[t["phys"] for t in tables.values()]],
                    "recommended_joins": [], "reason": ""}
    # Target SCD policy — drives current-vs-as-of in the bridge recommendation.
    scd_policy = {}
    if jrow and jrow.get("scd_policy_json"):
        try:
            scd_policy = _json.loads(jrow["scd_policy_json"]) or {}
        except (ValueError, TypeError):
            scd_policy = {}
    is_scd2 = scd_policy.get("type") == "scd2"

    # 0 or 1 base table → nothing to bridge (source-aligned datasets, single-
    # table consumer datasets). Trivially connected.
    if len(tables) < 2:
        return {**base, "connected": True, "components": [[t["phys"] for t in tables.values()]],
                "recommended_joins": [], "reason": ""}

    uris = list(tables)
    uf = _UnionFind(uris)
    for r in session.run(_FK_AMONG_QUERY, uris=uris):
        uf.union(r["from_uri"], r["to_uri"])

    comps: dict[str, list[str]] = {}
    for u in uris:
        comps.setdefault(uf.find(u), []).append(u)
    components = list(comps.values())

    if len(components) == 1:
        return {**base, "connected": True,
                "components": [[tables[u]["phys"] for u in components[0]]],
                "recommended_joins": [], "reason": ""}

    # Disconnected. Pick the anchor component (most mapped columns = most likely
    # the product's grain), and within it the table with the most mapped columns
    # as the FROM anchor. Then CHAIN every other table onto the growing join
    # graph: the bridge partner is ANY already-joined table — anchor first
    # (identical to the pre-chaining behaviour when the anchor shares a key),
    # then scored via _score_chain_key — so a two-hop path like
    # account —account_id→ transaction —customer_id→ customer resolves without
    # a human. Mirrors the generator's transitive fallback; keep in sync.
    components.sort(key=lambda c: sum(tables[u]["mapped_cols"] for u in c), reverse=True)
    anchor_comp = components[0]
    anchor_tbl = max(anchor_comp, key=lambda u: tables[u]["mapped_cols"])
    anchor_phys = tables[anchor_tbl]["phys"]

    # Grain natural-key hint — mirrors the generator's grain_natural_key
    # derivation (dedupe.keys[0] / grouping_keys[0]) so both sides prefer the
    # same chain key.
    preferred_key = None
    if jrow:
        try:
            dd = _json.loads(jrow.get("dedupe_json") or "null") or {}
            gk = _json.loads(jrow.get("grouping_keys_json") or "null") or []
        except (ValueError, TypeError):
            dd, gk = {}, []
        if isinstance(dd, dict) and dd.get("keys"):
            preferred_key = dd["keys"][0]
        elif gk:
            preferred_key = gk[0]

    taken: set[str] = set()
    anchor_alias = _alias_for(anchor_phys, taken)
    alias_of = {anchor_tbl: anchor_alias}
    recommended = [{
        "alias": anchor_alias, "dataset_uri": tables[anchor_tbl]["uri"],
        "kind": "cross", "predicate": "",   # joins[0] = FROM anchor (kind ignored by the builder)
    }]
    anchor_pivot = _pick_marker(tables[anchor_tbl]["cols"], _EFF_FROM_MARKERS) if is_scd2 else None

    def _chain_entry(u: str, partner_uri: str, key: str, extra: dict | None = None) -> None:
        """Append `u`'s recommended join, bridged to an already-joined partner."""
        alias = _alias_for(tables[u]["phys"], taken)
        alias_of[u] = alias
        predicate = f"{alias}.{key} = {alias_of[partner_uri]}.{key}"
        eff_from = _pick_marker(tables[u]["cols"], _EFF_FROM_MARKERS)
        eff_to = _pick_marker(tables[u]["cols"], _EFF_TO_MARKERS)
        if is_scd2 and anchor_pivot and eff_from and eff_to:
            # AS-OF interval vs the ANCHOR's span start (the anchor defines the
            # grain) — matching what the generator auto-emits.
            predicate += (
                f" AND {alias}.{eff_from} <= {anchor_alias}.{anchor_pivot}"
                f" AND ({alias}.{eff_to} IS NULL"
                f" OR {alias}.{eff_to} > {anchor_alias}.{anchor_pivot})"
            )
        entry = {
            "alias": alias, "dataset_uri": tables[u]["uri"], "kind": "left",
            "predicate": predicate,
            "via": {"table": tables[partner_uri]["phys"], "key": key},
        }
        if extra:
            entry.update(extra)
        recommended.append(entry)
        joined.append(u)

    # BFS chain planner. `joined` grows in dependency order — every predicate
    # references a previously-emitted alias, so the list one-click-applies.
    joined: list[str] = [anchor_tbl]
    pending = sorted((u for u in uris if u != anchor_tbl),
                     key=lambda u: (-tables[u]["mapped_cols"], tables[u]["phys"]))
    junction_bridges: list[dict] = []
    junction_candidates: list[dict] = []
    warnings: list[str] = []
    consumed_candidates = None  # lazy-fetched on first junction attempt

    progress = True
    while pending and progress:
        progress = False
        for u in list(pending):
            partner, key = None, None
            anchor_shared = _shared_identity_keys(
                tables[u]["cols"], tables[anchor_tbl]["cols"], preferred=preferred_key)
            if anchor_shared:
                partner, key = anchor_tbl, anchor_shared[0]
            else:
                best = None
                for order_idx, j_uri in enumerate(joined[1:], start=1):
                    for k in _shared_identity_keys(
                            tables[u]["cols"], tables[j_uri]["cols"], preferred=preferred_key):
                        sc = _score_chain_key(k, tables[u]["phys"], tables[j_uri]["phys"], preferred_key)
                        rank = (sc, -order_idx)
                        if best is None or rank > best[0]:
                            best = (rank, (j_uri, k))
                if best:
                    partner, key = best[1]
            if partner is None:
                continue
            _chain_entry(u, partner, key)
            pending.remove(u)
            progress = True

        if pending and not progress:
            # Gap B: no shared key chains the leftovers — look for a junction
            # dataset among the CONSUMES'd products' OTHER output datasets
            # (transaction carrying account_id + customer_id). Strict winner
            # synthesizes a pure bridge; a tie is surfaced, never guessed.
            if consumed_candidates is None:
                consumed_candidates = [dict(r) for r in session.run(
                    _CONSUMED_DATASETS_QUERY, product_uri=product_uri,
                    exclude_uris=list(tables),
                )]
            for u in list(pending):
                ranked = _rank_junction_candidates(
                    consumed_candidates, joined, tables, u, preferred_key)
                if not ranked:
                    continue
                top = ranked[0]
                second_score = ranked[1]["score"] if len(ranked) > 1 else None
                if second_score is not None and second_score >= top["score"]:
                    # Tie at the top — ambiguous. Don't guess; surface every
                    # top-tier candidate for the engineer.
                    junction_candidates.extend({
                        "dataset_uri": r["cand"]["uri"],
                        "table": r["cand"]["phys"],
                        "product_name": r["cand"]["product_name"],
                        "key_joined": r["key_joined"],
                        "key_stuck": r["key_stuck"],
                        "score": r["score"],
                        "rationale": r["rationale"],
                    } for r in ranked if r["score"] == top["score"])
                    continue
                cand = top["cand"]
                j_uri = cand["uri"]
                if j_uri not in tables:
                    # Register the junction as a joinable (but unmapped) table
                    # so later pending entries can chain onto it too.
                    tables[j_uri] = {
                        "uri": j_uri, "phys": cand["phys"],
                        "product_uri": cand["product_uri"],
                        "product_name": cand["product_name"],
                        "mapped_cols": 0, "cols": set(cand.get("cols") or []),
                    }
                    alias = _alias_for(cand["phys"], taken)
                    alias_of[j_uri] = alias
                    kj = top["key_joined"]
                    recommended.append({
                        "alias": alias, "dataset_uri": j_uri, "kind": "left",
                        "predicate": f"{alias}.{kj} = {alias_of[top['partner_uri']]}.{kj}",
                        "bridge_only": True,
                        "via": {"role": "junction",
                                "keys": [top["key_joined"], top["key_stuck"]],
                                "rationale": top["rationale"]},
                    })
                    joined.append(j_uri)
                    junction_bridges.append({
                        "dataset_uri": j_uri, "table": cand["phys"],
                        "product_name": cand["product_name"],
                        "keys": [top["key_joined"], top["key_stuck"]],
                        "src_deployment_status": cand.get("src_deployment_status") or "pending",
                    })
                    if (cand.get("src_deployment_status") or "pending") != "deployed":
                        warnings.append(
                            f"junction bridge '{cand['phys']}' comes from source product "
                            f"'{cand['product_name']}' whose serving view is not deployed — "
                            f"deploy it before serving this product."
                        )
                _chain_entry(u, j_uri, top["key_stuck"])
                pending.remove(u)
                progress = True

    # Whatever is still pending has no chainable key and no usable junction —
    # emit the empty-predicate placeholder for the engineer to fill.
    unbridged: list[str] = []
    for u in pending:
        alias = _alias_for(tables[u]["phys"], taken)
        alias_of[u] = alias
        unbridged.append(tables[u]["phys"])
        recommended.append({
            "alias": alias, "dataset_uri": tables[u]["uri"], "kind": "left",
            "predicate": "",   # no shared key found — engineer must fill in
        })

    cross_product = len({tables[u]["product_uri"] for u in uris}) > 1
    extras: dict[str, Any] = {}
    if junction_bridges:
        extras["junction_bridges"] = junction_bridges
    if junction_candidates:
        extras["junction_candidates"] = junction_candidates
    if warnings:
        extras["warnings"] = warnings

    # Auto-bridgeable: the generator synthesizes these natural-key bridges
    # itself (chained, as-of for SCD-2, DISTINCT-projected junctions), so a
    # disconnected-but-bridgeable product is NOT a blocking gap — report it
    # connected via auto bridge, with the joins as informational. Only a table
    # with NO shared identity key and no unambiguous junction is a true gap
    # that needs a human (the advise-not-guess boundary).
    if not unbridged:
        return {
            **base,
            "connected": True,
            "bridged_via": "auto_natural_key",
            "cross_product": cross_product,
            "components": [[tables[u]["phys"] for u in c] for c in components],
            "anchor_table": anchor_phys,
            "recommended_joins": recommended,   # informational (generator auto-applies)
            "unbridged_tables": [],
            "reason": (
                f"`{anchor_phys}` and its lookups span "
                + ("different source products" if cross_product else "disconnected FK components")
                + "; the serving layer auto-bridges them on shared identity keys"
                + (" (as-of, for the SCD-2 grain)" if is_scd2 else "")
                + (f", routing through junction dataset(s) "
                   f"{', '.join('`' + b['table'] + '`' for b in junction_bridges)}"
                   if junction_bridges else "")
                + "."
            ),
            **extras,
        }

    reason = (
        f"`{tables[anchor_tbl]['phys']}` and "
        f"{', '.join(sorted(unbridged))} "
        f"are not connected by a foreign key, and "
        f"{', '.join(unbridged)} share no identity key to bridge on"
        + (" (they come from different source products, which never share an FK edge)"
           if cross_product else "")
        + (
            f". Multiple junction candidates tie and none can be auto-picked: "
            f"{', '.join(sorted({c['table'] for c in junction_candidates}))}"
            if junction_candidates else
            ". No junction dataset in the CONSUMES'd products links them either"
            " — a linking table may exist in a product this consumer doesn't"
            " CONSUME (add the CONSUMES edge first)"
        )
        + ". A human must supply the join predicate (set_dataset_joins / the wizard)."
    )

    return {
        **base,
        "connected": False,
        "cross_product": cross_product,
        "components": [[tables[u]["phys"] for u in c] for c in components],
        "anchor_table": anchor_phys,
        "recommended_joins": recommended,
        "unbridged_tables": unbridged,   # non-empty → engineer must supply the predicate
        "reason": reason,
        **extras,
    }


def analyze_product_connectivity(session, product_uri: str,
                                 view_schema: str = "public") -> list[dict[str, Any]]:
    """Run the connectivity check across every output dataset of a product."""
    ds_uris = [r["uri"] for r in session.run(_OUTPUT_DATASETS_QUERY, product_uri=product_uri)]
    findings = []
    for ds_uri in ds_uris:
        findings.append(analyze_dataset_connectivity(session, product_uri, ds_uri, view_schema))
    return findings
