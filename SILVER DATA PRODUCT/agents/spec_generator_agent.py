"""
spec_generator_agent.py \u2014 Silver Specification & DDL Generator Agent.

Takes the SilverProductPlan and generates:
  1. A complete, executable BigQuery Standard SQL DDL script
  2. A formal specification JSON document

The DDL is ready to execute in BigQuery with no modifications.
"""

from __future__ import annotations
import os
from typing import Optional
from .base import run_agent


async def run(
    silver_product_plan: dict,
    bank_profile: dict,
    session_id: Optional[str] = None,
    prior_spec: Optional[dict] = None,
    feedback: Optional[str] = None,
) -> dict:
    """
    Run the Spec Generator Agent.

    Args:
        silver_product_plan: Output from SilverProductEngine.
        bank_profile:        Output from BankProfileAgent.
        session_id:          ADK session ID.
        prior_spec:          Previous spec output (for refinement iterations).
        feedback:            User feedback or validation errors (for retry).

    Returns:
        Dict with:
          ddl_script     \u2014 complete BigQuery DDL as a string
          specification  \u2014 formal spec JSON
    """
    from tools.knowledge_tool import get_naming_conventions
    from tools.schema_loader import get_crosswalk, get_common_block_catalog

    catalog       = os.environ.get("DATABRICKS_CATALOG", "main")
    silver_schema = os.environ.get("DATABRICKS_SILVER_SCHEMA", "banking_silver")
    gold_schema   = os.environ.get("DATABRICKS_GOLD_SCHEMA",  "banking_gold")

    # Block-level standards alignment (for DDL COMMENT descriptions)
    block_standards = {
        name: blk["standards_alignment"]
        for name, blk in get_common_block_catalog().items()
    }

    context: dict = {
        "silver_product_plan": silver_product_plan,
        "bank_profile": bank_profile,
        "databricks_config": {
            "catalog":       catalog,
            "silver_schema": silver_schema,
            "gold_schema":   gold_schema,
        },
        "naming_conventions": get_naming_conventions(),
        "standards_crosswalk": get_crosswalk(),
        "block_standards_alignment": block_standards,
        "ddl_rules": [
            "Use Azure Databricks SQL (Unity Catalog + Delta Lake) only — no BigQuery syntax.",
            "Table names: CREATE TABLE IF NOT EXISTS `{catalog}`.`{schema}`.`{table_name}` USING DELTA",
            "Partitioning / Clustering: PARTITIONED BY (...) or CLUSTER BY (surrogate_key) as appropriate for Delta Lake.",
            "COMMENT '...' must cite: BIAN service domain, ISO 20022 message, FIBO entity, and common blocks used.",
            "Column comments must cite the common block source, e.g. 'From money block. ISO 20022: ActiveCurrencyAndAmount. ISO 4217 currency.'.",
            "NEVER use DOUBLE for monetary or rate values — always DECIMAL(18, 4).",
            "Use TIMESTAMP for date-time values.",
            "surrogate_key is always the first column. Technical-metadata columns follow. Business columns come last.",
        ],
    }

    if feedback and prior_spec:
        context["prior_spec"] = prior_spec
        context["feedback"] = feedback
        prompt = (
            "The previous DDL specification had issues. "
            f"Feedback: {feedback}\n\n"
            "Apply all corrections and return the complete, fixed DDL script "
            "and updated specification JSON."
        )
    elif feedback:
        context["feedback"] = feedback
        prompt = (
            "Regenerate the DDL script and specification applying this feedback: "
            f"{feedback}"
        )
    else:
        prompt = (
            "Generate the complete BigQuery Standard SQL DDL script and formal "
            "specification JSON for the provided SilverProductPlan. "
            "Use the bigquery_config for project/dataset names. "
            "Output ONLY valid BigQuery Standard SQL \u2014 no Databricks, no Azure syntax."
        )

    return await run_agent(
        "spec-generator",
        prompt,
        context=context,
        session_id=session_id,
    )


def is_complete(output: dict) -> bool:
    """True if output has a non-empty DDL script and specification."""
    if not isinstance(output, dict) or "error" in output:
        return False
    ddl = output.get("ddl_script") or ""
    if not isinstance(ddl, str) or len(ddl.strip()) < 50:
        return False
    spec = output.get("specification")
    if not (isinstance(spec, (dict, list)) or (isinstance(spec, str) and len(spec.strip()) > 10)):
        return False
    return True


def get_ddl(output: dict) -> str:
    """Extract the DDL script string from spec generator output."""
    return output.get("ddl_script", "")


def get_specification(output: dict) -> dict:
    """Extract the specification dict from spec generator output."""
    return output.get("specification", {})
