import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocation, useParams } from "react-router-dom";
import api from "../api/client";
import type { ProjectInfo, WSMessage, WorkflowInfo } from "../types";
import Pipeline from "../components/Pipeline";
import ProductOwnerContextBanner from "../components/ProductOwnerContextBanner";
import ProjectEditBanner from "../components/ProjectEditBanner";
import ProjectAcceptBanner from "../components/ProjectAcceptBanner";
import ReconciliationBanner from "../components/ReconciliationBanner";
import UpstreamDriftBanner from "../components/UpstreamDriftBanner";
import IntakeOriginPanel from "../components/IntakeOriginPanel";
import ConfirmPhysicalSchemaPanel from "../components/ConfirmPhysicalSchemaPanel";
import StageDetail from "../components/StageDetail";
import StageChip from "../components/StageChip";
import ArtifactBrowser from "../components/ArtifactBrowser";
import ReviewPanel from "../components/ReviewPanel";
import ProjectDashboard from "../components/ProjectDashboard";
import ODCSEditor from "../components/ODCSEditor";
import ResultsViewer from "../components/ResultsViewer";
import TestRunsPanel from "../components/TestRunsPanel";
import ChatPanel from "../components/chat/ChatPanel";
import type { ChatContextKey } from "../components/chat/suggestedPrompts";
import SourcePlatformBadge from "../components/SourcePlatformBadge";
import MigrationSourceTargetPanel from "../components/MigrationSourceTargetPanel";
import { useWebSocket } from "../hooks/useWebSocket";
import { useStageExecution, useStageExecutions } from "../hooks/useStageExecutions";

type StageTab = "output" | "artifacts" | "reviews" | "odcs" | "results" | "runs";

// Runs tab — show for any DQ testing stage so users can see history from
// either the generation step (what tests were made) or the execution step.
const DQ_TEST_STAGE_IDS = new Set([
  "dq_test_generation_gx",
  "dq_test_generation_python",
  "dq_test_execution",
]);

// Stages that never stream (completed via POST /stages/{n}/complete) — these
// have no transcript to show.
const NON_STREAMING_STAGE_IDS = new Set([
  "initiate",
  "odcs_specification",
  "odcs_to_dprod",
  "publish",
  "mark_engineering_complete",
]);

// PO-owned stages collapsed into the context banner on the engineer side.
// odcs_to_dprod used to live here but moved to the engineer's workflow — it's
// their first technical step once the PO contract arrives.
// po_source_validation (dpe-sa) is hidden too — it's the PO's mid-pipeline
// gate. The engineer's awareness comes from product_materialization_sa
// rendering as "Pending PO validation" until the gate clears.
const PO_STAGE_IDS = new Set([
  "initiate",
  "odcs_specification",
  "publish",
  "po_source_validation",
]);

function filterPOStages(stages: ProjectInfo["stages"]): ProjectInfo["stages"] {
  return stages.filter((s) => !s.stage_id || !PO_STAGE_IDS.has(s.stage_id));
}

function filterPOWorkflows(workflows: WorkflowInfo[]): WorkflowInfo[] {
  return workflows
    .map((w) => ({ ...w, stages: filterPOStages(w.stages) }))
    .filter((w) => w.stages.length > 0);
}

// Look across ALL workflows (unfiltered) for the SA validation gate so the
// Pipeline's first materialization stage knows whether to render as
// "Pending PO validation". Returns null if there's no gate (dpe-cf, dq, dd).
function getPoGateStatus(project: ProjectInfo): string | null {
  const allStages = project.multi_workflow
    ? project.workflows.flatMap((w) => w.stages)
    : project.stages;
  const gate = allStages.find((s) => s.stage_id === "po_source_validation");
  return gate ? gate.status : null;
}

export default function ProjectDetailPage() {
  const { id } = useParams<{ id: string }>();
  const projectId = id ? parseInt(id) : null;
  const isEngineerShell = useLocation().pathname.startsWith("/engineer");

  const [project, setProject] = useState<ProjectInfo | null>(null);
  // Bumped to force the summary panels (ProductOwnerContextBanner + ProjectDashboard)
  // to re-fetch — they otherwise only load on mount, so stats went stale until a
  // full page reload when a stage completed.
  const [summaryRefreshKey, setSummaryRefreshKey] = useState(0);
  const [activeStage, setActiveStage] = useState<number | null>(null);
  const [activeWorkflow, setActiveWorkflow] = useState<string | null>(null);
  const [runningStage, setRunningStage] = useState<number | null>(null);
  const [runningWorkflow, setRunningWorkflow] = useState<string | null>(null);
  const [stageTab, setStageTab] = useState<StageTab>("output");
  const [selectedExecutionId, setSelectedExecutionId] = useState<number | null>(null);
  const [liveSelected, setLiveSelected] = useState(true);
  const [chatOpen, setChatOpen] = useState(false);
  // One-shot prefill for ChatPanel's input box. MappingReviewPanel's
  // "Guide me" button sets this to a contextual question, opens the chat
  // drawer, and the consumer (ChatPanel) clears it via onPrefillConsumed
  // after seeding. Null when there's no pending prefill.
  const [chatPrefill, setChatPrefill] = useState<string | null>(null);
  const openChatWithPrefill = useCallback((text: string) => {
    setChatPrefill(text);
    setChatOpen(true);
  }, []);
  // When a user clicks "Create mapping…" on the dashboard's unmapped table,
  // we hop to the data_mapping stage's Reviews tab and ask the
  // UnmappedColumnsPanel to pre-expand the row matching this URI.
  const [focusUnmappedColumnUri, setFocusUnmappedColumnUri] = useState<string | null>(null);
  // Latest non-rejected ProductRequest for this project — drives the
  // engineer-side ProjectEditBanner. We don't display this anywhere else;
  // status updates (accept / complete) just refresh the project on the next
  // loadProject pass.
  const [latestRequestKind, setLatestRequestKind] = useState<string | null>(null);
  const [latestRequestStatus, setLatestRequestStatus] = useState<string | null>(null);
  const [latestRequestId, setLatestRequestId] = useState<number | null>(null);

  const {
    status,
    pendingQuestions,
    sendAction,
    sendAgentResponse,
    getLiveRuns,
    getBuffer,
    lastEvent,
  } = useWebSocket(projectId);

  const loadProject = useCallback(async () => {
    if (!projectId) return;
    const res = await api.get(`/api/projects/${projectId}`);
    setProject(res.data);
    // Side fetch — 404 is fine (no submission yet); we just leave the
    // banner hidden in that case.
    api
      .get(`/api/projects/${projectId}/product-requests/latest`)
      .then((r) => {
        setLatestRequestKind(r.data?.kind ?? null);
        setLatestRequestStatus(r.data?.status ?? null);
        setLatestRequestId(r.data?.id ?? null);
      })
      .catch(() => {
        setLatestRequestKind(null);
        setLatestRequestStatus(null);
        setLatestRequestId(null);
      });
  }, [projectId]);

  useEffect(() => {
    loadProject();
  }, [loadProject]);

  // Reset per-project selection state when the URL's project id changes.
  // React Router keeps this component mounted across :id changes, so without
  // this reset the new project inherits the previous project's active stage,
  // selected execution, running-stage indicator, etc.
  useEffect(() => {
    setProject(null);
    setActiveStage(null);
    setActiveWorkflow(null);
    setRunningStage(null);
    setRunningWorkflow(null);
    setStageTab("output");
    setSelectedExecutionId(null);
    setLiveSelected(true);
  }, [projectId]);

  // Refresh project when a stage completes or status changes
  useEffect(() => {
    if (!lastEvent) return;
    if (
      lastEvent.type === "stage_complete" ||
      lastEvent.type === "stage_status_changed" ||
      lastEvent.type === "error"
    ) {
      setRunningStage(null);
      loadProject();
      // Bump so child panels (Reviews, Mappings, Summary) refetch — a
      // just-completed stage's review items now appear without a page reload.
      setSummaryRefreshKey((k) => k + 1);
      if (lastEvent.type === "stage_status_changed" && lastEvent.status === "awaiting_review") {
        setStageTab("reviews");
      }
    }
  }, [lastEvent, loadProject]);

  const handleRunStage = (stageNumber: number, stageConfig?: Record<string, string>, workflowId?: string) => {
    setActiveStage(stageNumber);
    setActiveWorkflow(workflowId || null);
    setRunningStage(stageNumber);
    setRunningWorkflow(workflowId || null);
    setStageTab("output");
    setLiveSelected(true);
    setSelectedExecutionId(null);
    sendAction("run_stage", {
      stage_number: stageNumber,
      stage_config: stageConfig || {},
      ...(workflowId ? { workflow_id: workflowId } : {}),
    });
  };

  /**
   * Reset a stage's StageRun then re-invoke the skill. Used by both
   * Pipeline.tsx's Rerun button and MappingReviewPanel's "Re-run mapping"
   * affordance — hoisted here so the two share one code path.
   *
   * Reset is a SQL-side flip (`StageRun.status = pending`); it doesn't
   * touch Neo4j data. For a true wipe (e.g. the engineer picked
   * "Start over" in MappingReviewPanel), the caller is responsible for
   * POSTing the wipe endpoint BEFORE invoking this helper.
   */
  const handleRerunStage = async (stageNumber: number, workflowId?: string) => {
    const wfParam = workflowId ? `?workflow_id=${workflowId}` : "";
    await api.post(`/api/projects/${projectId}/stages/${stageNumber}/reset${wfParam}`);
    handleRunStage(stageNumber, undefined, workflowId);
  };

  const handleStageClick = (stageNumber: number, workflowId?: string) => {
    setActiveStage(stageNumber);
    setActiveWorkflow(workflowId || null);
    setLiveSelected(true);
    setSelectedExecutionId(null);
    // Auto-select ODCS tab for the ODCS specification stage
    const allStages = project?.multi_workflow
      ? project.workflows.flatMap((w) => w.stages)
      : project?.stages || [];
    const stage = allStages.find(
      (s) => s.stage_number === stageNumber && (!workflowId || s.workflow_id === workflowId)
    );
    if (stage?.stage_id === "odcs_specification") {
      setStageTab("odcs");
    } else {
      setStageTab("output");
    }
  };

  const handleTitleClick = () => {
    setActiveStage(null);
  };

  const allStages = useMemo(() => {
    if (!project) return [];
    return project.multi_workflow
      ? project.workflows.flatMap((w) => w.stages)
      : project.stages;
  }, [project]);

  // data_mapping stage context for MappingReviewPanel's Re-run affordance.
  // The `repeatable` flag lives at the workflow level (WorkflowInfo.repeatable);
  // look up the workflow that owns the data_mapping stage and surface it
  // alongside the stage itself. Falls back to single-workflow shape for
  // projects that pre-date multi_workflow.
  const dataMappingStage = useMemo(
    () => allStages.find((s) => s.stage_id === "data_mapping") ?? null,
    [allStages],
  );
  const dataMappingRepeatable = useMemo(() => {
    if (!project || !dataMappingStage) return false;
    if (project.multi_workflow) {
      const wf = project.workflows.find((w) => w.workflow_id === dataMappingStage.workflow_id);
      return wf?.repeatable ?? false;
    }
    // Single-workflow projects: there's only one workflow; check its repeatable.
    return project.workflows[0]?.repeatable ?? true;
  }, [project, dataMappingStage]);

  /**
   * Deep-link from the dashboard's "Create mapping…" affordance: select the
   * data_mapping stage and switch to the Reviews tab. The
   * focusUnmappedColumnUri prop tells UnmappedColumnsPanel which row to
   * expand once it mounts.
   *
   * If no data_mapping stage exists in this project (rare — DPE-CF projects
   * always have one) we fall back to clearing the stage selection so the
   * user lands on the project summary rather than something stale.
   */
  const navigateToUnmappedColumn = (columnUri: string) => {
    const target = allStages.find((s) => s.stage_id === "data_mapping");
    if (!target) {
      setActiveStage(null);
      return;
    }
    setActiveStage(target.stage_number);
    setActiveWorkflow(target.workflow_id || null);
    setLiveSelected(true);
    setStageTab("reviews");
    setFocusUnmappedColumnUri(columnUri);
  };

  /**
   * Sibling of navigateToUnmappedColumn — opens the data_mapping stage's
   * Reviews tab without targeting a specific column. Used by the dashboard
   * Mappings card's graph view "Open in Reviews →" deep-link.
   */
  const navigateToMappingReview = () => {
    const target = allStages.find((s) => s.stage_id === "data_mapping");
    if (!target) return;
    setActiveStage(target.stage_number);
    setActiveWorkflow(target.workflow_id || null);
    setLiveSelected(true);
    setStageTab("reviews");
  };

  const activeStageInfo = useMemo(
    () =>
      allStages.find(
        (s) => s.stage_number === activeStage && (!activeWorkflow || s.workflow_id === activeWorkflow)
      ),
    [allStages, activeStage, activeWorkflow]
  );

  // Executions history for the active stage
  const executionsRefreshKey = useMemo(() => {
    if (!lastEvent) return 0;
    if (lastEvent.type === "stage_complete" || lastEvent.type === "error") {
      return Date.now();
    }
    return 0;
  }, [lastEvent]);

  const { executions } = useStageExecutions(
    projectId,
    activeStage,
    activeWorkflow,
    executionsRefreshKey,
  );

  // Live runs for this stage (from the WS keyed buffers)
  const liveRuns = useMemo(() => {
    if (!activeStage) return [];
    return getLiveRuns(activeWorkflow, activeStage);
  }, [getLiveRuns, activeStage, activeWorkflow]);

  const liveRunIds = useMemo(() => new Set(liveRuns.map((r) => r.runId)), [liveRuns]);

  // Pick the latest live run as the source when liveSelected
  const liveRun = liveRuns[0] ?? null;
  const liveBuffer: WSMessage[] = useMemo(() => {
    if (!liveRun || !activeStage) return [];
    return getBuffer(activeWorkflow, activeStage, liveRun.runId) ?? [];
  }, [liveRun, activeStage, activeWorkflow, getBuffer]);

  // Auto-select live when a new run starts for the active stage
  useEffect(() => {
    if (liveRun && liveRun.status === "running") {
      setLiveSelected(true);
      setSelectedExecutionId(null);
    }
  }, [liveRun?.runId, liveRun?.status]);

  // If liveSelected but no live buffer exists, fall back to latest historical
  const effectiveLiveSelected = liveSelected && liveRuns.length > 0;

  const selectedHistoricalId = useMemo(() => {
    if (effectiveLiveSelected) return null;
    if (selectedExecutionId) return selectedExecutionId;
    return executions[0]?.id ?? null;
  }, [effectiveLiveSelected, selectedExecutionId, executions]);

  const { detail: historicalDetail } = useStageExecution(selectedHistoricalId, projectId);

  const isStreaming = effectiveLiveSelected && liveRun?.status === "running";

  // Build the props passed to StageDetail based on selection
  const stageDetailSource = useMemo(() => {
    if (effectiveLiveSelected && liveRun) {
      return {
        events: liveBuffer,
        status: liveRun.status,
        startedAt: liveRun.startedAt,
        completedAt: null as string | null,
        costUsd: liveRun.cost_usd,
        truncated: false,
      };
    }
    if (historicalDetail) {
      return {
        events: historicalDetail.events,
        status: historicalDetail.status,
        startedAt: historicalDetail.started_at,
        completedAt: historicalDetail.completed_at,
        costUsd: historicalDetail.cost_usd,
        truncated: historicalDetail.truncated,
      };
    }
    return {
      events: [] as WSMessage[],
      status: activeStageInfo?.status ?? "pending",
      startedAt: activeStageInfo?.started_at ?? null,
      completedAt: activeStageInfo?.completed_at ?? null,
      costUsd: activeStageInfo?.cost_usd ?? null,
      truncated: false,
    };
  }, [effectiveLiveSelected, liveRun, liveBuffer, historicalDetail, activeStageInfo]);

  if (!project) return <div>Loading...</div>;

  const activeStageName = activeStageInfo?.stage_name || "";
  const reviewCount = allStages.filter((s) => s.status === "awaiting_review").length;
  const isODCSStage = activeStageInfo?.stage_id === "odcs_specification";
  const isDQTestStage = !!activeStageInfo?.stage_id && DQ_TEST_STAGE_IDS.has(activeStageInfo.stage_id);
  const dqFramework = activeStageInfo?.stage_id === "dq_test_generation_python" ? "pandera" : "gx";
  const isNonStreaming = !!activeStageInfo?.stage_id && NON_STREAMING_STAGE_IDS.has(activeStageInfo.stage_id);

  // Contextual tabs: Activity is always there; Reviews only when this stage has
  // a review surface; Results only on analysis stages that produce one; the
  // server-file Artifacts browser is dropped from the default surface.
  const showReviews = !!activeStageInfo?.review_type;
  const RESULT_STAGE_IDS = new Set([
    "dq_test_execution", "dq_test_generation_gx", "dq_test_generation_python",
    "data_scoring", "rescore_composite", "data_remediation",
    "data_remediation_planning", "data_quality_failure_analysis",
  ]);
  const showResults = !!activeStageInfo?.stage_id && RESULT_STAGE_IDS.has(activeStageInfo.stage_id);
  const stageTabs: { key: StageTab; label: string; badge?: number }[] = [
    ...(isODCSStage ? [{ key: "odcs" as StageTab, label: "ODCS Editor" }] : []),
    { key: "output", label: "Activity" },
    ...(showResults ? [{ key: "results" as StageTab, label: "Results" }] : []),
    ...(isDQTestStage ? [{ key: "runs" as StageTab, label: "Runs" }] : []),
    ...(showReviews ? [{ key: "reviews" as StageTab, label: "Reviews", badge: reviewCount || undefined }] : []),
  ];

  const chatContext: ChatContextKey = stageTab === "reviews" && activeStageInfo?.review_type
    ? { kind: "review", reviewType: activeStageInfo.review_type }
    : activeStageInfo?.stage_id
    ? { kind: "stage", stageId: activeStageInfo.stage_id }
    : { kind: "overview" };

  return (
    <div style={{ padding: "8px 28px 32px" }}>
      {/* Project header — title is clickable to return to project-level view */}
      <div style={{ marginBottom: 16, display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12 }}>
        <div>
          <h2
            onClick={handleTitleClick}
            style={{
              margin: 0,
              cursor: activeStage ? "pointer" : "default",
              display: "inline-block",
              textDecoration: activeStage ? "none" : "none",
            }}
            title={activeStage ? "Back to project overview" : undefined}
          >
            {project.name}
            {activeStage && (
              <span style={{ color: "#94a3b8", fontWeight: 400, fontSize: 16 }}>
                {" "}&rsaquo; {activeStageName}
              </span>
            )}
          </h2>
          <div style={{ fontSize: 13, color: "#64748b", display: "flex", alignItems: "center", gap: 8 }}>
            <span>{project.project_code} &middot; <SourcePlatformBadge projectId={project.id} /></span>
            {project.domain && (
              <span style={{
                padding: "2px 10px", borderRadius: 10, fontSize: 11, fontWeight: 600,
                backgroundColor: "#e0f2fe", color: "#0369a1",
              }}>
                {project.domain}
              </span>
            )}
          </div>
        </div>
        <button
          onClick={() => setChatOpen((v) => !v)}
          style={{
            padding: "6px 14px",
            border: "1px solid #cbd5e1",
            borderRadius: 6,
            background: chatOpen ? "#0f172a" : "white",
            color: chatOpen ? "white" : "#0f172a",
            fontSize: 13,
            fontWeight: 600,
            cursor: "pointer",
          }}
          title="Ask about this project"
        >
          {chatOpen ? "Hide Ask" : "Ask"}
        </button>
      </div>

      {isEngineerShell && (
        <div style={{ display: "flex", gap: 16, alignItems: "stretch" }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            {project.archetype === "dmig" ? (
              <MigrationSourceTargetPanel
                project={project}
                refreshKey={summaryRefreshKey}
                onChanged={() => { loadProject(); setSummaryRefreshKey((k) => k + 1); }}
              />
            ) : (
              <ProductOwnerContextBanner project={project} refreshKey={summaryRefreshKey} />
            )}
          </div>
          {project.multi_workflow && (() => {
            // Recommended plan — the ordered capabilities (deduped) with status.
            // Click a step to focus its card on the board.
            const wfs = filterPOWorkflows(project.workflows);
            const seen = new Set<string>();
            const steps: { num: number; name: string; status: string; stageNumber: number; workflowId?: string }[] = [];
            for (const wf of wfs) {
              for (const s of wf.stages) {
                const id = s.stage_id || `#${s.stage_number}`;
                if (seen.has(id)) continue;
                seen.add(id);
                steps.push({ num: steps.length + 1, name: s.stage_name, status: s.status, stageNumber: s.stage_number, workflowId: wf.workflow_id });
              }
            }
            const recIdx = steps.findIndex((st) => st.status === "pending" || st.status === "failed");
            return (
              <div style={{ width: 300, flexShrink: 0, border: "1px solid #e2e8f0", borderRadius: 10, background: "#fff", padding: "12px 14px", maxHeight: 230, overflow: "auto" }}>
                <div style={{ fontSize: 11, fontWeight: 700, letterSpacing: 0.5, textTransform: "uppercase", color: "#64748b", marginBottom: 8 }}>Recommended plan</div>
                {steps.map((st, i) => {
                  const isRec = i === recIdx;
                  const isDone = st.status === "complete" || st.status === "awaiting_review";
                  const dot = isDone ? "✓" : st.status === "running" ? "▶" : st.status === "failed" ? "✕" : "○";
                  const dotColor = isDone ? "#16a34a" : st.status === "failed" ? "#ef4444" : isRec ? "#3b82f6" : "#cbd5e1";
                  return (
                    <div
                      key={`${st.stageNumber}-${st.workflowId || ""}`}
                      onClick={() => handleStageClick(st.stageNumber, st.workflowId)}
                      style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 6px", borderRadius: 6, cursor: "pointer", backgroundColor: (activeStage === st.stageNumber && activeWorkflow === st.workflowId) ? "#e0f2fe" : isRec ? "#eff6ff" : "transparent" }}
                    >
                      <span style={{ color: dotColor, fontWeight: 700, fontSize: 12, width: 14, textAlign: "center" }}>{dot}</span>
                      <span style={{ fontSize: 12, color: "#334155", flex: 1, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{st.num}. {st.name}</span>
                      {isRec && <span style={{ fontSize: 8, fontWeight: 800, color: "#fff", background: "#3b82f6", borderRadius: 3, padding: "1px 5px" }}>NEXT</span>}
                    </div>
                  );
                })}
              </div>
            );
          })()}
        </div>
      )}
      {isEngineerShell && (
        <ProjectEditBanner
          projectId={project.id}
          latestRequestKind={latestRequestKind}
          latestRequestStatus={latestRequestStatus}
        />
      )}
      {isEngineerShell && (
        <ProjectAcceptBanner
          projectId={project.id}
          requestId={latestRequestId}
          latestRequestKind={latestRequestKind}
          latestRequestStatus={latestRequestStatus}
          archetype={project.archetype}
          onAccepted={() => { loadProject(); setSummaryRefreshKey((k) => k + 1); }}
        />
      )}
      {/* Phase 3: surface upstream drift when any :CONSUMES'd source has
          advanced beyond this project's pinned consumedVersion. Only mounted
          on the engineer shell — consumer engineers are the ones who act on
          stale mappings. Source-aligned projects have no inputs so the
          endpoint returns an empty drift list and the banner self-hides. */}
      {isEngineerShell && <UpstreamDriftBanner projectId={project.id} />}
      {isEngineerShell && (
        <ReconciliationBanner
          projectId={project.id}
          refreshKey={summaryRefreshKey}
          onReview={navigateToMappingReview}
        />
      )}

      {/* Provenance for intake-scaffolded projects — self-hides otherwise. */}
      {isEngineerShell && <IntakeOriginPanel projectId={project.id} />}

      {/* Confirm Physical Schema — only for schema-only dmig projects. */}
      {isEngineerShell && project.archetype === "dmig" && (
        <ConfirmPhysicalSchemaPanel projectId={project.id} mode={project.data_connectivity_mode} />
      )}

      {/* Top: capability board (full width) */}
      <Pipeline
        projectId={project.id}
        stages={isEngineerShell ? filterPOStages(project.stages) : project.stages}
        activeStage={activeStage}
        activeWorkflow={activeWorkflow}
        onStageClick={handleStageClick}
        onRunStage={handleRunStage}
        onStageCompleted={() => { loadProject(); setSummaryRefreshKey((k) => k + 1); }}
        runningStage={runningStage}
        runningWorkflow={runningWorkflow}
        multiWorkflow={project.multi_workflow}
        workflows={isEngineerShell ? filterPOWorkflows(project.workflows) : project.workflows}
        poGateStatus={getPoGateStatus(project)}
        requestNotAccepted={latestRequestStatus === "submitted"}
      />

      {/* Below: focus / work area, then Project Summary full-width underneath (stacked) */}
      <div style={{ display: "flex", flexDirection: "column", gap: 24, marginTop: 22 }}>
        {/* The selected capability's work area */}
        <div style={{ display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
          {activeStage === null ? (
            /* No capability selected — point the engineer at the board */
            <div style={{ padding: "32px 20px", backgroundColor: "#f8fafc", border: "1px dashed #cbd5e1", borderRadius: 10, color: "#64748b", fontSize: 14, textAlign: "center" }}>
              Select a capability above to run it, watch its progress, or review its output.
              <div style={{ fontSize: 12, color: "#94a3b8", marginTop: 6 }}>
                Recommended next steps are highlighted — you can run any step in any order.
              </div>
            </div>
          ) : (
            /* ── Selected capability: header + tabs (bordered focus card) ── */
            <div style={{ border: "1px solid #e2e8f0", borderRadius: 12, background: "#fff", padding: "16px 18px", display: "flex", flexDirection: "column", gap: 12 }}>
              {/* Capability header — name, status, description, Re-run */}
              <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12, marginBottom: 4 }}>
                <div style={{ minWidth: 0 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                    <span style={{ fontSize: 17, fontWeight: 700, color: "#1e293b" }}>{activeStageName}</span>
                    {activeStageInfo && <StageChip stage={activeStageInfo} />}
                  </div>
                  {activeStageInfo?.description && (
                    <div style={{ fontSize: 13, color: "#64748b", marginTop: 3, lineHeight: 1.5 }}>{activeStageInfo.description}</div>
                  )}
                </div>
                {activeStageInfo && (activeStageInfo.status === "complete" || activeStageInfo.status === "awaiting_review") && (
                  <button
                    onClick={() => handleRerunStage(activeStageInfo.stage_number, activeStageInfo.workflow_id)}
                    style={{ flexShrink: 0, padding: "6px 14px", borderRadius: 6, border: "1px solid #cbd5e1", backgroundColor: "#fff", color: "#475569", fontSize: 13, fontWeight: 600, cursor: "pointer" }}
                  >
                    ↻ Re-run
                  </button>
                )}
              </div>
              {/* Tabs */}
              <div
                style={{
                  display: "flex",
                  gap: 0,
                  borderBottom: "2px solid #e2e8f0",
                }}
              >
                {stageTabs.map((t) => {
                  const isActive = stageTab === t.key;
                  return (
                    <button
                      key={t.key}
                      onClick={() => setStageTab(t.key)}
                      style={{
                        padding: "10px 20px",
                        border: "none",
                        borderBottom: isActive ? "2px solid #3b82f6" : "2px solid transparent",
                        marginBottom: -2,
                        backgroundColor: "transparent",
                        color: isActive ? "#1e293b" : "#64748b",
                        fontWeight: isActive ? 700 : 500,
                        fontSize: 14,
                        cursor: "pointer",
                        transition: "all 0.15s",
                      }}
                    >
                      {t.label}
                      {t.badge != null && t.badge > 0 && (
                        <span
                          style={{
                            marginLeft: 6,
                            padding: "1px 7px",
                            borderRadius: 10,
                            fontSize: 11,
                            fontWeight: 700,
                            backgroundColor: "#8b5cf6",
                            color: "#fff",
                          }}
                        >
                          {t.badge}
                        </span>
                      )}
                    </button>
                  );
                })}
                <div
                  style={{
                    marginLeft: "auto",
                    fontSize: 12,
                    color: "#94a3b8",
                    alignSelf: "center",
                    paddingRight: 4,
                  }}
                >
                  WS: {status}
                </div>
              </div>

              {/* Tab content */}
              {stageTab === "output" && (
                isNonStreaming ? (
                  <div
                    style={{
                      padding: 16,
                      backgroundColor: "#f8fafc",
                      border: "1px dashed #cbd5e1",
                      borderRadius: 8,
                      color: "#64748b",
                      fontSize: 13,
                    }}
                  >
                    This stage does not produce a streaming transcript. Use the other tabs
                    (e.g. ODCS Editor, Artifacts) to work with it.
                  </div>
                ) : (
                  <StageDetail
                    events={stageDetailSource.events}
                    stageName={activeStageName}
                    pendingQuestions={pendingQuestions}
                    onAgentResponse={sendAgentResponse}
                    status={stageDetailSource.status}
                    startedAt={stageDetailSource.startedAt}
                    completedAt={stageDetailSource.completedAt}
                    costUsd={stageDetailSource.costUsd}
                    truncated={stageDetailSource.truncated}
                    executions={executions}
                    liveRunIds={liveRunIds}
                    selectedExecutionId={selectedHistoricalId}
                    liveSelected={effectiveLiveSelected}
                    onSelectLive={() => {
                      setLiveSelected(true);
                      setSelectedExecutionId(null);
                    }}
                    onSelectExecution={(id) => {
                      setLiveSelected(false);
                      setSelectedExecutionId(id);
                    }}
                    isStreaming={isStreaming}
                  />
                )
              )}
              {stageTab === "artifacts" && <ArtifactBrowser projectId={project.id} />}
              {stageTab === "odcs" && (
                <ODCSEditor projectId={project.id} stageNumber={activeStage ?? undefined} onSaved={loadProject} />
              )}
              {stageTab === "results" && (
                <ResultsViewer projectId={project.id} />
              )}
              {stageTab === "runs" && (
                <TestRunsPanel projectId={project.id} framework={dqFramework} />
              )}
              {stageTab === "reviews" && (
                <ReviewPanel
                  projectId={project.id}
                  archetype={project.archetype}
                  stages={allStages}
                  onReviewComplete={loadProject}
                  activeReviewType={activeStageInfo?.review_type ?? null}
                  focusUnmappedColumnUri={focusUnmappedColumnUri}
                  onUnmappedFocusConsumed={() => setFocusUnmappedColumnUri(null)}
                  dataMappingStage={dataMappingStage}
                  dataMappingRepeatable={dataMappingRepeatable}
                  onRerunStage={handleRerunStage}
                  onGuideMe={openChatWithPrefill}
                  refreshKey={summaryRefreshKey}
                />
              )}
            </div>
          )}
        </div>
        {/* Right: Project Summary — always visible */}
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <div style={{ fontSize: 15, fontWeight: 600, color: "#334155" }}>Project Summary</div>
            <button
              onClick={() => { loadProject(); setSummaryRefreshKey((k) => k + 1); }}
              title="Refresh project summary (stats update after stages complete)"
              style={{ padding: "3px 10px", borderRadius: 6, border: "1px solid #cbd5e1", background: "#fff",
                color: "#475569", fontSize: 12, fontWeight: 600, cursor: "pointer" }}
            >
              ↻ Refresh
            </button>
          </div>
          <ProjectDashboard
            projectId={project.id}
            onNavigateToUnmapped={navigateToUnmappedColumn}
            onNavigateToMappingReview={navigateToMappingReview}
            refreshKey={summaryRefreshKey}
          />
        </div>
      </div>
      {chatOpen && projectId !== null && (
        <ChatPanel
          projectId={projectId}
          projectCode={project.project_code}
          context={chatContext}
          onClose={() => setChatOpen(false)}
          inputPrefill={chatPrefill}
          onPrefillConsumed={() => setChatPrefill(null)}
        />
      )}
    </div>
  );
}
