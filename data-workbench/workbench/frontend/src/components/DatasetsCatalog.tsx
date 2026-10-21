import { useEffect, useMemo, useState } from "react";
import api from "../api/client";
import RelationshipKindChip from "./RelationshipKindChip";

// The marketplace "Datasets" sub-tab: discovered :Dataset nodes (from Discovery /
// Migration / in-flight product projects) shown as lighter-weight catalog entries.
// A dataset that has been promoted (its columns feed a product mapping) is
// excluded server-side and appears under Data Products instead.

interface DatasetCard {
  uri: string;
  name: string;
  schema: string;
  row_count: number | null;
  column_count: number;
  description: string;
  relationship_kind: string;
  project_code: string;
  project_name: string | null;
  archetype: string | null;
  project_id: number | null;
}

interface DatasetColumn {
  uri: string; name: string; type: string; nullable: boolean | null;
  primary_key: boolean | null; ordinal: number | null; description: string;
  profiling: { metric: string; value: unknown }[];
}
interface DatasetRef {
  table: string; schema: string; dataset_uri: string;
  columns: string | null; referenced_columns: string | null;
}
interface DatasetDetail extends DatasetCard {
  columns: DatasetColumn[];
  references_out: DatasetRef[];
  references_in: DatasetRef[];
  promoted: boolean;
}

const ARCHETYPE_LABEL: Record<string, string> = {
  dd: "Discovery", dq: "Data Quality", dmig: "Migration",
  "dpe-sa": "Source product", "dpe-cf": "Consumer product",
};

function ProvenanceChip({ card }: { card: DatasetCard }) {
  const label = (card.archetype && ARCHETYPE_LABEL[card.archetype]) || card.archetype || "Project";
  return (
    <span style={{ display: "inline-flex", alignItems: "center", padding: "2px 8px", borderRadius: 8,
      background: "#eef2ff", color: "#4338ca", fontSize: 11, fontWeight: 700 }}
      title={card.project_code}>
      {label}
    </span>
  );
}

export default function DatasetsCatalog() {
  const [datasets, setDatasets] = useState<DatasetCard[]>([]);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [archetypeFilter, setArchetypeFilter] = useState<string>("all");
  const [selectedUri, setSelectedUri] = useState<string | null>(null);
  const [detail, setDetail] = useState<DatasetDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  useEffect(() => {
    setLoading(true);
    api.get("/api/marketplace/datasets")
      .then((r) => setDatasets(r.data.datasets || []))
      .catch(() => setDatasets([]))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!selectedUri) { setDetail(null); return; }
    setDetailLoading(true);
    api.get("/api/marketplace/datasets/detail", { params: { uri: selectedUri } })
      .then((r) => setDetail(r.data))
      .catch(() => setDetail(null))
      .finally(() => setDetailLoading(false));
  }, [selectedUri]);

  const archetypes = useMemo(() => {
    const set = new Set(datasets.map((d) => d.archetype || "").filter(Boolean));
    return ["all", ...Array.from(set)];
  }, [datasets]);

  const filtered = datasets.filter((d) => {
    if (archetypeFilter !== "all" && (d.archetype || "") !== archetypeFilter) return false;
    if (!query.trim()) return true;
    const q = query.toLowerCase();
    return `${d.schema}.${d.name}`.toLowerCase().includes(q) || (d.project_code || "").toLowerCase().includes(q);
  });

  if (loading) return <div style={{ color: "#94a3b8", padding: 24 }}>Loading datasets…</div>;

  if (datasets.length === 0) {
    return (
      <div style={{ textAlign: "center", padding: "48px 20px", background: "#f8fafc", border: "1px dashed #cbd5e1", borderRadius: 12 }}>
        <div style={{ fontSize: 40, marginBottom: 10 }}>▤</div>
        <h3 style={{ margin: "0 0 8px", color: "#334155" }}>No datasets yet</h3>
        <p style={{ margin: 0, color: "#94a3b8", maxWidth: 460, marginInline: "auto", lineHeight: 1.5 }}>
          Run <strong>Data Discovery</strong> on a Discovery or Migration project to catalog its tables here.
          Discovered datasets appear as soon as they're loaded; once a dataset is promoted to a data product it
          moves to the <strong>Data Products</strong> tab.
        </p>
      </div>
    );
  }

  return (
    <div style={{ display: "grid", gridTemplateColumns: selectedUri ? "340px 1fr" : "repeat(auto-fill, minmax(300px, 1fr))", gap: 16 }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search datasets…"
          style={{ padding: "8px 11px", borderRadius: 8, border: "1px solid #cbd5e1", fontSize: 13 }}
        />
        {archetypes.length > 2 && (
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {archetypes.map((a) => (
              <button key={a} onClick={() => setArchetypeFilter(a)}
                style={{ padding: "3px 10px", borderRadius: 8, fontSize: 12, fontWeight: 600, cursor: "pointer",
                  border: "1px solid " + (archetypeFilter === a ? "#4338ca" : "#e2e8f0"),
                  background: archetypeFilter === a ? "#eef2ff" : "#fff",
                  color: archetypeFilter === a ? "#4338ca" : "#64748b" }}>
                {a === "all" ? "All" : (ARCHETYPE_LABEL[a] || a)}
              </button>
            ))}
          </div>
        )}
        {filtered.map((d) => (
          <div key={d.uri} onClick={() => setSelectedUri(d.uri)}
            style={{ padding: "14px 16px", borderRadius: 10, background: "#fff", cursor: "pointer",
              border: "1px solid " + (selectedUri === d.uri ? "#4338ca" : "#e2e8f0"),
              display: "flex", flexDirection: "column", gap: 6 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <span style={{ fontSize: 15, fontWeight: 700, color: "#0f172a" }}>{d.name}</span>
              {d.relationship_kind && <RelationshipKindChip kind={d.relationship_kind} />}
            </div>
            <div style={{ fontSize: 12, color: "#64748b", fontFamily: "monospace" }}>{d.schema}.{d.name}</div>
            {d.description && (
              <div style={{ fontSize: 12.5, color: "#64748b", lineHeight: 1.4, display: "-webkit-box",
                WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{d.description}</div>
            )}
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 2, flexWrap: "wrap" }}>
              <ProvenanceChip card={d} />
              <span style={{ fontSize: 11, color: "#94a3b8" }}>
                {d.column_count} cols{d.row_count != null ? ` · ${d.row_count.toLocaleString()} rows` : ""}
              </span>
            </div>
          </div>
        ))}
        {filtered.length === 0 && <div style={{ color: "#94a3b8", fontSize: 13, padding: 8 }}>No datasets match.</div>}
      </div>

      {selectedUri && (
        <div style={{ background: "#fff", border: "1px solid #e2e8f0", borderRadius: 12, padding: "18px 20px" }}>
          {detailLoading || !detail ? (
            <div style={{ color: "#94a3b8" }}>Loading…</div>
          ) : (
            <DatasetDetailView detail={detail} onClose={() => setSelectedUri(null)} />
          )}
        </div>
      )}
    </div>
  );
}

function LifecycleBadge({ promoted }: { promoted: boolean }) {
  const steps = ["Discovered", "Data Product"];
  const activeIdx = promoted ? 1 : 0;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
      {steps.map((s, i) => (
        <span key={s} style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ padding: "2px 10px", borderRadius: 20, fontSize: 11, fontWeight: 700,
            background: i === activeIdx ? "#dcfce7" : i < activeIdx ? "#e2e8f0" : "#f1f5f9",
            color: i === activeIdx ? "#166534" : "#94a3b8",
            border: "1px solid " + (i === activeIdx ? "#86efac" : "#e2e8f0") }}>{s}</span>
          {i < steps.length - 1 && <span style={{ color: "#cbd5e1" }}>→</span>}
        </span>
      ))}
    </div>
  );
}

function DatasetDetailView({ detail, onClose }: { detail: DatasetDetail; onClose: () => void }) {
  const cell = { padding: "6px 10px", fontSize: 12.5, borderBottom: "1px solid #f1f5f9", textAlign: "left" as const };
  const head = { ...cell, fontSize: 11, fontWeight: 700, color: "#64748b", textTransform: "uppercase" as const, letterSpacing: 0.4 };
  const refLine = (r: DatasetRef, dir: "→" | "←") =>
    `${dir} ${r.schema}.${r.table}${r.columns ? ` (${r.columns} ${dir === "→" ? "→" : "←"} ${r.referenced_columns})` : ""}`;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <h3 style={{ margin: 0, color: "#0f172a" }}>{detail.name}</h3>
            {detail.relationship_kind && <RelationshipKindChip kind={detail.relationship_kind} />}
            <ProvenanceChip card={detail} />
          </div>
          <div style={{ fontSize: 12.5, color: "#64748b", fontFamily: "monospace", marginTop: 3 }}>{detail.schema}.{detail.name}</div>
        </div>
        <button onClick={onClose} style={{ border: "1px solid #cbd5e1", background: "#fff", color: "#475569",
          borderRadius: 6, padding: "4px 10px", fontSize: 12, fontWeight: 600, cursor: "pointer" }}>Close</button>
      </div>

      <LifecycleBadge promoted={detail.promoted} />

      <div style={{ display: "flex", gap: 20, flexWrap: "wrap", fontSize: 13, color: "#334155" }}>
        <div><span style={{ color: "#94a3b8" }}>Rows: </span>{detail.row_count != null ? detail.row_count.toLocaleString() : "—"}</div>
        <div><span style={{ color: "#94a3b8" }}>Columns: </span>{detail.columns.length}</div>
        <div><span style={{ color: "#94a3b8" }}>Project: </span>{detail.project_name || detail.project_code || "—"}</div>
      </div>

      {detail.description && (
        <div style={{ fontSize: 13, color: "#475569", lineHeight: 1.5, background: "#f8fafc", borderRadius: 8, padding: "10px 12px" }}>
          {detail.description}
        </div>
      )}

      <div>
        <div style={{ fontSize: 12, fontWeight: 700, color: "#334155", marginBottom: 6 }}>Columns</div>
        <div style={{ overflowX: "auto", border: "1px solid #eef2f7", borderRadius: 8 }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr>
              <th style={head}>Name</th><th style={head}>Type</th><th style={head}>PK</th>
              <th style={head}>Nullable</th><th style={head}>Description</th><th style={head}>Profiling</th>
            </tr></thead>
            <tbody>
              {detail.columns.map((c) => (
                <tr key={c.uri || c.name}>
                  <td style={{ ...cell, fontWeight: 600 }}>{c.name}</td>
                  <td style={{ ...cell, fontFamily: "monospace", color: "#64748b" }}>{c.type}</td>
                  <td style={cell}>{c.primary_key ? "🔑" : ""}</td>
                  <td style={cell}>{c.nullable === false ? "no" : c.nullable === true ? "yes" : ""}</td>
                  <td style={{ ...cell, color: "#64748b" }}>{c.description}</td>
                  <td style={{ ...cell, color: "#94a3b8" }}>{c.profiling.length ? `${c.profiling.length} metrics` : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {(detail.references_out.length > 0 || detail.references_in.length > 0) && (
        <div>
          <div style={{ fontSize: 12, fontWeight: 700, color: "#334155", marginBottom: 6 }}>Relationships (FK)</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12.5, color: "#475569", fontFamily: "monospace" }}>
            {detail.references_out.map((r, i) => <div key={`o${i}`}>{refLine(r, "→")}</div>)}
            {detail.references_in.map((r, i) => <div key={`i${i}`}>{refLine(r, "←")}</div>)}
          </div>
        </div>
      )}

      {detail.promoted && (
        <div style={{ fontSize: 12.5, color: "#166534", background: "#f0fdf4", border: "1px solid #bbf7d0", borderRadius: 8, padding: "8px 12px" }}>
          This dataset has been promoted to a data product — see the <strong>Data Products</strong> tab.
        </div>
      )}
    </div>
  );
}
