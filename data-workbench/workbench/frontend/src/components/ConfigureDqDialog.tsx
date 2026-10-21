import { useState } from "react";
import api from "../api/client";
import type { StageInfo } from "../types";

/**
 * Configure DQ — the DQ counterpart to ConfigureServingDialog. A lightweight
 * checkpoint that picks the test FRAMEWORK (Great Expectations vs Pure Python /
 * Pandera) before the Build stage generates that framework's package. Selecting a
 * framework switches the `dq_test_gen` exclusive group (idempotent server-side),
 * then completes the checkpoint.
 *
 * The catalog-vs-dprod test MODE is NOT chosen here — it's determined by the
 * workflow the DQ stages live in (dq_testing = source; product_dq_testing =
 * deployed product), so this dialog is framework-only by design.
 */

interface Props {
  open: boolean;
  projectId: number;
  stage: StageInfo | null;
  /** The currently-active generation stage_id in this workflow
   *  (dq_test_generation_gx | dq_test_generation_python), or null if none yet. */
  activeGenStageId: string | null;
  onClose: () => void;
  onCompleted: () => void;
}

const FRAMEWORKS: { id: string; label: string; blurb: string }[] = [
  { id: "dq_test_generation_gx", label: "Great Expectations",
    blurb: "Industry-standard expectation suites (great-expectations)." },
  { id: "dq_test_generation_python", label: "Pure Python (Pandera)",
    blurb: "Lightweight Pandera schema validators — fewer dependencies." },
];

export default function ConfigureDqDialog({ open, projectId, stage, activeGenStageId, onClose, onCompleted }: Props) {
  const [choice, setChoice] = useState<string>(activeGenStageId || "dq_test_generation_gx");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open || !stage) return null;

  const handleConfirm = async () => {
    setSaving(true);
    setError(null);
    try {
      // Enforce the chosen framework on the exclusive group (idempotent — a no-op
      // when already the active member). Reuses the generic exclusive-group switch,
      // which re-keys the StageRuns positionally.
      if (choice !== activeGenStageId) {
        await api.post(`/api/projects/${projectId}/workflows/${stage.workflow_id || ""}/exclusive-group`, {
          group: "dq_test_gen",
          select_stage_id: choice,
        });
      }
      // Complete the checkpoint only when the backend will accept it (Reconfigure
      // re-opens an already-complete stage; POSTing /complete again 400s).
      if (stage.status === "pending" || stage.status === "running") {
        await api.post(
          `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
          null,
          { params: { workflow_id: stage.workflow_id } },
        );
      }
      onCompleted();
      onClose();
    } catch (e) {
      setError(`Failed to configure DQ: ${(e as Error).message || e}`);
    }
    setSaving(false);
  };

  return (
    <div
      role="dialog"
      style={{
        position: "fixed", inset: 0, backgroundColor: "rgba(0,0,0,0.4)",
        display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1100,
      }}
      onClick={() => !saving && onClose()}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "#fff", borderRadius: 10, width: "min(480px, 94vw)",
          padding: 24, boxShadow: "0 12px 40px rgba(0,0,0,0.25)",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
          <h2 style={{ margin: 0, fontSize: 18 }}>Configure DQ</h2>
          <button onClick={() => !saving && onClose()} style={{ border: "none", background: "none", fontSize: 22, cursor: "pointer", color: "#64748b" }}>×</button>
        </div>
        <p style={{ marginTop: 0, color: "#64748b", fontSize: 13 }}>
          Pick the test framework. The <strong>Build</strong> stage generates that framework's
          runnable, downloadable package; <strong>Run DQ Tests</strong> executes it.
        </p>

        {error && (
          <div style={{ background: "#fef2f2", border: "1px solid #fecaca", color: "#b91c1c", padding: "8px 12px", borderRadius: 6, fontSize: 13, marginBottom: 12 }}>
            {error}
          </div>
        )}

        <div style={{ display: "flex", flexDirection: "column", gap: 10, margin: "14px 0" }}>
          {FRAMEWORKS.map((fw) => (
            <label
              key={fw.id}
              style={{
                display: "flex", gap: 10, alignItems: "flex-start", padding: "10px 12px",
                border: `1px solid ${choice === fw.id ? "#3b82f6" : "#e2e8f0"}`,
                borderRadius: 8, cursor: "pointer",
                background: choice === fw.id ? "#eff6ff" : "#fff",
              }}
            >
              <input
                type="radio" name="dq_framework" checked={choice === fw.id}
                onChange={() => setChoice(fw.id)} style={{ marginTop: 3 }}
              />
              <span>
                <div style={{ fontWeight: 600, fontSize: 14 }}>{fw.label}</div>
                <div style={{ fontSize: 12, color: "#64748b" }}>{fw.blurb}</div>
              </span>
            </label>
          ))}
        </div>

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
          <button
            onClick={() => !saving && onClose()}
            style={{ padding: "6px 16px", borderRadius: 6, border: "1px solid #cbd5e1", background: "#fff", color: "#475569", fontSize: 13, fontWeight: 600, cursor: "pointer" }}
          >
            Cancel
          </button>
          <button
            onClick={handleConfirm}
            disabled={saving}
            style={{ padding: "6px 16px", borderRadius: 6, border: "none", background: saving ? "#cbd5e1" : "#3b82f6", color: "#fff", fontSize: 13, fontWeight: 600, cursor: saving ? "not-allowed" : "pointer" }}
          >
            {saving ? "Saving…" : "Confirm"}
          </button>
        </div>
      </div>
    </div>
  );
}
