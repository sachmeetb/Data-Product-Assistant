import { useMemo, useState } from "react";
import api from "../api/client";
import ModalShell from "./ModalShell";
import TransformEditor, { buildPreview, type TransformEditorValue } from "./TransformEditor";
import TransformSuggestionCard, { type TransformInterpretResult } from "./TransformSuggestionCard";
import {
  type SourceColumn,
  type MappingSource,
  type TransformKind,
  TRANSFORM_KIND_LABELS,
} from "../types";

interface StageReset {
  stage_id: string;
  stage_number: number;
  workflow_id: string | null;
}

interface Props {
  projectId: number;
  mappingUri: string;
  productColName: string;
  /** Seeded transform (kind / inputs / params / …) — from the mapping being edited,
   *  or a Direct default with a newly-wired source when opened via drag-to-wire. */
  initialValue: TransformEditorValue;
  availableSources: SourceColumn[];
  /** True when opened by drag-to-wire (a net-new source), false when editing an edge. */
  isNew?: boolean;
  /** The mapping's review status; when 'pending_review' the dialog offers Approve. */
  status?: string;
  onClose: () => void;
  /** Called after a successful save with the downstream stages that were reset. */
  onSaved: (stagesReset: StageReset[]) => void;
}

const ACCENT = "#7c3aed";

const KIND_OPTIONS: TransformKind[] = [
  "direct", "cast", "format", "concat", "split", "substring",
  "case", "arithmetic", "lookup", "literal", "expression",
  "bucket", "mask", "hash", "window", "date_difference",
];

const toMappingSource = (s: SourceColumn): MappingSource => ({
  uri: s.uri,
  schema: s.table_schema ?? null,
  table: s.table_name ?? null,
  name: s.column_name ?? s.name,
  dataType: s.data_type ?? null,
  description: s.description ?? null,
});

const bareFromUri = (uri: string): string => {
  const m = uri.match(/[^.:]+$/);
  return m ? m[0] : uri;
};

/** In-context, NL-first transform configurator. Opens as a pop-up when the engineer
 *  double-clicks a mapping edge (edit) or finishes a drag-to-wire (configure a new
 *  source→target). It leads with plain-language authoring ("describe this column"),
 *  shows a compact kind / source / SQL summary, and tucks the full field editor under
 *  "Advanced". Saving writes through the SAME replace_mapping choke point as the form —
 *  no new write path; editing an approved mapping flips it back to pending_review and may
 *  reset downstream serving/deploy (the cascade rides `stages_reset`). */
export default function TransformEditorDialog({
  projectId, mappingUri, productColName, initialValue, availableSources, isNew, status, onClose, onSaved,
}: Props) {
  const [value, setValue] = useState<TransformEditorValue>(initialValue);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isLiteral = value.transform_kind === "literal";

  // The selected input columns, as MappingSource (for the editor's pills + SQL preview).
  // value.transform_inputs is the single source of truth for the source set.
  const pickedColumns = useMemo<MappingSource[]>(() => {
    const byUri = new Map(availableSources.map((s) => [s.uri, s]));
    return value.transform_inputs.map((uri) => {
      const sc = byUri.get(uri);
      return sc
        ? toMappingSource(sc)
        : { uri, schema: null, table: null, name: bareFromUri(uri), dataType: null, description: null };
    });
  }, [value.transform_inputs, availableSources]);

  const previewSql = useMemo(() => buildPreview(value, pickedColumns), [value, pickedColumns]);
  const kindLabel = TRANSFORM_KIND_LABELS[value.transform_kind] || value.transform_kind;
  const fromLabel = isLiteral
    ? "constant (no source column)"
    : pickedColumns.map((s) => s.name).filter(Boolean).join(", ") || "— (pick a source below)";

  const applySuggestion = (r: TransformInterpretResult) => {
    if (!r.transform_kind) return;
    setValue((v) => ({
      ...v,
      transform_kind: r.transform_kind as TransformKind,
      transform_expression: r.transform_expression || "",
      transform_inputs: r.transform_kind === "literal" ? [] : r.transform_inputs,
      transform_params: r.transform_params || {},
      transform_decorators: r.transform_decorators || {},
    }));
  };

  const toggleSource = (uri: string) => {
    setValue((v) => {
      const has = v.transform_inputs.includes(uri);
      return {
        ...v,
        transform_inputs: has
          ? v.transform_inputs.filter((u) => u !== uri)
          : [...v.transform_inputs, uri],
      };
    });
  };

  const save = async () => {
    const inputs = isLiteral ? [] : value.transform_inputs;
    if (!isLiteral && inputs.length === 0) {
      setError("Pick at least one source column (open Advanced).");
      setAdvancedOpen(true);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const resp = await api.post(`/api/projects/${projectId}/reviews/mappings`, {
        action: "replace_mapping",
        mapping_uri: mappingUri,
        transform_kind: value.transform_kind,
        transform_expression: value.transform_expression,
        transform_inputs: inputs,
        transform_params: value.transform_params,
        transform_decorators: value.transform_decorators,
        source_col_uris: isLiteral ? undefined : inputs,
        category: "edited_default",
        detail: isNew ? "Wired on the canvas" : "Edited on the canvas",
        reviewer: "workbench-user",
      });
      onSaved((resp.data?.stages_reset || []) as StageReset[]);
    } catch {
      setError("Save failed. Check the fields and try again.");
      setSaving(false);
    }
  };

  const [quality, setQuality] = useState(2);
  const isPending = status === "pending_review";

  const approve = async () => {
    setSaving(true);
    setError(null);
    try {
      const resp = await api.post(`/api/projects/${projectId}/reviews/mappings`, {
        action: "approve", mapping_uri: mappingUri, quality, reviewer: "workbench-user",
      });
      onSaved((resp.data?.stages_reset || []) as StageReset[]);
    } catch {
      setError("Approve failed.");
      setSaving(false);
    }
  };

  const [confirmDel, setConfirmDel] = useState(false);
  const [srcFilter, setSrcFilter] = useState("");

  const deleteMapping = async () => {
    setSaving(true);
    setError(null);
    try {
      const resp = await api.post(`/api/projects/${projectId}/reviews/mappings`, {
        action: "reject", mapping_uri: mappingUri,
        category: "other", detail: "Deleted on the canvas", reviewer: "workbench-user",
      });
      onSaved((resp.data?.stages_reset || []) as StageReset[]);
    } catch {
      setError("Delete failed.");
      setSaving(false);
    }
  };

  const filteredSources = srcFilter.trim()
    ? availableSources.filter((s) =>
        `${s.name} ${s.column_name ?? ""} ${s.data_type ?? ""}`.toLowerCase().includes(srcFilter.trim().toLowerCase()))
    : availableSources;

  const labelId = "transform-dialog-title";
  const rowLabel = { fontSize: 11, fontWeight: 600, color: "#64748b", width: 44, flexShrink: 0 };
  const mono = { fontFamily: "monospace", fontSize: 12, color: "#0f172a", wordBreak: "break-all" as const };

  return (
    <ModalShell open onClose={onClose} closeDisabled={saving} labelledById={labelId} width={680}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12 }}>
        <h3 id={labelId} style={{ margin: 0, fontSize: 16, color: "#0f172a" }}>
          {isNew ? "Wire" : "Configure"} <span style={{ color: ACCENT }}>{productColName}</span>
        </h3>
        <span style={{ fontSize: 12, color: "#94a3b8" }}>Describe it, or edit the fields.</span>
      </div>

      {/* Natural-language authoring — the primary path. */}
      <TransformSuggestionCard
        sources={availableSources}
        targetColumn={productColName}
        mappingUri={mappingUri}
        disabled={saving}
        onApply={applySuggestion}
      />

      {/* Compact summary — kind picker + resolved sources + live SQL. */}
      <div style={{ border: "1px solid #e2e8f0", borderRadius: 8, padding: 12, display: "flex", flexDirection: "column", gap: 8 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={rowLabel}>Kind</span>
          <select
            value={value.transform_kind}
            disabled={saving}
            onChange={(e) => setValue((v) => ({ ...v, transform_kind: e.target.value as TransformKind }))}
            style={{ padding: "5px 8px", fontSize: 13, border: "1px solid #e2e8f0", borderRadius: 6 }}
          >
            {KIND_OPTIONS.map((k) => (
              <option key={k} value={k}>{TRANSFORM_KIND_LABELS[k] || k}</option>
            ))}
          </select>
          <span style={{ fontSize: 12, color: "#94a3b8" }}>{value.transform_kind === "direct" ? "1:1 passthrough" : kindLabel}</span>
        </div>
        <div style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
          <span style={rowLabel}>From</span>
          <span style={{ fontSize: 12, color: "#334155" }}>{fromLabel}</span>
        </div>
        <div style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
          <span style={rowLabel}>SQL</span>
          <span style={mono}>{previewSql}</span>
        </div>
      </div>

      {/* Advanced — the full field editor, incl. the source picker. */}
      <div>
        <button
          type="button"
          onClick={() => setAdvancedOpen((o) => !o)}
          style={{ background: "none", border: "none", color: ACCENT, fontSize: 13, fontWeight: 600, cursor: "pointer", padding: 0 }}
        >
          {advancedOpen ? "▾" : "▸"} Advanced — pick sources &amp; edit fields
        </button>
        {advancedOpen && (
          <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 12 }}>
            {!isLiteral && (
              <div>
                <div style={{ fontSize: 12, fontWeight: 600, color: "#334155", marginBottom: 4 }}>Source Column(s)</div>
                <input
                  type="text" placeholder="Search columns to map…" value={srcFilter}
                  onChange={(e) => setSrcFilter(e.target.value)}
                  style={{ width: "100%", padding: "5px 8px", fontSize: 12, border: "1px solid #e2e8f0", borderRadius: 6, marginBottom: 6 }}
                />
                <div style={{ maxHeight: 160, overflowY: "auto", border: "1px solid #e2e8f0", borderRadius: 6, backgroundColor: "#f8fafc", padding: 6 }}>
                  {filteredSources.length === 0 && (
                    <div style={{ fontSize: 12, color: "#94a3b8", padding: 8 }}>
                      {availableSources.length === 0 ? "No source columns available." : "No columns match your search."}
                    </div>
                  )}
                  {filteredSources.map((s) => (
                    <label key={s.uri} style={{ display: "flex", alignItems: "center", gap: 8, padding: "3px 6px", fontSize: 12, cursor: "pointer" }}>
                      <input
                        type="checkbox"
                        checked={value.transform_inputs.includes(s.uri)}
                        onChange={() => toggleSource(s.uri)}
                        disabled={saving}
                      />
                      <code style={{ fontSize: 11 }}>{s.name}</code>
                      {s.data_type && <span style={{ fontSize: 10, color: "#94a3b8" }}>{s.data_type}</span>}
                    </label>
                  ))}
                </div>
              </div>
            )}
            <TransformEditor
              value={value}
              sources={pickedColumns}
              author={null}
              confidence={null}
              onChange={setValue}
              saving={saving}
              availableSources={availableSources}
            />
          </div>
        )}
      </div>

      {error && <div style={{ fontSize: 12, color: "#dc2626" }}>{error}</div>}

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, borderTop: "1px solid #f1f5f9", paddingTop: 12 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <button
            type="button"
            onClick={() => (confirmDel ? deleteMapping() : setConfirmDel(true))}
            onBlur={() => setConfirmDel(false)}
            disabled={saving}
            style={{ padding: "6px 12px", fontSize: 12, fontWeight: 600, borderRadius: 6, border: "1px solid #fecaca", backgroundColor: confirmDel ? "#dc2626" : "#fff", color: confirmDel ? "#fff" : "#dc2626", cursor: "pointer" }}
          >
            {confirmDel ? "Confirm delete" : "Delete"}
          </button>
          <span style={{ fontSize: 11, color: "#94a3b8" }}>
            Deleting removes it (re-wire from a source anytime).
          </span>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            style={{ padding: "7px 16px", fontSize: 13, borderRadius: 6, border: "1px solid #cbd5e1", backgroundColor: "#fff", color: "#334155", cursor: "pointer" }}
          >
            Cancel
          </button>
          {isPending && (
            <>
              <select
                value={quality}
                disabled={saving}
                onChange={(e) => setQuality(Number(e.target.value))}
                title="Approval quality rating"
                style={{ padding: "6px 8px", fontSize: 12, border: "1px solid #e2e8f0", borderRadius: 6 }}
              >
                <option value={1}>Acceptable</option>
                <option value={2}>Good</option>
                <option value={3}>Excellent</option>
              </select>
              <button
                type="button"
                onClick={approve}
                disabled={saving}
                style={{ padding: "7px 16px", fontSize: 13, fontWeight: 600, borderRadius: 6, border: "1px solid #16a34a", backgroundColor: "#fff", color: "#166534", cursor: saving ? "default" : "pointer" }}
              >
                ✓ Approve
              </button>
            </>
          )}
          <button
            type="button"
            onClick={save}
            disabled={saving}
            style={{ padding: "7px 18px", fontSize: 13, fontWeight: 600, borderRadius: 6, border: "none", backgroundColor: saving ? "#a7f3d0" : "#16a34a", color: "#fff", cursor: saving ? "default" : "pointer" }}
          >
            {saving ? "Saving…" : "Save changes"}
          </button>
        </div>
      </div>
    </ModalShell>
  );
}
