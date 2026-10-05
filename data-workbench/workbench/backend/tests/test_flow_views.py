"""Tests for the layered value-flow ("Sankey") view.

Two halves, both DB-free:
  1. The pure ``flow_payload`` assembler — always-5-columns, empty→[], node +
     link dedup, cross-column reject, dropped dangling link, bucket_product_kind.
  2. The marketplace ``/flow`` endpoint with its three Neo4j reads monkeypatched
     to canned rows (fake NS dispatches on query text) → kind-bucketing,
     source_binding weights, consumes edges, empty use-case column.

Run from the repo ROOT: env/bin/python -m pytest workbench/backend/tests/test_flow_views.py -q
"""

from __future__ import annotations

import pytest

from workbench.backend import flow_payload as fp
from workbench.backend.flow_payload import FlowBuilder, bucket_product_kind
from workbench.backend.routers import marketplace as mkt


# ── Pure assembler ───────────────────────────────────────────────────────────


def test_bucket_product_kind():
    assert bucket_product_kind("source") == "source_aligned"
    assert bucket_product_kind("aggregate") == "aggregate"
    assert bucket_product_kind("consumer") == "consumer"
    # blank / None / unknown all fall to source_aligned (matches the
    # coalesce(dc.productKind,'') convention)
    assert bucket_product_kind("") == "source_aligned"
    assert bucket_product_kind(None) == "source_aligned"
    assert bucket_product_kind("mystery") == "source_aligned"
    # case + whitespace tolerant
    assert bucket_product_kind("  Aggregate ") == "aggregate"


def test_build_always_five_columns_in_fixed_order():
    out = FlowBuilder().build()
    keys = [c["key"] for c in out["columns"]]
    assert keys == fp.COLUMN_ORDER
    assert len(out["columns"]) == 5
    # every column present, empty ones carry nodes:[]
    for c in out["columns"]:
        assert c["nodes"] == []
    assert out["links"] == []


def test_context_overridable_source_label():
    default = FlowBuilder().build()["columns"][0]
    assert default["label"] == "Source Systems"
    relabelled = FlowBuilder(column_labels={"source_system": "Source Schemas"}).build()
    assert relabelled["columns"][0]["label"] == "Source Schemas"
    # keys never change, only labels
    assert relabelled["columns"][0]["key"] == "source_system"


def test_add_node_idempotent_same_column():
    b = FlowBuilder()
    b.add_node("aggregate", "p:1", "First", count=3)
    # re-add same id + column is a no-op; first write wins
    b.add_node("aggregate", "p:1", "Second", count=99)
    agg = next(c for c in b.build()["columns"] if c["key"] == "aggregate")
    assert len(agg["nodes"]) == 1
    assert agg["nodes"][0]["label"] == "First"
    assert agg["nodes"][0]["count"] == 3


def test_add_node_cross_column_reuse_raises():
    b = FlowBuilder()
    b.add_node("aggregate", "x:1", "X")
    with pytest.raises(ValueError):
        b.add_node("consumer", "x:1", "X again")


def test_add_node_unknown_column_raises():
    with pytest.raises(ValueError):
        FlowBuilder().add_node("not_a_column", "n:1", "N")


def test_add_link_dedups_on_source_target_kind():
    b = FlowBuilder()
    b.add_node("source_aligned", "a", "A")
    b.add_node("aggregate", "b", "B")
    b.add_link("a", "b", "consumes")
    b.add_link("a", "b", "consumes")  # dup → dropped
    b.add_link("a", "b", "source_binding")  # different kind → kept
    links = b.build()["links"]
    assert len(links) == 2
    assert {lk["kind"] for lk in links} == {"consumes", "source_binding"}


def test_build_drops_dangling_links():
    b = FlowBuilder()
    b.add_node("source_aligned", "a", "A")
    # target "ghost" was never added → the link must be dropped by build()
    b.add_link("a", "ghost", "consumes")
    b.add_link("a", "a", "consumes")  # present-both self link IS kept by builder
    out = b.build()
    assert out["links"] == [{"source": "a", "target": "a", "kind": "consumes"}]


def test_node_optional_fields_and_placeholder():
    b = FlowBuilder()
    b.add_node(
        "consumer", "c:1", "C",
        kind="consumer", status="published", count=2, meta={"domain": "sales"},
    )
    b.add_node("use_case", "ghost", "coming soon", placeholder=True)
    nodes = {n["id"]: n for col in b.build()["columns"] for n in col["nodes"]}
    assert nodes["c:1"]["status"] == "published"
    assert nodes["c:1"]["meta"] == {"domain": "sales"}
    assert nodes["ghost"]["placeholder"] is True
    # count omitted → key absent, not 0
    assert "count" not in nodes["ghost"]


# ── Marketplace /flow endpoint ───────────────────────────────────────────────


class _FlowNS:
    """Fake neo4j session dispatching on query text.

    The endpoint runs three queries at one NS. MAPS_SOURCE_COLUMN is unique to
    FLOW_SOURCE_SYSTEMS; CONSUMES to PRODUCT_LINEAGE_EDGES; everything else is
    FLOW_PRODUCT_NODES.
    """

    def __init__(self, products, edges, sources):
        self._products, self._edges, self._sources = products, edges, sources

    def run(self, query, **_params):
        if "MAPS_SOURCE_COLUMN" in query:
            return self._sources
        if "CONSUMES" in query:
            return self._edges
        return self._products


class _FlowCtx:
    def __init__(self, ns):
        self._ns = ns

    def __enter__(self):
        return self._ns

    def __exit__(self, *_a):
        return False


def _patch(monkeypatch, products, edges, sources):
    monkeypatch.setattr(mkt, "_get_settings", lambda session: object())
    monkeypatch.setattr(
        mkt, "_neo4j_from_settings", lambda settings: _FlowCtx(_FlowNS(products, edges, sources))
    )


def test_flow_buckets_kinds_and_wires_links(monkeypatch):
    products = [
        {"uri": "dprod:A", "name": "Customer Master", "product_kind": "source",
         "domain": "customer", "tags": ["gold"], "lifecycle_state": "published"},
        {"uri": "dprod:B", "name": "Customer 360", "product_kind": "aggregate",
         "domain": "customer", "tags": [], "lifecycle_state": "published"},
        {"uri": "dprod:C", "name": "Churn Report", "product_kind": "consumer",
         "domain": "customer", "tags": None, "lifecycle_state": "published"},
        # blank kind → source_aligned bucket
        {"uri": "dprod:D", "name": "Unclassified", "product_kind": "",
         "domain": "customer", "tags": [], "lifecycle_state": "superseded"},
    ]
    edges = [
        {"source_uri": "dprod:A", "target_uri": "dprod:B"},
        {"source_uri": "dprod:B", "target_uri": "dprod:C"},
        {"source_uri": "dprod:A", "target_uri": "dprod:B"},  # dup → deduped
        {"source_uri": "dprod:X", "target_uri": "dprod:C"},  # dangling → dropped
    ]
    sources = [
        {"product_uri": "dprod:A", "catalog_uri": "catalog:sales:public",
         "schema": "public", "domain": "customer", "source_columns": 7},
        # a second row for the same catalog+product is folded (weight from last)
        {"product_uri": "dprod:A", "catalog_uri": "catalog:sales:public",
         "schema": "public", "domain": "customer", "source_columns": 7},
        # source feeding a product not in the set → dropped
        {"product_uri": "dprod:GONE", "catalog_uri": "catalog:hr:public",
         "schema": "public", "domain": "hr", "source_columns": 3},
    ]
    _patch(monkeypatch, products, edges, sources)

    out = mkt.get_marketplace_flow(domain=None, session=None)
    cols = {c["key"]: c for c in out["columns"]}
    assert [c["key"] for c in out["columns"]] == fp.COLUMN_ORDER

    # kind-bucketing: source + blank → source_aligned; aggregate; consumer
    assert {n["id"] for n in cols["source_aligned"]["nodes"]} == {"dprod:A", "dprod:D"}
    assert {n["id"] for n in cols["aggregate"]["nodes"]} == {"dprod:B"}
    assert {n["id"] for n in cols["consumer"]["nodes"]} == {"dprod:C"}

    # source-schema column: one project-scoped catalog node, count = # products fed
    src = cols["source_system"]["nodes"]
    assert len(src) == 1
    assert src[0]["id"] == "catalog:sales:public"
    assert src[0]["label"] == "public"
    assert src[0]["count"] == 1
    assert src[0]["meta"]["synthesized"] is True
    assert src[0]["meta"]["project_code"] == "sales"

    # use-case column is empty (pure placeholder)
    assert cols["use_case"]["nodes"] == []

    # links: consumes (deduped, dangling dropped) + one source_binding with weight
    consumes = [lk for lk in out["links"] if lk["kind"] == "consumes"]
    assert sorted((lk["source"], lk["target"]) for lk in consumes) == [
        ("dprod:A", "dprod:B"), ("dprod:B", "dprod:C")
    ]
    bindings = [lk for lk in out["links"] if lk["kind"] == "source_binding"]
    assert bindings == [{"source": "catalog:sales:public", "target": "dprod:A",
                         "kind": "source_binding", "weight": 7}]

    # downstream count on product nodes: A feeds 1 (B), B feeds 1 (C), C feeds 0
    by_id = {n["id"]: n for c in out["columns"] for n in c["nodes"]}
    assert by_id["dprod:A"]["count"] == 1
    assert by_id["dprod:B"]["count"] == 1
    assert by_id["dprod:C"]["count"] == 0


def test_flow_domain_scope_drops_out_of_domain_and_their_edges(monkeypatch):
    products = [
        {"uri": "dprod:A", "name": "A", "product_kind": "source",
         "domain": "customer", "tags": [], "lifecycle_state": "published"},
        {"uri": "dprod:Z", "name": "Z", "product_kind": "consumer",
         "domain": "finance", "tags": [], "lifecycle_state": "published"},
    ]
    edges = [{"source_uri": "dprod:A", "target_uri": "dprod:Z"}]
    sources = [
        {"product_uri": "dprod:A", "catalog_uri": "catalog:sales:public",
         "schema": "public", "domain": "customer", "source_columns": 4},
    ]
    _patch(monkeypatch, products, edges, sources)

    out = mkt.get_marketplace_flow(domain="customer", session=None)
    by_id = {n["id"]: n for c in out["columns"] for n in c["nodes"]}
    assert "dprod:A" in by_id
    assert "dprod:Z" not in by_id  # out-of-domain product filtered
    # the consumes edge into the filtered product is dropped by the builder
    assert all(lk["target"] != "dprod:Z" for lk in out["links"])
    # the source binding into A survives
    assert any(lk["kind"] == "source_binding" and lk["target"] == "dprod:A"
               for lk in out["links"])


def test_flow_empty(monkeypatch):
    _patch(monkeypatch, [], [], [])
    out = mkt.get_marketplace_flow(domain=None, session=None)
    assert [c["key"] for c in out["columns"]] == fp.COLUMN_ORDER
    assert all(c["nodes"] == [] for c in out["columns"])
    assert out["links"] == []
