"""ODCS export (for the buyer) and Knowledge Catalog publish (for GCP)."""

import pytest
import yaml

from core.contracts.odcs import (
    ODCS_API_VERSION, ODCS_KIND, ContractMeta, domain_to_contract, to_yaml,
    unresolved_lineage, validate,
)
from core.model.provenance import Origin, Provenance
from core.model.tree import (
    Attribute, Domain, EntityType, Layer, PhysicalRef, SourceRef, Table,
)
from core.model.types import CanonicalType
from core.publish.knowledge_catalog import (
    MAX_ASSETS_PER_PRODUCT, KnowledgeCatalogClient, PublishError, PublishTarget,
    plan_publish,
)


def _attr(name, ctype=CanonicalType.STRING, **kw):
    return Attribute(name=name, canonical_type=ctype, **kw)


@pytest.fixture
def domain():
    gold = Table(
        name="gld_account_daily", layer=Layer.GOLD, entity_type=EntityType.AGGREGATE,
        grain="one row per account per day",
        physical_ref=PhysicalRef(dataset="gold", table="gld_account_daily"),
        attributes=[
            _attr("account_id", is_pk=True, nullable=False,
                  description="Account key",
                  standard_refs=["https://bian.org/servicedomain/current-account"],
                  source_ref=SourceRef(layer=Layer.SILVER, table="slv_account",
                                       attribute="account_id")),
            _attr("balance", CanonicalType.DECIMAL, nullable=False,
                  source_ref=SourceRef(layer=Layer.SILVER, table="slv_account",
                                       attribute="balance", transform="sum_by_day")),
            _attr("segment", fk_ref="slv_customer.segment"),
            _attr("orphan_metric", CanonicalType.FLOAT,
                  provenance=Provenance(origin=Origin.LLM)),
        ],
    )
    silver = Table(
        name="slv_account", layer=Layer.SILVER, entity_type=EntityType.DIMENSION,
        physical_ref=PhysicalRef(dataset="silver", table="slv_account"),
        attributes=[_attr("account_id", is_pk=True, nullable=False)],
    )
    bronze = Table(
        name="brz_account_raw", layer=Layer.BRONZE, entity_type=EntityType.RAW,
        physical_ref=PhysicalRef(dataset="bronze", table="brz_account_raw"),
        attributes=[_attr("acct_num")],
    )
    return Domain(
        name="retail_banking", industry="banking", display_name="Retail Banking",
        description="Accounts, customers and transactions.",
        standards=["FIBO", "BIAN", "ISO_20022"],
        pack_ref="banking/retail_banking.json",
        tables=[bronze, silver, gold],
        derived_metrics={"NPA_Rate": {"formula": "..."}},
    )


@pytest.fixture
def meta():
    return ContractMeta(
        id="dp-retail-banking-001", version="1.2.0", status="active",
        purpose="Daily account balances for the buying organisation.",
        owner_team="Data Products", support_url="https://example.invalid/docs",
    )


# --- ODCS export -------------------------------------------------------------

def test_contract_header(domain, meta):
    c = domain_to_contract(domain, meta)
    assert c["apiVersion"] == ODCS_API_VERSION == "v3.2.0"
    assert c["kind"] == ODCS_KIND == "DataContract"
    assert c["id"] == "dp-retail-banking-001"
    assert c["version"] == "1.2.0"
    assert c["domain"] == "retail_banking"


def test_contract_validates(domain, meta):
    assert validate(domain_to_contract(domain, meta)) == []


def test_bronze_is_never_exported(domain, meta):
    c = domain_to_contract(domain, meta)
    assert {o["name"] for o in c["schema"]} == {"slv_account", "gld_account_daily"}


def test_sttm_is_carried_as_odcs_transform_fields(domain, meta):
    c = domain_to_contract(domain, meta)
    gold = next(o for o in c["schema"] if o["name"] == "gld_account_daily")
    balance = next(p for p in gold["properties"] if p["name"] == "balance")

    assert balance["transformSourceObjects"] == ["slv_account"]
    assert balance["transformLogic"] == "sum_by_day"
    assert "slv_account.balance" in balance["transformDescription"]


def test_standards_become_authoritative_definitions(domain, meta):
    c = domain_to_contract(domain, meta)
    gold = next(o for o in c["schema"] if o["name"] == "gld_account_daily")
    acct = next(p for p in gold["properties"] if p["name"] == "account_id")

    assert acct["authoritativeDefinitions"][0]["url"].startswith("https://bian.org")
    assert {d["url"] for d in c["authoritativeDefinitions"]} == {
        "FIBO", "BIAN", "ISO_20022"
    }


def test_logical_and_physical_types_are_both_present(domain, meta):
    c = domain_to_contract(domain, meta)
    gold = next(o for o in c["schema"] if o["name"] == "gld_account_daily")
    balance = next(p for p in gold["properties"] if p["name"] == "balance")

    assert balance["logicalType"] == "number"
    assert balance["physicalType"] == "NUMERIC"   # BigQuery rendering of DECIMAL


def test_keys_and_relationships(domain, meta):
    c = domain_to_contract(domain, meta)
    gold = next(o for o in c["schema"] if o["name"] == "gld_account_daily")

    acct = next(p for p in gold["properties"] if p["name"] == "account_id")
    assert acct["primaryKey"] is True
    assert acct["required"] is True

    segment = next(p for p in gold["properties"] if p["name"] == "segment")
    assert segment["relationships"] == [{"to": "slv_customer", "property": "segment"}]
    assert segment["required"] is False


def test_grain_and_layer_tags(domain, meta):
    c = domain_to_contract(domain, meta)
    gold = next(o for o in c["schema"] if o["name"] == "gld_account_daily")

    assert gold["dataGranularityDescription"] == "one row per account per day"
    assert "layer:gold" in gold["tags"]
    assert "entity:aggregate" in gold["tags"]
    assert gold["physicalName"] == "gold.gld_account_daily"


def test_servers_emitted_when_project_and_dataset_given(domain, meta):
    c = domain_to_contract(domain, meta, project="proj", dataset="gold")
    assert c["servers"][0]["type"] == "bigquery"
    assert c["servers"][0]["project"] == "proj"


def test_yaml_round_trips(domain, meta):
    c = domain_to_contract(domain, meta)
    assert yaml.safe_load(to_yaml(c)) == c


def test_export_refuses_domain_with_no_consumer_layers(meta):
    bronze_only = Domain(
        name="x", industry="y",
        tables=[Table(name="brz_x", layer=Layer.BRONZE, entity_type=EntityType.RAW,
                      attributes=[_attr("a")])],
    )
    with pytest.raises(ValueError, match="nothing to export"):
        domain_to_contract(bronze_only, meta)


def test_validate_catches_broken_contract():
    problems = validate({"kind": "Wrong", "schema": []})
    assert any("apiVersion" in p for p in problems)
    assert any("kind must be" in p for p in problems)
    assert any("no schema" in p for p in problems)


def test_unresolved_lineage_is_surfaced(domain):
    gaps = unresolved_lineage(domain)
    assert any(g.endswith("orphan_metric") for g in gaps)
    assert not any(g.endswith("balance") for g in gaps)


# --- Knowledge Catalog publish ----------------------------------------------

@pytest.fixture
def target():
    return PublishTarget(
        project="my-proj", location="us-central1",
        owner_emails=["owner@example.invalid"],
        approver_emails=["approver@example.invalid"],
        refresh_frequency="Daily",
    )


def test_plan_is_publishable(domain, target):
    plan = plan_publish(domain, target, default_dataset="silver")
    assert plan.is_publishable, plan.problems


def test_data_product_payload_matches_api(domain, target):
    plan = plan_publish(domain, target, default_dataset="silver")
    assert plan.data_product["display_name"] == "Retail Banking"
    assert plan.data_product["owner_emails"] == ["owner@example.invalid"]
    assert plan.data_product["access_approval_config"]["approver_emails"]
    assert plan.data_product_id == "banking-retail-banking"


def test_assets_exclude_bronze_and_use_full_resource_names(domain, target):
    plan = plan_publish(domain, target, default_dataset="silver")

    assert {a.table for a in plan.assets} == {"slv_account", "gld_account_daily"}
    gold = next(a for a in plan.assets if a.table == "gld_account_daily")
    assert gold.resource == (
        "//bigquery.googleapis.com/projects/my-proj/datasets/gold/tables/gld_account_daily"
    )
    assert gold.payload() == {"resource": gold.resource}


def test_asset_limit_is_enforced(target):
    big = Domain(
        name="wide", industry="test",
        tables=[
            Table(name=f"slv_t{i}", layer=Layer.SILVER,
                  entity_type=EntityType.DIMENSION,
                  physical_ref=PhysicalRef(dataset="silver", table=f"slv_t{i}"),
                  attributes=[_attr("a")])
            for i in range(MAX_ASSETS_PER_PRODUCT + 1)
        ],
    )
    plan = plan_publish(big, target, default_dataset="silver")
    assert not plan.is_publishable
    assert any("exceeds the 50-asset limit" in p for p in plan.problems)


def test_unmaterialised_tables_block_publish(domain, target):
    domain.table("gld_account_daily").physical_ref = None
    plan = plan_publish(domain, target, default_dataset="silver")

    assert not plan.is_publishable
    assert any("must already exist" in p for p in plan.problems)


def test_missing_owner_emails_blocks_publish(domain):
    bare = PublishTarget(project="p", location="us-central1", owner_emails=[])
    plan = plan_publish(domain, bare, default_dataset="silver")
    assert any("owner_emails is required" in p for p in plan.problems)


def test_aspects_include_system_and_custom_types(domain, target):
    plan = plan_publish(domain, target, default_dataset="silver")

    assert "dataplex-types.global.overview" in plan.aspects
    assert "dataplex-types.global.refresh-cadence" in plan.aspects
    assert "my-proj.us-central1.data-product-domain" in plan.aspects
    assert "my-proj.us-central1.attribute-lineage" in plan.aspects

    overview = plan.aspects["dataplex-types.global.overview"]
    assert overview["aspectType"].startswith("projects/dataplex-types/locations/global")
    assert overview["data"]["content"]


def test_custom_aspect_carries_pack_and_standards(domain, target):
    plan = plan_publish(domain, target, default_dataset="silver")
    data = plan.aspects["my-proj.us-central1.data-product-domain"]["data"]

    assert data["sourcePack"] == "banking/retail_banking.json"
    assert data["standards"] == ["FIBO", "BIAN", "ISO_20022"]
    assert data["derivedMetrics"] == ["NPA_Rate"]


def test_attribute_lineage_aspect_excludes_bronze_tables(domain, target):
    plan = plan_publish(domain, target, default_dataset="silver")
    edges = plan.aspects["my-proj.us-central1.attribute-lineage"]["data"]["edges"]

    assert any(e["target"] == "gld_account_daily.balance" for e in edges)
    assert all(not e["target"].startswith("brz_") for e in edges)


def test_urls(domain, target):
    plan = plan_publish(domain, target, default_dataset="silver")
    assert plan.create_url(target).endswith(
        "/locations/us-central1/dataProducts?data_product_id=banking-retail-banking"
    )
    assert "/dataAssets?data_asset_id=" in plan.asset_url(target, plan.assets[0])


def test_dry_run_returns_the_plan(domain, target):
    plan = plan_publish(domain, target, default_dataset="silver")
    out = KnowledgeCatalogClient(target).apply(plan, dry_run=True)

    assert out["dry_run"] is True
    assert len(out["assets"]) == 2


def test_live_publish_is_not_implemented_yet(domain, target):
    plan = plan_publish(domain, target, default_dataset="silver")
    with pytest.raises(NotImplementedError):
        KnowledgeCatalogClient(target).apply(plan, dry_run=False)


def test_unpublishable_plan_is_refused(domain, target):
    domain.table("gld_account_daily").physical_ref = None
    plan = plan_publish(domain, target, default_dataset="silver")
    with pytest.raises(PublishError, match="unresolved problems"):
        KnowledgeCatalogClient(target).apply(plan, dry_run=True)


# --- real packs --------------------------------------------------------------

def test_every_pack_exports_after_materialisation(registry, meta):
    """A pack is silver-only until gold is designed, so silver alone must export."""
    for entry in registry.entries():
        domain = registry.load_domain(entry.key)
        for t in domain.tables:
            t.physical_ref = PhysicalRef(dataset="silver", table=t.name)

        contract = domain_to_contract(domain, meta)
        assert validate(contract) == [], f"{entry.key}: {validate(contract)}"
        assert len(contract["schema"]) == len(domain.tables)
