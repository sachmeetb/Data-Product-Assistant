import { useEffect, useState } from "react";
import api from "../api/client";

/** The dataset-level shape returned by GET /api/projects/{id}/dataset-transform. */
interface ShapeData {
  filter: string;
  filter_intent: string;
  dedupe: { keys: string[]; order_by?: string; direction?: string } | null;
  joins: Array<{ alias: string; dataset_uri: string; kind: string; predicate: string }>;
  grouping_keys: string[];
  scd_policy: { type: string; effective_column?: string; expiration_column?: string; add_is_current?: boolean } | null;
  suppressed_columns: string[];
  grain_prose: string;
}

interface DatasetOption { uri: string; name: string; }

const ACCENT = "#0ea5e9"; // sky — the serving/dataset accent

const cardStyle: React.CSSProperties = {
  border: "1px solid #e2e8f0", borderRadius: 10, backgroundColor: "#fff", padding: 14,
};
const stageTitle: React.CSSProperties = { fontSize: 14, fontWeight: 700, color: "#0f172a" };
const subtitle: React.CSSProperties = { fontSize: 12, color: "#64748b", marginTop: 2 };
const btn: React.CSSProperties = {
  padding: "5px 12px", fontSize: 12, fontWeight: 600, borderRadius: 6,
  border: "1px solid #cbd5e1", backgroundColor: "#fff", color: "#334155", cursor: "pointer",
};
const primaryBtn: React.CSSProperties = { ...btn, border: "none", backgroundColor: "#16a34a", color: "#fff" };
const inputStyle: React.CSSProperties = {
  padding: "6px 8px", fontSize: 13, border: "1px solid #e2e8f0", borderRadius: 6, width: "100%",
};

const Arrow = () => (
  <div style={{ textAlign: "center", color: "#cbd5e1", fontSize: 16, lineHeight: "10px", padding: "2px 0" }}>▼</div>
);

/** Vertical card holding one pipeline stage, with an Edit/Save affordance. */
function StageShell({
  title, subtitle: sub, editing, onEdit, onCancel, onSave, saving, canSave = true, children,
}: {
  title: string; subtitle?: string; editing: boolean;
  onEdit: () => void; onCancel: () => void; onSave: () => void; saving?: boolean; canSave?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div style={cardStyle}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
        <div>
          <div style={stageTitle}>{title}</div>
          {sub && <div style={subtitle}>{sub}</div>}
        </div>
        {!editing ? (
          onEdit && <button style={btn} onClick={onEdit}>Edit</button>
        ) : (
          <div style={{ display: "flex", gap: 6 }}>
            <button style={btn} onClick={onCancel} disabled={saving}>Cancel</button>
            <button style={primaryBtn} onClick={onSave} disabled={saving || !canSave}>{saving ? "Saving…" : "Save"}</button>
          </div>
        )}
      </div>
      <div style={{ marginTop: 10 }}>{children}</div>
    </div>
  );
}

const splitList = (s: string): string[] => s.split(",").map((x) => x.trim()).filter(Boolean);

export default function DatasetShapePanel({ projectId }: { projectId: number }) {
  const [datasets, setDatasets] = useState<DatasetOption[]>([]);
  const [selectedUri, setSelectedUri] = useState<string>("");
  const [shape, setShape] = useState<ShapeData | null>(null);
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  // Editor scratch state (only the editing stage's fields are live).
  const [dFilter, setDFilter] = useState("");
  const [dIntent, setDIntent] = useState("");
  const [interpreting, setInterpreting] = useState(false);
  const [dedupeKeys, setDedupeKeys] = useState("");
  const [dedupeOrder, setDedupeOrder] = useState("");
  const [dedupeDir, setDedupeDir] = useState("desc");
  const [grouping, setGrouping] = useState("");
  const [scdType, setScdType] = useState("");
  const [scdEff, setScdEff] = useState("");
  const [scdExp, setScdExp] = useState("");
  const [suppress, setSuppress] = useState("");

  useEffect(() => {
    (async () => {
      try {
        const res = await api.get(`/api/projects/${projectId}/dataset-transform/output-datasets`);
        const ds: DatasetOption[] = res.data.datasets || [];
        setDatasets(ds);
        if (ds.length > 0) setSelectedUri(ds[0].uri);
      } catch {
        setDatasets([]);
      }
    })();
  }, [projectId]);

  const loadShape = async (uri: string) => {
    if (!uri) return;
    setLoading(true);
    setEditing(null);
    try {
      const res = await api.get(`/api/projects/${projectId}/dataset-transform`, { params: { output_dataset_uri: uri } });
      setShape(res.data as ShapeData);
    } catch {
      setShape(null);
    }
    setLoading(false);
  };

  useEffect(() => {
    if (selectedUri) loadShape(selectedUri);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedUri]);

  const beginEdit = (stage: string) => {
    if (!shape) return;
    setNote(null);
    setDFilter(shape.filter);
    setDIntent(shape.filter_intent);
    setDedupeKeys((shape.dedupe?.keys || []).join(", "));
    setDedupeOrder(shape.dedupe?.order_by || "");
    setDedupeDir(shape.dedupe?.direction || "desc");
    setGrouping(shape.grouping_keys.join(", "));
    setScdType(shape.scd_policy?.type || "");
    setScdEff(shape.scd_policy?.effective_column || "");
    setScdExp(shape.scd_policy?.expiration_column || "");
    setSuppress(shape.suppressed_columns.join(", "));
    setEditing(stage);
  };

  const afterSave = (data: ShapeData, label: string) => {
    setShape(data);
    setEditing(null);
    setSaving(false);
    setNote(`${label} saved. A deployed product will need re-deploying.`);
  };

  const interpretFilter = async () => {
    if (!dIntent.trim()) return;
    setInterpreting(true);
    try {
      const res = await api.post(`/api/filter-intent/interpret`, {
        intent: dIntent.trim(), project_id: projectId, output_dataset_uri: selectedUri,
      });
      if (res.data?.predicate) setDFilter(res.data.predicate);
    } catch { /* leave the predicate as-is */ }
    setInterpreting(false);
  };

  const saveFilter = async () => {
    setSaving(true);
    try {
      const res = await api.put(`/api/projects/${projectId}/dataset-transform/filter`, {
        output_dataset_uri: selectedUri, filter: dFilter,
      });
      // filter PUT returns {filter} only; re-read the full shape.
      await loadShape(selectedUri);
      afterSave({ ...(shape as ShapeData), filter: res.data.filter }, "Filter");
    } catch (e) {
      setSaving(false);
      const detail = (e as { response?: { data?: { message?: string } } })?.response?.data?.message;
      setNote(detail || "Filter save failed (must be a valid SQL predicate, not prose).");
    }
  };

  const saveShapeField = async (body: Record<string, unknown>, label: string) => {
    setSaving(true);
    try {
      const res = await api.put(`/api/projects/${projectId}/dataset-transform/shape`, {
        output_dataset_uri: selectedUri, ...body,
      });
      afterSave(res.data as ShapeData, label);
    } catch (e) {
      setSaving(false);
      const errs = (e as { response?: { data?: { detail?: { errors?: string[] } } } })?.response?.data?.detail?.errors;
      setNote(errs ? errs.join("; ") : `${label} save failed.`);
    }
  };

  if (datasets.length === 0) {
    return (
      <div style={{ padding: 16, color: "#94a3b8", fontSize: 13 }}>
        {loading ? "Loading…" : "No output datasets yet — run ODCS → DProd first, then author the dataset shape here."}
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: "#334155" }}>Dataset shape for</span>
        {datasets.length > 1 ? (
          <select value={selectedUri} onChange={(e) => setSelectedUri(e.target.value)} style={{ ...inputStyle, width: "auto" }}>
            {datasets.map((d) => <option key={d.uri} value={d.uri}>{d.name}</option>)}
          </select>
        ) : (
          <code style={{ fontSize: 13, color: ACCENT }}>{datasets[0].name}</code>
        )}
        <span style={{ fontSize: 11, color: "#94a3b8" }}>
          The stages below run top-to-bottom to build the served view.
        </span>
      </div>

      {note && (
        <div style={{ fontSize: 12, color: "#0369a1", backgroundColor: "#f0f9ff", border: "1px solid #bae6fd", borderRadius: 8, padding: "8px 12px" }}>
          {note}
        </div>
      )}

      {loading || !shape ? (
        <div style={{ padding: 16, color: "#94a3b8", fontSize: 13 }}>Loading shape…</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column" }}>
          {/* 1. Source + joins (read-only for v1) */}
          <StageShell title="① Source & joins" subtitle="How the base rows are assembled"
            editing={false} onEdit={() => {}} onCancel={() => {}} onSave={() => {}}>
            {shape.joins.length === 0 ? (
              <div style={{ fontSize: 12, color: "#64748b" }}>
                FK-inferred automatically from the mapped tables (no explicit joins). Explicit joins are
                authored from the serving join-preflight flow.
              </div>
            ) : (
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12, color: "#334155" }}>
                {shape.joins.map((j, i) => (
                  <li key={i}><code>{j.kind.toUpperCase()} JOIN {j.alias}</code> ON {j.predicate || "(bridge)"}</li>
                ))}
              </ul>
            )}
          </StageShell>
          <Arrow />

          {/* 2. Filter (WHERE) */}
          <StageShell title="② Filter (WHERE)" subtitle="Which rows to keep"
            editing={editing === "filter"} onEdit={() => beginEdit("filter")}
            onCancel={() => setEditing(null)} onSave={saveFilter} saving={saving}>
            {editing === "filter" ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <div style={{ display: "flex", gap: 8 }}>
                  <input style={inputStyle} placeholder='Describe in words, e.g. "active EU customers"'
                    value={dIntent} onChange={(e) => setDIntent(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") interpretFilter(); }} />
                  <button style={{ ...btn, whiteSpace: "nowrap" }} onClick={interpretFilter} disabled={interpreting || !dIntent.trim()}>
                    {interpreting ? "…" : "✦ Suggest SQL"}
                  </button>
                </div>
                <input style={{ ...inputStyle, fontFamily: "monospace" }} placeholder="SQL predicate (no WHERE keyword)"
                  value={dFilter} onChange={(e) => setDFilter(e.target.value)} />
              </div>
            ) : (
              <div style={{ fontSize: 12, color: shape.filter ? "#334155" : "#94a3b8", fontFamily: shape.filter ? "monospace" : undefined }}>
                {shape.filter || "No filter — every row is included."}
              </div>
            )}
          </StageShell>
          <Arrow />

          {/* 3. Dedupe */}
          <StageShell title="③ Deduplicate" subtitle="Keep one row per key"
            editing={editing === "dedupe"} onEdit={() => beginEdit("dedupe")}
            onCancel={() => setEditing(null)} onSave={() => saveShapeField(
              { dedupe: splitList(dedupeKeys).length ? { keys: splitList(dedupeKeys), order_by: dedupeOrder.trim(), direction: dedupeDir } : null },
              "Dedupe",
            )} saving={saving}>
            {editing === "dedupe" ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <label style={subtitle}>Partition keys (comma-separated) — empty clears dedupe</label>
                <input style={inputStyle} value={dedupeKeys} onChange={(e) => setDedupeKeys(e.target.value)} placeholder="customer_id" />
                <div style={{ display: "flex", gap: 8 }}>
                  <input style={inputStyle} value={dedupeOrder} onChange={(e) => setDedupeOrder(e.target.value)} placeholder="order by column (e.g. updated_at)" />
                  <select style={{ ...inputStyle, width: 120 }} value={dedupeDir} onChange={(e) => setDedupeDir(e.target.value)}>
                    <option value="desc">DESC</option>
                    <option value="asc">ASC</option>
                  </select>
                </div>
              </div>
            ) : (
              <div style={{ fontSize: 12, color: shape.dedupe ? "#334155" : "#94a3b8" }}>
                {shape.dedupe
                  ? `Keep latest by ${shape.dedupe.keys.join(", ")}${shape.dedupe.order_by ? ` ordered by ${shape.dedupe.order_by} ${(shape.dedupe.direction || "desc").toUpperCase()}` : ""}`
                  : "No deduplication."}
              </div>
            )}
          </StageShell>
          <Arrow />

          {/* 4. Grouping / aggregate */}
          <StageShell title="④ Grouping & aggregate" subtitle="Roll rows up to a coarser grain"
            editing={editing === "grouping"} onEdit={() => beginEdit("grouping")}
            onCancel={() => setEditing(null)} onSave={() => saveShapeField({ grouping_keys: splitList(grouping) }, "Grouping")} saving={saving}>
            {editing === "grouping" ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <label style={subtitle}>Grouping keys (comma-separated) — empty clears grouping. Non-key columns then need an aggregate (set per column in the mapping editor).</label>
                <input style={inputStyle} value={grouping} onChange={(e) => setGrouping(e.target.value)} placeholder="region, product_category" />
              </div>
            ) : (
              <div style={{ fontSize: 12, color: shape.grouping_keys.length ? "#334155" : "#94a3b8" }}>
                {shape.grouping_keys.length ? `Group by ${shape.grouping_keys.join(", ")}` : "No grouping (row-level output)."}
              </div>
            )}
          </StageShell>
          <Arrow />

          {/* 5. SCD policy */}
          <StageShell title="⑤ History (SCD policy)" subtitle="How change over time is handled"
            editing={editing === "scd"} onEdit={() => beginEdit("scd")}
            onCancel={() => setEditing(null)} onSave={() => saveShapeField(
              { scd_policy: scdType ? { type: scdType, ...(scdType === "scd2" ? { effective_column: scdEff.trim(), expiration_column: scdExp.trim() } : {}) } : null },
              "SCD policy",
            )} saving={saving}>
            {editing === "scd" ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <select style={inputStyle} value={scdType} onChange={(e) => setScdType(e.target.value)}>
                  <option value="">None (current snapshot)</option>
                  <option value="latest_only">latest_only — one current row per key</option>
                  <option value="scd2">scd2 — full history with validity windows</option>
                  <option value="snapshot">snapshot — pin to a point in time</option>
                </select>
                {scdType === "scd2" && (
                  <div style={{ display: "flex", gap: 8 }}>
                    <input style={inputStyle} value={scdEff} onChange={(e) => setScdEff(e.target.value)} placeholder="effective (valid-from) column" />
                    <input style={inputStyle} value={scdExp} onChange={(e) => setScdExp(e.target.value)} placeholder="expiration (valid-to) column" />
                  </div>
                )}
              </div>
            ) : (
              <div style={{ fontSize: 12, color: shape.scd_policy ? "#334155" : "#94a3b8" }}>
                {shape.scd_policy ? `${shape.scd_policy.type}${shape.scd_policy.effective_column ? ` (${shape.scd_policy.effective_column} → ${shape.scd_policy.expiration_column || "?"})` : ""}` : "No SCD policy (current values only)."}
              </div>
            )}
          </StageShell>
          <Arrow />

          {/* 6. Suppression */}
          <StageShell title="⑥ Suppress columns" subtitle="Drop columns from the output (kept in lineage)"
            editing={editing === "suppress"} onEdit={() => beginEdit("suppress")}
            onCancel={() => setEditing(null)} onSave={() => saveShapeField({ suppressed_columns: splitList(suppress) }, "Suppression")} saving={saving}>
            {editing === "suppress" ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <label style={subtitle}>Product columns to drop from the served view (comma-separated). Primary keys can’t be suppressed.</label>
                <input style={inputStyle} value={suppress} onChange={(e) => setSuppress(e.target.value)} placeholder="ssn, internal_notes" />
              </div>
            ) : (
              <div style={{ fontSize: 12, color: shape.suppressed_columns.length ? "#334155" : "#94a3b8" }}>
                {shape.suppressed_columns.length ? `Dropped: ${shape.suppressed_columns.join(", ")}` : "No columns suppressed."}
              </div>
            )}
          </StageShell>
        </div>
      )}
    </div>
  );
}
