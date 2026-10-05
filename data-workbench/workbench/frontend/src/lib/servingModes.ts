// Shared serving-mode vocabulary.
//
// A product is served one of four ways; the axes here map between the three
// namespaces that describe them:
//   - the PO-facing MODE ("virtual" | "materialized" | "lakehouse" | "transfer")
//   - the advisor's capability-gated PATTERN id (native_virtual, …)
//   - the exclusive-group STAGE id that implements it (serving_virtual_view, …)
//
// Centralised so ConfigureServingDialog, ServingStrategyAdvice,
// ProductOwnerContextBanner, and MaterializationGateDialog stay in lockstep.

export type ServingMode = "virtual" | "materialized" | "lakehouse" | "transfer";

export const MODE_LABELS: Record<ServingMode, string> = {
  virtual: "Virtual (SQL view)",
  materialized: "Materialized (dbt)",
  lakehouse: "Lakehouse (Parquet + DuckDB)",
  transfer: "Cross-platform transfer",
};

// A serving mode maps to the exclusive-group stage that implements it.
export const MODE_TO_STAGE: Record<ServingMode, string> = {
  virtual: "serving_virtual_view",
  materialized: "serving_physical_copy",
  lakehouse: "serving_lakehouse_export",
  transfer: "serving_transfer",
};

// Reverse of MODE_TO_STAGE — the enabled serving stage_id → PO-facing mode.
export const STAGE_TO_MODE: Record<string, ServingMode> = {
  serving_virtual_view: "virtual",
  serving_physical_copy: "materialized",
  serving_lakehouse_export: "lakehouse",
  serving_transfer: "transfer",
};

// The advisor's pattern taxonomy id → PO-facing mode.
export const PATTERN_TO_MODE: Record<string, ServingMode> = {
  native_virtual: "virtual",
  native_materialized: "materialized",
  lakehouse_file: "lakehouse",
  transfer_then_transform: "transfer",
};

// Reverse of PATTERN_TO_MODE — mode → its pattern id (used to look a mode up in
// the advisor's patterns[] feasibility list).
export const MODE_TO_PATTERN: Record<ServingMode, string> = {
  virtual: "native_virtual",
  materialized: "native_materialized",
  lakehouse: "lakehouse_file",
  transfer: "transfer_then_transform",
};

// Per-mode accent colors for pills/banners so the surfaces read consistently.
export const MODE_ACCENT: Record<ServingMode, { fg: string; bg: string; border: string }> = {
  virtual: { fg: "#0369a1", bg: "#e0f2fe", border: "#bfdbfe" },
  materialized: { fg: "#7c3aed", bg: "#f3e8ff", border: "#e9d5ff" },
  lakehouse: { fg: "#0f766e", bg: "#ccfbf1", border: "#99f6e4" },
  transfer: { fg: "#b45309", bg: "#fef3c7", border: "#fde68a" },
};
