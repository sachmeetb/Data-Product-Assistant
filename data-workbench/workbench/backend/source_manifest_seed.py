"""Deterministic (no-LLM) greenfield seeder — an offline extraction manifest →
a source-aligned (``dpe-sa``) project's catalog + profiling graph.

Phase 2 of Offline Extraction (user guide: ``docs/offline-extraction.md``). The
SAME manifest the estate path replays into an ``:EstateScan`` also seeds a
greenfield project's ``:Catalog``/``:Dataset``/``:Column`` (+ DQV profiling) graph —
so a client who can't grant a live connection still gets a real source-aligned
product. It generalizes ``intake_schema_seed`` (the schema-only migration seeder):

  1. Emit ``data_discovery/<schema>__<table>.yaml`` (discovery loader shape) AND
     ``data_profiling/<schema>__<table>__profile.yaml`` (profiling loader shape),
     atomically (temp + ``os.replace``).
  2. Run the SAME two loader pairs live discovery uses — ``data-discovery-to-dcat-neo4j``
     (catalog) then ``data-profiling-to-dqv-neo4j`` (DQV measurements) — as bounded
     subprocesses. (The discovery half reuses ``intake_schema_seed._run_loader``.)
  3. Wire ``(:Project)-[:HAS_CATALOG]->(:Catalog)`` + tag ``seededFrom='offline_import'``.
  4. Verify node counts; the caller marks the stage complete only on ``ok``.

Everything downstream (enrichment, naming, PO validation, ODCS synth, mapping,
serving) reads the graph and is unchanged — it can't tell a seeded catalog from a
live-discovered one.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import datetime
from typing import Any

import yaml

from . import estate_manifest as manifest_mod
from . import intake_schema_seed as iss
from .config import BASE_PROJECT_DIR, SKILLS_DIR
from .models import Project

_GEN_TIMEOUT_S = 120
_RUN_TIMEOUT_S = 300
_DQV_LOADER_DIR = SKILLS_DIR / "data-profiling-to-dqv-neo4j" / "scripts"


class SeedError(Exception):
    """The manifest isn't seedable → router 409/422."""


# ── filesystem (atomic emit) ─────────────────────────────────────────────────

def _write_docs(dir_path, docs: dict[str, dict[str, Any]]) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    for filename, doc in docs.items():
        fd, tmp = tempfile.mkstemp(dir=str(dir_path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.dump(doc, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            os.replace(tmp, dir_path / filename)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


# ── profiling loader (discovery half is intake_schema_seed._run_loader) ──────

def _run_profile_loader(project: Project) -> list[str]:
    """generate_dqv_cypher.py → run_dqv_cypher.py. Best-effort caller wraps it —
    a project may legitimately have no profiles (metadata-only manifest)."""
    project_dir = BASE_PROJECT_DIR / project.project_code
    gen = _DQV_LOADER_DIR / "generate_dqv_cypher.py"
    run = _DQV_LOADER_DIR / "run_dqv_cypher.py"
    logs: list[str] = []

    def _exec(cmd: list[str], label: str, timeout: int) -> None:
        try:
            r = subprocess.run(cmd, cwd=str(project_dir), capture_output=True,
                               text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise SeedError(f"{label} timed out after {timeout}s")
        tail = (r.stdout or "")[-1500:] + (r.stderr or "")[-1500:]
        logs.append(f"[{label}] exit={r.returncode}\n{tail}")
        if r.returncode != 0:
            raise SeedError(f"{label} failed (exit {r.returncode}): {tail[-500:]}")

    _exec([sys.executable, str(gen), str(project_dir), "--project-code", project.project_code],
          "generate_dqv_cypher", _GEN_TIMEOUT_S)
    cypher_file = project_dir / "cypher_scripts" / "dqv_profile.cypher"
    if not cypher_file.exists():
        raise SeedError("generate_dqv_cypher produced no dqv_profile.cypher")
    _exec([sys.executable, str(run), str(cypher_file),
           "--host", project.neo4j_host, "--bolt-port", str(project.neo4j_port),
           "--username", project.neo4j_user, "--password", project.neo4j_password,
           "--database", project.neo4j_database], "run_dqv_cypher", _RUN_TIMEOUT_S)
    return logs


# ── provenance + comment-seeded descriptions ─────────────────────────────────

_TAG_PROVENANCE = """
MATCH (:Project {projectCode: $pc})-[:HAS_CATALOG]->(c:Catalog)
SET c.seededFrom = 'offline_import', c.seededAt = $ts
WITH c
OPTIONAL MATCH (c)-[:DCAT_DATASET]->(d:Dataset)
SET d.seededFrom = 'offline_import', d.seededAt = $ts
WITH collect(d) AS ds
UNWIND ds AS d
OPTIONAL MATCH (d)-[:HAS_COLUMN]->(col:Column)
SET col.seededFrom = 'offline_import', col.seededAt = $ts
"""

_COUNT_DATASETS = ("MATCH (:Project {projectCode: $pc})-[:HAS_CATALOG]->(:Catalog)"
                   "-[:DCAT_DATASET]->(d:Dataset) RETURN count(d) AS cnt")
_COUNT_COLUMNS = ("MATCH (:Project {projectCode: $pc})-[:HAS_CATALOG]->(:Catalog)"
                  "-[:DCAT_DATASET]->(:Dataset)-[:HAS_COLUMN]->(col:Column) RETURN count(col) AS cnt")


# ── the seed entry point ─────────────────────────────────────────────────────

def seed_graph_from_manifest(project: Project,
                             manifest: "manifest_mod.EstateManifest") -> dict[str, Any]:
    """Deterministically seed the project's catalog + profiling graph from an
    offline manifest. Returns ``{ok, datasets_loaded, columns_loaded,
    expected_datasets, expected_columns, profiled, verified, log}``. ``ok`` is True
    only when the discovery load verified (>= expected counts); profiling is a
    best-effort add (a metadata-only manifest still verifies)."""
    discovery_docs = manifest_mod.to_discovery_docs(manifest)
    if not discovery_docs:
        raise SeedError("manifest carries no relations to seed")
    profile_docs = manifest_mod.to_profile_docs(manifest)

    exp_datasets = len(discovery_docs)
    exp_columns = sum(len(rel.columns) for rel in manifest.relations)

    project_dir = BASE_PROJECT_DIR / project.project_code
    _write_docs(project_dir / "data_discovery", discovery_docs)
    if profile_docs:
        _write_docs(project_dir / "data_profiling", profile_docs)

    # 1. Catalog load (reuse the proven discovery loader runner).
    log = iss._run_loader(project)

    # 2. Wire :Project→:Catalog + stamp provenance (same as discovery post-step).
    from . import graph_ops
    graph_ops.ensure_project_node(project)
    graph_ops.link_catalogs_to_project(project)
    ts = datetime.utcnow().isoformat()
    with iss._neo4j(project) as ns:
        ns.run(_TAG_PROVENANCE, pc=project.project_code, ts=ts)
        datasets = ns.run(_COUNT_DATASETS, pc=project.project_code).single()["cnt"]
        columns = ns.run(_COUNT_COLUMNS, pc=project.project_code).single()["cnt"]

    # 3. Profiling load (best-effort — after Column nodes exist, since it MATCHes them).
    profiled = 0
    if profile_docs:
        try:
            log += _run_profile_loader(project)
            profiled = len(profile_docs)
        except SeedError as e:
            log.append(f"[profiling] non-fatal: {e}")

    verified = datasets >= exp_datasets and columns >= exp_columns
    return {
        "ok": verified,
        "datasets_loaded": datasets,
        "columns_loaded": columns,
        "expected_datasets": exp_datasets,
        "expected_columns": exp_columns,
        "profiled": profiled,
        "verified": verified,
        "log": log,
    }
