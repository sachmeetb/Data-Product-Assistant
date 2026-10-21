"""Postgres compatibility provider.

Wraps the existing Postgres implementation behind the platform SPI so
the rest of the codebase can call provider methods without knowing
which database they're talking to.

Phase 1 goal: all current Postgres behavior passes through this
provider unchanged.  No existing call sites are modified yet — this
provider is called directly from new code paths only.  Phase 2 will
route the legacy pg_connection paths through it.

This provider does NOT import psycopg2 at module level — imports are
deferred to method bodies so the module can be loaded in environments
where psycopg2 is absent (test fixtures, CI without a live Postgres).
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from ..interfaces import (
    CapabilityEvidence,
    CapabilityLevel,
    ColumnInfo,
    CompensationReport,
    DdlArtifact,
    DeploymentChangeSet,
    DeploymentRun,
    DiscoveryBundle,
    ForeignKeyInfo,
    ManagedConnection,
    NamespaceRef,
    QueryLimits,
    RelationSummary,
    ResultSet,
    VerificationReport,
    ValidationReport,
)

logger = logging.getLogger(__name__)

PLATFORM_TYPE = "postgres"


class PostgresConnectionProvider:
    """Validates DSN-style connections and probes basic capabilities."""

    def validate_config(
        self, public_config: dict[str, Any], secret_ref: str
    ) -> ValidationReport:
        host = public_config.get("host", "")
        database = public_config.get("database", "")
        errors = []
        if not host:
            errors.append("host is required")
        if not database:
            errors.append("database is required")
        if not secret_ref:
            errors.append("secret_ref is required (never pass plaintext passwords)")
        return ValidationReport(valid=not errors, errors=errors)

    def probe(self, connection_ref: dict[str, Any]) -> CapabilityEvidence:
        """Attempt a live connection and return server version + capabilities."""
        import psycopg2
        evidence = CapabilityEvidence(
            platform_type=PLATFORM_TYPE,
            instance_id=connection_ref.get("connection_id", "unknown"),
        )
        try:
            dsn = _resolve_dsn(connection_ref)
            with psycopg2.connect(dsn, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT version()")
                    row = cur.fetchone()
                    evidence.server_version = (row[0] if row else None)
            # If we got here, basic connection and read are certified.
            evidence.capabilities = {
                "connection": CapabilityLevel.CERTIFIED,
                "read_query": CapabilityLevel.CERTIFIED,
            }
        except Exception as exc:
            evidence.warnings.append(f"probe failed: {exc}")
        return evidence

    def open(
        self, connection_ref: dict[str, Any], purpose: str
    ) -> ManagedConnection:
        import psycopg2
        dsn = _resolve_dsn(connection_ref)
        conn = psycopg2.connect(dsn)
        return ManagedConnection(
            platform_type=PLATFORM_TYPE,
            instance_id=connection_ref.get("connection_id", "unknown"),
            purpose=purpose,
            _internal=conn,
        )


class PostgresDiscoveryProvider:
    """Lists Postgres schemas and tables via INFORMATION_SCHEMA."""

    def list_namespaces(
        self,
        connection_ref: dict[str, Any],
        parent: Optional[str] = None,
    ) -> list[NamespaceRef]:
        import psycopg2
        dsn = _resolve_dsn(connection_ref)
        instance_id = connection_ref.get("connection_id", "unknown")
        results = []
        try:
            with psycopg2.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT schema_name
                        FROM information_schema.schemata
                        WHERE schema_name NOT IN (
                            'information_schema', 'pg_catalog',
                            'pg_toast', 'pg_temp_1', 'pg_toast_temp_1'
                        )
                        ORDER BY schema_name
                        """
                    )
                    for (name,) in cur.fetchall():
                        results.append(
                            NamespaceRef(
                                platform_instance_id=instance_id,
                                parts=[name],
                                labels={"schema": name},
                            )
                        )
        except Exception as exc:
            logger.warning("list_namespaces failed: %s", exc)
        return results

    def list_relations(
        self,
        connection_ref: dict[str, Any],
        namespace: NamespaceRef,
    ) -> list[RelationSummary]:
        import psycopg2
        dsn = _resolve_dsn(connection_ref)
        schema = namespace.parts[0] if namespace.parts else "public"
        results = []
        try:
            with psycopg2.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT t.table_name, t.table_type,
                               c.reltuples::bigint AS row_estimate,
                               pg_total_relation_size(c.oid) AS total_bytes
                        FROM information_schema.tables t
                        LEFT JOIN pg_namespace n ON n.nspname = t.table_schema
                        LEFT JOIN pg_class c ON c.relname = t.table_name
                            AND c.relnamespace = n.oid
                        WHERE t.table_schema = %s
                        ORDER BY t.table_name
                        """,
                        (schema,),
                    )
                    for name, kind, row_est, total_bytes in cur.fetchall():
                        rel_kind = (
                            "view" if kind == "VIEW"
                            else "materialized_view" if kind == "MATERIALIZED VIEW"
                            else "table"
                        )
                        row_count = int(row_est) if row_est is not None and row_est >= 0 else None
                        size_bytes = int(total_bytes) if total_bytes is not None else None
                        metrics: dict = {}
                        if row_count is not None:
                            metrics["row_count_is_estimate"] = True
                        results.append(
                            RelationSummary(
                                namespace=namespace,
                                name=name,
                                relation_kind=rel_kind,
                                row_count=row_count,
                                size_bytes=size_bytes,
                                metrics=metrics,
                            )
                        )
        except Exception as exc:
            logger.warning("list_relations failed: %s", exc)
        return results

    def describe_relations(
        self,
        connection_ref: dict[str, Any],
        refs: list[RelationSummary],
    ) -> DiscoveryBundle:
        # Phase 1 stub — discovery is currently handled by the existing
        # data-discovery skill scripts.  This method returns an empty
        # bundle so the interface is satisfied; Phase 2 will fill it.
        return DiscoveryBundle(
            platform_type=PLATFORM_TYPE,
            platform_instance_id=connection_ref.get("connection_id", "unknown"),
            warnings=["describe_relations: delegating to skill scripts in Phase 1"],
        )

    def list_columns(
        self,
        connection_ref: dict[str, Any],
        relation: RelationSummary,
    ) -> list[ColumnInfo]:
        import psycopg2
        dsn = _resolve_dsn(connection_ref)
        schema = relation.namespace.parts[0] if relation.namespace.parts else "public"
        out: list[ColumnInfo] = []
        try:
            with psycopg2.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT column_name, data_type, is_nullable, ordinal_position "
                        "FROM information_schema.columns "
                        "WHERE table_schema = %s AND table_name = %s "
                        "ORDER BY ordinal_position",
                        (schema, relation.name),
                    )
                    for name, dtype, nullable, ordinal in cur.fetchall():
                        out.append(ColumnInfo(
                            name=name, data_type=dtype,
                            nullable=(nullable == "YES"), ordinal=ordinal,
                        ))
        except Exception as exc:
            logger.warning("postgres list_columns failed: %s", exc)
        return out

    def list_foreign_keys(
        self,
        connection_ref: dict[str, Any],
        schema: str,
    ) -> list[ForeignKeyInfo]:
        import psycopg2
        dsn = _resolve_dsn(connection_ref)
        out: list[ForeignKeyInfo] = []
        try:
            with psycopg2.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            kcu.table_schema, kcu.table_name, kcu.column_name,
                            ccu.table_schema AS to_schema,
                            ccu.table_name   AS to_table,
                            ccu.column_name  AS to_column
                        FROM information_schema.key_column_usage kcu
                        JOIN information_schema.referential_constraints rc
                            ON rc.constraint_name   = kcu.constraint_name
                           AND rc.constraint_schema = kcu.constraint_schema
                        JOIN information_schema.constraint_column_usage ccu
                            ON ccu.constraint_name   = rc.unique_constraint_name
                           AND ccu.constraint_schema = rc.unique_constraint_schema
                        WHERE kcu.table_schema = %s
                        ORDER BY kcu.table_name, kcu.column_name
                        """,
                        (schema,),
                    )
                    for row in cur.fetchall():
                        out.append(ForeignKeyInfo(
                            from_schema=row[0], from_table=row[1], from_column=row[2],
                            to_schema=row[3], to_table=row[4], to_column=row[5],
                        ))
        except Exception as exc:
            logger.warning("postgres list_foreign_keys failed for schema %s: %s", schema, exc)
        return out


class PostgresQueryExecutor:
    """Runs a single SELECT against Postgres inside a read-only transaction with
    a row cap + statement timeout. Returns a neutral ``ResultSet``; the facade
    owns the safety gate + audit. Raises the driver exception on failure — the
    facade catches it and calls ``classify_error``."""

    def explain(
        self,
        connection_ref: dict[str, Any],
        query: str,
        limits: QueryLimits,
    ) -> ValidationReport:
        # A live EXPLAIN needs a table context (sample_from) that isn't part of
        # this SPI — the facade's validate_predicate handles the dry-run.
        return ValidationReport(valid=True)

    def select(
        self,
        connection_ref: dict[str, Any],
        query: str,
        limits: QueryLimits,
    ) -> ResultSet:
        import time
        import psycopg2
        from ._exec_util import pg_typname, serialize_row, SELECT_TIMEOUT_MS
        dsn = _resolve_dsn(connection_ref)
        max_rows = max(1, min(limits.max_rows, 1000))
        started = time.monotonic()
        bare = query.rstrip().rstrip(";")
        # Inline the LIMIT as an int literal (our own clamped int) — a bind param
        # would make psycopg2 treat a literal '%' in the SQL as a placeholder.
        limit_n = int(max_rows) + 1
        wrapped = f"SELECT * FROM ({bare}) _wb_sub LIMIT {limit_n}"
        conn = None
        try:
            conn = psycopg2.connect(dsn)
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute("SET default_transaction_read_only = ON")
                cur.execute(f"SET statement_timeout = {limits.timeout_ms or SELECT_TIMEOUT_MS}")
                cur.execute(wrapped)
                raw_rows = cur.fetchall()
                description = cur.description or []
            conn.rollback()
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        truncated = len(raw_rows) > max_rows
        kept = raw_rows[:max_rows]
        columns = [{"name": d.name, "dataType": pg_typname(d.type_code)} for d in description]
        return ResultSet(
            columns=columns,
            rows=[serialize_row(r) for r in kept],
            truncated=truncated,
            row_count=len(kept),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def classify_error(self, exception: Exception) -> str:
        try:
            import psycopg2
            if isinstance(exception, psycopg2.errors.InsufficientPrivilege):
                return "permission_denied"
            if isinstance(exception, psycopg2.errors.QueryCanceled):
                return "timeout"
            if isinstance(exception, psycopg2.OperationalError):
                return "connection_error"
            if isinstance(exception, psycopg2.Error):
                return "sql_error"
        except Exception:
            pass
        return "unknown"


class PostgresDeploymentProvider:
    """Owns Postgres view deployment: CREATE/DROP VIEW + per-view smoke tests,
    plus the view-recreate recovery for a column-type/shape drift that
    ``CREATE OR REPLACE VIEW`` can't handle. Returns a neutral ``DeploymentRun``
    (never raises a vendor exception past this boundary)."""

    def plan(
        self, artifact: DdlArtifact, current_state: Optional[dict[str, Any]]
    ) -> DeploymentChangeSet:
        return DeploymentChangeSet(artifact=artifact)

    def apply(
        self, change_set: DeploymentChangeSet, idempotency_key: str
    ) -> DeploymentRun:
        # The sql_executor facade calls deploy_views directly (it carries the
        # connection_ref + view_schema). apply() satisfies the SPI shape.
        return DeploymentRun(
            run_id=idempotency_key or str(uuid.uuid4()),
            status="failed",
            error_message="Use deploy_views(connection_ref, ddl, view_schema, view_names).",
        )

    def verify(self, run: DeploymentRun) -> VerificationReport:
        return VerificationReport(passed=run.status == "succeeded")

    def compensate(self, run: DeploymentRun) -> CompensationReport:
        return CompensationReport(compensated=False, warnings=["compensation not yet implemented"])

    def deploy_views(
        self,
        connection_ref: dict[str, Any],
        ddl: str,
        view_schema: str,
        view_names: list,
    ) -> DeploymentRun:
        import time
        import psycopg2
        from ._exec_util import DDL_TIMEOUT_MS
        dsn = _resolve_dsn(connection_ref)
        started = time.monotonic()

        def _ms() -> int:
            return int((time.monotonic() - started) * 1000)

        def _bare_names() -> list[str]:
            return [n.rsplit(".", 1)[-1].strip('"') for n in view_names if n]

        conn = None
        try:
            conn = psycopg2.connect(dsn)
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = {DDL_TIMEOUT_MS}")
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{view_schema}"')
                cur.execute(ddl)
                smoke = []
                for bare in _bare_names():
                    if not bare:
                        continue
                    cur.execute(f'SELECT 1 FROM "{view_schema}"."{bare}" LIMIT 1')
                    smoke.append(bare)
            conn.commit()
            return DeploymentRun(run_id="", status="succeeded",
                                 deployed_objects=smoke, duration_ms=_ms())
        except psycopg2.errors.InsufficientPrivilege as e:
            _rollback(conn)
            return DeploymentRun(run_id="", status="failed", duration_ms=_ms(),
                                 error_class="permission_denied", error_message=str(e))
        except psycopg2.errors.QueryCanceled as e:
            _rollback(conn)
            return DeploymentRun(run_id="", status="failed", duration_ms=_ms(),
                                 error_class="timeout", error_message=str(e))
        except psycopg2.OperationalError as e:
            _rollback(conn)
            return DeploymentRun(run_id="", status="failed", duration_ms=_ms(),
                                 error_class="connection_error", error_message=str(e))
        except psycopg2.Error as e:
            _rollback(conn)
            # `CREATE OR REPLACE VIEW` can ADD columns + CHANGE expressions but
            # cannot CHANGE a column's type, REMOVE, or RENAME columns. On those
            # specific errors, drop the view(s) (NON-CASCADE, so dependents surface
            # as a clear error) and re-run. Idempotent on first deploys.
            err_msg = str(e).lower()
            is_view_schema_drift = any(
                phrase in err_msg for phrase in (
                    "cannot change data type of view column",
                    "cannot drop columns from view",
                    "cannot change name of view column",
                    "cannot change number of columns in view",
                )
            )
            if is_view_schema_drift and view_names:
                retry = None
                try:
                    retry = psycopg2.connect(dsn)
                    retry.autocommit = False
                    with retry.cursor() as cur:
                        cur.execute(f"SET statement_timeout = {DDL_TIMEOUT_MS}")
                        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{view_schema}"')
                        for bare in _bare_names():
                            if not bare:
                                continue
                            cur.execute(f'DROP VIEW IF EXISTS "{view_schema}"."{bare}"')
                        cur.execute(ddl)
                        smoke = []
                        for bare in _bare_names():
                            if not bare:
                                continue
                            cur.execute(f'SELECT 1 FROM "{view_schema}"."{bare}" LIMIT 1')
                            smoke.append(bare)
                    retry.commit()
                    return DeploymentRun(run_id="", status="succeeded",
                                         deployed_objects=smoke, duration_ms=_ms())
                except psycopg2.errors.DependentObjectsStillExist as e2:
                    _rollback(retry)
                    return DeploymentRun(
                        run_id="", status="failed", duration_ms=_ms(),
                        error_class="dependents_block_redeploy",
                        error_message=(
                            "Cannot redeploy because the view is referenced by other "
                            "objects downstream. Drop the dependents first or coordinate "
                            f"a schema migration. Postgres said: {e2}"
                        ),
                    )
                except psycopg2.Error as e2:
                    _rollback(retry)
                    return DeploymentRun(run_id="", status="failed", duration_ms=_ms(),
                                         error_class="sql_error", error_message=str(e2))
                finally:
                    if retry:
                        try:
                            retry.close()
                        except Exception:
                            pass
            return DeploymentRun(run_id="", status="failed", duration_ms=_ms(),
                                 error_class="sql_error", error_message=str(e))
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass


# ── helpers ───────────────────────────────────────────────────────────────────

def _rollback(conn) -> None:
    if conn:
        try:
            conn.rollback()
        except Exception:
            pass


def _resolve_dsn(connection_ref: dict[str, Any]) -> str:
    """Render a Postgres DSN from a connection reference.

    Accepts a transient ``dsn`` key (the sql_executor facade threads the
    resolved DSN through this during the pg_connection→structured migration) or,
    preferably, the STRUCTURED fields (host/port/database/username/
    resolved_password) written by ``resolve_source_connection_ref``. The
    ``resolved_password`` is ephemeral — never persisted."""
    dsn = connection_ref.get("dsn")
    if dsn:
        return dsn
    host = connection_ref.get("host", "localhost")
    port = connection_ref.get("port", 5432)
    database = connection_ref.get("database", "")
    username = connection_ref.get("username", "")
    password = connection_ref.get("resolved_password", "")
    from urllib.parse import quote
    user_part = f"{quote(str(username), safe='')}:{quote(str(password), safe='')}@" if username else ""
    return f"postgresql://{user_part}{host}:{port}/{database}"
