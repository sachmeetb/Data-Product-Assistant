// Curated question set + free-form probe for a data product.
//
// Shared presenter mounted in three places:
//   1. Marketplace product detail tab (consumer-facing read; owner sees
//      Regenerate). Pass `projectId` if you have it for /qa/evaluate +
//      engineer probe, or pass `marketplaceUri` for the public probe.
//   2. Engineer ProjectDashboard detail row (engineer's own QA view).
//   3. PO wizard OSI Readiness step (`scoreOnDemand` + skipping persistence
//      until publish).
//
// The component is read-mostly: if `initial` is passed, no fetch happens.
// Otherwise it loads GET /api/projects/{id}/qa/evaluation on mount. The
// Regenerate button (when shown) POSTs /qa/evaluate with trigger='manual'.
// The probe textarea (always rendered when probing is enabled) POSTs to
// /qa/probe (project-scoped) or /api/marketplace/qa/probe (consumer).

import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import api from "../api/client";

export type QaConfidence = "high" | "medium" | "low";

export type QaCategory =
  | "descriptive"
  | "comparative"
  | "trend"
  | "segmentation"
  | "drilldown"
  | "relational"
  | string;

export interface QaQuestion {
  text: string;
  category?: QaCategory;
  supporting_columns?: string[];
  supporting_rules?: string[];
  confidence?: QaConfidence;
  rationale?: string;
}

export interface QaNearMissGap {
  question: string;
  missing?: string[];
  remediation_hint?: string;
}

export interface QaEvaluationPayload {
  narrative: string;
  questions: QaQuestion[];
  near_miss_gaps: QaNearMissGap[];
  generated_for_version?: number | null;
  current_change_version?: number | null;
  stale?: boolean;
  advisor_error?: string | null;
  evaluated_at?: string | null;
  analyzer_version?: string | null;
  // Wizard-only: when the parent wants to render an ephemeral preview
  // without ever fetching/persisting, the parent provides the payload and
  // we treat it as the only source of truth.
}

export type QaVerdict = "answerable" | "partially" | "no" | "out_of_scope";

interface QaExecuteResult {
  status?: "ok" | "failed" | "refused";
  error?: "not_deployed";
  deployment_status?: string;
  sql?: string;
  columns?: Array<{ name: string; dataType: string }>;
  rows?: unknown[][];
  truncated?: boolean;
  row_count?: number;
  duration_ms?: number;
  view_name?: string;
  view_schema?: string;
  explanation?: string;
  // Phase 3a skill-based path
  aggregation_kind?: "raw" | "grouped" | "joined" | "trend" | "n/a";
  confidence?: "high" | "medium" | "low" | "n/a";
  refused_reason?: string;
  question_text?: string;
  error_class?: string;
  error_message?: string;
}

export interface QaProbeResult {
  question: string;
  verdict: QaVerdict;
  confidence: QaConfidence;
  reasoning: string;
  supporting_columns: string[];
  supporting_rules: string[];
  gaps: Array<{ category: string; detail: string; remediation_hint?: string }>;
  advisor_error?: string | null;
}

interface Props {
  // One of these two identifies the product. When projectId is present we
  // hit the engineer endpoints (POST /qa/evaluate, POST /qa/probe).
  // marketplaceUri is the dprod URI; we use it for the public probe when
  // projectId is null.
  projectId?: number | null;
  marketplaceUri?: string | null;

  // When provided, skip the initial fetch and render this directly. Used by
  // the wizard preview and by the marketplace detail when the latest eval
  // is already in `detail.qa_evaluation`.
  initial?: QaEvaluationPayload | null;

  // Toggle the Regenerate button. Owner / engineer / PO get it; anonymous
  // consumers don't.
  canRegenerate?: boolean;

  // Toggle the probe textarea. On by default for the marketplace + project
  // surfaces; off for the wizard preview (no contract yet to probe).
  canProbe?: boolean;

  // Suppress persistence on regenerate. Wizard sets this so a preview
  // doesn't write a :QAEvaluation row before the contract is even saved.
  persistOnRegenerate?: boolean;

  // Called after a successful regenerate so the parent can refresh adjacent
  // state (e.g. the marketplace re-fetches the whole detail).
  onRegenerated?: (payload: QaEvaluationPayload) => void;

  // Gate the Run-all button. Default `undefined` keeps current behavior
  // (button visible whenever `canExecute` is true) for the marketplace +
  // engineer-dashboard surfaces. The wizard preview passes `false` so
  // the button stays hidden pre-deploy — there is no virtual view to run
  // questions against until the engineer materializes the product.
  isDeployed?: boolean;
}

const CATEGORY_PALETTE: Record<string, { bg: string; fg: string }> = {
  descriptive: { bg: "#eff6ff", fg: "#1d4ed8" },
  comparative: { bg: "#f0fdf4", fg: "#15803d" },
  trend: { bg: "#fefce8", fg: "#a16207" },
  segmentation: { bg: "#faf5ff", fg: "#7e22ce" },
  drilldown: { bg: "#fff7ed", fg: "#c2410c" },
  relational: { bg: "#ecfeff", fg: "#0e7490" },
};

const VERDICT_PALETTE: Record<QaVerdict, { bg: string; fg: string; label: string }> = {
  answerable: { bg: "#f0fdf4", fg: "#15803d", label: "Answerable" },
  partially: { bg: "#fefce8", fg: "#a16207", label: "Partially answerable" },
  no: { bg: "#fef2f2", fg: "#991b1b", label: "Not answerable" },
  out_of_scope: { bg: "#f1f5f9", fg: "#475569", label: "Out of scope" },
};

const CONFIDENCE_DOT: Record<QaConfidence, string> = {
  high: "#16a34a",
  medium: "#d97706",
  low: "#dc2626",
};

// Run-all bucketing. Each question's QaExecuteResult lands in exactly one
// bucket; the chip on the question card and the summary line above the list
// both read from this map.
type RunBucket =
  | "ok"
  | "no_rows"
  | "refused"
  | "failed"
  | "not_deployed"
  | "skipped";

const BUCKET_PALETTE: Record<RunBucket, { bg: string; fg: string; label: string }> = {
  ok: { bg: "#f0fdf4", fg: "#15803d", label: "ok" },
  no_rows: { bg: "#fefce8", fg: "#a16207", label: "0 rows" },
  refused: { bg: "#fff7ed", fg: "#c2410c", label: "refused" },
  failed: { bg: "#fef2f2", fg: "#991b1b", label: "failed" },
  not_deployed: { bg: "#f1f5f9", fg: "#475569", label: "not deployed" },
  skipped: { bg: "#f1f5f9", fg: "#94a3b8", label: "skipped" },
};

type RunEntry = QaExecuteResult | { skipped: true };

const bucketOf = (r: RunEntry): RunBucket => {
  if ("skipped" in r) return "skipped";
  if (r.error === "not_deployed") return "not_deployed";
  if (r.status === "refused") return "refused";
  if (r.status === "failed") return "failed";
  if (r.status === "ok") return (r.row_count ?? 0) > 0 ? "ok" : "no_rows";
  return "failed";
};

export default function QuestionsPanel({
  projectId = null,
  marketplaceUri = null,
  initial = null,
  canRegenerate = false,
  canProbe = true,
  persistOnRegenerate = true,
  onRegenerated,
  isDeployed,
}: Props) {
  const [payload, setPayload] = useState<QaEvaluationPayload | null>(initial);
  const [loading, setLoading] = useState(initial === null && projectId !== null);
  const [regenerating, setRegenerating] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);

  const [probeText, setProbeText] = useState("");
  const [probeResult, setProbeResult] = useState<QaProbeResult | null>(null);
  const [probing, setProbing] = useState(false);
  const [probeError, setProbeError] = useState<string | null>(null);

  // Phase 3a: per-question Execute. Each clicked question expands an inline
  // result card showing the synthesized SELECT + columns + rows. Only one
  // question is "active" at a time to keep the UI tidy.
  const [executingIdx, setExecutingIdx] = useState<number | null>(null);
  const [executeResult, setExecuteResult] = useState<QaExecuteResult | null>(null);
  const [executeIdx, setExecuteIdx] = useState<number | null>(null);
  const [executeError, setExecuteError] = useState<string | null>(null);

  const canExecute = projectId !== null || (marketplaceUri || "").startsWith("dprod:");
  const executeEndpoint = projectId !== null
    ? `/api/projects/${projectId}/qa/execute`
    : `/api/marketplace/products/${(marketplaceUri || "").replace(/^dprod:/, "")}/qa/execute`;
  const executeQuestion = async (idx: number) => {
    if (!canExecute) return;
    setExecutingIdx(idx);
    setExecuteError(null);
    setExecuteResult(null);
    setExecuteIdx(idx);
    try {
      const res = await api.post(executeEndpoint, { question_index: idx, limit: 100 });
      setExecuteResult(res.data as QaExecuteResult);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      setExecuteError(err?.response?.data?.detail || err?.message || "Execute failed");
      setExecuteResult(null);
    } finally {
      setExecutingIdx(null);
    }
  };

  // Run-all: sequential validator that fires every curated question through
  // the same /qa/execute endpoint and reports per-question status. The map is
  // keyed by question index; clicking a per-row chip opens the cached result
  // in the same inline expand panel used by single-question Execute.
  const [runResults, setRunResults] = useState<Map<number, RunEntry>>(new Map());
  const [runAllPhase, setRunAllPhase] = useState<"idle" | "running" | "done">("idle");
  const [runCurrentIdx, setRunCurrentIdx] = useState<number | null>(null);
  const cancelRef = useRef(false);

  const runAll = async () => {
    if (!payload || !canExecute || runAllPhase === "running") return;
    cancelRef.current = false;
    setRunResults(new Map());
    setRunAllPhase("running");
    setExecuteIdx(null);
    setExecuteResult(null);
    setExecuteError(null);
    const next = new Map<number, RunEntry>();
    for (let i = 0; i < payload.questions.length; i++) {
      if (cancelRef.current) break;
      setRunCurrentIdx(i);
      const q = payload.questions[i];
      if (!q || (q.supporting_columns || []).length === 0) {
        // Mirrors today's Execute-button gate at the per-row level: a
        // question with no supporting columns has nothing for the executor
        // to ground SQL in, so we mark it skipped rather than burning a
        // call.
        next.set(i, { skipped: true });
        setRunResults(new Map(next));
        continue;
      }
      try {
        const res = await api.post(executeEndpoint, { question_index: i, limit: 100 });
        const data = res.data as QaExecuteResult;
        next.set(i, data);
        setRunResults(new Map(next));
        if (data.error === "not_deployed") {
          // The view isn't deployed — every subsequent call would return the
          // same thing. Stop early so the user sees the real signal.
          break;
        }
      } catch (e: unknown) {
        const err = e as { response?: { data?: { detail?: string } }; message?: string };
        next.set(i, {
          status: "failed",
          error_class: "request_error",
          error_message: err?.response?.data?.detail || err?.message || "Request failed",
        });
        setRunResults(new Map(next));
      }
    }
    setRunCurrentIdx(null);
    setRunAllPhase("done");
  };

  const cancelRunAll = () => {
    cancelRef.current = true;
  };

  const showCachedResult = (idx: number) => {
    const r = runResults.get(idx);
    if (!r || "skipped" in r) return;
    setExecuteIdx(idx);
    setExecuteResult(r);
    setExecuteError(null);
  };

  const runSummary = useMemo(() => {
    const counts: Record<RunBucket, number> = {
      ok: 0, no_rows: 0, refused: 0, failed: 0, not_deployed: 0, skipped: 0,
    };
    runResults.forEach((r) => { counts[bucketOf(r)] += 1; });
    return counts;
  }, [runResults]);

  useEffect(() => {
    if (initial !== null || projectId === null) return;
    api
      .get(`/api/projects/${projectId}/qa/evaluation`)
      .then((r) => {
        const ev = r.data?.evaluation;
        if (ev) {
          setPayload({
            narrative: ev.narrative || "",
            questions: ev.questions || [],
            near_miss_gaps: ev.near_miss_gaps || [],
            generated_for_version: ev.generated_for_version,
            current_change_version: ev.current_change_version,
            stale: !!ev.stale,
            advisor_error: ev.advisor_error,
            evaluated_at: ev.evaluated_at,
            analyzer_version: ev.analyzer_version,
          });
        } else {
          setPayload(null);
        }
        setFetchError(null);
      })
      .catch((e) => {
        setFetchError((e as Error).message || "Failed to load QA evaluation");
      })
      .finally(() => setLoading(false));
  }, [projectId, initial]);

  const handleRegenerate = async () => {
    if (projectId === null || regenerating) return;
    setRegenerating(true);
    setFetchError(null);
    try {
      const params = persistOnRegenerate ? "" : "?persist=false";
      const res = await api.post(
        `/api/projects/${projectId}/qa/evaluate${params}`,
        { trigger: "manual" }
      );
      const next: QaEvaluationPayload = {
        narrative: res.data.narrative || "",
        questions: res.data.questions || [],
        near_miss_gaps: res.data.near_miss_gaps || [],
        generated_for_version: res.data.generated_for_version,
        current_change_version: res.data.current_change_version,
        stale: false,
        advisor_error: res.data.advisor_error,
        evaluated_at: new Date().toISOString(),
        analyzer_version: null,
      };
      setPayload(next);
      if (onRegenerated) onRegenerated(next);
    } catch (e) {
      const msg =
        (e as { response?: { data?: { detail?: string } }; message?: string }).response
          ?.data?.detail ||
        (e as Error).message ||
        "Regeneration failed";
      setFetchError(msg);
    } finally {
      setRegenerating(false);
    }
  };

  const handleProbe = async () => {
    const q = probeText.trim();
    if (!q || probing) return;
    setProbing(true);
    setProbeError(null);
    setProbeResult(null);
    try {
      let res;
      if (projectId !== null) {
        res = await api.post(`/api/projects/${projectId}/qa/probe`, {
          question: q,
        });
      } else if (marketplaceUri) {
        res = await api.post(`/api/marketplace/qa/probe`, {
          uri: marketplaceUri,
          question: q,
        });
      } else {
        throw new Error("No project or marketplace URI to probe against");
      }
      setProbeResult(res.data);
    } catch (e) {
      const msg =
        (e as { response?: { data?: { detail?: string } }; message?: string }).response
          ?.data?.detail ||
        (e as Error).message ||
        "Probe failed";
      setProbeError(msg);
    } finally {
      setProbing(false);
    }
  };

  if (loading) {
    return (
      <div style={styles.empty}>Loading question analysis…</div>
    );
  }

  if (!payload && !fetchError) {
    return (
      <div style={styles.empty}>
        <div style={{ marginBottom: 12, color: "#475569" }}>
          No question analysis yet — run the analyzer to surface what this
          product can answer and where it has gaps.
        </div>
        {canRegenerate && projectId !== null && (
          <button
            type="button"
            onClick={handleRegenerate}
            disabled={regenerating}
            style={styles.primaryBtn}
          >
            {regenerating ? "Analyzing…" : "Run analysis"}
          </button>
        )}
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {fetchError && (
        <div style={styles.error}>{fetchError}</div>
      )}
      {payload && (
        <>
          <div style={styles.headerRow}>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <div style={styles.title}>What this product can answer</div>
              {payload.evaluated_at && (
                <div style={{ fontSize: 11, color: "#64748b" }}>
                  Last analyzed {new Date(payload.evaluated_at).toLocaleString()}
                  {payload.generated_for_version != null && (
                    <> · contract v{payload.generated_for_version}</>
                  )}
                </div>
              )}
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              {payload.stale && (
                <span style={styles.staleBadge} title="The contract has changed since this analysis was generated.">
                  Stale — contract has changed
                </span>
              )}
              {canExecute && isDeployed !== false && payload.questions.length > 0 && (
                runAllPhase === "running" ? (
                  <button
                    type="button"
                    onClick={cancelRunAll}
                    style={styles.secondaryBtn}
                    title="Stop after the current question finishes"
                  >
                    Cancel ({(runCurrentIdx ?? 0) + 1}/{payload.questions.length})
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={runAll}
                    style={styles.secondaryBtn}
                    title="Execute every question sequentially and report which ones fail to produce SQL or run against the deployed view"
                  >
                    {runAllPhase === "done" ? "Re-run all" : "Run all"}
                  </button>
                )
              )}
              {canRegenerate && projectId !== null && (
                <button
                  type="button"
                  onClick={handleRegenerate}
                  disabled={regenerating}
                  style={styles.primaryBtn}
                >
                  {regenerating ? "Analyzing…" : payload.stale ? "Regenerate" : "Re-run analysis"}
                </button>
              )}
            </div>
          </div>

          {payload.advisor_error && (
            <div style={styles.warn}>
              Analyzer reported an issue: {payload.advisor_error}
            </div>
          )}

          {payload.narrative && (
            <div style={styles.narrative}>{payload.narrative}</div>
          )}

          {payload.questions.length > 0 && (
            <div>
              <div style={styles.sectionLabel}>
                Questions ({payload.questions.length})
              </div>
              {runResults.size > 0 && (
                <div style={styles.runSummary}>
                  <span style={{ fontWeight: 700, color: "#0f172a" }}>
                    {runAllPhase === "running" ? "Running…" : "Run all:"}
                  </span>
                  {(["ok", "no_rows", "refused", "failed", "not_deployed", "skipped"] as RunBucket[]).map((b) => {
                    const count = runSummary[b];
                    if (count === 0) return null;
                    const p = BUCKET_PALETTE[b];
                    return (
                      <span
                        key={b}
                        style={{ ...styles.bucketBadge, backgroundColor: p.bg, color: p.fg }}
                        title={
                          b === "ok" ? "SQL executed and returned at least one row"
                          : b === "no_rows" ? "SQL executed but the view returned 0 rows — view is deployed but has no data for this question"
                          : b === "refused" ? "The executor skill declined to author SQL"
                          : b === "failed" ? "Either no SQL was generated, the SQL referenced non-allow-listed tables, or Postgres rejected the SQL"
                          : b === "not_deployed" ? "The product's virtual view is not deployed — run-all stopped early"
                          : "Question had no supporting_columns and was skipped"
                        }
                      >
                        {count} {p.label}
                      </span>
                    );
                  })}
                  {runAllPhase === "done" && runResults.size < payload.questions.length && (
                    <span style={{ fontSize: 11, color: "#64748b" }}>
                      ({payload.questions.length - runResults.size} not run)
                    </span>
                  )}
                </div>
              )}
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {payload.questions.map((q, i) => {
                  const cat = (q.category || "").toLowerCase();
                  const palette =
                    CATEGORY_PALETTE[cat] || { bg: "#f1f5f9", fg: "#475569" };
                  return (
                    <div key={i} style={styles.questionCard}>
                      <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
                        {q.category && (
                          <span
                            style={{
                              ...styles.chip,
                              backgroundColor: palette.bg,
                              color: palette.fg,
                            }}
                          >
                            {q.category}
                          </span>
                        )}
                        {runCurrentIdx === i && runAllPhase === "running" && (
                          <span style={{ ...styles.chip, backgroundColor: "#dbeafe", color: "#1e40af" }}>
                            running…
                          </span>
                        )}
                        {runResults.has(i) && (() => {
                          const entry = runResults.get(i)!;
                          const bucket = bucketOf(entry);
                          const p = BUCKET_PALETTE[bucket];
                          const clickable = !("skipped" in entry);
                          return (
                            <span
                              onClick={clickable ? () => showCachedResult(i) : undefined}
                              style={{
                                ...styles.chip,
                                backgroundColor: p.bg,
                                color: p.fg,
                                cursor: clickable ? "pointer" : "default",
                                textDecoration: clickable && executeIdx === i ? "underline" : "none",
                              }}
                              title={
                                clickable
                                  ? "Click to view the SQL / rows / error from Run all"
                                  : "Skipped — no supporting_columns to ground SQL in"
                              }
                            >
                              {p.label}
                            </span>
                          );
                        })()}
                        <div style={{ flex: 1, fontSize: 14, color: "#0f172a" }}>
                          {q.text}
                        </div>
                        {q.confidence && (
                          <span
                            style={styles.confidenceDot}
                            title={`Confidence: ${q.confidence}`}
                          >
                            <span
                              style={{
                                ...styles.confidenceDotInner,
                                backgroundColor: CONFIDENCE_DOT[q.confidence],
                              }}
                            />
                            {q.confidence}
                          </span>
                        )}
                      </div>
                      {q.rationale && (
                        <div style={{ fontSize: 12, color: "#475569", marginTop: 6 }}>
                          {q.rationale}
                        </div>
                      )}
                      {(q.supporting_columns || []).length > 0 && (
                        <div style={styles.supportingRow}>
                          <span style={{ color: "#94a3b8", marginRight: 6 }}>columns:</span>
                          {(q.supporting_columns || []).map((c) => (
                            <code key={c} style={styles.codeChip}>{c}</code>
                          ))}
                        </div>
                      )}
                      {(q.supporting_rules || []).length > 0 && (
                        <div style={styles.supportingRow}>
                          <span style={{ color: "#94a3b8", marginRight: 6 }}>rules:</span>
                          {(q.supporting_rules || []).map((r) => (
                            <code key={r} style={styles.codeChip}>{r}</code>
                          ))}
                        </div>
                      )}
                      {canExecute && (q.supporting_columns || []).length > 0 && (
                        <div style={{ marginTop: 10, display: "flex", alignItems: "center", gap: 8 }}>
                          <button
                            type="button"
                            onClick={() => executeQuestion(i)}
                            disabled={executingIdx === i}
                            style={{
                              fontSize: 12, padding: "4px 10px",
                              border: "1px solid #cbd5e1", borderRadius: 5,
                              backgroundColor: executingIdx === i ? "#f1f5f9" : "#fff",
                              color: "#334155", cursor: executingIdx === i ? "wait" : "pointer",
                              fontWeight: 600,
                            }}
                            title="Hand this question to the executor skill, which authors SQL against the deployed views"
                          >
                            {executingIdx === i ? "Synthesising…" : "Execute"}
                          </button>
                          {executeIdx === i && executeResult && executeResult.row_count !== undefined && (
                            <span style={{ fontSize: 11, color: "#64748b" }}>
                              {executeResult.row_count} row{executeResult.row_count === 1 ? "" : "s"} ·
                              {" "}{executeResult.view_schema}.{executeResult.view_name} ·
                              {" "}{executeResult.duration_ms}ms
                            </span>
                          )}
                          {executeIdx === i && (executeResult || executeError) && (
                            <button
                              type="button"
                              onClick={() => { setExecuteIdx(null); setExecuteResult(null); setExecuteError(null); }}
                              style={{
                                fontSize: 11, padding: "2px 6px",
                                border: "none", background: "transparent",
                                color: "#64748b", cursor: "pointer",
                              }}
                            >
                              Hide
                            </button>
                          )}
                        </div>
                      )}
                      {canExecute && executeIdx === i && (
                        <div style={{ marginTop: 10, padding: 10, border: "1px solid #e2e8f0", borderRadius: 6, backgroundColor: "#f8fafc" }}>
                          {executingIdx === i && !executeResult && !executeError && (
                            <div style={{ fontSize: 12, color: "#64748b", fontStyle: "italic" }}>
                              Synthesizing SQL and running against the deployed view…
                            </div>
                          )}
                          {executeError && (
                            <div style={{ color: "#991b1b", fontSize: 12 }}>{executeError}</div>
                          )}
                          {executeResult?.error === "not_deployed" && (
                            <div style={{ color: "#92400e", fontSize: 12 }}>
                              The product hasn't been deployed yet — run the Deploy Virtual View stage first.
                            </div>
                          )}
                          {executeResult?.status === "refused" && (
                            <div style={{
                              padding: 10, borderRadius: 4,
                              backgroundColor: "#fffbeb", color: "#92400e",
                              border: "1px solid #fed7aa", fontSize: 12,
                            }}>
                              <div style={{ fontWeight: 600, marginBottom: 4 }}>Executor declined this question</div>
                              <div>{executeResult.refused_reason}</div>
                            </div>
                          )}
                          {executeResult?.status === "failed" && (
                            <div style={{ color: "#991b1b", fontSize: 12 }}>
                              {executeResult.error_class}: {executeResult.error_message}
                              {executeResult.sql && (
                                <pre style={{
                                  margin: "8px 0 0 0", padding: 8,
                                  backgroundColor: "#0f172a", color: "#fda4af",
                                  fontSize: 11, lineHeight: 1.5, overflow: "auto",
                                  fontFamily: "'Fira Code', monospace",
                                  borderRadius: 4, whiteSpace: "pre-wrap",
                                }}>{executeResult.sql}</pre>
                              )}
                            </div>
                          )}
                          {executeResult?.status === "ok" && (
                            <>
                              {(executeResult.aggregation_kind && executeResult.aggregation_kind !== "raw") && (
                                <div style={{ marginBottom: 6 }}>
                                  <span style={{
                                    fontSize: 10, padding: "2px 6px", borderRadius: 4,
                                    backgroundColor: "#dbeafe", color: "#1e40af",
                                    fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.05em",
                                  }}>{executeResult.aggregation_kind}</span>
                                  {executeResult.confidence && executeResult.confidence !== "n/a" && (
                                    <span style={{
                                      marginLeft: 6, fontSize: 10, color: "#64748b",
                                    }}>
                                      confidence: {executeResult.confidence}
                                    </span>
                                  )}
                                </div>
                              )}
                              {executeResult.sql && (
                                <pre style={{
                                  margin: 0, marginBottom: 8, padding: 8,
                                  backgroundColor: "#0f172a", color: "#a5f3fc",
                                  fontSize: 11, lineHeight: 1.5, overflow: "auto",
                                  fontFamily: "'Fira Code', monospace",
                                  borderRadius: 4, whiteSpace: "pre-wrap",
                                }}>{executeResult.sql}</pre>
                              )}
                              {executeResult.explanation && (
                                <div style={{ fontSize: 11, color: "#64748b", fontStyle: "italic", marginBottom: 8 }}>
                                  {executeResult.explanation}
                                </div>
                              )}
                              {(executeResult.rows || []).length === 0 ? (
                                <div style={{ fontSize: 12, color: "#94a3b8", fontStyle: "italic" }}>
                                  The deployed view returned 0 rows.
                                </div>
                              ) : (
                                <div style={{ overflow: "auto", maxHeight: 320, border: "1px solid #e2e8f0", borderRadius: 4 }}>
                                  <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11, fontFamily: "'Fira Code', monospace" }}>
                                    <thead>
                                      <tr>
                                        {(executeResult.columns || []).map((c, ci) => (
                                          <th key={ci} style={{
                                            position: "sticky", top: 0,
                                            backgroundColor: "#f1f5f9", borderBottom: "1px solid #e2e8f0",
                                            padding: "5px 8px", textAlign: "left",
                                            fontWeight: 600, color: "#0f172a", whiteSpace: "nowrap",
                                          }}>{c.name}</th>
                                        ))}
                                      </tr>
                                    </thead>
                                    <tbody>
                                      {(executeResult.rows || []).map((r, ri) => (
                                        <tr key={ri} style={{ backgroundColor: ri % 2 === 0 ? "#fff" : "#f8fafc" }}>
                                          {(r as unknown[]).map((v, vi) => (
                                            <td key={vi} style={{
                                              padding: "4px 8px",
                                              borderBottom: "1px solid #f1f5f9",
                                              color: v === null ? "#94a3b8" : "#0f172a",
                                              fontStyle: v === null ? "italic" : "normal",
                                              whiteSpace: "nowrap", maxWidth: 240,
                                              overflow: "hidden", textOverflow: "ellipsis",
                                            }} title={v === null ? "null" : String(v)}>
                                              {v === null ? "null" : String(v)}
                                            </td>
                                          ))}
                                        </tr>
                                      ))}
                                    </tbody>
                                  </table>
                                </div>
                              )}
                              {executeResult.truncated && (
                                <div style={{ marginTop: 6, fontSize: 11, color: "#92400e" }}>
                                  Truncated — showing first {executeResult.row_count} row(s).
                                </div>
                              )}
                            </>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {payload.near_miss_gaps.length > 0 && (
            <div>
              <div style={styles.sectionLabel}>
                Near-miss gaps ({payload.near_miss_gaps.length})
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {payload.near_miss_gaps.map((g, i) => (
                  <div key={i} style={styles.gapCard}>
                    <div style={{ fontSize: 14, color: "#0f172a" }}>{g.question}</div>
                    {(g.missing || []).length > 0 && (
                      <div style={styles.supportingRow}>
                        <span style={{ color: "#94a3b8", marginRight: 6 }}>missing:</span>
                        {(g.missing || []).map((m) => (
                          <code key={m} style={styles.codeChip}>{m}</code>
                        ))}
                      </div>
                    )}
                    {g.remediation_hint && (
                      <div style={{ fontSize: 12, color: "#475569", marginTop: 6 }}>
                        <span style={{ color: "#94a3b8" }}>fix: </span>
                        {g.remediation_hint}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}

      {canProbe && (projectId !== null || marketplaceUri) && (
        <div style={styles.probeBlock}>
          <div style={styles.sectionLabel}>Ask a question of this product</div>
          <textarea
            value={probeText}
            onChange={(e) => setProbeText(e.target.value)}
            placeholder="e.g. What was the total revenue by region last quarter?"
            rows={3}
            style={styles.textarea}
          />
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button
              type="button"
              onClick={handleProbe}
              disabled={!probeText.trim() || probing}
              style={styles.primaryBtn}
            >
              {probing ? "Checking…" : "Check"}
            </button>
          </div>
          {probeError && <div style={styles.error}>{probeError}</div>}
          {probeResult && (
            <div
              style={{
                ...styles.verdictCard,
                backgroundColor: VERDICT_PALETTE[probeResult.verdict].bg,
                borderColor: VERDICT_PALETTE[probeResult.verdict].fg,
              }}
            >
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
                <span
                  style={{
                    fontWeight: 700,
                    fontSize: 13,
                    color: VERDICT_PALETTE[probeResult.verdict].fg,
                  }}
                >
                  {VERDICT_PALETTE[probeResult.verdict].label}
                </span>
                <span
                  style={styles.confidenceDot}
                  title={`Confidence: ${probeResult.confidence}`}
                >
                  <span
                    style={{
                      ...styles.confidenceDotInner,
                      backgroundColor: CONFIDENCE_DOT[probeResult.confidence],
                    }}
                  />
                  {probeResult.confidence}
                </span>
              </div>
              {probeResult.reasoning && (
                <div style={{ fontSize: 13, color: "#0f172a", marginBottom: 8 }}>
                  {probeResult.reasoning}
                </div>
              )}
              {(probeResult.supporting_columns || []).length > 0 && (
                <div style={styles.supportingRow}>
                  <span style={{ color: "#94a3b8", marginRight: 6 }}>columns:</span>
                  {(probeResult.supporting_columns || []).map((c) => (
                    <code key={c} style={styles.codeChip}>{c}</code>
                  ))}
                </div>
              )}
              {(probeResult.supporting_rules || []).length > 0 && (
                <div style={styles.supportingRow}>
                  <span style={{ color: "#94a3b8", marginRight: 6 }}>rules:</span>
                  {(probeResult.supporting_rules || []).map((r) => (
                    <code key={r} style={styles.codeChip}>{r}</code>
                  ))}
                </div>
              )}
              {(probeResult.gaps || []).length > 0 && (
                <div style={{ marginTop: 8 }}>
                  <div
                    style={{
                      fontSize: 12,
                      fontWeight: 700,
                      color: "#0f172a",
                      marginBottom: 4,
                    }}
                  >
                    Gaps
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                    {(probeResult.gaps || []).map((g, i) => (
                      <div key={i} style={{ fontSize: 12, color: "#0f172a" }}>
                        <code style={styles.codeChip}>{g.category}</code>{" "}
                        {g.detail}
                        {g.remediation_hint && (
                          <span style={{ color: "#475569" }}>
                            {" "}
                            — fix: {g.remediation_hint}
                          </span>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {probeResult.advisor_error && (
                <div style={styles.warn}>{probeResult.advisor_error}</div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const styles: Record<string, CSSProperties> = {
  empty: {
    padding: 24,
    textAlign: "center",
    color: "#64748b",
    fontSize: 13,
    backgroundColor: "#f8fafc",
    border: "1px dashed #cbd5e1",
    borderRadius: 8,
  },
  headerRow: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    gap: 12,
  },
  title: { fontSize: 14, fontWeight: 700, color: "#0f172a" },
  staleBadge: {
    padding: "2px 8px",
    borderRadius: 999,
    fontSize: 11,
    fontWeight: 600,
    backgroundColor: "#fef3c7",
    color: "#92400e",
    border: "1px solid #fde68a",
  },
  primaryBtn: {
    // Aligned with OsiAnalysisPanel's btnPrimary so both panels render
    // consistent action buttons when stacked on the wizard's readiness
    // review step (and on the marketplace product detail page).
    padding: "6px 14px",
    borderRadius: 6,
    border: "none",
    backgroundColor: "#0f172a",
    color: "#fff",
    fontSize: 12,
    fontWeight: 600,
    cursor: "pointer",
  },
  secondaryBtn: {
    padding: "6px 14px",
    borderRadius: 6,
    border: "1px solid #cbd5e1",
    backgroundColor: "#fff",
    color: "#334155",
    fontSize: 12,
    fontWeight: 600,
    cursor: "pointer",
  },
  runSummary: {
    display: "flex",
    flexWrap: "wrap",
    gap: 6,
    alignItems: "center",
    fontSize: 12,
    padding: "6px 8px",
    marginBottom: 8,
    border: "1px solid #e2e8f0",
    borderRadius: 6,
    backgroundColor: "#f8fafc",
  },
  bucketBadge: {
    padding: "2px 8px",
    borderRadius: 999,
    fontSize: 11,
    fontWeight: 700,
  },
  narrative: {
    padding: 12,
    backgroundColor: "#f8fafc",
    border: "1px solid #e2e8f0",
    borderRadius: 8,
    color: "#0f172a",
    fontSize: 13,
    whiteSpace: "pre-wrap",
  },
  sectionLabel: {
    fontSize: 11,
    fontWeight: 700,
    textTransform: "uppercase",
    color: "#64748b",
    letterSpacing: 0.4,
    marginBottom: 6,
  },
  questionCard: {
    padding: 12,
    backgroundColor: "#fff",
    border: "1px solid #e2e8f0",
    borderRadius: 8,
  },
  gapCard: {
    padding: 12,
    backgroundColor: "#fefce8",
    border: "1px solid #fef08a",
    borderRadius: 8,
  },
  chip: {
    padding: "2px 8px",
    borderRadius: 999,
    fontSize: 11,
    fontWeight: 600,
  },
  confidenceDot: {
    display: "inline-flex",
    alignItems: "center",
    gap: 4,
    fontSize: 11,
    color: "#475569",
  },
  confidenceDotInner: {
    width: 8,
    height: 8,
    borderRadius: 999,
    display: "inline-block",
  },
  supportingRow: {
    marginTop: 6,
    display: "flex",
    flexWrap: "wrap",
    gap: 4,
    alignItems: "center",
    fontSize: 12,
  },
  codeChip: {
    padding: "1px 6px",
    borderRadius: 4,
    backgroundColor: "#f1f5f9",
    color: "#0f172a",
    fontSize: 11,
    fontFamily: "ui-monospace, SFMono-Regular, monospace",
  },
  probeBlock: {
    marginTop: 8,
    padding: 12,
    border: "1px solid #e2e8f0",
    borderRadius: 8,
    backgroundColor: "#f8fafc",
    display: "flex",
    flexDirection: "column",
    gap: 8,
  },
  textarea: {
    width: "100%",
    boxSizing: "border-box",
    padding: 8,
    fontSize: 13,
    border: "1px solid #cbd5e1",
    borderRadius: 6,
    resize: "vertical",
    fontFamily: "inherit",
  },
  error: {
    padding: 8,
    borderRadius: 6,
    backgroundColor: "#fef2f2",
    color: "#991b1b",
    fontSize: 12,
    border: "1px solid #fecaca",
  },
  warn: {
    padding: 8,
    borderRadius: 6,
    backgroundColor: "#fffbeb",
    color: "#92400e",
    fontSize: 12,
    border: "1px solid #fde68a",
  },
  verdictCard: {
    marginTop: 8,
    padding: 12,
    border: "1px solid",
    borderRadius: 8,
  },
};
