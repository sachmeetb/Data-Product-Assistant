import { useState } from "react";
import api from "../../../api/client";
import type { GapAnalysisResponse, GapAnalysisColumnResult } from "../../../types";

/**
 * Step 8 "Pre-flight gap check". Manual fire — the PO clicks the button when
 * they're ready. Mirrors the OSI / Question Analysis pattern: collapsed
 * by default, on-demand fire, results rendered inline with per-column
 * status chips. The host wizard caches the result so a soft-confirm dialog
 * can warn before submit when gaps remain.
 */
export interface GapAnalysisSectionProps {
  projectId: number | null;
  /** Consumer columns from the wizard's current state. */
  consumerColumns: Array<{ name: string; logical_type?: string; description?: string }>;
  /** Matched candidate source contract_ids — empty when no sources picked. */
  candidateContractIds: string[];
  /** Optional context to give the LLM analyzer richer signal. */
  consumerIdea?: string;
  consumerDescription?: string;
  consumerDomain?: string;
  /** Called when the analysis completes. The host caches the result for the
   *  submit-soft-confirm gate. Pass `null` to clear the cache. */
  onResult: (result: GapAnalysisResponse | null) => void;
  /** Optional cached result to render (e.g. on step re-entry). */
  cachedResult: GapAnalysisResponse | null;
}

export default function GapAnalysisSection({
  projectId,
  consumerColumns,
  candidateContractIds,
  consumerIdea,
  consumerDescription,
  consumerDomain,
  onResult,
  cachedResult,
}: GapAnalysisSectionProps) {
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Section is collapsed by default until the PO clicks; results stay
  // expanded once produced so the PO can scroll through them.
  const [expanded, setExpanded] = useState(false);

  const runGapCheck = async () => {
    if (running || projectId === null) return;
    setRunning(true);
    setError(null);
    setExpanded(true);
    try {
      const res = await api.post<GapAnalysisResponse>(
        `/api/ingest-products/projects/${projectId}/gap-analysis`,
        {
          consumer_idea: consumerIdea || "",
          consumer_description: consumerDescription || "",
          consumer_domain: consumerDomain || "",
          consumer_columns: consumerColumns,
          candidate_contract_ids: candidateContractIds,
        },
      );
      onResult(res.data);
    } catch (e: unknown) {
      const msg =
        e && typeof e === "object" && "response" in e
          ? (e as { response?: { data?: { detail?: string } } }).response?.data?.detail ?? String(e)
          : String(e);
      setError(typeof msg === "string" ? msg : JSON.stringify(msg));
    } finally {
      setRunning(false);
    }
  };

  // Always render the wrapper; collapsed/expanded state controls the body.
  return (
    <section
      style={{
        padding: 16,
        borderRadius: 10,
        backgroundColor: "#fff",
        border: "1px solid #e2e8f0",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          gap: 12,
        }}
      >
        <div>
          <div style={{ fontSize: 14, fontWeight: 700, color: "#0f172a" }}>
            Pre-flight gap check (optional)
          </div>
          <div style={{ fontSize: 12, color: "#64748b", marginTop: 4, lineHeight: 1.5 }}>
            For each consumer schema column, check whether any candidate upstream
            product plausibly covers it. Baseline analysis — surfaces obvious gaps
            so you can address them before submitting.
          </div>
        </div>
        <button
          type="button"
          onClick={runGapCheck}
          disabled={running || projectId === null}
          style={{
            padding: "6px 14px",
            borderRadius: 6,
            border: "none",
            backgroundColor: running || projectId === null ? "#cbd5e1" : "#0f172a",
            color: "#fff",
            fontSize: 12,
            fontWeight: 600,
            cursor: running || projectId === null ? "default" : "pointer",
            whiteSpace: "nowrap",
          }}
          title={projectId === null ? "Save a draft from earlier steps to enable" : ""}
        >
          {running ? "Analyzing…" : cachedResult ? "Re-run gap check" : "Run gap check"}
        </button>
      </div>

      {running && (
        <div
          style={{
            marginTop: 12,
            padding: "10px 12px",
            borderRadius: 6,
            backgroundColor: "#f8fafc",
            border: "1px solid #cbd5e1",
            fontSize: 12,
            color: "#475569",
          }}
        >
          Comparing {consumerColumns.length} consumer column{consumerColumns.length === 1 ? "" : "s"}{" "}
          to the {candidateContractIds.length} candidate upstream product{candidateContractIds.length === 1 ? "" : "s"}…
          Usually takes 10–30 seconds.
        </div>
      )}

      {error && (
        <div
          style={{
            marginTop: 12,
            padding: 10,
            borderRadius: 6,
            backgroundColor: "#fef2f2",
            color: "#dc2626",
            border: "1px solid #fecaca",
            fontSize: 12,
          }}
        >
          {error}
        </div>
      )}

      {cachedResult && expanded && !running && (
        <GapAnalysisResults result={cachedResult} />
      )}
    </section>
  );
}

function GapAnalysisResults({ result }: { result: GapAnalysisResponse }) {
  const grouped: Record<string, GapAnalysisColumnResult[]> = {
    gap: [],
    ambiguous: [],
    derivable: [],
    covered: [],
  };
  for (const r of result.gaps) {
    if (r.status in grouped) grouped[r.status].push(r);
  }

  return (
    <div style={{ marginTop: 14, display: "flex", flexDirection: "column", gap: 10 }}>
      <div
        style={{
          padding: 10,
          borderRadius: 6,
          backgroundColor: result._no_sources ? "#fefce8" : "#f1f5f9",
          border: result._no_sources ? "1px solid #fde68a" : "1px solid #e2e8f0",
          fontSize: 13,
          color: "#0f172a",
        }}
      >
        <strong>Summary:</strong> {result.summary}
        {result._fallback && (
          <span style={{ marginLeft: 8, fontSize: 11, color: "#94a3b8", fontStyle: "italic" }}>
            (heuristic fallback — install the data-product-gap-analyzer skill for
            richer semantic analysis)
          </span>
        )}
      </div>

      {(["gap", "ambiguous", "derivable", "covered"] as const).map((status) => {
        const rows = grouped[status];
        if (rows.length === 0) return null;
        return (
          <div key={status} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <div
              style={{
                fontSize: 11,
                fontWeight: 700,
                textTransform: "uppercase",
                letterSpacing: 0.4,
                color: STATUS_LABEL_COLOR[status],
              }}
            >
              {STATUS_LABEL[status]} ({rows.length})
            </div>
            {rows.map((r) => (
              <GapRow key={r.column_name} row={r} />
            ))}
          </div>
        );
      })}
    </div>
  );
}

function GapRow({ row }: { row: GapAnalysisColumnResult }) {
  const palette = STATUS_PALETTE[row.status];
  return (
    <div
      style={{
        padding: 10,
        borderRadius: 6,
        backgroundColor: palette.bg,
        border: `1px solid ${palette.border}`,
        display: "flex",
        flexDirection: "column",
        gap: 4,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 8 }}>
        <div style={{ fontWeight: 700, color: palette.fg, fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace", fontSize: 13 }}>
          {row.column_name}
        </div>
        <span style={{ fontSize: 11, color: palette.fg, opacity: 0.85 }}>
          {STATUS_LABEL[row.status]} · {row.confidence}%
        </span>
      </div>
      <div style={{ fontSize: 12, color: palette.fg, opacity: 0.95 }}>{row.rationale}</div>
      {row.source_evidence.length > 0 && (
        <div style={{ fontSize: 11, color: palette.fg, opacity: 0.8 }}>
          Source evidence: {row.source_evidence.join(", ")}
        </div>
      )}
    </div>
  );
}

const STATUS_LABEL: Record<GapAnalysisColumnResult["status"], string> = {
  covered: "Covered",
  derivable: "Derivable",
  ambiguous: "Ambiguous",
  gap: "Gap",
};

const STATUS_LABEL_COLOR: Record<GapAnalysisColumnResult["status"], string> = {
  covered: "#065f46",
  derivable: "#1e3a8a",
  ambiguous: "#854d0e",
  gap: "#991b1b",
};

const STATUS_PALETTE: Record<
  GapAnalysisColumnResult["status"],
  { bg: string; border: string; fg: string }
> = {
  covered: { bg: "#ecfdf5", border: "#86efac", fg: "#065f46" },
  derivable: { bg: "#eef2ff", border: "#c7d2fe", fg: "#1e3a8a" },
  ambiguous: { bg: "#fefce8", border: "#fde68a", fg: "#854d0e" },
  gap: { bg: "#fef2f2", border: "#fecaca", fg: "#991b1b" },
};
