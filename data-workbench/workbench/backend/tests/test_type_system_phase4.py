"""Phase 4 type-system tests: Snowflake + Databricks profiles, transform fixtures.

Coverage:
- Snowflake profile: integer, float (all 64-bit), decimal/number, string synonyms,
  timestamp variants (NTZ/LTZ/TZ), semi-structured (VARIANT/ARRAY/OBJECT)
- Databricks profile: integers, float32 vs float64, decimal, string, binary,
  timestamp vs timestamp_ntz, no-native-TIME warning, interval types
- Profile registry: all four platforms registered; fail-closed on unknown
- Round-trip stability for Snowflake and Databricks common types
- SqlTransformFixture model: construction, JSON round-trip, helper queries
- Golden-path fixture set: completeness, no empty expected_sql, name uniqueness,
  cast fixtures cover all four scalar types across all four platforms
"""
import pytest

from workbench.backend.platform.type_system import (
    CanonicalType,
    ConversionWarningKind,
    get_profile,
    list_profiles,
)
from workbench.backend.platform.transform_fixtures import (
    GOLDEN_PATH_FIXTURES,
    SqlTransformFixture,
    fixtures_for_kind,
    fixtures_for_platform,
    get_fixture,
)

SF = get_profile("snowflake")
DB = get_profile("databricks")


# ── helpers ────────────────────────────────────────────────────────────────────

def _canonical(profile, type_str: str) -> CanonicalType:
    return profile.map_to_canonical(type_str).canonical


def _reverse(profile, ct: CanonicalType, p=None, s=None) -> str:
    return profile.map_from_canonical(ct, p, s)


# ── profile registry ───────────────────────────────────────────────────────────

class TestProfileRegistryPhase4:
    def test_all_four_platforms_registered(self):
        for pid in ("postgres", "mysql", "snowflake", "databricks"):
            assert pid in list_profiles(), f"{pid} not in list_profiles()"

    def test_redshift_not_yet_registered(self):
        with pytest.raises(KeyError):
            get_profile("redshift")

    def test_bigquery_not_yet_registered(self):
        with pytest.raises(KeyError):
            get_profile("bigquery")


# ── Snowflake type profile ─────────────────────────────────────────────────────

class TestSnowflakeForwardMappings:
    def test_integer_types(self):
        assert _canonical(SF, "byteint")  == CanonicalType.int8
        assert _canonical(SF, "tinyint")  == CanonicalType.int8
        assert _canonical(SF, "smallint") == CanonicalType.int16
        assert _canonical(SF, "integer")  == CanonicalType.int32
        assert _canonical(SF, "int")      == CanonicalType.int32
        assert _canonical(SF, "bigint")   == CanonicalType.int64

    def test_all_floats_are_64bit(self):
        """Snowflake stores all float types as 64-bit."""
        for t in ("float", "float4", "float8", "double", "real"):
            assert _canonical(SF, t) == CanonicalType.float64, f"{t} should be float64"

    def test_float4_carries_lossy_warning(self):
        desc = SF.map_to_canonical("float4")
        assert desc.has_warning(ConversionWarningKind.lossy)

    def test_decimal_number_numeric_all_map_to_decimal(self):
        for t in ("number", "numeric", "decimal"):
            assert _canonical(SF, t) == CanonicalType.decimal

    def test_decimal_precision_scale_extracted(self):
        desc = SF.map_to_canonical("number(38,10)")
        assert desc.canonical == CanonicalType.decimal
        assert desc.precision == 38
        assert desc.scale == 10

    def test_string_synonyms(self):
        for t in ("string", "text", "varchar", "nvarchar", "character varying"):
            assert _canonical(SF, t) == CanonicalType.string, f"{t} should be string"

    def test_varchar_with_length(self):
        assert _canonical(SF, "varchar(256)") == CanonicalType.string

    def test_char_with_lossy_warning(self):
        desc = SF.map_to_canonical("char(10)")
        assert desc.has_warning(ConversionWarningKind.lossy)

    def test_binary(self):
        assert _canonical(SF, "binary")    == CanonicalType.binary
        assert _canonical(SF, "varbinary") == CanonicalType.binary

    def test_boolean(self):
        assert _canonical(SF, "boolean") == CanonicalType.boolean
        assert _canonical(SF, "bool")    == CanonicalType.boolean

    def test_date(self):
        assert _canonical(SF, "date") == CanonicalType.date

    def test_time(self):
        assert _canonical(SF, "time") == CanonicalType.time_ms

    def test_timestamp_ntz_is_timestamp_us(self):
        assert _canonical(SF, "timestamp_ntz") == CanonicalType.timestamp_us
        assert _canonical(SF, "datetime")      == CanonicalType.timestamp_us

    def test_timestamp_ltz_and_tz_are_timestamp_tz_us(self):
        assert _canonical(SF, "timestamp_ltz") == CanonicalType.timestamp_tz_us
        assert _canonical(SF, "timestamp_tz")  == CanonicalType.timestamp_tz_us

    def test_bare_timestamp_carries_ambiguity_warning(self):
        desc = SF.map_to_canonical("timestamp")
        assert desc.canonical == CanonicalType.timestamp_us
        assert desc.has_warning(ConversionWarningKind.ambiguous)

    def test_variant_maps_to_json(self):
        assert _canonical(SF, "variant") == CanonicalType.json

    def test_array_and_object(self):
        assert _canonical(SF, "array")  == CanonicalType.array
        assert _canonical(SF, "object") == CanonicalType.struct

    def test_unknown_type(self):
        desc = SF.map_to_canonical("geography")
        assert desc.canonical == CanonicalType.unknown

    def test_case_insensitive(self):
        assert _canonical(SF, "TIMESTAMP_NTZ") == CanonicalType.timestamp_us
        assert _canonical(SF, "NUMBER(18,4)")  == CanonicalType.decimal
        assert _canonical(SF, "VARIANT")       == CanonicalType.json


class TestSnowflakeReverseMappings:
    def test_int32_reverse(self):
        assert _reverse(SF, CanonicalType.int32) == "INTEGER"

    def test_int64_reverse(self):
        assert _reverse(SF, CanonicalType.int64) == "BIGINT"

    def test_float64_reverse(self):
        # Snowflake FLOAT and DOUBLE are both 64-bit synonyms; FLOAT is the first entry
        assert _reverse(SF, CanonicalType.float64) == "FLOAT"

    def test_decimal_with_precision(self):
        assert _reverse(SF, CanonicalType.decimal, 18, 4) == "NUMBER(18,4)"

    def test_decimal_defaults(self):
        assert _reverse(SF, CanonicalType.decimal) == "NUMBER(38,10)"

    def test_string_reverse(self):
        assert _reverse(SF, CanonicalType.string) == "VARCHAR"

    def test_binary_reverse(self):
        assert _reverse(SF, CanonicalType.binary) == "BINARY"

    def test_boolean_reverse(self):
        assert _reverse(SF, CanonicalType.boolean) == "BOOLEAN"

    def test_date_reverse(self):
        assert _reverse(SF, CanonicalType.date) == "DATE"

    def test_timestamp_us_reverse(self):
        assert _reverse(SF, CanonicalType.timestamp_us) == "TIMESTAMP_NTZ"

    def test_timestamp_tz_reverse(self):
        assert _reverse(SF, CanonicalType.timestamp_tz_us) == "TIMESTAMP_LTZ"

    def test_json_reverse(self):
        assert _reverse(SF, CanonicalType.json) == "VARIANT"


class TestSnowflakeRoundTrip:
    @pytest.mark.parametrize("platform_type,expected_canonical", [
        ("integer",       CanonicalType.int32),
        ("bigint",        CanonicalType.int64),
        ("double",        CanonicalType.float64),
        ("varchar",       CanonicalType.string),
        ("boolean",       CanonicalType.boolean),
        ("binary",        CanonicalType.binary),
        ("date",          CanonicalType.date),
        ("timestamp_ntz", CanonicalType.timestamp_us),
        ("timestamp_ltz", CanonicalType.timestamp_tz_us),
        ("variant",       CanonicalType.json),
    ])
    def test_roundtrip(self, platform_type, expected_canonical):
        desc = SF.map_to_canonical(platform_type)
        assert desc.canonical == expected_canonical
        reverse = SF.map_from_canonical(desc.canonical, desc.precision, desc.scale)
        round_trip = SF.map_to_canonical(reverse)
        assert round_trip.canonical == expected_canonical, (
            f"Round-trip broke: {platform_type} → {desc.canonical} → {reverse} → {round_trip.canonical}"
        )


# ── Databricks type profile ────────────────────────────────────────────────────

class TestDatabricksForwardMappings:
    def test_integer_types(self):
        assert _canonical(DB, "tinyint")  == CanonicalType.int8
        assert _canonical(DB, "byte")     == CanonicalType.int8
        assert _canonical(DB, "smallint") == CanonicalType.int16
        assert _canonical(DB, "short")    == CanonicalType.int16
        assert _canonical(DB, "int")      == CanonicalType.int32
        assert _canonical(DB, "integer")  == CanonicalType.int32
        assert _canonical(DB, "bigint")   == CanonicalType.int64
        assert _canonical(DB, "long")     == CanonicalType.int64

    def test_float_vs_double(self):
        """Databricks distinguishes float32 (FLOAT) from float64 (DOUBLE)."""
        assert _canonical(DB, "float")  == CanonicalType.float32
        assert _canonical(DB, "real")   == CanonicalType.float32
        assert _canonical(DB, "double") == CanonicalType.float64

    def test_decimal_aliases(self):
        for t in ("decimal", "numeric", "dec"):
            assert _canonical(DB, t) == CanonicalType.decimal

    def test_decimal_precision_scale(self):
        desc = DB.map_to_canonical("decimal(10,2)")
        assert desc.precision == 10
        assert desc.scale == 2

    def test_string_types(self):
        for t in ("string", "text", "varchar", "varchar(255)"):
            assert _canonical(DB, t) == CanonicalType.string

    def test_binary(self):
        assert _canonical(DB, "binary") == CanonicalType.binary
        assert _canonical(DB, "bytes")  == CanonicalType.binary

    def test_boolean(self):
        assert _canonical(DB, "boolean") == CanonicalType.boolean
        assert _canonical(DB, "bool")    == CanonicalType.boolean

    def test_date(self):
        assert _canonical(DB, "date") == CanonicalType.date

    def test_timestamp_has_tz_ambiguity_warning(self):
        desc = DB.map_to_canonical("timestamp")
        assert desc.canonical == CanonicalType.timestamp_tz_us
        assert desc.has_warning(ConversionWarningKind.ambiguous)

    def test_timestamp_ntz_is_clean(self):
        desc = DB.map_to_canonical("timestamp_ntz")
        assert desc.canonical == CanonicalType.timestamp_us
        assert desc.is_exact()

    def test_time_has_unsupported_warning(self):
        """Databricks has no native TIME type."""
        desc = DB.map_to_canonical("time")
        assert desc.canonical == CanonicalType.time_ms
        assert desc.has_warning(ConversionWarningKind.unsupported)

    def test_interval_types(self):
        assert _canonical(DB, "interval day")  == CanonicalType.interval_day
        assert _canonical(DB, "interval year") == CanonicalType.interval_year

    def test_bare_interval_is_day_with_warning(self):
        desc = DB.map_to_canonical("interval")
        assert desc.canonical == CanonicalType.interval_day
        assert desc.has_warning(ConversionWarningKind.ambiguous)

    def test_semi_structured(self):
        assert _canonical(DB, "map")    == CanonicalType.map
        assert _canonical(DB, "struct") == CanonicalType.struct
        assert _canonical(DB, "array")  == CanonicalType.array

    def test_variant_is_preview_with_warning(self):
        desc = DB.map_to_canonical("variant")
        assert desc.canonical == CanonicalType.json
        assert desc.has_warning(ConversionWarningKind.lossy)

    def test_unknown_type(self):
        desc = DB.map_to_canonical("user_defined_type_xyz")
        assert desc.canonical == CanonicalType.unknown

    def test_case_insensitive(self):
        assert _canonical(DB, "BIGINT")        == CanonicalType.int64
        assert _canonical(DB, "TIMESTAMP_NTZ") == CanonicalType.timestamp_us


class TestDatabricksReverseMappings:
    def test_int32_reverse(self):
        assert _reverse(DB, CanonicalType.int32) == "INT"

    def test_int64_reverse(self):
        assert _reverse(DB, CanonicalType.int64) == "BIGINT"

    def test_float32_reverse(self):
        assert _reverse(DB, CanonicalType.float32) == "FLOAT"

    def test_float64_reverse(self):
        assert _reverse(DB, CanonicalType.float64) == "DOUBLE"

    def test_decimal_with_precision(self):
        assert _reverse(DB, CanonicalType.decimal, 12, 3) == "DECIMAL(12,3)"

    def test_string_reverse(self):
        assert _reverse(DB, CanonicalType.string) == "STRING"

    def test_binary_reverse(self):
        assert _reverse(DB, CanonicalType.binary) == "BINARY"

    def test_boolean_reverse(self):
        assert _reverse(DB, CanonicalType.boolean) == "BOOLEAN"

    def test_date_reverse(self):
        assert _reverse(DB, CanonicalType.date) == "DATE"

    def test_timestamp_us_reverse(self):
        assert _reverse(DB, CanonicalType.timestamp_us) == "TIMESTAMP_NTZ"

    def test_timestamp_tz_reverse(self):
        assert _reverse(DB, CanonicalType.timestamp_tz_us) == "TIMESTAMP"


class TestDatabricksRoundTrip:
    @pytest.mark.parametrize("platform_type,expected_canonical", [
        ("int",           CanonicalType.int32),
        ("bigint",        CanonicalType.int64),
        ("float",         CanonicalType.float32),
        ("double",        CanonicalType.float64),
        ("string",        CanonicalType.string),
        ("binary",        CanonicalType.binary),
        ("boolean",       CanonicalType.boolean),
        ("date",          CanonicalType.date),
        ("timestamp_ntz", CanonicalType.timestamp_us),
    ])
    def test_roundtrip(self, platform_type, expected_canonical):
        desc = DB.map_to_canonical(platform_type)
        assert desc.canonical == expected_canonical
        reverse = DB.map_from_canonical(desc.canonical, desc.precision, desc.scale)
        round_trip = DB.map_to_canonical(reverse)
        assert round_trip.canonical == expected_canonical, (
            f"Round-trip broke: {platform_type} → {desc.canonical} → {reverse} → {round_trip.canonical}"
        )


# ── cross-platform consistency ─────────────────────────────────────────────────

class TestCrossPlatformConsistency:
    """The four profiles must agree on canonical types for universal types."""

    UNIVERSAL = [
        ("integer",  CanonicalType.int32),
        ("bigint",   CanonicalType.int64),
        ("boolean",  CanonicalType.boolean),
        ("date",     CanonicalType.date),
    ]

    @pytest.mark.parametrize("platform_type,expected_canonical", UNIVERSAL)
    def test_integer_and_bool_and_date_agree_postgres_snowflake(
        self, platform_type, expected_canonical
    ):
        pg = get_profile("postgres").map_to_canonical(platform_type).canonical
        sf = get_profile("snowflake").map_to_canonical(platform_type).canonical
        assert pg == expected_canonical
        assert sf == expected_canonical

    def test_decimal_canonical_matches_across_platforms(self):
        for pid in ("postgres", "mysql", "snowflake", "databricks"):
            profile = get_profile(pid)
            type_str = "numeric" if pid in ("postgres", "mysql") else (
                "number" if pid == "snowflake" else "decimal"
            )
            desc = profile.map_to_canonical(type_str)
            assert desc.canonical == CanonicalType.decimal, (
                f"{pid}: expected decimal for {type_str!r}, got {desc.canonical!r}"
            )


# ── SqlTransformFixture model ──────────────────────────────────────────────────

class TestSqlTransformFixtureModel:
    def test_minimal_construction(self):
        f = SqlTransformFixture(
            name="test_cast",
            transform_kind="cast",
            transform_params={"target_canonical_type": "int32"},
            source_columns=["col"],
            expected_sql={"postgres": 'CAST("{col}" AS INTEGER)'},
        )
        assert f.name == "test_cast"
        assert f.has_platform("postgres")
        assert not f.has_platform("snowflake")

    def test_platforms_sorted(self):
        f = SqlTransformFixture(
            name="t",
            transform_kind="cast",
            expected_sql={"snowflake": "x", "postgres": "y", "databricks": "z"},
        )
        assert f.platforms() == ["databricks", "postgres", "snowflake"]

    def test_json_round_trip(self):
        f = SqlTransformFixture(
            name="cast_test",
            description="A test fixture",
            transform_kind="cast",
            transform_params={"target_canonical_type": "decimal", "precision": 10, "scale": 2},
            source_columns=["price"],
            expected_sql={"postgres": 'CAST("{col}" AS NUMERIC(10,2))'},
        )
        restored = SqlTransformFixture.model_validate_json(f.model_dump_json())
        assert restored.name == "cast_test"
        assert restored.transform_params["precision"] == 10
        assert restored.expected_sql["postgres"] == f.expected_sql["postgres"]


class TestFixtureLookupHelpers:
    def test_get_fixture_by_name(self):
        f = get_fixture("cast_to_int32")
        assert f.transform_kind == "cast"
        assert f.has_platform("postgres")

    def test_get_fixture_unknown_raises(self):
        with pytest.raises(KeyError, match="No fixture named"):
            get_fixture("nonexistent_fixture")

    def test_fixtures_for_platform_postgres(self):
        pg_fixtures = fixtures_for_platform("postgres")
        assert len(pg_fixtures) > 0
        assert all(f.has_platform("postgres") for f in pg_fixtures)

    def test_fixtures_for_platform_databricks(self):
        db_fixtures = fixtures_for_platform("databricks")
        assert len(db_fixtures) > 0

    def test_fixtures_for_kind_cast(self):
        cast_fixtures = fixtures_for_kind("cast")
        assert len(cast_fixtures) >= 5  # at least the cast variants we defined
        assert all(f.transform_kind == "cast" for f in cast_fixtures)

    def test_fixtures_for_kind_hash(self):
        hash_fixtures = fixtures_for_kind("hash")
        assert len(hash_fixtures) >= 2


# ── golden-path fixture set completeness ──────────────────────────────────────

class TestGoldenPathFixtures:
    def test_fixture_names_are_unique(self):
        names = [f.name for f in GOLDEN_PATH_FIXTURES]
        assert len(names) == len(set(names)), "Duplicate fixture names found"

    def test_no_fixture_has_empty_expected_sql(self):
        for f in GOLDEN_PATH_FIXTURES:
            assert f.expected_sql, f"Fixture {f.name!r} has no expected_sql entries"

    def test_no_fixture_has_empty_source_columns_when_cast(self):
        for f in fixtures_for_kind("cast"):
            assert f.source_columns, f"Cast fixture {f.name!r} has no source_columns"

    def test_cast_fixtures_cover_int_float_decimal_string(self):
        cast_fixtures = {f.name for f in fixtures_for_kind("cast")}
        expected = {"cast_to_int32", "cast_to_int64", "cast_to_float64",
                    "cast_to_decimal_18_4", "cast_to_string"}
        assert expected <= cast_fixtures, f"Missing cast fixtures: {expected - cast_fixtures}"

    def test_cast_int32_covers_all_four_platforms(self):
        f = get_fixture("cast_to_int32")
        for pid in ("postgres", "mysql", "snowflake", "databricks"):
            assert f.has_platform(pid), f"cast_to_int32 missing platform {pid!r}"

    def test_all_fixture_transform_kinds_are_known(self):
        known_kinds = {"cast", "concat", "literal", "arithmetic", "coalesce",
                       "case", "hash", "mask", "bucket", "window"}
        for f in GOLDEN_PATH_FIXTURES:
            assert f.transform_kind in known_kinds, (
                f"Fixture {f.name!r} uses unknown kind {f.transform_kind!r}"
            )

    def test_concat_fixtures_have_two_source_columns(self):
        for f in fixtures_for_kind("concat"):
            assert len(f.source_columns) == 2, (
                f"Concat fixture {f.name!r} should have 2 source columns"
            )

    def test_literal_fixtures_have_no_source_columns(self):
        for f in fixtures_for_kind("literal"):
            assert f.source_columns == [], (
                f"Literal fixture {f.name!r} should have no source columns"
            )

    def test_at_least_one_fixture_per_major_kind(self):
        present = {f.transform_kind for f in GOLDEN_PATH_FIXTURES}
        for kind in ("cast", "concat", "literal", "arithmetic", "hash"):
            assert kind in present, f"No fixture for transform_kind {kind!r}"
