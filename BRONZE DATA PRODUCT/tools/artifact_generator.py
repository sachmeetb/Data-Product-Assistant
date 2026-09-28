"""
artifact_generator.py — Multi-Artifact PDF, Excel, JSON, and DAG Generator for BFSI-Bronze-Agent.

Generates pipeline files progressively in-between agent runs:
  Stage 1 (Bank Profile):
    - brz_bank_profile_brief.pdf
  Stage 2 (Requirement Understanding):
    - brz_requirements_brief.pdf
    - brz_ingestion_specification.pdf
  Stage 3 (Source Scoping):
    - brz_sttm_mapping_v1.xlsx
    - brz_metadata_v1.xlsx
  Stage 4 (Bronze Product Engine & Spec Generator):
    - brz_banking_contract_v1.0.yaml
    - brz_banking_schema.sql
    - brz_sample_run_audit.xlsx
    - brz_dataplex_catalog_manifest.json
    - brz_pipeline_dag.py
"""

from __future__ import annotations

import io
import json
import logging
from typing import Any, Dict, List, Optional

from fpdf import FPDF
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

log = logging.getLogger(__name__)


# ── FPDF Helper Class for Header & Footer ──────────────────────────────────────

def _clean_pdf_text(text: str) -> str:
    """Sanitize string for FPDF core Helvetica font (latin-1)."""
    if not text:
        return ""
    s = str(text)
    s = s.replace("—", "-").replace("–", "-").replace("“", '"').replace("”", '"').replace("’", "'").replace("•", "*")
    return s.encode("latin-1", "replace").decode("latin-1")


class BFSIPDFReport(FPDF):
    def __init__(self, title_text: str):
        super().__init__()
        self.title_text = title_text

    def header(self):
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(115, 70, 0)  # Dark bronze/brown #734600
        self.cell(0, 8, _clean_pdf_text("DATA DOMAIN BRONZE AGENT - BFSI RAW LANDING ZONE"), new_x="LMARGIN", new_y="NEXT", align="L")
        self.set_draw_color(255, 163, 102)  # Bronze accent
        self.set_line_width(0.5)
        self.line(10, 18, 200, 18)
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, _clean_pdf_text(f"Page {self.page_no()}/{{nb}} | Confidential - Banking Bronze Layer Specification"), align="C")


# ── Stage 1: Bank Profile PDF ──────────────────────────────────────────────────

def generate_bank_profile_pdf(bank_profile: Dict[str, Any]) -> bytes:
    """Generate brz_bank_profile_brief.pdf for Stage 1."""
    pdf = BFSIPDFReport("Bank Profile Brief")
    pdf.alias_nb_pages()
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(115, 70, 0)
    pdf.cell(0, 10, _clean_pdf_text("Executive Bank Profile & Standards Brief"), new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(0, 6, _clean_pdf_text("Raw Data Ingestion & Governance Baseline"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    bank_name = bank_profile.get("bank_name", "Global BFSI Bank")
    bank_code = bank_profile.get("bank_code", "BFSI_US")
    region = ", ".join(bank_profile.get("regions", ["Global"]))
    btype = bank_profile.get("banking_type", "UNIVERSAL")
    core_sys = bank_profile.get("core_banking_system", "Temenos T24 / Flexcube")
    products = ", ".join(bank_profile.get("active_products", ["Deposits", "Lending", "Payments"]))
    frameworks = ", ".join(bank_profile.get("regulatory_frameworks", ["BIAN v12", "ISO 20022", "FIBO", "Basel III"]))

    pdf.set_fill_color(255, 230, 204)  # Light bronze
    pdf.set_text_color(115, 70, 0)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, _clean_pdf_text("  1. Institution Metadata"), new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)

    fields = [
        ("Bank Name", bank_name),
        ("Bank Code", bank_code),
        ("Primary Region(s)", region),
        ("Banking Type", btype),
        ("Core Banking System", core_sys),
        ("Active Products", products),
        ("Regulatory & Governance Standards", frameworks),
    ]

    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(40, 40, 40)
    for label, val in fields:
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.cell(60, 6, _clean_pdf_text(f"  * {label}:"), new_x="RIGHT", new_y="LAST")
        pdf.set_font("Helvetica", "", 9.5)
        pdf.multi_cell(0, 6, _clean_pdf_text(str(val)), new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)
    pdf.set_fill_color(255, 230, 204)
    pdf.set_text_color(115, 70, 0)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, _clean_pdf_text("  2. Bronze Layer Ingestion Strategy"), new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)

    strategy_text = (
        f"All Bronze layer data assets generated for {bank_name} focus on robust raw data ingestion. "
        "Source system records are stored with high fidelity. Technical envelopes capture ingestion metadata "
        "(ingest_ts, source_system, raw payload). Data Quality checks are focused on schema drift, file availability, "
        "volume bounds, and duplicate detection before downstream Silver processing."
    )
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(40, 40, 40)
    pdf.multi_cell(0, 5, _clean_pdf_text(strategy_text), new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())


# ── Stage 2: Requirements Brief PDF & Ingestion Specification PDF ──────────────────

def generate_requirements_brief_pdf(
    structured_req: Dict[str, Any],
    bank_profile: Dict[str, Any],
) -> bytes:
    """Generate brz_requirements_brief.pdf for Stage 2."""
    pdf = BFSIPDFReport("Business Requirements Brief (Bronze)")
    pdf.alias_nb_pages()
    pdf.add_page()

    use_case = structured_req.get("use_case_name") or "Source Data Ingestion"
    source_sys = structured_req.get("source_system") or "Core Banking"
    freshness = structured_req.get("latency_requirement") or structured_req.get("data_freshness") or "DAILY_BATCH"
    consumers = ", ".join(structured_req.get("primary_consumers", ["SILVER_PIPELINE"]))

    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(115, 70, 0)
    pdf.cell(0, 10, _clean_pdf_text(f"Ingestion Requirement: {use_case}"), new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "I", 10)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 6, _clean_pdf_text(f"Source System: {source_sys} | SLA: {freshness}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_fill_color(255, 230, 204)
    pdf.set_text_color(115, 70, 0)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, _clean_pdf_text("  1. Executive Summary & Source Scope"), new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)

    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(40, 40, 40)
    pdf.multi_cell(0, 5, _clean_pdf_text(f"This document specifies the raw data ingestion requirement for '{use_case}' from {source_sys}. It outlines the Bronze layer target to safely land raw payloads with appropriate metadata tracking."), new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)
    pdf.set_fill_color(255, 230, 204)
    pdf.set_text_color(115, 70, 0)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, _clean_pdf_text("  2. Source Requirements"), new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)

    req_table = [
        ("Source System", source_sys),
        ("Target Layer", "Bronze"),
        ("Required Data Freshness", freshness),
        ("Primary Consumers", consumers),
    ]

    for label, val in req_table:
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.cell(55, 6, _clean_pdf_text(f"  * {label}:"), new_x="RIGHT", new_y="LAST")
        pdf.set_font("Helvetica", "", 9.5)
        pdf.multi_cell(0, 6, _clean_pdf_text(str(val)), new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())


def generate_ingestion_specification_pdf(
    domain_scope: Dict[str, Any],
    structured_req: Dict[str, Any],
) -> bytes:
    """Generate brz_ingestion_specification.pdf for Stage 2."""
    pdf = BFSIPDFReport("Bronze Ingestion Specification")
    pdf.alias_nb_pages()
    pdf.add_page()

    source_sys = structured_req.get("source_system") or "Core Banking"
    use_case = structured_req.get("use_case_name") or "Bronze Schema"

    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(115, 70, 0)
    pdf.cell(0, 10, _clean_pdf_text("Ingestion Specification Report"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(0, 6, _clean_pdf_text(f"Assessment for '{use_case}' from source: {source_sys}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_fill_color(255, 230, 204)
    pdf.set_text_color(115, 70, 0)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, _clean_pdf_text("  1. Ingestion Pattern Details"), new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)

    blocks = domain_scope.get("common_blocks", ["ingestion-metadata", "raw-payload"])
    blocks_str = ", ".join([b if isinstance(b, str) else b.get("name", "") for b in blocks])

    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(40, 40, 40)
    pdf.multi_cell(0, 5, _clean_pdf_text(f"Bronze ingestion will append raw records using defined metadata envelopes."), new_x="LMARGIN", new_y="NEXT")

    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 9.5)
    pdf.cell(60, 6, _clean_pdf_text("  * Applied Blocks:"), new_x="RIGHT", new_y="LAST")
    pdf.set_font("Helvetica", "", 9.5)
    pdf.multi_cell(0, 6, _clean_pdf_text(blocks_str), new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())


# Backward-compatible alias for pipeline/server
generate_data_availability_pdf = generate_ingestion_specification_pdf


# ── Stage 3: Excel STTM & Metadata Generation ─────────────────────────────

def _apply_excel_header_styles(ws, title_text: str, col_count: int):
    """Apply bronze header styles to an openpyxl worksheet."""
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=col_count)
    cell = ws.cell(row=1, column=1)
    cell.value = title_text
    cell.font = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
    cell.fill = PatternFill(start_color="8B4513", end_color="8B4513", fill_type="solid") # SaddleBrown
    cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[1].height = 35
    ws.row_dimensions[2].height = 10


def _style_excel_table_headers(ws, header_row: int, headers: List[str]):
    header_fill = PatternFill(start_color="A0522D", end_color="A0522D", fill_type="solid") # Sienna
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    thin_border = Border(
        left=Side(style="thin", color="D2B48C"),
        right=Side(style="thin", color="D2B48C"),
        top=Side(style="thin", color="D2B48C"),
        bottom=Side(style="medium", color="8B4513"),
    )

    ws.row_dimensions[header_row].height = 24
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=header_row, column=col_idx, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = thin_border


def _autofit_excel_columns(ws, max_cols: int):
    thin_border = Border(
        left=Side(style="thin", color="E2E8F0"),
        right=Side(style="thin", color="E2E8F0"),
        top=Side(style="thin", color="E2E8F0"),
        bottom=Side(style="thin", color="E2E8F0"),
    )
    zebra_fill = PatternFill(start_color="FDF5E6", end_color="FDF5E6", fill_type="solid") # OldLace

    for row in range(4, ws.max_row + 1):
        ws.row_dimensions[row].height = 20
        is_even = (row % 2 == 0)
        for col in range(1, max_cols + 1):
            cell = ws.cell(row=row, column=col)
            cell.border = thin_border
            if is_even and not cell.fill.start_color.rgb:
                cell.fill = zebra_fill

    for col in ws.columns:
        col_letter = get_column_letter(col[0].column)
        max_len = 0
        for cell in col:
            val_str = str(cell.value or "")
            if len(val_str) > max_len:
                max_len = len(val_str)
        ws.column_dimensions[col_letter].width = max(max_len + 4, 12)


def generate_sttm_excel(
    mappings: List[Dict[str, Any]],
    structured_req: Dict[str, Any],
) -> bytes:
    """Generate brz_sttm_mapping_v1.xlsx for Stage 3."""
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Source Ingestion Mapping"

    use_case = structured_req.get("use_case_name") or "Bronze Ingestion"
    headers = ["SOURCE SYSTEM", "SOURCE FILE/TABLE", "TARGET BRONZE TABLE", "INGESTION TYPE", "PAYLOAD FORMAT"]
    _apply_excel_header_styles(ws1, f"  BFSI Bronze Layer Ingestion Mapping — {use_case}", len(headers))
    _style_excel_table_headers(ws1, 3, headers)

    row_idx = 4
    for m in mappings:
        src = m.get("source_system", "CORE")
        src_tbl = m.get("source_table", "raw_data")
        tgt_tbl = m.get("target_table", "banking_bronze.brz_entity")
        ing_type = m.get("ingestion_type", "FULL_LOAD")
        fmt = m.get("format", "JSON")

        ws1.append([src, src_tbl, tgt_tbl, ing_type, fmt])
        row_idx += 1

    _autofit_excel_columns(ws1, len(headers))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def generate_metadata_excel(
    product_plan: Dict[str, Any],
    domain_scope: Dict[str, Any],
    structured_req: Dict[str, Any],
) -> bytes:
    """Generate brz_metadata_v1.xlsx for Stage 3."""
    wb = openpyxl.Workbook()

    ws1 = wb.active
    ws1.title = "Bronze Table Metadata"
    t_headers = ["TABLE NAME", "SOURCE SYSTEM", "PARTITION STRATEGY", "RETENTION POLICY"]
    _apply_excel_header_styles(ws1, "  Bronze Layer Entity & Table Governance Metadata", len(t_headers))
    _style_excel_table_headers(ws1, 3, t_headers)

    tables = product_plan.get("tables", [])
    if not tables:
        tables = [
            {"table_name": "brz_core_customer", "source_system": "Core Banking"},
            {"table_name": "brz_core_account", "source_system": "Core Banking"},
        ]

    for t in tables:
        tname = t.get("table_name", "brz_entity")
        src = t.get("source_system", "Core")
        part = "PARTITION BY DATE(ingest_ts)"
        ws1.append([tname, src, part, "7 Years (Archive)"])

    _autofit_excel_columns(ws1, len(t_headers))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Stage 4: Sample Audit Excel, GCP Manifest & Airflow DAG ─────────────────

def generate_sample_audit_excel(
    product_plan: Dict[str, Any],
    mappings: List[Dict[str, Any]],
) -> bytes:
    """Generate brz_sample_run_audit.xlsx for Stage 4."""
    wb = openpyxl.Workbook()

    ws1 = wb.active
    ws1.title = "Bronze Output Audit"
    headers1 = ["BRONZE ID", "TABLE NAME", "INGEST TS", "SOURCE SYSTEM", "RAW PAYLOAD SAMPLE"]
    _apply_excel_header_styles(ws1, "  Sample Run Audit — Bronze Records Output", len(headers1))
    _style_excel_table_headers(ws1, 3, headers1)

    outputs = [
        ("brz_9901", "banking_bronze.brz_core_customer", "2026-08-31 06:05:00", "CORE", '{"cust_id": "CUST_99012"}'),
        ("brz_9902", "banking_bronze.brz_core_account", "2026-08-31 06:05:00", "CORE", '{"acc_id": "ACC_1001"}'),
    ]
    for row in outputs:
        ws1.append(list(row))
    _autofit_excel_columns(ws1, len(headers1))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def generate_dataplex_manifest_json(
    product_plan: Dict[str, Any],
    bank_profile: Dict[str, Any],
) -> str:
    """Generate brz_dataplex_catalog_manifest.json for Stage 4."""
    bank_name = bank_profile.get("bank_name", "Global BFSI Bank")
    tables = product_plan.get("tables", [])

    entities = []
    for t in tables:
        tname = t.get("table_name", "brz_entity")
        entities.append({
            "entity_id": f"dataplex:banking_bronze:{tname}",
            "display_name": tname,
            "asset_type": "BIGQUERY_TABLE",
            "schema_location": f"banking_bronze.{tname}",
            "governance_aspects": {
                "owner": f"{bank_name} Data Architecture Team",
                "retention": "7_YEARS",
            },
        })

    manifest = {
        "$schema": "https://cloud.google.com/dataplex/docs/reference/rest/v1/projects.locations.lakes.zones.entities",
        "dataplex_lake": "bfsi-banking-bronze-lake",
        "zone": "banking-bronze-zone",
        "project_id": bank_profile.get("project_id", "internal-data-mig"),
        "registered_at": "2026-08-31T06:00:00Z",
        "entities": entities,
    }
    return json.dumps(manifest, indent=2)


def generate_airflow_dag_code(
    product_plan: Dict[str, Any],
    bank_profile: Dict[str, Any],
) -> str:
    """Generate brz_pipeline_dag.py Airflow script for Stage 4."""
    bank_code = bank_profile.get("bank_code", "bfsi")

    dag_code = f'''"""
brz_banking_pipeline_dag.py — GCP Cloud Composer Airflow DAG.

Orchestrates daily Bronze Layer ingestion jobs for {bank_code.upper()} in BigQuery.
"""

from datetime import datetime, timedelta
from airflow import DAG
from airflow.providers.google.cloud.operators.bigquery import BigQueryInsertJobOperator

default_args = {{
    'owner': 'data-engineering',
    'depends_on_past': False,
    'email_on_failure': True,
    'retries': 2,
    'retry_delay': timedelta(minutes=5),
}}

with DAG(
    dag_id='{bank_code.lower()}_bronze_layer_daily_pipeline',
    default_args=default_args,
    description='Daily Bronze Layer Ingestion Pipeline',
    schedule_interval='0 4 * * *',
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=['BFSI', 'Bronze', 'BigQuery'],
) as dag:

    ingest_raw_data = BigQueryInsertJobOperator(
        task_id='ingest_brz_data',
        configuration={{
            'query': {{
                'query': "CALL `banking_bronze.sp_ingest_all`();",
                'useLegacySql': False,
            }}
        }},
    )

    ingest_raw_data
'''
    return dag_code
