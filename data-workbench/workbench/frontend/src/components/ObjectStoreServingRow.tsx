/**
 * ObjectStoreServingRow — the "publish data artifacts to object storage" surface
 * for the serving card (ADR-14). Sibling of ServingGitRow: git carries the recipe
 * (view/dbt/lakehouse CODE), the object store carries the DATA (Parquet + manifests).
 *
 * Per-project binding is configured HERE (unlike git, which is global Settings):
 *   status  GET  /api/projects/{id}/serving/storage-status
 *   bind    PUT  /api/projects/{id}/artifact-store-binding {connection_id, bucket, project_prefix}
 *   unbind  DELETE /api/projects/{id}/artifact-store-binding
 *   publish POST /api/projects/{id}/serving/push-to-storage
 */
import { useEffect, useRef, useState } from "react";
import api from "../api/client";
import { useConfirm } from "./dialogContext";

const OBJECT_STORE_PLATFORMS = ["s3", "gcs", "azure_adls"];

interface StorageStatus {
  configured: boolean;
  connection_name?: string | null;
  platform_type?: string | null;
  bucket?: string;
  project_prefix?: string;
  last_run?: {
    run_id: string;
    status: string;
    object_count: number;
    bytes_uploaded: number;
    run_prefix: string;
    created_at: string | null;
    error: string | null;
  } | null;
}

interface Connection {
  id: number;
  connection_name: string;
  platform_type: string;
  host: string;
  database: string;
}

interface PublishResult {
  status: string;
  object_count: number;
  bytes_uploaded: number;
  run_prefix?: string;
  message?: string;
  objects?: { key: string; kind: string }[];
  presigned_urls?: Record<string, string>;
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// ── binding modal ──────────────────────────────────────────────────────────────

export function BindingModal({ projectId, current, onClose, onSaved }: {
  projectId: number;
  current: StorageStatus;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [connections, setConnections] = useState<Connection[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState<number | "">("");
  const [bucket, setBucket] = useState(current.bucket ?? "");
  const [prefix, setPrefix] = useState(current.project_prefix ?? "");
  const [saving, setSaving] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const backdropRef = useRef<HTMLDivElement>(null);
  const confirm = useConfirm();

  useEffect(() => {
    api.get("/api/connections")
      .then((res) => {
        const all: Connection[] = res.data.connections ?? [];
        setConnections(all.filter((c) => OBJECT_STORE_PLATFORMS.includes(c.platform_type)));
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedId) return;
    setSaving(true);
    setError(null);
    try {
      await api.put(`/api/projects/${projectId}/artifact-store-binding`, {
        connection_id: selectedId,
        bucket: bucket.trim(),
        project_prefix: prefix.trim(),
      });
      onSaved();
      onClose();
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(msg ?? "Failed to save binding.");
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    if (!(await confirm({
      title: "Remove object-store binding",
      message: "Stop publishing this project's data artifacts to the object store?",
      confirmLabel: "Remove", tone: "danger",
    }))) return;
    setRemoving(true);
    setError(null);
    try {
      await api.delete(`/api/projects/${projectId}/artifact-store-binding`);
      onSaved();
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
    <div ref={backdropRef}
      onClick={(e) => { if (e.target === backdropRef.current) onClose(); }}
      style={{ position: "fixed", inset: 0, backgroundColor: "rgba(0,0,0,0.35)",
        display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000 }}>
      <div style={{ backgroundColor: "white", borderRadius: 10, padding: 24, width: 440,
        boxShadow: "0 8px 32px rgba(0,0,0,0.18)", maxHeight: "90vh", overflowY: "auto" }}>
        <h3 style={{ margin: "0 0 4px 0", fontSize: 15 }}>Object store — publish target</h3>
        <div style={{ fontSize: 12, color: "#64748b", marginBottom: 16 }}>
          Where this project publishes its generated data artifacts (Parquet + manifests).
        </div>

        {loading ? (
          <div style={{ color: "#94a3b8", fontSize: 13, marginBottom: 16 }}>Loading connections…</div>
        ) : connections.length === 0 ? (
          <div style={{ padding: "10px 14px", borderRadius: 8, backgroundColor: "#fefce8",
            border: "1px solid #fde68a", fontSize: 12, color: "#92400e", marginBottom: 16 }}>
            No object-store connections registered. Go to <strong>Connections</strong> and register an
            S3-compatible connection (platform <code>s3</code>) first.
          </div>
        ) : (
          <form onSubmit={save}>
            <div style={{ marginBottom: 12 }}>
              <label style={labelStyle}>Object-store connection</label>
              <select value={selectedId}
                onChange={(e) => setSelectedId(e.target.value ? Number(e.target.value) : "")}
                style={inputStyle} required>
                <option value="">— select a connection —</option>
                {connections.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.connection_name} ({c.platform_type} · {c.host}/{c.database})
                  </option>
                ))}
              </select>
            </div>
            <div style={{ marginBottom: 12 }}>
              <label style={labelStyle}>Bucket <span style={{ fontWeight: 400, color: "#94a3b8" }}>(optional — defaults to the connection's bucket)</span></label>
              <input value={bucket} onChange={(e) => setBucket(e.target.value)} style={inputStyle}
                placeholder="data-workbench" />
            </div>
            <div style={{ marginBottom: 16 }}>
              <label style={labelStyle}>Key prefix <span style={{ fontWeight: 400, color: "#94a3b8" }}>(optional — defaults to the project code)</span></label>
              <input value={prefix} onChange={(e) => setPrefix(e.target.value)} style={inputStyle}
                placeholder="my-project/" />
              <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 3 }}>
                Objects land at <code>&lt;prefix&gt;/runs/&lt;run_id&gt;/…</code>; a <code>latest.json</code> pointer sits at the prefix root.
              </div>
            </div>

            {error && (
              <div style={{ marginBottom: 10, padding: "6px 10px", borderRadius: 6, fontSize: 12,
                backgroundColor: "#fef2f2", border: "1px solid #fca5a5", color: "#991b1b" }}>
                {error}
              </div>
            )}

            <div style={{ display: "flex", gap: 8, justifyContent: "space-between" }}>
              <div style={{ display: "flex", gap: 8 }}>
                <button type="submit" disabled={saving || !selectedId} style={{
                  padding: "7px 16px", backgroundColor: saving ? "#93c5fd" : "#3b82f6",
                  color: "white", border: "none", borderRadius: 6, fontSize: 13, fontWeight: 600,
                  cursor: saving ? "default" : "pointer" }}>
                  {saving ? "Saving…" : "Save"}
                </button>
                <button type="button" onClick={onClose} style={{
                  padding: "7px 16px", backgroundColor: "white", color: "#374151",
                  border: "1px solid #cbd5e1", borderRadius: 6, fontSize: 13, cursor: "pointer" }}>
                  Cancel
                </button>
              </div>
              {current.configured && (
                <button type="button" onClick={remove} disabled={removing} style={{
                  padding: "7px 14px", backgroundColor: "#fef2f2", color: "#dc2626",
                  border: "1px solid #fca5a5", borderRadius: 6, fontSize: 12, cursor: "pointer" }}>
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

// ── row (main export) ──────────────────────────────────────────────────────────

export default function ObjectStoreServingRow({ projectId }: { projectId: number }) {
  const [status, setStatus] = useState<StorageStatus | null>(null);
  const [showModal, setShowModal] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [result, setResult] = useState<PublishResult | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const refresh = () => {
    api.get(`/api/projects/${projectId}/serving/storage-status`)
      .then((r) => setStatus(r.data))
      .catch(() => setStatus({ configured: false }));
  };
  useEffect(refresh, [projectId]);

  const publish = async () => {
    setPublishing(true);
    setErr(null);
    setResult(null);
    try {
      const r = await api.post(`/api/projects/${projectId}/serving/push-to-storage`);
      setResult(r.data);
      refresh();
    } catch (e) {
      const d = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setErr(d || "Publish failed");
    }
    setPublishing(false);
  };

  if (status === null) return null; // loading

  const heading = (
    <div style={{ fontSize: 11, fontWeight: 700, color: "#475569", textTransform: "uppercase",
      letterSpacing: "0.05em", marginBottom: 6 }}>
      Object store (data artifacts)
    </div>
  );
  const wrap: React.CSSProperties = { marginTop: 16, paddingTop: 12, borderTop: "1px solid #e2e8f0" };
  const btn: React.CSSProperties = {
    fontSize: 12, padding: "4px 10px", border: "1px solid #cbd5e1", borderRadius: 5,
    background: "#fff", color: "#0f766e", cursor: "pointer", fontWeight: 600,
  };

  if (!status.configured) {
    return (
      <div style={wrap}>
        {heading}
        <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
          <span style={{ fontSize: 13, color: "#94a3b8" }}>No publish target set.</span>
          <button type="button" onClick={() => setShowModal(true)} style={btn}>Configure →</button>
        </div>
        {showModal && (
          <BindingModal projectId={projectId} current={status}
            onClose={() => setShowModal(false)} onSaved={refresh} />
        )}
      </div>
    );
  }

  const dataLinks = Object.entries(result?.presigned_urls ?? {})
    .filter(([k]) => k.endsWith(".parquet"));

  return (
    <div style={wrap}>
      {heading}
      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <span style={{ fontSize: 13, color: "#374151" }}>
          <strong>{status.connection_name}</strong>{" "}
          <span style={{ color: "#94a3b8" }}>
            · {status.bucket || "(bucket)"}/{status.project_prefix}
          </span>
        </span>
        <button type="button" onClick={publish} disabled={publishing}
          style={{ ...btn, background: publishing ? "#f1f5f9" : "#fff" }}>
          {publishing ? "Publishing…" : "Publish to Object Store ↑"}
        </button>
        <button type="button" onClick={() => setShowModal(true)}
          style={{ ...btn, color: "#374151" }}>Change</button>
        {err && <span style={{ fontSize: 12, color: "#ef4444" }}>{err}</span>}
      </div>

      {/* Last run (from status) or the just-published result */}
      {status.last_run && !result && (
        <div style={{ fontSize: 12, color: "#64748b", marginTop: 6 }}>
          Last publish: <strong>{status.last_run.status}</strong>
          {status.last_run.status !== "empty" && <> · {status.last_run.object_count} objects · {fmtBytes(status.last_run.bytes_uploaded)}</>}
          {status.last_run.created_at && <> · {new Date(status.last_run.created_at).toLocaleString()}</>}
          {status.last_run.error && <span style={{ color: "#ef4444" }}> · {status.last_run.error}</span>}
        </div>
      )}
      {result && (
        <div style={{ fontSize: 12, color: "#0f766e", marginTop: 6 }}>
          {result.status === "empty" ? (
            <span style={{ color: "#b45309" }}>
              {result.message || "No publishable data artifacts yet — run a lakehouse or Parquet export first."}
            </span>
          ) : (
            <>
              Published <strong>{result.object_count}</strong> objects ({fmtBytes(result.bytes_uploaded)}) to{" "}
              <code>{result.run_prefix}</code>.
              {dataLinks.length > 0 && (
                <ul style={{ margin: "6px 0 0 0", paddingLeft: 18 }}>
                  {dataLinks.map(([key, url]) => (
                    <li key={key} style={{ marginBottom: 2 }}>
                      <a href={url} target="_blank" rel="noreferrer"
                        style={{ color: "#0f766e", textDecoration: "none", fontWeight: 600 }}>
                        {key.split("/").slice(-1)[0]} ↓
                      </a>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </div>
      )}

      {showModal && (
        <BindingModal projectId={projectId} current={status}
          onClose={() => setShowModal(false)} onSaved={refresh} />
      )}
    </div>
  );
}
