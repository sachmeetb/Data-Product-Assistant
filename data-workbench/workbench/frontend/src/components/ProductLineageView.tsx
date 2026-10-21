import { useEffect, useMemo, useState, type CSSProperties } from "react";
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
import api from "../api/client";
import { KIND_STYLES } from "./productKindStyles";
import { dagreLayout, traverse, type GraphNode, type GraphEdge } from "./estate-graph/estateLayout";

/**
 * Marketplace-wide PRODUCT lineage — a coarse product-to-product graph over the
 * `:CONSUMES` relationships (source → aggregate → consumer). Nodes are whole
 * data products (NOT tables/columns — that's the column-grain MappingGraphView);
 * edges run in data-flow direction (upstream source → downstream consumer).
 *
 * Reuses the estate graph's headless `dagreLayout` (LR layered DAG) for node
 * placement and React Flow for rendering. Node color comes from the shared
 * `KIND_STYLES` palette so it never drifts from the ProductKindChip badge.
 * Single-click a node to focus its full upstream+downstream lineage (dimming
 * the rest); "Open ↗" jumps to the product's marketplace detail.
 */

interface LineageNode {
  uri: string;
  name: string;
  product_kind: string;
  domain: string;
  tags: string[];
  lifecycle_state: string;
}
interface LineageEdge {
  source: string;
  target: string;
  kind: string;
}
interface LineagePayload {
  nodes: LineageNode[];
  edges: LineageEdge[];
}

interface ProductNodeData extends Record<string, unknown> {
  label: string;
  kind: string;
  domain: string;
  tags: string[];
  lifecycle: string;
  dimmed: boolean;
  focused: boolean;
  onOpen: (uri: string) => void;
  uri: string;
}

const DEFAULT_STYLE = { bg: "#f1f5f9", fg: "#334155", border: "#cbd5e1", label: "Product" };

function ProductNode({ data }: NodeProps) {
  const d = data as unknown as ProductNodeData;
  const style = KIND_STYLES[(d.kind || "").toLowerCase()] || DEFAULT_STYLE;
  return (
    <div
      style={{
        width: 224,
        boxSizing: "border-box",
        padding: "8px 10px",
        borderRadius: 10,
        background: "#fff",
        border: `2px solid ${d.focused ? "#0f172a" : style.border}`,
        boxShadow: d.focused ? "0 0 0 3px rgba(15,23,42,0.12)" : "0 1px 2px rgba(0,0,0,0.06)",
        opacity: d.dimmed ? 0.28 : 1,
        transition: "opacity 120ms ease",
      }}
    >
      <Handle type="target" position={Position.Left} style={{ background: style.border }} />
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 6 }}>
        <span
          style={{
            fontSize: 10,
            fontWeight: 700,
            padding: "1px 6px",
            borderRadius: 999,
            backgroundColor: style.bg,
            color: style.fg,
            border: `1px solid ${style.border}`,
            whiteSpace: "nowrap",
          }}
        >
          {style.label}
        </span>
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); d.onOpen(d.uri); }}
          title="Open product detail"
          style={{ border: "none", background: "transparent", color: "#3b82f6", cursor: "pointer", fontSize: 12, fontWeight: 600, padding: 0 }}
        >
          Open ↗
        </button>
      </div>
      <div style={{ fontSize: 13, fontWeight: 700, color: "#0f172a", marginTop: 4, lineHeight: 1.25, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {d.label}
      </div>
      <div style={{ fontSize: 11, color: "#64748b", marginTop: 2, display: "flex", gap: 6, flexWrap: "wrap" }}>
        {d.domain && <span>{d.domain}</span>}
        {d.lifecycle && d.lifecycle !== "published" && <span style={{ color: "#b45309" }}>· {d.lifecycle}</span>}
      </div>
      {d.tags.length > 0 && (
        <div style={{ fontSize: 10, color: "#94a3b8", marginTop: 3, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {d.tags.map((t) => `#${t}`).join(" ")}
        </div>
      )}
      <Handle type="source" position={Position.Right} style={{ background: style.border }} />
    </div>
  );
}

const NODE_TYPES = { product: ProductNode };

interface Props {
  onSelect?: (uri: string) => void;
}

export default function ProductLineageView({ onSelect }: Props) {
  const [payload, setPayload] = useState<LineagePayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showOnlyConnected, setShowOnlyConnected] = useState(true);
  const [kindFilter, setKindFilter] = useState<"all" | "source" | "aggregate" | "consumer">("all");
  const [tagFilter, setTagFilter] = useState<string>("");
  const [focusUri, setFocusUri] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.get("/api/marketplace/product-lineage")
      .then((res) => { if (!cancelled) setPayload(res.data); })
      .catch(() => { if (!cancelled) setError("Failed to load product lineage."); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const allTags = useMemo(() => {
    const seen = new Map<string, string>();
    for (const n of payload?.nodes || []) for (const t of (n.tags || [])) {
      const k = t.trim().toLowerCase();
      if (k && !seen.has(k)) seen.set(k, t.trim());
    }
    return Array.from(seen.values()).sort((a, b) => a.localeCompare(b));
  }, [payload]);

  // Filter nodes (kind + tag), keep edges whose endpoints both survive, then
  // optionally drop degree-0 nodes so the canvas is about relationships.
  const { visNodes, visEdges } = useMemo(() => {
    if (!payload) return { visNodes: [] as LineageNode[], visEdges: [] as LineageEdge[] };
    const kf = kindFilter;
    const tf = tagFilter.toLowerCase();
    let nodes = payload.nodes.filter((n) =>
      (kf === "all" || (n.product_kind || "").toLowerCase() === kf) &&
      (!tf || (n.tags || []).some((t) => t.toLowerCase() === tf))
    );
    let ids = new Set(nodes.map((n) => n.uri));
    let edges = payload.edges.filter((e) => ids.has(e.source) && ids.has(e.target));
    if (showOnlyConnected) {
      const deg = new Set<string>();
      for (const e of edges) { deg.add(e.source); deg.add(e.target); }
      nodes = nodes.filter((n) => deg.has(n.uri));
      ids = new Set(nodes.map((n) => n.uri));
      edges = edges.filter((e) => ids.has(e.source) && ids.has(e.target));
    }
    return { visNodes: nodes, visEdges: edges };
  }, [payload, kindFilter, tagFilter, showOnlyConnected]);

  // Plain {source,target} edges for the layout/traversal utilities (LineageEdge
  // carries an extra `kind` and so isn't structurally a GraphEdge).
  const gEdges: GraphEdge[] = useMemo(
    () => visEdges.map((e) => ({ source: e.source, target: e.target })),
    [visEdges],
  );

  // Layout only depends on the visible set (not focus), so focusing a node
  // re-styles without shuffling positions.
  const positions = useMemo(() => {
    const gNodes: GraphNode[] = visNodes.map((n) => ({ id: n.uri, domain: n.domain || "", system: n.product_kind || "" }));
    return dagreLayout(gNodes, gEdges).pos;
  }, [visNodes, gEdges]);

  // Focus set = the focused node's full upstream + downstream lineage.
  const focusSet = useMemo(() => {
    if (!focusUri) return null;
    const up = traverse(focusUri, gEdges, "up");
    const down = traverse(focusUri, gEdges, "down");
    return new Set<string>([focusUri, ...up, ...down]);
  }, [focusUri, gEdges]);

  const openProduct = (uri: string) => { if (onSelect) onSelect(uri); };

  const rfNodes: Node[] = useMemo(() =>
    visNodes.map((n) => ({
      id: n.uri,
      type: "product",
      position: positions[n.uri] || { x: 0, y: 0 },
      data: {
        label: n.name || n.uri,
        kind: n.product_kind,
        domain: n.domain,
        tags: n.tags || [],
        lifecycle: n.lifecycle_state,
        dimmed: !!focusSet && !focusSet.has(n.uri),
        focused: focusUri === n.uri,
        onOpen: openProduct,
        uri: n.uri,
      } as ProductNodeData,
    })),
  // eslint-disable-next-line react-hooks/exhaustive-deps
  [visNodes, positions, focusSet, focusUri]);

  const rfEdges: Edge[] = useMemo(() =>
    visEdges.map((e) => {
      const inFocus = !focusSet || (focusSet.has(e.source) && focusSet.has(e.target));
      return {
        id: `${e.source}>${e.target}`,
        source: e.source,
        target: e.target,
        type: "smoothstep",
        animated: false,
        markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: inFocus ? "#64748b" : "#cbd5e1" },
        style: { stroke: inFocus ? "#64748b" : "#e2e8f0", strokeWidth: inFocus ? 1.5 : 1, opacity: inFocus ? 1 : 0.4 },
      };
    }),
  [visEdges, focusSet]);

  const handleNodeClick: NodeMouseHandler = (_e, node) => {
    setFocusUri((cur) => (cur === node.id ? null : node.id));
  };

  if (loading) return <div style={{ color: "#94a3b8", padding: 24 }}>Loading product lineage…</div>;
  if (error) return <div style={{ color: "#ef4444", padding: 24 }}>{error}</div>;
  if (!payload || payload.nodes.length === 0) {
    return (
      <div style={styles.empty}>
        <div style={{ fontSize: 40, marginBottom: 10 }}>⇄</div>
        <h3 style={{ margin: "0 0 6px", color: "#334155" }}>No products to chart yet</h3>
        <p style={{ margin: 0, color: "#94a3b8", maxWidth: 420, lineHeight: 1.5 }}>
          Publish data products and bind them together (a consumer <code>:CONSUMES</code> a source or aggregate) to see the dependency graph here.
        </p>
      </div>
    );
  }

  const kindChips: Array<{ key: typeof kindFilter; label: string }> = [
    { key: "all", label: "All" },
    { key: "source", label: "Source" },
    { key: "aggregate", label: "Aggregate" },
    { key: "consumer", label: "Consumer" },
  ];

  return (
    <div>
      {/* Controls */}
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center", marginBottom: 10 }}>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {kindChips.map((c) => {
            const active = kindFilter === c.key;
            const st = c.key === "all" ? null : KIND_STYLES[c.key];
            return (
              <button
                key={c.key}
                type="button"
                onClick={() => setKindFilter(c.key)}
                style={{
                  padding: "4px 10px", borderRadius: 999, fontSize: 12, fontWeight: 600, cursor: "pointer",
                  backgroundColor: active ? (st ? st.bg : "#0f172a") : "#fff",
                  color: active ? (st ? st.fg : "#fff") : "#475569",
                  border: `1px solid ${active ? (st ? st.border : "#0f172a") : "#cbd5e1"}`,
                }}
              >
                {c.label}
              </button>
            );
          })}
        </div>
        {allTags.length > 0 && (
          <select
            value={tagFilter}
            onChange={(e) => setTagFilter(e.target.value)}
            style={{ fontSize: 12, padding: "4px 8px", borderRadius: 6, border: "1px solid #cbd5e1", color: "#475569" }}
          >
            <option value="">All tags</option>
            {allTags.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        )}
        <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "#475569", cursor: "pointer", userSelect: "none" }}>
          <input type="checkbox" checked={showOnlyConnected} onChange={(e) => setShowOnlyConnected(e.target.checked)} />
          Show only connected products
        </label>
        {focusUri && (
          <button
            type="button"
            onClick={() => setFocusUri(null)}
            style={{ fontSize: 12, padding: "4px 10px", borderRadius: 999, border: "1px solid #cbd5e1", background: "#fff", color: "#475569", cursor: "pointer" }}
          >
            Clear focus
          </button>
        )}
        <span style={{ fontSize: 12, color: "#94a3b8", marginLeft: "auto" }}>
          {visNodes.length} products · {visEdges.length} dependencies
        </span>
      </div>

      {/* Legend */}
      <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginBottom: 8, fontSize: 11, color: "#64748b" }}>
        {(["source", "aggregate", "consumer"] as const).map((k) => (
          <span key={k} style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
            <span style={{ width: 12, height: 12, borderRadius: 3, background: KIND_STYLES[k].bg, border: `1px solid ${KIND_STYLES[k].border}` }} />
            {KIND_STYLES[k].label}
          </span>
        ))}
        <span style={{ color: "#94a3b8" }}>Arrows point downstream (source → consumer). Click a node to focus its lineage.</span>
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

const styles: Record<string, CSSProperties> = {
  empty: {
    display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
    textAlign: "center", padding: "64px 24px", border: "1px dashed #e2e8f0", borderRadius: 12,
  },
};
