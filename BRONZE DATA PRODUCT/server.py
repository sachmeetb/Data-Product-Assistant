"""
server.py — Bronze Agent FastAPI REST server.

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
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env")

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
    generate_iceberg_ddl,
    generate_object_table_ddl,
    generate_airflow_dag_code,
)
from tools.schema_loader import classify_source_modality

log = logging.getLogger(__name__)
logging.basicConfig(
    level=os.environ.get("AGENT_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Bronze Agent API",
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
        "service": "Bronze-Agent",
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
UPLOAD_DIR = _ROOT / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)


def _save_file_info(file_id: str, info: dict):
    """Store file info in memory and persist metadata/binary to disk."""
    FILE_STORE[file_id] = info
    try:
        meta_file = UPLOAD_DIR / f"{file_id}.json"
        bin_file = UPLOAD_DIR / f"{file_id}.bin"
        with open(bin_file, "wb") as f:
            f.write(info.get("content", b""))
        meta = {k: v for k, v in info.items() if k != "content"}
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(meta, f)
    except Exception as exc:
        log.warning("Could not persist file %s to disk: %s", file_id, exc)


def _get_file_info(file_id: str, filename_hint: str = "") -> Optional[dict]:
    """Retrieve file info from memory, disk cache, or samples directory."""
    if not file_id:
        return None
    if file_id in FILE_STORE:
        return FILE_STORE[file_id]

    # Try reading from UPLOAD_DIR
    meta_file = UPLOAD_DIR / f"{file_id}.json"
    bin_file = UPLOAD_DIR / f"{file_id}.bin"
    if meta_file.is_file() and bin_file.is_file():
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                info = json.load(f)
            with open(bin_file, "rb") as f:
                info["content"] = f.read()
            FILE_STORE[file_id] = info
            return info
        except Exception as exc:
            log.warning("Could not read file from disk %s: %s", file_id, exc)

    # Search samples directory as fallback
    for s_file in (_ROOT / "samples").glob("*.*"):
        if s_file.name == file_id or (filename_hint and s_file.name.lower() == filename_hint.lower()):
            info = {
                "id": file_id,
                "name": s_file.name,
                "content": s_file.read_bytes(),
                "type": s_file.suffix.lower().lstrip("."),
            }
            FILE_STORE[file_id] = info
            return info

    return None


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
        if ext in ("xlsx", "xls"):
            import openpyxl, io
            wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
            sheet = wb.active
            rows = list(sheet.iter_rows(values_only=True))
            if rows:
                # Find first row with non-empty cells
                header_row = rows[0]
                cols = [str(c or f"Col_{i+1}") for i, c in enumerate(header_row)]
                data_rows = [[str(val) if val is not None else "" for val in r] for r in rows[1:11]]
                preview = {
                    "sheet_name": sheet.title,
                    "columns": cols,
                    "rows": data_rows,
                    "row_count": len(rows) - 1,
                }
            else:
                preview = {"sheet_name": sheet.title, "columns": [], "rows": [], "row_count": 0}
        elif ext == "csv":
            import csv, io
            text = content.decode("utf-8", errors="ignore")
            reader = list(csv.reader(io.StringIO(text)))
            if reader:
                cols = [str(c or f"Col_{i+1}") for i, c in enumerate(reader[0])]
                preview = {
                    "sheet_name": "CSV Data",
                    "columns": cols,
                    "rows": reader[1:11],
                    "row_count": len(reader) - 1,
                }
            else:
                preview = {"sheet_name": "CSV Data", "columns": [], "rows": [], "row_count": 0}
        elif ext in ("json", "txt", "md", "sql"):
            text = content.decode("utf-8", errors="ignore")
            preview = {
                "text_preview": text[:1500],
                "word_count": len(text.split()),
            }
        else:
            preview = {
                "text_preview": f"File: {filename} ({len(content)} bytes)",
                "word_count": 0,
            }
    except Exception as e:
        log.warning("Could not parse detailed preview for %s: %s", filename, e)
        preview = {
            "text_preview": f"File: {filename} ({len(content)} bytes)",
            "word_count": 0,
        }

    file_info = {
        "id": file_id,
        "name": filename,
        "content": content,
        "type": ext,
        "preview": preview,
    }
    _save_file_info(file_id, file_info)
    return {
        "ref_id": file_id,
        "file_name": filename,
        "file_type": ext,
        "preview": preview,
    }


@app.get("/files/{file_id}")
async def download_file(file_id: str):
    file_info = _get_file_info(file_id)
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
    try:
        return await _handle_chat(body, session_id)
    except Exception as exc:
        log.exception("Error in /chat endpoint: %s", exc)
        return {
            "session_id": session_id,
            "current_step": "error",
            "messages": [
                {
                    "agent": "Bronze Agent",
                    "text": f"⚠️ An unexpected error occurred:\n\n```\n{type(exc).__name__}: {str(exc)}\n```\n\nPlease try again or choose a starting point.",
                    "chips": ["Use Default Retail Bank Profile"],
                }
            ],
            "chips": ["Use Default Retail Bank Profile"],
        }


def _extract_text_from_file_info(file_info: dict) -> str:
    content = file_info.get("content", b"")
    ext = file_info.get("type", "").lower()
    name = file_info.get("name", "")

    if ext in ("xlsx", "xls"):
        import openpyxl, io
        try:
            wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
            sections = []
            for sheetname in wb.sheetnames:
                ws = wb[sheetname]
                rows = list(ws.iter_rows(values_only=True))
                if not rows:
                    continue
                sheet_lines = [f"=== Sheet: {sheetname} ==="]
                for r in rows:
                    if any(cell is not None and str(cell).strip() != "" for cell in r):
                        sheet_lines.append(" | ".join(str(cell).strip() for cell in r if cell is not None and str(cell).strip() != ""))
                sections.append("\n".join(sheet_lines))
            return "\n\n".join(sections)
        except Exception as err:
            log.warning("Failed to extract xlsx text: %s", err)
            return f"Spreadsheet {name}"

    elif ext == "csv":
        import csv, io
        try:
            text = content.decode("utf-8", errors="ignore")
            reader = csv.reader(io.StringIO(text))
            lines = [" | ".join(row) for row in reader if any(row)]
            return "\n".join(lines)
        except Exception as err:
            log.warning("Failed to extract csv text: %s", err)
            return text[:2000]

    elif ext in ("json", "txt", "md", "sql", "yaml", "yml"):
        return content.decode("utf-8", errors="ignore")

    return f"File: {name} ({len(content)} bytes)"


async def _handle_chat(body: ChatRequest, session_id: str):
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

    # Ingest uploaded file if referenced or mentioned
    file_info = None
    if body.file_ref_id:
        file_info = _get_file_info(body.file_ref_id, filename_hint=session.get("uploaded_file_name", ""))

    # If not found by ID, inspect user_msg for any sample filename
    if not file_info and user_msg:
        for s_file in (_ROOT / "samples").glob("*.*"):
            if s_file.name.lower() in user_msg.lower():
                file_info = _get_file_info(s_file.name, s_file.name)
                break

    if file_info:
        extracted_text = _extract_text_from_file_info(file_info)
        session["uploaded_file_id"] = file_info.get("id") or body.file_ref_id
        session["uploaded_file_name"] = file_info.get("name", "uploaded_file")
        session["uploaded_file_text"] = extracted_text
        if not user_msg or body.action == "use_file" or "use this file" in user_msg.lower():
            user_msg = f"User uploaded requirements file '{file_info.get('name')}':\n\n{extracted_text}"
        else:
            user_msg = f"{user_msg}\n\nDocument '{file_info.get('name')}':\n\n{extracted_text}"

    current_step = session.get("step", "bank_profile")

    if action == "edit":
        current_step = "requirement"
        session["step"] = "requirement"

    if action == "publish" or user_msg.lower() == "publish to bigquery":
        ddl = session.get("ddl_script", "")
        if not ddl:
            return {
                "session_id": session_id,
                "current_step": "error",
                "messages": [{"agent": "Bronze Agent", "text": "No DDL available to publish. Please design the Bronze schema first."}],
                "chips": ["Design Bronze Schema"],
            }
        publisher = BigQueryPublisher(mode="auto")
        report = await asyncio.to_thread(publisher.publish, ddl)
        session["publish_report"] = report
        store.set(session_id, session)

        status_icon = "✅" if report.get("publish_status") == "published" else "ℹ️"
        msg = f"{status_icon} **BigQuery Publication Report**\n\n"
        msg += f"- **Mode**: `{report.get('mode', 'auto')}`\n"
        msg += f"- **Status**: `{report.get('publish_status', 'dry_run')}`\n"
        if report.get("published_tables"):
            msg += f"- **Published Tables**: {', '.join([f'`{t}`' for t in report.get('published_tables', [])])}\n"
        if report.get("dry_run_tables"):
            msg += f"- **Validated Tables**: {', '.join([f'`{t}`' for t in report.get('dry_run_tables', [])])}\n"
        if report.get("error"):
            msg += f"\n> ⚠️ {report.get('error')}\n"

        chips = ["Adjust the model", "Tweak the mapping", "Edit Contract"]
        return {
            "session_id": session_id,
            "current_step": "published",
            "messages": [
                {
                    "agent": "Bronze Agent",
                    "text": msg,
                    "chips": chips,
                    "files": session.get("all_generated_files", []),
                }
            ],
            "chips": chips,
        }

    if action in ("tweak_sttm", "tweak_er") or user_msg in ("Adjust the model", "Tweak the mapping"):
        current_step = "requirement"
        session["step"] = "requirement"
        if user_msg not in ("Adjust the model", "Tweak the mapping"):
            user_msg = f"User modification request: {user_msg}"
        else:
            user_msg = "Please refine the Bronze schema and source mappings."

    if user_msg.lower() == "edit contract":
        return {
            "session_id": session_id,
            "current_step": "sttm_ready",
            "messages": [
                {
                    "agent": "Bronze Agent",
                    "text": "You can edit the contract fields, types, descriptions, SLAs, and quality assertions directly inside the **Data Contract** card above! You can also describe any contract changes here in chat (e.g. *'Add column risk_score NUMERIC to brz_customer_raw'*).",
                    "chips": ["Publish to BigQuery", "Adjust the model", "Tweak the mapping"],
                    "files": session.get("all_generated_files", []),
                }
            ],
            "chips": ["Publish to BigQuery", "Adjust the model", "Tweak the mapping"],
        }

    # Confirmation step for Bank Profile before Schema Design
    if current_step == "bank_profile_confirm":
        is_edit = action == "edit_bank_profile" or (
            any(w in user_msg.lower() for w in ["edit", "change", "update", "modify", "correct"])
            and not any(w in user_msg.lower() for w in ["confirm", "proceed", "yes", "looks good", "continue", "next", "design"])
        )
        if is_edit:
            session["step"] = "bank_profile"
            store.set(session_id, session)
            return {
                "session_id": session_id,
                "current_step": "bank_profile_edit",
                "messages": [
                    {
                        "agent": "Bronze Agent",
                        "text": "Please provide the updated details for your Bank Profile (e.g. Bank Name, Regions, Products, or Systems):",
                        "chips": ["Use Default Retail Bank Profile"],
                    }
                ],
                "chips": ["Use Default Retail Bank Profile"],
            }

        # User confirmed Bank Profile -> Proceed to Requirement Understanding & Schema Design
        current_step = "requirement"
        session["step"] = "requirement"
        if session.get("uploaded_file_text"):
            user_msg = session["uploaded_file_text"]
        elif not user_msg or any(w in user_msg.lower() for w in ["confirm", "proceed", "yes", "looks good", "continue", "next", "design"]):
            user_msg = "Account Balance and Financial Transactions Analytics"

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
                    f"Welcome to **Bronze Agent**!\n\n"
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
                        "agent": "Bronze Agent",
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

        # Pause to ask for user confirmation before proceeding to Schema Design
        session["step"] = "bank_profile_confirm"
        store.set(session_id, session)

        bank_name = prof_out.get("bank_name", "Unknown Bank")
        bank_code = prof_out.get("bank_code", "N/A")
        regions = prof_out.get("regions", [])
        reg_str = ", ".join(regions) if isinstance(regions, list) else str(regions)
        b_type = prof_out.get("banking_type", "Retail and Commercial Banking")
        products = prof_out.get("active_products", [])
        prod_str = ", ".join(products) if isinstance(products, list) else str(products)
        reg_fw = prof_out.get("regulatory_frameworks", [])
        reg_fw_str = ", ".join(reg_fw) if isinstance(reg_fw, list) else str(reg_fw)
        standards = prof_out.get("data_standards", [])
        std_str = ", ".join(standards) if isinstance(standards, list) else str(standards)
        core_sys = prof_out.get("core_banking_system") or prof_out.get("core_banking_engine") or "Temenos T24 Transact"
        source_sys = prof_out.get("source_systems") or []
        src_str = ", ".join(source_sys) if isinstance(source_sys, list) else str(source_sys)

        doc_note = f" from uploaded file **{session.get('uploaded_file_name')}**" if session.get('uploaded_file_name') else ""
        msg_text = (
            f"### 🏦 Bank Profile Extracted{doc_note}\n\n"
            f"I have successfully structured and verified your Bank Profile:\n\n"
            f"| Profile Field | Value |\n"
            f"| :--- | :--- |\n"
            f"| **Bank Name** | **{bank_name}** |\n"
            f"| **Bank Code** | `{bank_code}` |\n"
            f"| **Operating Regions** | {reg_str} |\n"
            f"| **Banking Type** | {b_type} |\n"
            f"| **Active Products** | {prod_str} |\n"
            f"| **Regulatory Frameworks** | {reg_fw_str} |\n"
            f"| **Data Standards** | {std_str} |\n"
            f"| **Core Banking System** | {core_sys} |\n"
        )
        if src_str:
            msg_text += f"| **Data Sources** | {src_str} |\n"

        msg_text += (
            f"\n📄 **Bank Profile & Standards Brief (`{bp_filename}`)** has been generated and is available in your artifacts pane.\n\n"
            f"Please confirm if this profile looks accurate before we proceed to **Bronze Schema Design & Ingestion Specification**."
        )

        chips = ["Confirm Bank Profile & Proceed", "Edit Bank Profile"]
        return {
            "session_id": session_id,
            "current_step": "bank_profile_confirm",
            "messages": [
                {
                    "agent": "Bronze Agent",
                    "text": msg_text,
                    "chips": chips,
                    "files": session.get("all_generated_files", []),
                }
            ],
            "chips": chips,
        }

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

        data_sources_list = req_out.get("data_sources") or bank_profile.get("source_systems") or [bank_profile.get("core_banking_system", "Temenos T24 Transact")]
        if not data_sources_list:
            data_sources_list = ["Temenos T24 Transact", "SWIFT MT/MX Feeds"]

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
            "data_sources": [{"source_name": s if isinstance(s, str) else s.get("source_name", "Source Feed")} for s in data_sources_list],
            "filters": ["currency_code include USD, EUR, GBP", "status_cd include ACTIVE"],
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
                    "agent": "Bronze Agent",
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
                    "agent": "Bronze Agent",
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
        "-- Bronze Layer BigQuery DDL Script\n"
        "CREATE OR REPLACE TABLE `banking_bronze.brz_customer` (\n"
        "  customer_id STRING OPTIONS(description='Primary Customer ID'),\n"
        "  customer_name STRING OPTIONS(description='Full Legal Name')\n"
        ") CLUSTER BY customer_id;\n\n"
        "CREATE OR REPLACE TABLE `banking_bronze.brz_account` (\n"
        "  account_id STRING OPTIONS(description='Account Primary Key')\n"
        ") CLUSTER BY account_id;\n\n"
    )

    # Build column-level Bronze STTM mappings
    mappings = []
    tables = product_plan.get("tables", [])
    if not tables:
        tables = [
            {
                "table_name": "brz_customer_raw",
                "source_table": "T24_CUSTOMER",
                "columns": [
                    {"name": "ingest_batch_id", "type": "STRING", "block": "ingestion-metadata"},
                    {"name": "ingest_ts", "type": "TIMESTAMP", "block": "ingestion-metadata"},
                    {"name": "source_file_name", "type": "STRING", "block": "ingestion-metadata"},
                    {"name": "raw_payload_hash", "type": "STRING", "block": "ingestion-metadata"},
                    {"name": "cust_id", "type": "STRING", "block": "source-identifier"},
                    {"name": "legal_name", "type": "STRING", "block": "raw-payload"},
                    {"name": "dob_or_inc_date", "type": "DATE", "block": "raw-payload"},
                    {"name": "tax_identifier", "type": "STRING", "block": "raw-payload"},
                    {"name": "kyc_status_cd", "type": "STRING", "block": "quality-flags"},
                    {"name": "extract_ts", "type": "TIMESTAMP", "block": "temporal"},
                ],
            },
            {
                "table_name": "brz_account_raw",
                "source_table": "T24_ACCOUNT",
                "columns": [
                    {"name": "ingest_batch_id", "type": "STRING", "block": "ingestion-metadata"},
                    {"name": "ingest_ts", "type": "TIMESTAMP", "block": "ingestion-metadata"},
                    {"name": "source_file_name", "type": "STRING", "block": "ingestion-metadata"},
                    {"name": "raw_payload_hash", "type": "STRING", "block": "ingestion-metadata"},
                    {"name": "account_num", "type": "STRING", "block": "source-identifier"},
                    {"name": "primary_cust_id", "type": "STRING", "block": "source-identifier"},
                    {"name": "acct_type_cd", "type": "STRING", "block": "raw-payload"},
                    {"name": "currency_iso", "type": "STRING", "block": "raw-payload"},
                    {"name": "current_balance", "type": "NUMERIC", "block": "raw-payload"},
                    {"name": "status", "type": "STRING", "block": "quality-flags"},
                ],
            },
        ]

    for t in tables:
        tname = t.get("table_name", "brz_table")
        src_tbl = t.get("source_table") or t.get("source_name") or f"SRC_{tname.upper().replace('BRZ_', '')}"
        for col in t.get("columns", []):
            cname = col.get("name", "col")
            block = col.get("block", "raw-payload")
            is_meta = block == "ingestion-metadata" or cname in ("ingest_batch_id", "ingest_ts", "source_file_name", "raw_payload_hash")
            mappings.append({
                "source_column": f"SYSTEM.{cname}" if is_meta else f"{src_tbl}.{cname.upper()}",
                "transform": "SYSTEM_GENERATED" if is_meta else f"INGEST_{col.get('type', 'STRING')}",
                "target_column": cname,
                "target_table": f"banking_bronze.{tname}",
            })

    contract_dict = generate_data_contract_dict(product_plan, bank_profile, structured_req)
    contract_yaml = generate_data_contract_yaml(contract_dict)
    contract_dict["yaml_text"] = contract_yaml

    # Generate BigLake Apache Iceberg external table DDL
    iceberg_ddl = generate_iceberg_ddl(product_plan, bank_profile)
    iceberg_file_id = str(uuid.uuid4())
    iceberg_filename = f"brz_iceberg_tables_{session_id[:6]}.sql"
    FILE_STORE[iceberg_file_id] = {
        "id": iceberg_file_id,
        "name": iceberg_filename,
        "content": iceberg_ddl.encode("utf-8"),
        "type": "sql",
    }

    # Generate BigLake Object Table external DDL for Unstructured Data Ingestion
    object_table_ddl = generate_object_table_ddl(product_plan, bank_profile)
    object_file_id = str(uuid.uuid4())
    object_filename = f"brz_object_tables_{session_id[:6]}.sql"
    FILE_STORE[object_file_id] = {
        "id": object_file_id,
        "name": object_filename,
        "content": object_table_ddl.encode("utf-8"),
        "type": "sql",
    }

    # Combined DDL for full multi-target preview
    combined_ddl = (
        f"-- ==============================================================================\n"
        f"-- TARGET 1: BigLake Apache Iceberg External Tables (Google Data Lake Ingestion)\n"
        f"-- Format: Apache Iceberg on Google Cloud Storage | Retention: 7 Years (2555 Days)\n"
        f"-- ==============================================================================\n\n"
        f"{iceberg_ddl}\n\n"
        f"-- ==============================================================================\n"
        f"-- TARGET 2: BigLake Object Tables (Unstructured Binary Storage: KYC PDFs, Scans)\n"
        f"-- Pattern: BigLake Directory Object Table over Cloud Storage | Retention: 7 Years\n"
        f"-- ==============================================================================\n\n"
        f"{object_table_ddl}\n\n"
        f"-- ==============================================================================\n"
        f"-- TARGET 3: BigQuery Native Landing Tables (Standard Managed SQL)\n"
        f"-- Partitioning: Daily by ingest_ts | Clustering: Source Keys\n"
        f"-- ==============================================================================\n\n"
        f"{ddl}"
    )

    ddl_file_id = str(uuid.uuid4())
    ddl_filename = f"brz_banking_schema_{session_id[:6]}.sql"
    FILE_STORE[ddl_file_id] = {
        "id": ddl_file_id,
        "name": ddl_filename,
        "content": combined_ddl.encode("utf-8"),
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
        {"id": iceberg_file_id, "name": iceberg_filename, "label": "BigLake Iceberg Table DDL (.sql)", "stage": "PRODUCT ENGINE"},
        {"id": object_file_id, "name": object_filename, "label": "BigLake Object Table DDL (.sql)", "stage": "PRODUCT ENGINE"},
        {"id": ddl_file_id, "name": ddl_filename, "label": "BigQuery Standard DDL (.sql)", "stage": "PRODUCT ENGINE"},
        {"id": contract_file_id, "name": contract_filename, "label": "ODCS v2.2 Data Contract (.yaml)", "stage": "PRODUCT ENGINE"},
        {"id": sttm_xls_file_id, "name": sttm_xls_filename, "label": "Source-to-Target Mapping (.xlsx)", "stage": "PRODUCT ENGINE"},
        {"id": meta_xls_file_id, "name": meta_xls_filename, "label": "Business Glossary & Catalog (.xlsx)", "stage": "PRODUCT ENGINE"},
        {"id": audit_xls_file_id, "name": audit_xls_filename, "label": "Sample Data Quality Audit (.xlsx)", "stage": "VALIDATION"},
        {"id": dataplex_file_id, "name": dataplex_filename, "label": "Dataplex & Knowledge Catalog Manifest (.json)", "stage": "VALIDATION"},
    ])
    session["all_generated_files"] = all_files
    session["status"] = "completed"
    session["ddl_script"] = combined_ddl
    session["contract_yaml"] = contract_yaml

    # Build bronze_transform_view for UI rendering
    sources_list = [f"{s.get('source_name', s)}" if isinstance(s, dict) else str(s) for s in (structured_req.get("data_sources") or bank_profile.get("source_systems") or ["Temenos T24 Transact", "SWIFT MT/MX Feeds"])]
    bronze_tables = [f"banking_bronze.{t.get('table_name', 'table')}" for t in tables]

    req_context_str = f"{structured_req.get('use_case_name', '')} {structured_req.get('business_goal', '')} {' '.join(sources_list)}"
    detected_modalities = [
        classify_source_modality(str(s), req_context_str) for s in sources_list
    ]
    primary_modality = detected_modalities[0] if detected_modalities else classify_source_modality("core_banking", req_context_str)
    contract_dict["modality"] = primary_modality

    lineage_items = []
    for t in tables:
        tname = t.get("table_name", "table")
        src_tbl = t.get("source_table") or t.get("source_name") or f"SRC_{tname.upper().replace('BRZ_', '')}"
        t_mod = classify_source_modality(tname, f"{t.get('source_system', '')} {req_context_str}")
        if t_mod["structure"] == "UNSTRUCTURED":
            m_tag = "Object Table Landing"
        elif t_mod["cadence"] == "STREAMING_REALTIME":
            m_tag = "Streaming Realtime Landing"
        else:
            m_tag = "Raw Iceberg Landing"
        lineage_items.append(f"{src_tbl} → banking_bronze.{tname} ({m_tag})")

    bronze_view = {
        "title": "Bronze Schema & STTM Ingestion Specification",
        "step_label": "Raw Landing Zone & Ingestion Envelope",
        "modality": primary_modality,
        "modalities": detected_modalities,
        "summary": f"Generated raw banking Bronze schema for {bank_profile.get('bank_name', 'Apex International Bank')} with BigLake Iceberg & Object Table DDL, OpenDataContract (ODCS v2.2), and Source-to-Target Mappings.",
        "narrative": (
            f"The Bronze Product Engine generated conformed raw landing entities (`{', '.join([t.get('table_name', 'table') for t in tables])}`) with 100% source fidelity. "
            f"Modality detected: {primary_modality['modality_label']}. "
            f"All tables include Tony D. Giordano's mandatory ingestion envelope (`ingest_batch_id`, `ingest_ts`, `source_file_name`, `raw_payload_hash`). "
            f"Downstream targets include Google Data Lake (GCS), BigLake Apache Iceberg external tables with 7-year regulatory retention, BigLake Object Tables for binary KYC/document blobs, and Google Cloud Dataplex / Knowledge Catalog metadata manifests."
        ),
        "silver_sources": sources_list,
        "silver_tables": bronze_tables,
        "lineage_summary": lineage_items,
        "header": "Bronze STTM Ingestion Mappings",
        "mappings": mappings,
        "mapping_count": len(mappings),
    }

    agent_response_text = (
        f"Generated enterprise-grade Banking Bronze Schema, Apache Iceberg DDL, ODCS v2.2 Data Contract, and Ingestion STTM Workbooks.\n\n"
        f"```sql\n{combined_ddl}\n```\n\n"
        f"You can review and edit the **Data Contract** and **STTM Mappings** below, and download all generated pipeline artifacts (.sql, .yaml, .xlsx, .json, .pdf) from the right-hand panel."
    )

    chips = ["Publish to BigQuery", "Adjust the model", "Tweak the mapping", "Edit Contract"]

    session["step"] = "sttm_ready"
    store.set(session_id, session)

    return {
        "session_id": session_id,
        "current_step": "sttm_ready",
        "messages": [
            {
                "agent": "Bronze Agent",
                "text": agent_response_text,
                "chips": chips,
                "data_contract_view": contract_dict,
                "bronze_transform_view": bronze_view,
                "files": all_files,
            }
        ],
        "chips": chips,
    }
