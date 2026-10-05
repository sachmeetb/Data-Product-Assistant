/**
 * Visual badge for a data product's kind. Backend persists
 * ``:DataContract.productKind`` — now THREE-valued ('source' | 'aggregate' |
 * 'consumer') — and emits it on every marketplace / dashboard endpoint.
 *
 * The data-mesh trichotomy:
 *   - source    (blue)   — mirrors an operational source close to 1:1.
 *   - aggregate (purple) — a reusable building block composed from other
 *                          products, meant to be consumed further.
 *   - consumer  (orange) — a fit-for-purpose leaf serving a specific need.
 *
 * This component is the single label authority — fix the palette/labels here
 * and every surface (marketplace, My Products, wizard picker) updates.
 */

import { KIND_STYLES } from "./productKindStyles";

interface Props {
  kind: string | null | undefined;
  /** When true, render in a smaller compact form for inline use in lists. */
  compact?: boolean;
}

export default function ProductKindChip({ kind, compact = false }: Props) {
  const k = (kind || "").toLowerCase();
  const style = KIND_STYLES[k];
  if (!style) return null;
  const padding = compact ? "1px 6px" : "2px 8px";
  const fontSize = compact ? 10 : 11;
  return (
    <span
      style={{
        padding,
        borderRadius: 999,
        fontSize,
        fontWeight: 600,
        backgroundColor: style.bg,
        color: style.fg,
        border: `1px solid ${style.border}`,
        whiteSpace: "nowrap",
      }}
      title={style.title}
    >
      {style.label}
    </span>
  );
}
