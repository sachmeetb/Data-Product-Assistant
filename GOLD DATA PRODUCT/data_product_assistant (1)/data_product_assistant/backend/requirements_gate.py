"""
requirements_gate.py — PURE decision logic for the DPI "requirements" turn.

This module has **zero third-party imports** and no dependency on the agent
runner, the model client, FastAPI, or Redis. That is deliberate: the one piece
of logic that dead-locked the live demo now lives here, where it can be
unit-tested in isolation — fast, offline, and without credentials.

──────────────────────────────────────────────────────────────────────────────
Why this exists (the bug it fixes)
──────────────────────────────────────────────────────────────────────────────
The requirements step used to advance to the confirmation card only when BOTH
`is_complete(result)` AND the model's own `handoff_ready` flag were true. When a
user answered a clarifying question vaguely / evasively / off-topic, the model
would return a *complete shape* but with `handoff_ready = false` (one or two
fields left as `needs_clarification` / `unknown_per_user`). The server then fell
into its "clarification" branch, emitted an **empty chip list**, and relayed the
JSON as if it were a question — so the user saw a message with **no question, no
card, and no button**. Nothing to click, nothing to answer: a hard dead end.
The only code that could explain the block (`get_blocking_mandatories`) lived
*behind* the confirmation card that never rendered.

`decide_next_step()` removes that state entirely:

  * SHOW_CARD  — as soon as the JSON *shape* is complete we show the summary +
                 "Yep, that reads right" / "Let me tweak this" chips, regardless
                 of `handoff_ready`. If a mandatory field is still missing, the
                 confirm handler blocks with a clear message + an Edit chip, so
                 the user can always tweak and move on.
  * CLARIFY    — shape not complete yet: the agent is genuinely asking a
                 question, and the text box is itself a way forward.
  * CLARIFY_WITH_ESCAPE — shape still not complete after ≥2 rounds (the user
                 keeps not giving the agent what it asked for): surface a manual
                 Edit escape hatch so the loop can ALWAYS be broken by hand.

Invariant guaranteed by this module: **every requirements turn yields an
actionable affordance** — either chips, or a real question the user can answer.
There is no "complete-but-silent" dead end anymore.
"""

# Locked mandatory field set — these three drive whether handoff can proceed.
# Everything else is optional / enriching and never blocks.
MANDATORY_FIELDS = ("use_case_name", "domain", "data_points")

# Historical field names for the "data points" concept (skill v2.8 renamed
# `kpis` → `data_points`; older cached outputs may still use `kpis`).
_DATA_POINTS_ALIASES = ("data_points", "kpis")

# User-facing labels for blocking-field messages (never the raw JSON keys).
_MANDATORY_LABELS = {
    "use_case_name": "use case name",
    "domain": "business domain",
    "data_points": "data points / attributes",
}

# ── Decision outcomes ─────────────────────────────────────────────────────────
ACTION_SHOW_CARD = "show_card"                    # → dpi_confirm_req + confirm/tweak chips
ACTION_CLARIFY = "clarify"                         # → stay clarifying, plain question
ACTION_CLARIFY_WITH_ESCAPE = "clarify_with_escape" # → stay clarifying + manual Edit escape

# After this many clarification rounds without a complete shape, always offer
# the manual-edit escape hatch so the user can never get stuck in a loop.
ESCAPE_AFTER_PASS = 2


def _get_data_points(result: dict):
    """Return the data_points list, falling back to the legacy `kpis` key."""
    for key in _DATA_POINTS_ALIASES:
        if key in result and result[key] is not None:
            return result[key]
    return None


def is_shape_complete(result: dict) -> bool:
    """
    True when `result` is a structured RequirementsOutput (has the mandatory
    keys) rather than a bare clarification question or an error.

    SHAPE check only — it does not judge whether the values are usable; use
    `mandatory_complete()` / `get_blocking_mandatories()` for that. Mirrors
    agents.requirement_understanding.is_complete so the two never drift.
    """
    if not isinstance(result, dict):
        return False
    return (
        "use_case_name" in result
        and _get_data_points(result) is not None
        and "raw_output" not in result
        and "error" not in result
    )


def mandatory_complete(result: dict) -> bool:
    """
    True when every mandatory field has a usable value: present, non-empty, not
    marked unknown_per_user, and not in needs_clarification.
    """
    if not is_shape_complete(result):
        return False

    field_status = result.get("field_status", {}) or {}
    unknown = set(field_status.get("unknown_per_user", []) or [])
    needs_clarif = set(field_status.get("needs_clarification", []) or [])

    for field in MANDATORY_FIELDS:
        aliases = _DATA_POINTS_ALIASES if field == "data_points" else (field,)
        if any(a in unknown for a in aliases):
            return False
        if any(a in needs_clarif for a in aliases):
            return False
        value = _get_data_points(result) if field == "data_points" else result.get(field)
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
        if isinstance(value, list) and len(value) == 0:
            return False
    return True


def get_blocking_mandatories(result: dict) -> list:
    """
    User-facing labels for the mandatory fields still blocking handoff.
    Empty list ⇒ nothing blocking (safe to hand off).
    """
    if mandatory_complete(result):
        return []
    if not is_shape_complete(result):
        return [_MANDATORY_LABELS[f] for f in MANDATORY_FIELDS]

    field_status = result.get("field_status", {}) or {}
    unknown = set(field_status.get("unknown_per_user", []) or [])
    needs_clarif = set(field_status.get("needs_clarification", []) or [])

    blocking = []
    for field in MANDATORY_FIELDS:
        aliases = _DATA_POINTS_ALIASES if field == "data_points" else (field,)
        if any(a in unknown for a in aliases) or any(a in needs_clarif for a in aliases):
            blocking.append(_MANDATORY_LABELS[field])
            continue
        value = _get_data_points(result) if field == "data_points" else result.get(field)
        if value is None or (isinstance(value, str) and not value.strip()) \
                or (isinstance(value, list) and len(value) == 0):
            blocking.append(_MANDATORY_LABELS[field])
    return blocking


def decide_next_step(result: dict, clarification_pass: int) -> dict:
    """
    Decide what the server should do with a requirements-agent `result`.

    Args:
        result:            the (already error-checked, already coerced) agent output.
        clarification_pass: how many clarification rounds have happened so far
                            (0 = the user's very first message).

    Returns a dict:
        {
          "action":  ACTION_SHOW_CARD | ACTION_CLARIFY | ACTION_CLARIFY_WITH_ESCAPE,
          "blocking": [labels]   # only for SHOW_CARD — mandatories still missing
        }

    The decision is intentionally simple and total (every input maps to an
    actionable outcome), which is the whole point: no branch can leave the user
    with nothing to do.
    """
    if is_shape_complete(result):
        return {"action": ACTION_SHOW_CARD, "blocking": get_blocking_mandatories(result)}
    if clarification_pass >= ESCAPE_AFTER_PASS:
        return {"action": ACTION_CLARIFY_WITH_ESCAPE, "blocking": []}
    return {"action": ACTION_CLARIFY, "blocking": []}
