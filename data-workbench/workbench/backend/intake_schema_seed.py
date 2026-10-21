"""Deterministic (no-LLM) seeder (D4): a confirmed physical schema → the project
graph catalog.

Instead of an agent running live discovery (there's no live source in schema-only
mode), this reuses the EXISTING discovery format + loader deterministically:

  1. Emit sanitized ``data_discovery/<schema>__<table>.yaml`` files (the exact
     shape ``data-discovery/extract_metadata.py`` writes) — atomically (temp +
     ``os.replace``) so a partial write never half-loads.
  2. Shell out to the SAME two loader CLIs discovery uses — ``generate_cypher.py``
     (emits idempotent MERGEs to ``cypher_scripts/catalog.cypher``) then
     ``run_cypher.py`` (executes them against the project's Neo4j) — as bounded
     subprocesses.
  3. Wire ``(:Project)-[:HAS_CATALOG]->(:Catalog)`` (graph_ops, same as discovery).
  4. Tag every seeded ``:Catalog``/``:Dataset``/``:Column`` with provenance
     (``seededFrom='intake'``, ``seededAt``) so the flip-to-live replacement (D5)
     can find and delete exactly the seeded nodes.
  5. VERIFY node counts against the confirmed schema; the caller marks the import
     stage complete ONLY when verification passes (partial load → stays pending).

The confirmed schema is authoritative here; nothing is inferred or fabricated.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import datetime
from typing import Any

import yaml

from . import physical_schema as ps
from .config import BASE_PROJECT_DIR, SKILLS_DIR
from .models import Project, ProjectPhysicalSchema

_GEN_TIMEOUT_S = 120
_RUN_TIMEOUT_S = 300
_LOADER_DIR = SKILLS_DIR / "data-discovery-to-dcat-neo4j" / "scripts"


class SeedError(Exception):
    """The schema isn't seedable (not confirmed, empty, etc.) → router 409/422."""


# ── pure conversion (unit-testable, no Neo4j / no subprocess) ────────────────

def _yaml_schema_name(table: ps.PhysicalTable) -> str:
    """Fold the (catalog, namespace) container into the single ``schema`` field
    the 2-level discovery YAML / loader understands. For a 3-level platform the
    catalog is prefixed (``catalog.namespace``) — an opaque identifier here; live
    discovery replaces it on flip-to-live (D5)."""
    if table.catalog:
        return f"{table.catalog}.{table.namespace}"
    return table.namespace


def _column_doc(col: ps.PhysicalColumn, ordinal: int) -> dict[str, Any]:
    # Mirror extract_metadata.py's per-column keys; unknown physical attributes
    # are null (we only confirmed type/nullable/pk). A confirmed nullable=None
    # defaults to True (loader stores a bool).
    return {
        "name": col.name,
        "ordinal": ordinal,
        "type": col.data_type,
        "character_maximum_length": None,
        "numeric_precision": None,
        "numeric_scale": None,
        "nullable": True if col.nullable is None else bool(col.nullable),
        "default": None,
        "comment": None,
    }


def _table_doc(table: ps.PhysicalTable) -> dict[str, Any]:
    schema = _yaml_schema_name(table)
    pk_cols = [c.name for c in table.columns if c.primary_key]
    fks = []
    for c in table.columns:
        if not c.fk_table:
            continue
        # fk_table may be "namespace.table" or bare "table".
        parts = [p for p in c.fk_table.split(".") if p]
        ref_table = parts[-1] if parts else c.fk_table
        ref_schema = ".".join(parts[:-1]) if len(parts) > 1 else schema
        fks.append({
            "constraint_name": f"{table.table}_{c.name}_fkey",
            "columns": [c.name],
            "referenced_schema": ref_schema,
            "referenced_table": ref_table,
            "referenced_columns": [c.fk_column] if c.fk_column else [],
            "on_delete": "NO ACTION",
            "on_update": "NO ACTION",
        })
    return {
        "schema": schema,
        "table": table.table,
        "comment": None,
        "columns": [_column_doc(c, i + 1) for i, c in enumerate(table.columns)],
        "primary_key": {"constraint_name": f"{table.table}_pkey", "columns": pk_cols} if pk_cols else None,
        "foreign_keys": fks,
        "unique_constraints": [],
        "indexes": [],
        "check_constraints": [],
    }


def schema_to_yaml_docs(schema: ps.PhysicalSchema) -> dict[str, dict[str, Any]]:
    """{filename: metadata_doc} for every INCLUDED table. Filenames are
    ``<schema>__<table>.yaml`` (matching discovery). Names were already
    validated path-safe at confirm time, but we re-guard here (defense in depth)."""
    docs: dict[str, dict[str, Any]] = {}
    for t in schema.tables:
        if t.excluded:
            continue
        yaml_schema = _yaml_schema_name(t)
        if ps._name_unsafe(yaml_schema) or ps._name_unsafe(t.table):
            raise SeedError(f"unsafe name in {yaml_schema}.{t.table}")
        docs[f"{yaml_schema}__{t.table}.yaml"] = _table_doc(t)
    return docs


def expected_counts(schema: ps.PhysicalSchema) -> tuple[int, int]:
    included = [t for t in schema.tables if not t.excluded]
    return len(included), sum(len(t.columns) for t in included)


# ── filesystem (atomic emit) ─────────────────────────────────────────────────

def _write_yaml_docs(project_code: str, docs: dict[str, dict[str, Any]]) -> "os.PathLike[str]":
    discovery_dir = BASE_PROJECT_DIR / project_code / "data_discovery"
    discovery_dir.mkdir(parents=True, exist_ok=True)
    for filename, doc in docs.items():
        target = discovery_dir / filename
        # temp + atomic replace so a crash mid-write never leaves a half file the
        # loader would choke on.
        fd, tmp = tempfile.mkstemp(dir=str(discovery_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.dump(doc, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return discovery_dir


# ── graph load + provenance + verification (needs Neo4j) ─────────────────────

_TAG_PROVENANCE = """
MATCH (:Project {projectCode: $pc})-[:HAS_CATALOG]->(c:Catalog)
SET c.seededFrom = 'intake', c.seededAt = $ts
WITH c
OPTIONAL MATCH (c)-[:DCAT_DATASET]->(d:Dataset)
SET d.seededFrom = 'intake', d.seededAt = $ts
WITH collect(d) AS ds
UNWIND ds AS d
OPTIONAL MATCH (d)-[:HAS_COLUMN]->(col:Column)
SET col.seededFrom = 'intake', col.seededAt = $ts
"""

_COUNT_DATASETS = ("MATCH (:Project {projectCode: $pc})-[:HAS_CATALOG]->(:Catalog)"
                   "-[:DCAT_DATASET]->(d:Dataset) RETURN count(d) AS cnt")
_COUNT_COLUMNS = ("MATCH (:Project {projectCode: $pc})-[:HAS_CATALOG]->(:Catalog)"
                  "-[:DCAT_DATASET]->(:Dataset)-[:HAS_COLUMN]->(col:Column) RETURN count(col) AS cnt")


_CLEAR_SEEDED = """
MATCH (:Project {projectCode: $pc})-[:HAS_CATALOG]->(c:Catalog {seededFrom: 'intake'})
OPTIONAL MATCH (c)-[:DCAT_DATASET]->(d:Dataset)
OPTIONAL MATCH (d)-[:HAS_COLUMN]->(col:Column)
DETACH DELETE col, d, c
"""


def clear_seeded_catalog(project: Project) -> None:
    """Seed→live reconciliation (D5): transactionally delete exactly the nodes
    this project seeded from intake (``seededFrom='intake'``), so a subsequent
    real discovery loads a fresh, authoritative catalog rather than MERGE-ing onto
    stale guessed types. Scoped to the project + the seeded tag — never touches
    discovered nodes. Best-effort (a missing graph shouldn't wedge the flip)."""
    try:
        with _neo4j(project) as ns:
            ns.run(_CLEAR_SEEDED, pc=project.project_code)
    except Exception:  # noqa: BLE001 — graph cleanup is best-effort
        pass


def _neo4j(project: Project):
    from .neo4j_client import neo4j_session
    return neo4j_session(
        project.neo4j_host, project.neo4j_port, project.neo4j_user,
        project.neo4j_password, project.neo4j_database,
    )


def _run_loader(project: Project) -> list[str]:
    """generate_cypher.py → run_cypher.py as bounded subprocesses. Returns log
    tails; raises SeedError on a nonzero exit or timeout."""
    project_dir = BASE_PROJECT_DIR / project.project_code
    gen = _LOADER_DIR / "generate_cypher.py"
    run = _LOADER_DIR / "run_cypher.py"
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
          "generate_cypher", _GEN_TIMEOUT_S)
    cypher_file = project_dir / "cypher_scripts" / "catalog.cypher"
    if not cypher_file.exists():
        raise SeedError("generate_cypher produced no catalog.cypher")
    _exec([sys.executable, str(run), str(cypher_file),
           "--host", project.neo4j_host, "--bolt-port", str(project.neo4j_port),
           "--username", project.neo4j_user, "--password", project.neo4j_password,
           "--database", project.neo4j_database], "run_cypher", _RUN_TIMEOUT_S)
    return logs


def seed_graph_from_confirmed_schema(session, project: Project) -> dict[str, Any]:
    """Deterministically seed the project catalog from the CONFIRMED physical
    schema. Returns ``{ok, datasets_loaded, columns_loaded, expected_datasets,
    expected_columns, verified, log}``. ``ok`` is True only when the load
    succeeded AND graph counts meet the expected counts — the caller marks the
    import stage complete only on ``ok`` (partial load → leave pending)."""
    row = session.get(ProjectPhysicalSchema, project.project_code)
    if row is None or row.status != "confirmed":
        raise SeedError("physical schema is not confirmed yet")
    schema = ps.PhysicalSchema.model_validate_json(row.physical_schema_json)
    docs = schema_to_yaml_docs(schema)
    if not docs:
        raise SeedError("no included tables to seed")
    exp_datasets, exp_columns = expected_counts(schema)

    _write_yaml_docs(project.project_code, docs)
    log = _run_loader(project)

    # Wire :Project→:Catalog exactly like the discovery post-step, then stamp
    # provenance so the flip-to-live replacement can target seeded nodes.
    from . import graph_ops
    graph_ops.ensure_project_node(project)
    graph_ops.link_catalogs_to_project(project)
    ts = datetime.utcnow().isoformat()
    with _neo4j(project) as ns:
        ns.run(_TAG_PROVENANCE, pc=project.project_code, ts=ts)
        datasets = ns.run(_COUNT_DATASETS, pc=project.project_code).single()["cnt"]
        columns = ns.run(_COUNT_COLUMNS, pc=project.project_code).single()["cnt"]

    verified = datasets >= exp_datasets and columns >= exp_columns
    return {
        "ok": verified,
        "datasets_loaded": datasets,
        "columns_loaded": columns,
        "expected_datasets": exp_datasets,
        "expected_columns": exp_columns,
        "verified": verified,
        "log": log,
    }
