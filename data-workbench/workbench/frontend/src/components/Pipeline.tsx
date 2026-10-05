import { useState, useEffect, useRef } from "react";
import api from "../api/client";
import type { StageInfo, ConfigOption, WorkflowInfo } from "../types";
import { canRunStage } from "../types";
import { useRole } from "../RoleContext";
import { useReadOnly } from "../AuthContext";
import StageChip, { STAGE_ICONS, STAGE_ICONS_BY_ID } from "./StageChip";
import PlaybookSelector from "./PlaybookSelector";
import ConnectionPickerDialog, { type ConnectionPickerResult } from "./ConnectionPickerDialog";
import MaterializationGateDialog from "./MaterializationGateDialog";
import { useConfirm, usePrompt, useNotify } from "./dialogContext";
import ConfigureServingDialog from "./ConfigureServingDialog";
import ConfigureTransferPlacementDialog from "./ConfigureTransferPlacementDialog";
import ConfigureDqDialog from "./ConfigureDqDialog";
import ConfigureMigrationDialog from "./ConfigureMigrationDialog";
import { downloadBlobZip } from "../lib/download";

// Completed serving stages expose a downloadable, runnable package — the same
// artifact Data Workbench runs. Maps the stage_id to its package endpoint suffix.
const SERVING_PACKAGE_ENDPOINTS: Record<string, string> = {
  // Package + Git actions live on the BUILD (serving) stage only — the package is
  // assembled at Build time (no live DB) and is the same artifact regardless of
  // deploy. The Deploy stages (deploy_virtual_view / _physical_copy / _lakehouse /
  // _transfer) intentionally do NOT repeat these buttons.
  serving_virtual_view: "view-package",
  serving_physical_copy: "dbt-project",
  serving_lakehouse_export: "lakehouse-package",
  serving_transfer: "transfer-package",
  // dmig: the migration package lives under /migration/package (not /serving/*);
  // the download onClick special-cases this suffix. The package is produced by the
  // Generate stage (writes migration.json; the endpoint assembles on demand), so
  // the Download/Push buttons live on Generate only — not on Run Migration.
  dmig_generate_pipeline: "migration-package",
};

// Package endpoints whose URL is /migration/* rather than /serving/*.
const MIGRATION_PACKAGE_STAGES = new Set(["dmig_generate_pipeline"]);

// DQ test generation stages that produce a downloadable test package.
const DQ_PACKAGE_STAGES = new Set(["dq_test_generation_gx", "dq_test_generation_python"]);

// Non-LLM stages whose NON_LLM_ACTIONS entry just opens a config/edit dialog that
// owns its own completion — never pre-reset them on Re-run. Resetting first would
// strand the stage at `pending` if the engineer cancels the dialog (matching each
// stage's dedicated Reconfigure/Edit button, which also never resets). Every OTHER
// non-LLM action self-completes (build/deploy/publish call /complete internally),
// so it IS reset-first (an already-`complete` stage 400s on the coupled /complete).
const NON_LLM_CONFIG_STAGES = new Set([
  "odcs_specification", "select_data_source",
  "configure_serving", "configure_dq", "configure_transfer_placement",
]);

interface Props {
  projectId: number;
  stages: StageInfo[];
  activeStage: number | null;
  activeWorkflow?: string | null;
  onStageClick: (stageNumber: number, workflowId?: string) => void;
  onRunStage: (stageNumber: number, stageConfig?: Record<string, string>, workflowId?: string) => void;
  onStageCompleted?: () => void;
  runningStage: number | null;
  runningWorkflow?: string | null;
  multiWorkflow?: boolean;
  workflows?: WorkflowInfo[];
  // For dpe-sa: status of the (filtered-out) po_source_validation stage so
  // the engineer's first materialization stage can render "Pending PO
  // validation" instead of plain "Pending". Null when the project has no
  // such gate (dpe-cf, dq, dd).
  poGateStatus?: string | null;
  /** True when the latest request is still 'submitted' (engineer must Accept) —
   *  disambiguates the blocked head stage from "waiting on the PO". */
  requestNotAccepted?: boolean;
}

// SA materialization stage that's the actual choke-point on the PO gate.
// Subsequent materialization stages cascade off this one's completion, so
// painting the whole tail amber would be noisy — mark only the head.
const SA_BLOCKED_HEAD_STAGE = "synthesize_odcs_from_graph";

// Stage ID → display name lookup for the catalog
const STAGE_REGISTRY_NAMES: Record<string, string> = {
  initiate: "Initiate", data_discovery_composite: "Discovery", data_profiling_composite: "Profiling",
  metadata_enrichment: "Enrichment", dq_rule_generation: "DQ Rules",
  data_scoring: "Scoring",
  data_remediation: "Remediation", rescore_composite: "Re-Score",
  data_mapping: "Mapping", configure_serving: "Configure Serving",
  configure_transfer_placement: "Transform Placement",
  serving_virtual_view: "Virtual View", serving_physical_copy: "dbt Project",
  serving_lakehouse_export: "Lakehouse Package", serving_transfer: "Transfer Pipeline",
  deploy_virtual_view: "Deploy View",
  deploy_physical_copy: "Materialize (dbt)", deploy_lakehouse: "Run Export",
  deploy_transfer: "Run Transfer",
  deployment_reflection: "Reflection",
  configure_dq: "Configure DQ",
  dq_testing_gx: "DQ Testing (GX)", dq_testing_python: "DQ Testing (Python)",
  odcs_specification: "ODCS Contract", odcs_to_dprod: "ODCS to DPROD",
  reflect_on_reviews: "Reflect", publish: "Publish",
  mark_engineering_complete: "Mark Complete",
  // dmig (data migration)
  dmig_configure: "Configure Migration", dmig_assess_plan: "Assess & Plan",
  dmig_generate_pipeline: "Generate Pipeline", dmig_execute_transfer: "Run Migration",
  dmig_reconcile: "Reconcile",
  // cmig (code migration)
  cmig_link: "Link Migration", cmig_import_code: "Import Code",
  cmig_configure: "Configure", cmig_reverse_engineer: "Reverse-Engineer",
  cmig_forward_engineer: "Forward-Engineer", cmig_package: "Package",
};

// Broad capability phases — the board groups every stage into one of these by
// stage_id, flattening across the granular workflows. Order = lifecycle order.
const CAPABILITY_PHASES: { key: string; label: string; optional?: boolean; ids: string[] }[] = [
  { key: "discover", label: "Discover & document", ids: [
    "select_data_source", "data_discovery", "data_discovery_composite", "load_schema",
    // Schema-only migration's deterministic replacement for live discovery.
    "dmig_import_schema",
    // Offline dpe-sa's deterministic replacement for live discovery + profiling.
    "data_discovery_offline",
    "data_profiling", "data_profiling_composite", "load_profiles", "metadata_enrichment",
    "source_naming_recommendations", "column_name_standardization", "product_definition",
  ] },
  { key: "validate", label: "Validate", ids: ["mark_discovery_complete", "po_source_validation"] },
  // dmig (data migration): configure target → assess & plan → generate pipeline →
  // run → reconcile. (select_data_source + discovery live in the discover phase.)
  { key: "migrate", label: "Migrate", ids: [
    "dmig_configure", "dmig_assess_plan", "dmig_generate_pipeline",
    "dmig_execute_transfer", "dmig_reconcile",
  ] },
  { key: "materialize", label: "Materialize & serve", ids: [
    "initiate", "odcs_specification", "synthesize_odcs_from_graph", "odcs_to_dprod", "auto_mapping_sa",
    "data_mapping", "configure_serving", "configure_transfer_placement",
    "serving_virtual_view", "serving_physical_copy", "serving_lakehouse_export", "serving_transfer",
    "deploy_virtual_view", "deploy_physical_copy", "deploy_lakehouse", "deploy_transfer",
    "deployment_reflection", "mark_engineering_complete", "publish", "reflect_on_reviews",
  ] },
  { key: "quality", label: "Data quality & scoring", optional: true, ids: [
    "dq_rule_generation", "configure_dq", "dq_testing_gx", "dq_testing_python", "dq_test_generation_gx",
    "dq_test_generation_python", "dq_test_execution", "data_scoring", "rescore_composite",
    "data_remediation", "data_remediation_analysis", "data_remediation_planning",
    "domain_rule_enhancement", "data_quality_failure_analysis",
  ] },
];
const PHASE_BY_STAGE: Record<string, string> = {};
for (const p of CAPABILITY_PHASES) for (const id of p.ids) PHASE_BY_STAGE[id] = p.key;
const phaseOf = (stageId?: string): string => (stageId && PHASE_BY_STAGE[stageId]) || "materialize";

// The config dialog contract is `configValues: Record<string, string>` — multiselects
// serialize as `", "`-joined strings (see toggleMultiselect / .split(", ") readers).
// Server-provided `defaults` are untyped JSON, though, and a multiselect default can
// arrive as an array (e.g. discovery-scope pre-checks). Coerce every incoming default
// into the string contract so a non-string value can't crash the render (`.trim` /
// `.split` is not a function).
const normalizeConfigDefaults = (raw: unknown): Record<string, string> => {
  const out: Record<string, string> = {};
  if (!raw || typeof raw !== "object") return out;
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    if (value == null) continue;
    out[key] = Array.isArray(value) ? value.map(String).join(", ") : String(value);
  }
  return out;
};

export default function Pipeline({ projectId, stages, activeStage, activeWorkflow, onStageClick, onRunStage, onStageCompleted, runningStage, runningWorkflow, multiWorkflow, workflows, poGateStatus, requestNotAccepted }: Props) {
  const role = useRole();
  const readOnly = useReadOnly();
  const confirm = useConfirm();
  const prompt = usePrompt();
  const { showError, notify } = useNotify();
  const [configOpen, setConfigOpen] = useState<number | null>(null);
  const [configWorkflow, setConfigWorkflow] = useState<string | undefined>(undefined);
  const [configValues, setConfigValues] = useState<Record<string, string>>({});
  const [configOptions, setConfigOptions] = useState<Record<string, ConfigOption[]>>({});
  const [optionsLoading, setOptionsLoading] = useState(false);
  const [playbookStage, setPlaybookStage] = useState<StageInfo | null>(null);
  const [playbookWorkflow, setPlaybookWorkflow] = useState<string | undefined>(undefined);
  const [actionLoading, setActionLoading] = useState<number | null>(null);
  const [pendingRerun, setPendingRerun] = useState<{ stageNumber: number; workflowId?: string } | null>(null);
  // Framework (DQ) and serving-mode switching are done in their Configure dialogs
  // (ConfigureDqDialog / ConfigureServingDialog), which perform the exclusive-group
  // swap. The Build cards no longer carry an inline toggle, so the old
  // handleDqFrameworkSwitch / handleServingSwitch helpers were removed.
  const [dataSourceTarget, setDataSourceTarget] = useState<StageInfo | null>(null);
  const [materializeGate, setMaterializeGate] = useState<StageInfo | null>(null);
  // Floating tooltip for stage rows — shows the registered :description so a
  // demo presenter can hover any stage and reveal a one-sentence functional
  // explainer. Single render outside the row loop keeps the DOM clean.
  const [hoveredStage, setHoveredStage] = useState<{ stage: StageInfo; x: number; y: number } | null>(null);
  const [connectionPickerOpen, setConnectionPickerOpen] = useState(false);
  const [connectionPickerSaving, setConnectionPickerSaving] = useState(false);
  const [dataSourceMode, setDataSourceMode] = useState<"create" | "edit">("create");
  const [configureServingStage, setConfigureServingStage] = useState<StageInfo | null>(null);
  const [transferPlacementStage, setTransferPlacementStage] = useState<StageInfo | null>(null);
  const [configureDqStage, setConfigureDqStage] = useState<StageInfo | null>(null);
  const [configureMigrationStage, setConfigureMigrationStage] = useState<StageInfo | null>(null);
  // Capability board scope: default hides cards that aren't part of the project's
  // plan (the addable-from-catalog "optional" cards + empty phases). Toggle on to
  // browse every capability by category.
  const [showAllCaps, setShowAllCaps] = useState(false);
  // Tracks whether the engineer has already sent a source-candidates-needed
  // request for THIS dialog open, so the empty-picker CTA can disable itself
  // after a click instead of spamming duplicate requests.
  const [sourceCandidatesRequestSent, setSourceCandidatesRequestSent] = useState(false);

  // Git integration: is a provider configured, and has this product been pushed?
  const [gitCfg, setGitCfg] = useState<{ configured: boolean; repo_url: string | null }>({
    configured: false, repo_url: null,
  });
  const [gitPushing, setGitPushing] = useState(false);
  const [gitPushErr, setGitPushErr] = useState<string | null>(null);

  useEffect(() => {
    api.get(`/api/projects/${projectId}/serving/git-status`)
      .then((r) => setGitCfg({ configured: !!r.data.configured, repo_url: r.data.repo_url || null }))
      .catch(() => {});
  }, [projectId]);

  const handlePushToGit = async () => {
    setGitPushing(true);
    setGitPushErr(null);
    try {
      const r = await api.post(`/api/projects/${projectId}/serving/push-to-git`);
      setGitCfg((prev) => ({ ...prev, repo_url: r.data.repo_url || prev.repo_url }));
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setGitPushErr(detail || "Push failed");
    }
    setGitPushing(false);
  };

  // Object-store publish: is a target bound for this project? (ADR-14) Config +
  // presigned links live on the ProjectDashboard serving card; this is the
  // in-pipeline trigger, shown only when a target is already bound.
  const [storageCfg, setStorageCfg] = useState<{ configured: boolean }>({ configured: false });
  const [storagePublishing, setStoragePublishing] = useState(false);
  const [storageMsg, setStorageMsg] = useState<string | null>(null);
  const [storageErr, setStorageErr] = useState<string | null>(null);

  useEffect(() => {
    api.get(`/api/projects/${projectId}/serving/storage-status`)
      .then((r) => setStorageCfg({ configured: !!r.data.configured }))
      .catch(() => {});
  }, [projectId]);

  const handlePublishToStorage = async () => {
    setStoragePublishing(true);
    setStorageErr(null);
    setStorageMsg(null);
    try {
      const r = await api.post(`/api/projects/${projectId}/serving/push-to-storage`);
      const d = r.data;
      setStorageMsg(d.status === "empty"
        ? "No data artifacts to publish yet"
        : `Published ${d.object_count} objects`);
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setStorageErr(detail || "Publish failed");
    }
    setStoragePublishing(false);
  };

  const requestSourceCandidatesFromPo = async (notes: string) => {
    if (sourceCandidatesRequestSent) return;
    try {
      await api.post(
        `/api/projects/${projectId}/product-requests/source-candidates-needed`,
        {
          engineer: "data-engineer",
          notes,
        },
      );
      setSourceCandidatesRequestSent(true);
    } catch (e) {
      console.error("Failed to send source-candidates-needed request", e);
    }
  };

  const openDataSourceDialog = (stage: StageInfo, mode: "create" | "edit") => {
    setDataSourceMode(mode);
    setDataSourceTarget(stage);
    setConnectionPickerOpen(true);
  };

  const handleRerunConfirm = async (stage: StageInfo, workflowId?: string) => {
    const wfId = workflowId || stage.workflow_id;
    setPendingRerun(null);
    const nonLlmAction = stage.stage_id ? NON_LLM_ACTIONS[stage.stage_id] : undefined;
    if (nonLlmAction) {
      // Non-LLM stages have no skill/prompt: the WS `run_stage` path does no
      // work for them (start_stage_run returns early) — the actual side-effect
      // (rebuild the package, publish, …) lives in the stage's own action, and
      // onRunStage historically stranded the card at "Running…" too. So drive
      // the stage's own action (self-clears via `actionLoading` →
      // onStageCompleted → loadProject) — identical to the first-run path.
      // Self-completing actions (build/deploy/publish call /complete internally)
      // need a reset first, else that coupled /complete 400s on an already-
      // `complete` stage; config/edit dialogs own their own completion (their
      // /complete is status-guarded), so they run WITHOUT a reset (a cancelled
      // dialog must not strand the stage at `pending`).
      if (stage.stage_id && !NON_LLM_CONFIG_STAGES.has(stage.stage_id)) {
        try {
          const wfParam = wfId ? `?workflow_id=${wfId}` : "";
          await api.post(`/api/projects/${projectId}/stages/${stage.stage_number}/reset${wfParam}`);
        } catch (err) {
          console.error(err);
          return;
        }
      }
      await nonLlmAction.action(stage);
      return;
    }
    // LLM stages: reset + WS run (unchanged) — the WS path emits stage_complete,
    // which clears runningStage in ProjectDetailPage.
    setActionLoading(stage.stage_number);
    try {
      const wfParam = wfId ? `?workflow_id=${wfId}` : "";
      await api.post(`/api/projects/${projectId}/stages/${stage.stage_number}/reset${wfParam}`);
      onRunStage(stage.stage_number, undefined, wfId);
    } catch (err) {
      console.error(err);
    }
    setActionLoading(null);
  };

  // Non-LLM stages that have direct actions
  const NON_LLM_ACTIONS: Record<string, { label: string; action: (stage: StageInfo) => Promise<void> }> = {
    odcs_specification: {
      label: "Edit Contract",
      action: async (stage) => {
        // Just select the stage — the ODCS editor tab opens automatically
        onStageClick(stage.stage_number);
      },
    },
    select_data_source: {
      label: "Configure",
      action: async (stage) => {
        await openDataSourceDialog(stage, "create");
      },
    },
    configure_serving: {
      label: "Configure",
      action: async (stage) => {
        setConfigureServingStage(stage);
      },
    },
    configure_transfer_placement: {
      label: "Configure",
      action: async (stage) => {
        setTransferPlacementStage(stage);
      },
    },
    configure_dq: {
      label: "Configure",
      action: async (stage) => {
        setConfigureDqStage(stage);
      },
    },
    odcs_to_dprod: {
      label: "Generate DPROD",
      action: async (stage) => {
        setActionLoading(stage.stage_number);
        try {
          await api.post(`/api/projects/${projectId}/odcs/generate-dprod`);
          await api.post(
            `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
            null,
            { params: { workflow_id: stage.workflow_id } }
          );
          onStageCompleted?.();
        } catch (e) {
          console.error(e);
        }
        setActionLoading(null);
      },
    },
    publish: {
      label: "Publish",
      action: async (stage) => {
        setActionLoading(stage.stage_number);
        try {
          await api.post(`/api/projects/${projectId}/odcs/publish`);
          await api.post(
            `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
            null,
            { params: { workflow_id: stage.workflow_id } }
          );
          onStageCompleted?.();
        } catch (e) {
          console.error(e);
        }
        setActionLoading(null);
      },
    },
    mark_engineering_complete: {
      label: "Mark Complete",
      action: async (stage) => {
        setActionLoading(stage.stage_number);
        try {
          // The backend's /stages/{n}/complete handler for mark_engineering_complete
          // completes the linked ProductRequest (idempotently) AND publishes the
          // contract before flipping the stage — so we just complete the stage.
          // This also works for projects with NO ProductRequest (created via the
          // raw API / MCP rather than the PO wizard), which the old
          // /product-requests/latest lookup 404'd on.
          await api.post(
            `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
            null,
            { params: { workflow_id: stage.workflow_id } }
          );
          onStageCompleted?.();
        } catch (e) {
          console.error(e);
          showError(e, { title: "Failed to mark complete" });
        }
        setActionLoading(null);
      },
    },
    deploy_virtual_view: {
      label: "Deploy view",
      action: async (stage) => {
        // First write-path to a source DB in the workbench. Require
        // explicit confirmation; the backend wraps everything in one
        // transaction and rolls back on any smoke-test failure, but
        // CREATE OR REPLACE VIEW is still surface area we want a
        // human gesture in front of.
        const ok = await confirm({
          title: "Deploy view to database",
          message:
            "This will execute CREATE OR REPLACE VIEW statements against the project's database. " +
            "All views are deployed atomically; a smoke-test SELECT runs against each before the transaction commits. Continue?",
          confirmLabel: "Deploy",
        });
        if (!ok) return;
        setActionLoading(stage.stage_number);
        try {
          const res = await api.post(`/api/projects/${projectId}/serving/deploy`, {});
          if (res.data?.status === "failed") {
            const cls = res.data?.error_class || "unknown";
            const msg = res.data?.error_message || "";
            throw new Error(`${cls}: ${msg}`);
          }
          await api.post(
            `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
            null,
            { params: { workflow_id: stage.workflow_id } }
          );
          onStageCompleted?.();
        } catch (e) {
          console.error(e);
          showError(e, { title: "Deploy failed" });
        }
        setActionLoading(null);
      },
    },
    // ── Serving BUILD stages (Configure → Build → Deploy) ─────────────────
    // Build assembles the runnable, downloadable package with NO live-DB
    // dependency; the coupled Deploy stage executes it. Because Build completes
    // without the DB, Download package / Push to Git appear as soon as it's done.
    serving_physical_copy: {
      label: "Build dbt Project",
      action: async (stage) => {
        // Scaffold + assemble the dbt project on disk (no `dbt build`). The
        // package is downloadable immediately; the build (with the verification
        // gate) runs in the coupled Deploy dbt (Materialize) stage.
        setActionLoading(stage.stage_number);
        try {
          await api.post(`/api/projects/${projectId}/serving/dbt/build-package`, {});
          await api.post(
            `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
            null, { params: { workflow_id: stage.workflow_id } },
          );
          onStageCompleted?.();
        } catch (e) {
          showError(e, { title: "dbt project build failed" });
        }
        setActionLoading(null);
      },
    },
    serving_lakehouse_export: {
      label: "Build Lakehouse Package",
      action: async (stage) => {
        // Assemble the runnable lakehouse package (models.json + run.py + README)
        // on disk — no live source needed. The export against the source runs in
        // the coupled Deploy Lakehouse (Run Export) stage.
        setActionLoading(stage.stage_number);
        try {
          await api.post(`/api/projects/${projectId}/serving/lakehouse/build`, {});
          await api.post(
            `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
            null, { params: { workflow_id: stage.workflow_id } },
          );
          onStageCompleted?.();
        } catch (e) {
          showError(e, { title: "Lakehouse package build failed" });
        }
        setActionLoading(null);
      },
    },
    // ── Serving DEPLOY stages ─────────────────────────────────────────────
    deploy_physical_copy: {
      label: "Materialize (dbt)",
      action: async (stage) => {
        // Materialized serving runs through the verification gate dialog:
        // sample build → inspect → approve full build. The dialog owns the
        // sample/full POSTs and the /complete call on approval.
        setMaterializeGate(stage);
      },
    },
    deploy_lakehouse: {
      label: "Run Export",
      action: async (stage) => {
        // Run the built lakehouse package against the live source → Parquet + a
        // DuckDB catalog. On failure the alert now shows the real reason (the
        // backend surfaces the runner's stderr tail).
        setActionLoading(stage.stage_number);
        try {
          await api.post(`/api/projects/${projectId}/serving/export`, { mode: "full" });
          await api.post(
            `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
            null, { params: { workflow_id: stage.workflow_id } },
          );
          onStageCompleted?.();
        } catch (e) {
          showError(e, { title: "Lakehouse export failed" });
        }
        setActionLoading(null);
      },
    },
    // Cross-platform transfer BUILD — assemble the runnable dlt package (no live
    // DB), just like the other Build stages. The extract→Parquet→load runs in the
    // coupled Run Transfer (deploy_transfer) stage.
    serving_transfer: {
      label: "Build Transfer Pipeline",
      action: async (stage) => {
        setActionLoading(stage.stage_number);
        try {
          await api.post(`/api/projects/${projectId}/serving/transfer/build`, {});
          await api.post(
            `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
            null, { params: { workflow_id: stage.workflow_id } },
          );
          onStageCompleted?.();
        } catch (e) {
          showError(e, { title: "Transfer pipeline build failed" });
        }
        setActionLoading(null);
      },
    },
    deploy_transfer: {
      label: "Run Transfer",
      action: async (stage) => {
        // Execute the built dlt package against the live source: extract each
        // dataset's shaped output → stage Parquet → load the DIFFERENT target
        // platform (dlt). Persists a transfer_then_transform :ServingDefinition.
        setActionLoading(stage.stage_number);
        try {
          await api.post(`/api/projects/${projectId}/serving/transfer`, {});
          await api.post(
            `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
            null, { params: { workflow_id: stage.workflow_id } },
          );
          onStageCompleted?.();
        } catch (e) {
          showError(e, { title: "Transfer failed" });
        }
        setActionLoading(null);
      },
    },
    // dmig (data migration) non-LLM stages. configure opens a dialog to pick the
    // target + strategy; run/reconcile POST to /migration/* then /complete
    // (mirrors the serving deploy pattern). The LLM stages (dmig_assess_plan /
    // dmig_generate_pipeline) run through the normal WS Run path.
    dmig_import_schema: {
      label: "Import schema",
      action: async (stage) => {
        // Deterministic seed of the confirmed physical schema into the graph.
        // Only mark complete when the load VERIFIED (partial → stays pending so
        // the engineer fixes the schema and retries).
        setActionLoading(stage.stage_number);
        try {
          const res = await api.post(`/api/projects/${projectId}/migration/seed-schema`, {});
          if (!res.data?.ok) {
            await notify({
              tone: "warning",
              title: "Schema import not verified",
              message: `Loaded ${res.data?.datasets_loaded ?? 0} of ${res.data?.expected_datasets ?? "?"} table(s). Confirm the physical schema above, then retry.`,
            });
          } else {
            await api.post(
              `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
              null, { params: { workflow_id: stage.workflow_id } },
            );
            onStageCompleted?.();
          }
        } catch (e) {
          showError(e, { title: "Schema import failed" });
        }
        setActionLoading(null);
      },
    },
    data_discovery_offline: {
      label: "Import metadata",
      action: async (stage) => {
        // Offline dpe-sa: upload the reviewed extraction manifest, then seed the
        // catalog + profiling deterministically. Complete only when the load
        // VERIFIED (partial → stays pending so the engineer re-uploads and retries).
        const input = document.createElement("input");
        input.type = "file";
        input.accept = ".yaml,.yml";
        input.onchange = async () => {
          const file = input.files?.[0];
          if (!file) return;
          setActionLoading(stage.stage_number);
          try {
            const fd = new FormData();
            fd.append("file", file);
            await api.post(`/api/projects/${projectId}/discovery/upload-manifest`, fd);
            const res = await api.post(`/api/projects/${projectId}/discovery/seed-offline`, {});
            if (!res.data?.ok) {
              await notify({
                tone: "warning",
                title: "Metadata import not verified",
                message: `Loaded ${res.data?.datasets_loaded ?? 0} of ${res.data?.expected_datasets ?? "?"} table(s). Re-upload a complete manifest and retry.`,
              });
            } else {
              await api.post(
                `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
                null, { params: { workflow_id: stage.workflow_id } },
              );
              onStageCompleted?.();
            }
          } catch (e) {
            showError(e, { title: "Metadata import failed" });
          }
          setActionLoading(null);
        };
        input.click();
      },
    },
    dmig_configure: {
      label: "Configure",
      action: async (stage) => {
        setConfigureMigrationStage(stage);
      },
    },
    dmig_execute_transfer: {
      label: "Run Migration",
      action: async (stage) => {
        const ok = await confirm({
          title: "Run the data migration",
          message:
            "This extracts every mapped table from the source and LOADS it into the target platform " +
            "(a real write to the target database). Per-dataset transfer manifests are written and the " +
            "run result is recorded. Continue?",
          confirmLabel: "Run Migration",
        });
        if (!ok) return;
        setActionLoading(stage.stage_number);
        try {
          const res = await api.post(`/api/projects/${projectId}/migration/snapshot`, {});
          if (res.data?.ok === false) {
            throw new Error(res.data?.error || "migration run failed");
          }
          await api.post(
            `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
            null, { params: { workflow_id: stage.workflow_id } },
          );
          onStageCompleted?.();
        } catch (e) {
          showError(e, { title: "Migration blocked" });
        }
        setActionLoading(null);
      },
    },
    dmig_reconcile: {
      label: "Reconcile",
      action: async (stage) => {
        // Read-only compare of source vs target row counts; records evidence on
        // the migration plan and advances it to 'reconciled' when all pass.
        setActionLoading(stage.stage_number);
        try {
          const res = await api.post(`/api/projects/${projectId}/migration/reconcile`, {});
          if (res.data?.ok && res.data?.all_pass === false) {
            console.warn("Reconciliation found mismatches:", res.data?.reconciliation);
          }
          await api.post(
            `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
            null, { params: { workflow_id: stage.workflow_id } },
          );
          onStageCompleted?.();
        } catch (e) {
          showError(e, { title: "Reconciliation blocked" });
        }
        setActionLoading(null);
      },
    },
    cmig_link: {
      label: "Link Migration",
      action: async (stage) => {
        setActionLoading(stage.stage_number);
        try {
          const el = await api.get(`/api/projects/${projectId}/code-migration/eligible-dmig`);
          const options: Array<{ project_code: string; name: string; status: string; eligible: boolean }> =
            el.data?.projects || [];
          const eligible = options.filter((o) => o.eligible);
          if (eligible.length === 0) {
            await notify({
              tone: "info",
              title: "Nothing to link",
              message: "No completed data-migration projects to link. Run a migration through Reconcile first.",
            });
            setActionLoading(null);
            return;
          }
          const listing = eligible.map((o) => `${o.project_code} (${o.name} — ${o.status})`).join("\n");
          const code = await prompt({
            title: "Link data-migration project",
            message: (
              <div style={{ whiteSpace: "pre-wrap" }}>
                {`Link to which data-migration project?\n\n${listing}`}
              </div>
            ),
            label: "Project code",
            defaultValue: eligible[0].project_code,
            confirmLabel: "Link",
          });
          if (!code) { setActionLoading(null); return; }
          await api.post(`/api/projects/${projectId}/code-migration/link`, { dmig_project_code: code.trim() });
          await api.post(`/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
            null, { params: { workflow_id: stage.workflow_id } });
          onStageCompleted?.();
        } catch (e) {
          showError(e, { title: "Link failed" });
        }
        setActionLoading(null);
      },
    },
    cmig_import_code: {
      label: "Import Code",
      action: async (stage) => {
        const input = document.createElement("input");
        input.type = "file";
        input.multiple = true;
        input.accept = ".sql,.py,.scala,.r,.java,.js,.ts,.hql,.sh,.txt,.ipynb,.pig,.ksh";
        input.onchange = async () => {
          if (!input.files || input.files.length === 0) return;
          setActionLoading(stage.stage_number);
          try {
            const fd = new FormData();
            Array.from(input.files).forEach((f) => fd.append("files", f));
            await api.post(`/api/projects/${projectId}/code-migration/import-code`, fd,
              { headers: { "Content-Type": "multipart/form-data" } });
            await api.post(`/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
              null, { params: { workflow_id: stage.workflow_id } });
            onStageCompleted?.();
          } catch (e) {
            showError(e, { title: "Import failed" });
          }
          setActionLoading(null);
        };
        input.click();
      },
    },
    cmig_configure: {
      label: "Configure",
      action: async (stage) => {
        const src = await prompt({
          title: "Configure code migration",
          label: "Source platform (e.g. mysql, postgres, teradata, oracle)",
          defaultValue: "mysql",
        });
        if (src === null) return;
        const srcVer = (await prompt({
          title: "Configure code migration",
          label: "Source platform version (optional, e.g. 16.20)",
          defaultValue: "",
        })) || "";
        const kind = (await prompt({
          title: "Configure code migration",
          label: "Target artifact kind (sql_script / pyspark_job / notebook)",
          defaultValue: "sql_script",
        })) || "sql_script";
        setActionLoading(stage.stage_number);
        try {
          await api.post(`/api/projects/${projectId}/code-migration/configure`, {
            source_platform: src.trim(),
            source_platform_version: srcVer.trim(),
            artifact_kind: kind.trim(),
            output_language: kind.trim() === "sql_script" ? "sql" : "python",
            // Pin the corpora used to ground the conversion (satisfies the
            // forward-engineering readiness guard).
            source_corpus: { platform: src.trim(), version: srcVer.trim() },
            target_corpus: { platform: "target" },
          });
          await api.post(`/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
            null, { params: { workflow_id: stage.workflow_id } });
          onStageCompleted?.();
        } catch (e) {
          showError(e, { title: "Configure failed" });
        }
        setActionLoading(null);
      },
    },
    cmig_package: {
      label: "Package",
      action: async (stage) => {
        setActionLoading(stage.stage_number);
        try {
          await api.post(`/api/projects/${projectId}/code-migration/package`, {});
          await api.post(`/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
            null, { params: { workflow_id: stage.workflow_id } });
          onStageCompleted?.();
        } catch (e) {
          showError(e, { title: "Package failed" });
        }
        setActionLoading(null);
      },
    },
    deployment_reflection: {
      label: "Run reflection",
      action: async (stage) => {
        // Read-only analysis: gather declared shape + sample preview rows,
        // run the deployment-reflector skill, persist :DeploymentReflection.
        // Can take 60-120s depending on advisor latency; the backend bounds
        // it at ADVISOR_TIMEOUT_SECONDS + 10s. No confirmation dialog —
        // this is purely a read operation.
        setActionLoading(stage.stage_number);
        try {
          const res = await api.post(`/api/projects/${projectId}/reflection/run`, {});
          if (res.data?.advisor_error) {
            // Warn but don't fail — the deterministic part persisted.
            console.warn("Reflector advisor error:", res.data.advisor_error);
          }
          await api.post(
            `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
            null,
            { params: { workflow_id: stage.workflow_id } }
          );
          onStageCompleted?.();
        } catch (e) {
          console.error(e);
          showError(e, { title: "Reflection failed" });
        }
        setActionLoading(null);
      },
    },
    // dpe-sa non-LLM stages. The backend's /stages/{n}/complete handler
    // recognises each stage_id and runs the side-effect (timestamp stamp,
    // ODCS synthesis, mapping write). Without these wirings the engineer's
    // Run button falls through to the LLM/WebSocket path, which hangs
    // forever on stages that have no skill or prompt.
    mark_discovery_complete: {
      label: "Mark Discovery Complete",
      action: async (stage) => {
        setActionLoading(stage.stage_number);
        try {
          await api.post(
            `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
            null,
            { params: { workflow_id: stage.workflow_id } }
          );
          onStageCompleted?.();
        } catch (e) {
          console.error(e);
          showError(e, { title: "Failed to mark discovery complete" });
        }
        setActionLoading(null);
      },
    },
    synthesize_odcs_from_graph: {
      label: "Synthesize ODCS",
      action: async (stage) => {
        setActionLoading(stage.stage_number);
        try {
          await api.post(
            `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
            null,
            { params: { workflow_id: stage.workflow_id } }
          );
          onStageCompleted?.();
        } catch (e) {
          console.error(e);
          showError(e, { title: "ODCS synthesis failed" });
        }
        setActionLoading(null);
      },
    },
    auto_mapping_sa: {
      label: "Auto-Map",
      action: async (stage) => {
        setActionLoading(stage.stage_number);
        try {
          await api.post(
            `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
            null,
            { params: { workflow_id: stage.workflow_id } }
          );
          onStageCompleted?.();
        } catch (e) {
          console.error(e);
          showError(e, { title: "Auto-mapping failed" });
        }
        setActionLoading(null);
      },
    },
  };

  const handleRunClick = async (e: React.MouseEvent, stage: StageInfo, wfId?: string) => {
    e.stopPropagation();
    const stageWfId = wfId || stage.workflow_id;
    const fields = stage.config_fields || [];
    const hasPlaybookSelect = fields.some((f) => f.type === "playbook_select");
    const regularFields = fields.filter((f) => f.type !== "playbook_select");

    if (hasPlaybookSelect) {
      setPlaybookStage(stage);
      setPlaybookWorkflow(stageWfId);
      return;
    }

    if (regularFields.length > 0) {
      setConfigOpen(stage.stage_number);
      setConfigWorkflow(stageWfId);
      setConfigValues({});
      setConfigOptions({});

      const hasSelectFields = regularFields.some((f) => f.type === "select" || f.type === "multiselect");
      if (hasSelectFields) {
        setOptionsLoading(true);
        try {
          const wfParam = stageWfId ? `?workflow_id=${stageWfId}` : "";
          const res = await api.get(`/api/projects/${projectId}/stages/${stage.stage_number}/config-options${wfParam}`);
          setConfigOptions(res.data);
          if (res.data.defaults) {
            setConfigValues((prev) => ({ ...normalizeConfigDefaults(res.data.defaults), ...prev }));
          }
        } catch {
          setConfigOptions({});
        }
        setOptionsLoading(false);
      }
    } else {
      onRunStage(stage.stage_number, undefined, stageWfId);
    }
  };

  const handlePlaybookSelect = async (version: string) => {
    if (!playbookStage) return;
    const stage = playbookStage;
    const wfId = playbookWorkflow;
    setPlaybookStage(null);
    setPlaybookWorkflow(undefined);

    const regularFields = (stage.config_fields || []).filter((f) => f.type !== "playbook_select");

    if (regularFields.length > 0) {
      setConfigOpen(stage.stage_number);
      setConfigWorkflow(wfId);
      setConfigValues({ playbook_version: version });
      setConfigOptions({});

      const hasSelectFields = regularFields.some((f) => f.type === "select" || f.type === "multiselect");
      if (hasSelectFields) {
        setOptionsLoading(true);
        try {
          const wfParam = wfId ? `?workflow_id=${wfId}` : "";
          const res = await api.get(`/api/projects/${projectId}/stages/${stage.stage_number}/config-options${wfParam}`);
          setConfigOptions(res.data);
          if (res.data.defaults) {
            setConfigValues((prev) => ({ ...normalizeConfigDefaults(res.data.defaults), ...prev }));
          }
        } catch {
          setConfigOptions({});
        }
        setOptionsLoading(false);
      }
    } else {
      onRunStage(stage.stage_number, { playbook_version: version }, wfId);
    }
  };

  const handleConfigSubmit = (stageNumber: number) => {
    const wfId = configWorkflow;
    setConfigOpen(null);
    setConfigWorkflow(undefined);
    onRunStage(stageNumber, configValues, wfId);
    setConfigValues({});
  };

  const toggleMultiselect = (key: string, value: string, filtersKey?: string) => {
    setConfigValues((prev) => {
      const current = prev[key] ? prev[key].split(", ") : [];
      const next = current.includes(value)
        ? current.filter((v) => v !== value)
        : [...current, value];
      const updated = { ...prev, [key]: next.join(", ") };
      // Clear the dependent field when filter selection changes
      if (filtersKey) updated[filtersKey] = "";
      return updated;
    });
  };

  const toggleSelectAll = (key: string, options: ConfigOption[], filtersKey?: string) => {
    setConfigValues((prev) => {
      const current = prev[key] ? prev[key].split(", ") : [];
      const allValues = options.map((o) => o.value);
      const allSelected = allValues.every((v) => current.includes(v));
      const updated = { ...prev, [key]: allSelected ? "" : allValues.join(", ") };
      if (filtersKey) updated[filtersKey] = "";
      return updated;
    });
  };

  const [collapsedWorkflows, setCollapsedWorkflows] = useState<Set<string>>(new Set());
  const [catalogOpen, setCatalogOpen] = useState(false);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [catalogItems, setCatalogItems] = useState<any[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(false);

  const toggleWorkflowCollapse = (wfId: string) => {
    setCollapsedWorkflows((prev) => {
      const next = new Set(prev);
      if (next.has(wfId)) next.delete(wfId);
      else next.add(wfId);
      return next;
    });
  };

  // Smart default (once, on first load): collapse phases whose every stage is
  // done so the board opens focused on what's actionable. After this, manual
  // toggles win — we don't re-collapse as the user works.
  const didInitCollapse = useRef(false);
  useEffect(() => {
    if (didInitCollapse.current) return;
    if (!multiWorkflow || !workflows || workflows.length === 0) return;
    didInitCollapse.current = true;
    const allStages = workflows.flatMap((wf) => wf.stages);
    const collapse = new Set<string>();
    for (const phase of CAPABILITY_PHASES) {
      const phaseStages = allStages.filter((s) => phaseOf(s.stage_id) === phase.key);
      const allDone = phaseStages.length > 0 && phaseStages.every(
        (s) => s.status === "complete" || s.status === "awaiting_review",
      );
      // Collapse fully-done phases, and the optional phase when nothing's added.
      if (allDone || (phase.optional && phaseStages.length === 0)) collapse.add(phase.key);
    }
    if (collapse.size > 0) setCollapsedWorkflows(collapse);
  }, [multiWorkflow, workflows]);

  // Fetch the workflow catalog up front so optional (not-yet-added) capabilities
  // can render as real cards on the board (no separate "Add Workflow" step).
  useEffect(() => {
    if (!multiWorkflow) return;
    setCatalogLoading(true);
    api.get(`/api/workflow-catalog?project_id=${projectId}`)
      .then((r) => setCatalogItems(r.data))
      .catch(() => { /* optional cards just won't have a target */ })
      .finally(() => setCatalogLoading(false));
  }, [multiWorkflow, projectId, workflows]);

  const addAndRun = async (workflowId: string) => {
    try {
      await api.post(`/api/projects/${projectId}/workflows`, { workflow_id: workflowId });
      onStageCompleted?.();
      onRunStage(1, {}, workflowId);
    } catch (err) {
      console.error(err);
    }
  };

  // The SINGLE recommended-next capability across the whole board: the first
  // pending/failed stage in the flattened (deduped) lifecycle order — identical
  // to the recommended-plan panel's "NEXT". Keyed by stage_number+workflow_id so
  // exactly one card highlights (not every workflow's first stage).
  let recommendedName: string | null = null;
  const recommendedKey: string | null = (() => {
    if (!multiWorkflow || !workflows) return null;
    const seen = new Set<string>();
    for (const wf of workflows) {
      for (const s of wf.stages) {
        const id = s.stage_id || `#${s.stage_number}`;
        if (seen.has(id)) continue;
        seen.add(id);
        if (s.status === "pending" || s.status === "failed") {
          recommendedName = s.stage_name;
          return `${s.stage_number}::${wf.workflow_id}`;
        }
      }
    }
    return null;
  })();

  const renderStageItem = (stage: StageInfo, stageList: StageInfo[], workflowId?: string) => {
    const isActive = activeStage === stage.stage_number && (!workflowId || activeWorkflow === workflowId);
    // Non-LLM stages are idempotent and the backend /complete endpoint
    // accepts pending OR running — so we surface the action button even on
    // a stuck "running" row, which lets the engineer self-recover from a
    // hung WebSocket without DB surgery.
    const isNonLlmAction = stage.stage_id ? Object.prototype.hasOwnProperty.call(NON_LLM_ACTIONS, stage.stage_id) : false;
    const statusAllowsRun =
      stage.status === "pending"
      || stage.status === "failed"
      || (isNonLlmAction && stage.status === "running");
    const prevStatus = stageList.find((s) => s.stage_number === stage.stage_number - 1)?.status;
    const prevComplete = stage.stage_number === 1 || prevStatus === "complete" || prevStatus === "awaiting_review";
    const roleAllowsRun = canRunStage(role, stage.stage_number, stage.stage_id) && !readOnly;
    const isRunning = runningStage === stage.stage_number && (!workflowId || runningWorkflow === workflowId);
    // SA: the head materialization stage stays amber-pending until the PO
    // closes the validation gate. Other materialization stages just look
    // pending — they're blocked on prior engineer stages, not on the PO.
    const blockedOnPo = (
      stage.status === "pending"
      && stage.stage_id === SA_BLOCKED_HEAD_STAGE
      && poGateStatus !== undefined
      && poGateStatus !== null
      && poGateStatus !== "complete"
    );

    const stageWfId = workflowId || stage.workflow_id;
    // The card title is always the registry stage name (stage_name) so it stays
    // consistent with the Recommended Plan and the stage detail header. For the
    // mode-selectable serving slot this means the per-mode registry name
    // ("Build Virtual View" / "Build dbt Project" / "Build Lakehouse Package");
    // the selected mode is also shown separately by the mode-toggle chip.
    const displayName = stage.stage_name;
    const isRecommended = recommendedKey != null && recommendedKey === `${stage.stage_number}::${stageWfId}`;
    // A completed stage always reads as "done" (green) — selection is shown as a
    // subtle ring, not by recolouring the whole card blue. Without this a
    // completed-but-active card competes visually with the recommended-next.
    const isDone = stage.status === "complete" || stage.status === "awaiting_review";
    // Freeform model: every completed / awaiting-review stage is re-runnable
    // (reset → run, graph data preserved), not only those whose workflow was
    // flagged `repeatable`. Re-running is the default, not an exception.
    const canRerun = (stage.status === "complete" || stage.status === "awaiting_review")
      && !isRunning && actionLoading !== stage.stage_number;
    const showRerunConfirm = pendingRerun?.stageNumber === stage.stage_number && pendingRerun?.workflowId === stageWfId;

    return (
      <div key={`${stageWfId || ""}-${stage.stage_number}`}>
        <div
          onClick={() => onStageClick(stage.stage_number, stageWfId)}
          onMouseEnter={(e) => {
            if (stage.description) {
              setHoveredStage({ stage, x: e.clientX, y: e.clientY });
            }
          }}
          onMouseMove={(e) => {
            // Track the cursor so the tooltip follows along — same UX as
            // the EdgeTooltip in MappingGraphView.tsx.
            if (hoveredStage?.stage.stage_number === stage.stage_number) {
              setHoveredStage({ stage, x: e.clientX, y: e.clientY });
            }
          }}
          onMouseLeave={() => setHoveredStage(null)}
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 6,
            minHeight: 64,
            padding: "10px 12px",
            borderRadius: 10,
            cursor: "pointer",
            backgroundColor: isDone ? "#f0fdf4"
              : isActive ? "#eff6ff"
              : blockedOnPo ? "#fffbeb"
              : "#fff",
            border: isDone ? "1px solid #bbf7d0"
              : (isActive || isRecommended) ? "2px solid #3b82f6"
              : blockedOnPo ? "1px solid #fde68a"
              : "1px solid #e2e8f0",
            // Active selection on a completed (green) card shows as a blue ring so it
            // doesn't recolour the card; recommended-next keeps its soft glow.
            boxShadow: (isActive && isDone) ? "0 0 0 2px rgba(59,130,246,0.45)"
              : (isRecommended && !isActive) ? "0 2px 12px rgba(59,130,246,0.18)"
              : undefined,
            transition: "all 0.15s",
          }}
        >
          {/* top: icon + name + role + discreet agentic marker (agentic only) */}
          <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
            <span style={{ fontSize: 15, minWidth: 20, textAlign: "center", opacity: 0.75 }}>
              {(stage.stage_id && STAGE_ICONS_BY_ID[stage.stage_id]) || STAGE_ICONS[stage.stage_number] || stage.stage_number}
            </span>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div style={{ fontWeight: 600, fontSize: 13.5, lineHeight: 1.2 }}>{displayName}</div>
              {stage.owner_role && (
                <div style={{ fontSize: 11, color: "#94a3b8" }}>{stage.owner_role}</div>
              )}
            </div>
            {stage.agentic && (
              <span
                title="Agentic — runs the Claude Agent SDK / makes LLM calls"
                aria-label="Agentic"
                style={{
                  flexShrink: 0, display: "inline-flex", alignItems: "center", justifyContent: "center",
                  width: 18, height: 18, fontSize: 10, borderRadius: 999,
                  background: "#f5f3ff", color: "#7c3aed", border: "1px solid #ddd6fe",
                }}
              >
                ✦
              </span>
            )}
          </div>
          {/* body: what this capability does (the registered one-liner) */}
          {stage.description && (
            <div
              style={{
                fontSize: 11.5, color: "#64748b", lineHeight: 1.4,
                display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical",
                overflow: "hidden",
              }}
            >
              {stage.description}
            </div>
          )}
          {/* foot: status pill + actions */}
          <div style={{ marginTop: "auto", display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
            <StageChip stage={stage} displayStatus={blockedOnPo ? "blocked_on_po" : undefined} />
            {/* DQ test-gen stages show which framework is active as a read-only chip.
                The framework is CHOSEN in the Configure DQ dialog (not re-picked here) —
                the Build stage just generates the selected framework's package. Mirrors
                Configure Serving → Data Serving. */}
            {(stage.stage_id === "dq_test_generation_gx" || stage.stage_id === "dq_test_generation_python") && (() => {
              const isGx = stage.stage_id === "dq_test_generation_gx";
              return (
                <span style={{
                  fontSize: 10, fontWeight: 700, padding: "2px 8px", borderRadius: 4,
                  backgroundColor: "#eef2ff", color: "#4338ca", border: "1px solid #ddd6fe",
                }} title="Framework is chosen in the Configure DQ step">
                  {isGx ? "Great Expectations" : "Pure Python (Pandera)"}
                </span>
              );
            })()}
            {/* Configure Serving completed — show which mode is active as a read-only chip */}
            {stage.stage_id === "configure_serving" && stage.status === "complete" && (() => {
              const activeServing = stageList.find(
                (s) => s.stage_id === "serving_virtual_view" || s.stage_id === "serving_physical_copy" || s.stage_id === "serving_lakehouse_export" || s.stage_id === "serving_transfer"
              );
              const mode = activeServing?.stage_id === "serving_physical_copy"
                ? "materialized"
                : activeServing?.stage_id === "serving_lakehouse_export"
                ? "lakehouse"
                : activeServing?.stage_id === "serving_transfer"
                ? "transfer"
                : "virtual";
              const chipStyle = {
                materialized: { bg: "#f5f3ff", fg: "#7c3aed", bd: "#e9d5ff", label: "Materialized (dbt)" },
                lakehouse:   { bg: "#ecfeff", fg: "#0e7490", bd: "#a5f3fc", label: "Lakehouse (DuckDB)" },
                transfer:    { bg: "#fef2f2", fg: "#b91c1c", bd: "#fecaca", label: "Cross-platform transfer" },
                virtual:     { bg: "#eff6ff", fg: "#1d4ed8", bd: "#bfdbfe", label: "Virtual (SQL view)" },
              }[mode];
              return (
                <span style={{
                  fontSize: 10, fontWeight: 700, padding: "2px 8px", borderRadius: 4,
                  backgroundColor: chipStyle.bg, color: chipStyle.fg,
                  border: `1px solid ${chipStyle.bd}`,
                }}>
                  {chipStyle.label}
                </span>
              );
            })()}
            {isRecommended && statusAllowsRun && !isRunning && actionLoading !== stage.stage_number && (
              <span
                title="Recommended next step — you're free to run any step in any order."
                style={{
                  fontSize: 9, fontWeight: 800, color: "#fff",
                  backgroundColor: "#3b82f6", borderRadius: 4, padding: "2px 6px",
                  letterSpacing: 0.3,
                }}
              >
                RECOMMENDED
              </span>
            )}
            {statusAllowsRun && !isRunning && actionLoading !== stage.stage_number && (() => {
              const nonLlmAction = stage.stage_id ? NON_LLM_ACTIONS[stage.stage_id] : undefined;
              const buttonLabel = nonLlmAction ? nonLlmAction.label : "Run";
              // prevComplete is no longer a hard gate — any stage runs in any
              // order (prevComplete now drives the "Recommended" hint instead).
              // blockedOnPo stays: the PO validation gate is a real dependency.
              const canRun = roleAllowsRun && !blockedOnPo;
              // Non-recommended-task guard: warn (Accept/Cancel) before running a
              // stage that isn't the recommended next step, naming the recommended
              // step and WHY this one is ahead of order. Mirrors the CLI rule in
              // the workbench-guide skill. Running the recommended step (or when
              // there's no recommendation) is silent.
              const prevName = stageList.find((s) => s.stage_number === stage.stage_number - 1)?.stage_name;
              const depOk = async () => {
                if (isRecommended || !recommendedKey) return true;
                const why = !prevComplete
                  ? `earlier step "${prevName || "a prerequisite"}" isn't complete yet, so the result may be thin or empty`
                  : `it's ahead of the recommended order`;
                return confirm({
                  title: `Run "${stage.stage_name}" out of order?`,
                  message: (
                    <>
                      "{stage.stage_name}" isn't the recommended next step
                      {recommendedName ? <> — that's "{recommendedName}".</> : "."}
                      <br /><br />
                      Reason: {why}.
                    </>
                  ),
                  confirmLabel: "Run anyway",
                });
              };
              const handleClick = async (e: React.MouseEvent) => {
                e.stopPropagation();
                if (!canRun) return;
                if (!(await depOk())) return;
                if (nonLlmAction) nonLlmAction.action(stage);
                else handleRunClick(e, stage, workflowId);
              };
              const tooltip = blockedOnPo
                ? (requestNotAccepted
                    ? "Request not accepted yet — accept it first (banner above) to unlock PO validation"
                    : "Waiting for the Product Owner to finish validation")
                : !roleAllowsRun ? `Requires ${stage.owner_role} role`
                : !prevComplete ? "Ahead of the recommended order — you can run it now, but earlier steps haven't completed, so the result may be thin."
                : undefined;
              return (
                <button
                  onClick={handleClick}
                  disabled={!canRun}
                  title={tooltip}
                  style={{
                    padding: "4px 12px",
                    borderRadius: 6,
                    border: "none",
                    backgroundColor: canRun ? "#3b82f6" : "#e2e8f0",
                    color: canRun ? "#fff" : "#94a3b8",
                    fontSize: 12,
                    fontWeight: 600,
                    cursor: canRun ? "pointer" : "not-allowed",
                    opacity: canRun ? 1 : 0.7,
                  }}
                >
                  {buttonLabel === "Run" ? "▶ Run" : buttonLabel}
                </button>
              );
            })()}
            {canRerun && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  if (!roleAllowsRun) return;
                  setPendingRerun({ stageNumber: stage.stage_number, workflowId: stageWfId });
                }}
                disabled={!roleAllowsRun}
                aria-label="Rerun stage"
                title={!roleAllowsRun ? `Requires ${stage.owner_role} role` : "Rerun this stage — graph data is preserved"}
                style={{
                  padding: "4px 10px",
                  borderRadius: 6,
                  border: "1px solid #cbd5e1",
                  backgroundColor: "#fff",
                  color: roleAllowsRun ? "#475569" : "#94a3b8",
                  fontSize: 12,
                  fontWeight: 600,
                  cursor: roleAllowsRun ? "pointer" : "not-allowed",
                }}
              >
                ↻ Re-run
              </button>
            )}
            {/* Edit affordance for completed select_data_source rows so a
                mistyped credential can be fixed without rerunning the
                whole workflow. The connection is the only durable artifact
                — we PUT-only on submit and skip the (already done) complete. */}
            {stage.stage_id === "select_data_source" && stage.status === "complete" && !isRunning && actionLoading !== stage.stage_number && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  if (!roleAllowsRun) return;
                  void openDataSourceDialog(stage, "edit");
                }}
                disabled={!roleAllowsRun}
                aria-label="Edit data source"
                title={!roleAllowsRun ? `Requires ${stage.owner_role} role` : "Edit the saved connection"}
                style={{
                  padding: "4px 10px",
                  borderRadius: 6,
                  border: "1px solid #cbd5e1",
                  backgroundColor: "#fff",
                  color: roleAllowsRun ? "#475569" : "#94a3b8",
                  fontSize: 12,
                  fontWeight: 600,
                  cursor: roleAllowsRun ? "pointer" : "not-allowed",
                }}
              >
                Edit
              </button>
            )}
            {/* Reconfigure button for configure_serving when complete */}
            {stage.stage_id === "configure_serving" && stage.status === "complete" && !isRunning && actionLoading !== stage.stage_number && (
              <button
                onClick={(e) => { e.stopPropagation(); if (roleAllowsRun) setConfigureServingStage(stage); }}
                disabled={!roleAllowsRun}
                title={!roleAllowsRun ? `Requires ${stage.owner_role} role` : "Adjust serving strategy or target dialect"}
                style={{
                  padding: "4px 10px", borderRadius: 6, border: "1px solid #cbd5e1",
                  backgroundColor: "#fff", color: roleAllowsRun ? "#475569" : "#94a3b8",
                  fontSize: 12, fontWeight: 600, cursor: roleAllowsRun ? "pointer" : "not-allowed",
                }}
              >
                Reconfigure
              </button>
            )}
            {/* Reconfigure button for configure_dq when complete (change framework). */}
            {stage.stage_id === "configure_dq" && stage.status === "complete" && !isRunning && actionLoading !== stage.stage_number && (
              <button
                onClick={(e) => { e.stopPropagation(); if (roleAllowsRun) setConfigureDqStage(stage); }}
                disabled={!roleAllowsRun}
                title={!roleAllowsRun ? `Requires ${stage.owner_role} role` : "Change the DQ test framework"}
                style={{
                  padding: "4px 10px", borderRadius: 6, border: "1px solid #cbd5e1",
                  backgroundColor: "#fff", color: roleAllowsRun ? "#475569" : "#94a3b8",
                  fontSize: 12, fontWeight: 600, cursor: roleAllowsRun ? "pointer" : "not-allowed",
                }}
              >
                Reconfigure
              </button>
            )}
            {/* Download the self-contained serving package for a completed
                serving stage — the same runnable artifact DWB executed. */}
            {SERVING_PACKAGE_ENDPOINTS[stage.stage_id] && (stage.status === "complete" || stage.status === "awaiting_review") && !isRunning && actionLoading !== stage.stage_number && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  const suffix = SERVING_PACKAGE_ENDPOINTS[stage.stage_id];
                  const url = MIGRATION_PACKAGE_STAGES.has(stage.stage_id)
                    ? `/api/projects/${projectId}/migration/package?format=zip`
                    : `/api/projects/${projectId}/serving/${suffix}?format=zip`;
                  void downloadBlobZip(url, `${projectId}-${suffix}.zip`).catch(() => {});
                }}
                title="Download the runnable serving package (README, run script, requirements)"
                style={{
                  padding: "4px 10px", borderRadius: 6, border: "1px solid #cbd5e1",
                  backgroundColor: "#fff", color: "#475569",
                  fontSize: 12, fontWeight: 600, cursor: "pointer",
                }}
              >
                ⤓ Download package
              </button>
            )}
            {/* Download the generated DQ test package (test code + README, no results). */}
            {stage.stage_id && DQ_PACKAGE_STAGES.has(stage.stage_id) && stage.status === "complete" && !isRunning && actionLoading !== stage.stage_number && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  // product_dq_testing carries the dprod suite (deployed-product
                  // views vs contract rules); every other DQ workflow is catalog.
                  // Threading source_mode keeps an SA product's two suites — which
                  // land in separate on-disk dirs — from clobbering each other.
                  const dqMode = stageWfId === "product_dq_testing" ? "dprod" : "catalog";
                  const suffix = dqMode === "dprod" ? "dq-tests-dprod" : "dq-tests";
                  const url = `/api/projects/${projectId}/dq-package?format=zip&source_mode=${dqMode}`;
                  void downloadBlobZip(url, `${projectId}-${suffix}.zip`).catch(() => {});
                }}
                title="Download the generated DQ test package (test code + README)"
                style={{
                  padding: "4px 10px", borderRadius: 6, border: "1px solid #cbd5e1",
                  backgroundColor: "#fff", color: "#475569",
                  fontSize: 12, fontWeight: 600, cursor: "pointer",
                }}
              >
                ⤓ Download DQ Package
              </button>
            )}
            {/* Push the product's artifacts (serving package + DQ test package +
                OKF docs + ODCS) to its git repository. Shown on serving AND DQ
                package stages — collect_for_git bundles whatever exists on disk,
                so a push from either stage carries the full repo. Git-configured only. */}
            {(SERVING_PACKAGE_ENDPOINTS[stage.stage_id] || (stage.stage_id && DQ_PACKAGE_STAGES.has(stage.stage_id))) && (stage.status === "complete" || stage.status === "awaiting_review") && !isRunning && actionLoading !== stage.stage_number && gitCfg.configured && (
              <>
                <button
                  onClick={(e) => { e.stopPropagation(); if (!gitPushing) void handlePushToGit(); }}
                  disabled={gitPushing}
                  title="Push this product's artifacts (serving package, DQ tests, docs, ODCS spec) to its git repository"
                  style={{
                    padding: "4px 10px", borderRadius: 6, border: "1px solid #cbd5e1",
                    backgroundColor: "#fff", color: "#475569",
                    fontSize: 12, fontWeight: 600, cursor: gitPushing ? "default" : "pointer",
                  }}
                >
                  {gitPushing ? "Pushing…" : gitCfg.repo_url ? "↑ Re-push to Git" : "↑ Push to Git"}
                </button>
                {gitCfg.repo_url && (
                  <a
                    href={gitCfg.repo_url}
                    target="_blank"
                    rel="noreferrer"
                    onClick={(e) => e.stopPropagation()}
                    style={{ fontSize: 12, fontWeight: 600, color: "#7c3aed", textDecoration: "none", alignSelf: "center" }}
                  >
                    View in Git →
                  </a>
                )}
                {gitPushErr && <span style={{ fontSize: 12, color: "#ef4444" }}>{gitPushErr}</span>}
              </>
            )}
            {/* Publish the product's DATA artifacts (Parquet + manifests) to its
                bound object store (ADR-14). Serving stages only; shown when a
                target is bound (configure it on the dashboard serving card).
                Excluded for serving_transfer: that stage only builds code — the
                actual Parquet goes to S3 natively during Run Transfer (Phase 5). */}
            {SERVING_PACKAGE_ENDPOINTS[stage.stage_id] && stage.stage_id !== "serving_transfer" && (stage.status === "complete" || stage.status === "awaiting_review") && !isRunning && actionLoading !== stage.stage_number && storageCfg.configured && (
              <>
                <button
                  onClick={(e) => { e.stopPropagation(); if (!storagePublishing) void handlePublishToStorage(); }}
                  disabled={storagePublishing}
                  title="Publish this product's data artifacts (Parquet + manifests) to its object store"
                  style={{
                    padding: "4px 10px", borderRadius: 6, border: "1px solid #cbd5e1",
                    backgroundColor: "#fff", color: "#0f766e",
                    fontSize: 12, fontWeight: 600, cursor: storagePublishing ? "default" : "pointer",
                  }}
                >
                  {storagePublishing ? "Publishing…" : "↑ Publish to Object Store"}
                </button>
                {storageMsg && <span style={{ fontSize: 12, color: "#0f766e" }}>{storageMsg}</span>}
                {storageErr && <span style={{ fontSize: 12, color: "#ef4444" }}>{storageErr}</span>}
              </>
            )}
            {actionLoading === stage.stage_number && (
              <span style={{ fontSize: 12, color: "#f59e0b", fontWeight: 600 }}>Processing...</span>
            )}
            {isRunning && (
              <span style={{ fontSize: 12, color: "#f59e0b", fontWeight: 600 }}>Running...</span>
            )}
            {stage.status === "failed" && stage.has_review && (
              <button
                onClick={async (e) => {
                  e.stopPropagation();
                  try {
                    await api.post(`/api/projects/${projectId}/stages/${stage.stage_number}/recover${stageWfId ? `?workflow_id=${stageWfId}` : ""}`);
                    onStageCompleted?.();
                  } catch { /* ignore */ }
                }}
                style={{
                  padding: "4px 10px", borderRadius: 6, border: "1px solid #f59e0b",
                  backgroundColor: "#fef3c7", color: "#92400e",
                  fontSize: 11, fontWeight: 600, cursor: "pointer",
                }}
                title="Check for pending reviews and recover this stage"
              >
                Recover
              </button>
            )}
          </div>
        </div>
        {/* Inline Rerun confirmation */}
        {showRerunConfirm && (
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              padding: "12px 16px",
              backgroundColor: "#f8fafc",
              border: "1px solid #e2e8f0",
              borderTop: "none",
              borderRadius: "0 0 8px 8px",
              display: "flex",
              flexDirection: "column",
              gap: 10,
            }}
          >
            <div style={{ fontSize: 13, color: "#334155" }}>
              Rerun <strong>{stage.stage_name}</strong>? Existing graph data stays — the stage appends a new batch or skips already-written items depending on the skill.
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <button
                onClick={() => handleRerunConfirm(stage, stageWfId)}
                style={{
                  padding: "5px 14px", borderRadius: 6, border: "none",
                  backgroundColor: "#3b82f6", color: "#fff",
                  fontSize: 12, fontWeight: 600, cursor: "pointer",
                }}
              >
                Rerun
              </button>
              <button
                onClick={() => setPendingRerun(null)}
                style={{
                  padding: "5px 14px", borderRadius: 6,
                  border: "1px solid #cbd5e1", backgroundColor: "#fff",
                  color: "#475569", fontSize: 12, fontWeight: 600, cursor: "pointer",
                }}
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>
    );
  };

  // In multi-workflow projects, stage_number is per-workflow (not globally
  // unique) — so a lookup by stage_number alone returns whichever workflow's
  // stage with that number we hit first, which can pull in the wrong stage
  // (e.g. opening the Mapping config modal but finding ODCS to DPROD with
  // the same number). Prefer matches that also share the supplied
  // workflowId; fall back to the bare-number lookup only when no workflow
  // context is provided (legacy flat workflows).
  const findStageByNumber = (stageNumber: number, workflowId?: string): StageInfo | null => {
    if (workflowId && workflows) {
      const wf = workflows.find((w) => w.workflow_id === workflowId);
      if (wf) {
        const match = wf.stages.find((s) => s.stage_number === stageNumber);
        if (match) return match;
      }
    }
    for (const s of stages) {
      if (s.stage_number === stageNumber && (!workflowId || s.workflow_id === workflowId)) {
        return s;
      }
    }
    if (workflows) {
      for (const wf of workflows) {
        if (workflowId && wf.workflow_id !== workflowId) continue;
        for (const s of wf.stages) {
          if (s.stage_number === stageNumber) return s;
        }
      }
    }
    return null;
  };

  const closeConfigDialog = () => {
    setConfigOpen(null);
    setConfigWorkflow(undefined);
    setConfigValues({});
    setSourceCandidatesRequestSent(false);
  };

  const renderConfigDialog = () => {
    if (configOpen === null) return null;
    const stage = findStageByNumber(configOpen, configWorkflow);
    if (!stage) return null;
    const fields = (stage.config_fields || []).filter((f) => f.type !== "playbook_select");
    const stageNumber = stage.stage_number;
    const submitDisabled = fields.some((f) => f.required && !String(configValues[f.key] ?? "").trim());

    return (
      <div
        onClick={closeConfigDialog}
        style={{
          position: "fixed",
          inset: 0,
          backgroundColor: "rgba(15, 23, 42, 0.55)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          zIndex: 100,
        }}
      >
        <div
          onClick={(e) => e.stopPropagation()}
          style={{
            width: 560,
            maxWidth: "calc(100vw - 32px)",
            maxHeight: "calc(100vh - 32px)",
            overflowY: "auto",
            backgroundColor: "#fff",
            borderRadius: 12,
            padding: 20,
            boxShadow: "0 20px 40px rgba(15, 23, 42, 0.25)",
            display: "flex",
            flexDirection: "column",
            gap: 14,
          }}
        >
          <div>
            <div style={{ fontSize: 16, fontWeight: 700, color: "#0f172a" }}>
              Configure: {stage.stage_name}
            </div>
            {stage.owner_role && (
              <div style={{ fontSize: 12, color: "#64748b", marginTop: 4 }}>
                {stage.owner_role}
              </div>
            )}
          </div>

          {optionsLoading ? (
            <div style={{ color: "#94a3b8", fontSize: 13 }}>Loading options...</div>
          ) : (
            fields.map((field) => {
              let options = field.options_key ? configOptions[field.options_key] || [] : [];

              const filterField = fields.find((f) => f.filters_key === field.key);
              if (filterField) {
                const filterValues = configValues[filterField.key]
                  ? configValues[filterField.key].split(", ").filter(Boolean)
                  : [];
                if (filterValues.length > 0) {
                  options = options.filter((o) =>
                    filterValues.includes((o as { description?: string }).description || "")
                  );
                }
              }

              if (field.type === "select") {
                return (
                  <div key={field.key}>
                    <label style={labelStyle}>
                      {field.label}{field.required && " *"}
                    </label>
                    {options.length === 0 ? (
                      <div style={{ fontSize: 12, color: "#ef4444" }}>No options available in graph</div>
                    ) : (
                      <select
                        value={configValues[field.key] || ""}
                        onChange={(e) => setConfigValues((v) => ({ ...v, [field.key]: e.target.value }))}
                        style={inputStyle}
                      >
                        <option value="">Select...</option>
                        {options.map((opt) => (
                          <option key={opt.value} value={opt.value}>{opt.label}</option>
                        ))}
                      </select>
                    )}
                  </div>
                );
              }

              if (field.type === "multiselect") {
                const selected = configValues[field.key] ? configValues[field.key].split(", ").filter(Boolean) : [];
                const allSelected = options.length > 0 && options.every((o) => selected.includes(o.value));
                return (
                  <div key={field.key}>
                    <label style={labelStyle}>
                      {field.label}{field.required && " *"}
                      {options.length > 0 && (
                        <>
                          <span
                            onClick={() => toggleSelectAll(field.key, options, field.filters_key)}
                            style={{
                              marginLeft: 12,
                              fontSize: 11,
                              color: "#3b82f6",
                              cursor: "pointer",
                              fontWeight: 500,
                            }}
                          >
                            {allSelected ? "Deselect all" : "Select all"}
                          </span>
                          {selected.length > 0 && (
                            <span style={{ marginLeft: 8, fontSize: 11, color: "#94a3b8" }}>
                              ({selected.length} selected)
                            </span>
                          )}
                        </>
                      )}
                    </label>
                    {options.length === 0 ? (
                      stage.stage_id === "data_mapping" && field.options_key === "source_tables" ? (
                        // Consumer-aligned mapping with no :CONSUMES'd source
                        // datasets — surface a clear engineer→PO escalation
                        // instead of a bare "No options" error. Identifying
                        // candidate source-aligned data products is PO work.
                        <div
                          style={{
                            padding: 14,
                            borderRadius: 8,
                            backgroundColor: "#fef3c7",
                            border: "1px solid #fde68a",
                            color: "#854d0e",
                            fontSize: 13,
                            display: "flex",
                            flexDirection: "column",
                            gap: 10,
                          }}
                        >
                          <div>
                            The PO hasn't specified any candidate source-aligned
                            data products for this consumer yet. Identifying
                            source candidates is the PO's responsibility —
                            request them so mapping can proceed.
                          </div>
                          <button
                            type="button"
                            disabled={sourceCandidatesRequestSent}
                            onClick={() =>
                              void requestSourceCandidatesFromPo(
                                "Engineer cannot start data_mapping — the source-tables picker is empty.",
                              )
                            }
                            style={{
                              alignSelf: "flex-start",
                              padding: "7px 14px",
                              borderRadius: 6,
                              backgroundColor: sourceCandidatesRequestSent ? "#cbd5e1" : "#0f172a",
                              color: "#fff",
                              border: "none",
                              fontSize: 12,
                              fontWeight: 700,
                              cursor: sourceCandidatesRequestSent ? "default" : "pointer",
                            }}
                          >
                            {sourceCandidatesRequestSent
                              ? "✓ Request sent — waiting for PO"
                              : "Request candidates from PO →"}
                          </button>
                        </div>
                      ) : (
                        <div style={{ fontSize: 12, color: filterField ? "#94a3b8" : "#ef4444" }}>
                          {filterField ? "Select a filter above to see options" : "No options available"}
                        </div>
                      )
                    ) : (
                      <div style={{
                        display: "flex", flexDirection: "column", gap: 4,
                        maxHeight: 240, overflowY: "auto",
                        padding: "6px 10px",
                        border: "1px solid #cbd5e1",
                        borderRadius: 5,
                        backgroundColor: "#fff",
                      }}>
                        {options.map((opt) => (
                          <label
                            key={opt.value}
                            style={{
                              display: "flex", alignItems: "center", gap: 8,
                              fontSize: 13, cursor: "pointer",
                              padding: "3px 0",
                            }}
                          >
                            <input
                              type="checkbox"
                              checked={selected.includes(opt.value)}
                              onChange={() => toggleMultiselect(field.key, opt.value, field.filters_key)}
                            />
                            <span>{opt.label}</span>
                            {(opt as { description?: string }).description && (
                              <span style={{ fontSize: 11, color: "#94a3b8" }}>
                                ({(opt as { description?: string }).description})
                              </span>
                            )}
                          </label>
                        ))}
                      </div>
                    )}
                  </div>
                );
              }

              return (
                <div key={field.key}>
                  <label style={labelStyle}>
                    {field.label}{field.required && " *"}
                  </label>
                  <input
                    value={configValues[field.key] || ""}
                    onChange={(e) => setConfigValues((v) => ({ ...v, [field.key]: e.target.value }))}
                    placeholder={field.placeholder}
                    style={inputStyle}
                  />
                </div>
              );
            })
          )}

          {!optionsLoading && (
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 4 }}>
              <button
                type="button"
                onClick={closeConfigDialog}
                style={{
                  padding: "8px 16px",
                  borderRadius: 6,
                  backgroundColor: "#fff",
                  color: "#334155",
                  border: "1px solid #cbd5e1",
                  fontSize: 13,
                  fontWeight: 600,
                  cursor: "pointer",
                }}
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => handleConfigSubmit(stageNumber)}
                disabled={submitDisabled}
                style={{
                  padding: "8px 16px",
                  borderRadius: 6,
                  backgroundColor: submitDisabled ? "#cbd5e1" : "#3b82f6",
                  color: "#fff",
                  border: "none",
                  fontSize: 13,
                  fontWeight: 700,
                  cursor: submitDisabled ? "not-allowed" : "pointer",
                }}
              >
                Run Stage
              </button>
            </div>
          )}
        </div>
      </div>
    );
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", margin: "0 0 12px" }}>
        <h3 style={{ margin: 0, color: "#334155" }}>
          {multiWorkflow ? "Capabilities" : "Pipeline Stages"}
        </h3>
        {multiWorkflow && (
          <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "#64748b", cursor: "pointer", userSelect: "none" }}
                 title="Show every capability by category, including ones not in this project's plan.">
            <input type="checkbox" checked={showAllCaps} onChange={(e) => setShowAllCaps(e.target.checked)}
                   style={{ cursor: "pointer" }} />
            Show all capabilities
          </label>
        )}
      </div>

      {/* Playbook selector modal */}
      {playbookStage && (
        <PlaybookSelector
          projectId={projectId}
          stageNumber={playbookStage.stage_number}
          stageName={playbookStage.stage_name}
          onSelect={handlePlaybookSelect}
          onCancel={() => setPlaybookStage(null)}
        />
      )}

      {/* Capability board — every stage grouped into a broad phase */}
      {multiWorkflow && workflows && (() => {
        const wfStages = new Map<string, StageInfo[]>();
        const allStages: StageInfo[] = [];
        workflows.forEach((wf) => { wfStages.set(wf.workflow_id, wf.stages); wf.stages.forEach((s) => allStages.push(s)); });
        const hasStage = (ids: string[]) => allStages.some((s) => s.stage_id && ids.includes(s.stage_id));
        // Full palette: every addable capability shows as a card in its phase,
        // so the engineer sees ALL available capabilities regardless of
        // archetype — the plan highlights which to use. Catalog workflows
        // bundle several stages (e.g. "Data Discovery" = select_data_source +
        // data_discovery + data_profiling); we EXPAND each not-yet-added
        // workflow into its individual stages so the palette shows the same
        // capability granularity as already-added stages (one card per
        // capability, not one per bundled workflow). Skip PO-only stages and
        // stages already on the board; dedupe by stage_id across workflows.
        // Running any expanded card adds its parent workflow.
        const PO_ONLY_STAGES = new Set(["initiate", "odcs_specification", "publish", "po_source_validation"]);
        const seenOptional = new Set<string>();
        const optionalCaps: { stageId: string; workflowId: string; name: string; role?: string; description?: string; phaseKey: string }[] = [];
        catalogItems
          .filter((it: { already_added?: boolean }) => !it.already_added)
          .forEach((it: { workflow_id: string; stages?: { stage_id: string; enabled?: boolean; name?: string; description?: string; owner_role?: string }[] }) => {
            (it.stages || []).filter((s) => s.enabled !== false).forEach((s) => {
              const sid = s.stage_id;
              if (!sid || PO_ONLY_STAGES.has(sid) || hasStage([sid]) || seenOptional.has(sid)) return;
              seenOptional.add(sid);
              optionalCaps.push({
                stageId: sid,
                workflowId: it.workflow_id,
                name: s.name || STAGE_REGISTRY_NAMES[sid] || sid,
                role: s.owner_role,
                description: s.description,
                phaseKey: phaseOf(sid),
              });
            });
          });
        return CAPABILITY_PHASES.map((phase, idx) => {
          // Dedupe by stage_id: a project can have overlapping workflows that
          // repeat the same capability (e.g. two workflows each with
          // serving_virtual_view). One capability = one card on the board.
          const seenIds = new Set<string>();
          const phaseStages = allStages.filter((s) => {
            if (phaseOf(s.stage_id) !== phase.key) return false;
            const key = s.stage_id || `#${s.stage_number}`;
            if (seenIds.has(key)) return false;
            seenIds.add(key);
            return true;
          });
          // Plan-only (default): drop the addable "optional" cards + hide any
          // phase that has no real plan stages. Show-all: full palette.
          const optionalCards = showAllCaps ? optionalCaps.filter((o) => o.phaseKey === phase.key) : [];
          if (phaseStages.length === 0 && optionalCards.length === 0) {
            if (!showAllCaps || !phase.optional) return null;
          }
          const isCollapsed = collapsedWorkflows.has(phase.key);
          const done = phaseStages.filter((s) => s.status === "complete" || s.status === "awaiting_review").length;
          const total = phaseStages.length;
          const allDone = total > 0 && done === total;
          return (
            <div key={phase.key} style={{ marginBottom: 16 }}>
              <div
                onClick={() => toggleWorkflowCollapse(phase.key)}
                style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "6px 2px", cursor: "pointer", borderBottom: "1px solid #eef2f7", userSelect: "none" }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
                  <span style={{ fontSize: 10, color: "#94a3b8", transition: "transform 0.15s", transform: isCollapsed ? "rotate(-90deg)" : "rotate(0)" }}>&#9660;</span>
                  <span style={{ fontWeight: 700, fontSize: 11.5, letterSpacing: 0.5, textTransform: "uppercase", color: "#64748b" }}>{`${idx + 1}. ${phase.label}`}</span>
                  {phase.optional && (
                    <span style={{ fontSize: 9, fontWeight: 700, color: "#64748b", background: "#f1f5f9", border: "1px solid #e2e8f0", borderRadius: 4, padding: "1px 6px" }}>OPTIONAL</span>
                  )}
                </div>
                {total > 0 && (
                  <span style={{ fontSize: 12, color: allDone ? "#16a34a" : "#64748b", fontWeight: allDone ? 600 : 500 }}>{done}/{total}{allDone ? " · Done" : ""}</span>
                )}
              </div>
              {!isCollapsed && (
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: 12, padding: "10px 2px 4px" }}>
                  {phaseStages.map((stage) => renderStageItem(stage, wfStages.get(stage.workflow_id || "") || phaseStages, stage.workflow_id))}
                  {optionalCards.map((o) => {
                    const wfId = o.workflowId;
                    return (
                      <div key={o.stageId} style={{ display: "flex", flexDirection: "column", gap: 6, minHeight: 64, padding: "10px 12px", borderRadius: 10, border: "1px dashed #cbd5e1", backgroundColor: "#fcfdfe" }}>
                        {/* top: icon + name + role — mirrors the real stage card */}
                        <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
                          <span style={{ fontSize: 15, minWidth: 20, textAlign: "center", opacity: 0.75 }}>{STAGE_ICONS_BY_ID[o.stageId] || "•"}</span>
                          <div style={{ minWidth: 0 }}>
                            <div style={{ fontWeight: 600, fontSize: 13.5, lineHeight: 1.2, color: "#334155" }}>{o.name}</div>
                            {o.role && <div style={{ fontSize: 11, color: "#94a3b8" }}>{o.role}</div>}
                          </div>
                        </div>
                        {/* body: registered one-liner */}
                        {o.description && (
                          <div style={{ fontSize: 11.5, color: "#64748b", lineHeight: 1.4, display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{o.description}</div>
                        )}
                        {/* foot: status pill + action */}
                        <div style={{ marginTop: "auto", display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                          <span style={{ fontSize: 11, fontWeight: 600, borderRadius: 20, padding: "2px 9px", background: "#f1f5f9", color: "#64748b" }}>Available</span>
                          <button
                            disabled={!wfId}
                            title={wfId ? "Add this capability to the project and run it" : "Not available for this project"}
                            onClick={(e) => { e.stopPropagation(); if (wfId) void addAndRun(wfId); }}
                            style={{ padding: "4px 12px", borderRadius: 6, border: "none", backgroundColor: wfId ? "#3b82f6" : "#e2e8f0", color: wfId ? "#fff" : "#94a3b8", fontSize: 12, fontWeight: 600, cursor: wfId ? "pointer" : "not-allowed" }}
                          >▶ Run</button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        });
      })()}

      {/* Catalog modal — no longer triggered by an Add button (all capabilities,
          including optional ones, render as cards on the board). Kept mounted so
          any lingering reference resolves; opens only if catalogOpen is set. */}
      {multiWorkflow && (
        <>
          {catalogOpen && (
            <div style={{
              position: "fixed", inset: 0, backgroundColor: "rgba(0,0,0,0.4)",
              display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000,
            }}
              onClick={() => setCatalogOpen(false)}
            >
              <div
                style={{
                  backgroundColor: "#fff", borderRadius: 12, padding: 24,
                  width: 480, maxHeight: "80vh", overflowY: "auto",
                  boxShadow: "0 20px 60px rgba(0,0,0,0.2)",
                }}
                onClick={(e) => e.stopPropagation()}
              >
                <h3 style={{ margin: "0 0 16px", color: "#1e293b" }}>Workflow Catalog</h3>
                {catalogLoading ? (
                  <div style={{ color: "#94a3b8" }}>Loading...</div>
                ) : (
                  <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                    {catalogItems.map((item) => {
                      const alreadyAdded = item.already_added;
                      const depsReady = item.all_deps_met;
                      const stageNames = (item.stages || [])
                        .filter((s: { enabled?: boolean }) => s.enabled !== false)
                        .map((s: { stage_id: string }) => STAGE_REGISTRY_NAMES[s.stage_id] || s.stage_id);

                      return (
                        <div
                          key={item.workflow_id}
                          style={{
                            padding: "12px 16px",
                            borderRadius: 8,
                            border: `1px solid ${alreadyAdded ? "#bbf7d0" : "#e2e8f0"}`,
                            backgroundColor: alreadyAdded ? "#f0fdf4" : "#fff",
                          }}
                        >
                          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                            <div>
                              <div style={{ fontWeight: 600, fontSize: 14, color: "#334155" }}>
                                {item.name}
                                {item.repeatable && (
                                  <span style={{ fontSize: 10, marginLeft: 8, padding: "1px 6px", borderRadius: 4, backgroundColor: "#e0e7ff", color: "#4338ca", fontWeight: 600 }}>
                                    Repeatable
                                  </span>
                                )}
                              </div>
                              {item.description && (
                                <div style={{ fontSize: 12, color: "#64748b", marginTop: 2 }}>{item.description}</div>
                              )}
                              <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 4 }}>
                                Stages: {stageNames.join(" → ")}
                              </div>
                              {item.required_stages && item.required_stages.length > 0 && (
                                <div style={{ fontSize: 11, marginTop: 4, color: depsReady ? "#16a34a" : "#f59e0b" }}>
                                  {depsReady ? "All prerequisites met" : `Requires: ${item.required_stages.map((r: { name: string }) => r.name).join(", ")}`}
                                </div>
                              )}
                            </div>
                            <div>
                              {alreadyAdded ? (
                                <span style={{ fontSize: 12, color: "#16a34a", fontWeight: 600 }}>Added</span>
                              ) : (
                                <button
                                  onClick={async () => {
                                    try {
                                      await api.post(`/api/projects/${projectId}/workflows`, { workflow_id: item.workflow_id });
                                      setCatalogOpen(false);
                                      onStageCompleted?.();
                                    } catch (err) {
                                      console.error(err);
                                    }
                                  }}
                                  style={{
                                    padding: "5px 14px",
                                    borderRadius: 6,
                                    border: "none",
                                    backgroundColor: "#3b82f6",
                                    color: "#fff",
                                    fontSize: 12,
                                    fontWeight: 600,
                                    cursor: "pointer",
                                  }}
                                >
                                  Add
                                </button>
                              )}
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
                <button
                  onClick={() => setCatalogOpen(false)}
                  style={{
                    marginTop: 16,
                    padding: "6px 16px",
                    borderRadius: 6,
                    border: "1px solid #cbd5e1",
                    backgroundColor: "#fff",
                    color: "#64748b",
                    fontSize: 13,
                    fontWeight: 600,
                    cursor: "pointer",
                  }}
                >
                  Close
                </button>
              </div>
            </div>
          )}
        </>
      )}

      {/* Legacy flat stage list */}
      {!multiWorkflow && stages.map((stage) => {
        const isActive = activeStage === stage.stage_number;
        const statusAllowsRun = stage.status === "pending" || stage.status === "failed";
        const prevStatus = stages.find((s) => s.stage_number === stage.stage_number - 1)?.status;
        const prevComplete = stage.stage_number === 1 || prevStatus === "complete" || prevStatus === "awaiting_review";
        const roleAllowsRun = canRunStage(role, stage.stage_number, stage.stage_id) && !readOnly;
        const isRunning = runningStage === stage.stage_number;

        return (
          <div key={stage.stage_number}>
            <div
              onClick={() => onStageClick(stage.stage_number)}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "10px 16px",
                borderRadius: 8,
                cursor: "pointer",
                backgroundColor: isActive ? "#e2e8f0" : "#fff",
                border: isActive ? "2px solid #3b82f6" : "1px solid #e2e8f0",
                transition: "all 0.15s",
              }}
            >
              <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ fontSize: 15, minWidth: 18, textAlign: "center", opacity: 0.7 }}>
                    {(stage.stage_id && STAGE_ICONS_BY_ID[stage.stage_id]) || STAGE_ICONS[stage.stage_number] || stage.stage_number}
                  </span>
                  <span style={{ fontWeight: 500, fontSize: 14 }}>{stage.stage_name}</span>
                </div>
                {stage.owner_role && (
                  <span style={{ fontSize: 11, color: "#94a3b8", marginLeft: 30 }}>
                    {stage.owner_role}
                  </span>
                )}
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <StageChip stage={stage} />
                {statusAllowsRun && !isRunning && actionLoading !== stage.stage_number && (() => {
                  const nonLlmAction = stage.stage_id ? NON_LLM_ACTIONS[stage.stage_id] : undefined;
                  const buttonLabel = nonLlmAction ? nonLlmAction.label : "Run";
                  // Freeform: stage ordering is a recommendation, not a gate.
                  const canRun = roleAllowsRun;
                  const handleClick = nonLlmAction
                    ? (e: React.MouseEvent) => { e.stopPropagation(); if (canRun) nonLlmAction.action(stage); }
                    : (e: React.MouseEvent) => canRun ? handleRunClick(e, stage) : e.stopPropagation();
                  const tooltip = !roleAllowsRun ? `Requires ${stage.owner_role} role` : !prevComplete ? "Ahead of the recommended order — you can run it now." : undefined;
                  return (
                    <button
                      onClick={handleClick}
                      disabled={!canRun}
                      title={tooltip}
                      style={{
                        padding: "4px 12px",
                        borderRadius: 6,
                        border: "none",
                        backgroundColor: canRun ? "#3b82f6" : "#e2e8f0",
                        color: canRun ? "#fff" : "#94a3b8",
                        fontSize: 12,
                        fontWeight: 600,
                        cursor: canRun ? "pointer" : "not-allowed",
                        opacity: canRun ? 1 : 0.7,
                      }}
                    >
                      {buttonLabel}
                    </button>
                  );
                })()}
                {stage.stage_id === "select_data_source" && stage.status === "complete" && !isRunning && actionLoading !== stage.stage_number && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      if (!roleAllowsRun) return;
                      void openDataSourceDialog(stage, "edit");
                    }}
                    disabled={!roleAllowsRun}
                    aria-label="Edit data source"
                    title={!roleAllowsRun ? `Requires ${stage.owner_role} role` : "Edit the saved connection"}
                    style={{
                      width: 26,
                      height: 26,
                      padding: 0,
                      borderRadius: 6,
                      border: "1px solid #cbd5e1",
                      backgroundColor: "#fff",
                      color: roleAllowsRun ? "#475569" : "#94a3b8",
                      fontSize: 13,
                      fontWeight: 600,
                      lineHeight: 1,
                      cursor: roleAllowsRun ? "pointer" : "not-allowed",
                      display: "inline-flex",
                      alignItems: "center",
                      justifyContent: "center",
                    }}
                  >
                    ✎
                  </button>
                )}
                {actionLoading === stage.stage_number && (
                  <span style={{ fontSize: 12, color: "#f59e0b", fontWeight: 600 }}>Processing...</span>
                )}
                {isRunning && (
                  <span style={{ fontSize: 12, color: "#f59e0b", fontWeight: 600 }}>Running...</span>
                )}
                {stage.status === "failed" && stage.has_review && (
                  <button
                    onClick={async (e) => {
                      e.stopPropagation();
                      try {
                        await api.post(`/api/projects/${projectId}/stages/${stage.stage_number}/recover`);
                        onStageCompleted?.();
                      } catch { /* ignore */ }
                    }}
                    style={{
                      padding: "4px 10px", borderRadius: 6, border: "1px solid #f59e0b",
                      backgroundColor: "#fef3c7", color: "#92400e",
                      fontSize: 11, fontWeight: 600, cursor: "pointer",
                    }}
                    title="Check for pending reviews and recover this stage"
                  >
                    Recover
                  </button>
                )}
              </div>
            </div>
          </div>
        );
      })}

      {renderConfigDialog()}

      <MaterializationGateDialog
        open={materializeGate !== null}
        projectId={projectId}
        stage={materializeGate}
        onClose={() => setMaterializeGate(null)}
        onCompleted={() => { onStageCompleted?.(); }}
      />

      <ConnectionPickerDialog
        open={connectionPickerOpen}
        projectId={projectId}
        submitting={connectionPickerSaving}
        mode={dataSourceMode}
        onClose={() => {
          if (!connectionPickerSaving) setConnectionPickerOpen(false);
        }}
        onSubmit={async (result: ConnectionPickerResult) => {
          if (!dataSourceTarget) return;
          const stage = dataSourceTarget;
          setConnectionPickerSaving(true);
          try {
            // NB: no target_dialect — the serving workflow (configure_serving) is
            // the sole owner of the SQL dialect. Omitting it leaves any existing
            // value untouched (the backend guards on None).
            await api.put(`/api/projects/${projectId}/source-binding`, {
              connection_id: result.connectionId,
              default_schema: result.defaultSchema,
            });
            // Only flip the stage to complete on first-time configure; in
            // edit mode the stage is already complete and the backend
            // rejects re-completion with a 400.
            if (dataSourceMode === "create") {
              await api.post(
                `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
                null,
                { params: { workflow_id: stage.workflow_id } }
              );
            }
            setConnectionPickerOpen(false);
            onStageCompleted?.();
          } catch (err) {
            console.error(err);
          } finally {
            setConnectionPickerSaving(false);
          }
        }}
      />

      <ConfigureServingDialog
        open={configureServingStage !== null}
        projectId={projectId}
        stage={configureServingStage}
        onClose={() => setConfigureServingStage(null)}
        onCompleted={() => {
          setConfigureServingStage(null);
          onStageCompleted?.();
        }}
      />

      <ConfigureTransferPlacementDialog
        open={transferPlacementStage !== null}
        projectId={projectId}
        stage={transferPlacementStage}
        onClose={() => setTransferPlacementStage(null)}
        onCompleted={() => {
          setTransferPlacementStage(null);
          onStageCompleted?.();
        }}
      />

      <ConfigureDqDialog
        open={configureDqStage !== null}
        projectId={projectId}
        stage={configureDqStage}
        activeGenStageId={(() => {
          if (!configureDqStage) return null;
          const wfId = configureDqStage.workflow_id;
          const wfStages = (workflows?.find((w) => w.workflow_id === wfId)?.stages) || stages;
          const gen = wfStages.find(
            (s) => s.stage_id === "dq_test_generation_gx" || s.stage_id === "dq_test_generation_python",
          );
          return gen?.stage_id || null;
        })()}
        onClose={() => setConfigureDqStage(null)}
        onCompleted={() => {
          setConfigureDqStage(null);
          onStageCompleted?.();
        }}
      />

      <ConfigureMigrationDialog
        open={configureMigrationStage !== null}
        projectId={projectId}
        stage={configureMigrationStage}
        onClose={() => setConfigureMigrationStage(null)}
        onCompleted={() => {
          setConfigureMigrationStage(null);
          onStageCompleted?.();
        }}
      />

      {/* Floating stage tooltip — rendered once, follows the cursor. */}
      {hoveredStage && hoveredStage.stage.description && (
        <div
          style={{
            position: "fixed",
            left: hoveredStage.x + 14,
            top: hoveredStage.y + 14,
            maxWidth: 340,
            padding: "10px 12px",
            background: "#0f172a",
            color: "#f8fafc",
            borderRadius: 6,
            fontSize: 12,
            lineHeight: 1.5,
            boxShadow: "0 4px 12px rgba(0,0,0,0.18)",
            pointerEvents: "none",
            zIndex: 1000,
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: 4 }}>
            {hoveredStage.stage.stage_name}
          </div>
          <div style={{ opacity: 0.92 }}>{hoveredStage.stage.description}</div>
        </div>
      )}
    </div>
  );
}

const labelStyle: React.CSSProperties = {
  fontSize: 12, color: "#64748b", display: "block", marginBottom: 3,
};

const inputStyle: React.CSSProperties = {
  width: "100%",
  padding: "6px 10px",
  borderRadius: 5,
  border: "1px solid #cbd5e1",
  fontSize: 13,
  boxSizing: "border-box",
};
