import { useEffect, useMemo, useState } from "react";
import api from "../api/client";
import {
  type PendingMapping,
  type TransformKind,
} from "../types";
import TransformEditor, { type TransformEditorValue } from "./TransformEditor";

/**
 * Steward-side queue of mappings escalated by the Data Engineer
 * (status='steward_review'). The steward can:
 *   - "Answer inline" — write a fresh transform for the engineer; mapping
 *     returns to pending_review with transformAuthor='steward_catalog'.
 *   - "Add to catalog" — coming in Phase 7 (writes a new YAML template).
 *   - "Bounce to PO" — coming in Phase 6 next (flips contract to revision_requested).
 *
 * Reads `GET  /api/projects/{id}/reviews/transformation_escalations`
 * Writes `POST /api/projects/{id}/reviews/transformation_escalations` with action='answer_inline'.
 */

interface Props {
  projectId: number;
  onReviewComplete: () => void;
}

const safeParse = <T,>(s: string | null, fallback: T): T => {
  if (!s) return fallback;
  try { return JSON.parse(s) as T; } catch { return fallback; }
};

const toEditorValue = (m: PendingMapping): TransformEditorValue => ({
  transform_kind: (m.transform_kind ?? "expression") as TransformKind,
  transform_expression: m.transform_expression ?? "",
  transform_inputs: safeParse<string[]>(m.transform_inputs_json, m.sources.map((s) => s.uri).filter((u): u is string => !!u)),
  transform_params: safeParse<Record<string, unknown>>(m.transform_params_json, {}),
  transform_decorators: safeParse<{ standardization?: string[]; default_if_null?: string }>(
    m.transform_decorators_json, {},
  ),
});

export default function TransformationEscalationsPanel({ projectId, onReviewComplete }: Props) {
  const [items, setItems] = useState<PendingMapping[]>([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [editorByUri, setEditorByUri] = useState<Record<string, TransformEditorValue>>({});

  const load = async () => {
    setLoading(true);
    try {
      const res = await api.get(`/api/projects/${projectId}/reviews/transformation_escalations`);
      const list: PendingMapping[] = res.data.items ?? [];
      setItems(list);
      const seeds: Record<string, TransformEditorValue> = {};
      for (const it of list) seeds[it.mapping_uri] = toEditorValue(it);
      setEditorByUri(seeds);
    } catch {
      setItems([]);
    }
    setLoading(false);
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [projectId]);

  const grouped = useMemo(() => {
    const out: Record<string, PendingMapping[]> = {};
    for (const it of items) {
      const key = it.product_name || "(unknown product)";
      (out[key] ||= []).push(it);
    }
    return out;
  }, [items]);

  const handleAnswer = async (m: PendingMapping) => {
    const val = editorByUri[m.mapping_uri];
    if (!val) return;
    setSubmitting(true);
    try {
      await api.post(`/api/projects/${projectId}/reviews/transformation_escalations`, {
        action: "answer_inline",
        mapping_uri: m.mapping_uri,
        transform_kind: val.transform_kind,
        transform_expression: val.transform_expression,
        transform_inputs: val.transform_inputs,
        transform_params: val.transform_params,
        transform_decorators: val.transform_decorators,
        reviewer: "workbench-steward",
      });
      await load();
      onReviewComplete();
    } catch (err) {
      console.error(err);
    }
    setSubmitting(false);
  };

  if (loading) return <div style={{ color: "#64748b" }}>Loading transformation escalations...</div>;
  if (items.length === 0) {
    return (
      <div style={{ padding: 16, color: "#64748b", fontSize: 14 }}>
        No mappings are awaiting steward attention.
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h3 style={{ margin: 0, color: "#334155" }}>
          Transformation Escalations <span style={{ color: "#94a3b8", fontWeight: 400 }}>({items.length} pending)</span>
        </h3>
      </div>

      {Object.entries(grouped).map(([product, list]) => (
        <div key={product}>
          <div style={{ fontSize: 12, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 8 }}>
            {product}
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {list.map((m) => (
              <div key={m.mapping_uri} style={{ backgroundColor: "#fff", borderRadius: 8, border: "1px solid #e2e8f0", padding: 16 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 12 }}>
                  <div>
                    <div style={{ fontWeight: 600, fontSize: 14, color: "#0f172a" }}>
                      {m.product_col_name}
                      <span style={{ marginLeft: 8, fontSize: 11, color: "#94a3b8" }}>
                        ← {m.sources.map((s) => `${s.schema}.${s.table}.${s.name}`).join(", ")}
                      </span>
                    </div>
                    {m.product_col_description && (
                      <div style={{ fontSize: 12, color: "#475569", marginTop: 2, fontStyle: "italic" }}>
                        {m.product_col_description}
                      </div>
                    )}
                  </div>
                </div>

                {m.transform_escalation_reason && (
                  <div style={{
                    padding: "8px 12px", backgroundColor: "#fef3c7",
                    borderLeft: "3px solid #f59e0b", borderRadius: 4,
                    fontSize: 13, color: "#92400e", marginBottom: 12,
                  }}>
                    <strong>Engineer flagged: </strong>{m.transform_escalation_reason}
                  </div>
                )}

                {editorByUri[m.mapping_uri] && (
                  <TransformEditor
                    value={editorByUri[m.mapping_uri]}
                    sources={m.sources}
                    author={m.transform_author}
                    confidence={m.transform_confidence}
                    onChange={(next) => setEditorByUri((prev) => ({ ...prev, [m.mapping_uri]: next }))}
                    saving={submitting}
                  />
                )}

                <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
                  <button
                    onClick={() => handleAnswer(m)}
                    disabled={submitting}
                    style={{
                      padding: "8px 16px", borderRadius: 6, border: "none",
                      backgroundColor: "#22c55e", color: "#fff", fontWeight: 600,
                      fontSize: 13, cursor: submitting ? "wait" : "pointer",
                    }}
                  >
                    Answer Inline
                  </button>
                  <button
                    disabled
                    title="Coming in Phase 7"
                    style={{
                      padding: "8px 16px", borderRadius: 6, border: "1px solid #cbd5e1",
                      backgroundColor: "#f1f5f9", color: "#94a3b8", fontWeight: 600,
                      fontSize: 13, cursor: "not-allowed",
                    }}
                  >
                    Add to Catalog
                  </button>
                  <button
                    disabled
                    title="Coming in Phase 6 next slice"
                    style={{
                      padding: "8px 16px", borderRadius: 6, border: "1px solid #cbd5e1",
                      backgroundColor: "#f1f5f9", color: "#94a3b8", fontWeight: 600,
                      fontSize: 13, cursor: "not-allowed",
                    }}
                  >
                    Bounce to PO
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
