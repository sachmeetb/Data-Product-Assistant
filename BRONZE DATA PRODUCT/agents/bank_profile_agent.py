"""
bank_profile_agent.py — Bank Profile Agent.

Collects and structures the bank's organisational context:
  bank_name, bank_code, regions, banking_type, active_products,
  regulatory_frameworks, data_standards, and optional fields.

This is always the FIRST step in the pipeline — the BankProfile is passed
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
    res = await run_agent(
        "bank-profile",
        user_input,
        session_id=session_id,
    )
    return normalize_bank_profile(res)


def normalize_bank_profile(output: dict) -> dict:
    """Normalize extracted keys and apply standard inferences if needed."""
    if not isinstance(output, dict) or "error" in output:
        return output

    # Normalize regions
    if not output.get("regions"):
        if output.get("primary_region"):
            pr = output["primary_region"]
            output["regions"] = [r.strip() for r in str(pr).replace("/", ",").split(",") if r.strip()]
        elif output.get("region"):
            output["regions"] = [str(output["region"])]
    elif isinstance(output.get("regions"), str):
        output["regions"] = [r.strip() for r in output["regions"].replace("/", ",").split(",") if r.strip()]

    # Normalize regulatory_frameworks
    if not output.get("regulatory_frameworks"):
        if output.get("regulatory_bodies"):
            output["regulatory_frameworks"] = output["regulatory_bodies"]
        elif output.get("regulations"):
            output["regulatory_frameworks"] = output["regulations"]
    if isinstance(output.get("regulatory_frameworks"), str):
        output["regulatory_frameworks"] = [r.strip() for r in output["regulatory_frameworks"].split(",") if r.strip()]

    # Normalize active_products
    if not output.get("active_products"):
        if output.get("products"):
            output["active_products"] = output["products"]
        elif output.get("banking_type"):
            output["active_products"] = ["Deposits", "Loans", "Payments", "Cards"]
    if isinstance(output.get("active_products"), str):
        output["active_products"] = [p.strip() for p in output["active_products"].split(",") if p.strip()]

    # Normalize data_standards
    if not output.get("data_standards"):
        output["data_standards"] = ["ISO 20022", "BIAN", "Basel III"]
    if isinstance(output.get("data_standards"), str):
        output["data_standards"] = [s.strip() for s in output["data_standards"].split(",") if s.strip()]

    # Infer bank_code if missing
    if not output.get("bank_code") and output.get("bank_name"):
        words = output["bank_name"].split()
        if len(words) > 1:
            output["bank_code"] = "".join(w[0] for w in words if w.isalnum()).upper()[:6]
        else:
            output["bank_code"] = output["bank_name"][:4].upper()

    # Core banking system
    if not output.get("core_banking_system"):
        if output.get("core_banking_engine"):
            output["core_banking_system"] = output["core_banking_engine"]
        elif isinstance(output.get("core_systems"), dict):
            output["core_banking_system"] = output["core_systems"].get("core_banking", "Temenos T24")

    mandatory = ("bank_name", "bank_code", "regions", "banking_type",
                 "active_products", "regulatory_frameworks", "data_standards")
    if all(output.get(f) for f in mandatory):
        output["profile_complete"] = True

    return output


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
