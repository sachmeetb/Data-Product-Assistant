"""Data-migration (dmig) — orchestrator lifecycle, package assembly, and the
runner's plan mode. No Neo4j / live DB needed: the plan lives in SQLite
(MigrationPlanRow) and the runner's `--mode plan` touches nothing external.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from workbench.backend import archetypes as arch
from workbench.backend import migration_orchestrator as orch
from workbench.backend import serving_package as sp
from workbench.backend.models import MigrationPlanRow, PlatformConnection, Project
from workbench.backend.platform.migration_plan import MigrationStatus


def _mk_project(session, code="dmig-test-01") -> Project:
    p = Project(project_code=code, archetype="dmig", name="Migration Test",
                pg_connection="postgresql://u:pw@localhost:5432/src")
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _mk_target(session, name="tgt") -> PlatformConnection:
    c = PlatformConnection(connection_name=name, platform_type="snowflake",
                           host="acct", database="DB", username="u", secret_ref="",
                           connection_roles_json='["target"]')
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


# ── archetype wiring ─────────────────────────────────────────────────────────

def test_dmig_archetype_enabled_and_template_valid():
    assert arch.ARCHETYPE_REGISTRY["dmig"]["implemented"] is True
    tmpl = arch.get_workflow_templates("dmig")
    assert [t["workflow_id"] for t in tmpl] == ["migration_raw"]
    steps = [s for t in tmpl for s in t["stages"]]
    assert arch.validate_workflow(steps) == []
    # every dmig_* stage is registered
    for sid in ("dmig_configure", "dmig_assess_plan", "dmig_generate_pipeline",
                "dmig_execute_transfer", "dmig_reconcile"):
        assert sid in arch.STAGE_REGISTRY
    assert "migration_raw" in {c["workflow_id"] for c in arch.get_workflow_catalog()}


# ── orchestrator lifecycle ───────────────────────────────────────────────────

def test_configure_creates_plan_and_row(session):
    project = _mk_project(session)
    target = _mk_target(session)
    row = orch.configure(session, project, target_connection_id=target.id,
                         write_disposition="replace", target_schema="migrated")
    assert isinstance(row, MigrationPlanRow)
    assert row.status == "draft"
    assert row.target_connection_id == target.id
    assert row.target_platform == "snowflake"
    plan = orch.load_plan(session, project.project_code)
    assert plan is not None
    assert plan.status == MigrationStatus.draft
    assert plan.target_asset_ref.schema_name == "migrated"


def test_snapshot_then_reconcile_advances_plan(session):
    project = _mk_project(session, code="dmig-test-02")
    target = _mk_target(session, name="tgt2")
    orch.configure(session, project, target_connection_id=target.id)
    # successful snapshot: draft → assessed → running → loaded
    plan = orch.record_snapshot(session, project, ok=True, by="eng@x")
    assert plan.status == MigrationStatus.initial_snapshot_loaded
    # reconciliation all-pass → reconciled
    plan = orch.record_reconciliation(session, project, [
        {"table": "employees", "kind": "row_count", "source_row_count": 3,
         "target_row_count": 3, "result": "pass"},
    ], by="eng@x")
    assert plan.status == MigrationStatus.reconciled
    assert plan.all_reconciliation_rules_pass()
    payload = orch.status_payload(session, project)
    assert payload["configured"] is True
    assert payload["status"] == "reconciled"


def test_snapshot_failure_marks_retryable(session):
    project = _mk_project(session, code="dmig-test-03")
    target = _mk_target(session, name="tgt3")
    orch.configure(session, project, target_connection_id=target.id)
    plan = orch.record_snapshot(session, project, ok=False, by="eng@x")
    assert plan.status == MigrationStatus.retryable_failed


def test_set_status_idempotent_rerun_never_raises():
    from workbench.backend.platform.transfer_batch import RelationalRelationRef
    from workbench.backend.platform.migration_plan import MigrationPlan
    ref = RelationalRelationRef(platform_instance_id="1", asset_id="s.*",
                                schema="s", relation="*")
    p = MigrationPlan(plan_id="x", source_asset_ref=ref, target_asset_ref=ref)
    for st in (MigrationStatus.assessed, MigrationStatus.initial_snapshot_running,
               MigrationStatus.initial_snapshot_loaded, MigrationStatus.reconciled):
        orch.set_status(p, st)
    assert p.status == MigrationStatus.reconciled
    # a non-forward re-run (idempotent stage rerun) must not raise
    orch.set_status(p, MigrationStatus.initial_snapshot_running)
    assert p.status == MigrationStatus.initial_snapshot_running


# ── package assembly + git collect ───────────────────────────────────────────

def test_assemble_migration_package_and_git_collect(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    monkeypatch.setattr(sp, "BASE_PROJECT_DIR", tmp)
    spec = {"source_platform": "postgres", "target_platform": "snowflake",
            "target_schema": "migrated", "write_disposition": "replace",
            "datasets": [{"source_schema": "public", "source_table": "employees",
                          "target_table": "employees", "write_disposition": "replace",
                          "primary_key": ["id"]}]}
    dest = sp.assemble_migration_package(project_code="dmig-test-04", spec=spec)
    names = {p.name for p in dest.iterdir()}
    assert {"run.py", "migration.json", "requirements.txt", "_wb_runresult.py",
            ".env.example", "README.md"} <= names
    reqs = (dest / "requirements.txt").read_text()
    assert "dlt[snowflake]" in reqs and "psycopg2" in reqs
    # git collect: text-only, at repo root for a migration project, no binaries
    proj = type("P", (), {"project_code": "dmig-test-04", "archetype": "dmig"})()
    files = sp.collect_for_git(proj, None)
    assert "run.py" in files and "migration.json" in files
    assert all(not k.endswith((".parquet", ".duckdb")) for k in files)


# ── runner plan mode (no dlt / no external deps) ─────────────────────────────

def test_runner_plan_mode_writes_run_result():
    """The packaged runner's `--mode plan` reads migration.json and writes a
    valid run_result.json without importing dlt or touching a database."""
    pkg = Path(tempfile.mkdtemp())
    # copy the runner + recorder as the package would
    runners = Path(__file__).resolve().parents[1] / "serving_runners"
    (pkg / "run.py").write_bytes((runners / "run_migration.py").read_bytes())
    (pkg / "_wb_runresult.py").write_bytes((runners / "_wb_runresult.py").read_bytes())
    spec = {"source_platform": "postgres", "target_platform": "postgres",
            "target_schema": "migrated", "write_disposition": "replace",
            "datasets": [{"source_schema": "public", "source_table": "employees",
                          "target_table": "employees"}]}
    (pkg / "migration.json").write_text(json.dumps(spec), encoding="utf-8")
    proc = subprocess.run([sys.executable, "run.py", "--mode", "plan"],
                          cwd=pkg, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    result = json.loads((pkg / "run_result.json").read_text())
    assert result["status"] == "success"
    assert result["metrics"]["dataset_count"] == 1
    assert result["metrics"]["plan"][0]["target"] == "migrated.employees"


def test_runner_dlt_scope_is_per_run_isolated(monkeypatch):
    """Two concurrent migrations must NOT share a dlt working dir / pipeline identity
    (dlt's default state dir is HOME-based + name-keyed, the concurrent-run collision).
    Each invocation gets a fresh, unique scope; a backend run_id is honoured."""
    import importlib.util
    import os
    runners = Path(__file__).resolve().parents[1] / "serving_runners"
    if str(runners) not in sys.path:
        sys.path.insert(0, str(runners))
    spec = importlib.util.spec_from_file_location(
        "run_migration_under_test", runners / "run_migration.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.delenv("WB_S3_RUN_ID", raising=False)
    rid1, dir1 = mod._dlt_scope()
    rid2, dir2 = mod._dlt_scope()
    assert rid1 != rid2 and dir1 != dir2                 # per-run unique
    assert dir1.endswith(rid1) and f"{os.sep}.dlt{os.sep}" in dir1
    monkeypatch.setenv("WB_S3_RUN_ID", "fixed-mig-xyz")
    rid3, dir3 = mod._dlt_scope()
    assert rid3 == "fixed-mig-xyz" and dir3.endswith("fixed-mig-xyz")


# ── Phase 2: Oracle + SQL Server onboarding ──────────────────────────────────

def test_phase2_manifests_registered_with_transfer_caps():
    from workbench.backend.platform.registry import get_registry
    r = get_registry()
    for pid in ("oracle", "sqlserver"):
        assert r.get_manifest(pid) is not None
        assert r.is_usable(pid, "transfer_source")
        assert r.is_usable(pid, "transfer_target")


def test_phase2_type_profiles_and_gotchas():
    from workbench.backend.platform.type_system import get_profile, list_profiles, CanonicalType
    assert {"oracle", "sqlserver"} <= set(list_profiles())
    orc = get_profile("oracle")
    # Oracle DATE carries time → timestamp (not date), flagged ambiguous
    d = orc.map_to_canonical("DATE")
    assert d.canonical == CanonicalType.timestamp_us
    assert any(w.kind.value == "ambiguous" for w in d.warnings)
    n = orc.map_to_canonical("NUMBER(10,2)")
    assert n.canonical == CanonicalType.decimal and n.precision == 10 and n.scale == 2
    ss = get_profile("sqlserver")
    # SQL Server `timestamp` is rowversion (binary), NOT temporal
    ts = ss.map_to_canonical("timestamp")
    assert ts.canonical == CanonicalType.binary
    # datetime2 must win over a bare date/time prefix
    assert ss.map_to_canonical("datetime2(7)").canonical == CanonicalType.timestamp_us
    assert ss.map_to_canonical("datetimeoffset(7)").canonical == CanonicalType.timestamp_tz_us
    # tinyint is unsigned → widened to int16 (lossy)
    tt = ss.map_to_canonical("tinyint")
    assert tt.canonical == CanonicalType.int16
    assert any(w.kind.value == "lossy" for w in tt.warnings)
    # get_profile stays fail-closed
    import pytest
    with pytest.raises(KeyError):
        get_profile("no-such-platform")


def test_phase2_package_requirements_for_new_platforms(monkeypatch):
    import tempfile as _tf
    from pathlib import Path as _P
    monkeypatch.setattr(sp, "BASE_PROJECT_DIR", _P(_tf.mkdtemp()))
    spec = {"source_platform": "oracle", "target_platform": "sqlserver",
            "target_schema": "m", "write_disposition": "replace",
            "datasets": [{"source_schema": "dbo", "source_table": "t", "target_table": "t"}]}
    dest = sp.assemble_migration_package(project_code="dmig-p2", spec=spec)
    reqs = (dest / "requirements.txt").read_text()
    assert "dlt[mssql]" in reqs and "oracledb" in reqs


def test_phase2_generator_reflect_mode(tmp_path):
    """The generator's --reflect enumerates tables + PKs from a live source via
    SQLAlchemy — no data-discovery skill needed (thin source path)."""
    import sqlite3
    db = tmp_path / "src.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT)")
    con.execute("CREATE TABLE dept (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    script = (Path(__file__).resolve().parents[3] / "workbench-skills" / "skills"
              / "migration-pipeline-generator-dlt" / "scripts" / "generate_migration_config.py")
    out = tmp_path / "migration.json"
    proc = subprocess.run(
        [sys.executable, str(script), "--reflect", "--source-url", f"sqlite:///{db}",
         "--target-platform", "sqlserver", "--target-schema", "migrated", "--output", str(out)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    spec = json.loads(out.read_text())
    tables = {d["source_table"]: d["primary_key"] for d in spec["datasets"]}
    assert tables == {"employees": ["id"], "dept": ["id"]}
    assert spec["target_platform"] == "sqlserver"
