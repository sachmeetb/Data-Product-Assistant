"""Deterministic source-table clustering tests (pure logic — no graph)."""
from __future__ import annotations

from workbench.backend.feasibility_clusters import cluster_datasets


def _ds(uri, table, cols, schema="s"):
    return {"uri": uri, "schema": schema, "table": table,
            "columns": [{"name": c} for c in cols]}


def test_shared_key_merges_selective():
    # customer_id joins customers↔orders↔addresses (a minority-of-tables key); a
    # standalone `products` table (no shared key) stays its own cluster.
    datasets = [
        _ds("u:customers", "customers", ["id", "customer_id", "name"]),
        _ds("u:orders", "orders", ["id", "order_id", "customer_id", "amount"]),
        _ds("u:addresses", "addresses", ["id", "address_id", "customer_id", "city"]),
        _ds("u:products", "products", ["id", "product_id", "sku"]),
    ]
    clusters = cluster_datasets(datasets)
    by_name = {c["name"]: c for c in clusters}
    cust = next(c for c in clusters if "customers" in c["table_refs"])
    assert set(cust["table_refs"]) == {"customers", "orders", "addresses"}  # merged on customer_id
    prod = next(c for c in clusters if "products" in c["table_refs"])
    assert prod["table_refs"] == ["products"]  # standalone
    assert cust["name"] == "Customer" and "customer_id" in cust["rationale"]


def test_ubiquitous_key_does_not_collapse():
    # A bare `id` present in EVERY table must NOT merge them (the selectivity guard);
    # with no other shared key, each table is its own cluster.
    datasets = [_ds(f"u:{t}", t, ["id", "name"]) for t in ("a", "b", "c", "d")]
    clusters = cluster_datasets(datasets)
    assert len(clusters) == 4
    assert all(len(c["table_refs"]) == 1 for c in clusters)


def test_denylist_key_never_merges():
    # tenant_id in only two tables would pass the frequency cut, but the denylist
    # blocks it — tenancy keys aren't a product seam.
    datasets = [
        _ds("u:a", "a", ["id", "tenant_id", "a_val"]),
        _ds("u:b", "b", ["id", "tenant_id", "b_val"]),
    ]
    clusters = cluster_datasets(datasets)
    assert len(clusters) == 2


def test_fk_edges_take_precedence():
    # No shared identity key, but a real FK edge joins the two.
    datasets = [
        _ds("u:invoice", "invoice", ["id", "invoice_no"]),
        _ds("u:line", "line", ["id", "line_no"]),
    ]
    fk = [{"src_uri": "u:line", "tgt_uri": "u:invoice", "src_table": "line", "tgt_table": "invoice"}]
    clusters = cluster_datasets(datasets, fk)
    assert len(clusters) == 1
    assert set(clusters[0]["table_refs"]) == {"invoice", "line"}
    assert "FK edge" in clusters[0]["rationale"]


def test_deterministic_and_stable_ids():
    datasets = [
        _ds("u:customers", "customers", ["customer_id", "name"]),
        _ds("u:orders", "orders", ["order_id", "customer_id"]),
        _ds("u:products", "products", ["product_id"]),
    ]
    a = cluster_datasets(datasets)
    b = cluster_datasets(datasets)
    assert a == b
    # Largest cluster first, ids assigned in order.
    assert a[0]["cluster_id"] == "c0" and len(a[0]["table_refs"]) >= len(a[-1]["table_refs"])


# ── boundary-advisor (agentic refinement) input + fail-closed validation ───────

from workbench.backend.feasibility_clusters import (  # noqa: E402
    boundary_advisor_input, validate_clusters,
)


def test_validate_clusters_partition_ok():
    tables = ["customer", "customer_address", "store_sales", "date_dim"]
    raw = [
        {"name": "Customers", "table_refs": ["customer", "customer_address"], "rationale": "master + addresses"},
        {"name": "Sales", "table_refs": ["store_sales"]},
        {"name": "Calendar", "table_refs": ["date_dim"]},
    ]
    out = validate_clusters(raw, tables)
    assert out is not None and len(out) == 3
    assert out[0]["source"] == "advisor" and out[0]["cluster_id"] == "c0"
    # Largest cluster first.
    assert out[0]["table_refs"] == ["customer", "customer_address"]


def test_validate_clusters_fail_closed():
    tables = ["a", "b", "c"]
    # missing a table
    assert validate_clusters([{"name": "X", "table_refs": ["a", "b"]}], tables) is None
    # duplicate table across clusters
    assert validate_clusters([{"name": "X", "table_refs": ["a", "b"]},
                              {"name": "Y", "table_refs": ["b", "c"]}], tables) is None
    # invented table
    assert validate_clusters([{"name": "X", "table_refs": ["a", "b", "c", "ghost"]}], tables) is None
    # empty / malformed
    assert validate_clusters([], tables) is None
    assert validate_clusters([{"name": "X"}], tables) is None
    # a single all-covering cluster IS a valid partition
    assert validate_clusters([{"name": "All", "table_refs": ["a", "b", "c"]}], tables) is not None


def test_boundary_advisor_input_shape():
    datasets = [
        {"uri": "u:customer", "table": "customer", "description": "Customer master.",
         "columns": [{"name": "c_customer_sk"}, {"name": "c_name"}]},
        {"uri": "u:store_sales", "table": "store_sales", "description": "POS lines.",
         "columns": [{"name": "ss_customer_sk"}, {"name": "ss_price"}]},
    ]
    fk = [{"src_table": "store_sales", "tgt_table": "customer", "columns": ["ss_customer_sk"]}]
    clusters = cluster_datasets(datasets, fk)
    payload = boundary_advisor_input(clusters, datasets, fk)
    assert {t["table_ref"] for t in payload["tables"]} == {"customer", "store_sales"}
    assert payload["tables"][0]["description"]
    assert payload["signals"] == [{"from": "store_sales", "to": "customer", "kind": "fk", "on": ["ss_customer_sk"]}]
    assert payload["seed_clusters"]


def test_validate_attribute_groups():
    from workbench.backend.feasibility_clusters import validate_attribute_groups
    names = ["customer_id", "email", "marketing_opt_in"]
    ok = validate_attribute_groups([
        {"name": "Identity", "attribute_names": ["customer_id"]},
        {"name": "Contact", "attribute_names": ["email"]},
        {"name": "Marketing", "attribute_names": ["marketing_opt_in"]},
    ], names)
    assert ok is not None and len(ok) == 3 and ok[0]["name"] == "Identity"
    # fail-closed: missing / duplicate / invented / empty
    assert validate_attribute_groups([{"name": "A", "attribute_names": ["customer_id"]}], names) is None
    assert validate_attribute_groups([{"name": "A", "attribute_names": ["customer_id", "email"]},
                                      {"name": "B", "attribute_names": ["email", "marketing_opt_in"]}], names) is None
    assert validate_attribute_groups([{"name": "A", "attribute_names": ["customer_id", "email", "marketing_opt_in", "ghost"]}], names) is None
    assert validate_attribute_groups([], names) is None
    # one all-covering group is valid
    assert validate_attribute_groups([{"name": "All", "attribute_names": names}], names) is not None
