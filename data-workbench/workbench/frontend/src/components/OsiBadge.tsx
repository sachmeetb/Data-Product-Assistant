// Readiness badge — small RAG pill rendered on every surface that shows
// the producer-side readiness score (marketplace list, marketplace detail,
// My Products dashboard, wizard step header). Score-only by design; the
// analysis narrative is wired up via OsiAnalysisPanel separately so
// marketplace consumers never see the underlying detail.
//
// The rubric the score was computed against drives the visible chip label
// ("OSI" by default, "AI-Ready" for the alternative rubric). Callers pass
// `rubricShortLabel` from the marketplace/MyProducts API; absent value
// falls back to "OSI" for back-compat with pre-rubric contracts.

export type OsiBand = "red" | "amber" | "green" | null | undefined;

interface Props {
  band: OsiBand;
  completeness: number | null | undefined;
  conformancePass?: boolean | null;
  size?: "sm" | "md";
  showLabel?: boolean;
  /** Chip label — defaults to "OSI" for back-compat. Marketplace + MyProducts
   *  pass the contract's rubric short_label so AI-Ready products render
   *  with the right chip. */
  rubricShortLabel?: string | null;
}

const PALETTE: Record<"red" | "amber" | "green", { bg: string; fg: string; dot: string; label: string }> = {
  red: { bg: "#fee2e2", fg: "#991b1b", dot: "#dc2626", label: "Red" },
  amber: { bg: "#fef3c7", fg: "#92400e", dot: "#d97706", label: "Amber" },
  green: { bg: "#dcfce7", fg: "#065f46", dot: "#16a34a", label: "Green" },
};

const NEUTRAL = { bg: "#f1f5f9", fg: "#64748b", dot: "#94a3b8", label: "Not scored" };

export default function OsiBadge({
  band,
  completeness,
  conformancePass,
  size = "md",
  showLabel = true,
  rubricShortLabel,
}: Props) {
  const palette = band ? PALETTE[band] : NEUTRAL;
  const compactPad = size === "sm" ? "2px 8px" : "4px 10px";
  const dotSize = size === "sm" ? 6 : 8;
  const fontSize = size === "sm" ? 11 : 12;
  const completenessLabel =
    typeof completeness === "number" ? `${completeness}%` : null;
  const chipLabel = (rubricShortLabel || "OSI").trim() || "OSI";

  return (
    <span
      title={
        band
          ? `${chipLabel}${completenessLabel ? " " + completenessLabel : ""}` +
            (conformancePass === false ? " · conformance fail" : "")
          : `${chipLabel} score not yet computed`
      }
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: compactPad,
        borderRadius: 999,
        backgroundColor: palette.bg,
        color: palette.fg,
        fontSize,
        fontWeight: 600,
        whiteSpace: "nowrap",
      }}
    >
      <span
        aria-hidden
        style={{
          width: dotSize,
          height: dotSize,
          borderRadius: "50%",
          backgroundColor: palette.dot,
          display: "inline-block",
        }}
      />
      {showLabel && <span>{chipLabel}</span>}
      {completenessLabel && <span style={{ opacity: 0.85 }}>{completenessLabel}</span>}
      {conformancePass === false && (
        <span
          aria-label="Conformance fail"
          title="Conformance fail"
          style={{ marginLeft: 2, fontSize: fontSize - 1, opacity: 0.85 }}
        >
          !
        </span>
      )}
    </span>
  );
}
