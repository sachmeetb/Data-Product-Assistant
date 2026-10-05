// Estate Graph — one graph, two layouts, semantic zoom. Ported from the
// "Lineage Merge" design (merge-app.jsx) to @xyflow/react v12 + TS, running on
// the live modernization inventory. Merges the estate overview (force layout)
// with the drill-in lineage DAG (dagre), disposition colour, and a proposed-
// change overlay (retire / add). Live force-sim, ELK routing, snap-to-structure
// and the tweaks panel from the original prototype are intentionally omitted.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  Background,
  BackgroundVariant,
  MiniMap,
  MarkerType,
  type Edge,
  type NodeChange,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import "./estate-graph.css";
import type { InventoryResponse } from "../../types";
import { inventoryToEstate, type EstateModel, type ChangeKind, type Tier } from "./estateModel";
import {
  forceLayout, dagreLayout, traverse, avoid,
  NODE_W, NODE_H, type PosMap, type Point, type GraphEdge,
} from "./estateLayout";
import { elkLayout, type ElkOut } from "./estateElk";
import {
  ObjectNode, RoutedEdge, useAnimatedNodes, useTier, TIER_SIZE,
  type EstateFlowNode,
} from "./EstateNode";
import { TopBar, Legend, ZoomLadder, Detail } from "./EstateChrome";
import { nodesSimilarity, fetchColumnSimilarity } from "../../lib/similarity";
import SimilarityRadar from "../SimilarityRadar";
import ExtLinkIcon from "../ExtLinkIcon";

// Folded-in constants (the prototype's tweak defaults).
const TRANSITION_MS = 460;
// LOD thresholds: glyph when zoom < GLYPH_MAX, chip when < CHIP_MAX, else card.
// GLYPH_MAX is raised so glyphs "come up" earlier when zooming out (glyph tier
// covers more of the range) instead of holding chips down to a low zoom.
const GLYPH_MAX = 0.7;
const CHIP_MAX = 1.0;
const HOPS = 2;
const CLUSTER_STRENGTH = 0.22;
// Overview cluster picker: "none" = pure force layout (nothing pulled into
// attribute rings); otherwise nodes are drawn toward per-value anchors.
export type ClusterKey = "none" | "domain" | "system" | "type";
const GLYPH_LABELS = true;
const GLYPH_GLOW = 8;
const ROUTE_EDGES = true;
const CORNER_RADIUS = 18;
const SHOW_MINIMAP = true;

const nodeTypes = { obj: ObjectNode };
const edgeTypes = { routed: RoutedEdge };
// Hue is reserved for change state. Everything else is neutral.
const EDGE_C: Record<string, string> = {
  none: "var(--eg-edge)",
  retire: "var(--eg-c-retire)",
  add: "var(--eg-c-add)",
};
// Live what-if palette: the new wiring is BLACK (pops), the surrounding context
// fades to a light gray while a preview is active so the change stands out.
const PREVIEW_ADD_EDGE = "#1e293b";
const CONTEXT_FADE_EDGE = "#cbd5e1";
// Amber for a migrate what-if relationship the plan flagged as a potential
// problem (must be migrated / repointed before cutover) — matches the DAG.
const CAUTION_EDGE = "#f59e0b";

interface GraphProps {
  model: EstateModel;
  containerRef: React.RefObject<HTMLDivElement | null>;
  onOpenRow?: (name: string) => void;
  onPlan?: (ids: string[]) => void;
  onSelectionChange?: (ids: string[]) => void;
  focusIds?: string[];
  focusNonce?: number;
  previewAddIds?: string[];
  previewRetireIds?: string[];
  previewCautionIds?: string[];
  previewCautionDetails?: Record<string, string>;
  projectId?: number | string;
}

function Graph({ model, containerRef, onOpenRow, onPlan, onSelectionChange, focusIds, focusNonce, previewAddIds, previewRetireIds, previewCautionIds, previewCautionDetails, projectId }: GraphProps) {
  const { fitView, setCenter } = useReactFlow();
  const { zoom, tier: autoTier } = useTier(GLYPH_MAX, CHIP_MAX);
  // Level-of-detail: null = auto (driven by zoom); otherwise the user pinned a
  // tier from the ladder (show only glyphs / chips / cards regardless of zoom).
  const [tierOverride, setTierOverride] = useState<Tier | null>(null);
  const tier = tierOverride ?? autoTier;

  const [mode, setMode] = useState<"overview" | "focused">("overview");
  // Focus roots: one on a normal drill-in, several when the disposition table
  // sends a multi-select. rootId (the first) is the camera target + crumb.
  const [roots, setRoots] = useState<string[]>([]);
  const rootId = roots[0] ?? null;
  // traceAll: highlight the COMBINED lineage of every root (a table multi-select
  // pull). Cleared once the user clicks a single node to trace just that one.
  const [traceAll, setTraceAll] = useState(false);
  // Plan selection: the node(s) the "Recommendation planning" button acts on.
  // Click toggles a node in/out; the bottom analysis panel tracks this live.
  const [planSet, setPlanSet] = useState<Set<string>>(new Set());
  // Live mirror of planSet so selectOne can compute a toggle + keep selId in sync
  // in one gesture (without nesting setState calls).
  const planSetRef = useRef(planSet);
  useEffect(() => { planSetRef.current = planSet; });
  // Lift the selection to the parent (drives the live analysis panel). Only
  // fires when the set actually changes, so an inline callback can't loop.
  const lastSentRef = useRef("");
  useEffect(() => {
    const ids = Array.from(planSet);
    const key = ids.join(",");
    if (key !== lastSentRef.current) { lastSentRef.current = key; onSelectionChange?.(ids); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [planSet]);
  const [selId, setSelId] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [fSys, setFSys] = useState("all");
  const [fType, setFType] = useState("all");
  const [fDom, setFDom] = useState("all");
  // "Show filtered objects": unchecked → the filters CULL (only matches on
  // stage); checked → show the whole estate with the matches highlighted and the
  // rest dimmed back. Defaults ON so filtering never silently removes estate
  // context — non-matching nodes dim rather than disappear, which keeps the
  // surrounding lineage readable while a filter is applied.
  const [showAll, setShowAll] = useState(true);
  const preview = false; // legacy "proposed changes" overlay — retired (checkbox repurposed)
  const [moved, setMoved] = useState<PosMap>({});
  const [dragging, setDragging] = useState(false);
  const [frameNonce, setFrameNonce] = useState(0);
  const dragFrom = useRef<{ id: string; x: number; y: number } | null>(null);

  const all = model.nodes;
  const allEdges = model.edges as GraphEdge[];
  const byId = useMemo(() => { const m: Record<string, typeof all[number]> = {}; all.forEach((n) => { m[n.id] = n; }); return m; }, [all]);

  // Ids the host has flagged as a live what-if preview, spanning every
  // recommendation-planning type: additions (new product / migrated node) read
  // as NEW; retirements (consolidated sources / legacy node / retire target)
  // read as RETIRING — both independent of the "Show proposed changes" toggle,
  // the same what-if the DAG showed. Declared here (before visIds) so the
  // focused-lineage closure can seed itself with the preview nodes.
  const previewAddSet = useMemo(() => new Set(previewAddIds || []), [previewAddIds]);
  const previewRetireSet = useMemo(() => new Set(previewRetireIds || []), [previewRetireIds]);
  const previewCautionSet = useMemo(() => new Set(previewCautionIds || []), [previewCautionIds]);

  // ── layout A: force, whole estate ──
  // Default clusters by domain — a pure force layout on 200+ nodes reads as one
  // undifferentiated hairball, whereas domain grouping gives the estate an
  // immediate structure to navigate. "No clustering" is still one pick away.
  const [clusterBy, setClusterBy] = useState<ClusterKey>("domain");
  const forcePos = useMemo(
    () => forceLayout(all, allEdges, clusterBy === "none"
      ? { cluster: 0 }
      : { cluster: CLUSTER_STRENGTH, key: clusterBy }),
    [all, allEdges, clusterBy]
  );
  // Changing the layout wholesale: drop manual repositions + refit the camera.
  const pickCluster = useCallback((k: ClusterKey) => {
    setClusterBy(k);
    setMoved({});
    if (mode === "overview") setFrameNonce((n) => n + 1);
  }, [mode]);

  const matches = useCallback((n: typeof all[number]) => {
    if (fSys !== "all" && n.system !== fSys) return false;
    if (fType !== "all" && n.type !== fType) return false;
    if (fDom !== "all" && n.domain !== fDom) return false;
    if (q && !n.name.toLowerCase().includes(q.toLowerCase())) return false;
    return true;
  }, [fSys, fType, fDom, q]);

  // Structural (platform / type / domain) match only — no search text. Drives the
  // focused-view 1-hop cluster pull-in so the SEARCH box only ever narrows WITHIN
  // the current subgraph (it never reaches out and drags estate-wide name matches
  // onto a drilled-in view).
  const filterMatch = useCallback((n: typeof all[number]) => {
    if (fSys !== "all" && n.system !== fSys) return false;
    if (fType !== "all" && n.type !== fType) return false;
    if (fDom !== "all" && n.domain !== fDom) return false;
    return true;
  }, [fSys, fType, fDom]);
  const hasStructuralFilter = fSys !== "all" || fType !== "all" || fDom !== "all";
  // A live text search casts a "spotlight": everything that isn't a match (or
  // part of the current selection) is veiled behind a scrim so the matches read
  // on top. Distinct from the structural filters, which cull / cluster instead.
  const searching = !!q.trim();

  const filtering = !!q || fSys !== "all" || fType !== "all" || fDom !== "all";

  // ── which nodes are on stage ──
  const visIds = useMemo(() => {
    if (mode === "focused" && roots.length) {
      // FULL up/down lineage closure of each root (like the DAG) — not just a
      // 2-hop window — so the whole chain back to the source databases is on
      // stage, with every hop's edges. Live what-if nodes (new product / retiring)
      // are seeded too, so a preview shows its full lineage (its feeders back to
      // the databases and everything it serves), not a truncated slice.
      const seeds = new Set<string>([...roots, ...previewAddSet, ...previewRetireSet]);
      const keep = new Set<string>();
      seeds.forEach((r) => {
        keep.add(r);
        traverse(r, allEdges, "up").forEach((id) => keep.add(id));
        traverse(r, allEdges, "down").forEach((id) => keep.add(id));
      });
      // Pull in the platform/database hub for any table on stage (the DAG shows
      // these; they sit one hop beyond the tables via the database→table edge).
      allEdges.forEach((e) => {
        if (keep.has(e.target) && byId[e.source]?.type === "database") keep.add(e.source);
      });
      // With a platform/type/domain filter active, also pull in filter-MATCHING
      // objects that sit ONE hop off the focused lineage — surfaces whether the
      // filtered set clusters around / overlaps this object's lineage. NOTE: this
      // uses filterMatch (NOT the search text) — a search on a drilled-in view
      // only narrows what's already there, it never drags in outside nodes.
      if (hasStructuralFilter) {
        const closure = new Set(keep);
        allEdges.forEach((e) => {
          if (closure.has(e.source) && !closure.has(e.target) && byId[e.target] && filterMatch(byId[e.target])) keep.add(e.target);
          else if (closure.has(e.target) && !closure.has(e.source) && byId[e.source] && filterMatch(byId[e.source])) keep.add(e.source);
        });
      }
      return keep;
    }
    // Overview: the platform/type/domain/search selectors hard-filter the estate
    // down to the matching subset — UNLESS "Show filtered objects" (showAll) is
    // on, in which case the whole estate stays on stage and the matches are just
    // highlighted (dim pass in targetNodes/edges).
    if (filtering && !showAll) return new Set(all.filter(matches).map((n) => n.id));
    return new Set(all.map((n) => n.id));
  }, [mode, roots, allEdges, all, filtering, showAll, matches, filterMatch, hasStructuralFilter, byId, previewAddSet, previewRetireSet]);

  // The filter-matching set (whenever a filter is active). Used to highlight
  // matches vs dim the rest — in overview when "Show filtered objects" is on, and
  // in a drilled view to spot filter-matches that cluster around the lineage.
  const matchSet = useMemo(
    () => (filtering ? new Set(all.filter(matches).map((n) => n.id)) : null),
    [filtering, all, matches]
  );
  // Highlight the matches (vs dim the rest) only when we're deliberately showing
  // more than the pure filtered subset: the overview show-all toggle, or a drill.
  const highlightMatches = filtering && (showAll || mode === "focused");

  const visNodes = useMemo(() => all.filter((n) => visIds.has(n.id)), [all, visIds]);

  // ── layout B: layered DAG over the focused subgraph ──
  // dagre is synchronous so it paints immediately; ELK is async and better (it
  // owns clearance, port order and orthogonal bend points) so it upgrades in
  // place — this is what makes the focused edges route at clean right angles.
  const dagreOut = useMemo(
    () => (mode === "focused" ? dagreLayout(visNodes, allEdges) : null),
    [mode, visNodes, allEdges]
  );

  const viewKey = mode + ":" + roots.join(",") + ":" + HOPS;
  const layoutKey = mode === "focused" ? viewKey + ":" + visNodes.length : "";
  const [elkOut, setElkOut] = useState<(ElkOut & { key: string }) | null>(null);
  const elkFramedRef = useRef("");
  useEffect(() => {
    if (mode !== "focused") { setElkOut(null); return; }
    let alive = true;
    elkLayout(visNodes, allEdges).then((o) => {
      if (!alive) return;
      setElkOut(Object.assign({ key: layoutKey }, o));
      // Re-frame only for a genuinely new view (toggling preview re-runs layout
      // but must never take the user's pan/zoom away).
      if (elkFramedRef.current !== viewKey) {
        elkFramedRef.current = viewKey;
        setFrameNonce((n) => n + 1);
      }
    }).catch((err) => {
      if (!alive) return;
      console.warn("ELK layout failed, staying on dagre:", err);
      setElkOut(null);
    });
    return () => { alive = false; };
  }, [mode, visNodes, allEdges, layoutKey, viewKey]);

  const useElk = !!(elkOut && elkOut.key === layoutKey);
  const routerOut = useElk ? elkOut : dagreOut;
  const dagrePos = routerOut ? routerOut.pos : null;

  const basePos = mode === "focused" ? (dagrePos as PosMap) : forcePos;
  const pos = useMemo(() => {
    if (!Object.keys(moved).length) return basePos;
    return Object.assign({}, basePos, moved);
  }, [basePos, moved]);

  // ── highlight: lineage of the selected node(s) ──
  // Single click traces one node; a table multi-select traces the COMBINED
  // up/down closure of every pulled-in root.
  const lineage = useMemo(() => {
    const sources = traceAll ? roots : planSet.size ? [...planSet] : selId ? [selId] : [];
    if (!sources.length) return null;
    const up = new Set<string>(), down = new Set<string>();
    sources.forEach((s) => {
      traverse(s, allEdges, "up").forEach((x) => up.add(x));
      traverse(s, allEdges, "down").forEach((x) => down.add(x));
    });
    return { up, down };
  }, [traceAll, roots, planSet, selId, allEdges]);

  const tierOff = useMemo(() => {
    const [tw, th] = TIER_SIZE[tier];
    return { ox: (NODE_W - tw) / 2, oy: (NODE_H - th) / 2 };
  }, [tier]);


  // proposed-change kind for a node: live preview add/retire always wins;
  // everything else only lights up when the preview toggle is on.
  const nodeChange = useCallback((n: typeof all[number]): ChangeKind => {
    if (previewAddSet.has(n.id)) return "add";
    if (previewRetireSet.has(n.id)) return "retire";
    if (!preview) return null;
    if (n.isProduct) return "add";
    if (n.disposition === "retire") return "retire";
    return null;
  }, [preview, previewAddSet, previewRetireSet]);

  const targetNodes: EstateFlowNode[] = useMemo(() => {
    const [tw, th] = TIER_SIZE[tier];
    const ox = tierOff.ox, oy = tierOff.oy;
    return visNodes.map((n) => {
      // Filters cull the visible set (see visIds), so every node on stage is a
      // match — no dim/hit pass needed here; only lineage tracing dims.
      // A live what-if node (new product / retiring) is the FOCUS — never dim it,
      // even when it falls outside the selected sources' lineage closure.
      const isLivePreviewNode = previewAddSet.has(n.id) || previewRetireSet.has(n.id);
      const isMatch = highlightMatches && !!matchSet && matchSet.has(n.id);
      // Priority: live-preview → selection glow → filter match → lineage → dim.
      let hl = "normal";
      if (isLivePreviewNode) {
        hl = "normal";
      } else if (planSet.has(n.id) || n.id === selId) {
        // "sel" (the glow) follows the actual SELECTION (planSet / selId), not the
        // focus roots which persist across clicks.
        hl = "sel";
      } else if (isMatch) {
        // Filter match pops out — in the show-all overview AND in a drill (so you
        // can spot filter objects clustering around the lineage).
        hl = "hit";
      } else if (searching && highlightMatches) {
        // Search spotlight: anything that isn't a match or the current selection
        // is veiled — even lineage nodes — so only what meets the search criteria
        // (plus what you already had selected) reads on top. Gated on
        // highlightMatches so it never fires in the cull overview, where every
        // on-stage node is already a match.
        hl = "dim";
      } else if (lineage) {
        hl = lineage.up.has(n.id) ? "up" : lineage.down.has(n.id) ? "down"
          : mode === "focused" ? "ctx" : "dim";
      } else if (highlightMatches) {
        hl = "dim"; // show-all overview: non-matches recede
      }
      // "+N" badge: for a SELECTED node, how many of its directly-connected
      // objects are currently hidden — i.e. "there's more lineage, drill to see
      // it". Also computed while drilled in: the root's own connections are all
      // on stage (0, no badge), but a filter-match selected OFF the root's
      // lineage still advertises what a re-drill onto it would reveal.
      let expandCount = 0;
      if (planSet.has(n.id) || n.id === selId) {
        const hidden = new Set<string>();
        allEdges.forEach((e) => {
          const other = e.source === n.id ? e.target : (e.target === n.id ? e.source : null);
          if (other && !visIds.has(other)) hidden.add(other);
        });
        expandCount = hidden.size;
      }
      const p = pos[n.id] || { x: 0, y: 0 };
      return {
        id: n.id,
        type: "obj" as const,
        position: { x: p.x + ox, y: p.y + oy },
        width: tw, height: th,
        draggable: true,
        selected: n.id === selId,
        data: {
          node: n, tier, hl, w: tw, h: th,
          change: nodeChange(n),
          livePreview: previewAddSet.has(n.id) || previewRetireSet.has(n.id),
          isRoot: planSet.has(n.id) || n.id === selId,
          expandCount,
          glyphLabels: GLYPH_LABELS,
          glow: GLYPH_GLOW,
          glyphSize: 22,
          systems: model.systems,
          types: model.types,
        },
      };
    });
  }, [visNodes, pos, tier, tierOff, lineage, selId, planSet, mode, nodeChange, model.systems, model.types, previewAddSet, previewRetireSet, matchSet, highlightMatches, searching, allEdges, visIds]);

  const nodes = useAnimatedNodes(targetNodes, dragging ? 0 : TRANSITION_MS);

  const edges: Edge[] = useMemo(() => {
    const vis = allEdges.filter((e) => visIds.has(e.source) && visIds.has(e.target));
    // While a live what-if is on stage, everything that isn't part of the change
    // fades to gray so the new (black) / retiring (red) wiring stands out.
    const previewActive = previewAddSet.size > 0 || previewRetireSet.size > 0;
    const [, th] = TIER_SIZE[tier];
    const sPort = new Map<string, number>(), tPort = new Map<string, number>();
    const group = (keyFn: (e: GraphEdge) => string, otherFn: (e: GraphEdge) => string, out: Map<string, number>) => {
      const g: Record<string, GraphEdge[]> = {};
      vis.forEach((e) => { (g[keyFn(e)] = g[keyFn(e)] || []).push(e); });
      Object.keys(g).forEach((k) => {
        const list = g[k].slice().sort((a, b) => ((pos[otherFn(a)] || {}).y || 0) - ((pos[otherFn(b)] || {}).y || 0));
        const nn = list.length;
        const span = Math.max(0, th * 0.55);
        const stepY = nn > 1 ? Math.min(9, span / (nn - 1)) : 0;
        list.forEach((e, i) => out.set(e.source + ">" + e.target, (i - (nn - 1) / 2) * stepY));
      });
    };
    group((e) => e.source, (e) => e.target, sPort);
    group((e) => e.target, (e) => e.source, tPort);

    const [tw2] = TIER_SIZE[tier];
    const rect: Record<string, Point & { w: number; h: number }> = {};
    visNodes.forEach((n) => {
      const p = pos[n.id] || { x: 0, y: 0 };
      rect[n.id] = { x: p.x + tierOff.ox, y: p.y + tierOff.oy, w: tw2, h: th };
    });
    const rectList = visNodes.map((n) => Object.assign({ id: n.id }, rect[n.id]));

    return vis.map((e) => {
      let state = "normal";
      if (searching && highlightMatches) {
        // Search spotlight: veil every edge except those between two lit endpoints
        // (a match or the current selection) — matching the node veil so only what
        // meets the search reads on top of the dimmed lineage.
        const lit = (id: string) => id === selId || planSet.has(id) || (!!matchSet && matchSet.has(id));
        state = lit(e.source) && lit(e.target) ? "normal" : "dim";
      } else if (lineage) {
        // a selected/root node is a lit endpoint (traverse() excludes the seed
        // node itself, so every selected node must be added explicitly or its
        // own edges dim — this is what makes EACH selected node's up/downstream
        // relationship draw, not just the last-clicked one)
        // A filter-match sitting 1 hop off the lineage counts as lit, so its
        // connecting edge is drawn (that's the cluster relationship to see).
        const lit = (id: string) => id === selId || planSet.has(id) || lineage.up.has(id) || lineage.down.has(id) || (highlightMatches && !!matchSet && matchSet.has(id));
        state = lit(e.source) && lit(e.target) ? "trace" : "dim";
      } else if (matchSet && highlightMatches) {
        // Show-filtered-objects mode: only edges between two matches stay lit.
        state = matchSet.has(e.source) && matchSet.has(e.target) ? "normal" : "dim";
      }
      // Live what-if edge styling: the new wiring into/out of an ADDED node is
      // green and animated (moving dash); the OLD wiring touching a RETIRING node
      // is red, faded. Add wins when an edge touches both. Outside a live
      // preview, the "Show proposed changes" toggle drives the hue.
      const isPreviewEdge = previewAddSet.has(e.source) || previewAddSet.has(e.target);
      const isRetireEdge = !isPreviewEdge && (previewRetireSet.has(e.source) || previewRetireSet.has(e.target));
      let ch: ChangeKind = null;
      if (!isPreviewEdge && !isRetireEdge && preview) {
        const s = byId[e.source], t = byId[e.target];
        if ((s && s.disposition === "retire") || (t && t.disposition === "retire")) ch = "retire";
        else if (e.isProductEdge) ch = "add";
      }
      // A new-node (migrate/product) edge whose other endpoint the plan flagged
      // is a CAUTION relationship — amber + ⚠, matching the DAG.
      const isCautionEdge = isPreviewEdge && (previewCautionSet.has(e.source) || previewCautionSet.has(e.target));
      // The flagged endpoint drives the ⚠ tooltip (the model's reason for it).
      const cautionNodeId = isCautionEdge ? (previewCautionSet.has(e.source) ? e.source : e.target) : null;
      const cautionDetail = cautionNodeId ? previewCautionDetails?.[cautionNodeId] : undefined;
      // Context edges (not part of the change) fade to gray during a preview.
      const isContextFade = previewActive && !isPreviewEdge && !isRetireEdge && !ch;
      const stroke = isCautionEdge ? CAUTION_EDGE
        : isPreviewEdge ? PREVIEW_ADD_EDGE
        : isRetireEdge ? EDGE_C.retire
        : ch ? EDGE_C[ch]
        : isContextFade ? CONTEXT_FADE_EDGE
        : EDGE_C.none;
      const id = e.source + ">" + e.target;
      const route = mode === "focused" && ROUTE_EDGES && routerOut ? routerOut.routes[id] : null;

      // Build the whole polyline here, in rendered-tier coordinates. ELK
      // already routed orthogonally around every card, so its bends are used
      // verbatim — only the endpoints are re-anchored to the current tier's
      // side (the first and last segments are horizontal, so moving them along
      // x preserves the routing). Dagre needs the local avoid() repair pass.
      let path: Point[] | null = null;
      if (route && rect[e.source] && rect[e.target]) {
        const rs = rect[e.source], rt = rect[e.target];
        // ELK's verbatim route is only valid while both endpoints still sit
        // where ELK placed them: the endpoint re-anchor slides along x on a
        // horizontal first/last segment, so a node dragged VERTICALLY leaves
        // the line at its old height. Once a route endpoint no longer falls
        // inside its node's current rect, drop to the local repair pass below
        // (endpoints on live centers + clamped bends + avoid()).
        const elkAnchorsValid =
          route.length >= 2 &&
          route[0].y >= rs.y - 1 && route[0].y <= rs.y + rs.h + 1 &&
          route[route.length - 1].y >= rt.y - 1 && route[route.length - 1].y <= rt.y + rt.h + 1;
        if (useElk && elkAnchorsValid) {
          const p0 = route[0], pl = route[route.length - 1];
          path = [{ x: rs.x + rs.w, y: p0.y }]
            .concat(route.slice(1, -1), [{ x: rt.x, y: pl.y }]);
        } else {
          const sx = rs.x + rs.w, sy = rs.y + rs.h / 2 + (sPort.get(id) || 0);
          const tx = rt.x, ty2 = rt.y + rt.h / 2 + (tPort.get(id) || 0);
          if (tx > sx + 12) {
            const stub = Math.max(6, Math.min(26, (tx - sx) / 3));
            const x0 = sx + stub, x1 = tx - stub;
            const inner: Point[] = [];
            route.slice(1, -1).forEach((p) => {
              const x = Math.min(Math.max(p.x, x0), x1);
              const last = inner[inner.length - 1];
              if (last && Math.abs(last.x - x) < 2 && Math.abs(last.y - p.y) < 2) return;
              inner.push({ x, y: p.y });
            });
            const obstacles = rectList.filter((r) => r.id !== e.source && r.id !== e.target);
            path = avoid(
              [{ x: sx, y: sy }, { x: x0, y: sy }].concat(inner, [{ x: x1, y: ty2 }, { x: tx, y: ty2 }]),
              obstacles, CORNER_RADIUS + 6, 12
            );
          }
        }
      }
      return {
        id,
        source: e.source, target: e.target,
        type: route ? "routed" : mode === "focused" ? "smoothstep" : "bezier",
        data: route ? { points: route, path, radius: CORNER_RADIUS, sOff: sPort.get(id) || 0, tOff: tPort.get(id) || 0, caution: isCautionEdge, cautionDetail } : (isCautionEdge ? { caution: true, cautionDetail } : undefined),
        // Retiring node's links are SEVERED (DAG-style cut): red dashed, no
        // arrowhead — so the downstream reads as disconnected, not still-fed.
        markerEnd: isRetireEdge ? undefined : { type: MarkerType.ArrowClosed, width: 14, height: 14, color: stroke },
        // New wiring gets the moving-dash class (custom routed edges don't honour
        // RF's `animated`, so a CSS keyframe on the dashoffset drives the motion).
        className: isPreviewEdge ? "eg-live-edge" : undefined,
        style: {
          stroke,
          strokeWidth: isPreviewEdge ? 2 : isRetireEdge ? 1.5 : isContextFade ? 1 : state === "trace" || ch ? 1.6 : state === "dim" ? 0.7 : filtering ? 1.4 : 1,
          strokeDasharray: isPreviewEdge ? "6 5" : isRetireEdge ? "5 4" : ch === "add" ? "7 5" : undefined,
          // A filter thins the estate down to a sparse set over lots of whitespace,
          // so the normally-faint overview edges (opacity 0.35 on the full hairball)
          // become effectively invisible — every surviving relationship should read
          // clearly. Darken them once a filter is active; the full-estate view stays
          // calm.
          opacity: isPreviewEdge ? 1 : isRetireEdge ? 0.6 : isContextFade ? 0.22 : state === "dim" ? 0.1 : ch ? 0.95 : mode === "focused" ? 0.7 : filtering ? 0.68 : 0.35,
        },
        animated: isPreviewEdge,
      } as Edge;
    });
  }, [allEdges, visIds, visNodes, lineage, selId, planSet, mode, tier, tierOff, pos, preview, byId, routerOut, useElk, previewAddSet, previewRetireSet, previewCautionSet, previewCautionDetails, matchSet, highlightMatches, filtering, searching]);

  // controlled flow: RF reports drag as position changes; store canonically.
  const onNodesChange = useCallback((changes: NodeChange[]) => {
    const next: PosMap = {};
    let any = false;
    changes.forEach((c) => {
      if (c.type === "position" && c.position) {
        next[c.id] = { x: c.position.x - tierOff.ox, y: c.position.y - tierOff.oy };
        any = true;
      }
    });
    if (any) setMoved((m) => Object.assign({}, m, next));
  }, [tierOff]);

  const onNodeDragStart = useCallback((_e: React.MouseEvent, n: EstateFlowNode) => {
    setDragging(true);
    dragFrom.current = { id: n.id, x: n.position.x, y: n.position.y };
  }, []);

  const onNodeDragStop = useCallback((_e: React.MouseEvent, n: EstateFlowNode) => {
    setDragging(false);
    const f = dragFrom.current;
    dragFrom.current = null;
    // a click is a zero-distance drag — never treat it as a reposition
    const dist = f && f.id === n.id ? Math.hypot(n.position.x - f.x, n.position.y - f.y) : 0;
    if (dist < 3) {
      setMoved((m) => {
        if (!(n.id in m)) return m;
        const nextM = Object.assign({}, m);
        delete nextM[n.id];
        return nextM;
      });
    }
  }, []);

  // ── drill in / out ──
  // Drilling in from a multi-select keeps the WHOLE selection: every selected
  // node stays a root (its lineage stays on stage, combined-traced), while the
  // double-clicked node leads — camera target + crumb. A single-select drill
  // behaves as before.
  // Drilling into a node's lineage clears the estate filters: the whole point of
  // the drill is the object's FULL lineage closure, so a lingering platform/type/
  // domain/search filter would only cull that lineage back down. Reset to a clean
  // overview filter state on the way in.
  const clearFilters = useCallback(() => {
    setQ(""); setFSys("all"); setFType("all"); setFDom("all");
  }, []);

  const focusOn = useCallback((id: string) => {
    setMoved({});
    clearFilters();
    const sel = new Set(planSetRef.current);
    sel.add(id);
    setRoots([id, ...Array.from(sel).filter((x) => x !== id)]);
    setSelId(id); setTraceAll(sel.size > 1); setPlanSet(sel); setMode("focused");
    setFrameNonce((n) => n + 1);
  }, [clearFilters]);

  // multi-root drill (the disposition table's "View in graph" jump): every
  // pulled-in object is selected and their combined lineage is traced.
  const focusMany = useCallback((ids: string[]) => {
    if (!ids.length) return;
    setMoved({});
    clearFilters();
    // The pulled-in objects ARE the selection (drives the glow) — seed planSet so
    // clicking off / picking another node clears them like any other selection.
    setRoots(ids); setSelId(ids[0]); setPlanSet(new Set(ids)); setTraceAll(ids.length > 1); setMode("focused");
    setFrameNonce((n) => n + 1);
  }, [clearFilters]);

  const backToEstate = useCallback(() => {
    setMoved({});
    // Selection survives the exit — returning to the (filtered) overview keeps
    // the same nodes selected, their "+N" expand badges included. Deselecting
    // is an explicit gesture (click the node again / click empty space).
    setMode("overview"); setRoots([]); setTraceAll(false);
    setFrameNonce((n) => n + 1);
  }, []);

  // Click a node to toggle it in/out of the selection (click again to deselect),
  // matching the DAG Tree — no modifier key needed. Accumulate as many as you
  // want; the "same recommendation" grouping is applied when the panel analyses
  // them. selId (detail rail) tracks the last-clicked node.
  const selectOne = useCallback((id: string) => {
    setTraceAll(false);
    const has = planSetRef.current.has(id);
    const next = new Set(planSetRef.current);
    if (has) next.delete(id); else next.add(id);
    setPlanSet(next);
    // Toggling a node OFF must also drop it from the detail rail: clear selId (or
    // move it to a remaining member) instead of re-pinning the just-removed node.
    setSelId(has ? (next.size ? Array.from(next)[next.size - 1] : null) : id);
  }, []);

  // Distinguish a genuine double-click (fast, back-to-back on the same node →
  // drill in) from a second click a beat later (→ toggle/deselect).
  // Match the OS double-click window (~500ms) — 260 was tight enough that real
  // double-clicks fell through and silently did nothing (toggle-toggle, no drill).
  const DBLCLICK_MS = 500;
  const lastClickRef = useRef<{ id: string; t: number; prevT: number }>({ id: "", t: 0, prevT: 0 });
  const handleNodeClick = useCallback((id: string) => {
    const now = performance.now();
    const last = lastClickRef.current;
    lastClickRef.current = { id, t: now, prevT: last.id === id ? last.t : 0 };
    selectOne(id);
  }, [selectOne]);
  const handleNodeDoubleClick = useCallback((id: string) => {
    const last = lastClickRef.current;
    const gap = last.id === id && last.prevT ? last.t - last.prevT : Infinity;
    lastClickRef.current = { id: "", t: 0, prevT: 0 };
    if (gap < DBLCLICK_MS) focusOn(id);
  }, [focusOn]);

  // External focus request from the disposition table (id list + bump nonce).
  // Runs on mount (tab-switch mounts this fresh) and on each new request.
  const focusKey = (focusIds || []).join(",");
  useEffect(() => {
    if (focusIds && focusIds.length) focusMany(focusIds);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusNonce, focusKey]);

  // Re-frame the camera onto the narrowed subset whenever the overview filters
  // change (skip the initial mount so we don't fight the entry fitView / focus).
  const filterKey = `${q}|${fSys}|${fType}|${fDom}`;
  const filterFitRef = useRef(filterKey);
  useEffect(() => {
    if (filterFitRef.current === filterKey) return;
    filterFitRef.current = filterKey;
    if (mode === "overview") setFrameNonce((n) => n + 1);
  }, [filterKey, mode]);

  // keep node content at constant screen size: CSS counter-scales by 1/zoom.
  // Quantize to 2 decimals and skip no-op writes — otherwise every sub-pixel
  // zoom step restyles all ~200 counter-scaled nodes, which is the glyph view's
  // dominant zoom cost. 2-decimal steps are visually indistinguishable.
  const zWriteRef = useRef<{ z: number; far: string }>({ z: -1, far: "" });
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const qz = Math.round(zoom * 100) / 100;
    // "far" hides glyph labels only when zoomed out past the usable range. The
    // whole-estate overview rests around 0.17×, so keep the cutoff below that
    // (near minZoom) — otherwise labels stay hidden at the resting magnification
    // and it's unclear when glyphs actually become legible.
    const far = zoom < 0.13 ? "1" : "0";
    const last = zWriteRef.current;
    if (qz !== last.z) { el.style.setProperty("--z", String(qz)); last.z = qz; }
    if (far !== last.far) { el.dataset.far = far; last.far = far; }
  }, [zoom, containerRef]);

  // re-frame ONLY on an explicit view action (drill in / out / re-centre).
  // pos is read through a ref so live tweaks never move the camera; the ref is
  // synced in an effect (never mutated during render).
  const posRef = useRef(pos);
  useEffect(() => { posRef.current = pos; });
  const framedRef = useRef("");
  useEffect(() => {
    const key = mode + ":" + (rootId || "") + ":" + frameNonce;
    if (framedRef.current === key) return;
    framedRef.current = key;
    const to = setTimeout(() => {
      const p = mode === "focused" && rootId && posRef.current && posRef.current[rootId];
      if (p) {
        setCenter(p.x + NODE_W / 2 + 170, p.y + NODE_H / 2, { zoom: 0.95, duration: 420 });
      } else {
        fitView({ duration: 420, padding: 0.06, minZoom: 0.12, maxZoom: 0.6 });
      }
    }, TRANSITION_MS + 60);
    return () => clearTimeout(to);
  }, [mode, rootId, frameNonce, fitView, setCenter]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { if (mode === "focused") backToEstate(); else setSelId(null); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mode, backToEstate]);

  const sel = selId ? byId[selId] : null;
  const upN = lineage ? lineage.up.size : 0, downN = lineage ? lineage.down.size : 0;
  // Recommendation planning is only offered when the whole selection shares one
  // recommendation (disposition) — a mixed set can't be planned together.
  const planSameRec = useMemo(() => {
    if (planSet.size === 0) return true;
    const disps = new Set(Array.from(planSet).map((id) => byId[id]?.disposition ?? "__none__"));
    return disps.size === 1;
  }, [planSet, byId]);
  // The recommendation the button acts on = the selection's shared disposition
  // (or the single focused node's when nothing is multi-selected). Drives the
  // button colour — not selId, which can point at a just-deselected node.
  const planDisp = useMemo(
    () => (planSet.size ? byId[Array.from(planSet)[0]]?.disposition ?? null : sel?.disposition ?? null),
    [planSet, byId, sel]
  );

  const simPicks = useMemo(() =>
    [...planSet]
      .map((id) => byId[id])
      .filter((n): n is NonNullable<typeof n> =>
        !!n &&
        ["table", "view", "snapshot"].includes((n.type || "").toLowerCase()) &&
        n.disposition === "modernize" &&
        (n.sample_columns?.length ?? 0) > 0
      ),
    [planSet, byId]
  );
  // Stable string key so the async effect only fires when the actual pick set changes.
  const simPicksKey = useMemo(() => simPicks.map((n) => n.id).sort().join(","), [simPicks]);

  // The attribute-comparison spider is ONLY for a pure modernization selection:
  // every selected node must be a comparable modernize object. A selection that
  // mixes in a report (or any non-modernize / non-comparable node) must NOT show
  // it — so require simPicks to cover the WHOLE selection, not just contain ≥2
  // modernize nodes. `simPicks` already filters to modernize table/view/snapshot
  // with column samples, so equal sizes ⇒ nothing else is in the selection.
  const modernizeOnly = simPicks.length >= 2 && simPicks.length === planSet.size;

  const nodeSimilarity = useMemo(
    () => modernizeOnly ? nodesSimilarity(simPicks) : null,
    [modernizeOnly, simPicks]
  );

  // Async upgrade: the sync heuristic above displays immediately; the backend
  // schema-DNA scorer (embeddings + instance stats when profiling is present)
  // replaces it once the request lands. Falls back silently on error.
  const [asyncSimilarity, setAsyncSimilarity] = useState<import("../../types").ProductSimilarity | null>(null);
  const [asyncMatches, setAsyncMatches] = useState<import("../../lib/similarity").AttrMatch[]>([]);
  const [simLoading, setSimLoading] = useState(false);
  useEffect(() => {
    setAsyncSimilarity(null); setAsyncMatches([]);
    if (!projectId || !modernizeOnly) return;
    const sourceCols = (simPicks[0].sample_columns || []).map((c) => ({ name: c.name, type: c.type }));
    const targetCols = simPicks.slice(1).flatMap((p) => p.sample_columns || []).map((c) => ({ name: c.name, type: c.type }));
    let cancelled = false;
    setSimLoading(true);
    fetchColumnSimilarity(projectId, sourceCols, { columns: targetCols })
      .then((res) => { if (!cancelled) { setAsyncSimilarity(res.aggregate); setAsyncMatches(res.matches || []); setSimLoading(false); } })
      .catch(() => { if (!cancelled) setSimLoading(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, simPicksKey]);

  const displaySim = modernizeOnly ? (asyncSimilarity ?? nodeSimilarity) : null;

  return (
    <>
      <TopBar
        stats={[[visNodes.length, "nodes"], [edges.length, "edges"], [Object.keys(model.systems).length, "systems"]]}
        mode={mode}
        q={q} setQ={setQ} fSys={fSys} setFSys={setFSys} fType={fType} setFType={setFType}
        fDom={fDom} setFDom={setFDom}
        showAll={showAll} setShowAll={setShowAll}
        root={rootId ? byId[rootId] : null}
        onBack={backToEstate}
        onFit={() => fitView({ duration: 420, padding: 0.12 })}
        onClearFilters={() => { setQ(""); setFSys("all"); setFType("all"); setFDom("all"); }}
        clusterBy={clusterBy} onPickCluster={pickCluster}
        onReset={() => { setQ(""); setFSys("all"); setFType("all"); setFDom("all"); setSelId(null); fitView({ duration: 400, padding: 0.06, maxZoom: 0.6 }); }}
        systems={model.systems} types={model.types} domains={model.domains}
      />

      <div className="body">
        <div className={"stage" + (searching && highlightMatches ? " eg-searching" : "")}>
          <ReactFlow
            nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes}
            onNodesChange={onNodesChange}
            onlyRenderVisibleElements
            minZoom={0.12} maxZoom={2.2} nodeDragThreshold={4}
            zoomOnDoubleClick={false}
            fitView fitViewOptions={{ padding: 0.06, maxZoom: 0.6 }}
            nodesConnectable={false} nodesDraggable elementsSelectable
            proOptions={{ hideAttribution: true }}
            onNodeClick={(_e, n) => handleNodeClick(n.id)}
            onNodeDoubleClick={(_e, n) => handleNodeDoubleClick(n.id)}
            onNodeDragStart={onNodeDragStart}
            onNodeDragStop={onNodeDragStop}
            onPaneClick={() => {
              // Clears the selection ONLY — deselecting must never move the
              // camera or collapse a drilled subgraph back to the overview.
              // Exiting a drill stays on Esc / the "Full estate" button.
              setSelId(null); setPlanSet(new Set()); setTraceAll(false);
            }}
          >
            <Background variant={BackgroundVariant.Dots} gap={26} size={1} color="var(--eg-grid-dot)" />
            {SHOW_MINIMAP ? (
              <MiniMap pannable zoomable nodeStrokeWidth={0} nodeBorderRadius={2}
                nodeColor={(n) => model.systems[(n.data as EstateFlowNode["data"]).node.system]?.color || "#94a3b8"}
                maskColor="var(--eg-mask)" />
            ) : null}
          </ReactFlow>

          <Legend collapsed={mode === "focused"} preview={preview} systems={model.systems} types={model.types} />
          <ZoomLadder tier={tier} override={tierOverride} onPick={setTierOverride} />
        </div>
        {displaySim ? (
          <div style={{ width: 300, flexShrink: 0, borderLeft: "1px solid #e2e8f0", background: "#fff", padding: 16, overflowY: "auto" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 8, marginBottom: 10 }}>
              <div>
                <div style={{ fontSize: 14, fontWeight: 800, color: "#6d28d9", lineHeight: 1.25 }}>{displaySim.title}</div>
                <div style={{ fontSize: 11, color: "#64748b", marginTop: 2 }}>
                  {displaySim.subtitle}
                  {simLoading && <span style={{ marginLeft: displaySim.subtitle ? 6 : 0, color: "#a78bfa" }}>Scoring…</span>}
                </div>
              </div>
              <button onClick={() => setPlanSet(new Set())} aria-label="Close" style={{ border: "none", background: "transparent", color: "#94a3b8", fontSize: 17, cursor: "pointer", lineHeight: 1 }}>×</button>
            </div>
            <SimilarityRadar data={displaySim} compact afterMatch={onPlan ? (
              <button
                className="btn primary wide eg-btn-icon"
                style={{ background: "#7c3aed", borderColor: "#7c3aed", marginTop: 10, marginBottom: 4 }}
                onClick={() => onPlan(Array.from(planSet))}
              >
                Recommendation planning ({planSet.size}) <ExtLinkIcon />
              </button>
            ) : undefined} />
            {asyncMatches.length > 0 && (
              <div style={{ marginTop: 12, borderTop: "1px solid #f1f5f9", paddingTop: 10 }}>
                <div style={{ fontSize: 11, fontWeight: 800, color: "#475569", textTransform: "uppercase", letterSpacing: 0.3, marginBottom: 6 }}>
                  Attribute-by-attribute
                </div>
                <div style={{ fontSize: 10.5, color: "#94a3b8", marginBottom: 8 }}>
                  Each column of <b style={{ color: "#64748b" }}>{simPicks[0]?.name}</b> vs its best match in the other selected node{simPicks.length > 2 ? "s" : ""}.
                </div>
                {[...asyncMatches].sort((a, b) => Number(b.matched) - Number(a.matched) || b.match - a.match).map((m, i) => (
                  <div key={m.source.name + i} style={{ display: "flex", gap: 6, padding: "4px 0", borderBottom: "1px solid #f8fafc" }}>
                    <span style={{ color: m.matched ? "#16a34a" : "#cbd5e1", fontWeight: 800, fontSize: 12, lineHeight: "16px", flex: "none" }}>{m.matched ? "✓" : "—"}</span>
                    <div style={{ minWidth: 0, flex: 1 }}>
                      <div style={{ fontSize: 11.5, color: "#334155", lineHeight: 1.3 }}>
                        <b>{m.source.name}</b>
                        {m.target ? (
                          <> <span style={{ color: "#cbd5e1" }}>→</span> {m.target.name} <span style={{ color: m.matched ? "#16a34a" : "#94a3b8", fontWeight: 700 }}>· {m.match}</span></>
                        ) : (
                          <span style={{ color: "#94a3b8" }}> · no candidate</span>
                        )}
                      </div>
                      {!m.matched && (
                        <div style={{ fontSize: 10, color: "#94a3b8", lineHeight: 1.3, marginTop: 1 }}>
                          {m.unmatched_reason || "Below the match threshold."}
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        ) : sel ? (
          <Detail
            node={sel} up={upN} down={downN} mode={mode}
            onFocus={() => focusOn(sel.id)}
            onClose={() => setSelId(null)}
            onOpenRow={onOpenRow ? () => onOpenRow(sel.name) : undefined}
            onPlan={onPlan ? () => onPlan(planSet.size ? Array.from(planSet) : [sel.id]) : undefined}
            planCount={planSet.size}
            planSameRec={planSameRec}
            planDisp={planDisp}
            systems={model.systems} types={model.types}
          />
        ) : null}
      </div>
    </>
  );
}

export interface EstateGraphViewProps {
  inventory: InventoryResponse;
  /** Jump to the disposition table with this object's name pre-searched. */
  onOpenRow?: (name: string) => void;
  /** Open recommendation planning for the selected object(s). */
  onPlan?: (ids: string[]) => void;
  /** Fires whenever the live node selection changes (drives the analysis panel). */
  onSelectionChange?: (ids: string[]) => void;
  /** Object id(s) to drill into on entry — the disposition table's "View in graph" jump. */
  focusIds?: string[];
  /** Bump to re-trigger a focus on the same id(s). */
  focusNonce?: number;
  /** Node id(s) rendered as a live what-if ADD (always styled NEW). */
  previewAddIds?: string[];
  /** Node id(s) rendered as a live what-if RETIREMENT (always styled RETIRING). */
  previewRetireIds?: string[];
  /** Node id(s) the migration plan flagged — their edges to the new node read amber + ⚠. */
  previewCautionIds?: string[];
  /** Per-flagged-node ⚠ tooltip text (the model's reason for the caution). */
  previewCautionDetails?: Record<string, string>;
  /** Explicit height for the graph container (default fills the viewport). */
  height?: number | string;
  /** Project id forwarded to the backend schema-DNA scorer for async similarity upgrades. */
  projectId?: number | string;
}

export default function EstateGraphView({ inventory, onOpenRow, onPlan, onSelectionChange, focusIds, focusNonce, previewAddIds, previewRetireIds, previewCautionIds, previewCautionDetails, height, projectId }: EstateGraphViewProps) {
  const model = useMemo(() => inventoryToEstate(inventory), [inventory]);
  const containerRef = useRef<HTMLDivElement | null>(null);
  return (
    <div className="estate-graph" ref={containerRef} style={{ height: height ?? "calc(100vh - 240px)", minHeight: 520 }}>
      <ReactFlowProvider>
        <Graph model={model} containerRef={containerRef} onOpenRow={onOpenRow} onPlan={onPlan} onSelectionChange={onSelectionChange} focusIds={focusIds} focusNonce={focusNonce} previewAddIds={previewAddIds} previewRetireIds={previewRetireIds} previewCautionIds={previewCautionIds} previewCautionDetails={previewCautionDetails} projectId={projectId} />
      </ReactFlowProvider>
    </div>
  );
}
