// Pre-flight coherence checks for a consumer-aligned data product.
//
// Mounted on the wizard's Step 7 (Readiness Review) alongside
// OsiAnalysisPanel + QuestionsPanel. Runs two heuristic checks on the
// graph state of the contract head:
//
//   1. Grain coherence — does the dataset's grainProse name concepts
//      that actually have corresponding schema columns?
//   2. Grouping coherence — when groupingKeysJson is non-empty, every
//      non-grouping non-PK non-suppressed column needs an aggregateFunction
//      somewhere or the grouped CTE silently emits MAX(...).
//
// Manual-fire — match the OSI / QA panel pattern. Empty state matches
// the other two so all three readiness-review panels look uniform when
// stacked.

import { useState, type CSSProperties } from "react";
import api from "../api/client";

interface GrainConcept {
  phrase: string;
  status: "matched" | "matched_with_drift" | "unmatched";
  matched_column: string | null;
  rationale: string;
}

interface GrainCoherence {
  verdict: "coherent" | "drift" | "no_grain_prose";
  concepts: GrainConcept[];
  summary: string;
}

interface GroupingIssue {
  column: string;
  missing: string;
  rationale: string;
  remediation: string;
}

interface GroupingCoherence {
  verdict: "coherent" | "drift" | "no_grouping";
  issues: GroupingIssue[];
  summary: string;
}

interface DatasetReport {
  dataset_name: string;
  dataset_uri: string;
  column_count: number;
  grouping_keys: string[];
  suppressed_columns: string[];
  grain_coherence: GrainCoherence;
  grouping_coherence: GroupingCoherence;
}

interface PreflightPayload {
  overall_verdict: "coherent" | "drift" | "unmateralised";
  datasets: DatasetReport[];
  summary: string;
}

interface Props {
  projectId: number;
}

const VERDICT_PALETTE: Record<string, { bg: string; fg: string; label: string }> = {
  coherent: { bg: "#f0fdf4", fg: "#15803d", label: "Coherent" },
  drift: { bg: "#fff7ed", fg: "#c2410c", label: "Drift detected" },
  unmateralised: { bg: "#f1f5f9", fg: "#475569", label: "Not materialised" },
  no_grain_prose: { bg: "#f1f5f9", fg: "#475569", label: "No grain prose" },
  no_grouping: { bg: "#f1f5f9", fg: "#475569", label: "No grouping keys" },
};

const STATUS_PALETTE: Record<GrainConcept["status"], { bg: string; fg: string }> = {
  matched: { bg: "#f0fdf4", fg: "#15803d" },
  matched_with_drift: { bg: "#fefce8", fg: "#a16207" },
  unmatched: { bg: "#fef2f2", fg: "#991b1b" },
};

export default function PreflightPanel({ projectId }: Props) {
  const [payload, setPayload] = useState<PreflightPayload | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const runCheck = async () => {
    if (running) return;
    setRunning(true);
    setError(null);
    try {
      const res = await api.post(`/api/projects/${projectId}/preflight`);
      setPayload(res.data as PreflightPayload);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      setError(err?.response?.data?.detail || err?.message || "Pre-flight check failed");
    } finally {
      setRunning(false);
    }
  };

  const toggleExpanded = (name: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  if (!payload && !error) {
    return (
      <div style={styles.empty}>
        <div style={{ marginBottom: 12, color: "#475569" }}>
          Check whether the dataset's grain prose names concepts the schema
          actually delivers, and whether grouped datasets have the
          aggregateFunction metadata they need before the view DDL is
          generated.
        </div>
        <button type="button" onClick={runCheck} disabled={running} style={btnPrimary}>
          {running ? "Checking…" : "Run pre-flight check"}
        </button>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {error && <div style={styles.error}>{error}</div>}
      {payload && (
        <>
          <div style={styles.headerRow}>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <div style={styles.title}>Pre-flight coherence</div>
              <div style={{ fontSize: 11, color: "#64748b" }}>{payload.summary}</div>
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <span
                style={{
                  ...styles.chip,
                  backgroundColor: VERDICT_PALETTE[payload.overall_verdict].bg,
                  color: VERDICT_PALETTE[payload.overall_verdict].fg,
                }}
              >
                {VERDICT_PALETTE[payload.overall_verdict].label}
              </span>
              <button type="button" onClick={runCheck} disabled={running} style={btnPrimary}>
                {running ? "Checking…" : "Re-run check"}
              </button>
            </div>
          </div>

          {payload.datasets.length === 0 && (
            <div style={styles.empty}>
              {payload.summary}
            </div>
          )}

          {payload.datasets.map((ds) => {
            const isOpen = expanded.has(ds.dataset_name);
            const grainBad = ds.grain_coherence.verdict === "drift";
            const groupBad = ds.grouping_coherence.verdict === "drift";
            const anyBad = grainBad || groupBad;
            return (
              <div key={ds.dataset_name} style={styles.datasetCard}>
                <div
                  style={{ ...styles.datasetHeader, cursor: "pointer" }}
                  onClick={() => toggleExpanded(ds.dataset_name)}
                >
                  <div style={{ display: "flex", gap: 8, alignItems: "center", flex: 1 }}>
                    <span style={{ fontSize: 13, fontWeight: 700, color: "#0f172a" }}>
                      {ds.dataset_name}
                    </span>
                    <span style={{ fontSize: 11, color: "#64748b" }}>
                      {ds.column_count} cols
                    </span>
                  </div>
                  <div style={{ display: "flex", gap: 6 }}>
                    <span
                      style={{
                        ...styles.chip,
                        backgroundColor: VERDICT_PALETTE[ds.grain_coherence.verdict].bg,
                        color: VERDICT_PALETTE[ds.grain_coherence.verdict].fg,
                      }}
                      title={ds.grain_coherence.summary}
                    >
                      grain: {grainBad ? "drift" : "ok"}
                    </span>
                    <span
                      style={{
                        ...styles.chip,
                        backgroundColor: VERDICT_PALETTE[ds.grouping_coherence.verdict].bg,
                        color: VERDICT_PALETTE[ds.grouping_coherence.verdict].fg,
                      }}
                      title={ds.grouping_coherence.summary}
                    >
                      grouping: {groupBad ? "drift" : "ok"}
                    </span>
                    <span style={{ fontSize: 11, color: "#64748b", marginLeft: 4 }}>
                      {isOpen ? "▾" : "▸"}
                    </span>
                  </div>
                </div>

                {isOpen && (
                  <div style={styles.datasetBody}>
                    {/* Grain coherence section */}
                    <div style={styles.sectionLabel}>Grain coherence</div>
                    <div style={styles.sectionSummary}>{ds.grain_coherence.summary}</div>
                    {ds.grain_coherence.concepts.length > 0 && (
                      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                        {ds.grain_coherence.concepts.map((c, i) => (
                          <div key={i} style={styles.conceptRow}>
                            <span
                              style={{
                                ...styles.statusChip,
                                backgroundColor: STATUS_PALETTE[c.status].bg,
                                color: STATUS_PALETTE[c.status].fg,
                              }}
                            >
                              {c.status === "matched_with_drift" ? "drift" : c.status}
                            </span>
                            <div style={{ flex: 1 }}>
                              <div style={{ fontSize: 12, color: "#0f172a" }}>
                                <code style={styles.codeChip}>{c.phrase}</code>
                                {c.matched_column && (
                                  <>
                                    {" → "}
                                    <code style={styles.codeChip}>{c.matched_column}</code>
                                  </>
                                )}
                              </div>
                              <div style={{ fontSize: 11, color: "#64748b", marginTop: 2 }}>
                                {c.rationale}
                              </div>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}

                    {/* Grouping coherence section */}
                    <div style={{ ...styles.sectionLabel, marginTop: 14 }}>
                      Grouping coherence
                    </div>
                    <div style={styles.sectionSummary}>
                      {ds.grouping_coherence.summary}
                      {ds.grouping_keys.length > 0 && (
                        <>
                          {" "}
                          Grouping keys:{" "}
                          {ds.grouping_keys.map((k) => (
                            <code key={k} style={styles.codeChip}>{k}</code>
                          ))}
                        </>
                      )}
                    </div>
                    {ds.grouping_coherence.issues.length > 0 && (
                      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                        {ds.grouping_coherence.issues.map((issue) => (
                          <div key={issue.column} style={styles.issueRow}>
                            <div style={{ fontSize: 12, color: "#991b1b", fontWeight: 600 }}>
                              <code style={styles.codeChip}>{issue.column}</code>
                              {" — missing "}
                              {issue.missing}
                            </div>
                            <div style={{ fontSize: 11, color: "#475569", marginTop: 3 }}>
                              {issue.rationale}
                            </div>
                            <div
                              style={{
                                fontSize: 11,
                                color: "#0f172a",
                                marginTop: 4,
                                fontStyle: "italic",
                              }}
                            >
                              Fix: {issue.remediation}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}

                    {!anyBad && (
                      <div
                        style={{
                          fontSize: 12,
                          color: "#15803d",
                          marginTop: 10,
                          fontStyle: "italic",
                        }}
                      >
                        No drift detected on this dataset.
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </>
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
  chip: {
    padding: "2px 8px",
    borderRadius: 999,
    fontSize: 11,
    fontWeight: 700,
  },
  statusChip: {
    padding: "1px 6px",
    borderRadius: 4,
    fontSize: 10,
    fontWeight: 700,
    textTransform: "uppercase",
    minWidth: 50,
    textAlign: "center",
    letterSpacing: 0.3,
  },
  datasetCard: {
    backgroundColor: "#fff",
    border: "1px solid #e2e8f0",
    borderRadius: 8,
  },
  datasetHeader: {
    padding: "10px 14px",
    display: "flex",
    alignItems: "center",
    gap: 8,
  },
  datasetBody: {
    padding: "10px 14px 14px 14px",
    borderTop: "1px solid #e2e8f0",
  },
  sectionLabel: {
    fontSize: 11,
    fontWeight: 700,
    color: "#64748b",
    textTransform: "uppercase",
    letterSpacing: 0.4,
    marginBottom: 4,
  },
  sectionSummary: {
    fontSize: 12,
    color: "#475569",
    marginBottom: 8,
  },
  conceptRow: {
    display: "flex",
    gap: 8,
    alignItems: "flex-start",
    padding: 8,
    backgroundColor: "#f8fafc",
    border: "1px solid #e2e8f0",
    borderRadius: 6,
  },
  issueRow: {
    padding: 8,
    backgroundColor: "#fef2f2",
    border: "1px solid #fecaca",
    borderRadius: 6,
  },
  codeChip: {
    padding: "1px 6px",
    borderRadius: 4,
    backgroundColor: "#f1f5f9",
    color: "#0f172a",
    fontSize: 11,
    fontFamily: "ui-monospace, SFMono-Regular, monospace",
    marginRight: 4,
  },
  error: {
    padding: 8,
    borderRadius: 6,
    backgroundColor: "#fef2f2",
    color: "#991b1b",
    fontSize: 12,
    border: "1px solid #fecaca",
  },
};

const btnPrimary: CSSProperties = {
  padding: "6px 14px",
  borderRadius: 6,
  border: "none",
  backgroundColor: "#0f172a",
  color: "#fff",
  fontSize: 12,
  fontWeight: 600,
  cursor: "pointer",
};
