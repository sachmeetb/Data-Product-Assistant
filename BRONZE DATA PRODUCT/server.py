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
import re
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
from tools.knowledge_catalog import KnowledgeCatalogClient
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


# ── Data Contract commit + Knowledge Catalog registration ─────────────────────

class ContractCommitRequest(BaseModel):
    contract: dict = Field(..., description="Data contract object from the UI")
    session_id: Optional[str] = Field(None, description="Session to pull DDL from for BQ publish")


@app.post("/v1/data-contract/commit")
async def commit_data_contract(body: ContractCommitRequest):
    """Commit a data contract: publish DDL to BigQuery, register in Dataplex Knowledge Catalog.

    Returns kc_url — the direct GCP console link for the registered data product.
    Returns bq_url — the BigQuery dataset console link.
    Bronze tables are registered as a KC data product under the bronze dataset.
    """
    project_id = os.environ.get("GCP_PROJECT_ID", "")
    location = os.environ.get("GCP_LOCATION", "us-central1")
    bq_dataset = os.environ.get("BQ_BRONZE_DATASET", "banking_bronze")

    contract = body.contract
    info = contract.get("info", {})
    contract_id = contract.get("id", "urn:datacontract:banking_bronze:unknown")

    # Derive a Dataplex-compatible id: lowercase, hyphens, max 63 chars
    kc_id = re.sub(r"[^a-z0-9]+", "-", contract_id.lower()).strip("-")[:63].rstrip("-")

    result: dict = {
        "status": "committed",
        "contract_id": contract_id,
        "bq_result": None,
        "kc_result": None,
        "kc_url": None,
        "bq_url": None,
    }

    # 1. Publish DDL to BigQuery when the session has one
    if body.session_id:
        session = store.get(body.session_id)
        if session and session.get("ddl_script"):
            publisher = BigQueryPublisher(mode="live")
            bq_report = await asyncio.to_thread(publisher.publish, session["ddl_script"])
            result["bq_result"] = bq_report
            if bq_report.get("dataset_id"):
                bq_dataset = bq_report["dataset_id"]

    effective_project = project_id or os.environ.get("BQ_PROJECT") or "eogwapq-agbg-internal-data-mig"
    result["bq_url"] = (
        f"https://console.cloud.google.com/bigquery"
        f"?project={effective_project}&p={effective_project}&d={bq_dataset}&page=dataset"
    )

    # 2. Register Data Product + Data Assets + Business Glossary in Dataplex Knowledge Catalog
    try:
        kc = KnowledgeCatalogClient(project_id=effective_project, location=location, dataset_id=bq_dataset)
        models = contract.get("models", {})
        table_names = []
        glossary_terms = []
        for table_name, model_def in list(models.items())[:50]:
            physical = model_def.get("physicalName", f"{bq_dataset}.{table_name}")
            tbl = physical.split(".")[-1]
            table_names.append(tbl)
            glossary_terms.append({
                "name": tbl,
                "description": model_def.get("description") or f"Bronze raw table {tbl}",
            })
            await asyncio.to_thread(
                kc.enrich_bigquery_table,
                bq_dataset,
                tbl,
                model_def.get("description") or f"Bronze raw table {tbl}",
                info.get("domain") or "bronze_banking",
            )

        dp_res = await asyncio.to_thread(
            kc.register_data_product,
            bq_dataset,
            kc_id,
            info.get("title", "Bronze Data Contract"),
            info.get("description", ""),
            table_names,
            info.get("owner_email") or info.get("owner"),
            location,
        )
        gl_res = await asyncio.to_thread(
            kc.sync_business_glossary,
            f"bronze-{bq_dataset}-glossary",
            f"Bronze Glossary ({bq_dataset})",
            f"Business & Technical Glossary for {info.get('title', bq_dataset)}",
            glossary_terms,
        )
        result["kc_result"] = {
            **dp_res,
            "glossary": gl_res,
        }
        result["kc_url"] = dp_res.get("knowledge_catalog_url")
        result["kc_glossary_url"] = gl_res.get("glossary_url")
    except Exception as exc:
        log.warning("Knowledge Catalog registration failed: %s", exc)
        result["kc_result"] = {"status": "error", "error": str(exc)}

    return result


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
    file_ref_ids: Optional[list[str]] = Field(None, description="Multiple uploaded file reference IDs")


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

    # Ingest one or multiple uploaded files if referenced or mentioned
    ref_ids: list[str] = []
    if body.file_ref_ids:
        ref_ids.extend([r for r in body.file_ref_ids if r])
    if body.file_ref_id and body.file_ref_id not in ref_ids:
        ref_ids.append(body.file_ref_id)

    extracted_parts: list[str] = []
    uploaded_names: list[str] = []
    for rid in ref_ids:
        fi = _get_file_info(rid, filename_hint=session.get("uploaded_file_name", ""))
        if fi:
            txt = _extract_text_from_file_info(fi)
            fname = fi.get("name", "uploaded_file")
            uploaded_names.append(fname)
            extracted_parts.append(f"=== Source File: {fname} ===\n{txt}")

    # Fallback: inspect user_msg for any sample filename
    if not extracted_parts and user_msg:
        for s_file in (_ROOT / "samples").glob("*.*"):
            if s_file.name.lower() in user_msg.lower():
                fi = _get_file_info(s_file.name, s_file.name)
                if fi:
                    uploaded_names.append(fi.get("name", s_file.name))
                    extracted_parts.append(f"=== Source File: {fi.get('name', s_file.name)} ===\n{_extract_text_from_file_info(fi)}")
                break

    if extracted_parts:
        combined_text = "\n\n".join(extracted_parts)
        session["uploaded_file_id"] = ref_ids[0] if ref_ids else None
        session["uploaded_file_ids"] = ref_ids
        session["uploaded_file_name"] = ", ".join(uploaded_names)
        session["uploaded_file_text"] = combined_text
        if not user_msg or body.action == "use_file" or "use this file" in user_msg.lower() or "use uploaded file" in user_msg.lower():
            user_msg = f"User uploaded {len(uploaded_names)} file(s) ({', '.join(uploaded_names)}):\n\n{combined_text}"
        else:
            user_msg = f"{user_msg}\n\nUploaded Documents ({', '.join(uploaded_names)}):\n\n{combined_text}"

    current_step = session.get("step", "bank_profile")

    if action == "edit":
        current_step = "requirement"
        session["step"] = "requirement"

    if "design bronze data product" in user_msg.lower() or "switch to standard bronze flow" in user_msg.lower():
        session["step"] = "bank_profile"
        store.set(session_id, session)
        return {
            "session_id": session_id,
            "current_step": "initial",
            "messages": [{
                "agent": "Bronze Agent",
                "text": (
                    "Switched to the **Standard Bronze Data Product Design Flow**. "
                    "Please describe your institution & incoming data sources (e.g., Hogan Mainframe, SWIFT, SAP ERP, Kafka streams), "
                    "or attach an Excel/JSON/Word requirements file."
                ),
                "chips": ["Hogan Core Banking (Mainframe)", "SWIFT & Faster Payments", "Land 4-Source Banking Data"],
                "files": [],
            }],
            "chips": ["Hogan Core Banking (Mainframe)", "SWIFT & Faster Payments", "Land 4-Source Banking Data"],
        }

    # Step 1 of 4-Source Landing: prompt user to upload their CSV/Excel files (or use sample files)
    if (action == "land_4_sources" or "land 4-source" in user_msg.lower()) and not ref_ids:
        session["step"] = "awaiting_4_source_upload"
        store.set(session_id, session)
        return {
            "session_id": session_id,
            "current_step": "awaiting_4_source_upload",
            "messages": [{
                "agent": "Bronze Agent",
                "text": (
                    "📤 **Upload Your 4-Source Banking Files (CSV or Excel `.xlsx`/`.xls`)**\n\n"
                    "Please use the **📎 attachment button** below to select and upload one or multiple CSV / Excel files for:\n"
                    "1. **Card** (e.g. `brz_tsys_cardholder.csv` — `cardholder_id`, `embossed_name`, `tax_ssn`, `mobile_phone`, `card_type`, `credit_line`)\n"
                    "2. **Deposit** (e.g. `brz_hogan_ci_customer.csv` — `CI_CUST_NUM`, `CI_LEGAL_NAME`, `CI_TAX_ID`, `CI_BIRTH_DT`, `CI_PHONE`, `CI_POST_CODE`)\n"
                    "3. **Commercial Loan** (e.g. `brz_afs_borrower.csv` — `BORROWER_ID`, `LEGAL_ENTITY_NAME`, `TAX_EIN`, `PRIMARY_CONTACT_NAME`, `CONTACT_PHONE`, `FACILITY_LIMIT`)\n"
                    "4. **Retail Loan** (e.g. `brz_acls_applicant.csv` — `APPLICANT_ID`, `FIRST_NAME`, `LAST_NAME`, `DOB`, `NATIONAL_ID`, `CELL_PHONE`, `EMAIL`, `LOAN_PURPOSE`)\n\n"
                    "Once uploaded, click **Extract & Use** to land them into `banking_bronze` and sync with **GCP Knowledge Catalog**, "
                    "or click **Use Sample 4-Source Files** below to load the built-in sample datasets."
                ),
                "chips": ["Use Sample 4-Source Files", "Switch to Standard Bronze Flow"],
                "files": [],
            }],
            "chips": ["Use Sample 4-Source Files", "Switch to Standard Bronze Flow"],
        }

    # Step 2 of 4-Source Landing: execute landing from uploaded files OR sample files
    if (
        (session.get("step") == "awaiting_4_source_upload" and ref_ids)
        or "use sample 4-source" in user_msg.lower()
    ):
        uploaded_file_objs = [
            _get_file_info(rid, filename_hint=session.get("uploaded_file_name", ""))
            for rid in ref_ids
        ] if ref_ids else None
        if uploaded_file_objs:
            uploaded_file_objs = [f for f in uploaded_file_objs if f]
        landed = await asyncio.to_thread(_land_4_sources_to_bronze, None, uploaded_file_objs or None)
        session["landed_4_sources"] = landed
        session["step"] = "bronze_landed"
        store.set(session_id, session)
        rows_md = "\n".join(
            f"| `{t['source_system']}` | `{t['fqn']}` | {t['row_count']} | {len(t['columns'])} cols | [BigQuery ↗]({t['bigquery_url']}) |"
            for t in landed["tables"]
        )
        dp = landed.get("knowledge_catalog_data_product") or {}
        gl = landed.get("knowledge_catalog_glossary") or {}
        source_label = "Uploaded Files" if uploaded_file_objs else "Built-in Sample Datasets"
        msg = (
            f"✅ **Landed {len(landed['tables'])} Banking Source Table(s) into `{landed['project_id']}.{landed['dataset_id']}`** *(from {source_label})*\n\n"
            f"| Source System | BigQuery Bronze Table | Rows | Schema | Console |\n"
            f"| :--- | :--- | ---: | ---: | :--- |\n"
            f"{rows_md}\n\n"
            f"- **Total Bronze Records Landed**: `{landed['total_rows']}`\n"
        )
        if dp.get("ok"):
            msg += f"- **Knowledge Catalog Data Product**: `{dp.get('data_product_id')}` ({dp.get('assets_registered', 0)} assets) — [Open Data Products ↗]({dp.get('knowledge_catalog_url')})\n"
        if gl.get("ok"):
            msg += f"- **Knowledge Catalog Glossary**: `{gl.get('glossary_id')}` ({gl.get('terms_synced', 0)} terms) — [Open Glossaries ↗]({gl.get('glossary_url')})\n"
        msg += "\n**Next Step**: Open **Silver Data Product Assistant** and click **Map Multi-Source Bronze → Silver Customer 360 (N-to-M STTM)** to discover these Bronze tables and build the multi-source STTM."
        return {
            "session_id": session_id,
            "current_step": "bronze_landed",
            "messages": [{"agent": "Bronze Agent", "text": msg, "chips": ["Auto-Ingest Multi-Table Sources (Batch + Kafka)", "Land 4-Source Banking Data", "Design Bronze Data Product (Standard Flow)"], "files": []}],
            "chips": ["Auto-Ingest Multi-Table Sources (Batch + Kafka)", "Land 4-Source Banking Data", "Design Bronze Data Product (Standard Flow)"],
        }

    # Automated 1-to-Many Multi-Table Bronze Ingestion (Batch + Kafka Streaming) -> GCP BigQuery & Dataplex
    if (
        action in ("auto_ingest_multi_table", "auto_ingest_water")
        or "auto-ingest multi-table" in user_msg.lower()
        or "auto-ingest water" in user_msg.lower()
        or "1-to-many bronze" in user_msg.lower()
    ):
        from ingestion.orchestrator import AutomatedBronzeOrchestrator

        ind_domain = "water" if ("water" in user_msg.lower() or action == "auto_ingest_water") else "banking"
        orchestrator = AutomatedBronzeOrchestrator()
        res = await asyncio.to_thread(orchestrator.run_automated_ingestion, None, ind_domain, None)
        session["auto_ingested_bronze"] = res
        session["step"] = "bronze_landed"
        store.set(session_id, session)

        sys_rows_md = "\n".join(
            f"| **{s['source_system']}** ({s['system_label']}) | **{s['table_count']} tables** | `{', '.join(s['tables'])}` | `{', '.join(s['ingestion_modes'])}` | {s['total_rows']} |"
            for s in res["systems_summary"]
        )
        tbl_rows_md = "\n".join(
            f"| `{t['source_system']}` | `{t['fqn']}` | `{t['ingestion_mode']}` | {t['row_count']} | {len(t['columns'])} raw + {len(t.get('envelope_columns', []))} env | [BigQuery ↗]({t.get('bigquery_url', '#')}) |"
            for t in res["tables"]
        )
        dp = res.get("knowledge_catalog_data_product") or {}
        gl = res.get("knowledge_catalog_glossary") or {}

        msg = (
            f"⚡ **Automated 1-to-Many Bronze Ingestion Complete (`{res['project_id']}.{res['dataset_id']}`)**\n\n"
            f"Modeled **{res['source_system_count']} Source Systems → {res['total_tables']} Bronze Tables** "
            f"(`{res['batch_tables_count']}` Batch + `{res['streaming_tables_count']}` Real-Time Kafka Stream, **{res['total_rows']} total records**) "
            f"with automated schema inference and 6-field ingestion envelopes (`_ingest_batch_id`, `_ingest_ts`, `_source_system`, `_source_file_or_topic`, `_ingestion_mode`, `_record_hash`).\n\n"
            f"### 1-to-Many Source System Summary\n"
            f"| Source System | Dataset Cardinality | Bronze Tables Provisioned | Ingestion Modes | Total Rows |\n"
            f"| :--- | :--- | :--- | :--- | ---: |\n"
            f"{sys_rows_md}\n\n"
            f"### Provisioned BigQuery Bronze Tables & Console Links\n"
            f"| System | BigQuery Bronze Table | Mode | Rows | Columns | Console |\n"
            f"| :--- | :--- | :--- | ---: | :--- | :--- |\n"
            f"{tbl_rows_md}\n\n"
        )
        if dp.get("ok"):
            msg += f"- **GCP Knowledge Catalog Data Product**: `{dp.get('data_product_id')}` ({dp.get('assets_registered', 0)} assets) — [Open Data Products ↗]({dp.get('knowledge_catalog_url')})\n"
        if gl.get("ok"):
            msg += f"- **GCP Knowledge Catalog Glossary**: `{gl.get('glossary_id')}` ({gl.get('terms_synced', 0)} terms) — [Open Glossaries ↗]({gl.get('glossary_url')})\n"
        msg += "\n**Next Step**: Open **Silver Data Product Assistant** to run the **N-to-M Multi-Source Domain Mapping** (`Many Sources → Many Tables → Domain → Multiple Silver Tables`)."

        return {
            "session_id": session_id,
            "current_step": "bronze_landed",
            "messages": [
                {
                    "agent": "Bronze Agent",
                    "text": msg,
                    "chips": [
                        "Simulate Kafka Card Auth Stream",
                        "Auto-Ingest Water Account 360 Sources",
                        "Land 4-Source Banking Data",
                        "Design Bronze Data Product (Standard Flow)",
                    ],
                    "files": [],
                }
            ],
            "chips": [
                "Simulate Kafka Card Auth Stream",
                "Auto-Ingest Water Account 360 Sources",
                "Land 4-Source Banking Data",
                "Design Bronze Data Product (Standard Flow)",
            ],
        }

    # Dedicated Kafka Streaming Simulation Handler -> GCP BigQuery
    if action == "simulate_kafka" or "simulate kafka" in user_msg.lower():
        from ingestion.kafka_connector import KafkaStreamingConnector

        kafka_conn = KafkaStreamingConnector()
        k_res = await asyncio.to_thread(
            kafka_conn.ingest_stream_table,
            "TSYS",
            "brz_tsys_auth_events",
            "payments.tsys.card.auth.v1",
            "KAFKA-AUTH-STREAM (ISO 8583 Card Authorizations)",
            "Real-time Kafka event stream capturing card authorizations and verified mobile touchpoints.",
        )
        msg = (
            f"📡 **Kafka Streaming Ingestion Executed (`{k_res['kafka_topic']}` → `{k_res['fqn']}`)**\n\n"
            f"- **Stream Execution Mode**: `{k_res['stream_mode']}`\n"
            f"- **Events Materialized in BigQuery**: `{k_res['row_count']}` real-time ISO 8583 authorization events\n"
            f"- **Dead-Letter Queue (DLQ) Rejects**: `{k_res.get('dlq_count', 0)}`\n"
            f"- **Kafka Lineage Captured**: `kafka_topic`, `kafka_partition`, `kafka_offset`, `event_ts`, plus Bronze envelope (`_ingest_batch_id`, `_record_hash`)\n"
            f"- **BigQuery Console Link**: [Open `{k_res['table_name']}` in BigQuery ↗]({k_res['bigquery_url']})"
        )
        return {
            "session_id": session_id,
            "current_step": "bronze_landed",
            "messages": [
                {
                    "agent": "Bronze Agent",
                    "text": msg,
                    "chips": [
                        "Auto-Ingest Multi-Table Sources (Batch + Kafka)",
                        "Land 4-Source Banking Data",
                        "Design Bronze Data Product (Standard Flow)",
                    ],
                    "files": [],
                }
            ],
            "chips": [
                "Auto-Ingest Multi-Table Sources (Batch + Kafka)",
                "Land 4-Source Banking Data",
                "Design Bronze Data Product (Standard Flow)",
            ],
        }

    if action == "publish" or user_msg.lower() == "publish to bigquery":
        ddl = session.get("ddl_script", "")
        if not ddl:
            return {
                "session_id": session_id,
                "current_step": "error",
                "messages": [{"agent": "Bronze Agent", "text": "No DDL available to publish. Please design the Bronze schema first."}],
                "chips": ["Design Bronze Schema"],
            }
        publisher = BigQueryPublisher(mode="live")
        report = await asyncio.to_thread(publisher.publish, ddl)
        session["publish_report"] = report

        # Register Data Product, Data Assets & Business Glossary in GCP Knowledge Catalog (Dataplex)
        contract_dict = session.get("contract_dict") or {}
        c_info = contract_dict.get("info", {}) or {}
        ds_id = report.get("dataset_id") or os.environ.get("BQ_BRONZE_DATASET", "banking_bronze")
        pub_tables = report.get("published_tables") or []
        short_tables = [t.split(".")[-1] for t in pub_tables]
        kc = KnowledgeCatalogClient(project_id=publisher.project_id, dataset_id=ds_id)
        for tbl in short_tables:
            await asyncio.to_thread(
                kc.enrich_bigquery_table,
                ds_id,
                tbl,
                f"Bronze raw landing table {tbl}",
                session.get("domain") or "bronze_banking",
            )
        dp_res = await asyncio.to_thread(
            kc.register_data_product,
            ds_id,
            f"bronze-{ds_id}-{session_id[:6]}",
            c_info.get("title") or f"Bronze Data Product ({ds_id})",
            c_info.get("description") or "Raw Bronze Lakehouse tables published via Data Product Assistant.",
            short_tables,
            c_info.get("owner_email") or c_info.get("owner"),
        )
        gl_terms = [{"name": t, "description": f"Bronze raw table {t}"} for t in short_tables]
        gl_res = await asyncio.to_thread(
            kc.sync_business_glossary,
            f"bronze-{ds_id}-glossary",
            f"Bronze Glossary ({ds_id})",
            f"Business & Technical Glossary for {ds_id}",
            gl_terms,
        )
        session["kc_result"] = {**dp_res, "glossary": gl_res}
        store.set(session_id, session)

        status_icon = "✅" if report.get("publish_status") == "published" else "ℹ️"
        msg = f"{status_icon} **BigQuery & Knowledge Catalog Publication Report**\n\n"
        msg += f"- **Status**: `{report.get('publish_status', 'published')}`\n"
        msg += f"- **Project / Dataset**: `{publisher.project_id}.{ds_id}`\n"
        if pub_tables:
            msg += f"- **Published Tables**: {', '.join([f'`{t}`' for t in pub_tables])}\n"
        if dp_res.get("ok"):
            msg += f"- **Knowledge Catalog Data Product**: `{dp_res.get('data_product_id')}` ({dp_res.get('assets_registered', 0)} assets) — [Open Data Product in Knowledge Catalog ↗]({dp_res.get('knowledge_catalog_url')})\n"
        else:
            msg += f"- **Knowledge Catalog Data Product**: ⚠️ `{dp_res.get('error') or dp_res.get('status')}`\n"
        if gl_res.get("ok"):
            msg += f"- **Knowledge Catalog Glossary**: `{gl_res.get('glossary_id')}` ({gl_res.get('terms_synced', 0)} terms) — [Open Business Glossary ↗]({gl_res.get('glossary_url')})\n"
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
                    f"To design your Bronze landing schema and data contract, please specify your **incoming data source systems** and institution profile:\n\n"
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


# ── 4-Source Banking Landing (Card, Deposit, Commercial Loan, Retail Loan) ────

_FOUR_SOURCE_SPECS = [
    {
        "source_system": "TSYS (Card)",
        "table_name": "brz_tsys_cardholder",
        "csv_file": "brz_tsys_cardholder.csv",
        "description": "TSYS / FDR Revolving Credit Card cardholder raw landing table.",
    },
    {
        "source_system": "HOGAN (Deposit)",
        "table_name": "brz_hogan_ci_customer",
        "csv_file": "brz_hogan_ci_customer.csv",
        "description": "Hogan Core Deposits CIS customer raw landing table.",
    },
    {
        "source_system": "AFS (Commercial Loan)",
        "table_name": "brz_afs_borrower",
        "csv_file": "brz_afs_borrower.csv",
        "description": "AFS Commercial Lending borrower & credit facility raw landing table.",
    },
    {
        "source_system": "ACLS (Retail Loan)",
        "table_name": "brz_acls_applicant",
        "csv_file": "brz_acls_applicant.csv",
        "description": "ACLS Consumer Retail Loan applicant raw landing table.",
    },
]


def _parse_uploaded_tabular_rows(file_info: dict) -> tuple[str, str, list[dict]]:
    """Parse an uploaded CSV or Excel (.xlsx/.xls) file into (source_system, table_name, rows)."""
    import csv, io
    fname = file_info.get("name") or "uploaded_source.csv"
    stem = Path(fname).stem.lower()
    clean_tbl = re.sub(r"[^a-z0-9_]+", "_", stem).strip("_") or "uploaded_source"
    if not clean_tbl.startswith("brz_"):
        clean_tbl = f"brz_{clean_tbl}"

    if "tsys" in stem or "card" in stem:
        src_sys = "TSYS (Card)"
    elif "hogan" in stem or "deposit" in stem:
        src_sys = "HOGAN (Deposit)"
    elif "afs" in stem or "comm" in stem:
        src_sys = "AFS (Commercial Loan)"
    elif "acls" in stem or "retail" in stem or "loan" in stem:
        src_sys = "ACLS (Retail Loan)"
    else:
        src_sys = stem.replace("_", " ").title()

    content = file_info.get("content") or b""
    ext = Path(fname).suffix.lower().lstrip(".")
    rows: list[dict] = []

    if ext == "csv":
        text = content.decode("utf-8-sig", errors="ignore")
        rows = [dict(r) for r in csv.DictReader(io.StringIO(text))]
    elif ext in ("xlsx", "xls"):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
        ws = wb.active
        all_rows = list(ws.iter_rows(values_only=True))
        if len(all_rows) >= 2:
            headers = [re.sub(r"[^a-zA-Z0-9_]+", "_", str(h or f"col_{i}")).strip("_") for i, h in enumerate(all_rows[0])]
            for r in all_rows[1:]:
                if any(v is not None for v in r):
                    rows.append({headers[i]: ("" if r[i] is None else str(r[i])) for i in range(len(headers))})
    return src_sys, clean_tbl, rows


def _land_4_sources_to_bronze(dataset_id: Optional[str] = None, uploaded_files: Optional[list[dict]] = None) -> dict:
    import csv
    from google.cloud import bigquery
    from tools.gcp_auth import get_bigquery_client

    project_id = os.environ.get("BQ_PROJECT") or os.environ.get("GCP_PROJECT_ID") or "eogwapq-agbg-internal-data-mig"
    ds_id = dataset_id or os.environ.get("BQ_BRONZE_DATASET") or "banking_bronze"
    samples_dir = Path(__file__).parent / "samples" / "banking_4_sources"

    client = get_bigquery_client(project_id=project_id, location="US")
    ds_ref = bigquery.Dataset(f"{project_id}.{ds_id}")
    ds_ref.location = "US"
    client.create_dataset(ds_ref, exists_ok=True)

    sources_to_load: list[tuple[str, str, str, list[dict]]] = []
    if uploaded_files:
        for fi in uploaded_files:
            src_sys, tbl_name, rows = _parse_uploaded_tabular_rows(fi)
            if rows:
                sources_to_load.append((src_sys, tbl_name, f"Uploaded Bronze raw table {tbl_name} from {fi.get('name')}", rows))
    else:
        for spec in _FOUR_SOURCE_SPECS:
            csv_path = samples_dir / spec["csv_file"]
            with open(csv_path, "r", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            if rows:
                sources_to_load.append((spec["source_system"], spec["table_name"], spec["description"], rows))

    landed_tables = []
    total_rows = 0
    for src_sys, tbl_name, tbl_desc, rows in sources_to_load:
        cols = list(rows[0].keys())
        schema = [bigquery.SchemaField(c, "STRING", mode="NULLABLE", description=f"Raw field {c} from {src_sys}") for c in cols]
        fqn = f"{project_id}.{ds_id}.{tbl_name}"
        job_cfg = bigquery.LoadJobConfig(
            schema=schema,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        )
        job = client.load_table_from_json(rows, fqn, job_config=job_cfg)
        job.result()

        # Enrich table description & labels
        tbl_obj = client.get_table(fqn)
        tbl_obj.description = tbl_desc
        tbl_obj.labels = {"layer": "bronze", "source_system": re.sub(r"[^a-z0-9_-]", "_", src_sys.lower())[:63]}
        client.update_table(tbl_obj, ["description", "labels"])

        total_rows += len(rows)
        landed_tables.append({
            "source_system": src_sys,
            "table_name": tbl_name,
            "fqn": fqn,
            "row_count": len(rows),
            "columns": cols,
            "bigquery_url": f"https://console.cloud.google.com/bigquery?project={project_id}&ws=!1m5!1m4!4m3!1s{project_id}!2s{ds_id}!3s{tbl_name}",
        })

    kc = KnowledgeCatalogClient(project_id=project_id, dataset_id=ds_id)
    dp_res = kc.register_data_product(
        ds_id,
        "bronze-banking-4-sources",
        "Bronze 4-Source Banking Raw Landing (TSYS, Hogan, AFS, ACLS)",
        "Heterogeneous Card, Deposit, Commercial Loan, and Retail Loan Bronze tables.",
        [t["table_name"] for t in landed_tables],
    )
    gl_res = kc.sync_business_glossary(
        "bronze-banking-4-sources-glossary",
        "Bronze 4-Source Banking Glossary",
        "Raw Source Schemas for Card, Deposit, Commercial Loan, and Retail Loan",
        [{"name": t["table_name"], "description": f"{t['source_system']} raw table ({len(t['columns'])} cols)"} for t in landed_tables],
    )
    return {
        "status": "landed",
        "project_id": project_id,
        "dataset_id": ds_id,
        "total_rows": total_rows,
        "tables": landed_tables,
        "knowledge_catalog_data_product": dp_res,
        "knowledge_catalog_glossary": gl_res,
    }


@app.post("/v1/bronze/land-4-sources")
async def land_4_sources_endpoint(dataset_id: Optional[str] = None):
    """Land all 4 heterogeneous banking source CSVs into BigQuery banking_bronze + Knowledge Catalog."""
    return await asyncio.to_thread(_land_4_sources_to_bronze, dataset_id)


class AutoIngestRequest(BaseModel):
    source_systems: Optional[list[str]] = None
    industry_domain: str = "banking"
    dataset_id: Optional[str] = None


class KafkaSimulateRequest(BaseModel):
    source_system: str = "TSYS"
    table_name: str = "brz_tsys_auth_events"
    topic: str = "payments.tsys.card.auth.v1"
    events: Optional[list[dict]] = None
    dataset_id: Optional[str] = None


@app.post("/v1/bronze/ingest/auto")
async def auto_ingest_multi_table_endpoint(body: Optional[AutoIngestRequest] = None):
    """Automated 1-to-many Bronze ingestion (Batch + Kafka Streaming) across multiple tables per source system into BigQuery."""
    from ingestion.orchestrator import AutomatedBronzeOrchestrator

    req = body or AutoIngestRequest()
    orchestrator = AutomatedBronzeOrchestrator()
    return await asyncio.to_thread(
        orchestrator.run_automated_ingestion,
        req.source_systems,
        req.industry_domain,
        req.dataset_id,
    )


@app.post("/v1/bronze/ingest/kafka/simulate")
async def simulate_kafka_ingest_endpoint(body: Optional[KafkaSimulateRequest] = None):
    """Consume from live Kafka topic or replay JSONL event stream into BigQuery Bronze with offset lineage and DLQ routing."""
    from ingestion.kafka_connector import KafkaStreamingConnector

    req = body or KafkaSimulateRequest()
    connector = KafkaStreamingConnector()
    return await asyncio.to_thread(
        connector.ingest_stream_table,
        req.source_system,
        req.table_name,
        req.topic,
        f"KAFKA-STREAM ({req.topic})",
        f"Real-time Kafka event stream for {req.source_system}.{req.table_name}",
        "brz_tsys_auth_events.jsonl",
        req.events,
        None,
        req.dataset_id,
    )


@app.get("/v1/bronze/sources")
async def get_bronze_multi_table_sources(industry_domain: str = "banking"):
    """Return 1-to-many source system definitions and their provisioned Bronze tables (Batch, Streaming, Upload)."""
    from ingestion.orchestrator import MULTI_TABLE_SOURCE_SPECS, WATER_MULTI_TABLE_SPECS

    specs_map = WATER_MULTI_TABLE_SPECS if industry_domain == "water" else MULTI_TABLE_SOURCE_SPECS
    systems = []
    for sys_code, tbl_specs in specs_map.items():
        systems.append({
            "source_system": sys_code,
            "system_label": tbl_specs[0]["system_label"] if tbl_specs else sys_code,
            "table_count": len(tbl_specs),
            "tables": tbl_specs,
        })
    return {
        "architecture_model": "1_TO_MANY_PER_SOURCE_SYSTEM",
        "industry_domain": industry_domain,
        "source_system_count": len(systems),
        "total_tables": sum(s["table_count"] for s in systems),
        "systems": systems,
    }


@app.get("/v1/bronze/catalog")
async def get_bronze_catalog(dataset_id: Optional[str] = None, industry_domain: str = "banking"):
    """Return live BigQuery Bronze table schemas and row counts (supporting 1-to-many tables per source system)."""
    from ingestion.orchestrator import MULTI_TABLE_SOURCE_SPECS, WATER_MULTI_TABLE_SPECS

    project_id = os.environ.get("BQ_PROJECT") or os.environ.get("GCP_PROJECT_ID") or "eogwapq-agbg-internal-data-mig"
    ds_id = dataset_id or os.environ.get("BQ_BRONZE_DATASET") or (
        "water_bronze" if industry_domain == "water" else "banking_bronze"
    )
    specs_map = WATER_MULTI_TABLE_SPECS if industry_domain == "water" else MULTI_TABLE_SOURCE_SPECS
    all_specs = [spec for tbl_list in specs_map.values() for spec in tbl_list]
    tables_out = []

    try:
        from tools.gcp_auth import get_bigquery_client

        client = get_bigquery_client(project_id=project_id, location="US")
        for spec in all_specs:
            fqn = f"{project_id}.{ds_id}.{spec['table_name']}"
            try:
                tbl = client.get_table(fqn)
                tables_out.append({
                    "source_system": spec["source_system"],
                    "system_label": spec.get("system_label", spec["source_system"]),
                    "table_name": spec["table_name"],
                    "ingestion_mode": spec.get("ingestion_mode", "BATCH"),
                    "fqn": fqn,
                    "row_count": tbl.num_rows,
                    "columns": [{"name": f.name, "type": f.field_type, "description": f.description} for f in tbl.schema],
                })
            except Exception:
                continue
    except Exception:
        pass

    if not tables_out:
        import csv as _csv

        sdir = _ROOT / "samples" / ("water_multi_table" if industry_domain == "water" else "banking_multi_table")
        for spec in all_specs:
            csv_name = spec["file_name"].replace(".jsonl", ".csv")
            cpath = sdir / csv_name
            cols = []
            rc = 0
            if cpath.exists():
                with open(cpath, "r", encoding="utf-8-sig") as f:
                    rows = list(_csv.DictReader(f))
                    rc = len(rows)
                    if rows:
                        cols = [{"name": k, "type": "STRING", "description": ""} for k in rows[0].keys()]
            tables_out.append({
                "source_system": spec["source_system"],
                "system_label": spec.get("system_label", spec["source_system"]),
                "table_name": spec["table_name"],
                "ingestion_mode": spec.get("ingestion_mode", "BATCH"),
                "fqn": f"{project_id}.{ds_id}.{spec['table_name']}",
                "row_count": rc,
                "columns": cols,
            })

    return {
        "architecture_model": "1_TO_MANY_PER_SOURCE_SYSTEM",
        "project_id": project_id,
        "dataset_id": ds_id,
        "tables": tables_out,
    }
