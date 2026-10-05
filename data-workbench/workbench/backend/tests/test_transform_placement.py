"""Tests for the transform-placement planner (Phase 2, Axis 2)."""
from __future__ import annotations

from workbench.backend.platform.transform_placement import (
    build_elt_plan,
    classify_op,
    landing_name,
    plan_extract_projections,
    plan_placement,
    plan_pushdown_filters,
    realized_placement,
    rewrite_base_relations,
)


def _base(body_inner: str) -> str:
    return f"WITH base AS ({body_inner}) SELECT * FROM base"


class TestClassifyOp:
    def test_mask_always_forced_to_extract(self):
        for placement in ("transform_on_extract", "hybrid", "transfer_then_transform"):
            side, _reason, forced = classify_op("mask", placement)
            assert side == "extract"
            assert forced is True

    def test_transform_on_extract_pushes_everything(self):
        side, _r, forced = classify_op("join", "transform_on_extract")
        assert side == "extract" and forced is False

    def test_elt_defers_everything_except_mask(self):
        assert classify_op("join", "transfer_then_transform")[0] == "target"
        assert classify_op("filter", "transfer_then_transform")[0] == "target"
        assert classify_op("mask", "transfer_then_transform")[0] == "extract"

    def test_hybrid_pushes_reducers_defers_heavy(self):
        assert classify_op("filter", "hybrid")[0] == "extract"
        assert classify_op("projection", "hybrid")[0] == "extract"
        assert classify_op("aggregate", "hybrid")[0] == "target"
        assert classify_op("scd2", "hybrid")[0] == "target"
        assert classify_op("join", "hybrid")[0] == "target"

    def test_hybrid_unknown_op_defaults_to_target_with_flag(self):
        side, reason, _ = classify_op("frobnicate", "hybrid")
        assert side == "target"
        assert "unclassified" in reason

    def test_hybrid_full_column_dsl_classifies_deliberately(self):
        # Every VALID_TRANSFORM_KINDS column kind must classify without the
        # "unclassified" fallback (would otherwise silently defer + warn).
        column_kinds = [
            "direct", "cast", "format", "concat", "split", "substring", "case",
            "arithmetic", "lookup", "literal", "expression", "bucket", "mask",
            "hash", "window",
        ]
        for kind in column_kinds:
            _side, reason, _forced = classify_op(kind, "hybrid")
            assert "unclassified" not in reason, kind

    def test_direct_is_a_source_side_reducer(self):
        # a straight passthrough/projection is cheap to push to the source
        assert classify_op("direct", "hybrid")[0] == "extract"

    def test_scalar_reshapers_defer_to_target(self):
        for kind in ("format", "split", "substring", "expression"):
            assert classify_op(kind, "hybrid")[0] == "target", kind


class TestPlanPlacement:
    def test_hybrid_split(self):
        ops = [
            {"kind": "filter", "dataset": "orders"},
            {"kind": "mask", "column": "ssn"},
            {"kind": "aggregate", "dataset": "orders"},
            {"kind": "join", "dataset": "orders"},
        ]
        d = plan_placement("hybrid", ops)
        extract_kinds = {o.kind for o in d.extract_ops}
        target_kinds = {o.kind for o in d.target_ops}
        assert extract_kinds == {"filter", "mask"}
        assert target_kinds == {"aggregate", "join"}
        assert [o.kind for o in d.forced_pre_boundary] == ["mask"]

    def test_transform_on_extract_all_extract(self):
        ops = [{"kind": "join"}, {"kind": "aggregate"}, {"kind": "cast"}]
        d = plan_placement("transform_on_extract", ops)
        assert d.target_ops == []
        assert len(d.extract_ops) == 3

    def test_elt_mask_still_forced_extract(self):
        ops = [{"kind": "join"}, {"kind": "mask", "column": "email"}]
        d = plan_placement("transfer_then_transform", ops)
        assert {o.kind for o in d.extract_ops} == {"mask"}
        assert {o.kind for o in d.target_ops} == {"join"}
        assert d.forced_pre_boundary[0].kind == "mask"

    def test_invalid_placement_falls_back_to_hybrid(self):
        d = plan_placement("bogus", [{"kind": "filter"}])
        assert d.effective_placement == "hybrid"
        assert d.requested_placement == "bogus"
        assert any("bogus" in w for w in d.warnings)

    def test_empty_ops(self):
        d = plan_placement("hybrid", [])
        assert d.extract_ops == [] and d.target_ops == []

    def test_realized_placement_is_transform_on_extract(self):
        d = plan_placement("hybrid", [{"kind": "join"}])
        assert realized_placement(d) == "transform_on_extract"


class TestEltSplit:
    def test_landing_name(self):
        assert landing_name("public", "orders") == "public_orders"
        assert landing_name("HR", "Emp-Master") == "hr_emp_master"

    def test_rewrite_base_relations(self):
        body = 'SELECT o.id FROM "public"."orders" o JOIN "public"."customers" c ON o.c=c.id'
        out, lm = rewrite_base_relations(body, ["public.orders", "public.customers"])
        assert '"wb_landing"."public_orders"' in out
        assert '"wb_landing"."public_customers"' in out
        assert '"public"."orders"' not in out
        assert {x["landing_table"] for x in lm} == {"public_orders", "public_customers"}

    def test_rewrite_custom_landing_schema(self):
        out, _ = rewrite_base_relations('FROM "s"."t"', ["s.t"], landing_schema="zone")
        assert '"zone"."s_t"' in out

    def test_rewrite_ignores_unlisted_relations(self):
        body = 'FROM "public"."orders", "other"."lookup"'
        out, _ = rewrite_base_relations(body, ["public.orders"])
        assert '"wb_landing"."public_orders"' in out
        assert '"other"."lookup"' in out  # not in tables → untouched

    def test_extract_projection_collects_qualified_columns(self):
        body = ('SELECT o.id, c.name FROM "public"."orders" o '
                'JOIN "public"."customers" c ON o.cust = c.id WHERE o.status = 1')
        proj = plan_extract_projections(body, ["public.orders", "public.customers"])
        assert proj["public.orders"] == ["cust", "id", "status"]
        assert proj["public.customers"] == ["id", "name"]

    def test_extract_projection_table_name_qualifier(self):
        body = 'SELECT orders.id FROM "public"."orders"'
        proj = plan_extract_projections(body, ["public.orders"])
        assert proj["public.orders"] == ["id"]

    def test_extract_projection_parse_failure_returns_empty(self):
        assert plan_extract_projections("NOT SQL @@@", ["public.orders"]) == {}

    def test_extract_projection_ignores_unlisted_tables(self):
        body = 'SELECT o.id, x.y FROM "public"."orders" o JOIN "other"."z" x ON 1=1'
        proj = plan_extract_projections(body, ["public.orders"])
        assert set(proj) == {"public.orders"}

    def test_build_elt_plan_attaches_unioned_projection(self):
        models = [
            {"physical_name": "a", "model_name": "a",
             "select_body": 'SELECT o.id FROM "public"."orders" o',
             "summary": {"tables": ["public.orders"]}},
            {"physical_name": "b", "model_name": "b",
             "select_body": 'SELECT o.amt FROM "public"."orders" o',
             "summary": {"tables": ["public.orders"]}},
        ]
        plan = build_elt_plan(models)
        orders = next(l for l in plan["landing_relations"] if l["source_table"] == "orders")
        assert orders["projection"] == ["amt", "id"]  # unioned across datasets

    def test_pushdown_anchor_only(self):
        body = _base('SELECT o.id, c.name FROM "public"."orders" o '
                     'JOIN "public"."customers" c ON o.cust=c.id '
                     'WHERE o.status = 1 AND c.region = 5')
        pf = plan_pushdown_filters(body, ["public.orders", "public.customers"])
        assert pf == {"public.orders": ["status = 1"]}  # c.region NOT pushed

    def test_pushdown_skips_nullable_side_of_left_join(self):
        body = _base('SELECT o.id FROM "public"."orders" o '
                     'LEFT JOIN "public"."customers" c ON o.cust=c.id WHERE c.region = 5')
        assert plan_pushdown_filters(body, ["public.orders", "public.customers"]) == {}

    def test_pushdown_skips_right_full_join(self):
        body = _base('SELECT o.id FROM "public"."orders" o '
                     'RIGHT JOIN "public"."customers" c ON o.cust=c.id WHERE o.status=1')
        assert plan_pushdown_filters(body, ["public.orders", "public.customers"]) == {}

    def test_pushdown_multiple_anchor_conjuncts(self):
        body = _base('SELECT o.id FROM "public"."orders" o WHERE o.status=1 AND o.amt>0')
        pf = plan_pushdown_filters(body, ["public.orders"])
        assert set(pf["public.orders"]) == {"status = 1", "amt > 0"}

    def test_build_elt_plan_filter_intersection(self):
        # orders anchored+filtered in ds A, but UNFILTERED anchor in ds B →
        # intersection empty → no push (ds B needs the dropped rows).
        models = [
            {"physical_name": "a", "model_name": "a",
             "select_body": _base('SELECT o.id FROM "public"."orders" o WHERE o.status=1'),
             "summary": {"tables": ["public.orders"]}},
            {"physical_name": "b", "model_name": "b",
             "select_body": _base('SELECT o.id FROM "public"."orders" o'),
             "summary": {"tables": ["public.orders"]}},
        ]
        orders = next(l for l in build_elt_plan(models)["landing_relations"]
                      if l["source_table"] == "orders")
        assert orders["filter"] is None

    def test_build_elt_plan_filter_pushed_when_all_agree(self):
        models = [
            {"physical_name": "a", "model_name": "a",
             "select_body": _base('SELECT o.id FROM "public"."orders" o WHERE o.status=1'),
             "summary": {"tables": ["public.orders"]}},
            {"physical_name": "b", "model_name": "b",
             "select_body": _base('SELECT o.amt FROM "public"."orders" o WHERE o.status=1'),
             "summary": {"tables": ["public.orders"]}},
        ]
        orders = next(l for l in build_elt_plan(models)["landing_relations"]
                      if l["source_table"] == "orders")
        assert orders["filter"] == "status = 1"

    def test_build_elt_plan_dedupes_landing(self):
        models = [
            {"physical_name": "a", "model_name": "a", "select_body": 'FROM "public"."orders"',
             "summary": {"tables": ["public.orders"]}},
            {"physical_name": "b", "model_name": "b", "select_body": 'FROM "public"."orders" JOIN "public"."items"',
             "summary": {"tables": ["public.orders", "public.items"]}},
        ]
        plan = build_elt_plan(models)
        landing = {(l["source_schema"], l["source_table"]) for l in plan["landing_relations"]}
        assert landing == {("public", "orders"), ("public", "items")}  # orders once
        assert len(plan["datasets"]) == 2
        assert all('"wb_landing"."public_orders"' in d["target_body"] for d in plan["datasets"])
