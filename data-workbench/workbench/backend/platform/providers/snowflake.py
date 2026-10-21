"""Snowflake discovery provider.

Implements ConnectionProvider and DiscoveryProvider for Snowflake
using snowflake-connector-python.  Mirrors the logic that would live in
a data-discovery-snowflake skill behind the platform SPI so new code
paths (MCP tools, namespace browser) can call it without spawning a
subprocess.

The skill scripts are NOT replaced — they remain the SDK entry point.
This provider is additive: it serves direct Python callers only.

Only snowflake.connector is imported inside method bodies (lazy import)
so the module loads cleanly in test environments that don't have the
connector installed.

Connection reference keys expected:
  host             — Snowflake account identifier (e.g. myorg.us-east-1.snowflakecomputing.com)
  database         — Snowflake database
  username         — Snowflake user
  resolved_password — Snowflake password (transient — NEVER log or persist)
  extra_config:
    warehouse      — required for most operations
    role           — optional Snowflake role
    schema         — optional default schema
"""
from __future__ import annotations

import logging
from typing import Any, Optional

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

PLATFORM_TYPE = "snowflake"

# Schemas that are never user data. PUBLIC is a *normal* user schema in
# Snowflake (the default schema of every database) — it is NOT filtered.
_SYSTEM_SCHEMAS = frozenset({"information_schema", "account_usage"})


class SnowflakeConnectionProvider:
    """Validates config and probes a live Snowflake connection."""

    def validate_config(
        self, public_config: dict[str, Any], secret_ref: str
    ) -> ValidationReport:
        errors = []
        if not public_config.get("host"):
            errors.append("host (Snowflake account identifier) is required")
        if not public_config.get("database"):
            errors.append("database is required")
        extra = public_config.get("extra_config", {})
        if not extra.get("warehouse"):
            errors.append("extra_config.warehouse is required")
        if not secret_ref:
            errors.append("secret_ref is required (never pass plaintext passwords)")
        return ValidationReport(valid=not errors, errors=errors)

    def probe(self, connection_ref: dict[str, Any]) -> CapabilityEvidence:
        import snowflake.connector
        evidence = CapabilityEvidence(
            platform_type=PLATFORM_TYPE,
            instance_id=connection_ref.get("connection_id", "unknown"),
        )
        conn = None
        try:
            kwargs = _resolve_connect_kwargs(connection_ref)
            conn = snowflake.connector.connect(**kwargs)
            cur = conn.cursor()
            cur.execute("SELECT CURRENT_VERSION()")
            row = cur.fetchone()
            evidence.server_version = row[0] if row else None
            cur.close()
            evidence.capabilities = {
                "connection": CapabilityLevel.EXPERIMENTAL,
                "discovery": CapabilityLevel.EXPERIMENTAL,
            }
        except Exception as exc:
            evidence.warnings.append(f"probe failed: {exc}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        return evidence

    def open(
        self, connection_ref: dict[str, Any], purpose: str
    ) -> ManagedConnection:
        import snowflake.connector
        kwargs = _resolve_connect_kwargs(connection_ref)
        conn = snowflake.connector.connect(**kwargs)
        return ManagedConnection(
            platform_type=PLATFORM_TYPE,
            instance_id=connection_ref.get("connection_id", "unknown"),
            purpose=purpose,
            _internal=conn,
        )


class SnowflakeDiscoveryProvider:
    """Lists schemas and tables; extracts per-table metadata from Snowflake."""

    def list_catalogs_and_schemas(
        self, connection_ref: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Enumerate the write-target namespace tree for the serving Target-namespace
        picker. Mirrors the Databricks provider's method (the serving endpoint
        dispatches generically), but is **scoped to the connection's database** — a
        shared Snowflake account can hold thousands of databases, and a per-database
        `SHOW SCHEMAS` fan-out makes the picker hang (>60s). The connection's own
        database is the serving/materialization target ~99% of the time
        (e.g. `DWB_SERVING_DB`); to serve into a different database the user types the
        `database.schema` directly (the field is a free-text input). When the
        connection has no database, list database *names* only (one cheap query) so a
        catalog can still be picked, schemas typed.
        """
        import snowflake.connector
        kwargs = _resolve_connect_kwargs(connection_ref)
        db = kwargs.get("database")
        out: list[dict[str, Any]] = []
        conn = None
        try:
            conn = snowflake.connector.connect(**kwargs)
            cur = conn.cursor()
            if db:
                # One query: schemas in the connected database.
                cur.execute(f'SHOW SCHEMAS IN DATABASE "{db}"')
                cols = [c[0].lower() for c in cur.description]
                sidx = cols.index("name") if "name" in cols else 1
                schemas = [
                    r[sidx] for r in cur.fetchall()
                    if r[sidx].lower() != "information_schema"
                ]
                out.append({"catalog": db, "schemas": sorted(schemas)})
            else:
                # No connected database — list database names only (cheap; no
                # per-database schema fetch). Schemas are typed by the user.
                cur.execute("SHOW DATABASES")
                cols = [c[0].lower() for c in cur.description]
                name_idx = cols.index("name") if "name" in cols else 1
                for row in cur.fetchall():
                    name = row[name_idx]
                    if name.lower() not in ("snowflake", "snowflake_sample_data"):
                        out.append({"catalog": name, "schemas": []})
        except Exception as exc:  # noqa: BLE001
            logger.warning("snowflake list_catalogs_and_schemas failed: %s", exc)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        return out

    def list_namespaces(
        self,
        connection_ref: dict[str, Any],
        parent: Optional[str] = None,
    ) -> list[NamespaceRef]:
        import snowflake.connector
        kwargs = _resolve_connect_kwargs(connection_ref)
        instance_id = connection_ref.get("connection_id", "unknown")
        results = []
        conn = None
        try:
            conn = snowflake.connector.connect(**kwargs)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT SCHEMA_NAME
                FROM INFORMATION_SCHEMA.SCHEMATA
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
            cur.close()
        except Exception as exc:
            logger.warning("snowflake list_namespaces failed: %s", exc)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        return results

    def list_relations(
        self,
        connection_ref: dict[str, Any],
        namespace: NamespaceRef,
    ) -> list[RelationSummary]:
        import snowflake.connector
        kwargs = _resolve_connect_kwargs(connection_ref)
        schema = namespace.parts[0] if namespace.parts else ""
        results = []
        conn = None
        try:
            conn = snowflake.connector.connect(**kwargs)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT TABLE_NAME, TABLE_TYPE, ROW_COUNT, BYTES, LAST_ALTERED, COMMENT
                FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_SCHEMA = %s
                  AND TABLE_TYPE IN ('BASE TABLE', 'VIEW')
                ORDER BY TABLE_NAME
                """,
                (schema,),
            )
            for row in cur.fetchall():
                name, table_type = row[0], row[1]
                row_count = row[2]
                bytes_val = row[3]
                last_altered = row[4]
                comment = row[5]
                kind = "view" if table_type == "VIEW" else "table"
                last_mod_str = str(last_altered) if last_altered else None
                metrics: dict = {}
                if row_count is not None:
                    metrics["row_count_is_estimate"] = True
                results.append(
                    RelationSummary(
                        namespace=namespace,
                        name=name,
                        relation_kind=kind,
                        row_count=row_count,
                        size_bytes=int(bytes_val) if bytes_val is not None else None,
                        last_modified=last_mod_str,
                        metrics=metrics,
                        comment=comment or None,
                    )
                )
            cur.close()
        except Exception as exc:
            logger.warning("snowflake list_relations failed: %s", exc)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        return results

    def describe_relations(
        self,
        connection_ref: dict[str, Any],
        refs: list[RelationSummary],
    ) -> DiscoveryBundle:
        import snowflake.connector
        kwargs = _resolve_connect_kwargs(connection_ref)
        instance_id = connection_ref.get("connection_id", "unknown")
        relations: list[dict[str, Any]] = []
        warnings: list[str] = []

        conn = None
        try:
            conn = snowflake.connector.connect(**kwargs)
            cur = conn.cursor()
            for ref in refs:
                schema = ref.namespace.parts[0] if ref.namespace.parts else ""
                table = ref.name
                try:
                    meta = _extract_table_metadata(cur, schema, table)
                    relations.append(meta)
                except Exception as exc:
                    warnings.append(f"{schema}.{table}: {exc}")
            cur.close()
        except Exception as exc:
            warnings.append(f"connection failed: {exc}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

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
        import snowflake.connector
        kwargs = _resolve_connect_kwargs(connection_ref)
        schema = relation.namespace.parts[0] if relation.namespace.parts else ""
        out: list[ColumnInfo] = []
        conn = None
        try:
            conn = snowflake.connector.connect(**kwargs)
            cur = conn.cursor()
            # INFORMATION_SCHEMA stores an unquoted-created object's identifiers
            # UPPER (Snowflake folds unquoted DDL — dlt/dbt loads, deployed views).
            # The schema/table here arrive logical-cased from the graph, so
            # upper-fold the lookup keys or the filter matches nothing and the
            # column hydration returns empty (silently degrading the Q&A prompt).
            # A no-op when a live-scan caller already passes UPPER names.
            cur.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, ORDINAL_POSITION "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                "ORDER BY ORDINAL_POSITION",
                ((schema or "").upper(), (relation.name or "").upper()),
            )
            for name, dtype, nullable, ordinal in cur.fetchall():
                out.append(ColumnInfo(
                    name=name, data_type=dtype,
                    nullable=(nullable == "YES"), ordinal=ordinal,
                ))
            cur.close()
        except Exception as exc:
            logger.warning("snowflake list_columns failed: %s", exc)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        return out

    def list_foreign_keys(
        self,
        connection_ref: dict[str, Any],
        schema: str,
    ) -> list[ForeignKeyInfo]:
        import snowflake.connector
        kwargs = _resolve_connect_kwargs(connection_ref)
        out: list[ForeignKeyInfo] = []
        conn = None
        try:
            conn = snowflake.connector.connect(**kwargs)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT
                    fk.table_schema, fk.table_name, fk.column_name,
                    pk.table_schema AS to_schema,
                    pk.table_name   AS to_table,
                    pk.column_name  AS to_column
                FROM information_schema.referential_constraints rc
                JOIN information_schema.key_column_usage fk
                    ON fk.constraint_name   = rc.constraint_name
                   AND fk.constraint_schema = rc.constraint_schema
                JOIN information_schema.key_column_usage pk
                    ON pk.constraint_name   = rc.unique_constraint_name
                   AND pk.constraint_schema = rc.unique_constraint_schema
                   AND pk.ordinal_position  = fk.position_in_unique_constraint
                WHERE fk.table_schema = %s
                ORDER BY fk.table_name, fk.column_name
                """,
                (schema,),
            )
            for row in cur.fetchall():
                out.append(ForeignKeyInfo(
                    from_schema=row[0], from_table=row[1], from_column=row[2],
                    to_schema=row[3], to_table=row[4], to_column=row[5],
                ))
            cur.close()
        except Exception as exc:
            logger.warning("snowflake list_foreign_keys failed for schema %s: %s", schema, exc)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        return out


class SnowflakeQueryExecutor:
    """Runs a single SELECT against Snowflake. Neutral ResultSet; raises on
    driver/import error (the facade classifies + audits)."""

    def explain(self, connection_ref, query, limits: QueryLimits) -> ValidationReport:
        return ValidationReport(valid=True)

    def select(self, connection_ref: dict[str, Any], query: str,
               limits: QueryLimits) -> ResultSet:
        import time
        import snowflake.connector as _sf
        from ._exec_util import generic_typename, serialize_row, SELECT_TIMEOUT_MS
        max_rows = max(1, min(limits.max_rows, 1000))
        started = time.monotonic()
        bare = query.rstrip().rstrip(";")
        wrapped = f"SELECT * FROM ({bare}) _wb_sub LIMIT {int(max_rows) + 1}"
        conn = None
        try:
            conn = _sf.connect(**_resolve_connect_kwargs(connection_ref))
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = "
                        f"{(limits.timeout_ms or SELECT_TIMEOUT_MS) // 1000}"
                    )
                except Exception:
                    pass  # best-effort
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
        """Map a Snowflake driver error to a neutral class so the UI can explain
        it. Snowflake couples "does not exist or not authorized" into ONE message
        for a missing/mis-cased relation — the dominant cause here (a lowercase-
        quoted read of an UPPER unquoted-created object) — so match that first."""
        msg = str(exception).lower()
        if "does not exist" in msg:
            return "relation_not_found"
        if "insufficient privileges" in msg or "not authorized" in msg:
            return "permission_denied"
        return "sql_error"


class SnowflakeDeploymentProvider:
    """Deploys CREATE/DROP VIEW DDL to Snowflake. Neutral DeploymentRun."""

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
        started = time.monotonic()

        def _ms() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            import snowflake.connector as _sf
        except ImportError:
            return DeploymentRun(run_id="", status="failed", duration_ms=_ms(),
                                 error_class="platform_not_supported",
                                 error_message="snowflake-connector-python not installed. Add snowflake-connector-python>=3.0.0.")
        conn = None
        try:
            conn = _sf.connect(**_resolve_connect_kwargs(connection_ref))
            with conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{view_schema}"')
                for raw_stmt in (sqlparse.split(ddl) or []):
                    stmt = raw_stmt.strip().rstrip(";").strip()
                    if not stmt:
                        continue
                    cur.execute(stmt)
                smoke = []
                for raw_name in view_names:
                    bare = raw_name.rsplit(".", 1)[-1].strip('"')
                    if not bare:
                        continue
                    cur.execute(f'SELECT 1 FROM "{view_schema}"."{bare}" LIMIT 1')
                    smoke.append(bare)
            conn.commit()
            return DeploymentRun(run_id="", status="succeeded",
                                 deployed_objects=smoke, duration_ms=_ms())
        except Exception as e:  # noqa: BLE001 — Snowflake errors are broad
            if conn:
                try:
                    conn.rollback()
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

def _resolve_connect_kwargs(connection_ref: dict[str, Any]) -> dict[str, Any]:
    """Build a snowflake.connector.connect() kwargs dict from a connection reference.

    Accepts explicit host/database/username + resolved_password keys, plus
    extra_config for warehouse/role/schema.  The resolved_password is the
    ephemeral secret — never log or persist the returned dict.
    """
    extra = connection_ref.get("extra_config", {})
    kwargs: dict[str, Any] = {
        "account": connection_ref.get("host", ""),
        "user": connection_ref.get("username", ""),
        # password MUST come from a secret resolver; never log or persist.
        "password": connection_ref.get("resolved_password", ""),
        "database": connection_ref.get("database", ""),
        "login_timeout": 30,
    }
    # Only pass non-empty optional extras so the connector uses its defaults.
    if extra.get("warehouse"):
        kwargs["warehouse"] = extra["warehouse"]
    if extra.get("role"):
        kwargs["role"] = extra["role"]
    if extra.get("schema"):
        kwargs["schema"] = extra["schema"]
    return kwargs


def _extract_table_metadata(cur, schema: str, table: str) -> dict[str, Any]:
    """Run INFORMATION_SCHEMA queries for one table.

    Returns a dict with keys: schema, table, comment, columns, primary_keys,
    foreign_keys, unique_constraints, indexes, check_constraints.

    Notes:
    - Snowflake constraints are informational only (not enforced).
    - Unquoted identifiers are folded to uppercase in Snowflake.
    """
    # 1. Columns
    cur.execute(
        """
        SELECT COLUMN_NAME, ORDINAL_POSITION, DATA_TYPE,
               CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE,
               IS_NULLABLE, COLUMN_DEFAULT, COMMENT
        FROM INFORMATION_SCHEMA.COLUMNS
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

    # 2. Table comment via TABLES
    cur.execute(
        "SELECT COMMENT FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
        (schema, table),
    )
    row = cur.fetchone()
    table_comment = row[0] if row else ""

    # 3. Primary keys (informational in Snowflake)
    primary_keys: list[str] = []
    try:
        cur.execute(
            """
            SELECT ccu.COLUMN_NAME
            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
            JOIN INFORMATION_SCHEMA.CONSTRAINT_COLUMN_USAGE ccu
              ON ccu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
             AND ccu.TABLE_SCHEMA    = tc.TABLE_SCHEMA
             AND ccu.TABLE_NAME      = tc.TABLE_NAME
            WHERE tc.TABLE_SCHEMA    = %s
              AND tc.TABLE_NAME      = %s
              AND tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
            ORDER BY ccu.COLUMN_NAME
            """,
            (schema, table),
        )
        primary_keys = [r[0] for r in cur.fetchall()]
    except Exception:
        pass

    # 4. Foreign keys (informational in Snowflake)
    foreign_keys: list[dict[str, Any]] = []
    try:
        cur.execute(
            """
            SELECT tc.CONSTRAINT_NAME,
                   kcu.COLUMN_NAME,
                   ccu.TABLE_SCHEMA  AS ref_schema,
                   ccu.TABLE_NAME    AS ref_table,
                   ccu.COLUMN_NAME   AS ref_column
            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
            JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
              ON kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
             AND kcu.TABLE_SCHEMA    = tc.TABLE_SCHEMA
             AND kcu.TABLE_NAME      = tc.TABLE_NAME
            JOIN INFORMATION_SCHEMA.CONSTRAINT_COLUMN_USAGE ccu
              ON ccu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
            WHERE tc.TABLE_SCHEMA    = %s
              AND tc.TABLE_NAME      = %s
              AND tc.CONSTRAINT_TYPE = 'FOREIGN KEY'
            ORDER BY tc.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
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
            }
            for r in cur.fetchall()
        ]
    except Exception:
        pass

    return {
        "schema": schema,
        "table": table,
        "comment": table_comment,
        "columns": columns,
        "primary_keys": primary_keys,
        "foreign_keys": foreign_keys,
        "unique_constraints": [],
        "indexes": [],
        "check_constraints": [],
    }


# ── Code asset provider ───────────────────────────────────────────────────────

import hashlib as _hashlib


class SnowflakeCodeAssetProvider:
    """Lists Snowflake code assets (tasks, dynamic tables, streams, notebooks,
    procedures) per selected namespace via SHOW commands."""

    def list_code_assets(
        self, connection_ref: dict[str, Any], namespaces: list[NamespaceRef]
    ) -> list:
        from ..interfaces import CodeAssetSummary
        import snowflake.connector
        kwargs = _resolve_connect_kwargs(connection_ref)
        results: list[CodeAssetSummary] = []
        conn = None
        try:
            conn = snowflake.connector.connect(**kwargs)
            cur = conn.cursor()
            for ns in namespaces:
                schema = ns.parts[0] if ns.parts else ""
                if not schema:
                    continue
                for kind, sql, name_idx, extra_fn in [
                    ("task", f'SHOW TASKS IN SCHEMA "{schema}"', 1, _parse_task),
                    ("dynamic_table", f'SHOW DYNAMIC TABLES IN SCHEMA "{schema}"', 1, _parse_dyn_table),
                    ("stream", f'SHOW STREAMS IN SCHEMA "{schema}"', 1, None),
                    ("notebook", f'SHOW NOTEBOOKS IN SCHEMA "{schema}"', 1, None),
                    ("procedure", f'SHOW PROCEDURES IN SCHEMA "{schema}"', 1, None),
                ]:
                    try:
                        cur.execute(sql)
                        cols = [c[0].lower() for c in cur.description]
                        for row in cur.fetchall():
                            name = row[name_idx]
                            preview = None
                            depends_on: list[str] = []
                            schedule = None
                            if extra_fn:
                                preview, depends_on, schedule = extra_fn(cols, row)
                            h = _hashlib.sha256((preview or name).encode()).hexdigest()[:16] if (preview or name) else None
                            results.append(CodeAssetSummary(
                                name=name,
                                asset_kind=kind,
                                namespace=ns,
                                definition_preview=(preview[:4096] if preview else None),
                                definition_hash=h,
                                depends_on=depends_on,
                                schedule=schedule,
                            ))
                    except Exception as exc:
                        logger.debug("SHOW %s in %s failed: %s", kind, schema, exc)
            cur.close()
        except Exception as exc:
            logger.warning("SnowflakeCodeAssetProvider.list_code_assets failed: %s", exc)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        return results


def _parse_task(cols: list, row: tuple) -> tuple:
    """Extract (preview, depends_on, schedule) from a SHOW TASKS row."""
    def _get(col: str):
        try:
            return row[cols.index(col)] if col in cols else None
        except Exception:
            return None
    body = _get("definition") or _get("body") or ""
    preds_raw = _get("predecessors") or ""
    deps: list[str] = []
    if preds_raw:
        import json as _json
        try:
            preds = _json.loads(preds_raw) if isinstance(preds_raw, str) else preds_raw
            deps = [str(p) for p in (preds if isinstance(preds, list) else [])]
        except Exception:
            deps = [str(preds_raw)]
    sched = _get("schedule") or _get("condition") or None
    return str(body), deps, str(sched) if sched else None


def _parse_dyn_table(cols: list, row: tuple) -> tuple:
    def _get(col: str):
        try:
            return row[cols.index(col)] if col in cols else None
        except Exception:
            return None
    body = _get("text") or _get("definition") or ""
    lag = _get("target_lag") or None
    return str(body), [], str(lag) if lag else None
