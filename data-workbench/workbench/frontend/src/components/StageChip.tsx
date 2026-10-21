import type { StageInfo } from "../types";

const STATUS_COLORS: Record<string, string> = {
  pending: "#94a3b8",
  running: "#f59e0b",
  awaiting_review: "#8b5cf6",
  complete: "#22c55e",
  failed: "#ef4444",
  // UI-only state derived in Pipeline.tsx — a pending stage whose unmet
  // upstream is owned by a different role (today: SA materialization
  // waiting on po_source_validation). Amber to distinguish from the grey
  // "engineer's turn next" pending.
  blocked_on_po: "#d97706",
};

const STATUS_LABELS: Record<string, string> = {
  pending: "Pending",
  running: "Running...",
  awaiting_review: "Review",
  complete: "Complete",
  failed: "Failed",
  blocked_on_po: "Pending PO validation",
};

// Simple Unicode icons per stage number
// Icons by stage_id (preferred) and legacy stage number
export const STAGE_ICONS_BY_ID: Record<string, string> = {
  initiate: "\u25B6",                   // ▶
  data_discovery: "\u2317",             // ⌗
  data_discovery_composite: "\u2317",   // ⌗
  load_schema: "\u29BF",               // ⦿
  data_profiling: "\u2261",            // ≡
  data_profiling_composite: "\u2261",  // ≡
  load_profiles: "\u29BE",            // ⦾
  metadata_enrichment: "\u270E",       // ✎
  dq_rule_generation: "\u2696",        // ⚖
  data_mapping: "\u21C4",             // ⇄
  dq_testing_gx: "\u2713",            // ✓
  dq_testing_python: "\u2714",        // ✔
  serving_virtual_view: "\u2750",     // ❐
  serving_physical_copy: "\u2751",   // ❑
  serving_lakehouse_export: "\u25A4", // ▤ layered files
  serving_transfer: "\u21C6",        // ⇆ cross-platform move
  reflect_on_reviews: "\u21BA",       // ↺
  odcs_specification: "\u2709",       // ✉
  odcs_to_dprod: "\u2699",           // ⚙
  publish: "\u2197",                  // ↗
};

export const STAGE_ICONS: Record<number, string> = {
  1:  "\u25B6",    // ▶  Initiate
  2:  "\u2317",    // ⌗  Data Discovery
  3:  "\u29BF",    // ⦿  Load Schema to Graph
  4:  "\u2261",    // ≡  Data Profiling
  5:  "\u29BE",    // ⦾  Load Profiles to Graph
  6:  "\u270E",    // ✎  Metadata Enrichment
  7:  "\u2696",    // ⚖  DQ Rule Generation
  8:  "\u21C4",    // ⇄  Data Mapping
  9:  "\u2713",    // ✓  DQ Testing (GX)
  10: "\u2714",    // ✔  DQ Testing (Python)
};

interface Props {
  stage: StageInfo;
  showName?: boolean;
  // UI-only override applied by Pipeline.tsx when a pending stage is
  // blocked on a cross-role upstream that hasn't completed yet (e.g.
  // SA materialization waiting on po_source_validation). Falls through
  // to stage.status when omitted.
  displayStatus?: string;
}

export default function StageChip({ stage, showName, displayStatus }: Props) {
  const effective = displayStatus || stage.status;
  const color = STATUS_COLORS[effective] || "#94a3b8";
  const label = STATUS_LABELS[effective] || effective;
  const icon = (stage.stage_id && STAGE_ICONS_BY_ID[stage.stage_id]) || STAGE_ICONS[stage.stage_number] || "";

  if (showName) {
    return (
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 5,
          padding: "3px 10px",
          borderRadius: 12,
          fontSize: 11,
          fontWeight: 600,
          color: "#fff",
          backgroundColor: color,
          whiteSpace: "nowrap",
        }}
        title={`${stage.stage_name}: ${label}`}
      >
        <span style={{ fontSize: 10 }}>{icon}</span>
        {stage.stage_name}
      </span>
    );
  }

  return (
    <span
      style={{
        display: "inline-block",
        padding: "2px 10px",
        borderRadius: 12,
        fontSize: 12,
        fontWeight: 600,
        color: "#fff",
        backgroundColor: color,
      }}
    >
      {label}
    </span>
  );
}
