import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import api from "../../api/client";
import ODCSEditor from "../../components/ODCSEditor";
import ProductKindChip from "../../components/ProductKindChip";
import { useConfirm, useNotify, useToast } from "../../components/dialogContext";
import { useCurrentUserEmail } from "../../AuthContext";

/** Blueprint Library detail — design-completeness "what's missing" + spec view,
 *  with clone / edit / export / publish. */

interface Spec {
  id: string;
  name: string;
  domain?: string;
  description?: string;
  purpose?: string;
  productKind?: string;
  templateStatus?: "draft" | "published";
  templateOrigin?: "seed" | "clone" | "import" | "authored";
  templateOwnerEmail?: string;
  clonedFrom?: string;
  schema?: Array<{ name?: string; physicalName?: string; description?: string;
                   properties?: Array<Record<string, unknown>> }>;
  [k: string]: unknown;
}

interface Check { id: string; label: string; status: string; detail?: string }
interface Completeness {
  band: "green" | "amber" | "red";
  completeness: number;
  design: { checklist: Check[]; missing: string[] };
  columns: { total: number; described: number; typed: number;
             gaps: Array<{ name: string; missing_description: boolean; missing_type: boolean }> };
  operational: { checklist: Check[]; note: string };
}

const BAND_COLOR: Record<string, string> = { green: "#16a34a", amber: "#d97706", red: "#dc2626" };

export default function TemplateDetailPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const confirm = useConfirm();
  const toast = useToast();
  const { showError } = useNotify();
  const myEmail = useCurrentUserEmail();

  const [spec, setSpec] = useState<Spec | null>(null);
  const [comp, setComp] = useState<Completeness | null>(null);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);

  const enc = encodeURIComponent(id);

  const load = useCallback(async () => {
    try {
      const [s, c] = await Promise.all([
        api.get(`/api/templates/${enc}`),
        api.get(`/api/templates/${enc}/completeness`),
      ]);
      setSpec(s.data.spec);
      setComp(c.data);
    } catch (e) {
      showError(e, { title: "Failed to load template" });
    }
  }, [enc, showError]);

  useEffect(() => { load(); }, [load]);

  if (!spec) return <div style={{ padding: 40, color: "#94a3b8", fontFamily: "system-ui" }}>Loading…</div>;

  const isSeed = spec.templateOrigin === "seed";
  const isDraft = spec.templateStatus === "draft";
  const isOwner = !!myEmail && spec.templateOwnerEmail === myEmail;

  const clone = async () => {
    setBusy(true);
    try {
      const r = await api.post(`/api/templates/${enc}/clone`);
      toast({ tone: "success", message: "Cloned to a new draft." });
      navigate(`/product/templates/${encodeURIComponent(r.data.contract_id)}`);
    } catch (e) { showError(e, { title: "Clone failed" }); }
    setBusy(false);
  };

  const exportOdcs = async () => {
    try {
      const r = await api.get(`/api/templates/${enc}/export`);
      const blob = new Blob([r.data.yaml], { type: "text/yaml" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = r.data.filename || "template.yaml";
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) { showError(e, { title: "Export failed" }); }
  };

  const publish = async () => {
    if (!(await confirm({ title: "Publish template?",
      message: "Publishing makes this visible to everyone and available in Feasibility.",
      confirmLabel: "Publish" }))) return;
    setBusy(true);
    try {
      await api.post(`/api/templates/${enc}/publish`);
      toast({ tone: "success", message: "Published — now visible to everyone and available in Feasibility." });
      await load();
    } catch (e) { showError(e, { title: "Publish failed" }); }
    setBusy(false);
  };

  const retract = async () => {
    if (!(await confirm({ title: "Retract to draft?",
      message: "This hides the template from consumers and removes it from Feasibility.",
      confirmLabel: "Retract", tone: "danger" }))) return;
    setBusy(true);
    try {
      await api.post(`/api/templates/${enc}/retract`);
      toast({ tone: "success", message: "Retracted — hidden from consumers." });
      await load();
    } catch (e) { showError(e, { title: "Retract failed" }); }
    setBusy(false);
  };

  const remove = async () => {
    if (!(await confirm({ title: "Delete template?", message: "This cannot be undone.",
      confirmLabel: "Delete", tone: "danger" }))) return;
    try {
      await api.delete(`/api/templates/${enc}`);
      navigate("/product/templates");
    } catch (e) { showError(e, { title: "Delete failed" }); }
  };

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto", padding: 24, fontFamily: "system-ui" }}>
      <button onClick={() => navigate("/product/templates")} style={styles.back}>← Data Product Templates</button>

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16, marginTop: 8 }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 800, color: "#0f172a", margin: 0, display: "flex", alignItems: "center", gap: 10 }}>
            {isSeed && <span title="Read-only seed">🔒</span>}
            {spec.name}
            <ProductKindChip kind={spec.productKind} />
          </h1>
          <div style={{ fontSize: 13, color: "#64748b", marginTop: 4 }}>
            {spec.domain} · <span style={{ fontWeight: 600 }}>{spec.templateStatus}</span> · {spec.templateOrigin}
            {spec.clonedFrom && (
              <> · cloned from{" "}
                <a onClick={() => navigate(`/product/templates/${encodeURIComponent(spec.clonedFrom || "")}`)}
                   style={{ color: "#7c3aed", cursor: "pointer" }}>
                  {spec.clonedFrom.split(":").slice(-1)[0]}
                </a>
              </>
            )}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button onClick={clone} disabled={busy} style={styles.secondaryBtn}>Clone</button>
          {isDraft && !isSeed && (
            <button onClick={() => setEditing((v) => !v)} style={styles.secondaryBtn}>
              {editing ? "Close editor" : "Edit"}
            </button>
          )}
          <button onClick={exportOdcs} style={styles.secondaryBtn}>Export ODCS</button>
          {isDraft && !isSeed && (isOwner || !myEmail) && (
            <button onClick={publish} disabled={busy} style={styles.primaryBtn}>Publish</button>
          )}
          {!isDraft && !isSeed && (isOwner || !myEmail) && (
            <button onClick={retract} disabled={busy} style={styles.secondaryBtn}>Retract</button>
          )}
          {!isSeed && (isOwner || !myEmail) && (
            <button onClick={remove} style={{ ...styles.secondaryBtn, color: "#dc2626", borderColor: "#fecaca" }}>Delete</button>
          )}
        </div>
      </div>

      {spec.description && <p style={{ color: "#475569", fontSize: 14, marginTop: 12 }}>{spec.description}</p>}

      {editing ? (
        <div style={{ marginTop: 16 }}>
          <ODCSEditor apiBase={`/api/templates/${enc}`} mode="template" onSaved={load} />
        </div>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginTop: 16, alignItems: "start" }}>
          {/* What's missing (design completeness) */}
          {comp && (
            <div style={styles.panel}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <h3 style={styles.panelTitle}>What's missing</h3>
                <span style={{ ...styles.bandPill, backgroundColor: BAND_COLOR[comp.band] }}>
                  {comp.completeness}% · {comp.band}
                </span>
              </div>
              <ul style={{ listStyle: "none", padding: 0, margin: "10px 0 0" }}>
                {comp.design.checklist.map((c) => (
                  <li key={c.id} style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 0", fontSize: 13 }}>
                    <span>{c.status === "set" ? "✅" : "⬜"}</span>
                    <span style={{ color: c.status === "set" ? "#0f172a" : "#64748b" }}>{c.label}</span>
                    {c.detail && <span style={{ fontSize: 11, color: "#94a3b8" }}>({c.detail})</span>}
                  </li>
                ))}
              </ul>
              {comp.columns.gaps.length > 0 && (
                <div style={{ marginTop: 10, fontSize: 12, color: "#64748b" }}>
                  {comp.columns.gaps.length} column(s) missing a description or type.
                </div>
              )}
            </div>
          )}

          {/* Operational (informational) */}
          {comp && (
            <div style={{ ...styles.panel, backgroundColor: "#f8fafc" }}>
              <h3 style={styles.panelTitle}>Operational fields</h3>
              <p style={{ fontSize: 11, color: "#94a3b8", margin: "2px 0 8px" }}>{comp.operational.note}</p>
              <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
                {comp.operational.checklist.map((c) => (
                  <li key={c.id} style={{ display: "flex", alignItems: "center", gap: 8, padding: "3px 0", fontSize: 13 }}>
                    <span>{c.status === "set" ? "✔️" : "◦"}</span>
                    <span style={{ color: "#64748b" }}>{c.label}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {/* Read-only schema overview */}
      {!editing && (
        <div style={{ marginTop: 20 }}>
          {(spec.schema || []).map((s, si) => (
            <div key={si} style={{ ...styles.panel, marginBottom: 12 }}>
              <h3 style={styles.panelTitle}>{s.name || s.physicalName} ({(s.properties || []).length} columns)</h3>
              {s.description && <p style={{ fontSize: 12, color: "#64748b" }}>{s.description}</p>}
              <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse", marginTop: 6 }}>
                <thead>
                  <tr style={{ textAlign: "left", color: "#94a3b8" }}>
                    <th style={styles.th}>Column</th><th style={styles.th}>Type</th>
                    <th style={styles.th}>Key</th><th style={styles.th}>Req</th><th style={styles.th}>Description</th>
                  </tr>
                </thead>
                <tbody>
                  {(s.properties || []).map((p, pi) => {
                    const col = p as Record<string, unknown>;
                    return (
                      <tr key={pi} style={{ borderTop: "1px solid #f1f5f9" }}>
                        <td style={styles.td}>{String(col.name ?? "")}</td>
                        <td style={styles.td}>{String(col.physicalType ?? col.logicalType ?? "")}</td>
                        <td style={styles.td}>{col.primaryKey ? "🔑" : ""}</td>
                        <td style={styles.td}>{col.required ? "•" : ""}</td>
                        <td style={{ ...styles.td, color: col.description ? "#475569" : "#cbd5e1" }}>
                          {String(col.description ?? "") || "—"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  back: { background: "none", border: "none", color: "#7c3aed", cursor: "pointer", fontSize: 13, padding: 0, marginBottom: 4 },
  primaryBtn: { padding: "7px 14px", borderRadius: 8, border: "none", backgroundColor: "#7c3aed", color: "#fff", fontWeight: 600, fontSize: 13, cursor: "pointer" },
  secondaryBtn: { padding: "7px 14px", borderRadius: 8, border: "1px solid #e2e8f0", backgroundColor: "#fff", color: "#475569", fontWeight: 600, fontSize: 13, cursor: "pointer" },
  panel: { padding: 16, borderRadius: 10, border: "1px solid #e2e8f0", backgroundColor: "#fff" },
  panelTitle: { fontSize: 14, fontWeight: 700, color: "#0f172a", margin: 0 },
  bandPill: { color: "#fff", padding: "3px 10px", borderRadius: 999, fontSize: 12, fontWeight: 700 },
  th: { padding: "4px 8px", fontWeight: 600, fontSize: 11 },
  td: { padding: "4px 8px" },
};
