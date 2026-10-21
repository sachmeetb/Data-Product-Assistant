import { useEffect, useState } from "react";
import api from "../api/client";
import type { ProjectDetail, StageInfo } from "../types";
import SourcePlatformBadge from "./SourcePlatformBadge";
import ConfigureMigrationDialog from "./ConfigureMigrationDialog";

interface MigrationStatus {
  configured: boolean;
  status?: string | null;
  landing_strategy?: string | null;
  target_platform?: string | null;
  target_intent_only?: boolean;
  write_disposition?: string | null;
}

interface Props {
  project: ProjectDetail;
  refreshKey?: number;
  onChanged?: () => void;
}

// Fills the dmig project-detail header slot: shows the migration SOURCE and TARGET
// platforms side by side and lets the engineer configure each — the source via the
// shared SourcePlatformBadge, the target via ConfigureMigrationDialog.
export default function MigrationSourceTargetPanel({ project, refreshKey, onChanged }: Props) {
  const [status, setStatus] = useState<MigrationStatus | null>(null);
  const [configureTarget, setConfigureTarget] = useState<StageInfo | null>(null);

  const load = () => {
    api.get(`/api/projects/${project.id}/migration/status`)
      .then((r) => setStatus(r.data))
      .catch(() => setStatus(null));
  };
  useEffect(load, [project.id, refreshKey]);

  // The dmig_configure StageInfo (needed to complete the stage after configuring).
  const configureStage: StageInfo | undefined = (project.workflows || [])
    .flatMap((w) => w.stages || [])
    .find((s) => s.stage_id === "dmig_configure");

  const box = {
    flex: 1, minWidth: 0, border: "1px solid #e2e8f0", borderRadius: 10, background: "#fff",
    padding: "14px 16px", display: "flex", flexDirection: "column" as const, gap: 8,
  };
  const label = { fontSize: 11, fontWeight: 700, letterSpacing: 0.5, textTransform: "uppercase" as const, color: "#64748b" };
  const platformPill = (text: string, color: string, bg: string) => (
    <span style={{ display: "inline-flex", alignItems: "center", padding: "3px 10px", borderRadius: 8,
      background: bg, color, fontSize: 13, fontWeight: 700 }}>{text}</span>
  );

  const targetPlatform = status?.target_platform || "";
  const configured = !!status?.configured;

  return (
    <div style={{ display: "flex", gap: 12, alignItems: "stretch", height: "100%" }}>
      {/* Source */}
      <div style={box}>
        <span style={label}>Source</span>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <SourcePlatformBadge projectId={project.id} onChanged={onChanged} />
        </div>
        <div style={{ fontSize: 12, color: "#94a3b8", marginTop: "auto" }}>
          The legacy platform to migrate from. Configure it above or from “+ Source”.
        </div>
      </div>

      {/* Arrow */}
      <div style={{ display: "flex", alignItems: "center", color: "#94a3b8", fontSize: 22, fontWeight: 700 }}>→</div>

      {/* Target */}
      <div style={box}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <span style={label}>Target</span>
          {configured && status?.status && (
            <span style={{ fontSize: 11, color: "#64748b" }}>
              {status.landing_strategy} · {status.write_disposition} · {status.status}
            </span>
          )}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {targetPlatform
            ? platformPill(targetPlatform, "#065f46", "#d1fae5")
            : <span style={{ fontSize: 13, color: "#94a3b8" }}>Not configured</span>}
          {status?.target_intent_only && (
            <span title="Platform intent only — no live target connection bound yet; Run/Reconcile are locked."
              style={{ fontSize: 11, fontWeight: 700, color: "#92400e", background: "#fef3c7", border: "1px solid #fcd34d", borderRadius: 6, padding: "2px 6px" }}>
              intent only
            </span>
          )}
          <button
            onClick={() => configureStage && setConfigureTarget(configureStage)}
            disabled={!configureStage}
            title={configureStage ? "Choose the target platform + write disposition" : "Migration workflow not present"}
            style={{ padding: "4px 12px", borderRadius: 6, border: "1px solid #cbd5e1", background: "#fff",
              color: "#475569", fontSize: 12, fontWeight: 600, cursor: configureStage ? "pointer" : "not-allowed" }}
          >
            {targetPlatform ? "Change target" : "Configure target"}
          </button>
        </div>
        <div style={{ fontSize: 12, color: "#94a3b8", marginTop: "auto" }}>
          The platform to migrate into (raw lift-and-shift — names and types preserved).
        </div>
      </div>

      <ConfigureMigrationDialog
        open={configureTarget !== null}
        projectId={project.id}
        stage={configureTarget}
        onClose={() => setConfigureTarget(null)}
        onCompleted={() => { setConfigureTarget(null); load(); onChanged?.(); }}
      />
    </div>
  );
}
