"""Dataset-shape stage encoding (dedupe / grouping / SCD / windows / suppression).

Locks the contract of `dataset_transform._encode_shape_field`: it validates each
stage's shape, clears on null/empty, normalises where sensible, and rejects a bad
SCD type / non-list grouping — so a malformed stage never reaches the graph. Pure
Python, no Neo4j (mirrors test_filter_intent.py).
"""

import json

from workbench.backend.routers import dataset_transform as dt


def test_grouping_keys_filters_blanks():
    out, err = dt._encode_shape_field("grouping_keys", ["a", "b", "", "  "])
    assert err is None
    assert json.loads(out) == ["a", "b"]


def test_grouping_keys_must_be_list():
    out, err = dt._encode_shape_field("grouping_keys", "not-a-list")
    assert out is None and "list" in err


def test_suppressed_columns_encode():
    out, err = dt._encode_shape_field("suppressed_columns", ["ssn"])
    assert err is None and json.loads(out) == ["ssn"]


def test_dedupe_normalises_direction_and_requires_keys():
    out, err = dt._encode_shape_field("dedupe", {"keys": ["id"], "order_by": "ts", "direction": "DESC"})
    assert err is None
    assert json.loads(out) == {"keys": ["id"], "order_by": "ts", "direction": "desc"}


def test_dedupe_without_keys_clears():
    out, err = dt._encode_shape_field("dedupe", {"keys": []})
    assert err is None and out == ""


def test_dedupe_null_clears():
    out, err = dt._encode_shape_field("dedupe", None)
    assert err is None and out == ""


def test_scd_valid_types_pass():
    for t in ("latest_only", "scd2", "snapshot"):
        out, err = dt._encode_shape_field("scd_policy", {"type": t})
        assert err is None and json.loads(out)["type"] == t


def test_scd_bad_type_rejected():
    out, err = dt._encode_shape_field("scd_policy", {"type": "bogus"})
    assert out is None and "latest_only" in err


def test_scd_empty_clears():
    out, err = dt._encode_shape_field("scd_policy", {"type": ""})
    assert err is None and out == ""


def test_windows_passthrough_object():
    spec = {"w1": {"partition_by": ["id"], "order_by": [{"column": "ts", "direction": "asc"}]}}
    out, err = dt._encode_shape_field("windows", spec)
    assert err is None and json.loads(out) == spec


def test_windows_must_be_object():
    out, err = dt._encode_shape_field("windows", ["not", "an", "object"])
    assert out is None and "object" in err


def test_field_prop_map_is_allowlisted():
    # Every mapped property is a *Json / known DatasetTransform field — never user input.
    assert set(dt._SHAPE_FIELD_TO_PROP.values()) == {
        "dedupeJson", "groupingKeysJson", "scdPolicyJson",
        "suppressedColumnsJson", "windowSpecsJson",
    }
    # The generic upsert only ever interpolates an allow-listed prop.
    for prop in dt._SHAPE_FIELD_TO_PROP.values():
        q = dt._upsert_dt_field_query(prop)
        assert f"SET dt.{prop} = $value" in q
