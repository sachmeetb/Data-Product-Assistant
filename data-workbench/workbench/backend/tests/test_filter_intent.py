"""Type-safe filter-literal grounding (B2) — pure-Python, no Neo4j.

Locks the contract of `filter_intent._ground_predicate_literals`: it fixes casing
to the exact observed value, LOWER()-wraps an unmatched TEXT value, and NEVER
wraps a non-text column (which would be invalid SQL). Also covers the evidence
merge + text-type predicate.
"""

from workbench.backend.routers import filter_intent as fi


def _ev(**cols):
    """cols: name -> (observed_values, type_str)."""
    return {k.lower(): {"values": list(v[0]), "type": v[1]} for k, v in cols.items()}


def test_exact_casing_fix_keeps_plain_equality():
    ev = _ev(employment_status=(["active", "terminated", "on_leave"], "text"))
    out, w = fi._ground_predicate_literals("employment_status = 'Active'", ev)
    assert out == "employment_status = 'active'"
    assert w == []


def test_unmatched_text_value_is_lowered():
    ev = _ev(status=([], "varchar"))
    out, _ = fi._ground_predicate_literals("status = 'Xyz'", ev)
    assert "LOWER(status) = LOWER('Xyz')" in out


def test_non_text_column_never_lowered():
    ev = _ev(created_date=([], "date"))
    out, w = fi._ground_predicate_literals("created_date = '2024-01-01'", ev)
    assert "LOWER(" not in out
    assert out == "created_date = '2024-01-01'"
    assert any("verify" in x.lower() for x in w)


def test_existing_lower_not_double_processed():
    ev = _ev(dept=(["Sales"], "text"))
    out, _ = fi._ground_predicate_literals("LOWER(dept) = 'sales'", ev)
    assert out.upper().count("LOWER") == 1


def test_in_list_exact_case_then_lower_on_unmatched():
    ev = _ev(region=(["NA", "APAC", "EMEA"], "text"))
    out1, _ = fi._ground_predicate_literals("region IN ('NA', 'apac')", ev)
    assert "'APAC'" in out1 and "LOWER(" not in out1
    out2, _ = fi._ground_predicate_literals("region IN ('NA', 'xx')", ev)
    assert "LOWER(region) IN" in out2


def test_qualified_column_grounds_by_bare_name():
    ev = _ev(employment_status=(["active"], "text"))
    out, _ = fi._ground_predicate_literals("t2.employment_status = 'Active'", ev)
    assert out == "t2.employment_status = 'active'"


def test_unknown_column_left_untouched():
    out, _ = fi._ground_predicate_literals("mystery = 'Foo'", {})
    assert out == "mystery = 'Foo'"


def test_compound_predicate_grounds_both_sides():
    ev = _ev(
        employment_status=(["active"], "text"),
        region=(["EMEA"], "text"),
    )
    out, _ = fi._ground_predicate_literals(
        "employment_status = 'active' AND region = 'emea'", ev
    )
    assert "'active'" in out and "'EMEA'" in out


def test_build_col_evidence_merges_case_insensitively():
    ev = fi._build_col_evidence(
        [{"name": "a", "type": "text", "top_values": ["X"]}],
        [{"name": "A", "type": "", "top_values": ["Y"]}],
    )
    assert set(ev["a"]["values"]) == {"X", "Y"}
    assert ev["a"]["type"] == "text"


def test_is_text_type():
    assert fi._is_text_type("varchar(20)")
    assert fi._is_text_type("TEXT")
    assert not fi._is_text_type("date")
    assert not fi._is_text_type("integer")
    assert not fi._is_text_type("")
