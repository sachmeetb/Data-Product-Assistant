"""Phase 0 DAG guard — self/cycle/eligibility validation for :CONSUMES bindings.

Two layers, both SQLite-safe (no Neo4j):
  * the pure `would_create_cycle` BFS with a stubbed neighbors function, and
  * `validate_consumes_bindings` against a fake session that dispatches the two
    Cypher shapes (eligibility + upstream-neighbors) to canned rows.

The graph-query *shapes* + transactional atomicity are exercised in the opt-in
live-Neo4j module (`test_consumes_live_neo4j.py`), which skips without a graph.
"""

import pytest

from workbench.backend._contract_versioning import (
    ConsumesBindingError,
    validate_consumes_bindings,
    would_create_cycle,
)


# ── Pure would_create_cycle ─────────────────────────────────────────────────


def _neighbors_from(graph: dict[str, set[str]]):
    """graph[c] = the set of contracts c already consumes (its upstream deps)."""
    return lambda c: graph.get(c, set())


def test_self_binding_is_a_cycle():
    assert would_create_cycle(_neighbors_from({}), "A", "A") is True


def test_direct_reciprocal_cycle():
    # A already consumes B; proposing B→A closes A↔B.
    graph = {"A": {"B"}}
    assert would_create_cycle(_neighbors_from(graph), "B", "A") is True


def test_deep_transitive_cycle():
    # A→B→C already; proposing C→A closes a 3-cycle.
    graph = {"A": {"B"}, "B": {"C"}}
    assert would_create_cycle(_neighbors_from(graph), "C", "A") is True


def test_valid_diamond_is_accepted():
    # A→B, A→C, B→D, C→D — a DAG. Re-adding C→D must NOT be flagged.
    graph = {"A": {"B", "C"}, "B": {"D"}, "C": set(), "D": set()}
    assert would_create_cycle(_neighbors_from(graph), "C", "D") is False


def test_unrelated_binding_accepted():
    graph = {"A": {"B"}, "X": {"Y"}}
    assert would_create_cycle(_neighbors_from(graph), "A", "X") is False


def test_cycle_bfs_terminates_on_shared_ancestors():
    # A wide graph with a shared ancestor must not loop forever (visited-set).
    graph = {"A": {"B", "C"}, "B": {"D"}, "C": {"D"}, "D": {"E"}, "E": set()}
    # Proposing E→A would close a cycle (A reaches E via B/C→D→E).
    assert would_create_cycle(_neighbors_from(graph), "E", "A") is True
    # Proposing F→A (F not in graph) is fine.
    assert would_create_cycle(_neighbors_from(graph), "F", "A") is False


# ── validate_consumes_bindings against a fake session ───────────────────────


class _Result(list):
    def single(self):
        return self[0] if self else None


class _FakeNs:
    """Dispatches ns.run(query, **params) to the two Cypher shapes the guard
    issues. `eligibility` maps dprod_uri → (dp_exists, product_kind, pubs);
    `neighbors` maps contract_id → list[upstream_contract_id]."""

    def __init__(self, eligibility: dict, neighbors: dict):
        self._elig = eligibility
        self._neighbors = neighbors

    def run(self, query, **params):
        if "upstream_contract_id" in query:
            cid = params["contract_id"]
            return _Result(
                [{"upstream_contract_id": u} for u in self._neighbors.get(cid, [])]
            )
        if "publishable_versions" in query:
            rows = []
            for uri in params["dprod_uris"]:
                exists, kind, pubs = self._elig.get(uri, (False, "", 0))
                rows.append({
                    "uri": uri,
                    "dp_exists": exists,
                    "owner_contract_id": uri[len("dprod:"):] if uri.startswith("dprod:") else None,
                    "product_kind": kind,
                    "publishable_versions": pubs,
                })
            return _Result(rows)
        return _Result([])


def _inp(uri):
    return {"dprod_uri": uri}


def test_validate_accepts_published_source():
    ns = _FakeNs(
        eligibility={"dprod:src-contract": (True, "source", 1)},
        neighbors={},
    )
    # Should not raise.
    validate_consumes_bindings(ns, "cons-contract", [_inp("dprod:src-contract")])


def test_validate_accepts_aggregate_and_consumer_upstreams():
    ns = _FakeNs(
        eligibility={
            "dprod:agg-contract": (True, "aggregate", 2),
            "dprod:leaf-contract": (True, "consumer", 1),
        },
        neighbors={},
    )
    validate_consumes_bindings(
        ns, "cons-contract",
        [_inp("dprod:agg-contract"), _inp("dprod:leaf-contract")],
    )


def test_validate_rejects_self():
    ns = _FakeNs(eligibility={"dprod:cons-contract": (True, "consumer", 1)}, neighbors={})
    with pytest.raises(ConsumesBindingError) as ei:
        validate_consumes_bindings(ns, "cons-contract", [_inp("dprod:cons-contract")])
    assert ei.value.reason == "self"


def test_validate_rejects_unknown_target():
    ns = _FakeNs(eligibility={}, neighbors={})
    with pytest.raises(ConsumesBindingError) as ei:
        validate_consumes_bindings(ns, "cons-contract", [_inp("dprod:ghost-contract")])
    assert ei.value.reason == "unknown_target"


def test_validate_rejects_unpublished_target():
    ns = _FakeNs(eligibility={"dprod:draft-contract": (True, "source", 0)}, neighbors={})
    with pytest.raises(ConsumesBindingError) as ei:
        validate_consumes_bindings(ns, "cons-contract", [_inp("dprod:draft-contract")])
    assert ei.value.reason == "not_published"


def test_validate_rejects_unsupported_kind():
    ns = _FakeNs(eligibility={"dprod:weird-contract": (True, "", 1)}, neighbors={})
    with pytest.raises(ConsumesBindingError) as ei:
        validate_consumes_bindings(ns, "cons-contract", [_inp("dprod:weird-contract")])
    assert ei.value.reason == "unsupported_kind"


def test_validate_rejects_cycle():
    # B currently consumes cons (neighbors[B-contract] = [cons-contract]); binding
    # cons→B would close cons↔B.
    ns = _FakeNs(
        eligibility={"dprod:b-contract": (True, "consumer", 1)},
        neighbors={"b-contract": ["cons-contract"]},
    )
    with pytest.raises(ConsumesBindingError) as ei:
        validate_consumes_bindings(ns, "cons-contract", [_inp("dprod:b-contract")])
    assert ei.value.reason == "cycle"


def test_validate_noop_on_empty_inputs():
    ns = _FakeNs(eligibility={}, neighbors={})
    validate_consumes_bindings(ns, "cons-contract", [])
    validate_consumes_bindings(ns, "cons-contract", None)
