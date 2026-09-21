"""
silver_product_engine.py — Silver Product Engine Agent.

Produces the detailed Silver-layer data product plan by composing common/ blocks
(lego bricks) into concrete BigQuery column definitions.

For each Silver table identified by the Domain Scoping Agent, this agent:
  1. Takes the selected_common_blocks list from the DomainScope
  2. Expands each block into its full column definitions (with BQ types)
  3. Adds any domain-specific columns not covered by the blocks
  4. Applies naming conventions (snake_case, prefixes for multi-money tables)
  5. Applies regulatory column requirements from the bank_profile
"""

from __future__ import annotations
import os
from typing import Optional
from .base import run_agent


async def run(
    domain_scope: dict,
    bank_profile: dict,
    structured_requirement: dict,
    session_id: Optional[str] = None,
    extra_context: Optional[dict] = None,
) -> dict:
    """
    Run the Silver Product Engine.

    Args:
        domain_scope:            Output from DomainScopingAgent (with required_tables
                                 each containing selected_common_blocks).
        bank_profile:            Output from BankProfileAgent.
        structured_requirement:  Output from RequirementUnderstandingAgent.
        session_id:              ADK session ID.
        extra_context:           Optional dict merged into context (retry feedback).

    Returns:
        SilverProductPlan dict with full per-table column specifications.
    """
    from tools.schema_loader import (
        get_common_block_catalog,
        get_domain_block_map,
        get_crosswalk,
    )
    from tools.knowledge_tool import (
        get_naming_conventions,
        get_validation_rules,
        get_banking_standards,
    )

    catalog = get_common_block_catalog()

    # ── Build per-block full column reference ─────────────────────────────────
    # For each block, expose all columns with bq_type, mode, description
    block_column_reference = {
        name: {
            "title":       blk["title"],
            "description": blk["description"],
            "usage_note":  blk["usage_note"],
            "standards":   blk["standards_alignment"],
            "columns": [
                {
                    "name":        c["name"],
                    "bq_type":     c["bq_type"],
                    "mode":        c["mode"],
                    "description": c["description"],
                    "enum_values": c.get("enum_values", []),
                }
                for c in blk["columns"]
            ],
        }
        for name, blk in catalog.items()
    }

    context = {
        "domain_scope": domain_scope,
        "bank_profile": bank_profile,
        "structured_requirement": structured_requirement,

        # ── Full common block reference with BQ types ─────────────────────────
        "common_block_reference": block_column_reference,

        # ── Domain block map for guidance ─────────────────────────────────────
        "domain_block_map": get_domain_block_map(),

        # ── Standards crosswalk ───────────────────────────────────────────────
        "standards_crosswalk": get_crosswalk(),

        # ── Naming & validation rules ─────────────────────────────────────────
        "naming_conventions": get_naming_conventions(),
        "validation_rules": {
            "global_rules": [
                f"{r['id']}: {r['name']}"
                for r in get_validation_rules().get("global_rules", [])
            ],
        },
        "gcp_project_id": os.environ.get("GCP_PROJECT_ID", "eogwapq-agbg-internal-data-mig"),
        "silver_dataset": os.environ.get("BQ_SILVER_DATASET", "banking_silver"),

        # ── Column composition rules ──────────────────────────────────────────
        "column_composition_rules": [
            "1. ALWAYS start every table with ALL columns from the 'technical-metadata' block (surrogate_key, lineage columns, versioning columns, DQ columns).",
            "2. For each selected_common_block in the DomainScope, look up its 'columns' in common_block_reference and add those columns to the table.",
            "3. If a table has multiple money fields (e.g. principal AND outstanding), prefix the money block columns: e.g. principal_amount, principal_currency, outstanding_amount, outstanding_currency.",
            "4. If a table has multiple identifiers (e.g. IBAN AND LEI), prefix the identifier block columns: e.g. iban_id_scheme, iban_id_value, lei_id_scheme, lei_id_value.",
            "5. After expanding the blocks, add any domain-specific columns NOT covered by the blocks (e.g. credit_score INT64, kyc_status STRING for party).",
            "6. Column names must be snake_case. Timestamps must end in _ts. Dates must end in _date. Codes must end in _code. Amounts must end in _amount.",
            "7. NEVER use FLOAT64 for monetary amounts — always NUMERIC.",
            "8. All technical-metadata columns (surrogate_key through is_enriched) are REQUIRED or NULLABLE as specified by the block — do not change their modes.",
            "9. Every column description should cite the common block it came from (e.g. 'From money block. ISO 20022: ActiveCurrencyAndAmount.').",
        ],
    }

    # Merge retry feedback if provided
    if extra_context:
        context.update(extra_context)

    output = await run_agent(
        "silver-product-engine",
        (
            "Using the domain_scope.required_tables and common_block_reference, "
            "expand each table's selected_common_blocks into a complete list of "
            "BigQuery columns. Follow all column_composition_rules strictly. "
            "Produce a complete SilverProductPlan JSON."
        ),
        context=context,
        session_id=session_id,
    )
    return _enrich_plan_with_block_columns(output, catalog, domain_scope)


def _enrich_plan_with_block_columns(plan: dict, catalog: dict, domain_scope: Optional[dict] = None) -> dict:
    """Ensure every table in the product plan has complete expanded block columns."""
    if not isinstance(plan, dict):
        plan = {}

    tables = plan.get("tables") or plan.get("required_tables") or plan.get("silver_tables") or []
    if (not isinstance(tables, list) or len(tables) == 0) and domain_scope and isinstance(domain_scope, dict):
        tables = domain_scope.get("required_tables", [])

    if not isinstance(tables, list) or len(tables) == 0:
        return plan

    enriched_tables = []
    for t in tables:
        if not isinstance(t, dict):
            continue
        cols = t.get("columns", [])
        if not isinstance(cols, list) or len(cols) == 0:
            blocks = t.get("selected_common_blocks", ["technical-metadata"])
            if "technical-metadata" not in blocks:
                blocks = ["technical-metadata"] + list(blocks)
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

    tables = output.get("tables") or output.get("required_tables") or output.get("silver_tables") or []
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
    tables = output.get("tables") or output.get("required_tables") or output.get("silver_tables") or []
    return [t.get("table_name", "") for t in tables if t.get("table_name")]


def get_reuse_summary(output: dict) -> str:
    """Return the block composition summary string."""
    return output.get("block_composition_summary", output.get("reuse_summary", ""))
