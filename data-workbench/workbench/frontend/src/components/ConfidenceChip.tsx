/** Confidence band for an intake-blueprint field (mirrors
 *  intake_blueprint.Confidence). Colour-coded like GapAnalysisSection's status
 *  palette so "please confirm" (low/missing) reads distinctly from "accept"
 *  (high). */
export type Confidence = "high" | "medium" | "low" | "missing" | string | null | undefined;

const STYLE: Record<string, { label: string; bg: string; fg: string; border: string }> = {
  high: { label: "High", bg: "#ecfdf5", fg: "#065f46", border: "#a7f3d0" },
  medium: { label: "Medium", bg: "#eef2ff", fg: "#4338ca", border: "#c7d2fe" },
  low: { label: "Low", bg: "#fefce8", fg: "#854d0e", border: "#fde68a" },
  missing: { label: "Missing", bg: "#fef2f2", fg: "#991b1b", border: "#fecaca" },
};

export default function ConfidenceChip({ confidence, compact = false }: { confidence: Confidence; compact?: boolean }) {
  const key = (confidence || "missing").toString().toLowerCase().trim();
  const s = STYLE[key] || STYLE.missing;
  return (
    <span
      title={`Parser confidence: ${key}`}
      style={{
        padding: compact ? "1px 5px" : "2px 7px",
        fontSize: compact ? 9 : 10,
        fontWeight: 600,
        background: s.bg,
        color: s.fg,
        border: `1px solid ${s.border}`,
        borderRadius: 4,
        letterSpacing: 0.3,
        textTransform: "uppercase",
        whiteSpace: "nowrap",
      }}
    >
      {s.label}
    </span>
  );
}
