"""Canonical type system — platform-neutral type representation.

The transform IR and all dialect renderers reason about ``CanonicalType`` values
rather than raw platform strings (e.g. ``int4``, ``INTEGER``, ``NUMBER(38,0)``).
Each platform provides a ``TypeMappingProfile`` that maps in both directions.

Design goals:
- Analytical workload coverage without trying to model every database quirk.
- Loss-awareness: every mapping knows whether it is exact, lossy, or ambiguous.
- Round-trip testability: canonical → platform → canonical must be stable for the
  common types even when intermediate precision is approximate.
- No live connections required: profiles are pure data.

Usage::

    from workbench.backend.platform.type_system import (
        CanonicalType, get_profile,
    )
    profile = get_profile("postgres")
    desc = profile.map_to_canonical("numeric(18,4)")
    sql_type = profile.map_from_canonical(desc.canonical, desc.precision, desc.scale)
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── canonical type enum ────────────────────────────────────────────────────────

class CanonicalType(str, Enum):
    """Platform-neutral abstract type tokens used by the transform IR.

    Integer types are distinguished by bit width.  Floating-point types follow
    IEEE 754 binary widths.  Decimal is exact arbitrary-precision.  Timestamps
    carry microsecond resolution; the _tz variant carries timezone information.
    """
    # Integer
    int8 = "int8"
    int16 = "int16"
    int32 = "int32"
    int64 = "int64"
    # Floating-point
    float32 = "float32"
    float64 = "float64"
    # Exact numeric
    decimal = "decimal"
    # String / binary
    string = "string"
    binary = "binary"
    # Boolean
    boolean = "boolean"
    # Temporal
    date = "date"
    time_ms = "time_ms"                  # microsecond-resolution time of day
    timestamp_us = "timestamp_us"        # UTC microseconds, no timezone
    timestamp_tz_us = "timestamp_tz_us"  # microseconds with timezone
    interval_day = "interval_day"        # day/time interval
    interval_year = "interval_year"      # year/month interval
    # Semi-structured
    json = "json"
    array = "array"
    struct = "struct"
    map = "map"
    # Fallback
    unknown = "unknown"


# ── type warning ───────────────────────────────────────────────────────────────

class ConversionWarningKind(str, Enum):
    lossy = "lossy"             # canonical type is a subset of the platform type
    ambiguous = "ambiguous"     # multiple canonical types could represent this platform type
    unsupported = "unsupported" # no round-trip exists; use canonical.unknown
    precision_capped = "precision_capped"  # precision trimmed to canonical maximum


class TypeConversionWarning(BaseModel):
    """Records a known imperfection in the type mapping."""
    kind: ConversionWarningKind
    details: str = ""


# ── platform-agnostic cast cost (for feasibility/matching ranking) ──────────────
# A coarse, profile-FREE view of a raw type string — its family + a width ordinal —
# enough to tell a cheap widening cast (int32→int64) or a routine cross-representation
# cast (a numeric stored as text) from a genuinely structural mismatch (json ↔ int).
# Deliberately independent of the per-platform TypeMappingProfiles above (those need a
# known platform; feasibility sees bare estate/spec type strings with no platform).


class CastCost(str, Enum):
    none = "none"                                    # identical family + width
    widening = "widening"                            # same family, no loss (int32→int64, varchar→text)
    cross_family_castable = "cross_family_castable"  # numeric↔text, numeric↔boolean, temporal↔text, boolean↔text
    narrowing_lossy = "narrowing_lossy"              # same family, may lose (int64→int32, decimal→float, timestamp→date)
    incompatible = "incompatible"                    # complex/binary vs scalar, or unknown either side


# (regex on a lowercased raw type string) → (family, width ordinal). First match wins;
# ordered specific→general so `bigint` beats `int`. Width is only used to grade
# widening vs narrowing WITHIN a family.
_CAST_PATTERNS: list[tuple[str, str, int]] = [
    (r"tinyint|\bint8\b(?!.*unsigned)|\bbyteint\b", "numeric", 1),
    (r"smallint|\bint16\b|\bint2\b", "numeric", 2),
    (r"mediumint", "numeric", 3),
    (r"bigint|\bint64\b|\block long\b|\blong\b|int8", "numeric", 8),
    (r"double|float64|float8|binary_double", "numeric", 8),
    (r"real|float32|float4|binary_float", "numeric", 4),
    (r"decimal|numeric|number|money|\bdec\b", "numeric", 16),
    (r"\bfloat\b", "numeric", 6),
    (r"int|integer|int32|int4|serial", "numeric", 4),
    (r"bool|\bbit\b", "boolean", 1),
    (r"timestamp.*tz|timestamptz|.*with time zone", "temporal", 4),
    (r"timestamp|datetime", "temporal", 3),
    (r"\btime\b", "temporal", 2),
    (r"date", "temporal", 1),
    (r"interval", "temporal", 5),
    (r"json|variant|array|struct|map|object|geography|geometry|xml", "complex", 0),
    (r"bytea|blob|binary|varbinary|raw", "binary", 0),
    (r"char|text|string|clob|nchar|nvarchar|varchar|uuid", "text", 5),
]

# Unordered family pairs where a cross-representation cast routinely succeeds — a
# number as text, a boolean as 0/1, a date as text. Structural families (complex,
# binary) are never castable to a scalar.
_CASTABLE_CROSS: set[frozenset[str]] = {
    frozenset({"numeric", "text"}),
    frozenset({"numeric", "boolean"}),
    frozenset({"temporal", "text"}),
    frozenset({"boolean", "text"}),
}


def _cast_family_width(type_str: str) -> tuple[str, int]:
    """(family, width) best-effort from a raw type string — platform-agnostic."""
    s = (type_str or "").strip().lower()
    if not s:
        return ("unknown", 0)
    for pat, fam, width in _CAST_PATTERNS:
        if re.search(pat, s):
            return (fam, width)
    return ("unknown", 0)


def cast_cost(from_type: str, to_type: str) -> CastCost:
    """The cost of casting a value of ``from_type`` into ``to_type`` (coarse, for the
    feasibility ranking). ``none`` when the families+widths match; ``widening`` when the
    target is at least as wide in the same family; ``narrowing_lossy`` when it's a
    narrower same-family cast; ``cross_family_castable`` for the routine
    numeric/text/boolean/temporal cross-representations; ``incompatible`` otherwise
    (a complex/binary type, or an unknown family on either side)."""
    ff, fw = _cast_family_width(from_type)
    tf, tw = _cast_family_width(to_type)
    if ff == "unknown" or tf == "unknown":
        return CastCost.incompatible
    if ff == tf:
        if ff in ("complex", "binary"):
            # A struct→struct / blob→blob "cast" is only safe when identical; we can't
            # verify shape here, so treat differing complex types as incompatible.
            return CastCost.none if fw == tw else CastCost.incompatible
        if fw == tw:
            return CastCost.none
        return CastCost.widening if tw >= fw else CastCost.narrowing_lossy
    if frozenset({ff, tf}) in _CASTABLE_CROSS:
        return CastCost.cross_family_castable
    return CastCost.incompatible


# ── column type descriptor ─────────────────────────────────────────────────────

class ColumnTypeDescriptor(BaseModel):
    """The canonical description of a single column's type.

    ``precision`` and ``scale`` are the numeric/decimal precision and scale
    extracted from the platform type string (e.g. ``NUMERIC(18, 4)`` → 18, 4).
    For non-numeric types they are ``None``.

    ``platform_hint`` preserves the raw platform type string so the renderer can
    choose a more exact target type when the canonical type alone is ambiguous
    (e.g. ``varchar(255)`` vs ``text`` both map to ``string``).
    """
    canonical: CanonicalType
    precision: Optional[int] = None
    scale: Optional[int] = None
    nullable: bool = True
    platform_hint: str = ""
    warnings: list[TypeConversionWarning] = Field(default_factory=list)

    def is_exact(self) -> bool:
        return not self.warnings

    def has_warning(self, kind: ConversionWarningKind) -> bool:
        return any(w.kind == kind for w in self.warnings)


# ── type mapping entry ─────────────────────────────────────────────────────────

class TypeMapping(BaseModel):
    """One forward + reverse mapping pair for a platform type string pattern.

    ``platform_type_pattern`` is a normalised lowercase prefix or exact string,
    e.g. ``"numeric"`` matches both ``"numeric"`` and ``"numeric(18,4)"``.
    The ``reverse_platform_type`` is the canonical SQL type string emitted when
    rendering back to this platform (e.g. ``"NUMERIC(38,10)"``).
    """
    platform_type_pattern: str = Field(
        ...,
        description=(
            "Normalised lowercase prefix to match against the platform type string. "
            "Exact match when no parentheses; prefix match otherwise."
        ),
    )
    canonical: CanonicalType
    reverse_platform_type: str = Field(
        ...,
        description="SQL type string to emit when rendering canonical → platform",
    )
    precision_from_string: bool = False   # extract (precision, scale) from the type string
    warnings: list[TypeConversionWarning] = Field(default_factory=list)


# ── type mapping profile ───────────────────────────────────────────────────────

# Regex to extract (precision, scale) from strings like "numeric(18,4)" or "decimal(10)"
_PRECISION_RE = re.compile(r"\(\s*(\d+)(?:\s*,\s*(\d+))?\s*\)")


class TypeMappingProfile(BaseModel):
    """Per-platform type mapping profile.

    Provides forward (platform → canonical) and reverse (canonical → platform)
    mappings.  The forward mapping is a priority-ordered list: first match wins.
    An ``unknown`` fallback entry should always be last.

    Usage::

        profile = TypeMappingProfile(platform_id="postgres", mappings=[...])
        desc = profile.map_to_canonical("numeric(18,4)")
        sql = profile.map_from_canonical(CanonicalType.decimal, 18, 4)
    """
    platform_id: str
    display_name: str = ""
    mappings: list[TypeMapping] = Field(default_factory=list)
    unmapped_fallback: CanonicalType = CanonicalType.unknown

    def map_to_canonical(self, platform_type: str) -> ColumnTypeDescriptor:
        """Map a platform type string to a ``ColumnTypeDescriptor``.

        Matching is case-insensitive and strips trailing whitespace.  Precision
        and scale are extracted from parenthesised suffixes when the mapping
        declares ``precision_from_string=True``.
        """
        normalised = platform_type.strip().lower()
        for mapping in self.mappings:
            pattern = mapping.platform_type_pattern
            if normalised == pattern or normalised.startswith(pattern + "(") or normalised.startswith(pattern + " "):
                precision: Optional[int] = None
                scale: Optional[int] = None
                if mapping.precision_from_string:
                    m = _PRECISION_RE.search(normalised)
                    if m:
                        precision = int(m.group(1))
                        scale = int(m.group(2)) if m.group(2) is not None else None
                return ColumnTypeDescriptor(
                    canonical=mapping.canonical,
                    precision=precision,
                    scale=scale,
                    platform_hint=platform_type,
                    warnings=list(mapping.warnings),
                )
        return ColumnTypeDescriptor(
            canonical=self.unmapped_fallback,
            platform_hint=platform_type,
            warnings=[
                TypeConversionWarning(
                    kind=ConversionWarningKind.unsupported,
                    details=f"No mapping found for platform type {platform_type!r} on {self.platform_id!r}",
                )
            ],
        )

    def map_from_canonical(
        self,
        canonical: CanonicalType,
        precision: Optional[int] = None,
        scale: Optional[int] = None,
    ) -> str:
        """Return the platform SQL type string for the given canonical type.

        Uses the first mapping whose ``canonical`` field matches.  Appends
        ``(precision, scale)`` or ``(precision)`` when the reverse type contains
        the placeholder ``{p}`` / ``{s}``.
        """
        for mapping in self.mappings:
            if mapping.canonical == canonical:
                rev = mapping.reverse_platform_type
                if "{p}" in rev or "{s}" in rev:
                    p = precision or 38
                    s = scale if scale is not None else 10
                    if "{s}" in rev:
                        return rev.replace("{p}", str(p)).replace("{s}", str(s))
                    return rev.replace("{p}", str(p))
                return rev
        # Fallback: just return the canonical name as uppercase SQL
        return canonical.value.upper()


# ── built-in platform profiles ─────────────────────────────────────────────────

def _postgres_profile() -> TypeMappingProfile:
    W = TypeConversionWarning
    K = ConversionWarningKind
    return TypeMappingProfile(
        platform_id="postgres",
        display_name="PostgreSQL",
        mappings=[
            # ── integer ──
            TypeMapping(platform_type_pattern="smallint",   canonical=CanonicalType.int16, reverse_platform_type="SMALLINT"),
            TypeMapping(platform_type_pattern="int2",       canonical=CanonicalType.int16, reverse_platform_type="SMALLINT"),
            TypeMapping(platform_type_pattern="integer",    canonical=CanonicalType.int32, reverse_platform_type="INTEGER"),
            TypeMapping(platform_type_pattern="int4",       canonical=CanonicalType.int32, reverse_platform_type="INTEGER"),
            TypeMapping(platform_type_pattern="int",        canonical=CanonicalType.int32, reverse_platform_type="INTEGER"),
            TypeMapping(platform_type_pattern="bigint",     canonical=CanonicalType.int64, reverse_platform_type="BIGINT"),
            TypeMapping(platform_type_pattern="int8",       canonical=CanonicalType.int64, reverse_platform_type="BIGINT"),
            # serial types → their underlying int
            TypeMapping(platform_type_pattern="smallserial", canonical=CanonicalType.int16, reverse_platform_type="SMALLINT",
                        warnings=[W(kind=K.lossy, details="SERIAL auto-increment not represented in canonical type")]),
            TypeMapping(platform_type_pattern="serial",      canonical=CanonicalType.int32, reverse_platform_type="INTEGER",
                        warnings=[W(kind=K.lossy, details="SERIAL auto-increment not represented in canonical type")]),
            TypeMapping(platform_type_pattern="bigserial",   canonical=CanonicalType.int64, reverse_platform_type="BIGINT",
                        warnings=[W(kind=K.lossy, details="BIGSERIAL auto-increment not represented in canonical type")]),
            # ── floating-point ──
            TypeMapping(platform_type_pattern="real",             canonical=CanonicalType.float32, reverse_platform_type="REAL"),
            TypeMapping(platform_type_pattern="float4",           canonical=CanonicalType.float32, reverse_platform_type="REAL"),
            TypeMapping(platform_type_pattern="double precision",  canonical=CanonicalType.float64, reverse_platform_type="DOUBLE PRECISION"),
            TypeMapping(platform_type_pattern="float8",           canonical=CanonicalType.float64, reverse_platform_type="DOUBLE PRECISION"),
            TypeMapping(platform_type_pattern="float",            canonical=CanonicalType.float64, reverse_platform_type="DOUBLE PRECISION"),
            # ── exact numeric ──
            TypeMapping(platform_type_pattern="numeric",  canonical=CanonicalType.decimal, reverse_platform_type="NUMERIC({p},{s})",
                        precision_from_string=True),
            TypeMapping(platform_type_pattern="decimal",  canonical=CanonicalType.decimal, reverse_platform_type="NUMERIC({p},{s})",
                        precision_from_string=True),
            # money → decimal with warning
            TypeMapping(platform_type_pattern="money", canonical=CanonicalType.decimal, reverse_platform_type="NUMERIC(19,2)",
                        warnings=[W(kind=K.lossy, details="MONEY locale-specific formatting lost")]),
            # ── string ──
            TypeMapping(platform_type_pattern="character varying", canonical=CanonicalType.string, reverse_platform_type="TEXT"),
            TypeMapping(platform_type_pattern="varchar",           canonical=CanonicalType.string, reverse_platform_type="TEXT"),
            TypeMapping(platform_type_pattern="character",         canonical=CanonicalType.string, reverse_platform_type="TEXT",
                        warnings=[W(kind=K.lossy, details="CHAR padding semantics not preserved")]),
            TypeMapping(platform_type_pattern="char",              canonical=CanonicalType.string, reverse_platform_type="TEXT",
                        warnings=[W(kind=K.lossy, details="CHAR padding semantics not preserved")]),
            TypeMapping(platform_type_pattern="text",              canonical=CanonicalType.string, reverse_platform_type="TEXT"),
            TypeMapping(platform_type_pattern="name",              canonical=CanonicalType.string, reverse_platform_type="TEXT",
                        warnings=[W(kind=K.lossy, details="pg NAME type limited to 63 bytes")]),
            TypeMapping(platform_type_pattern="uuid",              canonical=CanonicalType.string, reverse_platform_type="TEXT",
                        warnings=[W(kind=K.ambiguous, details="UUID stored as canonical string; UUID constraint not preserved")]),
            # ── binary ──
            TypeMapping(platform_type_pattern="bytea", canonical=CanonicalType.binary, reverse_platform_type="BYTEA"),
            # ── boolean ──
            TypeMapping(platform_type_pattern="boolean", canonical=CanonicalType.boolean, reverse_platform_type="BOOLEAN"),
            TypeMapping(platform_type_pattern="bool",    canonical=CanonicalType.boolean, reverse_platform_type="BOOLEAN"),
            # ── temporal ──
            TypeMapping(platform_type_pattern="date",                            canonical=CanonicalType.date,          reverse_platform_type="DATE"),
            # temporal — more specific patterns before less specific ones (prefix-match order)
            TypeMapping(platform_type_pattern="time without time zone",          canonical=CanonicalType.time_ms,       reverse_platform_type="TIME"),
            TypeMapping(platform_type_pattern="time",                            canonical=CanonicalType.time_ms,       reverse_platform_type="TIME"),
            TypeMapping(platform_type_pattern="timestamp with time zone",        canonical=CanonicalType.timestamp_tz_us, reverse_platform_type="TIMESTAMPTZ"),
            TypeMapping(platform_type_pattern="timestamp without time zone",     canonical=CanonicalType.timestamp_us,    reverse_platform_type="TIMESTAMP"),
            TypeMapping(platform_type_pattern="timestamptz",                     canonical=CanonicalType.timestamp_tz_us, reverse_platform_type="TIMESTAMPTZ"),
            TypeMapping(platform_type_pattern="timestamp",                       canonical=CanonicalType.timestamp_us,    reverse_platform_type="TIMESTAMP"),
            TypeMapping(platform_type_pattern="interval",                        canonical=CanonicalType.interval_day,   reverse_platform_type="INTERVAL",
                        warnings=[W(kind=K.ambiguous, details="Postgres INTERVAL can represent year/month or day/time; stored as interval_day")]),
            # ── semi-structured ──
            TypeMapping(platform_type_pattern="json",  canonical=CanonicalType.json, reverse_platform_type="JSONB"),
            TypeMapping(platform_type_pattern="jsonb", canonical=CanonicalType.json, reverse_platform_type="JSONB"),
            # ── array ──
            TypeMapping(platform_type_pattern="array", canonical=CanonicalType.array, reverse_platform_type="TEXT",
                        warnings=[W(kind=K.lossy, details="Postgres array type not representable in canonical without element type")]),
            # catch-all (must be last)
            TypeMapping(platform_type_pattern="", canonical=CanonicalType.unknown, reverse_platform_type="TEXT",
                        warnings=[W(kind=K.unsupported, details="No Postgres mapping found")]),
        ],
    )


def _mysql_profile() -> TypeMappingProfile:
    W = TypeConversionWarning
    K = ConversionWarningKind
    return TypeMappingProfile(
        platform_id="mysql",
        display_name="MySQL / MariaDB",
        mappings=[
            # ── integer ──
            TypeMapping(platform_type_pattern="tinyint",   canonical=CanonicalType.int8,  reverse_platform_type="TINYINT"),
            TypeMapping(platform_type_pattern="smallint",  canonical=CanonicalType.int16, reverse_platform_type="SMALLINT"),
            TypeMapping(platform_type_pattern="mediumint", canonical=CanonicalType.int32, reverse_platform_type="INT",
                        warnings=[W(kind=K.lossy, details="MEDIUMINT 3-byte range mapped to INT")]),
            TypeMapping(platform_type_pattern="int",       canonical=CanonicalType.int32, reverse_platform_type="INT"),
            TypeMapping(platform_type_pattern="bigint",    canonical=CanonicalType.int64, reverse_platform_type="BIGINT"),
            # ── floating-point ──
            TypeMapping(platform_type_pattern="float",  canonical=CanonicalType.float32, reverse_platform_type="FLOAT"),
            TypeMapping(platform_type_pattern="double", canonical=CanonicalType.float64, reverse_platform_type="DOUBLE"),
            TypeMapping(platform_type_pattern="real",   canonical=CanonicalType.float64, reverse_platform_type="DOUBLE"),
            # ── exact numeric ──
            TypeMapping(platform_type_pattern="decimal", canonical=CanonicalType.decimal, reverse_platform_type="DECIMAL({p},{s})",
                        precision_from_string=True),
            TypeMapping(platform_type_pattern="numeric", canonical=CanonicalType.decimal, reverse_platform_type="DECIMAL({p},{s})",
                        precision_from_string=True),
            # ── string ──
            TypeMapping(platform_type_pattern="varchar",    canonical=CanonicalType.string, reverse_platform_type="TEXT"),
            TypeMapping(platform_type_pattern="char",       canonical=CanonicalType.string, reverse_platform_type="TEXT",
                        warnings=[W(kind=K.lossy, details="CHAR padding semantics not preserved")]),
            TypeMapping(platform_type_pattern="tinytext",   canonical=CanonicalType.string, reverse_platform_type="TEXT"),
            TypeMapping(platform_type_pattern="text",       canonical=CanonicalType.string, reverse_platform_type="TEXT"),
            TypeMapping(platform_type_pattern="mediumtext", canonical=CanonicalType.string, reverse_platform_type="TEXT"),
            TypeMapping(platform_type_pattern="longtext",   canonical=CanonicalType.string, reverse_platform_type="TEXT"),
            TypeMapping(platform_type_pattern="enum",       canonical=CanonicalType.string, reverse_platform_type="TEXT",
                        warnings=[W(kind=K.lossy, details="MySQL ENUM constraint not preserved in canonical string")]),
            TypeMapping(platform_type_pattern="set",        canonical=CanonicalType.string, reverse_platform_type="TEXT",
                        warnings=[W(kind=K.lossy, details="MySQL SET type stored as canonical string")]),
            # ── binary ──
            TypeMapping(platform_type_pattern="binary",     canonical=CanonicalType.binary, reverse_platform_type="BLOB"),
            TypeMapping(platform_type_pattern="varbinary",  canonical=CanonicalType.binary, reverse_platform_type="BLOB"),
            TypeMapping(platform_type_pattern="tinyblob",   canonical=CanonicalType.binary, reverse_platform_type="BLOB"),
            TypeMapping(platform_type_pattern="blob",       canonical=CanonicalType.binary, reverse_platform_type="BLOB"),
            TypeMapping(platform_type_pattern="mediumblob", canonical=CanonicalType.binary, reverse_platform_type="BLOB"),
            TypeMapping(platform_type_pattern="longblob",   canonical=CanonicalType.binary, reverse_platform_type="BLOB"),
            # ── boolean ──
            TypeMapping(platform_type_pattern="bool", canonical=CanonicalType.boolean, reverse_platform_type="TINYINT(1)"),
            TypeMapping(platform_type_pattern="boolean", canonical=CanonicalType.boolean, reverse_platform_type="TINYINT(1)"),
            # ── temporal ──
            TypeMapping(platform_type_pattern="date",      canonical=CanonicalType.date,          reverse_platform_type="DATE"),
            TypeMapping(platform_type_pattern="time",      canonical=CanonicalType.time_ms,       reverse_platform_type="TIME"),
            TypeMapping(platform_type_pattern="year",      canonical=CanonicalType.int16,         reverse_platform_type="SMALLINT",
                        warnings=[W(kind=K.lossy, details="MySQL YEAR stored as int16")]),
            TypeMapping(platform_type_pattern="datetime",  canonical=CanonicalType.timestamp_us,  reverse_platform_type="DATETIME(6)"),
            TypeMapping(platform_type_pattern="timestamp", canonical=CanonicalType.timestamp_tz_us, reverse_platform_type="DATETIME(6)",
                        warnings=[W(kind=K.lossy, details="MySQL TIMESTAMP is stored UTC but timezone info not portable")]),
            # ── semi-structured ──
            TypeMapping(platform_type_pattern="json", canonical=CanonicalType.json, reverse_platform_type="JSON"),
            # catch-all
            TypeMapping(platform_type_pattern="", canonical=CanonicalType.unknown, reverse_platform_type="TEXT",
                        warnings=[W(kind=K.unsupported, details="No MySQL mapping found")]),
        ],
    )


# ── profile registry ───────────────────────────────────────────────────────────

_BUILTIN_PROFILES: dict[str, TypeMappingProfile] = {}


def _snowflake_profile() -> TypeMappingProfile:
    """Snowflake type mapping profile.

    Key Snowflake specifics:
    - All floating-point types are 64-bit (FLOAT, FLOAT4, FLOAT8, DOUBLE, REAL are synonyms).
    - Fixed-precision numeric is NUMBER(p,s); DECIMAL/NUMERIC are aliases.
    - VARCHAR, STRING, TEXT are all synonyms for the same variable-length type.
    - TIMESTAMP is an alias for TIMESTAMP_NTZ by default (session-configurable).
    - VARIANT is the semi-structured catch-all; ARRAY/OBJECT are specialisations.
    - No native TIME-with-microseconds; TIME stores hh:mm:ss.nnn (sub-second).
    """
    W = TypeConversionWarning
    K = ConversionWarningKind
    return TypeMappingProfile(
        platform_id="snowflake",
        display_name="Snowflake",
        mappings=[
            # ── integer ──
            TypeMapping(platform_type_pattern="byteint",   canonical=CanonicalType.int8,  reverse_platform_type="BYTEINT"),
            TypeMapping(platform_type_pattern="tinyint",   canonical=CanonicalType.int8,  reverse_platform_type="BYTEINT"),
            TypeMapping(platform_type_pattern="smallint",  canonical=CanonicalType.int16, reverse_platform_type="SMALLINT"),
            TypeMapping(platform_type_pattern="int2",      canonical=CanonicalType.int16, reverse_platform_type="SMALLINT"),
            TypeMapping(platform_type_pattern="integer",   canonical=CanonicalType.int32, reverse_platform_type="INTEGER"),
            TypeMapping(platform_type_pattern="int4",      canonical=CanonicalType.int32, reverse_platform_type="INTEGER"),
            TypeMapping(platform_type_pattern="int",       canonical=CanonicalType.int32, reverse_platform_type="INTEGER"),
            TypeMapping(platform_type_pattern="bigint",    canonical=CanonicalType.int64, reverse_platform_type="BIGINT"),
            TypeMapping(platform_type_pattern="int8",      canonical=CanonicalType.int64, reverse_platform_type="BIGINT"),
            # ── floating-point — all 64-bit in Snowflake ──
            TypeMapping(platform_type_pattern="float4",  canonical=CanonicalType.float64, reverse_platform_type="FLOAT",
                        warnings=[W(kind=K.lossy, details="Snowflake FLOAT4 is stored as 64-bit; canonical float32 loses that distinction")]),
            TypeMapping(platform_type_pattern="float8",  canonical=CanonicalType.float64, reverse_platform_type="FLOAT"),
            TypeMapping(platform_type_pattern="float",   canonical=CanonicalType.float64, reverse_platform_type="FLOAT"),
            TypeMapping(platform_type_pattern="double",  canonical=CanonicalType.float64, reverse_platform_type="DOUBLE"),
            TypeMapping(platform_type_pattern="real",    canonical=CanonicalType.float64, reverse_platform_type="DOUBLE"),
            # ── exact numeric ──
            TypeMapping(platform_type_pattern="number",   canonical=CanonicalType.decimal, reverse_platform_type="NUMBER({p},{s})",
                        precision_from_string=True),
            TypeMapping(platform_type_pattern="numeric",  canonical=CanonicalType.decimal, reverse_platform_type="NUMBER({p},{s})",
                        precision_from_string=True),
            TypeMapping(platform_type_pattern="decimal",  canonical=CanonicalType.decimal, reverse_platform_type="NUMBER({p},{s})",
                        precision_from_string=True),
            # ── string — all synonyms in Snowflake ──
            TypeMapping(platform_type_pattern="character varying", canonical=CanonicalType.string, reverse_platform_type="VARCHAR"),
            TypeMapping(platform_type_pattern="nvarchar2", canonical=CanonicalType.string, reverse_platform_type="VARCHAR"),
            TypeMapping(platform_type_pattern="nvarchar",  canonical=CanonicalType.string, reverse_platform_type="VARCHAR"),
            TypeMapping(platform_type_pattern="varchar",   canonical=CanonicalType.string, reverse_platform_type="VARCHAR"),
            TypeMapping(platform_type_pattern="string",    canonical=CanonicalType.string, reverse_platform_type="VARCHAR"),
            TypeMapping(platform_type_pattern="text",      canonical=CanonicalType.string, reverse_platform_type="VARCHAR"),
            TypeMapping(platform_type_pattern="nchar",     canonical=CanonicalType.string, reverse_platform_type="VARCHAR",
                        warnings=[W(kind=K.lossy, details="NCHAR padding semantics not preserved")]),
            TypeMapping(platform_type_pattern="character", canonical=CanonicalType.string, reverse_platform_type="VARCHAR",
                        warnings=[W(kind=K.lossy, details="CHAR padding semantics not preserved")]),
            TypeMapping(platform_type_pattern="char",      canonical=CanonicalType.string, reverse_platform_type="VARCHAR",
                        warnings=[W(kind=K.lossy, details="CHAR padding semantics not preserved")]),
            # ── binary ──
            TypeMapping(platform_type_pattern="varbinary", canonical=CanonicalType.binary, reverse_platform_type="BINARY"),
            TypeMapping(platform_type_pattern="binary",    canonical=CanonicalType.binary, reverse_platform_type="BINARY"),
            # ── boolean ──
            TypeMapping(platform_type_pattern="boolean", canonical=CanonicalType.boolean, reverse_platform_type="BOOLEAN"),
            TypeMapping(platform_type_pattern="bool",    canonical=CanonicalType.boolean, reverse_platform_type="BOOLEAN"),
            # ── temporal — most-specific first ──
            TypeMapping(platform_type_pattern="timestamp_ltz", canonical=CanonicalType.timestamp_tz_us, reverse_platform_type="TIMESTAMP_LTZ"),
            TypeMapping(platform_type_pattern="timestamp_tz",  canonical=CanonicalType.timestamp_tz_us, reverse_platform_type="TIMESTAMP_LTZ"),
            TypeMapping(platform_type_pattern="timestamp_ntz", canonical=CanonicalType.timestamp_us,    reverse_platform_type="TIMESTAMP_NTZ"),
            TypeMapping(platform_type_pattern="datetime",      canonical=CanonicalType.timestamp_us,    reverse_platform_type="TIMESTAMP_NTZ"),
            TypeMapping(platform_type_pattern="timestamp",     canonical=CanonicalType.timestamp_us,    reverse_platform_type="TIMESTAMP_NTZ",
                        warnings=[W(kind=K.ambiguous, details="Snowflake TIMESTAMP is an alias for TIMESTAMP_NTZ by default but is session-configurable")]),
            TypeMapping(platform_type_pattern="date", canonical=CanonicalType.date,    reverse_platform_type="DATE"),
            TypeMapping(platform_type_pattern="time", canonical=CanonicalType.time_ms, reverse_platform_type="TIME"),
            # ── semi-structured ──
            TypeMapping(platform_type_pattern="variant", canonical=CanonicalType.json,   reverse_platform_type="VARIANT"),
            TypeMapping(platform_type_pattern="array",   canonical=CanonicalType.array,  reverse_platform_type="ARRAY"),
            TypeMapping(platform_type_pattern="object",  canonical=CanonicalType.struct, reverse_platform_type="OBJECT"),
            # catch-all
            TypeMapping(platform_type_pattern="", canonical=CanonicalType.unknown, reverse_platform_type="VARIANT",
                        warnings=[W(kind=K.unsupported, details="No Snowflake mapping found")]),
        ],
    )


def _databricks_profile() -> TypeMappingProfile:
    """Databricks / Delta Lake (Spark SQL) type mapping profile.

    Key Databricks specifics:
    - Type names follow Apache Spark SQL; INT and INTEGER are both valid.
    - TIMESTAMP has session-timezone semantics (UTC by default in DBR); maps to
      timestamp_tz_us with an ambiguity warning.  TIMESTAMP_NTZ (DBR 3.0+) maps
      to timestamp_us with no warning.
    - There is no native TIME type; time-of-day values are stored as STRING or BIGINT.
    - VARIANT is a preview feature (DBR 15+); STRUCT/MAP/ARRAY require element types
      that are not represented in the canonical type token.
    """
    W = TypeConversionWarning
    K = ConversionWarningKind
    return TypeMappingProfile(
        platform_id="databricks",
        display_name="Databricks (Delta Lake / Spark SQL)",
        mappings=[
            # ── integer ──
            TypeMapping(platform_type_pattern="tinyint",  canonical=CanonicalType.int8,  reverse_platform_type="TINYINT"),
            TypeMapping(platform_type_pattern="byte",     canonical=CanonicalType.int8,  reverse_platform_type="TINYINT"),
            TypeMapping(platform_type_pattern="smallint", canonical=CanonicalType.int16, reverse_platform_type="SMALLINT"),
            TypeMapping(platform_type_pattern="short",    canonical=CanonicalType.int16, reverse_platform_type="SMALLINT"),
            TypeMapping(platform_type_pattern="integer",  canonical=CanonicalType.int32, reverse_platform_type="INT"),
            TypeMapping(platform_type_pattern="int",      canonical=CanonicalType.int32, reverse_platform_type="INT"),
            TypeMapping(platform_type_pattern="bigint",   canonical=CanonicalType.int64, reverse_platform_type="BIGINT"),
            TypeMapping(platform_type_pattern="long",     canonical=CanonicalType.int64, reverse_platform_type="BIGINT"),
            # ── floating-point ──
            TypeMapping(platform_type_pattern="float",  canonical=CanonicalType.float32, reverse_platform_type="FLOAT"),
            TypeMapping(platform_type_pattern="real",   canonical=CanonicalType.float32, reverse_platform_type="FLOAT"),
            TypeMapping(platform_type_pattern="double", canonical=CanonicalType.float64, reverse_platform_type="DOUBLE"),
            # ── exact numeric ──
            TypeMapping(platform_type_pattern="decimal", canonical=CanonicalType.decimal, reverse_platform_type="DECIMAL({p},{s})",
                        precision_from_string=True),
            TypeMapping(platform_type_pattern="numeric", canonical=CanonicalType.decimal, reverse_platform_type="DECIMAL({p},{s})",
                        precision_from_string=True),
            TypeMapping(platform_type_pattern="dec",     canonical=CanonicalType.decimal, reverse_platform_type="DECIMAL({p},{s})",
                        precision_from_string=True),
            # ── string ──
            TypeMapping(platform_type_pattern="string",  canonical=CanonicalType.string, reverse_platform_type="STRING"),
            TypeMapping(platform_type_pattern="varchar",  canonical=CanonicalType.string, reverse_platform_type="STRING"),
            TypeMapping(platform_type_pattern="char",     canonical=CanonicalType.string, reverse_platform_type="STRING",
                        warnings=[W(kind=K.lossy, details="CHAR padding semantics not preserved")]),
            TypeMapping(platform_type_pattern="text",     canonical=CanonicalType.string, reverse_platform_type="STRING"),
            # ── binary ──
            TypeMapping(platform_type_pattern="binary", canonical=CanonicalType.binary, reverse_platform_type="BINARY"),
            TypeMapping(platform_type_pattern="bytes",  canonical=CanonicalType.binary, reverse_platform_type="BINARY"),
            # ── boolean ──
            TypeMapping(platform_type_pattern="boolean", canonical=CanonicalType.boolean, reverse_platform_type="BOOLEAN"),
            TypeMapping(platform_type_pattern="bool",    canonical=CanonicalType.boolean, reverse_platform_type="BOOLEAN"),
            # ── temporal — most-specific first ──
            TypeMapping(platform_type_pattern="timestamp_ntz", canonical=CanonicalType.timestamp_us,    reverse_platform_type="TIMESTAMP_NTZ"),
            TypeMapping(platform_type_pattern="timestamp",     canonical=CanonicalType.timestamp_tz_us, reverse_platform_type="TIMESTAMP",
                        warnings=[W(kind=K.ambiguous, details="Databricks TIMESTAMP uses session timezone; canonical timestamp_tz_us implies UTC")]),
            TypeMapping(platform_type_pattern="date",          canonical=CanonicalType.date,            reverse_platform_type="DATE"),
            # no native TIME in Databricks — store as STRING with warning
            TypeMapping(platform_type_pattern="time",          canonical=CanonicalType.time_ms,         reverse_platform_type="STRING",
                        warnings=[W(kind=K.unsupported, details="Databricks has no native TIME type; stored as STRING")]),
            # interval — Databricks supports DAY-TIME and YEAR-MONTH intervals
            TypeMapping(platform_type_pattern="interval day",  canonical=CanonicalType.interval_day,   reverse_platform_type="INTERVAL DAY TO SECOND"),
            TypeMapping(platform_type_pattern="interval year", canonical=CanonicalType.interval_year,  reverse_platform_type="INTERVAL YEAR TO MONTH"),
            TypeMapping(platform_type_pattern="interval",      canonical=CanonicalType.interval_day,   reverse_platform_type="INTERVAL DAY TO SECOND",
                        warnings=[W(kind=K.ambiguous, details="INTERVAL without unit mapped to day-time interval")]),
            # ── semi-structured ──
            TypeMapping(platform_type_pattern="variant", canonical=CanonicalType.json,   reverse_platform_type="VARIANT",
                        warnings=[W(kind=K.lossy, details="Databricks VARIANT is a preview feature (DBR 15+)")]),
            TypeMapping(platform_type_pattern="map",     canonical=CanonicalType.map,    reverse_platform_type="MAP<STRING,STRING>",
                        warnings=[W(kind=K.lossy, details="MAP element types not represented in canonical; emitted as MAP<STRING,STRING>")]),
            TypeMapping(platform_type_pattern="struct",  canonical=CanonicalType.struct, reverse_platform_type="STRUCT<>",
                        warnings=[W(kind=K.lossy, details="STRUCT field names/types not represented in canonical")]),
            TypeMapping(platform_type_pattern="array",   canonical=CanonicalType.array,  reverse_platform_type="ARRAY<STRING>",
                        warnings=[W(kind=K.lossy, details="ARRAY element type not represented in canonical; emitted as ARRAY<STRING>")]),
            # catch-all
            TypeMapping(platform_type_pattern="", canonical=CanonicalType.unknown, reverse_platform_type="STRING",
                        warnings=[W(kind=K.unsupported, details="No Databricks mapping found")]),
        ],
    )


def _oracle_profile() -> TypeMappingProfile:
    """Oracle Database type mapping profile.

    Key Oracle specifics:
    - NUMBER is the universal numeric; NUMBER(p,s) is exact decimal, NUMBER with no
      precision is arbitrary-precision (ambiguous → decimal). INTEGER is NUMBER(38).
    - DATE carries a time component to the second (it is NOT date-only) — the single
      biggest migration gotcha; mapped to timestamp with an ambiguity warning.
    - VARCHAR2/NVARCHAR2 are the string types (VARCHAR is reserved/legacy).
    - BINARY_FLOAT/BINARY_DOUBLE are true IEEE floats; FLOAT is NUMBER-based.
    - Native BOOLEAN only in 23c+; earlier schemas encode it as NUMBER(1)/CHAR(1).
    """
    W = TypeConversionWarning
    K = ConversionWarningKind
    return TypeMappingProfile(
        platform_id="oracle",
        display_name="Oracle Database",
        mappings=[
            # ── exact/decimal numeric (NUMBER is the universal type) ──
            TypeMapping(platform_type_pattern="number",  canonical=CanonicalType.decimal, reverse_platform_type="NUMBER({p},{s})",
                        precision_from_string=True,
                        warnings=[W(kind=K.ambiguous, details="Oracle NUMBER without precision is arbitrary-precision; a fixed target scale may cap it")]),
            TypeMapping(platform_type_pattern="integer", canonical=CanonicalType.int64, reverse_platform_type="NUMBER(38)"),
            TypeMapping(platform_type_pattern="int",     canonical=CanonicalType.int64, reverse_platform_type="NUMBER(38)"),
            TypeMapping(platform_type_pattern="smallint", canonical=CanonicalType.int16, reverse_platform_type="NUMBER(5)"),
            TypeMapping(platform_type_pattern="numeric", canonical=CanonicalType.decimal, reverse_platform_type="NUMBER({p},{s})",
                        precision_from_string=True),
            TypeMapping(platform_type_pattern="decimal", canonical=CanonicalType.decimal, reverse_platform_type="NUMBER({p},{s})",
                        precision_from_string=True),
            # ── floating-point ──
            TypeMapping(platform_type_pattern="binary_float",  canonical=CanonicalType.float32, reverse_platform_type="BINARY_FLOAT"),
            TypeMapping(platform_type_pattern="binary_double", canonical=CanonicalType.float64, reverse_platform_type="BINARY_DOUBLE"),
            TypeMapping(platform_type_pattern="float", canonical=CanonicalType.float64, reverse_platform_type="BINARY_DOUBLE",
                        warnings=[W(kind=K.ambiguous, details="Oracle FLOAT is NUMBER-based binary precision, not IEEE; mapped to float64")]),
            # ── string ──
            TypeMapping(platform_type_pattern="varchar2",  canonical=CanonicalType.string, reverse_platform_type="VARCHAR2(4000)"),
            TypeMapping(platform_type_pattern="nvarchar2", canonical=CanonicalType.string, reverse_platform_type="VARCHAR2(4000)"),
            TypeMapping(platform_type_pattern="varchar",   canonical=CanonicalType.string, reverse_platform_type="VARCHAR2(4000)"),
            TypeMapping(platform_type_pattern="nchar",     canonical=CanonicalType.string, reverse_platform_type="VARCHAR2(4000)",
                        warnings=[W(kind=K.lossy, details="NCHAR padding semantics not preserved")]),
            TypeMapping(platform_type_pattern="char",      canonical=CanonicalType.string, reverse_platform_type="VARCHAR2(4000)",
                        warnings=[W(kind=K.lossy, details="CHAR padding semantics not preserved")]),
            TypeMapping(platform_type_pattern="nclob", canonical=CanonicalType.string, reverse_platform_type="CLOB"),
            TypeMapping(platform_type_pattern="clob",  canonical=CanonicalType.string, reverse_platform_type="CLOB"),
            TypeMapping(platform_type_pattern="long",  canonical=CanonicalType.string, reverse_platform_type="CLOB",
                        warnings=[W(kind=K.lossy, details="Oracle LONG is deprecated; migrate to CLOB")]),
            TypeMapping(platform_type_pattern="rowid",  canonical=CanonicalType.string, reverse_platform_type="VARCHAR2(4000)",
                        warnings=[W(kind=K.ambiguous, details="ROWID/UROWID physical addresses are not portable")]),
            TypeMapping(platform_type_pattern="urowid", canonical=CanonicalType.string, reverse_platform_type="VARCHAR2(4000)",
                        warnings=[W(kind=K.ambiguous, details="ROWID/UROWID physical addresses are not portable")]),
            # ── binary ──
            TypeMapping(platform_type_pattern="blob",     canonical=CanonicalType.binary, reverse_platform_type="BLOB"),
            TypeMapping(platform_type_pattern="raw",      canonical=CanonicalType.binary, reverse_platform_type="RAW(2000)"),
            TypeMapping(platform_type_pattern="long raw", canonical=CanonicalType.binary, reverse_platform_type="BLOB",
                        warnings=[W(kind=K.lossy, details="LONG RAW is deprecated; migrate to BLOB")]),
            TypeMapping(platform_type_pattern="bfile",    canonical=CanonicalType.binary, reverse_platform_type="BLOB",
                        warnings=[W(kind=K.unsupported, details="BFILE references external files; only the locator is portable")]),
            # ── boolean (23c+) ──
            TypeMapping(platform_type_pattern="boolean", canonical=CanonicalType.boolean, reverse_platform_type="BOOLEAN"),
            # ── temporal — most-specific first ──
            TypeMapping(platform_type_pattern="timestamp with local time zone", canonical=CanonicalType.timestamp_tz_us, reverse_platform_type="TIMESTAMP WITH LOCAL TIME ZONE"),
            TypeMapping(platform_type_pattern="timestamp with time zone",       canonical=CanonicalType.timestamp_tz_us, reverse_platform_type="TIMESTAMP WITH TIME ZONE"),
            TypeMapping(platform_type_pattern="timestamp",                      canonical=CanonicalType.timestamp_us,    reverse_platform_type="TIMESTAMP"),
            TypeMapping(platform_type_pattern="date", canonical=CanonicalType.timestamp_us, reverse_platform_type="DATE",
                        warnings=[W(kind=K.ambiguous, details="Oracle DATE carries a time component (to the second); it is not date-only")]),
            TypeMapping(platform_type_pattern="interval year", canonical=CanonicalType.interval_year, reverse_platform_type="INTERVAL YEAR TO MONTH"),
            TypeMapping(platform_type_pattern="interval day",  canonical=CanonicalType.interval_day,  reverse_platform_type="INTERVAL DAY TO SECOND"),
            # ── semi-structured ──
            TypeMapping(platform_type_pattern="xmltype", canonical=CanonicalType.json, reverse_platform_type="CLOB",
                        warnings=[W(kind=K.lossy, details="XMLTYPE has no canonical equivalent; migrated as text")]),
            TypeMapping(platform_type_pattern="json", canonical=CanonicalType.json, reverse_platform_type="JSON"),
            # catch-all
            TypeMapping(platform_type_pattern="", canonical=CanonicalType.unknown, reverse_platform_type="VARCHAR2(4000)",
                        warnings=[W(kind=K.unsupported, details="No Oracle mapping found")]),
        ],
    )


def _sqlserver_profile() -> TypeMappingProfile:
    """Microsoft SQL Server / Azure SQL type mapping profile.

    Key SQL Server specifics:
    - ``timestamp`` / ``rowversion`` is an 8-byte auto-versioning BINARY, NOT a
      temporal type — the single biggest gotcha; mapped to binary with a warning.
    - TINYINT is unsigned 0–255 (not signed) — mapped to int16 to avoid overflow.
    - DATETIME rounds to ~3.33 ms; DATETIME2 is full precision; SMALLDATETIME is
      minute precision. DATETIMEOFFSET carries a zone.
    - REAL is 32-bit; FLOAT(53) is 64-bit.
    """
    W = TypeConversionWarning
    K = ConversionWarningKind
    return TypeMappingProfile(
        platform_id="sqlserver",
        display_name="Microsoft SQL Server / Azure SQL",
        mappings=[
            # ── boolean ──
            TypeMapping(platform_type_pattern="bit", canonical=CanonicalType.boolean, reverse_platform_type="BIT"),
            # ── integer ──
            TypeMapping(platform_type_pattern="tinyint",  canonical=CanonicalType.int16, reverse_platform_type="SMALLINT",
                        warnings=[W(kind=K.lossy, details="SQL Server TINYINT is unsigned 0-255; widened to int16 to avoid overflow")]),
            TypeMapping(platform_type_pattern="smallint", canonical=CanonicalType.int16, reverse_platform_type="SMALLINT"),
            TypeMapping(platform_type_pattern="bigint",   canonical=CanonicalType.int64, reverse_platform_type="BIGINT"),
            TypeMapping(platform_type_pattern="int",      canonical=CanonicalType.int32, reverse_platform_type="INT"),
            # ── exact numeric ──
            TypeMapping(platform_type_pattern="decimal",    canonical=CanonicalType.decimal, reverse_platform_type="DECIMAL({p},{s})",
                        precision_from_string=True),
            TypeMapping(platform_type_pattern="numeric",    canonical=CanonicalType.decimal, reverse_platform_type="DECIMAL({p},{s})",
                        precision_from_string=True),
            TypeMapping(platform_type_pattern="smallmoney", canonical=CanonicalType.decimal, reverse_platform_type="DECIMAL(10,4)",
                        warnings=[W(kind=K.lossy, details="SMALLMONEY currency semantics collapsed to DECIMAL(10,4)")]),
            TypeMapping(platform_type_pattern="money",      canonical=CanonicalType.decimal, reverse_platform_type="DECIMAL(19,4)",
                        warnings=[W(kind=K.lossy, details="MONEY currency semantics collapsed to DECIMAL(19,4)")]),
            # ── floating-point ──
            TypeMapping(platform_type_pattern="real",  canonical=CanonicalType.float32, reverse_platform_type="REAL"),
            TypeMapping(platform_type_pattern="float", canonical=CanonicalType.float64, reverse_platform_type="FLOAT(53)"),
            # ── string ──
            TypeMapping(platform_type_pattern="nvarchar", canonical=CanonicalType.string, reverse_platform_type="NVARCHAR(MAX)"),
            TypeMapping(platform_type_pattern="varchar",  canonical=CanonicalType.string, reverse_platform_type="VARCHAR(MAX)"),
            TypeMapping(platform_type_pattern="nchar",    canonical=CanonicalType.string, reverse_platform_type="NVARCHAR(MAX)",
                        warnings=[W(kind=K.lossy, details="NCHAR padding semantics not preserved")]),
            TypeMapping(platform_type_pattern="char",     canonical=CanonicalType.string, reverse_platform_type="VARCHAR(MAX)",
                        warnings=[W(kind=K.lossy, details="CHAR padding semantics not preserved")]),
            TypeMapping(platform_type_pattern="ntext",    canonical=CanonicalType.string, reverse_platform_type="NVARCHAR(MAX)",
                        warnings=[W(kind=K.lossy, details="NTEXT is deprecated; migrate to NVARCHAR(MAX)")]),
            TypeMapping(platform_type_pattern="text",     canonical=CanonicalType.string, reverse_platform_type="VARCHAR(MAX)",
                        warnings=[W(kind=K.lossy, details="TEXT is deprecated; migrate to VARCHAR(MAX)")]),
            TypeMapping(platform_type_pattern="uniqueidentifier", canonical=CanonicalType.string, reverse_platform_type="VARCHAR(36)",
                        warnings=[W(kind=K.ambiguous, details="UNIQUEIDENTIFIER (GUID) migrated as string")]),
            TypeMapping(platform_type_pattern="sysname", canonical=CanonicalType.string, reverse_platform_type="NVARCHAR(128)"),
            # ── binary — NB: rowversion/timestamp are binary, not temporal ──
            TypeMapping(platform_type_pattern="rowversion", canonical=CanonicalType.binary, reverse_platform_type="VARBINARY(8)",
                        warnings=[W(kind=K.ambiguous, details="ROWVERSION is an auto-versioning binary, not a value; it should be regenerated on the target")]),
            TypeMapping(platform_type_pattern="varbinary", canonical=CanonicalType.binary, reverse_platform_type="VARBINARY(MAX)"),
            TypeMapping(platform_type_pattern="binary",    canonical=CanonicalType.binary, reverse_platform_type="VARBINARY(MAX)"),
            TypeMapping(platform_type_pattern="image",     canonical=CanonicalType.binary, reverse_platform_type="VARBINARY(MAX)",
                        warnings=[W(kind=K.lossy, details="IMAGE is deprecated; migrate to VARBINARY(MAX)")]),
            # ── temporal — most-specific first ──
            TypeMapping(platform_type_pattern="datetimeoffset", canonical=CanonicalType.timestamp_tz_us, reverse_platform_type="DATETIMEOFFSET"),
            TypeMapping(platform_type_pattern="datetime2",      canonical=CanonicalType.timestamp_us,    reverse_platform_type="DATETIME2"),
            TypeMapping(platform_type_pattern="smalldatetime",  canonical=CanonicalType.timestamp_us,    reverse_platform_type="DATETIME2",
                        warnings=[W(kind=K.lossy, details="SMALLDATETIME has minute precision; widened to DATETIME2")]),
            TypeMapping(platform_type_pattern="datetime",       canonical=CanonicalType.timestamp_us,    reverse_platform_type="DATETIME2",
                        warnings=[W(kind=K.lossy, details="DATETIME rounds to ~3.33 ms; DATETIME2 is exact")]),
            # `timestamp` (SQL Server) IS rowversion, NOT a datetime — must NOT map to a temporal type.
            TypeMapping(platform_type_pattern="timestamp", canonical=CanonicalType.binary, reverse_platform_type="VARBINARY(8)",
                        warnings=[W(kind=K.ambiguous, details="SQL Server TIMESTAMP is a synonym for ROWVERSION (binary), NOT a temporal type")]),
            TypeMapping(platform_type_pattern="date", canonical=CanonicalType.date,    reverse_platform_type="DATE"),
            TypeMapping(platform_type_pattern="time", canonical=CanonicalType.time_ms, reverse_platform_type="TIME"),
            # ── semi-structured ──
            TypeMapping(platform_type_pattern="xml", canonical=CanonicalType.json, reverse_platform_type="NVARCHAR(MAX)",
                        warnings=[W(kind=K.lossy, details="XML has no canonical equivalent; migrated as text")]),
            TypeMapping(platform_type_pattern="json", canonical=CanonicalType.json, reverse_platform_type="NVARCHAR(MAX)"),
            TypeMapping(platform_type_pattern="sql_variant", canonical=CanonicalType.unknown, reverse_platform_type="NVARCHAR(MAX)",
                        warnings=[W(kind=K.unsupported, details="SQL_VARIANT holds mixed types; not portable")]),
            # catch-all
            TypeMapping(platform_type_pattern="", canonical=CanonicalType.unknown, reverse_platform_type="NVARCHAR(MAX)",
                        warnings=[W(kind=K.unsupported, details="No SQL Server mapping found")]),
        ],
    )


def _register_builtins() -> None:
    for profile in [
        _postgres_profile(),
        _mysql_profile(),
        _snowflake_profile(),
        _databricks_profile(),
        _oracle_profile(),
        _sqlserver_profile(),
    ]:
        _BUILTIN_PROFILES[profile.platform_id] = profile


_register_builtins()


def get_profile(platform_id: str) -> TypeMappingProfile:
    """Return the ``TypeMappingProfile`` for ``platform_id``.

    Raises ``KeyError`` when the platform is unknown.  Never falls back to
    Postgres — the caller must be explicit about which platform they are
    operating against (fail-closed, per ADR-9).
    """
    if platform_id not in _BUILTIN_PROFILES:
        raise KeyError(
            f"No type mapping profile registered for platform {platform_id!r}. "
            f"Known platforms: {sorted(_BUILTIN_PROFILES)}"
        )
    return _BUILTIN_PROFILES[platform_id]


def register_profile(profile: TypeMappingProfile) -> None:
    """Register a custom ``TypeMappingProfile``, overriding any existing entry."""
    _BUILTIN_PROFILES[profile.platform_id] = profile


def list_profiles() -> list[str]:
    """Return all registered platform IDs."""
    return sorted(_BUILTIN_PROFILES)
