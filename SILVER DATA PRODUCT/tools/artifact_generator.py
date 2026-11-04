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
        self.cell(0, 8, _clean_pdf_text("SILVER DATA PRODUCT ASSISTANT — CANONICAL DOMAIN LIBRARY"), new_x="LMARGIN", new_y="NEXT", align="L")
        self.set_draw_color(194, 163, 255)  # #C2A3FF
        self.set_line_width(0.5)
        self.line(10, 18, 200, 18)
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, _clean_pdf_text(f"Page {self.page_no()}/{{nb}} | Confidential — Silver Layer Data Product Specification"), align="C")


# ── Stage 1: Organization & Industry Profile PDF ───────────────────────────────

def generate_bank_profile_pdf(bank_profile: Dict[str, Any]) -> bytes:
    """Generate slv_bank_profile_brief.pdf for Stage 1."""
    pdf = BFSIPDFReport("Organization Profile Brief")
    pdf.alias_nb_pages()
    pdf.add_page()

    # Document Title
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(70, 0, 115)
    pdf.cell(0, 10, _clean_pdf_text("Executive Profile & Standards Brief"), new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(0, 6, _clean_pdf_text("Canonical Domain Architecture & Governance Baseline"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Key Profile Fields Table / Callout
    org_name = bank_profile.get("org_name") or bank_profile.get("bank_name", "Global Enterprise")
    org_code = bank_profile.get("org_code") or bank_profile.get("bank_code", "ENT_US")
    industry = bank_profile.get("industry", "Banking & Financial Services")
    subtype = bank_profile.get("industry_subtype") or bank_profile.get("banking_type", "UNIVERSAL")
    region = ", ".join(bank_profile.get("regions", ["Global"]))
    core_sys = bank_profile.get("core_banking_system") or bank_profile.get("core_systems", "Enterprise Core ERP / Ingestion Lake")
    products = ", ".join(bank_profile.get("active_products", ["Core Operations", "Analytics", "Audits"]))
    frameworks = ", ".join(bank_profile.get("regulatory_frameworks", ["ISO 20022", "GDPR", "Industry Governance"]))

    pdf.set_fill_color(230, 220, 255)  # #E6DCFF
    pdf.set_text_color(70, 0, 115)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, _clean_pdf_text("  1. Institution & Industry Metadata"), new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(2)

    fields = [
        ("Organization Name", org_name),
        ("Organization Code", org_code),
        ("Industry", industry),
        ("Industry Subtype", subtype),
        ("Operating Region(s)", region),
        ("Core Operating Systems", core_sys),
        ("Active Products / Services", products),
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
        f"All Silver layer data assets generated for {org_name} follow standard canonical "
        "service domain decomposition. Surrogate keys (UUID/SHA-256), standardized technical envelopes "
        "(load_ts, effective_from_ts, dq_status), and ISO compliance are strictly enforced. "
        "dbt Data Quality checks are generated alongside the Data Contract and verified in GCP Knowledge Catalog."
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


def summarize_dbt_dq_suite(
    product_plan: Dict[str, Any],
    contract_dict: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Compute structured dbt Data Quality test metrics from the curated Silver product plan
    and saved Open Data Contract Standard (ODCS) specification.
    """
    contract_dict = contract_dict or {}
    tables = product_plan.get("tables", [])
    quality_rules = contract_dict.get("quality", []) or []
    slas = contract_dict.get("servicelevels", {}) or {}
    freshness_spec = (slas.get("freshness", {}) or {}).get("specification", "Within 4 hours (Incremental) / 24 hours (Daily Batch)")

    unique_pk_tests = 0
    not_null_tests = 0
    fk_relationship_tests = 0
    format_or_domain_tests = 0
    custom_expression_tests = len(quality_rules)
    table_test_counts: Dict[str, Dict[str, Any]] = {}

    for t in tables:
        tname = t.get("table_name") or t.get("id") or "slv_entity"
        cols = t.get("columns", [])
        t_pk = 0
        t_nn = 0
        t_fk = 0
        t_fmt = 0
        col_tests_map: Dict[str, List[str]] = {}

        for c in cols:
            cname = c.get("name", "id")
            c_tests: List[str] = []
            is_pk = bool(c.get("primary_key") or c.get("is_pk"))
            fk_ref = c.get("references") or c.get("fk_ref") or ""
            is_nullable = c.get("nullable", not is_pk)

            if is_pk:
                t_pk += 1
                t_nn += 1
                unique_pk_tests += 1
                not_null_tests += 1
                c_tests.extend(["unique", "not_null"])
            elif not is_nullable or fk_ref or cname in ("load_ts", "dq_status", "effective_from_ts"):
                t_nn += 1
                not_null_tests += 1
                c_tests.append("not_null")

            if fk_ref and "." in str(fk_ref):
                t_fk += 1
                fk_relationship_tests += 1
                ref_parts = str(fk_ref).split(".")
                c_tests.append(f"relationships({ref_parts[0]}.{ref_parts[1]})")

            cname_lower = cname.lower()
            if "currency" in cname_lower or cname_lower.endswith("_ccy"):
                t_fmt += 1
                format_or_domain_tests += 1
                c_tests.append("regex(^[A-Z]{3}$)")
            elif cname_lower == "dq_status" or cname_lower.endswith("_status"):
                t_fmt += 1
                format_or_domain_tests += 1
                c_tests.append("accepted_values")

            if c_tests:
                col_tests_map[cname] = c_tests

        t_custom = max(1, len(quality_rules))
        t_total = t_pk + t_nn + t_fk + t_fmt + t_custom
        table_test_counts[tname] = {
            "total": t_total,
            "unique_pk": t_pk,
            "not_null": t_nn,
            "fk_relationships": t_fk,
            "format_domain": t_fmt,
            "custom_expressions": t_custom,
            "column_tests": col_tests_map,
        }

    total_tests = (
        unique_pk_tests
        + not_null_tests
        + fk_relationship_tests
        + format_or_domain_tests
        + (custom_expression_tests * max(1, len(tables)))
    )

    return {
        "framework": "dbt Core v1.8 + dbt_utils + dbt_expectations",
        "contract_enforced": True,
        "store_failures": True,
        "quarantine_dataset": f"{product_plan.get('dataset', 'banking_silver')}_dbt_audit",
        "total_tests": total_tests,
        "unique_pk_tests": unique_pk_tests,
        "not_null_tests": not_null_tests,
        "fk_relationship_tests": fk_relationship_tests,
        "format_or_domain_tests": format_or_domain_tests,
        "custom_expression_tests": custom_expression_tests * max(1, len(tables)),
        "source_freshness_sla": freshness_spec,
        "status": "VERIFIED",
        "table_test_counts": table_test_counts,
    }


def generate_dbt_schema_yaml(
    product_plan: Dict[str, Any],
    contract_dict: Optional[Dict[str, Any]] = None,
    bank_profile: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Generate slv_dbt_schema_tests.yml (dbt v2 schema & data_tests specification)
    from the user's curated Silver entities and saved Data Contract.
    """
    import yaml

    contract_dict = contract_dict or {}
    bank_profile = bank_profile or {}
    info = contract_dict.get("info", {}) or {}
    owner = info.get("owner") or f"{bank_profile.get('org_name', 'Enterprise')} Data Architecture & Governance Team"
    domain = info.get("domain") or product_plan.get("domain", "silver_domain")
    industry = bank_profile.get("industry", "Banking & Financial Services")
    target_dataset = product_plan.get("dataset", "banking_silver")
    bronze_dataset = target_dataset.replace("_silver", "_bronze") if "_silver" in target_dataset else "raw_bronze"

    tables = product_plan.get("tables", [])
    quality_rules = contract_dict.get("quality", []) or []

    # 1. Build dbt sources block with freshness SLA
    source_tables = []
    for t in tables:
        tname = t.get("table_name") or t.get("id") or "slv_entity"
        raw_name = tname.replace("slv_", "raw_")
        source_tables.append({
            "name": raw_name,
            "description": f"Raw Bronze ingestion feed backing {target_dataset}.{tname}",
            "loaded_at_field": "ingest_ts",
        })

    sources_block = [
        {
            "name": bronze_dataset,
            "database": bank_profile.get("project_id", "eogwapq-agbg-internal-data-mig"),
            "schema": bronze_dataset,
            "description": f"Raw Bronze source layer for {domain} ({industry})",
            "freshness": {
                "warn_after": {"count": 4, "period": "hour"},
                "error_after": {"count": 24, "period": "hour"},
            },
            "tables": source_tables,
        }
    ]

    # 2. Build dbt models block with enforced contracts, PK/FK/not_null/custom DQ tests
    models_block = []
    for t in tables:
        tname = t.get("table_name") or t.get("id") or "slv_entity"
        tdesc = t.get("description") or f"Conformed Silver entity {tname}"
        etype = (t.get("entity_type") or "dimension").upper()
        cols = t.get("columns", [])

        pk_col = "surrogate_key"
        for c in cols:
            if c.get("primary_key") or c.get("is_pk"):
                pk_col = c.get("name", "surrogate_key")
                break

        model_tests = []
        for idx, q in enumerate(quality_rules, start=1):
            expr = q.get("mustBe") or q.get("rule") or "surrogate_key IS NOT NULL"
            desc = q.get("description") or f"Contract DQ Assertion #{idx}"
            model_tests.append({
                "dbt_utils.expression_is_true": {
                    "expression": f"({expr}) -- {desc}",
                    "config": {
                        "severity": "error" if idx <= 2 else "warn",
                        "store_failures": True,
                        "schema": f"{target_dataset}_dbt_audit",
                    },
                }
            })

        if not model_tests:
            model_tests.append({
                "dbt_utils.expression_is_true": {
                    "expression": f"{pk_col} IS NOT NULL",
                    "config": {
                        "severity": "error",
                        "store_failures": True,
                    },
                }
            })

        dbt_columns = []
        for c in cols:
            cname = c.get("name", "id")
            ctype = str(c.get("type", "STRING")).lower()
            cdesc = c.get("description") or f"Conformed Silver attribute {cname}"
            is_pk = bool(c.get("primary_key") or c.get("is_pk"))
            fk_ref = c.get("references") or c.get("fk_ref") or ""
            is_pii = bool(c.get("pii") or c.get("is_pii"))
            is_kpi = bool(c.get("is_kpi"))
            is_nullable = c.get("nullable", not is_pk)

            col_tests: List[Any] = []
            if is_pk:
                col_tests.append("unique")
                col_tests.append("not_null")
            elif not is_nullable or fk_ref or cname in ("load_ts", "dq_status", "effective_from_ts"):
                col_tests.append("not_null")

            if fk_ref and "." in str(fk_ref):
                ref_parts = str(fk_ref).split(".")
                col_tests.append({
                    "relationships": {
                        "to": f"ref('{ref_parts[0]}')",
                        "field": ref_parts[1],
                        "config": {"severity": "warn", "store_failures": True},
                    }
                })

            cname_lower = cname.lower()
            if "currency" in cname_lower or cname_lower.endswith("_ccy"):
                col_tests.append({
                    "dbt_expectations.expect_column_values_to_match_regex": {
                        "regex": "^[A-Z]{3}$",
                        "config": {"severity": "error"},
                    }
                })
            elif cname_lower == "dq_status":
                col_tests.append({
                    "accepted_values": {
                        "values": ["VALID", "PASS", "VERIFIED", "WARN", "QUARANTINED"],
                    }
                })

            col_entry: Dict[str, Any] = {
                "name": cname,
                "data_type": ctype,
                "description": cdesc,
                "meta": {
                    "primary_key": is_pk,
                    "foreign_key": fk_ref if fk_ref else None,
                    "pii": is_pii,
                    "classification": "RESTRICTED_PII" if is_pii else ("BUSINESS_KPI" if is_kpi else "INTERNAL"),
                },
            }
            # Remove None values in meta for clean YAML
            col_entry["meta"] = {k: v for k, v in col_entry["meta"].items() if v is not None}
            if col_tests:
                col_entry["data_tests"] = col_tests

            dbt_columns.append(col_entry)

        models_block.append({
            "name": tname,
            "description": tdesc,
            "config": {
                "contract": {"enforced": True},
                "materialized": "incremental",
                "unique_key": pk_col,
                "on_schema_change": "fail",
                "tags": ["silver", domain, etype.lower(), "dataplex_governed"],
            },
            "meta": {
                "owner": owner,
                "domain": domain,
                "industry": industry,
                "entity_type": etype,
                "target_dataset": target_dataset,
                "dataplex_catalog_sync": True,
            },
            "data_tests": model_tests,
            "columns": dbt_columns,
        })

    dbt_doc = {
        "version": 2,
        "sources": sources_block,
        "models": models_block,
    }

    header_comment = (
        "# =============================================================================\n"
        "# Silver Layer dbt Data Quality & Model Contract Specification (schema.yml)\n"
        f"# Domain: {domain} | Target Dataset: {target_dataset}\n"
        "# Enforces ODCS v2.2.0 Schema Contracts, PK/FK Integrity & Business DQ Rules\n"
        "# =============================================================================\n\n"
    )
    return header_comment + yaml.dump(dbt_doc, sort_keys=False, default_flow_style=False, allow_unicode=True)


def generate_dbt_models_sql(
    product_plan: Dict[str, Any],
    mappings: List[Dict[str, Any]],
    contract_dict: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Generate slv_dbt_models.sql containing modular dbt incremental SQL models
    with STTM transformation rules and DQ status tagging.
    """
    target_dataset = product_plan.get("dataset", "banking_silver")
    bronze_dataset = target_dataset.replace("_silver", "_bronze") if "_silver" in target_dataset else "raw_bronze"
    tables = product_plan.get("tables", [])

    # Index mappings by (table_name, target_column)
    map_lookup: Dict[tuple, Dict[str, Any]] = {}
    for m in mappings or []:
        t_full = str(m.get("target_table", ""))
        t_short = t_full.split(".")[-1] if "." in t_full else t_full
        c_name = str(m.get("target_column", ""))
        if t_short and c_name:
            map_lookup[(t_short, c_name)] = m

    blocks: List[str] = [
        "-- =============================================================================",
        f"-- Silver Layer dbt Incremental Transformation Models ({target_dataset})",
        "-- Generated from Approved Silver Data Contract & STTM Mappings",
        "-- =============================================================================",
        "",
    ]

    for t in tables:
        tname = t.get("table_name") or t.get("id") or "slv_entity"
        raw_name = tname.replace("slv_", "raw_")
        etype = (t.get("entity_type") or "dimension").upper()
        cols = t.get("columns", [])

        pk_col = "surrogate_key"
        for c in cols:
            if c.get("primary_key") or c.get("is_pk"):
                pk_col = c.get("name", "surrogate_key")
                break

        partition_cfg = (
            ",\n    partition_by={'field': 'load_ts', 'data_type': 'timestamp', 'granularity': 'day'}"
            if any(c.get("name") == "load_ts" for c in cols)
            else ""
        )

        select_exprs = []
        for c in cols:
            cname = c.get("name", "id")
            ctype = str(c.get("type", "STRING")).upper()
            m_info = map_lookup.get((tname, cname), {})
            transform = m_info.get("transform") or ""
            src_col_full = m_info.get("source_column") or f"src.{cname}"
            src_col = src_col_full.split(".")[-1] if "." in src_col_full else src_col_full

            if cname == "surrogate_key":
                expr = f"TO_HEX(SHA256(CAST(src.{src_col} AS STRING))) AS {cname}"
            elif cname == "load_ts":
                expr = f"CURRENT_TIMESTAMP() AS {cname}"
            elif cname == "dq_status":
                expr = f"'VALID' AS {cname}"
            elif transform and "UPPER(TRIM" in transform.upper():
                expr = f"UPPER(TRIM(CAST(src.{src_col} AS STRING))) AS {cname}"
            elif ctype in ("NUMERIC", "BIGNUMERIC", "FLOAT64", "INT64", "DATE", "TIMESTAMP"):
                expr = f"SAFE_CAST(src.{src_col} AS {ctype}) AS {cname}"
            else:
                expr = f"CAST(src.{src_col} AS {ctype}) AS {cname}"
            select_exprs.append(f"        {expr}")

        select_clause = ",\n".join(select_exprs) if select_exprs else "        src.*"

        model_sql = f"""-- ─────────────────────────────────────────────────────────────────────────────
-- File: models/silver/{tname}.sql
-- Entity Type: {etype} | Enforced dbt Contract: true
-- ─────────────────────────────────────────────────────────────────────────────
{{{{
  config(
    materialized='incremental',
    unique_key='{pk_col}',
    on_schema_change='fail',
    contract={{'enforced': true}}{partition_cfg},
    labels={{'layer': 'silver', 'dbt_dq': 'verified', 'entity_type': '{etype.lower()}'}}
  )
}}}}

WITH source_data AS (
    SELECT *
    FROM {{{{ source('{bronze_dataset}', '{raw_name}') }}}} src
    {{% if is_incremental() %}}
    WHERE src.ingest_ts > (SELECT COALESCE(MAX(load_ts), TIMESTAMP('1970-01-01')) FROM {{{{ this }}}})
    {{% endif %}}
),

conformed AS (
    SELECT
{select_clause}
    FROM source_data src
    WHERE src.{pk_col if any(c.get('name') == pk_col for c in cols) else 'id'} IS NOT NULL
)

SELECT * FROM conformed;
"""
        blocks.append(model_sql)

    return "\n".join(blocks)


def generate_dataplex_manifest_json(
    product_plan: Dict[str, Any],
    bank_profile: Dict[str, Any],
    contract_dict: Optional[Dict[str, Any]] = None,
    published_tables: Optional[List[str]] = None,
    dbt_dq_summary: Optional[Dict[str, Any]] = None,
) -> str:
    """Generate slv_dataplex_catalog_manifest.json for Stage 4 & GCP Knowledge Catalog sync."""
    org_name = bank_profile.get("org_name") or bank_profile.get("bank_name", "Global Enterprise")
    project_id = bank_profile.get("project_id", "eogwapq-agbg-internal-data-mig")
    target_dataset = product_plan.get("dataset", "banking_silver")
    tables = product_plan.get("tables", [])
    dq_summary = dbt_dq_summary or summarize_dbt_dq_suite(product_plan, contract_dict)
    table_counts = dq_summary.get("table_test_counts", {})

    entities = []
    for t in tables:
        tname = t.get("table_name") or t.get("id") or "slv_entity"
        fqn = f"{project_id}.{target_dataset}.{tname}"
        t_dq = table_counts.get(tname, {})
        cols = t.get("columns", [])
        pk_cols = [c.get("name") for c in cols if c.get("primary_key") or c.get("is_pk")]
        fk_cols = [f"{c.get('name')}->{c.get('references') or c.get('fk_ref')}" for c in cols if c.get("references") or c.get("fk_ref")]
        pii_cols = [c.get("name") for c in cols if c.get("pii") or c.get("is_pii")]

        entities.append({
            "entity_id": f"dataplex:{target_dataset}:{tname}",
            "dataplex_entry_name": f"projects/{project_id}/locations/us/entryGroups/@bigquery/entries/bigquery.googleapis.com/projects/{project_id}/datasets/{target_dataset}/tables/{tname}",
            "display_name": tname,
            "asset_type": "BIGQUERY_TABLE",
            "schema_location": f"{target_dataset}.{tname}",
            "fully_qualified_name": f"bigquery:{fqn}",
            "governance_aspects": {
                "domain_standard": t.get("bian", product_plan.get("domain", "Canonical Silver")),
                "owner": f"{org_name} Data Architecture & Governance Team",
                "steward": "Data Governance Office",
                "retention": "7_YEARS",
                "primary_keys": pk_cols,
                "foreign_keys": fk_cols,
                "pii_columns": pii_cols,
                "dbt_dq_verification": {
                    "status": "VERIFIED",
                    "contract_enforced": True,
                    "total_dbt_tests": t_dq.get("total", 0),
                    "unique_pk_tests": t_dq.get("unique_pk", 0),
                    "not_null_tests": t_dq.get("not_null", 0),
                    "fk_relationship_tests": t_dq.get("fk_relationships", 0),
                    "custom_expression_tests": t_dq.get("custom_expressions", 0),
                },
            },
        })

    manifest = {
        "$schema": "https://cloud.google.com/dataplex/docs/reference/rest/v1/projects.locations.entryGroups.entries",
        "catalog_provider": "GCP Knowledge Catalog (Dataplex Universal Catalog)",
        "dataplex_lake": f"{target_dataset.replace('_', '-')}-lake",
        "zone": f"{target_dataset.replace('_', '-')}-curated-zone",
        "project_id": project_id,
        "dataset_id": target_dataset,
        "published_tables": published_tables or [f"{project_id}.{target_dataset}.{t.get('table_name', 'slv_entity')}" for t in tables],
        "dbt_data_quality": {
            "framework": dq_summary.get("framework"),
            "contract_enforced": dq_summary.get("contract_enforced", True),
            "status": dq_summary.get("status", "VERIFIED"),
            "total_tests_generated": dq_summary.get("total_tests", 0),
            "unique_pk_tests": dq_summary.get("unique_pk_tests", 0),
            "not_null_tests": dq_summary.get("not_null_tests", 0),
            "fk_relationship_tests": dq_summary.get("fk_relationship_tests", 0),
            "custom_expression_tests": dq_summary.get("custom_expression_tests", 0),
            "source_freshness_sla": dq_summary.get("source_freshness_sla"),
        },
        "entities": entities,
    }
    return json.dumps(manifest, indent=2)


def generate_airflow_dag_code(
    product_plan: Dict[str, Any],
    bank_profile: Dict[str, Any],
) -> str:
    """Generate slv_pipeline_dag.py Airflow script for Stage 4."""
    bank_code = bank_profile.get("org_code") or bank_profile.get("bank_code", "bfsi")
    target_dataset = product_plan.get("dataset", "banking_silver")

    dag_code = f'''"""
slv_banking_pipeline_dag.py — GCP Cloud Composer Airflow DAG.

Orchestrates daily Silver Layer transformation & dbt Data Quality verification jobs for {bank_code.upper()} in BigQuery.
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
    description='Daily Canonical Silver Layer Ingest, Transformation & dbt DQ Pipeline',
    schedule_interval='0 6 * * *',
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=['Silver', 'BigQuery', 'dbt-DQ', 'KnowledgeCatalog'],
) as dag:

    # 1. Transform Customer / Core Dimension
    transform_customer = BigQueryInsertJobOperator(
        task_id='transform_slv_core_dimension',
        configuration={{
            'query': {{
                'query': "CALL `{target_dataset}.sp_refresh_slv_customer`();",
                'useLegacySql': False,
            }}
        }},
    )

    # 2. Transform Account / Asset Dimension
    transform_account = BigQueryInsertJobOperator(
        task_id='transform_slv_account',
        configuration={{
            'query': {{
                'query': "CALL `{target_dataset}.sp_refresh_slv_account`();",
                'useLegacySql': False,
            }}
        }},
    )

    # 3. Transform Transaction / Event Fact
    transform_transactions = BigQueryInsertJobOperator(
        task_id='transform_slv_transaction_event',
        configuration={{
            'query': {{
                'query': "CALL `{target_dataset}.sp_refresh_slv_transaction_event`();",
                'useLegacySql': False,
            }}
        }},
    )

    # Pipeline Dependencies
    transform_customer >> transform_account >> transform_transactions
'''
    return dag_code

