"""Canonical type system.

Nothing in core stores an engine-native type. Platform-agnostic pipeline
rendering is impossible otherwise, and the inherited domain frameworks already
demonstrate the failure mode: they declare `DOUBLE`, which is a Spark type and
invalid in BigQuery.
"""

from __future__ import annotations

from enum import Enum


class CanonicalType(str, Enum):
    STRING = "STRING"
    INT = "INT"
    DECIMAL = "DECIMAL"
    FLOAT = "FLOAT"
    BOOL = "BOOL"
    DATE = "DATE"
    TIME = "TIME"
    DATETIME = "DATETIME"
    TIMESTAMP = "TIMESTAMP"
    BYTES = "BYTES"
    JSON = "JSON"
    STRUCT = "STRUCT"
    ARRAY = "ARRAY"
    GEOGRAPHY = "GEOGRAPHY"


_ALIASES: dict[str, CanonicalType] = {
    # string
    "STRING": CanonicalType.STRING,
    "VARCHAR": CanonicalType.STRING,
    "CHAR": CanonicalType.STRING,
    "TEXT": CanonicalType.STRING,
    # integer
    "INT": CanonicalType.INT,
    "INT64": CanonicalType.INT,
    "INTEGER": CanonicalType.INT,
    "BIGINT": CanonicalType.INT,
    "SMALLINT": CanonicalType.INT,
    "TINYINT": CanonicalType.INT,
    "LONG": CanonicalType.INT,
    # exact numeric
    "DECIMAL": CanonicalType.DECIMAL,
    "NUMERIC": CanonicalType.DECIMAL,
    "BIGNUMERIC": CanonicalType.DECIMAL,
    # approximate numeric
    "FLOAT": CanonicalType.FLOAT,
    "FLOAT64": CanonicalType.FLOAT,
    "DOUBLE": CanonicalType.FLOAT,
    "REAL": CanonicalType.FLOAT,
    # boolean
    "BOOL": CanonicalType.BOOL,
    "BOOLEAN": CanonicalType.BOOL,
    # temporal
    "DATE": CanonicalType.DATE,
    "TIME": CanonicalType.TIME,
    "DATETIME": CanonicalType.DATETIME,
    "TIMESTAMP": CanonicalType.TIMESTAMP,
    "TIMESTAMP_NTZ": CanonicalType.DATETIME,
    # other
    "BYTES": CanonicalType.BYTES,
    "BINARY": CanonicalType.BYTES,
    "JSON": CanonicalType.JSON,
    "VARIANT": CanonicalType.JSON,
    "STRUCT": CanonicalType.STRUCT,
    "RECORD": CanonicalType.STRUCT,
    "ARRAY": CanonicalType.ARRAY,
    "REPEATED": CanonicalType.ARRAY,
    "GEOGRAPHY": CanonicalType.GEOGRAPHY,
}


class UnknownTypeError(ValueError):
    pass


def normalize(raw: str) -> CanonicalType:
    """Map an engine-native or framework-declared type to its canonical form.

    Raises rather than guessing: a silently defaulted type is how `DOUBLE`
    survived into BigQuery-targeted frameworks in the first place.
    """
    if not raw:
        raise UnknownTypeError("empty type")

    # Strip parameterisation and element types: DECIMAL(18,2), ARRAY<STRING>
    head = raw.strip().upper()
    for delim in ("(", "<"):
        if delim in head:
            head = head.split(delim, 1)[0]
    head = head.strip()

    try:
        return _ALIASES[head]
    except KeyError:
        raise UnknownTypeError(f"unrecognised type {raw!r}") from None


_BIGQUERY: dict[CanonicalType, str] = {
    CanonicalType.STRING: "STRING",
    CanonicalType.INT: "INT64",
    CanonicalType.DECIMAL: "NUMERIC",
    CanonicalType.FLOAT: "FLOAT64",
    CanonicalType.BOOL: "BOOL",
    CanonicalType.DATE: "DATE",
    CanonicalType.TIME: "TIME",
    CanonicalType.DATETIME: "DATETIME",
    CanonicalType.TIMESTAMP: "TIMESTAMP",
    CanonicalType.BYTES: "BYTES",
    CanonicalType.JSON: "JSON",
    CanonicalType.STRUCT: "STRUCT",
    CanonicalType.ARRAY: "ARRAY",
    CanonicalType.GEOGRAPHY: "GEOGRAPHY",
}

_TARGETS: dict[str, dict[CanonicalType, str]] = {
    "bigquery": _BIGQUERY,
}


def render(t: CanonicalType, target: str = "bigquery") -> str:
    """Render a canonical type for a specific engine."""
    try:
        mapping = _TARGETS[target]
    except KeyError:
        raise UnknownTypeError(
            f"no type mapping registered for target {target!r}; "
            f"known: {sorted(_TARGETS)}"
        ) from None
    return mapping[t]


def register_target(name: str, mapping: dict[CanonicalType, str]) -> None:
    """Register a renderer target. Every CanonicalType must be covered."""
    missing = set(CanonicalType) - set(mapping)
    if missing:
        raise ValueError(
            f"target {name!r} is missing mappings for: "
            f"{sorted(m.value for m in missing)}"
        )
    _TARGETS[name] = mapping
