"""Semantic Q&A queryable-surface tests (pure logic, no Neo4j/Postgres).

Covers the follow-up fix: Semantic Q&A must query dbt-materialized products
(not only deployed virtual views), and concept-guided retrieval must not
over-narrow to tangential neighbour views when the matched concepts bind to no
deployed view. Targets the extracted pure helpers in ``marketplace_chat``:
``_views_for_product`` and the matched-only narrowing guard inside
``apply_concept_guided_retrieval`` (exercised here via its building blocks).
"""

from __future__ import annotations

import json

from workbench.backend import marketplace_chat as mc


def _serving(**kw):
    base = {
        "serving_mode": None, "deployment_status": "pending", "build_status": "",
        "view_schema": "public", "view_names_json": "[]",
        "target_schema": "", "models_json": "[]",
    }
    base.update(kw)
    return base


# ── _views_for_product: virtual vs materialized vs unusable ──────────────────


def test_virtual_view_deployed():
    prod = {
        "product_uri": "u", "product_name": "Employee Master", "product_kind": "source",
        "contract_id": "c", "datasets": [{"physical_name": "employee", "relationship_kind": "fact"}],
        "servings": [_serving(serving_mode="virtual_view", deployment_status="deployed",
                              view_names_json=json.dumps(["vw_employee"]))],
    }
    views, pairs = mc._views_for_product(prod, "CONN", None)
    assert pairs == [("public", "vw_employee")]
    assert views[0]["serving_mode"] == "virtual_view"
    assert views[0]["is_snapshot"] is False
    assert views[0]["physical_name"] == "employee"  # vw_ prefix stripped
    assert views[0]["relationship_kind"] == "fact"


def test_materialized_built_snapshot_same_instance():
    prod = {
        "product_uri": "u2", "product_name": "Compensation History", "product_kind": "consumer",
        "contract_id": "c2", "datasets": [{"physical_name": "compensation_history"}],
        "servings": [_serving(serving_mode="dbt_materialized", build_status="built",
                              target_schema="dp_dpe_06242026_02",
                              models_json=json.dumps([
                                  {"model": "compensation_history", "kind": "snapshot", "status": "success"}]))],
    }
    # mat_conn None → source fallback → same instance as the chat connection.
    views, pairs = mc._views_for_product(prod, "CONN", None)
    assert pairs == [("dp_dpe_06242026_02", "compensation_history")]
    assert views[0]["serving_mode"] == "dbt_materialized"
    assert views[0]["is_snapshot"] is True
    assert views[0]["view_schema"] == "dp_dpe_06242026_02"


def test_materialized_table_kind_not_snapshot():
    prod = {
        "product_uri": "u", "product_name": "P", "product_kind": "consumer", "contract_id": "c",
        "datasets": [], "servings": [_serving(
            serving_mode="dbt_materialized", build_status="built", target_schema="dp_x",
            models_json=json.dumps([{"model": "roster", "kind": "table", "status": "success"}]))],
    }
    views, _ = mc._views_for_product(prod, "CONN", "CONN")
    assert views[0]["is_snapshot"] is False


def test_materialized_cross_instance_skipped():
    prod = {
        "product_uri": "u", "datasets": [], "servings": [_serving(
            serving_mode="dbt_materialized", build_status="built", target_schema="dp_y",
            models_json=json.dumps([{"model": "t", "kind": "table", "status": "success"}]))],
    }
    chat = "postgresql://u:p@host-a:5432/db1"
    other_db = "postgresql://u:p@host-b:5432/db2"
    # Different instance/db → unreachable on the chat connection → skipped.
    assert mc._views_for_product(prod, chat, other_db) == ([], [])


def test_materialized_same_instance_differing_search_path_included():
    prod = {
        "product_uri": "u", "product_name": "P", "product_kind": "consumer", "contract_id": "c",
        "datasets": [], "servings": [_serving(
            serving_mode="dbt_materialized", build_status="built", target_schema="dp_x",
            models_json=json.dumps([{"model": "t", "kind": "table", "status": "success"}]))],
    }
    chat = "postgresql://u:p@host-a:5432/db1?options=-csearch_path%3Dcore"
    mat = "postgresql://u:p@host-a:5432/db1?options=-csearch_path%3Dconsumer"
    # Same host/port/db, only search_path differs → reachable (tables are
    # fully-qualified) → included.
    _, pairs = mc._views_for_product(prod, chat, mat)
    assert pairs == [("dp_x", "t")]


def test_materialized_not_built_skipped():
    prod = {"product_uri": "u", "datasets": [], "servings": [_serving(
        serving_mode="dbt_materialized", build_status="building", target_schema="dp_z")]}
    assert mc._views_for_product(prod, "CONN", None) == ([], [])


def test_unbuilt_model_excluded():
    prod = {"product_uri": "u", "datasets": [], "servings": [_serving(
        serving_mode="dbt_materialized", build_status="built", target_schema="dp_x",
        models_json=json.dumps([
            {"model": "ok", "kind": "table", "status": "success"},
            {"model": "failed_one", "kind": "table", "status": "error"}]))]}
    _, pairs = mc._views_for_product(prod, "CONN", None)
    assert pairs == [("dp_x", "ok")]  # the errored model is dropped


def test_virtual_wins_when_both_servings_present():
    prod = {
        "product_uri": "u", "product_name": "P", "product_kind": "consumer", "contract_id": "c",
        "datasets": [], "servings": [
            _serving(serving_mode="dbt_materialized", build_status="built", target_schema="dp_x",
                     models_json=json.dumps([{"model": "t", "kind": "table", "status": "success"}])),
            _serving(serving_mode="virtual_view", deployment_status="deployed",
                     view_names_json=json.dumps(["vw_t"])),
        ],
    }
    views, pairs = mc._views_for_product(prod, "CONN", None)
    assert pairs == [("public", "vw_t")]  # virtual view preferred over materialized
    assert all(v["serving_mode"] == "virtual_view" for v in views)


def test_no_serving_yields_nothing():
    assert mc._views_for_product({"product_uri": "u", "datasets": [], "servings": []}, "CONN", None) == ([], [])


# ── _enrich_preview_columns: system-column detection (framework-agnostic) ────

from workbench.backend.routers import marketplace as mkt


def test_enrich_flags_system_columns():
    # Physical preview columns: two real contract columns + two dlt system cols.
    result_columns = [
        {"name": "employee_id", "dataType": "int"},
        {"name": "review_score", "dataType": "int"},
        {"name": "_dlt_load_id", "dataType": "text"},
        {"name": "_dlt_id", "dataType": "text"},
    ]
    # Contract columns: one carries a description, one has an EMPTY description
    # (must still count as in-schema — the diff is name-based, not description).
    meta_rows = [
        {"name": "employee_id", "description": "The employee identifier"},
        {"name": "review_score", "description": ""},
    ]
    out = mkt._enrich_preview_columns(result_columns, meta_rows)
    flags = {c["name"]: c["in_schema"] for c in out}
    assert flags == {
        "employee_id": True,
        "review_score": True,   # empty description, still a real column
        "_dlt_load_id": False,  # not in the published schema → system column
        "_dlt_id": False,
    }
    # Description only attached when non-empty.
    descs = {c["name"]: c["description"] for c in out}
    assert descs["employee_id"] == "The employee identifier"
    assert descs["review_score"] is None


def test_enrich_fails_open_without_contract_columns():
    # No resolvable contract column set (virtual-view / graph miss) → every
    # column is treated as in-schema so nothing is ever wrongly hidden.
    result_columns = [
        {"name": "a", "dataType": "int"},
        {"name": "_dlt_id", "dataType": "text"},
    ]
    out = mkt._enrich_preview_columns(result_columns, [])
    assert all(c["in_schema"] is True for c in out)
    assert all(c["description"] is None for c in out)


def test_enrich_case_insensitive_snowflake_upper_columns():
    # Snowflake returns UPPER physical column labels (objects created by unquoted
    # DDL fold to UPPER) while the :DProdColumn schema stores logical (lower)
    # names. Matching must be case-insensitive or every real column is wrongly
    # flagged as a system column and hidden by default (the reported bug).
    result_columns = [
        {"name": "RETAIL_BANKING_AMOUNT", "dataType": "decimal"},
        {"name": "RETAIL_BANKING_CARD_ID", "dataType": "decimal"},
        {"name": "_DLT_LOAD_ID", "dataType": "text"},   # dlt system col, also UPPER
        {"name": "_DLT_ID", "dataType": "text"},
    ]
    meta_rows = [
        {"name": "retail_banking_amount", "description": "Transaction amount"},
        {"name": "retail_banking_card_id", "description": ""},
    ]
    out = mkt._enrich_preview_columns(result_columns, meta_rows)
    flags = {c["name"]: c["in_schema"] for c in out}
    assert flags == {
        "RETAIL_BANKING_AMOUNT": True,   # matches lowercase contract name
        "RETAIL_BANKING_CARD_ID": True,
        "_DLT_LOAD_ID": False,           # genuinely not in the schema
        "_DLT_ID": False,
    }
    descs = {c["name"]: c["description"] for c in out}
    assert descs["RETAIL_BANKING_AMOUNT"] == "Transaction amount"


# ── Optional multi-hop lineage folds (pure Python, no Neo4j) ─────────────────
#
# The upstream/downstream folds mirror merge_lookup_graph_rows' dedup style, so
# they're testable with synthetic row dicts the same way.


def test_fold_upstream_dedups_and_targets_source_columns():
    # Two raw columns of one raw table feeding two rendered source columns, plus
    # a duplicate row (must dedup column + edge) and an orphan raw column with no
    # parent dataset (must be skipped, mirroring the primary phantom-table guard).
    rows = [
        {"target_col_uri": "dprod:col:P:emp.name", "up_col_uri": "column:S:hr.emp.name",
         "up_col_name": "name", "up_col_type": "text", "up_col_ordinal": 1,
         "up_table_uri": "dataset:S:hr.emp", "up_schema": "hr", "up_table": "emp",
         "up_source_product_uri": "dprod:S:employee-master",
         "up_source_product_name": "Employee Master", "status": "approved"},
        {"target_col_uri": "dprod:col:P:emp.dept", "up_col_uri": "column:S:hr.emp.dept",
         "up_col_name": "dept", "up_col_type": "text", "up_col_ordinal": 2,
         "up_table_uri": "dataset:S:hr.emp", "up_schema": "hr", "up_table": "emp",
         "up_source_product_uri": "dprod:S:employee-master",
         "up_source_product_name": "Employee Master", "status": "approved"},
        # exact duplicate of the first row
        {"target_col_uri": "dprod:col:P:emp.name", "up_col_uri": "column:S:hr.emp.name",
         "up_col_name": "name", "up_col_type": "text", "up_col_ordinal": 1,
         "up_table_uri": "dataset:S:hr.emp", "up_schema": "hr", "up_table": "emp",
         "up_source_product_uri": "dprod:S:employee-master",
         "up_source_product_name": "Employee Master", "status": "approved"},
        # orphan: no parent dataset URI → skipped
        {"target_col_uri": "dprod:col:P:emp.x", "up_col_uri": "column:S:hr.emp.x",
         "up_col_name": "x", "up_col_type": "text", "up_col_ordinal": 3,
         "up_table_uri": None, "up_schema": "", "up_table": None,
         "up_source_product_uri": None, "up_source_product_name": None, "status": "approved"},
    ]
    out = mkt._fold_upstream(rows)
    # One raw table, deduped to two columns.
    assert len(out["tables"]) == 1
    tbl = out["tables"][0]
    assert tbl["uri"] == "dataset:S:hr.emp"
    assert tbl["source_product_name"] == "Employee Master"
    assert {c["uri"] for c in tbl["columns"]} == {"column:S:hr.emp.name", "column:S:hr.emp.dept"}
    # Edges dedup and every target is a rendered source-column URI.
    assert len(out["edges"]) == 2
    assert {e["target_uri"] for e in out["edges"]} == {"dprod:col:P:emp.name", "dprod:col:P:emp.dept"}
    assert all(e["source_uri"].startswith("column:S:") for e in out["edges"])
    # The orphan raw column never made it into a table or an edge.
    assert all(c["uri"] != "column:S:hr.emp.x" for c in tbl["columns"])


def test_fold_upstream_empty_is_source_aligned_noop():
    # Source-aligned products yield zero upstream rows → empty tables/edges.
    assert mkt._fold_upstream([]) == {"tables": [], "edges": []}


def test_fold_upstream_marks_lookup_rows():
    # The lookup-upstream companion rows arrive tagged is_lookup=True (set in the
    # run block). The fold must thread that onto the edge and badge a raw table
    # that ONLY feeds lookups — while a table feeding a primary mapping stays
    # non-lookup even if it also feeds a lookup.
    rows = [
        # primary: hr_core.employee feeds a real source column
        {"target_col_uri": "dprod:col:P:emp.name", "up_col_uri": "column:S:hr.employee.name",
         "up_col_name": "name", "up_col_type": "text", "up_col_ordinal": 1,
         "up_table_uri": "dataset:S:hr.employee", "up_schema": "hr", "up_table": "employee",
         "up_source_product_uri": "dprod:S:em", "up_source_product_name": "Employee Master",
         "status": "approved"},
        # lookup: hr_core.department feeds a lookup reference column (tagged)
        {"target_col_uri": "dprod:col:P:emp.hr_department_id", "up_col_uri": "column:S:hr.department.department_id",
         "up_col_name": "department_id", "up_col_type": "int", "up_col_ordinal": 1,
         "up_table_uri": "dataset:S:hr.department", "up_schema": "hr", "up_table": "department",
         "up_source_product_uri": "dprod:S:em", "up_source_product_name": "Employee Master",
         "status": "approved", "is_lookup": True},
        # mixed: hr_core.employee ALSO feeds a lookup column → table stays non-lookup
        {"target_col_uri": "dprod:col:P:emp.hr_mgr_id", "up_col_uri": "column:S:hr.employee.mgr_id",
         "up_col_name": "mgr_id", "up_col_type": "int", "up_col_ordinal": 2,
         "up_table_uri": "dataset:S:hr.employee", "up_schema": "hr", "up_table": "employee",
         "up_source_product_uri": "dprod:S:em", "up_source_product_name": "Employee Master",
         "status": "approved", "is_lookup": True},
    ]
    out = mkt._fold_upstream(rows)
    tbl_lookup = {t["table"]: t["is_lookup"] for t in out["tables"]}
    # department feeds ONLY lookups → badged; employee feeds a primary edge → not.
    assert tbl_lookup == {"department": True, "employee": False}
    # Every edge carries the flag; primary vs lookup distinguished.
    assert all("is_lookup" in e for e in out["edges"])
    by_target = {e["target_uri"]: e["is_lookup"] for e in out["edges"]}
    assert by_target["dprod:col:P:emp.name"] is False
    assert by_target["dprod:col:P:emp.hr_department_id"] is True
    assert by_target["dprod:col:P:emp.hr_mgr_id"] is True
    # Invariant preserved: every edge target is a source (product-side) column URI.
    assert all(e["target_uri"].startswith("dprod:col:") for e in out["edges"])


def test_fold_downstream_dedups_and_shapes_edges():
    rows = [
        {"consumer_uri": "dprod:C:active-roster", "consumer_name": "Active Roster",
         "consumer_product_kind": "consumer", "consumer_contract_id": "C-contract",
         "consumer_lifecycle": "published"},
        # duplicate consumer → deduped
        {"consumer_uri": "dprod:C:active-roster", "consumer_name": "Active Roster",
         "consumer_product_kind": "consumer", "consumer_contract_id": "C-contract",
         "consumer_lifecycle": "published"},
        {"consumer_uri": "dprod:D:headcount", "consumer_name": "Headcount",
         "consumer_product_kind": "consumer", "consumer_contract_id": "D-contract",
         "consumer_lifecycle": "approved"},
    ]
    out = mkt._fold_downstream(rows, "dprod:S:employee-master")
    assert len(out["consumers"]) == 2
    # The node-kind seam is stamped on every consumer.
    assert all(c["consumer_kind"] == "data_product" for c in out["consumers"])
    assert out["consumers"][0]["product_kind"] == "consumer"
    # One aggregate edge per consumer, all anchored on the product URI.
    assert len(out["edges"]) == 2
    assert all(e["source_product_uri"] == "dprod:S:employee-master" for e in out["edges"])
    assert all(e["kind"] == "consumes" for e in out["edges"])
    assert {e["target_product_uri"] for e in out["edges"]} == {
        "dprod:C:active-roster", "dprod:D:headcount",
    }


def test_fold_downstream_empty():
    assert mkt._fold_downstream([], "dprod:S:x") == {"consumers": [], "edges": []}


class _FakeNS:
    """Minimal neo4j session stand-in: every query returns no rows."""

    def run(self, _query, **_params):
        return []


class _FakeNeo4jCtx:
    def __enter__(self):
        return _FakeNS()

    def __exit__(self, *_a):
        return False


def test_mapping_graph_query_params_default_false():
    # The absent-param contract: FastAPI resolves both include_* query params to
    # False when a request omits them, so the engineer paths (which never send
    # them) get the legacy shape. Assert the declared defaults rather than the
    # runtime sentinel a direct call would see.
    import inspect

    params = inspect.signature(mkt.get_mapping_graph).parameters
    assert params["include_upstream"].default.default is False
    assert params["include_downstream"].default.default is False


def test_mapping_graph_backcompat_keys(monkeypatch):
    # With the include_* flags off the response is byte-identical to the legacy
    # shape — no upstream/downstream keys leak in (the engineer paths depend on
    # this).
    monkeypatch.setattr(mkt, "_get_settings", lambda session: object())
    monkeypatch.setattr(mkt, "_neo4j_from_settings", lambda settings: _FakeNeo4jCtx())
    out = mkt.get_mapping_graph(
        uri="dataset:P:x", include_upstream=False, include_downstream=False, session=None
    )
    assert set(out.keys()) == {"source_tables", "product", "mappings", "orphan_source_mappings"}


def test_mapping_graph_adds_tiers_only_when_requested(monkeypatch):
    monkeypatch.setattr(mkt, "_get_settings", lambda session: object())
    monkeypatch.setattr(mkt, "_neo4j_from_settings", lambda settings: _FakeNeo4jCtx())
    out = mkt.get_mapping_graph(
        uri="dataset:P:x", include_upstream=True, include_downstream=True, session=None
    )
    assert out["upstream"] == {"tables": [], "edges": []}
    assert out["downstream"] == {"consumers": [], "edges": []}


# ── Product-level lineage (marketplace-wide DAG) ─────────────────────────────
#
# The endpoint dispatches two queries (nodes, then edges) at the same NS. The
# fake dispatches on the query text so we can exercise the Python-side shaping:
# dangling-edge drop, self-edge drop, and (source,target) dedup.


class _LineageNS:
    def __init__(self, nodes, edges):
        self._nodes, self._edges = nodes, edges

    def run(self, query, **_params):
        return self._edges if "CONSUMES" in query else self._nodes


class _LineageCtx:
    def __init__(self, ns):
        self._ns = ns

    def __enter__(self):
        return self._ns

    def __exit__(self, *_a):
        return False


def test_product_lineage_shapes_nodes_and_edges(monkeypatch):
    nodes = [
        {"uri": "dprod:A", "name": "Customer Master", "product_kind": "source",
         "domain": "customer", "tags": ["gold"], "lifecycle_state": "published"},
        {"uri": "dprod:B", "name": "Customer 360", "product_kind": "aggregate",
         "domain": "customer", "tags": [], "lifecycle_state": "published"},
        {"uri": "dprod:C", "name": "Churn Report", "product_kind": "consumer",
         "domain": "customer", "tags": None, "lifecycle_state": "draft"},
    ]
    edges = [
        {"source_uri": "dprod:A", "target_uri": "dprod:B"},
        {"source_uri": "dprod:B", "target_uri": "dprod:C"},
        # duplicate edge → deduped
        {"source_uri": "dprod:A", "target_uri": "dprod:B"},
        # dangling: source not in node set → dropped
        {"source_uri": "dprod:X", "target_uri": "dprod:C"},
        # self-edge → dropped (defensive; the DAG guard forbids it upstream)
        {"source_uri": "dprod:B", "target_uri": "dprod:B"},
    ]
    monkeypatch.setattr(mkt, "_get_settings", lambda session: object())
    monkeypatch.setattr(mkt, "_neo4j_from_settings", lambda settings: _LineageCtx(_LineageNS(nodes, edges)))
    out = mkt.get_product_lineage(session=None)
    assert {n["uri"] for n in out["nodes"]} == {"dprod:A", "dprod:B", "dprod:C"}
    # tags always normalized to a list (None → []).
    assert {n["uri"]: n["tags"] for n in out["nodes"]}["dprod:C"] == []
    # Only the two valid, deduped, data-flow edges survive.
    assert out["edges"] == [
        {"source": "dprod:A", "target": "dprod:B", "kind": "consumes"},
        {"source": "dprod:B", "target": "dprod:C", "kind": "consumes"},
    ]


def test_product_lineage_empty(monkeypatch):
    monkeypatch.setattr(mkt, "_get_settings", lambda session: object())
    monkeypatch.setattr(mkt, "_neo4j_from_settings", lambda settings: _LineageCtx(_LineageNS([], [])))
    assert mkt.get_product_lineage(session=None) == {"nodes": [], "edges": []}


# ── Tag hygiene (shared by canonicalizer + the /odcs/tags endpoint) ──────────


def test_clean_tags_trims_dedups_preserves_first_case():
    from workbench.backend.routers.odcs import _clean_tags
    assert _clean_tags([" Finance ", "finance", "Gold", "", None, "GOLD"]) == ["Finance", "Gold"]
    assert _clean_tags("solo") == ["solo"]          # bare string → single-element list
    assert _clean_tags(None) == []
    assert _clean_tags(42) == []                     # non-list/str → empty


# ── Listing tag filter (case-insensitive, server-side) ───────────────────────


def test_list_published_tag_filter(monkeypatch):
    rows = [
        {"uri": "dprod:A", "product_kind": "source", "tags": ["Finance", "gold"]},
        {"uri": "dprod:B", "product_kind": "consumer", "tags": ["hr"]},
        {"uri": "dprod:C", "product_kind": "consumer", "tags": []},
    ]

    class _ListNS:
        def run(self, _query, **_params):
            return [dict(r) for r in rows]

    monkeypatch.setattr(mkt, "_get_settings", lambda session: object())
    monkeypatch.setattr(mkt, "_neo4j_from_settings", lambda settings: _LineageCtx(_ListNS()))
    monkeypatch.setattr(mkt, "_rubric_short_label_map", lambda: {})
    monkeypatch.setattr(mkt, "_attach_project_id", lambda row, session: None)

    # case-insensitive match on 'finance' → only product A.
    out = mkt.list_published(session=None, owned_by=None, product_kind=None, tag="finance")
    assert [p["uri"] for p in out["products"]] == ["dprod:A"]
    # no tag filter → all three, each with a list-typed tags field.
    out_all = mkt.list_published(session=None, owned_by=None, product_kind=None, tag=None)
    assert out_all["count"] == 3
    assert all(isinstance(p["tags"], list) for p in out_all["products"])
