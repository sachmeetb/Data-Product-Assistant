// Per-table metadata-descriptions modal for the Connected Estate surface.
// Shows the LLM-generated table description + per-column descriptions for one
// scanned dataset. Self-contained (own styles); reads data already loaded by the
// scan drill-down, so it needs no API call. Adapted to estate data: estate
// columns carry a single native `data_type`, `nullable`, and `classification`
// (no logical/physical split, no PK flag), and datasets carry only a structural
// `relation_kind` (table/view) — NOT a semantic relationshipKind — so this stays
// platform-neutral and does not import the marketplace dprod model.
import { useEffect, type CSSProperties } from "react";

interface Col {
  uri: string;
  name: string;
  data_type: string | null;
  nullable: boolean | null;
  classification: string | null;
  description?: string | null;
}
interface Dataset {
  uri: string;
  database: string;
  schema: string;
  table: string;
  relation_kind: string;
  description?: string | null;
  columns: Col[];
}

const m: Record<string, CSSProperties> = {
  backdrop: {
    position: "fixed", inset: 0, background: "rgba(15,23,42,0.45)",
    display: "flex", alignItems: "flex-start", justifyContent: "center",
    padding: "6vh 16px", zIndex: 1000,
  },
  card: {
    background: "#fff", borderRadius: 10, maxWidth: 900, width: "100%",
    maxHeight: "84vh", overflow: "auto", boxShadow: "0 12px 40px rgba(15,23,42,0.28)",
    padding: 20,
  },
  headRow: { display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" },
  title: { fontSize: 18, fontWeight: 800, color: "#0f172a" },
  kindChip: {
    fontSize: 10, fontWeight: 700, padding: "1px 7px", borderRadius: 4,
    background: "#ede9fe", color: "#6d28d9", textTransform: "uppercase",
  },
  colCount: { fontSize: 12, color: "#64748b" },
  qualified: { fontSize: 11, color: "#94a3b8", marginTop: 2 },
  tableDesc: { fontSize: 13, color: "#334155", margin: "10px 0 14px", lineHeight: 1.45 },
  close: {
    marginLeft: "auto", fontSize: 13, fontWeight: 700, border: "1px solid #cbd5e1",
    background: "#fff", color: "#334155", borderRadius: 6, padding: "5px 12px", cursor: "pointer",
  },
  table: { borderCollapse: "collapse", width: "100%" },
  th: {
    textAlign: "left", fontSize: 11, color: "#64748b", fontWeight: 700,
    padding: "5px 8px", textTransform: "uppercase", borderBottom: "1px solid #e2e8f0",
  },
  td: { fontSize: 12.5, color: "#334155", padding: "6px 8px", borderTop: "1px solid #eef2f7", verticalAlign: "top" },
  prop: { fontSize: 12.5, color: "#0f172a", fontWeight: 600, padding: "6px 8px", borderTop: "1px solid #eef2f7", verticalAlign: "top" },
  type: { fontFamily: "monospace", fontSize: 12, color: "#7c3aed" },
  pii: { fontSize: 10, fontWeight: 700, padding: "1px 6px", borderRadius: 4, background: "#fee2e2", color: "#991b1b" },
  empty: { color: "#94a3b8" },
};

export default function EstateTableDescriptionsModal({
  dataset,
  onClose,
}: {
  dataset: Dataset;
  onClose: () => void;
}) {
  // Close on Escape.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const qualified = [dataset.database, dataset.schema, dataset.table].filter(Boolean).join(".");

  return (
    <div style={m.backdrop} onClick={onClose}>
      <div style={m.card} onClick={(e) => e.stopPropagation()}>
        <div style={m.headRow}>
          <span style={m.title}>{dataset.table}</span>
          {dataset.relation_kind ? <span style={m.kindChip}>{dataset.relation_kind}</span> : null}
          <span style={m.colCount}>{dataset.columns.length} columns</span>
          <button style={m.close} onClick={onClose}>✕ Close</button>
        </div>
        {qualified ? <div style={m.qualified}>{qualified}</div> : null}
        <div style={m.tableDesc}>
          {dataset.description
            ? dataset.description
            : <span style={m.empty}>No table description generated yet.</span>}
        </div>
        <table style={m.table}>
          <thead>
            <tr>
              <th style={m.th}>Property</th>
              <th style={m.th}>Type</th>
              <th style={m.th}>Nullable</th>
              <th style={m.th}>Description</th>
            </tr>
          </thead>
          <tbody>
            {dataset.columns.map((c) => (
              <tr key={c.uri}>
                <td style={m.prop}>
                  {c.name}
                  {c.classification === "pii" ? <span style={{ ...m.pii, marginLeft: 6 }}>PII</span> : null}
                </td>
                <td style={m.td}><span style={m.type}>{c.data_type || "—"}</span></td>
                <td style={m.td}>{c.nullable === false ? "not null" : "nullable"}</td>
                <td style={m.td}>
                  {c.description ? c.description : <span style={m.empty}>—</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
