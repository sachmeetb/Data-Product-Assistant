"""Object-store provider + dispatch + registry — hermetic Phase 3 coverage (ADR-14).

These tests need NO live object store and NO boto3 network calls: the s3 provider
lazy-imports boto3 inside method bodies, so dispatch resolution, registry
capability, Protocol conformance, and validate_config are all exercisable offline.
The live put/list/presign path is validated out-of-band against the SeaweedFS
fixture (docs/architecture/object-store.md — Conformance spike).
"""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from workbench.backend.platform import dispatch
from workbench.backend.platform.registry import get_registry
from workbench.backend.platform.interfaces import (
    ObjectStoreProvider, CapabilityLevel,
)
from workbench.backend.platform.providers.object_store_s3 import S3ObjectStoreProvider
from workbench.backend import serving_package
from workbench.backend import transfer_execution
from workbench.backend.main import app
from workbench.backend.database import engine
from workbench.backend.models import (
    Project, PlatformConnection, ProjectArtifactStoreBinding, ArtifactPublishRun,
)

# Module-level client (not entered as a context manager → no double lifespan).
_client = TestClient(app)


@pytest.fixture(scope="module")
def client():
    return _client


@pytest.fixture(autouse=True)
def _clean_os_tables():
    """Clear the object-store + connection + project rows around each test."""
    with Session(engine) as s:
        for model in (ArtifactPublishRun, ProjectArtifactStoreBinding,
                      PlatformConnection, Project):
            for row in s.exec(select(model)).all():
                s.delete(row)
        s.commit()
    yield


def _mk_s3_connection(client, name="os-conn", bucket="data-workbench") -> int:
    r = client.post("/api/connections", json={
        "connection_name": name, "platform_type": "s3",
        "host": "localhost", "port": 9000, "database": bucket,
        "username": "workbench", "password": "workbenchsecret",
        "extra_config": {"bucket": bucket, "endpoint_url": "http://localhost:9000"},
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _mk_project(code="os-proj") -> int:
    with Session(engine) as s:
        p = Project(project_code=code, name="OS Proj", archetype="dpe-cf")
        s.add(p); s.commit(); s.refresh(p)
        return p.id


class TestRegistryObjectStore:
    def test_s3_manifest_is_object_store_with_artifact_publish_preview(self):
        reg = get_registry()
        m = reg.get_manifest("s3")
        assert m is not None
        assert m.adapter_kind == "object_store"
        assert reg.get_capability("s3", "artifact_publish") == CapabilityLevel.PREVIEW
        assert reg.is_usable("s3", "artifact_publish") is True

    def test_gcs_azure_registered_but_artifact_publish_unsupported(self):
        reg = get_registry()
        for pid in ("gcs", "azure_adls"):
            m = reg.get_manifest(pid)
            assert m is not None and m.adapter_kind == "object_store"
            # No artifact_publish key yet → UNSUPPORTED, not usable.
            assert reg.is_usable(pid, "artifact_publish") is False

    def test_s3_sql_capabilities_stay_unsupported(self):
        reg = get_registry()
        for cap in ("connection", "read_query", "native_view", "dbt_materialized"):
            assert reg.is_usable("s3", cap) is False


class TestDispatchObjectStore:
    def test_s3_resolves_to_provider_implementing_protocol(self):
        prov = dispatch.get_object_store_provider("s3")
        assert isinstance(prov, S3ObjectStoreProvider)
        assert isinstance(prov, ObjectStoreProvider)  # runtime_checkable Protocol

    def test_gcs_known_but_unimplemented_raises_provider_unavailable(self):
        try:
            dispatch.get_object_store_provider("gcs")
            assert False, "expected ProviderUnavailable"
        except dispatch.ProviderUnavailable:
            pass

    def test_unknown_platform_raises_unknown_platform(self):
        try:
            dispatch.get_object_store_provider("not-a-store")
            assert False, "expected UnknownPlatform"
        except dispatch.UnknownPlatform:
            pass

    def test_sql_resolvers_still_reject_s3_zero_blast_radius(self):
        # s3 must NOT leak into the SQL dispatch family.
        for getter in (dispatch.get_query_executor, dispatch.get_deployment_provider,
                       dispatch.get_connection_provider):
            try:
                getter("s3")
                assert False, f"{getter.__name__}(s3) should raise UnknownPlatform"
            except dispatch.UnknownPlatform:
                pass


class TestValidateConfig:
    def test_missing_bucket_is_an_error(self):
        rep = S3ObjectStoreProvider().validate_config({"extra_config": {}}, "direct:x")
        assert rep.valid is False
        assert any("bucket" in e for e in rep.errors)

    def test_bucket_ok_but_no_public_endpoint_warns(self):
        rep = S3ObjectStoreProvider().validate_config(
            {"username": "k", "host": "seaweedfs",
             "extra_config": {"bucket": "b", "endpoint_url": "http://seaweedfs:8333"}},
            "direct:secret",
        )
        assert rep.valid is True
        assert any("public_endpoint_url" in w for w in rep.warnings)

    def test_no_credentials_warns_but_valid(self):
        rep = S3ObjectStoreProvider().validate_config(
            {"extra_config": {"bucket": "b", "public_endpoint_url": "http://x"}}, "",
        )
        assert rep.valid is True
        assert rep.warnings  # credential-chain warnings


class TestCollectForObjectStore:
    def test_allowlist_pairs_parquet_with_manifest_and_excludes_the_rest(self, tmp_path, monkeypatch):
        import pyarrow as pa
        import pyarrow.parquet as pq
        code = "collecttest"
        base = tmp_path / code
        (base / "exports").mkdir(parents=True)
        (base / "serving" / "lakehouse" / "data").mkdir(parents=True)
        tbl = pa.table({"id": [1]})
        pq.write_table(tbl, base / "exports" / "public__orders.parquet")
        (base / "exports" / "public__orders__manifest.json").write_text("{}")
        pq.write_table(tbl, base / "serving" / "lakehouse" / "data" / "m.parquet")
        # decoys that MUST be excluded (ADR-14 D9)
        (base / "serving" / "lakehouse" / "catalog.duckdb").write_bytes(b"decoy")
        (base / "exports" / "stray.db").write_bytes(b"decoy")
        (base / "exports" / "notes.txt").write_text("x")
        monkeypatch.setattr(serving_package, "BASE_PROJECT_DIR", tmp_path)

        arts = serving_package.collect_for_object_store(code)
        rels = sorted(a["rel_path"] for a in arts)
        assert rels == [
            "exports/public__orders.parquet",
            "exports/public__orders__manifest.json",
            "lakehouse/m.parquet",   # parquet with no paired manifest still collected
        ]
        # nothing binary-but-forbidden leaked
        assert not any("duckdb" in r or r.endswith(".db") or r.endswith(".txt") for r in rels)

    def test_empty_when_no_producer_dirs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(serving_package, "BASE_PROJECT_DIR", tmp_path)
        assert serving_package.collect_for_object_store("does-not-exist") == []


class TestArtifactStoreBindingCrud:
    def test_bind_get_delete_roundtrip(self, client):
        pid = _mk_project("os-bind-1")
        cid = _mk_s3_connection(client, name="os-bind-conn-1")
        # unbound → bound:false
        assert client.get(f"/api/projects/{pid}/artifact-store-binding").json()["bound"] is False
        # PUT
        r = client.put(f"/api/projects/{pid}/artifact-store-binding", json={
            "connection_id": cid, "bucket": "data-workbench", "project_prefix": "os-bind-1/"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["bound"] is True and body["bucket"] == "data-workbench"
        assert body["platform_type"] == "s3"
        # GET reflects it
        assert client.get(f"/api/projects/{pid}/artifact-store-binding").json()["project_prefix"] == "os-bind-1/"
        # DELETE
        assert client.delete(f"/api/projects/{pid}/artifact-store-binding").json()["deleted"] is True
        assert client.get(f"/api/projects/{pid}/artifact-store-binding").json()["bound"] is False

    def test_bind_non_object_store_connection_is_422(self, client):
        pid = _mk_project("os-bind-2")
        r = client.post("/api/connections", json={
            "connection_name": "pg-not-store", "platform_type": "postgres",
            "host": "localhost", "port": 5432, "database": "db", "username": "u", "password": "p"})
        pg_cid = r.json()["id"]
        rr = client.put(f"/api/projects/{pid}/artifact-store-binding", json={"connection_id": pg_cid})
        assert rr.status_code == 422
        assert "not an object store" in rr.json()["detail"]


class TestPushToStorageGuards:
    def test_push_without_binding_is_400(self, client):
        pid = _mk_project("os-push-1")
        r = client.post(f"/api/projects/{pid}/serving/push-to-storage")
        assert r.status_code == 400
        assert "no object-store binding" in r.json()["detail"]

    def test_push_with_binding_but_no_artifacts_is_empty(self, client):
        # Hermetic: with a bucket resolved but ZERO artifacts on disk, publish
        # returns status 'empty' BEFORE touching the object store (no network).
        pid = _mk_project("os-push-2")
        cid = _mk_s3_connection(client, name="os-push-conn-2")
        client.put(f"/api/projects/{pid}/artifact-store-binding",
                   json={"connection_id": cid, "bucket": "data-workbench"})
        r = client.post(f"/api/projects/{pid}/serving/push-to-storage")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "empty"
        assert body["object_count"] == 0
        # status endpoint shows the configured binding + the empty last run
        st = client.get(f"/api/projects/{pid}/serving/storage-status").json()
        assert st["configured"] is True
        assert st["last_run"]["status"] == "empty"


# ── Phase 5 (refined): reachability-gated transfer staging ────────────────────
import json as _json


def _mk_transfer_setup(*, code, host, port, extra, bucket="data-workbench", prefix=""):
    """Create a Project + s3 PlatformConnection + ProjectArtifactStoreBinding directly
    (no API/network) and return (project_obj, session). Caller closes the session."""
    s = Session(engine)
    p = Project(project_code=code, name=code, archetype="dpe-cf")
    s.add(p); s.commit(); s.refresh(p)
    conn = PlatformConnection(
        connection_name=f"{code}-conn", platform_type="s3",
        host=host, port=port, database=bucket,
        username="workbench", secret_ref="direct:sk",
        extra_config_json=_json.dumps(extra),
    )
    s.add(conn); s.commit(); s.refresh(conn)
    s.add(ProjectArtifactStoreBinding(
        project_id=p.id, connection_id=conn.id, bucket=bucket, project_prefix=prefix,
    ))
    s.commit()
    return p, s


class TestTransferStagingMode:
    """`_s3_staging_env` picks 'staging' only for a warehouse target on a
    warehouse-reachable store; everything else is the safe 'artifact' path."""

    def test_no_binding_returns_none(self):
        with Session(engine) as s:
            p = Project(project_code="ts-none", name="x", archetype="dpe-cf")
            s.add(p); s.commit(); s.refresh(p)
            assert transfer_execution._s3_staging_env(p, s, "rid", target_platform="databricks") is None

    def test_local_fixture_warehouse_is_artifact(self):
        p, s = _mk_transfer_setup(code="ts-fix", host="seaweedfs", port=8333,
                                  extra={"bucket": "data-workbench"})
        try:
            env = transfer_execution._s3_staging_env(p, s, "rid", target_platform="databricks")
            assert env["WB_S3_MODE"] == "artifact"
            assert env["WB_S3_ENDPOINT_URL"] == "http://seaweedfs:8333"
        finally:
            s.close()

    def test_aws_warehouse_is_staging_with_blank_endpoint(self):
        p, s = _mk_transfer_setup(code="ts-aws", host="aws", port=443, extra={})
        try:
            env = transfer_execution._s3_staging_env(p, s, "rid", target_platform="snowflake")
            assert env["WB_S3_MODE"] == "staging"
            assert env["WB_S3_ENDPOINT_URL"] == ""   # real AWS → default endpoints
        finally:
            s.close()

    def test_relational_target_is_always_artifact(self):
        # Even a reachable (AWS) store is 'artifact' for a relational target —
        # postgres/mysql don't COPY from S3.
        p, s = _mk_transfer_setup(code="ts-rel", host="aws", port=443, extra={})
        try:
            env = transfer_execution._s3_staging_env(p, s, "rid", target_platform="postgres")
            assert env["WB_S3_MODE"] == "artifact"
        finally:
            s.close()

    def test_explicit_warehouse_reachable_flag_enables_staging(self):
        p, s = _mk_transfer_setup(code="ts-flag", host="minio.corp.internal", port=9000,
                                  extra={"bucket": "data-workbench", "warehouse_reachable": True})
        try:
            env = transfer_execution._s3_staging_env(p, s, "rid", target_platform="databricks")
            assert env["WB_S3_MODE"] == "staging"
        finally:
            s.close()

    def test_use_ssl_string_false_is_honoured(self):
        p, s = _mk_transfer_setup(code="ts-ssl", host="seaweedfs", port=8333,
                                  extra={"bucket": "data-workbench", "use_ssl": "false"})
        try:
            env = transfer_execution._s3_staging_env(p, s, "rid", target_platform="databricks")
            assert env["WB_S3_USE_SSL"] == "0"
            assert env["WB_S3_ENDPOINT_URL"].startswith("http://")
        finally:
            s.close()


class TestTransferPresignAndAudit:
    def test_presign_uses_public_endpoint(self):
        # boto3 signs offline — no network. The presigned URL must target the
        # PUBLIC endpoint, not the internal one the runner writes to.
        p, s = _mk_transfer_setup(
            code="ts-presign", host="seaweedfs", port=8333,
            extra={"bucket": "data-workbench",
                   "endpoint_url": "http://seaweedfs:8333",
                   "public_endpoint_url": "http://localhost:9000"},
        )
        try:
            s3_env = transfer_execution._s3_staging_env(p, s, "rid", target_platform="databricks")
            urls = transfer_execution._presign_s3_transfer_files(
                project=p, session=s, s3_env=s3_env,
                s3_file_uris=["s3://data-workbench/ts-presign/runs/rid/t/x.parquet"],
            )
            assert urls, "expected a presigned URL"
            (url,) = urls.values()
            assert "localhost:9000" in url
            assert "seaweedfs:8333" not in url
        finally:
            s.close()

    def test_record_stores_scheme_less_run_prefix(self):
        p, s = _mk_transfer_setup(code="ts-audit", host="seaweedfs", port=8333,
                                  extra={"bucket": "data-workbench"}, prefix="ts-audit/")
        try:
            s3_env = transfer_execution._s3_staging_env(p, s, "rid42", target_platform="databricks")
            transfer_execution._record_s3_transfer_run(
                project=p, session=s, run_id="rid42", s3_env=s3_env,
                s3_run_prefix="s3://data-workbench/ts-audit/runs/rid42",  # full URI from runner metric
                s3_file_uris=["s3://data-workbench/ts-audit/runs/rid42/t/x.parquet"],
                actor="tester",
            )
            row = s.exec(select(ArtifactPublishRun).where(
                ArtifactPublishRun.project_id == p.id)).one()
            assert row.run_prefix == "ts-audit/runs/rid42"   # scheme-less, matches publish path
            assert row.object_count == 1
        finally:
            s.close()
