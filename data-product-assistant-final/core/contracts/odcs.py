"""Export the canonical tree as an Open Data Contract Standard v3.2.0 contract.

ODCS is the export format because the buying organisation ingests it directly
and may not be on GCP. It is vendor-neutral YAML under Bitol (Linux Foundation
AI & Data, graduated July 2026), so it travels; Knowledge Catalog's own
`contract` aspect is a GCP-side governance object and cannot serve this role.

The fit is close to lossless. ODCS carries at column level:
  * transformSourceObjects / transformLogic / transformDescription -- the STTM
  * authoritativeDefinitions -- BIAN / FIBO / ISO 20022 references
  * logicalType + physicalType -- mirrors canonical type vs rendered type
  * primaryKey / primaryKeyPosition, relationships, classification

Field names follow the published JSON Schema
(bitol-io/open-data-contract-standard, schema/odcs-json-schema-latest.json).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.model.provenance import Origin
from core.model.tree import Attribute, Domain, Layer, Table
from core.model.types import CanonicalType, render

ODCS_API_VERSION = "v3.2.0"
ODCS_KIND = "DataContract"

#: CanonicalType -> ODCS logicalType. ODCS keeps the engine type separately in
#: physicalType, which is why core stores neither as its primary representation.
_LOGICAL_TYPE: dict[CanonicalType, str] = {
    CanonicalType.STRING: "string",
    CanonicalType.INT: "integer",
    CanonicalType.DECIMAL: "number",
    CanonicalType.FLOAT: "number",
    CanonicalType.BOOL: "boolean",
    CanonicalType.DATE: "date",
    CanonicalType.TIME: "date",
    CanonicalType.DATETIME: "date",
    CanonicalType.TIMESTAMP: "date",
    CanonicalType.BYTES: "string",
    CanonicalType.JSON: "object",
    CanonicalType.STRUCT: "object",
    CanonicalType.ARRAY: "array",
    CanonicalType.GEOGRAPHY: "string",
}

#: Layers a buyer receives. Bronze is internal plumbing and is never exported --
#: the predecessor publisher already treated bronze as "source data, not output".
EXPORTED_LAYERS = (Layer.SILVER, Layer.GOLD)


@dataclass
class ContractMeta:
    id: str
    version: str = "1.0.0"
    status: str = "draft"
    tenant: str | None = None
    owner_team: str | None = None
    support_url: str | None = None
    purpose: str = ""
    usage: str = ""
    limitations: str = ""
    sla: list[dict] = field(default_factory=list)
    price: dict | None = None


def _custom(props: dict) -> list[dict]:
    """ODCS customProperties is an array of {property, value}."""
    return [{"property": k, "value": v} for k, v in props.items() if v not in (None, "", [])]


def _authoritative(refs: list[str]) -> list[dict]:
    return [{"type": "businessDefinition", "url": r} for r in refs]


def attribute_to_property(attr: Attribute, *, target: str = "bigquery") -> dict:
    """Map one Attribute to an ODCS schema property."""
    prop: dict = {
        "name": attr.name,
        "logicalType": _LOGICAL_TYPE[attr.canonical_type],
        "physicalType": render(attr.canonical_type, target),
        "required": not attr.nullable,
    }

    if attr.description:
        prop["description"] = attr.description
    if attr.is_pk:
        prop["primaryKey"] = True
    if attr.standard_refs:
        prop["authoritativeDefinitions"] = _authoritative(attr.standard_refs)

    if attr.fk_ref:
        table, _, column = attr.fk_ref.partition(".")
        prop["relationships"] = [{"to": table, "property": column}]

    # The STTM, expressed in ODCS's own vocabulary rather than a side artifact.
    if attr.source_ref is not None:
        prop["transformSourceObjects"] = [attr.source_ref.table]
        prop["transformDescription"] = (
            f"from {attr.source_ref.layer.value.lower()}."
            f"{attr.source_ref.table}.{attr.source_ref.attribute}"
        )
        if attr.source_ref.transform:
            prop["transformLogic"] = attr.source_ref.transform

    extras = _custom(
        {
            "provenance": attr.provenance.origin.value,
            "humanLocked": attr.provenance.locked or None,
            "sourceBlock": attr.block,
        }
    )
    if extras:
        prop["customProperties"] = extras

    return prop


def table_to_schema_object(table: Table, *, target: str = "bigquery") -> dict:
    obj: dict = {
        "name": table.name,
        "logicalType": "object",
        "physicalType": "table",
        "properties": [attribute_to_property(a, target=target) for a in table.attributes],
    }

    if table.physical_ref is not None:
        obj["physicalName"] = str(table.physical_ref)
    if table.description:
        obj["description"] = table.description
    if table.grain:
        obj["dataGranularityDescription"] = table.grain

    obj["tags"] = [
        f"layer:{table.layer.value.lower()}",
        f"entity:{table.entity_type.value.lower()}",
    ]
    return obj


def domain_to_contract(
    domain: Domain,
    meta: ContractMeta,
    *,
    project: str | None = None,
    dataset: str | None = None,
    target: str = "bigquery",
    layers: tuple[Layer, ...] = EXPORTED_LAYERS,
) -> dict:
    """Build an ODCS v3.2.0 contract for one domain.

    Only consumer-facing layers are exported; see EXPORTED_LAYERS.
    """
    tables = [t for t in domain.tables if t.layer in layers]
    if not tables:
        raise ValueError(
            f"domain {domain.key} has no tables in layers "
            f"{[l.value for l in layers]}; nothing to export"
        )

    contract: dict = {
        "apiVersion": ODCS_API_VERSION,
        "kind": ODCS_KIND,
        "id": meta.id,
        "version": meta.version,
        "status": meta.status,
        "name": domain.display_name or domain.name,
        "domain": domain.name,
        "contractCreatedTs": datetime.now(timezone.utc).isoformat(),
        "description": {
            "purpose": meta.purpose or domain.description,
            "usage": meta.usage,
            "limitations": meta.limitations,
        },
        "schema": [table_to_schema_object(t, target=target) for t in tables],
        "tags": [f"industry:{domain.industry}", f"domain:{domain.name}"],
    }

    if meta.tenant:
        contract["tenant"] = meta.tenant

    if domain.standards:
        contract["authoritativeDefinitions"] = _authoritative(domain.standards)

    if project and dataset:
        # ODCS servers are type-discriminated. Validate these two keys against
        # the JSON Schema for the pinned apiVersion before relying on them.
        contract["servers"] = [
            {
                "server": "production",
                "type": "bigquery",
                "environment": "prod",
                "project": project,
                "dataset": dataset,
            }
        ]

    if meta.owner_team:
        contract["team"] = {"name": meta.owner_team}
    if meta.support_url:
        contract["support"] = [{"channel": "docs", "url": meta.support_url}]
    if meta.sla:
        contract["slaProperties"] = meta.sla
    if meta.price:
        contract["price"] = meta.price

    provenance = _custom(
        {
            "sourcePack": domain.pack_ref,
            "derivedMetrics": sorted(domain.derived_metrics) or None,
        }
    )
    if provenance:
        contract["customProperties"] = provenance

    return contract


def to_yaml(contract: dict) -> str:
    import yaml

    return yaml.safe_dump(contract, sort_keys=False, allow_unicode=True)


def validate(contract: dict) -> list[str]:
    """Structural checks. Not a substitute for JSON Schema validation."""
    problems: list[str] = []

    for required in ("apiVersion", "kind", "id", "version"):
        if not contract.get(required):
            problems.append(f"missing required top-level field {required!r}")

    if contract.get("kind") != ODCS_KIND:
        problems.append(f"kind must be {ODCS_KIND!r}")

    for obj in contract.get("schema") or []:
        if not obj.get("name"):
            problems.append("schema object is missing required 'name'")
        if not obj.get("properties"):
            problems.append(f"schema object {obj.get('name')!r} has no properties")
        for prop in obj.get("properties") or []:
            if not prop.get("name"):
                problems.append(f"property in {obj.get('name')!r} is missing 'name'")

    if not contract.get("schema"):
        problems.append("contract has no schema")

    return problems


def unresolved_lineage(domain: Domain, *, layers: tuple[Layer, ...] = EXPORTED_LAYERS) -> list[str]:
    """Exported columns with no source_ref, i.e. no STTM entry to carry.

    Surfaced rather than silently omitted: a contract that claims a column but
    cannot say where it comes from is the gap a buyer will ask about first.
    """
    out: list[str] = []
    for table in domain.tables:
        if table.layer not in layers:
            continue
        for attr in table.attributes:
            if attr.source_ref is None and attr.provenance.origin is not Origin.PACK_BLOCK:
                out.append(f"{domain.key}/{table.name}/{attr.name}")
    return out
