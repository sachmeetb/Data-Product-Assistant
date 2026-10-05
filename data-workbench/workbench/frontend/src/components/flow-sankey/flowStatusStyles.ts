/**
 * Status-dot palette for the value-flow view.
 *
 * The estate context uses the feasibility stoplight (mirrors `FeasibilityGrid`'s
 * unexported `TIER` — kept in sync by hand); the marketplace context uses
 * lifecycle states. `statusStyleFor` picks by context and returns null for an
 * unknown status (the card then renders a neutral dot). Status is encoded by
 * BOTH dot color and a text label so it reads without color (a11y).
 */

import type { FlowContext } from "./flowSankeyTypes";

export interface StatusStyle {
  dot: string;
  label: string;
}

/** Feasibility stoplight — estate context only (harmless if unused elsewhere). */
export const FEASIBILITY_STATUS: Record<string, StatusStyle> = {
  ready: { dot: "#10b981", label: "Ready" },
  adaptable: { dot: "#38bdf8", label: "Adaptable" },
  assemblable: { dot: "#f59e0b", label: "Assemblable" },
  absent: { dot: "#94a3b8", label: "Absent" },
};

/** Marketplace product lifecycle. */
export const LIFECYCLE_STATUS: Record<string, StatusStyle> = {
  published: { dot: "#16a34a", label: "Published" },
  superseded: { dot: "#f59e0b", label: "Superseded" },
  approved: { dot: "#3b82f6", label: "Approved" },
  draft: { dot: "#94a3b8", label: "Draft" },
};

export function statusStyleFor(
  context: FlowContext,
  status: string | undefined,
): StatusStyle | null {
  if (!status) return null;
  const table = context === "estate" ? FEASIBILITY_STATUS : LIFECYCLE_STATUS;
  return table[status] || null;
}
