/**
 * Headless fixed-column layout for the value-flow ("Sankey") view.
 *
 * NOT d3-sankey — the reference is a uniform-box node-link diagram with
 * empty/ghost columns, which d3-sankey fights. This mirrors `MappingGraphView`'s
 * hand-computed `tierX` / `yStart` approach: uniform boxes, one fixed column per
 * payload column (indexed 0..4), each column vertically centered against the
 * tallest.
 *
 * Focus + reuse-heat helpers reuse `estateLayout`'s `traverse` (a `FlowLink` is
 * structurally a `GraphEdge`).
 */

import { traverse } from "../estate-graph/estateLayout";
import type { FlowLink, FlowNode, FlowPayload } from "./flowSankeyTypes";

export const NODE_W = 220;
export const NODE_H = 56;
export const COL_X0 = 40;
export const COL_PITCH = 340; // NODE_W + headroom for edge labels
export const ROW_GAP = 24;

export interface LayoutResult {
  pos: Record<string, { x: number; y: number }>;
  /** One synthesized ghost node per empty column (frontend-only; keeps the
   *  column occupying space so the 5-column frame always reads). */
  ghostNodes: FlowNode[];
}

/** Placeholder copy per column when it has no real nodes. */
const GHOST_COPY: Record<string, string> = {
  source_system: "No source schemas yet",
  source_aligned: "No source-aligned products yet",
  aggregate: "No aggregate products yet",
  consumer: "No consumer-aligned products yet",
  use_case: "Use cases coming soon",
};

/** Product columns — the ones reuse-heat and grain semantics apply to. The
 *  source column reaches everything, so it's excluded from the heat ramp. */
export const PRODUCT_COLUMN_KEYS = new Set(["source_aligned", "aggregate", "consumer"]);

export function layoutFlow(payload: FlowPayload): LayoutResult {
  const ghostNodes: FlowNode[] = [];
  // Effective per-column node list: real nodes, or a single synthesized ghost.
  const columnsEff = payload.columns.map((col) => {
    if (col.nodes.length > 0) return { key: col.key, nodes: col.nodes };
    const ghost: FlowNode = {
      id: `ghost:${col.key}`,
      label: GHOST_COPY[col.key] || "Coming soon",
      column: col.key,
      placeholder: true,
    };
    ghostNodes.push(ghost);
    return { key: col.key, nodes: [ghost] };
  });

  const heights = columnsEff.map(
    (c) => c.nodes.length * NODE_H + Math.max(0, c.nodes.length - 1) * ROW_GAP,
  );
  const maxH = heights.length ? Math.max(...heights) : 0;

  const pos: Record<string, { x: number; y: number }> = {};
  columnsEff.forEach((col, i) => {
    const x = COL_X0 + i * COL_PITCH;
    let y = (maxH - heights[i]) / 2;
    for (const n of col.nodes) {
      pos[n.id] = { x, y };
      y += NODE_H + ROW_GAP;
    }
  });

  return { pos, ghostNodes };
}

/** Up∪down BFS from a node over the links — the highlight/focus set. */
export function focusSetFor(id: string, links: FlowLink[]): Set<string> {
  const up = traverse(id, links, "up");
  const down = traverse(id, links, "down");
  return new Set<string>([id, ...up, ...down]);
}

/** Downstream reach size per node (reuse-heat magnitude). */
export function computeFanOut(nodeIds: string[], links: FlowLink[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const id of nodeIds) out[id] = traverse(id, links, "down").size;
  return out;
}
