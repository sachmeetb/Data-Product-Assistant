import { useEffect, useMemo, useState } from "react";
import api from "../api/client";
import ModalShell from "./ModalShell";
import type { SourceColumn } from "../types";

interface UnmappedTarget {
  column_uri: string;
  column_name: string;
  product_name: string;
  dataset_name?: string | null;
  data_type?: string | null;
}

interface Props {
  projectId: number;
  availableSources: SourceColumn[];
  onClose: () => void;
  /** Fired after the mapping is created, with the product column URI so the
   *  parent can open the transform editor for it. */
  onCreated: (productColUri: string) => void;
}

const ACCENT = "#7c3aed";
const listBox: React.CSSProperties = {
  maxHeight: 240, overflowY: "auto", border: "1px solid #e2e8f0", borderRadius: 6,
  backgroundColor: "#f8fafc", padding: 4,
};
const search: React.CSSProperties = {
  width: "100%", padding: "6px 8px", fontSize: 12, border: "1px solid #e2e8f0",
  borderRadius: 6, marginBottom: 6,
};

/** Wire a mapping by SEARCH instead of hunting the graph — for products with many
 *  columns. Pick an unmapped target product column + a source column, both via a
 *  filter box; Create makes a direct mapping (through the existing
 *  /reviews/unmapped_columns choke point) and hands the parent the product column
 *  URI so it can open the transform editor to refine it. */
export default function AddMappingDialog({ projectId, availableSources, onClose, onCreated }: Props) {
  const [targets, setTargets] = useState<UnmappedTarget[]>([]);
  const [loading, setLoading] = useState(true);
  const [tFilter, setTFilter] = useState("");
  const [sFilter, setSFilter] = useState("");
  const [target, setTarget] = useState("");   // product column URI
  const [source, setSource] = useState("");    // source column URI
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const res = await api.get(`/api/projects/${projectId}/reviews/unmapped_columns`);
        setTargets(res.data.items ?? []);
      } catch {
        setTargets([]);
      }
      setLoading(false);
    })();
  }, [projectId]);

  const filteredTargets = useMemo(() => {
    const q = tFilter.trim().toLowerCase();
    if (!q) return targets;
    return targets.filter((t) => `${t.column_name} ${t.product_name} ${t.data_type ?? ""}`.toLowerCase().includes(q));
  }, [targets, tFilter]);

  const filteredSources = useMemo(() => {
    const q = sFilter.trim().toLowerCase();
    if (!q) return availableSources;
    return availableSources.filter((s) => `${s.name} ${s.column_name ?? ""} ${s.data_type ?? ""}`.toLowerCase().includes(q));
  }, [availableSources, sFilter]);

  const create = async () => {
    if (!target || !source) return;
    setCreating(true);
    setError(null);
    try {
      await api.post(`/api/projects/${projectId}/reviews/unmapped_columns`, {
        product_col_uri: target,
        source_col_uris: [source],
        transform_kind: "direct",
        transform_expression: "",
        transform_inputs: [source],
        transform_params: {},
        transform_decorators: {},
        rationale: "Manually wired (add mapping)",
        reviewer: "workbench-engineer",
      });
      onCreated(target);
    } catch {
      setError("Couldn't create the mapping. Try again.");
      setCreating(false);
    }
  };

  const labelId = "add-mapping-title";
  const colHead: React.CSSProperties = { fontSize: 12, fontWeight: 700, color: "#334155", marginBottom: 6 };

  const rowStyle = (selected: boolean): React.CSSProperties => ({
    display: "block", padding: "5px 8px", borderRadius: 4, cursor: "pointer", fontSize: 12,
    backgroundColor: selected ? "#ede9fe" : "transparent",
  });

  return (
    <ModalShell open onClose={onClose} closeDisabled={creating} labelledById={labelId} width={760}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12 }}>
        <h3 id={labelId} style={{ margin: 0, fontSize: 16, color: "#0f172a" }}>Add a mapping</h3>
        <span style={{ fontSize: 12, color: "#94a3b8" }}>Search a target &amp; a source, then Create.</span>
      </div>

      <div style={{ display: "flex", gap: 12, alignItems: "stretch" }}>
        {/* Source (left) → Target (right) reads in the same direction as the card. */}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={colHead}>Source column</div>
          <input style={search} placeholder="Search source columns…" value={sFilter} onChange={(e) => setSFilter(e.target.value)} />
          <div style={listBox}>
            {filteredSources.length === 0 && <div style={{ fontSize: 12, color: "#94a3b8", padding: 8 }}>No matching source columns.</div>}
            {filteredSources.map((s) => (
              <label key={s.uri} style={rowStyle(source === s.uri)} title={s.description || undefined}>
                <input type="radio" name="add-map-source" checked={source === s.uri} onChange={() => setSource(s.uri)} style={{ marginRight: 8 }} />
                <code style={{ fontSize: 11 }}>{s.name}</code>
                {s.data_type && <span style={{ fontSize: 10, color: "#94a3b8", marginLeft: 6 }}>{s.data_type}</span>}
              </label>
            ))}
          </div>
        </div>

        <div style={{ alignSelf: "center", fontSize: 22, color: "#94a3b8" }} aria-hidden>→</div>

        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={colHead}>Target product column <span style={{ fontWeight: 400, color: "#94a3b8" }}>(unmapped)</span></div>
          <input style={search} placeholder="Search product columns…" value={tFilter} onChange={(e) => setTFilter(e.target.value)} />
          <div style={listBox}>
            {loading && <div style={{ fontSize: 12, color: "#94a3b8", padding: 8 }}>Loading…</div>}
            {!loading && filteredTargets.length === 0 && (
              <div style={{ fontSize: 12, color: "#94a3b8", padding: 8 }}>
                {targets.length === 0 ? "No unmapped columns — every product column is already mapped." : "No matching product columns."}
              </div>
            )}
            {filteredTargets.map((t) => (
              <label key={t.column_uri} style={rowStyle(target === t.column_uri)}>
                <input type="radio" name="add-map-target" checked={target === t.column_uri} onChange={() => setTarget(t.column_uri)} style={{ marginRight: 8 }} />
                <span style={{ fontWeight: 600 }}>{t.column_name}</span>
                {t.data_type && <span style={{ fontSize: 10, color: "#94a3b8", marginLeft: 6 }}>{t.data_type}</span>}
              </label>
            ))}
          </div>
        </div>
      </div>

      {error && <div style={{ fontSize: 12, color: "#dc2626" }}>{error}</div>}

      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, borderTop: "1px solid #f1f5f9", paddingTop: 12 }}>
        <button type="button" onClick={onClose} disabled={creating}
          style={{ padding: "7px 16px", fontSize: 13, borderRadius: 6, border: "1px solid #cbd5e1", backgroundColor: "#fff", color: "#334155", cursor: "pointer" }}>
          Cancel
        </button>
        <button type="button" onClick={create} disabled={creating || !target || !source}
          style={{ padding: "7px 18px", fontSize: 13, fontWeight: 600, borderRadius: 6, border: "none", backgroundColor: creating || !target || !source ? "#c4b5fd" : ACCENT, color: "#fff", cursor: creating || !target || !source ? "default" : "pointer" }}>
          {creating ? "Creating…" : "Create & configure"}
        </button>
      </div>
    </ModalShell>
  );
}
