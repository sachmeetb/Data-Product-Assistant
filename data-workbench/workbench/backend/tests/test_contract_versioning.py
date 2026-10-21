"""Guardrail tests for `discard_draft_version` — the one reverse versioning op.

These assert the pure-logic pre-flight checks (which run BEFORE any graph
mutation) via a fake Neo4j session that returns a controlled head-state row.
The full graph reversal is exercised e2e against a live Neo4j, not here — this
suite deliberately only covers the branch that decides *whether* a discard is
allowed, which is where the safety-critical logic lives.
"""

import pytest

from workbench.backend._contract_versioning import (
    DiscardNotAllowed,
    discard_draft_version,
)


class _Result:
    def __init__(self, row):
        self._row = row

    def single(self):
        return self._row


class _FakeNs:
    """Returns `first_row` for the first `.run()` (READ_DISCARD_STATE); any
    later `.run()` returns nothing. The guardrail cases all raise before a
    second run, so this is enough."""

    def __init__(self, first_row):
        self._first = first_row
        self.calls = 0

    def run(self, *args, **kwargs):
        self.calls += 1
        return _Result(self._first if self.calls == 1 else None)


def _row(**over):
    base = {
        "current_version": 2,
        "head_state": "draft",
        "prior_version": 1,
        "prior_state": "approved",
        "prior_name": None,
        "prior_description": None,
        "prior_purpose": None,
        "cur_name": None,
        "cur_description": None,
        "cur_purpose": None,
    }
    base.update(over)
    return base


def test_discard_rejects_missing_contract():
    with pytest.raises(DiscardNotAllowed):
        discard_draft_version(_FakeNs(None), "x-contract")


def test_discard_rejects_v1_no_prior():
    ns = _FakeNs(_row(current_version=1, prior_version=None))
    with pytest.raises(DiscardNotAllowed):
        discard_draft_version(ns, "x-contract")


def test_discard_rejects_when_prior_version_absent():
    ns = _FakeNs(_row(current_version=2, prior_version=None))
    with pytest.raises(DiscardNotAllowed):
        discard_draft_version(ns, "x-contract")


@pytest.mark.parametrize("state", ["published", "approved", "submitted", "superseded"])
def test_discard_rejects_non_draft_head(state):
    ns = _FakeNs(_row(head_state=state))
    with pytest.raises(DiscardNotAllowed):
        discard_draft_version(ns, "x-contract")


def test_discard_guardrails_pass_for_draft_branch():
    """A draft head branched from a prior version passes the guardrails and
    proceeds into the mutation phase (which then no-ops against the fake ns's
    empty later results — the point is it did NOT raise DiscardNotAllowed)."""
    ns = _FakeNs(_row(current_version=2, head_state="draft", prior_version=1))
    # Should not raise DiscardNotAllowed; may complete or raise something else
    # from the fake ns, but the guardrail gate must let it through.
    try:
        discard_draft_version(ns, "x-contract")
    except DiscardNotAllowed:
        pytest.fail("guardrails wrongly rejected a valid draft branch")
    except Exception:
        pass  # downstream fake-ns limitations are fine — guardrails passed
