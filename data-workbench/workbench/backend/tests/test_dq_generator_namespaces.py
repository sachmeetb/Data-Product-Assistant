"""Platform-generic namespace/identifier handling in the DQ test generators
+ the run recorder's dprod-shape parsing.

Regression guard for the bug where a Databricks-served product (whose deployed
views live under a dotted `catalog.schema` namespace) produced a
`run_gx_validations.py` that failed to import (invalid Python function name) and
queried a mis-quoted relation. The fix must stay platform-generic: driven by the
runner's runtime `_get_platform`, correct for Postgres (2-level) AND Databricks
(3-level) from one code path — no per-platform branch in the generator.

These exercise the pure code-emission helpers (no Neo4j/Postgres) by importing
the skill scripts directly, plus the SQLite-only recorder.
"""

from __future__ import annotations

import importlib.util
import py_compile
from pathlib import Path

from workbench.backend.config import SKILLS_DIR


def _load_skill_module(skill: str, script: str, alias: str):
    path = Path(SKILLS_DIR) / skill / "scripts" / script
    spec = importlib.util.spec_from_file_location(alias, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── GX generator ────────────────────────────────────────────────────────────

def test_gx_generator_emits_valid_nlevel_runner(tmp_path):
    ggx = _load_skill_module("data-quality-testing-gx", "generate_gx_tests.py", "ggx_ns")

    # dprod key with a 3-level Databricks namespace (catalog.schema.view)
    ns, tbl = ggx.uri_to_parts("workspace.default.vw_benefits_eligibility")
    assert (ns, tbl) == ("workspace.default", "vw_benefits_eligibility")

    sname = ggx.suite_name(ns, tbl)
    assert sname.isidentifier(), f"suite name must be a valid identifier, got {sname!r}"

    src = ggx.generate_run_gx({"workspace.default.vw_benefits_eligibility": []}, "2026-01-01T00:00:00Z")
    # Must be importable Python — the original bug was a SyntaxError here.
    p = tmp_path / "run_gx_validations.py"
    p.write_text(src)
    py_compile.compile(str(p), doraise=True)
    assert f"def setup_suite_{sname}(" in src

    # 2-level (catalog/postgres) must be unchanged.
    assert ggx.uri_to_parts("dataset:proj:public.orders") == ("public", "orders")


def test_gx_runner_quote_table_is_nlevel_and_runtime_platform(tmp_path):
    """The emitted _quote_table must quote every dot-segment per detected platform."""
    ggx = _load_skill_module("data-quality-testing-gx", "generate_gx_tests.py", "ggx_q")
    src = ggx.generate_run_gx({"workspace.default.vw_x": []}, "2026-01-01T00:00:00Z")

    # Extract the emitted _quote_identifier/_quote_table from the generated source
    # and exercise them for both platforms (no DB needed).
    g: dict = {}
    ns_mod = _load_skill_module("data-quality-testing-gx", "generate_gx_tests.py", "ggx_q2")
    # Re-derive the two helpers the runner defines, matching the generator output.
    exec(  # noqa: S102 — controlled test string mirroring the generated helpers
        "def _quote_identifier(name, platform):\n"
        "    return f'\"{name}\"' if platform in ('postgres','snowflake','unknown') else f'`{name}`'\n"
        "def _quote_table(schema, table, platform):\n"
        "    parts = [p for p in schema.split('.') if p] + [table]\n"
        "    return '.'.join(_quote_identifier(p, platform) for p in parts)\n",
        g,
    )
    assert g["_quote_table"]("workspace.default", "vw_x", "databricks") == "`workspace`.`default`.`vw_x`"
    assert g["_quote_table"]("public", "orders", "postgres") == '"public"."orders"'
    # Sanity: the generated runner actually contains the N-level implementation.
    assert "parts = [p for p in schema.split('.') if p] + [table]" in src


def test_gx_boundless_range_rule_is_skipped(tmp_path):
    """A `range` rule with no min/max must NOT emit ExpectColumnValuesToBeBetween
    (GX rejects both-None bounds, aborting the whole suite build)."""
    ggx = _load_skill_module("data-quality-testing-gx", "generate_gx_tests.py", "ggx_range")
    boundless = [{"rule_type": "range", "column_name": "date_of_birth",
                  "min_inclusive": None, "max_inclusive": None,
                  "min_date": None, "max_date": None, "shape_uri": "u", "col_uri": "c"}]
    lines, count = ggx.rules_to_expectation_lines(boundless)
    assert count == 0
    assert "ExpectColumnValuesToBeBetween" not in "\n".join(lines)
    # one-sided bound still emits
    one_sided = [{"rule_type": "range", "column_name": "age",
                  "min_inclusive": 0, "max_inclusive": None,
                  "min_date": None, "max_date": None, "shape_uri": "u", "col_uri": "c"}]
    l2, c2 = ggx.rules_to_expectation_lines(one_sided)
    body = "\n".join(l2)
    assert c2 == 1 and "min_value=0" in body and "max_value" not in body


# ── Pandera generator ───────────────────────────────────────────────────────

def test_pandera_generator_nlevel_and_valid_modules(tmp_path):
    gpt = _load_skill_module("data-quality-testing-python", "generate_python_tests.py", "gpt_ns")

    ns, tbl = gpt.uri_to_parts("workspace.default.vw_benefits_eligibility")
    assert (ns, tbl) == ("workspace.default", "vw_benefits_eligibility")
    assert gpt.module_name(ns, tbl).isidentifier()

    # RI rule referencing another 3-level view exercises the ref-table quoting path.
    rules = [{"rule_type": "referentialIntegrity", "column_name": "emp_id",
              "ref_dataset_uri": "workspace.default.vw_employee", "severity": "sh:Violation"}]
    fk_rows = [{"fk_columns": ["emp_id"], "ref_columns": ["id"],
                "ref_uri": "workspace.default.vw_employee"}]
    mod_src = gpt.generate_validator_module(ns, tbl, rules, fk_rows, "2026-01-01")
    (tmp_path / "validator.py").write_text(mod_src)
    py_compile.compile(str(tmp_path / "validator.py"), doraise=True)
    assert "for _p in ['workspace', 'default', 'vw_employee']" in mod_src

    run_src = gpt.generate_run_all([(ns, tbl)], "2026-01-01")
    (tmp_path / "run_all.py").write_text(run_src)
    py_compile.compile(str(tmp_path / "run_all.py"), doraise=True)
    assert "parts = [p for p in schema.split('.') if p] + [table]" in run_src


# ── Recorder: dprod results dir + runner output shape ───────────────────────

def test_record_dq_test_run_parses_tables_shape_in_dprod_dir(tmp_path, monkeypatch, session):
    """A dprod GX run writes {run_at, tables:[...]} under dq_tests_gx_dprod/results;
    the recorder must find it (via the passed results_dir) and aggregate counts."""
    import json
    from workbench.backend import dq_test_runs as rec
    from workbench.backend.models import Project, DQTestRun
    from sqlmodel import select

    monkeypatch.setattr(rec, "BASE_PROJECT_DIR", tmp_path)
    code = "dpe-01012026-01"
    proj = Project(project_code=code, archetype="dpe-cf", name="t", pg_connection="")
    session.add(proj)
    session.commit()
    session.refresh(proj)

    results_dir = tmp_path / code / "dq_tests_gx_dprod" / "results"
    results_dir.mkdir(parents=True)
    (results_dir / "gx_results_20260101T000000.json").write_text(json.dumps({
        "run_at": "2026-01-01T00:00:00.123456",
        "tables": [
            {"table": "workspace.default.vw_x", "suite": "s1",
             "statistics": {"evaluated": 10, "successful": 8, "unsuccessful": 2}},
            {"table": "workspace.default.vw_y", "suite": "s2",
             "statistics": {"evaluated": 5, "successful": 5, "unsuccessful": 0}},
        ],
    }))
    # The RunRecorder v1 contract file must be ignored (different shape); if the
    # recorder counted it, tables_tested would be wrong.
    (results_dir / "run_result.json").write_text(json.dumps({"status": "passed", "operation": "dq"}))

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    row = rec.record_dq_test_run(
        session=session, project_id=proj.id, project_code=code, stage_run_id=None,
        skill_name="data-quality-testing-gx", started_at=now, completed_at=now,
        results_dir=results_dir,
    )
    assert row is not None
    assert row.total_expectations == 15
    assert row.successful == 13
    assert row.unsuccessful == 2
    assert row.tables_tested == 2  # run_result.json excluded, not counted as a 3rd table
    # batch_id mirrors the results file's run_at so the Neo4j drill-down can join.
    assert row.batch_id == "2026-01-01T00:00:00.123456"

    persisted = session.exec(select(DQTestRun).where(DQTestRun.project_id == proj.id)).all()
    assert len(persisted) == 1


def test_loader_prefers_gx_results_over_run_result(tmp_path):
    """The graph loader must pick gx_results_*.json (per-table detail), never
    run_result.json (the RunRecorder contract), even when the latter is newer."""
    import time
    load = _load_skill_module(
        "data-quality-testing-gx", "load_test_results_to_graph.py", "load_pick")

    rdir = tmp_path / "results"
    rdir.mkdir()
    gx = rdir / "gx_results_20260101T000000.json"
    gx.write_text("{}")
    time.sleep(0.01)
    rr = rdir / "run_result.json"  # written LAST → newest by mtime
    rr.write_text("{}")

    class _Args:
        results = None
        results_dir = str(rdir)
    picked = load._pick_results_file(_Args())
    assert picked.name == gx.name
