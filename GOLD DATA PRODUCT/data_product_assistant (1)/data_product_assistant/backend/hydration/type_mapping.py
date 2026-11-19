"""
type_mapping.py — map the catalog's Databricks/Delta-style column types to
BigQuery Standard SQL types.

The catalog files (backend/data/utility_catalog*.json) store column types in
Databricks notation (STRING, BIGINT, DOUBLE, INT, DECIMAL(18,2), BOOL,
TIMESTAMP, DATE). BigQuery uses a different set (STRING, INT64, FLOAT64,
NUMERIC(p,s), BOOL, TIMESTAMP, DATE). This module is the single source of
truth for that translation so the hydrator and its tests agree.

Pure module — no third-party imports.
"""

import re

# Simple 1:1 mappings (case-insensitive, no parameters).
_SCALAR_MAP = {
    "STRING": "STRING",
    "VARCHAR": "STRING",
    "CHAR": "STRING",
    "TEXT": "STRING",
    "BIGINT": "INT64",
    "LONG": "INT64",
    "INT": "INT64",
    "INTEGER": "INT64",
    "SMALLINT": "INT64",
    "TINYINT": "INT64",
    "DOUBLE": "FLOAT64",
    "FLOAT": "FLOAT64",
    "REAL": "FLOAT64",
    "BOOL": "BOOL",
    "BOOLEAN": "BOOL",
    "TIMESTAMP": "TIMESTAMP",
    "TIMESTAMP_NTZ": "DATETIME",
    "DATETIME": "DATETIME",
    "DATE": "DATE",
    "BYTES": "BYTES",
    "BINARY": "BYTES",
    "JSON": "JSON",
}

# DECIMAL(p,s) / NUMERIC(p,s) → NUMERIC(p,s), promoting to BIGNUMERIC when the
# precision exceeds what NUMERIC supports (38 digits, scale ≤ 9 for NUMERIC).
_DECIMAL_RE = re.compile(r"^\s*(?:DECIMAL|NUMERIC)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)\s*$", re.I)
_BARE_DECIMAL_RE = re.compile(r"^\s*(?:DECIMAL|NUMERIC)\s*$", re.I)

# BigQuery NUMERIC limits.
_NUMERIC_MAX_PRECISION = 38
_NUMERIC_MAX_SCALE = 9


class UnknownTypeError(ValueError):
    """Raised when a catalog type can't be mapped and strict=True."""


def to_bigquery_type(catalog_type: str, *, strict: bool = False) -> str:
    """
    Translate a single catalog column type to its BigQuery Standard SQL type.

    Args:
        catalog_type: e.g. "BIGINT", "DECIMAL(18,2)", "STRING".
        strict:       when True, raise UnknownTypeError on an unrecognised type;
                      when False (default), fall back to STRING (a safe, lossless
                      landing type) so hydration never hard-fails on an oddball.

    Returns the BigQuery type string, e.g. "INT64", "NUMERIC(18, 2)".
    """
    if not catalog_type or not isinstance(catalog_type, str):
        if strict:
            raise UnknownTypeError(f"empty/invalid catalog type: {catalog_type!r}")
        return "STRING"

    raw = catalog_type.strip()

    m = _DECIMAL_RE.match(raw)
    if m:
        precision, scale = int(m.group(1)), int(m.group(2))
        if precision > _NUMERIC_MAX_PRECISION or scale > _NUMERIC_MAX_SCALE:
            return f"BIGNUMERIC({precision}, {scale})"
        return f"NUMERIC({precision}, {scale})"

    if _BARE_DECIMAL_RE.match(raw):
        return "NUMERIC"

    key = raw.upper()
    if key in _SCALAR_MAP:
        return _SCALAR_MAP[key]

    # ARRAY<...> / STRUCT<...> / MAP<...> — not needed by the current catalogs,
    # but degrade sensibly instead of crashing.
    if key.startswith(("ARRAY", "STRUCT", "MAP")):
        if strict:
            raise UnknownTypeError(f"complex type not supported: {catalog_type!r}")
        return "STRING"

    if strict:
        raise UnknownTypeError(f"unrecognised catalog type: {catalog_type!r}")
    return "STRING"
