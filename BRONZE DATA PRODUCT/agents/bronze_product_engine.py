"""
bronze_product_engine.py — Bronze Product Engine Agent.

Produces the detailed Bronze-layer data product plan by composing common/ blocks
(lego bricks) into concrete BigQuery column definitions.

For each Bronze table identified by the Source Scoping Agent, this agent:
  1. Takes the selected_common_blocks list from the SourceScope
  2. Expands each block into its full column definitions (with BQ types)
  3. Adds any domain-specific columns not covered by the blocks
  4. Applies naming conventions (snake_case, prefixes like raw_, brz_)
  5. Partitions by ingestion date, clusters by source system
"""

from __future__ import annotations
import os
from typing import Optional
from .base import run_agent


async def run(
    source_scope: dict,
    bank_profile: dict,
    structured_requirement: dict,
    session_id: Optional[str] = None,
    extra_context: Optional[dict] = None,
) -> dict:
    """
    Run the Bronze Product Engine.

    Args:
        source_scope:            Output from SourceScopingAgent.
        bank_profile:            Output from BankProfileAgent.
        structured_requirement:  Output from RequirementUnderstandingAgent.
        session_id:              ADK session ID.
        extra_context:           Optional dict merged into context (retry feedback).

    Returns:
        BronzeProductPlan dict with full per-table column specifications.
    """
    from tools.schema_loader import (
        get_common_block_catalog,
        get_source_block_map,
    )
    from tools.knowledge_tool import (
        get_naming_conventions,
        get_validation_rules,
    )

    catalog = get_common_block_catalog()

    # ── Build per-block full column reference ─────────────────────────────────
    block_column_reference = {
        name: {
            "title":       blk["title"],
            "description": blk["description"],
            "usage_note":  blk["usage_note"],
            "columns": [
                {
                    "name":        c["name"],
                    "bq_type":     c["bq_type"],
                    "mode":        c["mode"],
                    "description": c["description"],
                    "enum_values": c.get("enum_values", []),
                }
                for c in blk.get("columns", [])
            ],
        }
        for name, blk in catalog.items()
    }

    context = {
        "source_scope": source_scope,
        "bank_profile": bank_profile,
        "structured_requirement": structured_requirement,

        # ── Full common block reference with BQ types ─────────────────────────
        "common_block_reference": block_column_reference,

        # ── Source block map for guidance ─────────────────────────────────────
        "source_block_map": get_source_block_map(),

        # ── Naming & validation rules ─────────────────────────────────────────
        "naming_conventions": get_naming_conventions(),
        "validation_rules": {
            "global_rules": [
                f"{r['id']}: {r['name']}"
                for r in get_validation_rules().get("global_rules", [])
            ],
        },
        "gcp_project_id": os.environ.get("GCP_PROJECT_ID", "eogwapq-agbg-internal-data-mig"),
        "bronze_dataset": os.environ.get("BQ_BRONZE_DATASET", "banking_bronze"),

        # ── Column composition rules ──────────────────────────────────────────
        "column_composition_rules": [
            "1. ALWAYS start every table with ALL columns from the 'ingestion-metadata' block.",
            "2. Ensure the 'raw-payload' block columns are included (e.g. raw_data, record_hash).",
            "3. For each selected_common_block in the SourceScope, look up its 'columns' in common_block_reference and add those columns to the table.",
            "4. Column names must be snake_case.",
            "5. The raw payload should typically be STRING or JSON to preserve source format.",
            "6. Every column description should cite the common block it came from (e.g. 'From ingestion-metadata block.').",
        ],
    }

    # Merge retry feedback if provided
    if extra_context:
        context.update(extra_context)

    output = await run_agent(
        "bronze-product-engine",
        (
            "Using the source_scope.required_tables and common_block_reference, "
            "expand each table's selected_common_blocks into a complete list of "
            "BigQuery columns. Follow all column_composition_rules strictly. "
            "Produce a complete BronzeProductPlan JSON."
        ),
        context=context,
        session_id=session_id,
    )
    return _enrich_plan_with_block_columns(output, catalog, source_scope)


def _enrich_plan_with_block_columns(plan: dict, catalog: dict, source_scope: Optional[dict] = None) -> dict:
    """Ensure every table in the product plan has complete expanded block columns."""
    if not isinstance(plan, dict):
        plan = {}

    tables = plan.get("tables") or plan.get("required_tables") or plan.get("bronze_tables") or []
    if (not isinstance(tables, list) or len(tables) == 0) and source_scope and isinstance(source_scope, dict):
        tables = source_scope.get("required_tables", [])

    if not isinstance(tables, list) or len(tables) == 0:
        return plan

    enriched_tables = []
    for t in tables:
        if not isinstance(t, dict):
            continue
        cols = t.get("columns", [])
        if not isinstance(cols, list) or len(cols) == 0:
            blocks = t.get("selected_common_blocks", ["ingestion-metadata", "raw-payload"])
            if "ingestion-metadata" not in blocks:
                blocks = ["ingestion-metadata"] + list(blocks)
            expanded_cols = []
            for blk_name in blocks:
                blk_def = catalog.get(blk_name)
                if not blk_def:
                    continue
                for c in blk_def.get("columns", []):
                    expanded_cols.append({
                        "name": c["name"],
                        "bq_type": c["bq_type"],
                        "mode": c["mode"],
                        "description": c.get("description", f"From {blk_name} block"),
                        "source_block": blk_name,
                    })
            t["columns"] = expanded_cols
        enriched_tables.append(t)

    plan["tables"] = enriched_tables
    if "raw_output" in plan and len(enriched_tables) > 0:
        del plan["raw_output"]
    return plan


def is_complete(output: dict) -> bool:
    """True if the product plan has at least one table with columns."""
    if not isinstance(output, dict):
        return False
    if "error" in output or "raw_output" in output:
        return False

    tables = output.get("tables") or output.get("required_tables") or output.get("bronze_tables") or []
    if not isinstance(tables, list) or len(tables) == 0:
        return False

    # Every table must have columns
    for t in tables:
        cols = t.get("columns", [])
        if not isinstance(cols, list) or len(cols) == 0:
            return False

    return True


def get_table_names(output: dict) -> list[str]:
    """Return list of table names from the product plan."""
    tables = output.get("tables") or output.get("required_tables") or output.get("bronze_tables") or []
    return [t.get("table_name", "") for t in tables if t.get("table_name")]


def get_reuse_summary(output: dict) -> str:
    """Return the block composition summary string."""
    return output.get("block_composition_summary", output.get("reuse_summary", ""))
