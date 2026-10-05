"""MySQL discovery provider.

Implements ConnectionProvider and DiscoveryProvider for MySQL 5.7+ / 8.x
using pymysql.  Mirrors the logic in the data-discovery-mysql skill scripts
(discover_schemas.py / discover_tables.py / extract_metadata.py) behind the
platform SPI so new code paths (MCP tools, namespace browser) can call it
without spawning a subprocess.

The skill scripts are NOT replaced — they remain the SDK entry point.
This provider is additive: it serves direct Python callers only.

Only pymysql is imported inside method bodies (lazy import) so the module
loads cleanly in test environments that don't have pymysql installed.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlparse, unquote

from ..interfaces import (
    CapabilityEvidence,
    CapabilityLevel,
    ColumnInfo,
    DeploymentRun,
    DiscoveryBundle,
    ForeignKeyInfo,
    ManagedConnection,
    NamespaceRef,
    QueryLimits,
    RelationSummary,
    ResultSet,
    ValidationReport,
)

logger = logging.getLogger(__name__)

PLATFORM_TYPE = "mysql"

# System schemas that are never user data.
_SYSTEM_SCHEMAS = frozenset(
    {"information_schema", "performance_schema", "mysql", "sys"}
)


class MySQLConnectionProvider:
    """Validates config and probes a live MySQL connection."""

    def validate_config(
        self, public_config: dict[str, Any], secret_ref: str
    ) -> ValidationReport:
        errors = []
        if not public_config.get("host"):
            errors.append("host is required")
        if not public_config.get("database"):
            errors.append("database is required")
        if not secret_ref:
            errors.append("secret_ref is required (never pass plaintext passwords)")
        return ValidationReport(valid=not errors, errors=errors)

    def probe(self, connection_ref: dict[str, Any]) -> CapabilityEvidence:
        import pymysql
        evidence = CapabilityEvidence(
            platform_type=PLATFORM_TYPE,
            instance_id=connection_ref.get("connection_id", "unknown"),
        )
        try:
            kwargs = _resolve_connect_kwargs(connection_ref)
            with pymysql.connect(**kwargs) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT VERSION()")
                    row = cur.fetchone()
                    evidence.server_version = row[0] if row else None
            evidence.capabilities = {
                "connection": CapabilityLevel.EXPERIMENTAL,
                "discovery": CapabilityLevel.EXPERIMENTAL,
            }
        except Exception as exc:
            evidence.warnings.append(f"probe failed: {exc}")
        return evidence

    def open(
        self, connection_ref: dict[str, Any], purpose: str
    ) -> ManagedConnection:
        import pymysql
        kwargs = _resolve_connect_kwargs(connection_ref)
        conn = pymysql.connect(**kwargs)
        return ManagedConnection(
            platform_type=PLATFORM_TYPE,
            instance_id=connection_ref.get("connection_id", "unknown"),
            purpose=purpose,
            _internal=conn,
        )


class MySQLDiscoveryProvider:
    """Lists schemas and tables; extracts per-table metadata from MySQL."""

    def list_namespaces(
        self,
        connection_ref: dict[str, Any],
        parent: Optional[str] = None,
    ) -> list[NamespaceRef]:
        import pymysql
        kwargs = _resolve_connect_kwargs(connection_ref)
        instance_id = connection_ref.get("connection_id", "unknown")
        results = []
        try:
            with pymysql.connect(**kwargs) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT SCHEMA_NAME
                        FROM information_schema.SCHEMATA
                        ORDER BY SCHEMA_NAME
                        """
                    )
                    for (name,) in cur.fetchall():
                        if name.lower() in _SYSTEM_SCHEMAS:
                            continue
                        results.append(
                            NamespaceRef(
                                platform_instance_id=instance_id,
                                parts=[name],
                                labels={"schema": name},
                            )
                        )
        except Exception as exc:
            logger.warning("mysql list_namespaces failed: %s", exc)
        return results

    def list_relations(
        self,
        connection_ref: dict[str, Any],
        namespace: NamespaceRef,
    ) -> list[RelationSummary]:
        import pymysql
        kwargs = _resolve_connect_kwargs(connection_ref)
        schema = namespace.parts[0] if namespace.parts else ""
        results = []
        try:
            with pymysql.connect(**kwargs) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT TABLE_NAME, TABLE_TYPE, TABLE_ROWS,
                               DATA_LENGTH + INDEX_LENGTH AS total_bytes,
                               UPDATE_TIME
                        FROM information_schema.TABLES
                        WHERE TABLE_SCHEMA = %s
                          AND TABLE_TYPE IN ('BASE TABLE', 'VIEW')
                        ORDER BY TABLE_NAME
                        """,
                        (schema,),
                    )
                    for name, table_type, row_est, total_bytes, update_time in cur.fetchall():
                        rel_kind = "view" if table_type == "VIEW" else "table"
                        row_count = int(row_est) if row_est is not None else None
                        size_bytes = int(total_bytes) if total_bytes is not None else None
                        last_mod = str(update_time) if update_time else None
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
                                last_modified=last_mod,
                                metrics=metrics,
                            )
                        )
        except Exception as exc:
            logger.warning("mysql list_relations failed: %s", exc)
        return results

    def describe_relations(
        self,
        connection_ref: dict[str, Any],
        refs: list[RelationSummary],
    ) -> DiscoveryBundle:
        import pymysql
        kwargs = _resolve_connect_kwargs(connection_ref)
        instance_id = connection_ref.get("connection_id", "unknown")
        relations: list[dict[str, Any]] = []
        warnings: list[str] = []

        try:
            with pymysql.connect(**kwargs) as conn:
                with conn.cursor() as cur:
                    for ref in refs:
                        schema = ref.namespace.parts[0] if ref.namespace.parts else ""
                        table = ref.name
                        try:
                            meta = _extract_table_metadata(cur, schema, table)
                            relations.append(meta)
                        except Exception as exc:
                            warnings.append(f"{schema}.{table}: {exc}")
        except Exception as exc:
            warnings.append(f"connection failed: {exc}")

        return DiscoveryBundle(
            platform_type=PLATFORM_TYPE,
            platform_instance_id=instance_id,
            relations=relations,
            warnings=warnings,
        )

    def list_columns(
        self,
        connection_ref: dict[str, Any],
        relation: RelationSummary,
    ) -> list[ColumnInfo]:
        import pymysql
        kwargs = _resolve_connect_kwargs(connection_ref)
        schema = relation.namespace.parts[0] if relation.namespace.parts else ""
        out: list[ColumnInfo] = []
        try:
            with pymysql.connect(**kwargs) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, ORDINAL_POSITION "
                        "FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                        "ORDER BY ORDINAL_POSITION",
                        (schema, relation.name),
                    )
                    for name, dtype, nullable, ordinal in cur.fetchall():
                        out.append(ColumnInfo(
                            name=name, data_type=dtype,
                            nullable=(nullable == "YES"), ordinal=ordinal,
                        ))
        except Exception as exc:
            logger.warning("mysql list_columns failed: %s", exc)
        return out

    def list_foreign_keys(
        self,
        connection_ref: dict[str, Any],
        schema: str,
    ) -> list[ForeignKeyInfo]:
        import pymysql
        kwargs = _resolve_connect_kwargs(connection_ref)
        out: list[ForeignKeyInfo] = []
        try:
            with pymysql.connect(**kwargs) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME,
                            REFERENCED_TABLE_SCHEMA, REFERENCED_TABLE_NAME,
                            REFERENCED_COLUMN_NAME
                        FROM information_schema.KEY_COLUMN_USAGE
                        WHERE TABLE_SCHEMA = %s
                          AND REFERENCED_TABLE_NAME IS NOT NULL
                        ORDER BY TABLE_NAME, COLUMN_NAME
                        """,
                        (schema,),
                    )
                    for row in cur.fetchall():
                        out.append(ForeignKeyInfo(
                            from_schema=row[0], from_table=row[1], from_column=row[2],
                            to_schema=row[3], to_table=row[4], to_column=row[5],
                        ))
        except Exception as exc:
            logger.warning("mysql list_foreign_keys failed for schema %s: %s", schema, exc)
        return out


class MySQLQueryExecutor:
    """Runs a single SELECT against MySQL via pymysql. Neutral ResultSet;
    raises on driver/import error (the facade classifies + audits)."""

    def explain(self, connection_ref, query, limits: QueryLimits) -> ValidationReport:
        return ValidationReport(valid=True)

    def select(self, connection_ref: dict[str, Any], query: str,
               limits: QueryLimits) -> ResultSet:
        import time
        import pymysql  # ImportError → facade maps to platform_not_supported
        from ._exec_util import generic_typename, serialize_row, SELECT_TIMEOUT_MS
        max_rows = max(1, min(limits.max_rows, 1000))
        started = time.monotonic()
        bare = query.rstrip().rstrip(";")
        wrapped = f"SELECT * FROM ({bare}) _wb_sub LIMIT {int(max_rows) + 1}"
        conn = None
        try:
            conn = pymysql.connect(
                host=connection_ref.get("host", "localhost"),
                port=int(connection_ref.get("port", 3306)),
                user=connection_ref.get("username", ""),
                password=connection_ref.get("resolved_password", ""),
                database=connection_ref.get("database", "") or None,
                connect_timeout=60,
            )
            with conn.cursor() as cur:
                cur.execute(f"SET @@SESSION.MAX_EXECUTION_TIME = {limits.timeout_ms or SELECT_TIMEOUT_MS}")
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
        columns = [{"name": d[0], "dataType": generic_typename(d[1])} for d in description]
        return ResultSet(columns=columns, rows=[serialize_row(r) for r in kept],
                         truncated=truncated, row_count=len(kept),
                         duration_ms=int((time.monotonic() - started) * 1000))

    def classify_error(self, exception: Exception) -> str:
        name = type(exception).__name__
        return "connection_error" if name == "OperationalError" else "sql_error"


class MySQLDeploymentProvider:
    """Deploys CREATE/DROP VIEW DDL to MySQL via pymysql. Neutral DeploymentRun."""

    def plan(self, artifact, current_state):
        from ..interfaces import DeploymentChangeSet
        return DeploymentChangeSet(artifact=artifact)

    def apply(self, change_set, idempotency_key):
        return DeploymentRun(run_id=idempotency_key or "", status="failed",
                             error_message="Use deploy_views(...).")

    def verify(self, run):
        from ..interfaces import VerificationReport
        return VerificationReport(passed=run.status == "succeeded")

    def compensate(self, run):
        from ..interfaces import CompensationReport
        return CompensationReport(compensated=False)

    def deploy_views(self, connection_ref: dict[str, Any], ddl: str,
                     view_schema: str, view_names: list) -> DeploymentRun:
        import time
        import sqlparse
        from ._exec_util import rewrite_ddl_header_for_mysql
        started = time.monotonic()

        def _ms() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            import pymysql
        except ImportError:
            return DeploymentRun(run_id="", status="failed", duration_ms=_ms(),
                                 error_class="platform_not_supported",
                                 error_message="pymysql driver not installed. Ensure pymysql>=1.0 is in requirements.")
        conn = None
        try:
            conn = pymysql.connect(
                host=connection_ref.get("host", "localhost"),
                port=int(connection_ref.get("port", 3306)),
                user=connection_ref.get("username", ""),
                password=connection_ref.get("resolved_password", ""),
                database=connection_ref.get("database", "") or None,
                connect_timeout=60,
            )
            with conn.cursor() as cur:
                cur.execute(f"CREATE DATABASE IF NOT EXISTS `{view_schema}`")
                for raw_stmt in (sqlparse.split(ddl) or []):
                    stmt = raw_stmt.strip().rstrip(";").strip()
                    if not stmt:
                        continue
                    cur.execute(rewrite_ddl_header_for_mysql(stmt))
                smoke = []
                for raw_name in view_names:
                    bare = raw_name.rsplit(".", 1)[-1].strip('"').strip("`")
                    if not bare:
                        continue
                    cur.execute(f"SELECT 1 FROM `{view_schema}`.`{bare}` LIMIT 1")
                    smoke.append(bare)
            conn.commit()
            return DeploymentRun(run_id="", status="succeeded",
                                 deployed_objects=smoke, duration_ms=_ms())
        except pymysql.OperationalError as e:
            _rollback(conn)
            return DeploymentRun(run_id="", status="failed", duration_ms=_ms(),
                                 error_class="connection_error", error_message=str(e))
        except pymysql.Error as e:
            _rollback(conn)
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


def _resolve_connect_kwargs(connection_ref: dict[str, Any]) -> dict[str, Any]:
    """Build a pymysql.connect() kwargs dict from a connection reference.

    Accepts either:
      - A `mysql_connection` key with a `mysql://user:pass@host:port/db` DSN
      - Explicit host/port/user/database + resolved_password keys
    """
    if "mysql_connection" in connection_ref:
        return _parse_mysql_dsn(connection_ref["mysql_connection"])
    return {
        "host": connection_ref.get("host", "localhost"),
        "port": int(connection_ref.get("port", 3306)),
        "user": connection_ref.get("username", ""),
        # password MUST come from a secret resolver; see postgres.py for rationale.
        "password": connection_ref.get("resolved_password", ""),
        "database": connection_ref.get("database", ""),
        "connect_timeout": 10,
    }


def _parse_mysql_dsn(dsn: str) -> dict[str, Any]:
    p = urlparse(dsn)
    return {
        "host": p.hostname or "localhost",
        "port": p.port or 3306,
        "user": unquote(p.username or ""),
        "password": unquote(p.password or ""),
        "database": (p.path or "/").lstrip("/"),
        "connect_timeout": 10,
    }


def _extract_table_metadata(cur, schema: str, table: str) -> dict[str, Any]:
    """Run the six information_schema queries for one table.

    Mirrors extract_metadata.py's per-table extraction.  Returns a dict
    with keys: schema, table, comment, columns, primary_keys, foreign_keys,
    unique_constraints, indexes, check_constraints.
    """
    # 1. Table comment
    cur.execute(
        "SELECT TABLE_COMMENT FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
        (schema, table),
    )
    row = cur.fetchone()
    table_comment = row[0] if row else ""

    # 2. Columns
    cur.execute(
        """
        SELECT COLUMN_NAME, ORDINAL_POSITION, DATA_TYPE,
               CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE,
               IS_NULLABLE, COLUMN_DEFAULT, COLUMN_COMMENT
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
        """,
        (schema, table),
    )
    columns = [
        {
            "name": r[0],
            "ordinal": r[1],
            "data_type": r[2],
            "char_max_length": r[3],
            "numeric_precision": r[4],
            "numeric_scale": r[5],
            "is_nullable": r[6] == "YES",
            "default": r[7],
            "comment": r[8] or "",
        }
        for r in cur.fetchall()
    ]

    # 3. Primary keys
    cur.execute(
        """
        SELECT COLUMN_NAME
        FROM information_schema.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
          AND CONSTRAINT_NAME = 'PRIMARY'
        ORDER BY ORDINAL_POSITION
        """,
        (schema, table),
    )
    primary_keys = [r[0] for r in cur.fetchall()]

    # 4. Foreign keys
    cur.execute(
        """
        SELECT kcu.CONSTRAINT_NAME,
               kcu.COLUMN_NAME,
               kcu.REFERENCED_TABLE_SCHEMA,
               kcu.REFERENCED_TABLE_NAME,
               kcu.REFERENCED_COLUMN_NAME,
               rc.DELETE_RULE,
               rc.UPDATE_RULE
        FROM information_schema.KEY_COLUMN_USAGE kcu
        JOIN information_schema.REFERENTIAL_CONSTRAINTS rc
          ON rc.CONSTRAINT_NAME   = kcu.CONSTRAINT_NAME
         AND rc.CONSTRAINT_SCHEMA = kcu.TABLE_SCHEMA
        WHERE kcu.TABLE_SCHEMA = %s AND kcu.TABLE_NAME = %s
          AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
        ORDER BY kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
        """,
        (schema, table),
    )
    foreign_keys = [
        {
            "constraint_name": r[0],
            "column": r[1],
            "ref_schema": r[2],
            "ref_table": r[3],
            "ref_column": r[4],
            "on_delete": r[5],
            "on_update": r[6],
        }
        for r in cur.fetchall()
    ]

    # 5. Unique constraints
    cur.execute(
        """
        SELECT kcu.CONSTRAINT_NAME, kcu.COLUMN_NAME
        FROM information_schema.KEY_COLUMN_USAGE kcu
        JOIN information_schema.TABLE_CONSTRAINTS tc
          ON tc.CONSTRAINT_NAME   = kcu.CONSTRAINT_NAME
         AND tc.TABLE_SCHEMA      = kcu.TABLE_SCHEMA
         AND tc.TABLE_NAME        = kcu.TABLE_NAME
        WHERE kcu.TABLE_SCHEMA = %s AND kcu.TABLE_NAME = %s
          AND tc.CONSTRAINT_TYPE = 'UNIQUE'
        ORDER BY kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
        """,
        (schema, table),
    )
    unique_constraints: dict[str, list[str]] = {}
    for constraint, col in cur.fetchall():
        unique_constraints.setdefault(constraint, []).append(col)

    # 6. Indexes
    cur.execute(
        """
        SELECT INDEX_NAME,
               GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS cols,
               NON_UNIQUE
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
          AND INDEX_NAME <> 'PRIMARY'
        GROUP BY INDEX_NAME, NON_UNIQUE
        ORDER BY INDEX_NAME
        """,
        (schema, table),
    )
    indexes = [
        {"name": r[0], "columns": r[1].split(",") if r[1] else [], "non_unique": bool(r[2])}
        for r in cur.fetchall()
    ]

    # 7. Check constraints (silently skipped on MySQL 5.x which lacks the table)
    check_constraints: list[dict[str, Any]] = []
    try:
        cur.execute(
            """
            SELECT cc.CONSTRAINT_NAME, cc.CHECK_CLAUSE
            FROM information_schema.TABLE_CONSTRAINTS tc
            JOIN information_schema.CHECK_CONSTRAINTS cc
              ON cc.CONSTRAINT_NAME   = tc.CONSTRAINT_NAME
             AND cc.CONSTRAINT_SCHEMA = tc.TABLE_SCHEMA
            WHERE tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME = %s
              AND tc.CONSTRAINT_TYPE = 'CHECK'
            """,
            (schema, table),
        )
        check_constraints = [{"name": r[0], "clause": r[1]} for r in cur.fetchall()]
    except Exception:
        pass

    return {
        "schema": schema,
        "table": table,
        "comment": table_comment,
        "columns": columns,
        "primary_keys": primary_keys,
        "foreign_keys": foreign_keys,
        "unique_constraints": [
            {"name": k, "columns": v} for k, v in unique_constraints.items()
        ],
        "indexes": indexes,
        "check_constraints": check_constraints,
    }
