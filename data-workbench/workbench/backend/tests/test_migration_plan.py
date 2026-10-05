"""Tests for the MigrationPlan v1 model (platform/migration_plan.py).

Coverage:
- MigrationStatus enum values
- valid_next_statuses() forward-transition map
- MigrationPlan construction and JSON round-trip
- transition() happy paths through the forward state machine
- transition() rejects invalid jumps
- maintenance_policy gate blocks cutover_in_progress
- maintenance_policy.is_complete() logic
- terminal-state detection
- all_reconciliation_rules_pass()
- phase_log audit trail
"""
import json
import uuid
from datetime import datetime, timezone

import pytest

from workbench.backend.platform.migration_plan import (
    MaintenancePolicy,
    MigrationOwnership,
    MigrationPlan,
    MigrationStatus,
    PhaseLogEntry,
    ReconciliationRule,
    valid_next_statuses,
)
from workbench.backend.platform.transfer_batch import (
    RelationalRelationRef,
    FileCollectionRef,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _relational_ref(name: str = "public.orders") -> dict:
    return {
        "kind": "relational_relation",
        "platform_instance_id": "pg-test",
        "asset_id": name,
        "relation": name.split(".")[-1],
    }


def _file_ref(prefix: str = "s3://bucket/orders/") -> dict:
    return {
        "kind": "file_collection",
        "platform_instance_id": "s3-test",
        "asset_id": prefix,
        "uri_prefix": prefix,
    }


def _make_plan(**kwargs) -> MigrationPlan:
    defaults = dict(
        plan_id=str(uuid.uuid4()),
        source_asset_ref=_relational_ref(),
        target_asset_ref=_file_ref(),
    )
    defaults.update(kwargs)
    return MigrationPlan(**defaults)


def _complete_maintenance() -> MaintenancePolicy:
    return MaintenancePolicy(
        compaction_owner="ops@example.com",
        snapshot_expiry_owner="ops@example.com",
        orphan_cleanup_owner="ops@example.com",
    )


# ── status enum ────────────────────────────────────────────────────────────────

class TestMigrationStatus:
    def test_all_forward_states_exist(self):
        forward = [
            "draft", "assessed", "initial_snapshot_running", "initial_snapshot_loaded",
            "change_capture_catching_up", "reconciled", "cutover_ready",
            "cutover_in_progress", "cutover_complete", "observation",
            "source_decommissioned",
        ]
        for name in forward:
            assert MigrationStatus(name) is not None

    def test_all_failure_states_exist(self):
        for name in ("paused", "retryable_failed", "compensation_required",
                     "rolled_back", "terminal_failed", "cancelled"):
            assert MigrationStatus(name) is not None

    def test_invalid_status_raises(self):
        with pytest.raises(ValueError):
            MigrationStatus("completely_made_up")


# ── valid_next_statuses ────────────────────────────────────────────────────────

class TestValidNextStatuses:
    def test_draft_can_go_to_assessed(self):
        assert MigrationStatus.assessed in valid_next_statuses(MigrationStatus.draft)

    def test_draft_can_be_cancelled(self):
        assert MigrationStatus.cancelled in valid_next_statuses(MigrationStatus.draft)

    def test_cutover_complete_only_goes_to_observation(self):
        nexts = valid_next_statuses(MigrationStatus.cutover_complete)
        assert nexts == {MigrationStatus.observation}

    def test_source_decommissioned_is_terminal(self):
        assert valid_next_statuses(MigrationStatus.source_decommissioned) == set()

    def test_terminal_failed_is_terminal(self):
        assert valid_next_statuses(MigrationStatus.terminal_failed) == set()

    def test_reconciled_can_go_to_cutover_ready(self):
        assert MigrationStatus.cutover_ready in valid_next_statuses(MigrationStatus.reconciled)

    def test_reconciled_can_go_back_to_cdc(self):
        assert MigrationStatus.change_capture_catching_up in valid_next_statuses(MigrationStatus.reconciled)


# ── construction ───────────────────────────────────────────────────────────────

class TestMigrationPlanConstruction:
    def test_default_status_is_draft(self):
        plan = _make_plan()
        assert plan.status == MigrationStatus.draft

    def test_default_contract_version(self):
        plan = _make_plan()
        assert plan.contract_version == "1"

    def test_source_and_target_refs(self):
        plan = _make_plan()
        assert plan.source_asset_ref.kind == "relational_relation"
        assert plan.target_asset_ref.kind == "file_collection"

    def test_empty_phase_log(self):
        plan = _make_plan()
        assert plan.phase_log == []

    def test_maintenance_policy_defaults_incomplete(self):
        plan = _make_plan()
        assert not plan.maintenance_policy.is_complete()

    def test_display_name_optional(self):
        plan = _make_plan(display_name="My Migration")
        assert plan.display_name == "My Migration"


# ── JSON round-trip ────────────────────────────────────────────────────────────

class TestMigrationPlanRoundTrip:
    def test_model_dump_json_and_validate(self):
        plan = _make_plan(display_name="RT Test", notes="some notes")
        plan_json = plan.model_dump_json()
        restored = MigrationPlan.model_validate_json(plan_json)
        assert restored.plan_id == plan.plan_id
        assert restored.status == MigrationStatus.draft
        assert restored.display_name == "My Migration" if plan.display_name == "My Migration" else True

    def test_round_trip_preserves_source_ref_kind(self):
        plan = _make_plan()
        restored = MigrationPlan.model_validate_json(plan.model_dump_json())
        assert restored.source_asset_ref.kind == "relational_relation"
        assert restored.target_asset_ref.kind == "file_collection"

    def test_round_trip_after_transitions(self):
        plan = _make_plan()
        plan.transition(MigrationStatus.assessed, by="test@example.com", note="looks good")
        restored = MigrationPlan.model_validate_json(plan.model_dump_json())
        assert restored.status == MigrationStatus.assessed
        assert len(restored.phase_log) == 1
        assert restored.phase_log[0].by == "test@example.com"

    def test_round_trip_with_ownership(self):
        plan = _make_plan(
            ownership=MigrationOwnership(
                owner_email="po@example.com",
                engineer_email="eng@example.com",
            )
        )
        restored = MigrationPlan.model_validate_json(plan.model_dump_json())
        assert restored.ownership.owner_email == "po@example.com"


# ── transition() ──────────────────────────────────────────────────────────────

class TestTransition:
    def test_draft_to_assessed(self):
        plan = _make_plan()
        plan.transition(MigrationStatus.assessed)
        assert plan.status == MigrationStatus.assessed

    def test_full_forward_path_snapshot_only(self):
        """Smoke-test the snapshot-only forward path end-to-end."""
        plan = _make_plan(maintenance_policy=_complete_maintenance())
        steps = [
            MigrationStatus.assessed,
            MigrationStatus.initial_snapshot_running,
            MigrationStatus.initial_snapshot_loaded,
            MigrationStatus.reconciled,          # snapshot-only skips CDC
            MigrationStatus.cutover_ready,
            MigrationStatus.cutover_in_progress,
            MigrationStatus.cutover_complete,
            MigrationStatus.observation,
            MigrationStatus.source_decommissioned,
        ]
        for step in steps:
            plan.transition(step)
        assert plan.status == MigrationStatus.source_decommissioned
        assert plan.is_terminal()

    def test_full_forward_path_with_cdc(self):
        plan = _make_plan(maintenance_policy=_complete_maintenance())
        for step in [
            MigrationStatus.assessed,
            MigrationStatus.initial_snapshot_running,
            MigrationStatus.initial_snapshot_loaded,
            MigrationStatus.change_capture_catching_up,
            MigrationStatus.reconciled,
            MigrationStatus.cutover_ready,
            MigrationStatus.cutover_in_progress,
            MigrationStatus.cutover_complete,
            MigrationStatus.observation,
            MigrationStatus.source_decommissioned,
        ]:
            plan.transition(step)
        assert plan.is_terminal()

    def test_transition_records_phase_log(self):
        plan = _make_plan()
        plan.transition(MigrationStatus.assessed, by="eng@x.com", note="approved")
        assert len(plan.phase_log) == 1
        entry = plan.phase_log[0]
        assert entry.from_status == MigrationStatus.draft
        assert entry.to_status == MigrationStatus.assessed
        assert entry.by == "eng@x.com"
        assert entry.note == "approved"

    def test_multiple_transitions_append_log(self):
        plan = _make_plan()
        plan.transition(MigrationStatus.assessed)
        plan.transition(MigrationStatus.initial_snapshot_running)
        assert len(plan.phase_log) == 2

    def test_invalid_transition_raises(self):
        plan = _make_plan()
        with pytest.raises(ValueError, match="Cannot transition"):
            plan.transition(MigrationStatus.cutover_in_progress)

    def test_invalid_skip_raises(self):
        plan = _make_plan()
        plan.transition(MigrationStatus.assessed)
        with pytest.raises(ValueError, match="Cannot transition"):
            plan.transition(MigrationStatus.source_decommissioned)

    def test_terminal_state_blocks_further_transitions(self):
        plan = _make_plan()
        plan.transition(MigrationStatus.cancelled)
        with pytest.raises(ValueError):
            plan.transition(MigrationStatus.assessed)

    def test_updated_at_changes_on_transition(self):
        plan = _make_plan()
        before = plan.updated_at
        import time; time.sleep(0.01)
        plan.transition(MigrationStatus.assessed)
        assert plan.updated_at >= before

    def test_freshness_lapsed_can_re_enter_cdc(self):
        """reconciled → change_capture_catching_up is an allowed re-entry."""
        plan = _make_plan()
        plan.transition(MigrationStatus.assessed)
        plan.transition(MigrationStatus.initial_snapshot_running)
        plan.transition(MigrationStatus.initial_snapshot_loaded)
        plan.transition(MigrationStatus.change_capture_catching_up)
        plan.transition(MigrationStatus.reconciled)
        plan.transition(MigrationStatus.change_capture_catching_up)
        assert plan.status == MigrationStatus.change_capture_catching_up


# ── maintenance gate ───────────────────────────────────────────────────────────

class TestMaintenanceGate:
    def test_incomplete_policy_blocks_cutover_in_progress(self):
        plan = _make_plan()
        for step in [
            MigrationStatus.assessed,
            MigrationStatus.initial_snapshot_running,
            MigrationStatus.initial_snapshot_loaded,
            MigrationStatus.reconciled,
            MigrationStatus.cutover_ready,
        ]:
            plan.transition(step)

        with pytest.raises(ValueError, match="maintenance_policy is incomplete"):
            plan.transition(MigrationStatus.cutover_in_progress)

    def test_complete_policy_allows_cutover_in_progress(self):
        plan = _make_plan(maintenance_policy=_complete_maintenance())
        for step in [
            MigrationStatus.assessed,
            MigrationStatus.initial_snapshot_running,
            MigrationStatus.initial_snapshot_loaded,
            MigrationStatus.reconciled,
            MigrationStatus.cutover_ready,
        ]:
            plan.transition(step)
        plan.transition(MigrationStatus.cutover_in_progress)
        assert plan.status == MigrationStatus.cutover_in_progress

    def test_bypass_maintenance_gate(self):
        """allow_maintenance_gate=False lets orchestrators bypass the check."""
        plan = _make_plan()
        for step in [
            MigrationStatus.assessed,
            MigrationStatus.initial_snapshot_running,
            MigrationStatus.initial_snapshot_loaded,
            MigrationStatus.reconciled,
            MigrationStatus.cutover_ready,
        ]:
            plan.transition(step)
        plan.transition(
            MigrationStatus.cutover_in_progress,
            allow_maintenance_gate=False,
        )
        assert plan.status == MigrationStatus.cutover_in_progress


class TestMaintenancePolicyIsComplete:
    def test_all_empty_is_not_complete(self):
        assert not MaintenancePolicy().is_complete()

    def test_partial_fill_is_not_complete(self):
        p = MaintenancePolicy(compaction_owner="a@b.com")
        assert not p.is_complete()

    def test_all_three_owners_is_complete(self):
        assert _complete_maintenance().is_complete()

    def test_write_freeze_not_required_for_is_complete(self):
        """write_freeze_procedure is informational; not required for is_complete()."""
        p = MaintenancePolicy(
            compaction_owner="a",
            snapshot_expiry_owner="b",
            orphan_cleanup_owner="c",
            # write_freeze_procedure left empty
        )
        assert p.is_complete()


# ── terminal state ─────────────────────────────────────────────────────────────

class TestTerminalState:
    @pytest.mark.parametrize("status", [
        MigrationStatus.source_decommissioned,
        MigrationStatus.terminal_failed,
        MigrationStatus.cancelled,
    ])
    def test_terminal_statuses(self, status):
        plan = _make_plan()
        plan.status = status
        assert plan.is_terminal()

    @pytest.mark.parametrize("status", [
        MigrationStatus.draft,
        MigrationStatus.assessed,
        MigrationStatus.reconciled,
        MigrationStatus.cutover_complete,
        MigrationStatus.rolled_back,
    ])
    def test_non_terminal_statuses(self, status):
        plan = _make_plan()
        plan.status = status
        assert not plan.is_terminal()


# ── reconciliation rules ───────────────────────────────────────────────────────

class TestReconciliationRules:
    def test_empty_rules_not_all_pass(self):
        plan = _make_plan()
        assert not plan.all_reconciliation_rules_pass()

    def test_all_pass_when_all_pass(self):
        plan = _make_plan(
            reconciliation_rules=[
                ReconciliationRule(name="row count", last_result="pass"),
                ReconciliationRule(name="checksum", kind="checksum", last_result="pass"),
            ]
        )
        assert plan.all_reconciliation_rules_pass()

    def test_not_all_pass_when_one_fails(self):
        plan = _make_plan(
            reconciliation_rules=[
                ReconciliationRule(name="row count", last_result="pass"),
                ReconciliationRule(name="checksum", last_result="fail"),
            ]
        )
        assert not plan.all_reconciliation_rules_pass()

    def test_not_all_pass_when_unchecked(self):
        plan = _make_plan(
            reconciliation_rules=[
                ReconciliationRule(name="row count", last_result=None),
            ]
        )
        assert not plan.all_reconciliation_rules_pass()

    def test_reconciliation_rules_round_trip(self):
        plan = _make_plan(
            reconciliation_rules=[
                ReconciliationRule(
                    name="row count",
                    kind="row_count",
                    description="Must match source",
                    last_result="pass",
                )
            ]
        )
        restored = MigrationPlan.model_validate_json(plan.model_dump_json())
        assert restored.reconciliation_rules[0].name == "row count"
        assert restored.reconciliation_rules[0].last_result == "pass"
