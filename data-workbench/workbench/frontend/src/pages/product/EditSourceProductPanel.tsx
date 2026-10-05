import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import api from "../../api/client";
import { useCurrentUserEmail } from "../../AuthContext";
import ImpactPreviewPanel from "../../components/ImpactPreviewPanel";
import SensitivityChip from "../../components/SensitivityChip";
import { useConfirm } from "../../components/dialogContext";

/** Phase 2 — Source-aligned edit panel.
 *
 *  For deployed dpe-sa products, the PO can change metadata / per-column
 *  sensitivity / per-column descriptions without an engineer round-trip.
 *  Each write hits the backend's /source-edits/* endpoint, which mutates
 *  the upstream graph and then re-runs synthesize_odcs_from_graph → flows
 *  through Phase 1's classifier in _save_odcs_to_graph.
 *
 *  For column add/remove the PO requests a re-discovery — that DOES go
 *  through the engineer (different skill, different surface).
 */

interface SourceColumn {
  schema: string;
  table_name: string;
  col_uri: string;
  source_name: string;
  effective_name: string;
  data_type: string;
  name_status: string;
  description: string;
  sensitivity: string;
  ordinal: number;
}

interface Metadata {
  name: string | null;
  override_name: string;
  description: string | null;
  override_description: string;
  purpose: string | null;
  override_purpose: string;
  lifecycle_state: string | null;
  current_version: number | null;
}

interface InitialState {
  project: {
    id: number;
    project_code: string;
    name: string;
    archetype: string;
  };
  metadata: Metadata;
  columns: SourceColumn[];
}

const SENSITIVITY_OPTIONS = ["none", "internal", "confidential", "pii", "phi"] as const;
// Lifecycle states where opening the panel defaults to read-only "View" mode
// (a save here flows through the classifier and can branch a new version). The
// PO opts into editing via the banner. Mirrors NewProductWizard's set.
const VIEW_DEFAULT_STATES = new Set(["published", "approved", "superseded"]);

const TABS = ["metadata", "sensitivity", "descriptions", "rediscovery"] as const;
type Tab = typeof TABS[number];

export default function EditSourceProductPanel() {
  const CURRENT_USER_EMAIL = useCurrentUserEmail();
  const { projectId: projectIdParam } = useParams<{ projectId: string }>();
  const projectId = projectIdParam ? parseInt(projectIdParam, 10) : 0;
  const navigate = useNavigate();
  const confirm = useConfirm();

  const [state, setState] = useState<InitialState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("metadata");

  // Per-tab draft state
  const [metaDraft, setMetaDraft] = useState({ name: "", description: "", purpose: "" });
  const [sensitivityDraft, setSensitivityDraft] = useState<Record<string, string>>({});
  const [descriptionDraft, setDescriptionDraft] = useState<Record<string, string>>({});
  const [rediscoveryReason, setRediscoveryReason] = useState("");

  // Shared across tabs — passed through to ImpactPreviewPanel + save call.
  const [revisionNotes, setRevisionNotes] = useState("");
  const [changeKindOverride, setChangeKindOverride] = useState<"cosmetic" | "schema" | "breaking" | null>(null);

  const [saving, setSaving] = useState(false);
  const [saveResult, setSaveResult] = useState<{ kind: "ok" | "err"; msg: string } | null>(null);
  // Read-only View mode: source products in a version-branching lifecycle
  // state open read-only so a demo walk-through can't write. The PO opts into
  // editing via the banner's "Enable editing" button.
  const [editingEnabled, setEditingEnabled] = useState(false);

  const loadState = () => {
    setLoading(true);
    api
      .get<InitialState>(`/api/projects/${projectId}/source-edits`)
      .then((r) => {
        setState(r.data);
        setMetaDraft({
          name: r.data.metadata.override_name || r.data.metadata.name || "",
          description: r.data.metadata.override_description || r.data.metadata.description || "",
          purpose: r.data.metadata.override_purpose || r.data.metadata.purpose || "",
        });
        setSensitivityDraft(
          Object.fromEntries(r.data.columns.map((c) => [c.col_uri, c.sensitivity || "none"]))
        );
        setDescriptionDraft(
          Object.fromEntries(r.data.columns.map((c) => [c.col_uri, c.description || ""]))
        );
        setError(null);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    if (projectId) loadState();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  if (loading) return <div style={{ padding: 24 }}>Loading…</div>;
  if (error) return <div style={{ padding: 24, color: "#dc2626" }}>Error: {error}</div>;
  if (!state) return null;

  // Read-only unless the PO enabled editing. Derived from the authoritative
  // lifecycle_state returned by /source-edits.
  const viewOnly =
    !editingEnabled &&
    VIEW_DEFAULT_STATES.has((state.metadata.lifecycle_state || "").toLowerCase());

  const saveMetadata = async () => {
    if (viewOnly) return;
    setSaving(true);
    setSaveResult(null);
    try {
      await api.post(`/api/projects/${projectId}/source-edits/metadata`, {
        ...metaDraft,
        submitted_by: CURRENT_USER_EMAIL,
        revision_notes: revisionNotes,
      });
      setSaveResult({ kind: "ok", msg: "Metadata saved." });
      loadState();
    } catch (e) {
      setSaveResult({ kind: "err", msg: e instanceof Error ? e.message : String(e) });
    } finally {
      setSaving(false);
    }
  };

  const saveSensitivity = async () => {
    if (viewOnly) return;
    setSaving(true);
    setSaveResult(null);
    try {
      // Only send columns whose sensitivity actually changed.
      const changes = state.columns
        .filter((c) => (c.sensitivity || "none") !== (sensitivityDraft[c.col_uri] || "none"))
        .map((c) => ({
          col_uri: c.col_uri,
          sensitivity: sensitivityDraft[c.col_uri] || "none",
        }));
      if (changes.length === 0) {
        setSaveResult({ kind: "ok", msg: "No sensitivity changes to save." });
        return;
      }
      await api.post(`/api/projects/${projectId}/source-edits/sensitivity`, {
        changes,
        submitted_by: CURRENT_USER_EMAIL,
        revision_notes: revisionNotes,
      });
      setSaveResult({ kind: "ok", msg: `Sensitivity updated for ${changes.length} column${changes.length !== 1 ? "s" : ""}.` });
      loadState();
    } catch (e) {
      setSaveResult({ kind: "err", msg: e instanceof Error ? e.message : String(e) });
    } finally {
      setSaving(false);
    }
  };

  const saveDescriptions = async () => {
    if (viewOnly) return;
    setSaving(true);
    setSaveResult(null);
    try {
      const changes = state.columns
        .filter((c) => (c.description || "") !== (descriptionDraft[c.col_uri] || ""))
        .map((c) => ({
          col_uri: c.col_uri,
          description: descriptionDraft[c.col_uri] || "",
        }));
      if (changes.length === 0) {
        setSaveResult({ kind: "ok", msg: "No description changes to save." });
        return;
      }
      await api.post(`/api/projects/${projectId}/source-edits/column-descriptions`, {
        changes,
        submitted_by: CURRENT_USER_EMAIL,
        revision_notes: revisionNotes,
      });
      setSaveResult({ kind: "ok", msg: `Descriptions updated for ${changes.length} column${changes.length !== 1 ? "s" : ""}.` });
      loadState();
    } catch (e) {
      setSaveResult({ kind: "err", msg: e instanceof Error ? e.message : String(e) });
    } finally {
      setSaving(false);
    }
  };

  const requestRediscovery = async () => {
    if (viewOnly) return;
    if (!rediscoveryReason.trim()) {
      setSaveResult({ kind: "err", msg: "Please describe why re-discovery is needed." });
      return;
    }
    setSaving(true);
    setSaveResult(null);
    try {
      await api.post(`/api/projects/${projectId}/source-edits/request-rediscovery`, {
        reason: rediscoveryReason.trim(),
        submitted_by: CURRENT_USER_EMAIL,
      });
      setSaveResult({ kind: "ok", msg: "Re-discovery requested. The engineer will pick it up from the Incoming queue." });
      setRediscoveryReason("");
    } catch (e) {
      setSaveResult({ kind: "err", msg: e instanceof Error ? e.message : String(e) });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{ padding: 24, maxWidth: 960, margin: "0 auto" }}>
      <div style={{ marginBottom: 16 }}>
        <button
          onClick={() => navigate("/product/my-products")}
          style={{ background: "transparent", border: "none", color: "#6366f1", cursor: "pointer", fontSize: 13 }}
        >
          ← Back to My Products
        </button>
      </div>
      <h2 style={{ margin: "0 0 8px 0" }}>
        {viewOnly ? "View" : "Edit"} source product: {state.project.name}
      </h2>
      <div style={{ fontSize: 13, color: "#6b7280", marginBottom: 16 }}>
        Project <code>{state.project.project_code}</code> · {state.metadata.lifecycle_state} · v{state.metadata.current_version}
      </div>

      {viewOnly && (
        <div
          style={{
            margin: "0 0 16px",
            padding: 14,
            borderRadius: 10,
            border: "1px solid #fcd34d",
            backgroundColor: "#fffbeb",
            color: "#78350f",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span aria-hidden style={{ fontSize: 16 }}>👁</span>
              <div>
                <div style={{ fontWeight: 700, fontSize: 14 }}>
                  Viewing {state.metadata.lifecycle_state || "this"} product — read-only
                </div>
                <div style={{ fontSize: 13, marginTop: 4, lineHeight: 1.5 }}>
                  Review metadata, sensitivity, and descriptions. Nothing is saved. Enable
                  editing to change them — a save flows through the classifier and may branch a
                  new version.
                </div>
              </div>
            </div>
            <button
              type="button"
              onClick={async () => {
                const proceed = await confirm({
                  title: "Enable editing",
                  message:
                    "Saving changes flows through the change classifier — cosmetic edits patch in place, schema/breaking edits branch a new version.",
                  confirmLabel: "Enable editing",
                });
                if (proceed) setEditingEnabled(true);
              }}
              style={{
                flexShrink: 0,
                fontSize: 12,
                fontWeight: 700,
                padding: "8px 14px",
                borderRadius: 6,
                border: "1px solid #d97706",
                backgroundColor: "#d97706",
                color: "#fff",
                cursor: "pointer",
              }}
            >
              Enable editing
            </button>
          </div>
        </div>
      )}

      {/* Impact preview rides above the tab content so the PO sees consumer
          impact regardless of which tab they're on. Re-fetches when the
          backend save flips the contract state. */}
      <ImpactPreviewPanel
        projectId={projectId}
        resolvedKind={null}
        revisionNotes={revisionNotes}
        onRevisionNotesChange={setRevisionNotes}
        changeKindOverride={changeKindOverride}
        onChangeKindOverrideChange={setChangeKindOverride}
      />

      {/* Tab bar */}
      <div style={{ display: "flex", gap: 4, borderBottom: "1px solid #e5e7eb", marginBottom: 16 }}>
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            style={{
              padding: "8px 16px",
              border: "none",
              background: "transparent",
              borderBottom: tab === t ? "2px solid #3b82f6" : "2px solid transparent",
              fontWeight: tab === t ? 600 : 400,
              fontSize: 14,
              color: tab === t ? "#1e293b" : "#64748b",
              cursor: "pointer",
            }}
          >
            {t.charAt(0).toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>

      {tab === "metadata" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <label style={{ fontSize: 13, fontWeight: 600 }}>Product name</label>
          <input
            value={metaDraft.name}
            onChange={(e) => setMetaDraft({ ...metaDraft, name: e.target.value })}
            disabled={viewOnly}
            style={inputStyle}
          />
          <label style={{ fontSize: 13, fontWeight: 600 }}>Description</label>
          <textarea
            value={metaDraft.description}
            onChange={(e) => setMetaDraft({ ...metaDraft, description: e.target.value })}
            disabled={viewOnly}
            rows={4}
            style={{ ...inputStyle, fontFamily: "inherit", resize: "vertical" }}
          />
          <label style={{ fontSize: 13, fontWeight: 600 }}>Purpose</label>
          <textarea
            value={metaDraft.purpose}
            onChange={(e) => setMetaDraft({ ...metaDraft, purpose: e.target.value })}
            disabled={viewOnly}
            rows={3}
            style={{ ...inputStyle, fontFamily: "inherit", resize: "vertical" }}
          />
          {!viewOnly && (
            <button onClick={saveMetadata} disabled={saving} style={primaryBtn}>
              {saving ? "Saving…" : "Save metadata"}
            </button>
          )}
        </div>
      )}

      {tab === "sensitivity" && (
        <div>
          <p style={{ fontSize: 13, color: "#6b7280", marginBottom: 12 }}>
            Set per-column sensitivity. PII / PHI flags propagate to consumer products
            via the marketplace and mapping panels. Consumer engineers see the chip
            in their mapping review so they can apply masking transforms.
          </p>
          <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ textAlign: "left", borderBottom: "1px solid #e5e7eb" }}>
                <th style={th}>Schema.Table</th>
                <th style={th}>Column</th>
                <th style={th}>Type</th>
                <th style={th}>Sensitivity</th>
              </tr>
            </thead>
            <tbody>
              {state.columns.map((c) => (
                <tr key={c.col_uri} style={{ borderBottom: "1px solid #f3f4f6" }}>
                  <td style={td}>
                    <code style={{ fontSize: 11, color: "#6b7280" }}>
                      {c.schema}.{c.table_name}
                    </code>
                  </td>
                  <td style={td}>
                    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      <code>{c.effective_name}</code>
                      <SensitivityChip sensitivity={c.sensitivity} compact />
                    </div>
                  </td>
                  <td style={td}>
                    <code style={{ fontSize: 11, color: "#6b7280" }}>{c.data_type}</code>
                  </td>
                  <td style={td}>
                    <select
                      value={sensitivityDraft[c.col_uri] || "none"}
                      onChange={(e) =>
                        setSensitivityDraft({ ...sensitivityDraft, [c.col_uri]: e.target.value })
                      }
                      disabled={viewOnly}
                      style={{ fontSize: 12, padding: "4px 6px" }}
                    >
                      {SENSITIVITY_OPTIONS.map((s) => (
                        <option key={s} value={s}>
                          {s}
                        </option>
                      ))}
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!viewOnly && (
            <button onClick={saveSensitivity} disabled={saving} style={{ ...primaryBtn, marginTop: 16 }}>
              {saving ? "Saving…" : "Save sensitivity changes"}
            </button>
          )}
        </div>
      )}

      {tab === "descriptions" && (
        <div>
          <p style={{ fontSize: 13, color: "#6b7280", marginBottom: 12 }}>
            Edit per-column descriptions. These overwrite the current approved
            ColumnDescription text; pending-review descriptions stay in the
            engineer's review queue and are not touched here.
          </p>
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {state.columns.map((c) => (
              <div
                key={c.col_uri}
                style={{ padding: 10, background: "#f9fafb", borderRadius: 6, border: "1px solid #e5e7eb" }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                  <code style={{ fontSize: 12, color: "#6b7280" }}>
                    {c.schema}.{c.table_name}.{c.effective_name}
                  </code>
                  <SensitivityChip sensitivity={c.sensitivity} compact />
                </div>
                <textarea
                  value={descriptionDraft[c.col_uri] ?? ""}
                  onChange={(e) =>
                    setDescriptionDraft({ ...descriptionDraft, [c.col_uri]: e.target.value })
                  }
                  disabled={viewOnly}
                  rows={2}
                  style={{ ...inputStyle, fontFamily: "inherit", fontSize: 13, resize: "vertical" }}
                />
              </div>
            ))}
          </div>
          {!viewOnly && (
            <button onClick={saveDescriptions} disabled={saving} style={{ ...primaryBtn, marginTop: 16 }}>
              {saving ? "Saving…" : "Save description changes"}
            </button>
          )}
        </div>
      )}

      {tab === "rediscovery" && (
        <div>
          <p style={{ fontSize: 13, color: "#6b7280", marginBottom: 12 }}>
            For column add / remove or schema changes that require running discovery
            against the source database, request re-discovery. The engineer picks it
            up from the Incoming queue, re-runs the dpe-sa pipeline, and the change
            flows back through the standard po_source_validation gate before re-deploying.
          </p>
          <label style={{ fontSize: 13, fontWeight: 600 }}>What needs to change?</label>
          <textarea
            value={rediscoveryReason}
            onChange={(e) => setRediscoveryReason(e.target.value)}
            placeholder="e.g. The source database added a `last_login_at` column to `users`; need it in the contract."
            disabled={viewOnly}
            rows={4}
            style={{ ...inputStyle, fontFamily: "inherit", resize: "vertical", marginTop: 8 }}
          />
          {!viewOnly && (
            <button
              onClick={requestRediscovery}
              disabled={saving || !rediscoveryReason.trim()}
              style={{ ...primaryBtn, marginTop: 16 }}
            >
              {saving ? "Submitting…" : "Request re-discovery"}
            </button>
          )}
        </div>
      )}

      {saveResult && (
        <div
          style={{
            marginTop: 16,
            padding: 10,
            borderRadius: 6,
            background: saveResult.kind === "ok" ? "#f0fdf4" : "#fef2f2",
            color: saveResult.kind === "ok" ? "#166534" : "#991b1b",
            fontSize: 13,
          }}
        >
          {saveResult.msg}
        </div>
      )}
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  padding: 8,
  fontSize: 14,
  border: "1px solid #d1d5db",
  borderRadius: 6,
  width: "100%",
  boxSizing: "border-box",
};

const primaryBtn: React.CSSProperties = {
  padding: "8px 16px",
  fontSize: 14,
  fontWeight: 600,
  background: "#3b82f6",
  color: "white",
  border: "none",
  borderRadius: 6,
  cursor: "pointer",
};

const th: React.CSSProperties = { padding: "6px 4px", fontWeight: 600, fontSize: 12 };
const td: React.CSSProperties = { padding: "6px 4px", verticalAlign: "middle" };
