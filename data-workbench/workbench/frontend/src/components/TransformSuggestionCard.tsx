import { useState } from "react";
import api from "../api/client";
import { type SourceColumn, type TransformKind, TRANSFORM_KIND_LABELS } from "../types";

/** The structured payload returned by POST /api/transform-intent/interpret.
 *  Mirrors the backend TransformInterpretResponse — the existing 16-kind DSL,
 *  with inputs already resolved to source-column URIs. */
export interface TransformInterpretResult {
  readback: string;
  transform_kind: string;
  transform_inputs: string[]; // source-column URIs
  transform_params: Record<string, unknown>;
  transform_decorators: { standardization?: string[]; default_if_null?: string };
  transform_expression: string;
  confidence: number;
  warnings: string[];
  grounded_columns: string[];
}

interface Props {
  /** The source columns available to this mapping (backs grounding + URI resolution). */
  sources: SourceColumn[];
  /** The product column being derived — context for the interpreter. */
  targetColumn: string;
  mappingUri: string;
  disabled?: boolean;
  /** Apply the suggestion: parent maps it onto the TransformEditor value + source picks. */
  onApply: (r: TransformInterpretResult) => void;
}

const ACCENT = "#7c3aed"; // violet — signals AI assist, matching the product shell

/** Inline natural-language → structured-transform assist for the engineer mapping
 *  surface. The engineer describes a column's derivation in plain words; this calls
 *  the transform-intent interpreter and renders a one-click Apply card that pre-fills
 *  the transform editor (the engineer verifies against the live SQL preview before
 *  Save Replacement commits it). No new write path — Apply only mutates the editor. */
export default function TransformSuggestionCard({
  sources, targetColumn, mappingUri, disabled, onApply,
}: Props) {
  const [intent, setIntent] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<TransformInterpretResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [applied, setApplied] = useState(false);

  const lookupTables = Array.from(
    new Set(
      sources
        .map((s) => (s.table_schema && s.table_name ? `${s.table_schema}.${s.table_name}` : ""))
        .filter(Boolean),
    ),
  );

  const suggest = async () => {
    if (!intent.trim() || loading) return;
    setLoading(true);
    setError(null);
    setResult(null);
    setApplied(false);
    try {
      const resp = await api.post(`/api/transform-intent/interpret`, {
        intent: intent.trim(),
        target_column: targetColumn,
        source_columns: sources.map((s) => ({
          name: s.column_name || s.name,
          uri: s.uri,
          type: s.data_type || "",
        })),
        lookup_tables: lookupTables,
        mapping_uri: mappingUri,
      });
      setResult(resp.data as TransformInterpretResult);
    } catch {
      setError("Couldn't get a suggestion. Rephrase, or configure the transform manually below.");
    }
    setLoading(false);
  };

  const apply = () => {
    if (!result || !result.transform_kind) return;
    onApply(result);
    setApplied(true);
  };

  const kindLabel = result?.transform_kind
    ? TRANSFORM_KIND_LABELS[result.transform_kind as TransformKind] || result.transform_kind
    : "";
  const conf = result?.confidence ?? 0;
  const confColor = conf >= 75 ? "#16a34a" : conf >= 50 ? "#d97706" : "#dc2626";

  return (
    <div
      style={{
        border: `1px solid ${ACCENT}33`,
        backgroundColor: "#faf5ff",
        borderRadius: 8,
        padding: 12,
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      <div style={{ fontSize: 13, fontWeight: 600, color: ACCENT }}>
        ✦ Describe this column&rsquo;s derivation
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <input
          type="text"
          value={intent}
          disabled={disabled || loading}
          placeholder='e.g. "combine first and last name with a space" or "mask all but the last 4 of ssn"'
          onChange={(e) => setIntent(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") suggest();
          }}
          style={{
            flex: 1,
            padding: "7px 10px",
            fontSize: 13,
            border: "1px solid #e2e8f0",
            borderRadius: 6,
          }}
        />
        <button
          onClick={suggest}
          disabled={disabled || loading || !intent.trim()}
          style={{
            padding: "7px 14px",
            fontSize: 13,
            fontWeight: 600,
            color: "#fff",
            backgroundColor: loading || !intent.trim() ? "#c4b5fd" : ACCENT,
            border: "none",
            borderRadius: 6,
            cursor: loading || !intent.trim() ? "default" : "pointer",
            whiteSpace: "nowrap",
          }}
        >
          {loading ? "Thinking…" : "Suggest"}
        </button>
      </div>

      {error && <div style={{ fontSize: 12, color: "#dc2626" }}>{error}</div>}

      {result && (
        <div
          style={{
            border: "1px solid #e2e8f0",
            backgroundColor: "#fff",
            borderRadius: 6,
            padding: 10,
            display: "flex",
            flexDirection: "column",
            gap: 6,
          }}
        >
          <div style={{ fontSize: 13, color: "#0f172a" }}>{result.readback}</div>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            {result.transform_kind ? (
              <span
                style={{
                  fontSize: 11,
                  fontWeight: 600,
                  fontFamily: "monospace",
                  padding: "2px 8px",
                  borderRadius: 999,
                  backgroundColor: "#ede9fe",
                  color: ACCENT,
                }}
              >
                {kindLabel}
              </span>
            ) : (
              <span style={{ fontSize: 12, color: "#94a3b8" }}>
                No transform kind matched — refine the description or configure it below.
              </span>
            )}
            <span style={{ fontSize: 11, color: confColor }}>{conf}% confidence</span>
          </div>

          {result.warnings.length > 0 && (
            <ul style={{ margin: 0, paddingLeft: 18, fontSize: 11, color: "#b45309" }}>
              {result.warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          )}

          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <button
              onClick={apply}
              disabled={disabled || !result.transform_kind || applied}
              style={{
                padding: "6px 14px",
                fontSize: 13,
                fontWeight: 600,
                color: "#fff",
                backgroundColor: !result.transform_kind || applied ? "#a7f3d0" : "#16a34a",
                border: "none",
                borderRadius: 6,
                cursor: !result.transform_kind || applied ? "default" : "pointer",
              }}
            >
              {applied ? "✓ Applied — review below, then Save Replacement" : "Apply to editor"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
