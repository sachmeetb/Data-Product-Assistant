import { useEffect, useState } from "react";
import api from "../api/client";

interface GXTableResult {
  table: string;
  suite: string;
  success: boolean;
  statistics: {
    evaluated: number;
    successful: number;
    unsuccessful: number;
  };
  results: Array<{
    expectation_type: string;
    column: string;
    success: boolean;
    result: Record<string, unknown>;
  }>;
}

interface ResultFile {
  filename: string;
  path: string;
  modified: number;
  data: {
    run_at?: string;
    tables?: GXTableResult[];
    [key: string]: unknown;
  };
}

interface MarkdownReport {
  filename: string;
  label: string;
  content: string;
}

interface Props {
  projectId: number;
}

function simpleMarkdownToHtml(md: string): string {
  let html = md
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    // Code blocks
    .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre style="background:#0f172a;color:#a5f3fc;padding:12px;border-radius:6px;font-size:12px;overflow-x:auto;margin:8px 0">$2</pre>')
    // Headers
    .replace(/^### (.+)$/gm, '<h4 style="margin:16px 0 8px;color:#1e293b;font-size:14px">$1</h4>')
    .replace(/^## (.+)$/gm, '<h3 style="margin:20px 0 10px;color:#1e293b;font-size:16px;border-bottom:1px solid #e2e8f0;padding-bottom:6px">$1</h3>')
    .replace(/^# (.+)$/gm, '<h2 style="margin:0 0 12px;color:#0f172a;font-size:20px">$1</h2>')
    // Bold
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    // Tables
    .replace(/^\|(.+)\|$/gm, (match) => {
      const cells = match.split("|").filter(Boolean).map(c => c.trim());
      if (cells.every(c => /^[-:]+$/.test(c))) return "<!--sep-->";
      return "<tr>" + cells.map(c => `<td style="padding:4px 10px;border:1px solid #e2e8f0;font-size:13px">${c}</td>`).join("") + "</tr>";
    })
    // List items
    .replace(/^- (.+)$/gm, '<li style="margin:2px 0;font-size:13px">$1</li>')
    // Line breaks
    .replace(/\n\n/g, "<br/>")
    .replace(/\n/g, "\n");

  // Wrap table rows
  html = html.replace(/((?:<tr>.*<\/tr>\n?)+)/g, '<table style="border-collapse:collapse;width:100%;margin:8px 0">$1</table>');
  html = html.replace(/<!--sep-->\n?/g, "");
  // Wrap list items
  html = html.replace(/((?:<li[^>]*>.*<\/li>\n?)+)/g, '<ul style="margin:4px 0;padding-left:20px">$1</ul>');

  return html;
}

export default function ResultsViewer({ projectId }: Props) {
  const [results, setResults] = useState<ResultFile[]>([]);
  const [mdReports, setMdReports] = useState<MarkdownReport[]>([]);
  const [loading, setLoading] = useState(true);
  const [expandedTable, setExpandedTable] = useState<string | null>(null);

  useEffect(() => {
    // Markdown reports we know about. Each is fetched independently so a 404
    // on one doesn't suppress the others.
    const reportPaths: { path: string; label: string }[] = [
      { path: "remediation/analysis_report.md", label: "Remediation Analysis Report" },
      { path: "dq_tests_gx/failure_analysis.md", label: "DQ Failure Analysis" },
      { path: "osi/analysis.md", label: "OSI Readiness Analysis" },
    ];

    Promise.all([
      api.get(`/api/projects/${projectId}/artifacts/results`).then((r) => r.data.results).catch(() => []),
      Promise.all(
        reportPaths.map(({ path, label }) =>
          api.get(`/api/projects/${projectId}/artifacts/content?path=${encodeURIComponent(path)}`)
            .then((r) => ({ filename: path, label, content: r.data as string }))
            .catch(() => null)
        )
      ).then((rows) => rows.filter((r): r is { filename: string; label: string; content: string } => r !== null)),
    ]).then(([jsonResults, mdFiles]) => {
      setResults(jsonResults);
      setMdReports(mdFiles);
      setLoading(false);
    });
  }, [projectId]);

  if (loading) return <div style={{ color: "#94a3b8" }}>Loading results...</div>;
  if (results.length === 0 && mdReports.length === 0) return <div style={{ color: "#94a3b8" }}>No results found. Results appear here after DQ Testing or Remediation Analysis stages complete.</div>;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* Markdown reports (remediation analysis) */}
      {mdReports.map((report) => (
        <div key={report.filename}>
          <div style={styles.fileHeader}>
            <span style={{ fontWeight: 600, fontSize: 14 }}>{report.label}</span>
            <span style={{ fontSize: 12, color: "#94a3b8" }}>{report.filename}</span>
          </div>
          <div
            style={{
              border: "1px solid #e2e8f0",
              borderTop: "none",
              borderRadius: "0 0 8px 8px",
              padding: "16px 24px",
              backgroundColor: "#fff",
              maxHeight: 600,
              overflowY: "auto",
              lineHeight: 1.6,
              color: "#334155",
            }}
            dangerouslySetInnerHTML={{ __html: simpleMarkdownToHtml(report.content) }}
          />
        </div>
      ))}

      {/* JSON results (DQ testing) */}
      {results.map((rf) => (
        <div key={rf.filename}>
          {/* Result file header */}
          <div style={styles.fileHeader}>
            <span style={{ fontWeight: 600, fontSize: 14 }}>{rf.filename}</span>
            {rf.data.run_at && (
              <span style={{ fontSize: 12, color: "#94a3b8" }}>
                {new Date(rf.data.run_at).toLocaleString()}
              </span>
            )}
          </div>

          {/* GX-style results with tables */}
          {rf.data.tables ? (
            <div style={styles.tablesContainer}>
              {/* Summary bar */}
              <div style={styles.summaryBar}>
                {rf.data.tables.map((t) => (
                  <div
                    key={t.table}
                    onClick={() => setExpandedTable(expandedTable === t.table ? null : t.table)}
                    style={{
                      ...styles.tablePill,
                      backgroundColor: t.success ? "#dcfce7" : "#fef2f2",
                      borderColor: t.success ? "#22c55e" : "#ef4444",
                      cursor: "pointer",
                    }}
                  >
                    <span style={{ color: t.success ? "#16a34a" : "#dc2626", fontWeight: 700, marginRight: 4 }}>
                      {t.success ? "PASS" : "FAIL"}
                    </span>
                    <span style={{ fontWeight: 600 }}>{t.table}</span>
                    <span style={{ color: "#64748b", fontSize: 11, marginLeft: 6 }}>
                      {t.statistics.successful}/{t.statistics.evaluated}
                    </span>
                  </div>
                ))}
              </div>

              {/* Expanded table detail */}
              {expandedTable && (() => {
                const table = rf.data.tables!.find((t) => t.table === expandedTable);
                if (!table) return null;
                return (
                  <div style={styles.detailSection}>
                    <div style={styles.detailHeader}>
                      <span style={{ fontWeight: 700, fontSize: 15 }}>{table.table}</span>
                      <span style={{
                        fontSize: 12, fontWeight: 700, padding: "2px 8px", borderRadius: 10,
                        backgroundColor: table.success ? "#dcfce7" : "#fef2f2",
                        color: table.success ? "#16a34a" : "#dc2626",
                      }}>
                        {table.statistics.successful} passed / {table.statistics.unsuccessful} failed
                      </span>
                    </div>
                    <table style={styles.resultTable}>
                      <thead>
                        <tr>
                          <th style={styles.th}>Status</th>
                          <th style={styles.th}>Column</th>
                          <th style={styles.th}>Expectation</th>
                          <th style={styles.th}>Details</th>
                        </tr>
                      </thead>
                      <tbody>
                        {table.results.map((r, i) => (
                          <tr key={i} style={{ backgroundColor: i % 2 === 0 ? "#fff" : "#f8fafc" }}>
                            <td style={styles.td}>
                              <span style={{
                                display: "inline-block", width: 8, height: 8, borderRadius: 4,
                                backgroundColor: r.success ? "#22c55e" : "#ef4444", marginRight: 6,
                              }} />
                              {r.success ? "Pass" : "Fail"}
                            </td>
                            <td style={{ ...styles.td, fontWeight: 600 }}>{r.column}</td>
                            <td style={{ ...styles.td, fontFamily: "monospace", fontSize: 11 }}>
                              {r.expectation_type.replace("expect_", "").replaceAll("_", " ")}
                            </td>
                            <td style={{ ...styles.td, fontSize: 12, color: "#64748b" }}>
                              {r.result.unexpected_count !== undefined
                                ? `${r.result.element_count} rows, ${r.result.unexpected_count} unexpected`
                                : r.result.observed_value !== undefined
                                ? `observed: ${r.result.observed_value}`
                                : ""}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                );
              })()}
            </div>
          ) : (
            /* Generic JSON result */
            <pre style={styles.jsonBlock}>{JSON.stringify(rf.data, null, 2)}</pre>
          )}
        </div>
      ))}
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  fileHeader: {
    display: "flex", justifyContent: "space-between", alignItems: "center",
    padding: "10px 16px", backgroundColor: "#f8fafc", borderRadius: "8px 8px 0 0",
    border: "1px solid #e2e8f0",
  },
  tablesContainer: {
    border: "1px solid #e2e8f0", borderTop: "none", borderRadius: "0 0 8px 8px",
    overflow: "hidden",
  },
  summaryBar: {
    display: "flex", flexWrap: "wrap" as const, gap: 8, padding: 12,
  },
  tablePill: {
    display: "flex", alignItems: "center", gap: 4,
    padding: "6px 12px", borderRadius: 6, border: "1px solid",
    fontSize: 13, transition: "all 0.15s",
  },
  detailSection: {
    borderTop: "1px solid #e2e8f0", padding: 0,
  },
  detailHeader: {
    display: "flex", justifyContent: "space-between", alignItems: "center",
    padding: "10px 16px", backgroundColor: "#f1f5f9",
  },
  resultTable: {
    width: "100%", borderCollapse: "collapse" as const, fontSize: 13,
  },
  th: {
    textAlign: "left" as const, padding: "8px 14px", borderBottom: "2px solid #e2e8f0",
    color: "#64748b", fontWeight: 600, fontSize: 11, textTransform: "uppercase" as const,
    backgroundColor: "#f8fafc",
  },
  td: {
    padding: "6px 14px", borderBottom: "1px solid #f1f5f9", color: "#334155",
  },
  jsonBlock: {
    margin: 0, padding: 16, backgroundColor: "#0f172a", color: "#a5f3fc",
    borderRadius: "0 0 8px 8px", fontSize: 12, lineHeight: 1.5, overflow: "auto",
    maxHeight: 400, fontFamily: "'Fira Code', monospace",
  },
};
