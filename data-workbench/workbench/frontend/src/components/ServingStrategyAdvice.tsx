import { useState, useCallback } from "react";
import api from "../api/client";
import { type ServingMode, MODE_LABELS, MODE_TO_PATTERN, PATTERN_TO_MODE, MODE_ACCENT } from "../lib/servingModes";

/**
 * Serving Strategy Advisor surface — the four serving modes: Virtual (SQL view),
 * Materialized (dbt), Lakehouse (Parquet + DuckDB), and Cross-platform transfer.
 *
 * The recommendation + selectable modes are driven by the backend's
 * capability-gated pattern taxonomy (`patterns[]` / `recommended_pattern`);
 * infeasible modes are shown but disabled with their reason.
 *
 * Two callers, one backend engine:
 *  - PO wizard: pass `signals` (in-flight scd/grouping) → unscoped endpoint;
 *    pass `onChoose` to render the selectable "send to engineering as" picker.
 *  - Engineer at serving stage: pass `projectId` → reads the product's graph;
 *    pass `onApply` (+ `currentMode`) for the read-only recommendation + Apply.
 *
 * Manual-fire (matches the wizard's other readiness lenses): renders a button
 * until the PO/engineer asks for the recommendation.
 */

interface Driver {
  code: string;
  label: string;
  detail: string;
  severity: "required" | "consider" | "info";
  datasets?: string[];
}
interface ServingPattern {
  pattern: string;
  label: string;
  feasibility: "feasible" | "impossible" | "not_yet_supported";
  feasibility_reason: string;
  recommended?: boolean;
  rationale?: string;
}
interface Recommendation {
  recommended_mode: "virtual" | "materialized";
  required: boolean;
  confidence: number;
  drivers: Driver[];
  rationale: string;
  considerations?: string[];
  source: "heuristic" | "skill";
  advisor_error?: string | null;
  // Capability-aware pattern taxonomy (Phase 2) — read-only here; the engineer
  // picks the concrete pattern (incl. lakehouse / cross-platform transfer) in
  // Configure Serving. Absent on older backends → the panel just doesn't render.
  patterns?: ServingPattern[];
  recommended_pattern?: string | null;
  source_platform?: string;
  target_platform?: string;
}

interface Signals {
  archetype: string;
  datasets: { name: string; scd_policy: string; grouping: boolean }[];
  cross_platform?: boolean;
  // 'source' | 'aggregate' | 'consumer' — an aggregate defaults to a
  // materialized recommendation (so downstream consumers borrow a real table).
  product_kind?: string;
}

interface Props {
  projectId?: number;
  signals?: Signals;
  compact?: boolean;
  /** Current serving mode (so the Apply button can no-op when already aligned). */
  currentMode?: ServingMode;
  /** When set, renders an "Apply recommendation" button that asks the parent to
   *  switch serving mode to the recommended one. */
  onApply?: (mode: ServingMode) => void;
  /** PO-side (wizard): renders a selectable control so the PO records a serving
   *  preference (among feasible modes) for the engineer. `chosen` is the current
   *  selection. */
  onChoose?: (mode: ServingMode, reason: string) => void;
  chosen?: ServingMode | null;
}

const SEVERITY_STYLE: Record<Driver["severity"], { bg: string; fg: string; tag: string }> = {
  required: { bg: "#fef2f2", fg: "#b91c1c", tag: "REQUIRED" },
  consider: { bg: "#fffbeb", fg: "#92400e", tag: "CONSIDER" },
  info: { bg: "#f0f9ff", fg: "#0369a1", tag: "NOTE" },
};

export default function ServingStrategyAdvice({ projectId, signals, compact, currentMode, onApply, onChoose, chosen }: Props) {
  const [rec, setRec] = useState<Recommendation | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchAdvice = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = signals
        ? await api.post(`/api/serving-strategy/advise`, signals)
        : await api.post(`/api/projects/${projectId}/serving-strategy/advise`, {});
      setRec(res.data);
    } catch (e) {
      setError(`Could not get a recommendation: ${(e as Error).message || e}`);
    }
    setLoading(false);
  }, [projectId, signals]);

  if (!rec && !loading) {
    return (
      <div style={{ border: "1px dashed #cbd5e1", borderRadius: 8, padding: 14, textAlign: "center" }}>
        <div style={{ fontSize: 13, color: "#475569", marginBottom: 8 }}>
          How should this product be served — a live <b>SQL view</b>, dbt-<b>materialized</b> tables,
          a portable <b>Parquet + DuckDB lakehouse</b>, or a <b>cross-platform transfer</b>?
          Get a recommendation for your shape and sources.
        </div>
        <button onClick={fetchAdvice} style={{ padding: "6px 16px", borderRadius: 6, border: "none", background: "#3b82f6", color: "#fff", fontSize: 13, fontWeight: 600, cursor: "pointer" }}>
          Recommend serving strategy
        </button>
        {error && <div style={{ color: "#b91c1c", fontSize: 12, marginTop: 8 }}>{error}</div>}
      </div>
    );
  }

  if (loading) return <div style={{ color: "#64748b", fontSize: 13, padding: 12 }}>Analyzing…</div>;
  if (!rec) return null;

  // The picker's recommendation comes from the capability-gated pattern taxonomy
  // (which can point at lakehouse / transfer); fall back to the binary heuristic
  // mode on older backends that don't return a pattern.
  const recMode: ServingMode = PATTERN_TO_MODE[rec.recommended_pattern ?? ""] ?? rec.recommended_mode;
  const recAccent = MODE_ACCENT[recMode];
  return (
    <div style={{ border: "1px solid #e2e8f0", borderRadius: 8, padding: 14 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <span style={{ fontSize: 13, color: "#475569", fontWeight: 600 }}>Recommended:</span>
        <span style={{
          fontSize: 13, fontWeight: 700, padding: "4px 12px", borderRadius: 999,
          background: recAccent.bg, color: recAccent.fg,
        }}>
          {MODE_LABELS[recMode]}
        </span>
        {rec.required && (
          <span style={{ fontSize: 11, fontWeight: 700, color: "#b91c1c", background: "#fef2f2", borderRadius: 4, padding: "3px 8px" }}>
            REQUIRED — not just recommended
          </span>
        )}
        <span style={{ fontSize: 11, color: "#94a3b8", marginLeft: "auto" }}>
          {rec.source === "skill" ? "advisor" : "heuristic"} · {rec.confidence}%
        </span>
      </div>

      <p style={{ fontSize: 13, color: "#334155", lineHeight: 1.5, margin: "10px 0" }}>{rec.rationale}</p>

      {!compact && !onChoose && rec.patterns && rec.patterns.length > 0 && (() => {
        const feasible = rec.patterns.filter((p) => p.feasibility === "feasible")
          .sort((a, b) => (b.recommended ? 1 : 0) - (a.recommended ? 1 : 0));
        const unavailable = rec.patterns.filter((p) => p.feasibility !== "feasible");
        return (
          <div style={{ marginTop: 6, marginBottom: 10, paddingTop: 10, borderTop: "1px dashed #e2e8f0" }}>
            <div style={{ fontSize: 12, fontWeight: 700, color: "#475569", marginBottom: 6 }}>
              Serving patterns
              {rec.source_platform && (
                <span style={{ fontWeight: 500, color: "#94a3b8", fontSize: 11, marginLeft: 6 }}>
                  {rec.source_platform} → {rec.target_platform}
                </span>
              )}
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
              {feasible.map((p) => (
                <div key={p.pattern} style={{ fontSize: 12, color: "#334155" }}>
                  <span style={{ fontWeight: 700 }}>{p.label}</span>
                  {p.recommended && (
                    <span style={{ marginLeft: 6, fontSize: 9.5, fontWeight: 800, color: "#15803d",
                      background: "#dcfce7", borderRadius: 4, padding: "1px 5px" }}>RECOMMENDED</span>
                  )}
                  <span style={{ color: "#64748b" }}> — {p.rationale || p.feasibility_reason}</span>
                </div>
              ))}
              {unavailable.map((p) => (
                <div key={p.pattern} style={{ fontSize: 11.5, color: "#94a3b8" }}>
                  <span style={{ fontWeight: 600 }}>{p.label}</span>
                  <span style={{ fontSize: 9.5, fontWeight: 700, marginLeft: 6,
                    color: p.feasibility === "not_yet_supported" ? "#b45309" : "#94a3b8" }}>
                    {p.feasibility === "not_yet_supported" ? "COMING SOON" : "NOT POSSIBLE"}
                  </span>
                  <span> — {p.feasibility_reason}</span>
                </div>
              ))}
            </div>
          </div>
        );
      })()}

      {onApply && (
        currentMode === recMode ? (
          <div style={{ fontSize: 12, color: "#16a34a", fontWeight: 600 }}>✓ Current serving mode matches the recommendation.</div>
        ) : (
          <button
            onClick={() => onApply(recMode)}
            style={{ padding: "5px 14px", borderRadius: 6, border: "none", background: recAccent.fg,
              color: "#fff", fontSize: 12, fontWeight: 700, cursor: "pointer" }}
          >
            Apply: switch to {MODE_LABELS[recMode]}
          </button>
        )
      )}

      {onChoose && (() => {
        // Selectable picker across all four modes, feasibility-gated by the
        // advisor's pattern taxonomy. The effective selection is the explicit PO
        // choice, else the recommendation.
        const effective = chosen ?? recMode;
        const MODE_ORDER: ServingMode[] = ["virtual", "materialized", "lakehouse", "transfer"];
        const byPattern = new Map((rec.patterns ?? []).map((p) => [p.pattern, p]));
        // Back-compat: an older backend with no patterns[] can still offer the
        // two native modes.
        const rows = MODE_ORDER.map((mode) => {
          const pat = byPattern.get(MODE_TO_PATTERN[mode]);
          const feasibility: ServingPattern["feasibility"] = pat
            ? pat.feasibility
            : (mode === "virtual" || mode === "materialized" ? "feasible" : "impossible");
          return { mode, pat, feasibility, isRec: mode === recMode };
        });
        return (
          <div style={{ marginTop: 10, paddingTop: 10, borderTop: "1px dashed #e2e8f0" }}>
            <div style={{ fontSize: 12, fontWeight: 600, color: "#475569", marginBottom: 6 }}>
              Send to engineering as:
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {rows.map(({ mode, pat, feasibility, isRec }) => {
                const selectable = feasibility === "feasible";
                const active = selectable && effective === mode;
                const accent = MODE_ACCENT[mode];
                return (
                  <button
                    key={mode}
                    disabled={!selectable}
                    onClick={() => selectable && onChoose(mode, pat?.rationale || rec.rationale)}
                    style={{
                      textAlign: "left", padding: "8px 10px", borderRadius: 6, fontSize: 12,
                      border: `1px solid ${active ? accent.fg : "#e2e8f0"}`,
                      background: active ? accent.bg : "#fff",
                      color: selectable ? "#334155" : "#94a3b8",
                      cursor: selectable ? "pointer" : "not-allowed",
                      opacity: selectable ? 1 : 0.85,
                    }}
                  >
                    <span style={{ fontWeight: 700, color: active ? accent.fg : (selectable ? "#334155" : "#94a3b8") }}>
                      {MODE_LABELS[mode]}
                    </span>
                    {isRec && (
                      <span style={{ marginLeft: 6, fontSize: 9.5, fontWeight: 800, color: "#15803d",
                        background: "#dcfce7", borderRadius: 4, padding: "1px 5px" }}>★ RECOMMENDED</span>
                    )}
                    {!selectable && (
                      <span style={{ marginLeft: 6, fontSize: 9.5, fontWeight: 700,
                        color: feasibility === "not_yet_supported" ? "#b45309" : "#94a3b8" }}>
                        {feasibility === "not_yet_supported" ? "COMING SOON" : "NOT POSSIBLE"}
                      </span>
                    )}
                    {(pat?.rationale || pat?.feasibility_reason) && (
                      <div style={{ color: "#64748b", marginTop: 2 }}>
                        {selectable ? (pat?.rationale || pat?.feasibility_reason) : pat?.feasibility_reason}
                      </div>
                    )}
                  </button>
                );
              })}
            </div>
            <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 6 }}>
              ★ = advisor recommendation{chosen && chosen !== recMode ? " · you've overridden it" : ""}. The engineer confirms the concrete mode and target at Configure Serving.
            </div>
          </div>
        );
      })()}

      {!compact && rec.drivers.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {rec.drivers.map((d) => {
            const st = SEVERITY_STYLE[d.severity];
            return (
              <div key={d.code} style={{ background: st.bg, borderRadius: 6, padding: "6px 10px" }}>
                <span style={{ fontSize: 10, fontWeight: 800, color: st.fg, marginRight: 8 }}>{st.tag}</span>
                <span style={{ fontSize: 12, fontWeight: 700, color: "#334155" }}>{d.label}</span>
                <div style={{ fontSize: 12, color: "#475569", marginTop: 2 }}>{d.detail}</div>
              </div>
            );
          })}
        </div>
      )}

      {!compact && rec.considerations && rec.considerations.length > 0 && (
        <ul style={{ margin: "10px 0 0", paddingLeft: 18, fontSize: 12, color: "#64748b" }}>
          {rec.considerations.map((c, i) => <li key={i}>{c}</li>)}
        </ul>
      )}

      {rec.advisor_error && (
        <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 8 }}>
          (Narrative advisor unavailable — showing the deterministic recommendation.)
        </div>
      )}
    </div>
  );
}
