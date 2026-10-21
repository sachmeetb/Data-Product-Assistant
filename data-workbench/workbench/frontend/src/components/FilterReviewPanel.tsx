/**
 * Engineer-facing review/finalize surface for the dataset-level row filter
 * (`:DatasetTransform.filterPredicate`). Mounted on the engineer's
 * ProjectDashboard serving-card detail, next to JoinsOverridePanel.
 *
 * The Product Owner authors the filter in plain language ("active employees
 * only") in the wizard; that prose is interpreted to a candidate SQL predicate
 * and stored. The engineer OWNS the final SQL: this panel shows the PO's intent
 * (read-only) plus the editable compiled predicate, validates it through the
 * shared safety gate (PUT /dataset-transform/filter), and prompts a serving
 * re-run on save. A predicate that still reads like plain language is refused
 * by the backend with a clear message — it can never reach a deployed WHERE.
 */

import { useEffect, useRef, useState } from "react";
import api from "../api/client";

interface DatasetTransformResponse {
  output_dataset_uri: string;
  filter: string;
  filter_intent: string;
}

interface ObservedColumn {
  name: string;
  type: string;
  top_values: string[];
}

interface Props {
  projectId: number;
  outputDatasetUri: string;
  viewName?: string;
}

export default function FilterReviewPanel({ projectId, outputDatasetUri, viewName }: Props) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [intent, setIntent] = useState<string>("");
  const [predicate, setPredicate] = useState<string>("");
  const [original, setOriginal] = useState<string>("");
  const [observed, setObserved] = useState<ObservedColumn[]>([]);
  const taRef = useRef<HTMLTextAreaElement>(null);

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
        setIntent(r.data.filter_intent || "");
        setPredicate(r.data.filter || "");
        setOriginal(r.data.filter || "");
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Failed to load filter");
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [open, projectId, outputDatasetUri]);

  // Observed profile values from the bound sources (best-effort — hidden on failure).
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    api.get<{ columns: ObservedColumn[] }>(
      `/api/projects/${projectId}/dataset-transform/observed-values`,
      { params: { output_dataset_uri: outputDatasetUri } }
    )
      .then((r) => { if (!cancelled) setObserved(r.data.columns || []); })
      .catch(() => { if (!cancelled) setObserved([]); });
    return () => { cancelled = true; };
  }, [open, projectId, outputDatasetUri]);

  // Insert a single-quoted literal (exact stored casing) at the caret so the
  // engineer authors the real value directly — no casing guesswork.
  const insertValue = (raw: string) => {
    const lit = `'${raw.replace(/'/g, "''")}'`;
    setSuccess(null);
    setError(null);
    const el = taRef.current;
    if (!el) {
      setPredicate((p) => p + lit);
      return;
    }
    const start = el.selectionStart ?? predicate.length;
    const end = el.selectionEnd ?? predicate.length;
    setPredicate(predicate.slice(0, start) + lit + predicate.slice(end));
    requestAnimationFrame(() => {
      el.focus();
      const pos = start + lit.length;
      el.setSelectionRange(pos, pos);
    });
  };

  const dirty = predicate.trim() !== original.trim();

  const onSave = async () => {
    setError(null);
    setSuccess(null);
    setSaving(true);
    try {
      const res = await api.put<{ warning?: string | null }>(
        `/api/projects/${projectId}/dataset-transform/filter`,
        { output_dataset_uri: outputDatasetUri, filter: predicate.trim() }
      );
      setOriginal(predicate.trim());
      setSuccess(
        (res.data?.warning ? `${res.data.warning} ` : "") +
        "Saved. Re-run the serving stage so the view picks up the finalized filter."
      );
    } catch (e: unknown) {
      const errObj = e as { response?: { data?: { detail?: { message?: string } | string } } };
      const detail = errObj?.response?.data?.detail;
      if (detail && typeof detail === "object" && detail.message) {
        setError(detail.message);
      } else {
        setError(typeof detail === "string" ? detail : (e instanceof Error ? e.message : "Save failed"));
      }
    } finally {
      setSaving(false);
    }
  };

  const onRevert = () => {
    setPredicate(original);
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
        title="Review the PO's plain-language filter and finalize the SQL the view will use"
      >
        <span style={{ fontSize: 13 }}>{open ? "▾" : "▸"}</span>
        <span>Row filter review</span>
        {viewName && <span style={{ fontWeight: 400, color: "#94a3b8" }}>— {viewName}</span>}
        {original.trim() && (
          <span style={{ fontWeight: 400, color: "#0ea5e9", marginLeft: 4 }}>(filter set)</span>
        )}
      </button>
      {open && (
        <div style={{ padding: "4px 12px 12px 12px", fontSize: 12, color: "#334155" }}>
          {loading ? (
            <div style={{ color: "#94a3b8", padding: 8 }}>Loading…</div>
          ) : (
            <>
              <div style={{ marginBottom: 8, lineHeight: 1.4 }}>
                The Product Owner described which rows the product should include.
                Finalize the exact SQL <code>WHERE</code> condition below — it is emitted
                verbatim into the view's base CTE. Plain-language text is refused so it
                can't break the deploy.
              </div>
              <div style={{ marginBottom: 8 }}>
                <div style={{ fontSize: 11, color: "#64748b", marginBottom: 2 }}>Product Owner intent</div>
                <div style={{
                  padding: 8, borderRadius: 4, border: "1px solid #e2e8f0",
                  backgroundColor: "#fff", color: intent ? "#334155" : "#94a3b8",
                  fontStyle: intent ? "normal" : "italic",
                }}>
                  {intent || "— (no plain-language intent recorded; filter authored directly as SQL)"}
                </div>
              </div>
              {observed.length > 0 && (
                <div style={{ marginBottom: 8 }}>
                  <div style={{ fontSize: 11, color: "#64748b", marginBottom: 4 }}>
                    Observed values (from profiled sources) — click to insert the exact literal
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                    {observed.map((c) => (
                      <div key={c.name} style={{ display: "flex", flexWrap: "wrap", gap: 4, alignItems: "baseline" }}>
                        <code style={{ fontSize: 11, color: "#334155" }}>{c.name}:</code>
                        {c.top_values.map((v) => (
                          <button
                            key={v}
                            type="button"
                            onClick={() => insertValue(v)}
                            title={`Insert '${v}'`}
                            style={{
                              fontFamily: "'Fira Code', monospace", fontSize: 11,
                              padding: "1px 6px", border: "1px solid #cbd5e1", borderRadius: 10,
                              backgroundColor: "#fff", color: "#0369a1", cursor: "pointer",
                            }}
                          >
                            {v}
                          </button>
                        ))}
                      </div>
                    ))}
                  </div>
                </div>
              )}
              <label style={{ display: "flex", flexDirection: "column", gap: 3, fontSize: 11, color: "#475569" }}>
                <span>Filter predicate (SQL — no <code>WHERE</code> keyword)</span>
                <textarea
                  ref={taRef}
                  value={predicate}
                  onChange={(e) => { setPredicate(e.target.value); setSuccess(null); setError(null); }}
                  spellCheck={false}
                  rows={2}
                  placeholder="employment_status = 'active'"
                  style={{
                    width: "100%", boxSizing: "border-box",
                    fontFamily: "'Fira Code', monospace", fontSize: 12,
                    padding: 8, border: "1px solid #cbd5e1", borderRadius: 4,
                    backgroundColor: "#fff", resize: "vertical",
                  }}
                />
              </label>
              {error && (
                <div style={{
                  margin: "8px 0 0 0", padding: 8, borderRadius: 4,
                  backgroundColor: "#fef2f2", color: "#991b1b", fontSize: 11, lineHeight: 1.4,
                }}>
                  {error}
                </div>
              )}
              {success && (
                <div style={{
                  marginTop: 8, padding: 8, borderRadius: 4,
                  backgroundColor: "#f0fdf4", color: "#166534", fontSize: 11,
                }}>
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
                  {saving ? "Validating…" : "Validate & save"}
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
            </>
          )}
        </div>
      )}
    </div>
  );
}
