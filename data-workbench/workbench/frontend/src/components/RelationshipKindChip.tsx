/**
 * Renders a colored chip showing a table's `relationship_kind` classification.
 * Set by the metadata-enrichment skill (`:TableDescription.relationshipKind`)
 * and PO-approved in the SourceProductValidationPanel's Tables tab. Surfaces
 * across the workbench to give consumers / engineers context on what kind of
 * relationship a table embodies — same chip everywhere so the signal is
 * recognizable wherever the user encounters it.
 *
 * Values mirror the metadata-enrichment skill's classification:
 *   fact                — primary entity
 *   lookup_dimension    — small reference table
 *   general_membership  — M:N junction (preferred by bridge ranker)
 *   specialization      — role-specific subset
 *   audit_log           — history / event log
 *   configuration       — system settings
 *   unknown             — generator couldn't classify
 */

export const RELATIONSHIP_KIND_LABELS: Record<string, string> = {
  fact: "Fact",
  lookup_dimension: "Lookup",
  general_membership: "Membership",
  specialization: "Specialization",
  audit_log: "Audit log",
  configuration: "Config",
  unknown: "Unknown",
};

const KIND_BG: Record<string, string> = {
  general_membership: "#dcfce7",
  specialization:     "#fef3c7",
  fact:               "#dbeafe",
  lookup_dimension:   "#e0e7ff",
  audit_log:          "#fee2e2",
  configuration:      "#f1f5f9",
  unknown:            "#f1f5f9",
};
const KIND_FG: Record<string, string> = {
  general_membership: "#166534",
  specialization:     "#92400e",
  fact:               "#1e3a8a",
  lookup_dimension:   "#3730a3",
  audit_log:          "#991b1b",
  configuration:      "#475569",
  unknown:            "#475569",
};

interface Props {
  /** Empty / falsy → chip is not rendered (consumer datasets without an
   *  approved table description fall through). */
  kind: string | null | undefined;
  /** Use the long label ("General membership (M:N junction)") instead of
   *  the short one ("Membership"). Useful for accessibility / first
   *  exposure surfaces. */
  long?: boolean;
  /** Extra style overrides (e.g. fontSize for dense rows). */
  style?: React.CSSProperties;
}

export default function RelationshipKindChip({ kind, long = false, style }: Props) {
  if (!kind) return null;
  const k = String(kind).toLowerCase();
  const bg = KIND_BG[k] || KIND_BG.unknown;
  const fg = KIND_FG[k] || KIND_FG.unknown;
  const label = long ? LONG_LABELS[k] || k : RELATIONSHIP_KIND_LABELS[k] || k;
  return (
    <span
      style={{
        fontSize: 10,
        fontWeight: 700,
        padding: "2px 6px",
        borderRadius: 4,
        backgroundColor: bg,
        color: fg,
        letterSpacing: 0.2,
        whiteSpace: "nowrap",
        ...style,
      }}
      title={`relationship_kind: ${k} — drives consumer-side bridge ranker`}
    >
      {label}
    </span>
  );
}

const LONG_LABELS: Record<string, string> = {
  fact: "Fact (primary entity)",
  lookup_dimension: "Lookup / dimension",
  general_membership: "General membership (M:N junction)",
  specialization: "Specialization (role-specific)",
  audit_log: "Audit / event log",
  configuration: "Configuration",
  unknown: "Unknown",
};
