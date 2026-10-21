// ELK layered layout + orthogonal edge routing, for the focused view.
// Ported from the design's merge-elk.js. Replaces dagre's positions AND the
// hand-rolled port/stub/avoid router: ELK computes clearance, port order and
// orthogonal bend points as one system, so edges leave/enter node sides and
// turn at clean right angles instead of cutting diagonally across the canvas.
import ELK from "elkjs/lib/elk.bundled.js";
import { NODE_W, NODE_H, type GraphNode, type GraphEdge, type Point, type PosMap } from "./estateLayout";

const elk = new ELK();

const OPTS: Record<string, string> = {
  "elk.algorithm": "layered",
  "elk.direction": "RIGHT",
  "elk.edgeRouting": "ORTHOGONAL",
  "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
  "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
  "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
  "elk.spacing.nodeNode": "34",
  "elk.spacing.edgeNode": "26",
  "elk.spacing.edgeEdge": "14",
  "elk.layered.spacing.nodeNodeBetweenLayers": "150",
  "elk.layered.spacing.edgeNodeBetweenLayers": "30",
  "elk.layered.spacing.edgeEdgeBetweenLayers": "14",
  "elk.padding": "[top=40,left=40,bottom=40,right=40]",
  "elk.portConstraints": "FIXED_SIDE",
};

export interface ElkOut {
  pos: PosMap;
  routes: Record<string, Point[]>;
  width?: number;
  height?: number;
}

// Resolves to { pos, routes }. Node boxes are the canonical card size so
// positions stay tier-independent, exactly as with dagre. One port per side
// keeps every edge leaving right / entering left, and lets ELK order the
// fan-out instead of us guessing.
export async function elkLayout(nodes: GraphNode[], edges: GraphEdge[]): Promise<ElkOut> {
  const ids = new Set(nodes.map((n) => n.id));
  const used = edges.filter((x) => ids.has(x.source) && ids.has(x.target));

  const graph = {
    id: "root",
    layoutOptions: OPTS,
    children: nodes.map((n) => ({
      id: n.id,
      width: NODE_W,
      height: NODE_H,
      properties: { "org.eclipse.elk.portConstraints": "FIXED_SIDE" },
      ports: [
        { id: n.id + ":out", layoutOptions: { "elk.port.side": "EAST" }, width: 1, height: 1 },
        { id: n.id + ":in", layoutOptions: { "elk.port.side": "WEST" }, width: 1, height: 1 },
      ],
    })),
    edges: used.map((x) => ({
      id: x.source + ">" + x.target,
      sources: [x.source + ":out"],
      targets: [x.target + ":in"],
    })),
  };

  // elkjs' bundled types are loose; cast through unknown for the layout call.
  const res = (await elk.layout(graph as never)) as {
    width?: number;
    height?: number;
    children?: { id: string; x: number; y: number }[];
    edges?: { id: string; sections?: { startPoint: Point; endPoint: Point; bendPoints?: Point[] }[] }[];
  };

  const pos: PosMap = {};
  (res.children || []).forEach((c) => { pos[c.id] = { x: c.x, y: c.y }; });

  const routes: Record<string, Point[]> = {};
  (res.edges || []).forEach((ed) => {
    const sec = ed.sections && ed.sections[0];
    if (!sec) return;
    routes[ed.id] = [sec.startPoint].concat(sec.bendPoints || [], [sec.endPoint]);
  });
  return { pos, routes, width: res.width, height: res.height };
}
