import { useEffect, useState, useMemo } from "react";
import api from "../api/client";
import RelationshipKindChip from "./RelationshipKindChip";

// Preview surface for a deployed virtual view. Used by:
//   - MarketplacePage "Preview" tab (POST /api/marketplace/products/{cid}/preview)
//   - ProjectDashboard serving detail card (POST /api/projects/{pid}/serving/preview)
//
// Body shape matches both endpoints: {dataset_uri?, limit}. Response:
//   {status: "ok", columns:[{name, dataType}], rows:[[]], truncated, row_count, duration_ms, view_name, view_schema}
//   {error: "not_deployed", deployment_status}
//   {status: "failed", error_class, error_message, duration_ms}

export interface PreviewDataset {
  uri?: string;
  physicalName?: string;
  name?: string;
  // Surfaces in the per-dataset header above the row table. Both are
  // optional — datasets without an approved :TableDescription render the
  // header sans description / chip.
  description?: string | null;
  relationshipKind?: string | null;
}

interface Props {
  endpoint: string;                  // e.g. "/api/marketplace/products/foo-contract/preview"
  datasets: PreviewDataset[];        // for the switcher; pass [] if single-dataset
  limit?: number;
}

interface PreviewColumn {
  name: string;
  dataType: string;
  // Set by the preview endpoints when the column has an approved
  // :DProdColumn.description. Rendered as a `title` tooltip on the
  // column header and surfaced inline below the name.
  description?: string | null;
  // False when the physical column isn't part of the product's published
  // schema — a load-framework "system column" (e.g. dlt's _dlt_id /
  // _dlt_load_id). Hidden by default behind the "Show system columns"
  // toggle. Missing (older responses / engineer endpoint) = treated as
  // visible.
  in_schema?: boolean;
}

interface ServerDataset {
  dataset_uri?: string | null;
  physicalName?: string | null;
  view_name?: string | null;
  description?: string | null;
  relationshipKind?: string | null;
}

interface ActiveDataset {
  uri?: string | null;
  physicalName?: string | null;
  description?: string | null;
  relationshipKind?: string | null;
}

interface PreviewSuccess {
  status: "ok";
  columns: PreviewColumn[];
  rows: unknown[][];
  truncated: boolean;
  row_count: number;
  duration_ms: number;
  view_name: string;
  view_schema: string;
  // Engineer endpoint embeds this so the dropdown can populate without a
  // separate round-trip. Marketplace mode passes datasets via props.
  available_datasets?: ServerDataset[];
  // Picked-dataset metadata for the table-description header. Populated
  // by both preview endpoints when the resolved view maps to a known
  // :DProdOutputDataset.
  active_dataset?: ActiveDataset;
}

interface PreviewFailure {
  status: "failed";
  error_class: string;
  error_message: string;
  duration_ms: number;
  view_name?: string;
  view_schema?: string;
  available_datasets?: ServerDataset[];
  active_dataset?: ActiveDataset;
}

interface NotDeployed {
  error: "not_deployed";
  deployment_status: string;
}

type PreviewResponse = PreviewSuccess | PreviewFailure | NotDeployed;

const ERROR_HUMAN: Record<string, string> = {
  not_select: "Internal: query gating rejected the SELECT — please report.",
  timeout: "Query timed out (30s). The view may be expensive or under-indexed.",
  permission_denied: "The project's connection user lacks SELECT permission on the deployed view.",
  connection_error: "Could not connect to the project's database.",
  sql_error: "The database returned a SQL error executing the preview.",
  relation_not_found:
    "The referenced table or view doesn't exist on the target (or the connection user isn't authorized). " +
    "It may not be deployed yet, or the physical object's identifier case differs from what was queried.",
};

export default function PreviewTab({ endpoint, datasets, limit = 50 }: Props) {
  const propDatasets = useMemo(
    () => datasets.filter(d => d.physicalName || d.uri),
    [datasets]
  );
  // Server-side dataset list (engineer endpoint embeds this so the dropdown
  // can populate without an extra round-trip). Stays in sync with the
  // result.available_datasets from the most recent successful response.
  const [serverDatasets, setServerDatasets] = useState<PreviewDataset[]>([]);
  const dropdownDatasets = propDatasets.length > 0 ? propDatasets : serverDatasets;

  const [selectedUri, setSelectedUri] = useState<string | undefined>(
    propDatasets[0]?.uri
  );
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<PreviewResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Reveal load-framework system columns (hidden by default). Reset whenever
  // a fresh preview loads so a new dataset starts collapsed.
  const [showSystem, setShowSystem] = useState(false);

  const okResult = result && "status" in result && result.status === "ok" ? result : null;
  // Indices into result.columns (and each row's positional cell array) that
  // are currently visible: every schema column always, system columns only
  // when revealed. Rows are positional arrays, so the same index set filters
  // both the header and each row's cells.
  const keptIndices = useMemo(() => {
    if (!okResult) return [] as number[];
    return okResult.columns
      .map((_, i) => i)
      .filter(i => okResult.columns[i].in_schema !== false || showSystem);
  }, [okResult, showSystem]);
  const hiddenSystemCount = okResult
    ? okResult.columns.filter(c => c.in_schema === false).length
    : 0;

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    setError(null);
    setShowSystem(false);
    const body: Record<string, unknown> = { limit };
    if (selectedUri) body.dataset_uri = selectedUri;
    api.post(endpoint, body)
      .then(res => {
        if (cancelled) return;
        const data = res.data as PreviewResponse;
        setResult(data);
        // Cache the server-side dataset list so the dropdown renders even
        // when the parent passed an empty datasets[] (engineer mode).
        if (data && "available_datasets" in data && Array.isArray(data.available_datasets)) {
          setServerDatasets(
            data.available_datasets
              .filter(d => d.dataset_uri || d.physicalName)
              .map(d => ({
                uri: d.dataset_uri || undefined,
                physicalName: d.physicalName || undefined,
                name: d.physicalName || undefined,
                description: d.description ?? null,
                relationshipKind: d.relationshipKind ?? null,
              }))
          );
        }
      })
      .catch(e => {
        if (cancelled) return;
        const detail = e?.response?.data?.detail || e?.message || "Preview failed";
        setError(String(detail));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [endpoint, selectedUri, limit]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {dropdownDatasets.length > 1 && (
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <label htmlFor="preview-dataset" style={{ fontSize: 12, fontWeight: 600, color: "#475569" }}>
            Output dataset
          </label>
          <select
            id="preview-dataset"
            value={selectedUri || ""}
            onChange={e => setSelectedUri(e.target.value || undefined)}
            style={{
              fontSize: 13, padding: "4px 8px",
              border: "1px solid #cbd5e1", borderRadius: 6,
              backgroundColor: "#fff", color: "#0f172a",
            }}
          >
            {!selectedUri && <option value="">— pick a dataset —</option>}
            {dropdownDatasets.map(d => (
              <option key={d.uri || d.physicalName} value={d.uri || ""}>
                {d.physicalName || d.name || d.uri}
              </option>
            ))}
          </select>
        </div>
      )}

      {loading && (
        <div style={{ padding: "12px 0", color: "#64748b", fontSize: 13 }}>Loading preview…</div>
      )}

      {error && !loading && (
        <div style={{
          padding: 12, borderRadius: 6,
          backgroundColor: "#fef2f2", color: "#991b1b",
          border: "1px solid #fecaca", fontSize: 13,
        }}>
          {error}
        </div>
      )}

      {!loading && !error && result && "error" in result && result.error === "not_deployed" && (
        <div style={{
          padding: 16, borderRadius: 6,
          backgroundColor: "#fffbeb", color: "#92400e",
          border: "1px solid #fed7aa", fontSize: 13,
        }}>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>This product has not been deployed yet.</div>
          <div>Run the <strong>Deploy Virtual View</strong> stage in the engineering pipeline to enable preview.</div>
        </div>
      )}

      {!loading && !error && result && "status" in result && result.status === "failed" && (
        <div style={{
          padding: 16, borderRadius: 6,
          backgroundColor: "#fef2f2", color: "#991b1b",
          border: "1px solid #fecaca", fontSize: 13,
        }}>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>
            Preview failed: {result.error_class}
          </div>
          {ERROR_HUMAN[result.error_class] && (
            <div style={{ fontSize: 12 }}>{ERROR_HUMAN[result.error_class]}</div>
          )}
          {result.error_message && (
            <div style={{
              fontSize: 11, marginTop: 6,
              fontFamily: "'Fira Code', monospace",
              color: "#7f1d1d", whiteSpace: "pre-wrap", wordBreak: "break-word",
            }}>
              {result.error_message}
            </div>
          )}
        </div>
      )}

      {!loading && !error && result && "status" in result && result.status === "ok" && (
        <>
          {(() => {
            // Picked dataset prefers the server's active_dataset (knows what
            // was actually resolved when no dataset_uri was sent), falls back
            // to the dropdown's selected entry. Marketplace mode passes
            // description / relationshipKind via props.datasets so we look up
            // there as a third source when the server didn't echo them.
            const active = result.active_dataset || null;
            const propMatch = dropdownDatasets.find(
              d => (active?.uri && d.uri === active.uri)
                || (active?.physicalName && d.physicalName === active.physicalName)
            );
            const description = active?.description || propMatch?.description || null;
            const relationshipKind = active?.relationshipKind || propMatch?.relationshipKind || null;
            if (!description && !relationshipKind) return null;
            return (
              <div style={{
                padding: "10px 12px", borderRadius: 6,
                backgroundColor: "#f8fafc", border: "1px solid #e2e8f0",
                display: "flex", flexDirection: "column", gap: 6,
              }}>
                {relationshipKind && (
                  <div>
                    <RelationshipKindChip kind={relationshipKind} long />
                  </div>
                )}
                {description && (
                  <div style={{ fontSize: 12, color: "#334155", lineHeight: 1.5 }}>
                    {description}
                  </div>
                )}
              </div>
            );
          })()}

          <div style={{
            display: "flex", justifyContent: "space-between", alignItems: "center",
            fontSize: 12, color: "#64748b",
          }}>
            <span style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <span>
                {result.view_schema}.<strong style={{ color: "#0f172a" }}>{result.view_name}</strong>
                {" · "}
                {result.row_count} row{result.row_count === 1 ? "" : "s"}
                {result.truncated && " (showing first batch)"}
                {" · "}
                {result.duration_ms} ms
              </span>
              {hiddenSystemCount > 0 && (
                <label
                  style={{ display: "flex", alignItems: "center", gap: 4, cursor: "pointer" }}
                  title="Columns added by the load pipeline (e.g. load identifiers); not part of the product's published schema."
                >
                  <input
                    type="checkbox"
                    checked={showSystem}
                    onChange={e => setShowSystem(e.target.checked)}
                    style={{ cursor: "pointer" }}
                  />
                  Show system columns ({hiddenSystemCount})
                </label>
              )}
            </span>
            {result.truncated && (
              <span style={{
                fontSize: 11, padding: "2px 6px", borderRadius: 4,
                backgroundColor: "#fef3c7", color: "#92400e", fontWeight: 600,
              }}>
                Truncated to {limit}
              </span>
            )}
          </div>

          {result.rows.length === 0 ? (
            <div style={{ padding: 16, color: "#94a3b8", fontSize: 13, fontStyle: "italic" }}>
              The deployed view returned 0 rows.
            </div>
          ) : (
            <div style={{
              overflow: "auto", maxHeight: 480,
              border: "1px solid #e2e8f0", borderRadius: 6,
            }}>
              <table style={{
                width: "100%", borderCollapse: "collapse",
                fontSize: 12, fontFamily: "'Fira Code', monospace",
              }}>
                <thead>
                  <tr>
                    {keptIndices.map(i => result.columns[i]).map((c, i) => (
                      <th
                        key={i}
                        title={c.description || c.name}
                        style={{
                          position: "sticky", top: 0,
                          backgroundColor: "#f8fafc", borderBottom: "1px solid #e2e8f0",
                          padding: "8px 10px", textAlign: "left",
                          fontWeight: 600, color: "#0f172a", whiteSpace: "nowrap",
                          cursor: c.description ? "help" : "default",
                        }}
                      >
                        <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                          {c.name}
                          {c.description && (
                            <span
                              aria-hidden
                              style={{
                                fontSize: 9, color: "#64748b",
                                fontWeight: 400, fontFamily: "system-ui, sans-serif",
                              }}
                            >
                              ⓘ
                            </span>
                          )}
                        </div>
                        <div style={{
                          fontSize: 10, fontWeight: 400, color: "#94a3b8",
                          fontFamily: "system-ui, sans-serif",
                        }}>{c.dataType}</div>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {result.rows.map((r, i) => (
                    <tr key={i} style={{
                      backgroundColor: i % 2 === 0 ? "#ffffff" : "#f8fafc",
                    }}>
                      {keptIndices.map(idx => r[idx]).map((v, j) => (
                        <td key={j} style={{
                          padding: "6px 10px",
                          borderBottom: "1px solid #f1f5f9",
                          color: v === null ? "#94a3b8" : "#0f172a",
                          fontStyle: v === null ? "italic" : "normal",
                          whiteSpace: "nowrap", maxWidth: 320,
                          overflow: "hidden", textOverflow: "ellipsis",
                        }} title={v === null ? "null" : String(v)}>
                          {v === null ? "null" : String(v)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
