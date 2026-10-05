import { useEffect, useState } from "react";
import QuickConnect, { type ProvisionedSource } from "./QuickConnect";

interface Props {
  open: boolean;
  onClose: () => void;
  onSubmit: (payload: DataSourcePayload) => void | Promise<void>;
  submitting?: boolean;
  /** When provided, the dialog seeds its form from this on open — used by
   *  the edit-after-completion flow so the user can fix a typo without
   *  re-typing the whole connection. */
  initial?: Partial<DataSourcePayload>;
  /** "Configure" for first-time setup, "Edit" for re-opens. Just affects
   *  the title + submit button label so the user knows which mode they're in. */
  mode?: "create" | "edit";
}

// During demos the same connection is typed in over and over — this link
// seeds the credential fields so we don't burn screen time on a known
// localhost test database. Demo-only; not used outside the dialog.
const DEMO_DEFAULTS = {
  database: "h8employee",
  username: "postgres",
  password: "password",
};

export interface DataSourcePayload {
  platform: "postgres";
  host: string;
  port: number;
  database: string;
  username: string;
  password: string;
  schema_name: string | null;
}

const PLATFORMS: Array<{ value: string; label: string; enabled: boolean }> = [
  { value: "postgres", label: "PostgreSQL", enabled: true },
  { value: "snowflake", label: "Snowflake", enabled: false },
  { value: "databricks", label: "Databricks", enabled: false },
];

/**
 * Engineer-facing data-source picker. Credentials get POSTed to
 * /api/projects/{id}/data-source, which registers a structured
 * PlatformConnection + SourceBinding — the single connection contract every
 * downstream stage (discovery, profiling, DQ testing, serving) resolves through.
 */
export default function DataSourceDialog({ open, onClose, onSubmit, submitting = false, initial, mode = "create" }: Props) {
  const [platform, setPlatform] = useState<"postgres">("postgres");
  const [host, setHost] = useState("localhost");
  const [port, setPort] = useState(5432);
  const [database, setDatabase] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [schemaName, setSchemaName] = useState("");

  // Seed form from `initial` whenever the dialog opens, falling back to the
  // localhost/5432 defaults for first-time setup. Re-keying on `open` resets
  // the form between separate edit sessions even if the parent reuses props.
  useEffect(() => {
    if (!open) return;
    setPlatform("postgres");
    setHost(initial?.host ?? "localhost");
    setPort(initial?.port ?? 5432);
    setDatabase(initial?.database ?? "");
    setUsername(initial?.username ?? "");
    setPassword(initial?.password ?? "");
    setSchemaName(initial?.schema_name ?? "");
  }, [open, initial]);

  if (!open) return null;

  const applyDemoDefaults = () => {
    setDatabase(DEMO_DEFAULTS.database);
    setUsername(DEMO_DEFAULTS.username);
    setPassword(DEMO_DEFAULTS.password);
  };

  const applyQuickConnect = (s: ProvisionedSource) => {
    setHost(s.host);
    setPort(s.port);
    setDatabase(s.database);
    setUsername(s.username);
    setPassword(s.password);
    setSchemaName(s.schema || "");
  };

  const canSubmit =
    !submitting && host.trim() && database.trim() && username.trim() && password.length > 0;

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        backgroundColor: "rgba(15, 23, 42, 0.55)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 100,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 560,
          maxWidth: "calc(100vw - 32px)",
          maxHeight: "calc(100vh - 32px)",
          overflowY: "auto",
          backgroundColor: "#fff",
          borderRadius: 12,
          padding: 20,
          boxShadow: "0 20px 40px rgba(15, 23, 42, 0.25)",
          display: "flex",
          flexDirection: "column",
          gap: 14,
        }}
      >
        <div>
          <div style={{ fontSize: 16, fontWeight: 700, color: "#0f172a" }}>
            {mode === "edit" ? "Edit Data Source" : "Select Data Source"}
          </div>
          <div style={{ fontSize: 12, color: "#64748b", marginTop: 4, lineHeight: 1.5 }}>
            {mode === "edit"
              ? "Update the connection — downstream stages will pick up the new credentials on their next run."
              : "Pick the platform and enter connection credentials. Downstream stages (discovery, profiling, DQ testing) use this connection."}
          </div>
        </div>

        <div style={{ display: "flex", gap: 8 }}>
          {PLATFORMS.map((p) => {
            const active = p.enabled && platform === p.value;
            return (
              <button
                key={p.value}
                type="button"
                disabled={!p.enabled}
                onClick={() => p.enabled && setPlatform(p.value as "postgres")}
                style={{
                  flex: 1,
                  padding: "10px 12px",
                  borderRadius: 8,
                  border: `1px solid ${active ? "#3b82f6" : "#e2e8f0"}`,
                  backgroundColor: active ? "#dbeafe" : p.enabled ? "#fff" : "#f1f5f9",
                  color: p.enabled ? "#0f172a" : "#94a3b8",
                  cursor: p.enabled ? "pointer" : "not-allowed",
                  fontWeight: active ? 700 : 500,
                  fontSize: 13,
                  textAlign: "left",
                }}
                title={p.enabled ? undefined : "Coming soon"}
              >
                <div>{p.label}</div>
                {!p.enabled && (
                  <div style={{ fontSize: 10, color: "#94a3b8", marginTop: 2 }}>Coming soon</div>
                )}
              </button>
            );
          })}
        </div>

        {/* Prefill from a `dwb`-launched sample DB (postgres only here). */}
        <QuickConnect platform="postgres" onPick={applyQuickConnect} refreshKey={open} />

        <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 8 }}>
          <Field label="Host">
            <input value={host} onChange={(e) => setHost(e.target.value)} style={inputStyle} placeholder="localhost" />
          </Field>
          <Field label="Port">
            <input
              type="number"
              value={port}
              onChange={(e) => setPort(parseInt(e.target.value) || 0)}
              style={inputStyle}
            />
          </Field>
        </div>

        <Field label="Database">
          <input value={database} onChange={(e) => setDatabase(e.target.value)} style={inputStyle} placeholder="my_db" />
        </Field>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          <Field label="Username">
            <input value={username} onChange={(e) => setUsername(e.target.value)} style={inputStyle} autoComplete="off" />
          </Field>
          <Field label="Password">
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              style={inputStyle}
              autoComplete="off"
            />
          </Field>
        </div>

        <Field label="Schema (optional)">
          <input
            value={schemaName}
            onChange={(e) => setSchemaName(e.target.value)}
            style={inputStyle}
            placeholder="public"
          />
        </Field>

        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, marginTop: 4 }}>
          <button
            type="button"
            onClick={applyDemoDefaults}
            disabled={submitting}
            title="Fill database/username/password with the localhost demo values"
            style={{
              padding: 0,
              background: "transparent",
              border: "none",
              color: "#94a3b8",
              fontSize: 11,
              fontStyle: "italic",
              cursor: submitting ? "not-allowed" : "pointer",
              textDecoration: "underline",
              textUnderlineOffset: 2,
            }}
          >
            Use demo defaults
          </button>
          <div style={{ display: "flex", gap: 10 }}>
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            style={{
              padding: "8px 16px",
              borderRadius: 6,
              backgroundColor: "#fff",
              color: "#334155",
              border: "1px solid #cbd5e1",
              fontSize: 13,
              fontWeight: 600,
              cursor: submitting ? "not-allowed" : "pointer",
            }}
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={!canSubmit}
            onClick={() =>
              onSubmit({
                platform,
                host: host.trim(),
                port,
                database: database.trim(),
                username: username.trim(),
                password,
                schema_name: schemaName.trim() || null,
              })
            }
            style={{
              padding: "8px 16px",
              borderRadius: 6,
              backgroundColor: canSubmit ? "#3b82f6" : "#cbd5e1",
              color: "#fff",
              border: "none",
              fontSize: 13,
              fontWeight: 700,
              cursor: canSubmit ? "pointer" : "not-allowed",
            }}
          >
            {submitting ? "Saving…" : mode === "edit" ? "Update connection" : "Save connection"}
          </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <span style={{ fontSize: 12, fontWeight: 600, color: "#334155" }}>{label}</span>
      {children}
    </label>
  );
}

const inputStyle: React.CSSProperties = {
  padding: "8px 12px",
  borderRadius: 6,
  border: "1px solid #cbd5e1",
  fontSize: 13,
  fontFamily: "inherit",
};
