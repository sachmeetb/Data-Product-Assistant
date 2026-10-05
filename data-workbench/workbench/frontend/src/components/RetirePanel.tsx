import { useEffect, useMemo, useState } from "react";
import api from "../api/client";
import type { EstateObject } from "../types";
import { DISPOSITION } from "../lib/dispositionColors";

// This panel is the retire surface — one tone, pulled from the shared palette.
const R = DISPOSITION.retire;

interface Props {
  projectId: number;
  object: EstateObject;
  objById: Record<string, EstateObject>;
  edges: [string, string][];
  // Whether the proposed-DAG preview is currently active for THIS node — owned
  // by the parent (same source of truth as the DAG banner) so the two stay in
  // sync: cancelling the banner reverts these buttons, and vice versa.
  proposed: boolean;
  // Step 1: render the proposed post-retire DAG (node removed, orphans flagged).
  onPreview: (obj: EstateObject) => void;
  // Inline action: retire right here in the DAG (drops the node from the view).
  // Analogue of migrate's "Run Lakebridge conversion" — no page change.
  onRetireInView: (obj: EstateObject) => void;
  // Fired when an impact analysis is generated, with the node ids the backend
  // flagged (downstream that would be orphaned + upstream that becomes dead
  // weight) — the DAG badges these. Empty array clears them.
  onCautionNodes?: (ids: string[]) => void;
  // Fired alongside onCautionNodes with the structured caution detail (the
  // analogue of MigratePanel's onCautionInfo): upstream feeders that
  // go dead, downstream that would be orphaned, and the skill's caution prose.
  // `target` is empty for retire (no destination platform). Cleared to empty on
  // object change / before a re-analysis.
  onCautionInfo?: (info: { blocking_upstream: string[]; gating_downstream: string[]; cautions: string[]; target: string }) => void;
}

type Verdict = "safe" | "caution" | "blocked";

// The skill/heuristic-composed impact narrative from the backend. The verdict
// and the dependency facts are computed deterministically server-side — the
// skill only supplies the prose (summary / steps / cautions).
interface ImpactAnalysis {
  verdict: Verdict;
  summary: string;
  pre_retire_steps: string[];
  cautions: string[];
  source?: "skill" | "heuristic";
}

const VERDICT_STYLE: Record<Verdict, { fg: string; bg: string; border: string; label: string }> = {
  safe: { fg: "#047857", bg: "#f0fdf4", border: "#bbf7d0", label: "Safe to retire" },
  caution: { fg: "#b45309", bg: "#fffbeb", border: "#fde68a", label: "Retire with care" },
  blocked: { fg: R.fg, bg: R.wash, border: R.border, label: "Blocked — breaks consumers" },
};

// Inline retirement impact for a single "retire" node. Retire is ANALYSIS ONLY:
// it never creates a task and never reaches intake — the point is to understand
// the blast radius before someone decommissions by hand. A skill
// (data-retirement-impact-analyzer)
// analyses "what happens if this node is removed": the backend computes the
// dependency blast radius (which downstream consumers get orphaned, which
// upstream feeders go dead) + a safe/caution/blocked verdict deterministically,
// and the skill narrates it. The client-side table below is the instant
// grounding shown before the analysis runs. Nothing here persists.
export default function RetirePanel({ projectId, object, objById, edges, proposed, onPreview, onRetireInView, onCautionNodes, onCautionInfo }: Props) {
  // Instant client-side blast-radius (grounding) — walks the lineage edges so
  // the downstream table renders the moment the node is selected, before any
  // skill call. The backend computes the authoritative version for the verdict.
  const impact = useMemo(() => {
    const fwd: Record<string, string[]> = {};
    const bwd: Record<string, string[]> = {};
    edges.forEach(([a, b]) => { (fwd[a] = fwd[a] || []).push(b); (bwd[b] = bwd[b] || []).push(a); });
    const downstream = fwd[object.id] || [];
    const rows = downstream.map((d) => {
      const otherFeeders = (bwd[d] || []).filter((f) => f !== object.id);
      return {
        id: d,
        name: objById[d]?.name || d,
        orphaned: otherFeeders.length === 0,
        survivors: otherFeeders.map((f) => objById[f]?.name || f),
      };
    });
    return { rows, orphanCount: rows.filter((r) => r.orphaned).length };
  }, [object.id, objById, edges]);

  const [analysis, setAnalysis] = useState<ImpactAnalysis | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [analysisErr, setAnalysisErr] = useState<string | null>(null);

  // Reset the note + analysis when the selected object changes, and clear the
  // DAG's caution badges from the previous node's analysis.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { setAnalysis(null); setAnalysisErr(null); onCautionNodes?.([]); onCautionInfo?.({ blocking_upstream: [], gating_downstream: [], cautions: [], target: "" }); }, [object.id]);

  const generateAnalysis = async () => {
    setAnalyzing(true);
    setAnalysisErr(null);
    try {
      const res = await api.post(`/api/projects/${projectId}/discovery/${object.id}/retirement-impact`, { comment: null });
      const impact = res.data?.impact as ImpactAnalysis;
      setAnalysis(impact);
      // Badge the exact nodes the backend flagged: consumers that lose their
      // only feeder + upstream feeders that become dead weight.
      const cn = res.data?.caution_nodes as { orphaned_downstream?: string[]; dead_upstream?: string[] } | undefined;
      onCautionNodes?.([...(cn?.orphaned_downstream || []), ...(cn?.dead_upstream || [])]);
      // Structured caution detail (retire has no target platform). dead upstream →
      // blocking_upstream; orphaned downstream → gating_downstream — mirrors the
      // migrate panel so a shared consumer can render either the same way.
      onCautionInfo?.({
        blocking_upstream: cn?.dead_upstream || [],
        gating_downstream: cn?.orphaned_downstream || [],
        cautions: impact?.cautions || [],
        target: "",
      });
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } } };
      setAnalysisErr(err.response?.data?.detail || "Could not analyze the retirement impact.");
    } finally {
      setAnalyzing(false);
    }
  };

  const preview = () => onPreview(object);
  const v = analysis ? VERDICT_STYLE[analysis.verdict] : null;

  return (
    <div style={{ marginTop: 12, border: `1px solid ${R.border}`, borderRadius: 10, background: R.wash, padding: 14 }}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 12, flexWrap: "wrap", marginBottom: 10 }}>
        <div style={{ flex: 1, minWidth: 220 }}>
          <div style={{ fontSize: 15, fontWeight: 500, color: R.fg }}>
            Retire <span style={{ fontFamily: "ui-monospace, monospace" }}>{object.name}</span>
          </div>
          <div style={{ fontSize: 12, color: "#64748b", marginTop: 2 }}>
            {object.platform || object.database || "legacy"} · decommission impact. Nothing is saved.
          </div>
        </div>
        <div style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          {!proposed ? (
            <button onClick={preview} style={{ fontSize: 12.5, fontWeight: 700, color: "#fff", background: R.fg, border: "none", borderRadius: 7, padding: "7px 14px", cursor: "pointer" }}>
              ✦ Preview proposed change
            </button>
          ) : (
            <button onClick={() => onRetireInView(object)} style={{ fontSize: 12.5, fontWeight: 700, color: "#fff", background: R.fg, border: "none", borderRadius: 7, padding: "7px 14px", cursor: "pointer" }}>
              Apply changes
            </button>
          )}
        </div>
      </div>

      {/* Skill-backed impact analysis ("what happens if this node is removed"),
          derived from the lineage — no free-text intent. The verdict + facts are
          computed server-side; the skill narrates them. */}
      <div style={{ border: `1px solid ${R.bg}`, borderRadius: 8, background: "#fff", padding: 12, marginBottom: 12 }}>
        <div style={{ fontSize: 12, fontWeight: 700, color: "#1e293b", marginBottom: 6 }}>
          Retirement impact
          <span style={{ fontWeight: 400, color: "#64748b" }}> — analysed from this node's lineage; the data engineer confirms before decommissioning.</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 8 }}>
          <button
            onClick={generateAnalysis}
            disabled={analyzing}
            style={{ fontSize: 12.5, fontWeight: 700, color: "#fff", background: analyzing ? "#94a3b8" : R.fg, border: "none", borderRadius: 7, padding: "6px 13px", cursor: analyzing ? "default" : "pointer" }}
          >
            {analyzing ? "Analyzing…" : analysis ? "↻ Re-analyze impact" : "✦ Analyze retirement impact"}
          </button>
          {analysisErr && <span style={{ fontSize: 12, color: R.fg }}>{analysisErr}</span>}
        </div>

        {analysis && v && (
          <div style={{ marginTop: 12, borderTop: `1px solid ${R.bg}`, paddingTop: 10 }}>
            <div style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11, fontWeight: 800, textTransform: "uppercase", letterSpacing: 0.4, color: v.fg, background: v.bg, border: `1px solid ${v.border}`, borderRadius: 6, padding: "3px 9px", marginBottom: 8 }}>
              {v.label}
            </div>
            <div style={{ fontSize: 12.5, color: "#1e293b", lineHeight: 1.5, marginBottom: 10 }}>{analysis.summary}</div>
            {analysis.pre_retire_steps.length > 0 && (
              <>
                <div style={{ fontSize: 11, fontWeight: 700, color: "#334155", textTransform: "uppercase", letterSpacing: 0.3, marginBottom: 4 }}>
                  Before you decommission
                </div>
                <ol style={{ margin: "0 0 8px", paddingLeft: 20, display: "flex", flexDirection: "column", gap: 5 }}>
                  {analysis.pre_retire_steps.map((step, i) => (
                    <li key={i} style={{ fontSize: 12.5, color: "#1e293b", lineHeight: 1.45 }}>{step}</li>
                  ))}
                </ol>
              </>
            )}
            {analysis.cautions.length > 0 && (
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                {analysis.cautions.map((c, i) => (
                  <div key={i} style={{ fontSize: 12, color: "#92722a", background: "#fffbeb", border: "1px solid #fde68a", borderRadius: 6, padding: "5px 9px" }}>⚠ {c}</div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Instant client-side grounding — the downstream consumers and whether
          each survives, shown before/alongside the skill analysis. */}
      {!analysis && (
        <div style={{ fontSize: 12.5, color: object.active === false ? "#047857" : "#334155", marginBottom: 8 }}>
          {object.active === false && <span style={{ fontWeight: 700 }}>Inactive · </span>}
          {impact.orphanCount === 0
            ? `${impact.rows.length} downstream consumer${impact.rows.length === 1 ? "" : "s"} keep another feeder. Analyze for the full impact.`
            : `⚠ ${impact.orphanCount} downstream consumer${impact.orphanCount === 1 ? "" : "s"} would lose their only source. Analyze for the full impact.`}
        </div>
      )}

      {impact.rows.length > 0 && (
        <div style={{ border: "1px solid #e2e8f0", borderRadius: 8, overflow: "hidden", background: "#fff" }}>
          <div style={{ padding: "7px 12px", background: "#f8fafc", borderBottom: "1px solid #e2e8f0", fontSize: 12, fontWeight: 700, color: "#334155" }}>
            Downstream impact <span style={{ color: "#94a3b8", fontWeight: 400 }}>· {impact.rows.length}</span>
          </div>
          <div style={{ maxHeight: 200, overflow: "auto" }}>
            {impact.rows.map((r, i) => (
              <div key={r.id} style={{ display: "flex", alignItems: "center", gap: 10, padding: "6px 12px", borderBottom: i < impact.rows.length - 1 ? "1px solid #f1f5f9" : "none", fontSize: 12 }}>
                <span style={{ flex: "none", width: 74, fontSize: 9.5, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.04em", color: r.orphaned ? "#b45309" : "#047857" }}>
                  {r.orphaned ? "⚠ orphaned" : "✓ served"}
                </span>
                <span style={{ flex: "none", width: 180, fontWeight: 600, color: "#1e293b", fontFamily: "ui-monospace, monospace", fontSize: 11 }}>{r.name}</span>
                <span style={{ flex: 1, minWidth: 0, color: "#64748b" }}>
                  {r.orphaned ? `${object.name} is its only source` : `still fed by ${r.survivors.join(", ")}`}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
