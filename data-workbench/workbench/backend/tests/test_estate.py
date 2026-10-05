"""Estate model + scan-orchestration tests (no Neo4j / live DB).

Covers the collision-safe identity scheme, the deterministic namespace policy,
PII classification, schema-change detection, and the router-level scan
lifecycle guards (scan-version increment + one-active-scan-per-source), driven by
calling the handlers directly with a SQLite session (the leased worker + graph
writes are exercised separately against a real graph).
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlmodel import Session

from workbench.backend import estate as estate_mod
from workbench.backend import estate_scan
from workbench.backend.auth import AuthUser
from workbench.backend.database import engine
from workbench.backend.models import (
    Estate,
    EstateScan,
    EstateSource,
    PlatformConnection,
)
from workbench.backend.routers import estates as estates_router
from workbench.backend.routers import feasibility as feas_router


# ── identity scheme (collision-safe) ──────────────────────────────────────────

def test_dataset_uri_collision_safe():
    # Same table name in two schemas / two sources must NOT collide.
    a = estate_mod.dataset_uri(1, 1, "db", "sales", "orders")
    b = estate_mod.dataset_uri(1, 1, "db", "hr", "orders")
    c = estate_mod.dataset_uri(1, 2, "db", "sales", "orders")
    assert a != b != c and a != c
    assert estate_mod.column_uri(1, 1, "db", "sales", "orders", "id").endswith("orders.id")


def test_schema_hash_changes_on_column_drift():
    h1 = estate_mod._schema_hash([{"name": "a", "data_type": "int"}])
    h2 = estate_mod._schema_hash([{"name": "a", "data_type": "int"},
                                  {"name": "b", "data_type": "text"}])
    h3 = estate_mod._schema_hash([{"name": "a", "data_type": "bigint"}])
    assert h1 != h2 and h1 != h3
    # Order-independent.
    assert estate_mod._schema_hash([{"name": "b", "data_type": "text"},
                                    {"name": "a", "data_type": "int"}]) == h2


# ── namespace policy (deterministic) ──────────────────────────────────────────

def test_namespace_policy_all():
    avail = ["public", "sales", "hr"]
    assert set(estate_mod.selected_namespaces('{"mode":"all"}', avail)) == set(avail)


def test_namespace_policy_include_exclude():
    avail = ["public", "sales", "hr"]
    inc = estate_mod.selected_namespaces('{"mode":"include","namespaces":["sales"]}', avail)
    assert inc == ["sales"]
    exc = estate_mod.selected_namespaces('{"mode":"exclude","namespaces":["hr"]}', avail)
    assert set(exc) == {"public", "sales"}


def test_classify_column_pii():
    assert estate_mod.classify_column("email") == "pii"
    assert estate_mod.classify_column("pan_token") == "pii"
    assert estate_mod.classify_column("amount") == "internal"


def test_scan_identity_parts():
    from workbench.backend.platform.interfaces import NamespaceRef
    pg = NamespaceRef(platform_instance_id="1", parts=["public"])
    assert estate_scan._identity_parts(pg, "mydb") == ("mydb", "public")
    dbx = NamespaceRef(platform_instance_id="1", parts=["catalog", "schema"])
    assert estate_scan._identity_parts(dbx, "mydb") == ("catalog", "schema")


# ── router lifecycle guards (SQLite only) ─────────────────────────────────────

@pytest.fixture()
def estate_source():
    """Create an Estate + a PlatformConnection + an EstateSource in SQLite."""
    with Session(engine, expire_on_commit=False) as s:
        conn = PlatformConnection(connection_name="est-test-pg", platform_type="postgres",
                                  host="localhost", port=5432, database="d", username="u")
        s.add(conn)
        s.commit()
        s.refresh(conn)
        estate = Estate(name="Test Estate", domain="Cards", created_by="po@x.com")
        s.add(estate)
        s.commit()
        s.refresh(estate)
        source = EstateSource(estate_id=estate.id, connection_id=conn.id,
                              name="pg", platform="postgres")
        s.add(source)
        s.commit()
        s.refresh(source)
        yield estate, source, conn
        # cleanup
        for row in s.exec(_select_all(EstateScan)).all():
            s.delete(row)
        s.delete(source)
        s.delete(estate)
        s.delete(conn)
        s.commit()


def _select_all(model):
    from sqlmodel import select
    return select(model)


_OWNER = AuthUser(email="po@x.com", name="PO", role="owner")


def test_scan_version_increments(estate_source):
    estate, source, _ = estate_source
    with Session(engine) as s:
        body = estates_router.ScanCreate(source_id=source.id)
        r1 = estates_router.launch_scan(estate.id, body, session=s, user=_OWNER, _role=_OWNER)
        assert r1["scan_version"] == 1
        # mark it done so the one-active guard doesn't fire
        scan = s.get(EstateScan, r1["id"])
        scan.state = "completed"
        s.add(scan)
        s.commit()
        r2 = estates_router.launch_scan(estate.id, body, session=s, user=_OWNER, _role=_OWNER)
        assert r2["scan_version"] == 2


def test_scan_row_exposes_enrichment_progress_and_summary(estate_source):
    """_scan_row surfaces the new enrichment progress + summary payloads, and an
    unenriched scan reports empty dicts (pre-migration-safe defaults)."""
    estate, source, _ = estate_source
    with Session(engine) as s:
        scan = EstateScan(estate_id=estate.id, source_id=source.id, scan_version=1,
                          state="completed")
        s.add(scan)
        s.commit()
        s.refresh(scan)
        row = estates_router._scan_row(scan)
        assert row["enrichment_progress"] == {}
        assert row["enrichment_summary"] == {}
        assert row["enrichment_state"] is None

        # Populate mid-run progress + a schema rollup and confirm they round-trip.
        import json
        scan.enrichment_state = "enriching"
        scan.enrichment_progress_json = json.dumps({
            "schemas_total": 3, "schemas_done": 1, "current_schema": "accuweather",
            "tables_total": 30, "tables_done": 12, "columns_done": 88, "errors": [],
        })
        scan.enrichment_summary_json = json.dumps({
            "schema_descriptions": {"accuweather": "Daily and hourly weather forecasts."}
        })
        s.add(scan)
        s.commit()
        s.refresh(scan)
        row = estates_router._scan_row(scan)
        assert row["enrichment_progress"]["current_schema"] == "accuweather"
        assert row["enrichment_progress"]["tables_done"] == 12
        assert row["enrichment_summary"]["schema_descriptions"]["accuweather"].startswith("Daily")


def test_enrich_endpoint_marks_latest_scan_enriching(estate_source):
    """POST /estates/{id}/enrich flips the latest completed scan to 'enriching'."""
    estate, source, _ = estate_source
    with Session(engine) as s:
        scan = EstateScan(estate_id=estate.id, source_id=source.id, scan_version=1,
                          state="completed")
        s.add(scan)
        s.commit()
        s.refresh(scan)
        resp = estates_router.enrich_estate(estate.id, session=s, _role=_OWNER)
        assert resp["scan_id"] == scan.id
        assert resp["enrichment_state"] == "enriching"
        s.refresh(scan)
        assert scan.enrichment_state == "enriching"
        # Idempotent while in-flight.
        resp2 = estates_router.enrich_estate(estate.id, session=s, _role=_OWNER)
        assert resp2["enrichment_state"] == "enriching"


def test_one_active_scan_per_source(estate_source):
    estate, source, _ = estate_source
    with Session(engine) as s:
        body = estates_router.ScanCreate(source_id=source.id)
        estates_router.launch_scan(estate.id, body, session=s, user=_OWNER, _role=_OWNER)
        with pytest.raises(HTTPException) as exc:
            estates_router.launch_scan(estate.id, body, session=s, user=_OWNER, _role=_OWNER)
        assert exc.value.status_code == 409


def test_evaluate_blocks_running_scan(estate_source):
    estate, source, _ = estate_source
    with Session(engine) as s:
        scan = EstateScan(estate_id=estate.id, source_id=source.id, scan_version=1,
                          state="running")
        s.add(scan)
        s.commit()
        s.refresh(scan)
        body = feas_router.EvaluateRequest(estate_id=estate.id, scan_id=scan.id)
        with pytest.raises(HTTPException) as exc:
            feas_router.evaluate(body, session=s, user=_OWNER, _role=_OWNER)
        assert exc.value.status_code == 409


def test_evaluate_blocks_failed_scan(estate_source):
    estate, source, _ = estate_source
    with Session(engine) as s:
        scan = EstateScan(estate_id=estate.id, source_id=source.id, scan_version=1,
                          state="failed")
        s.add(scan)
        s.commit()
        s.refresh(scan)
        body = feas_router.EvaluateRequest(estate_id=estate.id, scan_id=scan.id)
        with pytest.raises(HTTPException) as exc:
            feas_router.evaluate(body, session=s, user=_OWNER, _role=_OWNER)
        assert exc.value.status_code == 422


# ── offline manifest import (deterministic replay, SQLite side) ───────────────

def _offline_manifest_dict():
    return {
        "manifest_version": "1", "kind": "estate", "platform": "postgres",
        "catalog": "analytics", "generated_at": "2026-08-27T00:00:00Z", "tool_version": "1",
        "extraction": {"metadata": True, "volumetrics": True, "profiling": True,
                       "values_included": True, "code_assets": False,
                       "redaction": {"pii_redacted": 1}},
        "relations": [
            {"schema": "sales", "table": "orders", "relation_kind": "table",
             "row_count": 100, "size_bytes": 4096, "comment": "Order header",
             "columns": [
                 {"name": "order_id", "data_type": "integer", "nullable": False,
                  "ordinal": 1, "primary_key": True},
                 {"name": "status", "data_type": "varchar", "nullable": True, "ordinal": 2,
                  "comment": "Lifecycle state",
                  "profile": {"null_count": 0, "distinct_count": 3,
                              "top_values": [{"value": "OPEN", "count": 40, "frequency": 0.4}]}},
             ],
             "foreign_keys": [{"from_column": "customer_id", "to_schema": "sales",
                               "to_table": "customers", "to_column": "id"}]},
        ],
        "code_assets": [],
    }


def test_import_estate_manifest_sqlite_side(estate_source, monkeypatch):
    """The offline import creates a real completed EstateScan (version-incremented),
    per-namespace rows, and an origin='offline_import' stats marker — and hands
    write_scan_snapshot the EXACT live-scan relation-dict shape. Graph writers are
    stubbed (no Neo4j in the unit suite); the round-trip-parity vs a live scan is
    the compose integration check."""
    from workbench.backend import estate_ingest, estate_manifest
    from workbench.backend.models import EstateScanNamespace
    estate, source, _ = estate_source

    captured = {}

    def _fake_snapshot(session, **kw):
        captured["relations"] = kw["relations"]
        captured["state"] = kw["state"]
        captured["version"] = kw["version"]
        return {"datasets": len(kw["relations"]), "columns": 2}

    monkeypatch.setattr(estate_mod, "write_scan_snapshot", _fake_snapshot)
    monkeypatch.setattr(estate_mod, "apply_manifest_enrichment",
                        lambda *a, **k: {"profiles": 1, "column_descriptions": 1})
    monkeypatch.setattr(estate_mod, "write_fk_edges", lambda *a, **k: 1)
    monkeypatch.setattr(estate_mod, "update_scan_graph_state", lambda *a, **k: None)

    manifest = estate_manifest.parse_manifest(_offline_manifest_dict())
    with Session(engine) as s:
        est = s.get(Estate, estate.id)
        src = s.get(EstateSource, source.id)
        result = estate_ingest.import_estate_manifest(
            s, estate=est, source=src, manifest=manifest, initiated_by="po@x.com")

        scan = s.get(EstateScan, result["scan_id"])
        assert scan.state == "completed"
        assert scan.scan_version == 1
        assert scan.depth == "profiled"
        stats = __import__("json").loads(scan.stats_json)
        assert stats["origin"] == "offline_import"
        assert stats["fk_edges_written"] == 1
        # per-namespace outcome row exists for the 'sales' schema
        nsrows = s.exec(_select_all(EstateScanNamespace)).all()
        assert any(n.namespace == "sales" and n.outcome == "scanned" for n in nsrows)

    # write_scan_snapshot got completed state + the exact 5-key column shape.
    assert captured["state"] == "completed"
    col = captured["relations"][0]["columns"][0]
    assert set(col.keys()) == {"name", "data_type", "nullable", "ordinal", "classification"}
    assert captured["relations"][0]["database"] == "analytics"


def test_preview_estate_manifest_no_write():
    from workbench.backend import estate_ingest
    import yaml as _yaml
    summary = estate_ingest.preview_estate_manifest(_yaml.safe_dump(_offline_manifest_dict()))
    assert summary["counts"]["relations"] == 1
    assert summary["depth"] == "profiled"


def test_manifest_upload_hardening():
    """The multipart reader rejects traversal names, non-YAML extensions, NUL bytes,
    and oversize (mirrors code_migration.import_code)."""
    from types import SimpleNamespace
    read = estates_router._read_uploaded_manifest
    fake = lambda name: SimpleNamespace(filename=name)
    # a dot-hidden name is rejected outright
    with pytest.raises(HTTPException) as e:
        read(fake(".hidden.yaml"), b"platform: postgres")
    assert e.value.status_code == 422
    # traversal names are NEUTRALIZED to a basename (the manifest is never written
    # to a filename-derived path), so they're accepted with the leading dirs stripped
    assert read(fake("../etc/passwd.yaml"), b"platform: postgres") == "platform: postgres"
    # disallowed extension
    with pytest.raises(HTTPException):
        read(fake("manifest.json"), b"{}")
    # NUL byte (binary)
    with pytest.raises(HTTPException):
        read(fake("m.yaml"), b"platform: postgres\x00")
    # oversize
    with pytest.raises(HTTPException):
        read(fake("m.yaml"), b"x" * (estates_router._MANIFEST_MAX_BYTES + 1))
    # happy path returns decoded text
    assert read(fake("ok.yml"), b"platform: postgres") == "platform: postgres"


def test_offline_add_source_no_connection(estate_source):
    """An offline source needs only a name + platform (no connection/creds)."""
    estate, _, _ = estate_source
    with Session(engine) as s:
        body = estates_router.SourceCreate(ingest_mode="offline", platform="snowflake",
                                           catalog="ANALYTICS", name="cant-connect")
        row = estates_router.add_source(estate.id, body, session=s, _role=_OWNER)
        assert row["ingest_mode"] == "offline"
        assert row["connection_id"] is None
        assert row["platform"] == "snowflake"
        assert row["catalog"] == "ANALYTICS"


def test_offline_add_source_requires_platform(estate_source):
    estate, _, _ = estate_source
    with Session(engine) as s:
        body = estates_router.SourceCreate(ingest_mode="offline", platform="")
        with pytest.raises(HTTPException) as exc:
            estates_router.add_source(estate.id, body, session=s, _role=_OWNER)
        assert exc.value.status_code == 422


def test_live_add_source_still_requires_connection(estate_source):
    estate, _, _ = estate_source
    with Session(engine) as s:
        body = estates_router.SourceCreate(ingest_mode="live", connection_id=None)
        with pytest.raises(HTTPException) as exc:
            estates_router.add_source(estate.id, body, session=s, _role=_OWNER)
        assert exc.value.status_code == 422


def test_extraction_package_endpoint_json(estate_source):
    """The kit download (format=json) yields a runnable file map — run.py, the
    driver-only requirements, the .env credential blank, and a README with both
    Mermaid diagrams — for an offline source."""
    import shutil
    from workbench.backend import extraction_package as ep
    estate, _, _ = estate_source
    with Session(engine) as s:
        body = estates_router.SourceCreate(ingest_mode="offline", platform="postgres",
                                           catalog="hr", name="offline-hr")
        row = estates_router.add_source(estate.id, body, session=s, _role=_OWNER)
        sid = row["id"]
        try:
            resp = estates_router.get_extraction_package(sid, format="json", session=s, _role=_OWNER)
            files = resp["files"]
            assert "run.py" in files and "_extract_core.py" in files
            assert "psycopg2-binary" in files["requirements.txt"]
            assert "WB_SOURCE_PASSWORD" in files[".env.example"]
            assert files["README.md"].count("```mermaid") == 2
        finally:
            shutil.rmtree(ep.extraction_package_dir(sid), ignore_errors=True)


def test_mcp_import_estate_scan_preview_and_validation():
    """The PO MCP import tool: preview_only summarizes with no write; a malformed
    manifest is rejected fail-closed (structured error, no graph touch)."""
    from workbench.backend import po_mcp_server as po
    from workbench.backend.models import PlatformConnection
    import yaml as _yaml
    with Session(engine, expire_on_commit=False) as s:
        est = Estate(name="MCP Offline", created_by="po@x.com"); s.add(est); s.commit(); s.refresh(est)
        src = EstateSource(estate_id=est.id, connection_id=None, ingest_mode="offline",
                           platform="postgres", catalog="analytics", name="off")
        s.add(src); s.commit(); s.refresh(src)
        sid = src.id
    raw = _yaml.safe_dump(_offline_manifest_dict())
    prev = po.import_estate_scan(sid, "po@x.com", raw, preview_only=True)
    assert prev["counts"]["relations"] == 1
    # platform mismatch is rejected
    bad = _offline_manifest_dict(); bad["platform"] = "mysql"
    res = po.import_estate_scan(sid, "po@x.com", _yaml.safe_dump(bad))
    assert "does not" in str(res.get("error", ""))
    # malformed manifest → validation error
    res2 = po.import_estate_scan(sid, "po@x.com", "not: [valid", preview_only=True)
    assert "error" in res2


# ── catalog-scoped sources + editable selection + lifecycle ───────────────────

def test_catalog_injected_into_connection_ref():
    """A source's `catalog` drives the live connect (extra_config.catalog) AND the
    `{db}` URI segment (database), without mutating the stored connection."""
    with Session(engine, expire_on_commit=False) as s:
        conn = PlatformConnection(connection_name="est-dbx", platform_type="databricks",
                                  host="h", port=443, database="",
                                  extra_config_json='{"http_path": "/sql/1.0/warehouses/x"}')
        s.add(conn); s.commit(); s.refresh(conn)
        est = Estate(name="E", created_by="po@x.com"); s.add(est); s.commit(); s.refresh(est)
        src = EstateSource(estate_id=est.id, connection_id=conn.id, platform="databricks",
                           catalog="samples")
        s.add(src); s.commit(); s.refresh(src)

        platform, ref = estate_mod.resolve_source_connection_ref(s, src)
        assert platform == "databricks"
        assert ref["extra_config"]["catalog"] == "samples"
        assert ref["extra_config"]["http_path"] == "/sql/1.0/warehouses/x"
        assert ref["database"] == "samples"  # drives the {db} URI segment
        # The stored connection's extra_config is untouched (no `catalog` leaked in).
        import json as _json
        assert "catalog" not in _json.loads(conn.extra_config_json)

        s.delete(src); s.delete(est); s.delete(conn); s.commit()


def test_add_source_persists_catalog(estate_source):
    estate, _, conn = estate_source
    with Session(engine) as s:
        row = estates_router.add_source(
            estate.id,
            estates_router.SourceCreate(connection_id=conn.id, catalog="workspace"),
            session=s, _role=None,
        )
        assert row["catalog"] == "workspace"
        s.delete(s.get(EstateSource, row["id"])); s.commit()


def test_update_source_edits_name_enabled_policy(estate_source):
    estate, source, _ = estate_source
    with Session(engine) as s:
        row = estates_router.update_source(
            estate.id, source.id,
            estates_router.SourceUpdate(name="renamed", enabled=False,
                                        namespace_policy={"mode": "include",
                                                          "namespaces": ["sales"]}),
            session=s, _role=None,
        )
        assert row["name"] == "renamed" and row["enabled"] is False
        assert row["namespace_policy"] == {"mode": "include", "namespaces": ["sales"]}


def test_catalog_immutable_once_scanned(estate_source):
    estate, source, _ = estate_source
    with Session(engine) as s:
        # Before any scan, catalog is editable.
        estates_router.update_source(estate.id, source.id,
                                     estates_router.SourceUpdate(catalog="c1"),
                                     session=s, _role=None)
        s.add(EstateScan(estate_id=estate.id, source_id=source.id, scan_version=1,
                         state="completed"))
        s.commit()
        with pytest.raises(HTTPException) as exc:
            estates_router.update_source(estate.id, source.id,
                                         estates_router.SourceUpdate(catalog="c2"),
                                         session=s, _role=None)
        assert exc.value.status_code == 409


def test_delete_source_cascades_sql_rows():
    """Deleting a source removes its scans, per-namespace rows, and any feasibility
    run/score whose evidence intersects its scans."""
    from sqlmodel import select as _select
    from workbench.backend.models import (
        EstateScanNamespace, FeasibilityRun, FeasibilityScore,
    )
    with Session(engine, expire_on_commit=False) as s:
        conn = PlatformConnection(connection_name="est-del", platform_type="postgres",
                                  host="h", port=5432, database="d", username="u")
        s.add(conn); s.commit(); s.refresh(conn)
        est = Estate(name="E", created_by="po@x.com"); s.add(est); s.commit(); s.refresh(est)
        src = EstateSource(estate_id=est.id, connection_id=conn.id, platform="postgres")
        s.add(src); s.commit(); s.refresh(src)
        scan = EstateScan(estate_id=est.id, source_id=src.id, scan_version=1, state="completed")
        s.add(scan); s.commit(); s.refresh(scan)
        s.add(EstateScanNamespace(scan_id=scan.id, estate_id=est.id, namespace="public"))
        run = FeasibilityRun(estate_id=est.id, scan_id=scan.id,
                             scan_ids_json=f"[{scan.id}]", state="completed")
        s.add(run); s.commit(); s.refresh(run)
        s.add(FeasibilityScore(run_id=run.id, estate_id=est.id, spec_id="s1"))
        s.commit()
        scan_id, run_id, src_id = scan.id, run.id, src.id

        estates_router.delete_source(est.id, src_id, session=s, _role=None)

        assert s.get(EstateSource, src_id) is None
        assert s.get(EstateScan, scan_id) is None
        assert s.get(FeasibilityRun, run_id) is None
        assert not s.exec(_select(EstateScanNamespace).where(
            EstateScanNamespace.scan_id == scan_id)).all()
        assert not s.exec(_select(FeasibilityScore).where(
            FeasibilityScore.run_id == run_id)).all()
        s.delete(s.get(Estate, est.id)); s.delete(s.get(PlatformConnection, conn.id))
        s.commit()


def test_delete_source_blocked_while_scan_active(estate_source):
    estate, source, _ = estate_source
    with Session(engine) as s:
        s.add(EstateScan(estate_id=estate.id, source_id=source.id, scan_version=1,
                         state="running"))
        s.commit()
        with pytest.raises(HTTPException) as exc:
            estates_router.delete_source(estate.id, source.id, session=s, _role=None)
        assert exc.value.status_code == 409


def test_list_connection_catalogs_unsupported_for_2level(estate_source):
    estate, _, conn = estate_source  # postgres connection (2-level)
    with Session(engine) as s:
        out = estates_router.list_connection_catalogs(estate.id, conn.id, session=s, _role=None)
        assert out["supported"] is False and out["catalogs"] == []


def test_tombstone_query_scoped_to_scanned_schemas():
    # Regression guard for the deselect-≠-delete fix: the tombstone must be bounded
    # to the schemas actually scanned, not just the source.
    assert "scanned_schemas" in estate_mod._TOMBSTONE_DATASETS


# ── act-on-green (SQLite: FeasibilityScore + IntakeSubmission composition) ─────

import json  # noqa: E402

from workbench.backend import feasibility as feas  # noqa: E402
from workbench.backend.models import (  # noqa: E402
    FeasibilityRun,
    FeasibilityScore,
    IntakeSubmission,
)


def _seed_run_score(s, estate_id, tier, spec_id="src_credit_card", evidence=None):
    run = FeasibilityRun(estate_id=estate_id, scan_id=1, state="completed")
    s.add(run)
    s.commit()
    s.refresh(run)
    score = FeasibilityScore(
        run_id=run.id, estate_id=estate_id, spec_id=spec_id, spec_name="Credit Card",
        domain="Cards", tier=tier, evaluation_state="completed",
        best_product_uri=("cards:v1" if tier in ("ready", "adaptable") else None),
        best_product_version="1.0", adaptation_notes="rename + currency_normalize",
        evidence_json=json.dumps(evidence or {}),
    )
    s.add(score)
    s.commit()
    return run, score


def test_action_ready_points_to_marketplace(estate_source):
    estate, _, _ = estate_source
    with Session(engine) as s:
        run, _ = _seed_run_score(s, estate.id, "ready")
        action = feas.build_action(s, run.id, "src_credit_card")
        assert action["action"] == "adopt"
        assert "marketplace" in action["marketplace_path"]


def test_action_adaptable_seeds_consumer_wizard(estate_source):
    estate, _, _ = estate_source
    with Session(engine) as s:
        run, _ = _seed_run_score(s, estate.id, "adaptable")
        action = feas.build_action(s, run.id, "src_credit_card")
        assert action["action"] == "adapt"
        assert action["consume_product_uri"] == "cards:v1"


def test_action_assemblable_composes_modernization_intake(estate_source):
    estate, _, _ = estate_source
    evidence = {"raw_candidates": {"datasets": [
        {"uri": "estatedataset:1:1:db.public.cards", "table": "cards", "schema": "public"},
        {"uri": "estatedataset:1:1:db.public.card_txns", "table": "card_txns", "schema": "public"},
    ]}}
    with Session(engine) as s:
        run, _ = _seed_run_score(s, estate.id, "assemblable", evidence=evidence)
        # build_action points at composition
        action = feas.build_action(s, run.id, "src_credit_card")
        assert action["action"] == "assemble"
        # act() stages a valid modernization IntakeSubmission (proposed)
        result = feas_router.act(run.id, "src_credit_card", session=s, user=_OWNER, _role=_OWNER)
        assert result["action"] == "assemble"
        assert result["source_product_count"] == 2
        sub = s.get(IntakeSubmission, result["intake_id"])
        assert sub is not None and sub.scenario == "modernization" and sub.status == "proposed"
        assert sub.source_system == "connected-estate"
        # cleanup
        s.delete(sub)
        s.commit()


# ── volumetrics + code-asset unit tests ─────────────────────────────────────────

def test_code_asset_uri_is_stable_and_unique():
    from workbench.backend.estate import code_asset_uri
    u1 = code_asset_uri(1, 2, "task", "my_task")
    u2 = code_asset_uri(1, 2, "task", "my_task")
    u3 = code_asset_uri(1, 2, "task", "other_task")
    assert u1 == u2, "URI must be deterministic"
    assert u1 != u3, "URI must be name-discriminating"
    assert u1.startswith("estatecodeasset:")


def test_compute_complexity_returns_loc_and_refs():
    from workbench.backend.estate import _compute_complexity
    preview = "SELECT a.col1, b.col2\nFROM schema.table_a a\nJOIN schema.table_b b ON a.id = b.id\n"
    loc, refs = _compute_complexity(preview)
    assert loc == 3
    assert refs >= 2  # schema.table_a + schema.table_b


def test_compute_complexity_empty_preview():
    from workbench.backend.estate import _compute_complexity
    assert _compute_complexity(None) == (0, 0)
    assert _compute_complexity("") == (0, 0)


def test_get_code_asset_provider_dispatch():
    from workbench.backend.platform.dispatch import (
        ProviderUnavailable,
        UnknownPlatform,
        get_code_asset_provider,
    )
    # Postgres has no code-asset provider → ProviderUnavailable
    with pytest.raises(ProviderUnavailable):
        get_code_asset_provider("postgres")
    # MySQL has no code-asset provider → ProviderUnavailable
    with pytest.raises(ProviderUnavailable):
        get_code_asset_provider("mysql")
    # Unknown platform → UnknownPlatform
    with pytest.raises(UnknownPlatform):
        get_code_asset_provider("oracle_xyzzy_unknown")
    # Snowflake and Databricks should return a provider (not raise)
    sf = get_code_asset_provider("snowflake")
    assert hasattr(sf, "list_code_assets")
    db = get_code_asset_provider("databricks")
    assert hasattr(db, "list_code_assets")


def test_scan_assets_endpoint_returns_empty_for_new_scan(estate_source):
    """scan_assets route returns an empty list when no code assets were captured."""
    estate, source, _ = estate_source
    with Session(engine) as s:
        # Use the scan row created by the fixture (there's always one active after run_estate_scan fixture)
        result_map = estates_router.list_scans(estate.id, session=s)
        scans = result_map.get("scans", [])
        if not scans:
            pytest.skip("no scan rows present for this estate fixture")
        scan_id = scans[0]["id"]
        result = estates_router.scan_assets(scan_id, session=s)
        assert result["scan_id"] == scan_id
        assert isinstance(result["assets"], list)
        assert result["count"] == len(result["assets"])


# ── R3 WS1: live scan progress (own-session persist, tolerant read, terminal) ──

def test_scan_progress_round_trip_and_tolerant(estate_source):
    estate, source, _ = estate_source
    with Session(engine) as s:
        scan = EstateScan(estate_id=estate.id, source_id=source.id, scan_version=1, state="running")
        s.add(scan); s.commit(); s.refresh(scan)
        sid = scan.id
        assert estates_router._scan_row(scan)["scan_progress"] == {}  # unstarted default
    # Written from _persist_scan_progress's OWN short-lived session.
    estate_scan._persist_scan_progress(sid, {
        "stage": "running", "namespaces_total": 3, "namespaces_done": 1,
        "current_namespace": "sales", "relations_found": 4, "columns_found": 20})
    with Session(engine) as s:
        scan = s.get(EstateScan, sid)
        row = estates_router._scan_row(scan)
        assert row["scan_progress"]["current_namespace"] == "sales"
        assert row["scan_progress"]["namespaces_done"] == 1
        assert row["scan_progress"]["relations_found"] == 4
        assert "updated_at" in row["scan_progress"]
        # A malformed value is tolerated → {} (never 500s the scan listing).
        scan.scan_progress_json = "{not valid json"
        s.add(scan); s.commit()
        assert estates_router._scan_row(scan)["scan_progress"] == {}


def test_scan_progress_failed_terminal_recorded(estate_source):
    estate, source, _ = estate_source
    with Session(engine) as s:
        scan = EstateScan(estate_id=estate.id, source_id=source.id, scan_version=1, state="failed")
        s.add(scan); s.commit(); s.refresh(scan)
        sid = scan.id
    estate_scan._persist_scan_progress(sid, {"stage": "failed", "reason": "connection_failure"})
    with Session(engine) as s:
        row = estates_router._scan_row(s.get(EstateScan, sid))
        assert row["scan_progress"]["stage"] == "failed"
        assert row["scan_progress"]["reason"] == "connection_failure"


# ── R3 WS2: per-catalog schema enumeration (schemas:[] never means "unknown") ──

def test_list_connection_catalogs_normalizes_schemas(estate_source, monkeypatch):
    estate, _, conn = estate_source

    class _Model:
        container_parts = ["catalog", "schema"]

    class _Prov:
        def list_catalogs_and_schemas(self, ref):
            return [{"catalog": "main", "schemas": ["sales", "hr"]},
                    {"catalog": "unknown_cat", "schemas": []},
                    {"catalog": "", "schemas": ["x"]},  # dropped (no catalog)
                    "junk"]                              # dropped (not a dict)

    monkeypatch.setattr(estates_router, "get_namespace_model", lambda p: _Model())
    monkeypatch.setattr(estates_router, "get_discovery_provider", lambda p: _Prov())
    with Session(engine) as s:
        out = estates_router.list_connection_catalogs(estate.id, conn.id, session=s, _role=None)
    assert out["supported"] is True
    cats = {c["catalog"]: c for c in out["catalogs"]}
    assert set(cats) == {"main", "unknown_cat"}
    assert cats["main"]["schemas"] == ["hr", "sales"] and cats["main"]["schemas_enumerated"] is True
    # An empty provider list is UNKNOWN, never "genuinely empty" → null + false.
    assert cats["unknown_cat"]["schemas"] is None
    assert cats["unknown_cat"]["schemas_enumerated"] is False
