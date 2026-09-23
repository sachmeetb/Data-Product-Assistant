"""
databricks_tool.py — Databricks SQL execution helper.

Takes the SQL code produced by the Pipeline Generator and executes it
statement-by-statement against Databricks Unity Catalog using:
  - Statement Execution API  (/api/2.0/sql/statements)  — DDL + DML
  - Unity Catalog REST API   (/api/2.1/unity-catalog/schemas) — schema creation

Environment variables (from backend/.env):
  DATABRICKS_HOST
  DATABRICKS_TOKEN
  DATABRICKS_SQL_WAREHOUSE_ID
  DATABRICKS_PUBLISHER_MODE   — "live" | "dry_run" | "auto" (default: auto)
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent.parent
load_dotenv(ROOT_DIR / ".env")

DEFAULT_PUBLISHER_MODE = os.environ.get("DATABRICKS_PUBLISHER_MODE", "auto").strip().lower()

# Disable SSL verification when behind a corporate proxy with self-signed certs.
# Set DATABRICKS_SSL_VERIFY=true in .env to re-enable.
_SSL_VERIFY = os.environ.get("DATABRICKS_SSL_VERIFY", "false").strip().lower() != "false"


# ── Low-level Databricks API helpers ─────────────────────────────────────────

def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def run_sql(host: str, token: str, warehouse_id: str, statement: str) -> dict:
    """
    Execute a single SQL statement via the Statement Execution API.
    Waits (polls) until the statement finishes and returns the result dict.
    Raises RuntimeError on failure.
    """
    url = f"{host.rstrip('/')}/api/2.0/sql/statements"
    resp = requests.post(
        url,
        headers=_headers(token),
        json={
            "warehouse_id": warehouse_id,
            "statement": statement,
            "wait_timeout": "30s",
        },
        timeout=60,
        verify=_SSL_VERIFY,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"SQL API error {resp.status_code}: {resp.text}")

    result = resp.json()
    statement_id = result.get("statement_id")
    state = result.get("status", {}).get("state", "")

    # Poll until terminal state
    while state in ("PENDING", "RUNNING"):
        time.sleep(1)
        poll = requests.get(
            f"{host.rstrip('/')}/api/2.0/sql/statements/{statement_id}",
            headers=_headers(token),
            timeout=30,
            verify=_SSL_VERIFY,
        )
        result = poll.json()
        state = result.get("status", {}).get("state", "")

    if state == "SUCCEEDED":
        return result

    error = result.get("status", {}).get("error", {})
    raise RuntimeError(
        f"SQL failed (state={state}): {error.get('message', json.dumps(error))}"
    )


# ── SQL parsing helpers ───────────────────────────────────────────────────────

def _split_statements(sql_code: str) -> list[str]:
    """
    Split a multi-statement SQL script into individual executable statements.
    Strips line comments, splits on semicolons, and filters blanks.
    """
    lines = []
    for line in sql_code.splitlines():
        stripped = line.strip()
        if not stripped.startswith("--"):
            lines.append(line)

    stmts = []
    for chunk in "\n".join(lines).split(";"):
        stmt = chunk.strip()
        # Only keep chunks that contain a real SQL keyword
        if stmt and re.search(
            r"\b(CREATE|INSERT|MERGE|UPDATE|DELETE|DROP|ALTER|SELECT|WITH)\b",
            stmt,
            re.IGNORECASE,
        ):
            stmts.append(stmt)
    return stmts


def _extract_table_names(statements: list[str]) -> list[str]:
    """Extract fully-qualified table names from CREATE TABLE statements."""
    tables = []
    for stmt in statements:
        m = re.search(
            r"CREATE\s+(?:OR\s+REPLACE\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
            r"([`'\"\w.]+)",
            stmt,
            re.IGNORECASE,
        )
        if m:
            tables.append(m.group(1).strip("`'\""))
    return tables


# ── Table query helper ────────────────────────────────────────────────────────

def query_table(
    host: str,
    token: str,
    warehouse_id: str,
    table_name: str,
    limit: int = 500,
) -> dict:
    """
    Run SELECT * FROM <table> LIMIT <limit> and return columns + rows.
    Returns {"columns": [...], "rows": [[...], ...]} or {"error": "..."}.
    """
    try:
        result = run_sql(host, token, warehouse_id, f"SELECT * FROM {table_name} LIMIT {limit}")
        manifest = result.get("manifest", {})
        schema   = manifest.get("schema", {})
        columns  = [c.get("name", "") for c in schema.get("columns", [])]
        rows     = result.get("result", {}).get("data_array", [])
        return {"columns": columns, "rows": rows}
    except RuntimeError as exc:
        return {"error": str(exc), "columns": [], "rows": []}


# ── Publisher ─────────────────────────────────────────────────────────────────

class DatabricksPublisher:
    """
    Executes the SQL pipeline code produced by the Pipeline Generator
    against Databricks Unity Catalog.

    Flow for each statement:
      - CREATE SCHEMA  →  Unity Catalog REST API  (handles IF NOT EXISTS / 409)
      - Everything else → Statement Execution API  (CREATE TABLE, MERGE, INSERT …)
    """

    def __init__(
        self,
        host: str | None = None,
        token: str | None = None,
        warehouse_id: str | None = None,
        mode: str | None = None,
    ):
        self.host = (host or os.environ.get("DATABRICKS_HOST", "") or "").rstrip("/")
        self.token = token or os.environ.get("DATABRICKS_TOKEN", "")
        self.warehouse_id = warehouse_id or os.environ.get("DATABRICKS_SQL_WAREHOUSE_ID", "")

        _mode = (
            mode or os.environ.get("DATABRICKS_PUBLISHER_MODE", DEFAULT_PUBLISHER_MODE)
        ).strip().lower()
        if _mode not in {"live", "dry_run", "auto"}:
            _mode = "auto"
        if _mode == "auto":
            _mode = "live" if (self.host and self.token and self.warehouse_id) else "dry_run"
        self.mode = _mode

    def publish(self, pipeline_code: str | None = None) -> dict:
        """
        Execute the SQL pipeline code in Databricks.

        Args:
            pipeline_code: Multi-statement SQL script from the Pipeline Generator.

        Returns:
            Publish report dict with keys:
              publish_status   — "published" | "partial" | "failed" | "dry_run"
              published_tables — list of table names from CREATE TABLE statements
              executed_statements — statements that ran successfully
              failed_statements   — list of {statement, error} dicts
              summary          — human-readable result
        """
        if not pipeline_code or not pipeline_code.strip():
            return {
                "publish_status": "failed",
                "published_tables": [],
                "summary": "No pipeline code provided to execute.",
            }

        statements = _split_statements(pipeline_code)
        if not statements:
            return {
                "publish_status": "failed",
                "published_tables": [],
                "summary": "No executable SQL statements found in pipeline code.",
            }

        report: dict = {
            "publish_status": "in_progress",
            "published_tables": [],
            "executed_statements": [],
            "failed_statements": [],
            "total_statements": len(statements),
            "summary": "",
        }

        # ── Dry-run mode — plan only, no execution ────────────────────────────
        if self.mode == "dry_run":
            report["publish_status"] = "dry_run"
            report["executed_statements"] = statements
            report["published_tables"] = _extract_table_names(statements)
            report["summary"] = (
                f"Dry run: {len(statements)} statement(s) planned, "
                f"{len(report['published_tables'])} table(s) identified."
            )
            return report

        # ── Live mode — execute each statement ────────────────────────────────
        for stmt in statements:
            is_create_schema = bool(
                re.match(r"\s*CREATE\s+SCHEMA\b", stmt, re.IGNORECASE)
            )
            try:
                if is_create_schema:
                    self._execute_create_schema(stmt)
                else:
                    run_sql(self.host, self.token, self.warehouse_id, stmt)
                report["executed_statements"].append(stmt)
            except RuntimeError as exc:
                err_str = str(exc)
                is_grant = bool(re.match(r"\s*GRANT\b", stmt, re.IGNORECASE))
                is_drop  = bool(re.match(r"\s*DROP\b", stmt, re.IGNORECASE))
                already_exists = (
                    "DELTA_CONSTRAINT_ALREADY_EXISTS" in err_str
                    or "SCHEMA_ALREADY_EXISTS" in err_str
                    or (is_grant and "PRINCIPAL_DOES_NOT_EXIST" in err_str)
                    # DROP TABLE/VIEW on a non-existent object is a no-op — treat as success
                    # so pipelines that defensively drop before create don't show false errors.
                    or (is_drop and "TABLE_OR_VIEW_NOT_FOUND" in err_str)
                )
                if already_exists:
                    report["executed_statements"].append(stmt)  # idempotent — treat as success
                else:
                    report["failed_statements"].append({
                        "statement": stmt[:300],
                        "error": err_str,
                    })

        tables = _extract_table_names(report["executed_statements"])
        report["published_tables"] = tables

        n_ok = len(report["executed_statements"])
        n_fail = len(report["failed_statements"])

        if n_fail == 0:
            report["publish_status"] = "published"
            report["summary"] = (
                f"All {n_ok} statement(s) executed successfully. "
                f"Tables: {', '.join(tables) or 'none'}."
            )
        elif n_ok > 0:
            report["publish_status"] = "partial"
            first_errors = "; ".join(
                f["error"] for f in report["failed_statements"][:2]
            )
            report["summary"] = (
                f"{n_ok} statement(s) succeeded, {n_fail} failed. "
                f"Errors: {first_errors}"
            )
        else:
            report["publish_status"] = "failed"
            first_errors = "; ".join(
                f["error"] for f in report["failed_statements"][:2]
            )
            report["summary"] = f"All {n_fail} statement(s) failed. Errors: {first_errors}"

        return report

    def _execute_create_schema(self, stmt: str) -> None:
        """
        Execute a CREATE SCHEMA statement via the SQL Statement Execution API.
        Uses SQL only — avoids the Unity Catalog REST API which requires
        the 'unity-catalog' token scope that most PATs don't have.
        CREATE SCHEMA IF NOT EXISTS is idempotent so re-runs are safe.
        """
        run_sql(self.host, self.token, self.warehouse_id, stmt)

    def can_execute(self) -> bool:
        return self.mode == "live" and bool(
            self.host and self.token and self.warehouse_id
        )
