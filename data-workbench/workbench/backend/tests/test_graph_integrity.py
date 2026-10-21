"""Unit tests for the graph-integrity checker (``graph_integrity.py``).

Mirrors the ``test_join_preflight.py`` pattern: the pure core
``run_integrity_checks(session, ...)`` takes a Neo4j session as its first
argument, so a hand-rolled ``_FakeSession`` feeds canned per-check rows with no
live Neo4j and no monkeypatching. Each check query carries a distinctive
``// CHECK:<key>`` marker comment; the fake dispatches on it.
"""

import re

from workbench.backend import graph_integrity as gi


class _Result(list):
    def single(self):
        return self[0] if self else None


class _FakeSession:
    """Dispatch session.run(query, **params) to canned ``{count, sample}`` rows
    by parsing the ``// CHECK:<key>`` marker each query carries.

    ``results`` maps a check key to a ``{"count": n, "sample": [...]}`` dict;
    any key not present defaults to a clean ``{count: 0, sample: []}``.
    ``raise_for`` names a check whose run() raises (to exercise the
    failure-isolation path). Captured ``last_params`` supports scoping asserts.
    """

    def __init__(self, results=None, raise_for=None):
        self._results = results or {}
        self._raise_for = raise_for
        self.last_params = None
        self.seen_keys = []

    def run(self, query, **params):
        self.last_params = params
        m = re.search(r"CHECK:(\w+)", query)
        key = m.group(1) if m else "__unknown__"
        self.seen_keys.append(key)
        if key == self._raise_for:
            raise RuntimeError("boom")
        row = self._results.get(key, {"count": 0, "sample": []})
        return _Result([row])


# ── The pure core ─────────────────────────────────────────────────────────

def test_clean_graph_reports_every_check_at_zero():
    sess = _FakeSession()
    checks = gi.run_integrity_checks(sess)
    # One entry per catalog check, all clean.
    assert len(checks) == len(gi._ALL_CHECKS)
    assert all(c["count"] == 0 for c in checks)
    # Every entry carries the structured scorecard shape.
    for c in checks:
        assert set(c) >= {"check", "category", "severity", "count", "description", "sample"}


def test_catalog_covers_the_known_invariants():
    keys = {c[0] for c in gi._ALL_CHECKS}
    expected = {
        # structural orphans
        "catalog_without_project", "dataset_without_catalog", "column_without_dataset",
        "datacontract_without_project", "dprod_product_without_contract",
        "dprod_dataset_without_port", "dprod_column_without_dataset",
        "propertyshape_without_parent", "propertyshape_without_anchor",
        "dprod_nodeshape_without_dataset", "nodeshape_without_dataset",
        "mapping_without_source", "mapping_without_target",
        # duplicates / null uris / versioning
        "duplicate_uri", "null_uri_core_label", "contract_missing_versioning",
        # leakage + cross-project edge validity
        "cross_project_leak", "consumes_target_invalid", "uses_dataset_target_invalid",
        # enum hygiene
        "bad_product_kind", "bad_rule_source", "bad_mapping_status", "bad_transform_kind",
        # isCurrent uniqueness
        "multiple_current_description", "multiple_current_mapping",
        # retired-term probes
        "retired_on_product_column", "retired_datacontract_column",
        "retired_rule_source_external",
    }
    missing = expected - keys
    assert not missing, f"missing checks: {sorted(missing)}"


def test_duplicate_dprod_nodeshape_is_flagged():
    # The live bug: shape node triplicated (3 copies, same URI) with 0 HAS_SHAPE.
    sess = _FakeSession(results={
        "duplicate_uri": {"count": 1, "sample": [
            {"uri": "shape:dprod:ds:dpe-1-contract:active_employees",
             "labels": ["DProdNodeShape"], "copies": 3},
        ]},
        "dprod_nodeshape_without_dataset": {"count": 3, "sample": [
            "shape:dprod:ds:dpe-1-contract:active_employees",
        ]},
    })
    by_key = {c["check"]: c for c in gi.run_integrity_checks(sess)}
    assert by_key["duplicate_uri"]["count"] == 1
    assert by_key["duplicate_uri"]["severity"] == "error"
    assert by_key["dprod_nodeshape_without_dataset"]["count"] == 3
    assert by_key["dprod_nodeshape_without_dataset"]["severity"] == "error"


def test_unanchored_property_shape_is_flagged():
    sess = _FakeSession(results={
        "propertyshape_without_anchor": {"count": 1, "sample": [
            {"uri": "rule:dpe-1:dprod:active_employees:work_location_type:allowedValues:0",
             "ruleSource": "domain", "status": "pending_review"},
        ]},
    })
    by_key = {c["check"]: c for c in gi.run_integrity_checks(sess)}
    hit = by_key["propertyshape_without_anchor"]
    assert hit["count"] == 1 and hit["severity"] == "error"
    assert hit["sample"][0]["ruleSource"] == "domain"


def test_cross_project_leak_is_flagged():
    sess = _FakeSession(results={
        "cross_project_leak": {"count": 1, "sample": [
            {"rel": "REFERENCES", "a_codes": ["dpe-1"], "b_codes": ["dpe-2"],
             "a": "dataset:dpe-1:hr.emp", "b": "dataset:dpe-2:hr.dept"},
        ]},
    })
    by_key = {c["check"]: c for c in gi.run_integrity_checks(sess)}
    assert by_key["cross_project_leak"]["count"] == 1
    assert by_key["cross_project_leak"]["severity"] == "error"


def test_query_failure_is_isolated_not_fatal():
    sess = _FakeSession(raise_for="cross_project_leak")
    checks = gi.run_integrity_checks(sess)
    by_key = {c["check"]: c for c in checks}
    # The broken check is captured as an error with count -1; others still ran.
    assert by_key["cross_project_leak"]["count"] == -1
    assert by_key["cross_project_leak"]["severity"] == "error"
    assert "error" in by_key["cross_project_leak"]
    assert by_key["catalog_without_project"]["count"] == 0


# ── Scoping params ────────────────────────────────────────────────────────

def test_global_scan_passes_unscoped_params():
    sess = _FakeSession()
    gi.run_integrity_checks(sess)
    assert sess.last_params["scoped"] is False
    assert sess.last_params["marker"] == ""
    assert sess.last_params["pc"] == ""


def test_scoped_scan_passes_marker_and_code():
    sess = _FakeSession()
    gi.run_integrity_checks(sess, project_code="dpe-08132026-01")
    assert sess.last_params["scoped"] is True
    assert sess.last_params["marker"] == ":dpe-08132026-01"
    assert sess.last_params["pc"] == "dpe-08132026-01"


def test_enum_vocabularies_are_passed_as_params():
    sess = _FakeSession()
    gi.run_integrity_checks(sess)
    p = sess.last_params
    assert p["sanctioned"] == ["CONSUMES", "USES_DATASET"]
    assert "literal" in p["kinds"] and "date_difference" in p["kinds"]
    assert p["product_kinds"] == ["source", "consumer"]
    assert set(p["rule_sources"]) == {"observation", "domain", "user", "spec"}
    assert "superseded" in p["mapping_status"]


# ── Scorecard shaping ─────────────────────────────────────────────────────

def test_shape_scorecard_orders_and_summarises():
    checks = [
        {"check": "a", "category": "enum", "severity": "warn", "count": 2,
         "description": "", "sample": []},
        {"check": "b", "category": "structural_orphan", "severity": "error", "count": 5,
         "description": "", "sample": []},
        {"check": "c", "category": "leakage", "severity": "error", "count": 0,
         "description": "", "sample": []},
        {"check": "d", "category": "duplicate", "severity": "error", "count": -1,
         "description": "", "sample": [], "error": "boom"},
    ]
    sc = gi._shape_scorecard(checks, project_code=None)
    # Errors sort first, then by count desc.
    assert sc["checks"][0]["check"] == "b"
    # Summary: one error-with-findings (b), one warn-with-findings (a);
    # clean error (c) not counted; failed query (d) → query_errors.
    assert sc["summary"]["error"] == 1
    assert sc["summary"]["warn"] == 1
    assert sc["summary"]["query_errors"] == 1
    assert sc["summary"]["total_findings"] == 7  # 5 + 2 (negative count skipped)
    # ok is False because there is an error-severity finding AND a query error.
    assert sc["ok"] is False
    assert sc["scope"] == {"project_code": None, "global": True}


def test_shape_scorecard_ok_when_clean():
    checks = [
        {"check": "a", "category": "enum", "severity": "warn", "count": 0,
         "description": "", "sample": []},
        {"check": "b", "category": "leakage", "severity": "error", "count": 0,
         "description": "", "sample": []},
    ]
    sc = gi._shape_scorecard(checks, project_code="dpe-1")
    assert sc["ok"] is True
    assert sc["summary"]["error"] == 0 and sc["summary"]["query_errors"] == 0
    assert sc["scope"] == {"project_code": "dpe-1", "global": False}
