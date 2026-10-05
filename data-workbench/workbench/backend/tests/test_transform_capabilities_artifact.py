"""Guards for the transform capability artifact + its authoring source.

The artifact (``platform/transform_capabilities.v1.json``) is generated from the
``data-transform-translation`` skill's per-platform YAML. These tests assert:

  - the committed artifact loads, is schema-valid, and matches its own checksum;
  - the checksum is IN SYNC with the YAML — i.e. re-running the build step from
    the reference corpus reproduces the committed artifact byte-for-content
    (a drift guard: edit the YAML, forget to rebuild → this fails);
  - a hand-edited/corrupt artifact fails closed on load;
  - every served platform is covered, and the AGE / SPLIT_PART matrix is right.

No Neo4j / SQLite / backend-app needed — pure artifact + stdlib + jsonschema.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import jsonschema
import pytest

from workbench.backend.platform.transform_capabilities import (
    CapabilityArtifactError,
    load_capabilities,
    _canonical_checksum,
)

REPO = Path(__file__).resolve().parents[3]
ARTIFACT = REPO / "workbench" / "backend" / "platform" / "transform_capabilities.v1.json"
SKILL = REPO / "workbench-skills" / "skills" / "data-transform-translation"
ARTIFACT_SCHEMA = SKILL / "schema" / "transform_capabilities.schema.json"

SERVED = {"postgres", "databricks", "snowflake", "bigquery", "mysql"}


def _load_build_module():
    """Import the skill's build_artifact.py by path (the backend never imports
    the skill, so we do it explicitly here to prove build/runtime agreement)."""
    path = SKILL / "scripts" / "build_artifact.py"
    spec = importlib.util.spec_from_file_location("_dtt_build_artifact", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_artifact_loads_and_checksum_verifies():
    caps = load_capabilities()
    assert caps.schema_version == "v1"
    assert caps.checksum.startswith("sha256:")
    assert set(caps.platforms) == SERVED


def test_artifact_is_schema_valid():
    raw = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    schema = json.loads(ARTIFACT_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.validate(raw, schema)  # raises on any violation


def test_committed_artifact_is_in_sync_with_yaml():
    """Rebuild from the reference YAML and assert the checksum matches the
    committed artifact — the corpus and the runtime artifact never drift."""
    build = _load_build_module()
    rebuilt = build.build()
    committed = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert rebuilt["checksum"] == committed["checksum"], (
        "The reference YAML changed but transform_capabilities.v1.json was not "
        "rebuilt. Run data-transform-translation/scripts/build_artifact.py."
    )
    # And the build step's checksum helper agrees with the backend loader's copy.
    assert build.canonical_checksum(rebuilt) == _canonical_checksum(rebuilt)


def test_corrupt_artifact_fails_closed(tmp_path):
    raw = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    tampered = copy.deepcopy(raw)
    # Flip a capability without recomputing the checksum — must be rejected.
    tampered["functions"]["AGE"]["platforms"]["databricks"]["capability"] = "native"
    p = tmp_path / "transform_capabilities.v1.json"
    p.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(CapabilityArtifactError):
        load_capabilities(p)


def test_missing_artifact_fails_closed(tmp_path):
    with pytest.raises(CapabilityArtifactError):
        load_capabilities(tmp_path / "does_not_exist.v1.json")


def test_age_and_split_part_matrix():
    caps = load_capabilities()
    # AGE: native on postgres, unsupported everywhere else.
    assert caps.function_support("AGE", "postgres") == "native"
    for p in SERVED - {"postgres"}:
        assert caps.function_support("AGE", p) == "unsupported", p
    # SPLIT_PART: native on pg/dbx/sf, unsupported on bq/mysql.
    for p in ("postgres", "databricks", "snowflake"):
        assert caps.function_support("SPLIT_PART", p) == "native", p
    for p in ("bigquery", "mysql"):
        assert caps.function_support("SPLIT_PART", p) == "unsupported", p


def test_unsupported_entries_carry_remediation():
    """Every unsupported claim must tell the author what to do instead."""
    caps = load_capabilities()
    for fname, node in caps.functions.items():
        for platform, entry in node["platforms"].items():
            if entry["capability"] == "unsupported":
                assert entry.get("remediation"), f"{fname}@{platform} unsupported w/o remediation"


def test_date_difference_semantics_present_for_every_platform():
    """The neutral date_difference op must declare all three semantics on every
    served platform (the whole point: no silent semantic degradation)."""
    caps = load_capabilities()
    for variant in ("completed_units", "boundary_count", "symbolic_interval"):
        for p in SERVED:
            assert caps.op_support("date_difference", variant, p) is not None, (variant, p)


# ── sync guard: served-platform set must not drift across binding points ──────

def test_served_platform_set_consistent_across_bindings():
    """The artifact, the view-DDL validation set, and the sqlglot binding must
    agree on the served native-view platforms — a drift guard so adding a
    platform to one place fails until it's added everywhere."""
    caps = load_capabilities()
    assert set(caps.platforms) == SERVED

    # generate_view_ddl's _SERVED_VALIDATION_PLATFORMS must equal the artifact's.
    spec = importlib.util.spec_from_file_location(
        "generate_view_ddl",
        REPO / "workbench-skills" / "skills" / "data-serving-virtual-view" / "scripts" / "generate_view_ddl.py",
    )
    gv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gv)
    assert set(gv._SERVED_VALIDATION_PLATFORMS) == set(caps.platforms)

    # Every served platform must have a sqlglot dialect binding (render engine).
    from workbench.backend.dialect_sql import PLATFORM_TO_SQLGLOT
    for p in caps.platforms:
        assert p in PLATFORM_TO_SQLGLOT, f"served platform {p} has no sqlglot binding"


def test_every_artifact_op_covers_every_served_platform():
    """No op/semantics may lack a served-platform entry — the 'renders or
    explicitly flagged' guarantee has no gaps."""
    caps = load_capabilities()
    for op, node in caps.ops.items():
        for variant, platmap in node["semantics"].items():
            missing = SERVED - set(platmap)
            assert not missing, f"op {op}/{variant} missing platforms: {sorted(missing)}"
