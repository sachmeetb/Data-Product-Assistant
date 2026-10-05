import { useState } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";
import { engineerTheme, productTheme } from "../theme";
import { useAuth } from "../AuthContext";
import api from "../api/client";

const PREF_KEY = "workbench.preferred";

function readPreferredWorkbench(): "product" | "engineer" | null {
  try {
    const pref = window.localStorage.getItem(PREF_KEY);
    return pref === "product" || pref === "engineer" ? pref : null;
  } catch {
    return null;
  }
}

export default function PersonaChooserPage() {
  const navigate = useNavigate();
  const { authEnabled, user } = useAuth();
  const [rememberNext, setRememberNext] = useState(true);

  // With auth on, the account role dictates the shell — skip the chooser.
  if (authEnabled && user) {
    return <Navigate to={user.role === "owner" ? "/product" : "/engineer"} replace />;
  }

  const preferred = readPreferredWorkbench();
  if (preferred) {
    return <Navigate to={preferred === "product" ? "/product" : "/engineer"} replace />;
  }

  const onChoose = (kind: "product" | "engineer") => {
    try {
      if (rememberNext) {
        window.localStorage.setItem(PREF_KEY, kind);
      } else {
        window.localStorage.removeItem(PREF_KEY);
      }
    } catch {
      /* ignore storage errors */
    }
    navigate(kind === "product" ? "/product" : "/engineer");
  };

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
        backgroundColor: "#f1f5f9",
        fontFamily: "system-ui, sans-serif",
      }}
    >
      <h1 style={{ fontSize: 28, fontWeight: 700, color: "#0f172a", marginBottom: 8 }}>Data Workbench</h1>
      <p style={{ fontSize: 15, color: "#475569", marginBottom: 32 }}>Choose a workbench to continue.</p>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(280px, 1fr))", gap: 20, maxWidth: 720, width: "100%" }}>
        <PersonaCard
          tint={productTheme.accentSoft}
          accent={productTheme.accent}
          title={productTheme.label}
          subtitle="Design & consume data products"
          description="Browse the marketplace, propose new products, or refine existing ones. The right place if you think about what a product should be — not how it gets built."
          cta="Enter Product Workbench"
          persona="po"
          onClick={() => onChoose("product")}
        />
        <PersonaCard
          tint={engineerTheme.accentSoft}
          accent={engineerTheme.accent}
          title={engineerTheme.label}
          subtitle="Build & operate the pipelines"
          description="Run discovery, profiling, mapping, quality testing, scoring, and serving. The right place if you're turning approved product specs into real data."
          cta="Enter Engineering Workbench"
          persona="de"
          onClick={() => onChoose("engineer")}
        />
      </div>

      <label style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 24, fontSize: 13, color: "#475569" }}>
        <input
          type="checkbox"
          checked={rememberNext}
          onChange={(e) => setRememberNext(e.target.checked)}
        />
        Remember my choice next time
      </label>

      <div style={{ marginTop: 32, fontSize: 12, color: "#94a3b8" }}>
        <Link to="/engineer/settings" style={{ color: "#64748b", textDecoration: "none" }}>
          Settings
        </Link>
      </div>
    </div>
  );
}

interface CardProps {
  tint: string;
  accent: string;
  title: string;
  subtitle: string;
  description: string;
  cta: string;
  persona: "po" | "de";
  onClick: () => void;
}

type CopyState = "idle" | "copying" | "copied" | "error";

function PersonaCard({ tint, accent, title, subtitle, description, cta, persona, onClick }: CardProps) {
  const [copyState, setCopyState] = useState<CopyState>("idle");

  const onCopy = async () => {
    setCopyState("copying");
    try {
      const res = await api.get("/api/bootstrap", {
        params: { persona },
        responseType: "text",
      });
      await navigator.clipboard.writeText(String(res.data));
      setCopyState("copied");
      setTimeout(() => setCopyState("idle"), 2500);
    } catch {
      setCopyState("error");
      setTimeout(() => setCopyState("idle"), 2500);
    }
  };

  const copyLabel =
    copyState === "copying"
      ? "Copying…"
      : copyState === "copied"
      ? "✓ Copied to clipboard"
      : copyState === "error"
      ? "Copy failed — try again"
      : "⧉ Copy Claude Code bootstrap prompt";

  return (
    <div
      style={{
        textAlign: "left",
        padding: 24,
        borderRadius: 12,
        border: `1px solid ${accent}`,
        backgroundColor: tint,
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      <div style={{ fontSize: 18, fontWeight: 700, color: "#0f172a" }}>{title}</div>
      <div style={{ fontSize: 13, color: "#334155", fontWeight: 600 }}>{subtitle}</div>
      <div style={{ fontSize: 13, color: "#475569", lineHeight: 1.5 }}>{description}</div>
      <button
        type="button"
        onClick={onClick}
        style={{
          marginTop: 12,
          alignSelf: "flex-start",
          padding: "6px 12px",
          borderRadius: 6,
          backgroundColor: accent,
          color: "#fff",
          fontSize: 13,
          fontWeight: 600,
          border: "none",
          cursor: "pointer",
        }}
      >
        {cta} →
      </button>
      <button
        type="button"
        onClick={onCopy}
        disabled={copyState === "copying"}
        title="Copies a prompt you can paste into a new Claude Code session to connect it to this workbench."
        style={{
          marginTop: 8,
          alignSelf: "flex-start",
          padding: 0,
          background: "none",
          border: "none",
          color: copyState === "error" ? "#b91c1c" : accent,
          fontSize: 12,
          fontWeight: 600,
          cursor: copyState === "copying" ? "default" : "pointer",
          textDecoration: "underline",
        }}
      >
        {copyLabel}
      </button>
    </div>
  );
}
