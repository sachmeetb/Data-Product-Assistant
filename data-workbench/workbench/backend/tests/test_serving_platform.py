"""Tests for platform threading in routers/serving.py deploy_virtual_view.

Coverage:
- Postgres path: the resolver returns a STRUCTURED ref; the deploy derives a
  transient DSN and threads it as WB_TARGET_DSN into the runner env
- Non-Postgres binding: rides connection_ref (WB_TARGET_* structured env)
- Consumer-aligned (any platform): resolve_read_connection_for_consumer handles
  the :CONSUMES borrow internally; callers get (platform, ref, borrowed_from)
- resolved_password never appears on disk (rides the runner env only)
- Missing Postgres connection still raises 409
"""
from __future__ import annotations

import json
import unittest.mock as mock
from contextlib import contextmanager, ExitStack
from typing import Callable

import pytest
from fastapi.testclient import TestClient

from workbench.backend.main import app
from workbench.backend.models import Project
from workbench.backend.sql_executor import DeployResult

_client = TestClient(app)

_PG_CONN = "postgresql://u:p@h:5432/db"
# The STRUCTURED Postgres ref that build_connection_string renders to _PG_CONN.
_PG_REF = {"host": "h", "port": 5432, "database": "db",
           "username": "u", "resolved_password": "p"}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_project(**_ignored) -> Project:
    # The source is resolved via the mocked resolver, not a project field.
    return Project(
        id=99,
        project_code="test-proj",
        name="Test",
        archetype="dpe-sa",
    )


def _neo4j_result(row: dict):
    """A mock neo4j Result that supports both iteration and .consume()."""
    result = mock.MagicMock()
    result.__iter__ = mock.Mock(return_value=iter([row]))
    result.consume = mock.Mock(return_value=mock.MagicMock())
    return result


def _neo4j_cm(ddl: str = 'CREATE OR REPLACE VIEW "s"."v" AS SELECT 1 AS col'):
    ns = mock.MagicMock()
    row = {
        "product_uri": "dprod:test-proj-contract",
        "ddl": ddl,
        "view_schema": "s",
        "view_names_json": '["v"]',
        "primary_view_name": "v",
    }
    # The first call to ns.run() fetches the serving definition (list(ns.run(...))).
    # Subsequent calls write audit nodes via ns.run(...).consume().
    # MagicMock's default return_value supports .consume() automatically, but
    # we need the FIRST call to be iterable with the row data.
    first_result = _neo4j_result(row)
    ns.run.side_effect = [first_result] + [mock.MagicMock()] * 20
    cm = mock.MagicMock()
    cm.__enter__ = mock.Mock(return_value=ns)
    cm.__exit__ = mock.Mock(return_value=False)
    return cm


def _ok_run():
    """A successful RunResult from the packaged runner."""
    from workbench.backend.serving_runtime import RunResult
    return RunResult(status="success", duration_ms=5,
                     metrics={"statements_executed": 1, "view_count": 1})


def _fail_run(error_class: str = "platform_not_supported"):
    from workbench.backend.serving_runtime import RunResult, RunError
    return RunResult(status="failed", duration_ms=0,
                     error=RunError(**{"class": error_class,
                                       "message": "runner reported failure"}))


@contextmanager
def _base_patches(
    project: Project,
    source_platform: str,
    conn_ref: dict,
    run_side_effect: Callable,
    borrowed_from: "str | None" = None,
):
    """Layer the patches needed to drive deploy_virtual_view in tests.

    The deploy path now assembles a package and runs its ``run.py`` via
    ``serving_runtime.execute_package_runner``; we patch that (capturing the
    subprocess ``env`` — where the platform + credentials travel) and stub
    ``assemble_view_package`` so no package is written to disk.
    """
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch(
                "workbench.backend.routers.serving._get_project_for_write",
                return_value=project,
            )
        )
        stack.enter_context(
            mock.patch(
                "workbench.backend.routers.serving.resolve_read_connection_for_consumer",
                return_value=(source_platform, conn_ref, borrowed_from),
            )
        )
        stack.enter_context(
            mock.patch(
                "workbench.backend.routers.serving._neo4j",
                return_value=_neo4j_cm(),
            )
        )
        stack.enter_context(
            mock.patch(
                "workbench.backend.serving_package.assemble_view_package",
                return_value="/tmp/fake-view-package",
            )
        )
        stack.enter_context(
            mock.patch(
                "workbench.backend.serving_runtime.execute_package_runner",
                side_effect=run_side_effect,
            )
        )
        yield stack


def _post(
    project: Project,
    source_platform: str,
    conn_ref: dict,
    run_side_effect: Callable,
    borrowed_from: "str | None" = None,
):
    """Drive POST /api/projects/99/serving/deploy and return (status, body)."""
    with _base_patches(
        project, source_platform, conn_ref, run_side_effect, borrowed_from
    ):
        resp = _client.post("/api/projects/99/serving/deploy", json={})
    ct = resp.headers.get("content-type", "")
    body = resp.json() if "application/json" in ct else {}
    return resp.status_code, body


def _capture_env(store: dict, result):
    """A run_side_effect that records the runner's env (positional pkg, args, kw)."""
    def _spy(package_dir, args, **kw):
        store["package_dir"] = package_dir
        store["args"] = args
        store["env"] = kw.get("env") or {}
        return result
    return _spy


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestDeployVirtualViewPlatformResolution:
    """The deploy path assembles a package and runs its run.py; the target
    platform + credentials travel in the runner's WB_TARGET_* env."""

    # ── Postgres fallback ────────────────────────────────────────────────────

    def test_postgres_fallback_passes_platform_postgres(self):
        cap = {}
        _post(_make_project(), "postgres", _PG_REF,
              _capture_env(cap, _ok_run()))
        assert cap["env"]["WB_TARGET_PLATFORM"] == "postgres"

    def test_postgres_fallback_passes_dsn_in_env(self):
        cap = {}
        _post(_make_project(), "postgres", _PG_REF,
              _capture_env(cap, _ok_run()))
        assert cap["env"]["WB_TARGET_DSN"] == _PG_CONN
        # DSN carries creds; no discrete password var for the pg path.
        assert "WB_TARGET_PASSWORD" not in cap["env"]

    def test_postgres_fallback_returns_200(self):
        status, _ = _post(
            _make_project(), "postgres", _PG_REF,
            _capture_env({}, _ok_run()),
        )
        assert status == 200

    # ── Non-Postgres (MySQL) binding — now a first-class runner target ─────────

    def test_mysql_binding_returns_200_not_409(self):
        """Non-Postgres platform must not raise 409 — runner result instead."""
        status, _ = _post(
            _make_project(pg_connection=""),
            "mysql",
            {"host": "mysql.example.com", "port": 3306, "username": "u",
             "database": "d", "resolved_password": "s3cr3t"},
            _capture_env({}, _ok_run()),
        )
        assert status == 200

    def test_mysql_runner_failure_error_class_propagates(self):
        _, body = _post(
            _make_project(pg_connection=""),
            "mysql",
            {"host": "mysql.example.com", "resolved_password": "pw"},
            _capture_env({}, _fail_run("sql_error")),
        )
        assert body.get("error_class") == "sql_error"

    def test_mysql_binding_passes_platform_mysql(self):
        cap = {}
        _post(
            _make_project(pg_connection=""),
            "mysql",
            {"host": "h", "resolved_password": "pw"},
            _capture_env(cap, _ok_run()),
        )
        assert cap["env"]["WB_TARGET_PLATFORM"] == "mysql"

    # ── credentials travel in env (the secure channel), never persisted ────────

    def test_secret_travels_in_runner_env(self):
        cap = {}
        _post(
            _make_project(pg_connection=""),
            "snowflake",
            {"host": "org.us-east-1", "username": "svc", "database": "DB",
             "resolved_password": "very_secret_token"},
            _capture_env(cap, _ok_run()),
        )
        # The secret rides the subprocess env (ephemeral), not connection_json/disk.
        assert cap["env"]["WB_TARGET_PASSWORD"] == "very_secret_token"

    def test_snowflake_non_secret_fields_in_env(self):
        cap = {}
        _post(
            _make_project(pg_connection=""),
            "snowflake",
            {"host": "myorg-account", "database": "ANALYTICS",
             "extra_config": {"warehouse": "WH"}, "resolved_password": "tok"},
            _capture_env(cap, _ok_run()),
        )
        assert cap["env"]["WB_TARGET_HOST"] == "myorg-account"
        assert cap["env"]["WB_TARGET_DBNAME"] == "ANALYTICS"
        assert cap["env"]["WB_TARGET_WAREHOUSE"] == "WH"

    # ── Platform string propagation ──────────────────────────────────────────

    def test_snowflake_platform_string_propagates(self):
        cap = {}
        _post(
            _make_project(pg_connection=""), "snowflake", {"host": "org"},
            _capture_env(cap, _ok_run()),
        )
        assert cap["env"]["WB_TARGET_PLATFORM"] == "snowflake"

    def test_databricks_platform_string_propagates(self):
        cap = {}
        _post(
            _make_project(pg_connection=""), "databricks",
            {"host": "adb.azuredatabricks.net"},
            _capture_env(cap, _ok_run()),
        )
        assert cap["env"]["WB_TARGET_PLATFORM"] == "databricks"

    # ── Non-Postgres platform resolves via resolve_read_connection_for_consumer ─

    def test_non_postgres_deploy_calls_resolve_read_connection_for_consumer(self):
        cap = {}
        mock_resolve = mock.Mock(
            return_value=("databricks", {"host": "adb.azuredatabricks.net"}, None)
        )
        with (
            mock.patch(
                "workbench.backend.routers.serving._get_project_for_write",
                return_value=_make_project(pg_connection=""),
            ),
            mock.patch(
                "workbench.backend.routers.serving.resolve_read_connection_for_consumer",
                mock_resolve,
            ),
            mock.patch(
                "workbench.backend.routers.serving._neo4j",
                return_value=_neo4j_cm(),
            ),
            mock.patch(
                "workbench.backend.serving_package.assemble_view_package",
                return_value="/tmp/fake-view-package",
            ),
            mock.patch(
                "workbench.backend.serving_runtime.execute_package_runner",
                side_effect=_capture_env(cap, _ok_run()),
            ),
        ):
            _client.post("/api/projects/99/serving/deploy", json={})

        mock_resolve.assert_called_once()
        assert cap["env"]["WB_TARGET_PLATFORM"] == "databricks"

    # ── Missing Postgres connection still 409 ────────────────────────────────

    def test_postgres_missing_connection_raises_409(self):
        status, _ = _post(
            _make_project(pg_connection=""),
            "postgres",
            {},
            _capture_env({}, _ok_run()),   # never reached
        )
        assert status == 409


# ── preview_engineer platform threading ───────────────────────────────────────


def _preview_post(
    project: Project,
    source_platform: str,
    conn_ref: dict,
    select_side_effect: Callable,
    borrowed_from: "str | None" = None,
    deployed: bool = True,
):
    """Drive POST /api/projects/99/serving/preview and return (status, body).

    Patches ``resolve_source_connection_for_project`` to return the
    supplied (platform, conn_ref, borrowed_from) 3-tuple — the unified
    entry point that replaced the old two-step resolve_source_connection_ref
    + resolve_pg_connection pattern.
    """
    from workbench.backend.sql_executor import SelectResult

    row = {
        "deployment_status": "deployed" if deployed else "pending",
        "product_uri": "dprod:test-proj-contract",
        "view_schema": "s",
        "view_names_json": '["v"]',
        "primary_view_name": "v",
        "datasets": [{"physicalName": "v", "uri": "dprod:col:test-proj-contract:v"}],
    }

    ns = mock.MagicMock()
    first_result = _neo4j_result(row)
    ns.run.side_effect = [first_result] + [mock.MagicMock()] * 20
    neo4j_cm = mock.MagicMock()
    neo4j_cm.__enter__ = mock.Mock(return_value=ns)
    neo4j_cm.__exit__ = mock.Mock(return_value=False)

    with (
        mock.patch(
            "workbench.backend.routers.serving._get_project",
            return_value=project,
        ),
        mock.patch(
            "workbench.backend.routers.serving.resolve_read_connection_for_consumer",
            return_value=(source_platform, conn_ref, borrowed_from),
        ),
        mock.patch(
            "workbench.backend.routers.serving._neo4j",
            return_value=neo4j_cm,
        ),
        mock.patch(
            "workbench.backend.sql_executor.execute_select",
            side_effect=select_side_effect,
        ),
    ):
        resp = _client.post("/api/projects/99/serving/preview", json={})

    ct = resp.headers.get("content-type", "")
    body = resp.json() if "application/json" in ct else {}
    return resp.status_code, body


class TestPreviewEngineerPlatformResolution:

    def test_postgres_fallback_select_called_with_pg_platform(self):
        captured = {}

        def _spy(**kw):
            captured.update(kw)
            from workbench.backend.sql_executor import SelectResult
            return SelectResult(status="ok", columns=[], rows=[])

        _preview_post(_make_project(), "postgres", _PG_REF, _spy)
        assert captured.get("platform") == "postgres"

    def test_postgres_fallback_select_called_with_empty_conn_json(self):
        captured = {}

        def _spy(**kw):
            captured.update(kw)
            from workbench.backend.sql_executor import SelectResult
            return SelectResult(status="ok", columns=[], rows=[])

        _preview_post(_make_project(), "postgres", _PG_REF, _spy)
        assert captured.get("connection_json") == "{}"

    def test_snowflake_preview_passes_platform_snowflake(self):
        captured = {}

        def _spy(**kw):
            captured.update(kw)
            from workbench.backend.sql_executor import SelectResult
            return SelectResult(
                status="failed", error_class="platform_not_supported",
                error_message="Use dbt path",
            )

        _preview_post(
            _make_project(pg_connection=""),
            "snowflake",
            {"account": "org-acct"},
            _spy,
        )
        assert captured.get("platform") == "snowflake"

    def test_snowflake_preview_resolved_password_stripped(self):
        captured = {}

        def _spy(**kw):
            captured.update(kw)
            from workbench.backend.sql_executor import SelectResult
            return SelectResult(status="failed", error_class="platform_not_supported")

        _preview_post(
            _make_project(pg_connection=""),
            "snowflake",
            {"account": "org-acct", "resolved_password": "tok3n"},
            _spy,
        )
        conn_json_str = captured.get("connection_json", "{}")
        assert "tok3n" not in conn_json_str
        assert "resolved_password" not in conn_json_str

    def test_postgres_missing_connection_raises_409_preview(self):
        status, _ = _preview_post(
            _make_project(pg_connection=""),
            "postgres",
            {},
            lambda **kw: (_ for _ in ()).throw(AssertionError("Should not be called")),
        )
        assert status == 409

    def test_non_postgres_does_not_409_preview(self):
        """Non-Postgres platform must not raise 409 on the preview endpoint."""
        def _spy(**kw):
            from workbench.backend.sql_executor import SelectResult
            return SelectResult(status="failed", error_class="platform_not_supported")

        status, _ = _preview_post(
            _make_project(pg_connection=""),
            "mysql",
            {"host": "mysql.example.com", "resolved_password": "pw"},
            _spy,
        )
        assert status != 409


# ── connection_ref passthrough ─────────────────────────────────────────────────
#
# Verify that connection_ref IS passed (with resolved_password intact) to
# execute_deploy / execute_select for non-Postgres platforms, and is NOT
# passed for the Postgres path.


class TestConnectionRefPassthrough:

    def test_non_postgres_deploy_env_carries_connection(self):
        """The runner env must carry the non-Postgres connection (host + creds)."""
        cap = {}
        _post(
            _make_project(pg_connection=""),
            "mysql",
            {
                "host": "mysql.example.com",
                "port": 3306,
                "username": "u",
                "database": "d",
                "resolved_password": "sekr3t",
            },
            _capture_env(cap, _ok_run()),
        )
        assert cap["env"]["WB_TARGET_HOST"] == "mysql.example.com"
        assert cap["env"]["WB_TARGET_USER"] == "u"
        assert cap["env"]["WB_TARGET_DBNAME"] == "d"

    def test_non_postgres_deploy_env_has_resolved_password(self):
        """The runner env must carry resolved_password (ephemeral subprocess env)."""
        cap = {}
        _post(
            _make_project(pg_connection=""),
            "snowflake",
            {
                "host": "org.us-east-1",
                "username": "svc",
                "database": "DWH",
                "resolved_password": "tok3n_value",
            },
            _capture_env(cap, _ok_run()),
        )
        assert cap["env"]["WB_TARGET_PASSWORD"] == "tok3n_value"

    def test_postgres_deploy_env_uses_dsn_not_password(self):
        """Postgres path passes a DSN; no discrete password var."""
        cap = {}
        _post(_make_project(), "postgres", _PG_REF,
              _capture_env(cap, _ok_run()))
        assert cap["env"]["WB_TARGET_DSN"] == _PG_CONN
        assert "WB_TARGET_PASSWORD" not in cap["env"]

    def test_non_postgres_preview_receives_connection_ref(self):
        """connection_ref must be passed to execute_select for non-Postgres platforms."""
        captured = {}

        def _spy(**kw):
            captured.update(kw)
            from workbench.backend.sql_executor import SelectResult
            return SelectResult(status="failed", error_class="platform_not_supported")

        _preview_post(
            _make_project(pg_connection=""),
            "databricks",
            {
                "host": "adb.azuredatabricks.net",
                "resolved_password": "dapi_abc123",
                "extra_config": {"http_path": "/sql/1.0/warehouses/abc"},
            },
            _spy,
        )
        assert "connection_ref" in captured
        cr = captured.get("connection_ref") or {}
        assert cr.get("resolved_password") == "dapi_abc123"

    def test_non_postgres_preview_connection_ref_has_password(self):
        """connection_ref passed to execute_select must contain resolved_password."""
        captured = {}

        def _spy(**kw):
            captured.update(kw)
            from workbench.backend.sql_executor import SelectResult
            return SelectResult(status="failed", error_class="platform_not_supported")

        _preview_post(
            _make_project(pg_connection=""),
            "mysql",
            {"host": "mysql.example.com", "resolved_password": "my_pw"},
            _spy,
        )
        cr = captured.get("connection_ref") or {}
        assert cr.get("resolved_password") == "my_pw"

    def test_postgres_preview_does_not_receive_connection_ref(self):
        """connection_ref must be None for Postgres preview."""
        captured = {}

        def _spy(**kw):
            captured.update(kw)
            from workbench.backend.sql_executor import SelectResult
            return SelectResult(status="ok", columns=[], rows=[])

        _preview_post(_make_project(), "postgres", _PG_REF, _spy)
        assert captured.get("connection_ref") is None
