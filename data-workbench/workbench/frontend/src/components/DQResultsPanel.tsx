import { Fragment, useEffect, useState } from "react";
import api from "../api/client";

// DQ test-run history + per-expectation breakdown for the Data Quality summary
// card. Sourced from SQLite via /test-runs (reliable for any served platform,
// including warehouse targets whose :TestResult graph load is a graceful skip).

interface TestRun {
  id: number;
  framework: string;
  batch_id: string;
  started_at: string | null;
  completed_at: string | null;
  total_expectations: number;
  successful: number;
  unsuccessful: number;
  tables_tested: number;
  pass_rate: number;
  status: string;
}

interface ResultRow {
  rule_type?: string;
  expectation_type?: string;
  evaluated?: number;
  successful?: number;
  unsuccessful?: number;
  pass_rate?: number;
  passed?: boolean;
  column_name?: string;
  dataset_name?: string;
  schema?: string;
}

const fmtWhen = (iso: string | null): string => {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
};

const pct = (v: number): string => `${Math.round(v * 100)}%`;

const th: React.CSSProperties = {
  textAlign: "left", padding: "6px 10px", fontSize: 11, fontWeight: 600,
  color: "#64748b", borderBottom: "1px solid #e2e8f0", whiteSpace: "nowrap",
};
const td: React.CSSProperties = {
  padding: "6px 10px", fontSize: 12, color: "#334155", borderBottom: "1px solid #f1f5f9",
};

export default function DQResultsPanel({ projectId }: { projectId: number }) {
  const [runs, setRuns] = useState<TestRun[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [results, setResults] = useState<Record<number, ResultRow[]>>({});
  const [loadingRun, setLoadingRun] = useState<number | null>(null);

  useEffect(() => {
    let alive = true;
    api.get(`/api/projects/${projectId}/test-runs`)
      .then((res) => { if (alive) setRuns(res.data.runs || []); })
      .catch(() => { if (alive) setError("Could not load test runs."); });
    return () => { alive = false; };
  }, [projectId]);

  const toggleRun = async (runId: number) => {
    if (expanded === runId) { setExpanded(null); return; }
    setExpanded(runId);
    if (!results[runId]) {
      setLoadingRun(runId);
      try {
        const res = await api.get(`/api/projects/${projectId}/test-runs/${runId}`);
        setResults((prev) => ({ ...prev, [runId]: res.data.results || [] }));
      } catch {
        setResults((prev) => ({ ...prev, [runId]: [] }));
      }
      setLoadingRun(null);
    }
  };

  if (error) return <div style={{ color: "#ef4444", fontSize: 13 }}>{error}</div>;
  if (!runs) return <div style={{ color: "#94a3b8", fontSize: 13 }}>Loading test runs…</div>;
  if (runs.length === 0) return <div style={{ color: "#94a3b8", fontSize: 13 }}>No DQ test runs yet.</div>;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <div style={{ fontSize: 12, color: "#64748b", marginBottom: 4 }}>
        {runs.length} run{runs.length === 1 ? "" : "s"} · click a run to see per-expectation results (failures first).
      </div>
      <table style={{ borderCollapse: "collapse", width: "100%" }}>
        <thead>
          <tr>
            <th style={th}></th>
            <th style={th}>When</th>
            <th style={th}>Framework</th>
            <th style={th}>Tables</th>
            <th style={th}>Passed</th>
            <th style={th}>Failed</th>
            <th style={th}>Pass rate</th>
            <th style={th}>Status</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => {
            const isOpen = expanded === r.id;
            const rows = results[r.id];
            return (
              <Fragment key={r.id}>
                <tr
                  onClick={() => toggleRun(r.id)}
                  style={{ cursor: "pointer", background: isOpen ? "#f0fdfa" : undefined }}
                >
                  <td style={{ ...td, color: "#0d9488", fontWeight: 700, width: 18 }}>{isOpen ? "▾" : "▸"}</td>
                  <td style={td}>{fmtWhen(r.completed_at || r.started_at)}</td>
                  <td style={td}>{r.framework?.toUpperCase()}</td>
                  <td style={td}>{r.tables_tested}</td>
                  <td style={{ ...td, color: "#059669", fontWeight: 600 }}>{r.successful}</td>
                  <td style={{ ...td, color: r.unsuccessful > 0 ? "#ef4444" : "#94a3b8", fontWeight: 600 }}>{r.unsuccessful}</td>
                  <td style={td}>{pct(r.pass_rate)}</td>
                  <td style={td}>
                    <span style={{
                      fontSize: 11, fontWeight: 600, padding: "1px 8px", borderRadius: 10,
                      color: r.status === "complete" ? "#065f46" : "#92400e",
                      background: r.status === "complete" ? "#d1fae5" : "#fef3c7",
                    }}>{r.status}</span>
                  </td>
                </tr>
                {isOpen && (
                  <tr>
                    <td colSpan={8} style={{ padding: "0 10px 10px 28px", background: "#f8fafc" }}>
                      {loadingRun === r.id ? (
                        <div style={{ color: "#94a3b8", fontSize: 12, padding: "8px 0" }}>Loading results…</div>
                      ) : !rows || rows.length === 0 ? (
                        <div style={{ color: "#94a3b8", fontSize: 12, padding: "8px 0" }}>
                          Per-expectation detail isn't available for this run (the graph load was skipped for this framework/platform). The pass/fail totals above are authoritative.
                        </div>
                      ) : (
                        <table style={{ borderCollapse: "collapse", width: "100%", marginTop: 6 }}>
                          <thead>
                            <tr>
                              <th style={th}></th>
                              <th style={th}>Table</th>
                              <th style={th}>Column</th>
                              <th style={th}>Expectation</th>
                              <th style={th}>Evaluated</th>
                              <th style={th}>Unexpected</th>
                              <th style={th}>Pass rate</th>
                            </tr>
                          </thead>
                          <tbody>
                            {rows.map((row, i) => (
                              <tr key={i}>
                                <td style={{ ...td, width: 18 }}>
                                  <span title={row.passed ? "passed" : "failed"} style={{ color: row.passed ? "#059669" : "#ef4444", fontWeight: 700 }}>
                                    {row.passed ? "✓" : "✗"}
                                  </span>
                                </td>
                                <td style={td}>{row.schema ? `${row.schema}.${row.dataset_name ?? ""}` : (row.dataset_name ?? "—")}</td>
                                <td style={td}>{row.column_name ?? "—"}</td>
                                <td style={{ ...td, fontFamily: "monospace", fontSize: 11 }}>{row.expectation_type ?? row.rule_type ?? "—"}</td>
                                <td style={td}>{row.evaluated ?? "—"}</td>
                                <td style={{ ...td, color: (row.unsuccessful ?? 0) > 0 ? "#ef4444" : "#94a3b8" }}>{row.unsuccessful ?? 0}</td>
                                <td style={td}>{row.pass_rate != null ? pct(row.pass_rate) : "—"}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      )}
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
