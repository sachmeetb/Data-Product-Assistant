import { useEffect, useMemo, useRef, useState } from "react";
import api from "../api/client";
import { useCurrentUserEmail } from "../AuthContext";
import {
  type UnmappedColumn,
  type TransformKind,
  type MappingSource,
  type SourceColumn,
} from "../types";
import TransformEditor, { type TransformEditorValue } from "./TransformEditor";
import RelationshipKindChip from "./RelationshipKindChip";
import SensitivityChip from "./SensitivityChip";

/**
 * Engineer-side panel: lists product columns that have NO current
 * :ColumnMapping and lets the engineer hand-create one. The data_mapping
 * skill skips columns whose best candidate scores below 0.60, so this is
 * the only path to fill those gaps without re-running the whole stage.
 *
 * Reads:  GET /api/projects/{id}/reviews/unmapped_columns
 *         GET /api/projects/{id}/reviews/mappings/source-columns
 * Writes: POST /api/projects/{id}/reviews/unmapped_columns
 *         (creates a :ColumnMapping with status='pending_review',
 *          transformAuthor='engineer'; the Reviewer still validates it
 *          via the regular Mappings review queue.)
 */

interface Props {
  projectId: number;
  onReviewComplete: () => void;
  /** Deep-link target: column_uri to pre-expand and scroll to. */
  focusColumnUri?: string | null;
  /** Called once the panel has consumed the focus signal so the parent can clear it. */
  onFocusConsumed?: () => void;
  /**
   * "Guide me" — open the project chat (the project-chat-assistant skill) with a
   * column-scoped prefill so the agent can suggest a source + transform for a
   * column the mapping skill left unmapped. Mirrors MappingReviewPanel.
   */
  onGuideMe?: (prefill: string) => void;
}

/** Column-scoped prompt for the chat assistant to propose a mapping. */
const buildUnmappedGuidePrefill = (uc: UnmappedColumn): string => {
  const typeClause = uc.data_type ? ` (\`${uc.data_type}\`)` : "";
  const descClause = uc.description ? ` — "${uc.description}"` : "";
  const hintClause = uc.transform_hint?.kind
    ? ` There's a PO hint of transform kind \`${uc.transform_hint.kind}\`${uc.transform_hint.inputs?.length ? ` with suggested inputs ${uc.transform_hint.inputs.join(", ")}` : ""}.`
    : "";
  return (
    `The mapping step left product column \`${uc.column_name}\`${typeClause}${descClause} unmapped.`
    + ` Which source column(s) from the data products this one CONSUMES could feed it,`
    + ` and what transform (kind + expression) should I use?${hintClause}`
    + ` If this column is a DERIVED band/tier (e.g. gold/silver/bronze) with no direct source,`
    + ` consider a \`bucket\` (or \`case\`) transform over a numeric source measure`
    + ` (e.g. lifetime_value), and suggest the band boundaries + labels from that measure's`
    + ` profiling percentiles. A bucket works in a virtual view; only choose materialization`
    + ` if the product needs SCD2 history of how the tier changed over time.`
    + ` If it is a METRIC/SCORE computed per-entity from a consumed fact/ledger table`
    + ` (counts, sums, recency, or a formula combining several aggregates), recommend a`
    + ` \`lookup\` transform with selection_strategy \`aggregate\`: a single`
    + ` \`aggregate_function\` + \`value_column\` for one aggregate, or a raw`
    + ` \`aggregate_expression\` (multiple aggregates, each with its own FILTER (WHERE ...))`
    + ` for composite formulas. transformInputs anchors on the MAIN table's join key;`
    + ` params carry lookup_table + key_column. Give me the exact field values to enter`
    + ` in the transform editor (Kind, Inputs, Strategy, Lookup table, Key column, and`
    + ` expression), and if I describe a formula, translate it into those fields.`
  );
};

const DEFAULT_EDITOR_VALUE: TransformEditorValue = {
  transform_kind: "direct",
  transform_expression: "",
  transform_inputs: [],
  transform_params: {},
  transform_decorators: {},
};

const seedFromHint = (uc: UnmappedColumn, picked: SourceColumn[]): TransformEditorValue => {
  const hint = uc.transform_hint;
  if (!hint || !hint.kind) {
    // No PO hint — default to direct if exactly one source picked, otherwise
    // concat as a sensible multi-source default.
    return {
      transform_kind: (picked.length > 1 ? "concat" : "direct") as TransformKind,
      transform_expression: "",
      transform_inputs: picked.map((s) => s.uri),
      transform_params: picked.length > 1 ? { separator: " " } : {},
      transform_decorators: {},
    };
  }
  return {
    transform_kind: hint.kind as TransformKind,
    transform_expression: hint.expression ?? "",
    transform_inputs: picked.map((s) => s.uri),
    transform_params: { ...(hint.params ?? {}), ...(hint.separator ? { separator: hint.separator } : {}) },
    transform_decorators: hint.decorators ?? {},
  };
};

export default function UnmappedColumnsPanel({
  projectId,
  onReviewComplete,
  focusColumnUri,
  onFocusConsumed,
  onGuideMe,
}: Props) {
  const engineerEmail = useCurrentUserEmail();
  const [items, setItems] = useState<UnmappedColumn[]>([]);
  const [sources, setSources] = useState<SourceColumn[]>([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  // Search box over the source-column picker (only one column is expanded at a
  // time, so a single filter string is enough).
  const [sourceSearch, setSourceSearch] = useState("");
  // Per-column picked source URIs and editor state (keyed by product col URI).
  const [pickedByColumn, setPickedByColumn] = useState<Record<string, Set<string>>>({});
  const [editorByColumn, setEditorByColumn] = useState<Record<string, TransformEditorValue>>({});
  const [errorByColumn, setErrorByColumn] = useState<Record<string, string>>({});
  // Engineer→PO "no source covers this column" escalation. Tracks which
  // column the engineer is currently composing a request for; the inline
  // form captures the reason. `poRequestSent` is the set of column URIs
  // already escalated this session.
  const [poRequestColumn, setPoRequestColumn] = useState<UnmappedColumn | null>(null);
  const [poRequestReason, setPoRequestReason] = useState<string>("");
  const [poRequestSent, setPoRequestSent] = useState<Set<string>>(new Set());
  const [poRequestSubmitting, setPoRequestSubmitting] = useState(false);
  const rowRefs = useRef<Record<string, HTMLDivElement | null>>({});

  const sendPoRequest = async () => {
    if (!poRequestColumn || !poRequestReason.trim()) return;
    setPoRequestSubmitting(true);
    try {
      await api.post(
        `/api/projects/${projectId}/product-requests/source-candidates-needed`,
        {
          engineer: engineerEmail,
          notes: "Engineer flagged an unmapped consumer column with no covering source.",
          gap_column_uri: poRequestColumn.column_uri,
          gap_reason: poRequestReason.trim(),
        },
      );
      setPoRequestSent((prev) => new Set(prev).add(poRequestColumn.column_uri));
      setPoRequestColumn(null);
      setPoRequestReason("");
    } catch (e) {
      console.error("Failed to send source-candidates-needed request", e);
    }
    setPoRequestSubmitting(false);
  };

  const load = async () => {
    setLoading(true);
    try {
      const [unmappedRes, sourcesRes] = await Promise.all([
        api.get(`/api/projects/${projectId}/reviews/unmapped_columns`),
        api.get(`/api/projects/${projectId}/reviews/mappings/source-columns`),
      ]);
      setItems(unmappedRes.data.items ?? []);
      setSources(sourcesRes.data.columns ?? []);
    } catch {
      setItems([]);
      setSources([]);
    }
    setLoading(false);
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [projectId]);

  // Deep-link from the dashboard: once items are loaded and a focus URI is
  // pending, auto-expand the matching row, scroll it into view, then clear
  // the focus signal so the parent doesn't keep retriggering us.
  useEffect(() => {
    if (!focusColumnUri || items.length === 0) return;
    const target = items.find((it) => it.column_uri === focusColumnUri);
    if (!target) {
      // The deep-linked column isn't unmapped here — e.g. someone else
      // mapped it between dashboard click and panel mount. Drop the signal.
      onFocusConsumed?.();
      return;
    }
    expand(target);
    requestAnimationFrame(() => {
      rowRefs.current[focusColumnUri]?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    onFocusConsumed?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusColumnUri, items]);

  const grouped = useMemo(() => {
    const out: Record<string, UnmappedColumn[]> = {};
    for (const it of items) {
      const key = it.product_name || "(unknown product)";
      (out[key] ||= []).push(it);
    }
    return out;
  }, [items]);

  const togglePick = (colUri: string, sourceUri: string) => {
    setPickedByColumn((prev) => {
      const set = new Set(prev[colUri] ?? []);
      if (set.has(sourceUri)) set.delete(sourceUri);
      else set.add(sourceUri);
      const next = { ...prev, [colUri]: set };

      // Sync editor inputs when the picked set changes.
      const uc = items.find((i) => i.column_uri === colUri);
      if (uc) {
        const pickedList = sources.filter((s) => set.has(s.uri));
        const current = editorByColumn[colUri];
        if (current) {
          setEditorByColumn((eprev) => ({
            ...eprev,
            [colUri]: { ...current, transform_inputs: pickedList.map((s) => s.uri) },
          }));
        }
      }
      return next;
    });
  };

  const expand = (uc: UnmappedColumn) => {
    if (expanded === uc.column_uri) {
      setExpanded(null);
      return;
    }
    setExpanded(uc.column_uri);
    if (!editorByColumn[uc.column_uri]) {
      // Pre-pick sources that match the PO hint's input names, if any.
      const hintInputs = uc.transform_hint?.inputs ?? [];
      const preMatched = sources.filter((s) =>
        hintInputs.some((hint) => hint.toLowerCase() === (s.name || "").toLowerCase()),
      );
      const pickedSet = new Set(preMatched.map((s) => s.uri));
      setPickedByColumn((prev) => ({ ...prev, [uc.column_uri]: pickedSet }));
      setEditorByColumn((prev) => ({ ...prev, [uc.column_uri]: seedFromHint(uc, preMatched) }));
    }
  };

  const submit = async (uc: UnmappedColumn) => {
    const picked = Array.from(pickedByColumn[uc.column_uri] ?? []);
    const editor = editorByColumn[uc.column_uri] ?? DEFAULT_EDITOR_VALUE;
    const isLiteral = editor.transform_kind === "literal";
    if (!isLiteral && picked.length === 0) {
      setErrorByColumn((prev) => ({ ...prev, [uc.column_uri]: "Pick at least one source column." }));
      return;
    }
    if (isLiteral && !((editor.transform_params?.literal_value as string) || "").trim()) {
      setErrorByColumn((prev) => ({ ...prev, [uc.column_uri]: "Literal value is required." }));
      return;
    }
    setErrorByColumn((prev) => ({ ...prev, [uc.column_uri]: "" }));
    setSubmitting(true);
    try {
      await api.post(`/api/projects/${projectId}/reviews/unmapped_columns`, {
        product_col_uri: uc.column_uri,
        source_col_uris: isLiteral ? [] : picked,
        transform_kind: editor.transform_kind,
        transform_expression: editor.transform_expression,
        transform_inputs: isLiteral ? [] : (editor.transform_inputs.length ? editor.transform_inputs : picked),
        transform_params: editor.transform_params,
        transform_decorators: editor.transform_decorators,
        rationale: `Engineer-authored mapping for ${uc.column_name}.`,
        reviewer: "workbench-engineer",
      });
      // Reset local state for this column and refetch.
      setExpanded(null);
      setPickedByColumn((prev) => { const n = { ...prev }; delete n[uc.column_uri]; return n; });
      setEditorByColumn((prev) => { const n = { ...prev }; delete n[uc.column_uri]; return n; });
      await load();
      onReviewComplete();
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
        ?? "Failed to create mapping.";
      setErrorByColumn((prev) => ({ ...prev, [uc.column_uri]: detail }));
    }
    setSubmitting(false);
  };

  if (loading) return <div style={{ color: "#64748b" }}>Loading unmapped columns...</div>;
  if (items.length === 0) {
    return (
      <div style={{ padding: 16, color: "#64748b", fontSize: 14 }}>
        Every product column in this project has a current mapping.
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h3 style={{ margin: 0, color: "#334155" }}>
          Unmapped Product Columns{" "}
          <span style={{ color: "#94a3b8", fontWeight: 400 }}>({items.length} need a mapping)</span>
        </h3>
      </div>

      {Object.entries(grouped).map(([product, list]) => (
        <div key={product}>
          <div style={{
            fontSize: 12, fontWeight: 700, color: "#475569",
            textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 8,
          }}>{product}</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {list.map((uc) => {
              const isOpen = expanded === uc.column_uri;
              const pickedSet = pickedByColumn[uc.column_uri] ?? new Set<string>();
              const editorValue = editorByColumn[uc.column_uri] ?? DEFAULT_EDITOR_VALUE;
              const editorSources: MappingSource[] = sources
                .filter((s) => pickedSet.has(s.uri))
                .map((s) => ({
                  uri: s.uri, schema: null, table: null,
                  name: s.name, dataType: s.data_type, description: s.description,
                }));

              const isLiteralKind = editorValue.transform_kind === "literal";
              return (
                <div
                  key={uc.column_uri}
                  ref={(el) => { rowRefs.current[uc.column_uri] = el; }}
                  style={{
                    backgroundColor: "#fff", borderRadius: 8,
                    border: focusColumnUri === uc.column_uri ? "2px solid #f59e0b" : "1px solid #e2e8f0",
                    padding: 14,
                    transition: "border-color 0.3s",
                  }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
                    <div>
                      <div style={{ fontWeight: 600, fontSize: 14, color: "#0f172a" }}>
                        {uc.column_name}
                        {uc.data_type && (
                          <span style={{ fontWeight: 400, color: "#64748b", marginLeft: 8, fontFamily: "monospace", fontSize: 12 }}>
                            {uc.data_type}
                          </span>
                        )}
                        {uc.transform_hint?.kind && (
                          <span style={{ marginLeft: 8, fontSize: 10, fontWeight: 700, padding: "1px 6px", borderRadius: 4, backgroundColor: "#dbeafe", color: "#1e40af" }}>
                            PO hint: {uc.transform_hint.kind}
                          </span>
                        )}
                      </div>
                      {uc.description && (
                        <div style={{ fontSize: 12, color: "#475569", marginTop: 2, fontStyle: "italic" }}>
                          {uc.description}
                        </div>
                      )}
                      {uc.transform_hint?.inputs && uc.transform_hint.inputs.length > 0 && (
                        <div style={{ fontSize: 11, color: "#1e40af", marginTop: 4 }}>
                          Suggested inputs: {uc.transform_hint.inputs.join(", ")}
                        </div>
                      )}
                    </div>
                    <div style={{ display: "flex", flexDirection: "column", gap: 6, alignItems: "flex-end" }}>
                      <button
                        onClick={() => expand(uc)}
                        style={{
                          padding: "6px 14px", borderRadius: 6, border: "none",
                          backgroundColor: isOpen ? "#fff" : "#3b82f6",
                          color: isOpen ? "#475569" : "#fff",
                          fontWeight: 600, fontSize: 12, cursor: "pointer",
                          whiteSpace: "nowrap",
                          ...(isOpen ? { border: "1px solid #cbd5e1" } : {}),
                        }}
                      >
                        {isOpen ? "Cancel" : "Create mapping"}
                      </button>
                      {onGuideMe && (
                        <button
                          onClick={() => onGuideMe(buildUnmappedGuidePrefill(uc))}
                          style={{
                            padding: "5px 10px", borderRadius: 6,
                            backgroundColor: "transparent", color: "#7c3aed",
                            border: "1px solid #ddd6fe", fontSize: 11, fontWeight: 600,
                            cursor: "pointer", whiteSpace: "nowrap",
                          }}
                          title="Ask the project assistant to suggest a source column + transform for this column"
                        >
                          ✦ Guide me
                        </button>
                      )}
                      <button
                        onClick={() => setPoRequestColumn(uc)}
                        disabled={poRequestSent.has(uc.column_uri)}
                        style={{
                          padding: "5px 10px", borderRadius: 6,
                          backgroundColor: "transparent",
                          color: poRequestSent.has(uc.column_uri) ? "#94a3b8" : "#854d0e",
                          border: "1px solid",
                          borderColor: poRequestSent.has(uc.column_uri) ? "#cbd5e1" : "#fde68a",
                          fontSize: 11, fontWeight: 600,
                          cursor: poRequestSent.has(uc.column_uri) ? "default" : "pointer",
                          whiteSpace: "nowrap",
                        }}
                        title="Ask the PO to add a source-aligned data product that supplies this column"
                      >
                        {poRequestSent.has(uc.column_uri) ? "✓ Sent to PO" : "Request from PO"}
                      </button>
                    </div>
                  </div>

                  {isOpen && (
                    <div style={{
                      marginTop: 14, paddingTop: 14, borderTop: "1px solid #e2e8f0",
                      display: "flex", flexDirection: "column", gap: 12,
                    }}>
                      {!isLiteralKind && (
                      <div>
                        <div style={{ fontSize: 11, fontWeight: 600, color: "#475569", textTransform: "uppercase", letterSpacing: 0.4, marginBottom: 6 }}>
                          Pick source columns
                        </div>
                        {sources.length > 0 && (
                          <input
                            type="text"
                            value={sourceSearch}
                            onChange={(e) => setSourceSearch(e.target.value)}
                            placeholder="Search source columns (name, type, description, table)…"
                            style={{
                              width: "100%", boxSizing: "border-box", marginBottom: 6,
                              padding: "5px 8px", fontSize: 12, border: "1px solid #cbd5e1", borderRadius: 6,
                            }}
                          />
                        )}
                        <div style={{
                          maxHeight: 180, overflowY: "auto",
                          border: "1px solid #e2e8f0", borderRadius: 6,
                          backgroundColor: "#f8fafc", padding: 6,
                        }}>
                          {sources.length === 0 && (
                            <div style={{ fontSize: 12, color: "#94a3b8", padding: 8 }}>
                              No source columns discovered yet. Run Data Discovery first.
                            </div>
                          )}
                          {(() => {
                            const q = sourceSearch.trim().toLowerCase();
                            const visible = q
                              ? sources.filter((s) => [s.name, s.data_type, s.description, s.table_description]
                                  .some((v) => (v || "").toLowerCase().includes(q)))
                              : sources;
                            if (sources.length > 0 && visible.length === 0) {
                              return <div style={{ fontSize: 12, color: "#94a3b8", padding: 8 }}>No source columns match “{sourceSearch}”.</div>;
                            }
                            return visible.map((s) => {
                            const checked = pickedSet.has(s.uri);
                            return (
                              <label
                                key={s.uri}
                                style={{
                                  display: "flex", alignItems: "flex-start", gap: 8,
                                  padding: "4px 8px", borderRadius: 4, cursor: "pointer",
                                  backgroundColor: checked ? "#eff6ff" : "transparent",
                                }}
                                title={s.table_description || undefined}
                              >
                                <input
                                  type="checkbox"
                                  checked={checked}
                                  onChange={() => togglePick(uc.column_uri, s.uri)}
                                  disabled={submitting}
                                  style={{ marginTop: 3 }}
                                />
                                <div style={{ flex: 1, minWidth: 0 }}>
                                  <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                                    <span style={{ fontSize: 12, fontFamily: "monospace" }}>{s.name}</span>
                                    {s.data_type && (
                                      <span style={{ fontSize: 10, color: "#94a3b8" }}>{s.data_type}</span>
                                    )}
                                    <SensitivityChip sensitivity={s.sensitivity} compact />
                                    <RelationshipKindChip kind={s.relationship_kind} />
                                  </div>
                                  {s.description && (
                                    <div style={{ fontSize: 11, color: "#64748b", marginTop: 2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                                      col: {s.description}
                                    </div>
                                  )}
                                  {s.table_description && (
                                    <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 2, fontStyle: "italic", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                                      table: {s.table_description}
                                    </div>
                                  )}
                                </div>
                              </label>
                            );
                            });
                          })()}
                        </div>
                      </div>
                      )}

                      <TransformEditor
                        value={editorValue}
                        sources={editorSources}
                        author="engineer"
                        confidence={null}
                        onChange={(next) => setEditorByColumn((prev) => ({ ...prev, [uc.column_uri]: next }))}
                        saving={submitting}
                        availableSources={sources}
                      />

                      {errorByColumn[uc.column_uri] && (
                        <div style={{
                          padding: "8px 12px", backgroundColor: "#fee2e2",
                          borderLeft: "3px solid #ef4444", borderRadius: 4,
                          fontSize: 12, color: "#991b1b",
                        }}>
                          {errorByColumn[uc.column_uri]}
                        </div>
                      )}

                      <div style={{ display: "flex", gap: 8 }}>
                        {(() => {
                          const blocked = isLiteralKind
                            ? !((editorValue.transform_params?.literal_value as string) || "").trim()
                            : pickedSet.size === 0;
                          return (
                            <button
                              onClick={() => submit(uc)}
                              disabled={submitting || blocked}
                              style={{
                                padding: "8px 16px", borderRadius: 6, border: "none",
                                backgroundColor: blocked ? "#cbd5e1" : "#22c55e",
                                color: "#fff", fontWeight: 600, fontSize: 13,
                                cursor: blocked || submitting ? "not-allowed" : "pointer",
                              }}
                            >
                              Create mapping
                            </button>
                          );
                        })()}
                        <button
                          onClick={() => expand(uc)}
                          style={{
                            padding: "8px 16px", borderRadius: 6,
                            border: "1px solid #cbd5e1", backgroundColor: "#fff",
                            color: "#64748b", fontWeight: 600, fontSize: 13, cursor: "pointer",
                          }}
                        >
                          Cancel
                        </button>
                        <span style={{ marginLeft: "auto", alignSelf: "center", fontSize: 11, color: "#94a3b8" }}>
                          The mapping starts in pending_review — Reviewer still signs off.
                        </span>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      ))}

      {poRequestColumn && (
        <div
          onClick={() => { setPoRequestColumn(null); setPoRequestReason(""); }}
          style={{
            position: "fixed",
            inset: 0,
            backgroundColor: "rgba(15, 23, 42, 0.55)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 100,
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              width: 520,
              maxWidth: "calc(100vw - 32px)",
              backgroundColor: "#fff",
              borderRadius: 12,
              padding: 20,
              boxShadow: "0 20px 40px rgba(15, 23, 42, 0.25)",
              display: "flex",
              flexDirection: "column",
              gap: 14,
            }}
          >
            <div>
              <div style={{ fontSize: 16, fontWeight: 700, color: "#0f172a" }}>
                Request source candidate from PO
              </div>
              <div style={{ fontSize: 13, color: "#475569", marginTop: 4 }}>
                Column <strong>{poRequestColumn.product_name}.{poRequestColumn.product_col_name}</strong> has
                no source-aligned data product covering it. Ask the PO to identify
                (or create) one before this column can be mapped.
              </div>
            </div>
            <div>
              <label style={{ fontSize: 13, fontWeight: 600, color: "#334155" }}>What's the gap?</label>
              <textarea
                value={poRequestReason}
                onChange={(e) => setPoRequestReason(e.target.value)}
                rows={4}
                style={{
                  width: "100%",
                  marginTop: 6,
                  padding: "8px 10px",
                  border: "1px solid #cbd5e1",
                  borderRadius: 6,
                  fontSize: 13,
                  fontFamily: "inherit",
                  resize: "vertical",
                  boxSizing: "border-box",
                }}
                placeholder="e.g. None of the bound sources expose customer demographics. Need a customer-360 or HR-style source product added to the candidate list."
              />
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
              <button
                onClick={() => { setPoRequestColumn(null); setPoRequestReason(""); }}
                style={{
                  padding: "8px 16px", borderRadius: 6,
                  backgroundColor: "#fff", color: "#64748b",
                  border: "1px solid #cbd5e1", fontSize: 13, fontWeight: 600,
                  cursor: "pointer",
                }}
              >
                Cancel
              </button>
              <button
                onClick={sendPoRequest}
                disabled={poRequestSubmitting || !poRequestReason.trim()}
                style={{
                  padding: "8px 16px", borderRadius: 6,
                  backgroundColor: poRequestReason.trim() ? "#0f172a" : "#cbd5e1",
                  color: "#fff", border: "none",
                  fontSize: 13, fontWeight: 600,
                  cursor: poRequestReason.trim() ? "pointer" : "default",
                }}
              >
                {poRequestSubmitting ? "Sending…" : "Send request"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
