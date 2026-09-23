"""
challenger.py — Challenger Agent wrapper.

Runs automatically after the user confirms discovery. Acts as a devil's
advocate — evaluating whether the DPI outputs (requirements, classification,
discovery) are internally consistent and whether any matched existing data
products genuinely and fully meet the stated requirement.

Returns:
  - ChallengerReview dict:  verdict, checks, summary, design_queue
  - design_queue is the ordered handoff list for the Data Designer workstream:
      curated  → Bronze/Silver source tables (upstream, design first)
      enriched → Gold consumption tables (downstream, design second)
"""

import json
from .base import run_agent

VERDICT_VALUES = ("clean", "concerns", "blockers")


async def run(
    discovery_output: dict,
    requirements: dict,
    classification: dict,
    session=None,
) -> dict:
    """
    Run the Challenger Agent.

    Args:
        discovery_output: Full DiscoveryResult dict from discovery.run().
        requirements:     Confirmed RequirementsOutput dict.
        classification:   UseCaseClassification dict.
        session:          MAF AgentSession (optional).

    Returns:
        ChallengerReview dict or {"error": "..."} on failure.
    """
    payload = {
        "requirements": requirements,
        "classification": classification,
        "discovery": discovery_output,
    }
    user_input = json.dumps(payload, indent=2, default=str)
    return await run_agent("challenger", user_input, {}, session=session)


def is_complete(output: dict) -> bool:
    """Return True if the output is a well-formed ChallengerReview."""
    if "error" in output or "raw_output" in output:
        return False
    return (
        output.get("verdict") in VERDICT_VALUES
        and isinstance(output.get("checks"), list)
        and len(output.get("checks", [])) == 5
        and isinstance(output.get("summary"), str)
        and isinstance(output.get("design_queue"), dict)
        and "curated" in output["design_queue"]
        and "enriched" in output["design_queue"]
    )


def build_challenger_view(output: dict) -> dict:
    """Extract the frontend card payload from a ChallengerReview dict."""
    return {
        "verdict": output.get("verdict", "concerns"),
        "checks": output.get("checks", []),
        "summary": output.get("summary", ""),
        "design_queue": output.get("design_queue", {"curated": [], "enriched": []}),
    }
