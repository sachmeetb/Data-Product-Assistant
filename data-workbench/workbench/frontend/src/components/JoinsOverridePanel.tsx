/**
 * Engineer-facing editor for `:DatasetTransform.joinsJson` (Phase 4 explicit
 * joins override). Mounted on the engineer's ProjectDashboard serving-card
 * detail, below the ServingWarningsCallout — the natural trigger to override
 * is reading the FK BFS bridge pick and disagreeing with it.
 *
 * v2: per-row form builder. v1 was a raw JSON textarea — engineers found the
 * comma/quoting overhead a distraction when the underlying shape is small
 * (alias / dataset_uri / kind / predicate per row). The form is now the
 * default; a "View as JSON" toggle keeps the raw-edit escape hatch for power
 * users who want to bulk paste or copy out.
 *
 * Save flow:
 *   1. Engineer adds / edits rows in the form (or pastes into the JSON view).
 *   2. On Save, the form rows serialize back to `[{alias, dataset_uri, kind, predicate}, ...]`.
 *   3. PUT /api/projects/{id}/dataset-transform/joins with the list.
 *   4. Backend validates each entry's shape; rejects malformed entries with
 *      a 400 + per-index error list, which renders below the form.
 *   5. Successful save persists to schema-side AND mirrors to ods-side, then
 *      the panel prompts engineer to re-run the serving stage so view-DDL
 *      picks up the new joins[].
 *
 * The JSON view is a single source of truth — toggling between views
 * round-trips through JSON.stringify / JSON.parse, so a malformed JSON in
 * the textarea blocks the switch back to form mode with an inline error.
 */

import { useEffect, useMemo, useState } from "react";
import api from "../api/client";

interface JoinEntry {
  alias: string;
  dataset_uri: string;
  kind: string;
  predicate: string;
  /** Pure bridge: join-only entry contributing no SELECT columns — view-DDL
   *  emits it as a DISTINCT projection of the predicate keys (fan-out-safe
   *  junction). Persisted only when true. */
  bridge_only?: boolean;
}

interface DatasetTransformResponse {
  output_dataset_uri: string;
  contract_id: string;
  schema_physical_name: string;
  joins: JoinEntry[];
  filter: string;
  dedupe: unknown;
  grouping_keys: string[];
  scd_policy: unknown;
  suppressed_columns: string[];
  grain_prose: string;
}

interface Props {
  projectId: number;
  outputDatasetUri: string;
  viewName?: string;
}

const JOIN_KINDS = ["left", "inner", "right", "full", "cross"] as const;

const blankRow = (): JoinEntry => ({ alias: "", dataset_uri: "", kind: "left", predicate: "" });

export default function JoinsOverridePanel({ projectId, outputDatasetUri, viewName }: Props) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [rows, setRows] = useState<JoinEntry[]>([]);
  const [original, setOriginal] = useState<JoinEntry[]>([]);
  // JSON view is opt-in. When active, the textarea is the source of truth
  // (rows[] are kept in sync only on view-switch).
  const [jsonMode, setJsonMode] = useState(false);
  const [jsonDraft, setJsonDraft] = useState<string>("[]");

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    api.get<DatasetTransformResponse>(`/api/projects/${projectId}/dataset-transform`, {
      params: { output_dataset_uri: outputDatasetUri },
    })
      .then((r) => {
        if (cancelled) return;
        const loaded = (r.data.joins || []).map((j) => ({
          alias: j.alias || "",
          dataset_uri: j.dataset_uri || "",
          kind: (j.kind || "left").toLowerCase(),
          predicate: j.predicate || "",
          ...(j.bridge_only ? { bridge_only: true } : {}),
        }));
        setRows(loaded);
        setOriginal(loaded);
        setJsonDraft(JSON.stringify(loaded, null, 2));
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Failed to load joins");
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [open, projectId, outputDatasetUri]);

  // Switch to JSON view: serialize current form rows into the textarea.
  const enterJsonMode = () => {
    setJsonDraft(JSON.stringify(rows, null, 2));
    setJsonMode(true);
    setError(null);
  };

  // Switch back to form view: parse textarea, validate it's a list of objects
  // matching the JoinEntry shape, push into rows[]. Bad JSON / wrong shape →
  // surface error and stay in JSON mode.
  const exitJsonMode = () => {
    try {
      const parsed = JSON.parse(jsonDraft.trim() || "[]");
      if (!Array.isArray(parsed)) {
        setError("JSON must be an array of objects");
        return;
      }
      const cleaned: JoinEntry[] = parsed.map((entry, i) => {
        if (!entry || typeof entry !== "object") {
          throw new Error(`Entry ${i} is not an object`);
        }
        const rec = entry as Record<string, unknown>;
        return {
          alias: String(rec.alias ?? ""),
          dataset_uri: String(rec.dataset_uri ?? ""),
          kind: String(rec.kind ?? "left").toLowerCase(),
          predicate: String(rec.predicate ?? ""),
          ...(rec.bridge_only ? { bridge_only: true } : {}),
        };
      });
      setRows(cleaned);
      setJsonMode(false);
      setError(null);
    } catch (e) {
      setError(`JSON parse error: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const addRow = () => setRows((prev) => [...prev, blankRow()]);
  const removeRow = (idx: number) => setRows((prev) => prev.filter((_, i) => i !== idx));
  const updateRow = (idx: number, patch: Partial<JoinEntry>) =>
    setRows((prev) => prev.map((r, i) => (i === idx ? { ...r, ...patch } : r)));

  // Effective "to-save" payload depends on which mode we're in. JSON mode
  // bypasses the row state entirely so engineers can save a textarea-only
  // edit without first switching back to form mode.
  const payloadFromState = (): JoinEntry[] | { error: string } => {
    if (jsonMode) {
      try {
        const parsed = JSON.parse(jsonDraft.trim() || "[]");
        if (!Array.isArray(parsed)) return { error: "JSON must be an array" };
        return parsed;
      } catch (e) {
        return { error: `JSON parse error: ${e instanceof Error ? e.message : String(e)}` };
      }
    }
    return rows;
  };

  const dirty = useMemo(() => {
    if (jsonMode) {
      try {
        return JSON.stringify(JSON.parse(jsonDraft)) !== JSON.stringify(original);
      } catch {
        return true;
      }
    }
    return JSON.stringify(rows) !== JSON.stringify(original);
  }, [jsonMode, jsonDraft, rows, original]);

  const onSave = async () => {
    setError(null);
    setSuccess(null);
    const payload = payloadFromState();
    if (!Array.isArray(payload)) {
      setError(payload.error);
      return;
    }
    setSaving(true);
    try {
      const res = await api.put<{ joins_count: number }>(
        `/api/projects/${projectId}/dataset-transform/joins`,
        { output_dataset_uri: outputDatasetUri, joins: payload }
      );
      const count = res.data.joins_count;
      setOriginal(payload);
      setRows(payload);
      setJsonDraft(JSON.stringify(payload, null, 2));
      setSuccess(
        count === 0
          ? "Cleared explicit joins — view-DDL will fall back to FK auto-discovery. Re-run the serving stage to apply."
          : `Saved ${count} join${count === 1 ? "" : "s"}. Re-run the serving stage to apply.`
      );
    } catch (e: unknown) {
      const errObj = e as { response?: { data?: { detail?: { errors?: string[] } | string } } };
      const detail = errObj?.response?.data?.detail;
      if (detail && typeof detail === "object" && Array.isArray(detail.errors)) {
        setError(`Validation:\n${detail.errors.join("\n")}`);
      } else {
        setError(typeof detail === "string" ? detail : (e instanceof Error ? e.message : "Save failed"));
      }
    } finally {
      setSaving(false);
    }
  };

  const onRevert = () => {
    setRows(original);
    setJsonDraft(JSON.stringify(original, null, 2));
    setError(null);
    setSuccess(null);
  };

  return (
    <div style={{ border: "1px solid #cbd5e1", borderRadius: 6, backgroundColor: "#f8fafc", marginTop: 8 }}>
      <button
        onClick={() => setOpen(!open)}
        style={{
          width: "100%", padding: "6px 10px", border: "none", background: "transparent",
          textAlign: "left", cursor: "pointer", display: "flex", alignItems: "center",
          gap: 8, fontSize: 12, fontWeight: 600, color: "#475569",
        }}
        title="Override FK auto-discovery with an explicit joins[] declaration"
      >
        <span style={{ fontSize: 13 }}>{open ? "▾" : "▸"}</span>
        <span>Explicit joins override (Phase 4)</span>
        {viewName && <span style={{ fontWeight: 400, color: "#94a3b8" }}>— {viewName}</span>}
        {original.length > 0 && (
          <span style={{ fontWeight: 400, color: "#0ea5e9", marginLeft: 4 }}>
            ({original.length} declared)
          </span>
        )}
      </button>
      {open && (
        <div style={{ padding: "4px 12px 12px 12px", fontSize: 12, color: "#334155" }}>
          <div style={{ marginBottom: 8, lineHeight: 1.4 }}>
            When set, view-DDL builds FROM/JOIN clauses verbatim from this list
            and skips FK auto-discovery entirely. Any source table a mapping
            references but not covered here raises <code>ViewGenerationError</code> —
            we refuse to silently emit <code>CROSS JOIN</code>. Use when FK BFS
            picks a wrong bridge.
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 6 }}>
            <button
              onClick={jsonMode ? exitJsonMode : enterJsonMode}
              style={{
                padding: "2px 10px", borderRadius: 4, border: "1px solid #cbd5e1",
                backgroundColor: "#fff", color: "#475569", fontSize: 11, cursor: "pointer",
              }}
              title={jsonMode ? "Switch back to per-row form (round-trips through JSON.parse)" : "Edit raw JSON instead of form rows"}
            >
              {jsonMode ? "↺ Back to form" : "{ } View as JSON"}
            </button>
          </div>
          {loading ? (
            <div style={{ color: "#94a3b8", padding: 8 }}>Loading…</div>
          ) : jsonMode ? (
            <>
              <textarea
                value={jsonDraft}
                onChange={(e) => { setJsonDraft(e.target.value); setSuccess(null); setError(null); }}
                spellCheck={false}
                rows={Math.max(10, rows.length * 5 + 2)}
                style={{
                  width: "100%", boxSizing: "border-box",
                  fontFamily: "'Fira Code', monospace", fontSize: 12,
                  padding: 8, border: "1px solid #cbd5e1", borderRadius: 4,
                  backgroundColor: "#0f172a", color: "#a5f3fc",
                  resize: "vertical",
                }}
              />
              <div style={{ fontSize: 11, color: "#64748b", marginTop: 4, lineHeight: 1.4 }}>
                Each entry: <code>{`{alias, dataset_uri, kind, predicate, bridge_only?}`}</code>.
                <code>bridge_only: true</code> marks a join-only junction (emitted as SELECT DISTINCT of the predicate keys).
                Empty array <code>[]</code> clears the override and re-enables FK auto-discovery.
              </div>
            </>
          ) : (
            <>
              {rows.length === 0 ? (
                <div style={{
                  padding: 12, borderRadius: 6, border: "1px dashed #cbd5e1",
                  backgroundColor: "#fff", color: "#64748b", textAlign: "center",
                }}>
                  No explicit joins declared. Click <strong>+ Add join</strong> to override FK auto-discovery,
                  or save empty to re-enable auto-discovery.
                </div>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {rows.map((row, i) => (
                    <div
                      key={i}
                      style={{
                        padding: 8, borderRadius: 6, border: "1px solid #cbd5e1",
                        backgroundColor: "#fff",
                        display: "grid", gridTemplateColumns: "auto 1fr auto", gap: 6, alignItems: "start",
                      }}
                    >
                      <div style={{ fontSize: 11, fontWeight: 600, color: "#94a3b8", marginTop: 4 }}>
                        #{i + 1}
                      </div>
                      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6 }}>
                          <label style={fieldLabelStyle}>
                            <span>Alias</span>
                            <input
                              value={row.alias}
                              onChange={(e) => updateRow(i, { alias: e.target.value })}
                              placeholder="emp"
                              style={inputStyle}
                            />
                          </label>
                          <label style={fieldLabelStyle}>
                            <span>Kind</span>
                            <select
                              value={row.kind}
                              onChange={(e) => updateRow(i, { kind: e.target.value })}
                              style={inputStyle}
                            >
                              {JOIN_KINDS.map((k) => <option key={k} value={k}>{k.toUpperCase()} JOIN</option>)}
                            </select>
                          </label>
                        </div>
                        <label style={fieldLabelStyle}>
                          <span>Dataset URI</span>
                          <input
                            value={row.dataset_uri}
                            onChange={(e) => updateRow(i, { dataset_uri: e.target.value })}
                            placeholder="dataset:my-proj:public.employee  or  dprod:ds:my-contract:emp"
                            style={{ ...inputStyle, fontFamily: "'Fira Code', monospace", fontSize: 11 }}
                          />
                        </label>
                        <label style={fieldLabelStyle}>
                          <span>Predicate {row.kind === "cross" ? <em style={{ color: "#94a3b8" }}>(not used for CROSS)</em> : ""}</span>
                          <textarea
                            value={row.predicate}
                            onChange={(e) => updateRow(i, { predicate: e.target.value })}
                            placeholder={row.kind === "cross" ? "" : "emp.dept_id = dept.id"}
                            disabled={row.kind === "cross"}
                            rows={2}
                            style={{ ...inputStyle, fontFamily: "'Fira Code', monospace", fontSize: 11, resize: "vertical" }}
                          />
                        </label>
                        {i > 0 && (
                          <label
                            style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11, color: "#475569", cursor: "pointer" }}
                            title="Join-only entry contributing no SELECT columns — emitted as SELECT DISTINCT of the predicate keys so a ledger-grain junction can't fan the view out"
                          >
                            <input
                              type="checkbox"
                              checked={!!row.bridge_only}
                              onChange={(e) => updateRow(i, e.target.checked ? { bridge_only: true } : { bridge_only: undefined })}
                            />
                            <span>
                              Pure bridge (no columns) {row.bridge_only && <em style={{ color: "#0ea5e9" }}>— emitted as SELECT DISTINCT of the predicate keys</em>}
                            </span>
                          </label>
                        )}
                      </div>
                      <button
                        onClick={() => removeRow(i)}
                        title="Remove this join"
                        style={{
                          width: 28, height: 28, borderRadius: 4,
                          border: "1px solid #fecaca", backgroundColor: "#fef2f2",
                          color: "#991b1b", fontSize: 14, fontWeight: 700, cursor: "pointer",
                        }}
                      >
                        ×
                      </button>
                    </div>
                  ))}
                </div>
              )}
              <button
                onClick={addRow}
                style={{
                  marginTop: 8, padding: "4px 12px", borderRadius: 4,
                  border: "1px solid #0ea5e9", backgroundColor: "#e0f2fe",
                  color: "#075985", fontSize: 12, fontWeight: 600, cursor: "pointer",
                }}
              >
                + Add join
              </button>
            </>
          )}
          {error && (
            <pre
              style={{
                margin: "8px 0 0 0", padding: 8, borderRadius: 4,
                backgroundColor: "#fef2f2", color: "#991b1b",
                fontSize: 11, lineHeight: 1.4, whiteSpace: "pre-wrap",
              }}
            >
              {error}
            </pre>
          )}
          {success && (
            <div
              style={{
                marginTop: 8, padding: 8, borderRadius: 4,
                backgroundColor: "#f0fdf4", color: "#166534", fontSize: 11,
              }}
            >
              {success}
            </div>
          )}
          <div style={{ marginTop: 8, display: "flex", gap: 8, alignItems: "center" }}>
            <button
              onClick={onSave}
              disabled={saving || !dirty}
              style={{
                padding: "4px 12px", border: "none", borderRadius: 4,
                backgroundColor: dirty && !saving ? "#0ea5e9" : "#cbd5e1",
                color: "#fff", fontSize: 12, fontWeight: 600,
                cursor: dirty && !saving ? "pointer" : "not-allowed",
              }}
            >
              {saving ? "Saving…" : "Save joins"}
            </button>
            <button
              onClick={onRevert}
              disabled={!dirty}
              style={{
                padding: "4px 12px", border: "1px solid #cbd5e1",
                backgroundColor: "#fff", color: "#475569", borderRadius: 4,
                fontSize: 12, cursor: dirty ? "pointer" : "not-allowed",
              }}
            >
              Revert
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  width: "100%", boxSizing: "border-box",
  padding: "5px 8px", fontSize: 12,
  border: "1px solid #cbd5e1", borderRadius: 4,
  backgroundColor: "#fff",
};

const fieldLabelStyle: React.CSSProperties = {
  display: "flex", flexDirection: "column", gap: 3, fontSize: 11, color: "#475569",
};
