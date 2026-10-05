"""Connected-Estate core: first-class graph identity + rescan/diff (Pillar 1).

A **separate bounded context** from the Pulse-backed estate discovery
(``routers/discovery.py`` + ``playbook/discovery/estate.yaml``). Here Data
Workbench connects to a live platform itself and materializes what it finds as
first-class graph nodes it OWNS:

    (:Estate {uri:"estate:{id}"})-[:HAS_SCAN]->(:EstateScan {uri:"estatescan:{id}", version})
    (:EstateScan)-[:OBSERVED]->(:EstateDataset {uri:"estatedataset:{estate}:{source}:{db}.{schema}.{table}"})
    (:EstateDataset)-[:HAS_COLUMN]->(:EstateColumn {uri:"estatecolumn:{...}.{col}"})

Identities carry source + database + schema + relation so two schemas (or two
sources) with the same table name never collide. Datasets/columns have STABLE
URIs across scans; each scan connects to the datasets it observed via
``:OBSERVED``, so a feasibility read pins cleanly to one scan. Rescans diff
against the prior snapshot — new added, changed schemas versioned, deleted
tombstoned (never silently dropped), profiles marked stale.

The broad metadata scan that feeds this is DETERMINISTIC and provider-driven
(see ``estate_scan.py``): NO LLM skill runs across the estate. This module owns
the Neo4j writes/reads; ``estate_scan.py`` orchestrates the scan; the Pulse
discovery loader is never touched.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from sqlmodel import Session, select

from . import embeddings, schema_dna
from .models import AppSettings, EstateSource, PlatformConnection
from .neo4j_client import neo4j_session
from .platform.namespace import get_namespace_model


# ── identity scheme ────────────────────────────────────────────────────────────

def estate_uri(estate_id: int) -> str:
    return f"estate:{estate_id}"


def scan_uri(scan_id: int) -> str:
    return f"estatescan:{scan_id}"


def _qualified(database: str, schema: str, relation: str) -> str:
    """A dotted db.schema.relation path, dropping empty leading parts."""
    return ".".join(p for p in (database, schema, relation) if p)


def dataset_uri(estate_id: int, source_id: int, database: str, schema: str, table: str) -> str:
    return f"estatedataset:{estate_id}:{source_id}:{_qualified(database, schema, table)}"


def column_uri(estate_id: int, source_id: int, database: str, schema: str, table: str, col: str) -> str:
    return f"estatecolumn:{estate_id}:{source_id}:{_qualified(database, schema, table)}.{col}"


def parse_dataset_uri(uri: str) -> Optional[dict[str, Any]]:
    """Inverse of :func:`dataset_uri` → ``{estate_id, source_id, database, schema, table}``.

    The forward builder drops empty *leading* path parts (``_qualified``), so the
    trailing path element is ALWAYS the table, the one before it the schema, and
    the one before that the database. The ``estatedataset:{estate}:{source}:``
    prefix uses colons (never present inside identifiers) and the qualified path
    uses dots, so a 4-way ``split(":", 3)`` cleanly separates the two. Returns
    ``None`` for a non-``estatedataset:`` URI or a structurally malformed one."""
    if not uri or not uri.startswith("estatedataset:"):
        return None
    parts = uri.split(":", 3)  # ["estatedataset", estate_id, source_id, "db.schema.table"]
    if len(parts) != 4:
        return None
    try:
        estate_id = int(parts[1])
        source_id = int(parts[2])
    except (TypeError, ValueError):
        return None
    path = [p for p in parts[3].split(".")]
    if not path or not path[-1]:
        return None
    table = path[-1]
    schema = path[-2] if len(path) >= 2 else ""
    database = path[-3] if len(path) >= 3 else ""
    return {"estate_id": estate_id, "source_id": source_id,
            "database": database, "schema": schema, "table": table}


def _schema_hash(columns: list[dict[str, Any]]) -> str:
    """Content hash of a dataset's column signature — drives change detection."""
    sig = ";".join(
        f"{c.get('name', '')}:{(c.get('data_type') or '').lower()}"
        for c in sorted(columns, key=lambda c: c.get("name", ""))
    )
    return hashlib.sha256(sig.encode("utf-8")).hexdigest()[:16]


# ── Neo4j session (estate is NOT project-scoped → uses global AppSettings) ──────

def estate_graph_session(session: Session):
    """Yield a Neo4j session against the global (AppSettings) graph — the same
    graph that holds the marketplace product nodes, so feasibility can read both
    the estate raw inventory and published products in one place."""
    settings = session.exec(select(AppSettings)).first()
    if settings is None:
        settings = AppSettings()
    return neo4j_session(
        settings.neo4j_host, settings.neo4j_port, settings.neo4j_user,
        settings.neo4j_password, settings.neo4j_database,
    )


def estate_settings(session: Session) -> AppSettings:
    """Resolve the global AppSettings row (Neo4j connection details) — a plain
    object safe to hand to a worker thread (its scalars are already loaded), so an
    async caller can offload CPU-bound embedding via ``asyncio.to_thread`` without
    touching the SQLModel session across the thread boundary."""
    settings = session.exec(select(AppSettings)).first()
    return settings if settings is not None else AppSettings()


# ── estate column embeddings (graph-native, for feasibility reuse) ───────────────
#
# Vectors for text-similarity matching are computed ONCE at enrichment time and
# stored on :EstateColumn (content-hash + model stamped so they're never stale),
# then READ BACK by feasibility instead of being recomputed every run. A native
# vector index enables both current reuse and future "find similar estate columns"
# search. Mirrors business_concepts' concept-vector pattern.

ESTATE_VECTOR_INDEX_NAME = "estate_column_embedding"

_ESTATE_VECTOR_INDEX = (
    "CREATE VECTOR INDEX estate_column_embedding IF NOT EXISTS "
    "FOR (c:EstateColumn) ON c.embedding "
    "OPTIONS {indexConfig: {`vector.dimensions`: %d, `vector.similarity_function`: 'cosine'}}"
    % embeddings.EMBED_DIM
)

_SET_COLUMN_EMBEDDING = """
MATCH (c:EstateColumn {uri: $uri})
SET c.embedding = $vec, c.embeddingTextHash = $hash,
    c.embeddingModel = $model, c.embeddedAt = datetime()
"""


def estate_embed_text_hash(text: str) -> str:
    """Content hash keying an :EstateColumn's stored vector. Paired with the
    stored ``embeddingModel`` — a text edit OR a model swap changes the key and
    forces a recompute; an unchanged re-enrich is a no-op."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def ensure_estate_vector_index(settings: AppSettings) -> None:
    """Best-effort, idempotent — mirrors :func:`business_concepts.ensure_constraints`.
    Creates the native vector index over ``:EstateColumn.embedding``. Swallowed on
    older Neo4j servers (feasibility just recomputes / falls back to token-Jaccard)."""
    try:
        with neo4j_session(
            settings.neo4j_host, settings.neo4j_port, settings.neo4j_user,
            settings.neo4j_password, settings.neo4j_database,
        ) as ns:
            try:
                ns.run(_ESTATE_VECTOR_INDEX).consume()
            except Exception:
                pass
    except Exception:
        pass


def embed_and_store_columns(settings: AppSettings, items: list[dict]) -> int:
    """Batch-embed ``items`` = ``[{uri, text, hash}]`` and write the vectors onto
    ``:EstateColumn`` (``embedding`` / ``embeddingTextHash`` / ``embeddingModel`` /
    ``embeddedAt``). Idempotency — the hash/model skip — is the CALLER's job (it
    knows the prior stamps). No-op when embeddings are unavailable or nothing needs
    writing; returns the count written.

    Kept a plain sync function that opens its OWN Neo4j session (never touches a
    SQLModel session) so an async caller can run it via ``asyncio.to_thread`` off
    the event loop — fastembed encode is CPU-bound and would otherwise freeze the
    single backend loop (see research/2026-08-24-background-jobs-and-async-freeze.md).
    """
    items = [it for it in items if it.get("uri") and it.get("text")]
    if not items or not embeddings.available():
        return 0
    vecs = embeddings.embed_documents([it["text"] for it in items])
    if not vecs or len(vecs) != len(items):
        return 0
    written = 0
    try:
        with neo4j_session(
            settings.neo4j_host, settings.neo4j_port, settings.neo4j_user,
            settings.neo4j_password, settings.neo4j_database,
        ) as ns:
            for it, vec in zip(items, vecs):
                ns.run(_SET_COLUMN_EMBEDDING, uri=it["uri"], vec=vec,
                       hash=it["hash"], model=embeddings.MODEL_NAME).consume()
                written += 1
    except Exception:
        return written
    return written


_READ_SCAN_COLUMNS_FOR_EMBED = """
MATCH (s:EstateScan {uri: $scan_uri})-[:OBSERVED]->(d:EstateDataset)
WHERE d.deletedInScan IS NULL OR d.deletedInScan > s.version
MATCH (d)-[:HAS_COLUMN]->(c:EstateColumn)
WHERE c.deletedInScan IS NULL OR c.deletedInScan > s.version
RETURN c.uri AS uri, c.name AS name, c.description AS description,
       c.embeddingTextHash AS hash, c.embeddingModel AS model
"""


def backfill_estate_embeddings(settings: AppSettings, scan_id: int) -> dict[str, Any]:
    """Embed every (non-deleted) column of a scan that lacks a current vector.

    For scans enriched before graph-native vectors shipped (or after a model swap).
    Content-hash + model keyed, so re-runs only touch what changed. Mirrors
    :func:`business_concepts.backfill_embeddings`.
    """
    ensure_estate_vector_index(settings)
    if not embeddings.available():
        return {"embedded": 0, "skipped": 0, "candidates": 0, "available": False}
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port, settings.neo4j_user,
        settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        rows = [dict(r) for r in ns.run(_READ_SCAN_COLUMNS_FOR_EMBED, scan_uri=scan_uri(scan_id))]
    items: list[dict] = []
    skipped = 0
    for r in rows:
        text = schema_dna.estate_column_embed_text(r.get("name") or "", r.get("description") or "")
        h = estate_embed_text_hash(text)
        if r.get("hash") == h and r.get("model") == embeddings.MODEL_NAME:
            skipped += 1
            continue
        items.append({"uri": r["uri"], "text": text, "hash": h})
    embedded = embed_and_store_columns(settings, items)
    return {"embedded": embedded, "skipped": skipped,
            "candidates": len(items), "available": True}


# ── graph writes ───────────────────────────────────────────────────────────────

_MERGE_ESTATE = """
MERGE (e:Estate {uri: $uri})
SET e.estateId = $estate_id, e.name = $name, e.domain = $domain,
    e.status = $status, e.updatedAt = datetime()
"""

_MERGE_SCAN = """
MATCH (e:Estate {uri: $estate_uri})
MERGE (s:EstateScan {uri: $scan_uri})
SET s.scanId = $scan_id, s.estateId = $estate_id, s.sourceId = $source_id,
    s.version = $version, s.state = $state, s.depth = $depth, s.scannedAt = datetime()
MERGE (e)-[:HAS_SCAN]->(s)
"""

_MERGE_DATASET = """
MATCH (s:EstateScan {uri: $scan_uri})
MERGE (d:EstateDataset {uri: $uri})
  ON CREATE SET d.firstSeenScan = $version, d.schemaVersion = 1
SET d.estateId = $estate_id, d.sourceId = $source_id,
    d.database = $database, d.schema = $schema, d.table = $table,
    d.name = $table, d.searchName = toLower($table),
    d.relationKind = $relation_kind, d.rowCount = $row_count,
    d.sizeBytes = $size_bytes, d.lastModified = $last_modified,
    d.numFiles = $num_files, d.rowCountIsEstimate = $row_count_is_estimate,
    d.lastSeenScan = $version, d.deletedInScan = null,
    d.schemaChanged = (coalesce(d.schemaHash,'') <> $schema_hash),
    d.schemaVersion = CASE WHEN d.schemaHash IS NOT NULL AND d.schemaHash <> $schema_hash
                           THEN coalesce(d.schemaVersion,1) + 1 ELSE coalesce(d.schemaVersion,1) END,
    d.profileStale = CASE WHEN d.schemaHash IS NOT NULL AND d.schemaHash <> $schema_hash
                          THEN true ELSE coalesce(d.profileStale,false) END,
    d.schemaHash = $schema_hash
MERGE (s)-[:OBSERVED]->(d)
WITH d, (d.firstSeenScan = $version) AS is_new
RETURN is_new AS is_new, d.schemaChanged AS changed
"""

_MERGE_COLUMN = """
MATCH (d:EstateDataset {uri: $dataset_uri})
MERGE (c:EstateColumn {uri: $uri})
SET c.name = $name, c.searchName = toLower($name), c.dataType = $data_type,
    c.nullable = $nullable, c.ordinal = $ordinal, c.classification = $classification,
    c.lastSeenScan = $version, c.deletedInScan = null
MERGE (d)-[:HAS_COLUMN]->(c)
"""

# Tombstone datasets/columns of this source not seen in the current scan — but
# ONLY within the schemas this scan actually covered. A dataset in a DESELECTED
# schema (dropped from the namespace policy) still physically exists and must NOT
# be tombstoned; it simply isn't re-observed by this scan. Scoping to
# $scanned_schemas is what keeps "deselect" distinct from "deleted".
_TOMBSTONE_DATASETS = """
MATCH (:Estate {uri:$estate_uri})-[:HAS_SCAN]->(:EstateScan)-[:OBSERVED]->(d:EstateDataset)
WHERE d.sourceId = $source_id AND d.schema IN $scanned_schemas
  AND coalesce(d.lastSeenScan,0) < $version AND d.deletedInScan IS NULL
SET d.deletedInScan = $version
RETURN count(d) AS tombstoned
"""

_TOMBSTONE_COLUMNS = """
MATCH (d:EstateDataset {uri:$dataset_uri})-[:HAS_COLUMN]->(c:EstateColumn)
WHERE coalesce(c.lastSeenScan,0) < $version AND c.deletedInScan IS NULL
SET c.deletedInScan = $version
RETURN count(c) AS tombstoned
"""

_DELETE_STALE_REFERENCES = """
MATCH (src:EstateDataset {uri: $src_uri})-[r:REFERENCES]->()
DELETE r
"""

_MERGE_REFERENCES = """
MATCH (src:EstateDataset {uri: $src_uri})
MATCH (tgt:EstateDataset {uri: $tgt_uri})
MERGE (src)-[r:REFERENCES {columns: $cols, referencedColumns: $ref_cols}]->(tgt)
"""


def write_scan_snapshot(
    session: Session,
    *,
    estate_id: int,
    estate_name: str,
    estate_domain: Optional[str],
    estate_status: str,
    scan_id: int,
    source_id: int,
    version: int,
    depth: str,
    state: str,
    relations: list[dict[str, Any]],
    scanned_schemas: Optional[list[str]] = None,
) -> dict[str, int]:
    """Write one scan snapshot to the graph and return diff stats.

    ``relations`` is a list of ``{database, schema, table, relation_kind,
    row_count, columns:[{name, data_type, nullable, ordinal, classification}]}``
    — already computed deterministically by the provider-driven scan. Idempotent:
    MERGE on stable URIs, tombstone anything of this source not seen this scan.

    ``scanned_schemas`` is the set of schemas this scan actually covered (the
    selected namespaces). Tombstoning is bounded to it so DESELECTING a schema
    (narrowing the namespace policy) never marks its still-existing tables
    deleted. Defaults to the schemas present in ``relations`` when omitted.
    """
    stats = {"datasets": 0, "columns": 0, "new": 0, "changed": 0,
             "deleted": 0, "columns_deleted": 0, "total_size_bytes": 0}
    e_uri = estate_uri(estate_id)
    s_uri = scan_uri(scan_id)
    # Bound tombstoning to the schemas actually scanned (see docstring). Fall back
    # to the schemas present in this scan's relations when the caller doesn't pass
    # an explicit selection.
    if scanned_schemas is None:
        scanned_schemas = sorted({(r.get("schema", "") or "") for r in relations})
    scanned_schemas = [s for s in scanned_schemas if s]
    with estate_graph_session(session) as ns:
        ns.run(_MERGE_ESTATE, uri=e_uri, estate_id=estate_id, name=estate_name,
               domain=estate_domain or "", status=estate_status)
        ns.run(_MERGE_SCAN, estate_uri=e_uri, scan_uri=s_uri, scan_id=scan_id,
               estate_id=estate_id, source_id=source_id, version=version,
               state=state, depth=depth)

        for rel in relations:
            db = rel.get("database", "") or ""
            sch = rel.get("schema", "") or ""
            tbl = rel.get("table", "") or ""
            cols = rel.get("columns", []) or []
            d_uri = dataset_uri(estate_id, source_id, db, sch, tbl)
            rec = ns.run(
                _MERGE_DATASET, scan_uri=s_uri, uri=d_uri, estate_id=estate_id,
                source_id=source_id, database=db, schema=sch, table=tbl,
                relation_kind=rel.get("relation_kind", "table"),
                row_count=rel.get("row_count"),
                size_bytes=rel.get("size_bytes"),
                last_modified=rel.get("last_modified"),
                num_files=rel.get("num_files"),
                row_count_is_estimate=bool(rel.get("row_count_is_estimate", False)),
                schema_hash=_schema_hash(cols), version=version,
            ).single()
            stats["total_size_bytes"] += rel.get("size_bytes") or 0
            stats["datasets"] += 1
            if rec and rec["is_new"]:
                stats["new"] += 1
            elif rec and rec["changed"]:
                stats["changed"] += 1
            for c in cols:
                c_uri = column_uri(estate_id, source_id, db, sch, tbl, c.get("name", ""))
                ns.run(_MERGE_COLUMN, dataset_uri=d_uri, uri=c_uri,
                       name=c.get("name", ""), data_type=c.get("data_type", ""),
                       nullable=bool(c.get("nullable", True)),
                       ordinal=c.get("ordinal"),
                       classification=c.get("classification", "internal"),
                       version=version)
                stats["columns"] += 1
            col_tomb = ns.run(_TOMBSTONE_COLUMNS, dataset_uri=d_uri,
                              version=version).single()
            if col_tomb:
                stats["columns_deleted"] += int(col_tomb["tombstoned"] or 0)

        tomb = ns.run(_TOMBSTONE_DATASETS, estate_uri=e_uri,
                      source_id=source_id, version=version,
                      scanned_schemas=scanned_schemas).single()
        if tomb:
            stats["deleted"] = int(tomb["tombstoned"] or 0)
    return stats


_UPDATE_SCAN_STATE = """
MATCH (s:EstateScan {uri: $scan_uri})
SET s.state = $state, s.statsJson = $stats_json
"""


def update_scan_graph_state(session: Session, scan_id: int, state: str, stats: dict) -> None:
    with estate_graph_session(session) as ns:
        ns.run(_UPDATE_SCAN_STATE, scan_uri=scan_uri(scan_id), state=state,
               stats_json=json.dumps(stats, default=str))


# ── offline-import enrichment (profileJson + source-comment descriptions) ───────
#
# The offline extraction manifest captures MORE than the live scan: guarded
# per-column profiles (already redacted client-side) and source-exposed comments.
# ``write_scan_snapshot`` writes the base identity graph byte-identically to a live
# scan (so tombstone/diff/version all behave the same); this SECOND pass layers the
# extra state onto the nodes it just created — keeping the shared live-scan writer
# untouched. A column/table ``comment`` seeds ``description`` (only when empty) with
# ``descriptionSource='source_comment'`` so the later LLM enrichment fills only the
# gaps and still computes embeddings (a genuine fidelity gain the live scan can't
# match). ``profileJson`` carries the compact guarded profile the client sent.

_MERGE_COLUMN_ENRICH = """
MATCH (d:EstateDataset {uri: $dataset_uri})-[:HAS_COLUMN]->(c:EstateColumn {uri: $uri})
SET c.profileJson = CASE WHEN $profile_json IS NOT NULL THEN $profile_json ELSE c.profileJson END,
    c.profiledFromImport = CASE WHEN $profile_json IS NOT NULL THEN true
                                ELSE coalesce(c.profiledFromImport, false) END,
    c.primaryKey = coalesce($primary_key, c.primaryKey),
    c.description = CASE WHEN coalesce(c.description,'') = '' AND coalesce($comment,'') <> ''
                        THEN $comment ELSE c.description END,
    c.descriptionSource = CASE WHEN coalesce(c.description,'') = '' AND coalesce($comment,'') <> ''
                               THEN 'source_comment' ELSE c.descriptionSource END
"""

_MERGE_DATASET_ENRICH = """
MATCH (d:EstateDataset {uri: $dataset_uri})
SET d.description = CASE WHEN coalesce(d.description,'') = '' AND coalesce($comment,'') <> ''
                        THEN $comment ELSE d.description END,
    d.descriptionSource = CASE WHEN coalesce(d.description,'') = '' AND coalesce($comment,'') <> ''
                               THEN 'source_comment' ELSE d.descriptionSource END
"""


def apply_manifest_enrichment(
    session: Session,
    *,
    estate_id: int,
    source_id: int,
    database: str,
    relations: list[dict[str, Any]],
) -> dict[str, int]:
    """Layer offline-manifest extras (profiles + source comments) onto the nodes
    ``write_scan_snapshot`` just created. ``relations`` is
    ``[{schema, table, comment, columns:[{name, comment, profile_json, primary_key}]}]``.
    Idempotent: MATCH-only against the freshly-written URIs; a missing node is a
    no-op. Returns counts of what was written."""
    stats = {"profiles": 0, "column_descriptions": 0, "table_descriptions": 0}
    with estate_graph_session(session) as ns:
        for rel in relations:
            sch = rel.get("schema", "") or ""
            tbl = rel.get("table", "") or ""
            d_uri = dataset_uri(estate_id, source_id, database, sch, tbl)
            tbl_comment = (rel.get("comment") or "").strip() or None
            if tbl_comment:
                ns.run(_MERGE_DATASET_ENRICH, dataset_uri=d_uri, comment=tbl_comment)
                stats["table_descriptions"] += 1
            for c in rel.get("columns", []) or []:
                c_uri = column_uri(estate_id, source_id, database, sch, tbl, c.get("name", ""))
                profile_json = c.get("profile_json")
                comment = (c.get("comment") or "").strip() or None
                if profile_json is None and comment is None and c.get("primary_key") is None:
                    continue
                ns.run(_MERGE_COLUMN_ENRICH, dataset_uri=d_uri, uri=c_uri,
                       profile_json=profile_json, comment=comment,
                       primary_key=c.get("primary_key"))
                if profile_json is not None:
                    stats["profiles"] += 1
                if comment:
                    stats["column_descriptions"] += 1
    return stats


def write_fk_edges(
    session: Session,
    *,
    estate_id: int,
    source_id: int,
    fk_pairs: list[tuple[str, str, list[str], list[str]]],
) -> int:
    """Write :REFERENCES edges between EstateDataset nodes for FK constraints.

    Each entry in ``fk_pairs`` is ``(src_uri, tgt_uri, from_columns, to_columns)``.
    Wipes all existing :REFERENCES edges on each referenced source dataset before
    re-merging, making this idempotent per scan. Returns number of edges written.
    """
    if not fk_pairs:
        return 0
    written = 0
    with estate_graph_session(session) as ns:
        seen_src: set[str] = set()
        for src_uri, _tgt_uri, _cols, _ref_cols in fk_pairs:
            if src_uri not in seen_src:
                ns.run(_DELETE_STALE_REFERENCES, src_uri=src_uri)
                seen_src.add(src_uri)
        for src_uri, tgt_uri, cols, ref_cols in fk_pairs:
            ns.run(_MERGE_REFERENCES, src_uri=src_uri, tgt_uri=tgt_uri,
                   cols=cols, ref_cols=ref_cols)
            written += 1
    return written


# ── graph reads (pinned to a scan) ─────────────────────────────────────────────

_READ_SCAN_DATASETS = """
MATCH (s:EstateScan {uri: $scan_uri})-[:OBSERVED]->(d:EstateDataset)
WHERE d.deletedInScan IS NULL OR d.deletedInScan > s.version
OPTIONAL MATCH (d)-[:HAS_COLUMN]->(c:EstateColumn)
WHERE c.deletedInScan IS NULL OR c.deletedInScan > s.version
WITH d, c ORDER BY c.ordinal
RETURN d.uri AS uri, d.database AS database, d.schema AS schema, d.table AS table,
       d.relationKind AS relation_kind, d.rowCount AS row_count,
       d.sizeBytes AS size_bytes, d.lastModified AS last_modified,
       d.numFiles AS num_files, d.rowCountIsEstimate AS row_count_is_estimate,
       d.description AS description,
       collect(CASE WHEN c IS NULL THEN null ELSE {
           uri: c.uri, name: c.name, data_type: c.dataType,
           nullable: c.nullable, classification: c.classification,
           description: c.description, description_source: c.descriptionSource,
           profile_json: c.profileJson, primary_key: c.primaryKey
       } END) AS columns
ORDER BY d.schema, d.table
"""


def read_scan_datasets(session: Session, scan_id: int) -> list[dict[str, Any]]:
    """Read the raw datasets + columns observed by a specific scan (pinned).

    The estate's raw-data candidates for feasibility ``assemblable`` evidence.
    """
    out: list[dict[str, Any]] = []
    with estate_graph_session(session) as ns:
        for rec in ns.run(_READ_SCAN_DATASETS, scan_uri=scan_uri(scan_id)):
            cols = [c for c in (rec["columns"] or []) if c]
            out.append({
                "uri": rec["uri"], "database": rec["database"] or "",
                "schema": rec["schema"] or "", "table": rec["table"] or "",
                "relation_kind": rec["relation_kind"] or "table",
                "row_count": rec["row_count"],
                "size_bytes": rec["size_bytes"],
                "last_modified": rec["last_modified"],
                "num_files": rec["num_files"],
                "row_count_is_estimate": rec["row_count_is_estimate"],
                "description": rec["description"],
                "columns": cols,
            })
    return out


_READ_ESTATE_DATASETS = """
UNWIND $scan_uris AS su
MATCH (s:EstateScan {uri: su})-[:OBSERVED]->(d:EstateDataset)
WHERE d.deletedInScan IS NULL OR d.deletedInScan > s.version
OPTIONAL MATCH (d)-[:HAS_COLUMN]->(c:EstateColumn)
WHERE c.deletedInScan IS NULL OR c.deletedInScan > s.version
WITH d, c ORDER BY c.ordinal
RETURN d.uri AS uri, d.database AS database, d.schema AS schema, d.table AS table,
       d.relationKind AS relation_kind, d.rowCount AS row_count,
       d.sizeBytes AS size_bytes, d.lastModified AS last_modified,
       d.numFiles AS num_files, d.rowCountIsEstimate AS row_count_is_estimate,
       d.description AS description,
       collect(CASE WHEN c IS NULL THEN null ELSE {
           uri: c.uri, name: c.name, data_type: c.dataType,
           nullable: c.nullable, classification: c.classification,
           description: c.description, description_source: c.descriptionSource,
           profile_json: c.profileJson, embedding: c.embedding
       } END) AS columns
ORDER BY d.database, d.schema, d.table
"""


def read_estate_datasets(session: Session, scan_ids: list[int]) -> list[dict[str, Any]]:
    """Read datasets + columns observed across a SET of scans (one per source).

    The multi-catalog counterpart of :func:`read_scan_datasets`: unions the
    latest scan of every enabled source so one feasibility run assesses every
    catalog in the estate. Each dataset URI is unique per source+db+schema+table,
    so a dataset is observed by exactly one of the listed scans (its own source's)
    and the per-``(s,d)`` ``deletedInScan > s.version`` guard stays correct. Deduped
    by URI defensively.
    """
    if not scan_ids:
        return []
    uris = [scan_uri(sid) for sid in scan_ids]
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    with estate_graph_session(session) as ns:
        for rec in ns.run(_READ_ESTATE_DATASETS, scan_uris=uris):
            if rec["uri"] in seen:
                continue
            seen.add(rec["uri"])
            cols = [c for c in (rec["columns"] or []) if c]
            out.append({
                "uri": rec["uri"], "database": rec["database"] or "",
                "schema": rec["schema"] or "", "table": rec["table"] or "",
                "relation_kind": rec["relation_kind"] or "table",
                "row_count": rec["row_count"],
                "size_bytes": rec.get("size_bytes"),
                "last_modified": rec.get("last_modified"),
                "num_files": rec.get("num_files"),
                "row_count_is_estimate": rec.get("row_count_is_estimate"),
                "description": rec.get("description"),
                "columns": cols,
            })
    return out


_READ_ESTATE_FK_EDGES = """
UNWIND $scan_uris AS su
MATCH (s:EstateScan {uri: su})-[:OBSERVED]->(d:EstateDataset)-[r:REFERENCES]->(t:EstateDataset)
WHERE (d.deletedInScan IS NULL OR d.deletedInScan > s.version)
RETURN DISTINCT d.uri AS src_uri, t.uri AS tgt_uri,
       d.table AS src_table, t.table AS tgt_table,
       d.schema AS src_schema, t.schema AS tgt_schema,
       r.columns AS columns, r.referencedColumns AS ref_columns
"""


def read_estate_fk_edges(session: Session, scan_ids: list[int]) -> list[dict[str, Any]]:
    """Read the FK ``:REFERENCES`` edges between observed ``:EstateDataset`` nodes for a
    scan set. These are captured at scan time (``write_fk_edges`` / scan §4c) but were
    never read back — a high-precision relationship signal the source-clustering
    (`feasibility_clusters`) consumes alongside shared-key inference. Best-effort: each
    edge is ``{src_uri, tgt_uri, src_table, tgt_table, src_schema, tgt_schema, columns,
    referenced_columns}``."""
    if not scan_ids:
        return []
    uris = [scan_uri(sid) for sid in scan_ids]
    out: list[dict[str, Any]] = []
    with estate_graph_session(session) as ns:
        for rec in ns.run(_READ_ESTATE_FK_EDGES, scan_uris=uris):
            out.append({
                "src_uri": rec["src_uri"], "tgt_uri": rec["tgt_uri"],
                "src_table": rec["src_table"] or "", "tgt_table": rec["tgt_table"] or "",
                "src_schema": rec["src_schema"] or "", "tgt_schema": rec["tgt_schema"] or "",
                "columns": rec["columns"] or [], "referenced_columns": rec["ref_columns"] or [],
            })
    return out


# ── code assets ─────────────────────────────────────────────────────────────────

def code_asset_uri(estate_id: int, source_id: int, kind: str, name: str) -> str:
    import hashlib
    key = f"{estate_id}:{source_id}:{kind}:{name}"
    return f"estatecodeasset:{hashlib.sha256(key.encode()).hexdigest()[:20]}"


def _compute_complexity(preview: Optional[str]) -> tuple[int, int]:
    """Return (loc, referenced_object_count) for a definition preview snippet."""
    if not preview:
        return 0, 0
    import re
    loc = len([ln for ln in preview.splitlines() if ln.strip()])
    refs = len(set(re.findall(r'\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\b', preview)))
    return loc, refs


_MERGE_CODE_ASSET = """
MATCH (s:EstateScan {uri: $scan_uri})
MERGE (a:EstateCodeAsset {uri: $uri})
  ON CREATE SET a.firstSeenScan = $version
SET a.estateId = $estate_id,
    a.sourceId = $source_id,
    a.name = $name,
    a.assetKind = $asset_kind,
    a.namespace = $namespace_str,
    a.language = $language,
    a.schedule = $schedule,
    a.definitionPreview = $definition_preview,
    a.definitionHash = $definition_hash,
    a.complexityLoc = $complexity_loc,
    a.complexityRefs = $complexity_refs,
    a.lastSeenScan = $version,
    a.deletedInScan = null,
    a.extra = $extra_json
MERGE (s)-[:OBSERVED_ASSET]->(a)
WITH a
UNWIND $depends_on AS dep_uri
MATCH (dep:EstateCodeAsset {uri: dep_uri})
MERGE (a)-[:DEPENDS_ON]->(dep)
"""

_TOMBSTONE_ASSETS = """
MATCH (s:EstateScan {uri: $scan_uri})-[:OBSERVED_ASSET]->(a:EstateCodeAsset)
WHERE a.sourceId = $source_id
  AND a.namespace IN $namespaces
  AND NOT a.uri IN $live_uris
  AND (a.deletedInScan IS NULL)
SET a.deletedInScan = $version
"""

_READ_SCAN_ASSETS = """
MATCH (s:EstateScan {uri: $scan_uri})-[:OBSERVED_ASSET]->(a:EstateCodeAsset)
WHERE a.deletedInScan IS NULL OR a.deletedInScan > s.version
OPTIONAL MATCH (a)-[:DEPENDS_ON]->(dep:EstateCodeAsset)
WITH a, collect(dep.uri) AS dep_uris ORDER BY a.assetKind, a.name
RETURN a.uri AS uri, a.name AS name, a.assetKind AS asset_kind,
       a.namespace AS namespace, a.language AS language,
       a.schedule AS schedule, a.definitionPreview AS definition_preview,
       a.definitionHash AS definition_hash, a.complexityLoc AS complexity_loc,
       a.complexityRefs AS complexity_refs, a.extra AS extra_json,
       dep_uris AS depends_on
"""


def write_code_asset_snapshot(
    session: Session,
    *,
    estate_id: int,
    source_id: int,
    scan_id: int,
    version: int,
    assets: "list[Any]",
    namespaces: "list[str]",
) -> int:
    """Merge CodeAssetSummary objects into the graph for a scan pass.

    Returns the count of assets written. ``namespaces`` is the list of
    namespaces included in THIS pass so tombstoning is scoped correctly —
    namespaces that weren't scanned this pass are left untouched.
    """
    s_uri = scan_uri(scan_id)
    written = 0
    live_uris: list[str] = []
    with estate_graph_session(session) as ns:
        for asset in assets:
            ns_ref = getattr(asset, "namespace", None)
            # A NamespaceRef carries `parts` (dotted), NOT a `.name` — derive the
            # dotted form so it matches the `namespaces` tombstoning scope (which is
            # `_namespace_str(ns)`). (Fixes a latent AttributeError the live path
            # masked behind its best-effort try/except; a bare string passes through.)
            if ns_ref is None:
                ns_str = None
            elif isinstance(ns_ref, str):
                ns_str = ns_ref
            else:
                parts = getattr(ns_ref, "parts", None)
                ns_str = ".".join(p for p in parts if p) if parts else getattr(ns_ref, "name", None)
            preview = asset.definition_preview
            loc, refs = _compute_complexity(preview)
            uri = code_asset_uri(estate_id, source_id, asset.asset_kind, asset.name)
            live_uris.append(uri)
            extra_json = json.dumps(asset.extra or {})
            dep_uris: list[str] = []
            for dep_name in (asset.depends_on or []):
                dep_uri = code_asset_uri(estate_id, source_id, asset.asset_kind, dep_name)
                dep_uris.append(dep_uri)
            ns.run(
                _MERGE_CODE_ASSET,
                scan_uri=s_uri,
                uri=uri,
                estate_id=estate_id,
                source_id=source_id,
                name=asset.name,
                asset_kind=asset.asset_kind,
                namespace_str=ns_str,
                language=asset.language,
                schedule=asset.schedule,
                definition_preview=preview[:4096] if preview else None,
                definition_hash=asset.definition_hash,
                complexity_loc=loc,
                complexity_refs=refs,
                version=version,
                extra_json=extra_json,
                depends_on=dep_uris,
            )
            written += 1
        if live_uris or namespaces:
            ns.run(
                _TOMBSTONE_ASSETS,
                scan_uri=s_uri,
                source_id=source_id,
                namespaces=namespaces,
                live_uris=live_uris,
                version=version,
            )
    return written


def read_scan_assets(session: Session, scan_id: int) -> list[dict[str, Any]]:
    """Return all live code assets observed in a scan."""
    s_uri = scan_uri(scan_id)
    out: list[dict[str, Any]] = []
    with estate_graph_session(session) as ns:
        for rec in ns.run(_READ_SCAN_ASSETS, scan_uri=s_uri):
            extra: dict[str, Any] = {}
            try:
                extra = json.loads(rec["extra_json"] or "{}")
            except (TypeError, ValueError):
                pass
            out.append({
                "uri": rec["uri"],
                "name": rec["name"],
                "asset_kind": rec["asset_kind"],
                "namespace": rec["namespace"],
                "language": rec["language"],
                "schedule": rec["schedule"],
                "definition_preview": rec["definition_preview"],
                "definition_hash": rec["definition_hash"],
                "complexity_loc": rec["complexity_loc"],
                "complexity_refs": rec["complexity_refs"],
                "depends_on": list(rec["depends_on"] or []),
                "extra": extra,
            })
    return out


# ── graph deletion (hard cascade) ───────────────────────────────────────────────

_DELETE_SOURCE_SCANS = """
MATCH (:Estate {uri:$estate_uri})-[:HAS_SCAN]->(s:EstateScan {sourceId:$source_id})
DETACH DELETE s
"""

_DELETE_SOURCE_DATASETS = """
MATCH (d:EstateDataset {estateId:$estate_id, sourceId:$source_id})
OPTIONAL MATCH (d)-[:HAS_COLUMN]->(c:EstateColumn)
DETACH DELETE d, c
"""

_DELETE_ESTATE_SUBTREE = """
MATCH (e:Estate {uri:$estate_uri})
OPTIONAL MATCH (e)-[:HAS_SCAN]->(s:EstateScan)
OPTIONAL MATCH (d:EstateDataset {estateId:$estate_id})
OPTIONAL MATCH (d)-[:HAS_COLUMN]->(c:EstateColumn)
DETACH DELETE e, s, d, c
"""


def delete_source_graph(session: Session, estate_id: int, source_id: int) -> None:
    """Hard-remove a source's graph subtree — its scans, datasets, and columns.

    The first ``DETACH DELETE`` path in the estate subsystem (the rescan diff only
    ever soft-tombstones). Idempotent; leaves the rest of the estate untouched.
    """
    with estate_graph_session(session) as ns:
        ns.run(_DELETE_SOURCE_SCANS, estate_uri=estate_uri(estate_id),
               source_id=source_id)
        ns.run(_DELETE_SOURCE_DATASETS, estate_id=estate_id, source_id=source_id)


def delete_estate_graph(session: Session, estate_id: int) -> None:
    """Hard-remove an estate's entire graph subtree (estate + scans + datasets +
    columns). Used by the cascading estate archive."""
    with estate_graph_session(session) as ns:
        ns.run(_DELETE_ESTATE_SUBTREE, estate_uri=estate_uri(estate_id),
               estate_id=estate_id)


# ── source resolution ──────────────────────────────────────────────────────────

def resolve_source_connection_ref(session: Session, source: EstateSource) -> tuple[str, dict]:
    """Resolve an EstateSource → (platform_type, structured connection_ref).

    Mirrors ``routers.connections.resolve_source_connection_ref`` but keyed on an
    EstateSource's connection_id (an estate is not a project). ``resolved_password``
    is ephemeral — never persist/log the returned ref.
    """
    from .platform.secrets import resolve_secret

    conn = session.get(PlatformConnection, source.connection_id)
    if conn is None:
        return "postgres", {}
    extra = json.loads(conn.extra_config_json or "{}")
    database = conn.database
    # Scope the source to its selected catalog (3-level platforms). The provider
    # path reads the catalog ONLY from extra_config.catalog; `database` never
    # reaches the live connect but IS the `{db}` segment of the estate dataset
    # URIs (via `_identity_parts`). Set both so Browse, relations, and the scan
    # all target the catalog and identities stay collision-safe across catalogs.
    # Copy extra_config so the stored connection is never mutated.
    catalog = (getattr(source, "catalog", "") or "").strip()
    if catalog:
        extra = {**extra, "catalog": catalog}
        database = catalog
    return conn.platform_type, {
        "connection_id": str(conn.id),
        "host": conn.host,
        "port": conn.port,
        "database": database,
        "username": conn.username,
        "resolved_password": resolve_secret(conn.secret_ref),
        "extra_config": extra,
    }


def selected_namespaces(policy_json: str, available: list[str]) -> list[str]:
    """Apply an EstateSource namespace policy to the available namespaces.

    ``policy`` = ``{"mode": "all"|"include"|"exclude", "namespaces": [...]}``.
    Deterministic; the LLM never chooses namespaces.
    """
    try:
        policy = json.loads(policy_json or "{}")
    except (TypeError, ValueError):
        policy = {}
    mode = policy.get("mode", "all")
    listed = {str(n) for n in (policy.get("namespaces") or [])}
    if mode == "include" and listed:
        return [n for n in available if n in listed]
    if mode == "exclude" and listed:
        return [n for n in available if n not in listed]
    return list(available)


# ── PII / classification (untrusted-input + sensitive-data handling) ────────────

_PII_TOKENS = (
    "pan", "aadhaar", "email", "mobile", "phone", "ssn", "dob",
    "card_number", "account_number", "ip_address", "passport", "password",
    "secret", "token", "address",
)


def classify_column(name: str) -> str:
    """Coarse classification of a column by name → ``pii`` or ``internal``.

    Used to redact/exclude sensitive top-values from the deeper profiling pass and
    to honor governance flags in the evidence bundle. Name-only (the metadata scan
    reads no values)."""
    low = (name or "").lower()
    return "pii" if any(t in low for t in _PII_TOKENS) else "internal"
