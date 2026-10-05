// Shared data model + inventory→graph adapter for the Estate Graph.
// The "Lineage Merge" design read a synthetic global `ESTATE`; here we derive
// the same shape (nodes, edges, systems, types, domains) from the real
// modernization inventory response so the graph runs on live estate data.
import type { EstateObject, InventoryResponse } from "../../types";
import { isDataBearing, isCodeObject } from "../../types";

export type Tier = "glyph" | "chip" | "card";
export type ChangeKind = "retire" | "add" | null;

export interface EGNode {
  id: string;
  name: string;
  type: string; // EstateNodeType (table|view|snapshot|query|report|database|product)
  system: string; // platform key (drives colour)
  domain: string;
  domainLabel: string;
  status: string; // fresh | warn | stale
  disposition: string | null; // migrate | modernize | remain | retire
  metric: string;
  panel: { rows: [string, string][] };
  core: boolean;
  isProduct: boolean;
  sample_columns?: { name: string; type: string; profile?: Record<string, number | boolean> }[];
  // Structural index signature so an EGNode satisfies the layout-engine
  // GraphNode contract (estateLayout.ts), which reads arbitrary keys
  // (n[opts.key]) and therefore requires `[k: string]: unknown`.
  [k: string]: unknown;
}

export interface EGEdge {
  source: string;
  target: string;
  isProductEdge?: boolean;
}

export interface SystemDef { label: string; color: string; }
export interface TypeDef { label: string; shape: string; }
export interface DomainDef { key: string; label: string; }

export interface EstateModel {
  nodes: EGNode[];
  edges: EGEdge[];
  systems: Record<string, SystemDef>;
  types: Record<string, TypeDef>;
  domains: DomainDef[];
}

// ── glyph geometry registry: shape name → SVG path, drawn in a -12..12 viewBox
// (~r10). SINGLE SOURCE OF TRUTH — EstateNode's <Glyph> renders straight from
// this. To add a new symbol: add ONE entry here, then either point a TYPE_DEFS
// entry at its name (for a curated type) or add its name to SHAPE_POOL below
// (so newly-discovered node types automatically draw it). "circle" is special-
// cased by the renderer (drawn as <circle>, not a path).
export const GLYPH_PATHS: Record<string, string> = {
  square: "M -8 -8 h 16 v 16 h -16 Z",
  triangle: "M 0 -9.5 L 9.5 8 L -9.5 8 Z",
  diamond: "M 0 -10.5 L 10.5 0 L 0 10.5 L -10.5 0 Z",
  star: "M 0 -11 L 3.1 -3.6 L 11 -3.1 L 4.9 2.1 L 6.9 10 L 0 5.8 L -6.9 10 L -4.9 2.1 L -11 -3.1 L -3.1 -3.6 Z",
  pentagon: "M 0 -10.5 L 10 -3.2 L 6.2 8.5 L -6.2 8.5 L -10 -3.2 Z",
  hexagon: "M 0 -10.5 L 9.1 -5.2 L 9.1 5.2 L 0 10.5 L -9.1 5.2 L -9.1 -5.2 Z",
  circle: "circle",
  // Extras — the pool that newly-discovered (non-curated) types draw from.
  octagon: "M -4.35 -10.5 L 4.35 -10.5 L 10.5 -4.35 L 10.5 4.35 L 4.35 10.5 L -4.35 10.5 L -10.5 4.35 L -10.5 -4.35 Z",
  plus: "M -3.5 -10 L 3.5 -10 L 3.5 -3.5 L 10 -3.5 L 10 3.5 L 3.5 3.5 L 3.5 10 L -3.5 10 L -3.5 3.5 L -10 3.5 L -10 -3.5 L -3.5 -3.5 Z",
  chevron: "M 0 -10 L 10 0 L 5 0 L 5 10 L -5 10 L -5 0 L -10 0 Z",
};

// Shapes offered to node types NOT in TYPE_DEFS, assigned in first-seen order
// (cycled if exhausted). Deliberately distinct from every curated shape above so
// a newly-discovered type never masquerades as a table/view/etc. Grow this list
// (and GLYPH_PATHS) to give more simultaneous new types their own symbol.
const SHAPE_POOL = ["octagon", "plus", "chevron"];

// "audit_log" → "Audit Log": friendly legend label for an auto-discovered type.
function humanizeType(t: string): string {
  return t.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()) || t;
}

// Curated glyph + label per KNOWN estate node type (the shape channel from the
// overview). Any type not listed here is auto-assigned a SHAPE_POOL symbol +
// humanized label at model-build time — see the `types` derivation below.
export const TYPE_DEFS: Record<string, TypeDef> = {
  table: { label: "Table", shape: "square" },
  view: { label: "View", shape: "triangle" },
  snapshot: { label: "Snapshot", shape: "diamond" },
  query: { label: "Job / Query", shape: "star" },
  report: { label: "Report", shape: "circle" },
  database: { label: "Database", shape: "hexagon" },
  product: { label: "Data Product", shape: "pentagon" },
};

// Deterministic platform palette (oklch, from the design's system colours),
// assigned by first-seen order with known aliases pinned.
const KNOWN_PLATFORM_COLORS: Record<string, string> = {
  teradata: "oklch(0.70 0.16 62)",
  hive: "oklch(0.58 0.10 210)",
  sas: "oklch(0.58 0.21 5)",
  powerbi: "oklch(0.62 0.17 148)",
  "power bi": "oklch(0.62 0.17 148)",
  informatica: "oklch(0.55 0.19 264)",
  flatfile: "oklch(0.58 0.19 300)",
  "flat file": "oklch(0.58 0.19 300)",
  databricks: "oklch(0.62 0.17 30)",
};
// Fallback palette for platforms NOT in the known map. Hues are deliberately
// offset from every known-platform hue (62/210/5/148/264/300/30) so an
// imported estate's first unknown platform never masquerades as Teradata etc.
const PALETTE = [
  "oklch(0.60 0.14 180)", "oklch(0.58 0.16 100)", "oklch(0.65 0.15 340)",
  "oklch(0.55 0.14 240)", "oklch(0.66 0.15 80)", "oklch(0.56 0.17 320)",
  "oklch(0.62 0.13 130)", "oklch(0.60 0.17 15)", "oklch(0.57 0.12 200)",
  "oklch(0.68 0.13 45)", "oklch(0.54 0.15 282)", "oklch(0.63 0.12 165)",
];

function platformKey(o: EstateObject): string {
  return o.platform || (isCodeObject(o) ? o.legacy_platform : undefined) || o.instance || "Unknown";
}

// Canonical display labels for the known platforms (their color-map keys are
// lowercase). Real imports are sloppy about casing — "teradata" and "Teradata"
// must be ONE system, not two legend entries.
const KNOWN_PLATFORM_LABELS: Record<string, string> = {
  teradata: "Teradata", hive: "Hive", sas: "SAS", powerbi: "PowerBI",
  "power bi": "Power BI", informatica: "Informatica",
  flatfile: "Flat File", "flat file": "Flat File", databricks: "Databricks",
};

/** Case-insensitive canonicalizer: known platforms get their proper label;
 * unknown ones collapse onto the first-seen casing. */
function makePlatformCanon(all: EstateObject[]): (raw: string) => string {
  const canon = new Map<string, string>();
  all.forEach((o) => {
    const raw = platformKey(o);
    const lower = raw.toLowerCase();
    if (!canon.has(lower)) canon.set(lower, KNOWN_PLATFORM_LABELS[lower] || raw);
  });
  return (raw: string) => canon.get(raw.toLowerCase()) || raw;
}

// Build the node/edge/system/type/domain model from the inventory response.
export function inventoryToEstate(inv: InventoryResponse): EstateModel {
  const objects = inv.objects || [];
  const productNodes = inv.product_nodes || [];
  const all: EstateObject[] = [...objects, ...productNodes];

  const seenProductIds = new Set(productNodes.map((p) => p.id));
  const canonPlatform = makePlatformCanon(all);
  const nodes: EGNode[] = all.map((o) => ({
    id: o.id,
    name: o.name,
    type: o.type,
    system: canonPlatform(platformKey(o)),
    domain: o.domain || "core",
    domainLabel: o.domain || "Core Warehouse",
    status: o.status,
    disposition: o.disposition ?? null,
    metric: o.metric,
    panel: { rows: o.panel_rows || [] },
    core: (o.scope ?? "core") === "core",
    isProduct: o.type === "product" || seenProductIds.has(o.id),
    sample_columns: isDataBearing(o) ? o.sample_columns : undefined,
  }));

  const nodeIds = new Set(nodes.map((n) => n.id));
  const edges: EGEdge[] = (inv.edges || [])
    .filter(([a, b]) => nodeIds.has(a) && nodeIds.has(b))
    .map(([a, b]) => ({ source: a, target: b, isProductEdge: seenProductIds.has(b) || seenProductIds.has(a) }));

  // Synthesized database nodes — one per platform, grouping its source tables —
  // mirroring the disposition DAG (LineageExplorer). These are decoration, not a
  // real lineage step; each gets a `database → table` contains-edge. Without
  // them the platform "hubs" the DAG shows would be missing from this view.
  const tablesByPlatform = new Map<string, EGNode[]>();
  nodes.forEach((n) => {
    if (n.type === "table") tablesByPlatform.set(n.system, [...(tablesByPlatform.get(n.system) || []), n]);
  });
  tablesByPlatform.forEach((tables, platform) => {
    const id = `db:${platform}`;
    nodes.push({
      id, name: platform, type: "database", system: platform,
      domain: "infrastructure", domainLabel: "Infrastructure",
      status: "fresh", disposition: null,
      metric: `${tables.length} source table${tables.length === 1 ? "" : "s"}`,
      panel: { rows: [["Platform", platform], ["Source tables", String(tables.length)]] },
      core: true, isProduct: false,
    });
    tables.forEach((t) => edges.push({ source: id, target: t.id }));
  });

  // systems: colour every platform present. Unknown platforms are assigned
  // from PALETTE in SORTED-name order (not response order) so colours are
  // stable across re-imports / row reordering.
  const systems: Record<string, SystemDef> = {};
  const unknownKeys = Array.from(new Set(nodes.map((n) => n.system)))
    .filter((s) => !KNOWN_PLATFORM_COLORS[s.toLowerCase()])
    .sort((a, b) => a.localeCompare(b));
  const unknownColor = new Map(unknownKeys.map((s, i) => [s, PALETTE[i % PALETTE.length]]));
  nodes.forEach((n) => {
    if (systems[n.system]) return;
    const known = KNOWN_PLATFORM_COLORS[n.system.toLowerCase()];
    systems[n.system] = { label: n.system, color: known || unknownColor.get(n.system) || PALETTE[0] };
  });

  // types: curated types first (in TYPE_DEFS order, only those present), then
  // any newly-discovered types in first-seen order — each auto-assigned a
  // distinct SHAPE_POOL symbol + humanized label so it's legible in the legend
  // and on the graph instead of collapsing into a nameless square.
  const types: Record<string, TypeDef> = {};
  Object.keys(TYPE_DEFS).forEach((k) => {
    if (nodes.some((n) => n.type === k)) types[k] = TYPE_DEFS[k];
  });
  let poolIdx = 0;
  nodes.forEach((n) => {
    if (types[n.type]) return;
    types[n.type] = { label: humanizeType(n.type), shape: SHAPE_POOL[poolIdx % SHAPE_POOL.length] };
    poolIdx++;
  });

  // domains: unique, with a friendly label
  const domSeen = new Set<string>();
  const domains: DomainDef[] = [];
  nodes.forEach((n) => {
    if (domSeen.has(n.domain)) return;
    domSeen.add(n.domain);
    domains.push({ key: n.domain, label: n.domainLabel });
  });

  return { nodes, edges, systems, types, domains };
}
