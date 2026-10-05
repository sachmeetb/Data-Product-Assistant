"""Phase 8 — transform catalog conformance.

The load-bearing invariant: for every offered transform op, on every served
platform, the compiler either RENDERS SQL that re-parses under the target dialect
OR fails closed — and its choice AGREES with the capability artifact
(native/emulated ⇒ renders; unsupported ⇒ fails closed). This is what keeps the
Dialect emitter and the capability artifact from silently diverging.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import sqlglot

from workbench.backend.platform.transform_capabilities import load_capabilities

_REPO = Path(__file__).resolve().parents[3]
_GEN = _REPO / "workbench-skills" / "skills" / "data-serving-virtual-view" / "scripts" / "generate_view_ddl.py"

_SQLGLOT = {"postgres": "postgres", "databricks": "databricks", "snowflake": "snowflake",
            "bigquery": "bigquery", "mysql": "mysql"}
SERVED = list(_SQLGLOT)


def _load_gv():
    spec = importlib.util.spec_from_file_location("generate_view_ddl", _GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gv = _load_gv()
caps = load_capabilities()
ALIAS = {"hr.emp": "t1"}


def _mapping(kind, params, col="c"):
    return {"transform_kind": kind, "source_schema": "hr", "source_table": "emp",
            "source_col": col, "transform_params_json": json.dumps(params), "mapping_uri": "m:x"}


def _reparses(sql, platform) -> bool:
    try:
        sqlglot.parse_one(f"SELECT {sql} FROM t", read=_SQLGLOT[platform])
        return True
    except Exception:  # noqa: BLE001
        return False


# ── date_difference: compiler ⇔ artifact agreement, every semantics × platform ─

@pytest.mark.parametrize("platform", SERVED)
@pytest.mark.parametrize("semantics", ["completed_units", "boundary_count", "symbolic_interval"])
def test_date_difference_compiler_agrees_with_artifact(platform, semantics):
    support = caps.op_support("date_difference", semantics, platform)
    assert support is not None, f"artifact has no date_difference/{semantics} for {platform}"
    dialect = gv.get_dialect(platform)
    try:
        sql = dialect.date_difference("t1.dob", "CURRENT_DATE", semantics=semantics)
    except gv.ViewGenerationError:
        assert support == "unsupported", (
            f"{platform}/{semantics}: compiler failed closed but artifact says {support!r}"
        )
        return
    assert support in ("native", "emulated"), (
        f"{platform}/{semantics}: compiler emitted SQL but artifact says {support!r}"
    )
    assert _reparses(sql, platform), f"{platform}/{semantics}: rendered SQL does not re-parse: {sql}"


# ── split_part op: every served platform has a form that renders ──────────────

@pytest.mark.parametrize("platform", SERVED)
def test_split_part_renders_on_every_platform(platform):
    support = caps.op_support("split_part", "default", platform)
    assert support in ("native", "emulated"), f"{platform}: split_part {support!r}"
    m = _mapping("split", {"delimiter": "-", "index": 2})
    sql = gv._compile_structured_kind("split", m, [m], ALIAS, gv.get_dialect(platform))
    assert sql is not None
    assert _reparses(sql, platform), f"{platform}: split does not re-parse: {sql}"


# ── every offered structured kind renders + re-parses on every served platform ─

_STRUCTURED_SINGLE = {
    "cast": {"target_type": "text"},
    "substring": {"start": 1, "length": 3},
    "split": {"delimiter": "-", "index": 1},
    "date_difference": {"semantics": "completed_units"},  # supported on all 5
    "bucket": {"boundaries": [10, 20], "labels": ["low", "mid", "high"]},
    "mask": {"algorithm": "keep_last", "keep_n": 4},
    "hash": {"algorithm": "sha256"},
}


@pytest.mark.parametrize("platform", SERVED)
@pytest.mark.parametrize("kind,params", sorted(_STRUCTURED_SINGLE.items()))
def test_single_source_kind_renders_and_reparses(platform, kind, params):
    m = _mapping(kind, params, col="dob")
    dialect = gv.get_dialect(platform)
    sql = gv._compile_select_expr("out", [m], ALIAS, {}, {}, dialect=dialect)
    assert sql, f"{kind}@{platform}: empty render"
    assert _reparses(sql, platform), f"{kind}@{platform}: does not re-parse: {sql}"


@pytest.mark.parametrize("platform", SERVED)
def test_concat_renders_and_reparses(platform):
    first = _mapping("concat", {"separator": " "}, col="first")
    last = _mapping("concat", {}, col="last")
    dialect = gv.get_dialect(platform)
    sql = gv._compile_select_expr("full_name", [first, last], ALIAS, {}, {}, dialect=dialect)
    assert _reparses(sql, platform), f"concat@{platform}: does not re-parse: {sql}"


# ── composable bucket-over-date_difference through the full select path ───────

_BUCKET_DD = {
    "value": {"op": "date_difference", "unit": "year", "semantics": "completed_units"},
    "boundaries": [1, 3, 7],
    "labels": ["under_1y", "1_to_3y", "3_to_7y", "7y_plus"],
}


@pytest.mark.parametrize("platform", SERVED)
def test_bucket_over_date_difference_via_select_expr(platform):
    m = _mapping("bucket", _BUCKET_DD, col="hire_date")
    sql = gv._compile_select_expr("tenure_band", [m], ALIAS, {}, {}, dialect=gv.get_dialect(platform))
    assert sql.startswith("CASE") and "'7y_plus'" in sql
    assert _reparses(sql, platform), f"bucket-dd@{platform}: {sql}"


def test_bucket_symbolic_interval_flagged_by_preflight_off_postgres():
    # The preflight (diagnostics accumulator) must surface a bucket whose nested
    # value-op semantics is unsupported on the target — not only fail at generate.
    params = {**_BUCKET_DD, "value": {"op": "date_difference", "semantics": "symbolic_interval"}}
    m = _mapping("bucket", params, col="hire_date")
    for platform in ("databricks", "snowflake", "bigquery", "mysql"):
        diags = gv._empty_transform_diagnostics()
        gv._accumulate_transform_diagnostics(diags, {"tenure_band": [m]}, gv.get_dialect(platform))
        codes = {e["code"] for e in diags["errors"]}
        assert "unsupported_op" in codes, f"{platform}: {diags['errors']}"
    # completed_units is supported everywhere → no error.
    ok = _mapping("bucket", _BUCKET_DD, col="hire_date")
    diags = gv._empty_transform_diagnostics()
    gv._accumulate_transform_diagnostics(diags, {"tenure_band": [ok]}, gv.get_dialect("databricks"))
    assert not diags["errors"]


# ── coverage: the whole offered op catalog is present for every served platform ─

def test_every_offered_op_covers_every_served_platform():
    for op, node in caps.ops.items():
        for variant, platmap in node["semantics"].items():
            for p in SERVED:
                assert p in platmap, f"artifact op {op}/{variant} missing served platform {p}"
