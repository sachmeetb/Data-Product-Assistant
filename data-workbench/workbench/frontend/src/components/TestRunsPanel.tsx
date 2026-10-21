import { useEffect, useState } from "react";
import api from "../api/client";
import type { DQTestRun } from "../types";

interface Props {
  projectId: number;
  framework?: "gx" | "pandera";
}

interface TestResultRow {
  rule_type: string | null;
  expectation_type: string | null;
  evaluated: number | null;
  successful: number | null;
  unsuccessful: number | null;
  pass_rate: number | null;
  passed: boolean | null;
  column_name: string | null;
  dataset_name: string | null;
  schema: string | null;
}

interface TestRunDetail extends DQTestRun {
  results: TestResultRow[];
}

function scoreColor(rate: number): string {
  if (rate >= 0.99) return "#22c55e";
  if (rate >= 0.9) return "#f59e0b";
  return "#ef4444";
}

function pct(rate: number | null | undefined): string {
  if (rate === null || rate === undefined) return "--";
  return `${Math.round(rate * 100)}%`;
}

export default function TestRunsPanel({ projectId, framework }: Props) {
  const [runs, setRuns] = useState<DQTestRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<TestRunDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  useEffect(() => {
    setLoading(true);
    const params = framework ? `?framework=${framework}` : "";
    api.get(`/api/projects/${projectId}/test-runs${params}`)
      .then((r) => setRuns(r.data.runs || []))
      .catch(() => setRuns([]))
      .finally(() => setLoading(false));
  }, [projectId, framework]);

  const openRun = async (runId: number) => {
    if (selected?.id === runId) {
      setSelected(null);
      return;
    }
    setDetailLoading(true);
    try {
      const r = await api.get(`/api/projects/${projectId}/test-runs/${runId}`);
      setSelected(r.data);
    } catch {
      setSelected(null);
    }
    setDetailLoading(false);
  };

  if (loading) return <div style={{ color: "#94a3b8", padding: 16 }}>Loading runs...</div>;
  if (runs.length === 0) {
    return (
      <div style={{ padding: 24, color: "#94a3b8", textAlign: "center" }}>
        <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 6, color: "#64748b" }}>No test runs yet</div>
        <div style={{ fontSize: 12 }}>
          Runs appear here after a DQ testing stage completes and loads results into the knowledge graph.
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ backgroundColor: "#fff", borderRadius: 10, border: "1px solid #e2e8f0", overflow: "hidden" }}>
        <div style={{ padding: "12px 18px", borderBottom: "1px solid #e2e8f0", fontWeight: 600, color: "#334155" }}>
          Test Runs
          <span style={{ fontWeight: 400, color: "#94a3b8", marginLeft: 8 }}>({runs.length})</span>
        </div>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr>
              <th style={thStyle}>Started</th>
              <th style={thStyle}>Framework</th>
              <th style={thStyle}>Tables</th>
              <th style={thStyle}>Expectations</th>
              <th style={thStyle}>Pass / Fail</th>
              <th style={thStyle}>Pass rate</th>
              <th style={thStyle}></th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r, i) => {
              const isOpen = selected?.id === r.id;
              return (
                <>
                  <tr
                    key={r.id}
                    onClick={() => openRun(r.id)}
                    style={{
                      backgroundColor: i % 2 === 0 ? "#fff" : "#f8fafc",
                      cursor: "pointer",
                    }}
                  >
                    <td style={tdStyle}>
                      {r.started_at ? new Date(r.started_at).toLocaleString() : "--"}
                    </td>
                    <td style={tdStyle}>
                      <span style={{
                        padding: "1px 8px", borderRadius: 4,
                        backgroundColor: "#e0e7ff", color: "#4338ca",
                        fontSize: 11, fontWeight: 600,
                      }}>{r.framework.toUpperCase()}</span>
                    </td>
                    <td style={tdStyle}>{r.tables_tested}</td>
                    <td style={tdStyle}>{r.total_expectations}</td>
                    <td style={tdStyle}>
                      <span style={{ color: "#16a34a", fontWeight: 600 }}>{r.successful}</span>
                      <span style={{ color: "#94a3b8" }}> / </span>
                      <span style={{ color: r.unsuccessful > 0 ? "#dc2626" : "#94a3b8", fontWeight: 600 }}>
                        {r.unsuccessful}
                      </span>
                    </td>
                    <td style={tdStyle}>
                      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                        <div style={{
                          width: 60, height: 8, backgroundColor: "#f1f5f9",
                          borderRadius: 4, overflow: "hidden",
                        }}>
                          <div style={{
                            height: "100%", width: `${Math.round(r.pass_rate * 100)}%`,
                            backgroundColor: scoreColor(r.pass_rate),
                          }} />
                        </div>
                        <span style={{
                          fontWeight: 600, fontSize: 12,
                          color: scoreColor(r.pass_rate),
                        }}>{pct(r.pass_rate)}</span>
                      </div>
                    </td>
                    <td style={tdStyle}>
                      <span style={{ fontSize: 10, color: "#94a3b8" }}>
                        {isOpen ? "\u25BC" : "\u25B6"}
                      </span>
                    </td>
                  </tr>
                  {isOpen && (
                    <tr key={`${r.id}-detail`}>
                      <td colSpan={7} style={{ padding: 0, backgroundColor: "#f8fafc" }}>
                        <RunDetail detail={selected} loading={detailLoading} />
                      </td>
                    </tr>
                  )}
                </>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function RunDetail({ detail, loading }: { detail: TestRunDetail | null; loading: boolean }) {
  if (loading) return <div style={{ padding: 14, color: "#94a3b8", fontSize: 12 }}>Loading run detail...</div>;
  if (!detail) return null;
  const results = detail.results || [];
  if (results.length === 0) {
    return (
      <div style={{ padding: 14, color: "#94a3b8", fontSize: 12 }}>
        No per-rule results loaded into the knowledge graph for this run.
        Raw JSON is at <code style={{ fontFamily: "monospace" }}>{detail.results_path}</code>.
      </div>
    );
  }
  return (
    <div style={{ padding: 14 }}>
      <div style={{ marginBottom: 8, fontSize: 12, color: "#64748b" }}>
        Batch <code style={{ fontFamily: "monospace" }}>{detail.batch_id}</code> —
        raw results at <code style={{ fontFamily: "monospace" }}>{detail.results_path}</code>
      </div>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead>
          <tr>
            <th style={subThStyle}>Status</th>
            <th style={subThStyle}>Dataset</th>
            <th style={subThStyle}>Column</th>
            <th style={subThStyle}>Rule</th>
            <th style={subThStyle}>Evaluated</th>
            <th style={subThStyle}>Failed</th>
            <th style={subThStyle}>Pass rate</th>
          </tr>
        </thead>
        <tbody>
          {results.map((r, i) => (
            <tr key={i} style={{ backgroundColor: i % 2 === 0 ? "#f8fafc" : "#f1f5f9" }}>
              <td style={subTdStyle}>
                <span style={{
                  display: "inline-block", width: 8, height: 8, borderRadius: 4,
                  backgroundColor: r.passed ? "#22c55e" : "#ef4444", marginRight: 6,
                }} />
                {r.passed ? "Pass" : "Fail"}
              </td>
              <td style={subTdStyle}>
                {r.schema && r.dataset_name ? `${r.schema}.${r.dataset_name}` : (r.dataset_name ?? "--")}
              </td>
              <td style={{ ...subTdStyle, fontWeight: 600 }}>{r.column_name ?? "--"}</td>
              <td style={{ ...subTdStyle, fontFamily: "monospace" }}>
                {r.rule_type ?? r.expectation_type ?? ""}
              </td>
              <td style={subTdStyle}>{r.evaluated ?? "--"}</td>
              <td style={{
                ...subTdStyle,
                color: (r.unsuccessful ?? 0) > 0 ? "#dc2626" : "#94a3b8",
                fontWeight: (r.unsuccessful ?? 0) > 0 ? 600 : 400,
              }}>{r.unsuccessful ?? 0}</td>
              <td style={{
                ...subTdStyle, fontWeight: 600,
                color: r.pass_rate !== null && r.pass_rate !== undefined ? scoreColor(r.pass_rate) : "#94a3b8",
              }}>
                {pct(r.pass_rate)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const thStyle: React.CSSProperties = {
  textAlign: "left", padding: "8px 14px", borderBottom: "2px solid #e2e8f0",
  color: "#64748b", fontWeight: 600, fontSize: 11, textTransform: "uppercase",
  letterSpacing: 0.5, backgroundColor: "#f8fafc",
};

const tdStyle: React.CSSProperties = {
  padding: "8px 14px", borderBottom: "1px solid #f1f5f9", color: "#334155",
};

const subThStyle: React.CSSProperties = {
  textAlign: "left", padding: "6px 10px", borderBottom: "1px solid #e2e8f0",
  color: "#94a3b8", fontWeight: 600, fontSize: 10, textTransform: "uppercase",
  letterSpacing: 0.5,
};

const subTdStyle: React.CSSProperties = {
  padding: "4px 10px", borderBottom: "1px solid #e2e8f0", color: "#475569",
};
