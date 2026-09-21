"""Publish to Google Cloud Knowledge Catalog as a native data product.

Knowledge Catalog is Dataplex Universal Catalog renamed (2026-04-10); data
products are GA. This replaces the predecessor's bespoke `utility_catalog-<id>.json`
as the *published* representation, so the catalog entry is the GCP-side source of
truth rather than a file we invented.

It does not replace the canonical tree. Knowledge Catalog models a data product
as a grouping of assets that already exist, with governance metadata attached.
It has no notion of a pre-materialisation design, a bronze->silver->gold mapping,
or per-field edit provenance, so publishing is strictly a post-materialisation
step and the design-time model stays ours.

API surface (v1):
  POST .../projects/{p}/locations/{l}/dataProducts?data_product_id={id}
  POST .../dataProducts/{id}/dataAssets?data_asset_id={id}
  PATCH entries  (to attach aspects)
Terraform: google_dataplex_data_product, google_dataplex_data_product_data_asset
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.model.tree import Domain, Layer, Table

API_ROOT = "https://dataplex.googleapis.com/v1"

#: Documented hard limit. A multi-domain design can exceed this, so the publish
#: plan enforces it rather than discovering it at call time.
MAX_ASSETS_PER_PRODUCT = 50

#: Layers a consumer sees. Bronze stays internal.
PUBLISHED_LAYERS = (Layer.SILVER, Layer.GOLD)

SYSTEM_ASPECT_OVERVIEW = "dataplex-types.global.overview"
SYSTEM_ASPECT_REFRESH = "dataplex-types.global.refresh-cadence"

_ID = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")


class PublishError(ValueError):
    pass


def _as_id(raw: str) -> str:
    """Knowledge Catalog ids are lowercase, hyphenated, <=63 chars."""
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:63].rstrip("-")
    if not _ID.match(slug):
        raise PublishError(f"cannot derive a valid id from {raw!r}")
    return slug


@dataclass
class PublishTarget:
    project: str
    location: str
    owner_emails: list[str]
    approver_emails: list[str] = field(default_factory=list)
    refresh_frequency: str | None = None


@dataclass
class AssetPlan:
    asset_id: str
    resource: str
    table: str
    layer: Layer

    def payload(self) -> dict:
        return {"resource": self.resource}


@dataclass
class PublishPlan:
    data_product_id: str
    data_product: dict
    assets: list[AssetPlan]
    aspects: dict
    problems: list[str] = field(default_factory=list)

    @property
    def is_publishable(self) -> bool:
        return not self.problems

    def create_url(self, target: PublishTarget) -> str:
        return (
            f"{API_ROOT}/projects/{target.project}/locations/{target.location}"
            f"/dataProducts?data_product_id={self.data_product_id}"
        )

    def asset_url(self, target: PublishTarget, asset: AssetPlan) -> str:
        return (
            f"{API_ROOT}/projects/{target.project}/locations/{target.location}"
            f"/dataProducts/{self.data_product_id}/dataAssets"
            f"?data_asset_id={asset.asset_id}"
        )


def bigquery_resource(project: str, dataset: str, table: str) -> str:
    return (
        f"//bigquery.googleapis.com/projects/{project}"
        f"/datasets/{dataset}/tables/{table}"
    )


def custom_aspect_type(target: PublishTarget, name: str) -> str:
    return f"{target.project}.{target.location}.{name}"


def _aspect(aspect_type_path: str, data: dict) -> dict:
    project, location, name = aspect_type_path.split(".", 2)
    return {
        "aspectType": f"projects/{project}/locations/{location}/aspectTypes/{name}",
        "data": data,
    }


def _system_aspect(short: str, data: dict) -> dict:
    return _aspect(short, data)


def build_aspects(domain: Domain, target: PublishTarget) -> dict:
    """System aspects plus custom aspects for what Knowledge Catalog has no slot for.

    Domain/pack provenance, medallion layer and attribute-level lineage have no
    system aspect type, so they go into custom aspect types under this project.
    """
    published = [t for t in domain.tables if t.layer in PUBLISHED_LAYERS]

    aspects: dict = {
        SYSTEM_ASPECT_OVERVIEW: _system_aspect(
            SYSTEM_ASPECT_OVERVIEW,
            {"content": domain.description or domain.display_name or domain.name},
        ),
        custom_aspect_type(target, "data-product-domain"): _aspect(
            custom_aspect_type(target, "data-product-domain"),
            {
                "industry": domain.industry,
                "domain": domain.name,
                "sourcePack": domain.pack_ref or "",
                "standards": list(domain.standards),
                "derivedMetrics": sorted(domain.derived_metrics),
            },
        ),
        custom_aspect_type(target, "medallion-layers"): _aspect(
            custom_aspect_type(target, "medallion-layers"),
            {
                "layers": sorted({t.layer.value for t in published}),
                "tableCount": len(published),
            },
        ),
        custom_aspect_type(target, "attribute-lineage"): _aspect(
            custom_aspect_type(target, "attribute-lineage"),
            {
                "edges": [
                    {
                        "target": f"{t.name}.{a.name}",
                        "source": str(a.source_ref),
                        "transform": a.source_ref.transform or "",
                    }
                    for t in published
                    for a in t.attributes
                    if a.source_ref is not None
                ]
            },
        ),
    }

    if target.refresh_frequency:
        aspects[SYSTEM_ASPECT_REFRESH] = _system_aspect(
            SYSTEM_ASPECT_REFRESH, {"frequency": target.refresh_frequency}
        )

    return aspects


def _dataset_for(table: Table, fallback: str) -> str:
    if table.physical_ref is not None and table.physical_ref.dataset:
        return table.physical_ref.dataset
    return fallback


def plan_publish(
    domain: Domain,
    target: PublishTarget,
    *,
    default_dataset: str,
    layers: tuple[Layer, ...] = PUBLISHED_LAYERS,
) -> PublishPlan:
    """Build a publish plan and report anything that would make it fail.

    Problems are returned rather than raised so the caller can show them all at
    once; `is_publishable` gates the actual calls.
    """
    problems: list[str] = []

    if not target.owner_emails:
        problems.append("owner_emails is required by dataProducts.create")

    published = [t for t in domain.tables if t.layer in layers]
    if not published:
        problems.append(
            f"domain {domain.key} has no tables in "
            f"{[l.value for l in layers]}; nothing to publish"
        )

    assets: list[AssetPlan] = []
    for table in published:
        dataset = _dataset_for(table, default_dataset)
        physical = table.physical_ref.table if table.physical_ref else table.name
        assets.append(
            AssetPlan(
                asset_id=_as_id(f"{table.layer.value}-{physical}"),
                resource=bigquery_resource(target.project, dataset, physical),
                table=table.name,
                layer=table.layer,
            )
        )

    if len(assets) > MAX_ASSETS_PER_PRODUCT:
        problems.append(
            f"{len(assets)} assets exceeds the {MAX_ASSETS_PER_PRODUCT}-asset limit "
            f"per data product; split {domain.key} into several products"
        )

    unmaterialised = [t.name for t in published if t.physical_ref is None]
    if unmaterialised:
        problems.append(
            "assets must already exist before attachment; these tables have no "
            f"physical_ref: {unmaterialised}"
        )

    data_product = {
        "display_name": domain.display_name or domain.name,
        "description": domain.description,
        "owner_emails": list(target.owner_emails),
    }
    if target.approver_emails:
        data_product["access_approval_config"] = {
            "approver_emails": list(target.approver_emails)
        }

    return PublishPlan(
        data_product_id=_as_id(f"{domain.industry}-{domain.name}"),
        data_product=data_product,
        assets=assets,
        aspects=build_aspects(domain, target),
        problems=problems,
    )


class KnowledgeCatalogClient:
    """Thin client. Left unimplemented until Phase 8 -- see docs/06-roadmap.md.

    Publishing creates externally-visible governance objects and grants IAM via
    access groups, so it must be an explicit, reviewed action rather than
    something an agent triggers implicitly.
    """

    def __init__(self, target: PublishTarget) -> None:
        self.target = target

    def apply(self, plan: PublishPlan, *, dry_run: bool = True) -> dict:
        if not plan.is_publishable:
            raise PublishError(
                "plan has unresolved problems: " + "; ".join(plan.problems)
            )
        if dry_run:
            return {
                "dry_run": True,
                "data_product_id": plan.data_product_id,
                "create": plan.create_url(self.target),
                "assets": [plan.asset_url(self.target, a) for a in plan.assets],
                "aspect_types": sorted(plan.aspects),
            }
        raise NotImplementedError(
            "live Knowledge Catalog publish is Phase 8; use dry_run=True"
        )
