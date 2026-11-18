"""
knowledge_catalog.py — Azure Databricks Unity Catalog Governance Synchronization
for the Bronze Data Product Assistant.

Responsibilities:
  1. Register Bronze Data Products & Data Assets as Unity Catalog tags/comments.
  2. Synchronize Business Glossary metadata via Unity Catalog table/schema properties.
  3. Enrich live Unity Catalog Bronze tables (COMMENT ON TABLE, SET TAGS).
  4. Generate clickable Azure Databricks Catalog Explorer deep links.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


def _sanitize_label(val: Any, default: str = "bronze") -> str:
    if val is None:
        return default
    cleaned = re.sub(r"[^a-z0-9_-]", "_", str(val).strip().lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return (cleaned or default)[:63]


def _sanitize_id(val: Any, default: str = "bronze-data-product") -> str:
    if val is None:
        return default
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(val).strip().lower()).strip("-")
    return (cleaned or default)[:63].rstrip("-") or default


class KnowledgeCatalogClient:
    """Client for Azure Databricks Unity Catalog governance & enrichment."""

    def __init__(
        self,
        catalog: Optional[str] = None,
        schema_name: Optional[str] = None,
        host: Optional[str] = None,
        # Legacy keyword aliases kept for backwards-compatible callers
        project_id: Optional[str] = None,
        location: Optional[str] = None,
        dataset_id: Optional[str] = None,
    ):
        self.catalog = (
            catalog
            or project_id
            or os.environ.get("DATABRICKS_CATALOG")
            or "main"
        ).strip()
        self.schema_name = (
            schema_name
            or dataset_id
            or os.environ.get("DATABRICKS_BRONZE_SCHEMA")
            or "banking_bronze"
        ).strip()
        self.host = (
            host
            or os.environ.get("DATABRICKS_HOST")
            or ""
        ).strip().rstrip("/")
        if self.host and not self.host.startswith("http"):
            self.host = f"https://{self.host}"

    def _catalog_url(self, schema_name: str, table_name: Optional[str] = None) -> str:
        base = self.host or "https://adb-workspace.azuredatabricks.net"
        if table_name:
            return f"{base}/explore/data/{self.catalog}/{schema_name}/{table_name}"
        return f"{base}/explore/data/{self.catalog}/{schema_name}"

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
        """Tag Unity Catalog schema & tables with Data Product metadata."""
        schema = dataset_id or self.schema_name
        dp_id = _sanitize_id(product_id)
        try:
            from tools.databricks_auth import get_sql_connection

            with get_sql_connection(catalog=self.catalog, schema=schema) as conn, conn.cursor() as cur:
                safe_desc = description.replace("'", "''")
                cur.execute(f"COMMENT ON SCHEMA `{self.catalog}`.`{schema}` IS '{safe_desc}'")
                for tbl in table_names:
                    cur.execute(
                        f"ALTER TABLE `{self.catalog}`.`{schema}`.`{tbl}` "
                        f"SET TAGS ('data_product_id' = '{dp_id}', 'layer' = 'bronze')"
                    )
            return {
                "ok": True,
                "status": "registered",
                "data_product_id": dp_id,
                "display_name": display_name,
                "assets_registered": len(table_names),
                "knowledge_catalog_url": self._catalog_url(schema),
            }
        except Exception as exc:
            log.warning("Unity Catalog data product registration skipped/failed: %s", exc)
            return {
                "ok": False,
                "status": "skipped",
                "data_product_id": dp_id,
                "assets_registered": 0,
                "error": str(exc),
                "knowledge_catalog_url": self._catalog_url(schema),
            }

    def sync_business_glossary(
        self,
        glossary_id: str,
        display_name: str,
        description: str,
        terms: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        """Persist glossary metadata as Unity Catalog schema properties."""
        gl_id = _sanitize_id(glossary_id, "bronze-glossary")
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
                "glossary_url": self._catalog_url(self.schema_name),
            }
        except Exception as exc:
            log.warning("Unity Catalog glossary sync skipped/failed: %s", exc)
            return {
                "ok": False,
                "status": "skipped",
                "glossary_id": gl_id,
                "terms_synced": 0,
                "error": str(exc),
                "glossary_url": self._catalog_url(self.schema_name),
            }

    def enrich_table(
        self,
        schema_name: str,
        table_name: str,
        description: str,
        domain: str = "bronze_banking",
    ) -> Dict[str, Any]:
        """Enrich a Unity Catalog table with COMMENT and governance TAGS."""
        schema = schema_name or self.schema_name
        safe_desc = (description or f"Bronze raw table {table_name}").replace("'", "''")
        domain_tag = _sanitize_label(domain, "bronze_banking")
        try:
            from tools.databricks_auth import get_sql_connection

            with get_sql_connection(catalog=self.catalog, schema=schema) as conn, conn.cursor() as cur:
                cur.execute(f"COMMENT ON TABLE `{self.catalog}`.`{schema}`.`{table_name}` IS '{safe_desc}'")
                cur.execute(
                    f"ALTER TABLE `{self.catalog}`.`{schema}`.`{table_name}` "
                    f"SET TAGS ('layer' = 'bronze', 'domain' = '{domain_tag}')"
                )
            return {"ok": True, "table": f"{self.catalog}.{schema}.{table_name}"}
        except Exception as exc:
            log.warning("Unity Catalog table enrichment failed for %s.%s: %s", schema, table_name, exc)
            return {"ok": False, "error": str(exc)}

    # Backwards-compatible alias
    def enrich_bigquery_table(
        self,
        dataset_id: str,
        table_name: str,
        description: str,
        domain: str = "bronze_banking",
    ) -> Dict[str, Any]:
        return self.enrich_table(dataset_id, table_name, description, domain)
