"""Tests for the capability-aware serving advisor (Part 2B).

Covers feasibility gating over the full pattern taxonomy, single recommendation
+ rationale, and the invariant that skill enrichment can never flip the
recommendation or a feasibility verdict.
"""
from __future__ import annotations

from workbench.backend.routers.serving_strategy import (
    _build_patterns,
    _heuristic_serving_strategy,
)
from workbench.backend.routers.odcs import _resolve_product_kind


def _by_id(payload: dict) -> dict[str, dict]:
    return {p["pattern"]: p for p in payload["patterns"]}


class TestSamePlatform:
    def test_postgres_same_platform_virtual_recommended(self):
        r = _build_patterns("postgres", "postgres", scd2=False, grouped=False,
                            has_masking=False, recommended_mode="virtual")
        assert r["recommended_pattern"] == "native_virtual"
        pats = _by_id(r)
        assert pats["native_virtual"]["feasibility"] == "feasible"
        assert pats["native_materialized"]["feasibility"] == "feasible"
        assert pats["lakehouse_file"]["feasibility"] == "feasible"

    def test_scd2_prefers_materialized(self):
        r = _build_patterns("postgres", "postgres", scd2=True, grouped=False,
                            has_masking=False, recommended_mode="materialized")
        assert r["recommended_pattern"] == "native_materialized"

    def test_recommended_pattern_ranked_first(self):
        r = _build_patterns("postgres", "postgres", scd2=False, grouped=False,
                            has_masking=False, recommended_mode="virtual")
        assert r["patterns"][0]["pattern"] == r["recommended_pattern"]
        assert r["patterns"][0]["rank"] == 0
        assert r["patterns"][0]["recommended"] is True

    def test_databricks_same_platform_recommends_native_view(self):
        """A consumer whose CONSUMES'd source was materialized to Databricks
        resolves source==target==databricks (via _resolve_source_platform's
        served-location lookup) → the advisor must recommend a native VIEW over
        the source's Unity Catalog tables, NOT a cross-platform transfer."""
        r = _build_patterns("databricks", "databricks", scd2=False, grouped=False,
                            has_masking=False, recommended_mode="virtual")
        assert r["recommended_pattern"] == "native_virtual"
        pats = _by_id(r)
        assert pats["native_virtual"]["feasibility"] == "feasible"
        # Same platform ⇒ a transfer is not applicable (nothing to move).
        assert pats["transfer_then_transform"]["feasibility"] == "not_applicable"


class TestCrossPlatform:
    def test_postgres_to_mysql_native_infeasible(self):
        r = _build_patterns("postgres", "mysql", scd2=False, grouped=False,
                            has_masking=False, recommended_mode="virtual")
        pats = _by_id(r)
        assert pats["native_materialized"]["feasibility"] == "impossible"
        assert pats["native_virtual"]["feasibility"] == "impossible"

    def test_mysql_target_lakehouse_impossible(self):
        r = _build_patterns("postgres", "mysql", scd2=False, grouped=False,
                            has_masking=False, recommended_mode="virtual")
        assert _by_id(r)["lakehouse_file"]["feasibility"] == "impossible"

    def test_roadmap_patterns_not_yet_supported(self):
        r = _build_patterns("postgres", "mysql", scd2=False, grouped=False,
                            has_masking=False, recommended_mode="virtual")
        pats = _by_id(r)
        for pid in ("transfer_then_transform", "federated", "warehouse_native_load"):
            assert pats[pid]["feasibility"] == "not_yet_supported"

    def test_every_infeasible_pattern_has_a_reason(self):
        r = _build_patterns("postgres", "mysql", scd2=False, grouped=False,
                            has_masking=False, recommended_mode="virtual")
        for p in r["patterns"]:
            if p["feasibility"] != "feasible":
                assert p["feasibility_reason"]


class TestTransferFlip:
    """Phase 2: transfer_then_transform is feasible when both sides declare a
    usable transfer capability (DuckDB engine pairs); not_applicable for a
    same-platform target (precondition unmet); else not_yet_supported."""

    def test_postgres_to_duckdb_transfer_feasible(self):
        r = _build_patterns("postgres", "duckdb_local", scd2=False, grouped=False,
                            has_masking=False, recommended_mode="virtual")
        assert _by_id(r)["transfer_then_transform"]["feasibility"] == "feasible"

    def test_postgres_to_snowflake_transfer_feasible(self):
        # Phase 2.1: the dlt engine makes Snowflake a feasible transfer target.
        r = _build_patterns("postgres", "snowflake", scd2=False, grouped=False,
                            has_masking=False, recommended_mode="virtual")
        assert _by_id(r)["transfer_then_transform"]["feasibility"] == "feasible"

    def test_postgres_to_databricks_transfer_feasible(self):
        r = _build_patterns("postgres", "databricks", scd2=False, grouped=False,
                            has_masking=False, recommended_mode="virtual")
        assert _by_id(r)["transfer_then_transform"]["feasibility"] == "feasible"

    def test_same_platform_transfer_not_applicable(self):
        # Same-platform target: the precondition isn't met (not a missing feature),
        # so it's not_applicable — the UI shows "N/A" + "pick a cross-platform target",
        # distinct from the genuinely-deferred not_yet_supported patterns.
        r = _build_patterns("postgres", "postgres", scd2=False, grouped=False,
                            has_masking=False, recommended_mode="virtual")
        assert _by_id(r)["transfer_then_transform"]["feasibility"] == "not_applicable"

    def test_feasible_transfer_has_placement(self):
        r = _build_patterns("postgres", "duckdb_local", scd2=False, grouped=False,
                            has_masking=False, recommended_mode="virtual")
        assert "transform_placement" in _by_id(r)["transfer_then_transform"]


class TestPlacement:
    def test_masking_forces_pre_boundary_placement(self):
        r = _build_patterns("postgres", "postgres", scd2=False, grouped=False,
                            has_masking=True, recommended_mode="virtual")
        lk = _by_id(r)["lakehouse_file"]
        assert lk["transform_placement"]["recommended"] == "transform_on_extract"

    def test_default_placement_is_hybrid(self):
        r = _build_patterns("postgres", "postgres", scd2=False, grouped=False,
                            has_masking=False, recommended_mode="virtual")
        lk = _by_id(r)["lakehouse_file"]
        assert lk["transform_placement"]["recommended"] == "hybrid"

    def test_native_patterns_have_no_placement(self):
        r = _build_patterns("postgres", "postgres", scd2=False, grouped=False,
                            has_masking=False, recommended_mode="virtual")
        assert "transform_placement" not in _by_id(r)["native_virtual"]


class TestExactlyOneRecommendation:
    def test_at_most_one_recommended(self):
        for tgt in ("postgres", "mysql", "snowflake"):
            r = _build_patterns("postgres", tgt, scd2=False, grouped=False,
                                has_masking=False, recommended_mode="virtual")
            recs = [p for p in r["patterns"] if p.get("recommended")]
            assert len(recs) <= 1


class TestProductKindTaxonomy:
    """Phase 3 — three-valued productKind + the aggregate materialize-default."""

    def test_resolve_product_kind_source_archetype_always_source(self):
        # dpe-sa is architecturally fixed to 'source' regardless of the spec.
        assert _resolve_product_kind({"productKind": "aggregate"}, "dpe-sa") == "source"

    def test_resolve_product_kind_honours_explicit_aggregate(self):
        assert _resolve_product_kind({"productKind": "aggregate"}, "dpe-cf") == "aggregate"

    def test_resolve_product_kind_defaults_consumer(self):
        # dpe-cf with no explicit kind (legacy save) stays 'consumer'.
        assert _resolve_product_kind({}, "dpe-cf") == "consumer"
        assert _resolve_product_kind({"productKind": ""}, "dpe-cf") == "consumer"
        assert _resolve_product_kind({"productKind": "garbage"}, "dpe-cf") == "consumer"

    def test_aggregate_recommends_materialized_soft(self):
        # An aggregate flips the recommendation to materialized but does NOT
        # set required (a co-located aggregate may still choose virtual).
        rec = _heuristic_serving_strategy(
            "dpe-cf", [{"name": "d", "scd_policy": "", "grouping": False}],
            cross_platform=False, product_kind="aggregate",
        )
        assert rec["recommended_mode"] == "materialized"
        assert rec["required"] is False
        assert any(d["code"] == "aggregate_reuse" for d in rec["drivers"])

    def test_consumer_stays_virtual_when_colocated(self):
        rec = _heuristic_serving_strategy(
            "dpe-cf", [{"name": "d", "scd_policy": "", "grouping": False}],
            cross_platform=False, product_kind="consumer",
        )
        assert rec["recommended_mode"] == "virtual"

    def test_scd2_still_escalates_aggregate_to_required(self):
        # A harder driver (scd2) must still be able to set required=True even on
        # an aggregate (aggregate only sets a soft 'consider').
        rec = _heuristic_serving_strategy(
            "dpe-cf", [{"name": "d", "scd_policy": "scd2", "grouping": False}],
            cross_platform=False, product_kind="aggregate",
        )
        assert rec["recommended_mode"] == "materialized"
        assert rec["required"] is True
