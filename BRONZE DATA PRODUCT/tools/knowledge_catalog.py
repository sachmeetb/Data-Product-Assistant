"""
knowledge_catalog.py — GCP Knowledge Catalog (Dataplex Universal Catalog) & BigQuery
Governance Synchronization for the Bronze Data Product Assistant.

Responsibilities:
  1. Register Bronze Data Products & Data Assets in Dataplex Knowledge Catalog
     (POST /v1/projects/{project}/locations/{loc}/dataProducts + /dataAssets).
  2. Create / synchronize a Dataplex Business Glossary & Terms
     (POST /v1/projects/{project}/locations/global/glossaries + /terms).
  3. Enrich live BigQuery Bronze tables (table description, governance labels,
     column descriptions) so @bigquery entries stay synchronized.
  4. Generate clickable GCP Console deep links for BigQuery Studio, Dataplex
     Data Products, and Business Glossaries.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

import google.auth
from google.auth.transport.requests import Request as AuthRequest
import requests

log = logging.getLogger(__name__)

DATAPLEX_API = "https://dataplex.googleapis.com/v1"


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


def _valid_owner_email(owner: Any, project_id: str) -> str:
    candidate = str(owner or "").strip()
    if "@" in candidate and "." in candidate.split("@")[-1] and " " not in candidate:
        return candidate
    return os.environ.get(
        "RUNTIME_SA_EMAIL",
        f"dp-assistant-sa@{project_id}.iam.gserviceaccount.com",
    )


class KnowledgeCatalogClient:
    """Client for GCP Knowledge Catalog (Dataplex Universal Catalog) & BigQuery enrichment."""

    def __init__(
        self,
        project_id: Optional[str] = None,
        location: Optional[str] = None,
        dataset_id: Optional[str] = None,
    ):
        self.project_id = (
            project_id
            or os.environ.get("BQ_PROJECT")
            or os.environ.get("GCP_PROJECT_ID")
            or "eogwapq-agbg-internal-data-mig"
        ).strip()
        self.location = (
            location
            or os.environ.get("DATAPLEX_LOCATION")
            or os.environ.get("GCP_LOCATION")
            or "us-central1"
        ).strip().lower()
        self.dataset_id = (
            dataset_id
            or os.environ.get("BQ_BRONZE_DATASET")
            or "banking_bronze"
        ).strip()
        self._credentials = None

    def _get_headers(self) -> Dict[str, str]:
        if self._credentials is None:
            self._credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
        if not self._credentials.valid:
            self._credentials.refresh(AuthRequest())
        return {
            "Authorization": f"Bearer {self._credentials.token}",
            "Content-Type": "application/json",
        }

    def _resolve_dataset_location(self, dataset_id: str, fallback: Optional[str] = None) -> str:
        """Resolve the actual BigQuery dataset location (e.g. 'us' or 'us-central1') for Dataplex."""
        try:
            from tools.gcp_auth import get_bigquery_client
            client = get_bigquery_client(project_id=self.project_id)
            ds = client.get_dataset(f"{self.project_id}.{dataset_id}")
            if ds.location:
                return ds.location.strip().lower()
        except Exception:
            pass
        loc = (fallback or os.environ.get("BQ_LOCATION") or "us").strip().lower()
        return loc if loc in ("us-central1", "us", "eu", "asia-south1") else "us"

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
        """Create/reuse a Dataplex Data Product and register BigQuery tables as Data Assets."""
        loc = self._resolve_dataset_location(dataset_id, fallback=location)
        kc_id = _sanitize_id(product_id, default=f"bronze-{dataset_id}")
        email = _valid_owner_email(owner_email, self.project_id)

        kc_url = (
            f"https://console.cloud.google.com/dataplex/govern/data-products"
            f"?project={self.project_id}"
        )

        try:
            headers = self._get_headers()
            create_url = (
                f"{DATAPLEX_API}/projects/{self.project_id}/locations/{loc}"
                f"/dataProducts?data_product_id={kc_id}"
            )
            body = {
                "display_name": (display_name or f"Bronze Data Product ({dataset_id})")[:255],
                "description": (description or f"Bronze Lakehouse Data Product for {dataset_id}")[:1024],
                "owner_emails": [email],
            }
            resp = requests.post(create_url, json=body, headers=headers, timeout=25)
            if resp.status_code not in (200, 201, 409):
                log.warning("Dataplex Data Product create returned %s: %s", resp.status_code, resp.text[:300])
                return {
                    "ok": False,
                    "status": "FAILED",
                    "http_status": resp.status_code,
                    "error": resp.text[:300],
                    "data_product_id": kc_id,
                    "knowledge_catalog_url": kc_url,
                }

            registered_assets: List[str] = []
            asset_errors: List[str] = []
            for raw_tname in table_names[:50]:
                tbl = raw_tname.strip("`").split(".")[-1]
                if not tbl:
                    continue
                asset_id = _sanitize_id(tbl, default="table")
                asset_url = (
                    f"{DATAPLEX_API}/projects/{self.project_id}/locations/{loc}"
                    f"/dataProducts/{kc_id}/dataAssets?data_asset_id={asset_id}"
                )
                resource = (
                    f"//bigquery.googleapis.com/projects/{self.project_id}"
                    f"/datasets/{dataset_id}/tables/{tbl}"
                )
                ar = requests.post(asset_url, json={"resource": resource}, headers=headers, timeout=20)
                if ar.status_code in (200, 201, 409):
                    registered_assets.append(asset_id)
                else:
                    asset_errors.append(f"{tbl}: {ar.status_code} {ar.text[:120]}")

            return {
                "ok": True,
                "status": "REGISTERED_IN_KNOWLEDGE_CATALOG",
                "data_product_id": kc_id,
                "location": loc,
                "assets_registered": len(registered_assets),
                "registered_assets": registered_assets,
                "asset_errors": asset_errors,
                "knowledge_catalog_url": kc_url,
            }
        except Exception as exc:
            log.warning("register_data_product error: %s", exc)
            return {
                "ok": False,
                "status": "ERROR",
                "error": str(exc),
                "data_product_id": kc_id,
                "knowledge_catalog_url": kc_url,
            }

    def sync_business_glossary(
        self,
        glossary_id: str,
        display_name: str,
        description: str,
        terms: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        """Create/reuse a Dataplex Business Glossary in global location and populate terms."""
        gid = _sanitize_id(glossary_id, default="bronze-banking-glossary")
        glossary_url = (
            f"https://console.cloud.google.com/dataplex/glossaries"
            f"?project={self.project_id}"
        )
        try:
            headers = self._get_headers()
            create_url = (
                f"{DATAPLEX_API}/projects/{self.project_id}/locations/global"
                f"/glossaries?glossary_id={gid}"
            )
            resp = requests.post(
                create_url,
                json={
                    "displayName": (display_name or "Bronze Data Product Glossary")[:255],
                    "description": (description or "Business & Technical Glossary")[:1024],
                },
                headers=headers,
                timeout=20,
            )
            if resp.status_code not in (200, 201, 409):
                return {
                    "ok": False,
                    "status": "FAILED",
                    "http_status": resp.status_code,
                    "error": resp.text[:250],
                    "glossary_id": gid,
                    "glossary_url": glossary_url,
                }

            terms_synced = 0
            for t in (terms or [])[:25]:
                t_name = t.get("name") or t.get("displayName") or ""
                if not t_name:
                    continue
                tid = _sanitize_id(t_name, default="term")
                term_url = (
                    f"{DATAPLEX_API}/projects/{self.project_id}/locations/global"
                    f"/glossaries/{gid}/terms?term_id={tid}"
                )
                tr = requests.post(
                    term_url,
                    json={
                        "displayName": t_name[:255],
                        "description": (t.get("description") or t_name)[:1024],
                        "parent": f"projects/{self.project_id}/locations/global/glossaries/{gid}",
                    },
                    headers=headers,
                    timeout=15,
                )
                if tr.status_code in (200, 201, 409):
                    terms_synced += 1

            return {
                "ok": True,
                "status": "GLOSSARY_SYNCED",
                "glossary_id": gid,
                "terms_synced": terms_synced,
                "glossary_url": glossary_url,
            }
        except Exception as exc:
            log.warning("sync_business_glossary error: %s", exc)
            return {
                "ok": False,
                "status": "ERROR",
                "error": str(exc),
                "glossary_id": gid,
                "glossary_url": glossary_url,
            }

    def enrich_bigquery_table(
        self,
        dataset_id: str,
        table_name: str,
        description: str,
        domain: str = "bronze",
    ) -> bool:
        """Enrich BigQuery table description and governance labels so @bigquery mirrors them."""
        try:
            from tools.gcp_auth import get_bigquery_client
            client = get_bigquery_client(project_id=self.project_id)
            tbl = table_name.strip("`").split(".")[-1]
            table_ref = f"{self.project_id}.{dataset_id}.{tbl}"
            table = client.get_table(table_ref)
            table.description = (description or f"Bronze Raw Table {tbl}")[:16000]
            labels = dict(table.labels or {})
            labels.update({
                "layer": "bronze",
                "domain": _sanitize_label(domain, "banking"),
                "governance": "dataplex_knowledge_catalog",
            })
            table.labels = labels
            client.update_table(table, ["description", "labels"])
            return True
        except Exception as exc:
            log.warning("Bronze BigQuery table enrichment warning for %s.%s: %s", dataset_id, table_name, exc)
            return False
