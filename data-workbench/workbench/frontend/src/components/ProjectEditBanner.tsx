import { useEffect, useState } from "react";
import api from "../api/client";

interface EditDiffResponse {
  has_edit: boolean;
  reason?: string;
  deployed_version?: number;
  current_version?: number;
  current_state?: string;
  schema?: {
    added: string[];
    removed: string[];
    type_changed: { name: string; from: string; to: string }[];
    description_changed: { name: string; from: string; to: string }[];
  };
  metadata?: {
    changed_fields: { field: string; from: string; to: string }[];
  };
  suggested_rerun_stages?: string[];
  impact_summary?: string[];
}

interface Props {
  projectId: number;
  /** Latest non-rejected ProductRequest for this project. We render only
   *  when kind='edit' AND status in (accepted, submitted), to avoid
   *  showing the banner for new/ingest flows or after the engineer marked
   *  the edit complete. */
  latestRequestKind: string | null;
  latestRequestStatus: string | null;
}

const STAGE_LABELS: Record<string, string> = {
  data_mapping: "Mapping",
  metadata_enrichment: "Enrichment",
  odcs_to_dprod: "ODCS → dprod",
  serving_virtual_view: "Serving (Virtual View)",
  serving_physical_copy: "Serving (Materialized)",
  serving_lakehouse_export: "Serving (Lakehouse)",
  serving_transfer: "Serving (Cross-platform)",
  deploy_virtual_view: "Deploy View",
  deployment_reflection: "Reflection",
  mark_engineering_complete: "Mark Complete",
  dq_test_generation_gx: "DQ Tests (GX)",
  dq_test_generation_python: "DQ Tests (Python)",
};

export default function ProjectEditBanner({ projectId, latestRequestKind, latestRequestStatus }: Props) {
  const isEditContext =
    latestRequestKind === "edit"
    && (latestRequestStatus === "accepted" || latestRequestStatus === "submitted");
  const [diff, setDiff] = useState<EditDiffResponse | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  // Phase 7: change-aware reconciliation. Replaces the old "reset
  // affected stages" pattern with surgical actions per change type
  // (deactivate orphaned mappings for removed columns, skip data_mapping
  // when no new mapping work is needed, etc.) plus stage resets only for
  // stages that genuinely need re-running. Auto-fires on engineer accept;
  // this button is the manual re-trigger.
  const [reconciling, setReconciling] = useState(false);
  const [reconResult, setReconResult] = useState<{ kind: "ok" | "err"; lines: string[] } | null>(null);

  const applyReconciliation = async () => {
    setReconciling(true);
    setReconResult(null);
    try {
      const res = await api.post<{
        has_edit: boolean;
        summary?: string[];
        actions_performed?: Array<{ kind: string; deactivated_count?: number }>;
        stages_reset?: Array<{ stage_id: string }>;
      }>(
        `/api/projects/${projectId}/edits/apply-reconciliation`,
        {}
      );
      const lines = res.data.summary ?? ["No reconciliation actions needed."];
      setReconResult({ kind: "ok", lines });
    } catch (e) {
      setReconResult({ kind: "err", lines: [e instanceof Error ? e.message : String(e)] });
    } finally {
      setReconciling(false);
    }
  };

  useEffect(() => {
    if (!isEditContext) {
      setDiff(null);
      return;
    }
    api
      .get<EditDiffResponse>(`/api/projects/${projectId}/edit-diff`)
      .then((r) => setDiff(r.data))
      .catch(() => setDiff(null));
  }, [projectId, isEditContext]);

  if (!isEditContext || !diff || !diff.has_edit) return null;

  const sch = diff.schema || { added: [], removed: [], type_changed: [], description_changed: [] };
  const meta = diff.metadata || { changed_fields: [] };
  const impact = diff.impact_summary || [];
  const suggested = diff.suggested_rerun_stages || [];

  return (
    <div
      style={{
        margin: "0 0 16px",
        padding: 14,
        borderRadius: 10,
        border: "1px solid #93c5fd",
        backgroundColor: "#eff6ff",
        color: "#1e3a8a",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
        <div style={{ fontWeight: 700, fontSize: 14 }}>
          Editing a deployed product · v{diff.deployed_version} → v{diff.current_version}
        </div>
        <button
          type="button"
          onClick={() => setCollapsed(!collapsed)}
          style={{
            fontSize: 11,
            padding: "2px 8px",
            borderRadius: 4,
            border: "1px solid #93c5fd",
            backgroundColor: "#fff",
            color: "#1e40af",
            cursor: "pointer",
          }}
        >
          {collapsed ? "Show diff" : "Hide diff"}
        </button>
      </div>
      <div style={{ fontSize: 13, marginTop: 6, lineHeight: 1.5 }}>
        The PO submitted a revision. Review the changes below and rerun any stages affected
        by the diff before clicking <strong>Mark Engineering Complete</strong>.
      </div>

      {!collapsed && (
        <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 10 }}>
          {sch.added.length > 0 && (
            <DiffRow color="#16a34a" label="Added columns" chips={sch.added} />
          )}
          {sch.removed.length > 0 && (
            <DiffRow color="#dc2626" label="Removed columns" chips={sch.removed} />
          )}
          {sch.type_changed.length > 0 && (
            <DiffRowList
              color="#d97706"
              label="Type changes"
              items={sch.type_changed.map((c) => `${c.name}: ${c.from} → ${c.to}`)}
            />
          )}
          {sch.description_changed.length > 0 && (
            <DiffRowList
              color="#0284c7"
              label="Description changes"
              items={sch.description_changed.map((c) => c.name)}
            />
          )}
          {meta.changed_fields.length > 0 && (
            <DiffRowList
              color="#0284c7"
              label="Metadata changes"
              items={meta.changed_fields.map((m) => m.field)}
            />
          )}

          {impact.length > 0 && (
            <div
              style={{
                marginTop: 4,
                padding: 10,
                borderRadius: 8,
                backgroundColor: "#fff7ed",
                border: "1px solid #fed7aa",
              }}
            >
              <div style={{ fontWeight: 700, fontSize: 12, color: "#9a3412", marginBottom: 4 }}>
                Engineering impact
              </div>
              {impact.map((line, i) => (
                <div key={i} style={{ fontSize: 12, color: "#7c2d12", lineHeight: 1.5 }}>
                  • {line}
                </div>
              ))}
            </div>
          )}

          {suggested.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
                <span style={{ fontSize: 12, fontWeight: 600, color: "#1e40af" }}>
                  Suggested rerun:
                </span>
                {suggested.map((s) => (
                  <span
                    key={s}
                    style={{
                      fontSize: 11,
                      padding: "2px 8px",
                      borderRadius: 10,
                      backgroundColor: "#fff",
                      border: "1px solid #93c5fd",
                      color: "#1e3a8a",
                    }}
                  >
                    {STAGE_LABELS[s] || s}
                  </span>
                ))}
                <button
                  type="button"
                  onClick={applyReconciliation}
                  disabled={reconciling}
                  style={{
                    marginLeft: "auto",
                    fontSize: 12,
                    fontWeight: 600,
                    padding: "4px 10px",
                    borderRadius: 6,
                    border: "none",
                    backgroundColor: reconciling ? "#9ca3af" : "#1e40af",
                    color: "white",
                    cursor: reconciling ? "wait" : "pointer",
                  }}
                  title="Apply change-aware reconciliation: deactivate orphaned mappings, reset only the stages that need re-running, skip data_mapping when no new mapping work is needed"
                >
                  {reconciling ? "Reconciling…" : "Apply reconciliation"}
                </button>
              </div>
              {reconResult && (
                <ul
                  style={{
                    margin: 0,
                    paddingLeft: 18,
                    fontSize: 12,
                    color: reconResult.kind === "err" ? "#b91c1c" : "#166534",
                    listStyle: "disc",
                  }}
                >
                  {reconResult.lines.map((line, i) => (
                    <li key={i}>{line}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function DiffRow({ label, color, chips }: { label: string; color: string; chips: string[] }) {
  return (
    <div>
      <div style={{ fontSize: 12, fontWeight: 600, color, marginBottom: 4 }}>{label}</div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        {chips.map((c) => (
          <span
            key={c}
            style={{
              fontSize: 11,
              padding: "2px 8px",
              borderRadius: 10,
              backgroundColor: "#fff",
              border: `1px solid ${color}`,
              color,
            }}
          >
            {c}
          </span>
        ))}
      </div>
    </div>
  );
}

function DiffRowList({ label, color, items }: { label: string; color: string; items: string[] }) {
  return (
    <div>
      <div style={{ fontSize: 12, fontWeight: 600, color, marginBottom: 4 }}>{label}</div>
      <ul style={{ margin: 0, paddingLeft: 18, color: "#334155", fontSize: 12, lineHeight: 1.6 }}>
        {items.map((it, i) => (
          <li key={i}>{it}</li>
        ))}
      </ul>
    </div>
  );
}
