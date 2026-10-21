"""Offline greenfield (dpe-sa) — Phase 2 of Offline Extraction.

SQLite-only: the deterministic seed (graph load) is exercised against a real graph
separately (compose integration). Here we assert the wiring: an offline dpe-sa
project scaffolds the data_discovery_offline stage (not live discovery/profiling),
persists data_connectivity_mode, and the upload/seed endpoints gate correctly.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlmodel import Session

from workbench.backend import archetypes, estate_manifest
from workbench.backend.auth import AuthUser
from workbench.backend.database import engine
from workbench.backend.models import Project, Workflow
from workbench.backend.routers import source_offline
from workbench.backend.routers.projects import ProjectCreate, create_project

_ENG = AuthUser(email="eng@x.com", name="Eng", role="engineer")


def _stage_ids(session: Session, project_id: int) -> set[str]:
    import json
    ids: set[str] = set()
    for wf in session.exec(
        __import__("sqlmodel").select(Workflow).where(Workflow.project_id == project_id)
    ).all():
        for s in json.loads(wf.workflow_json or "[]"):
            ids.add(s.get("stage_id"))
    return ids


def test_offline_dpe_sa_project_uses_offline_discovery():
    with Session(engine) as s:
        proj = create_project(ProjectCreate(
            name="Offline SA", archetype="dpe-sa", data_connectivity_mode="offline"), s)
        pid = proj["id"]
        p = s.get(Project, pid)
        assert p.data_connectivity_mode == "offline"
        ids = _stage_ids(s, pid)
        # offline discovery replaced live discovery + profiling + source picker
        assert "data_discovery_offline" in ids
        assert "select_data_source" not in ids
        assert "data_discovery_composite" not in ids
        assert "data_profiling_composite" not in ids
        # everything after discovery is unchanged
        assert {"metadata_enrichment", "po_source_validation",
                "synthesize_odcs_from_graph"} <= ids


def test_live_dpe_sa_project_unchanged():
    with Session(engine) as s:
        proj = create_project(ProjectCreate(name="Live SA", archetype="dpe-sa"), s)
        p = s.get(Project, proj["id"])
        assert (p.data_connectivity_mode or "live") == "live"
        ids = _stage_ids(s, proj["id"])
        assert "data_discovery_offline" not in ids
        assert "data_discovery_composite" in ids


def test_offline_discovery_stage_registered_non_llm():
    st = archetypes.STAGE_REGISTRY["data_discovery_offline"]
    assert st["requires_llm"] is False
    assert st["skill"] is None
    # enrichment unlocks after offline discovery (dependency alternative)
    assert "data_discovery_offline" in archetypes.DEPENDENCY_GRAPH["metadata_enrichment"]


def test_seed_offline_requires_manifest():
    with Session(engine) as s:
        proj = create_project(ProjectCreate(
            name="Offline SA2", archetype="dpe-sa", data_connectivity_mode="offline"), s)
        with pytest.raises(HTTPException) as e:
            source_offline.seed_offline(proj["id"], session=s, user=_ENG, _role=_ENG)
        assert e.value.status_code == 409


def test_seed_offline_rejects_non_dpe_sa():
    with Session(engine) as s:
        proj = create_project(ProjectCreate(name="A migration", archetype="dmig"), s)
        with pytest.raises(HTTPException) as e:
            source_offline.seed_offline(proj["id"], session=s, user=_ENG, _role=_ENG)
        assert e.value.status_code == 400


def test_upload_manifest_stores_and_previews(tmp_path, monkeypatch):
    """upload-manifest validates + stores the manifest and returns a preview with
    NO graph write."""
    import asyncio
    import workbench.backend.routers.source_offline as so
    monkeypatch.setattr(so, "BASE_PROJECT_DIR", tmp_path)
    with Session(engine) as s:
        proj = create_project(ProjectCreate(
            name="Offline SA3", archetype="dpe-sa", data_connectivity_mode="offline"), s)
        pcode = s.get(Project, proj["id"]).project_code
        manifest_yaml = __import__("yaml").safe_dump({
            "manifest_version": "1", "kind": "estate", "platform": "postgres",
            "catalog": "hr",
            "relations": [{"schema": "hr_core", "table": "employee",
                           "columns": [{"name": "id", "data_type": "integer",
                                        "primary_key": True}]}],
        })

        class _UF:
            filename = "m.yaml"
            async def read(self):
                return manifest_yaml.encode()

        resp = asyncio.run(
            so.upload_manifest(proj["id"], file=_UF(), session=s, user=_ENG, _role=_ENG))
        assert resp["stored"] is True
        assert resp["counts"]["relations"] == 1
        assert (tmp_path / pcode / "offline" / "source-manifest.yaml").exists()
