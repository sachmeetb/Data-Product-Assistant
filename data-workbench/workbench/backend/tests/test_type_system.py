"""Tests for the canonical type system (platform/type_system.py).

Coverage:
- CanonicalType enum completeness
- TypeMappingProfile.map_to_canonical: Postgres and MySQL forward mappings
- TypeMappingProfile.map_from_canonical: reverse mappings
- Precision/scale extraction from parenthesised type strings
- Loss-awareness: lossy/ambiguous mappings carry warnings
- Fail-closed: unknown type → CanonicalType.unknown + unsupported warning
- No Postgres fallback: get_profile("unknown_platform") raises KeyError
- Custom profile registration
- Round-trip stability: canonical → platform → canonical for common types
- Explicit case-insensitivity
"""
import pytest

from workbench.backend.platform.type_system import (
    CanonicalType,
    ColumnTypeDescriptor,
    ConversionWarningKind,
    TypeConversionWarning,
    TypeMapping,
    TypeMappingProfile,
    get_profile,
    list_profiles,
    register_profile,
)


# ── helpers ────────────────────────────────────────────────────────────────────

PG = get_profile("postgres")
MY = get_profile("mysql")


def _canonical(profile: TypeMappingProfile, type_str: str) -> CanonicalType:
    return profile.map_to_canonical(type_str).canonical


def _reverse(profile: TypeMappingProfile, ct: CanonicalType, p=None, s=None) -> str:
    return profile.map_from_canonical(ct, p, s)


# ── CanonicalType enum ─────────────────────────────────────────────────────────

class TestCanonicalTypeEnum:
    def test_integer_types_all_present(self):
        for name in ("int8", "int16", "int32", "int64"):
            assert CanonicalType(name) is not None

    def test_float_types(self):
        assert CanonicalType.float32 != CanonicalType.float64

    def test_temporal_types(self):
        for name in ("date", "time_ms", "timestamp_us", "timestamp_tz_us",
                     "interval_day", "interval_year"):
            assert CanonicalType(name) is not None

    def test_semi_structured(self):
        for name in ("json", "array", "struct", "map"):
            assert CanonicalType(name) is not None

    def test_unknown_exists(self):
        assert CanonicalType.unknown.value == "unknown"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            CanonicalType("NOT_A_TYPE")


# ── profile registry ───────────────────────────────────────────────────────────

class TestProfileRegistry:
    def test_postgres_registered(self):
        assert "postgres" in list_profiles()

    def test_mysql_registered(self):
        assert "mysql" in list_profiles()

    def test_unknown_platform_raises_key_error(self):
        with pytest.raises(KeyError, match="No type mapping profile"):
            get_profile("redshift")

    def test_no_fallback_to_postgres(self):
        """ADR-9: no silent Postgres fallback for unknown platforms."""
        with pytest.raises(KeyError):
            get_profile("teradata")  # not yet implemented

    def test_custom_profile_registration(self):
        profile = TypeMappingProfile(
            platform_id="test_platform",
            mappings=[
                TypeMapping(
                    platform_type_pattern="myint",
                    canonical=CanonicalType.int32,
                    reverse_platform_type="INTEGER",
                )
            ],
        )
        register_profile(profile)
        assert "test_platform" in list_profiles()
        assert get_profile("test_platform").platform_id == "test_platform"
        # clean up so other tests are not affected
        from workbench.backend.platform.type_system import _BUILTIN_PROFILES
        del _BUILTIN_PROFILES["test_platform"]

    def test_list_profiles_sorted(self):
        profiles = list_profiles()
        assert profiles == sorted(profiles)


# ── Postgres forward mappings ──────────────────────────────────────────────────

class TestPostgresForwardMappings:
    def test_integer_variants(self):
        assert _canonical(PG, "smallint") == CanonicalType.int16
        assert _canonical(PG, "int2") == CanonicalType.int16
        assert _canonical(PG, "integer") == CanonicalType.int32
        assert _canonical(PG, "int4") == CanonicalType.int32
        assert _canonical(PG, "int") == CanonicalType.int32
        assert _canonical(PG, "bigint") == CanonicalType.int64
        assert _canonical(PG, "int8") == CanonicalType.int64

    def test_float_types(self):
        assert _canonical(PG, "real") == CanonicalType.float32
        assert _canonical(PG, "float4") == CanonicalType.float32
        assert _canonical(PG, "double precision") == CanonicalType.float64
        assert _canonical(PG, "float8") == CanonicalType.float64

    def test_decimal_with_precision_scale(self):
        desc = PG.map_to_canonical("numeric(18,4)")
        assert desc.canonical == CanonicalType.decimal
        assert desc.precision == 18
        assert desc.scale == 4

    def test_decimal_without_precision(self):
        desc = PG.map_to_canonical("numeric")
        assert desc.canonical == CanonicalType.decimal
        assert desc.precision is None
        assert desc.scale is None

    def test_string_types(self):
        assert _canonical(PG, "text") == CanonicalType.string
        assert _canonical(PG, "varchar") == CanonicalType.string
        assert _canonical(PG, "character varying") == CanonicalType.string
        assert _canonical(PG, "varchar(255)") == CanonicalType.string

    def test_boolean(self):
        assert _canonical(PG, "boolean") == CanonicalType.boolean
        assert _canonical(PG, "bool") == CanonicalType.boolean

    def test_binary(self):
        assert _canonical(PG, "bytea") == CanonicalType.binary

    def test_date(self):
        assert _canonical(PG, "date") == CanonicalType.date

    def test_time(self):
        assert _canonical(PG, "time") == CanonicalType.time_ms
        assert _canonical(PG, "time without time zone") == CanonicalType.time_ms

    def test_timestamp_without_tz(self):
        assert _canonical(PG, "timestamp") == CanonicalType.timestamp_us
        assert _canonical(PG, "timestamp without time zone") == CanonicalType.timestamp_us

    def test_timestamp_with_tz(self):
        assert _canonical(PG, "timestamp with time zone") == CanonicalType.timestamp_tz_us
        assert _canonical(PG, "timestamptz") == CanonicalType.timestamp_tz_us

    def test_json_types(self):
        assert _canonical(PG, "json") == CanonicalType.json
        assert _canonical(PG, "jsonb") == CanonicalType.json

    def test_case_insensitive(self):
        assert _canonical(PG, "INTEGER") == CanonicalType.int32
        assert _canonical(PG, "BOOLEAN") == CanonicalType.boolean
        assert _canonical(PG, "TIMESTAMP WITH TIME ZONE") == CanonicalType.timestamp_tz_us

    def test_unknown_type_returns_unknown(self):
        desc = PG.map_to_canonical("pg_catalog.oid")
        assert desc.canonical == CanonicalType.unknown

    def test_unknown_carries_unsupported_warning(self):
        desc = PG.map_to_canonical("tstzrange")
        assert desc.has_warning(ConversionWarningKind.unsupported)

    def test_serial_carries_lossy_warning(self):
        desc = PG.map_to_canonical("serial")
        assert desc.canonical == CanonicalType.int32
        assert desc.has_warning(ConversionWarningKind.lossy)

    def test_char_carries_lossy_warning(self):
        desc = PG.map_to_canonical("char(10)")
        assert desc.has_warning(ConversionWarningKind.lossy)

    def test_uuid_carries_ambiguous_warning(self):
        desc = PG.map_to_canonical("uuid")
        assert desc.has_warning(ConversionWarningKind.ambiguous)

    def test_platform_hint_preserved(self):
        desc = PG.map_to_canonical("numeric(10,2)")
        assert desc.platform_hint == "numeric(10,2)"


# ── MySQL forward mappings ─────────────────────────────────────────────────────

class TestMySQLForwardMappings:
    def test_tinyint(self):
        assert _canonical(MY, "tinyint") == CanonicalType.int8

    def test_integer_variants(self):
        assert _canonical(MY, "smallint") == CanonicalType.int16
        assert _canonical(MY, "int") == CanonicalType.int32
        assert _canonical(MY, "bigint") == CanonicalType.int64

    def test_mediumint_maps_to_int32_with_warning(self):
        desc = MY.map_to_canonical("mediumint")
        assert desc.canonical == CanonicalType.int32
        assert desc.has_warning(ConversionWarningKind.lossy)

    def test_float_and_double(self):
        assert _canonical(MY, "float") == CanonicalType.float32
        assert _canonical(MY, "double") == CanonicalType.float64

    def test_decimal_with_precision_scale(self):
        desc = MY.map_to_canonical("decimal(12,3)")
        assert desc.canonical == CanonicalType.decimal
        assert desc.precision == 12
        assert desc.scale == 3

    def test_string_types(self):
        for t in ("varchar", "text", "tinytext", "mediumtext", "longtext"):
            assert _canonical(MY, t) == CanonicalType.string

    def test_varchar_with_length(self):
        assert _canonical(MY, "varchar(255)") == CanonicalType.string

    def test_blob_types(self):
        for t in ("blob", "tinyblob", "mediumblob", "longblob", "binary", "varbinary"):
            assert _canonical(MY, t) == CanonicalType.binary

    def test_boolean(self):
        assert _canonical(MY, "bool") == CanonicalType.boolean
        assert _canonical(MY, "boolean") == CanonicalType.boolean

    def test_date(self):
        assert _canonical(MY, "date") == CanonicalType.date

    def test_datetime_maps_to_timestamp_us(self):
        assert _canonical(MY, "datetime") == CanonicalType.timestamp_us

    def test_timestamp_maps_to_timestamp_tz_with_warning(self):
        desc = MY.map_to_canonical("timestamp")
        assert desc.canonical == CanonicalType.timestamp_tz_us
        assert desc.has_warning(ConversionWarningKind.lossy)

    def test_enum_maps_to_string_with_warning(self):
        desc = MY.map_to_canonical("enum")
        assert desc.canonical == CanonicalType.string
        assert desc.has_warning(ConversionWarningKind.lossy)

    def test_json(self):
        assert _canonical(MY, "json") == CanonicalType.json

    def test_unknown_type(self):
        desc = MY.map_to_canonical("spatial_type_xyz")
        assert desc.canonical == CanonicalType.unknown


# ── reverse mappings ───────────────────────────────────────────────────────────

class TestReverseMapping:
    def test_postgres_int32_reverse(self):
        assert _reverse(PG, CanonicalType.int32) == "INTEGER"

    def test_postgres_int64_reverse(self):
        assert _reverse(PG, CanonicalType.int64) == "BIGINT"

    def test_postgres_float64_reverse(self):
        assert _reverse(PG, CanonicalType.float64) == "DOUBLE PRECISION"

    def test_postgres_decimal_with_precision(self):
        result = _reverse(PG, CanonicalType.decimal, p=18, s=4)
        assert result == "NUMERIC(18,4)"

    def test_postgres_decimal_defaults(self):
        result = _reverse(PG, CanonicalType.decimal)
        assert result == "NUMERIC(38,10)"

    def test_postgres_string_reverse(self):
        assert _reverse(PG, CanonicalType.string) == "TEXT"

    def test_postgres_boolean_reverse(self):
        assert _reverse(PG, CanonicalType.boolean) == "BOOLEAN"

    def test_postgres_binary_reverse(self):
        assert _reverse(PG, CanonicalType.binary) == "BYTEA"

    def test_postgres_date_reverse(self):
        assert _reverse(PG, CanonicalType.date) == "DATE"

    def test_postgres_timestamp_us_reverse(self):
        assert _reverse(PG, CanonicalType.timestamp_us) == "TIMESTAMP"

    def test_postgres_timestamp_tz_reverse(self):
        assert _reverse(PG, CanonicalType.timestamp_tz_us) == "TIMESTAMPTZ"

    def test_postgres_json_reverse(self):
        assert _reverse(PG, CanonicalType.json) == "JSONB"

    def test_mysql_int32_reverse(self):
        assert _reverse(MY, CanonicalType.int32) == "INT"

    def test_mysql_decimal_with_precision(self):
        result = _reverse(MY, CanonicalType.decimal, p=10, s=2)
        assert result == "DECIMAL(10,2)"

    def test_mysql_boolean_reverse(self):
        assert _reverse(MY, CanonicalType.boolean) == "TINYINT(1)"


# ── round-trip stability ───────────────────────────────────────────────────────

class TestRoundTrip:
    """canonical → platform → canonical must be stable for common types."""

    @pytest.mark.parametrize("platform_type,expected_canonical", [
        ("integer",     CanonicalType.int32),
        ("bigint",      CanonicalType.int64),
        ("text",        CanonicalType.string),
        ("boolean",     CanonicalType.boolean),
        ("bytea",       CanonicalType.binary),
        ("date",        CanonicalType.date),
        ("timestamp",   CanonicalType.timestamp_us),
        ("timestamptz", CanonicalType.timestamp_tz_us),
        ("jsonb",       CanonicalType.json),
        ("real",        CanonicalType.float32),
        ("double precision", CanonicalType.float64),
    ])
    def test_postgres_roundtrip(self, platform_type, expected_canonical):
        desc = PG.map_to_canonical(platform_type)
        assert desc.canonical == expected_canonical, (
            f"Expected {expected_canonical!r} for {platform_type!r}, got {desc.canonical!r}"
        )
        reverse = PG.map_from_canonical(desc.canonical, desc.precision, desc.scale)
        round_trip = PG.map_to_canonical(reverse)
        assert round_trip.canonical == expected_canonical, (
            f"Round-trip broke for {platform_type!r}: "
            f"canonical={desc.canonical!r}, reverse={reverse!r}, round_trip={round_trip.canonical!r}"
        )

    @pytest.mark.parametrize("platform_type,expected_canonical", [
        ("int",      CanonicalType.int32),
        ("bigint",   CanonicalType.int64),
        ("float",    CanonicalType.float32),
        ("double",   CanonicalType.float64),
        ("varchar",  CanonicalType.string),
        ("text",     CanonicalType.string),
        ("date",     CanonicalType.date),
        ("datetime", CanonicalType.timestamp_us),
        ("json",     CanonicalType.json),
    ])
    def test_mysql_roundtrip(self, platform_type, expected_canonical):
        desc = MY.map_to_canonical(platform_type)
        assert desc.canonical == expected_canonical
        reverse = MY.map_from_canonical(desc.canonical, desc.precision, desc.scale)
        round_trip = MY.map_to_canonical(reverse)
        assert round_trip.canonical == expected_canonical


# ── precision extraction ───────────────────────────────────────────────────────

class TestPrecisionExtraction:
    def test_precision_only(self):
        desc = PG.map_to_canonical("decimal(10)")
        assert desc.precision == 10
        assert desc.scale is None

    def test_precision_and_scale(self):
        desc = PG.map_to_canonical("numeric(18, 4)")
        assert desc.precision == 18
        assert desc.scale == 4

    def test_no_parens(self):
        desc = PG.map_to_canonical("numeric")
        assert desc.precision is None
        assert desc.scale is None

    def test_mysql_decimal_precision_scale(self):
        desc = MY.map_to_canonical("decimal(8,2)")
        assert desc.precision == 8
        assert desc.scale == 2

    def test_varchar_length_not_extracted_as_precision(self):
        """varchar(255) should not be treated as a numeric precision."""
        desc = PG.map_to_canonical("varchar(255)")
        assert desc.canonical == CanonicalType.string
        assert desc.precision is None  # precision_from_string=False for string types


# ── descriptor completeness ────────────────────────────────────────────────────

class TestColumnTypeDescriptor:
    def test_exact_when_no_warnings(self):
        desc = PG.map_to_canonical("bigint")
        assert desc.is_exact()

    def test_not_exact_with_warning(self):
        desc = PG.map_to_canonical("serial")
        assert not desc.is_exact()

    def test_has_warning_kind(self):
        desc = PG.map_to_canonical("uuid")
        assert desc.has_warning(ConversionWarningKind.ambiguous)
        assert not desc.has_warning(ConversionWarningKind.lossy)
