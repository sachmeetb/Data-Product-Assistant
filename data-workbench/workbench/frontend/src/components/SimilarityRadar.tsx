import type { ReactNode } from "react";
import type { ProductSimilarity } from "../types";
import { MATCH, NO_MATCH } from "../lib/dispositionColors";

/**
 * SimilarityRadar — a DYNAMIC-axis radar scoring how similar the compared
 * schemas are, backed by the schema-DNA scorer. Only the axes that were
 * actually measured are plotted (an axis with no value — e.g. an instance-stat
 * dimension with no profiling — is dropped from the polygon and instead listed
 * under "Not measured" with the reason it's missing). With fewer than three
 * measured axes a radar degenerates to a line, so it falls back to bars.
 */

type Dim = ProductSimilarity["dimensions"][number];

// Concise, human definitions of each metric — shown under the name in the
// bars ("table") view so the score reads without needing the hover hint.
const METRIC_DEF: Record<string, string> = {
  data_type: "Datatype family match",
  character: "Name character overlap",
  semantic: "Meaning similarity (embeddings)",
  distribution: "Value spread — needs profiling",
  statistical: "Numeric stats — needs profiling",
};

function band(v: number): string {
  if (v >= 85) return "High";
  if (v >= 65) return "Above average";
  if (v >= 45) return "Average";
  if (v >= 30) return "Below average";
  return "Low";
}

function MissingAxes({ missing }: { missing: Dim[] }) {
  if (!missing.length) return null;
  return (
    <div style={{ marginTop: 10, paddingTop: 8, borderTop: "1px dashed #e2e8f0" }}>
      <div style={{ fontSize: 11, fontWeight: 800, color: "#94a3b8", marginBottom: 4 }}>
        Not measured ({missing.length})
      </div>
      {missing.map((d) => (
        <div key={d.key} style={{ fontSize: 11, color: "#64748b", lineHeight: 1.35, marginBottom: 3 }}>
          <span style={{ fontWeight: 700, color: "#475569" }}>{d.label}</span>: {d.reason || d.hint}
        </div>
      ))}
    </div>
  );
}

// Fallback when fewer than 3 axes are measured — horizontal bars.
function Bars({ dims, accent }: { dims: Dim[]; accent: string }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, margin: "4px 0 6px" }}>
      {dims.map((d) => {
        const v = d.value as number;
        return (
          <div key={d.key}>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, fontWeight: 700, color: "#1e293b" }}>
              <span>{d.label}</span><span style={{ color: accent }}>{v}</span>
            </div>
            {METRIC_DEF[d.key] && (
              <div style={{ fontSize: 10, color: "#94a3b8", margin: "0 0 3px", lineHeight: 1.2 }}>{METRIC_DEF[d.key]}</div>
            )}
            <div style={{ height: 7, borderRadius: 4, background: "#eef2f7", overflow: "hidden" }}>
              <div style={{ width: `${Math.max(0, Math.min(100, v))}%`, height: "100%", background: accent }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default function SimilarityRadar({
  data, compact = false, layout = "radar", muted = false, afterMatch,
}: {
  data: ProductSimilarity;
  compact?: boolean;
  // "radar" (default) for aggregate/product comparisons; "bars" for a single
  // column pair, where a 3–4 point polygon reads poorly in a small popup.
  layout?: "radar" | "bars";
  // Render the whole chart GREY (below-threshold / non-match) instead of the
  // normal purple, so a weak "closest match" never reads as a real one.
  muted?: boolean;
  // Optional content pinned immediately under the "Match: N" line (before the
  // per-dimension read-out) — lets a host place an action right under the score.
  afterMatch?: ReactNode;
}) {
  // Below-threshold comparisons render in the muted NO_MATCH tone so a weak
  // "closest match" never reads as a confident one.
  const accent = muted ? NO_MATCH.stroke : MATCH.stroke;
  const accentFill = muted ? NO_MATCH.fill : MATCH.fill;
  // DYNAMIC: only axes with a real value are plotted; the rest are explained.
  const active = data.dimensions.filter((d): d is Dim & { value: number } => d.value !== null);
  const missing = data.dimensions.filter((d) => d.value === null);

  // Bars for an explicit per-column request, or when a radar would degenerate.
  const useBars = layout === "bars" || active.length < 3;
  const N = active.length;
  const cx = 150, cy = 132, R = 82;
  const angle = (i: number) => (-90 + i * (360 / N)) * (Math.PI / 180);
  const pt = (i: number, r: number): [number, number] => [cx + r * Math.cos(angle(i)), cy + r * Math.sin(angle(i))];
  const poly = (r: (i: number) => number) => active.map((_, i) => pt(i, r(i)).map((n) => n.toFixed(1)).join(",")).join(" ");

  return (
    <div>
      {!useBars ? (
        <svg viewBox="-30 0 360 280" width="100%" style={{ display: "block" }} role="img" aria-label="Similarity radar">
          {/* grid rings */}
          {[0.25, 0.5, 0.75, 1].map((f) => (
            <polygon key={f} points={poly(() => R * f)} fill="none" stroke="#e2e8f0" strokeWidth={1} />
          ))}
          {/* spokes */}
          {active.map((_, i) => {
            const [x, y] = pt(i, R);
            return <line key={i} x1={cx} y1={cy} x2={x} y2={y} stroke="#e2e8f0" strokeWidth={1} />;
          })}
          {/* value polygon */}
          <polygon points={poly((i) => R * Math.max(0, Math.min(100, active[i].value)) / 100)} fill={accentFill} stroke={accent} strokeWidth={2} strokeLinejoin="round" />
          {/* value vertices */}
          {active.map((d, i) => {
            const [x, y] = pt(i, R * Math.max(0, Math.min(100, d.value)) / 100);
            return <circle key={i} cx={x} cy={y} r={2.6} fill={accent} />;
          })}
          {/* axis labels + values */}
          {active.map((d, i) => {
            const [lx, ly] = pt(i, R + 16);
            const c = Math.cos(angle(i)), s = Math.sin(angle(i));
            const anchor = Math.abs(c) < 0.35 ? "middle" : c > 0 ? "start" : "end";
            const dy = Math.abs(s) < 0.35 ? 0 : s > 0 ? 9 : -3;
            return (
              <text key={i} x={lx} y={ly + dy} textAnchor={anchor as "middle" | "start" | "end"} fontSize={11} fill="#334155">
                <tspan fontWeight={700}>{d.label}</tspan>
                <tspan fontWeight={800} fill={accent}>{"  "}{d.value}</tspan>
              </text>
            );
          })}
        </svg>
      ) : (
        <Bars dims={active} accent={accent} />
      )}

      {/* Match score */}
      <div style={{ textAlign: "center", margin: compact ? "0 0 2px" : "2px 0 10px" }}>
        <span style={{ fontSize: compact ? 12 : 13, fontWeight: 700, color: "#64748b" }}>Match: </span>
        <span style={{ fontSize: compact ? 17 : 20, fontWeight: 800, color: accent }}>{data.match}</span>
        {muted && <span style={{ fontSize: 10, fontWeight: 700, color: NO_MATCH.stroke, marginLeft: 6 }}>· below threshold</span>}
      </div>

      {afterMatch}

      {/* Per-dimension read-out (radar full mode only — bars already show
          label + value, so skip the duplicate list there). */}
      {!compact && !useBars && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {active.map((d) => (
            <div key={d.key} style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
              <span style={{ width: 8, height: 8, borderRadius: "50%", background: accent, marginTop: 5, flex: "none" }} />
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 12, fontWeight: 700, color: "#1e293b" }}>
                  {d.label}: {d.value} <span style={{ fontWeight: 600, color: "#94a3b8" }}>· {band(d.value)}</span>
                </div>
                <div style={{ fontSize: 11, color: "#64748b", lineHeight: 1.35 }}>{d.hint}</div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Why each missing axis is missing — shown in both modes. */}
      <MissingAxes missing={missing} />
    </div>
  );
}
