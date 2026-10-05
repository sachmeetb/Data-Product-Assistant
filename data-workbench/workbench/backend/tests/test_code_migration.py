"""Code-migration (cmig) lifecycle + the forward-engineering readiness guard.

Neo4j-free (graph writes in the orchestrator are best-effort try/except). These
assert the SQLite lifecycle + the pure ``require_forward_ready`` dependency set,
which is the load-bearing correctness invariant: forward-engineering must be
refused until the reverse-engineered spec is approved AND every dependency is
current. BASE_PROJECT_DIR is redirected to a tmp dir so on-disk artifacts
(source/, codespec.json, migration.json) don't touch the real projects tree.
"""
from __future__ import annotations

import hashlib
import json

import pytest
from sqlmodel import Session

from workbench.backend import code_migration_orchestrator as cmo
from workbench.backend.database import engine
from workbench.backend.models import CodeMigrationPlanRow, Project


@pytest.fixture()
def tmp_projects(tmp_path, monkeypatch):
    monkeypatch.setattr(cmo, "BASE_PROJECT_DIR", tmp_path)
    return tmp_path


def _mk_project(code: str, archetype: str) -> Project:
    with Session(engine, expire_on_commit=False) as s:
        p = Project(project_code=code, name=f"Test {code}", pg_connection="",
                    archetype=archetype, multi_workflow=True)
        s.add(p)
        s.commit()
        s.refresh(p)
        s.expunge_all()
        return p


def _write_migration_json(base, dmig_code: str) -> dict:
    mig = {
        "source_platform": "mysql", "target_platform": "databricks",
        "target_schema": "migrated",
        "datasets": [{"source_schema": "hr_core", "source_table": "employee",
                      "target_table": "employee"}],
    }
    d = base / dmig_code / "migration"
    d.mkdir(parents=True, exist_ok=True)
    (d / "migration.json").write_text(json.dumps(mig), encoding="utf-8")
    return mig


def _ready_project(tmp_projects):
    """Set up a cmig project linked, imported, configured, spec-approved — i.e.
    forward-ready. Returns (cmig_project, dmig_code)."""
    dmig_code = "dmig-cm-01"
    cmig_code = "cmig-cm-01"
    _mk_project(dmig_code, "dmig")
    cmig = _mk_project(cmig_code, "cmig")
    _write_migration_json(tmp_projects, dmig_code)

    with Session(engine) as s:
        # link (best-effort graph writes are no-ops without Neo4j)
        cmo.link(s, cmig, dmig_code)
        # import: write an immutable source file + record its manifest
        src = cmo.source_dir(cmig_code)
        src.mkdir(parents=True, exist_ok=True)
        (src / "report.sql").write_text("SELECT 1;", encoding="utf-8")
        manifest = {"report.sql": hashlib.sha256(
            (src / "report.sql").read_bytes().hex().encode("utf-8")).hexdigest()}
        cmo.record_import(s, cmig, manifest)
        # configure with pinned corpora
        cmo.configure(s, cmig, target_runtime="14.3", output_language="sql",
                      artifact_kind="sql_script", source_platform="mysql",
                      source_platform_version="8.0",
                      source_corpus={"platform": "mysql", "version": "8.0"},
                      target_corpus={"platform": "databricks"})
        # reverse-engineer output → ingest → approve
        cmo.code_dir(cmig_code).mkdir(parents=True, exist_ok=True)
        cmo.spec_path(cmig_code).write_text(json.dumps(
            {"intent": "headcount report", "source_references": []}), encoding="utf-8")
        cmo.ingest_reverse_engineered_spec(s, cmig)
        cmo.approve_spec(s, cmig, by="eng@example.com")
    return cmig, dmig_code


def test_spec_hash_is_order_stable(tmp_projects):
    assert cmo.spec_hash({"b": 1, "a": [2, 3]}) == cmo.spec_hash({"a": [2, 3], "b": 1})


def test_forward_ready_when_all_dependencies_met(tmp_projects):
    cmig, _ = _ready_project(tmp_projects)
    with Session(engine) as s:
        assert cmo.require_forward_ready(cmig, s)["ready"] is True


def test_forward_blocked_until_spec_approved(tmp_projects):
    """The core invariant: no approval → forward-engineering is refused."""
    cmig, _ = _ready_project(tmp_projects)
    with Session(engine) as s:
        cmo.reopen_spec(s, cmig)  # voids approval
        res = cmo.require_forward_ready(cmig, s)
        assert res["ready"] is False
        assert any("approved" in m for m in res["missing"])


def test_forward_blocked_when_spec_changes_after_approval(tmp_projects):
    cmig, _ = _ready_project(tmp_projects)
    with Session(engine) as s:
        cmo.update_spec(s, cmig, {"intent": "CHANGED", "source_references": []})
        res = cmo.require_forward_ready(cmig, s)
        assert res["ready"] is False
        # update_spec voids the approval, so the blocker is "not approved".
        assert any("approv" in m for m in res["missing"])


def test_forward_blocked_on_stale_source_manifest(tmp_projects):
    cmig, _ = _ready_project(tmp_projects)
    # Mutate the immutable source after import → manifest mismatch.
    (cmo.source_dir(cmig.project_code) / "report.sql").write_text("SELECT 2;", encoding="utf-8")
    with Session(engine) as s:
        res = cmo.require_forward_ready(cmig, s)
        assert res["ready"] is False
        assert any("source" in m for m in res["missing"])


def test_forward_blocked_on_deleted_dmig(tmp_projects):
    cmig, dmig_code = _ready_project(tmp_projects)
    with Session(engine) as s:
        dmig = s.exec(
            __import__("sqlmodel").select(Project).where(Project.project_code == dmig_code)
        ).first()
        s.delete(dmig)
        s.commit()
        res = cmo.require_forward_ready(cmig, s)
        assert res["ready"] is False
        assert any("deleted" in m or "linked" in m for m in res["missing"])


def test_forward_blocked_on_migration_drift(tmp_projects):
    cmig, dmig_code = _ready_project(tmp_projects)
    # The linked migration.json changed since link → drift.
    d = tmp_projects / dmig_code / "migration"
    d.mkdir(parents=True, exist_ok=True)
    (d / "migration.json").write_text(json.dumps({"datasets": [], "changed": True}), encoding="utf-8")
    with Session(engine) as s:
        res = cmo.require_forward_ready(cmig, s)
        assert res["ready"] is False
        assert any("migration changed" in m or "relink" in m for m in res["missing"])


def test_forward_blocked_when_corpus_unpinned(tmp_projects):
    cmig, _ = _ready_project(tmp_projects)
    with Session(engine) as s:
        row = cmo.get_row(s, cmig.project_code)
        row.target_corpus_provenance_json = "{}"
        s.add(row)
        s.commit()
        res = cmo.require_forward_ready(cmig, s)
        assert res["ready"] is False
        assert any("target SME corpus" in m for m in res["missing"])


def test_state_machine_progression(tmp_projects):
    cmig, _ = _ready_project(tmp_projects)
    with Session(engine) as s:
        row = cmo.get_row(s, cmig.project_code)
        assert row.status == cmo.CmigStatus.APPROVED
        cmo.record_conversion(s, cmig, ok=True, has_actions=True)
        assert cmo.get_row(s, cmig.project_code).status == cmo.CmigStatus.CONVERTED_WITH_ACTIONS
        cmo.record_conversion(s, cmig, ok=False)
        assert cmo.get_row(s, cmig.project_code).status == cmo.CmigStatus.FAILED


def test_ingest_is_fail_closed_without_spec(tmp_projects):
    """No codespec.json on disk → ingest returns None (stage stays incomplete)."""
    _mk_project("cmig-noc-01", "cmig")
    cmig = _mk_project("cmig-noc-02", "cmig")
    with Session(engine) as s:
        assert cmo.ingest_reverse_engineered_spec(s, cmig) is None


def test_eligible_dmig_flags(tmp_projects):
    from workbench.backend.models import MigrationPlanRow
    _mk_project("dmig-elig-01", "dmig")
    with Session(engine) as s:
        s.add(MigrationPlanRow(project_code="dmig-elig-01", status="reconciled",
                               target_platform="databricks"))
        s.commit()
        rows = {r["project_code"]: r for r in cmo.eligible_dmig_projects(s)}
        assert rows["dmig-elig-01"]["eligible"] is True
        assert rows["dmig-elig-01"]["verified"] is True


def _cleanup_rows():
    with Session(engine) as s:
        for row in s.exec(__import__("sqlmodel").select(CodeMigrationPlanRow)).all():
            s.delete(row)
        s.commit()


@pytest.fixture(autouse=True)
def _clean_cmig_rows():
    yield
    _cleanup_rows()
