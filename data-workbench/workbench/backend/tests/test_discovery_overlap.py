"""Unit tests for the discovery-screen semantic column-overlap helpers.

Pure-logic: no embedding model, no Neo4j, no SDK. `_semantic_overlap` is tested
by injecting a hand-built ``{normalized_name: vector}`` dict (the same shape
``_embed_column_names`` produces), so the cosine/threshold/coverage logic is
exercised deterministically even in a venv without ``fastembed``. The token
fallback and verdict derivation are plain pure functions.

These guard the fix for the "0% of your intended columns" bug: a candidate whose
columns carry a prefix (``hr_employee_id``) or a synonym (``worker_id``) must now
count as covered instead of reading 0 against the bare recommended names.
"""

from __future__ import annotations

import pytest

from workbench.backend.routers import domain_catalogs as dc


# ── _normalize_column_name ───────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("hr_employee_id", "hr employee id"),
    ("employeeId", "employee id"),
    ("EmployeeID", "employee id"),
    ("first-name", "first name"),
    ("  Total__Orders ", "total orders"),
    ("email", "email"),
    ("", ""),
])
def test_normalize_column_name(raw, expected):
    assert dc._normalize_column_name(raw) == expected


# ── _semantic_overlap ────────────────────────────────────────────────────────
#
# Near-orthogonal 3-dim axes: employee=[1,0,0], name=[0,1,0], date=[0,0,1].
# Vectors are keyed by the NORMALIZED name because _semantic_overlap normalizes
# before looking them up.

_VECS = {
    "employee id": [1.0, 0.0, 0.0],
    "hr employee id": [1.0, 0.0, 0.0],       # prefixed synonym of employee id
    "worker id": [0.98, 0.0, 0.02],          # true synonym of employee id
    "first name": [0.0, 1.0, 0.0],
    "hire date": [0.0, 0.0, 1.0],
}


def test_semantic_overlap_prefix_matches():
    # hr_employee_id must cover employee_id despite the literal-0 overlap.
    pct, missing = dc._semantic_overlap(
        ["employee_id"], ["hr_employee_id"], _VECS, dc._OVERLAP_SIM_THRESHOLD,
    )
    assert pct == 100
    assert missing == []


def test_semantic_overlap_synonym_matches():
    pct, missing = dc._semantic_overlap(
        ["employee_id"], ["worker_id"], _VECS, dc._OVERLAP_SIM_THRESHOLD,
    )
    assert pct == 100
    assert missing == []


def test_semantic_overlap_unrelated_is_missing():
    pct, missing = dc._semantic_overlap(
        ["first_name"], ["employee_id", "hire_date"], _VECS, dc._OVERLAP_SIM_THRESHOLD,
    )
    assert pct == 0
    assert missing == ["first_name"]


def test_semantic_overlap_combined():
    pct, missing = dc._semantic_overlap(
        ["employee_id", "first_name"],
        ["hr_employee_id", "hire_date"],
        _VECS,
        dc._OVERLAP_SIM_THRESHOLD,
    )
    assert pct == 50
    assert missing == ["first_name"]


def test_semantic_overlap_empty_candidate_caps_missing_and_preserves_order():
    recommended = [f"col_{i}" for i in range(10)]
    pct, missing = dc._semantic_overlap(recommended, [], _VECS, dc._OVERLAP_SIM_THRESHOLD)
    assert pct == 0
    assert len(missing) == dc._MISSING_ATTR_CAP  # capped at 8
    assert missing == recommended[: dc._MISSING_ATTR_CAP]  # order preserved


def test_semantic_overlap_threshold_is_inclusive():
    # A pair whose cosine equals the threshold EXACTLY counts as present (>=).
    rec, cand = [1.0, 0.0, 0.0], [0.9, 0.0, 0.1]
    exact = dc._cosine(rec, cand)
    pct, missing = dc._semantic_overlap(
        ["a"], ["b"], {"a": rec, "b": cand}, exact,
    )
    assert pct == 100
    assert missing == []


def test_semantic_overlap_empty_recommended():
    assert dc._semantic_overlap([], ["employee_id"], _VECS, dc._OVERLAP_SIM_THRESHOLD) == (0, [])


# ── _token_overlap (no-embeddings fallback) ──────────────────────────────────


def test_token_overlap_prefix_present():
    # Jaccard {employee,id}/{hr,employee,id} = 0.67 >= 0.50 -> present, beating
    # the old literal 0.
    pct, missing = dc._token_overlap(
        ["employee_id"], ["hr_employee_id"], dc._OVERLAP_TOKEN_THRESHOLD,
    )
    assert pct == 100
    assert missing == []


def test_token_overlap_unrelated_missing():
    pct, missing = dc._token_overlap(
        ["first_name"], ["hire_date"], dc._OVERLAP_TOKEN_THRESHOLD,
    )
    assert pct == 0
    assert missing == ["first_name"]


def test_token_overlap_bare_token_does_not_overmatch():
    # {id} vs {hr,employee,id} = 1/3 = 0.33 < 0.50 -> missing (a lone `id` must
    # not match everything).
    pct, missing = dc._token_overlap(
        ["id"], ["hr_employee_id"], dc._OVERLAP_TOKEN_THRESHOLD,
    )
    assert pct == 0
    assert missing == ["id"]


def test_token_overlap_empty_candidate_caps_missing():
    recommended = [f"col_{i}" for i in range(10)]
    pct, missing = dc._token_overlap(recommended, [], dc._OVERLAP_TOKEN_THRESHOLD)
    assert pct == 0
    assert len(missing) == dc._MISSING_ATTR_CAP
    assert missing == recommended[: dc._MISSING_ATTR_CAP]


def test_token_overlap_empty_recommended():
    assert dc._token_overlap([], ["employee_id"], dc._OVERLAP_TOKEN_THRESHOLD) == (0, [])


# ── _derive_product_verdict ──────────────────────────────────────────────────


@pytest.mark.parametrize("overlap,lifecycle,expected", [
    (85, "published", "reuse"),
    (80, "approved", "reuse"),          # boundary: == threshold
    (100, "PUBLISHED", "reuse"),        # case-insensitive lifecycle
    (85, "draft", "extend"),            # high coverage but unfinished
    (85, "in_engineering", "extend"),
    (50, "published", "extend"),        # finished but low coverage
    (90, "", "extend"),                 # unknown lifecycle
])
def test_derive_product_verdict(overlap, lifecycle, expected):
    assert dc._derive_product_verdict(overlap, lifecycle) == expected
