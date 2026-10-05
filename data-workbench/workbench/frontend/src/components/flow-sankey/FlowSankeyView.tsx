import { useCallback, useMemo, useState, type CSSProperties } from "react";
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
  type NodeMouseHandler,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { KIND_STYLES } from "../productKindStyles";
import {
  NODE_W,
  PRODUCT_COLUMN_KEYS,
  computeFanOut,
  focusSetFor,
  layoutFlow,
} from "./flowSankeyLayout";
import { statusStyleFor } from "./flowStatusStyles";
import type { FlowContext, FlowNodeData, FlowPayload } from "./flowSankeyTypes";

/**
 * Layered value-flow ("Sankey") view — a LEFT→RIGHT node-link diagram across
 * five fixed columns (source → source-aligned → aggregate → consumer → use
 * cases). Presentational only (no data-fetching); the parent tab owns the
 * fetch and hands the normalized `FlowPayload` in. Context-agnostic: the same
 * component renders the marketplace and (fast-follow) the estate view.
 *
 * Interactions: click a node to highlight its up∪down path (dimming the rest);
 * "links on select" hides cross-column links until a node is picked (default ON
 * above a threshold, to avoid a hairball — mirrors the reference's "Capability
 * links: on select"); a reuse-heat toggle recolors PRODUCT columns by
 * downstream fan-out. Ghost nodes (empty columns) are inert.
 */

// Neutral chrome for the two columns without a productKind palette.
const NEUTRAL_SOURCE = { bg: "#f1f5f9", fg: "#334155", border: "#cbd5e1", label: "Schema" };
const NEUTRAL_USECASE = { bg: "#f8fafc", fg: "#64748b", border: "#e2e8f0", label: "Use case" };

// Above this many links the canvas is a hairball → default to links-on-select.
const LINK_HAIRBALL_THRESHOLD = 24;

interface EdgeKindStyle {
  stroke: string;
  label: string;
}
const EDGE_KIND_STYLE: Record<string, EdgeKindStyle> = {
  consumes: { stroke: "#64748b", label: "consumes" },
  source_binding: { stroke: "#0ea5e9", label: "feeds" },
  supports: { stroke: "#16a34a", label: "supports" }, // reserved (estate)
  feasibility_match: { stroke: "#8b5cf6", label: "matches" }, // reserved (estate)
};

function chromeFor(column: string, kind: string | undefined) {
  if (column === "use_case") return NEUTRAL_USECASE;
  if (column === "source_system") return NEUTRAL_SOURCE;
  return KIND_STYLES[(kind || "").toLowerCase()] || NEUTRAL_SOURCE;
}

function FlowNodeCard({ data }: NodeProps<Node<FlowNodeData>>) {
  const d = data;
  const chrome = chromeFor(d.column, d.kind);
  const ghost = !!d.placeholder;
  const status = statusStyleFor(d.context, d.status);
  const heatBg =
    typeof d.heat === "number"
      ? `rgba(249, 115, 22, ${(0.06 + d.heat * 0.4).toFixed(3)})`
      : undefined;

  return (
    <div
      style={{
        width: NODE_W,
        boxSizing: "border-box",
        padding: "8px 10px",
        borderRadius: 10,
        background: ghost ? "transparent" : heatBg || "#fff",
        border: ghost
          ? "1px dashed #cbd5e1"
          : `2px solid ${d.focused ? "#0f172a" : chrome.border}`,
        boxShadow: ghost
          ? "none"
          : d.focused
          ? "0 0 0 3px rgba(15,23,42,0.12)"
          : "0 1px 2px rgba(0,0,0,0.06)",
        opacity: ghost ? 0.7 : d.dimmed ? 0.28 : 1,
        transition: "opacity 120ms ease",
        pointerEvents: ghost ? "none" : undefined,
        fontFamily: "system-ui, sans-serif",
      }}
    >
      {/* Handles kept on every card so bezier edges attach; ghosts have no links. */}
      <Handle type="target" position={Position.Left} style={{ background: chrome.border, opacity: ghost ? 0 : 1 }} />
      {ghost ? (
        <div style={{ fontSize: 12, color: "#94a3b8", textAlign: "center", fontStyle: "italic", padding: "6px 0" }}>
          {d.label}
        </div>
      ) : (
        <>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 6 }}>
            <span
              style={{
                fontSize: 10,
                fontWeight: 700,
                padding: "1px 6px",
                borderRadius: 999,
                backgroundColor: chrome.bg,
                color: chrome.fg,
                border: `1px solid ${chrome.border}`,
                whiteSpace: "nowrap",
              }}
            >
              {chrome.label}
            </span>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              {typeof d.count === "number" && d.count > 0 && (
                <span
                  title={
                    d.column === "source_system"
                      ? `Feeds ${d.count} source-aligned product(s)`
                      : `${d.count} direct downstream consumer(s)`
                  }
                  style={{
                    fontSize: 10,
                    fontWeight: 700,
                    color: "#475569",
                    backgroundColor: "#f1f5f9",
                    border: "1px solid #e2e8f0",
                    borderRadius: 999,
                    padding: "0 6px",
                  }}
                >
                  ×{d.count}
                </span>
              )}
              {(d.column === "source_aligned" || d.column === "aggregate" || d.column === "consumer") && (
                <button
                  type="button"
                  onClick={(e) => { e.stopPropagation(); d.onOpen(d.id); }}
                  title="Open product detail"
                  style={{ border: "none", background: "transparent", color: "#3b82f6", cursor: "pointer", fontSize: 12, fontWeight: 600, padding: 0 }}
                >
                  Open ↗
                </button>
              )}
            </span>
          </div>
          <div
            style={{
              fontSize: 13,
              fontWeight: 700,
              color: "#0f172a",
              marginTop: 4,
              lineHeight: 1.2,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {d.label}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 3, minHeight: 14 }}>
            {status && (
              <>
                <span style={{ width: 8, height: 8, borderRadius: 999, background: status.dot, flex: "0 0 auto" }} />
                <span style={{ fontSize: 10, color: "#64748b" }}>{status.label}</span>
              </>
            )}
            {d.column === "source_system" && typeof d.meta?.project_code === "string" && d.meta.project_code && (
              <span style={{ fontSize: 10, color: "#94a3b8", marginLeft: status ? "auto" : 0 }}>
                {String(d.meta.project_code)}
              </span>
            )}
          </div>
        </>
      )}
      <Handle type="source" position={Position.Right} style={{ background: chrome.border, opacity: ghost ? 0 : 1 }} />
    </div>
  );
}

const NODE_TYPES = { flow: FlowNodeCard };

interface Props {
  context: FlowContext;
  payload: FlowPayload | null;
  loading?: boolean;
  error?: string | null;
  onOpenNode?: (id: string) => void;
}

export default function FlowSankeyView({ context, payload, loading, error, onOpenNode }: Props) {
  const [focusUri, setFocusUri] = useState<string | null>(null);
  const [heatOn, setHeatOn] = useState(false);
  // Links-on-select defaults to ON above the hairball threshold. Stored once
  // the payload is known; user can override with the toggle.
  const linkCount = payload?.links.length ?? 0;
  const [linksOnSelect, setLinksOnSelect] = useState<boolean | null>(null);
  const effectiveLinksOnSelect = linksOnSelect ?? linkCount > LINK_HAIRBALL_THRESHOLD;

  const hasAnyRealNode = useMemo(
    () => !!payload && payload.columns.some((c) => c.nodes.length > 0),
    [payload],
  );

  const { pos, ghostNodes } = useMemo(
    () => (payload ? layoutFlow(payload) : { pos: {}, ghostNodes: [] }),
    [payload],
  );

  // Reuse-heat ramp over PRODUCT columns only (the source column reaches
  // everything and would flatten the ramp).
  const heatByNode = useMemo(() => {
    if (!payload) return {} as Record<string, number>;
    const productIds = payload.columns
      .filter((c) => PRODUCT_COLUMN_KEYS.has(c.key))
      .flatMap((c) => c.nodes.map((n) => n.id));
    const fan = computeFanOut(productIds, payload.links);
    const max = Math.max(1, ...Object.values(fan));
    const out: Record<string, number> = {};
    for (const id of productIds) out[id] = fan[id] / max;
    return out;
  }, [payload]);

  const focusSet = useMemo(
    () => (focusUri && payload ? focusSetFor(focusUri, payload.links) : null),
    [focusUri, payload],
  );

  const openNode = useCallback((id: string) => { if (onOpenNode) onOpenNode(id); }, [onOpenNode]);

  const rfNodes: Node<FlowNodeData>[] = useMemo(() => {
    if (!payload) return [];
    const realNodes = payload.columns.flatMap((c) => c.nodes);
    const all = [...realNodes, ...ghostNodes];
    return all.map((n) => ({
      id: n.id,
      type: "flow",
      position: pos[n.id] || { x: 0, y: 0 },
      draggable: !n.placeholder,
      selectable: !n.placeholder,
      data: {
        id: n.id,
        label: n.label,
        column: n.column,
        kind: n.kind,
        status: n.status,
        count: n.count,
        meta: n.meta,
        placeholder: n.placeholder,
        context,
        dimmed: !!focusSet && !focusSet.has(n.id),
        focused: focusUri === n.id,
        heat: heatOn ? heatByNode[n.id] : undefined,
        onOpen: openNode,
      },
    }));
  }, [payload, ghostNodes, pos, focusSet, focusUri, heatOn, heatByNode, context, openNode]);

  const rfEdges: Edge[] = useMemo(() => {
    if (!payload) return [];
    // Links-on-select with nothing selected → hide all links.
    if (effectiveLinksOnSelect && !focusUri) return [];
    return payload.links.map((lk) => {
      const st = EDGE_KIND_STYLE[lk.kind] || { stroke: "#94a3b8", label: lk.kind };
      const inFocus = !focusSet || (focusSet.has(lk.source) && focusSet.has(lk.target));
      const weightW = lk.kind === "source_binding" && typeof lk.weight === "number"
        ? Math.min(4, 1 + lk.weight / 6)
        : 1.5;
      return {
        id: `${lk.source}>${lk.target}:${lk.kind}`,
        source: lk.source,
        target: lk.target,
        type: "default", // bezier
        markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: inFocus ? st.stroke : "#e2e8f0" },
        style: {
          stroke: inFocus ? st.stroke : "#e2e8f0",
          strokeWidth: inFocus ? weightW : 1,
          strokeDasharray: lk.kind === "feasibility_match" ? "5 4" : undefined,
          opacity: inFocus ? 0.9 : 0.25,
        },
      };
    });
  }, [payload, focusSet, focusUri, effectiveLinksOnSelect]);

  const handleNodeClick: NodeMouseHandler = useCallback((_e, node) => {
    const nd = node.data as FlowNodeData;
    if (nd.placeholder) return;
    setFocusUri((cur) => (cur === node.id ? null : node.id));
  }, []);

  if (loading) return <div style={{ color: "#94a3b8", padding: 24 }}>Loading value-flow…</div>;
  if (error) return <div style={{ color: "#ef4444", padding: 24 }}>{error}</div>;

  if (!hasAnyRealNode) {
    return (
      <div style={styles.empty}>
        <div style={{ fontSize: 40, marginBottom: 10 }}>⇥</div>
        <h3 style={{ margin: "0 0 6px", color: "#334155" }}>Nothing to chart yet</h3>
        <p style={{ margin: 0, color: "#94a3b8", maxWidth: 460, lineHeight: 1.5 }}>
          Publish data products and bind them together (a consumer <code>:CONSUMES</code> a
          source or aggregate) to see the value flow across source schemas, products, and use cases.
        </p>
      </div>
    );
  }

  const columnLabels = payload!.columns.map((c) => ({ key: c.key, label: c.label, count: c.nodes.length }));

  return (
    <div>
      {/* Controls */}
      <div style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "center", marginBottom: 10 }}>
        <label style={ctrlLabel}>
          <input
            type="checkbox"
            checked={effectiveLinksOnSelect}
            onChange={(e) => setLinksOnSelect(e.target.checked)}
          />
          Capability links: on select
        </label>
        <label style={ctrlLabel}>
          <input type="checkbox" checked={heatOn} onChange={(e) => setHeatOn(e.target.checked)} />
          Reuse heat
        </label>
        {focusUri && (
          <button type="button" onClick={() => setFocusUri(null)} style={styles.clearBtn}>
            Clear focus
          </button>
        )}
        <span style={{ fontSize: 12, color: "#94a3b8", marginLeft: "auto" }}>
          {effectiveLinksOnSelect && !focusUri
            ? "Select a node to reveal its links"
            : `${linkCount} link${linkCount === 1 ? "" : "s"}`}
        </span>
      </div>

      {/* Column headers — always all 5 so the frame reads even where a column is empty. */}
      <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
        {columnLabels.map((c) => (
          <div
            key={c.key}
            style={{
              flex: 1,
              textAlign: "center",
              fontSize: 11,
              fontWeight: 700,
              letterSpacing: 0.3,
              textTransform: "uppercase",
              color: c.count > 0 ? "#475569" : "#cbd5e1",
            }}
          >
            {c.label}
          </div>
        ))}
      </div>

      <div style={{ height: 620, border: "1px solid #e2e8f0", borderRadius: 12, background: "#fafcff" }}>
        <ReactFlow
          nodes={rfNodes}
          edges={rfEdges}
          nodeTypes={NODE_TYPES}
          onNodeClick={handleNodeClick}
          onPaneClick={() => setFocusUri(null)}
          fitView
          fitViewOptions={{ padding: 0.15 }}
          proOptions={{ hideAttribution: true }}
          minZoom={0.2}
        >
          <Background gap={16} color="#e2e8f0" />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
    </div>
  );
}

const ctrlLabel: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  fontSize: 12,
  color: "#475569",
  cursor: "pointer",
  userSelect: "none",
};

const styles: Record<string, CSSProperties> = {
  empty: {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    textAlign: "center",
    padding: "64px 24px",
    border: "1px dashed #e2e8f0",
    borderRadius: 12,
  },
  clearBtn: {
    fontSize: 12,
    padding: "4px 10px",
    borderRadius: 999,
    border: "1px solid #cbd5e1",
    background: "#fff",
    color: "#475569",
    cursor: "pointer",
  },
};
