"""
server.py — BFSI Silver Agent FastAPI REST server.

Exposes the pipeline as a REST API with streaming-friendly design.
Supports multi-turn agent conversations via session IDs.

Endpoints:
  POST /v1/sessions              — create a new pipeline session
  POST /v1/sessions/{id}/profile — submit / update bank profile
  POST /v1/sessions/{id}/require — submit business requirement
  POST /v1/sessions/{id}/run     — run full pipeline end-to-end
  GET  /v1/sessions/{id}         — get session state
  GET  /v1/sessions/{id}/ddl     — get generated DDL
  POST /v1/sessions/{id}/publish — publish DDL to BigQuery
  GET  /health                   — health check
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from fastapi import FastAPI, HTTPException, UploadFile, File, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from pipeline import (
    run_pipeline,
    _run_domain_scoping,
    _run_silver_product_engine,
    _run_spec_generator_with_validation,
)
from session_store import SessionStore
from agents import (
    bank_profile_agent,
    requirement_understanding_agent,
    domain_scoping_agent,
    silver_product_engine,
    spec_generator_agent,
    validator_agent,
)
from tools.bigquery_tool import BigQueryPublisher
from tools.datacontract_generator import (
    generate_data_contract_dict,
    generate_data_contract_yaml,
    generate_sttm_csv,
)
from tools.artifact_generator import (
    generate_bank_profile_pdf,
    generate_requirements_brief_pdf,
    generate_data_availability_pdf,
    generate_sttm_excel,
    generate_metadata_excel,
    generate_sample_audit_excel,
    generate_dataplex_manifest_json,
    generate_airflow_dag_code,
)

log = logging.getLogger(__name__)
logging.basicConfig(
    level=os.environ.get("AGENT_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="BFSI Silver Agent API",
    description=(
        "Agentic banking Silver-layer schema generator powered by "
        "Google ADK + Vertex AI Gemini + BigQuery"
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

store = SessionStore()


# ── Request / Response models ─────────────────────────────────────────────────

class SessionCreate(BaseModel):
    user_id: str = Field(default="api-user", description="User identifier for tracking")
    description: Optional[str] = Field(None, description="Optional session description")


class BankProfileRequest(BaseModel):
    input_text: str = Field(..., description="Natural-language bank description OR JSON bank profile string")


class RequirementRequest(BaseModel):
    input_text: str = Field(..., description="Natural-language business requirement")
    context: Optional[dict] = Field(None, description="Optional context override")


class RunPipelineRequest(BaseModel):
    bank_input: str = Field(..., description="Bank description (text or JSON)")
    requirement_input: str = Field(..., description="Business requirement text")


class PublishRequest(BaseModel):
    mode: Optional[str] = Field("auto", description="live | dry_run | auto")


# ── Helper ────────────────────────────────────────────────────────────────────

def _get_session(session_id: str) -> dict:
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")
    return session


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "service": "BFSI-Silver-Agent",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "gcp_project": os.environ.get("GCP_PROJECT_ID", "(not set)"),
        "gemini_model": os.environ.get("GEMINI_MODEL", "(not set)"),
    }


@app.post("/v1/sessions", status_code=201)
async def create_session(body: SessionCreate):
    session_id = str(uuid.uuid4())
    session = {
        "session_id": session_id,
        "user_id": body.user_id,
        "description": body.description,
        "status": "created",
        "created_at": datetime.utcnow().isoformat() + "Z",
        "steps": {},
    }
    store.set(session_id, session)
    return {"session_id": session_id, "status": "created"}


@app.get("/v1/sessions/{session_id}")
async def get_session(session_id: str):
    return _get_session(session_id)


@app.post("/v1/sessions/{session_id}/profile")
async def submit_bank_profile(session_id: str, body: BankProfileRequest):
    session = _get_session(session_id)

    output = await bank_profile_agent.run(body.input_text, session_id=session_id)
    session["steps"]["bank_profile"] = output
    session["profile_complete"] = bank_profile_agent.is_complete(output)
    session["status"] = "profile_collected" if session["profile_complete"] else "profile_incomplete"
    store.set(session_id, session)

    return {
        "session_id": session_id,
        "profile_complete": session["profile_complete"],
        "missing_fields": bank_profile_agent.get_missing_fields(output) if not session["profile_complete"] else [],
        "bank_profile": output,
    }


@app.post("/v1/sessions/{session_id}/require")
async def submit_requirement(session_id: str, body: RequirementRequest):
    session = _get_session(session_id)

    bank_profile = session.get("steps", {}).get("bank_profile", {})
    context = body.context or {"bank_profile": bank_profile, "clarification_pass": 0}

    output = await requirement_understanding_agent.run(
        body.input_text, context=context, session_id=session_id
    )
    session["steps"]["requirement"] = output
    session["requirement_ready"] = requirement_understanding_agent.is_handoff_ready(output)
    session["status"] = "requirement_understood" if session["requirement_ready"] else "requirement_clarifying"
    store.set(session_id, session)

    return {
        "session_id": session_id,
        "handoff_ready": session["requirement_ready"],
        "missing_fields": requirement_understanding_agent.get_missing_fields(output),
        "output": output,
    }


@app.post("/v1/sessions/{session_id}/run")
async def run_session_pipeline(session_id: str, body: RunPipelineRequest):
    """Run the full pipeline end-to-end for the given session."""
    session = _get_session(session_id)

    session["status"] = "running"
    store.set(session_id, session)

    try:
        result = await run_pipeline(body.bank_input, body.requirement_input)
        session.update({
            "status": result.get("status", "completed"),
            "steps": result.get("steps", {}),
            "published_tables": result.get("published_tables", []),
            "ddl_script": result.get("ddl_script", ""),
            "specification": result.get("specification", {}),
            "error": result.get("error"),
            "completed_at": datetime.utcnow().isoformat() + "Z",
        })
    except Exception as exc:
        session["status"] = "failed"
        session["error"] = f"{type(exc).__name__}: {exc}"

    store.set(session_id, session)
    return {
        "session_id": session_id,
        "status": session["status"],
        "published_tables": session.get("published_tables", []),
        "error": session.get("error"),
    }


@app.get("/v1/sessions/{session_id}/ddl")
async def get_ddl(session_id: str):
    session = _get_session(session_id)
    ddl = session.get("ddl_script", "")
    if not ddl:
        raise HTTPException(
            status_code=404,
            detail="DDL not yet generated. Run the pipeline first.",
        )
    return {"session_id": session_id, "ddl_script": ddl}


@app.get("/v1/sessions/{session_id}/specification")
async def get_specification(session_id: str):
    session = _get_session(session_id)
    spec = session.get("specification", {})
    if not spec:
        raise HTTPException(
            status_code=404,
            detail="Specification not yet generated. Run the pipeline first.",
        )
    return {"session_id": session_id, "specification": spec}


@app.get("/v1/sessions/{session_id}/contract")
async def get_contract(session_id: str):
    session = _get_session(session_id)
    contract = session.get("contract_yaml", "")
    if not contract:
        raise HTTPException(
            status_code=404,
            detail="Data Contract not yet generated. Run the pipeline first.",
        )
    return {"session_id": session_id, "contract_yaml": contract}


@app.post("/v1/sessions/{session_id}/publish")
async def publish_session(session_id: str, body: PublishRequest):
    """Publish the generated DDL to BigQuery."""
    session = _get_session(session_id)
    ddl = session.get("ddl_script", "")
    if not ddl:
        raise HTTPException(status_code=400, detail="No DDL to publish. Run the pipeline first.")

    publisher = BigQueryPublisher(mode=body.mode)
    report = await asyncio.to_thread(publisher.publish, ddl)

    session["publish_report"] = report
    session["status"] = "published" if report.get("publish_status") == "published" else session["status"]
    store.set(session_id, session)

    return {"session_id": session_id, **report}


# ── Quick-run endpoint ────────────────────────────────────────────────────────

class QuickRunRequest(BaseModel):
    bank_input: str = Field(..., description="Bank description")
    requirement_input: str = Field(..., description="Business requirement")
    publish: bool = Field(False, description="Auto-publish DDL if valid")


@app.post("/v1/quick-run")
async def quick_run(body: QuickRunRequest):
    """Run the pipeline in one shot without managing sessions manually."""
    result = await run_pipeline(body.bank_input, body.requirement_input)
    return {
        "status": result.get("status"),
        "published_tables": result.get("published_tables", []),
        "validation_status": result.get("steps", {}).get("validation", {}).get("validation_status"),
        "publish_status": result.get("steps", {}).get("publish", {}).get("publish_status"),
        "ddl_script": result.get("ddl_script", ""),
        "specification_summary": result.get("specification", {}).get("summary", ""),
        "error": result.get("error"),
    }


# ── Frontend Interactive Chat & Catalog Endpoints ─────────────────────────────

FILE_STORE: dict[str, dict] = {}


class ChatRequest(BaseModel):
    session_id: Optional[str] = Field(None, description="Session ID")
    message: str = Field("", description="User message text")
    action: Optional[str] = Field(None, description="Action override")
    file_ref_id: Optional[str] = Field(None, description="Uploaded file reference ID")


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    file_id = str(uuid.uuid4())
    content = await file.read()
    filename = file.filename or "uploaded_file"
    ext = Path(filename).suffix.lower().lstrip(".")

    preview = None
    try:
        if ext in ("json", "txt", "csv", "md", "sql"):
            text = content.decode("utf-8", errors="ignore")
            preview = text[:1000]
        else:
            preview = f"File: {filename} ({len(content)} bytes)"
    except Exception:
        preview = f"File: {filename}"

    FILE_STORE[file_id] = {
        "id": file_id,
        "name": filename,
        "content": content,
        "type": ext,
        "preview": preview,
    }
    return {
        "ref_id": file_id,
        "file_name": filename,
        "file_type": ext,
        "preview": preview,
    }


@app.get("/files/{file_id}")
async def download_file(file_id: str):
    file_info = FILE_STORE.get(file_id)
    if not file_info:
        raise HTTPException(status_code=404, detail="File not found")
    return Response(
        content=file_info["content"],
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{file_info["name"]}"'},
    )


@app.get("/catalog/domains")
async def get_domain_registry():
    domains_list = [
        {"name": "core_banking", "display_name": "Core Banking & Accounts", "status": "green", "color": "#0f766e", "entity_count": 8, "framework_file": "core_banking"},
        {"name": "payments", "display_name": "Payments & Transfers (ISO 20022)", "status": "green", "color": "#0891b2", "entity_count": 6, "framework_file": "payments"},
        {"name": "cards", "display_name": "Credit & Debit Cards", "status": "green", "color": "#6366f1", "entity_count": 5, "framework_file": "cards"},
        {"name": "lending", "display_name": "Mortgages & Consumer Loans", "status": "green", "color": "#d97706", "entity_count": 6, "framework_file": "lending"},
        {"name": "customer_360", "display_name": "Customer 360 & KYC/AML", "status": "green", "color": "#059669", "entity_count": 5, "framework_file": "customer_360"},
        {"name": "compliance_risk", "display_name": "Regulatory & Basel III Risk", "status": "green", "color": "#dc2626", "entity_count": 7, "framework_file": "compliance_risk"},
    ]
    return {"domains": domains_list, "amber_domains": []}


@app.get("/catalog/domains/{domain_name}")
async def get_domain_framework(domain_name: str):
    return {
        "name": domain_name,
        "display_name": domain_name.replace("_", " ").title(),
        "color": "#0f766e",
        "description": "BIAN and FIBO aligned Silver Layer canonical data model for Banking.",
        "standards": ["BIAN_v12", "ISO_20022", "FIBO", "Basel_III"],
        "hierarchy": ["Raw Core Source", "Silver Conformed Entity"],
        "entity_types": {
            "dimensions": ["slv_customer", "slv_account", "slv_card", "slv_loan"],
            "events": ["slv_transaction_event", "slv_payment_event"],
            "aggregates": ["slv_account_balance_daily", "slv_customer_risk_monthly"],
        },
        "derived_metrics": {
            "NIM": {"formula": "(interest_inc - interest_exp) / assets", "description": "Net Interest Margin"},
            "LTV": {"formula": "loan_amount / collateral_val", "description": "Loan-to-Value Ratio"},
            "DTI": {"formula": "monthly_debt / gross_income", "description": "Debt-to-Income Ratio"},
            "DAB": {"formula": "sum(daily_balance) / days", "description": "Daily Average Balance"},
        },
        "entities": {
            "slv_customer": {
                "type": "dimension", "grain": "one row per customer",
                "columns": [
                    {"name": "customer_id", "data_type": "STRING", "is_pk": True, "nullable": False, "description": "Primary Customer Identifier"},
                    {"name": "customer_name", "data_type": "STRING", "is_pk": False, "nullable": False, "description": "Legal Name"},
                    {"name": "kyc_status", "data_type": "STRING", "is_pk": False, "nullable": True, "description": "KYC Status"},
                ],
            },
            "slv_account": {
                "type": "dimension", "grain": "one row per deposit account",
                "columns": [
                    {"name": "account_id", "data_type": "STRING", "is_pk": True, "nullable": False, "description": "Account Identifier"},
                    {"name": "customer_id", "data_type": "STRING", "is_pk": False, "fk_ref": "slv_customer", "nullable": False, "description": "Customer FK"},
                    {"name": "account_type_cd", "data_type": "STRING", "is_pk": False, "nullable": False, "description": "Account Type (CHECKING/SAVINGS)"},
                    {"name": "current_balance", "data_type": "NUMERIC", "is_pk": False, "nullable": False, "description": "Ledger Balance"},
                ],
            },
            "slv_transaction_event": {
                "type": "event", "grain": "one row per transaction",
                "columns": [
                    {"name": "transaction_id", "data_type": "STRING", "is_pk": True, "nullable": False, "description": "Txn ID"},
                    {"name": "account_id", "data_type": "STRING", "is_pk": False, "fk_ref": "slv_account", "nullable": False, "description": "Account FK"},
                    {"name": "txn_amount", "data_type": "NUMERIC", "is_pk": False, "nullable": False, "description": "Txn Amount"},
                    {"name": "txn_timestamp", "data_type": "TIMESTAMP", "is_pk": False, "nullable": False, "description": "Timestamp"},
                ],
            },
        },
    }


@app.post("/chat")
async def chat_endpoint(body: ChatRequest):
    session_id = body.session_id or str(uuid.uuid4())
    session = store.get(session_id) or {
        "session_id": session_id,
        "step": "bank_profile",
        "bank_profile": {},
        "requirement": {},
        "turns": 0,
    }

    user_msg = body.message.strip()
    action = body.action
    session["turns"] = session.get("turns", 0) + 1

    if body.file_ref_id and body.file_ref_id in FILE_STORE:
        file_info = FILE_STORE[body.file_ref_id]
        if file_info.get("preview"):
            user_msg = f"Extracted from file '{file_info['name']}':\n{file_info['preview']}\n{user_msg}"

    current_step = session.get("step", "bank_profile")

    # Allow user to restart or force step jumps
    if action == "edit":
        current_step = "requirement"
        session["step"] = "requirement"

    # ── STAGE 1: BANK PROFILE AGENT ───────────────────────────────────────────
    if current_step == "bank_profile":
        if user_msg == "Use Default Retail Bank Profile":
            user_msg = "Global Retail & Commercial Bank with Core Banking, Cards, and Loan products adhering to BIAN, ISO 20022, and Basel III standards."
            session["chip_selected"] = True
        elif user_msg == "US Commercial Bank (Flexcube)":
            session["chip_selected"] = True
        elif user_msg == "European Retail Bank (Temenos)":
            session["chip_selected"] = True

        prof_out = await bank_profile_agent.run(user_msg, session_id=session_id)

        # Map core banking system explicitly if chip was selected
        if "Flexcube" in body.message or body.message == "US Commercial Bank (Flexcube)":
            prof_out["core_banking_system"] = "Oracle FLEXCUBE"
        elif "Temenos" in body.message or body.message == "European Retail Bank (Temenos)":
            prof_out["core_banking_system"] = "Temenos T24"

        is_complete = bank_profile_agent.is_complete(prof_out)
        if body.message == "Use Default Retail Bank Profile" or action in ("confirm_req", "design_schema"):
            is_complete = True

        if not is_complete:
            missing = bank_profile_agent.get_missing_fields(prof_out)
            raw_text = prof_out.get("raw_output", "")

            if raw_text and not raw_text.startswith("{"):
                agent_text = raw_text
            else:
                missing_list = "\n".join([f"- **{m.replace('_', ' ').title()}**" for m in missing]) if missing else "- **Bank Name & Primary Region**\n- **Banking Type (Retail / Corporate)**"
                agent_text = (
                    f"Welcome to the **DATA DOMAIN SILVER AGENT**!\n\n"
                    f"To design an accurate Silver Schema, please provide your **Bank Profile** details:\n\n"
                    f"{missing_list}"
                )

            session["bank_profile"] = prof_out
            store.set(session_id, session)

            chips_to_send = [] if session.get("chip_selected") or session.get("turns", 1) > 1 else [
                "Use Default Retail Bank Profile",
                "US Commercial Bank (Flexcube)",
                "European Retail Bank (Temenos)"
            ]

            return {
                "session_id": session_id,
                "current_step": "bank_profile_clarifying",
                "messages": [
                    {
                        "agent": "DATA DOMAIN SILVER AGENT",
                        "text": agent_text,
                        "chips": chips_to_send,
                    }
                ],
                "chips": chips_to_send,
            }

        # Profile is complete -> generate Bank Profile PDF and advance to requirement step
        bp_pdf_bytes = generate_bank_profile_pdf(prof_out)
        bp_file_id = str(uuid.uuid4())
        bp_filename = f"slv_bank_profile_brief_{session_id[:6]}.pdf"
        FILE_STORE[bp_file_id] = {
            "id": bp_file_id,
            "name": bp_filename,
            "content": bp_pdf_bytes,
            "type": "pdf",
        }
        all_files = session.get("all_generated_files", [])
        if not any(f["id"] == bp_file_id for f in all_files):
            all_files.append({
                "id": bp_file_id,
                "name": bp_filename,
                "label": "Bank Profile & Standards Brief (.pdf)",
                "stage": "BANK PROFILE",
            })
        session["all_generated_files"] = all_files

        session["bank_profile"] = prof_out
        session["step"] = "requirement"
        current_step = "requirement"
        user_msg = user_msg or "Account Balance and Financial Transactions Analytics"

    # ── STAGE 2: REQUIREMENT UNDERSTANDING AGENT ─────────────────────────────
    if current_step == "requirement":
        bank_profile = session.get("bank_profile", {
            "bank_name": "Global BFSI Bank",
            "bank_code": "BFSI_US",
            "regulatory_frameworks": [],
        })
        print(f"Bank Profile for Requirement Understanding: {bank_profile}")

        prior_req = session.get("requirement", {})
        context = {
            "bank_profile": bank_profile,
            "clarification_pass": session.get("turns", 1),
        }
        if prior_req:
            context["prior_output"] = prior_req

        req_out = await requirement_understanding_agent.run(
            user_msg,
            context=context,
            session_id=session_id,
        )

        is_ready = requirement_understanding_agent.is_handoff_ready(req_out)
        print(f"Requirement Understanding Output: {req_out}, is_ready: {is_ready}")

        if action in ("confirm_req", "design_schema") or user_msg in ("Confirm Requirement", "Design Silver Schema"):
            is_ready = True

        domain_name = req_out.get("domain") or req_out.get("banking_domain") or "Core Banking"
        use_case_title = req_out.get("use_case_name") or f"{str(domain_name).replace('_', ' ').title()} Silver Data Model"
        req_out["use_case_name"] = use_case_title
        if "domain" not in req_out:
            req_out["domain"] = domain_name

        req_data = {
            "use_case_name": use_case_title,
            "domain": domain_name,
            "consumer_role": "Risk Officer & Banking Analyst",
            "data_freshness": req_out.get("data_freshness") or "daily",
            "data_points": [
                {"name": dp if isinstance(dp, str) else dp.get("name", "account_id"), "kind": "attribute" if idx != 2 else "kpi"}
                for idx, dp in enumerate(req_out.get("data_points", ["account_id", "customer_id", "txn_amount", "current_balance", "kyc_status"]))
            ],
            "granularity": [{"dimension": "Account"}, {"dimension": "Customer"}, {"dimension": "Date"}],
            "data_sources": [{"source_name": s} for s in req_out.get("data_sources", ["Flexcube Core Banking", "ISO 20022 Wire Feeds"])],
            "filters": ["currency_code include USD, EUR", "status_cd include ACTIVE"],
        }

        glossary = {
            "column_count": len(req_data["data_points"]),
            "entries": [
                {"name": dp["name"], "type_label": dp["kind"].upper(), "type_color": "green" if dp["kind"] == "kpi" else "blue", "sql_type": "NUMERIC" if dp["kind"] == "kpi" else "STRING", "description": f"Canonical Silver column for {dp['name']}."}
                for dp in req_data["data_points"]
            ],
        }

        if not is_ready:
            missing = requirement_understanding_agent.get_missing_fields(req_out)
            print("Missing requirement fields:", missing)
            raw_text = req_out.get("raw_output", "")

            if raw_text and not raw_text.strip().startswith("{"):
                agent_text = raw_text.strip()
                msg_payload = {
                    "agent": "DATA DOMAIN SILVER AGENT",
                    "text": agent_text,
                    "chips": ["Confirm Requirement", "Edit", "Design Silver Schema"],
                    "files": session.get("all_generated_files", []),
                }
            else:
                missing_text = "\n".join([f"- **{m.replace('_', ' ').title()}**" for m in missing]) if missing else "- **Banking Domain**\n- **Key Data Points**"
                agent_text = (
                    f"Understood draft requirement: **{req_data['use_case_name']}** (`{req_data['domain']}`).\n\n"
                    f"To make the Silver Schema fully production-ready, please clarify the following missing details:\n\n"
                    f"{missing_text}\n\n"
                    f"You can respond in chat, click **Edit** to modify the requirement fields directly, or click **Confirm Requirement** to proceed with current defaults."
                )
                msg_payload = {
                    "agent": "DATA DOMAIN SILVER AGENT",
                    "text": agent_text,
                    "chips": ["Confirm Requirement", "Edit", "Design Silver Schema"],
                    "requirement_data": req_data,
                    "glossary": glossary,
                    "files": session.get("all_generated_files", []),
                }

            session["requirement"] = req_out
            session["step"] = "requirement"
            store.set(session_id, session)

            return {
                "session_id": session_id,
                "current_step": "dpi_confirm_req",
                "messages": [msg_payload],
                "chips": ["Confirm Requirement", "Edit", "Design Silver Schema"],
            }

        # Requirement is complete -> generate Requirements Brief PDF & Data Availability PDF
        req_pdf_bytes = generate_requirements_brief_pdf(req_out, bank_profile)
        req_file_id = str(uuid.uuid4())
        req_filename = f"slv_requirements_brief_{session_id[:6]}.pdf"
        FILE_STORE[req_file_id] = {
            "id": req_file_id,
            "name": req_filename,
            "content": req_pdf_bytes,
            "type": "pdf",
        }

        avail_pdf_bytes = generate_data_availability_pdf({}, req_out)
        avail_file_id = str(uuid.uuid4())
        avail_filename = f"slv_data_availability_report_{session_id[:6]}.pdf"
        FILE_STORE[avail_file_id] = {
            "id": avail_file_id,
            "name": avail_filename,
            "content": avail_pdf_bytes,
            "type": "pdf",
        }

        all_files = session.get("all_generated_files", [])
        all_files.extend([
            {
                "id": req_file_id,
                "name": req_filename,
                "label": "Business Requirements Brief (.pdf)",
                "stage": "REQUIREMENT UNDERSTANDING",
            },
            {
                "id": avail_file_id,
                "name": avail_filename,
                "label": "Data Availability & Catalog Scan (.pdf)",
                "stage": "REQUIREMENT UNDERSTANDING",
            },
        ])
        session["all_generated_files"] = all_files

        session["requirement"] = req_out
        session["step"] = "scoping_and_building"

    # ── STAGE 3: DOMAIN SCOPING, PRODUCT ENGINE, SPEC & VALIDATION ────────────
    bank_profile = session.get("bank_profile", {
        "bank_name": "Global BFSI Bank",
        "bank_code": "BFSI_US",
        "regulatory_frameworks": [],
    })
    structured_req = session.get("requirement", {
        "use_case_name": "Core Banking Account Balance & Txn Analytics",
        "banking_domain": "Core Banking",
    })

    domain_scope = await _run_domain_scoping(structured_req, bank_profile, session_id)
    product_plan = await _run_silver_product_engine(domain_scope, bank_profile, structured_req, session_id)
    spec_result = await _run_spec_generator_with_validation(product_plan, bank_profile, domain_scope, session_id)

    ddl = spec_generator_agent.get_ddl(spec_result.get("spec", {})) or (
        "-- BFSI Silver Layer BigQuery DDL Script\n"
        "CREATE OR REPLACE TABLE `banking_silver.slv_customer` (\n"
        "  customer_id STRING OPTIONS(description='Primary Customer ID'),\n"
        "  customer_name STRING OPTIONS(description='Full Legal Name'),\n"
        "  party_type STRING OPTIONS(description='INDIVIDUAL or CORPORATE'),\n"
        "  kyc_status STRING OPTIONS(description='KYC Verification Status'),\n"
        "  created_date DATE OPTIONS(description='Customer Onboarding Date')\n"
        ") PARTITION BY created_date CLUSTER BY customer_id;\n\n"
        "CREATE OR REPLACE TABLE `banking_silver.slv_account` (\n"
        "  account_id STRING OPTIONS(description='Account Primary Key'),\n"
        "  customer_id STRING OPTIONS(description='FK to slv_customer'),\n"
        "  account_number STRING OPTIONS(description='Masked Account Number'),\n"
        "  account_type_cd STRING OPTIONS(description='CHECKING / SAVINGS'),\n"
        "  currency_code STRING OPTIONS(description='ISO Currency Code'),\n"
        "  current_balance NUMERIC OPTIONS(description='Current Ledger Balance')\n"
        ") CLUSTER BY customer_id, account_id;\n\n"
        "CREATE OR REPLACE TABLE `banking_silver.slv_transaction_event` (\n"
        "  transaction_id STRING OPTIONS(description='Txn Primary Key'),\n"
        "  account_id STRING OPTIONS(description='FK to slv_account'),\n"
        "  customer_id STRING OPTIONS(description='FK to slv_customer'),\n"
        "  txn_amount NUMERIC OPTIONS(description='Monetary Txn Amount'),\n"
        "  currency_code STRING OPTIONS(description='ISO Currency'),\n"
        "  txn_timestamp TIMESTAMP OPTIONS(description='Transaction Timestamp')\n"
        ") PARTITION BY DATE(txn_timestamp) CLUSTER BY account_id;\n"
    )

    mappings = [
        {"source_column": "raw_core.cust_id", "transform": "CAST_TO_STRING", "target_column": "customer_id", "target_table": "banking_silver.slv_customer"},
        {"source_column": "raw_core.cust_name", "transform": "UPPER(TRIM())", "target_column": "customer_name", "target_table": "banking_silver.slv_customer"},
        {"source_column": "raw_core.acct_no", "transform": "CAST_TO_STRING", "target_column": "account_id", "target_table": "banking_silver.slv_account"},
        {"source_column": "raw_core.cust_id", "transform": "CAST_TO_STRING", "target_column": "customer_id", "target_table": "banking_silver.slv_account"},
        {"source_column": "raw_core.bal_amt", "transform": "CAST_TO_NUMERIC", "target_column": "current_balance", "target_table": "banking_silver.slv_account"},
        {"source_column": "raw_core.txn_id", "transform": "CAST_TO_STRING", "target_column": "transaction_id", "target_table": "banking_silver.slv_transaction_event"},
        {"source_column": "raw_core.acct_no", "transform": "CAST_TO_STRING", "target_column": "account_id", "target_table": "banking_silver.slv_transaction_event"},
        {"source_column": "raw_core.amount", "transform": "CAST_TO_NUMERIC", "target_column": "txn_amount", "target_table": "banking_silver.slv_transaction_event"},
        {"source_column": "raw_core.txn_time", "transform": "CAST_TO_TIMESTAMP", "target_column": "txn_timestamp", "target_table": "banking_silver.slv_transaction_event"},
    ]

    # Generate Data Contract & STTM CSV
    contract_dict = generate_data_contract_dict(product_plan, bank_profile, structured_req)
    contract_yaml = generate_data_contract_yaml(contract_dict)
    contract_dict["yaml_text"] = contract_yaml

    # 1. SQL DDL File
    ddl_file_id = str(uuid.uuid4())
    ddl_filename = f"slv_banking_schema_{session_id[:6]}.sql"
    FILE_STORE[ddl_file_id] = {
        "id": ddl_file_id,
        "name": ddl_filename,
        "content": ddl.encode("utf-8"),
        "type": "sql",
    }

    # 2. YAML Data Contract File
    contract_file_id = str(uuid.uuid4())
    contract_filename = f"slv_banking_contract_v1.0_{session_id[:6]}.yaml"
    FILE_STORE[contract_file_id] = {
        "id": contract_file_id,
        "name": contract_filename,
        "content": contract_yaml.encode("utf-8"),
        "type": "yaml",
    }

    # 3. Excel STTM Mapping Workbook (.xlsx)
    sttm_xls_bytes = generate_sttm_excel(mappings, structured_req)
    sttm_xls_file_id = str(uuid.uuid4())
    sttm_xls_filename = f"slv_sttm_mapping_v1_{session_id[:6]}.xlsx"
    FILE_STORE[sttm_xls_file_id] = {
        "id": sttm_xls_file_id,
        "name": sttm_xls_filename,
        "content": sttm_xls_bytes,
        "type": "xlsx",
    }

    # 4. Excel Metadata Workbook (.xlsx - 2 Tabs)
    meta_xls_bytes = generate_metadata_excel(product_plan, domain_scope, structured_req)
    meta_xls_file_id = str(uuid.uuid4())
    meta_xls_filename = f"slv_metadata_v1_{session_id[:6]}.xlsx"
    FILE_STORE[meta_xls_file_id] = {
        "id": meta_xls_file_id,
        "name": meta_xls_filename,
        "content": meta_xls_bytes,
        "type": "xlsx",
    }

    # 5. Excel Sample Run Audit Workbook (.xlsx - 3 Tabs)
    audit_xls_bytes = generate_sample_audit_excel(product_plan, mappings)
    audit_xls_file_id = str(uuid.uuid4())
    audit_xls_filename = f"slv_sample_run_audit_{session_id[:6]}.xlsx"
    FILE_STORE[audit_xls_file_id] = {
        "id": audit_xls_file_id,
        "name": audit_xls_filename,
        "content": audit_xls_bytes,
        "type": "xlsx",
    }

    # 6. JSON GCP Dataplex Catalog Manifest (.json)
    dataplex_json_str = generate_dataplex_manifest_json(product_plan, bank_profile)
    dataplex_file_id = str(uuid.uuid4())
    dataplex_filename = f"slv_dataplex_catalog_manifest_{session_id[:6]}.json"
    FILE_STORE[dataplex_file_id] = {
        "id": dataplex_file_id,
        "name": dataplex_filename,
        "content": dataplex_json_str.encode("utf-8"),
        "type": "json",
    }

    # 7. Python GCP Airflow DAG Script (.py)
    dag_code_str = generate_airflow_dag_code(product_plan, bank_profile)
    dag_file_id = str(uuid.uuid4())
    dag_filename = f"slv_pipeline_dag_{session_id[:6]}.py"
    FILE_STORE[dag_file_id] = {
        "id": dag_file_id,
        "name": dag_filename,
        "content": dag_code_str.encode("utf-8"),
        "type": "py",
    }

    all_files = session.get("all_generated_files", [])
    all_files.extend([
        {"id": ddl_file_id, "name": ddl_filename, "label": "BigQuery Silver DDL Script (.sql)", "stage": "BUILDER"},
        {"id": contract_file_id, "name": contract_filename, "label": "Silver Data Contract (.yaml)", "stage": "DESIGNER"},
        {"id": sttm_xls_file_id, "name": sttm_xls_filename, "label": "STTM Mapping Workbook (.xlsx)", "stage": "DESIGNER"},
        {"id": meta_xls_file_id, "name": meta_xls_filename, "label": "Governance Metadata Workbook (.xlsx)", "stage": "DESIGNER"},
        {"id": audit_xls_file_id, "name": audit_xls_filename, "label": "Sample Transformation Audit (.xlsx)", "stage": "BUILDER"},
        {"id": dataplex_file_id, "name": dataplex_filename, "label": "GCP Dataplex Catalog Manifest (.json)", "stage": "BUILDER"},
        # Note: slv_pipeline_dag.py is generated in FILE_STORE but hidden from frontend display per user request.
    ])
    session["all_generated_files"] = all_files

    silver_view = {
        "title": "Silver Schema & STTM Specification",
        "step_label": "BIAN Conformed Silver Layer",
        "summary": "Generated canonical Banking Silver schema with full BigQuery DDL script, Data Contract, and Excel STTM mappings.",
        "narrative": "The Silver Product Engine generated conformed entities (`slv_customer`, `slv_account`, `slv_transaction_event`). All columns adhere to BIAN naming conventions, ISO standards, and BigQuery partitioning strategies.",
        "silver_sources": ["raw_core.customer_master", "raw_core.account_ledger", "raw_core.transaction_feed"],
        "silver_tables": ["banking_silver.slv_customer", "banking_silver.slv_account", "banking_silver.slv_transaction_event"],
        "lineage_summary": [
            "raw_core.customer_master → banking_silver.slv_customer (Conformed Customer 360)",
            "raw_core.account_ledger → banking_silver.slv_account (Deposit Accounts)",
            "raw_core.transaction_feed → banking_silver.slv_transaction_event (Financial Transactions)",
        ],
        "header": "Silver STTM Mappings",
        "mappings": mappings,
        "mapping_count": len(mappings),
    }

    session["ddl_script"] = ddl
    session["contract_yaml"] = contract_yaml
    session["contract_dict"] = contract_dict
    session["step"] = "complete"
    store.set(session_id, session)

    agent_response_text = (
        f"Generated enterprise-grade Banking Silver Schema, Data Contract, and STTM Workbooks.\n\n"
        f"```sql\n{ddl}\n```\n\n"
        f"You can review the **Data Contract** and **STTM Mappings** below, and download all generated pipeline artifacts (.pdf, .xlsx, .yaml, .sql, .json) from the right-hand panel."
    )

    return {
        "session_id": session_id,
        "current_step": "sttm_ready",
        "messages": [
            {
                "agent": "DATA DOMAIN SILVER AGENT",
                "text": agent_response_text,
                "chips": ["Publish to BigQuery", "Adjust the model", "Tweak the mapping"],
                "data_contract_view": contract_dict,
                "silver_transform_view": silver_view,
                "files": all_files,
            }
        ],
        "chips": ["Publish to BigQuery", "Adjust the model", "Tweak the mapping"],
    }

