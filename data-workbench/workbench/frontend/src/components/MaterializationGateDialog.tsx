import { useState, useEffect, useCallback } from "react";
import api from "../api/client";
import { apiErrorMessage } from "../lib/apiError";
import type { StageInfo } from "../types";
import { usePrompt } from "./dialogContext";

// A registered platform connection with the "target" role — the only supported
// materialization target (the old inline-Postgres DSN editor is retired; pick a
// connection registered on the Connections page instead).
interface TargetConnection {
  id: number;
  connection_name: string;
  platform_type: string;
  database: string;
  connection_roles: string[];
}
const SOURCE_TARGET = -1;   // sentinel: use the source connection (default)

/**
 * Verification gate for the materialized (dbt) serving stage.
 *
 * Walks the engineer through sample → review → full build:
 *   1. Build a capped SAMPLE into a `<schema>_preview` schema.
 *   2. Inspect per-model row counts, column types, and first rows.
 *   3. Approve → full build (gate-enforced server-side) → complete the stage,
 *      or Reject → re-blocks the full build until a fresh sample passes.
 *
 * Backend: /serving/materialize (mode sample|full), /serving/materialization/status,
 * /serving/materialization/reject.
 */

interface PreviewModel {
  columns: { name: string; type: string }[];
  rows: (string | null)[][];
  row_count: number | null;
  kind?: "table" | "snapshot";
  error?: string;
}
interface RunInfo {
  status: string;
  schema: string;
  models_json?: string;
  executed_at?: string;
  error?: string;
}
interface GateStatus {
  gate_state: "no_sample" | "awaiting_approval" | "built" | "rejected" | "sample_failed";
  sample: RunInfo | null;
  full: RunInfo | null;
  preview: Record<string, PreviewModel>;
}
interface TargetInfo {
  configured: boolean;
  effective_origin: string | null;
  target_connection_id?: number | null;
  // A connection-based target (e.g. Snowflake/Databricks) returns platform/
  // connection_name and NO port; a legacy DSN target returns host/port/database.
  target?: {
    host?: string; port?: number; database?: string; username?: string; password?: string;
    platform?: string; connection_name?: string; dialect?: string;
  } | null;
}

interface Props {
  open: boolean;
  projectId: number;
  stage: StageInfo | null;
  onClose: () => void;
  onCompleted: () => void;
}

export default function MaterializationGateDialog({ open, projectId, stage, onClose, onCompleted }: Props) {
  const prompt = usePrompt();
  const [status, setStatus] = useState<GateStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sampleLimit, setSampleLimit] = useState(100);
  const [target, setTarget] = useState<TargetInfo | null>(null);
  const [connections, setConnections] = useState<TargetConnection[]>([]);
  const [savingTarget, setSavingTarget] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.get(`/api/projects/${projectId}/serving/materialization/status`);
      setStatus(res.data);
    } catch (e) {
      setError(`Could not load gate status: ${apiErrorMessage(e)}`);
    }
    setLoading(false);
  }, [projectId]);

  const refreshTarget = useCallback(async () => {
    try {
      const res = await api.get(`/api/projects/${projectId}/serving/materialization-target`);
      setTarget(res.data);
    } catch { /* non-fatal — gate still works on the source default */ }
  }, [projectId]);

  const loadConnections = useCallback(async () => {
    try {
      const res = await api.get<{ connections: TargetConnection[] }>(`/api/connections`);
      setConnections(
        (res.data?.connections ?? []).filter(
          (c) => (c.connection_roles ?? ["source"]).includes("target")
        )
      );
    } catch { /* non-fatal — the source-default still works */ }
  }, []);

  useEffect(() => {
    if (open) { refresh(); refreshTarget(); loadConnections(); }
  }, [open, refresh, refreshTarget, loadConnections]);

  if (!open) return null;

  const runSample = async () => {
    setBusy("sample");
    setError(null);
    try {
      const res = await api.post(`/api/projects/${projectId}/serving/materialize`, {
        mode: "sample",
        sample_limit: sampleLimit,
      });
      if (res.data?.status !== "built") throw new Error(res.data?.error || "sample build failed");
      await refresh();
    } catch (e) {
      setError(`Sample build failed: ${apiErrorMessage(e)}`);
    }
    setBusy(null);
  };

  const approveFull = async () => {
    if (!stage) return;
    setBusy("full");
    setError(null);
    try {
      const res = await api.post(`/api/projects/${projectId}/serving/materialize`, { mode: "full" });
      if (res.data?.status !== "built") throw new Error(res.data?.error || "full build failed");
      await api.post(
        `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
        null,
        { params: { workflow_id: stage.workflow_id } }
      );
      onCompleted();
      onClose();
    } catch (e) {
      setError(`Full build failed: ${apiErrorMessage(e)}`);
    }
    setBusy(null);
  };

  const reject = async () => {
    const reason = await prompt({
      title: "Reject sample build",
      label: "Why reject this sample? (helps when re-sampling after a fix)",
      multiline: true,
      confirmLabel: "Reject",
    });
    if (reason === null) return;
    setBusy("reject");
    setError(null);
    try {
      await api.post(`/api/projects/${projectId}/serving/materialization/reject`, { reason });
      await refresh();
    } catch (e) {
      setError(`Reject failed: ${apiErrorMessage(e)}`);
    }
    setBusy(null);
  };

  // Pick a registered target connection (or the source default). The old inline
  // Postgres-only DSN editor is retired — a Snowflake/Databricks/MySQL target is
  // registered on the Connections page (with the "target" role) and selected here.
  const setTargetConnection = async (connId: number) => {
    setSavingTarget(true);
    setError(null);
    try {
      if (connId === SOURCE_TARGET) {
        await api.delete(`/api/projects/${projectId}/serving/materialization-target`);
      } else {
        await api.put(`/api/projects/${projectId}/serving/materialization-target`, {
          target_connection_id: connId,
        });
      }
      await refreshTarget();
    } catch (e) {
      setError(`Could not set target: ${apiErrorMessage(e)}`);
    }
    setSavingTarget(false);
  };

  const gs = status?.gate_state;
  const previewEntries = status ? Object.entries(status.preview) : [];
  // Build defensively — a connection target (Databricks) has no port, so the old
  // `${host}:${port}/${db}` rendered `host:undefined/db`.
  const targetLabel = (() => {
    const t = target?.configured ? target.target : null;
    if (!t) return "source connection (default)";
    const hostPort = t.host ? (t.port ? `${t.host}:${t.port}` : t.host) : "";
    const loc = [hostPort, t.database].filter(Boolean).join("/");
    const name = t.connection_name ? `${t.connection_name} — ` : "";
    const plat = t.platform ? ` (${t.platform})` : "";
    return `${name}${loc}${plat}` || "configured target";
  })();

  return (
    <div
      role="dialog"
      style={{
        position: "fixed", inset: 0, backgroundColor: "rgba(0,0,0,0.4)",
        display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1100,
      }}
      onClick={() => !busy && onClose()}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "#fff", borderRadius: 10, width: "min(880px, 94vw)",
          maxHeight: "88vh", overflow: "auto", padding: 24,
          boxShadow: "0 12px 40px rgba(0,0,0,0.25)",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
          <h2 style={{ margin: 0, fontSize: 18 }}>Materialize — verification gate</h2>
          <button onClick={() => !busy && onClose()} style={{ border: "none", background: "none", fontSize: 22, cursor: "pointer", color: "#64748b" }}>×</button>
        </div>
        <p style={{ marginTop: 0, color: "#64748b", fontSize: 13 }}>
          Build a capped sample, inspect it, then approve the full materialization. The full build is
          blocked until a sample passes.
        </p>

        {error && (
          <div style={{ background: "#fef2f2", border: "1px solid #fecaca", color: "#b91c1c", padding: "8px 12px", borderRadius: 6, fontSize: 13, marginBottom: 12, whiteSpace: "pre-wrap" }}>
            {error}
          </div>
        )}

        {/* Serving strategy was already chosen in Configure Serving — the gate is
            the deploy step, so we don't re-offer the recommendation here. */}

        {/* Target database — where dbt builds the tables, per product. */}
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap",
          border: "1px solid #e2e8f0", borderRadius: 8, padding: "8px 12px", marginBottom: 14, fontSize: 13 }}>
          <span style={{ color: "#475569", fontWeight: 600 }}>Materializes into:</span>
          <code style={{ color: "#0f172a" }}>{targetLabel}</code>
          {target?.configured && (
            <span title="Per-product target connection" style={{ fontSize: 10, fontWeight: 700, color: "#0369a1", background: "#e0f2fe", borderRadius: 4, padding: "2px 6px" }}>CUSTOM TARGET</span>
          )}
          <span style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
            <select
              value={target?.target_connection_id ?? SOURCE_TARGET}
              disabled={savingTarget}
              onChange={(e) => setTargetConnection(Number(e.target.value))}
              style={{ padding: "4px 8px", borderRadius: 6, border: "1px solid #cbd5e1", background: "#fff", color: "#475569", fontSize: 12, fontWeight: 600, cursor: "pointer" }}
            >
              <option value={SOURCE_TARGET}>Source connection (default)</option>
              {connections.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.connection_name} ({c.platform_type})
                </option>
              ))}
            </select>
          </span>
        </div>
        {connections.length === 0 && (
          <div style={{ fontSize: 11, color: "#94a3b8", marginTop: -8, marginBottom: 12 }}>
            To materialize into a different platform (e.g. Snowflake), register a connection with
            the <strong>target</strong> role on the Connections page, then select it here.
          </div>
        )}

        {loading && <div style={{ color: "#64748b", fontSize: 13 }}>Loading…</div>}

        {!loading && status && (
          <>
            <GateBadge state={gs!} sample={status.sample} full={status.full} />

            {/* Step 1: build / re-build a sample */}
            {(gs === "no_sample" || gs === "rejected" || gs === "sample_failed" || gs === "awaiting_approval") && (
              <div style={{ border: "1px solid #e2e8f0", borderRadius: 8, padding: 14, margin: "12px 0" }}>
                <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 8 }}>
                  {gs === "awaiting_approval" ? "Re-build sample" : "Step 1 — build a sample"}
                </div>
                <label style={{ fontSize: 13, color: "#475569" }}>
                  Row cap per table:{" "}
                  <input
                    type="number" min={1} value={sampleLimit}
                    onChange={(e) => setSampleLimit(Math.max(1, parseInt(e.target.value || "1", 10)))}
                    style={{ width: 90, padding: "4px 6px", border: "1px solid #cbd5e1", borderRadius: 6 }}
                  />
                </label>
                <button
                  onClick={runSample}
                  disabled={!!busy}
                  style={primaryBtn(!!busy)}
                >
                  {busy === "sample" ? "Building…" : "Build sample"}
                </button>
              </div>
            )}

            {/* Step 2: preview + approve/reject. Approve/Reject must stay
                reachable even when the preview read came back empty (e.g. the
                DB was briefly unavailable at status time) — otherwise the
                engineer is wedged in awaiting_approval. */}
            {gs === "awaiting_approval" && (
              <div style={{ margin: "12px 0" }}>
                <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 8 }}>Step 2 — inspect sample output</div>
                {previewEntries.length > 0 ? (
                  previewEntries.map(([model, p]) => (
                    <PreviewTable key={model} model={model} p={p} />
                  ))
                ) : (
                  <div style={{ fontSize: 13, color: "#92400e", background: "#fffbeb", borderRadius: 6, padding: "8px 12px" }}>
                    Sample built, but its preview couldn't be loaded right now. You can still approve the full build or reject and re-sample.
                  </div>
                )}
                <div style={{ display: "flex", gap: 10, marginTop: 14 }}>
                  <button onClick={approveFull} disabled={!!busy} style={primaryBtn(!!busy, "#16a34a")}>
                    {busy === "full" ? "Building full…" : "✓ Approve & build full"}
                  </button>
                  <button onClick={reject} disabled={!!busy} style={secondaryBtn(!!busy)}>
                    {busy === "reject" ? "Rejecting…" : "Reject"}
                  </button>
                </div>
              </div>
            )}

            {/* Terminal */}
            {gs === "built" && (
              <div style={{ background: "#f0fdf4", border: "1px solid #bbf7d0", borderRadius: 8, padding: 14, margin: "12px 0" }}>
                <div style={{ fontWeight: 600, color: "#15803d" }}>✓ Materialized</div>
                <div style={{ fontSize: 13, color: "#475569", marginTop: 4 }}>
                  Physical tables built in <code>{status.full?.schema}</code>. Re-build a sample below to re-materialize after changes.
                </div>
                <button onClick={runSample} disabled={!!busy} style={{ ...secondaryBtn(!!busy), marginTop: 10 }}>
                  {busy === "sample" ? "Building…" : "Re-sample"}
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function GateBadge({ state, sample, full }: { state: GateStatus["gate_state"]; sample: RunInfo | null; full: RunInfo | null }) {
  const map: Record<GateStatus["gate_state"], { label: string; bg: string; fg: string }> = {
    no_sample: { label: "No sample yet", bg: "#f1f5f9", fg: "#475569" },
    awaiting_approval: { label: "Sample built — awaiting approval", bg: "#fef9c3", fg: "#854d0e" },
    rejected: { label: "Sample rejected — re-sample to proceed", bg: "#fee2e2", fg: "#b91c1c" },
    sample_failed: { label: "Sample build failed", bg: "#fee2e2", fg: "#b91c1c" },
    built: { label: "Materialized", bg: "#dcfce7", fg: "#15803d" },
  };
  const m = map[state];
  return (
    <div style={{ display: "inline-block", background: m.bg, color: m.fg, padding: "4px 10px", borderRadius: 999, fontSize: 12, fontWeight: 700 }}>
      {m.label}
      {state === "sample_failed" && sample?.error ? `: ${sample.error.slice(0, 120)}` : ""}
      {state === "built" && full?.executed_at ? ` · ${full.executed_at.slice(0, 16).replace("T", " ")}` : ""}
    </div>
  );
}

function PreviewTable({ model, p }: { model: string; p: PreviewModel }) {
  return (
    <div style={{ border: "1px solid #e2e8f0", borderRadius: 8, marginBottom: 12, overflow: "hidden" }}>
      <div style={{ background: "#f8fafc", padding: "6px 12px", fontWeight: 600, fontSize: 13, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span>
          {model}
          {p.kind === "snapshot" && (
            <span title="Materialized as a dbt snapshot — accrues SCD2 history (dbt_valid_from / dbt_valid_to) across builds"
              style={{ marginLeft: 8, fontSize: 10, fontWeight: 700, color: "#7c3aed", background: "#f3e8ff", borderRadius: 4, padding: "2px 6px" }}>
              SCD2 SNAPSHOT
            </span>
          )}
        </span>
        <span style={{ color: "#64748b", fontWeight: 500 }}>{p.row_count ?? "?"} rows (capped) · {p.columns.length} cols</span>
      </div>
      {p.error ? (
        <div style={{ padding: 12, color: "#b91c1c", fontSize: 12 }}>{p.error}</div>
      ) : (
        <div style={{ overflow: "auto", maxHeight: 220 }}>
          <table style={{ borderCollapse: "collapse", fontSize: 12, width: "100%" }}>
            <thead>
              <tr>
                {p.columns.map((c) => (
                  <th key={c.name} style={{ textAlign: "left", padding: "4px 8px", borderBottom: "1px solid #e2e8f0", whiteSpace: "nowrap", position: "sticky", top: 0, background: "#fff" }}>
                    {c.name}<br /><span style={{ color: "#94a3b8", fontWeight: 400 }}>{c.type}</span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {p.rows.map((row, i) => (
                <tr key={i}>
                  {row.map((v, j) => (
                    <td key={j} style={{ padding: "4px 8px", borderBottom: "1px solid #f1f5f9", whiteSpace: "nowrap", maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis" }}>
                      {v ?? <span style={{ color: "#cbd5e1" }}>∅</span>}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function primaryBtn(disabled: boolean, bg = "#3b82f6"): React.CSSProperties {
  return {
    marginLeft: 10, padding: "6px 16px", borderRadius: 6, border: "none",
    backgroundColor: disabled ? "#cbd5e1" : bg, color: "#fff", fontSize: 13,
    fontWeight: 600, cursor: disabled ? "not-allowed" : "pointer",
  };
}
function secondaryBtn(disabled: boolean): React.CSSProperties {
  return {
    padding: "6px 16px", borderRadius: 6, border: "1px solid #cbd5e1",
    backgroundColor: "#fff", color: "#475569", fontSize: 13, fontWeight: 600,
    cursor: disabled ? "not-allowed" : "pointer",
  };
}
