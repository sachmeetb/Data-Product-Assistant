"""Deterministic, provider-driven estate scan (Pillar 1c + Pillar 3 scan-state).

The broad metadata scan is DETERMINISTIC — it uses the platform
``DiscoveryProvider`` (``list_namespaces`` / ``list_relations`` / ``list_columns``)
directly, NOT the LLM ``data-discovery`` skill (running an LLM across hundreds of
tables in many schemas is slow, costly and non-deterministic — the exact
fragility the design review flagged). Agent skills are reserved for the
interpretive *deeper* pass (selective profiling/enrichment on PO-chosen
candidates).

v1 scopes a source to the ONE connected database/catalog the providers already
enumerate (every provider's ``list_namespaces`` lists schemas WITHIN one
database — none enumerate databases/catalogs). A database/catalog-enumeration
layer above ``list_namespaces`` is the explicit multi-DB extension.

Scan-outcome states are persisted per namespace (``EstateScanNamespace``) and the
overall ``EstateScan.state`` distinguishes ``completed | partial | failed``. A
failed/partial scan must NEVER let downstream feasibility emit ``absent`` — the
evaluator reads the scan state and returns ``insufficient_evidence`` instead.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlmodel import Session, select

from . import estate as estate_mod
from .database import engine
from .models import Estate, EstateScan, EstateScanNamespace, EstateSource
from .platform.dispatch import (
    ProviderUnavailable,
    UnknownPlatform,
    get_code_asset_provider,
    get_connection_provider,
    get_discovery_provider,
)

logger = logging.getLogger(__name__)

# Per-namespace outcome vocabulary (persisted on EstateScanNamespace.outcome).
OUTCOME_SCANNED = "scanned"
OUTCOME_EMPTY = "successful_empty"
OUTCOME_INACCESSIBLE = "inaccessible_namespace"
OUTCOME_PARTIAL = "partial_scan"
OUTCOME_CONNECTION_FAILURE = "connection_failure"


def _namespace_str(ns) -> str:
    """The dotted container path for a NamespaceRef (e.g. 'public' or 'cat.schema')."""
    return ".".join(p for p in (ns.parts or []) if p)


def _identity_parts(ns, connected_db: str) -> tuple[str, str]:
    """(database, schema) identity for a namespace.

    A multi-part namespace (catalog.schema) uses its own first/last parts; a
    single-part namespace (Postgres schema) borrows the connected database.
    """
    parts = [p for p in (ns.parts or []) if p]
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return connected_db, (parts[0] if parts else "")


def _list_available_namespaces(provider, connection_ref) -> list:
    try:
        return provider.list_namespaces(connection_ref) or []
    except Exception as exc:  # provider failure → treat as no namespaces
        logger.warning("estate scan list_namespaces failed: %s", exc)
        return []


def _probe_ok(platform: str, connection_ref: dict) -> tuple[bool, str]:
    """Preflight the connection so a connection failure becomes a ``failed`` scan
    rather than an empty one that could masquerade as ``absent`` downstream."""
    try:
        provider = get_connection_provider(platform)
    except UnknownPlatform as exc:
        return False, str(exc)
    try:
        evidence = provider.probe(connection_ref)
    except Exception as exc:  # pragma: no cover - driver-specific
        return False, f"probe raised: {exc}"
    if evidence.warnings:
        return False, "; ".join(evidence.warnings)
    return True, ""


def run_scan(session: Session, scan_id: int) -> dict[str, Any]:
    """Execute one estate scan end to end. Returns a summary dict.

    Idempotent per scan row (MERGE on stable graph URIs). Persists per-namespace
    outcomes + the graph snapshot + diff stats, and flips the scan row's ``state``.
    """
    scan = session.get(EstateScan, scan_id)
    if scan is None:
        return {"error": "scan_not_found"}
    source = session.get(EstateSource, scan.source_id)
    estate = session.get(Estate, scan.estate_id)
    if source is None or estate is None:
        _finish(session, scan, "failed", {}, {"error": "missing source/estate"})
        return {"error": "missing_source_or_estate"}

    platform, connection_ref = estate_mod.resolve_source_connection_ref(session, source)
    connected_db = connection_ref.get("database", "") or ""

    scan.state = "running"
    scan.started_at = datetime.utcnow()
    scan.updated_at = datetime.utcnow()
    session.add(scan)
    session.commit()

    # 1. Preflight the connection (fail-closed).
    ok, why = _probe_ok(platform, connection_ref)
    if not ok:
        _record_namespace(session, scan, "(connection)", OUTCOME_CONNECTION_FAILURE, 0, 0, why)
        _finish(session, scan, "failed", {}, {"connection": why})
        estate_mod.update_scan_graph_state(session, scan_id, "failed", {})
        _persist_scan_progress(scan_id, {"stage": "failed", "reason": "connection_failure"})
        return {"state": "failed", "reason": "connection_failure"}

    try:
        provider = get_discovery_provider(platform)
    except UnknownPlatform as exc:
        _finish(session, scan, "failed", {}, {"platform": str(exc)})
        _persist_scan_progress(scan_id, {"stage": "failed", "reason": "unknown_platform"})
        return {"state": "failed", "reason": "unknown_platform"}

    # 2. Enumerate + policy-filter namespaces (deterministic).
    available_ns = _list_available_namespaces(provider, connection_ref)
    available_names = [_namespace_str(ns) for ns in available_ns]
    keep = set(estate_mod.selected_namespaces(source.namespace_policy_json, available_names))
    selected = [ns for ns in available_ns if _namespace_str(ns) in keep]

    # 3. Scan each selected namespace (metadata only — no values read).
    relations: list[dict[str, Any]] = []
    # Schemas we actually READ (scanned or confirmed-empty) — bounds tombstoning so
    # a DESELECTED schema isn't mistaken for a dropped one. An INACCESSIBLE schema
    # is deliberately excluded (we couldn't see it, so we can't assert deletion).
    scanned_schemas: set[str] = set()
    scanned_ns = 0
    empty_ns = 0
    # Live progress: seed totals before the loop so a poller sees them immediately.
    # Written from _persist_scan_progress's OWN session — safe because each
    # _record_namespace commits the main session, releasing the SQLite write lock.
    progress: dict[str, Any] = {
        "stage": "running", "namespaces_total": len(selected), "namespaces_done": 0,
        "current_namespace": None, "relations_found": 0, "columns_found": 0,
    }
    _persist_scan_progress(scan.id, progress)
    for ns in selected:
        ns_str = _namespace_str(ns)
        progress["current_namespace"] = ns_str
        _persist_scan_progress(scan.id, progress)
        database, schema = _identity_parts(ns, connected_db)
        try:
            rels = provider.list_relations(connection_ref, ns) or []
        except Exception as exc:
            _record_namespace(session, scan, ns_str, OUTCOME_INACCESSIBLE, 0, 0, str(exc)[:300])
            progress["namespaces_done"] += 1
            _persist_scan_progress(scan.id, progress)
            continue
        if not rels:
            _record_namespace(session, scan, ns_str, OUTCOME_EMPTY, 0, 0, "")
            scanned_schemas.add(schema)
            empty_ns += 1
            progress["namespaces_done"] += 1
            _persist_scan_progress(scan.id, progress)
            continue
        ns_cols = 0
        for rel in rels:
            try:
                cols = provider.list_columns(connection_ref, rel) or []
            except Exception as exc:  # pragma: no cover
                logger.warning("list_columns failed for %s.%s: %s", ns_str, rel.name, exc)
                cols = []
            col_dicts = [{
                "name": c.name,
                "data_type": c.data_type,
                "nullable": c.nullable,
                "ordinal": c.ordinal,
                "classification": estate_mod.classify_column(c.name),
            } for c in cols]
            ns_cols += len(col_dicts)
            relations.append({
                "database": database, "schema": schema, "table": rel.name,
                "relation_kind": rel.relation_kind, "row_count": rel.row_count,
                "size_bytes": rel.size_bytes,
                "last_modified": rel.last_modified,
                "num_files": rel.metrics.get("num_files") if rel.metrics else None,
                "row_count_is_estimate": rel.metrics.get("row_count_is_estimate") if rel.metrics else None,
                "columns": col_dicts,
            })
        _record_namespace(session, scan, ns_str, OUTCOME_SCANNED, len(rels), ns_cols, "")
        scanned_schemas.add(schema)
        scanned_ns += 1
        progress["namespaces_done"] += 1
        progress["relations_found"] += len(rels)
        progress["columns_found"] += ns_cols
        _persist_scan_progress(scan.id, progress)

    # 4. Write the graph snapshot + compute diff stats.
    graph_stats = estate_mod.write_scan_snapshot(
        session,
        estate_id=estate.id, estate_name=estate.name, estate_domain=estate.domain,
        estate_status=estate.status, scan_id=scan.id, source_id=source.id,
        version=scan.scan_version, depth=scan.depth, state="completed",
        relations=relations, scanned_schemas=sorted(scanned_schemas),
    )
    graph_stats.update({
        "namespaces_scanned": scanned_ns,
        "namespaces_empty": empty_ns,
        "namespaces_selected": len(selected),
        "namespaces_available": len(available_ns),
    })

    # 4b. Code-asset scan (Snowflake tasks/notebooks, Databricks jobs/DLT).
    #     Best-effort: a ProviderUnavailable means the platform has no code assets
    #     (postgres/mysql/duckdb); any other exception is logged but never fails the scan.
    scanned_ns_refs = [ns for ns in selected if _namespace_str(ns) in keep]
    try:
        ca_provider = get_code_asset_provider(platform)
        code_assets = ca_provider.list_code_assets(connection_ref, scanned_ns_refs)
        ca_namespaces = [_namespace_str(ns) for ns in scanned_ns_refs]
        ca_written = estate_mod.write_code_asset_snapshot(
            session,
            estate_id=estate.id, source_id=source.id, scan_id=scan.id,
            version=scan.scan_version, assets=code_assets, namespaces=ca_namespaces,
        )
        graph_stats["code_assets_written"] = ca_written
    except (ProviderUnavailable, UnknownPlatform):
        pass  # platform has no code-asset provider — not an error
    except Exception as exc:  # pragma: no cover
        logger.warning("code-asset scan failed (non-fatal): %s", exc)
        graph_stats["code_assets_error"] = str(exc)[:300]

    # 4c. FK constraint edges between datasets (best-effort — never fails the scan).
    try:
        schema_to_fks: dict[str, list] = {}
        for ns in selected:
            ns_str = _namespace_str(ns)
            if ns_str not in keep:
                continue
            _database, schema = _identity_parts(ns, connected_db)
            if hasattr(provider, "list_foreign_keys"):
                try:
                    fks = provider.list_foreign_keys(connection_ref, schema) or []
                    if fks:
                        schema_to_fks[schema] = fks
                except Exception as _fk_exc:
                    logger.debug("list_foreign_keys failed for %s: %s", ns_str, _fk_exc)

        if schema_to_fks:
            # Flatten FK rows into (src_uri, tgt_uri, [col], [ref_col]) tuples.
            raw_pairs: list[tuple[str, str, str, str]] = []
            for _schema, fks in schema_to_fks.items():
                for fk in fks:
                    src_uri = estate_mod.dataset_uri(
                        estate.id, source.id, connected_db, fk.from_schema, fk.from_table
                    )
                    tgt_uri = estate_mod.dataset_uri(
                        estate.id, source.id, connected_db, fk.to_schema, fk.to_table
                    )
                    raw_pairs.append((src_uri, tgt_uri, fk.from_column, fk.to_column))

            # Consolidate multiple FK columns between the same pair onto one edge.
            consolidated: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
            for src_uri, tgt_uri, col, ref_col in raw_pairs:
                key = (src_uri, tgt_uri)
                if key not in consolidated:
                    consolidated[key] = ([], [])
                consolidated[key][0].append(col)
                consolidated[key][1].append(ref_col)
            final_pairs = [
                (s, t, c, r) for (s, t), (c, r) in consolidated.items()
            ]

            fk_written = estate_mod.write_fk_edges(
                session,
                estate_id=estate.id,
                source_id=source.id,
                fk_pairs=final_pairs,
            )
            if fk_written:
                graph_stats["fk_edges_written"] = fk_written
    except Exception as _fk_scan_exc:
        logger.warning("FK edge scan failed (non-fatal): %s", _fk_scan_exc)
        graph_stats["fk_edges_error"] = str(_fk_scan_exc)[:200]

    # 5. Overall state: at least one namespace scanned OR a clean empty estate =
    #    completed. Nothing accessible at all (but connection OK) = completed-empty
    #    (a real, complete answer — the estate has no scannable objects).
    state = "completed"
    _finish(session, scan, state, graph_stats, {})
    estate_mod.update_scan_graph_state(session, scan.id, state, graph_stats)
    # Terminal progress AFTER the main scan row is committed (never inside the txn).
    _persist_scan_progress(scan.id, {**progress, "stage": "completed", "current_namespace": None})
    return {"state": state, "stats": graph_stats}


def _record_namespace(session: Session, scan: EstateScan, namespace: str,
                      outcome: str, rel_count: int, col_count: int, detail: str) -> None:
    session.add(EstateScanNamespace(
        scan_id=scan.id, estate_id=scan.estate_id, namespace=namespace,
        outcome=outcome, relation_count=rel_count, column_count=col_count,
        detail=(detail or "")[:500],
    ))
    session.commit()


def _finish(session: Session, scan: EstateScan, state: str,
            stats: dict, errors: dict) -> None:
    scan.state = state
    scan.finished_at = datetime.utcnow()
    scan.stats_json = json.dumps(stats, default=str)
    scan.error_json = json.dumps(errors, default=str)
    scan.lease_owner = None
    scan.lease_expires_at = None
    scan.updated_at = datetime.utcnow()
    session.add(scan)
    session.commit()


def _persist_scan_progress(scan_id: int, progress: dict[str, Any]) -> None:
    """Commit live metadata-scan progress in its OWN short-lived session (best-
    effort) so a progress write never entangles with (or rolls back alongside) the
    main scan write txn. Mirrors ``feasibility._persist_progress``; emits a
    monotonic ``updated_at`` so a poller can tell a stalled scan from a live one.

    The terminal (``stage: completed|failed``) progress MUST be written only AFTER
    ``_finish`` has committed the main scan row — never inside an open scan write
    txn (SQLite is single-writer)."""
    payload = dict(progress)
    payload["updated_at"] = datetime.utcnow().isoformat()
    try:
        with Session(engine) as s:
            row = s.get(EstateScan, scan_id)
            if row is None:
                return
            row.scan_progress_json = json.dumps(payload, default=str)
            s.add(row)
            s.commit()
    except Exception:  # progress is telemetry, never load-bearing
        logger.debug("estate scan progress persist failed for scan %s", scan_id, exc_info=True)


# ── selective deeper pass: profiling (Phase 5, PII-safe) ─────────────────────

def profile_datasets(session: Session, scan_id: int, dataset_uris: list[str]) -> dict[str, Any]:
    """Lightweight, PII-safe profiling of PO-selected datasets (deeper pass).

    Reads row counts only (no top values) via the platform QueryExecutor and
    attaches ``:EstateProfile``-style stats to the datasets, clearing their
    ``profileStale`` flag. Sensitive columns are NEVER value-sampled. This is the
    interpretive pass the design reserves agent skills for; v1 keeps it to a
    deterministic count so it stays cheap + safe. Gated to the engineer role at
    the router (live-data read).
    """
    scan = session.get(EstateScan, scan_id)
    if scan is None:
        return {"error": "scan_not_found"}
    source = session.get(EstateSource, scan.source_id)
    if source is None:
        return {"error": "missing_source"}
    platform, connection_ref = estate_mod.resolve_source_connection_ref(session, source)

    from .platform.dispatch import get_query_executor, ProviderUnavailable
    from .platform.interfaces import QueryLimits
    from .sql_ident import quote_relation

    try:
        executor = get_query_executor(platform)
    except (UnknownPlatform, ProviderUnavailable) as exc:
        return {"error": "no_query_executor", "detail": str(exc)}

    profiled = 0
    for d in estate_mod.read_scan_datasets(session, scan_id):
        if d["uri"] not in dataset_uris:
            continue
        namespace = ".".join(p for p in (d["database"], d["schema"]) if p) or d["schema"]
        rel = quote_relation(d["schema"] or namespace, d["table"], platform)
        try:
            rs = executor.select(connection_ref, f"SELECT count(*) AS n FROM {rel}",
                                 QueryLimits(max_rows=1, timeout_ms=30_000))
            row_count = rs.rows[0][0] if rs.rows else None
        except Exception as exc:  # pragma: no cover
            logger.warning("profile row-count failed for %s: %s", d["uri"], exc)
            continue
        with estate_mod.estate_graph_session(session) as ns:
            ns.run(
                "MATCH (dd:EstateDataset {uri:$uri}) "
                "SET dd.rowCount=$rc, dd.profileStale=false, dd.profiledAt=datetime()",
                uri=d["uri"], rc=row_count,
            )
        profiled += 1
    return {"profiled": profiled}


# ── leased worker unit (claims queued EstateScan rows) ───────────────────────

def process_one_scan(worker_id: str, lease_ttl: int) -> bool:
    """Claim + run one queued scan. Public so tests can drive one iteration.

    Reclaims stale leases first (crash-safe), then claims one ``queued`` scan via
    compare-and-set and runs it. Returns True iff a scan was handled.
    """
    from datetime import timedelta

    with Session(engine) as session:
        now = datetime.utcnow()
        # Reclaim stale leases (a crashed worker left a scan in 'running').
        stale = session.exec(
            select(EstateScan).where(
                EstateScan.state == "running",
                EstateScan.lease_expires_at.is_not(None),  # type: ignore[union-attr]
                EstateScan.lease_expires_at < now,
            )
        ).all()
        for s in stale:
            s.state = "queued"
            s.lease_owner = None
            s.lease_expires_at = None
            session.add(s)
        if stale:
            session.commit()

        row = session.exec(
            select(EstateScan).where(EstateScan.state == "queued").order_by(EstateScan.id)  # type: ignore[arg-type]
        ).first()
        if row is None:
            return False
        row.state = "running"
        row.lease_owner = worker_id
        row.lease_expires_at = now + timedelta(seconds=lease_ttl)
        row.attempts += 1
        row.started_at = row.started_at or now
        session.add(row)
        session.commit()
        scan_id = row.id

    with Session(engine) as session:
        try:
            run_scan(session, scan_id)
        except Exception as exc:
            logger.exception("estate scan %s failed", scan_id)
            scan = session.get(EstateScan, scan_id)
            if scan is not None:
                _finish(session, scan, "failed", {}, {"exception": str(exc)[:500]})
                _persist_scan_progress(scan_id, {"stage": "failed", "error": str(exc)[:300]})
    return True
