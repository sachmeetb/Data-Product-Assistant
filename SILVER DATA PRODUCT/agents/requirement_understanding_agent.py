"""
requirement_understanding_agent.py \u2014 Requirement Understanding Agent.

Multi-turn agent that converts a natural-language banking requirement into
a structured StructuredRequirement JSON. Asks clarifying questions when
mandatory fields (use_case_name, banking_domain, data_points) are missing.

Workflow (mirrors the existing backend pattern but adapted for banking):
  Pass 0: Extract what is possible; ask Phase A (business) questions.
  Pass 1: Re-extract; if mandatories resolved \u2192 handoff_ready=True.
  Pass 2+: Return output regardless, flag missing fields.
"""

from __future__ import annotations
from typing import Optional
from .base import run_agent

MANDATORY_FIELDS = ("domain", "data_points")


def ensure_use_case_name(output: dict) -> dict:
    """Auto-assign a suitable use case name if not provided by user."""
    if isinstance(output, dict) and "raw_output" not in output and "error" not in output:
        if not output.get("use_case_name"):
            domain = output.get("domain") or output.get("banking_domain") or "Banking"
            domain_title = str(domain).replace("_", " ").title()
            output["use_case_name"] = f"{domain_title} Silver Schema & Analytics"
    return output


async def run(
    user_input: str,
    context: Optional[dict] = None,
    session_id: Optional[str] = None,
) -> dict:
    """
    Run the Requirement Understanding Agent.

    Args:
        user_input:  The user's requirement text or follow-up answer.
        context:     Dict with optional keys:
                       - bank_profile: BankProfile dict
                       - clarification_pass: int (0, 1, 2...)
                       - conversation_history: list of prior Q&A
                       - prior_output: previous StructuredRequirement
        session_id:  ADK session ID for multi-turn.

    Returns:
        StructuredRequirement dict (with handoff_ready bool) OR
        {'raw_output': '...'} containing clarification questions.
    """
    result = await run_agent(
        "requirement-understanding",
        user_input,
        context=context,
        session_id=session_id,
    )
    return ensure_use_case_name(result)


def is_complete(output: dict) -> bool:
    """Shape check: True if output looks like a StructuredRequirement."""
    return (
        isinstance(output, dict)
        and ("domain" in output or "banking_domain" in output)
        and "data_points" in output
        and "raw_output" not in output
        and "error" not in output
    )


def is_handoff_ready(output: dict) -> bool:
    """True when the agent flagged the output as safe to pass downstream."""
    if "handoff_ready" in output and output.get("handoff_ready") is True:
        return True
    return mandatory_complete(output)


def mandatory_complete(output: dict) -> bool:
    """True when all mandatory fields have non-empty values."""
    if not is_complete(output):
        return False
    field_status = output.get("field_status", {})
    missing = set(field_status.get("missing", []))
    for field in MANDATORY_FIELDS:
        if field in missing:
            return False
        val = output.get(field)
        if field == "domain" and val is None:
            val = output.get("banking_domain")
        if val is None:
            return False
        if isinstance(val, str) and not val.strip():
            return False
        if isinstance(val, list) and len(val) == 0:
            return False
    return True


def get_missing_fields(output: dict) -> list[str]:
    """Return list of mandatory fields still missing."""
    if "raw_output" in output or "error" in output:
        return list(MANDATORY_FIELDS)
    field_status = output.get("field_status", {})
    missing = set(field_status.get("missing", []))
    res = []
    for field in MANDATORY_FIELDS:
        if field in missing:
            res.append(field)
            continue
        val = output.get(field)
        if field == "domain" and val is None:
            val = output.get("banking_domain")
        if val is None or (isinstance(val, str) and not val.strip()) or (isinstance(val, list) and len(val) == 0):
            res.append(field)
    return res

