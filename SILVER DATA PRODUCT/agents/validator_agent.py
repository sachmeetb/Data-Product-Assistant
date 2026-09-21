"""
validator_agent.py \u2014 Silver Specification Validator Agent.

Runs standards compliance, naming conventions, BigQuery syntax, and
regulatory guardrail checks on the generated DDL and specification.

Returns a ValidationReport with per-check status and overall readiness.
"""

from __future__ import annotations
from typing import Optional
from .base import run_agent


async def run(
    ddl_script: str,
    specification: dict,
    bank_profile: dict,
    domain_scope: dict,
    session_id: Optional[str] = None,
) -> dict:
    """
    Run the Validator Agent.

    Args:
        ddl_script:     BigQuery DDL script from SpecGeneratorAgent.
        specification:  Specification JSON from SpecGeneratorAgent.
        bank_profile:   BankProfile for regulatory context.
        domain_scope:   DomainScope for regulatory overlay determination.
        session_id:     ADK session ID.

    Returns:
        ValidationReport dict:
          validation_status   \u2014 PASSED | PASSED_WITH_WARNINGS | FAILED
          checks              \u2014 list of {check_id, status, table, message}
          errors, warnings    \u2014 filtered lists
          ready_to_publish    \u2014 boolean
          summary             \u2014 human-readable result
    """
    from tools.knowledge_tool import get_validation_rules

    context = {
        "ddl_script": ddl_script,
        "specification": specification,
        "bank_profile": bank_profile,
        "domain_scope": domain_scope,
        "validation_rules": get_validation_rules(),
        "active_regulatory_frameworks": bank_profile.get("regulatory_frameworks", []),
        "active_regions": bank_profile.get("regions", []),
    }

    return await run_agent(
        "validator",
        "Validate the provided ddl_script and specification against all checks. "
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
