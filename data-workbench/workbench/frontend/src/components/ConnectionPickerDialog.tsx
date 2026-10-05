import { useEffect, useState } from "react";
import api from "../api/client";

export type ConnectionPickerResult =
  | { type: "binding"; connectionId: number; defaultSchema: string };

interface PlatformConnection {
  id: number;
  connection_name: string;
  platform_type: string;
  host: string;
  port: number;
  database: string;
  username: string;
}

interface SourceBinding {
  bound: boolean;
  connection_id?: number;
  connection_name?: string;
  platform_type?: string;
  default_schema?: string;
}

interface Props {
  open: boolean;
  projectId: number;
  mode?: "create" | "edit";
  submitting?: boolean;
  /** PO hint for source platform — shown as a chip when set */
  poSourcePlatform?: string | null;
  onClose: () => void;
  onSubmit: (result: ConnectionPickerResult) => void | Promise<void>;
}

const PLATFORM_COLORS: Record<string, { bg: string; color: string; border: string }> = {
  postgres:   { bg: "#dbeafe", color: "#1e40af", border: "#93c5fd" },
  mysql:      { bg: "#dcfce7", color: "#166534", border: "#86efac" },
  snowflake:  { bg: "#e0f2fe", color: "#0c4a6e", border: "#7dd3fc" },
  databricks: { bg: "#ffedd5", color: "#9a3412", border: "#fdba74" },
};

const PLATFORM_LABELS: Record<string, string> = {
  postgres:   "PostgreSQL",
  mysql:      "MySQL",
  snowflake:  "Snowflake",
  databricks: "Databricks",
};

export default function ConnectionPickerDialog({
  open, projectId, mode = "create", submitting = false,
  poSourcePlatform,
  onClose, onSubmit,
}: Props) {
  const [connections, setConnections] = useState<PlatformConnection[]>([]);
  const [loading, setLoading] = useState(false);
  const [selectedConnectionId, setSelectedConnectionId] = useState<number | null>(null);
  const [defaultSchema, setDefaultSchema] = useState("");

  useEffect(() => {
    if (!open) return;
    setSelectedConnectionId(null);
    setDefaultSchema("");
    setLoading(true);

    Promise.all([
      api.get("/api/connections"),
      api.get(`/api/projects/${projectId}/source-binding`),
    ])
      .then(([connRes, bindRes]) => {
        const conns: PlatformConnection[] = connRes.data?.connections || [];
        setConnections(conns);
        const binding: SourceBinding = bindRes.data || { bound: false };
        if (binding.bound && binding.connection_id) {
          setSelectedConnectionId(binding.connection_id);
          setDefaultSchema(binding.default_schema || "");
        }
      })
      .catch(() => setConnections([]))
      .finally(() => setLoading(false));
  }, [open, projectId]);

  if (!open) return null;

  const canSubmit = !submitting && selectedConnectionId !== null;

  const handleSubmit = () => {
    if (!canSubmit) return;
    void onSubmit({ type: "binding", connectionId: selectedConnectionId!, defaultSchema });
  };

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, backgroundColor: "rgba(15, 23, 42, 0.55)",
        display: "flex", alignItems: "center", justifyContent: "center", zIndex: 100,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 600, maxWidth: "calc(100vw - 32px)", maxHeight: "calc(100vh - 32px)",
          overflowY: "auto", backgroundColor: "#fff", borderRadius: 12, padding: 24,
          boxShadow: "0 20px 40px rgba(15, 23, 42, 0.25)",
          display: "flex", flexDirection: "column", gap: 16,
        }}
      >
        {/* Header */}
        <div>
          <div style={{ fontSize: 16, fontWeight: 700, color: "#0f172a" }}>
            {mode === "edit" ? "Edit Data Source" : "Select Data Source"}
          </div>
          <div style={{ fontSize: 12, color: "#64748b", marginTop: 4, lineHeight: 1.5 }}>
            {mode === "edit"
              ? "Update the connection — downstream stages will pick up the new connection on their next run."
              : "Pick a registered connection. Downstream stages (discovery, profiling, DQ testing) use this connection."}
          </div>
        </div>

        {/* Registered connections */}
        {loading ? (
          <div style={{ color: "#94a3b8", fontSize: 13 }}>Loading connections...</div>
        ) : connections.length === 0 ? (
          <div style={{
            padding: 20, borderRadius: 8, border: "1px dashed #e2e8f0",
            backgroundColor: "#f8fafc", textAlign: "center",
          }}>
            <div style={{ fontSize: 13, color: "#64748b", marginBottom: 8 }}>
              No connections registered yet.
            </div>
            <a
              href="/engineer/connections"
              target="_blank"
              rel="noopener noreferrer"
              style={{ fontSize: 13, fontWeight: 600, color: "#3b82f6", textDecoration: "none" }}
            >
              Register a connection in Data Platform Connections →
            </a>
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ fontSize: 12, fontWeight: 600, color: "#334155" }}>
              Registered connections
            </div>
            {connections.map((conn) => {
              const platformKey = (conn.platform_type || "").toLowerCase();
              const colors = PLATFORM_COLORS[platformKey] ?? { bg: "#f1f5f9", color: "#475569", border: "#cbd5e1" };
              const isSelected = selectedConnectionId === conn.id;
              return (
                <div
                  key={conn.id}
                  onClick={() => setSelectedConnectionId(isSelected ? null : conn.id)}
                  style={{
                    display: "flex", alignItems: "center", gap: 12,
                    padding: "12px 14px", borderRadius: 8, cursor: "pointer",
                    border: `2px solid ${isSelected ? "#3b82f6" : "#e2e8f0"}`,
                    backgroundColor: isSelected ? "#eff6ff" : "#fff",
                    transition: "all 0.12s",
                  }}
                >
                  <span style={{
                    fontSize: 11, fontWeight: 700, padding: "2px 8px", borderRadius: 4,
                    backgroundColor: colors.bg, color: colors.color,
                    border: `1px solid ${colors.border}`, whiteSpace: "nowrap", flexShrink: 0,
                  }}>
                    {PLATFORM_LABELS[platformKey] ?? conn.platform_type}
                  </span>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontWeight: 600, fontSize: 13, color: "#1e293b" }}>
                      {conn.connection_name}
                    </div>
                    <div style={{ fontSize: 11, color: "#64748b", marginTop: 1 }}>
                      {conn.host}:{conn.port} / {conn.database}
                    </div>
                  </div>
                  {isSelected && (
                    <span style={{ marginLeft: "auto", color: "#3b82f6", fontWeight: 700, fontSize: 16 }}>
                      ✓
                    </span>
                  )}
                </div>
              );
            })}
            {/* Schema override for the selected connection */}
            {selectedConnectionId !== null && (
              <label style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 4 }}>
                <span style={{ fontSize: 12, fontWeight: 600, color: "#334155" }}>
                  Default schema (optional)
                </span>
                <input
                  value={defaultSchema}
                  onChange={(e) => setDefaultSchema(e.target.value)}
                  placeholder="e.g. hr_core"
                  style={{
                    padding: "8px 12px", borderRadius: 6, border: "1px solid #cbd5e1",
                    fontSize: 13, fontFamily: "inherit", width: "100%", boxSizing: "border-box",
                  }}
                />
              </label>
            )}
          </div>
        )}

        {/* PO source platform hint */}
        {poSourcePlatform && selectedConnectionId !== null && (
          <div style={{ fontSize: 12, color: "#475569" }}>
            PO source hint:{" "}
            <span style={{
              padding: "2px 7px", borderRadius: 4, backgroundColor: "#dbeafe",
              color: "#1e40af", border: "1px solid #93c5fd", fontWeight: 600, fontSize: 11,
            }}>
              {poSourcePlatform}
            </span>
            {(() => {
              const matchingConn = connections.find(
                (c) => c.platform_type?.toLowerCase() === poSourcePlatform.toLowerCase()
              );
              return matchingConn ? (
                <span style={{ marginLeft: 6, color: "#166534" }}>
                  ✓ Matching connection available
                </span>
              ) : (
                <span style={{ marginLeft: 6, color: "#b45309" }}>
                  No registered {poSourcePlatform} connection yet
                </span>
              );
            })()}
          </div>
        )}

        {/* Footer */}
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 4 }}>
          <button
            type="button" onClick={onClose} disabled={submitting}
            style={{
              padding: "8px 16px", borderRadius: 6, backgroundColor: "#fff",
              color: "#334155", border: "1px solid #cbd5e1", fontSize: 13,
              fontWeight: 600, cursor: submitting ? "not-allowed" : "pointer",
            }}
          >
            Cancel
          </button>
          <button
            type="button" disabled={!canSubmit} onClick={handleSubmit}
            style={{
              padding: "8px 16px", borderRadius: 6,
              backgroundColor: canSubmit ? "#3b82f6" : "#cbd5e1",
              color: "#fff", border: "none", fontSize: 13, fontWeight: 700,
              cursor: canSubmit ? "pointer" : "not-allowed",
            }}
          >
            {submitting ? "Saving..." : mode === "edit" ? "Update connection" : "Save connection"}
          </button>
        </div>
      </div>
    </div>
  );
}
