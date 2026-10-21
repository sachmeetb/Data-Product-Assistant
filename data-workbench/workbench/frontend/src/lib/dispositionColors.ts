/**
 * Single source of truth for the colors the disposition surfaces share.
 *
 * The app styles with inline CSS-in-JS (no UI lib, no global token layer — see
 * CLAUDE.md), so these are plain TS constants rather than CSS custom properties.
 * That keeps the house style intact while giving the seven Discovery /
 * discovery surfaces ONE place to change a disposition color instead of
 * re-declaring the same hex in each file.
 *
 * Deliberately NOT reused here:
 * - `--eg-*` in `components/estate-graph/estate-graph.css` — those are declared
 *   on `.estate-graph`, so they only resolve inside that subtree, and they're
 *   `oklch()` rather than hex. The graph keeps its own ported token set.
 * - `theme.ts`'s body-level `--accent` / `--badge-*` — those track the ACTIVE
 *   SHELL (violet in Product, blue in Engineer). Disposition colors are
 *   semantic, not shell-derived: migrate is blue and modernize is violet on the
 *   same page, so they must not collapse into one shell accent.
 */

export type DispositionKey = "migrate" | "modernize" | "retire" | "remain";

export interface Tone {
  /** Primary text / icon / solid-button background. */
  fg: string;
  /** Darker text, for use on top of `bg`. */
  deep: string;
  /** Faintest tint — the panel's own background. */
  wash: string;
  /** Tinted callout / banner background (a step stronger than `wash`). */
  bg: string;
  /** Stronger tint for pills / chips (reads at small sizes). */
  pill: string;
  /** Border on a tinted surface. */
  border: string;
}

/** The four estate dispositions. `remain` is intentionally neutral, not green —
 *  green means "newly added" in this UI (cf. `--eg-c-add`), not "left alone". */
export const DISPOSITION: Record<DispositionKey, Tone> = {
  migrate: { fg: "#1d4ed8", deep: "#1e40af", wash: "#f8fbff", bg: "#eff6ff", pill: "#dbeafe", border: "#bfdbfe" },
  modernize: { fg: "#6d28d9", deep: "#5b21b6", wash: "#faf5ff", bg: "#ede9fe", pill: "#ede9fe", border: "#ddd6fe" },
  retire: { fg: "#b91c1c", deep: "#991b1b", wash: "#fffafa", bg: "#fee2e2", pill: "#fee2e2", border: "#fecaca" },
  remain: { fg: "#64748b", deep: "#475569", wash: "#fafcfa", bg: "#f8fafc", pill: "#f1f5f9", border: "#e2e8f0" },
};

/** Non-disposition semantic tones used across the same panels. */
export const TONE = {
  /** Amber — a caution that does not block (missing sample, unresolved gap). */
  caution: { fg: "#b45309", deep: "#92400e", wash: "#fffdf7", bg: "#fffbeb", pill: "#fef3c7", border: "#fde68a" } as Tone,
  /** Green — a NEW thing being added (proposed product, migrated target node). */
  success: { fg: "#047857", deep: "#166534", wash: "#fafcfa", bg: "#f0fdf4", pill: "#dcfce7", border: "#bbf7d0" } as Tone,
};

/**
 * SimilarityRadar's two states. Named by MEANING (above / below the match
 * threshold) rather than by color, so a re-tint doesn't leave the constant lying.
 *
 * NOTE: `MATCH.stroke` is a softer violet than `DISPOSITION.modernize.fg` —
 * chosen for a large translucent chart fill rather than text. It is the one
 * remaining violet in the app that isn't in the modernize family.
 */
export const MATCH = { stroke: "#7c73d6", fill: "rgba(124, 115, 214, 0.42)" };
export const NO_MATCH = { stroke: "#94a3b8", fill: "rgba(148, 163, 184, 0.35)" };

/** Tone for an object's disposition, falling back to neutral for unknowns. */
export function toneFor(disposition: string | null | undefined): Tone {
  return DISPOSITION[disposition as DispositionKey] ?? DISPOSITION.remain;
}
