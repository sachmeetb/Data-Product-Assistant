"""The canonical model: Domain -> Table -> Attribute."""

from .provenance import (
    PROTECTED_ORIGINS,
    MergeReport,
    Origin,
    Provenance,
    Suggestion,
)
from .tree import (
    Attribute,
    DataProductDesign,
    Domain,
    EntityType,
    Layer,
    PathError,
    PhysicalRef,
    SourceRef,
    Table,
    domain_from_framework,
)
from .types import CanonicalType, UnknownTypeError, normalize, register_target, render

__all__ = [
    "Attribute",
    "CanonicalType",
    "DataProductDesign",
    "Domain",
    "EntityType",
    "Layer",
    "MergeReport",
    "Origin",
    "PROTECTED_ORIGINS",
    "PathError",
    "PhysicalRef",
    "Provenance",
    "SourceRef",
    "Suggestion",
    "Table",
    "UnknownTypeError",
    "domain_from_framework",
    "normalize",
    "register_target",
    "render",
]
