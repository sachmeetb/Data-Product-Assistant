"""Shared SQL execution FACADE for the data plane.

Single choke point for every code path that runs SQL against a Project's
database — deploy of virtual views (DDL), preview SELECTs from deployed
views, and Q&A / marketplace chat execution. All paths go through
`execute_deploy` or `execute_select`; both emit a `:QueryRun` audit node
into Neo4j regardless of outcome.

Facade / provider split (one engine, Postgres a peer):
  This module owns ONLY the facade concerns — the sqlparse safety gate
  (single-SELECT / CREATE-DROP-VIEW), row caps, the read-only policy, the
  `:QueryRun` audit write, and mapping a provider's neutral `ResultSet` /
  `DeploymentRun` to the `SelectResult` / `DeployResult` callers expect.

  Driver execution + per-platform error classification live in the
  per-platform providers (`platform/providers/*`), resolved through the
  central lookup (`platform/dispatch.get_query_executor` /
  `get_deployment_provider`). Postgres is one provider among peers; the
  Postgres view-recreate recovery lives in `PostgresDeploymentProvider`.
  An unknown/unsupported platform fails closed as
  `error_class="platform_not_supported"`; a missing driver (ImportError) maps
  to the same, with an actionable pip hint.

  `pg_connection` is a transient Postgres DSN param kept for back-compat; it is
  threaded to the Postgres provider via a neutral `dsn` key (no legacy
  DSN-in-ref shape). Non-Postgres callers pass a structured
  `connection_ref`. To add a platform: implement its provider's QueryExecutor /
  DeploymentProvider and wire it in `platform/dispatch` — nothing here changes.

Defensive primitives:
  * Statement timeout (60s DDL, 30s SELECT) — enforced in the provider session.
  * Hard row cap on SELECTs via subquery wrapper + LIMIT (in the provider).
  * sqlparse-based gating (here): DDL path accepts only CREATE/DROP VIEW;
    SELECT path accepts only single SELECT statements.
  * Read-only transaction on the SELECT path (in the provider).
  * Per-call audit row in Neo4j; failures still write one with errorClass.

No connection pool — every call opens and closes a fresh driver connection.
The execution paths are human-triggered and infrequent.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import sqlparse

from .platform.interfaces import QueryLimits
from .platform.providers._exec_util import (  # re-export for callers/tests
    rewrite_ddl_header_for_mysql as _rewrite_ddl_header_for_mysql,  # noqa: F401
)


DDL_TIMEOUT_MS = 60_000
SELECT_TIMEOUT_MS = 30_000
DEFAULT_MAX_ROWS = 1000
DEFAULT_PREVIEW_LIMIT = 50
MAX_QUERY_TEXT_BYTES = 4096

# Postgres platform aliases — validate_predicate's dry-run EXPLAIN is
# Postgres-only. All other platform dispatch goes through platform.dispatch.
_POSTGRES_PLATFORMS = frozenset({"postgres", "postgresql"})

# Allowed DDL prefixes after comment-stripping. sqlparse coalesces
# "CREATE OR REPLACE" into a single token so we don't go through its
# token API at all — we strip comments via sqlparse.format and then
# match a simple regex on the resulting text. Anything that doesn't
# match is rejected before opening a connection.
_DDL_ALLOWED_RE = re.compile(
    r"^\s*(CREATE\s+(OR\s+REPLACE\s+)?VIEW|DROP\s+VIEW)\b",
    re.IGNORECASE,
)

# Same strategy for the SELECT gate. We accept a leading "WITH ..." CTE
# too — the marketplace/preview path only builds bare SELECTs, but Phase 3
# may want CTEs and this is the obvious extension point.
_SELECT_ALLOWED_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


@dataclass
class DeployResult:
    status: str  # 'deployed' | 'failed'
    duration_ms: int
    error_class: Optional[str] = None
    error_message: Optional[str] = None
    statements_executed: int = 0
    smoke_test_count: int = 0


@dataclass
class SelectResult:
    status: str  # 'ok' | 'failed'
    columns: list[dict[str, Any]] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    truncated: bool = False
    row_count: int = 0
    duration_ms: int = 0
    error_class: Optional[str] = None
    error_message: Optional[str] = None


# ── sqlparse gating ────────────────────────────────────────────────────────


def _strip_comments(sql: str) -> str:
    """Return SQL with -- and /* */ comments removed, trimmed.

    Goes through sqlparse.format because regex-stripping comments would
    eat the contents of string literals like 'foo -- bar'."""
    return sqlparse.format(sql or "", strip_comments=True).strip()


def _classify_deploy_statement(raw_stmt: str) -> Optional[str]:
    """Return one of:
      ''           — comment-only / whitespace-only (skip, allowed)
      'CREATE VIEW'/'CREATE OR REPLACE VIEW'/'DROP VIEW' — accepted
      None         — rejected (caller should fail the deploy)
    """
    stripped = _strip_comments(raw_stmt).rstrip(";").strip()
    if not stripped:
        return ""
    m = _DDL_ALLOWED_RE.match(stripped)
    if not m:
        return None
    # Normalise to a canonical label for the metric.
    head = m.group(1).upper()
    head = re.sub(r"\s+", " ", head)
    return head


def _is_single_select(sql: str) -> bool:
    stripped = _strip_comments(sql).rstrip(";").strip()
    if not stripped:
        return False
    # Reject anything that looks like more than one statement after
    # comment-stripping (a literal semicolon mid-text). sqlparse.split
    # is the canonical splitter.
    statements = [s for s in (sqlparse.split(stripped) or []) if s.strip()]
    if len(statements) != 1:
        return False
    return bool(_SELECT_ALLOWED_RE.match(stripped))


# ── filter-predicate safety gate ────────────────────────────────────────────
#
# A dataset-level filter is authored by the PO in plain language and
# interpreted to SQL elsewhere (the filter-intent interpreter + the engineer
# filter-review panel). This is the shared backstop that makes sure a prose or
# otherwise-malformed predicate can NEVER silently reach a deployed view's
# WHERE clause — it is consumed at three hook points (engineer filter PUT,
# view-DDL generation parse-gate, and the deploy error arm).

# A predicate that "reads like plain language": two or more bare word tokens in
# a row with no SQL comparison/keyword operator between them anywhere in the
# fragment (e.g. "employee status is active"). `is`/`and`/`or`/`not` alone do
# not make it SQL — `IS` only counts when followed by NULL / TRUE / FALSE.
# Note: ILIKE and SIMILAR TO are Postgres-specific operators included here because
# this gate is a PROSE detector (catches "employee status is active"), NOT a dialect
# enforcer. SQL dialect correctness is enforced at execution time by the platform
# adapters. Including them here lets valid Postgres predicates authored by engineers
# pass the prose gate.
_PRED_OPERATOR_RE = re.compile(
    r"(?:[=<>]|!=|<>|\bIN\b|\bLIKE\b|\bILIKE\b|\bBETWEEN\b|"
    r"\bIS\s+(?:NOT\s+)?(?:NULL|TRUE|FALSE)\b|\bSIMILAR\s+TO\b|\b@@\b)",
    re.IGNORECASE,
)


@dataclass
class ValidationResult:
    ok: bool
    error_class: Optional[str] = None   # 'predicate_prose' | 'predicate_invalid'
    message: Optional[str] = None
    warning: Optional[str] = None       # non-fatal note (e.g. DB unreachable at generate-time)


_PROSE_MESSAGE = (
    "This filter still reads like plain language and isn't valid SQL. Open the "
    "filter review and pick the grounded column/value, or run “Check filter” "
    "again so it can be interpreted into a real condition."
)


def looks_like_prose(predicate: str) -> bool:
    """Heuristic: does this WHERE fragment read like prose rather than SQL?

    True when, after comment-stripping, the fragment contains NO comparison /
    membership / null-test operator at all. A real predicate always has at
    least one (`=`, `<`, `IN`, `LIKE`, `IS NULL`, ...). Bare-boolean-column
    predicates (`is_active`) are rare at the dataset level and would be caught
    by the dry-run EXPLAIN instead; the parse gate intentionally errs toward
    flagging operator-less text so prose like "employee status is active" is
    rejected before it ever reaches Postgres.
    """
    frag = _strip_comments(predicate or "").strip().rstrip(";").strip()
    if not frag:
        return False
    return _PRED_OPERATOR_RE.search(frag) is None


def _ddl_has_prose_where(ddl: str) -> bool:
    """True if any WHERE clause in the DDL reads like operator-less prose.

    Used by the deploy error arm to re-classify a raw Postgres syntax error as
    the actionable `predicate_prose` message. Scans each `WHERE ...` segment up
    to the next clause keyword and applies the same operator-less test.
    """
    text = _strip_comments(ddl or "")
    for m in re.finditer(r"\bWHERE\b(.*?)(?=\b(?:GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|UNION|\))|$)",
                         text, re.IGNORECASE | re.DOTALL):
        if looks_like_prose(m.group(1)):
            return True
    return False


def _clean_pg_error(msg: str) -> str:
    """Strip the noisy `LINE n:` / caret block from a psycopg2 error string."""
    first = (msg or "").strip().splitlines()[0] if (msg or "").strip() else ""
    return re.sub(r"\s+", " ", first).strip()


def validate_predicate(
    predicate: str,
    *,
    pg_connection: Optional[str] = None,
    sample_from: Optional[str] = None,
    platform: str = "postgres",
) -> ValidationResult:
    """Validate a dataset-level filter predicate before it can reach a view.

    Two phases:
      1. Parse (always, no DB): reject operator-less prose as 'predicate_prose'.
      2. Dry-run (when pg_connection given): EXPLAIN the predicate in a
         rolled-back transaction. Syntax / undefined-column errors map to
         'predicate_invalid' with a cleaned message. A connection/timeout
         failure is non-fatal — degrade to the parse-only result with a warning
         (so a transient DB outage never blocks an otherwise-sane edit).

    An empty predicate is valid (no filter).
    """
    frag = (predicate or "").strip()
    if not frag:
        return ValidationResult(ok=True)

    # Phase 1 — parse / prose detection (no DB needed).
    if looks_like_prose(frag):
        return ValidationResult(ok=False, error_class="predicate_prose", message=_PROSE_MESSAGE)

    # Phase 2 — dry-run EXPLAIN, but ONLY against a real table context. A bare
    # `EXPLAIN SELECT 1 WHERE (<predicate>)` would reject every column reference
    # as "does not exist" (no FROM), so without a `sample_from` we stop at the
    # parse check. The authoritative column-level validation happens at deploy
    # time when the full view DDL runs against the real source tables.
    # Non-Postgres platforms don't have a psycopg2 driver, so the dry-run is
    # skipped; the parse-only check above is the backstop.
    _plat = (platform or "postgres").lower()
    if not (pg_connection and sample_from) or _plat not in _POSTGRES_PLATFORMS:
        return ValidationResult(ok=True)

    import psycopg2  # lazy — the facade no longer hard-depends on the pg driver
    bare = frag.rstrip(";").strip()
    probe = f"EXPLAIN SELECT 1 FROM {sample_from} WHERE ({bare}) LIMIT 0"
    conn = None
    try:
        conn = psycopg2.connect(pg_connection)
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {SELECT_TIMEOUT_MS}")
            cur.execute(probe)
        conn.rollback()  # EXPLAIN has no side effects; rollback keeps the contract explicit
        return ValidationResult(ok=True)
    except psycopg2.Error as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return ValidationResult(
            ok=False,
            error_class="predicate_invalid",
            message=(
                "This filter isn't valid SQL against the source data: "
                f"{_clean_pg_error(str(e))}. Fix the column or value in the filter review."
            ),
        )
    except Exception as e:
        # Connection / timeout / anything non-Postgres: don't block on it.
        return ValidationResult(
            ok=True,
            warning=f"Could not dry-run the filter against the database ({e}); parse check passed.",
        )
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ── Audit ──────────────────────────────────────────────────────────────────


_WRITE_QUERY_RUN = """
MERGE (p:Project {projectCode: $project_code})
CREATE (qr:QueryRun {
  uri: $uri,
  projectCode: $project_code,
  kind: $kind,
  text: $text,
  productUri: $product_uri,
  viewSchema: $view_schema,
  executedBy: $executed_by,
  executedAt: datetime(),
  rowCount: $row_count,
  durationMs: $duration_ms,
  status: $status,
  errorClass: $error_class
})
CREATE (p)-[:HAS_QUERY_RUN]->(qr)
RETURN qr.uri AS uri
"""


def _truncate_text(text: str, max_bytes: int = MAX_QUERY_TEXT_BYTES) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore") + " …[truncated]"


def _write_query_run(
    neo4j_session,
    *,
    project_code: str,
    kind: str,
    text: str,
    executed_by: str,
    status: str,
    duration_ms: int,
    row_count: Optional[int] = None,
    error_class: Optional[str] = None,
    product_uri: Optional[str] = None,
    view_schema: Optional[str] = None,
) -> None:
    """Best-effort audit write. If Neo4j is unreachable the SQL operation
    still completes — we never fail a deploy/preview because the audit
    write failed."""
    try:
        neo4j_session.run(
            _WRITE_QUERY_RUN,
            uri=f"queryrun:{project_code}:{uuid.uuid4()}",
            project_code=project_code,
            kind=kind,
            text=_truncate_text(text or ""),
            product_uri=product_uri,
            view_schema=view_schema,
            executed_by=executed_by,
            row_count=row_count,
            duration_ms=duration_ms,
            status=status,
            error_class=error_class,
        ).consume()
    except Exception:
        # Swallow — audit write must never break the operation.
        pass


# ── facade helpers ───────────────────────────────────────────────────────────
#
# Execution is owned by the per-platform providers (platform/providers/*), which
# the central lookup (platform/dispatch) hands to this facade. The facade keeps
# the sqlparse safety gate, row caps, the :QueryRun audit, and the neutral result
# mapping ONLY — it never opens a driver connection itself.
# `_rewrite_ddl_header_for_mysql` is re-exported from
# platform.providers._exec_util (imported at module top) for back-compat.
#
# SECURITY: connection_ref["resolved_password"]/["dsn"] is consumed only inside a
# provider to open a live connection. It is NEVER logged, persisted, or returned.

# Actionable driver-missing message per platform (surfaced as platform_not_supported).
_DRIVER_HINTS = {
    "mysql": "pymysql driver not installed. Ensure pymysql>=1.0 is in requirements.",
    "snowflake": "snowflake-connector-python not installed. Add snowflake-connector-python>=3.0.0.",
    "databricks": "databricks-sql-connector not installed. Add databricks-sql-connector>=3.0.0.",
}


def _provider_conn_ref(platform, pg_connection, connection_ref):
    """The connection_ref handed to a provider. For Postgres, a transient DSN
    (the caller's ``pg_connection`` param) rides a neutral ``dsn`` key during the
    migration to structured refs; otherwise the structured ref passes through.
    No legacy DSN-in-ref shape."""
    cref = dict(connection_ref or {})
    if (platform or "").lower() in _POSTGRES_PLATFORMS and pg_connection:
        cref = {"dsn": pg_connection}
    return cref

# ── Public entry points ────────────────────────────────────────────────────


def execute_deploy(
    *,
    neo4j_session,
    project_code: str,
    pg_connection: str,
    ddl: str,
    view_schema: str,
    view_names: list[str],
    executed_by: str,
    product_uri: Optional[str] = None,
    platform: str = "postgres",
    connection_json: str = "{}",
    connection_ref: Optional[dict] = None,
) -> DeployResult:
    """Facade: gate → audit → delegate to the platform's DeploymentProvider.

    Executes CREATE/DROP VIEW DDL (only) and smoke-tests each promised view via
    the per-platform ``DeploymentProvider.deploy_views``. The safety gate + the
    :QueryRun audit live HERE; driver execution + the Postgres view-recreate
    recovery live in the provider (never a vendor exception past that boundary).
    ``pg_connection`` (a transient Postgres DSN) is threaded to the provider via a
    neutral ``dsn`` key; non-Postgres platforms ride ``connection_ref``.
    ``connection_json`` is accepted for back-compat and ignored (superseded by
    ``connection_ref``)."""
    from .platform.dispatch import (
        get_deployment_provider, UnknownPlatform, ProviderUnavailable,
    )
    started = time.monotonic()
    _plat = (platform or "postgres").lower()

    # Resolve the provider first — fail-closed for a truly unknown / unsupported
    # platform, with the same actionable message as before.
    try:
        provider = get_deployment_provider(_plat)
    except (UnknownPlatform, ProviderUnavailable):
        return _deploy_fail(
            neo4j_session, ddl, project_code, executed_by, started,
            "platform_not_supported",
            (
                f"Direct virtual-view deploy on platform '{platform}' is not "
                "supported. Use the dbt materialization path (serving_physical_copy "
                "stage) to serve data on non-Postgres targets."
            ),
            product_uri, view_schema,
        )

    # Parse-time gate — reject anything that isn't CREATE/DROP VIEW before a driver.
    raw_statements = [s for s in (sqlparse.split(ddl) or []) if s and s.strip()]
    if not raw_statements:
        return _deploy_fail(
            neo4j_session, ddl, project_code, executed_by, started,
            "ddl_rejected", "DDL contained no statements", product_uri, view_schema,
        )
    statements_executed = 0
    for raw_stmt in raw_statements:
        cls = _classify_deploy_statement(raw_stmt)
        if cls is None:
            preview = _strip_comments(raw_stmt)[:120]
            return _deploy_fail(
                neo4j_session, ddl, project_code, executed_by, started,
                "ddl_rejected", f"Disallowed statement: {preview}",
                product_uri, view_schema,
            )
        if cls:
            statements_executed += 1

    # Delegate driver execution to the provider (owns view-recreate recovery +
    # error classification; returns a neutral DeploymentRun).
    cref = _provider_conn_ref(_plat, pg_connection, connection_ref)
    run = provider.deploy_views(cref, ddl, view_schema, list(view_names))

    if run.status == "succeeded":
        duration_ms = run.duration_ms or int((time.monotonic() - started) * 1000)
        _write_query_run(
            neo4j_session, project_code=project_code, kind="deploy", text=ddl,
            executed_by=executed_by, status="deployed", duration_ms=duration_ms,
            product_uri=product_uri, view_schema=view_schema,
        )
        return DeployResult(
            status="deployed", duration_ms=duration_ms,
            statements_executed=statements_executed,
            smoke_test_count=len(run.deployed_objects or []),
        )

    # Failed — re-classify a raw syntax error as the actionable prose message when
    # the DDL carries an operator-less WHERE (an un-interpreted filter that
    # bypassed the authoring gates).
    error_class = run.error_class or "sql_error"
    error_message = run.error_message or ""
    if error_class == "sql_error" and _ddl_has_prose_where(ddl):
        error_class, error_message = "predicate_prose", _PROSE_MESSAGE
    return _deploy_fail(
        neo4j_session, ddl, project_code, executed_by, started,
        error_class, error_message, product_uri, view_schema,
    )
def _deploy_fail(
    neo4j_session, ddl, project_code, executed_by, started,
    error_class, error_message, product_uri, view_schema,
) -> DeployResult:
    duration_ms = int((time.monotonic() - started) * 1000)
    _write_query_run(
        neo4j_session, project_code=project_code, kind="deploy", text=ddl,
        executed_by=executed_by, status="failed", duration_ms=duration_ms,
        error_class=error_class, product_uri=product_uri, view_schema=view_schema,
    )
    return DeployResult(
        status="failed",
        duration_ms=duration_ms,
        error_class=error_class,
        error_message=error_message,
    )


def execute_select(
    *,
    neo4j_session,
    project_code: str,
    pg_connection: str,
    sql: str,
    executed_by: str,
    max_rows: int = DEFAULT_PREVIEW_LIMIT,
    product_uri: Optional[str] = None,
    view_schema: Optional[str] = None,
    platform: str = "postgres",
    connection_json: str = "{}",
    connection_ref: Optional[dict] = None,
) -> SelectResult:
    """Facade: gate → audit → delegate to the platform's QueryExecutor.

    Runs a single SELECT (gate-enforced) with a hard row cap. The provider wraps
    the query in a LIMIT + a read-only session and returns a neutral ResultSet;
    the :QueryRun audit lives here. ``pg_connection`` (a transient Postgres DSN)
    rides a neutral ``dsn`` key to the provider; non-Postgres platforms ride
    ``connection_ref``. ``connection_json`` is accepted for back-compat, ignored."""
    from .platform.dispatch import (
        get_query_executor, UnknownPlatform, ProviderUnavailable,
    )
    started = time.monotonic()
    max_rows = max(1, min(max_rows, DEFAULT_MAX_ROWS))
    _plat = (platform or "postgres").lower()

    try:
        executor = get_query_executor(_plat)
    except (UnknownPlatform, ProviderUnavailable):
        return _select_fail(
            neo4j_session, sql, project_code, executed_by, started,
            "platform_not_supported",
            (
                f"Query execution on platform '{platform}' is not supported. "
                "Marketplace Q&A and preview are available for the platforms with "
                "a registered query executor."
            ),
            product_uri, view_schema,
        )

    if not _is_single_select(sql):
        return _select_fail(
            neo4j_session, sql, project_code, executed_by, started,
            "not_select", "Only single SELECT statements are permitted",
            product_uri, view_schema,
        )

    cref = _provider_conn_ref(_plat, pg_connection, connection_ref)
    limits = QueryLimits(max_rows=max_rows, timeout_ms=SELECT_TIMEOUT_MS, read_only=True)
    try:
        rs = executor.select(cref, sql, limits)
    except ImportError:
        return _select_fail(
            neo4j_session, sql, project_code, executed_by, started,
            "platform_not_supported",
            _DRIVER_HINTS.get(_plat, f"driver for '{platform}' not installed"),
            product_uri, view_schema,
        )
    except Exception as e:  # noqa: BLE001 — providers surface driver errors here
        error_class = "sql_error"
        try:
            error_class = executor.classify_error(e) or "sql_error"
        except Exception:
            pass
        return _select_fail(
            neo4j_session, sql, project_code, executed_by, started,
            error_class, str(e), product_uri, view_schema,
        )

    duration_ms = rs.duration_ms or int((time.monotonic() - started) * 1000)
    _write_query_run(
        neo4j_session, project_code=project_code, kind="preview", text=sql,
        executed_by=executed_by, status="ok", duration_ms=duration_ms,
        row_count=rs.row_count, product_uri=product_uri, view_schema=view_schema,
    )
    return SelectResult(
        status="ok", columns=rs.columns, rows=rs.rows,
        truncated=rs.truncated, row_count=rs.row_count, duration_ms=duration_ms,
    )
def _select_fail(
    neo4j_session, sql, project_code, executed_by, started,
    error_class, error_message, product_uri, view_schema,
) -> SelectResult:
    duration_ms = int((time.monotonic() - started) * 1000)
    _write_query_run(
        neo4j_session, project_code=project_code, kind="preview", text=sql,
        executed_by=executed_by, status="failed", duration_ms=duration_ms,
        error_class=error_class, product_uri=product_uri, view_schema=view_schema,
    )
    return SelectResult(
        status="failed",
        duration_ms=duration_ms,
        error_class=error_class,
        error_message=error_message,
    )
