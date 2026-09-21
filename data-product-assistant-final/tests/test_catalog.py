"""The edit engine: reconciling the designed tree against live BigQuery.

Neither predecessor could do this. SILVER performed zero BigQuery reads and
fabricated its data-availability report.
"""

import pytest

from core.catalog.live import (
    BigQueryCatalog, InMemoryCatalog, InvalidIdentifierError, LiveColumn, LiveTable,
)
from core.catalog.reconcile import (
    AddableDomain, apply_addable_attribute, apply_addable_table,
    apply_addable_domain, reconcile,
)
from core.model.provenance import Origin
from core.model.tree import DataProductDesign, EntityType, Layer, PhysicalRef
from core.model.types import CanonicalType


def _col(name, ctype=CanonicalType.STRING, nullable=True, description=""):
    return LiveColumn(
        name=name, canonical_type=ctype, nullable=nullable, description=description
    )


@pytest.fixture
def design(registry):
    d = DataProductDesign()
    domain = registry.load_domain("banking.retail_banking")
    d.domains.append(domain)
    domain.table("slv_account").physical_ref = PhysicalRef(
        dataset="banking_silver", table="slv_account"
    )
    return d


@pytest.fixture
def snapshot(design):
    account = design.domain("banking.retail_banking").table("slv_account")
    live = LiveTable(
        dataset="banking_silver",
        name="slv_account",
        columns=[_col(a.name, a.canonical_type) for a in account.attributes]
        + [
            _col("branch_code", description="Owning branch sort code"),
            _col("risk_band", description="Internal risk band"),
        ],
    )
    orphan = LiveTable(
        dataset="banking_silver",
        name="slv_standing_order",
        columns=[_col("so_id", nullable=False), _col("amount", CanonicalType.DECIMAL)],
    )
    return InMemoryCatalog([live, orphan]).snapshot(["banking_silver"])


def test_column_present_in_bigquery_but_absent_from_design_is_addable(design, snapshot):
    delta = reconcile(design, snapshot)
    assert {a.column.name for a in delta.addable_attributes} == {
        "branch_code", "risk_band"
    }


def test_addable_attribute_carries_real_type_and_description(design, snapshot):
    delta = reconcile(design, snapshot)
    branch = next(a for a in delta.addable_attributes if a.column.name == "branch_code")
    assert branch.column.description == "Owning branch sort code"
    assert branch.physical == "banking_silver.slv_account"
    assert branch.path.endswith("slv_account/branch_code")


def test_applying_addable_attribute_locks_it_as_ground_truth(design, snapshot):
    delta = reconcile(design, snapshot)
    addable = next(a for a in delta.addable_attributes if a.column.name == "branch_code")

    added = apply_addable_attribute(design, addable, who="tester")

    assert added.provenance.origin is Origin.LIVE_CATALOG
    assert added.provenance.locked is True
    assert added.source_ref is not None
    assert design.domain("banking.retail_banking").table("slv_account").has("branch_code")


def test_applied_attribute_survives_regeneration(design, snapshot):
    """A pulled-in catalog column must not be clobbered by the next LLM pass."""
    delta = reconcile(design, snapshot)
    addable = next(a for a in delta.addable_attributes if a.column.name == "branch_code")
    apply_addable_attribute(design, addable)

    table = design.domain("banking.retail_banking").table("slv_account")
    from core.model.tree import Table

    regenerated = Table(
        name="slv_account", layer=Layer.SILVER, entity_type=EntityType.DIMENSION,
        attributes=[],
    )
    report = table.merge(regenerated)

    assert table.has("branch_code")
    assert "slv_account/branch_code" in report.preserved


def test_table_present_in_bigquery_but_absent_from_design_is_addable(design, snapshot):
    delta = reconcile(design, snapshot)
    assert [t.live.name for t in delta.addable_tables] == ["slv_standing_order"]


def test_applying_addable_table_brings_all_columns(design, snapshot):
    delta = reconcile(design, snapshot)
    added = apply_addable_table(
        design, delta.addable_tables[0], "banking.retail_banking"
    )

    assert {a.name for a in added.attributes} == {"so_id", "amount"}
    assert all(a.provenance.origin is Origin.LIVE_CATALOG for a in added.attributes)
    assert added.physical_ref is not None


def test_whole_domain_can_be_added(design, snapshot, registry):
    available = [
        AddableDomain(industry="banking", name="wealth_management",
                      display_name="Wealth Management",
                      pack_path="banking/wealth_management.json"),
        AddableDomain(industry="banking", name="retail_banking",
                      display_name="Retail Banking",
                      pack_path="banking/retail_banking.json"),
    ]
    delta = reconcile(design, snapshot, available_domains=available)

    # retail_banking is already in the design, so only wealth_management is offered
    assert [d.key for d in delta.addable_domains] == ["banking.wealth_management"]

    added = apply_addable_domain(design, registry.load_domain("banking.wealth_management"))
    assert added.tables
    assert len(design.domains) == 2


def test_adding_a_domain_twice_is_refused(design, registry):
    with pytest.raises(ValueError, match="already in the design"):
        apply_addable_domain(design, registry.load_domain("banking.retail_banking"))


def test_designed_table_not_in_catalog_is_reported_as_to_create(design, snapshot):
    delta = reconcile(design, snapshot)
    missing = {m.path for m in delta.missing_in_catalog}
    assert any("slv_customer" in p for p in missing)


def test_type_mismatch_is_flagged_with_protection_status(design):
    account = design.domain("banking.retail_banking").table("slv_account")
    target = account.attributes[0]

    other = (
        CanonicalType.INT
        if target.canonical_type is not CanonicalType.INT
        else CanonicalType.STRING
    )
    live = LiveTable(
        dataset="banking_silver", name="slv_account",
        columns=[_col(target.name, other)],
    )
    snap = InMemoryCatalog([live]).snapshot(["banking_silver"])

    delta = reconcile(design, snap)
    mismatch = next(m for m in delta.type_mismatches if m.path.endswith(target.name))
    assert mismatch.actual is other
    assert mismatch.protected is False


def test_clean_design_reports_clean(registry):
    d = DataProductDesign()
    domain = registry.load_domain("banking.wealth_management")
    d.domains.append(domain)

    tables = []
    for t in domain.tables:
        t.physical_ref = PhysicalRef(dataset="wm_silver", table=t.name)
        tables.append(
            LiveTable(
                dataset="wm_silver", name=t.name,
                columns=[_col(a.name, a.canonical_type) for a in t.attributes],
            )
        )

    delta = reconcile(d, InMemoryCatalog(tables).snapshot(["wm_silver"]))
    assert delta.is_clean, delta.summary()


# --- injection safety --------------------------------------------------------

@pytest.mark.parametrize(
    "dataset",
    ["bad-name", "a.b", "x;DROP TABLE y", "`quoted`", "with space", ""],
)
def test_unsafe_dataset_identifier_is_refused(dataset):
    """Dataset names cannot be query-parameterised, so they are validated."""
    catalog = BigQueryCatalog(project="proj", client=object())
    with pytest.raises(InvalidIdentifierError):
        catalog.snapshot([dataset])


def test_unsafe_project_identifier_is_refused():
    with pytest.raises(InvalidIdentifierError):
        BigQueryCatalog(project="proj;DROP")
