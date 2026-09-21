"""Reconcile the designed tree against the live catalog.

This is the edit engine. It answers three questions the predecessors could not:

  * this column exists in BigQuery but is not in the design -- add it?
  * this table exists but is not in the design -- add it?
  * this domain is implied but not instantiated -- add the whole thing?

and one they could not answer either: what does the design declare that does
not exist yet, and where do the two disagree on type?
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.model.provenance import Origin, Provenance
from core.model.tree import (
    Attribute,
    DataProductDesign,
    Domain,
    EntityType,
    Layer,
    SourceRef,
    Table,
)
from core.model.types import CanonicalType

from .live import LiveColumn, LiveSnapshot, LiveTable


@dataclass
class AddableAttribute:
    """Exists in the catalog, absent from the designed table."""

    domain_key: str
    table_name: str
    column: LiveColumn
    physical: str

    @property
    def path(self) -> str:
        return f"{self.domain_key}/{self.table_name}/{self.column.name}"


@dataclass
class AddableTable:
    """Exists in the catalog, absent from the design."""

    live: LiveTable
    suggested_domain_key: str | None = None

    @property
    def physical(self) -> str:
        return f"{self.live.dataset}.{self.live.name}"


@dataclass
class AddableDomain:
    """Available in a pack, not instantiated in the design."""

    industry: str
    name: str
    display_name: str
    pack_path: str
    matched_signals: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.industry}.{self.name}"


@dataclass
class TypeMismatch:
    path: str
    designed: CanonicalType
    actual: CanonicalType
    protected: bool


@dataclass
class MissingInCatalog:
    path: str
    layer: Layer
    physical: str | None = None


@dataclass
class Delta:
    addable_attributes: list[AddableAttribute] = field(default_factory=list)
    addable_tables: list[AddableTable] = field(default_factory=list)
    addable_domains: list[AddableDomain] = field(default_factory=list)
    type_mismatches: list[TypeMismatch] = field(default_factory=list)
    missing_in_catalog: list[MissingInCatalog] = field(default_factory=list)
    unresolved_types: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not (
            self.addable_attributes
            or self.addable_tables
            or self.addable_domains
            or self.type_mismatches
            or self.missing_in_catalog
        )

    def summary(self) -> str:
        return (
            f"{len(self.addable_attributes)} addable columns, "
            f"{len(self.addable_tables)} addable tables, "
            f"{len(self.addable_domains)} addable domains, "
            f"{len(self.type_mismatches)} type mismatches, "
            f"{len(self.missing_in_catalog)} not yet created"
        )


def _match_live(table: Table, snapshot: LiveSnapshot) -> LiveTable | None:
    """Physical ref wins; fall back to bare name match."""
    if table.physical_ref is not None:
        found = snapshot.table(
            table.physical_ref.table, dataset=table.physical_ref.dataset
        )
        if found is not None:
            return found
    return snapshot.table(table.name)


def reconcile(
    design: DataProductDesign,
    snapshot: LiveSnapshot,
    *,
    available_domains: list[AddableDomain] | None = None,
) -> Delta:
    delta = Delta(unresolved_types=list(snapshot.unresolved_types))

    designed_physical: set[str] = set()

    for domain in design.domains:
        for table in domain.tables:
            live = _match_live(table, snapshot)

            if live is None:
                delta.missing_in_catalog.append(
                    MissingInCatalog(
                        path=f"{domain.key}/{table.name}",
                        layer=table.layer,
                        physical=str(table.physical_ref) if table.physical_ref else None,
                    )
                )
                continue

            designed_physical.add(f"{live.dataset}.{live.name}")

            for col in live.columns:
                if table.has(col.name):
                    designed = table.attribute(col.name)
                    if designed.canonical_type is not col.canonical_type:
                        delta.type_mismatches.append(
                            TypeMismatch(
                                path=f"{domain.key}/{table.name}/{col.name}",
                                designed=designed.canonical_type,
                                actual=col.canonical_type,
                                protected=designed.provenance.is_protected,
                            )
                        )
                else:
                    delta.addable_attributes.append(
                        AddableAttribute(
                            domain_key=domain.key,
                            table_name=table.name,
                            column=col,
                            physical=f"{live.dataset}.{live.name}",
                        )
                    )

            for attr in table.attributes:
                if live.column(attr.name) is None:
                    delta.missing_in_catalog.append(
                        MissingInCatalog(
                            path=f"{domain.key}/{table.name}/{attr.name}",
                            layer=table.layer,
                            physical=f"{live.dataset}.{live.name}",
                        )
                    )

    for live in snapshot.tables:
        if f"{live.dataset}.{live.name}" not in designed_physical:
            delta.addable_tables.append(AddableTable(live=live))

    if available_domains:
        existing = {d.key for d in design.domains}
        delta.addable_domains = [
            a for a in available_domains if a.key not in existing
        ]

    return delta


def _layer_of(dataset: str) -> Layer:
    lowered = dataset.lower()
    for layer in (Layer.BRONZE, Layer.SILVER, Layer.GOLD):
        if layer.value.lower() in lowered:
            return layer
    return Layer.BRONZE


def apply_addable_attribute(
    design: DataProductDesign, addable: AddableAttribute, *, who: str | None = None
) -> Attribute:
    """Pull a real catalog column into the designed table.

    Lands as LIVE_CATALOG and locked: it is ground truth, so a later
    regeneration must not overwrite it.
    """
    domain = design.domain(addable.domain_key)
    table = domain.table(addable.table_name)
    col = addable.column

    dataset, _, table_name = addable.physical.partition(".")

    attribute = Attribute(
        name=col.name,
        canonical_type=col.canonical_type,
        nullable=col.nullable,
        description=col.description,
        provenance=Provenance(
            origin=Origin.LIVE_CATALOG,
            locked=True,
            detail=addable.physical,
            updated_by=who,
        ),
        source_ref=SourceRef(
            layer=_layer_of(dataset), table=table_name, attribute=col.name
        ),
    )
    table.attributes.append(attribute)
    return attribute


def apply_addable_table(
    design: DataProductDesign,
    addable: AddableTable,
    domain_key: str,
    *,
    entity_type: EntityType = EntityType.RAW,
    who: str | None = None,
) -> Table:
    """Pull a real catalog table, with all its columns, into a domain."""
    domain = design.domain(domain_key)
    live = addable.live
    layer = _layer_of(live.dataset)

    table = Table(
        name=live.name,
        layer=layer,
        entity_type=entity_type,
        attributes=[
            Attribute(
                name=c.name,
                canonical_type=c.canonical_type,
                nullable=c.nullable,
                description=c.description,
                provenance=Provenance(
                    origin=Origin.LIVE_CATALOG,
                    locked=True,
                    detail=addable.physical,
                    updated_by=who,
                ),
                source_ref=SourceRef(layer=layer, table=live.name, attribute=c.name),
            )
            for c in live.columns
        ],
        provenance=Provenance(
            origin=Origin.LIVE_CATALOG,
            locked=True,
            detail=addable.physical,
            updated_by=who,
        ),
    )
    table.physical_ref = _physical(live)
    domain.tables.append(table)
    return table


def apply_addable_domain(
    design: DataProductDesign, domain: Domain
) -> Domain:
    """Instantiate a whole domain from a pack framework."""
    if any(d.key == domain.key for d in design.domains):
        raise ValueError(f"domain {domain.key} is already in the design")
    design.domains.append(domain)
    return domain


def _physical(live: LiveTable):
    from core.model.tree import PhysicalRef

    return PhysicalRef(dataset=live.dataset, table=live.name)
