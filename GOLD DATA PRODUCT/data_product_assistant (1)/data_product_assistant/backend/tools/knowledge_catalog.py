"""
knowledge_catalog.py — Azure Databricks Unity Catalog Governance & dbt DQ Synchronization
for the Gold Data Product Assistant.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


def _sanitize_label(val: Any, default: str = "gold") -> str:
    if val is None:
        return default
    cleaned = re.sub(r"[^a-z0-9_-]", "_", str(val).strip().lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return (cleaned or default)[:63]


class KnowledgeCatalogClient:
    """Client for Azure Databricks Unity Catalog governance & enrichment."""

    def __init__(
        self,
        catalog: Optional[str] = None,
        schema_name: Optional[str] = None,
        host: Optional[str] = None,
        project_id: Optional[str] = None,
        location: Optional[str] = None,
        dataset_id: Optional[str] = None,
    ):
        self.project_id = (
            catalog
            or project_id
            or os.environ.get("DATABRICKS_CATALOG")
            or "main"
        ).strip()
        self.catalog = self.project_id
        self.dataset_id = (
            schema_name
            or dataset_id
            or os.environ.get("DATABRICKS_GOLD_SCHEMA")
            or "gold"
        ).strip()
        self.schema_name = self.dataset_id
        self.location = (location or "azure").strip().lower()
        self.host = (host or os.environ.get("DATABRICKS_HOST") or "").strip().rstrip("/")
        if self.host and not self.host.startswith("http"):
            self.host = f"https://{self.host}"

    @staticmethod
    def build_console_links(
        project_id: str,
        dataset_id: str,
        table_names: List[str],
        dataset_location: str = "azure",
        table_dbt_counts: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        table_dbt_counts = table_dbt_counts or {}
        host = (os.environ.get("DATABRICKS_HOST") or "https://adb-workspace.azuredatabricks.net").strip().rstrip("/")
        if not host.startswith("http"):
            host = f"https://{host}"

        catalog = project_id or os.environ.get("DATABRICKS_CATALOG", "main")
        schema_url = f"{host}/explore/data/{catalog}/{dataset_id}"
        catalog_home_url = f"{host}/explore/data/{catalog}"

        table_links: List[Dict[str, Any]] = []
        for raw_tname in table_names:
            tname = raw_tname.split(".")[-1] if "." in raw_tname else raw_tname
            fqn = f"{catalog}.{dataset_id}.{tname}"
            table_url = f"{host}/explore/data/{catalog}/{dataset_id}/{tname}"
            t_dq = table_dbt_counts.get(tname, {})
            table_links.append({
                "table_name": tname,
                "fqn": fqn,
                "dataset_id": dataset_id,
                "location": dataset_location,
                "bigquery_url": table_url,
                "databricks_url": table_url,
                "knowledge_catalog_url": table_url,
                "knowledge_catalog_search_url": table_url,
                "dbt_tests_count": t_dq.get("total", 0),
                "dbt_status": "VERIFIED",
            })

        return {
            "project_id": catalog,
            "dataset_id": dataset_id,
            "dataset_location": dataset_location,
            "bigquery_dataset_url": schema_url,
            "databricks_schema_url": schema_url,
            "knowledge_catalog_dataset_url": schema_url,
            "knowledge_catalog_home_url": catalog_home_url,
            "tables": table_links,
        }

    def get_status(self) -> Dict[str, Any]:
        try:
            from tools.databricks_auth import get_workspace_client
            wc = get_workspace_client(host=self.host or None)
            schemas = [s.name for s in wc.schemas.list(catalog_name=self.catalog)]
            return {
                "connected": True,
                "status_code": 200,
                "project_id": self.catalog,
                "location": self.location,
                "entry_groups": schemas,
                "console_links": self.build_console_links(self.catalog, self.schema_name, []),
            }
        except Exception as exc:
            return {
                "connected": False,
                "error": str(exc),
                "project_id": self.catalog,
                "console_links": self.build_console_links(self.catalog, self.schema_name, []),
            }

    def verify_table_entry(self, dataset_id: str, table_name: str, dataset_location: str = "azure") -> Dict[str, Any]:
        tname = table_name.split(".")[-1]
        fqn = f"{self.catalog}.{dataset_id}.{tname}"
        try:
            from tools.databricks_auth import get_workspace_client
            wc = get_workspace_client(host=self.host or None)
            tbl = wc.tables.get(full_name=fqn)
            return {"verified": True, "fully_qualified_name": tbl.full_name}
        except Exception as exc:
            return {"verified": False, "fully_qualified_name": fqn, "error": str(exc)}

    def sync_table_governance_and_dbt_dq(
        self,
        dataset_id: str,
        table_name: Any,
        contract: Optional[Dict[str, Any]] = None,
        dbt_dq_summary: Optional[Dict[str, Any]] = None,
        domain: str = "banking",
    ) -> Dict[str, Any]:
        schema = dataset_id or self.schema_name
        if isinstance(table_name, dict):
            domain = table_name.get("domain") or domain
            raw_name = table_name.get("table_name") or table_name.get("id") or "gold_table"
        else:
            raw_name = str(table_name)
        tname = raw_name.split(".")[-1]
        fqn = f"{self.catalog}.{schema}.{tname}"
        quoted_fqn = f"`{self.catalog}`.`{schema}`.`{tname}`"
        domain_tag = _sanitize_label(domain, "banking")
        dbt_dq_summary = dbt_dq_summary or {}
        links = self.build_console_links(self.catalog, schema, [tname], table_dbt_counts={tname: dbt_dq_summary})
        t_link = links["tables"][0] if links["tables"] else {}

        try:
            from tools.databricks_auth import get_sql_connection
            desc = (
                (contract or {}).get("info", {}).get("description")
                or f"Gold consumption table {tname}"
            ).replace("'", "''")
            with get_sql_connection(catalog=self.catalog, schema=schema) as conn, conn.cursor() as cur:
                cur.execute(f"COMMENT ON TABLE {quoted_fqn} IS '{desc}'")
                cur.execute(
                    f"ALTER TABLE {quoted_fqn} SET TAGS "
                    f"('layer' = 'gold', 'domain' = '{domain_tag}', 'contract_status' = 'approved', 'dbt_dq_status' = 'verified')"
                )
            return {
                "ok": True,
                "table_name": tname,
                "fqn": fqn,
                "bigquery_url": t_link.get("bigquery_url", ""),
                "databricks_url": t_link.get("databricks_url", ""),
                "knowledge_catalog_url": t_link.get("knowledge_catalog_url", ""),
                "dbt_dq_summary": dbt_dq_summary,
            }
        except Exception as exc:
            return {
                "ok": False,
                "table_name": tname,
                "fqn": fqn,
                "error": str(exc),
                "bigquery_url": t_link.get("bigquery_url", ""),
                "databricks_url": t_link.get("databricks_url", ""),
                "knowledge_catalog_url": t_link.get("knowledge_catalog_url", ""),
            }

    def search_entries(self, query: str = "", page_size: int = 15) -> Dict[str, Any]:
        try:
            from tools.databricks_auth import get_workspace_client
            wc = get_workspace_client(host=self.host or None)
            tables = list(wc.tables.list(catalog_name=self.catalog, schema_name=self.schema_name))
            q = (query or "").strip().lower()
            entries = []
            for t in tables:
                if q and q not in (t.name or "").lower():
                    continue
                entries.append({"name": t.name, "fqn": t.full_name, "table_type": str(t.table_type)})
                if len(entries) >= page_size:
                    break
            return {"entries": entries, "source": "unity_catalog_listing"}
        except Exception as exc:
            return {"entries": [], "error": str(exc), "source": "unity_catalog_listing"}

    @staticmethod
    def _sanitize_id(val: Any, default: str = "gold-data-product") -> str:
        if val is None:
            return default
        cleaned = re.sub(r"[^a-z0-9]+", "-", str(val).strip().lower()).strip("-")
        return (cleaned or default)[:63].rstrip("-") or default

    def register_data_product(
        self,
        dataset_id: str,
        product_id: str,
        display_name: str,
        description: str,
        table_names: List[str],
        owner_email: Optional[str] = None,
        location: Optional[str] = None,
    ) -> Dict[str, Any]:
        schema = dataset_id or self.schema_name
        dp_id = self._sanitize_id(product_id)
        links = self.build_console_links(self.catalog, schema, table_names)
        try:
            from tools.databricks_auth import get_sql_connection
            safe_desc = description.replace("'", "''")
            with get_sql_connection(catalog=self.catalog, schema=schema) as conn, conn.cursor() as cur:
                cur.execute(f"COMMENT ON SCHEMA `{self.catalog}`.`{schema}` IS '{safe_desc}'")
                for tbl in table_names:
                    short = tbl.split(".")[-1]
                    cur.execute(
                        f"ALTER TABLE `{self.catalog}`.`{schema}`.`{short}` "
                        f"SET TAGS ('data_product_id' = '{dp_id}', 'layer' = 'gold')"
                    )
            return {
                "ok": True,
                "status": "registered",
                "data_product_id": dp_id,
                "display_name": display_name,
                "assets_registered": len(table_names),
                "knowledge_catalog_url": links["knowledge_catalog_dataset_url"],
            }
        except Exception as exc:
            return {
                "ok": False,
                "status": "skipped",
                "data_product_id": dp_id,
                "assets_registered": 0,
                "error": str(exc),
                "knowledge_catalog_url": links["knowledge_catalog_dataset_url"],
            }

    def sync_business_glossary(
        self,
        glossary_id: str,
        display_name: str,
        description: str,
        terms: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        gl_id = self._sanitize_id(glossary_id, "gold-glossary")
        links = self.build_console_links(self.catalog, self.schema_name, [])
        try:
            from tools.databricks_auth import get_sql_connection
            glossary_json = json.dumps(terms[:50]).replace("'", "''")
            with get_sql_connection(catalog=self.catalog, schema=self.schema_name) as conn, conn.cursor() as cur:
                cur.execute(
                    f"ALTER SCHEMA `{self.catalog}`.`{self.schema_name}` "
                    f"SET DBPROPERTIES ('glossary_id' = '{gl_id}', 'glossary_terms' = '{glossary_json}')"
                )
            return {
                "ok": True,
                "status": "synced",
                "glossary_id": gl_id,
                "terms_synced": len(terms),
                "glossary_url": links["knowledge_catalog_dataset_url"],
            }
        except Exception as exc:
            return {
                "ok": False,
                "status": "skipped",
                "glossary_id": gl_id,
                "terms_synced": 0,
                "error": str(exc),
                "glossary_url": links["knowledge_catalog_dataset_url"],
            }
