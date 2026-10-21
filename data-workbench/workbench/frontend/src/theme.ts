/**
 * Theme tokens for the two-workbench shell split.
 *
 * The Product Workbench and Engineering Workbench share layout and typography
 * but diverge on accent color and background tint so users always know which
 * surface they're on. Tokens are exposed both as JS constants (for inline
 * styles in existing components) and as CSS custom properties on <body>
 * (so new components can opt in via var(--accent) without prop drilling).
 */

import { useEffect, useMemo } from "react";
import { useLocation } from "react-router-dom";

export type WorkbenchKind = "product" | "engineer";

export interface Theme {
  kind: WorkbenchKind;
  label: string;
  accent: string;
  accentSoft: string;
  bgTint: string;
  badgeBg: string;
  badgeFg: string;
  headerBg: string;
  headerFg: string;
  headerMuted: string;
}

export const engineerTheme: Theme = {
  kind: "engineer",
  label: "Engineering Workbench",
  accent: "#3b82f6",
  accentSoft: "#dbeafe",
  bgTint: "#f8fafc",
  badgeBg: "#eff6ff",
  badgeFg: "#1d4ed8",
  headerBg: "#1e293b",
  headerFg: "#ffffff",
  headerMuted: "#94a3b8",
};

export const productTheme: Theme = {
  kind: "product",
  label: "Product Workbench",
  accent: "#7c3aed",
  accentSoft: "#ede9fe",
  bgTint: "#faf9fb",
  badgeBg: "#f5f3ff",
  badgeFg: "#5b21b6",
  headerBg: "#2e1065",
  headerFg: "#ffffff",
  headerMuted: "#c4b5fd",
};

export function themeForPath(pathname: string): Theme {
  if (pathname.startsWith("/product")) return productTheme;
  return engineerTheme;
}

/**
 * Returns the active theme for the current route and writes the theme's
 * tokens as CSS custom properties on <body>. Descendants can read them
 * via `var(--accent)` etc. to pick up the correct tint without re-rendering
 * through React context.
 */
export function useTheme(): Theme {
  const { pathname } = useLocation();
  const theme = useMemo(() => themeForPath(pathname), [pathname]);

  useEffect(() => {
    const body = document.body;
    body.style.setProperty("--accent", theme.accent);
    body.style.setProperty("--accent-soft", theme.accentSoft);
    body.style.setProperty("--bg-tint", theme.bgTint);
    body.style.setProperty("--badge-bg", theme.badgeBg);
    body.style.setProperty("--badge-fg", theme.badgeFg);
    body.style.setProperty("--header-bg", theme.headerBg);
    body.style.setProperty("--header-fg", theme.headerFg);
    body.style.setProperty("--header-muted", theme.headerMuted);
    body.dataset.workbench = theme.kind;

    // Browser tab title + favicon vary by shell so the user always knows
    // which workbench a tab is on, especially helpful for demos with both
    // open at once.
    document.title = theme.kind === "product"
      ? "Data Product Workbench"
      : "Data Engineering Workbench";

    const letter = theme.kind === "product" ? "P" : "E";
    const accent = theme.accent;
    const accentDark = theme.kind === "product" ? "#5b21b6" : "#1d4ed8";
    const svg = `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>`
      + `<rect width='64' height='64' rx='14' fill='${accent}'/>`
      + `<rect x='4' y='4' width='56' height='56' rx='10' fill='${accentDark}' fill-opacity='0.25'/>`
      + `<text x='32' y='44' text-anchor='middle' font-family='system-ui, -apple-system, Segoe UI, sans-serif' `
      + `font-size='38' font-weight='700' fill='#ffffff'>${letter}</text></svg>`;
    const dataUrl = `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;

    let link = document.querySelector("link[rel~='icon']") as HTMLLinkElement | null;
    if (!link) {
      link = document.createElement("link");
      link.rel = "icon";
      document.head.appendChild(link);
    }
    link.type = "image/svg+xml";
    link.href = dataUrl;
  }, [theme]);

  return theme;
}
