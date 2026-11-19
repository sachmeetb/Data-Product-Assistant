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

    project_id     = os.environ.get("GCP_PROJECT_ID", "eogwapq-agbg-internal-data-mig")
    silver_dataset = os.environ.get("BQ_SILVER_DATASET", "banking_silver")
    gold_dataset   = os.environ.get("BQ_GOLD_DATASET",  "banking_gold")

    # Block-level standards alignment (for DDL OPTIONS descriptions)
    block_standards = {
        name: blk["standards_alignment"]
        for name, blk in get_common_block_catalog().items()
    }

    context: dict = {
        "silver_product_plan": silver_product_plan,
        "bank_profile": bank_profile,
        "bigquery_config": {
            "project_id":     project_id,
            "silver_dataset": silver_dataset,
            "gold_dataset":   gold_dataset,
            "location":       os.environ.get("BQ_LOCATION", "US"),
        },
        "naming_conventions": get_naming_conventions(),
        "standards_crosswalk": get_crosswalk(),
        "block_standards_alignment": block_standards,
        "ddl_rules": [
            "Use BigQuery Standard SQL only — no Databricks, no Azure SQL syntax.",
            "Table names: CREATE TABLE IF NOT EXISTS `{project}.{dataset}.{table_name}`",
            "Partition: PARTITION BY DATE(silver_load_ts)",
            "Cluster: CLUSTER BY surrogate_key (add business key if applicable)",
            "OPTIONS(description = '...') must cite: BIAN service domain, ISO 20022 message, FIBO entity, and common blocks used.",
            "Column descriptions must cite the common block source, e.g. 'From money block. ISO 20022: ActiveCurrencyAndAmount. ISO 4217 currency.'.",
            "NEVER use FLOAT64 for monetary or rate values — always NUMERIC.",
            "NEVER use DATETIME — always TIMESTAMP for date-time values.",
            "Surrogate_key is always the first column. Technical-metadata columns follow. Business columns come last.",
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
