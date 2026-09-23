"""
use_case_classification.py — Use Case Determinator Agent wrapper.

Receives the confirmed RequirementsOutput JSON from the Requirements Agent.
Classifies the use case into exactly one of:
  analytics | data_science | genai | digital_nosql | conformed_data | agentic

Returns dual output:
  - Structured UseCaseClassification JSON for downstream agents
  - Human-friendly classification card for user confirmation/override
"""

import copy
import json
from .base import run_agent


# Valid use case types — kept in sync with the skill enum.
VALID_USE_CASE_TYPES = (
    "analytics", "data_science", "genai",
    "digital_nosql", "conformed_data", "agentic",
)

# Deterministic mapping: use_case_type → schema_design_pattern.
# Updated in sync with SKILL.md v2.4 SCHEMA DESIGN PATTERN MAPPING table.
SCHEMA_DESIGN_PATTERNS: dict[str, str] = {
    "analytics":      "star_schema",
    "data_science":   "wide_flat_feature_table",
    "genai":          "flat_denormalised",
    "digital_nosql":  "event_schema",
    "conformed_data": "entity_schema",
    "agentic":        "flat_denormalised",
}

VALID_SCHEMA_PATTERNS = tuple(set(SCHEMA_DESIGN_PATTERNS.values()))


async def run(requirements_output: dict, context: dict = {}, session=None) -> dict:
    """
    Run the Use Case Determinator.

    Args:
        requirements_output: The confirmed RequirementsOutput dict from the
                             Requirements Agent. Must have confirmed_by_user=True.
        context: Optional dict. Currently unused but reserved for future
                 override history or session metadata.

    Returns:
        One of:
        - UseCaseClassification dict with optional 'display_output'
          containing the classification card
        - {'error': '...'} on failure or invalid input
    """
    if not requirements_output.get("confirmed_by_user", False):
        return {
            "error": "RequirementsOutput has not been confirmed by the user. "
                     "Cannot classify an unconfirmed requirement."
        }

    user_input = json.dumps(requirements_output, indent=2)
    return await run_agent("use-case-classification", user_input, context, session=session)


def is_classified(output: dict) -> bool:
    """
    Check whether the agent returned a valid, well-formed classification.

    Validates:
      - shape (use_case_type, schema_design_pattern, confidence present; no error)
      - use_case_type is one of the valid enum values
      - schema_design_pattern is consistent with use_case_type per SCHEMA_DESIGN_PATTERNS
      - confidence is a numeric value in [0.0, 1.0]
      - rationale is a non-empty string

    Returns False for outputs that look like classifications but contain
    invalid/contradictory values (e.g. confidence as a string, unknown type).
    Such outputs should NOT be passed downstream.
    """
    if "error" in output:
        return False
    if "use_case_type" not in output or "confidence" not in output:
        return False

    # Type must be a known enum value
    use_case_type = output["use_case_type"]
    if use_case_type not in VALID_USE_CASE_TYPES:
        return False

    # Schema design pattern must be present and match the expected pattern for the type.
    # If the agent omitted the field, backfill it deterministically rather than failing —
    # this ensures backward compatibility with any cached outputs from before v2.4.
    schema_pattern = output.get("schema_design_pattern")
    if not schema_pattern:
        output["schema_design_pattern"] = SCHEMA_DESIGN_PATTERNS[use_case_type]
    elif schema_pattern not in VALID_SCHEMA_PATTERNS:
        return False

    # Confidence must be a numeric in [0.0, 1.0]. Booleans pass isinstance(bool, int)
    # in Python, so explicitly reject those.
    conf = output["confidence"]
    if isinstance(conf, bool):
        return False
    if not isinstance(conf, (int, float)):
        return False
    if not (0.0 <= float(conf) <= 1.0):
        return False

    # Rationale should be a non-empty string. Soft check — agent should always
    # provide one, but we don't want to fail downstream on whitespace-only.
    rationale = output.get("rationale", "")
    if not isinstance(rationale, str) or not rationale.strip():
        return False

    return True


def apply_override(output: dict, new_type: str) -> dict:
    """
    Apply a user override to the classification.

    On override, the agent's reasoning (confidence, rationale, signals_matched)
    is no longer accurate — those fields described why the *original* type was
    chosen, not why the new type fits. Leaving them in place produces a
    contradictory classification card (e.g. "Type: data_science | Confidence:
    0.93 [for analytics] | Rationale: this is analytics because of dashboard").

    To prevent this, the override:
      - Sets use_case_type to the new value
      - Sets confidence to 1.0 (user override is authoritative)
      - Replaces rationale with an override marker
      - Clears signals_matched (the old signals don't justify the new type)
      - Sets overridden_by_user = True

    The original agent reasoning is preserved under `original_classification`
    for auditability.

    Args:
        output: The original UseCaseClassification dict
        new_type: The user's chosen type (must be one of the valid types)

    Returns:
        Updated dict (a deep copy — does NOT mutate the input), or
        {'error': '...'} if the input is malformed or new_type is invalid.
    """
    # Validate input dict shape — refuse to mutate something that isn't a
    # well-formed classification in the first place.
    if not isinstance(output, dict):
        return {"error": "Cannot apply override: classification output is not a dict."}
    if "use_case_type" not in output:
        return {
            "error": "Cannot apply override: classification output is missing "
                     "'use_case_type'. The original classification may have failed."
        }

    # Validate new_type
    if new_type not in VALID_USE_CASE_TYPES:
        return {
            "error": f"Invalid type '{new_type}'. Must be one of: "
                     f"{sorted(VALID_USE_CASE_TYPES)}"
        }

    # Deep-copy so callers don't see in-place mutation of session state.
    # This matters because server.py stores the classification in _sessions
    # and may inspect the previous value (e.g. for the override banner's
    # "changed from X to Y" message).
    updated = copy.deepcopy(output)
    previous_type = updated["use_case_type"]

    # Preserve the agent's original reasoning for audit/traceability.
    # If a previous override already created this nested record, don't
    # double-wrap — keep pointing at the agent's actual original output.
    if "original_classification" not in updated:
        updated["original_classification"] = {
            "use_case_type": previous_type,
            "confidence": output.get("confidence"),
            "rationale": output.get("rationale"),
            "signals_matched": output.get("signals_matched", []),
        }

    # Apply the override and reset reasoning fields to reflect that the
    # human, not the agent, made this call.
    updated["use_case_type"] = new_type
    updated["schema_design_pattern"] = SCHEMA_DESIGN_PATTERNS[new_type]
    updated["confidence"] = 1.0
    updated["rationale"] = (
        f"Classification overridden by user: "
        f"{previous_type} → {new_type}. "
        f"User decision is authoritative; original agent reasoning preserved "
        f"under 'original_classification'."
    )
    updated["signals_matched"] = []
    updated["overridden_by_user"] = True

    # Drop the stale display_output so any caller that re-renders builds a
    # fresh card from the updated fields rather than showing the old
    # contradictory one. (server.py rebuilds the discovery reply itself,
    # so this is belt-and-braces.)
    updated.pop("display_output", None)

    return updated