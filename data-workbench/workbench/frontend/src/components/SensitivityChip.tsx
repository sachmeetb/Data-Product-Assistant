/** Phase 1 sensitivity enum — see _contract_versioning.MERGE_PROPERTY +
 *  DPROD_CREATE_COLUMN. ``none`` is the default and renders nothing so
 *  unflagged columns don't add visual noise. */
type Sensitivity = "none" | "internal" | "confidential" | "pii" | "phi" | string | null | undefined;

interface Props {
  sensitivity: Sensitivity;
  /** Compact mode for tight row layouts; default for column-detail surfaces. */
  compact?: boolean;
}

const STYLE_BY_SENSITIVITY: Record<string, { label: string; bg: string; fg: string; border: string; title: string }> = {
  internal: {
    label: "Internal",
    bg: "#f1f5f9",
    fg: "#334155",
    border: "#cbd5e1",
    title: "Internal data — restricted to employees, no consumer-facing exposure controls.",
  },
  confidential: {
    label: "Confidential",
    bg: "#dbeafe",
    fg: "#1e40af",
    border: "#93c5fd",
    title: "Confidential data — handle with care; expose to consumers only with explicit purpose.",
  },
  pii: {
    label: "PII",
    bg: "#fed7aa",
    fg: "#9a3412",
    border: "#fb923c",
    title: "Personally identifiable information — consumers should mask/hash before exposing in derived views.",
  },
  phi: {
    label: "PHI",
    bg: "#fecaca",
    fg: "#991b1b",
    border: "#f87171",
    title: "Protected health information — additional handling controls apply (HIPAA / regional equivalents).",
  },
};

export default function SensitivityChip({ sensitivity, compact = false }: Props) {
  const key = (sensitivity || "").toString().toLowerCase().trim();
  if (!key || key === "none") return null;
  const style = STYLE_BY_SENSITIVITY[key];
  if (!style) {
    // Unknown enum value — render a generic chip so we don't silently drop
    // the signal. Lets us notice if a new value gets persisted that we
    // forgot to style.
    return (
      <span
        title={`Sensitivity: ${key}`}
        style={{
          padding: compact ? "1px 4px" : "2px 6px",
          fontSize: compact ? 9 : 10,
          fontWeight: 600,
          background: "#e5e7eb",
          color: "#374151",
          border: "1px solid #d1d5db",
          borderRadius: 4,
          letterSpacing: 0.3,
          textTransform: "uppercase",
          whiteSpace: "nowrap",
        }}
      >
        {key}
      </span>
    );
  }
  return (
    <span
      title={style.title}
      style={{
        padding: compact ? "1px 4px" : "2px 6px",
        fontSize: compact ? 9 : 10,
        fontWeight: 600,
        background: style.bg,
        color: style.fg,
        border: `1px solid ${style.border}`,
        borderRadius: 4,
        letterSpacing: 0.3,
        textTransform: "uppercase",
        whiteSpace: "nowrap",
      }}
    >
      {style.label}
    </span>
  );
}
