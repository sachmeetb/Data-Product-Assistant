/**
 * The single source of truth for data-product-kind color + label
 * ('source' | 'aggregate' | 'consumer' — the data-mesh trichotomy).
 *
 * Lives in its own module (not ProductKindChip.tsx) so both the chip component
 * and non-component surfaces — e.g. the product-lineage graph nodes — import
 * the exact same palette without tripping react-refresh's
 * "only-export-components" rule. Fix the palette/labels here and every surface
 * updates.
 */

export interface KindStyle {
  bg: string;
  fg: string;
  border: string;
  label: string;
  title: string;
}

export const KIND_STYLES: Record<string, KindStyle> = {
  source: {
    bg: "#dbeafe",
    fg: "#1d4ed8",
    border: "#93c5fd",
    label: "Source",
    title: "Source-aligned: mirrors an operational source close to 1:1.",
  },
  aggregate: {
    bg: "#ede9fe",
    fg: "#6d28d9",
    border: "#c4b5fd",
    label: "Aggregate",
    title:
      "Aggregate: a reusable building block composed from other products, " +
      "meant to be consumed further downstream.",
  },
  consumer: {
    bg: "#fde4d4",
    fg: "#b86a1d",
    border: "#fdba74",
    label: "Consumer",
    title:
      "Consumer-aligned: a fit-for-purpose product composed from one or more " +
      "upstream products.",
  },
};
