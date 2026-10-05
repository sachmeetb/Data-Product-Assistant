import { useEffect, useState } from "react";
import api from "../../api/client";
import { useConfirm } from "../../components/dialogContext";
import QuickConnect, { type ProvisionedSource } from "../../components/QuickConnect";

// ─── types ────────────────────────────────────────────────────────────────────

interface Connection {
  id: number;
  connection_name: string;
  platform_type: string;
  host: string;
  port: number;
  database: string;
  username: string;
  secret_ref: string;
  has_password: boolean;
  extra_config: Record<string, string>;
  connection_roles: string[];
  created_at: string | null;
  updated_at: string | null;
}

interface TestResult {
  ok: boolean;
  error?: string;
  server_version?: string;
  warnings?: string[];
}

const SUPPORTED_PLATFORMS = ["postgres", "mysql", "snowflake", "databricks", "s3"];

// Object-store platforms (adapterKind: object_store) — a publish TARGET for data
// artifacts, not a SQL source. Only s3 is wired end-to-end today (ADR-14).
const OBJECT_STORE_PLATFORMS = ["s3", "gcs", "azure_adls"];

const PLATFORM_DEFAULTS: Record<string, { port: number }> = {
  postgres:   { port: 5432 },
  mysql:      { port: 3306 },
  snowflake:  { port: 443 },
  databricks: { port: 443 },
  s3:         { port: 9000 },
};

// Short one-line descriptions shown under each role toggle.
const CONNECTION_ROLE_DESC: Record<string, string> = {
  source: "The platform this project discovers, profiles, and maps FROM — and where virtual views are deployed.",
  target: "The platform a product is served INTO — materialized tables, a lakehouse export, or a cross-platform transfer.",
};

// ─── platform chip ─────────────────────────────────────────────────────────────

function PlatformChip({ type }: { type: string }) {
  const palette: Record<string, { bg: string; fg: string }> = {
    postgres:   { bg: "#dbeafe", fg: "#1d4ed8" },
    mysql:      { bg: "#dcfce7", fg: "#15803d" },
    snowflake:  { bg: "#e0f2fe", fg: "#0369a1" },
    databricks: { bg: "#fff7ed", fg: "#c2410c" },
    s3:         { bg: "#ccfbf1", fg: "#0f766e" },
    gcs:        { bg: "#ccfbf1", fg: "#0f766e" },
    azure_adls: { bg: "#ccfbf1", fg: "#0f766e" },
  };
  const c = palette[type] ?? { bg: "#f1f5f9", fg: "#475569" };
  return (
    <span style={{
      padding: "2px 8px", borderRadius: 10, fontSize: 11, fontWeight: 700,
      backgroundColor: c.bg, color: c.fg, textTransform: "uppercase", letterSpacing: "0.04em",
    }}>
      {type}
    </span>
  );
}

// ─── role chips ────────────────────────────────────────────────────────────────

function RoleChips({ roles }: { roles: string[] }) {
  return (
    <span style={{ display: "inline-flex", gap: 4 }}>
      {(roles ?? ["source"]).map(r => (
        <span key={r} style={{
          fontSize: 10, fontWeight: 600, padding: "1px 6px", borderRadius: 8,
          backgroundColor: r === "target" ? "#fef3c7" : "#f0fdf4",
          color: r === "target" ? "#92400e" : "#166534",
          border: `1px solid ${r === "target" ? "#fcd34d" : "#86efac"}`,
        }}>
          {r === "target" ? "TARGET" : "SOURCE"}
        </span>
      ))}
    </span>
  );
}

// ─── test result banner ────────────────────────────────────────────────────────

function TestBanner({ result }: { result: TestResult }) {
  if (result.ok) {
    return (
      <div style={{
        marginTop: 6, padding: "6px 10px", borderRadius: 6, fontSize: 12,
        backgroundColor: "#f0fdf4", border: "1px solid #86efac", color: "#166534",
      }}>
        ✓ Connected — {result.server_version ?? "server responded"}
      </div>
    );
  }
  const msg = result.error ?? "Connection failed";
  const warns = result.warnings ?? [];
  return (
    <div style={{
      marginTop: 6, padding: "6px 10px", borderRadius: 6, fontSize: 12,
      backgroundColor: "#fef2f2", border: "1px solid #fca5a5", color: "#991b1b",
    }}>
      ✗ {msg}
      {warns.length > 0 && (
        <ul style={{ margin: "4px 0 0 0", paddingLeft: 16 }}>
          {warns.map((w, i) => <li key={i}>{w}</li>)}
        </ul>
      )}
    </div>
  );
}

// ─── shared form fields ────────────────────────────────────────────────────────

interface FormState {
  platform: string;
  name: string;
  host: string;
  port: number;
  database: string;
  username: string;
  // Unified credential field. Accepts:
  //   hrpass / a Snowflake PAT → submitted via `password` (stored contained,
  //                              never returned by the API, never sent to an LLM)
  //   env:HR_MYSQL_PASSWORD    → submitted via `secret_ref` (a reference only)
  credential: string;
  // Databricks: SQL-warehouse HTTP path.
  extraField: string;
  // Snowflake: warehouse / role / default schema → extra_config.
  warehouse: string;
  role: string;
  schema: string;
  // Object store (s3): endpoint split + region + TLS → extra_config.
  publicEndpoint: string;
  region: string;
  useSsl: boolean;
  roles: string[];
}

const EMPTY_FORM = (platform = "postgres"): FormState => ({
  platform,
  name: "",
  host: "",
  port: PLATFORM_DEFAULTS[platform]?.port ?? 5432,
  database: "",
  username: "",
  credential: "",
  extraField: "",
  warehouse: "",
  role: "",
  schema: "",
  publicEndpoint: "",
  region: "",
  useSsl: false,
  roles: OBJECT_STORE_PLATFORMS.includes(platform) ? ["target"] : ["source"],
});

// A credential is a *reference* (env:/vault:/ssm:/asm:) or a *literal*. References
// go in `secret_ref`; literals (incl. a Snowflake PAT) go in `password` so they
// are contained server-side (stored as direct:…, masked on read) and never land
// in `secret_ref` raw. Blank → neither key (leave unchanged on edit).
function credentialFields(cred: string): { password?: string; secret_ref?: string } {
  const c = cred.trim();
  if (!c) return {};
  if (/^(env|vault|ssm|asm):/.test(c)) return { secret_ref: c };
  return { password: c };
}

function buildExtraConfig(f: FormState): Record<string, string> {
  const extra: Record<string, string> = {};
  if (f.platform === "snowflake") {
    if (f.warehouse.trim()) extra.warehouse = f.warehouse.trim();
    if (f.role.trim()) extra.role = f.role.trim();
    if (f.schema.trim()) extra.schema = f.schema.trim();
  }
  if (f.platform === "databricks" && f.extraField.trim()) extra.http_path = f.extraField.trim();
  if (OBJECT_STORE_PLATFORMS.includes(f.platform)) {
    // Bucket rides `database`; the backend also reads extra_config.bucket, so
    // mirror it there for clarity. The internal endpoint is built from host:port
    // + use_ssl unless overridden; public_endpoint_url is what presigned URLs use.
    if (f.database.trim()) extra.bucket = f.database.trim();
    if (f.publicEndpoint.trim()) extra.public_endpoint_url = f.publicEndpoint.trim();
    if (f.region.trim()) extra.region = f.region.trim();
    extra.use_ssl = f.useSsl ? "true" : "false";
    extra.addressing_style = "path";
  }
  return extra;
}

interface ConnectionFormProps {
  initial: FormState;
  submitLabel: string;
  submitting: boolean;
  error: string | null;
  lockPlatform?: boolean;
  onSubmit: (f: FormState) => void;
  onCancel: () => void;
}

function ConnectionForm({ initial, submitLabel, submitting, error, lockPlatform, onSubmit, onCancel }: ConnectionFormProps) {
  const [f, setF] = useState<FormState>(initial);
  const isObjectStore = OBJECT_STORE_PLATFORMS.includes(f.platform);

  const set = <K extends keyof FormState>(k: K, v: FormState[K]) =>
    setF(prev => ({ ...prev, [k]: v }));

  const toggleRole = (role: string) =>
    setF(prev => ({
      ...prev,
      roles: prev.roles.includes(role)
        ? prev.roles.filter(r => r !== role)
        : [...prev.roles, role],
    }));

  const applyQuickConnect = (s: ProvisionedSource) =>
    setF(prev => ({
      ...prev,
      platform: SUPPORTED_PLATFORMS.includes(s.platform) ? s.platform : prev.platform,
      name: prev.name || s.name,
      host: s.host,
      port: s.port,
      database: s.database,
      username: s.username,
      credential: s.password,
    }));

  const input: React.CSSProperties = {
    width: "100%", padding: "6px 10px", borderRadius: 6,
    border: "1px solid #cbd5e1", fontSize: 13, boxSizing: "border-box",
  };
  const label: React.CSSProperties = {
    fontSize: 12, fontWeight: 600, color: "#475569", marginBottom: 3, display: "block",
  };
  const row: React.CSSProperties = { marginBottom: 12 };

  return (
    <div>
      {/* Prefill from a `dwb`-launched sample DB — create mode only (edit locks
          the platform). Renders nothing when no manifest exists. */}
      {!lockPlatform && (
        <div style={{ marginBottom: 12 }}>
          <QuickConnect onPick={applyQuickConnect} />
        </div>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0 16px" }}>
        {/* Platform */}
        <div style={row}>
          <label style={label}>Platform *</label>
          {lockPlatform ? (
            <div style={{ ...input, backgroundColor: "#f8fafc", color: "#64748b", padding: "7px 10px" }}>
              {f.platform}
            </div>
          ) : (
            <select value={f.platform} onChange={e => {
              const p = e.target.value;
              setF(prev => ({ ...prev, platform: p, port: PLATFORM_DEFAULTS[p]?.port ?? 5432, extraField: "" }));
            }} style={input} required>
              {SUPPORTED_PLATFORMS.map(p => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
          )}
        </div>

        {/* Name */}
        <div style={row}>
          <label style={label}>Connection Name *</label>
          <input value={f.name} onChange={e => set("name", e.target.value)} style={input}
            placeholder="e.g. prod-mysql" required />
        </div>

        {/* Host / endpoint */}
        <div style={row}>
          <label style={label}>{isObjectStore ? "Endpoint Host *" : "Host *"}</label>
          <input value={f.host} onChange={e => set("host", e.target.value)} style={input}
            placeholder={isObjectStore
              ? "seaweedfs (compose) / localhost — blank or 's3.amazonaws.com' for AWS"
              : "db.example.com or mysql-hr (Docker service name)"} required />
        </div>

        {/* Port */}
        <div style={row}>
          <label style={label}>Port</label>
          <input type="number" value={f.port} onChange={e => set("port", Number(e.target.value))}
            style={input} min={1} max={65535} />
        </div>

        {/* Database / bucket */}
        <div style={row}>
          <label style={label}>{isObjectStore ? "Bucket *" : "Database"}</label>
          <input value={f.database} onChange={e => set("database", e.target.value)} style={input}
            placeholder={isObjectStore ? "data-workbench" : "hr_core"} />
        </div>

        {/* Username / access key */}
        <div style={row}>
          <label style={label}>{isObjectStore ? "Access Key ID" : "Username"}</label>
          <input value={f.username} onChange={e => set("username", e.target.value)} style={input}
            placeholder={isObjectStore ? "workbench (access-key id) — blank uses IAM/ambient creds" : "reader"}
            autoComplete="off" />
        </div>

        {/* Platform-specific extra */}
        {f.platform === "snowflake" && (
          <>
            <div style={row}>
              <label style={label}>Warehouse *</label>
              <input value={f.warehouse} onChange={e => set("warehouse", e.target.value)} style={input}
                placeholder="COMPUTE_WH" autoComplete="off" />
            </div>
            <div style={row}>
              <label style={label}>Role</label>
              <input value={f.role} onChange={e => set("role", e.target.value)} style={input}
                placeholder="CORTEX_AGENT_POC_ADMIN_ROLE" autoComplete="off" />
            </div>
            <div style={{ ...row, gridColumn: "1 / -1" }}>
              <label style={label}>Default Schema</label>
              <input value={f.schema} onChange={e => set("schema", e.target.value)} style={input}
                placeholder="PUBLIC (optional)" autoComplete="off" />
              <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 3 }}>
                Host is the Snowflake <strong>account identifier</strong> (e.g.{" "}
                <code>myorg-account_region</code>); warehouse supplies compute; role is optional.
              </div>
            </div>
          </>
        )}
        {f.platform === "databricks" && (
          <div style={{ ...row, gridColumn: "1 / -1" }}>
            <label style={label}>HTTP Path *</label>
            <input value={f.extraField} onChange={e => set("extraField", e.target.value)} style={input}
              placeholder="/sql/1.0/warehouses/abc123" autoComplete="off" required />
          </div>
        )}
        {isObjectStore && (
          <>
            <div style={{ ...row, gridColumn: "1 / -1" }}>
              <label style={label}>Public Endpoint URL</label>
              <input value={f.publicEndpoint} onChange={e => set("publicEndpoint", e.target.value)} style={input}
                placeholder="http://localhost:9000 — what a browser hits for presigned downloads" autoComplete="off" />
              <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 3, lineHeight: 1.5 }}>
                The backend reaches the store at <code>Endpoint Host:Port</code> (e.g. the compose service
                name). Presigned download URLs are signed for THIS <strong>public</strong> URL so a browser
                can open them. Blank → the endpoint host is reused (fine when they're the same).
              </div>
            </div>
            <div style={row}>
              <label style={label}>Region</label>
              <input value={f.region} onChange={e => set("region", e.target.value)} style={input}
                placeholder="us-east-1" autoComplete="off" />
            </div>
            <div style={{ ...row, display: "flex", alignItems: "center", gap: 8, alignSelf: "end" }}>
              <input id="s3-ssl" type="checkbox" checked={f.useSsl}
                onChange={e => set("useSsl", e.target.checked)} style={{ flexShrink: 0 }} />
              <label htmlFor="s3-ssl" style={{ ...label, marginBottom: 0 }}>
                Use TLS (https) — on for AWS/cloud, off for the local fixture
              </label>
            </div>
          </>
        )}

        {/* Unified credential field */}
        <div style={{ ...row, gridColumn: "1 / -1" }}>
          <label style={label}>
            {isObjectStore ? "Secret Access Key or Secret Reference" : "Password / PAT or Secret Reference"}
          </label>
          <input
            type={f.credential.startsWith("env:") ? "text" : "password"}
            value={f.credential}
            onChange={e => set("credential", e.target.value)}
            style={input}
            placeholder="password / Snowflake PAT  —  or  env:MY_DB_PASSWORD"
            autoComplete="new-password"
          />
          <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 3, lineHeight: 1.5 }}>
            Enter the password or token directly (a Snowflake <strong>PAT</strong> is accepted here —
            it authenticates as a password), <em>or</em> type <code>env:VAR_NAME</code> to read a
            reference from an environment variable at runtime. A literal secret is stored contained,
            never returned by the API, and never sent to an LLM — used only for live probes and
            skill-script invocations.
          </div>
        </div>

        {/* Connection roles */}
        <div style={{ ...row, gridColumn: "1 / -1" }}>
          <label style={label}>Platform role</label>
          {(["source", "target"] as const).map((role) => (
            <label key={role} style={{ display: "flex", alignItems: "flex-start", gap: 8, marginBottom: 8, cursor: "pointer" }}>
              <input type="checkbox" checked={f.roles.includes(role)} onChange={() => toggleRole(role)}
                style={{ marginTop: 2, flexShrink: 0 }} />
              <span style={{ fontSize: 12, lineHeight: 1.4 }}>
                <strong>{role === "target" ? "Target Data Platform" : "Source Data Platform"}</strong>
                <span style={{ display: "block", color: "#64748b" }}>{CONNECTION_ROLE_DESC[role]}</span>
              </span>
            </label>
          ))}
          <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 2 }}>
            A platform can be both — e.g. read from it AND serve back into it.
          </div>
        </div>
      </div>

      {error && (
        <div style={{ color: "#dc2626", fontSize: 12, marginBottom: 8, padding: "6px 10px",
          backgroundColor: "#fef2f2", borderRadius: 6, border: "1px solid #fca5a5" }}>
          {error}
        </div>
      )}

      <div style={{ display: "flex", gap: 8 }}>
        <button type="button" onClick={() => onSubmit(f)} disabled={submitting} style={{
          padding: "7px 18px", backgroundColor: submitting ? "#93c5fd" : "#3b82f6",
          color: "white", border: "none", borderRadius: 6, fontSize: 13, fontWeight: 600,
          cursor: submitting ? "default" : "pointer",
        }}>
          {submitting ? "Saving…" : submitLabel}
        </button>
        <button type="button" onClick={onCancel} disabled={submitting} style={{
          padding: "7px 14px", backgroundColor: "white", color: "#374151",
          border: "1px solid #cbd5e1", borderRadius: 6, fontSize: 13, cursor: "pointer",
        }}>
          Cancel
        </button>
      </div>
    </div>
  );
}

// ─── connection row ────────────────────────────────────────────────────────────

interface RowProps {
  conn: Connection;
  onUpdated: (conn: Connection) => void;
  onDeleted: (id: number) => void;
  onClone: (conn: Connection) => void;
  testResult: TestResult | null;
  testLoading: boolean;
  onTest: (id: number) => void;
}

function ConnectionRow({ conn, onUpdated, onDeleted, onClone, testResult, testLoading, onTest }: RowProps) {
  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const confirm = useConfirm();

  const handleDelete = async () => {
    if (!(await confirm({
      title: "Delete connection",
      message: `Delete connection "${conn.connection_name}"?`,
      confirmLabel: "Delete",
      tone: "danger",
    }))) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await api.delete(`/api/connections/${conn.id}`);
      onDeleted(conn.id);
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setDeleteError(msg ?? "Delete failed.");
      setDeleting(false);
    }
  };

  const editInitial: FormState = {
    platform: conn.platform_type,
    name: conn.connection_name,
    host: conn.host,
    port: conn.port,
    database: conn.database,
    username: conn.username,
    // Leave blank so the user must re-enter; the row shows "password stored" indicator.
    credential: "",
    extraField: conn.extra_config?.http_path || "",
    warehouse: conn.extra_config?.warehouse || "",
    role: conn.extra_config?.role || "",
    schema: conn.extra_config?.schema || "",
    publicEndpoint: conn.extra_config?.public_endpoint_url || "",
    region: conn.extra_config?.region || "",
    useSsl: String(conn.extra_config?.use_ssl ?? "").toLowerCase() === "true",
    roles: conn.connection_roles ?? ["source"],
  };

  const handleSave = async (f: FormState) => {
    setSaveError(null);
    setSaving(true);
    try {
      const body: Record<string, unknown> = {
        connection_name: f.name,
        host: f.host,
        port: f.port,
        database: f.database,
        username: f.username,
        connection_roles: f.roles,
        extra_config: buildExtraConfig(f),
        ...credentialFields(f.credential),
      };
      const res = await api.put(`/api/connections/${conn.id}`, body);
      onUpdated(res.data);
      setEditing(false);
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setSaveError(msg ?? "Save failed.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{
      border: `1px solid ${editing ? "#93c5fd" : "#e2e8f0"}`,
      borderRadius: 8, backgroundColor: "white", overflow: "hidden",
    }}>
      {/* Summary row */}
      <div style={{ padding: 14 }}>
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12 }}>
          <div style={{ minWidth: 0 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4, flexWrap: "wrap" }}>
              <PlatformChip type={conn.platform_type} />
              <span style={{ fontWeight: 600, fontSize: 14 }}>{conn.connection_name}</span>
              <RoleChips roles={conn.connection_roles} />
            </div>
            <div style={{ fontSize: 12, color: "#64748b" }}>
              {conn.host}:{conn.port} / {conn.database || "(no db)"}
              {conn.username && <> &middot; {conn.username}</>}
              {conn.has_password && (
                <> &middot; <span style={{ color: "#94a3b8" }}>
                  {conn.secret_ref.startsWith("env:") ? `secret: ${conn.secret_ref}` : "password stored"}
                </span></>
              )}
              {!conn.has_password && <> &middot; <span style={{ color: "#f59e0b" }}>no password set</span></>}
            </div>
          </div>
          <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
            <button onClick={() => { setEditing(v => !v); setSaveError(null); }}
              style={{
                padding: "5px 12px", fontSize: 12, fontWeight: 600, borderRadius: 6, cursor: "pointer",
                border: "1px solid #cbd5e1",
                backgroundColor: editing ? "#eff6ff" : "white",
                color: editing ? "#1d4ed8" : "#374151",
              }}>
              {editing ? "Collapse" : "Edit"}
            </button>
            <button onClick={() => onClone(conn)}
              style={{
                padding: "5px 12px", fontSize: 12, fontWeight: 600, borderRadius: 6, cursor: "pointer",
                border: "1px solid #cbd5e1", backgroundColor: "white", color: "#374151",
              }}>
              Clone
            </button>
            <button onClick={() => onTest(conn.id)} disabled={testLoading}
              style={{
                padding: "5px 12px", fontSize: 12, fontWeight: 600, borderRadius: 6, cursor: "pointer",
                border: "1px solid #cbd5e1", backgroundColor: testLoading ? "#f1f5f9" : "white", color: "#374151",
              }}>
              {testLoading ? "Testing…" : "Test"}
            </button>
            <button onClick={handleDelete} disabled={deleting}
              style={{
                padding: "5px 12px", fontSize: 12, fontWeight: 600, borderRadius: 6, cursor: "pointer",
                border: "1px solid #fca5a5", backgroundColor: "#fef2f2", color: "#dc2626",
              }}>
              {deleting ? "Deleting…" : "Delete"}
            </button>
          </div>
        </div>
        {testResult && <TestBanner result={testResult} />}
        {deleteError && (
          <div style={{ marginTop: 6, padding: "6px 10px", borderRadius: 6, fontSize: 12,
            backgroundColor: "#fef2f2", border: "1px solid #fca5a5", color: "#991b1b" }}>
            {deleteError}
          </div>
        )}
      </div>

      {/* Inline edit form */}
      {editing && (
        <div style={{ borderTop: "1px solid #e0f2fe", padding: 16, backgroundColor: "#f8fafc" }}>
          <ConnectionForm
            initial={editInitial}
            submitLabel="Save Changes"
            submitting={saving}
            error={saveError}
            lockPlatform
            onSubmit={handleSave}
            onCancel={() => { setEditing(false); setSaveError(null); }}
          />
        </div>
      )}
    </div>
  );
}

// ─── page ──────────────────────────────────────────────────────────────────────

export default function ConnectionsPage() {
  const [connections, setConnections] = useState<Connection[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [createInitial, setCreateInitial] = useState<FormState>(() => EMPTY_FORM());
  const [createKey, setCreateKey] = useState(0);
  const [createError, setCreateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [testResults, setTestResults] = useState<Record<number, TestResult>>({});
  const [testLoading, setTestLoading] = useState<Set<number>>(new Set());

  const load = async () => {
    try {
      const res = await api.get("/api/connections");
      setConnections(res.data.connections ?? []);
    } catch {
      setError("Failed to load connections.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const handleCreate = async (f: FormState) => {
    setCreateError(null);
    setCreating(true);
    try {
      const res = await api.post("/api/connections", {
        connection_name: f.name,
        platform_type: f.platform,
        host: f.host,
        port: f.port,
        database: f.database,
        username: f.username,
        extra_config: buildExtraConfig(f),
        connection_roles: f.roles,
        ...credentialFields(f.credential),
      });
      setConnections(prev => [...prev, res.data]);
      setShowCreate(false);
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setCreateError(msg ?? "Failed to register connection.");
    } finally {
      setCreating(false);
    }
  };

  const handleUpdated = (conn: Connection) => {
    setConnections(prev => prev.map(c => c.id === conn.id ? conn : c));
  };

  const handleDeleted = (id: number) => {
    setConnections(prev => prev.filter(c => c.id !== id));
    setTestResults(prev => { const next = { ...prev }; delete next[id]; return next; });
  };

  const handleClone = (conn: Connection) => {
    setCreateInitial({
      platform: conn.platform_type,
      name: `${conn.connection_name} (copy)`,
      host: conn.host,
      port: conn.port,
      database: conn.database,
      username: conn.username,
      credential: "",
      extraField: conn.extra_config?.http_path || "",
      warehouse: conn.extra_config?.warehouse || "",
      role: conn.extra_config?.role || "",
      schema: conn.extra_config?.schema || "",
      publicEndpoint: conn.extra_config?.public_endpoint_url || "",
      region: conn.extra_config?.region || "",
      useSsl: String(conn.extra_config?.use_ssl ?? "").toLowerCase() === "true",
      roles: conn.connection_roles ?? ["source"],
    });
    setCreateKey(k => k + 1);
    setShowCreate(true);
    setCreateError(null);
  };

  const handleTest = async (id: number) => {
    setTestLoading(prev => new Set(prev).add(id));
    try {
      const res = await api.post(`/api/connections/${id}/test`, {});
      setTestResults(prev => ({ ...prev, [id]: res.data }));
    } catch {
      setTestResults(prev => ({ ...prev, [id]: { ok: false, error: "Request failed", warnings: [] } }));
    } finally {
      setTestLoading(prev => { const next = new Set(prev); next.delete(id); return next; });
    }
  };

  return (
    <div style={{ maxWidth: 760, margin: "0 auto", padding: "24px 0" }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 20 }}>
        <div>
          <h2 style={{ margin: 0 }}>Data Platform Connections</h2>
          <div style={{ fontSize: 13, color: "#64748b", marginTop: 4 }}>
            Register source and target data platforms. Passwords are never passed to LLM calls —
            connections are used only for live probes and skill-script invocations.
          </div>
        </div>
        <button
          onClick={() => {
            if (!showCreate) { setCreateInitial(EMPTY_FORM()); setCreateKey(k => k + 1); }
            setShowCreate(v => !v);
            setCreateError(null);
          }}
          style={{
            padding: "7px 16px", backgroundColor: showCreate ? "#f1f5f9" : "#3b82f6",
            color: showCreate ? "#374151" : "white",
            border: showCreate ? "1px solid #cbd5e1" : "none",
            borderRadius: 6, fontSize: 13, fontWeight: 600, cursor: "pointer",
          }}
        >
          {showCreate ? "Cancel" : "+ Register Connection"}
        </button>
      </div>

      {/* Create form */}
      {showCreate && (
        <div key={createKey} style={{ marginBottom: 20, padding: 16, border: "1px solid #e2e8f0", borderRadius: 8, backgroundColor: "#f8fafc" }}>
          <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 14, color: "#1e293b" }}>Register a new connection</div>
          <ConnectionForm
            initial={createInitial}
            submitLabel="Register Connection"
            submitting={creating}
            error={createError}
            onSubmit={handleCreate}
            onCancel={() => { setShowCreate(false); setCreateError(null); }}
          />
        </div>
      )}

      {/* List */}
      {loading && <div style={{ color: "#94a3b8", fontSize: 13 }}>Loading…</div>}
      {error && <div style={{ color: "#dc2626", fontSize: 13 }}>{error}</div>}

      {!loading && connections.length === 0 && (
        <div style={{
          padding: 32, textAlign: "center", border: "1px dashed #cbd5e1",
          borderRadius: 8, color: "#94a3b8", fontSize: 13,
        }}>
          No connections registered yet. Click <strong>+ Register Connection</strong> to add one.
        </div>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {connections.map(conn => (
          <ConnectionRow
            key={conn.id}
            conn={conn}
            onUpdated={handleUpdated}
            onDeleted={handleDeleted}
            onClone={handleClone}
            testResult={testResults[conn.id] ?? null}
            testLoading={testLoading.has(conn.id)}
            onTest={handleTest}
          />
        ))}
      </div>

      {/* Note about project binding */}
      {!loading && connections.length > 0 && (
        <div style={{
          marginTop: 20, padding: "10px 14px", borderRadius: 8,
          backgroundColor: "#eff6ff", border: "1px solid #bfdbfe", fontSize: 12, color: "#1e40af",
        }}>
          To use a connection as the source for a project, open the project and use the
          <strong> Source Platform</strong> control in the project header.
        </div>
      )}
    </div>
  );
}
