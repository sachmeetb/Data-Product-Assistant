"""
domain_scoping_agent.py — Domain Scoping Agent.

Maps a StructuredRequirement to:
  - BIAN service domains
  - Required common blocks per Silver table (not pre-built entity schemas)
  - Regulatory overlays
  - Data relationships

The agent composes Silver tables from common/ blocks (lego bricks), not from
entities/ pre-built schemas. The domain_block_map tells the agent which blocks
are standard for each table type.
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
    Run the Domain Scoping Agent.

    Args:
        structured_requirement: Output from RequirementUnderstandingAgent.
        bank_profile:           Output from BankProfileAgent.
        session_id:             ADK session ID.
        extra_context:          Optional dict merged into the agent context
                                (used by the retry loop for correction feedback).

    Returns:
        DomainScope dict with:
          bian_service_domains, required_tables (each with selected_common_blocks),
          regulatory_requirements, data_relationships, scope_notes.
    """
    from tools.knowledge_tool import (
        get_banking_ontology,
        get_bian_domains,
        get_regulatory_requirements,
    )
    from tools.schema_loader import (
        get_common_block_catalog,
        get_domain_block_map,
    )

    catalog = get_common_block_catalog()

    # Compact block summary — name, title, usage_note, column names only (not full defs)
    # Full column defs are injected in Silver Product Engine (next step)
    block_summary = {
        name: {
            "title":      blk["title"],
            "usage_note": blk["usage_note"],
            "columns":    [c["name"] for c in blk["columns"]],
            "standards":  blk["standards_alignment"],
        }
        for name, blk in catalog.items()
    }

    context = {
        "structured_requirement": structured_requirement,
        "bank_profile": bank_profile,
        "banking_ontology_domains": list(
            get_banking_ontology()
            .get("domain_hierarchy", {})
            .get("banking", {})
            .get("sub_domains", {})
            .keys()
        ),
        "bian_service_domains": get_bian_domains(),
        "regulatory_frameworks": bank_profile.get("regulatory_frameworks", []),

        # ── Common block catalog (the lego bricks) ────────────────────────────
        "common_block_catalog_summary": block_summary,

        # ── Domain → recommended blocks map ──────────────────────────────────
        "domain_block_map": get_domain_block_map(),

        # ── Composition instruction ───────────────────────────────────────────
        "composition_rules": (
            "Silver tables are assembled from common blocks. "
            "For each required Silver table, select the common blocks needed from "
            "common_block_catalog_summary. ALWAYS include 'technical-metadata'. "
            "Use domain_block_map as guidance. "
            "Do NOT reference entities/ folder — those pre-built schemas are not used."
        ),
    }

    # Add regulatory requirements
    reg_reqs = get_regulatory_requirements(
        bank_profile.get("regulatory_frameworks", [])
    )
    if reg_reqs:
        context["regulatory_requirements_reference"] = reg_reqs

    # Merge retry feedback if provided
    if extra_context:
        context.update(extra_context)

    return await run_agent(
        "domain-scoping",
        (
            "Analyse the structured_requirement and bank_profile. "
            "Select the required Silver tables and for each table select the "
            "common blocks from common_block_catalog_summary that are needed. "
            "Return a DomainScope JSON."
        ),
        context=context,
        session_id=session_id,
    )


def is_complete(output: dict) -> bool:
    """True if domain scope has the expected shape — required_tables with selected_common_blocks."""
    if not isinstance(output, dict):
        return False
    if "error" in output or "raw_output" in output:
        return False

    tables = output.get("required_tables", [])
    if not isinstance(tables, list) or len(tables) == 0:
        return False

    # Every table must have selected_common_blocks (list) and technical-metadata must be included
    for t in tables:
        blocks = t.get("selected_common_blocks", [])
        if not isinstance(blocks, list) or len(blocks) == 0:
            return False
        if "technical-metadata" not in blocks:
            return False

    return True


def get_recommended_tables(output: dict) -> list[str]:
    """Return list of Silver table names from scope output."""
    return [t.get("table_name", "") for t in output.get("required_tables", []) if t.get("table_name")]


def get_regulatory_overlays(output: dict) -> dict:
    """Return regulatory requirements from scope output."""
    return output.get("regulatory_requirements", {})
