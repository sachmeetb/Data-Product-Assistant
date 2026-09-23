"""
bigquery_tool.py — BigQuery SQL execution helper.

Takes the SQL code produced by the Pipeline Generator and executes it
statement-by-statement against Google BigQuery using the Python client library.
Authentication uses Application Default Credentials (already configured via
the Vertex AI setup — no extra credentials file needed).

Environment variables:
  BQ_PROJECT       — GCP project for BigQuery (defaults to GOOGLE_CLOUD_PROJECT)
  BQ_LOCATION      — BigQuery job/dataset location (default: us-central1)
  BQ_PUBLISHER_MODE — "live" | "dry_run" | "auto" (default: auto)
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent.parent
load_dotenv(ROOT_DIR / ".env")

_BQ_PROJECT  = os.environ.get("BQ_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
_BQ_LOCATION = os.environ.get("BQ_LOCATION", "us-central1")
DEFAULT_PUBLISHER_MODE = os.environ.get("BQ_PUBLISHER_MODE", "auto").strip().lower()


# ── SQL parsing helpers ───────────────────────────────────────────────────────

def _split_statements(sql_code: str) -> list[str]:
    """Split a multi-statement SQL script into individual executable statements."""
    lines = []
    for line in sql_code.splitlines():
        stripped = line.strip()
        if not stripped.startswith("--"):
            lines.append(line)

    stmts = []
    for chunk in "\n".join(lines).split(";"):
        stmt = chunk.strip()
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
            r"([`'\"\w.\-]+)",
            stmt,
            re.IGNORECASE,
        )
        if m:
            tables.append(m.group(1).strip("`'\""))
    return tables


# ── Low-level BigQuery helpers ────────────────────────────────────────────────

def _bq_client(project: str | None = None):
    """Return a BigQuery client, lazily imported."""
    from google.cloud import bigquery  # noqa: PLC0415
    return bigquery.Client(project=project or _BQ_PROJECT, location=_BQ_LOCATION)


def run_bq_query(sql: str, project: str | None = None, location: str | None = None) -> dict:
    """
    Execute a single BigQuery SQL statement and wait for it to complete.
    Returns a dict with 'rows' (list of row dicts) or raises RuntimeError on failure.
    """
    from google.cloud.exceptions import GoogleCloudError  # noqa: PLC0415
    client = _bq_client(project)
    loc = location or _BQ_LOCATION
    try:
        job = client.query(sql, location=loc)
        rows = list(job.result())  # blocks until done; raises on error
        return {
            "rows": [dict(r) for r in rows],
            "total_rows": job.result().total_rows if hasattr(job.result(), "total_rows") else len(rows),
        }
    except GoogleCloudError as exc:
        raise RuntimeError(str(exc)) from exc
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc


def query_table(
    table_name: str,
    limit: int = 500,
    project: str | None = None,
) -> dict:
    """
    Run SELECT * FROM <table> LIMIT <limit> and return columns + rows.
    Returns {"columns": [...], "rows": [[...], ...]} or {"error": "..."}.
    """
    try:
        result = run_bq_query(f"SELECT * FROM `{table_name}` LIMIT {limit}", project=project)
        rows = result.get("rows", [])
        columns = list(rows[0].keys()) if rows else []
        row_arrays = [[r.get(c) for c in columns] for r in rows]
        return {"columns": columns, "rows": row_arrays}
    except RuntimeError as exc:
        return {"error": str(exc), "columns": [], "rows": []}


# ── Publisher ─────────────────────────────────────────────────────────────────

class BigQueryPublisher:
    """
    Executes the SQL pipeline code produced by the Pipeline Generator
    against Google BigQuery.

    All statements are executed via the BigQuery client. CREATE SCHEMA
    (i.e. CREATE SCHEMA IF NOT EXISTS project.dataset) creates a BigQuery
    dataset. GRANTs are not supported in BigQuery SQL — use IAM instead;
    any GRANT statement in the script is logged and skipped.

    Mode:
      live     — execute against BigQuery (requires BQ_PROJECT + credentials)
      dry_run  — parse only, no execution
      auto     — live if BQ_PROJECT is set, else dry_run
    """

    def __init__(
        self,
        project: str | None = None,
        location: str | None = None,
        mode: str | None = None,
    ):
        self.project  = (project  or _BQ_PROJECT  or "").strip()
        self.location = (location or _BQ_LOCATION or "us-central1").strip()

        _mode = (mode or DEFAULT_PUBLISHER_MODE).strip().lower()
        if _mode not in {"live", "dry_run", "auto"}:
            _mode = "auto"
        if _mode == "auto":
            _mode = "live" if self.project else "dry_run"
        self.mode = _mode

    def publish(self, pipeline_code: str | None = None) -> dict:
        """
        Execute the SQL pipeline code in BigQuery.

        Returns:
            Publish report dict with keys:
              publish_status      — "published" | "partial" | "failed" | "dry_run"
              published_tables    — list of table FQNs from CREATE TABLE statements
              executed_statements — statements that ran successfully
              failed_statements   — list of {statement, error} dicts
              summary             — human-readable result
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

        # ── Dry-run mode ──────────────────────────────────────────────────────
        if self.mode == "dry_run":
            report["publish_status"] = "dry_run"
            report["executed_statements"] = statements
            report["published_tables"] = _extract_table_names(statements)
            report["summary"] = (
                f"Dry run: {len(statements)} statement(s) planned, "
                f"{len(report['published_tables'])} table(s) identified."
            )
            return report

        # ── Live mode ─────────────────────────────────────────────────────────
        for stmt in statements:
            is_grant = bool(re.match(r"\s*GRANT\b", stmt, re.IGNORECASE))
            if is_grant:
                # BigQuery uses IAM, not SQL GRANTs — skip silently
                report["executed_statements"].append(stmt)
                continue

            try:
                run_bq_query(stmt, project=self.project, location=self.location)
                report["executed_statements"].append(stmt)
            except RuntimeError as exc:
                err_str = str(exc)
                is_drop = bool(re.match(r"\s*DROP\b", stmt, re.IGNORECASE))
                already_exists = (
                    "Already Exists" in err_str
                    or "already exists" in err_str.lower()
                    or (is_drop and "Not found" in err_str)
                )
                if already_exists:
                    report["executed_statements"].append(stmt)
                else:
                    report["failed_statements"].append({
                        "statement": stmt[:300],
                        "error": err_str,
                    })

        tables = _extract_table_names(report["executed_statements"])
        report["published_tables"] = tables

        n_ok   = len(report["executed_statements"])
        n_fail = len(report["failed_statements"])

        if n_fail == 0:
            report["publish_status"] = "published"
            report["summary"] = (
                f"All {n_ok} statement(s) executed successfully. "
                f"Tables: {', '.join(tables) or 'none'}."
            )
        elif n_ok > 0:
            report["publish_status"] = "partial"
            first_errors = "; ".join(f["error"] for f in report["failed_statements"][:2])
            report["summary"] = (
                f"{n_ok} statement(s) succeeded, {n_fail} failed. Errors: {first_errors}"
            )
        else:
            report["publish_status"] = "failed"
            first_errors = "; ".join(f["error"] for f in report["failed_statements"][:2])
            report["summary"] = f"All {n_fail} statement(s) failed. Errors: {first_errors}"

        return report

    def can_execute(self) -> bool:
        return self.mode == "live" and bool(self.project)
