"""
requirements_agent.py — Requirements Agent wrapper.

Receives a natural-language business requirement from a technical/functional user.
Validates completeness against the mandatory field set (use_case_name, domain, kpis).

Workflow (per skill v2.5):
  - Pass 0: agent opens with a coaching frame (if input is brief) or extracts fields
            and asks Phase A (business) questions for missing mandatories
  - Pass 1: agent re-extracts; if mandatories resolved, asks Phase B (technical)
            questions for missing optionals; otherwise targeted Phase A retry
  - Pass 2+: agent emits RequirementsOutput regardless of remaining gaps,
             flags missing fields, sets handoff_ready accordingly

Returns dual output:
  - Structured JSON (RequirementsOutput) for downstream agents — includes
    `handoff_ready` boolean and field_status with `unknown_per_user`
  - Human-friendly summary table for user confirmation
"""

from .base import run_agent


# Locked mandatory field set per spec — these drive handoff_ready.
# All other fields are optional/enriching and do not block handoff.
#
# Note: skill v2.8 renamed `kpis` → `data_points`. The wrapper accepts either
# field name to remain compatible with cached agent outputs from older skills.
# When both are present, `data_points` wins.
MANDATORY_FIELDS = ("use_case_name", "domain", "data_points")

# Field names the agent has used historically for the same concept.
# Used for backward-compat lookups in is_complete / mandatory_complete.
_DATA_POINTS_ALIASES = ("data_points", "kpis")


def _get_data_points(output: dict):
    """Return the data_points list from the output, falling back to kpis if present."""
    for key in _DATA_POINTS_ALIASES:
        if key in output and output[key] is not None:
            return output[key]
    return None


async def run(user_input: str, context: dict = {}, session=None) -> dict:
    """
    Run the Requirements Agent.

    Args:
        user_input: The user's requirement text, or a follow-up answer to a
                    clarification request.
        context:    Dict passed verbatim to the agent. Expected keys:
                      - original_input: str         (the user's FIRST message; required for STEP 0)
                      - conversation_history: list  ([{agent_question, user_answer}, ...] in skill format)
                      - clarification_pass: int     (0, 1, 2, ...)
                      - prior_output: dict          (optional — the previous RequirementsOutput
                                                     when the user is editing a confirmed summary)

    Returns:
        One of:
        - Structured RequirementsOutput dict (when stopping condition is met)
          with optional 'display_output' containing the summary table
        - {'raw_output': '...'} containing batched clarification questions
        - {'error': '...'} on failure
    """
    return await run_agent("requirement-understanding", user_input, context, session=session)


def is_complete(output: dict) -> bool:
    """
    Check whether the agent returned a structured RequirementsOutput
    (as opposed to a clarification request or error).

    NOTE: This is a SHAPE check only — it returns True for any output that
    has the expected JSON keys, regardless of whether the values are usable.
    For content completeness use `mandatory_complete()` or `is_handoff_ready()`.

    Accepts either `data_points` (skill v2.8+) or `kpis` (older skills) as
    the data-points list field name.
    """
    return (
        "use_case_name" in output
        and _get_data_points(output) is not None
        and "raw_output" not in output
        and "error" not in output
    )


def is_confirmed(output: dict) -> bool:
    """Check whether the user has explicitly confirmed the requirement summary."""
    return output.get("confirmed_by_user", False) is True


def is_handoff_ready(output: dict) -> bool:
    """
    Check whether the agent has indicated this output is safe to hand off
    to downstream agents. Reads the agent-emitted `handoff_ready` flag.

    Falls back to `mandatory_complete()` for backward-compat with older
    skill versions that don't emit the flag.
    """
    if "handoff_ready" in output:
        return bool(output["handoff_ready"])
    return mandatory_complete(output)


def mandatory_complete(output: dict) -> bool:
    """
    Content check: verify that all mandatory fields have usable values.

    A mandatory field is considered usable when:
      - It is present and non-empty (non-null, non-empty-string, non-empty-list)
      - It is NOT marked as unknown_per_user (you cannot proceed without a domain)
      - It is NOT in needs_clarification

    This is the gate downstream agents should respect — even if the user
    confirms the summary, an output where mandatories are missing or marked
    unknown should NOT trigger Use Case Determinator / Discovery / etc.

    Note: mandatory field "data_points" matches either the new `data_points`
    key or the legacy `kpis` key in both the value lookup and field_status
    sets — older agent outputs may still emit `kpis`.
    """
    if not is_complete(output):
        return False

    field_status = output.get("field_status", {})
    unknown = set(field_status.get("unknown_per_user", []))
    needs_clarif = set(field_status.get("needs_clarification", []))

    for field in MANDATORY_FIELDS:
        # For the data_points field, check both new and legacy names
        # in field_status — older skills referenced "kpis" there.
        aliases = _DATA_POINTS_ALIASES if field == "data_points" else (field,)

        # Mandatory field marked unknown by user → not usable
        if any(a in unknown for a in aliases):
            return False
        # Mandatory field flagged as needing clarification → not usable
        if any(a in needs_clarif for a in aliases):
            return False
        # Mandatory field has empty/null value → not usable
        if field == "data_points":
            value = _get_data_points(output)
        else:
            value = output.get(field)
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
        if isinstance(value, list) and len(value) == 0:
            return False

    return True


def get_missing_fields(output: dict) -> list[str]:
    """
    Extract the list of fields still needing clarification.
    Does NOT include fields the user marked as unknown — those are resolved.
    """
    field_status = output.get("field_status", {})
    return field_status.get("needs_clarification", [])


def get_unknown_fields(output: dict) -> list[str]:
    """Extract fields the user explicitly marked as unknown."""
    field_status = output.get("field_status", {})
    return field_status.get("unknown_per_user", [])


def get_blocking_mandatories(output: dict) -> list[str]:
    """
    Return user-friendly labels for mandatory fields that are blocking handoff.
    Used to surface 'cannot proceed because X is missing' messages to the user.

    Returns the labels that should be displayed to the user — NOT the internal
    JSON field names. This way the gate message reads "data points / attributes"
    instead of "data_points" or "kpis".
    """
    # User-facing labels for each mandatory field (used in gate messages).
    labels = {
        "use_case_name": "use case name",
        "domain": "business domain",
        "data_points": "data points / attributes",
    }

    if mandatory_complete(output):
        return []
    if not is_complete(output):
        return [labels[f] for f in MANDATORY_FIELDS]

    field_status = output.get("field_status", {})
    unknown = set(field_status.get("unknown_per_user", []))
    needs_clarif = set(field_status.get("needs_clarification", []))

    blocking = []
    for field in MANDATORY_FIELDS:
        # For data_points, check both new and legacy field names in field_status.
        aliases = _DATA_POINTS_ALIASES if field == "data_points" else (field,)
        if any(a in unknown for a in aliases) or any(a in needs_clarif for a in aliases):
            blocking.append(labels[field])
            continue
        if field == "data_points":
            value = _get_data_points(output)
        else:
            value = output.get(field)
        if value is None:
            blocking.append(labels[field])
        elif isinstance(value, str) and not value.strip():
            blocking.append(labels[field])
        elif isinstance(value, list) and len(value) == 0:
            blocking.append(labels[field])
    return blocking