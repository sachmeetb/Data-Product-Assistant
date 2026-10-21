"""Generalization matrix for cross-product / grain-aware join construction in
the view-DDL generator (`_build_from_fk_inferred`).

One code path must handle every shape below without per-product branching — any
special-case is the overfit smell. Pure functions, no Neo4j. The end-to-end
"correct data against hr_demo" proof lives in scratchpad/repro.py (needs the
source DB); these lock the SQL-emission contract.
"""

import importlib.util
import pathlib

import pytest

_GEN = pathlib.Path(__file__).resolve().parents[3] / (
    "workbench-skills/skills/data-serving-virtual-view/scripts/generate_view_ddl.py"
)
_spec = importlib.util.spec_from_file_location("generate_view_ddl", _GEN)
g = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(g)


def test_view_header_quoting_and_copy_grants():
    # ANSI/Postgres: each part double-quoted independently.
    assert g.PostgresDialect().view_header("public", "vw_x") == \
        'CREATE OR REPLACE VIEW "public"."vw_x"'
    # A dotted (catalog.schema) target namespace is a MULTI-part identifier, not
    # one identifier containing dots. Snowflake folds unquoted DDL to UPPER, so
    # the CREATE header emits each part UPPER (quoted) to match the physical
    # object + the folded reads (sql_ident.quote_created_relation).
    assert g.get_dialect("snowflake").view_header("DWB_SERVING_DB.PUBLIC", "vw_x") == \
        'CREATE OR REPLACE VIEW "DWB_SERVING_DB"."PUBLIC"."VW_X" COPY GRANTS'
    # Databricks/MySQL quote with backticks.
    assert g.get_dialect("databricks").view_header("workspace.default", "vw_x") == \
        'CREATE OR REPLACE VIEW `workspace`.`default`.`vw_x`'
    assert g.get_dialect("mysql").view_header("app", "vw_x") == \
        'CREATE OR REPLACE VIEW `app`.`vw_x`'


def _two_table(scd_policy):
    """salary_history (grain anchor) + cross-product job_assignment_history
    bridge, NO FK between them (different source products)."""
    tables = {
        "public.vw_salary_history": {"schema": "public", "table": "vw_salary_history", "alias": "t1"},
        "public.vw_job_assignment_history": {"schema": "public", "table": "vw_job_assignment_history", "alias": "t2"},
    }
    cols = {
        "public.vw_salary_history": {"column_names": ["employee_id", "amt", "effective_from", "effective_to", "is_current"]},
        "public.vw_job_assignment_history": {"column_names": ["employee_id", "job_id", "department_id", "effective_from", "effective_to", "is_current"], "relationship_kind": "audit_log"},
    }
    return g._build_from_fk_inferred(
        tables=tables, alias_lookup={k: v["alias"] for k, v in tables.items()},
        fk_rows=[], output_dataset_uri="x", columns_by_key=cols, view_schema="public",
        scd_policy=scd_policy, grain_natural_key="employee_id",
    )


def test_case3_cross_product_scd2_asof_bridge():
    """Reported case: SCD-2 target → as-of interval bridge on the natural key."""
    _, jc, _, dedup = _two_table({"type": "scd2", "effective_column": "effective_from"})
    txt = "\n".join(jc)
    assert "t2.employee_id = t1.employee_id" in txt          # natural-key bridge
    assert "t2.effective_from <= t1.effective_from" in txt   # as-of pivot
    assert "t2.effective_to IS NULL OR t2.effective_to > t1.effective_from" in txt
    assert "ROW_NUMBER()" not in txt                         # interval, not current-row
    assert any(b.get("mode") == "as_of" and b.get("cross_product_bridge") for b in dedup)


def test_case2_cross_product_current_state_currentrow_bridge():
    """Current-state target → bridge on key + current-row (latest per key)."""
    _, jc, _, _ = _two_table(None)
    txt = "\n".join(jc)
    assert "t2.employee_id = t1.employee_id" in txt
    assert "ROW_NUMBER()" in txt and "WHERE is_current = true" in txt
    assert "<=" not in txt   # NOT as-of


def test_case1_single_source_fk_unchanged():
    """FK-connected tables → plain FK join, no bridge, no dedup (regression)."""
    tables = {
        "public.vw_employee": {"schema": "public", "table": "vw_employee", "alias": "t1"},
        "public.vw_department": {"schema": "public", "table": "vw_department", "alias": "t2"},
    }
    fk = [{"from_schema": "public", "from_table": "vw_employee", "from_uri": "a",
           "fk_columns": "department_id", "to_schema": "public", "to_table": "vw_department",
           "to_uri": "b", "pk_columns": "department_id"}]
    cols = {"public.vw_employee": {"column_names": ["employee_id", "department_id", "name"]},
            "public.vw_department": {"column_names": ["department_id", "dept_name"]}}
    _, jc, _, dedup = g._build_from_fk_inferred(
        tables=tables, alias_lookup={k: v["alias"] for k, v in tables.items()},
        fk_rows=fk, output_dataset_uri="x", columns_by_key=cols, view_schema="public",
        scd_policy=None, grain_natural_key=None,
    )
    txt = "\n".join(jc)
    assert "t1.department_id = t2.department_id" in txt
    assert "ROW_NUMBER()" not in txt
    assert not dedup


def test_truly_unbridgeable_still_raises():
    """No FK and no shared identity key → still a clean ViewGenerationError
    (we don't silently fabricate a join)."""
    tables = {
        "public.vw_a": {"schema": "public", "table": "vw_a", "alias": "t1"},
        "public.vw_b": {"schema": "public", "table": "vw_b", "alias": "t2"},
    }
    cols = {"public.vw_a": {"column_names": ["a_id", "x"]},
            "public.vw_b": {"column_names": ["b_id", "y"]}}  # no shared key
    with pytest.raises(g.ViewGenerationError):
        g._build_from_fk_inferred(
            tables=tables, alias_lookup={k: v["alias"] for k, v in tables.items()},
            fk_rows=[], output_dataset_uri="x", columns_by_key=cols, view_schema="public",
            scd_policy={"type": "scd2"}, grain_natural_key=None,
        )


def test_case4_direct_asof_lookup_no_bridge():
    """SCD-2 anchor with an effective-dated reference table keyed on the anchor
    (no bridge): the `asof` lookup strategy emits an interval predicate."""
    params = {
        "lookup_table": "vw_salary_history", "key_column": "employee_id",
        "value_column": "amt", "selection_strategy": "asof",
        "effective_from_column": "effective_from", "effective_to_column": "effective_to",
        "pivot_column": "as_of_date",
    }
    m = [{
        "mapping_uri": "m:salary_asof", "product_col": "salary_at_review",
        "source_schema": "public", "source_table": "vw_review", "source_col": "employee_id",
        "source_kind": "dprod", "transform_kind": "lookup",
        "transform_expression": "", "transform_decorators_json": None,
        "transform_params_json": __import__("json").dumps(params),
    }]
    alias_lookup = {"public.vw_review": "t1"}
    lookup_aliases, lookup_meta = {}, {}
    expr = g._compile_lookup("salary_at_review", m, alias_lookup, lookup_aliases, lookup_meta, "public")
    join = g._render_lookup_join_from_meta(next(iter(lookup_meta.values())))
    assert "t1.employee_id = lk1.employee_id" in join
    assert "lk1.effective_from <= t1.as_of_date" in join
    assert "lk1.effective_to IS NULL OR lk1.effective_to > t1.as_of_date" in join


def test_temporal_helpers():
    cols = ["employee_id", "effective_from", "effective_to", "is_current"]
    assert g._pick_effective_from_column(cols) == "effective_from"
    assert g._pick_effective_to_column(cols) == "effective_to"
    assert g._is_effective_dated(cols) is True
    assert g._is_effective_dated(["id", "name"]) is False
    assert g._shared_key_columns(["employee_id", "x"], ["employee_id", "job_id"]) == ["employee_id"]
    # preferred natural key floats to the front when shared
    assert g._shared_key_columns(["a_id", "b_id"], ["a_id", "b_id"], preferred="b_id")[0] == "b_id"


# --- Gap A: transitive chaining ----------------------------------------------

_ACC_COLS = ["account_id", "account_number", "product_code", "currency", "status"]
_CUS_COLS = ["customer_id", "name", "kyc_status", "onboarded_date"]
_TXN_COLS = ["txn_id", "account_id", "customer_id", "amount", "channel"]


def test_gapA_two_hop_chain_all_mapped():
    """G1 (banking): account —account_id→ transaction —customer_id→ customer,
    all mapped, no FK — the chain resolves; customer joins the txn alias."""
    tables = {
        "public.vw_account":     {"schema": "public", "table": "vw_account", "alias": "t1"},
        "public.vw_customer":    {"schema": "public", "table": "vw_customer", "alias": "t2"},
        "public.vw_transaction": {"schema": "public", "table": "vw_transaction", "alias": "t3"},
    }
    cols = {
        "public.vw_account":     {"column_names": _ACC_COLS},
        "public.vw_customer":    {"column_names": _CUS_COLS},
        "public.vw_transaction": {"column_names": _TXN_COLS},
    }
    _, jc, _, dedup = g._build_from_fk_inferred(
        tables=tables, alias_lookup={k: v["alias"] for k, v in tables.items()},
        fk_rows=[], output_dataset_uri="x", columns_by_key=cols, view_schema="public",
        scd_policy=None, grain_natural_key=None,
    )
    txt = "\n".join(jc)
    assert "t3.account_id = t1.account_id" in txt
    assert "t2.customer_id = t3.customer_id" in txt      # chained, not anchored
    assert "ROW_NUMBER()" not in txt                     # plain equi hops
    vias = {b["table"]: b.get("via") for b in dedup if b.get("cross_product_bridge")}
    assert vias["public.vw_transaction"] == "public.vw_account"
    assert vias["public.vw_customer"] == "public.vw_transaction"


# --- Gap B: junction bridge from a consumed-but-unmapped dataset -------------

def _txn_candidate(key="public.vw_transaction", phys="transaction", kind="fact"):
    return {"key": key, "schema": "public", "table": key.split(".", 1)[1],
            "uri": f"u:{phys}", "phys": phys, "relationship_kind": kind,
            "product_name": "Transaction Master", "src_deployment_status": "deployed",
            "cols": _TXN_COLS}


def _acc_cus_tables():
    tables = {
        "public.vw_account":  {"schema": "public", "table": "vw_account", "alias": "t1"},
        "public.vw_customer": {"schema": "public", "table": "vw_customer", "alias": "t2"},
    }
    cols = {
        "public.vw_account":  {"column_names": _ACC_COLS},
        "public.vw_customer": {"column_names": _CUS_COLS},
    }
    return tables, cols


def test_gapB_junction_distinct_emission():
    """G2: unmapped consumed transaction bridges account↔customer as a pure
    DISTINCT-projected derived table — fan-out-safe."""
    tables, cols = _acc_cus_tables()
    _, jc, _, dedup = g._build_from_fk_inferred(
        tables=tables, alias_lookup={k: v["alias"] for k, v in tables.items()},
        fk_rows=[], output_dataset_uri="x", columns_by_key=cols, view_schema="public",
        scd_policy=None, grain_natural_key=None,
        junction_candidates=[_txn_candidate()],
    )
    txt = "\n".join(jc)
    assert "SELECT DISTINCT account_id, customer_id" in txt
    assert "FROM public.vw_transaction" in txt
    assert "t3.account_id = t1.account_id" in txt        # junction → anchor
    assert "t2.customer_id = t3.customer_id" in txt      # customer → junction
    jx = next(b for b in dedup if b.get("mode") == "junction_distinct")
    assert jx["bridge_only"] is True
    assert jx["keys"] == ["account_id", "customer_id"]


def test_gapB_tied_candidates_raise():
    """G3: two equally-scored junction candidates → loud error naming both."""
    tables, cols = _acc_cus_tables()
    with pytest.raises(g.ViewGenerationError) as ei:
        g._build_from_fk_inferred(
            tables=tables, alias_lookup={k: v["alias"] for k, v in tables.items()},
            fk_rows=[], output_dataset_uri="x", columns_by_key=cols, view_schema="public",
            scd_policy=None, grain_natural_key=None,
            junction_candidates=[
                _txn_candidate(key="public.vw_ledger_main", phys="ledger_main"),
                _txn_candidate(key="public.vw_ledger_shadow", phys="ledger_shadow"),
            ],
        )
    msg = str(ei.value)
    assert "vw_ledger_main" in msg and "vw_ledger_shadow" in msg


def test_gapB_no_candidate_mentions_consumes():
    """G4: still-unbridgeable error now names the missing-CONSUMES cause."""
    tables, cols = _acc_cus_tables()
    with pytest.raises(g.ViewGenerationError) as ei:
        g._build_from_fk_inferred(
            tables=tables, alias_lookup={k: v["alias"] for k, v in tables.items()},
            fk_rows=[], output_dataset_uri="x", columns_by_key=cols, view_schema="public",
            scd_policy=None, grain_natural_key=None, junction_candidates=[],
        )
    assert ":CONSUMES" in str(ei.value)


def test_gapB_scd2_junction_projects_effective_dating():
    """G7: SCD-2 target + effective-dated junction → eff columns inside the
    DISTINCT projection and an as-of interval in the ON."""
    tables = {
        "public.vw_anchor":  {"schema": "public", "table": "vw_anchor", "alias": "t1"},
        "public.vw_history": {"schema": "public", "table": "vw_history", "alias": "t2"},
    }
    cols = {
        "public.vw_anchor":  {"column_names": ["a_id", "amt", "effective_from", "effective_to"]},
        "public.vw_history": {"column_names": ["b_id", "grade"]},
    }
    junction = {"key": "public.vw_link", "schema": "public", "table": "vw_link",
                "uri": "u:link", "phys": "link", "relationship_kind": "general_membership",
                "product_name": "Link", "src_deployment_status": "deployed",
                "cols": ["a_id", "b_id", "effective_from", "effective_to"]}
    _, jc, _, dedup = g._build_from_fk_inferred(
        tables=tables, alias_lookup={k: v["alias"] for k, v in tables.items()},
        fk_rows=[], output_dataset_uri="x", columns_by_key=cols, view_schema="public",
        scd_policy={"type": "scd2"}, grain_natural_key=None,
        junction_candidates=[junction],
    )
    txt = "\n".join(jc)
    assert "SELECT DISTINCT a_id, b_id, effective_from, effective_to" in txt
    assert ".effective_from <= t1.effective_from" in txt   # as-of vs anchor pivot
    jx = next(b for b in dedup if b.get("mode") == "junction_distinct")
    assert set(jx["keys"]) == {"a_id", "b_id", "effective_from", "effective_to"}


def test_explicit_bridge_only_distinct_and_no_placeholder():
    """G6: explicit joins[] with an unmapped bridge_only entry — resolved to
    its real relation (no `?.?`) and emitted as SELECT DISTINCT."""
    tables = {
        "public.vw_account":  {"schema": "public", "table": "vw_account", "alias": "acc"},
        "public.vw_customer": {"schema": "public", "table": "vw_customer", "alias": "cus"},
    }
    joins = [
        {"alias": "acc", "dataset_uri": "d:acc", "kind": "cross", "predicate": ""},
        {"alias": "txn", "dataset_uri": "d:txn", "kind": "left",
         "predicate": "txn.account_id = acc.account_id", "bridge_only": True},
        {"alias": "cus", "dataset_uri": "d:cus", "kind": "left",
         "predicate": "cus.customer_id = txn.customer_id"},
    ]
    from_clause, jc, dedup = g._build_from_explicit(
        joins=joins, tables=tables, view_schema="public", output_dataset_uri="x",
        columns_by_key={}, alias_relations={"txn": ("public", "vw_transaction")},
    )
    txt = "\n".join(jc)
    assert "?" not in txt
    assert "SELECT DISTINCT account_id, customer_id" in txt
    assert "FROM public.vw_transaction" in txt
    assert "ON txn.account_id = acc.account_id" in txt
    jx = next(b for b in dedup if b.get("mode") == "junction_distinct")
    assert jx["keys"] == ["account_id", "customer_id"]


def test_preflight_generator_consistency_banking():
    """G5: the preflight promise — identical banking fixture through both
    sides produces the same join order, partners, and key choices."""
    from workbench.backend import join_preflight as jp

    class _Result(list):
        def single(self):
            return self[0] if self else None

    class _Sess:
        def run(self, query, **params):
            if "MAPS_SOURCE_COLUMN" in query:
                return _Result([
                    {"uri": "u:acc", "phys": "account", "product_uri": "pA",
                     "product_name": "pA", "mapped_cols": 9, "cols": _ACC_COLS},
                    {"uri": "u:cus", "phys": "customer", "product_uri": "pB",
                     "product_name": "pB", "mapped_cols": 8, "cols": _CUS_COLS},
                ])
            if "HAS_DATASET_TRANSFORM" in query:
                return _Result([{"joins_json": None, "scd_policy_json": None}])
            if "CONSUMES" in query:
                return _Result([{"uri": "u:txn", "phys": "transaction",
                                 "relationship_kind": "fact", "product_uri": "pC",
                                 "product_name": "Transaction Master",
                                 "src_deployment_status": "deployed",
                                 "cols": _TXN_COLS}])
            return _Result([])

    pre = jp.analyze_dataset_connectivity(_Sess(), "p", "ds")
    assert pre["connected"] is True

    tables, cols = _acc_cus_tables()
    _, jc, _, dedup = g._build_from_fk_inferred(
        tables=tables, alias_lookup={k: v["alias"] for k, v in tables.items()},
        fk_rows=[], output_dataset_uri="x", columns_by_key=cols, view_schema="public",
        scd_policy=None, grain_natural_key=None,
        junction_candidates=[_txn_candidate()],
    )
    # Same dependency order (anchor, junction, customer)...
    pre_order = [j["dataset_uri"] for j in pre["recommended_joins"]]
    assert pre_order == ["u:acc", "u:txn", "u:cus"]
    # ...and the same key on each hop, on both sides.
    assert "account_id" in pre["recommended_joins"][1]["predicate"]
    assert "customer_id" in pre["recommended_joins"][2]["predicate"]
    txt = "\n".join(jc)
    assert "t3.account_id = t1.account_id" in txt
    assert "t2.customer_id = t3.customer_id" in txt


# --- lookup aggregate_expression (composite aggregates) -----------------------

def _churn_mapping(agg_expr, extra_params=None, source_table="vw_campaign_audience"):
    params = {
        "lookup_table": "transaction_ledger", "key_column": "customer_id",
        "selection_strategy": "aggregate",
        "aggregate_expression": agg_expr,
    }
    params.update(extra_params or {})
    return [{
        "mapping_uri": "m:churn", "product_col": "churn_risk_score",
        "source_schema": "public", "source_table": source_table,
        "source_col": "customer_id", "source_kind": "dprod",
        "transform_kind": "lookup", "transform_expression": "",
        "transform_decorators_json": None,
        "transform_params_json": __import__("json").dumps(params),
    }]


_CHURN_EXPR = ("GREATEST(0, LEAST(100, (CURRENT_DATE - MAX(txn_timestamp)::date) * 2 "
               "- COUNT(txn_id) FILTER (WHERE txn_timestamp >= CURRENT_DATE - 90) * 5))")


def test_lookup_aggregate_expression_emission():
    """Composite aggregate: one GROUP BY derived join, value ref = agg_value."""
    alias_lookup = {"public.vw_campaign_audience": "t1"}
    lookup_aliases, lookup_meta = {}, {}
    expr = g._compile_lookup("churn_risk_score", _churn_mapping(_CHURN_EXPR),
                             alias_lookup, lookup_aliases, lookup_meta, "public")
    assert expr == "lk1.agg_value"
    join = g._render_lookup_join_from_meta(next(iter(lookup_meta.values())))
    assert f"SELECT customer_id, {_CHURN_EXPR} AS agg_value" in join
    assert "GROUP BY customer_id" in join
    assert "t1.customer_id = lk1.customer_id" in join
    # dprod-mode lookup_table rewrite still applies
    assert "FROM public.vw_transaction_ledger" in join


def test_lookup_aggregate_expression_dedup():
    """Identical expressions share one scan; different expressions get two."""
    alias_lookup = {"public.vw_campaign_audience": "t1"}
    lookup_aliases, lookup_meta = {}, {}
    e1 = g._compile_lookup("churn_risk_score", _churn_mapping(_CHURN_EXPR),
                           alias_lookup, lookup_aliases, lookup_meta, "public")
    e2 = g._compile_lookup("churn_risk_score_b", _churn_mapping(_CHURN_EXPR),
                           alias_lookup, lookup_aliases, lookup_meta, "public")
    assert e1 == e2 and len(lookup_meta) == 1
    e3 = g._compile_lookup("txn_total", _churn_mapping("SUM(amount)"),
                           alias_lookup, lookup_aliases, lookup_meta, "public")
    assert e3 == "lk2.agg_value" and len(lookup_meta) == 2


def test_lookup_aggregate_function_arm_unchanged():
    """Back-compat: plain aggregate_function emission is untouched."""
    params = {"lookup_table": "transaction_ledger", "key_column": "customer_id",
              "value_column": "amount", "selection_strategy": "aggregate",
              "aggregate_function": "SUM"}
    m = [{
        "mapping_uri": "m:t", "product_col": "txn_total",
        "source_schema": "public", "source_table": "vw_campaign_audience",
        "source_col": "customer_id", "source_kind": "dprod",
        "transform_kind": "lookup", "transform_expression": "",
        "transform_decorators_json": None,
        "transform_params_json": __import__("json").dumps(params),
    }]
    alias_lookup = {"public.vw_campaign_audience": "t1"}
    lookup_aliases, lookup_meta = {}, {}
    expr = g._compile_lookup("txn_total", m, alias_lookup, lookup_aliases, lookup_meta, "public")
    assert expr == "lk1.amount"
    join = g._render_lookup_join_from_meta(next(iter(lookup_meta.values())))
    assert "SUM(amount) AS amount" in join and "GROUP BY customer_id" in join


def test_lookup_aggregate_expression_without_function_or_value():
    """aggregate_expression alone (no aggregate_function, no value_column) must
    NOT degrade to equi — the expression carries the whole projected value."""
    alias_lookup = {"public.vw_campaign_audience": "t1"}
    lookup_aliases, lookup_meta = {}, {}
    g._compile_lookup("churn_risk_score", _churn_mapping(_CHURN_EXPR),
                      alias_lookup, lookup_aliases, lookup_meta, "public")
    meta = next(iter(lookup_meta.values()))
    assert meta["strategy"] == "aggregate"


def test_substitute_product_aliases_derived_on_derived():
    """The enriched-CTE substitution rewrites sibling product-column refs."""
    expr = "GREATEST(0, LEAST(100, (CURRENT_DATE - last_txn_date::date) * 2 - txn_count_90d * 5))"
    out = g._substitute_product_aliases(expr, ["last_txn_date", "txn_count_90d"])
    assert "last_txn_date" in out and "txn_count_90d" in out  # refs preserved/rewritten, not dropped


def test_lookup_table_product_name_qualifier_rewritten():
    """The UI's source picker labels dprod tables `<Product Name>.<dataset>`
    (product name is NOT a SQL schema — spaces and all). Any dprod lookup_table
    without an explicit vw_ relation must resolve to the generated view."""
    for authored in ("transaction_ledger",
                     "analytics.transaction_ledger",
                     "Transaction Ledger.transaction_ledger"):
        alias_lookup = {"public.vw_campaign_audience": "t1"}
        lookup_aliases, lookup_meta = {}, {}
        g._compile_lookup(
            "churn_risk_score",
            _churn_mapping(_CHURN_EXPR, extra_params={"lookup_table": authored}),
            alias_lookup, lookup_aliases, lookup_meta, "public")
        join = g._render_lookup_join_from_meta(next(iter(lookup_meta.values())))
        assert "FROM public.vw_transaction_ledger" in join, authored
    # explicit vw_ relation stays an override
    alias_lookup = {"public.vw_campaign_audience": "t1"}
    lookup_aliases, lookup_meta = {}, {}
    g._compile_lookup(
        "churn_risk_score",
        _churn_mapping(_CHURN_EXPR, extra_params={"lookup_table": "other.vw_ledger_copy"}),
        alias_lookup, lookup_aliases, lookup_meta, "public")
    join = g._render_lookup_join_from_meta(next(iter(lookup_meta.values())))
    assert "FROM other.vw_ledger_copy" in join


# ── Served-location relation resolution (consumer over a materialized source) ──
#
# When a dpe-cf consumer :CONSUMES a source product that was materialized to a
# DISTINCT platform (e.g. a MySQL source loaded into Databricks), the FROM clause
# must reference the source's REAL catalog.schema.table — not a co-located
# vw_<name> in the consumer's own view_schema. The served map (keyed by
# _safe_name(source_dataset_physical) → "catalog.schema.relation") drives this;
# an EMPTY map must preserve the co-located behavior byte-for-byte.

class TestServedLocationRelation:
    def teardown_method(self):
        # Never leak the module-level served map between tests.
        g._set_source_served_map(None)

    def test_empty_map_is_colocated(self):
        g._set_source_served_map({})
        row = {"source_kind": "dprod", "source_dprod_dataset_physical": "Order Header"}
        assert g._resolve_source_relation(row, "public") == ("public", "vw_order_header")
        assert g._served_relation("Order Header") is None

    def test_served_map_databricks_three_level(self):
        g._set_source_served_map({"order_header": "main.sales.order_header"})
        row = {"source_kind": "dprod", "source_dprod_dataset_physical": "Order Header"}
        # catalog.schema in the schema slot; FROM emits schema.table raw → 3-level.
        assert g._resolve_source_relation(row, "public") == ("main.sales", "order_header")

    def test_served_map_two_level(self):
        g._set_source_served_map({"customer": "analytics.customer"})
        assert g._dprod_relation("Customer", "public") == ("analytics", "customer")

    def test_unmapped_dataset_falls_back_to_colocated(self):
        g._set_source_served_map({"order_header": "main.sales.order_header"})
        # A dataset not in the served map keeps the co-located vw_<name> path.
        assert g._dprod_relation("Unknown Table", "myschema") == ("myschema", "vw_unknown_table")

    def test_catalog_and_literal_kinds_unaffected(self):
        g._set_source_served_map({"order_header": "main.sales.order_header"})
        cat = {"source_kind": "catalog", "source_schema_raw": "raw", "source_table_raw": "t"}
        assert g._resolve_source_relation(cat, "public") == ("raw", "t")
        assert g._resolve_source_relation({"source_kind": "literal"}, "public") == (None, None)

    def test_map_reset_prevents_leakage(self):
        g._set_source_served_map({"order_header": "main.sales.order_header"})
        g._set_source_served_map(None)  # fresh invocation clears it
        assert g._served_relation("Order Header") is None

    def test_lookup_table_honors_served_map(self):
        g._set_source_served_map({"transaction_ledger": "main.fin.transaction_ledger"})
        alias_lookup = {"public.vw_campaign_audience": "t1"}
        lookup_aliases, lookup_meta = {}, {}
        g._compile_lookup(
            "churn_risk_score",
            _churn_mapping(_CHURN_EXPR, extra_params={"lookup_table": "transaction_ledger"}),
            alias_lookup, lookup_aliases, lookup_meta, "public")
        join = g._render_lookup_join_from_meta(next(iter(lookup_meta.values())))
        assert "FROM main.fin.transaction_ledger" in join


# ── A1: URI-keyed build-time JOIN-column validation (defense-in-depth) ───────
# columns_by_key entries MUST carry `uri` for validation to engage; fk rows
# already carry from_uri/to_uri. (Fixtures without `uri` — like the cases above —
# leave columns_by_uri empty, so validation is a no-op: no false positives.)


def _run_fk_validation(*, fk_columns="department_id", pk_columns="department_id",
                       from_cols=("employee_id", "department_id", "name"),
                       to_cols=("department_id", "dept_name")):
    tables = {
        "public.vw_employee": {"schema": "public", "table": "vw_employee", "alias": "t1"},
        "public.vw_department": {"schema": "public", "table": "vw_department", "alias": "t2"},
    }
    fk = [{"from_schema": "public", "from_table": "vw_employee", "from_uri": "u:emp",
           "fk_columns": fk_columns, "to_schema": "public", "to_table": "vw_department",
           "to_uri": "u:dept", "pk_columns": pk_columns}]
    cols = {
        "public.vw_employee": {"uri": "u:emp", "column_names": list(from_cols)},
        "public.vw_department": {"uri": "u:dept", "column_names": list(to_cols)},
    }
    diags = []
    result = g._build_from_fk_inferred(
        tables=tables, alias_lookup={k: v["alias"] for k, v in tables.items()},
        fk_rows=fk, output_dataset_uri="x", columns_by_key=cols, view_schema="public",
        scd_policy=None, grain_natural_key=None, join_diagnostics=diags,
    )
    return result, diags


def test_fk_join_nonexistent_column_raises():
    """FK payload names a column absent from the joined dataset → hard fail at
    BUILD with a message naming the bad column + the ones that exist."""
    with pytest.raises(g.ViewGenerationError) as e:
        _run_fk_validation(fk_columns="employee_dept")
    msg = str(e.value)
    assert "employee_dept" in msg
    assert "employee_id" in msg  # lists the columns that DO exist


def test_fk_join_valid_distinct_names_pass():
    """Differently-named but real fc/pc → no raise, correct oriented condition."""
    (_, jc, _, _), diags = _run_fk_validation(
        fk_columns="dept_ref", pk_columns="department_id",
        from_cols=("employee_id", "dept_ref", "name"),
    )
    txt = "\n".join(jc)
    assert "t1.dept_ref = t2.department_id" in txt
    assert not diags


def test_fk_join_unknown_column_metadata_diagnoses_not_raises():
    """No `uri` on the column metadata → columns_by_uri empty → a visible
    `validation_unavailable` diagnostic, NOT a false-positive failure."""
    tables = {
        "public.vw_employee": {"schema": "public", "table": "vw_employee", "alias": "t1"},
        "public.vw_department": {"schema": "public", "table": "vw_department", "alias": "t2"},
    }
    fk = [{"from_schema": "public", "from_table": "vw_employee", "from_uri": "u:emp",
           "fk_columns": "department_id", "to_schema": "public", "to_table": "vw_department",
           "to_uri": "u:dept", "pk_columns": "department_id"}]
    cols = {  # NO uri field → nothing to validate against
        "public.vw_employee": {"column_names": ["employee_id", "department_id"]},
        "public.vw_department": {"column_names": ["department_id"]},
    }
    diags = []
    _, jc, _, _ = g._build_from_fk_inferred(
        tables=tables, alias_lookup={k: v["alias"] for k, v in tables.items()},
        fk_rows=fk, output_dataset_uri="x", columns_by_key=cols, view_schema="public",
        scd_policy=None, grain_natural_key=None, join_diagnostics=diags,
    )
    assert any(d["kind"] == "validation_unavailable" for d in diags)
    assert "t1.department_id = t2.department_id" in "\n".join(jc)  # join still emitted


def test_fk_join_composite_length_mismatch_raises():
    """Composite FK with unequal column counts → hard fail (invalid ON clause)."""
    with pytest.raises(g.ViewGenerationError) as e:
        _run_fk_validation(fk_columns="department_id,extra", pk_columns="department_id")
    assert "mismatched" in str(e.value).lower()


def test_explicit_join_bad_column_warns_not_raises():
    """Engineer-authored explicit predicate with a nonexistent column → a
    non-fatal `join_predicate_warning`, never a hard fail."""
    tables = {
        "public.vw_account": {"schema": "public", "table": "vw_account", "alias": "acc"},
        "public.vw_customer": {"schema": "public", "table": "vw_customer", "alias": "cus"},
    }
    joins = [
        {"alias": "acc", "dataset_uri": "d:acc", "kind": "cross", "predicate": ""},
        {"alias": "txn", "dataset_uri": "d:txn", "kind": "left",
         "predicate": "txn.account_id = acc.account_id", "bridge_only": True},
        {"alias": "cus", "dataset_uri": "d:cus", "kind": "left",
         "predicate": "cus.bogus_col = txn.customer_id"},
    ]
    cols = {
        "public.vw_account": {"uri": "u:acc", "column_names": ["account_id"]},
        "public.vw_customer": {"uri": "u:cus", "column_names": ["customer_id", "name"]},
    }
    diags = []
    _fc, _jc, _d = g._build_from_explicit(
        joins=joins, tables=tables, view_schema="public", output_dataset_uri="x",
        columns_by_key=cols, alias_relations={"txn": ("public", "vw_transaction")},
        join_diagnostics=diags,
    )
    flagged = [(d["kind"], d["column"]) for d in diags]
    assert ("join_predicate_warning", "bogus_col") in flagged
    assert all(d["column"] != "account_id" for d in diags)  # valid columns not flagged


# ── Column-name safety for dprod (product-view) source references ────────────
# A `dprod` source is another product's deployed view, whose columns are aliased
# _safe_name(product_col). So a reference to a PO-authored CF column like
# `Mixed Case` must resolve to the safe identifier `mixed_case`. Catalog
# (raw-table) sources keep their real name; SA dprod names are already safe.

class TestSourceColRef:
    def test_dprod_source_special_char_name_is_safe_named(self):
        m = {"source_col": "Mixed Case", "source_kind": "dprod"}
        assert g._source_col_ref(m) == "mixed_case"

    def test_dprod_source_already_snake_is_noop(self):
        m = {"source_col": "customer_id", "source_kind": "dprod"}
        assert g._source_col_ref(m) == "customer_id"

    def test_catalog_source_keeps_raw_name(self):
        m = {"source_col": "CustomerID", "source_kind": "catalog"}
        assert g._source_col_ref(m) == "CustomerID"

    def test_missing_source_col_is_empty(self):
        assert g._source_col_ref({"source_kind": "dprod"}) == ""


# ── PO row filter authored against OUTPUT columns → source expressions ────────
# A base-CTE WHERE can't reference the SELECT's own AS-aliases, so a PO filter
# like `employment_status = 'active'` (alias of t1.hr_employment_status) must be
# inlined to its source expression. On Postgres it only "worked" when the output
# name coincided with a source column; on Databricks/Snowflake it errors
# (UNRESOLVED_COLUMN). The rewrite must be whole-word + string-literal safe.

class TestFilterPredicateRewrite:
    _SELECT_PARTS = [
        "t1.hr_employment_status AS employment_status",
        "lk2.hr_dept_name AS department_name",
        "CASE WHEN lk1.hr_amount < 60000 THEN 'A' ELSE 'B' END AS compensation_band",
    ]

    def _map(self):
        return g._output_alias_expr_map(self._SELECT_PARTS)

    def test_alias_map_splits_on_last_as(self):
        m = self._map()
        assert m["employment_status"] == "t1.hr_employment_status"
        assert m["compensation_band"].startswith("CASE WHEN")

    def test_passthrough_output_col_inlined_to_source(self):
        out = g._rewrite_predicate_output_cols("employment_status = 'active'", self._map())
        assert out == "(t1.hr_employment_status) = 'active'"

    def test_string_literal_is_not_rewritten(self):
        # The column name appearing INSIDE a quoted literal must be left alone.
        out = g._rewrite_predicate_output_cols("department_name = 'employment_status'", self._map())
        assert out == "(lk2.hr_dept_name) = 'employment_status'"

    def test_derived_column_inlines_full_expression(self):
        out = g._rewrite_predicate_output_cols("compensation_band = 'A'", self._map())
        assert out == "(CASE WHEN lk1.hr_amount < 60000 THEN 'A' ELSE 'B' END) = 'A'"

    def test_already_qualified_source_ref_is_noop(self):
        # A hand-written source predicate (e.g. the snapshot-SCD path) is untouched.
        pred = "t1.hr_employment_status = 'x'"
        assert g._rewrite_predicate_output_cols(pred, self._map()) == pred

    def test_empty_map_or_predicate_is_noop(self):
        assert g._rewrite_predicate_output_cols("employment_status = 'x'", {}) == "employment_status = 'x'"
        assert g._rewrite_predicate_output_cols("", self._map()) == ""


class TestRawExpressionDialectTranslation:
    """A raw ``transform_kind='expression'`` authored in one dialect (the steward
    catalog's Postgres) must be rendered to the target engine's native SQL at emit
    time — closing the "verbatim raw-SQL emission" gap. The reported break: the
    steward ``phone_digits_only`` template ``REGEXP_REPLACE(<phone>, '[^0-9]', '', 'g')``
    ships the Postgres global ``'g'`` flag, which Databricks reads as an INT position
    and fails to cast."""

    @staticmethod
    def _mapping():
        import json
        return [{
            "transform_kind": "expression",
            # As stored after the mapper fills the catalog's <phone> hint with the
            # real source column; _substitute_inputs then aliases it to t3.mobile.
            "transform_expression": "REGEXP_REPLACE(mobile, '[^0-9]', '', 'g')",
            "transform_decorators_json": json.dumps({"standardization": ["trim"]}),
            "expression_dialect": "postgres",
            "source_schema": "public",
            "source_table": "customers",
            "source_col": "mobile",
            "transform_inputs_json": None,
        }]

    def _compile(self, dialect_name):
        return g._compile_select_expr(
            "mobile_number", self._mapping(), {"public.customers": "t3"},
            {}, {}, dialect=g.get_dialect(dialect_name),
        )

    def test_databricks_drops_postgres_g_flag(self):
        out = self._compile("databricks")
        assert "'g'" not in out, f"Databricks emit must not carry the Postgres 'g' flag: {out}"
        assert out.startswith("TRIM("), f"trim decorator must still wrap the result: {out}"
        assert "REGEXP_REPLACE(t3.mobile, '[^0-9]', '')" in out

    def test_postgres_is_verbatim_noop(self):
        # Same-dialect path returns the expression untouched — zero regression for
        # the (overwhelmingly common) Postgres-served products.
        out = self._compile("postgres")
        assert "REGEXP_REPLACE(t3.mobile, '[^0-9]', '', 'g')" in out

    def test_snowflake_keeps_g_flag(self):
        # Snowflake's REGEXP_REPLACE accepts the flags arg, so it is preserved.
        out = self._compile("snowflake")
        assert "'g'" in out

    def test_unknown_target_fails_open_to_verbatim(self):
        # ansi/duckdb aren't served-validation platforms → helper leaves it verbatim.
        out = self._compile("ansi")
        assert "REGEXP_REPLACE(t3.mobile, '[^0-9]', '', 'g')" in out
