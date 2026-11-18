"""
databricks_tool.py — Azure Databricks SQL & Unity Catalog execution helper for Bronze-layer publishing.

Executes Databricks SQL / Delta Lake DDL & DML statements against a Databricks
SQL Warehouse via databricks-sql-connector (Unity Catalog 3-level namespace:
`catalog`.`schema`.`table`).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

_DEFAULT_MODE = os.environ.get("DATABRICKS_PUBLISHER_MODE", os.environ.get("BQ_PUBLISHER_MODE", "auto")).strip().lower()


def _split_statements(sql_code: str) -> list[str]:
    """
    Split a multi-statement Databricks SQL script into individual statements.
    Strips -- and # line comments, splits on semicolons, filters blanks.
    """
    lines = []
    for line in sql_code.splitlines():
        stripped = line.strip()
        if not stripped.startswith("--") and not stripped.startswith("#"):
            lines.append(line)

    stmts = []
    for chunk in "\n".join(lines).split(";"):
        stmt = chunk.strip()
        if stmt and re.search(
            r"\b(CREATE|INSERT|MERGE|UPDATE|DELETE|DROP|ALTER|SELECT|WITH|CALL|OPTIMIZE|COMMENT)\b",
            stmt,
            re.IGNORECASE,
        ):
            stmts.append(stmt)
    return stmts


def _extract_table_refs(statements: list[str]) -> list[str]:
    """Extract fully-qualified table references from CREATE TABLE/VIEW statements."""
    tables = []
    for stmt in statements:
        m = re.search(
            r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|MATERIALIZED\s+VIEW|STREAMING\s+TABLE)\s+"
            r"(?:IF\s+NOT\s+EXISTS\s+)?([`'\"]?[\w.]+[`'\"]?(?:\.[`'\"]?[\w]+[`'\"]?)*)",
            stmt,
            re.IGNORECASE,
        )
        if m:
            tables.append(m.group(1).replace("`", "").replace('"', "").replace("'", ""))
    return tables


class DatabricksPublisher:
    """
    Executes a Databricks SQL / Delta Lake DDL script produced by the Spec Generator.

    Supports:
      - Unity Catalog & Schema creation (CREATE CATALOG/SCHEMA IF NOT EXISTS)
      - CREATE TABLE IF NOT EXISTS ... USING DELTA
      - DML: INSERT, MERGE for sample data seeding
      - Dry-run mode (plan only, no execution)
    """

    def __init__(
        self,
        catalog: Optional[str] = None,
        bronze_schema: Optional[str] = None,
        host: Optional[str] = None,
        token: Optional[str] = None,
        warehouse_id: Optional[str] = None,
        mode: Optional[str] = None,
    ):
        self.catalog = catalog or os.environ.get("DATABRICKS_CATALOG", "main")
        self.bronze_schema = bronze_schema or os.environ.get("DATABRICKS_BRONZE_SCHEMA", "banking_bronze")
        self.host = host or os.environ.get("DATABRICKS_HOST", "")
        self.token = token or os.environ.get("DATABRICKS_TOKEN", "")
        self.warehouse_id = warehouse_id or os.environ.get("DATABRICKS_WAREHOUSE_ID", "")

        _mode = (mode or _DEFAULT_MODE).strip().lower()
        if _mode not in {"live", "dry_run", "auto"}:
            _mode = "auto"
        if _mode == "auto":
            _mode = "live" if (self.host and (self.warehouse_id or os.environ.get("DATABRICKS_HTTP_PATH"))) else "dry_run"
        self.mode = _mode

    @property
    def bronze_schema_ref(self) -> str:
        return f"{self.catalog}.{self.bronze_schema}"

    def _get_connection(self):
        from tools.databricks_auth import get_sql_connection

        return get_sql_connection(
            host=self.host or None,
            token=self.token or None,
            catalog=self.catalog,
            schema=self.bronze_schema,
        )

    def _ensure_schema(self, conn) -> None:
        """Create Unity Catalog schema if it does not exist. Idempotent."""
        with conn.cursor() as cur:
            cur.execute(f"CREATE CATALOG IF NOT EXISTS `{self.catalog}`")
            cur.execute(
                f"CREATE SCHEMA IF NOT EXISTS `{self.catalog}`.`{self.bronze_schema}` "
                f"COMMENT 'BFSI Bronze-layer — {self.bronze_schema} (managed by BFSI-Bronze-Agent)'"
            )
        log.info("Unity Catalog schema ready: %s", self.bronze_schema_ref)

    def _run_statement(self, conn, sql: str) -> dict:
        """Execute one SQL statement. Returns {'ok': True} or {'ok': False, 'error': '...'}."""
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def publish(self, pipeline_code: str) -> dict:
        """
        Execute a multi-statement Databricks SQL script.

        Returns:
            {
              publish_status:    "published" | "partial" | "failed" | "dry_run"
              published_tables:  list of table FQNs from CREATE TABLE statements
              executed_count:    int
              failed_statements: [{statement, error}, ...]
              summary:           human-readable result string
              catalog:           Unity Catalog name
              bronze_schema:     fully-qualified catalog.schema
            }
        """
        if not pipeline_code or not pipeline_code.strip():
            return {
                "publish_status": "failed",
                "published_tables": [],
                "summary": "No DDL code provided to publish.",
            }

        statements = _split_statements(pipeline_code)
        if not statements:
            return {
                "publish_status": "failed",
                "published_tables": [],
                "summary": "No executable SQL statements found in provided code.",
            }

        report: dict = {
            "publish_status": "in_progress",
            "published_tables": [],
            "executed_count": 0,
            "failed_statements": [],
            "total_statements": len(statements),
            "summary": "",
            "catalog": self.catalog,
            "bronze_schema": self.bronze_schema_ref,
        }

        if self.mode == "dry_run":
            report["publish_status"] = "dry_run"
            report["published_tables"] = _extract_table_refs(statements)
            report["executed_count"] = 0
            report["summary"] = (
                f"Dry run: {len(statements)} statement(s) planned, "
                f"{len(report['published_tables'])} table(s) identified for {self.bronze_schema_ref}."
            )
            return report

        try:
            with self._get_connection() as conn:
                self._ensure_schema(conn)
                executed_stmts = []
                for stmt in statements:
                    result = self._run_statement(conn, stmt)
                    if result["ok"]:
                        executed_stmts.append(stmt)
                        report["executed_count"] += 1
                    else:
                        report["failed_statements"].append({
                            "statement": stmt[:300],
                            "error": result["error"],
                        })
        except Exception as exc:
            report["publish_status"] = "failed"
            report["summary"] = f"Databricks SQL execution failed: {exc}"
            return report

        tables = _extract_table_refs(executed_stmts)
        report["published_tables"] = tables
        n_ok = report["executed_count"]
        n_fail = len(report["failed_statements"])

        if n_fail == 0:
            report["publish_status"] = "published"
            report["summary"] = (
                f"All {n_ok} statement(s) executed in {self.bronze_schema_ref}. "
                f"Tables: {', '.join(tables) or 'none'}."
            )
        elif n_ok > 0:
            report["publish_status"] = "partial"
            errors = "; ".join(f["error"] for f in report["failed_statements"][:2])
            report["summary"] = f"{n_ok} OK, {n_fail} failed. First errors: {errors}"
        else:
            report["publish_status"] = "failed"
            errors = "; ".join(f["error"] for f in report["failed_statements"][:2])
            report["summary"] = f"All {n_fail} statement(s) failed. Errors: {errors}"

        return report

    def query_table(self, table_ref: str, limit: int = 100) -> dict:
        """Run SELECT * FROM <table_ref> LIMIT <limit>."""
        try:
            quoted_ref = ".".join(f"`{part.strip('`')}`" for part in table_ref.split("."))
            with self._get_connection() as conn, conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {quoted_ref} LIMIT {int(limit)}")
                columns = [desc[0] for desc in cur.description] if cur.description else []
                rows = cur.fetchall()
                data = [[str(v) for v in row] for row in rows]
                return {"columns": columns, "rows": data}
        except Exception as exc:
            return {"error": str(exc), "columns": [], "rows": []}

    def can_execute(self) -> bool:
        return self.mode == "live" and bool(self.host)


def query_table(
    table_ref: str,
    limit: int = 100,
    host: Optional[str] = None,
    token: Optional[str] = None,
) -> dict:
    """Module-level convenience wrapper for querying a Unity Catalog table."""
    pub = DatabricksPublisher(host=host, token=token)
    return pub.query_table(table_ref, limit)
