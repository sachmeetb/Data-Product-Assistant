"""
source_scoping_agent.py — Source Scoping Agent.

Maps a StructuredRequirement to:
  - Source systems (core banking, payments, CRM, etc.)
  - File formats (CSV, JSON, Parquet, XML, AVRO)
  - Ingestion patterns (batch, streaming, CDC, API extract)
  - Required Bronze landing tables with selected common blocks

The agent composes Bronze tables from ingestion-metadata blocks (ingestion-metadata,
source-identifier, file-metadata, raw-payload, quality-flags, lineage-tracking, temporal).
"""

from __future__ import annotations
from typing import Optional
from .base import run_agent


async def run(
    structured_requirement: dict,
    bank_profile: dict,
    session_id: Optional[str] = None,
    extra_context: Optional[dict] = None,
) -> dict:
    """
    Run the Source Scoping Agent.

    Args:
        structured_requirement: Output from RequirementUnderstandingAgent.
        bank_profile:           Output from BankProfileAgent.
        session_id:             ADK session ID.
        extra_context:          Optional dict merged into the agent context
                                (used by the retry loop for correction feedback).

    Returns:
        SourceScope dict with:
          source_systems, file_formats, ingestion_patterns,
          required_tables (each with selected_common_blocks),
          scope_notes.
    """
    from tools.schema_loader import (
        get_common_block_catalog,
        get_source_block_map,
    )
    from tools.knowledge_tool import get_regulatory_requirements

    catalog = get_common_block_catalog()

    block_summary = {
        name: {
            "title":      blk["title"],
            "usage_note": blk["usage_note"],
            "columns":    [c["name"] for c in blk.get("columns", [])],
        }
        for name, blk in catalog.items()
    }

    context = {
        "structured_requirement": structured_requirement,
        "bank_profile": bank_profile,
        "supported_file_formats": ["CSV", "JSON", "Parquet", "XML", "AVRO"],
        "supported_ingestion_patterns": ["batch", "streaming", "CDC", "API extract"],
        
        # ── Common block catalog (Bronze legos) ─────────────────────────────
        "common_block_catalog_summary": block_summary,

        # ── Source → recommended blocks map ──────────────────────────────────
        "source_block_map": get_source_block_map(),

        # ── Composition instruction ───────────────────────────────────────────
        "composition_rules": (
            "Bronze tables are assembled from common ingestion blocks. "
            "For each required Bronze table, select the common blocks needed from "
            "common_block_catalog_summary. ALWAYS include 'ingestion-metadata' and 'raw-payload'. "
            "Use source_block_map as guidance."
        ),
    }

    # Add regulatory requirements if needed for masking etc
    reg_reqs = get_regulatory_requirements(
        bank_profile.get("regulatory_frameworks", [])
    )
    if reg_reqs:
        context["regulatory_requirements_reference"] = reg_reqs

    # Merge retry feedback if provided
    if extra_context:
        context.update(extra_context)

    return await run_agent(
        "source-scoping",
        (
            "Analyse the structured_requirement and bank_profile. "
            "Identify the source systems, file formats, and ingestion patterns. "
            "Select the required Bronze tables and for each table select the "
            "common blocks from common_block_catalog_summary that are needed. "
            "Return a SourceScope JSON."
        ),
        context=context,
        session_id=session_id,
    )


def is_complete(output: dict) -> bool:
    """True if source scope has the expected shape — required_tables with selected_common_blocks."""
    if not isinstance(output, dict):
        return False
    if "error" in output or "raw_output" in output:
        return False

    tables = output.get("required_tables", [])
    if not isinstance(tables, list) or len(tables) == 0:
        return False

    # Every table must have selected_common_blocks (list) and ingestion-metadata must be included
    for t in tables:
        blocks = t.get("selected_common_blocks", [])
        if not isinstance(blocks, list) or len(blocks) == 0:
            return False
        if "ingestion-metadata" not in blocks:
            return False

    return True


def get_recommended_tables(output: dict) -> list[str]:
    """Return list of Bronze table names from scope output."""
    return [t.get("table_name", "") for t in output.get("required_tables", []) if t.get("table_name")]

def get_source_systems(output: dict) -> list[str]:
    """Return list of source systems identified."""
    return output.get("source_systems", [])

def get_ingestion_patterns(output: dict) -> list[str]:
    """Return list of ingestion patterns identified."""
    return output.get("ingestion_patterns", [])
