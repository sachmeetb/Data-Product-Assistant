import { useEffect, useState } from "react";
import api from "../api/client";
import { useNotify } from "./dialogContext";
import type { StageInfo } from "../types";
import { type ServingMode, PATTERN_TO_MODE, STAGE_TO_MODE, MODE_TO_STAGE, MODE_LABELS } from "../lib/servingModes";
import { BindingModal } from "./ObjectStoreServingRow";

interface SourceBinding {
  bound: boolean;
  connection_id?: number;
  connection_name?: string;
  platform_type?: string;
  target_dialect?: string;
  view_target_namespace?: string;
}

// Platforms with a 3-level namespace (catalog.schema.table) whose write target
// can't be inferred from a possibly-read-only source — prompt for it explicitly.
const THREE_LEVEL_PLATFORMS = new Set(["databricks", "snowflake"]);

interface CatalogOption {
  catalog: string;
  schemas: string[];
}
interface TargetNamespaceOptions {
  supported: boolean;
  required?: boolean;
  platform?: string;
  container_parts?: string[];
  catalogs?: CatalogOption[];   // 3-level: catalog → schemas
  schemas?: string[];           // 2-level: flat schema list (Postgres/MySQL)
}

interface PlatformConnection {
  id: number;
  connection_name: string;
  platform_type: string;
  connection_roles?: string[];
}

interface ServingPattern {
  pattern: string;
  label: string;
  feasibility: "feasible" | "impossible" | "not_yet_supported" | "not_applicable";
  feasibility_reason: string;
  recommended?: boolean;
  rationale?: string;
  transform_placement?: { recommended: string; rationale: string };
}

interface ServingAdvice {
  recommended_pattern: string | null;
  recommended_placement: string | null;
  source_platform?: string;
  target_platform?: string;
  patterns: ServingPattern[];
}

interface MaterializationTarget {
  configured: boolean;
  target_connection_id: number | null;
  target?: { platform: string; connection_name?: string; dialect?: string };
}

interface Props {
  open: boolean;
  projectId: number;
  stage: StageInfo | null;
  onClose: () => void;
  onCompleted: () => void;
}

// Serving-mode vocabulary (ServingMode / PATTERN_TO_MODE / MODE_TO_STAGE /
// MODE_LABELS) is shared from ../lib/servingModes.

const PLATFORM_TO_DIALECT: Record<string, string> = {
  postgres: "postgres", postgresql: "postgres", mysql: "mysql",
  snowflake: "snowflake", databricks: "databricks", bigquery: "bigquery",
};

const SOURCE_TARGET = -1; // sentinel: "serve into the source (same instance)"

export default function ConfigureServingDialog({ open, projectId, stage, onClose, onCompleted }: Props) {
  const { showError } = useNotify();
  const [binding, setBinding] = useState<SourceBinding | null>(null);
  const [connections, setConnections] = useState<PlatformConnection[]>([]);
  const [advice, setAdvice] = useState<ServingAdvice | null>(null);
  const [targetConnId, setTargetConnId] = useState<number>(SOURCE_TARGET);
  const [loading, setLoading] = useState(false);
  const [adviceLoading, setAdviceLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [servingMode, setServingMode] = useState<ServingMode>("virtual");
  const [showUnavailable, setShowUnavailable] = useState(false);
  const [targetNamespace, setTargetNamespace] = useState<string>("");
  const [nsOpts, setNsOpts] = useState<TargetNamespaceOptions | null>(null);
  const [nsOptionsLoading, setNsOptionsLoading] = useState(false);
  const [storageConfigured, setStorageConfigured] = useState<boolean | null>(null);
  const [storageName, setStorageName] = useState<string>("");
  const [showBindingModal, setShowBindingModal] = useState(false);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setAdvice(null);
    // 1) Fast local data — render the dialog as soon as these resolve. Each is
    //    individually guarded so one failure never blanks the dialog.
    const wfId = stage?.workflow_id || "";
    Promise.all([
      api.get<SourceBinding>(`/api/projects/${projectId}/source-binding`).catch(() => null),
      // NB: /api/connections returns { connections: [...] }, NOT a bare array.
      api.get<{ connections: PlatformConnection[] }>(`/api/connections`).catch(() => null),
      api.get<MaterializationTarget>(`/api/projects/${projectId}/serving/materialization-target`).catch(() => null),
      api.get<{ view_target_namespace?: string }>(`/api/projects/${projectId}/serving/view-target-namespace`).catch(() => null),
      // The workflow's enabled serving member = the ACTUAL current mode. Seed the
      // toggle from this (not the recommendation) so it reflects reality; the
      // recommendation is surfaced separately as the RECOMMENDED pattern tag.
      wfId ? api.get<{ stages?: Array<{ stage_id?: string }> }>(`/api/projects/${projectId}/workflows/${wfId}`).catch(() => null) : Promise.resolve(null),
      api.get<{ configured: boolean; connection_name?: string | null }>(`/api/projects/${projectId}/serving/storage-status`).catch(() => null),
    ])
      .then(([bindRes, connRes, tgtRes, nsRes, wfRes, storRes]) => {
        if (cancelled) return;
        setBinding(bindRes?.data ?? null);
        // Namespace is stored per-contract on MaterializationTarget (universal —
        // dpe-cf has no SourceBinding), with the binding as a legacy fallback.
        setTargetNamespace(
          nsRes?.data?.view_target_namespace || bindRes?.data?.view_target_namespace || "",
        );
        setConnections(connRes?.data?.connections ?? []);
        setTargetConnId(tgtRes?.data?.target_connection_id ?? SOURCE_TARGET);
        const activeMember = (wfRes?.data?.stages ?? [])
          .map((s) => s.stage_id || "")
          .find((sid) => sid in STAGE_TO_MODE);
        if (activeMember) setServingMode(STAGE_TO_MODE[activeMember]);
        setStorageConfigured(storRes?.data?.configured ?? false);
        setStorageName(storRes?.data?.connection_name ?? "");
      })
      .catch(() => { if (!cancelled) setConnections([]); })
      .finally(() => { if (!cancelled) setLoading(false); });

    return () => { cancelled = true; };
  }, [open, projectId, stage?.workflow_id]);

  // 2) Advisor — fetched SEPARATELY, non-blocking (skip_skill = no slow LLM call),
  //    and RE-RUN whenever the target connection changes so mode feasibility
  //    reflects the CHOSEN target: picking a cross-platform target (e.g. MySQL
  //    source → Databricks) must flip Materialized (dbt) to impossible and enable
  //    Cross-platform transfer. Without this the feasibility stayed pinned to the
  //    same-instance default and wrongly offered dbt for a cross-platform serve.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setAdviceLoading(true);
    const sel = connections.find((c) => c.id === targetConnId);
    const body: Record<string, unknown> = { skip_skill: true };
    // Omit target_platform for the source-is-target case (let the backend resolve
    // the source); pass the selected connection's platform otherwise.
    if (targetConnId !== SOURCE_TARGET && sel) body.target_platform = sel.platform_type;
    api.post<ServingAdvice>(`/api/projects/${projectId}/serving-strategy/advise`, body)
      .then((advRes) => {
        if (cancelled) return;
        setAdvice(advRes?.data ?? null);
        // NB: do NOT seed servingMode from the recommendation — the toggle must
        // reflect the actual active member (set from the workflow fetch above).
      })
      .catch(() => { /* advisor is best-effort — the dialog works without it */ })
      .finally(() => { if (!cancelled) setAdviceLoading(false); });
    return () => { cancelled = true; };
  }, [open, projectId, targetConnId, connections]);

  // 3) Target-namespace options (catalogs + schemas) — best-effort, non-blocking.
  //    Only 3-level platforms return catalogs; a probe failure leaves the field
  //    as free-text. Fetched separately so a slow live probe never blocks the dialog.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setNsOptionsLoading(true);
    setNsOpts(null);
    // When a separate target connection is picked, probe THAT target (like the
    // migration flow) so a cross-platform serve gets the target's catalog/schema
    // picker; otherwise probe the source (source-is-target). Re-runs on change.
    const q = targetConnId !== SOURCE_TARGET ? `?target_connection_id=${targetConnId}` : "";
    api.get<TargetNamespaceOptions>(`/api/projects/${projectId}/serving/target-namespace-options${q}`)
      .then((res) => {
        if (cancelled) return;
        setNsOpts(res?.data ?? null);
      })
      .catch(() => { /* fall back to free-text */ })
      .finally(() => { if (!cancelled) setNsOptionsLoading(false); });
    return () => { cancelled = true; };
  }, [open, projectId, targetConnId]);

  if (!open || !stage) return null;

  const targetConnections = connections.filter((c) => (c.connection_roles ?? ["source"]).includes("target"));
  const feasibleModes = new Set(
    (advice?.patterns ?? [])
      .filter((p) => p.feasibility === "feasible" && PATTERN_TO_MODE[p.pattern])
      .map((p) => PATTERN_TO_MODE[p.pattern]),
  );
  // Per-mode pattern (for the disabled-button tooltip): a "not_applicable"
  // transfer means "precondition not met" (pick a cross-platform target), NOT
  // "coming soon".
  const patternByMode: Partial<Record<ServingMode, { feasibility: string; feasibility_reason: string }>> = {};
  for (const p of advice?.patterns ?? []) {
    const m = PATTERN_TO_MODE[p.pattern];
    if (m && !patternByMode[m]) patternByMode[m] = p;
  }

  const selectedConn = connections.find((c) => c.id === targetConnId) ?? null;
  // For the "source (same instance)" target the dialect follows the SOURCE
  // platform. Prefer the advisor's resolved platform (it resolves the borrowed
  // :CONSUMES source for a consumer with no local binding) over the binding, so
  // the served dialect always matches the real source engine.
  const sameInstancePlatform = (
    binding?.platform_type || advice?.target_platform || advice?.source_platform || ""
  ).toLowerCase();
  const effectiveDialect =
    targetConnId === SOURCE_TARGET
      ? (binding?.target_dialect || PLATFORM_TO_DIALECT[sameInstancePlatform] || "postgres")
      : (PLATFORM_TO_DIALECT[(selectedConn?.platform_type || "").toLowerCase()] || "ansi");

  // The effective TARGET platform (where the view is deployed) — for a separate
  // target connection use its platform, else the same-instance source platform.
  const effectiveTargetPlatform = (
    targetConnId === SOURCE_TARGET
      ? sameInstancePlatform
      : (selectedConn?.platform_type || "").toLowerCase()
  );
  // Show the target-namespace field when the backend reports options for this
  // platform, OR (immediate fallback for the REQUIRED 3-level case, before the
  // live probe returns) when the platform is a known 3-level engine.
  const is3Level =
    nsOpts?.container_parts?.includes("catalog") ??
    THREE_LEVEL_PLATFORMS.has(effectiveTargetPlatform);
  const needsTargetNamespace =
    nsOpts?.supported === true || THREE_LEVEL_PLATFORMS.has(effectiveTargetPlatform);

  // Lakehouse writes Parquet files + a DuckDB catalog to a directory — it has no
  // live-DB target connection, no target schema, and always compiles to DuckDB.
  // So the SQL-target fields (connection / schema / dialect) don't apply; they're
  // hidden below and skipped in handleConfirm.
  const isLakehouse = servingMode === "lakehouse";

  const handleModeSwitch = async (mode: ServingMode) => {
    if (mode === servingMode) return;
    setServingMode(mode);
    try {
      await api.post(`/api/projects/${projectId}/workflows/${stage.workflow_id || ""}/exclusive-group`, {
        group: "serving",
        select_stage_id: MODE_TO_STAGE[mode],
      });
    } catch (e) {
      console.error("Could not switch serving mode:", e);
    }
  };

  const handleConfirm = async () => {
    if (!stage) return;
    setSaving(true);
    try {
      // Authoritatively enforce the selected serving mode on the exclusive group.
      // Idempotent server-side (no-op when already the active member), so this is
      // safe even if the live handleModeSwitch toggle drifted or failed.
      await api.post(`/api/projects/${projectId}/workflows/${stage.workflow_id || ""}/exclusive-group`, {
        group: "serving",
        select_stage_id: MODE_TO_STAGE[servingMode],
      });
      // SQL-target persistence (connection / schema / dialect) applies only to the
      // view + dbt paths. Lakehouse resolves its own on-disk directory target and
      // ignores all three, so skip them entirely (persisting them would silently
      // stash values the export never reads).
      if (!isLakehouse) {
        // Persist the serving target for ALL archetypes (fixes the silent-discard bug).
        if (targetConnId !== SOURCE_TARGET) {
          await api.put(`/api/projects/${projectId}/serving/materialization-target`, {
            target_connection_id: targetConnId,
          });
        } else {
          // Source-is-target (same instance). Clear any prior target-connection and
          // keep the dialect override on the binding when one exists (dpe-sa).
          await api.delete(`/api/projects/${projectId}/serving/materialization-target`).catch(() => {});
          if (binding?.bound && binding.connection_id) {
            await api.put(`/api/projects/${projectId}/source-binding`, {
              connection_id: binding.connection_id,
              default_schema: "",
              target_dialect: effectiveDialect,
            });
          }
        }
        // Persist the target namespace LAST, after the connection-target DELETE
        // above, so it can't be wiped. It lives per-contract on MaterializationTarget
        // so it works for BOTH archetypes (dpe-cf has no SourceBinding).
        if (needsTargetNamespace) {
          await api.put(`/api/projects/${projectId}/serving/view-target-namespace`, {
            view_target_namespace: targetNamespace.trim(),
          });
        }
      }
      // Complete the checkpoint only when the backend will accept it. On
      // Reconfigure the stage is already `complete`; POSTing /complete again
      // 400s ("Stage is complete, cannot complete"). The meaningful reconfigure
      // work (mode switch + target) is already persisted above.
      if (stage.status === "pending" || stage.status === "running") {
        await api.post(
          `/api/projects/${projectId}/stages/${stage.stage_number}/complete`,
          null,
          { params: { workflow_id: stage.workflow_id } },
        );
      }
      onCompleted();
    } catch (e) {
      console.error("configure_serving confirm failed:", e);
      showError(e, { title: "Failed to confirm" });
    } finally {
      setSaving(false);
    }
  };

  const patterns = advice?.patterns ?? [];
  const feasiblePatterns = patterns.filter((p) => p.feasibility === "feasible");
  const unavailablePatterns = patterns.filter((p) => p.feasibility !== "feasible");
  // recommended first
  feasiblePatterns.sort((a, b) => (b.recommended ? 1 : 0) - (a.recommended ? 1 : 0));

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
          width: 660, maxWidth: "calc(100vw - 32px)", maxHeight: "calc(100vh - 40px)",
          overflowY: "auto", backgroundColor: "#fff", borderRadius: 12, padding: 24,
          boxShadow: "0 20px 40px rgba(15, 23, 42, 0.25)",
          display: "flex", flexDirection: "column", gap: 18,
        }}
      >
        <div>
          <div style={{ fontSize: 17, fontWeight: 700, color: "#0f172a" }}>Configure Serving</div>
          <div style={{ fontSize: 12, color: "#64748b", marginTop: 4, lineHeight: 1.5 }}>
            Choose how this product is served, and which target it lands on, before DDL generation runs.
          </div>
        </div>

        {loading ? (
          <div style={{ color: "#94a3b8", fontSize: 13 }}>Loading…</div>
        ) : (
          <>
            {/* Serving mode buttons — gated by advisor feasibility */}
            <div style={{ borderTop: "1px solid #f1f5f9", paddingTop: 16 }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: "#0f172a", marginBottom: 10 }}>
                Serving mode
              </div>
              <div style={{ display: "flex", gap: 10 }}>
                {(["virtual", "materialized", "lakehouse", "transfer"] as const).map((mode) => {
                  const enabled = feasibleModes.size === 0 || feasibleModes.has(mode);
                  const active = servingMode === mode;
                  const modePat = patternByMode[mode];
                  const disabledTitle = modePat?.feasibility === "not_applicable"
                    ? "Select a cross-platform target to enable"
                    : (modePat?.feasibility_reason || "Not feasible for the current source/target");
                  return (
                    <button
                      key={mode}
                      onClick={() => enabled && void handleModeSwitch(mode)}
                      disabled={!enabled}
                      title={!enabled ? disabledTitle : ""}
                      style={{
                        flex: 1, padding: "10px 12px", borderRadius: 8,
                        cursor: enabled ? "pointer" : "not-allowed",
                        border: `2px solid ${active ? "#3b82f6" : "#e2e8f0"}`,
                        backgroundColor: active ? "#eff6ff" : enabled ? "#fff" : "#f8fafc",
                        fontSize: 12.5, fontWeight: 600, lineHeight: 1.3,
                        color: active ? "#1d4ed8" : enabled ? "#334155" : "#cbd5e1",
                      }}
                    >
                      {MODE_LABELS[mode]}
                    </button>
                  );
                })}
              </div>
            </div>

            {/* Transfer: inline callout to bind an S3 object store — data lands in
                Parquet on S3 automatically if a binding is configured. */}
            {servingMode === "transfer" && (
              <div style={{ borderTop: "1px solid #f1f5f9", paddingTop: 16 }}>
                <div style={{ fontSize: 13, fontWeight: 700, color: "#0f172a", marginBottom: 4 }}>
                  Object store (optional — S3 staging)
                </div>
                {storageConfigured ? (
                  <div style={{
                    display: "flex", alignItems: "center", gap: 10,
                    fontSize: 12.5, color: "#0f766e", backgroundColor: "#f0fdfa",
                    border: "1px solid #99f6e4", borderRadius: 8, padding: "10px 12px",
                  }}>
                    <span>✓ Bound to <strong>{storageName}</strong> — Parquet files will be staged to S3 during the transfer run.</span>
                    <button type="button" onClick={() => setShowBindingModal(true)} style={{
                      marginLeft: "auto", flexShrink: 0,
                      fontSize: 12, padding: "3px 10px", border: "1px solid #99f6e4",
                      borderRadius: 5, background: "#fff", color: "#0f766e", cursor: "pointer", fontWeight: 600,
                    }}>Change</button>
                  </div>
                ) : (
                  <div style={{
                    display: "flex", alignItems: "center", gap: 10,
                    fontSize: 12.5, color: "#92400e", backgroundColor: "#fffbeb",
                    border: "1px solid #fcd34d", borderRadius: 8, padding: "10px 12px",
                  }}>
                    <span>No object store bound — transfer output stays local. Configure one to stage Parquet directly to S3.</span>
                    <button type="button" onClick={() => setShowBindingModal(true)} style={{
                      marginLeft: "auto", flexShrink: 0,
                      fontSize: 12, padding: "3px 10px", border: "1px solid #fcd34d",
                      borderRadius: 5, background: "#fff", color: "#92400e", cursor: "pointer", fontWeight: 600,
                    }}>Configure →</button>
                  </div>
                )}
              </div>
            )}

            {/* Lakehouse: no live-DB target — Parquet + DuckDB land in a directory.
                Replace the SQL-target fields with a plain explanation of where the
                output goes and what "target/dialect" mean here (nothing). */}
            {isLakehouse && (
              <div style={{ borderTop: "1px solid #f1f5f9", paddingTop: 16 }}>
                <div style={{ fontSize: 13, fontWeight: 700, color: "#0f172a", marginBottom: 4 }}>
                  Output target
                </div>
                <div style={{
                  fontSize: 12.5, color: "#0f766e", backgroundColor: "#f0fdfa",
                  border: "1px solid #99f6e4", borderRadius: 8, padding: "10px 12px", lineHeight: 1.5,
                }}>
                  This mode extracts the product to <b>Parquet files</b> plus a portable{" "}
                  <b>DuckDB catalog</b> (<code>catalog.duckdb</code>) in the project's{" "}
                  <code>lakehouse/</code> directory — no live database, target schema, or SQL
                  dialect applies (the compile always targets DuckDB). Download the runnable
                  package from the serving stage once the export completes.
                </div>
              </div>
            )}

            {/* Target connection picker — SQL-target modes only (view + dbt) */}
            {!isLakehouse && (
            <div style={{ borderTop: "1px solid #f1f5f9", paddingTop: 16 }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: "#0f172a", marginBottom: 4 }}>
                Target connection
              </div>
              <div style={{ fontSize: 12, color: "#64748b", marginBottom: 10, lineHeight: 1.5 }}>
                Where the product is served. The default serves into the source (same instance);
                pick a registered target connection to serve elsewhere. Dialect follows the target.
              </div>
              <select
                value={targetConnId}
                onChange={(e) => setTargetConnId(Number(e.target.value))}
                style={{
                  padding: "9px 12px", borderRadius: 6, border: "1px solid #cbd5e1",
                  fontSize: 13, fontFamily: "inherit", width: "100%", backgroundColor: "#fff",
                }}
              >
                <option value={SOURCE_TARGET}>
                  Source{binding?.connection_name ? ` — ${binding.connection_name}` : ""} (same instance)
                </option>
                {targetConnections.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.connection_name} ({c.platform_type})
                  </option>
                ))}
              </select>
              <div style={{ fontSize: 11.5, color: "#94a3b8", marginTop: 6 }}>
                Effective dialect: <b>{effectiveDialect}</b>
              </div>
            </div>
            )}

            {/* Target namespace — required for 3-level platforms (Databricks)
                whose source catalog is often read-only, so the view must be
                created in a writable catalog.schema chosen here. Not applicable to
                lakehouse (files on disk, no schema). */}
            {!isLakehouse && needsTargetNamespace && (() => {
              const catalogs = nsOpts?.catalogs ?? [];
              const schemas = nsOpts?.schemas ?? [];
              const selectStyle = {
                padding: "9px 12px", borderRadius: 6, border: "1px solid #cbd5e1",
                fontSize: 13, fontFamily: "inherit", width: "100%", backgroundColor: "#fff",
              } as const;
              // 3-level: dependent catalog → schema. 2-level: single schema.
              const nsParts = targetNamespace.split(".");
              const selCatalog = nsParts[0] || "";
              const selSchema = nsParts.length > 1 ? nsParts.slice(1).join(".") : "";
              const schemasForCatalog =
                catalogs.find((c) => c.catalog === selCatalog)?.schemas ?? [];
              const setNs = (cat: string, sch: string) =>
                setTargetNamespace(sch ? `${cat}.${sch}` : cat);
              // Platform-appropriate example + writable-target caveat (the copy was
              // Databricks-only: workspace.default / read-only `samples` catalog).
              const ns3Example =
                effectiveTargetPlatform === "snowflake" ? "ANALYTICS.PUBLIC"
                : effectiveTargetPlatform === "bigquery" ? "my_project.my_dataset"
                : "workspace.default";
              const writableCaveat =
                effectiveTargetPlatform === "databricks"
                  ? <> — the source catalog is often read-only (e.g. <code>samples</code>), so the view can't land there.</>
                  : effectiveTargetPlatform === "snowflake"
                  ? <> — point it at a writable database (e.g. a dedicated serving DB like <code>DWB_SERVING_DB</code>).</>
                  : <>.</>;
              return (
                <div style={{ borderTop: "1px solid #f1f5f9", paddingTop: 16 }}>
                  <div style={{ fontSize: 13, fontWeight: 700, color: "#0f172a", marginBottom: 4 }}>
                    {is3Level ? "Target catalog.schema" : "Target schema"}
                    {!is3Level && (
                      <span style={{ fontWeight: 500, color: "#94a3b8", fontSize: 11.5, marginLeft: 8 }}>
                        optional
                      </span>
                    )}
                  </div>
                  <div style={{ fontSize: 12, color: "#64748b", marginBottom: 10, lineHeight: 1.5 }}>
                    {is3Level ? (
                      <>Where the {effectiveTargetPlatform} view is created. Pick a <b>writable</b>{" "}
                        <code>catalog.schema</code>{writableCaveat}</>
                    ) : (
                      <>Which {effectiveTargetPlatform === "mysql" ? "database" : "schema"} the view is
                        created in. Leave as default (<code>public</code>) unless you want it elsewhere.</>
                    )}
                  </div>

                  {is3Level && catalogs.length > 0 ? (
                    <>
                      <div style={{ display: "flex", gap: 10 }}>
                        <div style={{ flex: 1 }}>
                          <div style={{ fontSize: 11, color: "#94a3b8", marginBottom: 3 }}>Catalog</div>
                          <select
                            value={catalogs.some((c) => c.catalog === selCatalog) ? selCatalog : ""}
                            onChange={(e) => setNs(e.target.value, "")}
                            style={selectStyle}
                          >
                            <option value="">Select…</option>
                            {catalogs.map((c) => (
                              <option key={c.catalog} value={c.catalog}>{c.catalog}</option>
                            ))}
                          </select>
                        </div>
                        <div style={{ flex: 1 }}>
                          <div style={{ fontSize: 11, color: "#94a3b8", marginBottom: 3 }}>Schema</div>
                          <select
                            value={schemasForCatalog.includes(selSchema) ? selSchema : ""}
                            onChange={(e) => setNs(selCatalog, e.target.value)}
                            disabled={!selCatalog}
                            style={{ ...selectStyle, backgroundColor: selCatalog ? "#fff" : "#f8fafc" }}
                          >
                            <option value="">{selCatalog ? "Select…" : "Pick a catalog first"}</option>
                            {schemasForCatalog.map((s) => (
                              <option key={s} value={s}>{s}</option>
                            ))}
                          </select>
                        </div>
                      </div>
                      <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 6 }}>
                        Target: <b>{targetNamespace || "—"}</b>{" · "}
                        <span onClick={() => setTargetNamespace("")}
                          style={{ color: "#3b82f6", cursor: "pointer" }} title="Clear and type manually">
                          type manually
                        </span>
                      </div>
                    </>
                  ) : !is3Level && (schemas.length > 0 || !nsOptionsLoading) ? (
                    <select
                      value={schemas.includes(targetNamespace) ? targetNamespace : ""}
                      onChange={(e) => setTargetNamespace(e.target.value)}
                      style={selectStyle}
                    >
                      <option value="">Default ({effectiveTargetPlatform === "mysql" ? "connected database" : "public"})</option>
                      {schemas.map((s) => (
                        <option key={s} value={s}>{s}</option>
                      ))}
                    </select>
                  ) : (
                    <>
                      <input
                        type="text"
                        value={targetNamespace}
                        onChange={(e) => setTargetNamespace(e.target.value)}
                        placeholder={is3Level ? ns3Example : "public"}
                        style={selectStyle}
                      />
                      <div style={{ fontSize: 11.5, color: "#94a3b8", marginTop: 6 }}>
                        {nsOptionsLoading
                          ? "Loading available namespaces…"
                          : `Couldn't list ${is3Level ? "catalogs" : "schemas"} — type it, or check the connection.`}
                      </div>
                    </>
                  )}

                  {is3Level && targetNamespace.trim() && !targetNamespace.includes(".") && (
                    <div style={{ fontSize: 11.5, color: "#b45309", marginTop: 6 }}>
                      Expected <code>catalog.schema</code> (two parts) — e.g. <code>{ns3Example}</code>.
                    </div>
                  )}
                </div>
              );
            })()}

            {/* Advisor pattern list */}
            {patterns.length === 0 && adviceLoading && (
              <div style={{ borderTop: "1px solid #f1f5f9", paddingTop: 16, fontSize: 12, color: "#94a3b8" }}>
                Analyzing serving patterns…
              </div>
            )}
            {patterns.length > 0 && (
              <div style={{ borderTop: "1px solid #f1f5f9", paddingTop: 16 }}>
                <div style={{ fontSize: 13, fontWeight: 700, color: "#0f172a", marginBottom: 8 }}>
                  Recommended patterns
                  {advice?.source_platform && (
                    <span style={{ fontWeight: 500, color: "#94a3b8", fontSize: 11.5, marginLeft: 8 }}>
                      {advice.source_platform} → {advice.target_platform}
                    </span>
                  )}
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {feasiblePatterns.map((p) => (
                    <div
                      key={p.pattern}
                      style={{
                        padding: "10px 12px", borderRadius: 8,
                        border: `1px solid ${p.recommended ? "#86efac" : "#e2e8f0"}`,
                        backgroundColor: p.recommended ? "#f0fdf4" : "#fff",
                      }}
                    >
                      <div style={{ fontSize: 12.5, fontWeight: 700, color: "#0f172a" }}>
                        {p.label}
                        {p.recommended && (
                          <span style={{
                            marginLeft: 8, fontSize: 10.5, fontWeight: 700, color: "#15803d",
                            backgroundColor: "#dcfce7", borderRadius: 4, padding: "1px 6px",
                          }}>RECOMMENDED</span>
                        )}
                      </div>
                      <div style={{ fontSize: 11.5, color: "#475569", marginTop: 3, lineHeight: 1.4 }}>
                        {p.rationale || p.feasibility_reason}
                      </div>
                      {p.transform_placement && (
                        <div style={{ fontSize: 11, color: "#64748b", marginTop: 4 }}>
                          Transform placement: <b>{p.transform_placement.recommended}</b> — {p.transform_placement.rationale}
                        </div>
                      )}
                    </div>
                  ))}
                </div>

                {unavailablePatterns.length > 0 && (
                  <div style={{ marginTop: 10 }}>
                    <button
                      onClick={() => setShowUnavailable((v) => !v)}
                      style={{
                        background: "none", border: "none", cursor: "pointer",
                        fontSize: 11.5, fontWeight: 600, color: "#64748b", padding: 0,
                      }}
                    >
                      {showUnavailable ? "▾" : "▸"} Not available ({unavailablePatterns.length})
                    </button>
                    {showUnavailable && (
                      <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 8 }}>
                        {unavailablePatterns.map((p) => (
                          <div
                            key={p.pattern}
                            style={{
                              padding: "8px 10px", borderRadius: 6, border: "1px dashed #e2e8f0",
                              backgroundColor: "#f8fafc", opacity: 0.85,
                            }}
                          >
                            <div style={{ fontSize: 11.5, fontWeight: 600, color: "#64748b" }}>
                              {p.label}
                              <span style={{
                                marginLeft: 6, fontSize: 10, fontWeight: 600,
                                color: p.feasibility === "not_yet_supported" ? "#b45309"
                                  : p.feasibility === "not_applicable" ? "#0ea5e9" : "#94a3b8",
                              }}>
                                {p.feasibility === "not_applicable" ? "N/A"
                                  : p.feasibility === "not_yet_supported" ? "COMING SOON"
                                  : "NOT POSSIBLE"}
                              </span>
                            </div>
                            <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 2, lineHeight: 1.4 }}>
                              {p.feasibility_reason}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}
          </>
        )}

        {showBindingModal && (
          <BindingModal
            projectId={projectId}
            current={{ configured: storageConfigured ?? false }}
            onClose={() => setShowBindingModal(false)}
            onSaved={() => {
              api.get<{ configured: boolean; connection_name?: string | null }>(`/api/projects/${projectId}/serving/storage-status`)
                .then((r) => { setStorageConfigured(r.data.configured); setStorageName(r.data.connection_name ?? ""); })
                .catch(() => {});
            }}
          />
        )}

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
          <button
            type="button" onClick={onClose} disabled={saving}
            style={{
              padding: "8px 16px", borderRadius: 6, backgroundColor: "#fff",
              color: "#334155", border: "1px solid #cbd5e1", fontSize: 13,
              fontWeight: 600, cursor: saving ? "not-allowed" : "pointer",
            }}
          >
            Cancel
          </button>
          <button
            type="button" onClick={() => void handleConfirm()} disabled={saving || loading}
            style={{
              padding: "8px 16px", borderRadius: 6,
              backgroundColor: saving || loading ? "#cbd5e1" : "#3b82f6",
              color: "#fff", border: "none", fontSize: 13, fontWeight: 700,
              cursor: saving || loading ? "not-allowed" : "pointer",
            }}
          >
            {saving ? "Saving…" : "Confirm & Continue"}
          </button>
        </div>
      </div>
    </div>
  );
}
