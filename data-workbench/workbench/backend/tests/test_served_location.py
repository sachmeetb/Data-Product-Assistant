"""Consumer served-location resolution (pg_resolver).

A dpe-cf consumer that :CONSUMES a source product materialized to a DISTINCT
target (e.g. a MySQL-origin source loaded into Databricks) must READ that source
from its SERVED location — not the origin. These lock:
  * `_served_namespace` — the graph-first / summaryJson-fallback namespace picker.
  * `resolve_consumed_source_serving` — returns a SourceServedLocation when the
    consumed source has a deployed materialized/transfer serving AND a distinct
    target connection; returns None (→ origin-borrow) otherwise.

Pure-ish: the Neo4j session and the SQLite target-connection resolver are mocked,
mirroring the suite's graph-stub convention.
"""
from __future__ import annotations

import json
from unittest import mock

from workbench.backend import pg_resolver as pr


# ── _served_namespace ─────────────────────────────────────────────────────────

class TestServedNamespace:
    def test_prefers_first_class_catalog_and_schema(self):
        assert pr._served_namespace("main", "sales", "") == "main.sales"

    def test_falls_back_to_summary_json_target_namespace(self):
        sj = json.dumps({"target_namespace": "main.sales"})
        assert pr._served_namespace("", "", sj) == "main.sales"

    def test_first_class_wins_over_summary(self):
        sj = json.dumps({"target_namespace": "stale.ns"})
        assert pr._served_namespace("main", "sales", sj) == "main.sales"

    def test_bare_schema_when_no_catalog(self):
        assert pr._served_namespace("", "public", "") == "public"

    def test_malformed_summary_json_is_safe(self):
        assert pr._served_namespace("", "public", "{not json") == "public"


# ── resolve_consumed_source_serving ───────────────────────────────────────────

def _project():
    p = mock.MagicMock()
    p.project_code = "cf-proj"
    p.neo4j_host = "h"; p.neo4j_port = 7687
    p.neo4j_user = "u"; p.neo4j_password = "pw"; p.neo4j_database = "neo4j"
    return p


def _neo4j_cm(rows, virtual_rows=None):
    """A context-manager Neo4j session that dispatches .run() by query: the
    MATERIALIZED-serving query returns `rows`; the VIRTUAL-view-serving query
    (a second pass the resolver now runs) returns `virtual_rows` (default none —
    these scenarios have no virtual-view upstreams)."""
    _virtual = list(virtual_rows or [])

    def _run(query, **params):
        if "virtual_view" in query:
            return _virtual
        return list(rows)

    ns = mock.MagicMock()
    ns.run.side_effect = _run
    cm = mock.MagicMock()
    cm.__enter__ = mock.Mock(return_value=ns)
    cm.__exit__ = mock.Mock(return_value=False)
    return cm


def _source_row(uri="dprod:dpe-sa-01-contract", *, catalog="main", schema="sales",
                summary_json="", datasets=("Order Header", "Customer"),
                mode="transfer_then_transform", status="deployed"):
    return {
        "source_dp_uri": uri, "serving_mode": mode, "target_platform": "databricks",
        "target_catalog": catalog, "target_schema": schema, "summary_json": summary_json,
        "deployment_status": status, "dataset_physicals": list(datasets),
    }


class TestResolveConsumedSourceServing:
    def test_databricks_transfer_served_location(self):
        with (
            mock.patch("workbench.backend.pg_resolver.neo4j_session",
                       return_value=_neo4j_cm([_source_row()])),
            mock.patch("workbench.backend.routers.connections.resolve_materialization_target_ref",
                       return_value=("databricks", {"host": "adb", "extra_config": {}})),
        ):
            loc = pr.resolve_consumed_source_serving(_project(), mock.MagicMock())
        assert loc is not None
        assert loc.served_platform == "databricks"
        assert loc.source_project_code == "dpe-sa-01"
        assert loc.namespace == "main.sales"
        # relations keyed by _safe_name → fully-qualified served relation.
        assert loc.relations["order_header"] == "main.sales.order_header"
        assert loc.relations["customer"] == "main.sales.customer"

    def test_multi_source_merges_all_served_relations(self):
        """A consumer over TWO materialized sources maps BOTH sources' relations.
        This is the fix: `LIMIT 1` mapped only the newest-built source, leaving the
        other's datasets to fall back to a non-existent `vw_<name>` at deploy."""
        rows = [  # newest-built first
            _source_row(uri="dprod:comp-master-contract", schema="hr_core",
                        datasets=("salary_history", "pay_employee")),
            _source_row(uri="dprod:emp-master-contract", schema="hr_core",
                        datasets=("employee", "department")),
        ]
        with (
            mock.patch("workbench.backend.pg_resolver.neo4j_session",
                       return_value=_neo4j_cm(rows)),
            mock.patch("workbench.backend.routers.connections.resolve_materialization_target_ref",
                       return_value=("databricks", {"host": "adb"})),
        ):
            loc = pr.resolve_consumed_source_serving(_project(), mock.MagicMock())
        assert loc is not None
        assert set(loc.relations) == {"salary_history", "pay_employee", "employee", "department"}
        assert loc.relations["salary_history"] == "main.hr_core.salary_history"
        assert loc.relations["employee"] == "main.hr_core.employee"  # the previously-missing one

    def test_different_platform_source_skipped(self):
        """A second consumed source served to a DIFFERENT platform than the primary
        is skipped — a single view can't federate across engines."""
        rows = [_source_row(uri="dprod:a-contract", datasets=("t1",)),
                _source_row(uri="dprod:b-contract", datasets=("t2",))]

        def _target(code, _session):
            return ("databricks", {"h": "a"}) if code == "a" else ("snowflake", {"h": "b"})

        with (
            mock.patch("workbench.backend.pg_resolver.neo4j_session",
                       return_value=_neo4j_cm(rows)),
            mock.patch("workbench.backend.routers.connections.resolve_materialization_target_ref",
                       side_effect=_target),
        ):
            loc = pr.resolve_consumed_source_serving(_project(), mock.MagicMock())
        assert loc.served_platform == "databricks"
        assert "t1" in loc.relations
        assert "t2" not in loc.relations  # different served platform → skipped

    def test_no_target_connection_returns_none(self):
        """Source served in-place (no distinct target connection) ⇒ None ⇒ the
        caller falls back to the origin-borrow (co-located case preserved)."""
        with (
            mock.patch("workbench.backend.pg_resolver.neo4j_session",
                       return_value=_neo4j_cm([_source_row()])),
            mock.patch("workbench.backend.routers.connections.resolve_materialization_target_ref",
                       return_value=None),
        ):
            loc = pr.resolve_consumed_source_serving(_project(), mock.MagicMock())
        assert loc is None

    def test_no_serving_row_returns_none(self):
        with mock.patch("workbench.backend.pg_resolver.neo4j_session",
                        return_value=_neo4j_cm([])):
            loc = pr.resolve_consumed_source_serving(_project(), mock.MagicMock())
        assert loc is None

    def test_virtual_view_upstream_served_from_deployed_view_schema(self):
        """A consumer over a DEPLOYED VIRTUAL-VIEW upstream (the multi-hop
        target) reads its `vw_<safe>` views from the upstream's own deployed
        viewSchema, on the upstream's resolved read connection."""
        vrow = {
            "source_dp_uri": "dprod:agg-01-contract",
            "view_schema": "agg_serving",
            "dataset_physicals": ["Order Summary"],
        }
        with (
            mock.patch("workbench.backend.pg_resolver.neo4j_session",
                       return_value=_neo4j_cm([], virtual_rows=[vrow])),
            mock.patch("workbench.backend.pg_resolver.select", return_value=mock.MagicMock()),
            mock.patch("workbench.backend.pg_resolver.resolve_source_connection_for_project",
                       return_value=("postgres", {"host": "pg-a", "database": "warehouse"}, "src-01")),
        ):
            session = mock.MagicMock()
            session.exec.return_value.first.return_value = _project()  # any non-None project
            loc = pr.resolve_consumed_source_serving(_project(), session)
        assert loc is not None
        assert loc.serving_mode == "virtual_view"
        assert loc.source_project_code == "agg-01"
        # relation keyed by _safe_name → "<deployed view_schema>.vw_<safe>".
        assert loc.relations["order_summary"] == "agg_serving.vw_order_summary"

    def test_namespace_from_summary_json_when_no_catalog_property(self):
        """Legacy source materialized before targetCatalog was persisted — the
        namespace is recovered from summaryJson.target_namespace."""
        row = _source_row(catalog="", summary_json=json.dumps({"target_namespace": "main.sales"}),
                          datasets=("Order Header",))
        with (
            mock.patch("workbench.backend.pg_resolver.neo4j_session",
                       return_value=_neo4j_cm([row])),
            mock.patch("workbench.backend.routers.connections.resolve_materialization_target_ref",
                       return_value=("databricks", {"host": "adb", "extra_config": {}})),
        ):
            loc = pr.resolve_consumed_source_serving(_project(), mock.MagicMock())
        assert loc is not None
        assert loc.namespace == "main.sales"
        assert loc.relations["order_header"] == "main.sales.order_header"


class TestReadConnectionChokePoint:
    def test_served_location_first(self):
        loc = pr.SourceServedLocation(
            source_project_code="dpe-sa-01", source_contract_id="dpe-sa-01-contract",
            served_platform="databricks", namespace="main.sales",
            connection_ref={"host": "adb"}, relations={}, deployment_status="deployed",
            serving_mode="transfer_then_transform",
        )
        with mock.patch("workbench.backend.pg_resolver.resolve_consumed_source_serving",
                        return_value=loc):
            platform, ref, borrowed = pr.resolve_read_connection_for_consumer(
                _project(), mock.MagicMock())
        assert platform == "databricks"
        assert ref == {"host": "adb"}
        assert borrowed == "dpe-sa-01"

    def test_falls_back_to_origin_borrow(self):
        with (
            mock.patch("workbench.backend.pg_resolver.resolve_consumed_source_serving",
                       return_value=None),
            mock.patch("workbench.backend.pg_resolver.resolve_source_connection_for_project",
                       return_value=("postgres", {"pg_connection": "dsn"}, None)) as origin,
        ):
            platform, ref, borrowed = pr.resolve_read_connection_for_consumer(
                _project(), mock.MagicMock())
        assert platform == "postgres"
        assert ref == {"pg_connection": "dsn"}
        origin.assert_called_once()
