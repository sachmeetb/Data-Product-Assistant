/**
 * Confirm Physical Schema (D3) — the reviewed, structured schema for a
 * schema-only migration project. Pre-filled best-effort from the intake
 * blueprint, then the engineer edits + explicitly confirms structured fields
 * (catalog/namespace/table, physical data_type, nullable, PK). The confirmed
 * artifact is what the deterministic seeder (Phase E) materializes as the
 * catalog. Self-hides for non-schema-only projects.
 *
 * Mounts on the engineer dmig ProjectDetailPage. Confirmation is gated backend-
 * side: a 422 returns errors[] (unsafe/colliding names, missing types).
 */
import { useCallback, useEffect, useState } from "react";
import api from "../api/client";
import { useConfirm } from "./dialogContext";

interface PhysColumn {
  name: string;
  data_type: string;
  nullable: boolean | null;
  primary_key: boolean;
  fk_table: string;
  fk_column: string;
}
interface PhysTable {
  catalog: string;
  namespace: string;
  table: string;
  columns: PhysColumn[];
  excluded: boolean;
}
interface PhysSchema { version: string; tables: PhysTable[]; }
interface PhysResponse {
  status: string;
  schema: PhysSchema;
  prefilled: boolean;
  confirmed_at: string | null;
}

const newCol = (): PhysColumn => ({ name: "", data_type: "", nullable: null, primary_key: false, fk_table: "", fk_column: "" });
const cellInput: React.CSSProperties = { padding: "4px 6px", borderRadius: 5, border: "1px solid #cbd5e1", fontSize: 12, width: "100%", boxSizing: "border-box" };

export default function ConfirmPhysicalSchemaPanel({ projectId, mode }: { projectId: number; mode?: string }) {
  const [schema, setSchema] = useState<PhysSchema | null>(null);
  const [status, setStatus] = useState<string>("draft");
  const [prefilled, setPrefilled] = useState(false);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [msg, setMsg] = useState<string | null>(null);
  // Aliased: this component already has a local `confirm` handler (see below).
  const askConfirm = useConfirm();

  const load = useCallback(() => {
    api.get(`/api/projects/${projectId}/migration/physical-schema`)
      .then((r) => {
        const d = r.data as PhysResponse;
        setSchema(d.schema);
        setStatus(d.status);
        setPrefilled(d.prefilled);
      })
      .catch(() => setSchema(null));
  }, [projectId]);

  useEffect(() => { if (mode === "schema_only") load(); }, [mode, load]);

  if (mode !== "schema_only" || !schema) return null;

  const mutate = (fn: (s: PhysSchema) => void) => {
    const next = JSON.parse(JSON.stringify(schema)) as PhysSchema;
    fn(next);
    setSchema(next);
    setMsg(null);
  };

  const save = async () => {
    setBusy(true); setErrors([]); setMsg(null);
    try {
      const r = await api.put(`/api/projects/${projectId}/migration/physical-schema`, { schema });
      setStatus(r.data.status); setPrefilled(false); setMsg("Draft saved.");
    } catch { setMsg("Save failed."); }
    setBusy(false);
  };

  const confirm = async () => {
    setBusy(true); setErrors([]); setMsg(null);
    try {
      const r = await api.post(`/api/projects/${projectId}/migration/physical-schema/confirm`, { schema });
      setStatus(r.data.status); setMsg("Physical schema confirmed."); setPrefilled(false);
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: { errors?: string[] } } } })?.response?.data?.detail;
      if (detail?.errors) setErrors(detail.errors);
      else setMsg("Confirm failed.");
    }
    setBusy(false);
  };

  const flipToLive = async () => {
    const ok = await askConfirm({
      title: "Flip to live?",
      message:
        "This attaches live discovery, deletes the seeded catalog, and re-opens enrichment/assess/generate to run against real data. Requires a live source + target connection.",
      confirmLabel: "Flip to live",
      tone: "danger",
    });
    if (!ok) return;
    setBusy(true); setErrors([]); setMsg(null);
    try {
      await api.post(`/api/projects/${projectId}/migration/flip-to-live`, {});
      window.location.reload();
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: { missing?: string[] } | string } } })?.response?.data?.detail;
      if (detail && typeof detail === "object" && detail.missing) setErrors(detail.missing);
      else setMsg(typeof detail === "string" ? detail : "Flip to live failed.");
    }
    setBusy(false);
  };

  const confirmed = status === "confirmed";
  const border = confirmed ? "#a7f3d0" : "#fcd34d";
  const bg = confirmed ? "#ecfdf5" : "#fffbeb";

  return (
    <div style={{ border: `1px solid ${border}`, background: bg, borderRadius: 10, padding: "10px 14px", marginBottom: 12 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.4, color: confirmed ? "#065f46" : "#92400e" }}>
          Physical schema · {confirmed ? "confirmed" : "needs confirmation"}
        </span>
        <span style={{ fontSize: 12, color: "#64748b" }}>
          {schema.tables.length} table(s){prefilled ? " · pre-filled from intake (unsaved)" : ""}
        </span>
        <div style={{ flex: 1 }} />
        <button onClick={flipToLive} disabled={busy} title="Source connectivity is now available — switch to a live pipeline" style={{ fontSize: 12, padding: "3px 10px", borderRadius: 6, border: "1px solid #cbd5e1", background: "#fff", color: "#334155", cursor: "pointer" }}>
          Flip to live
        </button>
        <button onClick={() => setOpen((o) => !o)} style={{ fontSize: 12, padding: "3px 10px", borderRadius: 6, border: `1px solid ${border}`, background: "#fff", color: "#334155", cursor: "pointer" }}>
          {open ? "Hide" : "Review & confirm"}
        </button>
      </div>

      <div style={{ fontSize: 12, color: "#64748b", marginTop: 6 }}>
        No live source yet — confirm the assessment's physical schema (names, types, keys) here. This is
        step 1 of 2: it only records the reviewed schema.
        {confirmed ? (
          <> <strong>Confirmed ✓</strong> — now run <strong>Import Provided Schema</strong> in the pipeline
          below to materialize it as the project catalog (that seeds the graph and completes the step).</>
        ) : (
          <> Once confirmed, run <strong>Import Provided Schema</strong> in the pipeline to seed the catalog
          so enrichment/assess/generate can run offline.</>
        )}
      </div>

      {open && (
        <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 10 }}>
          {errors.length > 0 && (
            <div style={{ border: "1px solid #fca5a5", background: "#fef2f2", borderRadius: 8, padding: "8px 12px" }}>
              <div style={{ fontWeight: 600, color: "#991b1b", fontSize: 13 }}>Cannot confirm yet:</div>
              <ul style={{ margin: "4px 0 0", paddingLeft: 18, color: "#b91c1c", fontSize: 12 }}>
                {errors.map((e, i) => <li key={i}>{e}</li>)}
              </ul>
            </div>
          )}
          {msg && <div style={{ fontSize: 12, color: "#065f46" }}>{msg}</div>}

          {schema.tables.map((t, ti) => (
            <div key={ti} style={{ background: "#fff", border: "1px solid #e2e8f0", borderRadius: 8, padding: 10, opacity: t.excluded ? 0.55 : 1 }}>
              <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 8, flexWrap: "wrap" }}>
                <input placeholder="catalog" value={t.catalog} onChange={(e) => mutate((s) => { s.tables[ti].catalog = e.target.value; })} style={{ ...cellInput, width: 110 }} />
                <span style={{ color: "#94a3b8" }}>.</span>
                <input placeholder="namespace/schema" value={t.namespace} onChange={(e) => mutate((s) => { s.tables[ti].namespace = e.target.value; })} style={{ ...cellInput, width: 150 }} />
                <span style={{ color: "#94a3b8" }}>.</span>
                <input placeholder="table" value={t.table} onChange={(e) => mutate((s) => { s.tables[ti].table = e.target.value; })} style={{ ...cellInput, width: 150, fontWeight: 600 }} />
                <div style={{ flex: 1 }} />
                <label style={{ fontSize: 12, color: "#64748b", display: "flex", gap: 4, alignItems: "center" }}>
                  <input type="checkbox" checked={t.excluded} onChange={(e) => mutate((s) => { s.tables[ti].excluded = e.target.checked; })} /> exclude
                </label>
                <button onClick={() => mutate((s) => { s.tables.splice(ti, 1); })} title="Remove table" style={{ border: "none", background: "none", color: "#ef4444", cursor: "pointer", fontSize: 14 }}>✕</button>
              </div>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                <thead>
                  <tr style={{ textAlign: "left", color: "#94a3b8" }}>
                    <th style={{ fontWeight: 600, padding: "2px 4px" }}>column</th>
                    <th style={{ fontWeight: 600, padding: "2px 4px" }}>data_type</th>
                    <th style={{ fontWeight: 600, padding: "2px 4px" }}>nullable</th>
                    <th style={{ fontWeight: 600, padding: "2px 4px" }}>PK</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {t.columns.map((c, ci) => (
                    <tr key={ci}>
                      <td style={{ padding: "2px 4px" }}><input value={c.name} onChange={(e) => mutate((s) => { s.tables[ti].columns[ci].name = e.target.value; })} style={cellInput} /></td>
                      <td style={{ padding: "2px 4px" }}><input placeholder="required" value={c.data_type} onChange={(e) => mutate((s) => { s.tables[ti].columns[ci].data_type = e.target.value; })} style={{ ...cellInput, background: c.data_type ? "#fff" : "#fef2f2" }} /></td>
                      <td style={{ padding: "2px 4px" }}>
                        <select value={c.nullable === null ? "" : c.nullable ? "y" : "n"} onChange={(e) => mutate((s) => { const v = e.target.value; s.tables[ti].columns[ci].nullable = v === "" ? null : v === "y"; })} style={cellInput}>
                          <option value="">?</option><option value="y">yes</option><option value="n">no</option>
                        </select>
                      </td>
                      <td style={{ padding: "2px 4px", textAlign: "center" }}><input type="checkbox" checked={c.primary_key} onChange={(e) => mutate((s) => { s.tables[ti].columns[ci].primary_key = e.target.checked; })} /></td>
                      <td style={{ padding: "2px 4px" }}><button onClick={() => mutate((s) => { s.tables[ti].columns.splice(ci, 1); })} style={{ border: "none", background: "none", color: "#94a3b8", cursor: "pointer" }}>✕</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <button onClick={() => mutate((s) => { s.tables[ti].columns.push(newCol()); })} style={{ marginTop: 6, fontSize: 12, border: "1px dashed #cbd5e1", background: "#fff", borderRadius: 6, padding: "3px 8px", cursor: "pointer", color: "#475569" }}>+ column</button>
            </div>
          ))}

          <button onClick={() => mutate((s) => { s.tables.push({ catalog: "", namespace: "", table: "", columns: [newCol()], excluded: false }); })} style={{ fontSize: 12, border: "1px dashed #cbd5e1", background: "#fff", borderRadius: 6, padding: "5px 10px", cursor: "pointer", color: "#475569", alignSelf: "flex-start" }}>+ table</button>

          <div style={{ display: "flex", gap: 8, borderTop: "1px solid #e2e8f0", paddingTop: 10 }}>
            <button onClick={save} disabled={busy} style={{ padding: "6px 14px", borderRadius: 8, border: "1px solid #cbd5e1", background: "#fff", color: "#334155", cursor: "pointer", fontSize: 13 }}>Save draft</button>
            <div style={{ flex: 1 }} />
            <button onClick={confirm} disabled={busy} style={{ padding: "6px 16px", borderRadius: 8, border: "none", background: "#059669", color: "#fff", cursor: "pointer", fontWeight: 600, fontSize: 13 }}>
              {busy ? "Working…" : "Confirm physical schema"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
