import { useEffect, useState } from "react";
import api from "../api/client";
import type { EstateObject } from "../types";
import ExtLinkIcon from "./ExtLinkIcon";
import { DISPOSITION } from "../lib/dispositionColors";

// This panel is the migrate surface — one tone, pulled from the shared palette.
const M = DISPOSITION.migrate;

interface CodeFile {
  filename: string;
  language: string;
  content: string;
}

interface Props {
  projectId: number;
  object: EstateObject;
  // Whether the proposed-DAG preview is active for THIS node — owned by the
  // parent (same source of truth as the DAG banner) so the banner's Cancel and
  // this button stay in sync.
  proposed: boolean;
  // Render the proposed post-migration what-if in the graph (a new node on the
  // target platform + the legacy node set to retire) before anything is sent on.
  onPreview: (obj: EstateObject, target?: string) => void;
  // "Send to intake" — hand the node to the Migration intake, where the actual
  // migration is planned + converted. We deliberately do NOT repeat that planning
  // here; this panel only shows WHAT the object is.
  onProceed: (obj: EstateObject, comment?: string) => void | Promise<void>;
  // Dismiss the active what-if preview (the button's "Exit preview" state).
  onCancelPreview?: () => void;
  // Clear any stale plan-caution badges on the graph when a migrate node opens
  // (there is no per-node plan here anymore, so cautions are always cleared).
  onCautionNodes?: (ids: string[]) => void;
  onCautionInfo?: (info: { blocking_upstream: string[]; gating_downstream: string[]; cautions: string[]; target: string }) => void;
}

function CodePane({ title, file }: { title: string; file: CodeFile }) {
  return (
    <div style={{ border: "1px solid #e2e8f0", borderRadius: 8, overflow: "hidden", display: "flex", flexDirection: "column" }}>
      <div style={{ padding: "7px 12px", background: "#f8fafc", borderBottom: "1px solid #e2e8f0", display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
        <span style={{ fontSize: 12, fontWeight: 700, color: "#b45309" }}>{title}</span>
        <span style={{ fontSize: 10.5, color: "#94a3b8", fontFamily: "ui-monospace, monospace" }}>{file.filename}</span>
      </div>
      <pre style={{ margin: 0, padding: 12, background: "#0f172a", color: "#e2e8f0", fontSize: 12, lineHeight: 1.5, fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", overflow: "auto", whiteSpace: "pre", minHeight: 200, maxHeight: 460 }}>
        {file.content}
      </pre>
    </div>
  );
}

// Source/detail view for a selected estate object: shows WHAT the object is —
// its legacy code for a query/view, or its schema for a table — plus two
// actions: preview the what-if change in the graph, or send it to intake.
//
// This is deliberately NOT a conversion surface. Planning, transpiling and
// conversion all happen in intake; this panel only lets you read the object
// before handing it on.
export default function MigratePanel({ projectId, object, proposed, onPreview, onProceed, onCancelPreview, onCautionNodes, onCautionInfo }: Props) {
  // The current (legacy) code, loaded as soon as the node is selected — present
  // for query/view nodes, absent for tables (which show their schema instead).
  const [source, setSource] = useState<CodeFile | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setSource(null);
    setLoading(true);
    // No per-node plan here — clear any caution badges a previous plan left.
    onCautionNodes?.([]);
    onCautionInfo?.({ blocking_upstream: [], gating_downstream: [], cautions: [], target: "" });
    api.get(`/api/projects/${projectId}/discovery/${object.id}/source`)
      .then((res) => { if (!cancelled) setSource((res.data?.source as CodeFile) ?? null); })
      .catch(() => { if (!cancelled) setSource(null); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, object.id]);

  const cols = ("sample_columns" in object && Array.isArray(object.sample_columns)) ? object.sample_columns : [];
  const target = object.target || "Databricks";

  return (
    <div style={{ marginTop: 12, border: `1px solid ${M.border}`, borderRadius: 10, background: M.wash, padding: 14 }}>
      <div style={{ marginBottom: 12 }}>
        <div style={{ fontSize: 14, fontWeight: 800, color: M.fg, fontFamily: "ui-monospace, monospace" }}>
          {object.name}
        </div>
        <div style={{ fontSize: 11.5, color: "#64748b" }}>
          {object.panel_note
            || `${object.object_type || object.type}${object.application ? ` · ${object.application}` : ""} on ${object.platform || object.database || "the legacy system"}`}
        </div>
      </div>

      {/* Two actions only: preview the what-if in the graph, or send to intake
          (where the work is actually planned and executed). */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: 12 }}>
        <button
          onClick={() => (proposed ? onCancelPreview?.() : onPreview(object, target))}
          style={{ fontSize: 12.5, fontWeight: 700, color: "#fff", background: proposed ? "#64748b" : M.fg, border: "none", borderRadius: 7, padding: "7px 14px", cursor: "pointer" }}
        >
          {proposed ? "✕ Exit preview" : "✦ Preview proposed change"}
        </button>
        <button
          onClick={() => onProceed(object)}
          style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12.5, fontWeight: 700, color: "#1e293b", background: "#fff", border: "1px solid #cbd5e1", borderRadius: 7, padding: "7px 14px", cursor: "pointer" }}
        >
          Send to intake <ExtLinkIcon />
        </button>
      </div>

      {proposed && (
        <div style={{ fontSize: 12.5, color: M.deep, background: M.bg, border: `1px solid ${M.border}`, borderRadius: 7, padding: "8px 12px", marginBottom: 10 }}>
          Reviewing the proposed change above — a new {target} node is added alongside <span style={{ fontFamily: "ui-monospace, monospace" }}>{object.name}</span>, reading the same sources and feeding the same downstream. Send to intake to plan and run the work.
        </div>
      )}

      {/* The object itself: legacy code for a query/view, or the schema for a table. */}
      {loading ? (
        <div style={{ fontSize: 12.5, color: "#94a3b8" }}>Loading…</div>
      ) : source ? (
        <CodePane title={`Current code (${source.language})`} file={source} />
      ) : cols.length ? (
        <div style={{ border: "1px solid #e2e8f0", borderRadius: 8, overflow: "hidden", background: "#fff" }}>
          <div style={{ padding: "7px 12px", background: "#f8fafc", borderBottom: "1px solid #e2e8f0", fontSize: 12, fontWeight: 700, color: "#334155" }}>
            Schema <span style={{ color: "#94a3b8", fontWeight: 400 }}>· {cols.length} column{cols.length === 1 ? "" : "s"}</span>
          </div>
          <div style={{ maxHeight: 320, overflow: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr style={{ textAlign: "left", borderBottom: "1px solid #e2e8f0" }}>
                  <th style={{ padding: "6px 12px", fontWeight: 700, color: "#64748b" }}>Column</th>
                  <th style={{ padding: "6px 12px", fontWeight: 700, color: "#64748b" }}>Type</th>
                </tr>
              </thead>
              <tbody>
                {cols.map((c, i) => (
                  <tr key={c.name + i} style={{ borderBottom: i < cols.length - 1 ? "1px solid #f1f5f9" : "none" }}>
                    <td style={{ padding: "6px 12px", fontWeight: 600, color: "#1e293b", fontFamily: "ui-monospace, monospace" }}>{c.name}</td>
                    <td style={{ padding: "6px 12px", color: "#64748b" }}>{c.type}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        <div style={{ fontSize: 12.5, color: "#92722a", background: "#fffbeb", border: "1px solid #fde68a", borderRadius: 7, padding: "8px 12px" }}>
          No code or schema captured for this object — send it to intake to attach it there.
        </div>
      )}
    </div>
  );
}
