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
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

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
    generate_airflow_dag_code,
    summarize_dbt_dq_suite,
    generate_dbt_schema_yaml,
    generate_dbt_models_sql,
)

from tools import schema_loader
from tools.schema_loader import (
    get_industries,
    get_industry,
    get_domains_for_industry,
    get_domain_by_name,
    get_navigator_tree,
    get_source_models_catalog,
    parse_external_entity_spec,
    _normalize_entity_list,
)

log = logging.getLogger(__name__)
logging.basicConfig(
    level=os.environ.get("AGENT_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Silver Data Product Assistant API",
    description=(
        "Enterprise Multi-Industry Silver-layer canonical schema generator powered by "
        "Google ADK + Vertex AI Gemini + BigQuery"
    ),
    version="2.0.0",
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
        "service": "Silver-Data-Product-Assistant",
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
    """Publish the generated Silver DDL to BigQuery and synchronize metadata & dbt DQ with GCP Knowledge Catalog."""
    session = _get_session(session_id)
    pub_out = await _execute_silver_publish_and_catalog_sync(session, mode=body.mode)
    return {
        "session_id": session_id,
        **pub_out["publish_view"],
        "files": pub_out["all_files"],
    }


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
    selected_entities: Optional[list[str]] = Field(None, description="Explicit list of selected entity table names")
    selected_attributes: Optional[dict[str, list[str]]] = Field(None, description="Map of table -> selected column names")
    domain_name: Optional[str] = Field(None, description="Selected domain name override")
    industry_id: Optional[str] = Field(None, description="Selected industry ID override")
    contract: Optional[dict] = Field(None, description="Edited Silver Data Contract dictionary when saving/applying contract")
    glossary: Optional[dict] = Field(None, description="Edited Business Glossary & Entity Attributes dictionary")


class NavigatorSelectRequest(BaseModel):
    session_id: Optional[str] = Field(None, description="Active session ID")
    industry_id: str = Field(default="banking", description="Selected industry ID")
    domain_name: Optional[str] = Field(None, description="Selected domain name or ID")
    selection_mode: str = Field(default="all", description="all | single | multiple | edit_existing")
    selected_entities: list[str] = Field(default_factory=list, description="Selected entity table names")
    selected_attributes: dict[str, list[str]] = Field(default_factory=dict, description="Map of table_name -> list of column names")


class AddEntityRequest(BaseModel):
    entity_name: Optional[str] = Field(None, description="Entity table name (e.g., slv_branch or custom name)")
    table_name: Optional[str] = Field(None, description="Alias for entity_name")
    from_catalog: bool = Field(default=False, description="True if adding an existing canonical entity from the domain catalog")
    mode: Optional[str] = Field(None, description="canonical | custom")
    domain_name: Optional[str] = Field(None, description="Domain name when adding from catalog")
    entity_type: str = Field(default="dimension", description="dimension | event | aggregate | bridge")
    scd_type: Optional[str] = Field("Type 2", description="SCD Type 1 | Type 2 | Append-Only")
    grain: str = Field(default="one row per conformed record", description="Entity grain statement")
    description: str = Field(default="Conformed Silver Entity", description="Business description")
    columns: Optional[list[dict]] = Field(None, description="Custom column definitions")


class ImportEntityRequest(BaseModel):
    source_type: str = Field(default="source_catalog", description="source_catalog | ddl | odcs")
    source_model_id: Optional[str] = Field(None, description="Source model ID from Bronze/Canonical catalog")
    payload: str = Field(default="", description="Raw SQL DDL or ODCS YAML/JSON string")
    raw_content: Optional[str] = Field(None, description="Alias for payload")
    entity_name: Optional[str] = Field(None, description="Target Silver table name override")
    target_table_name: Optional[str] = Field(None, description="Alias for entity_name")
    description: Optional[str] = Field(None, description="Target description override")


class SaveContractRequest(BaseModel):
    contract: dict = Field(..., description="Updated Data Contract dictionary from UI")


class SaveGlossaryRequest(BaseModel):
    glossary: dict = Field(..., description="Updated Business Glossary and Entity Attributes dictionary from UI")
    requirement_updates: Optional[dict] = Field(None, description="Optional requirement metadata updates")


def _build_domain_entities_for_session(session: dict) -> tuple[dict, list[dict], list[dict]]:
    """
    Resolve the active canonical domain for a session, returning:
      (canonical_domain_dict, all_domain_entities_list, active_selected_entities_list)
    """
    domain_name = (
        session.get("domain")
        or session.get("requirement", {}).get("domain")
        or session.get("requirement", {}).get("banking_domain")
        or "Core Banking & Accounts"
    )
    canonical_domain = get_domain_by_name(domain_name) or get_domain_by_name("core_banking") or {}
    all_entities = list(canonical_domain.get("entities_list") or _normalize_entity_list(canonical_domain.get("entities", {})))

    # Merge any custom or imported entities stored in the session
    custom_entities = session.get("custom_entities", [])
    existing_names = {e["table_name"] for e in all_entities}
    for ce in custom_entities:
        tname = ce.get("table_name") or ce.get("name")
        if tname and tname not in existing_names:
            all_entities.append(ce)
            existing_names.add(tname)
        elif tname:
            all_entities = [ce if e["table_name"] == tname else e for e in all_entities]

    selected_names = session.get("selected_entities")
    if selected_names and isinstance(selected_names, list) and len(selected_names) > 0:
        allowed = {str(n).strip().lower() for n in selected_names}
        active_entities = [e for e in all_entities if e["table_name"].lower() in allowed]
        if not active_entities:
            active_entities = list(all_entities)
    else:
        active_entities = list(all_entities)

    return canonical_domain, all_entities, active_entities


def _classify_column_role(col_def: dict) -> tuple[str, str]:
    """Return (role_label, badge_color) for a column definition."""
    cname = str(col_def.get("name") or col_def.get("column_name") or "").lower()
    sql_type = str(col_def.get("data_type") or col_def.get("type") or "STRING").upper()
    is_pk = bool(col_def.get("is_pk") or col_def.get("is_primary_key") or col_def.get("primary_key"))
    fk_ref = col_def.get("fk_ref") or col_def.get("references")
    is_pii = bool(col_def.get("pii") or col_def.get("is_pii"))
    explicit_role = str(col_def.get("role") or col_def.get("type_label") or "").upper().strip()

    color_map = {
        "PK": "purple",
        "FK": "indigo",
        "PII": "amber",
        "KPI": "green",
        "ATTRIBUTE": "blue",
    }
    if explicit_role in color_map:
        return explicit_role, color_map[explicit_role]
    if is_pk:
        return "PK", "purple"
    if fk_ref:
        return "FK", "indigo"
    if is_pii:
        return "PII", "amber"
    if sql_type in ("NUMERIC", "DECIMAL", "FLOAT64") or any(
        k in cname
        for k in (
            "amount", "amt", "balance", "rate", "pct", "score", "ratio",
            "total", "limit", "fee", "cost", "price", "qty", "count",
            "ppm", "rlu", "kwh", "kmh", "mpy", "ms", "days", "minutes",
        )
    ):
        return "KPI", "green"
    return "ATTRIBUTE", "blue"


def _build_dynamic_glossary_for_session(session: dict) -> tuple[dict, dict]:
    """
    Build dynamic entity-scoped `requirement_data` and `glossary` structures
    directly from the session's active selected entities and their real catalog columns.
    """
    canonical_domain, all_entities, active_entities = _build_domain_entities_for_session(session)
    ind_id = session.get("industry") or canonical_domain.get("industry_id") or "banking"
    domain_title = canonical_domain.get("display_name") or session.get("domain") or "Core Banking & Accounts"
    standards = canonical_domain.get("standards") or ["BIAN v12", "ISO 20022"]
    selected_attrs_map = session.get("selected_attributes") or {}

    referenced_keys = {
        (c.get("fk_ref") or c.get("references"))
        for e in all_entities
        for c in e.get("columns", [])
        if c.get("fk_ref") or c.get("references")
    }

    entity_groups: list[dict] = []
    flat_entries: list[dict] = []
    data_points: list[dict] = []

    for ent in active_entities:
        tname = ent["table_name"]
        allowed_c = selected_attrs_map.get(tname)
        ent_attrs: list[dict] = []

        for c in ent.get("columns", []):
            cname = c.get("name") or c.get("column_name")
            if not cname:
                continue
            fk_ref = c.get("fk_ref") or c.get("references")
            is_natural_pk = (not c.get("nullable", True)) and cname.endswith("_id") and not fk_ref
            is_pk = bool(
                c.get("is_pk")
                or c.get("is_primary_key")
                or c.get("primary_key")
                or f"{tname}.{cname}" in referenced_keys
                or is_natural_pk
            )
            if c.get("included") is False and not (is_pk or fk_ref):
                continue
            if allowed_c and cname not in allowed_c and not is_pk and not fk_ref:
                continue

            sql_type = _sql_type_from_contract_type(c.get("data_type") or c.get("type") or "STRING")
            role, type_color = _classify_column_role({**c, "is_pk": is_pk})
            is_pii = bool(c.get("pii") or c.get("is_pii") or role == "PII")
            nullable = False if is_pk else bool(c.get("nullable", True))
            desc = c.get("description") or f"Conformed {cname} ({', '.join(standards)} aligned)."

            attr_entry = {
                "id": f"{tname}.{cname}",
                "entity_name": tname,
                "entity_type": ent.get("type", "dimension"),
                "entity_description": ent.get("description", ""),
                "name": cname,
                "column_name": cname,
                "qualified_name": f"{tname}.{cname}",
                "type_label": role,
                "role": role,
                "type_color": type_color,
                "sql_type": sql_type,
                "data_type": sql_type,
                "is_pk": is_pk,
                "fk_ref": fk_ref,
                "pii": is_pii,
                "nullable": nullable,
                "locked_key": bool(is_pk or fk_ref),
                "included": True,
                "description": desc,
            }
            ent_attrs.append(attr_entry)
            flat_entries.append(attr_entry)
            data_points.append({
                "entity_name": tname,
                "name": cname,
                "qualified_name": f"{tname}.{cname}",
                "kind": "kpi" if role == "KPI" else "attribute",
                "role": role,
                "sql_type": sql_type,
                "description": desc,
            })

        entity_groups.append({
            "table_name": tname,
            "entity_type": ent.get("type", "dimension"),
            "grain": ent.get("grain", "one row per conformed record"),
            "description": ent.get("description", f"Conformed Silver entity {tname}"),
            "column_count": len(ent_attrs),
            "attributes": ent_attrs,
        })

    prior_req_data = session.get("requirement_data") or {}
    req_data = {
        "use_case_name": prior_req_data.get("use_case_name") or (
            f"{domain_title} — {active_entities[0]['table_name']} Silver Model"
            if len(active_entities) == 1
            else f"{domain_title} Silver Data Model ({len(active_entities)} Entities)"
        ),
        "domain": domain_title,
        "industry": ind_id,
        "consumer_role": prior_req_data.get("consumer_role") or "Analytics & Governance Team",
        "data_freshness": prior_req_data.get("data_freshness") or "daily",
        "selected_entities": [e["table_name"] for e in active_entities],
        "data_points": data_points,
        "granularity": prior_req_data.get("granularity") or [
            {"dimension": e["table_name"].replace("slv_", "").replace("_", " ").title()}
            for e in active_entities
        ],
        "data_sources": prior_req_data.get("data_sources") or [
            {"source_name": f"Raw {ind_id.replace('_', ' ').title()} Ingestion Stream"}
        ],
        "filters": prior_req_data.get("filters") or ["status include ACTIVE"],
    }

    glossary = {
        "domain_name": domain_title,
        "standards": standards,
        "entity_count": len(entity_groups),
        "column_count": len(flat_entries),
        "selected_entities": [e["table_name"] for e in active_entities],
        "entities": entity_groups,
        "entries": flat_entries,
    }

    session["requirement_data"] = req_data
    session["glossary"] = glossary
    session["requirement"] = {
        **(session.get("requirement") or {}),
        "use_case_name": req_data["use_case_name"],
        "domain": domain_title,
        "industry": ind_id,
        "selected_entities": [e["table_name"] for e in active_entities],
        "data_points": [dp["name"] for dp in data_points],
    }
    return req_data, glossary


def _apply_glossary_edits_to_session(
    session: dict,
    glossary_payload: Optional[dict] = None,
    requirement_updates: Optional[dict] = None,
    raw_text: Optional[str] = None,
) -> tuple[dict, dict]:
    """
    Apply user edits from BusinessGlossaryCard or EditRequirementsForm directly onto
    the session's active entity definitions (`session["custom_entities"]`) so that
    Stage 2 glossary, Stage 3A Data Contract, and Stage 3B DDL/STTM stay 100% in sync.
    """
    _, all_entities, active_entities = _build_domain_entities_for_session(session)
    active_by_name = {e["table_name"]: dict(e) for e in active_entities}
    referenced_keys = {
        (c.get("fk_ref") or c.get("references"))
        for e in all_entities
        for c in e.get("columns", [])
        if c.get("fk_ref") or c.get("references")
    }

    # 1. Apply structured glossary_payload (from BusinessGlossaryCard or EditRequirementsForm)
    if glossary_payload and isinstance(glossary_payload, dict):
        # Build per-entity attribute lists from either `entities` or `entries`
        grouped_incoming: dict[str, list[dict]] = {}
        if isinstance(glossary_payload.get("entities"), list) and glossary_payload["entities"]:
            for eg in glossary_payload["entities"]:
                if not isinstance(eg, dict):
                    continue
                tname = eg.get("table_name") or eg.get("entity_name")
                if tname and isinstance(eg.get("attributes"), list):
                    grouped_incoming[tname] = eg["attributes"]
        elif isinstance(glossary_payload.get("entries"), list) and glossary_payload["entries"]:
            for entry in glossary_payload["entries"]:
                if not isinstance(entry, dict):
                    continue
                tname = entry.get("entity_name")
                if tname:
                    grouped_incoming.setdefault(tname, []).append(entry)
                else:
                    # Fallback: match by column name across active entities
                    cname = entry.get("name") or entry.get("column_name")
                    for act_tname, act_ent in active_by_name.items():
                        if any((c.get("name") or c.get("column_name")) == cname for c in act_ent.get("columns", [])):
                            grouped_incoming.setdefault(act_tname, []).append(entry)

        for tname, incoming_attrs in grouped_incoming.items():
            if tname not in active_by_name:
                continue
            orig_ent = active_by_name[tname]
            orig_cols = orig_ent.get("columns", [])
            orig_by_name = {(c.get("name") or c.get("column_name")): c for c in orig_cols}

            new_cols: list[dict] = []
            seen_names: set[str] = set()

            for attr in incoming_attrs:
                cname = (attr.get("column_name") or attr.get("name") or "").strip()
                orig_name = (attr.get("original_name") or cname).strip()
                if not cname:
                    continue
                orig_c = orig_by_name.get(orig_name) or orig_by_name.get(cname) or {}
                fk_ref = orig_c.get("fk_ref") or orig_c.get("references") or attr.get("fk_ref")
                is_natural_pk = (not orig_c.get("nullable", True)) and (orig_name or cname).endswith("_id") and not fk_ref and bool(orig_c)
                is_pk = bool(
                    orig_c.get("is_pk")
                    or orig_c.get("is_primary_key")
                    or attr.get("is_pk")
                    or f"{tname}.{orig_name or cname}" in referenced_keys
                    or is_natural_pk
                )
                locked_key = bool(is_pk or fk_ref)

                # Guardrail 2: Never drop PK/FK columns even if included=False was toggled
                if attr.get("included") is False and not locked_key:
                    continue

                # Preserve original column name on locked PK/FK to protect relational integrity
                final_cname = orig_name if (locked_key and orig_name in orig_by_name) else cname
                sql_type = _sql_type_from_contract_type(
                    attr.get("sql_type") or attr.get("data_type") or orig_c.get("data_type") or "STRING"
                )
                role = str(attr.get("role") or attr.get("type_label") or "").upper().strip()
                is_pii = bool(attr.get("pii") or role == "PII")
                nullable = False if is_pk else bool(attr.get("nullable", orig_c.get("nullable", True)))
                desc = (attr.get("description") or orig_c.get("description") or f"Conformed attribute {final_cname}").strip()

                new_cols.append({
                    "name": final_cname,
                    "column_name": final_cname,
                    "data_type": sql_type,
                    "type": sql_type,
                    "is_pk": is_pk,
                    "is_primary_key": is_pk,
                    "primary_key": is_pk,
                    "fk_ref": fk_ref,
                    "references": fk_ref,
                    "pii": is_pii,
                    "is_pii": is_pii,
                    "nullable": nullable,
                    "role": role if role in ("PK", "FK", "PII", "KPI", "ATTRIBUTE") else None,
                    "description": desc,
                })
                seen_names.add(final_cname)
                if orig_name:
                    seen_names.add(orig_name)

            # Ensure any original PK/FK columns that were omitted from partial payload remain intact
            for orig_c in orig_cols:
                oc_name = orig_c.get("name") or orig_c.get("column_name")
                is_key = bool(orig_c.get("is_pk") or orig_c.get("is_primary_key") or orig_c.get("fk_ref") or orig_c.get("references"))
                if is_key and oc_name not in seen_names:
                    new_cols.insert(0 if orig_c.get("is_pk") else len(new_cols), dict(orig_c))
                    seen_names.add(oc_name)

            updated_ent = dict(orig_ent)
            updated_ent["columns"] = new_cols
            updated_ent["column_count"] = len(new_cols)
            active_by_name[tname] = updated_ent

    # 2. Apply requirement metadata updates (from EditRequirementsForm or requirement_updates dict)
    req_data = dict(session.get("requirement_data") or {})
    if requirement_updates and isinstance(requirement_updates, dict):
        if requirement_updates.get("use_case_name"):
            req_data["use_case_name"] = requirement_updates["use_case_name"]
        if requirement_updates.get("consumer_role"):
            req_data["consumer_role"] = requirement_updates["consumer_role"]
        if requirement_updates.get("data_freshness"):
            req_data["data_freshness"] = requirement_updates["data_freshness"]

    if raw_text:
        for line in raw_text.splitlines():
            line_s = line.strip().lstrip("-").strip()
            if line_s.startswith("Use Case Name:"):
                req_data["use_case_name"] = line_s.split(":", 1)[1].strip()
            elif line_s.startswith("Consumer Role:"):
                req_data["consumer_role"] = line_s.split(":", 1)[1].strip()
            elif line_s.startswith("Data Freshness:"):
                req_data["data_freshness"] = line_s.split(":", 1)[1].strip()
            elif "Update these data point descriptions" in line_s and ":" in line_s:
                pairs_part = line_s.split(":", 1)[1].strip()
                for pair in pairs_part.split("|"):
                    if ":" in pair:
                        k, v = pair.split(":", 1)
                        target_key = k.strip()
                        new_desc = v.strip()
                        for tname, ent in active_by_name.items():
                            for c in ent.get("columns", []):
                                cname = c.get("name") or c.get("column_name")
                                if target_key in (cname, f"{tname}.{cname}"):
                                    c["description"] = new_desc

    session["requirement_data"] = req_data

    # Persist updated active entities into session["custom_entities"]
    custom_map = {(ce.get("table_name") or ce.get("name")): ce for ce in session.get("custom_entities", [])}
    for tname, ent in active_by_name.items():
        custom_map[tname] = ent
    session["custom_entities"] = list(custom_map.values())

    return _build_dynamic_glossary_for_session(session)


def _sync_contract_for_session(session: dict) -> dict:
    """Rebuild or synchronize `session['contract_dict']` and `session['contract_yaml']` from session entities."""
    canonical_domain, all_entities, active_entities = _build_domain_entities_for_session(session)
    ind_id = session.get("industry") or canonical_domain.get("industry_id") or "banking"
    domain_title = canonical_domain.get("display_name") or session.get("domain") or "Core Banking & Accounts"
    standards = canonical_domain.get("standards") or ["BIAN v12", "ISO 20022", "FIBO"]

    bank_profile = session.get("bank_profile") or {
        "bank_name": "Enterprise Data Platform",
        "org_name": "Enterprise Data Platform",
        "industry": ind_id,
        "regulatory_frameworks": standards,
    }
    if not bank_profile.get("regulatory_frameworks"):
        bank_profile["regulatory_frameworks"] = standards

    structured_req = session.get("requirement") or {
        "use_case_name": f"{domain_title} Silver Data Model",
        "domain": domain_title,
        "industry": ind_id,
    }
    structured_req["selected_entities"] = [e["table_name"] for e in active_entities]
    if session.get("selected_attributes"):
        structured_req["selected_attributes"] = session["selected_attributes"]

    product_plan = {
        "tables": active_entities,
        "entities": active_entities,
        "domain": domain_title,
        "selected_entities": [e["table_name"] for e in active_entities],
        "selected_attributes": session.get("selected_attributes") or {},
    }

    contract_dict = generate_data_contract_dict(
        product_plan=product_plan,
        bank_profile=bank_profile,
        structured_req=structured_req,
        selected_entities=[e["table_name"] for e in active_entities],
    )

    # Attach catalog_context so the UI can inspect all domain entities, add unselected entities, or import sources
    active_names = {e["table_name"] for e in active_entities}
    unselected_entities = [e for e in all_entities if e["table_name"] not in active_names]
    contract_dict["catalog_context"] = {
        "industry_id": ind_id,
        "domain_id": canonical_domain.get("name", "core_banking"),
        "domain_name": domain_title,
        "standards": standards,
        "total_domain_entities": len(all_entities),
        "selected_entity_names": [e["table_name"] for e in active_entities],
        "all_domain_entities": all_entities,
        "available_canonical_entities": unselected_entities,
        "available_source_models": get_source_models_catalog(),
    }

    contract_yaml = generate_data_contract_yaml(contract_dict)
    contract_dict["yaml_text"] = contract_yaml

    # Rebuild DDL for active entities
    target_dataset = contract_dict.get("info", {}).get("target_dataset", "banking_silver")
    ddl_parts = []
    for ent in active_entities:
        tbl = ent.get("table_name", "slv_entity")
        cols_def = []
        for c in ent.get("columns", []):
            cn = c.get("column_name") or c.get("name", "col")
            dt = c.get("data_type") or c.get("type", "STRING")
            desc = (c.get("description") or f"Conformed {cn}").replace("'", "\\'")
            cols_def.append(f"  {cn} {dt} OPTIONS(description='{desc}')")
        ddl_parts.append(f"CREATE OR REPLACE TABLE `{target_dataset}.{tbl}` (\n" + ",\n".join(cols_def) + "\n);")
    session["ddl_script"] = "\n\n".join(ddl_parts)
    session["contract_dict"] = contract_dict
    session["contract_yaml"] = contract_yaml
    return contract_dict


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    file_id = str(uuid.uuid4())
    content = await file.read()
    filename = file.filename or "uploaded_file"
    ext = Path(filename).suffix.lower().lstrip(".")

    preview = None
    try:
        if ext in ("json", "txt", "csv", "md", "sql", "yaml", "yml"):
            text = content.decode("utf-8", errors="ignore")
            preview = text[:1500]
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


@app.get("/catalog/navigator")
async def get_domain_navigator_tree():
    """Return the full hierarchical Domain Navigator catalog (Industries -> Subtypes -> Domains -> Entities -> Attributes) plus source models."""
    return get_navigator_tree()


@app.get("/catalog/source-models")
async def get_catalog_source_models(industry: Optional[str] = None):
    """Return upstream Bronze / source models available to bring into a Silver model."""
    models = get_source_models_catalog()
    if industry:
        models = [m for m in models if m.get("industry", "").lower() == industry.lower()] or models
    return {"source_models": models}


@app.get("/catalog/industries")
async def get_industries_catalog():
    """Return all supported industries with their metadata, icons, and subtypes."""
    return {"industries": get_industries()}


@app.get("/catalog/industries/{industry_id}/subtypes")
async def get_industry_subtypes_catalog(industry_id: str):
    """Return subtypes for a selected industry."""
    ind = get_industry(industry_id)
    if not ind:
        raise HTTPException(status_code=404, detail=f"Industry '{industry_id}' not found.")
    return {"industry": industry_id, "subtypes": ind.get("subtypes", [])}


@app.get("/catalog/industries/{industry_id}/domains")
async def get_industry_domains_catalog(industry_id: str):
    """Return available canonical domains for an industry with normalized entity & column lists for the Domain Navigator."""
    domains = get_domains_for_industry(industry_id, normalized=True)
    return {"industry": industry_id, "domains": domains}


@app.get("/catalog/domains")
async def get_domain_registry(industry: Optional[str] = None):
    """Return available canonical domains, optionally filtered by industry."""
    target_ind = industry or "banking"
    domains_list = get_domains_for_industry(target_ind, normalized=True)
    return {"domains": domains_list, "industry": target_ind, "amber_domains": []}


@app.get("/catalog/domains/{domain_name}/entities")
async def get_domain_entities_catalog(domain_name: str):
    """Return detailed entity list, column schemas, and available source models for a domain."""
    dom = get_domain_by_name(domain_name)
    if not dom:
        raise HTTPException(status_code=404, detail=f"Domain '{domain_name}' not found.")
    entities_list = dom.get("entities_list", [])
    return {
        "domain_id": dom["name"],
        "domain_name": dom["display_name"],
        "industry_id": dom.get("industry_id", "banking"),
        "description": dom.get("description", ""),
        "standards": dom.get("standards", []),
        "entity_count": len(entities_list),
        "entities": entities_list,
        "source_models": get_source_models_catalog(),
    }


@app.get("/catalog/domains/{domain_name}")
async def get_domain_framework(domain_name: str):
    """Return full schema skeleton, entities, columns, metrics for a domain across all industries."""
    dom = get_domain_by_name(domain_name)
    if dom:
        entities = dom.get("entities_map") or dom.get("entities", {})
        dimensions = [k for k, v in entities.items() if v.get("type") == "dimension"]
        events = [k for k, v in entities.items() if v.get("type") == "event"]
        aggregates = [k for k, v in entities.items() if v.get("type") == "aggregate"]
        return {
            "name": dom["name"],
            "display_name": dom["display_name"],
            "color": dom.get("color", "#0f766e"),
            "description": dom.get("description", "Silver Layer canonical domain model."),
            "standards": dom.get("standards", ["Enterprise Canonical Baseline"]),
            "hierarchy": ["Raw Bronze Source", "Silver Conformed Entity"],
            "entity_count": len(entities),
            "entity_types": {
                "dimensions": dimensions,
                "events": events,
                "aggregates": aggregates,
            },
            "derived_metrics": dom.get("derived_metrics", {}),
            "entities": entities,
            "entities_list": dom.get("entities_list", []),
        }

    return {
        "name": domain_name,
        "display_name": domain_name.replace("_", " ").title(),
        "color": "#0f766e",
        "description": "Silver Layer canonical conformed data model.",
        "standards": ["Enterprise Canonical Baseline"],
        "hierarchy": ["Raw Bronze Source", "Silver Conformed Entity"],
        "entity_types": {
            "dimensions": [f"slv_{domain_name}_master"],
            "events": [f"slv_{domain_name}_event"],
            "aggregates": [],
        },
        "derived_metrics": {},
        "entities": {
            f"slv_{domain_name}_master": {
                "type": "dimension",
                "grain": "one row per entity instance",
                "columns": [
                    {"name": "surrogate_key", "data_type": "STRING", "is_pk": True, "nullable": False, "description": "Deterministic Surrogate Key"},
                    {"name": f"{domain_name}_id", "data_type": "STRING", "is_pk": False, "nullable": False, "description": "Business Identifier"},
                    {"name": "name", "data_type": "STRING", "is_pk": False, "nullable": False, "description": "Legal / Display Name"},
                    {"name": "status", "data_type": "STRING", "is_pk": False, "nullable": True, "description": "Operational Status"},
                ],
            }
        },
    }


@app.post("/catalog/navigator/select")
async def select_from_domain_navigator(body: NavigatorSelectRequest):
    """
    Backend handler for the Domain Navigator modal.
    Persists the selected industry, domain, entities (all/single/multiple), and attributes
    into the session, and optionally opens the existing Silver structure directly for editing.
    """
    session_id = body.session_id or str(uuid.uuid4())
    session = store.get(session_id) or {
        "session_id": session_id,
        "step": "entity_selection",
        "bank_profile": {},
        "requirement": {},
        "turns": 0,
    }

    session["industry"] = body.industry_id
    ind = get_industry(body.industry_id)
    if ind:
        session["industry_name"] = ind["name"]

    matched_dom = None
    if body.domain_name:
        matched_dom = get_domain_by_name(body.domain_name)
    if not matched_dom:
        doms = get_domains_for_industry(body.industry_id, normalized=True)
        matched_dom = doms[0] if doms else get_domain_by_name("core_banking")

    if matched_dom:
        session["domain"] = matched_dom["display_name"]
        session["domain_id"] = matched_dom["name"]
        session["domain_obj"] = matched_dom

    all_domain_entities = [e["table_name"] for e in (matched_dom.get("entities_list") or [])]
    if body.selected_entities:
        session["selected_entities"] = body.selected_entities
    elif body.selected_attributes:
        session["selected_entities"] = [tbl for tbl, cols in body.selected_attributes.items() if cols]
    else:
        session["selected_entities"] = all_domain_entities

    if body.selected_attributes:
        session["selected_attributes"] = body.selected_attributes

    contract_dict = _sync_contract_for_session(session)
    store.set(session_id, session)

    return {
        "session_id": session_id,
        "industry_id": session.get("industry"),
        "domain": session.get("domain"),
        "selection_mode": body.selection_mode,
        "selected_entities": session["selected_entities"],
        "data_contract_view": contract_dict,
    }


@app.get("/v1/sessions/{session_id}/structure")
async def get_session_silver_structure(session_id: str):
    """Inspect existing Silver structure and available entities for the session's domain."""
    session = store.get(session_id) or {
        "session_id": session_id,
        "industry": "banking",
        "domain": "Core Banking & Accounts",
    }
    contract_dict = _sync_contract_for_session(session)
    store.set(session_id, session)
    return {
        "session_id": session_id,
        "domain": session.get("domain"),
        "selected_entities": session.get("selected_entities", []),
        "contract": contract_dict,
        "ddl": session.get("ddl_script", ""),
    }


@app.post("/v1/sessions/{session_id}/entities/add")
async def add_entity_to_silver_structure(session_id: str, body: AddEntityRequest):
    """
    Extend an existing Silver structure by:
      1. Adding another canonical entity from the domain catalog (`from_catalog=True`), OR
      2. Creating a custom Silver entity (`from_catalog=False`).
    """
    session = store.get(session_id) or {
        "session_id": session_id,
        "industry": "banking",
        "domain": body.domain_name or "Core Banking & Accounts",
        "custom_entities": [],
    }
    if body.domain_name:
        dom = get_domain_by_name(body.domain_name)
        if dom:
            session["domain"] = dom["display_name"]

    canonical_domain, all_entities, active_entities = _build_domain_entities_for_session(session)
    selected_names = list(session.get("selected_entities") or [e["table_name"] for e in active_entities])

    raw_name = (body.entity_name or body.table_name or "slv_custom_entity").strip()
    tname = raw_name if raw_name.startswith("slv_") else f"slv_{raw_name}"
    is_from_catalog = bool(body.from_catalog or (body.mode or "").lower() == "canonical")

    if is_from_catalog:
        # If adding from canonical catalog, ensure it's in selected_entities
        # Also check if it exists in another domain across the catalog
        if not any(e["table_name"] == tname for e in all_entities):
            imported = parse_external_entity_spec("source_catalog", source_model_id=raw_name, entity_name=tname)
            custom_list = list(session.get("custom_entities", []))
            custom_list.append(imported)
            session["custom_entities"] = custom_list
        if tname not in selected_names:
            selected_names.append(tname)
    else:
        # Custom entity creation
        raw_cols = body.columns or [
            {"name": "surrogate_key", "data_type": "STRING", "is_pk": True, "nullable": False, "description": "Deterministic SHA-256 Surrogate Primary Key"},
            {"name": f"{tname.replace('slv_', '')}_id", "data_type": "STRING", "is_pk": False, "nullable": False, "description": "Business Natural Identifier"},
            {"name": "status_cd", "data_type": "STRING", "is_pk": False, "nullable": True, "description": "Conformed Lifecycle Status Code"},
            {"name": "load_ts", "data_type": "TIMESTAMP", "is_pk": False, "nullable": False, "description": "Silver Lakehouse Commit Timestamp"},
        ]
        norm_custom = _normalize_entity_list({
            tname: {
                "type": body.entity_type,
                "grain": body.grain,
                "description": body.description,
                "columns": raw_cols,
            }
        })[0]
        custom_list = [ce for ce in session.get("custom_entities", []) if ce.get("table_name") != tname]
        custom_list.append(norm_custom)
        session["custom_entities"] = custom_list
        if tname not in selected_names:
            selected_names.append(tname)

    session["selected_entities"] = selected_names
    contract_dict = _sync_contract_for_session(session)
    store.set(session_id, session)
    return {
        "session_id": session_id,
        "added_entity": tname,
        "selected_entities": selected_names,
        "contract": contract_dict,
        "data_contract_view": contract_dict,
        "ddl": session.get("ddl_script", ""),
    }


@app.post("/v1/sessions/{session_id}/entities/import")
async def import_entity_into_silver_structure(session_id: str, body: ImportEntityRequest):
    """
    Bring an entity into the Silver model from an existing source/model:
      - `source_catalog`: From upstream Bronze / Canonical source model
      - `ddl`: From SQL CREATE TABLE DDL snippet
      - `odcs`: From Open Data Contract Standard (ODCS) YAML / JSON
    """
    session = store.get(session_id) or {
        "session_id": session_id,
        "industry": "banking",
        "domain": "Core Banking & Accounts",
        "custom_entities": [],
    }
    _, _, active_entities = _build_domain_entities_for_session(session)
    selected_names = list(session.get("selected_entities") or [e["table_name"] for e in active_entities])

    imported_entity = parse_external_entity_spec(
        source_type=body.source_type,
        payload=body.payload or body.raw_content or "",
        entity_name=body.entity_name or body.target_table_name,
        source_model_id=body.source_model_id,
        description=body.description,
    )
    tname = imported_entity["table_name"]

    custom_list = [ce for ce in session.get("custom_entities", []) if ce.get("table_name") != tname]
    custom_list.append(imported_entity)
    session["custom_entities"] = custom_list

    if tname not in selected_names:
        selected_names.append(tname)
    session["selected_entities"] = selected_names

    contract_dict = _sync_contract_for_session(session)
    store.set(session_id, session)
    return {
        "session_id": session_id,
        "imported_entity": imported_entity,
        "selected_entities": selected_names,
        "contract": contract_dict,
        "data_contract_view": contract_dict,
        "ddl": session.get("ddl_script", ""),
    }


def _sql_type_from_contract_type(raw_type: str) -> str:
    t = str(raw_type or "string").strip().upper()
    mapping = {
        "STRING": "STRING",
        "VARCHAR": "STRING",
        "TEXT": "STRING",
        "NUMERIC": "NUMERIC",
        "DECIMAL": "NUMERIC",
        "DECIMAL(18,2)": "NUMERIC",
        "FLOAT": "FLOAT64",
        "DOUBLE": "FLOAT64",
        "INT": "INT64",
        "INTEGER": "INT64",
        "INT64": "INT64",
        "BIGINT": "INT64",
        "BOOL": "BOOL",
        "BOOLEAN": "BOOL",
        "DATE": "DATE",
        "TIMESTAMP": "TIMESTAMP",
        "DATETIME": "TIMESTAMP",
        "JSON": "JSON",
    }
    return mapping.get(t, t if t else "STRING")


def _build_artifacts_from_saved_contract(session: dict, contract_dict: Optional[dict] = None) -> dict:
    """
    Persist the user's edited Silver Data Contract (`contract_dict`) and generate all Stage 3
    downstream deliverables (.yaml Contract, .sql BigQuery DDL, .xlsx STTM/Metadata/Audit,
    .json Dataplex Manifest, .py Airflow DAG) directly from the saved contract models & lineage.
    """
    session_id = session.get("session_id") or str(uuid.uuid4())
    canonical_domain, all_domain_entities, default_active_entities = _build_domain_entities_for_session(session)
    ind_id = session.get("industry") or canonical_domain.get("industry_id") or "banking"
    domain_name = (
        (contract_dict or {}).get("info", {}).get("domain")
        or canonical_domain.get("display_name")
        or session.get("domain")
        or "Core Banking & Accounts"
    )
    standards = (
        (contract_dict or {}).get("info", {}).get("standards")
        or canonical_domain.get("standards")
        or ["BIAN v12", "ISO 20022", "FIBO"]
    )

    if not contract_dict or not isinstance(contract_dict, dict) or not contract_dict.get("models"):
        contract_dict = session.get("contract_dict") or _sync_contract_for_session(session)

    # Ensure status reflects saved/approved state
    if "info" not in contract_dict or not isinstance(contract_dict["info"], dict):
        contract_dict["info"] = {}
    contract_dict["info"]["status"] = "APPROVED"
    if not contract_dict["info"].get("domain"):
        contract_dict["info"]["domain"] = domain_name
    if not contract_dict["info"].get("standards"):
        contract_dict["info"]["standards"] = standards

    target_dataset = contract_dict.get("info", {}).get("target_dataset") or "banking_silver"

    # Reconstruct active_entities directly from contract_dict["models"] so every user edit
    # (renamed/added/deleted columns, edited types, PK/FK/PII flags, added/removed tables) is honored
    models_map = contract_dict.get("models") or {}
    saved_entities: list[dict] = []
    ddl_parts: list[str] = []

    for model_key, model_def in models_map.items():
        if not isinstance(model_def, dict):
            continue
        phys_name = model_def.get("physicalName") or f"{target_dataset}.{model_key}"
        tbl_name = phys_name.split(".")[-1].strip() if "." in phys_name else model_key.strip()
        fields_map = model_def.get("fields") or {}
        cols_list: list[dict] = []
        col_ddl_lines: list[str] = []

        for col_name, col_def in fields_map.items():
            if not isinstance(col_def, dict):
                continue
            sql_dt = _sql_type_from_contract_type(col_def.get("type", "string"))
            is_pk = bool(col_def.get("primary"))
            is_pii = bool(col_def.get("pii"))
            nullable = bool(col_def.get("nullable", not is_pk))
            fk_ref = col_def.get("references") or ""
            desc = col_def.get("description") or f"Conformed attribute {col_name}"

            cols_list.append({
                "name": col_name,
                "column_name": col_name,
                "data_type": sql_dt,
                "type": sql_dt,
                "is_pk": is_pk,
                "is_primary_key": is_pk,
                "primary_key": is_pk,
                "pii": is_pii,
                "is_pii": is_pii,
                "nullable": nullable,
                "fk_ref": fk_ref or None,
                "references": fk_ref or None,
                "description": desc,
            })

            not_null_clause = " NOT NULL" if (is_pk or not nullable) else ""
            meta_tags = []
            if is_pk:
                meta_tags.append("PK")
            if fk_ref:
                meta_tags.append(f"FK->{fk_ref}")
            if is_pii:
                meta_tags.append("PII")
            tag_prefix = f"[{' | '.join(meta_tags)}] " if meta_tags else ""
            safe_desc = (tag_prefix + desc).replace("'", "\\'")
            col_ddl_lines.append(f"  {col_name} {sql_dt}{not_null_clause} OPTIONS(description='{safe_desc}')")

        saved_entities.append({
            "table_name": tbl_name,
            "name": tbl_name,
            "type": model_def.get("entity_type") or model_def.get("type") or "dimension",
            "entity_type": model_def.get("entity_type") or model_def.get("type") or "dimension",
            "grain": model_def.get("grain") or "one row per conformed record",
            "description": model_def.get("description") or f"Conformed Silver Entity {tbl_name}",
            "column_count": len(cols_list),
            "columns": cols_list,
        })

        tbl_desc = (model_def.get("description") or f"Conformed Silver Table {tbl_name}").replace("'", "\\'")
        ddl_parts.append(
            f"CREATE OR REPLACE TABLE `{target_dataset}.{tbl_name}` (\n"
            + ",\n".join(col_ddl_lines)
            + f"\n)\nOPTIONS(description='{tbl_desc}');"
        )

    if not saved_entities:
        saved_entities = default_active_entities

    ddl = "\n\n".join(ddl_parts)
    contract_yaml = generate_data_contract_yaml(contract_dict)
    contract_dict["yaml_text"] = contract_yaml

    # Attach catalog_context so the Workbench remains inspectable/extensible after save
    active_names_set = {e["table_name"] for e in saved_entities}
    unselected_entities = [e for e in all_domain_entities if e["table_name"] not in active_names_set]
    contract_dict["catalog_context"] = {
        "industry_id": ind_id,
        "domain_id": canonical_domain.get("name", "core_banking"),
        "domain_name": domain_name,
        "standards": standards,
        "total_domain_entities": len(all_domain_entities),
        "selected_entity_names": [e["table_name"] for e in saved_entities],
        "all_domain_entities": all_domain_entities,
        "available_canonical_entities": unselected_entities,
        "available_source_models": get_source_models_catalog(),
    }

    session["selected_entities"] = [e["table_name"] for e in saved_entities]
    session["contract_dict"] = contract_dict
    session["contract_yaml"] = contract_yaml
    session["ddl_script"] = ddl

    bank_profile = session.get("bank_profile") or {
        "bank_name": contract_dict.get("info", {}).get("owner") or "Global BFSI Bank",
        "org_name": contract_dict.get("info", {}).get("owner") or "Global BFSI Bank",
        "bank_code": "BFSI_US",
        "industry": ind_id,
        "regulatory_frameworks": standards,
    }
    structured_req = session.get("requirement") or {
        "use_case_name": contract_dict.get("info", {}).get("title") or f"{domain_name} Silver Data Model",
        "domain": domain_name,
        "banking_domain": domain_name,
    }
    structured_req["selected_entities"] = [e["table_name"] for e in saved_entities]

    domain_scope = {
        "recommended_tables": [e["table_name"] for e in saved_entities],
        "domain": domain_name,
        "status": "APPROVED",
    }
    product_plan = {
        "dataset": target_dataset,
        "tables": saved_entities,
        "entities": saved_entities,
        "domain": domain_name,
        "selected_entities": [e["table_name"] for e in saved_entities],
        "use_case": structured_req.get("use_case_name", domain_name),
        "status": "CONFORMED",
    }

    # Build STTM mappings from saved contract lineage (if edited) + saved entities
    mappings: list[dict] = []
    lineage_entities = (contract_dict.get("lineage") or {}).get("entity_lineage") or []
    mapped_tables_seen = set()

    for el in lineage_entities:
        if not isinstance(el, dict):
            continue
        tgt_tbl = el.get("target_table") or ""
        if "." not in tgt_tbl and tgt_tbl:
            tgt_tbl = f"{target_dataset}.{tgt_tbl}"
        src_tbl = el.get("source_table") or (el.get("source_tables") or ["raw_bronze.source_table"])[0]
        for cl in el.get("column_lineage") or []:
            if not isinstance(cl, dict):
                continue
            t_col = cl.get("target_column") or "id"
            s_col = cl.get("source_column") or f"{src_tbl}.{t_col}"
            if "." not in s_col:
                s_col = f"{src_tbl}.{s_col}"
            mappings.append({
                "source_column": s_col,
                "transform": cl.get("transformation") or "DIRECT_CONFORM",
                "target_column": t_col,
                "target_table": tgt_tbl,
            })
        if tgt_tbl:
            mapped_tables_seen.add(tgt_tbl.split(".")[-1])

    for ent in saved_entities:
        tbl = ent["table_name"]
        if tbl in mapped_tables_seen:
            continue
        src_tbl = ent.get("source_table") or f"raw_bronze.{tbl.replace('slv_', 'src_')}"
        for c in ent.get("columns", []):
            cn = c.get("column_name") or c.get("name", "col")
            dt = c.get("data_type") or c.get("type", "STRING")
            mappings.append({
                "source_column": f"{src_tbl}.{cn}",
                "transform": "SHA256_SURROGATE_HASH" if c.get("is_pk") else f"CAST_AS_{dt}",
                "target_column": cn,
                "target_table": f"{target_dataset}.{tbl}",
            })

    # Compute dbt Data Quality summary & generate dbt schema.yml + dbt models.sql (Requirement A)
    dbt_dq_summary = summarize_dbt_dq_suite(product_plan, contract_dict)
    dbt_schema_yaml_str = generate_dbt_schema_yaml(product_plan, contract_dict, bank_profile)
    dbt_models_sql_str = generate_dbt_models_sql(product_plan, mappings, contract_dict)

    # Generate downloadable Stage 3 files from the saved contract
    # 1. YAML Data Contract File
    contract_file_id = str(uuid.uuid4())
    contract_filename = f"slv_banking_contract_v1.0_{session_id[:6]}.yaml"
    FILE_STORE[contract_file_id] = {
        "id": contract_file_id,
        "name": contract_filename,
        "content": contract_yaml.encode("utf-8"),
        "type": "yaml",
    }

    # 2. SQL DDL File
    ddl_file_id = str(uuid.uuid4())
    ddl_filename = f"slv_banking_schema_{session_id[:6]}.sql"
    FILE_STORE[ddl_file_id] = {
        "id": ddl_file_id,
        "name": ddl_filename,
        "content": ddl.encode("utf-8"),
        "type": "sql",
    }

    # 3. dbt Data Quality Schema & Tests YAML (.yml)
    dbt_yaml_file_id = str(uuid.uuid4())
    dbt_yaml_filename = f"slv_dbt_schema_tests_{session_id[:6]}.yml"
    FILE_STORE[dbt_yaml_file_id] = {
        "id": dbt_yaml_file_id,
        "name": dbt_yaml_filename,
        "content": dbt_schema_yaml_str.encode("utf-8"),
        "type": "yaml",
    }

    # 4. dbt Incremental Transformation Models (.sql)
    dbt_sql_file_id = str(uuid.uuid4())
    dbt_sql_filename = f"slv_dbt_models_{session_id[:6]}.sql"
    FILE_STORE[dbt_sql_file_id] = {
        "id": dbt_sql_file_id,
        "name": dbt_sql_filename,
        "content": dbt_models_sql_str.encode("utf-8"),
        "type": "sql",
    }

    # 5. Excel STTM Mapping Workbook (.xlsx)
    sttm_xls_bytes = generate_sttm_excel(mappings, structured_req)
    sttm_xls_file_id = str(uuid.uuid4())
    sttm_xls_filename = f"slv_sttm_mapping_v1_{session_id[:6]}.xlsx"
    FILE_STORE[sttm_xls_file_id] = {
        "id": sttm_xls_file_id,
        "name": sttm_xls_filename,
        "content": sttm_xls_bytes,
        "type": "xlsx",
    }

    # 6. Excel Metadata Workbook (.xlsx - 2 Tabs)
    meta_xls_bytes = generate_metadata_excel(product_plan, domain_scope, structured_req)
    meta_xls_file_id = str(uuid.uuid4())
    meta_xls_filename = f"slv_metadata_v1_{session_id[:6]}.xlsx"
    FILE_STORE[meta_xls_file_id] = {
        "id": meta_xls_file_id,
        "name": meta_xls_filename,
        "content": meta_xls_bytes,
        "type": "xlsx",
    }

    # 7. Excel Sample Run Audit Workbook (.xlsx - 3 Tabs)
    audit_xls_bytes = generate_sample_audit_excel(product_plan, mappings)
    audit_xls_file_id = str(uuid.uuid4())
    audit_xls_filename = f"slv_sample_run_audit_{session_id[:6]}.xlsx"
    FILE_STORE[audit_xls_file_id] = {
        "id": audit_xls_file_id,
        "name": audit_xls_filename,
        "content": audit_xls_bytes,
        "type": "xlsx",
    }

    # 8. JSON GCP Dataplex Catalog Manifest (.json)
    dataplex_json_str = generate_dataplex_manifest_json(
        product_plan,
        bank_profile,
        contract_dict=contract_dict,
        dbt_dq_summary=dbt_dq_summary,
    )
    dataplex_file_id = str(uuid.uuid4())
    dataplex_filename = f"slv_dataplex_catalog_manifest_{session_id[:6]}.json"
    FILE_STORE[dataplex_file_id] = {
        "id": dataplex_file_id,
        "name": dataplex_filename,
        "content": dataplex_json_str.encode("utf-8"),
        "type": "json",
    }

    # 9. Python GCP Airflow DAG Script (.py)
    dag_code_str = generate_airflow_dag_code(product_plan, bank_profile)
    dag_file_id = str(uuid.uuid4())
    dag_filename = f"slv_pipeline_dag_{session_id[:6]}.py"
    FILE_STORE[dag_file_id] = {
        "id": dag_file_id,
        "name": dag_filename,
        "content": dag_code_str.encode("utf-8"),
        "type": "py",
    }

    # Preserve Stage 1 & 2 files, replace any prior DESIGNER / BUILDER / QUALITY files with the newly saved versions
    prior_files = [
        f for f in session.get("all_generated_files", [])
        if f.get("stage") not in ("DESIGNER", "BUILDER", "QUALITY (dbt)", "PUBLISHED")
    ]
    stage3_files = [
        {"id": contract_file_id, "name": contract_filename, "label": "Silver Data Contract (.yaml)", "stage": "DESIGNER"},
        {"id": ddl_file_id, "name": ddl_filename, "label": "BigQuery Silver DDL Script (.sql)", "stage": "BUILDER"},
        {"id": dbt_yaml_file_id, "name": dbt_yaml_filename, "label": "dbt Data Quality Schema & Tests (.yml)", "stage": "QUALITY (dbt)"},
        {"id": dbt_sql_file_id, "name": dbt_sql_filename, "label": "dbt Incremental Silver Models (.sql)", "stage": "QUALITY (dbt)"},
        {"id": sttm_xls_file_id, "name": sttm_xls_filename, "label": "STTM Mapping Workbook (.xlsx)", "stage": "DESIGNER"},
        {"id": meta_xls_file_id, "name": meta_xls_filename, "label": "Governance Metadata Workbook (.xlsx)", "stage": "DESIGNER"},
        {"id": audit_xls_file_id, "name": audit_xls_filename, "label": "Sample Transformation Audit (.xlsx)", "stage": "BUILDER"},
        {"id": dataplex_file_id, "name": dataplex_filename, "label": "GCP Dataplex Catalog Manifest (.json)", "stage": "BUILDER"},
        {"id": dag_file_id, "name": dag_filename, "label": "GCP Airflow Pipeline DAG (.py)", "stage": "BUILDER"},
    ]
    all_files = prior_files + stage3_files
    session["all_generated_files"] = all_files
    session["saved_entities"] = saved_entities
    session["target_dataset"] = target_dataset
    session["dbt_dq_summary"] = dbt_dq_summary
    session["dataplex_file_id"] = dataplex_file_id
    session["product_plan"] = product_plan

    active_table_names = [e["table_name"] for e in saved_entities]
    silver_sources = [e.get("source_table") or f"raw_bronze.{e['table_name'].replace('slv_', 'src_')}" for e in saved_entities]
    silver_tables = [f"{target_dataset}.{e['table_name']}" for e in saved_entities]
    lineage_summary = [
        f"{src} → {tgt} ({ent.get('description', 'Conformed Silver Entity')})"
        for src, tgt, ent in zip(silver_sources, silver_tables, saved_entities)
    ]

    silver_view = {
        "title": f"Silver Schema & STTM Specification ({len(saved_entities)} {'Entity' if len(saved_entities) == 1 else 'Entities'})",
        "step_label": f"{domain_name} Conformed Silver Layer",
        "summary": (
            f"Generated from your saved Silver Data Contract ({len(saved_entities)} {'entity' if len(saved_entities) == 1 else 'entities'}, "
            f"{len(mappings)} column mappings, {dbt_dq_summary['total_tests']} dbt Data Quality tests)."
        ),
        "narrative": (
            f"Your Silver Data Contract (`{contract_dict.get('info', {}).get('title', domain_name)}`) has been saved and applied across "
            f"{len(saved_entities)} conformed {'entity' if len(saved_entities) == 1 else 'entities'}: "
            f"{', '.join([f'`{t}`' for t in active_table_names])}."
        ),
        "silver_sources": silver_sources,
        "silver_tables": silver_tables,
        "lineage_summary": lineage_summary,
        "header": "Silver STTM Mappings",
        "mappings": mappings,
        "mapping_count": len(mappings),
        "dbt_data_quality": dbt_dq_summary,
    }

    session["step"] = "complete"
    store.set(session_id, session)

    return {
        "contract_dict": contract_dict,
        "ddl": ddl,
        "silver_view": silver_view,
        "all_files": all_files,
        "active_table_names": active_table_names,
        "dbt_dq_summary": dbt_dq_summary,
        "dbt_schema_yaml": dbt_schema_yaml_str,
    }


async def _execute_silver_publish_and_catalog_sync(session: dict, mode: Optional[str] = None) -> dict:
    """
    Execute the saved Silver DDL against live GCP BigQuery, seed sample conformed rows,
    synchronize table & column descriptions + PK/FK/PII/KPI tags + dbt Data Quality coverage
    into BigQuery & GCP Knowledge Catalog (Dataplex), and return clickable console links.
    """
    session_id = session.get("session_id") or str(uuid.uuid4())
    if not session.get("ddl_script") or not session.get("saved_entities"):
        _build_artifacts_from_saved_contract(session, session.get("contract_dict"))

    ddl = session.get("ddl_script", "")
    saved_entities = session.get("saved_entities", [])
    contract_dict = session.get("contract_dict") or {}
    target_dataset = (
        session.get("target_dataset")
        or (contract_dict.get("info", {}) or {}).get("target_dataset")
        or "banking_silver"
    )
    dbt_dq_summary = session.get("dbt_dq_summary") or summarize_dbt_dq_suite(
        session.get("product_plan") or {"dataset": target_dataset, "tables": saved_entities},
        contract_dict,
    )

    publisher = BigQueryPublisher(silver_dataset=target_dataset, mode=mode or "live")
    bq_report = await asyncio.to_thread(publisher.publish, ddl)

    project_id = publisher.project_id
    dataset_id = bq_report.get("dataset_id") or target_dataset
    dataset_location = bq_report.get("dataset_location") or "US"

    # Seed realistic sample rows into the published Silver tables
    seed_report = await asyncio.to_thread(
        publisher.seed_sample_rows,
        saved_entities,
        dataset_id,
    )
    actual_table_data = seed_report.get("actual_table_data", {})

    # Synchronize Governance, Business Glossary & dbt Data Quality metadata to BigQuery & GCP Knowledge Catalog
    kc_client = KnowledgeCatalogClient(project_id=project_id, dataset_id=dataset_id)
    kc_table_syncs: list[dict] = []
    for ent in saved_entities:
        ent_enriched = {
            **ent,
            "domain": (contract_dict.get("info", {}) or {}).get("domain") or session.get("domain", "Canonical Silver"),
            "industry": session.get("industry", "banking"),
        }
        sync_res = await asyncio.to_thread(
            kc_client.sync_table_governance_and_dbt_dq,
            dataset_id,
            ent_enriched,
            contract_dict,
            dbt_dq_summary,
        )
        kc_table_syncs.append(sync_res)
        if sync_res.get("dataset_location"):
            dataset_location = sync_res["dataset_location"]

    active_table_names = [e.get("table_name") or e.get("id") for e in saved_entities if e.get("table_name") or e.get("id")]
    info_block = contract_dict.get("info", {}) or {}
    dp_res = await asyncio.to_thread(
        kc_client.register_data_product,
        dataset_id,
        f"silver-{dataset_id}-{session_id[:6]}",
        info_block.get("title") or f"Silver Data Product ({dataset_id})",
        info_block.get("description") or f"Conformed Silver Layer Data Product ({session.get('domain', 'Banking')})",
        active_table_names,
        info_block.get("owner_email") or info_block.get("owner"),
        dataset_location,
    )
    gl_terms = [
        {"name": e.get("table_name") or e.get("id"), "description": e.get("description") or f"Conformed Silver entity"}
        for e in saved_entities if e.get("table_name") or e.get("id")
    ]
    gl_res = await asyncio.to_thread(
        kc_client.sync_business_glossary,
        f"silver-{dataset_id}-glossary",
        f"Silver Business Glossary ({dataset_id})",
        f"Conformed Silver Business Glossary for {session.get('domain', dataset_id)}",
        gl_terms,
    )
    console_links = kc_client.build_console_links(
        project_id=project_id,
        dataset_id=dataset_id,
        table_names=active_table_names,
        dataset_location=dataset_location,
        table_dbt_counts=dbt_dq_summary.get("table_test_counts", {}),
    )

    # Update Dataplex Catalog Manifest file & create Publish Report JSON file
    bank_profile = session.get("bank_profile") or {"project_id": project_id, "industry": session.get("industry", "banking")}
    product_plan = session.get("product_plan") or {"dataset": dataset_id, "tables": saved_entities}
    updated_manifest_str = generate_dataplex_manifest_json(
        product_plan,
        bank_profile,
        contract_dict=contract_dict,
        published_tables=bq_report.get("published_tables", []),
        dbt_dq_summary=dbt_dq_summary,
    )
    dp_fid = session.get("dataplex_file_id")
    if dp_fid and dp_fid in FILE_STORE:
        FILE_STORE[dp_fid]["content"] = updated_manifest_str.encode("utf-8")

    publish_view = {
        "publish_status": bq_report.get("publish_status", "published"),
        "project_id": project_id,
        "dataset_id": dataset_id,
        "dataset_location": dataset_location,
        "executed_count": bq_report.get("executed_count", len(active_table_names)),
        "published_tables": bq_report.get("published_tables", [f"{dataset_id}.{t}" for t in active_table_names]),
        "seeded_tables": seed_report.get("seeded_tables", {}),
        "actual_table_data": actual_table_data,
        "bigquery_dataset_url": console_links["bigquery_dataset_url"],
        "knowledge_catalog_dataset_url": dp_res.get("knowledge_catalog_url") or console_links["knowledge_catalog_dataset_url"],
        "knowledge_catalog_data_product_url": dp_res.get("knowledge_catalog_url"),
        "knowledge_catalog_glossary_url": gl_res.get("glossary_url"),
        "knowledge_catalog_data_product": dp_res,
        "knowledge_catalog_glossary": gl_res,
        "knowledge_catalog_home_url": console_links["knowledge_catalog_home_url"],
        "tables": console_links["tables"],
        "knowledge_catalog_sync": kc_table_syncs,
        "dbt_data_quality": dbt_dq_summary,
        "summary": bq_report.get("summary", ""),
    }

    pub_report_fid = str(uuid.uuid4())
    pub_report_filename = f"slv_publish_report_{session_id[:6]}.json"
    FILE_STORE[pub_report_fid] = {
        "id": pub_report_fid,
        "name": pub_report_filename,
        "content": json.dumps(publish_view, indent=2).encode("utf-8"),
        "type": "json",
    }

    all_files = [f for f in session.get("all_generated_files", []) if f.get("stage") != "PUBLISHED"]
    all_files.append({
        "id": pub_report_fid,
        "name": pub_report_filename,
        "label": "BigQuery & Knowledge Catalog Publish Report (.json)",
        "stage": "PUBLISHED",
    })
    session["all_generated_files"] = all_files
    session["publish_report"] = publish_view
    session["status"] = "published"
    store.set(session_id, session)

    return {
        "publish_view": publish_view,
        "all_files": all_files,
        "bq_report": bq_report,
    }


@app.get("/catalog/knowledge-catalog/status")
async def get_knowledge_catalog_status(dataset_id: Optional[str] = None):
    """Return live GCP Knowledge Catalog (Dataplex Universal Catalog) connection status and console links."""
    kc = KnowledgeCatalogClient(dataset_id=dataset_id)
    status = await asyncio.to_thread(kc.get_status)
    return status


@app.get("/catalog/knowledge-catalog/search")
async def search_knowledge_catalog(q: str = "", page_size: int = 15):
    """Search entries in GCP Knowledge Catalog (Dataplex Universal Catalog)."""
    kc = KnowledgeCatalogClient()
    res = await asyncio.to_thread(kc.search_entries, q, page_size)
    return res


@app.put("/v1/sessions/{session_id}/contract")
async def update_session_contract(session_id: str, body: SaveContractRequest):
    """Persist inline edits made to a Silver Data Contract structure and generate YAML/DDL/STTM/dbt artifacts."""
    if not session_id or session_id in ("null", "undefined", "default-session"):
        session_id = str(uuid.uuid4())
    session = store.get(session_id) or {
        "session_id": session_id,
        "industry": "banking",
        "domain": "Core Banking & Accounts",
    }
    session["session_id"] = session_id
    built = _build_artifacts_from_saved_contract(session, body.contract)
    return {
        "session_id": session_id,
        "status": "SAVED",
        "current_step": "sttm_ready",
        "contract": built["contract_dict"],
        "data_contract_view": built["contract_dict"],
        "silver_transform_view": built["silver_view"],
        "dbt_data_quality": built["dbt_dq_summary"],
        "ddl": built["ddl"],
        "files": built["all_files"],
    }


@app.put("/v1/sessions/{session_id}/glossary")
async def update_session_glossary(session_id: str, body: SaveGlossaryRequest):
    """Persist inline edits made to the Entity-Scoped Business Glossary & Attributes."""
    if not session_id or session_id in ("null", "undefined", "default-session"):
        session_id = str(uuid.uuid4())
    session = store.get(session_id) or {
        "session_id": session_id,
        "industry": "banking",
        "domain": "Core Banking & Accounts",
    }
    session["session_id"] = session_id
    req_data, glossary = _apply_glossary_edits_to_session(
        session,
        glossary_payload=body.glossary,
        requirement_updates=body.requirement_updates,
    )
    contract_dict = _sync_contract_for_session(session)
    store.set(session_id, session)
    return {
        "session_id": session_id,
        "status": "SAVED",
        "requirement_data": req_data,
        "glossary": glossary,
        "data_contract_view": contract_dict,
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
    session["session_id"] = session_id

    user_msg = body.message.strip()
    action = body.action
    session["turns"] = session.get("turns", 0) + 1

    if body.file_ref_id and body.file_ref_id in FILE_STORE:
        file_info = FILE_STORE[body.file_ref_id]
        if file_info.get("preview"):
            user_msg = f"Extracted from file '{file_info['name']}':\n{file_info['preview']}\n{user_msg}"

    current_step = session.get("step", "industry_selection")

    # Apply any inline Business Glossary & Attribute edits sent from the UI
    if body.glossary and isinstance(body.glossary, dict):
        _apply_glossary_edits_to_session(session, glossary_payload=body.glossary)

    # Direct handler for saving / updating the Business Glossary & Requirement Attributes in Stage 2
    if action in ("save_glossary", "edit") or user_msg.lower() == "save glossary changes":
        req_data, glossary = _apply_glossary_edits_to_session(
            session,
            glossary_payload=body.glossary,
            raw_text=user_msg if action == "edit" else None,
        )
        canonical_domain, _, active_entities = _build_domain_entities_for_session(session)
        domain_title = canonical_domain.get("display_name") or session.get("domain") or "Core Banking & Accounts"
        entity_summary = ", ".join([
            f"`{eg['table_name']}` ({eg['column_count']} cols)"
            for eg in glossary.get("entities", [])
        ])
        agent_text = (
            f"✅ **Business Glossary & Entity Attributes Updated for {domain_title}!**\n\n"
            f"- **Active Entities ({glossary['entity_count']}):** {entity_summary}\n"
            f"- **Total Curated Attributes:** **{glossary['column_count']}** attributes across selected entities\n\n"
            f"All edited attribute names, SQL data types, KPI/PII classifications, and business definitions are locked in and will automatically populate your **Silver Data Contract** and **BigQuery DDL**.\n\n"
            f"Click **Confirm Requirement** (or **Apply Glossary & Proceed to Contract**) when ready."
        )
        session["step"] = "requirement"
        store.set(session_id, session)
        return {
            "session_id": session_id,
            "current_step": "dpi_confirm_req",
            "messages": [{
                "agent": "Silver Data Product Assistant",
                "text": agent_text,
                "chips": ["Confirm Requirement", "Edit", "Open & Edit Existing Silver Structure", "Change Entity Selection"],
                "requirement_data": req_data,
                "glossary": glossary,
                "files": session.get("all_generated_files", []),
            }],
            "chips": ["Confirm Requirement", "Edit", "Open & Edit Existing Silver Structure", "Change Entity Selection"],
        }

    # Direct handler for "Apply & Save Contract" / "Save Contract"
    if (
        action == "save_contract"
        or user_msg.lower() in (
            "apply & save contract",
            "save contract",
            "apply and save contract",
            "generate silver transformation & ddl",
            "generate silver ddl & sttm",
        )
    ):
        built = _build_artifacts_from_saved_contract(session, body.contract)
        active_table_names = built["active_table_names"]
        ddl = built["ddl"]
        silver_view = built["silver_view"]
        all_files = built["all_files"]
        dbt_dq = built["dbt_dq_summary"]

        agent_response_text = (
            f"✅ **Silver Data Contract Saved & Applied!**\n\n"
            f"Generated the finalized **Silver Data Contract (`.yaml`)**, **BigQuery Silver DDL (`.sql`)**, "
            f"**dbt Data Quality Schema & Tests (`slv_dbt_schema_tests.yml` — {dbt_dq['total_tests']} tests)**, "
            f"**dbt Incremental Models (`slv_dbt_models.sql`)**, **STTM Mapping Workbook (`.xlsx`)**, "
            f"and **Dataplex / Airflow Artifacts** for **{len(active_table_names)} "
            f"{'entity' if len(active_table_names) == 1 else 'entities'}** "
            f"({', '.join([f'`{t}`' for t in active_table_names])}):\n\n"
            f"```sql\n{ddl}\n```\n\n"
            f"**dbt Data Quality Suite Generated (`contract: enforced`):**\n"
            f"- **Total dbt Tests:** `{dbt_dq['total_tests']}` (`{dbt_dq['unique_pk_tests']}` PK Unique, `{dbt_dq['not_null_tests']}` Not-Null, `{dbt_dq['fk_relationship_tests']}` FK Relationships, `{dbt_dq['custom_expression_tests']}` Custom Contract Rules)\n"
            f"- **Source Freshness SLA:** `{dbt_dq['source_freshness_sla']}`\n\n"
            f"Click **Publish to BigQuery** below to materialize these Silver tables in **GCP BigQuery** and synchronize governance & dbt DQ metadata to **GCP Knowledge Catalog (Dataplex)**."
        )
        return {
            "session_id": session_id,
            "current_step": "sttm_ready",
            "silver_transform_view": silver_view,
            "messages": [
                {
                    "agent": "Silver Data Product Assistant",
                    "text": agent_response_text,
                    "chips": ["Publish to BigQuery", "Open & Edit Existing Silver Structure", "Adjust the model", "Tweak the mapping", "Change Entity Selection"],
                    "silver_transform_view": silver_view,
                    "files": all_files,
                }
            ],
            "chips": ["Publish to BigQuery", "Open & Edit Existing Silver Structure", "Adjust the model", "Tweak the mapping", "Change Entity Selection"],
        }

    # Direct handler for "Publish to BigQuery" (executes live BigQuery DDL + syncs to GCP Knowledge Catalog)
    if (
        action in ("publish_bq", "publish")
        or user_msg.lower() in (
            "publish to bigquery",
            "publish",
            "approve & publish",
            "publish to bq & knowledge catalog",
            "publish to bigquery & knowledge catalog",
        )
    ):
        pub_out = await _execute_silver_publish_and_catalog_sync(session, mode="live")
        pv = pub_out["publish_view"]
        all_files = pub_out["all_files"]
        dq = pv.get("dbt_data_quality", {})
        tables_info = pv.get("tables", [])

        table_rows_md = "\n".join([
            f"| `{t['fqn']}` | `VERIFIED ({t.get('dbt_tests_count', 0)} dbt tests)` | "
            f"[Open in BigQuery ↗]({t['bigquery_url']}) | "
            f"[Open in Knowledge Catalog ↗]({t['knowledge_catalog_url']}) |"
            for t in tables_info
        ])

        status_badge = "✅ LIVE PUBLISHED" if pv.get("publish_status") == "published" else f"⚠️ {str(pv.get('publish_status', '')).upper()}"
        agent_response_text = (
            f"🚀 **Published to GCP BigQuery & Synchronized with GCP Knowledge Catalog (Dataplex)!**\n\n"
            f"- **Status:** **{status_badge}** (`{pv.get('executed_count', 0)}` DDL statements executed + sample conformed rows seeded)\n"
            f"- **GCP Project:** `{pv.get('project_id')}` | **Target Silver Dataset:** `{pv.get('dataset_id')}` (`{pv.get('dataset_location')}`)\n"
            f"- **Dataset Links:** [Open `{pv.get('dataset_id')}` in BigQuery Studio ↗]({pv.get('bigquery_dataset_url')})  •  "
            f"[Explore `{pv.get('dataset_id')}` in GCP Knowledge Catalog (Dataplex) ↗]({pv.get('knowledge_catalog_dataset_url')})\n\n"
            f"### Published Silver Entities & Console Deep Links\n"
            f"| Silver Table (FQN) | dbt Data Quality Status | BigQuery Console | GCP Knowledge Catalog (Dataplex) |\n"
            f"| :--- | :--- | :--- | :--- |\n"
            f"{table_rows_md}\n\n"
            f"### Synchronized Governance & dbt Data Quality Metadata in Knowledge Catalog\n"
            f"- **dbt Model Contract:** `enforced: true` (`{dq.get('framework', 'dbt Core v1.8')}`)\n"
            f"- **Total Verified dbt Tests Synced to Catalog:** **{dq.get('total_tests', 0)}** "
            f"(`{dq.get('unique_pk_tests', 0)}` PK Uniqueness, `{dq.get('not_null_tests', 0)}` Not-Null, "
            f"`{dq.get('fk_relationship_tests', 0)}` FK Referential Integrity, `{dq.get('custom_expression_tests', 0)}` Custom Quality Rules)\n"
            f"- **BigQuery & Dataplex Labels Synced:** `layer: silver`, `contract_status: approved`, `dbt_dq_status: verified`, `governance: dataplex_knowledge_catalog`"
        )

        return {
            "session_id": session_id,
            "current_step": "published",
            "publish_view": pv,
            "messages": [
                {
                    "agent": "Silver Data Product Assistant",
                    "text": agent_response_text,
                    "chips": ["Open & Edit Existing Silver Structure", "Change Entity Selection", "Start New Domain"],
                    "publish_view": pv,
                    "files": all_files,
                }
            ],
            "chips": ["Open & Edit Existing Silver Structure", "Change Entity Selection", "Start New Domain"],
        }

    # Helper to check industry match
    all_industries = get_industries()
    matched_ind = None
    for ind in all_industries:
        if (ind["name"].lower() in user_msg.lower() or 
            ind["id"].lower() in user_msg.lower() or 
            user_msg.lower() in ind["name"].lower()):
            matched_ind = ind
            break

    # ── STAGE 1: INDUSTRY & SUBTYPE SELECTION ────────────────────────────────
    if matched_ind and (current_step in ("industry_selection", "bank_profile") or "industry" in user_msg.lower()):
        session["industry"] = matched_ind["id"]
        session["industry_name"] = matched_ind["name"]
        session["step"] = "subtype_selection"
        
        subtype_chips = [st["name"] for st in matched_ind.get("subtypes", [])]
        agent_text = (
            f"Industry selected: **{matched_ind['name']}** {matched_ind.get('icon', '🏢')}\n\n"
            f"{matched_ind.get('description', '')}\n\n"
            f"Please select your **Industry Subtype** to load the corresponding domain landscape:"
        )
        store.set(session_id, session)
        return {
            "session_id": session_id,
            "current_step": "subtype_selection",
            "messages": [{
                "agent": "Silver Data Product Assistant",
                "text": agent_text,
                "chips": subtype_chips,
            }],
            "chips": subtype_chips,
        }

    # Handle Subtype Selection
    if current_step == "subtype_selection":
        ind_id = session.get("industry", "banking")
        ind = get_industry(ind_id) or get_industry("banking")
        subtypes = ind.get("subtypes", [])
        matched_st = None
        for st in subtypes:
            if st["name"].lower() in user_msg.lower() or user_msg.lower() in st["name"].lower():
                matched_st = st
                break
        if not matched_st and subtypes:
            matched_st = subtypes[0]

        session["subtype"] = matched_st["id"] if matched_st else "core"
        session["subtype_name"] = matched_st["name"] if matched_st else "General"
        session["step"] = "domain_selection"

        domains = get_domains_for_industry(ind_id)
        domain_bullets = "\n".join([
            f"- **{d['display_name']}** — {d.get('description', '')} ({d.get('entity_count', 2)} entities)"
            for d in domains
        ])
        domain_chips = [d["display_name"] for d in domains]

        agent_text = (
            f"Subtype selected: **{session['subtype_name']}**.\n\n"
            f"Here are the available canonical Silver domains for this model:\n\n"
            f"{domain_bullets}\n\n"
            f"Select a domain below to begin designing your Silver Schema:"
        )
        store.set(session_id, session)
        return {
            "session_id": session_id,
            "current_step": "domain_selection",
            "messages": [{
                "agent": "Silver Data Product Assistant",
                "text": agent_text,
                "chips": domain_chips,
            }],
            "chips": domain_chips,
        }

    # Direct handler for "Open & Edit Existing Silver Structure" from any step or Domain Navigator
    if action == "open_existing_structure" or "open & edit existing silver structure" in user_msg.lower() or "open and edit existing silver structure" in user_msg.lower():
        if body.industry_id:
            session["industry"] = body.industry_id
        if body.domain_name:
            dom = get_domain_by_name(body.domain_name)
            if dom:
                session["domain"] = dom["display_name"]
                session["domain_obj"] = dom
        if body.selected_entities:
            session["selected_entities"] = body.selected_entities
        if body.selected_attributes:
            session["selected_attributes"] = body.selected_attributes

        contract_dict = _sync_contract_for_session(session)
        canonical_domain, all_entities, active_entities = _build_domain_entities_for_session(session)
        domain_title = canonical_domain.get("display_name") or session.get("domain") or "Core Banking & Accounts"
        active_names = [e["table_name"] for e in active_entities]
        unselected_count = len(all_entities) - len(active_entities)

        agent_text = (
            f"Opened the **Silver Structure & Data Contract Workbench** for **{domain_title}** "
            f"({len(active_names)} active {'entity' if len(active_names) == 1 else 'entities'}: "
            f"{', '.join([f'`{n}`' for n in active_names])}).\n\n"
            f"**Review, customize, or extend your Silver Data Contract before saving:**\n"
            f"- **Inspect & Edit**: Modify column names, data types, nullability, PK/FK references, SLAs, and Quality Rules inline\n"
            f"- **Add Canonical Entity**: Bring in any of the remaining {unselected_count} domain entities\n"
            f"- **Create Custom Entity**: Define a new Silver table with custom grain and attributes\n"
            f"- **Bring Entity from Source/Model**: Import from upstream Bronze tables, SQL DDL, or ODCS YAML/JSON\n\n"
            f"When you are satisfied with the structure, click **Apply & Save Contract** to finalize the `.yaml` Data Contract and generate the BigQuery Silver DDL (`.sql`) and STTM Workbooks (`.xlsx`)."
        )
        session["step"] = "contract_review"
        store.set(session_id, session)
        return {
            "session_id": session_id,
            "current_step": "contract_review",
            "data_contract_view": contract_dict,
            "messages": [{
                "agent": "Silver Data Product Assistant",
                "text": agent_text,
                "chips": ["Apply & Save Contract", "Change Entity Selection"],
                "data_contract_view": contract_dict,
                "files": [],
            }],
            "chips": ["Apply & Save Contract", "Change Entity Selection"],
        }

    # Direct handler for Domain Navigator selection ("navigator_apply") or explicit entity selection payload
    if action in ("select_entities", "navigator_apply") or user_msg.startswith("Augment Conformed Silver Model"):
        if body.industry_id:
            session["industry"] = body.industry_id
        if body.domain_name:
            dom = get_domain_by_name(body.domain_name)
            if dom:
                session["domain"] = dom["display_name"]
                session["domain_obj"] = dom
        if body.selected_entities:
            session["selected_entities"] = body.selected_entities
        if body.selected_attributes:
            session["selected_attributes"] = body.selected_attributes
        current_step = "entity_selection"
        session["step"] = "entity_selection"

    # Handle Domain Selection -> Transition to Flexible Entity Selection (All / Single / Multiple / Open Existing)
    if current_step == "domain_selection":
        ind_id = session.get("industry", "banking")
        domains = get_domains_for_industry(ind_id, normalized=True)
        matched_dom = None
        for d in domains:
            if (d["display_name"].lower() in user_msg.lower() or 
                d["name"].lower() in user_msg.lower() or 
                user_msg.lower() in d["display_name"].lower()):
                matched_dom = d
                break
        if not matched_dom and domains:
            matched_dom = domains[0]

        domain_title = matched_dom["display_name"]
        session["domain"] = domain_title
        session["domain_id"] = matched_dom["name"]
        session["domain_obj"] = matched_dom
        session["step"] = "entity_selection"

        norm_entities = matched_dom.get("entities_list") or _normalize_entity_list(matched_dom.get("entities", {}))
        entity_bullets = "\n".join([
            f"- **`{e['table_name']}`** ({e.get('type', 'dimension').upper()}, {e.get('column_count', len(e.get('columns', [])))} cols) — {e.get('description', '')}"
            for e in norm_entities
        ])

        entity_selector_view = {
            "domain_id": matched_dom["name"],
            "domain_name": domain_title,
            "domain_label": domain_title,
            "industry_id": ind_id,
            "description": matched_dom.get("description", ""),
            "standards": matched_dom.get("standards", ["BIAN v12", "ISO 20022"]),
            "entity_count": len(norm_entities),
            "total_entities": len(norm_entities),
            "supported_modes": ["all", "single", "multiple"],
            "entities": norm_entities,
            "source_models": get_source_models_catalog(),
        }

        single_entity_chips = [f"Only {e['table_name']}" for e in norm_entities[:4]]
        chips_list = [
            f"Select All Entities ({len(norm_entities)})",
            "Open & Edit Existing Silver Structure",
            *single_entity_chips,
        ]

        agent_text = (
            f"Domain selected: **{domain_title}** ({', '.join(matched_dom.get('standards', ['Enterprise Standard']))} aligned).\n\n"
            f"This area includes **{len(norm_entities)} canonical Silver entities**:\n"
            f"{entity_bullets}\n\n"
            f"**Choose your Entity Scope below**:\n"
            f"1. **Select All Entities** ({len(norm_entities)} tables)\n"
            f"2. **Select One Specific Entity** (focus on a single table)\n"
            f"3. **Select Multiple Entities** (custom subset)\n"
            f"4. **Open & Edit Existing Silver Structure** (inspect & extend existing tables directly)"
        )

        store.set(session_id, session)
        return {
            "session_id": session_id,
            "current_step": "entity_selection",
            "entity_selector_view": entity_selector_view,
            "messages": [{
                "agent": "Silver Data Product Assistant",
                "text": agent_text,
                "chips": chips_list,
                "entity_selector_view": entity_selector_view,
                "files": session.get("all_generated_files", []),
            }],
            "chips": chips_list,
        }

    # Handle Flexible Entity Selection Step (All, Single, or Multiple Entities)
    if current_step == "entity_selection":
        ind_id = session.get("industry", "banking")
        canonical_domain, all_entities, _ = _build_domain_entities_for_session(session)
        domain_title = canonical_domain.get("display_name") or session.get("domain") or "Core Banking & Accounts"
        all_table_names = [e["table_name"] for e in all_entities]

        selected_entities: list[str] = []
        if body.selected_entities and len(body.selected_entities) > 0:
            selected_entities = [t for t in body.selected_entities if t]
        elif body.selected_attributes and len(body.selected_attributes) > 0:
            selected_entities = [tbl for tbl, cols in body.selected_attributes.items() if cols]
        else:
            msg_lower = user_msg.lower()
            if "select all" in msg_lower or "all entities" in msg_lower:
                selected_entities = list(all_table_names)
            else:
                # Match any entity names mentioned in the user message (supports single or multiple)
                for tname in all_table_names:
                    if tname.lower() in msg_lower or tname.replace("slv_", "").lower() in msg_lower:
                        selected_entities.append(tname)
                # Also check across all industries if user came from Domain Navigator
                if not selected_entities:
                    cat = schema_loader.get_multi_industry_catalog()
                    for ind_obj in cat.get("industries", []):
                        for dom_obj in ind_obj.get("domains", []):
                            for k in dom_obj.get("entities", {}).keys():
                                if k.lower() in msg_lower and k not in selected_entities:
                                    selected_entities.append(k)
                                    session["domain"] = dom_obj["display_name"]
                                    session["industry"] = ind_obj["id"]
                                    domain_title = dom_obj["display_name"]
                                    ind_id = ind_obj["id"]

        if not selected_entities:
            selected_entities = list(all_table_names)

        session["selected_entities"] = selected_entities
        if body.selected_attributes:
            session["selected_attributes"] = body.selected_attributes
        session["step"] = "requirement"

        _, _, active_entities = _build_domain_entities_for_session(session)
        entity_bullets = "\n".join([
            f"- **`{e['table_name']}`** ({e.get('type', 'dimension').upper()}, {len(e.get('columns', []))} cols): {e.get('description', 'Conformed entity')}"
            for e in active_entities
        ])

        scope_mode_label = (
            f"Single Entity (`{active_entities[0]['table_name']}`)"
            if len(active_entities) == 1
            else f"All {len(active_entities)} Domain Entities"
            if len(active_entities) == len(all_entities)
            else f"{len(active_entities)} Selected Entities"
        )

        # Reset use_case_name when entity selection changes so it reflects the new entity scope
        if session.get("requirement_data"):
            session["requirement_data"].pop("use_case_name", None)
        req_data, glossary = _build_dynamic_glossary_for_session(session)

        agent_text = (
            f"Configured **{domain_title}** with scope: **{scope_mode_label}**.\n\n"
            f"**Selected Conformed {'Entity' if len(active_entities) == 1 else 'Entities'} ({len(active_entities)}):**\n"
            f"{entity_bullets}\n\n"
            f"**Dynamic Business Glossary & Attributes ({glossary['column_count']} attributes across {glossary['entity_count']} {'entity' if glossary['entity_count'] == 1 else 'entities'}):**\n"
            f"Use the interactive **Business Glossary & Attribute Workbench** below to filter by entity, edit business definitions, adjust SQL data types or KPI/PII classifications, add custom attributes, or exclude non-key attributes.\n\n"
            f"Click **Confirm Requirement** (or **Apply Glossary & Proceed to Contract**) to generate the scoped Silver Schema & Data Contract."
        )

        store.set(session_id, session)
        return {
            "session_id": session_id,
            "current_step": "dpi_confirm_req",
            "messages": [{
                "agent": "Silver Data Product Assistant",
                "text": agent_text,
                "chips": ["Confirm Requirement", "Edit", "Open & Edit Existing Silver Structure", "Change Entity Selection"],
                "requirement_data": req_data,
                "glossary": glossary,
                "files": session.get("all_generated_files", []),
            }],
            "chips": ["Confirm Requirement", "Edit", "Open & Edit Existing Silver Structure", "Change Entity Selection"],
        }

    # Allow user to re-open Entity Selection if requested
    if user_msg == "Change Entity Selection":
        canonical_domain, all_entities, active_entities = _build_domain_entities_for_session(session)
        domain_title = canonical_domain.get("display_name") or session.get("domain") or "Core Banking & Accounts"
        ind_id = session.get("industry", "banking")
        session["step"] = "entity_selection"
        entity_selector_view = {
            "domain_id": canonical_domain.get("name", "core_banking"),
            "domain_name": domain_title,
            "industry_id": ind_id,
            "description": canonical_domain.get("description", ""),
            "standards": canonical_domain.get("standards", ["BIAN v12", "ISO 20022"]),
            "entity_count": len(all_entities),
            "entities": all_entities,
            "selected_entities": [e["table_name"] for e in active_entities],
            "source_models": get_source_models_catalog(),
        }
        chips_list = [
            f"Select All Entities ({len(all_entities)})",
            "Open & Edit Existing Silver Structure",
            *[f"Only {e['table_name']}" for e in all_entities[:4]],
        ]
        store.set(session_id, session)
        return {
            "session_id": session_id,
            "current_step": "entity_selection",
            "entity_selector_view": entity_selector_view,
            "messages": [{
                "agent": "Silver Data Product Assistant",
                "text": f"Re-opened entity selection for **{domain_title}**. Select all entities, a single entity, or a custom subset:",
                "chips": chips_list,
                "entity_selector_view": entity_selector_view,
            }],
            "chips": chips_list,
        }

    # Backward-compatible banking profile handling
    if current_step == "bank_profile":
        if user_msg == "Use Default Retail Bank Profile" or not user_msg:
            user_msg = "Global Retail & Commercial Bank with Core Banking, Cards, and Loan products adhering to BIAN, ISO 20022, and Basel III standards."
            session["chip_selected"] = True

        prof_out = await bank_profile_agent.run(user_msg, session_id=session_id)
        if "Flexcube" in body.message or body.message == "US Commercial Bank (Flexcube)":
            prof_out["core_banking_system"] = "Oracle FLEXCUBE"
        elif "Temenos" in body.message or body.message == "European Retail Bank (Temenos)":
            prof_out["core_banking_system"] = "Temenos T24"

        is_complete = bank_profile_agent.is_complete(prof_out) or action in ("confirm_req", "design_schema")
        if not is_complete and body.message != "Use Default Retail Bank Profile":
            missing = bank_profile_agent.get_missing_fields(prof_out)
            missing_list = "\n".join([f"- **{m.replace('_', ' ').title()}**" for m in missing]) if missing else "- **Organization Name & Operating Region**"
            agent_text = (
                f"Welcome to the **Silver Data Product Assistant**!\n\n"
                f"Please specify your **Industry** or organization profile details:\n\n"
                f"{missing_list}"
            )
            chips_to_send = [
                "Banking & Financial Services",
                "Industrial & Water (Ecolab)",
                "Retail & Commerce",
                "Automotive & Fleet",
                "Life Sciences"
            ]
            session["bank_profile"] = prof_out
            store.set(session_id, session)
            return {
                "session_id": session_id,
                "current_step": "industry_selection",
                "messages": [{
                    "agent": "Silver Data Product Assistant",
                    "text": agent_text,
                    "chips": chips_to_send,
                }],
                "chips": chips_to_send,
            }

        bp_pdf_bytes = generate_bank_profile_pdf(prof_out)
        bp_file_id = str(uuid.uuid4())
        bp_filename = f"slv_organization_profile_{session_id[:6]}.pdf"
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
                "label": "Profile & Governance Brief (.pdf)",
                "stage": "ORGANIZATION PROFILE",
            })
        session["all_generated_files"] = all_files
        session["bank_profile"] = prof_out
        session["step"] = "requirement"
        current_step = "requirement"

    # ── STAGE 2: REQUIREMENT UNDERSTANDING AGENT ─────────────────────────────
    if current_step == "requirement":
        bank_profile = session.get("bank_profile", {
            "bank_name": "Global Enterprise",
            "org_name": "Global Enterprise",
            "bank_code": "ENT_US",
            "industry": session.get("industry", "banking"),
            "regulatory_frameworks": [],
        })

        prior_req = session.get("requirement", {})
        context = {
            "bank_profile": bank_profile,
            "clarification_pass": session.get("turns", 1),
        }
        if prior_req:
            context["prior_output"] = prior_req

        is_ready = (
            action in ("confirm_req", "design_schema")
            or user_msg in ("Confirm Requirement", "Design Silver Schema", "Apply Glossary & Proceed to Contract")
        )
        req_out = session.get("requirement_data") or session.get("requirement") or {}

        if not is_ready:
            req_out = await requirement_understanding_agent.run(
                user_msg,
                context=context,
                session_id=session_id,
            )
            is_ready = requirement_understanding_agent.is_handoff_ready(req_out)

        domain_name = req_out.get("domain") or req_out.get("banking_domain") or session.get("domain") or "Canonical Domain"
        use_case_title = req_out.get("use_case_name") or f"{str(domain_name).replace('_', ' ').title()} Silver Data Model"
        req_out["use_case_name"] = use_case_title
        if "domain" not in req_out:
            req_out["domain"] = domain_name
        if session.get("selected_entities"):
            req_out["selected_entities"] = session["selected_entities"]

        req_data, glossary = _build_dynamic_glossary_for_session(session)

        if not is_ready:
            missing = requirement_understanding_agent.get_missing_fields(req_out)
            missing_text = "\n".join([f"- **{m.replace('_', ' ').title()}**: Define core attributes or measures" for m in missing]) if missing else "- **Key Attributes & Metrics**: Recommended columns for conformed entities"
            dp_names = ", ".join([f"`{dp.get('name', '')}`" for dp in req_data.get('data_points', [])[:15]])
            agent_text = (
                f"Understood draft requirement: **{req_data['use_case_name']}** (`{req_data['domain']}`).\n\n"
                f"**Recommended Key Attributes & Metrics ({glossary['column_count']} total across {glossary['entity_count']} entities):**\n"
                f"{dp_names}\n\n"
                f"To finalize the Silver Schema, please review or customize the following details:\n\n"
                f"{missing_text}\n\n"
                f"You can edit attributes directly in the **Business Glossary** below, click **Confirm Requirement** to build the conformed schema, or click **Edit**."
            )
            msg_payload = {
                "agent": "Silver Data Product Assistant",
                "text": agent_text,
                "chips": ["Confirm Requirement", "Edit", "Design Silver Schema", "Change Entity Selection"],
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
                "chips": ["Confirm Requirement", "Edit", "Design Silver Schema", "Change Entity Selection"],
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
        session["step"] = "contract_review"

    # ── STAGE 3A: INTERACTIVE SILVER STRUCTURE & DATA CONTRACT WORKBENCH ──────
    # Present the editable Data Contract Workbench first. Final .yaml, .sql DDL,
    # .xlsx STTM workbooks, and Dataplex/Airflow artifacts are generated when the
    # user clicks "Apply & Save Contract".
    canonical_domain, all_domain_entities, active_entities = _build_domain_entities_for_session(session)
    domain_name = canonical_domain.get("display_name") or session.get("domain") or "Core Banking & Accounts"
    contract_dict = _sync_contract_for_session(session)
    if "info" not in contract_dict or not isinstance(contract_dict["info"], dict):
        contract_dict["info"] = {}
    contract_dict["info"]["status"] = "DRAFT — AWAITING SAVE"

    active_table_names = [e["table_name"] for e in active_entities]
    unselected_count = len(all_domain_entities) - len(active_entities)

    session["step"] = "contract_review"
    store.set(session_id, session)

    agent_response_text = (
        f"Prepared the interactive **Silver Structure & Data Contract Workbench** for **{domain_name}** "
        f"with **{len(active_entities)} selected {'entity' if len(active_entities) == 1 else 'entities'}** "
        f"({', '.join([f'`{t}`' for t in active_table_names])}).\n\n"
        f"**Step 1 — Review, Edit, or Extend Your Silver Structure Below:**\n"
        f"- **Models & Columns**: Edit table/column names, data types, PK/FK/PII flags, or add/delete columns inline\n"
        f"- **Extend Structure Toolbar**: Inspect all {len(all_domain_entities)} domain entities, add any of the remaining {unselected_count} canonical entities, create a custom entity, or import from Bronze/SQL DDL/ODCS\n"
        f"- **Lineage, Quality Rules & SLAs**: Customize upstream source mappings, DQ assertions, and freshness SLAs\n\n"
        f"**Step 2 — Click `Apply & Save Contract`** (in the card header/footer or the chip below) to save your contract and generate the finalized **Silver Data Contract (`.yaml`)**, **BigQuery Silver DDL (`.sql`)**, and **STTM Mapping Workbooks (`.xlsx`)**."
    )

    return {
        "session_id": session_id,
        "current_step": "contract_review",
        "data_contract_view": contract_dict,
        "messages": [
            {
                "agent": "Silver Data Product Assistant",
                "text": agent_response_text,
                "chips": ["Apply & Save Contract", "Change Entity Selection"],
                "data_contract_view": contract_dict,
                "files": [],
            }
        ],
        "chips": ["Apply & Save Contract", "Change Entity Selection"],
    }


