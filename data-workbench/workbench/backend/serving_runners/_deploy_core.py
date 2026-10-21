"""Portable virtual-view deploy core — applies CREATE/DROP VIEW DDL to a target.

STDLIB + ``sqlparse`` + the platform DB driver only (all declared in the
package's ``requirements.txt``). NO ``workbench.*`` imports — this file is copied
verbatim into a downloadable serving package next to ``run.py`` and must run
standalone on an engineer's machine.

This is the single operational implementation of "deploy the view" that BOTH
Data Workbench (via the packaged ``run.py``) and the engineer run. It mirrors the
behaviour of the legacy in-process ``sql_executor.execute_deploy`` — same DDL
gate, same per-platform apply, and the same Postgres **view-recreate recovery**
(``CREATE OR REPLACE VIEW`` cannot change a column's type / drop / rename
columns / change column count; on those specific errors we DROP the view(s)
non-CASCADE and re-run) — but returns a plain dict instead of writing a Neo4j
audit. The audit + deploy-status persistence stay in the backend caller.

``apply_ddl`` returns::

    {"status": "deployed"|"failed", "error_class": str|None, "error_message": str|None,
     "statements_executed": int, "smoke_test_count": int, "recovery_used": bool}
"""
from __future__ import annotations

import re
from typing import Any, Optional

import sqlparse


DDL_TIMEOUT_MS = 60_000

_DDL_ALLOWED_RE = re.compile(
    r"^\s*(CREATE\s+(OR\s+REPLACE\s+)?VIEW|DROP\s+VIEW)\b", re.IGNORECASE,
)

_MYSQL_VIEW_HEADER_RE = re.compile(
    r'^(CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+)'
    r'(?:"([^"]+)"|`([^`]+)`|([A-Za-z_][A-Za-z0-9_]*))\.'
    r'(?:"([^"]+)"|`([^`]+)`|([A-Za-z_][A-Za-z0-9_]*))',
    re.IGNORECASE | re.MULTILINE,
)

# Extracts the target relation from a CREATE [OR REPLACE] VIEW header so a
# per-view deploy can report which statement failed (double/backtick/bare quoting).
_VIEW_HEADER_TARGET_RE = re.compile(
    r'^\s*CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?([^\s(]+)',
    re.IGNORECASE,
)


def _view_target_from_stmt(stmt: str) -> str:
    m = _VIEW_HEADER_TARGET_RE.match(stmt or "")
    return _bare(m.group(1)) if m else ""


_PG_VIEW_DRIFT_PHRASES = (
    "cannot change data type of view column",
    "cannot drop columns from view",
    "cannot change name of view column",
    "cannot change number of columns in view",
)


class DeployError(Exception):
    """Carries an error_class so the runner can classify a failure.

    ``details`` optionally carries structured context (e.g. per-view
    success/failure for a partial deployment) that ``apply_ddl`` surfaces in the
    result dict under ``"details"``.
    """
    def __init__(self, error_class: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.error_class = error_class
        self.message = message
        self.details = details


def _strip_comments(sql: str) -> str:
    return sqlparse.format(sql or "", strip_comments=True).strip()


def classify_ddl(ddl: str) -> Optional[str]:
    """Return an error message if the DDL contains a non-CREATE/DROP-VIEW
    statement, else None. Mirrors ``sql_executor._classify_deploy_statement``."""
    raw = [s for s in (sqlparse.split(ddl) or []) if s and s.strip()]
    if not raw:
        return "DDL contained no statements"
    for stmt in raw:
        stripped = _strip_comments(stmt).rstrip(";").strip()
        if not stripped:
            continue
        if not _DDL_ALLOWED_RE.match(stripped):
            return f"Disallowed statement: {stripped[:120]}"
    return None


def rewrite_ddl_header_for_mysql(ddl: str) -> str:
    """Rewrite a CREATE VIEW ``schema.view`` header to MySQL backtick quoting.
    Mirrors ``sql_executor._rewrite_ddl_header_for_mysql``."""
    def _rw(m: "re.Match") -> str:
        prefix = m.group(1)
        schema = m.group(2) or m.group(3) or m.group(4) or ""
        view = m.group(5) or m.group(6) or m.group(7) or ""
        return f"{prefix}`{schema}`.`{view}`"
    return _MYSQL_VIEW_HEADER_RE.sub(_rw, ddl)


def _bare(raw_name: str) -> str:
    return raw_name.rsplit(".", 1)[-1].strip('"').strip("`")


class _NamespaceOps:
    """Stdlib mirror of ``platform.namespace.NamespaceModel``, reconstructed from
    the descriptor embedded in ``package.json``. The runner is stdlib-only and
    cannot import the backend, so the platform model serialises itself here and
    this reproduces quoting / session-setup / create-namespace generically — no
    per-platform namespace assumptions hand-coded in the runner.

    Descriptor shape (see ``NamespaceModel.to_descriptor``):
      {platform, parts:[...], quote_style:'backtick'|'double',
       session_catalog_statement:'USE CATALOG {catalog}' | ''}
    """

    def __init__(self, descriptor: dict):
        self.parts = list(descriptor.get("parts") or ["schema", "relation"])
        self.quote_style = descriptor.get("quote_style", "double")
        # Snowflake folds unquoted DDL to UPPER; the CREATE header + every read
        # address the view UPPER, so upper-fold here too (smoke test /
        # create-namespace) or the smoke SELECT misses the UPPER object.
        self.upper_fold = bool(descriptor.get("upper_fold", False))
        self.session_catalog_statement = descriptor.get("session_catalog_statement", "") or ""

    @property
    def container_parts(self) -> list:
        return self.parts[:-1]

    def quote(self, name: str) -> str:
        if self.upper_fold:
            name = (name or "").upper()
        return f"`{name}`" if self.quote_style == "backtick" else f'"{name}"'

    def parse(self, raw: str) -> dict:
        containers = self.container_parts
        values = [p for p in (raw or "").split(".") if p]
        if not values or not containers:
            return {}
        values = values[-len(containers):]
        offset = len(containers) - len(values)
        return {containers[offset + i]: v for i, v in enumerate(values)}

    def qualified(self, namespace: str, relation: str) -> str:
        parts = [p for p in (namespace or "").split(".") if p] + [relation]
        return ".".join(self.quote(p) for p in parts)

    def session_setup(self, source_raw: str) -> list:
        if not self.session_catalog_statement:
            return []
        catalog = self.parse(source_raw).get("catalog", "")
        if not catalog:
            return []
        return [self.session_catalog_statement.format(catalog=self.quote(catalog))]

    def create_namespace(self, namespace: str) -> "str | None":
        parsed = self.parse(namespace)
        if not parsed:
            return None
        return "CREATE SCHEMA IF NOT EXISTS " + ".".join(self.quote(v) for v in parsed.values())


# ── per-platform apply ─────────────────────────────────────────────────────────


def _apply_postgres(ddl, conn_params, view_schema, view_names, ns_ops=None):
    import psycopg2
    dsn = conn_params.get("dsn")
    connect = (lambda: psycopg2.connect(dsn)) if dsn else (lambda: psycopg2.connect(
        host=conn_params.get("host"), port=conn_params.get("port"),
        user=conn_params.get("user"), password=conn_params.get("password"),
        dbname=conn_params.get("dbname")))

    def _run(drop_first: bool):
        conn = None
        try:
            conn = connect()
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = {DDL_TIMEOUT_MS}")
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{view_schema}"')
                if drop_first:
                    for raw_name in view_names:
                        b = _bare(raw_name)
                        if b:
                            cur.execute(f'DROP VIEW IF EXISTS "{view_schema}"."{b}"')
                cur.execute(ddl)
                smoke = 0
                for raw_name in view_names:
                    b = _bare(raw_name)
                    if b:
                        cur.execute(f'SELECT 1 FROM "{view_schema}"."{b}" LIMIT 1')
                        smoke += 1
            conn.commit()
            return smoke
        except Exception:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    import psycopg2 as _pg
    try:
        return _run(drop_first=False), False
    except _pg.errors.InsufficientPrivilege as e:
        raise DeployError("permission_denied", str(e))
    except _pg.errors.QueryCanceled as e:
        raise DeployError("timeout", str(e))
    except _pg.OperationalError as e:
        raise DeployError("connection_error", str(e))
    except _pg.Error as e:
        err = str(e).lower()
        if any(p in err for p in _PG_VIEW_DRIFT_PHRASES) and view_names:
            # View-schema drift → drop non-CASCADE + re-run.
            try:
                return _run(drop_first=True), True
            except _pg.errors.DependentObjectsStillExist as e2:
                raise DeployError(
                    "dependents_block_redeploy",
                    "Cannot redeploy because the view is referenced by other objects "
                    f"downstream. Drop the dependents first. Postgres said: {e2}")
            except _pg.Error as e2:
                raise DeployError("sql_error", str(e2))
        raise DeployError("sql_error", str(e))


def _apply_mysql(ddl, conn_params, view_schema, view_names, ns_ops=None):
    try:
        import pymysql
    except ImportError:
        raise DeployError("platform_not_supported",
                          "pymysql driver not installed (add pymysql>=1.0 to requirements).")
    conn = None
    try:
        conn = pymysql.connect(
            host=conn_params.get("host", "localhost"), port=int(conn_params.get("port", 3306)),
            user=conn_params.get("user", ""), password=conn_params.get("password", ""),
            database=conn_params.get("dbname") or None, connect_timeout=60)
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{view_schema}`")
            for raw_stmt in (sqlparse.split(ddl) or []):
                stmt = raw_stmt.strip().rstrip(";").strip()
                if stmt:
                    cur.execute(rewrite_ddl_header_for_mysql(stmt))
            smoke = 0
            for raw_name in view_names:
                b = _bare(raw_name)
                if b:
                    cur.execute(f"SELECT 1 FROM `{view_schema}`.`{b}` LIMIT 1")
                    smoke += 1
        conn.commit()
        return smoke, False
    except pymysql.OperationalError as e:
        raise DeployError("connection_error", str(e))
    except pymysql.Error as e:
        raise DeployError("sql_error", str(e))
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _apply_snowflake(ddl, conn_params, view_schema, view_names, ns_ops=None):
    try:
        import snowflake.connector as sf
    except ImportError:
        raise DeployError("platform_not_supported",
                          "snowflake-connector-python not installed.")
    # Snowflake is a 3-level namespace (database.schema.relation) — drive quoting /
    # session-setup / create-namespace from the package descriptor exactly like
    # Databricks, so `catalog.schema` target namespaces quote each part correctly
    # (the old inline `"{view_schema}"` treated a dotted namespace as ONE
    # identifier). Fall back to a Snowflake-shaped default for legacy packages.
    if ns_ops is None:
        ns_ops = _NamespaceOps({"platform": "snowflake",
                                "parts": ["catalog", "schema", "relation"],
                                "quote_style": "double",
                                "upper_fold": True,
                                "session_catalog_statement": "USE DATABASE {catalog}"})
    conn = None
    try:
        # A Snowflake PAT authenticates as a plain password. Accept the credential
        # from either `token` (extra_config.token / WB_TARGET_TOKEN) or `password`
        # (resolved_password / WB_TARGET_PASSWORD) for parity with Databricks.
        secret = conn_params.get("token") or conn_params.get("password", "")

        # Unqualified refs in the view BODY resolve against the session database →
        # point it at the SOURCE database so cross-database bodies bind at creation.
        # (For the common single-database case connect(database=…) already sets it.)
        source_raw = conn_params.get("catalog") or conn_params.get("dbname", "")

        # Guard: a 3-level platform needs a database in the target namespace. A bare
        # schema would CREATE SCHEMA in the session's current database — fail fast
        # with an actionable message rather than a cryptic error (mirrors Databricks).
        if "catalog" in ns_ops.container_parts and not ns_ops.parse(view_schema).get("catalog"):
            raise DeployError(
                "target_namespace_required",
                f"Target namespace '{view_schema}' has no database. Snowflake needs a "
                "writable 'database.schema' (e.g. 'DWB_SERVING_DB.PUBLIC') — set it in "
                "Configure Serving.")

        conn = sf.connect(
            account=conn_params.get("host", ""), user=conn_params.get("user", ""),
            password=secret, database=conn_params.get("dbname", ""),
            warehouse=conn_params.get("warehouse", ""), role=conn_params.get("role", ""),
            schema=conn_params.get("schema", ""), login_timeout=60)
        with conn.cursor() as cur:
            for stmt in ns_ops.session_setup(source_raw):
                cur.execute(stmt)
            create_ns = ns_ops.create_namespace(view_schema)
            if create_ns:
                cur.execute(create_ns)

            # Per-view apply with partial-deployment reporting: run each CREATE VIEW
            # independently (Snowflake DDL auto-commits), tracking which relation each
            # statement targets so one bad view doesn't mask the rest.
            stmt_status: dict[str, str] = {}   # relation -> "created" | "error: …"
            for raw_stmt in (sqlparse.split(ddl) or []):
                stmt = raw_stmt.strip().rstrip(";").strip()
                if not stmt:
                    continue
                rel = _view_target_from_stmt(stmt) or f"stmt#{len(stmt_status) + 1}"
                try:
                    cur.execute(stmt)
                    stmt_status[rel] = "created"
                except Exception as e:  # noqa: BLE001 — per-view isolation
                    stmt_status[rel] = f"error: {e}"

            deployed, failed = [], []
            for raw_name in view_names:
                b = _bare(raw_name)
                if not b:
                    continue
                # stmt_status is keyed by _view_target_from_stmt = _bare(DDL
                # header), which is UPPER when the CREATE header was emitted UPPER
                # (Snowflake). view_names arrive logical-cased, so fold the lookup
                # key to match; the smoke SELECT below upper-folds via ns_ops.quote.
                b_key = b.upper() if ns_ops.upper_fold else b
                status = stmt_status.pop(b_key, "")
                if status.startswith("error"):
                    failed.append({"view": b, "stage": "create", "error": status[len("error: "):]})
                    continue
                try:
                    cur.execute(f"SELECT 1 FROM {ns_ops.qualified(view_schema, b)} LIMIT 1")
                    deployed.append(b)
                except Exception as e:  # noqa: BLE001
                    failed.append({"view": b, "stage": "smoke", "error": str(e)})
            # Any statement failure not matched to a declared view name (defensive).
            for rel, status in stmt_status.items():
                if status.startswith("error"):
                    failed.append({"view": rel, "stage": "create", "error": status[len("error: "):]})

        conn.commit()
        if failed:
            summary = "; ".join(f"{f['view']} ({f['stage']}): {f['error']}" for f in failed)
            raise DeployError(
                "partial_deploy",
                f"{len(deployed)} view(s) deployed, {len(failed)} failed — {summary}",
                details={"deployed": deployed, "failed": failed})
        return len(deployed), False
    except DeployError:
        # Typed failures (target_namespace_required / partial_deploy) keep their class.
        raise
    except Exception as e:
        raise DeployError("sql_error", str(e))
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _apply_databricks(ddl, conn_params, view_schema, view_names, ns_ops=None):
    try:
        from databricks import sql as dbsql
    except ImportError:
        raise DeployError("platform_not_supported",
                          "databricks-sql-connector not installed.")
    # Namespace behaviour is data-driven from the package descriptor (the
    # serialised platform NamespaceModel) so no 3-level assumption is hand-coded
    # here. Fall back to a databricks-shaped default for legacy packages that
    # predate the descriptor.
    if ns_ops is None:
        ns_ops = _NamespaceOps({"platform": "databricks",
                                "parts": ["catalog", "schema", "relation"],
                                "quote_style": "backtick",
                                "session_catalog_statement": "USE CATALOG {catalog}"})
    conn = None
    try:
        # The PAT arrives as `token` (extra_config.token) OR `password`
        # (the connection's resolved_password / WB_TARGET_PASSWORD) depending on
        # how the connection was registered. Accept either — reading only `token`
        # left access_token empty and hung the connect until the 300s timeout.
        access_token = conn_params.get("token") or conn_params.get("password", "")

        # Unqualified `schema.table` refs in the view BODY resolve against the
        # session default catalog → point it at the SOURCE (the connection's
        # database, e.g. "samples.bakehouse") so cross-catalog bodies bind at
        # creation time. Target CREATE SCHEMA + smoke test come from view_schema.
        # The generated DDL header is already fully qualified (build_prompt
        # passes --view-schema), so statements run raw.
        source_raw = conn_params.get("catalog") or conn_params.get("dbname", "")

        # Guard: a 3-level platform needs a catalog in the target namespace. A
        # bare schema (e.g. "public") would CREATE SCHEMA in the session's
        # current catalog — the read-only SOURCE — yielding a cryptic
        # PERMISSION_DENIED. Fail fast with an actionable message instead.
        if "catalog" in ns_ops.container_parts and not ns_ops.parse(view_schema).get("catalog"):
            raise DeployError(
                "target_namespace_required",
                f"Target namespace '{view_schema}' has no catalog. Databricks needs a "
                "writable 'catalog.schema' (e.g. 'workspace.default') — set it in "
                "Configure Serving; the source catalog is typically read-only.")

        conn = dbsql.connect(
            server_hostname=conn_params.get("host", ""),
            http_path=conn_params.get("http_path", ""),
            access_token=access_token)
        with conn.cursor() as cur:
            for stmt in ns_ops.session_setup(source_raw):
                cur.execute(stmt)
            create_ns = ns_ops.create_namespace(view_schema)
            if create_ns:
                cur.execute(create_ns)
            for raw_stmt in (sqlparse.split(ddl) or []):
                stmt = raw_stmt.strip().rstrip(";").strip()
                if stmt:
                    cur.execute(stmt)
            smoke = 0
            for raw_name in view_names:
                b = _bare(raw_name)
                if b:
                    cur.execute(f"SELECT 1 FROM {ns_ops.qualified(view_schema, b)} LIMIT 1")
                    smoke += 1
        return smoke, False
    except DeployError:
        # Our own typed failures (e.g. target_namespace_required) keep their
        # error_class — don't collapse them to a generic sql_error.
        raise
    except Exception as e:
        raise DeployError("sql_error", str(e))
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


_APPLY = {
    "postgres": _apply_postgres, "postgresql": _apply_postgres,
    "mysql": _apply_mysql, "snowflake": _apply_snowflake, "databricks": _apply_databricks,
}


def apply_ddl(ddl: str, platform: str, conn_params: dict,
              view_schema: str, view_names: list,
              namespace: dict | None = None) -> dict[str, Any]:
    """Apply CREATE/DROP VIEW ``ddl`` to ``platform`` and smoke-test each view.

    Returns a result dict (never raises). ``conn_params`` is the flat dict the
    runner builds from ``WB_*`` env vars (dsn OR host/port/user/password/dbname
    + warehouse/role/schema/http_path/token). ``namespace`` is the serialised
    platform NamespaceModel descriptor from ``package.json`` (parts / quote_style /
    session_catalog_statement) — drives quoting + session-setup + create-namespace
    for 3-level platforms without any assumption hand-coded here. Behaviour parity
    with ``sql_executor.execute_deploy`` minus the Neo4j audit.
    """
    plat = (platform or "postgres").lower()
    apply = _APPLY.get(plat)
    if apply is None:
        return {"status": "failed", "error_class": "platform_not_supported",
                "error_message": f"platform '{platform}' not supported by run_deploy",
                "statements_executed": 0, "smoke_test_count": 0, "recovery_used": False}

    gate_err = classify_ddl(ddl)
    if gate_err is not None:
        return {"status": "failed", "error_class": "ddl_rejected", "error_message": gate_err,
                "statements_executed": 0, "smoke_test_count": 0, "recovery_used": False}

    ns_ops = _NamespaceOps(namespace) if namespace else None
    stmt_count = len([s for s in (sqlparse.split(ddl) or []) if s and s.strip()])
    try:
        smoke, recovery = apply(ddl, conn_params, view_schema, list(view_names or []), ns_ops)
    except DeployError as e:
        res = {"status": "failed", "error_class": e.error_class, "error_message": e.message,
               "statements_executed": 0, "smoke_test_count": 0, "recovery_used": False}
        if e.details is not None:
            # Structured partial-deployment reporting (per-view success/failure).
            res["details"] = e.details
        return res
    return {"status": "deployed", "error_class": None, "error_message": None,
            "statements_executed": stmt_count, "smoke_test_count": smoke,
            "recovery_used": recovery}
