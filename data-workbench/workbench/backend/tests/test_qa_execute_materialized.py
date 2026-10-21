"""qa_execute must resolve dbt-materialized serving definitions.

Regression guard for the "No serving definition for this product" false 409 on a
fully-built materialized product: ``gather_inputs`` previously queried only
``virtual_view`` + ``transfer_then_transform`` and had no ``dbt_materialized``
branch, so a materialized-only product (the growing default for aggregate
products) was invisible to the curated NL->SQL execute path even though its
tables were built and previewable.

These are pure structural/logic checks — the harness has no Neo4j, so the graph
round-trip inside ``gather_inputs`` isn't exercised here (the end-to-end proof is
the manual docker repro). They pin the load-bearing invariants: the three-mode
dispatch and the safe-name convention that maps a materialized table back to its
:DProdOutputDataset.
"""

from __future__ import annotations

from workbench.backend import qa_execute as qe


def test_dispatch_tries_all_three_serving_modes():
    # gather_inputs falls through virtual -> materialized -> transfer; each mode
    # has its own servingMode-pinned query.
    assert "servingMode: 'virtual_view'" in qe._GRAPH_VIEWS_QUERY
    assert "servingMode: 'dbt_materialized'" in qe._GRAPH_MATERIALIZED_QUERY
    assert "servingMode: 'transfer_then_transform'" in qe._GRAPH_TRANSFER_QUERY


def test_materialized_query_returns_the_fields_the_branch_reads():
    q = qe._GRAPH_MATERIALIZED_QUERY
    # Readiness is buildStatus='built' (there is no deploy step / deploymentStatus).
    assert "buildStatus" in q and "build_status" in q
    # Physical-table names + schema the branch reads to build the FROM.
    assert "modelsJson" in q and "models_json" in q
    assert "targetSchema" in q and "target_schema" in q
    # Dataset-shape context for the executor skill (parity with the view query).
    assert "grainProse" in q and "scdPolicyJson" in q


def test_safe_name_matches_view_ddl_convention():
    # Must match generate_view_ddl._safe_name / pg_resolver._safe_name so a
    # materialized table name (`_safe_name(physicalName)`) resolves back to its
    # :DProdOutputDataset for grain/description metadata.
    assert qe._safe_name("Compensation History") == "compensation_history"
    assert qe._safe_name("dp.Foo-Bar") == "dp_foo_bar"
    assert qe._safe_name("already_safe") == "already_safe"
    assert qe._safe_name("") == ""
