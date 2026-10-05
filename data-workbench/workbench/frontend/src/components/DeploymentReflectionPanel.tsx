// Renders the most recent :DeploymentReflection for a data product.
// Mirrors OsiAnalysisPanel layout: verdict header + narrative + structured
// findings (surprises, alignment checks, recommendations). Read-only in v1.
//
// Engineer mode: passes `projectId` and renders a Re-run button.
// Marketplace mode: passes `contractId` and skips the Re-run button.

import { useEffect, useState, type CSSProperties } from "react";
import api from "../api/client";

export type ReflectionVerdict = "aligned" | "minor_issues" | "misaligned" | "unknown";
export type Severity = "low" | "medium" | "high";
export type RuleAlignmentStatus = "aligned" | "violated" | "unverifiable";
export type DescriptionAlignmentStatus = "aligned" | "partial" | "mismatched" | "unverifiable";
export type QaAlignmentStatus = "answerable" | "partial" | "unanswerable";

interface Surprise {
  kind?: string;
  severity?: Severity;
  column_uri?: string;
  column_name?: string;
  dataset_uri?: string;
  evidence?: string;
  cited_signal?: string;
  sample_artifact_risk?: Severity;
}

interface DescriptionAlignment {
  column_uri?: string;
  status?: DescriptionAlignmentStatus;
  note?: string;
}

interface RuleAlignment {
  column_uri?: string;
  rule_type?: string;
  status?: RuleAlignmentStatus;
  note?: string;
}

interface QaAlignment {
  question_text?: string;
  status?: QaAlignmentStatus;
  supporting_columns_present?: boolean;
  note?: string;
}

interface Recommendation {
  priority?: Severity;
  action?: string;
  related_columns?: string[];
}

export interface ReflectionPayload {
  uri: string;
  batch_id: string;
  verdict: ReflectionVerdict;
  narrative: string;
  surprises: Surprise[];
  description_alignment: DescriptionAlignment[];
  rule_alignment: RuleAlignment[];
  qa_alignment: QaAlignment[];
  recommendations: Recommendation[];
  preview_dataset_count?: number;
  preview_rows_sampled?: number;
  advisor_error?: string | null;
  evaluator_version?: string;
  evaluated_at?: string | null;
  triggered_by?: string;
  contract_version?: number;
}

interface Props {
  // Exactly one of these must be set. projectId enables Re-run.
  projectId?: number;
  contractId?: string;
}

const VERDICT_PALETTE: Record<ReflectionVerdict, { bg: string; fg: string; dot: string; headline: string }> = {
  aligned: { bg: "#f0fdf4", fg: "#065f46", dot: "#16a34a", headline: "Deployed view matches the declared shape" },
  minor_issues: { bg: "#fffbeb", fg: "#92400e", dot: "#d97706", headline: "Minor mismatches between declared shape and preview" },
  misaligned: { bg: "#fef2f2", fg: "#991b1b", dot: "#dc2626", headline: "Deployed view does not match the declared shape" },
  unknown: { bg: "#f1f5f9", fg: "#475569", dot: "#94a3b8", headline: "Reflection has not been run yet" },
};

const SEVERITY_COLOR: Record<Severity, string> = {
  low: "#94a3b8",
  medium: "#d97706",
  high: "#dc2626",
};

const STATUS_DOT_COLOR: Record<string, string> = {
  aligned: "#16a34a",
  answerable: "#16a34a",
  partial: "#d97706",
  violated: "#dc2626",
  mismatched: "#dc2626",
  unanswerable: "#dc2626",
  unverifiable: "#94a3b8",
};

const styles: Record<string, CSSProperties> = {
  container: { display: "flex", flexDirection: "column", gap: 12 },
  banner: { padding: "12px 16px", borderRadius: 8, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 },
  bannerLeft: { display: "flex", alignItems: "center", gap: 12 },
  dot: { width: 12, height: 12, borderRadius: "50%", flexShrink: 0 },
  headline: { fontWeight: 700, fontSize: 14 },
  subtitle: { fontSize: 11, fontWeight: 500, opacity: 0.85, marginTop: 2 },
  rerunBtn: {
    fontSize: 12, padding: "5px 10px",
    border: "1px solid #cbd5e1", borderRadius: 5,
    backgroundColor: "#fff", color: "#334155",
    cursor: "pointer", fontWeight: 600,
  },
  sectionLabel: { fontSize: 11, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 4 },
  panel: { border: "1px solid #e2e8f0", borderRadius: 6, padding: 12, backgroundColor: "#fff" },
  narrativeText: { fontSize: 13, lineHeight: 1.55, color: "#0f172a", whiteSpace: "pre-wrap" },
  surpriseCard: {
    border: "1px solid #e2e8f0", borderRadius: 6, padding: "10px 12px",
    display: "flex", flexDirection: "column", gap: 4, backgroundColor: "#fff",
  },
  uriCode: { fontFamily: "'Fira Code', monospace", fontSize: 11, color: "#475569", wordBreak: "break-all" },
  evidence: { fontSize: 12, color: "#334155", lineHeight: 1.4 },
  alignmentRow: {
    display: "grid", gridTemplateColumns: "16px 1fr auto", gap: 8, alignItems: "center",
    padding: "6px 0", borderBottom: "1px solid #f1f5f9", fontSize: 12,
  },
  recCard: {
    border: "1px solid #e2e8f0", borderRadius: 6, padding: "10px 12px",
    display: "flex", flexDirection: "column", gap: 4, backgroundColor: "#fff",
  },
  errorBanner: {
    padding: 10, borderRadius: 6, fontSize: 12,
    backgroundColor: "#fef2f2", color: "#991b1b", border: "1px solid #fecaca",
  },
};

const ENDPOINT_LATEST = (props: Props) => props.projectId !== undefined
  ? `/api/projects/${props.projectId}/reflection/latest`
  : `/api/marketplace/products/${props.contractId}/reflection/latest`;

export default function DeploymentReflectionPanel(props: Props) {
  const [payload, setPayload] = useState<ReflectionPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const endpoint = ENDPOINT_LATEST(props);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    api.get(endpoint)
      .then(res => {
        if (cancelled) return;
        const r = res.data?.reflection as ReflectionPayload | null;
        setPayload(r || null);
      })
      .catch(e => {
        if (cancelled) return;
        setError(String(e?.response?.data?.detail || e?.message || e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [endpoint]);

  const rerun = async () => {
    if (running || props.projectId === undefined) return;
    setRunning(true);
    try {
      const res = await api.post(`/api/projects/${props.projectId}/reflection/run`, { trigger: "rerun" });
      setPayload({
        uri: res.data?.uri || "",
        batch_id: res.data?.batch_id || "",
        verdict: (res.data?.verdict || "unknown") as ReflectionVerdict,
        narrative: res.data?.narrative || "",
        surprises: res.data?.surprises || [],
        description_alignment: res.data?.description_alignment || [],
        rule_alignment: res.data?.rule_alignment || [],
        qa_alignment: res.data?.qa_alignment || [],
        recommendations: res.data?.recommendations || [],
        advisor_error: res.data?.advisor_error || null,
        preview_dataset_count: res.data?.preview_dataset_count,
        evaluated_at: new Date().toISOString(),
        triggered_by: "rerun",
      });
    } catch (e) {
      setError(String((e as Error).message || e));
    } finally {
      setRunning(false);
    }
  };

  if (loading) {
    return <div style={{ padding: 12, color: "#64748b", fontSize: 13 }}>Loading reflection…</div>;
  }

  if (error) {
    return <div style={styles.errorBanner}>{error}</div>;
  }

  if (!payload) {
    return (
      <div style={{ ...styles.banner, ...VERDICT_PALETTE.unknown, color: VERDICT_PALETTE.unknown.fg }}>
        <div style={styles.bannerLeft}>
          <span style={{ ...styles.dot, backgroundColor: VERDICT_PALETTE.unknown.dot }} />
          <div>
            <div style={styles.headline}>No deployment reflection yet</div>
            <div style={styles.subtitle}>
              Run the Deployment Reflection stage in the engineering pipeline to compare the deployed view against the declared shape.
            </div>
          </div>
        </div>
        {props.projectId !== undefined && (
          <button type="button" style={styles.rerunBtn} onClick={rerun} disabled={running}>
            {running ? "Running…" : "Run now"}
          </button>
        )}
      </div>
    );
  }

  const verdict: ReflectionVerdict = payload.verdict || "unknown";
  const palette = VERDICT_PALETTE[verdict];

  return (
    <div style={styles.container}>
      {/* Verdict banner */}
      <div style={{ ...styles.banner, backgroundColor: palette.bg, color: palette.fg }}>
        <div style={styles.bannerLeft}>
          <span style={{ ...styles.dot, backgroundColor: palette.dot }} />
          <div>
            <div style={styles.headline}>{palette.headline}</div>
            <div style={styles.subtitle}>
              {payload.surprises.length} surprise{payload.surprises.length === 1 ? "" : "s"} ·
              {" "}{payload.recommendations.length} recommendation{payload.recommendations.length === 1 ? "" : "s"}
              {payload.preview_dataset_count !== undefined && (
                <> · {payload.preview_dataset_count} dataset{payload.preview_dataset_count === 1 ? "" : "s"} sampled</>
              )}
              {payload.evaluated_at && (
                <> · {new Date(payload.evaluated_at).toLocaleString()}</>
              )}
            </div>
          </div>
        </div>
        {props.projectId !== undefined && (
          <button type="button" style={styles.rerunBtn} onClick={rerun} disabled={running}>
            {running ? "Re-running…" : "Re-run"}
          </button>
        )}
      </div>

      {payload.advisor_error && (
        <div style={styles.errorBanner}>
          <strong>Advisor error:</strong> {payload.advisor_error}. The deterministic part of the report still saved.
        </div>
      )}

      {/* Narrative */}
      {payload.narrative && (
        <div style={styles.panel}>
          <div style={styles.sectionLabel}>Narrative</div>
          <div style={styles.narrativeText}>{payload.narrative}</div>
        </div>
      )}

      {/* Surprises */}
      {payload.surprises.length > 0 && (
        <div>
          <div style={styles.sectionLabel}>Surprises</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {payload.surprises.map((s, i) => (
              <div key={i} style={styles.surpriseCard}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  {s.severity && (
                    <span style={{
                      fontSize: 10, padding: "1px 6px", borderRadius: 4,
                      backgroundColor: SEVERITY_COLOR[s.severity], color: "#fff",
                      fontWeight: 700, textTransform: "uppercase",
                    }}>{s.severity}</span>
                  )}
                  {s.kind && (
                    <span style={{ fontSize: 11, fontFamily: "'Fira Code', monospace", color: "#475569" }}>
                      {s.kind}
                    </span>
                  )}
                  {s.sample_artifact_risk && s.sample_artifact_risk !== "low" && (
                    <span style={{
                      fontSize: 10, padding: "1px 6px", borderRadius: 4,
                      backgroundColor: "#f1f5f9", color: "#475569", fontWeight: 600,
                    }}>
                      sample-artifact risk: {s.sample_artifact_risk}
                    </span>
                  )}
                </div>
                {s.column_name && (
                  <div style={{ fontWeight: 600, fontSize: 13, color: "#0f172a" }}>{s.column_name}</div>
                )}
                {s.evidence && (
                  <div style={styles.evidence}>{s.evidence}</div>
                )}
                <div style={styles.uriCode}>
                  {s.column_uri || s.dataset_uri}
                </div>
                {s.cited_signal && (
                  <div style={{ fontSize: 11, color: "#64748b", fontStyle: "italic" }}>
                    cited from: {s.cited_signal}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Recommendations */}
      {payload.recommendations.length > 0 && (
        <div>
          <div style={styles.sectionLabel}>Recommendations</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {payload.recommendations.map((r, i) => (
              <div key={i} style={styles.recCard}>
                {r.priority && (
                  <span style={{
                    alignSelf: "flex-start",
                    fontSize: 10, padding: "1px 6px", borderRadius: 4,
                    backgroundColor: SEVERITY_COLOR[r.priority], color: "#fff",
                    fontWeight: 700, textTransform: "uppercase",
                  }}>{r.priority}</span>
                )}
                <div style={{ fontSize: 13, color: "#0f172a", lineHeight: 1.45 }}>{r.action}</div>
                {(r.related_columns || []).length > 0 && (
                  <div style={styles.uriCode}>
                    {(r.related_columns || []).join(", ")}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Alignment summaries — fold them up for compactness */}
      {(payload.rule_alignment.length + payload.description_alignment.length + payload.qa_alignment.length) > 0 && (
        <details style={styles.panel}>
          <summary style={{ cursor: "pointer", fontSize: 12, fontWeight: 600, color: "#475569" }}>
            Alignment checks ({payload.rule_alignment.length + payload.description_alignment.length + payload.qa_alignment.length})
          </summary>
          <div style={{ marginTop: 8 }}>
            {payload.rule_alignment.length > 0 && (
              <div>
                <div style={{ ...styles.sectionLabel, marginTop: 8 }}>Rule alignment</div>
                {payload.rule_alignment.map((r, i) => (
                  <div key={i} style={styles.alignmentRow}>
                    <span style={{
                      ...styles.dot,
                      backgroundColor: STATUS_DOT_COLOR[r.status || "unverifiable"],
                      width: 8, height: 8,
                    }} />
                    <span>
                      <span style={{ fontFamily: "'Fira Code', monospace", color: "#475569" }}>{r.column_uri}</span>
                      {r.rule_type && <span style={{ marginLeft: 8, fontWeight: 600 }}>{r.rule_type}</span>}
                      {r.note && <span style={{ marginLeft: 8, color: "#64748b" }}>· {r.note}</span>}
                    </span>
                    <span style={{ fontSize: 11, fontWeight: 600, color: STATUS_DOT_COLOR[r.status || "unverifiable"] }}>
                      {r.status}
                    </span>
                  </div>
                ))}
              </div>
            )}
            {payload.description_alignment.length > 0 && (
              <div>
                <div style={{ ...styles.sectionLabel, marginTop: 8 }}>Description alignment</div>
                {payload.description_alignment.map((d, i) => (
                  <div key={i} style={styles.alignmentRow}>
                    <span style={{
                      ...styles.dot,
                      backgroundColor: STATUS_DOT_COLOR[d.status || "unverifiable"],
                      width: 8, height: 8,
                    }} />
                    <span>
                      <span style={{ fontFamily: "'Fira Code', monospace", color: "#475569" }}>{d.column_uri}</span>
                      {d.note && <span style={{ marginLeft: 8, color: "#64748b" }}>· {d.note}</span>}
                    </span>
                    <span style={{ fontSize: 11, fontWeight: 600, color: STATUS_DOT_COLOR[d.status || "unverifiable"] }}>
                      {d.status}
                    </span>
                  </div>
                ))}
              </div>
            )}
            {payload.qa_alignment.length > 0 && (
              <div>
                <div style={{ ...styles.sectionLabel, marginTop: 8 }}>Q&A alignment</div>
                {payload.qa_alignment.map((q, i) => (
                  <div key={i} style={styles.alignmentRow}>
                    <span style={{
                      ...styles.dot,
                      backgroundColor: STATUS_DOT_COLOR[q.status || "unverifiable"],
                      width: 8, height: 8,
                    }} />
                    <span style={{ fontSize: 12, color: "#0f172a" }}>{q.question_text}</span>
                    <span style={{ fontSize: 11, fontWeight: 600, color: STATUS_DOT_COLOR[q.status || "unverifiable"] }}>
                      {q.status}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </details>
      )}
    </div>
  );
}
