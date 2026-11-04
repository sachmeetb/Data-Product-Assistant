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

_BQ_PROJECT  = os.environ.get("BQ_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT") or "eogwapq-agbg-internal-data-mig"
_BQ_LOCATION = os.environ.get("BQ_LOCATION", "us-central1")
DEFAULT_PUBLISHER_MODE = os.environ.get("BQ_PUBLISHER_MODE", "live").strip().lower()


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
    return bigquery.Client(project=project or _BQ_PROJECT)


def run_bq_query(sql: str, project: str | None = None, location: str | None = None) -> dict:
    """
    Execute a single BigQuery SQL statement and wait for it to complete.
    Returns a dict with 'rows' (list of row dicts) or raises RuntimeError on failure.
    """
    from google.cloud.exceptions import GoogleCloudError  # noqa: PLC0415
    client = _bq_client(project)
    try:
        job = client.query(sql, location=location) if location else client.query(sql)
        rows = list(job.result(timeout=1800))  # 30 min cap for large DDL/DML jobs
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
    clean_tbl = table_name.strip("`'\"")
    if clean_tbl.count(".") == 1:
        clean_tbl = f"{project or _BQ_PROJECT}.{clean_tbl}"
    try:
        result = run_bq_query(f"SELECT * FROM `{clean_tbl}` LIMIT {limit}", project=project)
        rows = result.get("rows", [])
        columns = list(rows[0].keys()) if rows else []
        row_arrays = [[str(r.get(c)) if r.get(c) is not None else "" for c in columns] for r in rows]
        return {"columns": columns, "rows": row_arrays}
    except RuntimeError as exc:
        return {"error": str(exc), "columns": [], "rows": []}


# ── Publisher ─────────────────────────────────────────────────────────────────

class BigQueryPublisher:
    """
    Executes the SQL pipeline code / Star Schema DDL produced by the Gold Layer
    against Google BigQuery.

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
        gold_dataset: str | None = None,
    ):
        self.project = (project or _BQ_PROJECT or "eogwapq-agbg-internal-data-mig").strip()
        self.project_id = self.project
        self.location = (location or _BQ_LOCATION or "us-central1").strip()
        self.gold_dataset = (gold_dataset or os.environ.get("BQ_DATASET_GOLD", "gold")).strip()

        _mode = (mode or DEFAULT_PUBLISHER_MODE).strip().lower()
        if _mode not in {"live", "dry_run", "auto"}:
            _mode = "live"
        if _mode == "auto":
            _mode = "live" if self.project else "dry_run"
        self.mode = _mode
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = _bq_client(self.project)
        return self._client

    def _ensure_dataset(self, dataset_id: str) -> str:
        """Ensure BigQuery dataset exists and return its actual GCP location."""
        from google.cloud import bigquery  # noqa: PLC0415
        client = self._get_client()
        clean_ds = dataset_id.strip("`'\"")
        ds_ref = f"{self.project}.{clean_ds}"
        try:
            existing = client.get_dataset(ds_ref)
            return existing.location or self.location
        except Exception:
            ds = bigquery.Dataset(ds_ref)
            ds.location = self.location
            ds.description = f"Gold Consumption Mart Dataset — {clean_ds} (Managed by Gold Data Product Assistant)"
            created = client.create_dataset(ds, exists_ok=True)
            return created.location or self.location

    def get_dataset_location(self, dataset_id: str) -> str:
        """Return the GCP location (e.g. 'US' or 'us-central1') of a BigQuery dataset."""
        try:
            client = self._get_client()
            ds = client.get_dataset(f"{self.project}.{dataset_id.strip('`')}")
            return ds.location or self.location
        except Exception:
            return self.location

    def publish(self, pipeline_code: str | None = None) -> dict:
        """
        Execute the SQL pipeline / Star Schema DDL in BigQuery.
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

        planned_tables = _extract_table_names(statements)
        detected_datasets = set()
        for tref in planned_tables:
            parts = tref.strip("`").split(".")
            if len(parts) == 3:
                detected_datasets.add(parts[1])
            elif len(parts) == 2:
                detected_datasets.add(parts[0])
        if not detected_datasets:
            detected_datasets.add(self.gold_dataset)

        primary_dataset = sorted(detected_datasets)[0]

        report: dict = {
            "publish_status": "in_progress",
            "published_tables": [],
            "executed_statements": [],
            "executed_count": 0,
            "failed_statements": [],
            "total_statements": len(statements),
            "summary": "",
            "project_id": self.project,
            "dataset_id": primary_dataset,
            "gold_dataset": f"{self.project}.{primary_dataset}",
            "dataset_location": self.location,
        }

        # ── Dry-run mode ──────────────────────────────────────────────────────
        if self.mode == "dry_run":
            report["publish_status"] = "dry_run"
            report["executed_statements"] = statements
            report["published_tables"] = planned_tables
            report["summary"] = (
                f"Dry run: {len(statements)} statement(s) planned, "
                f"{len(report['published_tables'])} table(s) identified."
            )
            return report

        # ── Live mode ─────────────────────────────────────────────────────────
        dataset_location = self.location
        try:
            for ds_name in detected_datasets:
                dataset_location = self._ensure_dataset(ds_name)
            report["dataset_location"] = dataset_location
        except Exception as exc:
            report["publish_status"] = "failed"
            report["summary"] = f"Dataset creation failed: {exc}"
            return report

        for stmt in statements:
            is_grant = bool(re.match(r"\s*GRANT\b", stmt, re.IGNORECASE))
            if is_grant:
                report["executed_statements"].append(stmt)
                report["executed_count"] += 1
                continue

            try:
                run_bq_query(stmt, project=self.project)
                report["executed_statements"].append(stmt)
                report["executed_count"] += 1
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
                    report["executed_count"] += 1
                else:
                    report["failed_statements"].append({
                        "statement": stmt[:300],
                        "error": err_str,
                    })

        raw_tables = _extract_table_names(report["executed_statements"])
        fqn_tables = []
        for t in raw_tables:
            clean_t = t.strip("`'\"")
            if clean_t.count(".") == 1:
                fqn_tables.append(f"{self.project}.{clean_t}")
            elif clean_t.count(".") == 0:
                fqn_tables.append(f"{self.project}.{primary_dataset}.{clean_t}")
            else:
                fqn_tables.append(clean_t)
        report["published_tables"] = fqn_tables

        n_ok = len(report["executed_statements"])
        n_fail = len(report["failed_statements"])

        if n_fail == 0:
            report["publish_status"] = "published"
            report["summary"] = (
                f"All {n_ok} statement(s) executed successfully in BigQuery ({self.project}). "
                f"Tables: {', '.join(fqn_tables) or 'none'}."
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

    def seed_sample_rows(
        self,
        saved_entities: list[dict],
        target_dataset: str | None = None,
    ) -> dict:
        """
        Seed 3 realistic, conformed sample rows into each published Gold Fact & Dimension table
        so that BigQuery Studio preview, Knowledge Catalog data profile, and the UI
        preview table immediately show live rows.
        """
        if self.mode == "dry_run":
            return {"seeded_tables": {}, "actual_table_data": {}}

        ds_id = (target_dataset or self.gold_dataset or "gold").strip("`")
        seeded_counts: dict = {}
        actual_table_data: dict = {}

        for ent in saved_entities or []:
            tname = (ent.get("name") or ent.get("table_name") or ent.get("id") or "").strip("`").split(".")[-1]
            if not tname:
                continue
            fqn = f"{self.project}.{ds_id}.{tname}"
            cols = ent.get("columns", [])
            if not cols:
                continue

            col_names = [c.get("name") for c in cols if c.get("name")]
            row_tuples = []
            for row_idx in range(1, 4):
                vals = []
                for c in cols:
                    cname = (c.get("name") or "").lower()
                    ctype = (c.get("data_type") or c.get("type") or "STRING").upper()
                    if cname.endswith("_sk") and ctype in ("STRING", "VARCHAR", "TEXT"):
                        vals.append(f"'gold_{tname[-6:]}_{row_idx:04d}_sk'")
                    elif cname.endswith("_sk") and ctype in ("INT64", "INTEGER", "BIGINT"):
                        vals.append(f"{10000 + row_idx}")
                    elif cname in ("load_ts", "updated_at", "created_at") or "timestamp" in ctype or cname.endswith("_ts"):
                        vals.append(f"TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {row_idx} HOUR)")
                    elif "date" in ctype or cname.endswith("_date") or cname.endswith("_dt"):
                        vals.append(f"DATE_SUB(CURRENT_DATE(), INTERVAL {row_idx} DAY)")
                    elif cname == "dq_status":
                        vals.append("'VALID'")
                    elif "currency" in cname or cname.endswith("_ccy"):
                        vals.append("'USD'" if row_idx % 2 == 1 else "'EUR'")
                    elif "status" in cname or "state" in cname or "tier" in cname:
                        vals.append("'VERIFIED'" if row_idx < 3 else "'ACTIVE'")
                    elif ctype in ("NUMERIC", "BIGNUMERIC", "FLOAT64", "DECIMAL", "FLOAT"):
                        vals.append(f"{round(14500.75 * row_idx, 2)}")
                    elif ctype in ("INT64", "INTEGER", "SMALLINT", "BIGINT"):
                        vals.append(f"{100 + row_idx}")
                    elif ctype in ("BOOL", "BOOLEAN"):
                        vals.append("TRUE" if row_idx % 2 == 1 else "FALSE")
                    elif c.get("is_pk") or c.get("primary") or cname.endswith("_id") or cname.endswith("_key"):
                        prefix = cname.replace("_id", "").replace("_key", "").upper()[:6] or "GLD"
                        vals.append(f"'{prefix}_{2000 + row_idx}'")
                    else:
                        clean_label = cname.replace("_", " ").title()
                        vals.append(f"'{clean_label} #{row_idx}'")
                row_tuples.append(f"({', '.join(vals)})")

            insert_sql = (
                f"INSERT INTO `{fqn}` ({', '.join(col_names)}) "
                f"VALUES {', '.join(row_tuples)}"
            )
            try:
                run_bq_query(insert_sql, project=self.project)
                seeded_counts[fqn] = 3
            except Exception:
                pass

            q_res = query_table(fqn, limit=10, project=self.project)
            if q_res.get("rows"):
                actual_table_data[fqn] = q_res

        return {
            "seeded_tables": seeded_counts,
            "actual_table_data": actual_table_data,
        }

    def query_table(self, table_ref: str, limit: int = 100) -> dict:
        return query_table(table_ref, limit=limit, project=self.project)

    def can_execute(self) -> bool:
        return self.mode == "live" and bool(self.project)

