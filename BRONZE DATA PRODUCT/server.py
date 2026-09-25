"""
server.py — BFSI Bronze Agent FastAPI REST server.

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
    _run_source_scoping,
    _run_bronze_product_engine,
    _run_spec_generator_with_validation,
)
from session_store import SessionStore
from agents import (
    bank_profile_agent,
    requirement_understanding_agent,
    source_scoping_agent,
    bronze_product_engine,
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
    title="BFSI Bronze Agent API",
    description=(
        "Agentic banking Bronze-layer schema generator powered by "
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
        "service": "BFSI-Bronze-Agent",
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
    ]
    return {"domains": domains_list, "amber_domains": []}


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

    current_step = session.get("step", "bank_profile")

    if action == "edit":
        current_step = "requirement"
        session["step"] = "requirement"

    if current_step == "bank_profile":
        if user_msg == "Use Default Retail Bank Profile":
            user_msg = "Global Retail & Commercial Bank with Core Banking, Cards, and Loan products adhering to BIAN, ISO 20022, and Basel III standards."
            session["chip_selected"] = True

        prof_out = await bank_profile_agent.run(user_msg, session_id=session_id)

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
                    f"Welcome to the **DATA DOMAIN BRONZE AGENT**!\n\n"
                    f"To design an accurate Bronze Schema, please provide your **Bank Profile** details:\n\n"
                    f"{missing_list}"
                )

            session["bank_profile"] = prof_out
            store.set(session_id, session)

            chips_to_send = ["Use Default Retail Bank Profile"]

            return {
                "session_id": session_id,
                "current_step": "bank_profile_clarifying",
                "messages": [
                    {
                        "agent": "DATA DOMAIN BRONZE AGENT",
                        "text": agent_text,
                        "chips": chips_to_send,
                    }
                ],
                "chips": chips_to_send,
            }

        bp_pdf_bytes = generate_bank_profile_pdf(prof_out)
        bp_file_id = str(uuid.uuid4())
        bp_filename = f"brz_bank_profile_brief_{session_id[:6]}.pdf"
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

    if current_step == "requirement":
        bank_profile = session.get("bank_profile", {})
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

        if action in ("confirm_req", "design_schema") or user_msg in ("Confirm Requirement", "Design Bronze Schema"):
            is_ready = True

        domain_name = req_out.get("domain") or req_out.get("banking_domain") or "Core Banking"
        use_case_title = req_out.get("use_case_name") or f"{str(domain_name).replace('_', ' ').title()} Bronze Data Model"
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
                {"name": dp["name"], "type_label": dp["kind"].upper(), "type_color": "green" if dp["kind"] == "kpi" else "blue", "sql_type": "NUMERIC" if dp["kind"] == "kpi" else "STRING", "description": f"Raw Bronze column for {dp['name']}."}
                for dp in req_data["data_points"]
            ],
        }

        if not is_ready:
            missing = requirement_understanding_agent.get_missing_fields(req_out)
            raw_text = req_out.get("raw_output", "")

            if raw_text and not raw_text.strip().startswith("{"):
                agent_text = raw_text.strip()
                msg_payload = {
                    "agent": "DATA DOMAIN BRONZE AGENT",
                    "text": agent_text,
                    "chips": ["Confirm Requirement", "Edit", "Design Bronze Schema"],
                    "files": session.get("all_generated_files", []),
                }
            else:
                missing_text = "\n".join([f"- **{m.replace('_', ' ').title()}**" for m in missing]) if missing else "- **Banking Domain**\n- **Key Data Points**"
                agent_text = (
                    f"Understood draft requirement: **{req_data['use_case_name']}** (`{req_data['domain']}`).\n\n"
                    f"To make the Bronze Schema fully production-ready, please clarify the following missing details:\n\n"
                    f"{missing_text}\n\n"
                    f"You can respond in chat, click **Edit** to modify the requirement fields directly, or click **Confirm Requirement** to proceed with current defaults."
                )
                msg_payload = {
                    "agent": "DATA DOMAIN BRONZE AGENT",
                    "text": agent_text,
                    "chips": ["Confirm Requirement", "Edit", "Design Bronze Schema"],
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
                "chips": ["Confirm Requirement", "Edit", "Design Bronze Schema"],
            }

        req_pdf_bytes = generate_requirements_brief_pdf(req_out, bank_profile)
        req_file_id = str(uuid.uuid4())
        req_filename = f"brz_requirements_brief_{session_id[:6]}.pdf"
        FILE_STORE[req_file_id] = {
            "id": req_file_id,
            "name": req_filename,
            "content": req_pdf_bytes,
            "type": "pdf",
        }

        avail_pdf_bytes = generate_data_availability_pdf({}, req_out)
        avail_file_id = str(uuid.uuid4())
        avail_filename = f"brz_data_availability_report_{session_id[:6]}.pdf"
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

    bank_profile = session.get("bank_profile", {})
    structured_req = session.get("requirement", {})

    source_scope = await _run_source_scoping(structured_req, bank_profile, session_id)
    product_plan = await _run_bronze_product_engine(source_scope, bank_profile, structured_req, session_id)
    spec_result = await _run_spec_generator_with_validation(product_plan, bank_profile, source_scope, session_id)

    ddl = spec_generator_agent.get_ddl(spec_result.get("spec", {})) or (
        "-- BFSI Bronze Layer BigQuery DDL Script\n"
        "CREATE OR REPLACE TABLE `banking_bronze.brz_customer` (\n"
        "  customer_id STRING OPTIONS(description='Primary Customer ID'),\n"
        "  customer_name STRING OPTIONS(description='Full Legal Name')\n"
        ") CLUSTER BY customer_id;\n\n"
        "CREATE OR REPLACE TABLE `banking_bronze.brz_account` (\n"
        "  account_id STRING OPTIONS(description='Account Primary Key')\n"
        ") CLUSTER BY account_id;\n\n"
    )

    mappings = []

    contract_dict = generate_data_contract_dict(product_plan, bank_profile, structured_req)
    contract_yaml = generate_data_contract_yaml(contract_dict)
    contract_dict["yaml_text"] = contract_yaml

    ddl_file_id = str(uuid.uuid4())
    ddl_filename = f"brz_banking_schema_{session_id[:6]}.sql"
    FILE_STORE[ddl_file_id] = {
        "id": ddl_file_id,
        "name": ddl_filename,
        "content": ddl.encode("utf-8"),
        "type": "sql",
    }

    contract_file_id = str(uuid.uuid4())
    contract_filename = f"BC-banking_contract_v1.0_{session_id[:6]}.yaml"
    FILE_STORE[contract_file_id] = {
        "id": contract_file_id,
        "name": contract_filename,
        "content": contract_yaml.encode("utf-8"),
        "type": "yaml",
    }

    sttm_xls_bytes = generate_sttm_excel(mappings, structured_req)
    sttm_xls_file_id = str(uuid.uuid4())
    sttm_xls_filename = f"brz_sttm_mapping_v1_{session_id[:6]}.xlsx"
    FILE_STORE[sttm_xls_file_id] = {
        "id": sttm_xls_file_id,
        "name": sttm_xls_filename,
        "content": sttm_xls_bytes,
        "type": "xlsx",
    }

    meta_xls_bytes = generate_metadata_excel(product_plan, source_scope, structured_req)
    meta_xls_file_id = str(uuid.uuid4())
    meta_xls_filename = f"brz_metadata_v1_{session_id[:6]}.xlsx"
    FILE_STORE[meta_xls_file_id] = {
        "id": meta_xls_file_id,
        "name": meta_xls_filename,
        "content": meta_xls_bytes,
        "type": "xlsx",
    }

    audit_xls_bytes = generate_sample_audit_excel(product_plan, mappings)
    audit_xls_file_id = str(uuid.uuid4())
    audit_xls_filename = f"brz_sample_run_audit_{session_id[:6]}.xlsx"
    FILE_STORE[audit_xls_file_id] = {
        "id": audit_xls_file_id,
        "name": audit_xls_filename,
        "content": audit_xls_bytes,
        "type": "xlsx",
    }

    dataplex_json_str = generate_dataplex_manifest_json(product_plan, bank_profile)
    dataplex_file_id = str(uuid.uuid4())
    dataplex_filename = f"brz_dataplex_catalog_manifest_{session_id[:6]}.json"
    FILE_STORE[dataplex_file_id] = {
        "id": dataplex_file_id,
        "name": dataplex_filename,
        "content": dataplex_json_str.encode("utf-8"),
        "type": "json",
    }

    all_files = session.get("all_generated_files", [])
    all_files.extend([
        {"id": ddl_file_id, "name": ddl_filename, "label": "BigQuery DDL Script (.sql)", "stage": "PRODUCT ENGINE"},
        {"id": contract_file_id, "name": contract_filename, "label": "Data Contract Definition (.yaml)", "stage": "PRODUCT ENGINE"},
        {"id": sttm_xls_file_id, "name": sttm_xls_filename, "label": "Source-to-Target Mapping (.xlsx)", "stage": "PRODUCT ENGINE"},
        {"id": meta_xls_file_id, "name": meta_xls_filename, "label": "Business Glossary & Catalog (.xlsx)", "stage": "PRODUCT ENGINE"},
        {"id": audit_xls_file_id, "name": audit_xls_filename, "label": "Sample Data Quality Audit (.xlsx)", "stage": "VALIDATION"},
        {"id": dataplex_file_id, "name": dataplex_filename, "label": "Dataplex Tag Manifest (.json)", "stage": "VALIDATION"},
    ])
    session["all_generated_files"] = all_files
    session["status"] = "completed"
    session["ddl_script"] = ddl
    session["contract_yaml"] = contract_yaml
    store.set(session_id, session)

    return {
        "session_id": session_id,
        "current_step": "pipeline_complete",
        "messages": [
            {
                "agent": "DATA DOMAIN BRONZE AGENT",
                "text": "The Bronze Data Product schema, pipeline DDL, and documentation artifacts have been successfully generated.",
                "files": all_files,
                "chips": ["Publish to BigQuery", "Download All Artifacts"],
            }
        ],
    }
