"""The canonical model: Domain -> Table -> Attribute.

Every agent reads and writes this tree. It is also the edit surface and the API
shape. It formalises the `schema_version: 2.0` domain framework files inherited
from the GOLD product, adding layer, provenance and lineage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator

from .provenance import MergeReport, Origin, Provenance, Suggestion
from .types import CanonicalType, normalize


class Layer(str, Enum):
    BRONZE = "BRONZE"
    SILVER = "SILVER"
    GOLD = "GOLD"


class EntityType(str, Enum):
    DIMENSION = "DIMENSION"
    EVENT = "EVENT"
    AGGREGATE = "AGGREGATE"
    RAW = "RAW"          # bronze landing tables


class PathError(KeyError):
    pass


@dataclass
class SourceRef:
    """Where an attribute comes from upstream. Drives attribute-level lineage."""

    layer: Layer
    table: str
    attribute: str
    transform: str | None = None     # canonical step name, not SQL

    def __str__(self) -> str:
        return f"{self.layer.value.lower()}:{self.table}.{self.attribute}"


@dataclass
class PhysicalRef:
    """Where a table actually lives once materialised."""

    dataset: str
    table: str
    project: str | None = None

    def __str__(self) -> str:
        parts = [p for p in (self.project, self.dataset, self.table) if p]
        return ".".join(parts)


@dataclass
class Attribute:
    name: str
    canonical_type: CanonicalType
    nullable: bool = True
    is_pk: bool = False
    fk_ref: str | None = None            # "table.attribute"
    description: str = ""
    provenance: Provenance = field(
        default_factory=lambda: Provenance(origin=Origin.LLM)
    )
    block: str | None = None             # shared block that contributed this
    standard_refs: list[str] = field(default_factory=list)
    source_ref: SourceRef | None = None

    @classmethod
    def from_framework(cls, raw: dict, *, detail: str | None = None) -> Attribute:
        """Build from a `schema_version: 2.0` framework column entry."""
        return cls(
            name=raw["name"],
            canonical_type=normalize(raw.get("data_type") or raw.get("bq_type") or ""),
            nullable=bool(raw.get("nullable", True)),
            is_pk=bool(raw.get("is_pk", False)),
            fk_ref=raw.get("fk_ref"),
            description=raw.get("description", ""),
            provenance=Provenance(origin=Origin.PACK_ENTITY, detail=detail),
            standard_refs=list(raw.get("standard_refs", [])),
        )


@dataclass
class Table:
    name: str
    layer: Layer
    entity_type: EntityType
    grain: str = ""
    attributes: list[Attribute] = field(default_factory=list)
    physical_ref: PhysicalRef | None = None
    provenance: Provenance = field(
        default_factory=lambda: Provenance(origin=Origin.LLM)
    )
    description: str = ""

    def attribute(self, name: str) -> Attribute:
        for a in self.attributes:
            if a.name == name:
                return a
        raise PathError(f"no attribute {name!r} on table {self.name!r}")

    def has(self, name: str) -> bool:
        return any(a.name == name for a in self.attributes)

    @property
    def primary_key(self) -> list[Attribute]:
        return [a for a in self.attributes if a.is_pk]

    def merge(self, incoming: Table) -> MergeReport:
        """Fold regenerated output into this table without destroying user work.

        Protected attributes (human-edited, or read from the live catalog) are
        kept, and the incoming version is recorded as a withheld suggestion.
        """
        report = MergeReport()
        incoming_by_name = {a.name: a for a in incoming.attributes}
        merged: list[Attribute] = []

        for existing in self.attributes:
            path = f"{self.name}/{existing.name}"
            candidate = incoming_by_name.pop(existing.name, None)

            if existing.provenance.is_protected:
                merged.append(existing)
                report.preserved.append(path)
                if candidate is not None and _differs(existing, candidate):
                    report.withheld.append(
                        Suggestion(
                            path=path,
                            current=_shape(existing),
                            proposed=_shape(candidate),
                        )
                    )
                continue

            if candidate is None:
                report.removed.append(path)
                continue

            if _differs(existing, candidate):
                merged.append(candidate)
                report.replaced.append(path)
            else:
                merged.append(existing)

        for name, candidate in incoming_by_name.items():
            merged.append(candidate)
            report.added.append(f"{self.name}/{name}")

        self.attributes = merged
        return report


@dataclass
class Domain:
    name: str
    industry: str
    display_name: str = ""
    description: str = ""
    standards: list[str] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    pack_ref: str | None = None
    provenance: Provenance = field(
        default_factory=lambda: Provenance(origin=Origin.LLM)
    )

    # Optional pack enrichments. Present in the legacy CPG frameworks and absent
    # from the 2.0 ones, so they are carried through rather than dropped --
    # source_mappings and value_normalisation are exactly what the STTM and
    # transformation agents need to avoid re-deriving mappings from prose.
    keywords: list[str] = field(default_factory=list)
    derived_metrics: dict = field(default_factory=dict)
    source_mappings: dict = field(default_factory=dict)
    value_normalisation: dict = field(default_factory=dict)

    def table(self, name: str) -> Table:
        for t in self.tables:
            if t.name == name:
                return t
        raise PathError(f"no table {name!r} in domain {self.name!r}")

    def has(self, name: str) -> bool:
        return any(t.name == name for t in self.tables)

    def by_layer(self, layer: Layer) -> list[Table]:
        return [t for t in self.tables if t.layer is layer]

    @property
    def key(self) -> str:
        return f"{self.industry}.{self.name}"


@dataclass
class DataProductDesign:
    """Root of the designed tree for one session."""

    domains: list[Domain] = field(default_factory=list)

    def domain(self, key: str) -> Domain:
        """Look up by `industry.name` or by bare `name` when unambiguous."""
        exact = [d for d in self.domains if d.key == key]
        if exact:
            return exact[0]
        bare = [d for d in self.domains if d.name == key]
        if len(bare) == 1:
            return bare[0]
        if len(bare) > 1:
            raise PathError(
                f"domain {key!r} is ambiguous; qualify it as industry.name "
                f"(candidates: {[d.key for d in bare]})"
            )
        raise PathError(f"no domain {key!r}")

    def resolve(self, path: str) -> Domain | Table | Attribute:
        """Resolve `domain[/table[/attribute]]`."""
        parts = [p for p in path.split("/") if p]
        if not parts or len(parts) > 3:
            raise PathError(f"bad path {path!r}; expected domain/table/attribute")
        node: Domain | Table | Attribute = self.domain(parts[0])
        if len(parts) > 1:
            node = node.table(parts[1])          # type: ignore[union-attr]
        if len(parts) > 2:
            node = node.attribute(parts[2])      # type: ignore[union-attr]
        return node

    def walk(self) -> Iterator[tuple[Domain, Table, Attribute]]:
        for d in self.domains:
            for t in d.tables:
                for a in t.attributes:
                    yield d, t, a

    def tables(self, layer: Layer | None = None) -> list[tuple[Domain, Table]]:
        return [
            (d, t)
            for d in self.domains
            for t in d.tables
            if layer is None or t.layer is layer
        ]

    @property
    def is_empty(self) -> bool:
        return not self.domains


def _normalise_metrics(raw: object) -> dict:
    """Both pack formats declare derived metrics, in different shapes.

    Legacy CPG: {"CTR": {formula, description}}
    2.0 packs:  [{"name": "NPA_Rate", formula, grain, description}]
    """
    if isinstance(raw, dict):
        return dict(raw)

    if isinstance(raw, list):
        out: dict = {}
        for item in raw:
            if isinstance(item, dict) and "name" in item:
                out[item["name"]] = {k: v for k, v in item.items() if k != "name"}
            elif isinstance(item, str):
                out[item] = {}
        return out

    return {}


def _shape(a: Attribute) -> dict:
    return {
        "name": a.name,
        "type": a.canonical_type.value,
        "nullable": a.nullable,
        "is_pk": a.is_pk,
        "fk_ref": a.fk_ref,
        "description": a.description,
    }


def _differs(left: Attribute, right: Attribute) -> bool:
    return _shape(left) != _shape(right)


def domain_from_framework(
    raw: dict, *, pack_ref: str | None = None, industry: str | None = None
) -> Domain:
    """Build a Domain from a domain framework file.

    Accepts both inherited formats: the `schema_version: 2.0` subdirectory packs
    (banking, healthcare, retail, cross_industry) and the older unversioned CPG
    packs, which carry no `schema_version` or `industry` but add richer
    `derived_metrics`, `source_platform_mappings` and `*_normalisation` blocks.

    Framework entities are silver-layer by definition in both formats.
    """
    version = str(raw.get("schema_version", "")).strip()
    if version and not version.startswith("2."):
        raise ValueError(
            f"unsupported framework schema_version {version!r}; expected 2.x or unversioned"
        )

    resolved_industry = raw.get("industry") or industry
    if not resolved_industry:
        raise ValueError(
            f"framework {raw.get('domain')!r} declares no industry and none was "
            "supplied; the registry entry should provide it"
        )

    entity_types: dict[str, str] = {}
    for group, names in (raw.get("entity_types") or {}).items():
        singular = {
            "dimensions": EntityType.DIMENSION,
            "events": EntityType.EVENT,
            "aggregates": EntityType.AGGREGATE,
        }.get(group)
        if singular is None:
            continue
        for n in names:
            entity_types[n] = singular

    ordered = raw.get("hierarchy") or list((raw.get("entities") or {}).keys())
    entities: dict = raw.get("entities") or {}

    tables: list[Table] = []
    for tname in ordered:
        spec = entities.get(tname)
        if spec is None:
            continue
        detail = f"{pack_ref}#{tname}" if pack_ref else tname
        tables.append(
            Table(
                name=tname,
                layer=Layer.SILVER,
                entity_type=entity_types.get(tname, EntityType.DIMENSION),
                grain=spec.get("grain", ""),
                attributes=[
                    Attribute.from_framework(c, detail=detail)
                    for c in spec.get("columns", [])
                ],
                provenance=Provenance(origin=Origin.PACK_ENTITY, detail=detail),
            )
        )

    return Domain(
        name=raw["domain"],
        industry=resolved_industry,
        display_name=raw.get("display_name", ""),
        description=raw.get("description", ""),
        standards=list(raw.get("standards", [])),
        tables=tables,
        pack_ref=pack_ref,
        provenance=Provenance(origin=Origin.PACK_ENTITY, detail=pack_ref),
        keywords=list(raw.get("domain_keywords") or raw.get("detection_signals") or []),
        derived_metrics=_normalise_metrics(raw.get("derived_metrics")),
        source_mappings=dict(raw.get("source_platform_mappings") or {})
        if isinstance(raw.get("source_platform_mappings"), dict)
        else {},
        value_normalisation={
            key[: -len("_normalisation")]: value
            for key, value in raw.items()
            if key.endswith("_normalisation") and isinstance(value, dict)
        },
    )
