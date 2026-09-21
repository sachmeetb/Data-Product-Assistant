"""
bank_profile_agent.py \u2014 Bank Profile Agent.

Collects and structures the bank's organisational context:
  bank_name, bank_code, regions, banking_type, active_products,
  regulatory_frameworks, data_standards, and optional fields.

This is always the FIRST step in the pipeline \u2014 the BankProfile is passed
to every downstream agent as context.
"""

from __future__ import annotations
from typing import Optional
from .base import run_agent


async def run(
    user_input: str,
    session_id: Optional[str] = None,
) -> dict:
    """
    Run the Bank Profile Agent.

    Args:
        user_input:  Natural-language description of the bank OR a partial/full
                     bank profile JSON string.
        session_id:  ADK session ID for multi-turn conversations.

    Returns:
        BankProfile dict with at minimum:
          bank_name, bank_code, regions, banking_type, active_products,
          regulatory_frameworks, data_standards, profile_complete (bool).
    """
    return await run_agent(
        "bank-profile",
        user_input,
        session_id=session_id,
    )


def is_complete(output: dict) -> bool:
    """Return True if the bank profile has all mandatory fields filled."""
    mandatory = ("bank_name", "bank_code", "regions", "banking_type",
                 "active_products", "regulatory_frameworks", "data_standards")
    return (
        isinstance(output, dict)
        and output.get("profile_complete", False) is True
        and all(output.get(f) for f in mandatory)
        and "error" not in output
        and "raw_output" not in output
    )


def get_missing_fields(output: dict) -> list[str]:
    """Return list of missing mandatory fields."""
    mandatory = ("bank_name", "bank_code", "regions", "banking_type",
                 "active_products", "regulatory_frameworks", "data_standards")
    return [f for f in mandatory if not output.get(f)]
