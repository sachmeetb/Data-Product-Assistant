"""
knowledge_catalog.py — Google Cloud Dataplex & Knowledge Catalog Connector.

Connects the Gold Data Product Assistant to Google Cloud Dataplex Universal Catalog
(Knowledge Catalog) and Data Catalog.

Features:
  1. Live connectivity verification to Dataplex Catalog Entry Groups (@bigquery, @dataplex).
  2. Live metadata search across indexed catalog entries.
  3. Automatic synchronization / verification of BigQuery Gold & Silver tables.
  4. Generation of standard Dataplex & Knowledge Catalog governance manifests.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional
import google.auth
from google.auth.transport.requests import AuthorizedSession

log = logging.getLogger(__name__)

_DEFAULT_PROJECT = "eogwapq-agbg-internal-data-mig"
_DEFAULT_LOCATION = "us-central1"


class KnowledgeCatalogClient:
    """Client for Google Cloud Dataplex & Knowledge Catalog."""

    def __init__(self, project_id: Optional[str] = None, location: Optional[str] = None):
        self.project_id = (
            project_id
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("BQ_PROJECT")
            or _DEFAULT_PROJECT
        ).strip()
        self.location = (
            location
            or os.environ.get("GOOGLE_CLOUD_LOCATION")
            or os.environ.get("BQ_LOCATION")
            or _DEFAULT_LOCATION
        ).strip()
        self._session: Optional[AuthorizedSession] = None

    def _get_session(self) -> AuthorizedSession:
        if self._session is None:
            credentials, _ = google.auth.default()
            self._session = AuthorizedSession(credentials)
        return self._session

    def get_status(self) -> Dict[str, Any]:
        """
        Verify live connection to Google Cloud Dataplex & Knowledge Catalog.
        Checks the built-in @bigquery and @dataplex entry groups.
        """
        try:
            session = self._get_session()
            url = f"https://dataplex.googleapis.com/v1/projects/{self.project_id}/locations/{self.location}/entryGroups"
            resp = session.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                entry_groups = [eg.get("name", "").split("/")[-1] for eg in data.get("entryGroups", [])]
                return {
                    "connected": True,
                    "catalog_system": "Google Cloud Dataplex & Knowledge Catalog",
                    "project_id": self.project_id,
                    "location": self.location,
                    "entry_groups": entry_groups,
                    "bigquery_synced": "@bigquery" in entry_groups,
                    "dataplex_synced": "@dataplex" in entry_groups,
                    "status_message": f"Active and connected to Dataplex Catalog ({', '.join(entry_groups)})",
                }
            return {
                "connected": False,
                "catalog_system": "Google Cloud Dataplex & Knowledge Catalog",
                "project_id": self.project_id,
                "location": self.location,
                "status_message": f"Dataplex API returned status {resp.status_code}: {resp.text[:200]}",
            }
        except Exception as exc:
            log.warning("Knowledge Catalog status check failed: %s", exc)
            return {
                "connected": False,
                "catalog_system": "Google Cloud Dataplex & Knowledge Catalog",
                "project_id": self.project_id,
                "location": self.location,
                "status_message": f"Connection error: {exc}",
            }

    def search_entries(self, query: str = "", page_size: int = 15) -> List[Dict[str, Any]]:
        """
        Search live entries across Dataplex Knowledge Catalog.
        """
        try:
            session = self._get_session()
            url = f"https://dataplex.googleapis.com/v1/projects/{self.project_id}/locations/global:searchEntries"
            payload = {"query": query or "*", "pageSize": page_size}
            resp = session.post(url, json=payload, timeout=12)
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                formatted = []
                for r in results:
                    entry = r.get("dataplexEntry", {})
                    name = entry.get("name", "")
                    entry_type = entry.get("entryType", "").split("/")[-1]
                    display_name = name.split("/")[-1]
                    formatted.append({
                        "name": name,
                        "display_name": display_name,
                        "entry_type": entry_type,
                        "fully_qualified_name": entry.get("fullyQualifiedName", name),
                        "create_time": entry.get("createTime"),
                        "update_time": entry.get("updateTime"),
                    })
                return formatted
            log.warning("Dataplex search returned %s: %s", resp.status_code, resp.text[:200])
            return []
        except Exception as exc:
            log.warning("Knowledge Catalog search failed: %s", exc)
            return []

    def verify_table_entry(self, table_name: str) -> Dict[str, Any]:
        """
        Verify if a BigQuery table entry is indexed in Dataplex Knowledge Catalog.
        """
        clean_name = table_name.strip("`")
        parts = clean_name.split(".")
        if len(parts) == 3:
            dataset, tbl = parts[1], parts[2]
        elif len(parts) == 2:
            dataset, tbl = parts[0], parts[1]
        else:
            dataset, tbl = "gold", parts[0]

        entry_resource = (
            f"bigquery.googleapis.com/projects/{self.project_id}/datasets/{dataset}/tables/{tbl}"
        )
        url = (
            f"https://dataplex.googleapis.com/v1/projects/{self.project_id}/"
            f"locations/{self.location}/entryGroups/@bigquery/entries/{entry_resource}"
        )
        try:
            session = self._get_session()
            resp = session.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "registered": True,
                    "entry_name": data.get("name"),
                    "entry_type": data.get("entryType", "").split("/")[-1],
                    "update_time": data.get("updateTime"),
                }
            return {
                "registered": False,
                "status_code": resp.status_code,
                "message": "Pending automatic Dataplex Catalog synchronization",
            }
        except Exception as exc:
            return {"registered": False, "error": str(exc)}

    def generate_dataplex_manifest(
        self,
        domain_name: str,
        entities: List[Dict[str, Any]],
        published_tables: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Generate a comprehensive Dataplex & Knowledge Catalog Governance Manifest JSON.
        """
        published_tables = published_tables or []
        catalog_entities = []

        for entity in entities:
            ename = entity.get("name", "entity")
            role = entity.get("entity_type", "DIMENSION").upper()
            attrs = entity.get("attributes", [])
            pii_cols = [
                a.get("name") for a in attrs
                if a.get("role") == "PII" or "pii" in str(a.get("classification", "")).lower()
            ]
            kpi_cols = [
                a.get("name") for a in attrs
                if a.get("role") == "KPI"
            ]

            catalog_entities.append({
                "entity_id": f"dataplex:gold_{domain_name}:{ename}",
                "display_name": ename,
                "asset_type": "BIGQUERY_TABLE",
                "medallion_layer": "GOLD",
                "role": role,
                "schema_location": f"`{self.project_id}`.gold.{ename}",
                "governance_aspects": {
                    "owner": f"Enterprise Data Governance Team - {domain_name.capitalize()} Domain",
                    "domain": domain_name,
                    "data_classification": {
                        "confidentiality": "RESTRICTED" if pii_cols else "INTERNAL",
                        "pii_fields": pii_cols,
                        "kpi_metrics": kpi_cols,
                        "regulatory_frameworks": ["BCBS 239", "GDPR", "ODCS v3.0.1"],
                    },
                    "retention_policy": {
                        "retention_period": "7_YEARS",
                        "archive_tier": "COLDLINE",
                    },
                    "dataplex_entry_group": f"projects/{self.project_id}/locations/{self.location}/entryGroups/@bigquery",
                },
                "attributes": [
                    {
                        "name": a.get("name"),
                        "sql_type": a.get("sql_type", "STRING"),
                        "role": a.get("role", "ATTRIBUTE"),
                        "is_nullable": a.get("is_nullable", True),
                        "business_definition": a.get("business_definition", ""),
                    }
                    for a in attrs
                ],
            })

        return {
            "$schema": "https://cloud.google.com/dataplex/docs/reference/rest/v1/projects.locations.lakes.zones.entities",
            "knowledge_catalog_system": "Google Cloud Dataplex & Knowledge Catalog",
            "project_id": self.project_id,
            "location": self.location,
            "domain": domain_name,
            "connected_status": "ACTIVE_CONNECTED",
            "entry_groups": [
                f"projects/{self.project_id}/locations/{self.location}/entryGroups/@bigquery",
                f"projects/{self.project_id}/locations/{self.location}/entryGroups/@dataplex",
            ],
            "published_tables": published_tables,
            "entities": catalog_entities,
        }


# Singleton instance
knowledge_catalog_client = KnowledgeCatalogClient()
