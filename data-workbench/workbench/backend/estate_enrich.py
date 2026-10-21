"""LLM-batch metadata enrichment for Connected Estate graph nodes.

Generates per-column and per-table descriptions by calling Claude once per
schema-group of tables (batched to bound cost), then writes them directly onto
the ``:EstateColumn`` and ``:EstateDataset`` nodes in Neo4j.

This is structurally distinct from the Pulse ``metadata-enrichment`` skill path
(which targets project-scoped ``:Column`` nodes via a pipeline stage). Here the
targets are ``:EstateColumn`` / ``:EstateDataset`` nodes on the global AppSettings
graph, and enrichment is triggered explicitly from the estate surface, not the
pipeline.

Enrichment state machine:
  None → enriching → enriched | failed

Because descriptions are an estate asset — reusable across all future feasibility
runs — they are written directly to the graph nodes (``c.description``,
``d.description``) and the scan's ``enrichment_state`` is updated in SQLite.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Optional

from sqlmodel import Session, select

from . import embeddings, schema_dna
from . import estate as estate_mod
from .database import engine
from .models import EstateScan, EstateSource

logger = logging.getLogger(__name__)

# ── Cypher write-back ──────────────────────────────────────────────────────────

_SET_COLUMN_DESCRIPTION = """
MATCH (c:EstateColumn {uri: $uri})
SET c.description = $description, c.descriptionGeneratedAt = datetime()
"""

_SET_DATASET_DESCRIPTION = """
MATCH (d:EstateDataset {uri: $uri})
SET d.description = $description, d.descriptionGeneratedAt = datetime()
"""

_READ_SCHEMA_DATASETS = """
MATCH (s:EstateScan {uri: $scan_uri})-[:OBSERVED]->(d:EstateDataset)
WHERE (d.deletedInScan IS NULL OR d.deletedInScan > s.version)
  AND d.schema = $schema
OPTIONAL MATCH (d)-[:HAS_COLUMN]->(c:EstateColumn)
WHERE c.deletedInScan IS NULL OR c.deletedInScan > s.version
WITH d, c ORDER BY c.ordinal
RETURN d.uri AS d_uri, d.table AS table,
       collect(CASE WHEN c IS NULL THEN null ELSE {
           uri: c.uri, name: c.name, data_type: c.dataType,
           nullable: c.nullable, description: c.description,
           embedding_hash: c.embeddingTextHash, embedding_model: c.embeddingModel
       } END) AS columns
ORDER BY d.table
"""

_READ_SCAN_SCHEMAS = """
MATCH (s:EstateScan {uri: $scan_uri})-[:OBSERVED]->(d:EstateDataset)
WHERE d.deletedInScan IS NULL OR d.deletedInScan > s.version
RETURN DISTINCT d.schema AS schema, d.database AS database, d.sourceId AS source_id
"""


# ── LLM call (tool-less, isolated) ──────────────────────────────────────────────

_ENRICH_SYSTEM = """\
You are a data catalog expert generating concise, accurate descriptions for database tables
and columns based solely on their structural metadata (names and types — no actual data values
are available). Your descriptions help data engineers and product owners understand what each
table/column represents and how it fits into the broader data model.

Respond ONLY with a valid JSON object matching the exact schema provided. Do not add commentary.
"""

_ENRICH_PROMPT_TMPL = """\
Platform: {platform}
Database: {database}
Schema: {schema}

Tables and columns to describe:
{tables_json}

Generate a JSON object with this exact structure:
{{
  "schema_description": "<1-2 sentence summary of what this namespace/schema holds, inferred from its tables>",
  "tables": {{
    "<table_name>": {{
      "description": "<1-2 sentence description of what this table represents>",
      "columns": {{
        "<column_name>": "<1-sentence description of what this column contains>"
      }}
    }}
  }}
}}

Rules:
- Keep descriptions factual and noun-phrase focused (e.g. "The unique identifier for a customer account.")
- Base descriptions ONLY on the column/table name and data type — never invent data values or business rules
- For ID columns: identify what entity the ID references based on naming conventions
- For datetime columns: describe the event or moment being recorded
- For status/code columns: describe what the value classifies without making up allowed values
- If a column purpose is truly unclear from the name, say so briefly ("Purpose unclear from name alone.")
- The schema_description should characterise the collection of tables as a whole (its subject area), not restate any single table.
"""

_JSON_OBJ_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _fallback_schema_description(
    schema: str, tables_out: dict[str, Any], datasets: list[dict],
) -> str:
    """Synthesize a schema description when the LLM omitted one (or failed).

    Deterministic (table names sorted) so re-runs are stable; always non-empty
    when the schema has ≥1 table. Prefers any per-table descriptions the LLM DID
    return, else falls back to the bare table inventory. Kept to 1–2 sentences.
    """
    table_names = sorted({(d.get("table") or "").strip() for d in datasets
                          if (d.get("table") or "").strip()})
    if not table_names:
        return ""
    shown = table_names[:8]
    more = len(table_names) - len(shown)
    listing = ", ".join(shown) + (f", and {more} more" if more > 0 else "")
    base = f"The '{schema}' namespace holds {len(table_names)} table(s): {listing}."
    # Fold in the first available per-table description as a subject-area hint.
    for tbl in table_names:
        info = (tables_out or {}).get(tbl) or {}
        td = (info.get("description") or "").strip()
        if td:
            base += f" {td[:200]}"
            break
    return base


def _extract_json(text: str) -> Optional[dict]:
    matches = _JSON_OBJ_RE.findall(text or "")
    candidate = matches[-1] if matches else None
    if candidate is None:
        stripped = (text or "").strip()
        candidate = stripped if stripped.startswith("{") else None
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def _call_llm(prompt: str) -> Optional[str]:
    """One tool-less SDK call for schema enrichment. Returns raw text or None."""
    try:
        from claude_agent_sdk import (  # type: ignore
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )
    except ImportError:
        return None

    from .config import BASE_DIR

    options = ClaudeAgentOptions(
        allowed_tools=[], permission_mode="default",
        cwd=str(BASE_DIR), max_turns=1,
        system_prompt=_ENRICH_SYSTEM,
    )
    transcript: list[str] = []
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        transcript.append(block.text)
            elif isinstance(message, ResultMessage):
                try:
                    from . import llm_usage
                    llm_usage.record_usage(
                        source="estate_enrich",
                        usage=llm_usage.extract_usage(message),
                    )
                except Exception:
                    pass
                if getattr(message, "is_error", False):
                    return None
    except Exception as exc:
        logger.warning("estate_enrich LLM call failed: %s", exc)
        return None
    return "".join(transcript)


# ── per-schema enrichment batch ───────────────────────────────────────────────

async def _enrich_schema(
    session: Session,
    *,
    scan_id: int,
    platform: str,
    database: str,
    schema: str,
) -> dict[str, Any]:
    """Enrich all datasets in one schema with LLM-generated descriptions.

    One LLM call per schema-group (bounded cost). Returns per-table stats.
    """
    s_uri = estate_mod.scan_uri(scan_id)
    stats: dict[str, Any] = {
        "schema": schema, "tables": 0, "columns": 0, "dataset_count": 0,
        "schema_description": "", "schema_description_source": "", "errors": [],
    }

    # Read this schema's datasets+columns from the graph.
    datasets: list[dict] = []
    with estate_mod.estate_graph_session(session) as ns:
        for rec in ns.run(_READ_SCHEMA_DATASETS, scan_uri=s_uri, schema=schema):
            cols = [c for c in (rec["columns"] or []) if c]
            datasets.append({
                "d_uri": rec["d_uri"],
                "table": rec["table"] or "",
                "columns": cols,
            })

    stats["dataset_count"] = len(datasets)
    if not datasets:
        return stats

    # Build the LLM prompt (JSON payload of table→columns structure).
    tables_payload = {
        d["table"]: [
            {"name": c["name"], "type": c.get("data_type") or "unknown",
             "nullable": c.get("nullable", True)}
            for c in d["columns"]
        ]
        for d in datasets
    }
    prompt = _ENRICH_PROMPT_TMPL.format(
        platform=platform,
        database=database,
        schema=schema,
        tables_json=json.dumps(tables_payload, indent=2),
    )

    raw = await _call_llm(prompt)
    if raw is None:
        stats["errors"].append("LLM call returned no output")
        # Still give the schema a deterministic description so downstream feasibility
        # (and the EstatePage) never show a blank namespace on a partial enrichment.
        stats["schema_description"] = _fallback_schema_description(schema, {}, datasets)
        stats["schema_description_source"] = "fallback" if stats["schema_description"] else ""
        return stats

    parsed = _extract_json(raw)
    if not parsed or "tables" not in parsed:
        stats["errors"].append("LLM response did not parse to expected JSON shape")
        stats["schema_description"] = _fallback_schema_description(schema, {}, datasets)
        stats["schema_description_source"] = "fallback" if stats["schema_description"] else ""
        return stats

    tables_out: dict[str, Any] = parsed.get("tables") or {}
    _llm_schema_desc = (parsed.get("schema_description") or "").strip()
    if _llm_schema_desc:
        stats["schema_description"] = _llm_schema_desc
        stats["schema_description_source"] = "llm"
    else:
        # Parsed OK but the model omitted schema_description (the accuweather bug):
        # synthesize from the table descriptions it DID return.
        stats["schema_description"] = _fallback_schema_description(schema, tables_out, datasets)
        stats["schema_description_source"] = "fallback" if stats["schema_description"] else ""

    # Write descriptions back to graph nodes.
    with estate_mod.estate_graph_session(session) as ns:
        for d in datasets:
            tbl = d["table"]
            tbl_info = tables_out.get(tbl) or {}
            tbl_desc = (tbl_info.get("description") or "").strip()
            if tbl_desc:
                ns.run(_SET_DATASET_DESCRIPTION, uri=d["d_uri"], description=tbl_desc)
                stats["tables"] += 1

            col_descs: dict[str, str] = tbl_info.get("columns") or {}
            for c in d["columns"]:
                col_name = c["name"]
                col_desc = (col_descs.get(col_name) or "").strip()
                if col_desc:
                    ns.run(_SET_COLUMN_DESCRIPTION, uri=c["uri"], description=col_desc)
                    stats["columns"] += 1

    # Persist a text-similarity vector on every column, keyed on the FINAL text
    # (name + the description just written, or the column's prior description) and
    # the model — so feasibility reads the vector instead of recomputing it, and an
    # unchanged re-enrich / model-match is skipped. Embedding is CPU-bound, so the
    # encode + write runs OFF the event loop (asyncio.to_thread); the LLM call above
    # already yields. See research/2026-08-24-background-jobs-and-async-freeze.md.
    embed_items: list[dict] = []
    for d in datasets:
        tbl = d["table"]
        col_descs = (tables_out.get(tbl) or {}).get("columns") or {}
        for c in d["columns"]:
            col_name = c["name"]
            new_desc = (col_descs.get(col_name) or "").strip()
            final_desc = new_desc or (c.get("description") or "")
            text = schema_dna.estate_column_embed_text(col_name, final_desc)
            h = estate_mod.estate_embed_text_hash(text)
            if c.get("embedding_hash") == h and c.get("embedding_model") == embeddings.MODEL_NAME:
                continue  # unchanged text + same model — vector is current, skip
            embed_items.append({"uri": c["uri"], "text": text, "hash": h})
    if embed_items and embeddings.available():
        settings = estate_mod.estate_settings(session)
        stats["columns_embedded"] = await asyncio.to_thread(
            estate_mod.embed_and_store_columns, settings, embed_items)

    return stats


# ── main enrichment entry point ────────────────────────────────────────────────

def _persist_enrichment(
    session: Session,
    scan: EstateScan,
    *,
    progress: Optional[dict[str, Any]] = None,
    summary: Optional[dict[str, str]] = None,
    summary_source: Optional[dict[str, str]] = None,
) -> None:
    """Commit incremental enrichment progress/summary so the 2.5s poller sees it.

    ``summary_source`` records per-schema provenance (``"llm"`` | ``"fallback"``)
    alongside the descriptions — a backward-compatible extra key so existing
    readers of ``schema_descriptions`` are unaffected.
    """
    if progress is not None:
        scan.enrichment_progress_json = json.dumps(progress, default=str)
    if summary is not None:
        payload: dict[str, Any] = {"schema_descriptions": summary}
        if summary_source is not None:
            payload["schema_description_source"] = summary_source
        scan.enrichment_summary_json = json.dumps(payload, default=str)
    scan.updated_at = datetime.utcnow()
    session.add(scan)
    session.commit()


async def enrich_scan(session: Session, scan_id: int) -> dict[str, Any]:
    """Enrich all datasets + columns in a scan with LLM-generated descriptions.

    Processes one schema at a time (one LLM call per schema) to bound cost.
    Writes descriptions directly to ``:EstateColumn`` / ``:EstateDataset`` nodes.
    Updates ``EstateScan.enrichment_state`` from 'enriching' → 'enriched' | 'failed'.
    """
    scan = session.get(EstateScan, scan_id)
    if scan is None:
        return {"error": "scan_not_found"}
    source = session.get(EstateSource, scan.source_id)
    if source is None:
        return {"error": "missing_source"}

    platform, connection_ref = estate_mod.resolve_source_connection_ref(session, source)
    database = connection_ref.get("database", "") or ""

    # Ensure the native vector index exists before we write any column vectors —
    # idempotent, best-effort (mirrors business_concepts.ensure_constraints).
    estate_mod.ensure_estate_vector_index(estate_mod.estate_settings(session))

    # Enumerate distinct schemas in the scan.
    s_uri = estate_mod.scan_uri(scan_id)
    schemas: list[dict] = []
    with estate_mod.estate_graph_session(session) as ns:
        for rec in ns.run(_READ_SCAN_SCHEMAS, scan_uri=s_uri):
            schemas.append({
                "schema": rec["schema"] or "",
                "database": rec["database"] or database,
            })
    schemas = [s for s in schemas if s["schema"]]

    # Total datasets across the scan (progress denominator); falls back to 0 when
    # unavailable so the UI uses the schema ratio instead.
    try:
        tables_total = int(json.loads(scan.stats_json or "{}").get("datasets") or 0)
    except (ValueError, TypeError):
        tables_total = 0

    # Seed live progress before the loop so a poller sees totals immediately.
    progress: dict[str, Any] = {
        "schemas_total": len(schemas), "schemas_done": 0,
        "current_schema": None, "current_database": None,
        "tables_total": tables_total, "tables_done": 0,
        "columns_done": 0, "errors": [],
    }
    schema_descriptions: dict[str, str] = {}
    schema_description_sources: dict[str, str] = {}
    _persist_enrichment(session, scan, progress=progress, summary=schema_descriptions,
                        summary_source=schema_description_sources)

    total_tables = 0
    total_cols = 0
    schema_errors: list[str] = []

    for sch_info in schemas:
        sch = sch_info["schema"]
        # Announce which namespace is in-flight (level-neutral: `sch` is a database
        # name on MySQL, a schema on the others — keyed the same either way).
        progress["current_schema"] = sch
        progress["current_database"] = sch_info["database"] or database
        _persist_enrichment(session, scan, progress=progress)
        try:
            result = await _enrich_schema(
                session,
                scan_id=scan_id,
                platform=platform,
                database=sch_info["database"] or database,
                schema=sch,
            )
            total_tables += result.get("tables", 0)
            total_cols += result.get("columns", 0)
            progress["tables_done"] += result.get("dataset_count", 0)
            progress["columns_done"] += result.get("columns", 0)
            sch_desc = (result.get("schema_description") or "").strip()
            if sch_desc:
                schema_descriptions[sch] = sch_desc
                schema_description_sources[sch] = result.get("schema_description_source") or "llm"
            if result.get("errors"):
                err = f"{sch}: {'; '.join(result['errors'])}"
                schema_errors.append(err)
                progress["errors"].append(err)
        except Exception as exc:
            logger.warning("estate_enrich schema %s failed: %s", sch, exc)
            err = f"{sch}: {exc}"
            schema_errors.append(err)
            progress["errors"].append(err)
        progress["schemas_done"] += 1
        _persist_enrichment(session, scan, progress=progress, summary=schema_descriptions,
                            summary_source=schema_description_sources)

    progress["current_schema"] = None
    progress["current_database"] = None
    final_state = "enriched" if not schema_errors else ("enriched" if total_cols > 0 else "failed")
    scan.enrichment_state = final_state
    scan.enriched_at = datetime.utcnow()
    _persist_enrichment(session, scan, progress=progress, summary=schema_descriptions,
                        summary_source=schema_description_sources)

    return {
        "state": final_state,
        "schemas_processed": len(schemas),
        "tables_enriched": total_tables,
        "columns_enriched": total_cols,
        "schema_errors": schema_errors,
    }


# ── leased worker unit (same pattern as estate_scan.process_one_scan) ─────────

async def process_one_enrichment(worker_id: str, lease_ttl: int) -> bool:
    """Claim + enrich one scan whose ``enrichment_state`` is 'enriching'.

    Called by the estate_worker loop alongside scan processing. Returns True iff
    a scan was handled.
    """
    from datetime import timedelta

    with Session(engine) as session:
        row = session.exec(
            select(EstateScan).where(
                EstateScan.enrichment_state == "enriching",  # type: ignore[arg-type]
                EstateScan.state.in_(["completed", "partial"]),  # type: ignore[attr-defined]
            ).order_by(EstateScan.id)  # type: ignore[arg-type]
        ).first()
        if row is None:
            return False
        scan_id = row.id

    with Session(engine) as session:
        try:
            await enrich_scan(session, scan_id)
        except Exception as exc:
            logger.exception("estate enrichment scan %s failed", scan_id)
            scan = session.get(EstateScan, scan_id)
            if scan is not None:
                scan.enrichment_state = "failed"
                scan.updated_at = datetime.utcnow()
                session.add(scan)
                session.commit()
    return True
