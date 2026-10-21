// Headless layout engines for the Estate Graph. Ported from the "Lineage
// Merge" design bundle (merge-layout.js), adapted to TS + ESM imports:
// d3-force for the clustered estate overview, @dagrejs/dagre for the focused
// left-to-right lineage DAG. Both return { [nodeId]: {x, y} } in flow coords.
import {
  forceSimulation,
  forceLink,
  forceManyBody,
  forceX,
  forceY,
  forceCollide,
  type SimulationNodeDatum,
} from "d3-force";
import dagre from "@dagrejs/dagre";

export const NODE_W = 244;
export const NODE_H = 78;

export interface GraphNode {
  id: string;
  domain: string;
  system: string;
  [k: string]: unknown;
}
export interface GraphEdge {
  source: string;
  target: string;
  [k: string]: unknown;
}
export type Point = { x: number; y: number };
export type PosMap = Record<string, Point>;

interface ForceOpts {
  cluster?: number;
  charge?: number;
  link?: number;
  ticks?: number;
  ring?: number;
  key?: "domain" | "system" | "type";
}

interface SimNode extends SimulationNodeDatum {
  id: string;
  k: string;
}

// ── overview: d3-force, clustered by domain (or system) ─────────
// Runs to completion synchronously — no animating simulation.
export function forceLayout(nodes: GraphNode[], edges: GraphEdge[], opts?: ForceOpts): PosMap {
  const o = { cluster: 0.22, charge: -290, link: 62, ticks: 340, ring: 1500, key: "domain" as const, ...opts };
  const keys = Array.from(new Set(nodes.map((n) => String(n[o.key]))));
  const anchor: Record<string, Point> = {};
  keys.forEach((k, i) => {
    const a = (i / keys.length) * Math.PI * 2 - Math.PI / 2;
    anchor[k] = { x: Math.cos(a) * o.ring, y: Math.sin(a) * o.ring * 0.66 };
  });
  // shared core sits in the middle — it is what every domain reaches into
  if (anchor.core) anchor.core = { x: 0, y: 0 };

  let seed = 7;
  const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const sim: SimNode[] = nodes.map((n) => {
    const k = String(n[o.key]);
    return {
      id: n.id,
      k,
      x: anchor[k].x + (rnd() - 0.5) * 320,
      y: anchor[k].y + (rnd() - 0.5) * 320,
    };
  });
  const byId: Record<string, SimNode> = {};
  sim.forEach((n) => { byId[n.id] = n; });
  const links = edges
    .filter((e) => byId[e.source] && byId[e.target])
    .map((e) => ({ source: e.source, target: e.target }));

  const s = forceSimulation(sim)
    .force("link", forceLink(links).id((d) => (d as SimNode).id).distance(o.link).strength(0.35))
    .force("charge", forceManyBody().strength(o.charge).distanceMax(1100))
    .force("x", forceX<SimNode>((d) => anchor[d.k].x).strength(o.cluster))
    .force("y", forceY<SimNode>((d) => anchor[d.k].y).strength(o.cluster))
    .force("collide", forceCollide(52).iterations(2))
    .stop();
  for (let i = 0; i < o.ticks; i++) s.tick();

  const out: PosMap = {};
  // centre node box on the simulated point so glyphs sit where the sim put them
  sim.forEach((n) => { out[n.id] = { x: (n.x ?? 0) - NODE_W / 2, y: (n.y ?? 0) - NODE_H / 2 }; });
  return out;
}

export interface DagreOut {
  pos: PosMap;
  routes: Record<string, Point[]>;
}

// ── focused: dagre, left-to-right layered DAG ───────────
// Returns positions AND dagre's own edge routes, which already bend around
// nodes — drawing straight edges instead is what puts lines under cards.
export function dagreLayout(nodes: GraphNode[], edges: GraphEdge[], opts?: { ranksep?: number; nodesep?: number; edgesep?: number; pad?: number }): DagreOut {
  const o = { ranksep: 170, nodesep: 70, edgesep: 34, pad: 16, ...opts };
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", ranksep: o.ranksep, nodesep: o.nodesep, edgesep: o.edgesep, marginx: 40, marginy: 40 });
  g.setDefaultEdgeLabel(() => ({}));
  const ids = new Set(nodes.map((n) => n.id));
  // lay out against padded boxes so dagre routes edges through a moat around
  // each card, then draw the cards at their true size inside it
  nodes.forEach((n) => g.setNode(n.id, { width: NODE_W + o.pad * 2, height: NODE_H + o.pad * 2 }));
  const used: GraphEdge[] = [];
  edges.forEach((e) => {
    if (ids.has(e.source) && ids.has(e.target)) { g.setEdge(e.source, e.target); used.push(e); }
  });
  dagre.layout(g);
  const pos: PosMap = {};
  nodes.forEach((n) => {
    const p = g.node(n.id);
    pos[n.id] = { x: p.x - NODE_W / 2, y: p.y - NODE_H / 2 };
  });
  const routes: Record<string, Point[]> = {};
  used.forEach((e) => {
    const ed = g.edge(e.source, e.target) as { points?: Point[] } | undefined;
    if (ed && ed.points) routes[e.source + ">" + e.target] = ed.points;
  });
  return { pos, routes };
}

// ── neighbourhood extraction ────────────────────────────
export function neighbourhood(rootId: string, edges: GraphEdge[], hops: number): Set<string> {
  const up: Record<string, string[]> = {}, down: Record<string, string[]> = {};
  edges.forEach((e) => {
    (down[e.source] = down[e.source] || []).push(e.target);
    (up[e.target] = up[e.target] || []).push(e.source);
  });
  const keep = new Set<string>([rootId]);
  let frontier = [rootId];
  for (let h = 0; h < hops; h++) {
    const next: string[] = [];
    frontier.forEach((id) => {
      (up[id] || []).concat(down[id] || []).forEach((n) => {
        if (!keep.has(n)) { keep.add(n); next.push(n); }
      });
    });
    frontier = next;
  }
  return keep;
}

export function traverse(rootId: string, edges: GraphEdge[], dir: "up" | "down"): Set<string> {
  const adj: Record<string, string[]> = {};
  edges.forEach((e) => {
    const a = dir === "down" ? e.source : e.target, b = dir === "down" ? e.target : e.source;
    (adj[a] = adj[a] || []).push(b);
  });
  const seen = new Set<string>();
  const stack = [rootId];
  while (stack.length) {
    const cur = stack.pop() as string;
    (adj[cur] || []).forEach((n) => { if (!seen.has(n)) { seen.add(n); stack.push(n); } });
  }
  return seen;
}

// ── obstacle avoidance ───────────────────────────────
// Dagre reserves a slot per rank but does not guarantee the straight line
// between two slots is clear, and the drawn box size changes with the LOD
// tier. So the final pass runs against the RENDERED rects.
type Rect = { id?: string; x: number; y: number; w: number; h: number };

function segHitsRect(a: Point, b: Point, r: Rect): boolean {
  let t0 = 0, t1 = 1;
  const dx = b.x - a.x, dy = b.y - a.y;
  const p = [-dx, dx, -dy, dy];
  const q = [a.x - r.x, r.x + r.w - a.x, a.y - r.y, r.y + r.h - a.y];
  for (let i = 0; i < 4; i++) {
    if (p[i] === 0) { if (q[i] < 0) return false; }
    else {
      const t = q[i] / p[i];
      if (p[i] < 0) { if (t > t1) return false; if (t > t0) t0 = t; }
      else { if (t < t0) return false; if (t < t1) t1 = t; }
    }
  }
  return true;
}

// Drops interior bends that do not pay for themselves.
function simplify(pts: Point[], cost: (p: Point[]) => number): Point[] {
  let out = pts.slice();
  let best = cost(out);
  let changed = true;
  while (changed && out.length > 4) {
    changed = false;
    for (let i = 2; i < out.length - 2; i++) {
      const trial = out.slice();
      trial.splice(i, 1);
      const c = cost(trial);
      if (c < best) { out = trial; best = c; changed = true; break; }
    }
  }
  return out;
}

// Inserts an over/under jog around any rect a segment cuts through.
export function avoid(pts: Point[], rects: Rect[], margin?: number, maxIter?: number): Point[] {
  const m = margin == null ? 10 : margin;
  const boxes: Rect[] = rects.map((r) => ({ x: r.x - m, y: r.y - m, w: r.w + m * 2, h: r.h + m * 2 }));
  const hits = (list: Point[]) => {
    let s = 0;
    for (let i = 0; i < list.length - 1; i++) {
      for (let j = 0; j < boxes.length; j++) if (segHitsRect(list[i], list[i + 1], boxes[j])) s++;
    }
    return s;
  };
  const len = (list: Point[]) => {
    let s = 0;
    for (let i = 0; i < list.length - 1; i++) s += Math.hypot(list[i + 1].x - list[i].x, list[i + 1].y - list[i].y);
    return s;
  };
  const cost = (list: Point[]) => hits(list) * 6000 + len(list);

  let out = simplify(pts, cost);
  let best = cost(out);
  for (let iter = 0; iter < (maxIter || 8); iter++) {
    if (hits(out) === 0) break;
    let improved = false;
    const cLo = Math.min(out[1].x, out[out.length - 2].x);
    const cHi = Math.max(out[1].x, out[out.length - 2].x);
    for (let i = 0; i < out.length - 1 && !improved; i++) {
      const a = out[i], b = out[i + 1];
      for (let j = 0; j < boxes.length && !improved; j++) {
        const box = boxes[j];
        if (!segHitsRect(a, b, box)) continue;
        const x1 = Math.max(cLo, box.x);
        const x2 = Math.min(cHi, box.x + box.w);
        if (x2 <= x1) continue;
        let pick: Point[] | null = null, pickCost = best;
        [box.y, box.y + box.h].forEach((by) => {
          const trial = out.slice();
          trial.splice(i + 1, 0, { x: x1, y: by }, { x: x2, y: by });
          const c = cost(trial);
          if (c < pickCost) { pickCost = c; pick = trial; }
        });
        if (pick) { out = pick; best = pickCost; improved = true; }
      }
    }
    if (!improved) break;
  }
  return simplify(out, cost);
}
