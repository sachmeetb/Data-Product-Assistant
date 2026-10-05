"""Tests for the transform-placement advisor core (deterministic, no graph/skill)."""
from __future__ import annotations

import asyncio

from workbench.backend.routers.transform_placement_advisor import (
    _advise_core,
    _heuristic_placement,
    _recommend_placement,
)


def _advise(ops, **kw):
    return asyncio.run(_advise_core(ops, skip_skill=True, **kw))


class TestRecommend:
    def test_no_defer_ops_recommends_extract(self):
        ops = [{"kind": "filter", "dataset": "o"}, {"kind": "direct", "column": "id", "dataset": "o"}]
        assert _recommend_placement(ops) == "transform_on_extract"

    def test_heavy_ops_recommend_hybrid(self):
        ops = [{"kind": "filter", "dataset": "o"}, {"kind": "aggregate", "dataset": "o"}]
        assert _recommend_placement(ops) == "hybrid"

    def test_empty_ops_recommend_extract(self):
        assert _recommend_placement([]) == "transform_on_extract"


class TestAdviseCore:
    def test_hybrid_split_and_governance(self):
        ops = [
            {"kind": "mask", "column": "ssn", "dataset": "customer"},
            {"kind": "filter", "dataset": "orders"},
            {"kind": "aggregate", "dataset": "orders"},
            {"kind": "join", "dataset": "orders"},
        ]
        r = _advise(ops)
        assert r["recommended_placement"] == "hybrid"
        assert r["chosen_placement"] == "hybrid"
        extract_kinds = {o["kind"] for o in r["extract_ops"]}
        target_kinds = {o["kind"] for o in r["target_ops"]}
        assert extract_kinds == {"mask", "filter"}
        assert target_kinds == {"aggregate", "join"}
        assert [o["kind"] for o in r["forced_pre_boundary"]] == ["mask"]
        codes = {d["code"] for d in r["drivers"]}
        assert "governance_masking" in codes
        assert r["advisor_error"] is None

    def test_governance_mask_forced_even_under_elt(self):
        ops = [{"kind": "mask", "column": "email", "dataset": "c"}, {"kind": "join", "dataset": "c"}]
        r = _advise(ops, requested_placement="transfer_then_transform")
        assert {o["kind"] for o in r["extract_ops"]} == {"mask"}
        assert {o["kind"] for o in r["target_ops"]} == {"join"}
        assert r["chosen_placement"] == "transfer_then_transform"

    def test_requested_placement_recomputes_split(self):
        ops = [{"kind": "filter", "dataset": "o"}, {"kind": "aggregate", "dataset": "o"}]
        # ETL forces everything to extract regardless of the recommendation
        r = _advise(ops, requested_placement="transform_on_extract")
        assert r["chosen_placement"] == "transform_on_extract"
        assert r["target_ops"] == []
        assert {o["kind"] for o in r["extract_ops"]} == {"filter", "aggregate"}

    def test_placements_options_present(self):
        r = _advise([{"kind": "filter", "dataset": "o"}])
        ids = {p["id"] for p in r["placements"]}
        assert ids == {"transform_on_extract", "hybrid", "transfer_then_transform"}

    def test_platforms_normalized(self):
        r = _advise([{"kind": "join", "dataset": "o"}], source_platform="postgresql", target_platform="databricks")
        assert r["source_platform"] == "postgres"
        assert r["target_platform"] == "databricks"


class TestHeuristicShape:
    def test_per_op_assignments_union_extract_target(self):
        ops = [{"kind": "filter", "dataset": "o"}, {"kind": "join", "dataset": "o"}]
        d = _heuristic_placement(ops, "hybrid")
        assert len(d["per_op_assignments"]) == len(d["extract_ops"]) + len(d["target_ops"])
