// Node renderer (three levels of detail) + the position-tween and tier hooks
// + the routed edge. Ported from the design's merge-node.jsx to @xyflow/react
// v12 (named ESM imports, no UMD globals) and TS.
import { memo, useEffect, useMemo, useRef, useState } from "react";
import {
  Handle,
  Position,
  useStore,
  getBezierPath,
  EdgeLabelRenderer,
  type Node,
  type NodeProps,
  type EdgeProps,
} from "@xyflow/react";
import { NODE_W, NODE_H, type Point } from "./estateLayout";
import { GLYPH_PATHS } from "./estateModel";
import type { EGNode, SystemDef, TypeDef, Tier, ChangeKind } from "./estateModel";

// Short recommendation tag so disposition stays legible in the chip tier
// (the full pill only fits in the card tier).
const DISP_ABBR: Record<string, string> = {
  modernize: "MOD",
  migrate: "MIG",
  retire: "RET",
  remain: "REM",
};

// The node box IS the visible shape at each tier, so edges always meet a side.
export const TIER_SIZE: Record<Tier, [number, number]> = {
  glyph: [46, 46],
  chip: [200, 34],
  card: [NODE_W, NODE_H],
};

export function Glyph({ shape, color, size, glow }: { shape: string; color: string; size?: number; glow?: number }) {
  const sc = (size || 22) / 22;
  return (
    <svg
      className={glow ? "glyph glowing" : "glyph"}
      width={size || 22}
      height={size || 22}
      viewBox="-12 -12 24 24"
      aria-hidden="true"
      style={glow ? ({ "--gc": color, "--gr": glow + "px" } as React.CSSProperties) : undefined}
    >
      <g transform={`scale(${sc})`}>
        {shape === "circle" ? <circle r="10" fill={color} /> : <path d={GLYPH_PATHS[shape] || GLYPH_PATHS.square} fill={color} />}
      </g>
    </svg>
  );
}

// ── the one node component, three tiers ───────────────────
export interface EstateNodeData {
  node: EGNode;
  tier: Tier;
  hl: string; // normal | sel | up | down | ctx | dim | hit
  w: number;
  h: number;
  change: ChangeKind;
  livePreview: boolean; // change comes from a live what-if (DAG-style dashed outline, no fill)
  isRoot: boolean;
  // On a selected node: how many directly-connected objects are currently HIDDEN
  // (0 once fully expanded / drilled in). Rendered as a "+N" corner badge.
  expandCount: number;
  glyphLabels: boolean;
  glow: number;
  glyphSize: number;
  systems: Record<string, SystemDef>;
  types: Record<string, TypeDef>;
  [k: string]: unknown;
}

export type EstateFlowNode = Node<EstateNodeData, "obj">;

// "+N" hidden-connections badge, coloured like the node's platform.
function more(count: number, color: string) {
  if (count <= 0) return null;
  return (
    <span className="eg-more" style={{ background: color }} title={`${count} connected object${count === 1 ? "" : "s"} hidden — double-click to expand`}>
      +{count}
    </span>
  );
}

function ObjectNodeImpl({ data, selected }: NodeProps<EstateFlowNode>) {
  const n = data.node;
  const sys = data.systems[n.system] || { label: n.system, color: "var(--eg-border-strong)" };
  const typeDef = data.types[n.type] || { label: n.type, shape: "square" };
  const tier = data.tier;
  const cls = ["onode", "tier-" + tier, "st-" + data.hl];
  if (selected) cls.push("is-sel");
  if (data.isRoot) cls.push("is-root");
  // Toggle-overlay changes keep the estate's tinted ch- classes; a LIVE what-if
  // instead mirrors the DAG — a dashed outline (navy new / red retiring), no fill.
  if (data.change && !data.livePreview) cls.push("ch-" + data.change);
  if (data.livePreview) { cls.push("is-live-preview"); if (data.change) cls.push("lp-" + data.change); }
  // Dashed what-if outline only reads on the full card — suppress it on the
  // glyph/chip tiers (the badge already carries NEW/RETIRING there).
  const previewOutline = tier !== "card" ? undefined
    : data.livePreview && data.change === "add" ? "2.5px dashed #1e293b"
    : data.livePreview && data.change === "retire" ? "2px dashed var(--eg-c-retire)" : undefined;

  return (
    <div className={cls.join(" ")} style={{ width: data.w, height: data.h, outline: previewOutline, outlineOffset: previewOutline ? 2 : undefined, borderRadius: previewOutline ? 10 : undefined }} data-screen-label={n.name}>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <Handle type="source" position={Position.Right} isConnectable={false} />
      {/* card tier: node box == visible card (no counter-scale), so the badge
          anchors to the box corner; card-wrap's overflow:hidden would clip it */}
      {tier === "card" ? more(data.expandCount, sys.color) : null}
      {/* "+N" corner badge: N directly-connected objects are hidden — double-click
          to expand and reveal them; gone (fully expanded) when everything shows.
          Rendered INSIDE each tier's (counter-scaled) content, not on the node
          box — the glyph/chip visuals scale with 1/zoom while the box doesn't,
          so a box-anchored badge drifts off the visible corner as you zoom. */}
      {tier === "glyph" ? (
        <div className="glyph-wrap">
          <Glyph shape={typeDef.shape} color={sys.color} size={data.glyphSize} glow={data.glow} />
          {data.glyphLabels ? <span className="glyph-label">{n.name}</span> : null}
          {more(data.expandCount, sys.color)}
        </div>
      ) : tier === "chip" ? (
        <div className="chip-wrap">
          <span className="chip-body" style={{ "--sys": sys.color } as React.CSSProperties}>
            <Glyph shape={typeDef.shape} color={sys.color} size={13} />
            <span className="chip-name">{n.name}</span>
            {more(data.expandCount, sys.color)}
            {data.change === "add" ? (
              <span className="chip-disp f-add">NEW</span>
            ) : data.change === "retire" ? (
              <span className="chip-disp f-retire">RET</span>
            ) : n.disposition && DISP_ABBR[n.disposition] ? (
              <span className={"chip-disp p-" + n.disposition}>{DISP_ABBR[n.disposition]}</span>
            ) : null}
            <span className={"chip-dot d-" + n.status}></span>
          </span>
        </div>
      ) : (
        <div className={"card-wrap ty-" + n.type} style={{ "--sys": sys.color } as React.CSSProperties}>
          <div className="card-head">
            <span className={"dot d-" + n.status}></span>
            <span className="card-name">{n.name}</span>
            <span className="card-type">{typeDef.label}</span>
          </div>
          <div className="card-sub">
            <span className="card-team">{n.domainLabel}</span>
            <span className="card-sys" style={{ "--sys": sys.color } as React.CSSProperties}>{sys.label}</span>
            {/* Change flag takes the recommendation (disposition) slot — a NEW /
                RETIRING badge reads where the disposition pill would otherwise be. */}
            {data.change === "add" ? <span className="card-disp f-add">NEW</span>
              : data.change === "retire" ? <span className="card-disp f-retire">RETIRING</span>
              : n.disposition ? <span className={"card-disp p-" + n.disposition}>{n.disposition.toUpperCase()}</span> : null}
          </div>
          <div className="card-metric">{n.metric}</div>
        </div>
      )}
    </div>
  );
}

// Re-render only when something VISIBLE changes. During pan/zoom RF keeps the
// same props reference, and the position tween moves the wrapper (not this
// body), so a field-level comparator lets unaffected cards skip reconciliation
// entirely — the win for the dense glyph view.
export const ObjectNode = memo(ObjectNodeImpl, (a, b) => {
  const da = a.data, db = b.data;
  return a.selected === b.selected
    && da.tier === db.tier && da.hl === db.hl && da.change === db.change
    && da.livePreview === db.livePreview && da.expandCount === db.expandCount
    && da.isRoot === db.isRoot && da.w === db.w && da.h === db.h
    && da.glyphLabels === db.glyphLabels && da.glow === db.glow
    && da.glyphSize === db.glyphSize && da.node === db.node
    && da.systems === db.systems && da.types === db.types;
});

// ── tween node positions between the two layouts ──────────
// React Flow will not interpolate `position` — this does, so the two views
// read as one graph rearranging itself.
export function useAnimatedNodes(target: EstateFlowNode[], duration: number): EstateFlowNode[] {
  // Position overrides applied ONLY while a layout tween is in flight. Node DATA
  // (hl / selected / change / …) always comes straight from `target`, so a
  // selection change with no move is never held back by a stale nodes snapshot.
  const overrideRef = useRef<Record<string, Point>>({});
  const prevRef = useRef<EstateFlowNode[]>(target);
  const raf = useRef<number>(0);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    cancelAnimationFrame(raf.current);
    // start each node from where it's currently drawn (its live override or its
    // last committed position), then tween toward the new target position.
    const startPos: Record<string, Point> = {};
    prevRef.current.forEach((n) => { startPos[n.id] = overrideRef.current[n.id] || n.position; });
    prevRef.current = target;
    const some = duration > 0 && target.some((n) => {
      const p = startPos[n.id];
      return p && (Math.abs(p.x - n.position.x) > 0.5 || Math.abs(p.y - n.position.y) > 0.5);
    });
    if (!some) { overrideRef.current = {}; setTick((t) => t + 1); return; }

    const t0 = performance.now();
    const ease = (t: number) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
    const step = (now: number) => {
      const t = Math.min(1, (now - t0) / duration), e = ease(t);
      const ov: Record<string, Point> = {};
      target.forEach((n) => {
        const s = startPos[n.id];
        if (s) ov[n.id] = { x: s.x + (n.position.x - s.x) * e, y: s.y + (n.position.y - s.y) * e };
      });
      overrideRef.current = t < 1 ? ov : {};
      setTick((tk) => tk + 1);
      if (t < 1) raf.current = requestAnimationFrame(step);
    };
    raf.current = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf.current);
  }, [target, duration]);

  // Always the latest target data; positions overridden only during a tween.
  return useMemo(() => {
    const ov = overrideRef.current;
    if (!Object.keys(ov).length) return target;
    return target.map((n) => (ov[n.id] ? { ...n, position: ov[n.id] } : n));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, tick]);
}

// ── zoom → level of detail ────────────────────────────────
export function useTier(glyphMax: number, chipMax: number): { zoom: number; tier: Tier } {
  const zoom = useStore((s) => s.transform[2]);
  return { zoom, tier: zoom < glyphMax ? "glyph" : zoom < chipMax ? "chip" : "card" };
}

// ── edge that follows dagre's routed polyline instead of cutting across ──
function roundedPath(p: Point[], r: number): string {
  if (p.length < 2) return "";
  if (p.length === 2) return `M ${p[0].x},${p[0].y} L ${p[1].x},${p[1].y}`;
  let d = `M ${p[0].x},${p[0].y}`;
  for (let i = 1; i < p.length - 1; i++) {
    const a = p[i - 1], b = p[i], c = p[i + 1];
    const d1 = Math.hypot(b.x - a.x, b.y - a.y) || 1;
    const d2 = Math.hypot(c.x - b.x, c.y - b.y) || 1;
    const k = Math.min(r, d1 / 2, d2 / 2);
    const inX = b.x - ((b.x - a.x) / d1) * k, inY = b.y - ((b.y - a.y) / d1) * k;
    const outX = b.x + ((c.x - b.x) / d2) * k, outY = b.y + ((c.y - b.y) / d2) * k;
    d += ` L ${inX},${inY} Q ${b.x},${b.y} ${outX},${outY}`;
  }
  const l = p[p.length - 1];
  return d + ` L ${l.x},${l.y}`;
}

interface RoutedEdgeData {
  points?: Point[];
  path?: Point[] | null;
  radius?: number;
  sOff?: number;
  tOff?: number;
  caution?: boolean;
  cautionDetail?: string;
  [k: string]: unknown;
}

function RoutedEdgeImpl({ id, sourceX, sourceY, targetX, targetY, style, data, markerEnd }: EdgeProps) {
  const d = data as RoutedEdgeData | undefined;
  const r = d && d.radius != null ? d.radius : 18;
  const caution = !!(d && d.caution);

  let pathStr: string;
  let mid: Point;
  // the view builds the full tier-aware, obstacle-avoiding polyline
  if (d && d.path && d.path.length > 1) {
    pathStr = roundedPath(d.path, r);
    mid = d.path[Math.floor(d.path.length / 2)];
  } else {
    // back edges and same-rank edges have no forward channel to route through
    const sy = sourceY + ((d && d.sOff) || 0);
    const ty = targetY + ((d && d.tOff) || 0);
    const [bp, lx, ly] = getBezierPath({
      sourceX, sourceY: sy, sourcePosition: Position.Right,
      targetX, targetY: ty, targetPosition: Position.Left, curvature: 0.6,
    });
    pathStr = bp;
    mid = { x: lx, y: ly };
  }
  return (
    <>
      <path id={id} className="react-flow__edge-path" d={pathStr} style={style} markerEnd={markerEnd} />
      {caution ? (
        <EdgeLabelRenderer>
          <div
            className="eg-caution-badge"
            style={{ transform: `translate(-50%, -50%) translate(${mid.x}px, ${mid.y}px)` }}
            title={(d && d.cautionDetail) || "Flagged by the migration plan — this must be migrated / repointed before cutover"}
          >⚠</div>
        </EdgeLabelRenderer>
      ) : null}
    </>
  );
}

export const RoutedEdge = memo(RoutedEdgeImpl);
