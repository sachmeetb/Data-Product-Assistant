"""Migration lifecycle model — MigrationPlan v1.

A MigrationPlan describes how a source asset moves to a target platform through
a structured state machine.  It captures the ownership assignments and
maintenance responsibilities required by Phase 3 (§9 of the architecture review):

  draft → assessed → initial_snapshot_running → initial_snapshot_loaded
    → change_capture_catching_up → reconciled → cutover_ready
    → cutover_in_progress → cutover_complete → observation
    → source_decommissioned

Failure / operator states:
  paused | retryable_failed | compensation_required | rolled_back
  | terminal_failed | cancelled

Maintenance ownership (compaction, snapshot, orphan cleanup) lives on
``maintenance_policy`` so it is explicit and never implicit.  Engineers
can only promote a plan through the state machine by providing all required
fields for the target state; the ``ready_for_state`` helpers enforce this.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .transfer_batch import PhysicalAssetRef, SnapshotBoundary


# ── state machine ──────────────────────────────────────────────────────────────

class MigrationStatus(str, Enum):
    # Forward path
    draft = "draft"
    assessed = "assessed"
    initial_snapshot_running = "initial_snapshot_running"
    initial_snapshot_loaded = "initial_snapshot_loaded"
    change_capture_catching_up = "change_capture_catching_up"
    reconciled = "reconciled"
    cutover_ready = "cutover_ready"
    cutover_in_progress = "cutover_in_progress"
    cutover_complete = "cutover_complete"
    observation = "observation"
    source_decommissioned = "source_decommissioned"
    # Operator / failure states
    paused = "paused"
    retryable_failed = "retryable_failed"
    compensation_required = "compensation_required"
    rolled_back = "rolled_back"
    terminal_failed = "terminal_failed"
    cancelled = "cancelled"


# Valid forward transitions (failure states can transition to any resumable state
# or to terminal/cancelled; not enumerated here — operator decision).
_FORWARD_TRANSITIONS: dict[MigrationStatus, set[MigrationStatus]] = {
    MigrationStatus.draft: {MigrationStatus.assessed, MigrationStatus.cancelled},
    MigrationStatus.assessed: {
        MigrationStatus.initial_snapshot_running,
        MigrationStatus.cancelled,
    },
    MigrationStatus.initial_snapshot_running: {
        MigrationStatus.initial_snapshot_loaded,
        MigrationStatus.retryable_failed,
        MigrationStatus.terminal_failed,
    },
    MigrationStatus.initial_snapshot_loaded: {
        MigrationStatus.change_capture_catching_up,
        MigrationStatus.reconciled,   # snapshot-only mode skips CDC
    },
    MigrationStatus.change_capture_catching_up: {
        MigrationStatus.reconciled,
        MigrationStatus.retryable_failed,
        MigrationStatus.compensation_required,
    },
    MigrationStatus.reconciled: {
        MigrationStatus.cutover_ready,
        MigrationStatus.change_capture_catching_up,  # freshness lapsed — re-catch
    },
    MigrationStatus.cutover_ready: {
        MigrationStatus.cutover_in_progress,
        MigrationStatus.cancelled,
    },
    MigrationStatus.cutover_in_progress: {
        MigrationStatus.cutover_complete,
        MigrationStatus.compensation_required,
        MigrationStatus.rolled_back,
    },
    MigrationStatus.cutover_complete: {MigrationStatus.observation},
    MigrationStatus.observation: {MigrationStatus.source_decommissioned, MigrationStatus.rolled_back},
}


def valid_next_statuses(current: MigrationStatus) -> set[MigrationStatus]:
    """Return the set of states reachable from ``current`` via a forward transition."""
    return _FORWARD_TRANSITIONS.get(current, set())


# ── supporting models ──────────────────────────────────────────────────────────

class MigrationOwnership(BaseModel):
    """Who is responsible for this migration."""
    owner_email: str = Field(..., description="Product Owner accountable for the migration")
    engineer_email: str = Field(..., description="Data Engineer executing the migration")
    assigned_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    notes: str = ""


class MaintenancePolicy(BaseModel):
    """Assigns explicit ownership for ongoing maintenance tasks.

    Every field that defaults to "" must be filled before the plan can advance
    past ``cutover_complete``.  Unassigned maintenance == a blocked promotion.
    """
    compaction_owner: str = Field(
        default="",
        description=(
            "Email or team responsible for running Iceberg table compaction. "
            "Required before cutover_complete → observation."
        ),
    )
    snapshot_expiry_owner: str = Field(
        default="",
        description="Owner responsible for expiring old Iceberg snapshots.",
    )
    orphan_cleanup_owner: str = Field(
        default="",
        description="Owner responsible for removing orphaned files not referenced by any snapshot.",
    )
    write_freeze_procedure: str = Field(
        default="",
        description=(
            "Description of the dual-run or write-freeze procedure during cutover. "
            "Required before cutover_ready → cutover_in_progress."
        ),
    )
    rollback_window_hours: int = Field(
        default=0,
        description="How long after cutover_complete the source remains live as a rollback target.",
    )

    def is_complete(self) -> bool:
        """True when all maintenance owners are assigned."""
        return all([
            self.compaction_owner,
            self.snapshot_expiry_owner,
            self.orphan_cleanup_owner,
        ])


class PhaseLogEntry(BaseModel):
    """One transition in the plan's state machine log."""
    from_status: MigrationStatus
    to_status: MigrationStatus
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    by: str = ""    # email of the person / system that triggered the transition
    note: str = ""


class ReconciliationRule(BaseModel):
    """One row-count, checksum, or business rule that must pass before cutover."""
    name: str
    kind: Literal["row_count", "checksum", "business_rule"] = "row_count"
    description: str = ""
    last_result: Optional[str] = None   # "pass" | "fail" | None (not yet run)
    last_checked_at: Optional[datetime] = None


# ── migration plan ─────────────────────────────────────────────────────────────

class MigrationPlan(BaseModel):
    """Full migration plan from source asset to target platform.

    Create one plan per source-to-target pair.  Advance status through the
    state machine by calling ``transition()`` or by direct assignment when
    an external orchestrator drives the lifecycle.

    Serialize to JSON for durable storage::

        plan_json = plan.model_dump_json(indent=2)

    Parse back::

        plan = MigrationPlan.model_validate_json(plan_json)
    """

    # ── identity ───────────────────────────────────────────────────────────
    contract_version: Literal["1"] = "1"
    plan_id: str = Field(..., description="Opaque plan identifier; typically a UUID")
    display_name: str = ""

    # ── assets ─────────────────────────────────────────────────────────────
    source_asset_ref: PhysicalAssetRef
    target_asset_ref: PhysicalAssetRef

    # ── status ─────────────────────────────────────────────────────────────
    status: MigrationStatus = MigrationStatus.draft
    phase_log: list[PhaseLogEntry] = Field(default_factory=list)

    # ── snapshot ───────────────────────────────────────────────────────────
    snapshot_boundary: Optional[SnapshotBoundary] = None
    snapshot_batch_id: str = Field(
        default="",
        description="batch_id of the TransferBatch that carried the initial snapshot",
    )

    # ── CDC ────────────────────────────────────────────────────────────────
    cdc_provider: str = Field(
        default="",
        description='e.g. "debezium", "aws_dms", "kafka_connector", "none" (snapshot-only)',
    )
    cdc_checkpoint: str = Field(
        default="",
        description="Last committed CDC offset / LSN / timestamp",
    )
    cdc_scope: Literal["snapshot_only", "snapshot_plus_cdc"] = "snapshot_only"

    # ── write policy ───────────────────────────────────────────────────────
    write_mode: Literal["append", "upsert", "full_replace", "scd2"] = "full_replace"
    schema_change_policy: Literal["fail", "evolve", "ignore"] = "fail"
    freshness_threshold_seconds: int = Field(
        default=0,
        description=(
            "Maximum allowed lag before the plan is no longer considered reconciled. "
            "0 = no threshold (snapshot-only plans)."
        ),
    )

    # ── reconciliation ─────────────────────────────────────────────────────
    reconciliation_rules: list[ReconciliationRule] = Field(default_factory=list)

    # ── rollback ───────────────────────────────────────────────────────────
    rollback_target: Optional[PhysicalAssetRef] = Field(
        default=None,
        description="Where to revert writes if cutover fails; often the source itself.",
    )

    # ── ownership ──────────────────────────────────────────────────────────
    ownership: Optional[MigrationOwnership] = None
    maintenance_policy: MaintenancePolicy = Field(
        default_factory=MaintenancePolicy
    )

    # ── lifecycle ──────────────────────────────────────────────────────────
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # ── notes ──────────────────────────────────────────────────────────────
    notes: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)

    # ── state machine helpers ──────────────────────────────────────────────

    def transition(
        self,
        to: MigrationStatus,
        *,
        by: str = "",
        note: str = "",
        allow_maintenance_gate: bool = True,
    ) -> None:
        """Advance the plan to ``to``, enforcing forward-transition rules.

        Raises ``ValueError`` for:
        - invalid forward transitions (consult ``valid_next_statuses()``).
        - attempting ``cutover_in_progress`` without a complete ``maintenance_policy``
          (when ``allow_maintenance_gate=True``).
        """
        allowed = valid_next_statuses(self.status)
        if to not in allowed:
            raise ValueError(
                f"Cannot transition from {self.status.value!r} to {to.value!r}. "
                f"Valid next states: {sorted(s.value for s in allowed)}"
            )
        if (
            allow_maintenance_gate
            and to == MigrationStatus.cutover_in_progress
            and not self.maintenance_policy.is_complete()
        ):
            raise ValueError(
                "Cannot enter cutover_in_progress: maintenance_policy is incomplete. "
                "Assign compaction_owner, snapshot_expiry_owner, and orphan_cleanup_owner first."
            )

        self.phase_log.append(
            PhaseLogEntry(from_status=self.status, to_status=to, by=by, note=note)
        )
        self.status = to
        self.updated_at = datetime.now(timezone.utc)

    def is_terminal(self) -> bool:
        return self.status in {
            MigrationStatus.source_decommissioned,
            MigrationStatus.terminal_failed,
            MigrationStatus.cancelled,
        }

    def all_reconciliation_rules_pass(self) -> bool:
        """True when every rule has been checked and passed."""
        return bool(self.reconciliation_rules) and all(
            r.last_result == "pass" for r in self.reconciliation_rules
        )
