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

/**
 * Dataset ERD view: discovered tables on the canvas, columns inside each
 * table card, and one edge per `:REFERENCES` foreign-key relation. Mirrors
 * the structure of MappingGraphView so embedding patterns stay consistent.
 *
 * Data is sourced from GET /api/projects/{id}/summary/dataset_graph.
 *
 * FK column-pair routing depends on `:FK_REFERENCES` edges produced by the
 * data-discovery-to-dcat-neo4j skill. Existing projects loaded before that
 * skill change will have populated `column_pairs[].src_col_uri` from the
 * legacy CSV columns property — this component falls back gracefully if a
 * pair is missing column URIs.
 */

interface ColumnInfo {
  uri: string;
  name: string;
  type: string | null;
  primary_key: boolean | null;
  ordinal: number | null;
}
interface DatasetInfo {
  uri: string;
  schema: string | null;
  table: string | null;
  columns: ColumnInfo[];
}
interface ColumnPair {
  src_col_name: string;
  dst_col_name: string;
  src_col_uri: string | null;
  dst_col_uri: string | null;
}
interface ReferenceEdge {
  src_dataset_uri: string;
  dst_dataset_uri: string;
  fk_name: string | null;
  on_delete: string | null;
  on_update: string | null;
  columns: ColumnPair[];
}
export interface DatasetGraphPayload {
  datasets: DatasetInfo[];
  references: ReferenceEdge[];
}

interface Props {
  projectId?: number;
  payload?: DatasetGraphPayload | null;
  readOnly?: boolean;
}

const TABLE_NODE_WIDTH = 260;
const COLUMN_ROW_HEIGHT = 24;
const TABLE_HEADER_HEIGHT = 38;
const ROW_GAP_X = 80;
const ROW_GAP_Y = 60;

interface ERDColumnRow {
  uri: string;
  name: string;
  type: string | null;
  primaryKey: boolean;
  isFk: boolean;
}

function TableNodeERD({ data }: NodeProps<Node<{
  title: string;
  subtitle?: string;
  columns: ERDColumnRow[];
}>>) {
  const accent = "#0369a1";
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
          backgroundColor: "#e0f2fe",
          color: accent,
          fontWeight: 700,
          fontSize: 12,
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          borderBottom: `1px solid ${accent}`,
        }}
      >
        <div style={{ textTransform: "uppercase", letterSpacing: 0.4, fontSize: 9 }}>Table</div>
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
        {data.columns.map((c, i) => (
          <div
            key={c.uri}
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
              backgroundColor: "#fff",
              color: "#0f172a",
            }}
          >
            <Handle type="source" position={Position.Right} id={`${c.uri}:r`} style={handleStyle(accent)} />
            <Handle type="target" position={Position.Left} id={`${c.uri}:l`} style={handleStyle(accent)} />
            <span style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
              {c.name}
              {c.primaryKey && (
                <span style={{ marginLeft: 6, fontSize: 9, color: "#7c3aed", fontWeight: 700 }}>PK</span>
              )}
              {c.isFk && (
                <span style={{ marginLeft: 6, fontSize: 9, color: "#0369a1", fontWeight: 700 }}>FK</span>
              )}
            </span>
            <code style={{ fontSize: 10, color: "#94a3b8", whiteSpace: "nowrap" }}>{c.type || ""}</code>
          </div>
        ))}
      </div>
    </div>
  );
}

function handleStyle(stroke: string) {
  return {
    background: stroke,
    width: 6,
    height: 6,
    border: "1px solid #fff",
  };
}

const NODE_TYPES = { erdTable: TableNodeERD };

function buildLayout(payload: DatasetGraphPayload): { nodes: Node[]; edges: Edge[] } {
  // Sorted alphabetically by schema.table for a stable layout.
  const sorted = [...payload.datasets].sort((a, b) => {
    const an = `${a.schema ?? ""}.${a.table ?? ""}`;
    const bn = `${b.schema ?? ""}.${b.table ?? ""}`;
    return an.localeCompare(bn);
  });

  // Mark FK columns by walking references and recording which source-side
  // columns are used as foreign keys. Adds the "FK" badge in the row.
  const fkColUris = new Set<string>();
  for (const r of payload.references) {
    for (const p of r.columns) {
      if (p.src_col_uri) fkColUris.add(p.src_col_uri);
    }
  }

  // Grid layout: ceil(sqrt(N)) columns wide. Each column is sized by
  // TABLE_NODE_WIDTH; row heights track the tallest table in the row.
  const cols = Math.max(1, Math.ceil(Math.sqrt(sorted.length || 1)));
  const tableHeight = (cols_count: number) => TABLE_HEADER_HEIGHT + cols_count * COLUMN_ROW_HEIGHT;

  // Compute per-row max height so rows don't overlap when tables vary in
  // length.
  const rowMaxHeights: number[] = [];
  for (let i = 0; i < sorted.length; i++) {
    const r = Math.floor(i / cols);
    const h = tableHeight(sorted[i].columns.length);
    rowMaxHeights[r] = Math.max(rowMaxHeights[r] || 0, h);
  }
  const rowYOffsets: number[] = [];
  let yCursor = 0;
  for (let r = 0; r < rowMaxHeights.length; r++) {
    rowYOffsets[r] = yCursor;
    yCursor += rowMaxHeights[r] + ROW_GAP_Y;
  }

  const nodes: Node[] = sorted.map((ds, i) => {
    const r = Math.floor(i / cols);
    const c = i % cols;
    return {
      id: ds.uri,
      type: "erdTable",
      position: {
        x: c * (TABLE_NODE_WIDTH + ROW_GAP_X),
        y: rowYOffsets[r],
      },
      data: {
        title: ds.table || ds.uri,
        subtitle: ds.schema || undefined,
        columns: ds.columns.map<ERDColumnRow>((col) => ({
          uri: col.uri,
          name: col.name,
          type: col.type,
          primaryKey: !!col.primary_key,
          isFk: fkColUris.has(col.uri),
        })),
      },
      draggable: true,
    };
  });

  const edges: Edge[] = [];
  payload.references.forEach((r, idx) => {
    // One edge per column-pair when we have full URIs; falls back to a
    // single dataset-to-dataset edge when column URIs aren't resolvable
    // (graph loaded before FK_REFERENCES was added — column URIs from CSV
    // parsing may still resolve fine via name lookup in the backend).
    const pairs = r.columns.filter((p) => p.src_col_uri && p.dst_col_uri);
    if (pairs.length === 0) {
      edges.push({
        id: `fk-${idx}`,
        source: r.src_dataset_uri,
        target: r.dst_dataset_uri,
        label: r.fk_name || undefined,
        style: { stroke: "#0369a1", strokeWidth: 1.5 },
        labelStyle: { fontSize: 10, fill: "#0369a1", fontWeight: 600 },
        labelBgStyle: { fill: "#fff" },
        labelBgPadding: [3, 4] as [number, number],
      });
      return;
    }
    pairs.forEach((p, i) => {
      edges.push({
        id: `fk-${idx}-${i}`,
        source: r.src_dataset_uri,
        sourceHandle: `${p.src_col_uri}:r`,
        target: r.dst_dataset_uri,
        targetHandle: `${p.dst_col_uri}:l`,
        label: i === 0 ? r.fk_name || undefined : undefined,
        style: { stroke: "#0369a1", strokeWidth: 1.5 },
        labelStyle: { fontSize: 10, fill: "#0369a1", fontWeight: 600 },
        labelBgStyle: { fill: "#fff" },
        labelBgPadding: [3, 4] as [number, number],
      });
    });
  });

  return { nodes, edges };
}

export default function DatasetERDView({
  projectId,
  payload: externalPayload,
}: Props) {
  const [fetched, setFetched] = useState<DatasetGraphPayload | null>(null);
  const [loading, setLoading] = useState<boolean>(externalPayload === undefined && !!projectId);
  const [error, setError] = useState<string | null>(null);

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
      .get(`/api/projects/${projectId}/summary/dataset_graph`)
      .then((res) => {
        if (cancelled) return;
        setFetched(res.data);
        setLoading(false);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Failed to load dataset graph");
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, externalPayload]);

  const payload = externalPayload !== undefined ? externalPayload : fetched;

  const { nodes, edges } = useMemo(() => {
    if (!payload) return { nodes: [], edges: [] };
    return buildLayout(payload);
  }, [payload]);

  if (loading) {
    return <div style={{ padding: 24, color: "#64748b", fontSize: 13 }}>Loading dataset graph…</div>;
  }
  if (error) {
    return (
      <div style={{ padding: 16, color: "#9a3412", fontSize: 13, backgroundColor: "#fff7ed", borderRadius: 6 }}>
        Couldn't load the graph: {error}
      </div>
    );
  }
  if (!payload || payload.datasets.length === 0) {
    return (
      <div style={{ padding: 24, color: "#64748b", fontSize: 13, textAlign: "center" }}>
        No datasets discovered yet — run the Data Discovery stage first.
      </div>
    );
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
        fitViewOptions={{ padding: 0.15 }}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={16} color="#e2e8f0" />
        <Controls showInteractive={false} />
      </ReactFlow>
      <ERDLegend referenceCount={payload.references.length} />
    </div>
  );
}

function ERDLegend({ referenceCount }: { referenceCount: number }) {
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
      }}
    >
      <div style={{ fontWeight: 700, marginBottom: 6, color: "#334155", textTransform: "uppercase", letterSpacing: 0.4, fontSize: 9 }}>
        ERD
      </div>
      <div style={{ color: "#475569", marginBottom: 3 }}>
        <span style={{ color: "#7c3aed", fontWeight: 700 }}>PK</span> primary key
      </div>
      <div style={{ color: "#475569", marginBottom: 3 }}>
        <span style={{ color: "#0369a1", fontWeight: 700 }}>FK</span> foreign key
      </div>
      <div style={{ marginTop: 6, paddingTop: 6, borderTop: "1px solid #e2e8f0", color: "#64748b", fontSize: 10 }}>
        {referenceCount} relationship{referenceCount === 1 ? "" : "s"}
      </div>
    </div>
  );
}
