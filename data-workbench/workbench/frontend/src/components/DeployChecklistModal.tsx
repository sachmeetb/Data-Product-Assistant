import { useEffect, useState } from "react";
import api from "../api/client";

/**
 * Non-blocking pre-deploy reminder for consumer products. Fetches the
 * deploy-readiness checklist and shows which operational ODCS sections are
 * set / will be auto-derived / are still empty, then lets the PO either add
 * details (opens the wizard's Operations & Support step, where SLA is
 * pre-seeded from inherited sources and contacts default to the owner) or
 * deploy right away. Servers are always auto-populated on deploy regardless.
 */

interface ChecklistSection {
  key: string;
  label: string;
  status: "set" | "derivable" | "empty";
  detail: string;
}

interface Props {
  projectId: number;
  /** Called when the PO chooses to deploy. The parent runs the publish +
   *  refresh (it already owns that logic and its busy state). */
  onDeploy: () => void;
  /** Open the wizard's Operations & Support step to author the fields. */
  onEditDetails: () => void;
  onClose: () => void;
  deploying: boolean;
}

const STATUS_META: Record<ChecklistSection["status"], { icon: string; color: string }> = {
  set: { icon: "✓", color: "#16a34a" },
  derivable: { icon: "⚠", color: "#d97706" },
  empty: { icon: "○", color: "#94a3b8" },
};

export default function DeployChecklistModal({
  projectId,
  onDeploy,
  onEditDetails,
  onClose,
  deploying,
}: Props) {
  const [loading, setLoading] = useState(true);
  const [sections, setSections] = useState<ChecklistSection[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        const r = await api.get(`/api/projects/${projectId}/odcs/deploy-checklist`);
        if (!cancelled) setSections(r.data?.sections || []);
      } catch (e) {
        if (!cancelled) {
          setError(
            (e as { response?: { data?: { detail?: string } }; message?: string })
              .response?.data?.detail || (e as Error).message || "Could not load checklist"
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [projectId]);

  const gapCount = sections.filter((s) => s.status !== "set").length;

  return (
    <div style={overlay} onClick={onClose}>
      <div style={modal} onClick={(e) => e.stopPropagation()}>
        <h2 style={{ fontSize: 18, fontWeight: 700, color: "#0f172a", margin: "0 0 4px" }}>
          Ready to deploy?
        </h2>
        <p style={{ fontSize: 13, color: "#64748b", margin: "0 0 16px" }}>
          A quick check of your product's operational details. None of these block deployment —
          servers are filled in automatically from where your product is served.
        </p>

        {loading && <div style={{ color: "#64748b", fontSize: 14 }}>Loading checklist…</div>}
        {error && <div style={{ color: "#dc2626", fontSize: 13 }}>{error}</div>}

        {!loading && !error && (
          <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 18 }}>
            {sections.map((s) => {
              const meta = STATUS_META[s.status];
              return (
                <div key={s.key} style={row}>
                  <span style={{ color: meta.color, fontWeight: 700, width: 16, flexShrink: 0 }}>
                    {meta.icon}
                  </span>
                  <span style={{ fontWeight: 600, color: "#1e293b", width: 130, flexShrink: 0 }}>
                    {s.label}
                  </span>
                  <span style={{ fontSize: 13, color: "#64748b" }}>{s.detail}</span>
                </div>
              );
            })}
          </div>
        )}

        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", alignItems: "center" }}>
          <button style={btnGhost} onClick={onClose} disabled={deploying}>Cancel</button>
          {gapCount > 0 && (
            <button style={btnSecondary} onClick={onEditDetails} disabled={deploying}>
              Add details
            </button>
          )}
          <button style={btnPrimary} onClick={onDeploy} disabled={deploying}>
            {deploying ? "Deploying…" : "Deploy now"}
          </button>
        </div>
      </div>
    </div>
  );
}

const overlay: React.CSSProperties = {
  position: "fixed", inset: 0, backgroundColor: "rgba(15,23,42,0.45)",
  display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000,
};
const modal: React.CSSProperties = {
  backgroundColor: "#fff", borderRadius: 12, padding: 24, width: 560, maxWidth: "92vw",
  boxShadow: "0 10px 40px rgba(0,0,0,0.2)",
};
const row: React.CSSProperties = {
  display: "flex", alignItems: "baseline", gap: 10, padding: "6px 0",
  borderBottom: "1px solid #f1f5f9",
};
const btnBase: React.CSSProperties = {
  padding: "8px 16px", borderRadius: 8, fontSize: 13, fontWeight: 600, cursor: "pointer",
  border: "1px solid transparent",
};
const btnPrimary: React.CSSProperties = { ...btnBase, backgroundColor: "#7c3aed", color: "#fff" };
const btnSecondary: React.CSSProperties = { ...btnBase, backgroundColor: "#fff", color: "#7c3aed", border: "1px solid #c4b5fd" };
const btnGhost: React.CSSProperties = { ...btnBase, backgroundColor: "transparent", color: "#64748b" };
