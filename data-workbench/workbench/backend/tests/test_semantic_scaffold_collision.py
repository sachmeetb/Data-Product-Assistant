"""Regression tests for the concept name-collision resolution (tier-aware).

Root cause of the "Employee entity disappeared" bug: the recommend step promoted
a cross-product concept named "Employee" as an *attribute* with
if_exists='deprecate_existing', and the level-blind collision check deprecated the
scaffolded "Employee" *entity*. The fix makes collision resolution tier-aware — a
higher-tier concept is never deprecated for a lower-tier one (it's coreferenced).

These exercise the pure decision helper `_collision_action` (no Neo4j needed).
"""
from __future__ import annotations

import pytest

from workbench.backend.business_concepts import _collision_action, _norm_name
from workbench.backend.entity_scaffolding import _pick_cross_product_bindings


class TestHrBugScenario:
    def test_attribute_promotion_never_clobbers_entity(self):
        # THE bug: recommend promotes an 'Employee' attribute while an 'Employee'
        # entity exists. Must coreference (protect the entity), NOT deprecate it.
        assert _collision_action("entity", "attribute", "deprecate_existing") == "coreference"

    def test_entity_protected_even_when_if_exists_is_fail(self):
        # A higher tier is protected unconditionally — if_exists is irrelevant.
        assert _collision_action("entity", "attribute", "fail") == "coreference"

    def test_value_promotion_never_clobbers_entity_or_attribute(self):
        assert _collision_action("entity", "value", "deprecate_existing") == "coreference"
        assert _collision_action("attribute", "value", "deprecate_existing") == "coreference"


class TestSameTierUnchanged:
    def test_same_tier_replace_still_works(self):
        assert _collision_action("attribute", "attribute", "deprecate_existing") == "replace"
        assert _collision_action("entity", "entity", "deprecate_existing") == "replace"
        assert _collision_action("value", "value", "deprecate_existing") == "replace"

    def test_same_tier_fail_still_raises_path(self):
        assert _collision_action("attribute", "attribute", "fail") == "fail"
        assert _collision_action("entity", "entity", "fail") == "fail"


class TestPromoteOver:
    def test_lower_tier_can_be_upgraded_when_replacing(self):
        # A stray lower-tier concept can be upgraded to a higher tier on an
        # explicit replace (e.g. recovering an entity that got demoted).
        assert _collision_action("attribute", "entity", "deprecate_existing") == "promote_over"
        assert _collision_action("value", "attribute", "deprecate_existing") == "promote_over"

    def test_promote_over_requires_explicit_replace(self):
        assert _collision_action("attribute", "entity", "fail") == "fail"


@pytest.mark.parametrize("dup_level,new_level", [
    ("entity", "attribute"), ("entity", "value"), ("attribute", "value"),
])
def test_higher_tier_is_never_deprecated(dup_level, new_level):
    # No if_exists value may ever deprecate a higher tier.
    for mode in ("deprecate_existing", "fail"):
        assert _collision_action(dup_level, new_level, mode) == "coreference"


class TestNameNormalization:
    """P2: singular/plural + case folding lets 'employees' (consumer) coreference
    the 'Employee' (source) entity instead of spawning a duplicate concept."""

    def test_plural_folds_to_singular(self):
        assert _norm_name("Employees") == _norm_name("employee") == "employee"
        assert _norm_name("Departments") == "department"
        assert _norm_name("Categories") == "category"
        assert _norm_name("Addresses") == "address"

    def test_case_and_whitespace_insensitive(self):
        assert _norm_name("  Performance   Reviews ") == "performance review"

    def test_does_not_overfold_ss_words(self):
        # 'class' / 'address' / 'status' must not lose meaningful characters.
        assert _norm_name("Class") == "class"
        assert _norm_name("Address") == "address"
        assert _norm_name("Status") == "status"

    def test_short_words_untouched(self):
        assert _norm_name("ID") == "id"
        assert _norm_name("OS") == "os"


class TestCrossProductBindingPicker:
    """Phase A: a consumer dataset coreferences a source dataset ONLY on provable
    majority lineage, and a mixed (junction) dataset is never guessed."""

    def test_clear_majority_binds(self):
        rows = [{"consumer_ds_uri": "c", "source_ds_uri": "A", "mapped_cols": 4, "total_cols": 4}]
        assert _pick_cross_product_bindings(rows) == {"c": "A"}

    def test_strict_max_above_ratio_binds(self):
        rows = [
            {"consumer_ds_uri": "c", "source_ds_uri": "A", "mapped_cols": 3, "total_cols": 4},
            {"consumer_ds_uri": "c", "source_ds_uri": "B", "mapped_cols": 1, "total_cols": 4},
        ]
        assert _pick_cross_product_bindings(rows) == {"c": "A"}

    def test_below_ratio_skips(self):
        rows = [{"consumer_ds_uri": "c", "source_ds_uri": "A", "mapped_cols": 1, "total_cols": 4}]
        assert _pick_cross_product_bindings(rows) == {}

    def test_tie_is_never_guessed(self):
        # A junction/blend that maps equally to two sources → left unbound.
        rows = [
            {"consumer_ds_uri": "c", "source_ds_uri": "A", "mapped_cols": 2, "total_cols": 4},
            {"consumer_ds_uri": "c", "source_ds_uri": "B", "mapped_cols": 2, "total_cols": 4},
        ]
        assert _pick_cross_product_bindings(rows) == {}

    def test_independent_consumers(self):
        rows = [
            {"consumer_ds_uri": "c1", "source_ds_uri": "A", "mapped_cols": 5, "total_cols": 5},
            {"consumer_ds_uri": "c2", "source_ds_uri": "B", "mapped_cols": 3, "total_cols": 3},
        ]
        assert _pick_cross_product_bindings(rows) == {"c1": "A", "c2": "B"}

    def test_empty(self):
        assert _pick_cross_product_bindings([]) == {}
