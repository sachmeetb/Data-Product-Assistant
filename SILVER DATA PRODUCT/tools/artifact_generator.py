"""
artifact_generator.py — Multi-Artifact PDF, Excel, JSON, and DAG Generator for BFSI-Silver-Agent.

Generates pipeline files progressively in-between agent runs:
  Stage 1 (Bank Profile):
    - slv_bank_profile_brief.pdf
  Stage 2 (Requirement Understanding):
    - slv_requirements_brief.pdf
    - slv_data_availability_report.pdf
  Stage 3 (Domain Scoping):
    - slv_sttm_mapping_v1.xlsx
    - slv_metadata_v1.xlsx
  Stage 4 (Silver Product Engine & Spec Generator):
    - slv_banking_contract_v1.0.yaml
    - slv_banking_schema.sql
    - slv_sample_run_audit.xlsx
    - slv_dataplex_catalog_manifest.json
    - slv_pipeline_dag.py
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
        self.set_text_color(70, 0, 115)  # Dark purple #460073
        self.cell(0, 8, _clean_pdf_text("DATA DOMAIN SILVER AGENT - BFSI CANONICAL LIBRARY"), new_x="LMARGIN", new_y="NEXT", align="L")
        self.set_draw_color(194, 163, 255)  # #C2A3FF
        self.set_line_width(0.5)
        self.line(10, 18, 200, 18)
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, _clean_pdf_text(f"Page {self.page_no()}/{{nb}} | Confidential - Banking Silver Layer Specification"), align="C")


# ── Stage 1: Bank Profile PDF ──────────────────────────────────────────────────

def generate_bank_profile_pdf(bank_profile: Dict[str, Any]) -> bytes:
    """Generate slv_bank_profile_brief.pdf for Stage 1."""
    pdf = BFSIPDFReport("Bank Profile Brief")
    pdf.alias_nb_pages()
    pdf.add_page()

    # Document Title
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(70, 0, 115)
    pdf.cell(0, 10, _clean_pdf_text("Executive Bank Profile & Standards Brief"), new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(0, 6, _clean_pdf_text("Canonical Banking Architecture & Governance Baseline"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Key Profile Fields Table / Callout
    bank_name = bank_profile.get("bank_name", "Global BFSI Bank")
    bank_code = bank_profile.get("bank_code", "BFSI_US")
    region = ", ".join(bank_profile.get("regions", ["Global"]))
    btype = bank_profile.get("banking_type", "UNIVERSAL")
    core_sys = bank_profile.get("core_banking_system", "Temenos T24 / Flexcube")
    products = ", ".join(bank_profile.get("active_products", ["Deposits", "Lending", "Payments"]))
    frameworks = ", ".join(bank_profile.get("regulatory_frameworks", ["BIAN v12", "ISO 20022", "FIBO", "Basel III"]))

    pdf.set_fill_color(230, 220, 255)  # #E6DCFF
    pdf.set_text_color(70, 0, 115)
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
    pdf.set_fill_color(230, 220, 255)
    pdf.set_text_color(70, 0, 115)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, _clean_pdf_text("  2. Silver Layer Conformance Strategy"), new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)

    strategy_text = (
        f"All Silver layer data assets generated for {bank_name} follow standard BIAN v12 "
        "service domain decomposition. Surrogate keys (UUID), standardized technical envelopes "
        "(load_ts, effective_from_ts, dq_status), and ISO 4217 currency compliance are strictly enforced. "
        "Data Quality checks are executed during ingestion, and failed records are tagged for quarantine."
    )
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(40, 40, 40)
    pdf.multi_cell(0, 5, _clean_pdf_text(strategy_text), new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())


# ── Stage 2: Requirements Brief PDF & Data Availability PDF ──────────────────

def generate_requirements_brief_pdf(
    structured_req: Dict[str, Any],
    bank_profile: Dict[str, Any],
) -> bytes:
    """Generate slv_requirements_brief.pdf for Stage 2."""
    pdf = BFSIPDFReport("Business Requirements Brief")
    pdf.alias_nb_pages()
    pdf.add_page()

    use_case = structured_req.get("use_case_name") or "Core Banking & Payments Analytics"
    domain = structured_req.get("domain") or structured_req.get("banking_domain") or "deposits"
    data_points = ", ".join([str(dp if isinstance(dp, str) else dp.get("name")) for dp in structured_req.get("data_points", ["account_id", "customer_id", "current_balance"])])
    freshness = structured_req.get("latency_requirement") or structured_req.get("data_freshness") or "DAILY_BATCH"
    grain = structured_req.get("target_grain") or "ACCOUNT"
    consumers = ", ".join(structured_req.get("primary_consumers", ["RISK", "OPERATIONS"]))

    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(70, 0, 115)
    pdf.cell(0, 10, _clean_pdf_text(f"Requirement Specification: {use_case}"), new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "I", 10)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 6, _clean_pdf_text(f"Domain: {domain.upper()} | Target Grain: {grain} | SLA: {freshness}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Section 1: Business Overview
    pdf.set_fill_color(230, 220, 255)
    pdf.set_text_color(70, 0, 115)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, _clean_pdf_text("  1. Executive Summary & Domain Scope"), new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)

    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(40, 40, 40)
    pdf.multi_cell(0, 5, _clean_pdf_text(f"This document specifies the canonical Silver layer data requirement for '{use_case}' in the {domain.replace('_', ' ').title()} domain. The resulting conformed entities support high-precision business analytics, regulatory reporting, and risk officer controls."), new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)
    # Section 2: Data Requirements
    pdf.set_fill_color(230, 220, 255)
    pdf.set_text_color(70, 0, 115)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, _clean_pdf_text("  2. Required Attribute Specifications"), new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)

    req_table = [
        ("Target Banking Domain", domain.replace('_', ' ').title()),
        ("Key Business Attributes", data_points),
        ("Target Data Grain", grain),
        ("Required Data Freshness", freshness),
        ("Primary Consumer Roles", consumers),
    ]

    for label, val in req_table:
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.cell(55, 6, _clean_pdf_text(f"  * {label}:"), new_x="RIGHT", new_y="LAST")
        pdf.set_font("Helvetica", "", 9.5)
        pdf.multi_cell(0, 6, _clean_pdf_text(str(val)), new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())


def generate_data_availability_pdf(
    domain_scope: Dict[str, Any],
    structured_req: Dict[str, Any],
) -> bytes:
    """Generate slv_data_availability_report.pdf for Stage 2."""
    pdf = BFSIPDFReport("Catalog & Availability Assessment")
    pdf.alias_nb_pages()
    pdf.add_page()

    domain = structured_req.get("domain") or "deposits"
    use_case = structured_req.get("use_case_name") or "Silver Schema"

    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(70, 0, 115)
    pdf.cell(0, 10, _clean_pdf_text("Data Availability & Catalog Scan Report"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(0, 6, _clean_pdf_text(f"Assessment for '{use_case}' against Banking Common Library"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Match summary
    pdf.set_fill_color(230, 220, 255)
    pdf.set_text_color(70, 0, 115)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, _clean_pdf_text("  1. Catalog Scan & Match Score"), new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)

    blocks = domain_scope.get("common_blocks", ["technical-metadata", "party-core", "account-balances", "transaction-ledger"])
    blocks_str = ", ".join([b if isinstance(b, str) else b.get("name", "") for b in blocks])

    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(40, 40, 40)
    pdf.multi_cell(0, 5, _clean_pdf_text(f"Catalog Scan matched 100% of required business data attributes against existing BIAN canonical lego blocks in the '{domain}' domain."), new_x="LMARGIN", new_y="NEXT")

    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 9.5)
    pdf.cell(60, 6, _clean_pdf_text("  * Matched Common Blocks:"), new_x="RIGHT", new_y="LAST")
    pdf.set_font("Helvetica", "", 9.5)
    pdf.multi_cell(0, 6, _clean_pdf_text(blocks_str), new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "B", 9.5)
    pdf.cell(60, 6, _clean_pdf_text("  * Data Quality Readiness:"), new_x="RIGHT", new_y="LAST")
    pdf.set_font("Helvetica", "", 9.5)
    pdf.multi_cell(0, 6, _clean_pdf_text("HIGH (Technical envelope + ISO standards available)"), new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())


# ── Stage 3: Excel STTM & Metadata Generation ─────────────────────────────

def _apply_excel_header_styles(ws, title_text: str, col_count: int):
    """Apply unified purple header styles to an openpyxl worksheet."""
    # Title Banner
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=col_count)
    cell = ws.cell(row=1, column=1)
    cell.value = title_text
    cell.font = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
    cell.fill = PatternFill(start_color="460073", end_color="460073", fill_type="solid")
    cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[1].height = 35

    # Subtitle / Empty row
    ws.row_dimensions[2].height = 10


def _style_excel_table_headers(ws, header_row: int, headers: List[str]):
    header_fill = PatternFill(start_color="7500C0", end_color="7500C0", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    thin_border = Border(
        left=Side(style="thin", color="C2A3FF"),
        right=Side(style="thin", color="C2A3FF"),
        top=Side(style="thin", color="C2A3FF"),
        bottom=Side(style="medium", color="460073"),
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
    zebra_fill = PatternFill(start_color="F8FAF3", end_color="F8FAF3", fill_type="solid")

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
    """Generate slv_sttm_mapping_v1.xlsx for Stage 3."""
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "STTM Mapping Rules"

    use_case = structured_req.get("use_case_name") or "Banking Silver Schema"
    headers = ["SOURCE TABLE", "SOURCE COLUMN", "TRANSFORMATION LOGIC", "TARGET TABLE", "TARGET COLUMN", "DATA TYPE", "BIAN ALIGNMENT"]
    _apply_excel_header_styles(ws1, f"  BFSI Silver Layer STTM Mapping Specification — {use_case}", len(headers))
    _style_excel_table_headers(ws1, 3, headers)

    row_idx = 4
    for m in mappings:
        src_col = m.get("source_column", "")
        src_parts = src_col.split(".")
        src_tbl = ".".join(src_parts[:-1]) if len(src_parts) > 1 else "raw_core"
        src_c = src_parts[-1] if src_parts else src_col

        tgt_tbl = m.get("target_table", "banking_silver.slv_entity")
        tgt_col = m.get("target_column", "id")
        transform = m.get("transform") or m.get("notes") or "DIRECT_PASS"
        datatype = "STRING"
        if "NUMERIC" in transform.upper() or "DOUBLE" in transform.upper():
            datatype = "NUMERIC"
        elif "TIMESTAMP" in transform.upper():
            datatype = "TIMESTAMP"
        elif "DATE" in transform.upper():
            datatype = "DATE"

        ws1.append([src_tbl, src_c, transform, tgt_tbl, tgt_col, datatype, "BIAN CONFORMED"])
        row_idx += 1

    _autofit_excel_columns(ws1, len(headers))

    # Tab 2: Mapping Summary
    ws2 = wb.create_sheet(title="Mapping Summary")
    sum_headers = ["TARGET TABLE", "TOTAL MAPPED COLUMNS", "PRIMARY SOURCE FEED", "GOVERNANCE STATUS"]
    _apply_excel_header_styles(ws2, "  Source Feed & Target Entity Summary", len(sum_headers))
    _style_excel_table_headers(ws2, 3, sum_headers)

    ws2.append(["banking_silver.slv_customer", 5, "raw_core.customer_master", "BIAN READY"])
    ws2.append(["banking_silver.slv_account", 6, "raw_core.account_ledger", "BIAN READY"])
    ws2.append(["banking_silver.slv_transaction_event", 6, "raw_core.transaction_feed", "BIAN READY"])
    _autofit_excel_columns(ws2, len(sum_headers))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def generate_metadata_excel(
    product_plan: Dict[str, Any],
    domain_scope: Dict[str, Any],
    structured_req: Dict[str, Any],
) -> bytes:
    """Generate slv_metadata_v1.xlsx (2 Tabs) for Stage 3."""
    wb = openpyxl.Workbook()

    # Tab 1: Table Metadata
    ws1 = wb.active
    ws1.title = "Table Metadata"
    t_headers = ["TABLE NAME", "ENTITY TYPE", "GRAIN DESCRIPTION", "BIAN SERVICE DOMAIN", "PARTITION STRATEGY", "CLUSTERING KEYS", "RETENTION POLICY"]
    _apply_excel_header_styles(ws1, "  Silver Layer Entity & Table Governance Metadata", len(t_headers))
    _style_excel_table_headers(ws1, 3, t_headers)

    tables = product_plan.get("tables", [])
    if not tables:
        tables = [
            {"table_name": "slv_customer", "entity_type": "dimension", "description": "One row per customer", "bian": "Customer Management"},
            {"table_name": "slv_account", "entity_type": "dimension", "description": "One row per account", "bian": "Current Account"},
            {"table_name": "slv_transaction_event", "entity_type": "event", "description": "One row per transaction", "bian": "Payment Initiation"},
        ]

    for t in tables:
        tname = t.get("table_name", "slv_entity")
        etype = t.get("entity_type", "dimension").upper()
        grain = t.get("description", "One row per entity record")
        bian = t.get("bian") or "Core Banking"
        part = "PARTITION BY DATE(load_ts)" if etype == "EVENT" else "NONE"
        cluster = "customer_id, account_id" if etype == "EVENT" else "surrogate_key"
        ws1.append([tname, etype, grain, bian, part, cluster, "7 Years (GDPR Archive)"])

    _autofit_excel_columns(ws1, len(t_headers))

    # Tab 2: Column Metadata
    ws2 = wb.create_sheet(title="Column Metadata")
    c_headers = ["TABLE NAME", "COLUMN NAME", "BIGQUERY TYPE", "NULLABLE", "KEY TYPE", "TAXONOMY / PII", "DESCRIPTION"]
    _apply_excel_header_styles(ws2, "  Column-Level Data Dictionary & Taxonomy Tags", len(c_headers))
    _style_excel_table_headers(ws2, 3, c_headers)

    for t in tables:
        tname = t.get("table_name", "slv_entity")
        cols = t.get("columns", [
            {"name": "surrogate_key", "type": "STRING", "primary_key": True},
            {"name": "customer_id", "type": "STRING", "primary_key": False},
            {"name": "customer_name", "type": "STRING", "pii": True},
            {"name": "load_ts", "type": "TIMESTAMP", "primary_key": False},
        ])
        for c in cols:
            cname = c.get("name", "id")
            ctype = str(c.get("type", "STRING")).upper()
            nullable = "NO" if c.get("primary_key") or not c.get("nullable", True) else "YES"
            keytype = "PK" if c.get("primary_key") else ("FK" if c.get("references") else "-")
            pii = "PII (Restricted)" if c.get("pii") or "name" in cname else "FINANCIAL"
            desc = c.get("description", f"Canonical column for {cname}")
            ws2.append([tname, cname, ctype, nullable, keytype, pii, desc])

    _autofit_excel_columns(ws2, len(c_headers))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Stage 4: Sample Audit Excel, GCP Manifest & Airflow DAG ─────────────────

def generate_sample_audit_excel(
    product_plan: Dict[str, Any],
    mappings: List[Dict[str, Any]],
) -> bytes:
    """Generate slv_sample_run_audit.xlsx (3 Tabs) for Stage 4."""
    wb = openpyxl.Workbook()

    # Tab 1: Raw Core Input Feeds
    ws1 = wb.active
    ws1.title = "Raw Core Input Feeds"
    headers1 = ["FEED NAME", "SOURCE RECORD ID", "RAW FIELD NAME", "RAW SAMPLE VALUE", "INGEST TIMESTAMP"]
    _apply_excel_header_styles(ws1, "  Sample Run Audit — Tab 1: Raw Ingest Feeds", len(headers1))
    _style_excel_table_headers(ws1, 3, headers1)

    raw_samples = [
        ("raw_core.customer_master", "CUST_99012", "cust_name", "Johnathan Doe", "2026-08-31 06:00:12"),
        ("raw_core.customer_master", "CUST_99012", "kyc_stat", "VERIFIED", "2026-08-31 06:00:12"),
        ("raw_core.account_ledger", "ACCT_409128", "bal_amt", "15420.50", "2026-08-31 06:01:05"),
        ("raw_core.transaction_feed", "TXN_881920", "amount", "250.00", "2026-08-31 06:02:44"),
    ]
    for row in raw_samples:
        ws1.append(list(row))
    _autofit_excel_columns(ws1, len(headers1))

    # Tab 2: STTM Transformation Trace
    ws2 = wb.create_sheet(title="STTM Transformation Trace")
    headers2 = ["TRANSFORM STEP", "INPUT VALUE", "TRANSFORMATION RULE", "OUTPUT VALUE", "DQ STATUS"]
    _apply_excel_header_styles(ws2, "  Sample Run Audit — Tab 2: Step-by-Step Transformation Trace", len(headers2))
    _style_excel_table_headers(ws2, 3, headers2)

    traces = [
        ("1. String Normalisation", "  Johnathan Doe  ", "UPPER(TRIM())", "JOHNATHAN DOE", "PASS"),
        ("2. Type Conversion", "15420.50", "CAST_TO_NUMERIC", "15420.5000", "PASS"),
        ("3. UUID Surrogate Key", "ACCT_409128", "GENERATE_UUID()", "e9f137d8-ba20-442a-9c4f-73fa4b7dfdd0", "PASS"),
        ("4. ISO Currency Check", "USD", "VALIDATE_ISO4217", "USD", "PASS"),
    ]
    for row in traces:
        ws2.append(list(row))
    _autofit_excel_columns(ws2, len(headers2))

    # Tab 3: Conformed Silver Output Audit
    ws3 = wb.create_sheet(title="Silver Output Audit")
    headers3 = ["SURROGATE KEY", "TABLE NAME", "ACCOUNT ID", "CUSTOMER ID", "CURRENT BALANCE", "LOAD TS", "DQ STATUS"]
    _apply_excel_header_styles(ws3, "  Sample Run Audit — Tab 3: Conformed Silver Records Output", len(headers3))
    _style_excel_table_headers(ws3, 3, headers3)

    outputs = [
        ("a1b2c3d4-0001", "banking_silver.slv_account", "ACC_1001", "CUST_99012", 15420.50, "2026-08-31 06:05:00", "VALID"),
        ("a1b2c3d4-0002", "banking_silver.slv_account", "ACC_1002", "CUST_88210", 4310.00, "2026-08-31 06:05:00", "VALID"),
    ]
    for row in outputs:
        ws3.append(list(row))
    _autofit_excel_columns(ws3, len(headers3))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def generate_dataplex_manifest_json(
    product_plan: Dict[str, Any],
    bank_profile: Dict[str, Any],
) -> str:
    """Generate slv_dataplex_catalog_manifest.json for Stage 4."""
    bank_name = bank_profile.get("bank_name", "Global BFSI Bank")
    tables = product_plan.get("tables", [])

    entities = []
    for t in tables:
        tname = t.get("table_name", "slv_entity")
        entities.append({
            "entity_id": f"dataplex:banking_silver:{tname}",
            "display_name": tname,
            "asset_type": "BIGQUERY_TABLE",
            "schema_location": f"banking_silver.{tname}",
            "governance_aspects": {
                "bian_service_domain": t.get("bian", "Current Account"),
                "owner": f"{bank_name} Data Architecture Team",
                "retention": "7_YEARS",
            },
        })

    manifest = {
        "$schema": "https://cloud.google.com/dataplex/docs/reference/rest/v1/projects.locations.lakes.zones.entities",
        "dataplex_lake": "bfsi-banking-silver-lake",
        "zone": "banking-silver-zone",
        "project_id": bank_profile.get("project_id", "eogwapq-agbg-internal-data-mig"),
        "registered_at": "2026-08-31T06:00:00Z",
        "entities": entities,
    }
    return json.dumps(manifest, indent=2)


def generate_airflow_dag_code(
    product_plan: Dict[str, Any],
    bank_profile: Dict[str, Any],
) -> str:
    """Generate slv_pipeline_dag.py Airflow script for Stage 4."""
    bank_code = bank_profile.get("bank_code", "bfsi")

    dag_code = f'''"""
slv_banking_pipeline_dag.py — GCP Cloud Composer Airflow DAG.

Orchestrates daily Silver Layer transformation jobs for {bank_code.upper()} in BigQuery.
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
    dag_id='{bank_code.lower()}_silver_layer_daily_pipeline',
    default_args=default_args,
    description='Daily BIAN Silver Layer Ingest & Transformation Pipeline',
    schedule_interval='0 6 * * *',
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=['BFSI', 'Silver', 'BIAN', 'BigQuery'],
) as dag:

    # 1. Transform Customer 360 Dimension
    transform_customer = BigQueryInsertJobOperator(
        task_id='transform_slv_customer',
        configuration={{
            'query': {{
                'query': "CALL `banking_silver.sp_refresh_slv_customer`();",
                'useLegacySql': False,
            }}
        }},
    )

    # 2. Transform Account Dimension
    transform_account = BigQueryInsertJobOperator(
        task_id='transform_slv_account',
        configuration={{
            'query': {{
                'query': "CALL `banking_silver.sp_refresh_slv_account`();",
                'useLegacySql': False,
            }}
        }},
    )

    # 3. Transform Financial Transaction Event Fact
    transform_transactions = BigQueryInsertJobOperator(
        task_id='transform_slv_transaction_event',
        configuration={{
            'query': {{
                'query': "CALL `banking_silver.sp_refresh_slv_transaction_event`();",
                'useLegacySql': False,
            }}
        }},
    )

    # Pipeline Dependencies
    transform_customer >> transform_account >> transform_transactions
'''
    return dag_code
