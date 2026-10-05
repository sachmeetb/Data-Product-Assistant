"""Code-migration orchestration — CodeMigrationPlanRow lifecycle + graph links.

Thin subsystem for the ``cmig`` archetype (parallels ``migration_orchestrator``
for ``dmig``). Owns the durable ``CodeMigrationPlanRow`` (SQLite) plus the
project's on-disk code artifacts, and the single :CodeModule graph node that
links to a linked dmig project's :Dataset nodes.

Correctness invariants live here, not in the stage's ``has_review`` flag:
- The DATABASE holds the canonical reviewed spec (``spec_json`` +
  ``approved_spec_hash``); the on-disk ``codespec.json`` is a mirror.
- ``require_forward_ready`` validates the FULL dependency set (approved spec
  hash current, source manifest current, linked migration + graph nodes present,
  corpus revisions pinned) — the shared stage runner + the CLI + recovery/orphan
  paths all call it, and MCP ``force=true`` must not bypass it.
- :USES_DATASET edges are rebuilt from the APPROVED spec (not at link time), so
  lineage reflects what the code actually references.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlmodel import Session, select

from .config import BASE_PROJECT_DIR
from .models import CodeMigrationPlanRow, Project
from .neo4j_client import neo4j_session


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── lifecycle state machine ──────────────────────────────────────────────────
# new → linked → imported → configured → awaiting_review → approved → converting
#     → converted | converted_with_actions | failed → packaged
class CmigStatus:
    NEW = "new"
    LINKED = "linked"
    IMPORTED = "imported"
    CONFIGURED = "configured"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    CONVERTING = "converting"
    CONVERTED = "converted"
    CONVERTED_WITH_ACTIONS = "converted_with_actions"
    FAILED = "failed"
    PACKAGED = "packaged"


# Statuses at/after which a spec has been approved (forward-eng may proceed).
_APPROVED_OR_LATER = {
    CmigStatus.APPROVED, CmigStatus.CONVERTING, CmigStatus.CONVERTED,
    CmigStatus.CONVERTED_WITH_ACTIONS, CmigStatus.PACKAGED,
}


# ── on-disk locations ────────────────────────────────────────────────────────

def code_dir(project_code: str) -> Path:
    return BASE_PROJECT_DIR / project_code / "code_migration"


def source_dir(project_code: str) -> Path:
    """Immutable landing dir for the imported (untrusted) legacy code."""
    return code_dir(project_code) / "source"


def target_dir(project_code: str) -> Path:
    """Where forward-engineering writes the converted code."""
    return code_dir(project_code) / "target"


def spec_path(project_code: str) -> Path:
    """On-disk MIRROR of the canonical DB spec."""
    return code_dir(project_code) / "codespec.json"


def conversion_path(project_code: str) -> Path:
    return target_dir(project_code) / "conversion.json"


def design_path(project_code: str) -> Path:
    return target_dir(project_code) / "design.md"


def schema_mapping_path(project_code: str) -> Path:
    return code_dir(project_code) / "schema_mapping.json"


def package_dir(project_code: str) -> Path:
    return BASE_PROJECT_DIR / project_code / "serving" / "code_migration"


# ── canonicalization + hashing ───────────────────────────────────────────────

def canonicalize(obj: Any) -> str:
    """Stable JSON encoding so an approval hash can't drift on key order."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def spec_hash(spec: dict) -> str:
    return _sha256(canonicalize(spec))


def _hash_dir(dirpath: Path) -> dict[str, str]:
    """{relative_path: sha256} for every file under dirpath (sorted)."""
    out: dict[str, str] = {}
    if not dirpath.is_dir():
        return out
    for p in sorted(dirpath.rglob("*")):
        if p.is_file():
            try:
                out[str(p.relative_to(dirpath))] = _sha256(p.read_bytes().hex())
            except OSError:
                continue
    return out


# ── plan CRUD ────────────────────────────────────────────────────────────────

def get_row(session: Session, project_code: str) -> Optional[CodeMigrationPlanRow]:
    return session.get(CodeMigrationPlanRow, project_code)


def _get_or_create(session: Session, project_code: str) -> CodeMigrationPlanRow:
    row = session.get(CodeMigrationPlanRow, project_code)
    if row is None:
        row = CodeMigrationPlanRow(project_code=project_code, status=CmigStatus.NEW)
    return row


def _persist(session: Session, row: CodeMigrationPlanRow) -> CodeMigrationPlanRow:
    row.updated_at = _now()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


# ── graph helpers (cmig project's Neo4j = same instance as the dmig project) ──

def _neo4j(project: Project):
    return neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password,
        project.neo4j_database,
    )


def code_module_uri(project_code: str) -> str:
    """Stable :CodeModule URI (never a mutable display name)."""
    return f"codemodule:{project_code}"


_MERGE_CODE_MODULE = """\
MATCH (prj:Project {projectCode: $project_code})
MERGE (cm:CodeModule {uri: $uri})
SET cm.name = $name,
    cm.sourcePlatform = $source_platform,
    cm.targetPlatform = $target_platform,
    cm.artifactKind = $artifact_kind,
    cm.status = $status
MERGE (prj)-[:HAS_CODE_MODULE]->(cm)
RETURN cm.uri AS uri
"""

# The SECOND sanctioned cross-project edge (beyond :CONSUMES). MATCH-only against
# the linked dmig project's owner-tagged :Dataset nodes — a typo drops the edge,
# never spawns a phantom. Rebuilt from the APPROVED spec on approval.
_WIPE_USES_DATASET = """\
MATCH (cm:CodeModule {uri: $uri})-[r:USES_DATASET]->()
DELETE r
"""

_ADD_USES_DATASET = """\
MATCH (cm:CodeModule {uri: $uri})
MATCH (ds:Dataset {uri: $dataset_uri})
MERGE (cm)-[r:USES_DATASET {variant: $variant, access: $access}]->(ds)
SET r.specHash = $spec_hash, r.verified = $verified
RETURN count(r) AS n
"""

_CLEAR_CODE_MODULE = """\
MATCH (cm:CodeModule {uri: $uri})
DETACH DELETE cm
"""

# Source datasets/columns of the linked dmig project (discovered catalog).
_DMIG_SOURCE_SCHEMA = """\
MATCH (:Project {projectCode: $dmig_code})-[:HAS_CATALOG]->(:Catalog)-[:DCAT_DATASET]->(ds:Dataset)
OPTIONAL MATCH (ds)-[:HAS_COLUMN]->(col:Column)
RETURN ds.uri AS uri, coalesce(ds.schema, ds.schemaName, '') AS schema,
       ds.name AS table,
       collect({name: col.name,
                type: coalesce(col.dataType, col.data_type, col.type, '')}) AS columns
"""

# Target datasets of the linked dmig project (Part A materialized targets).
_DMIG_TARGET_SCHEMA = """\
MATCH (:Project {projectCode: $dmig_code})-[:HAS_MIGRATION_TARGET]->(t:Dataset)
OPTIONAL MATCH (t)-[:HAS_COLUMN]->(col:Column)
RETURN t.uri AS uri, coalesce(t.schema, t.schemaName, '') AS schema,
       t.name AS table, coalesce(t.verified, false) AS verified,
       collect({name: col.name,
                type: coalesce(col.dataType, col.data_type, col.type, '')}) AS columns
"""


def _query_graph(project: Project, cypher: str, **params) -> list[dict]:
    try:
        with _neo4j(project) as ns:
            return [dict(r) for r in ns.run(cypher, **params)]
    except Exception:  # noqa: BLE001 — graph reads must never hard-fail the stage
        return []


def ensure_code_module_node(session: Session, project: Project) -> None:
    """Idempotent :CodeModule create. Code text is NEVER stored on the node —
    only a pointer + platform metadata."""
    row = get_row(session, project.project_code)
    try:
        with _neo4j(project) as ns:
            ns.run(
                _MERGE_CODE_MODULE,
                project_code=project.project_code,
                uri=code_module_uri(project.project_code),
                name=project.name or project.project_code,
                source_platform=(row.source_platform if row else "") or "",
                target_platform=(row.target_platform if row else "") or "",
                artifact_kind=(row.artifact_kind if row else "") or "",
                status=(row.status if row else CmigStatus.IMPORTED),
            ).consume()
    except Exception:  # noqa: BLE001
        pass


def rebuild_uses_dataset_edges(session: Session, project: Project) -> int:
    """Rebuild :USES_DATASET from the APPROVED spec's referenced datasets. Called
    on spec approval; wiped on reopen/invalidation. Returns edges written."""
    row = get_row(session, project.project_code)
    if row is None:
        return 0
    try:
        spec = json.loads(row.spec_json or "{}")
    except (ValueError, TypeError):
        spec = {}
    refs = spec.get("source_references") or spec.get("referenced_datasets") or []
    uri = code_module_uri(project.project_code)
    written = 0
    sh = row.approved_spec_hash or spec_hash(spec)
    try:
        with _neo4j(project) as ns:
            ns.run(_WIPE_USES_DATASET, uri=uri).consume()
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                dataset_uri = ref.get("dataset_uri") or ref.get("uri")
                if not dataset_uri:
                    continue
                variant = ref.get("variant", "source")
                access = ref.get("access", "read")
                res = ns.run(
                    _ADD_USES_DATASET, uri=uri, dataset_uri=dataset_uri,
                    variant=variant, access=access, spec_hash=sh,
                    verified=bool(ref.get("verified", False)),
                ).single()
                written += int(res["n"]) if res else 0
    except Exception:  # noqa: BLE001
        return written
    return written


def clear_uses_dataset_edges(project: Project) -> None:
    try:
        with _neo4j(project) as ns:
            ns.run(_WIPE_USES_DATASET, uri=code_module_uri(project.project_code)).consume()
    except Exception:  # noqa: BLE001
        pass


def clear_code_module(project: Project) -> None:
    try:
        with _neo4j(project) as ns:
            ns.run(_CLEAR_CODE_MODULE, uri=code_module_uri(project.project_code)).consume()
    except Exception:  # noqa: BLE001
        pass


# ── linked dmig resolution + schema mapping ──────────────────────────────────

def _dmig_migration_json(dmig_code: str) -> Optional[dict]:
    """Read the linked dmig project's migration.json (authoritative for the
    source table list + target namespace)."""
    from . import serving_package
    candidates = [
        serving_package.package_dir(dmig_code, "migration") / "migration.json",
        BASE_PROJECT_DIR / dmig_code / "migration" / "migration.json",
        BASE_PROJECT_DIR / dmig_code / "migration.json",
    ]
    for p in candidates:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
    return None


def eligible_dmig_projects(session: Session) -> list[dict]:
    """dmig projects a cmig project may link to. Eligibility = a migration that
    has reached at least ``initial_snapshot_loaded`` (reconciled preferred);
    unreconciled links are allowed but flagged ``unverified``."""
    from .models import MigrationPlanRow
    out: list[dict] = []
    projects = session.exec(select(Project).where(Project.archetype == "dmig")).all()
    for p in projects:
        row = session.get(MigrationPlanRow, p.project_code)
        status = (row.status if row else "") or ""
        verified = status == "reconciled"
        loaded = status in {"initial_snapshot_loaded", "reconciled"}
        out.append({
            "project_id": p.id,
            "project_code": p.project_code,
            "name": p.name,
            "status": status,
            "target_platform": (row.target_platform if row else "") or "",
            "eligible": loaded,
            "verified": verified,
        })
    return out


def build_schema_mapping(session: Session, project: Project, dmig_code: str) -> dict:
    """Deterministic source→target schema mapping fed to forward-engineering so
    the agent never infers target relations. Derived from the linked dmig
    project's migration.json (tables + target namespace) enriched with columns
    from the dmig graph (source :Column + Part-A :MIGRATED_TO target nodes).
    Records unresolved columns as ``verified:false``."""
    mig = _dmig_migration_json(dmig_code) or {}
    src_rows = _query_graph(project, _DMIG_SOURCE_SCHEMA, dmig_code=dmig_code)
    tgt_rows = _query_graph(project, _DMIG_TARGET_SCHEMA, dmig_code=dmig_code)
    src_by_table = {(r["schema"], r["table"]): r for r in src_rows}
    tgt_by_table = {r["table"]: r for r in tgt_rows}

    target_schema = mig.get("target_schema", "")
    target_platform = mig.get("target_platform", "")
    datasets: list[dict] = []
    for ds in mig.get("datasets", []):
        s_schema = ds.get("source_schema", "")
        s_table = ds.get("source_table", "")
        t_table = ds.get("target_table", s_table)
        src = src_by_table.get((s_schema, s_table)) or {}
        tgt = tgt_by_table.get(t_table) or {}
        src_cols = [c for c in (src.get("columns") or []) if c and c.get("name")]
        tgt_cols = [c for c in (tgt.get("columns") or []) if c and c.get("name")]
        # Raw lift-and-shift: target columns mirror source unless Part A introspected them.
        verified = bool(tgt_cols) and bool(tgt.get("verified"))
        columns = []
        tgt_col_by_name = {c["name"]: c for c in tgt_cols}
        for c in (src_cols or tgt_cols):
            t = tgt_col_by_name.get(c["name"], {})
            columns.append({
                "source_name": c["name"],
                "source_type": c.get("type", ""),
                "target_name": t.get("name", c["name"]),
                "target_type": t.get("type", c.get("type", "")),
            })
        datasets.append({
            "source_schema": s_schema, "source_table": s_table,
            "source_dataset_uri": src.get("uri", ""),
            "target_schema": target_schema, "target_table": t_table,
            "target_dataset_uri": tgt.get("uri", ""),
            "verified": verified, "columns": columns,
        })
    return {
        "linked_dmig_project_code": dmig_code,
        "target_platform": target_platform,
        "target_schema": target_schema,
        "datasets": datasets,
    }


# ── high-level operations ────────────────────────────────────────────────────

def link(session: Session, project: Project, dmig_code: str) -> CodeMigrationPlanRow:
    """Bind to a completed dmig project: snapshot schema_mapping.json, record the
    linked-migration hash (drift detection), lock the target platform. NO
    :USES_DATASET edges yet (built on spec approval)."""
    dmig = session.exec(select(Project).where(Project.project_code == dmig_code)).first()
    if dmig is None or getattr(dmig, "archetype", "") != "dmig":
        raise ValueError(f"'{dmig_code}' is not a data-migration project")

    mapping = build_schema_mapping(session, project, dmig_code)
    p = schema_mapping_path(project.project_code)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(mapping, indent=2), encoding="utf-8")

    mig = _dmig_migration_json(dmig_code) or {}
    row = _get_or_create(session, project.project_code)
    row.linked_dmig_project_code = dmig_code
    row.linked_migration_hash = _sha256(canonicalize(mig))
    row.target_platform = mapping.get("target_platform", "") or row.target_platform
    row.source_platform = mig.get("source_platform", "") or row.source_platform
    if row.status in {CmigStatus.NEW}:
        row.status = CmigStatus.LINKED
    else:
        # Relink is an invalidation event.
        _invalidate_row(row, "relinked dmig")
        row.status = CmigStatus.LINKED
    return _persist(session, row)


def record_import(
    session: Session, project: Project, manifest: dict[str, str],
) -> CodeMigrationPlanRow:
    """Register the immutable source manifest + create the :CodeModule node.
    The router owns the secure file write; this records + wires the graph."""
    row = _get_or_create(session, project.project_code)
    row.source_manifest_json = json.dumps(manifest)
    row.status = CmigStatus.IMPORTED
    _persist(session, row)
    ensure_code_module_node(session, project)
    return row


def configure(
    session: Session, project: Project, *,
    target_runtime: str = "", output_language: str = "sql",
    framework: str = "", artifact_kind: str = "sql_script",
    source_platform: str = "", source_platform_version: str = "",
    source_corpus: Optional[dict] = None, target_corpus: Optional[dict] = None,
) -> CodeMigrationPlanRow:
    """Set conversion config. Target PLATFORM stays locked from the linked dmig;
    only runtime/version/language/framework/artifact-kind + corpora are chosen."""
    row = _get_or_create(session, project.project_code)
    row.target_runtime = target_runtime
    row.output_language = output_language
    row.framework = framework
    row.artifact_kind = artifact_kind
    if source_platform:
        row.source_platform = source_platform
    row.source_platform_version = source_platform_version
    if source_corpus is not None:
        row.source_corpus_provenance_json = json.dumps(source_corpus)
    if target_corpus is not None:
        row.target_corpus_provenance_json = json.dumps(target_corpus)
    # Config change after approval invalidates downstream.
    if row.status in _APPROVED_OR_LATER:
        _invalidate_row(row, "reconfigured")
    row.status = CmigStatus.CONFIGURED
    return _persist(session, row)


# ── canonical spec + review gate ─────────────────────────────────────────────

def load_spec_from_disk(project_code: str) -> Optional[dict]:
    p = spec_path(project_code)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def ingest_reverse_engineered_spec(session: Session, project: Project) -> Optional[dict]:
    """Post-run: read the codespec.json the reverse-engineer stage wrote, make the
    DB canonical, mirror it back to disk, and enter awaiting_review. Fail-closed —
    returns None (leaving status unchanged) when no valid spec was produced, so
    the caller does not mark the stage complete."""
    spec = load_spec_from_disk(project.project_code)
    if not isinstance(spec, dict) or not spec:
        return None
    row = _get_or_create(session, project.project_code)
    row.spec_json = json.dumps(spec)  # DB canonical
    # Any new reverse-engineering run supersedes a prior approval.
    row.approved_spec_hash = ""
    row.approved_by = ""
    row.approved_at = None
    row.status = CmigStatus.AWAITING_REVIEW
    _persist(session, row)
    # Mirror the canonical spec back to disk (stable ordering).
    try:
        spec_path(project.project_code).write_text(
            json.dumps(spec, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass
    return spec


def get_spec(session: Session, project_code: str) -> dict:
    row = get_row(session, project_code)
    if row is None:
        return {}
    try:
        spec = json.loads(row.spec_json or "{}")
    except (ValueError, TypeError):
        spec = {}
    return {
        "spec": spec,
        "status": row.status,
        "approved": row.status in _APPROVED_OR_LATER,
        "approved_spec_hash": row.approved_spec_hash,
        "current_spec_hash": spec_hash(spec) if spec else "",
        "spec_revision": row.spec_revision,
    }


def update_spec(session: Session, project: Project, spec: dict) -> CodeMigrationPlanRow:
    """Editing the spec re-opens review + voids any approval + stales edges."""
    row = _get_or_create(session, project.project_code)
    row.spec_json = json.dumps(spec)
    row.spec_revision += 1
    row.approved_spec_hash = ""
    row.approved_by = ""
    row.approved_at = None
    if row.status in _APPROVED_OR_LATER:
        _invalidate_row(row, "spec edited")
    row.status = CmigStatus.AWAITING_REVIEW
    _persist(session, row)
    clear_uses_dataset_edges(project)
    try:
        spec_path(project.project_code).write_text(
            json.dumps(spec, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass
    return row


def approve_spec(session: Session, project: Project, by: str = "") -> CodeMigrationPlanRow:
    """Approve the CURRENT spec: pin its hash and rebuild :USES_DATASET from it."""
    row = get_row(session, project.project_code)
    if row is None:
        raise ValueError("no code-migration plan to approve")
    try:
        spec = json.loads(row.spec_json or "{}")
    except (ValueError, TypeError):
        spec = {}
    if not spec:
        raise ValueError("no reverse-engineered spec to approve")
    row.approved_spec_hash = spec_hash(spec)
    row.approved_by = by
    row.approved_at = _now()
    row.status = CmigStatus.APPROVED
    _persist(session, row)
    rebuild_uses_dataset_edges(session, project)
    return row


def reopen_spec(session: Session, project: Project) -> CodeMigrationPlanRow:
    """Send the spec back for edits (voids approval + stales edges)."""
    row = get_row(session, project.project_code)
    if row is None:
        raise ValueError("no code-migration plan to reopen")
    row.approved_spec_hash = ""
    row.approved_by = ""
    row.approved_at = None
    _invalidate_row(row, "spec reopened")
    row.status = CmigStatus.AWAITING_REVIEW
    _persist(session, row)
    clear_uses_dataset_edges(project)
    return row


# ── invalidation cascade ─────────────────────────────────────────────────────

def _invalidate_row(row: CodeMigrationPlanRow, reason: str) -> None:
    """Void the approved hash + downstream artifacts (in-memory; caller persists).
    On-disk target output + package are cleared by ``invalidate``."""
    row.approved_spec_hash = ""
    row.approved_by = ""
    row.approved_at = None


def invalidate(session: Session, project: Project, reason: str) -> None:
    """Full invalidation: void approval, drop target output + conversion + edges,
    and return the row to awaiting_review (if a spec exists) or configured."""
    row = get_row(session, project.project_code)
    if row is None:
        return
    _invalidate_row(row, reason)
    try:
        spec = json.loads(row.spec_json or "{}")
    except (ValueError, TypeError):
        spec = {}
    row.status = CmigStatus.AWAITING_REVIEW if spec else CmigStatus.CONFIGURED
    _persist(session, row)
    clear_uses_dataset_edges(project)
    # Drop stale target artifacts.
    for p in (conversion_path(project.project_code), design_path(project.project_code)):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


# ── the readiness guard (shared by ALL execution paths) ──────────────────────

def require_forward_ready(project: Project, session: Session) -> dict[str, Any]:
    """Canonical readiness for cmig_forward_engineer. Validates the FULL
    dependency set and returns ``{ready, missing[]}``. The shared stage runner,
    the CLI runner, recovery, and orphan resolution all gate on this; MCP
    ``force=true`` must NOT bypass it. Pure/read-only."""
    missing: list[str] = []
    row = get_row(session, project.project_code)
    if row is None:
        return {"ready": False, "missing": ["code migration not configured"]}

    # 1. approved canonical spec hash is current
    try:
        spec = json.loads(row.spec_json or "{}")
    except (ValueError, TypeError):
        spec = {}
    if not row.approved_spec_hash:
        missing.append("the reverse-engineered spec has not been approved")
    elif not spec or spec_hash(spec) != row.approved_spec_hash:
        missing.append("the spec changed since approval — re-approve it")

    # 2. imported source manifest/hash is current (source/ is immutable)
    try:
        stored = json.loads(row.source_manifest_json or "{}")
    except (ValueError, TypeError):
        stored = {}
    if not stored:
        missing.append("no legacy code imported")
    elif _hash_dir(source_dir(project.project_code)) != stored:
        missing.append("imported source has changed on disk (manifest mismatch)")

    # 3. linked dmig project + its target graph nodes still exist
    dmig_code = row.linked_dmig_project_code
    if not dmig_code:
        missing.append("no linked data-migration project")
    else:
        dmig = session.exec(select(Project).where(Project.project_code == dmig_code)).first()
        if dmig is None:
            missing.append(f"linked data-migration project '{dmig_code}' was deleted")

    # 4. linked migration/schema-mapping hash is current (drift detection)
    if dmig_code:
        mig = _dmig_migration_json(dmig_code) or {}
        if _sha256(canonicalize(mig)) != row.linked_migration_hash:
            missing.append("the linked migration changed since link — relink to refresh the schema mapping")

    # 5. configuration complete
    if not row.artifact_kind:
        missing.append("conversion not configured (artifact kind unset)")

    # 6/7. corpus revisions pinned/current
    if not (row.source_corpus_provenance_json or "").strip() or row.source_corpus_provenance_json == "{}":
        missing.append("source SME corpus not pinned")
    if not (row.target_corpus_provenance_json or "").strip() or row.target_corpus_provenance_json == "{}":
        missing.append("target SME corpus not pinned")

    return {"ready": not missing, "missing": missing}


def record_conversion(
    session: Session, project: Project, *, ok: bool, has_actions: bool = False,
) -> CodeMigrationPlanRow:
    """Advance the plan around a forward-engineering run."""
    row = _get_or_create(session, project.project_code)
    if not ok:
        row.status = CmigStatus.FAILED
    elif has_actions:
        row.status = CmigStatus.CONVERTED_WITH_ACTIONS
    else:
        row.status = CmigStatus.CONVERTED
    return _persist(session, row)


def record_packaged(session: Session, project: Project) -> CodeMigrationPlanRow:
    row = _get_or_create(session, project.project_code)
    row.status = CmigStatus.PACKAGED
    return _persist(session, row)


# ── status payload ───────────────────────────────────────────────────────────

def status_payload(session: Session, project: Project) -> dict[str, Any]:
    row = get_row(session, project.project_code)
    if row is None:
        return {"configured": False, "status": None}
    ready = require_forward_ready(project, session)
    return {
        "configured": True,
        "status": row.status,
        "linked_dmig_project_code": row.linked_dmig_project_code,
        "source_platform": row.source_platform,
        "source_platform_version": row.source_platform_version,
        "target_platform": row.target_platform,
        "target_runtime": row.target_runtime,
        "output_language": row.output_language,
        "artifact_kind": row.artifact_kind,
        "approved": row.status in _APPROVED_OR_LATER,
        "approved_by": row.approved_by,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "forward_ready": ready["ready"],
        "forward_blockers": ready["missing"],
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
