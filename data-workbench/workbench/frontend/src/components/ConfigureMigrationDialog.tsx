import { useEffect, useState } from "react";
import api from "../api/client";
import type { StageInfo } from "../types";

interface PlatformConnection {
  id: number;
  connection_name: string;
  platform_type: string;
  connection_roles?: string[];
}

interface CatalogOption { catalog: string; schemas: string[]; }
interface NamespaceOptions {
  supported: boolean;
  required?: boolean;
  platform?: string;
  container_parts?: string[];
  catalogs?: CatalogOption[];   // 3-level: catalog → schemas
  schemas?: string[];           // 2-level: flat schema list
}

// Target-intent platform choices (match platform/registry.py ids). Used only in
// schema-only intent mode, where no live PlatformConnection exists yet.
const TARGET_PLATFORMS: { id: string; label: string }[] = [
  { id: "postgres", label: "PostgreSQL" },
  { id: "mysql", label: "MySQL" },
  { id: "snowflake", label: "Snowflake" },
  { id: "databricks", label: "Databricks" },
  { id: "oracle", label: "Oracle" },
  { id: "sqlserver", label: "SQL Server" },
];

interface Props {
  open: boolean;
  projectId: number;
  stage: StageInfo | null;
  onClose: () => void;
  onCompleted: () => void;
}

// Configure Migration dialog (dmig_configure). Picks the TARGET connection, the
// write disposition, and an optional target schema, then POSTs
// /migration/configure and marks the stage complete. Landing strategy is fixed to
// "raw" (lift-and-shift) in this phase. Mirrors ConfigureServingDialog's shape.
export default function ConfigureMigrationDialog({ open, projectId, stage, onClose, onCompleted }: Props) {
  const [connections, setConnections] = useState<PlatformConnection[]>([]);
  const [targetId, setTargetId] = useState<number | "">("");
  // Target INTENT mode (D2): a schema-only project with no live target connection
  // picks a platform + namespace directly. Not executable until a real connection
  // is bound (run/reconcile stay gated).
  const [useIntent, setUseIntent] = useState(false);
  const [intentPlatform, setIntentPlatform] = useState("");
  const [writeDisposition, setWriteDisposition] = useState("replace");
  const [targetSchema, setTargetSchema] = useState("");
  const [targetCatalog, setTargetCatalog] = useState("");
  const [nsOptions, setNsOptions] = useState<NamespaceOptions | null>(null);
  const [nsLoading, setNsLoading] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setError(null);
    setLoading(true);
    Promise.all([
      api.get<{ connections: PlatformConnection[] }>(`/api/connections`).catch(() => ({ data: { connections: [] as PlatformConnection[] } })),
      api.get(`/api/projects/${projectId}/migration/status`).catch(() => null),
    ])
      .then(([connRes, statusRes]) => {
        // /api/connections returns { connections: [...] }, not a bare array.
        const all = connRes.data?.connections || [];
        // Target-capable connections (role "target", or roles unset → allow).
        const conns = all.filter(
          (c) => !c.connection_roles || c.connection_roles.length === 0 || c.connection_roles.includes("target"),
        );
        setConnections(conns);
        const status = statusRes?.data;
        if (status?.configured) {
          if (status.target_connection_id) setTargetId(status.target_connection_id);
          if (status.write_disposition) setWriteDisposition(status.write_disposition);
          if (status.target_catalog) setTargetCatalog(status.target_catalog);
          if (status.target_intent_only) { setUseIntent(true); setIntentPlatform(status.target_platform || ""); }
        }
        // Default to intent mode when there's nothing to connect to yet.
        if (!status?.configured && conns.length === 0) setUseIntent(true);
      })
      .finally(() => setLoading(false));
  }, [open, projectId]);

  // When a target connection is picked, enumerate its landing namespaces so we can
  // offer a writable catalog→schema picker (3-level targets) instead of free text.
  useEffect(() => {
    if (!open || targetId === "") { setNsOptions(null); return; }
    setNsLoading(true);
    api.get(`/api/projects/${projectId}/migration/target-namespace-options`, { params: { connection_id: targetId } })
      .then((r) => setNsOptions(r.data))
      .catch(() => setNsOptions(null))
      .finally(() => setNsLoading(false));
  }, [open, projectId, targetId]);

  if (!open || !stage) return null;

  const handleSave = async () => {
    if (useIntent) {
      if (!intentPlatform) { setError("Pick a target platform."); return; }
    } else {
      if (targetId === "") { setError("Pick a target connection."); return; }
      if (nsOptions?.required && !targetCatalog) { setError("Pick a writable target catalog."); return; }
    }
    setSaving(true);
    setError(null);
    try {
      await api.post(`/api/projects/${projectId}/migration/configure`, {
        // Exactly one of these; the backend records a target intent either way.
        ...(useIntent ? { target_platform: intentPlatform } : { target_connection_id: targetId }),
        landing_strategy: "raw",
        write_disposition: writeDisposition,
        target_schema: targetSchema,
        target_catalog: targetCatalog,
      });
      await api.post(
        `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
        null,
        { params: { workflow_id: stage.workflow_id } },
      );
      onCompleted();
    } catch (e) {
      setError((e as Error).message || "Failed to configure migration.");
    }
    setSaving(false);
  };

  const label = { display: "block", fontSize: 12, fontWeight: 600, color: "#334155", marginBottom: 4 };
  const input = {
    width: "100%", padding: "8px 10px", borderRadius: 6, border: "1px solid #cbd5e1",
    fontSize: 13, marginBottom: 16, boxSizing: "border-box" as const,
  };

  return (
    <div
      style={{
        position: "fixed", inset: 0, background: "rgba(15,23,42,0.45)",
        display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000,
      }}
      onClick={() => { if (!saving) onClose(); }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "#fff", borderRadius: 10, padding: 24, width: 440,
          maxWidth: "92vw", boxShadow: "0 12px 40px rgba(0,0,0,0.25)",
        }}
      >
        <h3 style={{ margin: "0 0 4px", fontSize: 16, color: "#0f172a" }}>Configure Migration</h3>
        <p style={{ margin: "0 0 18px", fontSize: 12.5, color: "#64748b" }}>
          Choose where the source lands. Landing is <strong>raw</strong> (lift-and-shift) — original
          names and types are preserved, no transformation.
        </p>

        {loading ? (
          <p style={{ fontSize: 13, color: "#64748b" }}>Loading connections…</p>
        ) : (
          <>
            <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12.5, color: "#334155", marginBottom: 12, cursor: "pointer" }}>
              <input type="checkbox" checked={useIntent} onChange={(e) => setUseIntent(e.target.checked)} />
              No target connection yet — set a platform intent (schema-only)
            </label>

            {useIntent ? (
              <>
                <label style={label}>Target platform <span style={{ color: "#94a3b8", fontWeight: 400 }}>(intent)</span></label>
                <select
                  value={intentPlatform}
                  onChange={(e) => setIntentPlatform(e.target.value)}
                  style={input as React.CSSProperties}
                >
                  <option value="">— select a target platform —</option>
                  {TARGET_PLATFORMS.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
                </select>
                <p style={{ fontSize: 12, color: "#b45309", marginTop: -10, marginBottom: 14 }}>
                  Records where this migration <em>intends</em> to land. Run &amp; Reconcile stay locked
                  until a real target connection is bound.
                </p>
                {(intentPlatform === "snowflake" || intentPlatform === "databricks") && (
                  <>
                    <label style={label}>Target catalog <span style={{ color: "#94a3b8", fontWeight: 400 }}>(optional)</span></label>
                    <input value={targetCatalog} onChange={(e) => setTargetCatalog(e.target.value)} placeholder="e.g. workspace" style={input as React.CSSProperties} />
                  </>
                )}
              </>
            ) : (
              <>
                <label style={label}>Target connection</label>
                <select
                  value={targetId}
                  onChange={(e) => { setTargetId(e.target.value === "" ? "" : Number(e.target.value)); setTargetCatalog(""); }}
                  style={input as React.CSSProperties}
                >
                  <option value="">— select a target platform —</option>
                  {connections.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.connection_name} ({c.platform_type})
                    </option>
                  ))}
                </select>
                {connections.length === 0 && (
                  <p style={{ fontSize: 12, color: "#b45309", marginTop: -10, marginBottom: 14 }}>
                    No target-capable connections registered. Add one in Settings → Connections (role
                    "target"), or tick the box above to set a platform intent.
                  </p>
                )}
              </>
            )}

            <label style={label}>Write disposition</label>
            <select
              value={writeDisposition}
              onChange={(e) => setWriteDisposition(e.target.value)}
              style={input as React.CSSProperties}
            >
              <option value="replace">replace — overwrite target tables</option>
              <option value="append">append — add rows to target tables</option>
            </select>

            {nsLoading && <p style={{ fontSize: 12, color: "#94a3b8", margin: "0 0 12px" }}>Loading target namespaces…</p>}

            {/* 3-level target (Databricks/Snowflake): pick a WRITABLE catalog, then
                the schema (created if it doesn't exist). 2-level / unsupported:
                free-text schema with existing schemas as hints. */}
            {(() => {
              const catalogs = nsOptions?.catalogs || [];
              const is3Level = !!nsOptions?.supported && catalogs.length > 0;
              const catSchemas = catalogs.find((c) => c.catalog === targetCatalog)?.schemas || [];
              const flatSchemas = nsOptions?.schemas || [];
              const hintSchemas = is3Level ? catSchemas : flatSchemas;
              return (
                <>
                  {is3Level && (
                    <>
                      <label style={label}>Target catalog <span style={{ color: "#94a3b8", fontWeight: 400 }}>(writable)</span></label>
                      <select
                        value={targetCatalog}
                        onChange={(e) => { setTargetCatalog(e.target.value); setTargetSchema(""); }}
                        style={input as React.CSSProperties}
                      >
                        <option value="">— select a writable catalog —</option>
                        {catalogs.map((c) => <option key={c.catalog} value={c.catalog}>{c.catalog}</option>)}
                      </select>
                      {catalogs.length === 0 && (
                        <p style={{ fontSize: 12, color: "#b45309", marginTop: -10, marginBottom: 14 }}>
                          No writable catalogs found on this target. Read-only system catalogs (e.g. samples) are hidden.
                        </p>
                      )}
                    </>
                  )}
                  <label style={label}>
                    Target schema{" "}
                    <span style={{ color: "#94a3b8", fontWeight: 400 }}>
                      {is3Level ? "(created if it doesn't exist)" : "(optional — defaults to the project code)"}
                    </span>
                  </label>
                  <input
                    list={hintSchemas.length ? "mig-target-schemas" : undefined}
                    value={targetSchema}
                    onChange={(e) => setTargetSchema(e.target.value)}
                    placeholder={is3Level ? "e.g. hr_core" : "e.g. migrated"}
                    style={input as React.CSSProperties}
                  />
                  {hintSchemas.length > 0 && (
                    <datalist id="mig-target-schemas">
                      {hintSchemas.map((s) => <option key={s} value={s} />)}
                    </datalist>
                  )}
                </>
              );
            })()}
          </>
        )}

        {error && <p style={{ fontSize: 12.5, color: "#dc2626", marginTop: 0 }}>{error}</p>}

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 8 }}>
          <button
            onClick={() => { if (!saving) onClose(); }}
            style={{
              padding: "8px 14px", borderRadius: 6, border: "1px solid #cbd5e1",
              background: "#fff", color: "#475569", fontSize: 13, fontWeight: 600, cursor: "pointer",
            }}
          >
            Cancel
          </button>
          {(() => {
          const incomplete = useIntent ? !intentPlatform : targetId === "";
          return (
          <button
            onClick={handleSave}
            disabled={saving || loading || incomplete}
            style={{
              padding: "8px 14px", borderRadius: 6, border: "none",
              background: saving || incomplete ? "#93c5fd" : "#2563eb",
              color: "#fff", fontSize: 13, fontWeight: 600,
              cursor: saving || incomplete ? "default" : "pointer",
            }}
          >
            {saving ? "Saving…" : "Save & Continue"}
          </button>
          ); })()}
        </div>
      </div>
    </div>
  );
}
