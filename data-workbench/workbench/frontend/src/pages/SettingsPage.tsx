import { useEffect, useState } from "react";
import api from "../api/client";

interface UsageSource {
  source: string;
  total_tokens: number;
  input_tokens?: number;
  output_tokens?: number;
  cost_usd: number;
  events: number;
}
interface UsageSummary {
  total_tokens: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  cost_usd: number;
  events: number;
  by_source: UsageSource[];
}

const fmt = (n: number | undefined) => (n ?? 0).toLocaleString();

function GlobalUsageCard() {
  const [u, setU] = useState<UsageSummary | null>(null);
  const [err, setErr] = useState(false);
  useEffect(() => {
    api.get("/api/usage/summary").then((r) => setU(r.data)).catch(() => setErr(true));
  }, []);
  return (
    <fieldset style={{ border: "1px solid #e2e8f0", borderRadius: 8, padding: 16 }}>
      <legend style={{ fontWeight: 600, color: "#334155" }}>LLM Token Usage (global)</legend>
      {err && <div style={{ color: "#94a3b8", fontSize: 13 }}>Usage data unavailable.</div>}
      {!err && !u && <div style={{ color: "#94a3b8", fontSize: 13 }}>Loading…</div>}
      {u && (
        <>
          <div style={{ display: "flex", gap: 24, flexWrap: "wrap", marginBottom: 12 }}>
            {[
              ["Total tokens", fmt(u.total_tokens)],
              ["Input", fmt(u.input_tokens)],
              ["Output", fmt(u.output_tokens)],
              ["Cost (USD)", `$${(u.cost_usd ?? 0).toFixed(2)}`],
              ["LLM calls", fmt(u.events)],
            ].map(([label, val]) => (
              <div key={label} style={{ display: "flex", flexDirection: "column" }}>
                <span style={{ fontSize: 22, fontWeight: 700, color: "#0f172a" }}>{val}</span>
                <span style={{ fontSize: 11, color: "#64748b", textTransform: "uppercase", letterSpacing: "0.04em" }}>{label}</span>
              </div>
            ))}
          </div>
          <div style={{ fontSize: 11, color: "#94a3b8", marginBottom: 6 }}>
            Working tokens (uncached input + output). Cached prompt overhead:{" "}
            {fmt((u.cache_read_tokens || 0) + (u.cache_creation_tokens || 0))} tokens.
          </div>
          {u.by_source.length > 0 && (
            <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ textAlign: "left", color: "#64748b" }}>
                  <th style={{ padding: "4px 6px" }}>Source</th>
                  <th style={{ padding: "4px 6px", textAlign: "right" }}>Tokens</th>
                  <th style={{ padding: "4px 6px", textAlign: "right" }}>Cost</th>
                  <th style={{ padding: "4px 6px", textAlign: "right" }}>Calls</th>
                </tr>
              </thead>
              <tbody>
                {u.by_source.map((s) => (
                  <tr key={s.source} style={{ borderTop: "1px solid #f1f5f9" }}>
                    <td style={{ padding: "4px 6px" }}>{s.source}</td>
                    <td style={{ padding: "4px 6px", textAlign: "right" }}>{fmt(s.total_tokens)}</td>
                    <td style={{ padding: "4px 6px", textAlign: "right" }}>${(s.cost_usd ?? 0).toFixed(2)}</td>
                    <td style={{ padding: "4px 6px", textAlign: "right" }}>{s.events}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </fieldset>
  );
}

interface Settings {
  neo4j_host: string;
  neo4j_port: number;
  neo4j_user: string;
  neo4j_password: string;
  neo4j_database: string;
  neo4j_browser_url: string;
  git_provider: string;
  git_base_url: string;
  git_web_base_url: string;
  git_token: string;
  git_org: string;
  git_auto_push: boolean;
  git_token_set?: boolean;
}

export default function SettingsPage() {
  const [form, setForm] = useState<Settings>({
    neo4j_host: "localhost",
    neo4j_port: 7687,
    neo4j_user: "neo4j",
    neo4j_password: "",
    neo4j_database: "neo4j",
    neo4j_browser_url: "http://localhost:7474",
    git_provider: "",
    git_base_url: "",
    git_web_base_url: "",
    git_token: "",
    git_org: "dataworkbench",
    git_auto_push: false,
  });
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [testingGit, setTestingGit] = useState(false);
  const [gitTest, setGitTest] = useState<{ ok: boolean; detail?: string; error?: string } | null>(null);

  // The server never returns git_token (only git_token_set) — keep the local
  // token input blank so submitting doesn't clobber the stored token.
  const mergeSettings = (data: Partial<Settings>) =>
    setForm((prev) => ({ ...prev, ...data, git_provider: data.git_provider ?? "", git_token: "" }));

  useEffect(() => {
    api.get("/api/settings").then((res) => mergeSettings(res.data)).catch(() => {});
  }, []);

  const set = (key: string, value: string | number | boolean) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setSaved(false);
    try {
      const payload: Record<string, unknown> = { ...form };
      // Only send the token when the user typed one — blank means "keep".
      if (!form.git_token) delete payload.git_token;
      delete payload.git_token_set;
      const res = await api.put("/api/settings", payload);
      mergeSettings(res.data);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (err) {
      console.error(err);
    }
    setSaving(false);
  };

  const handleTestGit = async () => {
    setTestingGit(true);
    setGitTest(null);
    try {
      const r = await api.get("/api/settings/git/test");
      setGitTest(r.data);
    } catch (err) {
      setGitTest({ ok: false, error: (err as Error)?.message || "request failed" });
    }
    setTestingGit(false);
  };

  const inputStyle = {
    padding: "8px 12px",
    borderRadius: 6,
    border: "1px solid #cbd5e1",
    fontSize: 14,
    width: "100%",
    boxSizing: "border-box" as const,
  };

  const labelStyle = { fontSize: 13, fontWeight: 600 as const, color: "#334155", marginBottom: 4 };

  return (
    <div style={{ maxWidth: 560, display: "flex", flexDirection: "column", gap: 24 }}>
    <form onSubmit={handleSave} style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <h2 style={{ margin: 0 }}>Settings</h2>
      <p style={{ color: "#64748b", fontSize: 14, margin: 0 }}>
        Global Neo4j connection settings. New projects will use these as defaults.
      </p>

      <fieldset style={{ border: "1px solid #e2e8f0", borderRadius: 8, padding: 16 }}>
        <legend style={{ fontWeight: 600, color: "#334155" }}>Neo4j Connection</legend>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <div>
            <label style={labelStyle}>Host</label>
            <input style={inputStyle} value={form.neo4j_host} onChange={(e) => set("neo4j_host", e.target.value)} />
          </div>
          <div>
            <label style={labelStyle}>Bolt Port</label>
            <input style={inputStyle} type="number" value={form.neo4j_port} onChange={(e) => set("neo4j_port", parseInt(e.target.value) || 7687)} />
          </div>
          <div>
            <label style={labelStyle}>Username</label>
            <input style={inputStyle} value={form.neo4j_user} onChange={(e) => set("neo4j_user", e.target.value)} />
          </div>
          <div>
            <label style={labelStyle}>Password</label>
            <input style={inputStyle} type="password" value={form.neo4j_password} onChange={(e) => set("neo4j_password", e.target.value)} />
          </div>
          <div style={{ gridColumn: "1 / -1" }}>
            <label style={labelStyle}>Database</label>
            <input style={inputStyle} value={form.neo4j_database} onChange={(e) => set("neo4j_database", e.target.value)} />
          </div>
        </div>
      </fieldset>

      <fieldset style={{ border: "1px solid #e2e8f0", borderRadius: 8, padding: 16 }}>
        <legend style={{ fontWeight: 600, color: "#334155" }}>Neo4j Browser</legend>
        <div>
          <label style={labelStyle}>Browser URL</label>
          <input
            style={inputStyle}
            value={form.neo4j_browser_url}
            onChange={(e) => set("neo4j_browser_url", e.target.value)}
            placeholder="http://localhost:7474"
          />
          <div style={{ fontSize: 12, color: "#94a3b8", marginTop: 4 }}>
            This URL will appear as a link in the header for quick access to the Neo4j Query Browser.
          </div>
        </div>
      </fieldset>

      <fieldset style={{ border: "1px solid #e2e8f0", borderRadius: 8, padding: 16 }}>
        <legend style={{ fontWeight: 600, color: "#334155" }}>Git Integration</legend>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <div>
            <label style={labelStyle}>Provider</label>
            <select
              style={inputStyle}
              value={form.git_provider || ""}
              onChange={(e) => set("git_provider", e.target.value)}
            >
              <option value="">None (disabled)</option>
              <option value="gitea">Gitea</option>
              <option value="github">GitHub</option>
            </select>
          </div>
          <div>
            <label style={labelStyle}>Organisation / user</label>
            <input
              style={inputStyle}
              value={form.git_org || ""}
              onChange={(e) => set("git_org", e.target.value)}
              placeholder="dataworkbench"
            />
          </div>
          {form.git_provider === "gitea" && (
            <>
              <div style={{ gridColumn: "1 / -1" }}>
                <label style={labelStyle}>Base URL (backend → Git)</label>
                <input
                  style={inputStyle}
                  value={form.git_base_url || ""}
                  onChange={(e) => set("git_base_url", e.target.value)}
                  placeholder="http://gitea:3000"
                />
              </div>
              <div style={{ gridColumn: "1 / -1" }}>
                <label style={labelStyle}>Browse URL (for links in the UI)</label>
                <input
                  style={inputStyle}
                  value={form.git_web_base_url || ""}
                  onChange={(e) => set("git_web_base_url", e.target.value)}
                  placeholder="http://localhost:3101"
                />
                <div style={{ fontSize: 12, color: "#94a3b8", marginTop: 4 }}>
                  Origin the "View in Git" links point to (your browser can't reach the
                  backend's internal host). Leave blank to default to http://localhost:3101.
                </div>
              </div>
            </>
          )}
          <div style={{ gridColumn: "1 / -1" }}>
            <label style={labelStyle}>Token (personal access token)</label>
            <input
              style={inputStyle}
              type="password"
              value={form.git_token || ""}
              onChange={(e) => set("git_token", e.target.value)}
              placeholder={form.git_token_set ? "•••••••• (saved — leave blank to keep)" : "Personal access token"}
            />
          </div>
          <div style={{ gridColumn: "1 / -1", display: "flex", alignItems: "center", gap: 8 }}>
            <input
              id="git_auto_push"
              type="checkbox"
              checked={!!form.git_auto_push}
              onChange={(e) => set("git_auto_push", e.target.checked)}
            />
            <label htmlFor="git_auto_push" style={{ ...labelStyle, marginBottom: 0 }}>
              Push automatically after a successful deploy
            </label>
          </div>
          <div style={{ gridColumn: "1 / -1", display: "flex", gap: 12, alignItems: "center" }}>
            <button
              type="button"
              onClick={handleTestGit}
              disabled={testingGit || !form.git_provider}
              style={{
                padding: "6px 14px",
                borderRadius: 6,
                border: "1px solid #cbd5e1",
                background: "#fff",
                color: "#475569",
                fontSize: 13,
                fontWeight: 600,
                cursor: testingGit || !form.git_provider ? "default" : "pointer",
              }}
            >
              {testingGit ? "Testing…" : "Test Connection"}
            </button>
            {gitTest && (
              <span style={{ color: gitTest.ok ? "#22c55e" : "#ef4444", fontSize: 13 }}>
                {gitTest.ok ? gitTest.detail || "Connected" : gitTest.error}
              </span>
            )}
          </div>
          <div style={{ gridColumn: "1 / -1", fontSize: 12, color: "#94a3b8" }}>
            Save settings before testing. Each deployed data product is pushed to its own repository
            (named after the project code) under the organisation above.
          </div>
        </div>
      </fieldset>

      <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
        <button
          type="submit"
          disabled={saving}
          style={{
            padding: "10px 24px",
            borderRadius: 8,
            border: "none",
            backgroundColor: "#3b82f6",
            color: "#fff",
            fontWeight: 700,
            fontSize: 15,
            cursor: saving ? "wait" : "pointer",
          }}
        >
          {saving ? "Saving..." : "Save Settings"}
        </button>
        {saved && <span style={{ color: "#22c55e", fontWeight: 600, fontSize: 14 }}>Saved</span>}
      </div>
    </form>
    <GlobalUsageCard />
    </div>
  );
}
