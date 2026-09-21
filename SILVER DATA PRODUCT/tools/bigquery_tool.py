"""
bigquery_tool.py — BigQuery execution helper for Silver-layer table publishing.

Replaces databricks_tool.py from the original backend.
Targets GCP BigQuery Standard SQL instead of Databricks Unity Catalog.

Key differences from Databricks:
  - Qualified names: `project.dataset.table` (backtick-quoted)
  - No USING DELTA clause
  - Partition syntax: PARTITION BY DATE(col)
  - Auth: Google ADC or service account JSON key
  - Execution: google-cloud-bigquery SDK (not Databricks Statement API)

Environment variables (from .env):
  GCP_PROJECT_ID
  BQ_SILVER_DATASET      (default: banking_silver)
  BQ_GOLD_DATASET        (default: banking_gold)
  BQ_LOCATION            (default: US)
  GCP_CREDENTIALS_PATH   (optional — uses ADC if absent)
  BQ_PUBLISHER_MODE      live | dry_run | auto  (default: auto)
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

_DEFAULT_MODE = os.environ.get("BQ_PUBLISHER_MODE", "auto").strip().lower()


# ── SQL parsing helpers ───────────────────────────────────────────────────────

def _split_statements(sql_code: str) -> list[str]:
    """
    Split a multi-statement BigQuery SQL script into individual statements.
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
            r"\b(CREATE|INSERT|MERGE|UPDATE|DELETE|DROP|ALTER|SELECT|WITH|CALL)\b",
            stmt,
            re.IGNORECASE,
        ):
            stmts.append(stmt)
    return stmts


def _extract_table_refs(statements: list[str]) -> list[str]:
    """Extract fully-qualified table references from CREATE TABLE statements."""
    tables = []
    for stmt in statements:
        m = re.search(
            r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|MATERIALIZED\s+VIEW)\s+"
            r"(?:IF\s+NOT\s+EXISTS\s+)?[`'\"]?([\w.`]+)[`'\"]?",
            stmt,
            re.IGNORECASE,
        )
        if m:
            tables.append(m.group(1).strip("`'\""))
    return tables


# ── Publisher ─────────────────────────────────────────────────────────────────

class BigQueryPublisher:
    """
    Executes a BigQuery Standard SQL DDL script produced by the Spec Generator.

    Supports:
      - Dataset creation (idempotent — exists_ok=True)
      - CREATE TABLE IF NOT EXISTS
      - DML: INSERT, MERGE for sample data seeding
      - Dry-run mode (plan only, no execution)
    """

    def __init__(
        self,
        project_id: Optional[str] = None,
        silver_dataset: Optional[str] = None,
        gold_dataset: Optional[str] = None,
        credentials_path: Optional[str] = None,
        location: Optional[str] = None,
        mode: Optional[str] = None,
    ):
        self.project_id = project_id or os.environ.get("GCP_PROJECT_ID", "")
        self.silver_dataset = silver_dataset or os.environ.get("BQ_SILVER_DATASET", "banking_silver")
        self.gold_dataset = gold_dataset or os.environ.get("BQ_GOLD_DATASET", "banking_gold")
        self.location = location or os.environ.get("BQ_LOCATION", "US")
        self.credentials_path = credentials_path or os.environ.get("GCP_CREDENTIALS_PATH", "")

        _mode = (mode or _DEFAULT_MODE).strip().lower()
        if _mode not in {"live", "dry_run", "auto"}:
            _mode = "auto"
        if _mode == "auto":
            _mode = "live" if self.project_id else "dry_run"
        self.mode = _mode

        self._client = None  # lazy init

    @property
    def silver_dataset_ref(self) -> str:
        return f"{self.project_id}.{self.silver_dataset}"

    @property
    def gold_dataset_ref(self) -> str:
        return f"{self.project_id}.{self.gold_dataset}"

    def _get_client(self):
        if self._client is None:
            from tools.gcp_auth import get_bigquery_client
            self._client = get_bigquery_client(
                project_id=self.project_id,
                credentials_path=self.credentials_path,
                location=self.location,
            )
        return self._client

    def _ensure_dataset(self, dataset_id: str) -> None:
        """Create BigQuery dataset if it does not exist. Idempotent."""
        from google.cloud import bigquery
        client = self._get_client()
        ds_ref = f"{self.project_id}.{dataset_id}"
        ds = bigquery.Dataset(ds_ref)
        ds.location = self.location
        ds.description = f"BFSI Silver-layer — {dataset_id} (managed by BFSI-Silver-Agent)"
        client.create_dataset(ds, exists_ok=True)
        log.info("Dataset ready: %s", ds_ref)

    def _run_statement(self, sql: str) -> dict:
        """
        Execute one SQL statement.
        Returns {"ok": True} or {"ok": False, "error": "..."}.
        """
        from google.api_core.exceptions import GoogleAPIError
        client = self._get_client()
        try:
            job = client.query(sql)
            job.result()
            return {"ok": True}
        except GoogleAPIError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def publish(self, pipeline_code: str) -> dict:
        """
        Execute a multi-statement BigQuery SQL script.

        Returns:
            {
              publish_status:    "published" | "partial" | "failed" | "dry_run"
              published_tables:  list of table FQNs from CREATE TABLE statements
              executed_count:    int
              failed_statements: [{statement, error}, ...]
              summary:           human-readable result string
              project_id:        GCP project used
              silver_dataset:    fully-qualified silver dataset
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
            "project_id": self.project_id,
            "silver_dataset": self.silver_dataset_ref,
        }

        # ── Dry-run ──────────────────────────────────────────────────────────
        if self.mode == "dry_run":
            report["publish_status"] = "dry_run"
            report["published_tables"] = _extract_table_refs(statements)
            report["executed_count"] = 0
            report["summary"] = (
                f"Dry run: {len(statements)} statement(s) planned, "
                f"{len(report['published_tables'])} table(s) identified."
            )
            return report

        # ── Live ─────────────────────────────────────────────────────────────
        try:
            self._ensure_dataset(self.silver_dataset)
            self._ensure_dataset(self.gold_dataset)
        except Exception as exc:
            report["publish_status"] = "failed"
            report["summary"] = f"Dataset creation failed: {exc}"
            return report

        executed_stmts = []
        for stmt in statements:
            result = self._run_statement(stmt)
            if result["ok"]:
                executed_stmts.append(stmt)
                report["executed_count"] += 1
            else:
                report["failed_statements"].append({
                    "statement": stmt[:300],
                    "error": result["error"],
                })

        tables = _extract_table_refs(executed_stmts)
        report["published_tables"] = tables
        n_ok = report["executed_count"]
        n_fail = len(report["failed_statements"])

        if n_fail == 0:
            report["publish_status"] = "published"
            report["summary"] = (
                f"All {n_ok} statement(s) executed. "
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
        """
        Run SELECT * FROM <table_ref> LIMIT <limit>.
        Returns {"columns": [...], "rows": [[...]]} or {"error": "..."}.
        """
        try:
            client = self._get_client()
            sql = f"SELECT * FROM `{table_ref}` LIMIT {limit}"
            rows = list(client.query(sql).result())
            if not rows:
                return {"columns": [], "rows": []}
            columns = list(rows[0].keys())
            data = [[str(row[c]) for c in columns] for row in rows]
            return {"columns": columns, "rows": data}
        except Exception as exc:
            return {"error": str(exc), "columns": [], "rows": []}

    def can_execute(self) -> bool:
        return self.mode == "live" and bool(self.project_id)


# ── Convenience function ──────────────────────────────────────────────────────

def query_table(project_id: str, credentials_path: str, table_ref: str, limit: int = 100) -> dict:
    """Module-level convenience wrapper for querying a table."""
    pub = BigQueryPublisher(project_id=project_id, credentials_path=credentials_path)
    return pub.query_table(table_ref, limit)
