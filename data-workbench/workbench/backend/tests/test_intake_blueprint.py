"""Intake scaffold-blueprint schema — the strict parser↔backend contract.

Pure/offline: no DB, Neo4j, or MCP needed. Guards the properties the hardened
plan depends on — discriminated union, required-field enforcement (so a bad
parse becomes parse_failed rather than a half-scaffold), and deterministic id
backfill that preserves parser-supplied ids.
"""
import pytest

from workbench.backend import intake_blueprint as bp
from workbench.backend.intake_blueprint import (
    BlueprintValidationError,
    Confidence,
    MigrationBlueprint,
    ModernizationBlueprint,
    ReviewState,
)


def _cf(value, confidence="high"):
    return {"value": value, "confidence": confidence}


def _valid_migration():
    return {
        "scenario": "migration",
        "project_name": _cf("Orders Lift-and-Shift"),
        "source_platform": _cf("Oracle"),
        "target_platform": _cf("Databricks", "medium"),
        "datasets": [
            {"name": _cf("SALES.ORDERS"), "columns": [{"name": _cf("ORDER_ID")}]},
        ],
    }


def _valid_modernization():
    return {
        "scenario": "modernization",
        "source_aligned": [{"name": _cf("Customer Master")}],
        "consumer_aligned": [{"name": _cf("Churn Features"), "purpose": "ML"}],
        "dependencies": [{"from_candidate_id": "con-0", "to_candidate_id": "src-0"}],
    }


def test_migration_discriminates():
    parsed = bp.parse_blueprint(_valid_migration())
    assert isinstance(parsed, MigrationBlueprint)
    assert parsed.scenario == "migration"
    assert parsed.blueprint_version == bp.BLUEPRINT_VERSION
    assert parsed.project_name.value == "Orders Lift-and-Shift"
    assert parsed.target_platform.confidence == Confidence.medium
    # untouched graded field defaults to missing, not a fabricated value
    assert parsed.write_disposition.value is None
    assert parsed.write_disposition.confidence == Confidence.missing


def test_column_note_null_coerced_to_empty():
    # The parser reasonably emits note: null for "no note"; schema must tolerate
    # it (coerce to "") rather than reject the whole blueprint.
    data = _valid_migration()
    data["datasets"][0]["columns"][0]["note"] = None
    parsed = bp.parse_blueprint(data)
    assert parsed.datasets[0].columns[0].note == ""


def test_modernization_discriminates():
    parsed = bp.parse_blueprint(_valid_modernization())
    assert isinstance(parsed, ModernizationBlueprint)
    assert len(parsed.source_aligned) == 1
    assert parsed.consumer_aligned[0].purpose == "ML"
    assert parsed.dependencies[0].from_candidate_id == "con-0"


def test_missing_scenario_is_parse_failure():
    data = _valid_migration()
    del data["scenario"]
    with pytest.raises(BlueprintValidationError):
        bp.parse_blueprint(data)


def test_unknown_scenario_is_parse_failure():
    data = _valid_migration()
    data["scenario"] = "something-else"
    with pytest.raises(BlueprintValidationError):
        bp.parse_blueprint(data)


def test_migration_requires_project_name():
    data = _valid_migration()
    del data["project_name"]
    with pytest.raises(BlueprintValidationError):
        bp.parse_blueprint(data)


def test_non_dict_is_parse_failure():
    with pytest.raises(BlueprintValidationError):
        bp.parse_blueprint("not-a-dict")  # type: ignore[arg-type]


def test_normalize_ids_fills_and_preserves():
    parsed = bp.parse_blueprint(_valid_migration())
    # parser omitted ids here
    assert parsed.datasets[0].candidate_id is None
    bp.normalize_ids(parsed)
    ds_id = parsed.datasets[0].candidate_id
    assert ds_id and ds_id.startswith("ds-0-")
    assert parsed.datasets[0].columns[0].candidate_id.startswith(ds_id + ".col-0-")

    # a parser-supplied id is preserved (so its own dependency refs stay valid)
    data = _valid_modernization()
    data["source_aligned"][0]["candidate_id"] = "src-0"
    m = bp.parse_blueprint(data)
    bp.normalize_ids(m)
    assert m.source_aligned[0].candidate_id == "src-0"
    assert m.dependencies[0].dependency_id == "dep-0"


def test_review_state_defaults_suggested():
    parsed = bp.parse_blueprint(_valid_migration())
    assert parsed.datasets[0].review_state == ReviewState.suggested
    assert parsed.project_name.review_state == ReviewState.suggested
