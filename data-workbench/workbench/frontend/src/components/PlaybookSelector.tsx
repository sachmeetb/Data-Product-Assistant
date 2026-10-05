import { useEffect, useState } from "react";
import api from "../api/client";
import type { PlaybookOptions } from "../types";

interface Props {
  projectId: number;
  stageNumber: number;
  stageName: string;
  onSelect: (version: string) => void;
  onCancel: () => void;
}

export default function PlaybookSelector({ projectId, stageNumber, stageName, onSelect, onCancel }: Props) {
  const [options, setOptions] = useState<PlaybookOptions | null>(null);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<"baseline" | "refined">("baseline");

  useEffect(() => {
    api.get(`/api/projects/${projectId}/stages/${stageNumber}/playbook-options`)
      .then((r) => {
        setOptions(r.data);
        // Default to refined if available
        if (r.data.refined) setSelected("refined");
      })
      .catch(() => setOptions(null))
      .finally(() => setLoading(false));
  }, [projectId, stageNumber]);

  // If no refined playbook exists, skip the modal entirely
  useEffect(() => {
    if (!loading && options && !options.refined) {
      onSelect("baseline");
    }
  }, [loading, options, onSelect]);

  // Still loading or auto-selecting
  if (loading || (options && !options.refined)) {
    return null;
  }

  if (!options) {
    onSelect("baseline");
    return null;
  }

  return (
    <div style={styles.overlay}>
      <div style={styles.modal}>
        <div style={styles.header}>
          <h3 style={styles.title}>Select Playbook for {stageName}</h3>
          <p style={styles.subtitle}>
            A refined playbook is available. Choose which version to use for this run.
          </p>
        </div>

        <div style={styles.cardRow}>
          {/* Baseline card */}
          <div
            onClick={() => setSelected("baseline")}
            style={{
              ...styles.card,
              borderColor: selected === "baseline" ? "#3b82f6" : "#e2e8f0",
              backgroundColor: selected === "baseline" ? "#eff6ff" : "#fff",
            }}
          >
            <div style={styles.cardLabel}>Baseline</div>
            <div style={styles.cardVersion}>v{options.baseline.version}</div>
            <div style={styles.cardSummary}>{options.baseline.summary}</div>
            {options.baseline.item_count != null && (
              <div style={styles.cardMeta}>{options.baseline.item_count} rules</div>
            )}
          </div>

          {/* Refined card */}
          {options.refined && (
            <div
              onClick={() => setSelected("refined")}
              style={{
                ...styles.card,
                borderColor: selected === "refined" ? "#22c55e" : "#e2e8f0",
                backgroundColor: selected === "refined" ? "#f0fdf4" : "#fff",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <div style={{ ...styles.cardLabel, color: "#16a34a" }}>Refined</div>
                <span style={styles.starBadge}>Updated</span>
              </div>
              <div style={styles.cardVersion}>v{options.refined.version}</div>
              <div style={styles.cardSummary}>{options.refined.summary}</div>
              {options.refined.item_count != null && (
                <div style={styles.cardMeta}>{options.refined.item_count} rules</div>
              )}
              {options.refined.updated_at && (
                <div style={styles.cardMeta}>
                  Updated: {new Date(options.refined.updated_at).toLocaleDateString()}
                </div>
              )}
            </div>
          )}
        </div>

        <div style={styles.actions}>
          <button onClick={onCancel} style={styles.cancelBtn}>Cancel</button>
          <button onClick={() => onSelect(selected)} style={styles.selectBtn}>
            Use {selected === "refined" ? "Refined" : "Baseline"} Playbook
          </button>
        </div>
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  overlay: {
    position: "fixed",
    inset: 0,
    backgroundColor: "rgba(0,0,0,0.4)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    zIndex: 1000,
  },
  modal: {
    backgroundColor: "#fff",
    borderRadius: 12,
    padding: 24,
    maxWidth: 560,
    width: "90%",
    boxShadow: "0 20px 60px rgba(0,0,0,0.2)",
  },
  header: {
    marginBottom: 20,
  },
  title: {
    margin: 0,
    fontSize: 18,
    fontWeight: 700,
    color: "#0f172a",
  },
  subtitle: {
    margin: "6px 0 0",
    fontSize: 13,
    color: "#64748b",
  },
  cardRow: {
    display: "grid",
    gridTemplateColumns: "1fr 1fr",
    gap: 12,
    marginBottom: 20,
  },
  card: {
    padding: 16,
    borderRadius: 10,
    border: "2px solid #e2e8f0",
    cursor: "pointer",
    transition: "all 0.15s",
  },
  cardLabel: {
    fontSize: 12,
    fontWeight: 700,
    textTransform: "uppercase" as const,
    letterSpacing: "0.05em",
    color: "#3b82f6",
    marginBottom: 4,
  },
  cardVersion: {
    fontSize: 22,
    fontWeight: 700,
    color: "#0f172a",
    marginBottom: 6,
  },
  cardSummary: {
    fontSize: 13,
    color: "#475569",
    lineHeight: 1.4,
    marginBottom: 8,
  },
  cardMeta: {
    fontSize: 11,
    color: "#94a3b8",
  },
  starBadge: {
    fontSize: 10,
    fontWeight: 700,
    padding: "2px 6px",
    borderRadius: 4,
    backgroundColor: "#dcfce7",
    color: "#16a34a",
  },
  actions: {
    display: "flex",
    justifyContent: "flex-end",
    gap: 8,
  },
  cancelBtn: {
    padding: "8px 18px",
    borderRadius: 6,
    border: "1px solid #cbd5e1",
    backgroundColor: "#fff",
    color: "#64748b",
    fontWeight: 600,
    fontSize: 13,
    cursor: "pointer",
  },
  selectBtn: {
    padding: "8px 18px",
    borderRadius: 6,
    border: "none",
    backgroundColor: "#3b82f6",
    color: "#fff",
    fontWeight: 600,
    fontSize: 13,
    cursor: "pointer",
  },
};
