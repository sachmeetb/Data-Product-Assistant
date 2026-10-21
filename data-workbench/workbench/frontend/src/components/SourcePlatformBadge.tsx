/**
 * SourcePlatformBadge — inline binding widget for the project header.
 *
 * Shows the project's current source platform binding (if any) and lets the
 * engineer change it via a small modal.  Read from GET /api/projects/{id}/source-binding;
 * write via PUT / DELETE on the same endpoint.
 */
import { useEffect, useRef, useState } from "react";
import api from "../api/client";
import { useConfirm } from "./dialogContext";

interface Binding {
  bound: boolean;
  connection_id?: number;
  connection_name?: string;
  platform_type?: string;
  default_schema?: string;
}

interface Connection {
  id: number;
  connection_name: string;
  platform_type: string;
  host: string;
  database: string;
}

interface Props {
  projectId: number;
  onChanged?: () => void;
}

const PLATFORM_COLOURS: Record<string, { bg: string; fg: string }> = {
  postgres: { bg: "#dbeafe", fg: "#1d4ed8" },
  mysql:    { bg: "#dcfce7", fg: "#15803d" },
};

function PlatformChip({ type }: { type: string }) {
  const c = PLATFORM_COLOURS[type] ?? { bg: "#f1f5f9", fg: "#475569" };
  return (
    <span style={{
      padding: "1px 7px", borderRadius: 9, fontSize: 10, fontWeight: 700,
      backgroundColor: c.bg, color: c.fg, textTransform: "uppercase", letterSpacing: "0.04em",
      display: "inline-block",
    }}>
      {type}
    </span>
  );
}

// ── binding modal ──────────────────────────────────────────────────────────────

interface ModalProps {
  projectId: number;
  current: Binding;
  onClose: () => void;
  onSaved: (b: Binding) => void;
}

function BindingModal({ projectId, current, onClose, onSaved }: ModalProps) {
  const [connections, setConnections] = useState<Connection[]>([]);
  const [loadingConns, setLoadingConns] = useState(true);
  const [selectedId, setSelectedId] = useState<number | "">(current.connection_id ?? "");
  const [schema, setSchema] = useState(current.default_schema ?? "");
  const [saving, setSaving] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const backdropRef = useRef<HTMLDivElement>(null);
  const confirm = useConfirm();

  useEffect(() => {
    api.get("/api/connections")
      .then(res => setConnections(res.data.connections ?? []))
      .catch(() => {})
      .finally(() => setLoadingConns(false));
  }, []);

  const handleBackdropClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (e.target === backdropRef.current) onClose();
  };

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedId) return;
    setSaving(true);
    setError(null);
    try {
      const res = await api.put(`/api/projects/${projectId}/source-binding`, {
        connection_id: selectedId,
        default_schema: schema,
      });
      onSaved({ ...res.data });
      onClose();
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(msg ?? "Failed to save binding.");
    } finally {
      setSaving(false);
    }
  };

  const handleRemove = async () => {
    if (!(await confirm({
      title: "Remove source binding",
      message: "Remove the source platform binding? The project will fall back to its default Postgres connection.",
      confirmLabel: "Remove",
      tone: "danger",
    }))) return;
    setRemoving(true);
    setError(null);
    try {
      await api.delete(`/api/projects/${projectId}/source-binding`);
      onSaved({ bound: false });
      onClose();
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(msg ?? "Failed to remove binding.");
      setRemoving(false);
    }
  };

  const inputStyle: React.CSSProperties = {
    width: "100%", padding: "6px 10px", borderRadius: 6,
    border: "1px solid #cbd5e1", fontSize: 13, boxSizing: "border-box",
  };
  const labelStyle: React.CSSProperties = {
    fontSize: 12, fontWeight: 600, color: "#475569", marginBottom: 3, display: "block",
  };

  return (
    <div
      ref={backdropRef}
      onClick={handleBackdropClick}
      style={{
        position: "fixed", inset: 0, backgroundColor: "rgba(0,0,0,0.35)",
        display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000,
      }}
    >
      <div style={{
        backgroundColor: "white", borderRadius: 10, padding: 24, width: 420,
        boxShadow: "0 8px 32px rgba(0,0,0,0.18)", maxHeight: "90vh", overflowY: "auto",
      }}>
        <h3 style={{ margin: "0 0 16px 0", fontSize: 15 }}>Source Platform</h3>

        {loadingConns ? (
          <div style={{ color: "#94a3b8", fontSize: 13, marginBottom: 16 }}>Loading connections…</div>
        ) : connections.length === 0 ? (
          <div style={{
            padding: "10px 14px", borderRadius: 8, backgroundColor: "#fefce8",
            border: "1px solid #fde68a", fontSize: 12, color: "#92400e", marginBottom: 16,
          }}>
            No connections registered. Go to <strong>Connections</strong> in the nav to register one first.
          </div>
        ) : (
          <form onSubmit={handleSave}>
            <div style={{ marginBottom: 12 }}>
              <label style={labelStyle}>Connection</label>
              <select
                value={selectedId}
                onChange={e => setSelectedId(e.target.value ? Number(e.target.value) : "")}
                style={inputStyle}
                required
              >
                <option value="">— select a connection —</option>
                {connections.map(c => (
                  <option key={c.id} value={c.id}>
                    {c.connection_name} ({c.platform_type} · {c.host}/{c.database})
                  </option>
                ))}
              </select>
            </div>
            <div style={{ marginBottom: 16 }}>
              <label style={labelStyle}>Default Schema <span style={{ fontWeight: 400, color: "#94a3b8" }}>(optional)</span></label>
              <input
                value={schema}
                onChange={e => setSchema(e.target.value)}
                style={inputStyle}
                placeholder="e.g. public"
              />
              <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 3 }}>
                If set, discovery and profiling stages will default to this schema.
              </div>
            </div>

            {error && (
              <div style={{
                marginBottom: 10, padding: "6px 10px", borderRadius: 6, fontSize: 12,
                backgroundColor: "#fef2f2", border: "1px solid #fca5a5", color: "#991b1b",
              }}>
                {error}
              </div>
            )}

            <div style={{ display: "flex", gap: 8, justifyContent: "space-between" }}>
              <div style={{ display: "flex", gap: 8 }}>
                <button type="submit" disabled={saving || !selectedId} style={{
                  padding: "7px 16px", backgroundColor: saving ? "#93c5fd" : "#3b82f6",
                  color: "white", border: "none", borderRadius: 6, fontSize: 13, fontWeight: 600,
                  cursor: saving ? "default" : "pointer",
                }}>
                  {saving ? "Saving…" : "Save"}
                </button>
                <button type="button" onClick={onClose} style={{
                  padding: "7px 16px", backgroundColor: "white", color: "#374151",
                  border: "1px solid #cbd5e1", borderRadius: 6, fontSize: 13, cursor: "pointer",
                }}>
                  Cancel
                </button>
              </div>
              {current.bound && (
                <button type="button" onClick={handleRemove} disabled={removing} style={{
                  padding: "7px 14px", backgroundColor: "#fef2f2", color: "#dc2626",
                  border: "1px solid #fca5a5", borderRadius: 6, fontSize: 12, cursor: "pointer",
                }}>
                  {removing ? "Removing…" : "Remove binding"}
                </button>
              )}
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

// ── badge (main export) ────────────────────────────────────────────────────────

export default function SourcePlatformBadge({ projectId, onChanged }: Props) {
  const [binding, setBinding] = useState<Binding | null>(null);
  const [showModal, setShowModal] = useState(false);

  useEffect(() => {
    api.get(`/api/projects/${projectId}/source-binding`)
      .then(res => setBinding(res.data))
      .catch(() => setBinding({ bound: false }));
  }, [projectId]);

  const handleSaved = (b: Binding) => {
    setBinding(b);
    onChanged?.();
  };

  if (binding === null) {
    // Still loading.
    return <span style={{ color: "#94a3b8" }}>…</span>;
  }

  if (!binding.bound) {
    return (
      <>
        <span style={{ color: "#94a3b8", fontSize: 13 }}>no source</span>
        {" "}
        <button
          onClick={() => setShowModal(true)}
          title="Bind a non-Postgres source platform to this project"
          style={{
            padding: "1px 8px", fontSize: 11, border: "1px solid #bfdbfe",
            borderRadius: 9, backgroundColor: "#eff6ff", color: "#1d4ed8",
            cursor: "pointer", fontWeight: 600, verticalAlign: "middle",
          }}
        >
          + Source
        </button>
        {showModal && (
          <BindingModal
            projectId={projectId}
            current={binding}
            onClose={() => setShowModal(false)}
            onSaved={handleSaved}
          />
        )}
      </>
    );
  }

  return (
    <>
      <PlatformChip type={binding.platform_type ?? "unknown"} />
      {" "}
      <span style={{ fontSize: 13, color: "#374151" }}>{binding.connection_name}</span>
      {binding.default_schema && (
        <span style={{ fontSize: 12, color: "#94a3b8" }}> / {binding.default_schema}</span>
      )}
      {" "}
      <button
        onClick={() => setShowModal(true)}
        title="Change source platform binding"
        style={{
          padding: "1px 8px", fontSize: 11, border: "1px solid #cbd5e1",
          borderRadius: 9, backgroundColor: "white", color: "#374151",
          cursor: "pointer", fontWeight: 600, verticalAlign: "middle",
        }}
      >
        Change
      </button>
      {showModal && (
        <BindingModal
          projectId={projectId}
          current={binding}
          onClose={() => setShowModal(false)}
          onSaved={handleSaved}
        />
      )}
    </>
  );
}
