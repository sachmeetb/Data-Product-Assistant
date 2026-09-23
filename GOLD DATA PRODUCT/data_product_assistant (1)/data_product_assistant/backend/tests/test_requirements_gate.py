"""
test_requirements_gate.py — exhaustive tests for the requirements dead-lock fix.

Runs TWO ways:
  * plain:  python tests/test_requirements_gate.py     (no deps — used in CI-less envs)
  * pytest: pytest tests/test_requirements_gate.py -q  (when pytest is installed)

Everything here targets requirements_gate.py, the pure module that decides what
the server does on a requirements turn. The scenarios deliberately include the
"user doesn't give the agent what it asked for" cases — vague answers, "I don't
know", off-topic replies, and multi-round loops — because those are exactly what
dead-locked the live demo.

THE GOLDEN INVARIANT (asserted across a large input matrix):
    For every possible agent result and clarification-pass count, decide_next_step
    returns an action that leaves the user with an affordance — either the summary
    card (chips), a manual-edit escape hatch (chips), or a genuine question (text
    box). There is NO "complete-but-silent" dead end.
"""

import os
import sys

# Make the backend root importable whether run from repo root or tests/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from requirements_gate import (  # noqa: E402
    decide_next_step,
    is_shape_complete,
    mandatory_complete,
    get_blocking_mandatories,
    ACTION_SHOW_CARD,
    ACTION_CLARIFY,
    ACTION_CLARIFY_WITH_ESCAPE,
    ESCAPE_AFTER_PASS,
)

# Chip labels the frontend (App.jsx) recognises as "open the Edit form".
# Kept here as a guard: if the escape-hatch chip ever stops matching one of
# these, the manual-edit escape silently breaks. Cross-boundary contract test.
FRONTEND_EDIT_CHIP_LABELS = {"Edit", "Let me tweak this", "Let me tweak one"}
ESCAPE_CHIP_LABEL = "Let me tweak this"
CONFIRM_CHIPS = ["Yep, that reads right", "Let me tweak this"]


# ── Builders for realistic agent outputs ──────────────────────────────────────

def _dp(name, kind="kpi", is_derived=False):
    return {"name": name, "kind": kind, "is_derived": is_derived, "description": f"{name} desc"}


def complete_ready():
    """The happy path: full brief, agent says it's ready."""
    return {
        "use_case_name": "Daily Campaign Engagement Summary",
        "domain": "Marketing",
        "data_points": [_dp("Impressions"), _dp("Clicks"), _dp("CTR", is_derived=True)],
        "consumer_role": "Campaign managers",
        "data_freshness": "daily",
        "handoff_ready": True,
        "field_status": {"needs_clarification": [], "unknown_per_user": []},
    }


def complete_not_ready_optional_pending():
    """
    THE BUG SHAPE. Mandatories are all fine, but an *optional* stayed in
    needs_clarification, so the model set handoff_ready=false. This used to
    produce chips=[] and a silent dead end.
    """
    return {
        "use_case_name": "Daily Campaign Engagement Summary",
        "domain": "Marketing",
        "data_points": [_dp("Impressions"), _dp("Clicks")],
        "handoff_ready": False,
        "field_status": {
            "needs_clarification": ["data_freshness"],   # optional, not mandatory
            "unknown_per_user": [],
        },
    }


def complete_missing_domain():
    """Complete shape (has use_case_name + data_points) but domain is empty."""
    return {
        "use_case_name": "Some Report",
        "domain": "",
        "data_points": [_dp("Revenue")],
        "handoff_ready": False,
        "field_status": {"needs_clarification": [], "unknown_per_user": []},
    }


def complete_domain_unknown():
    """User said 'I don't know' to the domain question (a mandatory)."""
    return {
        "use_case_name": "Some Report",
        "domain": "Marketing",
        "data_points": [_dp("Revenue")],
        "handoff_ready": False,
        "field_status": {"needs_clarification": [], "unknown_per_user": ["domain"]},
    }


def complete_empty_datapoints():
    """data_points present as a key but empty — user never named a metric."""
    return {
        "use_case_name": "Some Report",
        "domain": "Marketing",
        "data_points": [],
        "handoff_ready": False,
        "field_status": {"needs_clarification": ["data_points"], "unknown_per_user": []},
    }


def complete_legacy_kpis():
    """Older skill emitted `kpis` instead of `data_points` — must still count."""
    return {
        "use_case_name": "Legacy Report",
        "domain": "Sales",
        "kpis": [_dp("Units Sold")],
        "handoff_ready": True,
        "field_status": {"needs_clarification": [], "unknown_per_user": []},
    }


def bare_question():
    """Agent asked a clarifying question and returned no structured shape."""
    return {"raw_output": "Which metrics matter most for your morning check?"}


def raw_plus_shape_uncoerced():
    """
    A shape mixed with raw_output that has NOT been coerced yet. is_shape_complete
    must reject it (raw_output present) — the server coerces before deciding.
    """
    return {
        "use_case_name": "X",
        "data_points": [_dp("Y")],
        "raw_output": "one more thing…",
    }


# ── Tiny simulation of the server's decision→affordance mapping ────────────────
# Mirrors _handle_dpi_chat so we can assert the end-user always has something
# to do. If this mapping and the server ever diverge, the golden-invariant test
# below still protects the pure decision; this just models the consequence.

def simulate_affordance(result, clarification_pass):
    decision = decide_next_step(result, clarification_pass)
    action = decision["action"]
    if action == ACTION_SHOW_CARD:
        return {"chips": list(CONFIRM_CHIPS), "question": None}
    if action == ACTION_CLARIFY_WITH_ESCAPE:
        return {"chips": [ESCAPE_CHIP_LABEL], "question": None}
    # ACTION_CLARIFY — no chips, but there is a real question in the text box
    q = result.get("raw_output") or result.get("display_output") or ""
    return {"chips": [], "question": q}


# ══════════════════════════════════════════════════════════════════════════════
#  TESTS
# ══════════════════════════════════════════════════════════════════════════════

def test_happy_path_shows_card():
    d = decide_next_step(complete_ready(), 0)
    assert d["action"] == ACTION_SHOW_CARD
    assert d["blocking"] == []


def test_regression_complete_but_not_ready_is_not_a_deadend():
    """
    The exact bug: complete shape + handoff_ready=false. It MUST show the card
    (never fall into the silent clarify branch). Mandatories are fine, so nothing
    blocks on confirm.
    """
    r = complete_not_ready_optional_pending()
    d = decide_next_step(r, 0)
    assert d["action"] == ACTION_SHOW_CARD, "complete-but-not-ready must show the card, not dead-end"
    assert d["blocking"] == [], "only an optional was pending — no mandatory should block"


def test_complete_but_not_ready_at_every_pass():
    """handoff_ready=false must show the card at pass 0, 1, 2, 5 — always."""
    for p in (0, 1, 2, 5, 12):
        d = decide_next_step(complete_not_ready_optional_pending(), p)
        assert d["action"] == ACTION_SHOW_CARD, f"pass {p} regressed to a non-card action"


def test_missing_domain_shows_card_with_block():
    d = decide_next_step(complete_missing_domain(), 1)
    assert d["action"] == ACTION_SHOW_CARD
    assert "business domain" in d["blocking"]


def test_domain_marked_unknown_blocks():
    d = decide_next_step(complete_domain_unknown(), 1)
    assert d["action"] == ACTION_SHOW_CARD
    assert "business domain" in d["blocking"]


def test_empty_datapoints_blocks():
    d = decide_next_step(complete_empty_datapoints(), 1)
    assert d["action"] == ACTION_SHOW_CARD
    assert "data points / attributes" in d["blocking"]


def test_legacy_kpis_key_recognised():
    assert is_shape_complete(complete_legacy_kpis())
    d = decide_next_step(complete_legacy_kpis(), 0)
    assert d["action"] == ACTION_SHOW_CARD
    assert d["blocking"] == []


def test_bare_question_early_passes_stay_clarify():
    assert decide_next_step(bare_question(), 0)["action"] == ACTION_CLARIFY
    assert decide_next_step(bare_question(), 1)["action"] == ACTION_CLARIFY


def test_loop_gets_escape_hatch_after_two_passes():
    """
    The 'user keeps not giving what the agent asked for' loop. After ESCAPE_AFTER_PASS
    rounds without a complete shape, the manual-edit escape MUST appear so the
    user can never be trapped.
    """
    assert decide_next_step(bare_question(), ESCAPE_AFTER_PASS)["action"] == ACTION_CLARIFY_WITH_ESCAPE
    assert decide_next_step(bare_question(), ESCAPE_AFTER_PASS + 3)["action"] == ACTION_CLARIFY_WITH_ESCAPE


def test_vague_multiturn_conversation_always_advances_or_escapes():
    """
    Simulate a whole conversation of unhelpful answers — vague, 'I don't know',
    off-topic. Every single turn must leave the user with an affordance, and by
    the third unhelpful round they get the escape hatch.
    """
    unhelpful_turns = [
        (bare_question(), 0),   # "Which metrics?" — user: "the mornings are chaos"
        (bare_question(), 1),   # re-ask — user: "idk, whatever's useful"
        (bare_question(), 2),   # re-ask — user: "can you just do it"
        (bare_question(), 3),   # still nothing concrete
    ]
    saw_escape = False
    for result, p in unhelpful_turns:
        aff = simulate_affordance(result, p)
        has_affordance = bool(aff["chips"]) or bool(aff["question"].strip())
        assert has_affordance, f"pass {p}: user was left with nothing to do"
        if aff["chips"] == [ESCAPE_CHIP_LABEL]:
            saw_escape = True
    assert saw_escape, "a looping vague conversation never surfaced the escape hatch"


def test_raw_output_mixed_shape_is_not_complete_until_coerced():
    r = raw_plus_shape_uncoerced()
    assert not is_shape_complete(r), "raw_output present ⇒ not a finished shape (server coerces first)"
    # Untouched, it is treated as a clarification (with escape after 2 passes).
    assert decide_next_step(r, 0)["action"] == ACTION_CLARIFY
    assert decide_next_step(r, 2)["action"] == ACTION_CLARIFY_WITH_ESCAPE


def test_escape_chip_label_is_recognised_by_frontend():
    """Cross-boundary contract: the escape chip must open the Edit form in App.jsx."""
    assert ESCAPE_CHIP_LABEL in FRONTEND_EDIT_CHIP_LABELS


def test_confirm_tweak_chip_also_recognised_by_frontend():
    assert "Let me tweak this" in FRONTEND_EDIT_CHIP_LABELS


# ── Predicate-level tests ──────────────────────────────────────────────────────

def test_is_shape_complete_variants():
    assert is_shape_complete(complete_ready())
    assert is_shape_complete(complete_legacy_kpis())
    assert not is_shape_complete(bare_question())
    assert not is_shape_complete({"error": "boom"})
    assert not is_shape_complete({})
    assert not is_shape_complete({"use_case_name": "x"})  # no data_points


def test_mandatory_complete_true_only_when_all_present_and_clean():
    assert mandatory_complete(complete_ready())
    assert not mandatory_complete(complete_missing_domain())
    assert not mandatory_complete(complete_domain_unknown())
    assert not mandatory_complete(complete_empty_datapoints())


def test_needs_clarification_on_mandatory_blocks_but_optional_does_not():
    base = complete_ready()
    # optional pending → still complete
    base_opt = dict(base, handoff_ready=False,
                    field_status={"needs_clarification": ["consumer_role"], "unknown_per_user": []})
    assert mandatory_complete(base_opt), "an optional in needs_clarification must NOT block"
    # mandatory pending → blocks
    base_mand = dict(base, field_status={"needs_clarification": ["data_points"], "unknown_per_user": []})
    assert not mandatory_complete(base_mand)


def test_get_blocking_mandatories_labels_and_empty():
    assert get_blocking_mandatories(complete_ready()) == []
    assert get_blocking_mandatories(complete_missing_domain()) == ["business domain"]
    blocks = get_blocking_mandatories({})  # nothing at all
    assert set(blocks) == {"use case name", "business domain", "data points / attributes"}


def test_whitespace_only_values_treated_as_missing():
    r = {
        "use_case_name": "   ",
        "domain": "Marketing",
        "data_points": [_dp("X")],
        "field_status": {"needs_clarification": [], "unknown_per_user": []},
    }
    assert "use case name" in get_blocking_mandatories(r)


# ── The golden invariant: no dead ends, ever ───────────────────────────────────

def test_golden_invariant_no_deadend_across_matrix():
    """
    Brute-force a wide matrix of result shapes × pass counts and assert the user
    ALWAYS has an affordance. This is the guarantee that the demo can't lock up
    on the requirements panel again.
    """
    result_variants = [
        complete_ready(),
        complete_not_ready_optional_pending(),
        complete_missing_domain(),
        complete_domain_unknown(),
        complete_empty_datapoints(),
        complete_legacy_kpis(),
        bare_question(),
        raw_plus_shape_uncoerced(),
        {},                                   # empty
        {"raw_output": ""},                   # empty question
        {"display_output": "here you go"},    # display only, no shape
        {"use_case_name": "only-name"},       # partial shape
        {"data_points": [_dp("only-dp")]},    # partial shape (no name)
        {"domain": "Marketing"},              # partial
    ]
    known_actions = {ACTION_SHOW_CARD, ACTION_CLARIFY, ACTION_CLARIFY_WITH_ESCAPE}
    for result in result_variants:
        for p in range(0, 6):
            d = decide_next_step(result, p)
            assert d["action"] in known_actions, f"unknown action for {result} @pass {p}"
            aff = simulate_affordance(result, p)
            has_affordance = bool(aff["chips"]) or bool((aff["question"] or "").strip())
            # The ONLY case with no chips + empty question is a truly empty result
            # at pass < ESCAPE. That still isn't a lock: the frontend leaves the
            # text box open, and by pass ESCAPE the escape hatch appears. Assert
            # that escape kicks in for the empty/questionless case.
            if not has_affordance:
                assert p < ESCAPE_AFTER_PASS, (
                    f"questionless result {result} left no affordance at pass {p} "
                    f"(escape should have triggered)"
                )
                # And confirm escape DOES arrive once we reach the threshold.
                later = simulate_affordance(result, ESCAPE_AFTER_PASS)
                assert later["chips"] == [ESCAPE_CHIP_LABEL]


# ── Plain-python runner (no pytest needed) ─────────────────────────────────────

def _run_all():
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    passed, failed = 0, []
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, exc))
            print(f"  FAIL  {name}: {exc!r}")
    print(f"\n{passed}/{len(tests)} passed, {len(failed)} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
