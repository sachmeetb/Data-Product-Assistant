"""Every pack must load, in either inherited format, with mappable types."""

import pytest

from core.model.types import CanonicalType
from packs.spec import PackRegistry

ALL_KEYS = [e.key for e in PackRegistry().entries()]
ALL_BLOCKS = [
    "code-value", "contact-point", "identifier", "money", "party-name",
    "postal-address", "quantity", "rate", "technical-metadata", "temporal",
]


def test_registry_has_all_industries(registry):
    industries = {e.industry for e in registry.entries()}
    assert industries == {"cpg", "banking", "healthcare", "retail", "cross_industry"}
    assert len(registry.entries()) == 12


@pytest.mark.parametrize("key", ALL_KEYS)
def test_pack_loads(registry, key):
    """Covers both formats: unversioned CPG packs and schema_version 2.0 packs."""
    domain = registry.load_domain(key)
    assert domain.tables, f"{key} has no tables"
    assert domain.industry
    assert all(t.attributes for t in domain.tables), f"{key} has an empty table"


@pytest.mark.parametrize("key", ALL_KEYS)
def test_pack_types_are_canonical(registry, key):
    for table in registry.load_domain(key).tables:
        for attr in table.attributes:
            assert isinstance(attr.canonical_type, CanonicalType)


def test_derived_metrics_normalised_across_formats(registry):
    """CPG declares a dict, 2.0 packs declare a list. Both land as a dict."""
    cpg = registry.load_domain("cpg.campaign")
    two_oh = registry.load_domain("banking.retail_banking")

    assert "CTR" in cpg.derived_metrics
    assert cpg.derived_metrics["CTR"]["formula"] == "clicks / impressions"

    assert "NPA_Rate" in two_oh.derived_metrics
    assert "formula" in two_oh.derived_metrics["NPA_Rate"]


def test_cpg_enrichments_are_preserved(registry):
    """The legacy format is richer; those extras must not be dropped on load."""
    campaign = registry.load_domain("cpg.campaign")
    assert campaign.source_mappings, "source_platform_mappings lost"
    assert "channel" in campaign.value_normalisation
    assert campaign.keywords


def test_load_domain_returns_independent_instances(registry):
    """A design mutates its domains, so two loads must not share state."""
    first = registry.load_domain("banking.retail_banking")
    second = registry.load_domain("banking.retail_banking")

    assert first is not second
    assert first.tables[0] is not second.tables[0]

    first.tables[0].attributes.clear()
    first.tables.append(second.tables[0])

    assert second.tables[0].attributes, "mutating one instance leaked into the other"
    assert len(second.tables) != len(first.tables)


def test_detection_scores_and_ranks(registry):
    hits = registry.detect(
        "we need campaign impressions, clicks and roas by placement"
    )
    assert hits
    assert hits[0].entry.key == "cpg.campaign"
    assert hits == sorted(hits, key=lambda h: h.score, reverse=True)


def test_detection_below_threshold_returns_nothing(registry):
    assert registry.detect("hello there") == []


def test_banking_detection(registry):
    hits = registry.detect("reconcile iban and swift bic for each account statement")
    assert "banking.retail_banking" in {h.entry.key for h in hits}


@pytest.mark.parametrize("name", ALL_BLOCKS)
def test_block_expands(blocks, name):
    attrs = blocks.expand(name)
    if blocks.is_definitions_only(name):
        assert not attrs
        assert blocks.defs(name)
    else:
        assert attrs
        assert all(isinstance(a.canonical_type, CanonicalType) for a in attrs)


def test_block_prefixing(blocks):
    attrs = blocks.expand("money", prefix="txn_")
    assert {a.name for a in attrs} == {"txn_amount", "txn_currency", "txn_scale"}


def test_wire_type_override(blocks):
    """money.amount is a string on the wire but DECIMAL in the warehouse."""
    amount = next(a for a in blocks.expand("money") if a.name == "amount")
    assert amount.canonical_type is CanonicalType.DECIMAL


def test_union_type_is_nullable(blocks):
    scale = next(a for a in blocks.expand("money") if a.name == "scale")
    assert scale.canonical_type is CanonicalType.INT
    assert scale.nullable is True


def test_internal_defs_pointer(blocks):
    """identifier.scheme is `$ref: #/$defs/Scheme` -- a scalar enum, one column."""
    attrs = {a.name: a for a in blocks.expand("identifier")}
    assert attrs["scheme"].canonical_type is CanonicalType.STRING
    assert attrs["scheme"].nullable is False
    assert attrs["scheme_name"].nullable is True


def test_nested_object_ref_is_prefixed(blocks):
    """postal-address nests, so refs flatten into prefixed columns."""
    attrs = blocks.expand("postal-address")
    assert len(attrs) > 10
    assert any("_" in a.name for a in attrs)
