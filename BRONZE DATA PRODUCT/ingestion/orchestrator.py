"""
orchestrator.py — Automated Multi-Table Bronze Orchestrator.

Models each source system as 1-to-many datasets (Batch + Real-Time Kafka Streaming)
and provisions/lands all tables into the Bronze layer without requiring manual file uploads.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .batch_connector import BatchIngestionConnector
from .kafka_connector import KafkaStreamingConnector

MULTI_TABLE_SOURCE_SPECS: Dict[str, List[Dict[str, Any]]] = {
    "AFS": [
        {
            "source_system": "AFS",
            "system_label": "AFS Commercial Lending",
            "table_name": "brz_afs_borrower",
            "file_name": "brz_afs_borrower.csv",
            "feed_name": "BORROWER-MASTER (Commercial Obligors)",
            "ingestion_mode": "BATCH_SCHEDULED",
            "description": "AFS commercial loan borrowers, corporate obligor demographics, and risk classifications.",
        },
        {
            "source_system": "AFS",
            "system_label": "AFS Commercial Lending",
            "table_name": "brz_afs_facility",
            "file_name": "brz_afs_facility.csv",
            "feed_name": "FACILITY-LINE (Credit Facilities & Sub-Limits)",
            "ingestion_mode": "BATCH_SCHEDULED",
            "description": "AFS syndicated and bilateral commercial credit facilities and revolving lines (1:N per Borrower).",
        },
        {
            "source_system": "AFS",
            "system_label": "AFS Commercial Lending",
            "table_name": "brz_afs_collateral",
            "file_name": "brz_afs_collateral.csv",
            "feed_name": "COLLATERAL-PLEDGE (CRE & Equipment Liens)",
            "ingestion_mode": "BATCH_SCHEDULED",
            "description": "AFS pledged commercial real estate, equipment, and UCC-1 lien valuations.",
        },
        {
            "source_system": "AFS",
            "system_label": "AFS Commercial Lending",
            "table_name": "brz_afs_guarantor",
            "file_name": "brz_afs_guarantor.csv",
            "feed_name": "GUARANTOR-LINK (Personal & Corporate Guarantors)",
            "ingestion_mode": "BATCH_SCHEDULED",
            "description": "AFS individual principals and parent corporations guaranteeing commercial borrower obligations.",
        },
    ],
    "HOGAN": [
        {
            "source_system": "HOGAN",
            "system_label": "Hogan Core Banking (Mainframe)",
            "table_name": "brz_hogan_ci_customer",
            "file_name": "brz_hogan_ci_customer.csv",
            "feed_name": "CI-REC (CIS Customer Master Copybook)",
            "ingestion_mode": "BATCH_COPYBOOK",
            "description": "Hogan CIS master customer demographic and tax classification records.",
        },
        {
            "source_system": "HOGAN",
            "system_label": "Hogan Core Banking (Mainframe)",
            "table_name": "brz_hogan_dd_account",
            "file_name": "brz_hogan_dd_account.csv",
            "feed_name": "DD-REC (Demand Deposit Account Copybook)",
            "ingestion_mode": "BATCH_COPYBOOK",
            "description": "Hogan DDA master record containing checking, savings, and MMDA deposit accounts (1:N per Customer).",
        },
        {
            "source_system": "HOGAN",
            "system_label": "Hogan Core Banking (Mainframe)",
            "table_name": "brz_hogan_ci_address",
            "file_name": "brz_hogan_ci_address.csv",
            "feed_name": "CI-ADDR (CIS Multi-Address Copybook)",
            "ingestion_mode": "BATCH_COPYBOOK",
            "description": "Hogan CIS multi-address feed capturing residential, mailing, and statement addresses.",
        },
    ],
    "TSYS": [
        {
            "source_system": "TSYS",
            "system_label": "TSYS / FDR Card Servicing",
            "table_name": "brz_tsys_cardholder",
            "file_name": "brz_tsys_cardholder.csv",
            "feed_name": "CARDHOLDER-BASE (Cardholder Demographics)",
            "ingestion_mode": "DAILY_BATCH",
            "description": "TSYS revolving credit cardholder demographic master.",
        },
        {
            "source_system": "TSYS",
            "system_label": "TSYS / FDR Card Servicing",
            "table_name": "brz_tsys_card_account",
            "file_name": "brz_tsys_card_account.csv",
            "feed_name": "CARD-ACCT (Revolving Card Accounts & Balances)",
            "ingestion_mode": "DAILY_BATCH",
            "description": "TSYS card account cycle balances, APR tiers, and credit utilization metrics (1:N per Cardholder).",
        },
        {
            "source_system": "TSYS",
            "system_label": "TSYS / FDR Card Servicing",
            "table_name": "brz_tsys_auth_events",
            "file_name": "brz_tsys_auth_events.jsonl",
            "feed_name": "KAFKA-AUTH-STREAM (ISO 8583 Card Authorizations)",
            "ingestion_mode": "KAFKA_STREAM",
            "kafka_topic": "payments.tsys.card.auth.v1",
            "description": "Real-time Kafka event stream capturing card authorizations and verified mobile contact updates.",
        },
    ],
    "ACLS": [
        {
            "source_system": "ACLS",
            "system_label": "ACLS Consumer Loans",
            "table_name": "brz_acls_applicant",
            "file_name": "brz_acls_applicant.csv",
            "feed_name": "APPLICANT-MASTER (Consumer Loan Applicants)",
            "ingestion_mode": "DAILY_BATCH",
            "description": "ACLS retail installment loan applicants and contact demographics.",
        },
        {
            "source_system": "ACLS",
            "system_label": "ACLS Consumer Loans",
            "table_name": "brz_acls_loan_application",
            "file_name": "brz_acls_loan_application.csv",
            "feed_name": "LOAN-APP (Retail Installment & Auto Loan Contracts)",
            "ingestion_mode": "DAILY_BATCH",
            "description": "ACLS originated installment, auto, mortgage, and HELOC loan contracts (1:N per Applicant).",
        },
    ],
}

WATER_MULTI_TABLE_SPECS: Dict[str, List[Dict[str, Any]]] = {
    "SAP_ERP": [
        {
            "source_system": "SAP_ERP",
            "system_label": "SAP S/4HANA ERP (Commercial Accounts)",
            "table_name": "brz_sap_water_customer",
            "file_name": "brz_sap_water_customer.csv",
            "feed_name": "KNA1 (Industrial Customer Master)",
            "ingestion_mode": "BATCH_CDC",
            "description": "SAP ERP industrial water & hygiene customer master accounts.",
        },
        {
            "source_system": "SAP_ERP",
            "system_label": "SAP S/4HANA ERP (Commercial Accounts)",
            "table_name": "brz_sap_service_contract",
            "file_name": "brz_sap_service_contract.csv",
            "feed_name": "VBAK (Water Treatment Service Contracts)",
            "ingestion_mode": "BATCH_CDC",
            "description": "SAP commercial chemical & water treatment service contracts (1:N per Customer).",
        },
    ],
    "TRASAR_IOT": [
        {
            "source_system": "TRASAR_IOT",
            "system_label": "3D TRASAR IoT Water Telemetry",
            "table_name": "brz_trasar_cooling_asset",
            "file_name": "brz_trasar_cooling_asset.csv",
            "feed_name": "IOT-ASSET (Cooling Tower & RO Skids)",
            "ingestion_mode": "KAFKA_STREAM",
            "description": "Connected 3D TRASAR cooling towers, boilers, and reverse osmosis skids.",
        },
    ],
    "SFDC_FSL": [
        {
            "source_system": "SFDC_FSL",
            "system_label": "Salesforce Field Service Lightning",
            "table_name": "brz_sfdc_plant_site",
            "file_name": "brz_sfdc_plant_site.csv",
            "feed_name": "FSL-SITE (Plant Hygiene & Steward Contacts)",
            "ingestion_mode": "BATCH_SCHEDULED",
            "description": "Salesforce field service plant site locations and EHS steward contacts.",
        },
    ],
}


class AutomatedBronzeOrchestrator:
    """Metadata-driven orchestrator for 1-to-many automated Bronze creation (Batch + Kafka)."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or Path(__file__).resolve().parent.parent
        self.batch_connector = BatchIngestionConnector(self.base_dir / "samples" / "banking_multi_table")
        self.kafka_connector = KafkaStreamingConnector(self.base_dir / "samples" / "banking_multi_table")
        self.state_file = self.base_dir / "output" / "bronze_catalog_state.json"

    def run_automated_ingestion(
        self,
        source_systems: Optional[List[str]] = None,
        industry_domain: str = "banking",
        dataset_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        project_id = (
            os.environ.get("BQ_PROJECT")
            or os.environ.get("GCP_PROJECT_ID")
            or "eogwapq-agbg-internal-data-mig"
        )
        schema_name = dataset_id or os.environ.get("BQ_BRONZE_DATASET") or (
            "water_bronze" if industry_domain == "water" else "banking_bronze"
        )
        catalog = project_id
        specs_map = WATER_MULTI_TABLE_SPECS if industry_domain == "water" else MULTI_TABLE_SOURCE_SPECS
        custom_dir = (
            self.base_dir / "samples" / "water_multi_table"
            if industry_domain == "water"
            else self.base_dir / "samples" / "banking_multi_table"
        )

        selected_systems = (
            [s.upper() for s in source_systems if s.upper() in specs_map]
            if source_systems
            else list(specs_map.keys())
        )

        landed_tables: List[Dict[str, Any]] = []
        systems_summary: List[Dict[str, Any]] = []
        total_rows = 0
        batch_count = 0
        stream_count = 0

        for sys_code in selected_systems:
            entity_specs = specs_map.get(sys_code, [])
            sys_tables: List[Dict[str, Any]] = []
            sys_rows = 0
            sys_label = entity_specs[0]["system_label"] if entity_specs else sys_code

            for spec in entity_specs:
                mode = spec.get("ingestion_mode", "BATCH_SCHEDULED")
                if mode == "KAFKA_STREAM" and spec["file_name"].endswith(".jsonl"):
                    tbl_res = self.kafka_connector.ingest_stream_table(
                        source_system=sys_code,
                        table_name=spec["table_name"],
                        topic=spec.get("kafka_topic", "payments.tsys.card.auth.v1"),
                        feed_name=spec["feed_name"],
                        description=spec["description"],
                        jsonl_file=spec["file_name"],
                        catalog=catalog,
                        schema_name=schema_name,
                    )
                    stream_count += 1
                else:
                    tbl_res = self.batch_connector.ingest_table(
                        source_system=sys_code,
                        table_name=spec["table_name"],
                        file_name=spec["file_name"],
                        feed_name=spec["feed_name"],
                        description=spec["description"],
                        ingestion_mode=mode,
                        catalog=catalog,
                        schema_name=schema_name,
                        custom_dir=custom_dir,
                    )
                    if mode == "KAFKA_STREAM":
                        stream_count += 1
                    else:
                        batch_count += 1

                landed_tables.append(tbl_res)
                sys_tables.append(tbl_res)
                sys_rows += tbl_res["row_count"]
                total_rows += tbl_res["row_count"]

            systems_summary.append({
                "source_system": sys_code,
                "system_label": sys_label,
                "table_count": len(sys_tables),
                "total_rows": sys_rows,
                "ingestion_modes": sorted({t["ingestion_mode"] for t in sys_tables}),
                "tables": [t["table_name"] for t in sys_tables],
            })

        # Register in GCP Knowledge Catalog (Dataplex)
        dp_res: Dict[str, Any] = {}
        gl_res: Dict[str, Any] = {}
        try:
            if os.environ.get("BQ_OFFLINE_MODE") == "1":
                raise RuntimeError("BQ_OFFLINE_MODE")
            from tools.knowledge_catalog import KnowledgeCatalogClient

            kc = KnowledgeCatalogClient(project_id=project_id, dataset_id=schema_name)
            dp_res = kc.register_data_product(
                schema_name,
                f"bronze-{industry_domain}-multi-table",
                f"Bronze Multi-Table {industry_domain.title()} Sources (1-to-Many Batch + Kafka)",
                f"Automated 1-to-many Bronze landing across {len(selected_systems)} source systems ({len(landed_tables)} tables).",
                [t["table_name"] for t in landed_tables],
            )
            gl_res = kc.sync_business_glossary(
                f"bronze-{industry_domain}-multi-table-glossary",
                f"Bronze Multi-Table {industry_domain.title()} Glossary",
                "1-to-Many Raw Source Schemas & Ingestion Envelope Definitions",
                [
                    {
                        "name": t["table_name"],
                        "description": f"[{t['source_system']} · {t['ingestion_mode']}] {t['description']}",
                    }
                    for t in landed_tables
                ],
            )
        except Exception:
            pass

        result = {
            "status": "landed",
            "architecture_model": "1_TO_MANY_PER_SOURCE_SYSTEM",
            "industry_domain": industry_domain,
            "project_id": project_id,
            "dataset_id": schema_name,
            "catalog": project_id,
            "schema_name": schema_name,
            "source_system_count": len(systems_summary),
            "total_tables": len(landed_tables),
            "batch_tables_count": batch_count,
            "streaming_tables_count": stream_count,
            "total_rows": total_rows,
            "systems_summary": systems_summary,
            "tables": landed_tables,
            "knowledge_catalog_data_product": dp_res,
            "knowledge_catalog_glossary": gl_res,
            "ingested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)

        return result
