import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../AuthContext";

export default function LoginPage() {
  const { login, authEnabled, ready } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Auth disabled (local dev) → there's nothing to log into; send them home.
  if (ready && !authEnabled) {
    navigate("/", { replace: true });
    return null;
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await login(email.trim(), password);
      navigate("/", { replace: true });
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail || "Invalid email or password.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{
      minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center",
      background: "#0f172a", fontFamily: "system-ui, sans-serif",
    }}>
      <form onSubmit={submit} style={{
        width: 360, background: "#fff", borderRadius: 12, padding: 32,
        boxShadow: "0 10px 40px rgba(0,0,0,0.3)",
      }}>
        <h1 style={{ fontSize: 20, margin: "0 0 4px" }}>Data Workbench</h1>
        <p style={{ color: "#64748b", fontSize: 13, margin: "0 0 20px" }}>Sign in to continue</p>

        <label style={{ display: "block", fontSize: 12, color: "#334155", marginBottom: 4 }}>Email</label>
        <input
          type="email" value={email} autoFocus
          onChange={(e) => setEmail(e.target.value)}
          style={inputStyle} required
        />

        <label style={{ display: "block", fontSize: 12, color: "#334155", margin: "12px 0 4px" }}>Password</label>
        <input
          type="password" value={password}
          onChange={(e) => setPassword(e.target.value)}
          style={inputStyle} required
        />

        {error && (
          <div style={{ color: "#b91c1c", fontSize: 13, marginTop: 12 }}>{error}</div>
        )}

        <button type="submit" disabled={busy} style={{
          width: "100%", marginTop: 20, padding: "10px 0", borderRadius: 8, border: "none",
          background: busy ? "#94a3b8" : "#4f46e5", color: "#fff", fontSize: 14, fontWeight: 600,
          cursor: busy ? "default" : "pointer",
        }}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  width: "100%", padding: "8px 10px", borderRadius: 8, border: "1px solid #cbd5e1",
  fontSize: 14, boxSizing: "border-box",
};
