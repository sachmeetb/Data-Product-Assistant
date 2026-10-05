/**
 * Shared display metadata for a product's serving mode.
 *
 * The persisted `:ServingDefinition.servingMode` values are:
 *   virtual_view | dbt_materialized | lakehouse_local | transfer_then_transform
 * (`physical_copy` is a legacy stage-derived alias for the dbt path).
 *
 * One source of truth so every surface (engineer dashboard, marketplace Serving
 * tab, pipeline chips) labels the four modes consistently.
 */
export const SERVING_MODE_LABELS: Record<string, string> = {
  virtual_view: "Virtual View",
  dbt_materialized: "Materialized (dbt)",
  lakehouse_local: "Lakehouse (Parquet + DuckDB)",
  transfer_then_transform: "Cross-platform transfer",
  physical_copy: "Materialized (dbt)", // legacy alias
};

export const SERVING_MODE_COLOR: Record<string, string> = {
  virtual_view: "#059669",
  dbt_materialized: "#7c3aed",
  lakehouse_local: "#0e7490",
  transfer_then_transform: "#b91c1c",
  physical_copy: "#7c3aed",
};

export function servingModeLabel(mode?: string | null): string {
  if (!mode) return "—";
  return SERVING_MODE_LABELS[mode] ?? mode;
}

export function servingModeColor(mode?: string | null): string {
  if (!mode) return "#6366f1";
  return SERVING_MODE_COLOR[mode] ?? "#6366f1";
}
