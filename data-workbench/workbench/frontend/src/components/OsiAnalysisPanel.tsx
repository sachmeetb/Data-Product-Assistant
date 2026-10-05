// Full readiness analysis: band header + completeness + per-criterion
// checklist + LLM narrative. Used in the wizard's Step 6 (with onScoreNow)
// and inside the My Products "View Analysis" modal (read-only). The
// marketplace consumer surfaces deliberately do NOT use this component —
// they see only OsiBadge.
//
// Renders against whichever rubric the contract has selected (OSI by
// default; AI-Ready is the alternative shipped today). Rubric metadata —
// label, description, empty-state copy, band headlines, per-criterion
// hints — comes from the /api/projects/{id}/osi/evaluation payload, which
// embeds the rubric config so this component doesn't need to know its
// shape ahead of time.

import { useEffect, useState, type CSSProperties } from "react";
import api from "../api/client";
import MarkdownMessage from "./chat/MarkdownMessage";

export type OsiBand = "red" | "amber" | "green";
export type ChecklistStatus = "pass" | "partial" | "fail" | "na";

export interface OsiChecklistRow {
  criterion: string;
  status: ChecklistStatus;
  weight: number;
  reason: string;
}

export interface OsiEvaluationPayload {
  band: OsiBand;
  completeness: number;
  conformance_pass: boolean;
  errors: Array<{ kind: string; path: string; message: string }>;
  checklist: OsiChecklistRow[];
  narrative: string | null;
  triggered_by?: string;
  evaluator_version?: string;
  evaluated_at?: string | null;
  batch_id?: string;
  rubric?: string;
  rubric_label?: string;
}

interface RubricHint {
  title: string;
  detail?: string;
}

// Embedded rubric metadata served alongside the evaluation. The backend's
// /api/projects/{id}/osi/evaluation endpoint surfaces these so the panel
// has all the copy it needs without a second roundtrip.
interface RubricMetadata {
  rubric?: string;
  rubric_label?: string;
  rubric_short_label?: string;
  rubric_description?: string;
  rubric_empty_state_note?: string;
  rubric_band_headlines?: Partial<Record<OsiBand, string>>;
  // Per-criterion hint dictionary keyed by criterion label. Populated from
  // /api/scoring-rubrics/{id} on first render so checklist rows get the
  // right "How to fix" copy.
  hint_by_criterion?: Record<string, Partial<Record<ChecklistStatus, RubricHint>>>;
  rubric_changed_since_eval?: boolean;
}

interface Props {
  projectId: number;
  // When provided, skip the initial fetch and render this payload instead.
  // Used when the wizard already has a freshly-computed eval to display.
  initial?: OsiEvaluationPayload | null;
  // Wizard mode — shows a Score now button + advisor-error banner.
  scoreOnDemand?: boolean;
  // Triggered when Score now succeeds; lets the parent refresh adjacent
  // state (e.g. wizard suggestion chips).
  onScored?: (payload: OsiEvaluationPayload) => void;
}

const DEFAULT_BAND_HEADLINES: Record<OsiBand, string> = {
  red: "Significant gaps before this product is ready",
  amber: "Workable but several fields still missing",
  green: "Ready for downstream consumers",
};

const BAND_PALETTE: Record<OsiBand, { bg: string; fg: string; dot: string }> = {
  red: { bg: "#fef2f2", fg: "#991b1b", dot: "#dc2626" },
  amber: { bg: "#fffbeb", fg: "#92400e", dot: "#d97706" },
  green: { bg: "#f0fdf4", fg: "#065f46", dot: "#16a34a" },
};

const STATUS_ICON: Record<ChecklistStatus, string> = {
  pass: "✓",
  partial: "⚠",
  fail: "✗",
  na: "—",
};

const STATUS_COLOR: Record<ChecklistStatus, string> = {
  pass: "#16a34a",
  partial: "#d97706",
  fail: "#dc2626",
  na: "#94a3b8",
};

export default function OsiAnalysisPanel({ projectId, initial = null, scoreOnDemand = false, onScored }: Props) {
  const [payload, setPayload] = useState<OsiEvaluationPayload | null>(initial);
  const [rubricMeta, setRubricMeta] = useState<RubricMetadata>({});
  const [loading, setLoading] = useState(initial === null);
  const [scoring, setScoring] = useState(false);
  const [advisorError, setAdvisorError] = useState<string | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  // Tracks which checklist rows are expanded to show their "how to improve"
  // hint. Keyed by `${criterion}-${index}` to match the existing row key.
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  // Helper: extract per-criterion hint dictionary from the rubric catalog
  // (criteria[].label → {pass/partial/fail/na: {title, detail}}). Keyed by
  // the human-readable label since that's what shows up in the eval
  // checklist rows.
  const buildHintMap = (criteria: Array<{ label?: string; hint?: Record<string, RubricHint> }>): RubricMetadata["hint_by_criterion"] => {
    const out: NonNullable<RubricMetadata["hint_by_criterion"]> = {};
    for (const c of criteria) {
      if (c.label && c.hint) {
        out[c.label] = c.hint;
      }
    }
    return out;
  };

  useEffect(() => {
    // Initial state already reflects the initial-vs-fetch path
    // (`useState(initial === null)`). Avoid re-setting loading inside the
    // effect — the project's lint rule (react-hooks/set-state-in-effect)
    // flags it. The .finally() below handles the false transition.
    if (initial !== null) return;
    api
      .get(`/api/projects/${projectId}/osi/evaluation`)
      .then(async (r) => {
        const ev = r.data?.evaluation as OsiEvaluationPayload | null;
        setPayload(ev || null);
        // Surface rubric metadata from the eval response. Fetch the rubric
        // catalog for hint dictionaries (the eval endpoint emits the
        // metadata block but not the per-criterion hints, to keep its
        // response small).
        const rubricId: string = (ev?.rubric || r.data?.rubric || "osi") as string;
        const meta: RubricMetadata = {
          rubric: rubricId,
          rubric_label: ev?.rubric_label || r.data?.rubric_label,
          rubric_short_label: r.data?.rubric_short_label,
          rubric_description: r.data?.rubric_description,
          rubric_empty_state_note: r.data?.rubric_empty_state_note,
          rubric_band_headlines: r.data?.rubric_band_headlines || {},
          rubric_changed_since_eval: !!r.data?.rubric_changed_since_eval,
        };
        try {
          const rubricRes = await api.get(`/api/scoring-rubrics/${rubricId}`);
          meta.hint_by_criterion = buildHintMap(rubricRes.data?.criteria || []);
        } catch {
          meta.hint_by_criterion = {};
        }
        setRubricMeta(meta);
        setFetchError(null);
      })
      .catch((e) => setFetchError(String(e)))
      .finally(() => setLoading(false));
  }, [projectId, initial]);

  const scoreNow = async () => {
    if (scoring) return;
    setScoring(true);
    setAdvisorError(null);
    try {
      const r = await api.post(`/api/projects/${projectId}/osi/evaluate`, {
        trigger: "manual",
      });
      const fresh: OsiEvaluationPayload = {
        band: r.data.band,
        completeness: r.data.completeness,
        conformance_pass: r.data.conformance_pass,
        errors: r.data.errors || [],
        checklist: r.data.checklist || [],
        narrative: r.data.narrative,
        triggered_by: r.data.trigger,
        batch_id: r.data.batch_id,
        rubric: r.data.rubric,
        rubric_label: r.data.rubric_label,
      };
      setPayload(fresh);
      // Refresh rubric metadata too — Score now may have shifted to a
      // different rubric if the contract was edited since the last fetch.
      const rubricId: string = (r.data.rubric || "osi") as string;
      try {
        const rubricRes = await api.get(`/api/scoring-rubrics/${rubricId}`);
        setRubricMeta((prev) => ({
          ...prev,
          rubric: rubricId,
          rubric_label: r.data.rubric_label || prev.rubric_label,
          rubric_short_label: r.data.rubric_short_label || prev.rubric_short_label,
          hint_by_criterion: buildHintMap(rubricRes.data?.criteria || []),
        }));
      } catch {
        // non-fatal — checklist falls back to the backend's reason text.
      }
      if (r.data.advisor_error) {
        setAdvisorError(String(r.data.advisor_error));
      }
      onScored?.(fresh);
    } catch (e) {
      const msg = (e as { response?: { data?: { detail?: string } }; message?: string }).response?.data?.detail
        || (e as Error).message
        || "Score failed";
      setAdvisorError(`Failed to compute score: ${msg}`);
    }
    setScoring(false);
  };

  // Resolve the rubric's "How to fix" hint for a given checklist row.
  // Looks up the hint dictionary loaded from /api/scoring-rubrics/{id}; if
  // the rubric metadata hasn't loaded yet OR the criterion isn't in the
  // catalog, returns null so the row stays non-expandable.
  const getHint = (criterion: string, status: ChecklistStatus): RubricHint | null => {
    return rubricMeta.hint_by_criterion?.[criterion]?.[status] ?? null;
  };

  const rubricLabel = rubricMeta.rubric_label || payload?.rubric_label || "OSI Readiness";

  if (loading) {
    return <div style={{ color: "#64748b", padding: 16 }}>Loading readiness analysis…</div>;
  }
  if (fetchError) {
    return <div style={{ color: "#dc2626", padding: 16 }}>Could not load readiness analysis: {fetchError}</div>;
  }

  if (!payload) {
    // Empty state — copy comes from the rubric YAML so OSI and AI-Ready
    // each get rubric-appropriate framing without duplicating component
    // code. Falls back to OSI-flavoured defaults if the metadata didn't
    // load.
    const description =
      rubricMeta.rubric_description ||
      "Scores how self-describing your product's contract is — descriptions, primary keys, expressions, relationships, metrics, and AI context.";
    const emptyNote = rubricMeta.rubric_empty_state_note;
    return (
      <div style={emptyStateStyle}>
        <div style={{ fontWeight: 600, color: "#334155", marginBottom: 6 }}>
          No {rubricLabel} score yet
        </div>
        <div style={{ marginBottom: 12, color: "#475569", lineHeight: 1.5, whiteSpace: "pre-line" }}>
          {description}
          {" "}Bands:{" "}
          <span style={{ color: "#dc2626", fontWeight: 600 }}>red</span> below 50%,{" "}
          <span style={{ color: "#d97706", fontWeight: 600 }}>amber</span> 50-79%,{" "}
          <span style={{ color: "#16a34a", fontWeight: 600 }}>green</span> 80%+.
        </div>
        {emptyNote && (
          <div style={{ marginBottom: 12, color: "#64748b", fontSize: 12, lineHeight: 1.5, whiteSpace: "pre-line" }}>
            {emptyNote}
          </div>
        )}
        {scoreOnDemand && (
          <button
            type="button"
            onClick={scoreNow}
            disabled={scoring}
            style={btnPrimary}
          >
            {scoring ? "Scoring…" : "Score now"}
          </button>
        )}
      </div>
    );
  }

  const palette = BAND_PALETTE[payload.band];
  const bandHeadline =
    rubricMeta.rubric_band_headlines?.[payload.band] ||
    DEFAULT_BAND_HEADLINES[payload.band];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* Band header */}
      <div
        style={{
          padding: 16,
          borderRadius: 10,
          backgroundColor: palette.bg,
          border: `1px solid ${palette.dot}33`,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span
              aria-hidden
              style={{
                width: 16,
                height: 16,
                borderRadius: "50%",
                backgroundColor: palette.dot,
                display: "inline-block",
                flexShrink: 0,
              }}
            />
            <div>
              <div style={{ fontSize: 18, fontWeight: 700, color: palette.fg }}>
                {rubricLabel}
              </div>
              <div style={{ fontSize: 13, color: palette.fg, opacity: 0.85, marginTop: 2 }}>
                {bandHeadline}
              </div>
            </div>
          </div>
          <div style={{ textAlign: "right" }}>
            <div style={{ fontSize: 28, fontWeight: 700, color: palette.fg, lineHeight: 1 }}>
              {payload.completeness}%
            </div>
            <div style={{ fontSize: 11, color: palette.fg, opacity: 0.85, marginTop: 4 }}>
              Completeness · Conformance {payload.conformance_pass ? "pass" : "fail"}
            </div>
          </div>
        </div>
        {scoreOnDemand && (
          <div style={{ marginTop: 12, display: "flex", gap: 8, alignItems: "center" }}>
            <button type="button" onClick={scoreNow} disabled={scoring} style={btnPrimary}>
              {scoring ? "Scoring…" : "Re-score now"}
            </button>
            {payload.evaluated_at && (
              <span style={{ fontSize: 11, color: "#64748b" }}>
                Last scored: {new Date(payload.evaluated_at).toLocaleString()}
              </span>
            )}
          </div>
        )}
        {advisorError && (
          <div style={{ marginTop: 10, fontSize: 12, color: "#92400e" }}>
            Note: the LLM advisor narrative wasn't generated this run ({advisorError}). The deterministic
            score above is still authoritative.
          </div>
        )}
      </div>

      {/* Top next actions — highest-impact unmet criteria, computed client-side
          from the checklist. Hidden when nothing's failing/partial so a green
          product doesn't get nagged with empty advice. */}
      {(() => {
        const topActions = payload.checklist
          .filter((r) => r.status === "fail" || r.status === "partial")
          .map((r) => ({ row: r, hint: getHint(r.criterion, r.status) }))
          .filter((x): x is { row: typeof x.row; hint: RubricHint } => x.hint !== null)
          .sort((a, b) => b.row.weight - a.row.weight)
          .slice(0, 3);
        if (topActions.length === 0) return null;
        return (
          <div style={panelStyle}>
            <div style={panelHeader}>Top next actions</div>
            <div style={{ display: "flex", flexDirection: "column" }}>
              {topActions.map(({ row, hint }, i) => (
                <div
                  key={`top-${row.criterion}-${i}`}
                  style={{
                    display: "grid",
                    gridTemplateColumns: "24px 1fr auto",
                    alignItems: "start",
                    gap: 12,
                    padding: "8px 0",
                    borderTop: i === 0 ? "none" : "1px solid #f1f5f9",
                  }}
                >
                  <span
                    aria-hidden
                    style={{ color: STATUS_COLOR[row.status], fontWeight: 700, fontSize: 16, lineHeight: 1.2 }}
                  >
                    {STATUS_ICON[row.status]}
                  </span>
                  <div>
                    <div style={{ fontWeight: 600, color: "#0f172a", fontSize: 13 }}>{row.criterion}</div>
                    <div style={{ fontSize: 12, color: "#1e293b", marginTop: 2 }}>{hint.title}</div>
                  </div>
                  <div style={{ fontSize: 11, color: "#0f172a", whiteSpace: "nowrap", marginTop: 2, fontWeight: 600 }}>
                    +{row.weight} pts
                  </div>
                </div>
              ))}
            </div>
          </div>
        );
      })()}

      {/* Narrative — markdown (## headings, `code`, lists, **bold**) */}
      {payload.narrative && (
        <div style={panelStyle}>
          <div style={panelHeader}>Summary</div>
          <div style={{ fontSize: 13, color: "#1e293b", lineHeight: 1.55 }}>
            <MarkdownMessage content={payload.narrative} />
          </div>
        </div>
      )}

      {/* Checklist — each row is expandable when there's a "how to improve"
          hint for its current status (fail/partial always; pass/na only when
          the hint adds value beyond the backend's reason text). */}
      <div style={panelStyle}>
        <div style={panelHeader}>Checklist</div>
        <div style={{ display: "flex", flexDirection: "column" }}>
          {payload.checklist.map((row, i) => {
            const rowKey = `${row.criterion}-${i}`;
            const hint = getHint(row.criterion, row.status);
            const isExpandable = hint !== null && (row.status === "fail" || row.status === "partial");
            const isOpen = !!expanded[rowKey];
            return (
              <div
                key={rowKey}
                style={{
                  borderTop: i === 0 ? "none" : "1px solid #f1f5f9",
                }}
              >
                <div
                  onClick={isExpandable ? () => setExpanded((p) => ({ ...p, [rowKey]: !p[rowKey] })) : undefined}
                  role={isExpandable ? "button" : undefined}
                  tabIndex={isExpandable ? 0 : undefined}
                  onKeyDown={
                    isExpandable
                      ? (e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            setExpanded((p) => ({ ...p, [rowKey]: !p[rowKey] }));
                          }
                        }
                      : undefined
                  }
                  aria-expanded={isExpandable ? isOpen : undefined}
                  style={{
                    display: "grid",
                    gridTemplateColumns: "24px 1fr auto auto",
                    alignItems: "start",
                    gap: 12,
                    padding: "10px 0",
                    cursor: isExpandable ? "pointer" : "default",
                  }}
                >
                  <span
                    aria-label={row.status}
                    title={row.status}
                    style={{
                      color: STATUS_COLOR[row.status],
                      fontWeight: 700,
                      fontSize: 16,
                      lineHeight: 1.2,
                    }}
                  >
                    {STATUS_ICON[row.status]}
                  </span>
                  <div>
                    <div style={{ fontWeight: 600, color: "#0f172a", fontSize: 13 }}>{row.criterion}</div>
                    <div style={{ fontSize: 12, color: "#64748b", marginTop: 2 }}>{row.reason}</div>
                  </div>
                  <div style={{ fontSize: 11, color: "#94a3b8", whiteSpace: "nowrap", marginTop: 2 }}>
                    {row.weight > 0 ? `${row.weight} pts` : "n/a"}
                  </div>
                  <div
                    aria-hidden
                    style={{
                      fontSize: 11,
                      color: isExpandable ? "#64748b" : "transparent",
                      whiteSpace: "nowrap",
                      marginTop: 2,
                      width: 64,
                      textAlign: "right",
                      userSelect: "none",
                    }}
                  >
                    {isExpandable ? (isOpen ? "Hide ▴" : "How to fix ▾") : ""}
                  </div>
                </div>
                {isExpandable && isOpen && hint && (
                  <div
                    style={{
                      padding: "8px 12px 12px 36px",
                      fontSize: 12,
                      color: "#1e293b",
                      backgroundColor: "#f8fafc",
                      borderRadius: 6,
                      marginBottom: 4,
                      lineHeight: 1.5,
                    }}
                  >
                    <div style={{ fontWeight: 600, marginBottom: hint.detail ? 4 : 0 }}>{hint.title}</div>
                    {hint.detail && <div style={{ color: "#475569" }}>{hint.detail}</div>}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Conformance errors (only when present) */}
      {payload.errors.length > 0 && (
        <div style={{ ...panelStyle, borderColor: "#fecaca", backgroundColor: "#fef2f2" }}>
          <div style={{ ...panelHeader, color: "#991b1b" }}>Conformance errors</div>
          <ul style={{ margin: 0, paddingLeft: 20 }}>
            {payload.errors.map((e, i) => (
              <li key={i} style={{ fontSize: 12, color: "#991b1b", margin: "4px 0" }}>
                <strong>{e.kind}</strong> at <code style={{ fontSize: 11 }}>{e.path}</code> — {e.message}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

const panelStyle: CSSProperties = {
  padding: 16,
  borderRadius: 10,
  border: "1px solid #e2e8f0",
  backgroundColor: "#fff",
};

const panelHeader: CSSProperties = {
  fontSize: 13,
  fontWeight: 700,
  color: "#0f172a",
  marginBottom: 10,
  letterSpacing: 0.2,
  textTransform: "uppercase",
};

const btnPrimary: CSSProperties = {
  padding: "6px 14px",
  borderRadius: 6,
  border: "none",
  backgroundColor: "#0f172a",
  color: "#fff",
  fontSize: 12,
  fontWeight: 600,
  cursor: "pointer",
};

// Mirrors `styles.empty` in QuestionsPanel.tsx so the two readiness-review
// panels render identical no-data placeholders when stacked together.
const emptyStateStyle: CSSProperties = {
  padding: 24,
  textAlign: "center",
  color: "#64748b",
  fontSize: 13,
  backgroundColor: "#f8fafc",
  border: "1px dashed #cbd5e1",
  borderRadius: 8,
};
