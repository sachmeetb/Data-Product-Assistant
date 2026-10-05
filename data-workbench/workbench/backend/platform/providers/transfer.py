"""DuckDB cross-platform transfer provider (Phase 2.0).

Implements the ``TransferExecutionProvider`` Protocol. The Phase-2.0 engine is
DuckDB (reads postgres/mysql via extensions, writes an ATTACH'd postgres or a
DuckDB catalog); a dlt engine is the Phase-2.1 alternative for warehouse targets.

``plan_transfer`` is pure (builds a ``TransferPlan`` from a spec + compiled
DuckDB model bodies via the placement planner). The heavy ``execute_transfer``
is realized by ``transfer_execution.run_transfer`` (which needs project/session
context); this provider's ``execute_transfer`` is a thin conformance wrapper that
raises if called without that context, steering callers to the worker.
"""
from __future__ import annotations

from typing import Any

from ..interfaces import (
    CompensationReport,
    DeploymentRun,
    TransferDatasetPlan,
    TransferPlan,
    TransferSpec,
    VerificationReport,
)
from ..transform_placement import plan_placement, realized_placement

PLATFORM_TYPE = "duckdb"


def _safe(name: str) -> str:
    import re
    return re.sub(r"[^a-zA-Z0-9_]", "_", name or "").lower()


class DuckDBTransferProvider:
    """TransferExecutionProvider backed by the DuckDB EL engine."""

    def plan_transfer(
        self,
        spec: TransferSpec,
        compiled_models: list[dict[str, Any]] | None = None,
        model_summary: dict[str, Any] | None = None,
    ) -> TransferPlan:
        """Build a TransferPlan from a spec + compiled DuckDB model bodies.

        Pure: no data I/O. ``compiled_models`` items:
        ``{physical_name, model_name, select_body}`` (from
        ``generate_view_ddl.generate_lakehouse_models``). The placement decision
        is recorded for reporting; the Phase-2.0 executor realizes
        ``transform_on_extract`` (source-side compute, shaped-result move).
        """
        ops = _ops_from_summary(model_summary or {})
        decision = plan_placement(spec.placement, ops)
        realized = realized_placement(decision)
        datasets: list[TransferDatasetPlan] = []
        for m in compiled_models or []:
            safe = m.get("model_name") or _safe(m.get("physical_name", ""))
            datasets.append(TransferDatasetPlan(
                physical_name=m.get("physical_name", ""),
                target_relation=f"{spec.target_schema}.{safe}",
                extract_sql=m.get("select_body", ""),
                write_disposition=spec.write_disposition,
                target_model_sql=None,  # transform_on_extract: shape already in extract
            ))
        warnings = list(decision.warnings)
        if realized != decision.requested_placement and decision.requested_placement != "transform_on_extract":
            warnings.append(
                f"requested placement '{decision.requested_placement}' realized as "
                f"'{realized}' (source-side compute); hybrid/ELT push-down is Phase 2.1."
            )
        return TransferPlan(
            spec=spec, datasets=datasets, placement=realized, warnings=warnings,
        )

    def execute_transfer(self, plan: TransferPlan, idempotency_key: str) -> DeploymentRun:
        raise NotImplementedError(
            "Use transfer_execution.run_transfer(project, session, ...) — the "
            "executor needs project/session context not carried by the Protocol."
        )

    def verify_transfer(self, run: DeploymentRun) -> VerificationReport:
        # Verification is inline in run_transfer (landed vs expected count).
        return VerificationReport(passed=(run.status == "succeeded"))

    def compensate_transfer(self, run: DeploymentRun) -> CompensationReport:
        return CompensationReport(compensated=False, warnings=["compensation is manual in Phase 2.0"])


def _ops_from_summary(model_summary: dict[str, Any]) -> list[dict]:
    ops: list[dict] = []
    for v in (model_summary or {}).get("views", []) or []:
        if not isinstance(v, dict):
            continue
        ds = v.get("view_name") or v.get("physical_name") or ""
        if v.get("used_filter") or v.get("filter"):
            ops.append({"kind": "filter", "dataset": ds})
        if v.get("used_grouping") or v.get("grouping"):
            ops.append({"kind": "aggregate", "dataset": ds})
        if v.get("join_count") or v.get("joins"):
            ops.append({"kind": "join", "dataset": ds})
    return ops
