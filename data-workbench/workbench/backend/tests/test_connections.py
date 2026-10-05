"""Tests for connection management and source namespace browsing.

Covers:
  - PlatformConnection CRUD via the REST router
  - SourceBinding upsert / delete
  - Connection test endpoint (no live DB — verifies dispatch only)
  - Secret resolver
  - resolve_source_connection_ref routing logic
  - list_source_namespaces / list_source_tables dispatch (mocked provider)
"""
from __future__ import annotations

import json
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

# conftest.py sets WB_DATABASE_URL + WB_MCP_ALLOW_INSECURE before imports.

from workbench.backend.main import app
from workbench.backend.database import engine
from workbench.backend.models import PlatformConnection, SourceBinding, Project

# Module-level client — avoids re-entering the lifespan context which would
# try to start the MCP StreamableHTTPSessionManager a second time.
_client = TestClient(app)


@pytest.fixture(scope="module")
def client():
    return _client


@pytest.fixture
def session():
    with Session(engine) as s:
        yield s


@pytest.fixture(autouse=True)
def _clean_connection_tables():
    """Remove PlatformConnection + SourceBinding rows before each test.

    The shared conftest._clean_tables only clears Project/ProductRequest.
    Without this, SQLite recycles project IDs and leftover SourceBinding rows
    appear to belong to newly-created projects — causing false positives.
    """
    with Session(engine) as s:
        for row in s.exec(select(SourceBinding)).all():
            s.delete(row)
        for row in s.exec(select(PlatformConnection)).all():
            s.delete(row)
        s.commit()
    yield


def _make_project(session, code="test-proj") -> Project:
    p = session.exec(
        select(Project).where(Project.project_code == code)
    ).first()
    if p is None:
        p = Project(
            project_code=code,
            name="Test Project",
            pg_connection="postgresql://user:pass@localhost/testdb",
        )
        session.add(p)
        session.commit()
        session.refresh(p)
    return p


# ══════════════════════════════════════════════════════════════════════════════
# 1. PlatformConnection CRUD
# ══════════════════════════════════════════════════════════════════════════════

class TestConnectionCRUD:
    def test_list_connections_empty(self, client):
        r = client.get("/api/connections")
        assert r.status_code == 200
        data = r.json()
        assert "connections" in data
        assert "count" in data

    def test_create_connection_postgres(self, client):
        r = client.post("/api/connections", json={
            "connection_name": "test-pg-conn",
            "platform_type": "postgres",
            "host": "pg.internal",
            "port": 5432,
            "database": "mydb",
            "username": "admin",
            "secret_ref": "env:PG_PASSWORD",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["connection_name"] == "test-pg-conn"
        assert data["platform_type"] == "postgres"
        assert "password" not in data         # never returned
        assert data["secret_ref"] == "env:PG_PASSWORD"

    def test_create_connection_mysql(self, client):
        r = client.post("/api/connections", json={
            "connection_name": "test-mysql-conn",
            "platform_type": "mysql",
            "host": "mysql.internal",
            "port": 3306,
            "database": "warehouse",
            "username": "reader",
            "secret_ref": "env:MYSQL_PASSWORD",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["platform_type"] == "mysql"

    def test_create_connection_unknown_platform_rejected(self, client):
        r = client.post("/api/connections", json={
            "connection_name": "bad-conn",
            "platform_type": "teradata",
            "host": "teradata.internal",
            "database": "db",
        })
        assert r.status_code == 422

    def test_create_connection_duplicate_name_rejected(self, client):
        client.post("/api/connections", json={
            "connection_name": "dupe-conn",
            "platform_type": "postgres",
            "host": "h",
            "database": "d",
        })
        r = client.post("/api/connections", json={
            "connection_name": "dupe-conn",
            "platform_type": "postgres",
            "host": "h2",
            "database": "d2",
        })
        assert r.status_code == 409

    def test_get_connection(self, client):
        cr = client.post("/api/connections", json={
            "connection_name": "get-test-conn",
            "platform_type": "mysql",
            "host": "h",
            "database": "d",
        })
        conn_id = cr.json()["id"]
        r = client.get(f"/api/connections/{conn_id}")
        assert r.status_code == 200
        assert r.json()["id"] == conn_id

    def test_get_connection_not_found(self, client):
        r = client.get("/api/connections/99999")
        assert r.status_code == 404

    def test_update_connection(self, client):
        cr = client.post("/api/connections", json={
            "connection_name": "update-test-conn",
            "platform_type": "postgres",
            "host": "old-host",
            "database": "db",
        })
        conn_id = cr.json()["id"]
        r = client.put(f"/api/connections/{conn_id}", json={"host": "new-host"})
        assert r.status_code == 200
        assert r.json()["host"] == "new-host"

    def test_delete_connection(self, client):
        cr = client.post("/api/connections", json={
            "connection_name": "del-test-conn",
            "platform_type": "postgres",
            "host": "h",
            "database": "d",
        })
        conn_id = cr.json()["id"]
        r = client.delete(f"/api/connections/{conn_id}")
        assert r.status_code == 200
        assert r.json()["deleted"] is True
        assert client.get(f"/api/connections/{conn_id}").status_code == 404

    def test_delete_bound_connection_rejected(self, client, session):
        project = _make_project(session, "bind-block-proj")
        cr = client.post("/api/connections", json={
            "connection_name": "bound-conn",
            "platform_type": "mysql",
            "host": "h",
            "database": "d",
        })
        conn_id = cr.json()["id"]
        client.put(f"/api/projects/{project.id}/source-binding", json={
            "connection_id": conn_id,
        })
        r = client.delete(f"/api/connections/{conn_id}")
        assert r.status_code == 409


# ══════════════════════════════════════════════════════════════════════════════
# 2. SourceBinding
# ══════════════════════════════════════════════════════════════════════════════

class TestSourceBinding:
    def test_no_binding_returns_bound_false(self, client, session):
        project = _make_project(session, "unbound-proj")
        r = client.get(f"/api/projects/{project.id}/source-binding")
        assert r.status_code == 200
        assert r.json()["bound"] is False

    def test_upsert_binding(self, client, session):
        project = _make_project(session, "binding-proj")
        cr = client.post("/api/connections", json={
            "connection_name": "binding-mysql",
            "platform_type": "mysql",
            "host": "h",
            "database": "d",
        })
        conn_id = cr.json()["id"]
        r = client.put(f"/api/projects/{project.id}/source-binding", json={
            "connection_id": conn_id,
            "default_schema": "analytics",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["bound"] is True
        assert data["platform_type"] == "mysql"
        assert data["default_schema"] == "analytics"

    def test_get_binding_after_upsert(self, client, session):
        project = _make_project(session, "getbinding-proj")
        cr = client.post("/api/connections", json={
            "connection_name": "getbinding-mysql",
            "platform_type": "mysql",
            "host": "h",
            "database": "d",
        })
        conn_id = cr.json()["id"]
        client.put(f"/api/projects/{project.id}/source-binding", json={"connection_id": conn_id})
        r = client.get(f"/api/projects/{project.id}/source-binding")
        assert r.status_code == 200
        assert r.json()["bound"] is True
        assert r.json()["connection_id"] == conn_id

    def test_delete_binding(self, client, session):
        project = _make_project(session, "delbinding-proj")
        cr = client.post("/api/connections", json={
            "connection_name": "delbinding-mysql",
            "platform_type": "mysql",
            "host": "h",
            "database": "d",
        })
        conn_id = cr.json()["id"]
        client.put(f"/api/projects/{project.id}/source-binding", json={"connection_id": conn_id})
        r = client.delete(f"/api/projects/{project.id}/source-binding")
        assert r.status_code == 200
        assert client.get(f"/api/projects/{project.id}/source-binding").json()["bound"] is False


# ══════════════════════════════════════════════════════════════════════════════
# 3. Connection test endpoint
# ══════════════════════════════════════════════════════════════════════════════

class TestConnectionTestEndpoint:
    def test_test_unusable_object_store_returns_ok_false(self, client):
        # gcs/azure_adls are registered object stores whose artifact_publish
        # capability is still unsupported — is_usable() requires PREVIEW or
        # CERTIFIED, so the test endpoint reports not usable without a live probe.
        # (s3 advanced to artifact_publish: preview — ADR-14 — so it is exercised
        # against the live SeaweedFS fixture out-of-band, not in this hermetic suite.)
        cr = client.post("/api/connections", json={
            "connection_name": "gcs-conn",
            "platform_type": "gcs",
            "host": "storage.googleapis.com",
            "database": "MYBUCKET",
        })
        conn_id = cr.json()["id"]
        r = client.post(f"/api/connections/{conn_id}/test", json={})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        assert "not usable" in data["error"].lower()
        assert "artifact_publish" in data["error"]

    def test_test_postgres_no_live_db_returns_warning(self, client):
        # Postgres IS usable (certified), so probe() runs.
        # Without a live DB it returns a warning, not a hard error.
        cr = client.post("/api/connections", json={
            "connection_name": "pg-probe-conn",
            "platform_type": "postgres",
            "host": "127.0.0.1",
            "port": 19999,    # nothing listening on this port
            "database": "nonexistent",
            "username": "nobody",
        })
        conn_id = cr.json()["id"]
        r = client.post(f"/api/connections/{conn_id}/test", json={})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        assert len(data["warnings"]) > 0


# ══════════════════════════════════════════════════════════════════════════════
# 4. Secret resolver
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# 3b. build_connection_string helper
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildConnectionString:
    def test_postgres_builds_dsn_from_structured_ref(self):
        from workbench.backend.routers.connections import build_connection_string
        ref = {"host": "host", "port": 5432, "database": "db",
               "username": "user", "resolved_password": "pw"}
        assert build_connection_string("postgres", ref) == "postgresql://user:pw@host:5432/db"

    def test_postgresql_alias_also_works(self):
        from workbench.backend.routers.connections import build_connection_string
        ref = {"host": "h", "port": 5432, "database": "d",
               "username": "x", "resolved_password": "y"}
        assert build_connection_string("postgresql", ref) == "postgresql://x:y@h:5432/d"

    def test_transient_dsn_key_short_circuits(self):
        # The sql_executor facade threads a transient DSN under a neutral `dsn`
        # key (there is no pg_connection shape) — it is returned verbatim.
        from workbench.backend.routers.connections import build_connection_string
        assert build_connection_string("postgres", {"dsn": "postgresql://u:p@h/d"}) == "postgresql://u:p@h/d"

    def test_mysql_builds_dsn_from_parts(self):
        from workbench.backend.routers.connections import build_connection_string
        ref = {
            "host": "db.example.com", "port": 3306,
            "database": "mydb", "username": "reader",
            "resolved_password": "s3cr3t",
        }
        assert build_connection_string("mysql", ref) == "mysql://reader:s3cr3t@db.example.com:3306/mydb"

    def test_mysql_no_credentials(self):
        from workbench.backend.routers.connections import build_connection_string
        ref = {"host": "h", "port": 3306, "database": "d", "username": "", "resolved_password": ""}
        result = build_connection_string("mysql", ref)
        assert result.startswith("mysql://h:")
        assert "d" in result

    def test_snowflake_builds_dsn_with_warehouse_role_schema(self):
        from urllib.parse import urlparse, parse_qs
        from workbench.backend.routers.connections import build_connection_string
        ref = {
            "host": "myorg-account_region", "database": "DWB_SERVING_DB",
            "username": "NEYDE", "resolved_password": "pat/tok+en=x",
            "extra_config": {"warehouse": "COMPUTE_WH", "role": "MY_ROLE", "schema": "PUBLIC"},
        }
        dsn = build_connection_string("snowflake", ref)
        p = urlparse(dsn)
        assert p.scheme == "snowflake"
        assert p.hostname == "myorg-account_region"      # account identifier, hyphen+underscore preserved
        assert (p.path or "").lstrip("/") == "DWB_SERVING_DB"
        qs = {k: v[0] for k, v in parse_qs(p.query).items()}
        assert qs == {"warehouse": "COMPUTE_WH", "role": "MY_ROLE", "schema": "PUBLIC"}
        # The generic fallback would have dropped warehouse+role entirely.
        assert "warehouse=COMPUTE_WH" in dsn and "role=MY_ROLE" in dsn

    def test_snowflake_schema_falls_back_to_default_schema(self):
        from urllib.parse import urlparse, parse_qs
        from workbench.backend.routers.connections import build_connection_string
        ref = {
            "host": "acct", "database": "DB", "username": "u", "resolved_password": "p",
            "default_schema": "ANALYTICS",
            "extra_config": {"warehouse": "WH"},
        }
        dsn = build_connection_string("snowflake", ref)
        qs = {k: v[0] for k, v in parse_qs(urlparse(dsn).query).items()}
        assert qs.get("schema") == "ANALYTICS"

    def test_discovery_skill_by_platform_contains_mysql(self):
        from workbench.backend.routers.connections import DISCOVERY_SKILL_BY_PLATFORM
        assert "mysql" in DISCOVERY_SKILL_BY_PLATFORM
        assert DISCOVERY_SKILL_BY_PLATFORM["mysql"] == "data-discovery-mysql"

    def test_profiling_skill_by_platform_contains_mysql(self):
        from workbench.backend.routers.connections import PROFILING_SKILL_BY_PLATFORM
        assert "mysql" in PROFILING_SKILL_BY_PLATFORM
        assert PROFILING_SKILL_BY_PLATFORM["mysql"] == "data-profiling-mysql"

    def test_stage_execution_imports_from_connections(self):
        # Regression guard: the shared constants must be re-exported, not redefined.
        from workbench.backend.stage_execution import _PLATFORM_SKILL_OVERRIDES
        assert _PLATFORM_SKILL_OVERRIDES["data_discovery"]["mysql"] == "data-discovery-mysql"
        assert _PLATFORM_SKILL_OVERRIDES["data_profiling"]["mysql"] == "data-profiling-mysql"


# ══════════════════════════════════════════════════════════════════════════════
# 3c. Tier-0 secret containment (never store/return a secret unmasked)
# ══════════════════════════════════════════════════════════════════════════════

class TestSecretContainment:
    def test_literal_password_stored_contained_and_masked(self, client):
        r = client.post("/api/connections", json={
            "connection_name": "sf-pat", "platform_type": "snowflake",
            "host": "acct", "port": 443, "database": "DB", "username": "NEYDE",
            "password": "super-secret-pat-token",
            "extra_config": {"warehouse": "COMPUTE_WH"},
        })
        assert r.status_code == 200
        data = r.json()
        assert data["has_password"] is True
        # The stored secret is NEVER echoed — neither as `password` nor via secret_ref.
        assert "password" not in data
        assert "super-secret-pat-token" not in json.dumps(data)
        assert data["secret_ref"] == "***"
        # And it stays masked on subsequent reads (list + get).
        got = client.get(f"/api/connections/{data['id']}").json()
        assert got["secret_ref"] == "***"
        assert "super-secret-pat-token" not in json.dumps(got)
        listed = client.get("/api/connections").json()["connections"]
        assert all("super-secret-pat-token" not in json.dumps(c) for c in listed)

    def test_non_reference_secret_ref_is_contained_not_echoed(self, client):
        # A literal accidentally posted to secret_ref must be contained, not stored raw.
        r = client.post("/api/connections", json={
            "connection_name": "raw-in-secretref", "platform_type": "postgres",
            "host": "h", "database": "d", "username": "u",
            "secret_ref": "a-raw-literal-password",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["has_password"] is True
        assert data["secret_ref"] == "***"
        assert "a-raw-literal-password" not in json.dumps(data)

    def test_env_reference_is_echoed_as_pointer(self, client):
        r = client.post("/api/connections", json={
            "connection_name": "env-ref", "platform_type": "postgres",
            "host": "h", "database": "d", "username": "u",
            "secret_ref": "env:PG_PASSWORD",
        })
        assert r.status_code == 200
        assert r.json()["secret_ref"] == "env:PG_PASSWORD"   # references are safe pointers
        assert r.json()["has_password"] is True

    def test_update_password_clears_and_recontains(self, client):
        cid = client.post("/api/connections", json={
            "connection_name": "upd-pw", "platform_type": "postgres",
            "host": "h", "database": "d", "username": "u", "secret_ref": "env:OLD",
        }).json()["id"]
        r = client.put(f"/api/connections/{cid}", json={"password": "new-literal-pw"})
        assert r.status_code == 200
        assert r.json()["secret_ref"] == "***"
        assert "new-literal-pw" not in json.dumps(r.json())

    def test_extra_config_round_trips(self, client):
        r = client.post("/api/connections", json={
            "connection_name": "sf-extra", "platform_type": "snowflake",
            "host": "acct", "database": "DB", "username": "u", "password": "x",
            "extra_config": {"warehouse": "WH", "role": "R", "schema": "S"},
        })
        assert r.json()["extra_config"] == {"warehouse": "WH", "role": "R", "schema": "S"}


class TestSecretResolver:
    def test_env_prefix_reads_env_var(self, monkeypatch):
        from workbench.backend.platform.secrets import resolve_secret
        monkeypatch.setenv("WB_TEST_SECRET", "hunter2")
        assert resolve_secret("env:WB_TEST_SECRET") == "hunter2"

    def test_empty_ref_returns_empty(self):
        from workbench.backend.platform.secrets import resolve_secret
        assert resolve_secret("") == ""
        assert resolve_secret(None) == ""

    def test_missing_env_var_returns_empty(self, monkeypatch):
        from workbench.backend.platform.secrets import resolve_secret
        monkeypatch.delenv("WB_DEFINITELY_NOT_SET", raising=False)
        assert resolve_secret("env:WB_DEFINITELY_NOT_SET") == ""

    def test_unknown_format_returns_literal(self):
        # Unrecognised prefixes are treated as a literal password so users can
        # paste a raw value without the direct: prefix (see secrets.resolve_secret).
        from workbench.backend.platform.secrets import resolve_secret
        assert resolve_secret("vault:/secret/mypath") == "vault:/secret/mypath"


# ══════════════════════════════════════════════════════════════════════════════
# 5. resolve_source_connection_ref routing
# ══════════════════════════════════════════════════════════════════════════════

class TestResolveSourceConnectionRef:
    def test_no_binding_returns_unresolved_structured_ref(self, session):
        # A project with no SourceBinding resolves to an empty STRUCTURED ref —
        # NOT the retired {"pg_connection": ""} sentinel shape. Callers detect the
        # missing `host` as unresolved.
        from workbench.backend.routers.connections import resolve_source_connection_ref
        project = _make_project(session, "unbound-proj")
        platform_type, ref = resolve_source_connection_ref(project, session)
        assert platform_type == "postgres"
        assert "pg_connection" not in ref  # the shape no longer exists
        assert not ref.get("host")         # unresolved

    def test_with_binding_returns_bound_platform(self, client, session):
        from workbench.backend.routers.connections import resolve_source_connection_ref
        project = _make_project(session, "bound-mysql-proj")
        cr = client.post("/api/connections", json={
            "connection_name": "resolve-mysql",
            "platform_type": "mysql",
            "host": "mysql.internal",
            "port": 3306,
            "database": "mydb",
            "username": "reader",
            "secret_ref": "env:MYSQL_PW",
        })
        conn_id = cr.json()["id"]
        client.put(f"/api/projects/{project.id}/source-binding", json={
            "connection_id": conn_id,
            "default_schema": "analytics",
        })
        # Refresh project from DB (binding was added after it was fetched).
        session.expire(project)
        project = session.get(Project, project.id)
        platform_type, ref = resolve_source_connection_ref(project, session)
        assert platform_type == "mysql"
        assert ref["host"] == "mysql.internal"
        assert ref["database"] == "mydb"
        assert "password" not in ref             # resolved_password key only
        assert "default_schema" in ref


# ══════════════════════════════════════════════════════════════════════════════
# 6. list_source_namespaces / list_source_tables — mock provider
# ══════════════════════════════════════════════════════════════════════════════

class TestNamespaceBrowserMCP:
    """Verifies dispatch logic by calling MCP tool functions directly
    (bypassing the HTTP transport, same pattern as test_acceptance_gate.py)."""

    def test_list_namespaces_unknown_project_returns_error(self):
        from workbench.backend.mcp_server import list_source_namespaces
        result = list_source_namespaces("no-such-project")
        assert "error" in result

    def test_list_tables_unknown_project_returns_error(self):
        from workbench.backend.mcp_server import list_source_tables
        result = list_source_tables("no-such-project", "public")
        assert "error" in result

    def test_list_namespaces_legacy_postgres_project(self, session):
        # A project with pg_connection but no SourceBinding falls back to
        # Postgres — the provider runs list_namespaces.  With a bad DSN it
        # returns a result with an empty namespaces list (the provider swallows
        # the connection error and returns []).
        project = _make_project(session, "ns-pg-proj")
        from workbench.backend.mcp_server import list_source_namespaces
        result = list_source_namespaces("ns-pg-proj")
        # Should return a dict with platform_type, not crash.
        assert isinstance(result, dict)
        assert result.get("platform_type") == "postgres"
        assert "namespaces" in result

    def test_list_namespaces_dispatches_to_provider(self, client, session):
        # Register a MySQL binding and verify the result says mysql.
        project = _make_project(session, "ns-mysql-proj")
        cr = client.post("/api/connections", json={
            "connection_name": "ns-mysql-conn",
            "platform_type": "mysql",
            "host": "127.0.0.1",
            "port": 19999,  # nothing listening
            "database": "db",
            "username": "u",
        })
        conn_id = cr.json()["id"]
        client.put(f"/api/projects/{project.id}/source-binding",
                   json={"connection_id": conn_id})
        from workbench.backend.mcp_server import list_source_namespaces
        result = list_source_namespaces("ns-mysql-proj")
        assert isinstance(result, dict)
        # With no live DB the provider swallows the error and returns [].
        assert result.get("platform_type") == "mysql"
        assert "namespaces" in result


# ══════════════════════════════════════════════════════════════════════════════
# 7. _resolve_platform_context — stage routing logic
# ══════════════════════════════════════════════════════════════════════════════

class TestResolvePlatformContext:
    """Unit tests for the _resolve_platform_context helper in stage_execution.py.

    This is the routing layer that selects the correct discovery skill and
    builds a non-Postgres connection string when a SourceBinding exists.
    """

    def test_non_routed_stage_returns_none(self, session):
        from workbench.backend.stage_execution import _resolve_platform_context
        project = _make_project(session, "ctx-unrouted-proj")
        result = _resolve_platform_context("serving_virtual_view", project, session)
        assert result is None

    def test_routed_stage_no_binding_returns_none(self, session):
        from workbench.backend.stage_execution import _resolve_platform_context
        project = _make_project(session, "ctx-nobinding-proj")
        result = _resolve_platform_context("data_discovery", project, session)
        assert result is None

    def test_profiling_stage_also_routes_to_mysql(self, client, session):
        from workbench.backend.stage_execution import _resolve_platform_context
        project = _make_project(session, "ctx-prof-mysql-proj")
        cr = client.post("/api/connections", json={
            "connection_name": "ctx-prof-mysql-conn",
            "platform_type": "mysql",
            "host": "mysql.internal",
            "port": 3306,
            "database": "warehouse",
            "username": "reader",
        })
        conn_id = cr.json()["id"]
        client.put(f"/api/projects/{project.id}/source-binding",
                   json={"connection_id": conn_id})
        session.expire_all()
        project = session.get(Project, project.id)

        result = _resolve_platform_context("data_profiling", project, session)
        assert result is not None
        assert result["platform_type"] == "mysql"
        assert result["skill_override"] == "data-profiling-mysql"
        assert result["connection_string"].startswith("mysql://")

    def test_mysql_binding_returns_skill_and_dsn(self, client, session):
        from workbench.backend.stage_execution import _resolve_platform_context
        project = _make_project(session, "ctx-mysql-proj")
        cr = client.post("/api/connections", json={
            "connection_name": "ctx-mysql-conn",
            "platform_type": "mysql",
            "host": "mysql.internal",
            "port": 3306,
            "database": "warehouse",
            "username": "reader",
            "secret_ref": "",
        })
        conn_id = cr.json()["id"]
        client.put(f"/api/projects/{project.id}/source-binding",
                   json={"connection_id": conn_id, "default_schema": "analytics"})

        # Refresh project + session so the binding is visible.
        session.expire_all()
        project = session.get(Project, project.id)
        result = _resolve_platform_context("data_discovery", project, session)

        assert result is not None
        assert result["platform_type"] == "mysql"
        assert result["skill_override"] == "data-discovery-mysql"
        assert result["connection_string"].startswith("mysql://")
        assert "mysql.internal" in result["connection_string"]
        assert "warehouse" in result["connection_string"]

    def test_mysql_connection_string_format(self, client, session):
        from workbench.backend.stage_execution import _resolve_platform_context
        import os
        project = _make_project(session, "ctx-dsn-proj")
        os.environ["WB_TEST_MYSQL_PW"] = "s3cr3t"
        cr = client.post("/api/connections", json={
            "connection_name": "ctx-dsn-conn",
            "platform_type": "mysql",
            "host": "db.example.com",
            "port": 3306,
            "database": "mydb",
            "username": "admin",
            "secret_ref": "env:WB_TEST_MYSQL_PW",
        })
        conn_id = cr.json()["id"]
        client.put(f"/api/projects/{project.id}/source-binding",
                   json={"connection_id": conn_id})

        session.expire_all()
        project = session.get(Project, project.id)
        result = _resolve_platform_context("data_discovery", project, session)
        os.environ.pop("WB_TEST_MYSQL_PW", None)

        assert result is not None
        # password appears in the DSN (same model as the existing pg_connection path)
        assert result["connection_string"] == "mysql://admin:s3cr3t@db.example.com:3306/mydb"

    def test_profiling_stage_in_routed_stages_set(self):
        from workbench.backend.stage_execution import _PLATFORM_ROUTED_STAGES
        assert "data_discovery" in _PLATFORM_ROUTED_STAGES
        assert "data_profiling" in _PLATFORM_ROUTED_STAGES

    def test_build_prompt_uses_platform_context_skill(self, client, session):
        from workbench.backend.stage_execution import _resolve_platform_context
        from workbench.backend.pipeline import build_prompt
        from workbench.backend.archetypes import STAGE_REGISTRY
        project = _make_project(session, "ctx-prompt-proj")
        cr = client.post("/api/connections", json={
            "connection_name": "ctx-prompt-mysql",
            "platform_type": "mysql",
            "host": "h",
            "port": 3306,
            "database": "d",
            "username": "u",
        })
        conn_id = cr.json()["id"]
        client.put(f"/api/projects/{project.id}/source-binding",
                   json={"connection_id": conn_id})
        session.expire_all()
        project = session.get(Project, project.id)

        stage_def = STAGE_REGISTRY["data_discovery"]
        ctx = _resolve_platform_context("data_discovery", project, session)
        prompt = build_prompt(
            stage_def, project,
            stage_config={"discovery_tables": "public.orders"},
            platform_context=ctx,
        )
        # The preamble should reference the mysql skill, not data-discovery.
        assert "data-discovery-mysql" in prompt
        # Tier-0 credential containment: the credential-bearing DSN must NOT appear
        # in the prompt — the placeholder renders as the "$WB_SOURCE_DSN" shell
        # reference (threaded into the SDK subprocess env), so the resolved
        # password never enters the prompt text / tool-call log / persisted log.
        assert "$WB_SOURCE_DSN" in prompt
        assert "mysql://" not in prompt
        assert ctx["connection_string"] not in prompt
