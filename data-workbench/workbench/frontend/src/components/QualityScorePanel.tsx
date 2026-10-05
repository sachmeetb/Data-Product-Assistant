import { useEffect, useState } from "react";
import api from "../api/client";
import type { EvidenceKind, TestRunSummary, Tier } from "../types";

interface DatasetScore {
  uri: string;
  name: string;
  schema: string;
  evidence?: Partial<Record<string, EvidenceKind>>;
  composite?: number;
  completeness?: number;
  uniqueness?: number;
  validity?: number;
  consistency?: number;
  schema_conformance?: number;
  rule_coverage?: number;
  documentation?: number;
  grounding?: number;
}

interface ColumnScore {
  uri: string;
  name: string;
  ordinal: number;
  composite?: number;
  completeness?: number;
  uniqueness?: number;
  validity?: number;
  consistency?: number;
  schema_conformance?: number;
  rule_coverage?: number;
  documentation?: number;
  grounding?: number;
}

interface ScoringResponse {
  batch_id: string | null;
  scored_at: string | null;
  overall: Record<string, number>;
  overall_evidence?: Partial<Record<string, EvidenceKind>>;
  datasets: DatasetScore[];
  tier?: Tier;
  tier_label?: string;
}

interface TrendBatch {
  batch_id: string;
  scored_at: string | null;
  avg_composite: number;
  datasets: Record<string, number>;
}

const DIMENSIONS = [
  { key: "completeness", label: "Completeness", weight: 0.20, color: "#3b82f6" },
  { key: "uniqueness", label: "Uniqueness", weight: 0.16, color: "#8b5cf6" },
  { key: "validity", label: "Validity", weight: 0.16, color: "#22c55e" },
  { key: "consistency", label: "Consistency", weight: 0.10, color: "#f59e0b" },
  { key: "schema_conformance", label: "Schema Conformance", weight: 0.08, color: "#06b6d4" },
  { key: "rule_coverage", label: "Rule Coverage", weight: 0.08, color: "#ec4899" },
  { key: "documentation", label: "Documentation", weight: 0.12, color: "#a855f7" },
  { key: "grounding", label: "Grounding", weight: 0.10, color: "#be185d" },
];

const EVIDENCE_COLORS: Record<EvidenceKind, { bg: string; fg: string; label: string; title: string }> = {
  test:    { bg: "#dcfce7", fg: "#15803d", label: "tested",   title: "Driven by actual test execution results" },
  profile: { bg: "#fef3c7", fg: "#b45309", label: "profiled", title: "Derived from profiling measurements (sample-based)" },
  rule:    { bg: "#e2e8f0", fg: "#475569", label: "rule",     title: "Derived from rule presence only — not executed" },
  meta:    { bg: "#e0e7ff", fg: "#4338ca", label: "meta",     title: "Derived from schema/metadata" },
};

const TIER_STYLES: Record<Tier, { bg: string; fg: string; label: string; hint: string }> = {
  1: { bg: "#dbeafe", fg: "#1d4ed8", label: "Tier 1 — Internal consistency",
       hint: "Score reflects how the data measures against what was observed about it." },
  2: { bg: "#ede9fe", fg: "#6d28d9", label: "Tier 2 — Shared meaning",
       hint: "Descriptions have been reviewed. People agree on what each column represents." },
  3: { bg: "#fce7f3", fg: "#be185d", label: "Tier 3 — External grounding",
       hint: "Validation incorporates rules anchored to authoritative external sources." },
};

function scoreColor(score: number): string {
  if (score >= 0.8) return "#22c55e";
  if (score >= 0.6) return "#f59e0b";
  return "#ef4444";
}

function pct(score: number | undefined): string {
  if (score === undefined || score === null) return "--";
  return `${Math.round(score * 100)}%`;
}

interface Props {
  projectId: number;
}

export default function QualityScorePanel({ projectId }: Props) {
  const [data, setData] = useState<ScoringResponse | null>(null);
  const [trend, setTrend] = useState<TrendBatch[]>([]);
  const [testSummary, setTestSummary] = useState<TestRunSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [expandedDataset, setExpandedDataset] = useState<string | null>(null);
  const [columnScores, setColumnScores] = useState<ColumnScore[]>([]);
  const [columnLoading, setColumnLoading] = useState(false);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      api.get(`/api/projects/${projectId}/scoring`).then((r) => r.data),
      api.get(`/api/projects/${projectId}/scoring/trend`).then((r) => r.data).catch(() => ({ batches: [] })),
      api.get(`/api/projects/${projectId}/test-runs/summary`).then((r) => r.data).catch(() => ({ has_runs: false })),
    ])
      .then(([scoring, trendData, summary]) => {
        setData(scoring);
        setTrend(trendData.batches || []);
        setTestSummary(summary);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [projectId]);

  const handleDatasetClick = async (datasetUri: string) => {
    if (expandedDataset === datasetUri) {
      setExpandedDataset(null);
      return;
    }
    setExpandedDataset(datasetUri);
    setColumnLoading(true);
    try {
      const res = await api.get(`/api/projects/${projectId}/scoring/dataset/${encodeURIComponent(datasetUri)}`);
      setColumnScores(res.data.columns || []);
    } catch {
      setColumnScores([]);
    }
    setColumnLoading(false);
  };

  if (loading) return <div style={{ color: "#94a3b8", padding: 16 }}>Loading scores...</div>;

  if (!data || !data.batch_id) {
    return (
      <div style={{ padding: 24, color: "#94a3b8", textAlign: "center" }}>
        <div style={{ fontSize: 16, fontWeight: 600, marginBottom: 8, color: "#64748b" }}>No quality scores yet</div>
        <div style={{ fontSize: 13 }}>Run the Data Quality Scoring stage to generate a quality baseline.</div>
      </div>
    );
  }

  const overall = data.overall;
  const overallEvidence = data.overall_evidence || {};
  const compositeScore = overall.composite;
  const tier = (data.tier ?? 1) as Tier;
  const tierStyle = TIER_STYLES[tier];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* Header: Overall Composite */}
      <div style={{ display: "flex", gap: 20, alignItems: "flex-start", flexWrap: "wrap" }}>
        {/* Gauge */}
        <div style={{
          display: "flex", flexDirection: "column", alignItems: "center",
          padding: "20px 28px", backgroundColor: "#fff", borderRadius: 10,
          border: "1px solid #e2e8f0", minWidth: 140,
        }}>
          <div style={{
            fontSize: 48, fontWeight: 800, lineHeight: 1,
            color: compositeScore !== undefined ? scoreColor(compositeScore) : "#94a3b8",
          }}>
            {pct(compositeScore)}
          </div>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#334155", marginTop: 6 }}>Overall Quality</div>
          <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 2 }}>
            {data.scored_at ? `Scored ${new Date(data.scored_at).toLocaleDateString()}` : ""}
          </div>
          <div
            title={tierStyle.hint}
            style={{
              marginTop: 10, padding: "3px 10px", borderRadius: 10,
              backgroundColor: tierStyle.bg, color: tierStyle.fg,
              fontSize: 11, fontWeight: 700, whiteSpace: "nowrap",
            }}
          >
            {tierStyle.label}
          </div>
        </div>

        {/* Dimension Breakdown */}
        <div style={{ flex: 1, minWidth: 300 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#334155", marginBottom: 8 }}>Dimension Breakdown</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {DIMENSIONS.map((dim) => {
              const score = overall[dim.key];
              const evidence = overallEvidence[dim.key];
              return (
                <div key={dim.key} style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <div style={{ width: 170, fontSize: 12, color: "#64748b", flexShrink: 0, display: "flex", alignItems: "center", gap: 6 }}>
                    <span>{dim.label}</span>
                    <span style={{ color: "#94a3b8", fontSize: 10 }}>({Math.round(dim.weight * 100)}%)</span>
                    {evidence && <EvidenceBadge evidence={evidence} />}
                  </div>
                  <div style={{
                    flex: 1, height: 18, backgroundColor: "#f1f5f9",
                    borderRadius: 9, overflow: "hidden", position: "relative",
                  }}>
                    <div style={{
                      height: "100%", width: score !== undefined ? `${Math.round(score * 100)}%` : "0%",
                      backgroundColor: dim.color, borderRadius: 9,
                      transition: "width 0.3s ease",
                    }} />
                  </div>
                  <div style={{
                    width: 40, textAlign: "right", fontSize: 12, fontWeight: 600,
                    color: score !== undefined ? scoreColor(score) : "#94a3b8",
                  }}>
                    {pct(score)}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      {/* Test Coverage strip */}
      {testSummary?.has_runs && (
        <div style={{
          display: "flex", gap: 24, padding: "10px 16px",
          backgroundColor: "#fff", borderRadius: 10, border: "1px solid #e2e8f0",
          fontSize: 12, color: "#475569", alignItems: "center", flexWrap: "wrap",
        }}>
          <span style={{ fontWeight: 600, color: "#334155" }}>Test Coverage</span>
          <span>
            Pass rate: <strong style={{ color: scoreColor(testSummary.pass_rate ?? 0) }}>
              {pct(testSummary.pass_rate)}
            </strong>
            <span style={{ color: "#94a3b8", marginLeft: 4 }}>
              ({testSummary.successful ?? 0}/{testSummary.total_expectations ?? 0})
            </span>
          </span>
          <span>
            Rules tested: <strong>{testSummary.rules_tested ?? 0}/{testSummary.rules_total ?? 0}</strong>
            <span style={{ color: "#94a3b8", marginLeft: 4 }}>({pct(testSummary.coverage)})</span>
          </span>
          {testSummary.framework && (
            <span style={{ color: "#94a3b8" }}>framework: {testSummary.framework}</span>
          )}
          {testSummary.executed_at && (
            <span style={{ color: "#94a3b8" }}>
              last run {new Date(testSummary.executed_at).toLocaleString()}
            </span>
          )}
        </div>
      )}

      {/* Dataset Scores Table */}
      <div style={{ backgroundColor: "#fff", borderRadius: 10, border: "1px solid #e2e8f0", overflow: "hidden" }}>
        <div style={{ padding: "12px 18px", borderBottom: "1px solid #e2e8f0" }}>
          <span style={{ fontWeight: 600, fontSize: 14, color: "#334155" }}>
            Dataset Scores
            <span style={{ fontWeight: 400, color: "#94a3b8", marginLeft: 8 }}>({data.datasets.length})</span>
          </span>
        </div>
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr>
                <th style={thStyle}>Dataset</th>
                <th style={thStyle}>Composite</th>
                {DIMENSIONS.map((d) => (
                  <th key={d.key} style={thStyle}>{d.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.datasets.map((ds, i) => (
                <>
                  <tr
                    key={ds.uri}
                    onClick={() => handleDatasetClick(ds.uri)}
                    style={{
                      backgroundColor: i % 2 === 0 ? "#fff" : "#f8fafc",
                      cursor: "pointer",
                    }}
                  >
                    <td style={tdStyle}>
                      <span style={{ fontWeight: 500 }}>{ds.name}</span>
                      <span style={{ fontSize: 10, color: "#94a3b8", marginLeft: 6 }}>
                        {expandedDataset === ds.uri ? "\u25BC" : "\u25B6"}
                      </span>
                    </td>
                    <td style={tdStyle}>
                      <ScoreBadge score={ds.composite} />
                    </td>
                    {DIMENSIONS.map((dim) => (
                      <td key={dim.key} style={tdStyle}>
                        <MiniBar score={(ds as Record<string, unknown>)[dim.key] as number | undefined} color={dim.color} />
                      </td>
                    ))}
                  </tr>
                  {expandedDataset === ds.uri && (
                    <tr key={`${ds.uri}-detail`}>
                      <td colSpan={2 + DIMENSIONS.length} style={{ padding: 0 }}>
                        <ColumnDetail
                          columns={columnScores}
                          loading={columnLoading}
                        />
                      </td>
                    </tr>
                  )}
                </>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Trend */}
      {trend.length > 1 && (
        <div style={{ backgroundColor: "#fff", borderRadius: 10, border: "1px solid #e2e8f0", overflow: "hidden" }}>
          <div style={{ padding: "12px 18px", borderBottom: "1px solid #e2e8f0" }}>
            <span style={{ fontWeight: 600, fontSize: 14, color: "#334155" }}>Score Trend</span>
          </div>
          <div style={{ padding: "12px 18px" }}>
            <div style={{ display: "flex", alignItems: "flex-end", gap: 8, height: 80 }}>
              {trend.map((batch, i) => {
                const height = Math.max(4, Math.round(batch.avg_composite * 100));
                return (
                  <div
                    key={batch.batch_id}
                    title={`${batch.scored_at ? new Date(batch.scored_at).toLocaleDateString() : ""}: ${pct(batch.avg_composite)}`}
                    style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 2 }}
                  >
                    <div style={{
                      fontSize: 10, fontWeight: 600,
                      color: scoreColor(batch.avg_composite),
                    }}>
                      {pct(batch.avg_composite)}
                    </div>
                    <div style={{
                      width: 32, height, borderRadius: 4,
                      backgroundColor: scoreColor(batch.avg_composite),
                      opacity: 0.8,
                    }} />
                    <div style={{ fontSize: 9, color: "#94a3b8" }}>
                      {batch.scored_at ? new Date(batch.scored_at).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : `#${i + 1}`}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Sub-components ──────────────────────────────────────────────────────────

function EvidenceBadge({ evidence }: { evidence: EvidenceKind }) {
  const s = EVIDENCE_COLORS[evidence];
  return (
    <span
      title={s.title}
      style={{
        fontSize: 9, fontWeight: 600, padding: "1px 5px",
        borderRadius: 4, backgroundColor: s.bg, color: s.fg,
        textTransform: "uppercase", letterSpacing: 0.3,
      }}
    >
      {s.label}
    </span>
  );
}

function ScoreBadge({ score }: { score: number | undefined }) {
  if (score === undefined || score === null) return <span style={{ color: "#cbd5e1" }}>--</span>;
  return (
    <span style={{
      padding: "2px 10px", borderRadius: 10, fontSize: 12, fontWeight: 700,
      color: "#fff", backgroundColor: scoreColor(score),
    }}>
      {pct(score)}
    </span>
  );
}

function MiniBar({ score, color }: { score: number | undefined; color: string }) {
  if (score === undefined || score === null) return <span style={{ color: "#cbd5e1" }}>--</span>;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
      <div style={{
        width: 50, height: 8, backgroundColor: "#f1f5f9",
        borderRadius: 4, overflow: "hidden",
      }}>
        <div style={{
          height: "100%", width: `${Math.round(score * 100)}%`,
          backgroundColor: color, borderRadius: 4,
        }} />
      </div>
      <span style={{ fontSize: 11, color: "#64748b", minWidth: 28 }}>{pct(score)}</span>
    </div>
  );
}

function ColumnDetail({ columns, loading }: { columns: ColumnScore[]; loading: boolean }) {
  if (loading) return <div style={{ padding: 12, color: "#94a3b8", fontSize: 12 }}>Loading columns...</div>;
  if (columns.length === 0) return <div style={{ padding: 12, color: "#94a3b8", fontSize: 12 }}>No column scores.</div>;

  return (
    <div style={{ backgroundColor: "#f8fafc", borderTop: "1px solid #e2e8f0", overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead>
          <tr>
            <th style={subThStyle}>Column</th>
            <th style={subThStyle}>Composite</th>
            {DIMENSIONS.map((d) => (
              <th key={d.key} style={subThStyle}>{d.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {columns.map((col, i) => (
            <tr key={col.uri} style={{ backgroundColor: i % 2 === 0 ? "#f8fafc" : "#f1f5f9" }}>
              <td style={subTdStyle}>{col.name}</td>
              <td style={subTdStyle}>
                <ScoreBadge score={col.composite} />
              </td>
              {DIMENSIONS.map((dim) => (
                <td key={dim.key} style={subTdStyle}>
                  <span style={{
                    color: (col as Record<string, unknown>)[dim.key] !== undefined
                      ? scoreColor((col as Record<string, unknown>)[dim.key] as number)
                      : "#cbd5e1",
                    fontWeight: 600,
                  }}>
                    {pct((col as Record<string, unknown>)[dim.key] as number | undefined)}
                  </span>
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Styles ──────────────────────────────────────────────────────────────────

const thStyle: React.CSSProperties = {
  textAlign: "left", padding: "8px 14px", borderBottom: "2px solid #e2e8f0",
  color: "#64748b", fontWeight: 600, fontSize: 11, textTransform: "uppercase",
  letterSpacing: 0.5, position: "sticky", top: 0, backgroundColor: "#f8fafc",
};

const tdStyle: React.CSSProperties = {
  padding: "6px 14px", borderBottom: "1px solid #f1f5f9", color: "#334155",
};

const subThStyle: React.CSSProperties = {
  textAlign: "left", padding: "6px 10px", borderBottom: "1px solid #e2e8f0",
  color: "#94a3b8", fontWeight: 600, fontSize: 10, textTransform: "uppercase",
  letterSpacing: 0.5,
};

const subTdStyle: React.CSSProperties = {
  padding: "4px 10px", borderBottom: "1px solid #e2e8f0", color: "#475569",
};
