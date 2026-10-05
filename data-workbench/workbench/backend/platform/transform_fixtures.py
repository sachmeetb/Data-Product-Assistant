"""SQL transform conformance fixtures — Phase 4 renderer gate.

A ``SqlTransformFixture`` declares:
  - the transform kind and params (matching the existing transform DSL in
    ``generate_view_ddl.py`` / ``transformKind`` / ``transformParams``);
  - a minimal source column set (names only; types inferred from params);
  - the expected SQL expression fragment **per platform**.

These fixtures are the specification.  Phase 4 renderer conformance tests drive
a ``SqlExpressionRenderer`` (to be implemented per-platform) and assert that
its output matches the ``expected_sql`` entry.  Until renderers exist, the
fixtures serve as authoritative documentation of cross-platform SQL behaviour.

Platform IDs used here must match ``type_system.get_profile()`` keys:
  ``postgres`` | ``mysql`` | ``snowflake`` | ``databricks``

Expression fragments use single-quoted identifier placeholders:
  ``{col}``  — the primary source column name, double-quoted for Postgres/Snowflake,
               backtick-quoted for MySQL/Databricks.
Renderers substitute real identifiers at emit time.  The ``{col}`` token is
retained in fixture strings so the intent is readable without a symbol table.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ── fixture model ──────────────────────────────────────────────────────────────

class SqlTransformFixture(BaseModel):
    """One row of the conformance matrix: transform → expected SQL per platform.

    Fields
    ------
    name : str
        Short slug, unique within the fixture set.
    description : str
        Human-readable intent — used in test failure messages.
    transform_kind : str
        Matches the ``transformKind`` values in the existing DSL
        (cast, concat, literal, arithmetic, coalesce, case, hash, mask, bucket, window).
    transform_params : dict[str, Any]
        The params dict as it would appear in a ``ColumnMapping.transformParams``.
    source_columns : list[str]
        Column name tokens used in the expression (positional; no type).
    expected_sql : dict[str, str]
        Platform ID → expected SQL expression fragment.
        A missing platform entry means the transform is not yet specified for
        that platform (renderer should raise NotImplementedError, not silently
        emit Postgres SQL).
    notes : str
        Any caveats about platform differences, known lossy behaviour, etc.
    """
    name: str
    description: str = ""
    transform_kind: str
    transform_params: dict[str, Any] = Field(default_factory=dict)
    source_columns: list[str] = Field(default_factory=list)
    expected_sql: dict[str, str] = Field(
        default_factory=dict,
        description="platform_id → expected SQL expression fragment",
    )
    notes: str = ""

    def platforms(self) -> list[str]:
        return sorted(self.expected_sql)

    def has_platform(self, platform_id: str) -> bool:
        return platform_id in self.expected_sql


# ── golden-path fixture set ────────────────────────────────────────────────────
# Identifier quoting conventions:
#   Postgres / Snowflake: double-quotes ("col")
#   MySQL:                backticks (`col`)
#   Databricks:           backticks (`col`)
#
# Fixture strings use {col} / {col2} as symbolic placeholders so they document
# the intent without hard-coding a specific column name.

GOLDEN_PATH_FIXTURES: list[SqlTransformFixture] = [

    # ── cast to integer ────────────────────────────────────────────────────────
    SqlTransformFixture(
        name="cast_to_int32",
        description="Cast a string or numeric column to 32-bit integer",
        transform_kind="cast",
        transform_params={"target_canonical_type": "int32"},
        source_columns=["amount_str"],
        expected_sql={
            "postgres":   'CAST("{col}" AS INTEGER)',
            "mysql":      'CAST(`{col}` AS SIGNED)',
            "snowflake":  'CAST("{col}" AS INTEGER)',
            "databricks": 'CAST(`{col}` AS INT)',
        },
    ),

    SqlTransformFixture(
        name="cast_to_int64",
        description="Cast to 64-bit integer",
        transform_kind="cast",
        transform_params={"target_canonical_type": "int64"},
        source_columns=["id_str"],
        expected_sql={
            "postgres":   'CAST("{col}" AS BIGINT)',
            "mysql":      'CAST(`{col}` AS UNSIGNED)',
            "snowflake":  'CAST("{col}" AS BIGINT)',
            "databricks": 'CAST(`{col}` AS BIGINT)',
        },
    ),

    # ── cast to float ──────────────────────────────────────────────────────────
    SqlTransformFixture(
        name="cast_to_float64",
        description="Cast to double-precision float",
        transform_kind="cast",
        transform_params={"target_canonical_type": "float64"},
        source_columns=["score_str"],
        expected_sql={
            "postgres":   'CAST("{col}" AS DOUBLE PRECISION)',
            "mysql":      'CAST(`{col}` AS DOUBLE)',
            "snowflake":  'CAST("{col}" AS FLOAT)',
            "databricks": 'CAST(`{col}` AS DOUBLE)',
        },
    ),

    # ── cast to decimal ────────────────────────────────────────────────────────
    SqlTransformFixture(
        name="cast_to_decimal_18_4",
        description="Cast to fixed-precision decimal (18 digits, 4 decimal places)",
        transform_kind="cast",
        transform_params={"target_canonical_type": "decimal", "precision": 18, "scale": 4},
        source_columns=["price_str"],
        expected_sql={
            "postgres":   'CAST("{col}" AS NUMERIC(18,4))',
            "mysql":      'CAST(`{col}` AS DECIMAL(18,4))',
            "snowflake":  'CAST("{col}" AS NUMBER(18,4))',
            "databricks": 'CAST(`{col}` AS DECIMAL(18,4))',
        },
    ),

    # ── cast to string ─────────────────────────────────────────────────────────
    SqlTransformFixture(
        name="cast_to_string",
        description="Cast any column to text/string",
        transform_kind="cast",
        transform_params={"target_canonical_type": "string"},
        source_columns=["id_num"],
        expected_sql={
            "postgres":   'CAST("{col}" AS TEXT)',
            "mysql":      'CAST(`{col}` AS CHAR)',
            "snowflake":  'CAST("{col}" AS VARCHAR)',
            "databricks": 'CAST(`{col}` AS STRING)',
        },
    ),

    # ── cast to boolean ────────────────────────────────────────────────────────
    SqlTransformFixture(
        name="cast_to_boolean",
        description="Cast integer or string to boolean",
        transform_kind="cast",
        transform_params={"target_canonical_type": "boolean"},
        source_columns=["active_flag"],
        expected_sql={
            "postgres":   'CAST("{col}" AS BOOLEAN)',
            "snowflake":  'CAST("{col}" AS BOOLEAN)',
            "databricks": 'CAST(`{col}` AS BOOLEAN)',
        },
        notes="MySQL does not have a native BOOLEAN in CAST; omitted from expected_sql.",
    ),

    # ── cast to date ───────────────────────────────────────────────────────────
    SqlTransformFixture(
        name="cast_to_date",
        description="Cast a timestamp or string to date (no time component)",
        transform_kind="cast",
        transform_params={"target_canonical_type": "date"},
        source_columns=["created_at"],
        expected_sql={
            "postgres":   'CAST("{col}" AS DATE)',
            "mysql":      'CAST(`{col}` AS DATE)',
            "snowflake":  'CAST("{col}" AS DATE)',
            "databricks": 'CAST(`{col}` AS DATE)',
        },
    ),

    # ── cast to timestamp (no tz) ──────────────────────────────────────────────
    SqlTransformFixture(
        name="cast_to_timestamp_us",
        description="Cast to timezone-naive timestamp (microsecond resolution)",
        transform_kind="cast",
        transform_params={"target_canonical_type": "timestamp_us"},
        source_columns=["event_time_str"],
        expected_sql={
            "postgres":   'CAST("{col}" AS TIMESTAMP)',
            "mysql":      'CAST(`{col}` AS DATETIME)',
            "snowflake":  'CAST("{col}" AS TIMESTAMP_NTZ)',
            "databricks": 'CAST(`{col}` AS TIMESTAMP_NTZ)',
        },
    ),

    # ── cast to timestamp with tz ──────────────────────────────────────────────
    SqlTransformFixture(
        name="cast_to_timestamp_tz_us",
        description="Cast to timezone-aware timestamp",
        transform_kind="cast",
        transform_params={"target_canonical_type": "timestamp_tz_us"},
        source_columns=["event_time_str"],
        expected_sql={
            "postgres":   'CAST("{col}" AS TIMESTAMPTZ)',
            "snowflake":  'CAST("{col}" AS TIMESTAMP_LTZ)',
            "databricks": 'CAST(`{col}` AS TIMESTAMP)',
        },
        notes=(
            "MySQL does not have a timezone-aware timestamp type; omitted. "
            "Databricks TIMESTAMP uses session timezone (UTC by default)."
        ),
    ),

    # ── string concatenation ───────────────────────────────────────────────────
    SqlTransformFixture(
        name="concat_two_cols_pipe",
        description="Concatenate two columns, coalescing nulls to empty string first",
        transform_kind="concat",
        transform_params={"separator": "", "null_as_empty": True},
        source_columns=["first_name", "last_name"],
        expected_sql={
            "postgres":   "COALESCE(\"{col}\", '') || COALESCE(\"{col2}\", '')",
            "mysql":      "CONCAT(COALESCE(`{col}`, ''), COALESCE(`{col2}`, ''))",
            "snowflake":  "CONCAT(COALESCE(\"{col}\", ''), COALESCE(\"{col2}\", ''))",
            "databricks": "CONCAT(COALESCE(`{col}`, ''), COALESCE(`{col2}`, ''))",
        },
    ),

    SqlTransformFixture(
        name="concat_two_cols_with_space",
        description="Concatenate two columns with a space separator",
        transform_kind="concat",
        transform_params={"separator": " ", "null_as_empty": True},
        source_columns=["first_name", "last_name"],
        expected_sql={
            "postgres":   "COALESCE(\"{col}\", '') || ' ' || COALESCE(\"{col2}\", '')",
            "mysql":      "CONCAT(COALESCE(`{col}`, ''), ' ', COALESCE(`{col2}`, ''))",
            "snowflake":  "CONCAT(COALESCE(\"{col}\", ''), ' ', COALESCE(\"{col2}\", ''))",
            "databricks": "CONCAT(COALESCE(`{col}`, ''), ' ', COALESCE(`{col2}`, ''))",
        },
    ),

    # ── literal ────────────────────────────────────────────────────────────────
    SqlTransformFixture(
        name="literal_string",
        description="Emit a constant string literal (no source column)",
        transform_kind="literal",
        transform_params={"value": "ACTIVE", "canonical_type": "string"},
        source_columns=[],
        expected_sql={
            "postgres":   "'ACTIVE'",
            "mysql":      "'ACTIVE'",
            "snowflake":  "'ACTIVE'",
            "databricks": "'ACTIVE'",
        },
    ),

    SqlTransformFixture(
        name="literal_integer",
        description="Emit a constant integer literal",
        transform_kind="literal",
        transform_params={"value": 0, "canonical_type": "int32"},
        source_columns=[],
        expected_sql={
            "postgres":   "0",
            "mysql":      "0",
            "snowflake":  "0",
            "databricks": "0",
        },
    ),

    SqlTransformFixture(
        name="literal_null",
        description="Emit a typed NULL",
        transform_kind="literal",
        transform_params={"value": None, "canonical_type": "string"},
        source_columns=[],
        expected_sql={
            "postgres":   "CAST(NULL AS TEXT)",
            "mysql":      "CAST(NULL AS CHAR)",
            "snowflake":  "CAST(NULL AS VARCHAR)",
            "databricks": "CAST(NULL AS STRING)",
        },
    ),

    # ── arithmetic ─────────────────────────────────────────────────────────────
    SqlTransformFixture(
        name="arithmetic_multiply",
        description="Multiply two numeric columns",
        transform_kind="arithmetic",
        transform_params={"operator": "*"},
        source_columns=["unit_price", "quantity"],
        expected_sql={
            "postgres":   '"{col}" * "{col2}"',
            "mysql":      '`{col}` * `{col2}`',
            "snowflake":  '"{col}" * "{col2}"',
            "databricks": '`{col}` * `{col2}`',
        },
    ),

    SqlTransformFixture(
        name="arithmetic_divide_safe",
        description="Safe division — coerce zero divisor to NULL to avoid divide-by-zero",
        transform_kind="arithmetic",
        transform_params={"operator": "/", "safe_divide": True},
        source_columns=["numerator", "denominator"],
        expected_sql={
            "postgres":   'CASE WHEN "{col2}" = 0 THEN NULL ELSE "{col}" / NULLIF("{col2}", 0) END',
            "mysql":      'CASE WHEN `{col2}` = 0 THEN NULL ELSE `{col}` / NULLIF(`{col2}`, 0) END',
            "snowflake":  'CASE WHEN "{col2}" = 0 THEN NULL ELSE "{col}" / NULLIF("{col2}", 0) END',
            "databricks": 'CASE WHEN `{col2}` = 0 THEN NULL ELSE `{col}` / NULLIF(`{col2}`, 0) END',
        },
    ),

    # ── hash / mask ────────────────────────────────────────────────────────────
    SqlTransformFixture(
        name="hash_md5",
        description="MD5 hash of a string column (produces hex string)",
        transform_kind="hash",
        transform_params={"algorithm": "md5"},
        source_columns=["email"],
        expected_sql={
            "postgres":   'MD5(CAST("{col}" AS TEXT))',
            "mysql":      'MD5(`{col}`)',
            "snowflake":  'MD5("{col}")',
            "databricks": 'MD5(CAST(`{col}` AS STRING))',
        },
    ),

    SqlTransformFixture(
        name="hash_sha256",
        description="SHA-256 hash as hex string",
        transform_kind="hash",
        transform_params={"algorithm": "sha256"},
        source_columns=["ssn"],
        expected_sql={
            "postgres":   "ENCODE(DIGEST(CAST(\"{col}\" AS TEXT), 'sha256'), 'hex')",
            "snowflake":  'SHA2("{col}", 256)',
            "databricks": 'SHA2(CAST(`{col}` AS STRING), 256)',
        },
        notes=(
            "MySQL SHA2 function exists (SHA2(`col`, 256)) but returns VARBINARY; "
            "omitted pending charset clarification. "
            "Postgres uses pgcrypto DIGEST — requires the pgcrypto extension."
        ),
    ),

    SqlTransformFixture(
        name="mask_email",
        description="Mask email address — keep domain, replace local part with asterisks",
        transform_kind="mask",
        transform_params={"mask_kind": "email"},
        source_columns=["email"],
        expected_sql={
            "postgres":   "REGEXP_REPLACE(\"{col}\", '^[^@]+', '***')",
            "snowflake":  "REGEXP_REPLACE(\"{col}\", '^[^@]+', '***')",
            "databricks": "REGEXP_REPLACE(`{col}`, '^[^@]+', '***')",
        },
        notes="MySQL REGEXP_REPLACE requires MySQL 8.0+; not included to avoid version gate.",
    ),

    # ── coalesce ───────────────────────────────────────────────────────────────
    SqlTransformFixture(
        name="coalesce_two_cols",
        description="Return first non-NULL from two columns",
        transform_kind="coalesce",
        transform_params={},
        source_columns=["preferred_name", "legal_name"],
        expected_sql={
            "postgres":   'COALESCE("{col}", "{col2}")',
            "mysql":      'COALESCE(`{col}`, `{col2}`)',
            "snowflake":  'COALESCE("{col}", "{col2}")',
            "databricks": 'COALESCE(`{col}`, `{col2}`)',
        },
    ),

    # ── CASE WHEN ──────────────────────────────────────────────────────────────
    SqlTransformFixture(
        name="case_when_simple",
        description="Simple two-branch CASE expression",
        transform_kind="case",
        transform_params={
            "branches": [
                {"when": "{col} = 'Y'", "then": "'active'"},
            ],
            "else_value": "'inactive'",
        },
        source_columns=["flag"],
        expected_sql={
            "postgres":   "CASE WHEN \"{col}\" = 'Y' THEN 'active' ELSE 'inactive' END",
            "mysql":      "CASE WHEN `{col}` = 'Y' THEN 'active' ELSE 'inactive' END",
            "snowflake":  "CASE WHEN \"{col}\" = 'Y' THEN 'active' ELSE 'inactive' END",
            "databricks": "CASE WHEN `{col}` = 'Y' THEN 'active' ELSE 'inactive' END",
        },
    ),

    # ── bucket ─────────────────────────────────────────────────────────────────
    SqlTransformFixture(
        name="bucket_numeric",
        description="Bucket a numeric column into three labelled bands",
        transform_kind="bucket",
        transform_params={
            "boundaries": [0, 100, 1000],
            "labels": ["low", "medium", "high"],
        },
        source_columns=["order_value"],
        expected_sql={
            "postgres": (
                "CASE "
                "WHEN \"{col}\" < 0 THEN 'low' "
                "WHEN \"{col}\" < 100 THEN 'low' "
                "WHEN \"{col}\" < 1000 THEN 'medium' "
                "ELSE 'high' END"
            ),
            "snowflake": (
                "CASE "
                "WHEN \"{col}\" < 0 THEN 'low' "
                "WHEN \"{col}\" < 100 THEN 'low' "
                "WHEN \"{col}\" < 1000 THEN 'medium' "
                "ELSE 'high' END"
            ),
            "databricks": (
                "CASE "
                "WHEN `{col}` < 0 THEN 'low' "
                "WHEN `{col}` < 100 THEN 'low' "
                "WHEN `{col}` < 1000 THEN 'medium' "
                "ELSE 'high' END"
            ),
        },
    ),
]


# ── lookup helpers ─────────────────────────────────────────────────────────────

_FIXTURE_INDEX: dict[str, SqlTransformFixture] = {f.name: f for f in GOLDEN_PATH_FIXTURES}


def get_fixture(name: str) -> SqlTransformFixture:
    """Return a fixture by name, raising ``KeyError`` when not found."""
    if name not in _FIXTURE_INDEX:
        raise KeyError(
            f"No fixture named {name!r}. "
            f"Available: {sorted(_FIXTURE_INDEX)}"
        )
    return _FIXTURE_INDEX[name]


def fixtures_for_platform(platform_id: str) -> list[SqlTransformFixture]:
    """Return all fixtures that include an entry for ``platform_id``."""
    return [f for f in GOLDEN_PATH_FIXTURES if f.has_platform(platform_id)]


def fixtures_for_kind(transform_kind: str) -> list[SqlTransformFixture]:
    """Return all fixtures for a given ``transform_kind``."""
    return [f for f in GOLDEN_PATH_FIXTURES if f.transform_kind == transform_kind]
