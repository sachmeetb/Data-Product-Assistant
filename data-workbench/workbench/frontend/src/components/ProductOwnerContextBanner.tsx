import { useEffect, useState } from "react";
import api from "../api/client";
import type { ProjectInfo } from "../types";
import { type ServingMode, MODE_LABELS, MODE_ACCENT } from "../lib/servingModes";

interface Props {
  project: ProjectInfo;
  /** Bump to force a re-fetch of the summary stats (e.g. after a stage completes). */
  refreshKey?: number;
}

interface SummaryStats {
  archetype: string;
  po_serving_preference?: ServingMode | null;
  po_serving_reason?: string | null;
  po_source_platform?: string | null;
  po_target_platform?: string | null;
  datasets: number;
  columns: number;
  dq_rules: number;
  quality_composite: number | null;
  serving_definitions: number;
  dprod_columns_total: number;
  dprod_columns_mapped: number;
  inputs_source_products: number;
  inputs_datasets: number;
  inputs_columns: number;
}

interface Stat {
  label: string;
  value: string;
}

// Mirror of ProjectDashboard.CONSUMER_HIDDEN_CARDS — consumer-aligned
// products have no source catalog, so the datasets/columns cells would
// read 0. Pick a CONSUMES-oriented stat set for them instead.
function buildStats(s: SummaryStats): Stat[] {
  const score = s.quality_composite != null ? `${Math.round(s.quality_composite * 100)}%` : "—";
  const mapped = s.dprod_columns_total > 0
    ? `${s.dprod_columns_mapped}/${s.dprod_columns_total}`
    : "—";
  const deployed = s.serving_definitions > 0 ? "yes" : "no";

  if (s.archetype === "dpe-cf") {
    const stats: Stat[] = [
      { label: "Inputs", value: String(s.inputs_source_products) },
      { label: "Input columns", value: String(s.inputs_columns) },
      { label: "Mapped", value: mapped },
      { label: "Deployed", value: deployed },
    ];
    if (s.dq_rules > 0) stats.push({ label: "DQ rules", value: String(s.dq_rules) });
    if (s.quality_composite != null) stats.push({ label: "DQ score", value: score });
    return stats;
  }

  return [
    { label: "Datasets", value: String(s.datasets) },
    { label: "Columns", value: String(s.columns) },
    { label: "Mapped", value: mapped },
    { label: "DQ score", value: score },
    { label: "DQ rules", value: String(s.dq_rules) },
    { label: "Deployed", value: deployed },
  ];
}

const PO_STAGE_IDS = new Set([
  "initiate",
  "odcs_specification",
  "publish",
  "po_source_validation",
]);
const PO_STAGE_LABELS: Record<string, string> = {
  initiate: "Initiate",
  odcs_specification: "ODCS Contract",
  publish: "Publish",
  po_source_validation: "PO Source Validation",
};

/**
 * Collapsed read-only banner shown above the pipeline in the Engineering
 * Workbench. Summarises the product-owner context (ODCS spec, domain, status
 * of the PO-owned stages) so the engineer understands what they're picking
 * up without needing to see — or be tempted to run — those stages inline.
 * For dpe-sa, also surfaces the PO's free-form idea text from
 * Project.product_idea so the engineer can re-read the intent any time.
 */
export default function ProductOwnerContextBanner({ project, refreshKey }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [stats, setStats] = useState<SummaryStats | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.get(`/api/projects/${project.id}/summary`)
      .then((r) => { if (!cancelled) setStats(r.data as SummaryStats); })
      .catch(() => { if (!cancelled) setStats(null); });
    return () => { cancelled = true; };
  }, [project.id, refreshKey]);

  const allStages = project.multi_workflow
    ? project.workflows.flatMap((w) => w.stages)
    : project.stages;

  const poStages = allStages.filter((s) => s.stage_id && PO_STAGE_IDS.has(s.stage_id));
  const idea = (project.product_idea || "").trim();
  // SA projects always have at least po_source_validation; dpe-cf projects
  // always have initiate/odcs_specification. If we somehow have neither
  // PO stages nor an idea, there's nothing to show.
  if (poStages.length === 0 && !idea) return null;

  const completed = poStages.filter((s) => s.status === "complete").length;

  return (
    <div
      style={{
        marginBottom: 16,
        padding: 12,
        borderRadius: 8,
        border: "1px solid #e2e8f0",
        backgroundColor: "#f8fafc",
        fontSize: 13,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <span
            style={{
              padding: "2px 8px",
              fontSize: 10,
              fontWeight: 700,
              letterSpacing: 0.4,
              textTransform: "uppercase",
              color: "#5b21b6",
              backgroundColor: "#ede9fe",
              borderRadius: 3,
            }}
          >
            Product Owner context
          </span>
          <span style={{ fontWeight: 600, color: "#0f172a" }}>{project.name}</span>
          {project.domain && (
            <span style={{ color: "#64748b" }}>domain: {project.domain}</span>
          )}
          {poStages.length > 0 && (
            <span style={{ color: "#64748b" }}>
              {completed}/{poStages.length} PO stages complete
            </span>
          )}
        </div>
        {(poStages.length > 0 || idea) && (
          <button
            type="button"
            onClick={() => setExpanded(!expanded)}
            style={{
              padding: "4px 10px",
              fontSize: 12,
              fontWeight: 600,
              color: "#334155",
              backgroundColor: "#fff",
              border: "1px solid #cbd5e1",
              borderRadius: 5,
              cursor: "pointer",
            }}
          >
            {expanded ? "Hide details" : "Show details"}
          </button>
        )}
      </div>

      {stats?.po_serving_preference && (() => {
        const pref = stats.po_serving_preference as ServingMode;
        const accent = MODE_ACCENT[pref] ?? MODE_ACCENT.virtual;
        return (
          <div style={{
            marginTop: 10, padding: "8px 12px", borderRadius: 6, fontSize: 13,
            background: accent.bg, border: `1px solid ${accent.border}`, color: "#334155",
          }}>
            <span style={{ fontWeight: 700, color: accent.fg }}>
              Product Owner recommends: {MODE_LABELS[pref] ?? pref}
            </span>
            {stats.po_serving_reason ? <span style={{ color: "#475569" }}> — {stats.po_serving_reason}</span> : null}
            <span style={{ color: "#94a3b8" }}> · advisory; choose the serving mode on the serving stage.</span>
          </div>
        );
      })()}

      {(stats?.po_source_platform || stats?.po_target_platform) && (
        <div style={{
          marginTop: 6, display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap",
          fontSize: 12, color: "#475569",
        }}>
          <span style={{ fontWeight: 600, color: "#334155" }}>Platform hint:</span>
          {stats?.po_source_platform && (
            <span style={{
              padding: "2px 8px", borderRadius: 4, backgroundColor: "#dbeafe",
              color: "#1e40af", border: "1px solid #93c5fd", fontWeight: 600, fontSize: 11,
            }}>
              Source: {stats.po_source_platform}
            </span>
          )}
          {stats?.po_source_platform && (stats?.po_target_platform && stats.po_target_platform !== "same") && (
            <span style={{ color: "#94a3b8" }}>→</span>
          )}
          {stats?.po_target_platform && stats.po_target_platform !== "same" && (
            <span style={{
              padding: "2px 8px", borderRadius: 4, backgroundColor: "#dcfce7",
              color: "#166534", border: "1px solid #86efac", fontWeight: 600, fontSize: 11,
            }}>
              Target: {stats.po_target_platform}
            </span>
          )}
          <span style={{ color: "#94a3b8" }}>· advisory; configure in Select Data Source.</span>
        </div>
      )}

      {(idea || stats) && (
        <div
          style={{
            marginTop: 10,
            display: "grid",
            gridTemplateColumns: idea && stats ? "1fr minmax(220px, 0.7fr)" : "1fr",
            gap: 10,
            alignItems: "stretch",
          }}
        >
          {idea && (
            <div
              style={{
                padding: "8px 10px",
                borderRadius: 6,
                backgroundColor: "#fff",
                border: "1px solid #e2e8f0",
                fontSize: 13,
                color: "#334155",
                whiteSpace: "pre-wrap",
              }}
            >
              <div style={{ fontSize: 11, fontWeight: 600, color: "#5b21b6", marginBottom: 4, textTransform: "uppercase", letterSpacing: 0.4 }}>
                PO's intent
              </div>
              {idea}
            </div>
          )}

          {stats && (
            <div
              style={{
                padding: "8px 10px",
                borderRadius: 6,
                backgroundColor: "#fff",
                border: "1px solid #e2e8f0",
              }}
            >
              <div style={{ fontSize: 11, fontWeight: 600, color: "#5b21b6", marginBottom: 6, textTransform: "uppercase", letterSpacing: 0.4 }}>
                At a glance
              </div>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
                  gap: "6px 14px",
                }}
              >
                {buildStats(stats).map((st) => (
                  <div key={st.label} style={{ display: "flex", flexDirection: "column" }}>
                    <span style={{ fontSize: 10, color: "#64748b", textTransform: "uppercase", letterSpacing: 0.3 }}>
                      {st.label}
                    </span>
                    <span style={{ fontSize: 15, fontWeight: 700, color: "#0f172a" }}>{st.value}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {expanded && poStages.length > 0 && (
        <div
          style={{
            marginTop: 10,
            paddingTop: 10,
            borderTop: "1px solid #e2e8f0",
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))",
            gap: 8,
          }}
        >
          {poStages.map((s) => (
            <div
              key={`${s.workflow_id ?? ""}-${s.stage_number}`}
              style={{
                padding: "6px 10px",
                borderRadius: 6,
                border: "1px solid #e2e8f0",
                backgroundColor: "#fff",
                display: "flex",
                flexDirection: "column",
                gap: 2,
              }}
            >
              <div style={{ fontSize: 12, fontWeight: 600, color: "#334155" }}>
                {(s.stage_id && PO_STAGE_LABELS[s.stage_id]) || s.stage_name}
              </div>
              <div style={{ fontSize: 11, color: statusColor(s.status) }}>{s.status}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function statusColor(status: string): string {
  switch (status) {
    case "complete":
      return "#059669";
    case "running":
      return "#d97706";
    case "awaiting_review":
      return "#7c3aed";
    case "failed":
      return "#dc2626";
    default:
      return "#64748b";
  }
}
