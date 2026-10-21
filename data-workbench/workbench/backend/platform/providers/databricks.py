"""Databricks discovery provider.

Implements ConnectionProvider and DiscoveryProvider for Databricks SQL
Warehouses using databricks-sql-connector.  Mirrors the logic that would
live in a data-discovery-databricks skill behind the platform SPI so new
code paths (MCP tools, namespace browser) can call it without spawning a
subprocess.

The skill scripts are NOT replaced — they remain the SDK entry point.
This provider is additive: it serves direct Python callers only.

Only databricks.sql is imported inside method bodies (lazy import) so
the module loads cleanly in test environments that don't have the
connector installed.

Connection reference keys expected:
  host             — Databricks workspace hostname
                     (e.g. adb-1234567890.12.azuredatabricks.net)
  resolved_password — Databricks personal access token (transient — NEVER log)
  extra_config:
    http_path      — SQL warehouse HTTP path (e.g. /sql/1.0/warehouses/abc123) — REQUIRED
    catalog        — Unity Catalog name (optional, default hive_metastore)
    schema         — default schema (optional)
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

PLATFORM_TYPE = "databricks"

# Schemas that are never user data. NB: `default` is a legitimate user schema on
# Databricks (Unity Catalog auto-creates it and users put real tables there, e.g.
# workspace.default), so it is NOT filtered — the namespace policy decides whether
# to scan it. Only `information_schema` is always excluded.
_SYSTEM_SCHEMAS = frozenset({"information_schema"})


class DatabricksConnectionProvider:
    """Validates config and probes a live Databricks SQL Warehouse connection."""

    def validate_config(
        self, public_config: dict[str, Any], secret_ref: str
    ) -> ValidationReport:
        errors = []
        if not public_config.get("host"):
            errors.append("host (Databricks workspace hostname) is required")
        extra = public_config.get("extra_config", {})
        if not extra.get("http_path"):
            errors.append(
                "extra_config.http_path is required (SQL warehouse HTTP path)"
            )
        if not secret_ref:
            errors.append(
                "secret_ref is required (Databricks personal access token)"
            )
        return ValidationReport(valid=not errors, errors=errors)

    def probe(self, connection_ref: dict[str, Any]) -> CapabilityEvidence:
        evidence = CapabilityEvidence(
            platform_type=PLATFORM_TYPE,
            instance_id=connection_ref.get("connection_id", "unknown"),
        )
        conn = None
        try:
            from databricks import sql as databricks_sql
            kwargs = _resolve_connect_kwargs(connection_ref)
            conn = databricks_sql.connect(**kwargs)
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
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
        from databricks import sql as databricks_sql
        kwargs = _resolve_connect_kwargs(connection_ref)
        conn = databricks_sql.connect(**kwargs)
        return ManagedConnection(
            platform_type=PLATFORM_TYPE,
            instance_id=connection_ref.get("connection_id", "unknown"),
            purpose=purpose,
            _internal=conn,
        )


class DatabricksDiscoveryProvider:
    """Lists schemas and tables; extracts per-table metadata from Databricks."""

    def list_catalogs_and_schemas(
        self, connection_ref: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Enumerate the write-target namespace tree: every catalog and its
        schemas. Used by the serving Target-namespace picker so an engineer
        selects ``catalog.schema`` instead of typing it. ``information_schema``
        is filtered (never a write target) but ``default`` is KEPT — it's a
        legitimate target schema. Per-catalog `SHOW SCHEMAS` is guarded so one
        unreadable catalog (no USE grant) doesn't blank the whole list.
        """
        from databricks import sql as databricks_sql
        kwargs = _resolve_connect_kwargs(connection_ref)
        out: list[dict[str, Any]] = []
        conn = None
        try:
            conn = databricks_sql.connect(**kwargs)
            with conn.cursor() as cur:
                cur.execute("SHOW CATALOGS")
                catalogs = [r[0] for r in cur.fetchall()]
                for cat in catalogs:
                    schemas: list[str] = []
                    try:
                        cur.execute(f"SHOW SCHEMAS IN `{cat}`")
                        schemas = [
                            r[0] for r in cur.fetchall()
                            if r[0].lower() != "information_schema"
                        ]
                    except Exception as exc:  # noqa: BLE001
                        logger.info("SHOW SCHEMAS IN %s failed: %s", cat, exc)
                    out.append({"catalog": cat, "schemas": sorted(schemas)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("databricks list_catalogs_and_schemas failed: %s", exc)
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
        from databricks import sql as databricks_sql
        kwargs = _resolve_connect_kwargs(connection_ref)
        catalog = kwargs.get("catalog", "")
        instance_id = connection_ref.get("connection_id", "unknown")
        results = []
        conn = None
        try:
            conn = databricks_sql.connect(**kwargs)
            with conn.cursor() as cur:
                for name in _schema_names(cur, catalog):
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
            logger.warning("databricks list_namespaces failed: %s", exc)
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
        from databricks import sql as databricks_sql
        kwargs = _resolve_connect_kwargs(connection_ref)
        catalog = kwargs.get("catalog", "")
        schema = namespace.parts[0] if namespace.parts else ""
        results = []
        conn = None
        try:
            conn = databricks_sql.connect(**kwargs)
            with conn.cursor() as cur:
                for name, kind, comment in _relation_rows(cur, catalog, schema):
                    size_bytes = None
                    last_modified = None
                    metrics: dict = {}
                    if kind == "table":
                        try:
                            qual = (
                                f"`{catalog}`.`{schema}`.`{name}`"
                                if catalog else f"`{schema}`.`{name}`"
                            )
                            cur.execute(f"DESCRIBE DETAIL {qual}")
                            row = cur.fetchone()
                            if row:
                                detail_cols = [d[0].lower() for d in cur.description]

                                def _dget(col: str, _cols=detail_cols, _row=row):
                                    try:
                                        return _row[_cols.index(col)] if col in _cols else None
                                    except Exception:
                                        return None

                                size_val = _dget("sizeinbytes") or _dget("size_in_bytes")
                                num_files = _dget("numfiles") or _dget("num_files")
                                mod_time = _dget("lastmodified") or _dget("last_modified")
                                size_bytes = int(size_val) if size_val is not None else None
                                if num_files is not None:
                                    metrics["num_files"] = int(num_files)
                                last_modified = str(mod_time) if mod_time else None
                        except Exception as exc:
                            logger.debug("DESCRIBE DETAIL %s.%s failed: %s", schema, name, exc)
                    results.append(
                        RelationSummary(
                            namespace=namespace,
                            name=name,
                            relation_kind=kind,
                            size_bytes=size_bytes,
                            last_modified=last_modified,
                            metrics=metrics,
                            comment=comment or None,
                        )
                    )
        except Exception as exc:
            logger.warning("databricks list_relations failed: %s", exc)
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
        from databricks import sql as databricks_sql
        kwargs = _resolve_connect_kwargs(connection_ref)
        instance_id = connection_ref.get("connection_id", "unknown")
        relations: list[dict[str, Any]] = []
        warnings: list[str] = []

        conn = None
        try:
            conn = databricks_sql.connect(**kwargs)
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
        from databricks import sql as databricks_sql
        kwargs = _resolve_connect_kwargs(connection_ref)
        catalog = kwargs.get("catalog", "")
        schema = relation.namespace.parts[0] if relation.namespace.parts else ""
        out: list[ColumnInfo] = []
        conn = None
        try:
            conn = databricks_sql.connect(**kwargs)
            with conn.cursor() as cur:
                out = _column_infos(cur, catalog, schema, relation.name)
        except Exception as exc:
            logger.warning("databricks list_columns failed: %s", exc)
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
        # Databricks Unity Catalog doesn't reliably expose FK constraints via
        # INFORMATION_SCHEMA in all runtime versions — return empty (best-effort).
        return []


class DatabricksQueryExecutor:
    """Runs a single SELECT against Databricks SQL. Neutral ResultSet; raises on
    driver/import error (the facade classifies + audits)."""

    def explain(self, connection_ref, query, limits: QueryLimits) -> ValidationReport:
        return ValidationReport(valid=True)

    def select(self, connection_ref: dict[str, Any], query: str,
               limits: QueryLimits) -> ResultSet:
        import time
        from databricks import sql as _dbr_sql
        from ._exec_util import generic_typename, serialize_row, SELECT_TIMEOUT_MS
        max_rows = max(1, min(limits.max_rows, 1000))
        started = time.monotonic()
        bare = query.rstrip().rstrip(";")
        wrapped = f"SELECT * FROM ({bare}) _wb_sub LIMIT {int(max_rows) + 1}"
        conn = None
        try:
            conn = _dbr_sql.connect(
                socket_timeout=(limits.timeout_ms or SELECT_TIMEOUT_MS) // 1000,
                **_resolve_connect_kwargs(connection_ref),
            )
            with conn.cursor() as cur:
                cur.execute(wrapped)
                raw_rows = cur.fetchall()
                description = cur.description or []
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
        """Map a Databricks/Spark SQL driver error to a neutral class so the UI
        can explain it, instead of a blanket ``sql_error``. A missing relation is
        raised as ``[TABLE_OR_VIEW_NOT_FOUND]`` (Spark) or a "not found" message."""
        msg = str(exception).lower()
        if ("table_or_view_not_found" in msg or "table or view not found" in msg
                or "does not exist" in msg):
            return "relation_not_found"
        if "permission" in msg or "not authorized" in msg or "access denied" in msg:
            return "permission_denied"
        return "sql_error"


class DatabricksDeploymentProvider:
    """Deploys CREATE/DROP VIEW DDL to Databricks SQL. Neutral DeploymentRun."""

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
            from databricks import sql as _dbr_sql
        except ImportError:
            return DeploymentRun(run_id="", status="failed", duration_ms=_ms(),
                                 error_class="platform_not_supported",
                                 error_message="databricks-sql-connector not installed. Add databricks-sql-connector>=3.0.0.")
        conn = None
        try:
            conn = _dbr_sql.connect(**_resolve_connect_kwargs(connection_ref))
            with conn.cursor() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS `{view_schema}`")
                for raw_stmt in (sqlparse.split(ddl) or []):
                    stmt = raw_stmt.strip().rstrip(";").strip()
                    if not stmt:
                        continue
                    cur.execute(stmt)
                smoke = []
                for raw_name in view_names:
                    bare = raw_name.rsplit(".", 1)[-1].strip('"').strip("`")
                    if not bare:
                        continue
                    cur.execute(f"SELECT 1 FROM `{view_schema}`.`{bare}` LIMIT 1")
                    smoke.append(bare)
            # Databricks SQL operates in autocommit mode; no explicit commit.
            return DeploymentRun(run_id="", status="succeeded",
                                 deployed_objects=smoke, duration_ms=_ms())
        except Exception as e:  # noqa: BLE001 — Databricks errors are broad
            return DeploymentRun(run_id="", status="failed", duration_ms=_ms(),
                                 error_class="sql_error", error_message=str(e))
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass


# ── helpers ───────────────────────────────────────────────────────────────────

# Enumeration is INFORMATION_SCHEMA-first (Unity Catalog managed catalogs), with a
# SHOW/DESCRIBE fallback for catalogs that don't expose an information schema —
# notably Delta-Sharing "shares received" catalogs like the `samples` catalog,
# where the INFORMATION_SCHEMA queries return empty or error. Without the fallback
# a scan of such a catalog silently finds nothing. Mirrors the fallback the
# data-discovery-databricks skill already uses.

def _schema_names(cur, catalog: str) -> list[str]:
    """Schema names in the connected catalog (INFORMATION_SCHEMA → SHOW SCHEMAS)."""
    try:
        cur.execute(
            "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA ORDER BY SCHEMA_NAME"
        )
        rows = [r[0] for r in cur.fetchall()]
        if rows:
            return rows
    except Exception:  # noqa: BLE001 — fall through to SHOW SCHEMAS
        pass
    stmt = f"SHOW SCHEMAS IN `{catalog}`" if catalog else "SHOW SCHEMAS"
    cur.execute(stmt)
    # SHOW SCHEMAS returns (databaseName,) or (namespace,) per DBR version.
    return [r[0] for r in cur.fetchall()]


def _relation_rows(cur, catalog: str, schema: str) -> list[tuple[str, str, Optional[str]]]:
    """(name, kind, comment) for a schema (INFORMATION_SCHEMA → SHOW TABLES).

    The SHOW TABLES fallback can't distinguish views or return comments, so those
    degrade to kind='table'/comment=None — acceptable for a metadata estate scan.
    """
    try:
        cur.execute(
            """
            SELECT TABLE_NAME, TABLE_TYPE, COMMENT
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = %s
              AND TABLE_TYPE IN ('BASE TABLE', 'VIEW')
            ORDER BY TABLE_NAME
            """,
            (schema,),
        )
        rows = cur.fetchall()
        if rows:
            return [(r[0], "view" if r[1] == "VIEW" else "table", r[2]) for r in rows]
    except Exception:  # noqa: BLE001 — fall through to SHOW TABLES
        pass
    ident = f"`{catalog}`.`{schema}`" if catalog else f"`{schema}`"
    cur.execute(f"SHOW TABLES IN {ident}")
    # SHOW TABLES returns (database, tableName, isTemporary).
    return [(r[1], "table", None) for r in cur.fetchall()]


def _column_infos(cur, catalog: str, schema: str, table: str) -> list[ColumnInfo]:
    """Column metadata for a table (INFORMATION_SCHEMA.COLUMNS → DESCRIBE TABLE)."""
    try:
        cur.execute(
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, ORDINAL_POSITION "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
            "ORDER BY ORDINAL_POSITION",
            (schema, table),
        )
        rows = cur.fetchall()
        if rows:
            return [ColumnInfo(name=r[0], data_type=r[1],
                               nullable=(r[2] == "YES"), ordinal=r[3]) for r in rows]
    except Exception:  # noqa: BLE001 — fall through to DESCRIBE TABLE
        pass
    ident = f"`{catalog}`.`{schema}`.`{table}`" if catalog else f"`{schema}`.`{table}`"
    cur.execute(f"DESCRIBE TABLE {ident}")
    out: list[ColumnInfo] = []
    ordinal = 1
    for row in cur.fetchall():
        col_name = (row[0] or "").strip()
        # DESCRIBE appends a "# Partition Information" section + blank separators.
        if not col_name or col_name.startswith("#"):
            break
        dtype = row[1] if len(row) > 1 else ""
        out.append(ColumnInfo(name=col_name, data_type=dtype or "",
                              nullable=True, ordinal=ordinal))
        ordinal += 1
    return out


def _resolve_connect_kwargs(connection_ref: dict[str, Any]) -> dict[str, Any]:
    """Build a databricks.sql.connect() kwargs dict from a connection reference.

    Accepts explicit host + resolved_password (PAT) keys, plus extra_config
    for http_path/catalog/schema.  The resolved_password is the ephemeral PAT
    — never log or persist the returned dict.
    """
    extra = connection_ref.get("extra_config", {})
    kwargs: dict[str, Any] = {
        "server_hostname": connection_ref.get("host", ""),
        "http_path": extra.get("http_path", ""),
        # access_token MUST come from a secret resolver; never log or persist.
        "access_token": connection_ref.get("resolved_password", ""),
    }
    # Only pass non-empty optional extras so the connector uses its defaults.
    catalog = extra.get("catalog", "")
    if catalog:
        kwargs["catalog"] = catalog
    schema = extra.get("schema", "")
    if schema:
        kwargs["schema"] = schema
    return kwargs


def _extract_table_metadata(cur, schema: str, table: str) -> dict[str, Any]:
    """Run INFORMATION_SCHEMA queries for one table.

    Returns a dict with keys: schema, table, comment, columns, primary_keys,
    foreign_keys, unique_constraints, indexes, check_constraints.

    Notes:
    - Databricks constraints are informational only (not enforced).
    - Identifiers use backticks and are case-insensitive.
    - Primary / foreign key support depends on Unity Catalog configuration.
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
            "name": row[0],
            "ordinal": row[1],
            "data_type": row[2],
            "char_max_length": row[3],
            "numeric_precision": row[4],
            "numeric_scale": row[5],
            "is_nullable": row[6] == "YES",
            "default": row[7],
            "comment": row[8] or "",
        }
        for row in cur.fetchall()
    ]

    # 2. Table comment
    cur.execute(
        "SELECT COMMENT FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
        (schema, table),
    )
    row = cur.fetchone()
    table_comment = row[0] if row else ""

    # 3. Primary keys (informational in Databricks)
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

    return {
        "schema": schema,
        "table": table,
        "comment": table_comment,
        "columns": columns,
        "primary_keys": primary_keys,
        "foreign_keys": [],
        "unique_constraints": [],
        "indexes": [],
        "check_constraints": [],
    }


# ── Code asset provider ───────────────────────────────────────────────────────

class DatabricksCodeAssetProvider:
    """Lists Databricks jobs and DLT pipelines (+ referenced notebooks) via
    the databricks-sdk REST API. Lazy import — fails gracefully if not installed."""

    def list_code_assets(
        self, connection_ref: dict[str, Any], namespaces: list[NamespaceRef]
    ) -> list:
        from ..interfaces import CodeAssetSummary
        results: list[CodeAssetSummary] = []
        try:
            from databricks.sdk import WorkspaceClient
        except ImportError:
            logger.warning("databricks-sdk not installed; code-asset scan skipped")
            return results

        import hashlib as _hashlib

        host = connection_ref.get("host", "")
        if host and not host.startswith("https://"):
            host = f"https://{host}"
        token = connection_ref.get("resolved_password", "")
        try:
            w = WorkspaceClient(host=host, token=token)
        except Exception as exc:
            logger.warning("DatabricksCodeAssetProvider WorkspaceClient init failed: %s", exc)
            return results

        # Jobs
        try:
            for job in w.jobs.list():
                depends_on: list[str] = []
                try:
                    settings = job.settings
                    for task in (getattr(settings, "tasks", None) or []):
                        nb = getattr(task, "notebook_task", None)
                        if nb and getattr(nb, "notebook_path", None):
                            depends_on.append(nb.notebook_path)
                except Exception:
                    pass
                name = str(job.settings.name if job.settings else job.job_id)
                preview = name
                h = _hashlib.sha256(preview.encode()).hexdigest()[:16]
                results.append(CodeAssetSummary(
                    name=name,
                    asset_kind="job",
                    definition_preview=preview[:4096],
                    definition_hash=h,
                    depends_on=depends_on,
                    extra={"job_id": job.job_id},
                ))
        except Exception as exc:
            logger.warning("DatabricksCodeAssetProvider jobs.list failed: %s", exc)

        # DLT Pipelines
        try:
            for pipeline in w.pipelines.list_pipelines():
                depends_on_libs: list[str] = []
                target_schema = None
                try:
                    spec = getattr(pipeline, "spec", None)
                    if spec:
                        target_schema = getattr(spec, "target", None)
                        for lib in (getattr(spec, "libraries", None) or []):
                            nb = getattr(lib, "notebook", None)
                            if nb and getattr(nb, "path", None):
                                depends_on_libs.append(nb.path)
                except Exception:
                    pass
                name = pipeline.name or str(pipeline.pipeline_id)
                h = _hashlib.sha256(name.encode()).hexdigest()[:16]
                results.append(CodeAssetSummary(
                    name=name,
                    asset_kind="pipeline",
                    depends_on=depends_on_libs,
                    definition_hash=h,
                    extra={"pipeline_id": pipeline.pipeline_id,
                           "target_schema": target_schema},
                ))
        except Exception as exc:
            logger.warning("DatabricksCodeAssetProvider pipelines.list failed: %s", exc)

        return results
