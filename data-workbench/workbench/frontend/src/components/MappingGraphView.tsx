import { useEffect, useMemo, useRef, useState, useCallback, type CSSProperties } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  Handle,
  Position,
  MarkerType,
  type Node,
  type Edge,
  type NodeProps,
  type EdgeMouseHandler,
  type NodeMouseHandler,
  type ReactFlowInstance,
  type Connection,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import api from "../api/client";
import { productTheme } from "../theme";

/**
 * Mapping graph view: renders source tables (left) and the data product
 * (right), with one edge per :ColumnMapping colored by status.
 *
 * The engineer's data_mapping Reviews tab embeds this above the existing
 * TransformEditor. Click an edge → the parent receives the mapping_uri
 * via onMappingSelect and opens its editor inline below.
 *
 * Data is sourced from GET /api/projects/{id}/reviews/mappings/graph
 * which returns ALL current mappings regardless of status, so the canvas
 * shows the full picture (approved + pending + escalated + rejected) and
 * unmapped product columns appear as no-incoming-edge nodes.
 */

interface SourceColumn {
  uri: string;
  name: string;
  data_type: string | null;
  ordinal: number | null;
  /** True when this column is referenced only as a lookup source (a value/key
   *  read by a lookup-kind transform) rather than mapped 1:1. Drives a
   *  "lookup" badge so it isn't mistaken for a primary mapped source. */
  is_lookup?: boolean;
  /** 'value' | 'key' — the lookup role, when is_lookup. */
  role?: string | null;
}
interface SourceTable {
  uri: string;
  schema: string | null;
  table: string | null;
  columns: SourceColumn[];
  /** True when this whole table only appears as a lookup reference source
   *  (e.g. a different CONSUMES'd product the lookup reads from). */
  is_lookup?: boolean;
}
interface ProductColumn {
  uri: string;
  name: string;
  ordinal: number | null;
  data_type: string | null;
  primary_key: boolean;
  // The :DProdOutputDataset.physicalName this column belongs to. Drives
  // canvas grouping so SA products with multiple datasets render as N
  // boxes on the right side instead of one collapsed product node.
  // Optional for back-compat with payloads that pre-date the field.
  dataset_name?: string | null;
}
interface MappingEdge {
  uri: string;
  /** For lookup_via edges, `uri` is synthetic; this carries the real
   *  :ColumnMapping uri so clicks open the right editor. */
  mapping_uri?: string;
  status: string;
  /** Null for literal-kind mappings — they have no source column. */
  source_uri: string | null;
  target_uri: string;
  transform_kind: string | null;
  transform_author: string | null;
  transform_expression: string | null;
  /** JSON-encoded transform params (e.g. lookup table/column, mask format).
   *  Surfaced in hover tooltip so the reviewer can see *what* the transform
   *  does without leaving the graph. */
  transform_params_json?: string | null;
  transform_escalation_reason: string | null;
  similarity_score: number | null;
  /** Populated only when transform_kind === 'literal'; the engineer-quoted
   *  SQL literal (e.g. "'USD'", "42"). Surfaced as a chip on the target row. */
  literal_value: string | null;
  /** 'maps_from' (default, primary source edge) | 'lookup_via' (a lookup-kind
   *  mapping reading from a separate reference table). Lookup edges render
   *  dashed + secondary-colored so they read distinctly from primary mappings. */
  relation?: string | null;
  /** Lookup role ('value' | 'key'), when relation === 'lookup_via'. */
  role?: string | null;
  /** Lookup selection strategy (equi | latest | aggregate | exists). */
  strategy?: string | null;
}

// ── Optional multi-hop lineage tiers (marketplace opt-in only) ──────────────
// Present on the payload ONLY when the parent fetched them via the backend's
// ?include_upstream / ?include_downstream params. Absent for every engineer
// payload → all hop-expansion code paths stay inert.
interface UpstreamColumn {
  uri: string;
  name: string | null;
  data_type: string | null;
  ordinal: number | null;
}
interface UpstreamTable {
  uri: string;
  schema: string | null;
  table: string | null;
  /** The source-aligned product this raw table feeds (label only). */
  source_product_uri?: string | null;
  source_product_name?: string | null;
  /** True when this raw table feeds ONLY lookup references (badge it `lookup`). */
  is_lookup?: boolean;
  columns: UpstreamColumn[];
}
interface UpstreamEdge {
  /** Raw catalog column URI — the right-handle id on the tier −1 card. */
  source_uri: string;
  /** The existing source :DProdColumn URI already rendered on a tier-0 card;
   *  wires to that card's new left handle (`in:${uri}`). */
  target_uri: string;
  status: string | null;
  /** True when this upstream edge feeds a lookup reference (renders dashed
   *  violet, matching the tier-0 → product lookup edges). */
  is_lookup?: boolean;
}
interface DownstreamConsumer {
  uri: string;
  name: string | null;
  product_kind: string | null;
  contract_id: string | null;
  lifecycle_state: string | null;
  /** 'data_product' today; seam for future report/software-component kinds. */
  consumer_kind: string;
}
interface DownstreamEdge {
  source_product_uri: string;
  target_product_uri: string;
  kind: string;
}
export interface GraphPayload {
  source_tables: SourceTable[];
  product: { uri: string | null; name: string | null; columns: ProductColumn[] };
  mappings: MappingEdge[];
  /** Raw catalog one hop past the source cards (tier −1). */
  upstream?: { tables: UpstreamTable[]; edges: UpstreamEdge[] };
  /** Products that consume this one (tier +2). */
  downstream?: { consumers: DownstreamConsumer[]; edges: DownstreamEdge[] };
}

interface Props {
  /** Engineer-side: fetch from /api/projects/{id}/reviews/mappings/graph. */
  projectId?: number;
  /** Marketplace / read-only: parent has already fetched the payload (from
   *  e.g. /api/marketplace/mapping-graph) and passes it in directly. When
   *  provided, projectId is ignored and no fetching happens. */
  payload?: GraphPayload | null;
  /** Disable interactive callbacks (edge clicks, unmapped-column clicks).
   *  Used by the marketplace lineage view where viewers can't edit. */
  readOnly?: boolean;
  /** Mapping URI to highlight (e.g., from list-view selection or external deep-link). */
  selectedMappingUri?: string | null;
  /** Product column URI to highlight as the focused unmapped column. */
  focusUnmappedColumnUri?: string | null;
  onMappingSelect?: (mappingUri: string) => void;
  onUnmappedColumnSelect?: (productColumnUri: string) => void;
  /** Engineer authoring: drag a source-column handle onto a product-column
   *  handle to wire it. Fires (sourceColumnUri, productColumnUri). Enables
   *  onConnect + node connectability — only when set AND not readOnly, so the
   *  marketplace lineage canvas stays non-interactive. */
  onWireSource?: (sourceColumnUri: string, productColumnUri: string) => void;
  /** Engineer authoring: double-click an edge to edit its mapping in a pop-up.
   *  Fires the mapping URI. Gated by readOnly (the marketplace canvas is inert). */
  onEditMapping?: (mappingUri: string) => void;
  /** Marketplace-only: render the "Show upstream sources" / "Show downstream
   *  consumers" toggles and lazily fetch the extra tiers. Only MarketplacePage
   *  sets this — engineer embedders leave it false so nothing new renders and
   *  no extra network calls fire. */
  enableHopExpansion?: boolean;
  /** Product URI, needed to re-fetch the opt-in tiers. */
  productUri?: string;
  /** 'source' | 'consumer' — gates the upstream toggle (source-aligned
   *  products have no upstream hop). */
  productKind?: string;
}

/** Edge color by mapping status. */
const STATUS_STYLE: Record<string, { stroke: string; label: string }> = {
  approved: { stroke: "#16a34a", label: "Approved" },
  pending_review: { stroke: "#f59e0b", label: "Pending review" },
  steward_review: { stroke: "#7c3aed", label: "Steward review" },
  rejected: { stroke: "#dc2626", label: "Rejected" },
};

const TABLE_NODE_WIDTH = 280;
const COLUMN_ROW_HEIGHT = 26;
const TABLE_HEADER_HEIGHT = 38;
const TABLE_GAP = 24;

// Horizontal pitch between tiers. Equals today's PRODUCT_X − SOURCE_X
// (TABLE_NODE_WIDTH + 360 = 640), so the default 2-tier layout stays pixel-
// identical: tier 0 at x=40, tier 1 at x=680.
const COLUMN_PITCH = TABLE_NODE_WIDTH + 360;
const TIER_X0 = 40;
const SUMMARY_NODE_WIDTH = 210;
const SUMMARY_NODE_HEIGHT = 66;
// Reserved handle ids for the compact downstream tier (product card right
// edge → summary node left edge). Distinct from any column URI.
const DOWNSTREAM_OUT_HANDLE = "__downstream_out__";
const SUMMARY_IN_HANDLE = "__in__";

const STORAGE_KEY_HIDE_UNMAPPED = "mapping_graph.hideUnmappedSources";
const STORAGE_KEY_SHOW_UPSTREAM = "mapping_graph.showUpstream";
const STORAGE_KEY_SHOW_DOWNSTREAM = "mapping_graph.showDownstream";

/** A vertical stack of column "handles" inside a table card. */
function TableNodeView({ data }: NodeProps<Node<{
  title: string;
  subtitle?: string;
  side: "source" | "product";
  /** True when this source table only appears as a lookup reference. */
  isLookup?: boolean;
  columns: Array<{
    uri: string;
    name: string;
    type: string | null;
    primaryKey?: boolean;
    unmapped?: boolean;
    /** True when the column is a lookup-only source (badge it). */
    isLookup?: boolean;
    /** Engineer-quoted SQL literal for literal-kind product columns. When
     *  set, the row renders a "= 'USD'" chip instead of the UNMAPPED badge. */
    literalValue?: string | null;
    /** Source-side only: render an extra LEFT target handle (`in:${uri}`) so
     *  an upstream (tier −1) edge can wire into this rendered source column.
     *  Additive — the existing right-side source handle keyed on the bare URI
     *  is untouched. */
    isUpstreamTarget?: boolean;
  }>;
  highlightColumnUri?: string | null;
  /** Product-card only: render a node-level RIGHT source handle so downstream
   *  (tier +2) consumer edges can anchor off the product. */
  hasDownstreamOut?: boolean;
}>>) {
  const handleSide = data.side === "source" ? Position.Right : Position.Left;
  const accent = data.side === "product" ? productTheme.accent : "#0369a1";
  return (
    <div
      style={{
        width: TABLE_NODE_WIDTH,
        borderRadius: 8,
        border: `1px solid ${accent}`,
        backgroundColor: "#fff",
        boxShadow: "0 2px 6px rgba(15,23,42,0.08)",
        overflow: "hidden",
        fontFamily: "system-ui, sans-serif",
      }}
    >
      <div
        style={{
          height: TABLE_HEADER_HEIGHT,
          padding: "6px 10px",
          backgroundColor: data.side === "product" ? productTheme.accentSoft : "#e0f2fe",
          color: accent,
          fontWeight: 700,
          fontSize: 12,
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          borderBottom: `1px solid ${accent}`,
        }}
      >
        <div style={{ textTransform: "uppercase", letterSpacing: 0.4, fontSize: 9 }}>
          {data.side === "product" ? "Data Product" : "Source"}
          {data.isLookup && (
            <span
              style={{
                marginLeft: 6,
                color: "#9333ea",
                backgroundColor: "#f3e8ff",
                padding: "0 4px",
                borderRadius: 3,
                fontWeight: 700,
              }}
            >
              lookup
            </span>
          )}
        </div>
        <div>
          {data.title}
          {data.subtitle && (
            <span style={{ fontWeight: 400, fontSize: 11, marginLeft: 6, color: "#64748b" }}>
              {data.subtitle}
            </span>
          )}
        </div>
      </div>
      <div style={{ display: "flex", flexDirection: "column" }}>
        {data.columns.map((c, i) => {
          const isHighlighted = data.highlightColumnUri && data.highlightColumnUri === c.uri;
          return (
            <div
              key={c.uri}
              data-column-uri={c.uri}
              style={{
                position: "relative",
                height: COLUMN_ROW_HEIGHT,
                padding: "0 10px",
                display: "grid",
                gridTemplateColumns: "1fr auto",
                gap: 6,
                alignItems: "center",
                fontSize: 11,
                borderTop: i === 0 ? "none" : "1px solid #f1f5f9",
                backgroundColor: isHighlighted
                  ? "#fef3c7"
                  : c.unmapped
                  ? "#fff7ed"
                  : "#fff",
                color: "#0f172a",
              }}
            >
              <Handle
                type={data.side === "source" ? "source" : "target"}
                position={handleSide}
                id={c.uri}
                style={{
                  background: c.unmapped ? "#fb923c" : accent,
                  width: 8,
                  height: 8,
                  border: "1px solid #fff",
                }}
              />
              {data.side === "source" && c.isUpstreamTarget && (
                <Handle
                  type="target"
                  position={Position.Left}
                  id={`in:${c.uri}`}
                  style={{
                    background: "#6366f1",
                    width: 8,
                    height: 8,
                    border: "1px solid #fff",
                  }}
                />
              )}
              <span style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                {c.name}
                {c.primaryKey && (
                  <span style={{ marginLeft: 6, fontSize: 9, color: "#7c3aed", fontWeight: 700 }}>PK</span>
                )}
                {c.isLookup && (
                  <span
                    style={{
                      marginLeft: 6,
                      fontSize: 9,
                      color: "#9333ea",
                      backgroundColor: "#f3e8ff",
                      fontWeight: 700,
                      padding: "1px 5px",
                      borderRadius: 3,
                    }}
                  >
                    lookup
                  </span>
                )}
                {c.literalValue ? (
                  <span
                    style={{
                      marginLeft: 6,
                      fontSize: 9,
                      color: productTheme.accent,
                      backgroundColor: productTheme.accentSoft,
                      fontWeight: 700,
                      padding: "1px 5px",
                      borderRadius: 3,
                      fontFamily: "ui-monospace, Menlo, monospace",
                    }}
                  >
                    = {c.literalValue}
                  </span>
                ) : c.unmapped && data.side === "product" ? (
                  <span style={{ marginLeft: 6, fontSize: 9, color: "#9a3412", fontWeight: 700 }}>
                    UNMAPPED
                  </span>
                ) : null}
              </span>
              <code style={{ fontSize: 10, color: "#94a3b8", whiteSpace: "nowrap" }}>{c.type || ""}</code>
            </div>
          );
        })}
      </div>
      {data.hasDownstreamOut && (
        <Handle
          type="source"
          position={Position.Right}
          id={DOWNSTREAM_OUT_HANDLE}
          style={{ background: "#6366f1", width: 9, height: 9, border: "1px solid #fff" }}
        />
      )}
    </div>
  );
}

/** Compact one-box node for a downstream consumer (tier +2). Column-level
 *  downstream lineage doesn't exist in the graph yet, so a summary node is the
 *  honest representation — one box, one aggregate "consumes" edge. Carries a
 *  consumer_kind badge as the seam for future report / software-component
 *  kinds. Left `__in__` handle receives the aggregate edge; the right handle is
 *  reserved (unused today) for a future further hop. */
function SummaryNodeView({ data }: NodeProps<Node<{
  title: string;
  /** 'source' | 'consumer' — the consuming product's own kind. */
  productKind?: string | null;
  /** Node kind: 'data_product' today; seam for future report/component. */
  consumerKind?: string;
  lifecycle?: string | null;
}>>) {
  const indigo = "#6366f1";
  return (
    <div
      style={{
        width: SUMMARY_NODE_WIDTH,
        minHeight: SUMMARY_NODE_HEIGHT,
        borderRadius: 8,
        border: `1px solid ${indigo}`,
        backgroundColor: "#fff",
        boxShadow: "0 2px 6px rgba(15,23,42,0.08)",
        overflow: "hidden",
        fontFamily: "system-ui, sans-serif",
        position: "relative",
      }}
    >
      <Handle
        type="target"
        position={Position.Left}
        id={SUMMARY_IN_HANDLE}
        style={{ background: indigo, width: 9, height: 9, border: "1px solid #fff" }}
      />
      <div
        style={{
          padding: "6px 10px",
          backgroundColor: "#eef2ff",
          color: indigo,
          borderBottom: `1px solid ${indigo}`,
        }}
      >
        <div style={{ textTransform: "uppercase", letterSpacing: 0.4, fontSize: 9, fontWeight: 700 }}>
          Consumer
          <span
            style={{
              marginLeft: 6,
              color: "#4338ca",
              backgroundColor: "#e0e7ff",
              padding: "0 4px",
              borderRadius: 3,
              fontWeight: 700,
              textTransform: "none",
            }}
          >
            {data.consumerKind === "data_product" ? "data product" : (data.consumerKind || "product")}
          </span>
        </div>
        <div style={{ fontWeight: 700, fontSize: 12, marginTop: 2, color: "#1e293b" }}>{data.title}</div>
      </div>
      <div style={{ padding: "6px 10px", fontSize: 11, color: "#64748b" }}>
        {data.productKind ? `${data.productKind} product` : "consumes this product"}
        {data.lifecycle && (
          <span style={{ marginLeft: 6, fontSize: 9, fontWeight: 700, color: "#475569" }}>
            {data.lifecycle}
          </span>
        )}
      </div>
      {/* Reserved right handle for a future further downstream hop. */}
      <Handle
        type="source"
        position={Position.Right}
        id="__out__"
        style={{ background: indigo, width: 8, height: 8, border: "1px solid #fff", opacity: 0.35 }}
      />
    </div>
  );
}

const NODE_TYPES = { table: TableNodeView, summary: SummaryNodeView };

function buildLayout(
  payload: GraphPayload,
  highlightColumnUri: string | null,
  hideUnmappedSources: boolean,
): { nodes: Node[]; edges: Edge[] } {
  const product = payload.product;
  const mappedTargetUris = new Set(payload.mappings.map((m) => m.target_uri));
  const mappedSourceColumnUris = new Set(
    payload.mappings.map((m) => m.source_uri).filter((u): u is string => !!u),
  );
  // Literal-kind mappings have no source_uri and therefore no edge on the
  // canvas. Surface their value as a chip on the target product column row
  // so the viewer sees "this is a constant", not "this is unmapped".
  const literalsByTarget = new Map<string, string>();
  for (const m of payload.mappings) {
    if (m.transform_kind === "literal") {
      literalsByTarget.set(m.target_uri, m.literal_value ?? "(literal)");
    }
  }

  // ── Tier 0: source tables ──────────────────────────────────────────────
  // Decide which source tables/columns to render. Default (toggle off)
  // shows the full discovered schema with unmapped columns muted, so the
  // engineer can pre-visualise the canvas before data_mapping has run.
  // Toggle on restricts to tables that have ≥1 mapped column and within
  // them only the mapped columns — the legacy view. Sort is UNCHANGED so the
  // default view never reorders.
  const decorated = payload.source_tables.map((t) => ({
    tbl: t,
    tableHasMapped: t.columns.some((c) => mappedSourceColumnUris.has(c.uri)),
  }));
  const sourceTables: SourceTable[] = hideUnmappedSources
    ? decorated
        .filter((x) => x.tableHasMapped)
        .map((x) => ({
          ...x.tbl,
          columns: x.tbl.columns.filter((c) => mappedSourceColumnUris.has(c.uri)),
        }))
        .sort((a, b) => {
          const sa = a.schema || "";
          const sb = b.schema || "";
          if (sa !== sb) return sa.localeCompare(sb);
          return (a.table || "").localeCompare(b.table || "");
        })
    : decorated
        .sort((a, b) => {
          if (a.tableHasMapped !== b.tableHasMapped) return a.tableHasMapped ? -1 : 1;
          const sa = a.tbl.schema || "";
          const sb = b.tbl.schema || "";
          if (sa !== sb) return sa.localeCompare(sb);
          return (a.tbl.table || "").localeCompare(b.tbl.table || "");
        })
        .map((x) => x.tbl);

  // ── Tier −1: upstream raw catalog ──────────────────────────────────────
  // Prune to columns that actually feed a rendered source column (and, since
  // renderedSourceColUris already reflects hideUnmappedSources, this also
  // implements the hide-unmapped interaction: an upstream edge into a pruned
  // source column drops, and its raw column drops with it). Keeps tier −1
  // bounded to mapped lineage rather than whole raw schemas.
  const renderedSourceColUris = new Set<string>();
  for (const t of sourceTables) for (const c of t.columns) renderedSourceColUris.add(c.uri);
  const survivingUpstreamEdges = (payload.upstream?.edges ?? []).filter((e) =>
    renderedSourceColUris.has(e.target_uri),
  );
  const survivingUpColUris = new Set(survivingUpstreamEdges.map((e) => e.source_uri));
  const upstreamTargetUris = new Set(survivingUpstreamEdges.map((e) => e.target_uri));
  const upColToTableUri = new Map<string, string>();
  const upstreamTables: UpstreamTable[] = (payload.upstream?.tables ?? [])
    .map((t) => ({ ...t, columns: t.columns.filter((c) => survivingUpColUris.has(c.uri)) }))
    .filter((t) => t.columns.length > 0);
  for (const t of upstreamTables) for (const c of t.columns) upColToTableUri.set(c.uri, t.uri);

  // ── Tier 1: product groups ─────────────────────────────────────────────
  // One node per :DProdOutputDataset. SA products typically have multiple
  // datasets (employee/department/salary/etc.); collapsing them into a single
  // visual anchor misrepresents the structure. Group columns by dataset_name;
  // columns without one fall back to a single bucket keyed on the product name
  // (consumer products today emit one schema — the common non-SA case).
  const productGroups = new Map<string, { title: string; columns: ProductColumn[] }>();
  const fallbackKey = product.name || "Product";
  for (const c of product.columns) {
    const key = (c.dataset_name && c.dataset_name.trim()) || fallbackKey;
    if (!productGroups.has(key)) {
      productGroups.set(key, { title: key, columns: [] });
    }
    productGroups.get(key)!.columns.push(c);
  }
  const productGroupList = Array.from(productGroups.entries()).sort(([a], [b]) => a.localeCompare(b));
  // Multi-dataset (SA) products anchor the single downstream edge on the FIRST
  // product group (column-level downstream lineage removes this ambiguity
  // later).
  const firstGroupKey = productGroupList.length > 0 ? productGroupList[0][0] : fallbackKey;

  // ── Tier 2: downstream consumers ───────────────────────────────────────
  const consumers = payload.downstream?.consumers ?? [];

  // ── Tier geometry ──────────────────────────────────────────────────────
  const tableHeight = (nCols: number) => TABLE_HEADER_HEIGHT + nCols * COLUMN_ROW_HEIGHT;
  const stackOf = (heights: number[]) =>
    heights.length === 0 ? 0 : heights.reduce((a, b) => a + b, 0) + (heights.length - 1) * TABLE_GAP;

  type TierKey = "up" | "src" | "prod" | "down";
  const tierStack: Record<TierKey, number> = {
    up: stackOf(upstreamTables.map((t) => tableHeight(t.columns.length))),
    src: stackOf(sourceTables.map((t) => tableHeight(t.columns.length))),
    prod: stackOf(productGroupList.map(([, g]) => tableHeight(g.columns.length))),
    down: stackOf(consumers.map(() => SUMMARY_NODE_HEIGHT)),
  };
  const tierHasNodes: Record<TierKey, boolean> = {
    up: upstreamTables.length > 0,
    src: sourceTables.length > 0,
    prod: productGroupList.length > 0,
    down: consumers.length > 0,
  };
  // Present tiers left-to-right. X indexes into the present set, so the default
  // 2-tier layout keeps tier 0 at x=40 and tier 1 at x=680 (pixel-identical);
  // adding tier −1 shifts everything right by one pitch and fitView reframes.
  const presentTiers = (["up", "src", "prod", "down"] as TierKey[]).filter((k) => tierHasNodes[k]);
  const maxStack = presentTiers.length ? Math.max(...presentTiers.map((k) => tierStack[k])) : 0;
  const tierX: Record<TierKey, number> = { up: 0, src: 0, prod: 0, down: 0 };
  presentTiers.forEach((k, i) => { tierX[k] = TIER_X0 + i * COLUMN_PITCH; });
  // Per-tier vertical start centers each tier against the tallest — the
  // generalization of the old product-centering.
  const yStart = (k: TierKey) => (maxStack - tierStack[k]) / 2;

  const nodes: Node[] = [];

  // ── Place tier 0, recording per-column center Y (for barycenter ordering) ──
  const srcColY = new Map<string, number>();
  {
    let cursorY = yStart("src");
    for (const tbl of sourceTables) {
      tbl.columns.forEach((c, idx) => {
        srcColY.set(
          c.uri,
          cursorY + TABLE_HEADER_HEIGHT + idx * COLUMN_ROW_HEIGHT + COLUMN_ROW_HEIGHT / 2,
        );
      });
      nodes.push({
        id: `src:${tbl.uri}`,
        type: "table",
        position: { x: tierX.src, y: cursorY },
        data: {
          title: tbl.table || tbl.uri,
          subtitle: tbl.schema || undefined,
          side: "source" as const,
          isLookup: tbl.is_lookup,
          highlightColumnUri,
          columns: tbl.columns.map((c) => ({
            uri: c.uri,
            name: c.name,
            type: c.data_type,
            unmapped: !mappedSourceColumnUris.has(c.uri),
            isLookup: c.is_lookup,
            isUpstreamTarget: upstreamTargetUris.has(c.uri),
          })),
        },
        draggable: true,
      });
      cursorY += tableHeight(tbl.columns.length) + TABLE_GAP;
    }
  }

  // ── Place tier −1 (barycenter-ordered by the mean Y of the source columns
  //    each raw table feeds — only the NEW tier reorders) ────────────────────
  if (upstreamTables.length > 0) {
    const barycenter = (t: UpstreamTable): number => {
      const ys: number[] = [];
      for (const e of survivingUpstreamEdges) {
        if (upColToTableUri.get(e.source_uri) === t.uri) {
          const y = srcColY.get(e.target_uri);
          if (y !== undefined) ys.push(y);
        }
      }
      return ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : Number.MAX_SAFE_INTEGER;
    };
    const orderedUpstream = [...upstreamTables].sort((a, b) => barycenter(a) - barycenter(b));
    let cursorY = yStart("up");
    for (const tbl of orderedUpstream) {
      nodes.push({
        id: `up:${tbl.uri}`,
        type: "table",
        position: { x: tierX.up, y: cursorY },
        data: {
          title: tbl.table || tbl.uri,
          subtitle: tbl.schema || tbl.source_product_name || undefined,
          side: "source" as const,
          // Raw tables that feed only lookup references badge as `lookup`, like
          // the tier-0 lookup source cards they wire into.
          isLookup: tbl.is_lookup,
          highlightColumnUri,
          columns: tbl.columns.map((c) => ({
            uri: c.uri,
            name: c.name,
            type: c.data_type,
            unmapped: false,
          })),
        },
        draggable: true,
      });
      cursorY += tableHeight(tbl.columns.length) + TABLE_GAP;
    }
  }

  // ── Place tier 1 (product groups) ──────────────────────────────────────
  const colUriToNodeId = new Map<string, string>();
  {
    let cursorY = yStart("prod");
    for (const [key, group] of productGroupList) {
      const nodeId = `prod:${key}`;
      for (const c of group.columns) colUriToNodeId.set(c.uri, nodeId);
      nodes.push({
        id: nodeId,
        type: "table",
        position: { x: tierX.prod, y: cursorY },
        data: {
          title: group.title,
          // For SA products with multiple datasets we suppress a separate
          // subtitle (the title already names the dataset). For consumer /
          // single-dataset products the product name is shown as subtitle.
          subtitle: productGroupList.length > 1 ? undefined : (product.name || undefined),
          side: "product" as const,
          highlightColumnUri,
          hasDownstreamOut: consumers.length > 0 && key === firstGroupKey,
          columns: group.columns.map((c) => ({
            uri: c.uri,
            name: c.name,
            type: c.data_type,
            primaryKey: c.primary_key,
            unmapped: !mappedTargetUris.has(c.uri),
            literalValue: literalsByTarget.get(c.uri) ?? null,
          })),
        },
        draggable: true,
      });
      cursorY += tableHeight(group.columns.length) + TABLE_GAP;
    }
  }

  // ── Place tier 2 (downstream consumers, name-ordered) ──────────────────
  const orderedConsumers = [...consumers].sort((a, b) =>
    (a.name || a.uri).localeCompare(b.name || b.uri),
  );
  const renderedConsumerUris = new Set(orderedConsumers.map((c) => c.uri));
  {
    let cursorY = yStart("down");
    for (const c of orderedConsumers) {
      nodes.push({
        id: `dn:${c.uri}`,
        type: "summary",
        position: { x: tierX.down, y: cursorY },
        data: {
          title: c.name || c.uri,
          productKind: c.product_kind,
          consumerKind: c.consumer_kind,
          lifecycle: c.lifecycle_state,
        },
        draggable: true,
      });
      cursorY += SUMMARY_NODE_HEIGHT + TABLE_GAP;
    }
  }

  // ── Edges ──────────────────────────────────────────────────────────────
  const edges: Edge[] = [];

  // Primary + lookup mapping edges (tier 0 → tier 1). Unchanged. Literal-kind
  // mappings have no source side and are surfaced as chips, not edges.
  for (const m of payload.mappings) {
    if (m.source_uri === null) continue;
    const sty = STATUS_STYLE[m.status] || { stroke: "#94a3b8", label: m.status };
    const isRejected = m.status === "rejected";
    const isLookup = m.relation === "lookup_via";
    // Target the specific dataset node the column belongs to. Falls back to the
    // legacy single-prod node id when the column wasn't seen in product.columns
    // (shouldn't happen, but keeps edges from vanishing).
    const targetNodeId = colUriToNodeId.get(m.target_uri) || `prod:${fallbackKey}`;
    // Lookup edges read distinctly: a secondary (violet) dashed line labeled
    // "lookup", so a reference-table read isn't mistaken for a 1:1 mapping.
    const stroke = isLookup ? "#9333ea" : sty.stroke;
    const lookupLabel = m.role && m.role !== "value" ? `lookup (${m.role})` : "lookup";
    edges.push({
      id: m.uri,
      source: `src:${sourceUriToTableUri(m.source_uri, sourceTables)}`,
      sourceHandle: m.source_uri,
      target: targetNodeId,
      targetHandle: m.target_uri,
      style: {
        stroke,
        strokeWidth: 1.5,
        strokeDasharray: isLookup ? "5 4" : isRejected ? "4 4" : undefined,
        opacity: isLookup ? 0.85 : isRejected ? 0.6 : 1,
      },
      label: isLookup
        ? lookupLabel
        : m.transform_kind && m.transform_kind !== "direct"
        ? m.transform_kind
        : undefined,
      labelStyle: { fontSize: 10, fill: stroke, fontWeight: 600 },
      labelBgStyle: { fill: "#fff" },
      labelBgPadding: [3, 4] as [number, number],
      data: { mapping: m },
    });
  }

  // Upstream edges (tier −1 → tier 0): raw col right handle → source col left
  // handle (`in:${uri}`). Primary lineage is solid, status-colored; lookup-
  // reference upstream is dashed violet (matching the tier-0 → product lookup
  // edges), so lookup lineage reads distinctly end-to-end.
  for (const e of survivingUpstreamEdges) {
    const srcTableUri = upColToTableUri.get(e.source_uri);
    if (!srcTableUri) continue;
    const sty = STATUS_STYLE[e.status ?? ""] || { stroke: "#94a3b8" };
    const style = e.is_lookup
      ? { stroke: "#9333ea", strokeWidth: 1.25, strokeDasharray: "5 4", opacity: 0.85 }
      : { stroke: sty.stroke, strokeWidth: 1.25, opacity: 0.9 };
    edges.push({
      id: `up-edge:${e.source_uri}->${e.target_uri}`,
      source: `up:${srcTableUri}`,
      sourceHandle: e.source_uri,
      target: `src:${sourceUriToTableUri(e.target_uri, sourceTables)}`,
      targetHandle: `in:${e.target_uri}`,
      style,
      data: { upstream: { source_uri: e.source_uri, target_uri: e.target_uri } },
    });
  }

  // Downstream edges (product anchor → summary node): one aggregate indigo edge
  // with an arrow + "consumes" label. Anchored on the first product group.
  const anchorColUris =
    productGroupList.length > 0 ? productGroupList[0][1].columns.map((c) => c.uri) : [];
  for (const e of payload.downstream?.edges ?? []) {
    if (!renderedConsumerUris.has(e.target_product_uri)) continue;
    edges.push({
      id: `dn-edge:${e.target_product_uri}`,
      source: `prod:${firstGroupKey}`,
      sourceHandle: DOWNSTREAM_OUT_HANDLE,
      target: `dn:${e.target_product_uri}`,
      targetHandle: SUMMARY_IN_HANDLE,
      style: { stroke: "#6366f1", strokeWidth: 1.75 },
      markerEnd: { type: MarkerType.ArrowClosed, color: "#6366f1", width: 16, height: 16 },
      label: e.kind || "consumes",
      labelStyle: { fontSize: 10, fill: "#4338ca", fontWeight: 600 },
      labelBgStyle: { fill: "#fff" },
      labelBgPadding: [3, 4] as [number, number],
      data: { anchorColUris },
    });
  }

  return { nodes, edges };
}

/** Resolve the table URI containing a given source column URI. The mapping
 *  payload doesn't carry the table URI on each mapping row, so we walk the
 *  pre-built source_tables structure. Falls back to the column URI itself
 *  if not found (defensive — should never happen for current mappings). */
function sourceUriToTableUri(colUri: string, tables: SourceTable[]): string {
  for (const t of tables) {
    if (t.columns.some((c) => c.uri === colUri)) return t.uri;
  }
  return colUri;
}

export default function MappingGraphView({
  projectId,
  payload: externalPayload,
  readOnly = false,
  selectedMappingUri,
  focusUnmappedColumnUri,
  onMappingSelect,
  onUnmappedColumnSelect,
  onWireSource,
  onEditMapping,
  enableHopExpansion = false,
  productUri,
  productKind,
}: Props) {
  const [fetchedPayload, setFetchedPayload] = useState<GraphPayload | null>(null);
  // When the parent provides a payload, skip the loading state — they own
  // the fetch lifecycle. Otherwise we own it via projectId.
  const [loading, setLoading] = useState<boolean>(externalPayload === undefined && !!projectId);
  const [error, setError] = useState<string | null>(null);
  // Default OFF — show every discovered source table on the canvas so the
  // engineer can pre-visualise pre-mapping. Toggle persists in localStorage
  // so the choice sticks across visits.
  const [hideUnmappedSources, setHideUnmappedSources] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return window.localStorage.getItem(STORAGE_KEY_HIDE_UNMAPPED) === "1";
  });
  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(STORAGE_KEY_HIDE_UNMAPPED, hideUnmappedSources ? "1" : "0");
  }, [hideUnmappedSources]);

  // ── Opt-in multi-hop lineage (marketplace only) ─────────────────────────
  // Both tiers default OFF and are fetched lazily on first toggle-on — the
  // upstream query walks cross-project into source projects and downstream
  // scans consumer contracts, so neither should ride the default load or any
  // engineer render. Toggle state persists across visits.
  const [showUpstream, setShowUpstream] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return window.localStorage.getItem(STORAGE_KEY_SHOW_UPSTREAM) === "1";
  });
  const [showDownstream, setShowDownstream] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return window.localStorage.getItem(STORAGE_KEY_SHOW_DOWNSTREAM) === "1";
  });
  const [hopUpstream, setHopUpstream] = useState<GraphPayload["upstream"] | null>(null);
  const [hopDownstream, setHopDownstream] = useState<GraphPayload["downstream"] | null>(null);
  // Captured via onInit so we can imperatively re-fit — React Flow's `fitView`
  // prop only fires on mount, but toggling a tier on/off shifts every tier
  // horizontally and would otherwise leave the rightmost tier off-screen.
  const rfInstance = useRef<ReactFlowInstance | null>(null);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(STORAGE_KEY_SHOW_UPSTREAM, showUpstream ? "1" : "0");
  }, [showUpstream]);
  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(STORAGE_KEY_SHOW_DOWNSTREAM, showDownstream ? "1" : "0");
  }, [showDownstream]);

  // Lazy-fetch each tier once, the first time its toggle flips on.
  useEffect(() => {
    if (!enableHopExpansion || !productUri || !showUpstream || hopUpstream !== null) return;
    let cancelled = false;
    api
      .get("/api/marketplace/mapping-graph", { params: { uri: productUri, include_upstream: 1 } })
      .then((res) => {
        if (!cancelled) setHopUpstream(res.data?.upstream ?? { tables: [], edges: [] });
      })
      .catch(() => {
        if (!cancelled) setHopUpstream({ tables: [], edges: [] });
      });
    return () => { cancelled = true; };
  }, [enableHopExpansion, productUri, showUpstream, hopUpstream]);

  useEffect(() => {
    if (!enableHopExpansion || !productUri || !showDownstream || hopDownstream !== null) return;
    let cancelled = false;
    api
      .get("/api/marketplace/mapping-graph", { params: { uri: productUri, include_downstream: 1 } })
      .then((res) => {
        if (!cancelled) setHopDownstream(res.data?.downstream ?? { consumers: [], edges: [] });
      })
      .catch(() => {
        if (!cancelled) setHopDownstream({ consumers: [], edges: [] });
      });
    return () => { cancelled = true; };
  }, [enableHopExpansion, productUri, showDownstream, hopDownstream]);

  // Loading is DERIVED (never stored) so we never setState synchronously inside
  // an effect: a tier reads as loading from the moment its toggle flips on
  // until its fetch resolves and populates the cache (which flips these false).
  const hopLoading = {
    up: enableHopExpansion && !!productUri && showUpstream && hopUpstream === null,
    down: enableHopExpansion && !!productUri && showDownstream && hopDownstream === null,
  };

  // Re-fit whenever a tier is added/removed (toggle flip or its lazy fetch
  // lands), so the widened canvas re-frames and the rightmost tier stays in
  // view. A double rAF gives React Flow a frame to measure the new nodes
  // before we fit to them.
  useEffect(() => {
    if (!enableHopExpansion) return;
    let raf2 = 0;
    const raf1 = requestAnimationFrame(() => {
      raf2 = requestAnimationFrame(() => {
        rfInstance.current?.fitView({ padding: 0.22, duration: 200 });
      });
    });
    return () => {
      cancelAnimationFrame(raf1);
      cancelAnimationFrame(raf2);
    };
  }, [enableHopExpansion, showUpstream, showDownstream, hopUpstream, hopDownstream]);

  // Only fetch when the parent didn't supply a payload AND a projectId is
  // available. Marketplace usage hands us a payload directly.
  useEffect(() => {
    if (externalPayload !== undefined) {
      setLoading(false);
      return;
    }
    if (!projectId) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .get(`/api/projects/${projectId}/reviews/mappings/graph`)
      .then((res) => {
        if (cancelled) return;
        setFetchedPayload(res.data);
        setLoading(false);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Failed to load mapping graph");
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, externalPayload]);

  const payloadBase = externalPayload !== undefined ? externalPayload : fetchedPayload;

  // Effective payload = base + whichever opt-in tiers are toggled on and cached.
  // Toggling a tier off strips its key so buildLayout (which gates purely on
  // payload.upstream / payload.downstream presence) drops the tier. When
  // enableHopExpansion is false (every engineer embedder), this is a pure
  // pass-through, so those paths stay byte-identical.
  const payload = useMemo(() => {
    if (!payloadBase || !enableHopExpansion) return payloadBase;
    const next: GraphPayload = { ...payloadBase };
    if (showUpstream && hopUpstream) next.upstream = hopUpstream;
    else delete next.upstream;
    if (showDownstream && hopDownstream) next.downstream = hopDownstream;
    else delete next.downstream;
    return next;
  }, [payloadBase, enableHopExpansion, showUpstream, hopUpstream, showDownstream, hopDownstream]);

  // Click-to-isolate: when the engineer clicks a column row on either side,
  // dim every edge that doesn't touch that column. Lets them disambiguate a
  // single column's mappings from the noise on a dense canvas. Cleared by
  // clicking the same column again, clicking empty canvas, or the X on the
  // floating callout. Independent from focusUnmappedColumnUri (which is
  // parent-driven for dashboard deep-links).
  const [focusedColumnUri, setFocusedColumnUri] = useState<string | null>(null);

  // Effective column highlight — the focused column wins over the parent's
  // deep-link target so click feedback is immediate.
  const effectiveHighlightUri = focusedColumnUri ?? focusUnmappedColumnUri ?? null;

  const { nodes, edges } = useMemo(() => {
    if (!payload) return { nodes: [], edges: [] };
    const { nodes: nL, edges: eL } = buildLayout(payload, effectiveHighlightUri, hideUnmappedSources);
    const eStyled = eL.map((e) => {
      const d = e.data as
        | { mapping?: MappingEdge; upstream?: { source_uri: string; target_uri: string }; anchorColUris?: string[] }
        | undefined;
      const mapping = d?.mapping;
      // A focused column lights the edges that touch it. Mapping + upstream
      // edges carry column identity; downstream product-level edges have none,
      // so they light only when a column in the anchor product group is focused.
      let inFocus = true;
      if (focusedColumnUri) {
        if (mapping) {
          inFocus = mapping.source_uri === focusedColumnUri || mapping.target_uri === focusedColumnUri;
        } else if (d?.upstream) {
          inFocus = d.upstream.source_uri === focusedColumnUri || d.upstream.target_uri === focusedColumnUri;
        } else if (d?.anchorColUris) {
          inFocus = d.anchorColUris.includes(focusedColumnUri);
        } else {
          inFocus = false;
        }
      }
      const baseStyle = { ...e.style };
      if (!inFocus) {
        // Dim out-of-focus edges hard so the focused column's edges read clearly.
        baseStyle.opacity = 0.08;
      }
      if (e.id === selectedMappingUri) {
        return { ...e, style: { ...baseStyle, strokeWidth: 3 }, animated: inFocus };
      }
      return { ...e, style: baseStyle };
    });
    return { nodes: nL, edges: eStyled };
  }, [payload, selectedMappingUri, effectiveHighlightUri, focusedColumnUri, hideUnmappedSources]);

  /** Resolve a focused column's display name from the payload, for the
   *  "Showing only edges for …" callout. Falls back to the URI's last
   *  segment if the column isn't found (shouldn't happen since we only set
   *  the URI by clicking a rendered row). */
  const focusedColumnDisplay = useMemo(() => {
    if (!focusedColumnUri || !payload) return null;
    for (const t of payload.source_tables) {
      const c = t.columns.find((c) => c.uri === focusedColumnUri);
      if (c) return `${t.schema}.${t.table}.${c.name}`;
    }
    const pc = payload.product.columns.find((c) => c.uri === focusedColumnUri);
    if (pc) return `${pc.dataset_name ?? payload.product.name ?? "product"}.${pc.name}`;
    const m = focusedColumnUri.match(/[^.:]+$/);
    return m ? m[0] : focusedColumnUri;
  }, [focusedColumnUri, payload]);

  // Authoring: dragging a source-column handle onto a product-column handle
  // fires (sourceColumnUri, productColumnUri). The handle ids ARE the column
  // URIs (see edge construction). Parent decides what to do (wire + open the
  // editor). Gated by readOnly + onWireSource so the marketplace canvas is inert.
  const handleConnect = useCallback(
    (conn: Connection) => {
      if (readOnly || !onWireSource) return;
      if (conn.sourceHandle && conn.targetHandle) {
        onWireSource(conn.sourceHandle, conn.targetHandle);
      }
    },
    [onWireSource, readOnly],
  );

  const handleEdgeClick = useCallback<EdgeMouseHandler>(
    (_event, edge) => {
      if (readOnly || !onMappingSelect) return;
      // Lookup edges carry a synthetic id; open the underlying mapping editor
      // via the real mapping_uri stashed on the edge data.
      const m = (edge.data as { mapping?: MappingEdge } | undefined)?.mapping;
      onMappingSelect((m?.relation === "lookup_via" && m.mapping_uri) || edge.id);
    },
    [onMappingSelect, readOnly]
  );

  // Double-click an edge → open the pop-up transform editor for that mapping.
  const handleEdgeDoubleClick = useCallback<EdgeMouseHandler>(
    (_event, edge) => {
      if (readOnly || !onEditMapping) return;
      const m = (edge.data as { mapping?: MappingEdge } | undefined)?.mapping;
      onEditMapping((m?.relation === "lookup_via" && m.mapping_uri) || edge.id);
    },
    [onEditMapping, readOnly]
  );

  // Edge hover tooltip — shows the mapping's transform details so the
  // reviewer can scan kind + expression + params without leaving the graph.
  // Positioned relative to the canvas container so it tracks the cursor.
  const [hoveredEdge, setHoveredEdge] = useState<MappingEdge | null>(null);
  const [hoverPos, setHoverPos] = useState<{ x: number; y: number }>({ x: 0, y: 0 });
  const handleEdgeMouseEnter = useCallback<EdgeMouseHandler>(
    (event, edge) => {
      const mapping = (edge.data as { mapping?: MappingEdge } | undefined)?.mapping;
      if (!mapping) return;
      setHoveredEdge(mapping);
      // Use the bounding rect of the ReactFlow container so the tooltip
      // stays inside the canvas regardless of page scroll.
      const container = (event.currentTarget as HTMLElement)?.closest(".react-flow") as HTMLElement | null;
      const rect = container?.getBoundingClientRect();
      setHoverPos({
        x: event.clientX - (rect?.left ?? 0) + 12,
        y: event.clientY - (rect?.top ?? 0) + 12,
      });
    },
    []
  );
  const handleEdgeMouseLeave = useCallback<EdgeMouseHandler>(() => {
    setHoveredEdge(null);
  }, []);

  const handleNodeClick = useCallback<NodeMouseHandler>(
    (event, node) => {
      if (readOnly) return;
      if (!payload) return;
      // Walk up to the column row that received the click. If the click
      // didn't land on a column row (e.g. the table header) just no-op.
      const target = event.target as HTMLElement | null;
      const row = target?.closest?.("[data-column-uri]") as HTMLElement | null;
      if (!row) return;
      const colUri = row.dataset.columnUri;
      if (!colUri) return;

      // Toggle column-isolation focus on every column click. Clicking the
      // same column again clears the focus. Edge-dimming is driven by
      // focusedColumnUri in the useMemo above.
      setFocusedColumnUri((prev) => (prev === colUri ? null : colUri));

      // Preserve the existing unmapped-product deep-link behavior: an
      // unmapped product column click also fires onUnmappedColumnSelect so
      // the dashboard's "Create mapping…" panel opens.
      if (node.id.startsWith("prod:") && onUnmappedColumnSelect) {
        const isMapped = payload.mappings.some((m) => m.target_uri === colUri);
        if (!isMapped) onUnmappedColumnSelect(colUri);
      }
    },
    [onUnmappedColumnSelect, payload, readOnly]
  );

  /** Pane-click clears column isolation. React Flow fires this when the
   *  user clicks empty canvas (not a node, not an edge). Gives the
   *  engineer an escape from focus without hunting for the callout's X. */
  const handlePaneClick = useCallback(() => {
    if (focusedColumnUri) setFocusedColumnUri(null);
  }, [focusedColumnUri]);

  if (loading) {
    return (
      <div style={{ padding: 24, color: "#64748b", fontSize: 13 }}>
        Loading mapping graph…
      </div>
    );
  }
  if (error) {
    return (
      <div style={{ padding: 16, color: "#9a3412", fontSize: 13, backgroundColor: "#fff7ed", borderRadius: 6 }}>
        Couldn't load the graph: {error}
      </div>
    );
  }
  if (!payload || payload.product.columns.length === 0) {
    return (
      <div style={{ padding: 24, color: "#64748b", fontSize: 13, textAlign: "center" }}>
        No data product columns to display yet — run ODCS Specification + ODCS&nbsp;→&nbsp;DProd first.
      </div>
    );
  }

  return (
    <div
      style={{
        position: "relative",
        // Taller only for the marketplace hop-expansion surface, so the extra
        // tiers + the corner overlays (toggles top-left, legend bottom-right)
        // don't crowd the graph. Engineer embedders keep the legacy 620.
        height: enableHopExpansion ? 700 : 620,
        border: "1px solid #e2e8f0",
        borderRadius: 8,
        backgroundColor: "#f8fafc",
      }}
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        onInit={(inst) => { rfInstance.current = inst; }}
        onConnect={handleConnect}
        nodesConnectable={!readOnly && !!onWireSource}
        onEdgeClick={handleEdgeClick}
        onEdgeDoubleClick={handleEdgeDoubleClick}
        onEdgeMouseEnter={handleEdgeMouseEnter}
        onEdgeMouseLeave={handleEdgeMouseLeave}
        onNodeClick={handleNodeClick}
        onPaneClick={handlePaneClick}
        fitView
        // Extra padding when the hop tiers can widen the canvas, so the
        // rightmost product/consumer tier clears the top-corner overlays.
        fitViewOptions={{ padding: enableHopExpansion ? 0.22 : 0.15 }}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={16} color="#e2e8f0" />
        <Controls showInteractive={false} />
      </ReactFlow>
      <FilterToggle
        checked={hideUnmappedSources}
        onChange={setHideUnmappedSources}
      />
      {enableHopExpansion && (
        <HopToggles
          showUpstream={showUpstream}
          showDownstream={showDownstream}
          onShowUpstream={setShowUpstream}
          onShowDownstream={setShowDownstream}
          upstreamEligible={productKind === "consumer"}
          loading={hopLoading}
          noDownstream={showDownstream && !!hopDownstream && hopDownstream.consumers.length === 0}
        />
      )}
      <Legend showTiers={enableHopExpansion} />
      {focusedColumnUri && focusedColumnDisplay && (
        <FocusCallout
          columnLabel={focusedColumnDisplay}
          onClear={() => setFocusedColumnUri(null)}
        />
      )}
      {hoveredEdge && <EdgeTooltip mapping={hoveredEdge} pos={hoverPos} />}
    </div>
  );
}

/** Floating callout that surfaces the active column-isolation focus and
 *  provides an explicit Clear button. Engineers who land on the canvas
 *  with edges already dimmed need an obvious way to recover, so we always
 *  render this when focusedColumnUri is set. */
function FocusCallout({
  columnLabel,
  onClear,
}: {
  columnLabel: string;
  onClear: () => void;
}) {
  return (
    <div
      style={{
        position: "absolute",
        left: "50%",
        top: 12,
        transform: "translateX(-50%)",
        padding: "6px 12px",
        borderRadius: 16,
        backgroundColor: "rgba(15,23,42,0.92)",
        color: "#f1f5f9",
        fontSize: 12,
        boxShadow: "0 4px 12px rgba(15,23,42,0.25)",
        display: "flex",
        alignItems: "center",
        gap: 10,
        zIndex: 11,
      }}
    >
      <span>
        Showing only edges for <span style={{ fontFamily: "monospace", fontWeight: 600 }}>{columnLabel}</span>
      </span>
      <button
        type="button"
        onClick={onClear}
        style={{
          border: "1px solid #475569",
          backgroundColor: "transparent",
          color: "#f1f5f9",
          borderRadius: 4,
          padding: "1px 8px",
          fontSize: 11,
          fontWeight: 600,
          cursor: "pointer",
        }}
        title="Clear focus (or click empty canvas, or click the column again)"
      >
        Clear
      </button>
    </div>
  );
}

/** Hover tooltip showing a mapping's transform details — kind, expression,
 *  and params. Positioned relative to the canvas container. Surfaces enough
 *  for the reviewer to recognize "this is the mask of email" or "this is a
 *  lookup against country_code_iso" without clicking through to the editor. */
function EdgeTooltip({
  mapping,
  pos,
}: {
  mapping: MappingEdge;
  pos: { x: number; y: number };
}) {
  const params = useMemo(() => {
    if (!mapping.transform_params_json) return null;
    try {
      const parsed = JSON.parse(mapping.transform_params_json);
      if (parsed && typeof parsed === "object" && Object.keys(parsed).length > 0) return parsed;
    } catch {
      // fall through
    }
    return null;
  }, [mapping.transform_params_json]);

  // A dashed lookup_via edge: surface the lookup CONFIG (strategy, table/key,
  // aggregate function or raw aggregate_expression, filter) as a first-class
  // block instead of a raw params dump — this is the "what does this lookup
  // actually compute" question the canvas couldn't answer.
  const isLookupEdge = mapping.relation === "lookup_via";
  const p = (params ?? {}) as Record<string, unknown>;
  const str = (k: string): string | null => {
    const v = p[k];
    return typeof v === "string" && v.trim() ? v.trim() : null;
  };
  const lk = {
    strategy: mapping.strategy ?? str("selection_strategy"),
    lookup_table: str("lookup_table"),
    key_column: str("key_column"),
    value_column: str("value_column"),
    aggregate_function: str("aggregate_function"),
    aggregate_expression: str("aggregate_expression"),
    filter_clause: str("filter_clause"),
  };
  // Keys already rendered in the lookup block above — don't repeat them in the
  // generic params dump below.
  const LOOKUP_KEYS = new Set([
    "selection_strategy", "lookup_table", "key_column", "value_column",
    "aggregate_function", "aggregate_expression", "filter_clause",
  ]);
  const extraParams = isLookupEdge && params
    ? Object.fromEntries(Object.entries(params).filter(([k]) => !LOOKUP_KEYS.has(k)))
    : params;
  const showParams = extraParams && Object.keys(extraParams).length > 0;

  return (
    <div
      style={{
        position: "absolute",
        left: pos.x,
        top: pos.y,
        zIndex: 20,
        maxWidth: 360,
        padding: "8px 10px",
        borderRadius: 6,
        backgroundColor: "rgba(15,23,42,0.92)",
        color: "#f1f5f9",
        fontSize: 11,
        lineHeight: 1.4,
        boxShadow: "0 4px 12px rgba(15,23,42,0.25)",
        pointerEvents: "none",
        fontFamily: "system-ui, sans-serif",
      }}
    >
      <div style={{ marginBottom: 4 }}>
        <span style={{ color: "#94a3b8" }}>Kind: </span>
        <span style={{ fontFamily: "monospace", fontWeight: 600 }}>
          {mapping.transform_kind ?? "direct"}
        </span>
        {mapping.transform_author && (
          <span style={{ marginLeft: 8, color: "#94a3b8", fontSize: 10 }}>
            ({mapping.transform_author})
          </span>
        )}
      </div>
      {isLookupEdge && (
        <div style={{ marginBottom: 6, paddingBottom: 6, borderBottom: "1px solid rgba(148,163,184,0.25)" }}>
          <div style={{ color: "#c4b5fd", fontWeight: 600, marginBottom: 2 }}>
            Lookup — reads this table as a {mapping.role === "key" ? "join key" : "value source"}
          </div>
          {lk.strategy && (
            <div style={{ fontFamily: "monospace", fontSize: 10 }}>
              <span style={{ color: "#94a3b8" }}>strategy: </span>{lk.strategy}
              {lk.strategy === "aggregate" && lk.aggregate_function && !lk.aggregate_expression && (
                <> · {lk.aggregate_function}({lk.value_column ?? "…"})</>
              )}
            </div>
          )}
          {(lk.lookup_table || lk.key_column) && (
            <div style={{ fontFamily: "monospace", fontSize: 10 }}>
              <span style={{ color: "#94a3b8" }}>on: </span>
              {lk.lookup_table ?? "?"}{lk.key_column ? `.${lk.key_column}` : ""}
            </div>
          )}
          {lk.aggregate_expression && (
            <div style={{ marginTop: 3 }}>
              <div style={{ color: "#94a3b8", fontSize: 10 }}>aggregate expression:</div>
              <div style={{
                fontFamily: "monospace", fontSize: 10, whiteSpace: "pre-wrap",
                backgroundColor: "rgba(148,163,184,0.12)", padding: "3px 5px",
                borderRadius: 4, marginTop: 1,
              }}>
                {lk.aggregate_expression}
              </div>
            </div>
          )}
          {lk.filter_clause && (
            <div style={{ fontFamily: "monospace", fontSize: 10, marginTop: 2 }}>
              <span style={{ color: "#94a3b8" }}>filter: </span>{lk.filter_clause}
            </div>
          )}
        </div>
      )}
      {mapping.transform_expression && !(isLookupEdge && lk.aggregate_expression) && (
        <div style={{ marginBottom: 4 }}>
          <div style={{ color: "#94a3b8" }}>Expression:</div>
          <div style={{ fontFamily: "monospace", wordBreak: "break-all" }}>
            {mapping.transform_expression}
          </div>
        </div>
      )}
      {showParams && (
        <div style={{ marginBottom: 4 }}>
          <div style={{ color: "#94a3b8" }}>Params:</div>
          {Object.entries(extraParams as Record<string, unknown>).map(([k, v]) => (
            <div key={k} style={{ fontFamily: "monospace", fontSize: 10 }}>
              {k}: {typeof v === "string" ? v : JSON.stringify(v)}
            </div>
          ))}
        </div>
      )}
      {mapping.literal_value && (
        <div style={{ marginBottom: 4 }}>
          <span style={{ color: "#94a3b8" }}>Literal: </span>
          <span style={{ fontFamily: "monospace" }}>{mapping.literal_value}</span>
        </div>
      )}
      {mapping.transform_escalation_reason && (
        <div style={{ marginBottom: 4, color: "#fbbf24" }}>
          <div style={{ fontWeight: 600 }}>Steward escalation:</div>
          <div>{mapping.transform_escalation_reason}</div>
        </div>
      )}
      {mapping.similarity_score != null && (
        <div style={{ marginTop: 4, color: "#94a3b8", fontSize: 10 }}>
          score: {mapping.similarity_score.toFixed(2)} · click to open in review
        </div>
      )}
    </div>
  );
}

function FilterToggle({
  checked,
  onChange,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <div
      style={{
        position: "absolute",
        left: 12,
        top: 12,
        padding: "6px 10px",
        borderRadius: 6,
        backgroundColor: "rgba(255,255,255,0.95)",
        border: "1px solid #e2e8f0",
        fontSize: 12,
        boxShadow: "0 2px 6px rgba(15,23,42,0.06)",
        zIndex: 10,
      }}
    >
      <label
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          cursor: "pointer",
          color: "#334155",
        }}
      >
        <input
          type="checkbox"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          style={{ cursor: "pointer" }}
        />
        Show only sources with mappings
      </label>
    </div>
  );
}

/** Marketplace-only checkboxes to expand one extra hop of lineage in each
 *  direction. Sits just below FilterToggle. The upstream checkbox is hidden
 *  for source-aligned products (they have no upstream hop). */
function HopToggles({
  showUpstream,
  showDownstream,
  onShowUpstream,
  onShowDownstream,
  upstreamEligible,
  loading,
  noDownstream,
}: {
  showUpstream: boolean;
  showDownstream: boolean;
  onShowUpstream: (v: boolean) => void;
  onShowDownstream: (v: boolean) => void;
  upstreamEligible: boolean;
  loading: { up: boolean; down: boolean };
  noDownstream: boolean;
}) {
  const rowStyle: CSSProperties = {
    display: "flex",
    alignItems: "center",
    gap: 6,
    cursor: "pointer",
    color: "#334155",
  };
  return (
    <div
      style={{
        position: "absolute",
        left: 12,
        top: 52,
        padding: "6px 10px",
        borderRadius: 6,
        backgroundColor: "rgba(255,255,255,0.95)",
        border: "1px solid #e2e8f0",
        fontSize: 12,
        boxShadow: "0 2px 6px rgba(15,23,42,0.06)",
        zIndex: 10,
        display: "flex",
        flexDirection: "column",
        gap: 4,
      }}
    >
      {upstreamEligible && (
        <label style={rowStyle}>
          <input
            type="checkbox"
            checked={showUpstream}
            onChange={(e) => onShowUpstream(e.target.checked)}
            style={{ cursor: "pointer" }}
          />
          Show upstream sources
          {loading.up && <Spinner />}
        </label>
      )}
      <label style={rowStyle}>
        <input
          type="checkbox"
          checked={showDownstream}
          onChange={(e) => onShowDownstream(e.target.checked)}
          style={{ cursor: "pointer" }}
        />
        Show downstream consumers
        {loading.down && <Spinner />}
      </label>
      {noDownstream && (
        <div style={{ fontSize: 10, color: "#94a3b8", paddingLeft: 22 }}>
          No products consume this yet.
        </div>
      )}
    </div>
  );
}

/** Tiny inline spinner shown on a toggling checkbox while its tier fetches. */
function Spinner() {
  return (
    <>
      <style>{"@keyframes mgv-spin { to { transform: rotate(360deg); } }"}</style>
      <span
        style={{
          display: "inline-block",
          width: 10,
          height: 10,
          border: "1.5px solid #cbd5e1",
          borderTopColor: "#6366f1",
          borderRadius: "50%",
          animation: "mgv-spin 0.7s linear infinite",
        }}
      />
    </>
  );
}

function Legend({ showTiers = false }: { showTiers?: boolean }) {
  return (
    <div
      style={{
        position: "absolute",
        right: 12,
        // Anchored to the bottom-right so it clears the rightmost product /
        // consumer tier (which centers vertically and reaches the right edge
        // once the hop tiers widen the canvas).
        bottom: 12,
        padding: 10,
        borderRadius: 6,
        backgroundColor: "rgba(255,255,255,0.95)",
        border: "1px solid #e2e8f0",
        fontSize: 11,
        boxShadow: "0 2px 6px rgba(15,23,42,0.06)",
        zIndex: 10,
      }}
    >
      <div style={{ fontWeight: 700, marginBottom: 6, color: "#334155", textTransform: "uppercase", letterSpacing: 0.4, fontSize: 9 }}>
        Edge color
      </div>
      {Object.entries(STATUS_STYLE).map(([k, v]) => (
        <div key={k} style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 3 }}>
          <span
            style={{
              display: "inline-block",
              width: 18,
              height: 2,
              backgroundColor: v.stroke,
              borderTop: k === "rejected" ? `2px dashed ${v.stroke}` : undefined,
              backgroundImage: k === "rejected" ? "none" : undefined,
            }}
          />
          <span style={{ color: "#475569" }}>{v.label}</span>
        </div>
      ))}
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 3 }}>
        <span
          style={{
            display: "inline-block",
            width: 18,
            height: 0,
            borderTop: "2px dashed #9333ea",
          }}
        />
        <span style={{ color: "#475569" }}>Lookup source</span>
      </div>
      <div style={{ marginTop: 6, paddingTop: 6, borderTop: "1px solid #e2e8f0", color: "#9a3412", fontSize: 10 }}>
        Orange handle = unmapped product column
      </div>
      {showTiers && (
        <div style={{ marginTop: 6, paddingTop: 6, borderTop: "1px solid #e2e8f0" }}>
          <div style={{ fontWeight: 700, marginBottom: 6, color: "#334155", textTransform: "uppercase", letterSpacing: 0.4, fontSize: 9 }}>
            Tiers
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 3 }}>
            <span style={{ display: "inline-block", width: 12, height: 10, border: "1px solid #0369a1", backgroundColor: "#e0f2fe", borderRadius: 2 }} />
            <span style={{ color: "#475569" }}>Upstream (raw catalog)</span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 3 }}>
            <span style={{ display: "inline-block", width: 12, height: 10, border: "1px solid #6366f1", backgroundColor: "#eef2ff", borderRadius: 2 }} />
            <span style={{ color: "#475569" }}>Downstream consumer</span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ display: "inline-block", width: 18, height: 0, borderTop: "2px solid #6366f1" }} />
            <span style={{ color: "#475569" }}>consumes →</span>
          </div>
        </div>
      )}
    </div>
  );
}
