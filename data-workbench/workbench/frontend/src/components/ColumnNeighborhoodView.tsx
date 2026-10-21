import { useEffect, useMemo, useState } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  Handle,
  Position,
  type Node,
  type Edge,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import api from "../api/client";
import { productTheme } from "../theme";

/**
 * Column neighborhood view: a single source column at the center, with its
 * lineage (sources → mappings → product columns), the rules attached to it,
 * and recent test results laid out around it.
 *
 * Data is sourced from GET /api/projects/{id}/summary/column_neighborhood?
 * col_uri={uri}. Source-column focus only for v1.
 */

interface FocusInfo {
  uri: string;
  name: string;
  data_type: string | null;
  primary_key: boolean | null;
  nullable: boolean | null;
  schema: string | null;
  table: string | null;
  dataset_uri: string | null;
  role: "source";
}
interface DescriptionInfo {
  uri: string;
  text: string | null;
  status: string | null;
}
interface ProductColumn {
  uri: string;
  name: string | null;
  dataset_uri: string | null;
  dataset_name: string | null;
}
interface MappingInfo {
  uri: string;
  status: string | null;
  transform_kind: string | null;
  transform_author: string | null;
  transform_expression: string | null;
  similarity_score: number | null;
  src_col_uri: string;
  dst_col_uri: string | null;
}
interface RuleInfo {
  uri: string;
  rule_type: string | null;
  severity: string | null;
  rule_source: string | null;
  status: string | null;
  path: string | null;
}
interface TestResultInfo {
  uri: string;
  expectation_type: string | null;
  success: boolean | null;
  failed_count: number | null;
  observed_value: string | null;
  run_uri: string | null;
  framework: string | null;
  started_at: string | null;
}
export interface ColumnNeighborhoodPayload {
  focus: FocusInfo;
  description: DescriptionInfo | null;
  products: ProductColumn[];
  mappings: MappingInfo[];
  rules: RuleInfo[];
  test_results: TestResultInfo[];
}

interface Props {
  projectId: number;
  colUri: string;
  readOnly?: boolean;
}

const STATUS_STROKE: Record<string, string> = {
  approved: "#16a34a",
  pending_review: "#f59e0b",
  steward_review: "#7c3aed",
  rejected: "#dc2626",
};
const RULE_SOURCE_COLOR: Record<string, string> = {
  observation: "#0369a1",
  domain: "#7c3aed",
  user: "#16a34a",
  spec: "#0891b2",
};

// ── Node renderers ─────────────────────────────────────────────────────────

function FocusNode({ data }: NodeProps<Node<{ focus: FocusInfo; description: DescriptionInfo | null }>>) {
  const { focus, description } = data;
  return (
    <div
      style={{
        width: 280,
        borderRadius: 10,
        border: "2px solid #0369a1",
        backgroundColor: "#fff",
        boxShadow: "0 2px 8px rgba(15,23,42,0.12)",
        fontFamily: "system-ui, sans-serif",
      }}
    >
      <Handle type="target" position={Position.Left} id="focus:l" style={dotStyle("#0369a1")} />
      <Handle type="source" position={Position.Right} id="focus:r" style={dotStyle("#0369a1")} />
      <Handle type="source" position={Position.Bottom} id="focus:b" style={dotStyle("#0369a1")} />
      <div
        style={{
          padding: "8px 12px",
          backgroundColor: "#e0f2fe",
          borderBottom: "1px solid #0369a1",
          color: "#0369a1",
          fontWeight: 700,
          fontSize: 12,
        }}
      >
        <div style={{ textTransform: "uppercase", letterSpacing: 0.4, fontSize: 9 }}>Column</div>
        <div style={{ fontSize: 14 }}>{focus.name}</div>
        <div style={{ fontWeight: 400, fontSize: 11, color: "#475569" }}>
          {focus.schema}.{focus.table}
        </div>
      </div>
      <div style={{ padding: "8px 12px", fontSize: 11, color: "#475569" }}>
        <div>
          <strong style={{ color: "#0f172a" }}>Type:</strong> <code>{focus.data_type || "?"}</code>
        </div>
        {focus.primary_key && (
          <div style={{ marginTop: 2, color: "#7c3aed", fontWeight: 600 }}>PRIMARY KEY</div>
        )}
        {focus.nullable === false && <div style={{ marginTop: 2, color: "#9a3412" }}>NOT NULL</div>}
        {description?.text && (
          <div style={{ marginTop: 6, paddingTop: 6, borderTop: "1px solid #e2e8f0", fontStyle: "italic", color: "#334155" }}>
            "{description.text}"
            {description.status && (
              <span style={{ marginLeft: 6, fontSize: 9, color: "#94a3b8", fontWeight: 600, textTransform: "uppercase" }}>
                [{description.status}]
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function SourceTableNode({ data }: NodeProps<Node<{ schema: string | null; table: string | null }>>) {
  return (
    <div
      style={{
        width: 200,
        borderRadius: 8,
        border: "1px solid #cbd5e1",
        backgroundColor: "#fff",
        boxShadow: "0 1px 4px rgba(15,23,42,0.05)",
        fontFamily: "system-ui, sans-serif",
        padding: "10px 12px",
        fontSize: 11,
      }}
    >
      <Handle type="source" position={Position.Right} id="src:r" style={dotStyle("#94a3b8")} />
      <div style={{ textTransform: "uppercase", letterSpacing: 0.4, fontSize: 9, color: "#64748b" }}>From</div>
      <div style={{ fontWeight: 600, color: "#0f172a" }}>{data.table || "?"}</div>
      <div style={{ color: "#94a3b8" }}>{data.schema || ""}</div>
    </div>
  );
}

function ProductColumnNode({ data }: NodeProps<Node<{ name: string | null; datasetName: string | null }>>) {
  return (
    <div
      style={{
        width: 200,
        borderRadius: 8,
        border: `1px solid ${productTheme.accent}`,
        backgroundColor: "#fff",
        boxShadow: "0 1px 4px rgba(15,23,42,0.05)",
        fontFamily: "system-ui, sans-serif",
        padding: "10px 12px",
        fontSize: 11,
      }}
    >
      <Handle type="target" position={Position.Left} id="prod:l" style={dotStyle(productTheme.accent)} />
      <div style={{ textTransform: "uppercase", letterSpacing: 0.4, fontSize: 9, color: productTheme.accent }}>
        Product Column
      </div>
      <div style={{ fontWeight: 600, color: "#0f172a" }}>{data.name || "?"}</div>
      {data.datasetName && <div style={{ color: "#94a3b8" }}>{data.datasetName}</div>}
    </div>
  );
}

function RuleNode({ data }: NodeProps<Node<{ rule: RuleInfo }>>) {
  const { rule } = data;
  const color = RULE_SOURCE_COLOR[rule.rule_source || ""] || "#64748b";
  return (
    <div
      style={{
        width: 180,
        borderRadius: 6,
        border: `1px solid ${color}`,
        backgroundColor: "#fff",
        padding: "6px 8px",
        fontSize: 10,
        fontFamily: "system-ui, sans-serif",
      }}
    >
      <Handle type="target" position={Position.Top} id="rule:t" style={dotStyle(color)} />
      <div style={{ textTransform: "uppercase", letterSpacing: 0.3, fontSize: 8, color, fontWeight: 700 }}>
        {rule.rule_source || "rule"}
      </div>
      <div style={{ fontWeight: 600, color: "#0f172a" }}>{rule.rule_type || "rule"}</div>
      <div style={{ color: "#64748b", display: "flex", gap: 6 }}>
        <span>{rule.severity || ""}</span>
        {rule.status && <span style={{ color: rule.status === "approved" ? "#16a34a" : "#94a3b8" }}>· {rule.status}</span>}
      </div>
    </div>
  );
}

function TestNode({ data }: NodeProps<Node<{ test: TestResultInfo }>>) {
  const { test } = data;
  const color = test.success ? "#16a34a" : "#dc2626";
  return (
    <div
      style={{
        width: 180,
        borderRadius: 6,
        border: `1px solid ${color}`,
        backgroundColor: "#fff",
        padding: "6px 8px",
        fontSize: 10,
        fontFamily: "system-ui, sans-serif",
      }}
    >
      <Handle type="target" position={Position.Top} id="test:t" style={dotStyle(color)} />
      <div style={{ textTransform: "uppercase", letterSpacing: 0.3, fontSize: 8, color, fontWeight: 700 }}>
        {test.success ? "Pass" : "Fail"}
      </div>
      <div style={{ fontWeight: 600, color: "#0f172a", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
        {test.expectation_type || "test"}
      </div>
      <div style={{ color: "#64748b" }}>
        {test.framework}
        {test.failed_count != null && !test.success && <span> · {test.failed_count} failed</span>}
      </div>
    </div>
  );
}

function dotStyle(color: string) {
  return { background: color, width: 8, height: 8, border: "1px solid #fff" };
}

const NODE_TYPES = {
  focus: FocusNode,
  srcTable: SourceTableNode,
  prodCol: ProductColumnNode,
  rule: RuleNode,
  test: TestNode,
};

// ── Layout ─────────────────────────────────────────────────────────────────

const COL_X = {
  source: 0,
  focus: 320,
  product: 700,
};
const FOCUS_Y = 60;
const RULE_Y = 320;
const TEST_Y = 460;
const PRODUCT_GAP_Y = 80;

function buildLayout(payload: ColumnNeighborhoodPayload): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  // Focus node (center)
  nodes.push({
    id: "focus",
    type: "focus",
    position: { x: COL_X.focus, y: FOCUS_Y },
    data: { focus: payload.focus, description: payload.description },
    draggable: true,
  });

  // Source dataset card on the left
  if (payload.focus.dataset_uri) {
    nodes.push({
      id: "src",
      type: "srcTable",
      position: { x: COL_X.source, y: FOCUS_Y + 40 },
      data: { schema: payload.focus.schema, table: payload.focus.table },
      draggable: true,
    });
    edges.push({
      id: "src-focus",
      source: "src",
      sourceHandle: "src:r",
      target: "focus",
      targetHandle: "focus:l",
      style: { stroke: "#94a3b8", strokeWidth: 1.2, strokeDasharray: "4 3" },
      label: "HAS_COLUMN",
      labelStyle: { fontSize: 9, fill: "#64748b" },
      labelBgStyle: { fill: "#fff" },
      labelBgPadding: [2, 3] as [number, number],
    });
  }

  // Product columns stacked on the right; one edge per mapping
  payload.products.forEach((p, i) => {
    const id = `prod:${p.uri}`;
    nodes.push({
      id,
      type: "prodCol",
      position: { x: COL_X.product, y: FOCUS_Y + i * PRODUCT_GAP_Y },
      data: { name: p.name, datasetName: p.dataset_name },
      draggable: true,
    });
  });
  payload.mappings.forEach((m) => {
    if (!m.dst_col_uri) return;
    const stroke = STATUS_STROKE[m.status || ""] || "#94a3b8";
    edges.push({
      id: `map:${m.uri}`,
      source: "focus",
      sourceHandle: "focus:r",
      target: `prod:${m.dst_col_uri}`,
      targetHandle: "prod:l",
      style: { stroke, strokeWidth: 1.5 },
      label: m.transform_kind && m.transform_kind !== "direct" ? m.transform_kind : undefined,
      labelStyle: { fontSize: 10, fill: stroke, fontWeight: 600 },
      labelBgStyle: { fill: "#fff" },
      labelBgPadding: [3, 4] as [number, number],
    });
  });

  // Rules below the focus, spread horizontally
  payload.rules.forEach((r, i) => {
    const id = `rule:${r.uri}`;
    nodes.push({
      id,
      type: "rule",
      position: { x: COL_X.focus + (i - (payload.rules.length - 1) / 2) * 200, y: RULE_Y },
      data: { rule: r },
      draggable: true,
    });
    edges.push({
      id: `e-${id}`,
      source: "focus",
      sourceHandle: "focus:b",
      target: id,
      targetHandle: "rule:t",
      style: {
        stroke: RULE_SOURCE_COLOR[r.rule_source || ""] || "#94a3b8",
        strokeWidth: 1.2,
      },
    });
  });

  // Tests below rules
  payload.test_results.forEach((t, i) => {
    const id = `test:${t.uri}`;
    nodes.push({
      id,
      type: "test",
      position: { x: COL_X.focus + (i - (payload.test_results.length - 1) / 2) * 200, y: TEST_Y },
      data: { test: t },
      draggable: true,
    });
    edges.push({
      id: `e-${id}`,
      source: "focus",
      sourceHandle: "focus:b",
      target: id,
      targetHandle: "test:t",
      style: {
        stroke: t.success ? "#16a34a" : "#dc2626",
        strokeWidth: 1.2,
        strokeDasharray: "3 3",
      },
    });
  });

  return { nodes, edges };
}

// ── Component ──────────────────────────────────────────────────────────────

export default function ColumnNeighborhoodView({ projectId, colUri }: Props) {
  const [payload, setPayload] = useState<ColumnNeighborhoodPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setPayload(null);
    api
      .get(`/api/projects/${projectId}/summary/column_neighborhood`, { params: { col_uri: colUri } })
      .then((res) => {
        if (cancelled) return;
        setPayload(res.data);
        setLoading(false);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Failed to load neighborhood");
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, colUri]);

  const { nodes, edges } = useMemo(() => {
    if (!payload) return { nodes: [], edges: [] };
    return buildLayout(payload);
  }, [payload]);

  if (loading) {
    return <div style={{ padding: 24, color: "#64748b", fontSize: 13 }}>Loading column neighborhood…</div>;
  }
  if (error) {
    return (
      <div style={{ padding: 16, color: "#9a3412", fontSize: 13, backgroundColor: "#fff7ed", borderRadius: 6 }}>
        Couldn't load the neighborhood: {error}
      </div>
    );
  }
  if (!payload) {
    return null;
  }

  return (
    <div
      style={{
        position: "relative",
        height: 620,
        border: "1px solid #e2e8f0",
        borderRadius: 8,
        backgroundColor: "#f8fafc",
      }}
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={16} color="#e2e8f0" />
        <Controls showInteractive={false} />
      </ReactFlow>
      <NeighborhoodSummary payload={payload} />
    </div>
  );
}

function NeighborhoodSummary({ payload }: { payload: ColumnNeighborhoodPayload }) {
  return (
    <div
      style={{
        position: "absolute",
        right: 12,
        top: 12,
        padding: 10,
        borderRadius: 6,
        backgroundColor: "rgba(255,255,255,0.95)",
        border: "1px solid #e2e8f0",
        fontSize: 11,
        boxShadow: "0 2px 6px rgba(15,23,42,0.06)",
        zIndex: 10,
        minWidth: 140,
      }}
    >
      <div style={{ fontWeight: 700, marginBottom: 6, color: "#334155", textTransform: "uppercase", letterSpacing: 0.4, fontSize: 9 }}>
        Neighborhood
      </div>
      <Row label="Mappings" value={payload.mappings.length} />
      <Row label="Products" value={payload.products.length} />
      <Row label="Rules" value={payload.rules.length} />
      <Row label="Tests" value={payload.test_results.length} />
    </div>
  );
}

function Row({ label, value }: { label: string; value: number }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", color: "#475569", marginBottom: 2 }}>
      <span>{label}</span>
      <strong style={{ color: "#0f172a" }}>{value}</strong>
    </div>
  );
}
