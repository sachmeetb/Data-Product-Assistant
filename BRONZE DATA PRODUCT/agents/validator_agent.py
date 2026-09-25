"""
validator_agent.py — Bronze Specification Validator Agent.

Runs standards compliance, naming conventions, BigQuery syntax, and
data quality check readiness (file availability, schema drift, volume bounds, duplicate detection)
on the generated DDL and specification.

Returns a ValidationReport with per-check status and overall readiness.
"""

from __future__ import annotations
from typing import Optional
from .base import run_agent


async def run(
    ddl_script: str,
    specification: dict,
    bank_profile: dict,
    source_scope: dict,
    session_id: Optional[str] = None,
) -> dict:
    """
    Run the Validator Agent.

    Args:
        ddl_script:     BigQuery DDL script from SpecGeneratorAgent.
        specification:  Specification JSON from SpecGeneratorAgent.
        bank_profile:   BankProfile for regulatory context.
        source_scope:   SourceScope for source system and ingestion pattern context.
        session_id:     ADK session ID.

    Returns:
        ValidationReport dict:
          validation_status   — PASSED | PASSED_WITH_WARNINGS | FAILED
          checks              — list of {check_id, status, table, message}
          errors, warnings    — filtered lists
          ready_to_publish    — boolean
          summary             — human-readable result
    """
    from tools.knowledge_tool import get_validation_rules

    context = {
        "ddl_script": ddl_script,
        "specification": specification,
        "bank_profile": bank_profile,
        "source_scope": source_scope,
        "validation_rules": get_validation_rules(),
        "active_regulatory_frameworks": bank_profile.get("regulatory_frameworks", []),
        "bronze_checks": [
            "file_availability",
            "schema_drift",
            "volume_bounds",
            "duplicate_detection"
        ]
    }

    return await run_agent(
        "validator",
        "Validate the provided ddl_script and specification against all checks including bronze-specific data quality. "
        "Return a complete ValidationReport JSON.",
        context=context,
        session_id=session_id,
    )


def is_passing(output: dict) -> bool:
    """True if validation passed (with or without warnings)."""
    if not isinstance(output, dict) or "error" in output:
        return False
    status = str(output.get("validation_status", "")).upper()
    if status in ("PASSED", "PASSED_WITH_WARNINGS"):
        return True
    if output.get("ready_to_publish") is True:
        return True
    if len(get_errors(output)) == 0 and ("checks" in output or "validation_status" in output):
        return True
    return False


def get_errors(output: dict) -> list[str]:
    """Return list of error messages from validation report."""
    return [
        c.get("message", "")
        for c in output.get("checks", [])
        if c.get("status") == "FAILED"
    ]


def get_warnings(output: dict) -> list[str]:
    """Return list of warning messages from validation report."""
    return [
        c.get("message", "")
        for c in output.get("checks", [])
        if c.get("status") == "WARNING"
    ]
